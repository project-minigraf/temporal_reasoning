"""Write spool for auto-memory facts that arrive while ingestion owns the graph (#379).

Ingestion holds the graph for its whole run so it never drops its handle
mid-run -- on minigraf 2.x every drop runs a full O(graph size) checkpoint,
measured at ~80% of Stage A's write path on a 3.8 GB graph. An out-of-process
auto-memory hook therefore cannot take the graph lock during a run. Instead of
waiting for it (and silently losing the turn's facts when the wait times out),
the hook writes one RECORD here and returns; ingestion drains the spool inside
its own lease.

Layout: one JSON file per record in ``<graph_path>.spool/``. A record is
written to a temporary name and ``os.replace``d into place, so a reader sees a
whole record or nothing -- on every platform, with no file locking. Names sort
by creation time, which is the drain order.

A record carries the extracted facts and the valid time they were extracted
at, so a fact drained minutes later is stored with the time it was learned,
not the time it was drained.

Delivery is at-least-once: a record is removed only after it has been applied,
so a crash between the two re-applies it. That is safe because the facts are
re-transacted at the record's own valid time -- the same (e, a, v, valid-from)
-- which minigraf collapses and the fact index's UNIQUE constraint ignores.

This module does file I/O only; applying a record is mcp_server's job.
"""
import json
import os
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

RECORD_VERSION = 1
_SUFFIX = ".json"
_BAD_SUFFIX = ".bad"


def spool_dir_for(graph_path: str) -> str:
    """The spool directory beside a graph file."""
    return graph_path + ".spool"


def write_record(graph_path: str, facts: List[Dict[str, Any]], valid_from: str) -> str:
    """Spool one batch of extracted facts. Returns the record's path.

    Raises OSError if the record cannot be written; the caller decides
    whether that is fatal (the hooks swallow it, as they swallow every
    memory error).
    """
    spool = spool_dir_for(graph_path)
    os.makedirs(spool, exist_ok=True)
    # time_ns first so names sort in creation order; pid + uuid make two
    # hooks spooling in the same nanosecond distinct.
    name = f"{time.time_ns():020d}-{os.getpid()}-{uuid.uuid4().hex}"
    record = {"v": RECORD_VERSION, "valid_from": valid_from, "facts": facts}
    tmp = os.path.join(spool, f".{name}.tmp")
    final = os.path.join(spool, name + _SUFFIX)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    return final


def _parse(raw: Any) -> Optional[Tuple[List[Dict[str, Any]], str]]:
    if not isinstance(raw, dict) or raw.get("v") != RECORD_VERSION:
        return None
    facts, valid_from = raw.get("facts"), raw.get("valid_from")
    if not isinstance(facts, list) or not isinstance(valid_from, str):
        return None
    for fact in facts:
        if not isinstance(fact, dict) or not all(
            isinstance(fact.get(k), str) for k in ("entity", "attribute", "value")
        ):
            return None
    return facts, valid_from


def pending(graph_path: str) -> Iterator[Tuple[str, Optional[Tuple[List[Dict[str, Any]], str]]]]:
    """Yield (record_path, parsed) for every spooled record, oldest first.

    ``parsed`` is ``(facts, valid_from)``, or None for a record that cannot be
    read or does not have the expected shape -- the caller quarantines it so
    one bad record never blocks the rest of the queue. Temporary files (a
    hook still writing) are never yielded.
    """
    spool = spool_dir_for(graph_path)
    try:
        names = sorted(n for n in os.listdir(spool) if n.endswith(_SUFFIX) and not n.startswith("."))
    except FileNotFoundError:
        return
    for name in names:
        path = os.path.join(spool, name)
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            continue  # drained by someone else since the listing
        except (OSError, ValueError):
            yield path, None
            continue
        yield path, _parse(raw)


def remove(record_path: str) -> None:
    """Delete an applied record. Missing is fine (at-least-once)."""
    try:
        os.remove(record_path)
    except FileNotFoundError:
        pass


def quarantine(record_path: str) -> None:
    """Rename an unreadable record out of the queue, keeping it for inspection."""
    try:
        os.replace(record_path, record_path + _BAD_SUFFIX)
    except FileNotFoundError:
        pass

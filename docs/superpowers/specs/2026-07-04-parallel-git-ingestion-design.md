# Parallel Git-Ingestion Extraction + Crash-Safe Resume — Design Spec

**Issue:** #94
**Date:** 2026-07-04

## Background

`_run_ingestion` in `mcp_server.py` walks git history one commit at a time. Per-commit cost is dominated by subprocess-spawn overhead (`git diff-tree`, one `git show` per changed file) plus tree-sitter parsing — not by DB writes, which complete near-instantly even for heavy commits. Verified while ingesting the arangodb repo (52,665 commits): throughput visibly tracks how many files/functions a commit touches, consistent with subprocess+parse cost being the bottleneck.

Full parallelization across commits isn't safe: `entity_valid_from`/`file_entities` are mutable state read and updated by every commit in strict chronological order (new-vs-modified decisions, bi-temporal `:valid-from` semantics, and removed-ident detection all depend on it), and the DB is single-writer (lock contention already tracked in #84/#91).

Additionally, for large repos this process runs for hours. A Claude Code session (and the MCP server subprocess backing it) can end abruptly at any point during that run. The design must also make shutdown safe and guarantee the next run resumes from the correct point.

## Section 1: Producer/Consumer Split

Split per-commit work into two stages:

- **Producer (parallelized):** for each commit, run `_git_changed_files`, then for each changed file apply the existing parser-support filter (skip if unsupported language) and — for `A`/`M` files — `_git_file_content` + `_extract_from_source`. This is entirely read-only and stateless: no DB access, no shared mutable state.
- **Consumer (unchanged, sequential):** the existing per-commit bookkeeping in `_run_ingestion` — new-vs-modified diffing against `entity_valid_from`/`file_entities`, dep diffing, removed-ident detection, `:contains`/`:depends-on`/close-item transacts, watermark update, checkpoint. This logic is **not modified** by this design; it only changes where its inputs come from.

### New function: `_extract_commit(repo_path, commit_hash) -> List[Tuple[str, str, Optional[Dict]]]`

Runs in a worker thread. Returns one entry per changed file that passes the parser-support filter:
- `status == "D"`: `extracted` is `None` (no content needed).
- `status in ("A", "M")`, content fetched and parsed: `extracted` is the dict from `_extract_from_source`.
- `status in ("A", "M")`, `_git_file_content` raises: the file is **omitted** from the returned list entirely — identical to today's `continue`, so the consumer never sees it for this commit.

### Sliding-window pipeline in `_run_ingestion`

A bounded pipeline replaces the inline extraction call, keeping memory bounded to `pipeline_depth` commits' worth of extracted data rather than buffering all of history:

```python
pending: Deque[Tuple[tuple, "asyncio.Future"]] = deque()
commits_iter = iter(commits)

def submit_next() -> bool:
    try:
        commit = next(commits_iter)
    except StopIteration:
        return False
    fut = loop.run_in_executor(executor, _extract_commit, repo_path, commit[0])
    pending.append((commit, fut))
    return True

for _ in range(pipeline_depth):
    if not submit_next():
        break

while pending:
    if _shutdown_requested.is_set():
        break
    (commit_hash, commit_ts_iso, author, subject), fut = pending.popleft()
    extracted_files = await fut
    submit_next()
    # ... existing consumer body, reading extracted_files instead of
    # calling _git_changed_files/_get_parser/_git_file_content/_extract_from_source inline
```

`pipeline_depth = max_workers * 2` (implementation detail, not user-configurable — no evidence a different multiplier matters).

The executor is created as a context manager (`with concurrent.futures.ThreadPoolExecutor(max_workers=...) as executor:`) wrapping the whole commit loop, so it shuts down cleanly — draining any in-flight (side-effect-free) producer work — on both normal completion and exceptions.

## Section 2: Thread-Local Parsers

Tree-sitter `Parser` objects are not safe for concurrent `.parse()` calls from multiple threads on the same instance. Rather than serialize parsing behind a lock (which would erase the parallelism benefit for parse time), each worker thread gets its **own** `Parser` instance per language.

`_get_parser`/`_grammar_cache` are unchanged in behavior and contract — they remain the single shared cache used directly by existing unit tests (`TestGetParser`, `TestExtractFromSource`, etc.), including the once-per-language "no grammar available" warning. A small `threading.Lock` is added around `_get_parser`'s first-time-construction branch only, so two producer threads racing to discover the same never-before-seen language don't double-import the grammar module or double-print the warning. This does not affect the cached-hit path, which stays a plain dict read.

New helper, used only by producer workers:

```python
_thread_local = threading.local()

def _thread_parser(file_path: str) -> Optional[Any]:
    """Parser private to the calling thread for file_path's language.

    Reuses _get_parser purely as the "is this language supported" check
    (and its shared cache/once-only warning), but builds a separate Parser
    instance per thread so concurrent workers never call .parse() on a
    Parser another thread is also using.
    """
    if _get_parser(file_path) is None:
        return None
    lang_name = _EXT_TO_LANG[Path(file_path).suffix.lower()]
    cache = getattr(_thread_local, "parsers", None)
    if cache is None:
        cache = {}
        _thread_local.parsers = cache
    if lang_name not in cache:
        cache[lang_name] = _build_parser(lang_name)
    return cache[lang_name]
```

`_build_parser(lang_name) -> Optional[Any]` is extracted from `_get_parser`'s existing construction logic (import grammar module, build `Language`, build `Parser`) with no caching or warning side effects of its own — those stay in `_get_parser`. Since `_thread_parser` only calls `_build_parser` after `_get_parser` has already proven the grammar loads successfully, construction here is expected to succeed deterministically; an unexpected failure propagates up through the producer task's future and is handled the same as any other producer exception (Section 4).

`_extract_commit` calls `_thread_parser` wherever the current sequential loop calls `_get_parser`.

## Section 3: Configuration

Worker count via `MINIGRAF_INGEST_WORKERS` env var, matching the existing `MINIGRAF_GRAPH_PATH` convention (`_get_graph_path`). Unset → resolved to Python's own `ThreadPoolExecutor` default heuristic, computed explicitly (`min(32, (os.cpu_count() or 1) + 4)`) rather than passed as `max_workers=None`, so the same resolved integer is available for `pipeline_depth` in Section 1 without duplicating or guessing the stdlib's internal default. Set to `1` effectively disables parallelism (useful for debugging) without any special-cased code path — the pipeline logic is correct at any worker count ≥ 1.

## Section 4: Producer Error Handling

An unhandled exception in `_extract_commit` propagates when its future is awaited in the consumer loop, hits the same outer `try/except` that already wraps all of `_run_ingestion`, and sets `_ingest_progress["status"] = "error"` — unchanged from today's behavior for unexpected failures. Expected, per-file failures (a single `git show` failing for one file in one commit) are handled inside `_extract_commit` exactly as today: that file is dropped from the commit's file list, the rest of the commit proceeds normally.

## Section 5: Reload `file_deps`/`dep_valid_from` on Startup

**Pre-existing gap, fixed as part of this work:** unlike `entity_valid_from`/`file_entities` (reconstructed from durable DB facts by `_preload_known_entities` on every start), `file_deps`/`dep_valid_from` are populated purely in memory and start empty on every run. After *any* restart (not just an abrupt one), dependency-removal detection silently never fires for deps introduced before that restart, and worse — since `current_deps - previous_deps` treats every currently-present import as newly added when `previous_deps` is wrongly empty, a restart can cause an already-long-standing `:depends-on` edge to be re-transacted with a `:valid-from` of whatever commit happens to touch that file next, corrupting its true introduction history.

### Fix: reload from minigraf's per-fact temporal metadata

Verified against the actual pinned/installed minigraf version (`v1.2.1` — matches `.venv`'s installed `minigraf-1.2.1`, and is what `pyproject.toml`'s `minigraf>=1.2.0` floor resolves to in this project): the `PseudoAttr::ValidFrom`/`ValidTo` mechanism and its fact-correlation "fast path" (`__fvf_`-prefixed hidden binding keys tying the pseudo-attribute to the specific fact matched by the preceding real-attribute pattern, not a cross-join over all of that entity's facts) are present as far back as `v1.0.0`, so this doesn't depend on anything ahead of the pin. `i64::MAX` as the `VALID_TIME_FOREVER` "still open" sentinel for `:valid-to` is documented in-repo (`CLAUDE.md`, `ROADMAP.md`) as a stable public contract at `v1.2.1`.

New function `_preload_known_deps(db, file_entities) -> Tuple[Dict[str, set], Dict[tuple, str]]`, called alongside `_preload_known_entities` before the commit loop starts:

```clojure
(query [:find ?src ?dep ?vf
        :any-valid-time
        :where [?src :depends-on ?dep]
               [?src :db/valid-from ?vf]
               [?src :db/valid-to ?vt]
               [(= ?vt 9223372036854775807)]])
```

`:any-valid-time` is required for any per-fact pseudo-attribute to bind at all (a plain/`:as-of`-less query hard-errors on `:db/valid-from` otherwise); the explicit `?vt` equality against the `VALID_TIME_FOREVER` sentinel is what restricts results to edges that haven't been closed (`:any-valid-time` alone would also return historical, already-closed `:depends-on` facts). `?vf` returns as an integer (ms since epoch) and is converted to the same ISO-8601 string format `dep_valid_from` stores elsewhere (`datetime.fromtimestamp(vf_ms / 1000, tz=utc)...`).

`_code_ident("module", file_path)` is deterministic (not a hash requiring inversion), so build a one-off `{module_ident: file_path}` lookup by computing it forward for every `file_path` already in the reloaded `file_entities`. For each `(src_module_ident, dep_ident, vf_iso)` row from the query above, resolve `src_module_ident` through that lookup to its `file_path`, then populate `file_deps[file_path].add(dep_ident)` and `dep_valid_from[(src_module_ident, dep_ident)] = vf_iso`.

## Section 6: Clean Shutdown / Resume-via-Watermark

The consumer's DB-write phase for a single commit is unchanged and stays atomic-at-the-commit-granularity: watermark is only advanced after that commit's transacts and checkpoint complete. This means the crash-safety floor that exists today is unaffected by parallelizing the producer side. What this section adds is *graceful* shutdown, so an ending session doesn't rely on landing exactly on that floor more often than necessary.

- A module-level `_shutdown_requested = asyncio.Event()`.
- POSIX signal handlers for `SIGTERM`/`SIGINT`, registered via `asyncio.get_running_loop().add_signal_handler` in `main()`, set this event. (Windows has no `add_signal_handler`; this is a non-goal for this design, consistent with the project's existing POSIX-only subprocess/git tooling assumptions.)
- The consumer loop (Section 1's `while pending:`) checks `_shutdown_requested.is_set()` at the **top of each iteration only** — i.e. between commits, never mid-commit's DB-write section — and breaks cleanly if set. `_ingest_progress["status"]` is set to a new `"stopped"` value (distinct from `"error"`) so `minigraf_ingest_status` reports this as an intentional pause, not a crash.
- In practice, the MCP server's most common "session ended" signal is stdin EOF (the parent process closing the pipe) rather than a delivered signal. `main()`'s `async with stdio_server() as (...): await server.run(...)` block is wrapped so that on exit for *any* reason, it sets `_shutdown_requested` and does `await asyncio.wait_for(_ingest_task, timeout=30)` before returning — giving the ingestion loop a chance to reach its next commit boundary and exit cleanly instead of `asyncio.run()` abruptly cancelling it mid-write when the loop closes. If the task doesn't finish within the timeout, fall back to `_ingest_task.cancel()`.
- Resume itself needs no new code: `main()` already unconditionally starts `_run_ingestion` from `_watermark_query(db)` on every startup (unless `MINIGRAF_NO_AUTO_INGEST` is set), and that watermark reflects the last commit whose writes fully completed and checkpointed — whether the previous run ended gracefully (this section) or not.

**Explicit non-goal:** `SIGKILL` and power loss cannot be intercepted by any process. That failure mode is unchanged from today's behavior and is already partially mitigated by the existing stale-lock detection/recovery path (`_clear_stale_lock`/`_stale_lock_holder_pid`) for a DB file lock left behind by a hard kill.

## Testing

- No changes needed to `TestGetParser`/`TestExtractFromSource`/etc. — they call `_get_parser`/`_extract_from_source` directly and never exercise the concurrent path.
- `TestRunIngestion` tests that monkeypatch `mcp_server._git_changed_files` etc. continue to work unmodified: everything still runs in-process against the same module globals, just invoked from worker threads instead of the main thread.
- New tests to add:
  - Concurrent extraction on a multi-commit, multi-file fixture repo produces results identical to a sequential run (same facts, same ordering of DB writes).
  - Two threads racing to build the same never-before-seen language grammar still warn exactly once.
  - A `_git_file_content` failure for one file in a producer task doesn't affect other files in that commit or other commits.
  - `_preload_known_deps` correctly reconstructs `file_deps`/`dep_valid_from` from a DB containing open `:depends-on` facts, and correctly excludes closed ones.
  - Simulated graceful shutdown (setting `_shutdown_requested` mid-run) stops at a commit boundary, and a subsequent `_run_ingestion` call resumes from the correct watermark with no duplicated or skipped commits.

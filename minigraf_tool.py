#!/usr/bin/env python3
"""
Minigraf CLI/HTTP wrapper for AI coding agents.

Provides query and transact functions for persistent bi-temporal graph memory.
Supports both CLI mode (subprocess) and HTTP mode (Axum server).

Usage:
    CLI mode (default):  MINIGRAF_MODE=cli python minigraf_tool.py ...
    HTTP mode:           MINIGRAF_MODE=http python minigraf_tool.py ...
"""

import subprocess
import json
import os
import sys
import time
import urllib.request
import urllib.error
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Union

logger = logging.getLogger("minigraf_tool")
logger.addHandler(logging.NullHandler())

MINIGRAF_BIN = "minigraf"

def _get_timeout() -> int:
    """Get timeout from environment variable, default to 30 seconds."""
    env_timeout = os.environ.get("MINIGRAF_TIMEOUT")
    if env_timeout:
        try:
            return int(env_timeout)
        except ValueError:
            pass
    return 30

MINIGRAF_TIMEOUT = _get_timeout()

def _get_default_graph_path() -> str:
    """Get default graph path with proper platform support."""
    import platform
    
    env_path = os.environ.get("MINIGRAF_GRAPH_PATH")
    if env_path:
        return env_path
    
    system = platform.system()
    
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        graph_dir = Path(base) / "temporal-reasoning"
    elif system == "Darwin":
        graph_dir = Path.home() / "Library" / "Application Support" / "temporal-reasoning"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            graph_dir = Path(xdg_data) / "temporal-reasoning"
        else:
            graph_dir = Path.home() / ".local" / "share" / "temporal-reasoning"
    
    graph_dir.mkdir(parents=True, exist_ok=True)
    return str(graph_dir / "memory.graph")

DEFAULT_GRAPH_PATH = _get_default_graph_path()

MINIGRAF_MODE = os.environ.get("MINIGRAF_MODE", "cli")
MINIGRAF_HTTP_URL = os.environ.get("MINIGRAF_HTTP_URL", "http://localhost:8080")


class MinigrafError(Exception):
    """Error from minigraf operations."""
    pass


def _run_http(endpoint: str, data: Dict) -> Dict[str, Any]:
    """Call HTTP server and return parsed result."""
    try:
        req = urllib.request.Request(
            f"{MINIGRAF_HTTP_URL}/{endpoint}",
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=MINIGRAF_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
            return {"ok": True, "data": result}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"HTTP error: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _run_minigraf(args: List[str], input_data: Optional[str] = None) -> Dict[str, Any]:
    """Run minigraf CLI and return parsed result.
    
    Note: Uses list args (not shell=True) to prevent shell injection.
    Timeout is configurable via MINIGRAF_TIMEOUT env var (default 30s).
    """
    logger.debug(f"Running minigraf with args: {args}")
    try:
        result = subprocess.run(
            [MINIGRAF_BIN] + args,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=MINIGRAF_TIMEOUT
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
            return {"ok": False, "error": error_msg or "Unknown error"}
        
        return {"ok": True, "output": result.stdout.strip()}
    except FileNotFoundError:
        return {"ok": False, "error": f"minigraf not found. Is it installed and on PATH?"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "minigraf command timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def query(datalog: str, as_of: Optional[Union[int, str]] = None, graph_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Query the graph memory with a Datalog query.
    
    Args:
        datalog: A valid Datalog query string
        as_of: Optional transaction count to query as of (temporal query)
        graph_path: Optional path to .graph file. Uses default temp location if not provided.
    
    Returns:
        Dict with 'ok', 'results' (list of results), 'path' (graph path), and optional 'error'
    """
    path = graph_path or DEFAULT_GRAPH_PATH
    
    if MINIGRAF_MODE == "http":
        # HTTP mode
        payload = {"datalog": datalog}
        if as_of is not None:
            payload["as_of"] = as_of
        result = _run_http("query", payload)
        if not result.get("ok"):
            return result
        data = result.get("data", {})
        return {"ok": True, "results": data.get("results", []), "path": path, "mode": "http"}
    
    # CLI mode (original implementation)
    if not os.path.exists(path):
        return {"ok": False, "error": f"No graph file at {path}. Transact first."}
    
    # Handle temporal query - require explicit :as-of in datalog
    if as_of is not None and ":as-of" not in datalog:
        return {
            "ok": False,
            "error": "as_of requires :as-of clause in datalog. Use: [:find ?x :as-of N :where [?e :attr ?x]]"
        }
    
    full_query = f"(query {datalog})"
    result = _run_minigraf(["--file", path], input_data=full_query)
    
    if not result.get("ok"):
        return result
    
    output = result["output"]
    
    if "No results found" in output:
        return {"ok": True, "results": []}
    
    lines = output.split("\n")
    if len(lines) < 3:
        return {"ok": True, "results": []}
    
    # Parse results - Note: verified against minigraf v0.18.0
    # Output format: header line, separator line (---), then data lines
    # Header contains ?variable or :keyword tokens
    if len(lines) < 3:
        return {"ok": True, "results": []}
    
    result_header = lines[0]
    separator = lines[1]
    
    # Count expected columns from header tokens
    col_count = result_header.count("?") + result_header.count(":")
    if col_count == 0:
        return {"ok": False, "error": f"Unexpected output format from minigraf: {output[:200]}"}
    
    results = []
    
    for line in lines[2:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("---"):
            continue
        if "No results" in stripped or stripped.endswith("found."):
            continue
        
        values = [v.strip() for v in line.split("|")]
        if len(values) >= col_count:
            results.append(values[:col_count])
    
    return {"ok": True, "results": results}


def transact(facts: str, reason: Optional[str] = None, graph_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Store facts in the graph memory.
    
    Args:
        facts: Datalog transact string with facts to store
        reason: Why this fact deserves long-term storage (for future validation)
        graph_path: Optional path to .graph file. Uses default temp location if not provided.
    
    Returns:
        Dict with 'ok', 'tx' (transaction count), and optional 'error'
    """
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for all writes"}
    
    path = graph_path or DEFAULT_GRAPH_PATH
    
    if MINIGRAF_MODE == "http":
        # HTTP mode
        payload = {"facts": facts, "reason": reason}
        result = _run_http("transact", payload)
        if not result.get("ok"):
            return result
        data = result.get("data", {})
        return {"ok": True, "tx": data.get("tx", "unknown"), "reason": reason, "path": path, "mode": "http"}
    
    # CLI mode
    full_tx = f"(transact {facts})"
    result = _run_minigraf(["--file", path], input_data=full_tx)
    
    if not result.get("ok"):
        return result
    
    output = result["output"]
    
    if "Transacted successfully" in output:
        tx_match = output.split("tx:")[1].strip().rstrip(")") if "tx:" in output else "unknown"
        return {"ok": True, "tx": tx_match, "reason": reason, "path": path, "mode": "cli"}
    
    return {"ok": True, "output": output, "path": path, "mode": "cli"}


def temporal_query(datalog: str, as_of: Union[int, str], graph_path: Optional[str] = None) -> Dict[str, Any]:
    """
    DEPRECATED: Use query() with explicit :as-of in datalog instead.
    
    Query the graph as of a specific transaction time.
    
    Args:
        datalog: A valid Datalog query string with :as-of clause
        as_of: Ignored (kept for backwards compatibility)
        graph_path: Optional path to .graph file
    
    Returns:
        Dict with query results
    """
    return query(datalog, graph_path=graph_path)


def reset(graph_path: Optional[str] = None) -> Dict[str, Any]:
    """Delete the graph file to start fresh."""
    path = graph_path or DEFAULT_GRAPH_PATH
    if os.path.exists(path):
        os.remove(path)
        return {"ok": True, "deleted": path}
    return {"ok": True, "deleted": None, "note": "No file to delete"}


def export(graph_path: Optional[str] = None) -> Dict[str, Any]:
    """Export all facts from the graph to a JSON file."""
    path = graph_path or DEFAULT_GRAPH_PATH
    
    if not os.path.exists(path):
        return {"ok": False, "error": f"No graph file at {path}"}
    
    result = query("[:find ?e ?a ?v :where [?e ?a ?v]]", graph_path=path)
    if not result.get("ok"):
        return result
    
    export_data = {
        "version": "1.0",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "graph_path": path,
        "facts": result.get("results", [])
    }
    
    return {"ok": True, "data": export_data}


def import_data(data: Dict, graph_path: Optional[str] = None) -> Dict[str, Any]:
    """Import facts from exported JSON data."""
    path = graph_path or DEFAULT_GRAPH_PATH
    
    facts_list = data.get("facts", [])
    if not facts_list:
        return {"ok": False, "error": "No facts to import"}
    
    for fact in facts_list:
        if len(fact) >= 3:
            entity, attr, value = fact[0], fact[1], fact[2]
            facts = f"[[{entity} {attr} {value}]]"
            transact(facts, reason=f"Import from backup", graph_path=path)
    
    return {"ok": True, "imported": len(facts_list)}


def get_graph_path() -> str:
    """Return the default graph path."""
    return DEFAULT_GRAPH_PATH


if __name__ == "__main__":
    import sys
    
    mode = os.environ.get("MINIGRAF_MODE", "cli")
    
    if len(sys.argv) < 2:
        print("Usage: minigraf_tool.py <command> [args]")
        print("Commands: query, transact, reset, path")
        print(f"Mode: {mode} (set MINIGRAF_MODE=http for HTTP server)")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "query":
        if len(sys.argv) < 3:
            print("Usage: minigraf_tool.py query '<datalog>' [--as-of <tx>]")
            sys.exit(1)
        datalog = sys.argv[2]
        as_of = None
        if "--as-of" in sys.argv:
            idx = sys.argv.index("--as-of")
            if idx + 1 < len(sys.argv):
                as_of = sys.argv[idx + 1]
        result = query(datalog, as_of=as_of)
        print(json.dumps(result, indent=2))
    elif cmd == "transact":
        if len(sys.argv) < 3:
            print("Usage: minigraf_tool.py transact '<facts>' [--reason '<reason>']")
            sys.exit(1)
        facts = sys.argv[2]
        reason = None
        if "--reason" in sys.argv:
            idx = sys.argv.index("--reason")
            if idx + 1 < len(sys.argv):
                reason = sys.argv[idx + 1]
        result = transact(facts, reason=reason)
        print(json.dumps(result, indent=2))
    elif cmd == "reset":
        result = reset()
        print(json.dumps(result, indent=2))
    elif cmd == "path":
        print(get_graph_path())
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
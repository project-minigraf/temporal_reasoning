#!/usr/bin/env python3
"""
Temporal Reasoning MCP Server.

Persistent stdio MCP server providing bi-temporal graph memory for AI coding agents.
Sole interface to the minigraf .graph file via the MiniGrafDb Python binding.
"""
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from minigraf import MiniGrafDb, MiniGrafError

# ---------------------------------------------------------------------------
# Session-scoped rules — registered once at startup, cached in RuleRegistry
# ---------------------------------------------------------------------------
SESSION_RULES = [
    "(rule [(linked ?a ?b) [?a :depends-on ?b]])",
    "(rule [(linked ?a ?b) [?a :calls ?b]])",
    "(rule [(reachable ?a ?b) [?a :depends-on ?b]])",
    "(rule [(reachable ?a ?b) [?a :calls ?b]])",
]

# Module-level DB instance — opened once, held for the session lifetime
_db: Optional[MiniGrafDb] = None

# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------


def _get_graph_path() -> str:
    return os.environ.get("MINIGRAF_GRAPH_PATH", str(Path.cwd() / "memory.graph"))


def open_db(graph_path: Optional[str] = None) -> MiniGrafDb:
    """Open MiniGrafDb and register session-scoped rules. Called once at startup."""
    global _db
    path = graph_path or _get_graph_path()
    _db = MiniGrafDb.open(path)
    for rule in SESSION_RULES:
        try:
            _db.execute(rule)
        except MiniGrafError:
            pass
    return _db


def get_db() -> MiniGrafDb:
    """Return the open DB instance; raises RuntimeError if not initialised."""
    if _db is None:
        raise RuntimeError("DB not initialised — call open_db() first")
    return _db


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def _parse_query_result(raw_json: str) -> Dict[str, Any]:
    """Parse JSON returned by MiniGrafDb.execute() for a query command."""
    try:
        data = json.loads(raw_json)
        return {"ok": True, "results": data.get("results", [])}
    except (json.JSONDecodeError, KeyError) as e:
        return {"ok": False, "error": f"Unexpected result format: {e} — raw: {raw_json[:200]}"}


def _parse_tx_result(raw_json: str) -> Dict[str, Any]:
    """Parse JSON returned by MiniGrafDb.execute() for a transact/retract command."""
    try:
        data = json.loads(raw_json)
        return {"ok": True, "tx": str(data.get("tx", "unknown"))}
    except (json.JSONDecodeError, KeyError) as e:
        return {"ok": False, "error": f"Unexpected result format: {e} — raw: {raw_json[:200]}"}


# ---------------------------------------------------------------------------
# Explicit agent tool handlers
# ---------------------------------------------------------------------------

def handle_vulcan_query(datalog: str) -> Dict[str, Any]:
    """Query the graph. Returns {ok, results} or {ok, error}."""
    db = get_db()
    try:
        raw = db.execute(f"(query {datalog})")
        return _parse_query_result(raw)
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


def handle_vulcan_transact(facts: str, reason: str) -> Dict[str, Any]:
    """Transact facts into the graph. reason is required."""
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for all writes"}
    db = get_db()
    try:
        raw = db.execute(f"(transact {facts})")
        db.checkpoint()
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
        return result
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


def handle_vulcan_retract(facts: str, reason: str) -> Dict[str, Any]:
    """Retract facts from the graph. reason is required."""
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for retract"}
    db = get_db()
    try:
        raw = db.execute(f"(retract [{facts}])")
        db.checkpoint()
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
        return result
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


def handle_vulcan_report_issue(
    category: str,
    description: str,
    datalog: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Delegate to report_issue.py."""
    try:
        from report_issue import report_issue
        report_issue(category, description, datalog=datalog, error=error)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# MCP server (tools wired in subsequent tasks)
# ---------------------------------------------------------------------------

server = Server("temporal-reasoning")


async def main() -> None:
    open_db()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())

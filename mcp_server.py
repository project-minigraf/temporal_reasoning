#!/usr/bin/env python3
"""
Temporal Reasoning MCP Server.

Persistent stdio MCP server providing bi-temporal graph memory for AI coding agents.
Sole interface to the minigraf .graph file via the MiniGrafDb Python binding.
"""
import asyncio
import concurrent.futures
import concurrent.futures.process
import configparser
import contextlib
import datetime
import fnmatch
import gc
import hashlib
import json
import multiprocessing
import os
import random
import socket
import re
import signal
import subprocess as _subprocess
import sys
import threading
import time
import traceback
import uuid
import weakref
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from mcp.server import Server
from mcp.server.stdio import stdio_server
from minigraf import MiniGrafDb, MiniGrafError
import fact_index
import frontier_registry
import ingest_progress

# ---------------------------------------------------------------------------
# Session-scoped rules — registered once at startup, cached in RuleRegistry
# ---------------------------------------------------------------------------
SESSION_RULES = [
    "(rule [(linked ?a ?b) [?a :depends-on ?b]])",
    "(rule [(linked ?a ?b) [?a :calls ?b]])",
    "(rule [(reachable ?a ?b) [?a :depends-on ?b]])",
    "(rule [(reachable ?a ?b) [?a :calls ?b]])",
    "(rule [(linked ?a ?b) [?a :contains ?b]])",
    "(rule [(reachable ?a ?b) [?a :contains ?b]])",
    # Commit-graph traversal: (ancestor ?child ?anc) holds when ?anc is a
    # (possibly transitive) git ancestor of ?child via :parent edges.
    # Only evaluated when a query explicitly calls (ancestor ...).
    "(rule [(ancestor ?child ?anc) [?child :parent ?anc]])",
    "(rule [(ancestor ?child ?anc) [?child :parent ?mid] (ancestor ?mid ?anc)])",
]

# User-registered rules — persisted across DB reopens (unlike SESSION_RULES,
# these are accumulated at runtime via minigraf_rule and re-applied on every open).
_user_rules: List[str] = []

# Serializes every native call into the shared MiniGrafDb handle across the
# threads that can touch it concurrently: the event-loop thread (call_tool
# handlers), the ingestion write_executor thread, and worker threads used
# for preload/lock-retry. minigraf's
# own sidecar .lock file only guarantees single-process exclusivity — it says
# nothing about concurrent calls into one already-open handle from multiple
# threads within this same process. Without this, two threads racing a
# call/checkpoint on the same (or a handle open concurrently with another's
# in-flight write) can observe a torn header or a mid-write index, producing
# silently wrong query results or a transient "Header checksum mismatch" (#110).
#
# It serializes calls into ONE handle. It does not, and cannot, stop a second
# handle being opened on the same file — those are distinct Rust objects with
# distinct internal mutexes. See the single-handle invariant below.
_db_native_lock = threading.Lock()

# THE SINGLE-HANDLE INVARIANT (#255, #253, #251, project-minigraf/minigraf#304)
#
# At most one live MiniGrafDb handle per process. _DbLeaseManager enforces it:
# the handle is opened at lease count 0 -> 1, reused by every acquisition in
# between, and dropped at 1 -> 0. There is no window in which the module says
# "no handle" while a caller still holds one, which is what `_db = None` could
# never promise -- it released only when it dropped the LAST reference.
#
# Two handles on one file each cache their own FileHeader.page_count, allocate
# from it, and bounds-check read_page against it, which is where the flaky
# "Page N out of bounds (total pages: M)" came from. Since minigraf 1.2.2 a
# second same-process open raises instead of corrupting, and this project
# requires >= 1.2.3 -- so that path is closed at the source as well as here.
#
# _db_native_lock (above) is a DIFFERENT concern and is deliberately separate:
# it serializes calls INTO one handle; the manager governs the handle's
# LIFETIME. Conflating them is what made the old comment here necessary.
#
# The remaining hole is broader than "a caller keeps its `as db` name past
# the block": it is ANY retained reference to a frame that touched the
# handle, including a retained traceback. Verified empirically -- retaining
# an exception raised INSIDE a native call pins the real handle even though
# _LeasedDb severed its own reference right on schedule, because the
# traceback's `execute` frame holds the raw MiniGrafDb as `self`:
#
#   with db_lease() as db:
#       try: _db_execute(db, "(query [:find ?e :where")   # malformed
#       except Exception as e: held = e
#   # count == 0, but a second open is REFUSED until `held` is dropped
#
# None of this can be prevented in Python, so it is DETECTED, not closed: see
# _DbLeaseManager._detect_leaked_handle. Detection turns it into a named
# diagnostic plus a retry failure at the next acquire, not corruption -- which
# is why _run_ingestion's `e.__traceback__ = None` (Stage B) is still
# load-bearing rather than a leftover from the pre-proxy mechanism.

# Module-level server reference — set after server creation for MCP sampling
_server_ref: Optional[Server] = None

# Retry parameters for acquiring the DB file lock when another process
# (hook subprocess or background ingestion) is briefly holding it.
# Total max wait: 0.05 + 0.10 + 0.20 + 0.40 + 0.80 = 1.55s.
_LOCK_RETRY_MAX = 5
_LOCK_RETRY_BASE = 0.05  # seconds; doubles each attempt

# Extended retry budget for the one-time startup/manual-trigger lock
# acquisition only (_load_ingestion_preload_state) — separate from
# _LOCK_RETRY_MAX/_LOCK_RETRY_BASE above, which gate synchronous
# per-request paths (call_tool) where long blocking would be harmful.
# This path runs on a dedicated worker thread and can
# afford to be patient enough to survive a typical orphan-process cleanup
# window (SIGTERM grace period before SIGKILL) instead of giving up in
# ~1.55s and entering a permanent "error" state (#106).
_INGEST_LOCK_RETRY_BASE = 0.05     # seconds; matches _LOCK_RETRY_BASE for consistency
_INGEST_LOCK_RETRY_CAP = 15.0      # seconds; per-attempt sleep never exceeds this
_INGEST_LOCK_RETRY_BUDGET = 120.0  # seconds; total time before giving up

# Portable graph-ownership hint (#284 item 5).
#
# minigraf 2.0.0 moved locking into the kernel and deleted the `.graph.lock`
# PID sidecar, so nothing on disk names the holder any more. There is no
# mechanism that is at once non-contending, PID-returning and portable across
# Linux/macOS/Windows -- /proc/locks is Linux-only, and a non-blocking flock is
# POSIX-only AND cannot tell our own handle from another process's, which is
# fatal here because this server routinely holds a lease. So we publish our own
# advisory hint instead of reading minigraf's lock.
#
# Correctness still rests entirely on minigraf's kernel lock. This is a
# scheduling courtesy: acting on a wrong hint costs one race or one needless
# decline, never a correctness failure.
#
# Staleness is decided by the hint file's mtime, NEVER by PID liveness. That is
# what makes it portable, and it is why os.kill no longer appears in this file:
# CPython maps non-CTRL signals to TerminateProcess on Windows, so the old
# `os.kill(pid, 0)` "liveness check" would have terminated the process it asked
# about. A fresh hint means something is actively refreshing it right now,
# which is the liveness claim we actually want -- and it disposes of PID reuse
# without special handling, since a reused PID under a stale hint is expired by
# definition.
_OWNER_HINT_HEARTBEAT = 5.0  # seconds between refreshes while held
try:
    # TTL must comfortably exceed the heartbeat so a merely-busy holder is
    # never declared dead. The cost is that after a hard crash the graph looks
    # owned for up to this long; declining is recoverable on the next attempt,
    # whereas racing a long ingestion is what #108 was filed against.
    _OWNER_HINT_TTL = float(os.environ.get("MINIGRAF_OWNER_HINT_TTL", "30.0"))
except ValueError:
    _OWNER_HINT_TTL = 30.0

# #222 phase 5 item C. Stage B releases its lease every _SWEEP_YIELD_COMMITS
# swept commits or _SWEEP_YIELD_SECONDS, whichever comes first, so the
# out-of-process auto-memory hooks can win the graph file lock.
#
# Stage B used to hold ONE lease across its whole sweep. A lease is cheap
# in-process (at count > 0 try_acquire joins and returns the same handle, so a
# concurrent call_tool never blocks) but EXCLUSIVE out-of-process, and BOTH
# auto-memory hooks (hooks/claude-code.json) are `command` hooks in separate
# processes -- finalize_hook.py takes a lease to write each turn's facts. Their
# retry budget is _LOCK_RETRY_MAX x _LOCK_RETRY_BASE doubling = 0.75 s total and
# both swallow failures with `except Exception: pass`. So the whole-sweep hold
# did not block queries; it SILENTLY DISCARDED every auto-memory write for the
# sweep's duration, which on a large repo is a large fraction of the ingest.
#
# Yielding only makes the hook's write SUCCEED if the fact index is committed
# before every release -- which is why each window goes through
# _db_lease_async_committing_index. A window released with the batched
# index_con's SQLite write transaction still open is a lock-order inversion:
# the hook takes the graph lock then blocks on SQLite (5 s busy timeout)
# holding it, ingestion holds SQLite and cannot get the graph back (~2.6 s),
# the run ends `status: error`, and the hook's index insert is swallowed --
# fact in graph, missing from index (#302). Measured, not supposed: see
# CLAUDE.md, "The fact index must be COMMITTED". Every other index-writing
# lease in _run_ingestion goes through the same wrapper since #347.
#
# NOT a per-commit release: _DbLeaseManager.release() at refcount 1 -> 0 drops
# the handle, and minigraf's `Drop for Inner` then runs a full O(graph size)
# checkpoint -- #280, measured at 47.3% of Stage A's write time and growing
# 3.47x within a 220-commit run, outside _CheckpointPolicy's duty gate and
# invisible to the trace's ckpt_d_seconds. A window amortises that over N
# commits.
#
# _SWEEP_YIELD_SECONDS is sized against the hooks' own retry budget (0.75 s
# total): the lock must come free often enough that a hook already retrying can
# win it. It is the SECOND trigger, not the first -- on a large graph one
# window's worth of commits can take far longer than the clock bound, and
# without it the hooks' window would be set by graph size rather than by
# anything anyone chose.
#
# _DbLeaseManager exposes no waiter or contention signal, so "release only when
# something is actually waiting" is not available without building one. The
# trigger has to be a counter or a clock; it is both.
#
# When #280 lands (blocked on upstream minigraf#322), the drop checkpoint is
# suppressed and N can safely go to 1 -- which is why this is a constant to
# lower rather than a structure to rewrite.
#
# Read at import, so the conftest MINIGRAF_* scrub cannot reach it: a test that
# depends on a value must patch the CONSTANT, not the variable.
_SWEEP_YIELD_COMMITS = int(os.environ.get("MINIGRAF_SWEEP_YIELD_COMMITS", "25"))
_SWEEP_YIELD_SECONDS = float(os.environ.get("MINIGRAF_SWEEP_YIELD_SECONDS", "2.0"))

# How long the boundary leaves the graph ACTUALLY unlocked.
#
# Without this the window is worthless in practice, and the reason is worth
# stating exactly. Releasing the lease and re-acquiring it costs no awaits --
# `window_started = ...`, `window_count = 0`, `try_acquire` -- so the graph is
# free for MICROSECONDS. That is a free instant, not a free interval, and a
# hook polling 5 times over its 0.75 s budget will essentially never land in
# it. The boundary creates the right PLACE to yield; this constant is what
# makes the yield real.
#
# Why 0.1 s. Under minigraf 2.0.0 `open()` does not fail fast: it blocks for
# ~375 ms, adaptively polling 5->50 ms, and returns as soon as the lock frees.
# So a hook ALREADY blocked in open() acquires within ~5-50 ms of the lock
# becoming free, and 100 ms clears that comfortably while costing ~5% of a 2 s
# window. A hook that has not started yet gains nothing from any PARTICULAR
# boundary -- it simply blocks and wins at the next one.
#
# Paid only between windows, never after the last one (the sweep sets
# sweep_done first), so a sweep that fits in one window pays nothing at all.
_SWEEP_YIELD_PAUSE_SECONDS = float(
    os.environ.get("MINIGRAF_SWEEP_YIELD_PAUSE_SECONDS", "0.1")
)

# Ingestion state
_ingest_task: Optional[asyncio.Task] = None
_ingest_progress: Dict[str, Any] = {
    "status": "idle", "total": 0, "prior_ingested": 0,
    "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    "phase": None, "orphaned_commits": None,
}
_shutdown_requested = asyncio.Event()

# Startup fact-index backfill task (#147)
_backfill_task: Optional[asyncio.Task] = None

# PID of our immediate supervisor (e.g. `uvx`), recorded at launch. `uvx`
# does not forward its own death to the spawned server — no signal, no stdin
# EOF — so a dead supervisor just reparents us (typically to PID 1 or a
# user-level systemd instance) with nothing to react to. _orphan_watchdog
# polls os.getppid() against this to detect that case. See #104.
_launch_ppid: Optional[int] = None
_ORPHAN_CHECK_INTERVAL = 5.0  # seconds

# ---------------------------------------------------------------------------
# Language detection and grammar caching
# ---------------------------------------------------------------------------

_EXT_TO_LANG: Dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "javascript", ".rs": "rust",
    ".go": "go", ".java": "java", ".c": "c", ".cpp": "cpp",
    ".cs": "c_sharp", ".rb": "ruby", ".php": "php",
    ".kt": "kotlin", ".swift": "swift", ".scala": "scala",
    ".hs": "haskell", ".lua": "lua", ".ex": "elixir", ".exs": "elixir",
    ".h": "c", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".cc": "cpp", ".cxx": "cpp",
}

# Maps lang_name to the actual importable module, for the (currently only)
# case where a single package ships multiple grammar variants. tsx and
# typescript are both exposed by the tree_sitter_typescript package via
# separate language_tsx()/language_typescript() functions — there is no
# separate tree_sitter_tsx module, unlike every other language here.
_LANG_MODULE_OVERRIDES: Dict[str, str] = {
    "tsx": "tree_sitter_typescript",
}

_grammar_cache: Dict[str, Any] = {}  # lang_name → Parser or None

_grammar_cache_lock = threading.Lock()


def _build_parser(lang_name: str) -> Any:
    """Construct a fresh tree_sitter.Parser for lang_name. Raises on failure
    (missing grammar package, incompatible tree-sitter version, etc).

    No caching, no warning side effects — those stay in _get_parser, the
    only caller that needs to turn a failure into a one-time stderr warning.
    Also used by _thread_parser to build a private-to-this-thread instance
    once _get_parser has already proven the grammar loads; an unexpected
    failure there is left to propagate to the caller (Task 3's
    _extract_commit, running in a worker process — see #116) rather than
    being swallowed, consistent with how any other producer-task exception
    is handled.
    """
    module_name = _LANG_MODULE_OVERRIDES.get(lang_name, f"tree_sitter_{lang_name}")
    mod = __import__(module_name, fromlist=["language"])
    from tree_sitter import Language, Parser  # type: ignore
    # PHP exposes language_php() instead of language(); tsx exposes
    # language_tsx() from within the tree_sitter_typescript module.
    lang_fn = getattr(mod, f"language_{lang_name}", None) or mod.language
    lang_obj = Language(lang_fn())
    return Parser(lang_obj)


def _get_parser(file_path: str) -> Optional[Any]:
    """Return a cached tree_sitter.Parser for the file's language, or None if unsupported.

    Uses the individual tree-sitter-<lang> packages (e.g. tree-sitter-python,
    tree-sitter-rust) via the tree-sitter >=0.22 API, compatible across Python
    3.10-3.14+.

    Previously this also tried the bundled `tree_sitter_languages` package as a
    fast path. That package pins no upper bound on its `tree-sitter` dependency
    and hasn't been updated since tree-sitter's 0.22 API redesign, so a fresh
    install silently resolves an incompatible `tree-sitter` and every parse
    fails at runtime (see issue #86). It has been dropped in favor of the
    per-language packages, which are what `install.py` provisions anyway.
    """
    ext = Path(file_path).suffix.lower()
    lang_name = _EXT_TO_LANG.get(ext)
    if not lang_name:
        return None
    if lang_name in _grammar_cache:
        return _grammar_cache[lang_name]

    with _grammar_cache_lock:
        if lang_name in _grammar_cache:  # another thread populated it while we waited
            return _grammar_cache[lang_name]
        try:
            parser = _build_parser(lang_name)
        except Exception as exc:
            parser = None
            print(
                f"[_get_parser] no tree-sitter grammar available for '{lang_name}' "
                f"({exc!r}); code-structure extraction disabled for this language "
                f"until 'tree-sitter-{lang_name}' is installed.",
                file=sys.stderr,
            )
        _grammar_cache[lang_name] = parser
        return parser


_thread_local = threading.local()


def _thread_parser(file_path: str) -> Optional[Any]:
    """Return a Parser instance private to the calling thread for file_path's language.

    tree_sitter.Parser objects are not safe for concurrent .parse() calls
    from multiple threads. Rather than lock around every parse (which would
    serialize the CPU-bound part of concurrent ingestion), each thread gets
    its own Parser per language, built once and cached in thread-local
    storage. Reuses _get_parser purely as the "is this language supported"
    check — including its shared cache and once-only warning — since that
    part is safe to share across threads (a plain dict read after the first
    population, or a briefly-held lock on a miss).
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

# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------

_LANG_NODE_TYPES: Dict[str, Dict[str, set]] = {
    "python": {
        "functions": {"function_definition", "async_function_definition"},
        "classes": {"class_definition"},
        "imports": {"import_statement", "import_from_statement"},
        "calls": {"call"},
    },
    "javascript": {
        "functions": {"function_declaration", "function_expression", "method_definition"},
        "classes": {"class_declaration"},
        "imports": {"import_statement"},
        "calls": {"call_expression"},
    },
    "typescript": {
        "functions": {"function_declaration", "function_expression", "method_definition"},
        "classes": {"class_declaration"},
        "imports": {"import_statement"},
        "calls": {"call_expression"},
    },
    "rust": {
        "functions": {"function_item"},
        "classes": {"struct_item", "impl_item"},
        "imports": {"use_declaration"},
        "calls": {"call_expression"},
    },
    "go": {
        "functions": {"function_declaration", "method_declaration"},
        "classes": {"type_declaration"},
        "imports": {"import_declaration"},
        "calls": {"call_expression"},
    },
    "java": {
        "functions": {"method_declaration"},
        "classes": {"class_declaration"},
        "imports": {"import_declaration"},
        "calls": {"method_invocation"},
    },
    "c": {
        "functions": {"function_definition"},
        "classes": {"struct_specifier"},
        "imports": {"preproc_include"},
        "calls": {"call_expression"},
    },
    "cpp": {
        "functions": {"function_definition"},
        "classes": {"class_specifier", "struct_specifier"},
        "imports": {"preproc_include"},
        "calls": {"call_expression"},
    },
    "c_sharp": {
        "functions": {"method_declaration"},
        "classes": {"class_declaration"},
        "imports": {"using_directive"},
        "calls": {"invocation_expression"},
    },
    "ruby": {
        "functions": {"method"},
        "classes": {"class"},
        "imports": {"call"},
        "calls": set(),
    },
    "php": {
        "functions": {"function_definition", "method_declaration"},
        "classes": {"class_declaration"},
        "imports": {"require_expression", "include_expression",
                    "require_once_expression", "include_once_expression"},
        "calls": {"function_call_expression"},
    },
    "kotlin": {
        "functions": {"function_declaration"},
        "classes": {"class_declaration"},
        "imports": {"import"},
        "calls": {"call_expression"},
    },
    "swift": {
        "functions": {"function_declaration"},
        "classes": {"class_declaration"},
        "imports": {"import_declaration"},
        "calls": {"call_expression"},
    },
    "scala": {
        "functions": {"function_definition"},
        "classes": {"class_definition"},
        "imports": {"import_declaration"},
        "calls": {"call_expression"},
    },
    "haskell": {
        "functions": {"function"},
        "classes": {"data_type"},
        "imports": {"import"},
        "calls": {"apply"},
    },
    "lua": {
        # Named function statements (`function foo() end`, `local function
        # bar() end`, `function t.baz() end`, `function t:qux() end`) parse
        # as "function_declaration" in the real tree-sitter-lua grammar, with
        # a "name" field -- verified empirically (#171). "function_definition"
        # is the anonymous `function() end` expression form and never carries
        # a "name" field, so it can never surface a function name here.
        "functions": {"function_declaration"},
        "classes": set(),
        "imports": {"function_call"},
        "calls": set(),
    },
    "elixir": {
        # Vestigial/unused for functions & classes: defmodule/def/defp all
        # parse as generic "call" nodes in the real grammar, not as their own
        # node types, so _walk_ast/_collect_entity_nodes special-case
        # lang_name == "elixir" entirely and never consult these two sets
        # (see #170). "imports" is still consulted only indirectly, via the
        # same elixir-specific branch's fallback to _extract_import_name.
        "functions": {"def", "defp", "defmacro", "defmacrop", "defguard", "defguardp", "defdelegate"},
        "classes": {"defmodule"},
        "imports": {"call"},
        "calls": set(),
    },
}


def _rust_use_root(node) -> Optional[str]:
    """Return the root crate/module name from a Rust use_declaration node.

    Rust use paths have these shapes in the tree-sitter AST:
      use_declaration
        scoped_identifier          → std::collections::HashMap
        scoped_use_list            → crate::storage::{mod1, mod2}
        identifier                 → use foo;
        use_as_clause              → use foo as bar;

    We always want the leftmost identifier in the path, which is the crate name
    (e.g. "std", "tokio") or "crate"/"super"/"self" for intra-project paths.
    For crate-relative paths we return the first path segment after "crate" so
    the edge points to the local module, not the generic keyword "crate".
    """
    def leftmost_ident(n) -> Optional[str]:
        """Recursively find the leftmost identifier/keyword in a path node."""
        if n.type == "identifier":
            return n.text.decode("utf-8")
        if n.type in ("crate", "super", "self"):
            # intra-project: find first real identifier among siblings/children
            return None  # caller will try the next path segment
        # scoped_identifier / scoped_use_list: path is in named children
        for child in n.named_children:
            result = leftmost_ident(child)
            if result is not None:
                return result
        return None

    def root_from_path(n) -> Optional[str]:
        """Extract root module name from a path-like node."""
        if n.type == "identifier":
            return n.text.decode("utf-8")
        if n.type in ("crate", "super", "self"):
            return None  # skip; caller handles intra-project
        if n.type in ("scoped_identifier", "scoped_use_list"):
            children = n.named_children
            if not children:
                return None
            first = children[0]
            if first.type in ("crate", "super", "self"):
                # intra-project: return the next segment
                if len(children) > 1:
                    seg = children[1]
                    if seg.type == "identifier":
                        return seg.text.decode("utf-8")
                return None
            return root_from_path(first)
        if n.type == "use_as_clause":
            path_node = n.child_by_field_name("path")
            return root_from_path(path_node) if path_node else None
        return None

    for child in node.named_children:
        result = root_from_path(child)
        if result:
            return result
    return None


def _c_include_name(node) -> Optional[str]:
    """Return the include target (path preserved, extension stripped) from a
    C/C++ preproc_include node.

    Handles both:
      #include <stdio.h>          → system_lib_string → "stdio"
      #include <unicode/uloc.h>   → system_lib_string → "unicode/uloc"
      #include "sub/myheader.h"   → string_literal    → "sub/myheader"

    Path structure is preserved (not reduced to a bare basename) so
    _resolve_module_import can match vendored in-tree headers precisely —
    both angle-bracket and quoted forms commonly carry a real subdirectory
    (<sys/socket.h>, <unicode/uloc.h>, "app/config.h"), not just stdlib-style
    bare names like <vector>.
    """
    for child in node.children:
        if child.type in ("system_lib_string", "string_literal"):
            raw = child.text.decode("utf-8").strip("<>\"'")
            return os.path.splitext(raw)[0]
    return None


def _csharp_using_name(node) -> Optional[str]:
    """Return the full dotted namespace from a C# using_directive node.

    using System;                     → "System"
    using System.Collections.Generic; → "System.Collections.Generic"

    The dotted path is one named child (qualified_name for multi-segment
    paths, identifier for single-segment ones) whose own .text is already
    the full joined name.
    """
    for child in node.named_children:
        if child.type in ("qualified_name", "identifier"):
            return child.text.decode("utf-8")
    return None


def _ruby_require_name(node) -> Optional[str]:
    """Return the required path from a Ruby call node (path preserved,
    extension stripped). A require_relative target is prefixed with "./" so
    it reuses the same relative-import detection _resolve_module_import
    already needs for JS/TS-style "./foo" specifiers, rather than plumbing a
    separate is-relative flag through the whole imports pipeline.

    Handles:
      require 'rails'                            → "rails"
      require 'active_support/core_ext/string'    → "active_support/core_ext/string"
      require_relative 'my_mod'                   → "./my_mod"
    Returns None for non-require calls.
    """
    method = node.child_by_field_name("method")
    if method is None or method.text.decode("utf-8") not in ("require", "require_relative"):
        return None
    is_relative = method.text.decode("utf-8") == "require_relative"
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.named_children:
        if child.type == "string":
            content_node = next(
                (c for c in child.named_children if c.type == "string_content"),
                None,
            )
            if content_node:
                val = content_node.text.decode("utf-8")
            else:
                val = child.text.decode("utf-8").strip("'\"")
            path = os.path.splitext(val)[0]
            return f"./{path}" if is_relative else path
    return None


def _lua_require_name(node) -> Optional[str]:
    """Return the module name from a Lua function_call to require().

    require("socket")  → "socket"
    Returns None for non-require calls.

    AST shape:
      function_call
        identifier  b'require'
        arguments
          (  b'('
          string  b'"socket"'
          )  b')'
    """
    fn_node = None
    for child in node.children:
        if child.type == "identifier":
            fn_node = child
            break
    if fn_node is None or fn_node.text.decode("utf-8") != "require":
        return None
    for child in node.children:
        if child.type == "arguments":
            for arg in child.children:
                if arg.type == "string":
                    return arg.text.decode("utf-8").strip("'\"")
    return None


def _elixir_module_name(node) -> Optional[str]:
    """Return the full dotted module name from an Elixir alias/import/use/require call.

    alias MyApp.Router     → "MyApp.Router"
    import Ecto.Query      → "Ecto.Query"
    use Phoenix.Controller → "Phoenix.Controller"
    require Logger         → "Logger"
    Returns None for non-module calls (e.g. IO.puts/1 where target is a dot node).
    """
    _ELIXIR_MODULE_CALLS = {"alias", "import", "use", "require"}
    # The call target is the field named "target" — an identifier for alias/import/use/require,
    # or a dot node for things like IO.puts/1.
    target = node.child_by_field_name("target")
    if target is None or target.type != "identifier":
        return None
    if target.text.decode("utf-8") not in _ELIXIR_MODULE_CALLS:
        return None
    # The module argument is in an "arguments" child (unnamed field).
    # It contains an "alias" node whose text is the full dotted module name.
    for child in node.children:
        if child.type == "arguments":
            for arg in child.children:
                if arg.type == "alias":
                    return arg.text.decode("utf-8")
    return None


def _elixir_call_target_text(node) -> Optional[str]:
    """Return an Elixir `call` node's target identifier text (e.g. "def",
    "defmodule", "foo"), or None if the target isn't a plain identifier
    (e.g. a dotted call like IO.puts)."""
    target = node.child_by_field_name("target")
    if target is not None and target.type == "identifier":
        return target.text.decode("utf-8")
    return None


def _elixir_defmodule_name(node) -> Optional[str]:
    """Return the dotted module name from a `defmodule` call node's
    `arguments` (the `alias` node's text, e.g. "Foo.Bar")."""
    arguments = next((c for c in node.children if c.type == "arguments"), None)
    if arguments is None:
        return None
    alias_node = next((c for c in arguments.children if c.type == "alias"), None)
    return alias_node.text.decode("utf-8") if alias_node is not None else None


def _elixir_def_function_name(node) -> Optional[str]:
    """Return the function name from a def/defmacro/defguard/defdelegate-family
    call node's `arguments` (`def`, `defp`, `defmacro`, `defmacrop`, `defguard`,
    `defguardp`, `defdelegate` -- all parse to this identical `call` shape, #205).

    `def bar do` -> arguments' first named child is a bare `identifier` (no
    parens, zero-arg). `def bar(x, y) do` -> arguments' first named child is a
    `call` node (the parenthesized parameter list itself parses as a nested
    call expression) whose own target identifier is the function name. A
    guard clause (`def bar(x) when x > 0 do`, or any `defguard`/`defguardp`,
    which always carries one) wraps that call one level deeper in a
    `binary_operator` chain (`field:operator` text "when") -- descend its
    `field:left` to reach the same call node. `defdelegate qux(x), to: Other`
    has a second named child (the `to:` keyword pair) that's ignored since
    only the first named child is inspected.
    """
    arguments = next((c for c in node.children if c.type == "arguments"), None)
    if arguments is None:
        return None
    target = next(iter(arguments.named_children), None)
    while target is not None:
        if target.type == "identifier":
            return target.text.decode("utf-8")
        if target.type == "call":
            return _elixir_call_target_text(target)
        if target.type == "binary_operator":
            target = target.child_by_field_name("left")
            continue
        return None
    return None


def _extract_import_name(node, lang_name: str) -> List[str]:
    """Extract top-level module names from an import node (may return multiple)."""
    names: List[str] = []
    if lang_name == "python":
        if node.type == "import_from_statement":
            m = node.child_by_field_name("module_name")
            if m:
                # m.text is the raw specifier as written, including relative
                # forms: "pathlib", ".sub", "..pkg" — see _resolve_module_import
                # for how leading dots get resolved against the importing file.
                names.append(m.text.decode("utf-8"))
        else:
            # import_statement: collect all full dotted module names
            for child in node.named_children:
                if child.type == "aliased_import":
                    n = child.child_by_field_name("name")
                    if n:
                        names.append(n.text.decode("utf-8"))
                elif child.type == "dotted_name":
                    names.append(child.text.decode("utf-8"))
    elif lang_name in ("javascript", "typescript", "tsx"):
        src = node.child_by_field_name("source")
        if src:
            names.append(src.text.decode("utf-8").strip("'\""))
    elif lang_name == "rust":
        name = _rust_use_root(node)
        if name:
            names.append(name)
    elif lang_name == "go":
        def _go_spec(spec_node):
            path = spec_node.child_by_field_name("path")
            if path:
                names.append(path.text.decode("utf-8").strip('"'))

        for child in node.named_children:
            if child.type == "import_spec":
                _go_spec(child)
            elif child.type == "import_spec_list":
                for spec in child.named_children:
                    if spec.type == "import_spec":
                        _go_spec(spec)
    elif lang_name == "java":
        # import_declaration's dotted path is one named child, already the
        # full text (e.g. "java.util.List") — scoped_identifier for
        # multi-segment paths, plain identifier for single-segment ones.
        for child in node.named_children:
            if child.type in ("scoped_identifier", "identifier"):
                names.append(child.text.decode("utf-8"))
                break
    elif lang_name in ("c", "cpp"):
        name = _c_include_name(node)
        if name:
            names.append(name)
    elif lang_name == "c_sharp":
        name = _csharp_using_name(node)
        if name:
            names.append(name)
    elif lang_name == "ruby":
        name = _ruby_require_name(node)
        if name:
            names.append(name)
    elif lang_name == "php":
        for child in node.children:
            if child.type in ("string", "encapsed_string", "string_literal"):
                val = child.text.decode("utf-8").strip("'\"")
                names.append(os.path.splitext(val)[0])
                break
    elif lang_name == "kotlin":
        # import node's dotted path is one named child (qualified_identifier
        # for multi-segment, identifier for single-segment) whose .text is
        # already the full joined name.
        for child in node.named_children:
            if child.type in ("qualified_identifier", "identifier"):
                names.append(child.text.decode("utf-8"))
                break
    elif lang_name == "swift":
        # import_declaration's single "identifier" named child already
        # holds the full dotted text (e.g. "Foundation.NSString") directly —
        # no recursion needed.
        for child in node.named_children:
            if child.type in ("identifier", "simple_identifier"):
                names.append(child.text.decode("utf-8"))
                break
    elif lang_name == "scala":
        # import_declaration's path is flattened into individual "identifier"
        # named children (no wrapping scoped node), so join the leading run
        # of identifiers rather than taking the first one's text alone.
        segments = []
        for child in node.named_children:
            if child.type != "identifier":
                break
            segments.append(child.text.decode("utf-8"))
        if segments:
            names.append(".".join(segments))
    elif lang_name == "haskell":
        for child in node.named_children:
            if child.type in ("module", "qualified_module", "constructor"):
                names.append(child.text.decode("utf-8"))
                break
    elif lang_name == "lua":
        name = _lua_require_name(node)
        if name:
            names.append(name)
    elif lang_name == "elixir":
        name = _elixir_module_name(node)
        if name:
            names.append(name)
    return names


def _extract_call_name(node, lang_name: str) -> Optional[str]:
    """Extract the function name from a call node (best-effort, identifiers only).

    The callee's field name and node type are not universal across grammars:
      - JS/TS/Rust/C/C++/Go/C#/Scala: field "function", type "identifier" (default case below).
      - Java (method_invocation): callee field is named "name", not "function".
      - Kotlin/Swift (call_expression): no field name at all — callee is the
        first named child (an identifier/simple_identifier).
      - PHP (function_call_expression): field is "function" but the node type
        there is "name", not "identifier".
      - Haskell (apply): field is "function" but the node type is "variable",
        and calls with 2+ arguments curry into nested apply nodes
        (`f x y` -> apply(function=apply(function=variable f, argument=x), argument=y)),
        so the callee sits at the bottom of the leftward "function"-field chain.
    """
    if lang_name == "java":
        fn = node.child_by_field_name("name")
        if fn and fn.type == "identifier":
            return fn.text.decode("utf-8")
        return None
    if lang_name in ("kotlin", "swift"):
        children = node.named_children
        if children and children[0].type in ("identifier", "simple_identifier"):
            return children[0].text.decode("utf-8")
        return None
    if lang_name == "php":
        fn = node.child_by_field_name("function")
        if fn and fn.type == "name":
            return fn.text.decode("utf-8")
        return None
    if lang_name == "haskell":
        # A curried call `f x y` nests as apply(function=apply(function=variable
        # f, argument=x), argument=y) -- _walk_ast visits every "apply" node in
        # that chain, so without this check each inner link would independently
        # walk back down to the same innermost callee, reporting one call as
        # many. Only the outermost apply (the one NOT itself sitting in a
        # parent apply's "function" field) should emit.
        parent = node.parent
        if parent is not None and parent.type == "apply" and parent.child_by_field_name("function") == node:
            return None
        current = node
        while current.type == "apply":
            fn = current.child_by_field_name("function")
            if fn is None:
                return None
            if fn.type == "variable":
                return fn.text.decode("utf-8")
            current = fn
        return None
    fn = node.child_by_field_name("function")
    if fn and fn.type == "identifier":
        return fn.text.decode("utf-8")
    return None


def _c_family_function_name(node) -> Optional[str]:
    """Resolve a function/method name from a C/C++ declarator chain.

    Unlike most tree-sitter grammars, C-family function_definition nodes have
    no direct `name` field — the identifier is nested under one or more
    `declarator` fields (pointer_declarator, function_declarator, ...). An
    out-of-line qualified definition (`Foo::bar`) wraps the identifier in a
    qualified_identifier, which exposes it via a `name` field instead.
    """
    current = node.child_by_field_name("declarator")
    while current is not None:
        if current.type in ("identifier", "field_identifier", "destructor_name", "operator_name"):
            return current.text.decode("utf-8")
        if current.type == "qualified_identifier":
            current = current.child_by_field_name("name")
            continue
        current = current.child_by_field_name("declarator")
    return None


def _go_struct_type_specs(node) -> List[Tuple[str, Any]]:
    """Resolve (name, type_spec) pairs for struct types from a Go
    `type_declaration` node.

    Unlike most tree-sitter grammars, `type_declaration` has no `name` field
    of its own -- it belongs to the nested `type_spec` child(ren), one level
    down (#172). A grouped `type (\n A struct{...}\n B struct{...}\n)` block
    keeps its `type_spec` children direct (no `type_spec_list` wrapper,
    unlike grouped `var (...)`) -- verified empirically -- so a single
    `type_declaration` node can carry more than one struct. Scoped to struct
    types only, matching `_extract_go_globals_and_fields`'s existing scope
    (a plain alias like `type MyInt int` has no fields to attribute, so it's
    not treated as a class-equivalent entity here).
    """
    results: List[Tuple[str, Any]] = []
    for type_spec in node.children:
        if type_spec.type != "type_spec":
            continue
        struct_type = type_spec.child_by_field_name("type")
        if struct_type is None or struct_type.type != "struct_type":
            continue
        name_node = type_spec.child_by_field_name("name")
        if name_node:
            results.append((name_node.text.decode("utf-8"), type_spec))
    return results


def _walk_ast(node, results: Dict[str, List[str]], lang_name: str) -> None:
    """Recursively extract code entities from a tree-sitter AST node.

    tsx is treated as an alias of typescript here (and in _extract_import_name)
    rather than duplicating every _LANG_NODE_TYPES entry — the TSX grammar is
    a strict superset of TypeScript's node types for the constructs this
    module cares about (functions, classes, imports, calls).

    Elixir bypasses the generic node_types-driven dispatch below entirely:
    `defmodule`/`def`/`defp`/`defmacro`/`defmacrop`/`defguard`/`defguardp`/
    `defdelegate`/`alias`/`import`/`use`/`require` (and every ordinary
    function call) all parse as the *same* generic `call` node type in the
    real tree-sitter-elixir grammar — there is no dedicated `defmodule`/`def`/
    `defp` node type to match against, the way `_LANG_NODE_TYPES["elixir"]`
    used to assume (#170, extended to the macro/guard/delegate forms in #205).
    Disambiguation requires inspecting the call's target identifier text
    instead.
    """
    if lang_name == "elixir":
        if node.type == "call":
            target_text = _elixir_call_target_text(node)
            if target_text == "defmodule":
                name = _elixir_defmodule_name(node)
                if name:
                    results["classes"].append(name)
            elif target_text in (
                "def", "defp", "defmacro", "defmacrop",
                "defguard", "defguardp", "defdelegate",
            ):
                name = _elixir_def_function_name(node)
                if name:
                    results["functions"].append(name)
            else:
                names = _extract_import_name(node, lang_name)
                results["imports"].extend(names)
        for child in node.children:
            _walk_ast(child, results, lang_name)
        return

    node_types = _LANG_NODE_TYPES.get("typescript" if lang_name == "tsx" else lang_name)
    if node_types is None:
        return

    if node.type in node_types.get("functions", set()):
        if lang_name in ("c", "cpp"):
            name = _c_family_function_name(node)
            if name:
                results["functions"].append(name)
        else:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                results["functions"].append(name)

    elif node.type in node_types.get("classes", set()):
        if lang_name == "go":
            for name, _type_spec in _go_struct_type_specs(node):
                results["classes"].append(name)
        else:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                results["classes"].append(name)

    elif node.type in node_types.get("imports", set()):
        names = _extract_import_name(node, lang_name)
        results["imports"].extend(names)

    elif node.type in node_types.get("calls", set()):
        name = _extract_call_name(node, lang_name)
        if name:
            results["calls"].append(name)

    for child in node.children:
        _walk_ast(child, results, lang_name)


def _extract_globals_and_fields(root_node: Any, lang_name: str) -> Dict[str, Any]:
    """Scope-aware extraction of module-level globals and class fields.

    Deliberately NOT a _walk_ast-style full-tree recursion: an assignment-
    like node is ubiquitous (appears inside every function body too), so a
    naive table-driven walk would misclassify every local variable as a
    global. Each per-language function in _GLOBAL_FIELD_EXTRACTORS is
    responsible for only descending into module-level and class-body-level
    statements, never into a function/method body (barring a narrow,
    per-language, deliberate exception — see each language's own extractor).
    """
    empty: Dict[str, Any] = {
        "globals": [], "global_bodies": {}, "fields": [], "field_info": {},
        "global_nodes": {}, "field_nodes": {},
    }
    extractor = _GLOBAL_FIELD_EXTRACTORS.get(lang_name)
    if extractor is None or root_node is None:
        return empty
    return extractor(root_node)


_GLOBAL_FIELD_EXTRACTORS: Dict[str, Callable[[Any], Dict[str, Any]]] = {}


def _extract_python_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware Python globals/fields extraction.

    Only descends into: module-root direct children (globals), a
    class_definition's body direct children (class-level/static fields),
    and __init__'s own body direct children (self.x = ... instance
    fields). Never recurses into any other function/method body, so a
    known limitation is that fields first assigned outside __init__ (e.g.
    dynamically added attributes) are not captured — deliberate, bounded
    heuristic, not an oversight.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def plain_assignment_name(stmt_node: Any) -> Optional[Tuple[str, Any]]:
        if stmt_node.type != "expression_statement" or stmt_node.child_count == 0:
            return None
        assign = stmt_node.children[0]
        if assign.type != "assignment":
            return None
        left = assign.child_by_field_name("left")
        if left is not None and left.type == "identifier":
            return left.text.decode("utf-8"), assign
        return None

    def self_attr_assignment_name(stmt_node: Any) -> Optional[Tuple[str, Any]]:
        if stmt_node.type != "expression_statement" or stmt_node.child_count == 0:
            return None
        assign = stmt_node.children[0]
        if assign.type != "assignment":
            return None
        left = assign.child_by_field_name("left")
        if left is None or left.type != "attribute":
            return None
        obj = left.child_by_field_name("object")
        attr = left.child_by_field_name("attribute")
        if obj is not None and obj.type == "identifier" and obj.text == b"self" and attr is not None:
            return attr.text.decode("utf-8"), assign
        return None

    for stmt in root_node.children:
        if stmt.type == "class_definition":
            class_name_node = stmt.child_by_field_name("name")
            class_name = class_name_node.text.decode("utf-8") if class_name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                match = plain_assignment_name(member)
                if match:
                    field_name, assign_node = match
                    fields.append((field_name, class_name, True))
                    field_info[field_name] = {
                        "class": class_name, "static": True,
                        "body": assign_node.text.decode("utf-8", "replace"),
                    }
                    field_nodes[f"{class_name}.{field_name}"] = assign_node
                elif member.type == "function_definition":
                    fn_name_node = member.child_by_field_name("name")
                    if fn_name_node is not None and fn_name_node.text == b"__init__":
                        fn_body = member.child_by_field_name("body")
                        if fn_body is not None:
                            for fn_stmt in fn_body.children:
                                self_match = self_attr_assignment_name(fn_stmt)
                                if self_match:
                                    field_name, assign_node = self_match
                                    fields.append((field_name, class_name, False))
                                    field_info[field_name] = {
                                        "class": class_name, "static": False,
                                        "body": assign_node.text.decode("utf-8", "replace"),
                                    }
                                    field_nodes[f"{class_name}.{field_name}"] = assign_node
        else:
            match = plain_assignment_name(stmt)
            if match:
                name, assign_node = match
                globals_.append(name)
                global_bodies[name] = assign_node.text.decode("utf-8", "replace")
                global_nodes[name] = assign_node

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["python"] = _extract_python_globals_and_fields


def _extract_js_family_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware JavaScript/TypeScript globals/fields extraction.

    Only descends into: program-root direct children (globals, from
    lexical_declaration/variable_declaration's variable_declarator
    children) and a class_declaration's class_body direct children
    (field_definition for JS, public_field_definition for TS). A
    program-root `export_statement` (covering `export const`/`export
    let`/`export class`/`export default class`) is unwrapped via its
    `declaration` field before the same type checks apply — this is the
    one extra step permitted; it does not add any further recursion.
    Never recurses into a function/method body, so a class field assigned
    only inside a constructor (e.g. `this.x = 1` with no class-body field
    declaration) is not captured — deliberate, bounded heuristic
    consistent with the Python extractor's __init__-only scope.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    for raw_stmt in root_node.children:
        stmt = raw_stmt
        if stmt.type == "export_statement":
            # `export const X = 5;`, `export class Foo {...}`, and
            # `export default class Bar {...}` all wrap the actual
            # declaration inside an export_statement node, exposed
            # uniformly via the `declaration` field (verified against the
            # real installed tree-sitter-javascript/typescript grammars,
            # including the `export default class` variant). Unwrap it
            # once here; no further recursion is added beyond this.
            declaration = stmt.child_by_field_name("declaration")
            if declaration is None:
                continue
            stmt = declaration
        if stmt.type in ("lexical_declaration", "variable_declaration"):
            for child in stmt.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    if name_node is not None and name_node.type == "identifier":
                        name = name_node.text.decode("utf-8")
                        globals_.append(name)
                        # Use raw_stmt (not the unwrapped stmt) so an
                        # exported global's body includes the `export`
                        # keyword, matching its actual source text.
                        global_bodies[name] = raw_stmt.text.decode("utf-8", "replace")
                        global_nodes[name] = raw_stmt
        elif stmt.type == "class_declaration":
            class_name_node = stmt.child_by_field_name("name")
            class_name = class_name_node.text.decode("utf-8") if class_name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type not in ("field_definition", "public_field_definition"):
                    continue
                name_node = member.child_by_field_name("property") or member.child_by_field_name("name")
                if name_node is None or name_node.type not in ("property_identifier",):
                    continue
                field_name = name_node.text.decode("utf-8")
                is_static = any(c.type == "static" for c in member.children)
                fields.append((field_name, class_name, is_static))
                field_info[field_name] = {
                    "class": class_name, "static": is_static,
                    "body": member.text.decode("utf-8", "replace"),
                }
                field_nodes[f"{class_name}.{field_name}"] = member

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["javascript"] = _extract_js_family_globals_and_fields
_GLOBAL_FIELD_EXTRACTORS["typescript"] = _extract_js_family_globals_and_fields


def _extract_rust_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware Rust globals/fields extraction.

    Only descends into: module-root direct children (`static_item`/
    `const_item` globals, `struct_item` field lists) and, as the one
    deliberate exception to "fields are always instance-only" in this
    language, an `impl_item` block's direct `const_item` children --
    treated as static fields of the impl'd type (the closest Rust analog
    to a class-static constant). Never recurses into a function/method
    body.

    A leading `pub` visibility_modifier is a CHILD of static_item/
    const_item/struct_item/field_declaration in the real installed
    tree-sitter-rust grammar, not a wrapping node (unlike JS's `export`
    wrapping the declaration in an export_statement) -- so no unwrapping
    step is needed here; `child_by_field_name("name")` resolves correctly
    regardless of `pub`. Verified empirically before writing this code.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    for stmt in root_node.children:
        if stmt.type in ("static_item", "const_item"):
            name_node = stmt.child_by_field_name("name")
            if name_node is not None:
                name = name_node.text.decode("utf-8")
                globals_.append(name)
                global_bodies[name] = stmt.text.decode("utf-8", "replace")
                global_nodes[name] = stmt
        elif stmt.type == "struct_item":
            struct_name_node = stmt.child_by_field_name("name")
            struct_name = struct_name_node.text.decode("utf-8") if struct_name_node else ""
            body = stmt.child_by_field_name("body")
            if body is not None:
                for member in body.children:
                    if member.type == "field_declaration":
                        fname_node = member.child_by_field_name("name")
                        if fname_node is not None:
                            fname = fname_node.text.decode("utf-8")
                            fields.append((fname, struct_name, False))
                            field_info[fname] = {
                                "class": struct_name, "static": False,
                                "body": member.text.decode("utf-8", "replace"),
                            }
                            field_nodes[f"{struct_name}.{fname}"] = member
        elif stmt.type == "impl_item":
            type_node = stmt.child_by_field_name("type")
            # For a generic impl (`impl<T> Foo<T> { ... }`), the `type` field
            # is a `generic_type` node whose own `type` sub-field holds the
            # bare `type_identifier` ("Foo"). Unwrap it so the owning-class
            # name matches the clean name registered for the struct itself
            # (struct_item's `name` field never includes generic params).
            # Verified empirically against the installed tree-sitter-rust
            # grammar for both `impl<T> Foo<T> { const CAP: ... }` (type
            # field = generic_type -> type = type_identifier "Foo") and
            # `impl Foo { const ASSOC: ... }` (type field = type_identifier
            # "Foo" directly, unchanged by this unwrap).
            if type_node is not None and type_node.type == "generic_type":
                inner_type_node = type_node.child_by_field_name("type")
                if inner_type_node is not None:
                    type_node = inner_type_node
            type_name = type_node.text.decode("utf-8") if type_node is not None else ""
            body = stmt.child_by_field_name("body")
            if body is not None:
                for member in body.children:
                    if member.type == "const_item":
                        cname_node = member.child_by_field_name("name")
                        if cname_node is not None:
                            cname = cname_node.text.decode("utf-8")
                            fields.append((cname, type_name, True))
                            field_info[cname] = {
                                "class": type_name, "static": True,
                                "body": member.text.decode("utf-8", "replace"),
                            }
                            field_nodes[f"{type_name}.{cname}"] = member

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["rust"] = _extract_rust_globals_and_fields


def _extract_go_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware Go globals/fields extraction.

    Only descends into: file-root direct children (`var_declaration`/
    `const_declaration` globals, via their `var_spec`/`const_spec`
    children) and a `type_declaration > type_spec`'s `struct_type` body
    direct children (fields). Never recurses into a function/method body.

    Go has no export keyword -- exported identifiers are just capitalized
    -- so there is no wrapping-node analog to JS's `export_statement` to
    unwrap here; verified empirically that field:name resolves the same
    way regardless of capitalization.

    NOTE: `struct_type`'s `field_declaration_list` child is NOT exposed
    via a `body` field in the real installed tree-sitter-go grammar (it's
    a plain positional child, unlike Rust's struct_item/C's
    struct_specifier which both do expose `field:body`) -- verified
    empirically. It must be located by node type instead of
    child_by_field_name("body").

    NOTE: a grouped/parenthesized `var (\n A = 1\n B = 2\n)` -- an
    idiomatic and common real-world Go pattern -- wraps its `var_spec`
    children in an intermediate `var_spec_list` node, unlike a grouped
    `const (...)` or `type (...)`, which do NOT wrap their specs (their
    `const_spec`/`type_spec` children stay direct children of the
    declaration node even when grouped) -- verified empirically against
    the real installed tree-sitter-go grammar. `iter_specs` below
    unwraps a `{spec_type}_list` if present so grouped var declarations
    aren't silently dropped; it's a no-op for const/type, which never
    produce that wrapper.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def iter_specs(stmt: Any, spec_type: str) -> Any:
        list_type = f"{spec_type}_list"
        for child in stmt.children:
            if child.type == spec_type:
                yield child
            elif child.type == list_type:
                for inner in child.children:
                    if inner.type == spec_type:
                        yield inner

    for stmt in root_node.children:
        if stmt.type in ("var_declaration", "const_declaration"):
            spec_type = "var_spec" if stmt.type == "var_declaration" else "const_spec"
            for spec in iter_specs(stmt, spec_type):
                name_node = spec.child_by_field_name("name")
                if name_node is not None:
                    name = name_node.text.decode("utf-8")
                    globals_.append(name)
                    global_bodies[name] = stmt.text.decode("utf-8", "replace")
                    global_nodes[name] = stmt
        elif stmt.type == "type_declaration":
            for type_spec in stmt.children:
                if type_spec.type != "type_spec":
                    continue
                type_name_node = type_spec.child_by_field_name("name")
                type_name = type_name_node.text.decode("utf-8") if type_name_node else ""
                struct_type = type_spec.child_by_field_name("type")
                if struct_type is None or struct_type.type != "struct_type":
                    continue
                body = next(
                    (c for c in struct_type.children if c.type == "field_declaration_list"),
                    None,
                )
                if body is None:
                    continue
                for member in body.children:
                    if member.type == "field_declaration":
                        # `X, Y int` inside a struct puts more than one
                        # node under the `name` field of one
                        # field_declaration -- child_by_field_name
                        # (singular) only returns the first, silently
                        # dropping `Y`. Verified empirically; use the
                        # plural children_by_field_name to capture all.
                        for fname_node in member.children_by_field_name("name"):
                            fname = fname_node.text.decode("utf-8")
                            fields.append((fname, type_name, False))
                            field_info[fname] = {
                                "class": type_name, "static": False,
                                "body": member.text.decode("utf-8", "replace"),
                            }
                            field_nodes[f"{type_name}.{fname}"] = member

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["go"] = _extract_go_globals_and_fields


def _extract_c_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware C globals/fields extraction.

    Only descends into: translation-unit-root direct `declaration`
    children (globals, whether a bare `identifier` declarator or an
    `init_declarator` wrapping one) and a `struct_specifier`'s
    `field_declaration_list` body direct children (fields, via each
    `field_declaration`'s `field:declarator` = `field_identifier`).
    Never recurses into a function body.

    C has no export/visibility keyword; `static`/`extern` show up as a
    `storage_class_specifier` sibling of the declarator inside
    `declaration`, not a wrapper around it -- verified empirically that
    field:declarator resolves the same way with or without them present.

    NOTE: a multi-declarator statement (`int a, b = 2;` at file scope, or
    `int a, b;` inside a struct) -- an ordinary, common C pattern -- puts
    more than one node under the `declarator` field, so
    `child_by_field_name("declarator")` (singular) only returns the
    first one and silently drops the rest. Verified empirically against
    the real installed tree-sitter-c grammar; `children_by_field_name`
    (plural) is used instead to capture all of them.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def declarator_name(node: Any) -> Optional[str]:
        if node.type == "identifier":
            return node.text.decode("utf-8")
        if node.type == "init_declarator":
            inner = node.child_by_field_name("declarator")
            return declarator_name(inner) if inner is not None else None
        return None

    for stmt in root_node.children:
        if stmt.type == "declaration":
            for declarator in stmt.children_by_field_name("declarator"):
                name = declarator_name(declarator)
                if name:
                    globals_.append(name)
                    global_bodies[name] = stmt.text.decode("utf-8", "replace")
                    global_nodes[name] = stmt
        elif stmt.type == "struct_specifier":
            struct_name_node = stmt.child_by_field_name("name")
            struct_name = struct_name_node.text.decode("utf-8") if struct_name_node else ""
            body = stmt.child_by_field_name("body")
            if body is not None:
                for member in body.children:
                    if member.type == "field_declaration":
                        for declarator in member.children_by_field_name("declarator"):
                            if declarator.type == "field_identifier":
                                fname = declarator.text.decode("utf-8")
                                fields.append((fname, struct_name, False))
                                field_info[fname] = {
                                    "class": struct_name, "static": False,
                                    "body": member.text.decode("utf-8", "replace"),
                                }
                                field_nodes[f"{struct_name}.{fname}"] = member

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["c"] = _extract_c_globals_and_fields


def _extract_java_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware Java globals/fields extraction.

    Java has no true top-level globals -- all state lives inside a class
    -- so this always returns "globals": []. Only descends into a
    class_declaration's class_body direct children (field_declaration).
    Never recurses into a method/constructor body.

    `walk()` recurses into every node (not just direct children of the
    root), unlike this plan's C/Go/Rust extractors' strictly-direct-
    children approach -- this is still safe scope-wise because
    class_declaration is itself a structural node (same non-ambiguity
    argument as functions/classes in `_walk_ast`), and nested/inner
    classes are a real, valid Java construct worth capturing fields from.
    The recursion only ever *enters* a matched class's own member list
    (walk_class only looks at class_node's body's direct children), never
    a method body, so the "don't misclassify locals" invariant holds even
    though walk() itself descends everywhere.

    A field_declaration is optionally preceded by a `modifiers` wrapper
    node (one node containing `static`/`public`/etc. as separate
    children, e.g. "public static final") -- verified empirically against
    the real installed tree-sitter-java grammar; static iff any child of
    that `modifiers` node has type "static".

    NOTE: `int a, b;` puts more than one node under the `declarator`
    field of a single field_declaration -- child_by_field_name (singular)
    only returns the first, silently dropping `b`. Verified empirically
    (same lesson as Go's multi-name struct field and C's multi-declarator
    statement); children_by_field_name (plural) is used instead.
    """
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def walk_class(class_node: Any) -> None:
        name_node = class_node.child_by_field_name("name")
        class_name = name_node.text.decode("utf-8") if name_node else ""
        body = class_node.child_by_field_name("body")
        if body is None:
            return
        for member in body.children:
            if member.type != "field_declaration":
                continue
            is_static = False
            for child in member.children:
                if child.type == "modifiers":
                    is_static = any(mod.type == "static" for mod in child.children)
            for declarator in member.children_by_field_name("declarator"):
                if declarator.type != "variable_declarator":
                    continue
                fname_node = declarator.child_by_field_name("name")
                if fname_node is not None:
                    fname = fname_node.text.decode("utf-8")
                    fields.append((fname, class_name, is_static))
                    field_info[fname] = {
                        "class": class_name, "static": is_static,
                        "body": member.text.decode("utf-8", "replace"),
                    }
                    field_nodes[f"{class_name}.{fname}"] = member

    def walk(node: Any) -> None:
        if node.type == "class_declaration":
            walk_class(node)
        for child in node.children:
            walk(child)

    walk(root_node)
    return {
        "globals": [], "global_bodies": {}, "fields": fields, "field_info": field_info,
        "global_nodes": {}, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["java"] = _extract_java_globals_and_fields


def _extract_csharp_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware C# globals/fields extraction.

    C# has no true top-level globals -- all state lives inside a class --
    so this always returns "globals": []. Only descends into a
    class_declaration's declaration_list body direct children
    (field_declaration). Never recurses into a method/constructor body.

    `walk()` recurses into every node, same rationale and same "never
    enters a method body" invariant as the Java extractor above -- nested
    classes are a real, valid C# construct.

    Unlike Java, a field_declaration's modifiers are direct sibling
    children of type "modifier" (not wrapped in an intermediate node) --
    verified empirically against the real installed tree-sitter-c-sharp
    grammar; static iff any child has type "modifier" and text b"static".

    A field_declaration wraps a single variable_declaration child, itself
    containing one or more variable_declarator children (via field:name
    on the declarator, not on the variable_declaration step -- the
    variable_declaration node has no field-name of its own per the
    verified dump, so it's located by type). `int a, b;` puts multiple
    variable_declarator nodes as plain positional children of that one
    variable_declaration -- iterating them directly (no field lookup)
    already captures all of them, verified empirically.
    """
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def walk_class(class_node: Any) -> None:
        name_node = class_node.child_by_field_name("name")
        class_name = name_node.text.decode("utf-8") if name_node else ""
        body = class_node.child_by_field_name("body")
        if body is None:
            return
        for member in body.children:
            if member.type != "field_declaration":
                continue
            is_static = any(c.type == "modifier" and c.text == b"static" for c in member.children)
            var_decl = next((c for c in member.children if c.type == "variable_declaration"), None)
            if var_decl is None:
                continue
            for declarator in var_decl.children:
                if declarator.type == "variable_declarator":
                    fname_node = declarator.child_by_field_name("name")
                    if fname_node is not None:
                        fname = fname_node.text.decode("utf-8")
                        fields.append((fname, class_name, is_static))
                        field_info[fname] = {
                            "class": class_name, "static": is_static,
                            "body": member.text.decode("utf-8", "replace"),
                        }
                        field_nodes[f"{class_name}.{fname}"] = member

    def walk(node: Any) -> None:
        if node.type == "class_declaration":
            walk_class(node)
        for child in node.children:
            walk(child)

    walk(root_node)
    return {
        "globals": [], "global_bodies": {}, "fields": fields, "field_info": field_info,
        "global_nodes": {}, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["c_sharp"] = _extract_csharp_globals_and_fields


def _extract_cpp_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware C++ globals/fields extraction.

    Top-level `int x = 5;` is a `declaration` directly under
    `translation_unit`, same shape as C -- reuses C's declarator_name
    helper (identifier, or init_declarator wrapping one).

    `class Foo { ... };` / `struct Foo { ... };` are `class_specifier` /
    `struct_specifier` nodes with `field:body` = `field_declaration_list`;
    verified empirically against the real installed tree-sitter-cpp
    grammar that both node types expose `name`/`body` fields identically,
    so class and struct fields are handled by the same code path (structs
    default to public, classes to private, but that doesn't affect the
    AST shape used here). `access_specifier` nodes (public:/private:/
    protected:) are just plain sibling children of field_declaration_list
    -- multiple such sections in one class don't disrupt iteration since
    non-field_declaration members are simply skipped.

    Each member `field_declaration` optionally has a
    `storage_class_specifier` child with text b"static"; a plain
    (non-method) field's declarator is directly a `field_identifier`, not
    wrapped in `init_declarator` (unlike C's free variable declarations,
    verified empirically). A method declaration's declarator is a
    `function_declarator` wrapping a `field_identifier` -- filtering on
    `declarator.type == "field_identifier"` naturally excludes methods.

    NOTE: `int a, b;` (at file scope, or as a field inside a class/struct)
    puts more than one node under the `declarator` field of a single
    declaration/field_declaration -- child_by_field_name (singular) only
    returns the first one and silently drops the rest. Verified
    empirically against the real installed tree-sitter-cpp grammar (same
    lesson as Task 15's C extractor and Task 16's Java extractor);
    children_by_field_name (plural) is used instead to capture all of
    them, for both globals and fields.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def declarator_name(node: Any) -> Optional[str]:
        if node.type == "identifier":
            return node.text.decode("utf-8")
        if node.type == "init_declarator":
            inner = node.child_by_field_name("declarator")
            return declarator_name(inner) if inner is not None else None
        return None

    for stmt in root_node.children:
        if stmt.type == "declaration":
            for declarator in stmt.children_by_field_name("declarator"):
                name = declarator_name(declarator)
                if name:
                    globals_.append(name)
                    global_bodies[name] = stmt.text.decode("utf-8", "replace")
                    global_nodes[name] = stmt
        elif stmt.type in ("class_specifier", "struct_specifier"):
            name_node = stmt.child_by_field_name("name")
            class_name = name_node.text.decode("utf-8") if name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type != "field_declaration":
                    continue
                is_static = any(
                    c.type == "storage_class_specifier" and c.text == b"static"
                    for c in member.children
                )
                for declarator in member.children_by_field_name("declarator"):
                    if declarator.type != "field_identifier":
                        continue
                    fname = declarator.text.decode("utf-8")
                    fields.append((fname, class_name, is_static))
                    field_info[fname] = {
                        "class": class_name, "static": is_static,
                        "body": member.text.decode("utf-8", "replace"),
                    }
                    field_nodes[f"{class_name}.{fname}"] = member

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["cpp"] = _extract_cpp_globals_and_fields


def _extract_ruby_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware Ruby globals/fields extraction.

    Ruby's grammar distinguishes `$global`/`CONST`/`@@class_var`/
    `@instance_var` via distinct node types (global_variable, constant,
    class_variable, instance_variable), so -- unlike every other
    language in this plan -- no heuristic modifier-inspection is needed;
    classification is purely by field:left's node type.

    Only descends into: program-root direct children (globals, from a
    top-level `assignment` whose field:left is global_variable/constant),
    a `class` node's field:body (body_statement) direct children
    (class_variable assignment -> static field), and a `method` named
    "initialize" that is itself a direct child of that same body_statement
    (its own field:body's direct-child assignments with field:left of
    type instance_variable -> instance field). Never recurses into any
    other method body.

    A `module Foo ... end` node has type "module", not "class" -- a
    distinct node type in the real installed tree-sitter-ruby grammar
    even though it exposes the same name/body fields -- so it is simply
    not matched by the `stmt.type == "class"` check below; module-level
    constants/class variables are out of scope for this extractor by
    design. Verified empirically.

    `attr_accessor`/`attr_reader`/`attr_writer` are ordinary method
    calls (node type `call`), not assignments -- verified empirically --
    so they are naturally excluded without any special-casing.

    NOTE: Ruby's multi-assignment (`$a, $b = 1, 2` or, inside a class,
    `@@a, @@b = 1, 2` / `@x, @y = 1, 2`) wraps the left side in a
    `left_assignment_list` node instead of exposing a bare
    global_variable/constant/class_variable/instance_variable directly
    under field:left -- verified empirically against the real installed
    tree-sitter-ruby grammar. Same lesson as the multi-declarator gaps
    found in every other language in this plan (Go/C/Java/C++): iterate
    `left_assignment_list`'s children too, or every name but the first
    is silently dropped.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def left_targets(left_node: Any, target_types: Tuple[str, ...]) -> List[Any]:
        if left_node.type in target_types:
            return [left_node]
        if left_node.type == "left_assignment_list":
            return [c for c in left_node.children if c.type in target_types]
        return []

    for stmt in root_node.children:
        if stmt.type == "assignment":
            left = stmt.child_by_field_name("left")
            if left is None:
                continue
            for target in left_targets(left, ("global_variable", "constant")):
                name = target.text.decode("utf-8")
                globals_.append(name)
                global_bodies[name] = stmt.text.decode("utf-8", "replace")
                global_nodes[name] = stmt
        elif stmt.type == "class":
            name_node = stmt.child_by_field_name("name")
            class_name = name_node.text.decode("utf-8") if name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type == "assignment":
                    left = member.child_by_field_name("left")
                    if left is None:
                        continue
                    for target in left_targets(left, ("class_variable",)):
                        fname = target.text.decode("utf-8")
                        fields.append((fname, class_name, True))
                        field_info[fname] = {
                            "class": class_name, "static": True,
                            "body": member.text.decode("utf-8", "replace"),
                        }
                        field_nodes[f"{class_name}.{fname}"] = member
                elif member.type == "method":
                    method_name_node = member.child_by_field_name("name")
                    if method_name_node is not None and method_name_node.text == b"initialize":
                        method_body = member.child_by_field_name("body")
                        if method_body is not None:
                            for inner in method_body.children:
                                if inner.type == "assignment":
                                    left = inner.child_by_field_name("left")
                                    if left is None:
                                        continue
                                    for target in left_targets(left, ("instance_variable",)):
                                        fname = target.text.decode("utf-8")
                                        fields.append((fname, class_name, False))
                                        field_info[fname] = {
                                            "class": class_name, "static": False,
                                            "body": inner.text.decode("utf-8", "replace"),
                                        }
                                        field_nodes[f"{class_name}.{fname}"] = inner

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["ruby"] = _extract_ruby_globals_and_fields


def _extract_php_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware PHP globals/fields extraction.

    Top-level `$x = 5;` is `expression_statement > assignment_expression`
    with field:left = variable_name -> global. `class Foo { ... }` is
    class_declaration with field:body = declaration_list; each member is
    a property_declaration containing an optional static_modifier child
    and one or more property_element children (field:name =
    variable_name) -> field.

    Multi-property declarations (`public static $a = 1, $b = 2;`)
    already work with plain iteration: verified empirically that a
    single property_declaration node holds multiple property_element
    children directly, unlike the multi-declarator gaps found in every
    other C-family language in this plan (Go/Java/C++) -- no unwrapping
    needed here.

    Typed properties (`public int $x = 5;`, PHP 7.4+) add a
    primitive_type/named_type child to property_declaration but keep the
    same property_element shape -- verified empirically -- so no special
    handling is required.

    Namespaces have two forms, verified empirically against the real
    installed tree-sitter-php grammar:
      - Semicolon style (`namespace App; $x = 5;`) does NOT wrap
        subsequent statements; they remain direct children of `program`,
        so the plain top-level loop already sees them.
      - Block style (`namespace App { $x = 5; }`) wraps its statements in
        a compound_statement exposed via namespace_definition's
        field:body. Without recursing into it, every global/class inside
        a block-style namespace would be silently dropped -- the same
        shape-changing-wrapper lesson as JS's export_statement. PHP
        namespaces are extremely common in real-world code, so
        namespace_definition nodes are unwrapped recursively (namespaces
        can themselves be nested).

    PHP 8+ constructor property promotion
    (`public function __construct(public int $x) {}`) produces a
    property_promotion_parameter node inside the constructor's
    formal_parameters -- NOT a property_declaration under the class
    body -- verified empirically, so it requires separate handling.
    Promoted properties cannot carry a `static` modifier in real PHP
    (verified: adding one produces a parse ERROR node), so they are
    always recorded as instance (non-static) fields.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def handle_class(stmt: Any) -> None:
        name_node = stmt.child_by_field_name("name")
        class_name = name_node.text.decode("utf-8") if name_node else ""
        body = stmt.child_by_field_name("body")
        if body is None:
            return
        for member in body.children:
            if member.type == "property_declaration":
                is_static = any(c.type == "static_modifier" for c in member.children)
                for elem in member.children:
                    if elem.type == "property_element":
                        elem_name_node = elem.child_by_field_name("name")
                        if elem_name_node is not None:
                            fname = elem_name_node.text.decode("utf-8")
                            fields.append((fname, class_name, is_static))
                            field_info[fname] = {
                                "class": class_name, "static": is_static,
                                "body": member.text.decode("utf-8", "replace"),
                            }
                            field_nodes[f"{class_name}.{fname}"] = member
            elif member.type == "method_declaration":
                method_name_node = member.child_by_field_name("name")
                if method_name_node is not None and method_name_node.text == b"__construct":
                    params = member.child_by_field_name("parameters")
                    if params is not None:
                        for param in params.children:
                            if param.type == "property_promotion_parameter":
                                param_name_node = param.child_by_field_name("name")
                                if param_name_node is not None:
                                    fname = param_name_node.text.decode("utf-8")
                                    fields.append((fname, class_name, False))
                                    field_info[fname] = {
                                        "class": class_name, "static": False,
                                        "body": param.text.decode("utf-8", "replace"),
                                    }
                                    field_nodes[f"{class_name}.{fname}"] = param

    def handle_stmts(stmts: Sequence[Any]) -> None:
        for stmt in stmts:
            if stmt.type == "expression_statement" and stmt.child_count > 0:
                expr = stmt.children[0]
                if expr.type == "assignment_expression":
                    left = expr.child_by_field_name("left")
                    if left is not None and left.type == "variable_name":
                        name = left.text.decode("utf-8")
                        globals_.append(name)
                        global_bodies[name] = stmt.text.decode("utf-8", "replace")
                        global_nodes[name] = stmt
            elif stmt.type == "class_declaration":
                handle_class(stmt)
            elif stmt.type == "namespace_definition":
                ns_body = stmt.child_by_field_name("body")
                if ns_body is not None:
                    handle_stmts(ns_body.children)

    handle_stmts(root_node.children)

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["php"] = _extract_php_globals_and_fields


def _extract_kotlin_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware Kotlin globals/fields extraction.

    Top-level `val`/`var` is a property_declaration directly under
    source_file, wrapping a variable_declaration (single name) or a
    multi_variable_declaration (destructuring, e.g. `val (a, b) =
    ...`) -- NEITHER exposes a named field for the identifier(s) in the
    real installed tree-sitter-kotlin grammar, verified empirically, so
    both are located purely by node type. `class Foo { ... }` is a
    class_declaration whose `class_body` child is likewise not exposed
    via a named field (only `name` is a named field on
    class_declaration) -- verified empirically. A property_declaration
    that is a direct child of class_body is an instance field; one
    nested inside a companion_object's own class_body is Kotlin's
    static-equivalent.

    Multi-declarator/destructuring (`val (a, b) = Pair(1, 2)`) wraps
    its names in multi_variable_declaration instead of a bare
    variable_declaration -- verified empirically, same lesson as the
    multi-declarator gaps found in every prior language in this plan
    (Go/C/Java/C++/Ruby): every identifier inside it is extracted, not
    just treated as absent.

    Kotlin's primary-constructor property shorthand (`class Foo(val x:
    Int)`) is an idiomatic and extremely common way to declare instance
    fields (near-universal in `data class`), but it is structurally a
    class_parameter inside primary_constructor's class_parameters list
    -- NOT a property_declaration under class_body -- verified
    empirically. class_parameter exposes no named fields either; its
    optional `val`/`var` keyword child and its `identifier` name child
    are both located by node type, taking only the direct-child
    identifier so the nested one inside the parameter's type
    annotation (e.g. `Int` in `val x: Int`) is never matched. A
    class_parameter with neither a `val` nor `var` child is a plain
    (non-property) constructor parameter and is excluded. These are
    always instance fields: Kotlin has no syntax for a `val`/`var`
    primary-constructor parameter inside a companion object (companion
    objects are declared with `object`, which has no primary
    constructor), so no static variant of this exists.

    Never recurses into a nested class_declaration found inside a
    class_body (fields two classes deep are out of scope, consistent
    with every other language in this plan) or into any function/method
    body.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def property_names(prop_node: Any) -> List[str]:
        names: List[str] = []
        for child in prop_node.children:
            if child.type == "variable_declaration":
                for inner in child.children:
                    if inner.type == "identifier":
                        names.append(inner.text.decode("utf-8"))
                        break
            elif child.type == "multi_variable_declaration":
                for decl in child.children:
                    if decl.type == "variable_declaration":
                        for inner in decl.children:
                            if inner.type == "identifier":
                                names.append(inner.text.decode("utf-8"))
                                break
        return names

    def record_constructor_param_fields(class_node: Any, class_name: str) -> None:
        primary_ctor = next((c for c in class_node.children if c.type == "primary_constructor"), None)
        if primary_ctor is None:
            return
        params = next((c for c in primary_ctor.children if c.type == "class_parameters"), None)
        if params is None:
            return
        for param in params.children:
            if param.type != "class_parameter":
                continue
            if not any(c.type in ("val", "var") for c in param.children):
                continue
            name_node = next((c for c in param.children if c.type == "identifier"), None)
            if name_node is None:
                continue
            fname = name_node.text.decode("utf-8")
            fields.append((fname, class_name, False))
            field_info[fname] = {
                "class": class_name, "static": False,
                "body": param.text.decode("utf-8", "replace"),
            }
            field_nodes[f"{class_name}.{fname}"] = param

    for stmt in root_node.children:
        if stmt.type == "property_declaration":
            for name in property_names(stmt):
                globals_.append(name)
                global_bodies[name] = stmt.text.decode("utf-8", "replace")
                global_nodes[name] = stmt
        elif stmt.type == "class_declaration":
            name_node = stmt.child_by_field_name("name")
            class_name = name_node.text.decode("utf-8") if name_node else ""
            record_constructor_param_fields(stmt, class_name)
            class_body = next((c for c in stmt.children if c.type == "class_body"), None)
            if class_body is None:
                continue
            for member in class_body.children:
                if member.type == "property_declaration":
                    for fname in property_names(member):
                        fields.append((fname, class_name, False))
                        field_info[fname] = {
                            "class": class_name, "static": False,
                            "body": member.text.decode("utf-8", "replace"),
                        }
                        field_nodes[f"{class_name}.{fname}"] = member
                elif member.type == "companion_object":
                    companion_body = next((c for c in member.children if c.type == "class_body"), None)
                    if companion_body is None:
                        continue
                    for inner_member in companion_body.children:
                        if inner_member.type == "property_declaration":
                            for fname in property_names(inner_member):
                                fields.append((fname, class_name, True))
                                field_info[fname] = {
                                    "class": class_name, "static": True,
                                    "body": inner_member.text.decode("utf-8", "replace"),
                                }
                                field_nodes[f"{class_name}.{fname}"] = inner_member

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["kotlin"] = _extract_kotlin_globals_and_fields


def _extract_swift_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware Swift globals/fields extraction.

    Top-level `let`/`var` is a property_declaration directly under
    source_file. `class Foo { ... }`, `struct Foo { ... }`, `enum Foo
    { ... }`, and `extension Foo { ... }` are ALL parsed as the same
    class_declaration node type (distinguished only by a
    `declaration_kind` field whose text is "class"/"struct"/"enum"/
    "extension") -- verified empirically against the real installed
    tree-sitter-swift grammar. Members are fetched via
    `child_by_field_name("body")`, which works uniformly even though
    the body node's *type* differs (class_body for class/struct/
    extension, enum_class_body for enum), because it's identified by
    field name, not type.

    A property_declaration can bind MULTIPLE names in one statement
    (`let a = 1, b = 2`), each its own `field:name` -> pattern ->
    field:bound_identifier chain -- verified empirically. This is the
    Swift analog of the multi-declarator gap found in every prior
    language in this plan: `children_by_field_name("name")` is used
    (not `child_by_field_name`, which would silently return only the
    first binding) so every name in the statement is extracted.

    Tuple destructuring (`let (x, y) = (1, 2)`) wraps names in a
    pattern whose nested per-name patterns expose no bound_identifier
    field -- verified empirically -- so it is safely skipped (no name
    extracted), consistent with other out-of-scope destructuring forms
    in this plan (e.g. Kotlin's multi_variable_declaration, which IS
    in scope there because it exposes a different structural shape;
    Swift's tuple pattern here does not surface identifiers via any
    field, only by node type, so it is left alone).

    Swift has no primary-constructor property shorthand analogous to
    Kotlin's `class Foo(val x: Int)`: properties are always declared
    inside the body via property_declaration regardless of how `init`
    initializes them -- verified empirically (a class with only an
    `init` and no property_declaration produces zero fields, and `init`
    parameters/assignments are never treated as field declarations).

    `extension Foo { ... }` is in scope as a side effect of sharing the
    class_declaration node type with `class`/`struct`/`enum`: its
    `field:name` is a user_type node wrapping Foo's type_identifier,
    and `.text` on that node still resolves to the plain name "Foo" --
    verified empirically -- so properties declared in an extension are
    picked up and correctly attributed to class "Foo" rather than
    silently dropped or misattributed. This is not explicitly
    requested by the task brief but is a safe, useful side effect
    rather than a misbehavior: it does not crash and does not merge
    unrelated types.

    Static iff the member's `modifiers` child has a `property_modifier`
    child with text "static" (Swift's `class var` for overridable
    static-like properties uses modifier text "class", not "static",
    and is therefore treated as instance per the task brief's
    definition).

    Never recurses into a nested class_declaration found inside a
    class/enum body (fields two types deep are out of scope, consistent
    with every other language in this plan) or into any function/method
    body.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def property_names(prop_node: Any) -> List[str]:
        names: List[str] = []
        for pattern in prop_node.children_by_field_name("name"):
            bound = pattern.child_by_field_name("bound_identifier")
            if bound is not None:
                names.append(bound.text.decode("utf-8"))
        return names

    for stmt in root_node.children:
        if stmt.type == "property_declaration":
            for name in property_names(stmt):
                globals_.append(name)
                global_bodies[name] = stmt.text.decode("utf-8", "replace")
                global_nodes[name] = stmt
        elif stmt.type == "class_declaration":
            name_node = stmt.child_by_field_name("name")
            class_name = name_node.text.decode("utf-8") if name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type != "property_declaration":
                    continue
                is_static = False
                for child in member.children:
                    if child.type == "modifiers":
                        is_static = any(
                            m.type == "property_modifier" and m.text == b"static"
                            for m in child.children
                        )
                for fname in property_names(member):
                    fields.append((fname, class_name, is_static))
                    field_info[fname] = {
                        "class": class_name, "static": is_static,
                        "body": member.text.decode("utf-8", "replace"),
                    }
                    field_nodes[f"{class_name}.{fname}"] = member

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["swift"] = _extract_swift_globals_and_fields


def _extract_scala_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware Scala globals/fields extraction.

    Deliberate simplification (do not "fix" -- this is an intentional,
    reasoned non-goal, not an oversight): Scala's closest analog to
    "static" is a companion `object` sharing a class's name, but
    matching an object_definition to its companion class_definition by
    name -- and only then treating its members as that class's static
    fields -- is real extra complexity for a niche pattern. Instead, a
    top-level object_definition (direct child of compilation_unit, or
    of a package block -- see below) is treated as a globals namespace,
    not a fields-owner: its members are extracted as plain
    module-level globals, no `:class` edge and no `:static` concept
    invoked at all. Only genuine class_definition members become
    instance fields, always `:static=False` (Scala classes have no
    static-member concept). A nested object_definition found inside a
    class's template_body is out of scope (it simply doesn't match the
    val_definition/var_definition type filter used there, so it is
    skipped without special-casing).

    `class Foo { val instanceField = 2 }` is class_definition with
    field:body = template_body, containing val_definition/
    var_definition members whose field:pattern normally holds a plain
    identifier -- verified empirically against the real installed
    tree-sitter-scala grammar.

    Multi-binding in one val/var statement is real in Scala and comes
    in TWO distinct structural forms, both verified empirically (same
    multi-declarator lesson as nearly every other language in this
    plan):
      - Tuple destructuring (`val (a, b) = (1, 2)`) puts a
        tuple_pattern in field:pattern, whose children are `(`, `,`,
        `)`, and nested identifier/tuple_pattern nodes (tuple patterns
        can nest, e.g. `val (a, (b, c)) = ...`).
      - Comma-separated multi-name binding (`val a, b = 5`, binding
        both names to the same value) puts an "identifiers" node --
        NOT a tuple_pattern -- in field:pattern, whose children are
        `,`-separated identifier nodes.
    Both shapes are handled by a single recursive pattern_names()
    helper so no destructured/multi-bound name is silently dropped.

    Scala's primary-constructor property shorthand (`class Foo(val x:
    Int, var y: String)`) is idiomatic and extremely common (case
    classes in particular), but it is structurally a class_parameter
    inside class_definition's own field:class_parameters list -- NOT a
    val_definition/var_definition under template_body -- verified
    empirically, the same kind of justified, narrowly-scoped extension
    Kotlin's task added for its analogous primary-constructor shorthand.
    Unlike Kotlin's grammar, tree-sitter-scala DOES expose a `name`
    field directly on class_parameter, so no by-type child search is
    needed for the identifier; only the optional `val`/`var` keyword
    child is found by type (it carries no field name). A
    class_parameter with neither a `val` nor `var` child is a plain
    (non-property) constructor parameter and is excluded -- this
    deliberately also excludes case class parameters that lack an
    explicit `val`/`var` keyword, even though Scala implicitly treats
    unmarked case class parameters as public vals; recognizing that
    implicit rule would require keying off the class_definition's
    `case` child, a separate semantic inference beyond the structural,
    by-keyword scope of this extension, so it is left out.

    Scala's `package foo { ... }` block form wraps its contents in
    package_clause's field:body (a template_body) -- verified
    empirically -- the same shape-changing-wrapper hazard as JS's
    export_statement and PHP's block-style namespace_definition
    elsewhere in this plan. Without unwrapping it, every global/class
    inside a braced package block would be silently dropped. The bare
    `package foo` form (no braces) does NOT wrap subsequent statements
    -- they remain direct siblings under compilation_unit, verified
    empirically -- so no special handling is needed for that form.
    package_clause blocks can nest, so they are unwrapped recursively.

    Never recurses into a class_definition's or object_definition's
    template_body looking for further nested class/object definitions,
    nor into any function/method body.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def pattern_names(pattern: Any) -> List[str]:
        if pattern is None:
            return []
        if pattern.type == "identifier":
            return [pattern.text.decode("utf-8")]
        if pattern.type in ("tuple_pattern", "identifiers"):
            names: List[str] = []
            for child in pattern.children:
                names.extend(pattern_names(child))
            return names
        return []

    def record_constructor_param_fields(class_node: Any, class_name: str) -> None:
        params = class_node.child_by_field_name("class_parameters")
        if params is None:
            return
        for param in params.children:
            if param.type != "class_parameter":
                continue
            if not any(c.type in ("val", "var") for c in param.children):
                continue
            name_node = param.child_by_field_name("name")
            if name_node is None:
                continue
            fname = name_node.text.decode("utf-8")
            fields.append((fname, class_name, False))
            field_info[fname] = {
                "class": class_name, "static": False,
                "body": param.text.decode("utf-8", "replace"),
            }
            field_nodes[f"{class_name}.{fname}"] = param

    def handle_stmts(stmts: Sequence[Any]) -> None:
        for stmt in stmts:
            if stmt.type == "object_definition":
                body = stmt.child_by_field_name("body")
                if body is None:
                    continue
                for member in body.children:
                    if member.type in ("val_definition", "var_definition"):
                        for name in pattern_names(member.child_by_field_name("pattern")):
                            globals_.append(name)
                            global_bodies[name] = member.text.decode("utf-8", "replace")
                            global_nodes[name] = member
            elif stmt.type == "class_definition":
                name_node = stmt.child_by_field_name("name")
                class_name = name_node.text.decode("utf-8") if name_node else ""
                record_constructor_param_fields(stmt, class_name)
                body = stmt.child_by_field_name("body")
                if body is None:
                    continue
                for member in body.children:
                    if member.type in ("val_definition", "var_definition"):
                        for name in pattern_names(member.child_by_field_name("pattern")):
                            fields.append((name, class_name, False))
                            field_info[name] = {
                                "class": class_name, "static": False,
                                "body": member.text.decode("utf-8", "replace"),
                            }
                            field_nodes[f"{class_name}.{name}"] = member
            elif stmt.type == "package_clause":
                pkg_body = stmt.child_by_field_name("body")
                if pkg_body is not None:
                    handle_stmts(pkg_body.children)

    handle_stmts(root_node.children)

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["scala"] = _extract_scala_globals_and_fields


def _extract_haskell_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Scope-aware Haskell top-level bindings/record-fields extraction.

    Haskell is a genuinely different paradigm from every other language in
    this plan -- no OOP, no static/instance concept, so extracted fields
    are always `:static=False`.

    A zero-argument top-level `bind` node (field:name = a `variable` node)
    is the "global value" signal, cleanly distinguished by the grammar
    itself from a parameterized `function` node (already targeted by
    `_LANG_NODE_TYPES["haskell"]["functions"]`). A destructuring bind such
    as `(a, b) = (1, 2)` puts a `tuple` node in field:pattern instead --
    NOT field:name -- verified empirically; child_by_field_name("name")
    correctly returns None for it, so it is silently excluded rather than
    partially/incorrectly extracted. This is a deliberate simplification,
    not a bug: recognizing destructured tuple binds as multiple globals
    would require pattern-name recursion for comparatively rare top-level
    syntax.

    `module Foo where` produces a sibling `header` field on the root
    node -- verified empirically -- it does NOT wrap the declarations the
    way JS's export_statement or PHP's block-style namespace_definition
    do elsewhere in this plan. field:declarations still holds top-level
    declarations directly regardless of whether a module header is
    present, so no unwrapping is needed.

    `where`-clause local bindings (`f x = y where y = 1`) live nested
    inside the enclosing function node's body (as a `local_binds` node),
    NOT as direct children of field:declarations -- verified empirically
    -- so they are correctly excluded without any special-casing, the
    same as this extractor never recursing into function bodies elsewhere.

    `data Foo = Foo { fieldA :: Int }` is `data_type` (field:name = the
    type name) -> field:constructors -> `data_constructors` -> each
    `data_constructor` (field:constructor) -> if that constructor is
    specifically a `record` node -> field:fields -> `fields` -> each
    `field` child (field:name) -> `field_name` -> `variable` (the actual
    field name text). A `data` type with MULTIPLE constructors mixing
    record and non-record shapes (e.g. `data Shape = Circle { radius ::
    Double } | Rectangle { width :: Double } | Point`) works correctly
    because each `data_constructor` within `data_constructors` gets its
    own independent record-shape check in the loop -- verified
    empirically; a non-record constructor (a `prefix` node, e.g. `Point`)
    is simply skipped, not mistaken for a record.

    `newtype Foo = Foo { unFoo :: Int }` -- a newtype's single record
    field is a very common real Haskell pattern -- is a STRUCTURALLY
    DIFFERENT top-level node from data_type: a `newtype` node (not
    `data_type`) whose field:constructor holds a `newtype_constructor`
    directly (no intermediate `data_constructor`/`data_constructors`
    wrapper at all), and whose record child is reached via the
    confusingly-named field:field (NOT field:record or field:fields) --
    verified empirically. A braces-less newtype (`newtype Foo = Foo
    Int`) puts a plain `field` node (not `record`) at that same
    field:field, so the `.type != "record"` check correctly excludes it
    without misreading its wrapped type name as a field.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def add_field_from_wrapper(field_wrapper: Any, type_name: str) -> None:
        if field_wrapper.type != "field":
            return
        field_name_node = field_wrapper.child_by_field_name("name")
        if field_name_node is None:
            return
        variable_node = next(
            (c for c in field_name_node.children if c.type == "variable"), None
        )
        if variable_node is not None:
            fname = variable_node.text.decode("utf-8")
            fields.append((fname, type_name, False))
            field_info[fname] = {
                "class": type_name, "static": False,
                "body": field_wrapper.text.decode("utf-8", "replace"),
            }
            field_nodes[f"{type_name}.{fname}"] = field_wrapper

    def record_fields(record_node: Any, type_name: str) -> None:
        # A record with multiple comma-separated fields (the shape
        # data_type constructors use) wraps them in a "fields" node at
        # field:fields. A newtype's record -- restricted by the
        # language to exactly one field -- instead exposes that single
        # field node directly at field:field (no wrapper) -- verified
        # empirically. Both shapes are handled here.
        fields_node = record_node.child_by_field_name("fields")
        if fields_node is not None:
            for field_wrapper in fields_node.children:
                add_field_from_wrapper(field_wrapper, type_name)
            return
        single_field = record_node.child_by_field_name("field")
        if single_field is not None:
            add_field_from_wrapper(single_field, type_name)

    declarations = root_node.child_by_field_name("declarations")
    if declarations is None:
        return {
            "globals": [], "global_bodies": {}, "fields": [], "field_info": {},
            "global_nodes": {}, "field_nodes": {},
        }

    for decl in declarations.children:
        if decl.type == "bind":
            name_node = decl.child_by_field_name("name")
            if name_node is not None:
                name = name_node.text.decode("utf-8")
                globals_.append(name)
                global_bodies[name] = decl.text.decode("utf-8", "replace")
                global_nodes[name] = decl
        elif decl.type == "data_type":
            type_name_node = decl.child_by_field_name("name")
            type_name = type_name_node.text.decode("utf-8") if type_name_node else ""
            constructors = decl.child_by_field_name("constructors")
            if constructors is None:
                continue
            for ctor_wrapper in constructors.children:
                if ctor_wrapper.type != "data_constructor":
                    continue
                ctor = ctor_wrapper.child_by_field_name("constructor")
                if ctor is None or ctor.type != "record":
                    continue
                record_fields(ctor, type_name)
        elif decl.type == "newtype":
            type_name_node = decl.child_by_field_name("name")
            type_name = type_name_node.text.decode("utf-8") if type_name_node else ""
            newtype_ctor = decl.child_by_field_name("constructor")
            if newtype_ctor is None:
                continue
            record = newtype_ctor.child_by_field_name("field")
            if record is None or record.type != "record":
                continue
            record_fields(record, type_name)

    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["haskell"] = _extract_haskell_globals_and_fields


def _extract_lua_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Extract true top-level global variable assignments in Lua.

    `_LANG_NODE_TYPES["lua"]["classes"]` is already `set()` -- Lua has no
    class node type at all (table-based OOP is a library convention, not a
    grammar construct) -- so fields are always empty here, consistent with
    that existing precedent. Table-field writes (`Foo.staticField = 1`) are
    deliberately excluded: there is no class entity for such a field to
    attach a `:class` edge to.

    A true global is an `assignment_statement` that is a *direct* child of
    `chunk` whose `variable_list` holds one or more plain `identifier`
    nodes (not `dot_index_expression`, the table-field-write shape) -- and
    which is not wrapped in a `variable_declaration` (the wrapper node
    `local` produces). Because this loop only matches `assignment_statement`
    nodes directly under `chunk`, a `local`-wrapped assignment is
    automatically excluded: its actual `assignment_statement` is one level
    deeper, inside the `variable_declaration` wrapper -- verified
    empirically.

    `local function foo() ... end` and top-level `function foo() ... end`
    both parse as `function_declaration` nodes, never `assignment_statement`
    -- verified empirically -- so they cannot be misidentified as global
    variable assignments here; functions are handled separately by
    `_LANG_NODE_TYPES["lua"]["functions"]`.

    Lua supports multiple assignment in one statement (`a, b = 1, 2`): the
    `variable_list` node exposes each name via a repeated `field:name` --
    verified empirically -- so `children_by_field_name` (plural) is used
    instead of `child_by_field_name`, which would silently return only the
    first name. This is the Lua analog of the multi-assignment gap found in
    every prior language in this plan (Ruby, Kotlin, Swift, Scala). A
    variable_list can also mix plain identifiers with dot_index_expressions
    in the same statement (`a, Foo.x = 1, 2`) -- verified empirically --
    each name node is checked individually so only the plain identifiers
    are captured.
    """
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    global_nodes: Dict[str, Any] = {}

    for stmt in root_node.children:
        if stmt.type != "assignment_statement":
            continue
        var_list = next((c for c in stmt.children if c.type == "variable_list"), None)
        if var_list is None:
            continue
        stmt_text = stmt.text.decode("utf-8", "replace")
        for name_node in var_list.children_by_field_name("name"):
            if name_node.type == "identifier":
                name = name_node.text.decode("utf-8")
                globals_.append(name)
                global_bodies[name] = stmt_text
                global_nodes[name] = stmt

    return {
        "globals": globals_, "global_bodies": global_bodies, "fields": [], "field_info": {},
        "global_nodes": global_nodes, "field_nodes": {},
    }


_GLOBAL_FIELD_EXTRACTORS["lua"] = _extract_lua_globals_and_fields


def _extract_elixir_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    """Extract Elixir module attributes as static fields of their module.

    Elixir has no top-level mutable globals outside module attributes --
    verified empirically there is no syntactic construct (destructuring or
    otherwise) that produces one -- so `"globals"` is always empty here.

    A module is a `call` node whose `field:target` is an `identifier` with
    text `"defmodule"`, whose module-name argument is an `alias` node inside
    an `arguments` child, and which has a `do_block` child holding the
    module's body. A module attribute (`@module_attr 5`) is a
    `unary_operator` with `field:operator` text `"@"` and `field:operand` a
    `call` node whose `field:target` is the attribute's name; it is treated
    as a `:static=True` field of the enclosing module -- the closest Elixir
    analog to compile-time class-scoped state.

    Verified empirically: unlike `field:target`/`field:operator`/
    `field:operand` (which do resolve via `child_by_field_name`), the
    `arguments` child of a `defmodule` call is *not* exposed under a field
    name here -- `child_by_field_name("arguments")` returns None even though
    an `arguments` node is present as a plain (unnamed-field) child. It must
    be located by scanning `node.children` for `type == "arguments"`
    instead, matching the existing precedent in `_elixir_module_name` above.

    Nested modules (`defmodule Foo do defmodule Bar do ... end end`) are
    handled correctly by this recursive walk: each `defmodule` call's own
    `do_block` is scanned only for its *direct* children when processing
    that module, and a nested `defmodule` call is itself just another node
    the walk recurses into separately, so its attributes are attributed to
    its own (inner) module name -- verified empirically. A dotted single
    declaration (`defmodule Foo.Bar do ... end`) is one `call` node whose
    `alias` node's text is already the full dotted name "Foo.Bar", not two
    levels of AST nesting -- verified empirically.

    A bare attribute reference with no value (`@attr`, reading a
    previously-defined attribute rather than defining one) parses with
    `operand.type == "identifier"`, not `"call"` -- verified empirically --
    so it is naturally excluded by the `operand.type == "call"` check below
    and never mistaken for a field definition.

    `@moduledoc`/`@doc`/`@spec`/`@type` (Elixir's built-in documentation/
    typespec attributes) parse identically to any other module attribute --
    verified empirically, same `unary_operator` -> `call` shape -- and are
    deliberately *not* excluded as noise here: no other language task in
    this plan special-cases built-in annotations/decorators, and inventing
    a bespoke exclusion list was not requested by this task's spec.
    """
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}
    field_nodes: Dict[str, Any] = {}

    def walk(node: Any) -> None:
        if node.type == "call":
            if _elixir_call_target_text(node) == "defmodule":
                module_name = _elixir_defmodule_name(node) or ""
                do_block = next((c for c in node.children if c.type == "do_block"), None)
                if do_block is not None:
                    for member in do_block.children:
                        if member.type == "unary_operator":
                            op = member.child_by_field_name("operator")
                            operand = member.child_by_field_name("operand")
                            if op is not None and op.text == b"@" and operand is not None and operand.type == "call":
                                attr_target = operand.child_by_field_name("target")
                                if attr_target is not None:
                                    fname = attr_target.text.decode("utf-8")
                                    fields.append((fname, module_name, True))
                                    field_info[fname] = {
                                        "class": module_name, "static": True,
                                        "body": member.text.decode("utf-8", "replace"),
                                    }
                                    field_nodes[f"{module_name}.{fname}"] = member
        for child in node.children:
            walk(child)

    walk(root_node)
    return {
        "globals": [], "global_bodies": {}, "fields": fields, "field_info": field_info,
        "global_nodes": {}, "field_nodes": field_nodes,
    }


_GLOBAL_FIELD_EXTRACTORS["elixir"] = _extract_elixir_globals_and_fields


def _extract_from_source(
    source: bytes, parser: Any, file_path: str
) -> Dict[str, Any]:
    """Parse source bytes and extract functions, classes, imports, calls,
    module-level globals, and class fields — the plain-data structural summary
    of a file.

    This dict crosses the ProcessPoolExecutor boundary (see #116) as part of
    _extract_commit's return value, so it deliberately carries ONLY the
    lightweight name/structure lists the downstream pipeline actually consumes.
    It does NOT carry entity body text: the rename matcher
    (_match_renamed_entities) operates on live, re-parsed tree_sitter nodes
    collected inside the worker process (_collect_entity_nodes /
    _extract_globals_and_fields' *_nodes keys, via _extract_commit), never on
    decoded body text — so shipping full function/class/global bodies and
    per-field metadata across the process boundary would be pure serialization
    cost for no consumer.
    """
    results: Dict[str, Any] = {
        "functions": [], "classes": [], "imports": [], "calls": [],
        "globals": [], "fields": [],
    }
    try:
        tree = parser.parse(source)
        lang_name = _EXT_TO_LANG.get(Path(file_path).suffix.lower(), "")
        _walk_ast(tree.root_node, results, lang_name)
        gf = _extract_globals_and_fields(tree.root_node, "typescript" if lang_name == "tsx" else lang_name)
        results["globals"] = gf["globals"]
        results["fields"] = gf["fields"]
    except Exception:
        pass  # best-effort; parse failures are non-fatal
    return results


def _match_candidate_pair(
    old_node: Any,
    new_node: Any,
    tracked_names: Dict[str, Optional[str]],
    tracked_reserved: Optional[Dict[str, int]] = None,
    exclude_names: Tuple[str, ...] = (),
    exclude_reserved: Tuple[str, ...] = (),
) -> Optional[Dict[str, str]]:
    """Lockstep-walk two tree-sitter nodes, allowing local (untracked)
    identifiers to differ under a one-to-one bijective mapping.

    tracked_names maps every entity name known in this commit's context to
    either None (must appear unchanged) or a confirmed new name (must appear
    renamed to exactly that). Any identifier NOT a key in tracked_names is
    treated as local/unresolved and is free to differ, as long as the
    mapping stays consistent (same old token always maps to the same new
    token) and injective (no two distinct old tokens collapse onto one new
    token) for THIS candidate pair only — the mapping is never reused across
    other pairs or persisted as an entity.

    Returns the discovered bijection dict on a full match (empty dict if no
    local identifiers were involved — plain exact match is the case where
    the bijection happens to be the identity mapping), or None if the nodes
    don't match structurally or a tracked/bijection constraint is violated.

    Internally, `mapping`/`local_reverse` record EVERY local identifier
    correspondence seen (including identity ones, e.g. an untouched
    parameter name) so consistency and injectivity can be enforced across
    the whole pair. Injectivity against *tracked* entities is enforced
    separately via `tracked_reserved`, a multiset {new-side-token: count}
    covering every tracked entity's new-side text (its confirmed rename
    target, or its own unchanged name when tracked_names[name] is None) —
    otherwise a local/untracked identifier could silently claim the exact
    new text already reserved for a different, tracked entity, which is a
    real injectivity violation (two distinct old tokens collapsing onto one
    new token) that the tracked-name equality check alone doesn't catch
    since it lives in a disjoint namespace from `mapping`.

    Performance (see the O(n^3) matcher fix): tracked_names/tracked_reserved
    are built ONCE per matching round by _match_renamed_entities and passed
    in read-only, rather than reconstructed per candidate pair. The pair's
    own two names are excluded cheaply via exclude_names (skip the tracked
    equality constraint — they are exactly what's under test) and
    exclude_reserved (their reserved new-side tokens, decremented from the
    multiset so the pair may legitimately map onto them). Both are tiny
    (<=2 entries), so exclusion is O(1) per identifier instead of an
    O(all_names) dict rebuild per pair. When tracked_reserved is None it is
    derived from tracked_names (the standalone/test call path); production
    callers pass it precomputed.
    """
    if tracked_reserved is None:
        tracked_reserved = {}
        for name, target in tracked_names.items():
            tok = target if target is not None else name
            tracked_reserved[tok] = tracked_reserved.get(tok, 0) + 1

    mapping: Dict[str, str] = {}
    local_reverse: Dict[str, str] = {}

    def walk(a: Any, b: Any) -> bool:
        if a.type != b.type:
            return False
        if a.child_count == 0 and b.child_count == 0:
            a_text = a.text.decode("utf-8", "replace")
            b_text = b.text.decode("utf-8", "replace")
            if a.type == "identifier" or a.type.endswith("_identifier"):
                if a_text in tracked_names and a_text not in exclude_names:
                    expected = tracked_names[a_text]
                    return b_text == (expected if expected is not None else a_text)
                if a_text in mapping:
                    return mapping[a_text] == b_text
                if b_text in local_reverse:
                    return False
                # Injectivity vs tracked entities: b_text may not claim a
                # new-side token already reserved by a tracked name, EXCEPT
                # the (<=2) reserved tokens belonging to this pair's own
                # excluded names.
                reserved = tracked_reserved.get(b_text, 0) - exclude_reserved.count(b_text)
                if reserved > 0:
                    return False
                mapping[a_text] = b_text
                local_reverse[b_text] = a_text
                return True
            return a_text == b_text
        if a.child_count != b.child_count:
            return False
        return all(walk(ac, bc) for ac, bc in zip(a.children, b.children))

    return {k: v for k, v in mapping.items() if k != v} if walk(old_node, new_node) else None


_MAX_MATCH_ROUNDS = 10
_MIN_MATCH_BODY_LEN = 20  # normalized chars; avoids matching trivial boilerplate stubs
# A pair straddling two files with no established relationship (i.e. not the
# same path, and not linked by a git-detected "R" rename — see file_groups in
# _match_renamed_entities) is confirmed on structural equality alone, with no
# corroborating evidence a real rename/move happened. That's a much weaker
# signal than a same-file or git-confirmed-rename match, so it needs a much
# larger body before two entities are unlikely to collide by coincidence
# (#174 — a same-shaped one-line boilerplate stub/getter/repr in two unrelated
# classes/files cleared the old single 20-char floor easily and was
# confirmed as a false "rename").
_MIN_CROSS_FILE_MATCH_BODY_LEN = 80
# Above this total pool size (removed + added entries across all categories)
# the matcher skips a commit entirely, mirroring git's own `-M` rename-limit
# degradation: a missed rename is the accepted fallback, never an unbounded
# (~cubic) stall on a 20k+-file vendored-dependency commit. Overridable via
# MINIGRAF_MATCH_MAX_POOL, following the env-var pattern used elsewhere here.
_MAX_MATCH_POOL_SIZE = int(os.environ.get("MINIGRAF_MATCH_MAX_POOL", "3000"))


def _normalize_body_for_matching(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _match_body_name(category: str, name: str) -> str:
    """Map an entity's pool key to the bare identifier that actually appears in
    its body text, for the matcher's internal name bookkeeping.

    Fields are pooled under QUALIFIED keys ("Class.leaf", per Task 11) so that
    same-named fields in different classes get distinct, collision-free
    :type/field idents downstream — that qualification MUST stay on the pool
    keys and on _match_renamed_entities' returned pairs (both the ident
    construction in Task 11/27 and renamed_pairs depend on it). But a field's
    body text only ever contains its BARE leaf identifier: the declaration
    `Config = 1` contains the token `Config`, never `A.Config`.

    _match_candidate_pair matches body-text tokens, so _match_renamed_entities'
    tracked-name set, confirmed-rename map, and per-pair self-exclusion must all
    key on that bare leaf, not the qualified pool key. Otherwise a field's own
    leaf token is never excluded from its own candidate test and can be captured
    by an UNRELATED already-confirmed rename that happens to share the bare name
    (e.g. a function `Config`->`ConfigFn` confirmed in an earlier round), which
    forces the field's body token to a specific new spelling and produces a
    WRONG confirmed match. Non-field categories are already unqualified (pool
    key == body identifier), so they pass through unchanged.
    """
    if category == "field":
        return name.rsplit(".", 1)[-1]
    return name


def _match_renamed_entities(
    removed: Dict[str, List[Tuple[str, Any]]],
    added: Dict[str, List[Tuple[str, Any]]],
    unchanged_names: Optional[Set[str]] = None,
    file_groups: Optional[Dict[int, str]] = None,
) -> List[Tuple[str, str, Any, str, Any]]:
    """Round-based rename matching across entity categories, scoped to a
    single commit's touched files (callers build removed/added from just
    that commit — see _extract_commit's use in Task 9).

    file_groups (#174) is an optional {id(node): group_key} map used to tell
    a genuinely file-related candidate pair from a purely coincidental
    cross-file one. Two nodes share a group when they come from the same
    path (an in-file "M" edit) or from the two sides of one git-detected "R"
    rename (including a combined rename+move) — real, evidence-backed
    relationships. Nodes from an unrelated "D" (whole file deleted) and "A"
    (whole file added, different path) pair get distinct, never-equal group
    keys, since git itself draws no connection between them. When a
    candidate pair's groups differ, matching still proceeds but must clear
    the higher _MIN_CROSS_FILE_MATCH_BODY_LEN bar rather than
    _MIN_MATCH_BODY_LEN — cross-file matches remain possible (a function
    really can move from one file to a wholly unrelated new one, since
    that's simply invisible to git's own diff), just at a much narrower,
    less coincidence-prone confidence threshold than same-file/git-rename
    matches. file_groups is None for every standalone/test caller below
    (no file information exists at that level) — the original single
    _MIN_MATCH_BODY_LEN floor applies unchanged in that case, preserving
    this function's pre-#174 behavior for callers that never had file
    context to begin with.

    A rename confirmed in one category (e.g. a function) becomes available
    as a "tracked, confirmed-renamed" name for other not-yet-matched pairs
    (in the same or a different category) evaluated in a later round — this
    resolves cascading/mutual renames within one commit regardless of
    dependency order. Capped at _MAX_MATCH_ROUNDS as a defensive bound.

    unchanged_names is the set of BARE body-text identifiers (see
    _match_body_name) that are present, with the SAME name, on BOTH the old
    and new side of a touched file this commit — i.e. tracked entities that
    survived unrenamed. The design requires a reference to such an entity to
    "match exactly" rather than be treated as a free local eligible for
    bijective substitution. These names never appear in the removed/added
    pools (an unchanged same-path entity is excluded from both by the "M"
    diff), so without seeding them here the matcher would treat a reference
    to a surviving helper as a free local and could confirm a FALSE rename
    between two entities that merely call two different, still-present
    helpers. They are seeded into tracked_names with target None (must appear
    identically); a name that is ALSO confirmed renamed this round takes the
    confirmed target instead (confirmed wins, so a genuine rename still
    resolves). The pair under test always excludes its own name from the
    constraint (see the self-exclusion below), so a real rename whose old and
    new bodies share an unchanged helper still matches.

    Mutates removed/added in place, removing matched entries.

    Returns (category, old_name, old_node, new_name, new_node) 5-tuples —
    the matched node objects are included (not just their names) because
    this function is file-path-agnostic by design (reused as-is for Task
    26's globals/fields), yet callers like _extract_commit need to recover
    which file each side came from. Two different removed entities in two
    different deleted files can coincidentally share a name, so the name
    alone isn't a safe lookup key back to a file — the node identity is.
    (Retrofitted here, while wiring this into _extract_commit in Task 9,
    from the original 3-tuple (category, old_name, new_name) shape.)
    """
    matches: List[Tuple[str, str, Any, str, Any]] = []
    # Keyed by BARE body-text identifier (see _match_body_name), not the pool
    # key, because a field's qualified pool key ("Class.leaf") never appears as
    # a token in any body text — only its bare leaf does.
    confirmed: Dict[str, str] = {}  # bare old_name -> bare new_name, shared across all categories

    # Pool-size guard: above _MAX_MATCH_POOL_SIZE total entries the pairwise
    # scan (inherently O(removed x added) per category per round) is skipped
    # outright, mirroring git's -M rename-limit degradation. A missed rename
    # is the accepted fallback; the entity is simply treated as removed+added.
    total_pool = sum(len(v) for v in removed.values()) + sum(len(v) for v in added.values())
    if total_pool > _MAX_MATCH_POOL_SIZE:
        return matches

    all_names: set = set(unchanged_names or ())
    for pool in (removed, added):
        for category, entries in pool.items():
            all_names.update(_match_body_name(category, name) for name, _node in entries)

    for _round in range(_MAX_MATCH_ROUNDS):
        changed = False
        # Seed every known name to its confirmed rename target if it has one,
        # else None ("must match exactly"). Unchanged-tracked names (folded
        # into all_names above) therefore default to None unless a genuine
        # rename was confirmed for them this round, in which case confirmed
        # wins. Both tracked_names and its derived new-side reserved-token
        # multiset are built ONCE per round and passed read-only into every
        # _match_candidate_pair call — the pair's own two names are excluded
        # cheaply per-pair (see below) instead of rebuilding an O(all_names)
        # dict per candidate, which was the cubic term in the old code.
        tracked_names: Dict[str, Optional[str]] = {
            name: confirmed.get(name) for name in all_names
        }
        tracked_reserved: Dict[str, int] = {}
        for name, target in tracked_names.items():
            tok = target if target is not None else name
            tracked_reserved[tok] = tracked_reserved.get(tok, 0) + 1
        for category in list(removed.keys()):
            r_list = removed.get(category, [])
            a_list = added.get(category, [])
            for r_name, r_node in list(r_list):
                r_text = _normalize_body_for_matching(r_node.text.decode("utf-8", "replace"))
                if len(r_text) < _MIN_MATCH_BODY_LEN:
                    continue
                # The bare identifier the pair-under-test actually spells in its
                # body (equal to the pool key for non-field categories). Used
                # for self-exclusion below so the walker treats the pair's own
                # name as free/under-test, never as an inherited constraint.
                # Its reserved new-side token (own confirmed target, else the
                # name itself) is excluded from the injectivity multiset for
                # the same reason.
                r_match_name = _match_body_name(category, r_name)
                r_reserved_token = confirmed.get(r_match_name, r_match_name)
                candidates = []
                for a_name, a_node in a_list:
                    if file_groups is not None:
                        # #174: a pair with no established file relationship
                        # (different group keys — see file_groups' docstring)
                        # needs a much larger body before structural equality
                        # alone is trusted as rename evidence.
                        same_file_group = file_groups.get(id(r_node)) == file_groups.get(id(a_node))
                        min_len = _MIN_MATCH_BODY_LEN if same_file_group else _MIN_CROSS_FILE_MATCH_BODY_LEN
                        if len(r_text) < min_len:
                            continue
                    a_match_name = _match_body_name(category, a_name)
                    a_reserved_token = confirmed.get(a_match_name, a_match_name)
                    # Exclude this specific pair's own old/new names from the
                    # tracked-equality constraint and their reserved tokens
                    # from the injectivity multiset: they are exactly what's
                    # under test here (is r_name renamed to a_name?), not an
                    # already-known constraint. Without this, an unconfirmed
                    # entity's own name would be treated as "must stay
                    # unchanged" and no rename could ever be confirmed for it.
                    # Excluding by BARE name (not the qualified pool key) is
                    # essential for fields: the body token is the bare leaf, so
                    # excluding "A.Config" would leave the field's own "Config"
                    # token still bound to an unrelated confirmed
                    # "Config"->... rename (the false positive this closes).
                    try:
                        matched = _match_candidate_pair(
                            r_node,
                            a_node,
                            tracked_names,
                            tracked_reserved=tracked_reserved,
                            exclude_names=(r_match_name, a_match_name),
                            exclude_reserved=(r_reserved_token, a_reserved_token),
                        )
                    except RecursionError:
                        # A single pathological pair — an AST deep enough to
                        # survive _collect_entity_nodes but blow the recursion
                        # limit inside the pair walk (which uses more stack per
                        # level) — must degrade to no-match for THIS pair only,
                        # not abort the whole commit's matching (and, via
                        # _extract_commit's outer propagation, the entire
                        # ingestion run). Skip it and keep testing the rest.
                        continue
                    if matched is not None:
                        candidates.append((a_name, a_node))
                        # Ambiguity is already certain at 2 candidates: the
                        # match is only kept when exactly one survives, so
                        # walking the 3rd, 4th, ... against this same removed
                        # entry is pure wasted work. Bail the inner scan (never
                        # the outer removed-entries loop).
                        if len(candidates) >= 2:
                            break
                if len(candidates) == 1:
                    a_name, a_node = candidates[0]
                    # Returned pair keeps the QUALIFIED pool keys (r_name/a_name)
                    # for downstream ident construction; only the shared
                    # confirmed map records the bare body names.
                    matches.append((category, r_name, r_node, a_name, a_node))
                    confirmed[r_match_name] = _match_body_name(category, a_name)
                    r_list.remove((r_name, r_node))
                    a_list.remove((a_name, a_node))
                    changed = True
        if not changed:
            break
    return matches


def _collect_entity_nodes(root_node: Any, lang_name: str) -> Dict[str, Dict[str, Any]]:
    """Like _walk_ast, but returns live nodes keyed by name instead of text —
    for use only inside a single worker-process call (_extract_commit), never
    returned across the ProcessPoolExecutor boundary. Only functions/classes
    are collected here; Task 26 extends this for globals/fields once those
    categories exist.
    """
    result: Dict[str, Dict[str, Any]] = {"function": {}, "class": {}}

    if lang_name == "elixir":
        def walk_elixir(node: Any) -> None:
            if node.type == "call":
                target_text = _elixir_call_target_text(node)
                if target_text == "defmodule":
                    name = _elixir_defmodule_name(node)
                    if name:
                        result["class"][name] = node
                elif target_text in (
                    "def", "defp", "defmacro", "defmacrop",
                    "defguard", "defguardp", "defdelegate",
                ):
                    name = _elixir_def_function_name(node)
                    if name:
                        result["function"][name] = node
            for child in node.children:
                walk_elixir(child)

        walk_elixir(root_node)
        return result

    node_types = _LANG_NODE_TYPES.get("typescript" if lang_name == "tsx" else lang_name)
    if node_types is None:
        return result

    def walk(node: Any) -> None:
        if node.type in node_types.get("functions", set()):
            if lang_name in ("c", "cpp"):
                name = _c_family_function_name(node)
                if name:
                    result["function"][name] = node
            else:
                name_node = node.child_by_field_name("name")
                if name_node:
                    result["function"][name_node.text.decode("utf-8")] = node
        elif node.type in node_types.get("classes", set()):
            if lang_name == "go":
                for name, type_spec in _go_struct_type_specs(node):
                    result["class"][name] = type_spec
            else:
                name_node = node.child_by_field_name("name")
                if name_node:
                    result["class"][name_node.text.decode("utf-8")] = node
        for child in node.children:
            walk(child)

    walk(root_node)
    return result


def _normalized_body_hash(node: Any) -> str:
    """Whitespace-insensitive content hash of a tree-sitter node's span.

    Joins the text of every leaf token (a node with no children) in
    document order, then hashes the result -- so a purely cosmetic reformat
    (e.g. this repo's own periodic clang-format sweeps) hashes identically
    to the original, while any change to the token text stream itself changes
    the hash. No per-language handling needed: leaf-token walking is
    generic across every tree-sitter grammar. Comment text is NOT stripped
    (see #221 design doc's Scope section) -- a comment-only edit still
    counts as a body change in v1. Note: tree structure/indentation changes
    are not captured; in indentation-significant languages, a pure re-indentation
    that changes semantics will hash identically (accepted v1 tradeoff).
    """
    leaves: List[bytes] = []

    def walk(n: Any) -> None:
        if len(n.children) == 0:
            leaves.append(n.text)
        else:
            for child in n.children:
                walk(child)

    walk(node)
    return hashlib.sha256(b"\x00".join(leaves)).hexdigest()


# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------


def _get_graph_path() -> str:
    return os.environ.get("MINIGRAF_GRAPH_PATH", str(Path.cwd() / "memory.graph"))


def open_db(graph_path: Optional[str] = None) -> None:
    """Point this module at a graph. Does NOT leave a handle open.

    Returns None by design: handing back a handle nobody holds a lease on is
    exactly the #255 bug -- the caller has no way to say when it is finished,
    so the release never happens. Callers that need a handle take a lease.
    """
    _lease_manager.bind_path(graph_path or _get_graph_path())


def _is_lock_error(exc: Exception) -> bool:
    """True for the two "someone else has the graph" opens, both retryable.

    minigraf raises two distinct messages, and only one contains "locked":

      * "Database is locked by another process"       -- another PROCESS
      * "Database is already open in this process"    -- another handle HERE
        (project-minigraf/minigraf#304, minigraf >= 1.2.2)

    The second must be matched too. It is transient in exactly the same way:
    the other holder is usually a poller or a call_tool worker about to drop
    its reference, so backing off and retrying is the right response. Matching
    only "locked" made it fall through as a fatal non-lock error and abort the
    caller on the first attempt, turning a momentary overlap into a failed
    ingestion.

    Retrying does NOT paper over the invariant: a genuinely leaked handle still
    exhausts the retry budget and surfaces. It only absorbs the races.
    """
    msg = str(exc).lower()
    return "locked" in msg or "already open in this process" in msg


def _owner_hint_path(graph_path: str) -> str:
    """Path of our ownership hint for graph_path.

    Deliberately NOT named `.lock`: a pre-2.0.0 `.graph.lock` may still be on
    disk (2.0.0 ignores it and never deletes it), and nothing should confuse
    ours with minigraf's.
    """
    return graph_path + ".owner"


def _graph_owner_hint_is_fresh(graph_path: str) -> bool:
    """True if a hint exists and its mtime is within _OWNER_HINT_TTL."""
    try:
        age = time.time() - os.stat(_owner_hint_path(graph_path)).st_mtime
    except OSError:
        return False
    return age < _OWNER_HINT_TTL


def _graph_owner_hint(graph_path: str) -> Optional[Dict[str, Any]]:
    """Return another live process's ownership hint, or None.

    Replaces _live_lock_holder_pid, which read the deleted sidecar and so
    returned None even while another process demonstrably held the graph.

    None means "no reason to decline": absent, stale, unreadable, or ours.
    Self-detection compares pid AND host, because a PID from another machine
    on a shared filesystem means nothing -- matching on pid alone would let an
    unrelated remote holder look like our own handle.
    """
    if not _graph_owner_hint_is_fresh(graph_path):
        return None
    try:
        with open(_owner_hint_path(graph_path)) as f:
            hint = json.load(f)
    except (OSError, ValueError):
        return None  # unreadable or malformed -- advisory only, so ignore it
    if not isinstance(hint, dict) or not isinstance(hint.get("pid"), int):
        return None
    if hint.get("pid") == os.getpid() and hint.get("host") == socket.gethostname():
        return None  # our own hint, not another process
    return hint


def _write_owner_hint(graph_path: str, purpose: str) -> bool:
    """Publish our ownership hint. Returns False if it could not be written.

    Best-effort by contract: a hint we cannot write must never be able to stop
    real work, so every failure here degrades to "no hint".
    """
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "purpose": purpose,
        "started": _now_utc_ms(),
        "graph": graph_path,
    }
    try:
        with open(_owner_hint_path(graph_path), "w") as f:
            json.dump(payload, f)
        return True
    except OSError:
        return False


def _touch_owner_hint(graph_path: str) -> None:
    """Refresh the hint's mtime -- the heartbeat that proves we are alive."""
    try:
        os.utime(_owner_hint_path(graph_path), None)
    except OSError:
        pass


def _remove_owner_hint(graph_path: str) -> None:
    try:
        os.remove(_owner_hint_path(graph_path))
    except OSError:
        pass


def _start_owner_hint_heartbeat(graph_path: str) -> Callable[[], None]:
    """Refresh the hint's mtime on a timer. Returns a cancel callable.

    Uses loop.call_later rather than a coroutine awaiting asyncio.sleep. A
    sleeping coroutine would consume `mcp_server.asyncio.sleep`, which
    ingestion tests patch to instrument the per-commit yield cadence -- the
    heartbeat would then spin against the patched sleep and inject snapshots
    into a measurement it has nothing to do with. A timer is also simply the
    right primitive for a heartbeat: it schedules the next tick from the
    completion of the last, and never competes for the event loop.
    """
    loop = asyncio.get_running_loop()
    state: Dict[str, Any] = {"handle": None, "cancelled": False}

    def tick() -> None:
        if state["cancelled"]:
            return
        _touch_owner_hint(graph_path)
        state["handle"] = loop.call_later(_OWNER_HINT_HEARTBEAT, tick)

    state["handle"] = loop.call_later(_OWNER_HINT_HEARTBEAT, tick)

    def cancel() -> None:
        state["cancelled"] = True
        if state["handle"] is not None:
            state["handle"].cancel()

    return cancel


@contextlib.asynccontextmanager
async def _graph_owner_hint_held(graph_path: str, purpose: str):
    """Publish an ownership hint for the duration of a LONG-held claim.

    Published only around long-held ownership (ingestion), not around every
    lease. That is a deliberate narrowing of what the sidecar reader used to
    see: declining to start ingestion because another process ran a 50ms query
    would be wrong, and the lease's own retry already absorbs short overlaps.

    The heartbeat is a dedicated timer rather than a refresh folded into the
    per-commit progress update, so that one slow commit cannot let the hint
    expire while ingestion is demonstrably alive.
    """
    written = _write_owner_hint(graph_path, purpose)
    cancel_heartbeat = _start_owner_hint_heartbeat(graph_path) if written else None
    try:
        yield
    finally:
        if cancel_heartbeat is not None:
            cancel_heartbeat()
        if written:
            _remove_owner_hint(graph_path)


class _LeasedDb:
    """What a lease hands out. Forwards to the real MiniGrafDb, and severs
    that link the moment the lease ends.

    `with db_lease() as db:` does NOT unbind `db` at block exit -- that is
    plain Python. Without this wrapper the caller's surviving binding keeps the
    real handle alive past its own lease: the count reaches zero with the
    handle still referenced, and the next acquire opens a SECOND handle on the
    same file. Measured, that raises "Database is already open in this process"
    on the second iteration of any loop -- precisely the shape of
    _run_ingestion's per-commit loop.

    Severing here removes ONE specific way a release fails to genuinely
    release: the caller's surviving `as db` binding. Without it every lease
    block would have to end by dropping its own binding, which is the same
    invisible ordering rule that produced #251/#253, merely relocated.

    It does NOT make release a property of the protocol in full generality.
    A reference to the real handle can still survive through a channel this
    class does not touch -- concretely, an exception raised inside a native
    call keeps the raw MiniGrafDb alive via its traceback's `execute` frame
    (that frame's `self`), and severing `_handle` here changes nothing about
    that. See "THE SINGLE-HANDLE INVARIANT" comment (below `_db_native_lock`,
    near the top of this module) for the empirical case and what catches it
    (the leak detector, at the next acquire -- not this class).

    __getattr__ fires only for attributes not found normally, so `_handle`
    (a slot) and `_sever` resolve directly and never recurse.
    """

    __slots__ = ("_handle",)

    def __init__(self, handle: MiniGrafDb) -> None:
        object.__setattr__(self, "_handle", handle)

    def _sever(self) -> None:
        object.__setattr__(self, "_handle", None)

    def __getattr__(self, name: str) -> Any:
        handle = object.__getattribute__(self, "_handle")
        if handle is None:
            raise RuntimeError(
                f"graph handle used after its lease ended (attribute {name!r}) "
                f"-- take a new lease with db_lease() rather than holding one "
                f"past its block"
            )
        return getattr(handle, name)

    def __repr__(self) -> str:
        live = object.__getattribute__(self, "_handle") is not None
        return f"<_LeasedDb {'live' if live else 'released'}>"


def _open_for_lease(path: str) -> MiniGrafDb:
    """Open a handle FOR THE LEASE MANAGER, self-healing a stale lock.

    Deliberately never publishes into a module global -- that publication was
    _open_db_at's contract, and it was precisely the stray reference the
    lease protocol exists to remove: release() could never drop the last
    reference while a global also held the handle, so the lock file would
    survive every lease and the next open would be a second handle.

    This is now the ONLY opener. `_db`, `_open_db_at`, `_try_open_with_self_heal`
    and the two retry wrappers (_open_db_at_with_retry,
    _open_db_at_with_extended_retry) are deleted -- the self-heal logic below
    is what remains of them, minus the global publication.
    """
    try:
        return MiniGrafDb.open(path)
    except Exception as e:
        if not _is_lock_error(e):
            raise
        # The stale-lock self-heal that used to live here is gone (#284).
        # It scraped a holder PID out of the error text and deleted the
        # sidecar when that PID was dead. Measured, it recovered nothing on
        # EITHER version: 2.0.0 has no sidecar at all and the kernel releases
        # the lock on process exit however it exits, while 1.2.3 leaves the
        # sidecar behind after a SIGKILL but reopens successfully anyway,
        # checking the recorded PID's liveness itself. Do not reintroduce it
        # from 1.2.3's "delete the lock file manually" error text -- that text
        # invites exactly this mistake. See the probe's stale_recovery section.
        raise


def _describe_referrers(obj: Any, limit: int = 6) -> str:
    """Name the places still referencing obj, for the lease-leak diagnostic.

    Reports the BINDING NAME where it can, because "still held by: escaped"
    is actionable and "still held by: dict" is not. Two mechanisms, because
    neither alone covers the common case on this interpreter (Python 3.13+,
    PEP 667):

    * A call-stack walk checks every live frame's f_locals for a name bound
      to obj. This is the primary path -- a leaked lease handle is almost
      always sitting in a caller's local variable. But PEP 667 turned
      f_locals into a write-through proxy (not a plain dict), and an
      ordinary function frame is frequently not gc-tracked at all, so
      gc.get_referrers(obj) returns nothing for it -- verified empirically:
      it silently produced "<no named holder found>" for exactly this case
      before the stack walk was added, which is the "verification fails
      open" failure mode this project has been bitten by before.
    * gc.get_referrers still covers what the stack walk cannot: a module's
      globals (a real dict, found this way) or another object's __dict__
      holding the reference from outside the current call stack.

    Two innermost frames are skipped when walking the stack: this function's
    own (holds `obj` itself as a parameter) always, and its direct caller's
    only when that caller is `_detect_leaked_handle` (holds the weakref
    target in a local for the duration of this call) -- both are artifacts of
    running the diagnostic, not a real holder. The second skip is matched by
    code identity rather than a fixed stack depth, so a second, unanticipated
    caller only loses that one (harmless) skip rather than silently
    mis-skipping a real frame.

    Best-effort by nature -- a reference held only from a C-level structure
    has no name to report.
    """
    found: List[str] = []

    frame = sys._getframe(1)  # skip this function's own frame
    if frame is not None and frame.f_code is _DbLeaseManager._detect_leaked_handle.__code__:
        frame = frame.f_back  # also skip _detect_leaked_handle's bookkeeping local
    while frame is not None and len(found) < limit:
        for name, value in frame.f_locals.items():
            if value is obj and name not in found:
                found.append(name)
        frame = frame.f_back

    for referrer in gc.get_referrers(obj):
        if len(found) >= limit:
            break
        if isinstance(referrer, dict):
            names = [k for k, v in referrer.items() if v is obj]
            if names:
                found.append(", ".join(sorted(names)))
        elif hasattr(referrer, "f_code"):
            code = referrer.f_code
            found.append(f"{os.path.basename(code.co_filename)}:{code.co_name}")
        else:
            found.append(type(referrer).__name__)

    return "; ".join(found) if found else "<no named holder found>"


class _DbLeaseManager:
    """Owns this process's single MiniGrafDb handle and its lifetime (#255).

    `_db = None` was this module's "release the graph file lock" idiom. It is
    not one: it releases only when it drops the LAST reference, so any local
    `db` still on a stack keeps the handle -- and its lock -- alive while the
    global says otherwise. That is the #251/#253 mechanism, and since minigraf
    1.2.2 it surfaces as "Database is already open in this process" rather than
    as silent page-table corruption.

    Here the count is authoritative. The handle is opened at 0 -> 1 and dropped
    at 1 -> 0; every acquisition in between reuses it. `_db_native_lock` is a
    separate concern and is NOT folded in: it serializes CALLS INTO a handle,
    while this class governs the handle's LIFETIME.

    THIS IS NOT THE WEAKREF GUARD REJECTED IN #253. That one reused a handle
    whenever a weakref to it was still live, including handles whose backing
    file had been deleted -- it resurrected dead graphs and segfaulted the
    suite 3/3 runs. Reuse here requires count > 0: a caller is inside a `with`
    block right now, so the file cannot have been torn down under them. A live
    weakref at count == 0 is the opposite of a reuse candidate -- it is the
    leak signal (see _detect_leaked_handle).

    One open attempt runs under self._lock, and the caller backs off OUTSIDE
    it. Holding the lock across the attempt is what makes a same-process double
    open impossible: a second thread blocks, then finds count > 0 and joins.
    Sleeping under it would serialize every waiter behind one backoff budget.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handle: Optional[MiniGrafDb] = None
        self._path: str = ""
        self._count: int = 0
        self._prev_ref: Optional["weakref.ref"] = None
        # Set False only by a test that deliberately reconstructs a leak (see
        # the #255 interleaving ablation). Defaults to strict; flipping the
        # default would quietly turn the always-on detector into a test-only one.
        self.strict_leak_detection: bool = True

    @property
    def lease_count(self) -> int:
        with self._lock:
            return self._count

    @property
    def path(self) -> str:
        with self._lock:
            return self._path

    def bind_path(self, path: str) -> None:
        """Point the manager at a graph without opening it (open_db's job)."""
        with self._lock:
            if self._count > 0 and self._path and path != self._path:
                raise RuntimeError(
                    f"cannot bind {path!r}: {self._count} lease(s) outstanding "
                    f"on {self._path!r}"
                )
            self._path = path

    def try_acquire(self, path: Optional[str] = None) -> Optional[MiniGrafDb]:
        """One acquisition attempt.

        path=None resolves the target from self._path (falling back to
        _get_graph_path()) INSIDE this method's lock, which is what
        db_lease()/db_lease_async() pass -- resolving it themselves first and
        handing in the result would read self._path OUTSIDE the lock, and a
        bind_path(new) landing at count 0 in the gap between that read and
        this call would silently target the stale path instead of the
        current one. _get_graph_path() is a cheap env lookup, so resolving it
        on every attempt costs nothing. Callers that need to request or
        verify a SPECIFIC path (tests, the path-conflict check itself) still
        pass one explicitly, unaffected by this default.

        Returns the leased handle, or None if the graph file lock is currently
        held by another PROCESS -- the caller backs off and retries. Raises for
        any non-lock error and for a path conflict.
        """
        with self._lock:
            if path is None:
                path = self._path or _get_graph_path()
            if self._count > 0:
                if path != self._path:
                    raise RuntimeError(
                        f"lease requested for {path!r} while {self._count} "
                        f"lease(s) are outstanding on {self._path!r}"
                    )
                self._count += 1
                return self._handle

            self._detect_leaked_handle(path)
            try:
                handle = _open_for_lease(path)
            except Exception as e:
                if _is_lock_error(e):
                    return None
                raise

            # Rules are registered under the lock, before the count goes
            # positive: a thread joining at count > 0 must never observe a
            # handle whose session rules are half-registered.
            try:
                for rule in SESSION_RULES:
                    _db_execute(handle, rule)
                for rule in _user_rules:
                    _db_execute(handle, rule)
            except Exception:
                # A raise here leaves self._handle/_count/_prev_ref untouched
                # -- count stays 0, exactly as if this acquire had never
                # started. But `handle` itself is real and open right now,
                # and it survives in THIS frame via the propagating
                # traceback, so the file lock is still held: count 0, handle
                # alive, which is the state the whole protocol exists to
                # abolish (see _LeasedDb's docstring and the module's
                # SINGLE-HANDLE INVARIANT comment). Feed it to the detector
                # ourselves -- _prev_ref would otherwise stay None, and
                # _detect_leaked_handle silently no-ops on None, so the very
                # failure most likely to strand a handle would also be the
                # one the detector stays blind to.
                self._prev_ref = weakref.ref(handle)
                del handle
                raise

            self._handle = handle
            self._path = path
            self._count = 1
            self._prev_ref = None
            return handle

    def release(self) -> None:
        with self._lock:
            if self._count <= 0:
                raise RuntimeError(
                    "release() called with no outstanding lease -- an unbalanced "
                    "release would drop a handle another caller is still using"
                )
            self._count -= 1
            if self._count == 0:
                handle, self._handle = self._handle, None
                if handle is not None:
                    # The detector's input. If this weakref is still live at the
                    # next 0 -> 1 acquire, someone escaped their lease.
                    self._prev_ref = weakref.ref(handle)
                    # `handle` is a real strong reference sitting in THIS frame,
                    # not just in self._handle. self._handle = None above drops
                    # the manager's own reference, but that alone does not free
                    # the object while this local still names it -- and this
                    # local survives until the `with self._lock:` block exits
                    # and release() returns. Under contention, the lock can wake
                    # a waiting acquirer (which runs _detect_leaked_handle) in
                    # that gap, before this frame is torn down: the lock says
                    # "free" but the weakref is still live, and the detector
                    # reports a leak that isn't one -- <no named holder found>,
                    # since the only holder is this dead-but-not-yet-cleared
                    # local, invisible to gc.get_referrers by the time it looks.
                    # Delete it here, still under the lock, so the reference
                    # count actually reaches zero before anyone can observe the
                    # lock as free. Do not "simplify" this away.
                    del handle

    def reset(self) -> None:
        """Force the manager back to its initial state.

        Test-only. _run_ingestion's error path deliberately does NOT call
        this -- see the comment at its `except Exception as e:` handler
        (mcp_server.py, near _ingest_progress["status"] = "error") for why:
        every ingestion lease is `with`-scoped, so by the time an exception
        reaches that handler there is nothing of THIS run's to clean up, and
        calling reset() there would desync the count if some OTHER caller
        holds a legitimate concurrent lease. Runs the leak detector first,
        so a test that leaks a handle and then resets is blamed at its own
        teardown rather than at its successor's first acquire.
        """
        with self._lock:
            self._detect_leaked_handle(self._path or "<reset>")
            # Tempting to weakref self._handle into _prev_ref here (when it
            # is not None) before discarding it, the same way release() does
            # -- otherwise a leak that straddles a reset() is permanently
            # undetectable: nothing else will ever check on this handle
            # again. Tried it; reverted. It makes TestLeaseAcquireCannotBe
            # RacedByReset.test_reset_cannot_interleave_inside_an_acquire
            # fail: that test deliberately races reset() against a real
            # outstanding try_acquire() (not through db_lease(), so nothing
            # releases it), keeps the handle alive in `outcome["handle"]`
            # afterward to assert it's real, and cleans up with a SECOND
            # _reset_db_state() at the end -- which would see the first
            # reset's weakref still live (outcome["handle"] pins it) and
            # raise a spurious "DB lease leak" from teardown, not from a
            # bug. Reset() is a "force back to zero" escape hatch, not a
            # release -- there is no reliable way from inside it to tell a
            # caller who's mid-flight through a legitimate try_acquire from
            # an actually-abandoned handle. Left undetected; #255 already
            # narrows this to test/eval-only code (reset() is never called
            # in production, see this method's own docstring).
            self._handle = None
            self._count = 0
            self._prev_ref = None
            # The path must clear too, or a graph path set by one test leaks
            # into the next one that never binds its own -- nothing else
            # resets this manager between tests.
            self._path = ""

    def _detect_leaked_handle(self, path: str) -> None:
        """Fire if the previously released handle is still alive.

        Called at 0 -> 1 and from reset(). A live weakref here means a caller
        kept its `db` past the end of its `with` block: the manager dropped its
        reference, the count says "released", and the file lock is still held.
        Left alone, that surfaces later and elsewhere as minigraf's "Database
        is already open in this process". Naming the holder is the whole point
        -- gc.get_referrers is what found the four holder sites in PR #254,
        after two attempts to reason it out from the source were both wrong.

        Must be called with self._lock held.
        """
        ref = self._prev_ref
        if ref is None:
            return
        stale = ref()
        self._prev_ref = None
        if stale is None:
            return  # released cleanly, which is the normal path
        holders = _describe_referrers(stale)
        del stale  # do not let this frame be one of the holders we report
        msg = (
            f"DB lease leak: the handle from the previous lease on {path!r} was "
            f"still alive at the next acquire. A caller kept a reference past "
            f"the end of its `with db_lease()` block, so the graph file lock "
            f"was never released. Still held by: {holders}"
        )
        if self.strict_leak_detection and os.environ.get("PYTEST_CURRENT_TEST"):
            raise RuntimeError(msg)
        print(f"[db_lease] {msg}", file=sys.stderr)


_lease_manager = _DbLeaseManager()


@contextlib.contextmanager
def db_lease(extended: bool = False):
    """Hold a lease on the graph handle for the duration of the block.

    Blocking backoff -- call this OFF the event loop, or from inside an
    already-held async lease (where the count is already positive and no open
    happens). extended=True selects the long time-budgeted backoff that
    _load_ingestion_preload_state needs to survive an orphan-process cleanup
    window (#106) instead of giving up in ~1.55s.
    """
    # No path resolved here: try_acquire(None) resolves self._path (falling
    # back to _get_graph_path()) itself, INSIDE its own lock, on every
    # attempt. Resolving it once here instead -- outside the lock -- would
    # leave a window between that read and the first try_acquire() call in
    # which a bind_path(new) landing at count 0 silently targets the stale
    # path (#255 review). `path` below is for the error messages only, and
    # is deliberately re-read fresh at raise time rather than cached from
    # before the retry loop, for the same reason.
    handle = None
    if extended:
        deadline = time.monotonic() + _INGEST_LOCK_RETRY_BUDGET
        delay = _INGEST_LOCK_RETRY_BASE
        while True:
            handle = _lease_manager.try_acquire()
            if handle is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                path = _lease_manager.path or _get_graph_path()
                raise RuntimeError(
                    f"could not acquire a lease on {path!r} within "
                    f"{_INGEST_LOCK_RETRY_BUDGET}s: the graph file lock did not "
                    f"clear -- see the preceding [db_lease] diagnostic for who is "
                    f"still holding it"
                )
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _INGEST_LOCK_RETRY_CAP)
    else:
        delay = _LOCK_RETRY_BASE
        for attempt in range(_LOCK_RETRY_MAX):
            handle = _lease_manager.try_acquire()
            if handle is not None:
                break
            if attempt < _LOCK_RETRY_MAX - 1:
                time.sleep(delay)
                delay *= 2
        if handle is None:
            path = _lease_manager.path or _get_graph_path()
            raise RuntimeError(
                f"could not acquire a lease on {path!r} after "
                f"{_LOCK_RETRY_MAX} attempts: the graph file lock did not clear "
                f"-- see the preceding [db_lease] diagnostic for who is still "
                f"holding it"
            )
    leased = _LeasedDb(handle)
    handle = None  # this frame must not outlive the lease either
    try:
        yield leased
    finally:
        # Sever BEFORE releasing: the caller's `as db` name is still bound at
        # this point, and severing is what stops it keeping the real handle
        # alive past the count reaching zero.
        leased._sever()
        leased = None
        _lease_manager.release()


@contextlib.asynccontextmanager
async def db_lease_async():
    """Hold a lease, backing off with asyncio.sleep instead of time.sleep.

    Await this from any event-loop coroutine (call_tool, _run_ingestion). A
    blocking sleep here would freeze the single-threaded loop for the whole
    retry budget, and worse, would prevent the very coroutine holding the lock
    from ever releasing it during the wait (#99).
    """
    # See db_lease()'s matching comment: try_acquire(None) resolves the path
    # itself, inside its own lock, on every attempt -- resolving it once here
    # instead would read _lease_manager.path outside the lock and leave a
    # window for a bind_path(new) at count 0 to silently redirect this
    # acquire to a stale path (#255 review).
    handle = None
    delay = _LOCK_RETRY_BASE
    for attempt in range(_LOCK_RETRY_MAX):
        handle = _lease_manager.try_acquire()
        if handle is not None:
            break
        if attempt < _LOCK_RETRY_MAX - 1:
            await asyncio.sleep(delay)
            delay *= 2
    if handle is None:
        path = _lease_manager.path or _get_graph_path()
        raise RuntimeError(
            f"could not acquire a lease on {path!r} after {_LOCK_RETRY_MAX} "
            f"attempts: the graph file lock did not clear -- see the preceding "
            f"[db_lease] diagnostic for who is still holding it"
        )
    leased = _LeasedDb(handle)
    handle = None
    try:
        yield leased
    finally:
        leased._sever()
        leased = None
        _lease_manager.release()


@contextlib.asynccontextmanager
async def _db_lease_async_committing_index(loop, write_executor, index_con):
    """db_lease_async(), plus a commit of the batched fact-index connection
    BEFORE the lease is released -- on every exit path, exceptions included.

    Used by EVERY lease _run_ingestion takes that writes index rows (#347):
    the preload lease, Stage A's per-commit dispatch, Stage B's sweep WINDOW
    (#222 phase 5 item C, where it first shipped), the skipped-span flush,
    the lineage fold and _ingest_tags/_last_run_write. Any of those releases
    lets an out-of-process auto-memory hook take the graph lock. Releasing
    the graph lease while `index_con` still holds an open SQLite write
    transaction is a lock-order inversion: the hook
    (finalize_hook.py -> handle_minigraf_transact -> _transact with no
    index_con) takes the graph lock and THEN blocks on SQLite for up to
    fact_index.open_writer's 5 s busy timeout, still holding the graph lock;
    ingestion holds SQLite and needs the graph lock back, gives up after
    _LOCK_RETRY_MAX attempts (~2.6 s), and the run ends `status: error`. The
    hook's index insert then fails "database is locked" and is swallowed by
    _index_write, so the fact is in the graph and missing from the index --
    a #302 divergence. Reproduced 2 of 2 at shipped defaults (100 commits)
    before this existed; see CLAUDE.md, "Stage B now yields its lease".

    The commit sits INSIDE the lease and after the window's last
    _correction_sweep_through_update, so a window still ends only between
    fully-swept commits. _commit_index_writer_safe never raises, so it cannot
    mask an exception that is already propagating.

    Looks up db_lease_async by module global at call time, so tests that
    patch it still see every window's acquire.
    """
    async with db_lease_async() as db:
        try:
            yield db
        finally:
            await loop.run_in_executor(write_executor, _commit_index_writer_safe, index_con)


def _graph_path_current() -> str:
    """The bound graph path, falling back to the environment."""
    return _lease_manager.path or _get_graph_path()


def _reset_db_state() -> None:
    """Force the module's DB state back to its initial condition.

    Replaces the old "clear the `_db` global directly" idiom (now deleted,
    #255) at every test and eval call site. Strictly stronger: it clears the
    lease COUNT too, so a test that leaks a lease cannot poison its successor
    -- with the bare global there was no way to reset the count at all.
    """
    global _get_db_stack, _get_db_handle
    # Release the get_db() shim's own lease BEFORE resetting the manager, or
    # its lease would outlive the reset and the count would never reach zero.
    if _get_db_stack is not None:
        _get_db_handle = None      # drop the proxy before the stack severs it
        _get_db_stack.close()
        _get_db_stack = None
    _lease_manager.reset()


# The shim's lease. get_db() is no longer called by any production path -- the
# nine former call sites take scoped leases -- but ~65 tests and evals call it
# directly, and converting them is churn with no correctness gain.
_get_db_stack: Optional[contextlib.ExitStack] = None
_get_db_handle: Optional["_LeasedDb"] = None


def get_db() -> "_LeasedDb":
    """Return the graph handle, acquiring a long-lived lease if none is held.

    DEPRECATED for production use; no production path calls it. It survives for
    tests and evals, and it is no longer #255's hazard: the handle it returns is
    LEASED, held by this module until _reset_db_state() releases it. A handler
    that takes its own lease therefore nests at count 1->2 rather than opening a
    second handle on the same file.

    The old #122 guarantee -- read the `_db` global exactly once -- is gone
    because the global is gone from this path: try_acquire does its check, open
    and count increment under one lock, so there is no window to race. See
    TestLeaseAcquireCannotBeRacedByReset.
    """
    global _get_db_stack, _get_db_handle
    if _get_db_stack is None:
        _get_db_stack = contextlib.ExitStack()
        _get_db_handle = _get_db_stack.enter_context(db_lease())
    return _get_db_handle


def _db_execute(db: Any, datalog: str) -> str:
    """Execute a datalog command against db, serialized via _db_native_lock.

    Every call site that invokes db.execute() on the shared handle must go
    through this (or _db_checkpoint below) rather than calling db.execute()
    directly — see _db_native_lock's docstring for why (#110).
    """
    with _db_native_lock:
        return db.execute(datalog)


def _db_checkpoint(db: Any) -> None:
    """Checkpoint db, serialized via _db_native_lock. See _db_execute."""
    with _db_native_lock:
        db.checkpoint()


_ingest_checkpoint_policy: Optional["_CheckpointPolicy"] = None

_ingest_trace: Optional["_IngestTrace"] = None

_DEFAULT_CHECKPOINT_DUTY = 0.05


def _trace_work_counters(extracted_files: Sequence[tuple]) -> Dict[str, Any]:
    """Per-commit work size, for the #260 cost trace. Pure; len() only.

    extracted_files is _extract_commit's file_results -- one
    (status, file_path, extracted, precomputed, old_path) per changed file. A
    "D" entry carries extracted=None and precomputed=None, so it counts as a
    touched file and contributes nothing else: there is no module being
    introduced and no entities to consider.

    `idents_considered` is #260's frozen work metric W (see
    docs/superpowers/specs/2026-08-17-per-commit-cost-attribution-design.md):
    one module ident per file that has a precomputed, plus one per extracted
    entity, plus one per RESOLVED import. It is the unit _build_code_triples
    iterates. Unresolved imports are excluded because they take the
    external-dependency fallback rather than an entity path;
    n_imports_total - n_imports_resolved is kept as exploratory signal.

    n_unchanged_idents (#221's body-diff narrowing) is likewise exploratory and
    deliberately NOT subtracted from W: W counts idents CONSIDERED, and an
    unchanged ident is considered before it is narrowed out.

    W is FROZEN. Changing this arithmetic after a trace exists redefines the
    experiment; fork it instead.
    """
    files_by_status: Dict[str, int] = {}
    n_modules = n_functions = n_classes = n_globals = n_fields = 0
    n_imports_total = n_imports_resolved = 0
    n_unchanged_idents = 0

    for status, _file_path, _extracted, precomputed, _old_path in extracted_files:
        files_by_status[status] = files_by_status.get(status, 0) + 1
        if not precomputed:
            continue
        n_modules += 1
        n_functions += len(precomputed["function_entries"])
        n_classes += len(precomputed["class_entries"])
        n_globals += len(precomputed["global_entries"])
        n_fields += len(precomputed["field_entries"])
        for _import_name, _dep_ident, is_resolved in precomputed["resolved_imports"]:
            n_imports_total += 1
            if is_resolved:
                n_imports_resolved += 1
        n_unchanged_idents += len(precomputed.get("unchanged_idents", ()))

    return {
        "files_by_status": files_by_status,
        "n_modules": n_modules,
        "n_functions": n_functions,
        "n_classes": n_classes,
        "n_globals": n_globals,
        "n_fields": n_fields,
        "n_imports_total": n_imports_total,
        "n_imports_resolved": n_imports_resolved,
        "n_unchanged_idents": n_unchanged_idents,
        "idents_considered": (
            n_modules + n_functions + n_classes + n_globals + n_fields
            + n_imports_resolved
        ),
    }


class _IngestTrace:
    """Per-commit cost trace for #260, armed by MINIGRAF_INGEST_TRACE_PATH.

    One JSON object per line, appended as each commit's write half finishes.
    JSONL rather than a single document so a killed run still leaves a
    readable partial trace -- at-scale runs take ~30 minutes and the
    interesting ones are sometimes the ones that die.

    Checkpoint cost is recorded as a DELTA of _CheckpointPolicy's cumulative
    `checkpoints`/`total_seconds` across each commit, so the policy gains no
    state of its own. `policy` may be None (the two terminal finally sites in
    _run_ingestion clear it), which records zero deltas rather than raising:
    an instrument must never be the reason a run dies.

    That same principle covers the write itself: an OSError from the
    underlying file (e.g. disk full mid-run) disables the trace and warns on
    stderr rather than propagating -- emit() is called from inside
    _run_ingestion's per-commit loop, outside its own try/except, so letting
    an I/O failure raise there would abort the whole run over an instrument.

    Writes only, no locks, no awaits, no DB access -- see this module's
    _db_native_lock invariant comment for why the per-commit loop tolerates
    nothing else.
    """

    def __init__(self, path: str, clock: "Callable[[], float]" = time.monotonic) -> None:
        self._fh: Optional[Any] = open(path, "a", encoding="utf-8")
        self._clock = clock
        self._started_at = clock()
        self._ckpt_count = 0
        self._ckpt_seconds = 0.0
        self.records = 0

    def emit(
        self,
        pos: int,
        tag: str,
        commit_hash: str,
        await_s: float,
        apply_s: float,
        extracted_files: Sequence[tuple],
        policy: Optional["_CheckpointPolicy"],
    ) -> None:
        if self._fh is None:
            return
        if policy is None:
            d_count, d_seconds = 0, 0.0
        else:
            d_count = policy.checkpoints - self._ckpt_count
            d_seconds = policy.total_seconds - self._ckpt_seconds
            self._ckpt_count = policy.checkpoints
            self._ckpt_seconds = policy.total_seconds

        record: Dict[str, Any] = {
            "pos": pos,
            "tag": tag,
            "hash": commit_hash,
            "t_since_start": self._clock() - self._started_at,
            "await_s": await_s,
            "apply_s": apply_s,
            "ckpt_d_count": d_count,
            "ckpt_d_seconds": d_seconds,
        }
        record.update(_trace_work_counters(extracted_files))
        try:
            self._fh.write(json.dumps(record) + "\n")
            self._fh.flush()
        except OSError as e:
            print(
                f"[_run_ingestion] per-commit trace write failed ({e}); "
                f"tracing disabled for the rest of this run",
                file=sys.stderr,
            )
            self.close()
            return
        self.records += 1

    def close(self) -> None:
        """Idempotent -- _run_ingestion has two terminal paths that both
        release the trace, and either may run first.

        Also absorbs OSError from the close() call itself: emit()'s own
        write-failure handler calls back into close() to disable the trace,
        and on a broken fd (see emit()'s guard) the flush-on-close this
        performs fails with a SECOND OSError from the same underlying
        problem. Swallowing it here is what keeps that cleanup call from
        defeating emit()'s guard by raising anyway.
        """
        if self._fh is None:
            return
        try:
            self._fh.close()
        except OSError:
            pass
        finally:
            self._fh = None


def _ingest_trace_from_env() -> Optional["_IngestTrace"]:
    """Read MINIGRAF_INGEST_TRACE_PATH and open a trace, or None.

    Degrades to None on any open failure rather than raising, matching
    _checkpoint_duty_from_env and _parse_stream_ratio: an unwritable trace path
    is a typo in an instrument, and must not become the reason a repository
    never ingests (#260).
    """
    raw = os.environ.get("MINIGRAF_INGEST_TRACE_PATH")
    if not raw:
        return None
    try:
        return _IngestTrace(raw)
    except OSError as e:
        print(
            f"[_run_ingestion] cannot open MINIGRAF_INGEST_TRACE_PATH={raw!r} "
            f"({e}); per-commit tracing disabled",
            file=sys.stderr,
        )
        return None


def _checkpoint_duty_from_env() -> float:
    """Read MINIGRAF_INGEST_CHECKPOINT_DUTY, falling back to the default on
    anything unparseable or out of range. A typo must not crash ingestion or
    silently switch checkpointing off (#241)."""
    raw = os.environ.get("MINIGRAF_INGEST_CHECKPOINT_DUTY")
    if raw is None:
        return _DEFAULT_CHECKPOINT_DUTY
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[_run_ingestion] ignoring unparseable "
            f"MINIGRAF_INGEST_CHECKPOINT_DUTY={raw!r}; "
            f"using {_DEFAULT_CHECKPOINT_DUTY}",
            file=sys.stderr,
        )
        return _DEFAULT_CHECKPOINT_DUTY
    if not 0.0 < value <= 1.0:
        print(
            f"[_run_ingestion] MINIGRAF_INGEST_CHECKPOINT_DUTY={value} out of "
            f"(0, 1]; using {_DEFAULT_CHECKPOINT_DUTY}",
            file=sys.stderr,
        )
        return _DEFAULT_CHECKPOINT_DUTY
    return value


class _CheckpointPolicy:
    """Decides when an ingestion write batch is compacted to disk.

    db.checkpoint() is O(graph size) WAL compaction that is FLAT in dirty
    bytes -- measured at ~5.1 ms/MB, a checkpoint after one fact costing
    roughly the same as one after 5,000, converging tightly only once
    per-checkpoint cost dominates the graph size (#241). Running it once per
    commit therefore costs N_commits x avg_graph_size, super-linear in
    history length, and was ~51% of at-scale ingestion wall clock.

    It is NOT a durability boundary. minigraf appends every transact to
    <graph>.wal, and writes survive a hard kill with no checkpoint at all;
    the handle also compacts on clean close. Deferring a checkpoint trades
    REOPEN LATENCY (~45 ms per MB of outstanding WAL, paid once by the next
    process to open the graph), never data integrity.

    The gate holds checkpointing to `duty` of wall clock: after a checkpoint
    costing d seconds the next is suppressed until d * (1/duty - 1) seconds
    have passed, since d / (d + W) <= duty exactly when W >= d * (1/duty - 1).
    Because the wait scales with d, the FRACTION stays fixed as the graph
    grows -- that is what removes the super-linear term rather than dividing
    it by a constant. Same self-scaling shape as #242's ingestion poller,
    which held 8.56% duty on CI against 8.65% locally on 23%-slower hardware.

    Thread confinement: every ingestion checkpoint site runs on
    _run_ingestion's single-worker write_executor, so this object's mutable
    state is confined to one thread and needs no lock of its own beyond the
    _db_native_lock that _db_checkpoint already takes. Do not call it from
    another thread without adding one.
    """

    def __init__(self, duty: float, clock: "Callable[[], float]" = time.monotonic) -> None:
        if not 0.0 < duty <= 1.0:
            raise ValueError(f"checkpoint duty must be in (0, 1], got {duty!r}")
        self._duty = duty
        self._clock = clock
        self._created_at = clock()
        self._last_duration: Optional[float] = None
        self._last_finished_at = 0.0
        self.checkpoints = 0
        self.suppressed = 0
        self.total_seconds = 0.0

    def _budget_elapsed(self) -> bool:
        if self._last_duration is None:
            return True  # nothing measured yet; checkpoint once to seed d
        wait = self._last_duration * (1.0 / self._duty - 1.0)
        return self._clock() - self._last_finished_at >= wait

    def maybe(self, db: Any) -> bool:
        """Checkpoint if the budget allows. Returns whether it did."""
        if not self._budget_elapsed():
            self.suppressed += 1
            return False
        self.force(db)
        return True

    def force(self, db: Any) -> None:
        """Checkpoint regardless of budget, and re-measure d."""
        started = self._clock()
        _db_checkpoint(db)
        finished = self._clock()
        self._last_duration = finished - started
        self._last_finished_at = finished
        self.checkpoints += 1
        self.total_seconds += self._last_duration

    def summary(self) -> Dict[str, Any]:
        """A small, JSON-safe snapshot of realised checkpoint duty (#241 Task
        6), taken by the caller just before it discards this policy.

        _run_ingestion clears _ingest_checkpoint_policy to None at its two
        terminal-path finally sites (see their own comments for why there
        are two) so a later interactive transact is never gated by a stale
        run's budget -- which means this object and its counters are gone
        the instant that happens. _ingest_progress is the channel that
        already survives past a finished run, so the caller publishes this
        dict there before clearing the policy, not after.

        `elapsed_seconds` is measured from this policy's construction, which
        is the FIRST statement in _run_ingestion's try, to whenever this is
        called, which is a few statements before the run returns -- close
        enough to the run's own wall clock to report a meaningful duty
        fraction without threading a second timer through the caller.

        That construction used to sit ~100 lines lower, below write_executor,
        which put the two git enumerations and the whole preload OUTSIDE this
        window; `realised_duty` was correspondingly overstated in every
        benchmark.md row report.py wrote from it. Moved for #270 (see
        _run_ingestion's own comment there). Duty numbers recorded before
        that move are measured over the narrower window.
        """
        elapsed = max(self._clock() - self._created_at, 0.0)
        return {
            "checkpoints": self.checkpoints,
            "suppressed": self.suppressed,
            "total_seconds": self.total_seconds,
            "elapsed_seconds": elapsed,
            "realised_duty": (self.total_seconds / elapsed) if elapsed > 0 else 0.0,
        }


def _db_checkpoint_gated(db: Any) -> bool:
    """Checkpoint unless the active ingestion policy says the budget is spent.

    With no ingestion in flight the policy is None and this is exactly
    _db_checkpoint(db), so the interactive write path is unchanged (#241).
    Returns whether a checkpoint actually ran.
    """
    policy = _ingest_checkpoint_policy
    if policy is None:
        _db_checkpoint(db)
        return True
    return policy.maybe(db)


def _checkpoint_after_write(db: Any, tool_name: str, result: Dict[str, Any]) -> None:
    """Checkpoint after a transact/retract that has already applied its graph
    and fact-index write. A checkpoint failure here must not flip an
    already-successful write's result to ok:False (#176) -- the caller would
    reasonably retry, and a retry uses a fresh valid_from, which creates a
    genuine duplicate live datom rather than a no-op (per #156's finding that
    minigraf only treats an identical (entity, attribute, value, valid_from)
    tuple as idempotent). Mutates result in place, adding a "warning" key
    when the checkpoint fails and the write itself succeeded.
    """
    try:
        _db_checkpoint(db)
    except MiniGrafError as e:
        print(f"[{tool_name}] checkpoint failed after successful write: {e}", file=sys.stderr)
        if result.get("ok"):
            result["warning"] = f"checkpoint failed after write succeeded: {e}"


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def _parse_query_result(raw_json: str) -> Dict[str, Any]:
    """Parse JSON returned by MiniGrafDb.execute() for a query command."""
    try:
        data = json.loads(raw_json)
        return {"ok": True, "results": data.get("results", [])}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Unexpected result format: {e} — raw: {raw_json[:200]}"}


def _parse_tx_result(raw_json: str) -> Dict[str, Any]:
    """Parse JSON returned by MiniGrafDb.execute() for a transact/retract command."""
    try:
        data = json.loads(raw_json)
        tx = data.get("transacted", data.get("retracted", data.get("tx", "unknown")))
        return {"ok": True, "tx": str(tx)}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Unexpected result format: {e} — raw: {raw_json[:200]}"}


# ---------------------------------------------------------------------------
# Explicit agent tool handlers
# ---------------------------------------------------------------------------

def handle_minigraf_query(datalog: str) -> Dict[str, Any]:
    """Query the graph. Returns {ok, results} or {ok, error}."""
    with db_lease() as db:
        try:
            raw = _db_execute(db, f"(query {datalog})")
            return _parse_query_result(raw)
        except MiniGrafError as e:
            return {"ok": False, "error": str(e)}


_TAGGED_LITERAL = r'#(?:uuid|inst)\s+"[^"\\]*"'
# The bare-boolean alternative is #303. It comes LAST in the value group so
# the keyword alternative still claims `:true`, and it is safe unanchored only
# because the group is followed by `\]`: `[:f :static truthy]` tries `true`,
# is left holding `thy]`, and fails every alternative rather than indexing a
# truncated value into the graph's only independent witness (#302).
_FACTS_TRIPLE_PATTERN = re.compile(
    r'\[(\:[^\s\]]+|' + _TAGGED_LITERAL + r')\s+(\:[^\s\]]+)\s+'
    r'("(?:[^"\\]|\\.)*"|\:[^\s\]]+|-?\d+(?:\.\d+)?|' + _TAGGED_LITERAL
    + r'|true|false)\]'
)
_TAGGED_LITERAL_PATTERN = re.compile(r'#(?:uuid|inst)\s+"([^"\\]*)"')

# `nil` is NOT in the value group above, and #306 is the decision not to put it
# there. Indexing it would mean a row whose value is the text 'nil' -- "no
# value" occupying a slot in a lexical retrieval index and answering a search
# for "nil" -- and it would need fact_audit._index_text to grow a `None` case
# in the same commit, since minigraf returns None and str(None) is 'None'. A
# pattern-only fix trades one divergence for another. So a nil-valued triple is
# refused at the transact boundary instead: it is not a storable value, and the
# at-scale audit gate (#302, zero tolerance) stays honest -- a nil fact found in
# a graph is a genuine write-path defect, not the index being blamed for what
# it cannot hold.
_EDN_STRING_PATTERN = re.compile(r'"(?:[^"\\]|\\.)*"')
_NIL_VALUE_PATTERN = re.compile(
    r'\[(?:\:[^\s\]]+|' + _TAGGED_LITERAL + r')\s+\:[^\s\]]+\s+nil\]'
)


def _has_nil_valued_triple(facts_str: str) -> bool:
    r"""True if facts_str contains a triple whose VALUE is a bare `nil`.

    Quoted strings are blanked before the scan, and that is load-bearing
    rather than tidy: a note written ABOUT this defect carries the offending
    triple as prose, so a raw text scan would refuse a legitimate write.
    `_FACTS_TRIPLE_PATTERN` shares the blind spot but pays only a spurious
    index row for it -- here the cost is a rejected write, so this scan cannot
    inherit the shortcut. Blanking leaves `#uuid ""`, which `_TAGGED_LITERAL`
    still matches, so a #uuid-tagged entity stays detectable.

    Entity and attribute token shapes are `_FACTS_TRIPLE_PATTERN`'s own so the
    two cannot drift on what counts as a triple, and the trailing `\]` is what
    makes the unanchored `nil` safe: `[:d/x :note nilpotent]` is left holding
    `potent]` and does not match, exactly as `truthy` does not match `true`
    (#303).
    """
    return _NIL_VALUE_PATTERN.search(_EDN_STRING_PATTERN.sub('""', facts_str)) is not None


def _unwrap_facts_block_token(raw: str) -> str:
    """Strip quoting/tagging from a captured entity or value token: a quoted
    string loses its quotes and has its EDN escaping reversed (\\" -> ",
    \\\\ -> \\), a #uuid/#inst "..." literal loses the tag and keeps the raw
    UUID/timestamp text, anything else (keyword, number) is kept as-is."""
    if raw.startswith('"'):
        return _edn_unescape(raw[1:-1])
    m = _TAGGED_LITERAL_PATTERN.match(raw)
    if m:
        return m.group(1)
    return raw


def _parse_facts_block(facts_str: str) -> List[Tuple[str, str, str]]:
    """Parse every [entity attribute value] triple out of a Datalog facts
    block or a single triple string -- scans for all matches rather than
    requiring a strict split, so it works on both shapes uniformly (mirrors
    _parse_transact_facts' existing regex-scan approach, extended to also
    capture keyword-valued, bare-numeric-valued and bare-boolean-valued
    triples, which schema validation intentionally skips but the index must
    not). Value is unquoted for string-valued triples, kept as-is (a keyword,
    number, `true`/`false`, or entity reference) otherwise -- so a boolean is
    indexed in its EDN spelling, lowercase, the same datalog text it was
    transacted from, even though minigraf reads it back as a Python bool
    (#303). #uuid/#inst-tagged entity references and values are also
    captured, with the tag stripped and the raw UUID/timestamp text kept as
    the indexed entity/value (#177) -- this is not
    a keyword ident, so a caller wanting identity-resolved output should use
    _resolved_facts_triples() instead, which wraps this function and
    resolves #uuid-tagged entities to their stored :ident when one exists
    (#194).
    """
    triples = []
    for m in _FACTS_TRIPLE_PATTERN.finditer(facts_str):
        entity, attribute, raw_value = m.groups()
        triples.append((
            _unwrap_facts_block_token(entity),
            attribute,
            _unwrap_facts_block_token(raw_value),
        ))
    return triples


def _query_ident(db: Any, entity_ref: str) -> Optional[str]:
    """Look up the :ident fact for entity_ref -- a bare keyword literal
    (e.g. ':decision/x') or a #uuid "..."-tagged literal -- returning the
    stored keyword ident string, or None if no :ident fact exists or the
    query fails. Never raises: a caller resolving an entity for fact-index
    purposes must fall back to the raw entity_ref on any failure, not break
    a write that has already committed by the time this runs (#194).
    """
    try:
        raw = _db_execute(db, f'(query [:find ?v :where [{entity_ref} :ident ?v]])')
        result = _parse_query_result(raw)
        if result.get("ok"):
            for row in result.get("results", []):
                if row and isinstance(row[0], str):
                    return row[0]
    except Exception as e:
        print(f"[fact_index] ident lookup failed for {entity_ref}: {e}", file=sys.stderr)
    return None


def _resolved_facts_triples(facts_str: str, db: Any) -> List[Tuple[str, str, str]]:
    """Parse facts_str via _parse_facts_block, then resolve any #uuid/#inst
    -tagged entity (identified post-unwrap by not starting with ':') to its
    stored keyword :ident via _query_ident, falling back to the raw
    UUID/timestamp text when no :ident fact exists (#194) -- without this,
    a fact transacted against a #uuid-tagged reference to an existing
    memory-category entity indexes under the opaque UUID and never gets
    fact_index._MEMORY_PREFIXES' BM25 boost, even though it's a fact about
    that same entity. Resolutions are cached per call so a UUID referenced
    by multiple triples in one transact/retract only queries once.

    This is the default deriver _transact/_retract use when the caller
    doesn't pass index_triples explicitly. A caller that already has a
    resolved ident more cheaply available (see handle_minigraf_audit)
    should keep passing index_triples to skip these queries entirely.
    """
    triples = _parse_facts_block(facts_str)
    cache: Dict[str, Optional[str]] = {}
    resolved = []
    for entity, attribute, value in triples:
        if not entity.startswith(":"):
            if entity not in cache:
                cache[entity] = _query_ident(db, f'#uuid "{entity}"')
            entity = cache[entity] or entity
        resolved.append((entity, attribute, value))
    return resolved


def _index_write(
    action: str,
    triples: List[Tuple[str, str, str, Optional[str], Optional[str]]],
    index_con: Optional[Any] = None,
) -> None:
    """Apply an insert or delete to the fact index, never raising -- index
    maintenance must never block a graph write. action is 'insert' or 'delete'. When
    index_con is provided, writes onto it without committing (caller controls
    the transaction boundary — used by ingestion's batching). Otherwise opens
    a connection, writes, commits, and closes immediately.
    """
    if not triples:
        return
    try:
        if index_con is not None:
            (fact_index.insert_facts if action == "insert" else fact_index.delete_facts)(
                index_con, triples
            )
            return
        path = fact_index.index_path_for(_graph_path_current())
        con = fact_index.open_writer(path)
        try:
            (fact_index.insert_facts if action == "insert" else fact_index.delete_facts)(
                con, triples
            )
            con.commit()
        finally:
            con.close()
    except Exception as e:
        print(f"[fact_index] {action} failed: {e}", file=sys.stderr)


def _open_index_writer_safe(path: str) -> Optional[Any]:
    """Open the batched fact-index writer connection used by _run_ingestion,
    never raising (#150). Retries lock contention with the same blocking
    backoff constants db_lease() uses (_LOCK_RETRY_MAX/_LOCK_RETRY_BASE) --
    the eager startup backfill (#147) can hold fact_index.rebuild_index()'s
    write transaction open for a whole historical rescan, and giving up on
    the first "database is locked" would otherwise silently downgrade this
    entire ingestion run's fact-index writes to the slow per-triple path for
    no reason beyond a transient startup race. Only safe off the asyncio
    event-loop thread (blocking time.sleep) -- always invoked via
    write_executor, never inline on the loop.

    Any other failure (disk full, corrupted file, permissions) degrades
    immediately to per-triple index writes instead of aborting the whole
    ingestion run: downstream call sites already accept index_con=None and
    fall back to _index_write's own open+commit+close path, which is
    independently fault-isolated per call.
    """
    delay = _LOCK_RETRY_BASE
    for attempt in range(_LOCK_RETRY_MAX):
        try:
            return fact_index.open_writer(path)
        except Exception as e:
            if not _is_lock_error(e) or attempt == _LOCK_RETRY_MAX - 1:
                print(f"[fact_index] open_writer failed: {e}", file=sys.stderr)
                return None
            time.sleep(delay)
            delay *= 2
    return None  # unreachable -- loop above always returns


def _commit_index_writer_safe(index_con: Optional[Any]) -> None:
    """Commit the batched fact-index connection, never raising (#150)."""
    if index_con is None:
        return
    try:
        index_con.commit()
    except Exception as e:
        print(f"[fact_index] commit failed: {e}", file=sys.stderr)


def _close_index_writer_safe(index_con: Optional[Any]) -> None:
    """Close the batched fact-index connection, never raising (#150) -- a
    failure here must not mask an otherwise-successful ingestion run as an
    error."""
    if index_con is None:
        return
    try:
        fact_index.close_writer(index_con)
    except Exception as e:
        print(f"[fact_index] close_writer failed: {e}", file=sys.stderr)


def _transact(
    db: Any,
    datalog_facts: str,
    valid_from: str,
    valid_to: Optional[str] = None,
    index_triples: Optional[List[Tuple[str, str, str]]] = None,
    index_con: Optional[Any] = None,
) -> str:
    """Execute (transact {opts} datalog_facts) against minigraf, then write
    index_triples into the fact index -- ALWAYS, not just when valid_to is
    None. A current (valid_to=None) transact is indexed as a live row; a
    bounded transact is indexed as a historical row carrying its window,
    which is the actual entry point into retracted/superseded graph regions
    (see the design doc). This is the one behavior change from the base
    branch's _transact: previously bounded transacts were skipped entirely.

    index_triples defaults to auto-deriving via _resolved_facts_triples()
    (which returns 3-tuples (entity, attribute, value), resolving any
    #uuid-tagged entity to its stored :ident when one exists, #194 -- the
    window is appended here, not inside that function, since it has no way
    to know valid_from/valid_to); pass index_triples explicitly when a
    caller already has a resolved keyword ident more cheaply available than
    a fresh query would provide (e.g. handle_minigraf_audit, which already
    fetched the entity's attributes including :ident) -- in that case pass
    3-tuples too, the window is still appended here uniformly.
    """
    opts = f':valid-from "{valid_from}"'
    if valid_to is not None:
        opts += f' :valid-to "{valid_to}"'
    raw = _db_execute(db, f"(transact {{{opts}}} {datalog_facts})")
    triples_3 = index_triples if index_triples is not None else _resolved_facts_triples(datalog_facts, db)
    triples_5 = [(e, a, v, valid_from, valid_to) for e, a, v in triples_3]
    _index_write("insert", triples_5, index_con=index_con)
    return raw


def _retract(
    db: Any,
    datalog_facts: str,
    index_triples: Optional[List[Tuple[str, str, str]]] = None,
    index_con: Optional[Any] = None,
) -> str:
    """Execute (retract datalog_facts) against minigraf, then delete the
    matching CURRENT row from the fact index (same decoupling as _transact
    -- index_triples overrides auto-derivation (_resolved_facts_triples, which
    resolves #uuid-tagged entities to their :ident when available, #194) when a
    caller already has a resolved ident more cheaply available). delete_facts only ever
    targets valid_to IS NULL rows, so historical rows from an earlier
    lifecycle of the same (entity, attribute, value) are untouched -- pass
    None, None for the window here unconditionally, since a retract only
    ever means "remove the live assertion."
    """
    raw = _db_execute(db, f"(retract {datalog_facts})")
    triples_3 = index_triples if index_triples is not None else _resolved_facts_triples(datalog_facts, db)
    triples_5 = [(e, a, v, None, None) for e, a, v in triples_3]
    _index_write("delete", triples_5, index_con=index_con)
    return raw


def _ensure_memory_idents(db: Any, facts_str: str, valid_from: str) -> None:
    """After a successful transact, write a self-referencing :ident fact for
    any keyword entity in facts_str whose ident string starts with a
    fact_index._MEMORY_PREFIXES category (:decision/, :preference/,
    :constraint/, :dependency/) and doesn't already have one (#194) --
    without this, an ordinary minigraf_transact-created decision/
    preference/constraint/dependency entity has no way to resolve a later
    #uuid-tagged reference back to its keyword form for the memory-fact
    BM25 boost (see _resolved_facts_triples).

    Query-gated, not unconditional: re-transacting an identical fact at a
    different valid_from creates a new bi-temporal history row every time
    (confirmed empirically; consistent with #156's finding documented in
    _checkpoint_after_write) -- writing :ident on every call would bloat
    history. Never raises: the caller's actual write has already committed
    by the time this runs, and a failure here must not affect that result.
    """
    triples = _parse_facts_block(facts_str)
    already_idented = {e for e, a, v in triples if a == ":ident"}
    candidates = {
        e for e, a, v in triples
        if e.startswith(":") and e.startswith(fact_index._MEMORY_PREFIXES)
    } - already_idented
    for entity in sorted(candidates):
        if _query_ident(db, entity) is not None:
            continue
        try:
            _transact(db, f'[[{entity} :ident "{_edn_escape(entity)}"]]', valid_from)
        except Exception as e:
            print(f"[fact_index] auto-ident write failed for {entity}: {e}", file=sys.stderr)


def handle_minigraf_transact(facts: str, reason: str) -> Dict[str, Any]:
    """Transact facts into the graph. reason is required.

    :valid-at is set to the current UTC ms timestamp so every agent-initiated
    write has a recorded valid time, enabling correct bi-temporal queries.
    On success, also ensures any memory-category entity (fact_index.
    _MEMORY_PREFIXES) created by this call has a resolvable :ident fact --
    see _ensure_memory_idents (#194).
    """
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for all writes"}
    # #306: refuse the whole block, before the graph is touched. minigraf
    # accepts a nil-valued triple, so nothing downstream would stop it, and
    # the fact index cannot hold one -- see _has_nil_valued_triple.
    if _has_nil_valued_triple(facts):
        return {
            "ok": False,
            "error": "nil is not a storable value: a nil-valued triple reaches "
                     "the graph but never the fact index (#306). Omit the "
                     "attribute, or give it an explicit value.",
        }
    # Schema validation — closed-world enforcement on parseable string-valued triples.
    # Only string-valued triples are schema-validated. Keyword-valued triples
    # (e.g. relationship edges like [:service/auth :calls :component/jwt]) are
    # not covered by MINIGRAF_SCHEMA and pass through unvalidated by design.
    parsed = _parse_transact_facts(facts)
    if parsed:
        violations = _validate_facts(parsed)
        if violations:
            return {"ok": False, "error": f"schema violations: {'; '.join(violations)}"}
    with db_lease() as db:
        valid_from = _now_utc_ms()
        try:
            raw = _transact(db, facts, valid_from)
        except MiniGrafError as e:
            return {"ok": False, "error": str(e)}
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
            _ensure_memory_idents(db, facts, valid_from)
        _checkpoint_after_write(db, "minigraf_transact", result)
        return result


def handle_minigraf_retract(facts: str, reason: str) -> Dict[str, Any]:
    """Retract facts from the graph. reason is required."""
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for retract"}
    with db_lease() as db:
        try:
            raw = _retract(db, facts)
        except MiniGrafError as e:
            return {"ok": False, "error": str(e)}
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
        _checkpoint_after_write(db, "minigraf_retract", result)
        return result


def handle_minigraf_rule(rule: str) -> Dict[str, Any]:
    """Register a Datalog rule for use in subsequent queries.

    Rules persist for the lifetime of the server session and are re-registered
    whenever the DB is reopened. To make a rule permanent across server restarts,
    add it to SESSION_RULES in mcp_server.py.

    Syntax: [(rule-name ?arg ...) body-clause ...]
    Example: [(ancestor ?a ?d) [?a :parent ?d]]
    """
    global _user_rules
    with db_lease() as db:
        try:
            _db_execute(db, f"(rule {rule})")
            rule_expr = f"(rule {rule})"
            if rule_expr not in _user_rules:
                _user_rules.append(rule_expr)
            return {"ok": True, "rule": rule}
        except MiniGrafError as e:
            return {"ok": False, "error": str(e)}


def handle_minigraf_report_issue(
    category: str,
    description: str,
    datalog: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Delegate to report_issue.py."""
    try:
        from report_issue import report_issue
        return report_issue(category, description, datalog=datalog, error=error)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def handle_minigraf_audit(as_of: Optional[int] = None) -> Dict[str, Any]:
    """Audit graph entities against MINIGRAF_SCHEMA.

    Current state (as_of=None): validates all entities and retracts violators.
    Point-in-time (as_of=N): reports violations only — no retractions.

    Ported from Schema.audit_as_of() in minigraf-examples minigraf-schema crate.
    """
    audited = 0
    retracted = 0
    all_violations: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    as_of_clause = f":as-of {as_of} " if as_of is not None else ""

    with db_lease() as db:
        for entity_type in MINIGRAF_SCHEMA:
            # Step 1: Find all entity UUIDs of this type.
            type_query = (
                f"[:find ?e {as_of_clause}"
                f":where [?e :entity-type :type/{entity_type}]]"
            )
            try:
                type_result = handle_minigraf_query(type_query)
                type_rows = type_result.get("results", [])
            except Exception as e:
                print(
                    f"[minigraf_audit] type query failed for {entity_type}: {e}",
                    file=sys.stderr,
                )
                skipped.append({"entity_type": entity_type, "stage": "type_query", "error": str(e)})
                continue

            for row in type_rows:
                if not row:
                    continue
                entity_uuid = row[0]
                audited += 1

                # Step 2: Fetch all attributes using #uuid tagged literal.
                # minigraf's EDN parser treats #uuid "..." as EdnValue::Uuid and routes
                # it through edn_to_entity_id directly — no keyword-to-UUID derivation
                # needed and no join-variable round-trip problem.
                attr_query = (
                    f'[:find ?a ?v {as_of_clause}'
                    f':where [#uuid "{entity_uuid}" ?a ?v]]'
                )
                try:
                    attr_result = handle_minigraf_query(attr_query)
                    attr_rows = attr_result.get("results", [])
                except Exception as e:
                    print(
                        f"[minigraf_audit] attr query failed for {entity_uuid} "
                        f"({entity_type}): {e}",
                        file=sys.stderr,
                    )
                    skipped.append({
                        "entity": entity_uuid,
                        "entity_type": entity_type,
                        "stage": "attr_query",
                        "error": str(e),
                    })
                    continue

                # Extract keyword ident from the stored :ident datom for reporting.
                # Falls back to the UUID string if :ident was not written.
                kw_ident = next(
                    (v for a, v in attr_rows if a == ":ident" and isinstance(v, str)),
                    entity_uuid,
                )

                # Exclude system attributes from schema validation.
                attr_facts = [
                    {
                        "entity": kw_ident,
                        "entity_type": entity_type,
                        "attribute": a,
                        "value": v,
                    }
                    for a, v in attr_rows
                    if a not in _SYSTEM_ATTRS
                ]

                if not attr_facts:
                    attr_facts = [{"entity": kw_ident, "entity_type": entity_type,
                                   "attribute": ":__no_attributes__", "value": ""}]

                violations = _validate_facts(attr_facts)
                if violations:
                    for v in violations:
                        all_violations.append({"entity": kw_ident, "detail": v})

                    if as_of is None:
                        # Retract using #uuid tagged literal — works even without knowing
                        # the original keyword ident. History preserved (bi-temporal).
                        try:
                            retract_triples = [
                                f'[#uuid "{entity_uuid}" :entity-type :type/{entity_type}]',
                            ]
                            for a, v in attr_rows:
                                if isinstance(v, str):
                                    escaped = v.replace('"', '\\"')
                                    retract_triples.append(
                                        f'[#uuid "{entity_uuid}" {a} "{escaped}"]'
                                    )
                            retract_facts = "[" + " ".join(retract_triples) + "]"
                            index_triples = [
                                (kw_ident, ":entity-type", f":type/{entity_type}"),
                            ] + [
                                (kw_ident, a, v) for a, v in attr_rows if isinstance(v, str)
                            ]
                            _retract(db, retract_facts, index_triples=index_triples)
                        except Exception as e:
                            print(f"[minigraf_audit] retract failed for {kw_ident}: {e}", file=sys.stderr)
                        else:
                            # The retract (graph + fact index) already applied above --
                            # count it regardless of whether the checkpoint that follows
                            # succeeds (#176), and never let a checkpoint failure raise
                            # out of this loop and abort the rest of the audit.
                            retracted += 1
                            try:
                                _db_checkpoint(db)
                            except Exception as e:
                                print(
                                    f"[minigraf_audit] checkpoint failed after retracting "
                                    f"{kw_ident}: {e}",
                                    file=sys.stderr,
                                )

    return {
        "ok": True,
        "audited": audited,
        "retracted": retracted,
        "violations": all_violations,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# memory_prepare_turn
# ---------------------------------------------------------------------------

# Moved to fact_index (#354) so the retrieval query can share it.
_STOP_WORDS = fact_index._STOP_WORDS

_MIN_ENTITY_LEN = 4


def _canonical_ident(entity_type: str, value: str) -> str:
    """Slug-canonicalize a value into a Minigraf keyword ident.

    Lowercases, replaces any character outside [a-z0-9_-] with a hyphen,
    strips leading/trailing hyphens. Originally ported from _to_kw() in
    minigraf-examples' LlamaIndex integration.

    #263 — TWO deliberate departures from that original rule, both load-bearing
    for identity and neither safe to "simplify" away:

    1. '_' is INSIDE the allowed charset, so a private marker survives. Mapping
       it to '-' made `foo` and `_foo` (and `__init__` and `_init`) the same
       entity.
    2. Consecutive hyphens are NOT collapsed, so separator arity carries
       information: `_code_ident`'s '::' join survives as '--' and stays
       distinct from a single '-' that came from one character in the path.

    Together these are rule R3 of the #263 audit, measured at ZERO residual
    collisions over 674 commits of this repo (9 of 2780 idents collided under
    the old rule). That zero is MEASURED, NOT PROVEN BY CONSTRUCTION — a
    contrived path/name combination can still collide. R4 (a hash suffix) would
    have been collision-free by construction and was rejected because idents are
    the human- and agent-legible handle in query results. Three guards, and they
    answer different questions — do not treat any one as covering the others:

      * TestIdentCollisionRegression263 (in the suite) — the 9 measured pairs
        still separate. A fixed corpus: catches a REGRESSION in this rule,
        discovers nothing new.
      * evals/at_scale/probe_ident_collision_new_history.py (#267) — censuses
        FULL history against this function, live, and is the only thing that can
        discover a NEW collision. Runs in the at-scale nightly with
        --fail-on-collision; ~97s over 835 commits.
      * evals/at_scale/probe_ident_collision_census.py — FROZEN at the pre-#263
        rule. It reproduces the audit that chose R3 and deliberately does NOT
        track this function; it is not a guard on the shipped rule.

    Changing this rule changes every ident the graph will ever mint, and
    ingestion recomputes idents from scratch on every run rather than reading
    them back — so an old-rule graph read by new-rule code forks every entity
    silently. That is what :ingestion/format-version and its refusal exist to
    catch; bump GRAPH_FORMAT_VERSION with any change here.
    """
    slug = re.sub(r"[^a-z0-9_-]", "-", value.lower()).strip("-")
    return f":{entity_type}/{slug}"


def _path_segments(path_str: str) -> List[str]:
    """Split a path into non-empty segments, normalizing os.sep to '/'."""
    return [seg for seg in path_str.replace(os.sep, "/").split("/") if seg]


def _segments_end_with(full_segments: List[str], candidate_segments: List[str]) -> bool:
    """True if full_segments' trailing slice equals candidate_segments exactly.

    A whole-segment suffix comparison, not a raw string suffix — comparing
    strings directly would let e.g. "xyzcom/google" wrongly match a
    candidate of "com/google" (the substring is present but not as its own
    path segment).
    """
    if not candidate_segments or len(candidate_segments) > len(full_segments):
        return False
    return full_segments[-len(candidate_segments):] == candidate_segments


def _dep_import_segments(import_name: str) -> List[str]:
    """Split a dependency-edge import specifier into path segments.

    Mirrors _resolve_module_import's tier-3 splitting rule exactly (slash if
    present, else dot) so a submodule path prefix match (see
    _submodule_path_matches_import, #112) uses the same segmentation the
    resolver itself would have used.
    """
    return import_name.split("/") if "/" in import_name else import_name.split(".")


def _submodule_path_matches_import(submodule_path: str, import_name: str) -> bool:
    """True if a dependency-edge import specifier falls under a submodule's path.

    Used to link an unresolved-import stub ident (computed via
    _canonical_ident from the raw import specifier) to a submodule entity
    (computed via _code_ident from its .gitmodules/gitlink path) — the two
    are never the same ident string, so #112's fix connects them with an
    explicit :resolves-to edge whenever the submodule's path is a whole-segment
    prefix of the import's own segments.
    """
    submodule_segments = _path_segments(submodule_path)
    import_segments = _dep_import_segments(import_name)
    if not submodule_segments or len(submodule_segments) > len(import_segments):
        return False
    return import_segments[:len(submodule_segments)] == submodule_segments


class _SegmentSuffixIndex:
    """Reverse index over file_entities' path segments, bucketed by last segment.

    _resolve_module_import's tiers 3a/3b used to linear-scan every entry in
    file_entities and recompute its Path/segment work on every single call,
    even though only files sharing the candidate's last segment (the most
    discriminating part of a path or module specifier) can ever match. This
    buckets each file once by that last segment so a lookup only suffix-checks
    the handful of files that could plausibly match, independent of how many
    files exist overall. Built once per known_files snapshot (see
    _extract_commit) and reused across every import resolved against it.
    """

    __slots__ = ("_file_buckets", "_parent_buckets")

    def __init__(self, file_entities: Dict[str, List[str]]):
        self._file_buckets: Dict[str, List[Tuple[str, List[str]]]] = {}
        self._parent_buckets: Dict[str, List[Tuple[str, List[str]]]] = {}
        for file_path in file_entities:
            file_segments = _path_segments(str(Path(file_path).with_suffix("")))
            if file_segments:
                self._file_buckets.setdefault(file_segments[-1], []).append((file_path, file_segments))
            parent_segments = _path_segments(str(Path(file_path).parent))
            if parent_segments:
                self._parent_buckets.setdefault(parent_segments[-1], []).append((file_path, parent_segments))

    def match_file(self, candidate_segments: List[str]) -> Optional[str]:
        for file_path, file_segments in self._file_buckets.get(candidate_segments[-1], []):
            if _segments_end_with(file_segments, candidate_segments):
                return file_path
        return None

    def match_parent(self, candidate_segments: List[str]) -> Optional[str]:
        for file_path, parent_segments in self._parent_buckets.get(candidate_segments[-1], []):
            if _segments_end_with(parent_segments, candidate_segments):
                return file_path
        return None


def _resolve_module_import(
    import_name: str,
    file_entities: Dict[str, List[str]],
    importing_file: Optional[str] = None,
    segment_index: Optional[_SegmentSuffixIndex] = None,
) -> Tuple[str, bool]:
    """Resolve an import name to a module ident that joins with stored module entities.

    Tries a relative-import resolution first (see below), then Rust's exact
    source-root conventions (src/storage.rs, src/storage/mod.rs), then a
    generic, language-agnostic segment-suffix matcher used by every other
    language. This exists because every other language's import extraction
    already reduces to a bare or dotted/slashed specifier (see
    _extract_import_name) that would otherwise always fall through to the
    external-dependency fallback — including for real in-tree vendored code,
    which must stay internal per the design spec's Non-goals.

    When import_name is relative (starts with ".") and importing_file is
    given, resolves against the importing file's own directory before any
    other tier runs. Covers three conventions: JS/TS/Ruby-style "./foo" and
    "../foo/bar" (plain relative filesystem paths — Ruby's require_relative
    results already carry this "./" prefix, added by _ruby_require_name),
    and Python-style leading dots with no slash ("." = same package, each
    extra dot = one directory further up) followed by an optional dotted
    module path.

    The generic matcher splits the specifier into segments on "/" if present
    (Go, C/C++, Ruby, PHP already use "/" natively — note Go paths like
    "github.com/user/pkg" contain literal dots inside a segment that must
    NOT be treated as separators), otherwise on "." (Java, C#, Python, Scala,
    Kotlin, Swift, Haskell, Elixir — genuinely dot-separated). Matching a
    whole-segment suffix (not a raw substring) against either a file's own
    path or its parent directory uniformly covers: an exact match, a
    vendored path with extra prefix segments, a package-only/wildcard-style
    import (via the parent-directory tier), and a bare single-segment name
    (degenerates to a basename check).

    Returns (ident, is_resolved). is_resolved is True when import_name
    matched a real file in file_entities, False when it fell through to the
    bare _canonical_ident guess.

    segment_index, if given, must be a _SegmentSuffixIndex built from this
    same file_entities — used to speed up tiers 3a/3b. When omitted, one is
    built on the fly from file_entities (same cost as the old linear scan);
    callers resolving many imports against the same file_entities should
    build it once and pass it in.
    """
    if importing_file and import_name.startswith("."):
        base_dir = Path(importing_file).parent
        if import_name.startswith("./") or import_name.startswith("../"):
            target = os.path.normpath(str(base_dir / import_name))
        else:
            stripped = import_name.lstrip(".")
            levels_up = len(import_name) - len(stripped) - 1
            target_dir = base_dir
            for _ in range(levels_up):
                target_dir = target_dir.parent
            target = str(target_dir / stripped.replace(".", "/")) if stripped else str(target_dir)
            target = os.path.normpath(target)
        target = target.replace(os.sep, "/")
        for file_path in file_entities:
            if str(Path(file_path).with_suffix("")).replace(os.sep, "/") == target:
                return _code_ident("module", file_path), True
        return _canonical_ident("module", import_name), False

    # Priority 1: canonical Rust module root paths under common source roots
    for src_root in ("src", "lib", ""):
        prefix = f"{src_root}/" if src_root else ""
        candidate_file = f"{prefix}{import_name}.rs"
        candidate_mod = f"{prefix}{import_name}/mod.rs"
        if candidate_file in file_entities:
            return _code_ident("module", candidate_file), True
        if candidate_mod in file_entities:
            return _code_ident("module", candidate_mod), True

    # Priority 2: broader search — only match files directly under a src root
    # (parent.parent is the source root, not a nested subdir)
    for file_path in file_entities:
        p = Path(file_path)
        if p.stem == "mod" and p.parent.name == import_name:
            return _code_ident("module", file_path), True

    # Priority 3: generic segment-suffix matcher for every other language.
    candidate_segments = import_name.split("/") if "/" in import_name else import_name.split(".")
    index = segment_index if segment_index is not None else _SegmentSuffixIndex(file_entities)

    # 3a. file match (exact, or a vendored path with extra prefix segments), extension stripped
    file_match = index.match_file(candidate_segments)
    if file_match is not None:
        return _code_ident("module", file_match), True

    # 3b. parent-directory match (package-only/wildcard-style imports, e.g.
    # a Java "import com.google.gson.*;" or a bare "com.google.gson" reference
    # with no specific trailing class name)
    parent_match = index.match_parent(candidate_segments)
    if parent_match is not None:
        return _code_ident("module", parent_match), True

    return _canonical_ident("module", import_name), False


def _code_ident(entity_type: str, file_path: str, name: Optional[str] = None) -> str:
    """Return a canonical ident for a code entity.

    Appends '::name' to file_path before slugging so that the function
    name appears AFTER the file extension in the slug, keeping it distinct
    from a file whose path ends with the name (e.g. 'src/auth_login.py').

    This is best-effort — the separator itself becomes '-' after slugging,
    so collisions are still possible for contrived path/name combinations.
    """
    if name:
        value = f"{file_path}::{name}"
    else:
        value = file_path
    return _canonical_ident(entity_type, value)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _default_git_branch(repo_path: str) -> str:
    """Resolve the branch to ingest when the caller didn't pass one explicitly.

    MINIGRAF_GIT_BRANCH (matching the other MINIGRAF_* ingestion env vars)
    takes precedence, trusted as-is with no existence check. Otherwise
    auto-detect the repo's actual default branch by trying main then master,
    so ingestion tracks a stable target instead of silently following
    whatever ref happens to be checked out (#130). Only falls back to "HEAD"
    if neither exists.
    """
    env_branch = os.environ.get("MINIGRAF_GIT_BRANCH")
    if env_branch:
        return env_branch
    for candidate in ("main", "master"):
        result = _subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return candidate
    return "HEAD"


def _git_commits(
    repo_path: str,
    watermark_hash: Optional[str],
    branch: str = "HEAD",
) -> List[tuple]:
    """Return list of (hash, ts_iso, author_email, subject) in topological order."""
    range_spec = f"{watermark_hash}..{branch}" if watermark_hash else branch
    result = _subprocess.run(
        ["git", "log", "--topo-order", "--reverse", "--format=%H %at %ae %s", range_spec],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    commits = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 3)
        hash_ = parts[0]
        ts_unix = int(parts[1])
        author = parts[2]
        subject = parts[3] if len(parts) > 3 else ""
        ts_iso = datetime.datetime.fromtimestamp(ts_unix, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commits.append((hash_, ts_iso, author, subject))
    return commits


def _git_diff_tree_raw(repo_path: str, commit_hash: str) -> List[tuple]:
    """Return (status_char, old_mode, new_mode, old_sha, new_sha, path, old_path,
    similarity) for every changed path in a commit, via a single
    `git diff-tree --raw` call.

    -M enables git's own content-similarity rename detection (default 50%
    threshold, unchanged — see the 2026-07-14 rename-tracking design doc's
    "Component 1" for why no additional threshold filtering is applied on
    top of git's own judgment). Deliberately no -C (copy detection) — a copy
    leaves the original in place *and* creates a new, independent entity;
    treating it as a rename would misrepresent history.

    A rename/copy raw line has TWO tab-separated paths (old, then new), not
    one, e.g. ":100644 100644 <sha> <sha> R100\told.py\tnew.py" — naively
    keeping the old single-partition parse would fold both paths into one
    bogus string. old_path is "" for every non-rename status. similarity is
    the numeric suffix of the status (e.g. 100 for "R100", 57 for "R057"),
    None for non-rename statuses.

    Supersedes running diff-tree a second time just to detect gitlinks:
    --raw already carries file mode (needed to spot submodule paths, mode
    160000) in the same subprocess invocation _extract_commit already makes.

    Merge commits (#185): plain `git diff-tree --raw` (no -m/-c/--cc) is
    documented git behavior to emit NOTHING for a commit with more than one
    parent, even when real content was authored at the merge point itself
    (most commonly, manual conflict-resolution edits). Ordinary clean merges
    don't need special handling here -- every underlying change is already
    reachable via the individual non-merge commits `_git_commits`' plain
    `git log` walk visits regardless.

    Rather than checking parent count up front (an extra `git log -1`
    subprocess call on every single commit, working against this function's
    single-subprocess-call design goal for the overwhelmingly common
    single-parent case), the plain diff-tree call always runs first; a
    parent-count check (and the _git_diff_tree_combined_raw fallback it
    guards) only happens on the rare path where it comes back empty -- which
    is exactly the signal ("root or single-parent commit truly touched
    nothing" vs. "this is a merge commit, plain diff-tree always returns
    nothing regardless of content") this needs to distinguish.

    On that merge path, _git_diff_tree_merge_missed_removals additionally
    supplements --cc's own output with content genuinely discarded at the
    merge (#191) -- present on exactly one parent's side and dropped
    entirely during conflict resolution, which --cc's combined-diff
    semantics can never surface (see that function's docstring).
    """
    result = _subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "-r", "-M", "--raw", "--root", commit_hash],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    entries = []
    for line in result.stdout.strip().splitlines():
        if not line.startswith(":"):
            continue
        meta, sep, rest = line.partition("\t")
        if not sep:
            continue
        fields = meta[1:].split(" ")
        if len(fields) < 5:
            continue
        old_mode, new_mode, old_sha, new_sha, status_field = fields[0], fields[1], fields[2], fields[3], fields[4]
        status = status_field[0]
        similarity = int(status_field[1:]) if len(status_field) > 1 and status_field[1:].isdigit() else None
        if status in ("R", "C"):
            old_path, _, path = rest.partition("\t")
        else:
            old_path, path = "", rest
        entries.append((status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity))
    if not entries:
        parent_hashes = _git_parent_hashes(repo_path, commit_hash)
        if len(parent_hashes) > 1:
            cc_entries = _git_diff_tree_combined_raw(repo_path, commit_hash)
            cc_paths = {e[5] for e in cc_entries}
            missed_removals = _git_diff_tree_merge_missed_removals(
                repo_path, commit_hash, parent_hashes, cc_paths
            )
            return cc_entries + missed_removals
    return entries


def _git_diff_tree_combined_raw(repo_path: str, commit_hash: str) -> List[tuple]:
    """Combined-diff (`--cc`) raw parse, used only for merge commits (#185).

    `--cc`'s combined-diff format already restricts its output to paths whose
    content differs from EVERY parent -- exactly the "genuinely authored at
    the merge point" set this exists to recover. A file that matches at
    least one parent unchanged never appears here, which is what keeps this
    safe for the common "both sides touched different files" clean-merge
    case (reports nothing) and the "both sides touched the same file in
    non-overlapping, auto-merged hunks" case (reports the file, since its
    merged content differs from both individual parents, same as a manual
    conflict resolution would).

    Combined raw lines carry one leading ':' and one mode/sha per parent
    (plus one more of each for the merge result itself), e.g. for an
    ordinary 2-parent merge:
    "::100644 100644 100644 <sha1> <sha2> <sha_new> MM\tpath". Rename/copy
    detection doesn't apply in combined-diff mode, so there is always
    exactly one tab-separated path field -- old_path is always "" and
    similarity is always None, matching _git_diff_tree_raw's non-rename rows.

    status is derived from the mode columns rather than the trailing status
    letters (which are one char per parent, e.g. "MM", and don't collapse
    cleanly to _git_diff_tree_raw's single-char contract): new_mode all
    zeros means "D"; every old_mode all zeros means "A" (the path exists in
    none of the parents); otherwise "M". old_mode/old_sha are taken from the
    first parent only -- combined diff has no single unambiguous "old" side
    for a merge, and the exact old-side content only feeds this codebase's
    best-effort rename-matching heuristic (_extract_commit's
    old_entity_nodes), not the fact-extraction this issue is about.

    Residual gap (--cc only reports a path when it differs from EVERY
    parent, so content that exists on exactly one parent's side and is
    discarded entirely at the merge -- final tree matches the OTHER parent,
    which never had it -- never appears here) is covered by a separate
    supplement, `_git_diff_tree_merge_missed_removals`, called from
    `_git_diff_tree_raw` right after this function for every merge commit.
    See #191 and that function's docstring.
    """
    result = _subprocess.run(
        ["git", "diff-tree", "--cc", "--no-commit-id", "-r", "--raw", "--root", commit_hash],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    entries = []
    for line in result.stdout.strip().splitlines():
        if not line.startswith(":"):
            continue
        meta, sep, path = line.partition("\t")
        if not sep:
            continue
        n_parents = len(meta) - len(meta.lstrip(":"))
        fields = meta[n_parents:].split(" ")
        if len(fields) != 2 * n_parents + 3:
            continue
        old_modes = fields[:n_parents]
        new_mode = fields[n_parents]
        old_shas = fields[n_parents + 1: 2 * n_parents + 1]
        new_sha = fields[2 * n_parents + 1]
        zero_mode = "0" * len(new_mode)
        if new_mode == zero_mode:
            status = "D"
        elif all(m == zero_mode for m in old_modes):
            status = "A"
        else:
            status = "M"
        entries.append((status, old_modes[0], new_mode, old_shas[0], new_sha, path, "", None))
    return entries


def _git_diff_tree_merge_missed_removals(
    repo_path: str, commit_hash: str, parent_hashes: List[str], already_reported_paths: Set[str],
) -> List[tuple]:
    """Recover paths whose content was discarded entirely while resolving a
    merge (#191) -- present on exactly one parent's side, absent from the
    merge's own final tree, and therefore invisible to both the plain
    diff-tree call (always empty for any merge commit) and `--cc`'s combined
    diff (which only reports a path when it differs from EVERY parent -- a
    path absent from the final tree AND absent from some other parent
    matches that other parent trivially, so --cc excludes it too; see
    _git_diff_tree_combined_raw's docstring).

    Only ever called as a supplement to _git_diff_tree_combined_raw's output
    for a merge commit, with that output's paths passed in as
    already_reported_paths so a path --cc already reported (e.g. a genuine
    full removal, differing from every parent) is never double-counted.

    For each parent Pi, diffing the merge commit directly against Pi's own
    tree (mirroring what `-m` reports for that parent) surfaces every path
    Pi had that the merge's final tree lacks, as an ordinary "D" row. Most of
    these are NOT this issue's bug: the overwhelmingly common case is a path
    that already existed back at the merge-base too, and was deleted by an
    ordinary single-parent commit on some OTHER parent's own lineage --
    `_git_commits`' plain walk already visited that commit directly and
    reported the same "D" there, so re-reporting it here would double-close
    an already-closed fact. The distinguishing test: was this path already
    absent at the merge-base between Pi and every other parent? If so, the
    removal is old news, already handled by that ordinary commit -- skip it.
    Only a path that did NOT exist at any other-parent merge-base (i.e. it
    was born strictly after the branches diverged, entirely on Pi's side,
    and the merge simply never incorporated it) is the
    never-recorded-elsewhere case #191 is about.

    An octopus merge (>2 parents) is handled the same way, checking each
    candidate path's history against every OTHER parent individually -- a
    path only counts as genuinely new if it's absent at the merge-base with
    ALL of them, not just one.
    """
    entries: List[tuple] = []
    seen = set(already_reported_paths)
    for i, parent in enumerate(parent_hashes):
        other_parents = [p for j, p in enumerate(parent_hashes) if j != i]
        if not other_parents:
            continue
        result = _subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "-r", "-M", "--raw", parent, commit_hash],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode != 0:
            continue
        for line in result.stdout.strip().splitlines():
            if not line.startswith(":"):
                continue
            meta, sep, path = line.partition("\t")
            if not sep:
                continue
            fields = meta[1:].split(" ")
            if len(fields) < 5:
                continue
            old_mode, new_mode, old_sha, new_sha, status_field = fields[0], fields[1], fields[2], fields[3], fields[4]
            if status_field[0] != "D" or path in seen:
                continue
            existed_elsewhere_at_divergence = False
            for other in other_parents:
                mb = _subprocess.run(
                    ["git", "merge-base", parent, other],
                    cwd=repo_path, capture_output=True, text=True,
                )
                base = mb.stdout.strip() if mb.returncode == 0 else ""
                if not base:
                    existed_elsewhere_at_divergence = True  # no common ancestor -- be conservative
                    break
                check = _subprocess.run(
                    ["git", "cat-file", "-e", f"{base}:{path}"],
                    cwd=repo_path, capture_output=True,
                )
                if check.returncode == 0:
                    existed_elsewhere_at_divergence = True
                    break
            if existed_elsewhere_at_divergence:
                continue
            seen.add(path)
            entries.append(("D", old_mode, new_mode, old_sha, new_sha, path, "", None))
    return entries


_GITLINK_MODE = "160000"


def _gitlink_changes(raw_entries: List[tuple]) -> List[tuple]:
    """Filter _git_diff_tree_raw's output down to gitlink-involving rows,
    collapsed into three cases by mode pair rather than by the raw status
    letter (which varies: A/D/M/T can all represent a gitlink change
    depending on what else happened to the same path):

      "add"    — new_mode is a gitlink, old_mode is not. Covers a plain
                 submodule addition (status A) and a same-path flip from a
                 regular blob into a gitlink (status T).
      "bump"   — both modes are gitlinks (status M): the pinned commit changed.
      "remove" — old_mode is a gitlink, new_mode is not. Covers a plain
                 submodule removal (status D) and a same-path flip from a
                 gitlink back into a regular blob (status T).

    sha is the new pinned commit for "add"/"bump", or the last-known pinned
    commit for "remove" (needed by the caller to close the right fact).
    """
    changes = []
    for status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity in raw_entries:
        old_is_link = old_mode == _GITLINK_MODE
        new_is_link = new_mode == _GITLINK_MODE
        if not old_is_link and not new_is_link:
            continue
        if new_is_link and not old_is_link:
            changes.append(("add", new_sha, path))
        elif old_is_link and new_is_link:
            changes.append(("bump", new_sha, path))
        else:
            changes.append(("remove", old_sha, path))
    return changes


def _git_changed_files(repo_path: str, commit_hash: str) -> List[tuple]:
    """Return list of (status_char, path) for files changed in this commit.

    Not currently called by the ingestion pipeline (which uses _git_diff_tree_raw
    instead, for mode-aware parsing) — retained as a general-purpose git helper.
    """
    result = _subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "-r", "--name-status", "--root", commit_hash],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    changes = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status_char = parts[0][0]  # A, M, D, R, C → take first char
            changes.append((status_char, parts[1]))
    return changes


def _edn_escape(s: str) -> str:
    """Escape a string for embedding in an EDN double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


_EDN_ESCAPE_SEQUENCE = re.compile(r'\\(.)')


def _edn_unescape(s: str) -> str:
    """Reverse _edn_escape: \\\" -> \" and \\\\ -> \\ in a captured EDN string body."""
    return _EDN_ESCAPE_SEQUENCE.sub(lambda m: m.group(1), s)


def _git_file_content(repo_path: str, commit_hash: str, file_path: str) -> bytes:
    """Return raw bytes of a file at the given commit."""
    result = _subprocess.run(
        ["git", "show", f"{commit_hash}:{file_path}"],
        cwd=repo_path, capture_output=True, check=True,
    )
    return result.stdout


def _git_blob_content(repo_path: str, blob_sha: str) -> bytes:
    """Return raw bytes of a blob by its own SHA, independent of any commit/path.

    Used to fetch a file's *old* content directly from _git_diff_tree_raw's
    old_sha field (a plain blob SHA) when comparing pre/post rename or
    modification content — cheaper than resolving a parent commit hash and
    re-deriving the old path, and correct even when the old path no longer
    exists at any reachable commit-ish (e.g. mid-history rewrites).
    """
    result = _subprocess.run(
        ["git", "cat-file", "blob", blob_sha],
        cwd=repo_path, capture_output=True, check=True,
    )
    return result.stdout


def _is_ignored_path(file_path: str, patterns: Sequence[str]) -> bool:
    """Simplified .gitignore-style match: no negation, no ** anchoring, no new
    dependency (see 2026-07-14 path-ignore design doc's "Matching" section for
    why full gitignore semantics via pathspec were rejected).

    - Pattern ending in "/": matches if that name is any path segment
      (directory-anywhere-in-path semantics — "vendor/" matches both
      "src/vendor/foo.js" and "vendor/bar.js", but never a bare substring
      like "vendored_thing.js").
    - Pattern containing a glob char (*, ?, [): fnmatch against the
      basename, then the full path.
    - Otherwise: exact match against any path segment or the basename.
    """
    segments = Path(file_path).parts
    basename = segments[-1] if segments else file_path
    for pattern in patterns:
        if pattern.endswith("/"):
            if pattern.rstrip("/") in segments:
                return True
        elif any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatch(basename, pattern) or fnmatch.fnmatch(file_path, pattern):
                return True
        elif pattern in segments or pattern == basename:
            return True
    return False


_DEFAULT_IGNORE_PATTERNS: Tuple[str, ...] = (
    "3rdParty/", "third_party/", "vendor/", "node_modules/",
    "dist/", "build/", "*.min.js", "*.map",
)


def _load_ignore_patterns(repo_path: str) -> List[str]:
    """Resolve the effective ignore-pattern list for one ingestion run.

    Merges, in order: built-in defaults, MINIGRAF_INGEST_IGNORE (comma-separated),
    and an optional .temporalignore file (one pattern per line, blank lines and
    "#"-prefixed comments skipped) read once from repo_path's current working
    tree — not re-read per historical commit, since ignore config describes how
    this run should behave, not something that varies commit-to-commit.

    Fails closed: an unreadable or undecodable .temporalignore file contributes
    zero extra patterns (defaults + env var still apply), matching best-effort
    conventions used elsewhere in this file (e.g. _parse_gitmodules).
    """
    patterns: List[str] = list(_DEFAULT_IGNORE_PATTERNS)

    env_patterns = os.environ.get("MINIGRAF_INGEST_IGNORE")
    if env_patterns:
        patterns.extend(p.strip() for p in env_patterns.split(",") if p.strip())

    ignore_file = Path(repo_path) / ".temporalignore"
    if ignore_file.is_file():
        try:
            lines = ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            lines = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)

    return patterns


def _known_files_at_commit(
    repo_path: str, commit_hash: str, ignore_patterns: Sequence[str] = ()
) -> Dict[str, List[str]]:
    """Return {file_path: []} for every file tracked at commit_hash whose extension
    has a supported tree-sitter grammar (_EXT_TO_LANG) and that doesn't match
    ignore_patterns (see _is_ignored_path) — excluding a vendored path here means
    any import resolving against it falls through to the external-dependency
    fallback in _resolve_module_import instead of matching internally (#115).

    A pure function of commit_hash via `git ls-tree -r --name-only`, independent of
    ingestion progress — unlike the incrementally-mutated file_entities dict, this
    reflects the repo's actual state at that specific historical commit, so it can
    run inside _extract_commit on the worker pool instead of waiting for the serial
    main thread to catch up. Shaped like file_entities (dict keyed on path, values
    unused) so it can be passed straight into _resolve_module_import, which only
    ever reads the dict's keys.
    """
    result = _subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit_hash],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    known: Dict[str, List[str]] = {}
    for path in result.stdout.strip().splitlines():
        if Path(path).suffix.lower() in _EXT_TO_LANG and not _is_ignored_path(path, ignore_patterns):
            known[path] = []
    return known


def _parse_gitmodules(content: bytes) -> Dict[str, Dict[str, str]]:
    """Parse .gitmodules content into {path: {"name": ..., "url": ...}}.

    Best-effort: git config's `[section "subsection"]` syntax is a strict
    superset of what configparser expects for ordinary cases, so malformed
    or unusual .gitmodules content fails closed to an empty dict rather
    than raising — matches this file's existing best-effort git/parse
    conventions (see _extract_from_source's bare except).
    """
    result: Dict[str, Dict[str, str]] = {}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content.decode("utf-8", errors="replace"))
    except configparser.Error:
        return result
    for section in parser.sections():
        m = re.match(r'submodule\s+"(.+)"', section)
        if not m:
            continue
        path = parser.get(section, "path", fallback=None)
        url = parser.get(section, "url", fallback=None)
        if path:
            result[path] = {"name": m.group(1), "url": url or ""}
    return result


def _git_gitmodules_at(repo_path: str, commit_hash: str) -> Dict[str, Dict[str, str]]:
    """Fetch and parse .gitmodules as it exists at commit_hash.

    Empty dict if missing or unparseable — most repos never have a
    .gitmodules file at all, which is the normal case, not an error.
    """
    try:
        content = _git_file_content(repo_path, commit_hash, ".gitmodules")
    except Exception:
        return {}
    return _parse_gitmodules(content)


def _git_parent_hashes(repo_path: str, commit_hash: str) -> List[str]:
    """Return the parent commit hashes for the given commit (empty for root commits)."""
    result = _subprocess.run(
        ["git", "log", "-1", "--format=%P", commit_hash],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    raw = result.stdout.strip()
    return raw.split() if raw else []


def _git_tags(repo_path: str) -> List[tuple]:
    """Return list of (tag_name, commit_hash, date_iso) for all tags in the repo.

    For annotated tags, returns the dereferenced commit hash.
    For lightweight tags, returns the tagged commit directly.
    Date is the tagger date for annotated tags, or commit date for lightweight.
    """
    result = _subprocess.run(
        ["git", "tag", "-l", "--sort=version:refname",
         "--format=%(refname:short)\t%(*objectname)\t%(objectname)\t%(creatordate:iso-strict)"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    tags = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 3)
        if len(parts) < 3:
            continue
        tag_name = parts[0]
        deref_hash = parts[1].strip()   # non-empty for annotated tags
        obj_hash = parts[2].strip()
        date_raw = parts[3].strip() if len(parts) > 3 else ""
        commit_hash = deref_hash if deref_hash else obj_hash
        if not commit_hash:
            continue
        tags.append((tag_name, commit_hash, date_raw))
    return tags


# ---------------------------------------------------------------------------
# Bi-temporal write helpers
# ---------------------------------------------------------------------------


def _resolve_introduced_by(
    db: Any, state: "_ForwardWalkState", ident: str
) -> Optional[str]:
    """The commit ident to retract for ident's :introduced-by at a close site.

    Prefers the walk state, which the forward walk maintains for free at every
    introduction. Falls back to a DB read for entities the state never saw --
    principally entities Stream 2 introduced during THIS run, which Stage B's
    _forward_apply(lifecycle_only=True) then closes. Without the fallback #231
    would survive for exactly those.

    The fallback is a per-CLOSE read, not per-ident-per-commit, so it does not
    feed #239's hot path (1.33M :introduced-by point queries, 33.6% of at-scale
    wall clock). Do not hoist it into a preloaded set: #235 removed a
    provisional-ident prefilter for being wrong in both directions, and the
    same argument applies to any run-start snapshot of lineage.

    Returns None when the entity genuinely has no :introduced-by (an
    unresolved-import stub), which _build_close_triples treats as "do not
    retract".
    """
    known = state.entity_introduced_by.get(ident)
    if known is not None:
        return known
    return _entity_introduced_by_query(db, ident)


def _build_close_triples(
    ident: str,
    description: str,
    module_ident: str,
    extra_contains_parent: Optional[str] = None,
    *,
    close_entity_type: bool = False,
    entity_type_kw: Optional[str] = None,
    file_value: Optional[str] = None,
    is_static: Optional[bool] = None,
    introduced_by: Optional[str] = None,
) -> List[str]:
    """Return triple strings needed to bi-temporally close an entity.

    Closes :ident (canonical existence fact), :description (with real value),
    and the parent module's :contains edge.  The module's own :contains triple
    is omitted when ident == module_ident (modules have no parent module here).

    extra_contains_parent closes a SECOND :contains edge alongside the module's
    one.  Fields with a real (extracted) owning class carry two containment
    parents — [module :contains field] AND [class :contains field] (see
    _precompute_file_triples) — so both must be retracted when the field closes,
    or the class-contains edge leaks open forever.  Callers pass the field's
    class ident here (from field_class_ident); it is ignored when None or equal
    to ident/module_ident so non-field close sites are unaffected. The field's
    OWN [ident :class extra_contains_parent] edge (the reverse direction) is
    always closed alongside it — see issue #134, this was the concrete gap that
    let [?f :class ?c] queries without an :ident join resurrect removed fields.

    close_entity_type/file_value/is_static close the remaining secondary
    attributes flagged by #134 (:entity-type, :path/:file, :static) that
    _precompute_file_triples asserts at introduction but this function
    previously never retracted, letting queries that filter on them without
    joining :ident silently include removed entities. These are opt-in
    (default off) because this function is also called for external-dependency
    (submodule) idents that reuse the "module" ident prefix but were never
    actually asserted as :type/module — deriving :entity-type from the ident
    prefix there would transact a false fact, so only call sites that KNOW
    they're closing a real module/function/class/variable/field pass
    close_entity_type=True.

    entity_type_kw is the escape hatch for exactly that submodule case (#137):
    an explicit ":type/xxx" keyword to close instead of deriving one from the
    ident prefix, for callers whose ident prefix does NOT match their real
    :entity-type. Takes precedence over close_entity_type when both are given
    (they shouldn't be — pass one or the other).

    introduced_by is the entity's :introduced-by commit ident, closed alongside
    everything else (#231). Opt-in for the same reason close_entity_type is:
    unresolved-import stubs reuse the module ident prefix but never have an
    :introduced-by fact (see _forward_apply's dep-edge handling), so deriving
    one here would retract a fact that was never asserted. Callers get the
    value from _resolve_introduced_by, which prefers the walk state and falls
    back to a DB read.

    Leaving this fact open was the whole of #231: a closed-and-purged entity
    still answered a bare [?e :introduced-by ?c] query, which made
    _entity_introduced_by_query an unsound liveness test.
    """
    triples = [
        f'[{ident} :ident "{_edn_escape(ident)}"]',
        f'[{ident} :description "{_edn_escape(description)}"]',
    ]
    if ident != module_ident:
        triples.append(f"[{module_ident} :contains {ident}]")
    if (
        extra_contains_parent is not None
        and extra_contains_parent != ident
        and extra_contains_parent != module_ident
    ):
        triples.append(f"[{extra_contains_parent} :contains {ident}]")
        triples.append(f"[{ident} :class {extra_contains_parent}]")
    if entity_type_kw is not None:
        triples.append(f"[{ident} :entity-type {entity_type_kw}]")
    elif close_entity_type:
        entity_type = ident.split("/", 1)[0].lstrip(":")
        triples.append(f"[{ident} :entity-type :type/{entity_type}]")
    if file_value is not None:
        attr = ":path" if ident == module_ident else ":file"
        triples.append(f'[{ident} {attr} "{_edn_escape(file_value)}"]')
    if is_static is not None:
        triples.append(f"[{ident} :static {'true' if is_static else 'false'}]")
    if introduced_by is not None:
        triples.append(f"[{ident} :introduced-by {introduced_by}]")
    return triples


def _forget_closed_entity(
    ident: str,
    file_path: Optional[str],
    entity_valid_from: Dict[str, str],
    entity_descriptions: Dict[str, str],
    field_class_ident: Dict[str, str],
    file_entities: Dict[str, List[str]],
    field_static_ident: Optional[Dict[str, bool]] = None,
    entity_introduced_by: Optional[Dict[str, str]] = None,
) -> None:
    """Purge a just-closed ident from all in-memory lifecycle bookkeeping.

    Once an entity's bi-temporal window is genuinely closed (i.e. the fact is
    invisible at current time — true only since the transact-ordering fix in
    1b2e262), its stale entries in these serially-threaded dicts must be
    dropped so a later commit is not misled by them:

    - entity_valid_from: _build_code_triples keys "is this genuinely new?" on
      absence here. Leaving a stale entry makes a re-introduction at the same
      ident take the "already known, only :modified-in" branch, so its
      :ident/:description/:path/:introduced-by never get re-asserted — a ghost
      entity with no current :ident fact.
    - file_entities[file_path]: a stale ident lingering here is re-discovered by
      a later commit's removal-detection diff (previous_idents - current) if the
      path is reused, and closed a SECOND time; because entity_valid_from still
      held its ORIGINAL introduction timestamp, that second close would span the
      whole gap and silently resurrect the entity across its closed window.
    - entity_descriptions / field_class_ident / field_static_ident: purged for
      consistency so no stale description, class-containment parent, or
      :static value is read for a future re-introduction of the same ident.
    - entity_introduced_by: holds the ident's introducing commit for #231's
      close-time retract. A stale entry would make a re-introduction at the
      same ident retract the OLD introduction's :introduced-by on its next
      close -- a fact that no longer exists at that value.

    Call this at EVERY entity close site, AFTER that site has read whatever it
    needs (description, orig_ts, class ident) to build its close triples — never
    before. Each ident is closed at exactly one site per commit, so purging one
    ident never removes state another site in the same commit still needs.

    file_path is the owning file's path (the module the ident lives under); pass
    None to skip the file_entities removal (e.g. idents not tracked per-file).
    Callers that iterate a file_entities list while calling this MUST iterate a
    copy, since this mutates file_entities[file_path] in place.
    """
    entity_valid_from.pop(ident, None)
    entity_descriptions.pop(ident, None)
    field_class_ident.pop(ident, None)
    if field_static_ident is not None:
        field_static_ident.pop(ident, None)
    if entity_introduced_by is not None:
        entity_introduced_by.pop(ident, None)
    if file_path is not None:
        idents = file_entities.get(file_path)
        if idents is not None:
            try:
                idents.remove(ident)
            except ValueError:
                pass


def _ingest_transact(
    db: Any,
    triples: List[str],
    commit_ts_iso: str,
    reason: str,
    index_con: Optional[Any] = None,
) -> None:
    """Transact code-structure facts with :valid-from set to the commit timestamp."""
    if not triples:
        return
    facts_str = "[" + " ".join(triples) + "]"
    _transact(db, facts_str, commit_ts_iso, index_con=index_con)


def _ingest_close(
    db: Any,
    triples: List[str],
    original_ts_iso: str,
    commit_ts_iso: str,
    reason: str,
    index_con: Optional[Any] = None,
) -> None:
    """Close a fact's valid window at the deletion commit timestamp.

    Two-step process:
    1. Retract each original open-ended fact so it vanishes from current-time
       queries (retract has no temporal options, so this removes the unbounded
       assertion from the live view while keeping it in transaction history).
       This is also the step that removes the fact from the live index.
    2. Re-transact the same facts with explicit :valid-from + :valid-to so the
       historical valid window is preserved for point-in-time queries. This
       half is bounded (valid_to is not None) but IS indexed too, as a
       historical row carrying its window -- this is what makes a closed
       entity's facts recoverable through the fact index as a labeled entry
       point into history, instead of just vanishing.

    Triples are retracted one-by-one to avoid EAVT collision on :contains edges
    (Minigraf's pending index omits value bytes, so batching multiple
    [module :contains fn] retracts could collide).
    """
    if not triples:
        return
    for triple in triples:
        try:
            _retract(db, f"[{triple}]", index_con=index_con)
        except Exception:
            pass  # best-effort: original may not exist if preload was incomplete
    facts_str = "[" + " ".join(triples) + "]"
    _transact(
        db, facts_str, original_ts_iso, valid_to=commit_ts_iso, index_con=index_con,
    )


def _watermark_query(db: Any) -> Optional[str]:
    """Return the hash of the last ingested commit, or None if no watermark exists."""
    raw = _db_execute(db, "(query [:find ?h :where [:ingestion/watermark :hash ?h]])")
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _commit_date_query(db: Any, commit_hash: Optional[str]) -> Optional[str]:
    """Return an already-ingested commit's ISO 8601 :date, or None.

    :any-valid-time is required, not cosmetic: a commit's own facts are
    transacted at THAT COMMIT's timestamp, which can sit in the future
    relative to wall-clock time (clock skew, doctored committer dates), so a
    plain query's implicit "as of now" filter can miss them -- the same
    reason _total_ingested_query gives. Commit facts are never closed, so
    unlike that function no :db/valid-to filter is needed to pick the live
    row; a re-walked position can re-transact an identical :date under a
    fresh valid-from (#156), but every such row carries the same value.

    Returning None for a NON-EMPTY watermark is not benign: it degrades every
    caller's resume-position bound back to the unrestricted, pre-fix B1
    queries (see _load_ingestion_preload_state), which is a correctness
    regression rather than a slow path. The degradation is therefore
    announced on stderr instead of failing silently. None for an EMPTY
    watermark is the ordinary fresh-graph case and stays quiet.
    """
    if not commit_hash:
        return None
    try:
        raw = _db_execute(
            db,
            f"(query [:find ?d :any-valid-time "
            f":where [:commit/{commit_hash[:12]} :date ?d]])",
        )
        results = json.loads(raw).get("results", [])
    except Exception as e:
        print(
            f"[ingest] watermark commit {commit_hash[:12]} :date lookup failed "
            f"({e}); preloads fall back to unbounded current-graph queries",
            file=sys.stderr,
        )
        return None
    if not results:
        print(
            f"[ingest] watermark commit {commit_hash[:12]} has no :date fact; "
            f"preloads fall back to unbounded current-graph queries",
            file=sys.stderr,
        )
        return None
    return results[0][0]


def _iso_to_epoch_ms(ts_iso: Optional[str]) -> Optional[int]:
    """Convert an ingest ISO 8601 timestamp to minigraf's epoch-ms valid-time
    scale, so it can be compared against the :db/valid-from/:db/valid-to
    pseudo-attributes. Returns None if ts_iso is absent or unparseable."""
    if not ts_iso:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _total_ingested_query(db: Any) -> int:
    """Return the :total-ingested watermark recorded by the last *completed* run, or 0.

    Only written on clean completion (see _last_run_write) — a run interrupted
    mid-way (e.g. by lock contention) leaves this stale even though further
    commits were durably persisted. Use _count_commit_entities for the true
    current count.

    :any-valid-time is required here (not a design choice) -- valid-from is
    the run's own timestamp, not real wall-clock time, so a plain query's
    implicit "as of now" filter can miss facts whose valid-from lands after
    the real current moment. But :any-valid-time also surfaces already-closed
    historical rows from prior runs, so the :db/valid-to pseudo-attribute is
    bound and filtered to the open-fact sentinel to select only the live
    value -- otherwise, even after _last_run_write's #186 retract-before-
    reassert fix, this could still nondeterministically return a stale run's
    value depending on row order.
    """
    raw = _db_execute(
        db,
        "(query [:find ?n :any-valid-time "
        ":where [:ingestion/last-run-at :total-ingested ?n] "
        "[:ingestion/last-run-at :db/valid-to ?vt] [(= ?vt 9223372036854775807)]])",
    )
    results = json.loads(raw).get("results", [])
    return int(results[0][0]) if results else 0


def _count_commit_entities(db: Any) -> int:
    """Return the true number of durably persisted :type/commit entities.

    Unlike _total_ingested_query, this reflects reality even after a run was
    interrupted before it could write its completion watermark.

    count-distinct, NOT count (#317). `(count ?e)` counts matching ROWS, not
    distinct entities -- measured on minigraf 2.0.0: two :type/commit entities
    read 2, and a second live :entity-type row on ONE of them makes the same
    query read 3 while count-distinct still reads 2. That second row is
    reachable because minigraf is not idempotent at the graph level for
    re-transacting the same (entity, attribute, value) under a different
    valid-from (#156) -- the same non-idempotency _watermark_update diffs its
    constant attributes to avoid.

    NOT reachable from ingestion, and the narrow claim is the honest one:
    _reverse_apply and _forward_apply transact the commit triples at
    commit_ts_iso, so a resumed run re-walking a position whose
    _frontier_persist_claim never landed (#313) re-writes the identical triple
    at the identical valid-from and it COLLAPSES rather than duplicating. The
    reachable path is the public handler -- it accepts a caller-written
    `[:commit/xyz :entity-type :type/commit]` and transacts it at wall-clock
    valid-from (it does NOT add an :entity-type of its own: measured, a bare
    `[:commit/xyz :description "..."]` leaves no :entity-type row, #353);
    twice, seconds apart, on a graph mixing memory writes with ingested
    history, and the old query was wrong.

    #317's commit census compares this number against the repo's own
    `git rev-list --count`, where one duplicated entity would CANCEL one
    genuinely lost commit and read clean on a graph that lost history -- the
    precise failure that census exists to catch.
    """
    raw = _db_execute(db, "(query [:find (count-distinct ?e) :where [?e :entity-type :type/commit]])")
    results = json.loads(raw).get("results", [])
    return int(results[0][0]) if results else 0


def _watermark_update(db: Any, commit_hash: str, commit_ts_iso: str, reason: str, index_con: Optional[Any] = None) -> None:
    """Record the last successfully ingested commit hash in the graph.

    Called once per COMMIT (not once per run) inside _run_ingestion's main loop.
    :entity-type/:ident/:description are constant and never change after the
    first call -- diffed against the entity's current live values first, and
    only retracted+re-transacted when the value actually changed, so they are
    written exactly once rather than accumulating a duplicate per commit
    (minigraf is not idempotent at the graph level for re-transacting the same
    (entity, attribute, value) under a different valid-from -- see #156). :hash
    always changes, so it keeps its unconditional retract-then-reassert. Does
    NOT retroactively collapse duplicates a pre-fix run already created -- a
    duplicate row whose value trivially matches desired is left alone, same
    bounded/self-healing-by-omission scoping as _ingest_tags' own #156 fix.
    """
    current_raw = _db_execute(db, "(query [:find ?a ?v :where [:ingestion/watermark ?a ?v]])")
    current: Dict[str, str] = dict(json.loads(current_raw).get("results", []))

    def _edn(attr: str, value: str) -> str:
        return value if attr == ":entity-type" else f'"{_edn_escape(value)}"'

    constants = {
        ":entity-type": ":type/ingestion",
        ":ident": ":ingestion/watermark",
        ":description": "git ingestion watermark",
    }

    to_retract: List[str] = []
    to_transact: List[str] = []
    for attr, value in constants.items():
        if current.get(attr) == value:
            continue  # already correct -- skip to avoid creating a duplicate live fact (#156)
        if attr in current:
            to_retract.append(f"[:ingestion/watermark {attr} {_edn(attr, current[attr])}]")
        to_transact.append(f"[:ingestion/watermark {attr} {_edn(attr, value)}]")

    if ":hash" in current:
        to_retract.append(f"[:ingestion/watermark :hash {_edn(':hash', current[':hash'])}]")
    to_transact.append(f"[:ingestion/watermark :hash {_edn(':hash', commit_hash)}]")

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)


_FRONTIER_LOW_IDENT = ":ingestion/frontier-low"
_FRONTIER_HIGH_IDENT = ":ingestion/frontier-high"


def _frontier_read_bounds(db: Any, ident: str) -> Optional[Tuple[str, str]]:
    """Return (lo_hash, hi_hash) for ident's :type/ingest-interval fact, or
    None if that interval hasn't been created yet."""
    raw = _db_execute(
        db,
        f"(query [:find ?lo ?hi :where [{ident} :lo-hash ?lo] [{ident} :hi-hash ?hi]])",
    )
    results = json.loads(raw).get("results", [])
    return (results[0][0], results[0][1]) if results else None


def _frontier_read_pos_count(db: Any, ident: str) -> Optional[int]:
    """#326: the interval's stored position-count denominator, or None if it
    carries none (a graph written before this attribute existed).

    Read by its own query rather than joined into _frontier_read_bounds: an
    interval with no :pos-count must still be READABLE -- a five-way join would
    make it invisible, which is a different and much worse failure than
    reporting the count as absent.
    """
    raw = _db_execute(
        db, f"(query [:find ?c :where [{ident} :pos-count ?c]])"
    )
    results = json.loads(raw).get("results", [])
    if not results:
        return None
    try:
        return int(results[0][0])
    except (TypeError, ValueError):
        return None


_INTERVAL_PROVISIONAL_IDENT_PREFIX = ":ingestion/interval-provisional-"


def _interval_ident(anchor_hash: str) -> str:
    """Deterministic ident for a provisional interval created at anchor_hash.

    MINTED ONCE, at creation, and never re-derived from current bounds. A
    provisional interval grows DOWNWARD, so keying on :lo-hash would recreate
    the entity on every claim; keying on the CURRENT :hi-hash would rename it
    on every merge. #326 paid for the bounds-keyed version once: two regions
    collided onto one entity, :pos-count became nondeterministic through a
    last-write-wins join, and a retract destroyed the surviving witness.
    """
    return f"{_INTERVAL_PROVISIONAL_IDENT_PREFIX}{anchor_hash[:12]}"


def _intervals_read_extra(db: Any) -> List[Tuple[str, str, str, Optional[int]]]:
    """Every minted provisional interval entity, as
    (ident, lo_hash, hi_hash, pos_count), sorted by ident.

    Callers treat these as living ABOVE frontier-high -- that is a caller
    CONTRACT, not something this query enforces: the where-clause below binds
    only :entity-type plus the presence of :ident/:lo-hash/:hi-hash, with no
    tag predicate, no positional predicate, and no check of the minted-ident
    prefix a caller might otherwise key on. An entity satisfying those three
    clauses is returned regardless of where its bounds actually sit. This is
    deliberate, not an oversight: a below-base extra (which #325's own
    machinery does not produce today, but nothing here rules out) degrades
    conservatively -- it is one more interval a caller re-walks or folds
    defensively, never one silently dropped -- so the query stays permissive
    and callers are the ones who must not assume position from membership
    alone.

    Binds ?ident rather than ?e: `[?e :entity-type :type/ingest-interval]`
    answers in UUID space. frontier-high and frontier-low carry no :ident fact
    and are therefore invisible here BY CONSTRUCTION -- they are read by their
    fixed idents, which is what makes this change migration-free.

    :pos-count is a SECOND query, not a join. An interval carrying no count is
    untrustworthy but must still be enumerable -- it has to be retractable --
    and a single wide join would make it invisible instead, leaking its facts
    forever. Same rule as _completed_regions_read_full.
    """
    raw = _db_execute(
        db,
        "(query [:find ?ident ?lo ?hi :where"
        " [?e :entity-type :type/ingest-interval]"
        " [?e :ident ?ident] [?e :lo-hash ?lo] [?e :hi-hash ?hi]])",
    )
    raw_counts = _db_execute(
        db,
        "(query [:find ?ident ?c :where"
        " [?e :entity-type :type/ingest-interval]"
        " [?e :ident ?ident] [?e :pos-count ?c]])",
    )
    counts: Dict[str, Optional[int]] = {}
    for ident, c in json.loads(raw_counts).get("results", []):
        try:
            counts[str(ident)] = int(c)
        except (TypeError, ValueError):
            counts[str(ident)] = None
    seen = set()
    out: List[Tuple[str, str, str, Optional[int]]] = []
    for ident, lo, hi in json.loads(raw).get("results", []):
        if str(ident) in seen:
            continue
        seen.add(str(ident))
        out.append((str(ident), lo, hi, counts.get(str(ident))))
    return sorted(out, key=lambda r: r[0])


def _frontier_span_count(
    linearization: List[str], lo_hash: str, hi_hash: str
) -> Optional[int]:
    """Positions spanned by [lo_hash, hi_hash] under THIS linearization, or
    None if either bound is absent from it.

    One list scan per bound rather than a hash->pos dict: this is called once
    per persisted claim, and building an O(n) dict there would make a 20k-commit
    ingestion pay 20k dict constructions.
    """
    try:
        return linearization.index(hi_hash) - linearization.index(lo_hash) + 1
    except ValueError:
        return None


# Bumped whenever a change makes facts already in a graph unreadable by the
# current code -- today that means the ident rule in _canonical_ident (#263),
# since ingestion recomputes every ident from scratch rather than reading it
# back, so an old-rule graph read by new-rule code silently FORKS every entity
# instead of erroring. Version 1 is the R3 rule; a graph with no stamp at all
# predates it. There is deliberately NO migration -- see
# docs/superpowers/specs/2026-08-14-ident-rule-r3-and-format-version-design.md:
# the supported recovery is a rebuild into a fresh graph path.
GRAPH_FORMAT_VERSION = 1
_FORMAT_VERSION_IDENT = ":ingestion/format-version"


class GraphFormatVersionError(RuntimeError):
    """Raised when a graph's format version does not match GRAPH_FORMAT_VERSION.

    Deliberately a hard failure rather than a warning: continuing would write
    new-rule idents alongside old-rule ones for the same entities, which
    produces no error anywhere downstream and corrupts the graph silently.
    """


def _graph_format_version_read(db: Any) -> Optional[int]:
    """Return the graph's stamped format version, or None if it has no stamp."""
    raw = _db_execute(
        db, f"(query [:find ?v :where [{_FORMAT_VERSION_IDENT} :version ?v]])"
    )
    results = json.loads(raw).get("results", [])
    if not results:
        return None
    try:
        return int(results[0][0])
    except (TypeError, ValueError):
        # A non-integer stamp is a corrupt stamp, and treating it as "absent"
        # would let the graph be adopted at the current version. Report it as a
        # version that can never match instead.
        return -1


def _graph_has_ingestion_state(db: Any) -> bool:
    """True if this graph has been ingested into before.

    This is what makes an ABSENT stamp mean "version 0", not "fresh" -- the
    distinction the whole guard turns on. Every graph that exists today
    predates the stamp, so treating absence as "new, adopt the current version"
    would stamp a pre-#263 graph as good and produce exactly the silent fork
    the stamp exists to prevent. Only a graph with no ingestion state at all
    may be adopted.
    """
    return (
        _watermark_query(db) is not None
        or _frontier_read_bounds(db, _FRONTIER_LOW_IDENT) is not None
        or _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT) is not None
    )


def _graph_format_version_verify(db: Any) -> None:
    """Refuse to ingest a graph written under a different ident rule.

    READ-ONLY, and deliberately so: this runs at the very top of a run, before
    anything has written, because a refusal partway through would leave a graph
    half-written under two ident rules -- worse than either rule applied
    consistently. The matching write lives in _graph_format_version_stamp_if_new.

    Passes silently for a graph that is stamped at the current version, and for
    a genuinely new graph (which the stamping half then adopts).
    """
    stamped = _graph_format_version_read(db)
    if stamped == GRAPH_FORMAT_VERSION:
        return
    if stamped is None and not _graph_has_ingestion_state(db):
        return  # genuinely new -- _graph_format_version_stamp_if_new adopts it
    found = "no version stamp (pre-#263)" if stamped is None else f"version {stamped}"
    raise GraphFormatVersionError(
        f"This graph has {found}, but this build ingests at graph format "
        f"version {GRAPH_FORMAT_VERSION}. The entity ident rule changed "
        "(#263), and ingesting would silently create a second, forked "
        "entity for everything already in the graph. There is no "
        "migration: re-ingest into a FRESH graph path (set "
        "MINIGRAF_GRAPH_PATH to a new file, or delete the existing graph "
        "and its .fts.sqlite3 index first)."
    )


class GraphIndexDamageError(RuntimeError):
    """Raised when the graph's EAVT and AEVT indexes disagree about an entity.

    project-minigraf/minigraf#370: a process killed mid-save can leave a graph
    that opens without error, answers every attribute-driven scan and count
    correctly, and returns [] for entity-bound lookups, because its EAVT index
    lost most entities' entries while AEVT kept them. Deliberately a hard
    failure (#336): this module has >=14 entity-bound point-query sites --
    frontier bounds, :introduced-by, :ident liveness, the watermarks, the format
    stamp -- and every one misreads on such a graph, so no part of ingestion is
    safe to run on it.
    """


# #336. Lists every live entity through AEVT. minigraf's executor
# (executor.rs selective_fact_fetch) serves an attribute-only pattern from
# FactStorage::get_facts_by_attribute (AEVT) and an entity-literal pattern
# from get_facts_by_entity (EAVT), so this query and _index_cross_check_probe
# read the same facts through the two different indexes. Module-level so a
# test can recognise it.
_INDEX_CROSS_CHECK_POPULATION_QUERY = (
    "(query [:find ?e ?t :where [?e :entity-type ?t]])"
)
# Ingestion control state, probed exhaustively rather than sampled: the format
# stamp, the watermarks, frontier intervals and archived regions. They are few,
# and misreading them is what makes a damaged mature graph look brand new.
# Literals rather than the constants because _COMPLETED_REGION_ENTITY_TYPE is
# defined further down this module; a test pins the two together.
_INDEX_CROSS_CHECK_CONTROL_TYPES = frozenset({
    ":type/ingestion", ":type/ingest-interval", ":type/completed-region",
})
# Uniform random sample of the non-control population. Misses damage covering
# a fraction f of entities with probability (1 - f) ** 512: 0.6% at f = 1%.
_INDEX_CROSS_CHECK_SAMPLE_SIZE = 512


def _index_cross_check_fixed_idents() -> Tuple[str, ...]:
    """The fixed-ident control entities, probed whether or not AEVT lists them.

    This is what keeps the check from failing OPEN on AEVT damage: a graph
    whose AEVT lost the whole :entity-type block returns an empty population
    (measured) while EAVT still answers for these, so an empty population alone
    must not be read as "nothing to check". An ident absent from both indexes
    compares empty to empty, so a fresh graph passes.

    Built at call time, not as a module constant, because
    _LINEAGE_CONFIRMED_THROUGH_IDENT and _CORRECTION_SWEEP_THROUGH_IDENT are
    defined further down this module.
    """
    return (
        _FORMAT_VERSION_IDENT,
        ":ingestion/watermark",
        _FRONTIER_LOW_IDENT,
        _FRONTIER_HIGH_IDENT,
        _LINEAGE_CONFIRMED_THROUGH_IDENT,
        _CORRECTION_SWEEP_THROUGH_IDENT,
        ":ingestion/last-run-at",
    )


def _index_cross_check_population(db: Any) -> Dict[str, Set[str]]:
    """Every live entity's :entity-type values, read through AEVT.

    Indexes "results" rather than .get()-ing it: an unexpected response shape
    must fail CLOSED (raise), never read as an empty population.
    """
    raw = _db_execute(db, _INDEX_CROSS_CHECK_POPULATION_QUERY)
    population: Dict[str, Set[str]] = {}
    for entity, entity_type in json.loads(raw)["results"]:
        population.setdefault(entity, set()).add(entity_type)
    return population


def _index_cross_check_probe(db: Any, entity_uuid: str) -> Set[str]:
    """One entity's :entity-type values, read through EAVT. Fails closed on an
    unexpected response shape, like _index_cross_check_population."""
    raw = _db_execute(
        db, f'(query [:find ?t :where [#uuid "{entity_uuid}" :entity-type ?t]])'
    )
    return {row[0] for row in json.loads(raw)["results"]}


def _index_damage_message(
    entity_uuid: str, ident: Optional[str], aevt: Set[str], eavt: Set[str]
) -> str:
    name = f"{ident} ({entity_uuid})" if ident else entity_uuid
    return (
        f"Graph index damage: entity {name} has :entity-type {sorted(aevt)} "
        f"through the attribute index (AEVT) but {sorted(eavt)} through the "
        "entity index (EAVT). This is project-minigraf/minigraf#370, an index "
        "left partial by a process killed mid-save. Every entity-bound read "
        "misreads on such a graph, so ingestion refuses to run rather than "
        "write from wrong answers. Nothing repairs it in place: re-running "
        "ingestion and checkpoint() both copy the damaged index forward. "
        "Re-ingest into a FRESH graph path (set MINIGRAF_GRAPH_PATH to a new "
        "file, or delete the existing graph and its .fts.sqlite3 index first)."
    )


def _graph_index_cross_check(
    db: Any,
    sample_size: int = _INDEX_CROSS_CHECK_SAMPLE_SIZE,
    rng: Optional[random.Random] = None,
) -> Dict[str, int]:
    """Refuse a graph whose EAVT and AEVT indexes disagree (#336).

    READ-ONLY, and must be the FIRST read of a run: _graph_format_version_verify
    and _graph_has_ingestion_state are themselves entity-bound reads, so on a
    damaged graph they read the stamp, watermark and frontiers as absent and
    adopt a mature graph as fresh (measured on a real ingested graph: 863
    commits through AEVT, has_state False through EAVT). See
    docs/superpowers/specs/2026-09-11-index-cross-check-design.md.

    Probes every control entity (the fixed idents plus everything carrying a
    control type) and a uniform random sample of the rest. It refuses ONLY
    when exactly one index reads an entity's :entity-type set as EMPTY and the
    other does not: EAVT empty with AEVT non-empty is #370's shape, AEVT empty
    with EAVT non-empty is the shape AEVT damage leaves on a fixed ident. Both
    empty agrees. That catches loss in either index for the entities probed.

    Both non-empty but DIFFERENT is not refused, because a healthy graph
    produces it. An entity given two :entity-type values in ONE transact
    carries two facts sharing (entity, attribute, tx_count, asserted), and
    minigraf keeps only one of them per read: its EAVT/AEVT keys carry no value
    bytes, build_sorted_index_entries sorts each index with sort_unstable_by
    (storage/persistent_facts.rs), and selective_fact_fetch dedups on exactly
    that tuple, keeping whichever fact comes first (query/datalog/executor.rs).
    So each index can return a DIFFERENT single value -- measured 3 of 6
    healthy graphs built through handle_minigraf_transact, e.g. AEVT
    {:type/decision} against EAVT {:type/constraint}. Such entities are
    counted and summarized in one stderr line, never refused, and never
    re-read. Two residuals follow from the same dedup. An entity given two
    same-transaction types that later has ANY of them retracted -- one, or both
    in a single retract, whose retractions share the tuple too -- can read empty
    through one index and a value through the other, and is then refused
    although the graph is healthy. And since refusal needs one side EMPTY, an
    entity holding :entity-type values from DIFFERENT transactions that loses
    some but not all of them, in either index, passes.

    An empty-vs-non-empty disagreement is re-read once before it refuses,
    because call_tool can join this lease and retract a sampled entity between
    scan and probe -- and a false refusal tells the user to discard a healthy
    graph. Only that shape is re-read: it is the only one that can refuse, and
    the re-read is a full population scan. The fresh scan replaces the old one,
    so later entities compare against it and a race costs one rescan, not one
    per entity.

    Returns {"population", "probed", "control_probed"}. "population" is the
    size of the scan the sample was drawn from. A population of 0 is not a
    verification; it is reported so nobody reads it as one (#316's
    code_entities_scanned idiom).
    """
    population = _index_cross_check_population(db)
    population_size = len(population)
    fixed = {
        str(uuid.uuid5(uuid.NAMESPACE_OID, ident)): ident
        for ident in _index_cross_check_fixed_idents()
    }
    control = set(fixed) | {
        entity for entity, types in population.items()
        if types & _INDEX_CROSS_CHECK_CONTROL_TYPES
    }
    rest = sorted(entity for entity in population if entity not in control)
    if len(rest) > sample_size:
        rest = (rng or random.Random()).sample(rest, sample_size)
    selected = sorted(control) + rest
    differing_values = 0
    for entity in selected:
        eavt = _index_cross_check_probe(db, entity)
        aevt = population.get(entity, set())
        if eavt == aevt:
            continue
        if eavt and aevt:
            # Both non-empty: never refused, so a re-read (a full population
            # scan) would buy nothing on what is by construction a healthy run.
            differing_values += 1
            continue
        population = _index_cross_check_population(db)
        aevt = population.get(entity, set())
        eavt = _index_cross_check_probe(db, entity)
        if bool(eavt) != bool(aevt):
            raise GraphIndexDamageError(
                _index_damage_message(entity, fixed.get(entity), aevt, eavt)
            )
        if eavt != aevt:
            differing_values += 1
    if differing_values:
        print(
            f"[_graph_index_cross_check] {differing_values} entities read "
            "different :entity-type values through AEVT and EAVT; not index "
            "damage -- minigraf keeps one of several same-transaction values "
            "per index (not refused)",
            file=sys.stderr,
        )
    return {
        "population": population_size,
        "probed": len(selected),
        "control_probed": len(control),
    }


def _graph_format_version_stamp_if_new(
    db: Any, run_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Stamp a genuinely new graph at the current format version. No-op
    otherwise.

    Must be the FIRST write of a run. A fresh graph that got its ingestion state
    written but not its stamp would, on the next run, look exactly like a
    pre-#263 graph (state present, stamp absent) and be refused -- so this
    cannot be deferred until after the walk.

    Takes index_con so it joins the run's single fact-index session rather than
    falling through _index_write's index_con=None path, which opens and commits
    a connection of its own (pinned by
    TestRunIngestionBatchedIndexWrites.test_ingestion_commits_index_once_per_commit_not_per_triple).

    Writes via the internal _transact helper, never the public
    handle_minigraf_transact handler, matching _watermark_update and
    _frontier_persist_claim. Guarded read-first (#156: a deterministic ident
    does NOT make a re-transact idempotent at the graph level), so calling this
    on an already-stamped graph adds no facts.
    """
    if _graph_format_version_read(db) is not None or _graph_has_ingestion_state(db):
        return
    _transact(
        db,
        f"[[{_FORMAT_VERSION_IDENT} :entity-type :type/ingestion]"
        f" [{_FORMAT_VERSION_IDENT} :ident \"{_FORMAT_VERSION_IDENT}\"]"
        f' [{_FORMAT_VERSION_IDENT} :description "graph format version"]'
        f" [{_FORMAT_VERSION_IDENT} :version {GRAPH_FORMAT_VERSION}]]",
        run_ts_iso,
        index_con=index_con,
    )


def _frontier_seed_from_watermark(
    db: Any, linearization: List[str], run_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """One-time migration: seed :ingestion/frontier-low as [C0, W] tagged
    authoritative from the old scalar :ingestion/watermark. No-op if
    frontier-low already exists or there is no watermark to migrate from
    (see the #222 phase-1 design spec's "Migration" section).
    """
    if _frontier_read_bounds(db, _FRONTIER_LOW_IDENT) is not None:
        return
    watermark_hash = _watermark_query(db)
    if watermark_hash is None or not linearization:
        return
    facts = [
        f"[{_FRONTIER_LOW_IDENT} :entity-type :type/ingest-interval]",
        f'[{_FRONTIER_LOW_IDENT} :lo-hash "{linearization[0]}"]',
        f'[{_FRONTIER_LOW_IDENT} :hi-hash "{_edn_escape(watermark_hash)}"]',
        f"[{_FRONTIER_LOW_IDENT} :tag :authoritative]",
    ]
    # #326: every interval carries the denominator, seeded ones included -- an
    # interval with no :pos-count is treated as untrustworthy downstream, and
    # a migration is exactly the wrong place to manufacture one gratuitously
    # missing case.
    seeded_count = _frontier_span_count(linearization, linearization[0], watermark_hash)
    if seeded_count is not None:
        facts.append(f"[{_FRONTIER_LOW_IDENT} :pos-count {seeded_count}]")
    _transact(db, "[" + " ".join(facts) + "]", run_ts_iso, index_con=index_con)


def _frontier_load(
    db: Any, linearization: List[str], run_ts_iso: str, index_con: Optional[Any] = None
) -> "frontier_registry.FrontierAllocator":
    """Reconstruct a FrontierAllocator from persisted graph facts, migrating
    a pre-#222 watermark-only graph on first load. See the design spec's
    "Migration" and "Graph persistence schema" sections.
    """
    if not linearization:
        return frontier_registry.FrontierAllocator(0, [])

    if (
        _frontier_read_bounds(db, _FRONTIER_LOW_IDENT) is None
        and _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT) is None
    ):
        _frontier_seed_from_watermark(db, linearization, run_ts_iso, index_con=index_con)
    _lineage_confirmed_through_migrate(db, run_ts_iso, index_con=index_con)
    _candidate_diff_purge_legacy(db, index_con=index_con)

    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    intervals: List[frontier_registry.Interval] = []
    low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
    if low_bounds is not None:
        low_lo = hash_to_pos.get(low_bounds[0])
        low_hi = hash_to_pos.get(low_bounds[1])
        low_count = _frontier_read_pos_count(db, _FRONTIER_LOW_IDENT)
        # #222 phase 5 item A. The same three conditions _load_one_interval
        # demands of every PROVISIONAL interval, applied to the authoritative
        # one, which had only the bounds-resolve test. A commit grafted below
        # the forward frontier (#222's "merge grafts old history" edge case)
        # leaves both hash bounds resolving while the SPAN between them grows
        # -- and FrontierAllocator._unclaimed() is the complement of the
        # interval set, so every position inside it is handed to no stream and
        # silently never walked.
        #
        # An interval carrying NO :pos-count is not retained either: "no
        # denominator" and "a denominator that still checks out" must not be
        # the same branch when the failure mode is silent permanent loss.
        # _frontier_pos_count_delta maintains it on the from_low path, so any
        # graph that has taken a forward claim since #326 carries one.
        #
        # Discarded rather than routed through _load_one_interval, which also
        # ARCHIVES a :type/completed-region -- regions are consumed by
        # _skip_claim, which honours PROVISIONAL regions only. The cost of a
        # discard is a forward re-walk from C0: expensive, never lossy.
        if (
            low_lo is not None
            and low_hi is not None
            and low_lo <= low_hi
            and low_count == low_hi - low_lo + 1
        ):
            intervals.append(frontier_registry.Interval(
                low_lo, low_hi,
                frontier_registry.TAG_AUTHORITATIVE, anchor_pos=0, is_base=True,
                ident=_FRONTIER_LOW_IDENT,
            ))
        else:
            _frontier_discard_interval(
                db, _FRONTIER_LOW_IDENT, low_bounds,
                index_con=index_con, pos_count=low_count,
            )
    high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
    if high_bounds is not None:
        high_count = _frontier_read_pos_count(db, _FRONTIER_HIGH_IDENT)
        _load_one_interval(
            db, _FRONTIER_HIGH_IDENT, high_bounds, high_count, hash_to_pos,
            linearization, run_ts_iso, intervals, is_base=True, index_con=index_con,
        )
    for ident, lo_hash, hi_hash, count in _intervals_read_extra(db):
        _load_one_interval(
            db, ident, (lo_hash, hi_hash), count, hash_to_pos,
            linearization, run_ts_iso, intervals, is_base=False, index_con=index_con,
        )
    _frontier_promote_base_if_missing(db, intervals, linearization, run_ts_iso, index_con=index_con)
    # #329: LAST, and after the base promotion -- see _frontier_coalesce_loaded.
    coalesced = _frontier_coalesce_loaded(
        db, linearization, intervals, run_ts_iso, index_con=index_con
    )
    _frontier_check_load_invariants(intervals, strict=coalesced)
    return frontier_registry.FrontierAllocator(len(linearization), intervals)


def _load_one_interval(
    db: Any,
    ident: str,
    bounds: Tuple[str, str],
    pos_count: Optional[int],
    hash_to_pos: Dict[str, int],
    linearization: List[str],
    run_ts_iso: str,
    intervals: List["frontier_registry.Interval"],
    is_base: bool,
    index_con: Optional[Any] = None,
) -> None:
    """Retain, or archive-and-retract, one persisted provisional interval.

    RETAINED iff both bounds resolve in this linearization, lo <= hi, and the
    STORED :pos-count still equals the current span. Dropping the old
    `hi == last position` test is #325's whole point -- new commits landing
    on HEAD no longer force a re-walk of the whole region, only a fresh hole
    above it. Adding the count check is mandatory, not defensive: the old
    retain path performed no count check at all and was safe only by
    accident -- a genuinely new commit implies a new tip, so a commit landing
    strictly INSIDE the old bounds forced `hi != last` and pushed the case
    onto the discard path, where the count check already lived. Retaining
    `hi < last` removes that accident, and an insertion inside a retained
    interval is silent permanent loss: the commit reaches neither the graph
    nor the index, so fact_audit's two witnesses agree, both :introduced-by
    checks only examine entities that exist, and stderr carries nothing.

    An interval carrying NO count is not retained. "No denominator" and "a
    denominator that still checks out" must not be the same branch when the
    failure mode is silent permanent loss -- the fail-safe direction is to
    re-walk.

    "_frontier_persist_claim is the LAST write of a position, so membership in
    this interval proves that position completed" is FALSE as a standalone
    claim -- see _skip_claim's docstring. :lo-hash is a closed RANGE bound, so
    membership is implied by a NEIGHBOUR's claim, not necessarily by the
    position's own: a write that raises takes _run_ingestion's per-commit
    `except`, which continues the descent, and the next lower position that
    succeeds sweeps the failed one into the interval. What makes membership
    mean the position's OWN completion is _run_ingestion's
    `rev_claim_floor` gate (per target ident since #325), which stops
    :lo-hash descending past a position this run failed to complete -- a
    retained (or later archived) interval is only ever as precise as that
    floor made it.

    A region is stored as two hashes but consumed as a closed POSITION RANGE,
    which holds only if the linearization grew by APPENDING above it.
    `git log --topo-order --reverse` gives no such guarantee: it places a new
    commit immediately after its branch point whenever the old tip's line
    stalls behind it -- "branch off an old commit, merge the mainline in,
    fast-forward the mainline" is enough. Such a commit lands INSIDE the
    bounds, is covered by a proof that was never about it, and is skipped.
    That is exactly what the stored :pos-count checksum is for: the count
    MUST come from the interval, never be recomputed here, because a count
    computed from the very span it is then compared against always agrees and
    discriminates nothing (equal count does not by itself prove the same
    member set -- see the design spec's "checksum, not proof of set
    identity" residual -- but it is what stands between this path and the
    #325 tip-growth loss it exists to close).

    Anything not retained is discarded (its facts retracted). Only the
    UNRESOLVABLE-bounds case (the "divergent-ref leak", #325) is also
    ARCHIVED as a :type/completed-region first: a REPRESENTABLE pair that
    fails the retain check failed it on the count, so archiving it here too
    would record a region whose freshly-computed count always matches its own
    (now-wrong) bounds -- the same discriminates-nothing failure the retain
    check exists to avoid. An unresolvable pair carries no position space to
    recompute a count from at all -- routing it through
    _completed_region_record's `order`-driven count would silently produce
    None -- so the archive writes the interval's OWN stored pos_count
    directly instead (#325 review round 2), preserving the CLAIM-time
    denominator as the witness's checksum rather than discarding it.

    That checksum only becomes USABLE again in a run whose linearization
    regains these exact commit hashes -- ordinary history rewrites (rebase,
    amend) never reproduce a prior hash, they only replace it, so in
    practice an archived divergent-ref region is discovered once, discarded,
    and never consulted by _skip_claim again in any two-run scenario. That is
    a real, measured consequence of #325 (not a defect introduced by this
    docstring's fix): the fast path _skip_claim provides stays CORRECT for
    the case it still covers, but that case is narrower after #325 than
    #326 assumed -- see TestDivergentRefEndToEnd's docstring in
    tests/test_mcp_server.py for the full accounting.

    Leaving a non-retained interval's facts in the graph -- what happened to
    an unresolvable pair before this fix -- was NEITHER a discard NOR an
    archive: the facts stayed while no interval loaded, so the next
    _frontier_persist_claim read a non-None `existing` and extended bounds the
    allocator no longer believed in.
    """
    lo_hash, hi_hash = bounds
    lo_pos = hash_to_pos.get(lo_hash)
    hi_pos = hash_to_pos.get(hi_hash)
    tag = ":provisional"
    resolved = lo_pos is not None and hi_pos is not None
    if resolved and lo_pos <= hi_pos and pos_count == hi_pos - lo_pos + 1:
        if is_base:
            anchor_pos = hi_pos
        else:
            # #325: the ident's identity is the anchor hash it was minted
            # from (_interval_ident), never the current bounds -- a
            # provisional interval grows downward, so :hi-hash stays fixed at
            # the anchor under normal claims, but recover it from the ident
            # rather than assuming that, in case a merge ever changes it.
            # Falls back to hi_pos only if the anchor hash itself has dropped
            # out of this linearization -- and #325 review round 3 (Finding
            # 3) is why the ORIGINAL `ident` string is still carried onto the
            # Interval below in that case: anchor_pos=hi_pos is a stand-in
            # for GAP MATH only, and re-deriving _interval_ident from it
            # would mint a DIFFERENT ident than the one actually on disk,
            # creating a second live entity for the same region the moment a
            # later claim persists through the re-derived one instead.
            suffix = ident[len(_INTERVAL_PROVISIONAL_IDENT_PREFIX):]
            anchor_pos = next(
                (pos for h, pos in hash_to_pos.items() if h.startswith(suffix)), hi_pos
            )
        intervals.append(frontier_registry.Interval(
            lo_pos, hi_pos, frontier_registry.TAG_PROVISIONAL,
            anchor_pos=anchor_pos, is_base=is_base, ident=ident,
        ))
        return
    if not resolved and pos_count is not None:
        # #325 review round 2: carry the interval's OWN stored :pos-count
        # through to the archive, rather than routing through
        # _completed_region_record (whose count would be None here -- no
        # order map exists, since at least one bound is absent from this
        # linearization and there is no position space to compute a fresh
        # count from). A None count is exactly what _completed_regions_load
        # refuses to trust: "no denominator" and "a denominator that still
        # checks out" must not be the same branch. This interval's
        # pos_count was written at CLAIM time under whatever linearization
        # these positions were claimed against, and stays a legitimate
        # checksum even though it can no longer be recomputed here -- if
        # the branch ever un-diverges back to these exact hashes, a later
        # run's _completed_regions_load can then trust it (though bounds
        # this specific are only ever resolvable again if the exact same
        # commit hashes reappear, which no ordinary rebase produces).
        #
        # Bypasses _completed_region_record's coalescing machinery
        # entirely, rather than calling it with an explicit count it has
        # no parameter to accept: an unresolvable region has no position
        # space to merge into by definition (verified against order=None
        # per the original Task 4 ruling -- it records fine, just with a
        # count of None). Query-before-write directly, matching the
        # pattern every other write site here uses (#156): this ident is
        # archived at most once in practice, since the discard below
        # retracts the source facts and _load_one_interval is never
        # called again for this ident once they are gone -- but a stray
        # second call must still not duplicate a live datom.
        region_ident = _completed_region_ident(lo_hash, tag)
        existing_region = _db_execute(
            db, f"(query [:find ?lo :where [{region_ident} :lo-hash ?lo]])"
        )
        if not json.loads(existing_region).get("results", []):
            _transact(
                db,
                _completed_region_facts(lo_hash, hi_hash, tag, pos_count=pos_count),
                run_ts_iso,
                index_con=index_con,
            )
    _frontier_discard_interval(
        db, ident, bounds, index_con=index_con, pos_count=pos_count, tag=tag,
    )


def _frontier_promote_base_if_missing(
    db: Any,
    intervals: List["frontier_registry.Interval"],
    linearization: List[str],
    run_ts_iso: str,
    index_con: Optional[Any] = None,
) -> None:
    """#325 review round 3, Finding 1: restore the base invariant ON DISK
    when a run ends with a fragmented provisional side that has no base at
    all.

    Reachable, reproduced against a real graph: a commit landing inside
    frontier-high's span breaks its stored :pos-count, so it is discarded
    (_load_one_interval), while an extra interval ABOVE it is unaffected and
    stays retained. _load_one_interval only ever passes is_base=True for
    :ingestion/frontier-high, so the provisional side then loads with every
    interval is_base=False. frontier_registry._extend cannot self-heal this:
    `is_base = not any(iv.tag == tag for iv in self._intervals)` is False
    while ANY same-tag interval already exists, and _coalesce's survivor
    rule only PRESERVES an existing base, it never MANUFACTURES one. Left
    alone, :ingestion/frontier-high never comes back -- and
    _correction_sweep_select_position returns None on `high_bounds is
    None`, so Stage B never runs again, :ingestion/lineage-confirmed-through
    never advances, and the forward stream is deliberately starved above a
    retained provisional region (#325 review round 2, Ruling 1) -- so
    nothing else ever upgrades that lineage. Provisional :introduced-by
    stays provisional for the life of the graph, silently, on a run that
    reports status: complete.

    The fix lands ON DISK, not just on the returned allocator's in-memory
    intervals: the write dispatch re-mints a claim's persist target from
    interval.is_base/.anchor_pos (_reverse_claim_persist_target) every run,
    so an in-memory-only promotion would still persist through the OLD
    minted ident next time and :ingestion/frontier-high would still never
    reappear on disk.

    Picks the LOWEST retained extra (by lo_pos) -- the one that would have
    become the base under ordinary claiming, had frontier-high not been
    discarded out from under it. Retracts its facts at its OWN (minted)
    ident and rewrites them at the fixed :ingestion/frontier-high ident,
    copying its stored :pos-count VERBATIM -- never recomputed, since
    recomputing would discard the claim-time origin the retain check itself
    depends on (the same "count must come from the interval, never be
    recomputed where it is read" rule _load_one_interval follows).
    Retract-before-transact, the same order every other absorb-then-extend
    write here uses: a crash in between leaves a WINDOW WITH NEITHER entity
    describing the span (the old ident's facts are already gone, the fixed
    ident's are not yet written) -- the span reads unclaimed and is
    re-walked, fail-safe -- rather than a DUPLICATE description, which is
    what transacting first would produce (both entities live at once, each
    still faithful to its own span, and nothing left to notice the
    redundancy once the second write lands).
    """
    provisional = [iv for iv in intervals if iv.tag == frontier_registry.TAG_PROVISIONAL]
    if not provisional or any(iv.is_base for iv in provisional):
        return
    chosen = min(provisional, key=lambda iv: iv.lo_pos)
    old_ident = chosen.ident
    if old_ident is None:
        # Never happens for a loaded extra -- _load_one_interval always sets
        # ident. Defensive only: fail safe (no promotion, re-walked next
        # time via the same missing-base path) rather than promote an
        # interval this function cannot correctly retract.
        return
    lo_hash, hi_hash = linearization[chosen.lo_pos], linearization[chosen.hi_pos]
    pos_count = _frontier_read_pos_count(db, old_ident)
    _frontier_discard_interval(
        db, old_ident, (lo_hash, hi_hash), index_con=index_con,
        pos_count=pos_count, tag=":provisional",
    )
    facts = [
        f"[{_FRONTIER_HIGH_IDENT} :entity-type :type/ingest-interval]",
        f"[{_FRONTIER_HIGH_IDENT} :tag :provisional]",
        f'[{_FRONTIER_HIGH_IDENT} :lo-hash "{_edn_escape(lo_hash)}"]',
        f'[{_FRONTIER_HIGH_IDENT} :hi-hash "{_edn_escape(hi_hash)}"]',
    ]
    if pos_count is not None:
        facts.append(f"[{_FRONTIER_HIGH_IDENT} :pos-count {int(pos_count)}]")
    _transact(db, "[" + " ".join(facts) + "]", run_ts_iso, index_con=index_con)

    idx = next(i for i, iv in enumerate(intervals) if iv is chosen)
    intervals[idx] = frontier_registry.Interval(
        chosen.lo_pos, chosen.hi_pos, frontier_registry.TAG_PROVISIONAL,
        anchor_pos=chosen.hi_pos, is_base=True, ident=_FRONTIER_HIGH_IDENT,
    )


def _frontier_check_load_invariants(
    intervals: List["frontier_registry.Interval"], strict: bool = True
) -> None:
    """#329: what _frontier_load promises its callers about the provisional
    interval set it returns.

    ADJACENT OR OVERLAPPING -> raise (when `strict`). After
    _frontier_coalesce_loaded this is unreachable unless
    frontier_registry.coalesce_intervals or _frontier_persist_merge is
    broken, so it should never fire. A raise here is caught by
    _run_ingestion's run-level `except`: status goes to `error`, the
    traceback reaches fd 2 (so stderr_capture's `error_signals` and
    run_ingestion_benchmark._exit_code fail the at-scale gate), and no walk
    has started, so nothing is half-written. `strict=False` is for the one
    caller path that DELIBERATELY did not merge -- see
    _frontier_coalesce_loaded's ident guard -- where leaving the graph as
    found and re-walking is the fail-safe outcome and a raise would not be.

    BASE NOT LOWEST -> stderr, never a raise. Coalescing does not enforce
    this: two DISJOINT provisional intervals with a real gap between them
    never merge, and _intervals_read_extra's query carries no positional
    predicate, so a below-base extra would load. Nothing produces that state
    today and it degrades conservatively (one more interval a caller
    re-walks or folds defensively, never one silently dropped). Raising on
    it would abort every future run on such a graph forever, with no repair
    path -- turning conservative degradation into permanent denial of
    service, which is worse than the state being guarded against.

    Cross-tag overlap is deliberately NOT checked. It is unreachable
    (claim_low/claim_high are served from _unclaimed, the complement of the
    interval set, so no claim can land inside an interval of ANY tag), and
    adjacency ACROSS tags is the normal converged state -- the
    authoritative/provisional boundary is the lineage frontier, not a
    defect. A third raise would only widen the blast radius of a guard whose
    whole point is to stay quiet.
    """
    prov = sorted(
        (iv for iv in intervals if iv.tag == frontier_registry.TAG_PROVISIONAL),
        key=lambda iv: iv.lo_pos,
    )
    for prev, nxt in zip(prov, prov[1:]):
        if nxt.lo_pos <= prev.hi_pos + 1:
            message = (
                "[_frontier_load] provisional intervals still adjacent or "
                f"overlapping after coalescing: [{prev.lo_pos},{prev.hi_pos}] "
                f"and [{nxt.lo_pos},{nxt.hi_pos}] (#329)"
            )
            if strict:
                raise RuntimeError(message)
            print(message, file=sys.stderr)
            return
    bases = [iv for iv in prov if iv.is_base]
    if bases and bases[0].lo_pos != prov[0].lo_pos:
        print(
            "[_frontier_load] provisional base is not the lowest provisional "
            f"interval: base at [{bases[0].lo_pos},{bases[0].hi_pos}], lowest "
            f"at [{prov[0].lo_pos},{prov[0].hi_pos}] (#329)",
            file=sys.stderr,
        )


def _frontier_persist_merge(
    db: Any,
    linearization: List[str],
    merged: List["frontier_registry.Interval"],
    absorbed: List["frontier_registry.Interval"],
    run_ts_iso: str,
    index_con: Optional[Any] = None,
) -> None:
    """#329: mirror a load-time coalesce onto the graph.

    Idents come from _interval_persist_ident for BOTH sides, so this agrees
    exactly with what the reverse walk's write dispatch
    (_reverse_claim_persist_target) would resolve for the same Interval --
    including #325 Finding 3's case, where a loaded extra's anchor_pos fell
    back to hi_pos and re-deriving _interval_ident would mint a DIFFERENT
    ident than the one actually on disk.

    ORDER: absorbed entities are discarded FIRST, then survivors are
    widened. Same order and same rationale as _frontier_persist_claim's
    absorb-then-extend. A crash between the two leaves the absorbed span
    described by NOBODY -- it reads unclaimed and is re-walked by the next
    _frontier_load, losing nothing. Widening first would risk the DUPLICATE
    outcome: the survivor already claiming the merged span while the
    absorbed entity's now-redundant facts are still live, invisible right up
    until the discard never runs, leaking a phantom into
    _intervals_read_extra forever.

    The survivor's new :pos-count is the merged span, and that is a
    CLAIM-TIME denominator, not #326's computed-where-it-is-read trap. The
    difference is which run does the comparing. Both components were
    validated against THIS run's linearization moments earlier
    (_load_one_interval retains only when the STORED claim-time count still
    equals the current span), and their adjacency was established in that
    same linearization -- so the merged count is a fresh assertion about
    THIS run, compared in a LATER run against a linearization that may
    differ. It discriminates. #326's archive case was different: archiving
    and loading ran in the SAME run against the SAME linearization, so a
    count computed at archive time always agreed.

    ACCEPTED COST: merging is coarser than keeping the components apart. A
    later commit landing inside what used to be the upper component now
    discards the whole union rather than that component alone. Bigger
    re-walk, never a loss. And :pos-count remains a CHECKSUM, not a proof of
    set identity -- the #326 residual applies verbatim to every interval
    this produces.
    """
    tag = ":provisional"
    for iv in absorbed:
        ident = _interval_persist_ident(iv, linearization)
        bounds = _frontier_read_bounds(db, ident)
        # Nothing on disk to retract: an interval minted and merged away
        # within one run before its first claim ever persisted. Skipped
        # rather than treated as an error, matching _frontier_persist_claim.
        if bounds is None:
            continue
        _frontier_discard_interval(
            db, ident, bounds, index_con=index_con,
            pos_count=_frontier_read_pos_count(db, ident), tag=tag,
        )

    for iv in merged:
        if iv.tag != frontier_registry.TAG_PROVISIONAL:
            continue
        ident = _interval_persist_ident(iv, linearization)
        existing = _frontier_read_bounds(db, ident)
        # The survivor's entity is not on disk. Fail safe: the absorbed
        # facts are already gone, so the whole span reads unclaimed and is
        # re-walked, rather than being described by a half-written entity.
        if existing is None:
            continue
        new_lo_hash, new_hi_hash = linearization[iv.lo_pos], linearization[iv.hi_pos]
        if existing == (new_lo_hash, new_hi_hash):
            continue
        to_retract: List[str] = []
        to_transact: List[str] = []
        if existing[0] != new_lo_hash:
            to_retract.append(f'[{ident} :lo-hash "{_edn_escape(existing[0])}"]')
            to_transact.append(f'[{ident} :lo-hash "{_edn_escape(new_lo_hash)}"]')
        if existing[1] != new_hi_hash:
            to_retract.append(f'[{ident} :hi-hash "{_edn_escape(existing[1])}"]')
            to_transact.append(f'[{ident} :hi-hash "{_edn_escape(new_hi_hash)}"]')
        _frontier_pos_count_delta(
            db, ident, iv.hi_pos - iv.lo_pos + 1, to_retract, to_transact,
        )
        if to_retract:
            _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
        if to_transact:
            _transact(db, "[" + " ".join(to_transact) + "]", run_ts_iso, index_con=index_con)


def _frontier_coalesce_loaded(
    db: Any,
    linearization: List[str],
    intervals: List["frontier_registry.Interval"],
    run_ts_iso: str,
    index_con: Optional[Any] = None,
) -> bool:
    """#329: merge contiguous or overlapping LOADED provisional intervals,
    in memory and on disk. Returns whether the set is now guaranteed
    disjoint and non-adjacent (the post-condition's `strict`).

    frontier_registry._coalesce runs only from _extend, and _extend only
    from a claim. FrontierAllocator.__init__ stores what it is handed
    verbatim. So a load producing two adjacent entities WITH AN ALREADY-EMPTY
    GAP never merged: no claim ever happens, and _intervals_read_extra stays
    permanently non-empty -- which makes both
    _correction_sweep_select_position and _should_fold_lineage_watermark
    return early on every subsequent run. Stage B never runs again,
    :ingestion/lineage-confirmed-through never advances, and provisional
    :introduced-by stays provisional for the life of the graph, on runs
    reporting status: complete.

    MUST run after _frontier_promote_base_if_missing: coalesce_intervals'
    survivor rule PRESERVES a base but never manufactures one, so merging
    before the base is restored would leave the union at a minted ident
    while :ingestion/frontier-high stays absent -- the very state that
    function exists to repair.

    Only TAG_PROVISIONAL: _frontier_load appends at most one authoritative
    interval, so that side has no same-tag pair to merge, and the
    authoritative/provisional boundary must survive the two sides becoming
    adjacent.
    """
    provisional = [
        iv for iv in intervals if iv.tag == frontier_registry.TAG_PROVISIONAL
    ]
    if any(iv.ident is None for iv in provisional):
        # Unreachable: _load_one_interval and _frontier_promote_base_if_missing
        # both always set an ident on a loaded interval. Defensive only, and
        # the fail-safe direction is to leave the graph EXACTLY as found and
        # re-walk -- never to retract an entity this function cannot name.
        # The caller drops to strict=False so this does not become a raise.
        print(
            "[_frontier_load] a loaded provisional interval carries no ident; "
            "skipping the load-time coalesce (#329)",
            file=sys.stderr,
        )
        return False
    merged, absorbed = frontier_registry.coalesce_intervals(
        intervals, frontier_registry.TAG_PROVISIONAL
    )
    if absorbed:
        _frontier_persist_merge(
            db, linearization, merged, absorbed, run_ts_iso, index_con=index_con
        )
        intervals[:] = merged
    return True


def _frontier_discard_interval(
    db: Any,
    ident: str,
    bounds: Tuple[str, str],
    index_con: Optional[Any] = None,
    pos_count: Optional[int] = None,
    tag: Optional[str] = None,
) -> None:
    """Retract an interval's persisted facts, mirroring the set
    _frontier_persist_claim creates. Used by _frontier_load when a persisted
    pair cannot faithfully describe the claimed region (see its call site).

    `pos_count` is #326's denominator, retracted with the rest when the caller
    read one. Left behind it would attach to whatever interval the next
    _frontier_persist_claim creates at this ident and license a comparison
    against a span it was never measured from.

    `tag` defaults to None, meaning "derive it the way the two fixed idents
    always have" -- frontier-low is :authoritative, everything else (including
    frontier-high) is :provisional. #325's minted per-anchor idents need the
    caller to say which tag was actually written, since neither fixed ident
    equality holds for them.

    A minted ident (the #325 _INTERVAL_PROVISIONAL_IDENT_PREFIX form) also
    carries a string-valued :ident fact -- unlike frontier-high/-low, which are
    read by fixed ident and never had one -- so its retract set includes it.
    Leaving it live would leak the fact and keep the entity answering
    _intervals_read_extra's enumeration after every other fact is gone.
    """
    if tag is None:
        tag = ":authoritative" if ident == _FRONTIER_LOW_IDENT else ":provisional"
    facts = [
        f"[{ident} :entity-type :type/ingest-interval]",
        f"[{ident} :tag {tag}]",
        f'[{ident} :lo-hash "{_edn_escape(bounds[0])}"]',
        f'[{ident} :hi-hash "{_edn_escape(bounds[1])}"]',
    ]
    if pos_count is not None:
        facts.append(f"[{ident} :pos-count {int(pos_count)}]")
    if ident.startswith(_INTERVAL_PROVISIONAL_IDENT_PREFIX):
        facts.append(f'[{ident} :ident "{_edn_escape(ident)}"]')
    _retract(db, "[" + " ".join(facts) + "]", index_con=index_con)


_COMPLETED_REGION_ENTITY_TYPE = ":type/completed-region"


def _completed_region_ident(lo_hash: str, tag: str) -> str:
    """Deterministic ident for the archived `tag` region starting at lo_hash.

    The TAG is part of the ident, not just of the fact set. Two regions sharing
    a low hash but differing in tag would otherwise collide onto one entity and
    _completed_regions_read's join would return their CROSS PRODUCT -- including
    a (lo, hi, tag) triple that was never recorded. In the concrete case
    (":provisional" [h1,h4] plus ":authoritative" [h1,h9]) the phantom row is a
    provisional region LARGER than anything ever proven, which _skip_claim would
    honour. Only :provisional is archived today, but the tag is stored and
    checked precisely so a future authoritative discard cannot silently license
    a forward skip, and the collision does exactly the opposite.

    Not a public schema type -- :type/completed-region is deliberately absent
    from MINIGRAF_SCHEMA, so handle_minigraf_audit's registered-type loop never
    scans for it (same status as :type/ingest-interval). Every write below goes
    through the internal _transact/_retract helpers; the public handler's
    _validate_facts would reject an unregistered type outright.
    """
    return f":ingestion/completed-region-{tag.lstrip(':')}-{lo_hash[:12]}"


def _completed_regions_read_full(db: Any) -> List[Tuple[str, str, str, Optional[int]]]:
    """Every archived completed region, as (lo_hash, hi_hash, tag, pos_count),
    sorted.

    Binds ?ident rather than ?e. `[?e :entity-type :type/completed-region]`
    answers in UUID space -- _count_commit_entities gets away with that pattern
    only because it counts and never reads ?e back. The string-valued :ident
    fact each region carries is what makes this enumeration (and the retract in
    _completed_region_record) work without UUID-to-ident resolution.

    :pos-count is read by a SECOND query rather than being joined into the
    first. A region that carries no :pos-count is untrustworthy but must still
    be enumerable -- it has to be retractable and carryable -- and a single
    five-way join would make it invisible instead, leaking its facts forever.
    """
    raw = _db_execute(
        db,
        "(query [:find ?ident ?lo ?hi ?tag :where"
        f" [?e :entity-type {_COMPLETED_REGION_ENTITY_TYPE}]"
        " [?e :ident ?ident] [?e :lo-hash ?lo] [?e :hi-hash ?hi] [?e :tag ?tag]])",
    )
    raw_counts = _db_execute(
        db,
        "(query [:find ?ident ?c :where"
        f" [?e :entity-type {_COMPLETED_REGION_ENTITY_TYPE}]"
        " [?e :ident ?ident] [?e :pos-count ?c]])",
    )
    counts: Dict[str, Optional[int]] = {}
    for ident, c in json.loads(raw_counts).get("results", []):
        try:
            counts[str(ident)] = int(c)
        except (TypeError, ValueError):
            counts[str(ident)] = None

    seen = set()
    out: List[Tuple[str, str, str, Optional[int]]] = []
    for ident, lo, hi, tag in json.loads(raw).get("results", []):
        key = (lo, hi, str(tag))
        if key not in seen:
            seen.add(key)
            out.append((lo, hi, str(tag), counts.get(str(ident))))
    return sorted(out, key=lambda r: (r[0], r[1], r[2]))


def _completed_regions_read(db: Any) -> List[Tuple[str, str, str]]:
    """Every archived completed region, as (lo_hash, hi_hash, tag), sorted.

    The bounds-only view of _completed_regions_read_full, for callers and tests
    that assert on the region set itself rather than on its stored denominator.
    """
    return [(lo, hi, tag) for lo, hi, tag, _count in _completed_regions_read_full(db)]


def _completed_region_facts(
    lo_hash: str, hi_hash: str, tag: str, pos_count: Optional[int] = None
) -> str:
    ident = _completed_region_ident(lo_hash, tag)
    facts = [
        f"[{ident} :entity-type {_COMPLETED_REGION_ENTITY_TYPE}]",
        f'[{ident} :ident "{ident}"]',
        f'[{ident} :lo-hash "{_edn_escape(lo_hash)}"]',
        f'[{ident} :hi-hash "{_edn_escape(hi_hash)}"]',
        f"[{ident} :tag {tag}]",
    ]
    # #326: the region's position-count denominator, absent only when the
    # caller had no position map to compute it from. See
    # _completed_regions_load for why an absent count is untrustworthy.
    if pos_count is not None:
        facts.append(f"[{ident} :pos-count {int(pos_count)}]")
    return "[" + " ".join(facts) + "]"


def _completed_region_record(
    db: Any,
    lo_hash: str,
    hi_hash: str,
    tag: str,
    run_ts_iso: str,
    index_con: Optional[Any] = None,
    order: Optional[Dict[str, int]] = None,
) -> None:
    """Archive [lo_hash, hi_hash] as a completed region, coalescing it into the
    existing same-tag set.

    #325 final review: this function currently has ZERO production call
    sites (`grep -n '_completed_region_record(' mcp_server.py` matches only
    this def). The one place that archives an unresolvable-bounds region
    (`_load_one_interval`, the `not resolved and pos_count is not None`
    branch) writes `_completed_region_facts` directly behind its own
    query-before-write guard instead of calling here, precisely because that
    case has no position space to coalesce into (see that branch's own
    comment). So the coalescing/pruning machinery below is exercised only by
    tests today, and unresolvable-bounds regions accumulate on disk without
    bound -- one entity per divergence event, never merged, never retracted.
    Not a bug to fix here: recorded so a future caller of this function (or a
    decision to prune those regions another way) does not have to
    rediscover the gap first.

    Query-before-write, the guard _watermark_update and _frontier_persist_claim
    established: a deterministic ident only guarantees repeated writes target
    the SAME entity, it does not stop minigraf creating a duplicate live datom
    for a re-transact at a new valid-from (#156). The current set is read first
    and only the difference is written -- a call that changes nothing writes
    nothing.

    `order` maps hash -> position for the current linearization. Regions can
    only be compared (and therefore coalesced) when both endpoints are
    orderable; a region whose hashes are not in `order` is kept as-is and never
    merged. Callers inside a run pass the linearization's map; the coalescing
    tests pass a lexicographic fallback via order=None, which sorts by hash.

    Each written region also carries `:pos-count`, the number of positions its
    bounds spanned under `order` -- the denominator _completed_regions_load
    re-checks before believing the region covers anything. A merged region's
    count is recomputed against the current `order`; a region carried through
    unmerged keeps the stored count it was archived with.

    A region whose stored count no longer matches its current span is dropped
    from the MERGE (it can no longer manufacture coverage) but is NOT
    retracted, matching _completed_regions_load, the design spec and #326
    Finding D. Retracting it would destroy the witness permanently over a
    branch that may straighten out later; leaving it costs one untrusted entity
    and no skip, because both this function and the load path re-check the
    denominator every time.

    Orderability is tested per REGION, not per hash: `order` may cover only
    some of the hashes in play (e.g. a caller passes the map for its own two
    endpoints but an unrelated archived region's hashes have since fallen out
    of `order`). Comparing an int position key against a raw string key raises
    TypeError, so a region with even one endpoint missing from `order` is
    partitioned out of the merge entirely and passed through to the target set
    untouched -- including the newly-recorded region itself, which becomes a
    standalone entry rather than merging into anything.
    """
    def region_orderable(lo: str, hi: str) -> bool:
        return order is None or (lo in order and hi in order)

    def key(h: str) -> Any:
        return order[h] if order is not None else h

    def count_of(lo: str, hi: str) -> Optional[int]:
        if order is not None and lo in order and hi in order:
            return order[hi] - order[lo] + 1
        return None

    current = _completed_regions_read_full(db)
    other_tag = [r for r in current if r[2] != tag]

    # #326 FIX: drop stale-denominator regions BEFORE the merge, never after.
    # The merge compares regions archived under DIFFERENT linearizations using
    # CURRENT positions, so a stale region left in the candidate set can
    # manufacture coverage over positions that were in neither original
    # region. This is _completed_regions_load's guard one level up, and it has
    # to be ordered ahead of the coalescing or the merge re-introduces the hole
    # the load-time guard exists to close.
    same_tag: List[Tuple[str, str]] = []
    stale_count_regions: List[Tuple[str, str]] = []
    for lo, hi, t, stored in current:
        if t != tag:
            continue
        live = count_of(lo, hi)
        if live is not None and live != stored:
            # Positions shifted unevenly under it: not a proof any more. Kept
            # OUT OF THE MERGE, but kept in the graph -- see the passthrough
            # below.
            stale_count_regions.append((lo, hi))
            continue
        same_tag.append((lo, hi))

    candidates = same_tag + [(lo_hash, hi_hash)]
    orderable_regions = sorted(
        {r for r in candidates if region_orderable(*r)}, key=lambda p: key(p[0])
    )
    non_orderable_regions = sorted({r for r in candidates if not region_orderable(*r)})

    merged: List[Tuple[str, str]] = []
    for lo, hi in orderable_regions:
        if merged and key(lo) <= key(merged[-1][1]):
            prev_lo, prev_hi = merged[-1]
            merged[-1] = (prev_lo, hi if key(hi) > key(prev_hi) else prev_hi)
        else:
            merged.append((lo, hi))

    stored_counts = {(lo, hi, t): c for lo, hi, t, c in current}
    target: List[Tuple[str, str, str, Optional[int]]] = []
    for lo, hi in merged:
        # A merged (or freshly recorded) region's denominator is computed
        # against the CURRENT order, which is the linearization the merge was
        # decided under.
        target.append((lo, hi, tag, count_of(lo, hi)))
    for lo, hi in non_orderable_regions:
        # Carried through untouched, so its stored denominator is carried too:
        # it describes the linearization the region was archived under, which
        # this call knows nothing about.
        target.append((lo, hi, tag, stored_counts.get((lo, hi, tag))))
    # #326 Finding D: a stale-denominator region is DROPPED FROM THE MERGE but
    # KEPT IN THE GRAPH, which is what _completed_regions_load's docstring and
    # the design spec both say happens to it. Falling out of `target` here
    # would have sent it to the retract loop below instead -- destroying the
    # witness for good over a branch that may yet straighten out. Note the
    # ordering with the unmappable case: an endpoint missing from `order` is
    # the STRONGER symptom of the same thing (the branch moved under us) and is
    # already carried through, so treating the weaker symptom more harshly was
    # incoherent as well as undocumented. A region already represented in
    # `target` is skipped: that entry has a count computed against the CURRENT
    # order, which is the better one.
    #
    # #326 Finding (post-214d8fc): dedup on the IDENT, not the (lo, hi) BOUNDS.
    # _completed_region_ident is keyed on (lo_hash, tag) ONLY -- two regions
    # sharing a low hash but differing in `hi` render onto the SAME entity
    # regardless of their bounds. A bounds-keyed passthrough check lets a
    # stale [A, T1] slip past a fresh [A, T2] (same lo, different hi) and both
    # get appended to `target`, so the write loop below transacts both fact
    # sets onto one ident -- the entity ends up with two :hi-hash values and
    # two :pos-count values, and the count read back is whichever one
    # last-write-wins picks, nondeterministically across runs.
    #
    # Dropping the stale region here in that case is safe and is NOT the
    # witness-destroying drop Finding D guards against: the ident survives in
    # `target` via the other (surviving) entry, and the write loop below
    # retracts the stale entry's old facts and re-transacts the surviving
    # entry's in the same call -- retract-then-transact, exactly what the
    # pre-#326 code did for a single region at one ident. What would destroy
    # the witness is dropping an ident out of `target` ENTIRELY, which this
    # does not do.
    passthrough_idents = {_completed_region_ident(lo, tag) for lo, _hi, _t, _c in target}
    for lo, hi in stale_count_regions:
        if _completed_region_ident(lo, tag) in passthrough_idents:
            continue
        target.append((lo, hi, tag, stored_counts.get((lo, hi, tag))))
    target.extend(other_tag)
    target = sorted(target, key=lambda r: (r[0], r[1], r[2]))

    if target == current:
        return

    target_keys = {(lo, hi, t) for lo, hi, t, _c in target}
    current_keys = {(lo, hi, t): c for lo, hi, t, c in current}
    for lo, hi, t, c in current:
        if (lo, hi, t) not in target_keys:
            _retract(db, _completed_region_facts(lo, hi, t, c), index_con=index_con)
    for lo, hi, t, c in target:
        if (lo, hi, t) not in current_keys:
            _transact(
                db, _completed_region_facts(lo, hi, t, c), run_ts_iso, index_con=index_con
            )
        elif current_keys[(lo, hi, t)] != c:
            # Same bounds, different denominator (a re-record under a grown
            # linearization). Move only the :pos-count fact -- #156 means a
            # re-transact of the whole set at a new valid-from would duplicate
            # every live datom rather than being a no-op.
            ident = _completed_region_ident(lo, t)
            old = current_keys[(lo, hi, t)]
            if old is not None:
                _retract(db, f"[[{ident} :pos-count {int(old)}]]", index_con=index_con)
            if c is not None:
                _transact(
                    db, f"[[{ident} :pos-count {int(c)}]]", run_ts_iso, index_con=index_con
                )


_REGION_TAG_TO_FRONTIER_TAG = {
    ":provisional": frontier_registry.TAG_PROVISIONAL,
    ":authoritative": frontier_registry.TAG_AUTHORITATIVE,
}


def _completed_regions_load(
    db: Any,
    linearization: List[str],
    allocator: "frontier_registry.FrontierAllocator",
    index_con: Optional[Any] = None,
) -> List["frontier_registry.Interval"]:
    """Archived regions mapped into this run's position space, pruned.

    Kept separate from _frontier_load on purpose: the ARCHIVING has to live
    there (that is where the doomed bounds are), but widening _frontier_load's
    return to a tuple would break roughly a dozen call sites and tests that use
    its result directly as an allocator, for no gain.

    Two ways a region leaves the set, and they are not the same:
      * fully covered by a live SAME-TAG interval -- redundant, so the facts are
        RETRACTED as well, or the set grows by one per run forever;
      * an endpoint not in this linearization -- the branch moved under us, so
        it is dropped from the returned list but its facts are KEPT. Dropping
        costs a re-walk; retracting would destroy the witness for good.

    The stored :pos-count is the third way, and it is what makes the
    hash-to-position mapping defensible rather than merely well-defined. A region
    is persisted as two hashes but consumed as a CLOSED POSITION RANGE: every
    position between the bounds is treated as proven-complete. That is only
    true if the linearization grew by APPENDING above the region.
    `git log --topo-order --reverse` gives no such guarantee -- it places a new
    commit immediately after its branch point whenever the old tip's line
    stalls behind it, which is exactly what "branch off an old commit, merge
    master in, fast-forward master" produces. Such a commit lands INSIDE the
    archived bounds, is covered by a proof that was never about it, is skipped,
    and is lost permanently and silently: it is written to neither the graph
    nor the index, so fact_audit's two witnesses agree, both :introduced-by
    checks only examine entities that exist, and stderr carries nothing.

    Pure tip growth shifts both endpoints by the same amount, so the position
    COUNT is preserved and there are no false drops. An insertion inside the
    range breaks the count, and the region falls back to no region at all -- a
    full re-walk, which is correct and merely slower. This is #316's
    `code_entities_scanned` idiom: a stored denominator makes every run
    re-prove its own positive control instead of trusting the day it was
    written.

    A region carrying NO stored count is dropped for the same reason, not
    trusted. There are none in the wild (nothing released has ever written a
    :type/completed-region fact), but "no denominator" and "a denominator that
    still checks out" must not be the same branch: an absent count cannot
    distinguish an append from an insertion, and the fail-safe direction for a
    predicate whose failure mode is permanent data loss is to re-walk.

    Dropped-for-count regions are NOT retracted, for the same reason an
    unmappable one is not: the branch may straighten out on a later run.
    _completed_region_record holds to the same rule (#326 Finding D) -- it drops
    such a region from the MERGE but leaves its facts in the graph.

    RESIDUAL, stated rather than papered over (#326 Finding B): the count is a
    CHECKSUM, not a proof of set identity. Equal count does not imply the same
    member set -- an old commit inside the range that is neither ancestor nor
    descendant of `lo` could be reordered below `lo` by a later topo-order while
    a new commit lands inside, leaving the count unchanged and this region
    trusted. A real repository realizing that was NOT constructed; git's
    tie-breaking constrains which of the valid topological orders it actually
    emits. So it is an undemonstrated residual, not a measured loss -- but it is
    a residual, and the word "sound" (which this docstring used) overstates what
    a checksum can do. Closing it takes a per-position completion marker
    (approach B in the design spec), at one fact per commit on the write path
    this feature exists to make cheaper.
    """
    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    out: List["frontier_registry.Interval"] = []
    for lo_hash, hi_hash, tag, pos_count in _completed_regions_read_full(db):
        frontier_tag = _REGION_TAG_TO_FRONTIER_TAG.get(str(tag))
        if frontier_tag is None:
            continue
        if lo_hash not in hash_to_pos or hi_hash not in hash_to_pos:
            continue
        lo_pos, hi_pos = hash_to_pos[lo_hash], hash_to_pos[hi_hash]
        if lo_pos > hi_pos:
            continue
        covered = any(
            iv.tag == frontier_tag and iv.lo_pos <= lo_pos and hi_pos <= iv.hi_pos
            for iv in allocator.intervals()
        )
        if covered:
            _retract(
                db,
                _completed_region_facts(lo_hash, hi_hash, str(tag), pos_count),
                index_con=index_con,
            )
            continue
        if pos_count is None or hi_pos - lo_pos + 1 != pos_count:
            continue
        out.append(frontier_registry.Interval(lo_pos, hi_pos, frontier_tag))
    return sorted(out, key=lambda iv: iv.lo_pos)


def _skip_claim(
    tag: str, pos: int, regions: Sequence["frontier_registry.Interval"]
) -> bool:
    """True iff this claim can be retired without parsing or writing the commit.

    Sound because of what it does NOT read. The witness is membership in an
    archived completed region, whose bounds came from a persisted frontier
    interval. A #313 torn position's claim never persisted, so that position was
    never inside the interval that got archived, so it is in no region and is
    never skipped -- correct by construction rather than by care.

    An earlier version of this docstring justified that with "
    _frontier_persist_claim is the LAST write of a position, so membership in a
    persisted interval proves that position completed". **That is FALSE as
    written** (#326 Finding A). :lo-hash is a closed RANGE bound, so membership
    is implied by a NEIGHBOUR's claim, not by the position's own: a write that
    RAISES takes _run_ingestion's per-commit `except`, which continues the
    descent, and the next lower position that succeeds sweeps the failed one
    into the interval. #313 is safe only because a SIGKILL stops the process.
    What makes membership mean the position's own completion is the floor gate
    in _run_ingestion (`rev_claim_floor`, keyed per target ident since #325 --
    a single run-global scalar stopped discriminating once one run could serve
    more than one gap), which stops :lo-hash descending past a position this
    run failed to complete.

    The other guard is a CHECKSUM, not a proof of set identity: the stored
    :pos-count catches a linearization whose span changed, but equal count does
    not imply the same member set. See _completed_regions_load for the residual
    that leaves.

    'fwd' never skips: see TestSkipClaim's forward case for the two independent
    reasons. Restricting to same-tag skipping gets both from one clause.
    """
    if tag != "rev":
        return False
    return any(
        iv.tag == frontier_registry.TAG_PROVISIONAL and iv.lo_pos <= pos <= iv.hi_pos
        for iv in regions
    )


def _frontier_persist_claim(
    db: Any,
    linearization: List[str],
    pos: int,
    from_low: bool,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
    ident: Optional[str] = None,
    absorbed_idents: Optional[List[str]] = None,
) -> None:
    """Persist a single claimed position by extending the correct interval
    fact -- retracts+reasserts only the moved bound, mirroring
    _watermark_update's per-commit cost profile (see the design spec's
    "Persistence timing" and "Graph persistence schema" sections).

    `ident` defaults to today's fixed low/high choice (_FRONTIER_LOW_IDENT /
    _FRONTIER_HIGH_IDENT), so every pre-#325 call site and test keeps writing
    exactly what it always has. #325's reverse walk instead names a specific
    provisional interval entity (_interval_ident) when the claim belongs to
    one already tracked separately from frontier-high.

    `absorbed_idents` names entities this claim's merge swallows -- the
    interval named by `ident` and one or more others have just become
    contiguous, and the others stop existing as their own entities. They are
    retracted BEFORE `ident`'s bound is extended: a crash between the two
    then leaves a WINDOW WITH NEITHER entity describing the absorbed span
    (the absorbed entity's facts are already retracted, `ident`'s bound has
    not yet been extended over that span) -- unclaimed and re-walked by the
    next _frontier_load, nothing lost. Extending first would instead risk the
    DUPLICATE outcome: a crash between the two would leave `ident` already
    claiming the full merged span while the absorbed entity's now-redundant
    facts are still live -- and a crash there is invisible right up until
    _frontier_discard_interval never runs, silently leaking a phantom entity
    into _intervals_read_extra forever.

    #325 review Finding 1: a merging claim's survivor takes the UNION of its
    own existing bounds, every absorbed interval's bounds, and the claimed
    position -- never just the one bound the non-merging branch below moves.
    An earlier version of this function moved only that one bound on a merge
    too, which retracted the absorbed entity's facts while its span ended up
    described by NOBODY: measured producing an inverted survivor with a
    NEGATIVE :pos-count (`('h7','h5')`, count -1) from claims that had built
    `('h4','h5')` and `('h8','h9')` before the merge. `_frontier_load`'s
    `lo <= hi` guard happens to catch that specific shape today by discarding
    frontier-high outright -- but that throws away the completion witness for
    the WHOLE interval, which is exactly the loss #326's archiving exists to
    prevent, and an absorbed interval BELOW the survivor is not caught at all
    ([100,120] absorbing [50,98] via a claim at 99 would silently produce
    [99,120], losing the proven [50,98] region with no inversion to trip the
    guard).

    `_coalesce`'s same-tag filter (frontier_registry.py) is what makes
    passing the survivor's own `tag` to every absorbed entity's discard
    correct -- a merge can only ever absorb an interval carrying the SAME tag
    as the survivor. If that invariant is ever relaxed, a mismatched `:tag`
    literal here matches nothing on retract, leaving a live `:tag` fact on an
    entity whose `:entity-type` and `:ident` are already gone -- silently,
    since `_intervals_read_extra` can never surface it again to notice.
    """
    if ident is None:
        ident = _FRONTIER_LOW_IDENT if from_low else _FRONTIER_HIGH_IDENT
    tag = ":authoritative" if from_low else ":provisional"

    absorbed_bounds_list: List[Tuple[str, str]] = []
    for absorbed in absorbed_idents or []:
        absorbed_bounds = _frontier_read_bounds(db, absorbed)
        # An absorbed ident can name an interval that was minted and merged
        # away again within the same run before its first claim ever
        # persisted -- there is nothing on disk to retract or fold into the
        # union below, so it is skipped rather than treated as an error.
        if absorbed_bounds is not None:
            absorbed_bounds_list.append(absorbed_bounds)
            _frontier_discard_interval(
                db, absorbed, absorbed_bounds, index_con=index_con,
                pos_count=_frontier_read_pos_count(db, absorbed), tag=tag,
            )

    moved_hash = linearization[pos]
    existing = _frontier_read_bounds(db, ident)

    to_retract: List[str] = []
    to_transact: List[str] = []
    # #325 review round 2: branch on `absorbed_idents` (what the CALLER says
    # merged), never on `absorbed_bounds_list` (what survived the phantom
    # filter above). A merge whose absorbed interval was minted and coalesced
    # away again within the SAME _extend() call -- degenerate from birth, so
    # it never got an independent claim to persist -- leaves
    # absorbed_bounds_list empty while absorbed_idents is not, and the two
    # used to be treated as the same signal. They are not: falling through to
    # the plain single-bound-move branch below then MOVES the wrong bound
    # (assuming the claim is adjacent to the survivor's own lo, which is false
    # whenever the claim actually landed on the far side of a gap and merged
    # through a same-call phantom) and silently INVERTS the pair -- measured
    # producing exactly `(hash-at-9, hash-at-8)` for a claim at position 9
    # against a base spanning [6,8], from TestMultiStreamParityWithForwardOnly.
    # A phantom absorbed interval's span is always exactly the claimed
    # position itself (it was created AND absorbed by that one claim, with no
    # independent history), so `moved_hash` already covers it in the union
    # below -- no special contribution needed, only the branch choice matters.
    if absorbed_idents:
        # #325: the merged span is the union of the claimed position, the
        # survivor's own current bounds (if it already existed), and every
        # absorbed interval's bounds -- see the docstring's Finding 1 note
        # for what moving only one bound produced instead.
        candidate_hashes = [moved_hash]
        if existing is not None:
            candidate_hashes.extend(existing)
        for absorbed_lo, absorbed_hi in absorbed_bounds_list:
            candidate_hashes.extend((absorbed_lo, absorbed_hi))
        positions = [linearization.index(h) for h in candidate_hashes]
        new_lo_hash = linearization[min(positions)]
        new_hi_hash = linearization[max(positions)]

        if existing is None:
            to_transact.append(f"[{ident} :entity-type :type/ingest-interval]")
            to_transact.append(f"[{ident} :tag {tag}]")
        else:
            lo_hash, hi_hash = existing
            if lo_hash != new_lo_hash:
                to_retract.append(f'[{ident} :lo-hash "{_edn_escape(lo_hash)}"]')
            if hi_hash != new_hi_hash:
                to_retract.append(f'[{ident} :hi-hash "{_edn_escape(hi_hash)}"]')
        to_transact.append(f'[{ident} :lo-hash "{_edn_escape(new_lo_hash)}"]')
        to_transact.append(f'[{ident} :hi-hash "{_edn_escape(new_hi_hash)}"]')
        if existing is None and ident.startswith(_INTERVAL_PROVISIONAL_IDENT_PREFIX):
            to_transact.append(f'[{ident} :ident "{_edn_escape(ident)}"]')
        new_count: Optional[int] = _frontier_span_count(linearization, new_lo_hash, new_hi_hash)
    elif existing is None:
        to_transact.append(f"[{ident} :entity-type :type/ingest-interval]")
        to_transact.append(f"[{ident} :tag {tag}]")
        to_transact.append(f'[{ident} :lo-hash "{_edn_escape(moved_hash)}"]')
        to_transact.append(f'[{ident} :hi-hash "{_edn_escape(moved_hash)}"]')
        # #325: a minted ident (_INTERVAL_PROVISIONAL_IDENT_PREFIX's form)
        # needs its own string-valued :ident fact so _intervals_read_extra
        # can enumerate it -- frontier-low/-high are read by fixed ident and
        # never carry one, and adding one to them would make them show up in
        # that enumeration too, breaking its "frontier-high is not extra"
        # contract.
        if ident.startswith(_INTERVAL_PROVISIONAL_IDENT_PREFIX):
            to_transact.append(f'[{ident} :ident "{_edn_escape(ident)}"]')
        new_count = 1
    else:
        lo_hash, hi_hash = existing
        if from_low:
            to_retract.append(f'[{ident} :hi-hash "{_edn_escape(hi_hash)}"]')
            to_transact.append(f'[{ident} :hi-hash "{_edn_escape(moved_hash)}"]')
            new_count = _frontier_span_count(linearization, lo_hash, moved_hash)
        else:
            to_retract.append(f'[{ident} :lo-hash "{_edn_escape(lo_hash)}"]')
            to_transact.append(f'[{ident} :lo-hash "{_edn_escape(moved_hash)}"]')
            new_count = _frontier_span_count(linearization, moved_hash, hi_hash)

    # #326: the interval's position-count denominator, written in the SAME
    # transact as the bound it belongs to. It has to be recorded HERE, at claim
    # time, and not later where the interval is archived: the count is a
    # statement about THIS run's linearization, and _frontier_load's archiving
    # branch runs against a linearization that has already changed -- which is
    # precisely the change that has to be detected. A count computed at archive
    # time always agrees with the span it was computed from and discriminates
    # nothing.
    _frontier_pos_count_delta(db, ident, new_count, to_retract, to_transact)

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)


def _frontier_pos_count_delta(
    db: Any,
    ident: str,
    new_count: Optional[int],
    to_retract: List[str],
    to_transact: List[str],
) -> None:
    """Append the :pos-count moves for `ident` onto an in-progress bound write.

    Batched with the bound triples rather than written separately: the two
    carry DIFFERENT attributes, so minigraf#287 (a batch sharing
    (entity, attribute, valid_from) keeps only the last value) does not reach
    them, and folding them in keeps the per-commit persist at the same two DB
    calls it has always cost.

    Never leaves a stale count behind: if the new span is unknowable (a bound
    absent from this linearization) the old count is retracted and none is
    written, so the interval reads as carrying no denominator -- the fail-safe
    state, which downstream treats as "do not trust this interval".
    """
    old_count = _frontier_read_pos_count(db, ident)
    if old_count == new_count:
        return
    if old_count is not None:
        to_retract.append(f"[{ident} :pos-count {int(old_count)}]")
    if new_count is not None:
        to_transact.append(f"[{ident} :pos-count {int(new_count)}]")


def _frontier_persist_span(
    db: Any,
    linearization: List[str],
    lo_pos: int,
    hi_pos: int,
    from_low: bool,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
    ident: Optional[str] = None,
) -> None:
    """Persist a whole claimed SPAN in one write, for #326's end-of-walk flush.

    _frontier_persist_claim cannot do this job. After a discard the interval's
    facts are gone, so its `existing is None` branch writes lo == hi ==
    moved_hash: the interval collapses to a point and the top bound is lost.

    `ident` defaults to today's fixed low/high choice, same rationale as
    _frontier_persist_claim's: every pre-#325 call site keeps flushing
    frontier-low/-high unchanged, and #325's walk names a specific
    provisional interval entity when the flush belongs to one.

    Advance-only in both directions -- the flush is bookkeeping catching up with
    the allocator, and must never retreat a bound a real per-commit claim
    already moved. A span that changes nothing writes nothing (#156: a
    re-transact at a new valid-from is not a graph-level no-op).
    """
    if ident is None:
        ident = _FRONTIER_LOW_IDENT if from_low else _FRONTIER_HIGH_IDENT
    tag = ":authoritative" if from_low else ":provisional"
    lo_hash, hi_hash = linearization[lo_pos], linearization[hi_pos]
    existing = _frontier_read_bounds(db, ident)

    if existing is None:
        facts = [
            f"[{ident} :entity-type :type/ingest-interval]",
            f"[{ident} :tag {tag}]",
            f'[{ident} :lo-hash "{_edn_escape(lo_hash)}"]',
            f'[{ident} :hi-hash "{_edn_escape(hi_hash)}"]',
            f"[{ident} :pos-count {hi_pos - lo_pos + 1}]",
        ]
        # #325: same rule _frontier_persist_claim follows -- a minted ident
        # needs its own :ident fact to be enumerable via _intervals_read_extra;
        # frontier-low/-high never get one.
        if ident.startswith(_INTERVAL_PROVISIONAL_IDENT_PREFIX):
            facts.append(f'[{ident} :ident "{_edn_escape(ident)}"]')
        _transact(
            db,
            "[" + " ".join(facts) + "]",
            commit_ts_iso,
            index_con=index_con,
        )
        return

    pos_of = {h: i for i, h in enumerate(linearization)}
    cur_lo, cur_hi = existing
    new_lo = cur_lo if pos_of.get(cur_lo, lo_pos) <= lo_pos else lo_hash
    new_hi = cur_hi if pos_of.get(cur_hi, hi_pos) >= hi_pos else hi_hash

    to_retract: List[str] = []
    to_transact: List[str] = []
    if new_lo != cur_lo:
        to_retract.append(f'[{ident} :lo-hash "{_edn_escape(cur_lo)}"]')
        to_transact.append(f'[{ident} :lo-hash "{_edn_escape(new_lo)}"]')
    if new_hi != cur_hi:
        to_retract.append(f'[{ident} :hi-hash "{_edn_escape(cur_hi)}"]')
        to_transact.append(f'[{ident} :hi-hash "{_edn_escape(new_hi)}"]')
    # #326: same denominator the per-claim path maintains, kept in step with
    # the bounds this flush moved.
    _frontier_pos_count_delta(
        db, ident, _frontier_span_count(linearization, new_lo, new_hi),
        to_retract, to_transact,
    )
    if not to_transact:
        if to_retract:
            _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
        return
    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)


_LINEAGE_MARKER_ENTITY_TYPE = ":type/lineage-marker"


def _lineage_marker_ident(entity_ident: str) -> str:
    """Deterministic companion-entity ident for entity_ident's provisional
    marker. Not a public schema type -- see the #222 phase 2a design spec's
    "Schema/audit status of new entity types" section.

    Collapses every '/' in entity_ident to '-', so this is injective only
    because every real caller passes a `_code_ident`-produced ident, which
    always carries exactly one '/' (`_canonical_ident` slugs the value before
    joining it to the type prefix, so no '/' from the source path can survive
    into the ident body). Single-slash alone is NOT sufficient, though: the
    collapse also needs the TYPE PREFIX to be hyphen-free, so the one '/' sits
    at a fixed boundary the '-' it becomes cannot be confused with. With a
    hyphenated prefix, `:a-b/c` and `:a/b-c` both collapse to
    `:lineage/a-b-c`. That holds today because `_code_ident` is only ever
    called with the literals module/function/class/variable/field (the
    category loops iterate exactly those four, and renamed_pairs' categories
    come from the same pools) -- a hyphenated code entity type would break it.
    Both properties -- the raw function is NOT
    injective in general, and `_code_ident` output IS single-slash -- are
    pinned by test rather than asserted by reasoning: #222 phase 5 task 10,
    `test_lineage_marker_ident_is_not_injective_on_raw_input` and
    `test_code_idents_carry_exactly_one_slash` (tests/test_mcp_server.py).
    If a future caller ever mints `_lineage_marker_ident` from something
    other than a `_code_ident` output, re-check this property before trusting
    it.
    """
    return f":lineage/{entity_ident.lstrip(':').replace('/', '-')}"


def _lineage_mark_provisional(
    db: Any, entity_ident: str, commit_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Create the :type/lineage-marker companion entity for entity_ident, if
    one doesn't already exist. Query-before-write (mirrors _watermark_update)
    -- a marker already present is a no-op, never a duplicate write. Uses
    internal _transact directly, never handle_minigraf_transact: :type/
    lineage-marker is deliberately unregistered in MINIGRAF_SCHEMA, and the
    public handler's schema gate would reject it outright. One-entity form
    of _lineage_mark_provisional_batch, delegating so the two cannot drift
    (#233, matching _lineage_confirm's own delegation rationale).
    """
    _lineage_mark_provisional_batch(db, [entity_ident], commit_ts_iso, index_con=index_con)


def _lineage_mark_provisional_batch(
    db: Any, entity_idents: Sequence[str], commit_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Batched _lineage_mark_provisional (#233). Same query-before-write
    per ident -- an entity already marked is skipped, never duplicated --
    but the facts for every genuinely-new marker go in ONE _transact.

    Collision-free: each marker is its own :lineage/... companion entity, so
    no two facts in the batch share (entity, attribute, valid_from).
    """
    facts: List[str] = []
    for entity_ident in entity_idents:
        if _lineage_is_provisional(db, entity_ident):
            continue
        ident = _lineage_marker_ident(entity_ident)
        facts.extend([
            f"[{ident} :entity-type {_LINEAGE_MARKER_ENTITY_TYPE}]",
            f"[{ident} :entity {entity_ident}]",
            f"[{ident} :status :provisional]",
        ])
    if facts:
        _transact(db, "[" + " ".join(facts) + "]", commit_ts_iso, index_con=index_con)


def _lineage_confirm_batch(
    db: Any, entity_idents: Sequence[str], index_con: Optional[Any] = None
) -> None:
    """Batched _lineage_confirm (#233). Retracts the :type/lineage-marker
    companion entity's facts for every still-provisional ident in ONE
    _retract; idents with no marker are skipped, so callers can pass a whole
    commit's candidates unconditionally.

    Collision-free: each marker is its own :lineage/... companion entity.
    """
    facts = []
    for entity_ident in entity_idents:
        if not _lineage_is_provisional(db, entity_ident):
            continue
        ident = _lineage_marker_ident(entity_ident)
        facts.extend([
            f"[{ident} :entity-type {_LINEAGE_MARKER_ENTITY_TYPE}]",
            f"[{ident} :entity {entity_ident}]",
            f"[{ident} :status :provisional]",
        ])
    if facts:
        _retract(db, "[" + " ".join(facts) + "]", index_con=index_con)


def _lineage_confirm(db: Any, entity_ident: str, index_con: Optional[Any] = None) -> None:
    """Retract the :type/lineage-marker companion entity's facts for
    entity_ident if present; no-op if absent, so callers can call this
    unconditionally without checking first. One-entity form of
    _lineage_confirm_batch, delegating so the two cannot drift (#233).
    """
    _lineage_confirm_batch(db, [entity_ident], index_con=index_con)


def _lineage_is_provisional(db: Any, entity_ident: str) -> bool:
    """True iff a :type/lineage-marker companion entity currently exists for
    entity_ident."""
    ident = _lineage_marker_ident(entity_ident)
    raw = _db_execute(db, f"(query [:find ?e :where [{ident} :entity ?e]])")
    return bool(json.loads(raw).get("results", []))


_LINEAGE_CONFIRMED_THROUGH_IDENT = ":ingestion/lineage-confirmed-through"


def _lineage_confirmed_through_query(db: Any) -> Optional[str]:
    """Return the hash of the last commit through which lineage is fully
    confirmed, or None if nothing has been confirmed yet."""
    raw = _db_execute(
        db, f"(query [:find ?h :where [{_LINEAGE_CONFIRMED_THROUGH_IDENT} :hash ?h]])"
    )
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _lineage_confirmed_through_update(
    db: Any, commit_hash: str, commit_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Record the last lineage-confirmed commit hash, mirroring
    _watermark_update's retract-only-if-changed pattern. Uses :type/
    ingestion -- the SAME registered/audited entity type :ingestion/
    watermark already uses -- so this entity carries the same required
    :description constant _watermark_update's own entity does.
    """
    current_raw = _db_execute(
        db, f"(query [:find ?a ?v :where [{_LINEAGE_CONFIRMED_THROUGH_IDENT} ?a ?v]])"
    )
    current: Dict[str, str] = dict(json.loads(current_raw).get("results", []))

    def _edn(attr: str, value: str) -> str:
        return value if attr == ":entity-type" else f'"{_edn_escape(value)}"'

    constants = {
        ":entity-type": ":type/ingestion",
        ":ident": _LINEAGE_CONFIRMED_THROUGH_IDENT,
        ":description": "lineage confirmed-through watermark",
    }

    to_retract: List[str] = []
    to_transact: List[str] = []
    for attr, value in constants.items():
        if current.get(attr) == value:
            continue
        if attr in current:
            to_retract.append(f"[{_LINEAGE_CONFIRMED_THROUGH_IDENT} {attr} {_edn(attr, current[attr])}]")
        to_transact.append(f"[{_LINEAGE_CONFIRMED_THROUGH_IDENT} {attr} {_edn(attr, value)}]")

    if ":hash" in current:
        to_retract.append(f"[{_LINEAGE_CONFIRMED_THROUGH_IDENT} :hash {_edn(':hash', current[':hash'])}]")
    to_transact.append(f"[{_LINEAGE_CONFIRMED_THROUGH_IDENT} :hash {_edn(':hash', commit_hash)}]")

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)


def _lineage_confirmed_through_migrate(
    db: Any, run_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """One-time catch-up: if :ingestion/frontier-low exists (this graph has
    an authoritative region, whether freshly migrated by
    _frontier_seed_from_watermark just now or already established by an
    earlier Phase-1-only run) but :ingestion/lineage-confirmed-through is
    unset, seed the watermark from frontier-low's *current* :hi-hash --
    that whole region was ingested by the original single-stream
    forward-only authoritative walk, so it is already fully
    lineage-confirmed. No-op if frontier-low doesn't exist yet, or
    lineage-confirmed-through is already set (so later phases' own sweep
    updates are never clobbered back to a stale value).
    """
    if _lineage_confirmed_through_query(db) is not None:
        return
    low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
    if low_bounds is None:
        return
    _, hi_hash = low_bounds
    _lineage_confirmed_through_update(db, hi_hash, run_ts_iso, index_con=index_con)


def _candidate_diff_purge_legacy(db: Any, index_con: Optional[Any] = None) -> int:
    """One-time cleanup for graphs written by #222 phase 2d, which persisted
    a :type/candidate-diff record per (claimed commit, candidate entity)
    pair. #233 deleted that path outright -- 2a specced the records so 2c
    could confirm a candidate by hash comparison instead of re-parsing, but
    2c as built re-parses on the process pool and reads
    precomputed["unchanged_idents"], so nothing ever read them.

    Without this, the records a 2d graph already holds are orphaned: the
    writer AND the clearer are both gone. They are not inert -- fact_index
    filters nothing by prefix (_MEMORY_PREFIXES affects scoring, not
    inclusion), so they stay retrievable scratch noise indefinitely.

    Called unconditionally from _frontier_load rather than gated on a
    watermark: it is one query per load, cheap when there is nothing to
    purge, and gating it would need a new watermark whose only job is to
    record that a one-time deletion happened.

    Returns the number of records purged (0 if there was nothing to purge,
    or if the retract itself failed -- see below).

    COST SHAPE: cheap when there is nothing to purge (one query, no
    retract), but the retract itself is NOT cheap for a graph that actually
    holds legacy records -- measured on the real backend at ~0.5s for 500
    records, ~7.2s for 2,000, and ~127s for 8,000 (roughly O(N^2); chunking
    the retract does not help, since the cost is in minigraf's retract
    itself, not in call-count). A phase-2d graph can hold on the order of
    10k+ live records, i.e. minutes of no output. This is still called
    unconditionally (see above) because it is one-time per graph -- once
    purged, later loads see zero rows and return immediately -- and gating
    it behind a new watermark would add persistent state for a problem that
    self-resolves after the first successful run. A row count is logged to
    stderr before retracting so a multi-minute stall is legible rather than
    looking like a hang, and the retract is best-effort: a failure is
    logged and swallowed rather than propagated, because this is scratch
    cleanup, not core ingestion, and _frontier_load's caller must not be
    bricked for every future run by one bad retract here.

    Records for distinct :candidate/ entities do not share (entity,
    attribute, valid_from), so the whole purge is one collision-free
    _retract call (cost above is backend-internal to that one call, not a
    batching artifact).

    Note: minigraf resolves ?e in a query result to its internal entity id,
    not the :candidate/... keyword the fact was minted with, and that
    internal id is not accepted as an entity token in a retract pattern
    (no :ident back-reference is populated for these records the way it is
    for e.g. commits -- see _rebuild_index_from_graph's ident_map). The
    keyword ident is deterministic from (commit_ident, entity_ident) --
    the same formula the deleted _candidate_diff_ident used -- so it is
    rebuilt here instead of relying on ?e.
    """
    raw = _db_execute(
        db, "(query [:find ?c ?ent ?h :where "
            "[?e :entity-type :type/candidate-diff] [?e :commit ?c] "
            "[?e :entity ?ent] [?e :body-hash ?h]])"
    )
    rows = json.loads(raw).get("results", [])
    if not rows:
        return 0
    print(
        f"[_candidate_diff_purge_legacy] purging {len(rows)} legacy "
        ":type/candidate-diff record(s) -- this is O(n^2) in minigraf's "
        "retract and may take a while (measured ~127s at 8,000 records)",
        file=sys.stderr,
    )
    facts = []
    for commit_ident, entity_ident, body_hash in rows:
        ident = f":candidate/{commit_ident[len(':commit/'):]}-{entity_ident.lstrip(':').replace('/', '-')}"
        facts.extend([
            f"[{ident} :entity-type :type/candidate-diff]",
            f"[{ident} :commit {commit_ident}]",
            f"[{ident} :entity {entity_ident}]",
            f'[{ident} :body-hash "{_edn_escape(body_hash)}"]',
        ])
    try:
        _retract(db, "[" + " ".join(facts) + "]", index_con=index_con)
    except Exception as e:
        print(f"[_candidate_diff_purge_legacy] retract failed, {len(rows)} record(s) left in place: {e}", file=sys.stderr)
        return 0
    return len(rows)


def _entity_ident_is_live(db: Any, entity_ident: str) -> bool:
    """True iff entity_ident currently has a live :ident fact.

    The reverse walk's "do I already know this entity?" gate (#231). It used
    to ask _entity_introduced_by_query(db, ident) is not None, which was
    unsound: _build_close_triples never retracted :introduced-by, so a
    closed-and-purged entity kept that fact forever and the gate answered
    "known" for it. _build_code_triples then took its "already known" branch
    and emitted only :modified-in -- the entity was resurrected with lineage
    but no identity, invisible to nearly every query, and
    _correction_sweep_apply could not repair it either (it reconciles lineage
    only; the one place it emits structural facts, #349's rebirth branch,
    requires ZERO :introduced-by values, and a ghost still carries one).

    Task 4 of this change does make close sites retract :introduced-by, which
    would make the old gate correct too. This gate stays on :ident anyway: the
    question it asks IS liveness, and coupling it to a lineage attribute is
    what made #231 possible. It also stays correct if a future close site
    forgets :introduced-by.

    Current-time query by design -- an entity live in a CLOSED window is
    exactly the resurrection case this must answer False for.
    """
    raw = _db_execute(db, f"(query [:find ?i :where [{entity_ident} :ident ?i]])")
    return bool(json.loads(raw).get("results", []))


def _entity_introduced_by_values_query(db: Any, entity_ident: str) -> List[str]:
    """Every live :introduced-by value for entity_ident, in the backend's
    unspecified order; [] if it has none.

    A list rather than a single value because an entity CAN hold more than
    one (#235): the forward walk used to mint a second alongside the reverse
    stream's provisional guess. _correction_sweep_apply's repair path needs
    to see all of them to collapse them.
    """
    raw = _db_execute(db, f"(query [:find ?c :where [{entity_ident} :introduced-by ?c]])")
    return [row[0] for row in json.loads(raw).get("results", [])]


# Max two-value :introduced-by warnings _entity_introduced_by_query writes to
# stderr per ingestion run (#235). Same budget and same intent as
# _CORRECTION_SWEEP_LOG_CAP -- a corrupted at-scale graph must not drown the
# log -- but it cannot use that cap's caller-threaded-total mechanism: this is
# a leaf query called from four sites in two walks (_reverse_apply alone calls
# it up to 3x per ident per commit), none of which carry, or should carry, a
# running log total just to reach it.
#
# The budget therefore lives in a module global, with the per-run reset that
# _CORRECTION_SWEEP_LOG_CAP gets for free from its caller's zero-initialized
# local supplied explicitly instead: _run_ingestion calls
# _reset_introduced_by_ambiguity_log_budget() at the top of every run. That
# preserves the property the sweep's design note actually cares about -- this
# server is long-lived and runs many ingests, and a never-reset counter would
# burn its budget on the first one and log nothing ever after.
_INTRODUCED_BY_AMBIGUITY_LOG_CAP = 10
_introduced_by_ambiguity_logged = 0


def _reset_introduced_by_ambiguity_log_budget() -> None:
    """Restore _entity_introduced_by_query's full stderr budget.

    Called once per ingestion run (see _run_ingestion). Not thread-safe in
    any strict sense, and deliberately so: the counter guards log volume
    only, never a decision, so a lost increment under concurrency costs at
    most one extra or one missing warning line.
    """
    global _introduced_by_ambiguity_logged
    _introduced_by_ambiguity_logged = 0


def _entity_introduced_by_query(db: Any, entity_ident: str) -> Optional[str]:
    """Return entity_ident's current :introduced-by value (a commit ident
    string), or None if it has none yet.

    An entity holding two values is corrupt (#235). Which of them is
    returned here is UNSPECIFIED -- the backend imposes no ordering -- so
    this warns and returns an arbitrary one rather than pretending the
    graph is well-formed. It deliberately does NOT raise: both walks call
    this during a run, long before Stage B's repair pass, so raising would
    hard-fail ingestion on exactly the graphs that repair exists to heal.
    Position-based selection of the survivor lives in
    _correction_sweep_apply, which has the linearization positions this
    function does not.

    THE REPAIR IS WITHIN-RUN ONLY, and this used to say otherwise (#287).
    The sweep heals what it reaches while a run is still climbing, and that
    is all. A finished run parks :ingestion/correction-sweep-through at
    frontier-high's own :hi-hash, so the next run's
    _correction_sweep_select_position computes pos = through + 1 > ceiling_pos
    and returns None on its FIRST call -- _correction_sweep_apply, which owns
    the repair, then runs zero times. Measured during #235: watermark at
    linearization position 13 of 13, one select call, zero apply calls. Later
    commits raise the ceiling but never lower the resume point, so a position
    already passed is not revisited either.

    So an entity left corrupt by a COMPLETED run stays corrupt, and no amount
    of re-ingestion changes that: the graph must be REBUILT into a fresh graph
    path (CLAUDE.md's standing decision). evals/at_scale/introduced_by_audit.py
    is how a graph is asked whether it is in that state.

    The warning is rate-capped per run (_INTRODUCED_BY_AMBIGUITY_LOG_CAP);
    the RETURN VALUE never is. A corrupt at-scale graph can hold thousands
    of ambiguous entities and _reverse_apply calls this up to 3x per ident
    per commit, so an uncapped line here buries the rest of the ingest log
    long before the repair pass can run.
    """
    global _introduced_by_ambiguity_logged
    values = _entity_introduced_by_values_query(db, entity_ident)
    if len(values) > 1:
        _introduced_by_ambiguity_logged += 1
        if _introduced_by_ambiguity_logged <= _INTRODUCED_BY_AMBIGUITY_LOG_CAP:
            print(
                f"[_entity_introduced_by] {entity_ident} has {len(values)} live "
                f":introduced-by values {sorted(values)} -- returning an arbitrary "
                "one (#235); the correction sweep repairs this only while "
                "this run is still climbing -- a graph left this way by a "
                "completed run must be rebuilt, not re-ingested (#287)",
                file=sys.stderr,
            )
            if _introduced_by_ambiguity_logged == _INTRODUCED_BY_AMBIGUITY_LOG_CAP:
                print(
                    f"[_entity_introduced_by] log cap "
                    f"({_INTRODUCED_BY_AMBIGUITY_LOG_CAP}) reached -- further "
                    "ambiguity warnings suppressed for this run",
                    file=sys.stderr,
                )
    return values[0] if values else None


def _entity_introduced_by_set_provisional_batch(
    db: Any,
    entity_idents: Sequence[str],
    commit_ident: str,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
    pos: Optional[int] = None,
    pos_by_commit_ident: Optional[Dict[str, int]] = None,
) -> Set[str]:
    """Assert or move a PROVISIONAL :introduced-by to commit_ident for many
    entities at once (#233), in one _retract and at most two _transact
    calls instead of two writes per ident. _reverse_apply's two loops were
    the only production callers of the per-ident form, at ~1,265 idents per
    commit on a real repository; retracts cost ~13ms against a transact's
    ~1ms, so the retract batching is the load-bearing half.

    Returns the set of idents whose guess was actually asserted or moved.
    NO PRODUCTION CONSUMER -- both call sites in _reverse_apply discard it.
    It is kept because the test suite asserts on it to distinguish "moved" from
    "left alone", which is otherwise only observable by re-querying every
    ident. Do not "clean it up" into None without re-pointing those tests.

    Every gate is per-ident and is applied in the same order the per-ident
    function used, because batching must not turn one ident's refusal into
    the whole batch being dropped, nor a refused ident into an included one:

      - authoritative (a value exists and _lineage_is_provisional is False)
        -- never touched; reverse walk must never clobber a confirmed fact
      - value already equals commit_ident -- no write, but still marked
      - the monotonicity refusal (a guess may only move EARLIER), with its
        own stderr line per refused ident

    Idempotent per ident: an ident whose value already equals commit_ident
    contributes no fact write (but is still included in the marker batch).
    Query-before-write, retract-then-reassert only for idents whose value
    genuinely changed -- mirrors _watermark_update's pattern, since
    re-transacting the same (entity, attribute, value) at a new valid_from
    creates a duplicate live datom under minigraf's write semantics.

    Positions, never timestamps: ingest :date values are AUTHOR dates
    (_git_commits reads `%at`) and are not monotonic in topological order
    (clock skew, rebases, cherry-picks), which is why build_linearization
    uses --topo-order at all, so comparing :date values would silently
    mis-order commits. pos and pos_by_commit_ident both default to None, in
    which case the monotonicity guard is skipped for every ident in the
    batch; _reverse_apply passes them always.

    Collision-free: within each batch the facts differ in entity, and only
    facts sharing (entity, attribute, valid_from) collapse in minigraf's
    EAVT pending index. :contains is the attribute where that bites, and it
    is not written here.
    """
    to_retract: List[str] = []
    to_transact: List[str] = []
    to_mark: List[str] = []
    moved: Set[str] = set()

    for entity_ident in entity_idents:
        current = _entity_introduced_by_query(db, entity_ident)
        if current is not None and not _lineage_is_provisional(db, entity_ident):
            continue  # authoritative -- never touch
        if current == commit_ident:
            to_mark.append(entity_ident)
            continue
        if current is not None and pos is not None and pos_by_commit_ident is not None:
            current_pos = pos_by_commit_ident.get(current)
            if current_pos is not None and pos >= current_pos:
                print(
                    f"[_entity_introduced_by_set_provisional] refusing to move "
                    f"{entity_ident}'s guess from {current} (position {current_pos}) "
                    f"to {commit_ident} (position {pos}): a guess may only move earlier",
                    file=sys.stderr,
                )
                continue
        if current is not None:
            to_retract.append(f"[{entity_ident} :introduced-by {current}]")
        to_transact.append(f"[{entity_ident} :introduced-by {commit_ident}]")
        to_mark.append(entity_ident)
        moved.add(entity_ident)

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    if to_transact:
        _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)
    _lineage_mark_provisional_batch(db, to_mark, commit_ts_iso, index_con=index_con)
    return moved


def _entity_introduced_by_set_provisional(
    db: Any,
    entity_ident: str,
    commit_ident: str,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
    pos: Optional[int] = None,
    pos_by_commit_ident: Optional[Dict[str, int]] = None,
) -> None:
    """One-entity form of _entity_introduced_by_set_provisional_batch --
    see that function for the gates. A delegating wrapper rather than a
    parallel implementation (#233) so the batched and unbatched paths
    cannot drift apart; the batch is the only production caller shape.
    """
    _entity_introduced_by_set_provisional_batch(
        db, [entity_ident], commit_ident, commit_ts_iso,
        index_con=index_con, pos=pos, pos_by_commit_ident=pos_by_commit_ident,
    )


def _re_date_structural_facts(
    db: Any,
    structural_triples: List[str],
    new_ts_iso: str,
    index_con: Optional[Any] = None,
) -> None:
    """Retract and re-assert an entity's structural facts at new_ts_iso.

    Used whenever a provisional :introduced-by guess is resolved to an
    earlier commit -- by _correction_sweep_apply when the sweep confirms an
    entity at its introduction, and by _forward_reconcile_provisional when
    the forward walk reaches the true introduction. In both cases the facts
    were written at the timestamp of the commit where the entity was first
    SIGHTED, which is later than the introduction now is, leaving a valid
    time window where :introduced-by is live for an entity with no type,
    name or file -- so ":as-of the introduction" reports it as nonexistent.

    _reverse_apply deliberately does NOT call this (#233): it used to, on
    every provisional move, which re-dated a long-lived entity once per
    touch instead of once. Both callers above fire once per entity.

    _retract targets live rows regardless of their original valid_from, so
    this is a straight re-assert at the earlier timestamp.

    :contains is retracted and transacted ONE TRIPLE PER CALL, on both
    sides. Minigraf's EAVT pending index omits value bytes, so facts sharing
    (entity, attribute, valid_from) in one call collapse to the last -- see
    #222 phase 2b1, where this cost five of six containment edges on an
    ordinary multi-entity file, permanently.
    """
    contains = [t for t in structural_triples if ":contains" in t]
    other = [t for t in structural_triples if ":contains" not in t]
    if other:
        _retract(db, "[" + " ".join(other) + "]", index_con=index_con)
        _transact(db, "[" + " ".join(other) + "]", new_ts_iso, index_con=index_con)
    for triple in contains:
        _retract(db, "[" + triple + "]", index_con=index_con)
        _transact(db, "[" + triple + "]", new_ts_iso, index_con=index_con)


def _forward_reconcile_provisional(
    db: Any,
    entity_ident: str,
    structural_triples: List[str],
    true_commit_ts_iso: str,
    ts_by_commit_ident: Dict[str, str],
    commit_ident: str,
    index_con: Optional[Any] = None,
) -> Optional[str]:
    """#222 phase 2d: the forward walk has reached entity_ident's TRUE
    introduction and found a provisional guess left by Stream 2. Clear the
    guess so the caller's normal authoritative emission is the only
    :introduced-by fact that survives.

    Returns the superseded guess's commit ident, or None if entity_ident was
    not provisional (in which case nothing is written at all).

    Deliberately does NOT write the authoritative :introduced-by itself --
    the caller's ordinary _build_code_triples output does that, so there is
    exactly one code path that mints an authoritative introduction and this
    function cannot drift from it.

    Re-dates structural facts itself (via _re_date_structural_facts), same
    as _correction_sweep_apply's case 1 does (#233) -- _reverse_apply's own
    supersede path deliberately does NOT do this anymore (see its
    docstring), so this function no longer mirrors it on that point.
    Still mirrors _reverse_apply's supersede path for the refusal to
    back-date a :modified-in edge whose commit timestamp is unknown, and --
    what commit_ident is for -- the same refusal to hand the entity a
    :modified-in edge at its OWN introduction (_reverse_apply's
    `superseded_ident != commit_ident` guard).
    """
    guess_ident = _entity_introduced_by_query(db, entity_ident)
    if not _lineage_is_provisional(db, entity_ident):
        return None

    # 1. Drop the guess. The caller writes the real one immediately after.
    if guess_ident is not None:
        _retract(db, f"[[{entity_ident} :introduced-by {guess_ident}]]", index_con=index_con)

    # 2. Re-date structural facts to the true (earlier) introduction.
    _re_date_structural_facts(
        db,
        [t for t in structural_triples if ":introduced-by" not in t],
        true_commit_ts_iso,
        index_con=index_con,
    )

    # 3. The entity's lineage is authoritative from here on.
    _lineage_confirm(db, entity_ident, index_con=index_con)

    if guess_ident is None:
        return None

    # 4. The guess commit is now known to be a genuine modification rather
    # than the introduction -- UNLESS it is this very commit, which is the
    # introduction. Stream 2 can guess the right commit and still leave a
    # provisional marker on it (it claims high-to-low, so the first sighting
    # of an entity born inside its own region IS that entity's introduction);
    # the marker then survives whenever the run is interrupted before Stage B
    # confirms it, and a later run's forward walk re-walks the position after
    # _frontier_load discards the unrepresentable high interval. Writing the
    # edge here would assert that the entity was modified at the commit that
    # created it -- the exact shape _correction_sweep_apply refuses to write
    # via its own self-introduction guard, and one a forward-only ingest can
    # never produce. It is also unreachable by the sweep once the position is
    # forward territory, so it would be permanent.
    if guess_ident == commit_ident:
        return guess_ident

    # Otherwise the guess commit earns the :modified-in edge Stream 2
    # withheld from it. Dated at ITS OWN timestamp, not this commit's.
    #
    # This inherits 2b's documented over-assertion: #221's unchanged-body
    # narrowing cannot be re-checked against the guess commit's own diff,
    # because only that commit's own parse carries the data. That is already
    # owned -- the guess commit lies inside frontier-high's territory, so the
    # correction sweep visits it, finds exactly one :introduced-by (case 3),
    # and retracts this edge if its own parse says the body was unchanged.
    guess_ts = ts_by_commit_ident.get(guess_ident)
    if guess_ts is None:
        print(
            f"[_forward_reconcile_provisional] skipping retroactive :modified-in "
            f"for {entity_ident} at {guess_ident}: no timestamp in commit_metadata",
            file=sys.stderr,
        )
        return guess_ident
    _transact(
        db, f"[[{entity_ident} :modified-in {guess_ident}]]", guess_ts, index_con=index_con,
    )
    return guess_ident


_LAST_RUN_KEYWORD_ATTRS = frozenset({":entity-type"})
_LAST_RUN_NUMERIC_ATTRS = frozenset({":total-ingested"})


def _last_run_write(db: Any, commit_hash: str, run_at: str, total_ingested: int, index_con: Optional[Any] = None) -> None:
    """Record the wall-clock time, final commit hash, and cumulative ingested count.

    Same graph-level non-idempotency (#156) as _watermark_update/_ingest_tags:
    re-transacting the same (entity, attribute, value) under a fresh valid-from
    creates a second genuinely live duplicate rather than a no-op -- this was
    unconditionally re-transacting all six attributes on every completed run,
    so after the second run the singleton :ingestion/last-run-at entity carried
    multiple live values per attribute, and any-valid-time readers (e.g.
    handle_minigraf_ingest_status) could pair one run's timestamp with a
    different run's commit hash (#186). Diffs against the entity's current
    live values first and only retracts+reasserts attributes that actually
    changed -- :entity-type/:ident/:description are constant and written once;
    :last-run-at/:last-commit/:total-ingested change every run and always
    retract-then-reassert, same as :hash in _watermark_update.
    """
    desired: Dict[str, Any] = {
        ":entity-type": ":type/ingestion",
        ":ident": ":ingestion/last-run-at",
        ":description": "last ingestion run timestamp",
        ":last-run-at": run_at,
        ":last-commit": commit_hash,
        ":total-ingested": total_ingested,
    }

    current_raw = _db_execute(db, "(query [:find ?a ?v :where [:ingestion/last-run-at ?a ?v]])")
    current: Dict[str, Any] = dict(json.loads(current_raw).get("results", []))

    def _edn(attr: str, value: Any) -> str:
        if attr in _LAST_RUN_KEYWORD_ATTRS:
            return value
        if attr in _LAST_RUN_NUMERIC_ATTRS:
            return str(value)
        return f'"{_edn_escape(value)}"'

    to_retract: List[str] = []
    to_transact: List[str] = []
    for attr, value in desired.items():
        if current.get(attr) == value:
            continue  # already correct -- skip to avoid creating a duplicate live fact (#156)
        if attr in current:
            to_retract.append(f"[:ingestion/last-run-at {attr} {_edn(attr, current[attr])}]")
        to_transact.append(f"[:ingestion/last-run-at {attr} {_edn(attr, value)}]")

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    if to_transact:
        _transact(db, "[" + " ".join(to_transact) + "]", run_at, index_con=index_con)


_INGESTION_BRANCH_IDENT = ":ingestion/branch"


def _ingestion_branch_read(db: Any) -> Optional[str]:
    """The ref this graph was last ingested against, or None if it predates
    #222 phase 5 (or no run has completed its first write yet).

    None is NOT "master" and must never be defaulted to one: the orphan check
    reads an absent branch as "proved nothing", which is the honest answer for
    a graph that never recorded one.
    """
    raw = _db_execute(
        db, f"(query [:find ?b :where [{_INGESTION_BRANCH_IDENT} :branch ?b]])"
    )
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _ingestion_branch_write(
    db: Any, branch: str, run_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Record the ref this run is walking. Written on EVERY run -- the branch
    can change between runs -- which is why it is not a stamp-if-new like
    _graph_format_version_stamp_if_new.

    Value-diffed before writing, exactly as _last_run_write and _ingest_tags
    do: minigraf is NOT idempotent at the graph level for re-transacting the
    same (entity, attribute, value) at a fresh valid-from (#156), so an
    unconditional re-transact accumulates a duplicate live fact per run.

    Not folded into :ingestion/last-run-at, which is written only under
    `if completed_all:` -- an interrupted run would record no branch, and the
    orphan check needs the discriminator MORE on an interrupted graph, not
    less.
    """
    current = _ingestion_branch_read(db)
    if current == branch:
        return
    desired = {
        ":entity-type": ":type/ingestion",
        ":ident": _INGESTION_BRANCH_IDENT,
        ":description": "ref this graph was last ingested against",
        ":branch": branch,
    }
    to_retract: List[str] = []
    to_transact: List[str] = []
    raw = _db_execute(
        db, f"(query [:find ?a ?v :where [{_INGESTION_BRANCH_IDENT} ?a ?v]])"
    )
    live: Dict[str, Any] = dict(json.loads(raw).get("results", []))
    for attr, value in desired.items():
        if live.get(attr) == value:
            continue
        rendered = value if attr == ":entity-type" else f'"{_edn_escape(value)}"'
        if attr in live:
            old = live[attr]
            old_rendered = old if attr == ":entity-type" else f'"{_edn_escape(old)}"'
            to_retract.append(f"[{_INGESTION_BRANCH_IDENT} {attr} {old_rendered}]")
        to_transact.append(f"[{_INGESTION_BRANCH_IDENT} {attr} {rendered}]")
    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    if to_transact:
        _transact(db, "[" + " ".join(to_transact) + "]", run_ts_iso, index_con=index_con)


def _orphaned_commit_count(
    db: Any, linearization: List[str], recorded_branch: Optional[str], ref: str
) -> Optional[int]:
    """How many live :type/commit entities hold a hash this ref's history no
    longer contains, or None when that question cannot be answered.

    None when the graph recorded no branch, or recorded a different one: those
    commits may belong to another ingested branch, and reporting a count would
    invite a reader to treat real history as garbage. Detection only -- nothing
    here retracts anything.
    """
    if recorded_branch is None or recorded_branch != ref:
        return None
    raw = _db_execute(
        db, "(query [:find ?h :where [?e :entity-type :type/commit] [?e :hash ?h]])"
    )
    graph_hashes = {r[0] for r in json.loads(raw).get("results", []) if r}
    if not graph_hashes:
        return None
    return len(graph_hashes - set(linearization))


# System attributes written by _transact_extracted_facts alongside domain attributes.
# They are invisible to schema validation and filtered from attr_facts in minigraf_audit.
_SYSTEM_ATTRS: frozenset = frozenset({":entity-type", ":ident"})

# Maximum length (characters) for a string-valued fact attribute. Bounds how much
# raw text (e.g. LLM/agent-extracted conversation content) can be written into the
# graph and the FTS5 fact index in a single fact.
_MAX_FACT_VALUE_LENGTH = int(os.environ.get("MINIGRAF_MAX_FACT_VALUE_LENGTH", "4096"))

MINIGRAF_SCHEMA: Dict[str, Dict[str, Dict[str, type]]] = {
    "decision": {
        "required": {":description": str},
        "optional": {":rationale": str, ":date": str, ":alias": str},
    },
    "preference": {
        "required": {":description": str},
        "optional": {":rationale": str, ":alias": str},
    },
    "constraint": {
        "required": {":description": str},
        "optional": {":rationale": str, ":alias": str},
    },
    "dependency": {
        "required": {":description": str},
        "optional": {":rationale": str, ":alias": str},
    },
    "module": {
        "required": {":description": str},
        "optional": {
            ":path": str, ":alias": str,
            # graph edges (keyword-valued, stored as strings)
            ":contains": str, ":depends-on": str, ":calls": str,
            # commit cross-references
            ":introduced-by": str, ":modified-in": str,
            # rename/move continuity (see 2026-07-14 rename-tracking design doc)
            ":renamed-from": str, ":renamed-to": str,
        },
    },
    "function": {
        "required": {":description": str},
        "optional": {
            ":file": str, ":alias": str,
            ":introduced-by": str, ":modified-in": str,
            ":renamed-from": str, ":renamed-to": str,
        },
    },
    "class": {
        "required": {":description": str},
        "optional": {
            ":file": str, ":alias": str,
            ":introduced-by": str, ":modified-in": str,
            ":renamed-from": str, ":renamed-to": str,
        },
    },
    "variable": {
        "required": {":description": str},
        "optional": {
            ":file": str, ":alias": str,
            ":introduced-by": str, ":modified-in": str,
            ":renamed-from": str, ":renamed-to": str,
        },
    },
    "field": {
        "required": {":description": str},
        "optional": {
            ":file": str, ":alias": str, ":class": str, ":static": bool,
            ":introduced-by": str, ":modified-in": str,
            ":renamed-from": str, ":renamed-to": str,
        },
    },
    "ingestion": {
        "required": {":description": str},
        # :version carries the graph format version (#263). It MUST stay listed
        # here: minigraf_audit iterates every registered type and retracts any
        # attribute outside its allowed set, querying the live graph directly,
        # so dropping this line makes an audit run silently delete the very
        # stamp that protects the graph from being read under the wrong ident
        # rule. :branch (#222 phase 5) is load-bearing for the same reason --
        # it is the discriminator that tells a force-push orphan apart from a
        # second branch ingested into the same graph, and an audit that
        # retracted it would make every orphan count uninterpretable.
        "optional": {":hash": str, ":alias": str, ":last-run-at": str, ":last-commit": str,
                     ":total-ingested": int, ":version": int, ":branch": str},
    },
    "commit": {
        "required": {":description": str},
        "optional": {
            ":hash": str, ":author": str, ":subject": str, ":date": str, ":alias": str,
            # parent commit reference (keyword-valued edge, stored as string)
            ":parent": str,
        },
    },
}


def _validate_facts(facts: List[Dict[str, Any]]) -> List[str]:
    """Validate proposed facts against MINIGRAF_SCHEMA. Returns violation strings.

    Closed-world: unknown entity types and unknown attributes are both violations.
    System attributes (_SYSTEM_ATTRS) are silently skipped — they are internal
    tags added by _transact_extracted_facts, not domain attributes.
    Pure function — no DB access. Mirrors Schema.validate() from minigraf-schema.
    """
    violations: List[str] = []

    # Group facts by entity to check required attributes across all facts for one entity.
    entity_attrs: Dict[str, Dict[str, Any]] = {}
    entity_types: Dict[str, str] = {}
    for fact in facts:
        entity = fact.get("entity", "")
        entity_type = fact.get("entity_type", "")
        attribute = fact.get("attribute", "")
        value = fact.get("value")
        if attribute in _SYSTEM_ATTRS:
            continue  # system attributes bypass schema validation
        entity_attrs.setdefault(entity, {})[attribute] = value
        if entity_type:
            entity_types[entity] = entity_type

    for entity, attrs in entity_attrs.items():
        entity_type = entity_types.get(entity, "")

        # Closed-world: unknown entity type is a violation.
        if entity_type not in MINIGRAF_SCHEMA:
            violations.append(
                f"entity '{entity}' has unknown type '{entity_type}' — "
                f"allowed: {list(MINIGRAF_SCHEMA)}"
            )
            continue

        schema = MINIGRAF_SCHEMA[entity_type]
        required = schema["required"]
        optional = schema["optional"]
        allowed = set(required) | set(optional)

        # Check required attributes are present with correct type.
        for attr, expected_type in required.items():
            if attr not in attrs:
                violations.append(
                    f"entity '{entity}' missing required attribute '{attr}'"
                )
            elif not isinstance(attrs[attr], expected_type):
                violations.append(
                    f"entity '{entity}' attribute '{attr}' has wrong type "
                    f"(expected {expected_type.__name__}, got {type(attrs[attr]).__name__})"
                )

        # Check optional attributes, if present, have correct type.
        for attr, value in attrs.items():
            if attr in optional and not isinstance(value, optional[attr]):
                violations.append(
                    f"entity '{entity}' attribute '{attr}' has wrong type "
                    f"(expected {optional[attr].__name__}, got {type(value).__name__})"
                )

        # Bound string-valued attributes so a single fact can't inject an
        # arbitrarily large value into the graph and FTS5 index.
        for attr, value in attrs.items():
            if isinstance(value, str) and len(value) > _MAX_FACT_VALUE_LENGTH:
                violations.append(
                    f"entity '{entity}' attribute '{attr}' value exceeds maximum "
                    f"length ({len(value)} > {_MAX_FACT_VALUE_LENGTH} characters)"
                )

        # Closed-world: unknown attributes are violations.
        for attr in attrs:
            if attr not in allowed:
                violations.append(
                    f"entity '{entity}' has unknown attribute '{attr}' — "
                    f"allowed: {sorted(allowed)}"
                )

    return violations


def _parse_transact_facts(facts_str: str) -> List[Dict[str, Any]]:
    """Parse a Datalog transact string into fact dicts for schema validation.

    Only captures string-valued triples (quoted values). Keyword values
    like :type/decision are skipped — they are internal type tags, not
    user-authored facts subject to schema validation.
    """
    pattern = r'\[(\:[^\s\]]+)\s+(\:[^\s\]]+)\s+"((?:[^"\\]|\\.)+)"\]'
    result = []
    for match in re.finditer(pattern, facts_str):
        entity, attribute, raw_value = match.groups()
        entity_type = entity.split("/")[0].lstrip(":") if "/" in entity else ""
        result.append({
            "entity": entity,
            "entity_type": entity_type,
            "attribute": attribute,
            "value": _edn_unescape(raw_value),
        })
    return result


def _query_canonical_entities() -> str:
    """Query existing canonical entity idents for schema-aware prompt injection.

    Returns a formatted string listing up to 50 entity idents and their
    descriptions. Returns empty string if the graph has no entities — in
    that case the caller omits the section from the prompt entirely.

    Uses a two-step approach: first fetches all stored :ident keyword strings,
    then fetches each entity's :description using the keyword ident as a literal.
    This returns proper keyword idents (e.g. :decision/redis) rather than the
    internal UUIDs that join-variable queries would return for ?e.
    """
    try:
        ident_result = handle_minigraf_query("[:find ?id :where [?e :ident ?id]]")
        ident_rows = ident_result.get("results", [])
    except Exception:
        return ""
    if not ident_rows:
        return ""
    lines = []
    for row in ident_rows[:50]:
        kw_ident = row[0] if row else None
        if not isinstance(kw_ident, str) or not kw_ident.startswith(":"):
            continue
        try:
            desc_result = handle_minigraf_query(
                f"[:find ?desc :where [{kw_ident} :description ?desc]]"
            )
            desc_rows = desc_result.get("results", [])
            desc = desc_rows[0][0] if desc_rows else ""
        except Exception:
            desc = ""
        if desc:
            lines.append(f"  {kw_ident} — {desc}")
    return "\n".join(lines)


def _extract_entities(text: str) -> List[str]:
    """Extract candidate entity tokens from user message text."""
    tokens = text.lower().split()
    result = []
    for t in tokens:
        stripped = t.strip(".,?!;:\"'()[]")
        if len(stripped) >= _MIN_ENTITY_LEN and stripped not in _STOP_WORDS:
            result.append(stripped)
    return result


# Rows that restate an entity's identity rather than saying anything about it.
# The entity is already each line's head, so echoing these is pure noise.
_PREPARE_OMITTED_ATTRIBUTES = frozenset({":ident", ":entity-type"})


def _format_memory_entities(results: List[List[str]], max_entities: int) -> str:
    """Collapse ranked fact-index rows into one line per entity (#354).

    Entities keep the rank of their best-matching row; at most max_entities
    lines are produced. Each line lists the entity's matched attribute/value
    pairs once (exact repeats dropped), with a historical value labeled by its
    validity window so the agent still has what it needs for a precise
    :as-of/:valid-at follow-up query. :ident/:entity-type rows are omitted,
    but an entity matched only through them still gets its (bare) line.
    """
    grouped: Dict[str, List[str]] = {}
    for entity, attribute, value, valid_from, valid_to in results:
        if entity not in grouped:
            if len(grouped) >= max_entities:
                continue
            grouped[entity] = []
        if attribute in _PREPARE_OMITTED_ATTRIBUTES:
            continue
        part = f"{attribute.lstrip(':')}: {value}"
        if valid_to is not None:
            part += f" [was valid {valid_from} → {valid_to}]"
        if part not in grouped[entity]:
            grouped[entity].append(part)
    return "\n".join(
        f"  {entity}" + (f" | {'; '.join(parts)}" if parts else "")
        for entity, parts in grouped.items()
    )


def _now_utc_ms() -> str:
    """Return current UTC time as an ISO 8601 string with millisecond precision and Z suffix.

    minigraf requires UTC (no timezone offsets) and millisecond precision to
    reliably find facts transacted in the same second as the query.
    e.g. "2026-05-02T15:44:52.184Z"
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _rebuild_index_from_graph() -> None:
    """One-time full rebuild: rescan the graph's full history (not just the
    current-valid snapshot) and write it into a fresh fact_index table, with
    each fact's validity window preserved -- this is what makes a closed/
    retracted entity's facts recoverable as labeled historical entries after
    an index file is lost or was never built. This is the only place a full
    Datalog rescan happens post-launch (everywhere else is incremental via
    _transact/_retract) -- triggered by fact_index.needs_backfill().

    Uses two separate queries rather than one combined query, deliberately:
    a query combining a bound clause ([?e :ident ?ident]) with a free clause
    sharing the same entity variable ([?e ?a ?v]) is a strictly riskier
    Datalog shape to depend on than it looks -- its join semantics for a
    shared free variable aren't something this codebase documents or tests
    elsewhere, unlike a plain single-purpose lookup query. Concretely,
    against a stale minigraf==1.1.1 (older than this project's pinned
    minigraf>=1.2.1 floor, but observed installed on one dev machine's
    non-project Python), that combined-clause query collapsed to returning
    only the single triple that satisfied the bound clause, discarding
    every other fact the entity had -- not reproduced under the pinned
    1.2.1. The two-query form removes the dependency on that join shape
    either way, at the cost of one extra query on this rarely-run path.
    _preload_known_entities never combines the two: it always names every
    attribute explicitly (?path, ?desc, ?date) instead of using a free
    [?e ?a ?v] clause, so matching its clause *ordering* alone (which this
    function's first draft did) doesn't carry over the same safety
    guarantee -- the free-vs-named-clause distinction is what actually
    matters, not just where :ident appears in the :where list.

    The fix: query 1 builds a UUID -> keyword-ident lookup table using ONLY
    the bound clause (no free clause combined). Query 2 is the bare, already
    independently-verified-correct full scan (see #141's root-cause note:
    binding ?e directly yields minigraf's internal UUID, not the keyword
    ident). Substituting the ident where known (falling back to the raw
    UUID otherwise) in Python gives full fact content for every entity,
    idented or not -- unlike a dropped fact, an entity recovered under its
    raw UUID just isn't boost-eligible (never starts with a
    fact_index._MEMORY_PREFIXES keyword), which accurately reflects that its
    true keyword ident is unrecoverable from the graph alone once written
    without an explicit :ident fact.

    Query 1 now also adds :any-valid-time so a HISTORICAL entity's ident can
    still be recovered during backfill: without it, only currently-idented
    entities would resolve, and a closed/removed entity's historical rows
    would fall back to their raw UUID instead of the correct ident, even
    though the entity itself was idented before it was closed. This doesn't
    reintroduce the free-vs-named-clause risk documented above -- it only
    changes which facts are visible to the bound-only lookup, not its join
    shape.

    Query 2 now also projects each fact's validity window via minigraf's
    :db/valid-from/:db/valid-to pseudo-attributes, combined with a free
    [?e ?a ?v] clause and :any-valid-time (to see retracted/historical facts
    at all, not just current ones). This exact combination -- pseudo-attrs
    joined to a FREE clause, not a named one like _preload_known_deps uses
    -- was not previously exercised anywhere in this codebase and was
    spike-tested directly against the real, pinned minigraf>=1.2.1 before
    being relied on here (see the 2026-07-18 design doc): confirmed correct
    per-fact window binding (no collapse/cross-contamination) and confirmed
    :any-valid-time does not duplicate a retracted-then-bounded-re-transacted
    fact as a ghost row alongside its historical replacement.

    A row's ?vt equal to _VALID_TIME_FOREVER_MS means still-open (current,
    valid_to=None in the index); any other value means historical
    (valid_to=ISO(?vt)). ms->ISO conversion reuses the exact pattern
    _preload_known_deps already uses, rather than duplicating it.
    """
    with db_lease() as db:
        ident_raw = _db_execute(
            db, '(query [:find ?e ?ident :any-valid-time :where [?e :ident ?ident]])'
        )
        facts_raw = _db_execute(
            db,
            "(query [:find ?e ?a ?v ?vf ?vt :any-valid-time "
            ":where [?e ?a ?v] [?e :db/valid-from ?vf] [?e :db/valid-to ?vt]])",
        )
    ident_map = {e: ident for e, ident in json.loads(ident_raw).get("results", [])}

    triples = []
    for e, a, v, vf_ms, vt_ms in json.loads(facts_raw).get("results", []):
        entity = ident_map.get(str(e), str(e))
        vf_iso = (
            datetime.datetime.fromtimestamp(int(vf_ms) / 1000, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        if int(vt_ms) == _VALID_TIME_FOREVER_MS:
            vt_iso = None
        else:
            vt_iso = (
                datetime.datetime.fromtimestamp(int(vt_ms) / 1000, datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            )
        triples.append((entity, str(a), str(v), vf_iso, vt_iso))
    path = fact_index.index_path_for(_graph_path_current())
    fact_index.rebuild_index(path, triples)


async def _run_startup_backfill() -> None:
    """Eagerly check-and-run the fact-index backfill from the long-lived
    server process at startup (#147), mirroring main()'s auto-start-ingestion
    pattern -- offloaded to a worker thread so the (potentially slow, full
    graph rescan) work never blocks the stdio handshake.

    Without this, backfill only ever ran lazily inside
    handle_memory_prepare_turn, which is very often invoked from the
    UserPromptSubmit hook's short-lived, timeout-bound process: a slow rescan
    there trips the timeout and retry-storms on every subsequent turn instead
    of ever completing. (The bound is 30 s since #344; the 5000 configured
    before that was read by Claude Code as SECONDS, not the intended 5 s.)

    The lease taken by _rebuild_index_from_graph releases the graph's file lock
    when the rebuild returns, so the prepare_hook subprocess can still acquire
    it between turns. Without that, a rebuild triggered here would leave the
    persistent server process holding the lock indefinitely -- reproducing this
    issue's own failure mode by lock contention instead of a slow rescan.
    """
    path = fact_index.index_path_for(_graph_path_current())
    loop = asyncio.get_running_loop()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as backfill_executor:
            needs = await loop.run_in_executor(backfill_executor, fact_index.needs_backfill, path)
            if needs:
                await loop.run_in_executor(backfill_executor, _rebuild_index_from_graph)
    except Exception as e:
        print(f"[fact_index] startup backfill failed: {e}", file=sys.stderr)


_NAV_TASK_VERBS = re.compile(
    r"\b(?:add(?:ing|ed|s)?|implement(?:ing|ed|s)?|build(?:ing|s)?|built|"
    r"fix(?:ing|ed|es)?|debug(?:ging|ged|s)?|refactor(?:ing|ed|s)?)\b",
    re.IGNORECASE,
)
_NAV_TASK_PHRASES = re.compile(
    r"\b(?:where\s+is|where's|how\s+does|how\s+do)\b", re.IGNORECASE
)
_NAV_TASK_NOUNS = re.compile(
    r"\b(?:code|function|method|class|module|file|bug|feature|endpoint|api|"
    r"service|component|test|logic|handler|query|database|schema|route|"
    r"script|implementation)\b",
    re.IGNORECASE,
)

_NAV_NUDGE = (
    'This repo has an ingested code graph. Consider minigraf_query for impact '
    '(reverse :depends-on plus the reachable rule) and co-change precedent '
    '(shared :introduced-by/:modified-in commits) before diving in -- see '
    'SKILL.md\'s "Using ingested code structure to scope a change" section.'
)


def _looks_like_navigation_task(user_message: str) -> bool:
    """Heuristic match for a build/fix/navigate task shape (#220): a task
    verb (add/implement/build/fix/debug/refactor, including common
    inflections like "fixing"/"fixed"/"debugged") or a navigation phrase
    (where is/how does) combined with a code-ish noun -- the noun
    requirement keeps everyday phrasing that happens to share a verb (e.g.
    "fix dinner") from triggering the nudge.
    """
    if not (_NAV_TASK_VERBS.search(user_message) or _NAV_TASK_PHRASES.search(user_message)):
        return False
    return bool(_NAV_TASK_NOUNS.search(user_message))


def handle_memory_prepare_turn(user_message: str) -> str:
    """Query the persisted fact index for facts relevant to the user message,
    including labeled historical (retracted/superseded) facts -- the index
    is the entry point into history, the bi-temporal graph is the archive.
    Also appends a lightweight code-graph navigation nudge (#220) on
    build/fix/navigate-shaped messages, gated on ingestion being present --
    read from the fact index, so the steady-state path takes no graph lease
    at all (#353).

    Only memory facts (:decision/ :preference/ :constraint/ :dependency/) are
    injected (#354). Ingested code-graph rows matched on common words and
    were nearly all noise -- ~50 rows every turn -- so code structure is left
    to minigraf_query and the nudge. The filter is applied at QUERY time; the
    index keeps every row as #302's audit witness. Up to
    MINIGRAF_PREPARE_SCAN_LIMIT rows are scanned and collapsed into at most
    MINIGRAF_PREPARE_MAX_ENTITIES lines, one per entity.

    Returns a formatted context block string for injection as
    additionalContext, or an empty string if no relevant facts are found.
    Proactively checks fact_index.needs_backfill() before querying (fresh
    install, pre-existing graph, corruption recovery, or a write that raced
    ahead of the first read all leave the index in a needs-backfill state --
    see the 2026-07-18 design doc for why file-existence alone is not a
    reliable signal).
    """
    scan_limit = int(os.environ.get("MINIGRAF_PREPARE_SCAN_LIMIT", "50"))
    max_entities = int(os.environ.get("MINIGRAF_PREPARE_MAX_ENTITIES", "8"))
    boost = float(os.environ.get("MINIGRAF_MEMORY_BOOST", "2.0"))
    historical_discount = float(os.environ.get("MINIGRAF_HISTORICAL_DISCOUNT", "0.5"))
    path = fact_index.index_path_for(_graph_path_current())
    memory_block = ""
    try:
        if fact_index.needs_backfill(path):
            _rebuild_index_from_graph()
        results = fact_index.query_facts(
            path, user_message, top_n=scan_limit, boost=boost,
            historical_discount=historical_discount, memory_only=True,
        )
        if results:
            memory_block = (
                "Relevant memory context:\n"
                f"{_format_memory_entities(results, max_entities)}"
            )
    except Exception as e:
        print(f"[fact_index] prepare_turn failed: {e}", file=sys.stderr)

    nav_nudge = ""
    if _looks_like_navigation_task(user_message):
        # Answered from the fact index, never a graph lease (#353): the lease
        # cost a handle open plus an aggregate scan on every nav-shaped
        # prompt, and under contention blocked ~3.1 s and then lost the nudge.
        try:
            if fact_index.has_commit_entities(path):
                nav_nudge = _NAV_NUDGE
        except Exception as e:
            print(f"[prepare_turn] navigation nudge check failed: {e}", file=sys.stderr)

    return "\n\n".join(part for part in (memory_block, nav_nudge) if part)


# ---------------------------------------------------------------------------
# Fact extraction — heuristic strategy
# ---------------------------------------------------------------------------

_SIGNAL_PATTERNS = [
    # Each pattern captures a single token after the signal phrase. Articles ("a", "the", etc.)
    # will match first if present (e.g. "depends on the auth-service" → captures "the"), but
    # the stop-word filter below drops them, producing zero facts for that phrase. Users should
    # write "depends on auth-service" (no article) to ensure capture.
    (r"we(?:'ll?|\s+will)\s+use\s+([\w\-]+)", "decision", ":description", "chosen technology or approach"),
    (r"going\s+with\s+([\w\-]+)", "decision", ":description", "chosen approach"),
    (r"decided\s+(?:to\s+)?(?:use\s+)?([\w\-]+)", "decision", ":description", "decided approach"),
    (r"we\s+chose\s+([\w\-]+)", "decision", ":description", "chosen option"),
    (r"I\s+prefer\s+([\w\-]+)", "preference", ":description", "stated preference"),
    (r"I\s+don'?t\s+like\s+([\w\-]+)", "preference", ":description", "stated dislike"),
    (r"always\s+use\s+([\w\-]+)", "preference", ":description", "always-use preference"),
    (r"never\s+use\s+([\w\-]+)", "preference", ":description", "never-use preference"),
    (r"prioritize\s+([\w\-]+)", "preference", ":description", "priority preference"),
    (r"must\s+be\s+([\w\-]+)", "constraint", ":description", "hard constraint"),
    (r"can'?t\s+use\s+([\w\-]+)", "constraint", ":description", "exclusion constraint"),
    (r"depends\s+on\s+([\w\-]+)", "dependency", ":description", "dependency relationship"),
    (r"requires?\s+([\w\-]+)", "dependency", ":description", "required dependency"),
]


def heuristic_extract(text: str) -> List[Dict[str, str]]:
    """
    Scan text for decision-signal phrases and return a list of fact dicts.
    Each dict has keys: entity, attribute, value, reason.
    """
    facts = []
    seen_values: set = set()

    for pattern, entity_type, attribute, reason_prefix in _SIGNAL_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = match.group(1).strip()
            if len(value) < 2 or value.lower() in _STOP_WORDS:
                continue
            key = (entity_type, value.lower())
            if key in seen_values:
                continue
            seen_values.add(key)
            entity_ident = _canonical_ident(entity_type, value)
            facts.append({
                "entity": entity_ident,
                "entity_type": entity_type,
                "attribute": attribute,
                "value": value,
                "reason": f"{reason_prefix} — extracted by heuristic strategy",
            })

    return facts


def _transact_extracted_facts(facts: List[Dict[str, str]], valid_from: Optional[str] = None) -> int:
    """
    Transact a list of extracted fact dicts. Returns count of successfully stored facts.

    Sets :valid-from to the current UTC ms timestamp on every write so that
    valid-time is recorded. Combined with :as-of in queries this enables true
    bi-temporal point-in-time reads.

    valid_from: override the :valid-from timestamp (ISO 8601). If None, defaults
    to the current UTC time. Pass a past date to backdate facts (e.g. from
    LLM-annotated '; valid-at: YYYY-MM-DD' hints).

    Validation is done per-entity, not per-fact: facts are grouped by entity
    before validation so that sibling facts for the same entity (e.g. a
    :description triple and a separate :alias triple, which is how Datalog
    triples and this function's own extraction prompts always shape
    multi-attribute entities) are checked together. An entity with
    :description present anywhere in its group passes the required-attribute
    check even though any single triple examined in isolation would look
    incomplete; an entity with no :description anywhere in the batch is still
    correctly rejected. (Validating fact-by-fact instead of entity-by-entity
    was a latent bug: it silently dropped every optional-attribute-only fact
    -- :alias, :rationale, :date -- whenever it arrived as its own triple
    rather than bundled into the same dict as :description.)
    """
    stored = 0

    entity_groups: Dict[str, List[Dict[str, Any]]] = {}
    for fact in facts:
        entity_groups.setdefault(fact["entity"], []).append(fact)
    invalid_entities = {
        entity for entity, group in entity_groups.items() if _validate_facts(group)
    }

    with db_lease() as db:
        for fact in facts:
            entity = fact["entity"]
            entity_type = fact.get("entity_type", "")
            attribute = fact["attribute"]
            value = fact["value"]
            # Schema validation — closed-world: skip facts belonging to any entity
            # whose full fact group (across this batch) has violations.
            if entity in invalid_entities:
                continue
            now_z = valid_from or _now_utc_ms()
            try:
                # Combine main fact, :entity-type tag, and :ident into one transact so
                # all triples are written atomically — a single (transact [...]) is one
                # transaction. :ident stores the keyword ident as a string value so that
                # handle_minigraf_audit and _query_canonical_entities can surface it for
                # display without knowing the UUID (audits retract via #uuid "..." syntax).
                escaped_value = _edn_escape(value)
                if entity_type:
                    triples = (
                        f'[{entity} {attribute} "{escaped_value}"]'
                        f' [{entity} :entity-type :type/{entity_type}]'
                        f' [{entity} :ident "{entity}"]'
                    )
                else:
                    triples = f'[{entity} {attribute} "{escaped_value}"]'
                _transact(db, "[" + triples + "]", now_z)
                stored += 1
            except MiniGrafError as e:
                print(
                    f"[_transact_extracted_facts] dropped fact for {entity} {attribute}: {e}",
                    file=sys.stderr,
                )
                continue
        if stored:
            _db_checkpoint(db)
    return stored


# ---------------------------------------------------------------------------
# Fact extraction — llm strategy
# ---------------------------------------------------------------------------

_LLM_EXTRACTION_PROMPT = """You are a memory extraction assistant for a bi-temporal graph database. Review the conversation below and identify any decisions, preferences, constraints, or dependencies that should be stored in long-term memory.

Return ONLY a Datalog transact expression — a list of triples in this exact format:
[[:entity/ident :attribute "value"]
 [:entity/ident :attribute "value"]]

If nothing worth storing was found, return an empty list: []

Allowed entity type prefixes: :decision/ :preference/ :constraint/ :dependency/
Canonical ident form: lowercase, hyphens only — :decision/redis not :decision/Redis_cache.
{canonical_entities_section}
Use these attributes: :description (required), :rationale (optional), :date (optional), :alias (optional).
No other attributes are valid.

IMPORTANT — quoting: if a value itself contains a double-quote character, escape it as \\" so
it doesn't end the string literal early — e.g. :description "she called it \\"the fix\\"".

IMPORTANT — entity resolution: if a reference matches an existing canonical ident or alias above,
reuse that exact ident. Only mint a new ident if the entity is genuinely new.

IMPORTANT — alias generation: for each NEWLY-minted entity (not one you're reusing an
existing ident for), also emit an :alias fact with 2-5 comma-separated alternative
terms, synonyms, or broader concepts a developer might later use to refer to it —
e.g. for a decision to use Redis, `:alias "in-memory data store, key-value cache,
caching backend"`. Retrieval is purely lexical (exact word match), so these aliases
are what let a later, differently-worded query still find this fact.

IMPORTANT — bi-temporality: this database is bi-temporal. Facts have both a transaction time
(when they were recorded) and a valid time (when they were true in the world). When the conversation
mentions that something was decided or true at a specific past date, note that date alongside the
fact so the caller can set :valid-at accordingly. Wrap such facts in a comment line:
; valid-at: 2024-03-15
[[:entity/ident :attribute "value"]]

For point-in-time historical queries, always use :as-of N and :valid-at "date" TOGETHER —
using only one gives a partial view.

Conversation:
{conversation}"""


_LLM_CLIENT_TIMEOUT_SECONDS = float(os.environ.get("MINIGRAF_LLM_TIMEOUT_SECONDS", "30"))


def _get_anthropic_client():
    """Return an Anthropic client. Raises if anthropic package or API key is missing."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed — pip install anthropic")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key, timeout=_LLM_CLIENT_TIMEOUT_SECONDS)


_OPENAI_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4")


def _is_openai_model(model: str) -> bool:
    return any(model.startswith(p) for p in _OPENAI_MODEL_PREFIXES)


def _get_openai_client():
    """Return an OpenAI client. Raises if openai package or API key is missing."""
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai package not installed — pip install openai")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    return openai.OpenAI(api_key=api_key, timeout=_LLM_CLIENT_TIMEOUT_SECONDS)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences that LLMs sometimes wrap around Datalog output.

    Handles both ``` and ```datalog (or any language tag). Returns the inner
    content, stripped. If no fences are present, returns the input unchanged.
    """
    text = text.strip()
    if text.startswith("```"):
        # Drop the opening fence line (``` or ```datalog etc.)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        # Drop the closing fence if present
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _llm_missing_package_warning(error: str) -> str:
    """Return a user-facing install instruction when the LLM package is absent.

    Inspects the error string from _llm_extract_and_transact and maps it to
    the correct pip install command based on the configured model.
    Returns an empty string when the error is not a missing-package error.
    """
    model = os.environ.get("MINIGRAF_LLM_MODEL", "claude-haiku-4-5-20251001")
    if "anthropic package not installed" in error:
        return (
            "ACTION REQUIRED: pip install anthropic\n"
            f"  The configured model '{model}' requires the anthropic package.\n"
            "  Set MINIGRAF_LLM_MODEL in .mcp.json if you want to use an OpenAI model instead."
        )
    if "openai package not installed" in error:
        return (
            "ACTION REQUIRED: pip install openai\n"
            f"  The configured model '{model}' requires the openai package.\n"
            "  Set MINIGRAF_LLM_MODEL in .mcp.json if you want to use an Anthropic model instead."
        )
    return ""


def _call_llm(model: str, prompt: str) -> str:
    """Call an LLM and return the response text. Dispatches to OpenAI or Anthropic by model name."""
    if _is_openai_model(model):
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content
    else:
        client = _get_anthropic_client()
        message = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text


_VALID_AT_LINE_RE = re.compile(r"^;\s*valid-at:\s*", re.IGNORECASE)

# Tried in order; each is a full-string match (no unconverted trailing data),
# so "2024-03-15T10:00:00Z" only matches the datetime formats, not the plain
# date one. %z accepts a bare "Z" suffix as UTC since Python 3.7.
_VALID_AT_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_valid_at_hint(raw: str):
    """Extract optional '; valid-at: <date>' comment from model output.

    Accepts 'YYYY-MM-DD' (zero-padded or not) and full ISO 8601 datetimes,
    normalizing any of them down to a plain 'YYYY-MM-DD' date. Returns
    (valid_at, cleaned_datalog) where valid_at defaults to the current UTC
    ms timestamp if no hint line is present. If a hint line is present but
    its date is unparseable or calendar-invalid (e.g. "2024-13-45"), the
    line is still stripped from the returned datalog and valid_at defaults
    to now, but a warning is printed to stderr so that default is
    distinguishable from "no hint given".
    """
    valid_at = _now_utc_ms()
    kept = []
    for line in raw.splitlines():
        stripped = line.strip()
        match = _VALID_AT_LINE_RE.match(stripped)
        if match:
            date_str = stripped[match.end():].strip()
            parsed = None
            for fmt in _VALID_AT_DATE_FORMATS:
                try:
                    parsed = datetime.datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue
            if parsed is not None:
                valid_at = parsed.strftime("%Y-%m-%d")
            else:
                print(
                    f"[valid-at] unparseable date hint {date_str!r}; "
                    "defaulting valid-at to now",
                    file=sys.stderr,
                )
        else:
            kept.append(line)
    return valid_at, "\n".join(kept).strip()


def _llm_extract_and_transact(conversation_delta: str) -> Dict[str, Any]:
    """Call a lightweight LLM to extract facts. Returns {ok, stored_count, strategy}."""
    try:
        model = os.environ.get("MINIGRAF_LLM_MODEL", "claude-haiku-4-5-20251001")
        canonical = _query_canonical_entities()
        if canonical:
            canonical_entities_section = (
                "\nExisting canonical entities (reuse these idents — do not invent synonyms):\n"
                + canonical
            )
        else:
            canonical_entities_section = ""
        prompt = _LLM_EXTRACTION_PROMPT.format(
            conversation=conversation_delta,
            canonical_entities_section=canonical_entities_section,
        )
        raw_facts = _strip_code_fences(_call_llm(model, prompt))
        if not raw_facts or raw_facts == "[]":
            return {"ok": True, "stored_count": 0, "strategy": "llm"}
        valid_at, datalog = _parse_valid_at_hint(raw_facts)
        if not datalog or datalog == "[]":
            return {"ok": True, "stored_count": 0, "strategy": "llm"}
        # Route through _transact_extracted_facts so each fact gets schema
        # validation and an :entity-type tag — same path as heuristic extraction.
        parsed = _parse_transact_facts(datalog)
        stored_count = _transact_extracted_facts(parsed, valid_from=valid_at)
        return {"ok": True, "stored_count": stored_count, "strategy": "llm"}
    except Exception as e:
        return {"ok": False, "error": str(e), "strategy": "llm"}


# ---------------------------------------------------------------------------
# Fact extraction — agent (MCP sampling) strategy
# ---------------------------------------------------------------------------

_AGENT_SAMPLING_PROMPT = """Review this conversation turn and output ONLY a Datalog transact expression for any decisions, preferences, constraints, or dependencies worth storing in long-term memory.

Allowed entity type prefixes: :decision/ :preference/ :constraint/ :dependency/
Canonical ident form: lowercase, hyphens only — :decision/redis not :decision/Redis_cache.
{canonical_entities_section}
Use these attributes: :description (required), :rationale (optional), :date (optional), :alias (optional).
No other attributes are valid. If an entity matches an existing ident or alias, reuse it exactly.

If a value itself contains a double-quote character, escape it as \\" so it doesn't end the
string literal early — e.g. :description "she called it \\"the fix\\"".

For each newly-minted entity, also emit an :alias fact with 2-5 comma-separated
alternative terms or broader concepts someone might use to refer to it later —
retrieval is purely lexical, so this is what lets a differently-worded query still
find the fact.

Format:
[[:entity/ident :attribute "value"]]

Return [] if nothing is worth storing.

{conversation}"""


async def _request_agent_memory_block_async(conversation_delta: str, canonical_entities_section: str = "") -> str:
    """Use MCP sampling to ask the connected agent for a memory block."""
    if _server_ref is None:
        raise RuntimeError("Server reference not set")
    from mcp.types import SamplingMessage, TextContent as TC
    prompt = _AGENT_SAMPLING_PROMPT.format(
        conversation=conversation_delta,
        canonical_entities_section=canonical_entities_section,
    )
    result = await _server_ref.request_context.session.create_message(
        messages=[SamplingMessage(role="user", content=TC(type="text", text=prompt))],
        max_tokens=512,
    )
    return result.content.text if hasattr(result.content, "text") else str(result.content)


async def _agent_extract_and_transact(conversation_delta: str) -> Dict[str, Any]:
    """Request a memory block from the agent via MCP sampling, then transact it."""
    try:
        canonical = _query_canonical_entities()
        if canonical:
            canonical_entities_section = (
                "\nExisting canonical entities (reuse these idents — do not invent synonyms):\n"
                + canonical
            )
        else:
            canonical_entities_section = ""
        raw_facts = _strip_code_fences(await _request_agent_memory_block_async(conversation_delta, canonical_entities_section))
        if not raw_facts or raw_facts == "[]":
            return {"ok": True, "stored_count": 0, "strategy": "agent"}
        valid_at, datalog = _parse_valid_at_hint(raw_facts)
        if not datalog or datalog == "[]":
            return {"ok": True, "stored_count": 0, "strategy": "agent"}
        # Route through _transact_extracted_facts (same as the LLM strategy)
        # rather than transacting the sampled model's raw text directly --
        # that raw text is unconstrained model output (an injection surface,
        # see #146) and skips schema validation entirely (#153).
        parsed = _parse_transact_facts(datalog)
        stored_count = _transact_extracted_facts(parsed, valid_from=valid_at)
        return {"ok": True, "stored_count": stored_count, "strategy": "agent"}
    except Exception as e:
        return {"ok": False, "error": str(e), "strategy": "agent"}


# ---------------------------------------------------------------------------
# memory_finalize_turn — dispatcher
# ---------------------------------------------------------------------------

async def handle_memory_finalize_turn(conversation_delta: str) -> Dict[str, Any]:
    """
    Extract facts from conversation_delta and transact them.
    Strategy selected via MINIGRAF_EXTRACTION_STRATEGY env var (default: heuristic).
    """
    strategy = os.environ.get("MINIGRAF_EXTRACTION_STRATEGY", "heuristic")
    async with contextlib.AsyncExitStack() as stack:
        if strategy in ("heuristic", "llm", "agent"):
            await stack.enter_async_context(db_lease_async())

        if strategy == "heuristic":
            facts = heuristic_extract(conversation_delta)
            stored = _transact_extracted_facts(facts)
            return {"ok": True, "stored_count": stored, "strategy": "heuristic"}

        if strategy == "llm":
            # _llm_extract_and_transact makes a blocking network call (_call_llm);
            # run it in a worker thread so it can't freeze the shared event loop
            # for other concurrent tool calls (#180), mirroring the executor
            # pattern already used for fact-index rebuild and git ingestion.
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as llm_executor:
                result = await loop.run_in_executor(
                    llm_executor, _llm_extract_and_transact, conversation_delta
                )
            if result["ok"]:
                return result
            # LLM failed — fall back to heuristic and surface a warning so the user
            # can see what went wrong (e.g. missing package, bad API key).
            llm_error = result.get("error", "")
            warning = _llm_missing_package_warning(llm_error)
            facts = heuristic_extract(conversation_delta)
            stored = _transact_extracted_facts(facts)
            response: Dict[str, Any] = {
                "ok": True,
                "stored_count": stored,
                "strategy": "heuristic (llm fallback)",
            }
            if warning:
                response["warning"] = warning
            elif llm_error:
                response["warning"] = f"LLM extraction failed ({llm_error}); fell back to heuristic."
            return response

        if strategy == "agent":
            return await _agent_extract_and_transact(conversation_delta)

        return {"ok": False, "error": f"Unknown strategy: {strategy}"}


def _precompute_file_triples(
    file_path: str,
    extracted: Dict[str, List[str]],
    commit_ident: str,
    known_files: Dict[str, List[str]],
    segment_index: Optional[_SegmentSuffixIndex] = None,
    old_entity_nodes: Optional[Dict[str, Dict[str, Any]]] = None,
    new_entity_nodes: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Pure, per-commit-independent precomputation for _build_code_triples.

    Runs inside _extract_commit on the worker pool. Computes everything that does
    NOT depend on the serially-maintained entity_valid_from/file_deps state:
      - the candidate triple strings for the module/function/class idents this file
        would introduce, ready to use verbatim IF the main thread's diff against
        entity_valid_from decides the ident is genuinely new (see _build_code_triples);
      - the resolved dependency ident for every import in the file, via
        _resolve_module_import against known_files (this commit's own git-ls-tree
        state, not the incrementally-mutated file_entities);
      - (#221) unchanged_idents: idents whose body provably did NOT change in
        this commit, via old_entity_nodes/new_entity_nodes.

    known_files must come from _known_files_at_commit for the SAME commit_hash this
    file was extracted from — it determines what "is_resolved" means here.

    segment_index, if given, must be a _SegmentSuffixIndex built from that same
    known_files — _extract_commit builds it once per commit and passes it here for
    every A/M file so _resolve_module_import's tiers 3a/3b aren't rebuilding it (or
    linear-scanning known_files) once per import.

    old_entity_nodes/new_entity_nodes, if given, are the SAME category-keyed
    ("function"/"class"/"variable"/"field") live tree-sitter node maps
    _extract_commit's own collect_all_nodes already produces from the old
    (parent-blob) and new (this commit's) parse of this file, for rename
    matching. Reused here (#221) as a per-entity body-diff signal: a name
    present in both maps with a matching _normalized_body_hash did NOT
    actually change in this commit, even though the file did. Both default
    to None (treated as {}), so every caller that doesn't have diff context
    gets an empty unchanged_idents -- the same (safe, if overzealous)
    unconditional :modified-in behavior as before this parameter existed.
    """
    module_ident = _code_ident("module", file_path)
    module_candidate_triples = [
        f"[{module_ident} :entity-type :type/module]",
        f'[{module_ident} :ident "{module_ident}"]',
        f'[{module_ident} :description "{_edn_escape(file_path)}"]',
        f'[{module_ident} :path "{_edn_escape(file_path)}"]',
        f"[{module_ident} :introduced-by {commit_ident}]",
    ]

    function_entries: List[Tuple[str, str, List[str]]] = []
    for fn_name in extracted.get("functions", []):
        fn_ident = _code_ident("function", file_path, fn_name)
        function_entries.append((fn_ident, fn_name, [
            f"[{fn_ident} :entity-type :type/function]",
            f'[{fn_ident} :ident "{fn_ident}"]',
            f'[{fn_ident} :description "{_edn_escape(fn_name)}"]',
            f'[{fn_ident} :file "{_edn_escape(file_path)}"]',
            f"[{module_ident} :contains {fn_ident}]",
            f"[{fn_ident} :introduced-by {commit_ident}]",
        ]))

    class_entries: List[Tuple[str, str, List[str]]] = []
    for cls_name in extracted.get("classes", []):
        cls_ident = _code_ident("class", file_path, cls_name)
        class_entries.append((cls_ident, cls_name, [
            f"[{cls_ident} :entity-type :type/class]",
            f'[{cls_ident} :ident "{cls_ident}"]',
            f'[{cls_ident} :description "{_edn_escape(cls_name)}"]',
            f'[{cls_ident} :file "{_edn_escape(file_path)}"]',
            f"[{module_ident} :contains {cls_ident}]",
            f"[{cls_ident} :introduced-by {commit_ident}]",
        ]))

    global_entries: List[Tuple[str, str, List[str]]] = []
    for gvar_name in extracted.get("globals", []):
        gvar_ident = _code_ident("variable", file_path, gvar_name)
        global_entries.append((gvar_ident, gvar_name, [
            f"[{gvar_ident} :entity-type :type/variable]",
            f'[{gvar_ident} :ident "{gvar_ident}"]',
            f'[{gvar_ident} :description "{_edn_escape(gvar_name)}"]',
            f'[{gvar_ident} :file "{_edn_escape(file_path)}"]',
            f"[{module_ident} :contains {gvar_ident}]",
            f"[{gvar_ident} :introduced-by {commit_ident}]",
        ]))

    field_entries: List[Tuple[str, str, List[str]]] = []
    # field_ident -> owning class_ident, but ONLY for fields whose owning class
    # is genuinely extracted as a :type/class entity in this same file. Threaded
    # to close sites via field_class_ident so the class-contains edge is retracted
    # when the field closes.
    field_class_map: Dict[str, str] = {}
    field_static_map: Dict[str, bool] = {}
    extracted_class_names = set(extracted.get("classes", []))
    for field_name, owning_class, is_static in extracted.get("fields", []):
        qualified_name = f"{owning_class}.{field_name}"
        field_ident = _code_ident("field", file_path, qualified_name)
        field_static_map[field_ident] = is_static
        static_literal = "true" if is_static else "false"
        candidate_triples = [
            f"[{field_ident} :entity-type :type/field]",
            f'[{field_ident} :ident "{field_ident}"]',
            f'[{field_ident} :description "{_edn_escape(qualified_name)}"]',
            f'[{field_ident} :file "{_edn_escape(file_path)}"]',
            f"[{field_ident} :static {static_literal}]",
            f"[{module_ident} :contains {field_ident}]",
            f"[{field_ident} :introduced-by {commit_ident}]",
        ]
        # Only emit class-level linkage (:class edge + class :contains edge) when
        # the owning class is a real extracted :type/class entity. Otherwise the
        # owner name (e.g. an Elixir defmodule attribute or a Haskell newtype) is
        # never opened as a class, so a :class edge would dangle and a class
        # :contains edge would point at a nonexistent parent. Module containment
        # alone is kept in that case (see issues.md P2 findings).
        if owning_class in extracted_class_names:
            class_ident = _code_ident("class", file_path, owning_class)
            candidate_triples.append(f"[{field_ident} :class {class_ident}]")
            candidate_triples.append(f"[{class_ident} :contains {field_ident}]")
            field_class_map[field_ident] = class_ident
        field_entries.append((field_ident, qualified_name, candidate_triples))

    resolved_imports: List[Tuple[str, str, bool]] = []
    for import_name in set(extracted.get("imports", [])):
        dep_ident, is_resolved = _resolve_module_import(
            import_name, known_files, importing_file=file_path, segment_index=segment_index,
        )
        resolved_imports.append((import_name, dep_ident, is_resolved))

    # #221: per-entity body-diff signal for _build_code_triples' "already
    # known" branches. A name present in both old_entity_nodes and
    # new_entity_nodes, with an identical normalized (whitespace-insensitive)
    # hash, did NOT actually change here, even though the file did. Absent
    # from either side (a genuinely new/removed entity, a parse failure, or
    # simply no diff context passed) is treated conservatively as changed --
    # this only ever NARROWS which idents get :modified-in, never widens it.
    # Category keys ("function"/"class"/"variable"/"field") are identical to
    # the entity_type string _code_ident expects for each, by construction.
    unchanged_idents: Set[str] = set()
    try:
        old_nodes = old_entity_nodes or {}
        new_nodes = new_entity_nodes or {}
        for category in ("function", "class", "variable", "field"):
            old_cat = old_nodes.get(category, {})
            new_cat = new_nodes.get(category, {})
            for name in old_cat.keys() & new_cat.keys():
                if _normalized_body_hash(old_cat[name]) == _normalized_body_hash(new_cat[name]):
                    unchanged_idents.add(_code_ident(category, file_path, name))
    except Exception:
        unchanged_idents = set()

    return {
        "module_ident": module_ident,
        "module_candidate_triples": module_candidate_triples,
        "function_entries": function_entries,
        "class_entries": class_entries,
        "global_entries": global_entries,
        "field_entries": field_entries,
        "field_class_map": field_class_map,
        "field_static_map": field_static_map,
        "resolved_imports": resolved_imports,
        "unchanged_idents": unchanged_idents,
    }


def _build_code_triples(
    file_path: str,
    extracted: Dict[str, List[str]],
    commit_ts_iso: str,
    entity_valid_from: Dict[str, str],
    entity_descriptions: Dict[str, str],
    file_entities: Dict[str, List[str]],
    commit_ident: str,
    precomputed: Dict[str, Any],
    field_class_ident: Optional[Dict[str, str]] = None,
    field_static_ident: Optional[Dict[str, bool]] = None,
    entity_introduced_by: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Return Datalog triple strings for a file's extracted code entities.

    Stable attributes (:entity-type, :ident, :description, :path/:file,
    :introduced-by, :contains) are written ONCE on first introduction. On
    subsequent modifications only a :modified-in edge is added. This prevents
    bi-temporal fact explosion from N re-assertions of the same attribute
    joining into N² result rows.

    precomputed comes from _precompute_file_triples (see mcp_server.py),
    computed ahead of time in the extraction worker pool — the candidate
    triple strings for a would-be-new entity are a pure function of the
    file's own extracted structure and ident, independent of whether
    entity_valid_from turns out to already know about it. This function's
    only remaining job is the diff against entity_valid_from itself, which
    genuinely needs the serially-maintained state.

    :depends-on edges are written in the commit loop by _run_ingestion as the
    file's imports change, giving them proper bi-temporal bounds.

    entity_introduced_by, when supplied, records ident -> commit_ident at every
    introduction branch -- the same five places entity_valid_from is written,
    and written once for the same reason. Close sites need the commit IDENT to
    retract [ident :introduced-by commit] (#231); entity_valid_from only has
    the timestamp. Optional and defaulting to None because _reverse_apply
    filters this function's :introduced-by output out entirely and owns that
    attribute's write timing itself, so a forward-biased guess must never
    reach its state.
    """
    triples: List[str] = []
    module_ident = precomputed["module_ident"]
    field_class_map = precomputed.get("field_class_map", {})
    field_static_map = precomputed.get("field_static_map", {})
    # #221: idents whose body provably did NOT change this commit (empty for
    # every caller that doesn't pass old_entity_nodes/new_entity_nodes to
    # _precompute_file_triples, preserving today's unconditional behavior).
    unchanged_idents = precomputed.get("unchanged_idents", set())

    is_new_module = module_ident not in entity_valid_from
    # Track all idents for this file (for deletion cleanup)
    idents_for_file = file_entities.setdefault(file_path, [])

    if is_new_module:
        triples += precomputed["module_candidate_triples"]
        if module_ident not in idents_for_file:
            idents_for_file.append(module_ident)
        entity_valid_from[module_ident] = commit_ts_iso
        if entity_introduced_by is not None:
            entity_introduced_by[module_ident] = commit_ident
        entity_descriptions[module_ident] = file_path
    else:
        # Existing module: only record that this commit modified it. NOT
        # gated by unchanged_idents (#221) -- the module IS the file, so any
        # file change is legitimate module-level churn.
        triples.append(f"[{module_ident} :modified-in {commit_ident}]")

    for fn_ident, fn_name, candidate_triples in precomputed["function_entries"]:
        if fn_ident not in entity_valid_from:
            triples += candidate_triples
            if fn_ident not in idents_for_file:
                idents_for_file.append(fn_ident)
            entity_valid_from[fn_ident] = commit_ts_iso
            if entity_introduced_by is not None:
                entity_introduced_by[fn_ident] = commit_ident
            entity_descriptions[fn_ident] = fn_name
        elif fn_ident not in unchanged_idents:
            # Pre-existing function whose body actually changed (#221):
            # record that this commit modified it.
            triples.append(f"[{fn_ident} :modified-in {commit_ident}]")

    for cls_ident, cls_name, candidate_triples in precomputed["class_entries"]:
        if cls_ident not in entity_valid_from:
            triples += candidate_triples
            if cls_ident not in idents_for_file:
                idents_for_file.append(cls_ident)
            entity_valid_from[cls_ident] = commit_ts_iso
            if entity_introduced_by is not None:
                entity_introduced_by[cls_ident] = commit_ident
            entity_descriptions[cls_ident] = cls_name
        elif cls_ident not in unchanged_idents:
            # Pre-existing class whose body actually changed (#221): record
            # that this commit modified it.
            triples.append(f"[{cls_ident} :modified-in {commit_ident}]")

    for gvar_ident, gvar_name, candidate_triples in precomputed["global_entries"]:
        if gvar_ident not in entity_valid_from:
            triples += candidate_triples
            if gvar_ident not in idents_for_file:
                idents_for_file.append(gvar_ident)
            entity_valid_from[gvar_ident] = commit_ts_iso
            if entity_introduced_by is not None:
                entity_introduced_by[gvar_ident] = commit_ident
            entity_descriptions[gvar_ident] = gvar_name
        elif gvar_ident not in unchanged_idents:
            triples.append(f"[{gvar_ident} :modified-in {commit_ident}]")

    for field_ident, field_name, candidate_triples in precomputed["field_entries"]:
        if field_ident not in entity_valid_from:
            triples += candidate_triples
            if field_ident not in idents_for_file:
                idents_for_file.append(field_ident)
            entity_valid_from[field_ident] = commit_ts_iso
            if entity_introduced_by is not None:
                entity_introduced_by[field_ident] = commit_ident
            entity_descriptions[field_ident] = field_name
            # Record the field's real owning-class parent so every close path
            # can retract the [class :contains field] edge alongside the module
            # one. Only fields with an extracted owning class appear in the map.
            if field_class_ident is not None and field_ident in field_class_map:
                field_class_ident[field_ident] = field_class_map[field_ident]
            # Record the field's :static value so its close site can retract it
            # (see _build_close_triples / issue #134) without re-deriving it.
            if field_static_ident is not None and field_ident in field_static_map:
                field_static_ident[field_ident] = field_static_map[field_ident]
        elif field_ident not in unchanged_idents:
            triples.append(f"[{field_ident} :modified-in {commit_ident}]")

    return triples


def _preload_known_entities(
    db: Any,
    repo_path: str,
    valid_at: Optional[str] = None,
    hash_to_pos: Optional[Dict[str, int]] = None,
    watermark_pos: Optional[int] = None,
    ts_positions: Optional[Dict[str, List[int]]] = None,
    t_hi_ms: Optional[int] = None,
    stats: Optional[Dict[str, int]] = None,
) -> tuple:
    """Load all existing module/function/class/external-dependency idents from
    the DB, and pre-seed file_entities with all currently tracked files in the
    repo.

    valid_at + hash_to_pos + watermark_pos together bound this query to the
    graph as it stood at the forward walk's RESUME POSITION. Before the
    two-stream ingest a current-graph preload and a resume-position preload
    were the same thing; the reverse stream broke that by writing structural
    facts across the whole frontier-high region, with Stage B's lifecycle pass
    then applying that region's deletions and renames. Both directions of the
    mismatch corrupt the graph, and they are NOT equally severe:

      * an entity wrongly INCLUDED (born in the reverse region) is absent from
        the parse of the earlier commit being replayed, so it is closed and
        _forget_closed_entity-purged with an orig_ts LATER than the close's
        valid_to -- an inverted valid interval. UNRECOVERABLE.
      * an entity wrongly EXCLUDED (closed in the reverse region) makes replay
        take _build_code_triples' introduction branch and mint a second live
        :introduced-by. RECOVERABLE -- #235's correction sweep repairs it, and
        a still-provisional entity is reconciled in place by
        _forward_reconcile_provisional rather than duplicated.

    Wrong-inclusion is caused SOLELY by the introduction end, and the
    introduction position is exactly recoverable: [?e :introduced-by ?c]
    [?c :hash ?hash] -> hash_to_pos[?hash]. So watermark_pos closes the
    unrecoverable direction exactly, with no fact-model change (#238).

    The close END is recovered too, as of #245's work, but by a different
    route: _ingest_close holds no reference to the closing commit, yet it
    records valid_to = commit_ts_iso, so the closing POSITION comes from
    inverting that instant against commit_metadata (_fact_is_live_at_position).
    An earlier version of this docstring called the close end "not recoverable
    at all"; that was true of joins and false of inversion.

    valid_at survives as phase 1's bound and is fed the monotone envelope
    T_hi(W) = max(ts[0..W]). It no longer carries a safety property in either
    direction: phase 2 below re-admits the entities it drops, gated on
    position. Passing ts_positions/t_hi_ms is what enables phase 2 --
    test_readmission_is_position_gated_not_date_gated pins that the
    re-admission comes from the position rule and not from a widened date
    bound, which is the "add-back union" #238 forbids.

    Residual, and it is no longer about membership: phase 2 re-admits exactly
    the entity this paragraph used to describe as excluded (introduced at or
    below W, deleted or renamed above W, close date earlier than T_hi(W)).
    Membership is position-exact in BOTH directions now.

    What survives is VALUES. entity_descriptions still carries whichever
    :description version was live at DATE T_hi(W), which can be a version
    written above W with an inverted author date. Membership is position-exact;
    values are not. That is #257 -- CLOSED as an ACCEPTED residual, not fixed.
    The xfail(strict=True) in tests/test_mcp_server.py's
    TestPreloadKnownEntitiesDescriptionValueIsDateBounded pins that the shipped
    query really does return the future value, so any fix trips the suite; do
    not remove that marker to make it green.

    Why accepting it is defensible, and what would make it fire. The defect
    needs ONE ident carrying TWO distinct :description values in disjoint
    windows. Every write site here is guarded by "ident not currently live"
    (_build_code_triples' introduction branches; the unresolved-import stub at
    the `dep_ident not in state.entity_valid_from` gate; the gitlink "add"
    handler), so a second value requires a CLOSE-THEN-REINTRODUCE from a
    different raw source value -- not merely a second writer. Two arms survive
    #263:

      1. A submodule REMOVED and re-ADDED at the same path with its .gitmodules
         name changed in between. NOT a rename: _gitlink_changes classifies
         purely from the gitlink tree entry's old/new modes and never reads
         .gitmodules, gitmodules_map is only fetched when a commit carries an
         "add", and the "bump" branch writes :pinned-commit and :modified-in
         only. So a name-only .gitmodules edit produces no event at all and
         cannot rewrite :description. Unmeasurable on this repository: every
         full-history sweep so far (#245, #257, #263) found 0 gitlink events,
         the same blind spot #245 recorded for :pinned-commit.
      2. A residual _canonical_ident slug collision, because the :description
         at these sites is the PRE-SLUG raw value -- module -> file_path,
         unresolved import -> the raw specifier, function/class/variable/field
         -> the raw name. So ident -> description is injective only if the slug
         is, and R3's zero is MEASURED over 674 commits, not proven: `a/b.py`
         and `a-b.py` both reach :module/a-b-py. This arm is why "the value is
         a deterministic function of the ident" is true of the INPUTS and not
         of the ident string.

    Fixing it means position-filtering an INTERVAL per attribute, not
    inverting one valid_to the way the close side does. Do not build that
    without evidence the mechanism fires; there is none as of 2026-08-14.

    What the consequence is NOT: body-change detection. An earlier version of
    this paragraph claimed the forward walk diffs descriptions to decide
    whether a body changed, so a from-the-future value would make a real change
    compare equal and go unrecorded. That is false. Body-change detection is
    unchanged_idents, computed in _precompute_file_triples from the parsed node
    text (#221) and consumed by _build_code_triples; entity_descriptions is
    only ever WRITTEN there. Every READ of it in _forward_apply feeds
    _build_close_triples' `desc` argument.

    What the consequence IS: `desc` is the value _build_close_triples retracts
    when an entity closes. A wrong value there retracts a :description fact
    that was never asserted, so the one that IS live never gets closed and
    stays live past its window -- a stale-fact bug, not a lost body edit.
    MEASURED on this repository's history and recorded in
    evals/at_scale/results/257-description-preload-exposure.json (the #245
    probe measured membership only); read that artifact's limitations before
    treating its mismatch count as a bound.

    _preload_known_deps and _preload_pinned_commits are position-filtered the
    same way (#245). This function's close side is what made their exposure
    look 16x smaller than it is: four modules deleted above the watermark with
    an inverted close date dropped out of file_entities, taking 30
    misclassified :depends-on edges with them before any diff was computed.

    Five optional position parameters now gate this function's behaviour, not
    three. `valid_at=None` disables phase 1's bound outright (unbounded
    query). `watermark_pos=None` forces `t_hi_ms` None too (see the assertion
    in `_load_ingestion_preload_state`) and makes `position_mode` False,
    which disables phase 2 entirely -- `hash_to_pos` and `ts_positions` then
    go unused, since both are only read inside branches `watermark_pos is
    not None` guards. So `valid_at=None, watermark_pos=None` (with
    `hash_to_pos`/`ts_positions`/`t_hi_ms` following as a consequence, not
    independently) is what actually restores the pre-#222 behaviour for a
    fresh graph with no watermark.

    external-dependency entities share the module ident namespace and use the
    same "path" attribute as modules, so folding them into this same query
    means the existing close/reopen machinery (entity_valid_from,
    entity_descriptions) just works for submodules without new parallel state.
    Unresolved-import placeholders (no :path) are not reloaded by this query —
    nothing in this codebase ever closes one, so the gap is harmless; see the
    design spec's Section 2. (submodule_paths below deliberately reuses this
    same :path-bearing external-dependency row set — see its own docstring.)

    Pre-seeding from `git ls-files` ensures that _resolve_module_import can
    find any module file even when processing early commits — before those files
    have been introduced in the chronological commit walk.

    Returns (entity_valid_from, entity_descriptions, entity_introduced_by,
    file_entities, submodule_paths). entity_introduced_by maps ident -> the
    :commit/... ident that introduced it, derived from the same ?hash the
    position clause uses -- #231's close-time retract value. submodule_paths
    stays LAST: an existing test destructures with `*_, submodule_paths`.
    """
    entity_valid_from: Dict[str, str] = {}
    entity_descriptions: Dict[str, str] = {}
    entity_introduced_by: Dict[str, str] = {}
    file_entities: Dict[str, List[str]] = {}
    submodule_paths: Dict[str, str] = {}

    # Pre-seed file_entities with all files currently in the repo
    try:
        result = _subprocess.run(
            ["git", "ls-files", "--full-name"],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        for filepath in result.stdout.strip().splitlines():
            if Path(filepath).suffix.lower() in _EXT_TO_LANG:
                file_entities.setdefault(filepath, [])
    except Exception:
        pass

    def _collect(valid_at_str: Optional[str], accept) -> None:
        """Run the structural preload query at one instant and fold accepted
        rows into the shared output dicts.

        accept(ident) -> bool selects rows; the introduction position clause
        is applied here, so it governs phase 1 and every re-admission pass
        alike.
        """
        valid_at_clause = (
            f':valid-at "{_edn_escape(valid_at_str)}" ' if valid_at_str else ""
        )
        for entity_type in (
            "module", "function", "class", "variable", "field",
            "external-dependency",
        ):
            path_attr = (
                "path" if entity_type in ("module", "external-dependency")
                else "file"
            )
            try:
                raw = _db_execute(
                    db,
                    f'(query [:find ?ident ?path ?desc ?date ?hash '
                    f'{valid_at_clause}'
                    f':where [?e :entity-type :type/{entity_type}] '
                    f'[?e :ident ?ident] '
                    f'[?e :{path_attr} ?path] '
                    f'[?e :description ?desc] '
                    f'[?e :introduced-by ?c] '
                    f'[?c :date ?date] '
                    f'[?c :hash ?hash]])',
                )
                rows = json.loads(raw).get("results", [])
                for ident, path, desc, date, hash_ in rows:
                    if not accept(ident):
                        continue
                    # #238: the resume bound, POSITION-indexed. CONJUNCTIVE
                    # over every row, in every pass. Wrong-INCLUSION (the
                    # unrecoverable direction) is caused solely by the
                    # introduction end, which this closes exactly. Never turn
                    # this into an "add-back" branch beside a date bound.
                    #
                    # pos is None means the introducing commit is not in this
                    # linearization (a rewritten or foreign history): exclude,
                    # which is the benign direction.
                    if watermark_pos is not None:
                        pos = (
                            hash_to_pos.get(hash_)
                            if hash_to_pos is not None else None
                        )
                        if pos is None or pos > watermark_pos:
                            continue
                    entity_valid_from[ident] = date
                    entity_descriptions[ident] = desc
                    # Reconstructed from ?hash, not read off a bound ?c:
                    # ?c is a SUBJECT variable here, and binding a subject in
                    # :find position returns minigraf's internal UUID, not the
                    # keyword ident string -- verified empirically for ?c
                    # specifically (adding it to :find returns a UUID like
                    # "bd5e9774-8fbd-5ec4-81e8-7310073fa5c3"), the same reason
                    # _preload_known_deps binds ?srci and
                    # _preload_pinned_commits binds ?ei. This value becomes
                    # the :introduced-by retract value at close time (#231),
                    # so it must stay byte-for-byte identical to the
                    # commit_ident both write sites build;
                    # test_entity_introduced_by_matches_commit_write_site
                    # pins that against the real write sites.
                    entity_introduced_by[ident] = f":commit/{hash_[:12]}"
                    file_entities.setdefault(path, [])
                    if ident not in file_entities[path]:
                        file_entities[path].append(ident)
                    if entity_type == "external-dependency":
                        submodule_paths[ident] = path
            except Exception:
                pass

    # Phase 1: everything live at the envelope.
    _collect(valid_at, lambda ident: True)

    # Phase 2 (#238 close side, #245): entities phase 1 missed because their
    # close DATE fell at or below the envelope while their close POSITION sits
    # above the watermark.
    #
    # `[(<= ?vt t_hi_ms)]` is NOT a sound bound in isolation -- a close above W
    # can carry an arbitrarily early date, which is the defect. It is sound
    # only as the COMPLEMENT of phase 1: an entity missing from a
    # :valid-at T_hi(W) query has either vt <= T_hi(W) or vf > T_hi(W), and
    # vf > T_hi(W) implies intro_pos > W (every position at or below W has a
    # date at or below the envelope) and is correctly excluded. Never lift
    # this clause into a standalone query.
    #
    # Only :ident's own window is bound here, not :path or :description:
    # :ident is written once per entity lifetime, so this is roughly one
    # interval per entity, while :description is rewritten on every body edit
    # and would explode the row count under :any-valid-time.
    position_mode = (
        watermark_pos is not None
        and ts_positions is not None
        and t_hi_ms is not None
    )
    if position_mode:
        readmit: Dict[str, int] = {}
        for entity_type in (
            "module", "function", "class", "variable", "field",
            "external-dependency",
        ):
            try:
                raw = _db_execute(
                    db,
                    f'(query [:find ?ident ?vf ?vt :any-valid-time '
                    f':where [?e :entity-type :type/{entity_type}] '
                    f'[?e :ident ?ident] '
                    f'[?e :db/valid-from ?vf] '
                    f'[?e :db/valid-to ?vt] '
                    f'[(<= ?vt {t_hi_ms})]])',
                )
                for ident, vf_ms, vt_ms in json.loads(raw).get("results", []):
                    if ident in entity_valid_from:
                        continue
                    # vf here is the INTERVAL's own start, used for interval
                    # selection among an ident's several lifetimes. The
                    # AUTHORITATIVE introduction gate is _collect's
                    # :introduced-by position clause, applied independently to
                    # every re-admitted row below. Both gates apply.
                    if not _fact_is_live_at_position(
                        vf_ms, vt_ms, watermark_pos, ts_positions, stats
                    ):
                        continue
                    readmit[ident] = vt_ms
            except Exception:
                pass

        # One re-admission pass per DISTINCT closing instant, at the last
        # instant those entities were live. On the repository this was
        # developed against that is a single extra query, for df6b8be.
        #
        # Known uncovered hole (verified empirically against a real graph,
        # deliberately left unfixed here): an entity whose :ident interval
        # has valid_from at position W and valid_to ABOVE W but with an
        # EARLIER date is collected into `readmit` above and judged live by
        # _fact_is_live_at_position, but this pass's instant --
        # ISO(close_ms - 1) -- precedes that entity's own valid_from, so
        # _collect finds none of its facts and it is silently dropped.
        # Reachable whenever an entity is introduced at a late-dated commit
        # and deleted at an earlier-dated commit above it: the same
        # author-date inversion #238 exists for, mirrored onto introduction
        # instead of closing. Wrong-exclusion, therefore recoverable on a
        # later resume, and out of scope for #238/#245 -- but undocumented
        # before this comment.
        for close_ms in sorted(set(readmit.values())):
            # _epoch_ms_to_iso forbids the _VALID_TIME_FOREVER_MS sentinel
            # (see its own docstring) and this call sits outside any try, so
            # an uncaught raise here would abort an entire ingestion. It is
            # safe only because the `[(<= ?vt {t_hi_ms})]` clause in the
            # query above already filters the sentinel out of `readmit`'s
            # values at query level -- close_ms can never be the sentinel by
            # the time it reaches this line.
            _collect(
                _epoch_ms_to_iso(close_ms - 1),
                lambda ident, _c=close_ms: (
                    readmit.get(ident) == _c and ident not in entity_valid_from
                ),
            )

    return (
        entity_valid_from, entity_descriptions, entity_introduced_by,
        file_entities, submodule_paths,
    )


def _preload_unresolved_dep_idents(
    db: Any, valid_at: Optional[str] = None
) -> Dict[str, str]:
    """Reload ident -> import_name for every unresolved-import stub (#112).

    _preload_known_entities' external-dependency branch requires a :path fact,
    which only real submodule entities have — unresolved-import stubs (see
    _forward_apply's dep-edge handling) never get one, so they're invisible to
    that query. This runs the same :entity-type match WITHOUT the :path
    requirement, then subtracts the idents that DO have a :path to get exactly
    the stub idents, restart-safe.

    Needed so a submodule added in a later, separate ingestion run can still
    find and link any stub created in an earlier run (see the gitlink "add"
    handling in _forward_apply) — without this, only same-run stubs would ever
    get linked.

    The subtrahend is the UNION of two queries, deliberately not
    _preload_known_entities' submodule_paths (#238). Stub-ness is "has no
    :path" — a property of the entity, not of the resume position — and
    submodule_paths is filtered by the introducing commit's LINEARIZATION
    POSITION (hash_to_pos[hash] <= watermark_pos), not by date. That
    position bound is exactly what makes this reachable: because commit
    author-dates are not monotonic in topological order (a rebase,
    cherry-pick or side branch can carry an earlier author-date onto a
    later position — measured on this repo, 6 of 552 watermark positions
    have a strictly-earlier-dated later position), a real submodule's own
    facts can be dated below ts(W) — so this function's DATE-bounded minuend
    includes it — while its introducing commit sits at a POSITION above the
    watermark — so a position-filtered subtrahend excludes it anyway. That
    misclassifies it as a stub, reaching state.unresolved_dep_idents, where
    the replayed gitlink "add" handler's _submodule_path_matches_import
    check can fire on it (a submodule's :description is `name or path`) and
    mint a bogus [:module/sub-b :resolves-to :module/sub-a]. This is not a
    date-bound mismatch; a shared date bound on both sides would not
    reproduce it (round-2 review caught an earlier draft of this reasoning,
    and the test file's docstring, making exactly that substitution).

    Final-review Finding 1: an unbounded-only subtrahend opened a SECOND
    direction of the same failure. Its temporal base disagreed with the
    minuend's own ts(W) bound below, and that mismatch has its own concrete
    trigger: a submodule live AT OR BELOW the watermark whose gitlink is
    removed ABOVE it. A prior run's Stage B _forward_apply(lifecycle_only=
    True) calls _build_close_triples(..., file_value=path), retracting the
    entity's :path (and :ident/:description) — so it drops out of an
    unbounded-only subtrahend (no longer live at current time), while the
    minuend below still sees it (:ident/:description/:path were all still
    live at ts(W)). That dead submodule then reaches
    state.unresolved_dep_idents, where a later gitlink "add" whose path
    prefixes its :description mints a bogus :resolves-to edge — but this
    time pointing at a CLOSED entity, the exact thing #112's dedup exists to
    prevent. Fixed by widening the subtrahend to the UNION of the unbounded
    query and a second, :valid-at ts(W)-bounded :path query — matching the
    minuend's own temporal base for exactly this case, while leaving the
    unbounded disjunct in place for the ordinary (still-live) case. See
    test_removed_above_watermark_submodule_is_not_misclassified_as_a_stub,
    which fails against an unbounded-only subtrahend.

    A ts(W)-ONLY subtrahend (replacing the unbounded disjunct rather than
    unioning with it) would instead regress the FIRST direction: it would
    reintroduce exactly the position-inversion failure
    test_above_watermark_submodule_is_not_misclassified_as_a_stub and
    test_position_inverted_submodule_is_not_misclassified_as_a_stub pin,
    where a real submodule introduced above the watermark (so absent at
    ts(W)) but still live right now would drop back out of the subtrahend.
    The union is required, not either bound alone.

    The MINUEND keeps the narrow ts(W) bound rather than #238's widened
    envelope, because this set's asymmetry runs the other way from
    _preload_known_entities': an EXTRA entry is the bogus edge above, while a
    MISSING one is merely a link the forward-only oracle would not have made
    at that position either. Narrower is safer for the minuend — and by the
    same asymmetry, WIDER is safer for the subtrahend: a wider subtrahend can
    only ever remove idents from the stub set, never add one, so unioning in
    a second query is the safe direction here even though it looks like it
    is loosening the bound.
    """
    unresolved: Dict[str, str] = {}
    valid_at_clause = f':valid-at "{_edn_escape(valid_at)}" ' if valid_at else ""
    path_bearing: set = set()
    try:
        raw = _db_execute(
            db,
            "(query [:find ?ident "
            ":where "
            "[?e :entity-type :type/external-dependency] "
            "[?e :ident ?ident] "
            "[?e :path ?path]])",
        )
        path_bearing = {row[0] for row in json.loads(raw).get("results", [])}
    except Exception:
        return unresolved
    if valid_at_clause:
        # Second subtrahend disjunct (final-review Finding 1): a submodule
        # live at ts(W) but already closed at current time (see docstring
        # above). Best-effort like the unbounded query above — a failure
        # here just falls back to the unbounded-only result rather than
        # aborting the whole function.
        try:
            raw = _db_execute(
                db,
                "(query [:find ?ident "
                f"{valid_at_clause}"
                ":where "
                "[?e :entity-type :type/external-dependency] "
                "[?e :ident ?ident] "
                "[?e :path ?path]])",
            )
            path_bearing |= {row[0] for row in json.loads(raw).get("results", [])}
        except Exception:
            pass
    try:
        raw = _db_execute(
            db,
            "(query [:find ?ident ?desc "
            f"{valid_at_clause}"
            ":where "
            "[?e :entity-type :type/external-dependency] "
            "[?e :ident ?ident] "
            "[?e :description ?desc]])",
        )
        for ident, desc in json.loads(raw).get("results", []):
            if ident not in path_bearing:
                unresolved[ident] = desc
    except Exception:
        pass
    return unresolved


def _preload_field_class_idents(db: Any) -> Dict[str, str]:
    """Reload field_ident -> owning class_ident for every field with a live :class edge.

    A field only carries a :class edge when its owning class was genuinely
    extracted as a :type/class entity (see _precompute_file_triples). Without
    this reload, field_class_ident starts empty on every restart, so a field
    introduced in an earlier run would have its [class :contains field] edge
    silently leaked open when a later run closes the field (its module-contains
    edge would still close, but not the class one). Current-time query semantics
    naturally exclude already-closed fields' edges.

    Deliberately NOT bounded to the resume position, unlike the preloads
    around it (#222 phase 2d review, B1). This is a pure side-table read
    through .get() at close sites, never a "what existed then" set: an extra
    entry for a field the forward walk has not reached is inert (nothing
    looks it up until that field is genuinely closed, at which point the
    value is right), while a MISSING entry is the leaked-edge bug above. So
    the failure modes are asymmetric here in the opposite direction, and the
    unrestricted read is the safer one. _preload_field_static_idents is the
    same shape and is left alone for the same reason.
    """
    field_class_ident: Dict[str, str] = {}
    try:
        # Bind the field's :ident object (the canonical ":field/…" string), not
        # the subject variable — minigraf returns an internal UUID for a subject
        # in find position, whereas close sites key field_class_ident by the same
        # ident string _code_ident produces. This mirrors _preload_known_entities.
        raw = _db_execute(
            db,
            "(query [:find ?fi ?c :where "
            "[?f :entity-type :type/field] [?f :ident ?fi] [?f :class ?c]])",
        )
        for field_ident, class_ident in json.loads(raw).get("results", []):
            field_class_ident[field_ident] = class_ident
    except Exception:
        pass
    return field_class_ident


def _preload_field_static_idents(db: Any) -> Dict[str, bool]:
    """Reload field_ident -> :static value for every currently live field.

    Mirrors _preload_field_class_idents (see #134): without this reload,
    field_static_ident starts empty on every restart, so a field introduced
    in an earlier run would have its :static fact silently leaked open when
    a later run closes the field — _build_close_triples' is_static param
    would get None (skip) instead of the real value, reproducing the same
    gap this preload's sibling closes for :class.
    """
    field_static_ident: Dict[str, bool] = {}
    try:
        raw = _db_execute(
            db,
            "(query [:find ?fi ?s :where "
            "[?f :entity-type :type/field] [?f :ident ?fi] [?f :static ?s]])",
        )
        for field_ident, static_value in json.loads(raw).get("results", []):
            field_static_ident[field_ident] = bool(static_value)
    except Exception:
        pass
    return field_static_ident


_VALID_TIME_FOREVER_MS = (1 << 63) - 1  # minigraf's i64::MAX "still open" :valid-to sentinel


def _valid_time_window_clauses(valid_at_ms: Optional[int]) -> str:
    """Predicate clauses selecting the facts live at valid_at_ms, for the
    :any-valid-time preloads that bind ?vf/?vt (#222 phase 2d review, B1).

    valid_at_ms=None keeps the pre-existing "still open right now" test
    (:valid-to still at the forever sentinel) unchanged. Otherwise the test
    becomes half-open containment, [?vf, ?vt) ∋ valid_at_ms -- the same
    boundary rule :valid-at itself uses (a fact starting exactly at the
    instant is live, one ending exactly at it is not), verified against the
    backend so the two preload styles cannot disagree at the watermark.
    The forever sentinel satisfies [(> ?vt valid_at_ms)] for any real
    timestamp, so still-open facts stay in.
    """
    if valid_at_ms is None:
        return f"[(= ?vt {_VALID_TIME_FOREVER_MS})]"
    return f"[(<= ?vf {valid_at_ms})] [(> ?vt {valid_at_ms})]"


def _epoch_ms_to_iso(ms: int) -> str:
    """minigraf's epoch-ms valid-time scale back to millisecond-precision ISO.

    Millisecond precision is load-bearing for the re-admission pass in
    _preload_known_entities, which queries at ISO(vt - 1 ms). Verified against
    minigraf's temporal.rs parse_timestamp: any string containing 'T' goes
    through chrono's DateTime<Utc> parser, which accepts fractional seconds.
    Pinned by test_valid_at_accepts_millisecond_precision.

    Never pass _VALID_TIME_FOREVER_MS -- callers must test for the sentinel
    first, as minigraf's own millis_to_timestamp_string documents.
    """
    return (
        datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def _build_ts_positions(
    commit_metadata: List[Tuple[str, str, str, str]]
) -> Dict[str, List[int]]:
    """Map each author-date instant to EVERY linearization position holding it.

    A list, not a single position, and deliberately so: _git_commits formats
    "%Y-%m-%dT%H:%M:%SZ" at second granularity, so distinct commits routinely
    share an instant. Collapsing that to one position would silently pick a
    winner; _position_of_valid_time resolves the ambiguity explicitly instead.
    """
    ts_positions: Dict[str, List[int]] = {}
    for pos, (_hash, ts_iso, _author, _subject) in enumerate(commit_metadata):
        ts_positions.setdefault(ts_iso, []).append(pos)
    return ts_positions


def _position_of_valid_time(
    ms: int,
    ts_positions: Dict[str, List[int]],
    *,
    end: str,
    stats: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    """Recover the linearization position a :db/valid-from or :db/valid-to
    was written at (#238 close side, #245).

    Every :depends-on, :pinned-commit and :ident valid-time is written from
    commit_ts_iso, i.e. commit_metadata[pos][1], so it is always some commit's
    author date and its position is recoverable WITHOUT a commit reference to
    join to. Both issues state these sites admit no position filter; that is
    true of joins and false of positions. The inversion needs full-history
    commit_metadata at preload time, which is exactly what PR #246 made
    available by moving build_linearization and _git_commits above the preload
    block.

    AMBIGUITY RESOLVES TOWARD WRONG-EXCLUSION AT BOTH ENDS. An ambiguous
    introduction takes the LATEST colliding position (more likely to read as
    above W); an ambiguous close takes the EARLIEST (more likely to read as at
    or below W). Both land on exclusion, the direction #235's correction sweep
    repairs, rather than on the unrecoverable inverted-interval direction.

    THIS IS THE INVERSE OF evals/at_scale/probe_dep_preload_exposure.py's
    edge_live_at, which takes min for an introduction and max for a close.
    That is correct THERE and wrong HERE: a measurement must not understate
    exposure, because a number rounded in our own favour would have argued for
    closing #245; a fix must not risk the unrecoverable direction. DO NOT
    refactor the two into a shared helper. Pinned by
    test_collision_resolves_toward_exclusion_at_both_ends.

    Returns None for an instant matching no commit -- a rewritten history, or
    a fact dated by something other than a commit. Callers exclude on None.
    """
    if end not in ("intro", "close"):
        raise ValueError(f"end must be 'intro' or 'close', got {end!r}")
    ts_iso = (
        datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    positions = ts_positions.get(ts_iso)
    if not positions:
        if stats is not None:
            key = f"unmappable_{end}"
            stats[key] = stats.get(key, 0) + 1
        return None
    if len(positions) > 1 and stats is not None:
        stats["collisions"] = stats.get("collisions", 0) + 1
    return max(positions) if end == "intro" else min(positions)


def _fact_is_live_at_position(
    vf_ms: int,
    vt_ms: int,
    watermark_pos: int,
    ts_positions: Dict[str, List[int]],
    stats: Optional[Dict[str, int]] = None,
) -> bool:
    """The membership rule for the forward walk's preload state (#238, #245):

        live at W  <=>  intro_pos <= W  AND  (open OR close_pos > W)

    Position alone. Date clauses in the callers' queries are prefilters for
    row-count reduction and carry NO safety property -- see the spec's
    "Why this is not the add-back union #238 forbids".

    An unplaceable endpoint excludes, because the fact cannot be proven live
    at W. That is the recoverable direction.
    """
    intro_pos = _position_of_valid_time(
        vf_ms, ts_positions, end="intro", stats=stats
    )
    if intro_pos is None or intro_pos > watermark_pos:
        return False
    if vt_ms >= _VALID_TIME_FOREVER_MS:
        return True
    close_pos = _position_of_valid_time(
        vt_ms, ts_positions, end="close", stats=stats
    )
    if close_pos is None:
        return False
    return close_pos > watermark_pos


def _announce_unplaceable_facts(stats: Dict[str, int]) -> None:
    """Report facts whose valid-time matched no commit in the linearization.

    Not a hard failure: aborting an ingestion because one fact is unplaceable
    is worse than excluding it, and the ordinary rewritten-history case is
    already handled by watermark_pos falling to None, which disables position
    filtering wholesale. But a silent exclusion here would look exactly like
    the bug #245 fixed, so it is announced -- the same reasoning
    _commit_date_query uses when a non-empty watermark has no :date.

    Collisions are NOT announced: they are expected at second granularity and
    are resolved deterministically toward exclusion.
    """
    intro = stats.get("unmappable_intro", 0)
    close = stats.get("unmappable_close", 0)
    if not intro and not close:
        return
    print(
        f"[ingest] preload: {intro} unplaceable :db/valid-from and {close} "
        "unplaceable :db/valid-to facts -- their instants match no commit in "
        "this linearization, so they were excluded from the resume state "
        "(#245). Expect duplicate introductions rather than data loss.",
        file=sys.stderr,
    )


def _preload_known_deps(
    db: Any,
    file_entities: Dict[str, List[str]],
    valid_at_ms: Optional[int] = None,
    ts_positions: Optional[Dict[str, List[int]]] = None,
    watermark_pos: Optional[int] = None,
    t_hi_ms: Optional[int] = None,
    stats: Optional[Dict[str, int]] = None,
) -> tuple:
    """Reload file_deps/dep_valid_from from durable :depends-on facts.

    valid_at_ms is _preload_known_entities' valid_at on minigraf's epoch-ms
    valid-time scale, and means the same thing: the graph as it stood at the
    forward walk's resume position (#222 phase 2d review, B1). :depends-on is
    never written by the reverse stream, but Stage B's lifecycle pass calls
    _forward_apply(lifecycle_only=True), whose dep_add_triples are NOT gated
    on that flag -- so a completed two-stream run does leave :depends-on edges
    above the watermark, and a current-time reload would hand them to a
    resuming forward walk that then closes them at an earlier commit. This
    query cannot express the bound as :valid-at, because the
    :db/valid-from/:db/valid-to pseudo-attributes it needs only bind under
    :any-valid-time; the equivalent is stated directly as
    [valid_from, valid_to) containing valid_at_ms, which is exactly
    :valid-at's own half-open semantics.

    #238/#245: membership is now decided by POSITION, not by date. A
    :depends-on fact carries no commit reference, but its :db/valid-from and
    :db/valid-to are always some commit's author date (every write site dates
    them from commit_ts_iso), so _fact_is_live_at_position recovers both
    endpoints' positions by inverting the timestamp. #245's own text says
    these sites "admit no position filter"; that is true of JOINS and false of
    POSITIONS, and the inversion only became available once PR #246 moved the
    full-history commit_metadata above the preload block.

    The `[(<= ?vf t_hi_ms)]` clause is a PREFILTER for row-count reduction and
    carries no safety property: a fact introduced at position p <= W has
    vf = ts[p] <= T_hi(W), so it drops only rows the position rule would drop
    anyway. There is deliberately NO clause on ?vt -- a close above W can
    carry an arbitrarily early date, which is the whole defect.
    Widening the prefilter without the position filter is the "add-back union"
    #238 forbids; test_the_prefilter_alone_does_not_close_the_hole pins that.

    position_mode off (no watermark, or a watermark absent from this
    linearization) restores today's ts(W) date window exactly, which is
    narrower than open-facts-only and therefore the safer degradation.

    Mirrors _preload_known_entities, but :depends-on facts have no
    :introduced-by-style companion edge to a commit's :date, so the
    introduction timestamp has to come from the fact's own :db/valid-from
    via minigraf's per-fact temporal metadata pseudo-attributes (minigraf
    >=1.0.0, verified present at the pinned/installed 1.2.1). :any-valid-time
    is required for any per-fact pseudo-attribute to bind at all; the
    explicit :db/valid-to equality against the "forever" sentinel is what
    restricts results to edges that haven't been closed (:any-valid-time
    alone would also return already-closed historical facts).

    Without this, file_deps/dep_valid_from start empty on every restart,
    which not only breaks removed-dependency detection but actively
    corrupts history: current_deps - previous_deps would treat every
    already-standing dependency as newly introduced the next time its file
    is touched, overwriting its true :valid-from.

    Returns (file_deps, dep_valid_from):
    file_deps maps file_path -> set of dep module idents.
    dep_valid_from maps (src_module_ident, dep_ident) -> ISO 8601 intro timestamp.
    """
    file_deps: Dict[str, set] = {}
    dep_valid_from: Dict[tuple, str] = {}

    ident_to_file = {
        _code_ident("module", file_path): file_path for file_path in file_entities
    }

    position_mode = (
        watermark_pos is not None
        and ts_positions is not None
        and t_hi_ms is not None
    )
    window_clauses = (
        f"[(<= ?vf {t_hi_ms})]" if position_mode
        else _valid_time_window_clauses(valid_at_ms)
    )

    try:
        # Bind the source module's :ident object (the canonical ":module/…"
        # string _code_ident produces), not the bare ?src subject variable —
        # minigraf returns an internal UUID for a subject in find position,
        # which would never match ident_to_file's ident-string keys. This
        # mirrors _preload_known_entities/_preload_field_class_idents.
        #
        # The [?src :ident ?srci] clause must precede [?src :depends-on ?dep]
        # in clause order: minigraf's :db/valid-from/:db/valid-to pseudo-
        # attributes bind to whichever EAV clause on ?src most recently
        # precedes them, so putting :ident after :depends-on would make ?vf
        # bind to the :depends-on fact's valid-from (unaffected here) but
        # putting it *between* :depends-on and :db/valid-from would instead
        # make ?vf bind to the :ident fact's own valid-from — wrong. Keeping
        # :ident first and :depends-on immediately before the two pseudo-
        # attribute clauses preserves the correct binding.
        raw = _db_execute(
            db,
            "(query [:find ?srci ?dep ?vf ?vt "
            ":any-valid-time "
            ":where [?src :ident ?srci] "
            "[?src :depends-on ?dep] "
            "[?src :db/valid-from ?vf] "
            "[?src :db/valid-to ?vt] "
            f"{window_clauses}])"
        )
        rows = json.loads(raw).get("results", [])
    except Exception:
        return file_deps, dep_valid_from

    for src_ident, dep_ident, vf_ms, vt_ms in rows:
        file_path = ident_to_file.get(src_ident)
        if file_path is None:
            continue
        if position_mode and not _fact_is_live_at_position(
            vf_ms, vt_ms, watermark_pos, ts_positions, stats
        ):
            continue
        vf_iso = _epoch_ms_to_iso(vf_ms)
        file_deps.setdefault(file_path, set()).add(dep_ident)
        dep_valid_from[(src_ident, dep_ident)] = vf_iso

    return file_deps, dep_valid_from


def _preload_pinned_commits(
    db: Any,
    valid_at_ms: Optional[int] = None,
    ts_positions: Optional[Dict[str, List[int]]] = None,
    watermark_pos: Optional[int] = None,
    t_hi_ms: Optional[int] = None,
    stats: Optional[Dict[str, int]] = None,
) -> Dict[str, tuple]:
    """Reload each external-dependency entity's current :pinned-commit value
    and the timestamp it was set at, mirroring _preload_known_deps's per-fact
    :any-valid-time pattern for :depends-on.

    valid_at_ms carries the same resume-position bound, for the same reason:
    Stage B's lifecycle pass runs the gitlink handling over the reverse
    region, so a completed two-stream run can leave a bump recorded above the
    watermark (#222 phase 2d review, B1).

    #238/#245: membership is decided by POSITION, exactly as
    _preload_known_deps does and for the same reason -- a :pinned-commit fact
    carries no commit reference, but its :db/valid-from / :db/valid-to are
    always some commit's author date. See that function's docstring for the
    prefilter's role and the degradation path.

    UNMEASURABLE on the repository this was developed against: 0 gitlink
    events in 610 commits, so that history produces no :pinned-commit facts at
    all and the #245 exposure probe reports nothing here. This ships on the
    argument that the mechanism is identical to :depends-on's, NOT on measured
    field exposure. Unlike :depends-on this function has no ident_to_file
    narrowing, so it lacks even that partial mitigation.

    Needed because :pinned-commit is bi-temporally closed and reopened on
    every bump (see _run_ingestion's gitlink handling) — without this, the
    server would lose track of the prior SHA and valid-from across a restart,
    corrupting the close on the next bump or removal exactly the way
    _preload_known_deps' docstring describes for :depends-on.

    Returns {ident: (sha, valid_from_iso)}.
    """
    pinned: Dict[str, tuple] = {}
    position_mode = (
        watermark_pos is not None
        and ts_positions is not None
        and t_hi_ms is not None
    )
    window_clauses = (
        f"[(<= ?vf {t_hi_ms})]" if position_mode
        else _valid_time_window_clauses(valid_at_ms)
    )
    try:
        # Bind the entity's :ident object, not the bare ?e subject variable —
        # same UUID-vs-ident pitfall _preload_known_deps guards against.
        # [?e :ident ?ei] must precede [?e :pinned-commit ?sha] so that the
        # :db/valid-from/:db/valid-to pseudo-attributes (which bind to
        # whichever EAV clause on ?e most recently precedes them) continue
        # to bind to the :pinned-commit fact, not the :ident fact.
        raw = _db_execute(
            db,
            "(query [:find ?ei ?sha ?vf ?vt "
            ":any-valid-time "
            ":where [?e :ident ?ei] "
            "[?e :pinned-commit ?sha] "
            "[?e :db/valid-from ?vf] "
            "[?e :db/valid-to ?vt] "
            f"{window_clauses}])"
        )
        rows = json.loads(raw).get("results", [])
    except Exception:
        return pinned
    for ident, sha, vf_ms, vt_ms in rows:
        if position_mode and not _fact_is_live_at_position(
            vf_ms, vt_ms, watermark_pos, ts_positions, stats
        ):
            continue
        pinned[ident] = (sha, _epoch_ms_to_iso(vf_ms))
    return pinned


def _preload_provisional_idents(db: Any) -> Set[str]:
    """Every tracked ident that currently has a :type/lineage-marker
    companion entity, i.e. whose :introduced-by is a provisional guess.

    No :status clause is needed: the marker exists ONLY while the entity is
    provisional -- _lineage_confirm retracts the whole companion entity
    rather than flipping its :status -- so existence is the test, exactly as
    _lineage_is_provisional does per-ident. Keeping the two queries the same
    shape is deliberate: this set is consulted where a per-ident check is
    too expensive, and the two must never disagree.

    Deliberately NOT bounded to the resume position the way the mutable walk
    state is (#222 phase 2d review, B1). This set is the reconciliation
    AUTHORITY: its whole job is to name the entities the reverse stream
    introduced above the watermark, so restricting it to the watermark's
    valid-time would empty it of exactly the rows it exists to carry.
    """
    raw = _db_execute(
        db,
        f"(query [:find ?e :where [?m :entity-type {_LINEAGE_MARKER_ENTITY_TYPE}] [?m :entity ?e]])",
    )
    return {row[0] for row in json.loads(raw).get("results", [])}


def _resume_envelope(
    commit_metadata: List[Tuple[str, str, str, str]], watermark_pos: Optional[int]
) -> Optional[str]:
    """T_hi(W) = max(ts[0..W]) -- the monotone envelope of every author date at
    or below the resume position, and the valid-time bound
    _preload_known_entities takes (#238).

    NOT ts(W). Author dates are not monotonic in topological order
    (_git_commits reads %at, not %ct), so ts(W) does not cleanly separate "at
    or below the resume position" from "above it". The envelope is the widest
    bound that still excludes every close at or below W: such a close has
    valid_to = ts[p] <= T_hi(W), and :valid-at's half-open semantics require
    valid_at < valid_to.

    Widening the bound this way is only safe BECAUSE _preload_known_entities
    also applies a conjunctive position clause. Alone it is the "add-back
    union" #238 warns produces a change that looks like a fix and isn't -- see
    test_the_envelope_alone_does_not_close_the_hole.

    Timestamps are fixed-width UTC ("%Y-%m-%dT%H:%M:%SZ") from _git_commits, so
    lexicographic max is chronological max.

    Returns None for a None position (a fresh graph, no watermark), which
    degrades the preload to its pre-#222 unrestricted form.
    """
    if watermark_pos is None:
        return None
    window = commit_metadata[: watermark_pos + 1]
    if not window:
        return None
    return max(ts for _hash, ts, _author, _subject in window)


def _load_ingestion_preload_state(
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
) -> tuple:
    """Open the DB and run every startup preload query for _run_ingestion.

    Executed via run_in_executor on a worker thread (see _run_ingestion), not
    inline on the event loop: opening/mmapping a graph file plus these preload
    queries contain no internal awaits, so running them directly on the event
    loop thread starves the stdio handshake for as long as they take — on a
    large enough graph, longer than a client's connection timeout (issue #103).

    Takes an extended lease (db_lease(extended=True)): this runs off the event
    loop, so blocking backoff is correct here, and it can afford to wait out a
    typical orphan-process cleanup window rather than giving up in ~1.55s and
    entering a permanent "error" state (#106). The lease -- not a manual
    `_db = None` -- is what releases the graph file lock when this returns.

    linearization and commit_metadata are #238's resume bound. They are
    supplied by the caller rather than built here because the git enumeration
    must not run while this function holds the graph file lock -- see
    _run_ingestion, which now runs both enumerations before the preload rather
    than after it. That ordering, not any preference for valid-time, is the
    whole reason the bound used to be expressed in author dates: the
    linearization simply did not exist yet when this ran.

    Both are validated for positional alignment before use: length AND
    per-position hash equality, matching _reverse_apply's own check
    (mcp_server.py, `commit_hash != linearization[pos]`) for the same reason
    (silent, systematic error) -- but the consequence here is worse: a
    misaligned pair mis-filters the ENTIRE preload rather than misattributing
    one commit. Length alone is not enough: build_linearization and
    _git_commits(repo_path, None, branch) are two separate `git log`
    invocations, so a ref moving between them can keep lengths equal while
    permuting hashes across positions.
    """
    with db_lease(extended=True) as db:
        # FIRST thing after the handle exists (#336), ahead even of the format
        # check below, because that check is itself an entity-bound read: on a
        # graph with a damaged EAVT index (project-minigraf/minigraf#370) the
        # stamp, watermark and frontiers all read as absent and a mature graph
        # is adopted as fresh -- or, with partial damage, refused for an ident-
        # rule problem it does not have. Read-only. Raises GraphIndexDamageError,
        # which _run_ingestion surfaces as a failed run.
        _ingest_progress["index_cross_check"] = _graph_index_cross_check(db)
        # Second, and still ahead of everything else: this is the earliest point
        # after the index check, and everything below it (and every write in
        # _frontier_load and the walks after it) would be written under the
        # current ident rule. A refusal that fired later would leave a graph
        # half-written under two rules. Read-only; the matching stamp write is
        # _run_ingestion's first write. Raises GraphFormatVersionError, which
        # _run_ingestion surfaces as a failed run.
        _graph_format_version_verify(db)
        watermark = _watermark_query(db)
        if len(commit_metadata) != len(linearization):
            raise ValueError(
                "commit_metadata must be positionally aligned with linearization "
                f"(got {len(commit_metadata)} entries vs {len(linearization)}); "
                "a misaligned pair mis-filters the entire preload (#238)"
            )
        # Length equality alone does not rule out a PERMUTATION: build_linearization
        # and _git_commits(repo_path, None, branch) are two separate `git log`
        # invocations, so a ref moving between them can keep both lists the same
        # length while reordering hashes across positions -- silently shifting
        # T_hi(W) (_resume_envelope reads commit_metadata by index) and
        # mis-filtering the whole preload without ever raising. Mirrors
        # _reverse_apply's own per-position `commit_hash != linearization[pos]`
        # check, generalized to every position since this function has no single
        # `pos` to check -- it consumes the full lists up front.
        for i, (meta_hash, linearization_hash) in enumerate(zip(
            (h for h, _t, _a, _s in commit_metadata), linearization,
        )):
            if meta_hash != linearization_hash:
                raise ValueError(
                    f"commit_metadata[{i}] is {meta_hash}, but linearization[{i}] is "
                    f"{linearization_hash}: the two must be positionally aligned "
                    "(#238) -- a ref move between the two git-log invocations can "
                    "keep lengths equal while permuting hashes"
                )
        hash_to_pos = {h: i for i, h in enumerate(linearization)}
        watermark_pos = hash_to_pos.get(watermark) if watermark is not None else None

        # #238/#245: two DIFFERENT bounds now, deliberately.
        #
        # resume_valid_at is ts(W), the watermark commit's own :date. As of #245,
        # :depends-on and :pinned-commit are ALSO position-filtered -- both via
        # the inversion of their own :db/valid-from/:db/valid-to, the same
        # mechanism entities use, not by adopting the envelope as their date
        # bound. resume_valid_at survives only for two callers:
        # _preload_unresolved_dep_idents (position-unaware by design, see its own
        # docstring), and as the degraded-path bound _preload_known_deps /
        # _preload_pinned_commits fall back to when watermark_pos is None (see
        # the t_hi_ms guard ~12 lines below). It is no longer true that deps/pins
        # "admit no position clause" -- see how ts_positions/watermark_pos/
        # t_hi_ms are threaded into both calls below.
        #
        # entity_valid_at is the monotone envelope T_hi(W) = max(ts[0..W]), which
        # is safe only because _preload_known_entities pairs it with the
        # conjunctive position clause below. A None watermark_pos (fresh graph, or
        # a watermark absent from this linearization -- a rewritten history)
        # degrades both to the pre-#222 unrestricted queries rather than to an
        # empty state.
        resume_valid_at = _commit_date_query(db, watermark)
        resume_valid_at_ms = _iso_to_epoch_ms(resume_valid_at)
        ts_positions = _build_ts_positions(commit_metadata)

        # #238/#245: membership at all four sites is decided by POSITION.
        #
        # t_hi_ms is derived from _resume_envelope BEFORE entity_valid_at's
        # fallback below, and stays None whenever watermark_pos is None. That
        # guard is load-bearing: a watermark that exists but is absent from this
        # linearization leaves resume_valid_at a real ts(W) while disabling the
        # position filter, and letting that become t_hi_ms would hand the deps and
        # pins queries a WIDENED prefilter with no position clause -- exactly the
        # widening #245 forbids. With t_hi_ms None they keep the ts(W) date
        # window, which is strictly no worse than today.
        #
        # resume_valid_at (the ISO string) survives for _preload_unresolved_dep_idents
        # only. resume_valid_at_ms (derived above) is different: it remains the
        # degraded-path bound for _preload_known_deps and _preload_pinned_commits,
        # used whenever position_mode is off in either.
        entity_valid_at = _resume_envelope(commit_metadata, watermark_pos)
        t_hi_ms = _iso_to_epoch_ms(entity_valid_at)
        assert watermark_pos is not None or t_hi_ms is None, (
            "t_hi_ms must stay None whenever watermark_pos is None -- callees "
            "only read t_hi_ms inside their own position_mode branch, which "
            "already requires watermark_pos is not None (#245); a non-None "
            "t_hi_ms here would hand a widened prefilter to a callee with its "
            "position filter off, exactly the 'add-back union' #238 forbids"
        )
        if entity_valid_at is None:
            entity_valid_at = resume_valid_at
        position_stats: Dict[str, int] = {}
        prior_ingested = _count_commit_entities(db)
        (
            entity_valid_from, entity_descriptions, entity_introduced_by,
            file_entities, submodule_paths,
        ) = _preload_known_entities(
            db, repo_path, valid_at=entity_valid_at,
            hash_to_pos=hash_to_pos, watermark_pos=watermark_pos,
            ts_positions=ts_positions, t_hi_ms=t_hi_ms, stats=position_stats,
        )
        file_deps, dep_valid_from = _preload_known_deps(
            db, file_entities, valid_at_ms=resume_valid_at_ms,
            ts_positions=ts_positions, watermark_pos=watermark_pos,
            t_hi_ms=t_hi_ms, stats=position_stats,
        )
        pinned_commit_state = _preload_pinned_commits(
            db, valid_at_ms=resume_valid_at_ms,
            ts_positions=ts_positions, watermark_pos=watermark_pos,
            t_hi_ms=t_hi_ms, stats=position_stats,
        )
        _announce_unplaceable_facts(position_stats)
        field_class_ident = _preload_field_class_idents(db)
        field_static_ident = _preload_field_static_idents(db)
        # Independent of _preload_known_entities' bound now (#238): the subtrahend
        # is that function's own unbounded :path query, so stub classification no
        # longer moves with the resume position. Keeps ts(W), not the envelope --
        # see its docstring for why narrower is safer for this set.
        unresolved_dep_idents = _preload_unresolved_dep_idents(
            db, valid_at=resume_valid_at,
        )
        # No provisional-ident preload (#235): the forward walk's reconciliation
        # gate is _lineage_is_provisional(db, ident), queried per ident at the
        # moment it matters. A run-start snapshot cannot serve that purpose -- it
        # is empty on a fresh ingest, where Stream 2 writes its guesses during
        # this same run -- so preloading one only invites a future maintainer to
        # re-derive a gate from it. _preload_provisional_idents still exists for
        # whole-graph assertions ("no markers left after a full ingest"); it is
        # deliberately not part of the walk's state.
        return (
            watermark, prior_ingested, entity_valid_from, entity_descriptions,
            entity_introduced_by, file_entities, file_deps, dep_valid_from,
            pinned_commit_state, field_class_ident, field_static_ident,
            submodule_paths, unresolved_dep_idents,
        )


# Tag attributes whose value is a keyword reference, not an EDN string literal
# -- everything else in _ingest_tags' triples is string-valued.
_TAG_KEYWORD_ATTRS = frozenset({":entity-type", ":tagged-commit"})


def _ingest_tags(db: Any, repo_path: str, run_ts_iso: str, index_con: Optional[Any] = None) -> None:
    """Ingest git tags as :tag/<slug> entities with :tagged-commit references.

    Called once after the commit walk. All tags are re-checked on every run so
    newly created tags pointing to previously ingested commits are picked up
    -- but each attribute is only retracted+re-transacted when its VALUE
    actually changed (unlike _watermark_update's unconditional retract-then-
    reassert, this skips the write entirely when nothing changed).
    Minigraf is NOT idempotent at the graph level for re-transacting the same
    (entity, attribute, value) under a different valid-from: it creates a
    second, genuinely live duplicate fact rather than a no-op (#156).
    Blindly re-transacting every tag's full triple set on every run therefore
    accumulates unbounded duplicate facts for every unchanged tag; diffing
    against the tag's current live facts first avoids that. This does not
    retroactively collapse duplicates a pre-fix run already created (a stale
    duplicate value matches the desired value trivially, so it's left alone)
    -- only new duplication going forward is prevented, by design/scope.
    """
    try:
        tags = _git_tags(repo_path)
    except Exception:
        return  # non-fatal

    for tag_name, commit_hash, date_raw in tags:
        try:
            slug = re.sub(r"[^a-z0-9]+", "-", tag_name.lower()).strip("-")
            tag_ident = f":tag/{slug}"
            commit_ident = f":commit/{commit_hash[:12]}"

            desired: Dict[str, str] = {
                ":entity-type": ":type/tag",
                ":name": tag_name,
                ":ident": tag_ident,
                ":description": f"git tag {tag_name}",
                ":tagged-commit": commit_ident,
            }
            if date_raw:
                desired[":date"] = date_raw

            current_raw = _db_execute(db, f"(query [:find ?a ?v :where [{tag_ident} ?a ?v]])")
            current: Dict[str, str] = dict(json.loads(current_raw).get("results", []))

            def _edn(attr: str, value: str) -> str:
                return value if attr in _TAG_KEYWORD_ATTRS else f'"{_edn_escape(value)}"'

            to_retract: List[str] = []
            to_transact: List[str] = []
            for attr, value in desired.items():
                if current.get(attr) == value:
                    continue  # already correct -- skip to avoid creating a duplicate live fact (#156)
                if attr in current:
                    to_retract.append(f"[{tag_ident} {attr} {_edn(attr, current[attr])}]")
                to_transact.append(f"[{tag_ident} {attr} {_edn(attr, value)}]")

            if to_retract:
                _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
            if to_transact:
                _transact(db, "[" + " ".join(to_transact) + "]", run_ts_iso, index_con=index_con)
        except Exception:
            pass  # non-fatal per tag


def _extract_commit(
    repo_path: str, commit_hash: str, ignore_patterns: Sequence[str] = ()
) -> Tuple[List[tuple], List[tuple], Dict[str, Dict[str, str]], List[Tuple[str, str, str, str, str]]]:
    """Read-only, stateless per-commit extraction: diff-tree + git-show + tree-sitter parse,
    plus import resolution and "if this turns out to be new" triple precomputation —
    both pure functions of this commit alone (see _known_files_at_commit and
    _precompute_file_triples), unlike the incrementally-mutated file_entities/
    entity_valid_from state only the serial main thread maintains.

    ignore_patterns (see _is_ignored_path/_load_ignore_patterns) are checked first,
    before _thread_parser even runs — an ignored file costs zero parse time and is
    also excluded from known_files, so anything importing it falls through to the
    external-dependency fallback instead of resolving internally (#115).

    Runs in a worker process via the ProcessPoolExecutor in _run_ingestion (#116 —
    a thread pool here let tree-sitter's GIL-holding C parse starve the event
    loop). Touches no shared mutable state and no DB — a hard requirement now
    that this crosses a process boundary, not just a nice property. Returns
    (file_results, gitlink_changes, gitmodules_map, renamed_pairs):

      file_results: one entry per changed file that has a supported parser, as
        (status, file_path, extracted, precomputed, old_path). A/M files whose
        content fetch fails are omitted entirely, mirroring the previous inline
        `continue` — same as before this pipeline existed. For a "D" (deleted)
        file, extracted and precomputed are both None — the main thread only
        needs file_path to know what to close. old_path is the pre-rename path
        for "R" entries and "" for every other status (A/M/D) — kept as a fixed
        5th tuple element rather than variable arity so downstream consumers
        (_run_ingestion) can unpack uniformly.
      gitlink_changes: _gitlink_changes' output, filtered through ignore_patterns
        (via _is_ignored_path) exactly like a regular file's path would be — an
        ignored gitlink is dropped from the list entirely, before gitmodules_map
        below is even considered. Never fed through the tree-sitter parser
        (gitlink paths never have a resolvable extension).
      gitmodules_map: path -> {"name", "url"}, populated only when this commit has at
        least one gitlink "add" — avoids a wasted git-show call on the (overwhelmingly
        common) case of a commit that touches no submodules at all.
      renamed_pairs: (category, old_file_path, old_name, new_file_path, new_name)
        plain-string 5-tuples — one per function/class the AST-lockstep matcher
        (_match_renamed_entities, Task 8) confirmed renamed and/or moved within
        this commit. Deliberately plain strings, not the tree_sitter Node
        objects _match_renamed_entities itself works with — those live only
        for the duration of this call and cannot cross the ProcessPoolExecutor
        boundary back to the main process (see #116).

    Sources both file_results and gitlink_changes from a single
    `git diff-tree --raw` call (via _git_diff_tree_raw) rather than a --name-status
    call, which discarded file mode entirely.

    known_files (via _known_files_at_commit) is computed lazily, once per commit,
    and shared across every A/M file in this commit — a commit with only deletions
    never pays for it. Its _SegmentSuffixIndex (for _resolve_module_import's tiers
    3a/3b) is built alongside it, once, and reused the same way — otherwise every
    import in every A/M file would re-scan and re-derive segments for the whole
    known_files set from scratch.
    """
    raw_entries = _git_diff_tree_raw(repo_path, commit_hash)
    commit_ident = f":commit/{commit_hash[:12]}"
    results: List[tuple] = []
    known_files: Optional[Dict[str, List[str]]] = None
    segment_index: Optional[_SegmentSuffixIndex] = None

    # removed/added pools for _match_renamed_entities, scoped to this commit.
    # Populated alongside the per-file loop below; matched entirely inside
    # this worker process — tree_sitter Node objects never cross the process
    # boundary (#116), only the plain-string renamed_pairs derived from
    # matches does.
    removed_pool: Dict[str, List[Tuple[str, Any]]] = {
        "function": [], "class": [], "variable": [], "field": [],
    }
    added_pool: Dict[str, List[Tuple[str, Any]]] = {
        "function": [], "class": [], "variable": [], "field": [],
    }
    # (category, old_file_path, old_name, new_file_path, new_name) is only
    # knowable once we know which FILE each pooled node came from — track
    # that alongside the pool itself, keyed by node identity (id()), since
    # two different removed entities in two different deleted files could
    # coincidentally share a name.
    node_origin: Dict[int, str] = {}  # id(node) -> file_path
    # id(node) -> file-relationship group key, fed to _match_renamed_entities'
    # file_groups param (#174). A "D" or plain "A" node's own path is unique
    # to it (no other node shares that exact string), so it can never
    # coincidentally group with an unrelated file's nodes; an "M" node's old
    # and new sides share the same literal path already; an "R" pair's old
    # and new sides get one synthetic shared key (their paths genuinely
    # differ) so a git-confirmed rename/move still matches at the lower,
    # same-file confidence bar. Distinct from node_origin (which always
    # records the real path, for renamed_pairs' output) since D/A's own path
    # must stay a valid, reportable file_path while still acting as a
    # never-shared group key here.
    node_group: Dict[int, str] = {}
    # Bare body-text names (see _match_body_name) present, with the SAME name,
    # on BOTH the old and new side of some touched file this commit — tracked
    # entities that survived unrenamed. Passed to _match_renamed_entities so a
    # reference to one of them must match exactly rather than be treated as a
    # free local (see the P1 false-continuity fix). Unchanged same-path
    # entities never enter removed_pool/added_pool (the "M" diff excludes
    # them), so they must be threaded separately to constrain OTHER entities'
    # candidate walks.
    unchanged_names: Set[str] = set()

    def collect_all_nodes(root: Any, lang: str) -> Dict[str, Dict[str, Any]]:
        # Widens _collect_entity_nodes's function/class-only result with the
        # variable/field categories from Component 3 (Tasks 13-25), reusing
        # _extract_globals_and_fields directly (not through
        # _extract_from_source) so its live-node keys — never exposed across
        # the ProcessPoolExecutor boundary — are available here, entirely
        # inside this worker process (Task 26).
        base = _collect_entity_nodes(root, lang)
        gf = _extract_globals_and_fields(root, "typescript" if lang == "tsx" else lang)
        base["variable"] = dict(gf.get("global_nodes", {}))
        base["field"] = dict(gf.get("field_nodes", {}))
        return base

    for status, old_mode, new_mode, old_sha, new_sha, file_path, old_path, similarity in raw_entries:
        # Trackable == not ignored AND has a supported parser. Short-circuits
        # so an ignored path never pays for a parser build (see the docstring's
        # "ignored file costs zero parse time" contract).
        new_trackable = (
            not _is_ignored_path(file_path, ignore_patterns)
            and _thread_parser(file_path) is not None
        )
        if status == "R":
            # -M folds a rename's old+new sides into ONE "R" row, but each side
            # can have a different trackability (cross-extension rename, or a
            # move into/out of an ignored directory). Keying the skip on the
            # NEW path alone (as A/M/D do) silently drops the whole row when the
            # new side is untrackable — leaking the old module/children/deps
            # open forever — and, in reverse, closes a phantom old module that
            # was never opened. So resolve the OLD side independently, with its
            # own ignore/parser lookup keyed on old_path's extension.
            old_trackable = (
                not _is_ignored_path(old_path, ignore_patterns)
                and _thread_parser(old_path) is not None
            )
            if old_trackable and not new_trackable:
                # Forward (tracked -> unsupported/ignored): rewrite the row as a
                # synthetic delete of the OLD path so the existing "D" handling
                # below closes the old module, its children, and its deps.
                status, file_path, old_path = "D", old_path, ""
            elif new_trackable and not old_trackable:
                # Reverse (unsupported/ignored -> tracked): the new path is a
                # brand-new entity and the old ident was never opened. Treat as
                # a plain add — no rename linkage, no old-module close.
                status, old_path = "A", ""
            elif not new_trackable:  # neither side trackable — nothing to do
                continue
            # else: both sides trackable — unchanged "R" handling below.
        elif not new_trackable:
            continue

        parser = _thread_parser(file_path)
        if parser is None:
            continue

        old_lang_path = old_path if status == "R" else file_path
        old_entity_nodes: Dict[str, Dict[str, Any]] = {
            "function": {}, "class": {}, "variable": {}, "field": {},
        }
        if status in ("D", "M", "R") and old_sha and old_sha != "0" * len(old_sha):
            try:
                old_content = _git_blob_content(repo_path, old_sha)
                # For "R" (rename), old_lang_path is the PRE-rename path,
                # which can map to a different language than file_path (the
                # NEW path) on a cross-extension rename — reuse `parser`
                # (already selected for file_path) would silently walk the
                # old blob with the wrong grammar. _thread_parser(old_lang_path)
                # selects the grammar matching the blob's own language. For
                # "M"/"D", old_lang_path == file_path already (no rename), so
                # this is the same parser instance as `parser` (thread-local
                # cache hit) — no behavior change there.
                old_parser = _thread_parser(old_lang_path) if status == "R" else parser
                old_tree = old_parser.parse(old_content)
                old_lang = _EXT_TO_LANG.get(Path(old_lang_path).suffix.lower(), "")
                old_entity_nodes = collect_all_nodes(old_tree.root_node, old_lang)
            except Exception:
                pass  # best-effort: matching degrades to no-match, not a hard failure

        if status == "D":
            for category in ("function", "class", "variable", "field"):
                for name, node in old_entity_nodes[category].items():
                    removed_pool[category].append((name, node))
                    node_origin[id(node)] = old_lang_path
                    node_group[id(node)] = old_lang_path
            results.append((status, file_path, None, None, ""))
            continue

        try:
            content = _git_file_content(repo_path, commit_hash, file_path)
        except Exception:
            continue

        # Live nodes for the NEW side come from re-parsing (extracted only
        # carries text, per Task 6) — cheap, since this is the same content
        # already fetched above; a second parse of the same bytes is a
        # deliberate simplicity/cost tradeoff over threading Node references
        # through _extract_from_source's return value, which must stay
        # plain-data-only for other callers. Computed here, BEFORE
        # _precompute_file_triples, so #221's body-diff hash-compare can use
        # it alongside old_entity_nodes; still reused further below for its
        # original rename-matching purpose too.
        #
        # Wrapped best-effort, same as the old-side call above: a
        # pathologically nested file parses fine under tree-sitter but can
        # blow the Python recursion limit inside _collect_entity_nodes's
        # own recursive walk() (RecursionError). Left unguarded, that
        # exception would propagate out of _extract_commit and (in the real
        # ProcessPoolExecutor pipeline) abort the entire ingestion run
        # rather than just this one commit — contradicting this function's
        # own contract that ordinary exceptions fail only the one commit.
        new_lang = _EXT_TO_LANG.get(Path(file_path).suffix.lower(), "")
        try:
            new_tree = parser.parse(content)
            new_entity_nodes = collect_all_nodes(new_tree.root_node, new_lang)
        except Exception:
            new_entity_nodes = {
                "function": {}, "class": {}, "variable": {}, "field": {},
            }  # best-effort: matching degrades to no-match

        extracted = _extract_from_source(content, parser, file_path)
        if known_files is None:
            known_files = _known_files_at_commit(repo_path, commit_hash, ignore_patterns)
            segment_index = _SegmentSuffixIndex(known_files)
        precomputed = _precompute_file_triples(
            file_path, extracted, commit_ident, known_files, segment_index=segment_index,
            old_entity_nodes=old_entity_nodes, new_entity_nodes=new_entity_nodes,
        )
        results.append((status, file_path, extracted, precomputed, old_path if status == "R" else ""))

        # Record every entity whose name is present, unchanged, on BOTH sides
        # of this file — these survive the commit unrenamed and so must be
        # matched exactly (not treated as free locals) when they appear inside
        # some OTHER entity's candidate body. Uses the bare body-text name
        # (via _match_body_name) to match how the matcher keys tracked names.
        # For "A" the old side is empty and for "D" we already `continue`d, so
        # only "M"/"R" (both-sides) files contribute here.
        for category in ("function", "class", "variable", "field"):
            common = set(old_entity_nodes[category].keys()) & set(new_entity_nodes[category].keys())
            for name in common:
                unchanged_names.add(_match_body_name(category, name))

        if status == "A":
            for category in ("function", "class", "variable", "field"):
                for name, node in new_entity_nodes[category].items():
                    added_pool[category].append((name, node))
                    node_origin[id(node)] = file_path
                    node_group[id(node)] = file_path
        elif status == "R":
            # Ident changes for every entity in a renamed file, even ones
            # whose text is byte-identical — pool everything on both sides,
            # not just the local diff (unlike "M" below). Both sides share
            # ONE synthetic group key (#174) — old_lang_path != file_path
            # here, but git already confirmed this specific pair as a real
            # rename/move, so they're linked at the lower, same-file
            # confidence bar rather than treated as unrelated cross-file
            # candidates.
            rename_group = f"rename:{old_lang_path}->{file_path}"
            for category in ("function", "class", "variable", "field"):
                for name, node in old_entity_nodes[category].items():
                    removed_pool[category].append((name, node))
                    node_origin[id(node)] = old_lang_path
                    node_group[id(node)] = rename_group
                for name, node in new_entity_nodes[category].items():
                    added_pool[category].append((name, node))
                    node_origin[id(node)] = file_path
                    node_group[id(node)] = rename_group
        else:  # "M" — same path, only the local diff needs matching
            for category in ("function", "class", "variable", "field"):
                old_names = set(old_entity_nodes[category].keys())
                new_names = set(new_entity_nodes[category].keys())
                for name in old_names - new_names:
                    node = old_entity_nodes[category][name]
                    removed_pool[category].append((name, node))
                    node_origin[id(node)] = old_lang_path
                    node_group[id(node)] = old_lang_path
                for name in new_names - old_names:
                    node = new_entity_nodes[category][name]
                    added_pool[category].append((name, node))
                    node_origin[id(node)] = file_path
                    node_group[id(node)] = file_path

    raw_matches = _match_renamed_entities(removed_pool, added_pool, unchanged_names, file_groups=node_group)
    # raw_matches carries the matched node objects themselves (see Task 8's
    # _match_renamed_entities retrofit), so file paths can be recovered
    # directly via node_origin — no second pass or pre-mutation snapshot
    # needed. (The brief's original sketch tried to translate from bare
    # (category, old_name, new_name) 3-tuples, which loses the file path
    # whenever a name collides across two different files touched in the
    # same commit; that gap is why _match_renamed_entities' return type was
    # widened to include the nodes.)
    renamed_pairs: List[Tuple[str, str, str, str, str]] = []
    for category, old_name, old_node, new_name, new_node in raw_matches:
        renamed_pairs.append((
            category, node_origin[id(old_node)], old_name, node_origin[id(new_node)], new_name,
        ))

    gitlink_changes = [
        (kind, sha, path) for kind, sha, path in _gitlink_changes(raw_entries)
        if not _is_ignored_path(path, ignore_patterns)
    ]
    gitmodules_map: Dict[str, Dict[str, str]] = {}
    if any(kind == "add" for kind, _, _ in gitlink_changes):
        gitmodules_map = _git_gitmodules_at(repo_path, commit_hash)

    return results, gitlink_changes, gitmodules_map, renamed_pairs


def _reverse_apply(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    pos: int,
    file_results: List[tuple],
    index_con: Optional[Any] = None,
    persist_claim: bool = True,
    claim_ident: Optional[str] = None,
    absorbed_idents: Optional[List[str]] = None,
) -> str:
    """#222 phase 2b: apply one already-claimed, already-extracted commit --
    structural facts, :modified-in edges, and provisional :introduced-by for
    every entity the commit's "A"/"M" files touch. "D"/"R" files are skipped
    entirely (deletions and renames are out of scope for this sub-phase --
    see the design spec).

    `claim_ident`/`absorbed_idents` (#325 review round 2, the ident-wiring
    slice of Task 5's scope): passed straight through to
    _frontier_persist_claim's own `ident`/`absorbed_idents`. Both default to
    None, which _frontier_persist_claim reads as "the fixed
    :ingestion/frontier-high ident, nothing absorbed" -- correct for every
    caller before #325's provisional side could hold more than one
    interval. Once it can, the caller MUST supply the claim's own target:
    _run_ingestion captures `allocator.last_claim` right after the claim
    that produced this position (before any LATER claim overwrites it) and
    resolves its ident via `_interval_persist_ident`, which prefers the
    interval's own carried `.ident` over re-deriving one from
    `.is_base`/`.anchor_pos` -- `_load_one_interval` no longer resolves an
    ident on the read side either, it carries the one it was handed (#325
    review round 3, Finding 3). Get this wrong and every reverse claim
    persists to :ingestion/frontier-high regardless of which in-memory
    interval it actually belongs to --
    measured producing an INVERTED pair (`lo` from the new claim's hash,
    `hi` still the old interval's) the moment a reverse claim lands above a
    RETAINED interval it does not touch, which is silent since
    `_frontier_read_bounds` returns the pair as-is and nothing checks
    lo <= hi until the next _frontier_load.

    Split out of _reverse_fill_claim_and_process's single body (#222 phase
    2d): `pos` (from allocator.claim_high()) and `file_results` (from
    _extract_commit) are now supplied by the caller instead of produced
    here, so the CPU-bound tree-sitter parse and these DB-bound writes can
    be scheduled onto separate executors. See
    _reverse_fill_claim_and_process's own docstring for why that split
    matters and who is responsible for driving each half.

    Reuses _build_code_triples unchanged for entity discovery/parsing
    (direction-agnostic). BOTH :introduced-by and :modified-in are filtered
    out of _build_code_triples's own output and written by this function
    instead -- _build_code_triples's gate for both attributes is
    entity_valid_from membership, which for forward walk coincides with
    "is this the introduction commit" but for reverse walk does not: the
    first commit reverse walk sees an entity at is the newest touch (not
    the introduction), and the commit where reverse walk finally stops
    finding an even-earlier occurrence (the true introduction) is by then
    already "known" from later, already-visited commits. Reusing
    _build_code_triples's :modified-in emission verbatim would misclassify
    both ends. See the design spec's "Per-commit algorithm" section
    (revised after the final whole-branch review found this defect) for
    the full derivation.

    :introduced-by: a newly-discovered entity's guess is asserted via
    _entity_introduced_by_set_provisional and gets no :modified-in yet
    (mirrors forward walk never emitting :modified-in at an entity's own
    introduction commit). When an already-provisional entity is found at
    an even earlier commit, its guess moves to this commit (also getting
    no :modified-in yet) and the PREVIOUSLY-guessed commit -- now
    confirmed to be a genuine modification, not the introduction --
    retroactively receives a :modified-in fact using its OWN commit
    timestamp (looked up from commit_metadata), not this commit's.
    Already-authoritative entities are never touched, but a genuine later
    touch always gets :modified-in (unless #221's unchanged-body check
    says the body is provably unchanged this commit). "Already
    authoritative" means a live :introduced-by that carries no lineage
    marker -- an entity that is live but holds NO :introduced-by is a torn
    write from an interrupted run, not an authoritative one, and is
    re-introduced here rather than read as confirmed (#313; see the
    classification loop's own comment).

    Does NOT re-date structural facts as a guess moves earlier (#233). They
    stay at the timestamp of the commit where this walk first SIGHTED the
    entity, which is later than the provisional :introduced-by, so ":as-of
    the provisional introduction" reports the entity as nonexistent for as
    long as it stays provisional. The correction sweep closes that window
    when it CONFIRMS the entity (case 1) -- but leaves it open for an entity
    the sweep instead leaves provisional (case 2's fail-safe skip on an
    ambiguous/wrong guess, already logged as skipped): that entity keeps its
    stale-dated structural facts indefinitely, not just until the sweep
    runs. This is the same "temporarily dangling, and convergent" shape as
    the :parent edges below, and the region is already excluded from 2a's
    trust predicate -- but note that an INTERRUPTED run now leaves a wider
    inconsistent window than it did before #233, closed by the next run's
    sweep (for entities that reach case 1, not case 2).

    Known, documented limitation, and who fixes it: the retroactive
    :modified-in for a superseded commit does not re-check #221's
    unchanged-body narrowing against THAT commit's own diff, because only
    this call's diff carries that data -- so it is asserted whenever the
    superseded commit is genuinely later (see the monotonicity guard
    above). This can over-assert an edge forward walk would have
    suppressed; it can never produce a missing or misattributed one.

    2c's correction sweep corrects it, in the ordinary already-authoritative
    branch of _correction_sweep_apply: walking forward with its own parse in
    hand, it retracts a :modified-in whose entity reads as unchanged at that
    commit. Note the shape of the fix -- 2c repairs the fact after the fact
    rather than this walk preventing the write, which is why this stays a
    documented limitation here rather than a bug. Phrased carefully because
    an intermediate draft of 2c's spec dropped the case entirely, leaving it
    briefly owned by nobody (see the 2b review).

    Returns the applied commit's hash. Writes for one commit are followed
    by exactly one _db_checkpoint(db) call, after _frontier_persist_claim
    records the claim -- mirrors _run_ingestion's one-checkpoint-per-commit
    cadence (see the design spec's "Resume-safety / atomicity boundary"
    section).

    `persist_claim=False` does everything except record the claim (#326
    Finding A). The caller uses it to keep frontier-high's :lo-hash from
    descending past a position this run failed to complete; see the guard at
    the persist site at the bottom of this function.
    """
    # commit_metadata is indexed POSITIONALLY against linearization below,
    # while _frontier_persist_claim persists linearization[pos] -- so a
    # misaligned list makes this walk attribute entities to one commit and
    # persist another: silent, systematic misattribution. _run_ingestion
    # builds a watermark-relative list for its own use
    # (_git_commits(repo, watermark, branch)), which is exactly the wrong
    # thing to hand this function. _reverse_fill_claim_and_process checks
    # this before claim_high() so a bad call does not consume a position;
    # this copy is duplicated here because 2d calls _reverse_apply directly,
    # after already claiming pos itself.
    if len(commit_metadata) != len(linearization):
        raise ValueError(
            "commit_metadata must be full-history and positionally aligned with "
            f"linearization (got {len(commit_metadata)} entries vs {len(linearization)}); "
            "pass _git_commits(repo, watermark_hash=None)"
        )

    commit_hash, commit_ts_iso, author, subject = commit_metadata[pos]
    if commit_hash != linearization[pos]:
        raise ValueError(
            f"commit_metadata[{pos}] is {commit_hash}, but linearization[{pos}] is "
            f"{linearization[pos]}: the two must be positionally aligned"
        )
    commit_ident = f":commit/{commit_hash[:12]}"

    all_triples: List[str] = [
        f"[{commit_ident} :entity-type :type/commit]",
        f'[{commit_ident} :ident "{commit_ident}"]',
        f'[{commit_ident} :description "{_edn_escape(subject[:120])}"]',
        f'[{commit_ident} :hash "{commit_hash}"]',
        f'[{commit_ident} :author "{_edn_escape(author)}"]',
        f'[{commit_ident} :subject "{_edn_escape(subject[:200])}"]',
        f'[{commit_ident} :date "{commit_ts_iso}"]',
    ]
    pos_by_commit_ident = {f":commit/{h[:12]}": i for i, (h, _t, _a, _s) in enumerate(commit_metadata)}
    ts_by_commit_ident = {f":commit/{h[:12]}": ts for h, ts, _a, _s in commit_metadata}
    new_candidates: List[str] = []
    provisional_moves: List[Tuple[str, str]] = []  # (ident, superseded_commit_ident)
    already_authoritative_touched: List[Tuple[str, Optional[str]]] = []
    unchanged_by_ident: Dict[str, bool] = {}

    for status, file_path, extracted, precomputed, _old_path in file_results:
        if status not in ("A", "M"):
            continue  # "D"/"R" deferred -- see design spec scope

        candidate_idents = (
            [precomputed["module_ident"]]
            + [ident for ident, _name, _t in precomputed["function_entries"]]
            + [ident for ident, _name, _t in precomputed["class_entries"]]
            + [ident for ident, _name, _t in precomputed["global_entries"]]
            + [ident for ident, _name, _t in precomputed["field_entries"]]
        )
        # #231: LIVENESS, not lineage. _entity_introduced_by_query was unsound
        # here -- a closed-and-purged entity kept its :introduced-by forever,
        # so this gate answered "known" for it, _build_code_triples took its
        # "already known" branch, and the entity came back as a ghost with no
        # current :ident. Same one query per candidate ident, so #239's cost
        # profile is unchanged.
        known_before: Dict[str, str] = {
            ident: "known" for ident in candidate_idents
            if _entity_ident_is_live(db, ident)
        }
        known_before_snapshot = set(known_before.keys())

        triples = _build_code_triples(
            file_path, extracted, commit_ts_iso, known_before, {}, {}, commit_ident,
            precomputed, {}, {},
        )
        # This walk owns the write timing of BOTH attributes itself now --
        # filter both out of _build_code_triples's forward-biased gating.
        all_triples.extend(
            t for t in triples if ":introduced-by" not in t and ":modified-in" not in t
        )

        unchanged_idents = precomputed.get("unchanged_idents", set())
        for ident in candidate_idents:
            unchanged_by_ident[ident] = ident in unchanged_idents

        new_candidates.extend(set(known_before.keys()) - known_before_snapshot)
        for ident in set(candidate_idents) & known_before_snapshot:
            if _lineage_is_provisional(db, ident):
                superseded_ident = _entity_introduced_by_query(db, ident)
                provisional_moves.append((ident, superseded_ident))
                continue
            intro_ident = _entity_introduced_by_query(db, ident)
            if intro_ident is None:
                # TORN by an interrupted run (#313). One commit's writes are
                # a SEQUENCE of _transact calls, not one atomic unit: the
                # structural facts above (:ident included) land first and
                # the provisional :introduced-by lands several calls later,
                # so a process killed in between leaves an entity that is
                # live, has no lineage, and has no lineage marker either --
                # and _frontier_persist_claim, last of all, never ran, so
                # the resumed run re-claims this very position and arrives
                # back here.
                #
                # Live-but-unintroduced is NOT authoritative. Reading it as
                # such was the defect: the branch below only ever considers
                # :modified-in, so the entity kept no :introduced-by at all,
                # and _correction_sweep_apply could not repair it either --
                # its case 3 reads zero values as ambiguous and fail-safe
                # skips. The run then reported complete with nothing on
                # stderr. Treating it as newly discovered is exactly right:
                # this walk is re-applying the commit whose interrupted
                # write created it, so the guess it gets here is the one an
                # uninterrupted run would have written.
                new_candidates.append(ident)
                continue
            already_authoritative_touched.append((ident, intro_ident))

    # Split :contains out before batching (#222 phase 2b1). Minigraf's EAVT
    # pending index lacks value bytes in the key, so batching multiple
    # [module :contains fn] facts in ONE transact silently keeps only the
    # last -- five of six containment edges on an ordinary multi-entity file.
    # Forward walk splits them for exactly this reason (see _run_ingestion's
    # own one-transact-per-edge loop and _ingest_close's docstring). Only
    # :contains repeats (entity, attribute) within this batch: the
    # :modified-in triples below are one per DISTINCT entity, and facts
    # differing in entity do not collide.
    contains_triples = [t for t in all_triples if ":contains" in t]
    other_triples = [t for t in all_triples if ":contains" not in t]
    _transact(db, "[" + " ".join(other_triples) + "]", commit_ts_iso, index_con=index_con)
    for contains_triple in contains_triples:
        _transact(db, "[" + contains_triple + "]", commit_ts_iso, index_con=index_con)

    # :parent edges (#222 phase 2b1), one transact per parent -- a merge
    # commit has two, and they share (entity, attribute, valid_from), the
    # same EAVT collision :contains has. The bootstrap `ancestor` rule is
    # defined purely over :parent, so without these it returns nothing
    # across the whole reverse-filled region. Reverse walk reaches a
    # commit's parents LATER than the commit itself, so the edge points at
    # an entity that does not exist yet and materialises when the walk
    # descends to it (or when the forward stream covers it, for parents
    # below frontier-high's floor): temporarily dangling, and convergent --
    # the same shape as the lineage facts around it.
    for parent_hash in _git_parent_hashes(repo_path, commit_hash):
        _transact(
            db, f"[[{commit_ident} :parent :commit/{parent_hash[:12]}]]",
            commit_ts_iso, index_con=index_con,
        )

    # Third of the three write paths the monotonicity invariant covers
    # (#222 phase 2b1), and the one the 2b review did not list: an entity
    # whose :introduced-by is already authoritative could still be handed a
    # :modified-in at an EARLIER claimed commit, with no check at all. That
    # is also the symptom half of the 2c interleaving (a sweep confirms an
    # entity, then this walk descends past it). Suppressing the edge does
    # not fix that entity's lineage -- reverse walk must never clobber an
    # authoritative :introduced-by -- so 2c's gap-closed precondition stays
    # load-bearing; this only stops the contradictory fact being written.
    authoritative_modified_triples = []
    for ident, intro_ident in already_authoritative_touched:
        if unchanged_by_ident.get(ident, False):
            continue
        intro_pos = pos_by_commit_ident.get(intro_ident) if intro_ident is not None else None
        if intro_pos is not None and pos <= intro_pos:
            print(
                f"[_reverse_apply] skipping :modified-in for {ident} at "
                f"position {pos}: not later than its introduction {intro_ident} "
                f"(position {intro_pos})",
                file=sys.stderr,
            )
            continue
        authoritative_modified_triples.append(f"[{ident} :modified-in {commit_ident}]")
    if authoritative_modified_triples:
        _transact(
            db, "[" + " ".join(authoritative_modified_triples) + "]", commit_ts_iso, index_con=index_con,
        )

    _entity_introduced_by_set_provisional_batch(
        db, new_candidates, commit_ident, commit_ts_iso, index_con=index_con,
        pos=pos, pos_by_commit_ident=pos_by_commit_ident,
    )
    _entity_introduced_by_set_provisional_batch(
        db, [ident for ident, _superseded in provisional_moves], commit_ident,
        commit_ts_iso, index_con=index_con,
        pos=pos, pos_by_commit_ident=pos_by_commit_ident,
    )

    # #233: one transact per entity here was 10,161 calls over 12 commits of
    # this repo. Each edge is asserted at the SUPERSEDED commit's own
    # timestamp, not this commit's, so one batch is impossible -- but
    # entities touched by one commit were almost always last sighted at the
    # same commit, so grouping by that timestamp collapses to a handful.
    # Every per-ident gate below stays per-ident and is evaluated during
    # grouping. Facts differing in entity do not collide, so each group is
    # one safe transact.
    retroactive_by_ts: Dict[str, List[str]] = {}
    for ident, superseded_ident in provisional_moves:
        if superseded_ident is None or superseded_ident == commit_ident:
            continue
        superseded_pos = pos_by_commit_ident.get(superseded_ident)
        if superseded_pos is not None and superseded_pos <= pos:
            # The move was refused (or would be): the "superseded" guess is
            # not actually later than this commit, so asserting it as a
            # modification would claim the entity changed at or before its
            # own introduction.
            continue
        superseded_ts = ts_by_commit_ident.get(superseded_ident)
        if superseded_ts is None:
            # Falling back to THIS commit's timestamp back-dates the edge to
            # before the modification it describes -- a fact asserted valid
            # before it was true. Skip and say so instead.
            print(
                f"[_reverse_apply] skipping retroactive :modified-in "
                f"for {ident} at {superseded_ident}: no timestamp in commit_metadata",
                file=sys.stderr,
            )
            continue
        retroactive_by_ts.setdefault(superseded_ts, []).append(
            f"[{ident} :modified-in {superseded_ident}]"
        )
    for superseded_ts, triples in retroactive_by_ts.items():
        _transact(db, "[" + " ".join(triples) + "]", superseded_ts, index_con=index_con)

    # #326 (Finding A): `persist_claim=False` withholds the BOOKKEEPING for this
    # position, never the work -- every triple above has already been written.
    # See _run_ingestion's `rev_claim_floor` (per target ident since #325) for
    # why: :lo-hash is a RANGE bound, so claiming a position below one whose
    # write FAILED silently swallows the failed position into the interval,
    # and the archive then turns that into a permanent skip. Do NOT
    # "optimize" this into skipping the
    # writes as well: the run below the floor is still doing real, necessary
    # ingestion, it is simply not entitled to assert that it completed.
    if persist_claim:
        _frontier_persist_claim(
            db, linearization, pos, from_low=False, commit_ts_iso=commit_ts_iso,
            index_con=index_con, ident=claim_ident, absorbed_idents=absorbed_idents,
        )
    _db_checkpoint_gated(db)
    return commit_hash


def _reverse_fill_claim_and_process(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    allocator: "frontier_registry.FrontierAllocator",
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> Optional[str]:
    """NOT REACHABLE FROM _run_ingestion, AND MUST NOT BECOME SO. This persists
    its claim unconditionally and has no floor/ceiling concept, so a write
    that raises is swallowed by the interval's closed-range semantics --
    #326 Critical 3, which is permanent silent commit loss that every
    at-scale detector reads clean. Pinned by
    test_run_ingestion_does_not_call_the_legacy_walk_wrappers.

    Synchronous convenience wrapper: claim one position from the gap's
    high end and process it. Kept for tests and _reverse_bulk_fill_walk.

    **2d must not call this from async code** -- it fuses the CPU-bound
    tree-sitter parse and the DB-bound writes into one function body, so it
    can only be scheduled onto one executor as a unit. On write_executor (a
    thread) that parse holds the GIL for its whole duration, which is
    exactly the event-loop starvation #116 introduced the process pool to
    fix. 2d awaits _extract_commit on the process pool and _reverse_apply on
    write_executor instead. Same caveat 2c's own wrappers carry.

    Returns the claimed commit's hash, or None if the gap was already empty.
    """
    if len(commit_metadata) != len(linearization):
        raise ValueError(
            "commit_metadata must be full-history and positionally aligned with "
            f"linearization (got {len(commit_metadata)} entries vs {len(linearization)}); "
            "pass _git_commits(repo, watermark_hash=None)"
        )
    pos = allocator.claim_high()
    if pos is None:
        return None
    file_results, _gitlink_changes, _gitmodules_map, _renamed_pairs = _extract_commit(
        repo_path, linearization[pos], ignore_patterns
    )
    return _reverse_apply(
        db, repo_path, linearization, commit_metadata, pos, file_results, index_con=index_con,
    )


def _reverse_bulk_fill_walk(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    allocator: "frontier_registry.FrontierAllocator",
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> int:
    """NOT REACHABLE FROM _run_ingestion, AND MUST NOT BECOME SO. This persists
    its claim unconditionally and has no floor/ceiling concept, so a write
    that raises is swallowed by the interval's closed-range semantics --
    #326 Critical 3, which is permanent silent commit loss that every
    at-scale detector reads clean. Pinned by
    test_run_ingestion_does_not_call_the_legacy_walk_wrappers.

    #222 phase 2b: repeatedly call _reverse_fill_claim_and_process until
    the gap closes. Returns the count of commits processed. No caller in
    this sub-phase -- 2d wires this into the real concurrent ingestion
    loop alongside the forward stream.

    Stops loudly if a claim ever fails to move strictly downward (#222
    phase 2b1). This loop is unbounded by construction and drives a git
    subprocess, a tree-sitter parse, a DB write batch and a _db_checkpoint
    fsync on every iteration, so an allocator that stops making progress
    turns it into an unbounded fsync loop inside what 2d intends to run as a
    background task -- which is exactly what happened before 2b1 fixed
    FrontierAllocator._extend. The guard stays even though that root cause
    is fixed: it converts any future allocator regression from a hang into a
    stop plus a stderr line.
    """
    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    count = 0
    last_pos: Optional[int] = None
    while True:
        result = _reverse_fill_claim_and_process(
            db, repo_path, linearization, commit_metadata, allocator,
            ignore_patterns=ignore_patterns, index_con=index_con,
        )
        if result is None:
            break
        count += 1
        pos = hash_to_pos.get(result)
        if pos is not None and last_pos is not None and pos >= last_pos:
            print(
                f"[_reverse_bulk_fill_walk] no progress: claim returned position {pos} "
                f"after {last_pos}; stopping after {count} commits to avoid spinning",
                file=sys.stderr,
            )
            break
        last_pos = pos
    return count


def _forward_apply(
    db: Any,
    repo_path: str,
    state: "_ForwardWalkState",
    commit: Tuple[str, str, str, str],
    extracted: Tuple[list, list, dict, list],
    index_con: Optional[Any] = None,
    linearization: Optional[List[str]] = None,
    pos: Optional[int] = None,
    lifecycle_only: bool = False,
    persist_claim: bool = True,
) -> None:
    """Apply one commit's forward-walk writes.

    Moved verbatim out of _run_ingestion's `while pending:` loop (issue #222
    phase 2d) so a single commit's write section can be driven per-commit by
    the two-stream interleave. Purely synchronous: the eight individual
    `run_in_executor(write_executor, ...)` submissions the inline body used
    became direct calls, and the caller submits this whole function to
    write_executor once instead.

    linearization/pos are the allocator's view of this commit. When both are
    supplied (Stage A's forward stream always does), this also persists the
    frontier-low claim and advances the lineage-confirmed-through watermark;
    both default to None so the existing tests that call this function with a
    bare commit tuple stay valid.

    persist_claim (#342) is the forward mirror of _reverse_apply's own
    parameter: False withholds this position's BOOKKEEPING -- the
    frontier-low claim, :ingestion/watermark and
    :ingestion/lineage-confirmed-through -- while every triple above is
    still written. All three move together because all three mean
    "contiguous from C0", so a gate holding back only one would leave the
    other two asserting past a position this run failed to complete. See
    _run_ingestion's `fwd_claim_ceiling` for why that matters and do NOT
    "optimize" this into skipping the writes as well.

    lifecycle_only (#222 phase 2d, Stage B) restricts this to the facts the
    REVERSE stream never wrote for a commit in frontier-high's territory --
    deletions, renames, dependency-edge churn and gitlink changes -- while
    leaving that commit's "A"/"M" entity facts alone. It is not a re-run of
    the forward body: nothing wrote D/R or gitlink facts for these commits,
    so there is nothing to deduplicate against, whereas re-running the A/M
    emission WOULD duplicate (minigraf mints a genuinely live duplicate when
    the same (entity, attribute, value) is re-transacted at a different
    valid-from -- issue #156). Concretely, when true:

    * The commit's own :type/commit entity and its :parent edges are skipped
      -- _reverse_apply already wrote both for every commit in this range
      (the same reason _correction_sweep_apply gives for not writing them).
    * "D" files, "R" files and gitlink changes are processed unchanged. "R"
      keeps its _build_code_triples emission for the NEW path: the reverse
      stream skipped renames entirely, so nothing ever wrote those entities.
    * "A"/"M" files still CALL _build_code_triples, and its returned triples
      are DISCARDED -- including for an entity reborn after a removal this
      pass already closed, whose introduction _correction_sweep_apply has
      written just before this call (#349). This looks redundant and is
      not: state.entity_valid_from
      after Stage A covers only positions 0..meeting_point, so an entity
      introduced INSIDE the reverse region is absent from it and a later
      deletion of that entity within the same region would close with
      orig_ts falling back to the delete commit's own timestamp -- a wrong,
      often zero-width valid interval. Calling the function and dropping its
      output keeps entity_valid_from / entity_descriptions / file_entities /
      field_class_ident / field_static_ident current at zero fact cost, and
      the sweep's ascending order makes the recorded timestamp the correct
      EARLIEST one (_build_code_triples only writes entity_valid_from[ident]
      when the ident is not already present).
    * Provisional-lineage reconciliation is skipped for "A"/"M" --
      _correction_sweep_apply owns those entities' lineage and has already
      run for this commit. It is still performed for an "R" file's new path,
      where the reverse stream may hold a provisional guess at a LATER
      commit that this rename supersedes; there the DB
      (_lineage_is_provisional) is consulted directly, per ident.
    * _watermark_update, _frontier_persist_claim and
      _lineage_confirmed_through_update are skipped. Stage B tracks its own
      progress through :ingestion/correction-sweep-through, and the forward
      frontier must not appear to advance into the reverse region.
    """
    commit_hash, commit_ts_iso, author, subject = commit
    extracted_files, gitlink_changes, gitmodules_map, renamed_pairs = extracted
    commit_ident = f":commit/{commit_hash[:12]}"
    reason = f"git:{commit_hash} {author}: {subject}"

    add_triples: List[str] = [] if lifecycle_only else [
        f"[{commit_ident} :entity-type :type/commit]",
        f'[{commit_ident} :ident "{commit_ident}"]',
        f'[{commit_ident} :description "{_edn_escape(subject[:120])}"]',
        f'[{commit_ident} :hash "{commit_hash}"]',
        f'[{commit_ident} :author "{_edn_escape(author)}"]',
        f'[{commit_ident} :subject "{_edn_escape(subject[:200])}"]',
        f'[{commit_ident} :date "{commit_ts_iso}"]',
    ]
    close_items: List[tuple] = []  # (triples, original_ts_iso)
    closed_idents: List[str] = []  # idents closed this commit (lineage discard)
    dep_add_triples: List[str] = []  # :depends-on triples to transact individually
    # Old paths of files renamed this commit (R status). Their
    # unmatched child entities / dependency edges are closed in a
    # final pass after renamed_pairs is consumed (see below).
    renamed_old_paths: set = set()

    for status, file_path, extracted, precomputed, old_path in extracted_files:
        if status == "D":
            # Close module and all known child entities for this file.
            # Iterate a copy: _forget_closed_entity mutates
            # state.file_entities[file_path] in place as it purges.
            idents = list(state.file_entities.get(file_path, [_code_ident("module", file_path)]))
            module_ident = _code_ident("module", file_path)
            for ident in idents:
                orig_ts = state.entity_valid_from.get(ident, commit_ts_iso)
                desc = state.entity_descriptions.get(ident, "")
                close_items.append(
                    (_build_close_triples(
                        ident, desc, module_ident,
                        state.field_class_ident.get(ident),
                        close_entity_type=True, file_value=file_path,
                        is_static=state.field_static_ident.get(ident),
                        introduced_by=_resolve_introduced_by(db, state, ident),
                    ), orig_ts)
                )
                _forget_closed_entity(
                    ident, file_path, state.entity_valid_from,
                    state.entity_descriptions, state.field_class_ident, state.file_entities,
                    state.field_static_ident, state.entity_introduced_by,
                )
                closed_idents.append(ident)
            # Whole file is gone: drop its (now-empty) state.file_entities key
            # so nothing stale lingers under this path (matches state.file_deps).
            state.file_entities.pop(file_path, None)
            # Close all :depends-on edges for the deleted module
            for dep_ident in state.file_deps.get(file_path, set()):
                orig_ts = state.dep_valid_from.get((module_ident, dep_ident), commit_ts_iso)
                close_items.append(
                    ([f"[{module_ident} :depends-on {dep_ident}]"], orig_ts)
                )
            state.file_deps.pop(file_path, None)
        else:  # A or M or R
            if status == "R" and old_path:
                renamed_old_paths.add(old_path)
                old_module_ident = _code_ident("module", old_path)
                new_module_ident = _code_ident("module", file_path)
                add_triples.append(f"[{new_module_ident} :renamed-from {old_module_ident}]")
                # :renamed-to is a brand-new fact that becomes
                # true at the rename commit and stays true forever
                # after — it must be transacted open-ended (like
                # :renamed-from), NOT closed with the old entity's
                # historical valid window via _ingest_close.
                add_triples.append(f"[{old_module_ident} :renamed-to {new_module_ident}]")
                old_desc = state.entity_descriptions.get(old_module_ident, old_path)
                orig_ts = state.entity_valid_from.get(old_module_ident, commit_ts_iso)
                close_items.append((
                    _build_close_triples(
                        old_module_ident, old_desc, old_module_ident,
                        close_entity_type=True, file_value=old_path,
                        introduced_by=_resolve_introduced_by(db, state, old_module_ident),
                    ),
                    orig_ts,
                ))
                # Purge the closed old module. Its remaining child
                # entities under old_path are closed+purged by the
                # renamed_old_paths pass below, which also pops the
                # whole state.file_entities[old_path] key — so only the
                # scalar dicts and the module's own list slot need
                # dropping here.
                _forget_closed_entity(
                    old_module_ident, old_path, state.entity_valid_from,
                    state.entity_descriptions, state.field_class_ident, state.file_entities,
                    state.field_static_ident, state.entity_introduced_by,
                )
                closed_idents.append(old_module_ident)
            previous_idents = set(state.file_entities.get(file_path, []))
            # #222 phase 2d: an entity Stream 2 already introduced
            # provisionally is NOT authoritatively introduced, so the
            # forward walk must treat it as new. Popping it out of
            # entity_valid_from is what makes _build_code_triples's own
            # gate (entity_valid_from membership) agree -- deliberately
            # in preference to widening that function's signature, since
            # its gate means "is this the introduction" for a forward
            # walk and nothing else should depend on that meaning.
            #
            # It is also semantically right on its own terms: the
            # valid_from Stream 2 recorded is a wrong guess, and must
            # never be used as an orig_ts for a close.
            #
            # #235: _lineage_is_provisional(db, ident) is the SOLE authority
            # for reconcilability, asked per ident, right here. There is no
            # preloaded set, and none may be reintroduced as a prefilter --
            # that is the bug this fix removed, in both directions:
            #
            #   * stale-NEGATIVE. A run-start snapshot is EMPTY on a fresh
            #     ingest, and Stream 2 writes its guesses during that same
            #     run. Prefiltering through it dropped every same-run guess
            #     before the DB check could see it,
            #     _forward_reconcile_provisional never fired, and
            #     _build_code_triples minted a SECOND :introduced-by
            #     alongside the guess. A prefilter's false negatives are
            #     unrecoverable precisely because the authority never runs.
            #   * stale-POSITIVE. By the time this commit is reached, an
            #     ident such a snapshot listed may already be authoritative
            #     in the DB (reconciled earlier in this same forward pass, or
            #     by a previous run's correction sweep). Popping
            #     entity_valid_from for it hands it to _build_code_triples as
            #     "new" and mints the same second fact, while
            #     _forward_reconcile_provisional no-ops and never retracts it.
            #
            # lifecycle_only (Stage B): _correction_sweep_apply owns "A"/"M"
            # lineage and has already run for this very commit, so
            # reconciling here would mint a second :introduced-by behind its
            # back. An "R" file's NEW path is the one case that still needs
            # it -- nothing wrote those entities in Stage A, so the reverse
            # stream's provisional guess (if any) sits at a LATER commit that
            # this rename supersedes.
            candidates = (
                _forward_candidate_idents(precomputed)
                if (not lifecycle_only or status == "R") else []
            )
            reconcilable = [
                ident for ident in candidates if _lineage_is_provisional(db, ident)
            ]
            # Built once per file rather than scanned per ident (matches
            # _correction_sweep_apply's candidate_triples_by_ident
            # precedent) -- a per-ident linear scan here is O(n^2) per file
            # once there is more than one reconcilable entity.
            structural_triples_by_ident = (
                _forward_structural_triples_by_ident(precomputed) if reconcilable else {}
            )
            for ident in reconcilable:
                state.entity_valid_from.pop(ident, None)
                if ident not in structural_triples_by_ident:
                    # Unreachable in normal operation: _forward_candidate_idents
                    # and _forward_structural_triples_by_ident scan the same
                    # five sources. If they ever desynchronize, failing loudly
                    # beats a silent [] -- that would let
                    # _forward_reconcile_provisional retract the guess and
                    # confirm lineage while skipping the re-dating, stranding
                    # structural facts at the wrong valid-time.
                    raise RuntimeError(
                        f"_forward_structural_triples_by_ident has no entry for "
                        f"{ident!r}, but it came from _forward_candidate_idents, "
                        "which scans the same five sources -- the two have "
                        "desynchronized"
                    )
                _forward_reconcile_provisional(
                    db, ident, structural_triples_by_ident[ident],
                    commit_ts_iso, state.ts_by_commit_ident, commit_ident,
                    index_con=index_con,
                )
            triples = _build_code_triples(
                file_path, extracted, commit_ts_iso, state.entity_valid_from,
                state.entity_descriptions, state.file_entities, commit_ident, precomputed,
                state.field_class_ident, state.field_static_ident, state.entity_introduced_by,
            )
            # lifecycle_only: called for its dict side effects, output
            # DISCARDED for "A"/"M" (see this function's docstring). "R" keeps
            # it -- the reverse stream skipped renames entirely, so nothing
            # ever wrote the new path's entities.
            if not lifecycle_only or status == "R":
                add_triples.extend(triples)
            # Detect entities removed from a modified file.
            # _build_code_triples only appends to state.file_entities, never removes.
            # Compare previous idents against the idents derivable from the
            # current extraction to find what was deleted.
            if status == "M":
                module_ident = _code_ident("module", file_path)
                current_extracted_idents: set = {module_ident}
                for fn_ident, _fn_name, _fn_triples in precomputed["function_entries"]:
                    current_extracted_idents.add(fn_ident)
                for cls_ident, _cls_name, _cls_triples in precomputed["class_entries"]:
                    current_extracted_idents.add(cls_ident)
                # Globals and fields are tracked in state.file_entities too
                # (see _build_code_triples): omitting them here would
                # make every still-present global/field look "removed"
                # on any later edit and wrongly close it (#113).
                for gvar_ident, _gvar_name, _gvar_triples in precomputed["global_entries"]:
                    current_extracted_idents.add(gvar_ident)
                for field_ident, _field_name, _field_triples in precomputed["field_entries"]:
                    current_extracted_idents.add(field_ident)
                removed_idents = previous_idents - current_extracted_idents
                # An in-place rename (old->new in the same file) is
                # closed with :renamed-to linkage by the renamed_pairs
                # loop below; exclude those old idents here so they are
                # not ALSO closed as a plain removal (double close).
                same_file_renamed_old_idents = {
                    _code_ident(cat, o_file, o_name)
                    for cat, o_file, o_name, _n_file, _n_name in renamed_pairs
                    if o_file == file_path
                }
                removed_idents -= same_file_renamed_old_idents
                for ident in removed_idents:
                    orig_ts = state.entity_valid_from.get(ident, commit_ts_iso)
                    desc = state.entity_descriptions.get(ident, "")
                    close_items.append(
                        (_build_close_triples(
                            ident, desc, module_ident,
                            state.field_class_ident.get(ident),
                            close_entity_type=True, file_value=file_path,
                            is_static=state.field_static_ident.get(ident),
                            introduced_by=_resolve_introduced_by(db, state, ident),
                        ), orig_ts)
                    )
                    # File survives (M), only this child was removed:
                    # purge just this ident from the file's list.
                    _forget_closed_entity(
                        ident, file_path, state.entity_valid_from,
                        state.entity_descriptions, state.field_class_ident, state.file_entities,
                        state.field_static_ident, state.entity_introduced_by,
                    )
                    closed_idents.append(ident)
            # Compute dep edges for this file and diff against previous.
            # Resolution itself already happened in _extract_commit
            # (precomputed["resolved_imports"]) against that commit's
            # own git-ls-tree state — nothing left to resolve here.
            module_ident = _code_ident("module", file_path)
            current_deps: set = set()
            for import_name, dep_ident, is_resolved in precomputed["resolved_imports"]:
                if dep_ident != module_ident:
                    current_deps.add(dep_ident)
                    is_relative = import_name.startswith(".")
                    if not is_resolved and not is_relative and dep_ident not in state.entity_valid_from:
                        add_triples.extend([
                            f"[{dep_ident} :entity-type :type/external-dependency]",
                            f'[{dep_ident} :ident "{_edn_escape(dep_ident)}"]',
                            f'[{dep_ident} :description "{_edn_escape(import_name)}"]',
                        ])
                        state.entity_valid_from[dep_ident] = commit_ts_iso
                        state.entity_descriptions[dep_ident] = import_name
                        state.unresolved_dep_idents[dep_ident] = import_name
                        # #112: an already-known submodule may be the real
                        # target this unresolvable import was reaching for
                        # (submodule directories are never in state.file_entities,
                        # so any import into one always falls through here).
                        for sub_ident, sub_path in state.submodule_paths.items():
                            if _submodule_path_matches_import(sub_path, import_name):
                                add_triples.append(f"[{dep_ident} :resolves-to {sub_ident}]")
            previous_deps = state.file_deps.get(file_path, set())
            for dep_ident in current_deps - previous_deps:
                dep_add_triples.append(f"[{module_ident} :depends-on {dep_ident}]")
                state.dep_valid_from[(module_ident, dep_ident)] = commit_ts_iso
            if status == "M":
                for dep_ident in previous_deps - current_deps:
                    orig_ts = state.dep_valid_from.get((module_ident, dep_ident), commit_ts_iso)
                    close_items.append(
                        ([f"[{module_ident} :depends-on {dep_ident}]"], orig_ts)
                    )
            state.file_deps[file_path] = current_deps

    # Function/class rename linkage (Task 9's renamed_pairs).
    # Module-level linkage is handled separately per-file
    # above (Task 5) since it comes from git's own -M
    # detection, not this commit-wide matcher.
    for category, old_file, old_name, new_file, new_name in renamed_pairs:
        old_ident = _code_ident(category, old_file, old_name)
        new_ident = _code_ident(category, new_file, new_name)
        add_triples.append(f"[{new_ident} :renamed-from {old_ident}]")
        # :renamed-to becomes true at the rename commit and stays
        # open-ended thereafter — transact it via the add path, do
        # NOT fold it into the old entity's _ingest_close window.
        add_triples.append(f"[{old_ident} :renamed-to {new_ident}]")
        old_desc = state.entity_descriptions.get(old_ident, old_name)
        old_module_ident = _code_ident("module", old_file)
        orig_ts = state.entity_valid_from.get(old_ident, commit_ts_iso)
        close_items.append((
            _build_close_triples(
                old_ident, old_desc, old_module_ident,
                state.field_class_ident.get(old_ident),
                close_entity_type=True, file_value=old_file,
                is_static=state.field_static_ident.get(old_ident),
                introduced_by=_resolve_introduced_by(db, state, old_ident),
            ),
            orig_ts,
        ))
        _forget_closed_entity(
            old_ident, old_file, state.entity_valid_from,
            state.entity_descriptions, state.field_class_ident, state.file_entities,
            state.field_static_ident, state.entity_introduced_by,
        )
        closed_idents.append(old_ident)

    # A file rename (R status) only closes the old MODULE above.
    # Child entities and dependency edges under the old path are
    # closed here as plain removals UNLESS the matcher established
    # a rename continuity edge for them (handled with :renamed-to
    # by the loop above). This runs after renamed_pairs is fully
    # consumed so those confirmed renames can be excluded; without
    # it, unmatched old children/deps leak open forever under the
    # old path while new ones open under the new path.
    if renamed_old_paths:
        renamed_covered_idents = {
            _code_ident(cat, o_file, o_name)
            for cat, o_file, o_name, _n_file, _n_name in renamed_pairs
        }
        for r_old_path in renamed_old_paths:
            r_old_module_ident = _code_ident("module", r_old_path)
            # Iterate a copy: _forget_closed_entity mutates
            # state.file_entities[r_old_path] in place as it purges.
            for ident in list(state.file_entities.get(r_old_path, [])):
                if ident == r_old_module_ident:
                    continue  # already closed+purged by the R block above
                if ident in renamed_covered_idents:
                    continue  # already closed+purged with :renamed-to linkage
                orig_ts = state.entity_valid_from.get(ident, commit_ts_iso)
                desc = state.entity_descriptions.get(ident, "")
                close_items.append(
                    (_build_close_triples(
                        ident, desc, r_old_module_ident,
                        state.field_class_ident.get(ident),
                        close_entity_type=True, file_value=r_old_path,
                        is_static=state.field_static_ident.get(ident),
                        introduced_by=_resolve_introduced_by(db, state, ident),
                    ), orig_ts)
                )
                _forget_closed_entity(
                    ident, r_old_path, state.entity_valid_from,
                    state.entity_descriptions, state.field_class_ident, state.file_entities,
                    state.field_static_ident, state.entity_introduced_by,
                )
                closed_idents.append(ident)
            # Whole old path is gone (renamed away): drop the key so
            # no stale ident lingers to be re-discovered by a later
            # commit that reuses this path (e.g. a shim at old_path).
            state.file_entities.pop(r_old_path, None)
            for dep_ident in state.file_deps.get(r_old_path, set()):
                orig_ts = state.dep_valid_from.get((r_old_module_ident, dep_ident), commit_ts_iso)
                close_items.append(
                    ([f"[{r_old_module_ident} :depends-on {dep_ident}]"], orig_ts)
                )
            state.file_deps.pop(r_old_path, None)

    # Process gitlink changes (submodule add/bump/remove).
    # The "remove" case's interaction with the ordinary per-file module-open
    # logic (elsewhere in this loop) is only sound because real submodule paths
    # are extensionless (no tree-sitter parser matches them, so no module is
    # ever opened for a bare gitlink path) — a gitlink path that happened to
    # carry a recognized source extension is an untested, unreachable-in-practice edge case.
    for kind, sha, path in gitlink_changes:
        ext_ident = _code_ident("module", path)
        if kind == "add":
            info = gitmodules_map.get(path, {})
            name = info.get("name", "")
            url = info.get("url", "")
            description = name or path
            ext_triples = [
                f"[{ext_ident} :entity-type :type/external-dependency]",
                f'[{ext_ident} :ident "{_edn_escape(ext_ident)}"]',
                f'[{ext_ident} :description "{_edn_escape(description)}"]',
                f'[{ext_ident} :path "{_edn_escape(path)}"]',
                f'[{ext_ident} :pinned-commit "{_edn_escape(sha)}"]',
                f"[{ext_ident} :introduced-by {commit_ident}]",
            ]
            if name:
                ext_triples.append(f'[{ext_ident} :submodule-name "{_edn_escape(name)}"]')
            if url:
                ext_triples.append(f'[{ext_ident} :submodule-url "{_edn_escape(url)}"]')
            add_triples.extend(ext_triples)
            state.entity_valid_from[ext_ident] = commit_ts_iso
            state.entity_descriptions[ext_ident] = description
            state.pinned_commit_state[ext_ident] = (sha, commit_ts_iso)
            state.submodule_paths[ext_ident] = path
            # #112: link any pre-existing unresolved-import stub whose
            # import path reaches into this submodule — the ordering in
            # the issue's own repro (stub created before the submodule
            # was ever added), which the per-import check above can't
            # catch since the submodule wasn't known yet at that time.
            for stub_ident, import_name in state.unresolved_dep_idents.items():
                if _submodule_path_matches_import(path, import_name):
                    add_triples.append(f"[{stub_ident} :resolves-to {ext_ident}]")
        elif kind == "bump":
            old_sha, orig_ts = state.pinned_commit_state.get(ext_ident, (None, commit_ts_iso))
            if old_sha is not None:
                close_items.append(
                    ([f'[{ext_ident} :pinned-commit "{_edn_escape(old_sha)}"]'], orig_ts)
                )
            add_triples.append(f'[{ext_ident} :pinned-commit "{_edn_escape(sha)}"]')
            add_triples.append(f"[{ext_ident} :modified-in {commit_ident}]")
            state.pinned_commit_state[ext_ident] = (sha, commit_ts_iso)
        else:  # "remove"
            orig_ts = state.entity_valid_from.get(ext_ident, commit_ts_iso)
            desc = state.entity_descriptions.get(ext_ident, "")
            close_items.append(
                (_build_close_triples(
                    ext_ident, desc, ext_ident,
                    entity_type_kw=":type/external-dependency",
                    file_value=path,
                    introduced_by=_resolve_introduced_by(db, state, ext_ident),
                ), orig_ts)
            )
            # Submodule removed: purge lifecycle state so a later
            # re-add at the same path is treated as genuinely new.
            # (Submodule paths aren't tracked in state.file_entities, so the
            # path arg is a no-op there, but pass it for consistency.)
            _forget_closed_entity(
                ext_ident, path, state.entity_valid_from,
                state.entity_descriptions, state.field_class_ident, state.file_entities,
                state.field_static_ident, state.entity_introduced_by,
            )
            closed_idents.append(ext_ident)
            old_sha, pin_orig_ts = state.pinned_commit_state.pop(ext_ident, (None, commit_ts_iso))
            if old_sha is not None:
                close_items.append(
                    ([f'[{ext_ident} :pinned-commit "{_edn_escape(old_sha)}"]'], pin_orig_ts)
                )

    # Split :contains triples out before batching.  Minigraf's EAVT
    # pending index lacks value bytes in the key, so batching multiple
    # [module :contains fn] facts in one transact silently drops all
    # but the last.  Each :contains triple gets its own transact so
    # they receive distinct tx_counts and avoid the index collision.
    contains_triples = [t for t in add_triples if ":contains" in t]
    other_triples = [t for t in add_triples if ":contains" not in t]
    _ingest_transact(db, other_triples, commit_ts_iso, reason, index_con)
    for ct in contains_triples:
        _ingest_transact(db, [ct], commit_ts_iso, reason, index_con)
    # :depends-on triples transacted individually — same EAVT collision risk
    # as :contains when multiple deps share the same source module
    for dt in dep_add_triples:
        _ingest_transact(db, [dt], commit_ts_iso, reason, index_con)
    for close_triples, orig_ts in close_items:
        _ingest_close(db, close_triples, orig_ts, commit_ts_iso, reason, index_con)

    # A closed entity must not leave its :type/lineage-marker behind:
    # _lineage_is_provisional is the sole authority for reconcilability
    # (#235), so a stale marker makes a re-introduction at the same ident read
    # as provisional and hands _forward_reconcile_provisional a guess that
    # belongs to the dead entity.
    #
    # Batched once per commit rather than per close site: the batch form
    # issues ONE retract for the whole set (idents with no marker are skipped),
    # where six per-site calls would issue up to six. "confirm" is the
    # existing name for "retract the marker" -- semantically it is a discard
    # here, but delegating to the batch keeps the two from drifting (#233).
    if closed_idents:
        _lineage_confirm_batch(db, closed_idents, index_con=index_con)

    # Ingest :parent edges — one transact per parent to avoid EAVT
    # collision for merge commits (which have two parent hashes).
    # Routed through _transact (not a raw _db_execute call) so the
    # edge also lands in the persisted fact index -- see #118 review
    # finding: this call site used to build its own raw (transact
    # ...) string and bypass the index choke point entirely.
    # lifecycle_only: _reverse_apply already wrote this commit's :parent
    # edges (and its :type/commit entity, skipped at the top of this
    # function) for every commit in frontier-high's territory.
    if not lifecycle_only:
        try:
            for parent_hash in _git_parent_hashes(repo_path, commit_hash):
                parent_ident = f":commit/{parent_hash[:12]}"
                _transact(
                    db,
                    f'[[{commit_ident} :parent {parent_ident}]]',
                    commit_ts_iso,
                    None,
                    None,
                    index_con,
                )
        except Exception:
            pass  # non-fatal; parent edges are best-effort

    # lifecycle_only: Stage B tracks its own progress through
    # :ingestion/correction-sweep-through (written by _correction_sweep_apply),
    # and none of these three watermarks may advance into the reverse region --
    # :ingestion/watermark and :ingestion/lineage-confirmed-through both mean
    # "contiguous from C0", and frontier-low belongs to the forward stream.
    #
    # #342: `persist_claim=False` withholds all three of these together. They
    # are the ONLY three writes in this function that assert something about
    # positions OTHER than this one -- :lo-hash/:hi-hash is a closed RANGE and
    # both watermarks mean "contiguous from C0" -- so each of them, written
    # for a position above one whose write FAILED, silently declares that
    # failed position complete. The work above has already happened; only the
    # claim to have completed it is refused.
    if not lifecycle_only and persist_claim:
        _watermark_update(db, commit_hash, commit_ts_iso, reason, index_con)
        if linearization is not None and pos is not None:
            _frontier_persist_claim(
                db, linearization, pos, from_low=True,
                commit_ts_iso=commit_ts_iso, index_con=index_con,
            )
        # The virgin positions this walk claims are authoritative on first write,
        # so lineage is confirmed contiguously from C0 through here. The sweep
        # folds its own region in later (Task 9); this watermark must not be
        # advanced past the forward frontier before that happens.
        _lineage_confirmed_through_update(db, commit_hash, commit_ts_iso, index_con=index_con)
    # Stage B's lifecycle pass is followed immediately by
    # _correction_sweep_through_update and a checkpoint in _run_ingestion's
    # sweep loop, so checkpointing here would be a pure duplicate -- and it
    # fires BEFORE the watermark advances, so a crash between the two
    # re-processes the commit either way. Measured at 25% of all ingestion
    # checkpoints and 10.3% of wall clock (#241).
    if not lifecycle_only:
        _db_checkpoint_gated(db)
    _commit_index_writer_safe(index_con)


def _forward_candidate_idents(precomputed: Dict[str, Any]) -> List[str]:
    """Every entity ident a parsed file contributes -- module plus all four
    child categories. Mirrors the identical collection kept inline in
    _reverse_apply (its own local `candidate_idents`, moved verbatim out of
    _reverse_fill_claim_and_process by #222 phase 2d Task 6) and in
    _correction_sweep_apply (same name, same construction) -- those two are
    NOT wired to call this helper (considered and declined, #222 phase 2d
    Task 5 review), so all three must be kept in sync by hand."""
    return (
        [precomputed["module_ident"]]
        + [ident for ident, _name, _t in precomputed["function_entries"]]
        + [ident for ident, _name, _t in precomputed["class_entries"]]
        + [ident for ident, _name, _t in precomputed["global_entries"]]
        + [ident for ident, _name, _t in precomputed["field_entries"]]
    )


def _forward_structural_triples_by_ident(precomputed: Dict[str, Any]) -> Dict[str, List[str]]:
    """Every candidate ident's own structural triples, for re-dating, built
    once per file instead of scanned per ident (#222 phase 2d Task 5 fix --
    the previous per-ident linear scan was O(n^2) per file once more than one
    entity needed reconciling). Called by both _forward_apply and
    _correction_sweep_apply, which need the same shape of dict built from
    the same five sources for the same reason (#233).

    A child's own list carries its [parent :contains child] edge, so
    re-dating a child re-dates its containment edge with it (#222 phase
    2b1)."""
    by_ident: Dict[str, List[str]] = {
        precomputed["module_ident"]: list(precomputed["module_candidate_triples"]),
    }
    for entries_key in ("function_entries", "class_entries", "global_entries", "field_entries"):
        for entry_ident, _entry_name, entry_triples in precomputed[entries_key]:
            by_ident[entry_ident] = list(entry_triples)
    return by_ident


_CORRECTION_SWEEP_THROUGH_IDENT = ":ingestion/correction-sweep-through"
# Max per-ident skip lines this sweep writes to stderr per run; see the
# Observability section of the design spec for why this must be
# caller-threaded state (skipped_so_far), not a module-level counter.
_CORRECTION_SWEEP_LOG_CAP = 10


def _correction_sweep_through_query(db: Any) -> Optional[str]:
    """Return the hash of the last commit this sweep has itself confirmed/
    corrected, or None if it has never successfully processed one yet."""
    raw = _db_execute(
        db, f"(query [:find ?h :where [{_CORRECTION_SWEEP_THROUGH_IDENT} :hash ?h]])"
    )
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _correction_sweep_through_update(
    db: Any, commit_hash: str, commit_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Record the last commit this sweep processed. Mirrors
    _lineage_confirmed_through_update's retract-only-if-changed pattern
    exactly, at a different ident with its own :description -- tracks this
    sweep's own progress through frontier-high's territory, independent of
    lineage-confirmed-through's "contiguous from C0" semantics.
    """
    current_raw = _db_execute(
        db, f"(query [:find ?a ?v :where [{_CORRECTION_SWEEP_THROUGH_IDENT} ?a ?v]])"
    )
    current: Dict[str, str] = dict(json.loads(current_raw).get("results", []))

    def _edn(attr: str, value: str) -> str:
        return value if attr == ":entity-type" else f'"{_edn_escape(value)}"'

    constants = {
        ":entity-type": ":type/ingestion",
        ":ident": _CORRECTION_SWEEP_THROUGH_IDENT,
        ":description": "correction sweep progress watermark",
    }

    to_retract: List[str] = []
    to_transact: List[str] = []
    for attr, value in constants.items():
        if current.get(attr) == value:
            continue
        if attr in current:
            to_retract.append(f"[{_CORRECTION_SWEEP_THROUGH_IDENT} {attr} {_edn(attr, current[attr])}]")
        to_transact.append(f"[{_CORRECTION_SWEEP_THROUGH_IDENT} {attr} {_edn(attr, value)}]")

    if ":hash" in current:
        to_retract.append(f"[{_CORRECTION_SWEEP_THROUGH_IDENT} :hash {_edn(':hash', current[':hash'])}]")
    to_transact.append(f"[{_CORRECTION_SWEEP_THROUGH_IDENT} :hash {_edn(':hash', commit_hash)}]")

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)


class _SweepNext(NamedTuple):
    """_correction_sweep_next's answer: the next commit to sweep, or why
    there is none. `reason` is "selected" or one of
    ingest_progress.SWEEP_STATE_FOR_REASON's keys. The positions are filled
    wherever the function got far enough to know them, so a caller can plan
    `to_sweep` (ceiling - start + 1) from the same call that selects."""
    selected: Optional[Tuple[str, str]]
    reason: str
    region_lo: Optional[int] = None
    start_pos: Optional[int] = None
    ceiling_pos: Optional[int] = None


def _correction_sweep_next(
    db: Any,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    hash_to_pos: Optional[Dict[str, int]] = None,
    fragmented: Optional[bool] = None,
) -> _SweepNext:
    """_correction_sweep_select_position's body, returning WHY as well as
    WHAT (#222 phase 4). Every gate, and the order of the gates, is
    unchanged -- see _correction_sweep_select_position's docstring for what
    each one guards."""
    low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
    high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
    if high_bounds is None:
        return _SweepNext(None, "no-frontier-high")  # Stream 2 hasn't claimed anything -- nothing to correct

    if hash_to_pos is None:
        hash_to_pos = {h: i for i, h in enumerate(linearization)}

    if high_bounds[0] not in hash_to_pos:
        return _SweepNext(None, "stale-bound")  # a boundary hash is stale (rewritten history); nothing safe to do

    region_lo = hash_to_pos[high_bounds[0]]

    if low_bounds is None:
        # An ABSENT frontier-low means an EMPTY low region, not an unknown
        # one, so its highest claimed position is -1 -- exactly how
        # FrontierAllocator.gap_lo treats "no interval covers position 0".
        # A fresh graph seeds neither side, and frontier-low is only created
        # once the forward stream persists its first claim, so reading
        # absent as "nothing safe to do" would strand every entity
        # provisional forever whenever Stream 2 claims the whole history
        # before Stream 1 claims anything -- reachable in 2d, where the
        # forward stream does a large preload before its first claim.
        low_hi_pos = -1
    else:
        if low_bounds[1] not in hash_to_pos:
            return _SweepNext(None, "stale-bound", region_lo)  # a boundary hash is stale (rewritten history)
        low_hi_pos = hash_to_pos[low_bounds[1]]

    if low_hi_pos + 1 != region_lo:
        return _SweepNext(None, "gap-open", region_lo)  # gap still open -- Stream 2 may still descend past a position
                                                          # this sweep would otherwise confirm

    if fragmented if fragmented is not None else _intervals_read_extra(db):
        return _SweepNext(None, "fragmented", region_lo)  # #325: a hole remains above frontier-high, so Stream 2 can
                                                            # still descend past a position this sweep would confirm.
                                                            # Once everything coalesces there is exactly one provisional
                                                            # interval and the gap-closed test above is exact again.

    if high_bounds[1] not in hash_to_pos:
        return _SweepNext(None, "stale-bound", region_lo)  # frontier-high's :hi-hash is stale; nothing safe to do
    ceiling_pos = hash_to_pos[high_bounds[1]]

    through_hash = _correction_sweep_through_query(db)
    if through_hash is not None and through_hash in hash_to_pos:
        pos = hash_to_pos[through_hash] + 1
    else:
        # Unset (first-ever call), or a stale hash from rewritten/rebased
        # history -- (re)start from frontier-high's current lo-hash,
        # mirroring _frontier_load's own precedent of dropping a bound
        # that no longer resolves rather than erroring.
        pos = region_lo  # already validated above

    if pos > ceiling_pos:
        return _SweepNext(None, "reached-ceiling", region_lo, pos, ceiling_pos)  # reached frontier-high's own :hi-hash; nothing left to correct

    if len(commit_metadata) != len(linearization) or commit_metadata[pos][0] != linearization[pos]:
        return _SweepNext(None, "metadata-mismatch", region_lo, pos, ceiling_pos)  # commit_metadata violates its stated contract -- nothing safe to
                                                                                     # do, rather than an IndexError or a wrong-commit read

    commit_hash, commit_ts_iso, _author, _subject = commit_metadata[pos]
    return _SweepNext((commit_hash, commit_ts_iso), "selected", region_lo, pos, ceiling_pos)


def _correction_sweep_select_position(
    db: Any,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    hash_to_pos: Optional[Dict[str, int]] = None,
    fragmented: Optional[bool] = None,
) -> Optional[Tuple[str, str]]:
    """Returns (commit_hash, commit_ts_iso) for the next commit this sweep
    should process, upward through frontier-high's own claimed territory,
    or None if there is nothing safe to correct yet: the gap is still open
    (Stream 2 could still descend past a position this call would confirm
    -- see the design spec's "Why confirming requires the gap to already
    be closed"), frontier-high hasn't claimed anything yet, a required
    boundary hash is stale, the provisional side is still fragmented (a
    hole above frontier-high, #325), commit_metadata doesn't match
    linearization, or the sweep has already reached frontier-high's own
    :hi-hash.

    DB-bound, parse-free -- must run off the event-loop thread (per the
    design spec's Execution context) but never on the same executor as
    _extract_commit, since the two must be independently schedulable.

    hash_to_pos, if omitted, is built fresh from linearization -- callers
    doing a full sweep should build it once and pass it in instead to
    avoid rebuilding an N-entry map on every one of a full sweep's N calls.

    fragmented, if omitted, is computed fresh via _intervals_read_extra(db)
    -- two datalog queries. #325 review Finding 3: the Stage B driving loop
    in _run_ingestion calls this function once per swept position, and
    fragmentation cannot change within that loop (Stage B only starts once
    completed_all is True, meaning the whole gap is claimed and the walk
    that mints/merges interval entities has already finished for this run;
    nothing in the Stage B loop body writes an interval fact), so that
    driver computes it ONCE before the loop and passes it in on every call,
    the same hash_to_pos idiom -- turning O(N) extra queries per sweep into
    O(1). A caller that omits it (every direct test call, and any future
    caller that cannot prove the no-mid-loop-mutation invariant for its own
    situation) gets the always-correct, always-fresh read.
    """
    return _correction_sweep_next(
        db, linearization, commit_metadata, hash_to_pos, fragmented,
    ).selected


def _correction_sweep_apply(
    db: Any,
    commit_hash: str,
    commit_ts_iso: str,
    file_results: List[tuple],
    index_con: Optional[Any] = None,
    skipped_so_far: int = 0,
    update_watermark: bool = True,
    pos_by_commit_ident: Optional[Dict[str, int]] = None,
) -> int:
    """Reconciles every candidate entity file_results describes for
    commit_hash, then records progress via _correction_sweep_through_update
    and checkpoints. Returns skipped_events -- how many candidate idents
    landed in the fail-safe skip (provisional with an ambiguous/wrong
    guess, or already-authoritative with an ambiguous introduced-by count),
    i.e. stayed provisional or unreconciled despite this call visiting
    their commit.

    Never calls _extract_commit itself (that's the caller's job, on a
    different executor -- see the design spec's Execution context) and
    never writes commit_hash's own :type/commit entity (2b already wrote
    it for every commit in this sweep's range). DB-bound, parse-free.

    The one entity it WRITES rather than reconciles is a rebirth (#349): a
    candidate with no :introduced-by and no live :ident was closed earlier in
    this same sweep by the lifecycle pass and reappears here, so it gets the
    full introduction a forward walk would write at this commit, and the
    reverse stream's retroactive :modified-in at this commit is retracted.
    See the branch's own comment for why nothing else in Stage B writes it.

    Re-dates each confirmed (case 1) entity's structural facts to the
    introduction commit (#233), which _reverse_apply used to do eagerly on
    every provisional move. Sound here and only here: the gap-closed
    precondition means Stream 2's guess is final, so an entity reaching
    case 1 is at its introduction, and this pass visits each commit exactly
    once ascending. An entity case 2 leaves provisional (ambiguous or wrong
    guess, already logged as skipped) is NOT re-dated and keeps its
    stale-dated structural facts -- the valid-time window _reverse_apply's
    docstring describes closes only for entities this sweep confirms, not
    for ones it skips.

    skipped_so_far is the driving loop's running total of skipped_events
    from every previous call this run -- it exists solely to make the
    stderr log cap (_CORRECTION_SWEEP_LOG_CAP) work across calls without
    this function holding any state of its own. Deriving the budget from a
    caller-supplied running total, rather than a module-level counter, is
    what makes the cap reset per run automatically: this server is
    long-lived and runs many ingests, and a module counter would burn its
    budget on the first one and log nothing ever after. The RETURNED count
    is never capped; only what reaches stderr is.

    Never calls _frontier_persist_claim -- frontier-low is not touched by
    this sweep.

    pos_by_commit_ident (#235) enables the repair of graphs that already
    carry two live :introduced-by facts for one entity -- corruption an
    earlier version of _forward_apply created and which nothing converges,
    since every later reader sees one value, retracts it, and asserts a
    fresh one. When supplied, an ident holding 2+ values is collapsed to
    the one at the LOWEST linearization position before the case 1/2/3
    branching below runs; the survivor is then handled exactly as a
    single-valued entity would be. Repair collapses multiplicity only, it
    never confirms.

    Earliest-by-position is the right survivor because the reverse stream's
    guess is a sighting at or above the true introduction --
    _entity_introduced_by_set_provisional_batch's monotonicity rule already
    encodes that direction -- while the spurious second mint landed at the
    true introduction itself.

    Positions are required rather than derivable from arrival order: the
    second mint happens in the FORWARD region, which this sweep never
    visits, so it meets these entities at commits that are neither of their
    two values. A value absent from the map sorts last, so an unrecognised
    commit ident can never win and can never raise. Left None or empty (the
    default) the repair is inert and every caller keeps the pre-#235
    fail-safe -- empty is gated out too, since with no positions to compare
    the "absent sorts last" rule degenerates to picking the
    lexicographically smallest ident.

    Reach, stated plainly: repair only touches entities this sweep visits,
    and on an already-COMPLETED graph it visits none. A finished run leaves
    :ingestion/correction-sweep-through at frontier-high's :hi-hash, so the
    next run's _correction_sweep_select_position hits its pos > ceiling_pos
    guard, returns None on its first call, and this function is never
    invoked -- re-running ingestion on a corrupted graph repairs nothing.
    Recovery requires new commits above the old ceiling (and then only for
    entities that are candidates in those commits), a resumed run whose
    watermark is still short of the ceiling, or an explicit repair pass /
    watermark reset.

    update_watermark=False suppresses BOTH the trailing
    _correction_sweep_through_update and the _db_checkpoint that follows it,
    handing both back to the caller. Only a caller that does MORE per-commit
    work after this returns should pass False -- 2d's Stage B, which follows
    every call with _forward_apply(..., lifecycle_only=True) to write the
    D/R/gitlink facts the reverse stream skipped. If the watermark advanced
    here, a failure in that lifecycle pass would leave
    :ingestion/correction-sweep-through naming a commit whose deletions,
    renames and gitlink changes were never written, and the next run's
    _correction_sweep_select_position would resume at through + 1 and skip
    it permanently. Deferring the watermark (and the checkpoint that
    durably records it) to after the lifecycle pass makes the whole
    per-commit unit atomic with respect to resume, and keeps the
    one-checkpoint-per-commit cadence intact. The default True preserves the
    2c behaviour for every caller that does nothing after this returns.
    """
    commit_ident = f":commit/{commit_hash[:12]}"
    skipped_events = 0

    for status, _file_path, _extracted, precomputed, _old_path in file_results:
        if status not in ("A", "M"):
            continue  # "D"/"R" deferred -- matches 2b's own scope cut
        candidate_idents = (
            [precomputed["module_ident"]]
            + [ident for ident, _name, _t in precomputed["function_entries"]]
            + [ident for ident, _name, _t in precomputed["class_entries"]]
            + [ident for ident, _name, _t in precomputed["global_entries"]]
            + [ident for ident, _name, _t in precomputed["field_entries"]]
        )
        unchanged_idents = precomputed.get("unchanged_idents", set())

        # #233: the sweep re-dates structural facts, which _reverse_apply
        # used to do eagerly on every provisional move (48% of Stage A's
        # wall time -- 17,250 retracts over 12 real commits). This pass
        # already visits every commit in the reverse region exactly once,
        # ascending, and its gap-closed precondition means Stream 2's guess
        # is final -- so the commit at which an entity reaches case 1 IS its
        # introduction, and re-dating there is once per entity for the whole
        # region. precomputed already carried these triples; only the idents
        # were being read out of it.
        candidate_triples_by_ident = _forward_structural_triples_by_ident(precomputed)

        # #233: _lineage_confirm was one retract per confirmed entity here,
        # and retracts cost ~13ms against a transact's ~1ms. Collected per
        # file and flushed once at the bottom of this same iteration, so a
        # file's confirms are one call.
        to_confirm: List[str] = []
        # Deduplicated: the collection mirrors _forward_candidate_idents'
        # construction, which can repeat an ident when a file declares the
        # same name twice within one category -- a module-level constant
        # reassigned, a function redefined, or a `self.` attribute reassigned
        # a second time in __init__ (cross-category collision is impossible:
        # _canonical_ident bakes entity_type into the ident prefix). The
        # per-ident work below is idempotent, so a repeat was harmless -- but
        # it paid for a full _entity_introduced_by_values_query per
        # duplicate. dict.fromkeys, not set(): both dedupe, but set()'s
        # hash-randomised iteration over strings would make the per-ident
        # stderr skip-log order -- and so which idents hit
        # _CORRECTION_SWEEP_LOG_CAP first -- vary between process runs;
        # dict.fromkeys preserves first-seen order deterministically.
        for ident in dict.fromkeys(candidate_idents):
            introduced_by_values = set(_entity_introduced_by_values_query(db, ident))

            # #235 repair: collapse a corrupted multi-valued entity BEFORE the
            # case branching below, so cases 1/2/3 always see a well-formed
            # entity and need no multi-value handling of their own.
            # Truthiness, not `is not None`: an EMPTY map scores every value
            # with the same len(map)==0 default, collapsing to "smallest
            # ident wins" -- which contradicts "a value absent from the map
            # sorts last" above. Unreachable in production (a run with no
            # commits has nothing to sweep), gated so code and docstring agree.
            if pos_by_commit_ident and len(introduced_by_values) > 1:
                survivor = min(
                    sorted(introduced_by_values),
                    key=lambda c: pos_by_commit_ident.get(c, len(pos_by_commit_ident)),
                )
                doomed = sorted(introduced_by_values - {survivor})
                _retract(
                    db,
                    "[" + " ".join(f"[{ident} :introduced-by {c}]" for c in doomed) + "]",
                    index_con=index_con,
                )
                print(
                    f"[_correction_sweep] repaired {ident}: kept {survivor}, "
                    f"retracted {doomed} (#235)",
                    file=sys.stderr,
                )
                introduced_by_values = {survivor}

            # #349 REBIRTH: no :introduced-by AND no live :ident means this
            # sweep's own lifecycle pass closed the entity at an EARLIER
            # commit of the region (_forward_apply(lifecycle_only=True)
            # retracts both, #231), and the entity reappears here. The
            # reverse stream never saw the two incarnations as distinct: it
            # skips "D" files and the removing commit's diff simply lacks
            # the entity, so it wrote ONE entity, moved its guess down to
            # the first birth, and left a retroactive [ident :modified-in
            # <this commit>] behind at the rebirth. Nothing else writes a
            # birth in Stage B -- the lifecycle pass discards its A/M
            # output -- so without this branch the entity fell into case
            # 3's ambiguous-zero skip and stayed closed at HEAD on a run
            # reporting complete, betrayed only by an ordinary skip line
            # that no at-scale gate fails on.
            #
            # Write exactly what a forward walk writes at an introduction:
            # the entity's full candidate triples, :introduced-by included,
            # at this commit's timestamp -- and retract the retroactive
            # :modified-in, since forward never asserts :modified-in at an
            # entity's own introduction. The lifecycle pass that follows
            # records the same introduction in its walk state (the close
            # purged the ident, so _build_code_triples reads it as new),
            # which is what lets a LATER removal close this incarnation.
            #
            # Gated on BOTH halves, and evaluated only on the zero-values
            # path, so the common case pays no extra query. Liveness is the
            # discriminator from #313's torn entity: live with zero
            # :introduced-by is an interrupted write, not a rebirth, and
            # keeps the fail-safe skip below.
            if not introduced_by_values and not _entity_ident_is_live(db, ident):
                rebirth = candidate_triples_by_ident[ident]
                contains = [t for t in rebirth if ":contains" in t]
                other = [t for t in rebirth if ":contains" not in t]
                _transact(db, "[" + " ".join(other) + "]", commit_ts_iso, index_con=index_con)
                for triple in contains:  # one per call: minigraf#287
                    _transact(db, "[" + triple + "]", commit_ts_iso, index_con=index_con)
                raw_mod = _db_execute(
                    db, f"(query [:find ?c :where [{ident} :modified-in ?c]])"
                )
                if [commit_ident] in json.loads(raw_mod).get("results", []):
                    _retract(db, f"[[{ident} :modified-in {commit_ident}]]", index_con=index_con)
                continue

            if _lineage_is_provisional(db, ident):
                if introduced_by_values == {commit_ident}:
                    # Case 1: the provisional guess matches this commit --
                    # confirm. The :introduced-by fact itself is untouched,
                    # since its value was already correct.
                    _re_date_structural_facts(
                        db,
                        [t for t in candidate_triples_by_ident[ident]
                         if ":introduced-by" not in t],
                        commit_ts_iso,
                        index_con=index_con,
                    )
                    to_confirm.append(ident)
                else:
                    # Case 2: guess points elsewhere, or an ambiguous
                    # (zero/2+) value count -- fail safe, leave untouched.
                    skipped_events += 1
                    if skipped_so_far + skipped_events <= _CORRECTION_SWEEP_LOG_CAP:
                        print(
                            f"[_correction_sweep] {ident} left provisional at {commit_hash} "
                            f"(introduced-by values: {sorted(introduced_by_values)})",
                            file=sys.stderr,
                        )
            else:
                # Case 3: already authoritative.
                if len(introduced_by_values) == 1:
                    (only_value,) = introduced_by_values
                    if only_value == commit_ident:
                        continue  # self-introduction guard: no self-:modified-in
                    raw2 = _db_execute(db, f"(query [:find ?c :where [{ident} :modified-in ?c]])")
                    modified_in_values = {row[0] for row in json.loads(raw2).get("results", [])}
                    already_has_modified_in = commit_ident in modified_in_values
                    if ident in unchanged_idents:
                        if already_has_modified_in:
                            _retract(db, f"[[{ident} :modified-in {commit_ident}]]", index_con=index_con)
                    else:
                        if not already_has_modified_in:
                            _transact(
                                db, f"[[{ident} :modified-in {commit_ident}]]", commit_ts_iso, index_con=index_con,
                            )
                else:
                    # Zero or 2+ distinct values -- same duplicate-fact
                    # risk as case 2 -- skip, left alone rather than
                    # guessed at.
                    skipped_events += 1
                    if skipped_so_far + skipped_events <= _CORRECTION_SWEEP_LOG_CAP:
                        print(
                            f"[_correction_sweep] {ident} left unreconciled at {commit_hash} "
                            f"(ambiguous introduced-by values: {sorted(introduced_by_values)})",
                            file=sys.stderr,
                        )

        _lineage_confirm_batch(db, to_confirm, index_con=index_con)

    if update_watermark:
        _correction_sweep_through_update(db, commit_hash, commit_ts_iso, index_con=index_con)
        _db_checkpoint_gated(db)
    return skipped_events


def _correction_sweep_log_summary(skipped_events: int) -> None:
    """Emit the one-line end-of-sweep summary to stderr if skipped_events
    is nonzero; no-op otherwise. A named function rather than an inline
    print in each loop precisely because there are two loops that must say
    the same thing -- _correction_sweep_walk (Task 5) and 2d's own -- and
    an operator grepping for this line should not have to know which drove
    the sweep.
    """
    if skipped_events:
        print(
            f"[_correction_sweep] {skipped_events} entities left provisional/unreconciled this run",
            file=sys.stderr,
        )


def _correction_sweep_claim_and_process(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
    hash_to_pos: Optional[Dict[str, int]] = None,
    skipped_so_far: int = 0,
    pos_by_commit_ident: Optional[Dict[str, int]] = None,
) -> Optional[Tuple[str, int]]:
    """NOT REACHABLE FROM _run_ingestion, AND MUST NOT BECOME SO. This persists
    its claim unconditionally and has no floor/ceiling concept, so a write
    that raises is swallowed by the interval's closed-range semantics --
    #326 Critical 3, which is permanent silent commit loss that every
    at-scale detector reads clean. Pinned by
    test_run_ingestion_does_not_call_the_legacy_walk_wrappers.

    Synchronous convenience wrapper composing
    _correction_sweep_select_position, _extract_commit, and
    _correction_sweep_apply in order -- for tests and any caller that
    doesn't need them on separate executors. **2d must not call this
    directly** from async code: it fuses the CPU-bound parse and the
    DB-bound writes back into one function body, which can only be
    scheduled onto one executor as a unit. 2d's real loop should await
    each of the three pieces on its own executor instead (see the design
    spec's Execution context).

    Returns (commit_hash, skipped_events), or None if
    _correction_sweep_select_position found nothing safe to do.
    skipped_so_far is forwarded to _correction_sweep_apply unchanged (see
    its docstring for why it exists).

    pos_by_commit_ident (#235) is likewise forwarded unchanged: it enables
    _correction_sweep_apply's two-value :introduced-by repair, which is
    inert without it. Optional here only because the repair is optional --
    but every driving loop should supply it, built exactly as _reverse_apply
    and _correction_sweep_walk build it,
    {f":commit/{h[:12]}": i for i, (h, ...) in enumerate(commit_metadata)},
    which is positionally aligned with linearization by that argument's own
    contract.
    """
    selected = _correction_sweep_select_position(db, linearization, commit_metadata, hash_to_pos)
    if selected is None:
        return None
    commit_hash, commit_ts_iso = selected
    file_results, _gitlink_changes, _gitmodules_map, _renamed_pairs = _extract_commit(
        repo_path, commit_hash, ignore_patterns
    )
    skipped_events = _correction_sweep_apply(
        db, commit_hash, commit_ts_iso, file_results,
        index_con=index_con, skipped_so_far=skipped_so_far,
        pos_by_commit_ident=pos_by_commit_ident,
    )
    return commit_hash, skipped_events


def _correction_sweep_walk(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> Tuple[int, int]:
    """NOT REACHABLE FROM _run_ingestion, AND MUST NOT BECOME SO. This persists
    its claim unconditionally and has no floor/ceiling concept, so a write
    that raises is swallowed by the interval's closed-range semantics --
    #326 Critical 3, which is permanent silent commit loss that every
    at-scale detector reads clean. Pinned by
    test_run_ingestion_does_not_call_the_legacy_walk_wrappers.

    Build hash_to_pos and pos_by_commit_ident once and repeatedly call
    _correction_sweep_claim_and_process (passing them down, along with the
    running skipped-events total as skipped_so_far) until that returns
    None, then call _correction_sweep_log_summary with the final total.

    pos_by_commit_ident (#235) is what arms _correction_sweep_apply's
    two-value :introduced-by repair; without it this walk would visit a
    corrupt entity and merely log it as ambiguous. Built with the same
    expression _reverse_apply uses, off commit_metadata, which
    _correction_sweep_claim_and_process's own callees require to be
    positionally aligned with linearization.

    Returns (commits_processed, skipped_events) -- summed across every
    call, both 0 when the gap-closed precondition isn't met yet (the
    common case early in a run) and when the sweep has already fully
    caught up to frontier-high's :hi-hash.

    Also a synchronous convenience wrapper, same caveat as
    _correction_sweep_claim_and_process: 2d should drive the three-step
    pipeline directly in its own loop, not call this -- but 2d's loop owes
    the same two things this one does: threading skipped_so_far through
    every _correction_sweep_apply call, and calling
    _correction_sweep_log_summary when its own loop ends.
    """
    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    pos_by_commit_ident = {f":commit/{h[:12]}": i for i, (h, _t, _a, _s) in enumerate(commit_metadata)}
    commits_processed = 0
    skipped_events = 0
    while True:
        result = _correction_sweep_claim_and_process(
            db, repo_path, linearization, commit_metadata,
            ignore_patterns=ignore_patterns, index_con=index_con,
            hash_to_pos=hash_to_pos, skipped_so_far=skipped_events,
            pos_by_commit_ident=pos_by_commit_ident,
        )
        if result is None:
            break
        _commit_hash, call_skipped = result
        commits_processed += 1
        skipped_events += call_skipped
    _correction_sweep_log_summary(skipped_events)
    return commits_processed, skipped_events


def _should_fold_lineage_watermark(db: Any, linearization: List[str]) -> bool:
    """True iff the correction sweep genuinely reached frontier-high's own
    :hi-hash, so :ingestion/lineage-confirmed-through may be folded forward
    to it.

    Stage B's loop exit alone must NOT trigger the fold.
    _correction_sweep_select_position returns None for seven different
    reasons, only one of which is "reached the ceiling": it also returns
    None when frontier-high is absent, when either boundary hash is stale
    (rewritten history), when the gap is still open, when the provisional
    side is still fragmented -- a hole above frontier-high (#325) -- and
    when commit_metadata violates its contract. Folding on any None would
    report lineage as confirmed through HEAD in exactly the situations
    where the sweep did no work at all.
    """
    high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
    if high_bounds is None:
        return False
    if _intervals_read_extra(db):
        # #325: a hole remains above frontier-high, so the sweep itself
        # would have declined (see _correction_sweep_select_position) even
        # if through_hash already equals high_bounds[1] from before the
        # hole opened up -- folding here would still misreport lineage as
        # confirmed through a ceiling Stream 2 can still descend past.
        return False
    through_hash = _correction_sweep_through_query(db)
    if through_hash is None:
        return False
    return through_hash == high_bounds[1] and through_hash in set(linearization)


_DEFAULT_STREAM_RATIO = (1, 1)


def _parse_stream_ratio(raw: Optional[str]) -> Tuple[int, int]:
    """Parse MINIGRAF_INGEST_STREAM_RATIO ("F:R") into (forward_per_round,
    reverse_per_round), falling back to 1:1 on anything malformed.

    Never raises. This is read inside the background ingestion coroutine,
    where a bad env var must degrade to the default rather than become the
    reason a repository never ingests at all.

    The ratio trades total work against how fast recent history becomes
    usable: a commit the forward stream claims is parsed once and is
    authoritative immediately, while a commit the reverse stream claims is
    parsed twice (reverse walk, then the correction sweep's own
    _extract_commit call). Total parse cost is N * (1 + reverse_fraction).
    """
    if raw is None:
        return _DEFAULT_STREAM_RATIO
    try:
        forward_str, reverse_str = raw.split(":")
        forward, reverse = int(forward_str.strip()), int(reverse_str.strip())
        if forward < 1 or reverse < 1:
            raise ValueError("both sides must be >= 1")
        return forward, reverse
    except Exception as e:
        print(
            f"[_parse_stream_ratio] ignoring malformed MINIGRAF_INGEST_STREAM_RATIO "
            f"{raw!r} ({e}); using {_DEFAULT_STREAM_RATIO[0]}:{_DEFAULT_STREAM_RATIO[1]}",
            file=sys.stderr,
        )
        return _DEFAULT_STREAM_RATIO


class _RoundRobinClaimer:
    """#222 phase 2d: hands out positions from the shared gap, alternating
    forward and reverse by a fixed ratio.

    This IS the fairness mechanism. Because claims are handed out in one
    deterministic sequence rather than raced between two tasks, starvation
    is not expressible and the interleave is directly assertable in a test.
    """

    def __init__(
        self,
        allocator: "frontier_registry.FrontierAllocator",
        forward_per_round: int,
        reverse_per_round: int,
    ):
        self._allocator = allocator
        self._forward_per_round = forward_per_round
        self._reverse_per_round = reverse_per_round
        self._taken_in_phase = 0
        self._forward_phase = True

    def next_claim(self) -> Optional[Tuple[str, int]]:
        """('fwd', pos) or ('rev', pos), or None once the gap is empty.

        #325 review round 2: claim_low() and claim_high() no longer return
        None on exactly the same condition -- that invariant held only
        because claim_low() used to serve the lowest unclaimed position
        unconditionally. It now refuses a hole that is not adjacent to the
        authoritative interval's own edge (the forward stream's contiguous-
        from-C0 contract; see claim_low()'s own docstring), which can be
        true while claim_high() can still serve the topmost hole -- e.g. the
        bulk gap between the two sides has already closed and only a fresh
        tip gap remains, not touching the authoritative interval at all.

        So a None from the CURRENT phase's own stream is no longer proof the
        shared gap is empty; only is_gap_empty() is. When it is forward's
        turn and claim_low() has nothing legitimate to do, this hands the
        turn to reverse instead of reporting no claim at all -- the forward
        stream stays starved (by design) until the provisional region folds
        into the authoritative one, but the reverse stream must keep making
        progress on whatever remains.

        claim_high() itself is unchanged and still returns None on exactly
        is_gap_empty(), so no symmetric fallback is needed on that side.

        #325 review round 3 (Finding 5): the fallback used to keep
        `_taken_in_phase` as accumulated under FORWARD's budget but then
        compare it against REVERSE's limit for the very same call -- a
        forward phase already `forward_per_round - 1` claims in would
        immediately trip `_taken_in_phase >= reverse_per_round` on the
        fallback claim (since `_taken_in_phase` was never reset), flip back
        to "forward" one call later on a fresh reverse-phase entry, refuse
        again, and repeat -- serving roughly `2 x reverse_per_round`
        reverse claims per nominal cycle instead of `reverse_per_round`,
        silently detaching `MINIGRAF_INGEST_STREAM_RATIO` from what it
        configures the moment forward starves. Falling through now ENDS the
        forward phase outright (resets `_taken_in_phase` to 0 and flips
        `_forward_phase` to False) before charging the fallback claim
        against reverse's own, fresh budget -- so a starved forward phase
        costs nothing but the one free peek, and reverse gets exactly
        `reverse_per_round` claims per cycle, same as an ordinary rotation.
        """
        if self._allocator.is_gap_empty():
            return None
        if self._forward_phase:
            pos = self._allocator.claim_low()
            if pos is not None:
                self._taken_in_phase += 1
                if self._taken_in_phase >= self._forward_per_round:
                    self._taken_in_phase = 0
                    self._forward_phase = False
                return "fwd", pos
            # Forward has nothing legitimate to do this turn. The phase
            # ends HERE, not after forward_per_round more attempts that
            # would all refuse identically -- so the reverse phase that
            # follows starts its own count from zero, never inheriting
            # whatever forward had already accumulated.
            self._taken_in_phase = 0
            self._forward_phase = False

        pos = self._allocator.claim_high()
        if pos is None:
            return None
        self._taken_in_phase += 1
        if self._taken_in_phase >= self._reverse_per_round:
            self._taken_in_phase = 0
            self._forward_phase = True
        return "rev", pos


def _interval_persist_ident(
    interval: "frontier_registry.Interval", linearization: List[str]
) -> str:
    """The graph ident `interval` actually persists to (or would, if newly
    minted this run).

    #325 review round 3 (Finding 3): prefers `interval.ident` -- the REAL
    on-disk identity _load_one_interval carried onto a LOADED interval --
    over re-deriving one from `anchor_pos`. Re-derivation is only safe for
    an interval created FRESH during this run, whose anchor_pos is its own
    creation position in THIS linearization and therefore always resolves;
    for a loaded extra whose original anchor hash has since dropped out of
    the linearization, anchor_pos falls back to hi_pos for gap math only
    (_load_one_interval), and re-deriving _interval_ident from THAT
    fallback would mint a DIFFERENT ident than the one on disk -- creating a
    second live entity for the same region the moment a claim persists
    through it instead of the original.

    `interval.ident` is None for every interval this run creates itself
    (frontier_registry never sets it, only carries a caller-set one through
    _extend/_coalesce), so the fallback below is exactly the pre-existing
    behaviour for that case: the fixed :ingestion/frontier-high ident for a
    base, or a freshly minted ident from the interval's own (always
    resolvable) anchor_pos otherwise.
    """
    if interval.ident is not None:
        return interval.ident
    if interval.is_base:
        return _FRONTIER_HIGH_IDENT
    return _interval_ident(linearization[interval.anchor_pos])


def _reverse_claim_persist_target(
    allocator: "frontier_registry.FrontierAllocator", linearization: List[str]
) -> Tuple[Optional[str], List[str]]:
    """The (ident, absorbed_idents) a reverse claim's _frontier_persist_claim
    call needs, read from `allocator.last_claim` -- the ClaimResult the
    claim that produced this position just left behind.

    #325 review round 2 (Task 5's ident-wiring, applied narrowly here: this
    function and its one call site are the minimum needed to stop a reverse
    claim above a RETAINED interval from corrupting :ingestion/frontier-high;
    the per-interval reverse floor and end-of-walk flush generalization
    Task 5's brief also describes are untouched).

    Every absorbed interval gets the same ident resolution
    (_interval_persist_ident) so _frontier_persist_claim can retract its
    entity: the coalesce survivor rule (frontier_registry._coalesce) never
    lets the base be absorbed, so an absorbed interval here is always
    itself a non-base ident.

    MUST be called immediately after the claim that produced `pos` -- a
    later claim overwrites `allocator.last_claim`, and reading it after the
    fact would attribute the wrong interval's ident to this position.
    """
    claim = allocator.last_claim
    if claim is None:
        return None, []
    ident = _interval_persist_ident(claim.interval, linearization)
    absorbed_idents = [
        _interval_persist_ident(iv, linearization) for iv in claim.absorbed
    ]
    return ident, absorbed_idents


@dataclass
class _ForwardWalkState:
    """The forward walk's twelve mutable preload dicts, threaded as one object.

    Held by reference, not by value: _forward_apply mutates these in place
    exactly as the inline loop body it was extracted from did (issue #222
    phase 2d).
    """

    entity_valid_from: Dict[str, str]
    entity_descriptions: Dict[str, str]
    file_entities: Dict[str, list]
    file_deps: Dict[str, set]
    dep_valid_from: Dict[tuple, str]
    pinned_commit_state: Dict[str, tuple]
    field_class_ident: Dict[str, str]
    field_static_ident: Dict[str, bool]
    submodule_paths: Dict[str, str]
    unresolved_dep_idents: Dict[str, str]
    # No provisional-ident set here on purpose (#235) -- see _forward_apply's
    # reconciliation block. Lineage provisionality is a DB question
    # (_lineage_is_provisional), asked per ident at the moment it matters;
    # a run-start snapshot of it is wrong on a fresh ingest in both
    # directions and must not be reintroduced as a prefilter.
    ts_by_commit_ident: Dict[str, str] = field(default_factory=dict)
    # #231/#238: ident -> the :commit/... ident that introduced it. Mirrors
    # entity_valid_from (which holds the same introduction's TIMESTAMP) and is
    # maintained at exactly the same points: set at _build_code_triples'
    # introduction branches, popped by _forget_closed_entity, seeded by
    # _preload_known_entities. Close sites need it as the retract VALUE for
    # [ident :introduced-by commit] -- a retract needs the exact value, and
    # entity_valid_from only carries the timestamp.
    #
    # Entities the REVERSE walk introduced during this run are absent here:
    # they were never written through the forward state. Close sites must
    # therefore go through _resolve_introduced_by, which falls back to a DB
    # read, or #231 survives for exactly those entities when Stage B's
    # lifecycle pass closes them.
    entity_introduced_by: Dict[str, str] = field(default_factory=dict)


def _build_run_progress(
    linearization: List[str],
    allocator: "frontier_registry.FrontierAllocator",
    lct_hash: Optional[str],
    sweep_through_hash: Optional[str],
) -> "ingest_progress.RunProgress":
    """#222 phase 4: the run's progress model, from the frontier AS LOADED.

    `to_retire` is the allocator's gap at load -- every position this run
    will claim, each retired at most once. The two watermarks are mapped
    into this run's position space: an unset lineage watermark is -1
    (nothing confirmed), one whose hash no longer resolves is None (reported
    as null, never guessed). The sweep watermark only counts when it falls
    inside the base provisional interval, the region the sweep confirms.
    """
    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    lineage_pos: Optional[int] = -1 if lct_hash is None else hash_to_pos.get(lct_hash)
    base = next(
        (iv for iv in allocator.intervals()
         if iv.tag == frontier_registry.TAG_PROVISIONAL and iv.is_base),
        None,
    )
    sweep_lo = sweep_through = None
    if base is not None and sweep_through_hash is not None:
        p = hash_to_pos.get(sweep_through_hash)
        if p is not None and base.lo_pos <= p <= base.hi_pos:
            sweep_lo, sweep_through = base.lo_pos, p
    return ingest_progress.RunProgress(
        linearization, allocator.unclaimed_count(), lineage_pos, sweep_lo, sweep_through,
    )


async def _run_ingestion(repo_path: str, branch: str) -> None:
    """Background coroutine: walk git history and ingest code structure.

    Extraction (git show + tree-sitter parse) for upcoming commits runs
    ahead of time on a process pool (#116) via a bounded sliding-window
    pipeline; all DB-writing bookkeeping below stays strictly sequential,
    one commit at a time, exactly as before this pipeline was introduced —
    the actual db.execute()/checkpoint() calls just run on a dedicated
    single-worker thread executor (write_executor) instead of inline on the
    event-loop thread, so each fsync no longer blocks concurrent
    call_tool() requests. write_executor also runs the extraction process
    pool's own (blocking) shutdown for the same reason — see its
    construction below.

    Note on failure isolation: a worker process crashing outright (OOM
    kill, native segfault in tree-sitter) raises BrokenProcessPool for
    every pending future in the sliding window, not just the commit that
    triggered it — a strictly worse blast radius than the old thread pool,
    where a crash would have taken down this whole server process anyway.
    Ordinary exceptions (bad git ref, unreadable blob, unsupported syntax)
    are unaffected and still fail only the one commit as before.
    """
    global _ingest_progress, _ingest_checkpoint_policy, _ingest_trace
    # Safe to clear unconditionally: handle_minigraf_ingest_git refuses to start a
    # new run while one is already active, so no in-flight shutdown signal is ever
    # stomped on here; main()'s finally block re-sets the flag on exit regardless,
    # so a shutdown request arriving between runs is never silently lost.
    _shutdown_requested.clear()
    # Per-run stderr budget for the two-value :introduced-by warning (#235).
    # Reset HERE, not at module import, because this server is long-lived:
    # see _reset_introduced_by_ambiguity_log_budget.
    _reset_introduced_by_ambiguity_log_budget()
    # #336. None until this run's index cross-check passes, so a run that was
    # refused, or failed before reaching it, never reports a previous run's
    # clean check. Here rather than in the _ingest_progress initializers:
    # tests and the at-scale harness call _run_ingestion with their own dicts.
    _ingest_progress["index_cross_check"] = None
    # #222 phase 4: this run's progress model. None until it is built from
    # the loaded frontier below, for the same reason as index_cross_check: a
    # run refused or failing before that point must never show a previous
    # run's numbers. It lives in the dict, not a module global, so every
    # site that resets _ingest_progress by assignment clears it too.
    _ingest_progress["_run"] = None
    # #222 phase 5 item B, fix round 1: same reasoning as index_cross_check
    # above -- None until this run's own _orphaned_commit_count call below
    # runs, so a run refused or failing before that point never echoes a
    # previous run's orphan count under a run that never computed one.
    _ingest_progress["orphaned_commits"] = None
    # Bound BEFORE the try so the outermost finally can shut it down no matter
    # where a failure lands, including the two awaited calls
    # (_open_index_writer_safe, _frontier_load) that sit above the inner try
    # (#250). Without this the finally would need an unbound-name guard.
    write_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
    # Advertise this long-held claim so another process's #108 pre-check can
    # decline instead of racing us. Entered before the try so the outermost
    # finally always releases it; best-effort, so a hint that cannot be
    # written never blocks the run.
    owner_hint = _graph_owner_hint_held(_graph_path_current(), "ingestion")
    await owner_hint.__aenter__()
    try:
        # Hold checkpointing to a fixed fraction of wall clock rather than
        # once per commit. See _CheckpointPolicy for why the per-commit
        # cadence was super-linear and why deferring is safe (#241).
        #
        # FIRST statement in the try, above every fallible one, and that
        # position is the fix for #270 -- it used to sit ~100 lines below,
        # just after write_executor. Both finally blocks publish
        # summary() into _ingest_progress["checkpoint_summary"] guarded on
        # this being non-None, so a failure in the git enumerations, in
        # _load_ingestion_preload_state (which takes db_lease(extended=True)
        # and raises when the graph file lock never clears) or in the
        # `git rev-list --count` below published NOTHING, and the at-scale
        # benchmark's summary assertion reported `assert None is not None`
        # while the real exception sat unread in _ingest_progress["error"].
        # That gap is why #270 spent 48 sampled runs unable to name a cause.
        #
        # It also fixes summary()'s window. elapsed_seconds runs from this
        # construction to the clearing finally, so with the old position the
        # preload and both git enumerations were OUTSIDE it -- minutes of
        # them on a large graph -- and realised_duty, the acceptance
        # criterion #241 is measured against and the number
        # report.py writes into benchmark.md, was overstated by exactly that
        # ratio. Duty rows recorded before this change are measured over a
        # narrower window and are not comparable with later ones.
        #
        # Safe this high because __init__ touches no DB and no git: it reads
        # one env var and one clock. _checkpoint_duty_from_env() raising on a
        # malformed MINIGRAF_INGEST_CHECKPOINT_DUTY is the one remaining
        # no-summary path, and deliberately so -- that is a deterministic
        # config error, not a transient, and leaving it distinguishable keeps
        # "the policy never existed" from being confused with "the policy
        # existed and never checkpointed", which is the whole content of an
        # all-zeros summary.
        _ingest_checkpoint_policy = _CheckpointPolicy(_checkpoint_duty_from_env())
        # Enumerated BEFORE the preload (#238), which needs the positions to
        # bound its queries. Above the DB open too, not merely above the
        # lease release, so the graph file lock is held for no longer than
        # it was. These are git subprocesses and touch no DB.
        linearization = frontier_registry.build_linearization(repo_path, branch)
        # FULL history, positionally aligned with linearization. Both
        # _reverse_apply and _correction_sweep_select_position index it
        # positionally, and _reverse_apply raises ValueError on a length
        # mismatch -- a watermark-relative list is exactly the wrong thing
        # to hand them.
        commit_metadata = _git_commits(repo_path, None, branch)
        ignore_patterns = _load_ignore_patterns(repo_path)

        # Read watermark and pre-load known entities/deps before releasing DB.
        # Off-loaded to a worker thread (see _load_ingestion_preload_state)
        # so this potentially slow phase never blocks the event loop from
        # servicing the stdio handshake concurrently (issue #103).
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as preload_executor:
            (
                watermark, prior_ingested, entity_valid_from, entity_descriptions,
                entity_introduced_by, file_entities, file_deps, dep_valid_from,
                pinned_commit_state, field_class_ident, field_static_ident,
                submodule_paths, unresolved_dep_idents,
            ) = await loop.run_in_executor(
                preload_executor, _load_ingestion_preload_state,
                repo_path, linearization, commit_metadata,
            )
        state = _ForwardWalkState(
            entity_valid_from=entity_valid_from,
            entity_descriptions=entity_descriptions,
            file_entities=file_entities,
            file_deps=file_deps,
            dep_valid_from=dep_valid_from,
            pinned_commit_state=pinned_commit_state,
            field_class_ident=field_class_ident,
            field_static_ident=field_static_ident,
            submodule_paths=submodule_paths,
            unresolved_dep_idents=unresolved_dep_idents,
            entity_introduced_by=entity_introduced_by,
            # Full-history, so a resumed run's retroactive :modified-in for a
            # guess commit at or below the watermark still finds a timestamp
            # (a post-watermark-only map would silently skip it).
            ts_by_commit_ident={f":commit/{h[:12]}": ts for h, ts, _a, _s in commit_metadata},
        )
        # `branch`, NOT "HEAD" (#317). This was hardcoded to HEAD while every
        # other git call in this function takes the branch argument, so the
        # progress bar's denominator described a different ref than the walk
        # for any run that is not on HEAD -- and silently, since both are
        # plausible integers. The at-scale harness hits exactly that: it
        # resolves the branch via _default_git_branch (e.g. "master") while
        # the checkout sits on a feature branch. Found while building #317's
        # commit census, which compares this same count against the graph and
        # would have inherited the wrong ref.
        repo_total_result = _subprocess.run(
            ["git", "rev-list", "--count", branch],
            cwd=repo_path, capture_output=True, text=True,
        )
        repo_total = int(repo_total_result.stdout.strip()) if repo_total_result.returncode == 0 else len(commit_metadata)
        _ingest_progress["total"] = repo_total
        _ingest_progress["status"] = "running"
        _ingest_progress["phase"] = "converging"
        _ingest_progress["prior_ingested"] = prior_ingested

        env_workers = os.environ.get("MINIGRAF_INGEST_WORKERS")
        # CPU-bound-appropriate default: one worker per core, not the
        # I/O-bound ThreadPoolExecutor heuristic (cpu_count() + 4) this used
        # before #116 — extra worker *processes* beyond the core count only
        # add context-switch overhead for a pool that's actually saturating
        # the CPU (see #116, "needs a process pool"). Still capped at 32:
        # each worker is now a spawned OS process that re-imports this whole
        # module (plus whichever tree-sitter grammars it touches), far
        # pricier per-worker than a thread, so an uncapped cpu_count() on a
        # very high-core host would spawn an excessive number of them.
        max_workers = int(env_workers) if env_workers else min(32, (os.cpu_count() or 1))
        pipeline_depth = max_workers * 2

        completed_all = True
        # Dedicated single-worker pool for every DB write below. Each write is a
        # synchronous, fsync'd call into the Rust-backed MiniGrafDb (see minigraf
        # issue #287 for why facts can't batch across :contains/:depends-on edges
        # into fewer fsyncs). The FFI call releases the GIL for its duration, so
        # running it via run_in_executor lets the event loop keep servicing
        # concurrent call_tool() requests while a write's fsync is in flight,
        # instead of blocking the whole loop for that call. A single worker keeps
        # writes strictly one-at-a-time, matching the existing invariant that only
        # one commit's write section ever holds the leased handle at once. Also reused below
        # (#116) to run the extraction ProcessPoolExecutor's blocking shutdown()
        # off the event-loop thread — that reuse is only safe because this pool
        # isn't shut down itself until the outer `finally` further down, after
        # the extraction pool's own shutdown has already been submitted to it.
        write_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        _ingest_trace = _ingest_trace_from_env()

        # Single fact-index write connection for the whole ingestion run,
        # opened on write_executor's one worker thread and never touched from
        # any other thread thereafter (sqlite3 connections are thread-affine
        # by default) -- every use below is itself routed through
        # write_executor, so all access stays on the thread that created it.
        # Committed once per source-commit, not once per triple: large
        # repositories can cross 1M facts well before ingestion completes,
        # and per-triple commits would be dominated by fsync overhead at
        # that scale. This is a deliberate asymmetry with the graph
        # checkpoint below: the fact-index sqlite commit stays per-commit,
        # while the graph checkpoint is now duty-gated rather than
        # per-commit (#241) -- the two are unrelated costs (sqlite commit
        # vs minigraf WAL compaction) and only the latter was found to be
        # super-linear in graph size.
        index_path = fact_index.index_path_for(_graph_path_current())
        index_con = await loop.run_in_executor(write_executor, _open_index_writer_safe, index_path)

        # #222 phase 2d: the two streams share one gap. _frontier_load WRITES
        # (one-time watermark->interval migration, plus the lineage-marker
        # migration), so it needs a live db and index_con and must run on
        # write_executor rather than inline on the event-loop thread.
        run_ts_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        async with _db_lease_async_committing_index(loop, write_executor, index_con) as db:
            # The run's FIRST write, ahead of _frontier_load's own migrations: a
            # fresh graph that got ingestion state without its stamp would look
            # exactly like a pre-#263 graph on the next run and be refused (#263).
            await loop.run_in_executor(
                write_executor, _graph_format_version_stamp_if_new, db, run_ts_iso, index_con,
            )
            # #222 phase 5 item B. AFTER the format stamp, never before --
            # defensive, not load-bearing: _graph_has_ingestion_state's
            # disjunction is exactly three reads (_watermark_query and
            # _frontier_read_bounds on each fixed frontier), so this fact is
            # invisible to it and the stamp fires correctly either way.
            # Keeping the stamp unambiguously first costs nothing.
            #
            # NEVER add :ingestion/branch to _graph_has_ingestion_state. This
            # is written before any walk, so a run that recorded a branch and
            # then died would afterwards read as "already ingested" --
            # suppressing its own stamp, then being refused by
            # _graph_format_version_verify as a state-present/stamp-absent
            # pre-#263 graph. That condemns a graph holding no ingested data.
            #
            # Read BEFORE the write below overwrites it. _orphaned_commit_count
            # compares the PREVIOUSLY-recorded branch against this run's ref --
            # reading after the write would compare this run's ref against
            # itself, which always matches and silently defeats the guard.
            prior_branch = await loop.run_in_executor(
                write_executor, _ingestion_branch_read, db,
            )
            await loop.run_in_executor(
                write_executor, _ingestion_branch_write, db, branch, run_ts_iso, index_con,
            )
            # #222 phase 5 item B. Computed ONCE here, where the linearization
            # and a lease are both already in hand, and stored as a plain
            # _ingest_progress key -- NOT queried at poll time (phase 4: status
            # is never derived from graph queries at poll time, which contends
            # on _db_native_lock and is staler than memory anyway), and NOT put
            # on RunProgress, which is deliberately pure (no DB, no git,
            # injected clocks).
            #
            # None, never 0, when the previously-recorded branch does not
            # match this run's ref: the graph carries no per-commit branch, so
            # commits from another ingested branch are indistinguishable from
            # a rewrite's leftovers. A 0 there would read as "verified clean".
            _ingest_progress["orphaned_commits"] = await loop.run_in_executor(
                write_executor, _orphaned_commit_count, db, linearization, prior_branch, branch,
            )
            allocator = await loop.run_in_executor(
                write_executor, _frontier_load, db, linearization, run_ts_iso, index_con,
            )
            # #326: archived completion witnesses, mapped into this run's
            # position space. _frontier_load does the ARCHIVING (that is where
            # the doomed bounds are) but not the loading -- widening its return
            # would break a dozen call sites that use it directly as an
            # allocator.
            completed_regions = await loop.run_in_executor(
                write_executor, _completed_regions_load, db, linearization, allocator, index_con,
            )
            lct_hash = await loop.run_in_executor(
                write_executor, _lineage_confirmed_through_query, db,
            )
            sweep_through_hash = await loop.run_in_executor(
                write_executor, _correction_sweep_through_query, db,
            )
        run_progress = _build_run_progress(
            linearization, allocator, lct_hash, sweep_through_hash,
        )
        _ingest_progress["_run"] = run_progress
        claimer = _RoundRobinClaimer(
            allocator, *_parse_stream_ratio(os.environ.get("MINIGRAF_INGEST_STREAM_RATIO"))
        )

        try:
            # Extraction (git show + tree-sitter parse + triple construction) runs
            # in real OS processes, not threads (#116): tree-sitter's C parse holds
            # the GIL for its whole duration (confirmed empirically — a single
            # busy thread stalls a concurrent event loop's asyncio.sleep(0) ticks
            # by tens of ms per tick vs sub-millisecond baseline), so a thread pool
            # here would starve the event loop for as long as a heavy commit takes
            # to parse, exactly the symptom #116 reports. An explicit "spawn"
            # context is used rather than the platform default ("fork" on Linux)
            # because worker processes are created lazily as commits are submitted
            # below, by which point write_executor's thread (and potentially other
            # background threads -- see #122) may already be alive in this
            # process — forking with other threads running risks inheriting a
            # lock one of them held at the instant of fork, which would deadlock
            # forever in the child. spawn starts each worker from a clean
            # interpreter instead, at the one-time cost of re-importing this
            # module per worker process (not per commit).
            mp_context = multiprocessing.get_context("spawn")
            executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=max_workers, mp_context=mp_context
            )
            try:
                pending: Any = deque()

                # #326: the end-of-walk flush's bounds. A run of skips is
                # normally subsumed for free -- _frontier_persist_claim moves
                # :lo-hash, a RANGE bound, so the next genuinely-walked reverse
                # position below the skips persists a bound covering them. These
                # exist for the one case that is not covered: the walk ending
                # while still inside a run of skips.
                # #326 FIX: the flush's hi bound is the highest SKIPPED
                # position, never the highest rev position claimed. The two
                # differ on exactly the case that matters. `highest_rev_pos`
                # was updated for every rev claim BEFORE the skip test, so it
                # includes positions that were claimed, walked, and whose
                # write FAILED -- the per-commit `except` retires the
                # position as failed and persists no claim, and
                # `completed_all` stays True. _frontier_persist_span moves
                # :hi-hash UP (while _frontier_persist_claim never does for
                # the high interval), so passing highest_rev_pos would raise
                # the persisted top bound over those failed positions. Once
                # :hi-hash reaches the tip the interval is REPRESENTABLE, so
                # the next _frontier_load retains it instead of discarding
                # it, and those positions are never re-walked: permanent
                # silent loss. The skipped span is the only thing this flush
                # is entitled to assert; positions above it either persisted
                # their own claim or legitimately did not.
                #
                # #325: keyed by target ident, not a run-global pair. A skipped
                # claim still came out of the allocator, so it still extended
                # (and possibly merged) an in-memory interval, and the flush
                # has to persist that skipped span to the SAME entity the
                # claim belonged to -- a run that skips into more than one
                # provisional interval (a retained one below a fresh tip gap,
                # say) would otherwise fold an unrelated interval's skip span
                # into whichever ident happened to be current when the two
                # scalars were last written.
                skipped_span: Dict[str, Tuple[int, int]] = {}

                # #325 review Finding 4: which idents CONTRIBUTED to
                # skipped_span[ident] -- always at least {ident} itself. A
                # merge fold (below) moves the SPAN onto the survivor but
                # cannot move the FLOOR with it: floors are set at DISPATCH
                # (inside the `while pending:` loop's per-commit `except`
                # blocks, via _note_incomplete_rev), while a merge folds at
                # CLAIM time (inside submit_next(), when the claim is made
                # and appended to `pending`) -- strictly earlier for that
                # same position. So `rev_claim_floor` may not yet hold the
                # absorbed ident's entry at fold time.
                #
                # (An earlier version of this comment claimed dispatch "runs
                # strictly AFTER every claim [...] has already happened" --
                # false as written: `pending` is bounded by pipeline_depth,
                # so a position's dispatch only postdates the claims made
                # while it sat in the queue, not every claim the run will
                # ever make. The property this code actually needs is
                # weaker and does hold: dispatch order equals claim order,
                # since `pending` is FIFO and nothing reorders it, and the
                # reverse stream's claims descend monotonically in position
                # -- so for any two reverse positions, the higher one is
                # both claimed AND dispatched before the lower one. A
                # merging claim at some ident is therefore always dispatched
                # -- and any floor it sets -- before a lower position under
                # the same (post-merge) ident is dispatched, which is the
                # ordering the flush actually relies on.)
                #
                # The flush (at true end-of-walk, after every floor
                # exists) reads this to find every floor relevant to a
                # (possibly folded) skipped span, not just the survivor's
                # own -- see the flush loop below for why a floor found this
                # way still is not enough on its own.
                skipped_span_sources: Dict[str, Set[str]] = {}

                # #326 Finding A: the floor :lo-hash may not cross this run.
                #
                # The witness this whole feature rests on -- "
                # _frontier_persist_claim is the LAST write of a position, so
                # membership in a persisted frontier interval proves that
                # position completed" -- was FALSE as written, and the archive
                # is what promoted an always-imprecise interval into a trusted
                # one. Membership was only ever implied by a NEIGHBOUR's claim:
                # frontier-high is a closed RANGE, so when a position's write
                # fails, the per-commit `except` below logs, counts it, and
                # CONTINUES THE DESCENT -- and the next lower position that
                # succeeds moves :lo-hash beneath the failed one, sweeping it
                # into the interval nothing ever claimed for it. #313's SIGKILL
                # is safe only because the process STOPS there; the `except`
                # path does not.
                #
                # On master that was self-healing by accident: the interval was
                # discarded on tip growth and everything got re-walked. #326
                # archives the interval instead, so the same imprecision became
                # a PERMANENT, SILENT skip of the failed position -- and every
                # at-scale detector reads that graph clean (fact_audit's two
                # witnesses agree about an absence, both :introduced-by checks
                # only examine entities that EXIST, stderr carries only the one
                # skip line from the run that failed, and commit_census runs on
                # a fresh graph).
                #
                # So the fix is to make the interval PRECISE rather than to
                # weaken the predicate: once a reverse position fails to
                # complete, no lower reverse position claims. The reverse
                # stream descends monotonically, so the highest such position
                # is the floor for the rest of the run.
                #
                # This withholds BOOKKEEPING, never WORK. Positions below the
                # floor are still claimed, still parsed and still written in
                # full; they simply do not assert completion, so the next run
                # re-walks them. Do not "optimize" this into skipping them.
                # The cost is one run's re-walk below a transient failure,
                # which is what master effectively did anyway.
                #
                # Both incompleteness paths set it, because they are the same
                # defect: a write that raised (below), and an extraction that
                # raised (which never even reaches _reverse_apply, so it is
                # strictly the weaker case). The forward stream is untouched --
                # it claims frontier-low and its failure semantics are out of
                # scope for #326.
                #
                # A third way a reverse position retires incomplete does NOT
                # set this: the shutdown `break` below (`while pending: if
                # _shutdown_requested.is_set(): ...`), which abandons whatever
                # is still queued in `pending` unclaimed. It needs no floor --
                # nothing lower ever claims after a shutdown, and
                # `completed_all = False` already gates the end-of-walk flush
                # off for this run -- so do not go looking for a third
                # _note_incomplete_rev call to match it.
                #
                # #325: ONE FLOOR PER INTERVAL, not per run. #326 shipped this
                # as a single scalar because a run's reverse stream made one
                # contiguous descent -- correct then. #325's allocator now
                # serves the topmost gap first and falls through to a bulk gap
                # entirely below it (claim_high() + a merge into a retained
                # interval), so a failure in the tip gap's ident must not sit
                # above every position in a disjoint bulk gap's ident: that
                # would withhold the bookkeeping for work the bulk gap's claims
                # actually completed, silently, and the next run would re-walk
                # all of it for no reason. Same guarantee as #326 Finding A,
                # stated over the unit it was always really about -- the
                # interval a claim targets, not the run.
                #
                # #325 review Finding 1 (CRITICAL): a floor keyed on an ident
                # an absorbed interval no longer HAS is no floor at all -- a
                # merge reassigns the floored interval's own claims to the
                # SURVIVOR's ident, so the write-dispatch floor check below
                # must consult every ident a merging claim's `absorbed_idents`
                # names, not only `claim_ident`, or the survivor reads as
                # unrestricted and a mid-gap write failure gets swept into the
                # persisted interval permanently.
                rev_claim_floor: Dict[str, int] = {}

                # #342: the forward mirror. A CEILING, not a floor -- the
                # reverse stream descends, so a failure there blocks every
                # LOWER position from claiming; the forward stream ascends, so
                # a failure here blocks every HIGHER one. Same defect either
                # way: :ingestion/frontier-low is a closed RANGE bound, so a
                # position whose write raised is swept inside it by the next
                # position that succeeds -- membership implied by a
                # NEIGHBOUR's claim, never by its own (#326 Finding A, stated
                # over the other stream).
                #
                # A run-scoped SCALAR, deliberately, where the reverse side
                # needs a dict keyed by target ident. #325 made the reverse
                # floor per-interval because one run's allocator can serve a
                # tip gap and a disjoint bulk gap in the same run, so a
                # failure in one must not withhold bookkeeping for the other.
                # The forward stream has no such split: it claims exactly one
                # interval (:ingestion/frontier-low, the authoritative one),
                # and claim_low() is CONTIGUITY-bound since #325 -- it serves
                # only the hole adjacent to that interval's own edge and
                # returns None for every other hole. One ident, one ascent,
                # one number. Should claim_low() ever be widened to serve a
                # non-adjacent hole, this must become per-ident with it.
                fwd_claim_ceiling: Optional[int] = None

                def _note_incomplete_fwd(claim_tag: str, claim_pos: int) -> None:
                    # min(), not first-wins: the two are identical while the
                    # FIFO pipeline delivers forward positions strictly
                    # ascending (see submit_next's ordering note), and this
                    # way the guarantee does not silently depend on that.
                    nonlocal fwd_claim_ceiling
                    if claim_tag != "fwd":
                        return
                    fwd_claim_ceiling = (
                        claim_pos if fwd_claim_ceiling is None
                        else min(fwd_claim_ceiling, claim_pos)
                    )

                def _note_incomplete_rev(claim_tag: str, claim_pos: int, ident: str) -> None:
                    # #325 review Finding 3 (Minor): `ident` is typed `str`,
                    # never `Optional[str]` -- this is only ever called with
                    # `claim_tag == "rev"` carrying a real claim_ident, since
                    # 'fwd' claims resolve (None, []) in submit_next and this
                    # function returns before touching `ident` for them, and
                    # a 'rev' claim's ident is always set immediately after
                    # claim_high() by _reverse_claim_persist_target. A None
                    # key here would silently poison rev_claim_floor with a
                    # non-str key, which sorted(skipped_span.items()) below
                    # raises TypeError on the moment a real str key also
                    # exists.
                    if claim_tag != "rev":
                        return
                    assert ident is not None, "a 'rev' claim must always resolve a real ident"
                    prev = rev_claim_floor.get(ident)
                    rev_claim_floor[ident] = (
                        claim_pos if prev is None else max(prev, claim_pos)
                    )

                # #222 phase 2d: positions come from the shared-gap claimer,
                # not a plain commit iterator, and each pipeline entry carries
                # the tag of the stream that claimed it. Extraction is stream-
                # agnostic (every position goes to the same process pool); only
                # the write half below dispatches on the tag.
                #
                # The pipeline is FIFO, and that is what preserves the ordering
                # each stream requires: claims are handed out in one
                # deterministic sequence, appended in that sequence and popped
                # in it, so "fwd" positions reach _forward_apply strictly
                # ascending (its state machine's precondition) and "rev"
                # positions reach _reverse_apply strictly descending (its
                # monotonicity and progress guards' precondition).
                # #326: a skippable claim is retired here, BEFORE the parse is
                # queued, so it costs neither the git show + tree-sitter parse
                # nor _reverse_apply's write batch nor the checkpoint nor the
                # per-commit handle drop. The loop body is a pure in-memory
                # interval scan, so a long run of skips costs microseconds per
                # position and never stalls the event loop.
                def submit_next() -> bool:
                    while True:
                        claim = claimer.next_claim()
                        if claim is None:
                            return False
                        tag, pos = claim
                        # #325 review round 2's placement rule applies to a
                        # SKIPPED claim too, not only the one that breaks out
                        # of this loop below: allocator.last_claim is
                        # overwritten by the very next claim, so the ident a
                        # skipped position belongs to must be resolved here,
                        # immediately, or it is lost. A skipped position still
                        # came out of the allocator and still extended (and
                        # possibly merged) an in-memory interval -- the
                        # end-of-walk flush has to persist that skipped span
                        # to the SAME entity the claim belonged to. Only
                        # 'rev' claims need it -- 'fwd' never skips
                        # (_skip_claim), and the authoritative side never
                        # fragments, so _frontier_persist_claim's own default
                        # (:ingestion/frontier-low, nothing absorbed) is
                        # already correct for 'fwd'.
                        target_ident, absorbed = (
                            _reverse_claim_persist_target(allocator, linearization)
                            if tag == "rev" else (None, [])
                        )
                        # #325 review Finding 2: a merge can absorb an
                        # interval that already holds its OWN skipped_span
                        # entry from an earlier claim. This claim's merge (if
                        # it persists) makes _frontier_persist_claim retract
                        # the absorbed entity outright -- a flush that still
                        # named it afterwards would resurrect it, because
                        # _frontier_persist_span's `existing is None` branch
                        # mints a fresh entity unconditionally. Fold at CLAIM
                        # time, here, not at dispatch: the merge itself
                        # happens here (allocator.last_claim), and dispatch
                        # never sees which idents a skipped claim's own merge
                        # touched.
                        for a in absorbed:
                            if a in skipped_span:
                                lo_a, hi_a = skipped_span.pop(a)
                                lo_t, hi_t = skipped_span.get(target_ident, (lo_a, hi_a))
                                skipped_span[target_ident] = (
                                    min(lo_t, lo_a), max(hi_t, hi_a)
                                )
                                # #325 review Finding 4: carry provenance
                                # along with the span, so the flush can find
                                # a floor that was never the survivor's own.
                                # Union rather than overwrite -- transitive
                                # folds (T1 into T2, T2 later into T3) must
                                # accumulate every original source, not just
                                # the most recent one.
                                sources_a = skipped_span_sources.pop(a, {a})
                                sources_t = skipped_span_sources.get(
                                    target_ident, {target_ident}
                                )
                                skipped_span_sources[target_ident] = sources_t | sources_a
                        if not _skip_claim(tag, pos, completed_regions):
                            break
                        lo, hi = skipped_span.get(target_ident, (pos, pos))
                        skipped_span[target_ident] = (min(lo, pos), max(hi, pos))
                        # A skipped position is still RETIRED (RunProgress
                        # "skipped"), which is what #317's commit_census reads
                        # through walk_claimed_from_progress -- excluding it
                        # would redefine walk_claimed.
                        run_progress.retired(tag, "skipped", pos)
                    claim_ident, absorbed_idents = target_ident, absorbed
                    fut = loop.run_in_executor(
                        executor, _extract_commit, repo_path, linearization[pos], ignore_patterns
                    )
                    pending.append((tag, pos, fut, claim_ident, absorbed_idents))
                    return True

                run_progress.stage_a_started()
                for _ in range(pipeline_depth):
                    if not submit_next():
                        break

                while pending:
                    if _shutdown_requested.is_set():
                        completed_all = False
                        break

                    tag, pos, fut, claim_ident, absorbed_idents = pending.popleft()
                    commit_hash, commit_ts_iso, author, subject = commit_metadata[pos]
                    # renamed_pairs (Task 9's 4th _extract_commit return element) is
                    # unpacked here but not yet consumed — Task 10 wires it into
                    # :renamed-from/:renamed-to triple emission for functions/classes.
                    # Widening this unpack now (rather than leaving it a 3-tuple) is
                    # required as soon as _extract_commit returns 4 elements: every
                    # ingestion run — including the many existing tests that drive
                    # _run_ingestion through a real ProcessPoolExecutor worker — would
                    # otherwise fail with "too many values to unpack".
                    _trace_t_await = time.perf_counter()
                    try:
                        extracted_files, gitlink_changes, gitmodules_map, renamed_pairs = await fut
                    except concurrent.futures.process.BrokenProcessPool:
                        # The whole worker pool died (OOM kill, native segfault) --
                        # every other pending future in the sliding window is
                        # equally poisoned, so this is not isolable to one commit
                        # (see this function's docstring). Propagate to the outer
                        # handler as before.
                        raise
                    except Exception as e:
                        # Ordinary extraction failure (bad git ref, unreadable
                        # blob, unsupported syntax) -- isolate it to this one
                        # commit instead of aborting the whole run, matching this
                        # function's own documented "fail only the one commit"
                        # contract and the per-file try/except _extract_commit
                        # already uses one level down for content-fetch failures.
                        print(
                            f"[_run_ingestion] skipping unreadable commit {commit_hash} "
                            f"({subject!r}): {e}",
                            file=sys.stderr,
                        )
                        _note_incomplete_rev(tag, pos, claim_ident)
                        _note_incomplete_fwd(tag, pos)
                        submit_next()
                        _ingest_progress["current_commit"] = commit_hash
                        run_progress.retired(tag, "failed", pos)
                        await asyncio.sleep(0)  # yield to event loop
                        continue
                    # #260 M1: read BEFORE submit_next(), not after -- await_s
                    # is documented as extraction stall (wall clock stalled on
                    # `await fut`), and submit_next() does real work (queues
                    # the next commit's extraction). Reading the clock after it
                    # would fold submission cost into a field the spec defines
                    # as pure stall. Immaterial in magnitude on the shipped
                    # trace (5.10s total over 767 commits), but the field
                    # should mean what it says.
                    _trace_await_s = time.perf_counter() - _trace_t_await
                    submit_next()

                    _ingest_progress["current_commit"] = commit_hash

                    # A lease, not a manual acquire/release pair. The old code
                    # cleared the local before the global because a concurrent
                    # thread calling get_db() inside that window would open a
                    # SECOND handle (#251/#253). There is no window now: the
                    # count is authoritative and the handle drops exactly when
                    # it reaches zero.
                    # #260: apply_s deliberately spans the lease ACQUIRE as
                    # well as the executor call -- the acquire is real serial
                    # per-commit cost, and a reader must not take apply_s for
                    # pure write time. #260 M2: it also spans the lease
                    # RELEASE -- the timer below is read after `async with
                    # db_lease_async()` has exited, and at refcount 0 that
                    # release drops the handle. A follow-up attribution task
                    # narrowing apply_s further needs to know handle open AND
                    # drop are both inside the measured span, not just open.
                    _trace_t_apply = time.perf_counter()
                    _trace_write_ok = True
                    async with _db_lease_async_committing_index(loop, write_executor, index_con) as db:
                        try:
                            if tag == "fwd":
                                await loop.run_in_executor(
                                    write_executor, _forward_apply, db, repo_path, state,
                                    commit_metadata[pos],
                                    (extracted_files, gitlink_changes, gitmodules_map, renamed_pairs),
                                    index_con, linearization, pos,
                                    # lifecycle_only=False, then #342's
                                    # persist_claim. Positional because
                                    # run_in_executor takes no kwargs.
                                    #
                                    # Evaluated at DISPATCH time, not claim
                                    # time, for the same reason the reverse
                                    # check is: claims run ahead of writes by
                                    # pipeline_depth, so the position that
                                    # fails may not have set the ceiling yet
                                    # when a higher position was ALLOCATED in
                                    # submit_next -- only by the time its own
                                    # write is dispatched here.
                                    False,
                                    fwd_claim_ceiling is None or pos < fwd_claim_ceiling,
                                )
                            else:
                                await loop.run_in_executor(
                                    write_executor, _reverse_apply, db, repo_path, linearization,
                                    commit_metadata, pos, extracted_files, index_con,
                                    # #326 Finding A / #325: below THIS
                                    # ident's floor we do the work but
                                    # withhold the claim. Keyed by
                                    # claim_ident, not a run-global scalar --
                                    # a floor set by a failure in one
                                    # interval must not block a claim
                                    # targeting a different, disjoint one.
                                    #
                                    # #325 review (Finding 1, CRITICAL): a
                                    # merging claim's target is the SURVIVOR,
                                    # never the interval that was actually
                                    # floored. A minted tip interval T
                                    # floored at position X by a failed write
                                    # descends and eventually touches a
                                    # retained base -- _coalesce makes the
                                    # base the survivor and T absorbed, so
                                    # claim_ident becomes :ingestion/frontier-
                                    # high, which carries no floor entry of
                                    # its own. Checking claim_ident alone
                                    # reads that as unrestricted and persists
                                    # a union spanning the gap X sits in --
                                    # sweeping the failed position into the
                                    # graph permanently, the exact
                                    # closed-range defect #326 exists to
                                    # prevent, reintroduced across a merge.
                                    # This MUST be evaluated at dispatch
                                    # time, not claim time (as it already is,
                                    # here): claims run ahead of writes by
                                    # pipeline_depth, so the ident that fails
                                    # may not have a floor entry yet when the
                                    # merging claim is first ALLOCATED in
                                    # submit_next, only by the time its write
                                    # is actually dispatched.
                                    pos > max(
                                        [rev_claim_floor[i] for i in
                                         [claim_ident, *(absorbed_idents or [])]
                                         if i in rev_claim_floor] or [-1]
                                    ),
                                    # #325 review round 2: the ident this
                                    # claim's interval resolved to when it
                                    # was MADE (captured in submit_next),
                                    # never re-derived here -- see
                                    # _reverse_claim_persist_target's
                                    # docstring.
                                    claim_ident, absorbed_idents,
                                )

                        except Exception as e:
                            # Ordinary per-commit write failure (malformed EDN, a
                            # transient constraint violation, ...) -- isolate it to
                            # this one commit rather than aborting every commit
                            # still pending, matching this function's own
                            # documented "fail only the one commit" contract and
                            # the extraction-phase isolation above. Opening the DB
                            # itself (the lease acquire, just above this try) is
                            # deliberately NOT covered here -- that failure is
                            # unrecoverable for every remaining commit too, so it
                            # still propagates to the outer handler.
                            print(
                                f"[_run_ingestion] skipping commit {commit_hash} "
                                f"({subject!r}): write failed: {e}",
                                file=sys.stderr,
                            )
                            _trace_write_ok = False
                            _note_incomplete_rev(tag, pos, claim_ident)
                            _note_incomplete_fwd(tag, pos)

                    # #260: no record for a commit whose write failed -- same
                    # contamination class the brief excluded extraction
                    # failures for. apply_s on a failed attempt does not
                    # measure the quantity the downstream regression models
                    # (the cost of successfully applying a commit), so
                    # recording it would inject a bad point into the fit.
                    if _ingest_trace is not None and _trace_write_ok:
                        _ingest_trace.emit(
                            pos, tag, commit_hash,
                            _trace_await_s,
                            time.perf_counter() - _trace_t_apply,
                            extracted_files,
                            _ingest_checkpoint_policy,
                        )
                    run_progress.retired(tag, "written" if _trace_write_ok else "failed", pos)
                    await asyncio.sleep(0)  # yield to event loop

                run_progress.stage_a_finished(completed_all)

                # #326: the walk may have ended with the gap empty while still
                # inside a run of skips, which nothing below persisted. Not
                # _frontier_persist_claim: after a discard the interval's facts
                # are gone, so its `existing is None` branch would write
                # lo == hi and lose the top bound.
                #
                # Gated on completed_all, like Stage B and both folds below,
                # and NOT merely for symmetry. On the shutdown break `pending`
                # is still non-empty, and those entries are positions claimed
                # and queued for extraction but never applied -- nothing
                # persisted a claim for them. :lo-hash is a RANGE bound, so an
                # ungated flush down to a skipped span's lo would swallow them
                # and declare them complete: the next run's _frontier_load
                # would read the gap as closed and never re-walk commits that
                # have no entity in the graph at all. #326's own shape makes
                # that the LIKELY case rather than an exotic one -- skips are
                # retired inline in submit_next and never occupy `pending`, so
                # the genuine tip claims sit there while the skipped span is
                # driven far below them. And _shutdown_requested is set by
                # plain stdin EOF at session end, not just by a signal.
                #
                # Skipping the flush costs nothing real: the completed-region
                # facts survive an interrupted run (_completed_regions_load
                # retracts a region only when a live same-tag interval fully
                # covers it), so the next run re-skips the same region at
                # microsecond cost -- exactly what this feature makes cheap.
                #
                # #325: one flush per ident, not one flush for the whole run.
                # A run that skips into more than one provisional interval
                # (a retained one below a fresh tip gap, say) has to persist
                # each interval's own skipped span to ITS OWN entity -- a
                # single flush written unconditionally to
                # :ingestion/frontier-high would either miss a minted
                # interval's span entirely or, worse, misattribute it.
                #
                # #326 Finding A: each entry obeys the SAME ident's floor the
                # per-commit claims do. _frontier_persist_span moves :lo-hash
                # DOWN, so an unclamped flush would re-open the exact hole the
                # floor closes -- a skipped position below a failed one would
                # drag the persisted bound past it in one write. Clamping the
                # lo bound (rather than dropping the flush) keeps it doing its
                # job for the skipped span that IS above that ident's floor.
                #
                # #325 review Finding 4 (CRITICAL): clamping `lo_pos` against
                # the widened (provenance-aware) floor is NECESSARY but not
                # SUFFICIENT once a fold has moved another interval's span
                # onto this ident. Demonstrated case: T is floored at
                # position 10 by a failed write; T's own skipped span (from
                # an archived region at position 11) folds onto a retained
                # base whose own on-disk range is [1,6] -- far below either
                # number. The span being flushed, (11,11), is already ABOVE
                # the floor (10), so clamping `lo_pos` by `floor + 1 = 11`
                # is a no-op -- lo_pos was already 11. The danger is not in
                # the flushed span's OWN bounds; it is that
                # _frontier_persist_span's advance-only union takes base's
                # existing hi (6) and the flushed hi (11) and silently
                # bridges the entire gap between them, INCLUDING position 10
                # -- which no witness anywhere ever proved complete for
                # `base`. Reproduced empirically pre-fix: frontier-high ends
                # [1,11], with zero :commit/... entities at position 10.
                #
                # So the flush additionally REFUSES this ident outright on
                # the DISJUNCTION `lo_pos > floor or len(sources) > 1`,
                # rather than clamping -- and BOTH halves are required,
                # because they guard two DIFFERENT ways the clamp can fail,
                # not one restated twice.
                #
                # `lo_pos > floor` guards case C: a flush whose own lo_pos
                # already sits above the floor, with NO fold involved
                # (len(sources) == 1) -- there the clamp `max(lo_pos,
                # floor + 1)` degenerates to a no-op and refusing is the
                # only thing that still does anything. Stated precisely,
                # because the case demonstrated just above (Finding 4) is
                # NOT case C: T's own skipped span there folds onto the
                # retained base to produce the flush, so len(sources) == 2
                # and `len(sources) > 1` alone already refuses it -- the
                # same-numbered test asserts on frontier-high's persisted
                # range, not on which half of the disjunction fired, so it
                # does not distinguish the two. Case C is NOT ruled out by
                # needing a fold -- within ONE ident, a same-run skip at a
                # HIGHER position followed by a write failure at a LOWER one
                # produces exactly lo_pos > floor with sources == {that
                # ident} and no merge anywhere (skipped_span/
                # skipped_span_sources default to the singleton {ident}
                # until a merge unions another one in, and
                # _note_incomplete_rev floors whichever ident the failing
                # claim already belongs to). What actually keeps this hard
                # to reach today is narrower: _skip_claim (what populates
                # skipped_span at all) requires a loadable archived
                # :type/completed-region covering the position, and after
                # #325 that only exists for the narrow divergent-ref-
                # regained case -- see _load_one_interval's docstring. So
                # constructing case C requires first constructing that
                # already-narrow prerequisite. No test isolates
                # `lo_pos > floor` from `len(sources) > 1`; that half is
                # belt-and-braces against exercising this narrow same-ident
                # shape, not a demonstrated requirement.
                #
                # `len(sources) > 1` guards a SEPARATE case that
                # `lo_pos > floor` cannot see at all: a fold whose merged
                # span STRADDLES the floor, i.e. `lo_pos <= floor < hi_pos`.
                # There `lo_pos > floor` is False, so the clamp fires and
                # looks fine in isolation -- lo_pos becomes floor + 1, still
                # inside [lo_pos, hi_pos]. But _frontier_persist_span's own
                # union (see its docstring) is against the FOLD TARGET's
                # EXISTING on-disk hi, not against lo_pos, and a fold is
                # exactly what can put that existing hi far BELOW the floor
                # (the absorbed interval's span reaches down to lo_pos while
                # the target's own claimed territory stops well short of
                # it). The union then bridges existing_hi -> hi_pos in one
                # shot, crossing the floored position regardless of what
                # lo_pos was clamped to. #325 review round 3 reproduced this
                # exact shape by seeding an extra completed region BELOW the
                # injected write failure so the fold's merged span straddled
                # rather than sat entirely above it: `lo_pos > floor` alone
                # let the clamp+flush through and left frontier-high at
                # [1,11] with zero :commit/... entities at position 10 --
                # the identical corruption the fold-only trigger existed to
                # prevent, just reached through the other half of the
                # disjunction. `len(sources) > 1` is exactly "a merge folded
                # something else into this span this run", the one
                # condition under which the flush target's on-disk bounds
                # and the flushed span's own claim history can have
                # diverged this far -- so it is the correct, and only,
                # guard for that shape.
                #
                # An earlier version of this comment claimed testing
                # `lo_pos > floor` alone "needs none of th[e] side
                # reasoning" `len(sources) > 1` rested on. That claim was
                # the bug: it swapped one case for the other instead of
                # widening the refusal, and the straddling scenario above is
                # the reachable counter-example. Refusing on either
                # condition costs nothing but a re-walk next run (the
                # completed-region fact that produced the skip is untouched,
                # independent of skipped_span entirely), which is the
                # accepted price #326 established throughout for "unproven
                # -> don't assert, re-walk instead".
                if completed_all:
                    for ident, (lo_pos, hi_pos) in sorted(skipped_span.items()):
                        sources = skipped_span_sources.get(ident, {ident})
                        floor = max(
                            (rev_claim_floor[s] for s in sources if s in rev_claim_floor),
                            default=None,
                        )
                        # `floor is None` (no clamp, no refusal below) is safe
                        # only because of a three-link chain, none of it
                        # visible at this call site alone -- remove any one
                        # link and this becomes a bridge across an
                        # incomplete position:
                        #
                        # 1. next_claim() returns None only on
                        #    self._allocator.is_gap_empty() -- never on a
                        #    single stream having nothing to do this call
                        #    (see its docstring's #325 review round 2 note).
                        #    So the `while pending` loop draining normally
                        #    (completed_all left True) means every position
                        #    in the shared gap was actually claimed and
                        #    popped, not skipped over by a stream that gave
                        #    up early.
                        # 2. Both paths that retire a claimed reverse
                        #    position WITHOUT a persisted claim -- the
                        #    extraction `except` and the write `except`,
                        #    above -- call _note_incomplete_rev, which sets
                        #    rev_claim_floor for that position's ident (and,
                        #    via a merge's absorbed_idents, every ident it
                        #    folded through). So an ident with no entry in
                        #    rev_claim_floor had no incomplete retirement.
                        # 3. The one other way a claimed reverse position
                        #    retires without a persisted claim -- the
                        #    _shutdown_requested break in the `while pending`
                        #    loop above -- sets completed_all = False instead
                        #    of a floor, and this whole block is gated on
                        #    completed_all being True.
                        #
                        # Together: completed_all True means every claimed
                        # position either persisted normally or is accounted
                        # for by a floor. So `floor is None` for this ident's
                        # sources means every position this run claimed under
                        # them completed -- the unclamped flush below is
                        # flushing a span this run actually proved, not one
                        # it merely didn't happen to fail on.
                        if floor is not None:
                            if lo_pos > floor or len(sources) > 1:
                                continue
                            lo_pos = max(lo_pos, floor + 1)
                        if lo_pos > hi_pos:
                            continue
                        async with _db_lease_async_committing_index(loop, write_executor, index_con) as db:
                            await loop.run_in_executor(
                                write_executor, _frontier_persist_span, db, linearization,
                                lo_pos, hi_pos, False,
                                commit_metadata[hi_pos][1], index_con, ident,
                            )

                # Stage B: the correction sweep. A third, strictly
                # SEQUENTIAL pass, not a third concurrent task -- claim_low()
                # and claim_high() partition one shared gap, so no sequence of
                # forward claims can reach territory the reverse stream
                # already claimed, and the sweep's own precondition is that
                # the gap is already closed.
                #
                # Drives 2c's three pieces directly on their correct
                # executors. The legacy synchronous convenience wrappers for
                # this sweep (see their docstrings) must NOT be used here:
                # they fuse the CPU-bound parse and the DB-bound writes into
                # one body.
                if completed_all:
                    _ingest_progress["phase"] = "sweeping"
                    hash_to_pos = {h: i for i, h in enumerate(linearization)}
                    # #235: the sweep's repair path needs linearization
                    # positions to pick which of a corrupted entity's
                    # :introduced-by values survives. Same construction
                    # _reverse_apply uses.
                    sweep_pos_by_commit_ident = {
                        f":commit/{h[:12]}": i
                        for i, (h, _t, _a, _s) in enumerate(commit_metadata)
                    }
                    skipped = 0
                    # #222 phase 5 item C. Stage B no longer holds ONE lease
                    # across the whole sweep -- see _SWEEP_YIELD_COMMITS for why
                    # that silently discarded every auto-memory write for the
                    # sweep's duration. The loop below is now an outer WINDOW
                    # loop that re-enters the lease each time round, so these
                    # three are hoisted above it to survive across windows.
                    sweep_fragmented = None
                    nxt = None
                    sweep_done = False
                    # NOT `while not sweep_done and not _shutdown_requested`: the
                    # first window must always be entered, because that is where
                    # the sweep is PLANNED (run_progress.sweep_planned). A run
                    # whose shutdown flag is already set on arrival here would
                    # otherwise report a sweep it never even planned, where the
                    # single-lease version planned first and only then found the
                    # flag. The inner loop's own condition reproduces that
                    # exactly, and the trailing break ends every later window.
                    while not sweep_done:
                        window_started = time.monotonic()
                        window_count = 0
                        # The fact index is committed before this lease is
                        # released, on EVERY exit from the window (count or
                        # clock boundary, sweep done, sweep aborted, shutdown,
                        # exception). _correction_sweep_through_update writes
                        # index rows AFTER _forward_apply's own commit, and
                        # _index_write never commits a caller-supplied
                        # index_con -- so without this the window released the
                        # graph with SQLite's writer lock still held, and a
                        # hook that took the graph lock then deadlocked against
                        # it. See _db_lease_async_committing_index.
                        async with _db_lease_async_committing_index(
                            loop, write_executor, index_con,
                        ) as db:
                            if sweep_fragmented is None:
                                # #325 review Finding 3: computed ONCE, not inside
                                # the loop below. Stage B only starts once
                                # completed_all is True -- the whole gap is
                                # claimed and the walk that mints/merges interval
                                # entities has already finished for this run -- and
                                # nothing in this loop's body (select/extract/apply/
                                # lifecycle-apply/watermark-update/checkpoint)
                                # writes an interval fact, so fragmentation cannot
                                # change between iterations. Passing it in turns
                                # 2 extra datalog queries per swept commit into 2
                                # for the whole sweep, mirroring the hash_to_pos
                                # idiom just above.
                                sweep_fragmented = bool(await loop.run_in_executor(
                                    write_executor, _intervals_read_extra, db,
                                ))
                            if nxt is None:
                                # #222 phase 4: the first call both PLANS the sweep
                                # (to_sweep, or why it declines) and is the loop's
                                # first iteration, so planning costs no query.
                                nxt = await loop.run_in_executor(
                                    write_executor, _correction_sweep_next,
                                    db, linearization, commit_metadata, hash_to_pos,
                                    sweep_fragmented,
                                )
                                run_progress.sweep_planned(
                                    nxt.reason, nxt.region_lo, nxt.start_pos, nxt.ceiling_pos,
                                )
                            while not _shutdown_requested.is_set():
                                if nxt.selected is None:
                                    run_progress.sweep_ended(nxt.reason)
                                    sweep_done = True
                                    break
                                sweep_hash, sweep_ts = nxt.selected
                                try:
                                    sweep_extracted = await loop.run_in_executor(
                                        executor, _extract_commit, repo_path, sweep_hash, ignore_patterns,
                                    )
                                    sweep_files = sweep_extracted[0]
                                    # update_watermark=False: this commit is only
                                    # half-processed until the lifecycle pass below
                                    # also lands, so the sweep watermark (and its
                                    # checkpoint) is deferred to after it -- see
                                    # _correction_sweep_apply's own docstring.
                                    skipped += await loop.run_in_executor(
                                        write_executor, _correction_sweep_apply,
                                        db, sweep_hash, sweep_ts, sweep_files, index_con, skipped,
                                        False, sweep_pos_by_commit_ident,
                                    )
                                    # Apply the lifecycle facts the reverse stream
                                    # skipped entirely (D/R closes, renames,
                                    # dependency-edge churn, gitlink changes).
                                    # Fresh writes, not re-application -- nothing
                                    # wrote them for these commits. Runs AFTER the
                                    # A/M reconciliation above so an entity's
                                    # lineage is already authoritative before a
                                    # close in the same commit reads its window.
                                    await loop.run_in_executor(
                                        write_executor, _forward_apply, db, repo_path, state,
                                        commit_metadata[hash_to_pos[sweep_hash]],
                                        sweep_extracted, index_con, None, None, True,
                                    )
                                    # Both halves landed -- only now is this commit
                                    # genuinely swept, so only now may the watermark
                                    # name it. Same one-checkpoint-per-commit cadence
                                    # _correction_sweep_apply had when it owned this.
                                    await loop.run_in_executor(
                                        write_executor, _correction_sweep_through_update,
                                        db, sweep_hash, sweep_ts, index_con,
                                    )
                                    run_progress.swept(hash_to_pos[sweep_hash])
                                    await loop.run_in_executor(write_executor, _db_checkpoint_gated, db)
                                except concurrent.futures.process.BrokenProcessPool:
                                    raise
                                except Exception as e:
                                    # A sweep-step failure aborts Stage B only.
                                    # Stage A's work is already persisted, and this
                                    # commit's watermark was never advanced (see
                                    # update_watermark=False above), so the next
                                    # run's _correction_sweep_select_position
                                    # re-selects THIS commit and reprocesses it from
                                    # the start -- nothing is silently skipped.
                                    print(
                                        f"[_run_ingestion] correction sweep aborted at {sweep_hash}: {e}",
                                        file=sys.stderr,
                                    )
                                    # Drop the traceback before leaving the loop.
                                    # It chains back through _WorkItem.run to
                                    # wherever `db` (a _LeasedDb, post-proxy) was
                                    # passed into the failing call -- but severing
                                    # THAT reference doesn't help: inside the
                                    # native call, `db.__getattr__` already
                                    # unwrapped it to the real MiniGrafDb, and the
                                    # native `execute` frame in the traceback
                                    # holds THAT as its own `self`, not the proxy.
                                    # So the raw handle stays alive no matter how
                                    # carefully the finallys below clear their
                                    # locals or how faithfully _LeasedDb severs.
                                    # The outer finally's final checkpoint then
                                    # cannot open the graph ("Database is already
                                    # open in this process") and an interrupted
                                    # run silently skips its WAL compaction. Only
                                    # the message is used above, so nothing is
                                    # lost by dropping the traceback.
                                    e.__traceback__ = None
                                    completed_all = False
                                    run_progress.sweep_ended("aborted")
                                    sweep_done = True
                                    break
                                await asyncio.sleep(0)  # yield to event loop
                                nxt = await loop.run_in_executor(
                                    write_executor, _correction_sweep_next,
                                    db, linearization, commit_metadata, hash_to_pos,
                                    sweep_fragmented,
                                )

                                # #222 phase 5 item C: the ONLY safe place to end a
                                # window. _correction_sweep_apply,
                                # _forward_apply(lifecycle_only=True) and
                                # _correction_sweep_through_update are ONE unit -- the
                                # watermark is deliberately deferred until both halves
                                # land (update_watermark=False above), so a boundary
                                # anywhere inside that sequence creates exactly the
                                # half-processed state the deferral exists to prevent.
                                # Here the previous commit is fully swept AND its
                                # watermark has landed, and `nxt` already names the next
                                # one, so dropping the lease loses nothing: the next
                                # window re-enters with `nxt` carried across and does not
                                # re-plan.
                                #
                                # Two triggers, neither redundant. The COUNT bounds how
                                # many O(graph size) drop-checkpoints the release costs
                                # (#280); the CLOCK bounds how long the hooks are locked
                                # out when one window's commits are individually slow,
                                # which on a large graph they are.
                                #
                                # Never when `nxt` selected nothing: the sweep is
                                # already over, and breaking here would pay a
                                # pause, a fresh lease and its O(graph size)
                                # drop-checkpoint (#280) only to discover that at
                                # the top of the next window. Falling through lets
                                # the loop head end the sweep inside THIS lease --
                                # which is what makes "a sweep fitting in one
                                # window pays nothing" true when its length is an
                                # exact multiple of _SWEEP_YIELD_COMMITS.
                                window_count += 1
                                if nxt.selected is not None and (
                                    window_count >= _SWEEP_YIELD_COMMITS
                                    or time.monotonic() - window_started >= _SWEEP_YIELD_SECONDS
                                ):
                                    break
                        # A window that ended on the shutdown flag must not open
                        # another one. Checked out here rather than in the outer
                        # condition so the first window is still entered above.
                        if _shutdown_requested.is_set():
                            break
                        # Hold the graph genuinely unlocked for a moment. This
                        # sleep is OUTSIDE the lease by construction -- the
                        # `async with` above has exited and the next window's
                        # has not been entered -- which is the whole point: a
                        # sleep inside the lease would accomplish nothing but
                        # slow the sweep down. See _SWEEP_YIELD_PAUSE_SECONDS
                        # for why the gap has to be an interval rather than the
                        # instant a bare release leaves behind.
                        #
                        # asyncio.sleep, never time.sleep: this runs on the
                        # event loop, so a blocking sleep would freeze every
                        # concurrent call_tool for the duration (#99) -- and
                        # tests/_forbid_blocking_sleep_on_event_loop fires on
                        # exactly that.
                        #
                        # Skipped when the sweep is already finished, so the
                        # last window never pays it.
                        if not sweep_done:
                            await asyncio.sleep(_SWEEP_YIELD_PAUSE_SECONDS)
                    if _shutdown_requested.is_set():
                        completed_all = False
                        run_progress.sweep_ended("stopped")
                    _correction_sweep_log_summary(skipped)
                    # The fold takes its own lease: the sweep's last window has
                    # already released, and re-entering here is what keeps every
                    # window's acquire/release balanced (#255's single-handle
                    # invariant) instead of leaving one straggler scope open.
                    async with _db_lease_async_committing_index(loop, write_executor, index_con) as db:
                        # DB-bound like everything else here, so it runs on
                        # write_executor rather than inline on the event loop.
                        should_fold = completed_all and await loop.run_in_executor(
                            write_executor, _should_fold_lineage_watermark, db, linearization,
                        )
                        if should_fold:
                            await loop.run_in_executor(
                                write_executor, _lineage_confirmed_through_update,
                                db, linearization[-1], commit_metadata[-1][1], index_con,
                            )
                            run_progress.folded()
                            await loop.run_in_executor(write_executor, _db_checkpoint_gated, db)

                # Call _ingest_tags and _last_run_write before closing index_con
                # so they use the batched connection instead of opening new ones
                if completed_all:
                    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                    async with _db_lease_async_committing_index(loop, write_executor, index_con) as db:
                        await loop.run_in_executor(write_executor, _ingest_tags, db, repo_path, now, index_con)
                        # #222 phase 4: the tip this run covered, never
                        # whichever commit Stage A applied last -- on a
                        # converging run that was the meeting point, on a
                        # no-op run the forward watermark, on a no-commits run
                        # the starting watermark. And the TRUE commit count,
                        # never the seeded walk counter, which exceeds the repo
                        # on any re-walk (28 of 20 measured).
                        graph_commits = await loop.run_in_executor(
                            write_executor, _count_commit_entities, db,
                        )
                        last_hash = linearization[-1] if linearization else (watermark or "")
                        await loop.run_in_executor(
                            write_executor, _last_run_write, db, last_hash, now,
                            graph_commits, index_con,
                        )
                        # No checkpoint here: the unconditional final
                        # checkpoint in the outer finally below (#241)
                        # already covers this path. A checkpoint here would
                        # be a full ungated compaction immediately followed
                        # by the lease release, a reopen, and another
                        # compaction with nothing dirty in between -- the same
                        # structural duplicate #241 removed from Stage B.
            finally:
                # Compact the WAL on EVERY terminal path, not just
                # completed_all. Under the duty-cycle cadence an interrupted
                # run can leave a whole run's writes outstanding in
                # <graph>.wal, and the next process to open the graph pays
                # the replay (~45 ms/MB). Nothing is lost if this fails --
                # the WAL is already durable -- so a failure here must never
                # mask the real error that brought us into this finally
                # (same rule as _checkpoint_after_write, #176). Deliberately
                # calls _db_checkpoint directly, not _db_checkpoint_gated:
                # this one must never be suppressed. (The completed_all path
                # used to run its own redundant checkpoint immediately
                # before falling into this finally -- removed, since this
                # one already covers it; see the comment left at its old
                # call site.)
                try:
                    async with db_lease_async() as final_db:
                        await loop.run_in_executor(write_executor, _db_checkpoint, final_db)
                except Exception as e:
                    print(f"[_run_ingestion] final checkpoint failed: {e}", file=sys.stderr)
                # ProcessPoolExecutor.shutdown(wait=True) blocks joining the
                # worker OS processes — measured ~90ms even for a pool that
                # never did any real work, entirely from process-exit
                # teardown, not GIL contention. That's a plain blocking call:
                # running it inline here would stall the event loop for that
                # whole span, undoing this fix's own purpose in its teardown.
                # Routing it through write_executor keeps the wait off the
                # event-loop thread, same as every other blocking call above.
                await loop.run_in_executor(write_executor, _close_index_writer_safe, index_con)
                await loop.run_in_executor(write_executor, executor.shutdown)

            # "phase" describes what a RUNNING ingest is currently doing, so
            # it must not outlive the run: leaving Stage B's "sweeping" in
            # place made a finished run report {"status": "complete", "phase":
            # "sweeping"} forever. Cleared on every terminal path below, not
            # just this one.
            _ingest_progress["phase"] = None
            if completed_all:
                _ingest_progress["status"] = "complete"
            else:
                _ingest_progress["status"] = "stopped"
            run_progress.ended(_ingest_progress["status"])
        finally:
            # Publish realised checkpoint duty into _ingest_progress BEFORE
            # discarding the policy below -- its counters do not survive
            # past this point, and _ingest_progress is the established
            # channel for run metadata that does (#241 Task 6). Guarded on
            # `is not None` so this is a no-op if the outer finally's own
            # guarded publish (below) already ran first on some exit path
            # that reaches both -- summary() is a pure read, so writing it
            # twice would be harmless anyway, but the guard keeps one
            # obvious writer per policy lifetime.
            if _ingest_checkpoint_policy is not None:
                _ingest_progress["checkpoint_summary"] = _ingest_checkpoint_policy.summary()
            # Never let a finished run's budget gate a later interactive
            # transact (#241).
            _ingest_checkpoint_policy = None
            if _ingest_trace is not None:
                _ingest_trace.close()
            _ingest_trace = None

    except Exception as e:
        # write_executor is shut down by the outermost finally below, which
        # covers every path into this handler -- including the two awaited
        # calls above the inner try (_open_index_writer_safe, _frontier_load).
        # The comment that used to sit here asserted the inner finally already
        # covered them; it did not, and that is #250.
        #
        # No _reset_db_state() here (#255): every ingestion lease is now
        # `with`-scoped, so by the time an exception reaches this handler
        # there is nothing of THIS run's to clean up. Calling it anyway is
        # actively wrong on two counts -- it desyncs the count if some OTHER
        # caller (e.g. a concurrent call_tool request) holds a legitimate
        # lease right now, and it zeroes the count without setting
        # _prev_ref, which would silently defeat _detect_leaked_handle for
        # exactly the failure path most likely to leak one.
        _ingest_progress["phase"] = None
        _ingest_progress["status"] = "error"
        _ingest_progress["error"] = str(e)
        _ingest_progress["error_at"] = _now_utc_ms()
        if _ingest_progress.get("_run") is not None:
            _ingest_progress["_run"].ended("error")
        # #270. Until this print, _ingest_progress was the ONLY record of
        # what killed the run: this handler swallows the exception and
        # returns normally, so a background run started through
        # handle_minigraf_ingest_git left nothing in any log, and the
        # at-scale harness's stderr tee could not scan for text that was
        # never written to fd 2 -- error_signals read CLEAN for a run that
        # produced nothing. The per-commit skip sites above already print
        # their own failures; this is the run-level one that was missing.
        #
        # The traceback, not just str(e): this handler covers ~500 lines of
        # _run_ingestion, and str() of a bare KeyError is the key alone.
        # format_exc() returns a plain string -- it holds no frame past this
        # statement, so it cannot keep a leased MiniGrafDb alive into the
        # next acquire's _detect_leaked_handle check (the hazard the sweep's
        # `e.__traceback__ = None` guards against, for a traceback that IS
        # retained). Best-effort: fd 2 can be a closed tee pipe by the time
        # a late failure lands here, and a BrokenPipeError from logging must
        # not displace the error being reported.
        with contextlib.suppress(BaseException):
            print(
                f"[_run_ingestion] ingestion failed: {e}\n{traceback.format_exc()}",
                file=sys.stderr,
            )
    finally:
        # The checkpoint policy is a separate story from write_executor
        # above: it is installed just after write_executor is created,
        # several statements above the `try` that owns the finally clearing
        # it in the normal case (see that finally's own comment). A failure
        # in that gap -- e.g. _frontier_load's one-time migration -- skips
        # that inner finally entirely. A `finally` here (rather than another
        # `except Exception`) is required, not just belt-and-suspenders:
        # `except Exception` does not catch BaseException-rooted control
        # flow (asyncio.CancelledError, KeyboardInterrupt, SystemExit), so a
        # cancellation landing in that same gap would still leak the policy
        # with only an `except Exception` clear. Idempotent with the inner
        # finally's own clear on every path that reaches it (#241).
        #
        # A failure in the pre-write-scope gap (e.g. _frontier_load's
        # migration) never reaches the inner finally at all, so THIS is the
        # only place that publishes the summary for that path -- guarded the
        # same way, and for the same reason (#241 Task 6).
        if _ingest_checkpoint_policy is not None:
            _ingest_progress["checkpoint_summary"] = _ingest_checkpoint_policy.summary()
        _ingest_checkpoint_policy = None
        if _ingest_trace is not None:
            _ingest_trace.close()
        _ingest_trace = None
        # The single shutdown for this executor. It lives here, not in the
        # inner finally, because two awaited calls (_open_index_writer_safe,
        # _frontier_load) sit above the inner try and can raise in the gap --
        # #250. One cleanup writer for one resource.
        if write_executor is not None:
            write_executor.shutdown(wait=True)
        # Retract the ownership hint LAST, so it stays published for as long
        # as this run holds anything another process would race us for.
        # Suppressed rather than awaited bare: a failure retracting an
        # advisory hint must not replace whatever exception is already in
        # flight, and the TTL expires a leaked hint anyway.
        with contextlib.suppress(Exception):
            await owner_hint.__aexit__(None, None, None)


async def handle_minigraf_ingest_git(
    repo_path: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Start background git ingestion. Returns immediately."""
    global _ingest_task, _ingest_progress
    if _ingest_task and not _ingest_task.done():
        return {"ok": False, "error": "ingestion already in progress"}
    # Proactive check-before-attempt: if another live process already owns
    # the graph, don't start ingestion here rather than racing for it and
    # losing (#108). Reads our own ownership hint, not minigraf's lock --
    # see _graph_owner_hint for why nothing portable can read the latter.
    owner = _graph_owner_hint(_graph_path_current())
    if owner is not None:
        holder_pid = owner.get("pid")
        _ingest_progress["phase"] = None
        _ingest_progress["status"] = "skipped"
        _ingest_progress["owner_pid"] = holder_pid
        # A declined start must not echo the previous in-process run's
        # this_run/streams/visibility/lineage numbers -- there is no run this
        # time, so there is nothing to report them for.
        _ingest_progress["_run"] = None
        # Fix round 1: same reasoning -- there is no run this time, so no
        # orphan count was computed for it either.
        _ingest_progress["orphaned_commits"] = None
        return {
            "ok": False,
            "error": f"ingestion already owned by live process (pid {holder_pid})",
            "owner_pid": holder_pid,
        }
    repo = repo_path or str(Path.cwd())
    try:
        check = _subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo, capture_output=True, text=True,
        )
        valid = check.returncode == 0
    except OSError:
        valid = False
    if not valid:
        return {
            "ok": False,
            "error": f"Not a git repository (or git not found): {repo}",
        }
    _ingest_progress = {
        "status": "starting", "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
        "phase": None, "orphaned_commits": None,
    }
    _ingest_task = asyncio.create_task(_run_ingestion(repo, branch or _default_git_branch(repo)))
    return {"ok": True, "job_id": "git-ingest", "message": f"Ingestion started for {repo}"}


def handle_minigraf_ingest_status() -> Dict[str, Any]:
    """Return current ingestion progress, augmented with graph-backed last-run info."""
    # Keys starting with "_" are in-process objects (#222 phase 4's "_run"),
    # never response fields -- call_tool json.dumps this dict.
    result: Dict[str, Any] = {
        "ok": True,
        **{k: v for k, v in _ingest_progress.items() if not k.startswith("_")},
    }
    run = _ingest_progress.get("_run")
    if run is not None:
        result.update(run.snapshot())
    # Staleness: a terminal error/skipped state can outlive the condition
    # that caused it (e.g. the orphaned holder it names has since died) —
    # re-check liveness on every poll instead of echoing a dead PID forever.
    # Purely informational: never auto-retries ingestion (#106).
    #
    # The "error" branch used to name the holder by scraping a PID out of
    # minigraf's lock-contention message. minigraf 2.0.0 removed "holder PID:
    # N" from that text entirely, so there is nothing left to scrape and no
    # staleness can be reported for that path (#284).
    if _ingest_progress["status"] == "skipped":
        if _ingest_progress.get("owner_pid") is not None:
            # Staleness now follows the ownership hint's freshness rather than
            # the recorded PID's liveness: a hint stops being refreshed when
            # its holder stops running, however it stops.
            result["stale"] = not _graph_owner_hint_is_fresh(_graph_path_current())
    if _ingest_progress["status"] != "running":
        try:
            with db_lease() as db:
                # :any-valid-time is needed since valid-from is the run's own
                # timestamp, not real wall-clock time (see _total_ingested_query),
                # but it also surfaces already-closed historical rows -- bind and
                # filter :db/valid-to to the open-fact sentinel on each attribute
                # so only the current run's own (?t, ?h) pair is returned, not a
                # cross-product with a different historical run's value (#186).
                raw = _db_execute(
                    db,
                    "(query [:find ?t ?h :any-valid-time "
                    ":where [:ingestion/last-run-at :last-run-at ?t] "
                    "[:ingestion/last-run-at :db/valid-to ?vt1] [(= ?vt1 9223372036854775807)] "
                    "[:ingestion/last-run-at :last-commit ?h] "
                    "[:ingestion/last-run-at :db/valid-to ?vt2] [(= ?vt2 9223372036854775807)]])"
                )
                rows = json.loads(raw).get("results", [])
                if rows:
                    result["last_run_at"] = rows[0][0]
                    result["last_commit"] = rows[0][1]
                else:
                    result["last_run_at"] = None
                    result["last_commit"] = None
                # True persisted count, not the :total-ingested watermark — the
                # watermark is only written on clean completion, so it drifts
                # arbitrarily far from reality after a run is interrupted
                # mid-way (see issue #85).
                n = _count_commit_entities(db)
                result["total_ingested"] = n if n > 0 else None
                result["lineage_confirmed_through"] = _lineage_confirmed_through_query(db)
        except Exception:
            result["last_run_at"] = None
            result["last_commit"] = None
            result["total_ingested"] = None
            result["lineage_confirmed_through"] = None
    return result


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

from mcp.types import Tool, TextContent  # noqa: E402

# `pyproject.toml` carries this until `release.yml` stamps the real version
# into it at build time, so a dev install's dist-info holds it as if it were
# a version. It is not one, here or anywhere else.
_VERSION_PLACEHOLDER = "0.0.0"


def _plugin_json_version() -> Optional[str]:
    """The canonical version, from the file that owns it -- or None.

    `.claude-plugin/plugin.json` is NOT in the wheel (`py-modules` ships four
    `.py` files and nothing else), so this returns None for an installed
    package and a real version for a checkout. That is the right way round:
    an installed package has stamped metadata, and a checkout does not.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".claude-plugin", "plugin.json")
    try:
        with open(path) as f:
            return json.load(f)["version"] or None
    except Exception:
        return None


def _package_version() -> str:
    """The version reported to MCP clients in `serverInfo` (#312).

    Passing no version at all makes the SDK substitute its own package
    version, so clients were told this server was `mcp`'s version -- a number
    that moved with a user's resolver and never with a release here.

    Installed metadata first, since that is what a released wheel has and a
    checkout does not. But a dev install writes a real dist-info holding the
    `0.0.0` placeholder, which shadows nothing and answers nothing, so it is
    treated as absent rather than trusted. `unknown` beats reporting a
    placeholder as if it were a version.
    """
    import importlib.metadata

    try:
        installed = importlib.metadata.version("temporal-reasoning")
    except Exception:
        installed = None
    if installed and installed != _VERSION_PLACEHOLDER:
        return installed
    return _plugin_json_version() or "unknown"


server = Server("temporal-reasoning", version=_package_version())

_TOOLS: List[Tool] = [
    Tool(
        name="minigraf_query",
        description=(
            "Query Minigraf's persistent bi-temporal graph memory using Datalog. "
            "Call this BEFORE answering anything about past decisions, architecture, "
            "dependencies, or preferences. Two independent temporal axes are supported: "
            "transaction time via :as-of N (what the graph contained as of write N) and "
            "valid time via :valid-at \"2024-01-01\" (what was true in the world on that "
            "date, e.g. for code-structure queries). Use :any-valid-time to ignore the "
            "valid-time filter entirely."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "datalog": {
                    "type": "string",
                    "description": "A valid Datalog query, e.g. [:find ?name :where [?e :component/name ?name]]",
                },
            },
            "required": ["datalog"],
        },
    ),
    Tool(
        name="minigraf_transact",
        description=(
            "Store a durable fact in Minigraf's graph memory. Only call this for decisions, "
            "architecture, dependencies, constraints, or preferences — NOT for transient "
            "observations or intermediate reasoning."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "facts": {
                    "type": "string",
                    "description": (
                        'A Datalog transact block, e.g. [[:decision/cache-strategy '
                        ':description "use Redis"]] -- attributes are bare '
                        '(:description, :rationale, :date, :alias), NOT namespaced'
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why this fact deserves long-term storage. "
                        "Forces you to justify writes — only store facts worth remembering."
                    ),
                },
            },
            "required": ["facts", "reason"],
        },
    ),
    Tool(
        name="minigraf_retract",
        description=(
            "Retract a fact from Minigraf's graph memory. Retraction records a new fact with "
            "asserted=false — the original stays in history for bi-temporal auditing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "facts": {
                    "type": "string",
                    "description": "A Datalog retract block, e.g. [[:component/auth :calls :component/jwt]]",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this fact is being retracted. Forces you to justify the removal.",
                },
            },
            "required": ["facts", "reason"],
        },
    ),
    Tool(
        name="minigraf_rule",
        description=(
            "Register a Datalog rule for use in subsequent queries. "
            "Rules enable recursive graph traversal (e.g. ancestor, reachable). "
            "A rule persists for the server session — re-register after a server restart. "
            "Syntax: [(rule-name ?arg ...) body-clause ...] — omit the outer (rule ...) wrapper."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "rule": {
                    "type": "string",
                    "description": (
                        "Rule vector, e.g. [(ancestor ?a ?d) [?a :parent ?d]] "
                        "or [(ancestor ?a ?d) [?a :parent ?m] (ancestor ?m ?d)]"
                    ),
                },
            },
            "required": ["rule"],
        },
    ),
    Tool(
        name="minigraf_report_issue",
        description=(
            "Report an issue with Minigraf query or transact operations. "
            "Use this when Minigraf returns errors to file a GitHub issue for tracking."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_type": {
                    "type": "string",
                    "description": "Type of issue to report",
                    "enum": ["invalid_query", "transact_failure", "parse_error", "minigraf_bug"],
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable description of the issue",
                },
                "datalog": {
                    "type": "string",
                    "description": "Optional Datalog query or transact that failed",
                },
                "error": {
                    "type": "string",
                    "description": "Optional error message returned by Minigraf",
                },
            },
            "required": ["issue_type", "description"],
        },
    ),
    Tool(
        name="memory_prepare_turn",
        description=(
            "Retrieve relevant memory context for the current user message. "
            "Call this at the START of every turn, before reading the user's message. "
            "Returns a context block string to prepend to your working context. "
            "On build/fix/navigate-shaped messages, also appends a one-line nudge "
            "toward minigraf_query-based code-graph navigation when the repo has "
            "an ingested graph."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user_message": {
                    "type": "string",
                    "description": "The user's message for this turn",
                },
            },
            "required": ["user_message"],
        },
    ),
    Tool(
        name="memory_finalize_turn",
        description=(
            "Extract and store memorable facts from the completed conversation turn. "
            "Call this at the END of every turn, after composing your response. "
            "Pass the full user+agent exchange for this turn."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_delta": {
                    "type": "string",
                    "description": "The user message and agent response for this turn",
                },
            },
            "required": ["conversation_delta"],
        },
    ),
    Tool(
        name="minigraf_audit",
        description=(
            "Audit all graph entities against the built-in schema. "
            "Retracts entities with schema violations (missing required attributes, "
            "unknown types, unknown attributes). Run periodically or after heavy write sessions. "
            "Pass as_of (transaction number) for a read-only point-in-time audit without retractions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "as_of": {
                    "type": "integer",
                    "description": "Optional transaction number for point-in-time audit (read-only, no retractions)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="minigraf_ingest_git",
        description=(
            "Ingest code structure from git history into the bi-temporal graph. "
            "Starts a background task and returns immediately. "
            "Call minigraf_ingest_status to poll progress."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the git repo root. Defaults to cwd.",
                },
                "branch": {
                    "type": "string",
                    "description": (
                        "Branch or ref to walk. Defaults to MINIGRAF_GIT_BRANCH if "
                        "set, otherwise auto-detects the repo's main/master branch, "
                        "falling back to HEAD only if neither exists."
                    ),
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="minigraf_ingest_status",
        description=(
            "Return the current git ingestion progress. status is one of: idle, "
            "starting, running, complete, error, stopped, skipped. starting means "
            "a background task exists but has not finished its preload phase, so "
            "total is not populated yet. this_run appears once the run's "
            "frontier is loaded, which can lag status: running, so a running "
            "status can show total set with this_run still absent. stopped means a graceful "
            "shutdown paused ingestion between commits — not a failure; the next "
            "run resumes from the watermark. skipped means another live process "
            "already owns the graph (see owner_pid) — this server will not start "
            "ingestion on its own; call minigraf_ingest_git again later to retry. "
            "For skipped only, a stale field may be present: stale=true means the "
            "owning process stopped refreshing its ownership hint, so a retry is "
            "likely to succeed now. error carries no stale field — it was derived "
            "by scraping a holder PID out of minigraf's lock-contention message, "
            "and minigraf 2.0.0 removed that PID from the text (#284) — but it "
            "does include error_at, the timestamp the failure occurred. "
            "this_run reports this run's own work: to_retire (positions in "
            "the gap at load) and retired (written + skipped + failed), which "
            "never exceeds to_retire; this_run.skipped climbing while the "
            "commit count stays flat means the run is replaying an "
            "already-ingested region (#326). streams gives forward, reverse "
            "and sweep state, counts and rate_per_min; sweep.blocked_reason "
            "says why the confirmation pass declined. status=complete means "
            "only that the run finished: ingestion is done when "
            "visibility.complete and lineage.complete are both true."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


@server.list_tools()
async def list_tools() -> List[Tool]:
    return _TOOLS


# Exactly the tools whose handler touches the graph. call_tool pre-acquires
# for these and only these: the acquisition must be ASYNC so the synchronous
# handler's own lease nests at count 1->2 and never runs blocking backoff on
# the event-loop thread (#99). minigraf_report_issue and minigraf_ingest_git
# are absent because they touch no graph here; memory_finalize_turn is absent
# because it takes its own lease internally, conditional on the extraction
# strategy (see handle_memory_finalize_turn).
_DB_LEASE_TOOLS = frozenset({
    "minigraf_query", "minigraf_transact", "minigraf_retract", "minigraf_rule",
    "memory_prepare_turn", "minigraf_audit",
})


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    needs_db = name in _DB_LEASE_TOOLS or (
        name == "minigraf_ingest_status" and _ingest_progress["status"] != "running"
    )
    async with contextlib.AsyncExitStack() as stack:
        if needs_db:
            # Acquired here, asynchronously, purely so the synchronous handler
            # below nests at count 1->2 and never runs blocking backoff on the
            # event loop (#99). The lease ends when this block does, which is
            # what lets the prepare_hook subprocess open the DB between turns
            # -- the job `finally: _db = None` used to do, badly.
            await stack.enter_async_context(db_lease_async())

        if name == "minigraf_query":
            result = handle_minigraf_query(arguments["datalog"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "minigraf_transact":
            result = handle_minigraf_transact(arguments["facts"], arguments["reason"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "minigraf_retract":
            result = handle_minigraf_retract(arguments["facts"], arguments["reason"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "minigraf_rule":
            result = handle_minigraf_rule(arguments["rule"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "minigraf_report_issue":
            result = handle_minigraf_report_issue(
                arguments["issue_type"],
                arguments["description"],
                datalog=arguments.get("datalog"),
                error=arguments.get("error"),
            )
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "memory_prepare_turn":
            block = handle_memory_prepare_turn(arguments["user_message"])
            return [TextContent(type="text", text=block)]

        if name == "memory_finalize_turn":
            result = await handle_memory_finalize_turn(arguments["conversation_delta"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "minigraf_audit":
            as_of = arguments.get("as_of")
            result = handle_minigraf_audit(as_of=as_of)
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "minigraf_ingest_git":
            result = await handle_minigraf_ingest_git(
                repo_path=arguments.get("repo_path"),
                branch=arguments.get("branch"),
            )
            return [TextContent(type="text", text=json.dumps(result))]


        if name == "minigraf_ingest_status":
            result = handle_minigraf_ingest_status()
            return [TextContent(type="text", text=json.dumps(result))]

        raise ValueError(f"Unknown tool: {name}")


async def _orphan_watchdog() -> None:
    """Detect the case where our immediate supervisor (`uvx`) dies without
    ever sending us a signal or closing stdin — we just get silently
    reparented to init/systemd instead. Neither of main()'s other shutdown
    triggers can see this, so poll os.getppid() against the PID recorded at
    launch and request the same graceful shutdown a real SIGTERM would.
    See #104."""
    while not _shutdown_requested.is_set():
        await asyncio.sleep(_ORPHAN_CHECK_INTERVAL)
        if os.getppid() != _launch_ppid:
            _shutdown_requested.set()
            return


async def main() -> None:
    global _server_ref, _ingest_task, _ingest_progress, _launch_ppid, _backfill_task
    _server_ref = server
    _launch_ppid = os.getppid()
    # Auto-start incremental ingest on server startup so ingestion begins
    # immediately without waiting for a user prompt.  Runs as a background
    # asyncio task — never blocks the message loop.
    # Set MINIGRAF_NO_AUTO_INGEST=1 to skip auto-start (used by eval sandboxes).
    _ingest_progress = {
        "status": "idle", "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
        "phase": None, "orphaned_commits": None,
    }
    if not os.environ.get("MINIGRAF_NO_AUTO_INGEST"):
        # Proactive check-before-attempt: if another live process already
        # owns the graph, don't start ingestion here at all rather than
        # racing for it and losing (#108).
        owner = _graph_owner_hint(_get_graph_path())
        holder_pid = owner.get("pid") if owner is not None else None
        if owner is not None:
            print(
                f"[ingestion] skipped: already owned by live pid {holder_pid}",
                file=sys.stderr,
            )
            _ingest_progress["status"] = "skipped"
            _ingest_progress["owner_pid"] = holder_pid
        else:
            _ingest_progress["status"] = "starting"
            cwd = str(Path.cwd())
            _ingest_task = asyncio.create_task(_run_ingestion(cwd, _default_git_branch(cwd)))

        # Eager fact-index backfill (#147): also gated on MINIGRAF_NO_AUTO_INGEST
        # since it's the same kind of background write to on-disk state that a
        # deterministic eval sandbox wants to opt out of, alongside ingestion.
        # Independent of the live-lock-holder check above -- unlike ingestion,
        # this doesn't race another process for the graph's write lock.
        _backfill_task = asyncio.create_task(_run_startup_backfill())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown_requested.set)
        except (NotImplementedError, AttributeError):
            pass  # Windows: add_signal_handler unsupported; no graceful-shutdown-by-signal there

    watchdog_task = asyncio.ensure_future(_orphan_watchdog())
    try:
        async with stdio_server() as (read_stream, write_stream):
            server_task = asyncio.ensure_future(
                server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )
            )
            shutdown_task = asyncio.ensure_future(_shutdown_requested.wait())
            done, _ = await asyncio.wait(
                {server_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if server_task in done:
                shutdown_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await shutdown_task
                server_task.result()  # propagate any exception from a normal exit
            else:
                server_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await server_task
    finally:
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task
        # The MCP server's most common "session ended" signal is stdin EOF
        # (the parent closing the pipe) rather than a delivered signal, so
        # this runs on every exit path. Give a long-running ingest a chance
        # to reach its next commit boundary and exit cleanly — leaving the
        # watermark correctly reflecting the last fully-completed commit —
        # instead of asyncio.run() abruptly cancelling it mid-write once
        # this coroutine returns.
        _shutdown_requested.set()
        if _ingest_task is not None and not _ingest_task.done():
            try:
                await asyncio.wait_for(_ingest_task, timeout=30)
            except asyncio.TimeoutError:
                _ingest_task.cancel()
        if _backfill_task is not None and not _backfill_task.done():
            try:
                await asyncio.wait_for(_backfill_task, timeout=30)
            except asyncio.TimeoutError:
                _backfill_task.cancel()


def run() -> None:
    """Sync entry point for the `temporal-reasoning` console script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()

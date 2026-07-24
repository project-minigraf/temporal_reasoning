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
import hashlib
import json
import multiprocessing
import os
import re
import signal
import subprocess as _subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from mcp.server import Server
from mcp.server.stdio import stdio_server
from minigraf import MiniGrafDb, MiniGrafError
import fact_index
import frontier_registry

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

# Module-level DB instance — opened once, held for the session lifetime
_db: Optional[MiniGrafDb] = None

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
_db_native_lock = threading.Lock()

# Track graph path and last-known mtime so we can detect external modifications.
# minigraf's Drop impl writes to the file even for read-only handles, which
# invalidates any other open handle's in-memory page table.  Reopening on
# mtime change is the workaround until the upstream bug is fixed.
_graph_path: str = ""
_db_mtime: float = 0.0

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

# Ingestion state
_ingest_task: Optional[asyncio.Task] = None
_ingest_progress: Dict[str, Any] = {
    "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
    "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
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


def _open_db_at(path: str, *, force: bool = True) -> MiniGrafDb:
    """Open MiniGrafDb at path, register session rules, update mtime tracking.

    force=False reuses an already-open _db instead of opening a second handle,
    checked atomically under _db_native_lock. Without this, two threads that
    both observe _db as None (e.g. the ingestion preload thread and an
    _ensure_db_async() caller racing during the "starting" phase, before
    _run_ingestion flips status to "running") each call MiniGrafDb.open() on
    the same path from this same process. The second open collides with the
    first handle's still-held lock file and surfaces as "Database is locked
    by another process", with the lock file's own PID equal to *our* PID
    (#107). force=True (the default) is for callers that need a genuine
    reopen regardless of any existing handle, e.g. _refresh_if_stale().
    """
    global _db, _graph_path, _db_mtime
    with _db_native_lock:
        if not force and _db is not None:
            return _db
        _db = MiniGrafDb.open(path)
    for rule in SESSION_RULES:
        _db_execute(_db, rule)
    for rule in _user_rules:
        _db_execute(_db, rule)
    _graph_path = path
    try:
        _db_mtime = os.path.getmtime(path)
    except OSError:
        _db_mtime = 0.0
    return _db


def open_db(graph_path: Optional[str] = None) -> MiniGrafDb:
    """Open MiniGrafDb and register session-scoped rules. Called once at startup."""
    return _open_db_at(graph_path or _get_graph_path())


def _update_mtime() -> None:
    """Record the graph file mtime after our own checkpoint so we don't
    treat our own write as an external modification on the next call."""
    global _db_mtime
    if not _graph_path:
        return
    try:
        _db_mtime = os.path.getmtime(_graph_path)
    except OSError:
        pass


def _refresh_if_stale() -> None:
    """Reopen the DB if the graph file was modified externally since last open.

    minigraf's Drop impl writes to the file even for read-only handles (upstream
    bug).  Any subprocess that opens the same file — including the prepare/finalize
    hooks — will change the mtime and invalidate this process's in-memory page
    table.  Detect this via mtime and reopen transparently.
    """
    global _db_mtime
    if not _graph_path:
        return
    try:
        current_mtime = os.path.getmtime(_graph_path)
    except OSError:
        return
    if current_mtime != _db_mtime:
        _open_db_at(_graph_path)


def _is_lock_error(exc: Exception) -> bool:
    return "locked" in str(exc).lower()


def _stale_lock_holder_pid(exc: Exception) -> Optional[int]:
    """Extract the holder PID from a minigraf lock-contention error message."""
    match = re.search(r"holder PID:\s*(\d+)", str(exc))
    return int(match.group(1)) if match else None


def _pid_is_alive(pid: int) -> bool:
    """Conservative liveness check: only a positive ProcessLookupError counts
    as dead. Uncertain cases (PermissionError, other OSError) are treated as
    alive rather than risking a false "safe to proceed" signal.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # PermissionError or other — can't confirm death, assume alive
    return True


def _read_lock_holder_raw(path: str) -> Optional[str]:
    """Read path's lock file and return its raw, unparsed contents, or None
    if the lock file doesn't exist. Shared by _clear_stale_lock and
    _live_lock_holder_pid, whose parsing/validation needs diverge past this
    point (#106, #178)."""
    try:
        with open(path + ".lock") as f:
            return f.read().strip()
    except OSError:
        return None  # no lock file


def _clear_stale_lock(path: str, holder_pid: int) -> bool:
    """Remove path's lock file if its recorded holder process is no longer alive.

    holder_pid is extracted from an earlier lock-contention error (at time
    T0); by the time this runs (T1), the lock file may have already changed
    hands to a different, live, legitimate holder. Re-reads the lock file's
    *current* contents immediately before deleting and only proceeds if it
    still names holder_pid, to avoid stripping a live holder of its lock
    protection (#178). This narrows but does not eliminate the underlying
    TOCTOU race -- a gap remains between this re-check and the os.remove.

    Returns True if a stale lock was removed.
    """
    if _pid_is_alive(holder_pid):
        return False  # holder still alive (or we lack permission to tell — leave it)
    current_holder = _read_lock_holder_raw(path)
    if current_holder is None:
        return False  # no lock file (already cleared by someone else)
    if current_holder != str(holder_pid):
        return False  # reclaimed by a different holder since T0 — not ours to clear
    try:
        os.remove(path + ".lock")
        return True
    except OSError:
        return False


def _live_lock_holder_pid(path: str) -> Optional[int]:
    """Return path's lock-file holder PID if that process is live and isn't
    us, else None.

    Reads the sidecar `.lock` file directly — never attempts to open the DB,
    so this check can never itself contend for the lock. Used as a
    proactive pre-check before starting ingestion, to avoid racing another
    live session for the same lock instead of losing that race (#108).

    Best-effort / racy by nature (the holder can appear or disappear
    between this check and the real open attempt) — existing retry/self-heal
    logic (_try_open_with_self_heal, _ensure_db_async) still runs as the
    fallback if the race is lost anyway.
    """
    holder = _read_lock_holder_raw(path)
    if holder is None:
        return None  # no lock file
    if not holder.isdigit():
        return None
    pid = int(holder)
    if pid == os.getpid():
        return None  # our own leaked handle, not another process
    return pid if _pid_is_alive(pid) else None


def _try_open_with_self_heal(path: str) -> MiniGrafDb:
    """Attempt one open, self-healing a stale lock (holder process no longer
    running) by removing it and retrying once, instead of surfacing a
    permanent error.

    Reuses an already-open _db instead of opening a redundant second handle
    (force=False) — see _open_db_at's docstring (#107).

    Raises the lock-contention exception if the lock is still held by a live
    process (caller decides whether to back off and retry); raises any
    non-lock exception immediately.
    """
    try:
        return _open_db_at(path, force=False)
    except Exception as e:
        if not _is_lock_error(e):
            raise
        holder_pid = _stale_lock_holder_pid(e)
        if holder_pid is not None and _clear_stale_lock(path, holder_pid):
            try:
                return _open_db_at(path, force=False)
            except Exception as e2:
                if not _is_lock_error(e2):
                    raise
                raise e2 from None
        raise


def _open_db_at_with_retry(path: str) -> MiniGrafDb:
    """Open MiniGrafDb at path, retrying with blocking backoff on lock contention.

    Only safe off the asyncio event-loop thread: the backoff uses
    time.sleep(), which would otherwise freeze the single-threaded event
    loop for the whole retry budget — see _ensure_db_async for the
    event-loop-safe equivalent (issue #99).
    """
    delay = _LOCK_RETRY_BASE
    last_exc: Optional[Exception] = None
    for attempt in range(_LOCK_RETRY_MAX):
        try:
            return _try_open_with_self_heal(path)
        except Exception as e:
            if not _is_lock_error(e):
                raise
            last_exc = e
            if attempt < _LOCK_RETRY_MAX - 1:
                time.sleep(delay)
                delay *= 2
    assert last_exc is not None
    raise last_exc


def _open_db_at_with_extended_retry(path: str) -> MiniGrafDb:
    """Open MiniGrafDb at path, retrying lock contention with a much longer
    time-budgeted backoff than _open_db_at_with_retry.

    Used only by _load_ingestion_preload_state, which runs on a dedicated
    worker thread (see issue #103) and can afford to wait out a typical
    orphan-process cleanup window instead of giving up after ~1.55s and
    leaving _run_ingestion permanently stuck in an "error" state (#106).
    Self-heals a dead holder's lock on every attempt via
    _try_open_with_self_heal, exactly like _open_db_at_with_retry.
    """
    deadline = time.monotonic() + _INGEST_LOCK_RETRY_BUDGET
    delay = _INGEST_LOCK_RETRY_BASE
    last_exc: Optional[Exception] = None
    while True:
        try:
            return _try_open_with_self_heal(path)
        except Exception as e:
            if not _is_lock_error(e):
                raise
            last_exc = e
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _INGEST_LOCK_RETRY_CAP)
    assert last_exc is not None
    raise last_exc


async def _ensure_db_async() -> MiniGrafDb:
    """Ensure the DB is open, retrying lock contention without blocking the
    event loop.

    Await this from any event-loop coroutine (call_tool, _run_ingestion)
    before code that will call the synchronous get_db() — once _db is
    populated, get_db() just returns it, so its own blocking retry path never
    runs on the event-loop thread. Backs off with asyncio.sleep instead of
    time.sleep so a lock held by another coroutine on this same event loop
    (e.g. ingestion mid-commit) gets a chance to be released during the wait,
    instead of the retry deterministically exhausting itself against a lock
    state its own blocking sleep prevented from changing (issue #99).
    """
    if _db is not None:
        return _db
    path = _graph_path or _get_graph_path()
    delay = _LOCK_RETRY_BASE
    last_exc: Optional[Exception] = None
    for attempt in range(_LOCK_RETRY_MAX):
        try:
            return _try_open_with_self_heal(path)
        except Exception as e:
            if not _is_lock_error(e):
                raise
            last_exc = e
            if attempt < _LOCK_RETRY_MAX - 1:
                await asyncio.sleep(delay)
                delay *= 2
    assert last_exc is not None
    raise last_exc


def get_db() -> MiniGrafDb:
    """Return the open DB instance, opening it if not currently held.

    The DB is opened per-operation and released after each call_tool() invocation
    so that the prepare_hook subprocess can acquire the file lock between turns.
    Opening retries with a blocking backoff on lock contention (see
    _open_db_at_with_retry) — safe here only because event-loop call sites
    (call_tool, _run_ingestion) always await _ensure_db_async() first, so _db
    is already populated by the time they reach this function.

    Reads the module-level _db global into a local exactly once. A
    background thread calling this function concurrently with call_tool()'s
    finally block resetting _db to None — reading the global a second time
    for the return would let that reset race in between the None-check and
    the return, yielding None even though _db was live at call time (issue
    #122; the original background caller that surfaced this, IndexCache's
    rebuild thread, was deleted in #118, but the exactly-once read remains a
    general defensive guarantee against any future background caller).
    """
    db = _db
    if db is None:
        db = _open_db_at_with_retry(_graph_path or _get_graph_path())
    return db


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
        _update_mtime()
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
    db = get_db()
    try:
        raw = _db_execute(db, f"(query {datalog})")
        return _parse_query_result(raw)
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


_TAGGED_LITERAL = r'#(?:uuid|inst)\s+"[^"\\]*"'
_FACTS_TRIPLE_PATTERN = re.compile(
    r'\[(\:[^\s\]]+|' + _TAGGED_LITERAL + r')\s+(\:[^\s\]]+)\s+'
    r'("(?:[^"\\]|\\.)*"|\:[^\s\]]+|-?\d+(?:\.\d+)?|' + _TAGGED_LITERAL + r')\]'
)
_TAGGED_LITERAL_PATTERN = re.compile(r'#(?:uuid|inst)\s+"([^"\\]*)"')


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
    capture keyword-valued and bare-numeric-valued triples, which schema
    validation intentionally skips but the index must not). Value is
    unquoted for string-valued triples, kept as-is (a keyword, number, or
    entity reference) otherwise. #uuid/#inst-tagged entity references and
    values are also captured, with the tag stripped and the raw UUID/
    timestamp text kept as the indexed entity/value (#177) -- this is not
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
        path = fact_index.index_path_for(_graph_path or _get_graph_path())
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
    backoff _open_db_at_with_retry uses (_LOCK_RETRY_MAX/_LOCK_RETRY_BASE) --
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
    # Schema validation — closed-world enforcement on parseable string-valued triples.
    # Only string-valued triples are schema-validated. Keyword-valued triples
    # (e.g. relationship edges like [:service/auth :calls :component/jwt]) are
    # not covered by MINIGRAF_SCHEMA and pass through unvalidated by design.
    parsed = _parse_transact_facts(facts)
    if parsed:
        violations = _validate_facts(parsed)
        if violations:
            return {"ok": False, "error": f"schema violations: {'; '.join(violations)}"}
    _refresh_if_stale()
    db = get_db()
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
    _refresh_if_stale()
    db = get_db()
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
    db = get_db()
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
    _refresh_if_stale()
    db = get_db()
    audited = 0
    retracted = 0
    all_violations: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    as_of_clause = f":as-of {as_of} " if as_of is not None else ""

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
                            _update_mtime()
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

_STOP_WORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would could should "
    "may might shall can need dare ought used to am i we you he she it they what which who "
    "this that these those my our your his her its their about above after all also and as at "
    "before but by for from if in into just me more most no not of on only or other our out "
    "same so than then there they through to too under up us very via was we what when where "
    "which while who why with".split()
)

_MIN_ENTITY_LEN = 4


def _canonical_ident(entity_type: str, value: str) -> str:
    """Slug-canonicalize a value into a Minigraf keyword ident.

    Lowercases, replaces any character outside [a-z0-9-] with a hyphen,
    collapses consecutive hyphens, strips leading/trailing hyphens.
    Ported from _to_kw() in minigraf-examples LlamaIndex integration.
    """
    slug = re.sub(r"[^a-z0-9-]", "-", value.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
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
    return triples


def _forget_closed_entity(
    ident: str,
    file_path: Optional[str],
    entity_valid_from: Dict[str, str],
    entity_descriptions: Dict[str, str],
    field_class_ident: Dict[str, str],
    file_entities: Dict[str, List[str]],
    field_static_ident: Optional[Dict[str, bool]] = None,
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
    """
    raw = _db_execute(db, "(query [:find (count ?e) :where [?e :entity-type :type/commit]])")
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

    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    intervals: List[frontier_registry.Interval] = []
    low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
    if low_bounds is not None and low_bounds[0] in hash_to_pos and low_bounds[1] in hash_to_pos:
        intervals.append(frontier_registry.Interval(
            hash_to_pos[low_bounds[0]], hash_to_pos[low_bounds[1]], frontier_registry.TAG_AUTHORITATIVE
        ))
    high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
    if high_bounds is not None and high_bounds[0] in hash_to_pos and high_bounds[1] in hash_to_pos:
        intervals.append(frontier_registry.Interval(
            hash_to_pos[high_bounds[0]], hash_to_pos[high_bounds[1]], frontier_registry.TAG_PROVISIONAL
        ))
    return frontier_registry.FrontierAllocator(len(linearization), intervals)


def _frontier_persist_claim(
    db: Any,
    linearization: List[str],
    pos: int,
    from_low: bool,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
) -> None:
    """Persist a single claimed position by extending the correct fixed-ident
    interval fact -- retracts+reasserts only the moved bound, mirroring
    _watermark_update's per-commit cost profile (see the design spec's
    "Persistence timing" and "Graph persistence schema" sections).
    """
    ident = _FRONTIER_LOW_IDENT if from_low else _FRONTIER_HIGH_IDENT
    tag = ":authoritative" if from_low else ":provisional"
    moved_hash = linearization[pos]
    existing = _frontier_read_bounds(db, ident)

    to_retract: List[str] = []
    to_transact: List[str] = []
    if existing is None:
        to_transact.append(f"[{ident} :entity-type :type/ingest-interval]")
        to_transact.append(f"[{ident} :tag {tag}]")
        to_transact.append(f'[{ident} :lo-hash "{_edn_escape(moved_hash)}"]')
        to_transact.append(f'[{ident} :hi-hash "{_edn_escape(moved_hash)}"]')
    else:
        lo_hash, hi_hash = existing
        if from_low:
            to_retract.append(f'[{ident} :hi-hash "{_edn_escape(hi_hash)}"]')
            to_transact.append(f'[{ident} :hi-hash "{_edn_escape(moved_hash)}"]')
        else:
            to_retract.append(f'[{ident} :lo-hash "{_edn_escape(lo_hash)}"]')
            to_transact.append(f'[{ident} :lo-hash "{_edn_escape(moved_hash)}"]')

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)


_LINEAGE_MARKER_ENTITY_TYPE = ":type/lineage-marker"


def _lineage_marker_ident(entity_ident: str) -> str:
    """Deterministic companion-entity ident for entity_ident's provisional
    marker. Not a public schema type -- see the #222 phase 2a design spec's
    "Schema/audit status of new entity types" section.
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
    public handler's schema gate would reject it outright.
    """
    if _lineage_is_provisional(db, entity_ident):
        return
    ident = _lineage_marker_ident(entity_ident)
    facts = [
        f"[{ident} :entity-type {_LINEAGE_MARKER_ENTITY_TYPE}]",
        f"[{ident} :entity {entity_ident}]",
        f"[{ident} :status :provisional]",
    ]
    _transact(db, "[" + " ".join(facts) + "]", commit_ts_iso, index_con=index_con)


def _lineage_confirm(db: Any, entity_ident: str, index_con: Optional[Any] = None) -> None:
    """Retract the :type/lineage-marker companion entity's facts for
    entity_ident if present; no-op if absent, so callers (2c) can call this
    unconditionally without checking first.
    """
    if not _lineage_is_provisional(db, entity_ident):
        return
    ident = _lineage_marker_ident(entity_ident)
    facts = [
        f"[{ident} :entity-type {_LINEAGE_MARKER_ENTITY_TYPE}]",
        f"[{ident} :entity {entity_ident}]",
        f"[{ident} :status :provisional]",
    ]
    _retract(db, "[" + " ".join(facts) + "]", index_con=index_con)


def _lineage_is_provisional(db: Any, entity_ident: str) -> bool:
    """True iff a :type/lineage-marker companion entity currently exists for
    entity_ident."""
    ident = _lineage_marker_ident(entity_ident)
    raw = _db_execute(db, f"(query [:find ?e :where [{ident} :entity ?e]])")
    return bool(json.loads(raw).get("results", []))


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
        "optional": {":hash": str, ":alias": str, ":last-run-at": str, ":last-commit": str, ":total-ingested": int},
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


def _format_facts(results: List[List[str]]) -> str:
    """Format fact-index rows as a readable block. Each row is
    [entity, attribute, value] (2-tuple attr/val rows from other callers) or
    [entity, attribute, value, valid_from, valid_to] (5-element fact-index
    rows). A historical row (valid_to present and non-None) is labeled with
    its validity window so the agent has the entity ident + window it needs
    to follow up with a precise :as-of/:valid-at Datalog query."""
    if not results:
        return ""
    lines = []
    for row in results:
        if len(row) == 5:
            entity, attribute, value, valid_from, valid_to = row
            base = f"  {entity} | {attribute} | {value}"
            if valid_to is not None:
                base += f"  [was valid {valid_from} → {valid_to}]"
            lines.append(base)
        else:
            lines.append("  " + " | ".join(str(v) for v in row))
    return "\n".join(lines)


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
    db = get_db()
    ident_raw = _db_execute(
        db, '(query [:find ?e ?ident :any-valid-time :where [?e :ident ?ident]])'
    )
    ident_map = {e: ident for e, ident in json.loads(ident_raw).get("results", [])}

    facts_raw = _db_execute(
        db,
        "(query [:find ?e ?a ?v ?vf ?vt :any-valid-time "
        ":where [?e ?a ?v] [?e :db/valid-from ?vf] [?e :db/valid-to ?vt]])",
    )
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
    path = fact_index.index_path_for(_graph_path or _get_graph_path())
    fact_index.rebuild_index(path, triples)


async def _run_startup_backfill() -> None:
    """Eagerly check-and-run the fact-index backfill from the long-lived
    server process at startup (#147), mirroring main()'s auto-start-ingestion
    pattern -- offloaded to a worker thread so the (potentially slow, full
    graph rescan) work never blocks the stdio handshake.

    Without this, backfill only ever ran lazily inside
    handle_memory_prepare_turn, which is very often invoked from the
    UserPromptSubmit hook's short-lived, 5-second-timeout-bound process: a
    slow rescan there trips the timeout and retry-storms on every subsequent
    turn instead of ever completing.

    Releases the graph's file lock (_db = None) once done, unconditionally --
    mirroring call_tool's own finally block -- so the prepare_hook subprocess
    can still acquire it between turns. Without this, a rebuild triggered
    here would leave the persistent server process holding the lock open
    indefinitely (never reset the way a single call_tool invocation is),
    reproducing this issue's own failure mode -- a hook unable to get the
    lock in time -- by lock contention instead of a slow rescan.
    """
    global _db
    path = fact_index.index_path_for(_graph_path or _get_graph_path())
    loop = asyncio.get_running_loop()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as backfill_executor:
            needs = await loop.run_in_executor(backfill_executor, fact_index.needs_backfill, path)
            if needs:
                await loop.run_in_executor(backfill_executor, _rebuild_index_from_graph)
    except Exception as e:
        print(f"[fact_index] startup backfill failed: {e}", file=sys.stderr)
    finally:
        _db = None


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
    build/fix/navigate-shaped messages, gated on ingestion being present.

    Returns a formatted context block string for injection as
    additionalContext, or an empty string if no relevant facts are found.
    Proactively checks fact_index.needs_backfill() before querying (fresh
    install, pre-existing graph, corruption recovery, or a write that raced
    ahead of the first read all leave the index in a needs-backfill state --
    see the 2026-07-18 design doc for why file-existence alone is not a
    reliable signal).
    """
    scan_limit = int(os.environ.get("MINIGRAF_PREPARE_SCAN_LIMIT", "50"))
    boost = float(os.environ.get("MINIGRAF_MEMORY_BOOST", "2.0"))
    historical_discount = float(os.environ.get("MINIGRAF_HISTORICAL_DISCOUNT", "0.5"))
    path = fact_index.index_path_for(_graph_path or _get_graph_path())
    memory_block = ""
    try:
        if fact_index.needs_backfill(path):
            _rebuild_index_from_graph()
        results = fact_index.query_facts(
            path, user_message, top_n=scan_limit, boost=boost,
            historical_discount=historical_discount,
        )
        if results:
            memory_block = f"Relevant memory context:\n{_format_facts(results)}"
    except Exception as e:
        print(f"[fact_index] prepare_turn failed: {e}", file=sys.stderr)

    nav_nudge = ""
    if _looks_like_navigation_task(user_message):
        try:
            if _count_commit_entities(get_db()) > 0:
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
    _refresh_if_stale()
    db = get_db()
    stored = 0

    entity_groups: Dict[str, List[Dict[str, Any]]] = {}
    for fact in facts:
        entity_groups.setdefault(fact["entity"], []).append(fact)
    invalid_entities = {
        entity for entity, group in entity_groups.items() if _validate_facts(group)
    }

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
        _update_mtime()
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
        _refresh_if_stale()
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
    if strategy in ("heuristic", "llm", "agent"):
        await _ensure_db_async()

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
            entity_descriptions[gvar_ident] = gvar_name
        elif gvar_ident not in unchanged_idents:
            triples.append(f"[{gvar_ident} :modified-in {commit_ident}]")

    for field_ident, field_name, candidate_triples in precomputed["field_entries"]:
        if field_ident not in entity_valid_from:
            triples += candidate_triples
            if field_ident not in idents_for_file:
                idents_for_file.append(field_ident)
            entity_valid_from[field_ident] = commit_ts_iso
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


def _preload_known_entities(db: Any, repo_path: str) -> tuple:
    """Load all existing module/function/class/external-dependency idents from
    the DB, and pre-seed file_entities with all currently tracked files in the
    repo.

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

    Returns (entity_valid_from, entity_descriptions, file_entities, submodule_paths).
    entity_valid_from maps ident → git commit timestamp of first introduction.
    entity_descriptions maps ident → human-readable name (function/class/file).
    submodule_paths maps external-dependency (submodule) ident → its :path,
    used by the gitlink "add"/stub-creation linking in #112's fix to tell a
    real submodule ident apart from an unresolved-import stub ident (which
    never has a :path).
    """
    entity_valid_from: Dict[str, str] = {}
    entity_descriptions: Dict[str, str] = {}
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

    for entity_type in ("module", "function", "class", "variable", "field", "external-dependency"):
        path_attr = "path" if entity_type in ("module", "external-dependency") else "file"
        try:
            raw = _db_execute(
                db,
                f'(query [:find ?ident ?path ?desc ?date '
                f':where [?e :entity-type :type/{entity_type}] '
                f'[?e :ident ?ident] '
                f'[?e :{path_attr} ?path] '
                f'[?e :description ?desc] '
                f'[?e :introduced-by ?c] '
                f'[?c :date ?date]])',
            )
            rows = json.loads(raw).get("results", [])
            for ident, path, desc, date in rows:
                entity_valid_from[ident] = date
                entity_descriptions[ident] = desc
                file_entities.setdefault(path, [])
                if ident not in file_entities[path]:
                    file_entities[path].append(ident)
                if entity_type == "external-dependency":
                    submodule_paths[ident] = path
        except Exception:
            pass

    return entity_valid_from, entity_descriptions, file_entities, submodule_paths


def _preload_unresolved_dep_idents(db: Any, submodule_paths: Dict[str, str]) -> Dict[str, str]:
    """Reload ident -> import_name for every unresolved-import stub (#112).

    _preload_known_entities' external-dependency branch requires a :path fact,
    which only real submodule entities have — unresolved-import stubs (see
    _run_ingestion's dep-edge handling) never get one, so they're invisible to
    that query. This runs the same :entity-type match WITHOUT the :path
    requirement, then subtracts submodule_paths' idents (already known
    submodules) to get exactly the stub idents, restart-safe.

    Needed so a submodule added in a later, separate ingestion run can still
    find and link any stub created in an earlier run (see the gitlink "add"
    handling in _run_ingestion) — without this, only same-run stubs would
    ever get linked.
    """
    unresolved: Dict[str, str] = {}
    try:
        raw = _db_execute(
            db,
            "(query [:find ?ident ?desc :where "
            "[?e :entity-type :type/external-dependency] "
            "[?e :ident ?ident] "
            "[?e :description ?desc]])",
        )
        for ident, desc in json.loads(raw).get("results", []):
            if ident not in submodule_paths:
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


def _preload_known_deps(
    db: Any, file_entities: Dict[str, List[str]]
) -> tuple:
    """Reload file_deps/dep_valid_from from durable :depends-on facts.

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
            "(query [:find ?srci ?dep ?vf "
            ":any-valid-time "
            ":where [?src :ident ?srci] "
            "[?src :depends-on ?dep] "
            "[?src :db/valid-from ?vf] "
            "[?src :db/valid-to ?vt] "
            f"[(= ?vt {_VALID_TIME_FOREVER_MS})]])"
        )
        rows = json.loads(raw).get("results", [])
    except Exception:
        return file_deps, dep_valid_from

    for src_ident, dep_ident, vf_ms in rows:
        file_path = ident_to_file.get(src_ident)
        if file_path is None:
            continue
        vf_iso = (
            datetime.datetime.fromtimestamp(vf_ms / 1000, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        file_deps.setdefault(file_path, set()).add(dep_ident)
        dep_valid_from[(src_ident, dep_ident)] = vf_iso

    return file_deps, dep_valid_from


def _preload_pinned_commits(db: Any) -> Dict[str, tuple]:
    """Reload each external-dependency entity's current :pinned-commit value
    and the timestamp it was set at, mirroring _preload_known_deps's per-fact
    :any-valid-time pattern for :depends-on.

    Needed because :pinned-commit is bi-temporally closed and reopened on
    every bump (see _run_ingestion's gitlink handling) — without this, the
    server would lose track of the prior SHA and valid-from across a restart,
    corrupting the close on the next bump or removal exactly the way
    _preload_known_deps' docstring describes for :depends-on.

    Returns {ident: (sha, valid_from_iso)}.
    """
    pinned: Dict[str, tuple] = {}
    try:
        # Bind the entity's :ident object, not the bare ?e subject variable —
        # same UUID-vs-ident pitfall _preload_known_deps guards against.
        # [?e :ident ?ei] must precede [?e :pinned-commit ?sha] so that the
        # :db/valid-from/:db/valid-to pseudo-attributes (which bind to
        # whichever EAV clause on ?e most recently precedes them) continue
        # to bind to the :pinned-commit fact, not the :ident fact.
        raw = _db_execute(
            db,
            "(query [:find ?ei ?sha ?vf "
            ":any-valid-time "
            ":where [?e :ident ?ei] "
            "[?e :pinned-commit ?sha] "
            "[?e :db/valid-from ?vf] "
            "[?e :db/valid-to ?vt] "
            f"[(= ?vt {_VALID_TIME_FOREVER_MS})]])"
        )
        rows = json.loads(raw).get("results", [])
    except Exception:
        return pinned
    for ident, sha, vf_ms in rows:
        vf_iso = (
            datetime.datetime.fromtimestamp(vf_ms / 1000, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        pinned[ident] = (sha, vf_iso)
    return pinned


def _load_ingestion_preload_state(repo_path: str) -> tuple:
    """Open the DB and run every startup preload query for _run_ingestion.

    Executed via run_in_executor on a worker thread (see _run_ingestion), not
    inline on the event loop: opening/mmapping a graph file plus these preload
    queries contain no internal awaits, so running them directly on the event
    loop thread starves the stdio handshake for as long as they take — on a
    large enough graph, longer than a client's connection timeout (issue #103).
    Uses _open_db_at_with_extended_retry's much longer blocking lock-retry
    (rather than get_db()'s ~1.55s budget or _ensure_db_async()'s
    event-loop-safe variant) precisely because this runs off that thread and
    can afford to wait out a typical orphan cleanup window instead of
    entering a permanent "error" state (#106). Mirrors get_db()'s
    "reuse the already-open handle" short-circuit rather than reopening
    unconditionally.
    """
    db = _db if _db is not None else _open_db_at_with_extended_retry(_graph_path or _get_graph_path())
    watermark = _watermark_query(db)
    prior_ingested = _count_commit_entities(db)
    entity_valid_from, entity_descriptions, file_entities, submodule_paths = _preload_known_entities(db, repo_path)
    file_deps, dep_valid_from = _preload_known_deps(db, file_entities)
    pinned_commit_state = _preload_pinned_commits(db)
    field_class_ident = _preload_field_class_idents(db)
    field_static_ident = _preload_field_static_idents(db)
    unresolved_dep_idents = _preload_unresolved_dep_idents(db, submodule_paths)
    return (
        watermark, prior_ingested, entity_valid_from, entity_descriptions,
        file_entities, file_deps, dep_valid_from, pinned_commit_state,
        field_class_ident, field_static_ident, submodule_paths, unresolved_dep_idents,
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
    global _db, _ingest_progress
    # Safe to clear unconditionally: handle_minigraf_ingest_git refuses to start a
    # new run while one is already active, so no in-flight shutdown signal is ever
    # stomped on here; main()'s finally block re-sets the flag on exit regardless,
    # so a shutdown request arriving between runs is never silently lost.
    _shutdown_requested.clear()
    try:
        # Read watermark and pre-load known entities/deps before releasing DB.
        # Off-loaded to a worker thread (see _load_ingestion_preload_state)
        # so this potentially slow phase never blocks the event loop from
        # servicing the stdio handshake concurrently (issue #103).
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as preload_executor:
            (
                watermark, prior_ingested, entity_valid_from, entity_descriptions,
                file_entities, file_deps, dep_valid_from, pinned_commit_state,
                field_class_ident, field_static_ident, submodule_paths, unresolved_dep_idents,
            ) = await loop.run_in_executor(preload_executor, _load_ingestion_preload_state, repo_path)
        # minigraf exposes no explicit close(): the file lock is only released once
        # every reference to the handle is gone — the worker thread's own `db`
        # local already went out of scope when it returned above, so clearing
        # the global here is enough to release the lock.
        _db = None  # release file lock while enumerating commits

        commits = _git_commits(repo_path, watermark, branch)
        ignore_patterns = _load_ignore_patterns(repo_path)
        repo_total_result = _subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        repo_total = int(repo_total_result.stdout.strip()) if repo_total_result.returncode == 0 else len(commits)
        _ingest_progress["total"] = repo_total
        _ingest_progress["status"] = "running"
        _ingest_progress["processed"] = prior_ingested
        _ingest_progress["prior_ingested"] = prior_ingested

        last_hash = watermark or ""

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
        # one commit's write section ever holds _db/db at once. Also reused below
        # (#116) to run the extraction ProcessPoolExecutor's blocking shutdown()
        # off the event-loop thread — that reuse is only safe because this pool
        # isn't shut down itself until the outer `finally` further down, after
        # the extraction pool's own shutdown has already been submitted to it.
        write_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        # Single fact-index write connection for the whole ingestion run,
        # opened on write_executor's one worker thread and never touched from
        # any other thread thereafter (sqlite3 connections are thread-affine
        # by default) -- every use below is itself routed through
        # write_executor, so all access stays on the thread that created it.
        # Committed once per source-commit (matching the existing
        # _db_checkpoint(db) cadence just below), not once per triple: large
        # repositories can cross 1M facts well before ingestion completes,
        # and per-triple commits would be dominated by fsync overhead at
        # that scale.
        index_path = fact_index.index_path_for(_graph_path or _get_graph_path())
        index_con = await loop.run_in_executor(write_executor, _open_index_writer_safe, index_path)

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
                commits_iter = iter(commits)
                pending: Any = deque()

                def submit_next() -> bool:
                    try:
                        commit = next(commits_iter)
                    except StopIteration:
                        return False
                    fut = loop.run_in_executor(
                        executor, _extract_commit, repo_path, commit[0], ignore_patterns
                    )
                    pending.append((commit, fut))
                    return True

                for _ in range(pipeline_depth):
                    if not submit_next():
                        break

                while pending:
                    if _shutdown_requested.is_set():
                        completed_all = False
                        break

                    (commit_hash, commit_ts_iso, author, subject), fut = pending.popleft()
                    # renamed_pairs (Task 9's 4th _extract_commit return element) is
                    # unpacked here but not yet consumed — Task 10 wires it into
                    # :renamed-from/:renamed-to triple emission for functions/classes.
                    # Widening this unpack now (rather than leaving it a 3-tuple) is
                    # required as soon as _extract_commit returns 4 elements: every
                    # ingestion run — including the many existing tests that drive
                    # _run_ingestion through a real ProcessPoolExecutor worker — would
                    # otherwise fail with "too many values to unpack".
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
                        submit_next()
                        _ingest_progress["current_commit"] = commit_hash
                        _ingest_progress["processed"] += 1
                        await asyncio.sleep(0)  # yield to event loop
                        continue
                    submit_next()

                    last_hash = commit_hash
                    _ingest_progress["current_commit"] = commit_hash
                    reason = f"git:{commit_hash} {author}: {subject}"

                    # Build commit entity ident from first 12 chars of hash
                    commit_ident = f":commit/{commit_hash[:12]}"

                    # Acquire DB fresh each commit — never hold across yield
                    db = await _ensure_db_async()
                    try:
                        add_triples: List[str] = [
                            f"[{commit_ident} :entity-type :type/commit]",
                            f'[{commit_ident} :ident "{commit_ident}"]',
                            f'[{commit_ident} :description "{_edn_escape(subject[:120])}"]',
                            f'[{commit_ident} :hash "{commit_hash}"]',
                            f'[{commit_ident} :author "{_edn_escape(author)}"]',
                            f'[{commit_ident} :subject "{_edn_escape(subject[:200])}"]',
                            f'[{commit_ident} :date "{commit_ts_iso}"]',
                        ]
                        close_items: List[tuple] = []  # (triples, original_ts_iso)
                        dep_add_triples: List[str] = []  # :depends-on triples to transact individually
                        # Old paths of files renamed this commit (R status). Their
                        # unmatched child entities / dependency edges are closed in a
                        # final pass after renamed_pairs is consumed (see below).
                        renamed_old_paths: set = set()

                        for status, file_path, extracted, precomputed, old_path in extracted_files:
                            if status == "D":
                                # Close module and all known child entities for this file.
                                # Iterate a copy: _forget_closed_entity mutates
                                # file_entities[file_path] in place as it purges.
                                idents = list(file_entities.get(file_path, [_code_ident("module", file_path)]))
                                module_ident = _code_ident("module", file_path)
                                for ident in idents:
                                    orig_ts = entity_valid_from.get(ident, commit_ts_iso)
                                    desc = entity_descriptions.get(ident, "")
                                    close_items.append(
                                        (_build_close_triples(
                                            ident, desc, module_ident,
                                            field_class_ident.get(ident),
                                            close_entity_type=True, file_value=file_path,
                                            is_static=field_static_ident.get(ident),
                                        ), orig_ts)
                                    )
                                    _forget_closed_entity(
                                        ident, file_path, entity_valid_from,
                                        entity_descriptions, field_class_ident, file_entities,
                                        field_static_ident,
                                    )
                                # Whole file is gone: drop its (now-empty) file_entities key
                                # so nothing stale lingers under this path (matches file_deps).
                                file_entities.pop(file_path, None)
                                # Close all :depends-on edges for the deleted module
                                for dep_ident in file_deps.get(file_path, set()):
                                    orig_ts = dep_valid_from.get((module_ident, dep_ident), commit_ts_iso)
                                    close_items.append(
                                        ([f"[{module_ident} :depends-on {dep_ident}]"], orig_ts)
                                    )
                                file_deps.pop(file_path, None)
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
                                    old_desc = entity_descriptions.get(old_module_ident, old_path)
                                    orig_ts = entity_valid_from.get(old_module_ident, commit_ts_iso)
                                    close_items.append((
                                        _build_close_triples(
                                            old_module_ident, old_desc, old_module_ident,
                                            close_entity_type=True, file_value=old_path,
                                        ),
                                        orig_ts,
                                    ))
                                    # Purge the closed old module. Its remaining child
                                    # entities under old_path are closed+purged by the
                                    # renamed_old_paths pass below, which also pops the
                                    # whole file_entities[old_path] key — so only the
                                    # scalar dicts and the module's own list slot need
                                    # dropping here.
                                    _forget_closed_entity(
                                        old_module_ident, old_path, entity_valid_from,
                                        entity_descriptions, field_class_ident, file_entities,
                                        field_static_ident,
                                    )
                                previous_idents = set(file_entities.get(file_path, []))
                                triples = _build_code_triples(
                                    file_path, extracted, commit_ts_iso, entity_valid_from,
                                    entity_descriptions, file_entities, commit_ident, precomputed,
                                    field_class_ident, field_static_ident,
                                )
                                add_triples.extend(triples)
                                # Detect entities removed from a modified file.
                                # _build_code_triples only appends to file_entities, never removes.
                                # Compare previous idents against the idents derivable from the
                                # current extraction to find what was deleted.
                                if status == "M":
                                    module_ident = _code_ident("module", file_path)
                                    current_extracted_idents: set = {module_ident}
                                    for fn_ident, _fn_name, _fn_triples in precomputed["function_entries"]:
                                        current_extracted_idents.add(fn_ident)
                                    for cls_ident, _cls_name, _cls_triples in precomputed["class_entries"]:
                                        current_extracted_idents.add(cls_ident)
                                    # Globals and fields are tracked in file_entities too
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
                                        orig_ts = entity_valid_from.get(ident, commit_ts_iso)
                                        desc = entity_descriptions.get(ident, "")
                                        close_items.append(
                                            (_build_close_triples(
                                                ident, desc, module_ident,
                                                field_class_ident.get(ident),
                                                close_entity_type=True, file_value=file_path,
                                                is_static=field_static_ident.get(ident),
                                            ), orig_ts)
                                        )
                                        # File survives (M), only this child was removed:
                                        # purge just this ident from the file's list.
                                        _forget_closed_entity(
                                            ident, file_path, entity_valid_from,
                                            entity_descriptions, field_class_ident, file_entities,
                                            field_static_ident,
                                        )
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
                                        if not is_resolved and not is_relative and dep_ident not in entity_valid_from:
                                            add_triples.extend([
                                                f"[{dep_ident} :entity-type :type/external-dependency]",
                                                f'[{dep_ident} :ident "{_edn_escape(dep_ident)}"]',
                                                f'[{dep_ident} :description "{_edn_escape(import_name)}"]',
                                            ])
                                            entity_valid_from[dep_ident] = commit_ts_iso
                                            entity_descriptions[dep_ident] = import_name
                                            unresolved_dep_idents[dep_ident] = import_name
                                            # #112: an already-known submodule may be the real
                                            # target this unresolvable import was reaching for
                                            # (submodule directories are never in file_entities,
                                            # so any import into one always falls through here).
                                            for sub_ident, sub_path in submodule_paths.items():
                                                if _submodule_path_matches_import(sub_path, import_name):
                                                    add_triples.append(f"[{dep_ident} :resolves-to {sub_ident}]")
                                previous_deps = file_deps.get(file_path, set())
                                for dep_ident in current_deps - previous_deps:
                                    dep_add_triples.append(f"[{module_ident} :depends-on {dep_ident}]")
                                    dep_valid_from[(module_ident, dep_ident)] = commit_ts_iso
                                if status == "M":
                                    for dep_ident in previous_deps - current_deps:
                                        orig_ts = dep_valid_from.get((module_ident, dep_ident), commit_ts_iso)
                                        close_items.append(
                                            ([f"[{module_ident} :depends-on {dep_ident}]"], orig_ts)
                                        )
                                file_deps[file_path] = current_deps

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
                            old_desc = entity_descriptions.get(old_ident, old_name)
                            old_module_ident = _code_ident("module", old_file)
                            orig_ts = entity_valid_from.get(old_ident, commit_ts_iso)
                            close_items.append((
                                _build_close_triples(
                                    old_ident, old_desc, old_module_ident,
                                    field_class_ident.get(old_ident),
                                    close_entity_type=True, file_value=old_file,
                                    is_static=field_static_ident.get(old_ident),
                                ),
                                orig_ts,
                            ))
                            _forget_closed_entity(
                                old_ident, old_file, entity_valid_from,
                                entity_descriptions, field_class_ident, file_entities,
                                field_static_ident,
                            )

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
                                # file_entities[r_old_path] in place as it purges.
                                for ident in list(file_entities.get(r_old_path, [])):
                                    if ident == r_old_module_ident:
                                        continue  # already closed+purged by the R block above
                                    if ident in renamed_covered_idents:
                                        continue  # already closed+purged with :renamed-to linkage
                                    orig_ts = entity_valid_from.get(ident, commit_ts_iso)
                                    desc = entity_descriptions.get(ident, "")
                                    close_items.append(
                                        (_build_close_triples(
                                            ident, desc, r_old_module_ident,
                                            field_class_ident.get(ident),
                                            close_entity_type=True, file_value=r_old_path,
                                            is_static=field_static_ident.get(ident),
                                        ), orig_ts)
                                    )
                                    _forget_closed_entity(
                                        ident, r_old_path, entity_valid_from,
                                        entity_descriptions, field_class_ident, file_entities,
                                        field_static_ident,
                                    )
                                # Whole old path is gone (renamed away): drop the key so
                                # no stale ident lingers to be re-discovered by a later
                                # commit that reuses this path (e.g. a shim at old_path).
                                file_entities.pop(r_old_path, None)
                                for dep_ident in file_deps.get(r_old_path, set()):
                                    orig_ts = dep_valid_from.get((r_old_module_ident, dep_ident), commit_ts_iso)
                                    close_items.append(
                                        ([f"[{r_old_module_ident} :depends-on {dep_ident}]"], orig_ts)
                                    )
                                file_deps.pop(r_old_path, None)

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
                                entity_valid_from[ext_ident] = commit_ts_iso
                                entity_descriptions[ext_ident] = description
                                pinned_commit_state[ext_ident] = (sha, commit_ts_iso)
                                submodule_paths[ext_ident] = path
                                # #112: link any pre-existing unresolved-import stub whose
                                # import path reaches into this submodule — the ordering in
                                # the issue's own repro (stub created before the submodule
                                # was ever added), which the per-import check above can't
                                # catch since the submodule wasn't known yet at that time.
                                for stub_ident, import_name in unresolved_dep_idents.items():
                                    if _submodule_path_matches_import(path, import_name):
                                        add_triples.append(f"[{stub_ident} :resolves-to {ext_ident}]")
                            elif kind == "bump":
                                old_sha, orig_ts = pinned_commit_state.get(ext_ident, (None, commit_ts_iso))
                                if old_sha is not None:
                                    close_items.append(
                                        ([f'[{ext_ident} :pinned-commit "{_edn_escape(old_sha)}"]'], orig_ts)
                                    )
                                add_triples.append(f'[{ext_ident} :pinned-commit "{_edn_escape(sha)}"]')
                                add_triples.append(f"[{ext_ident} :modified-in {commit_ident}]")
                                pinned_commit_state[ext_ident] = (sha, commit_ts_iso)
                            else:  # "remove"
                                orig_ts = entity_valid_from.get(ext_ident, commit_ts_iso)
                                desc = entity_descriptions.get(ext_ident, "")
                                close_items.append(
                                    (_build_close_triples(
                                        ext_ident, desc, ext_ident,
                                        entity_type_kw=":type/external-dependency",
                                        file_value=path,
                                    ), orig_ts)
                                )
                                # Submodule removed: purge lifecycle state so a later
                                # re-add at the same path is treated as genuinely new.
                                # (Submodule paths aren't tracked in file_entities, so the
                                # path arg is a no-op there, but pass it for consistency.)
                                _forget_closed_entity(
                                    ext_ident, path, entity_valid_from,
                                    entity_descriptions, field_class_ident, file_entities,
                                )
                                old_sha, pin_orig_ts = pinned_commit_state.pop(ext_ident, (None, commit_ts_iso))
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
                        await loop.run_in_executor(
                            write_executor, _ingest_transact, db, other_triples, commit_ts_iso, reason, index_con
                        )
                        for ct in contains_triples:
                            await loop.run_in_executor(
                                write_executor, _ingest_transact, db, [ct], commit_ts_iso, reason, index_con
                            )
                        # :depends-on triples transacted individually — same EAVT collision risk
                        # as :contains when multiple deps share the same source module
                        for dt in dep_add_triples:
                            await loop.run_in_executor(
                                write_executor, _ingest_transact, db, [dt], commit_ts_iso, reason, index_con
                            )
                        for close_triples, orig_ts in close_items:
                            await loop.run_in_executor(
                                write_executor, _ingest_close, db, close_triples, orig_ts, commit_ts_iso, reason, index_con
                            )

                        # Ingest :parent edges — one transact per parent to avoid EAVT
                        # collision for merge commits (which have two parent hashes).
                        # Routed through _transact (not a raw _db_execute call) so the
                        # edge also lands in the persisted fact index -- see #118 review
                        # finding: this call site used to build its own raw (transact
                        # ...) string and bypass the index choke point entirely.
                        try:
                            for parent_hash in _git_parent_hashes(repo_path, commit_hash):
                                parent_ident = f":commit/{parent_hash[:12]}"
                                await loop.run_in_executor(
                                    write_executor,
                                    _transact,
                                    db,
                                    f'[[{commit_ident} :parent {parent_ident}]]',
                                    commit_ts_iso,
                                    None,
                                    None,
                                    index_con,
                                )
                        except Exception:
                            pass  # non-fatal; parent edges are best-effort

                        await loop.run_in_executor(write_executor, _watermark_update, db, commit_hash, commit_ts_iso, reason, index_con)
                        await loop.run_in_executor(write_executor, _db_checkpoint, db)
                        await loop.run_in_executor(write_executor, _commit_index_writer_safe, index_con)

                    except Exception as e:
                        # Ordinary per-commit write failure (malformed EDN, a
                        # transient constraint violation, ...) -- isolate it to
                        # this one commit rather than aborting every commit
                        # still pending, matching this function's own
                        # documented "fail only the one commit" contract and
                        # the extraction-phase isolation above. Opening the DB
                        # itself (_ensure_db_async, just above this try) is
                        # deliberately NOT covered here -- that failure is
                        # unrecoverable for every remaining commit too, so it
                        # still propagates to the outer handler.
                        print(
                            f"[_run_ingestion] skipping commit {commit_hash} "
                            f"({subject!r}): write failed: {e}",
                            file=sys.stderr,
                        )
                    finally:
                        _db = None  # release file lock between commits
                        db = None   # drop local reference too — see note above

                    _ingest_progress["processed"] += 1
                    await asyncio.sleep(0)  # yield to event loop

                # Call _ingest_tags and _last_run_write before closing index_con
                # so they use the batched connection instead of opening new ones
                if completed_all:
                    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                    db = await _ensure_db_async()
                    try:
                        await loop.run_in_executor(write_executor, _ingest_tags, db, repo_path, now, index_con)
                        await loop.run_in_executor(
                            write_executor, _last_run_write, db, last_hash, now, _ingest_progress["processed"], index_con
                        )
                        await loop.run_in_executor(write_executor, _db_checkpoint, db)
                    finally:
                        _db = None
            finally:
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

            if completed_all:
                _ingest_progress["status"] = "complete"
            else:
                _ingest_progress["status"] = "stopped"
        finally:
            write_executor.shutdown(wait=True)

    except Exception as e:
        # write_executor is already shut down by the inner finally above by the
        # time we get here (it runs on any exit from that try, including this
        # exception propagating through it) — nothing left to clean up.
        _ingest_progress["status"] = "error"
        _ingest_progress["error"] = str(e)
        _ingest_progress["error_at"] = _now_utc_ms()
        _db = None


async def handle_minigraf_ingest_git(
    repo_path: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Start background git ingestion. Returns immediately."""
    global _ingest_task, _ingest_progress
    if _ingest_task and not _ingest_task.done():
        return {"ok": False, "error": "ingestion already in progress"}
    # Proactive check-before-attempt: if another live process already owns
    # the graph lock, don't start ingestion here rather than racing for it
    # and losing (#108).
    holder_pid = _live_lock_holder_pid(_graph_path or _get_graph_path())
    if holder_pid is not None:
        _ingest_progress["status"] = "skipped"
        _ingest_progress["owner_pid"] = holder_pid
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
        "status": "starting", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    }
    _ingest_task = asyncio.create_task(_run_ingestion(repo, branch or _default_git_branch(repo)))
    return {"ok": True, "job_id": "git-ingest", "message": f"Ingestion started for {repo}"}


def handle_minigraf_ingest_status() -> Dict[str, Any]:
    """Return current ingestion progress, augmented with graph-backed last-run info."""
    result: Dict[str, Any] = {"ok": True, **_ingest_progress}
    # processed_this_run is derived in-memory (no extra DB query) so it stays
    # accurate even mid-run, distinguishing "this attempt's progress" from the
    # cumulative total in `processed` — see issue #85.
    result["processed_this_run"] = (
        _ingest_progress["processed"] - _ingest_progress.get("prior_ingested", 0)
    )
    # Staleness: a terminal error/skipped state can outlive the condition
    # that caused it (e.g. the orphaned holder it names has since died) —
    # re-check liveness on every poll instead of echoing a dead PID forever.
    # Purely informational: never auto-retries ingestion (#106).
    if _ingest_progress["status"] == "error":
        holder_pid = _stale_lock_holder_pid(_ingest_progress.get("error") or "")
        if holder_pid is not None:
            result["stale"] = not _pid_is_alive(holder_pid)
    elif _ingest_progress["status"] == "skipped":
        owner_pid = _ingest_progress.get("owner_pid")
        if owner_pid is not None:
            result["stale"] = not _pid_is_alive(owner_pid)
    if _ingest_progress["status"] != "running":
        try:
            db = get_db()
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
        except Exception:
            result["last_run_at"] = None
            result["last_commit"] = None
            result["total_ingested"] = None
    return result


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

from mcp.types import Tool, TextContent  # noqa: E402

server = Server("temporal-reasoning")

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
                        ':decision/description "use Redis"]]'
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
            "Return the current git ingestion progress. "
            "status is one of: idle, running, complete, error, skipped. "
            "skipped means another live process already owns the graph lock "
            "(see owner_pid) — this server will not start ingestion on its own; "
            "call minigraf_ingest_git again later if you want to retry. "
            "For error and skipped, a stale field may be present: stale=true means "
            "the condition that caused this state (the cited or owning PID) is no "
            "longer alive, so a minigraf_ingest_git retry is likely to succeed now; "
            "error also includes error_at, the timestamp the failure occurred."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


@server.list_tools()
async def list_tools() -> List[Tool]:
    return _TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    global _db
    try:
        if name == "minigraf_query":
            await _ensure_db_async()
            result = handle_minigraf_query(arguments["datalog"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "minigraf_transact":
            await _ensure_db_async()
            result = handle_minigraf_transact(arguments["facts"], arguments["reason"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "minigraf_retract":
            await _ensure_db_async()
            result = handle_minigraf_retract(arguments["facts"], arguments["reason"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "minigraf_rule":
            await _ensure_db_async()
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
            await _ensure_db_async()
            block = handle_memory_prepare_turn(arguments["user_message"])
            return [TextContent(type="text", text=block)]

        if name == "memory_finalize_turn":
            result = await handle_memory_finalize_turn(arguments["conversation_delta"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "minigraf_audit":
            await _ensure_db_async()
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
            if _ingest_progress["status"] != "running":
                await _ensure_db_async()
            result = handle_minigraf_ingest_status()
            return [TextContent(type="text", text=json.dumps(result))]

        raise ValueError(f"Unknown tool: {name}")
    finally:
        # Release the file lock after every tool call so that the prepare_hook
        # subprocess can open the DB between turns. get_db() re-opens on demand.
        _db = None


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
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    }
    if not os.environ.get("MINIGRAF_NO_AUTO_INGEST"):
        # Proactive check-before-attempt: if another live process already
        # owns the graph lock, don't start ingestion here at all rather
        # than racing for it and losing (#108).
        holder_pid = _live_lock_holder_pid(_get_graph_path())
        if holder_pid is not None:
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

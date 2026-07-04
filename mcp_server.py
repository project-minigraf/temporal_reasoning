#!/usr/bin/env python3
"""
Temporal Reasoning MCP Server.

Persistent stdio MCP server providing bi-temporal graph memory for AI coding agents.
Sole interface to the minigraf .graph file via the MiniGrafDb Python binding.
"""
import asyncio
import concurrent.futures
import configparser
import contextlib
import datetime
import json
import os
import re
import signal
import subprocess as _subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp.server import Server
from mcp.server.stdio import stdio_server
from minigraf import MiniGrafDb, MiniGrafError

try:
    from rank_bm25 import BM25Okapi as _BM25Okapi
    _BM25_AVAILABLE = True
except ImportError:
    _BM25Okapi = None  # type: ignore[assignment,misc]
    _BM25_AVAILABLE = False

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

# Ingestion state
_ingest_task: Optional[asyncio.Task] = None
_ingest_progress: Dict[str, Any] = {
    "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
    "current_commit": "", "error": None,
}
_shutdown_requested = asyncio.Event()

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
    _extract_commit, running in a worker thread) rather than being
    swallowed, consistent with how any other producer-task exception is
    handled.
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
        "functions": {"function_definition"},
        "classes": set(),
        "imports": {"function_call"},
        "calls": set(),
    },
    "elixir": {
        "functions": {"def", "defp"},
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
    """Return the root module name from an Elixir alias/import/use/require call.

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
    """Extract the function name from a call node (best-effort, identifiers only)."""
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


def _walk_ast(node, results: Dict[str, List[str]], lang_name: str) -> None:
    """Recursively extract code entities from a tree-sitter AST node.

    tsx is treated as an alias of typescript here (and in _extract_import_name)
    rather than duplicating every _LANG_NODE_TYPES entry — the TSX grammar is
    a strict superset of TypeScript's node types for the constructs this
    module cares about (functions, classes, imports, calls).
    """
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
                results["functions"].append(name_node.text.decode("utf-8"))

    elif node.type in node_types.get("classes", set()):
        name_node = node.child_by_field_name("name")
        if name_node:
            results["classes"].append(name_node.text.decode("utf-8"))

    elif node.type in node_types.get("imports", set()):
        names = _extract_import_name(node, lang_name)
        results["imports"].extend(names)

    elif node.type in node_types.get("calls", set()):
        name = _extract_call_name(node, lang_name)
        if name:
            results["calls"].append(name)

    for child in node.children:
        _walk_ast(child, results, lang_name)


def _extract_from_source(
    source: bytes, parser: Any, file_path: str
) -> Dict[str, List[str]]:
    """Parse source bytes and extract functions, classes, imports, calls."""
    results: Dict[str, List[str]] = {
        "functions": [], "classes": [], "imports": [], "calls": []
    }
    try:
        tree = parser.parse(source)
        lang_name = _EXT_TO_LANG.get(Path(file_path).suffix.lower(), "")
        _walk_ast(tree.root_node, results, lang_name)
    except Exception:
        pass  # best-effort; parse failures are non-fatal
    return results

# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------


def _get_graph_path() -> str:
    return os.environ.get("MINIGRAF_GRAPH_PATH", str(Path.cwd() / "memory.graph"))


def _open_db_at(path: str) -> MiniGrafDb:
    """Open MiniGrafDb at path, register session rules, update mtime tracking."""
    global _db, _graph_path, _db_mtime
    _db = MiniGrafDb.open(path)
    for rule in SESSION_RULES:
        _db.execute(rule)
    for rule in _user_rules:
        _db.execute(rule)
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


def _clear_stale_lock(path: str, holder_pid: int) -> bool:
    """Remove path's lock file if its recorded holder process is no longer alive.

    Returns True if a stale lock was removed.
    """
    try:
        os.kill(holder_pid, 0)
        return False  # holder still alive (or we lack permission to tell — leave it)
    except ProcessLookupError:
        pass
    except PermissionError:
        return False
    except OSError:
        return False
    try:
        os.remove(path + ".lock")
        return True
    except OSError:
        return False


def _open_db_at_with_retry(path: str) -> MiniGrafDb:
    """Open MiniGrafDb at path, retrying with backoff on lock contention.

    Self-heals a stale lock (holder process no longer running) by removing it
    before the next attempt, instead of surfacing a permanent error.
    """
    delay = _LOCK_RETRY_BASE
    last_exc: Optional[Exception] = None
    for attempt in range(_LOCK_RETRY_MAX):
        try:
            return _open_db_at(path)
        except Exception as e:
            if not _is_lock_error(e):
                raise
            last_exc = e
            holder_pid = _stale_lock_holder_pid(e)
            if holder_pid is not None and _clear_stale_lock(path, holder_pid):
                try:
                    return _open_db_at(path)
                except Exception as e2:
                    if not _is_lock_error(e2):
                        raise
                    last_exc = e2
            if attempt < _LOCK_RETRY_MAX - 1:
                time.sleep(delay)
                delay *= 2
    assert last_exc is not None
    raise last_exc


def get_db() -> MiniGrafDb:
    """Return the open DB instance, opening it if not currently held.

    The DB is opened per-operation and released after each call_tool() invocation
    so that the prepare_hook subprocess can acquire the file lock between turns.
    Opening retries with backoff on lock contention (see _open_db_at_with_retry).
    """
    if _db is None:
        _open_db_at_with_retry(_graph_path or _get_graph_path())
    return _db


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
        return {"ok": True, "tx": str(data.get("tx", "unknown"))}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Unexpected result format: {e} — raw: {raw_json[:200]}"}


# ---------------------------------------------------------------------------
# Explicit agent tool handlers
# ---------------------------------------------------------------------------

def handle_minigraf_query(datalog: str) -> Dict[str, Any]:
    """Query the graph. Returns {ok, results} or {ok, error}."""
    db = get_db()
    try:
        raw = db.execute(f"(query {datalog})")
        return _parse_query_result(raw)
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


def handle_minigraf_transact(facts: str, reason: str) -> Dict[str, Any]:
    """Transact facts into the graph. reason is required.

    :valid-at is set to the current UTC ms timestamp so every agent-initiated
    write has a recorded valid time, enabling correct bi-temporal queries.
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
    try:
        raw = db.execute(f'(transact {facts} {{:valid-from "{_now_utc_ms()}"}})')
        db.checkpoint()
        _update_mtime()
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
            _index_cache.invalidate()
        return result
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


def handle_minigraf_retract(facts: str, reason: str) -> Dict[str, Any]:
    """Retract facts from the graph. reason is required."""
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for retract"}
    _refresh_if_stale()
    db = get_db()
    try:
        raw = db.execute(f"(retract {facts})")
        db.checkpoint()
        _update_mtime()
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
            _index_cache.invalidate()
        return result
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


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
        db.execute(f"(rule {rule})")
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
        except Exception:
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
            except Exception:
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
                        retract_expr = f"(retract [{' '.join(retract_triples)}])"
                        db.execute(retract_expr)
                        db.checkpoint()
                        _update_mtime()
                        retracted += 1
                    except Exception:
                        pass

    return {
        "ok": True,
        "audited": audited,
        "retracted": retracted,
        "violations": all_violations,
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

# Each entity triggers one unindexed, O(graph-size) `contains?` scan in
# _handle_memory_prepare_turn_heuristic (see issue #96). Cap the count so a
# long user message can't turn one hook invocation into an unbounded number
# of full-graph scans.
_MAX_HEURISTIC_ENTITIES = int(os.environ.get("MINIGRAF_PREPARE_MAX_ENTITIES", "8"))


def _canonical_ident(entity_type: str, value: str) -> str:
    """Slug-canonicalize a value into a Minigraf keyword ident.

    Lowercases, replaces any character outside [a-z0-9-] with a hyphen,
    collapses consecutive hyphens, strips leading/trailing hyphens.
    Ported from _to_kw() in minigraf-examples LlamaIndex integration.
    """
    slug = re.sub(r"[^a-z0-9-]", "-", value.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f":{entity_type}/{slug}"


def _resolve_module_import(import_name: str, file_entities: Dict[str, List[str]]) -> Tuple[str, bool]:
    """Resolve an import name to a module ident that joins with stored module entities.

    For a name like "storage", tries standard Rust source-root locations first
    (src/storage.rs, src/storage/mod.rs) before falling back to a broader name
    search. The ordered-priority approach prevents e.g. src/graph/storage.rs
    from matching a top-level `use crate::storage` import.

    Returns (ident, is_resolved). is_resolved is True when import_name matched
    a real file in file_entities, False when it fell through to the bare
    _canonical_ident guess — the caller uses this to tag genuinely unresolved
    imports as :type/external-dependency without also tagging real (if not
    yet visited) internal modules.
    """
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


def _git_commits(
    repo_path: str,
    watermark_hash: Optional[str],
    branch: str = "HEAD",
) -> List[tuple]:
    """Return list of (hash, ts_iso, author_email, subject) in chronological order."""
    range_spec = f"{watermark_hash}..{branch}" if watermark_hash else branch
    result = _subprocess.run(
        ["git", "log", "--reverse", "--format=%H %at %ae %s", range_spec],
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
    """Return (status_char, old_mode, new_mode, old_sha, new_sha, path) for
    every changed path in a commit, via a single `git diff-tree --raw` call.

    Supersedes running diff-tree a second time just to detect gitlinks:
    --raw already carries file mode (needed to spot submodule paths, mode
    160000) in the same subprocess invocation _extract_commit already makes.
    """
    result = _subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "-r", "--raw", "--root", commit_hash],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    entries = []
    for line in result.stdout.strip().splitlines():
        if not line.startswith(":"):
            continue
        meta, sep, path = line.partition("\t")
        if not sep:
            continue
        fields = meta[1:].split(" ")
        if len(fields) < 5:
            continue
        old_mode, new_mode, old_sha, new_sha, status = fields[0], fields[1], fields[2], fields[3], fields[4]
        entries.append((status[0], old_mode, new_mode, old_sha, new_sha, path))
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
    for status, old_mode, new_mode, old_sha, new_sha, path in raw_entries:
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
    """Return list of (status_char, path) for files changed in this commit."""
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


def _git_file_content(repo_path: str, commit_hash: str, file_path: str) -> bytes:
    """Return raw bytes of a file at the given commit."""
    result = _subprocess.run(
        ["git", "show", f"{commit_hash}:{file_path}"],
        cwd=repo_path, capture_output=True, check=True,
    )
    return result.stdout


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
) -> List[str]:
    """Return triple strings needed to bi-temporally close an entity.

    Closes :ident (canonical existence fact), :description (with real value),
    and the parent module's :contains edge.  The module's own :contains triple
    is omitted when ident == module_ident (modules have no parent module here).
    """
    triples = [
        f'[{ident} :ident "{_edn_escape(ident)}"]',
        f'[{ident} :description "{_edn_escape(description)}"]',
    ]
    if ident != module_ident:
        triples.append(f"[{module_ident} :contains {ident}]")
    return triples


def _ingest_transact(
    db: Any,
    triples: List[str],
    commit_ts_iso: str,
    reason: str,
) -> None:
    """Transact code-structure facts with :valid-from set to the commit timestamp."""
    if not triples:
        return
    facts_str = "[" + " ".join(triples) + "]"
    db.execute(f'(transact {facts_str} {{:valid-from "{commit_ts_iso}"}})')


def _ingest_close(
    db: Any,
    triples: List[str],
    original_ts_iso: str,
    commit_ts_iso: str,
    reason: str,
) -> None:
    """Close a fact's valid window at the deletion commit timestamp.

    Two-step process:
    1. Retract each original open-ended fact so it vanishes from current-time
       queries (retract has no temporal options, so this removes the unbounded
       assertion from the live view while keeping it in transaction history).
    2. Re-transact the same facts with explicit :valid-from + :valid-to so the
       historical valid window is preserved for point-in-time queries.

    Triples are retracted one-by-one to avoid EAVT collision on :contains edges
    (Minigraf's pending index omits value bytes, so batching multiple
    [module :contains fn] retracts could collide).
    """
    if not triples:
        return
    for triple in triples:
        try:
            db.execute(f"(retract [{triple}])")
        except Exception:
            pass  # best-effort: original may not exist if preload was incomplete
    facts_str = "[" + " ".join(triples) + "]"
    db.execute(
        f'(transact {facts_str} {{:valid-from "{original_ts_iso}" :valid-to "{commit_ts_iso}"}})'
    )


def _watermark_query(db: Any) -> Optional[str]:
    """Return the hash of the last ingested commit, or None if no watermark exists."""
    raw = db.execute("(query [:find ?h :where [:ingestion/watermark :hash ?h]])")
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _total_ingested_query(db: Any) -> int:
    """Return the :total-ingested watermark recorded by the last *completed* run, or 0.

    Only written on clean completion (see _last_run_write) — a run interrupted
    mid-way (e.g. by lock contention) leaves this stale even though further
    commits were durably persisted. Use _count_commit_entities for the true
    current count.
    """
    raw = db.execute("(query [:find ?n :any-valid-time :where [:ingestion/last-run-at :total-ingested ?n]])")
    results = json.loads(raw).get("results", [])
    return int(results[0][0]) if results else 0


def _count_commit_entities(db: Any) -> int:
    """Return the true number of durably persisted :type/commit entities.

    Unlike _total_ingested_query, this reflects reality even after a run was
    interrupted before it could write its completion watermark.
    """
    raw = db.execute("(query [:find (count ?e) :where [?e :entity-type :type/commit]])")
    results = json.loads(raw).get("results", [])
    return int(results[0][0]) if results else 0


def _watermark_update(db: Any, commit_hash: str, commit_ts_iso: str, reason: str) -> None:
    """Record the last successfully ingested commit hash in the graph."""
    existing = _watermark_query(db)
    if existing:
        db.execute(f'(retract [[:ingestion/watermark :hash "{existing}"]])')
    db.execute(
        f'(transact [[:ingestion/watermark :entity-type :type/ingestion] '
        f'[:ingestion/watermark :ident ":ingestion/watermark"] '
        f'[:ingestion/watermark :description "git ingestion watermark"] '
        f'[:ingestion/watermark :hash "{commit_hash}"]] '
        f'{{:valid-from "{commit_ts_iso}"}})'
    )


def _last_run_write(db: Any, commit_hash: str, run_at: str, total_ingested: int) -> None:
    """Record the wall-clock time, final commit hash, and cumulative ingested count."""
    db.execute(
        f'(transact [[:ingestion/last-run-at :entity-type :type/ingestion] '
        f'[:ingestion/last-run-at :ident ":ingestion/last-run-at"] '
        f'[:ingestion/last-run-at :description "last ingestion run timestamp"] '
        f'[:ingestion/last-run-at :last-run-at "{run_at}"] '
        f'[:ingestion/last-run-at :last-commit "{commit_hash}"] '
        f'[:ingestion/last-run-at :total-ingested {total_ingested}]])'
    )


# System attributes written by _transact_extracted_facts alongside domain attributes.
# They are invisible to schema validation and filtered from attr_facts in minigraf_audit.
_SYSTEM_ATTRS: frozenset = frozenset({":entity-type", ":ident"})

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
        },
    },
    "function": {
        "required": {":description": str},
        "optional": {
            ":file": str, ":alias": str,
            ":introduced-by": str, ":modified-in": str,
        },
    },
    "class": {
        "required": {":description": str},
        "optional": {
            ":file": str, ":alias": str,
            ":introduced-by": str, ":modified-in": str,
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
    pattern = r'\[(\:[^\s\]]+)\s+(\:[^\s\]]+)\s+"([^"]+)"\]'
    result = []
    for match in re.finditer(pattern, facts_str):
        entity, attribute, value = match.groups()
        entity_type = entity.split("/")[0].lstrip(":") if "/" in entity else ""
        result.append({
            "entity": entity,
            "entity_type": entity_type,
            "attribute": attribute,
            "value": value,
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
    """Format a list of [attr, val] or [e, attr, val] rows as a readable block."""
    if not results:
        return ""
    lines = []
    for row in results:
        lines.append("  " + " | ".join(str(v) for v in row))
    return "\n".join(lines)


_HISTORICAL_SIGNALS = re.compile(
    r"\b(last\s+\w+|yesterday|before|earlier|as\s+of|at\s+the\s+time|back\s+when|previously)\b",
    re.IGNORECASE,
)
# Note: "last <word>" is a broad pattern — "last resort", "last mile", etc. will match.
# Without an explicit ISO date in the message, _build_query_clauses falls back to the
# current UTC timestamp regardless, so false positives cause no harm in practice.
_DATE_PATTERN = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b"
)


def _is_historical_query(user_message: str) -> bool:
    return bool(_HISTORICAL_SIGNALS.search(user_message))


def _now_utc_ms() -> str:
    """Return current UTC time as an ISO 8601 string with millisecond precision and Z suffix.

    minigraf requires UTC (no timezone offsets) and millisecond precision to
    reliably find facts transacted in the same second as the query.
    e.g. "2026-05-02T15:44:52.184Z"
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _build_query_clauses(user_message: str) -> str:
    """
    Return temporal clauses to append to a Datalog query.

    For current-state queries use :valid-at with the current UTC timestamp
    (millisecond precision). This correctly finds all facts whose valid window
    includes right now — including facts transacted earlier the same second —
    while excluding expired/retracted facts and future-dated facts.

    For historical queries where an explicit ISO date is detected in the user
    message, use :valid-at with that date (resolves to midnight UTC on that
    date — intentional for point-in-time historical semantics).

    minigraf :valid-at accepts: ISO 8601 date ("YYYY-MM-DD" → midnight UTC)
    or UTC datetime with Z suffix ("YYYY-MM-DDTHH:MM:SS.mmmZ").
    Timezone offsets are not supported; :any-valid-time disables filtering.
    """
    if _is_historical_query(user_message):
        date_match = _DATE_PATTERN.search(user_message)
        if date_match:
            valid_at = date_match.group(1)
            return f':valid-at "{valid_at}"'
    return f':valid-at "{_now_utc_ms()}"'


# ---------------------------------------------------------------------------
# BM25 index — semantic retrieval primitives
# ---------------------------------------------------------------------------

_MEMORY_PREFIXES = (":decision/", ":preference/", ":constraint/", ":dependency/")


def _tokenize(text: str) -> List[str]:
    """Split text on non-alphanumeric chars, lowercase, filter empties.

    Works on raw fact values and keyword idents alike:
      ":decision/use-redis" → ["decision", "use", "redis"]
      "use Redis for caching" → ["use", "redis", "for", "caching"]
    """
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


class FactIndex:
    """Immutable BM25 snapshot over a set of graph facts.

    Each fact row [e, a, v] is tokenised as a single document.
    Memory facts (entity idents with a known memory prefix) receive
    a configurable score multiplier at query time.
    """

    def __init__(self, facts: List[List], boost: float = 2.0) -> None:
        self._boost = boost
        docs = [_tokenize(" ".join(str(x) for x in row)) for row in facts]
        # Filter out rows whose full text produces no tokens
        valid = [
            (row, doc, any(str(row[0]).startswith(p) for p in _MEMORY_PREFIXES))
            for row, doc in zip(facts, docs)
            if doc
        ]
        if not valid or _BM25Okapi is None:
            self._bm25 = None
            self._facts: List[List] = []
            self._is_memory: List[bool] = []
            self._docs: List[List[str]] = []
            return
        rows, valid_docs, memory_flags = zip(*valid)
        self._facts = list(rows)
        self._is_memory = list(memory_flags)
        self._docs: List[List[str]] = list(valid_docs)
        self._bm25 = _BM25Okapi(self._docs)

    def query(self, text: str, top_n: int = 50) -> List[List]:
        """Return up to top_n facts ranked by BM25 score (memory boost applied).

        Facts with no token overlap with the query are excluded. Returns []
        if the index is empty or no query tokens appear in any indexed fact.
        """
        if self._bm25 is None or not self._facts:
            return []
        tokens = _tokenize(text)
        if not tokens:
            return []
        raw_scores = self._bm25.get_scores(tokens).tolist()
        # Identify docs with any token overlap.
        # BM25Okapi can return negative scores in small corpora (negative IDF),
        # so we detect overlap via a per-token presence check rather than relying on score > 0.
        token_set = set(tokens)
        has_overlap = [bool(token_set & set(doc)) for doc in self._docs]
        overlapping_scores = [raw_scores[i] for i in range(len(raw_scores)) if has_overlap[i]]
        if not overlapping_scores:
            return []
        # Shift so minimum overlapping score is 1.0 — ensures boost always raises
        # memory facts in rank, even when BM25 produces negative IDF in small corpora.
        shift = max(0.0, 1.0 - min(overlapping_scores))
        scores = [raw_scores[i] + shift for i in range(len(raw_scores))]
        for i, is_mem in enumerate(self._is_memory):
            if is_mem:
                scores[i] *= self._boost
        ranked = sorted(
            [(scores[i], self._facts[i]) for i in range(len(self._facts)) if has_overlap[i]],
            key=lambda x: x[0],
            reverse=True,
        )
        return [row for _, row in ranked[:top_n]]


class IndexCache:
    """Module-level singleton managing the live BM25 FactIndex.

    Rebuilds asynchronously in a background thread. Serves the stale index
    during rebuilds; returns None before the first successful rebuild.
    Invalidation is idempotent while a rebuild is in progress.
    """

    def __init__(self) -> None:
        self._current: Optional[FactIndex] = None
        self._rebuilding: bool = False
        self._lock = threading.Lock()

    def get(self) -> Optional[FactIndex]:
        """Return the current index (may be stale or None)."""
        return self._current

    def invalidate(self) -> None:
        """Trigger an async rebuild if one is not already running."""
        if self._rebuilding:
            return
        self._rebuilding = True
        t = threading.Thread(target=self._rebuild, daemon=True)
        t.start()

    def _rebuild(self) -> None:
        """Fetch all currently-valid facts from the DB and swap the index."""
        try:
            db = get_db()
            boost = float(os.environ.get("MINIGRAF_MEMORY_BOOST", "2.0"))
            raw = db.execute(
                f'(query [:find ?e ?a ?v :valid-at "{_now_utc_ms()}" :where [?e ?a ?v]])'
            )
            facts = json.loads(raw).get("results", [])
            new_index = FactIndex(facts, boost=boost)
            with self._lock:
                self._current = new_index
        except Exception as e:
            print(f"[IndexCache] rebuild failed: {e}", file=sys.stderr)
        finally:
            self._rebuilding = False


_index_cache = IndexCache()


def _handle_memory_prepare_turn_heuristic(user_message: str) -> str:
    """Heuristic fallback for handle_memory_prepare_turn.

    Used when rank_bm25 is unavailable. Queries the graph using substring
    token matching (contains?) for entities extracted from the user message,
    falling back to a broad scan when no targeted results are found.

    For current-state queries, uses :valid-at with the current UTC ms timestamp
    (via _build_query_clauses) so facts whose valid window includes right now
    are returned. For historical queries where an explicit ISO date is detected
    in the user message, :valid-at is set to that date (midnight UTC).
    """
    db = get_db()
    scan_limit = int(os.environ.get("MINIGRAF_PREPARE_SCAN_LIMIT", "50"))
    temporal_clauses = _build_query_clauses(user_message)

    entities = _extract_entities(user_message)[:_MAX_HEURISTIC_ENTITIES]
    collected: List[List[str]] = []
    seen: set = set()

    for entity in entities:
        try:
            raw = db.execute(
                f'(query [:find ?a ?v {temporal_clauses} :where [?e ?a ?v] (contains? ?v "{entity}")])'
            )
            data = json.loads(raw)
            for row in data.get("results", []):
                key = tuple(row)
                if key not in seen:
                    seen.add(key)
                    collected.append(row)
        except (MiniGrafError, json.JSONDecodeError):
            continue

    if not collected:
        # Broad fallback scan — still respect temporal clause
        try:
            raw = db.execute(
                f"(query [:find ?e ?a ?v {temporal_clauses} :where [?e ?a ?v]])"
            )
            data = json.loads(raw)
            all_results = data.get("results", [])
            collected = all_results[:scan_limit]
        except (MiniGrafError, json.JSONDecodeError):
            pass

    if not collected:
        return ""

    block = _format_facts(collected)
    return f"Relevant memory context:\n{block}"


def handle_memory_prepare_turn(user_message: str) -> str:
    """Query graph for facts relevant to the user message.

    Uses BM25-ranked retrieval over a cached FactIndex when rank_bm25 is
    available. Falls back to the heuristic (substring token) implementation
    when rank_bm25 is not installed.

    Returns a formatted context block string for injection as additionalContext,
    or an empty string if no relevant facts are found.
    """
    if not _BM25_AVAILABLE:
        return _handle_memory_prepare_turn_heuristic(user_message)

    scan_limit = int(os.environ.get("MINIGRAF_PREPARE_SCAN_LIMIT", "50"))
    index = _index_cache.get()
    if index is None:
        return ""
    results = index.query(user_message, top_n=scan_limit)
    if not results:
        return ""
    return f"Relevant memory context:\n{_format_facts(results)}"


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
    """
    _refresh_if_stale()
    db = get_db()
    stored = 0
    for fact in facts:
        entity = fact["entity"]
        entity_type = fact.get("entity_type", "")
        attribute = fact["attribute"]
        value = fact["value"]
        # Schema validation — closed-world: skip invalid facts.
        violations = _validate_facts([fact])
        if violations:
            continue
        now_z = valid_from or _now_utc_ms()
        try:
            # Combine main fact, :entity-type tag, and :ident into one transact so
            # all triples are written atomically — a single (transact [...]) is one
            # transaction. :ident stores the keyword ident as a string value so that
            # handle_minigraf_audit and _query_canonical_entities can surface it for
            # display without knowing the UUID (audits retract via #uuid "..." syntax).
            if entity_type:
                triples = (
                    f'[{entity} {attribute} "{value}"]'
                    f' [{entity} :entity-type :type/{entity_type}]'
                    f' [{entity} :ident "{entity}"]'
                )
            else:
                triples = f'[{entity} {attribute} "{value}"]'
            db.execute(f'(transact [{triples}] {{:valid-from "{now_z}"}})')
            stored += 1
        except MiniGrafError:
            continue
    if stored:
        db.checkpoint()
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

IMPORTANT — entity resolution: if a reference matches an existing canonical ident or alias above,
reuse that exact ident. Only mint a new ident if the entity is genuinely new.

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


def _get_anthropic_client():
    """Return an Anthropic client. Raises if anthropic package or API key is missing."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed — pip install anthropic")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)


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
    return openai.OpenAI(api_key=api_key)


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


def _parse_valid_at_hint(raw: str):
    """Extract optional '; valid-at: YYYY-MM-DD' comment from model output.

    Returns (valid_at, cleaned_datalog) where valid_at defaults to the current
    UTC ms timestamp if no hint is present.
    """
    valid_at = _now_utc_ms()
    kept = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("; valid-at:"):
            date_str = stripped[len("; valid-at:"):].strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
                valid_at = date_str
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
        _refresh_if_stale()
        db = get_db()
        db.execute(f'(transact {datalog} {{:valid-from "{valid_at}"}})')
        db.checkpoint()
        _update_mtime()
        # Approximate: count "[:" occurrences as a proxy for triple count.
        stored_count = datalog.count("[:")
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

    if strategy == "heuristic":
        facts = heuristic_extract(conversation_delta)
        stored = _transact_extracted_facts(facts)
        return {"ok": True, "stored_count": stored, "strategy": "heuristic"}

    if strategy == "llm":
        result = _llm_extract_and_transact(conversation_delta)
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


def _build_code_triples(
    file_path: str,
    extracted: Dict[str, List[str]],
    commit_ts_iso: str,
    entity_valid_from: Dict[str, str],
    entity_descriptions: Dict[str, str],
    file_entities: Dict[str, List[str]],
    commit_ident: str,
) -> List[str]:
    """Return Datalog triple strings for a file's extracted code entities.

    Stable attributes (:entity-type, :ident, :description, :path/:file,
    :introduced-by, :contains) are written ONCE on first introduction. On
    subsequent modifications only a :modified-in edge is added. This prevents
    bi-temporal fact explosion from N re-assertions of the same attribute
    joining into N² result rows.

    :depends-on edges are written in the commit loop by _run_ingestion as the
    file's imports change, giving them proper bi-temporal bounds.
    """
    triples: List[str] = []
    module_ident = _code_ident("module", file_path)

    is_new_module = module_ident not in entity_valid_from
    # Track all idents for this file (for deletion cleanup)
    idents_for_file = file_entities.setdefault(file_path, [])

    if is_new_module:
        # Write all stable attributes once, at introduction time
        triples += [
            f"[{module_ident} :entity-type :type/module]",
            f'[{module_ident} :ident "{module_ident}"]',
            f'[{module_ident} :description "{_edn_escape(file_path)}"]',
            f'[{module_ident} :path "{_edn_escape(file_path)}"]',
            f"[{module_ident} :introduced-by {commit_ident}]",
        ]
        if module_ident not in idents_for_file:
            idents_for_file.append(module_ident)
        entity_valid_from[module_ident] = commit_ts_iso
        entity_descriptions[module_ident] = file_path

    else:
        # Existing module: only record that this commit modified it
        triples.append(f"[{module_ident} :modified-in {commit_ident}]")

    for fn_name in extracted["functions"]:
        fn_ident = _code_ident("function", file_path, fn_name)
        if fn_ident not in entity_valid_from:
            # New function: write all stable attributes once
            triples += [
                f"[{fn_ident} :entity-type :type/function]",
                f'[{fn_ident} :ident "{fn_ident}"]',
                f'[{fn_ident} :description "{_edn_escape(fn_name)}"]',
                f'[{fn_ident} :file "{_edn_escape(file_path)}"]',
                f"[{module_ident} :contains {fn_ident}]",
                f"[{fn_ident} :introduced-by {commit_ident}]",
            ]
            if fn_ident not in idents_for_file:
                idents_for_file.append(fn_ident)
            entity_valid_from[fn_ident] = commit_ts_iso
            entity_descriptions[fn_ident] = fn_name
        else:
            # Pre-existing function: record that this commit modified it
            triples.append(f"[{fn_ident} :modified-in {commit_ident}]")

    for cls_name in extracted["classes"]:
        cls_ident = _code_ident("class", file_path, cls_name)
        if cls_ident not in entity_valid_from:
            # New class: write all stable attributes once
            triples += [
                f"[{cls_ident} :entity-type :type/class]",
                f'[{cls_ident} :ident "{cls_ident}"]',
                f'[{cls_ident} :description "{_edn_escape(cls_name)}"]',
                f'[{cls_ident} :file "{_edn_escape(file_path)}"]',
                f"[{module_ident} :contains {cls_ident}]",
                f"[{cls_ident} :introduced-by {commit_ident}]",
            ]
            if cls_ident not in idents_for_file:
                idents_for_file.append(cls_ident)
            entity_valid_from[cls_ident] = commit_ts_iso
            entity_descriptions[cls_ident] = cls_name
        else:
            # Pre-existing class: record that this commit modified it
            triples.append(f"[{cls_ident} :modified-in {commit_ident}]")

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
    design spec's Section 2.

    Pre-seeding from `git ls-files` ensures that _resolve_module_import can
    find any module file even when processing early commits — before those files
    have been introduced in the chronological commit walk.

    Returns (entity_valid_from, entity_descriptions, file_entities).
    entity_valid_from maps ident → git commit timestamp of first introduction.
    entity_descriptions maps ident → human-readable name (function/class/file).
    """
    entity_valid_from: Dict[str, str] = {}
    entity_descriptions: Dict[str, str] = {}
    file_entities: Dict[str, List[str]] = {}

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

    for entity_type in ("module", "function", "class", "external-dependency"):
        path_attr = "path" if entity_type in ("module", "external-dependency") else "file"
        try:
            raw = db.execute(
                f'(query [:find ?ident ?path ?desc ?date '
                f':where [?e :entity-type :type/{entity_type}] '
                f'[?e :ident ?ident] '
                f'[?e :{path_attr} ?path] '
                f'[?e :description ?desc] '
                f'[?e :introduced-by ?c] '
                f'[?c :date ?date]])'
            )
            rows = json.loads(raw).get("results", [])
            for ident, path, desc, date in rows:
                entity_valid_from[ident] = date
                entity_descriptions[ident] = desc
                file_entities.setdefault(path, [])
                if ident not in file_entities[path]:
                    file_entities[path].append(ident)
        except Exception:
            pass

    return entity_valid_from, entity_descriptions, file_entities


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
        raw = db.execute(
            "(query [:find ?src ?dep ?vf "
            ":any-valid-time "
            ":where [?src :depends-on ?dep] "
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
        raw = db.execute(
            "(query [:find ?e ?sha ?vf "
            ":any-valid-time "
            ":where [?e :pinned-commit ?sha] "
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


def _ingest_tags(db: Any, repo_path: str, run_ts_iso: str) -> None:
    """Ingest git tags as :tag/<slug> entities with :tagged-commit references.

    Called once after the commit walk. All tags are re-ingested on every run
    so newly created tags pointing to previously ingested commits are picked up.
    Re-transacting identical facts is idempotent in Minigraf.
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
            triples = [
                f"[{tag_ident} :entity-type :type/tag]",
                f'[{tag_ident} :name "{_edn_escape(tag_name)}"]',
                f'[{tag_ident} :ident "{tag_ident}"]',
                f'[{tag_ident} :description "git tag {_edn_escape(tag_name)}"]',
                f"[{tag_ident} :tagged-commit {commit_ident}]",
            ]
            if date_raw:
                triples.append(f'[{tag_ident} :date "{_edn_escape(date_raw)}"]')
            db.execute(f'(transact [{" ".join(triples)}] {{:valid-from "{run_ts_iso}"}})')
        except Exception:
            pass  # non-fatal per tag


def _extract_commit(
    repo_path: str, commit_hash: str
) -> Tuple[List[Tuple[str, str, Optional[Dict[str, List[str]]]]], List[tuple], Dict[str, Dict[str, str]]]:
    """Read-only, stateless per-commit extraction: diff-tree + git-show + tree-sitter parse.

    Runs in a worker thread via the ThreadPoolExecutor in _run_ingestion.
    Touches no shared mutable state and no DB. Returns (file_results,
    gitlink_changes, gitmodules_map):

      file_results: one entry per changed file that has a supported parser;
        A/M files whose content fetch fails are omitted entirely, mirroring
        the previous inline `continue` — same as before this pipeline existed.
      gitlink_changes: _gitlink_changes' output — gitlink-involving rows,
        never fed through the tree-sitter parser (gitlink paths never have
        a resolvable extension).
      gitmodules_map: path -> {"name", "url"}, populated only when this
        commit has at least one gitlink "add" — avoids a wasted git-show
        call on the (overwhelmingly common) case of a commit that touches
        no submodules at all.

    Sources both file_results and gitlink_changes from a single
    `git diff-tree --raw` call (via _git_diff_tree_raw) rather than the
    former --name-status call, which discarded file mode entirely.
    """
    raw_entries = _git_diff_tree_raw(repo_path, commit_hash)
    results: List[Tuple[str, str, Optional[Dict[str, List[str]]]]] = []
    for status, old_mode, new_mode, old_sha, new_sha, file_path in raw_entries:
        parser = _thread_parser(file_path)
        if parser is None:
            continue
        if status == "D":
            results.append((status, file_path, None))
            continue
        try:
            content = _git_file_content(repo_path, commit_hash, file_path)
        except Exception:
            continue
        extracted = _extract_from_source(content, parser, file_path)
        results.append((status, file_path, extracted))

    gitlink_changes = _gitlink_changes(raw_entries)
    gitmodules_map: Dict[str, Dict[str, str]] = {}
    if any(kind == "add" for kind, _, _ in gitlink_changes):
        gitmodules_map = _git_gitmodules_at(repo_path, commit_hash)

    return results, gitlink_changes, gitmodules_map


async def _run_ingestion(repo_path: str, branch: str) -> None:
    """Background coroutine: walk git history and ingest code structure.

    Extraction (git show + tree-sitter parse) for upcoming commits runs
    ahead of time on a thread pool via a bounded sliding-window pipeline;
    all DB-writing bookkeeping below stays strictly sequential, one commit
    at a time, exactly as before this pipeline was introduced.
    """
    global _db, _ingest_progress
    # Safe to clear unconditionally: handle_minigraf_ingest_git refuses to start a
    # new run while one is already active, so no in-flight shutdown signal is ever
    # stomped on here; main()'s finally block re-sets the flag on exit regardless,
    # so a shutdown request arriving between runs is never silently lost.
    _shutdown_requested.clear()
    try:
        # Read watermark and pre-load known entities/deps before releasing DB
        db = get_db()
        watermark = _watermark_query(db)
        prior_ingested = _count_commit_entities(db)
        entity_valid_from, entity_descriptions, file_entities = _preload_known_entities(db, repo_path)
        file_deps, dep_valid_from = _preload_known_deps(db, file_entities)
        pinned_commit_state = _preload_pinned_commits(db)
        # minigraf exposes no explicit close(): the file lock is only released once
        # every reference to the handle is gone, so the local `db` must be cleared
        # too, not just the global — otherwise this frame keeps it alive (and the
        # lock held) through the potentially slow commit enumeration below.
        _db = None  # release file lock while enumerating commits
        db = None

        commits = _git_commits(repo_path, watermark, branch)
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
        max_workers = int(env_workers) if env_workers else min(32, (os.cpu_count() or 1) + 4)
        pipeline_depth = max_workers * 2

        loop = asyncio.get_running_loop()
        completed_all = True

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            commits_iter = iter(commits)
            pending: Any = deque()

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
                    completed_all = False
                    break

                (commit_hash, commit_ts_iso, author, subject), fut = pending.popleft()
                extracted_files, gitlink_changes, gitmodules_map = await fut
                submit_next()

                last_hash = commit_hash
                _ingest_progress["current_commit"] = commit_hash
                reason = f"git:{commit_hash} {author}: {subject}"

                # Build commit entity ident from first 12 chars of hash
                commit_ident = f":commit/{commit_hash[:12]}"

                # Acquire DB fresh each commit — never hold across yield
                db = get_db()
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

                    for status, file_path, extracted in extracted_files:
                        if status == "D":
                            # Close module and all known child entities for this file
                            idents = file_entities.get(file_path, [_code_ident("module", file_path)])
                            module_ident = _code_ident("module", file_path)
                            for ident in idents:
                                orig_ts = entity_valid_from.get(ident, commit_ts_iso)
                                desc = entity_descriptions.get(ident, "")
                                close_items.append(
                                    (_build_close_triples(ident, desc, module_ident), orig_ts)
                                )
                            # Close all :depends-on edges for the deleted module
                            for dep_ident in file_deps.get(file_path, set()):
                                orig_ts = dep_valid_from.get((module_ident, dep_ident), commit_ts_iso)
                                close_items.append(
                                    ([f"[{module_ident} :depends-on {dep_ident}]"], orig_ts)
                                )
                            file_deps.pop(file_path, None)
                        else:  # A or M
                            previous_idents = set(file_entities.get(file_path, []))
                            triples = _build_code_triples(
                                file_path, extracted, commit_ts_iso, entity_valid_from,
                                entity_descriptions, file_entities, commit_ident,
                            )
                            add_triples.extend(triples)
                            # Detect entities removed from a modified file.
                            # _build_code_triples only appends to file_entities, never removes.
                            # Compare previous idents against the idents derivable from the
                            # current extraction to find what was deleted.
                            if status == "M":
                                module_ident = _code_ident("module", file_path)
                                current_extracted_idents: set = {module_ident}
                                for fn_name in extracted.get("functions", []):
                                    current_extracted_idents.add(_code_ident("function", file_path, fn_name))
                                for cls_name in extracted.get("classes", []):
                                    current_extracted_idents.add(_code_ident("class", file_path, cls_name))
                                removed_idents = previous_idents - current_extracted_idents
                                for ident in removed_idents:
                                    orig_ts = entity_valid_from.get(ident, commit_ts_iso)
                                    desc = entity_descriptions.get(ident, "")
                                    close_items.append(
                                        (_build_close_triples(ident, desc, module_ident), orig_ts)
                                    )
                            # Compute dep edges for this file and diff against previous
                            module_ident = _code_ident("module", file_path)
                            current_deps: set = set()
                            for import_name in set(extracted.get("imports", [])):
                                dep_ident, is_resolved = _resolve_module_import(import_name, file_entities)
                                if dep_ident != module_ident:
                                    current_deps.add(dep_ident)
                                    if not is_resolved and dep_ident not in entity_valid_from:
                                        add_triples.extend([
                                            f"[{dep_ident} :entity-type :type/external-dependency]",
                                            f'[{dep_ident} :ident "{_edn_escape(dep_ident)}"]',
                                            f'[{dep_ident} :description "{_edn_escape(import_name)}"]',
                                        ])
                                        entity_valid_from[dep_ident] = commit_ts_iso
                                        entity_descriptions[dep_ident] = import_name
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
                                (_build_close_triples(ext_ident, desc, ext_ident), orig_ts)
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
                    _ingest_transact(db, other_triples, commit_ts_iso, reason)
                    for ct in contains_triples:
                        _ingest_transact(db, [ct], commit_ts_iso, reason)
                    # :depends-on triples transacted individually — same EAVT collision risk
                    # as :contains when multiple deps share the same source module
                    for dt in dep_add_triples:
                        _ingest_transact(db, [dt], commit_ts_iso, reason)
                    for close_triples, orig_ts in close_items:
                        _ingest_close(db, close_triples, orig_ts, commit_ts_iso, reason)

                    # Ingest :parent edges — one transact per parent to avoid EAVT
                    # collision for merge commits (which have two parent hashes).
                    try:
                        for parent_hash in _git_parent_hashes(repo_path, commit_hash):
                            parent_ident = f":commit/{parent_hash[:12]}"
                            db.execute(
                                f'(transact [[{commit_ident} :parent {parent_ident}]] '
                                f'{{:valid-from "{commit_ts_iso}"}})'
                            )
                    except Exception:
                        pass  # non-fatal; parent edges are best-effort

                    _watermark_update(db, commit_hash, commit_ts_iso, reason)
                    db.checkpoint()

                finally:
                    _db = None  # release file lock between commits
                    db = None   # drop local reference too — see note above

                _ingest_progress["processed"] += 1
                await asyncio.sleep(0)  # yield to event loop

        if completed_all:
            now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            db = get_db()
            try:
                _ingest_tags(db, repo_path, now)
                _last_run_write(db, last_hash, now, _ingest_progress["processed"])
                db.checkpoint()
            finally:
                _db = None

            _ingest_progress["status"] = "complete"
            _index_cache.invalidate()
        else:
            _ingest_progress["status"] = "stopped"

    except Exception as e:
        _ingest_progress["status"] = "error"
        _ingest_progress["error"] = str(e)
        _db = None


async def handle_minigraf_ingest_git(
    repo_path: Optional[str] = None,
    branch: str = "HEAD",
) -> Dict[str, Any]:
    """Start background git ingestion. Returns immediately."""
    global _ingest_task, _ingest_progress
    if _ingest_task and not _ingest_task.done():
        return {"ok": False, "error": "ingestion already in progress"}
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
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None,
    }
    _ingest_task = asyncio.create_task(_run_ingestion(repo, branch))
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
    if _ingest_progress["status"] != "running":
        try:
            db = get_db()
            raw = db.execute(
                "(query [:find ?t ?h :any-valid-time "
                ":where [:ingestion/last-run-at :last-run-at ?t] "
                "[:ingestion/last-run-at :last-commit ?h]])"
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
            "dependencies, or preferences. Supports :as-of for temporal queries to see "
            "what the graph contained at a past transaction time."
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
            "Returns a context block string to prepend to your working context."
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
                    "description": "Branch or ref to walk. Defaults to HEAD.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="minigraf_ingest_status",
        description=(
            "Return the current git ingestion progress. "
            "status is one of: idle, running, complete, error."
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
                branch=arguments.get("branch", "HEAD"),
            )
            return [TextContent(type="text", text=json.dumps(result))]


        if name == "minigraf_ingest_status":
            result = handle_minigraf_ingest_status()
            return [TextContent(type="text", text=json.dumps(result))]

        raise ValueError(f"Unknown tool: {name}")
    finally:
        # Release the file lock after every tool call so that the prepare_hook
        # subprocess can open the DB between turns. get_db() re-opens on demand.
        _db = None


async def main() -> None:
    global _server_ref, _ingest_task, _ingest_progress
    _server_ref = server
    # Auto-start incremental ingest on server startup so ingestion begins
    # immediately without waiting for a user prompt.  Runs as a background
    # asyncio task — never blocks the message loop.
    # Set MINIGRAF_NO_AUTO_INGEST=1 to skip auto-start (used by eval sandboxes).
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None,
    }
    if not os.environ.get("MINIGRAF_NO_AUTO_INGEST"):
        _ingest_task = asyncio.create_task(_run_ingestion(str(Path.cwd()), "HEAD"))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown_requested.set)
        except (NotImplementedError, AttributeError):
            pass  # Windows: add_signal_handler unsupported; no graceful-shutdown-by-signal there

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


def run() -> None:
    """Sync entry point for the `temporal-reasoning` console script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()

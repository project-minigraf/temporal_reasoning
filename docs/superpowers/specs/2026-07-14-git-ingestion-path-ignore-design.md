# Git Ingestion Path-Ignore Config — Design

**Date:** 2026-07-14
**Issue:** #115

## Problem

Git ingestion walks every changed file in every historical commit through tree-sitter to
extract function/class entities, with no way to exclude vendored/third-party/generated
directories. On a real-world repo (ArangoDB, ~53k commits) this stalls for hours at commits
that vendor V8/ICU wholesale — one commit alone (`8e858bc9`, "Upgrade V8 to 4.2.77") touches
23,051 files, ~4,954 of them AST-parseable C++. Because extraction now runs in a
`ProcessPoolExecutor` (#116), a mega-commit like this pins every worker on CPU for as long as
the parse takes; the graph also gets bloated with thousands of vendored-code entities that
every structural query then has to contend with.

## Goal

Let ingestion skip vendored/generated paths entirely — no tree-sitter parse, no per-file
`:type/module`/function/class entities — while keeping in-repo files that legitimately
depend on that vendored code resolvable to *something*, not silently dangling.

## Design

### Treatment: same as external code, not a new entity type

The codebase already has a fallback for imports it can't resolve to an in-tree file: it
creates a single `:type/external-dependency` entity (mcp_server.py:3320-3331), the same
mechanism used for real npm/cargo/etc. packages and (as a distinct code path) for gitlink
submodules (mcp_server.py:3350-3389).

`_resolve_module_import`'s generic segment-suffix matcher currently treats vendored in-tree
code as fully internal, matching it against `known_files` like first-party source — that's
exactly why vendored code today produces full per-function/per-class entities instead of
falling through to the external-dependency fallback.

The fix requires no new entity-creation code: exclude ignored paths from `known_files`
(mcp_server.py:1585, `_known_files_at_commit`), and any in-repo import pointing at
now-excluded vendored code can no longer match the internal segment-suffix tiers, so it falls
through to the existing unresolved-import fallback automatically — one
`:type/external-dependency` entity for the whole ident, not one entity per vendored function.

### Ignore pattern resolution — once, at ingestion start

New function `_load_ignore_patterns(repo_path: str) -> List[str]`, called once in
`_run_ingestion` before the commit loop (near where `max_workers`/`pipeline_depth` are
computed, mcp_server.py:3173-3184). Merges three sources into one list, in this order:

1. Built-in defaults:
   ```python
   _DEFAULT_IGNORE_PATTERNS = (
       "3rdParty/", "third_party/", "vendor/", "node_modules/",
       "dist/", "build/", "*.min.js", "*.map",
   )
   ```
2. `MINIGRAF_INGEST_IGNORE` env var — comma-separated extra patterns, following the existing
   `MINIGRAF_INGEST_WORKERS`-style naming convention (mcp_server.py:3173).
3. An optional `.temporalignore` file at `repo_path`'s root — one pattern per line, blank
   lines and `#`-prefixed comment lines skipped. Read once via a plain file read against the
   current working tree (not `git show` against a historical commit) — the ignore config is
   a property of *how this ingestion run should behave*, not something that should vary
   commit-to-commit; re-reading it per commit would also mean early history ingests under an
   inconsistent (often absent) file.

Resolved once, so it's a plain `List[str]`, cheap to pass as an explicit argument across the
process-pool boundary (workers can't share module-level state set after `_run_ingestion`
starts — see #116's `spawn` context).

### Matching — simple glob/prefix, no new dependency

New pure function, placed near `_known_files_at_commit`:

```python
def _is_ignored_path(file_path: str, patterns: Sequence[str]) -> bool:
    """Simplified .gitignore-style match: no negation, no new dependency.

    - Pattern ending in "/": matches if that name is any path segment
      (directory-anywhere-in-path semantics, e.g. "vendor/" matches
      "src/vendor/foo.js" and "vendor/bar.js").
    - Pattern containing a glob char (*, ?, [): fnmatch against the
      basename, then the full path (covers "*.min.js" and prefix-style
      globs like "generated/**" alike without needing real ** support).
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
```

`fnmatch` is already stdlib — no new dependency, matching the recommended approach (full
`.gitignore` semantics via `pathspec` was considered and rejected: this project currently
has zero dependencies beyond `minigraf`/`mcp`, and the issue's own proposal only asks for
"glob/prefix exclusion list", not negation or `**` anchoring).

### Plumbing through the process-pool boundary

`_extract_commit`'s signature (mcp_server.py:3043) gains `ignore_patterns: Sequence[str]`:

```python
def _extract_commit(
    repo_path: str, commit_hash: str, ignore_patterns: Sequence[str]
) -> Tuple[List[tuple], List[tuple], Dict[str, Dict[str, str]]]:
```

Inside its per-file loop (mcp_server.py:3087-3105), ignored files are skipped *before*
`_thread_parser` runs — zero parse cost, matching the issue's actual complaint:

```python
for status, old_mode, new_mode, old_sha, new_sha, file_path in raw_entries:
    if _is_ignored_path(file_path, ignore_patterns):
        continue
    parser = _thread_parser(file_path)
    ...
```

`_known_files_at_commit` (mcp_server.py:1585) gains the same parameter and excludes matches:

```python
def _known_files_at_commit(
    repo_path: str, commit_hash: str, ignore_patterns: Sequence[str]
) -> Dict[str, List[str]]:
    ...
    for path in result.stdout.strip().splitlines():
        if Path(path).suffix.lower() in _EXT_TO_LANG and not _is_ignored_path(path, ignore_patterns):
            known[path] = []
```

`_run_ingestion`'s submission call (mcp_server.py:3232) passes the resolved list through:

```python
fut = loop.run_in_executor(executor, _extract_commit, repo_path, commit[0], ignore_patterns)
```

Gitlink/submodule handling (mcp_server.py:3344-3389) is untouched — gitlink paths are
extensionless and already produce a single opaque `:type/external-dependency` entity; the
ignore list only concerns AST-parseable in-tree files.

### Scope: forward-only

Ignored paths are skipped for any commit processed after this ships — new commits appended
to an already-ingested repo, or a from-scratch re-ingestion. A repo that was already fully
ingested before this feature existed keeps whatever vendored entities it already has; no
retroactive purge/backfill worker is in scope here (analogous to how #114's backfill for
#111/#112/#113 is its own separate, explicitly-scoped follow-up).

## Affected functions

| Function | Change |
|---|---|
| `_is_ignored_path` | New — pure glob/prefix/segment matcher |
| `_load_ignore_patterns` | New — merges defaults + env var + `.temporalignore` once at ingestion start |
| `_known_files_at_commit` | Gains `ignore_patterns` param; excludes matches from the known-files set |
| `_extract_commit` | Gains `ignore_patterns` param; skips ignored files before `_thread_parser` |
| `_run_ingestion` | Resolves ignore patterns once; passes them through the process-pool submission call |

## Testing

- `_is_ignored_path`: directory-anywhere match (`"vendor/"` vs `"src/vendor/foo.js"`,
  `"vendor/bar.js"`, and a non-match like `"vendored_thing.js"` — must not match on a bare
  substring); glob match (`"*.min.js"`, `"*.map"`); exact-segment/basename match; a path with
  no match returns `False`.
- `_load_ignore_patterns`: defaults present with no env var/file; env var patterns appended;
  `.temporalignore` lines parsed with comments/blanks skipped; all three merge together.
- `_known_files_at_commit`: an ignored path present in `git ls-tree` output is excluded from
  the returned dict even though its extension is in `_EXT_TO_LANG`.
- Ingestion-level regression: a commit adding a file under a default-ignored directory (e.g.
  `third_party/foo.cpp`) produces zero `:type/module`/function/class triples for that file;
  a separate in-repo file whose import resolves to that now-excluded path gets a single
  `:type/external-dependency` entity instead of an internally-resolved module dependency edge
  (mirrors the existing `TestExternalDependencyLabeling`-style test, per the 2026-07-04
  external-dependency design doc).

## Docs

- `SKILL.md`: document `MINIGRAF_INGEST_IGNORE` and `.temporalignore` alongside the other
  `MINIGRAF_*` ingestion env vars, including the built-in default pattern list.

## Non-goals

- No retroactive purge/backfill of vendored entities already ingested before this ships.
- No full `.gitignore` semantics (no negation, no `**` anchoring) — simple glob/prefix only.
- No per-commit re-reading of `.temporalignore` from historical tree state — resolved once
  from the current working tree at ingestion start.
- No change to gitlink/submodule handling — already minimal/opaque.

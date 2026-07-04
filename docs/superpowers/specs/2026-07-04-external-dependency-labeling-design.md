# External Dependency Labeling (Submodules, Unresolved Imports, Import Resolution) — Design Spec

**Issue:** #97
**Date:** 2026-07-04

## Background

Ingestion has no concept of "external" vs "in-source" code, confirmed against the arangodb repo:

1. **Real git submodules are invisible.** A gitlink path (mode `160000`, e.g. `3rdParty/abseil-cpp`) has no file extension `_get_parser` recognizes, and even if it did, `git show <hash>:<path>` fails on a gitlink (no blob content). The bare `except Exception: continue` in `_extract_commit` silently drops it — a submodule bump commit gets a `:type/commit` entity but contributes zero structure facts.
2. **Unresolved imports become unmarked placeholders.** `_resolve_module_import` falls back to `_canonical_ident("module", import_name)` for anything it can't match, minting a `:depends-on` edge target with no `:entity-type`, `:description`, or `:path` — indistinguishable from a real module that just hasn't been visited yet.
3. **While investigating #2, a deeper pre-existing gap surfaced:** `_resolve_module_import`'s matching logic is Rust-only (`src/{name}.rs`, `{name}/mod.rs` conventions). Every other supported language's import extraction already truncates to a bare top-level segment (first segment for Java/C#/Python/Scala/Kotlin/Swift/Haskell/Elixir, last segment for Go, basename for C/C++/Ruby/PHP) and *always* falls through to the external fallback today — meaning shipping "tag unresolved as external" as originally scoped would mislabel real in-tree vendored dependencies (e.g. arangodb's `3rdParty/icu`, `3rdParty/v8`) as external, contradicting the explicit requirement that vendored-in-tree code stays internal.
4. **Unrelated bug found during the same audit:** `.tsx` files are completely unsupported despite `_EXT_TO_LANG` listing them and SKILL.md claiming TSX support. `_build_parser` assumes the importable module name is always `tree_sitter_{lang_name}`; for `tsx` the actual (and only) module is `tree_sitter_typescript`, which exposes both `language_typescript()` and `language_tsx()`. The import throws, `_get_parser` caches `None`, and every `.tsx` file is silently skipped — same failure shape as the submodule problem, different root cause. Folded into this design as an additional fix.

## Goals

- Real submodules become queryable entities instead of silently vanishing.
- Genuinely unresolved imports are distinguishable from "not yet visited" internal modules.
- Vendored-in-tree code (regular files, not gitlinks) keeps today's internal treatment — never mislabeled external.
- Import resolution works consistently across all 17 supported languages, not just Rust.
- A path that flips between vendored-regular-file and submodule at the exact same location transitions bi-temporally: point-in-time queries before/after the flip see the correct designation.
- `.tsx` ingestion actually works.

## Non-goals

- Renamed/copied gitlink paths (`R`/`C` diff-tree status) — rare enough in practice to leave as a documented limitation, matching the existing (also incomplete) rename handling for regular files.
- Retroactive backfill of `:depends-on` edge granularity for already-ingested history in namespace languages — only files touched in commits after this ships get the new fuller-granularity edges; existing historical edges remain valid for their original time window.

## Section 1: Schema — `:type/external-dependency`

New entity type. Ident reuses the existing slug scheme (`:module/<slugified-path-or-name>`) so `:depends-on`/`:contains` edges naturally reference it the same way they reference `:type/module` — only `:entity-type` distinguishes internal from external.

| Attribute | Notes |
|---|---|
| `:description` | submodule's declared name from `.gitmodules` if resolvable, else raw path (submodules); raw import name (unresolved imports) |
| `:path` | submodule's repo path (submodules only; absent for unresolved-import placeholders) |
| `:pinned-commit` | pinned SHA the submodule currently points to (submodules only); bi-temporally closed/reopened on every bump |
| `:submodule-name` / `:submodule-url` | from `.gitmodules`, when parseable (submodules only) |
| `:introduced-by` / `:modified-in` (keyword refs) | same convention as `:type/module` |

Verified empirically (scratch repo): a submodule **add** is a plain `A` on the gitlink path; a **pinned-commit bump** is `M` with old/new mode both `160000`; a genuine same-path flip between a regular blob and a gitlink is `T` (type-change) — and only occurs when the path was a single file (not a directory) on both sides. "Vendored directory of files → submodule at the directory's path" is an ordinary `D`+`A` pair on two different literal paths, requiring no special-casing.

## Section 2: Detection & control flow

`_extract_commit` sources both file-level changes and gitlink-mode info from a single `git diff-tree --raw` call per commit (replacing the current `--name-status` call, which discards mode entirely — no added subprocess overhead). From the raw rows:

- the existing `(status, path)` list still feeds the unchanged tree-sitter extraction loop.
- gitlink-relevant rows (`old_mode == 160000` or `new_mode == 160000`) collapse into three cases based on the mode pair, regardless of the raw status letter:
  - **becoming external** (`new_mode==160000`, `old_mode!=160000`): fetch `.gitmodules` *at this commit* (current content, not diffed; best-effort `configparser` parse, `{}` on failure) to look up name/url for this path, then open a new `:type/external-dependency` entity.
  - **still external, pinned commit changed** (`old_mode==new_mode==160000`): close the old `:pinned-commit` fact, reopen with the new SHA, add a `:modified-in` edge.
  - **becoming internal or removed** (`old_mode==160000`, `new_mode!=160000`): close the external-dependency entity (same `:ident`/`:description`/`:path`/`:pinned-commit` close pattern used elsewhere). If the path is now a real blob with a supported extension, the existing, untouched tree-sitter loop creates the new internal module in the same commit automatically.

**State reuse, not new parallel structures:** `_preload_known_entities`'s query is broadened to also load `:type/external-dependency` idents into the same `entity_valid_from`/`entity_descriptions` dicts already used for close/reopen of modules — idents share one namespace, so this "just works." A new small preload, `_preload_pinned_commits`, modeled directly on the existing `_preload_known_deps` per-fact `:any-valid-time` pattern, tracks each external entity's current pinned SHA + its valid-from for correct closing on the next bump or removal.

**Unresolved imports:** `_resolve_module_import`'s return type changes from `str` to `(str, bool)` — the bool says whether it resolved to a known file or fell through to a bare-ident guess. The call site checks that flag: the first time a given fallback ident is seen (not already in `entity_valid_from`, which now also contains previously-tagged externals), it emits `:entity-type :type/external-dependency` + `:description <bare import name>` alongside the `:depends-on` edge already being written. No `:path` is set. If a same-named local module appears in a later commit, it resolves to a *different* ident (full-path-based vs bare-name-based), so the existing `:depends-on` add/remove diffing naturally retargets the edge — the stale external entity remains as accurate history for the period it was genuinely unresolved.

## Section 3: Generalized import resolution

This replaces the Rust-only heuristic so the labeling in Section 2 doesn't systematically mislabel vendored dependencies in every other language.

**Extraction changes** — capture the fullest identifying string per language instead of today's truncation:

| Language(s) | Today | Change |
|---|---|---|
| Java, C#, Python, Scala, Kotlin, Swift, Haskell, Elixir | first dotted segment only (`com.google.Gson` → `com`) | keep full dotted name |
| Go | last slash segment only (`github.com/foo/bar` → `bar`) | keep full path |
| JS/TS, Ruby | already carries enough of the raw string | unchanged |
| C/C++ quoted (`#include "unicode/uloc.h"`) | basename only (`uloc`) — `_c_include_name` currently applies `os.path.basename` to both quoted and angle-bracket forms alike | keep the full quoted path (`unicode/uloc.h`); quoted includes are relative-path-like almost by convention, so this materially improves resolution precision |
| C/C++ angle-bracket (`#include <vector>`) | basename only | unchanged — no real path exists to preserve for `<vector>` |

**Resolution changes** — one shared tiered matcher in `_resolve_module_import`, run after Rust's existing exact conventions (kept as-is, Rust unaffected):

1. Normalize (`.` → `/`), then **exact file match**: does any `file_entities` path, extension stripped, equal the candidate? (resolves `com/google/gson/Gson` against `com/google/gson/Gson.java`)
2. **Parent-directory match**: does the candidate minus its last segment equal some file's parent directory? (resolves package/wildcard-style imports)
3. **Bare basename match**: last segment only, against any file's stem — broadest, final tier (resolves C/C++ vendored headers like `uloc` against `.../unicode/uloc.h`)
4. Otherwise → genuinely unresolved → tag `:type/external-dependency` per Section 2.

**Relative imports** (`./utils/foo`, Python `from . import x`, Ruby `require_relative`): `_resolve_module_import` gains the importing file's own path as a parameter and resolves the relative specifier against that file's directory before running the tiered matcher. A relative import that still fails to resolve (bug in the source, deleted target, etc.) is *not* tagged `:type/external-dependency` — that label is reserved for "no such reference could exist internally," and a relative specifier can only ever refer to something local. It's left as today's bare, untagged fallback ident (no `:entity-type`) — a known, narrow residual gap (the placeholder-vs-real-module ambiguity issue #97 raised still applies to this one case) rather than a regression.

**Compatibility:** `:depends-on` edge granularity for the namespace/Go languages changes going forward. Existing coarse single-segment historical edges remain valid for their original time window (untouched); only files modified in commits after this ships get recomputed with the new fuller-granularity edges — consistent with the incremental ingester's existing behavior of only recomputing deps for files touched in the current run.

## Section 4: TSX fix (unrelated root cause, folded in)

- `_build_parser` gets an explicit `lang_name → module_name` override map (`{"tsx": "tree_sitter_typescript"}`) instead of assuming `tree_sitter_{lang_name}` always holds. `language_fn` lookup (`language_tsx` vs `language_typescript`) already works correctly once the right module is imported — no change needed there.
- `_LANG_NODE_TYPES.get(lang_name)` and `_extract_import_name`'s dispatch both resolve `"tsx"` as an alias of `"typescript"` at the top of each function, rather than duplicating every table entry.

## Error handling

- `.gitmodules` fetch/parse failures (missing file, malformed INI) are best-effort — empty dict, matches the file's existing `except Exception: pass` conventions elsewhere.
- Gitlink close/reopen triples use the same `_ingest_close`/`_ingest_transact` helpers already in place; retract-before-reopen failures are already tolerated (`except Exception: pass`) since a prior preload may have been incomplete.
- No new exception-handling patterns introduced — everything routes through existing bi-temporal helpers.

## Testing plan

- Extend `git_repo`-style fixtures with a scratch repo that adds a gitlink, bumps its pinned commit, removes it, and (separately) performs a same-path blob↔gitlink flip — assert entity-type, `:pinned-commit` history, and close/reopen timestamps at each step.
- Unresolved-import test: an import with no matching file gets tagged `:type/external-dependency` with `:description` set to the bare name, created exactly once across multiple commits/files referencing it.
- Per-language resolution tests for the new tiered matcher: exact-file, parent-directory, and basename tiers, at least for Java (package match), Go (full-path match), and C/C++ (vendored header basename match).
- Relative-import test: an unresolved `./utils/foo`-style import does not get tagged external.
- `.tsx` regression test: a `.tsx` file with a function/class/import is ingested and yields non-empty `functions`/`classes`/`imports`, mirroring the existing `.ts` test.

## Suggested implementation order

Four independently testable slices, in dependency order (each buildable/reviewable on its own even though they land in one plan):

1. TSX fix (Section 4) — fully independent, no dependency on anything else here.
2. Schema + submodule detection/control flow (Sections 1-2, submodule half) — the `:type/external-dependency` type, gitlink add/bump/remove/flip handling.
3. Unresolved-import tagging using today's existing (Rust-only) resolver (Section 2, import half) — proves out the tagging mechanism before the resolver itself changes underneath it.
4. Generalized import resolution + relative imports (Section 3) — layers the tiered matcher and per-language extraction changes on top of the tagging mechanism from (3).

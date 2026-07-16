# Git Ingestion Rename/Move Tracking — Design

**Date:** 2026-07-14
**Issues:** #111 (file/function/class rename tracking), #113 (global/field/static extraction, folded in as a prerequisite)

## Problem

Module/function/class identity is computed purely from `(current file path, name)` via
`_code_ident` (mcp_server.py:1442-1456), which itself slugs through `_canonical_ident`
(mcp_server.py:1272-1281). There is no content hash, no stable UUID, nothing that survives a
path change. `_git_diff_tree_raw` (mcp_server.py:1489-1513) never enables git's rename
detection (`-M`/`-C`), so any commit that renames or moves a file produces a plain delete +
add in the raw diff — indistinguishable from an unrelated file being deleted and a different
file being created. `_extract_commit` (mcp_server.py:3115-3191) has no branch for a rename
status because git never emits one today.

The result: any renamed or moved file gets a brand-new, fully disconnected entity, discarding
all history. This propagates one level down — function/class identity is *also*
`(file_path, name)`-derived, so a function untouched by a rename except for living in a
renamed file gets a second, unrelated entity too. It also propagates to a same-file rename
(`oldName()` → `newName()`) and a cross-file move (function cut from one file, pasted
unchanged into another) — both fracture identity the same way, one level below what git's own
diff machinery can see.

Issue #113 (globals/fields/statics are never extracted at all — no `:type/field`/
`:type/variable`) is folded into this work: implementing rename tracking for functions/classes
requires building a general entity-rename-matching mechanism, and extending that same
mechanism to cover globals/fields costs little extra once it exists, versus building it twice.

## Scope

**In scope:**
- File/module-level rename and move detection via git's own `-M` (not `-C`).
- Function/class-level rename and move detection (same-file rename, cross-file move) via a new
  content-based matcher, since these are below git's diff granularity.
- New `:type/variable` (module-level globals) and `:type/field` (class members, instance and
  static) entity types, extracted across all 16 currently-supported languages.
- Rename/move tracking for the new global/field entities, using the same matcher.
- New `:renamed-from`/`:renamed-to` schema attributes on all five entity types (module,
  function, class, variable, field).

**Out of scope:**
- Copy detection (`-C`). A copy leaves the original in place *and* creates a new, independent
  entity — treating it as a rename would misrepresent history (the old entity didn't close).
- The `:modified-in` over-attribution bug flagged in the issue's comment thread (currently
  attributed to every function/class in a touched file, not just the ones whose text actually
  changed in that commit's diff). Orthogonal, unconfirmed by its own reporter — file separately
  once confirmed with a clean repro.
- Retroactive backfill for already-ingested repos. Forward-only, same as #115's precedent —
  #114 is the explicitly-scoped follow-up for backfilling #111/#112/#113 into existing graphs.
- Local variables as graph entities. They participate in the matcher only as a text-level
  bijective-matching aid (see below) — never get their own ident, never appear in the graph.
- A function/class that is both renamed *and* has its own logic edited in the same commit.
  Falls back to today's disconnected close/open — a known limitation, not a regression.

## Identity scheme decision

Considered switching to a stable, non-path-derived identity (content hash or synthetic ID at
first introduction) so no new ident is ever created on a rename at all. Rejected: idents are
deeply embedded — every existing query, every doc example (`:module/src-auth-py`), every
`:calls`/`:depends-on` edge assumes the current path-derived format. Switching would be a much
larger, invasive change with no corresponding benefit over the alternative.

**Decision:** keep path-derived idents exactly as they are. On a detected rename, close the old
ident and open the new one through the existing bi-temporal close/open machinery, unchanged —
but also write an explicit `:renamed-from`/`:renamed-to` edge linking them. Queries that want
continuous history traverse the edge; everything else (ident format, existing queries, doc
examples) is untouched.

## Schema changes

New entity types, following the existing `function`/`class` block pattern
(mcp_server.py:1911-1934):

- `:type/variable` — module-level globals. Optional attrs: `:file`, `:alias`, `:introduced-by`,
  `:modified-in`, `:renamed-from`, `:renamed-to`.
- `:type/field` — class members (instance and static). Same attrs as `variable`, plus `:class`
  (edge to the owning class ident, mirroring how function/class carry `:file`) and `:static`
  (boolean).

New optional attribute on all five entity types (module, function, class, variable, field):
`:renamed-from`, `:renamed-to` — string-valued edges to another entity's ident, symmetric with
the existing `:introduced-by`/`:modified-in` edges to commit idents. Both directions are
written explicitly at rename time (old entity gets `:renamed-to <new-ident>` when closed, new
entity gets `:renamed-from <old-ident>` when opened) so a query can traverse either direction
without needing reverse-edge support from the query engine.

## Component 1 — File/module-level rename detection

Git already computes this reliably and cheaply via content-similarity diffing; no custom
matching needed at this level.

- `_git_diff_tree_raw` (mcp_server.py:1489-1513): add `-M` (rename detection only, no `-C`) to
  the `git diff-tree` invocation.
- **Parser fix required**: the raw-line parser currently does `line.partition("\t")`, which
  only splits on the *first* tab. A rename/copy raw line has the shape
  `:oldmode newmode oldsha newsha R100\told_path\tnew_path` — two tab-separated paths, not one.
  Without fixing this, `path` becomes the literal string `"old_path\tnew_path"`, `status[0]`
  becomes `"R"` (not the `"D"` branch `_extract_commit` currently checks), so it falls into the
  add/modify branch, calls `git show <hash>:old_path\tnew_path`, which fails and is silently
  swallowed by the bare `except Exception: continue` at mcp_server.py:3175-3176 — the file
  would be **silently dropped from ingestion entirely**, strictly worse than today's
  split-into-two-entities behavior. The fix must parse both paths and preserve the full status
  string (e.g. `R100` vs `R057`) instead of truncating to just the first character, since the
  similarity score should be threaded through (used for observability/debugging, not as a
  match-acceptance threshold — git's own default 50% threshold is trusted as-is, no additional
  filtering).
- `_extract_commit` (mcp_server.py:3115-3191): new branch for `status == "R"` — extract the new
  path's content as usual (same as today's add/modify path), tag the result with
  `renamed_from=<old_path>` instead of falling through to the exception-swallowing path above.
- `_precompute_file_triples` / `_build_code_triples` (mcp_server.py:2748-2820, 2823-2891): when
  a module's introduction carries `renamed_from`, emit `:renamed-from`/`:renamed-to` triples
  alongside the normal open/close triples the bi-temporal machinery already writes. No changes
  needed to the close/open mechanics themselves — this is a pure additive edge.

## Component 2 — Function/class/global/field rename detection

Runs once per commit, scoped only to entities from files touched in that commit (not a
repo-wide search) — bounds cost and false-positive risk, and matches how the issue's own
cross-file-move reproduction had both files in the same commit's diff.

### Algorithm: iterative AST-lockstep matching with local bijection

Reuses the tree-sitter ASTs already produced during extraction (no re-parsing). For a candidate
pair (removed entity body R, added entity body A) within one entity category (function, class,
variable, field), walk both ASTs in lockstep:

- **Node type mismatch at any position → bail immediately** (cheap early exit).
- **Identifier tokens** are classified at each position:
  - References a tracked entity (module/function/class/variable/field) with a confirmed rename
    **this round** → must equal the confirmed new name exactly.
  - References a tracked entity with **no rename** (name unchanged, still present) → must match
    exactly.
  - **Unrecognized** (a parameter, local variable, or unresolved external reference — anything
    not a currently-tracked entity name) → free to differ, but must satisfy a running
    one-to-one mapping (old↔new) for *this candidate pair only*: the same old token always maps
    to the same new token, and no two distinct old tokens collapse onto one new token. The
    mapping resets per candidate pair — it is a matching aid only, never persisted, and no
    entity is ever created for a local variable.
- **Everything else** (literals, operators, punctuation, keywords) must match exactly.

A full successful walk confirms the match at the entity level (`R`'s ident → `A`'s ident); the
local-variable mappings discovered along the way are discarded once the pair is confirmed.

This is a strict generalization of plain exact-match (exact match is the case where the
bijection happens to be the identity mapping), and is precise about scope in a way a
text/regex-substitution approach would not be — no risk of a word-boundary substitution
matching inside a string literal or comment.

### Outer round-based loop

A rename confirmed in one category (e.g. a global) needs to become an available "confirmed
rename" for other not-yet-matched candidate pairs (e.g. a function body that calls it) evaluated
in a later round. Rounds repeat, re-attempting all not-yet-matched pairs across all four
categories with the current confirmed-rename set, until a round produces no new matches. Capped
at 10 rounds as a defensive bound (expected to terminate in 1-2 rounds for realistic commits;
the cap only guards against pathological inputs).

### Ambiguity and minimum-size guards

- If a removed body has 2+ equally-good candidate matches among added bodies (e.g. duplicate
  boilerplate), **skip** — do not guess. False continuity is worse than missing continuity: it
  would corrupt history in a way indistinguishable from a real rename after the fact, whereas a
  missed match just reproduces today's existing (already-accepted) disconnected behavior.

  **Accepted deviation from this section's original text**: the implementation does not
  currently prefer same-file candidates over cross-file ones when disambiguating — any 2+-way
  ambiguity, same-file or not, is skipped uniformly. This was flagged by post-implementation
  review as a documented behavior gap rather than fixed, since the effect is strictly
  conservative (a same-file rename that's ambiguous only because of a coincidental cross-file
  match is missed, never falsely matched) and the added matching complexity wasn't judged worth
  it for a narrow edge case. Revisit if same-file-preference disambiguation turns out to matter
  in practice.
- Bodies below a minimum normalized size are excluded from matching entirely, to avoid spurious
  matches between trivial boilerplate (e.g. `def x(self): pass`-style stubs).

### Known limitations (all conservative — cause missed matches, never false ones)

- A function renamed *and* logically edited in the same commit: not caught, falls back to
  today's disconnected behavior.
- No true lexical scope resolution: if a local variable shadows a tracked entity's name, it's
  misclassified as "must match exactly," blocking the match.
- A name reused across genuinely different scopes within one body (shadowing) must map
  consistently to a single new name everywhere in that body — a real edit that renamed two
  shadowed instances differently would be missed.

## Component 3 — Global/field/static extraction (#113)

New entity categories extracted across all 16 currently-supported languages (python,
javascript, typescript, tsx, rust, go, java, c, cpp, c_sharp, ruby, php, kotlin, swift, scala,
haskell, lua, elixir — per `_EXT_TO_LANG`, mcp_server.py:122-131).

- `_extract_from_source` (mcp_server.py:712-725): extend the returned dict with new
  `"globals"`/`"fields"` keys, alongside the existing `"functions"`/`"classes"`/`"imports"`/
  `"calls"`.
- `_LANG_NODE_TYPES` (mcp_server.py:238-342): extend with new category → node-type sets per
  language for module-level global declarations and class field declarations. Cheap and mostly
  generic, following the same pattern as the existing `"functions"`/`"classes"` categories
  (name extraction via `node.child_by_field_name("name")` where the grammar exposes it).
- **Static vs. instance vs. module-scope classification is new work, not covered by the
  existing generic pattern** — node type alone (e.g. `field_declaration` in Java/C#) doesn't
  encode "static"; that's a modifier on the node. Requires new per-language modifier-inspection
  logic, one function per language, similar in shape to the existing per-language
  `_extract_import_name` branches (mcp_server.py:534-641).
- `_precompute_file_triples` / `_build_code_triples`: new `global_entries`/`field_entries`
  lists mirroring `function_entries`/`class_entries` exactly (mcp_server.py:2783-2793,
  2795-2805 for the existing pattern).
- `_preload_known_entities` (mcp_server.py:2894-, loop at 2931): add `"variable"`/`"field"` to
  the hardcoded entity-type reload list (currently `("module", "function", "class",
  "external-dependency")`) so incremental/restarted ingestion recognizes pre-existing idents.

## Component 4 — Rename tracking for globals/fields

Uses the same Component 2 matcher — globals and fields are just two more entity categories
participating in the same round-based matching loop. A global rename confirmed in one round
becomes available as a "confirmed rename" for function bodies (or other globals/fields)
referencing it in later rounds, and vice versa.

## Affected functions

| Function | Change |
|---|---|
| `_git_diff_tree_raw` | Add `-M`; fix two-path raw-line parsing for `R` status; preserve similarity score |
| `_extract_commit` | New `R`-status branch; extract new path, tag with `renamed_from` |
| `_extract_from_source` | New `"globals"`/`"fields"` keys in returned dict |
| `_LANG_NODE_TYPES` | New per-language node-type sets for globals/fields, across 16 languages |
| New per-language modifier-inspection helpers | Classify static vs. instance vs. module-scope (new, one per language) |
| New matcher module (e.g. `_match_renamed_entities`) | Component 2's AST-lockstep round-based matching loop |
| `_precompute_file_triples` | New `global_entries`/`field_entries`; emit `:renamed-from`/`:renamed-to` when paired with a confirmed rename |
| `_build_code_triples` | New entity-category loops for globals/fields, mirroring function/class handling |
| `_run_ingestion` | Invoke the new matcher after computing removed/added ident sets, before finalizing per-commit triples |
| `_preload_known_entities` | Add `"variable"`/`"field"` to the reloaded entity-type list |
| `MINIGRAF_SCHEMA` | New `variable`/`field` entity types; `:renamed-from`/`:renamed-to` on all five code entity types |

## Testing

Following existing conventions (`tests/test_mcp_server.py`, real throwaway git repos in
`tmp_path`, grouped `TestXxx` classes, `@pytest.mark.asyncio` for `_run_ingestion`-touching
tests):

- **Flip** `test_renamed_file_closes_old_entities_and_opens_new`
  (tests/test_mcp_server.py:4572-4597) — it currently asserts the *broken* disconnected
  behavior as correct (closes old, opens new, no link). Update to assert the new
  `:renamed-from`/`:renamed-to` edges are present.
- New `_git_diff_tree_raw` cases: `-M` raw lines for exact (`R100`) and partial (`R057`-style)
  similarity, verifying both old and new paths parse correctly and don't corrupt adjacent
  add/delete/modify line parsing.
- New `_extract_commit` tests for `R`-status handling, including the failure path (renamed file
  that fails to extract — must not silently drop, must log and treat gracefully).
- New matcher unit tests: pure in-place function rename; cross-file move (unchanged body);
  cascading mutual rename (A calls B, both renamed same commit); rename with an internal local
  variable also renamed (the bijection case); rename + logic edit in the same commit (expect no
  match — limitation test); duplicate-boilerplate ambiguity (expect no match); local variable
  shadowing a tracked entity name (expect no match — limitation test).
- Per-language (16) fixtures exercising: module-level global, instance field, static
  field/member extraction — at minimum one of each per language.
- New rename-tracking tests for globals/fields, mirroring the function/class rename tests.
- Full suite regression run before merge.

## Docs

- `SKILL.md`: document the new `:type/variable`/`:type/field` entity types and the
  `:renamed-from`/`:renamed-to` attributes, alongside the existing entity-type documentation.
- `ROADMAP.md:59` currently claims "The graph stores `:calls` and `:depends-on` edges that are
  entity-addressed, not file-addressed. Refactors do not break the history" — this is the
  aspirational claim #111 falsifies today and this work is meant to fulfill. Worth revisiting
  once this ships to confirm the claim now holds (or narrowing it if it still doesn't fully).

## Non-goals

- No copy detection (`-C`).
- No fix for the `:modified-in` over-attribution bug (separate issue).
- No retroactive backfill for already-ingested repos (#114's scope).
- No local-variable entities.
- No true lexical scope resolution in the bijective matcher.
- No coverage guarantee for a function that is both renamed and logically edited in the same
  commit.

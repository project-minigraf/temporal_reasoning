# Per-Function Body-Diff Attribution for `:modified-in` — Design

**Date:** 2026-07-24
**Issue:** #221

## Problem

`:modified-in` on a code entity (function/class/variable/field) is currently a **file-change
signal broadcast to every entity in the changed file**, not a per-body-change signal. In
`_build_code_triples`, for every changed file at each commit, every pre-existing entity in that
file unconditionally gets `[entity :modified-in commit]` appended — no comparison of whether that
entity's own body actually changed. Consequence: function-level churn just re-derives file-level
churn (every function in a file shares the file's modification count for as long as both are
live). A hot file with many long-lived functions emits mostly-spurious edges — the issue cites
`N_functions x N_file_commits` on a large real-world file, the overwhelming majority false.

This makes true function-level churn, function-level co-change, and fine-grained impact analysis
impossible, and inflates the graph with mostly-false `:modified-in` facts. It's also the
data-quality substrate #219 (navigation docs) and #220 (prepare_turn nudge) depend on — per their
own comment threads, function-level navigation signals aren't trustworthy until this is fixed.

## Root cause confirmed

`_build_code_triples` (mcp_server.py:5970-6071) iterates every entity `_precompute_file_triples`
found in the file's **current** content. If the entity's ident is already in `entity_valid_from`
(known from an earlier commit), it unconditionally appends `:modified-in`, regardless of whether
that specific entity's body changed in this commit — it has no way to tell, since
`_precompute_file_triples` only ever sees the current file's text, never a diff.

## Key finding that simplifies the fix

`_extract_commit` (mcp_server.py:6451+) already re-parses **both** the old (parent) blob and the
new blob for every M/R file, purely to support rename matching (`_match_renamed_entities`). This
already produces `old_entity_nodes`/`new_entity_nodes` — live tree-sitter nodes with byte spans,
keyed by name, per category (`function`/`class`/`variable`/`field`) — at zero extra parse cost.
The old side always comes from git's own diff-tree parent blob (`old_sha` in
`_git_diff_tree_raw`'s output), never from any in-memory or persisted state.

This means the body-diff comparison needs **no persistent hash sidecar**, unlike the issue's
original proposal. It's a pure old-blob-vs-new-blob comparison, fully derivable per-commit from
git itself, in either walk direction — confirmed compatible with #222's proposed multi-stream
design, whose own edge-case notes already treat `:modified-in` as "order-independent (per-commit
parent diff), always authoritative." Rename interaction is also already solved for free: a
renamed entity gets a brand-new ident (different name -> different `_code_ident`), so it never
reaches the `:modified-in`-emitting "already-known ident" branch at all — only the existing
`:renamed-from`/`:renamed-to` linkage path touches it.

## Design

Three coordinated, additive changes, all within `_extract_commit` / `_precompute_file_triples` /
`_build_code_triples`. No new persisted state, no schema/version bump, no new call-site plumbing
outside these three functions. Ships as default behavior, no opt-in flag — this is a strictly more
correct replacement for an already-broken signal, forward-only (see Scope below).

### 1. New helper: `_normalized_body_hash(node) -> str`

Walks a tree-sitter node's subtree, collects the text of every leaf node (no children) in
document order, joins with a separator byte, hashes with `hashlib.sha256`. Whitespace-insensitive
by construction — only token text is ever hashed, never the bytes between tokens — so a
reformatting-only change (e.g. this repo's own periodic clang-format sweeps) does not register as
churn. No per-language special-casing needed; the same leaf-walk works identically across all 18
supported grammars. Comment text is **not** stripped in this pass (see Scope) — a comment-only
edit still counts as a body change.

### 2. Reorder + extend `_extract_commit` / `_precompute_file_triples`

`_extract_commit` already computes `new_entity_nodes` (mcp_server.py:6667-6674) — just after
`_precompute_file_triples` is called. Move that computation a few lines earlier so it's available
before the call. `old_entity_nodes` is already computed earlier in the function; no change needed
there.

`_precompute_file_triples` gains two new required params, `old_entity_nodes` and
`new_entity_nodes` (the same category-keyed dicts `collect_all_nodes` already produces). For each
of the four categories, for every name present in **both** maps, compute the ident using the same
`_code_ident(category, file_path, name_or_qualified_name)` convention the existing candidate-triple
loops already use for that category (field names are already the qualified `"Class.field"` form
`field_nodes` produces, matching `_precompute_file_triples`'s own `qualified_name` exactly — no
reconciliation needed). If `_normalized_body_hash(old_node) == _normalized_body_hash(new_node)`,
add that ident to a new `unchanged_idents: Set[str]`, returned in the `precomputed` dict. The
whole computation is wrapped in `try/except Exception -> unchanged_idents = set()`, matching this
pipeline's existing per-file fail-open convention (see Error handling).

### 3. `_build_code_triples`: gate `:modified-in` on `unchanged_idents`

The four `else` branches (function/class/variable/field — module is deliberately excluded, since
file-level churn on the module entity is legitimate: the module *is* the file) change from
unconditionally appending `:modified-in` to:

```python
if ident not in unchanged_idents:
    triples.append(f"[{ident} :modified-in {commit_ident}]")
```

`unchanged_idents = precomputed.get("unchanged_idents", set())` read once at the top of the
function.

## Error handling

- **Old-blob parse failure** (existing `try/except` around the old-tree parse, mcp_server.py:6626,
  already falls back to empty `old_entity_nodes` category dicts): no names land in the
  old/new intersection for that file -> `unchanged_idents` stays empty -> every already-known
  ident in the file still gets `:modified-in`, exactly today's behavior. Fails open to the current
  (safe, if overzealous) signal; never suppresses a real change.
- **New-blob parse failure**: same fallback path (mcp_server.py:6671-6674), same fail-open result.
- **Hash-computation errors**: caught by the `try/except` around the whole unchanged-idents loop
  in `_precompute_file_triples`, falling back to an empty set for that file. Recursion-depth risk
  is a non-issue: any single entity's subtree is strictly shallower than the whole-tree walk
  `_collect_entity_nodes` already completed successfully for the same file (a prerequisite for
  `old_entity_nodes`/`new_entity_nodes` existing at all).
- **Renamed entities**: never reach this code — a rename produces a different ident and is handled
  entirely by the existing `:renamed-from`/`:renamed-to` path.

## Scope (deliberately excluded from this pass)

- **No comment-stripping.** Only whitespace-insensitivity is in scope for v1 — comment-only edits
  still count as churn. Comment-node identification is genuinely per-grammar fiddly work (the issue's
  own admission) and not required to fix the reformatting false-positive case this issue is
  primarily motivated by.
- **No indentation-structure hashing.** Tree structure and indentation are not encoded in the hash;
  only leaf-token text is captured. In indentation-significant languages (Python, Haskell, YAML),
  a pure re-indentation can change semantics without changing the token stream, causing such
  changes to hash identically and thus not be flagged as modifications (accepted v1 tradeoff;
  such changes are rare and body-diff attribution remains strictly more correct than pre-#221
  file-broadcast behavior).
- **No backfill.** This repo's own already-ingested graph keeps its existing over-marked
  `:modified-in` edges from before this fix; they are not retroactively pruned. Per the sequencing
  already recorded in project memory, the backfill mechanism was folded into #222's shared
  provisional/authoritative correction-pass framework, which hasn't been built yet and is
  sequenced after this issue. Backfilling is left to #222 (or a later, explicitly-scoped follow-up)
  once that framework exists.
- **No opt-in flag.** Forward-only + strictly-more-correct-than-today means there's no real
  migration risk to gate behind a flag.

## Testing

Per `docs/testing-conventions.md` (real `minigraf` backend, real temp git repo with real commits —
no mocks), new `TestBodyDiffAttribution`-style tests covering:

1. Whitespace/formatting-only change to a function body -> **not** marked `:modified-in`.
2. Genuine body change -> still marked `:modified-in`.
3. One function changed among several in the same file -> only that one gets `:modified-in`, the
   others don't (the issue's core repro).
4. Simulated reformat sweep (whitespace-only change across every function in a file) -> **none**
   marked `:modified-in`.
5. Field (qualified-name) and global-variable equivalents of #1/#2.
6. New entity introduced this commit -> unaffected, goes through the existing introduce path.
7. Old-blob parse failure -> falls back to marking `:modified-in` (fail-open), verified via a
   deliberately malformed/unparseable old blob.
8. Rename (same commit changes both name and body, or name only) -> confirmed to go through
   `:renamed-from`/`:renamed-to`, never touches `unchanged_idents` bookkeeping.

# evals/at_scale/introduced_by_audit.py
"""`:introduced-by` well-formedness, read off `fact_audit`'s existing scan.

TWO OPPOSITE DEFECTS, TWO KEYS, ONE PASS. `introduced_by_duplicates` (#287)
finds entities holding TWO values -- the corruption from #235.
`entities_without_introduced_by` (#316) finds live code entities holding NONE
-- the tear #313 fixed on the write path. Each is invisible to the other's
detector by construction, so they are reported separately; but both read the
same Counter, so the second costs nothing the first had not already paid.

WHY DETECTION, WITH NO REPAIR. #244 proposed repairing this and was closed on
the standing "rebuild, never migrate" decision (CLAUDE.md; docs/superpowers/
specs/2026-08-14-ident-rule-r3-and-format-version-design.md). The detection is
worth having on its own terms, because the answer decides whether a graph must
be thrown away and rebuilding is not free. Without it a user cannot tell a
healthy graph from a condemned one.

RE-RUNNING INGESTION REPAIRS NOTHING, and that is mechanical, not a policy
choice. A finished run parks `:ingestion/correction-sweep-through` at
frontier-high's `:hi-hash`. On the next run `_correction_sweep_select_position`
computes `ceiling_pos` from that bound and returns None on its FIRST call
(`if pos > ceiling_pos: return None`, mcp_server.py), so
`_correction_sweep_apply` -- which owns the repair -- runs zero times.
Measured during #235: watermark at linearization position 13 of 13, one
`select` call, zero `apply` calls. So an affected graph is unreachable by the
sweep that would have healed it mid-run, and the only remedy is to rebuild
into a FRESH graph path -- never repaired, never re-ingested in place.

IT RIDES THE SCAN IT DOES NOT PAY FOR. `fact_audit._graph_facts` already runs
`[:find ?e ?a ?v]` over the whole graph and builds a Counter of every fact.
Everything below is one pass over that dict, so the marginal cost of this
check is not a second query, a second scan, or a second lease.

IT IS NOT A SECOND-WITNESS CHECK, and is deliberately kept out of
`fact_audit`'s `divergence` for that reason. `divergence` means "the graph and
its index disagree" -- two independent storage engines cross-checking each
other. This is a well-formedness check on the graph side ALONE: both
`:introduced-by` values are faithfully in the index too, so a corrupt graph
diverges by zero. Folding the two into one number would make it mean two
incomparable things.

NARROWED TO `:introduced-by` ON PURPOSE. `:contains`, `:modified-in` and
`:depends-on` are legitimately multi-valued -- a module contains every function
in it. A detector keyed on "this entity has two values for some attribute"
would report every module in every healthy graph.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

INTRODUCED_BY = ":introduced-by"

_SAMPLE_CAP = 5


def introduced_by_duplicates(
    graph_facts: Counter, sample_cap: int = _SAMPLE_CAP
) -> dict[str, Any]:
    """Entities holding more than one live `:introduced-by` value.

    graph_facts is `fact_audit._graph_facts`' Counter, keyed by
    `(entity, attribute, index-text of value)`.

    Returns `{"entities": int, "sample": [[name, [value, ...]], ...]}`.

    `entities` is the number the rebuild decision rests on and is never
    capped; `sample` is. The count is of DISTINCT values, read from the number
    of Counter keys rather than their counts: identical (entity, attribute,
    value) triples collapse into one key with a count above one, and that is a
    different defect with a different answer, not #235.

    Each sampled entity is named by its own `:ident` fact when the scan
    carries one -- every code entity writes a self-referencing `[ident :ident
    "ident"]` (`_build_code_triples`), and it is already in this Counter, so
    naming it costs nothing. An entity without one falls back to the raw
    entity id: a partial write is exactly the state this looks for, and
    dropping such an entity from the sample would hide the worst case.
    """
    values_by_entity: dict[str, set] = defaultdict(set)
    idents: dict[str, str] = {}
    for entity, attribute, value in graph_facts:
        if attribute == INTRODUCED_BY:
            values_by_entity[entity].add(value)
        elif attribute == ":ident":
            idents[entity] = value

    affected = 0
    sample: list[list[Any]] = []
    for entity, values in values_by_entity.items():
        if len(values) < 2:
            continue
        affected += 1
        if len(sample) < sample_cap:
            sample.append([idents.get(entity, entity), sorted(values)])
    return {"entities": affected, "sample": sample}


# The five types `_build_code_triples` writes an `:introduced-by` for
# (mcp_server.py). Everything else in a graph legitimately has none.
#
# `:type/external-dependency` IS a code entity and is deliberately NOT here.
# `_forward_apply`'s dep-edge handling opens an unresolved-import stub with
# exactly three triples -- `:entity-type`, `:ident`, `:description` -- and no
# `:introduced-by`; only the submodule branch writes one, and
# `_build_close_triples` documents the same asymmetry from the close side
# ("unresolved-import stubs reuse the module ident prefix but never have an
# :introduced-by fact"). Including the type would report every unresolvable
# import in every healthy graph and leave the at-scale gate permanently red.
#
# `:type/commit`, `:type/ingestion`, `:type/ingest-interval`,
# `:type/lineage-marker` and `:type/candidate-diff` are bookkeeping, and
# `:type/decision` and friends are user-authored memory. None has a commit to
# be introduced by.
CODE_ENTITY_TYPES = frozenset({
    ":type/module",
    ":type/function",
    ":type/class",
    ":type/variable",
    ":type/field",
})

ENTITY_TYPE = ":entity-type"
IDENT = ":ident"


def entities_without_introduced_by(
    graph_facts: Counter, sample_cap: int = _SAMPLE_CAP
) -> dict[str, Any]:
    """Live code entities holding ZERO `:introduced-by` values (#316).

    THE ABSENT CASE, WHICH EVERY OTHER GATE READS AS CLEAN. Such an entity
    exists, answers structural queries and counts normally in entity totals,
    but has no lineage -- so it is invisible to `:as-of` reasoning and to
    every lineage traversal. #313 fixed the write path that produced it (a
    process killed inside `_reverse_apply`'s multi-transact window leaves the
    entity live with no `:introduced-by` and no lineage marker); this is the
    detection side, which nothing covered:

      * `introduced_by_duplicates` above is narrowed to entities holding TWO
        or more (`if len(values) < 2: continue`). The zero case is the
        opposite defect and falls straight through.
      * `fact_audit`'s `divergence` is a two-witness check, and the index is
        missing exactly the facts the graph is missing -- they are written
        from the same triples in the same transaction boundary. The two
        witnesses agree perfectly about a graph that is wrong. Same caveat as
        `introduced_by_duplicates`, and the same reason this must NOT fold
        into `divergence`: a run can read `divergence | 0` and be condemned.
      * `stderr_capture` -- #313's runs had zero bytes on stderr and zero
        `error_signals`.
      * `probe_provisional_residue` (#256) asserts M <= N over lineage
        markers. A torn entity has NO marker, so it never raises M, while the
        sweep does count it as unreconciled, raising N. That probe reads
        clean MORE comfortably when this defect is present.

    LIVENESS IS `:ident` PLUS `:entity-type`, NEVER THE IDENT PREFIX.
    `_build_close_triples` retracts `:ident`, `:entity-type` and
    `:introduced-by` in one triple list, but `close_entity_type` and
    `introduced_by` are each opt-in, so the reachable partial-close states are
    "live `:entity-type`, no `:ident`" and "live `:introduced-by`, no
    `:ident`" -- both excluded by requiring a live `:ident`. No site retracts
    `:introduced-by` while leaving `:ident` live. And the type is read from
    the `:entity-type` FACT rather than derived from the ident prefix,
    because unresolved-import stubs carry a `:module/...` ident while being
    `:type/external-dependency`; a prefix-derived type would report exactly
    the entities CODE_ENTITY_TYPES exists to exclude.

    IT RIDES THE SCAN IT DOES NOT PAY FOR, exactly as the check above does:
    one pass over `fact_audit._graph_facts`' existing Counter, no second
    query, no second scan, no second lease.

    Returns `{"entities": int, "code_entities_scanned": int, "sample":
    [ident, ...]}`.

    `code_entities_scanned` IS THE POSITIVE CONTROL, IN THE ARTIFACT.
    CLAUDE.md's standing requirement for a zero-tolerance gate is that its
    clean baseline is measured AND its positive control checked -- a check
    that matched no entities at all also reports 0, so the zero is only
    believable next to a denominator. Reporting it means every future run
    re-proves the check scanned something, rather than that being a fact
    about the day the gate was wired. `entities` is a SUBSET of it, not a
    disjoint tally: the reading is "0 of N", and N must include the offenders
    or a wholly corrupt graph would report 0 of 0.

    THE ONE FALSE POSITIVE THIS CANNOT RULE OUT is a memory-written code
    entity. `module`/`function`/`class`/`variable`/`field` are registered in
    MINIGRAF_SCHEMA with `:introduced-by` optional, so
    `handle_minigraf_transact` will accept a caller-written
    `[:module/foo :entity-type :type/module]` and produce a legitimately
    lineage-free `:type/module`. The handler adds no :entity-type of its own,
    so a bare `[:module/foo :description "x"]` is not a `:type/module` and
    cannot trip this check (measured, #353). The at-scale gate
    runs on a fresh ingestion-only graph, so it cannot fire there; on a mixed
    graph this row is informational. Narrowing further would need a
    discriminator between an ingested and a memory-written code entity, and
    the graph carries none.
    """
    has_introduced_by: set = set()
    entity_types: dict[str, str] = {}
    idents: dict[str, str] = {}
    for entity, attribute, value in graph_facts:
        if attribute == INTRODUCED_BY:
            has_introduced_by.add(entity)
        elif attribute == ENTITY_TYPE:
            entity_types[entity] = value
        elif attribute == IDENT:
            idents[entity] = value

    orphans: list[str] = []
    scanned = 0
    for entity, entity_type in entity_types.items():
        if entity_type not in CODE_ENTITY_TYPES or entity not in idents:
            continue
        scanned += 1
        if entity not in has_introduced_by:
            orphans.append(idents[entity])
    orphans.sort()
    return {
        "entities": len(orphans),
        "code_entities_scanned": scanned,
        "sample": orphans[:sample_cap],
    }

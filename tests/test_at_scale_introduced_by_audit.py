# tests/test_at_scale_introduced_by_audit.py
"""#287 and #316: `:introduced-by` well-formedness, read off one shared scan.

#287 is the two-value corruption from #235; #316 is the ABSENT case, where a
live code entity holds none at all. They are opposite defects and each is
invisible to the other's detector, so they are separate functions with
separate keys -- but they read the same Counter in the same pass.

The units under test are PURE -- they read the fact Counter that
`fact_audit._graph_facts` already builds -- so these tests construct that
Counter directly rather than a graph. The real-backend half, where the state
is transacted into an actual graph and the audit reports it, lives in
tests/test_at_scale_fact_audit.py; a pure function cannot prove that the
Counter it is handed has the shape it assumes.
"""

from collections import Counter

from evals.at_scale.introduced_by_audit import (
    entities_without_introduced_by,
    introduced_by_duplicates,
)


def _facts(*triples):
    """A Counter shaped like fact_audit._graph_facts' output."""
    return Counter({t: 1 for t in triples})


class TestTheCorruptionIsDetected:
    def test_two_values_on_one_entity_is_reported(self):
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
            )
        )
        assert result["entities"] == 1

    def test_a_single_value_is_not_reported(self):
        """The overwhelmingly common case. A detector that fired here would
        condemn every healthy graph."""
        result = introduced_by_duplicates(
            _facts(("uuid-a", ":introduced-by", ":commit/aaa"))
        )
        assert result["entities"] == 0
        assert result["sample"] == []

    def test_an_entity_with_no_introduced_by_is_not_reported(self):
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":description", "a function"),
                ("uuid-a", ":entity-type", ":type/function"),
            )
        )
        assert result["entities"] == 0

    def test_three_values_still_counts_as_one_entity(self):
        """The unit is the affected ENTITY, not the surplus fact: the answer
        the user needs is how many entities are wrong, and the standing
        decision is the same at two values as at three."""
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
                ("uuid-a", ":introduced-by", ":commit/ccc"),
            )
        )
        assert result["entities"] == 1
        assert result["sample"][0][1] == [":commit/aaa", ":commit/bbb", ":commit/ccc"]

    def test_two_affected_entities_count_separately(self):
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
                ("uuid-b", ":introduced-by", ":commit/ccc"),
                ("uuid-b", ":introduced-by", ":commit/ddd"),
            )
        )
        assert result["entities"] == 2


class TestLegitimatelyMultiValuedAttributesAreNotSwept:
    """The one way this check could be catastrophically wrong.

    `:contains`, `:modified-in` and `:depends-on` hold many values per entity
    BY DESIGN -- a module contains every function in it. A duplicate detector
    that keyed on "entity has two values for an attribute" rather than on
    `:introduced-by` specifically would report every module in every healthy
    graph and condemn it.
    """

    def test_a_module_containing_many_functions_is_not_reported(self):
        result = introduced_by_duplicates(
            _facts(
                ("uuid-mod", ":contains", ":function/f1"),
                ("uuid-mod", ":contains", ":function/f2"),
                ("uuid-mod", ":contains", ":function/f3"),
            )
        )
        assert result["entities"] == 0

    def test_many_modified_in_edges_are_not_reported(self):
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":modified-in", ":commit/aaa"),
                ("uuid-a", ":modified-in", ":commit/bbb"),
            )
        )
        assert result["entities"] == 0

    def test_the_corruption_is_still_found_alongside_them(self):
        """The other half: narrowing to :introduced-by must not narrow so far
        that a real duplicate sitting next to legitimate multi-valued facts
        goes unseen."""
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":contains", ":function/f1"),
                ("uuid-a", ":contains", ":function/f2"),
                ("uuid-a", ":modified-in", ":commit/xxx"),
                ("uuid-a", ":modified-in", ":commit/yyy"),
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
            )
        )
        assert result["entities"] == 1


class TestTheSampleNamesTheEntity:
    def test_the_sample_uses_the_entitys_ident_when_the_scan_carries_one(self):
        """A raw UUID is useless to someone deciding whether to rebuild. Every
        code entity carries a self-referencing `[ident :ident "ident"]` fact
        (_build_code_triples), and it is in the same Counter, so naming the
        entity costs nothing extra."""
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":ident", ":function/mcp_server.py::foo"),
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
            )
        )
        assert result["sample"] == [
            [":function/mcp_server.py::foo", [":commit/aaa", ":commit/bbb"]]
        ]

    def test_an_entity_with_no_ident_fact_falls_back_to_the_raw_entity(self):
        """Not every entity that can hold :introduced-by is guaranteed to
        carry an :ident -- a partial write is exactly the state this audit
        exists to find, and dropping such an entity from the sample would hide
        the worst case."""
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
            )
        )
        assert result["sample"] == [["uuid-a", [":commit/aaa", ":commit/bbb"]]]
        assert result["entities"] == 1

    def test_the_values_are_sorted(self):
        """minigraf imposes no ordering on query results
        (_entity_introduced_by_values_query: "in the backend's unspecified
        order"), and a Counter's iteration order is insertion order. Sorting
        is what makes a recorded sample comparable across two runs."""
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":introduced-by", ":commit/zzz"),
                ("uuid-a", ":introduced-by", ":commit/aaa"),
            )
        )
        assert result["sample"][0][1] == [":commit/aaa", ":commit/zzz"]

    def test_the_sample_is_capped_but_the_count_is_not(self):
        """A corrupt at-scale graph can hold thousands of ambiguous entities
        (#235's own log cap exists for that reason). The COUNT is the number
        the decision rests on, so it must stay exact."""
        triples = []
        for i in range(20):
            triples.append((f"uuid-{i:02d}", ":introduced-by", ":commit/aaa"))
            triples.append((f"uuid-{i:02d}", ":introduced-by", ":commit/bbb"))
        result = introduced_by_duplicates(_facts(*triples), sample_cap=5)
        assert result["entities"] == 20
        assert len(result["sample"]) == 5


class TestRepeatsOfOneValue:
    def test_the_same_value_twice_is_not_the_two_value_corruption(self):
        """#235 is two DIFFERENT commits, and the Counter's key already
        collapses identical (entity, attribute, value) triples into one key
        with a count. Reading the count instead of the number of distinct
        values would report an entity whose only defect is a duplicated
        identical fact -- a different problem, with a different answer."""
        result = introduced_by_duplicates(
            Counter({("uuid-a", ":introduced-by", ":commit/aaa"): 2})
        )
        assert result["entities"] == 0


class TestOrphanEntitiesAreDetected:
    """#316: the ABSENT case, which every existing gate reads as clean.

    A live entity holding ZERO `:introduced-by` values is the opposite defect
    from #235's two, and `introduced_by_duplicates` skips it by construction
    (`if len(values) < 2: continue`). It is invisible to `fact_audit`'s
    `divergence` too -- the index is missing exactly what the graph is
    missing, so the two witnesses agree perfectly about a graph that is wrong.
    """

    def test_a_live_code_entity_with_no_introduced_by_is_reported(self):
        result = entities_without_introduced_by(
            _facts(
                ("uuid-a", ":ident", ":module/mcp_server.py"),
                ("uuid-a", ":entity-type", ":type/module"),
                ("uuid-a", ":description", "mcp_server.py"),
            )
        )
        assert result["entities"] == 1

    def test_a_healthy_code_entity_is_not_reported(self):
        result = entities_without_introduced_by(
            _facts(
                ("uuid-a", ":ident", ":module/mcp_server.py"),
                ("uuid-a", ":entity-type", ":type/module"),
                ("uuid-a", ":introduced-by", ":commit/aaa"),
            )
        )
        assert result["entities"] == 0
        assert result["sample"] == []

    def test_every_code_entity_type_is_covered(self):
        """All five types `_build_code_triples` writes an `:introduced-by`
        for. A set that silently omitted one would report clean for every
        orphaned function in a graph while looking like it covered them."""
        triples = []
        for i, kind in enumerate(
            ("module", "function", "class", "variable", "field")
        ):
            triples.append((f"uuid-{i}", ":ident", f":{kind}/x"))
            triples.append((f"uuid-{i}", ":entity-type", f":type/{kind}"))
        result = entities_without_introduced_by(_facts(*triples))
        assert result["entities"] == 5

    def test_two_orphans_count_separately(self):
        result = entities_without_introduced_by(
            _facts(
                ("uuid-a", ":ident", ":module/a.py"),
                ("uuid-a", ":entity-type", ":type/module"),
                ("uuid-b", ":ident", ":module/b.py"),
                ("uuid-b", ":entity-type", ":type/module"),
            )
        )
        assert result["entities"] == 2


class TestEntitiesThatLegitimatelyHaveNoIntroducedBy:
    """The one way this check could be catastrophically wrong.

    Several entity types never carry an `:introduced-by` in a perfectly
    healthy graph. An unnarrowed check would report every one of them and the
    at-scale gate would be permanently red -- the same trap
    `introduced_by_duplicates` avoided by narrowing to one attribute.
    """

    def test_an_unresolved_import_stub_is_not_reported(self):
        """The expensive one, and the reason `:type/external-dependency` is
        NOT in the code-entity set. `_forward_apply`'s dep-edge handling
        creates an unresolved-import stub with exactly three triples --
        `:entity-type`, `:ident`, `:description` -- and never an
        `:introduced-by` (mcp_server.py, the `not is_resolved and not
        is_relative` branch); `_build_close_triples` documents the same thing
        from the close side. Only the submodule branch writes one. Including
        the type would condemn every graph with an unresolvable import.
        """
        result = entities_without_introduced_by(
            _facts(
                ("uuid-a", ":entity-type", ":type/external-dependency"),
                ("uuid-a", ":ident", ":module/numpy"),
                ("uuid-a", ":description", "numpy"),
            )
        )
        assert result["entities"] == 0

    def test_bookkeeping_entity_types_are_not_reported(self):
        """`:type/commit`, `:type/ingestion`, `:type/ingest-interval`,
        `:type/lineage-marker` and `:type/candidate-diff` all exist in a
        healthy graph with a live `:ident` and no lineage of their own."""
        triples = []
        for i, kind in enumerate(
            ("commit", "ingestion", "ingest-interval", "lineage-marker",
             "candidate-diff")
        ):
            triples.append((f"uuid-{i}", ":ident", f":{kind}/x"))
            triples.append((f"uuid-{i}", ":entity-type", f":type/{kind}"))
        result = entities_without_introduced_by(_facts(*triples))
        assert result["entities"] == 0

    def test_memory_entity_types_are_not_reported(self):
        """`:type/decision` and friends are user-authored memory facts. They
        have no commit to be introduced by."""
        result = entities_without_introduced_by(
            _facts(
                ("uuid-a", ":ident", ":decision/cache"),
                ("uuid-a", ":entity-type", ":type/decision"),
                ("uuid-a", ":description", "use Redis"),
            )
        )
        assert result["entities"] == 0

    def test_the_orphan_is_still_found_alongside_them(self):
        """The other half: narrowing must not narrow so far that a real orphan
        sitting next to legitimately lineage-free entities goes unseen."""
        result = entities_without_introduced_by(
            _facts(
                ("uuid-dep", ":ident", ":module/numpy"),
                ("uuid-dep", ":entity-type", ":type/external-dependency"),
                ("uuid-commit", ":ident", ":commit/aaa"),
                ("uuid-commit", ":entity-type", ":type/commit"),
                ("uuid-a", ":ident", ":module/mcp_server.py"),
                ("uuid-a", ":entity-type", ":type/module"),
            )
        )
        assert result["entities"] == 1
        assert result["sample"] == [":module/mcp_server.py"]


class TestLivenessIsRequired:
    """A CLOSED entity has no lineage and is not a defect.

    `_build_close_triples` retracts `:ident`, `:entity-type` and
    `:introduced-by` in one triple list, but `close_entity_type` and
    `introduced_by` are each opt-in. So the reachable partial-close states are
    "live `:entity-type`, no `:ident`" and "live `:introduced-by`, no
    `:ident`" -- both excluded by requiring a live `:ident`. There is no site
    that retracts `:introduced-by` while leaving `:ident` live.
    """

    def test_an_entity_type_with_no_live_ident_is_not_reported(self):
        result = entities_without_introduced_by(
            _facts(("uuid-a", ":entity-type", ":type/module"))
        )
        assert result["entities"] == 0

    def test_an_ident_with_no_live_entity_type_is_not_reported(self):
        """The other partial close. The ident prefix is NOT read as a type:
        `_build_close_triples` documents that unresolved-import stubs reuse
        the "module" ident prefix while being `:type/external-dependency`, so
        deriving a type from the prefix would report exactly the entities the
        class above exists to exclude."""
        result = entities_without_introduced_by(
            _facts(("uuid-a", ":ident", ":module/mcp_server.py"))
        )
        assert result["entities"] == 0


class TestTheScannedDenominatorIsReported:
    """The positive control, IN THE ARTIFACT rather than in one afternoon's
    measurement.

    docs/design-notes.md's standing requirement for a zero-tolerance gate is that its
    clean baseline is measured AND its positive control checked -- a check
    that matched no entities at all also reports 0. Reporting the denominator
    means every future run re-proves the check scanned something, instead of
    that being a fact about the day it was wired.
    """

    def test_healthy_entities_raise_the_denominator(self):
        result = entities_without_introduced_by(
            _facts(
                ("uuid-a", ":ident", ":module/a.py"),
                ("uuid-a", ":entity-type", ":type/module"),
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-b", ":ident", ":function/a.py::f"),
                ("uuid-b", ":entity-type", ":type/function"),
                ("uuid-b", ":introduced-by", ":commit/aaa"),
            )
        )
        assert result["entities"] == 0
        assert result["code_entities_scanned"] == 2

    def test_orphans_are_counted_in_the_denominator_too(self):
        """`entities` is a subset of `code_entities_scanned`, not a disjoint
        tally: the reading that matters is "0 of N", and N must include the
        offenders or a wholly corrupt graph would report 0 of 0."""
        result = entities_without_introduced_by(
            _facts(
                ("uuid-a", ":ident", ":module/a.py"),
                ("uuid-a", ":entity-type", ":type/module"),
            )
        )
        assert result["entities"] == 1
        assert result["code_entities_scanned"] == 1

    def test_a_graph_with_no_code_entities_reports_a_zero_denominator(self):
        """The state the denominator exists to distinguish from a clean one.
        Both report `entities: 0`; only this one reports scanning nothing."""
        result = entities_without_introduced_by(
            _facts(
                ("uuid-a", ":ident", ":commit/aaa"),
                ("uuid-a", ":entity-type", ":type/commit"),
            )
        )
        assert result["entities"] == 0
        assert result["code_entities_scanned"] == 0

    def test_non_code_entities_do_not_raise_the_denominator(self):
        result = entities_without_introduced_by(
            _facts(
                ("uuid-a", ":ident", ":module/numpy"),
                ("uuid-a", ":entity-type", ":type/external-dependency"),
                ("uuid-b", ":ident", ":decision/cache"),
                ("uuid-b", ":entity-type", ":type/decision"),
            )
        )
        assert result["code_entities_scanned"] == 0


class TestTheOrphanSampleNamesTheEntity:
    def test_the_sample_uses_the_entitys_ident(self):
        result = entities_without_introduced_by(
            _facts(
                ("uuid-a", ":ident", ":function/mcp_server.py::foo"),
                ("uuid-a", ":entity-type", ":type/function"),
            )
        )
        assert result["sample"] == [":function/mcp_server.py::foo"]

    def test_the_sample_is_sorted(self):
        """A Counter's iteration order is insertion order, which is minigraf's
        unspecified result order. Sorting is what makes a recorded sample
        comparable across two runs."""
        result = entities_without_introduced_by(
            _facts(
                ("uuid-z", ":ident", ":module/z.py"),
                ("uuid-z", ":entity-type", ":type/module"),
                ("uuid-a", ":ident", ":module/a.py"),
                ("uuid-a", ":entity-type", ":type/module"),
            )
        )
        assert result["sample"] == [":module/a.py", ":module/z.py"]

    def test_the_sample_is_capped_but_the_count_is_not(self):
        triples = []
        for i in range(20):
            triples.append((f"uuid-{i:02d}", ":ident", f":module/m{i:02d}.py"))
            triples.append((f"uuid-{i:02d}", ":entity-type", ":type/module"))
        result = entities_without_introduced_by(_facts(*triples), sample_cap=5)
        assert result["entities"] == 20
        assert result["code_entities_scanned"] == 20
        assert len(result["sample"]) == 5

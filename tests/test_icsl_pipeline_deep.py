"""icsl_tools deep integration + pipeline caller-attribution — architecture.md §3.

Tests write-edge lifecycle with RepairLog, gate mechanism with real BFS,
and the pipeline's overloaded-function misattribution bug with CastEngine data.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from codemap_lite.agent.icsl_tools import (
    check_complete,
    query_reachable,
    write_edge,
)
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    SourcePointNode,
    UnresolvedCallNode,
)
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator


CASTENGINE_DIR = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")


# ---------------------------------------------------------------------------
# write-edge deep integration
# ---------------------------------------------------------------------------


class TestWriteEdgeDeep:
    """architecture.md §3: write-edge atomicity and side effects."""

    @pytest.fixture
    def store(self):
        s = InMemoryGraphStore()
        s.create_function(FunctionNode(
            id="fn_a", name="A", signature="void A()",
            file_path="a.cpp", start_line=1, end_line=20, body_hash="ha",
        ))
        s.create_function(FunctionNode(
            id="fn_b", name="B", signature="void B()",
            file_path="b.cpp", start_line=1, end_line=15, body_hash="hb",
        ))
        s.create_function(FunctionNode(
            id="fn_c", name="C", signature="void C()",
            file_path="c.cpp", start_line=1, end_line=10, body_hash="hc",
        ))
        # Chain: a -> b (resolved), b -> ??? (UC)
        s.create_calls_edge("fn_a", "fn_b", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="a.cpp", call_line=10,
        ))
        s.create_unresolved_call(UnresolvedCallNode(
            id="uc_1", caller_id="fn_b", call_expression="dispatch()",
            call_file="b.cpp", call_line=8, call_type="indirect",
            source_code_snippet="ptr->dispatch()", var_name="ptr", var_type="Base*",
            candidates=["fn_c"],
        ))
        s.create_source_point(SourcePointNode(
            id="fn_a", function_id="fn_a",
            entry_point_kind="entry", reason="test", status="running",
        ))
        return s

    def test_write_edge_then_gate_passes(self, store):
        """Full lifecycle: resolve gap → gate passes."""
        assert check_complete("fn_a", store)["complete"] is False
        write_edge("fn_b", "fn_c", "indirect", "b.cpp", 8, store,
                   reasoning_summary="ptr is C")
        assert check_complete("fn_a", store)["complete"] is True

    def test_write_edge_creates_repair_log_with_timestamp(self, store):
        write_edge("fn_b", "fn_c", "indirect", "b.cpp", 8, store,
                   llm_response="raw", reasoning_summary="because")
        logs = store.get_repair_logs()
        assert len(logs) == 1
        assert logs[0].timestamp  # Non-empty ISO timestamp
        assert "T" in logs[0].timestamp  # ISO format

    def test_write_edge_repair_log_call_location_format(self, store):
        """architecture.md §4: call_location = 'file:line'."""
        write_edge("fn_b", "fn_c", "indirect", "b.cpp", 8, store)
        logs = store.get_repair_logs()
        assert logs[0].call_location == "b.cpp:8"

    def test_write_edge_reasoning_exactly_200_not_truncated(self, store):
        summary = "x" * 200
        write_edge("fn_b", "fn_c", "indirect", "b.cpp", 8, store,
                   reasoning_summary=summary)
        logs = store.get_repair_logs()
        assert logs[0].reasoning_summary == summary
        assert len(logs[0].reasoning_summary) == 200

    def test_new_uc_appears_after_edge_extends_reachability(self, store):
        """Resolving a gap may reveal new reachable UCs (BFS extends)."""
        # Add UC on fn_c — only reachable after b->c edge
        store.create_unresolved_call(UnresolvedCallNode(
            id="uc_deep", caller_id="fn_c", call_expression="deep()",
            call_file="c.cpp", call_line=5, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        ))
        # Before: uc_deep not in pending (fn_c not reachable from fn_a)
        result_before = check_complete("fn_a", store)
        assert "uc_deep" not in result_before["pending_gap_ids"]

        # Resolve b->c
        write_edge("fn_b", "fn_c", "indirect", "b.cpp", 8, store)

        # After: uc_deep IS pending (fn_c now reachable)
        result_after = check_complete("fn_a", store)
        assert result_after["complete"] is False
        assert "uc_deep" in result_after["pending_gap_ids"]


# ---------------------------------------------------------------------------
# CastEngine: pipeline caller-attribution bug
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def castengine_store():
    if not CASTENGINE_DIR.exists():
        pytest.skip("CastEngine directory not available")
    store = InMemoryGraphStore()
    orch = PipelineOrchestrator(store=store, target_dir=CASTENGINE_DIR)
    orch.run_full_analysis()
    return store


class TestCallerAttributionBug:
    """BUG: by_file_name[(file, name)] = id overwrites overloaded functions.

    201 (file, name) pairs have multiple definitions in CastEngine.
    The pipeline only stores the LAST one's ID, causing 200 edges to be
    attributed to the wrong function instance.
    """

    def test_overloaded_pairs_count(self, castengine_store):
        """CastEngine has 201+ overloaded (file, name) pairs."""
        file_name_count: dict[tuple[str, str], int] = defaultdict(int)
        for fn in castengine_store.list_functions():
            file_name_count[(fn.file_path, fn.name)] += 1
        overloaded = sum(1 for v in file_name_count.values() if v > 1)
        assert overloaded >= 200

    def test_misattributed_edge_percentage(self, castengine_store):
        """Misattributed edges should be ≤ 4.5% (known bug baseline)."""
        fns = {fn.id: fn for fn in castengine_store.list_functions()}
        edges = castengine_store.list_calls_edges()
        total = 0
        outside = 0
        for e in edges:
            caller = fns.get(e.caller_id)
            if caller:
                total += 1
                if not (caller.start_line <= e.props.call_line <= caller.end_line):
                    outside += 1
        pct = outside / total * 100
        assert pct <= 4.5, f"Misattribution rate: {pct:.1f}%"

    def test_uc_misattribution_rate(self, castengine_store):
        """UCs also affected by the same bug — document baseline."""
        fns = {fn.id: fn for fn in castengine_store.list_functions()}
        ucs = castengine_store.get_unresolved_calls()
        total = 0
        outside = 0
        for uc in ucs:
            caller = fns.get(uc.caller_id)
            if caller:
                total += 1
                if not (caller.start_line <= uc.call_line <= caller.end_line):
                    outside += 1
        pct = outside / total * 100 if total > 0 else 0
        # UCs have the same bug — document it
        assert pct <= 5.0, f"UC misattribution rate: {pct:.1f}%"


# ---------------------------------------------------------------------------
# CastEngine: edge/UC consistency
# ---------------------------------------------------------------------------


class TestEdgeUCConsistency:
    """No function should have both an edge AND a UC at the same call site.

    BUG: Pipeline creates BOTH a CALLS edge AND an UnresolvedCall for ~1026
    call sites. Root cause: plugin.build_calls() returns the same call in both
    the `calls` list (resolved direct) and `unresolved` list (ambiguous/virtual),
    and the pipeline's second pass processes both without deduplication.

    Breakdown of 1026 conflicts:
    - 934 are 'direct' UCs (parser reports as both resolved AND unresolved)
    - 85 are 'virtual' UCs
    - 7 are 'indirect' UCs
    """

    def test_edge_uc_conflict_count_baseline(self, castengine_store):
        """Document the known edge+UC conflict count (should decrease as bug is fixed)."""
        edges = castengine_store.list_calls_edges()
        ucs = castengine_store.get_unresolved_calls()

        # Build set of (caller_id, call_file, call_line) from edges
        edge_sites: set[tuple[str, str, int]] = set()
        for e in edges:
            edge_sites.add((e.caller_id, e.props.call_file, e.props.call_line))

        # Check UCs against edge sites
        conflicts = []
        for uc in ucs:
            key = (uc.caller_id, uc.call_file, uc.call_line)
            if key in edge_sites:
                conflicts.append(uc)

        # KNOWN BUG: ~1026 conflicts. Should be 0 when fixed.
        # This test documents the baseline — should not get WORSE.
        assert len(conflicts) <= 1100, (
            f"Edge+UC conflicts increased: {len(conflicts)} (baseline ~1026)"
        )
        # At least some exist (documenting the bug)
        assert len(conflicts) > 900, (
            f"Only {len(conflicts)} conflicts — bug may be partially fixed, update baseline"
        )

    def test_conflict_uc_types_are_mostly_direct(self, castengine_store):
        """Most conflicts are 'direct' UCs — parser double-reports direct calls."""
        edges = castengine_store.list_calls_edges()
        ucs = castengine_store.get_unresolved_calls()

        edge_sites: set[tuple[str, str, int]] = set()
        for e in edges:
            edge_sites.add((e.caller_id, e.props.call_file, e.props.call_line))

        conflict_types: dict[str, int] = defaultdict(int)
        for uc in ucs:
            key = (uc.caller_id, uc.call_file, uc.call_line)
            if key in edge_sites:
                conflict_types[uc.call_type] += 1

        # 'direct' should be the dominant conflict type (>85%)
        total_conflicts = sum(conflict_types.values())
        direct_pct = conflict_types.get("direct", 0) / total_conflicts * 100
        assert direct_pct > 80, (
            f"Only {direct_pct:.1f}% of conflicts are 'direct' — unexpected distribution: {dict(conflict_types)}"
        )


# ---------------------------------------------------------------------------
# CastEngine: stats consistency
# ---------------------------------------------------------------------------


class TestStatsConsistency:
    """count_stats should match actual graph contents."""

    def test_stats_match_actual_counts(self, castengine_store):
        stats = castengine_store.count_stats()
        actual_fns = len(castengine_store.list_functions())
        actual_files = len(castengine_store.list_files())
        actual_edges = len(castengine_store.list_calls_edges())
        actual_ucs = len(castengine_store.get_unresolved_calls())

        assert stats["total_functions"] == actual_fns
        assert stats["total_files"] == actual_files
        assert stats["total_calls"] == actual_edges
        assert stats["total_unresolved"] == actual_ucs

    def test_resolved_by_buckets_sum_to_total(self, castengine_store):
        stats = castengine_store.count_stats()
        by_resolved = stats["calls_by_resolved_by"]
        total_from_buckets = sum(by_resolved.values())
        assert total_from_buckets == stats["total_calls"]

    def test_call_type_buckets_sum_to_total(self, castengine_store):
        stats = castengine_store.count_stats()
        by_type = stats["calls_by_call_type"]
        total_from_buckets = sum(by_type.values())
        assert total_from_buckets == stats["total_calls"]

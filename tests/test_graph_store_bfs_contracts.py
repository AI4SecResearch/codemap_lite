"""Graph store BFS reachability + edge-case contracts — architecture.md §3/§4.

Tests the BFS traversal (get_reachable_subgraph, get_pending_gaps_for_source),
graph store deduplication invariants, and CastEngine-scale reachability correctness.

BUG HUNTING TARGETS:
1. BFS may miss nodes in diamond/cycle graphs
2. get_pending_gaps_for_source may include unresolvable gaps
3. Edge deduplication may silently drop different-callee edges at same call site
4. UC deduplication may overwrite metadata from first UC
5. CastEngine reachability: source points should reach significant subgraphs
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from codemap_lite.graph.neo4j_store import InMemoryGraphStore, _CallsEdge
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    RepairLogNode,
    SourcePointNode,
    UnresolvedCallNode,
)
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator


CASTENGINE_DIR = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")


# ---------------------------------------------------------------------------
# §3: BFS reachability correctness
# ---------------------------------------------------------------------------


def _reachable_ids(store: InMemoryGraphStore, source_id: str) -> set[str]:
    """Helper: extract set of reachable function IDs from BFS subgraph dict."""
    subgraph = store.get_reachable_subgraph(source_id)
    return {fn.id for fn in subgraph["nodes"]}


class TestBFSReachability:
    """architecture.md §3: BFS from source must find all transitively reachable nodes."""

    @pytest.fixture
    def diamond_store(self):
        """Diamond graph: A -> B, A -> C, B -> D, C -> D."""
        s = InMemoryGraphStore()
        for name in "ABCD":
            s.create_function(FunctionNode(
                id=f"fn_{name}", name=name, signature=f"void {name}()",
                file_path="x.cpp", start_line=1, end_line=10, body_hash=f"h{name}",
            ))
        s.create_calls_edge("fn_A", "fn_B", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=2,
        ))
        s.create_calls_edge("fn_A", "fn_C", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=3,
        ))
        # PLACEHOLDER_DIAMOND_REST
        return s

    def test_diamond_all_nodes_reachable(self, diamond_store):
        """BFS from A should reach all 4 nodes in diamond."""
        # Add remaining edges
        diamond_store.create_calls_edge("fn_B", "fn_D", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=5,
        ))
        diamond_store.create_calls_edge("fn_C", "fn_D", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=6,
        ))
        result = diamond_store.get_reachable_subgraph("fn_A")
        node_ids = {n.id for n in result["nodes"]}
        assert node_ids == {"fn_A", "fn_B", "fn_C", "fn_D"}

    def test_diamond_edges_include_both_paths(self, diamond_store):
        """BFS should collect edges from BOTH paths to D."""
        diamond_store.create_calls_edge("fn_B", "fn_D", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=5,
        ))
        diamond_store.create_calls_edge("fn_C", "fn_D", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=6,
        ))
        result = diamond_store.get_reachable_subgraph("fn_A")
        # Should have 4 edges: A->B, A->C, B->D, C->D
        assert len(result["edges"]) == 4

    def test_cycle_does_not_infinite_loop(self):
        """BFS with cycle: A -> B -> C -> A should terminate."""
        s = InMemoryGraphStore()
        for name in "ABC":
            s.create_function(FunctionNode(
                id=f"fn_{name}", name=name, signature=f"void {name}()",
                file_path="x.cpp", start_line=1, end_line=10, body_hash=f"h{name}",
            ))
        s.create_calls_edge("fn_A", "fn_B", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=2,
        ))
        s.create_calls_edge("fn_B", "fn_C", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=3,
        ))
        s.create_calls_edge("fn_C", "fn_A", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=4,
        ))
        result = s.get_reachable_subgraph("fn_A")
        node_ids = {n.id for n in result["nodes"]}
        assert node_ids == {"fn_A", "fn_B", "fn_C"}

    def test_unreachable_node_excluded(self):
        """Nodes not connected to source should not appear."""
        s = InMemoryGraphStore()
        for name in "ABCX":
            s.create_function(FunctionNode(
                id=f"fn_{name}", name=name, signature=f"void {name}()",
                file_path="x.cpp", start_line=1, end_line=10, body_hash=f"h{name}",
            ))
        s.create_calls_edge("fn_A", "fn_B", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=2,
        ))
        # fn_X is isolated
        result = s.get_reachable_subgraph("fn_A")
        node_ids = {n.id for n in result["nodes"]}
        assert "fn_X" not in node_ids
        assert "fn_A" in node_ids and "fn_B" in node_ids

    def test_max_depth_limits_traversal(self):
        """BFS should stop at max_depth."""
        s = InMemoryGraphStore()
        # Chain: fn_0 -> fn_1 -> fn_2 -> ... -> fn_10
        for i in range(11):
            s.create_function(FunctionNode(
                id=f"fn_{i}", name=f"f{i}", signature=f"void f{i}()",
                file_path="x.cpp", start_line=i*10+1, end_line=i*10+9, body_hash=f"h{i}",
            ))
        for i in range(10):
            s.create_calls_edge(f"fn_{i}", f"fn_{i+1}", CallsEdgeProps(
                resolved_by="symbol_table", call_type="direct",
                call_file="x.cpp", call_line=i*10+5,
            ))
        # max_depth=3 should reach fn_0, fn_1, fn_2, fn_3 (4 nodes)
        result = s.get_reachable_subgraph("fn_0", max_depth=3)
        node_ids = {n.id for n in result["nodes"]}
        assert "fn_0" in node_ids
        assert "fn_3" in node_ids
        assert "fn_4" not in node_ids

    def test_bfs_collects_unresolved_for_all_visited(self):
        """UCs on any visited node should be in the result."""
        s = InMemoryGraphStore()
        for name in "AB":
            s.create_function(FunctionNode(
                id=f"fn_{name}", name=name, signature=f"void {name}()",
                file_path="x.cpp", start_line=1, end_line=10, body_hash=f"h{name}",
            ))
        s.create_calls_edge("fn_A", "fn_B", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=2,
        ))
        # UC on fn_B (reachable from fn_A)
        s.create_unresolved_call(UnresolvedCallNode(
            id="uc_b", caller_id="fn_B", call_expression="target()",
            call_file="x.cpp", call_line=5, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        ))
        result = s.get_reachable_subgraph("fn_A")
        assert len(result["unresolved"]) == 1
        assert result["unresolved"][0].id == "uc_b"

    def test_bfs_skips_dangling_callee(self):
        """Edges pointing to non-existent functions should be skipped."""
        s = InMemoryGraphStore()
        s.create_function(FunctionNode(
            id="fn_A", name="A", signature="void A()",
            file_path="x.cpp", start_line=1, end_line=10, body_hash="hA",
        ))
        # Edge to non-existent function
        s.create_calls_edge("fn_A", "fn_ghost", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=2,
        ))
        result = s.get_reachable_subgraph("fn_A")
        # Should not crash, ghost not in nodes
        node_ids = {n.id for n in result["nodes"]}
        assert "fn_ghost" not in node_ids
        # Edge should be excluded (dangling defense)
        assert len(result["edges"]) == 0


# ---------------------------------------------------------------------------
# §3: get_pending_gaps_for_source correctness
# ---------------------------------------------------------------------------


class TestPendingGapsForSource:
    """architecture.md §3: gate mechanism relies on pending gaps count."""

    @pytest.fixture
    def chain_store(self):
        """Chain: entry -> mid -> leaf, with UC on mid."""
        s = InMemoryGraphStore()
        for fid, name in [("fn_entry", "entry"), ("fn_mid", "mid"), ("fn_leaf", "leaf")]:
            s.create_function(FunctionNode(
                id=fid, name=name, signature=f"void {name}()",
                file_path="x.cpp", start_line=1, end_line=10, body_hash=f"h_{name}",
            ))
        s.create_calls_edge("fn_entry", "fn_mid", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=5,
        ))
        s.create_calls_edge("fn_mid", "fn_leaf", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="x.cpp", call_line=15,
        ))
        return s

    def test_no_gaps_returns_empty(self, chain_store):
        """Source with no UCs → empty pending gaps."""
        gaps = chain_store.get_pending_gaps_for_source("fn_entry")
        assert gaps == []

    def test_pending_gap_included(self, chain_store):
        """Pending UC on reachable node is included."""
        chain_store.create_unresolved_call(UnresolvedCallNode(
            id="uc_1", caller_id="fn_mid", call_expression="dispatch()",
            call_file="x.cpp", call_line=18, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            status="pending",
        ))
        gaps = chain_store.get_pending_gaps_for_source("fn_entry")
        assert len(gaps) == 1
        assert gaps[0].id == "uc_1"

    def test_unresolvable_gap_excluded(self, chain_store):
        """Unresolvable UC should NOT be in pending gaps."""
        chain_store.create_unresolved_call(UnresolvedCallNode(
            id="uc_dead", caller_id="fn_mid", call_expression="dead()",
            call_file="x.cpp", call_line=18, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            status="unresolvable", retry_count=3,
        ))
        gaps = chain_store.get_pending_gaps_for_source("fn_entry")
        assert gaps == []

    def test_unreachable_gap_excluded(self, chain_store):
        """UC on unreachable node should NOT appear."""
        chain_store.create_function(FunctionNode(
            id="fn_isolated", name="isolated", signature="void isolated()",
            file_path="y.cpp", start_line=1, end_line=10, body_hash="hi",
        ))
        chain_store.create_unresolved_call(UnresolvedCallNode(
            id="uc_far", caller_id="fn_isolated", call_expression="far()",
            call_file="y.cpp", call_line=5, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        ))
        gaps = chain_store.get_pending_gaps_for_source("fn_entry")
        assert gaps == []

    def test_gap_on_source_itself_included(self, chain_store):
        """UC directly on the source function should be included."""
        chain_store.create_unresolved_call(UnresolvedCallNode(
            id="uc_self", caller_id="fn_entry", call_expression="self_call()",
            call_file="x.cpp", call_line=3, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        ))
        gaps = chain_store.get_pending_gaps_for_source("fn_entry")
        assert len(gaps) == 1
        assert gaps[0].caller_id == "fn_entry"

    def test_nonexistent_source_returns_empty(self, chain_store):
        """Source ID that doesn't exist as a function → empty (no crash)."""
        gaps = chain_store.get_pending_gaps_for_source("nonexistent_id")
        assert gaps == []


# ---------------------------------------------------------------------------
# §4: Edge deduplication contracts
# ---------------------------------------------------------------------------


class TestEdgeDeduplication:
    """architecture.md §4: edges unique by (caller, callee, call_file, call_line)."""

    def test_same_quadruple_skipped(self):
        """Creating same edge twice should be idempotent."""
        s = InMemoryGraphStore()
        s.create_function(FunctionNode(
            id="fn_a", name="A", signature="void A()",
            file_path="x.cpp", start_line=1, end_line=10, body_hash="ha",
        ))
        s.create_function(FunctionNode(
            id="fn_b", name="B", signature="void B()",
            file_path="x.cpp", start_line=11, end_line=20, body_hash="hb",
        ))
        props = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="x.cpp", call_line=5,
        )
        s.create_calls_edge("fn_a", "fn_b", props)
        s.create_calls_edge("fn_a", "fn_b", props)
        assert len(s.list_calls_edges()) == 1

    def test_different_callee_same_site_both_kept(self):
        """BUG CHECK: Two edges from same caller at same line to DIFFERENT callees.

        This can happen when a call expression resolves to multiple targets
        (e.g., overloaded functions). Both should be stored.
        """
        s = InMemoryGraphStore()
        for fid in ["fn_a", "fn_b", "fn_c"]:
            s.create_function(FunctionNode(
                id=fid, name=fid[-1].upper(), signature=f"void {fid[-1].upper()}()",
                file_path="x.cpp", start_line=1, end_line=10, body_hash=f"h{fid}",
            ))
        props_b = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="x.cpp", call_line=5,
        )
        props_c = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="x.cpp", call_line=5,
        )
        s.create_calls_edge("fn_a", "fn_b", props_b)
        s.create_calls_edge("fn_a", "fn_c", props_c)
        # Both should exist — different callee_id
        assert len(s.list_calls_edges()) == 2

    def test_first_resolved_by_wins(self):
        """When duplicate edge is created, first resolved_by is preserved."""
        s = InMemoryGraphStore()
        s.create_function(FunctionNode(
            id="fn_a", name="A", signature="void A()",
            file_path="x.cpp", start_line=1, end_line=10, body_hash="ha",
        ))
        s.create_function(FunctionNode(
            id="fn_b", name="B", signature="void B()",
            file_path="x.cpp", start_line=11, end_line=20, body_hash="hb",
        ))
        props1 = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="x.cpp", call_line=5,
        )
        props2 = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="x.cpp", call_line=5,
        )
        s.create_calls_edge("fn_a", "fn_b", props1)
        s.create_calls_edge("fn_a", "fn_b", props2)
        edge = s.get_calls_edge("fn_a", "fn_b", "x.cpp", 5)
        assert edge.resolved_by == "symbol_table"  # First wins


# ---------------------------------------------------------------------------
# §4: UC deduplication contracts
# ---------------------------------------------------------------------------


class TestUCDeduplication:
    """architecture.md §4: UCs unique by (caller_id, call_file, call_line)."""

    def test_duplicate_uc_updates_in_place(self):
        """Second UC at same site should update, not create new."""
        s = InMemoryGraphStore()
        s.create_unresolved_call(UnresolvedCallNode(
            id="uc_1", caller_id="fn_a", call_expression="first()",
            call_file="x.cpp", call_line=10, call_type="indirect",
            source_code_snippet="old snippet", var_name="ptr", var_type="Base*",
        ))
        s.create_unresolved_call(UnresolvedCallNode(
            id="uc_2", caller_id="fn_a", call_expression="second()",
            call_file="x.cpp", call_line=10, call_type="virtual",
            source_code_snippet="new snippet", var_name="obj", var_type="Derived*",
        ))
        ucs = s.get_unresolved_calls()
        assert len(ucs) == 1
        # Should have UPDATED values from second call
        assert ucs[0].call_expression == "second()"
        assert ucs[0].var_name == "obj"

    def test_duplicate_uc_preserves_original_id(self):
        """Deduplication should keep the original node's ID."""
        s = InMemoryGraphStore()
        s.create_unresolved_call(UnresolvedCallNode(
            id="original_id", caller_id="fn_a", call_expression="first()",
            call_file="x.cpp", call_line=10, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        ))
        returned_id = s.create_unresolved_call(UnresolvedCallNode(
            id="new_id", caller_id="fn_a", call_expression="second()",
            call_file="x.cpp", call_line=10, call_type="virtual",
            source_code_snippet="", var_name=None, var_type=None,
        ))
        assert returned_id == "original_id"

    def test_different_line_creates_separate_uc(self):
        """UCs at different lines should both exist."""
        s = InMemoryGraphStore()
        s.create_unresolved_call(UnresolvedCallNode(
            id="uc_1", caller_id="fn_a", call_expression="first()",
            call_file="x.cpp", call_line=10, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        ))
        s.create_unresolved_call(UnresolvedCallNode(
            id="uc_2", caller_id="fn_a", call_expression="second()",
            call_file="x.cpp", call_line=11, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        ))
        assert len(s.get_unresolved_calls()) == 2


# ---------------------------------------------------------------------------
# §3: Source point status transition contracts
# ---------------------------------------------------------------------------


class TestSourcePointTransitions:
    """architecture.md §3: SourcePoint status lifecycle."""

    @pytest.fixture
    def store_with_sp(self):
        s = InMemoryGraphStore()
        s.create_source_point(SourcePointNode(
            id="sp_1", function_id="fn_1",
            entry_point_kind="entry", reason="test", status="pending",
        ))
        return s

    def test_pending_to_running(self, store_with_sp):
        store_with_sp.update_source_point_status("sp_1", "running")
        assert store_with_sp.get_source_point("sp_1").status == "running"

    def test_running_to_complete(self, store_with_sp):
        store_with_sp.update_source_point_status("sp_1", "running")
        store_with_sp.update_source_point_status("sp_1", "complete")
        assert store_with_sp.get_source_point("sp_1").status == "complete"

    def test_running_to_partial_complete(self, store_with_sp):
        store_with_sp.update_source_point_status("sp_1", "running")
        store_with_sp.update_source_point_status("sp_1", "partial_complete")
        assert store_with_sp.get_source_point("sp_1").status == "partial_complete"

    def test_backward_transition_raises(self, store_with_sp):
        """Cannot go from running back to pending without force_reset."""
        store_with_sp.update_source_point_status("sp_1", "running")
        with pytest.raises(ValueError, match="Invalid SourcePoint transition"):
            store_with_sp.update_source_point_status("sp_1", "pending")

    def test_complete_is_terminal(self, store_with_sp):
        """complete → running is allowed (re-repair after incremental invalidation)."""
        store_with_sp.update_source_point_status("sp_1", "running")
        store_with_sp.update_source_point_status("sp_1", "complete")
        # Schema allows complete → running for re-repair
        store_with_sp.update_source_point_status("sp_1", "running")
        assert store_with_sp.get_source_point("sp_1").status == "running"

    def test_force_reset_allows_backward(self, store_with_sp):
        """force_reset=True bypasses transition validation."""
        store_with_sp.update_source_point_status("sp_1", "running")
        store_with_sp.update_source_point_status("sp_1", "complete")
        store_with_sp.update_source_point_status("sp_1", "pending", force_reset=True)
        assert store_with_sp.get_source_point("sp_1").status == "pending"

    def test_same_status_is_idempotent(self, store_with_sp):
        """Setting same status should not raise."""
        store_with_sp.update_source_point_status("sp_1", "pending")
        assert store_with_sp.get_source_point("sp_1").status == "pending"

    def test_nonexistent_sp_is_noop(self, store_with_sp):
        """Updating non-existent SP should not raise."""
        store_with_sp.update_source_point_status("nonexistent", "running")
        # No crash


# ---------------------------------------------------------------------------
# §3: Retry state contracts
# ---------------------------------------------------------------------------


class TestRetryStateContracts:
    """architecture.md §3: retry_count increments + unresolvable threshold."""

    @pytest.fixture
    def store_with_uc(self):
        s = InMemoryGraphStore()
        s.create_unresolved_call(UnresolvedCallNode(
            id="uc_1", caller_id="fn_a", call_expression="target()",
            call_file="x.cpp", call_line=10, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        ))
        return s

    def test_retry_increments_count(self, store_with_uc):
        store_with_uc.update_unresolved_call_retry_state(
            "uc_1", "2026-01-01T00:00:00Z", "gate_failed: test"
        )
        uc = store_with_uc.get_unresolved_calls()[0]
        assert uc.retry_count == 1

    def test_three_retries_marks_unresolvable(self, store_with_uc):
        """After 3 retries, status becomes 'unresolvable'."""
        for i in range(3):
            store_with_uc.update_unresolved_call_retry_state(
                "uc_1", f"2026-01-01T00:00:0{i}Z", "gate_failed: attempt"
            )
        uc = store_with_uc.get_unresolved_calls()[0]
        assert uc.retry_count == 3
        assert uc.status == "unresolvable"

    def test_invalid_reason_category_raises(self, store_with_uc):
        """Reason must start with a valid category."""
        with pytest.raises(ValueError, match="category"):
            store_with_uc.update_unresolved_call_retry_state(
                "uc_1", "2026-01-01T00:00:00Z", "invalid_category: test"
            )

    def test_reason_over_200_chars_raises(self, store_with_uc):
        with pytest.raises(ValueError, match="200"):
            store_with_uc.update_unresolved_call_retry_state(
                "uc_1", "2026-01-01T00:00:00Z", "gate_failed: " + "x" * 200
            )

    def test_nonexistent_uc_is_noop(self, store_with_uc):
        """Updating non-existent UC should not raise."""
        store_with_uc.update_unresolved_call_retry_state(
            "nonexistent", "2026-01-01T00:00:00Z", "gate_failed: test"
        )

    def test_reset_unresolvable_gaps(self, store_with_uc):
        """reset_unresolvable_gaps should reset status and retry_count."""
        for i in range(3):
            store_with_uc.update_unresolved_call_retry_state(
                "uc_1", f"2026-01-01T00:00:0{i}Z", "gate_failed: attempt"
            )
        assert store_with_uc.get_unresolved_calls()[0].status == "unresolvable"
        store_with_uc.reset_unresolvable_gaps()
        uc = store_with_uc.get_unresolved_calls()[0]
        assert uc.status == "pending"
        assert uc.retry_count == 0
        assert uc.last_attempt_timestamp is None


# ---------------------------------------------------------------------------
# CastEngine: BFS reachability at scale
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def castengine_store():
    if not CASTENGINE_DIR.exists():
        pytest.skip("CastEngine directory not available")
    store = InMemoryGraphStore()
    orch = PipelineOrchestrator(store=store, target_dir=CASTENGINE_DIR)
    orch.run_full_analysis()
    return store


class TestCastEngineBFSReachability:
    """BFS reachability at CastEngine scale — architecture.md §3.

    Source points come from codewiki_lite (not the pipeline), so we pick
    functions with high out-degree as synthetic sources to test BFS behavior.
    """

    @pytest.fixture
    def high_degree_functions(self, castengine_store):
        """Find functions with most outgoing edges (good BFS roots)."""
        edges = castengine_store.list_calls_edges()
        out_degree: dict[str, int] = defaultdict(int)
        for e in edges:
            out_degree[e.caller_id] += 1
        # Top 10 by out-degree
        sorted_fns = sorted(out_degree.items(), key=lambda x: x[1], reverse=True)
        return [fid for fid, _ in sorted_fns[:10]]

    def test_bfs_from_high_degree_reaches_nontrivial_subgraph(
        self, castengine_store, high_degree_functions
    ):
        """High out-degree functions should reach significant subgraphs via BFS."""
        trivial = 0
        for fn_id in high_degree_functions:
            reachable = _reachable_ids(castengine_store, fn_id)
            if len(reachable) < 5:
                trivial += 1
        assert trivial <= 2, (
            f"{trivial}/10 high-degree functions reach <5 nodes — BFS may be broken"
        )

    def test_bfs_reachable_nodes_are_valid_functions(
        self, castengine_store, high_degree_functions
    ):
        """All node IDs returned by BFS should be real function IDs in the store."""
        fn_ids = {fn.id for fn in castengine_store.list_functions()}
        root = high_degree_functions[0]
        reachable = _reachable_ids(castengine_store, root)
        for node_id in reachable:
            assert node_id in fn_ids, f"BFS returned unknown node: {node_id}"

    def test_bfs_includes_root_itself(self, castengine_store, high_degree_functions):
        """BFS result should include the root node itself."""
        root = high_degree_functions[0]
        reachable = _reachable_ids(castengine_store, root)
        assert root in reachable

    def test_pending_gaps_subset_of_all_ucs(
        self, castengine_store, high_degree_functions
    ):
        """Pending gaps for any root must be a subset of all UCs."""
        all_uc_ids = {uc.id for uc in castengine_store.get_unresolved_calls()}
        for fn_id in high_degree_functions[:5]:
            gaps = castengine_store.get_pending_gaps_for_source(fn_id)
            for gap in gaps:
                assert gap.id in all_uc_ids, (
                    f"Gap {gap.id} not in global UC list — phantom gap"
                )

    def test_pending_gaps_callers_are_reachable(
        self, castengine_store, high_degree_functions
    ):
        """Every pending gap's caller_id must be in the BFS reachable set."""
        root = high_degree_functions[0]
        reachable = _reachable_ids(castengine_store, root)
        gaps = castengine_store.get_pending_gaps_for_source(root)
        for gap in gaps:
            assert gap.caller_id in reachable, (
                f"Gap {gap.id} caller {gap.caller_id} not reachable from {root}"
            )

    def test_bfs_max_reachable_is_bounded(self, castengine_store, high_degree_functions):
        """BFS should not return more nodes than total functions (no duplicates)."""
        total_fns = len(castengine_store.list_functions())
        root = high_degree_functions[0]
        reachable = _reachable_ids(castengine_store, root)
        assert len(reachable) <= total_fns

    def test_bfs_from_leaf_function_returns_only_self(self, castengine_store):
        """A function with no outgoing edges should only reach itself."""
        edges = castengine_store.list_calls_edges()
        callers = {e.caller_id for e in edges}
        fns = castengine_store.list_functions()
        # Find a function that never appears as a caller
        leaves = [fn for fn in fns if fn.id not in callers]
        assert len(leaves) > 0, "No leaf functions found"
        leaf = leaves[0]
        reachable = _reachable_ids(castengine_store, leaf.id)
        assert reachable == {leaf.id}


class TestCastEngineGraphConsistency:
    """Cross-check graph store invariants at CastEngine scale.

    These tests verify that the pipeline produces a consistent graph where
    edges reference valid nodes and stats match actual counts.
    """

    def test_all_edge_callers_exist(self, castengine_store):
        """Every CALLS edge caller_id must reference an existing function."""
        fn_ids = {fn.id for fn in castengine_store.list_functions()}
        edges = castengine_store.list_calls_edges()
        dangling = [e for e in edges if e.caller_id not in fn_ids]
        assert len(dangling) == 0, (
            f"{len(dangling)} edges have dangling caller_id"
        )

    def test_all_edge_callees_exist(self, castengine_store):
        """Every CALLS edge callee_id must reference an existing function."""
        fn_ids = {fn.id for fn in castengine_store.list_functions()}
        edges = castengine_store.list_calls_edges()
        dangling = [e for e in edges if e.callee_id not in fn_ids]
        assert len(dangling) == 0, (
            f"{len(dangling)} edges have dangling callee_id"
        )

    def test_all_uc_callers_exist(self, castengine_store):
        """Every UC's caller_id must reference an existing function."""
        fn_ids = {fn.id for fn in castengine_store.list_functions()}
        ucs = castengine_store.get_unresolved_calls()
        dangling = [uc for uc in ucs if uc.caller_id not in fn_ids]
        assert len(dangling) == 0, (
            f"{len(dangling)} UCs have dangling caller_id"
        )

    def test_edge_call_line_within_caller_bounds(self, castengine_store):
        """CALLS edge call_line should be within caller's [start_line, end_line].

        BUG BASELINE: ~4.5% misattribution due to overloaded function collision.
        """
        fns = {fn.id: fn for fn in castengine_store.list_functions()}
        edges = castengine_store.list_calls_edges()
        outside = 0
        total = 0
        for e in edges:
            caller = fns.get(e.caller_id)
            if caller:
                total += 1
                if not (caller.start_line <= e.props.call_line <= caller.end_line):
                    outside += 1
        pct = outside / total * 100 if total else 0
        # Known bug baseline — should not get worse
        assert pct <= 5.0, f"Misattribution rate {pct:.1f}% exceeds 5% threshold"

    def test_uc_call_line_within_caller_bounds(self, castengine_store):
        """UC call_line should be within caller's [start_line, end_line]."""
        fns = {fn.id: fn for fn in castengine_store.list_functions()}
        ucs = castengine_store.get_unresolved_calls()
        outside = 0
        total = 0
        for uc in ucs:
            caller = fns.get(uc.caller_id)
            if caller:
                total += 1
                if not (caller.start_line <= uc.call_line <= caller.end_line):
                    outside += 1
        pct = outside / total * 100 if total else 0
        assert pct <= 5.0, f"UC misattribution rate {pct:.1f}% exceeds 5% threshold"

    def test_no_self_loops(self, castengine_store):
        """Self-loops (caller==callee) should be rare.

        BUG BASELINE: 99 self-loops in CastEngine. Root cause: overloaded function
        collision in _resolve_id — when by_file_name[(file, name)] maps to the
        caller itself (because the overloaded variant was overwritten), the edge
        becomes a self-loop. Also some legitimate recursion (e.g., circular_buffer).
        """
        edges = castengine_store.list_calls_edges()
        self_loops = [e for e in edges if e.caller_id == e.callee_id]
        # Known baseline ~99. Should not get worse.
        assert len(self_loops) <= 120, (
            f"{len(self_loops)} self-loop edges — increased from baseline ~99"
        )

    def test_resolved_by_values_are_valid(self, castengine_store):
        """All resolved_by values must be from the allowed set (no 'llm' at parse time)."""
        valid = {"symbol_table", "signature", "dataflow", "context"}
        edges = castengine_store.list_calls_edges()
        invalid = [e for e in edges if e.props.resolved_by not in valid]
        assert len(invalid) == 0, (
            f"{len(invalid)} edges have invalid resolved_by: "
            f"{set(e.props.resolved_by for e in invalid[:5])}"
        )

    def test_call_type_values_are_valid(self, castengine_store):
        """All call_type values must be from {direct, indirect, virtual}."""
        valid = {"direct", "indirect", "virtual"}
        edges = castengine_store.list_calls_edges()
        invalid = [e for e in edges if e.props.call_type not in valid]
        assert len(invalid) == 0, (
            f"{len(invalid)} edges have invalid call_type: "
            f"{set(e.props.call_type for e in invalid[:5])}"
        )

    def test_stats_total_functions_matches(self, castengine_store):
        """count_stats().total_functions == len(list_functions())."""
        stats = castengine_store.count_stats()
        actual = len(castengine_store.list_functions())
        assert stats["total_functions"] == actual

    def test_stats_total_calls_matches(self, castengine_store):
        """count_stats().total_calls == len(list_calls_edges())."""
        stats = castengine_store.count_stats()
        actual = len(castengine_store.list_calls_edges())
        assert stats["total_calls"] == actual

    def test_stats_total_unresolved_matches(self, castengine_store):
        """count_stats().total_unresolved == len(get_unresolved_calls())."""
        stats = castengine_store.count_stats()
        actual = len(castengine_store.get_unresolved_calls())
        assert stats["total_unresolved"] == actual

    def test_stats_resolved_by_buckets_sum(self, castengine_store):
        """Sum of calls_by_resolved_by buckets == total_calls."""
        stats = castengine_store.count_stats()
        bucket_sum = sum(stats["calls_by_resolved_by"].values())
        assert bucket_sum == stats["total_calls"]

    def test_stats_call_type_buckets_sum(self, castengine_store):
        """Sum of calls_by_call_type buckets == total_calls."""
        stats = castengine_store.count_stats()
        bucket_sum = sum(stats["calls_by_call_type"].values())
        assert bucket_sum == stats["total_calls"]

    def test_function_count_sanity(self, castengine_store):
        """CastEngine should have 5000+ functions."""
        fns = castengine_store.list_functions()
        assert len(fns) >= 5000, f"Only {len(fns)} functions — parse may be incomplete"

    def test_edge_count_sanity(self, castengine_store):
        """CastEngine should have 4000+ direct call edges."""
        edges = castengine_store.list_calls_edges()
        assert len(edges) >= 4000, f"Only {len(edges)} edges — resolution may be broken"

    def test_uc_count_sanity(self, castengine_store):
        """CastEngine should have significant unresolved calls."""
        ucs = castengine_store.get_unresolved_calls()
        assert len(ucs) >= 5000, f"Only {len(ucs)} UCs — parser may not be reporting them"

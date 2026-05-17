"""InMemoryGraphStore edge-case tests — architecture.md §3-§4 deep dive.

Targets: BFS cycle handling, retry→unresolvable transition, category filter
consistency, get_pending_gaps_for_source accuracy, deduplication semantics.
"""
from __future__ import annotations

import pytest

from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    RepairLogNode,
    SourcePointNode,
    UnresolvedCallNode,
    VALID_REASON_CATEGORIES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store():
    return InMemoryGraphStore()


def _fn(id: str, name: str = "f", file_path: str = "a.cpp", start: int = 1) -> FunctionNode:
    return FunctionNode(
        id=id, name=name, signature=f"void {name}()",
        file_path=file_path, start_line=start, end_line=start + 5, body_hash="h",
    )


def _uc(id: str, caller_id: str, line: int = 1, status: str = "pending",
         retry_count: int = 0, reason: str | None = None) -> UnresolvedCallNode:
    return UnresolvedCallNode(
        id=id, caller_id=caller_id, call_expression="x()",
        call_file="a.cpp", call_line=line, call_type="indirect",
        source_code_snippet="", var_name=None, var_type=None,
        retry_count=retry_count, status=status,
        last_attempt_reason=reason,
    )


def _edge(resolved_by: str = "symbol_table", call_type: str = "direct",
           call_file: str = "a.cpp", call_line: int = 1) -> CallsEdgeProps:
    return CallsEdgeProps(
        resolved_by=resolved_by, call_type=call_type,
        call_file=call_file, call_line=call_line,
    )


# ---------------------------------------------------------------------------
# Test: BFS cycle handling in get_reachable_subgraph
# ---------------------------------------------------------------------------

class TestBFSCycleHandling:
    """architecture.md §3: BFS must terminate on cyclic call graphs."""

    def test_simple_cycle_terminates(self, store):
        """A→B→C→A cycle must not infinite-loop."""
        store.create_function(_fn("A"))
        store.create_function(_fn("B"))
        store.create_function(_fn("C"))
        store.create_calls_edge("A", "B", _edge(call_line=1))
        store.create_calls_edge("B", "C", _edge(call_line=2))
        store.create_calls_edge("C", "A", _edge(call_line=3))

        result = store.get_reachable_subgraph("A")
        node_ids = {n.id for n in result["nodes"]}
        assert node_ids == {"A", "B", "C"}

    def test_self_loop(self, store):
        """A→A (recursive) must not infinite-loop."""
        store.create_function(_fn("A"))
        store.create_calls_edge("A", "A", _edge(call_line=1))

        result = store.get_reachable_subgraph("A")
        assert len(result["nodes"]) == 1
        assert result["nodes"][0].id == "A"
        # Self-edge should be in edges
        assert len(result["edges"]) == 1

    def test_diamond_no_duplicate_nodes(self, store):
        """A→B, A→C, B→D, C→D — D visited once."""
        store.create_function(_fn("A"))
        store.create_function(_fn("B"))
        store.create_function(_fn("C"))
        store.create_function(_fn("D"))
        store.create_calls_edge("A", "B", _edge(call_line=1))
        store.create_calls_edge("A", "C", _edge(call_line=2))
        store.create_calls_edge("B", "D", _edge(call_line=3))
        store.create_calls_edge("C", "D", _edge(call_line=4))

        result = store.get_reachable_subgraph("A")
        node_ids = [n.id for n in result["nodes"]]
        # D should appear exactly once
        assert node_ids.count("D") == 1
        assert set(node_ids) == {"A", "B", "C", "D"}

    def test_max_depth_limits_traversal(self, store):
        """BFS respects max_depth parameter."""
        # Chain: A→B→C→D→E
        for name in "ABCDE":
            store.create_function(_fn(name))
        store.create_calls_edge("A", "B", _edge(call_line=1))
        store.create_calls_edge("B", "C", _edge(call_line=2))
        store.create_calls_edge("C", "D", _edge(call_line=3))
        store.create_calls_edge("D", "E", _edge(call_line=4))

        result = store.get_reachable_subgraph("A", max_depth=2)
        node_ids = {n.id for n in result["nodes"]}
        # depth 0=A, depth 1=B, depth 2=C; D and E unreachable
        assert "A" in node_ids
        assert "B" in node_ids
        assert "C" in node_ids
        # D is at depth 3 — should NOT be reached
        assert "D" not in node_ids
        assert "E" not in node_ids

    def test_nonexistent_source_returns_empty(self, store):
        """BFS from non-existent function_id returns empty subgraph."""
        result = store.get_reachable_subgraph("nonexistent")
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["unresolved"] == []


# ---------------------------------------------------------------------------
# Test: retry_count → unresolvable transition
# ---------------------------------------------------------------------------

class TestRetryToUnresolvable:
    """architecture.md §3: retry_count reaches 3 → status='unresolvable'."""

    def test_three_retries_marks_unresolvable(self, store):
        """After 3 retry stamps, status transitions to 'unresolvable'."""
        store.create_unresolved_call(_uc("uc1", "fn1"))

        store.update_unresolved_call_retry_state(
            "uc1", "2026-01-01T00:00:00Z", "gate_failed: attempt 1"
        )
        uc = store._unresolved_calls["uc1"]
        assert uc.retry_count == 1
        assert uc.status == "pending"

        store.update_unresolved_call_retry_state(
            "uc1", "2026-01-02T00:00:00Z", "gate_failed: attempt 2"
        )
        uc = store._unresolved_calls["uc1"]
        assert uc.retry_count == 2
        assert uc.status == "pending"

        store.update_unresolved_call_retry_state(
            "uc1", "2026-01-03T00:00:00Z", "gate_failed: attempt 3"
        )
        uc = store._unresolved_calls["uc1"]
        assert uc.retry_count == 3
        assert uc.status == "unresolvable"

    def test_fourth_retry_stays_unresolvable(self, store):
        """Stamping a 4th retry on an already-unresolvable UC keeps status."""
        store.create_unresolved_call(_uc("uc1", "fn1", retry_count=2))
        # 3rd stamp → unresolvable
        store.update_unresolved_call_retry_state(
            "uc1", "2026-01-01T00:00:00Z", "agent_error: crash"
        )
        # 4th stamp — should still be unresolvable (3+1=4 >= 3)
        store.update_unresolved_call_retry_state(
            "uc1", "2026-01-02T00:00:00Z", "agent_error: crash again"
        )
        uc = store._unresolved_calls["uc1"]
        assert uc.retry_count == 4
        assert uc.status == "unresolvable"

    def test_retry_on_nonexistent_uc_is_noop(self, store):
        """Stamping retry on missing UC silently does nothing."""
        # Should not raise
        store.update_unresolved_call_retry_state(
            "ghost", "2026-01-01T00:00:00Z", "gate_failed: test"
        )

    def test_invalid_reason_category_raises(self, store):
        """Reason with invalid category prefix raises ValueError."""
        store.create_unresolved_call(_uc("uc1", "fn1"))
        with pytest.raises(ValueError, match="category"):
            store.update_unresolved_call_retry_state(
                "uc1", "2026-01-01T00:00:00Z", "invalid_category: oops"
            )

    def test_reason_over_200_chars_raises(self, store):
        """Reason exceeding 200 chars raises ValueError."""
        store.create_unresolved_call(_uc("uc1", "fn1"))
        with pytest.raises(ValueError, match="200"):
            store.update_unresolved_call_retry_state(
                "uc1", "2026-01-01T00:00:00Z", "gate_failed: " + "x" * 200
            )

    def test_standalone_category_accepted(self, store):
        """Reason without ': ' separator is treated as standalone category."""
        store.create_unresolved_call(_uc("uc1", "fn1"))
        store.update_unresolved_call_retry_state(
            "uc1", "2026-01-01T00:00:00Z", "agent_exited_without_edge"
        )
        uc = store._unresolved_calls["uc1"]
        assert uc.last_attempt_reason == "agent_exited_without_edge"
        assert uc.retry_count == 1


# ---------------------------------------------------------------------------
# Test: Category filter consistency between count_stats and get_unresolved_calls
# ---------------------------------------------------------------------------

class TestCategoryFilterConsistency:
    """architecture.md §8: stats buckets must be consistent with query filters."""

    def test_colon_space_reason_matches_both(self, store):
        """'gate_failed: detail' matches both count_stats and get_unresolved_calls."""
        store.create_unresolved_call(
            _uc("uc1", "fn1", reason="gate_failed: remaining pending GAPs")
        )
        # count_stats should bucket under "gate_failed"
        stats = store.count_stats()
        assert stats["unresolved_by_category"]["gate_failed"] == 1

        # get_unresolved_calls with category filter should also find it
        results = store.get_unresolved_calls(category="gate_failed")
        assert len(results) == 1

    def test_colon_no_space_reason(self, store):
        """'gate_failed:no_space' — count_stats vs get_unresolved_calls consistency.

        BUG DETECTION: count_stats uses split(":", 1)[0].strip() which extracts
        'gate_failed', but get_unresolved_calls uses startswith("gate_failed:")
        which also matches. However, update_unresolved_call_retry_state uses
        find(": ") which would NOT find ": " in "gate_failed:no_space" and would
        treat the whole string as a standalone category — rejecting it.

        This test verifies the validator prevents such reasons from being stored.
        """
        store.create_unresolved_call(_uc("uc1", "fn1"))
        # The validator should reject "gate_failed:no_space" because find(": ")
        # returns -1, so it treats the whole string as category, which is invalid
        with pytest.raises(ValueError, match="category"):
            store.update_unresolved_call_retry_state(
                "uc1", "2026-01-01T00:00:00Z", "gate_failed:no_space"
            )

    def test_none_category_filter(self, store):
        """category='none' matches UCs with no last_attempt_reason."""
        store.create_unresolved_call(_uc("uc1", "fn1", reason=None))
        store.create_unresolved_call(
            _uc("uc2", "fn1", line=2, reason="gate_failed: x")
        )

        results = store.get_unresolved_calls(category="none")
        assert len(results) == 1
        assert results[0].id == "uc1"

        stats = store.count_stats()
        assert stats["unresolved_by_category"]["none"] == 1
        assert stats["unresolved_by_category"]["gate_failed"] == 1

    def test_all_valid_categories_in_stats(self, store):
        """Stats always contains all 5 category keys + 'none' even when empty."""
        stats = store.count_stats()
        expected_keys = {
            "gate_failed", "agent_error", "subprocess_crash",
            "subprocess_timeout", "agent_exited_without_edge", "none",
        }
        assert set(stats["unresolved_by_category"].keys()) == expected_keys

    def test_count_stats_vs_get_unresolved_calls_totals_match(self, store):
        """Sum of category buckets in stats == total unresolved calls."""
        store.create_unresolved_call(_uc("uc1", "fn1", reason=None))
        store.create_unresolved_call(
            _uc("uc2", "fn1", line=2, reason="gate_failed: x")
        )
        store.create_unresolved_call(
            _uc("uc3", "fn1", line=3, reason="agent_error: timeout")
        )

        stats = store.count_stats()
        category_sum = sum(stats["unresolved_by_category"].values())
        assert category_sum == stats["total_unresolved"]

    def test_exact_category_match_without_colon(self, store):
        """Standalone category 'agent_exited_without_edge' matches filter."""
        store.create_unresolved_call(
            _uc("uc1", "fn1", reason="agent_exited_without_edge")
        )
        results = store.get_unresolved_calls(category="agent_exited_without_edge")
        assert len(results) == 1

        stats = store.count_stats()
        assert stats["unresolved_by_category"]["agent_exited_without_edge"] == 1


# ---------------------------------------------------------------------------
# Test: get_pending_gaps_for_source accuracy
# ---------------------------------------------------------------------------

class TestGetPendingGapsForSource:
    """architecture.md §3 门禁机制: BFS from source, return only pending UCs."""

    def test_finds_direct_caller_gaps(self, store):
        """Gaps on the source function itself are found."""
        store.create_function(_fn("src"))
        store.create_unresolved_call(_uc("uc1", "src"))

        gaps = store.get_pending_gaps_for_source("src")
        assert len(gaps) == 1
        assert gaps[0].id == "uc1"

    def test_finds_multi_hop_gaps(self, store):
        """Gaps on functions reachable via CALLS edges are found."""
        store.create_function(_fn("src"))
        store.create_function(_fn("mid"))
        store.create_function(_fn("leaf"))
        store.create_calls_edge("src", "mid", _edge(call_line=1))
        store.create_calls_edge("mid", "leaf", _edge(call_line=2))
        store.create_unresolved_call(_uc("uc_leaf", "leaf", line=10))

        gaps = store.get_pending_gaps_for_source("src")
        assert len(gaps) == 1
        assert gaps[0].id == "uc_leaf"

    def test_excludes_unresolvable_gaps(self, store):
        """Only status='pending' gaps are returned, not 'unresolvable'."""
        store.create_function(_fn("src"))
        store.create_unresolved_call(_uc("uc_pending", "src", line=1))
        store.create_unresolved_call(
            _uc("uc_dead", "src", line=2, status="unresolvable", retry_count=3)
        )

        gaps = store.get_pending_gaps_for_source("src")
        assert len(gaps) == 1
        assert gaps[0].id == "uc_pending"

    def test_excludes_unreachable_gaps(self, store):
        """Gaps on functions NOT reachable from source are excluded."""
        store.create_function(_fn("src"))
        store.create_function(_fn("isolated"))
        store.create_unresolved_call(_uc("uc_src", "src"))
        store.create_unresolved_call(_uc("uc_iso", "isolated"))

        gaps = store.get_pending_gaps_for_source("src")
        assert len(gaps) == 1
        assert gaps[0].id == "uc_src"

    def test_cycle_does_not_duplicate_gaps(self, store):
        """Cycle A→B→A: gaps on A and B each appear once."""
        store.create_function(_fn("A"))
        store.create_function(_fn("B"))
        store.create_calls_edge("A", "B", _edge(call_line=1))
        store.create_calls_edge("B", "A", _edge(call_line=2))
        store.create_unresolved_call(_uc("uc_a", "A", line=10))
        store.create_unresolved_call(_uc("uc_b", "B", line=20))

        gaps = store.get_pending_gaps_for_source("A")
        gap_ids = [g.id for g in gaps]
        assert sorted(gap_ids) == ["uc_a", "uc_b"]

    def test_nonexistent_source_returns_empty(self, store):
        """Non-existent source_id returns empty list (no crash)."""
        store.create_unresolved_call(_uc("uc1", "fn1"))
        gaps = store.get_pending_gaps_for_source("nonexistent")
        assert gaps == []


# ---------------------------------------------------------------------------
# Test: create_unresolved_call deduplication semantics
# ---------------------------------------------------------------------------

class TestUnresolvedCallDedup:
    """architecture.md §5: UC regeneration after review-incorrect resets state."""

    def test_dedup_key_is_caller_file_line(self, store):
        """Same (caller_id, call_file, call_line) → update in place."""
        uc1 = _uc("uc_orig", "fn1", line=10)
        store.create_unresolved_call(uc1)

        # Create another UC with same logical key but different id
        uc2 = UnresolvedCallNode(
            id="uc_new", caller_id="fn1", call_expression="y()",
            call_file="a.cpp", call_line=10, call_type="indirect",
            source_code_snippet="new snippet", var_name="v", var_type="T",
        )
        returned_id = store.create_unresolved_call(uc2)

        # Should return the ORIGINAL id (dedup preserves old id)
        assert returned_id == "uc_orig"
        # But content is updated
        stored = store._unresolved_calls["uc_orig"]
        assert stored.call_expression == "y()"
        assert stored.source_code_snippet == "new snippet"

    def test_dedup_resets_retry_state(self, store):
        """Dedup with fresh UC resets retry_count and status.

        This is the intended behavior for review-incorrect cascade:
        regenerated UC has retry_count=0, status='pending'.
        """
        # Existing UC with retries
        uc_old = _uc("uc1", "fn1", line=10, retry_count=2,
                      reason="gate_failed: attempt 2")
        store.create_unresolved_call(uc_old)
        # Stamp it to make it unresolvable
        store.update_unresolved_call_retry_state(
            "uc1", "2026-01-01T00:00:00Z", "gate_failed: attempt 3"
        )
        assert store._unresolved_calls["uc1"].status == "unresolvable"

        # Regenerate with fresh state (review cascade)
        uc_fresh = UnresolvedCallNode(
            id="uc_regen", caller_id="fn1", call_expression="x()",
            call_file="a.cpp", call_line=10, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            retry_count=0, status="pending",
        )
        store.create_unresolved_call(uc_fresh)

        stored = store._unresolved_calls["uc1"]
        assert stored.retry_count == 0
        assert stored.status == "pending"
        assert stored.last_attempt_reason is None

    def test_different_line_creates_new_uc(self, store):
        """Different call_line → separate UC (no dedup)."""
        store.create_unresolved_call(_uc("uc1", "fn1", line=10))
        store.create_unresolved_call(_uc("uc2", "fn1", line=20))
        assert len(store._unresolved_calls) == 2


# ---------------------------------------------------------------------------
# Test: delete_calls_edge correctness
# ---------------------------------------------------------------------------

class TestDeleteCallsEdge:
    """architecture.md §5: targeted edge deletion returns bool."""

    def test_delete_existing_returns_true(self, store):
        store.create_function(_fn("A"))
        store.create_function(_fn("B"))
        store.create_calls_edge("A", "B", _edge(call_file="x.cpp", call_line=5))

        assert store.delete_calls_edge("A", "B", "x.cpp", 5) is True
        assert store.edge_exists("A", "B", "x.cpp", 5) is False

    def test_delete_nonexistent_returns_false(self, store):
        assert store.delete_calls_edge("A", "B", "x.cpp", 5) is False

    def test_delete_only_matching_edge(self, store):
        """Multiple edges from same caller — only exact match deleted."""
        store.create_function(_fn("A"))
        store.create_function(_fn("B"))
        store.create_function(_fn("C"))
        store.create_calls_edge("A", "B", _edge(call_line=1))
        store.create_calls_edge("A", "C", _edge(call_line=2))

        store.delete_calls_edge("A", "B", "a.cpp", 1)
        assert not store.edge_exists("A", "B", "a.cpp", 1)
        assert store.edge_exists("A", "C", "a.cpp", 2)


# ---------------------------------------------------------------------------
# Test: reset_unresolvable_gaps
# ---------------------------------------------------------------------------

class TestResetUnresolvableGaps:
    """architecture.md §10: retry_failed_gaps=true resets all unresolvable."""

    def test_resets_unresolvable_to_pending(self, store):
        store.create_unresolved_call(
            _uc("uc1", "fn1", status="unresolvable", retry_count=3,
                 reason="gate_failed: max retries")
        )
        store.reset_unresolvable_gaps()

        uc = store._unresolved_calls["uc1"]
        assert uc.status == "pending"
        assert uc.retry_count == 0
        assert uc.last_attempt_timestamp is None
        assert uc.last_attempt_reason is None

    def test_does_not_touch_pending(self, store):
        store.create_unresolved_call(_uc("uc1", "fn1", status="pending"))
        store.reset_unresolvable_gaps()

        uc = store._unresolved_calls["uc1"]
        assert uc.status == "pending"
        assert uc.retry_count == 0

    def test_resets_multiple(self, store):
        store.create_unresolved_call(
            _uc("uc1", "fn1", line=1, status="unresolvable", retry_count=3)
        )
        store.create_unresolved_call(
            _uc("uc2", "fn1", line=2, status="unresolvable", retry_count=3)
        )
        store.create_unresolved_call(_uc("uc3", "fn1", line=3, status="pending"))

        store.reset_unresolvable_gaps()

        assert store._unresolved_calls["uc1"].status == "pending"
        assert store._unresolved_calls["uc2"].status == "pending"
        assert store._unresolved_calls["uc3"].status == "pending"
        # Only the unresolvable ones had retry_count reset
        assert store._unresolved_calls["uc1"].retry_count == 0
        assert store._unresolved_calls["uc2"].retry_count == 0


# ---------------------------------------------------------------------------
# Test: SourcePoint status transitions
# ---------------------------------------------------------------------------

class TestSourcePointTransitions:
    """architecture.md §3: forward-only state machine."""

    def test_pending_to_running(self, store):
        sp = SourcePointNode(
            id="sp1", function_id="fn1",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp1", "running")
        assert store.get_source_point("sp1").status == "running"

    def test_running_to_complete(self, store):
        sp = SourcePointNode(
            id="sp1", function_id="fn1",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp1", "running")
        store.update_source_point_status("sp1", "complete")
        assert store.get_source_point("sp1").status == "complete"

    def test_running_to_partial_complete(self, store):
        sp = SourcePointNode(
            id="sp1", function_id="fn1",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp1", "running")
        store.update_source_point_status("sp1", "partial_complete")
        assert store.get_source_point("sp1").status == "partial_complete"

    def test_backward_transition_raises(self, store):
        sp = SourcePointNode(
            id="sp1", function_id="fn1",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp1", "running")
        with pytest.raises(ValueError, match="Invalid SourcePoint transition"):
            store.update_source_point_status("sp1", "pending")

    def test_force_reset_allows_backward(self, store):
        sp = SourcePointNode(
            id="sp1", function_id="fn1",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp1", "running")
        store.update_source_point_status("sp1", "complete")
        # force_reset allows complete → pending
        store.update_source_point_status("sp1", "pending", force_reset=True)
        assert store.get_source_point("sp1").status == "pending"

    def test_same_status_is_idempotent(self, store):
        sp = SourcePointNode(
            id="sp1", function_id="fn1",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)
        # pending → pending should be no-op, not raise
        store.update_source_point_status("sp1", "pending")
        assert store.get_source_point("sp1").status == "pending"

    def test_invalid_status_raises(self, store):
        sp = SourcePointNode(
            id="sp1", function_id="fn1",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)
        with pytest.raises(ValueError, match="must be one of"):
            store.update_source_point_status("sp1", "invalid_status")

    def test_nonexistent_source_point_is_noop(self, store):
        """Updating non-existent source point silently does nothing."""
        store.update_source_point_status("ghost", "running")
        # No crash, no source point created


# ---------------------------------------------------------------------------
# Test: Edge uniqueness and create_calls_edge idempotency
# ---------------------------------------------------------------------------

class TestEdgeUniqueness:
    """architecture.md §4: CALLS edge uniqueness on (caller, callee, file, line)."""

    def test_duplicate_edge_silently_skipped(self, store):
        store.create_function(_fn("A"))
        store.create_function(_fn("B"))
        store.create_calls_edge("A", "B", _edge(call_line=5))
        store.create_calls_edge("A", "B", _edge(call_line=5))

        edges = store.list_calls_edges()
        assert len(edges) == 1

    def test_same_pair_different_line_creates_two(self, store):
        """Same caller+callee at different call sites → two edges."""
        store.create_function(_fn("A"))
        store.create_function(_fn("B"))
        store.create_calls_edge("A", "B", _edge(call_line=5))
        store.create_calls_edge("A", "B", _edge(call_line=10))

        edges = store.list_calls_edges()
        assert len(edges) == 2

    def test_first_resolved_by_preserved(self, store):
        """Duplicate edge keeps first resolved_by (architecture.md §4)."""
        store.create_function(_fn("A"))
        store.create_function(_fn("B"))
        store.create_calls_edge("A", "B", _edge(resolved_by="symbol_table", call_line=5))
        store.create_calls_edge("A", "B", _edge(resolved_by="llm", call_line=5))

        edge = store.get_calls_edge("A", "B", "a.cpp", 5)
        assert edge.resolved_by == "symbol_table"


# ---------------------------------------------------------------------------
# Test: count_stats correctness
# ---------------------------------------------------------------------------

class TestCountStats:
    """architecture.md §8: stats endpoint required buckets."""

    def test_empty_store_has_all_keys(self, store):
        stats = store.count_stats()
        assert stats["total_functions"] == 0
        assert stats["total_files"] == 0
        assert stats["total_calls"] == 0
        assert stats["total_unresolved"] == 0
        assert stats["total_repair_logs"] == 0
        assert stats["total_llm_edges"] == 0
        # All bucket keys present
        assert set(stats["calls_by_resolved_by"].keys()) == {
            "symbol_table", "signature", "dataflow", "context", "llm"
        }
        assert set(stats["calls_by_call_type"].keys()) == {
            "direct", "indirect", "virtual"
        }
        assert set(stats["unresolved_by_status"].keys()) == {
            "pending", "unresolvable"
        }
        assert set(stats["source_points_by_status"].keys()) == {
            "pending", "running", "complete", "partial_complete"
        }

    def test_llm_edges_counted(self, store):
        store.create_function(_fn("A"))
        store.create_function(_fn("B"))
        store.create_calls_edge("A", "B", _edge(resolved_by="llm", call_line=1))

        stats = store.count_stats()
        assert stats["total_llm_edges"] == 1
        assert stats["calls_by_resolved_by"]["llm"] == 1

    def test_source_points_by_status(self, store):
        sp1 = SourcePointNode(
            id="sp1", function_id="fn1",
            entry_point_kind="api", reason="r", status="pending",
        )
        sp2 = SourcePointNode(
            id="sp2", function_id="fn2",
            entry_point_kind="api", reason="r", status="pending",
        )
        store.create_source_point(sp1)
        store.create_source_point(sp2)
        store.update_source_point_status("sp1", "running")

        stats = store.count_stats()
        assert stats["source_points_by_status"]["pending"] == 1
        assert stats["source_points_by_status"]["running"] == 1


# ---------------------------------------------------------------------------
# Test: RepairLog deduplication
# ---------------------------------------------------------------------------

class TestRepairLogDedup:
    """architecture.md §3: RepairLog dedup on (caller_id, callee_id, call_location)."""

    def test_same_triple_updates_in_place(self, store):
        log1 = RepairLogNode(
            id="log1", caller_id="A", callee_id="B",
            call_location="x.cpp:10", repair_method="llm",
            llm_response="first", timestamp="t1", reasoning_summary="r1",
        )
        log2 = RepairLogNode(
            id="log2", caller_id="A", callee_id="B",
            call_location="x.cpp:10", repair_method="llm",
            llm_response="second", timestamp="t2", reasoning_summary="r2",
        )
        store.create_repair_log(log1)
        returned_id = store.create_repair_log(log2)

        # Preserves original id
        assert returned_id == "log1"
        # Content updated
        stored = store._repair_logs["log1"]
        assert stored.llm_response == "second"
        assert stored.reasoning_summary == "r2"
        # Only one log exists
        assert len(store._repair_logs) == 1

    def test_different_location_creates_new(self, store):
        log1 = RepairLogNode(
            id="log1", caller_id="A", callee_id="B",
            call_location="x.cpp:10", repair_method="llm",
            llm_response="r", timestamp="t", reasoning_summary="s",
        )
        log2 = RepairLogNode(
            id="log2", caller_id="A", callee_id="B",
            call_location="x.cpp:20", repair_method="llm",
            llm_response="r", timestamp="t", reasoning_summary="s",
        )
        store.create_repair_log(log1)
        store.create_repair_log(log2)
        assert len(store._repair_logs) == 2

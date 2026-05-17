"""Repair orchestrator retry logic + gate mechanism — architecture.md §3.

Tests the retry loop, gate check, source point status transitions,
and the has_retryable_gaps logic with real InMemoryGraphStore.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codemap_lite.analysis.repair_orchestrator import (
    RepairConfig,
    RepairOrchestrator,
    SourceRepairResult,
    _safe_dirname,
)
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    SourcePointNode,
    UnresolvedCallNode,
)


@pytest.fixture
def store_with_gaps():
    """Store with a source point that has pending gaps."""
    store = InMemoryGraphStore()
    # Create functions forming a chain: fn_entry -> fn_mid -> fn_leaf
    store.create_function(FunctionNode(
        id="fn_entry", name="entry", signature="void entry()",
        file_path="main.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="fn_mid", name="middle", signature="void middle()",
        file_path="main.cpp", start_line=20, end_line=30, body_hash="h2",
    ))
    store.create_function(FunctionNode(
        id="fn_leaf", name="leaf", signature="void leaf()",
        file_path="util.cpp", start_line=1, end_line=10, body_hash="h3",
    ))

    # Edge: entry -> mid (resolved)
    store.create_calls_edge("fn_entry", "fn_mid", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="main.cpp", call_line=5,
    ))

    # UC: mid -> ??? (pending gap)
    store.create_unresolved_call(UnresolvedCallNode(
        id="uc_1", caller_id="fn_mid", call_expression="dispatch()",
        call_file="main.cpp", call_line=25, call_type="indirect",
        source_code_snippet="ptr->dispatch()", var_name="ptr", var_type="Base*",
        candidates=["fn_leaf"],
    ))

    # Source point
    store.create_source_point(SourcePointNode(
        id="fn_entry", function_id="fn_entry",
        entry_point_kind="entry", reason="main entry", status="pending",
    ))

    return store


@pytest.fixture
def target_dir(tmp_path):
    d = tmp_path / "target"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# §3: _has_retryable_gaps
# ---------------------------------------------------------------------------


class TestHasRetryableGaps:
    """architecture.md §3: retry loop continues while pending gaps exist."""

    def test_has_gaps_with_pending(self, store_with_gaps, target_dir):
        config = RepairConfig(target_dir=target_dir, graph_store=store_with_gaps)
        orch = RepairOrchestrator(config)
        assert orch._has_retryable_gaps("fn_entry") is True

    def test_no_gaps_after_all_unresolvable(self, store_with_gaps, target_dir):
        """Once all gaps hit retry limit, _has_retryable_gaps returns False."""
        config = RepairConfig(target_dir=target_dir, graph_store=store_with_gaps)
        orch = RepairOrchestrator(config)
        # Manually set retry_count to MAX
        store_with_gaps.update_unresolved_call_retry_state(
            "uc_1", "2026-01-01T00:00:00Z", "gate_failed: test"
        )
        store_with_gaps.update_unresolved_call_retry_state(
            "uc_1", "2026-01-01T00:00:01Z", "gate_failed: test"
        )
        store_with_gaps.update_unresolved_call_retry_state(
            "uc_1", "2026-01-01T00:00:02Z", "gate_failed: test"
        )
        # After 3 retries, gap should be unresolvable
        assert orch._has_retryable_gaps("fn_entry") is False

    def test_no_store_fallback(self, target_dir):
        """Without graph_store, uses source-level counter."""
        config = RepairConfig(target_dir=target_dir, graph_store=None)
        orch = RepairOrchestrator(config)
        # First 3 calls return True, then False
        assert orch._has_retryable_gaps("src_001") is True
        assert orch._has_retryable_gaps("src_001") is True
        assert orch._has_retryable_gaps("src_001") is True
        assert orch._has_retryable_gaps("src_001") is False


# ---------------------------------------------------------------------------
# §3: Source point status transitions
# ---------------------------------------------------------------------------


class TestSourcePointStatusTransitions:
    """architecture.md §3: pending → running → complete | partial_complete."""

    def test_ensure_source_point_creates_if_missing(self, target_dir):
        store = InMemoryGraphStore()
        config = RepairConfig(target_dir=target_dir, graph_store=store)
        orch = RepairOrchestrator(config)
        orch._ensure_source_point("new_source")
        sp = store.get_source_point("new_source")
        assert sp is not None
        assert sp.status == "pending"

    def test_ensure_source_point_noop_if_exists(self, store_with_gaps, target_dir):
        config = RepairConfig(target_dir=target_dir, graph_store=store_with_gaps)
        orch = RepairOrchestrator(config)
        orch._ensure_source_point("fn_entry")
        sp = store_with_gaps.get_source_point("fn_entry")
        assert sp.status == "pending"  # Not overwritten

    def test_update_source_status(self, store_with_gaps, target_dir):
        config = RepairConfig(target_dir=target_dir, graph_store=store_with_gaps)
        orch = RepairOrchestrator(config)
        orch._update_source_status("fn_entry", "running")
        sp = store_with_gaps.get_source_point("fn_entry")
        assert sp.status == "running"


# ---------------------------------------------------------------------------
# §3: _record_retry_attempt
# ---------------------------------------------------------------------------


class TestRecordRetryAttempt:
    """architecture.md §3 Retry 审计字段: stamp on every pending GAP."""

    def test_stamps_pending_gaps(self, store_with_gaps, target_dir):
        config = RepairConfig(target_dir=target_dir, graph_store=store_with_gaps)
        orch = RepairOrchestrator(config)
        orch._record_retry_attempt("fn_entry", "gate_failed: test")
        uc = store_with_gaps.get_unresolved_calls()[0]
        assert uc.retry_count == 1
        assert uc.last_attempt_reason == "gate_failed: test"
        assert uc.last_attempt_timestamp is not None

    def test_increments_retry_count(self, store_with_gaps, target_dir):
        config = RepairConfig(target_dir=target_dir, graph_store=store_with_gaps)
        orch = RepairOrchestrator(config)
        orch._record_retry_attempt("fn_entry", "gate_failed: attempt 1")
        orch._record_retry_attempt("fn_entry", "gate_failed: attempt 2")
        uc = store_with_gaps.get_unresolved_calls()[0]
        assert uc.retry_count == 2

    def test_noop_without_store(self, target_dir):
        config = RepairConfig(target_dir=target_dir, graph_store=None)
        orch = RepairOrchestrator(config)
        # Should not raise
        orch._record_retry_attempt("src_001", "gate_failed: test")


# ---------------------------------------------------------------------------
# §3: _count_edges_written
# ---------------------------------------------------------------------------


class TestCountEdgesWritten:
    """architecture.md §3: count LLM edges reachable from source."""

    def test_counts_llm_edges(self, store_with_gaps, target_dir):
        # Add an LLM edge
        store_with_gaps.create_calls_edge("fn_mid", "fn_leaf", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="main.cpp", call_line=25,
        ))
        config = RepairConfig(target_dir=target_dir, graph_store=store_with_gaps)
        orch = RepairOrchestrator(config)
        count = orch._count_edges_written("fn_entry")
        assert count >= 1

    def test_zero_without_llm_edges(self, store_with_gaps, target_dir):
        config = RepairConfig(target_dir=target_dir, graph_store=store_with_gaps)
        orch = RepairOrchestrator(config)
        count = orch._count_edges_written("fn_entry")
        assert count == 0

    def test_zero_without_store(self, target_dir):
        config = RepairConfig(target_dir=target_dir, graph_store=None)
        orch = RepairOrchestrator(config)
        assert orch._count_edges_written("src_001") == 0


# ---------------------------------------------------------------------------
# §3: Full repair loop (mocked subprocess)
# ---------------------------------------------------------------------------


class TestRepairLoopIntegration:
    """architecture.md §3: full repair loop with mocked subprocess."""

    @pytest.mark.asyncio
    async def test_no_gaps_completes_immediately(self, target_dir):
        """Source with no pending gaps → success, 0 attempts."""
        store = InMemoryGraphStore()
        store.create_function(FunctionNode(
            id="fn_clean", name="clean", signature="void clean()",
            file_path="a.cpp", start_line=1, end_line=10, body_hash="h",
        ))
        store.create_source_point(SourcePointNode(
            id="fn_clean", function_id="fn_clean",
            entry_point_kind="entry", reason="test", status="pending",
        ))
        config = RepairConfig(target_dir=target_dir, graph_store=store)
        orch = RepairOrchestrator(config)
        result = await orch._run_single_repair("fn_clean")
        assert result.success is True
        assert result.attempts == 0
        # Source should be "complete"
        sp = store.get_source_point("fn_clean")
        assert sp.status == "complete"

    @pytest.mark.asyncio
    async def test_subprocess_crash_records_reason(self, store_with_gaps, target_dir):
        """Subprocess FileNotFoundError → agent_error stamped."""
        config = RepairConfig(
            target_dir=target_dir,
            graph_store=store_with_gaps,
            command="nonexistent_binary_xyz",
            args=[],
        )
        orch = RepairOrchestrator(config)
        result = await orch._run_single_repair("fn_entry")
        # Should fail after MAX_RETRIES_PER_GAP attempts
        assert result.success is False
        # UC should have retry stamps
        uc = store_with_gaps.get_unresolved_calls()[0]
        assert uc.retry_count >= 1
        assert "subprocess_crash" in (uc.last_attempt_reason or "")

    @pytest.mark.asyncio
    async def test_reset_unresolvable_gaps_on_new_run(self, target_dir):
        """architecture.md §10: retry_failed_gaps resets unresolvable gaps."""
        store = InMemoryGraphStore()
        store.create_function(FunctionNode(
            id="fn_a", name="a", signature="void a()",
            file_path="a.cpp", start_line=1, end_line=10, body_hash="h",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            id="uc_old", caller_id="fn_a", call_expression="old()",
            call_file="a.cpp", call_line=5, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            status="unresolvable", retry_count=3,
        ))
        config = RepairConfig(
            target_dir=target_dir,
            graph_store=store,
            retry_failed_gaps=True,
        )
        orch = RepairOrchestrator(config)
        orch._reset_unresolvable_gaps()
        uc = store.get_unresolved_calls()[0]
        assert uc.status == "pending"
        assert uc.retry_count == 0

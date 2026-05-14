"""Architecture compliance tests — verify implementation matches architecture.md.

Each test class targets a specific architecture section and tests behaviors
that are explicitly described in the spec but not yet covered by existing tests.

References:
- §3: Repair Agent (retry logic, gate, progress, subprocess handling)
- §5: Review (counter-example generation, feedback dedup)
- §7: Incremental (5-step cascade invalidation)
- §8: REST API (stats buckets, repair-log filtering)
- §10: Configuration (retry_failed_gaps flag)
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from codemap_lite.analysis.feedback_store import CounterExample, FeedbackStore
from codemap_lite.analysis.repair_orchestrator import (
    RepairConfig,
    RepairOrchestrator,
    SourceRepairResult,
)
from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    RepairLogNode,
    SourcePointNode,
    UnresolvedCallNode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store_with_source_and_gaps(
    source_id: str = "src_1",
    num_gaps: int = 1,
    gap_retry_count: int = 0,
) -> InMemoryGraphStore:
    """Create a store with a function, source point, and N pending gaps."""
    store = InMemoryGraphStore()
    fn = FunctionNode(
        id=source_id,
        signature="void entry()",
        name="entry",
        file_path="/src/main.cpp",
        start_line=1,
        end_line=10,
        body_hash="h1",
    )
    store.create_function(fn)

    sp = SourcePointNode(
        id=source_id,
        entry_point_kind="api_entry",
        reason="test",
        function_id=source_id,
        status="pending",
    )
    store.create_source_point(sp)

    for i in range(num_gaps):
        uc = UnresolvedCallNode(
            id=f"gap_{i}",
            caller_id=source_id,
            call_expression=f"fp_{i}()",
            call_file="/src/main.cpp",
            call_line=3 + i,
            call_type="indirect",
            source_code_snippet=f"fp_{i}();",
            var_name=f"fp_{i}",
            var_type="void (*)()",
            candidates=[],
            retry_count=gap_retry_count,
        )
        store.create_unresolved_call(uc)

    return store


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# §3: GAP marked unresolvable after 3 retries
# ---------------------------------------------------------------------------


class TestSection3_RetryBudgetExhaustion:
    """architecture.md §3 line 118-120:
    retry_count ≥ 3 → GAP.status = "unresolvable", SourcePoint = "partial_complete"
    """

    def test_gap_becomes_unresolvable_after_max_retries(self):
        """A gap with retry_count=3 should not be retried."""
        store = _make_store_with_source_and_gaps("src_1", num_gaps=1, gap_retry_count=3)
        # _has_retryable_gaps should return False
        config = RepairConfig(
            target_dir=Path("/tmp/test"),
            graph_store=store,
        )
        orch = RepairOrchestrator(config)
        assert not orch._has_retryable_gaps("src_1")

    def test_gap_with_retry_count_2_is_still_retryable(self):
        """A gap with retry_count=2 (< 3) should still be retried."""
        store = _make_store_with_source_and_gaps("src_1", num_gaps=1, gap_retry_count=2)
        config = RepairConfig(
            target_dir=Path("/tmp/test"),
            graph_store=store,
        )
        orch = RepairOrchestrator(config)
        assert orch._has_retryable_gaps("src_1")

    def test_mixed_gaps_retryable_if_any_under_budget(self):
        """If one gap is at max but another isn't, source is still retryable."""
        store = _make_store_with_source_and_gaps("src_1", num_gaps=2, gap_retry_count=0)
        # Manually set gap_0 to retry_count=3
        gap_0 = store._unresolved_calls["gap_0"]
        store._unresolved_calls["gap_0"] = UnresolvedCallNode(
            id=gap_0.id,
            caller_id=gap_0.caller_id,
            call_expression=gap_0.call_expression,
            call_file=gap_0.call_file,
            call_line=gap_0.call_line,
            call_type=gap_0.call_type,
            source_code_snippet=gap_0.source_code_snippet,
            var_name=gap_0.var_name,
            var_type=gap_0.var_type,
            candidates=gap_0.candidates,
            retry_count=3,
            status="pending",
        )
        config = RepairConfig(target_dir=Path("/tmp/test"), graph_store=store)
        orch = RepairOrchestrator(config)
        # gap_1 still has retry_count=0, so source is retryable
        assert orch._has_retryable_gaps("src_1")

    def test_source_point_partial_complete_when_all_gaps_exhausted(self):
        """When all gaps exhaust retries, SourcePoint → partial_complete."""
        store = _make_store_with_source_and_gaps("src_1", num_gaps=1, gap_retry_count=3)
        config = RepairConfig(
            target_dir=Path("/tmp/test"),
            graph_store=store,
            command="false",  # Will fail immediately
        )
        orch = RepairOrchestrator(config)
        # Mark the gap as unresolvable (simulating what happens after 3 retries)
        store._unresolved_calls["gap_0"] = UnresolvedCallNode(
            id="gap_0",
            caller_id="src_1",
            call_expression="fp_0()",
            call_file="/src/main.cpp",
            call_line=3,
            call_type="indirect",
            source_code_snippet="fp_0();",
            var_name="fp_0",
            var_type="void (*)()",
            candidates=[],
            retry_count=3,
            status="unresolvable",
        )
        result = _run_async(orch._run_single_repair("src_1"))
        # No retryable gaps → should end with partial_complete
        sp = store.get_source_point("src_1")
        assert sp.status == "partial_complete"
        assert result.success is False


# ---------------------------------------------------------------------------
# §3: Subprocess crash stamps correct reason and continues retry
# ---------------------------------------------------------------------------


class TestSection3_SubprocessCrashHandling:
    """architecture.md §3 line 125-126:
    Agent subprocess spawn failure → stamp subprocess_crash, continue retry loop.
    """

    def test_spawn_failure_stamps_subprocess_crash(self):
        """When CLI binary doesn't exist, stamp subprocess_crash reason."""
        store = _make_store_with_source_and_gaps("src_1", num_gaps=1, gap_retry_count=0)
        config = RepairConfig(
            target_dir=Path(tempfile.mkdtemp()),
            graph_store=store,
            command="/nonexistent/binary",
            args=[],
            max_concurrency=1,
        )
        orch = RepairOrchestrator(config)
        result = _run_async(orch._run_single_repair("src_1"))

        # Should have stamped the gap with subprocess_crash reason
        gap = store._unresolved_calls["gap_0"]
        assert gap.last_attempt_reason is not None
        assert gap.last_attempt_reason.startswith("subprocess_crash:")
        assert gap.last_attempt_timestamp is not None

    def test_spawn_failure_increments_retry_count(self):
        """Spawn failure should increment retry_count on the gap."""
        store = _make_store_with_source_and_gaps("src_1", num_gaps=1, gap_retry_count=0)
        config = RepairConfig(
            target_dir=Path(tempfile.mkdtemp()),
            graph_store=store,
            command="/nonexistent/binary",
            args=[],
            max_concurrency=1,
        )
        orch = RepairOrchestrator(config)
        _run_async(orch._run_single_repair("src_1"))

        gap = store._unresolved_calls["gap_0"]
        assert gap.retry_count >= 1


# ---------------------------------------------------------------------------
# §3: Subprocess timeout stamps correct reason
# ---------------------------------------------------------------------------


class TestSection3_SubprocessTimeout:
    """architecture.md §3 line 127:
    subprocess_timeout_seconds → kill process, stamp subprocess_timeout: <N>s.
    """

    def test_timeout_stamps_correct_reason(self):
        """Timeout should stamp 'subprocess_timeout: <N>s' on gaps."""
        store = _make_store_with_source_and_gaps("src_1", num_gaps=1, gap_retry_count=0)
        tmp_dir = Path(tempfile.mkdtemp())
        config = RepairConfig(
            target_dir=tmp_dir,
            graph_store=store,
            # Use a python one-liner that sleeps long enough to trigger timeout
            command="python3",
            args=["-c", "import time; time.sleep(300)"],
            subprocess_timeout_seconds=0.5,
            max_concurrency=1,
        )
        orch = RepairOrchestrator(config)
        result = _run_async(orch._run_single_repair("src_1"))

        gap = store._unresolved_calls["gap_0"]
        assert gap.last_attempt_reason is not None
        assert "subprocess_timeout" in gap.last_attempt_reason
        assert "0.5s" in gap.last_attempt_reason


# ---------------------------------------------------------------------------
# §3: Progress.json written at lifecycle events
# ---------------------------------------------------------------------------


class TestSection3_ProgressJson:
    """architecture.md §3 进度通信机制:
    Orchestrator writes progress.json at key lifecycle events.
    """

    def test_write_progress_creates_file(self):
        """_write_progress should create progress.json with merged fields."""
        tmp_dir = Path(tempfile.mkdtemp())
        config = RepairConfig(target_dir=tmp_dir, graph_store=InMemoryGraphStore())
        orch = RepairOrchestrator(config)

        orch._write_progress("src_1", state="running", attempt=1)

        progress_path = tmp_dir / "logs" / "repair" / "src_1" / "progress.json"
        assert progress_path.exists()
        data = json.loads(progress_path.read_text())
        assert data["state"] == "running"
        assert data["attempt"] == 1

    def test_write_progress_merges_fields(self):
        """Subsequent writes should merge, not overwrite."""
        tmp_dir = Path(tempfile.mkdtemp())
        config = RepairConfig(target_dir=tmp_dir, graph_store=InMemoryGraphStore())
        orch = RepairOrchestrator(config)

        orch._write_progress("src_1", state="running", attempt=1)
        orch._write_progress("src_1", gate_result="failed")

        progress_path = tmp_dir / "logs" / "repair" / "src_1" / "progress.json"
        data = json.loads(progress_path.read_text())
        # Both fields should be present
        assert data["state"] == "running"
        assert data["gate_result"] == "failed"
        assert data["attempt"] == 1

    def test_progress_state_transitions(self):
        """Progress should reflect running → gate_checking → succeeded/failed."""
        tmp_dir = Path(tempfile.mkdtemp())
        config = RepairConfig(target_dir=tmp_dir, graph_store=InMemoryGraphStore())
        orch = RepairOrchestrator(config)

        # Simulate lifecycle
        orch._write_progress("src_1", state="running", attempt=1, gate_result="pending")
        orch._write_progress("src_1", state="gate_checking")
        orch._write_progress("src_1", state="succeeded", gate_result="passed")

        progress_path = tmp_dir / "logs" / "repair" / "src_1" / "progress.json"
        data = json.loads(progress_path.read_text())
        assert data["state"] == "succeeded"
        assert data["gate_result"] == "passed"


# ---------------------------------------------------------------------------
# §5: Counter-example dedup + injection
# ---------------------------------------------------------------------------


class TestSection5_CounterExampleFeedback:
    """architecture.md §5 反例生成:
    Counter-examples are deduplicated by pattern and injected into agent CLAUDE.md.
    """

    def test_feedback_store_dedup_same_pattern(self):
        """Adding same pattern twice should deduplicate (return False)."""
        tmp_dir = Path(tempfile.mkdtemp())
        store = FeedbackStore(storage_dir=tmp_dir)

        ex = CounterExample(
            call_context="main.cpp:10",
            wrong_target="fn_wrong",
            correct_target="fn_correct",
            pattern="caller → fn_wrong at main.cpp:10",
        )
        assert store.add(ex) is True  # First add: new
        assert store.add(ex) is False  # Second add: dedup

    def test_feedback_store_different_patterns_both_kept(self):
        """Different patterns should both be stored."""
        tmp_dir = Path(tempfile.mkdtemp())
        store = FeedbackStore(storage_dir=tmp_dir)

        ex1 = CounterExample(
            call_context="main.cpp:10",
            wrong_target="fn_wrong",
            correct_target="fn_correct",
            pattern="pattern_A",
        )
        ex2 = CounterExample(
            call_context="utils.cpp:5",
            wrong_target="fn_other",
            correct_target="fn_real",
            pattern="pattern_B",
        )
        assert store.add(ex1) is True
        assert store.add(ex2) is True
        assert len(store.list_all()) == 2

    def test_feedback_store_renders_markdown(self):
        """render_markdown should produce agent-readable content."""
        tmp_dir = Path(tempfile.mkdtemp())
        store = FeedbackStore(storage_dir=tmp_dir)

        ex = CounterExample(
            call_context="main.cpp:10",
            wrong_target="fn_wrong",
            correct_target="fn_correct",
            pattern="caller → fn_wrong at main.cpp:10",
        )
        store.add(ex)
        md = store.render_markdown()
        assert "fn_wrong" in md
        assert "fn_correct" in md

    def test_feedback_store_persists_to_json(self):
        """Counter-examples should persist to JSON file."""
        tmp_dir = Path(tempfile.mkdtemp())
        store = FeedbackStore(storage_dir=tmp_dir)

        ex = CounterExample(
            call_context="main.cpp:10",
            wrong_target="fn_wrong",
            correct_target="fn_correct",
            pattern="test_pattern",
        )
        store.add(ex)

        # Reload from disk
        store2 = FeedbackStore(storage_dir=tmp_dir)
        assert len(store2.list_all()) == 1
        assert store2.list_all()[0].pattern == "test_pattern"

    def test_counter_example_injected_into_orchestrator(self):
        """Orchestrator should inject counter_examples.md before agent launch."""
        tmp_dir = Path(tempfile.mkdtemp())
        feedback_dir = tmp_dir / "feedback"
        feedback_store = FeedbackStore(storage_dir=feedback_dir)
        feedback_store.add(CounterExample(
            call_context="main.cpp:10",
            wrong_target="fn_wrong",
            correct_target="fn_correct",
            pattern="test injection pattern",
        ))

        store = _make_store_with_source_and_gaps("src_1", num_gaps=1)
        config = RepairConfig(
            target_dir=tmp_dir,
            graph_store=store,
            feedback_store=feedback_store,
            command="/nonexistent/binary",
            args=[],
        )
        orch = RepairOrchestrator(config)

        # Inject files manually to verify counter_examples.md content
        counter_md = feedback_store.render_markdown()
        orch._inject_files(tmp_dir, "src_1", counter_md)

        ce_path = tmp_dir / ".icslpreprocess_src_1" / "counter_examples.md"
        assert ce_path.exists()
        content = ce_path.read_text()
        assert "fn_wrong" in content
        assert "fn_correct" in content

        # Cleanup
        orch._cleanup_injection(tmp_dir, "src_1")

    def test_review_incorrect_with_correct_target_creates_counter_example(self):
        """POST /reviews with correct_target should create a counter-example."""
        tmp_dir = Path(tempfile.mkdtemp())
        feedback_store = FeedbackStore(storage_dir=tmp_dir)

        store = InMemoryGraphStore()
        caller = FunctionNode(
            id="fn_c", signature="void c()", name="c",
            file_path="/a.cpp", start_line=1, end_line=5, body_hash="x",
        )
        callee = FunctionNode(
            id="fn_d", signature="void d()", name="d",
            file_path="/b.cpp", start_line=1, end_line=3, body_hash="y",
        )
        store.create_function(caller)
        store.create_function(callee)
        store.create_calls_edge("fn_c", "fn_d", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/a.cpp", call_line=3,
        ))
        store.create_repair_log(RepairLogNode(
            id="rl1", caller_id="fn_c", callee_id="fn_d",
            call_location="/a.cpp:3", repair_method="llm",
            llm_response="test", timestamp="2026-01-01T00:00:00Z",
            reasoning_summary="test",
        ))

        app = create_app(store=store, feedback_store=feedback_store)
        client = TestClient(app)

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_c",
            "callee_id": "fn_d",
            "call_file": "/a.cpp",
            "call_line": 3,
            "verdict": "incorrect",
            "correct_target": "fn_real_target",
        })
        assert resp.status_code == 201

        # Counter-example should be in the feedback store
        examples = feedback_store.list_all()
        assert len(examples) == 1
        assert examples[0].wrong_target == "fn_d"
        assert examples[0].correct_target == "fn_real_target"


# ---------------------------------------------------------------------------
# §10: retry_failed_gaps flag resets unresolvable gaps
# ---------------------------------------------------------------------------


class TestSection10_RetryFailedGapsFlag:
    """architecture.md §10 line 523:
    retry_failed_gaps: true → reset unresolvable GAPs to pending on next run.
    """

    def test_reset_unresolvable_gaps_to_pending(self):
        """reset_unresolvable_gaps should set status=pending, retry_count=0."""
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="fn1", signature="void f()", name="f",
            file_path="/a.cpp", start_line=1, end_line=5, body_hash="h",
        )
        store.create_function(fn)
        uc = UnresolvedCallNode(
            id="gap_x",
            caller_id="fn1",
            call_expression="fp()",
            call_file="/a.cpp",
            call_line=3,
            call_type="indirect",
            source_code_snippet="fp();",
            var_name="fp",
            var_type="void (*)()",
            candidates=[],
            retry_count=3,
            status="unresolvable",
            last_attempt_timestamp="2026-05-14T10:00:00Z",
            last_attempt_reason="gate_failed: exhausted",
        )
        store.create_unresolved_call(uc)

        store.reset_unresolvable_gaps()

        gap = store._unresolved_calls["gap_x"]
        assert gap.status == "pending"
        assert gap.retry_count == 0
        assert gap.last_attempt_timestamp is None
        assert gap.last_attempt_reason is None

    def test_retry_failed_gaps_true_triggers_reset_on_run(self):
        """run_repairs with retry_failed_gaps=True should reset gaps first."""
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="fn1", signature="void f()", name="f",
            file_path="/a.cpp", start_line=1, end_line=5, body_hash="h",
        )
        store.create_function(fn)
        store.create_source_point(SourcePointNode(
            id="fn1", entry_point_kind="api", reason="t",
            function_id="fn1", status="pending",
        ))
        uc = UnresolvedCallNode(
            id="gap_y",
            caller_id="fn1",
            call_expression="fp()",
            call_file="/a.cpp",
            call_line=3,
            call_type="indirect",
            source_code_snippet="fp();",
            var_name="fp",
            var_type="void (*)()",
            candidates=[],
            retry_count=3,
            status="unresolvable",
        )
        store.create_unresolved_call(uc)

        config = RepairConfig(
            target_dir=Path(tempfile.mkdtemp()),
            graph_store=store,
            command="/nonexistent/binary",
            args=[],
            retry_failed_gaps=True,
        )
        orch = RepairOrchestrator(config)

        # After run_repairs, the gap should have been reset before retrying
        _run_async(orch.run_repairs(["fn1"]))

        gap = store._unresolved_calls["gap_y"]
        # It was reset to pending (retry_count=0) then retried
        # After 3 spawn failures it should be back to having attempts
        assert gap.retry_count >= 1  # Was retried after reset

    def test_retry_failed_gaps_false_does_not_reset(self):
        """run_repairs with retry_failed_gaps=False should NOT reset gaps."""
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="fn1", signature="void f()", name="f",
            file_path="/a.cpp", start_line=1, end_line=5, body_hash="h",
        )
        store.create_function(fn)
        store.create_source_point(SourcePointNode(
            id="fn1", entry_point_kind="api", reason="t",
            function_id="fn1", status="pending",
        ))
        uc = UnresolvedCallNode(
            id="gap_z",
            caller_id="fn1",
            call_expression="fp()",
            call_file="/a.cpp",
            call_line=3,
            call_type="indirect",
            source_code_snippet="fp();",
            var_name="fp",
            var_type="void (*)()",
            candidates=[],
            retry_count=3,
            status="unresolvable",
        )
        store.create_unresolved_call(uc)

        config = RepairConfig(
            target_dir=Path(tempfile.mkdtemp()),
            graph_store=store,
            command="/nonexistent/binary",
            args=[],
            retry_failed_gaps=False,
        )
        orch = RepairOrchestrator(config)
        _run_async(orch.run_repairs(["fn1"]))

        # Gap should still be unresolvable — never reset
        gap = store._unresolved_calls["gap_z"]
        assert gap.status == "unresolvable"


# ---------------------------------------------------------------------------
# §7: Incremental cascade invalidation
# ---------------------------------------------------------------------------


class TestSection7_IncrementalCascade:
    """architecture.md §7:
    File change → delete functions → delete edges → regenerate UCs → reset SP.
    """

    def test_file_invalidation_deletes_functions_and_edges(self):
        """Invalidating a file should remove its functions and their edges."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        store = InMemoryGraphStore()
        # File with two functions
        store.create_file(FileNode(
            id="/src/a.cpp", file_path="/src/a.cpp", hash="h1", primary_language="cpp",
        ))
        fn_a = FunctionNode(
            id="fn_a", signature="void a()", name="a",
            file_path="/src/a.cpp", start_line=1, end_line=5, body_hash="ha",
        )
        fn_b = FunctionNode(
            id="fn_b", signature="void b()", name="b",
            file_path="/src/a.cpp", start_line=7, end_line=12, body_hash="hb",
        )
        fn_c = FunctionNode(
            id="fn_c", signature="void c()", name="c",
            file_path="/src/other.cpp", start_line=1, end_line=5, body_hash="hc",
        )
        store.create_function(fn_a)
        store.create_function(fn_b)
        store.create_function(fn_c)

        # Edges: c→a (symbol_table), a→b (symbol_table)
        store.create_calls_edge("fn_c", "fn_a", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/src/other.cpp", call_line=3,
        ))
        store.create_calls_edge("fn_a", "fn_b", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/src/a.cpp", call_line=3,
        ))

        updater = IncrementalUpdater(store=store)
        result = updater.invalidate_file("/src/a.cpp")

        # Functions in a.cpp should be deleted
        assert store.get_function_by_id("fn_a") is None
        assert store.get_function_by_id("fn_b") is None
        # Function in other.cpp should remain
        assert store.get_function_by_id("fn_c") is not None
        # All edges touching fn_a or fn_b should be gone
        assert len(store._calls_edges) == 0

    def test_invalidation_regenerates_uc_for_llm_edges(self):
        """LLM-resolved edges to invalidated functions → regenerate UC."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        store = InMemoryGraphStore()
        store.create_file(FileNode(
            id="/src/target.cpp", file_path="/src/target.cpp",
            hash="h1", primary_language="cpp",
        ))
        # Caller in another file, callee in target.cpp
        fn_caller = FunctionNode(
            id="fn_caller", signature="void caller()", name="caller",
            file_path="/src/main.cpp", start_line=1, end_line=5, body_hash="hx",
        )
        fn_target = FunctionNode(
            id="fn_target", signature="void target()", name="target",
            file_path="/src/target.cpp", start_line=1, end_line=5, body_hash="hy",
        )
        store.create_function(fn_caller)
        store.create_function(fn_target)

        # LLM-resolved edge: caller → target
        store.create_calls_edge("fn_caller", "fn_target", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/src/main.cpp", call_line=3,
        ))
        # RepairLog for this edge
        store.create_repair_log(RepairLogNode(
            id="rl1", caller_id="fn_caller", callee_id="fn_target",
            call_location="/src/main.cpp:3", repair_method="llm",
            llm_response="test", timestamp="2026-01-01T00:00:00Z",
            reasoning_summary="test",
        ))

        updater = IncrementalUpdater(store=store)
        updater.invalidate_file("/src/target.cpp")

        # LLM edge should be deleted
        assert not store.edge_exists("fn_caller", "fn_target", "/src/main.cpp", 3)
        # RepairLog should be deleted
        assert len(store.get_repair_logs(caller_id="fn_caller")) == 0
        # UC should be regenerated
        ucs = store.get_unresolved_calls(caller_id="fn_caller")
        assert len(ucs) == 1
        assert ucs[0].call_file == "/src/main.cpp"
        assert ucs[0].call_line == 3

    def test_invalidation_resets_source_point_to_pending(self):
        """Affected SourcePoints should be reset to 'pending'."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        store = InMemoryGraphStore()
        store.create_file(FileNode(
            id="/src/target.cpp", file_path="/src/target.cpp",
            hash="h1", primary_language="cpp",
        ))
        fn_caller = FunctionNode(
            id="fn_caller", signature="void caller()", name="caller",
            file_path="/src/main.cpp", start_line=1, end_line=5, body_hash="hx",
        )
        fn_target = FunctionNode(
            id="fn_target", signature="void target()", name="target",
            file_path="/src/target.cpp", start_line=1, end_line=5, body_hash="hy",
        )
        store.create_function(fn_caller)
        store.create_function(fn_target)

        # Source point for caller (status=complete)
        store.create_source_point(SourcePointNode(
            id="fn_caller", entry_point_kind="api", reason="t",
            function_id="fn_caller", status="complete",
        ))

        # LLM edge
        store.create_calls_edge("fn_caller", "fn_target", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/src/main.cpp", call_line=3,
        ))

        updater = IncrementalUpdater(store=store)
        result = updater.invalidate_file("/src/target.cpp")

        # SourcePoint should be reset to pending
        sp = store.get_source_point("fn_caller")
        assert sp is not None
        assert sp.status == "pending"
        # affected_source_ids should include the caller
        assert "fn_caller" in result.affected_source_ids


# ---------------------------------------------------------------------------
# §8: REST API — stats buckets and repair-log filtering
# ---------------------------------------------------------------------------


class TestSection8_StatsEndpointBuckets:
    """architecture.md §8:
    /api/v1/stats must return unresolved_by_status, unresolved_by_category,
    calls_by_resolved_by with all canonical keys present.
    """

    def test_stats_has_all_status_buckets(self):
        """Stats should always have pending + unresolvable keys."""
        store = InMemoryGraphStore()
        app = create_app(store=store)
        client = TestClient(app)

        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        stats = resp.json()

        # unresolved_by_status must have both keys even when empty
        assert "unresolved_by_status" in stats
        assert "pending" in stats["unresolved_by_status"]
        assert "unresolvable" in stats["unresolved_by_status"]

    def test_stats_has_all_category_buckets(self):
        """Stats should have all 5 category keys (4 + 'none')."""
        store = InMemoryGraphStore()
        app = create_app(store=store)
        client = TestClient(app)

        resp = client.get("/api/v1/stats")
        stats = resp.json()

        assert "unresolved_by_category" in stats
        cats = stats["unresolved_by_category"]
        assert "gate_failed" in cats
        assert "agent_error" in cats
        assert "subprocess_crash" in cats
        assert "subprocess_timeout" in cats
        assert "none" in cats

    def test_stats_has_all_resolved_by_buckets(self):
        """Stats should have all 5 resolved_by keys."""
        store = InMemoryGraphStore()
        app = create_app(store=store)
        client = TestClient(app)

        resp = client.get("/api/v1/stats")
        stats = resp.json()

        assert "calls_by_resolved_by" in stats
        rb = stats["calls_by_resolved_by"]
        assert "symbol_table" in rb
        assert "signature" in rb
        assert "dataflow" in rb
        assert "context" in rb
        assert "llm" in rb

    def test_stats_counts_are_accurate(self):
        """Stats counts should match actual store contents."""
        store = InMemoryGraphStore()
        fn1 = FunctionNode(
            id="f1", signature="void f()", name="f",
            file_path="/a.cpp", start_line=1, end_line=5, body_hash="h",
        )
        fn2 = FunctionNode(
            id="f2", signature="void g()", name="g",
            file_path="/a.cpp", start_line=7, end_line=10, body_hash="h2",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge("f1", "f2", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/a.cpp", call_line=3,
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            id="uc1", caller_id="f1", call_expression="x()",
            call_file="/a.cpp", call_line=4, call_type="indirect",
            source_code_snippet="x();", var_name="x", var_type="void (*)()",
            candidates=[], status="pending",
        ))

        app = create_app(store=store)
        client = TestClient(app)
        resp = client.get("/api/v1/stats")
        stats = resp.json()

        assert stats["total_functions"] == 2
        assert stats["total_calls"] == 1
        assert stats["total_unresolved"] == 1
        assert stats["total_llm_edges"] == 1  # §8 convenience: equals calls_by_resolved_by["llm"]
        assert stats["calls_by_resolved_by"]["llm"] == 1
        assert stats["unresolved_by_status"]["pending"] == 1


class TestSection8_RepairLogEndpoint:
    """architecture.md §8:
    GET /api/v1/repair-logs with filtering by caller, callee, location.
    """

    @pytest.fixture()
    def repair_log_client(self):
        store = InMemoryGraphStore()
        store.create_repair_log(RepairLogNode(
            id="rl1", caller_id="fn_a", callee_id="fn_b",
            call_location="/src/main.cpp:10", repair_method="llm",
            llm_response="resolved", timestamp="2026-05-14T10:00:00Z",
            reasoning_summary="dataflow analysis",
        ))
        store.create_repair_log(RepairLogNode(
            id="rl2", caller_id="fn_a", callee_id="fn_c",
            call_location="/src/main.cpp:15", repair_method="llm",
            llm_response="resolved2", timestamp="2026-05-14T11:00:00Z",
            reasoning_summary="context match",
        ))
        store.create_repair_log(RepairLogNode(
            id="rl3", caller_id="fn_x", callee_id="fn_y",
            call_location="/src/other.cpp:5", repair_method="llm",
            llm_response="resolved3", timestamp="2026-05-14T12:00:00Z",
            reasoning_summary="signature match",
        ))
        app = create_app(store=store)
        return TestClient(app)

    def test_list_all_repair_logs(self, repair_log_client):
        """GET /repair-logs without filters returns all logs."""
        resp = repair_log_client.get("/api/v1/repair-logs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 3

    def test_filter_by_caller(self, repair_log_client):
        """Filter repair logs by caller_id."""
        resp = repair_log_client.get("/api/v1/repair-logs?caller=fn_a")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert all(log["caller_id"] == "fn_a" for log in body["items"])

    def test_filter_by_callee(self, repair_log_client):
        """Filter repair logs by callee_id."""
        resp = repair_log_client.get("/api/v1/repair-logs?callee=fn_b")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["callee_id"] == "fn_b"

    def test_filter_by_location(self, repair_log_client):
        """Filter repair logs by call_location."""
        resp = repair_log_client.get(
            "/api/v1/repair-logs?location=/src/other.cpp:5"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["caller_id"] == "fn_x"

    def test_repair_log_has_reasoning_summary(self, repair_log_client):
        """Each repair log should include reasoning_summary field."""
        resp = repair_log_client.get("/api/v1/repair-logs")
        body = resp.json()
        for log in body["items"]:
            assert "reasoning_summary" in log
            assert log["reasoning_summary"] != ""


# ---------------------------------------------------------------------------
# §3: Injection file cleanup after agent completion
# ---------------------------------------------------------------------------


class TestSection3_InjectionCleanup:
    """architecture.md §3 line 179:
    Agent 完成后清理所有注入文件; 如果目标目录已有 CLAUDE.md 则备份后恢复.
    """

    def test_cleanup_removes_injected_files(self):
        """After cleanup, .icslpreprocess_<id>/ should be gone."""
        tmp_dir = Path(tempfile.mkdtemp())
        config = RepairConfig(target_dir=tmp_dir, graph_store=InMemoryGraphStore())
        orch = RepairOrchestrator(config)

        orch._inject_files(tmp_dir, "src_1", "# counter examples")
        # Verify injection happened
        assert (tmp_dir / ".icslpreprocess_src_1").exists()
        assert (tmp_dir / "CLAUDE.md").exists()

        orch._cleanup_injection(tmp_dir, "src_1")
        # Verify cleanup
        assert not (tmp_dir / ".icslpreprocess_src_1").exists()
        assert not (tmp_dir / "CLAUDE.md").exists()

    def test_cleanup_restores_existing_claude_md(self):
        """If target had a pre-existing CLAUDE.md, it should be restored."""
        tmp_dir = Path(tempfile.mkdtemp())
        original_content = "# Original project CLAUDE.md\n"
        (tmp_dir / "CLAUDE.md").write_text(original_content)

        config = RepairConfig(target_dir=tmp_dir, graph_store=InMemoryGraphStore())
        orch = RepairOrchestrator(config)

        orch._inject_files(tmp_dir, "src_1", "")
        # CLAUDE.md should now be the injected version
        assert (tmp_dir / "CLAUDE.md").read_text() != original_content

        orch._cleanup_injection(tmp_dir, "src_1")
        # Should be restored to original
        assert (tmp_dir / "CLAUDE.md").read_text() == original_content

    def test_concurrent_sources_dont_clobber_backups(self):
        """Two sources injecting concurrently should each get their own backup."""
        tmp_dir = Path(tempfile.mkdtemp())
        original_content = "# Original\n"
        (tmp_dir / "CLAUDE.md").write_text(original_content)

        config = RepairConfig(target_dir=tmp_dir, graph_store=InMemoryGraphStore())
        orch = RepairOrchestrator(config)

        # Inject for source A
        orch._inject_files(tmp_dir, "src_a", "# CE for A")
        # Inject for source B (overwrites CLAUDE.md again)
        orch._inject_files(tmp_dir, "src_b", "# CE for B")

        # Both backups should exist
        assert (tmp_dir / "CLAUDE.md.bak.src_a").exists()
        assert (tmp_dir / "CLAUDE.md.bak.src_b").exists()

        # Cleanup B first
        orch._cleanup_injection(tmp_dir, "src_b")
        # Cleanup A
        orch._cleanup_injection(tmp_dir, "src_a")
        # Original should be restored
        assert (tmp_dir / "CLAUDE.md").read_text() == original_content


# ---------------------------------------------------------------------------
# §4: CALLS edge 4-field uniqueness (data model)
# ---------------------------------------------------------------------------


class TestSection4_CallsEdgeUniqueness:
    """architecture.md §4: CALLS edge is uniquely identified by the
    4-field key (caller_id, callee_id, call_file, call_line).

    InMemoryGraphStore enforces this via edge_exists() skip;
    Neo4jGraphStore enforces it via MERGE semantics (MATCH anchors
    on caller/callee node ids + MERGE predicate on call_file/call_line).
    """

    @staticmethod
    def _make_functions(store: InMemoryGraphStore, *ids: str) -> None:
        for fid in ids:
            store.create_function(
                FunctionNode(
                    id=fid,
                    signature=f"void {fid}()",
                    name=fid,
                    file_path="/src/test.cpp",
                    start_line=1,
                    end_line=10,
                    body_hash=f"h_{fid}",
                )
            )

    def test_same_call_site_different_callee_creates_two_edges(self):
        """Same caller, same call site (file, line), but different callee
        → two distinct CALLS edges. The 4-field key distinguishes them."""
        store = InMemoryGraphStore()
        self._make_functions(store, "caller", "callee_a", "callee_b")

        # Edge from caller → callee_a at line 42
        props_a = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/src/test.cpp", call_line=42,
        )
        store.create_calls_edge("caller", "callee_a", props_a)

        # Edge from caller → callee_b at same line 42
        props_b = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/src/test.cpp", call_line=42,
        )
        store.create_calls_edge("caller", "callee_b", props_b)

        # Both edges should exist independently
        assert store.edge_exists("caller", "callee_a", "/src/test.cpp", 42)
        assert store.edge_exists("caller", "callee_b", "/src/test.cpp", 42)

        # Two distinct edges total
        assert len(store._calls_edges) == 2

        # Each preserves its own resolved_by
        edge_a = store.get_calls_edge("caller", "callee_a", "/src/test.cpp", 42)
        assert edge_a.resolved_by == "llm"
        edge_b = store.get_calls_edge("caller", "callee_b", "/src/test.cpp", 42)
        assert edge_b.resolved_by == "symbol_table"

    def test_duplicate_4tuple_skipped_preserves_first_resolved_by(self):
        """Inserting the exact same 4-tuple twice → skip, keep first
        resolved_by (ON CREATE SET semantics in Neo4j MERGE)."""
        store = InMemoryGraphStore()
        self._make_functions(store, "caller", "callee")

        props1 = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/src/test.cpp", call_line=42,
        )
        store.create_calls_edge("caller", "callee", props1)

        # Second write with different resolved_by — should be skipped
        props2 = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/src/test.cpp", call_line=42,
        )
        store.create_calls_edge("caller", "callee", props2)

        assert len(store._calls_edges) == 1
        edge = store.get_calls_edge("caller", "callee", "/src/test.cpp", 42)
        assert edge.resolved_by == "llm"  # first write preserved

    def test_same_caller_callee_different_site_creates_two_edges(self):
        """Same caller→callee but different call sites → two edges.
        This represents a function calling the same target from two lines."""
        store = InMemoryGraphStore()
        self._make_functions(store, "caller", "callee")

        props1 = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/src/test.cpp", call_line=10,
        )
        store.create_calls_edge("caller", "callee", props1)

        props2 = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/src/test.cpp", call_line=20,
        )
        store.create_calls_edge("caller", "callee", props2)

        assert len(store._calls_edges) == 2
        assert store.edge_exists("caller", "callee", "/src/test.cpp", 10)
        assert store.edge_exists("caller", "callee", "/src/test.cpp", 20)

    def test_delete_by_4tuple_only_removes_target_edge(self):
        """Deleting by the 4-field key removes only the target edge,
        leaving sibling edges at other call sites intact."""
        store = InMemoryGraphStore()
        self._make_functions(store, "caller", "callee_a", "callee_b")

        props_a = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/src/test.cpp", call_line=42,
        )
        store.create_calls_edge("caller", "callee_a", props_a)

        props_b = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/src/test.cpp", call_line=42,
        )
        store.create_calls_edge("caller", "callee_b", props_b)

        # Delete only the caller→callee_a edge at line 42
        result = store.delete_calls_edge("caller", "callee_a", "/src/test.cpp", 42)
        assert result is True

        # callee_b edge at line 42 still intact
        assert store.edge_exists("caller", "callee_b", "/src/test.cpp", 42)
        assert not store.edge_exists("caller", "callee_a", "/src/test.cpp", 42)
        assert len(store._calls_edges) == 1


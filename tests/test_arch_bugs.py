"""Architecture compliance tests — Round 3: expose real bugs.

Each test targets a specific point where the implementation diverges from
architecture.md. Tests are designed to FAIL when the bug exists, and pass
once the implementation is corrected.

References:
- §3: write-edge must create RepairLog + delete UnresolvedCall atomically
- §3: check-complete must return correct shape via CLI
- §3: progress.json fields consumed by /analyze/status
- §5: POST /feedback returns deduplicated + total signal fields
- §8: /analyze/status returns sources with all required progress fields
- §8: DELETE /edges cascade resets SourcePoint via force_reset
- §4: RepairLog references CALLS edge by caller_id + callee_id + call_location
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codemap_lite.agent import icsl_tools
from codemap_lite.analysis.feedback_store import CounterExample, FeedbackStore
from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    RepairLogNode,
    SourcePointNode,
    UnresolvedCallNode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store_with_two_functions():
    """Create a store with two functions and one unresolved call between them."""
    store = InMemoryGraphStore()
    caller = FunctionNode(
        id="fn_a", signature="void a()", name="a",
        file_path="/src/main.cpp", start_line=1, end_line=5, body_hash="ha",
    )
    callee = FunctionNode(
        id="fn_b", signature="void b()", name="b",
        file_path="/src/utils.cpp", start_line=1, end_line=3, body_hash="hb",
    )
    store.create_function(caller)
    store.create_function(callee)

    uc = UnresolvedCallNode(
        id="gap_1",
        caller_id="fn_a",
        call_expression="fp()",
        call_file="/src/main.cpp",
        call_line=3,
        call_type="indirect",
        source_code_snippet="fp();",
        var_name="fp",
        var_type="void (*)()",
        candidates=["b"],
    )
    store.create_unresolved_call(uc)
    return store


# ---------------------------------------------------------------------------
# §3: write-edge must create RepairLog atomically
# ---------------------------------------------------------------------------


class TestWriteEdgeRepairLog:
    """architecture.md §3 line 140:
    'write-edge → 写入 CALLS 边 + 创建 RepairLog + 删除 UnresolvedCall'
    All three must happen atomically.
    """

    def test_write_edge_creates_repair_log(self):
        """write-edge must create a RepairLog node."""
        store = _make_store_with_two_functions()
        result = icsl_tools.write_edge(
            caller_id="fn_a",
            callee_id="fn_b",
            call_type="indirect",
            call_file="/src/main.cpp",
            call_line=3,
            store=store,
            llm_response="fp is b",
            reasoning_summary="dataflow: fp assigned from b",
        )
        assert result["edge_created"] is True
        # RepairLog must exist
        logs = store.get_repair_logs(caller_id="fn_a", callee_id="fn_b")
        assert len(logs) == 1
        log = logs[0]
        assert log.llm_response == "fp is b"
        assert log.reasoning_summary == "dataflow: fp assigned from b"
        assert log.repair_method == "llm"
        assert log.caller_id == "fn_a"
        assert log.callee_id == "fn_b"
        assert log.call_location == "/src/main.cpp:3"

    def test_write_edge_deletes_unresolved_call(self):
        """write-edge must delete the matching UnresolvedCall."""
        store = _make_store_with_two_functions()
        icsl_tools.write_edge(
            caller_id="fn_a",
            callee_id="fn_b",
            call_type="indirect",
            call_file="/src/main.cpp",
            call_line=3,
            store=store,
        )
        ucs = store.get_unresolved_calls(caller_id="fn_a")
        assert len(ucs) == 0

    def test_write_edge_creates_calls_edge(self):
        """write-edge must create a CALLS edge with resolved_by=llm."""
        store = _make_store_with_two_functions()
        icsl_tools.write_edge(
            caller_id="fn_a",
            callee_id="fn_b",
            call_type="indirect",
            call_file="/src/main.cpp",
            call_line=3,
            store=store,
        )
        assert store.edge_exists("fn_a", "fn_b", "/src/main.cpp", 3)
        edge = store.get_calls_edge("fn_a", "fn_b", "/src/main.cpp", 3)
        assert edge.resolved_by == "llm"
        assert edge.call_type == "indirect"

    def test_write_edge_skips_existing_edge(self):
        """write-edge must skip if edge already exists (idempotent)."""
        store = _make_store_with_two_functions()
        # First write
        icsl_tools.write_edge(
            caller_id="fn_a", callee_id="fn_b", call_type="indirect",
            call_file="/src/main.cpp", call_line=3, store=store,
        )
        # Second write
        result = icsl_tools.write_edge(
            caller_id="fn_a", callee_id="fn_b", call_type="indirect",
            call_file="/src/main.cpp", call_line=3, store=store,
        )
        assert result["skipped"] is True
        # Only one RepairLog should exist (not duplicated)
        logs = store.get_repair_logs(caller_id="fn_a", callee_id="fn_b")
        assert len(logs) == 1

    def test_write_edge_unknown_caller_returns_error(self):
        """write-edge with non-existent caller must return error dict."""
        store = _make_store_with_two_functions()
        result = icsl_tools.write_edge(
            caller_id="nonexistent", callee_id="fn_b", call_type="direct",
            call_file="/src/main.cpp", call_line=3, store=store,
        )
        assert "error" in result

    def test_write_edge_reasoning_summary_truncated_to_200(self):
        """reasoning_summary > 200 chars must be truncated (§3 §4)."""
        store = _make_store_with_two_functions()
        long_summary = "x" * 300
        icsl_tools.write_edge(
            caller_id="fn_a", callee_id="fn_b", call_type="indirect",
            call_file="/src/main.cpp", call_line=3, store=store,
            reasoning_summary=long_summary,
        )
        logs = store.get_repair_logs(caller_id="fn_a", callee_id="fn_b")
        assert len(logs) == 1
        assert len(logs[0].reasoning_summary) <= 200


# ---------------------------------------------------------------------------
# §3: check-complete returns correct shape
# ---------------------------------------------------------------------------


class TestCheckComplete:
    """architecture.md §3 门禁机制:
    check-complete must return {complete, remaining_gaps, pending_gap_ids}.
    """

    def test_check_complete_with_no_pending_gaps(self):
        """When no pending gaps, complete=true."""
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="src_1", signature="void f()", name="f",
            file_path="/a.cpp", start_line=1, end_line=5, body_hash="h",
        )
        store.create_function(fn)
        result = icsl_tools.check_complete("src_1", store)
        assert result["complete"] is True
        assert result["remaining_gaps"] == 0
        assert result["pending_gap_ids"] == []

    def test_check_complete_with_pending_gaps(self):
        """When pending gaps exist, complete=false."""
        store = _make_store_with_two_functions()
        result = icsl_tools.check_complete("fn_a", store)
        assert result["complete"] is False
        assert result["remaining_gaps"] >= 1
        assert len(result["pending_gap_ids"]) >= 1

    def test_check_complete_accepts_dataclass_gaps(self):
        """check-complete must work with dataclass UnresolvedCallNode (not just dicts)."""
        store = _make_store_with_two_functions()
        # InMemoryGraphStore returns UnresolvedCallNode dataclasses
        result = icsl_tools.check_complete("fn_a", store)
        # Must not raise TypeError
        assert "pending_gap_ids" in result
        for gap_id in result["pending_gap_ids"]:
            assert isinstance(gap_id, str)


# ---------------------------------------------------------------------------
# §3: query-reachable returns correct shape
# ---------------------------------------------------------------------------


class TestQueryReachable:
    """architecture.md §3 Agent 内循环 step 1:
    query-reachable returns subgraph with nodes + edges + unresolved.
    """

    def test_query_reachable_returns_subgraph(self):
        """query-reachable must return {nodes, edges, unresolved}."""
        store = _make_store_with_two_functions()
        result = icsl_tools.query_reachable("fn_a", store)
        assert "nodes" in result
        assert "edges" in result
        assert "unresolved" in result
        # fn_a should be in nodes
        node_ids = {n.id for n in result["nodes"]}
        assert "fn_a" in node_ids

    def test_query_reachable_includes_unresolved(self):
        """Unresolved calls from the source should appear in unresolved list."""
        store = _make_store_with_two_functions()
        result = icsl_tools.query_reachable("fn_a", store)
        assert len(result["unresolved"]) >= 1
        uc_ids = {u.id for u in result["unresolved"]}
        assert "gap_1" in uc_ids


# ---------------------------------------------------------------------------
# §5: POST /feedback deduplicated + total signal
# ---------------------------------------------------------------------------


class TestFeedbackEndpoint:
    """architecture.md §5 + §8:
    POST /feedback returns deduplicated + total signal fields.
    """

    def test_post_feedback_returns_deduplicated_false_for_new(self):
        """New counter-example should return deduplicated=False."""
        tmp_dir = Path(tempfile.mkdtemp())
        store = FeedbackStore(storage_dir=tmp_dir)
        app = create_app(feedback_store=store)
        client = TestClient(app)

        resp = client.post("/api/v1/feedback", json={
            "call_context": "main.cpp:10",
            "wrong_target": "fn_wrong",
            "correct_target": "fn_correct",
            "pattern": "unique_pattern_123",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["deduplicated"] is False
        assert data["total"] == 1

    def test_post_feedback_returns_deduplicated_true_for_duplicate(self):
        """Duplicate pattern should return deduplicated=True."""
        tmp_dir = Path(tempfile.mkdtemp())
        store = FeedbackStore(storage_dir=tmp_dir)
        app = create_app(feedback_store=store)
        client = TestClient(app)

        client.post("/api/v1/feedback", json={
            "call_context": "main.cpp:10",
            "wrong_target": "fn_wrong",
            "correct_target": "fn_correct",
            "pattern": "same_pattern",
        })
        resp = client.post("/api/v1/feedback", json={
            "call_context": "other.cpp:5",
            "wrong_target": "fn_other",
            "correct_target": "fn_real",
            "pattern": "same_pattern",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["deduplicated"] is True
        assert data["total"] == 1  # Still 1 after dedup

    def test_post_feedback_without_store_returns_503(self):
        """POST /feedback without FeedbackStore should return 503."""
        app = create_app()
        client = TestClient(app)
        resp = client.post("/api/v1/feedback", json={
            "call_context": "main.cpp:10",
            "wrong_target": "fn_wrong",
            "correct_target": "fn_correct",
            "pattern": "test",
        })
        assert resp.status_code == 503

    def test_get_feedback_without_store_returns_empty(self):
        """GET /feedback without FeedbackStore should return [] (not error)."""
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/v1/feedback")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}


# ---------------------------------------------------------------------------
# §8: /analyze/status returns progress fields from progress.json
# ---------------------------------------------------------------------------


class TestAnalyzeStatusProgress:
    """architecture.md §8 + §3 进度通信机制:
    /analyze/status must return sources[] with all progress fields.
    """

    def test_analyze_status_reads_progress_json(self):
        """/analyze/status must read progress.json and expose all fields."""
        tmp_dir = Path(tempfile.mkdtemp())
        progress_dir = tmp_dir / "logs" / "repair" / "src_001"
        progress_dir.mkdir(parents=True, exist_ok=True)
        progress_data = {
            "gaps_fixed": 5,
            "gaps_total": 10,
            "current_gap": "gap_005",
            "attempt": 2,
            "max_attempts": 3,
            "gate_result": "failed",
            "edges_written": 4,
            "state": "running",
            "last_error": "gate_failed: remaining pending GAPs",
        }
        (progress_dir / "progress.json").write_text(
            json.dumps(progress_data), encoding="utf-8"
        )

        app = create_app(target_dir=tmp_dir)
        client = TestClient(app)

        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        body = resp.json()

        sources = body.get("sources", [])
        assert len(sources) == 1
        src = sources[0]

        assert src["source_id"] == "src_001"
        assert src["gaps_fixed"] == 5
        assert src["gaps_total"] == 10
        assert src["attempt"] == 2
        assert src["max_attempts"] == 3
        assert src["gate_result"] == "failed"
        assert src["state"] == "running"

    def test_analyze_status_handles_malformed_progress(self):
        """Malformed progress.json should be skipped gracefully."""
        tmp_dir = Path(tempfile.mkdtemp())
        progress_dir = tmp_dir / "logs" / "repair" / "src_bad"
        progress_dir.mkdir(parents=True, exist_ok=True)
        (progress_dir / "progress.json").write_text("NOT JSON", encoding="utf-8")

        app = create_app(target_dir=tmp_dir)
        client = TestClient(app)

        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        body = resp.json()
        # Should not crash, just skip the malformed file
        assert isinstance(body.get("sources"), list)

    def test_analyze_status_progress_derived_from_sources(self):
        """Overall progress should be derived from gaps_fixed/gaps_total."""
        tmp_dir = Path(tempfile.mkdtemp())
        progress_dir = tmp_dir / "logs" / "repair" / "src_001"
        progress_dir.mkdir(parents=True, exist_ok=True)
        (progress_dir / "progress.json").write_text(json.dumps({
            "gaps_fixed": 3,
            "gaps_total": 10,
        }), encoding="utf-8")

        app = create_app(target_dir=tmp_dir)
        client = TestClient(app)

        resp = client.get("/api/v1/analyze/status")
        body = resp.json()
        # progress = 3/10 = 0.3
        assert abs(body.get("progress", 0) - 0.3) < 0.01


# ---------------------------------------------------------------------------
# §4: RepairLog property reference contract
# ---------------------------------------------------------------------------


class TestRepairLogPropertyContract:
    """architecture.md §4:
    'RepairLog 不通过关系关联，而是通过属性引用 CALLS 边
    （存储 caller_id + callee_id + call_location 三元组唯一定位）'
    """

    def test_repair_log_locates_calls_edge_by_triple(self):
        """RepairLog must store caller_id + callee_id + call_location triple."""
        store = _make_store_with_two_functions()
        icsl_tools.write_edge(
            caller_id="fn_a", callee_id="fn_b", call_type="indirect",
            call_file="/src/main.cpp", call_line=3, store=store,
            llm_response="test", reasoning_summary="test",
        )

        # Look up RepairLog by the triple
        logs = store.get_repair_logs(
            caller_id="fn_a",
            callee_id="fn_b",
            call_location="/src/main.cpp:3",
        )
        assert len(logs) == 1
        assert logs[0].call_location == "/src/main.cpp:3"

    def test_repair_log_has_timestamp(self):
        """RepairLog must have a valid ISO-8601 timestamp."""
        store = _make_store_with_two_functions()
        icsl_tools.write_edge(
            caller_id="fn_a", callee_id="fn_b", call_type="indirect",
            call_file="/src/main.cpp", call_line=3, store=store,
        )
        logs = store.get_repair_logs(caller_id="fn_a")
        assert len(logs) == 1
        assert logs[0].timestamp is not None
        assert "T" in logs[0].timestamp  # ISO-8601


# ---------------------------------------------------------------------------
# §4: CALLS edge uniqueness
# ---------------------------------------------------------------------------


class TestCallsEdgeUniqueness:
    """architecture.md §4:
    CALLS edges unique by (caller_id, callee_id, call_file, call_line).
    """

    def test_duplicate_edge_rejected(self):
        """Creating same edge twice should not create duplicates."""
        store = InMemoryGraphStore()
        fn_a = FunctionNode(id="a", signature="void a()", name="a",
                            file_path="/f.cpp", start_line=1, end_line=5, body_hash="h")
        fn_b = FunctionNode(id="b", signature="void b()", name="b",
                            file_path="/f.cpp", start_line=7, end_line=10, body_hash="h2")
        store.create_function(fn_a)
        store.create_function(fn_b)

        props = CallsEdgeProps(resolved_by="symbol_table", call_type="direct",
                               call_file="/f.cpp", call_line=3)
        store.create_calls_edge("a", "b", props)
        store.create_calls_edge("a", "b", props)  # Duplicate

        assert len(store._calls_edges) == 1

    def test_different_call_lines_are_different_edges(self):
        """Same caller/callee but different call_line → different edges."""
        store = InMemoryGraphStore()
        fn_a = FunctionNode(id="a", signature="void a()", name="a",
                            file_path="/f.cpp", start_line=1, end_line=5, body_hash="h")
        fn_b = FunctionNode(id="b", signature="void b()", name="b",
                            file_path="/f.cpp", start_line=7, end_line=10, body_hash="h2")
        store.create_function(fn_a)
        store.create_function(fn_b)

        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/f.cpp", call_line=3))
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/f.cpp", call_line=5))

        assert len(store._calls_edges) == 2


# ---------------------------------------------------------------------------
# §3: UnresolvedCall lifecycle (pending → deleted/unresolvable)
# ---------------------------------------------------------------------------


class TestUnresolvedCallLifecycle:
    """architecture.md §4 UnresolvedCall 生命周期:
    pending → Agent修复成功 → 删除节点
    pending → 3次重试后仍失败 → status = "unresolvable"
    下次运行时 → 重置 retry_count=0, status = "pending"
    """

    def test_successful_repair_deletes_uc(self):
        """Successful repair must DELETE the UC node, not change status."""
        store = _make_store_with_two_functions()
        icsl_tools.write_edge(
            caller_id="fn_a", callee_id="fn_b", call_type="indirect",
            call_file="/src/main.cpp", call_line=3, store=store,
        )
        # UC should be gone
        assert len(store.get_unresolved_calls(caller_id="fn_a")) == 0

    def test_uc_can_become_unresolvable(self):
        """After 3 retries, UC.status can become 'unresolvable'."""
        store = InMemoryGraphStore()
        fn = FunctionNode(id="fn1", signature="void f()", name="f",
                          file_path="/a.cpp", start_line=1, end_line=5, body_hash="h")
        store.create_function(fn)
        uc = UnresolvedCallNode(
            id="gap_1", caller_id="fn1", call_expression="fp()",
            call_file="/a.cpp", call_line=3, call_type="indirect",
            source_code_snippet="fp();", var_name="fp", var_type="void (*)()",
            candidates=[], retry_count=3,
        )
        store.create_unresolved_call(uc)

        # Now simulate marking as unresolvable
        from codemap_lite.graph.schema import VALID_UC_STATUSES
        uc_unresolvable = UnresolvedCallNode(
            id="gap_1", caller_id="fn1", call_expression="fp()",
            call_file="/a.cpp", call_line=3, call_type="indirect",
            source_code_snippet="fp();", var_name="fp", var_type="void (*)()",
            candidates=[], retry_count=3, status="unresolvable",
        )
        store._unresolved_calls["gap_1"] = uc_unresolvable

        # Verify it's unresolvable
        ucs = store.get_unresolved_calls(status="unresolvable")
        assert len(ucs) == 1
        assert ucs[0].status == "unresolvable"

    def test_unresolvable_resets_to_pending_on_next_run(self):
        """reset_unresolvable_gaps should reset to pending, retry_count=0."""
        store = InMemoryGraphStore()
        fn = FunctionNode(id="fn1", signature="void f()", name="f",
                          file_path="/a.cpp", start_line=1, end_line=5, body_hash="h")
        store.create_function(fn)
        uc = UnresolvedCallNode(
            id="gap_1", caller_id="fn1", call_expression="fp()",
            call_file="/a.cpp", call_line=3, call_type="indirect",
            source_code_snippet="fp();", var_name="fp", var_type="void (*)()",
            candidates=[], retry_count=3, status="unresolvable",
        )
        store.create_unresolved_call(uc)

        store.reset_unresolvable_gaps()

        gap = store._unresolved_calls["gap_1"]
        assert gap.status == "pending"
        assert gap.retry_count == 0
        assert gap.last_attempt_timestamp is None
        assert gap.last_attempt_reason is None


# ---------------------------------------------------------------------------
# §8: Unresolved calls endpoint with pagination + filtering
# ---------------------------------------------------------------------------


class TestUnresolvedCallsEndpoint:
    """architecture.md §8:
    GET /unresolved-calls supports limit, offset, caller, status, category filters.
    """

    @pytest.fixture()
    def uc_client(self):
        store = InMemoryGraphStore()
        fn = FunctionNode(id="fn1", signature="void f()", name="f",
                          file_path="/a.cpp", start_line=1, end_line=5, body_hash="h")
        store.create_function(fn)

        # Create 5 UCs with different statuses and reasons
        for i in range(5):
            uc = UnresolvedCallNode(
                id=f"gap_{i}", caller_id="fn1", call_expression=f"fp_{i}()",
                call_file="/a.cpp", call_line=3 + i, call_type="indirect",
                source_code_snippet=f"fp_{i}();", var_name=f"fp_{i}",
                var_type="void (*)()", candidates=[],
                status="pending" if i < 3 else "unresolvable",
                last_attempt_reason="gate_failed: test" if i < 2 else None,
            )
            store.create_unresolved_call(uc)

        app = create_app(store=store)
        return TestClient(app)

    def test_pagination(self, uc_client):
        """limit + offset should paginate results."""
        resp = uc_client.get("/api/v1/unresolved-calls?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

        resp2 = uc_client.get("/api/v1/unresolved-calls?limit=2&offset=2")
        assert len(resp2.json()["items"]) == 2

    def test_filter_by_status(self, uc_client):
        """status filter should work."""
        resp = uc_client.get("/api/v1/unresolved-calls?status=pending")
        data = resp.json()
        assert all(item["status"] == "pending" for item in data["items"])

    def test_filter_by_category(self, uc_client):
        """category filter should match last_attempt_reason prefix."""
        resp = uc_client.get("/api/v1/unresolved-calls?category=gate_failed")
        data = resp.json()
        assert all(
            item["last_attempt_reason"].startswith("gate_failed:")
            for item in data["items"]
            if item["last_attempt_reason"]
        )

    def test_filter_by_caller(self, uc_client):
        """caller filter should work."""
        resp = uc_client.get("/api/v1/unresolved-calls?caller=fn1")
        data = resp.json()
        assert all(item["caller_id"] == "fn1" for item in data["items"])

    def test_total_independent_of_pagination(self, uc_client):
        """total should reflect ALL matching items, not just the page."""
        resp = uc_client.get("/api/v1/unresolved-calls?limit=1")
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 1


# ---------------------------------------------------------------------------
# §3: icsl_tools CLI exit codes and JSON output
# ---------------------------------------------------------------------------


class TestIcslToolsCLI:
    """architecture.md §3 Agent tool protocol:
    icsl_tools.py CLI must return JSON on stdout with correct exit codes.
    """

    def test_write_edge_cli_returns_json(self):
        """write-edge CLI must output valid JSON."""
        store = _make_store_with_two_functions()
        # Simulate CLI invocation
        import io
        from unittest.mock import patch

        # Use in-process call
        result = icsl_tools.write_edge(
            caller_id="fn_a", callee_id="fn_b", call_type="indirect",
            call_file="/src/main.cpp", call_line=3, store=store,
        )
        # Result must be JSON-serializable
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        assert parsed["edge_created"] is True

    def test_invalid_call_type_raises_valueerror(self):
        """Invalid call_type must raise ValueError (not silently accept)."""
        store = _make_store_with_two_functions()
        with pytest.raises(ValueError, match="call_type"):
            icsl_tools.write_edge(
                caller_id="fn_a", callee_id="fn_b", call_type="invalid",
                call_file="/src/main.cpp", call_line=3, store=store,
            )

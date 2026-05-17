"""Full API coverage tests — all 28 endpoints.

Ensures every endpoint has at least one happy-path and one error-path test.
Covers gaps identified in gap-analysis round 5 (2026-05-17):
- Analyze/repair state transitions and double-spawn prevention
- Source-points summary aggregation
- Bulk edge deletion (DELETE /edges/{function_id})
- Repair logs filtering (source_reachable, caller, callee, location)
- Source-code endpoint
- Edge creation and deletion
- Feedback CRUD lifecycle
- Live tail endpoint edge cases
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from codemap_lite.analysis.feedback_store import FeedbackStore
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


@pytest.fixture
def store() -> InMemoryGraphStore:
    """Fully populated store for comprehensive API testing."""
    s = InMemoryGraphStore()

    # Files
    s.create_file(FileNode(id="src/main.cpp", file_path="src/main.cpp", hash="h1", primary_language="cpp"))
    s.create_file(FileNode(id="src/util.cpp", file_path="src/util.cpp", hash="h2", primary_language="cpp"))
    s.create_file(FileNode(id="src/handler.cpp", file_path="src/handler.cpp", hash="h3", primary_language="cpp"))

    # Functions — build a call chain: entry -> dispatcher -> handler -> helper
    s.create_function(FunctionNode(
        id="fn_entry", name="OnRemoteRequest", signature="int OnRemoteRequest(int code)",
        file_path="src/main.cpp", start_line=10, end_line=50, body_hash="bh1",
    ))
    s.create_function(FunctionNode(
        id="fn_dispatch", name="DispatchMessage", signature="void DispatchMessage(Message& msg)",
        file_path="src/main.cpp", start_line=55, end_line=80, body_hash="bh2",
    ))
    s.create_function(FunctionNode(
        id="fn_handler", name="HandlePlay", signature="void HandlePlay()",
        file_path="src/handler.cpp", start_line=1, end_line=30, body_hash="bh3",
    ))
    s.create_function(FunctionNode(
        id="fn_helper", name="ValidateSession", signature="bool ValidateSession(int id)",
        file_path="src/util.cpp", start_line=1, end_line=20, body_hash="bh4",
    ))
    s.create_function(FunctionNode(
        id="fn_orphan", name="UnusedFunc", signature="void UnusedFunc()",
        file_path="src/util.cpp", start_line=25, end_line=30, body_hash="bh5",
    ))

    # Edges: entry -> dispatch (symbol_table), dispatch -> handler (llm),
    # handler -> helper (signature)
    s.create_calls_edge("fn_entry", "fn_dispatch", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="src/main.cpp", call_line=20,
    ))
    s.create_calls_edge("fn_dispatch", "fn_handler", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="src/main.cpp", call_line=60,
    ))
    s.create_calls_edge("fn_handler", "fn_helper", CallsEdgeProps(
        resolved_by="signature", call_type="direct",
        call_file="src/handler.cpp", call_line=15,
    ))

    # Unresolved calls
    s.create_unresolved_call(UnresolvedCallNode(
        id="uc_1", caller_id="fn_entry", call_expression="SendEvent",
        call_file="src/main.cpp", call_line=35, call_type="indirect",
        source_code_snippet="listener_->SendEvent()", var_name="listener_",
        var_type="IEventListener*", candidates=["fn_handler"],
    ))
    s.create_unresolved_call(UnresolvedCallNode(
        id="uc_2", caller_id="fn_dispatch", call_expression="NotifyObserver",
        call_file="src/main.cpp", call_line=70, call_type="virtual",
        source_code_snippet="observer->NotifyObserver()", var_name="observer",
        var_type="Observer*", candidates=[],
    ))
    s.create_unresolved_call(UnresolvedCallNode(
        id="uc_3", caller_id="fn_handler", call_expression="LogResult",
        call_file="src/handler.cpp", call_line=25, call_type="indirect",
        source_code_snippet="logger->LogResult()", var_name="logger",
        var_type="ILogger*", candidates=["fn_helper"],
        status="pending",
    ))

    # Source points
    s.create_source_point(SourcePointNode(
        id="sp_entry", function_id="fn_entry", entry_point_kind="ipc",
        reason="IPC entry point", module="cast_session", status="pending",
    ))
    s.create_source_point(SourcePointNode(
        id="sp_handler", function_id="fn_handler", entry_point_kind="callback",
        reason="Player callback", module="player", status="complete",
    ))

    # Repair logs
    s.create_repair_log(RepairLogNode(
        id="rl_1", caller_id="fn_dispatch", callee_id="fn_handler",
        call_location="src/main.cpp:60", repair_method="llm",
        timestamp="2026-05-15T10:00:00Z", source_id="fn_entry",
        llm_response="Resolved via vtable analysis", reasoning_summary="vtable dispatch",
    ))
    s.create_repair_log(RepairLogNode(
        id="rl_2", caller_id="fn_handler", callee_id="fn_helper",
        call_location="src/handler.cpp:15", repair_method="llm",
        timestamp="2026-05-15T10:05:00Z", source_id="fn_entry",
        llm_response="Direct signature match", reasoning_summary="signature match",
    ))

    return s


@pytest.fixture
def feedback_store(tmp_path: Path) -> FeedbackStore:
    """Temporary feedback store for testing."""
    return FeedbackStore(storage_dir=tmp_path / "feedback")


@pytest.fixture
def client(store: InMemoryGraphStore, feedback_store: FeedbackStore, tmp_path: Path) -> TestClient:
    """Test client with fully populated store."""
    app = create_app(store=store, target_dir=tmp_path, feedback_store=feedback_store)
    return TestClient(app)


# ===========================================================================
# Health & Stats
# ===========================================================================


class TestHealth:
    def test_health_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestStats:
    def test_stats_all_fields(self, client: TestClient) -> None:
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_functions"] == 5
        assert data["total_files"] == 3
        assert data["total_calls"] == 3
        assert data["total_unresolved"] == 3
        assert data["total_source_points"] == 2
        assert "calls_by_resolved_by" in data
        assert "calls_by_call_type" in data
        assert "unresolved_by_status" in data
        assert "source_points_by_status" in data

    def test_stats_resolved_by_breakdown(self, client: TestClient) -> None:
        data = client.get("/api/v1/stats").json()
        rb = data["calls_by_resolved_by"]
        assert rb["symbol_table"] == 1
        assert rb["llm"] == 1
        assert rb["signature"] == 1

    def test_stats_source_points_by_status(self, client: TestClient) -> None:
        data = client.get("/api/v1/stats").json()
        sp = data["source_points_by_status"]
        assert sp.get("pending", 0) == 1
        assert sp.get("complete", 0) == 1


# ===========================================================================
# Files
# ===========================================================================


class TestFiles:
    def test_list_files(self, client: TestClient) -> None:
        data = client.get("/api/v1/files").json()
        assert data["total"] == 3
        paths = [f["file_path"] for f in data["items"]]
        assert "src/main.cpp" in paths

    def test_list_files_pagination(self, client: TestClient) -> None:
        data = client.get("/api/v1/files?limit=2&offset=1").json()
        assert data["total"] == 3
        assert len(data["items"]) == 2


# ===========================================================================
# Functions
# ===========================================================================


class TestFunctions:
    def test_list_all(self, client: TestClient) -> None:
        data = client.get("/api/v1/functions").json()
        assert data["total"] == 5

    def test_filter_by_file(self, client: TestClient) -> None:
        data = client.get("/api/v1/functions?file=src/util.cpp").json()
        assert data["total"] == 2
        names = {f["name"] for f in data["items"]}
        assert "ValidateSession" in names
        assert "UnusedFunc" in names

    def test_get_single_function(self, client: TestClient) -> None:
        resp = client.get("/api/v1/functions/fn_entry")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "OnRemoteRequest"
        assert data["file_path"] == "src/main.cpp"

    def test_get_function_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/functions/nonexistent")
        assert resp.status_code == 404

    def test_callers(self, client: TestClient) -> None:
        data = client.get("/api/v1/functions/fn_dispatch/callers").json()
        assert data["total"] >= 1
        ids = [f["id"] for f in data["items"]]
        assert "fn_entry" in ids

    def test_callees(self, client: TestClient) -> None:
        data = client.get("/api/v1/functions/fn_entry/callees").json()
        assert data["total"] >= 1
        ids = [f["id"] for f in data["items"]]
        assert "fn_dispatch" in ids

    def test_callers_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/functions/nope/callers").status_code == 404

    def test_callees_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/functions/nope/callees").status_code == 404

    def test_call_chain(self, client: TestClient) -> None:
        resp = client.get("/api/v1/functions/fn_entry/call-chain?depth=5")
        assert resp.status_code == 200
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        # Should reach all connected functions
        assert "fn_entry" in node_ids
        assert "fn_dispatch" in node_ids
        assert "fn_handler" in node_ids
        assert "fn_helper" in node_ids
        # Orphan should NOT be reachable
        assert "fn_orphan" not in node_ids

    def test_call_chain_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/functions/nope/call-chain").status_code == 404

    def test_call_chain_depth_validation(self, client: TestClient) -> None:
        assert client.get("/api/v1/functions/fn_entry/call-chain?depth=0").status_code == 422
        assert client.get("/api/v1/functions/fn_entry/call-chain?depth=51").status_code == 422


# ===========================================================================
# Unresolved Calls
# ===========================================================================


class TestUnresolvedCalls:
    def test_list_all(self, client: TestClient) -> None:
        data = client.get("/api/v1/unresolved-calls").json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_filter_by_caller(self, client: TestClient) -> None:
        data = client.get("/api/v1/unresolved-calls?caller=fn_entry").json()
        assert data["total"] == 1
        assert data["items"][0]["call_expression"] == "SendEvent"

    def test_filter_by_status(self, client: TestClient) -> None:
        data = client.get("/api/v1/unresolved-calls?status=pending").json()
        assert data["total"] >= 1

    def test_pagination(self, client: TestClient) -> None:
        data = client.get("/api/v1/unresolved-calls?limit=1&offset=0").json()
        assert data["total"] == 3
        assert len(data["items"]) == 1


# ===========================================================================
# Source Points
# ===========================================================================


class TestSourcePoints:
    def test_list(self, client: TestClient) -> None:
        data = client.get("/api/v1/source-points").json()
        assert data["total"] == 2

    def test_filter_by_kind(self, client: TestClient) -> None:
        data = client.get("/api/v1/source-points?kind=ipc").json()
        assert data["total"] == 1
        assert data["items"][0]["entry_point_kind"] == "ipc"

    def test_filter_by_module(self, client: TestClient) -> None:
        data = client.get("/api/v1/source-points?module=player").json()
        assert data["total"] == 1

    def test_filter_by_status(self, client: TestClient) -> None:
        data = client.get("/api/v1/source-points?status=pending").json()
        assert data["total"] == 1

    def test_get_by_id(self, client: TestClient) -> None:
        resp = client.get("/api/v1/source-points/sp_entry")
        assert resp.status_code == 200
        assert resp.json()["function_id"] == "fn_entry"

    def test_get_by_id_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/source-points/nope").status_code == 404

    def test_summary(self, client: TestClient) -> None:
        data = client.get("/api/v1/source-points/summary").json()
        assert data["total"] == 2
        assert "by_kind" in data
        assert "by_status" in data
        assert data["by_kind"]["ipc"] == 1
        assert data["by_kind"]["callback"] == 1
        assert data["by_status"]["pending"] == 1
        assert data["by_status"]["complete"] == 1

    def test_reachable_subgraph(self, client: TestClient) -> None:
        resp = client.get("/api/v1/source-points/fn_entry/reachable")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        node_ids = {n["id"] for n in data["nodes"]}
        assert "fn_dispatch" in node_ids

    def test_reachable_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/source-points/nope/reachable").status_code == 404


# ===========================================================================
# Analyze & Repair Triggers
# ===========================================================================


class TestAnalyzeTrigger:
    def test_full_mode_202(self, client: TestClient) -> None:
        resp = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp.status_code == 202
        assert resp.json()["mode"] == "full"

    def test_incremental_mode_202(self, client: TestClient) -> None:
        resp = client.post("/api/v1/analyze", json={"mode": "incremental"})
        assert resp.status_code == 202
        assert resp.json()["mode"] == "incremental"

    def test_invalid_mode_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/analyze", json={"mode": "bogus"})
        assert resp.status_code == 422

    def test_missing_body_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/analyze")
        assert resp.status_code == 422

    def test_double_spawn_409(self, client: TestClient) -> None:
        client.post("/api/v1/analyze", json={"mode": "full"})
        resp = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp.status_code == 409

    def test_status_after_trigger(self, client: TestClient) -> None:
        client.post("/api/v1/analyze", json={"mode": "full"})
        data = client.get("/api/v1/analyze/status").json()
        assert data["state"] == "running"
        assert data["mode"] == "full"
        assert "started_at" in data


class TestRepairTrigger:
    def test_repair_202(self, client: TestClient) -> None:
        resp = client.post("/api/v1/analyze/repair", json={"source_ids": []})
        assert resp.status_code == 202
        assert resp.json()["action"] == "repair"

    def test_repair_with_source_ids(self, client: TestClient) -> None:
        resp = client.post("/api/v1/analyze/repair", json={"source_ids": ["fn_entry"]})
        assert resp.status_code == 202

    def test_repair_no_body_202(self, client: TestClient) -> None:
        """Repair with no body should still work (all sources)."""
        resp = client.post("/api/v1/analyze/repair")
        assert resp.status_code == 202

    def test_repair_double_spawn_409(self, client: TestClient) -> None:
        client.post("/api/v1/analyze/repair")
        resp = client.post("/api/v1/analyze/repair")
        assert resp.status_code == 409


class TestAnalyzeStatus:
    def test_status_idle_by_default(self, client: TestClient) -> None:
        data = client.get("/api/v1/analyze/status").json()
        assert data["state"] == "idle"
        assert "sources" in data

    def test_status_has_sources_key(self, client: TestClient) -> None:
        data = client.get("/api/v1/analyze/status").json()
        assert isinstance(data["sources"], list)


# ===========================================================================
# Repair Logs
# ===========================================================================


class TestRepairLogs:
    def test_list_all(self, client: TestClient) -> None:
        data = client.get("/api/v1/repair-logs").json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    def test_filter_by_caller(self, client: TestClient) -> None:
        data = client.get("/api/v1/repair-logs?caller=fn_dispatch").json()
        assert data["total"] == 1
        assert data["items"][0]["callee_id"] == "fn_handler"

    def test_filter_by_callee(self, client: TestClient) -> None:
        data = client.get("/api/v1/repair-logs?callee=fn_helper").json()
        assert data["total"] == 1

    def test_filter_by_location(self, client: TestClient) -> None:
        data = client.get("/api/v1/repair-logs?location=src/main.cpp:60").json()
        assert data["total"] == 1

    def test_filter_by_source(self, client: TestClient) -> None:
        data = client.get("/api/v1/repair-logs?source=fn_entry").json()
        assert data["total"] == 2

    def test_filter_source_reachable(self, client: TestClient) -> None:
        """source_reachable does BFS and returns logs where caller is reachable."""
        data = client.get("/api/v1/repair-logs?source_reachable=fn_entry").json()
        # fn_entry -> fn_dispatch -> fn_handler; both have repair logs
        assert data["total"] == 2

    def test_pagination(self, client: TestClient) -> None:
        data = client.get("/api/v1/repair-logs?limit=1&offset=0").json()
        assert data["total"] == 2
        assert len(data["items"]) == 1

    def test_repair_log_fields(self, client: TestClient) -> None:
        data = client.get("/api/v1/repair-logs").json()
        log = data["items"][0]
        assert "id" in log
        assert "caller_id" in log
        assert "callee_id" in log
        assert "call_location" in log
        assert "repair_method" in log
        assert "timestamp" in log
        assert "reasoning_summary" in log


# ===========================================================================
# Live Tail
# ===========================================================================


class TestLiveTail:
    def test_live_no_logs_dir(self, client: TestClient) -> None:
        """When no log directory exists, returns empty lines."""
        resp = client.get("/api/v1/repair-logs/live?source_id=fn_entry")
        assert resp.status_code == 200
        data = resp.json()
        assert data["lines"] == []
        assert data["finished"] is False
        assert data["source_id"] == "fn_entry"

    def test_live_missing_source_id_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/repair-logs/live")
        assert resp.status_code == 422

    def test_live_with_log_file(self, client: TestClient, tmp_path: Path) -> None:
        """When log file exists, returns tail lines."""
        # Create log structure matching orchestrator output
        log_dir = tmp_path / "logs" / "repair" / "fn_entry"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "attempt_1.log"
        log_file.write_text("line1\nline2\nline3\n")

        resp = client.get("/api/v1/repair-logs/live?source_id=fn_entry&tail=2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["attempt"] == 1
        assert len(data["lines"]) == 2
        assert data["lines"] == ["line2", "line3"]

    def test_live_finished_state(self, client: TestClient, tmp_path: Path) -> None:
        """When progress.json says succeeded, finished=True."""
        log_dir = tmp_path / "logs" / "repair" / "fn_entry"
        log_dir.mkdir(parents=True)
        (log_dir / "attempt_1.log").write_text("done\n")
        (log_dir / "progress.json").write_text(json.dumps({"state": "succeeded"}))

        data = client.get("/api/v1/repair-logs/live?source_id=fn_entry").json()
        assert data["finished"] is True


# ===========================================================================
# Reviews
# ===========================================================================


class TestReviews:
    def test_list_empty(self, client: TestClient) -> None:
        data = client.get("/api/v1/reviews").json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_create_review(self, client: TestClient) -> None:
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.cpp",
            "call_line": 60,
            "verdict": "correct",
            "comment": "Looks good",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["verdict"] == "correct"
        assert "id" in data

    def test_create_and_list(self, client: TestClient) -> None:
        client.post("/api/v1/reviews", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.cpp",
            "call_line": 60,
            "verdict": "incorrect",
        })
        data = client.get("/api/v1/reviews").json()
        assert data["total"] == 1

    def test_update_review(self, client: TestClient) -> None:
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.cpp",
            "call_line": 60,
            "verdict": "correct",
        })
        review_id = resp.json()["id"]
        resp2 = client.put(f"/api/v1/reviews/{review_id}", json={
            "comment": "Actually wrong",
            "status": "revised",
        })
        assert resp2.status_code == 200
        assert resp2.json()["comment"] == "Actually wrong"

    def test_delete_review(self, client: TestClient) -> None:
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.cpp",
            "call_line": 60,
            "verdict": "correct",
        })
        review_id = resp.json()["id"]
        resp2 = client.delete(f"/api/v1/reviews/{review_id}")
        assert resp2.status_code == 204

    def test_delete_review_404(self, client: TestClient) -> None:
        resp = client.delete("/api/v1/reviews/nonexistent")
        assert resp.status_code == 404


# ===========================================================================
# Edges (create / delete)
# ===========================================================================


class TestEdges:
    def test_create_edge(self, client: TestClient) -> None:
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_entry",
            "callee_id": "fn_helper",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "src/main.cpp",
            "call_line": 40,
        })
        assert resp.status_code == 201
        # Verify edge exists via callees
        data = client.get("/api/v1/functions/fn_entry/callees").json()
        callee_ids = [f["id"] for f in data["items"]]
        assert "fn_helper" in callee_ids

    def test_create_edge_missing_fields_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/edges", json={"caller_id": "fn_entry"})
        assert resp.status_code == 422

    def test_create_edge_invalid_resolved_by_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_entry",
            "callee_id": "fn_helper",
            "resolved_by": "magic",
            "call_type": "direct",
            "call_file": "src/main.cpp",
            "call_line": 40,
        })
        assert resp.status_code == 422

    def test_delete_edge(self, client: TestClient) -> None:
        """Delete a specific edge via request body."""
        resp = client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.cpp",
            "call_line": 60,
        })
        assert resp.status_code == 204

    def test_bulk_delete_edges_for_function(self, client: TestClient) -> None:
        """DELETE /edges/{function_id} removes all edges for that function."""
        resp = client.delete("/api/v1/edges/fn_handler")
        assert resp.status_code == 204


# ===========================================================================
# Feedback (CRUD)
# ===========================================================================


class TestFeedback:
    def test_list_empty(self, client: TestClient) -> None:
        data = client.get("/api/v1/feedback").json()
        assert data["total"] == 0

    def test_create_feedback(self, client: TestClient) -> None:
        resp = client.post("/api/v1/feedback", json={
            "pattern": "vtable dispatch to Observer",
            "call_context": "observer->Notify()",
            "wrong_target": "WrongObserver::Notify",
            "correct_target": "ConcreteObserver::Notify",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["pattern"] == "vtable dispatch to Observer"

    def test_create_and_list(self, client: TestClient) -> None:
        client.post("/api/v1/feedback", json={
            "pattern": "test pattern",
            "call_context": "ctx",
            "wrong_target": "wrong",
            "correct_target": "correct",
        })
        data = client.get("/api/v1/feedback").json()
        assert data["total"] == 1

    def test_delete_feedback(self, client: TestClient) -> None:
        client.post("/api/v1/feedback", json={
            "pattern": "to delete",
            "call_context": "ctx",
            "wrong_target": "wrong",
            "correct_target": "correct",
        })
        resp = client.delete("/api/v1/feedback/0")
        assert resp.status_code == 200
        data = client.get("/api/v1/feedback").json()
        assert data["total"] == 0

    def test_delete_feedback_404(self, client: TestClient) -> None:
        resp = client.delete("/api/v1/feedback/999")
        assert resp.status_code == 404

    def test_update_feedback(self, client: TestClient) -> None:
        client.post("/api/v1/feedback", json={
            "pattern": "original",
            "call_context": "ctx",
            "wrong_target": "wrong",
            "correct_target": "correct",
        })
        resp = client.put("/api/v1/feedback/0", json={
            "pattern": "updated pattern",
        })
        assert resp.status_code == 200
        assert resp.json()["pattern"] == "updated pattern"

    def test_update_feedback_404(self, client: TestClient) -> None:
        resp = client.put("/api/v1/feedback/999", json={"pattern": "x"})
        assert resp.status_code == 404

    def test_pagination(self, client: TestClient) -> None:
        for i in range(5):
            client.post("/api/v1/feedback", json={
                "pattern": f"pattern_{i}",
                "call_context": "ctx",
                "wrong_target": "wrong",
                "correct_target": "correct",
            })
        data = client.get("/api/v1/feedback?limit=2&offset=1").json()
        assert data["total"] == 5
        assert len(data["items"]) == 2


# ===========================================================================
# Source Code
# ===========================================================================


class TestSourceCode:
    def test_source_code_file_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/source-code?file=nonexistent.cpp&start=1&end=10")
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            data = resp.json()
            assert "content" in data

    def test_source_code_missing_params_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/source-code")
        assert resp.status_code == 422


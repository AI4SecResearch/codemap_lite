"""Tests for the FastAPI REST API layer."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from codemap_lite.analysis.feedback_store import CounterExample, FeedbackStore
from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    RepairLogNode,
    UnresolvedCallNode,
)


def get_test_client() -> tuple[TestClient, InMemoryGraphStore]:
    store = InMemoryGraphStore()
    app = create_app(store=store)
    return TestClient(app), store


class TestHealthCheck:
    def test_health_check(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


class TestFilesEndpoint:
    def test_get_files_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/files")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_files_with_data(self) -> None:
        client, store = get_test_client()
        f = FileNode(file_path="src/main.py", hash="abc123", primary_language="python")
        store.create_file(f)
        resp = client.get("/api/v1/files")
        assert resp.status_code == 200
        files = resp.json()
        assert len(files) == 1
        assert files[0]["file_path"] == "src/main.py"


class TestFunctionsEndpoint:
    def test_get_functions_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/functions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_functions_filtered_by_file(self) -> None:
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="def foo()",
            name="foo",
            file_path="src/a.py",
            start_line=1,
            end_line=3,
            body_hash="h1",
        )
        fn2 = FunctionNode(
            signature="def bar()",
            name="bar",
            file_path="src/b.py",
            start_line=1,
            end_line=5,
            body_hash="h2",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        resp = client.get("/api/v1/functions", params={"file": "src/a.py"})
        assert resp.status_code == 200
        funcs = resp.json()
        assert len(funcs) == 1
        assert funcs[0]["name"] == "foo"

    def test_create_function_then_get(self) -> None:
        client, store = get_test_client()
        fn = FunctionNode(
            signature="def hello()",
            name="hello",
            file_path="src/main.py",
            start_line=10,
            end_line=15,
            body_hash="xyz",
            id="func-001",
        )
        store.create_function(fn)
        resp = client.get("/api/v1/functions/func-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "hello"
        assert data["id"] == "func-001"

    def test_get_function_not_found(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/functions/nonexistent")
        assert resp.status_code == 404


class TestCallersCalleesEndpoint:
    def _setup_graph(self, store: InMemoryGraphStore) -> None:
        self.fn_a = FunctionNode(
            signature="def a()", name="a", file_path="f.py",
            start_line=1, end_line=3, body_hash="ha", id="a",
        )
        self.fn_b = FunctionNode(
            signature="def b()", name="b", file_path="f.py",
            start_line=5, end_line=8, body_hash="hb", id="b",
        )
        self.fn_c = FunctionNode(
            signature="def c()", name="c", file_path="f.py",
            start_line=10, end_line=12, body_hash="hc", id="c",
        )
        store.create_function(self.fn_a)
        store.create_function(self.fn_b)
        store.create_function(self.fn_c)
        # a -> b -> c
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="static", call_type="direct", call_file="f.py", call_line=2,
        ))
        store.create_calls_edge("b", "c", CallsEdgeProps(
            resolved_by="static", call_type="direct", call_file="f.py", call_line=6,
        ))

    def test_get_callers(self) -> None:
        client, store = get_test_client()
        self._setup_graph(store)
        resp = client.get("/api/v1/functions/b/callers")
        assert resp.status_code == 200
        callers = resp.json()
        assert len(callers) == 1
        assert callers[0]["id"] == "a"

    def test_get_callees(self) -> None:
        client, store = get_test_client()
        self._setup_graph(store)
        resp = client.get("/api/v1/functions/b/callees")
        assert resp.status_code == 200
        callees = resp.json()
        assert len(callees) == 1
        assert callees[0]["id"] == "c"

    def test_get_call_chain(self) -> None:
        client, store = get_test_client()
        self._setup_graph(store)
        resp = client.get("/api/v1/functions/a/call-chain", params={"depth": 5})
        assert resp.status_code == 200
        data = resp.json()
        node_ids = [n["id"] for n in data["nodes"]]
        assert "a" in node_ids
        assert "b" in node_ids
        assert "c" in node_ids

    def test_get_call_chain_depth_limited(self) -> None:
        client, store = get_test_client()
        self._setup_graph(store)
        resp = client.get("/api/v1/functions/a/call-chain", params={"depth": 1})
        assert resp.status_code == 200
        data = resp.json()
        node_ids = [n["id"] for n in data["nodes"]]
        assert "a" in node_ids
        assert "b" in node_ids
        # c should NOT be reachable at depth=1
        assert "c" not in node_ids

    def test_get_callers_nonexistent_function_returns_404(self) -> None:
        """architecture.md §8: callers endpoint must return 404 for
        non-existent function, not 200 with empty list."""
        client, _ = get_test_client()
        resp = client.get("/api/v1/functions/nonexistent/callers")
        assert resp.status_code == 404

    def test_get_callees_nonexistent_function_returns_404(self) -> None:
        """architecture.md §8: callees endpoint must return 404 for
        non-existent function."""
        client, _ = get_test_client()
        resp = client.get("/api/v1/functions/nonexistent/callees")
        assert resp.status_code == 404

    def test_get_call_chain_nonexistent_function_returns_404(self) -> None:
        """architecture.md §8: call-chain endpoint must return 404 for
        non-existent function."""
        client, _ = get_test_client()
        resp = client.get("/api/v1/functions/nonexistent/call-chain")
        assert resp.status_code == 404


class TestAnalyzeEndpoint:
    def test_analyze_trigger(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"

    def test_analyze_trigger_incremental(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze", json={"mode": "incremental"})
        assert resp.status_code == 202

    def test_analyze_trigger_conflict_returns_409(self) -> None:
        """architecture.md §8: double-spawn of analysis must return 409."""
        client, _ = get_test_client()
        # First POST sets state to "analyzing"
        resp1 = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp1.status_code == 202
        # Second POST should detect conflict via real state transition
        resp2 = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp2.status_code == 409
        assert "already running" in resp2.json()["detail"]

    def test_analyze_trigger_spawns_background_task(self) -> None:
        """architecture.md §8: POST /analyze with settings triggers pipeline."""
        from unittest.mock import patch, MagicMock
        from codemap_lite.config.settings import Settings

        store = InMemoryGraphStore()
        settings = Settings()
        app = create_app(store=store, settings=settings)
        client = TestClient(app)

        with patch(
            "codemap_lite.api.routes.analyze._run_analysis_background"
        ) as mock_run:
            resp = client.post("/api/v1/analyze", json={"mode": "full"})
            assert resp.status_code == 202
            mock_run.assert_called_once()

    def test_analyze_trigger_invalid_mode(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze", json={"mode": "invalid"})
        assert resp.status_code == 422

    def test_analyze_trigger_missing_mode(self) -> None:
        """architecture.md §8: POST /analyze requires 'mode' field."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze", json={})
        assert resp.status_code == 422

    def test_analyze_repair(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze/repair")
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"

    def test_analyze_repair_conflict_returns_409(self) -> None:
        """architecture.md §8: double-spawn must return 409 Conflict."""
        client, store = get_test_client()
        # First call sets state to "repairing" (no settings → demo mode)
        resp1 = client.post("/api/v1/analyze/repair")
        assert resp1.status_code == 202
        # Second call should detect conflict via real state transition
        resp2 = client.post("/api/v1/analyze/repair")
        assert resp2.status_code == 409
        assert "already running" in resp2.json()["detail"]

    def test_analyze_status(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data
        assert "progress" in data
        # sources[] is always present (empty when no target_dir / no
        # progress files yet) — architecture.md §3, ADR #52.
        assert data["sources"] == []

    def test_analyze_status_includes_timestamps_after_repair(self) -> None:
        """architecture.md §8: status should expose started_at/completed_at."""
        from codemap_lite.config.settings import Settings
        client, _ = get_test_client()
        client.app.state.settings = Settings()
        # Simulate a completed repair session
        client.app.state.analyze_state = {
            "state": "idle",
            "progress": 0.0,
            "started_at": "2026-05-13T10:00:00+00:00",
            "completed_at": "2026-05-13T10:05:00+00:00",
        }
        resp = client.get("/api/v1/analyze/status")
        data = resp.json()
        assert data["started_at"] == "2026-05-13T10:00:00+00:00"
        assert data["completed_at"] == "2026-05-13T10:05:00+00:00"

    def test_analyze_status_aggregates_progress_files(self, tmp_path) -> None:
        store = InMemoryGraphStore()
        app = create_app(store=store, target_dir=tmp_path)
        client = TestClient(app)

        repair_root = tmp_path / "logs" / "repair"
        (repair_root / "src_001").mkdir(parents=True)
        (repair_root / "src_001" / "progress.json").write_text(
            json.dumps({"gaps_fixed": 2, "gaps_total": 5, "current_gap": "gap_003"}),
            encoding="utf-8",
        )
        (repair_root / "src_002").mkdir(parents=True)
        (repair_root / "src_002" / "progress.json").write_text(
            json.dumps({"gaps_fixed": 3, "gaps_total": 3, "current_gap": None}),
            encoding="utf-8",
        )

        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        data = resp.json()
        sources = {s["source_id"]: s for s in data["sources"]}
        assert set(sources.keys()) == {"src_001", "src_002"}
        assert sources["src_001"]["gaps_fixed"] == 2
        assert sources["src_001"]["gaps_total"] == 5
        assert sources["src_001"]["current_gap"] == "gap_003"
        assert sources["src_002"]["gaps_fixed"] == 3
        assert sources["src_002"]["current_gap"] is None
        # Overall progress is (2+3) / (5+3) = 0.625
        assert data["progress"] == pytest.approx(0.625)

    def test_analyze_status_ignores_unreadable_progress(self, tmp_path) -> None:
        store = InMemoryGraphStore()
        app = create_app(store=store, target_dir=tmp_path)
        client = TestClient(app)

        repair_root = tmp_path / "logs" / "repair"
        (repair_root / "src_bad").mkdir(parents=True)
        (repair_root / "src_bad" / "progress.json").write_text(
            "not json {{", encoding="utf-8"
        )
        (repair_root / "src_ok").mkdir(parents=True)
        (repair_root / "src_ok" / "progress.json").write_text(
            json.dumps({"gaps_fixed": 1, "gaps_total": 2, "current_gap": "g"}),
            encoding="utf-8",
        )

        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        sources = {s["source_id"]: s for s in resp.json()["sources"]}
        assert "src_bad" not in sources

    def test_analyze_status_skips_malformed_numeric_fields(self, tmp_path) -> None:
        """architecture.md §3: progress.json with non-numeric gaps_fixed/
        gaps_total must be skipped gracefully, not crash the endpoint."""
        store = InMemoryGraphStore()
        app = create_app(store=store, target_dir=tmp_path)
        client = TestClient(app)

        repair_root = tmp_path / "logs" / "repair"
        (repair_root / "src_bad").mkdir(parents=True)
        (repair_root / "src_bad" / "progress.json").write_text(
            json.dumps({"gaps_fixed": "not_a_number", "gaps_total": 5}),
            encoding="utf-8",
        )
        (repair_root / "src_ok").mkdir(parents=True)
        (repair_root / "src_ok" / "progress.json").write_text(
            json.dumps({"gaps_fixed": 1, "gaps_total": 2}),
            encoding="utf-8",
        )

        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        sources = {s["source_id"]: s for s in resp.json()["sources"]}
        # Malformed entry skipped, valid entry preserved
        assert "src_bad" not in sources
        assert "src_ok" in sources
        assert sources["src_ok"]["gaps_fixed"] == 1
        assert sources["src_ok"]["gaps_total"] == 2


class TestSourcePointsEndpoint:
    def test_get_source_points_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/source-points")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_source_point_reachable(self) -> None:
        client, store = get_test_client()
        fn = FunctionNode(
            signature="def entry()", name="entry", file_path="main.py",
            start_line=1, end_line=5, body_hash="h1", id="entry-1",
        )
        store.create_function(fn)
        resp = client.get("/api/v1/source-points/entry-1/reachable")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data

    def test_get_source_point_reachable_full_schema(self) -> None:
        """architecture.md §8: GET /source-points/{id}/reachable must return
        {nodes: [...], edges: [...], unresolved: [...]} with proper field shapes."""
        client, store = get_test_client()
        caller = FunctionNode(
            signature="void caller()", name="caller", file_path="a.cpp",
            start_line=1, end_line=10, body_hash="hc", id="caller-1",
        )
        callee = FunctionNode(
            signature="void callee()", name="callee", file_path="b.cpp",
            start_line=1, end_line=5, body_hash="hd", id="callee-1",
        )
        store.create_function(caller)
        store.create_function(callee)
        store.create_calls_edge(
            "caller-1", "callee-1",
            CallsEdgeProps(
                resolved_by="symbol_table", call_type="direct",
                call_file="a.cpp", call_line=5,
            ),
        )
        gap = UnresolvedCallNode(
            caller_id="caller-1", call_expression="fp()",
            call_file="a.cpp", call_line=8, call_type="indirect",
            source_code_snippet="fp();", var_name="fp", var_type="void(*)()",
        )
        store.create_unresolved_call(gap)

        resp = client.get("/api/v1/source-points/caller-1/reachable")
        assert resp.status_code == 200
        data = resp.json()

        # Must have all three top-level keys
        assert set(data.keys()) >= {"nodes", "edges", "unresolved"}

        # Nodes must have required fields
        assert len(data["nodes"]) >= 1
        node = data["nodes"][0]
        for field in ("id", "name", "signature", "file_path"):
            assert field in node, f"node missing field: {field}"

        # Edges must have caller/callee/props
        assert len(data["edges"]) >= 1
        edge = data["edges"][0]
        assert "caller_id" in edge or "source" in edge  # accept either naming

        # Unresolved must have caller_id and call_expression
        assert len(data["unresolved"]) >= 1
        gap_data = data["unresolved"][0]
        assert "caller_id" in gap_data
        assert "call_expression" in gap_data


class TestReviewEndpoint:
    def _setup_edge(self, store: InMemoryGraphStore) -> None:
        fn1 = FunctionNode(
            signature="void f()", name="f", file_path="a.c",
            start_line=1, end_line=5, body_hash="h1", id="func-1",
        )
        fn2 = FunctionNode(
            signature="void g()", name="g", file_path="a.c",
            start_line=10, end_line=15, body_hash="h2", id="func-2",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge(
            "func-1", "func-2",
            CallsEdgeProps(
                resolved_by="llm", call_type="indirect",
                call_file="a.c", call_line=3,
            ),
        )

    def test_post_review(self) -> None:
        client, store = get_test_client()
        self._setup_edge(store)
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "func-1",
            "callee_id": "func-2",
            "call_file": "a.c",
            "call_line": 3,
            "verdict": "correct",
            "comment": "Looks correct",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["caller_id"] == "func-1"
        assert data["verdict"] == "correct"
        assert "id" in data

    def test_get_reviews(self) -> None:
        client, store = get_test_client()
        self._setup_edge(store)
        client.post("/api/v1/reviews", json={
            "caller_id": "func-1",
            "callee_id": "func-2",
            "call_file": "a.c",
            "call_line": 3,
            "verdict": "correct",
        })
        resp = client.get("/api/v1/reviews")
        assert resp.status_code == 200
        reviews = resp.json()
        assert len(reviews) == 1

    def test_update_review(self) -> None:
        client, store = get_test_client()
        self._setup_edge(store)
        create_resp = client.post("/api/v1/reviews", json={
            "caller_id": "func-1",
            "callee_id": "func-2",
            "call_file": "a.c",
            "call_line": 3,
            "verdict": "correct",
            "comment": "Initial",
        })
        review_id = create_resp.json()["id"]
        resp = client.put(f"/api/v1/reviews/{review_id}", json={
            "comment": "Updated",
            "status": "approved",
        })
        assert resp.status_code == 200
        assert resp.json()["comment"] == "Updated"

    def test_delete_review(self) -> None:
        client, store = get_test_client()
        self._setup_edge(store)
        create_resp = client.post("/api/v1/reviews", json={
            "caller_id": "func-1",
            "callee_id": "func-2",
            "call_file": "a.c",
            "call_line": 3,
            "verdict": "correct",
            "comment": "To delete",
        })
        review_id = create_resp.json()["id"]
        resp = client.delete(f"/api/v1/reviews/{review_id}")
        assert resp.status_code == 204

    def test_delete_review_not_found_returns_404(self) -> None:
        """architecture.md §8: DELETE /reviews/{id} must return 404 for
        non-existent review."""
        client, _ = get_test_client()
        resp = client.delete("/api/v1/reviews/nonexistent-id-999")
        assert resp.status_code == 404

    def test_post_edge(self) -> None:
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="def x()", name="x", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="x",
        )
        fn2 = FunctionNode(
            signature="def y()", name="y", file_path="f.py",
            start_line=5, end_line=8, body_hash="h2", id="y",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        resp = client.post("/api/v1/edges", json={
            "caller_id": "x",
            "callee_id": "y",
            "resolved_by": "manual",
            "call_type": "direct",
            "call_file": "f.py",
            "call_line": 2,
        })
        assert resp.status_code == 201

    def test_post_edge_nonexistent_caller_returns_404(self) -> None:
        """architecture.md §8: edges must reference valid Function nodes."""
        client, store = get_test_client()
        fn2 = FunctionNode(
            signature="def y()", name="y", file_path="f.py",
            start_line=5, end_line=8, body_hash="h2", id="y",
        )
        store.create_function(fn2)
        resp = client.post("/api/v1/edges", json={
            "caller_id": "nonexistent",
            "callee_id": "y",
            "resolved_by": "manual",
            "call_type": "direct",
            "call_file": "f.py",
            "call_line": 2,
        })
        assert resp.status_code == 404

    def test_post_edge_nonexistent_callee_returns_404(self) -> None:
        """architecture.md §8: edges must reference valid Function nodes."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="def x()", name="x", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="x",
        )
        store.create_function(fn1)
        resp = client.post("/api/v1/edges", json={
            "caller_id": "x",
            "callee_id": "nonexistent",
            "resolved_by": "manual",
            "call_type": "direct",
            "call_file": "f.py",
            "call_line": 2,
        })
        assert resp.status_code == 404
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="def x()", name="x", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="x",
        )
        store.create_function(fn1)
        resp = client.delete("/api/v1/edges/x")
        assert resp.status_code == 204

    def test_post_edge_invalid_resolved_by_returns_422(self) -> None:
        """architecture.md §8: resolved_by must be one of the allowed enum values."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/edges", json={
            "caller_id": "x",
            "callee_id": "y",
            "resolved_by": "magic",
            "call_type": "direct",
            "call_file": "f.py",
            "call_line": 2,
        })
        assert resp.status_code == 422

    def test_post_edge_invalid_call_type_returns_422(self) -> None:
        """architecture.md §8: call_type must be one of {direct, indirect, virtual}."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/edges", json={
            "caller_id": "x",
            "callee_id": "y",
            "resolved_by": "llm",
            "call_type": "unknown",
            "call_file": "f.py",
            "call_line": 2,
        })
        assert resp.status_code == 422


class TestEdgeCentricReview:
    """architecture.md §5 审阅交互 + §8 REST API:
    POST /api/v1/reviews must be edge-centric — accept caller_id, callee_id,
    call_file, call_line, and verdict (correct/incorrect). When verdict=correct,
    record approval. When verdict=incorrect, trigger the 4-step error flow."""

    def _setup_edge(self, store: InMemoryGraphStore) -> None:
        """Create two functions and an LLM-resolved edge between them."""
        fn1 = FunctionNode(
            signature="void dispatch()", name="dispatch", file_path="src/main.c",
            start_line=10, end_line=20, body_hash="h1", id="fn_dispatch",
        )
        fn2 = FunctionNode(
            signature="void handler()", name="handler", file_path="src/handler.c",
            start_line=1, end_line=5, body_hash="h2", id="fn_handler",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge(
            "fn_dispatch", "fn_handler",
            CallsEdgeProps(
                resolved_by="llm", call_type="indirect",
                call_file="src/main.c", call_line=15,
            ),
        )

    def test_review_mark_correct_records_approval(self) -> None:
        """architecture.md §5: 标记正确 → record approval on the edge."""
        client, store = get_test_client()
        self._setup_edge(store)

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.c",
            "call_line": 15,
            "verdict": "correct",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["verdict"] == "correct"
        assert data["caller_id"] == "fn_dispatch"
        assert data["callee_id"] == "fn_handler"
        assert "id" in data

    def test_review_mark_incorrect_triggers_error_flow(self) -> None:
        """architecture.md §5: 标记错误 → delete edge + RepairLog + regenerate UC."""
        client, store = get_test_client()
        self._setup_edge(store)

        # Also create a RepairLog for this edge
        from codemap_lite.graph.schema import RepairLogNode
        store.create_repair_log(RepairLogNode(
            caller_id="fn_dispatch",
            callee_id="fn_handler",
            call_location="src/main.c:15",
            repair_method="llm",
            llm_response="analysis",
            timestamp="2026-05-14T00:00:00Z",
            reasoning_summary="dispatch calls handler",
        ))

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.c",
            "call_line": 15,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["verdict"] == "incorrect"

        # Edge should be deleted
        edges = store.list_calls_edges()
        assert len(edges) == 0

        # RepairLog should be deleted
        logs = store.get_repair_logs(
            caller_id="fn_dispatch", callee_id="fn_handler"
        )
        assert len(logs) == 0

        # UnresolvedCall should be regenerated
        ucs = store.get_unresolved_calls(status="pending")
        assert len(ucs) == 1
        assert ucs[0].caller_id == "fn_dispatch"
        assert ucs[0].call_file == "src/main.c"
        assert ucs[0].call_line == 15

    def test_review_nonexistent_edge_returns_404(self) -> None:
        """Cannot review an edge that doesn't exist."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "no_such",
            "callee_id": "no_such",
            "call_file": "x.c",
            "call_line": 1,
            "verdict": "correct",
        })
        assert resp.status_code == 404

    def test_review_invalid_verdict_returns_422(self) -> None:
        """Verdict must be 'correct' or 'incorrect'."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.c",
            "call_line": 15,
            "verdict": "maybe",
        })
        assert resp.status_code == 422

    def test_review_incorrect_with_correct_target_creates_counter_example(self) -> None:
        """architecture.md §5: when verdict=incorrect AND correct_target is
        provided, the review endpoint must create a counter-example in the
        FeedbackStore. This is the primary mechanism for building the
        counter-example library from reviewer feedback."""
        from codemap_lite.analysis.feedback_store import FeedbackStore
        import tempfile

        store = InMemoryGraphStore()
        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path
            feedback_store = FeedbackStore(storage_dir=Path(tmpdir))
            app = create_app(store=store, feedback_store=feedback_store)
            client = TestClient(app)

            # Setup edge
            fn1 = FunctionNode(
                signature="void dispatch()", name="dispatch",
                file_path="src/main.c", start_line=10, end_line=20,
                body_hash="h1", id="fn_dispatch",
            )
            fn2 = FunctionNode(
                signature="void handler()", name="handler",
                file_path="src/handler.c", start_line=1, end_line=5,
                body_hash="h2", id="fn_handler",
            )
            store.create_function(fn1)
            store.create_function(fn2)
            store.create_calls_edge(
                "fn_dispatch", "fn_handler",
                CallsEdgeProps(
                    resolved_by="llm", call_type="indirect",
                    call_file="src/main.c", call_line=15,
                ),
            )

            # Mark incorrect WITH correct_target
            resp = client.post("/api/v1/reviews", json={
                "caller_id": "fn_dispatch",
                "callee_id": "fn_handler",
                "call_file": "src/main.c",
                "call_line": 15,
                "verdict": "incorrect",
                "correct_target": "fn_real_handler",
            })
            assert resp.status_code == 201

            # Counter-example should have been created
            examples = feedback_store.list_all()
            assert len(examples) == 1, (
                "architecture.md §5: correct_target provided → counter-example must be created"
            )
            assert examples[0].wrong_target == "fn_handler"
            assert examples[0].correct_target == "fn_real_handler"


class TestFeedbackEndpoint:
    def test_get_feedback_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/feedback")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_feedback_with_store(self, tmp_path) -> None:
        # Seed a FeedbackStore on disk, then wire it into create_app so
        # GET /api/v1/feedback surfaces the structured entries
        # (architecture.md §3 反馈机制 + §8).
        store_dir = tmp_path / ".codemap_lite" / "feedback"
        feedback_store = FeedbackStore(storage_dir=store_dir)
        feedback_store.add(
            CounterExample(
                call_context="dispatch_event(handler, evt)",
                wrong_target="logger.warn",
                correct_target="on_event",
                pattern="dispatch_event callbacks must match signature EventHandler",
            )
        )
        feedback_store.add(
            CounterExample(
                call_context="table[idx](ctx)",
                wrong_target="fallback_noop",
                correct_target="action_commit",
                pattern="vtable index resolution must honour ctx.role",
            )
        )

        graph_store = InMemoryGraphStore()
        app = create_app(store=graph_store, feedback_store=feedback_store)
        client = TestClient(app)

        resp = client.get("/api/v1/feedback")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        patterns = {item["pattern"] for item in data}
        assert "dispatch_event callbacks must match signature EventHandler" in patterns
        assert "vtable index resolution must honour ctx.role" in patterns
        first = next(
            item for item in data
            if item["pattern"] == "dispatch_event callbacks must match signature EventHandler"
        )
        assert first["call_context"] == "dispatch_event(handler, evt)"
        assert first["wrong_target"] == "logger.warn"
        assert first["correct_target"] == "on_event"

    def test_post_feedback_persists_to_store(self, tmp_path) -> None:
        """POST /api/v1/feedback routes the CounterExample into FeedbackStore.

        Closes the write half of the feedback loop (architecture.md §5
        审阅交互): after a human marks a repair wrong and fills the correct
        target, the generalized reason lands in the store and the next
        repair round picks it up via ``RepairOrchestrator``.
        """
        store_dir = tmp_path / ".codemap_lite" / "feedback"
        feedback_store = FeedbackStore(storage_dir=store_dir)

        app = create_app(store=InMemoryGraphStore(), feedback_store=feedback_store)
        client = TestClient(app)

        payload = {
            "call_context": "dispatcher->handle(req)",
            "wrong_target": "legacy_handler",
            "correct_target": "modern_handler",
            "pattern": "dispatcher vtable resolution must prefer modern_handler",
        }
        resp = client.post("/api/v1/feedback", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        # Response echoes the example plus the dedup signal fields
        # (architecture.md §3 反馈机制 steps 3-5).
        for key, value in payload.items():
            assert data[key] == value
        assert data["deduplicated"] is False
        assert data["total"] == 1

        # Round-trips through GET and through the underlying store
        stored = feedback_store.list_all()
        assert len(stored) == 1
        assert stored[0].pattern == payload["pattern"]

        listing = client.get("/api/v1/feedback").json()
        assert len(listing) == 1
        assert listing[0]["correct_target"] == "modern_handler"

    def test_post_feedback_dedupes_by_pattern(self, tmp_path) -> None:
        """Posting the same pattern twice does not duplicate entries.

        FeedbackStore.add() merges by pattern (architecture.md §3 反馈机制
        step 4 "相似 → 总结合并"); the HTTP layer inherits that contract
        and surfaces it via ``deduplicated: true`` on the second response
        so the reviewer knows their submission broadened an existing rule.
        """
        feedback_store = FeedbackStore(
            storage_dir=tmp_path / ".codemap_lite" / "feedback"
        )
        app = create_app(store=InMemoryGraphStore(), feedback_store=feedback_store)
        client = TestClient(app)

        payload = {
            "call_context": "cb(x)",
            "wrong_target": "wrong_cb",
            "correct_target": "right_cb",
            "pattern": "callback must be selected by x.role",
        }
        first = client.post("/api/v1/feedback", json=payload)
        assert first.status_code == 201
        assert first.json()["deduplicated"] is False
        assert first.json()["total"] == 1

        second = client.post("/api/v1/feedback", json=payload)
        assert second.status_code == 201
        assert second.json()["deduplicated"] is True
        assert second.json()["total"] == 1

        assert len(client.get("/api/v1/feedback").json()) == 1

    def test_post_feedback_requires_all_fields(self, tmp_path) -> None:
        """Missing a required field → 422 (Pydantic validation)."""
        feedback_store = FeedbackStore(
            storage_dir=tmp_path / ".codemap_lite" / "feedback"
        )
        app = create_app(store=InMemoryGraphStore(), feedback_store=feedback_store)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/feedback",
            json={"call_context": "foo()", "wrong_target": "a", "correct_target": "b"},
        )
        assert resp.status_code == 422

    def test_post_feedback_without_store_returns_503(self) -> None:
        """No store wired → 503 so the UI can surface a clear error."""
        client, _ = get_test_client()
        resp = client.post(
            "/api/v1/feedback",
            json={
                "call_context": "foo()",
                "wrong_target": "a",
                "correct_target": "b",
                "pattern": "p",
            },
        )
        assert resp.status_code == 503

    def test_get_stats(self) -> None:
        client, store = get_test_client()
        fn = FunctionNode(
            signature="def a()", name="a", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="a",
        )
        store.create_function(fn)
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_functions"] == 1
        assert "total_files" in data
        assert "total_calls" in data
        assert "total_unresolved" in data
        # New breakdown surfaces GAP lifecycle on the Dashboard without
        # drilling into ReviewQueue (architecture.md §3 UnresolvedCall 生命周期).
        assert "unresolved_by_status" in data
        assert data["unresolved_by_status"] == {"pending": 0, "unresolvable": 0}
        # Breakdown by CallsEdgeProps.resolved_by (architecture.md §4 +
        # §5 审阅对象：单条 CALLS 边，特别是 resolved_by='llm' 的).
        # All 5 keys must always be present (architecture.md §8).
        assert "calls_by_resolved_by" in data
        assert data["calls_by_resolved_by"] == {
            "symbol_table": 0, "signature": 0, "dataflow": 0, "context": 0, "llm": 0,
        }
        # Counter-example library size (architecture.md §3 反馈机制 + §8).
        # Without a wired FeedbackStore the field is present and 0 so
        # the left-nav chip can render deterministically (北极星 #5).
        assert "total_feedback" in data
        assert data["total_feedback"] == 0
        # RepairLog count (architecture.md §4 + §8). Surfaces total LLM
        # repair volume so the Dashboard can advertise cumulative repair
        # provenance without hitting /repair-logs.
        assert "total_repair_logs" in data
        assert data["total_repair_logs"] == 0

    def test_get_stats_total_feedback_with_store(self, tmp_path) -> None:
        """/stats reports `total_feedback` from the wired FeedbackStore so
        the left-nav Feedback label can show a live count chip without
        mounting FeedbackLog (architecture.md §3 反馈机制 + §8; 北极星 #5
        状态透明度 + 候选优化方向 #4 进度与可观测性)."""
        store_dir = tmp_path / ".codemap_lite" / "feedback"
        feedback_store = FeedbackStore(storage_dir=store_dir)
        feedback_store.add(
            CounterExample(
                call_context="dispatch(handler)",
                wrong_target="noop",
                correct_target="on_event",
                pattern="dispatch handler must match EventHandler",
            )
        )
        feedback_store.add(
            CounterExample(
                call_context="vtable[i](ctx)",
                wrong_target="fallback",
                correct_target="commit",
                pattern="vtable resolution honours ctx.role",
            )
        )
        graph_store = InMemoryGraphStore()
        app = create_app(store=graph_store, feedback_store=feedback_store)
        client = TestClient(app)
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        assert resp.json()["total_feedback"] == 2

    def test_get_stats_unresolved_by_status(self) -> None:
        """/stats buckets UnresolvedCall nodes by `status` so the Dashboard
        can distinguish retryable pending GAPs from agent-abandoned ones
        (architecture.md §3: retry_count ≥ 3 → status="unresolvable")."""
        client, store = get_test_client()
        fn = FunctionNode(
            signature="def a()", name="a", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="caller",
        )
        store.create_function(fn)
        store.create_unresolved_call(
            UnresolvedCallNode(
                caller_id="caller", call_expression="fp()", call_file="f.py",
                call_line=2, call_type="indirect", source_code_snippet="fp()",
                var_name=None, var_type=None, id="g1", status="pending",
                retry_count=1,
            )
        )
        store.create_unresolved_call(
            UnresolvedCallNode(
                caller_id="caller", call_expression="gp()", call_file="f.py",
                call_line=3, call_type="indirect", source_code_snippet="gp()",
                var_name=None, var_type=None, id="g2", status="unresolvable",
                retry_count=3,
            )
        )
        store.create_unresolved_call(
            UnresolvedCallNode(
                caller_id="caller", call_expression="hp()", call_file="f.py",
                call_line=4, call_type="indirect", source_code_snippet="hp()",
                var_name=None, var_type=None, id="g3", status="unresolvable",
                retry_count=3,
            )
        )
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_unresolved"] == 3
        assert data["unresolved_by_status"] == {"pending": 1, "unresolvable": 2}

    def test_get_stats_calls_by_resolved_by(self) -> None:
        """/stats buckets CALLS edges by `resolved_by` so the Dashboard
        can surface the llm-repaired edge backlog without drilling into
        ReviewQueue (architecture.md §4 CALLS 边属性 + §5 审阅对象：
        单条 CALLS 边，特别是 resolved_by='llm' 的)."""
        client, store = get_test_client()
        for fid in ("a", "b", "c", "d"):
            store.create_function(
                FunctionNode(
                    signature=f"def {fid}()", name=fid, file_path="f.py",
                    start_line=1, end_line=3, body_hash=f"h-{fid}", id=fid,
                )
            )
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="f.py", call_line=2,
        ))
        store.create_calls_edge("a", "c", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.py", call_line=3,
        ))
        store.create_calls_edge("b", "d", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.py", call_line=4,
        ))
        store.create_calls_edge("c", "d", CallsEdgeProps(
            resolved_by="signature", call_type="indirect",
            call_file="f.py", call_line=5,
        ))
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 4
        assert data["calls_by_resolved_by"] == {
            "symbol_table": 1,
            "llm": 2,
            "signature": 1,
            "dataflow": 0,
            "context": 0,
        }

    def test_get_stats_unresolved_by_category(self) -> None:
        """/stats buckets UnresolvedCall nodes by the `<category>:` prefix
        of last_attempt_reason so the Dashboard can show a per-category
        chip row telling reviewers whether the agent-abandoned backlog
        is dominated by LLM stalls (subprocess_timeout) vs hook crashes
        (agent_error) vs gate misses (gate_failed) — architecture.md §3
        Retry 审计字段 4 档 + §5 drill-down 契约 (category chip row)."""
        client, store = get_test_client()
        store.create_function(
            FunctionNode(
                signature="def a()", name="a", file_path="f.py",
                start_line=1, end_line=3, body_hash="h1", id="caller",
            )
        )
        # One of each of the 4 §3 categories + one without any audit
        # stamp (never retried yet) → should bucket to "none".
        categorized = [
            ("g1", "gate_failed: remaining pending GAPs"),
            ("g2", "agent_error: exit 1"),
            ("g3", "subprocess_crash: FileNotFoundError: no such binary"),
            ("g4", "subprocess_timeout: 0.2s"),
        ]
        for gid, reason in categorized:
            store.create_unresolved_call(
                UnresolvedCallNode(
                    caller_id="caller", call_expression="fp()", call_file="f.py",
                    call_line=2, call_type="indirect", source_code_snippet="fp()",
                    var_name=None, var_type=None, id=gid, status="pending",
                    retry_count=1, last_attempt_reason=reason,
                    last_attempt_timestamp="2026-05-13T00:00:00+00:00",
                )
            )
        # Second subprocess_timeout so we can verify counts aggregate
        # (not just that keys are present).
        store.create_unresolved_call(
            UnresolvedCallNode(
                caller_id="caller", call_expression="fp()", call_file="f.py",
                call_line=2, call_type="indirect", source_code_snippet="fp()",
                var_name=None, var_type=None, id="g5", status="pending",
                retry_count=2,
                last_attempt_reason="subprocess_timeout: 5.0s",
                last_attempt_timestamp="2026-05-13T00:01:00+00:00",
            )
        )
        # Never-retried GAP: no audit stamp → bucket "none".
        store.create_unresolved_call(
            UnresolvedCallNode(
                caller_id="caller", call_expression="hp()", call_file="f.py",
                call_line=4, call_type="indirect", source_code_snippet="hp()",
                var_name=None, var_type=None, id="g6", status="pending",
                retry_count=0,
            )
        )
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_unresolved"] == 6
        assert data["unresolved_by_category"] == {
            "gate_failed": 1,
            "agent_error": 1,
            "subprocess_crash": 1,
            "subprocess_timeout": 2,
            "none": 1,
        }

    def test_get_stats_unresolved_by_category_empty(self) -> None:
        """Empty store still returns all category keys with 0 so the
        frontend doesn't have to guard against undefined."""
        client, _ = get_test_client()
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        assert resp.json()["unresolved_by_category"] == {
            "gate_failed": 0, "agent_error": 0, "subprocess_crash": 0,
            "subprocess_timeout": 0, "none": 0,
        }


class TestNoPrivateAttrLeak:
    """Regression: routes must not reach into store._files / ._functions /
    ._calls_edges / ._unresolved_calls. A Protocol-only fake (no private
    dicts) must work — this is what Neo4jGraphStore looks like."""

    def _make_protocol_store(self):
        """Minimal fake that only exposes public Protocol methods."""
        from dataclasses import dataclass
        from codemap_lite.graph.neo4j_store import _CallsEdge

        class _ProtocolOnlyStore:
            def list_files(self):
                return [FileNode(file_path="a.cpp", hash="h", primary_language="cpp")]

            def list_functions(self, file_path=None):
                fn = FunctionNode(
                    signature="void f()", name="f", file_path="a.cpp",
                    start_line=1, end_line=5, body_hash="bh",
                )
                if file_path and file_path != "a.cpp":
                    return []
                return [fn]

            def list_calls_edges(self):
                return [_CallsEdge(
                    caller_id="f1", callee_id="f2",
                    props=CallsEdgeProps(
                        resolved_by="llm", call_type="indirect",
                        call_file="a.cpp", call_line=10,
                    ),
                )]

            def count_stats(self):
                return {
                    "total_functions": 1, "total_files": 1,
                    "total_calls": 1, "total_unresolved": 0,
                    "total_repair_logs": 0,
                    "unresolved_by_status": {"pending": 0, "unresolvable": 0},
                    "unresolved_by_category": {},
                    "calls_by_resolved_by": {"llm": 1},
                }

            def get_unresolved_calls(self, caller_id=None, status=None):
                return []

            def get_callers(self, fid):
                return []

            def get_callees(self, fid):
                return []

            def get_function_by_id(self, fid):
                return None

            def get_reachable_subgraph(self, sid, max_depth=50):
                return {"nodes": [], "edges": [], "unresolved": []}

            def get_repair_logs(self, limit=100, offset=0):
                return []

        return _ProtocolOnlyStore()

    def test_stats_no_private_attrs(self) -> None:
        store = self._make_protocol_store()
        app = create_app(store=store)
        client = TestClient(app)
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        assert resp.json()["total_functions"] == 1
        assert resp.json()["calls_by_resolved_by"] == {"llm": 1}

    def test_list_files_no_private_attrs(self) -> None:
        store = self._make_protocol_store()
        app = create_app(store=store)
        client = TestClient(app)
        resp = client.get("/api/v1/files")
        assert resp.status_code == 200
        assert resp.json()[0]["file_path"] == "a.cpp"

    def test_list_functions_no_private_attrs(self) -> None:
        store = self._make_protocol_store()
        app = create_app(store=store)
        client = TestClient(app)
        resp = client.get("/api/v1/functions")
        assert resp.status_code == 200
        assert resp.json()[0]["name"] == "f"

    def test_unresolved_calls_no_private_attrs(self) -> None:
        store = self._make_protocol_store()
        app = create_app(store=store)
        client = TestClient(app)
        resp = client.get("/api/v1/unresolved-calls")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestUnresolvedCallsFiltering:
    """architecture.md §5 line 371-372: ReviewQueue needs ?caller=, ?status=,
    ?category= filters on GET /api/v1/unresolved-calls."""

    def test_filter_by_caller(self) -> None:
        client, store = get_test_client()
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="func_a", call_expression="ptr(x)",
            call_file="a.cpp", call_line=5, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None, id="g1",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="func_b", call_expression="cb(y)",
            call_file="b.cpp", call_line=10, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None, id="g2",
        ))
        resp = client.get("/api/v1/unresolved-calls?caller=func_a")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["caller_id"] == "func_a"

    def test_filter_by_status(self) -> None:
        client, store = get_test_client()
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f1", call_expression="x()",
            call_file="a.cpp", call_line=1, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g1", status="pending",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f2", call_expression="y()",
            call_file="b.cpp", call_line=2, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g2", status="unresolvable", retry_count=3,
        ))
        resp = client.get("/api/v1/unresolved-calls?status=unresolvable")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "g2"

    def test_filter_by_category(self) -> None:
        client, store = get_test_client()
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f1", call_expression="x()",
            call_file="a.cpp", call_line=1, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g1", last_attempt_reason="gate_failed: remaining GAPs",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f2", call_expression="y()",
            call_file="b.cpp", call_line=2, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g2", last_attempt_reason="agent_error: exit 1",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f3", call_expression="z()",
            call_file="c.cpp", call_line=3, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g3",  # no last_attempt_reason → "none" category
        ))
        # Filter by gate_failed
        resp = client.get("/api/v1/unresolved-calls?category=gate_failed")
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["id"] == "g1"
        # Filter by "none" (no audit stamp)
        resp = client.get("/api/v1/unresolved-calls?category=none")
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["id"] == "g3"


def _make_repair_log(
    *,
    caller_id: str = "func_a",
    callee_id: str = "func_b",
    call_location: str = "foo.cpp:42",
    log_id: str | None = None,
    reasoning_summary: str = "vtable resolved via static analysis",
) -> RepairLogNode:
    kwargs: dict = dict(
        caller_id=caller_id,
        callee_id=callee_id,
        call_location=call_location,
        repair_method="llm",
        llm_response="agent stdout",
        timestamp="2026-05-13T12:00:00+00:00",
        reasoning_summary=reasoning_summary,
    )
    if log_id is not None:
        kwargs["id"] = log_id
    return RepairLogNode(**kwargs)


class TestRepairLogsEndpoint:
    """architecture.md §4 RepairLog schema + §8 GET /repair-logs +
    ADR #51 属性引用契约 — the (caller_id, callee_id, call_location)
    triple locates the matching CALLS edge so the frontend
    CallGraphView can render an audit panel for any selected
    `resolved_by='llm'` edge."""

    def test_list_all_repair_logs_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/repair-logs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_persisted_logs(self) -> None:
        client, store = get_test_client()
        log = _make_repair_log(log_id="r1")
        store.create_repair_log(log)
        resp = client.get("/api/v1/repair-logs")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == "r1"
        assert body[0]["caller_id"] == "func_a"
        assert body[0]["callee_id"] == "func_b"
        assert body[0]["call_location"] == "foo.cpp:42"
        assert body[0]["repair_method"] == "llm"
        assert body[0]["reasoning_summary"].startswith("vtable")

    def test_filter_by_triple_locates_single_log(self) -> None:
        client, store = get_test_client()
        store.create_repair_log(
            _make_repair_log(call_location="foo.cpp:42", log_id="r1")
        )
        store.create_repair_log(
            _make_repair_log(call_location="foo.cpp:99", log_id="r2")
        )
        resp = client.get(
            "/api/v1/repair-logs",
            params={
                "caller": "func_a",
                "callee": "func_b",
                "location": "foo.cpp:42",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == "r1"

    def test_filter_by_caller_only(self) -> None:
        client, store = get_test_client()
        store.create_repair_log(_make_repair_log(caller_id="func_a", log_id="r1"))
        store.create_repair_log(_make_repair_log(caller_id="func_z", log_id="r2"))
        resp = client.get("/api/v1/repair-logs", params={"caller": "func_a"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == "r1"

    def test_total_repair_logs_in_stats(self) -> None:
        """/stats reports `total_repair_logs` so the Dashboard can show
        cumulative llm-repair volume without hitting /repair-logs
        (architecture.md §8 stats契约)."""
        client, store = get_test_client()
        # Empty case still surfaces the field.
        empty = client.get("/api/v1/stats").json()
        assert empty["total_repair_logs"] == 0

        store.create_repair_log(_make_repair_log(log_id="r1"))
        store.create_repair_log(
            _make_repair_log(log_id="r2", call_location="foo.cpp:99")
        )
        populated = client.get("/api/v1/stats").json()
        assert populated["total_repair_logs"] == 2


class TestEdgeDeletion:
    """architecture.md §5 审阅交互: '标记错误时 → 立即删除该 CALLS 边 + 对应
    RepairLog'. DELETE /api/v1/edges must target a specific edge by
    (caller_id, callee_id, call_file, call_line), not bulk-delete."""

    def test_delete_specific_edge(self) -> None:
        """Deleting a specific edge by its identifying tuple."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        fn3 = FunctionNode(
            signature="void c()", name="c", file_path="f.cpp",
            start_line=20, end_line=25, body_hash="h3", id="c",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_function(fn3)
        # Two edges from a
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="direct",
            call_file="f.cpp", call_line=3,
        ))
        store.create_calls_edge("a", "c", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="f.cpp", call_line=4,
        ))
        assert len(store.list_calls_edges()) == 2

        # Delete only the a→b edge
        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "a",
                "callee_id": "b",
                "call_file": "f.cpp",
                "call_line": 3,
            },
        )
        assert resp.status_code == 204

        # Only a→c should remain
        remaining = store.list_calls_edges()
        assert len(remaining) == 1
        assert remaining[0].callee_id == "c"

    def test_delete_edge_not_found_returns_404(self) -> None:
        """Deleting a non-existent edge returns 404."""
        client, store = get_test_client()
        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "x",
                "callee_id": "y",
                "call_file": "f.cpp",
                "call_line": 1,
            },
        )
        assert resp.status_code == 404

    def test_delete_edge_also_deletes_repair_log(self) -> None:
        """architecture.md §5 line 326: '立即删除该 CALLS 边 + 对应 RepairLog'.
        When an edge is deleted, the corresponding RepairLog must also be removed."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.cpp", call_line=3,
        ))
        store.create_repair_log(RepairLogNode(
            id="rl1", caller_id="a", callee_id="b",
            call_location="f.cpp:3",
            repair_method="llm", llm_response="resolved",
            timestamp="2026-05-14T00:00:00Z", reasoning_summary="test",
        ))

        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "a",
                "callee_id": "b",
                "call_file": "f.cpp",
                "call_line": 3,
            },
        )
        assert resp.status_code == 204

        logs = store.get_repair_logs(caller_id="a", callee_id="b")
        assert len(logs) == 0, (
            "architecture.md §5: deleting edge must also delete RepairLog"
        )

    def test_delete_edge_regenerates_unresolved_call(self) -> None:
        """architecture.md §5 line 327: '重新生成 UnresolvedCall 节点（retry_count=0）'.
        After deleting an edge, a new UnresolvedCall must be created for the caller."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.cpp", call_line=3,
        ))

        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "a",
                "callee_id": "b",
                "call_file": "f.cpp",
                "call_line": 3,
            },
        )
        assert resp.status_code == 204

        gaps = store.get_unresolved_calls(caller_id="a")
        assert len(gaps) == 1, (
            "architecture.md §5: deleting edge must regenerate UnresolvedCall"
        )
        gap = gaps[0]
        assert gap.caller_id == "a"
        assert gap.call_file == "f.cpp"
        assert gap.call_line == 3
        assert gap.call_type == "indirect"
        assert gap.retry_count == 0
        assert gap.status == "pending"

    def test_delete_edge_triggers_async_repair(self) -> None:
        """architecture.md §5 line 328: '触发 Agent 重新修复该 source 点（异步）'.

        When settings are available on app.state, deleting an edge must
        schedule a background repair task for the affected source.
        """
        from unittest.mock import patch, MagicMock

        store = InMemoryGraphStore()
        # Create a minimal settings mock
        settings = MagicMock()
        settings.agent.backend = "claudecode"
        settings.agent.max_concurrency = 1
        settings.agent.subprocess_timeout_seconds = None
        settings.project.target_dir = "/tmp/test"
        settings.neo4j.uri = "bolt://localhost:7687"
        settings.neo4j.user = "neo4j"
        settings.neo4j.password = ""

        app = create_app(store=store, settings=settings)
        client = TestClient(app)

        # Set up an edge to delete
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.cpp", call_line=3,
        ))

        # Patch _trigger_repair_for_source to verify it's called
        with patch(
            "codemap_lite.api.routes.review._trigger_repair_for_source"
        ) as mock_trigger:
            resp = client.request(
                "DELETE", "/api/v1/edges",
                json={
                    "caller_id": "a",
                    "callee_id": "b",
                    "call_file": "f.cpp",
                    "call_line": 3,
                },
            )
            assert resp.status_code == 204
            # Background task should have been called with settings + caller_id
            mock_trigger.assert_called_once_with(settings, "a")

    def test_delete_edges_for_function_bulk(self) -> None:
        """architecture.md §7: DELETE /edges/{function_id} bulk-deletes all
        edges touching a function (used by incremental invalidation)."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        fn3 = FunctionNode(
            signature="void c()", name="c", file_path="g.cpp",
            start_line=1, end_line=5, body_hash="h3", id="c",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_function(fn3)
        # a→b, b→c, c→a (cycle)
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.cpp", call_line=3,
        ))
        store.create_calls_edge("b", "c", CallsEdgeProps(
            resolved_by="signature", call_type="direct",
            call_file="f.cpp", call_line=12,
        ))
        store.create_calls_edge("c", "a", CallsEdgeProps(
            resolved_by="dataflow", call_type="indirect",
            call_file="g.cpp", call_line=4,
        ))
        assert len(store.list_calls_edges()) == 3

        # Delete all edges touching function "b" (a→b and b→c)
        resp = client.delete("/api/v1/edges/b")
        assert resp.status_code == 204

        # Only c→a should remain
        remaining = store.list_calls_edges()
        assert len(remaining) == 1
        assert remaining[0].caller_id == "c"
        assert remaining[0].callee_id == "a"
    """architecture.md §8: /api/v1/stats must return unresolved_by_category
    with all 5 keys always present, and calls_by_resolved_by with all 5
    resolved_by values."""

    def test_stats_unresolved_by_category_all_keys_present(self) -> None:
        """Even with no unresolved calls, all category keys must appear with 0."""
        client, store = get_test_client()
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "unresolved_by_category" in data
        cats = data["unresolved_by_category"]
        expected_keys = {"gate_failed", "agent_error", "subprocess_crash",
                         "subprocess_timeout", "none"}
        assert set(cats.keys()) == expected_keys, (
            f"unresolved_by_category must always have all 5 keys, got {set(cats.keys())}"
        )
        # All should be 0 when no unresolved calls exist
        for k in expected_keys:
            assert cats[k] == 0

    def test_stats_calls_by_resolved_by_all_keys_present(self) -> None:
        """Even with no calls edges, all resolved_by keys must appear with 0."""
        client, store = get_test_client()
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "calls_by_resolved_by" in data
        resolved = data["calls_by_resolved_by"]
        expected_keys = {"symbol_table", "signature", "dataflow", "context", "llm"}
        assert set(resolved.keys()) == expected_keys, (
            f"calls_by_resolved_by must always have all 5 keys, got {set(resolved.keys())}"
        )
        for k in expected_keys:
            assert resolved[k] == 0

    def test_stats_category_bucketing_correct(self) -> None:
        """Verify category extraction from last_attempt_reason prefix."""
        client, store = get_test_client()
        # Add unresolved calls with different reasons
        fn = FunctionNode(
            id="f1", signature="void f()", name="f",
            file_path="a.c", start_line=1, end_line=5, body_hash="h",
        )
        store.create_function(fn)
        reasons = [
            ("gap_1", "gate_failed: remaining pending GAPs"),
            ("gap_2", "agent_error: exit 1"),
            ("gap_3", "subprocess_timeout: 30s"),
            ("gap_4", "subprocess_crash: OSError: No such file"),
            ("gap_5", None),  # no reason → "none" bucket
        ]
        for gap_id, reason in reasons:
            gap = UnresolvedCallNode(
                id=gap_id, caller_id="f1", call_expression="x()",
                call_file="a.c", call_line=1, call_type="indirect",
                source_code_snippet="x();", var_name=None, var_type=None,
                last_attempt_reason=reason,
            )
            store.create_unresolved_call(gap)

        resp = client.get("/api/v1/stats")
        data = resp.json()
        cats = data["unresolved_by_category"]
        assert cats["gate_failed"] == 1
        assert cats["agent_error"] == 1
        assert cats["subprocess_timeout"] == 1
        assert cats["subprocess_crash"] == 1
        assert cats["none"] == 1

"""Tests for the FastAPI REST API layer."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
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

    def test_analyze_trigger_invalid_mode(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze", json={"mode": "invalid"})
        assert resp.status_code == 422

    def test_analyze_repair(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze/repair")
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"

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


class TestReviewEndpoint:
    def test_post_review(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/reviews", json={
            "function_id": "func-1",
            "comment": "Looks correct",
            "status": "approved",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["function_id"] == "func-1"
        assert "id" in data

    def test_get_reviews(self) -> None:
        client, _ = get_test_client()
        # Create a review first
        client.post("/api/v1/reviews", json={
            "function_id": "func-1",
            "comment": "OK",
            "status": "approved",
        })
        resp = client.get("/api/v1/reviews")
        assert resp.status_code == 200
        reviews = resp.json()
        assert len(reviews) == 1

    def test_update_review(self) -> None:
        client, _ = get_test_client()
        create_resp = client.post("/api/v1/reviews", json={
            "function_id": "func-1",
            "comment": "Initial",
            "status": "pending",
        })
        review_id = create_resp.json()["id"]
        resp = client.put(f"/api/v1/reviews/{review_id}", json={
            "comment": "Updated",
            "status": "approved",
        })
        assert resp.status_code == 200
        assert resp.json()["comment"] == "Updated"

    def test_delete_review(self) -> None:
        client, _ = get_test_client()
        create_resp = client.post("/api/v1/reviews", json={
            "function_id": "func-1",
            "comment": "To delete",
            "status": "pending",
        })
        review_id = create_resp.json()["id"]
        resp = client.delete(f"/api/v1/reviews/{review_id}")
        assert resp.status_code == 204

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

    def test_delete_edge(self) -> None:
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="def x()", name="x", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="x",
        )
        store.create_function(fn1)
        resp = client.delete("/api/v1/edges/x")
        assert resp.status_code == 204


class TestFeedbackEndpoint:
    def test_get_feedback_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/feedback")
        assert resp.status_code == 200
        assert resp.json() == []

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

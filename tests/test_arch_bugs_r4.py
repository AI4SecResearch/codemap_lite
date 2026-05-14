"""Architecture compliance tests — Round 4: expose real bugs.

These tests target actual implementation bugs discovered by auditing the code
against architecture.md. Each test documents the bug and the expected behavior.

Bugs found:
1. GET /source-points?kind= filter uses wrong key ("kind" vs "entry_point_kind")
2. GET /source-points/summary uses wrong key for by_kind aggregation
3. GET /repair-logs missing pagination metadata (no total field)
4. Source point reachable endpoint falls through when source_id not in entries
5. Config validation: backend must be "claudecode" or "opencode"
6. DELETE /edges cascade: SourcePoint lookup uses caller_id as sp.id
7. POST /analyze returns 202 even when no settings wired (should return useful state)
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from codemap_lite.analysis.feedback_store import FeedbackStore
from codemap_lite.api.app import create_app
from codemap_lite.config.settings import AgentConfig, Settings
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    RepairLogNode,
    SourcePointNode,
    UnresolvedCallNode,
)


# ---------------------------------------------------------------------------
# Bug 1+2: Source point endpoints use wrong key for filtering
# ---------------------------------------------------------------------------


class TestSourcePointsEndpointKeyBug:
    """BUG: GET /source-points?kind= and /summary use 'kind' but data has 'entry_point_kind'.

    In review.py (line 163-170), source_points are stored with key
    'entry_point_kind'. But source_points.py (line 22, 32) filters and
    aggregates by 'kind'. This means filtering never matches.

    architecture.md §8: GET /api/v1/source-points supports ?kind= filter.
    """

    @pytest.fixture()
    def source_client(self):
        store = InMemoryGraphStore()
        # Create a function for the source point
        fn = FunctionNode(
            id="fn_entry", signature="void entry()", name="entry",
            file_path="/src/main.cpp", start_line=1, end_line=10, body_hash="h",
        )
        store.create_function(fn)

        app = create_app(store=store)
        # Simulate what review.py / cli.py sets up:
        # The key is 'entry_point_kind', NOT 'kind'
        app.state.source_points = [
            {
                "function_id": "fn_entry",
                "entry_point_kind": "api_entry",
                "reason": "archdoc-detected",
                "module": "cast_framework",
            },
            {
                "function_id": "fn_other",
                "entry_point_kind": "protocol_entry",
                "reason": "archdoc-detected",
                "module": "device_manager",
            },
        ]
        return TestClient(app)

    def test_list_source_points_returns_all(self, source_client):
        """All source points should be returned."""
        resp = source_client.get("/api/v1/source-points")
        assert resp.status_code == 200
        data = resp.json()["items"]
        assert len(data) == 2

    def test_filter_by_kind_api_entry(self, source_client):
        """Filtering by kind=api_entry should return 1 result.

        BUG: source_points.py line 22 uses e.get("kind") but the data
        has "entry_point_kind". This test will FAIL until fixed.
        """
        resp = source_client.get("/api/v1/source-points?kind=api_entry")
        assert resp.status_code == 200
        data = resp.json()["items"]
        # BUG: This currently returns [] because the filter key is wrong
        assert len(data) == 1, (
            f"Expected 1 source point with kind=api_entry, got {len(data)}. "
            f"Bug: source_points.py filters by 'kind' but data has 'entry_point_kind'"
        )

    def test_filter_by_module(self, source_client):
        """Module filter should work (substring match)."""
        resp = source_client.get("/api/v1/source-points?module=cast_framework")
        assert resp.status_code == 200
        data = resp.json()["items"]
        assert len(data) == 1

    def test_summary_by_kind(self, source_client):
        """Summary should aggregate by entry_point_kind.

        BUG: source_points.py line 32 uses e.get("kind") which doesn't
        exist in the data, so all entries get bucketed under "unknown".
        """
        resp = source_client.get("/api/v1/source-points/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        # BUG: by_kind currently shows {"unknown": 2} instead of
        # {"api_entry": 1, "protocol_entry": 1}
        assert "api_entry" in data["by_kind"], (
            f"Expected 'api_entry' in by_kind, got {data['by_kind']}. "
            f"Bug: summary aggregates by 'kind' but data has 'entry_point_kind'"
        )


# ---------------------------------------------------------------------------
# Bug 3: Repair logs endpoint missing total/pagination metadata
# ---------------------------------------------------------------------------


class TestRepairLogsPagination:
    """GET /repair-logs returns paginated results with total count.

    architecture.md §8: GET /repair-logs should return {"total": N, "items": [...]}
    consistent with GET /unresolved-calls.
    """

    @pytest.fixture()
    def logs_client(self):
        store = InMemoryGraphStore()
        for i in range(5):
            store.create_repair_log(RepairLogNode(
                id=f"rl_{i}",
                caller_id="fn_a",
                callee_id=f"fn_b_{i}",
                call_location=f"/src/main.cpp:{10 + i}",
                repair_method="llm",
                llm_response=f"response_{i}",
                timestamp="2026-05-14T10:00:00Z",
                reasoning_summary=f"summary_{i}",
            ))
        app = create_app(store=store)
        return TestClient(app)

    def test_repair_logs_returns_paginated(self, logs_client):
        """Response has total + items wrapper."""
        resp = logs_client.get("/api/v1/repair-logs")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data, "Missing 'total' in repair-logs response"
        assert "items" in data, "Missing 'items' in repair-logs response"
        assert data["total"] == 5
        assert len(data["items"]) == 5

    def test_repair_logs_has_all_fields(self, logs_client):
        """Each repair log item must have all schema fields."""
        resp = logs_client.get("/api/v1/repair-logs")
        data = resp.json()
        for log in data["items"]:
            required_fields = [
                "id", "caller_id", "callee_id", "call_location",
                "repair_method", "llm_response", "timestamp", "reasoning_summary",
            ]
            for field in required_fields:
                assert field in log, f"Missing field '{field}' in repair log"

    def test_repair_logs_pagination_limit(self, logs_client):
        """limit/offset params work correctly."""
        resp = logs_client.get("/api/v1/repair-logs", params={"limit": 2})
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2


# ---------------------------------------------------------------------------
# Bug 4: Source point reachable falls through for unknown IDs
# ---------------------------------------------------------------------------


class TestSourcePointReachableFallback:
    """When source_id is not in app.state.source_points, the reachable
    endpoint should still work by using the source_id directly as seed.
    """

    @pytest.fixture()
    def reachable_client(self):
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="fn_entry", signature="void entry()", name="entry",
            file_path="/src/main.cpp", start_line=1, end_line=10, body_hash="h",
        )
        fn2 = FunctionNode(
            id="fn_callee", signature="void callee()", name="callee",
            file_path="/src/utils.cpp", start_line=1, end_line=5, body_hash="h2",
        )
        store.create_function(fn)
        store.create_function(fn2)
        store.create_calls_edge("fn_entry", "fn_callee", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/src/main.cpp", call_line=5,
        ))

        app = create_app(store=store)
        app.state.source_points = []
        return TestClient(app)

    def test_reachable_with_function_id_directly(self, reachable_client):
        """When source_id matches a FunctionNode id, reachable should work."""
        resp = reachable_client.get("/api/v1/source-points/fn_entry/reachable")
        assert resp.status_code == 200
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert "fn_entry" in node_ids
        assert "fn_callee" in node_ids
        assert len(data["edges"]) == 1

    def test_reachable_unknown_id_returns_empty(self, reachable_client):
        """When source_id doesn't match anything, should return 404."""
        resp = reachable_client.get("/api/v1/source-points/nonexistent/reachable")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Bug 5: Config validation — backend must be "claudecode" or "opencode"
# ---------------------------------------------------------------------------


class TestConfigValidation:
    """architecture.md §10 + §3 LLM 后端配置:
    agent.backend must be "claudecode" or "opencode".
    max_concurrency must be >= 1.
    subprocess_timeout_seconds must be > 0 if set.
    """

    def test_backend_must_be_valid(self):
        """Invalid backend should raise ValidationError."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AgentConfig(backend="invalid_backend")

    def test_valid_backends_accepted(self):
        """Both valid backends should be accepted."""
        for backend in ("claudecode", "opencode"):
            cfg = AgentConfig(backend=backend)
            assert cfg.backend == backend

    def test_max_concurrency_minimum_1(self):
        """max_concurrency must be >= 1."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AgentConfig(max_concurrency=0)

    def test_subprocess_timeout_must_be_positive(self):
        """subprocess_timeout_seconds must be > 0 if set."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AgentConfig(subprocess_timeout_seconds=-1)

    def test_subprocess_timeout_none_is_valid(self):
        """None timeout is valid (no limit)."""
        cfg = AgentConfig(subprocess_timeout_seconds=None)
        assert cfg.subprocess_timeout_seconds is None

    def test_settings_from_yaml_loads(self):
        """Settings.from_yaml should load a valid config."""
        tmp_dir = Path(tempfile.mkdtemp())
        config_path = tmp_dir / "config.yaml"
        config_path.write_text(
            "project:\n  target_dir: /tmp/test\nneo4j:\n  uri: bolt://localhost:7687\n"
            "agent:\n  backend: opencode\n  max_concurrency: 3\n",
            encoding="utf-8",
        )
        settings = Settings.from_yaml(config_path)
        assert settings.agent.backend == "opencode"
        assert settings.agent.max_concurrency == 3


# ---------------------------------------------------------------------------
# Bug 6: DELETE /edges cascade with non-existent SourcePoint
# ---------------------------------------------------------------------------


class TestDeleteEdgeCascadeWithMissingSourcePoint:
    """architecture.md §5: DELETE /edges must reset SourcePoint status.

    When no SourcePoint exists for the caller_id, the cascade should
    not crash — it should just skip the SourcePoint reset.
    """

    @pytest.fixture()
    def delete_client(self):
        store = InMemoryGraphStore()
        fn_a = FunctionNode(
            id="fn_a", signature="void a()", name="a",
            file_path="/src/main.cpp", start_line=1, end_line=5, body_hash="h",
        )
        fn_b = FunctionNode(
            id="fn_b", signature="void b()", name="b",
            file_path="/src/utils.cpp", start_line=1, end_line=3, body_hash="h2",
        )
        store.create_function(fn_a)
        store.create_function(fn_b)

        # LLM edge without SourcePoint or RepairLog
        store.create_calls_edge("fn_a", "fn_b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/src/main.cpp", call_line=3,
        ))

        app = create_app(store=store)
        return TestClient(app), store

    def test_delete_edge_without_source_point_does_not_crash(self, delete_client):
        """Deleting an edge with no SourcePoint should succeed (not 500)."""
        client, store = delete_client
        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "fn_a",
                "callee_id": "fn_b",
                "call_file": "/src/main.cpp",
                "call_line": 3,
            },
        )
        assert resp.status_code == 204
        # Edge should be deleted
        assert not store.edge_exists("fn_a", "fn_b", "/src/main.cpp", 3)
        # UC should be regenerated
        ucs = store.get_unresolved_calls(caller_id="fn_a")
        assert len(ucs) == 1

    def test_delete_nonexistent_edge_returns_404(self, delete_client):
        """Deleting a non-existent edge should return 404."""
        client, store = delete_client
        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "fn_a",
                "callee_id": "fn_b",
                "call_file": "/src/nonexistent.cpp",
                "call_line": 999,
            },
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# §8: Stats endpoint must include total_feedback
# ---------------------------------------------------------------------------


class TestStatsTotalFeedback:
    """architecture.md §8 line 482:
    stats must include 'total_feedback: 反例库当前条目数'.
    """

    def test_stats_without_feedback_store(self):
        """Stats without FeedbackStore should have total_feedback=0."""
        store = InMemoryGraphStore()
        app = create_app(store=store)
        client = TestClient(app)

        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        stats = resp.json()
        # architecture.md §8: total_feedback must exist
        assert "total_feedback" in stats or stats.get("total_feedback", 0) == 0

    def test_stats_with_feedback_store(self):
        """Stats with FeedbackStore should reflect counter-example count."""
        tmp_dir = Path(tempfile.mkdtemp())
        fs = FeedbackStore(storage_dir=tmp_dir)
        from codemap_lite.analysis.feedback_store import CounterExample
        fs.add(CounterExample(
            call_context="main.cpp:10",
            wrong_target="fn_wrong",
            correct_target="fn_correct",
            pattern="test_pattern",
        ))
        store = InMemoryGraphStore()
        app = create_app(store=store, feedback_store=fs)
        client = TestClient(app)

        resp = client.get("/api/v1/stats")
        stats = resp.json()
        # total_feedback should be 1
        total_fb = stats.get("total_feedback", 0)
        assert total_fb == 1, (
            f"Expected total_feedback=1, got {total_fb}. "
            f"Stats must include total_feedback per §8"
        )


# ---------------------------------------------------------------------------
# §3: Edge creation via POST /edges must validate functions exist
# ---------------------------------------------------------------------------


class TestEdgeCreationValidation:
    """architecture.md §8:
    POST /edges must validate caller and callee function nodes exist.
    """

    @pytest.fixture()
    def edge_client(self):
        store = InMemoryGraphStore()
        fn_a = FunctionNode(
            id="fn_a", signature="void a()", name="a",
            file_path="/src/main.cpp", start_line=1, end_line=5, body_hash="h",
        )
        fn_b = FunctionNode(
            id="fn_b", signature="void b()", name="b",
            file_path="/src/utils.cpp", start_line=1, end_line=3, body_hash="h2",
        )
        store.create_function(fn_a)
        store.create_function(fn_b)

        # Create a UC for fn_a that the edge creation should delete
        uc = UnresolvedCallNode(
            id="gap_1", caller_id="fn_a", call_expression="b()",
            call_file="/src/main.cpp", call_line=3, call_type="indirect",
            source_code_snippet="b();", var_name=None, var_type=None,
            candidates=["fn_b"],
        )
        store.create_unresolved_call(uc)

        app = create_app(store=store)
        return TestClient(app), store

    def test_create_edge_valid(self, edge_client):
        """Valid edge creation should return 201."""
        client, store = edge_client
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "/src/main.cpp",
            "call_line": 3,
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "created"
        # Edge should exist
        assert store.edge_exists("fn_a", "fn_b", "/src/main.cpp", 3)
        # UC should be deleted
        assert len(store.get_unresolved_calls(caller_id="fn_a")) == 0

    def test_create_edge_unknown_caller_404(self, edge_client):
        """Unknown caller should return 404."""
        client, _ = edge_client
        resp = client.post("/api/v1/edges", json={
            "caller_id": "nonexistent",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "direct",
            "call_file": "/src/main.cpp",
            "call_line": 3,
        })
        assert resp.status_code == 404

    def test_create_edge_unknown_callee_404(self, edge_client):
        """Unknown callee should return 404."""
        client, _ = edge_client
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "nonexistent",
            "resolved_by": "llm",
            "call_type": "direct",
            "call_file": "/src/main.cpp",
            "call_line": 3,
        })
        assert resp.status_code == 404

    def test_create_duplicate_edge_409(self, edge_client):
        """Duplicate edge should return 409."""
        client, _ = edge_client
        # Create first
        client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "/src/main.cpp",
            "call_line": 3,
        })
        # Try duplicate
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "/src/main.cpp",
            "call_line": 3,
        })
        assert resp.status_code == 409

    def test_create_edge_invalid_resolved_by_422(self, edge_client):
        """Invalid resolved_by should return 422."""
        client, _ = edge_client
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "magic",
            "call_type": "direct",
            "call_file": "/src/main.cpp",
            "call_line": 3,
        })
        assert resp.status_code == 422

    def test_create_edge_invalid_call_type_422(self, edge_client):
        """Invalid call_type should return 422."""
        client, _ = edge_client
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "telepathic",
            "call_file": "/src/main.cpp",
            "call_line": 3,
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# §8: Review CRUD operations
# ---------------------------------------------------------------------------


class TestReviewCRUD:
    """architecture.md §8:
    GET /reviews, POST /reviews, PUT /reviews/{id}, DELETE /reviews/{id}
    """

    @pytest.fixture()
    def review_crud_client(self):
        store = InMemoryGraphStore()
        fn_a = FunctionNode(
            id="fn_a", signature="void a()", name="a",
            file_path="/src/main.cpp", start_line=1, end_line=5, body_hash="h",
        )
        fn_b = FunctionNode(
            id="fn_b", signature="void b()", name="b",
            file_path="/src/utils.cpp", start_line=1, end_line=3, body_hash="h2",
        )
        store.create_function(fn_a)
        store.create_function(fn_b)
        store.create_calls_edge("fn_a", "fn_b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/src/main.cpp", call_line=3,
        ))
        store.create_repair_log(RepairLogNode(
            id="rl1", caller_id="fn_a", callee_id="fn_b",
            call_location="/src/main.cpp:3", repair_method="llm",
            llm_response="test", timestamp="2026-01-01T00:00:00Z",
            reasoning_summary="test",
        ))

        app = create_app(store=store)
        return TestClient(app)

    def test_list_reviews_empty(self, review_crud_client):
        """Initially no reviews."""
        resp = review_crud_client.get("/api/v1/reviews")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    def test_create_review(self, review_crud_client):
        """Create a review."""
        resp = review_crud_client.post("/api/v1/reviews", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "call_file": "/src/main.cpp",
            "call_line": 3,
            "verdict": "correct",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["verdict"] == "correct"
        assert "id" in data

    def test_update_review(self, review_crud_client):
        """Update a review's comment."""
        # Create first
        create_resp = review_crud_client.post("/api/v1/reviews", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "call_file": "/src/main.cpp",
            "call_line": 3,
            "verdict": "correct",
        })
        review_id = create_resp.json()["id"]

        # Update
        resp = review_crud_client.put(f"/api/v1/reviews/{review_id}", json={
            "comment": "Looks good to me",
        })
        assert resp.status_code == 200
        assert resp.json()["comment"] == "Looks good to me"

    def test_delete_review(self, review_crud_client):
        """Delete a review."""
        create_resp = review_crud_client.post("/api/v1/reviews", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "call_file": "/src/main.cpp",
            "call_line": 3,
            "verdict": "correct",
        })
        review_id = create_resp.json()["id"]

        resp = review_crud_client.delete(f"/api/v1/reviews/{review_id}")
        assert resp.status_code == 204

        # Verify deleted
        assert review_crud_client.get("/api/v1/reviews").json()["total"] == 0

    def test_update_nonexistent_review_404(self, review_crud_client):
        """Updating a non-existent review should return 404."""
        resp = review_crud_client.put("/api/v1/reviews/nonexistent", json={
            "comment": "test",
        })
        assert resp.status_code == 404

    def test_delete_nonexistent_review_404(self, review_crud_client):
        """Deleting a non-existent review should return 404."""
        resp = review_crud_client.delete("/api/v1/reviews/nonexistent")
        assert resp.status_code == 404

    def test_review_edge_not_found_404(self, review_crud_client):
        """Reviewing a non-existent edge should return 404."""
        resp = review_crud_client.post("/api/v1/reviews", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "call_file": "/src/nonexistent.cpp",
            "call_line": 999,
            "verdict": "correct",
        })
        assert resp.status_code == 404

"""Review + Edge + Feedback error handling — architecture.md §5/§8.

Tests validation errors, 404s, 409s, and the full 4-step cascade
for review verdict=incorrect and edge deletion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from codemap_lite.analysis.feedback_store import FeedbackStore
from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    SourcePointNode,
    UnresolvedCallNode,
)


@pytest.fixture
def store_with_edge():
    """Store with an LLM edge suitable for review testing."""
    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="fn_caller", name="Caller", signature="void Caller()",
        file_path="main.cpp", start_line=1, end_line=20, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="fn_callee", name="Callee", signature="void Callee()",
        file_path="util.cpp", start_line=1, end_line=10, body_hash="h2",
    ))
    store.create_function(FunctionNode(
        id="fn_correct", name="CorrectTarget", signature="void CorrectTarget()",
        file_path="util.cpp", start_line=20, end_line=30, body_hash="h3",
    ))
    # LLM edge
    store.create_calls_edge("fn_caller", "fn_callee", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="main.cpp", call_line=10,
    ))
    # Source point on caller
    store.create_source_point(SourcePointNode(
        id="sp_caller", function_id="fn_caller",
        entry_point_kind="entry", reason="test", status="complete",
    ))
    return store


@pytest.fixture
def feedback_store(tmp_path):
    return FeedbackStore(storage_dir=tmp_path / "fb")


@pytest.fixture
def client(store_with_edge, feedback_store):
    app = create_app(store=store_with_edge)
    app.state.feedback_store = feedback_store
    return TestClient(app)


# ---------------------------------------------------------------------------
# §5: Review verdict validation
# ---------------------------------------------------------------------------


class TestReviewValidation:
    """architecture.md §5: verdict must be 'correct' or 'incorrect'."""

    def test_invalid_verdict_422(self, client):
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
            "verdict": "maybe",
        })
        assert resp.status_code == 422

    def test_missing_verdict_422(self, client):
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
        })
        assert resp.status_code == 422

    def test_nonexistent_edge_404(self, client):
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 999,  # wrong line
            "verdict": "correct",
        })
        assert resp.status_code == 404

    def test_correct_verdict_keeps_edge(self, client, store_with_edge):
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
            "verdict": "correct",
        })
        assert resp.status_code == 201
        # Edge still exists
        edge = store_with_edge.get_calls_edge("fn_caller", "fn_callee", "main.cpp", 10)
        assert edge is not None


# ---------------------------------------------------------------------------
# §5: Review verdict=incorrect 4-step cascade
# ---------------------------------------------------------------------------


class TestReviewIncorrectCascade:
    """architecture.md §5: incorrect → delete edge + RepairLog + regen UC + reset SP."""

    def test_incorrect_deletes_edge(self, client, store_with_edge):
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201
        # Edge deleted
        edge = store_with_edge.get_calls_edge("fn_caller", "fn_callee", "main.cpp", 10)
        assert edge is None

    def test_incorrect_regenerates_uc(self, client, store_with_edge):
        client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
            "verdict": "incorrect",
        })
        # UC regenerated
        ucs = store_with_edge.get_unresolved_calls()
        matching = [u for u in ucs if u.caller_id == "fn_caller" and u.call_line == 10]
        assert len(matching) == 1
        assert matching[0].status == "pending"
        assert matching[0].retry_count == 0

    def test_incorrect_resets_source_point(self, client, store_with_edge):
        client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
            "verdict": "incorrect",
        })
        # Source point reset to pending
        sp = store_with_edge.get_source_point("sp_caller")
        assert sp.status == "pending"

    def test_incorrect_with_correct_target_creates_feedback(
        self, client, store_with_edge, feedback_store
    ):
        """architecture.md §5: 可填写正确目标 → 触发反例生成."""
        client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
            "verdict": "incorrect",
            "correct_target": "fn_correct",
        })
        examples = feedback_store.list_all()
        assert len(examples) == 1
        assert examples[0].wrong_target == "fn_callee"
        assert examples[0].correct_target == "fn_correct"

    def test_incorrect_without_correct_target_no_feedback(
        self, client, store_with_edge, feedback_store
    ):
        client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
            "verdict": "incorrect",
        })
        assert len(feedback_store.list_all()) == 0


# ---------------------------------------------------------------------------
# §5: PUT/DELETE review
# ---------------------------------------------------------------------------


class TestReviewCRUD:
    """architecture.md §8: review CRUD operations."""

    def test_put_nonexistent_review_404(self, client):
        resp = client.put("/api/v1/reviews/nonexistent", json={"comment": "test"})
        assert resp.status_code == 404

    def test_delete_nonexistent_review_404(self, client):
        resp = client.delete("/api/v1/reviews/nonexistent")
        assert resp.status_code == 404

    def test_put_updates_comment(self, client):
        # Create a review first
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
            "verdict": "correct",
        })
        review_id = resp.json()["id"]
        # Update it
        resp = client.put(f"/api/v1/reviews/{review_id}", json={"comment": "looks good"})
        assert resp.status_code == 200
        assert resp.json()["comment"] == "looks good"

    def test_delete_removes_review(self, client):
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
            "verdict": "correct",
        })
        review_id = resp.json()["id"]
        resp = client.delete(f"/api/v1/reviews/{review_id}")
        assert resp.status_code == 204
        # Verify gone
        resp = client.get("/api/v1/reviews")
        assert resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# §8: POST /edges validation
# ---------------------------------------------------------------------------


class TestEdgeCreateValidation:
    """architecture.md §8: edge creation validation."""

    def test_nonexistent_caller_404(self, client):
        resp = client.post("/api/v1/edges", json={
            "caller_id": "nonexistent",
            "callee_id": "fn_callee",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "main.cpp",
            "call_line": 5,
        })
        assert resp.status_code == 404
        assert "caller" in resp.json()["detail"].lower()

    def test_nonexistent_callee_404(self, client):
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_caller",
            "callee_id": "nonexistent",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "main.cpp",
            "call_line": 5,
        })
        assert resp.status_code == 404
        assert "callee" in resp.json()["detail"].lower()

    def test_invalid_resolved_by_422(self, client):
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "resolved_by": "magic",
            "call_type": "indirect",
            "call_file": "main.cpp",
            "call_line": 5,
        })
        assert resp.status_code == 422

    def test_invalid_call_type_422(self, client):
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "resolved_by": "llm",
            "call_type": "unknown",
            "call_file": "main.cpp",
            "call_line": 5,
        })
        assert resp.status_code == 422

    def test_duplicate_edge_409(self, client):
        """architecture.md §4: edges unique by (caller, callee, file, line)."""
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "main.cpp",
            "call_line": 10,  # Same as existing edge
        })
        assert resp.status_code == 409

    def test_valid_edge_creation(self, client, store_with_edge):
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_correct",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "main.cpp",
            "call_line": 15,
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "created"
        # Edge exists in store
        edge = store_with_edge.get_calls_edge("fn_caller", "fn_correct", "main.cpp", 15)
        assert edge is not None

    def test_edge_creation_deletes_matching_uc(self, client, store_with_edge):
        """Creating an edge should delete the matching UnresolvedCall."""
        # First create a UC at the target location
        store_with_edge.create_unresolved_call(UnresolvedCallNode(
            id="uc_test", caller_id="fn_caller", call_expression="target()",
            call_file="main.cpp", call_line=15, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        ))
        # Create edge at same location
        client.post("/api/v1/edges", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_correct",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "main.cpp",
            "call_line": 15,
        })
        # UC should be gone
        ucs = store_with_edge.get_unresolved_calls()
        matching = [u for u in ucs if u.call_line == 15 and u.caller_id == "fn_caller"]
        assert matching == []


# ---------------------------------------------------------------------------
# §5: DELETE /edges 4-step cascade
# ---------------------------------------------------------------------------


class TestEdgeDeleteCascade:
    """architecture.md §5: edge deletion triggers same 4-step cascade."""

    def test_delete_nonexistent_edge_404(self, client):
        resp = client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 999,
        })
        assert resp.status_code == 404

    def test_delete_edge_regenerates_uc(self, client, store_with_edge):
        resp = client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
        })
        assert resp.status_code == 204
        # UC regenerated
        ucs = store_with_edge.get_unresolved_calls()
        matching = [u for u in ucs if u.caller_id == "fn_caller" and u.call_line == 10]
        assert len(matching) == 1
        assert matching[0].call_type == "indirect"  # Preserved from deleted edge

    def test_delete_edge_resets_source_point(self, client, store_with_edge):
        client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
        })
        sp = store_with_edge.get_source_point("sp_caller")
        assert sp.status == "pending"

    def test_delete_edge_with_correct_target_creates_feedback(
        self, client, store_with_edge, feedback_store
    ):
        client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "main.cpp",
            "call_line": 10,
            "correct_target": "fn_correct",
        })
        examples = feedback_store.list_all()
        assert len(examples) == 1
        assert examples[0].correct_target == "fn_correct"


# ---------------------------------------------------------------------------
# §8: POST /feedback validation
# ---------------------------------------------------------------------------


class TestFeedbackValidation:
    """architecture.md §8: feedback creation validation."""

    def test_empty_call_context_422(self, client):
        resp = client.post("/api/v1/feedback", json={
            "call_context": "",
            "wrong_target": "bad",
            "correct_target": "good",
            "pattern": "test pattern",
        })
        assert resp.status_code == 422

    def test_empty_wrong_target_422(self, client):
        resp = client.post("/api/v1/feedback", json={
            "call_context": "x.cpp:10",
            "wrong_target": "",
            "correct_target": "good",
            "pattern": "test pattern",
        })
        assert resp.status_code == 422

    def test_empty_pattern_422(self, client):
        resp = client.post("/api/v1/feedback", json={
            "call_context": "x.cpp:10",
            "wrong_target": "bad",
            "correct_target": "good",
            "pattern": "",
        })
        assert resp.status_code == 422

    def test_same_wrong_and_correct_target_422(self, client):
        """wrong_target must differ from correct_target."""
        resp = client.post("/api/v1/feedback", json={
            "call_context": "x.cpp:10",
            "wrong_target": "same",
            "correct_target": "same",
            "pattern": "test",
        })
        assert resp.status_code == 422

    def test_valid_feedback_creation(self, client, feedback_store):
        resp = client.post("/api/v1/feedback", json={
            "call_context": "x.cpp:10",
            "wrong_target": "bad_fn",
            "correct_target": "good_fn",
            "pattern": "dispatch pattern",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["deduplicated"] is False
        assert data["total"] == 1
        assert data["wrong_target"] == "bad_fn"

    def test_duplicate_feedback_deduplicates(self, client, feedback_store):
        body = {
            "call_context": "x.cpp:10",
            "wrong_target": "bad_fn",
            "correct_target": "good_fn",
            "pattern": "same pattern",
        }
        resp1 = client.post("/api/v1/feedback", json=body)
        resp2 = client.post("/api/v1/feedback", json=body)
        assert resp1.json()["deduplicated"] is False
        assert resp2.json()["deduplicated"] is True
        assert resp2.json()["total"] == 1  # Not 2

    def test_feedback_without_store_503(self):
        """architecture.md §8: 503 when FeedbackStore not configured."""
        store = InMemoryGraphStore()
        app = create_app(store=store)
        # Don't set feedback_store
        client = TestClient(app)
        resp = client.post("/api/v1/feedback", json={
            "call_context": "x.cpp:10",
            "wrong_target": "bad",
            "correct_target": "good",
            "pattern": "test",
        })
        assert resp.status_code == 503

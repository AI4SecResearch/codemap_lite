"""Review workflow + feedback store integration tests — architecture.md §5.

Tests the full review cascade (verdict=incorrect → 4-step flow) and
counter-example generation + injection into repair prompts.
Uses InMemoryGraphStore + FastAPI TestClient.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store_with_llm_edge():
    """Store with two functions + an LLM edge + RepairLog + SourcePoint."""
    store = InMemoryGraphStore()
    caller = FunctionNode(
        id="caller_01", name="dispatch", signature="void dispatch()",
        file_path="src/dispatch.cpp", start_line=10, end_line=20, body_hash="aaa",
    )
    callee = FunctionNode(
        id="callee_01", name="handler", signature="void handler()",
        file_path="src/handler.cpp", start_line=5, end_line=15, body_hash="bbb",
    )
    store.create_function(caller)
    store.create_function(callee)

    # LLM edge
    props = CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="src/dispatch.cpp", call_line=15,
    )
    store.create_calls_edge("caller_01", "callee_01", props)

    # RepairLog for this edge
    log = RepairLogNode(
        id="log_01",
        caller_id="caller_01",
        callee_id="callee_01",
        call_location="src/dispatch.cpp:15",
        repair_method="llm",
        llm_response="resolved handler via vtable",
        timestamp="2026-01-01T00:00:00Z",
        reasoning_summary="vtable dispatch pattern",
    )
    store.create_repair_log(log)

    # SourcePoint for caller (status=complete)
    sp = SourcePointNode(
        id="sp_caller", function_id="caller_01",
        entry_point_kind="api", reason="entry point", status="pending",
    )
    store.create_source_point(sp)
    store.update_source_point_status("sp_caller", "running")
    store.update_source_point_status("sp_caller", "complete")

    return store


@pytest.fixture
def feedback_store(tmp_path: Path):
    return FeedbackStore(storage_dir=tmp_path / "feedback")


@pytest.fixture
def client(store_with_llm_edge, feedback_store):
    app = create_app(store=store_with_llm_edge, feedback_store=feedback_store)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Test: Review verdict=correct
# ---------------------------------------------------------------------------

class TestReviewCorrect:
    """architecture.md §5: verdict=correct → edge stays, review recorded."""

    def test_correct_verdict_preserves_edge(self, client, store_with_llm_edge):
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
            "verdict": "correct",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["verdict"] == "correct"
        assert "id" in data

        # Edge still exists
        edge = store_with_llm_edge.get_calls_edge(
            "caller_01", "callee_01", "src/dispatch.cpp", 15
        )
        assert edge is not None

    def test_correct_verdict_does_not_delete_repair_log(self, client, store_with_llm_edge):
        client.post("/api/v1/reviews", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
            "verdict": "correct",
        })
        logs = store_with_llm_edge.get_repair_logs()
        assert len(logs) >= 1


# ---------------------------------------------------------------------------
# Test: Review verdict=incorrect — 4-step cascade
# ---------------------------------------------------------------------------

class TestReviewIncorrect:
    """architecture.md §5: verdict=incorrect → delete edge + RepairLog + regen UC + reset SP."""

    def test_incorrect_deletes_edge(self, client, store_with_llm_edge):
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201

        # Edge should be gone
        edge = store_with_llm_edge.get_calls_edge(
            "caller_01", "callee_01", "src/dispatch.cpp", 15
        )
        assert edge is None

    def test_incorrect_deletes_repair_log(self, client, store_with_llm_edge):
        client.post("/api/v1/reviews", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
            "verdict": "incorrect",
        })
        logs = store_with_llm_edge.get_repair_logs()
        assert len(logs) == 0

    def test_incorrect_regenerates_uc(self, client, store_with_llm_edge):
        client.post("/api/v1/reviews", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
            "verdict": "incorrect",
        })
        # UC should be regenerated for the caller
        ucs = store_with_llm_edge.get_unresolved_calls(caller_id="caller_01")
        assert len(ucs) >= 1
        uc = ucs[0]
        assert uc.call_file == "src/dispatch.cpp"
        assert uc.call_line == 15
        assert uc.retry_count == 0
        assert uc.status == "pending"

    def test_incorrect_resets_source_point(self, client, store_with_llm_edge):
        client.post("/api/v1/reviews", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
            "verdict": "incorrect",
        })
        sp = store_with_llm_edge.get_source_point("sp_caller")
        assert sp.status == "pending"

    def test_incorrect_with_correct_target_creates_counter_example(
        self, client, store_with_llm_edge, feedback_store
    ):
        """architecture.md §5: correct_target → counter-example in FeedbackStore."""
        # Create the correct target function
        correct_fn = FunctionNode(
            id="correct_01", name="real_handler", signature="void real_handler()",
            file_path="src/real.cpp", start_line=1, end_line=10, body_hash="ccc",
        )
        store_with_llm_edge.create_function(correct_fn)

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
            "verdict": "incorrect",
            "correct_target": "correct_01",
        })
        assert resp.status_code == 201

        # Counter-example should be in feedback store
        examples = feedback_store.get_for_source("caller_01")
        assert len(examples) >= 1
        ex = examples[0]
        assert ex.wrong_target == "callee_01"
        assert ex.correct_target == "correct_01"

    def test_nonexistent_edge_returns_404(self, client):
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "nonexistent",
            "callee_id": "also_nonexistent",
            "call_file": "fake.cpp",
            "call_line": 1,
            "verdict": "incorrect",
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: Manual edge creation (POST /edges)
# ---------------------------------------------------------------------------

class TestManualEdgeCreate:
    """architecture.md §5: manual edge creation deletes matching UC."""

    def test_create_edge_success(self, client, store_with_llm_edge):
        # Create a third function to be the new callee
        fn3 = FunctionNode(
            id="fn_003", name="new_target", signature="void new_target()",
            file_path="src/new.cpp", start_line=1, end_line=5, body_hash="ddd",
        )
        store_with_llm_edge.create_function(fn3)

        resp = client.post("/api/v1/edges", json={
            "caller_id": "caller_01",
            "callee_id": "fn_003",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "src/dispatch.cpp",
            "call_line": 18,
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "created"

    def test_create_edge_deletes_matching_uc(self, client, store_with_llm_edge):
        """Creating an edge should delete the UC at the same call site."""
        # First create a UC
        uc = UnresolvedCallNode(
            id="uc_manual", caller_id="caller_01",
            call_expression="new_target()", call_file="src/dispatch.cpp",
            call_line=18, call_type="indirect", source_code_snippet="",
            var_name=None, var_type=None,
        )
        store_with_llm_edge.create_unresolved_call(uc)

        # Create a function for the callee
        fn3 = FunctionNode(
            id="fn_003", name="new_target", signature="void new_target()",
            file_path="src/new.cpp", start_line=1, end_line=5, body_hash="ddd",
        )
        store_with_llm_edge.create_function(fn3)

        resp = client.post("/api/v1/edges", json={
            "caller_id": "caller_01",
            "callee_id": "fn_003",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "src/dispatch.cpp",
            "call_line": 18,
        })
        assert resp.status_code == 201

        # UC should be deleted
        ucs = store_with_llm_edge.get_unresolved_calls(caller_id="caller_01")
        matching = [u for u in ucs if u.call_line == 18]
        assert len(matching) == 0

    def test_duplicate_edge_returns_409(self, client, store_with_llm_edge):
        """architecture.md §4: edge uniqueness enforced."""
        resp = client.post("/api/v1/edges", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
        })
        assert resp.status_code == 409

    def test_invalid_resolved_by_returns_422(self, client):
        resp = client.post("/api/v1/edges", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "resolved_by": "magic",
            "call_type": "direct",
            "call_file": "a.cpp",
            "call_line": 1,
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test: Manual edge deletion (DELETE /edges)
# ---------------------------------------------------------------------------

class TestManualEdgeDelete:
    """architecture.md §5: DELETE /edges → same 4-step cascade as review incorrect."""

    def test_delete_edge_regenerates_uc(self, client, store_with_llm_edge):
        resp = client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
        })
        assert resp.status_code == 204

        # UC regenerated
        ucs = store_with_llm_edge.get_unresolved_calls(caller_id="caller_01")
        assert len(ucs) >= 1

    def test_delete_nonexistent_edge_returns_404(self, client):
        resp = client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "nope",
            "callee_id": "nope",
            "call_file": "x.cpp",
            "call_line": 1,
        })
        assert resp.status_code == 404

    def test_delete_edge_with_correct_target_creates_counter_example(
        self, client, store_with_llm_edge, feedback_store
    ):
        resp = client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
            "correct_target": "real_handler_id",
        })
        assert resp.status_code == 204

        examples = feedback_store.get_for_source("caller_01")
        assert len(examples) >= 1
        assert examples[0].correct_target == "real_handler_id"


# ---------------------------------------------------------------------------
# Test: FeedbackStore counter-example deduplication
# ---------------------------------------------------------------------------

class TestFeedbackStoreDedup:
    """architecture.md §3 反馈机制: same pattern → deduplicated."""

    def test_same_pattern_deduplicated(self, tmp_path: Path):
        store = FeedbackStore(storage_dir=tmp_path / "fb")
        ex1 = CounterExample(
            call_context="a.cpp:10",
            wrong_target="wrong_fn",
            correct_target="right_fn",
            pattern="caller -> wrong_fn at a.cpp:10",
            source_id="src_001",
        )
        assert store.add(ex1) is True  # first time → new
        assert store.add(ex1) is False  # duplicate → deduplicated

    def test_different_patterns_both_stored(self, tmp_path: Path):
        store = FeedbackStore(storage_dir=tmp_path / "fb")
        ex1 = CounterExample(
            call_context="a.cpp:10",
            wrong_target="wrong_fn",
            correct_target="right_fn",
            pattern="pattern_A",
            source_id="src_001",
        )
        ex2 = CounterExample(
            call_context="b.cpp:20",
            wrong_target="other_wrong",
            correct_target="other_right",
            pattern="pattern_B",
            source_id="src_001",
        )
        assert store.add(ex1) is True
        assert store.add(ex2) is True
        assert len(store.get_for_source("src_001")) == 2

    def test_render_markdown_for_source(self, tmp_path: Path):
        """Counter-examples render to markdown for injection into CLAUDE.md."""
        store = FeedbackStore(storage_dir=tmp_path / "fb")
        ex = CounterExample(
            call_context="dispatch.cpp:15",
            wrong_target="wrong_handler",
            correct_target="real_handler",
            pattern="dispatch -> wrong_handler",
            source_id="src_001",
        )
        store.add(ex)
        md = store.render_markdown_for_source("src_001")
        assert "wrong_handler" in md
        assert "real_handler" in md

    def test_render_empty_source_returns_empty(self, tmp_path: Path):
        store = FeedbackStore(storage_dir=tmp_path / "fb")
        md = store.render_markdown_for_source("nonexistent")
        assert md == "" or "No counter" in md


# ---------------------------------------------------------------------------
# Test: GET /feedback endpoint
# ---------------------------------------------------------------------------

class TestFeedbackEndpoint:
    """architecture.md §8: GET /api/v1/feedback returns counter-examples."""

    def test_feedback_empty(self, client):
        resp = client.get("/api/v1/feedback")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_feedback_after_review_incorrect(self, client, store_with_llm_edge, feedback_store):
        # Create correct target
        fn3 = FunctionNode(
            id="correct_fn", name="correct", signature="void correct()",
            file_path="src/c.cpp", start_line=1, end_line=5, body_hash="eee",
        )
        store_with_llm_edge.create_function(fn3)

        # Submit incorrect review with correct_target
        client.post("/api/v1/reviews", json={
            "caller_id": "caller_01",
            "callee_id": "callee_01",
            "call_file": "src/dispatch.cpp",
            "call_line": 15,
            "verdict": "incorrect",
            "correct_target": "correct_fn",
        })

        resp = client.get("/api/v1/feedback")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        item = data["items"][0]
        assert item["wrong_target"] == "callee_01"
        assert item["correct_target"] == "correct_fn"

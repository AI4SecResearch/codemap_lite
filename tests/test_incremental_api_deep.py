"""Incremental cascade + API review deep-dive tests.

Tests complex scenarios in the 5-step incremental invalidation and the
4-step review cascade that are most likely to harbor bugs.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from codemap_lite.api.app import create_app
from codemap_lite.analysis.feedback_store import FeedbackStore
from codemap_lite.graph.incremental import IncrementalUpdater, InvalidationResult
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

def _fn(id: str, file_path: str = "a.cpp", start: int = 1) -> FunctionNode:
    return FunctionNode(
        id=id, name=id, signature=f"void {id}()",
        file_path=file_path, start_line=start, end_line=start + 5, body_hash="h",
    )


def _edge(resolved_by: str = "symbol_table", call_type: str = "direct",
           call_file: str = "a.cpp", call_line: int = 1) -> CallsEdgeProps:
    return CallsEdgeProps(
        resolved_by=resolved_by, call_type=call_type,
        call_file=call_file, call_line=call_line,
    )


# ---------------------------------------------------------------------------
# Test: Incremental cascade — cross-file LLM edge invalidation
# ---------------------------------------------------------------------------

class TestIncrementalCascadeLLM:
    """architecture.md §7: LLM edges to changed file → regenerate UC."""

    def test_cross_file_llm_edge_regenerates_uc(self):
        """Caller in file_a, callee in file_b (LLM edge).
        Invalidating file_b → UC regenerated for caller."""
        store = InMemoryGraphStore()
        store.create_function(_fn("caller", file_path="file_a.cpp"))
        store.create_function(_fn("callee", file_path="file_b.cpp"))
        store.create_calls_edge(
            "caller", "callee",
            _edge(resolved_by="llm", call_file="file_a.cpp", call_line=10)
        )
        # RepairLog for this edge
        store.create_repair_log(RepairLogNode(
            id="log1", caller_id="caller", callee_id="callee",
            call_location="file_a.cpp:10", repair_method="llm",
            llm_response="resolved", timestamp="t", reasoning_summary="r",
        ))

        updater = IncrementalUpdater(store)
        result = updater.invalidate_file("file_b.cpp")

        # Callee function deleted
        assert store.get_function_by_id("callee") is None
        # Edge deleted
        assert not store.edge_exists("caller", "callee", "file_a.cpp", 10)
        # RepairLog deleted
        assert len(store.get_repair_logs(caller_id="caller")) == 0
        # UC regenerated
        ucs = store.get_unresolved_calls(caller_id="caller")
        assert len(ucs) == 1
        assert ucs[0].call_file == "file_a.cpp"
        assert ucs[0].call_line == 10
        assert ucs[0].retry_count == 0
        assert ucs[0].status == "pending"
        # Caller is affected
        assert "caller" in result.affected_callers

    def test_cross_file_non_llm_edge_no_uc_regen(self):
        """Non-LLM cross-file edge: caller affected but NO UC regenerated."""
        store = InMemoryGraphStore()
        store.create_function(_fn("caller", file_path="file_a.cpp"))
        store.create_function(_fn("callee", file_path="file_b.cpp"))
        store.create_calls_edge(
            "caller", "callee",
            _edge(resolved_by="symbol_table", call_file="file_a.cpp", call_line=10)
        )

        updater = IncrementalUpdater(store)
        result = updater.invalidate_file("file_b.cpp")

        # Callee deleted, edge deleted
        assert store.get_function_by_id("callee") is None
        assert not store.edge_exists("caller", "callee", "file_a.cpp", 10)
        # Caller is affected (needs re-parse)
        assert "caller" in result.affected_callers
        # But NO UC regenerated (non-LLM edges are re-discovered by re-parse)
        ucs = store.get_unresolved_calls(caller_id="caller")
        assert len(ucs) == 0

    def test_same_file_llm_edge_regenerates_uc(self):
        """Both caller and callee in same file, LLM edge → NO UC regenerated.

        When both caller and callee are in the same file, the caller is being
        deleted and will be re-parsed. UC regeneration is skipped because the
        caller_id is in the set of deleted functions — re-parse will rebuild
        with a new function ID.
        """
        store = InMemoryGraphStore()
        store.create_function(_fn("caller", file_path="same.cpp", start=1))
        store.create_function(_fn("callee", file_path="same.cpp", start=20))
        store.create_calls_edge(
            "caller", "callee",
            _edge(resolved_by="llm", call_file="same.cpp", call_line=5)
        )

        updater = IncrementalUpdater(store)
        result = updater.invalidate_file("same.cpp")

        # Both functions deleted
        assert store.get_function_by_id("caller") is None
        assert store.get_function_by_id("callee") is None
        # NO UC regenerated — caller is deleted, re-parse handles it
        assert len(result.regenerated_unresolved_calls) == 0

    def test_source_point_reset_on_invalidation(self):
        """SourcePoint for affected caller is reset to 'pending'."""
        store = InMemoryGraphStore()
        store.create_function(_fn("caller", file_path="file_a.cpp"))
        store.create_function(_fn("callee", file_path="file_b.cpp"))
        store.create_calls_edge(
            "caller", "callee",
            _edge(resolved_by="llm", call_file="file_a.cpp", call_line=10)
        )
        # SourcePoint for caller, status=complete
        sp = SourcePointNode(
            id="sp_caller", function_id="caller",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp_caller", "running")
        store.update_source_point_status("sp_caller", "complete")

        updater = IncrementalUpdater(store)
        result = updater.invalidate_file("file_b.cpp")

        # SourcePoint reset to pending
        sp_after = store.get_source_point("sp_caller")
        assert sp_after.status == "pending"
        assert "caller" in result.affected_source_ids

    def test_no_functions_in_file_returns_empty_result(self):
        """Invalidating a file with no functions → empty result."""
        store = InMemoryGraphStore()
        store.create_function(_fn("other", file_path="other.cpp"))

        updater = IncrementalUpdater(store)
        result = updater.invalidate_file("empty.cpp")

        assert result.removed_functions == []
        assert result.removed_edges == 0
        assert result.affected_callers == []

    def test_uc_metadata_preserved_on_regen(self):
        """Regenerated UC preserves var_name/var_type from original."""
        store = InMemoryGraphStore()
        store.create_function(_fn("caller", file_path="file_a.cpp"))
        store.create_function(_fn("callee", file_path="file_b.cpp"))
        store.create_calls_edge(
            "caller", "callee",
            _edge(resolved_by="llm", call_file="file_a.cpp", call_line=10)
        )
        # Create a UC with metadata (simulating static analysis found it first)
        uc = UnresolvedCallNode(
            id="uc_orig", caller_id="caller", call_expression="fp()",
            call_file="file_a.cpp", call_line=10, call_type="indirect",
            source_code_snippet="code", var_name="fp", var_type="FuncPtr",
        )
        store.create_unresolved_call(uc)

        updater = IncrementalUpdater(store)
        result = updater.invalidate_file("file_b.cpp")

        # The regenerated UC should preserve var_name/var_type
        ucs = store.get_unresolved_calls(caller_id="caller")
        assert len(ucs) == 1
        assert ucs[0].var_name == "fp"
        assert ucs[0].var_type == "FuncPtr"


# ---------------------------------------------------------------------------
# Test: API review cascade edge cases
# ---------------------------------------------------------------------------

class TestReviewCascadeEdgeCases:
    """architecture.md §5: review cascade with tricky scenarios."""

    @pytest.fixture
    def store_and_client(self, tmp_path):
        """Store with caller+callee+LLM edge+RepairLog+SourcePoint."""
        store = InMemoryGraphStore()
        store.create_function(_fn("caller", file_path="src/a.cpp"))
        store.create_function(_fn("callee", file_path="src/b.cpp"))
        store.create_calls_edge(
            "caller", "callee",
            _edge(resolved_by="llm", call_type="indirect",
                  call_file="src/a.cpp", call_line=15)
        )
        store.create_repair_log(RepairLogNode(
            id="log1", caller_id="caller", callee_id="callee",
            call_location="src/a.cpp:15", repair_method="llm",
            llm_response="resolved", timestamp="t", reasoning_summary="r",
        ))
        sp = SourcePointNode(
            id="sp_caller", function_id="caller",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp_caller", "running")
        store.update_source_point_status("sp_caller", "complete")

        feedback_store = FeedbackStore(storage_dir=tmp_path / "fb")
        app = create_app(store=store, feedback_store=feedback_store)
        client = TestClient(app)
        return store, client, feedback_store

    def test_incorrect_review_full_cascade(self, store_and_client):
        """Full 4-step cascade: edge deleted, log deleted, UC regen, SP reset."""
        store, client, _ = store_and_client

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "caller",
            "callee_id": "callee",
            "call_file": "src/a.cpp",
            "call_line": 15,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201

        # Step 1: Edge deleted
        assert store.get_calls_edge("caller", "callee", "src/a.cpp", 15) is None
        # Step 2: RepairLog deleted
        assert len(store.get_repair_logs(caller_id="caller")) == 0
        # Step 3: UC regenerated
        ucs = store.get_unresolved_calls(caller_id="caller")
        assert len(ucs) == 1
        assert ucs[0].call_line == 15
        assert ucs[0].retry_count == 0
        assert ucs[0].status == "pending"
        # Step 4: SourcePoint reset
        sp = store.get_source_point("sp_caller")
        assert sp.status == "pending"

    def test_review_nonexistent_edge_returns_404(self, store_and_client):
        """Reviewing a non-existent edge returns 404."""
        _, client, _ = store_and_client

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "ghost",
            "callee_id": "ghost",
            "call_file": "x.cpp",
            "call_line": 1,
            "verdict": "incorrect",
        })
        assert resp.status_code == 404

    def test_review_correct_preserves_everything(self, store_and_client):
        """verdict=correct: edge, log, SP all unchanged."""
        store, client, _ = store_and_client

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "caller",
            "callee_id": "callee",
            "call_file": "src/a.cpp",
            "call_line": 15,
            "verdict": "correct",
        })
        assert resp.status_code == 201

        # Everything preserved
        assert store.get_calls_edge("caller", "callee", "src/a.cpp", 15) is not None
        assert len(store.get_repair_logs(caller_id="caller")) == 1
        sp = store.get_source_point("sp_caller")
        assert sp.status == "complete"

    def test_review_incorrect_with_counter_example(self, store_and_client):
        """verdict=incorrect + correct_target → counter-example stored."""
        store, client, feedback_store = store_and_client
        # Create the correct target function
        store.create_function(_fn("real_target", file_path="src/real.cpp"))

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "caller",
            "callee_id": "callee",
            "call_file": "src/a.cpp",
            "call_line": 15,
            "verdict": "incorrect",
            "correct_target": "real_target",
        })
        assert resp.status_code == 201

        # Counter-example stored
        examples = feedback_store.get_for_source("caller")
        assert len(examples) == 1
        assert examples[0].wrong_target == "callee"
        assert examples[0].correct_target == "real_target"

    def test_caller_without_source_point_no_crash(self, tmp_path):
        """Caller that is NOT a SourcePoint — cascade still works, no SP reset."""
        store = InMemoryGraphStore()
        store.create_function(_fn("caller", file_path="src/a.cpp"))
        store.create_function(_fn("callee", file_path="src/b.cpp"))
        store.create_calls_edge(
            "caller", "callee",
            _edge(resolved_by="llm", call_type="indirect",
                  call_file="src/a.cpp", call_line=15)
        )
        # NO SourcePoint for caller

        feedback_store = FeedbackStore(storage_dir=tmp_path / "fb")
        app = create_app(store=store, feedback_store=feedback_store)
        client = TestClient(app)

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "caller",
            "callee_id": "callee",
            "call_file": "src/a.cpp",
            "call_line": 15,
            "verdict": "incorrect",
        })
        # Should succeed without crash (no SP to reset)
        assert resp.status_code == 201
        # Edge still deleted, UC still regenerated
        assert store.get_calls_edge("caller", "callee", "src/a.cpp", 15) is None
        ucs = store.get_unresolved_calls(caller_id="caller")
        assert len(ucs) == 1

    def test_invalid_verdict_returns_422(self, store_and_client):
        """Invalid verdict value returns 422."""
        _, client, _ = store_and_client

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "caller",
            "callee_id": "callee",
            "call_file": "src/a.cpp",
            "call_line": 15,
            "verdict": "maybe",
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test: Manual edge creation/deletion edge cases
# ---------------------------------------------------------------------------

class TestManualEdgeEdgeCases:
    """architecture.md §5: POST/DELETE /edges edge cases."""

    @pytest.fixture
    def store_and_client(self, tmp_path):
        store = InMemoryGraphStore()
        store.create_function(_fn("fn_a", file_path="src/a.cpp"))
        store.create_function(_fn("fn_b", file_path="src/b.cpp"))
        feedback_store = FeedbackStore(storage_dir=tmp_path / "fb")
        app = create_app(store=store, feedback_store=feedback_store)
        client = TestClient(app)
        return store, client

    def test_create_edge_nonexistent_caller_404(self, store_and_client):
        _, client = store_and_client
        resp = client.post("/api/v1/edges", json={
            "caller_id": "ghost",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "src/a.cpp",
            "call_line": 1,
        })
        assert resp.status_code == 404
        assert "Caller" in resp.json()["detail"]

    def test_create_edge_nonexistent_callee_404(self, store_and_client):
        _, client = store_and_client
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "ghost",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "src/a.cpp",
            "call_line": 1,
        })
        assert resp.status_code == 404
        assert "Callee" in resp.json()["detail"]

    def test_create_edge_deletes_matching_uc(self, store_and_client):
        """Creating edge at same call site deletes the UC."""
        store, client = store_and_client
        # Pre-existing UC at line 10
        uc = UnresolvedCallNode(
            id="uc1", caller_id="fn_a", call_expression="fn_b()",
            call_file="src/a.cpp", call_line=10, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        )
        store.create_unresolved_call(uc)

        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "src/a.cpp",
            "call_line": 10,
        })
        assert resp.status_code == 201

        # UC deleted
        ucs = store.get_unresolved_calls(caller_id="fn_a")
        assert len(ucs) == 0

    def test_create_duplicate_edge_409(self, store_and_client):
        """Creating same edge twice returns 409."""
        store, client = store_and_client
        store.create_calls_edge("fn_a", "fn_b", _edge(
            resolved_by="llm", call_type="indirect",
            call_file="src/a.cpp", call_line=5,
        ))

        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "src/a.cpp",
            "call_line": 5,
        })
        assert resp.status_code == 409

    def test_delete_edge_regenerates_uc(self, store_and_client):
        """DELETE /edges regenerates UC at the deleted call site."""
        store, client = store_and_client
        store.create_calls_edge("fn_a", "fn_b", _edge(
            resolved_by="llm", call_type="indirect",
            call_file="src/a.cpp", call_line=5,
        ))

        resp = client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "call_file": "src/a.cpp",
            "call_line": 5,
        })
        assert resp.status_code == 204

        # UC regenerated
        ucs = store.get_unresolved_calls(caller_id="fn_a")
        assert len(ucs) == 1
        assert ucs[0].call_line == 5
        assert ucs[0].call_type == "indirect"  # preserved from edge

    def test_delete_nonexistent_edge_404(self, store_and_client):
        _, client = store_and_client
        resp = client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "call_file": "src/a.cpp",
            "call_line": 999,
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: get_source_point_by_function_id fallback
# ---------------------------------------------------------------------------

class TestSourcePointByFunctionIdFallback:
    """The fallback to id-based lookup when function_id doesn't match."""

    def test_primary_lookup_by_function_id(self):
        store = InMemoryGraphStore()
        sp = SourcePointNode(
            id="sp_001", function_id="fn_abc",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)

        result = store.get_source_point_by_function_id("fn_abc")
        assert result is not None
        assert result.id == "sp_001"

    def test_fallback_lookup_by_id(self):
        """When function_id doesn't match, falls back to id-based lookup."""
        store = InMemoryGraphStore()
        # SP where id == the function_id we'll query with
        sp = SourcePointNode(
            id="fn_xyz", function_id="different_fn",
            entry_point_kind="api", reason="entry", status="pending",
        )
        store.create_source_point(sp)

        # Query by "fn_xyz" — function_id is "different_fn" so primary fails,
        # but id is "fn_xyz" so fallback succeeds
        result = store.get_source_point_by_function_id("fn_xyz")
        assert result is not None
        assert result.id == "fn_xyz"

    def test_no_match_returns_none(self):
        store = InMemoryGraphStore()
        result = store.get_source_point_by_function_id("nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# Test: Incremental cascade with multiple LLM edges to same callee
# ---------------------------------------------------------------------------

class TestMultiEdgeInvalidation:
    """Multiple callers with LLM edges to same callee in changed file."""

    def test_multiple_callers_all_get_uc_regen(self):
        """Two callers with LLM edges to same callee → both get UC regen."""
        store = InMemoryGraphStore()
        store.create_function(_fn("caller_a", file_path="a.cpp"))
        store.create_function(_fn("caller_b", file_path="b.cpp"))
        store.create_function(_fn("callee", file_path="target.cpp"))

        store.create_calls_edge(
            "caller_a", "callee",
            _edge(resolved_by="llm", call_file="a.cpp", call_line=10)
        )
        store.create_calls_edge(
            "caller_b", "callee",
            _edge(resolved_by="llm", call_file="b.cpp", call_line=20)
        )

        updater = IncrementalUpdater(store)
        result = updater.invalidate_file("target.cpp")

        # Both callers affected
        assert set(result.affected_callers) == {"caller_a", "caller_b"}
        # Both get UC regenerated
        ucs_a = store.get_unresolved_calls(caller_id="caller_a")
        ucs_b = store.get_unresolved_calls(caller_id="caller_b")
        assert len(ucs_a) == 1
        assert len(ucs_b) == 1
        assert ucs_a[0].call_line == 10
        assert ucs_b[0].call_line == 20

    def test_mixed_llm_and_static_edges(self):
        """One LLM edge + one static edge to same callee.
        Only LLM edge gets UC regen."""
        store = InMemoryGraphStore()
        store.create_function(_fn("caller_llm", file_path="a.cpp"))
        store.create_function(_fn("caller_static", file_path="b.cpp"))
        store.create_function(_fn("callee", file_path="target.cpp"))

        store.create_calls_edge(
            "caller_llm", "callee",
            _edge(resolved_by="llm", call_file="a.cpp", call_line=10)
        )
        store.create_calls_edge(
            "caller_static", "callee",
            _edge(resolved_by="symbol_table", call_file="b.cpp", call_line=20)
        )

        updater = IncrementalUpdater(store)
        result = updater.invalidate_file("target.cpp")

        # Both callers affected
        assert "caller_llm" in result.affected_callers
        assert "caller_static" in result.affected_callers
        # Only LLM caller gets UC regen
        ucs_llm = store.get_unresolved_calls(caller_id="caller_llm")
        ucs_static = store.get_unresolved_calls(caller_id="caller_static")
        assert len(ucs_llm) == 1
        assert len(ucs_static) == 0

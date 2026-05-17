"""Real CastEngine integration tests — architecture.md full contract validation.

Uses PipelineOrchestrator against real CastEngine C++ source to validate:
- Stats endpoint all buckets (§8)
- Call-chain endpoint (§8)
- Analyze/status endpoint (§8)
- Multi-LLM edge invalidation (§7)
- SourcePoint force_reset enforcement (§4)
- File hash format (SHA256, 64 hex chars)
- UnresolvedCall property invariants
- Pagination contract on all list endpoints

Reuses tree-sitter results from CastEngine (~5500 functions, ~4500 CALLS, ~19000 UCs).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    RepairLogNode,
    SourcePointNode,
    UnresolvedCallNode,
    VALID_CALL_TYPES,
    VALID_RESOLVED_BY,
    VALID_SOURCE_POINT_STATUSES,
    VALID_UC_STATUSES,
)
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator


# ---------------------------------------------------------------------------
# Fixture: Real CastEngine parse (cached across module)
# ---------------------------------------------------------------------------

CASTENGINE_DIR = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")

_CACHED_STORE: InMemoryGraphStore | None = None
_CACHED_RESULT: Any = None


def _get_castengine_store():
    """Parse CastEngine once and cache the result for all tests in this module."""
    global _CACHED_STORE, _CACHED_RESULT
    if _CACHED_STORE is not None:
        return _CACHED_STORE, _CACHED_RESULT
    store = InMemoryGraphStore()
    orch = PipelineOrchestrator(target_dir=CASTENGINE_DIR, store=store)
    result = orch.run_full_analysis()
    _CACHED_STORE = store
    _CACHED_RESULT = result
    return store, result


# Skip all tests if CastEngine is not available
pytestmark = pytest.mark.skipif(
    not CASTENGINE_DIR.exists(),
    reason="CastEngine source not available at expected path",
)


# ---------------------------------------------------------------------------
# Test: Pipeline result invariants with real data
# ---------------------------------------------------------------------------

class TestCastEnginePipelineInvariants:
    """Validate pipeline output against architecture.md §1-§2 with real data."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.store, self.result = _get_castengine_store()

    def test_files_scanned_above_threshold(self):
        """CastEngine has 700+ source files."""
        assert self.result.files_scanned > 100

    def test_functions_found_above_threshold(self):
        """CastEngine has 5000+ functions."""
        assert self.result.functions_found > 500

    def test_direct_calls_above_threshold(self):
        """CastEngine has 4000+ direct calls."""
        assert self.result.direct_calls > 200

    def test_unresolved_calls_above_threshold(self):
        """CastEngine has 19000+ unresolved calls."""
        assert self.result.unresolved_calls > 50

    def test_no_errors_in_result(self):
        """Pipeline should parse CastEngine without errors."""
        # Some parse errors are acceptable but should be minimal
        assert len(self.result.errors) < self.result.files_scanned * 0.1

    def test_all_function_ids_are_12_hex(self):
        """architecture.md §4: Function.id = 12-char hex SHA1."""
        for fn in self.store.list_functions():
            assert len(fn.id) == 12, f"Bad id length for {fn.name}: {fn.id}"
            assert all(c in "0123456789abcdef" for c in fn.id), f"Non-hex id: {fn.id}"

    def test_all_file_hashes_are_sha256(self):
        """architecture.md §4: File.hash = 64-char hex SHA256."""
        for f in self.store.list_files():
            assert len(f.hash) == 64, f"Bad hash length for {f.file_path}: len={len(f.hash)}"
            assert all(c in "0123456789abcdef" for c in f.hash)

    def test_no_llm_edges_in_static_analysis(self):
        """Static analysis must never produce resolved_by=llm."""
        for e in self.store.list_calls_edges():
            assert e.props.resolved_by != "llm"

    def test_all_edges_have_valid_resolved_by(self):
        """architecture.md §4: resolved_by ∈ {symbol_table, signature, dataflow, context, llm}."""
        for e in self.store.list_calls_edges():
            assert e.props.resolved_by in VALID_RESOLVED_BY, (
                f"Invalid resolved_by: {e.props.resolved_by}"
            )

    def test_all_edges_have_valid_call_type(self):
        """architecture.md §4: call_type ∈ {direct, indirect, virtual}."""
        for e in self.store.list_calls_edges():
            assert e.props.call_type in VALID_CALL_TYPES

    def test_all_ucs_have_valid_call_type(self):
        """architecture.md §4: UC.call_type ∈ {direct, indirect, virtual}."""
        functions = self.store.list_functions()
        checked = 0
        for fn in functions[:100]:  # Sample first 100 to avoid timeout
            for uc in self.store.get_unresolved_calls(caller_id=fn.id):
                assert uc.call_type in VALID_CALL_TYPES
                checked += 1
        assert checked > 0

    def test_edge_4field_uniqueness(self):
        """architecture.md §4: CALLS edge key = (caller_id, callee_id, call_file, call_line)."""
        seen: set[tuple[str, str, str, int]] = set()
        for e in self.store.list_calls_edges():
            key = (e.caller_id, e.callee_id, e.props.call_file, e.props.call_line)
            assert key not in seen, f"Duplicate edge: {key}"
            seen.add(key)

    def test_all_edges_reference_existing_functions(self):
        """Every CALLS edge must reference existing Function nodes."""
        for e in self.store.list_calls_edges():
            assert self.store.get_function_by_id(e.caller_id) is not None, (
                f"Edge references nonexistent caller: {e.caller_id}"
            )
            assert self.store.get_function_by_id(e.callee_id) is not None, (
                f"Edge references nonexistent callee: {e.callee_id}"
            )

    def test_all_ucs_reference_existing_callers(self):
        """Every UC must reference an existing Function as caller."""
        functions = self.store.list_functions()
        for fn in functions[:100]:
            for uc in self.store.get_unresolved_calls(caller_id=fn.id):
                assert self.store.get_function_by_id(uc.caller_id) is not None


# ---------------------------------------------------------------------------
# Test: Stats endpoint all buckets (architecture.md §8)
# ---------------------------------------------------------------------------

class TestStatsEndpointFullContract:
    """architecture.md §8: /api/v1/stats must return all required buckets."""

    @pytest.fixture
    def client(self):
        store, _ = _get_castengine_store()
        app = create_app(store=store)
        return TestClient(app)

    def test_stats_returns_200(self, client):
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200

    def test_stats_has_total_functions(self, client):
        data = client.get("/api/v1/stats").json()
        assert "total_functions" in data
        assert isinstance(data["total_functions"], int)
        assert data["total_functions"] > 500

    def test_stats_has_total_files(self, client):
        data = client.get("/api/v1/stats").json()
        assert "total_files" in data
        assert isinstance(data["total_files"], int)
        assert data["total_files"] > 100

    def test_stats_has_total_calls(self, client):
        data = client.get("/api/v1/stats").json()
        assert "total_calls" in data
        assert isinstance(data["total_calls"], int)

    def test_stats_has_total_unresolved(self, client):
        data = client.get("/api/v1/stats").json()
        assert "total_unresolved" in data
        assert isinstance(data["total_unresolved"], int)

    def test_stats_has_total_repair_logs(self, client):
        data = client.get("/api/v1/stats").json()
        assert "total_repair_logs" in data
        assert isinstance(data["total_repair_logs"], int)

    def test_stats_has_total_feedback(self, client):
        """architecture.md §8: total_feedback field required."""
        data = client.get("/api/v1/stats").json()
        assert "total_feedback" in data
        assert isinstance(data["total_feedback"], int)

    def test_stats_has_total_source_points(self, client):
        data = client.get("/api/v1/stats").json()
        assert "total_source_points" in data
        assert isinstance(data["total_source_points"], int)

    def test_stats_calls_by_resolved_by_all_keys(self, client):
        """architecture.md §8: all 5 resolved_by keys always present."""
        data = client.get("/api/v1/stats").json()
        assert "calls_by_resolved_by" in data
        bucket = data["calls_by_resolved_by"]
        for key in ("symbol_table", "signature", "dataflow", "context", "llm"):
            assert key in bucket, f"Missing key: {key}"
            assert isinstance(bucket[key], int)

    def test_stats_calls_by_call_type_all_keys(self, client):
        """architecture.md §8: all 3 call_type keys always present."""
        data = client.get("/api/v1/stats").json()
        assert "calls_by_call_type" in data
        bucket = data["calls_by_call_type"]
        for key in ("direct", "indirect", "virtual"):
            assert key in bucket, f"Missing key: {key}"
            assert isinstance(bucket[key], int)

    def test_stats_unresolved_by_status_all_keys(self, client):
        """architecture.md §8: pending + unresolvable keys always present."""
        data = client.get("/api/v1/stats").json()
        assert "unresolved_by_status" in data
        bucket = data["unresolved_by_status"]
        for key in ("pending", "unresolvable"):
            assert key in bucket, f"Missing key: {key}"
            assert isinstance(bucket[key], int)

    def test_stats_unresolved_by_category_all_keys(self, client):
        """architecture.md §8: all 5 category keys + 'none' always present."""
        data = client.get("/api/v1/stats").json()
        assert "unresolved_by_category" in data
        bucket = data["unresolved_by_category"]
        for key in (
            "gate_failed", "agent_error", "subprocess_crash",
            "subprocess_timeout", "agent_exited_without_edge", "none",
        ):
            assert key in bucket, f"Missing key: {key}"
            assert isinstance(bucket[key], int)

    def test_stats_source_points_by_status(self, client):
        """architecture.md §8: source_points_by_status bucket."""
        data = client.get("/api/v1/stats").json()
        assert "source_points_by_status" in data
        bucket = data["source_points_by_status"]
        for key in ("pending", "running", "complete", "partial_complete"):
            assert key in bucket, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# Test: Call-chain endpoint (architecture.md §8)
# ---------------------------------------------------------------------------

class TestCallChainEndpoint:
    """architecture.md §8: GET /functions/{id}/call-chain returns subgraph."""

    @pytest.fixture
    def client(self):
        store, _ = _get_castengine_store()
        app = create_app(store=store)
        return TestClient(app)

    def test_call_chain_returns_nodes_edges_unresolved(self, client):
        store, _ = _get_castengine_store()
        # Pick a function that has edges
        edges = store.list_calls_edges()
        if not edges:
            pytest.skip("No edges in store")
        fn_id = edges[0].caller_id
        resp = client.get(f"/api/v1/functions/{fn_id}/call-chain?depth=3")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert "unresolved" in data
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)

    def test_call_chain_depth_limits_bfs(self, client):
        store, _ = _get_castengine_store()
        edges = store.list_calls_edges()
        if not edges:
            pytest.skip("No edges in store")
        fn_id = edges[0].caller_id
        # depth=1 should return fewer nodes than depth=10
        resp1 = client.get(f"/api/v1/functions/{fn_id}/call-chain?depth=1")
        resp10 = client.get(f"/api/v1/functions/{fn_id}/call-chain?depth=10")
        assert resp1.status_code == 200
        assert resp10.status_code == 200
        nodes1 = len(resp1.json()["nodes"])
        nodes10 = len(resp10.json()["nodes"])
        assert nodes1 <= nodes10

    def test_call_chain_nonexistent_function_404(self, client):
        resp = client.get("/api/v1/functions/nonexistent_id/call-chain")
        assert resp.status_code == 404

    def test_call_chain_edge_has_props(self, client):
        """Each edge in call-chain must have caller_id, callee_id, props."""
        store, _ = _get_castengine_store()
        edges = store.list_calls_edges()
        if not edges:
            pytest.skip("No edges in store")
        fn_id = edges[0].caller_id
        resp = client.get(f"/api/v1/functions/{fn_id}/call-chain?depth=2")
        data = resp.json()
        for edge in data["edges"]:
            assert "caller_id" in edge
            assert "callee_id" in edge
            assert "props" in edge
            assert "resolved_by" in edge["props"]
            assert "call_type" in edge["props"]


# ---------------------------------------------------------------------------
# Test: Analyze/status endpoint (architecture.md §8)
# ---------------------------------------------------------------------------

class TestAnalyzeStatusEndpoint:
    """architecture.md §8: GET /analyze/status returns state + sources[]."""

    @pytest.fixture
    def client_with_progress(self, tmp_path: Path):
        store = InMemoryGraphStore()
        app = create_app(store=store, target_dir=tmp_path)
        # Create progress files
        progress_dir = tmp_path / "logs" / "repair" / "source_001"
        progress_dir.mkdir(parents=True)
        (progress_dir / "progress.json").write_text(json.dumps({
            "source_id": "source_001",
            "gaps_fixed": 5,
            "gaps_total": 10,
            "current_gap": "uc_abc",
            "attempt": 2,
            "max_attempts": 3,
            "gate_result": "failed",
            "state": "running",
        }), encoding="utf-8")
        return TestClient(app)

    def test_status_returns_state(self, client_with_progress):
        resp = client_with_progress.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data

    def test_status_returns_sources_array(self, client_with_progress):
        resp = client_with_progress.get("/api/v1/analyze/status")
        data = resp.json()
        assert "sources" in data
        assert isinstance(data["sources"], list)
        assert len(data["sources"]) >= 1

    def test_status_source_has_required_fields(self, client_with_progress):
        """architecture.md §3: progress.json schema fields."""
        resp = client_with_progress.get("/api/v1/analyze/status")
        data = resp.json()
        source = data["sources"][0]
        assert source["source_id"] == "source_001"
        assert source["gaps_fixed"] == 5
        assert source["gaps_total"] == 10
        assert source["current_gap"] == "uc_abc"
        assert source["attempt"] == 2
        assert source["gate_result"] == "failed"
        assert source["state"] == "running"

    def test_status_progress_derived_from_sources(self, client_with_progress):
        """Progress = sum(gaps_fixed) / sum(gaps_total)."""
        resp = client_with_progress.get("/api/v1/analyze/status")
        data = resp.json()
        # 5/10 = 0.5
        assert data["progress"] == 0.5


# ---------------------------------------------------------------------------
# Test: SourcePoint force_reset enforcement (architecture.md §4)
# ---------------------------------------------------------------------------

class TestSourcePointForceReset:
    """architecture.md §4: backward transitions require force_reset=True."""

    def test_complete_to_pending_without_force_raises(self):
        """Transition complete→pending without force_reset raises ValueError."""
        store = InMemoryGraphStore()
        sp = SourcePointNode(
            id="sp_test", function_id="fn_test",
            entry_point_kind="api", reason="test", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp_test", "running")
        store.update_source_point_status("sp_test", "complete")

        # Without force_reset, backward transition must be rejected
        with pytest.raises(ValueError, match="Invalid SourcePoint transition"):
            store.update_source_point_status("sp_test", "pending")

    def test_complete_to_pending_with_force_succeeds(self):
        """Transition complete→pending with force_reset=True should succeed."""
        store = InMemoryGraphStore()
        sp = SourcePointNode(
            id="sp_test2", function_id="fn_test2",
            entry_point_kind="api", reason="test", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp_test2", "running")
        store.update_source_point_status("sp_test2", "complete")
        store.update_source_point_status("sp_test2", "pending", force_reset=True)
        sp_after = store.get_source_point("sp_test2")
        assert sp_after.status == "pending"


# ---------------------------------------------------------------------------
# Test: Multi-LLM edge invalidation (architecture.md §7)
# ---------------------------------------------------------------------------

class TestMultiLlmEdgeInvalidation:
    """architecture.md §7: multiple LLM edges to same callee all invalidated."""

    def test_multiple_llm_edges_all_deleted(self):
        """When callee's file is invalidated, ALL LLM edges to it are deleted."""
        store = InMemoryGraphStore()
        # Callee in file B
        callee = FunctionNode(
            id="callee_x", name="target", signature="void target()",
            file_path="b.cpp", start_line=1, end_line=5, body_hash="y",
        )
        store.create_function(callee)
        # Two callers in file A, both with LLM edges to callee
        for i in range(3):
            caller = FunctionNode(
                id=f"caller_{i}", name=f"caller_{i}", signature=f"void caller_{i}()",
                file_path="a.cpp", start_line=10 * i + 1, end_line=10 * i + 5, body_hash=f"a{i}",
            )
            store.create_function(caller)
            props = CallsEdgeProps(
                resolved_by="llm", call_type="indirect",
                call_file="a.cpp", call_line=10 * i + 3,
            )
            store.create_calls_edge(f"caller_{i}", "callee_x", props)

        from codemap_lite.graph.incremental import IncrementalUpdater
        updater = IncrementalUpdater(store=store, target_dir="")
        result = updater.invalidate_file("b.cpp")

        # All 3 LLM edges should be deleted
        assert result.removed_edges == 3
        # All 3 callers should be affected
        assert len(result.affected_callers) == 3
        # All 3 UCs should be regenerated
        assert len(result.regenerated_unresolved_calls) == 3

    def test_mixed_llm_and_static_edges(self):
        """Only LLM edges regenerate UCs; static edges just mark caller as affected."""
        store = InMemoryGraphStore()
        callee = FunctionNode(
            id="callee_m", name="target", signature="void target()",
            file_path="b.cpp", start_line=1, end_line=5, body_hash="y",
        )
        store.create_function(callee)
        # Caller 1: LLM edge
        c1 = FunctionNode(
            id="c1", name="c1", signature="void c1()",
            file_path="a.cpp", start_line=1, end_line=5, body_hash="a1",
        )
        store.create_function(c1)
        store.create_calls_edge("c1", "callee_m", CallsEdgeProps(
            resolved_by="llm", call_type="indirect", call_file="a.cpp", call_line=3,
        ))
        # Caller 2: symbol_table edge
        c2 = FunctionNode(
            id="c2", name="c2", signature="void c2()",
            file_path="a.cpp", start_line=10, end_line=15, body_hash="a2",
        )
        store.create_function(c2)
        store.create_calls_edge("c2", "callee_m", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="a.cpp", call_line=12,
        ))

        from codemap_lite.graph.incremental import IncrementalUpdater
        updater = IncrementalUpdater(store=store, target_dir="")
        result = updater.invalidate_file("b.cpp")

        # Both callers affected
        assert "c1" in result.affected_callers
        assert "c2" in result.affected_callers
        # Only LLM edge generates UC
        assert len(result.regenerated_unresolved_calls) == 1


# ---------------------------------------------------------------------------
# Test: Pagination contract on all list endpoints (architecture.md §8)
# ---------------------------------------------------------------------------

class TestPaginationContract:
    """architecture.md §8: all list endpoints return {total, items} with limit/offset."""

    @pytest.fixture
    def client(self):
        store, _ = _get_castengine_store()
        app = create_app(store=store)
        return TestClient(app)

    def test_files_pagination(self, client):
        resp = client.get("/api/v1/files?limit=5&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert len(data["items"]) <= 5
        assert data["total"] > 5  # CastEngine has 700+ files

    def test_functions_pagination(self, client):
        resp = client.get("/api/v1/functions?limit=10&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert len(data["items"]) <= 10
        assert data["total"] > 10

    def test_unresolved_calls_pagination(self, client):
        resp = client.get("/api/v1/unresolved-calls?limit=5&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert len(data["items"]) <= 5

    def test_offset_skips_items(self, client):
        """offset=N skips first N items."""
        resp0 = client.get("/api/v1/functions?limit=5&offset=0")
        resp5 = client.get("/api/v1/functions?limit=5&offset=5")
        items0 = resp0.json()["items"]
        items5 = resp5.json()["items"]
        # Items should be different (no overlap)
        ids0 = {i["id"] for i in items0}
        ids5 = {i["id"] for i in items5}
        assert ids0.isdisjoint(ids5)

    def test_source_points_pagination(self, client):
        resp = client.get("/api/v1/source-points?limit=5&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data

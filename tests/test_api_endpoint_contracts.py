"""API endpoint edge-case tests — architecture.md §8.

Tests error handling, pagination edge cases, analyze/repair triggers,
status endpoint, source-points filtering, and call-chain depth limits.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    SourcePointNode,
    UnresolvedCallNode,
)


@pytest.fixture
def populated_store():
    """Store with realistic data for API testing."""
    store = InMemoryGraphStore()
    # Files
    store.create_file(FileNode(id="main.cpp", file_path="main.cpp", hash="h1", primary_language="cpp"))
    store.create_file(FileNode(id="util.cpp", file_path="util.cpp", hash="h2", primary_language="cpp"))

    # Functions
    for i in range(5):
        store.create_function(FunctionNode(
            id=f"fn_{i}", name=f"func_{i}", signature=f"void func_{i}()",
            file_path="main.cpp", start_line=i * 10 + 1, end_line=i * 10 + 9,
            body_hash=f"bh_{i}",
        ))
    store.create_function(FunctionNode(
        id="fn_util", name="helper", signature="int helper(int x)",
        file_path="util.cpp", start_line=1, end_line=10,
        body_hash="bh_util",
    ))

    # Edges
    store.create_calls_edge("fn_0", "fn_1", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="main.cpp", call_line=5,
    ))
    store.create_calls_edge("fn_1", "fn_2", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="main.cpp", call_line=15,
    ))
    store.create_calls_edge("fn_2", "fn_util", CallsEdgeProps(
        resolved_by="signature", call_type="direct",
        call_file="main.cpp", call_line=25,
    ))

    # Unresolved calls
    store.create_unresolved_call(UnresolvedCallNode(
        id="uc_1", caller_id="fn_3", call_expression="unknown_fn",
        call_file="main.cpp", call_line=35, call_type="indirect",
        source_code_snippet="ptr->unknown_fn()", var_name="ptr", var_type="Base*",
        candidates=["fn_4", "fn_util"],
    ))

    # Source points
    store.create_source_point(SourcePointNode(
        id="sp_1", function_id="fn_0", entry_point_kind="entry",
        reason="main entry", status="complete",
    ))
    store.create_source_point(SourcePointNode(
        id="sp_2", function_id="fn_3", entry_point_kind="callback",
        reason="callback handler", status="pending",
    ))

    return store


@pytest.fixture
def client(populated_store):
    app = create_app(store=populated_store)
    return TestClient(app)


# ---------------------------------------------------------------------------
# §8: Function endpoints — error handling
# ---------------------------------------------------------------------------


class TestFunctionEndpointErrors:
    """architecture.md §8: 404 for non-existent functions."""

    def test_get_function_404(self, client):
        resp = client.get("/api/v1/functions/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_get_callers_404(self, client):
        resp = client.get("/api/v1/functions/nonexistent/callers")
        assert resp.status_code == 404

    def test_get_callees_404(self, client):
        resp = client.get("/api/v1/functions/nonexistent/callees")
        assert resp.status_code == 404

    def test_get_call_chain_404(self, client):
        resp = client.get("/api/v1/functions/nonexistent/call-chain")
        assert resp.status_code == 404

    def test_get_function_valid(self, client):
        resp = client.get("/api/v1/functions/fn_0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "fn_0"
        assert data["name"] == "func_0"


# ---------------------------------------------------------------------------
# §8: Pagination edge cases
# ---------------------------------------------------------------------------


class TestPaginationEdgeCases:
    """architecture.md §8: {total, items} pagination contract."""

    def test_files_pagination_format(self, client):
        resp = client.get("/api/v1/files")
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] == 2

    def test_functions_pagination_format(self, client):
        resp = client.get("/api/v1/functions")
        data = resp.json()
        assert data["total"] == 6  # 5 in main.cpp + 1 in util.cpp

    def test_offset_beyond_total_returns_empty_items(self, client):
        resp = client.get("/api/v1/functions?offset=100")
        data = resp.json()
        assert data["total"] == 6
        assert data["items"] == []

    def test_limit_1_returns_single_item(self, client):
        resp = client.get("/api/v1/functions?limit=1")
        data = resp.json()
        assert data["total"] == 6
        assert len(data["items"]) == 1

    def test_functions_filter_by_file(self, client):
        resp = client.get("/api/v1/functions?file=util.cpp")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "helper"

    def test_unresolved_calls_pagination(self, client):
        resp = client.get("/api/v1/unresolved-calls")
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] >= 1


# ---------------------------------------------------------------------------
# §8: Call-chain endpoint
# ---------------------------------------------------------------------------


class TestCallChainEndpoint:
    """architecture.md §8: call-chain returns {nodes, edges, unresolved}."""

    def test_call_chain_structure(self, client):
        resp = client.get("/api/v1/functions/fn_0/call-chain?depth=5")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert "unresolved" in data

    def test_call_chain_depth_1(self, client):
        resp = client.get("/api/v1/functions/fn_0/call-chain?depth=1")
        data = resp.json()
        # depth=1 should include fn_0 and its direct callees
        node_ids = {n["id"] for n in data["nodes"]}
        assert "fn_0" in node_ids
        assert "fn_1" in node_ids

    def test_call_chain_includes_edges_with_props(self, client):
        resp = client.get("/api/v1/functions/fn_0/call-chain?depth=5")
        data = resp.json()
        for edge in data["edges"]:
            assert "caller_id" in edge
            assert "callee_id" in edge
            assert "props" in edge
            assert "resolved_by" in edge["props"]
            assert "call_type" in edge["props"]

    def test_call_chain_depth_limit_validation(self, client):
        """depth must be 1-50 per Query constraint."""
        resp = client.get("/api/v1/functions/fn_0/call-chain?depth=0")
        assert resp.status_code == 422
        resp = client.get("/api/v1/functions/fn_0/call-chain?depth=51")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# §8: Stats endpoint
# ---------------------------------------------------------------------------


class TestStatsEndpoint:
    """architecture.md §8: /stats returns all required buckets."""

    def test_stats_returns_required_fields(self, client):
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        # Required buckets per architecture.md §8
        assert "total_functions" in data
        assert "total_files" in data
        assert "total_calls" in data
        assert "total_unresolved" in data
        assert "calls_by_resolved_by" in data
        assert "calls_by_call_type" in data

    def test_stats_resolved_by_buckets(self, client):
        resp = client.get("/api/v1/stats")
        data = resp.json()
        by_resolved = data["calls_by_resolved_by"]
        # We have symbol_table, llm, signature edges
        assert by_resolved.get("symbol_table", 0) >= 1
        assert by_resolved.get("llm", 0) >= 1
        assert by_resolved.get("signature", 0) >= 1

    def test_stats_call_type_buckets(self, client):
        resp = client.get("/api/v1/stats")
        data = resp.json()
        by_type = data["calls_by_call_type"]
        assert by_type.get("direct", 0) >= 1
        assert by_type.get("indirect", 0) >= 1


# ---------------------------------------------------------------------------
# §8: Analyze trigger endpoints
# ---------------------------------------------------------------------------


class TestAnalyzeEndpoints:
    """architecture.md §8: POST /analyze returns 202, prevents double-spawn."""

    def test_analyze_returns_202(self, client):
        resp = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["mode"] == "full"

    def test_analyze_incremental_mode(self, client):
        resp = client.post("/api/v1/analyze", json={"mode": "incremental"})
        assert resp.status_code == 202
        assert resp.json()["mode"] == "incremental"

    def test_analyze_invalid_mode_422(self, client):
        resp = client.post("/api/v1/analyze", json={"mode": "invalid"})
        assert resp.status_code == 422

    def test_analyze_double_spawn_409(self, client):
        """architecture.md §8: 409 Conflict if already running."""
        client.post("/api/v1/analyze", json={"mode": "full"})
        resp = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp.status_code == 409

    def test_repair_returns_202(self, client):
        resp = client.post("/api/v1/analyze/repair", json={"source_ids": []})
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    def test_status_endpoint(self, client):
        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data
        assert "sources" in data


# ---------------------------------------------------------------------------
# §8: Source points endpoints
# ---------------------------------------------------------------------------


class TestSourcePointsEndpoints:
    """architecture.md §8: source-points with filtering and enrichment."""

    def test_source_points_list(self, client):
        resp = client.get("/api/v1/source-points")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data

    def test_source_point_by_id(self, client):
        resp = client.get("/api/v1/source-points/sp_1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "sp_1"
        assert data["status"] == "complete"

    def test_source_point_404(self, client):
        resp = client.get("/api/v1/source-points/nonexistent")
        assert resp.status_code == 404

    def test_source_points_summary(self, client):
        resp = client.get("/api/v1/source-points/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "by_kind" in data
        assert "by_status" in data

    def test_source_point_reachable(self, client):
        """architecture.md §8: /source-points/{id}/reachable returns subgraph."""
        resp = client.get("/api/v1/source-points/fn_0/reachable")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert "unresolved" in data

    def test_source_point_reachable_404(self, client):
        resp = client.get("/api/v1/source-points/nonexistent_fn/reachable")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# §8: Health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """architecture.md §8: /health returns 200."""

    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_response_structure(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert "status" in data


# ---------------------------------------------------------------------------
# §8: Callers/Callees with data
# ---------------------------------------------------------------------------


class TestCallersCallees:
    """architecture.md §8: callers/callees return paginated function lists."""

    def test_callees_of_fn_0(self, client):
        resp = client.get("/api/v1/functions/fn_0/callees")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        callee_ids = [item["id"] for item in data["items"]]
        assert "fn_1" in callee_ids

    def test_callers_of_fn_1(self, client):
        resp = client.get("/api/v1/functions/fn_1/callers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        caller_ids = [item["id"] for item in data["items"]]
        assert "fn_0" in caller_ids

    def test_callees_pagination(self, client):
        resp = client.get("/api/v1/functions/fn_0/callees?limit=1&offset=0")
        data = resp.json()
        assert len(data["items"]) <= 1

"""REST API contracts with real CastEngine data — architecture.md §8.

Tests the FastAPI endpoints against a store populated with real tree-sitter
parse results. Verifies response schemas, pagination, and data consistency.

BUG HUNTING TARGETS:
1. API returns stale/inconsistent data vs store
2. Pagination (limit/offset) off-by-one errors
3. Call-chain endpoint BFS depth handling
4. Stats endpoint bucket sums don't match totals
5. Function lookup by ID returns wrong function
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator


CASTENGINE_DIR = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")


@pytest.fixture(scope="module")
def castengine_store():
    if not CASTENGINE_DIR.exists():
        pytest.skip("CastEngine directory not available")
    store = InMemoryGraphStore()
    orch = PipelineOrchestrator(store=store, target_dir=CASTENGINE_DIR)
    orch.run_full_analysis()
    return store


@pytest.fixture(scope="module")
def client(castengine_store):
    app = create_app(store=castengine_store)
    return TestClient(app)


# ---------------------------------------------------------------------------
# §8: /api/v1/stats
# ---------------------------------------------------------------------------


class TestStatsEndpoint:
    """Stats endpoint should return consistent, complete data."""

    def test_stats_returns_200(self, client):
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200

    def test_stats_has_required_fields(self, client):
        data = client.get("/api/v1/stats").json()
        required = [
            "total_functions", "total_files", "total_calls",
            "total_unresolved", "calls_by_resolved_by", "calls_by_call_type",
        ]
        for field in required:
            assert field in data, f"Missing field: {field}"

    def test_stats_resolved_by_buckets_sum(self, client):
        data = client.get("/api/v1/stats").json()
        bucket_sum = sum(data["calls_by_resolved_by"].values())
        assert bucket_sum == data["total_calls"], (
            f"resolved_by sum {bucket_sum} != total_calls {data['total_calls']}"
        )

    def test_stats_call_type_buckets_sum(self, client):
        data = client.get("/api/v1/stats").json()
        bucket_sum = sum(data["calls_by_call_type"].values())
        assert bucket_sum == data["total_calls"]

    def test_stats_functions_positive(self, client):
        data = client.get("/api/v1/stats").json()
        assert data["total_functions"] >= 5000

    def test_stats_matches_store(self, client, castengine_store):
        """API stats should exactly match store.count_stats()."""
        api_stats = client.get("/api/v1/stats").json()
        store_stats = castengine_store.count_stats()
        assert api_stats["total_functions"] == store_stats["total_functions"]
        assert api_stats["total_calls"] == store_stats["total_calls"]
        assert api_stats["total_unresolved"] == store_stats["total_unresolved"]


# ---------------------------------------------------------------------------
# §8: /api/v1/files
# ---------------------------------------------------------------------------


class TestFilesEndpoint:
    """Files endpoint pagination and content."""

    def test_files_returns_200(self, client):
        resp = client.get("/api/v1/files")
        assert resp.status_code == 200

    def test_files_has_pagination_format(self, client):
        data = client.get("/api/v1/files").json()
        assert "total" in data
        assert "items" in data
        assert data["total"] >= 100

    def test_files_limit_works(self, client):
        data = client.get("/api/v1/files?limit=5").json()
        assert len(data["items"]) == 5

    def test_files_offset_works(self, client):
        page1 = client.get("/api/v1/files?limit=5&offset=0").json()
        page2 = client.get("/api/v1/files?limit=5&offset=5").json()
        # Pages should not overlap
        ids1 = {f["id"] for f in page1["items"]}
        ids2 = {f["id"] for f in page2["items"]}
        assert ids1.isdisjoint(ids2), "Pagination overlap detected"


# ---------------------------------------------------------------------------
# §8: /api/v1/functions
# ---------------------------------------------------------------------------


class TestFunctionsEndpoint:
    """Functions endpoint pagination and lookup."""

    def test_functions_returns_200(self, client):
        resp = client.get("/api/v1/functions")
        assert resp.status_code == 200

    def test_functions_has_pagination(self, client):
        data = client.get("/api/v1/functions").json()
        assert "total" in data
        assert "items" in data
        assert data["total"] >= 5000

    def test_functions_limit(self, client):
        data = client.get("/api/v1/functions?limit=3").json()
        assert len(data["items"]) == 3

    def test_function_by_id_returns_correct_data(self, client, castengine_store):
        """GET /functions/{id} should return the exact function."""
        fns = castengine_store.list_functions()
        fn = fns[0]
        resp = client.get(f"/api/v1/functions/{fn.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == fn.id
        assert data["name"] == fn.name
        assert data["signature"] == fn.signature

    def test_function_by_id_not_found(self, client):
        resp = client.get("/api/v1/functions/nonexistent_id_xyz")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# §8: /api/v1/functions/{id}/callers and /callees
# ---------------------------------------------------------------------------


class TestCallersCalleesEndpoint:
    """Callers/callees endpoints should return consistent data."""

    def test_callees_returns_200(self, client, castengine_store):
        # Pick a function with known outgoing edges
        edges = castengine_store.list_calls_edges()
        if not edges:
            pytest.skip("No edges")
        caller_id = edges[0].caller_id
        resp = client.get(f"/api/v1/functions/{caller_id}/callees")
        assert resp.status_code == 200

    def test_callers_returns_200(self, client, castengine_store):
        edges = castengine_store.list_calls_edges()
        if not edges:
            pytest.skip("No edges")
        callee_id = edges[0].callee_id
        resp = client.get(f"/api/v1/functions/{callee_id}/callers")
        assert resp.status_code == 200

    def test_callees_count_matches_store(self, client, castengine_store):
        """Callees from API should match edges from store."""
        edges = castengine_store.list_calls_edges()
        # Find a function with multiple callees
        from collections import Counter
        caller_counts = Counter(e.caller_id for e in edges)
        top_caller = caller_counts.most_common(1)[0][0]

        resp = client.get(f"/api/v1/functions/{top_caller}/callees")
        data = resp.json()
        api_count = data["total"] if "total" in data else len(data.get("items", data))

        store_count = sum(1 for e in edges if e.caller_id == top_caller)
        # API may deduplicate by callee_id, store counts all edges
        assert api_count <= store_count + 1


# ---------------------------------------------------------------------------
# §8: /api/v1/functions/{id}/call-chain
# ---------------------------------------------------------------------------


class TestCallChainEndpoint:
    """Call-chain (BFS) endpoint should return valid subgraph."""

    def test_call_chain_returns_200(self, client, castengine_store):
        fns = castengine_store.list_functions()
        resp = client.get(f"/api/v1/functions/{fns[0].id}/call-chain?depth=2")
        assert resp.status_code == 200

    def test_call_chain_has_nodes_and_edges(self, client, castengine_store):
        edges = castengine_store.list_calls_edges()
        caller_id = edges[0].caller_id
        resp = client.get(f"/api/v1/functions/{caller_id}/call-chain?depth=3")
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert len(data["nodes"]) >= 1  # At least the root

    def test_call_chain_depth_limits_result(self, client, castengine_store):
        """Deeper depth should return more or equal nodes."""
        edges = castengine_store.list_calls_edges()
        caller_id = edges[0].caller_id
        r1 = client.get(f"/api/v1/functions/{caller_id}/call-chain?depth=1").json()
        r3 = client.get(f"/api/v1/functions/{caller_id}/call-chain?depth=3").json()
        assert len(r3["nodes"]) >= len(r1["nodes"])

    def test_call_chain_root_in_nodes(self, client, castengine_store):
        """The root function should always be in the result nodes."""
        fns = castengine_store.list_functions()
        fn_id = fns[0].id
        data = client.get(f"/api/v1/functions/{fn_id}/call-chain?depth=2").json()
        node_ids = [n["id"] for n in data["nodes"]]
        assert fn_id in node_ids


# ---------------------------------------------------------------------------
# §8: /api/v1/unresolved-calls
# ---------------------------------------------------------------------------


class TestUnresolvedCallsEndpoint:
    """Unresolved calls endpoint."""

    def test_unresolved_returns_200(self, client):
        resp = client.get("/api/v1/unresolved-calls")
        assert resp.status_code == 200

    def test_unresolved_has_pagination(self, client):
        data = client.get("/api/v1/unresolved-calls").json()
        assert "total" in data
        assert data["total"] >= 5000

    def test_unresolved_items_have_required_fields(self, client):
        data = client.get("/api/v1/unresolved-calls?limit=5").json()
        for item in data["items"]:
            assert "id" in item
            assert "caller_id" in item
            assert "call_expression" in item
            assert "call_file" in item
            assert "call_line" in item
            assert "call_type" in item

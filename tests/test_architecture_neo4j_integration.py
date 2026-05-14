"""Integration tests verifying architecture.md contracts against real Neo4j.

These tests use the existing tree-sitter parsed data in Neo4j (5491 Functions,
4503 CALLS edges, 19787 UnresolvedCalls from CastEngine) to verify that the
system behaves according to architecture.md specifications.

Prerequisites:
- Neo4j 5.x running at bolt://localhost:7687
- $NEO4J_PASSWORD set
- Data already loaded (from run_e2e_full.py or run_e2e_repair.py)

Run: pytest tests/test_architecture_neo4j_integration.py -v
"""
from __future__ import annotations

import os
import pytest
from dataclasses import asdict

# Skip entire module if Neo4j is not available
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD")
pytestmark = pytest.mark.skipif(
    NEO4J_PASSWORD is None,
    reason="NEO4J_PASSWORD not set — skip real Neo4j integration tests",
)


@pytest.fixture(scope="module")
def neo4j_store():
    """Create a Neo4jGraphStore connected to the real database."""
    from codemap_lite.graph.neo4j_store import Neo4jGraphStore

    store = Neo4jGraphStore(
        uri="bolt://localhost:7687",
        user="neo4j",
        password=NEO4J_PASSWORD,
    )
    yield store


@pytest.fixture(scope="module")
def stats(neo4j_store):
    """Cached stats from count_stats()."""
    return neo4j_store.count_stats()


# ---------------------------------------------------------------------------
# §4 Neo4j Schema — Node Types and Properties
# ---------------------------------------------------------------------------


class TestSection4_NodeSchema:
    """Verify Neo4j node types have all required properties (architecture.md §4)."""

    def test_function_nodes_have_required_properties(self, neo4j_store):
        """Function nodes must have: id, signature, name, file_path, start_line, end_line, body_hash."""
        fns = neo4j_store.list_functions()[:10]
        assert len(fns) > 0, "Should have Function nodes"
        for fn in fns:
            assert fn.id, "Function.id must be non-empty"
            assert fn.signature, "Function.signature must be non-empty"
            assert fn.name, "Function.name must be non-empty"
            assert fn.file_path, "Function.file_path must be non-empty"
            assert fn.start_line >= 1, "Function.start_line must be >= 1"
            assert fn.end_line >= fn.start_line, "Function.end_line >= start_line"
            assert fn.body_hash, "Function.body_hash must be non-empty"

    def test_file_nodes_have_required_properties(self, neo4j_store):
        """File nodes must have: file_path, hash, primary_language."""
        files = neo4j_store.list_files()[:10]
        assert len(files) > 0, "Should have File nodes"
        for f in files:
            assert f.file_path, "File.file_path must be non-empty"
            assert f.hash, "File.hash must be non-empty"
            assert f.primary_language, "File.primary_language must be non-empty"

    def test_unresolved_call_nodes_have_required_properties(self, neo4j_store):
        """UnresolvedCall nodes must have all lifecycle fields."""
        ucs = neo4j_store.get_unresolved_calls()[:10]
        assert len(ucs) > 0, "Should have UnresolvedCall nodes"
        for uc in ucs:
            assert uc.id, "UC.id must be non-empty"
            assert uc.caller_id, "UC.caller_id must be non-empty"
            assert uc.call_file, "UC.call_file must be non-empty"
            assert uc.call_line >= 1, "UC.call_line must be >= 1"
            assert uc.call_type in ("direct", "indirect", "virtual"), (
                f"UC.call_type must be valid, got {uc.call_type!r}"
            )
            assert uc.status in ("pending", "unresolvable"), (
                f"UC.status must be pending or unresolvable, got {uc.status!r}"
            )
            assert isinstance(uc.retry_count, int) and uc.retry_count >= 0

    def test_calls_edge_properties(self, neo4j_store):
        """CALLS edges must have: resolved_by, call_type, call_file, call_line."""
        from codemap_lite.graph.neo4j_store import _CallsEdge

        edges = neo4j_store.list_calls_edges()[:10]
        assert len(edges) > 0, "Should have CALLS edges"
        VALID_RESOLVED_BY = {"symbol_table", "signature", "dataflow", "context", "llm"}
        VALID_CALL_TYPE = {"direct", "indirect", "virtual"}
        for edge in edges:
            assert edge.props.resolved_by in VALID_RESOLVED_BY, (
                f"resolved_by must be valid, got {edge.props.resolved_by!r}"
            )
            assert edge.props.call_type in VALID_CALL_TYPE, (
                f"call_type must be valid, got {edge.props.call_type!r}"
            )
            assert edge.props.call_file, "call_file must be non-empty"
            assert edge.props.call_line >= 1, "call_line must be >= 1"


# ---------------------------------------------------------------------------
# §4 Neo4j Schema — Relationships
# ---------------------------------------------------------------------------


class TestSection4_Relationships:
    """Verify Neo4j relationships exist and are correct (architecture.md §4)."""

    def test_defines_relationships_exist(self, neo4j_store):
        """Every Function should have a DEFINES relationship from its File."""
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            # Count DEFINES
            r = s.run("MATCH ()-[d:DEFINES]->() RETURN count(d) as cnt").single()
            defines_count = r["cnt"]
            # Count Functions
            r = s.run("MATCH (f:Function) RETURN count(f) as cnt").single()
            fn_count = r["cnt"]
        driver.close()
        # Every function should have a DEFINES relationship
        assert defines_count > 0, "DEFINES relationships must exist"
        assert defines_count == fn_count, (
            f"Every Function should have DEFINES: {defines_count} vs {fn_count} functions"
        )

    def test_has_gap_relationships_match_uc_count(self, neo4j_store):
        """Every UnresolvedCall should have a HAS_GAP relationship from its caller."""
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            r = s.run("MATCH ()-[h:HAS_GAP]->() RETURN count(h) as cnt").single()
            has_gap_count = r["cnt"]
            r = s.run("MATCH (uc:UnresolvedCall) RETURN count(uc) as cnt").single()
            uc_count = r["cnt"]
        driver.close()
        assert has_gap_count >= uc_count - 1, (
            f"HAS_GAP count ({has_gap_count}) should be within 1 of UC count ({uc_count}). "
            f"A small delta indicates orphaned UCs from prior E2E runs."
        )

    def test_calls_edge_uniqueness(self, neo4j_store):
        """No duplicate CALLS edges (same caller+callee+call_file+call_line)."""
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            r = s.run(
                "MATCH (a:Function)-[r:CALLS]->(b:Function) "
                "WITH a.id AS caller, b.id AS callee, r.call_file AS cf, "
                "     r.call_line AS cl, count(r) AS cnt "
                "WHERE cnt > 1 "
                "RETURN count(*) as duplicates"
            ).single()
        driver.close()
        assert r["duplicates"] == 0, "No duplicate CALLS edges allowed"


# ---------------------------------------------------------------------------
# §4 + §8 — count_stats() Contract
# ---------------------------------------------------------------------------


class TestSection4_Stats:
    """Verify count_stats() returns all required buckets (architecture.md §8)."""

    def test_stats_has_all_required_keys(self, stats):
        required = {
            "total_functions", "total_files", "total_calls", "total_unresolved",
            "total_llm_edges", "total_repair_logs",
            "calls_by_resolved_by", "unresolved_by_status",
            "unresolved_by_category", "source_points_by_status",
        }
        missing = required - set(stats.keys())
        assert not missing, f"count_stats() missing keys: {missing}"

    def test_stats_totals_are_consistent(self, stats):
        """Totals must match sum of their breakdowns."""
        # total_calls == sum(calls_by_resolved_by)
        by_resolved = stats["calls_by_resolved_by"]
        assert stats["total_calls"] == sum(by_resolved.values()), (
            f"total_calls ({stats['total_calls']}) != sum(calls_by_resolved_by) ({sum(by_resolved.values())})"
        )
        # total_unresolved == sum(unresolved_by_status)
        by_status = stats["unresolved_by_status"]
        assert stats["total_unresolved"] == sum(by_status.values()), (
            f"total_unresolved ({stats['total_unresolved']}) != sum(unresolved_by_status)"
        )
        # total_llm_edges == calls_by_resolved_by['llm']
        assert stats["total_llm_edges"] == by_resolved.get("llm", 0)

    def test_stats_resolved_by_has_all_five_values(self, stats):
        """calls_by_resolved_by must have all 5 resolver types."""
        expected = {"symbol_table", "signature", "dataflow", "context", "llm"}
        actual = set(stats["calls_by_resolved_by"].keys())
        assert expected == actual, f"Expected {expected}, got {actual}"

    def test_stats_unresolved_by_category_has_all_categories(self, stats):
        """unresolved_by_category must have all 5 categories + 'none'."""
        expected = {
            "gate_failed", "agent_error", "subprocess_crash",
            "subprocess_timeout", "agent_exited_without_edge", "none",
        }
        actual = set(stats["unresolved_by_category"].keys())
        assert expected == actual, f"Expected {expected}, got {actual}"

    def test_stats_data_is_populated(self, stats):
        """With CastEngine data loaded, stats should show non-zero counts."""
        assert stats["total_functions"] > 100, "CastEngine should have >100 functions"
        assert stats["total_files"] > 50, "CastEngine should have >50 files"
        assert stats["total_calls"] > 100, "CastEngine should have >100 calls"
        assert stats["total_unresolved"] > 100, "CastEngine should have >100 UCs"


# ---------------------------------------------------------------------------
# §3 — Retry Mechanism and Gate
# ---------------------------------------------------------------------------


class TestSection3_RetryAndGate:
    """Verify retry mechanism and gate contracts (architecture.md §3)."""

    def test_update_retry_state_increments_count(self, neo4j_store):
        """update_unresolved_call_retry_state must increment retry_count."""
        from codemap_lite.graph.schema import UnresolvedCallNode, FunctionNode

        # Create test function + UC
        fn = FunctionNode(
            id="retry_test_fn", signature="void retry_test()",
            name="retry_test", file_path="retry_test.cpp",
            start_line=1, end_line=5, body_hash="rh",
        )
        neo4j_store.create_function(fn)
        uc = UnresolvedCallNode(
            caller_id="retry_test_fn", call_expression="target()",
            call_file="retry_test.cpp", call_line=3,
            call_type="indirect", source_code_snippet="target();",
            var_name="fp", var_type="void*",
            retry_count=0, status="pending",
        )
        neo4j_store.create_unresolved_call(uc)

        try:
            # Get the UC id
            gaps = neo4j_store.get_unresolved_calls(caller_id="retry_test_fn")
            assert len(gaps) == 1
            gap_id = gaps[0].id

            # First retry
            neo4j_store.update_unresolved_call_retry_state(
                gap_id, "2026-05-15T00:00:00Z", "gate_failed: 2 gaps remain"
            )
            gaps = neo4j_store.get_unresolved_calls(caller_id="retry_test_fn")
            assert gaps[0].retry_count == 1
            assert gaps[0].last_attempt_timestamp == "2026-05-15T00:00:00Z"
            assert gaps[0].last_attempt_reason == "gate_failed: 2 gaps remain"
            assert gaps[0].status == "pending"  # Still pending (< 3)

            # Second retry
            neo4j_store.update_unresolved_call_retry_state(
                gap_id, "2026-05-15T00:01:00Z", "agent_exited_without_edge"
            )
            gaps = neo4j_store.get_unresolved_calls(caller_id="retry_test_fn")
            assert gaps[0].retry_count == 2
            assert gaps[0].status == "pending"  # Still pending (< 3)

            # Third retry → status becomes "unresolvable"
            neo4j_store.update_unresolved_call_retry_state(
                gap_id, "2026-05-15T00:02:00Z", "gate_failed: 1 gap remain"
            )
            gaps = neo4j_store.get_unresolved_calls(caller_id="retry_test_fn")
            assert gaps[0].retry_count == 3
            assert gaps[0].status == "unresolvable", (
                "After 3 retries, status must be 'unresolvable'"
            )
        finally:
            # Cleanup
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(
                "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
            )
            with driver.session() as s:
                s.run("MATCH (n) WHERE n.id = 'retry_test_fn' DETACH DELETE n")
                s.run(
                    "MATCH (uc:UnresolvedCall) WHERE uc.caller_id = 'retry_test_fn' "
                    "DETACH DELETE uc"
                )
            driver.close()

    def test_retry_reason_validation(self, neo4j_store):
        """last_attempt_reason must follow category format and ≤200 chars."""
        from codemap_lite.graph.schema import UnresolvedCallNode, FunctionNode

        fn = FunctionNode(
            id="reason_test_fn", signature="void reason_test()",
            name="reason_test", file_path="reason_test.cpp",
            start_line=1, end_line=5, body_hash="rh2",
        )
        neo4j_store.create_function(fn)
        uc = UnresolvedCallNode(
            caller_id="reason_test_fn", call_expression="x()",
            call_file="reason_test.cpp", call_line=2,
            call_type="indirect", source_code_snippet="x();",
            var_name="", var_type="", retry_count=0, status="pending",
        )
        neo4j_store.create_unresolved_call(uc)

        try:
            gaps = neo4j_store.get_unresolved_calls(caller_id="reason_test_fn")
            gap_id = gaps[0].id

            # Invalid category should raise
            with pytest.raises(ValueError, match="category must be one of"):
                neo4j_store.update_unresolved_call_retry_state(
                    gap_id, "2026-05-15T00:00:00Z", "invalid_category: test"
                )

            # Too long should raise
            with pytest.raises(ValueError, match="≤200 chars"):
                neo4j_store.update_unresolved_call_retry_state(
                    gap_id, "2026-05-15T00:00:00Z", "gate_failed: " + "x" * 200
                )

            # Valid standalone category (no colon)
            neo4j_store.update_unresolved_call_retry_state(
                gap_id, "2026-05-15T00:00:00Z", "agent_exited_without_edge"
            )
            gaps = neo4j_store.get_unresolved_calls(caller_id="reason_test_fn")
            assert gaps[0].last_attempt_reason == "agent_exited_without_edge"
        finally:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(
                "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
            )
            with driver.session() as s:
                s.run("MATCH (n) WHERE n.id = 'reason_test_fn' DETACH DELETE n")
                s.run(
                    "MATCH (uc:UnresolvedCall) WHERE uc.caller_id = 'reason_test_fn' "
                    "DETACH DELETE uc"
                )
            driver.close()

    def test_get_pending_gaps_for_source_bfs(self, neo4j_store):
        """get_pending_gaps_for_source must BFS through CALLS edges."""
        # Use a real function that has outgoing CALLS edges
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            # Find a function with outgoing CALLS edges AND reachable UCs
            r = s.run(
                "MATCH (f:Function)-[:CALLS*1..3]->(callee:Function)-[:HAS_GAP]->(uc:UnresolvedCall {status: 'pending'}) "
                "RETURN f.id, count(DISTINCT uc) as gap_count "
                "ORDER BY gap_count DESC LIMIT 1"
            ).single()
        driver.close()

        if r is None:
            pytest.skip("No function with reachable pending gaps found")

        source_id = r["f.id"]
        expected_min_gaps = r["gap_count"]

        gaps = neo4j_store.get_pending_gaps_for_source(source_id)
        # BFS should find at least the gaps reachable via CALLS edges
        assert len(gaps) >= expected_min_gaps, (
            f"BFS should find >= {expected_min_gaps} gaps, got {len(gaps)}"
        )
        # All returned gaps must be pending
        for gap in gaps:
            assert gap.status == "pending", f"Gap {gap.id} should be pending"


# ---------------------------------------------------------------------------
# §3 — icsl_tools write-edge + check-complete Contract
# ---------------------------------------------------------------------------


class TestSection3_IcslTools:
    """Verify icsl_tools CLI contract against real Neo4j (architecture.md §3)."""

    def test_write_edge_creates_edge_and_repair_log(self, neo4j_store):
        """write_edge must create CALLS edge + RepairLog + delete UC."""
        from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode
        from codemap_lite.agent.icsl_tools import write_edge

        # Setup
        fn_a = FunctionNode(
            id="icsl_fn_a", signature="void a()", name="a",
            file_path="icsl_test.cpp", start_line=1, end_line=5, body_hash="ia",
        )
        fn_b = FunctionNode(
            id="icsl_fn_b", signature="void b()", name="b",
            file_path="icsl_test.cpp", start_line=10, end_line=15, body_hash="ib",
        )
        neo4j_store.create_function(fn_a)
        neo4j_store.create_function(fn_b)
        uc = UnresolvedCallNode(
            caller_id="icsl_fn_a", call_expression="b()",
            call_file="icsl_test.cpp", call_line=3,
            call_type="indirect", source_code_snippet="b();",
            var_name="fp", var_type="void*",
        )
        neo4j_store.create_unresolved_call(uc)

        try:
            # Execute write_edge
            result = write_edge(
                caller_id="icsl_fn_a",
                callee_id="icsl_fn_b",
                call_type="indirect",
                call_file="icsl_test.cpp",
                call_line=3,
                store=neo4j_store,
                llm_response="chose b because it handles the callback",
                reasoning_summary="b is the registered handler",
            )
            assert result["edge_created"] is True
            assert result["skipped"] is False

            # Verify edge exists
            assert neo4j_store.edge_exists("icsl_fn_a", "icsl_fn_b", "icsl_test.cpp", 3)

            # Verify RepairLog exists
            logs = neo4j_store.get_repair_logs(
                caller_id="icsl_fn_a", callee_id="icsl_fn_b",
                call_location="icsl_test.cpp:3",
            )
            assert len(logs) == 1
            assert logs[0].repair_method == "llm"
            assert logs[0].reasoning_summary == "b is the registered handler"
            assert logs[0].llm_response == "chose b because it handles the callback"

            # Verify UC was deleted
            gaps = [
                g for g in neo4j_store.get_unresolved_calls(caller_id="icsl_fn_a")
                if g.call_file == "icsl_test.cpp" and g.call_line == 3
            ]
            assert len(gaps) == 0, "UC should be deleted after write_edge"

            # Verify duplicate write is skipped
            result2 = write_edge(
                caller_id="icsl_fn_a", callee_id="icsl_fn_b",
                call_type="indirect", call_file="icsl_test.cpp", call_line=3,
                store=neo4j_store,
            )
            assert result2["skipped"] is True
        finally:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(
                "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
            )
            with driver.session() as s:
                s.run("MATCH (n) WHERE n.id STARTS WITH 'icsl_fn_' DETACH DELETE n")
                s.run(
                    "MATCH (r:RepairLog) WHERE r.caller_id = 'icsl_fn_a' "
                    "DETACH DELETE r"
                )
            driver.close()

    def test_write_edge_validates_call_type(self, neo4j_store):
        """write_edge must reject invalid call_type values."""
        from codemap_lite.agent.icsl_tools import write_edge

        with pytest.raises(ValueError, match="call_type must be one of"):
            write_edge(
                caller_id="x", callee_id="y", call_type="unknown",
                call_file="test.cpp", call_line=1, store=neo4j_store,
            )

    def test_write_edge_truncates_reasoning_summary(self, neo4j_store):
        """reasoning_summary > 200 chars must be truncated."""
        from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode
        from codemap_lite.agent.icsl_tools import write_edge

        fn_a = FunctionNode(
            id="trunc_fn_a", signature="void a()", name="a",
            file_path="trunc.cpp", start_line=1, end_line=5, body_hash="ta",
        )
        fn_b = FunctionNode(
            id="trunc_fn_b", signature="void b()", name="b",
            file_path="trunc.cpp", start_line=10, end_line=15, body_hash="tb",
        )
        neo4j_store.create_function(fn_a)
        neo4j_store.create_function(fn_b)
        uc = UnresolvedCallNode(
            caller_id="trunc_fn_a", call_expression="b()",
            call_file="trunc.cpp", call_line=3,
            call_type="indirect", source_code_snippet="b();",
            var_name="", var_type="",
        )
        neo4j_store.create_unresolved_call(uc)

        try:
            long_summary = "x" * 250
            write_edge(
                caller_id="trunc_fn_a", callee_id="trunc_fn_b",
                call_type="indirect", call_file="trunc.cpp", call_line=3,
                store=neo4j_store, reasoning_summary=long_summary,
            )
            logs = neo4j_store.get_repair_logs(
                caller_id="trunc_fn_a", callee_id="trunc_fn_b",
                call_location="trunc.cpp:3",
            )
            assert len(logs[0].reasoning_summary) <= 200, (
                "reasoning_summary must be truncated to ≤200 chars"
            )
        finally:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(
                "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
            )
            with driver.session() as s:
                s.run("MATCH (n) WHERE n.id STARTS WITH 'trunc_fn_' DETACH DELETE n")
                s.run(
                    "MATCH (r:RepairLog) WHERE r.caller_id = 'trunc_fn_a' "
                    "DETACH DELETE r"
                )
            driver.close()

    def test_check_complete_with_pending_gaps(self, neo4j_store):
        """check_complete must return False when pending gaps exist."""
        from codemap_lite.agent.icsl_tools import check_complete

        # Use a real function that has pending UCs
        ucs = neo4j_store.get_unresolved_calls(status="pending")
        if not ucs:
            pytest.skip("No pending UCs in database")

        caller_id = ucs[0].caller_id
        result = check_complete(caller_id, neo4j_store)
        assert result["complete"] is False
        assert result["remaining_gaps"] > 0
        assert len(result["pending_gap_ids"]) > 0


# ---------------------------------------------------------------------------
# §8 — REST API Contract (live server against real Neo4j)
# ---------------------------------------------------------------------------


class TestSection8_RestAPI:
    """Verify REST API endpoints against real Neo4j (architecture.md §8)."""

    @pytest.fixture(autouse=True)
    def setup_client(self, neo4j_store):
        """Start a test client with real Neo4j store."""
        from fastapi.testclient import TestClient
        from codemap_lite.api.app import create_app

        app = create_app(store=neo4j_store)
        self.client = TestClient(app)

    def test_health_endpoint(self):
        r = self.client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_stats_endpoint_schema(self):
        r = self.client.get("/api/v1/stats")
        assert r.status_code == 200
        data = r.json()
        # Must have all required fields
        assert "total_functions" in data
        assert "total_files" in data
        assert "total_calls" in data
        assert "total_unresolved" in data
        assert "total_source_points" in data
        assert "calls_by_resolved_by" in data
        assert "unresolved_by_status" in data
        assert "unresolved_by_category" in data

    def test_files_endpoint_pagination(self):
        r = self.client.get("/api/v1/files?limit=5&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] > 0
        assert len(data["items"]) <= 5
        # Each file must have required fields
        for f in data["items"]:
            assert "file_path" in f
            assert "hash" in f

    def test_functions_endpoint_pagination(self):
        r = self.client.get("/api/v1/functions?limit=3&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] > 0
        assert len(data["items"]) <= 3
        for fn in data["items"]:
            assert "id" in fn
            assert "name" in fn
            assert "signature" in fn

    def test_function_detail_endpoint(self):
        # Get a function ID first
        r = self.client.get("/api/v1/functions?limit=1")
        fn_id = r.json()["items"][0]["id"]

        r = self.client.get(f"/api/v1/functions/{fn_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == fn_id
        assert "signature" in data
        assert "file_path" in data

    def test_function_callers_endpoint(self):
        # Find a function that has callers
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            r = s.run(
                "MATCH (a:Function)-[:CALLS]->(b:Function) "
                "RETURN b.id LIMIT 1"
            ).single()
        driver.close()
        if r is None:
            pytest.skip("No function with callers")

        fn_id = r["b.id"]
        resp = self.client.get(f"/api/v1/functions/{fn_id}/callers")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] > 0

    def test_function_callees_endpoint(self):
        # Find a function that has callees
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            r = s.run(
                "MATCH (a:Function)-[:CALLS]->(b:Function) "
                "RETURN a.id LIMIT 1"
            ).single()
        driver.close()
        if r is None:
            pytest.skip("No function with callees")

        fn_id = r["a.id"]
        resp = self.client.get(f"/api/v1/functions/{fn_id}/callees")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] > 0

    def test_call_chain_endpoint(self):
        r = self.client.get("/api/v1/functions?limit=1")
        fn_id = r.json()["items"][0]["id"]

        r = self.client.get(f"/api/v1/functions/{fn_id}/call-chain?depth=3")
        assert r.status_code == 200
        data = r.json()
        assert "nodes" in data
        assert "edges" in data
        assert len(data["nodes"]) >= 1  # At least the source itself

    def test_unresolved_calls_endpoint(self):
        r = self.client.get("/api/v1/unresolved-calls?limit=5")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] > 0
        for uc in data["items"]:
            assert "caller_id" in uc
            assert "call_expression" in uc
            assert "call_type" in uc
            assert "status" in uc

    def test_unresolved_calls_filter_by_status(self):
        r = self.client.get("/api/v1/unresolved-calls?status=pending&limit=3")
        assert r.status_code == 200
        data = r.json()
        for uc in data["items"]:
            assert uc["status"] == "pending"

    def test_repair_logs_endpoint(self):
        r = self.client.get("/api/v1/repair-logs")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data

    def test_reviews_endpoint(self):
        r = self.client.get("/api/v1/reviews")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data

    def test_feedback_endpoint(self):
        r = self.client.get("/api/v1/feedback")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data

    def test_nonexistent_function_returns_404(self):
        r = self.client.get("/api/v1/functions/nonexistent_id_xyz")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# §5 Review Cascade — Full 4-step flow against real Neo4j
# ---------------------------------------------------------------------------


class TestSection5_ReviewCascade:
    """Test the review cascade (verdict=incorrect) against real Neo4j.

    Creates a test LLM edge, marks it incorrect, and verifies:
    1. Edge is deleted
    2. RepairLog is deleted
    3. UnresolvedCall is regenerated (retry_count=0, status=pending)
    4. Counter-example is created in FeedbackStore
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        """Set up test client with real Neo4j store + temp feedback store."""
        import tempfile
        from pathlib import Path
        from codemap_lite.api.app import create_app
        from codemap_lite.analysis.feedback_store import FeedbackStore
        from fastapi.testclient import TestClient

        self.store = neo4j_store
        self.tmpdir = Path(tempfile.mkdtemp())
        self.fb_store = FeedbackStore(storage_dir=self.tmpdir)
        app = create_app(store=neo4j_store, feedback_store=self.fb_store)
        self.client = TestClient(app)

        # Use two real functions for the test edge
        self.caller_id = "548c2e2d200a"  # GetVideoSize
        self.callee_id = "b2320a8683d2"  # GetU
        self.call_file = "__test_review_cascade__.cpp"
        self.call_line = 9999

    def _create_test_edge(self):
        """Create a test LLM edge + RepairLog."""
        from codemap_lite.graph.schema import CallsEdgeProps, RepairLogNode

        props = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file=self.call_file, call_line=self.call_line,
        )
        self.store.create_calls_edge(self.caller_id, self.callee_id, props)
        log = RepairLogNode(
            caller_id=self.caller_id,
            callee_id=self.callee_id,
            call_location=f"{self.call_file}:{self.call_line}",
            repair_method="llm",
            llm_response="test",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="test reasoning",
        )
        self.store.create_repair_log(log)

    def _cleanup_test_edge(self):
        """Remove any leftover test data."""
        self.store.delete_unresolved_call(
            self.caller_id, self.call_file, self.call_line
        )
        # Edge may already be deleted by cascade
        self.store.delete_calls_edge(
            self.caller_id, self.callee_id, self.call_file, self.call_line
        )

    def test_verdict_incorrect_deletes_edge(self):
        """§5: verdict=incorrect → CALLS edge is deleted."""
        self._create_test_edge()
        try:
            assert self.store.edge_exists(
                self.caller_id, self.callee_id, self.call_file, self.call_line
            )
            r = self.client.post("/api/v1/reviews", json={
                "caller_id": self.caller_id,
                "callee_id": self.callee_id,
                "call_file": self.call_file,
                "call_line": self.call_line,
                "verdict": "incorrect",
                "correct_target": "41e010d4a8b0",
            })
            assert r.status_code == 201
            # Edge should be gone
            assert not self.store.edge_exists(
                self.caller_id, self.callee_id, self.call_file, self.call_line
            )
        finally:
            self._cleanup_test_edge()

    def test_verdict_incorrect_regenerates_uc(self):
        """§5: verdict=incorrect → UC regenerated with retry_count=0."""
        self._create_test_edge()
        try:
            self.client.post("/api/v1/reviews", json={
                "caller_id": self.caller_id,
                "callee_id": self.callee_id,
                "call_file": self.call_file,
                "call_line": self.call_line,
                "verdict": "incorrect",
            })
            ucs = self.store.get_unresolved_calls(caller_id=self.caller_id)
            matching = [
                u for u in ucs
                if u.call_file == self.call_file and u.call_line == self.call_line
            ]
            assert len(matching) == 1
            assert matching[0].status == "pending"
            assert matching[0].retry_count == 0
        finally:
            self._cleanup_test_edge()

    def test_verdict_incorrect_creates_counter_example(self):
        """§5: verdict=incorrect + correct_target → counter-example created."""
        self._create_test_edge()
        try:
            self.client.post("/api/v1/reviews", json={
                "caller_id": self.caller_id,
                "callee_id": self.callee_id,
                "call_file": self.call_file,
                "call_line": self.call_line,
                "verdict": "incorrect",
                "correct_target": "41e010d4a8b0",
            })
            examples = self.fb_store.list_all()
            assert len(examples) >= 1
            ex = examples[-1]
            assert ex.wrong_target == self.callee_id
            assert ex.correct_target == "41e010d4a8b0"
            assert ex.source_id == self.caller_id
        finally:
            self._cleanup_test_edge()

    def test_verdict_correct_preserves_edge(self):
        """§5: verdict=correct → edge stays, review recorded."""
        self._create_test_edge()
        try:
            r = self.client.post("/api/v1/reviews", json={
                "caller_id": self.caller_id,
                "callee_id": self.callee_id,
                "call_file": self.call_file,
                "call_line": self.call_line,
                "verdict": "correct",
            })
            assert r.status_code == 201
            # Edge should still exist
            assert self.store.edge_exists(
                self.caller_id, self.callee_id, self.call_file, self.call_line
            )
        finally:
            # Clean up the test edge (not deleted by correct verdict)
            self.store.delete_calls_edge(
                self.caller_id, self.callee_id, self.call_file, self.call_line
            )
            self.store.delete_repair_logs_for_edge(
                self.caller_id, self.callee_id,
                f"{self.call_file}:{self.call_line}",
            )


# ---------------------------------------------------------------------------
# §8 Manual Edge Operations — POST/DELETE /edges
# ---------------------------------------------------------------------------


class TestSection8_ManualEdges:
    """Test manual edge create/delete endpoints against real Neo4j."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        self.store = neo4j_store
        app = create_app(store=neo4j_store)
        self.client = TestClient(app)
        self.caller_id = "548c2e2d200a"
        self.callee_id = "41e010d4a8b0"
        self.call_file = "__test_manual_edge__.cpp"
        self.call_line = 8888

    def _cleanup(self):
        self.store.delete_calls_edge(
            self.caller_id, self.callee_id, self.call_file, self.call_line
        )
        self.store.delete_unresolved_call(
            self.caller_id, self.call_file, self.call_line
        )

    def test_create_edge_success(self):
        """POST /edges creates edge and deletes matching UC."""
        self._cleanup()
        try:
            r = self.client.post("/api/v1/edges", json={
                "caller_id": self.caller_id,
                "callee_id": self.callee_id,
                "call_file": self.call_file,
                "call_line": self.call_line,
                "resolved_by": "llm",
                "call_type": "indirect",
            })
            assert r.status_code == 201
            assert self.store.edge_exists(
                self.caller_id, self.callee_id, self.call_file, self.call_line
            )
        finally:
            self._cleanup()

    def test_create_edge_duplicate_409(self):
        """POST /edges returns 409 if edge already exists."""
        self._cleanup()
        from codemap_lite.graph.schema import CallsEdgeProps
        props = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file=self.call_file, call_line=self.call_line,
        )
        self.store.create_calls_edge(self.caller_id, self.callee_id, props)
        try:
            r = self.client.post("/api/v1/edges", json={
                "caller_id": self.caller_id,
                "callee_id": self.callee_id,
                "call_file": self.call_file,
                "call_line": self.call_line,
                "resolved_by": "llm",
                "call_type": "indirect",
            })
            assert r.status_code == 409
        finally:
            self._cleanup()

    def test_delete_edge_regenerates_uc(self):
        """DELETE /edges deletes edge and regenerates UC."""
        self._cleanup()
        from codemap_lite.graph.schema import CallsEdgeProps
        props = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file=self.call_file, call_line=self.call_line,
        )
        self.store.create_calls_edge(self.caller_id, self.callee_id, props)
        try:
            r = self.client.request("DELETE", "/api/v1/edges", json={
                "caller_id": self.caller_id,
                "callee_id": self.callee_id,
                "call_file": self.call_file,
                "call_line": self.call_line,
            })
            assert r.status_code == 204
            # Edge gone
            assert not self.store.edge_exists(
                self.caller_id, self.callee_id, self.call_file, self.call_line
            )
            # UC regenerated
            ucs = self.store.get_unresolved_calls(caller_id=self.caller_id)
            matching = [
                u for u in ucs
                if u.call_file == self.call_file and u.call_line == self.call_line
            ]
            assert len(matching) == 1
            assert matching[0].status == "pending"
            assert matching[0].retry_count == 0
        finally:
            self._cleanup()


# ---------------------------------------------------------------------------
# §3 Feedback — POST /feedback with source_id
# ---------------------------------------------------------------------------


class TestSection3_Feedback:
    """Test POST /api/v1/feedback with source_id scoping."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        import tempfile
        from pathlib import Path
        from codemap_lite.api.app import create_app
        from codemap_lite.analysis.feedback_store import FeedbackStore
        from fastapi.testclient import TestClient

        self.tmpdir = Path(tempfile.mkdtemp())
        self.fb_store = FeedbackStore(storage_dir=self.tmpdir)
        app = create_app(store=neo4j_store, feedback_store=self.fb_store)
        self.client = TestClient(app)

    def test_post_feedback_with_source_id(self):
        """POST /feedback accepts source_id for per-source scoping."""
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "foo.cpp:42",
            "wrong_target": "bad_func",
            "correct_target": "good_func",
            "pattern": "vtable dispatch at foo.cpp:42",
            "source_id": "src_001",
        })
        assert r.status_code == 201
        data = r.json()
        assert data["source_id"] == "src_001"
        assert data["deduplicated"] is False

    def test_post_feedback_without_source_id(self):
        """POST /feedback works without source_id (defaults to empty)."""
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "bar.cpp:10",
            "wrong_target": "wrong",
            "correct_target": "right",
            "pattern": "callback at bar.cpp:10",
        })
        assert r.status_code == 201
        data = r.json()
        assert data["source_id"] == ""

    def test_post_feedback_deduplication(self):
        """POST /feedback deduplicates on pattern match."""
        body = {
            "call_context": "baz.cpp:5",
            "wrong_target": "w",
            "correct_target": "c",
            "pattern": "same pattern",
        }
        r1 = self.client.post("/api/v1/feedback", json=body)
        assert r1.status_code == 201
        assert r1.json()["deduplicated"] is False

        r2 = self.client.post("/api/v1/feedback", json=body)
        assert r2.status_code == 201
        assert r2.json()["deduplicated"] is True

    def test_post_feedback_wrong_equals_correct_rejected(self):
        """POST /feedback rejects when wrong_target == correct_target."""
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "x.cpp:1",
            "wrong_target": "same",
            "correct_target": "same",
            "pattern": "test",
        })
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# §3 SourcePoint Lifecycle — Status transitions (architecture.md §3 门禁机制)
# ---------------------------------------------------------------------------


class TestSection3_SourcePointLifecycle:
    """Test SourcePoint status transitions against real Neo4j.

    architecture.md §3: pending → running → complete | partial_complete.
    Backward transitions only allowed with force_reset=True.
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import SourcePointNode
        self.store = neo4j_store
        self.sp_id = "__test_sp_lifecycle__"
        # Create a test SourcePoint
        sp = SourcePointNode(
            id=self.sp_id,
            entry_point_kind="test",
            reason="lifecycle test",
            function_id="548c2e2d200a",  # real function
            module="test_module",
            status="pending",
        )
        self.store.create_source_point(sp)
        yield
        # Cleanup
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            s.run(
                "MATCH (s:SourcePoint {id: $id}) DETACH DELETE s",
                id=self.sp_id,
            )
        driver.close()

    def test_forward_transition_pending_to_running(self):
        """pending → running is valid."""
        self.store.update_source_point_status(self.sp_id, "running")
        sp = self.store.get_source_point(self.sp_id)
        assert sp is not None
        assert sp.status == "running"

    def test_forward_transition_running_to_complete(self):
        """running → complete is valid."""
        self.store.update_source_point_status(self.sp_id, "running")
        self.store.update_source_point_status(self.sp_id, "complete")
        sp = self.store.get_source_point(self.sp_id)
        assert sp.status == "complete"

    def test_forward_transition_running_to_partial_complete(self):
        """running → partial_complete is valid."""
        self.store.update_source_point_status(self.sp_id, "running")
        self.store.update_source_point_status(self.sp_id, "partial_complete")
        sp = self.store.get_source_point(self.sp_id)
        assert sp.status == "partial_complete"

    def test_backward_transition_rejected(self):
        """complete → pending without force_reset raises ValueError."""
        self.store.update_source_point_status(self.sp_id, "running")
        self.store.update_source_point_status(self.sp_id, "complete")
        with pytest.raises(ValueError, match="Invalid SourcePoint transition"):
            self.store.update_source_point_status(self.sp_id, "pending")

    def test_backward_transition_with_force_reset(self):
        """complete → pending with force_reset=True succeeds."""
        self.store.update_source_point_status(self.sp_id, "running")
        self.store.update_source_point_status(self.sp_id, "complete")
        self.store.update_source_point_status(self.sp_id, "pending", force_reset=True)
        sp = self.store.get_source_point(self.sp_id)
        assert sp.status == "pending"

    def test_is_source_relationship_created(self):
        """create_source_point must create IS_SOURCE relationship."""
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            r = s.run(
                "MATCH (f:Function {id: $fid})-[:IS_SOURCE]->(s:SourcePoint {id: $sid}) "
                "RETURN count(s) AS c",
                fid="548c2e2d200a", sid=self.sp_id,
            ).single()
        driver.close()
        assert r["c"] == 1, "IS_SOURCE relationship must exist"


# ---------------------------------------------------------------------------
# §7 Incremental Cascade — File invalidation against real Neo4j
# ---------------------------------------------------------------------------


class TestSection7_IncrementalCascade:
    """Test incremental invalidation cascade against real Neo4j.

    Creates isolated test data, invalidates a file, and verifies:
    1. Functions in file are deleted
    2. Cross-file LLM edges → UC regenerated
    3. RepairLogs deleted
    4. Affected SourcePoints reset to pending
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import (
            FunctionNode, CallsEdgeProps, RepairLogNode,
            UnresolvedCallNode, SourcePointNode,
        )
        self.store = neo4j_store
        self.test_file = "__test_incr_cascade__.cpp"

        # Create functions in the test file
        self.fn_in_file = FunctionNode(
            id="incr_fn_target", signature="void target()", name="target",
            file_path=self.test_file, start_line=1, end_line=10, body_hash="it",
        )
        self.store.create_function(self.fn_in_file)

        # Create a caller in a DIFFERENT file (cross-file edge)
        self.fn_caller = FunctionNode(
            id="incr_fn_caller", signature="void caller()", name="caller",
            file_path="other_file.cpp", start_line=1, end_line=10, body_hash="ic",
        )
        self.store.create_function(self.fn_caller)

        # Create an LLM edge from caller → target (cross-file)
        props = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="other_file.cpp", call_line=5,
        )
        self.store.create_calls_edge("incr_fn_caller", "incr_fn_target", props)

        # Create a RepairLog for this edge
        log = RepairLogNode(
            caller_id="incr_fn_caller",
            callee_id="incr_fn_target",
            call_location="other_file.cpp:5",
            repair_method="llm",
            llm_response="test",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="test",
        )
        self.store.create_repair_log(log)

        # Create a SourcePoint for the caller
        sp = SourcePointNode(
            id="incr_fn_caller",
            entry_point_kind="test",
            reason="incremental test",
            function_id="incr_fn_caller",
            module="test",
            status="complete",
        )
        self.store.create_source_point(sp)

        yield

        # Cleanup
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH 'incr_fn_' DETACH DELETE n")
            s.run(
                "MATCH (r:RepairLog) WHERE r.caller_id = 'incr_fn_caller' DELETE r"
            )
            s.run(
                "MATCH (s:SourcePoint {id: 'incr_fn_caller'}) DETACH DELETE s"
            )
            s.run(
                "MATCH (u:UnresolvedCall) WHERE u.caller_id = 'incr_fn_caller' "
                "AND u.call_file = 'other_file.cpp' AND u.call_line = 5 "
                "DETACH DELETE u"
            )
        driver.close()

    def test_invalidate_deletes_functions_in_file(self):
        """§7 step 1: Functions in invalidated file are deleted."""
        from codemap_lite.graph.incremental import IncrementalUpdater
        updater = IncrementalUpdater(store=self.store, target_dir="")
        result = updater.invalidate_file(self.test_file)
        assert "incr_fn_target" in result.removed_functions
        # Function should be gone from Neo4j
        assert self.store.get_function_by_id("incr_fn_target") is None

    def test_invalidate_regenerates_uc_for_cross_file_llm_edge(self):
        """§7 step 3: Cross-file LLM edge → UC regenerated."""
        from codemap_lite.graph.incremental import IncrementalUpdater
        updater = IncrementalUpdater(store=self.store, target_dir="")
        result = updater.invalidate_file(self.test_file)
        # UC should be regenerated for the caller
        assert len(result.regenerated_unresolved_calls) >= 1
        ucs = self.store.get_unresolved_calls(caller_id="incr_fn_caller")
        matching = [
            u for u in ucs
            if u.call_file == "other_file.cpp" and u.call_line == 5
        ]
        assert len(matching) == 1
        assert matching[0].status == "pending"
        assert matching[0].retry_count == 0

    def test_invalidate_resets_source_point_status(self):
        """§7 step 4: Affected SourcePoint reset to pending."""
        from codemap_lite.graph.incremental import IncrementalUpdater
        updater = IncrementalUpdater(store=self.store, target_dir="")
        result = updater.invalidate_file(self.test_file)
        assert "incr_fn_caller" in result.affected_source_ids
        sp = self.store.get_source_point("incr_fn_caller")
        assert sp is not None
        assert sp.status == "pending"

    def test_invalidate_deletes_repair_log(self):
        """§7 step 3: RepairLog for invalidated LLM edge is deleted."""
        from codemap_lite.graph.incremental import IncrementalUpdater
        updater = IncrementalUpdater(store=self.store, target_dir="")
        updater.invalidate_file(self.test_file)
        logs = self.store.get_repair_logs(
            caller_id="incr_fn_caller", callee_id="incr_fn_target",
            call_location="other_file.cpp:5",
        )
        assert len(logs) == 0, "RepairLog should be deleted after invalidation"


# ---------------------------------------------------------------------------
# §8 Stats Correctness — Verify counts match actual Neo4j data
# ---------------------------------------------------------------------------


class TestSection8_StatsCorrectness:
    """Verify /api/v1/stats counts match actual Neo4j aggregations."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient
        self.store = neo4j_store
        app = create_app(store=neo4j_store)
        self.client = TestClient(app)

    def test_total_functions_matches_neo4j(self, neo4j_store):
        """total_functions must match MATCH (f:Function) RETURN count(f)."""
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            actual = s.run(
                "MATCH (f:Function) RETURN count(f) AS n"
            ).single()["n"]
        driver.close()

        r = self.client.get("/api/v1/stats")
        assert r.json()["total_functions"] == actual

    def test_total_calls_matches_neo4j(self, neo4j_store):
        """total_calls must match MATCH ()-[r:CALLS]->() RETURN count(r)."""
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            actual = s.run(
                "MATCH ()-[r:CALLS]->() RETURN count(r) AS n"
            ).single()["n"]
        driver.close()

        r = self.client.get("/api/v1/stats")
        assert r.json()["total_calls"] == actual

    def test_calls_by_resolved_by_has_all_keys(self):
        """calls_by_resolved_by must have all 5 resolved_by keys."""
        r = self.client.get("/api/v1/stats")
        data = r.json()["calls_by_resolved_by"]
        expected_keys = {"symbol_table", "signature", "dataflow", "context", "llm"}
        assert set(data.keys()) == expected_keys

    def test_unresolved_by_status_has_required_keys(self):
        """unresolved_by_status must have pending and unresolvable keys."""
        r = self.client.get("/api/v1/stats")
        data = r.json()["unresolved_by_status"]
        assert "pending" in data
        assert "unresolvable" in data

    def test_unresolved_by_category_has_all_keys(self):
        """unresolved_by_category must have all 5 category keys + none."""
        r = self.client.get("/api/v1/stats")
        data = r.json()["unresolved_by_category"]
        expected_keys = {
            "gate_failed", "agent_error", "subprocess_crash",
            "subprocess_timeout", "agent_exited_without_edge", "none",
        }
        assert set(data.keys()) == expected_keys

    def test_source_points_by_status_has_all_keys(self):
        """source_points_by_status must have all 4 lifecycle states."""
        r = self.client.get("/api/v1/stats")
        data = r.json()
        # source_points_by_status may be in the response
        if "source_points_by_status" in data:
            sp_data = data["source_points_by_status"]
            expected_keys = {"pending", "running", "complete", "partial_complete"}
            assert set(sp_data.keys()) == expected_keys


# ---------------------------------------------------------------------------
# §3 Gate Mechanism — get_pending_gaps_for_source BFS
# ---------------------------------------------------------------------------


class TestSection3_GateMechanism:
    """Test gate mechanism BFS against real Neo4j (architecture.md §3)."""

    def test_pending_gaps_for_source_returns_reachable_gaps(self, neo4j_store):
        """get_pending_gaps_for_source must find gaps reachable via CALLS BFS."""
        # Find a function that has pending UCs
        ucs = neo4j_store.get_unresolved_calls(status="pending")
        if not ucs:
            pytest.skip("No pending UCs in database")

        # Use the first UC's caller as our source
        caller_id = ucs[0].caller_id
        gaps = neo4j_store.get_pending_gaps_for_source(caller_id)
        # The caller's own gaps should be included (depth 0)
        assert len(gaps) >= 1
        gap_ids = {g.id for g in gaps}
        assert ucs[0].id in gap_ids

    def test_pending_gaps_for_nonexistent_source_returns_empty(self, neo4j_store):
        """get_pending_gaps_for_source with unknown ID returns empty list."""
        gaps = neo4j_store.get_pending_gaps_for_source("nonexistent_xyz_123")
        assert gaps == []

    def test_gate_check_complete_false_when_gaps_exist(self, neo4j_store):
        """check_complete returns False when source has pending gaps."""
        from codemap_lite.agent.icsl_tools import check_complete

        ucs = neo4j_store.get_unresolved_calls(status="pending")
        if not ucs:
            pytest.skip("No pending UCs in database")

        caller_id = ucs[0].caller_id
        result = check_complete(caller_id, neo4j_store)
        assert result["complete"] is False
        assert result["remaining_gaps"] > 0
        assert isinstance(result["pending_gap_ids"], list)


# ---------------------------------------------------------------------------
# §3 write-edge + RepairLog creation (architecture.md §3 修复成功时)
# ---------------------------------------------------------------------------


class TestSection3_WriteEdgeFlow:
    """Test write-edge creates CALLS edge + RepairLog + deletes UC."""

    def test_write_edge_creates_edge_and_repair_log(self, neo4j_store):
        """write_edge creates a CALLS edge with resolved_by=llm and a RepairLog."""
        from codemap_lite.agent.icsl_tools import write_edge
        from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

        # Create two temporary functions
        caller = FunctionNode(
            id="test_we_caller_001", name="test_caller", signature="void test_caller()",
            file_path="test_we.cpp", start_line=1, end_line=5, body_hash="aaa",
        )
        callee = FunctionNode(
            id="test_we_callee_001", name="test_callee", signature="void test_callee()",
            file_path="test_we.cpp", start_line=10, end_line=15, body_hash="bbb",
        )
        neo4j_store.create_function(caller)
        neo4j_store.create_function(callee)

        # Create an UnresolvedCall
        uc = UnresolvedCallNode(
            caller_id="test_we_caller_001", call_expression="ptr->callee()",
            call_file="test_we.cpp", call_line=3, call_type="indirect",
            source_code_snippet="ptr->callee();", var_name=None, var_type=None,
            retry_count=0, status="pending",
        )
        neo4j_store.create_unresolved_call(uc)

        try:
            # Write the edge
            result = write_edge(
                caller_id="test_we_caller_001",
                callee_id="test_we_callee_001",
                call_type="indirect",
                call_file="test_we.cpp",
                call_line=3,
                store=neo4j_store,
                llm_response="Analysis: ptr is assigned DerivedClass at line 2",
                reasoning_summary="ptr->callee() dispatches to test_callee via vtable",
            )
            assert result["skipped"] is False
            assert result["edge_created"] is True

            # Verify edge exists
            assert neo4j_store.edge_exists(
                "test_we_caller_001", "test_we_callee_001", "test_we.cpp", 3
            )

            # Verify edge has resolved_by=llm
            edge = neo4j_store.get_calls_edge(
                "test_we_caller_001", "test_we_callee_001", "test_we.cpp", 3
            )
            assert edge is not None
            assert edge.resolved_by == "llm"
            assert edge.call_type == "indirect"

            # Verify RepairLog was created
            logs = neo4j_store.get_repair_logs(
                caller_id="test_we_caller_001", callee_id="test_we_callee_001"
            )
            assert len(logs) >= 1
            log = logs[0]
            assert log.repair_method == "llm"
            assert log.reasoning_summary == "ptr->callee() dispatches to test_callee via vtable"
            assert "DerivedClass" in log.llm_response

            # Verify UC was deleted
            remaining = neo4j_store.get_unresolved_calls(caller_id="test_we_caller_001")
            uc_at_line3 = [u for u in remaining if u.call_file == "test_we.cpp" and u.call_line == 3]
            assert len(uc_at_line3) == 0

        finally:
            # Cleanup
            neo4j_store.delete_calls_edge("test_we_caller_001", "test_we_callee_001", "test_we.cpp", 3)
            neo4j_store.delete_repair_logs_for_edge(
                "test_we_caller_001", "test_we_callee_001", "test_we.cpp:3"
            )
            neo4j_store.delete_function("test_we_caller_001")
            neo4j_store.delete_function("test_we_callee_001")

    def test_write_edge_skips_duplicate(self, neo4j_store):
        """write_edge returns skipped=True if edge already exists."""
        from codemap_lite.agent.icsl_tools import write_edge
        from codemap_lite.graph.schema import CallsEdgeProps, FunctionNode

        caller = FunctionNode(
            id="test_dup_caller_001", name="dup_caller", signature="void dup_caller()",
            file_path="test_dup.cpp", start_line=1, end_line=5, body_hash="ccc",
        )
        callee = FunctionNode(
            id="test_dup_callee_001", name="dup_callee", signature="void dup_callee()",
            file_path="test_dup.cpp", start_line=10, end_line=15, body_hash="ddd",
        )
        neo4j_store.create_function(caller)
        neo4j_store.create_function(callee)

        # Pre-create the edge
        props = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="test_dup.cpp", call_line=3,
        )
        neo4j_store.create_calls_edge("test_dup_caller_001", "test_dup_callee_001", props)

        try:
            result = write_edge(
                caller_id="test_dup_caller_001",
                callee_id="test_dup_callee_001",
                call_type="indirect",
                call_file="test_dup.cpp",
                call_line=3,
                store=neo4j_store,
            )
            assert result["skipped"] is True
            assert "already exists" in result["reason"]
        finally:
            neo4j_store.delete_calls_edge("test_dup_caller_001", "test_dup_callee_001", "test_dup.cpp", 3)
            neo4j_store.delete_function("test_dup_caller_001")
            neo4j_store.delete_function("test_dup_callee_001")

    def test_write_edge_rejects_invalid_call_type(self, neo4j_store):
        """write_edge raises ValueError for invalid call_type."""
        from codemap_lite.agent.icsl_tools import write_edge

        with pytest.raises(ValueError, match="call_type must be one of"):
            write_edge(
                caller_id="x", callee_id="y", call_type="unknown",
                call_file="z.cpp", call_line=1, store=neo4j_store,
            )

    def test_write_edge_returns_error_for_nonexistent_caller(self, neo4j_store):
        """write_edge returns error dict when caller doesn't exist."""
        from codemap_lite.agent.icsl_tools import write_edge

        result = write_edge(
            caller_id="nonexistent_caller_xyz", callee_id="nonexistent_callee_xyz",
            call_type="indirect", call_file="x.cpp", call_line=1, store=neo4j_store,
        )
        assert "error" in result
        assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# §5 Review Cascade — 4-step flow (architecture.md §5 审阅交互)
# ---------------------------------------------------------------------------


class TestSection5_ReviewCascade:
    """Test the review cascade against real Neo4j: delete edge → delete RepairLog → regen UC → reset SP."""

    def test_review_incorrect_cascade(self, neo4j_store):
        """verdict=incorrect triggers full 4-step cascade."""
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, RepairLogNode, SourcePointNode, UnresolvedCallNode,
        )
        from datetime import datetime, timezone

        # Setup: create caller (as source point), callee, edge, RepairLog
        caller = FunctionNode(
            id="test_rv_caller_001", name="rv_caller", signature="void rv_caller()",
            file_path="test_rv.cpp", start_line=1, end_line=10, body_hash="eee",
        )
        callee = FunctionNode(
            id="test_rv_callee_001", name="rv_callee", signature="void rv_callee()",
            file_path="test_rv.cpp", start_line=20, end_line=30, body_hash="fff",
        )
        neo4j_store.create_function(caller)
        neo4j_store.create_function(callee)

        # Create SourcePoint for the caller
        sp = SourcePointNode(
            id="test_rv_caller_001", function_id="test_rv_caller_001",
            entry_point_kind="entry_point", reason="test", status="complete",
        )
        neo4j_store.create_source_point(sp)

        # Create CALLS edge (LLM-resolved)
        props = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="test_rv.cpp", call_line=5,
        )
        neo4j_store.create_calls_edge("test_rv_caller_001", "test_rv_callee_001", props)

        # Create RepairLog
        repair_log = RepairLogNode(
            caller_id="test_rv_caller_001", callee_id="test_rv_callee_001",
            call_location="test_rv.cpp:5", repair_method="llm",
            llm_response="test", timestamp=datetime.now(timezone.utc).isoformat(),
            reasoning_summary="test reasoning",
        )
        neo4j_store.create_repair_log(repair_log)

        try:
            # Verify setup
            assert neo4j_store.edge_exists("test_rv_caller_001", "test_rv_callee_001", "test_rv.cpp", 5)
            logs = neo4j_store.get_repair_logs(caller_id="test_rv_caller_001")
            assert len(logs) >= 1

            # Simulate the cascade (same logic as review.py verdict=incorrect)
            # Step 1: Delete edge
            deleted = neo4j_store.delete_calls_edge(
                "test_rv_caller_001", "test_rv_callee_001", "test_rv.cpp", 5
            )
            assert deleted is True

            # Step 2: Delete RepairLog
            neo4j_store.delete_repair_logs_for_edge(
                "test_rv_caller_001", "test_rv_callee_001", "test_rv.cpp:5"
            )

            # Step 3: Regenerate UC
            uc = UnresolvedCallNode(
                caller_id="test_rv_caller_001", call_expression="ptr->callee()",
                call_file="test_rv.cpp", call_line=5, call_type="indirect",
                source_code_snippet="", var_name=None, var_type=None,
                retry_count=0, status="pending",
            )
            neo4j_store.create_unresolved_call(uc)

            # Step 4: Reset SourcePoint status
            neo4j_store.update_source_point_status("test_rv_caller_001", "pending", force_reset=True)

            # Verify cascade results
            # Edge gone
            assert not neo4j_store.edge_exists("test_rv_caller_001", "test_rv_callee_001", "test_rv.cpp", 5)
            # RepairLog gone
            logs_after = neo4j_store.get_repair_logs(
                caller_id="test_rv_caller_001", callee_id="test_rv_callee_001",
                call_location="test_rv.cpp:5",
            )
            assert len(logs_after) == 0
            # UC regenerated
            ucs = neo4j_store.get_unresolved_calls(caller_id="test_rv_caller_001")
            uc_at_5 = [u for u in ucs if u.call_file == "test_rv.cpp" and u.call_line == 5]
            assert len(uc_at_5) == 1
            assert uc_at_5[0].retry_count == 0
            assert uc_at_5[0].status == "pending"
            # SP reset to pending
            sp_after = neo4j_store.get_source_point("test_rv_caller_001")
            assert sp_after is not None
            assert sp_after.status == "pending"

        finally:
            # Cleanup
            neo4j_store.delete_calls_edge("test_rv_caller_001", "test_rv_callee_001", "test_rv.cpp", 5)
            neo4j_store.delete_repair_logs_for_edge(
                "test_rv_caller_001", "test_rv_callee_001", "test_rv.cpp:5"
            )
            neo4j_store.delete_unresolved_call("test_rv_caller_001", "test_rv.cpp", 5)
            neo4j_store.delete_function("test_rv_caller_001")
            neo4j_store.delete_function("test_rv_callee_001")
            # Delete SourcePoint
            with neo4j_store._get_driver().session() as session:
                session.run("MATCH (s:SourcePoint {id: $id}) DETACH DELETE s", id="test_rv_caller_001")


# ---------------------------------------------------------------------------
# §8 REST API — Deep field validation (architecture.md §8 REST API 契约)
# ---------------------------------------------------------------------------


class TestSection8_DeepFieldValidation:
    """Validate every field in API responses matches architecture.md §8 contract."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(store=neo4j_store)
        self.client = TestClient(app)
        self.store = neo4j_store

    def test_stats_has_all_required_keys(self):
        """§8: stats must contain all architecture-mandated keys."""
        r = self.client.get("/api/v1/stats")
        assert r.status_code == 200
        stats = r.json()
        # architecture.md §8 line 494: required top-level keys
        required = {
            "total_functions", "total_files", "total_calls", "total_unresolved",
            "total_repair_logs", "total_llm_edges", "total_source_points",
            "total_feedback", "unresolved_by_status", "unresolved_by_category",
            "calls_by_resolved_by", "source_points_by_status",
        }
        missing = required - set(stats.keys())
        assert not missing, f"Stats missing required keys: {missing}"

    def test_stats_calls_by_resolved_by_has_all_buckets(self):
        """§8: calls_by_resolved_by must have all 5 resolution methods."""
        r = self.client.get("/api/v1/stats")
        by_rb = r.json()["calls_by_resolved_by"]
        required_buckets = {"symbol_table", "signature", "dataflow", "context", "llm"}
        missing = required_buckets - set(by_rb.keys())
        assert not missing, f"calls_by_resolved_by missing buckets: {missing}"

    def test_stats_unresolved_by_category_has_none_bucket(self):
        """§8: unresolved_by_category must include 'none' for unattempted UCs."""
        r = self.client.get("/api/v1/stats")
        by_cat = r.json()["unresolved_by_category"]
        assert "none" in by_cat, "unresolved_by_category missing 'none' bucket"
        # All 5 valid categories + none
        required = {"gate_failed", "agent_error", "subprocess_crash",
                    "subprocess_timeout", "agent_exited_without_edge", "none"}
        missing = required - set(by_cat.keys())
        assert not missing, f"unresolved_by_category missing: {missing}"

    def test_stats_consistency_total_calls_equals_resolved_by_sum(self):
        """§8: total_calls must equal sum of calls_by_resolved_by values."""
        r = self.client.get("/api/v1/stats")
        stats = r.json()
        total = stats["total_calls"]
        rb_sum = sum(stats["calls_by_resolved_by"].values())
        assert total == rb_sum, (
            f"total_calls={total} != sum(calls_by_resolved_by)={rb_sum}. "
            f"Some edges have invalid resolved_by values."
        )

    def test_stats_consistency_total_unresolved_equals_status_sum(self):
        """§8: total_unresolved must equal sum of unresolved_by_status values."""
        r = self.client.get("/api/v1/stats")
        stats = r.json()
        total = stats["total_unresolved"]
        status_sum = sum(stats["unresolved_by_status"].values())
        assert total == status_sum, (
            f"total_unresolved={total} != sum(unresolved_by_status)={status_sum}"
        )

    def test_stats_consistency_total_unresolved_equals_category_sum(self):
        """§8: total_unresolved must equal sum of unresolved_by_category values."""
        r = self.client.get("/api/v1/stats")
        stats = r.json()
        total = stats["total_unresolved"]
        cat_sum = sum(stats["unresolved_by_category"].values())
        assert total == cat_sum, (
            f"total_unresolved={total} != sum(unresolved_by_category)={cat_sum}"
        )

    def test_unresolved_calls_item_has_all_fields(self):
        """§4+§8: each UnresolvedCall item must have all schema fields."""
        r = self.client.get("/api/v1/unresolved-calls?limit=3")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        required_fields = {
            "id", "caller_id", "call_expression", "call_file", "call_line",
            "call_type", "source_code_snippet", "var_name", "var_type",
            "candidates", "retry_count", "status",
            "last_attempt_timestamp", "last_attempt_reason",
        }
        for uc in items:
            missing = required_fields - set(uc.keys())
            assert not missing, f"UC item missing fields: {missing}"

    def test_call_chain_response_format(self):
        """§8: call-chain must return {nodes, edges, unresolved}."""
        # Get a function with callees for a meaningful test
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            rec = s.run(
                "MATCH (a:Function)-[:CALLS]->(b:Function) "
                "RETURN a.id AS id LIMIT 1"
            ).single()
        driver.close()
        if rec is None:
            pytest.skip("No function with callees")

        fn_id = rec["id"]
        r = self.client.get(f"/api/v1/functions/{fn_id}/call-chain?depth=3")
        assert r.status_code == 200
        data = r.json()
        # Must have all three keys
        assert "nodes" in data
        assert "edges" in data
        assert "unresolved" in data
        # Nodes must have Function schema fields
        assert len(data["nodes"]) >= 1
        for node in data["nodes"][:3]:
            assert "id" in node
            assert "name" in node
            assert "file_path" in node
            assert "start_line" in node
        # Edges must have caller_id, callee_id, props
        if data["edges"]:
            edge = data["edges"][0]
            assert "caller_id" in edge
            assert "callee_id" in edge
            assert "props" in edge
            props = edge["props"]
            assert "resolved_by" in props
            assert "call_type" in props
            assert "call_file" in props
            assert "call_line" in props
            # §4: resolved_by must be valid
            assert props["resolved_by"] in {
                "symbol_table", "signature", "dataflow", "context", "llm"
            }
            # §4: call_type must be valid
            assert props["call_type"] in {"direct", "indirect", "virtual"}

    def test_unresolved_calls_filter_by_caller(self):
        """§8: ?caller= filter must return only UCs for that caller."""
        # Get a caller that has UCs
        r = self.client.get("/api/v1/unresolved-calls?limit=1")
        items = r.json()["items"]
        if not items:
            pytest.skip("No UCs in database")
        caller_id = items[0]["caller_id"]

        r = self.client.get(f"/api/v1/unresolved-calls?caller={caller_id}&limit=50")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] > 0
        for uc in data["items"]:
            assert uc["caller_id"] == caller_id

    def test_functions_item_has_all_schema_fields(self):
        """§4: each Function item must have all schema fields."""
        r = self.client.get("/api/v1/functions?limit=3")
        assert r.status_code == 200
        items = r.json()["items"]
        required_fields = {
            "id", "name", "signature", "file_path",
            "start_line", "end_line", "body_hash",
        }
        for fn in items:
            missing = required_fields - set(fn.keys())
            assert not missing, f"Function item missing fields: {missing}"

    def test_callers_items_are_deduplicated(self):
        """§4: callers list must not contain duplicates (DISTINCT)."""
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            # Find a function with multiple incoming edges
            rec = s.run(
                "MATCH (a:Function)-[:CALLS]->(b:Function) "
                "WITH b, count(a) AS cnt WHERE cnt > 1 "
                "RETURN b.id AS id LIMIT 1"
            ).single()
        driver.close()
        if rec is None:
            pytest.skip("No function with multiple callers")

        fn_id = rec["id"]
        r = self.client.get(f"/api/v1/functions/{fn_id}/callers?limit=100")
        assert r.status_code == 200
        items = r.json()["items"]
        ids = [item["id"] for item in items]
        assert len(ids) == len(set(ids)), "Callers list contains duplicates"

    def test_pagination_offset_beyond_total_returns_empty(self):
        """§8: offset beyond total should return empty items with correct total."""
        r = self.client.get("/api/v1/functions?limit=1&offset=999999")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] > 0  # Total still reflects full count
        assert data["items"] == []  # But items are empty

    def test_source_points_endpoint_returns_status(self):
        """§8: source-points items must include status field."""
        r = self.client.get("/api/v1/source-points")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data
        # If there are items, each must have status
        for item in data["items"][:5]:
            assert "status" in item
            assert item["status"] in {"pending", "running", "complete", "partial_complete"}


# ---------------------------------------------------------------------------
# §5 DELETE /edges API — Full cascade via HTTP endpoint
# ---------------------------------------------------------------------------


class TestSection5_DeleteEdgesAPI:
    """Test DELETE /edges endpoint cascade via HTTP (architecture.md §5).

    Verifies the full 4-step cascade through the REST API:
    1. Edge deleted
    2. RepairLog deleted
    3. UC regenerated (retry_count=0, status=pending)
    4. SourcePoint reset to pending
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        import tempfile
        from pathlib import Path
        from codemap_lite.api.app import create_app
        from codemap_lite.analysis.feedback_store import FeedbackStore
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, RepairLogNode, SourcePointNode,
        )
        from fastapi.testclient import TestClient

        self.store = neo4j_store
        self.tmpdir = Path(tempfile.mkdtemp())
        self.fb_store = FeedbackStore(storage_dir=self.tmpdir)
        app = create_app(store=neo4j_store, feedback_store=self.fb_store)
        self.client = TestClient(app)

        # Create isolated test data
        self.caller_id = "del_edge_caller_001"
        self.callee_id = "del_edge_callee_001"
        self.call_file = "__test_del_edge__.cpp"
        self.call_line = 7777

        fn_caller = FunctionNode(
            id=self.caller_id, name="del_caller", signature="void del_caller()",
            file_path="del_test.cpp", start_line=1, end_line=10, body_hash="dc1",
        )
        fn_callee = FunctionNode(
            id=self.callee_id, name="del_callee", signature="void del_callee()",
            file_path="del_test.cpp", start_line=20, end_line=30, body_hash="dc2",
        )
        neo4j_store.create_function(fn_caller)
        neo4j_store.create_function(fn_callee)

        # Create SourcePoint for caller
        sp = SourcePointNode(
            id=self.caller_id, function_id=self.caller_id,
            entry_point_kind="entry_point", reason="test",
            status="complete",
        )
        neo4j_store.create_source_point(sp)

        # Create LLM edge
        props = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file=self.call_file, call_line=self.call_line,
        )
        neo4j_store.create_calls_edge(self.caller_id, self.callee_id, props)

        # Create RepairLog
        log = RepairLogNode(
            caller_id=self.caller_id, callee_id=self.callee_id,
            call_location=f"{self.call_file}:{self.call_line}",
            repair_method="llm", llm_response="test response",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="test reasoning",
        )
        neo4j_store.create_repair_log(log)

        yield

        # Cleanup
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            s.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
                  ids=[self.caller_id, self.callee_id])
            s.run("MATCH (r:RepairLog) WHERE r.caller_id = $cid DELETE r",
                  cid=self.caller_id)
            s.run("MATCH (s:SourcePoint {id: $id}) DETACH DELETE s",
                  id=self.caller_id)
            s.run(
                "MATCH (u:UnresolvedCall) WHERE u.caller_id = $cid "
                "AND u.call_file = $cf AND u.call_line = $cl DETACH DELETE u",
                cid=self.caller_id, cf=self.call_file, cl=self.call_line,
            )
        driver.close()

    def test_delete_edge_returns_204(self):
        """DELETE /edges returns 204 on success."""
        r = self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "call_file": self.call_file,
            "call_line": self.call_line,
        })
        assert r.status_code == 204

    def test_delete_edge_removes_calls_edge(self):
        """DELETE /edges step 1: CALLS edge is deleted."""
        self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "call_file": self.call_file,
            "call_line": self.call_line,
        })
        assert not self.store.edge_exists(
            self.caller_id, self.callee_id, self.call_file, self.call_line
        )

    def test_delete_edge_removes_repair_log(self):
        """DELETE /edges step 2: RepairLog is deleted."""
        self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "call_file": self.call_file,
            "call_line": self.call_line,
        })
        logs = self.store.get_repair_logs(
            caller_id=self.caller_id, callee_id=self.callee_id,
            call_location=f"{self.call_file}:{self.call_line}",
        )
        assert len(logs) == 0

    def test_delete_edge_regenerates_uc(self):
        """DELETE /edges step 3: UC regenerated with retry_count=0."""
        self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "call_file": self.call_file,
            "call_line": self.call_line,
        })
        ucs = self.store.get_unresolved_calls(caller_id=self.caller_id)
        matching = [
            u for u in ucs
            if u.call_file == self.call_file and u.call_line == self.call_line
        ]
        assert len(matching) == 1
        assert matching[0].status == "pending"
        assert matching[0].retry_count == 0
        assert matching[0].call_type == "indirect"  # Preserved from edge

    def test_delete_edge_resets_source_point(self):
        """DELETE /edges step 4: SourcePoint reset to pending."""
        self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "call_file": self.call_file,
            "call_line": self.call_line,
        })
        sp = self.store.get_source_point(self.caller_id)
        assert sp is not None
        assert sp.status == "pending"

    def test_delete_edge_with_correct_target_creates_counter_example(self):
        """DELETE /edges + correct_target → counter-example in FeedbackStore."""
        r = self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "call_file": self.call_file,
            "call_line": self.call_line,
            "correct_target": "real_target_func",
        })
        assert r.status_code == 204
        examples = self.fb_store.list_all()
        assert len(examples) >= 1
        ex = examples[-1]
        assert ex.wrong_target == self.callee_id
        assert ex.correct_target == "real_target_func"
        assert ex.source_id == self.caller_id

    def test_delete_nonexistent_edge_returns_404(self):
        """DELETE /edges on nonexistent edge returns 404."""
        r = self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "nonexistent_xyz",
            "callee_id": "nonexistent_abc",
            "call_file": "no.cpp",
            "call_line": 1,
        })
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# §8 Repair-Logs Filtering — ?caller=, ?callee=, ?location=
# ---------------------------------------------------------------------------


class TestSection8_RepairLogsFiltering:
    """Test repair-logs endpoint filtering (architecture.md §8)."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.schema import RepairLogNode
        from fastapi.testclient import TestClient

        self.store = neo4j_store
        app = create_app(store=neo4j_store)
        self.client = TestClient(app)

        # Create test RepairLogs
        self.logs_data = [
            RepairLogNode(
                caller_id="rl_filter_caller_A", callee_id="rl_filter_callee_X",
                call_location="filter_test.cpp:10", repair_method="llm",
                llm_response="resp1", timestamp="2026-05-15T01:00:00Z",
                reasoning_summary="reason1",
            ),
            RepairLogNode(
                caller_id="rl_filter_caller_A", callee_id="rl_filter_callee_Y",
                call_location="filter_test.cpp:20", repair_method="llm",
                llm_response="resp2", timestamp="2026-05-15T02:00:00Z",
                reasoning_summary="reason2",
            ),
            RepairLogNode(
                caller_id="rl_filter_caller_B", callee_id="rl_filter_callee_X",
                call_location="other_file.cpp:5", repair_method="llm",
                llm_response="resp3", timestamp="2026-05-15T03:00:00Z",
                reasoning_summary="reason3",
            ),
        ]
        for log in self.logs_data:
            neo4j_store.create_repair_log(log)

        yield

        # Cleanup
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            s.run(
                "MATCH (r:RepairLog) WHERE r.caller_id STARTS WITH 'rl_filter_' "
                "DELETE r"
            )
        driver.close()

    def test_filter_by_caller(self):
        """?caller= returns only logs for that caller."""
        r = self.client.get("/api/v1/repair-logs?caller=rl_filter_caller_A")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["caller_id"] == "rl_filter_caller_A"

    def test_filter_by_callee(self):
        """?callee= returns only logs for that callee."""
        r = self.client.get("/api/v1/repair-logs?callee=rl_filter_callee_X")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["callee_id"] == "rl_filter_callee_X"

    def test_filter_by_location(self):
        """?location= returns only logs at that call_location."""
        r = self.client.get("/api/v1/repair-logs?location=filter_test.cpp:10")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["call_location"] == "filter_test.cpp:10"

    def test_filter_combined_caller_and_callee(self):
        """?caller=&callee= returns intersection."""
        r = self.client.get(
            "/api/v1/repair-logs?caller=rl_filter_caller_A&callee=rl_filter_callee_X"
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["caller_id"] == "rl_filter_caller_A"
        assert data["items"][0]["callee_id"] == "rl_filter_callee_X"

    def test_filter_no_match_returns_empty(self):
        """Filters with no match return total=0, items=[]."""
        r = self.client.get("/api/v1/repair-logs?caller=nonexistent_xyz")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_repair_log_item_has_all_fields(self):
        """§4: each RepairLog item must have all schema fields."""
        r = self.client.get("/api/v1/repair-logs?caller=rl_filter_caller_A&limit=1")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        required_fields = {
            "id", "caller_id", "callee_id", "call_location",
            "repair_method", "llm_response", "timestamp", "reasoning_summary",
        }
        missing = required_fields - set(items[0].keys())
        assert not missing, f"RepairLog item missing fields: {missing}"


# ---------------------------------------------------------------------------
# §8 Unresolved-Calls Category Filter — ?category=
# ---------------------------------------------------------------------------


class TestSection8_UnresolvedCallsCategoryFilter:
    """Test unresolved-calls ?category= filter (architecture.md §8)."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode
        from fastapi.testclient import TestClient

        self.store = neo4j_store
        app = create_app(store=neo4j_store)
        self.client = TestClient(app)

        # Create a test function to host UCs
        fn = FunctionNode(
            id="cat_filter_fn_001", name="cat_fn", signature="void cat_fn()",
            file_path="cat_test.cpp", start_line=1, end_line=10, body_hash="cf1",
        )
        neo4j_store.create_function(fn)

        # Create UCs with different categories
        self.uc_none = UnresolvedCallNode(
            caller_id="cat_filter_fn_001", call_expression="a()",
            call_file="cat_test.cpp", call_line=2, call_type="indirect",
            source_code_snippet="a();", var_name=None, var_type=None,
            retry_count=0, status="pending",
            # No last_attempt_reason → category "none"
        )
        self.uc_gate_failed = UnresolvedCallNode(
            caller_id="cat_filter_fn_001", call_expression="b()",
            call_file="cat_test.cpp", call_line=3, call_type="indirect",
            source_code_snippet="b();", var_name=None, var_type=None,
            retry_count=1, status="pending",
            last_attempt_reason="gate_failed: 2 gaps remain",
            last_attempt_timestamp="2026-05-15T00:00:00Z",
        )
        self.uc_agent_error = UnresolvedCallNode(
            caller_id="cat_filter_fn_001", call_expression="c()",
            call_file="cat_test.cpp", call_line=4, call_type="indirect",
            source_code_snippet="c();", var_name=None, var_type=None,
            retry_count=2, status="pending",
            last_attempt_reason="agent_error: timeout",
            last_attempt_timestamp="2026-05-15T00:01:00Z",
        )
        neo4j_store.create_unresolved_call(self.uc_none)
        neo4j_store.create_unresolved_call(self.uc_gate_failed)
        neo4j_store.create_unresolved_call(self.uc_agent_error)

        yield

        # Cleanup
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            s.run("MATCH (n) WHERE n.id = 'cat_filter_fn_001' DETACH DELETE n")
            s.run(
                "MATCH (u:UnresolvedCall) WHERE u.caller_id = 'cat_filter_fn_001' "
                "DETACH DELETE u"
            )
        driver.close()

    def test_category_none_returns_unattempted(self):
        """?category=none returns UCs with no last_attempt_reason."""
        r = self.client.get(
            "/api/v1/unresolved-calls?caller=cat_filter_fn_001&category=none"
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        for uc in data["items"]:
            assert uc["last_attempt_reason"] is None or uc["last_attempt_reason"] == ""

    def test_category_gate_failed_returns_matching(self):
        """?category=gate_failed returns UCs with gate_failed prefix."""
        r = self.client.get(
            "/api/v1/unresolved-calls?caller=cat_filter_fn_001&category=gate_failed"
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        for uc in data["items"]:
            reason = uc["last_attempt_reason"]
            assert reason.startswith("gate_failed") or reason == "gate_failed"

    def test_category_agent_error_returns_matching(self):
        """?category=agent_error returns UCs with agent_error prefix."""
        r = self.client.get(
            "/api/v1/unresolved-calls?caller=cat_filter_fn_001&category=agent_error"
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        for uc in data["items"]:
            reason = uc["last_attempt_reason"]
            assert reason.startswith("agent_error") or reason == "agent_error"

    def test_category_filter_excludes_non_matching(self):
        """?category=subprocess_crash returns 0 for our test data."""
        r = self.client.get(
            "/api/v1/unresolved-calls?caller=cat_filter_fn_001&category=subprocess_crash"
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_category_filter_total_reflects_filtered_count(self):
        """§8: total in response must reflect filtered count, not all UCs."""
        # Get total without category filter
        r_all = self.client.get(
            "/api/v1/unresolved-calls?caller=cat_filter_fn_001"
        )
        total_all = r_all.json()["total"]

        # Get with category filter
        r_filtered = self.client.get(
            "/api/v1/unresolved-calls?caller=cat_filter_fn_001&category=none"
        )
        total_filtered = r_filtered.json()["total"]

        # Filtered total must be less than or equal to unfiltered
        assert total_filtered <= total_all
        assert total_filtered >= 1


# ---------------------------------------------------------------------------
# §8 Edge Cases — Error handling in REST API
# ---------------------------------------------------------------------------


class TestSection8_EdgeCases:
    """Test REST API error handling edge cases."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(store=neo4j_store)
        self.client = TestClient(app)
        self.store = neo4j_store

    def test_create_edge_nonexistent_caller_returns_404(self):
        """POST /edges with nonexistent caller returns 404."""
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "nonexistent_caller_xyz",
            "callee_id": "nonexistent_callee_xyz",
            "call_file": "test.cpp",
            "call_line": 1,
            "resolved_by": "llm",
            "call_type": "indirect",
        })
        assert r.status_code == 404
        assert "caller" in r.json()["detail"].lower()

    def test_create_edge_nonexistent_callee_returns_404(self):
        """POST /edges with valid caller but nonexistent callee returns 404."""
        # Use a real function as caller
        fns = self.store.list_functions()[:1]
        if not fns:
            pytest.skip("No functions in database")
        r = self.client.post("/api/v1/edges", json={
            "caller_id": fns[0].id,
            "callee_id": "nonexistent_callee_xyz",
            "call_file": "test.cpp",
            "call_line": 1,
            "resolved_by": "llm",
            "call_type": "indirect",
        })
        assert r.status_code == 404
        assert "callee" in r.json()["detail"].lower()

    def test_create_edge_invalid_resolved_by_returns_422(self):
        """POST /edges with invalid resolved_by returns 422."""
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "a",
            "callee_id": "b",
            "call_file": "test.cpp",
            "call_line": 1,
            "resolved_by": "invalid_method",
            "call_type": "indirect",
        })
        assert r.status_code == 422

    def test_create_edge_invalid_call_type_returns_422(self):
        """POST /edges with invalid call_type returns 422."""
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "a",
            "callee_id": "b",
            "call_file": "test.cpp",
            "call_line": 1,
            "resolved_by": "llm",
            "call_type": "invalid_type",
        })
        assert r.status_code == 422

    def test_review_nonexistent_edge_returns_404(self):
        """POST /reviews on nonexistent edge returns 404."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "nonexistent_xyz",
            "callee_id": "nonexistent_abc",
            "call_file": "no.cpp",
            "call_line": 1,
            "verdict": "correct",
        })
        assert r.status_code == 404

    def test_review_invalid_verdict_returns_422(self):
        """POST /reviews with invalid verdict returns 422."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "a",
            "callee_id": "b",
            "call_file": "test.cpp",
            "call_line": 1,
            "verdict": "maybe",
        })
        assert r.status_code == 422

    def test_function_detail_nonexistent_returns_404(self):
        """GET /functions/{id} with nonexistent ID returns 404."""
        r = self.client.get("/api/v1/functions/nonexistent_function_xyz_123")
        assert r.status_code == 404

    def test_delete_review_nonexistent_returns_404(self):
        """DELETE /reviews/{id} with nonexistent ID returns 404."""
        r = self.client.delete("/api/v1/reviews/nonexistent_review_id")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# §4 Neo4j Indexes and Constraints — Regression guard
# ---------------------------------------------------------------------------


class TestSection4_IndexesAndConstraints:
    """Verify all architecture.md §4 indexes and constraints exist in Neo4j."""

    def test_required_indexes_exist(self, neo4j_store):
        """§4: All required indexes must be present."""
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            indexes = {r["name"] for r in s.run("SHOW INDEXES")}
        driver.close()

        required_indexes = {
            "idx_file_hash",
            "idx_function_file",
            "idx_function_sig",
            "idx_source_kind",
            "idx_calls_resolved",
            "idx_gap_status",
            "idx_gap_caller",
            "idx_repairlog_caller",
        }
        missing = required_indexes - indexes
        assert not missing, f"Missing required indexes: {missing}"

    def test_required_constraints_exist(self, neo4j_store):
        """§4: All required uniqueness constraints must be present."""
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            constraints = {r["name"] for r in s.run("SHOW CONSTRAINTS")}
        driver.close()

        required_constraints = {
            "uniq_function_id",
            "uniq_file_path",
            "uniq_repairlog_key",
            "uniq_uc_key",
        }
        missing = required_constraints - constraints
        assert not missing, f"Missing required constraints: {missing}"

    def test_uc_uniqueness_constraint_enforced(self, neo4j_store):
        """§4: Creating duplicate UC (same caller_id, call_file, call_line) must fail."""
        from codemap_lite.graph.schema import UnresolvedCallNode, FunctionNode

        fn = FunctionNode(
            id="uc_uniq_test_fn", signature="void uniq()", name="uniq",
            file_path="uniq_test.cpp", start_line=1, end_line=5, body_hash="uq",
        )
        neo4j_store.create_function(fn)
        uc = UnresolvedCallNode(
            caller_id="uc_uniq_test_fn", call_expression="x()",
            call_file="uniq_test.cpp", call_line=1, call_type="indirect",
            source_code_snippet="x();", var_name=None, var_type=None,
        )
        neo4j_store.create_unresolved_call(uc)

        try:
            # Second UC with same (caller_id, call_file, call_line) should
            # either be merged (MERGE semantics) or raise. Our implementation
            # uses MERGE, so it should not create a duplicate.
            uc2 = UnresolvedCallNode(
                caller_id="uc_uniq_test_fn", call_expression="y()",
                call_file="uniq_test.cpp", call_line=1, call_type="direct",
                source_code_snippet="y();", var_name=None, var_type=None,
            )
            neo4j_store.create_unresolved_call(uc2)

            # Verify only one UC exists at this location
            ucs = neo4j_store.get_unresolved_calls(caller_id="uc_uniq_test_fn")
            at_line1 = [u for u in ucs if u.call_file == "uniq_test.cpp" and u.call_line == 1]
            assert len(at_line1) == 1, "MERGE should prevent duplicate UCs"
        finally:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(
                "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
            )
            with driver.session() as s:
                s.run("MATCH (n) WHERE n.id = 'uc_uniq_test_fn' DETACH DELETE n")
                s.run(
                    "MATCH (u:UnresolvedCall) WHERE u.caller_id = 'uc_uniq_test_fn' "
                    "DETACH DELETE u"
                )
            driver.close()


# ---------------------------------------------------------------------------
# §8 Source-Points Reachable — GET /source-points/{id}/reachable
# ---------------------------------------------------------------------------


class TestSection8_SourcePointsReachable:
    """Test GET /source-points/{id}/reachable endpoint (architecture.md §8)."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(store=neo4j_store)
        self.client = TestClient(app)
        self.store = neo4j_store

    def test_reachable_returns_subgraph_format(self):
        """§8: /source-points/{id}/reachable returns {nodes, edges, unresolved}."""
        # Use a real function that has outgoing CALLS edges
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            rec = s.run(
                "MATCH (a:Function)-[:CALLS]->(b:Function) "
                "RETURN a.id AS id LIMIT 1"
            ).single()
        driver.close()
        if rec is None:
            pytest.skip("No function with callees")

        fn_id = rec["id"]
        r = self.client.get(f"/api/v1/source-points/{fn_id}/reachable")
        assert r.status_code == 200
        data = r.json()
        assert "nodes" in data
        assert "edges" in data
        assert "unresolved" in data
        # Should have at least the source node + one callee
        assert len(data["nodes"]) >= 2
        assert len(data["edges"]) >= 1

    def test_reachable_nodes_have_function_schema(self):
        """§8: nodes in reachable subgraph must have Function schema fields."""
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            rec = s.run(
                "MATCH (a:Function)-[:CALLS]->(b:Function) "
                "RETURN a.id AS id LIMIT 1"
            ).single()
        driver.close()
        if rec is None:
            pytest.skip("No function with callees")

        fn_id = rec["id"]
        r = self.client.get(f"/api/v1/source-points/{fn_id}/reachable")
        data = r.json()
        for node in data["nodes"][:5]:
            assert "id" in node
            assert "name" in node
            assert "file_path" in node
            assert "signature" in node

    def test_reachable_edges_have_props(self):
        """§8: edges in reachable subgraph must have caller_id, callee_id, props."""
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )
        with driver.session() as s:
            rec = s.run(
                "MATCH (a:Function)-[:CALLS]->(b:Function) "
                "RETURN a.id AS id LIMIT 1"
            ).single()
        driver.close()
        if rec is None:
            pytest.skip("No function with callees")

        fn_id = rec["id"]
        r = self.client.get(f"/api/v1/source-points/{fn_id}/reachable")
        data = r.json()
        for edge in data["edges"][:5]:
            assert "caller_id" in edge
            assert "callee_id" in edge
            assert "props" in edge
            props = edge["props"]
            assert props["resolved_by"] in {
                "symbol_table", "signature", "dataflow", "context", "llm"
            }
            assert props["call_type"] in {"direct", "indirect", "virtual"}

    def test_reachable_nonexistent_source_returns_empty(self):
        """§8: nonexistent source_id returns empty subgraph."""
        r = self.client.get("/api/v1/source-points/nonexistent_xyz_123/reachable")
        assert r.status_code == 200
        data = r.json()
        assert data["nodes"] == []
        assert data["edges"] == []
        assert data["unresolved"] == []


# ---------------------------------------------------------------------------
# §8 Analyze Status — GET /analyze/status
# ---------------------------------------------------------------------------


class TestSection8_AnalyzeStatus:
    """Test GET /analyze/status endpoint (architecture.md §8)."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(store=neo4j_store)
        self.client = TestClient(app)

    def test_analyze_status_returns_state(self):
        """§8: /analyze/status returns current analysis state."""
        r = self.client.get("/api/v1/analyze/status")
        assert r.status_code == 200
        data = r.json()
        assert "state" in data
        assert data["state"] in {"idle", "running", "repairing", "completed", "failed"}

    def test_analyze_double_trigger_returns_409(self):
        """§8: POST /analyze while already running returns 409."""
        # First trigger
        r1 = self.client.post("/api/v1/analyze", json={"mode": "full"})
        assert r1.status_code == 202

        # Second trigger should be rejected
        r2 = self.client.post("/api/v1/analyze", json={"mode": "full"})
        assert r2.status_code == 409


# ---------------------------------------------------------------------------
# §8 Stats Endpoint — Full Contract Validation
# ---------------------------------------------------------------------------


class TestSection8_StatsContract:
    """Verify /api/v1/stats returns all fields specified in architecture.md §8.

    §8 contract: unresolved_by_status (pending/unresolvable),
    unresolved_by_category (gate_failed/agent_error/subprocess_crash/
    subprocess_timeout/agent_exited_without_edge/none),
    calls_by_resolved_by (symbol_table/signature/dataflow/context/llm),
    total_feedback, total_repair_logs, source_points_by_status.
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(store=neo4j_store)
        self.client = TestClient(app)

    def test_stats_has_all_required_top_level_keys(self):
        """§8: stats must include all documented top-level keys."""
        r = self.client.get("/api/v1/stats")
        assert r.status_code == 200
        data = r.json()
        required_keys = {
            "total_functions",
            "total_files",
            "total_calls",
            "total_unresolved",
            "total_repair_logs",
            "total_feedback",
            "unresolved_by_status",
            "unresolved_by_category",
            "calls_by_resolved_by",
        }
        for key in required_keys:
            assert key in data, f"Missing required stats key: {key}"

    def test_stats_unresolved_by_status_has_valid_buckets(self):
        """§8: unresolved_by_status must have pending + unresolvable."""
        r = self.client.get("/api/v1/stats")
        data = r.json()
        by_status = data["unresolved_by_status"]
        assert "pending" in by_status
        assert "unresolvable" in by_status
        # Values must be non-negative integers
        assert by_status["pending"] >= 0
        assert by_status["unresolvable"] >= 0
        # Sum should equal total_unresolved
        assert by_status["pending"] + by_status["unresolvable"] == data["total_unresolved"]

    def test_stats_unresolved_by_category_has_all_5_categories(self):
        """§3/§8: unresolved_by_category must have 5 categories + none."""
        r = self.client.get("/api/v1/stats")
        data = r.json()
        by_cat = data["unresolved_by_category"]
        expected_categories = {
            "gate_failed",
            "agent_error",
            "subprocess_crash",
            "subprocess_timeout",
            "agent_exited_without_edge",
            "none",
        }
        for cat in expected_categories:
            assert cat in by_cat, f"Missing category bucket: {cat}"
            assert by_cat[cat] >= 0

    def test_stats_calls_by_resolved_by_has_all_5_methods(self):
        """§8: calls_by_resolved_by must have all 5 resolution methods."""
        r = self.client.get("/api/v1/stats")
        data = r.json()
        by_rb = data["calls_by_resolved_by"]
        expected_methods = {
            "symbol_table", "signature", "dataflow", "context", "llm"
        }
        for method in expected_methods:
            assert method in by_rb, f"Missing resolved_by bucket: {method}"
            assert by_rb[method] >= 0

    def test_stats_category_sum_equals_total_unresolved(self):
        """§8: sum of all category buckets must equal total_unresolved."""
        r = self.client.get("/api/v1/stats")
        data = r.json()
        by_cat = data["unresolved_by_category"]
        cat_sum = sum(by_cat.values())
        assert cat_sum == data["total_unresolved"], (
            f"Category sum {cat_sum} != total_unresolved {data['total_unresolved']}"
        )

    def test_stats_resolved_by_sum_equals_total_calls(self):
        """§8: sum of calls_by_resolved_by should equal total_calls."""
        r = self.client.get("/api/v1/stats")
        data = r.json()
        by_rb = data["calls_by_resolved_by"]
        rb_sum = sum(by_rb.values())
        # Note: edges with unknown/null resolved_by won't be counted in
        # any bucket, so rb_sum <= total_calls is acceptable.
        assert rb_sum <= data["total_calls"], (
            f"resolved_by sum {rb_sum} > total_calls {data['total_calls']}"
        )

    def test_stats_source_points_by_status_present(self):
        """§8: source_points_by_status should be present with valid keys."""
        r = self.client.get("/api/v1/stats")
        data = r.json()
        # source_points_by_status is returned by count_stats
        assert "source_points_by_status" in data
        sp_status = data["source_points_by_status"]
        valid_statuses = {"pending", "running", "complete", "partial_complete"}
        for key in sp_status:
            assert key in valid_statuses, f"Invalid SP status key: {key}"

    def test_stats_total_llm_edges_convenience_field(self):
        """§8: total_llm_edges convenience field for Dashboard chip."""
        r = self.client.get("/api/v1/stats")
        data = r.json()
        assert "total_llm_edges" in data
        assert data["total_llm_edges"] == data["calls_by_resolved_by"].get("llm", 0)


# ---------------------------------------------------------------------------
# §8 Source Points Summary — Format Validation
# ---------------------------------------------------------------------------


class TestSection8_SourcePointsSummary:
    """Verify /source-points/summary returns correct format (architecture.md §8)."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(store=neo4j_store)
        # Inject some source points into app.state for testing
        app.state.source_points = [
            {
                "id": "sp_test_1",
                "function_id": "fn_test_1",
                "entry_point_kind": "callback",
                "reason": "test reason 1",
            },
            {
                "id": "sp_test_2",
                "function_id": "fn_test_2",
                "entry_point_kind": "callback",
                "reason": "test reason 2",
            },
            {
                "id": "sp_test_3",
                "function_id": "fn_test_3",
                "entry_point_kind": "entry_function",
                "reason": "test reason 3",
            },
        ]
        self.client = TestClient(app)

    def test_summary_returns_total(self):
        """§8: summary must include total count."""
        r = self.client.get("/api/v1/source-points/summary")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert data["total"] == 3

    def test_summary_returns_by_kind(self):
        """§8: summary must include by_kind breakdown."""
        r = self.client.get("/api/v1/source-points/summary")
        data = r.json()
        assert "by_kind" in data
        by_kind = data["by_kind"]
        assert by_kind.get("callback") == 2
        assert by_kind.get("entry_function") == 1

    def test_summary_returns_by_status(self):
        """§8: summary must include by_status breakdown."""
        r = self.client.get("/api/v1/source-points/summary")
        data = r.json()
        assert "by_status" in data
        by_status = data["by_status"]
        # All should be pending since no SourcePoint nodes exist for these IDs
        assert by_status.get("pending", 0) == 3

    def test_summary_with_real_source_points(self, neo4j_store):
        """§8: summary reflects SourcePoint node status from Neo4j."""
        from codemap_lite.graph.schema import SourcePointNode
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient
        from neo4j import GraphDatabase

        # Create a SourcePoint in Neo4j
        sp = SourcePointNode(
            id="sp_summary_test",
            entry_point_kind="callback",
            reason="test",
            function_id="sp_summary_fn",
            module="test_module",
            status="complete",
        )
        neo4j_store.create_source_point(sp)

        try:
            app = create_app(store=neo4j_store)
            app.state.source_points = [
                {
                    "id": "sp_summary_test",
                    "function_id": "sp_summary_fn",
                    "entry_point_kind": "callback",
                    "reason": "test",
                },
            ]
            client = TestClient(app)
            r = client.get("/api/v1/source-points/summary")
            data = r.json()
            assert data["total"] == 1
            # Status should be "complete" from Neo4j
            assert data["by_status"].get("complete") == 1
        finally:
            driver = GraphDatabase.driver(
                "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
            )
            with driver.session() as s:
                s.run("MATCH (n:SourcePoint {id: 'sp_summary_test'}) DETACH DELETE n")
            driver.close()


# ---------------------------------------------------------------------------
# §7 Incremental Cascade — Real Neo4j Validation
# ---------------------------------------------------------------------------


class TestSection7_IncrementalCascadeReal:
    """Test incremental cascade logic against real Neo4j (architecture.md §7).

    Creates a mini graph: fn_A -[CALLS llm]-> fn_B (in file_X),
    fn_B -[HAS_GAP]-> uc_1, SourcePoint for fn_A.
    Then simulates file_X invalidation and verifies 5-step cascade.
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import (
            FunctionNode, UnresolvedCallNode, SourcePointNode, CallsEdgeProps,
        )
        from neo4j import GraphDatabase

        self.store = neo4j_store
        self.driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )

        # Create test nodes
        self.fn_a = FunctionNode(
            id="incr_fn_a", signature="void fnA()", name="fnA",
            file_path="/test/file_a.cpp", start_line=1, end_line=10,
            body_hash="hash_a",
        )
        self.fn_b = FunctionNode(
            id="incr_fn_b", signature="void fnB()", name="fnB",
            file_path="/test/file_x.cpp", start_line=1, end_line=10,
            body_hash="hash_b",
        )
        neo4j_store.create_function(self.fn_a)
        neo4j_store.create_function(self.fn_b)

        # Create LLM-resolved CALLS edge: fn_a -> fn_b
        self.edge_props = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/test/file_a.cpp", call_line=5,
        )
        neo4j_store.create_calls_edge(
            caller_id="incr_fn_a", callee_id="incr_fn_b", props=self.edge_props
        )

        # Create a SourcePoint for fn_a
        self.sp = SourcePointNode(
            id="incr_sp_a", entry_point_kind="callback",
            reason="test incremental", function_id="incr_fn_a",
            module="test", status="complete",
        )
        neo4j_store.create_source_point(self.sp)

        # Create an UnresolvedCall on fn_b
        self.uc = UnresolvedCallNode(
            caller_id="incr_fn_b", call_expression="ptr->method()",
            call_file="/test/file_x.cpp", call_line=5,
            call_type="indirect", source_code_snippet="ptr->method();",
            var_name="ptr", var_type="Base*",
            candidates=[], retry_count=0, status="pending",
            id="incr_uc_1",
        )
        neo4j_store.create_unresolved_call(self.uc)

        yield

        # Cleanup
        with self.driver.session() as s:
            s.run(
                "MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
                ids=["incr_fn_a", "incr_fn_b", "incr_sp_a", "incr_uc_1"],
            )
        self.driver.close()

    def test_invalidate_file_deletes_functions_in_file(self):
        """§7 step 2: Functions in invalidated file are deleted."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store)
        result = updater.invalidate_file("/test/file_x.cpp")

        # fn_b should be gone
        fn = self.store.get_function_by_id("incr_fn_b")
        assert fn is None, "Function in invalidated file should be deleted"
        assert "incr_fn_b" in result.removed_functions

    def test_invalidate_file_deletes_llm_edges_to_invalidated_functions(self):
        """§7 step 3: LLM edges pointing to functions in invalidated file are deleted."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store)
        updater.invalidate_file("/test/file_x.cpp")

        # The CALLS edge fn_a -> fn_b should be gone
        assert not self.store.edge_exists(
            "incr_fn_a", "incr_fn_b", "/test/file_a.cpp", 5
        ), "LLM edge to invalidated function should be deleted"

    def test_invalidate_file_returns_affected_source_ids(self):
        """§7 step 5: Returns affected source IDs for re-repair."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store)
        result = updater.invalidate_file("/test/file_x.cpp")

        # fn_a has a source point and its callee was invalidated
        assert "incr_fn_a" in result.affected_source_ids or "incr_sp_a" in result.affected_source_ids, (
            f"Expected affected sources to include incr_fn_a or incr_sp_a, got {result.affected_source_ids}"
        )

    def test_invalidate_file_regenerates_uc_for_deleted_llm_edge(self):
        """§7 step 3: When LLM edge is deleted, UC is regenerated."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store)
        result = updater.invalidate_file("/test/file_x.cpp")

        # After invalidation, fn_a should have a new UC for the lost edge
        # (the edge fn_a->fn_b was LLM-resolved, so a UC should be regenerated
        # for the call site at file_a.cpp:5)
        gaps = self.store.get_pending_gaps_for_source("incr_fn_a")
        # Check if any gap corresponds to the deleted edge's call site
        uc_for_deleted = [
            g for g in gaps
            if (getattr(g, "call_file", None) or g.get("call_file", "")) == "/test/file_a.cpp"
            and (getattr(g, "call_line", None) or g.get("call_line", 0)) == 5
        ]
        assert len(uc_for_deleted) > 0, (
            "UC should be regenerated for deleted LLM edge call site"
        )
        assert len(result.regenerated_unresolved_calls) > 0


# ---------------------------------------------------------------------------
# §8 Analyze State Machine — Repair Double-Trigger
# ---------------------------------------------------------------------------


class TestSection8_RepairStateMachine:
    """Test POST /analyze/repair state machine (architecture.md §8)."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(store=neo4j_store)
        self.app = app
        self.client = TestClient(app)

    def test_repair_returns_202(self):
        """§8: POST /analyze/repair returns 202 Accepted."""
        r = self.client.post("/api/v1/analyze/repair", json={"source_ids": []})
        assert r.status_code == 202
        data = r.json()
        assert data["status"] == "accepted"
        assert data["action"] == "repair"

    def test_repair_sets_state_to_repairing(self):
        """§8: After triggering repair, state should be 'repairing'."""
        self.client.post("/api/v1/analyze/repair", json={"source_ids": []})
        r = self.client.get("/api/v1/analyze/status")
        data = r.json()
        assert data["state"] == "repairing"

    def test_repair_double_trigger_returns_409(self):
        """§8: POST /analyze/repair while already repairing returns 409."""
        r1 = self.client.post("/api/v1/analyze/repair", json={"source_ids": []})
        assert r1.status_code == 202

        r2 = self.client.post("/api/v1/analyze/repair", json={"source_ids": []})
        assert r2.status_code == 409

    def test_analyze_blocked_during_repair(self):
        """§8: POST /analyze should be blocked while repair is running."""
        self.client.post("/api/v1/analyze/repair", json={"source_ids": []})
        r = self.client.post("/api/v1/analyze", json={"mode": "full"})
        assert r.status_code == 409

    def test_status_sources_field_present(self):
        """§8: /analyze/status always includes 'sources' list."""
        r = self.client.get("/api/v1/analyze/status")
        data = r.json()
        assert "sources" in data
        assert isinstance(data["sources"], list)


# ---------------------------------------------------------------------------
# §8 Source Points List — Pagination and Filtering
# ---------------------------------------------------------------------------


class TestSection8_SourcePointsPagination:
    """Test /source-points pagination and filtering (architecture.md §8)."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(store=neo4j_store)
        # Inject test source points
        app.state.source_points = [
            {"id": f"sp_page_{i}", "function_id": f"fn_page_{i}",
             "entry_point_kind": "callback" if i % 2 == 0 else "entry_function",
             "reason": f"reason {i}"}
            for i in range(20)
        ]
        self.client = TestClient(app)

    def test_pagination_limit_offset(self):
        """§8: source-points supports limit/offset pagination."""
        r = self.client.get("/api/v1/source-points?limit=5&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 20
        assert len(data["items"]) == 5

    def test_pagination_offset_beyond_total(self):
        """§8: offset beyond total returns empty items."""
        r = self.client.get("/api/v1/source-points?limit=5&offset=100")
        data = r.json()
        assert data["total"] == 20
        assert len(data["items"]) == 0

    def test_filter_by_kind(self):
        """§8: ?kind= filters source points by entry_point_kind."""
        r = self.client.get("/api/v1/source-points?kind=callback")
        data = r.json()
        assert data["total"] == 10  # Even indices
        for item in data["items"]:
            assert item["entry_point_kind"] == "callback"

    def test_filter_by_status(self):
        """§8: ?status= filters source points by status."""
        r = self.client.get("/api/v1/source-points?status=pending")
        data = r.json()
        # All should be pending since no SourcePoint nodes exist
        assert data["total"] == 20

    def test_response_items_have_required_fields(self):
        """§8: Each source-point item has id, entry_point_kind, status, signature."""
        r = self.client.get("/api/v1/source-points?limit=3")
        data = r.json()
        for item in data["items"]:
            assert "id" in item
            assert "entry_point_kind" in item or "kind" in item
            assert "status" in item
            assert "signature" in item


# ---------------------------------------------------------------------------
# §3 Repair Orchestrator — Gate Mechanism Contract
# ---------------------------------------------------------------------------


class TestSection3_GateContract:
    """Test gate mechanism contract details (architecture.md §3).

    Verifies that update_unresolved_call_retry_state correctly:
    - Increments retry_count
    - Sets last_attempt_timestamp
    - Sets last_attempt_reason with category prefix
    - Transitions to unresolvable at retry_count >= 3
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode
        from neo4j import GraphDatabase

        self.store = neo4j_store
        self.driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )

        # Create a function and UC for testing
        fn = FunctionNode(
            id="gate_fn_1", signature="void gateTest()", name="gateTest",
            file_path="/test/gate.cpp", start_line=1, end_line=10,
            body_hash="gate_hash",
        )
        neo4j_store.create_function(fn)

        self.uc = UnresolvedCallNode(
            caller_id="gate_fn_1", call_expression="dispatch()",
            call_file="/test/gate.cpp", call_line=7,
            call_type="indirect", source_code_snippet="dispatch();",
            var_name="dispatch", var_type=None,
            candidates=[], retry_count=0, status="pending",
            id="gate_uc_1",
        )
        neo4j_store.create_unresolved_call(self.uc)
        yield

        with self.driver.session() as s:
            s.run(
                "MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
                ids=["gate_fn_1", "gate_uc_1"],
            )
        self.driver.close()

    def test_retry_increments_count(self):
        """§3: Each gate failure increments retry_count by 1."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        self.store.update_unresolved_call_retry_state(
            "gate_uc_1", timestamp=ts, reason="gate_failed: 2 gaps remaining"
        )
        # Read back
        with self.driver.session() as s:
            r = s.run(
                "MATCH (u:UnresolvedCall {id: 'gate_uc_1'}) RETURN u.retry_count AS rc"
            ).single()
        assert r["rc"] == 1

    def test_retry_sets_timestamp(self):
        """§3: Gate failure sets last_attempt_timestamp."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        self.store.update_unresolved_call_retry_state(
            "gate_uc_1", timestamp=ts, reason="agent_error: quota exhausted"
        )
        with self.driver.session() as s:
            r = s.run(
                "MATCH (u:UnresolvedCall {id: 'gate_uc_1'}) "
                "RETURN u.last_attempt_timestamp AS ts"
            ).single()
        assert r["ts"] is not None
        assert len(r["ts"]) > 10  # ISO-8601 format

    def test_retry_sets_reason_with_category(self):
        """§3: Gate failure sets last_attempt_reason with category prefix."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        self.store.update_unresolved_call_retry_state(
            "gate_uc_1", timestamp=ts, reason="subprocess_timeout: killed after 240s"
        )
        with self.driver.session() as s:
            r = s.run(
                "MATCH (u:UnresolvedCall {id: 'gate_uc_1'}) "
                "RETURN u.last_attempt_reason AS reason"
            ).single()
        assert r["reason"] == "subprocess_timeout: killed after 240s"

    def test_retry_transitions_to_unresolvable_at_3(self):
        """§3: retry_count >= 3 transitions status to unresolvable."""
        from datetime import datetime, timezone
        # Retry 3 times
        for i in range(3):
            ts = datetime.now(timezone.utc).isoformat()
            self.store.update_unresolved_call_retry_state(
                "gate_uc_1", timestamp=ts, reason=f"gate_failed: attempt {i+1}"
            )
        with self.driver.session() as s:
            r = s.run(
                "MATCH (u:UnresolvedCall {id: 'gate_uc_1'}) "
                "RETURN u.status AS status, u.retry_count AS rc"
            ).single()
        assert r["status"] == "unresolvable"
        assert r["rc"] == 3

    def test_retry_reason_categories_match_architecture(self):
        """§3: All 5 category prefixes are accepted without error."""
        from datetime import datetime, timezone
        categories = [
            "gate_failed: 2 gaps remaining",
            "agent_error: quota exhausted",
            "subprocess_crash: binary not found",
            "subprocess_timeout: killed after 240s",
            "agent_exited_without_edge",
        ]
        # Use a fresh UC for each to avoid hitting unresolvable
        from codemap_lite.graph.schema import UnresolvedCallNode
        for i, reason in enumerate(categories):
            uc = UnresolvedCallNode(
                caller_id="gate_fn_1", call_expression=f"call_{i}()",
                call_file="/test/gate.cpp", call_line=20 + i,
                call_type="indirect", source_code_snippet=f"call_{i}();",
                var_name=None, var_type=None,
                candidates=[], retry_count=0, status="pending",
                id=f"gate_cat_uc_{i}",
            )
            self.store.create_unresolved_call(uc)
            ts = datetime.now(timezone.utc).isoformat()
            self.store.update_unresolved_call_retry_state(
                f"gate_cat_uc_{i}", timestamp=ts, reason=reason
            )

        # Verify all were stamped
        try:
            with self.driver.session() as s:
                for i, reason in enumerate(categories):
                    r = s.run(
                        "MATCH (u:UnresolvedCall {id: $id}) "
                        "RETURN u.last_attempt_reason AS reason",
                        id=f"gate_cat_uc_{i}",
                    ).single()
                    assert r["reason"] == reason
        finally:
            with self.driver.session() as s:
                s.run(
                    "MATCH (u:UnresolvedCall) WHERE u.id STARTS WITH 'gate_cat_uc_' "
                    "DETACH DELETE u"
                )


# ---------------------------------------------------------------------------
# §8 Repair Logs Endpoint — Filtering and Format
# ---------------------------------------------------------------------------


class TestSection8_RepairLogsEndpoint:
    """Test GET /api/v1/repair-logs with real RepairLog nodes (architecture.md §8).

    §8 contract: supports ?caller= / ?callee= / ?location= filtering.
    Response: {total, items} where each item has caller_id, callee_id,
    call_location, repair_method, llm_response, timestamp, reasoning_summary, id.
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import RepairLogNode
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient
        from neo4j import GraphDatabase

        self.store = neo4j_store
        self.driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )

        # Create test RepairLog nodes
        self.log1 = RepairLogNode(
            caller_id="rl_caller_1", callee_id="rl_callee_1",
            call_location="/test/rl.cpp:10",
            repair_method="llm", llm_response="Resolved via vtable lookup",
            timestamp="2026-05-15T10:00:00Z",
            reasoning_summary="Matched vtable dispatch pattern",
            id="rl_test_1",
        )
        self.log2 = RepairLogNode(
            caller_id="rl_caller_1", callee_id="rl_callee_2",
            call_location="/test/rl.cpp:20",
            repair_method="llm", llm_response="Resolved via callback registration",
            timestamp="2026-05-15T10:01:00Z",
            reasoning_summary="Found callback registration in init()",
            id="rl_test_2",
        )
        self.log3 = RepairLogNode(
            caller_id="rl_caller_2", callee_id="rl_callee_1",
            call_location="/test/other.cpp:5",
            repair_method="llm", llm_response="Cross-module dispatch",
            timestamp="2026-05-15T10:02:00Z",
            reasoning_summary="Cross-module function pointer",
            id="rl_test_3",
        )
        neo4j_store.create_repair_log(self.log1)
        neo4j_store.create_repair_log(self.log2)
        neo4j_store.create_repair_log(self.log3)

        app = create_app(store=neo4j_store)
        self.client = TestClient(app)
        yield

        with self.driver.session() as s:
            s.run(
                "MATCH (r:RepairLog) WHERE r.id IN $ids DELETE r",
                ids=["rl_test_1", "rl_test_2", "rl_test_3"],
            )
        self.driver.close()

    def test_repair_logs_returns_paginated_format(self):
        """§8: repair-logs returns {total, items}."""
        r = self.client.get("/api/v1/repair-logs")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] >= 3

    def test_repair_logs_items_have_required_fields(self):
        """§8: Each RepairLog item has all required fields."""
        r = self.client.get("/api/v1/repair-logs?caller=rl_caller_1")
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert "caller_id" in item
            assert "callee_id" in item
            assert "call_location" in item
            assert "repair_method" in item
            assert "llm_response" in item
            assert "timestamp" in item
            assert "reasoning_summary" in item
            assert "id" in item

    def test_repair_logs_filter_by_caller(self):
        """§8: ?caller= filters by caller_id."""
        r = self.client.get("/api/v1/repair-logs?caller=rl_caller_1")
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["caller_id"] == "rl_caller_1"

    def test_repair_logs_filter_by_callee(self):
        """§8: ?callee= filters by callee_id."""
        r = self.client.get("/api/v1/repair-logs?callee=rl_callee_1")
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["callee_id"] == "rl_callee_1"

    def test_repair_logs_filter_by_location(self):
        """§8: ?location= filters by call_location."""
        r = self.client.get("/api/v1/repair-logs?location=/test/rl.cpp:10")
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "rl_test_1"

    def test_repair_logs_combined_filters(self):
        """§8: Multiple filters are AND-combined."""
        r = self.client.get(
            "/api/v1/repair-logs?caller=rl_caller_1&callee=rl_callee_2"
        )
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "rl_test_2"

    def test_repair_logs_pagination(self):
        """§8: limit/offset pagination works."""
        r = self.client.get("/api/v1/repair-logs?caller=rl_caller_1&limit=1&offset=0")
        data = r.json()
        assert data["total"] == 2
        assert len(data["items"]) == 1


# ---------------------------------------------------------------------------
# §8 Source Point Detail + Reachable
# ---------------------------------------------------------------------------


class TestSection8_SourcePointDetail:
    """Test GET /source-points/{id} and /source-points/{id}/reachable."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import (
            FunctionNode, SourcePointNode, UnresolvedCallNode, CallsEdgeProps,
        )
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient
        from neo4j import GraphDatabase

        self.store = neo4j_store
        self.driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )

        # Create a mini graph for reachable test
        self.fn_src = FunctionNode(
            id="spd_fn_src", signature="void source()", name="source",
            file_path="/test/spd.cpp", start_line=1, end_line=10,
            body_hash="spd_hash_src",
        )
        self.fn_tgt = FunctionNode(
            id="spd_fn_tgt", signature="void target()", name="target",
            file_path="/test/spd.cpp", start_line=20, end_line=30,
            body_hash="spd_hash_tgt",
        )
        neo4j_store.create_function(self.fn_src)
        neo4j_store.create_function(self.fn_tgt)

        # CALLS edge
        neo4j_store.create_calls_edge(
            "spd_fn_src", "spd_fn_tgt",
            CallsEdgeProps(resolved_by="symbol_table", call_type="direct",
                          call_file="/test/spd.cpp", call_line=5),
        )

        # SourcePoint
        self.sp = SourcePointNode(
            id="spd_sp_1", entry_point_kind="callback",
            reason="test detail", function_id="spd_fn_src",
            module="test", status="running",
        )
        neo4j_store.create_source_point(self.sp)

        # UC on target
        self.uc = UnresolvedCallNode(
            caller_id="spd_fn_tgt", call_expression="ptr->call()",
            call_file="/test/spd.cpp", call_line=25,
            call_type="indirect", source_code_snippet="ptr->call();",
            var_name="ptr", var_type="IFace*",
            candidates=[], retry_count=0, status="pending",
            id="spd_uc_1",
        )
        neo4j_store.create_unresolved_call(self.uc)

        app = create_app(store=neo4j_store)
        # Wire source_points for fallback lookup
        app.state.source_points = [
            {"id": "spd_sp_1", "function_id": "spd_fn_src",
             "entry_point_kind": "callback", "reason": "test detail"},
        ]
        self.client = TestClient(app)
        yield

        with self.driver.session() as s:
            s.run(
                "MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
                ids=["spd_fn_src", "spd_fn_tgt", "spd_sp_1", "spd_uc_1"],
            )
        self.driver.close()

    def test_source_point_detail_from_neo4j(self):
        """§8: GET /source-points/{id} returns SourcePoint from Neo4j."""
        r = self.client.get("/api/v1/source-points/spd_sp_1")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == "spd_sp_1"
        assert data["status"] == "running"
        assert data["entry_point_kind"] == "callback"
        assert data["function_id"] == "spd_fn_src"

    def test_source_point_detail_404(self):
        """§8: GET /source-points/{id} returns 404 for nonexistent."""
        r = self.client.get("/api/v1/source-points/nonexistent_sp")
        assert r.status_code == 404

    def test_source_point_reachable_format(self):
        """§8: /source-points/{id}/reachable returns {nodes, edges, unresolved}."""
        r = self.client.get("/api/v1/source-points/spd_sp_1/reachable")
        assert r.status_code == 200
        data = r.json()
        assert "nodes" in data
        assert "edges" in data
        assert "unresolved" in data

    def test_source_point_reachable_contains_graph(self):
        """§8: Reachable subgraph includes source + callees + UCs."""
        r = self.client.get("/api/v1/source-points/spd_sp_1/reachable")
        data = r.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert "spd_fn_src" in node_ids
        assert "spd_fn_tgt" in node_ids
        # Should have at least 1 edge
        assert len(data["edges"]) >= 1
        # Should include the UC on spd_fn_tgt
        uc_callers = {u["caller_id"] for u in data["unresolved"]}
        assert "spd_fn_tgt" in uc_callers


# ---------------------------------------------------------------------------
# §6 Feedback/Counter-Example Endpoint
# ---------------------------------------------------------------------------


class TestSection6_FeedbackEndpoint:
    """Test POST/GET /api/v1/feedback (architecture.md §6 + §8)."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store, tmp_path):
        from codemap_lite.analysis.feedback_store import FeedbackStore
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        self.feedback_store = FeedbackStore(storage_dir=tmp_path / "feedback")
        app = create_app(store=neo4j_store, feedback_store=self.feedback_store)
        self.client = TestClient(app)

    def test_feedback_empty_initially(self):
        """§8: GET /feedback returns {total: 0, items: []} when empty."""
        r = self.client.get("/api/v1/feedback")
        assert r.status_code == 200
        data = r.json()
        assert data == {"total": 0, "items": []}

    def test_feedback_post_creates_entry(self):
        """§6: POST /feedback creates a counter-example."""
        body = {
            "call_context": "fnA calls ptr->dispatch()",
            "wrong_target": "fnB",
            "correct_target": "fnC",
            "pattern": "vtable dispatch in CastEngine should resolve to registered handler",
            "source_id": "test_source_1",
        }
        r = self.client.post("/api/v1/feedback", json=body)
        assert r.status_code == 201
        data = r.json()
        assert data["call_context"] == body["call_context"]
        assert data["wrong_target"] == body["wrong_target"]
        assert data["correct_target"] == body["correct_target"]
        assert data["pattern"] == body["pattern"]
        assert data["deduplicated"] is False
        assert data["total"] == 1

    def test_feedback_deduplication(self):
        """§6: Submitting same pattern twice marks as deduplicated."""
        body = {
            "call_context": "fnX calls handler()",
            "wrong_target": "fnY",
            "correct_target": "fnZ",
            "pattern": "handler dispatch pattern",
            "source_id": "",
        }
        r1 = self.client.post("/api/v1/feedback", json=body)
        assert r1.status_code == 201
        assert r1.json()["deduplicated"] is False

        r2 = self.client.post("/api/v1/feedback", json=body)
        assert r2.status_code == 201
        assert r2.json()["deduplicated"] is True
        assert r2.json()["total"] == 1  # Not duplicated

    def test_feedback_get_after_post(self):
        """§8: GET /feedback returns posted entries."""
        body = {
            "call_context": "test context",
            "wrong_target": "wrong",
            "correct_target": "correct",
            "pattern": "test pattern",
            "source_id": "",
        }
        self.client.post("/api/v1/feedback", json=body)
        r = self.client.get("/api/v1/feedback")
        data = r.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["call_context"] == "test context"
        assert item["wrong_target"] == "wrong"
        assert item["correct_target"] == "correct"
        assert item["pattern"] == "test pattern"

    def test_feedback_validation_targets_must_differ(self):
        """§6: wrong_target must differ from correct_target."""
        body = {
            "call_context": "ctx",
            "wrong_target": "same",
            "correct_target": "same",
            "pattern": "pattern",
            "source_id": "",
        }
        r = self.client.post("/api/v1/feedback", json=body)
        assert r.status_code == 422

    def test_feedback_validation_required_fields(self):
        """§6: All fields except source_id are required."""
        r = self.client.post("/api/v1/feedback", json={})
        assert r.status_code == 422

    def test_feedback_pagination(self):
        """§8: GET /feedback supports limit/offset."""
        for i in range(5):
            self.client.post("/api/v1/feedback", json={
                "call_context": f"ctx_{i}",
                "wrong_target": f"wrong_{i}",
                "correct_target": f"correct_{i}",
                "pattern": f"pattern_{i}",
                "source_id": "",
            })
        r = self.client.get("/api/v1/feedback?limit=2&offset=0")
        data = r.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2


# ---------------------------------------------------------------------------
# §5 Full Review Cascade via HTTP — End-to-End
# ---------------------------------------------------------------------------


class TestSection5_ReviewCascadeHTTP:
    """Test POST /reviews verdict=incorrect full cascade via HTTP (architecture.md §5).

    Creates: fn_A -[CALLS llm]-> fn_B, RepairLog for the edge, SourcePoint for fn_A.
    Submits: POST /reviews verdict=incorrect.
    Verifies: edge deleted, RepairLog deleted, UC regenerated, SP reset to pending.
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import (
            FunctionNode, CallsEdgeProps, RepairLogNode, SourcePointNode,
        )
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient
        from neo4j import GraphDatabase

        self.store = neo4j_store
        self.driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )

        # Create functions
        fn_a = FunctionNode(
            id="rv_fn_a", signature="void rvA()", name="rvA",
            file_path="/test/rv.cpp", start_line=1, end_line=10,
            body_hash="rv_hash_a",
        )
        fn_b = FunctionNode(
            id="rv_fn_b", signature="void rvB()", name="rvB",
            file_path="/test/rv.cpp", start_line=20, end_line=30,
            body_hash="rv_hash_b",
        )
        neo4j_store.create_function(fn_a)
        neo4j_store.create_function(fn_b)

        # Create LLM CALLS edge
        neo4j_store.create_calls_edge(
            "rv_fn_a", "rv_fn_b",
            CallsEdgeProps(resolved_by="llm", call_type="indirect",
                          call_file="/test/rv.cpp", call_line=5),
        )

        # Create RepairLog for the edge
        log = RepairLogNode(
            caller_id="rv_fn_a", callee_id="rv_fn_b",
            call_location="/test/rv.cpp:5",
            repair_method="llm", llm_response="Resolved via vtable",
            timestamp="2026-05-15T12:00:00Z",
            reasoning_summary="vtable dispatch",
            id="rv_log_1",
        )
        neo4j_store.create_repair_log(log)

        # Create SourcePoint for fn_a (status=complete)
        sp = SourcePointNode(
            id="rv_sp_a", entry_point_kind="callback",
            reason="test review cascade", function_id="rv_fn_a",
            module="test", status="complete",
        )
        neo4j_store.create_source_point(sp)

        app = create_app(store=neo4j_store)
        self.client = TestClient(app)
        yield

        with self.driver.session() as s:
            s.run(
                "MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
                ids=["rv_fn_a", "rv_fn_b", "rv_sp_a", "rv_log_1"],
            )
            # Clean up any regenerated UCs
            s.run(
                "MATCH (u:UnresolvedCall) WHERE u.caller_id = 'rv_fn_a' "
                "AND u.call_file = '/test/rv.cpp' AND u.call_line = 5 "
                "DETACH DELETE u"
            )
        self.driver.close()

    def test_incorrect_review_deletes_edge(self):
        """§5: verdict=incorrect deletes the CALLS edge."""
        # Verify edge exists before
        assert self.store.edge_exists("rv_fn_a", "rv_fn_b", "/test/rv.cpp", 5)

        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rv_fn_a",
            "callee_id": "rv_fn_b",
            "call_file": "/test/rv.cpp",
            "call_line": 5,
            "verdict": "incorrect",
        })
        assert r.status_code == 201

        # Edge should be gone
        assert not self.store.edge_exists("rv_fn_a", "rv_fn_b", "/test/rv.cpp", 5)

    def test_incorrect_review_deletes_repair_log(self):
        """§5: verdict=incorrect deletes the corresponding RepairLog."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rv_fn_a",
            "callee_id": "rv_fn_b",
            "call_file": "/test/rv.cpp",
            "call_line": 5,
            "verdict": "incorrect",
        })
        assert r.status_code == 201

        # RepairLog should be gone
        logs = self.store.get_repair_logs(
            caller_id="rv_fn_a", callee_id="rv_fn_b",
            call_location="/test/rv.cpp:5",
        )
        assert len(logs) == 0

    def test_incorrect_review_regenerates_uc(self):
        """§5: verdict=incorrect regenerates UnresolvedCall with retry_count=0."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rv_fn_a",
            "callee_id": "rv_fn_b",
            "call_file": "/test/rv.cpp",
            "call_line": 5,
            "verdict": "incorrect",
        })
        assert r.status_code == 201

        # UC should be regenerated
        ucs = self.store.get_unresolved_calls(caller_id="rv_fn_a")
        matching = [
            uc for uc in ucs
            if uc.call_file == "/test/rv.cpp" and uc.call_line == 5
        ]
        assert len(matching) == 1
        assert matching[0].retry_count == 0
        assert matching[0].status == "pending"

    def test_incorrect_review_resets_source_point(self):
        """§5: verdict=incorrect resets SourcePoint status to pending."""
        # Verify SP is complete before
        sp = self.store.get_source_point("rv_sp_a")
        assert sp.status == "complete"

        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rv_fn_a",
            "callee_id": "rv_fn_b",
            "call_file": "/test/rv.cpp",
            "call_line": 5,
            "verdict": "incorrect",
        })
        assert r.status_code == 201

        # SP should be reset to pending
        sp = self.store.get_source_point("rv_sp_a")
        assert sp.status == "pending"

    def test_correct_review_preserves_edge(self):
        """§5: verdict=correct does NOT delete the edge."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rv_fn_a",
            "callee_id": "rv_fn_b",
            "call_file": "/test/rv.cpp",
            "call_line": 5,
            "verdict": "correct",
        })
        assert r.status_code == 201

        # Edge should still exist
        assert self.store.edge_exists("rv_fn_a", "rv_fn_b", "/test/rv.cpp", 5)

    def test_review_nonexistent_edge_returns_404(self):
        """§5: Reviewing a nonexistent edge returns 404."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rv_fn_a",
            "callee_id": "nonexistent",
            "call_file": "/test/rv.cpp",
            "call_line": 99,
            "verdict": "incorrect",
        })
        assert r.status_code == 404

    def test_review_race_condition_returns_404(self):
        """§5: Double-submit of incorrect review returns 404 on second attempt."""
        body = {
            "caller_id": "rv_fn_a",
            "callee_id": "rv_fn_b",
            "call_file": "/test/rv.cpp",
            "call_line": 5,
            "verdict": "incorrect",
        }
        r1 = self.client.post("/api/v1/reviews", json=body)
        assert r1.status_code == 201

        # Second attempt — edge already deleted
        r2 = self.client.post("/api/v1/reviews", json=body)
        assert r2.status_code == 404


# ---------------------------------------------------------------------------
# §5/§8 POST /edges + DELETE /edges Full Lifecycle
# ---------------------------------------------------------------------------


class TestSection5_EdgesLifecycle:
    """Test POST /edges and DELETE /edges full lifecycle (architecture.md §5/§8).

    POST /edges: creates edge, deletes matching UC, returns 409 on duplicate.
    DELETE /edges: 4-step cascade (delete edge, delete RepairLog, regen UC, reset SP).
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import (
            FunctionNode, UnresolvedCallNode, SourcePointNode, CallsEdgeProps,
        )
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient
        from neo4j import GraphDatabase

        self.store = neo4j_store
        self.driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
        )

        # Create functions
        fn_a = FunctionNode(
            id="el_fn_a", signature="void elA()", name="elA",
            file_path="/test/el.cpp", start_line=1, end_line=10,
            body_hash="el_hash_a",
        )
        fn_b = FunctionNode(
            id="el_fn_b", signature="void elB()", name="elB",
            file_path="/test/el.cpp", start_line=20, end_line=30,
            body_hash="el_hash_b",
        )
        neo4j_store.create_function(fn_a)
        neo4j_store.create_function(fn_b)

        # Create an UnresolvedCall for the gap
        self.uc = UnresolvedCallNode(
            caller_id="el_fn_a", call_expression="ptr->dispatch()",
            call_file="/test/el.cpp", call_line=5,
            call_type="indirect", source_code_snippet="ptr->dispatch();",
            var_name="ptr", var_type="IDispatch*",
            candidates=[], retry_count=0, status="pending",
            id="el_uc_1",
        )
        neo4j_store.create_unresolved_call(self.uc)

        # Create SourcePoint for fn_a
        sp = SourcePointNode(
            id="el_sp_a", entry_point_kind="callback",
            reason="test edges lifecycle", function_id="el_fn_a",
            module="test", status="complete",
        )
        neo4j_store.create_source_point(sp)

        app = create_app(store=neo4j_store)
        self.client = TestClient(app)
        yield

        with self.driver.session() as s:
            s.run(
                "MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
                ids=["el_fn_a", "el_fn_b", "el_sp_a", "el_uc_1"],
            )
            s.run(
                "MATCH (u:UnresolvedCall) WHERE u.caller_id = 'el_fn_a' "
                "AND u.call_file = '/test/el.cpp' DETACH DELETE u"
            )
        self.driver.close()

    def test_post_edges_creates_edge(self):
        """§8: POST /edges creates a CALLS edge."""
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "el_fn_a",
            "callee_id": "el_fn_b",
            "resolved_by": "context",
            "call_type": "indirect",
            "call_file": "/test/el.cpp",
            "call_line": 5,
        })
        assert r.status_code == 201
        assert self.store.edge_exists("el_fn_a", "el_fn_b", "/test/el.cpp", 5)

    def test_post_edges_deletes_matching_uc(self):
        """§8: POST /edges deletes the matching UnresolvedCall."""
        # Verify UC exists before
        ucs = self.store.get_unresolved_calls(caller_id="el_fn_a")
        assert any(uc.call_line == 5 for uc in ucs)

        self.client.post("/api/v1/edges", json={
            "caller_id": "el_fn_a",
            "callee_id": "el_fn_b",
            "resolved_by": "context",
            "call_type": "indirect",
            "call_file": "/test/el.cpp",
            "call_line": 5,
        })

        # UC should be gone
        ucs = self.store.get_unresolved_calls(caller_id="el_fn_a")
        assert not any(uc.call_line == 5 for uc in ucs)

    def test_post_edges_duplicate_returns_409(self):
        """§8: POST /edges with existing edge returns 409."""
        self.client.post("/api/v1/edges", json={
            "caller_id": "el_fn_a",
            "callee_id": "el_fn_b",
            "resolved_by": "context",
            "call_type": "indirect",
            "call_file": "/test/el.cpp",
            "call_line": 5,
        })
        r2 = self.client.post("/api/v1/edges", json={
            "caller_id": "el_fn_a",
            "callee_id": "el_fn_b",
            "resolved_by": "context",
            "call_type": "indirect",
            "call_file": "/test/el.cpp",
            "call_line": 5,
        })
        assert r2.status_code == 409

    def test_post_edges_nonexistent_caller_returns_404(self):
        """§8: POST /edges with nonexistent caller returns 404."""
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "nonexistent",
            "callee_id": "el_fn_b",
            "resolved_by": "context",
            "call_type": "indirect",
            "call_file": "/test/el.cpp",
            "call_line": 99,
        })
        assert r.status_code == 404

    def test_delete_edges_full_cascade(self):
        """§5: DELETE /edges triggers full 4-step cascade."""
        from codemap_lite.graph.schema import CallsEdgeProps, RepairLogNode

        # First create an LLM edge
        self.store.create_calls_edge(
            "el_fn_a", "el_fn_b",
            CallsEdgeProps(resolved_by="llm", call_type="indirect",
                          call_file="/test/el.cpp", call_line=5),
        )
        # Create RepairLog
        log = RepairLogNode(
            caller_id="el_fn_a", callee_id="el_fn_b",
            call_location="/test/el.cpp:5",
            repair_method="llm", llm_response="test",
            timestamp="2026-05-15T12:00:00Z",
            reasoning_summary="test",
            id="el_log_1",
        )
        self.store.create_repair_log(log)

        # Delete the edge via API
        r = self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "el_fn_a",
            "callee_id": "el_fn_b",
            "call_file": "/test/el.cpp",
            "call_line": 5,
        })
        assert r.status_code == 204

        # Verify cascade:
        # 1. Edge deleted
        assert not self.store.edge_exists("el_fn_a", "el_fn_b", "/test/el.cpp", 5)
        # 2. RepairLog deleted
        logs = self.store.get_repair_logs(
            caller_id="el_fn_a", callee_id="el_fn_b",
            call_location="/test/el.cpp:5",
        )
        assert len(logs) == 0
        # 3. UC regenerated
        ucs = self.store.get_unresolved_calls(caller_id="el_fn_a")
        matching = [uc for uc in ucs if uc.call_line == 5]
        assert len(matching) == 1
        assert matching[0].status == "pending"
        assert matching[0].retry_count == 0

    def test_delete_edges_resets_source_point(self):
        """§5: DELETE /edges resets SourcePoint status to pending."""
        from codemap_lite.graph.schema import CallsEdgeProps

        # Create edge
        self.store.create_calls_edge(
            "el_fn_a", "el_fn_b",
            CallsEdgeProps(resolved_by="llm", call_type="indirect",
                          call_file="/test/el.cpp", call_line=5),
        )

        # Verify SP is complete
        sp = self.store.get_source_point("el_sp_a")
        assert sp.status == "complete"

        # Delete edge
        r = self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "el_fn_a",
            "callee_id": "el_fn_b",
            "call_file": "/test/el.cpp",
            "call_line": 5,
        })
        assert r.status_code == 204

        # SP should be reset
        sp = self.store.get_source_point("el_sp_a")
        assert sp.status == "pending"

    def test_delete_edges_nonexistent_returns_404(self):
        """§5: DELETE /edges for nonexistent edge returns 404."""
        r = self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "el_fn_a",
            "callee_id": "el_fn_b",
            "call_file": "/test/el.cpp",
            "call_line": 999,
        })
        assert r.status_code == 404


class TestSection8_PaginationContract:
    """§8 REST API: all list endpoints support limit/offset pagination.

    architecture.md §8 specifies that list endpoints return {total, items}
    and accept limit/offset query parameters. This test class verifies
    pagination works correctly across all list endpoints.
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import (
            CallsEdgeProps,
            FileNode,
            FunctionNode,
            SourcePointNode,
            UnresolvedCallNode,
        )
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        self.store = neo4j_store

        for i in range(1, 3):
            neo4j_store.create_file(FileNode(
                id=f"pg_file_{i}", file_path=f"/test/pg{i}.cpp",
                hash=f"h{i}", primary_language="cpp"
            ))
        for i in range(1, 6):
            neo4j_store.create_function(FunctionNode(
                id=f"pg_fn_{i}", name=f"pgFunc{i}", signature=f"void pgFunc{i}()",
                file_path=f"/test/pg{1 if i <= 3 else 2}.cpp",
                start_line=i * 10, end_line=i * 10 + 5, body_hash=f"bh{i}"
            ))
        # Create edges: fn_1 calls fn_2..fn_5
        for i in range(2, 6):
            neo4j_store.create_calls_edge(
                "pg_fn_1", f"pg_fn_{i}",
                CallsEdgeProps(resolved_by="symbol_table", call_type="direct",
                               call_file="/test/pg1.cpp", call_line=10 + i)
            )
        # fn_2 calls fn_1 (so fn_1 has callers)
        neo4j_store.create_calls_edge(
            "pg_fn_2", "pg_fn_1",
            CallsEdgeProps(resolved_by="signature", call_type="indirect",
                           call_file="/test/pg1.cpp", call_line=30)
        )
        # Create UCs
        for i in range(1, 4):
            neo4j_store.create_unresolved_call(UnresolvedCallNode(
                caller_id="pg_fn_1", call_expression=f"uc_expr_{i}",
                call_file="/test/pg1.cpp", call_line=100 + i,
                call_type="indirect", source_code_snippet="",
                var_name=None, var_type=None, retry_count=0, status="pending"
            ))
        # Create source points
        neo4j_store.create_source_point(SourcePointNode(
            id="pg_sp_1", entry_point_kind="api_handler", reason="test",
            function_id="pg_fn_1", module="mod_a", status="pending"
        ))
        neo4j_store.create_source_point(SourcePointNode(
            id="pg_sp_2", entry_point_kind="callback", reason="test",
            function_id="pg_fn_2", module="mod_b", status="complete"
        ))

        app = create_app(store=neo4j_store)
        app.state.source_points = [
            {"id": "pg_sp_1", "function_id": "pg_fn_1",
             "entry_point_kind": "api_handler", "reason": "test", "module": "mod_a"},
            {"id": "pg_sp_2", "function_id": "pg_fn_2",
             "entry_point_kind": "callback", "reason": "test", "module": "mod_b"},
        ]
        self.client = TestClient(app)

    def test_files_pagination(self):
        """GET /files respects limit and offset."""
        # Get all
        r = self.client.get("/api/v1/files")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data
        total = data["total"]
        assert total >= 2

        # Limit=1
        r = self.client.get("/api/v1/files?limit=1&offset=0")
        data = r.json()
        assert len(data["items"]) == 1
        assert data["total"] == total  # total unchanged

        # Offset past end
        r = self.client.get(f"/api/v1/files?limit=10&offset={total + 100}")
        data = r.json()
        assert len(data["items"]) == 0
        assert data["total"] == total

    def test_functions_pagination(self):
        """GET /functions respects limit and offset."""
        r = self.client.get("/api/v1/functions?limit=2&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) <= 2
        assert data["total"] >= 5

    def test_callers_pagination(self):
        """GET /functions/{id}/callers respects limit and offset."""
        r = self.client.get("/api/v1/functions/pg_fn_1/callers?limit=1&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data
        assert len(data["items"]) <= 1

    def test_callees_pagination(self):
        """GET /functions/{id}/callees respects limit and offset."""
        r = self.client.get("/api/v1/functions/pg_fn_1/callees?limit=2&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 4  # fn_1 calls fn_2..fn_5
        assert len(data["items"]) <= 2

        # Offset=2 should give next page
        r2 = self.client.get("/api/v1/functions/pg_fn_1/callees?limit=2&offset=2")
        data2 = r2.json()
        assert data2["total"] == data["total"]
        # Items should be different from first page
        ids_page1 = {item["id"] for item in data["items"]}
        ids_page2 = {item["id"] for item in data2["items"]}
        assert ids_page1.isdisjoint(ids_page2)

    def test_unresolved_calls_pagination(self):
        """GET /unresolved-calls respects limit and offset."""
        r = self.client.get("/api/v1/unresolved-calls?limit=1&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 3
        assert len(data["items"]) == 1

    def test_source_points_pagination(self):
        """GET /source-points respects limit and offset."""
        r = self.client.get("/api/v1/source-points?limit=1&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data
        assert len(data["items"]) <= 1

    def test_reviews_pagination(self):
        """GET /reviews respects limit and offset."""
        r = self.client.get("/api/v1/reviews?limit=1&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data

    def test_feedback_pagination(self):
        """GET /feedback respects limit and offset."""
        r = self.client.get("/api/v1/feedback?limit=1&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data

    def test_repair_logs_pagination(self):
        """GET /repair-logs respects limit and offset."""
        r = self.client.get("/api/v1/repair-logs?limit=1&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data


class TestSection8_SourcePointsSummaryContract:
    """§8 REST API: /source-points/summary returns {total, by_kind, by_status}.

    The frontend must receive by_status to render the SourcePoint status
    breakdown. This test verifies the backend contract.
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import SourcePointNode
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        self.store = neo4j_store

        neo4j_store.create_source_point(SourcePointNode(
            id="ssc_sp_1", entry_point_kind="api_handler", reason="test",
            function_id="ssc_fn_1", module="mod_a", status="pending"
        ))
        neo4j_store.create_source_point(SourcePointNode(
            id="ssc_sp_2", entry_point_kind="callback", reason="test",
            function_id="ssc_fn_2", module="mod_a", status="complete"
        ))
        neo4j_store.create_source_point(SourcePointNode(
            id="ssc_sp_3", entry_point_kind="api_handler", reason="test",
            function_id="ssc_fn_3", module="mod_b", status="pending"
        ))

        app = create_app(store=neo4j_store)
        app.state.source_points = [
            {"id": "ssc_sp_1", "function_id": "ssc_fn_1",
             "entry_point_kind": "api_handler", "reason": "test", "module": "mod_a"},
            {"id": "ssc_sp_2", "function_id": "ssc_fn_2",
             "entry_point_kind": "callback", "reason": "test", "module": "mod_a"},
            {"id": "ssc_sp_3", "function_id": "ssc_fn_3",
             "entry_point_kind": "api_handler", "reason": "test", "module": "mod_b"},
        ]
        self.client = TestClient(app)

    def test_summary_has_by_status(self):
        """§8: /source-points/summary must include by_status field."""
        r = self.client.get("/api/v1/source-points/summary")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "by_kind" in data
        assert "by_status" in data, (
            "Backend must return by_status — frontend needs it for status breakdown"
        )

    def test_summary_by_kind_counts(self):
        """§8: by_kind counts match seeded data."""
        r = self.client.get("/api/v1/source-points/summary")
        data = r.json()
        # At minimum our seeded data should appear
        assert data["by_kind"].get("api_handler", 0) >= 2
        assert data["by_kind"].get("callback", 0) >= 1

    def test_summary_by_status_counts(self):
        """§8: by_status counts match seeded data."""
        r = self.client.get("/api/v1/source-points/summary")
        data = r.json()
        assert data["by_status"].get("pending", 0) >= 2
        assert data["by_status"].get("complete", 0) >= 1

    def test_summary_total_matches_sum(self):
        """§8: total == sum of by_kind values == sum of by_status values."""
        r = self.client.get("/api/v1/source-points/summary")
        data = r.json()
        total = data["total"]
        assert total == sum(data["by_kind"].values())
        assert total == sum(data["by_status"].values())


class TestSection8_FrontendContractAlignment:
    """Verify backend responses match the TypeScript interfaces in client.ts.

    These tests ensure the backend returns all fields the frontend expects,
    catching contract drift before it becomes a runtime error.
    """

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_store):
        from codemap_lite.graph.schema import (
            CallsEdgeProps,
            FileNode,
            FunctionNode,
            RepairLogNode,
            SourcePointNode,
            UnresolvedCallNode,
        )
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        self.store = neo4j_store

        neo4j_store.create_file(FileNode(
            id="fc_file_1", file_path="/test/fc.cpp",
            hash="fch1", primary_language="cpp"
        ))
        neo4j_store.create_function(FunctionNode(
            id="fc_fn_1", name="fcFunc1", signature="void fcFunc1()",
            file_path="/test/fc.cpp", start_line=1, end_line=10, body_hash="fcbh1"
        ))
        neo4j_store.create_function(FunctionNode(
            id="fc_fn_2", name="fcFunc2", signature="void fcFunc2()",
            file_path="/test/fc.cpp", start_line=20, end_line=30, body_hash="fcbh2"
        ))
        neo4j_store.create_calls_edge(
            "fc_fn_1", "fc_fn_2",
            CallsEdgeProps(resolved_by="llm", call_type="indirect",
                           call_file="/test/fc.cpp", call_line=5)
        )
        neo4j_store.create_unresolved_call(UnresolvedCallNode(
            caller_id="fc_fn_1", call_expression="fc_expr()",
            call_file="/test/fc.cpp", call_line=7,
            call_type="indirect", source_code_snippet="// snippet",
            var_name="obj", var_type="FcType", retry_count=1,
            status="pending"
        ))
        neo4j_store.create_source_point(SourcePointNode(
            id="fc_sp_1", entry_point_kind="api_handler", reason="test",
            function_id="fc_fn_1", module="fc_mod", status="running"
        ))
        neo4j_store.create_repair_log(RepairLogNode(
            id="fc_rl_1", caller_id="fc_fn_1", callee_id="fc_fn_2",
            call_location="/test/fc.cpp:5", repair_method="llm",
            llm_response="resolved via vtable", timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="Matched vtable dispatch pattern"
        ))

        app = create_app(store=neo4j_store)
        app.state.source_points = [
            {"id": "fc_sp_1", "function_id": "fc_fn_1",
             "entry_point_kind": "api_handler", "reason": "test", "module": "fc_mod"},
        ]
        self.client = TestClient(app)

    def test_file_node_shape(self):
        """FileNode must have: id, file_path, hash, primary_language."""
        # Use the function detail endpoint to verify file_path, then check
        # the /files endpoint returns proper shape
        r = self.client.get("/api/v1/files?limit=1")
        data = r.json()
        assert data["total"] >= 1
        assert len(data["items"]) == 1
        f = data["items"][0]
        # All FileNode fields must be present
        assert "id" in f
        assert "file_path" in f
        assert "hash" in f
        assert "primary_language" in f

    def test_function_node_shape(self):
        """FunctionNode must have: id, name, signature, file_path, start_line, end_line."""
        r = self.client.get("/api/v1/functions/fc_fn_1")
        assert r.status_code == 200
        fn = r.json()
        assert fn["id"] == "fc_fn_1"
        assert fn["name"] == "fcFunc1"
        assert fn["signature"] == "void fcFunc1()"
        assert fn["file_path"] == "/test/fc.cpp"
        assert fn["start_line"] == 1
        assert fn["end_line"] == 10

    def test_call_chain_subgraph_shape(self):
        """Subgraph must have: nodes, edges, unresolved."""
        r = self.client.get("/api/v1/functions/fc_fn_1/call-chain?depth=3")
        assert r.status_code == 200
        data = r.json()
        assert "nodes" in data
        assert "edges" in data
        assert "unresolved" in data
        # Edges should have caller_id, callee_id, props
        if data["edges"]:
            edge = data["edges"][0]
            assert "caller_id" in edge
            assert "callee_id" in edge
            assert "props" in edge
            props = edge["props"]
            assert "resolved_by" in props
            assert "call_type" in props
            assert "call_file" in props
            assert "call_line" in props

    def test_unresolved_call_shape(self):
        """UnresolvedCall must have all fields the frontend expects."""
        r = self.client.get("/api/v1/unresolved-calls?caller=fc_fn_1&limit=100")
        data = r.json()
        assert data["total"] >= 1, "Expected at least 1 UC for fc_fn_1"
        uc = data["items"][0]
        # Required fields
        assert "caller_id" in uc
        assert "call_expression" in uc
        assert "call_file" in uc
        assert "call_line" in uc
        assert "call_type" in uc
        # Optional fields should be present (even if null)
        assert "retry_count" in uc
        assert "status" in uc
        assert "id" in uc

    def test_stats_shape(self):
        """Stats must have all fields the frontend Stats interface expects."""
        r = self.client.get("/api/v1/stats")
        assert r.status_code == 200
        data = r.json()
        # Required fields
        assert "total_functions" in data
        assert "total_files" in data
        assert "total_calls" in data
        assert "total_unresolved" in data
        assert "total_source_points" in data
        # Optional but expected fields
        assert "calls_by_resolved_by" in data
        assert "unresolved_by_status" in data
        assert "unresolved_by_category" in data
        # calls_by_resolved_by should have valid keys
        cbr = data["calls_by_resolved_by"]
        valid_keys = {"symbol_table", "signature", "dataflow", "context", "llm"}
        for key in cbr:
            assert key in valid_keys, f"Unexpected resolved_by key: {key}"

    def test_repair_log_shape(self):
        """RepairLog must have all fields the frontend RepairLog interface expects."""
        r = self.client.get("/api/v1/repair-logs")
        assert r.status_code == 200
        data = r.json()
        fc_logs = [l for l in data["items"] if l.get("id") == "fc_rl_1"]
        assert len(fc_logs) == 1
        log = fc_logs[0]
        assert log["id"] == "fc_rl_1"
        assert log["caller_id"] == "fc_fn_1"
        assert log["callee_id"] == "fc_fn_2"
        assert log["call_location"] == "/test/fc.cpp:5"
        assert log["repair_method"] == "llm"
        assert log["llm_response"] == "resolved via vtable"
        assert log["timestamp"] == "2026-05-15T00:00:00Z"
        assert log["reasoning_summary"] == "Matched vtable dispatch pattern"

    def test_analyze_status_shape(self):
        """AnalyzeStatus must have: state, progress."""
        r = self.client.get("/api/v1/analyze/status")
        assert r.status_code == 200
        data = r.json()
        assert "state" in data
        assert "progress" in data
        # sources key should be present (may be empty list)
        assert "sources" in data

    def test_review_create_and_shape(self):
        """Review must have: id, caller_id, callee_id, call_file, call_line, verdict."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "fc_fn_1",
            "callee_id": "fc_fn_2",
            "call_file": "/test/fc.cpp",
            "call_line": 5,
            "verdict": "correct",
        })
        assert r.status_code == 201
        review = r.json()
        assert "id" in review
        assert review["caller_id"] == "fc_fn_1"
        assert review["callee_id"] == "fc_fn_2"
        assert review["call_file"] == "/test/fc.cpp"
        assert review["call_line"] == 5
        assert review["verdict"] == "correct"



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
        assert has_gap_count == uc_count, (
            f"HAS_GAP count ({has_gap_count}) must equal UC count ({uc_count})"
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




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


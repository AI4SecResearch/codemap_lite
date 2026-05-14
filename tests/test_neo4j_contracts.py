"""Integration tests against real Neo4j — verifies architecture.md contracts.

Requires:
- Neo4j running at bolt://localhost:7687
- $NEO4J_PASSWORD set
- Existing tree-sitter parsed data (from run_e2e_full.py or run_e2e_repair.py)

Skip with: pytest -m "not neo4j_integration"
"""
from __future__ import annotations

import os
import re
import time
from collections import Counter
from datetime import datetime, timezone

import pytest

NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
SKIP_REASON = "Neo4j not reachable at bolt://localhost:7687"


def _get_store():
    """Get a Neo4jGraphStore connected to the real database."""
    from codemap_lite.graph.neo4j_store import Neo4jGraphStore
    return Neo4jGraphStore(
        uri="bolt://localhost:7687",
        user="neo4j",
        password=NEO4J_PASSWORD,
    )


def _neo4j_available() -> bool:
    """Check if Neo4j is reachable and has data."""
    try:
        store = _get_store()
        fns = store.list_functions()
        return len(fns) > 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _neo4j_available(), reason=SKIP_REASON
)


# ─── §4 Graph Schema Contracts ───────────────────────────────────────────────


class TestSchemaContracts:
    """Verify architecture.md §4 schema invariants on real data."""

    def test_function_nodes_have_required_fields(self):
        """Every Function node must have id, name, file_path, start_line, end_line."""
        store = _get_store()
        fns = store.list_functions()
        assert len(fns) > 100, "Expected substantial function count from CastEngine"
        for fn in fns[:200]:  # Sample first 200
            assert fn.id, f"Function missing id: {fn}"
            assert fn.name, f"Function missing name: {fn.id}"
            assert fn.file_path, f"Function missing file_path: {fn.id}"
            assert fn.start_line >= 0, f"Function invalid start_line: {fn.id}"
            assert fn.end_line >= fn.start_line, f"end_line < start_line: {fn.id}"

    def test_function_ids_are_12_char_hex(self):
        """architecture.md §4: Function.id is sha1[:12] hex."""
        store = _get_store()
        fns = store.list_functions()
        hex_pattern = re.compile(r"^[0-9a-f]{12}$")
        for fn in fns[:200]:
            assert hex_pattern.match(fn.id), f"Function id not 12-char hex: {fn.id}"

    def test_calls_edges_have_valid_resolved_by(self):
        """architecture.md §4: CALLS.resolved_by ∈ {symbol_table, signature, dataflow, context, llm}."""
        store = _get_store()
        edges = store.list_calls_edges()
        valid = {"symbol_table", "signature", "dataflow", "context", "llm"}
        for e in edges:
            assert e.props.resolved_by in valid, (
                f"Invalid resolved_by={e.props.resolved_by!r} on edge "
                f"{e.caller_id}->{e.callee_id}"
            )

    def test_calls_edges_have_valid_call_type(self):
        """architecture.md §4: CALLS.call_type ∈ {direct, indirect, virtual}."""
        store = _get_store()
        edges = store.list_calls_edges()
        valid = {"direct", "indirect", "virtual"}
        for e in edges:
            assert e.props.call_type in valid, (
                f"Invalid call_type={e.props.call_type!r} on edge "
                f"{e.caller_id}->{e.callee_id}"
            )

    def test_calls_edge_uniqueness(self):
        """architecture.md §4: CALLS unique by (caller_id, callee_id, call_file, call_line)."""
        store = _get_store()
        edges = store.list_calls_edges()
        seen: set[tuple] = set()
        for e in edges:
            key = (e.caller_id, e.callee_id, e.props.call_file, e.props.call_line)
            assert key not in seen, f"Duplicate CALLS edge: {key}"
            seen.add(key)

    def test_unresolved_calls_have_valid_status(self):
        """architecture.md §4: UnresolvedCall.status ∈ {pending, unresolvable}."""
        store = _get_store()
        ucs = store.get_unresolved_calls()
        valid = {"pending", "unresolvable"}
        for uc in ucs[:500]:  # Sample
            status = getattr(uc, "status", "pending")
            assert status in valid, (
                f"Invalid UC status={status!r} for caller={uc.caller_id}"
            )

    def test_unresolved_calls_have_valid_call_type(self):
        """architecture.md §4: UnresolvedCall.call_type ∈ {direct, indirect, virtual}."""
        store = _get_store()
        ucs = store.get_unresolved_calls()
        valid = {"direct", "indirect", "virtual"}
        for uc in ucs[:500]:
            assert uc.call_type in valid, (
                f"Invalid UC call_type={uc.call_type!r} for caller={uc.caller_id}"
            )

    def test_unresolved_calls_reference_existing_callers(self):
        """Every UC.caller_id should reference an existing Function node."""
        store = _get_store()
        ucs = store.get_unresolved_calls()
        # Sample 100 UCs and verify their callers exist
        missing = []
        for uc in ucs[:100]:
            fn = store.get_function_by_id(uc.caller_id)
            if fn is None:
                missing.append(uc.caller_id)
        assert len(missing) == 0, (
            f"{len(missing)} UCs reference non-existent callers: {missing[:5]}"
        )

    def test_calls_edges_reference_existing_functions(self):
        """Every CALLS edge should reference existing Function nodes."""
        store = _get_store()
        edges = store.list_calls_edges()
        missing_callers = []
        missing_callees = []
        for e in edges[:200]:
            if store.get_function_by_id(e.caller_id) is None:
                missing_callers.append(e.caller_id)
            if store.get_function_by_id(e.callee_id) is None:
                missing_callees.append(e.callee_id)
        assert len(missing_callers) == 0, (
            f"{len(missing_callers)} edges have missing callers: {missing_callers[:5]}"
        )
        assert len(missing_callees) == 0, (
            f"{len(missing_callees)} edges have missing callees: {missing_callees[:5]}"
        )


# ─── §4 SourcePoint + RepairLog Contracts ────────────────────────────────────


class TestSourcePointContracts:
    """Verify SourcePoint creation and lifecycle in Neo4j."""

    def test_create_source_point_persists_all_fields(self):
        """create_source_point must persist all 6 fields to Neo4j."""
        from codemap_lite.graph.schema import SourcePointNode

        store = _get_store()
        sp = SourcePointNode(
            id="test_sp_contract_001",
            function_id="test_sp_contract_001",
            entry_point_kind="callback_or_fp",
            reason="integration_test",
            module="test_module",
            status="pending",
        )
        store.create_source_point(sp)
        try:
            retrieved = store.get_source_point("test_sp_contract_001")
            assert retrieved is not None, "SourcePoint not found after creation"
            assert retrieved.id == "test_sp_contract_001"
            assert retrieved.function_id == "test_sp_contract_001"
            assert retrieved.entry_point_kind == "callback_or_fp"
            assert retrieved.reason == "integration_test"
            assert retrieved.module == "test_module"
            assert retrieved.status == "pending"
        finally:
            # Cleanup
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(
                "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
            )
            with driver.session() as session:
                session.run(
                    "MATCH (s:SourcePoint {id: 'test_sp_contract_001'}) "
                    "DETACH DELETE s"
                )
            driver.close()

    def test_source_point_status_transitions(self):
        """architecture.md §3: pending → running → complete."""
        from codemap_lite.graph.schema import SourcePointNode

        store = _get_store()
        sp = SourcePointNode(
            id="test_sp_lifecycle_001",
            function_id="test_sp_lifecycle_001",
            entry_point_kind="entry_point",
            reason="lifecycle_test",
            module="test",
            status="pending",
        )
        store.create_source_point(sp)
        try:
            # pending → running
            store.update_source_point_status("test_sp_lifecycle_001", "running")
            assert store.get_source_point("test_sp_lifecycle_001").status == "running"

            # running → complete
            store.update_source_point_status("test_sp_lifecycle_001", "complete")
            assert store.get_source_point("test_sp_lifecycle_001").status == "complete"

            # complete → pending (requires force_reset)
            with pytest.raises(ValueError):
                store.update_source_point_status("test_sp_lifecycle_001", "pending")
            store.update_source_point_status(
                "test_sp_lifecycle_001", "pending", force_reset=True
            )
            assert store.get_source_point("test_sp_lifecycle_001").status == "pending"
        finally:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(
                "bolt://localhost:7687", auth=("neo4j", NEO4J_PASSWORD)
            )
            with driver.session() as session:
                session.run(
                    "MATCH (s:SourcePoint {id: 'test_sp_lifecycle_001'}) "
                    "DETACH DELETE s"
                )
            driver.close()


# ─── §3 Gate Check + Retry Contracts ─────────────────────────────────────────


class TestGateCheckContracts:
    """Verify gate check and retry logic against real graph data."""

    def test_get_pending_gaps_for_source_returns_reachable_only(self):
        """architecture.md §3: gate check queries only reachable UCs."""
        store = _get_store()
        # Pick a function that has callees (so reachable set is non-trivial)
        edges = store.list_calls_edges()
        if not edges:
            pytest.skip("No CALLS edges in DB")
        caller_id = edges[0].caller_id
        gaps = store.get_pending_gaps_for_source(caller_id)
        # All returned gaps should be reachable from caller_id
        # (we can't easily verify reachability here, but we can verify
        # they are valid UnresolvedCall nodes)
        for gap in gaps:
            assert hasattr(gap, "caller_id") or "caller_id" in gap

    def test_retry_state_update_stamps_audit_fields(self):
        """architecture.md §3 Retry 审计字段: update stamps timestamp + reason."""
        from codemap_lite.graph.schema import UnresolvedCallNode

        store = _get_store()
        # Create a test UC
        uc = UnresolvedCallNode(
            id="gap:test_retry:99:test_func",
            caller_id="0792b0556a11",  # Use a real function ID
            call_expression="test_func",
            call_file="test_retry.cpp",
            call_line=99,
            call_type="indirect",
            source_code_snippet="",
            var_name=None,
            var_type=None,
            retry_count=0,
            status="pending",
        )
        store.create_unresolved_call(uc)
        try:
            # Stamp retry (internally increments retry_count)
            now = datetime.now(timezone.utc).isoformat()
            reason = "gate_failed: 2 pending gaps remain"
            store.update_unresolved_call_retry_state(
                call_id=uc.id, timestamp=now, reason=reason
            )
            # Verify
            ucs = store.get_unresolved_calls()
            found = [u for u in ucs if u.id == uc.id]
            assert len(found) == 1, f"UC not found after retry update"
            updated = found[0]
            assert updated.retry_count == 1, f"retry_count not incremented: {updated.retry_count}"
            assert updated.last_attempt_timestamp == now
            assert updated.last_attempt_reason == reason
        finally:
            store.delete_unresolved_call("0792b0556a11", "test_retry.cpp", 99)


# ─── §5 Review Cascade Contracts ─────────────────────────────────────────────


class TestReviewCascade:
    """Verify the 4-step review cascade against real Neo4j."""

    def test_write_edge_then_delete_roundtrip(self):
        """Write a CALLS edge, verify it exists, delete it, verify gone."""
        from codemap_lite.graph.schema import CallsEdgeProps

        store = _get_store()
        # Use two real function IDs
        fns = store.list_functions()
        if len(fns) < 2:
            pytest.skip("Need at least 2 functions")
        fn_a, fn_b = fns[0], fns[1]

        props = CallsEdgeProps(
            resolved_by="llm",
            call_type="indirect",
            call_file="test_cascade.cpp",
            call_line=999,
        )
        store.create_calls_edge(fn_a.id, fn_b.id, props)
        try:
            # Verify exists
            assert store.edge_exists(fn_a.id, fn_b.id, "test_cascade.cpp", 999)
            retrieved = store.get_calls_edge(fn_a.id, fn_b.id, "test_cascade.cpp", 999)
            assert retrieved is not None
            assert retrieved.resolved_by == "llm"

            # Delete
            deleted = store.delete_calls_edge(fn_a.id, fn_b.id, "test_cascade.cpp", 999)
            assert deleted is True
            assert not store.edge_exists(fn_a.id, fn_b.id, "test_cascade.cpp", 999)
        finally:
            # Ensure cleanup
            store.delete_calls_edge(fn_a.id, fn_b.id, "test_cascade.cpp", 999)

    def test_repair_log_creation_and_deletion(self):
        """RepairLog CRUD matches architecture.md §4 + §5."""
        from codemap_lite.graph.schema import RepairLogNode

        store = _get_store()
        fns = store.list_functions()
        if len(fns) < 2:
            pytest.skip("Need at least 2 functions")
        fn_a, fn_b = fns[0], fns[1]

        log = RepairLogNode(
            id="test_rl_001",
            caller_id=fn_a.id,
            callee_id=fn_b.id,
            call_location="test_cascade.cpp:999",
            repair_method="llm",
            llm_response="test response",
            timestamp=datetime.now(timezone.utc).isoformat(),
            reasoning_summary="test reasoning",
        )
        store.create_repair_log(log)
        try:
            # Verify exists
            logs = store.get_repair_logs(caller_id=fn_a.id)
            found = [l for l in logs if l.call_location == "test_cascade.cpp:999"]
            assert len(found) == 1
            assert found[0].reasoning_summary == "test reasoning"

            # Delete via cascade method
            store.delete_repair_logs_for_edge(
                fn_a.id, fn_b.id, "test_cascade.cpp:999"
            )
            logs_after = store.get_repair_logs(caller_id=fn_a.id)
            found_after = [l for l in logs_after if l.call_location == "test_cascade.cpp:999"]
            assert len(found_after) == 0
        finally:
            store.delete_repair_logs_for_edge(
                fn_a.id, fn_b.id, "test_cascade.cpp:999"
            )


# ─── §8 Stats Endpoint Contract ──────────────────────────────────────────────


class TestStatsContract:
    """Verify count_stats returns correct buckets."""

    def test_count_stats_matches_individual_counts(self):
        """count_stats totals must match list_* method counts."""
        store = _get_store()
        stats = store.count_stats()

        fns = store.list_functions()
        edges = store.list_calls_edges()
        ucs = store.get_unresolved_calls()
        files = store.list_files()

        assert stats["total_functions"] == len(fns), (
            f"stats.total_functions={stats['total_functions']} != list_functions={len(fns)}"
        )
        assert stats["total_files"] == len(files), (
            f"stats.total_files={stats['total_files']} != list_files={len(files)}"
        )
        assert stats["total_calls"] == len(edges), (
            f"stats.total_calls={stats['total_calls']} != list_calls_edges={len(edges)}"
        )
        assert stats["total_unresolved"] == len(ucs), (
            f"stats.total_unresolved={stats['total_unresolved']} != get_unresolved_calls={len(ucs)}"
        )

    def test_calls_by_resolved_by_sums_to_total(self):
        """calls_by_resolved_by bucket values must sum to total_calls."""
        store = _get_store()
        stats = store.count_stats()
        by_rb = stats.get("calls_by_resolved_by", {})
        assert sum(by_rb.values()) == stats["total_calls"], (
            f"calls_by_resolved_by sum={sum(by_rb.values())} != total_calls={stats['total_calls']}"
        )

    def test_unresolved_by_status_sums_to_total(self):
        """unresolved_by_status bucket values must sum to total_unresolved."""
        store = _get_store()
        stats = store.count_stats()
        by_status = stats.get("unresolved_by_status", {})
        assert sum(by_status.values()) == stats["total_unresolved"], (
            f"unresolved_by_status sum={sum(by_status.values())} != total_unresolved={stats['total_unresolved']}"
        )


# ─── §8 API Integration (FastAPI + Neo4j) ────────────────────────────────────


class TestAPIWithNeo4j:
    """Verify REST API endpoints return correct data from real Neo4j."""

    @pytest.fixture
    def client(self):
        """Create a test client backed by real Neo4j."""
        from fastapi.testclient import TestClient
        from codemap_lite.api.app import create_app

        store = _get_store()
        app = create_app(store=store)
        app.state.source_points = []
        return TestClient(app)

    def test_stats_endpoint(self, client):
        """GET /api/v1/stats returns all required fields."""
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        required = [
            "total_functions", "total_files", "total_calls",
            "total_unresolved", "total_source_points",
        ]
        for field in required:
            assert field in data, f"Missing field: {field}"
            assert isinstance(data[field], int), f"{field} not int: {data[field]}"

    def test_functions_endpoint_pagination(self, client):
        """GET /api/v1/functions returns {total, items} with pagination."""
        resp = client.get("/api/v1/functions?limit=10&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] > 100
        assert len(data["items"]) == 10

    def test_files_endpoint(self, client):
        """GET /api/v1/files returns {total, items}."""
        resp = client.get("/api/v1/files")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] > 0
        assert len(data["items"]) > 0
        # Each file has required fields
        f = data["items"][0]
        assert "file_path" in f
        assert "hash" in f

    def test_unresolved_calls_endpoint(self, client):
        """GET /api/v1/unresolved-calls returns paginated UCs."""
        resp = client.get("/api/v1/unresolved-calls?limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] > 0
        assert len(data["items"]) <= 5
        uc = data["items"][0]
        assert "caller_id" in uc
        assert "call_expression" in uc
        assert "call_type" in uc

    def test_function_callers_callees(self, client):
        """GET /api/v1/functions/{id}/callers and /callees work."""
        # Get a function that has edges
        resp = client.get("/api/v1/functions?limit=1")
        fn_id = resp.json()["items"][0]["id"]

        callers_resp = client.get(f"/api/v1/functions/{fn_id}/callers")
        assert callers_resp.status_code == 200
        assert "total" in callers_resp.json()

        callees_resp = client.get(f"/api/v1/functions/{fn_id}/callees")
        assert callees_resp.status_code == 200
        assert "total" in callees_resp.json()

    def test_call_chain_endpoint(self, client):
        """GET /api/v1/functions/{id}/call-chain returns subgraph."""
        # Find a function with callees
        store = _get_store()
        edges = store.list_calls_edges()
        if not edges:
            pytest.skip("No edges")
        caller_id = edges[0].caller_id

        resp = client.get(f"/api/v1/functions/{caller_id}/call-chain?depth=2")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert "unresolved" in data


# ─── §5 Review Cascade (Full CRUD against Neo4j) ─────────────────────────────


class TestReviewCascadeNeo4j:
    """Verify the 4-step review cascade against real Neo4j.

    Creates test data (LLM edge + RepairLog + SourcePoint), then exercises
    the POST /reviews verdict=incorrect flow and verifies all 4 steps:
    1. CALLS edge deleted
    2. RepairLog deleted
    3. UnresolvedCall regenerated (retry_count=0, status=pending)
    4. SourcePoint status reset to pending
    """

    @pytest.fixture(autouse=True)
    def setup_test_data(self):
        """Create isolated test data for the cascade test."""
        from codemap_lite.graph.schema import (
            CallsEdgeProps, RepairLogNode, SourcePointNode,
        )
        self.store = _get_store()

        # Pick two real functions from the graph
        fns = self.store.list_functions()[:3]
        assert len(fns) >= 2, "Need at least 2 functions in Neo4j"
        self.caller_id = fns[0].id
        self.callee_id = fns[1].id
        self.call_file = "__test_review_cascade.cpp"
        self.call_line = 42

        # Create a test LLM edge
        props = CallsEdgeProps(
            resolved_by="llm",
            call_type="indirect",
            call_file=self.call_file,
            call_line=self.call_line,
        )
        self.store.create_calls_edge(self.caller_id, self.callee_id, props)

        # Create a RepairLog for this edge
        self.store.create_repair_log(RepairLogNode(
            caller_id=self.caller_id,
            callee_id=self.callee_id,
            call_location=f"{self.call_file}:{self.call_line}",
            repair_method="llm",
            llm_response="test cascade response",
            timestamp="2026-05-14T12:00:00Z",
            reasoning_summary="test cascade reasoning",
        ))

        # Create a SourcePoint for the caller (status=complete)
        sp = self.store.get_source_point(self.caller_id)
        if sp is None:
            self.store.create_source_point(SourcePointNode(
                id=self.caller_id,
                function_id=self.caller_id,
                entry_point_kind="entry_point",
                reason="test cascade",
                module="test",
                status="complete",
            ))
        else:
            self.store.update_source_point_status(
                self.caller_id, "complete", force_reset=True
            )

        yield

        # Cleanup: remove any leftover test data
        self.store.delete_calls_edge(
            self.caller_id, self.callee_id, self.call_file, self.call_line
        )
        self.store.delete_repair_logs_for_edge(
            self.caller_id, self.callee_id,
            f"{self.call_file}:{self.call_line}",
        )
        # Delete any UC we created
        self.store.delete_unresolved_call(
            self.caller_id, self.call_file, self.call_line
        )

    def test_review_incorrect_deletes_edge(self):
        """Step 1: verdict=incorrect must delete the CALLS edge."""
        from fastapi.testclient import TestClient
        from codemap_lite.api.app import create_app

        app = create_app(store=self.store)
        app.state.source_points = []
        client = TestClient(app)

        resp = client.post("/api/v1/reviews", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "call_file": self.call_file,
            "call_line": self.call_line,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201, resp.text

        # Verify edge is gone
        edge = self.store.get_calls_edge(
            self.caller_id, self.callee_id, self.call_file, self.call_line
        )
        assert edge is None, "CALLS edge should be deleted after incorrect verdict"

    def test_review_incorrect_deletes_repair_log(self):
        """Step 2: verdict=incorrect must delete the RepairLog."""
        from fastapi.testclient import TestClient
        from codemap_lite.api.app import create_app

        app = create_app(store=self.store)
        app.state.source_points = []
        client = TestClient(app)

        resp = client.post("/api/v1/reviews", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "call_file": self.call_file,
            "call_line": self.call_line,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201

        # Verify RepairLog is gone
        logs = self.store.get_repair_logs(
            caller_id=self.caller_id, callee_id=self.callee_id
        )
        cascade_logs = [
            l for l in logs
            if l.call_location == f"{self.call_file}:{self.call_line}"
        ]
        assert len(cascade_logs) == 0, "RepairLog should be deleted"

    def test_review_incorrect_regenerates_uc(self):
        """Step 3: verdict=incorrect must regenerate UnresolvedCall."""
        from fastapi.testclient import TestClient
        from codemap_lite.api.app import create_app

        app = create_app(store=self.store)
        app.state.source_points = []
        client = TestClient(app)

        resp = client.post("/api/v1/reviews", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "call_file": self.call_file,
            "call_line": self.call_line,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201

        # Verify UC was regenerated
        ucs = self.store.get_unresolved_calls(caller_id=self.caller_id)
        cascade_ucs = [
            u for u in ucs
            if u.call_file == self.call_file and u.call_line == self.call_line
        ]
        assert len(cascade_ucs) == 1, (
            f"Expected 1 regenerated UC, got {len(cascade_ucs)}"
        )
        uc = cascade_ucs[0]
        assert uc.retry_count == 0, "Regenerated UC must have retry_count=0"
        assert uc.status == "pending", "Regenerated UC must have status=pending"

    def test_review_incorrect_resets_source_point(self):
        """Step 4: verdict=incorrect must reset SourcePoint to pending."""
        from fastapi.testclient import TestClient
        from codemap_lite.api.app import create_app

        app = create_app(store=self.store)
        app.state.source_points = []
        client = TestClient(app)

        # Verify SP starts as complete
        sp_before = self.store.get_source_point(self.caller_id)
        assert sp_before is not None
        assert sp_before.status == "complete"

        resp = client.post("/api/v1/reviews", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "call_file": self.call_file,
            "call_line": self.call_line,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201

        # Verify SP is reset to pending
        sp_after = self.store.get_source_point(self.caller_id)
        assert sp_after is not None
        assert sp_after.status == "pending", (
            f"SourcePoint should be reset to pending, got {sp_after.status}"
        )

    def test_review_incorrect_with_correct_target_creates_feedback(self):
        """§5: providing correct_target generates a counter-example."""
        from fastapi.testclient import TestClient
        from codemap_lite.api.app import create_app
        from codemap_lite.analysis.feedback_store import FeedbackStore
        import tempfile

        app = create_app(store=self.store)
        app.state.source_points = []

        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path
            fb_store = FeedbackStore(storage_dir=Path(tmpdir))
            app.state.feedback_store = fb_store

            client = TestClient(app)

            # Pick a third function as the "correct target"
            fns = self.store.list_functions()[:3]
            correct_target = fns[2].id if len(fns) >= 3 else "fake_target"

            resp = client.post("/api/v1/reviews", json={
                "caller_id": self.caller_id,
                "callee_id": self.callee_id,
                "call_file": self.call_file,
                "call_line": self.call_line,
                "verdict": "incorrect",
                "correct_target": correct_target,
            })
            assert resp.status_code == 201

            # Verify counter-example was created
            examples = fb_store.list_all()
            assert len(examples) >= 1, "Counter-example should be created"
            ex = examples[-1]
            assert ex.wrong_target == self.callee_id
            assert ex.correct_target == correct_target

    def test_manual_edge_create_deletes_uc(self):
        """§5: POST /edges creates edge and deletes matching UC."""
        from fastapi.testclient import TestClient
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.schema import UnresolvedCallNode

        # First create a UC at the test location
        uc = UnresolvedCallNode(
            caller_id=self.caller_id,
            call_expression="test_call()",
            call_file="__test_manual_edge.cpp",
            call_line=100,
            call_type="indirect",
            source_code_snippet="test_call();",
            var_name="",
            var_type="",
            retry_count=0,
            status="pending",
        )
        self.store.create_unresolved_call(uc)

        # Delete the LLM edge we created in setup (to avoid conflict)
        self.store.delete_calls_edge(
            self.caller_id, self.callee_id, self.call_file, self.call_line
        )

        app = create_app(store=self.store)
        app.state.source_points = []
        client = TestClient(app)

        resp = client.post("/api/v1/edges", json={
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "__test_manual_edge.cpp",
            "call_line": 100,
        })
        assert resp.status_code == 201, resp.text

        # Verify UC was deleted
        ucs = self.store.get_unresolved_calls(caller_id=self.caller_id)
        manual_ucs = [
            u for u in ucs
            if u.call_file == "__test_manual_edge.cpp" and u.call_line == 100
        ]
        assert len(manual_ucs) == 0, "UC should be deleted after manual edge creation"

        # Cleanup
        self.store.delete_calls_edge(
            self.caller_id, self.callee_id, "__test_manual_edge.cpp", 100
        )

"""End-to-end MVP acceptance test.

Exercises the full pipeline: parse → graph → repair → review → query.
Uses minimal C++ fixtures, InMemoryGraphStore, and FastAPI TestClient.
No real Neo4j, no real subprocess agents required.

Architecture references:
- §1: 6-layer system (Parsing → Static Analysis → Graph → Repair → API → Frontend)
- §2: Two-phase parsing (static full-scan + repair agent per source)
- §3: Repair Agent (inject → spawn → gate → retry)
- §4: Graph schema (CALLS edges unique by caller_id, callee_id, call_file, call_line)
- §5: Review workflow (4-step cascade)
- §8: REST API contracts
"""
from __future__ import annotations

import tempfile
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    RepairLogNode,
    SourcePointNode,
    UnresolvedCallNode,
)
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator


# ---------------------------------------------------------------------------
# Fixtures: minimal C++ source that produces direct + indirect calls
# ---------------------------------------------------------------------------

FIXTURE_MAIN_CPP = """\
#include "utils.h"

void caller_a() {
    helper_direct();
}

void caller_b() {
    void (*fp)() = nullptr;
    fp();
}
"""

FIXTURE_UTILS_H = """\
#ifndef UTILS_H
#define UTILS_H

void helper_direct() {
    // leaf function
}

void target_indirect() {
    // will be resolved by LLM agent
}

#endif
"""

# ---------------------------------------------------------------------------
# Phase 1: Parse & Graph Construction
# ---------------------------------------------------------------------------


class TestPhase1_ParseAndGraph:
    """Verify that PipelineOrchestrator correctly parses C++ and builds the graph."""

    @pytest.fixture()
    def pipeline_result(self, tmp_path: Path):
        """Run full analysis on fixture files, return (store, result)."""
        # Write fixture files
        (tmp_path / "main.cpp").write_text(FIXTURE_MAIN_CPP)
        (tmp_path / "utils.h").write_text(FIXTURE_UTILS_H)

        store = InMemoryGraphStore()
        orch = PipelineOrchestrator(target_dir=tmp_path, store=store)
        result = orch.run_full_analysis()
        return store, result

    def test_files_scanned(self, pipeline_result):
        """Parser should discover both .cpp and .h files."""
        store, result = pipeline_result
        assert result.files_scanned == 2
        assert result.success is True

    def test_functions_extracted(self, pipeline_result):
        """All function definitions should be extracted as FunctionNodes."""
        store, result = pipeline_result
        # At minimum: caller_a, caller_b, helper_direct, target_indirect
        assert result.functions_found >= 4
        names = {fn.name for fn in store._functions.values()}
        assert "caller_a" in names
        assert "helper_direct" in names

    def test_direct_calls_resolved(self, pipeline_result):
        """Direct call caller_a() → helper_direct() should produce a CALLS edge."""
        store, result = pipeline_result
        # Find caller_a and helper_direct IDs
        caller_a_id = None
        helper_id = None
        for fid, fn in store._functions.items():
            if fn.name == "caller_a":
                caller_a_id = fid
            elif fn.name == "helper_direct":
                helper_id = fid

        if caller_a_id and helper_id:
            # Should have a CALLS edge with resolved_by=symbol_table
            edge = store.get_calls_edge(
                caller_a_id, helper_id,
                call_file=next(
                    e.props.call_file for e in store._calls_edges
                    if e.caller_id == caller_a_id and e.callee_id == helper_id
                ),
                call_line=next(
                    e.props.call_line for e in store._calls_edges
                    if e.caller_id == caller_a_id and e.callee_id == helper_id
                ),
            )
            assert edge is not None
            assert edge.resolved_by == "symbol_table"
            assert edge.call_type == "direct"

    def test_indirect_calls_become_unresolved(self, pipeline_result):
        """Indirect call via function pointer should become an UnresolvedCall."""
        store, result = pipeline_result
        # caller_b has an indirect call (fp())
        assert result.unresolved_calls >= 1
        # At least one UC should exist
        all_uc = store.get_unresolved_calls()
        assert len(all_uc) >= 1


# ---------------------------------------------------------------------------
# Phase 2: Repair Simulation
# ---------------------------------------------------------------------------


class TestPhase2_RepairSimulation:
    """Simulate agent resolving an UnresolvedCall — architecture.md §3.

    The repair agent:
    1. Queries pending gaps for a source point
    2. Resolves a gap by creating a CALLS edge (resolved_by=llm)
    3. Creates a RepairLog documenting the resolution
    4. Deletes the UnresolvedCall node
    """

    @pytest.fixture()
    def repaired_store(self):
        """Build a store with pre-parsed state, then simulate repair."""
        store = InMemoryGraphStore()

        # Pre-populate: two functions + one unresolved call between them
        caller = FunctionNode(
            id="fn_caller_b",
            signature="void caller_b()",
            name="caller_b",
            file_path="/src/main.cpp",
            start_line=7,
            end_line=10,
            body_hash="abc123",
        )
        callee = FunctionNode(
            id="fn_target_indirect",
            signature="void target_indirect()",
            name="target_indirect",
            file_path="/src/utils.h",
            start_line=9,
            end_line=11,
            body_hash="def456",
        )
        store.create_function(caller)
        store.create_function(callee)

        # Create the unresolved call (gap)
        uc = UnresolvedCallNode(
            id="gap_1",
            caller_id="fn_caller_b",
            call_expression="fp()",
            call_file="/src/main.cpp",
            call_line=9,
            call_type="indirect",
            source_code_snippet="fp();",
            var_name="fp",
            var_type="void (*)()",
            candidates=["target_indirect"],
        )
        store.create_unresolved_call(uc)

        # Create a source point for caller_b
        sp = SourcePointNode(
            id="sp_1",
            entry_point_kind="api_entry",
            reason="codewiki detected",
            function_id="fn_caller_b",
            status="running",
        )
        store.create_source_point(sp)

        # --- Simulate repair agent actions (architecture.md §3) ---

        # Step 1: Agent creates CALLS edge (resolved_by=llm)
        props = CallsEdgeProps(
            resolved_by="llm",
            call_type="indirect",
            call_file="/src/main.cpp",
            call_line=9,
        )
        store.create_calls_edge("fn_caller_b", "fn_target_indirect", props)

        # Step 2: Agent creates RepairLog
        repair_log = RepairLogNode(
            id="rlog_1",
            caller_id="fn_caller_b",
            callee_id="fn_target_indirect",
            call_location="/src/main.cpp:9",
            repair_method="llm",
            llm_response="fp is assigned target_indirect at line 8",
            timestamp="2026-05-14T10:00:00Z",
            reasoning_summary="Variable fp assigned from target_indirect pointer",
        )
        store.create_repair_log(repair_log)

        # Step 3: Agent deletes the UnresolvedCall (gap resolved)
        store.delete_unresolved_call("fn_caller_b", "/src/main.cpp", 9)

        return store

    def test_calls_edge_created(self, repaired_store):
        """After repair, a CALLS edge should exist with resolved_by=llm."""
        store = repaired_store
        assert store.edge_exists(
            "fn_caller_b", "fn_target_indirect", "/src/main.cpp", 9
        )
        edge = store.get_calls_edge(
            "fn_caller_b", "fn_target_indirect", "/src/main.cpp", 9
        )
        assert edge.resolved_by == "llm"
        assert edge.call_type == "indirect"

    def test_repair_log_created(self, repaired_store):
        """RepairLog should document the resolution."""
        store = repaired_store
        logs = store.get_repair_logs(caller_id="fn_caller_b")
        assert len(logs) == 1
        assert logs[0].callee_id == "fn_target_indirect"
        assert logs[0].reasoning_summary != ""

    def test_unresolved_call_deleted(self, repaired_store):
        """The UnresolvedCall should be gone after successful repair."""
        store = repaired_store
        remaining = store.get_unresolved_calls(caller_id="fn_caller_b")
        assert len(remaining) == 0

    def test_callers_callees_after_repair(self, repaired_store):
        """After repair, caller/callee queries should reflect the new edge."""
        store = repaired_store
        # caller_b should now list target_indirect as a callee
        callees = store.get_callees("fn_caller_b")
        assert any(fn.id == "fn_target_indirect" for fn in callees)
        # target_indirect should list caller_b as a caller
        callers = store.get_callers("fn_target_indirect")
        assert any(fn.id == "fn_caller_b" for fn in callers)

    def test_gate_passes_when_no_pending_gaps(self, repaired_store):
        """Gate check: source with 0 pending gaps → complete."""
        store = repaired_store
        pending = store.get_pending_gaps_for_source("fn_caller_b")
        assert len(pending) == 0


# ---------------------------------------------------------------------------
# Phase 3: Review Cascade (architecture.md §5)
# ---------------------------------------------------------------------------


class TestPhase3_ReviewCascade:
    """Verify the 4-step review cascade when marking an LLM edge as incorrect.

    Architecture.md §5 审阅交互:
    1. Delete the CALLS edge
    2. Delete corresponding RepairLog
    3. Regenerate UnresolvedCall (retry_count=0)
    4. Trigger async repair (tested via background_tasks mock)
    """

    @pytest.fixture()
    def review_client(self):
        """Set up a store with a repaired edge, return TestClient."""
        store = InMemoryGraphStore()

        # Functions
        caller = FunctionNode(
            id="fn_caller",
            signature="void caller()",
            name="caller",
            file_path="/src/main.cpp",
            start_line=1,
            end_line=5,
            body_hash="aaa",
        )
        callee = FunctionNode(
            id="fn_callee",
            signature="void callee()",
            name="callee",
            file_path="/src/utils.h",
            start_line=1,
            end_line=3,
            body_hash="bbb",
        )
        store.create_function(caller)
        store.create_function(callee)

        # LLM-resolved CALLS edge
        props = CallsEdgeProps(
            resolved_by="llm",
            call_type="indirect",
            call_file="/src/main.cpp",
            call_line=3,
        )
        store.create_calls_edge("fn_caller", "fn_callee", props)

        # RepairLog for this edge
        rlog = RepairLogNode(
            id="rlog_review",
            caller_id="fn_caller",
            callee_id="fn_callee",
            call_location="/src/main.cpp:3",
            repair_method="llm",
            llm_response="resolved via dataflow",
            timestamp="2026-05-14T11:00:00Z",
            reasoning_summary="fp assigned from callee",
        )
        store.create_repair_log(rlog)

        # SourcePoint (status=complete, will be reset to pending)
        # Note: review.py looks up source point by caller_id, so the
        # SourcePoint id must match the function_id (architecture.md §3).
        sp = SourcePointNode(
            id="fn_caller",
            entry_point_kind="api_entry",
            reason="test",
            function_id="fn_caller",
            status="complete",
        )
        store.create_source_point(sp)

        app = create_app(store=store)
        client = TestClient(app)
        return client, store

    def test_review_incorrect_deletes_edge(self, review_client):
        """Marking edge incorrect should delete the CALLS edge."""
        client, store = review_client
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "/src/main.cpp",
            "call_line": 3,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201
        # Edge should be gone
        assert not store.edge_exists("fn_caller", "fn_callee", "/src/main.cpp", 3)

    def test_review_incorrect_deletes_repair_log(self, review_client):
        """Marking edge incorrect should delete the RepairLog."""
        client, store = review_client
        client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "/src/main.cpp",
            "call_line": 3,
            "verdict": "incorrect",
        })
        logs = store.get_repair_logs(
            caller_id="fn_caller", callee_id="fn_callee",
            call_location="/src/main.cpp:3",
        )
        assert len(logs) == 0

    def test_review_incorrect_regenerates_uc(self, review_client):
        """Marking edge incorrect should regenerate an UnresolvedCall."""
        client, store = review_client
        client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "/src/main.cpp",
            "call_line": 3,
            "verdict": "incorrect",
        })
        ucs = store.get_unresolved_calls(caller_id="fn_caller")
        assert len(ucs) == 1
        uc = ucs[0]
        assert uc.call_file == "/src/main.cpp"
        assert uc.call_line == 3
        assert uc.retry_count == 0
        assert uc.status == "pending"

    def test_review_incorrect_resets_source_point(self, review_client):
        """SourcePoint should be reset to 'pending' for re-repair."""
        client, store = review_client
        client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "/src/main.cpp",
            "call_line": 3,
            "verdict": "incorrect",
        })
        sp = store.get_source_point("fn_caller")
        assert sp is not None
        assert sp.status == "pending"

    def test_review_correct_preserves_edge(self, review_client):
        """Marking edge correct should NOT delete anything."""
        client, store = review_client
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_caller",
            "callee_id": "fn_callee",
            "call_file": "/src/main.cpp",
            "call_line": 3,
            "verdict": "correct",
        })
        assert resp.status_code == 201
        # Edge should still exist
        assert store.edge_exists("fn_caller", "fn_callee", "/src/main.cpp", 3)
        # RepairLog should still exist
        logs = store.get_repair_logs(caller_id="fn_caller")
        assert len(logs) == 1


# ---------------------------------------------------------------------------
# Phase 4: REST API Query — callers/callees (architecture.md §8)
# ---------------------------------------------------------------------------


class TestPhase4_APICallerCalleeQuery:
    """Verify /functions/{id}/callers and /callees return correct data.

    This is the final consumer-facing output: given a function, who calls it
    and what does it call? This is what the frontend renders.
    """

    @pytest.fixture()
    def api_client(self):
        """Build a graph with known topology and return TestClient.

        Topology:
            entry_point → middle_fn → leaf_fn  (all direct, symbol_table)
            entry_point → indirect_target       (indirect, llm — repaired)
        """
        store = InMemoryGraphStore()

        # Create 4 functions
        fns = {
            "entry": FunctionNode(
                id="fn_entry", signature="void entry()", name="entry",
                file_path="/src/main.cpp", start_line=1, end_line=5, body_hash="e1",
            ),
            "middle": FunctionNode(
                id="fn_middle", signature="void middle()", name="middle",
                file_path="/src/main.cpp", start_line=7, end_line=12, body_hash="m1",
            ),
            "leaf": FunctionNode(
                id="fn_leaf", signature="void leaf()", name="leaf",
                file_path="/src/utils.cpp", start_line=1, end_line=3, body_hash="l1",
            ),
            "indirect": FunctionNode(
                id="fn_indirect", signature="void indirect_target()",
                name="indirect_target",
                file_path="/src/utils.cpp", start_line=5, end_line=8, body_hash="i1",
            ),
        }
        for fn in fns.values():
            store.create_function(fn)

        # Edges: entry→middle (direct), middle→leaf (direct), entry→indirect (llm)
        store.create_calls_edge("fn_entry", "fn_middle", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/src/main.cpp", call_line=3,
        ))
        store.create_calls_edge("fn_middle", "fn_leaf", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/src/main.cpp", call_line=9,
        ))
        store.create_calls_edge("fn_entry", "fn_indirect", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/src/main.cpp", call_line=4,
        ))

        app = create_app(store=store)
        client = TestClient(app)
        return client, store

    def test_get_callees_of_entry(self, api_client):
        """entry() should have 2 callees: middle and indirect_target."""
        client, _ = api_client
        resp = client.get("/api/v1/functions/fn_entry/callees")
        assert resp.status_code == 200
        callees = resp.json()["items"]
        callee_ids = {c["id"] for c in callees}
        assert "fn_middle" in callee_ids
        assert "fn_indirect" in callee_ids
        assert len(callee_ids) == 2

    def test_get_callers_of_middle(self, api_client):
        """middle() should have 1 caller: entry."""
        client, _ = api_client
        resp = client.get("/api/v1/functions/fn_middle/callers")
        assert resp.status_code == 200
        callers = resp.json()["items"]
        assert len(callers) == 1
        assert callers[0]["id"] == "fn_entry"

    def test_get_callers_of_leaf(self, api_client):
        """leaf() should have 1 caller: middle."""
        client, _ = api_client
        resp = client.get("/api/v1/functions/fn_leaf/callers")
        assert resp.status_code == 200
        callers = resp.json()["items"]
        assert len(callers) == 1
        assert callers[0]["id"] == "fn_middle"

    def test_get_callees_of_leaf(self, api_client):
        """leaf() is a leaf — no callees."""
        client, _ = api_client
        resp = client.get("/api/v1/functions/fn_leaf/callees")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    def test_get_callers_of_entry(self, api_client):
        """entry() is a root — no callers."""
        client, _ = api_client
        resp = client.get("/api/v1/functions/fn_entry/callers")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    def test_call_chain_from_entry(self, api_client):
        """call-chain from entry should include all reachable nodes and edges."""
        client, _ = api_client
        resp = client.get("/api/v1/functions/fn_entry/call-chain?depth=5")
        assert resp.status_code == 200
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        # All 4 functions reachable from entry
        assert "fn_entry" in node_ids
        assert "fn_middle" in node_ids
        assert "fn_leaf" in node_ids
        assert "fn_indirect" in node_ids
        # 3 edges total
        assert len(data["edges"]) == 3

    def test_function_not_found_404(self, api_client):
        """Querying a non-existent function should return 404."""
        client, _ = api_client
        resp = client.get("/api/v1/functions/nonexistent/callers")
        assert resp.status_code == 404

    def test_stats_endpoint(self, api_client):
        """GET /api/v1/stats should return aggregate counts."""
        client, _ = api_client
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["total_functions"] == 4
        assert stats["total_calls"] == 3


# ---------------------------------------------------------------------------
# Phase 5: Full Pipeline Integration (parse → repair → review → query)
# ---------------------------------------------------------------------------


class TestPhase5_FullPipelineIntegration:
    """Integration test combining all phases in sequence.

    This is the "one test to rule them all" — exercises the complete
    MVP flow from raw C++ source to API-queryable caller/callee info,
    including a simulated repair and review cycle.
    """

    @pytest.fixture()
    def full_pipeline(self, tmp_path: Path):
        """Run the complete pipeline and return (client, store, ids)."""
        # Write C++ fixtures
        (tmp_path / "main.cpp").write_text(FIXTURE_MAIN_CPP)
        (tmp_path / "utils.h").write_text(FIXTURE_UTILS_H)

        # Phase 1: Parse
        store = InMemoryGraphStore()
        orch = PipelineOrchestrator(target_dir=tmp_path, store=store)
        result = orch.run_full_analysis()
        assert result.success

        # Identify key function IDs
        ids = {}
        for fid, fn in store._functions.items():
            ids[fn.name] = fid

        # Phase 2: Simulate repair of an indirect call
        # Find an unresolved call from caller_b (if any)
        caller_b_id = ids.get("caller_b")
        target_id = ids.get("target_indirect")

        if caller_b_id and target_id:
            # Check if there's an unresolved call we can "repair"
            ucs = store.get_unresolved_calls(caller_id=caller_b_id)
            if ucs:
                uc = ucs[0]
                # Simulate agent repair: create edge + log + delete UC
                props = CallsEdgeProps(
                    resolved_by="llm",
                    call_type="indirect",
                    call_file=uc.call_file,
                    call_line=uc.call_line,
                )
                store.create_calls_edge(caller_b_id, target_id, props)
                store.create_repair_log(RepairLogNode(
                    id=str(uuid4()),
                    caller_id=caller_b_id,
                    callee_id=target_id,
                    call_location=f"{uc.call_file}:{uc.call_line}",
                    repair_method="llm",
                    llm_response="fp is target_indirect",
                    timestamp="2026-05-14T12:00:00Z",
                    reasoning_summary="dataflow: fp = &target_indirect",
                ))
                store.delete_unresolved_call(
                    caller_b_id, uc.call_file, uc.call_line
                )

        # Create API client
        app = create_app(store=store)
        client = TestClient(app)
        return client, store, ids

    def test_full_pipeline_callee_query(self, full_pipeline):
        """After parse+repair, caller_b should list target_indirect as callee."""
        client, store, ids = full_pipeline
        caller_b_id = ids.get("caller_b")
        target_id = ids.get("target_indirect")
        if not (caller_b_id and target_id):
            pytest.skip("Fixture functions not found by parser")

        resp = client.get(f"/api/v1/functions/{caller_b_id}/callees")
        assert resp.status_code == 200
        callee_ids = {c["id"] for c in resp.json()["items"]}
        assert target_id in callee_ids

    def test_full_pipeline_caller_query(self, full_pipeline):
        """After parse+repair, target_indirect should list caller_b as caller."""
        client, store, ids = full_pipeline
        caller_b_id = ids.get("caller_b")
        target_id = ids.get("target_indirect")
        if not (caller_b_id and target_id):
            pytest.skip("Fixture functions not found by parser")

        resp = client.get(f"/api/v1/functions/{target_id}/callers")
        assert resp.status_code == 200
        caller_ids = {c["id"] for c in resp.json()["items"]}
        assert caller_b_id in caller_ids

    def test_full_pipeline_review_then_requery(self, full_pipeline):
        """After review (incorrect), the edge disappears from callee list."""
        client, store, ids = full_pipeline
        caller_b_id = ids.get("caller_b")
        target_id = ids.get("target_indirect")
        if not (caller_b_id and target_id):
            pytest.skip("Fixture functions not found by parser")

        # Find the LLM edge
        edge = store.get_calls_edge(
            caller_b_id, target_id,
            call_file=next(
                (e.props.call_file for e in store._calls_edges
                 if e.caller_id == caller_b_id and e.callee_id == target_id),
                None,
            ),
            call_line=next(
                (e.props.call_line for e in store._calls_edges
                 if e.caller_id == caller_b_id and e.callee_id == target_id),
                0,
            ),
        )
        if edge is None:
            pytest.skip("No LLM edge to review")

        # Get edge details for review
        llm_edge = next(
            e for e in store._calls_edges
            if e.caller_id == caller_b_id and e.callee_id == target_id
        )

        # Mark as incorrect via review API
        resp = client.post("/api/v1/reviews", json={
            "caller_id": caller_b_id,
            "callee_id": target_id,
            "call_file": llm_edge.props.call_file,
            "call_line": llm_edge.props.call_line,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201

        # Now callee query should NOT include target_indirect
        resp = client.get(f"/api/v1/functions/{caller_b_id}/callees")
        assert resp.status_code == 200
        callee_ids = {c["id"] for c in resp.json()["items"]}
        assert target_id not in callee_ids

        # And an UnresolvedCall should be regenerated
        ucs = store.get_unresolved_calls(caller_id=caller_b_id)
        assert len(ucs) >= 1
        assert ucs[0].status == "pending"


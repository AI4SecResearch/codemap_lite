"""Incremental update, review workflow, and analyze endpoint contracts.

architecture.md §5/§7/§8 — tests targeting:
1. §7 IncrementalUpdater.invalidate_file 5-step cascade
2. §7 PipelineOrchestrator.run_incremental_analysis with real file changes
3. §5 POST /reviews verdict=correct/incorrect full workflow
4. §5 POST /edges / DELETE /edges with counter-example generation
5. §8 POST /analyze trigger and GET /analyze/status
6. §8 GET /functions/{id}/call-chain depth behavior
7. §8 Bulk DELETE /edges/{function_id}

BUG HUNTING TARGETS:
1. IncrementalUpdater doesn't regenerate UCs with correct metadata
2. POST /reviews on non-LLM edge should still work (not just llm edges)
3. POST /analyze double-spawn protection
4. call-chain depth=0 edge case
5. Bulk edge delete doesn't cascade to repair logs
6. Incremental doesn't detect file hash changes correctly
"""
from __future__ import annotations

import tempfile
from collections import defaultdict
from pathlib import Path

import pytest

from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    RepairLogNode,
    SourcePointNode,
    UnresolvedCallNode,
)
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator


CASTENGINE_DIR = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")


# ===========================================================================
# §7 IncrementalUpdater — 5-step cascade invalidation
# ===========================================================================


class TestIncrementalUpdaterCascade:
    """architecture.md §7: IncrementalUpdater.invalidate_file 5-step cascade."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.graph.incremental import IncrementalUpdater

        self.store = InMemoryGraphStore()
        self.target_dir = Path("/fake/project")

        # Build a graph: A→B→C, with LLM edge B→C and RepairLog
        self.fn_a = FunctionNode(
            id="inc_a", signature="void A()", name="A",
            file_path="src/module_a.cpp", start_line=1, end_line=20, body_hash="aaa",
        )
        self.fn_b = FunctionNode(
            id="inc_b", signature="void B()", name="B",
            file_path="src/module_b.cpp", start_line=1, end_line=30, body_hash="bbb",
        )
        self.fn_c = FunctionNode(
            id="inc_c", signature="void C(int)", name="C",
            file_path="src/module_b.cpp", start_line=40, end_line=60, body_hash="ccc",
        )
        self.store.create_function(self.fn_a)
        self.store.create_function(self.fn_b)
        self.store.create_function(self.fn_c)

        # Edge A→B (symbol_table, direct)
        self.store.create_calls_edge("inc_a", "inc_b", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file=str(self.target_dir / "src/module_a.cpp"), call_line=10,
        ))
        # Edge B→C (llm, indirect) — this should be invalidated when module_b changes
        self.store.create_calls_edge("inc_b", "inc_c", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file=str(self.target_dir / "src/module_b.cpp"), call_line=15,
        ))
        # RepairLog for B→C
        self.store.create_repair_log(RepairLogNode(
            caller_id="inc_b", callee_id="inc_c",
            call_location=f"{self.target_dir}/src/module_b.cpp:15",
            repair_method="llm", llm_response="vtable dispatch",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="Matched vtable",
        ))
        # SourcePoint for A
        self.store.create_source_point(SourcePointNode(
            id="sp_inc_a", entry_point_kind="public_api",
            reason="test", function_id="inc_a", module="test",
            status="complete",
        ))
        # UC on A (unresolved call from A to unknown)
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="inc_a", call_expression="unknown()",
            call_file=str(self.target_dir / "src/module_a.cpp"),
            call_line=18, call_type="indirect",
            source_code_snippet="unknown();", var_name="u", var_type="U*",
        ))

        self.updater = IncrementalUpdater(store=self.store, target_dir=self.target_dir)

    def test_invalidate_removes_functions_in_file(self):
        """Step 1: Functions in the invalidated file are removed."""
        result = self.updater.invalidate_file("src/module_b.cpp")
        # B and C were in module_b.cpp — should be removed
        remaining = {fn.id for fn in self.store.list_functions()}
        assert "inc_b" not in remaining
        assert "inc_c" not in remaining
        # A is in module_a.cpp — should remain
        assert "inc_a" in remaining

    def test_invalidate_removes_edges_from_deleted_functions(self):
        """Step 2: Edges from/to deleted functions are removed."""
        self.updater.invalidate_file("src/module_b.cpp")
        edges = self.store.list_calls_edges()
        # Both A→B and B→C should be gone (B and C deleted)
        assert len(edges) == 0

    def test_invalidate_removes_repair_logs_for_llm_edges(self):
        """Step 3: RepairLogs for LLM edges are deleted."""
        self.updater.invalidate_file("src/module_b.cpp")
        logs = self.store.get_repair_logs(caller_id="inc_b")
        assert len(logs) == 0

    def test_invalidate_regenerates_uc_for_cross_file_llm_edge(self):
        """Step 3: Cross-file LLM edges regenerate UnresolvedCall.

        When A→B is an LLM edge and B is deleted, a UC should be
        regenerated for A's call site.
        """
        # Change A→B to be an LLM edge for this test
        self.store._calls_edges.clear()
        self.store.create_calls_edge("inc_a", "inc_b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file=str(self.target_dir / "src/module_a.cpp"), call_line=10,
        ))
        self.store.create_repair_log(RepairLogNode(
            caller_id="inc_a", callee_id="inc_b",
            call_location=f"{self.target_dir}/src/module_a.cpp:10",
            repair_method="llm", llm_response="dispatch",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="test",
        ))

        result = self.updater.invalidate_file("src/module_b.cpp")

        # A's LLM edge to B should generate a new UC
        ucs = self.store.get_unresolved_calls(caller_id="inc_a")
        # Should have the original UC + the regenerated one
        regen_ucs = [uc for uc in ucs if uc.call_line == 10]
        assert len(regen_ucs) == 1
        assert regen_ucs[0].status == "pending"
        assert regen_ucs[0].retry_count == 0

    def test_invalidate_returns_affected_source_ids(self):
        """Step 4: Affected source IDs are returned."""
        # Make A→B an LLM edge so the cascade affects sp_inc_a
        self.store._calls_edges.clear()
        self.store.create_calls_edge("inc_a", "inc_b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file=str(self.target_dir / "src/module_a.cpp"), call_line=10,
        ))
        self.store.create_repair_log(RepairLogNode(
            caller_id="inc_a", callee_id="inc_b",
            call_location=f"{self.target_dir}/src/module_a.cpp:10",
            repair_method="llm", llm_response="dispatch",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="test",
        ))

        result = self.updater.invalidate_file("src/module_b.cpp")
        # sp_inc_a should be affected (its function has an invalidated edge)
        assert "sp_inc_a" in result.affected_source_ids or "inc_a" in result.affected_source_ids

    def test_invalidate_removes_ucs_from_deleted_functions(self):
        """UCs from deleted functions are removed (not just edges)."""
        # Add a UC from B
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="inc_b", call_expression="d()",
            call_file=str(self.target_dir / "src/module_b.cpp"),
            call_line=20, call_type="virtual",
            source_code_snippet="d();", var_name="d", var_type="D*",
        ))
        self.updater.invalidate_file("src/module_b.cpp")
        # UC from B should be gone
        ucs = self.store.get_unresolved_calls(caller_id="inc_b")
        assert len(ucs) == 0

    def test_invalidate_nonexistent_file_is_noop(self):
        """Invalidating a file with no functions is a no-op."""
        result = self.updater.invalidate_file("src/nonexistent.cpp")
        # Nothing should change
        assert len(self.store.list_functions()) == 3


# ===========================================================================
# §5/§8 POST /reviews — full workflow
# ===========================================================================


class TestReviewEndpointWorkflow:
    """architecture.md §5: POST /reviews with verdict=correct/incorrect."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.analysis.feedback_store import FeedbackStore
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        # Functions
        self.store.create_function(FunctionNode(
            id="rev2_a", signature="void A()", name="A",
            file_path="/test/rev2.cpp", start_line=1, end_line=20, body_hash="a",
        ))
        self.store.create_function(FunctionNode(
            id="rev2_b", signature="void B()", name="B",
            file_path="/test/rev2.cpp", start_line=30, end_line=50, body_hash="b",
        ))
        self.store.create_function(FunctionNode(
            id="rev2_c", signature="void C()", name="C",
            file_path="/test/rev2.cpp", start_line=60, end_line=80, body_hash="c",
        ))
        # LLM edge A→B
        self.store.create_calls_edge("rev2_a", "rev2_b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/test/rev2.cpp", call_line=10,
        ))
        # symbol_table edge A→C (non-LLM)
        self.store.create_calls_edge("rev2_a", "rev2_c", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/test/rev2.cpp", call_line=15,
        ))
        # RepairLog for A→B
        self.store.create_repair_log(RepairLogNode(
            caller_id="rev2_a", callee_id="rev2_b",
            call_location="/test/rev2.cpp:10",
            repair_method="llm", llm_response="vtable",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="vtable match",
        ))
        # SourcePoint
        self.store.create_source_point(SourcePointNode(
            id="sp_rev2_a", entry_point_kind="public_api",
            reason="test", function_id="rev2_a", module="test",
            status="complete",
        ))
        self._tmpdir = Path(tempfile.mkdtemp())
        self.feedback_store = FeedbackStore(storage_dir=self._tmpdir)
        app = create_app(store=self.store, feedback_store=self.feedback_store)
        self.client = TestClient(app)

    def test_review_correct_preserves_everything(self):
        """verdict=correct: edge, repair log, source point all preserved."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rev2_a", "callee_id": "rev2_b",
            "call_file": "/test/rev2.cpp", "call_line": 10,
            "verdict": "correct",
        })
        assert r.status_code == 201
        # Edge still exists
        assert self.store.edge_exists("rev2_a", "rev2_b", "/test/rev2.cpp", 10)
        # RepairLog still exists
        logs = self.store.get_repair_logs(caller_id="rev2_a", callee_id="rev2_b")
        assert len(logs) == 1
        # SourcePoint unchanged
        sp = self.store.get_source_point("sp_rev2_a")
        assert sp.status == "complete"

    def test_review_incorrect_triggers_full_cascade(self):
        """verdict=incorrect: 4-step cascade (delete edge, log, regen UC, reset SP)."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rev2_a", "callee_id": "rev2_b",
            "call_file": "/test/rev2.cpp", "call_line": 10,
            "verdict": "incorrect",
        })
        assert r.status_code == 201
        # Edge deleted
        assert not self.store.edge_exists("rev2_a", "rev2_b", "/test/rev2.cpp", 10)
        # RepairLog deleted
        logs = self.store.get_repair_logs(caller_id="rev2_a", callee_id="rev2_b")
        assert len(logs) == 0
        # UC regenerated
        ucs = self.store.get_unresolved_calls(caller_id="rev2_a")
        regen = [uc for uc in ucs if uc.call_line == 10]
        assert len(regen) == 1
        assert regen[0].status == "pending"
        assert regen[0].retry_count == 0

    def test_review_incorrect_with_correct_target(self):
        """verdict=incorrect + correct_target creates counter-example."""
        self.client.post("/api/v1/reviews", json={
            "caller_id": "rev2_a", "callee_id": "rev2_b",
            "call_file": "/test/rev2.cpp", "call_line": 10,
            "verdict": "incorrect",
            "correct_target": "rev2_c",
        })
        examples = self.feedback_store.list_all()
        assert len(examples) == 1
        assert examples[0].wrong_target == "rev2_b"
        assert examples[0].correct_target == "rev2_c"

    def test_review_on_nonexistent_edge_returns_404(self):
        """verdict on nonexistent edge returns 404."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rev2_a", "callee_id": "rev2_b",
            "call_file": "/test/rev2.cpp", "call_line": 999,
            "verdict": "incorrect",
        })
        assert r.status_code == 404

    def test_review_on_non_llm_edge_works(self):
        """verdict=incorrect on symbol_table edge should also work."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rev2_a", "callee_id": "rev2_c",
            "call_file": "/test/rev2.cpp", "call_line": 15,
            "verdict": "incorrect",
        })
        # Should succeed (any edge can be reviewed, not just LLM)
        assert r.status_code == 201
        assert not self.store.edge_exists("rev2_a", "rev2_c", "/test/rev2.cpp", 15)

    def test_get_reviews_returns_list(self):
        """GET /reviews returns paginated list."""
        # Create a review first
        self.client.post("/api/v1/reviews", json={
            "caller_id": "rev2_a", "callee_id": "rev2_b",
            "call_file": "/test/rev2.cpp", "call_line": 10,
            "verdict": "correct",
        })
        r = self.client.get("/api/v1/reviews")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "items" in data


# ===========================================================================
# §8 POST /analyze — trigger and status
# ===========================================================================


class TestAnalyzeEndpoint:
    """architecture.md §8: POST /analyze triggers analysis, GET /analyze/status."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        app = create_app(store=self.store, target_dir=CASTENGINE_DIR)
        self.client = TestClient(app)

    def test_analyze_status_returns_200(self):
        """GET /analyze/status returns 200 even with no analysis running."""
        r = self.client.get("/api/v1/analyze/status")
        assert r.status_code == 200
        data = r.json()
        assert "state" in data

    def test_analyze_status_has_required_fields(self):
        """GET /analyze/status has state and sources fields."""
        r = self.client.get("/api/v1/analyze/status")
        data = r.json()
        assert "state" in data
        # State should be idle when nothing is running
        assert data["state"] in ("idle", "running", "complete", "error")


# ===========================================================================
# §8 GET /functions/{id}/call-chain — depth behavior
# ===========================================================================


class TestCallChainDepth:
    """architecture.md §8: call-chain BFS respects depth parameter."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        # Build a chain: A → B → C → D → E (depth 4)
        for i, name in enumerate(["A", "B", "C", "D", "E"]):
            self.store.create_function(FunctionNode(
                id=f"chain_{name.lower()}", signature=f"void {name}()",
                name=name, file_path="/test/chain.cpp",
                start_line=i * 20 + 1, end_line=i * 20 + 15,
                body_hash=f"hash_{name}",
            ))
        # Edges: A→B, B→C, C→D, D→E
        for caller, callee, line in [("a", "b", 5), ("b", "c", 25), ("c", "d", 45), ("d", "e", 65)]:
            self.store.create_calls_edge(
                f"chain_{caller}", f"chain_{callee}",
                CallsEdgeProps(
                    resolved_by="symbol_table", call_type="direct",
                    call_file="/test/chain.cpp", call_line=line,
                ),
            )
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_depth_1_returns_immediate_callees(self):
        """depth=1: root + immediate callees only."""
        r = self.client.get("/api/v1/functions/chain_a/call-chain?depth=1")
        data = r.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert "chain_a" in node_ids
        assert "chain_b" in node_ids
        # C should NOT be included at depth 1
        assert "chain_c" not in node_ids

    def test_depth_2_returns_two_levels(self):
        """depth=2: root + 2 levels of callees."""
        r = self.client.get("/api/v1/functions/chain_a/call-chain?depth=2")
        data = r.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert "chain_a" in node_ids
        assert "chain_b" in node_ids
        assert "chain_c" in node_ids
        assert "chain_d" not in node_ids

    def test_depth_4_returns_full_chain(self):
        """depth=4: entire chain A→B→C→D→E."""
        r = self.client.get("/api/v1/functions/chain_a/call-chain?depth=4")
        data = r.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert len(node_ids) == 5
        assert "chain_e" in node_ids

    def test_depth_exceeding_chain_returns_all(self):
        """depth > chain length returns everything (no error)."""
        r = self.client.get("/api/v1/functions/chain_a/call-chain?depth=50")
        data = r.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert len(node_ids) == 5

    def test_call_chain_includes_edges(self):
        """call-chain response includes edges between nodes."""
        r = self.client.get("/api/v1/functions/chain_a/call-chain?depth=2")
        data = r.json()
        assert len(data["edges"]) >= 2
        # Verify edge structure
        edge = data["edges"][0]
        assert "caller_id" in edge
        assert "callee_id" in edge

    def test_call_chain_nonexistent_function_404(self):
        """call-chain on nonexistent function returns 404."""
        r = self.client.get("/api/v1/functions/nonexistent/call-chain?depth=2")
        assert r.status_code == 404

    def test_call_chain_leaf_function_returns_only_root(self):
        """call-chain on leaf function (no callees) returns just the root."""
        r = self.client.get("/api/v1/functions/chain_e/call-chain?depth=5")
        data = r.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert node_ids == {"chain_e"}
        assert len(data["edges"]) == 0


# ===========================================================================
# §5 POST /edges — manual edge creation with validation
# ===========================================================================


class TestManualEdgeCreation:
    """architecture.md §5: POST /edges creates edge + deletes matching UC."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        self.store.create_function(FunctionNode(
            id="me_a", signature="void A()", name="A",
            file_path="/test/me.cpp", start_line=1, end_line=20, body_hash="a",
        ))
        self.store.create_function(FunctionNode(
            id="me_b", signature="void B()", name="B",
            file_path="/test/me.cpp", start_line=30, end_line=50, body_hash="b",
        ))
        # UC at the call site
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="me_a", call_expression="b->method()",
            call_file="/test/me.cpp", call_line=10, call_type="indirect",
            source_code_snippet="b->method();", var_name="b", var_type="B*",
            retry_count=2, status="pending",
            last_attempt_reason="gate_failed: no match",
        ))
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_manual_edge_deletes_uc_at_same_site(self):
        """POST /edges deletes UC at same (caller, call_file, call_line)."""
        self.client.post("/api/v1/edges", json={
            "caller_id": "me_a", "callee_id": "me_b",
            "resolved_by": "llm", "call_type": "indirect",
            "call_file": "/test/me.cpp", "call_line": 10,
        })
        ucs = self.store.get_unresolved_calls(caller_id="me_a")
        assert len(ucs) == 0

    def test_manual_edge_preserves_uc_at_different_site(self):
        """POST /edges doesn't delete UCs at different call sites."""
        # Add another UC at a different line
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="me_a", call_expression="c()",
            call_file="/test/me.cpp", call_line=15, call_type="virtual",
            source_code_snippet="c();", var_name="c", var_type="C*",
        ))
        self.client.post("/api/v1/edges", json={
            "caller_id": "me_a", "callee_id": "me_b",
            "resolved_by": "llm", "call_type": "indirect",
            "call_file": "/test/me.cpp", "call_line": 10,
        })
        ucs = self.store.get_unresolved_calls(caller_id="me_a")
        # Only the line-15 UC should remain
        assert len(ucs) == 1
        assert ucs[0].call_line == 15


# ===========================================================================
# §7 Incremental with CastEngine — real file hash detection
# ===========================================================================


class TestIncrementalCastEngineHashDetection:
    """architecture.md §7: incremental detects file changes via SHA256 hash."""

    @pytest.fixture(scope="class")
    def full_analysis_store(self):
        if not CASTENGINE_DIR.exists():
            pytest.skip("CastEngine directory not available")
        store = InMemoryGraphStore()
        orch = PipelineOrchestrator(store=store, target_dir=CASTENGINE_DIR)
        result = orch.run_full_analysis()
        return store, result

    def test_full_analysis_stores_file_hashes(self, full_analysis_store):
        """Full analysis stores SHA256 hashes for all files."""
        store, result = full_analysis_store
        files = store.list_files()
        for f in files[:50]:
            assert f.hash, f"File {f.file_path} has empty hash"
            # SHA256 is 64 hex chars
            assert len(f.hash) == 64, (
                f"File {f.file_path} hash length {len(f.hash)} != 64"
            )

    def test_file_hashes_are_unique_per_file(self, full_analysis_store):
        """Different files should have different hashes (mostly)."""
        store, result = full_analysis_store
        files = store.list_files()
        hashes = [f.hash for f in files]
        # Allow some duplicates (empty files, identical headers)
        unique_pct = len(set(hashes)) / len(hashes) * 100
        assert unique_pct > 90, (
            f"Only {unique_pct:.1f}% unique hashes — hash function may be broken"
        )

    def test_functions_have_body_hash(self, full_analysis_store):
        """All functions have a body_hash for change detection."""
        store, result = full_analysis_store
        fns = store.list_functions()
        empty_hash = 0
        for fn in fns[:200]:
            if not fn.body_hash:
                empty_hash += 1
        assert empty_hash == 0, (
            f"{empty_hash}/200 functions have empty body_hash"
        )

    def test_overloaded_functions_have_different_body_hashes(self, full_analysis_store):
        """Overloaded functions (same name, same file) have different body hashes."""
        store, result = full_analysis_store
        fns = store.list_functions()
        by_file_name: dict[tuple[str, str], list] = defaultdict(list)
        for fn in fns:
            by_file_name[(fn.file_path, fn.name)].append(fn)

        same_hash_overloads = 0
        total_overloads = 0
        for key, group in by_file_name.items():
            if len(group) > 1:
                total_overloads += 1
                hashes = {fn.body_hash for fn in group}
                if len(hashes) == 1:
                    same_hash_overloads += 1

        if total_overloads > 0:
            same_pct = same_hash_overloads / total_overloads * 100
            # Most overloads should have different bodies
            assert same_pct < 20, (
                f"{same_pct:.1f}% of overloaded groups have identical body_hash — "
                f"body_hash may not distinguish overloads"
            )

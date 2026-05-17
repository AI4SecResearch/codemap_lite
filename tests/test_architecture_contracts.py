"""Contract tests verifying architecture.md specifications using InMemoryGraphStore.

These tests do NOT require Neo4j — they validate the API and store contracts
using the in-memory implementation. They cover:
- §5 审阅交互: POST/DELETE /edges cascade, POST /reviews verdict=incorrect
- §3 SourcePoint lifecycle state machine
- §3 RepairLog triple-key deduplication
- §3 UnresolvedCall retry (max 3) + status transition
- §4 CALLS edge 4-tuple uniqueness
- §4 UnresolvedCall deduplication
- §8 REST endpoint contracts (/repair-logs, /source-points/reachable, /stats)
- §7 reset_unresolvable_gaps

Run: pytest tests/test_architecture_contracts.py -v
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator


# ===========================================================================
# §5 审阅交互 — POST /edges full cascade (manual edge creation)
# ===========================================================================


class TestEdgeCreateCascade:
    """architecture.md §5: POST /api/v1/edges creates edge + deletes matching UC."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, UnresolvedCallNode,
        )
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        # Create two functions
        self.fn_a = FunctionNode(
            id="edge_fn_a", signature="void A()", name="A",
            file_path="/test/edge.cpp", start_line=1, end_line=10, body_hash="aaa",
        )
        self.fn_b = FunctionNode(
            id="edge_fn_b", signature="void B()", name="B",
            file_path="/test/edge.cpp", start_line=20, end_line=30, body_hash="bbb",
        )
        self.store.create_function(self.fn_a)
        self.store.create_function(self.fn_b)
        # Create an UnresolvedCall at the same call site
        self.uc = UnresolvedCallNode(
            caller_id="edge_fn_a", call_expression="b->method()",
            call_file="/test/edge.cpp", call_line=5, call_type="indirect",
            source_code_snippet="b->method();", var_name="b", var_type="B*",
        )
        self.store.create_unresolved_call(self.uc)
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_create_edge_success(self):
        """POST /edges creates edge and returns 201."""
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "edge_fn_a",
            "callee_id": "edge_fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "/test/edge.cpp",
            "call_line": 5,
        })
        assert r.status_code == 201
        data = r.json()
        assert data["caller_id"] == "edge_fn_a"
        assert data["callee_id"] == "edge_fn_b"
        assert data["status"] == "created"

    def test_create_edge_deletes_matching_uc(self):
        """POST /edges must delete the matching UnresolvedCall (architecture.md §3)."""
        # Verify UC exists before
        ucs = self.store.get_unresolved_calls(caller_id="edge_fn_a")
        assert len(ucs) == 1

        self.client.post("/api/v1/edges", json={
            "caller_id": "edge_fn_a",
            "callee_id": "edge_fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "/test/edge.cpp",
            "call_line": 5,
        })

        # UC should be gone
        ucs_after = self.store.get_unresolved_calls(caller_id="edge_fn_a")
        assert len(ucs_after) == 0

    def test_create_edge_idempotency_409(self):
        """POST /edges returns 409 if edge already exists (4-tuple uniqueness)."""
        self.client.post("/api/v1/edges", json={
            "caller_id": "edge_fn_a",
            "callee_id": "edge_fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "/test/edge.cpp",
            "call_line": 5,
        })
        # Second attempt → 409
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "edge_fn_a",
            "callee_id": "edge_fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "/test/edge.cpp",
            "call_line": 5,
        })
        assert r.status_code == 409

    def test_create_edge_validates_resolved_by(self):
        """POST /edges rejects invalid resolved_by values."""
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "edge_fn_a",
            "callee_id": "edge_fn_b",
            "resolved_by": "magic",
            "call_type": "indirect",
            "call_file": "/test/edge.cpp",
            "call_line": 5,
        })
        assert r.status_code == 422

    def test_create_edge_validates_call_type(self):
        """POST /edges rejects invalid call_type values."""
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "edge_fn_a",
            "callee_id": "edge_fn_b",
            "resolved_by": "llm",
            "call_type": "unknown",
            "call_file": "/test/edge.cpp",
            "call_line": 5,
        })
        assert r.status_code == 422

    def test_create_edge_caller_not_found(self):
        """POST /edges returns 404 if caller function doesn't exist."""
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "nonexistent",
            "callee_id": "edge_fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "/test/edge.cpp",
            "call_line": 5,
        })
        assert r.status_code == 404

    def test_create_edge_callee_not_found(self):
        """POST /edges returns 404 if callee function doesn't exist."""
        r = self.client.post("/api/v1/edges", json={
            "caller_id": "edge_fn_a",
            "callee_id": "nonexistent",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "/test/edge.cpp",
            "call_line": 5,
        })
        assert r.status_code == 404


# ===========================================================================
# §5 审阅交互 — DELETE /edges full 4-step cascade
# ===========================================================================


class TestEdgeDeleteCascade:
    """architecture.md §5: DELETE /api/v1/edges triggers 4-step cascade.

    Steps: delete edge → delete RepairLog → regenerate UC → reset SourcePoint.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.analysis.feedback_store import FeedbackStore
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, RepairLogNode, SourcePointNode,
        )
        from fastapi.testclient import TestClient
        import tempfile
        from pathlib import Path

        self.store = InMemoryGraphStore()
        # Two functions
        self.fn_a = FunctionNode(
            id="del_fn_a", signature="void A()", name="A",
            file_path="/test/del.cpp", start_line=1, end_line=10, body_hash="aaa",
        )
        self.fn_b = FunctionNode(
            id="del_fn_b", signature="void B()", name="B",
            file_path="/test/del.cpp", start_line=20, end_line=30, body_hash="bbb",
        )
        self.fn_c = FunctionNode(
            id="del_fn_c", signature="void C()", name="C",
            file_path="/test/del.cpp", start_line=40, end_line=50, body_hash="ccc",
        )
        self.store.create_function(self.fn_a)
        self.store.create_function(self.fn_b)
        self.store.create_function(self.fn_c)
        # Create a CALLS edge A→B
        self.edge_props = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/test/del.cpp", call_line=5,
        )
        self.store.create_calls_edge("del_fn_a", "del_fn_b", self.edge_props)
        # Create a RepairLog for this edge
        self.repair_log = RepairLogNode(
            caller_id="del_fn_a", callee_id="del_fn_b",
            call_location="/test/del.cpp:5",
            repair_method="llm", llm_response="vtable dispatch",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="Matched vtable pattern",
        )
        self.store.create_repair_log(self.repair_log)
        # Create a SourcePoint for fn_a (status=complete)
        self.sp = SourcePointNode(
            id="sp_del_a", entry_point_kind="public_api",
            reason="test", function_id="del_fn_a", module="test",
            status="complete",
        )
        self.store.create_source_point(self.sp)
        # FeedbackStore
        self._tmpdir = Path(tempfile.mkdtemp())
        self.feedback_store = FeedbackStore(storage_dir=self._tmpdir)
        app = create_app(store=self.store, feedback_store=self.feedback_store)
        self.client = TestClient(app)

    def test_delete_edge_returns_204(self):
        """DELETE /edges returns 204 on success."""
        r = self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "del_fn_a", "callee_id": "del_fn_b",
            "call_file": "/test/del.cpp", "call_line": 5,
        })
        assert r.status_code == 204

    def test_delete_edge_removes_calls_edge(self):
        """Step 1: CALLS edge is deleted."""
        self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "del_fn_a", "callee_id": "del_fn_b",
            "call_file": "/test/del.cpp", "call_line": 5,
        })
        assert not self.store.edge_exists("del_fn_a", "del_fn_b", "/test/del.cpp", 5)

    def test_delete_edge_removes_repair_log(self):
        """Step 2: Corresponding RepairLog is deleted."""
        self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "del_fn_a", "callee_id": "del_fn_b",
            "call_file": "/test/del.cpp", "call_line": 5,
        })
        logs = self.store.get_repair_logs(
            caller_id="del_fn_a", callee_id="del_fn_b",
            call_location="/test/del.cpp:5",
        )
        assert len(logs) == 0

    def test_delete_edge_regenerates_uc(self):
        """Step 3: UnresolvedCall is regenerated with retry_count=0."""
        self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "del_fn_a", "callee_id": "del_fn_b",
            "call_file": "/test/del.cpp", "call_line": 5,
        })
        ucs = self.store.get_unresolved_calls(caller_id="del_fn_a")
        assert len(ucs) == 1
        uc = ucs[0]
        assert uc.call_file == "/test/del.cpp"
        assert uc.call_line == 5
        assert uc.retry_count == 0
        assert uc.status == "pending"
        # call_type should be preserved from the deleted edge
        assert uc.call_type == "indirect"

    def test_delete_edge_resets_source_point(self):
        """Step 4: SourcePoint status reset to pending."""
        self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "del_fn_a", "callee_id": "del_fn_b",
            "call_file": "/test/del.cpp", "call_line": 5,
        })
        sp = self.store.get_source_point("sp_del_a")
        assert sp is not None
        assert sp.status == "pending"

    def test_delete_edge_not_found(self):
        """DELETE /edges returns 404 if edge doesn't exist."""
        r = self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "del_fn_a", "callee_id": "del_fn_b",
            "call_file": "/test/del.cpp", "call_line": 999,
        })
        assert r.status_code == 404

    def test_delete_edge_with_correct_target_creates_counter_example(self):
        """§5: correct_target triggers counter-example generation."""
        self.client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "del_fn_a", "callee_id": "del_fn_b",
            "call_file": "/test/del.cpp", "call_line": 5,
            "correct_target": "del_fn_c",
        })
        examples = self.feedback_store.list_all()
        assert len(examples) == 1
        ex = examples[0]
        assert ex.wrong_target == "del_fn_b"
        assert ex.correct_target == "del_fn_c"
        assert ex.source_id == "del_fn_a"


# ===========================================================================
# §5 审阅交互 — POST /reviews verdict=incorrect full cascade
# ===========================================================================


class TestReviewIncorrectCascade:
    """architecture.md §5: verdict=incorrect triggers same 4-step cascade as DELETE /edges."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.analysis.feedback_store import FeedbackStore
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, RepairLogNode, SourcePointNode,
        )
        from fastapi.testclient import TestClient
        import tempfile
        from pathlib import Path

        self.store = InMemoryGraphStore()
        self.fn_a = FunctionNode(
            id="rev_fn_a", signature="void A()", name="A",
            file_path="/test/rev.cpp", start_line=1, end_line=10, body_hash="aaa",
        )
        self.fn_b = FunctionNode(
            id="rev_fn_b", signature="void B()", name="B",
            file_path="/test/rev.cpp", start_line=20, end_line=30, body_hash="bbb",
        )
        self.fn_c = FunctionNode(
            id="rev_fn_c", signature="void C()", name="C",
            file_path="/test/rev.cpp", start_line=40, end_line=50, body_hash="ccc",
        )
        self.store.create_function(self.fn_a)
        self.store.create_function(self.fn_b)
        self.store.create_function(self.fn_c)
        # Edge + RepairLog
        props = CallsEdgeProps(
            resolved_by="llm", call_type="virtual",
            call_file="/test/rev.cpp", call_line=7,
        )
        self.store.create_calls_edge("rev_fn_a", "rev_fn_b", props)
        self.store.create_repair_log(RepairLogNode(
            caller_id="rev_fn_a", callee_id="rev_fn_b",
            call_location="/test/rev.cpp:7",
            repair_method="llm", llm_response="virtual dispatch",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="Virtual call pattern",
        ))
        # SourcePoint
        self.sp = SourcePointNode(
            id="sp_rev_a", entry_point_kind="callback",
            reason="test", function_id="rev_fn_a", module="test",
            status="complete",
        )
        self.store.create_source_point(self.sp)
        self._tmpdir = Path(tempfile.mkdtemp())
        self.feedback_store = FeedbackStore(storage_dir=self._tmpdir)
        app = create_app(store=self.store, feedback_store=self.feedback_store)
        self.client = TestClient(app)

    def test_incorrect_review_deletes_edge(self):
        """verdict=incorrect deletes the CALLS edge."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rev_fn_a", "callee_id": "rev_fn_b",
            "call_file": "/test/rev.cpp", "call_line": 7,
            "verdict": "incorrect",
        })
        assert r.status_code == 201
        assert not self.store.edge_exists("rev_fn_a", "rev_fn_b", "/test/rev.cpp", 7)

    def test_incorrect_review_deletes_repair_log(self):
        """verdict=incorrect deletes the RepairLog."""
        self.client.post("/api/v1/reviews", json={
            "caller_id": "rev_fn_a", "callee_id": "rev_fn_b",
            "call_file": "/test/rev.cpp", "call_line": 7,
            "verdict": "incorrect",
        })
        logs = self.store.get_repair_logs(
            caller_id="rev_fn_a", callee_id="rev_fn_b",
            call_location="/test/rev.cpp:7",
        )
        assert len(logs) == 0

    def test_incorrect_review_regenerates_uc(self):
        """verdict=incorrect regenerates UC with retry_count=0, status=pending."""
        self.client.post("/api/v1/reviews", json={
            "caller_id": "rev_fn_a", "callee_id": "rev_fn_b",
            "call_file": "/test/rev.cpp", "call_line": 7,
            "verdict": "incorrect",
        })
        ucs = self.store.get_unresolved_calls(caller_id="rev_fn_a")
        assert len(ucs) == 1
        uc = ucs[0]
        assert uc.call_line == 7
        assert uc.retry_count == 0
        assert uc.status == "pending"
        assert uc.call_type == "virtual"

    def test_incorrect_review_resets_source_point(self):
        """verdict=incorrect resets SourcePoint to pending."""
        self.client.post("/api/v1/reviews", json={
            "caller_id": "rev_fn_a", "callee_id": "rev_fn_b",
            "call_file": "/test/rev.cpp", "call_line": 7,
            "verdict": "incorrect",
        })
        sp = self.store.get_source_point("sp_rev_a")
        assert sp.status == "pending"

    def test_incorrect_review_with_correct_target(self):
        """verdict=incorrect + correct_target creates counter-example."""
        self.client.post("/api/v1/reviews", json={
            "caller_id": "rev_fn_a", "callee_id": "rev_fn_b",
            "call_file": "/test/rev.cpp", "call_line": 7,
            "verdict": "incorrect",
            "correct_target": "rev_fn_c",
        })
        examples = self.feedback_store.list_all()
        assert len(examples) == 1
        assert examples[0].wrong_target == "rev_fn_b"
        assert examples[0].correct_target == "rev_fn_c"

    def test_incorrect_review_edge_not_found(self):
        """verdict=incorrect on nonexistent edge returns 404."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rev_fn_a", "callee_id": "rev_fn_b",
            "call_file": "/test/rev.cpp", "call_line": 999,
            "verdict": "incorrect",
        })
        assert r.status_code == 404

    def test_correct_review_preserves_edge(self):
        """verdict=correct does NOT delete the edge."""
        r = self.client.post("/api/v1/reviews", json={
            "caller_id": "rev_fn_a", "callee_id": "rev_fn_b",
            "call_file": "/test/rev.cpp", "call_line": 7,
            "verdict": "correct",
        })
        assert r.status_code == 201
        assert self.store.edge_exists("rev_fn_a", "rev_fn_b", "/test/rev.cpp", 7)


# ===========================================================================
# §3 SourcePoint 生命周期 — 状态机 + force_reset
# ===========================================================================


class TestSourcePointLifecycle:
    """architecture.md §3: SourcePoint status transitions are forward-only unless force_reset."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import SourcePointNode

        self.store = InMemoryGraphStore()
        self.sp = SourcePointNode(
            id="sp_lc_1", entry_point_kind="public_api",
            reason="lifecycle test", function_id="fn_lc_1",
            module="test", status="pending",
        )
        self.store.create_source_point(self.sp)

    def test_pending_to_running(self):
        """pending → running is valid."""
        self.store.update_source_point_status("sp_lc_1", "running")
        sp = self.store.get_source_point("sp_lc_1")
        assert sp.status == "running"

    def test_running_to_complete(self):
        """running → complete is valid."""
        self.store.update_source_point_status("sp_lc_1", "running")
        self.store.update_source_point_status("sp_lc_1", "complete")
        sp = self.store.get_source_point("sp_lc_1")
        assert sp.status == "complete"

    def test_running_to_partial_complete(self):
        """running → partial_complete is valid."""
        self.store.update_source_point_status("sp_lc_1", "running")
        self.store.update_source_point_status("sp_lc_1", "partial_complete")
        sp = self.store.get_source_point("sp_lc_1")
        assert sp.status == "partial_complete"

    def test_backward_transition_raises(self):
        """complete → pending without force_reset raises ValueError."""
        self.store.update_source_point_status("sp_lc_1", "running")
        self.store.update_source_point_status("sp_lc_1", "complete")
        with pytest.raises(ValueError, match="Invalid SourcePoint transition"):
            self.store.update_source_point_status("sp_lc_1", "pending")

    def test_force_reset_allows_backward(self):
        """force_reset=True allows complete → pending."""
        self.store.update_source_point_status("sp_lc_1", "running")
        self.store.update_source_point_status("sp_lc_1", "complete")
        self.store.update_source_point_status("sp_lc_1", "pending", force_reset=True)
        sp = self.store.get_source_point("sp_lc_1")
        assert sp.status == "pending"

    def test_invalid_status_raises(self):
        """Invalid status value raises ValueError."""
        with pytest.raises(ValueError, match="SourcePoint.status must be one of"):
            self.store.update_source_point_status("sp_lc_1", "invalid_state")

    def test_same_status_is_idempotent(self):
        """Setting same status is a no-op (no error)."""
        self.store.update_source_point_status("sp_lc_1", "pending")
        sp = self.store.get_source_point("sp_lc_1")
        assert sp.status == "pending"

    def test_pending_to_complete_skips_running(self):
        """pending → complete is NOT valid (must go through running)."""
        with pytest.raises(ValueError, match="Invalid SourcePoint transition"):
            self.store.update_source_point_status("sp_lc_1", "complete")


# ===========================================================================
# §3 RepairLog triple-key deduplication
# ===========================================================================


class TestRepairLogTripleKey:
    """architecture.md §4: RepairLog deduplicates on (caller_id, callee_id, call_location)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import RepairLogNode

        self.store = InMemoryGraphStore()
        self.log1 = RepairLogNode(
            caller_id="rl_fn_a", callee_id="rl_fn_b",
            call_location="/test/rl.cpp:10",
            repair_method="llm", llm_response="first attempt",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="First reasoning",
        )

    def test_first_insert_succeeds(self):
        """First RepairLog insert returns its id."""
        rid = self.store.create_repair_log(self.log1)
        assert rid == self.log1.id
        logs = self.store.get_repair_logs(caller_id="rl_fn_a")
        assert len(logs) == 1

    def test_duplicate_triple_key_overwrites(self):
        """Second insert with same triple-key overwrites (not duplicates)."""
        from codemap_lite.graph.schema import RepairLogNode

        self.store.create_repair_log(self.log1)
        log2 = RepairLogNode(
            caller_id="rl_fn_a", callee_id="rl_fn_b",
            call_location="/test/rl.cpp:10",
            repair_method="llm", llm_response="second attempt",
            timestamp="2026-05-15T01:00:00Z",
            reasoning_summary="Second reasoning",
        )
        rid2 = self.store.create_repair_log(log2)
        # Should reuse the same id
        assert rid2 == self.log1.id
        logs = self.store.get_repair_logs(caller_id="rl_fn_a")
        assert len(logs) == 1
        # Content should be updated
        assert logs[0].llm_response == "second attempt"
        assert logs[0].reasoning_summary == "Second reasoning"

    def test_different_call_location_creates_new(self):
        """Different call_location creates a separate RepairLog."""
        from codemap_lite.graph.schema import RepairLogNode

        self.store.create_repair_log(self.log1)
        log2 = RepairLogNode(
            caller_id="rl_fn_a", callee_id="rl_fn_b",
            call_location="/test/rl.cpp:20",  # different line
            repair_method="llm", llm_response="different site",
            timestamp="2026-05-15T01:00:00Z",
            reasoning_summary="Different site",
        )
        self.store.create_repair_log(log2)
        logs = self.store.get_repair_logs(caller_id="rl_fn_a")
        assert len(logs) == 2

    def test_different_callee_creates_new(self):
        """Different callee_id creates a separate RepairLog."""
        from codemap_lite.graph.schema import RepairLogNode

        self.store.create_repair_log(self.log1)
        log2 = RepairLogNode(
            caller_id="rl_fn_a", callee_id="rl_fn_c",  # different callee
            call_location="/test/rl.cpp:10",
            repair_method="llm", llm_response="different callee",
            timestamp="2026-05-15T01:00:00Z",
            reasoning_summary="Different callee",
        )
        self.store.create_repair_log(log2)
        logs = self.store.get_repair_logs(caller_id="rl_fn_a")
        assert len(logs) == 2


# ===========================================================================
# §3 UnresolvedCall retry — max 3 attempts + status transition
# ===========================================================================


class TestUnresolvedCallRetry:
    """architecture.md §3: UC retry_count max 3, then status=unresolvable."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import UnresolvedCallNode

        self.store = InMemoryGraphStore()
        self.uc = UnresolvedCallNode(
            caller_id="retry_fn_a", call_expression="foo()",
            call_file="/test/retry.cpp", call_line=10, call_type="indirect",
            source_code_snippet="foo();", var_name="x", var_type="Foo*",
            retry_count=0, status="pending",
        )
        self.store.create_unresolved_call(self.uc)
        self.uc_id = self.uc.id

    def test_first_retry_increments_count(self):
        """First retry: count 0→1, status stays pending."""
        self.store.update_unresolved_call_retry_state(
            self.uc_id, "2026-05-15T01:00:00Z", "gate_failed: no edges produced"
        )
        uc = self.store._unresolved_calls[self.uc_id]
        assert uc.retry_count == 1
        assert uc.status == "pending"
        assert uc.last_attempt_timestamp == "2026-05-15T01:00:00Z"
        assert uc.last_attempt_reason == "gate_failed: no edges produced"

    def test_second_retry_increments_count(self):
        """Second retry: count 1→2, status stays pending."""
        self.store.update_unresolved_call_retry_state(
            self.uc_id, "2026-05-15T01:00:00Z", "agent_error: timeout"
        )
        self.store.update_unresolved_call_retry_state(
            self.uc_id, "2026-05-15T02:00:00Z", "subprocess_timeout"
        )
        uc = self.store._unresolved_calls[self.uc_id]
        assert uc.retry_count == 2
        assert uc.status == "pending"

    def test_third_retry_marks_unresolvable(self):
        """Third retry: count reaches 3, status becomes unresolvable."""
        self.store.update_unresolved_call_retry_state(
            self.uc_id, "2026-05-15T01:00:00Z", "gate_failed: no edges"
        )
        self.store.update_unresolved_call_retry_state(
            self.uc_id, "2026-05-15T02:00:00Z", "agent_error: crash"
        )
        self.store.update_unresolved_call_retry_state(
            self.uc_id, "2026-05-15T03:00:00Z", "subprocess_crash"
        )
        uc = self.store._unresolved_calls[self.uc_id]
        assert uc.retry_count == 3
        assert uc.status == "unresolvable"

    def test_invalid_reason_category_raises(self):
        """Invalid reason category raises ValueError."""
        with pytest.raises(ValueError, match="last_attempt_reason category"):
            self.store.update_unresolved_call_retry_state(
                self.uc_id, "2026-05-15T01:00:00Z", "invalid_category: something"
            )

    def test_reason_too_long_raises(self):
        """Reason > 200 chars raises ValueError."""
        long_reason = "gate_failed: " + "x" * 200
        with pytest.raises(ValueError, match="≤200 chars"):
            self.store.update_unresolved_call_retry_state(
                self.uc_id, "2026-05-15T01:00:00Z", long_reason
            )

    def test_standalone_category_accepted(self):
        """Standalone category without colon is valid."""
        self.store.update_unresolved_call_retry_state(
            self.uc_id, "2026-05-15T01:00:00Z", "agent_exited_without_edge"
        )
        uc = self.store._unresolved_calls[self.uc_id]
        assert uc.last_attempt_reason == "agent_exited_without_edge"

    def test_all_five_categories_accepted(self):
        """All 5 valid categories are accepted."""
        from codemap_lite.graph.schema import VALID_REASON_CATEGORIES
        for cat in sorted(VALID_REASON_CATEGORIES):
            # Reset UC
            from codemap_lite.graph.schema import UnresolvedCallNode
            uc = UnresolvedCallNode(
                caller_id="retry_fn_a", call_expression="foo()",
                call_file="/test/retry.cpp", call_line=100 + hash(cat) % 100,
                call_type="indirect", source_code_snippet="foo();",
                var_name="x", var_type="Foo*",
            )
            uid = self.store.create_unresolved_call(uc)
            self.store.update_unresolved_call_retry_state(
                uid, "2026-05-15T01:00:00Z", f"{cat}: test"
            )


# ===========================================================================
# §8 GET /repair-logs endpoint with filtering
# ===========================================================================


class TestRepairLogsEndpoint:
    """architecture.md §8: GET /api/v1/repair-logs with caller/callee/location filters."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import FunctionNode, RepairLogNode
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        self.store.create_function(FunctionNode(
            id="rl_ep_a", signature="void A()", name="A",
            file_path="/test/rl_ep.cpp", start_line=1, end_line=10, body_hash="a",
        ))
        self.store.create_function(FunctionNode(
            id="rl_ep_b", signature="void B()", name="B",
            file_path="/test/rl_ep.cpp", start_line=20, end_line=30, body_hash="b",
        ))
        self.store.create_function(FunctionNode(
            id="rl_ep_c", signature="void C()", name="C",
            file_path="/test/rl_ep.cpp", start_line=40, end_line=50, body_hash="c",
        ))
        # Create 3 repair logs
        self.store.create_repair_log(RepairLogNode(
            caller_id="rl_ep_a", callee_id="rl_ep_b",
            call_location="/test/rl_ep.cpp:5",
            repair_method="llm", llm_response="resp1",
            timestamp="2026-05-15T00:00:00Z", reasoning_summary="r1",
        ))
        self.store.create_repair_log(RepairLogNode(
            caller_id="rl_ep_a", callee_id="rl_ep_c",
            call_location="/test/rl_ep.cpp:8",
            repair_method="llm", llm_response="resp2",
            timestamp="2026-05-15T01:00:00Z", reasoning_summary="r2",
        ))
        self.store.create_repair_log(RepairLogNode(
            caller_id="rl_ep_b", callee_id="rl_ep_c",
            call_location="/test/rl_ep.cpp:25",
            repair_method="llm", llm_response="resp3",
            timestamp="2026-05-15T02:00:00Z", reasoning_summary="r3",
        ))
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_list_all_repair_logs(self):
        """GET /repair-logs returns all logs with {total, items}."""
        r = self.client.get("/api/v1/repair-logs")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_filter_by_caller(self):
        """GET /repair-logs?caller=X filters correctly."""
        r = self.client.get("/api/v1/repair-logs", params={"caller": "rl_ep_a"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["caller_id"] == "rl_ep_a"

    def test_filter_by_callee(self):
        """GET /repair-logs?callee=X filters correctly."""
        r = self.client.get("/api/v1/repair-logs", params={"callee": "rl_ep_c"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["callee_id"] == "rl_ep_c"

    def test_filter_by_call_location(self):
        """GET /repair-logs?location=X filters correctly."""
        r = self.client.get("/api/v1/repair-logs", params={
            "location": "/test/rl_ep.cpp:5"
        })
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["caller_id"] == "rl_ep_a"
        assert data["items"][0]["callee_id"] == "rl_ep_b"

    def test_repair_log_has_all_fields(self):
        """Each RepairLog item has all required fields."""
        r = self.client.get("/api/v1/repair-logs")
        item = r.json()["items"][0]
        required_fields = {
            "id", "caller_id", "callee_id", "call_location",
            "repair_method", "llm_response", "timestamp", "reasoning_summary",
        }
        for field in required_fields:
            assert field in item, f"Missing field: {field}"


# ===========================================================================
# §8 GET /source-points/{id}/reachable — BFS subgraph
# ===========================================================================


class TestSourcePointReachable:
    """architecture.md §8: GET /source-points/{id}/reachable returns BFS subgraph."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, SourcePointNode, UnresolvedCallNode,
        )
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        # Chain: A → B → C, with UC on B
        self.store.create_function(FunctionNode(
            id="reach_a", signature="void A()", name="A",
            file_path="/test/reach.cpp", start_line=1, end_line=10, body_hash="a",
        ))
        self.store.create_function(FunctionNode(
            id="reach_b", signature="void B()", name="B",
            file_path="/test/reach.cpp", start_line=20, end_line=30, body_hash="b",
        ))
        self.store.create_function(FunctionNode(
            id="reach_c", signature="void C()", name="C",
            file_path="/test/reach.cpp", start_line=40, end_line=50, body_hash="c",
        ))
        # Edges: A→B, B→C
        self.store.create_calls_edge("reach_a", "reach_b", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/test/reach.cpp", call_line=5,
        ))
        self.store.create_calls_edge("reach_b", "reach_c", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/test/reach.cpp", call_line=25,
        ))
        # UC on B (unresolved call from B)
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="reach_b", call_expression="d->call()",
            call_file="/test/reach.cpp", call_line=28, call_type="indirect",
            source_code_snippet="d->call();", var_name="d", var_type="D*",
        ))
        # SourcePoint for A
        self.store.create_source_point(SourcePointNode(
            id="sp_reach_a", entry_point_kind="public_api",
            reason="test", function_id="reach_a", module="test",
            status="running",
        ))
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_reachable_returns_subgraph(self):
        """GET /source-points/{id}/reachable returns nodes + edges + unresolved."""
        r = self.client.get("/api/v1/source-points/sp_reach_a/reachable")
        assert r.status_code == 200
        data = r.json()
        # Should have nodes, edges, unresolved keys
        assert "nodes" in data
        assert "edges" in data
        assert "unresolved" in data

    def test_reachable_includes_transitive_callees(self):
        """BFS reaches A→B→C (transitive)."""
        r = self.client.get("/api/v1/source-points/sp_reach_a/reachable")
        data = r.json()
        node_ids = {n["id"] for n in data["nodes"]}
        # Should include A, B, C
        assert "reach_a" in node_ids
        assert "reach_b" in node_ids
        assert "reach_c" in node_ids

    def test_reachable_includes_unresolved_calls(self):
        """BFS includes UnresolvedCalls from reachable functions."""
        r = self.client.get("/api/v1/source-points/sp_reach_a/reachable")
        data = r.json()
        assert len(data["unresolved"]) >= 1
        uc = data["unresolved"][0]
        assert uc["caller_id"] == "reach_b"

    def test_reachable_not_found(self):
        """GET /source-points/nonexistent/reachable returns 404."""
        r = self.client.get("/api/v1/source-points/nonexistent/reachable")
        assert r.status_code == 404

    def test_reachable_depth_limit(self):
        """GET /source-points/{id}/reachable?depth=1 limits BFS depth."""
        r = self.client.get(
            "/api/v1/source-points/sp_reach_a/reachable", params={"depth": 1}
        )
        assert r.status_code == 200
        data = r.json()
        node_ids = {n["id"] for n in data["nodes"]}
        # Depth 1: A + B only (C is depth 2)
        assert "reach_a" in node_ids
        assert "reach_b" in node_ids


# ===========================================================================
# §3 UnresolvedCall deduplication — (caller_id, call_file, call_line) triple
# ===========================================================================


class TestUnresolvedCallDeduplication:
    """architecture.md §4: UC deduplicates on (caller_id, call_file, call_line)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import UnresolvedCallNode

        self.store = InMemoryGraphStore()
        self.uc1 = UnresolvedCallNode(
            caller_id="dedup_fn_a", call_expression="foo()",
            call_file="/test/dedup.cpp", call_line=10, call_type="indirect",
            source_code_snippet="foo();", var_name="x", var_type="Foo*",
        )

    def test_first_insert(self):
        """First UC insert succeeds."""
        uid = self.store.create_unresolved_call(self.uc1)
        assert uid == self.uc1.id
        ucs = self.store.get_unresolved_calls(caller_id="dedup_fn_a")
        assert len(ucs) == 1

    def test_duplicate_triple_updates_in_place(self):
        """Second UC with same triple updates existing (no duplicate)."""
        from codemap_lite.graph.schema import UnresolvedCallNode

        self.store.create_unresolved_call(self.uc1)
        uc2 = UnresolvedCallNode(
            caller_id="dedup_fn_a", call_expression="bar()",
            call_file="/test/dedup.cpp", call_line=10, call_type="virtual",
            source_code_snippet="bar();", var_name="y", var_type="Bar*",
        )
        uid2 = self.store.create_unresolved_call(uc2)
        # Should reuse same id
        assert uid2 == self.uc1.id
        ucs = self.store.get_unresolved_calls(caller_id="dedup_fn_a")
        assert len(ucs) == 1
        # Content updated
        assert ucs[0].call_expression == "bar()"
        assert ucs[0].call_type == "virtual"

    def test_different_line_creates_new(self):
        """Different call_line creates a separate UC."""
        from codemap_lite.graph.schema import UnresolvedCallNode

        self.store.create_unresolved_call(self.uc1)
        uc2 = UnresolvedCallNode(
            caller_id="dedup_fn_a", call_expression="baz()",
            call_file="/test/dedup.cpp", call_line=20, call_type="indirect",
            source_code_snippet="baz();", var_name="z", var_type="Baz*",
        )
        self.store.create_unresolved_call(uc2)
        ucs = self.store.get_unresolved_calls(caller_id="dedup_fn_a")
        assert len(ucs) == 2

    def test_different_file_creates_new(self):
        """Different call_file creates a separate UC."""
        from codemap_lite.graph.schema import UnresolvedCallNode

        self.store.create_unresolved_call(self.uc1)
        uc2 = UnresolvedCallNode(
            caller_id="dedup_fn_a", call_expression="foo()",
            call_file="/test/other.cpp", call_line=10, call_type="indirect",
            source_code_snippet="foo();", var_name="x", var_type="Foo*",
        )
        self.store.create_unresolved_call(uc2)
        ucs = self.store.get_unresolved_calls(caller_id="dedup_fn_a")
        assert len(ucs) == 2


# ===========================================================================
# §3 CALLS edge 4-tuple uniqueness + idempotency
# ===========================================================================


class TestCallsEdgeUniqueness:
    """architecture.md §4: CALLS edge unique by (caller_id, callee_id, call_file, call_line)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import CallsEdgeProps, FunctionNode

        self.store = InMemoryGraphStore()
        self.store.create_function(FunctionNode(
            id="eu_a", signature="void A()", name="A",
            file_path="/test/eu.cpp", start_line=1, end_line=10, body_hash="a",
        ))
        self.store.create_function(FunctionNode(
            id="eu_b", signature="void B()", name="B",
            file_path="/test/eu.cpp", start_line=20, end_line=30, body_hash="b",
        ))
        self.props = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/test/eu.cpp", call_line=5,
        )

    def test_first_edge_created(self):
        """First edge creation succeeds."""
        self.store.create_calls_edge("eu_a", "eu_b", self.props)
        assert self.store.edge_exists("eu_a", "eu_b", "/test/eu.cpp", 5)

    def test_duplicate_edge_is_idempotent(self):
        """Duplicate edge (same 4-tuple) is silently skipped."""
        from codemap_lite.graph.schema import CallsEdgeProps

        self.store.create_calls_edge("eu_a", "eu_b", self.props)
        # Try again with different resolved_by — should be skipped
        props2 = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/test/eu.cpp", call_line=5,
        )
        self.store.create_calls_edge("eu_a", "eu_b", props2)
        # Only one edge should exist
        edges = [e for e in self.store._calls_edges
                 if e.caller_id == "eu_a" and e.callee_id == "eu_b"]
        assert len(edges) == 1
        # First resolved_by preserved
        assert edges[0].props.resolved_by == "symbol_table"

    def test_different_line_creates_new_edge(self):
        """Different call_line creates a separate edge."""
        from codemap_lite.graph.schema import CallsEdgeProps

        self.store.create_calls_edge("eu_a", "eu_b", self.props)
        props2 = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/test/eu.cpp", call_line=8,
        )
        self.store.create_calls_edge("eu_a", "eu_b", props2)
        edges = [e for e in self.store._calls_edges
                 if e.caller_id == "eu_a" and e.callee_id == "eu_b"]
        assert len(edges) == 2

    def test_different_file_creates_new_edge(self):
        """Different call_file creates a separate edge."""
        from codemap_lite.graph.schema import CallsEdgeProps

        self.store.create_calls_edge("eu_a", "eu_b", self.props)
        props2 = CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/test/other.cpp", call_line=5,
        )
        self.store.create_calls_edge("eu_a", "eu_b", props2)
        edges = [e for e in self.store._calls_edges
                 if e.caller_id == "eu_a" and e.callee_id == "eu_b"]
        assert len(edges) == 2


# ===========================================================================
# §8 GET /api/v1/stats — all required buckets
# ===========================================================================


class TestStatsEndpointBuckets:
    """architecture.md §8: /stats must return all required aggregation buckets."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, SourcePointNode, UnresolvedCallNode,
        )
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        # Add some data for meaningful stats
        self.store.create_function(FunctionNode(
            id="stats_a", signature="void A()", name="A",
            file_path="/test/stats.cpp", start_line=1, end_line=10, body_hash="a",
        ))
        self.store.create_function(FunctionNode(
            id="stats_b", signature="void B()", name="B",
            file_path="/test/stats.cpp", start_line=20, end_line=30, body_hash="b",
        ))
        self.store.create_calls_edge("stats_a", "stats_b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/test/stats.cpp", call_line=5,
        ))
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="stats_a", call_expression="c()",
            call_file="/test/stats.cpp", call_line=8, call_type="indirect",
            source_code_snippet="c();", var_name="c", var_type="C*",
        ))
        self.store.create_source_point(SourcePointNode(
            id="sp_stats_a", entry_point_kind="public_api",
            reason="test", function_id="stats_a", module="test",
            status="running",
        ))
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_stats_has_required_keys(self):
        """GET /stats returns all architecture-required buckets."""
        r = self.client.get("/api/v1/stats")
        assert r.status_code == 200
        data = r.json()
        # Required top-level keys per architecture.md §8
        required_keys = {
            "total_functions", "total_files", "total_calls",
            "total_unresolved", "calls_by_resolved_by", "calls_by_call_type",
        }
        for key in required_keys:
            assert key in data, f"Missing stats key: {key}"

    def test_stats_resolved_by_buckets(self):
        """calls_by_resolved_by has all 5 valid keys."""
        r = self.client.get("/api/v1/stats")
        cbr = r.json()["calls_by_resolved_by"]
        valid_keys = {"symbol_table", "signature", "dataflow", "context", "llm"}
        for key in valid_keys:
            assert key in cbr, f"Missing resolved_by bucket: {key}"
        # Our test data has 1 llm edge
        assert cbr["llm"] == 1

    def test_stats_call_type_buckets(self):
        """calls_by_call_type has all 3 valid keys."""
        r = self.client.get("/api/v1/stats")
        cbt = r.json()["calls_by_call_type"]
        valid_keys = {"direct", "indirect", "virtual"}
        for key in valid_keys:
            assert key in cbt, f"Missing call_type bucket: {key}"
        assert cbt["indirect"] == 1

    def test_stats_counts_correct(self):
        """Stats counts match actual data."""
        r = self.client.get("/api/v1/stats")
        data = r.json()
        assert data["total_functions"] == 2
        assert data["total_calls"] == 1
        assert data["total_unresolved"] == 1


# ===========================================================================
# §7 reset_unresolvable_gaps — bulk reset for retry
# ===========================================================================


class TestResetUnresolvableGaps:
    """architecture.md §7: reset_unresolvable_gaps resets all unresolvable UCs to pending."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import UnresolvedCallNode

        self.store = InMemoryGraphStore()
        # Create 3 UCs: 2 unresolvable, 1 pending
        self.uc1 = UnresolvedCallNode(
            caller_id="reset_a", call_expression="foo()",
            call_file="/test/reset.cpp", call_line=10, call_type="indirect",
            source_code_snippet="foo();", var_name="x", var_type="X*",
            retry_count=3, status="unresolvable",
            last_attempt_reason="gate_failed: no edges",
        )
        self.uc2 = UnresolvedCallNode(
            caller_id="reset_a", call_expression="bar()",
            call_file="/test/reset.cpp", call_line=20, call_type="virtual",
            source_code_snippet="bar();", var_name="y", var_type="Y*",
            retry_count=3, status="unresolvable",
            last_attempt_reason="subprocess_timeout",
        )
        self.uc3 = UnresolvedCallNode(
            caller_id="reset_a", call_expression="baz()",
            call_file="/test/reset.cpp", call_line=30, call_type="direct",
            source_code_snippet="baz();", var_name="z", var_type="Z*",
            retry_count=0, status="pending",
        )
        self.store.create_unresolved_call(self.uc1)
        self.store.create_unresolved_call(self.uc2)
        self.store.create_unresolved_call(self.uc3)

    def test_reset_changes_unresolvable_to_pending(self):
        """All unresolvable UCs become pending with retry_count=0."""
        self.store.reset_unresolvable_gaps()
        ucs = self.store.get_unresolved_calls(caller_id="reset_a")
        for uc in ucs:
            assert uc.status == "pending"
            assert uc.retry_count == 0

    def test_reset_preserves_pending_ucs(self):
        """Already-pending UCs are unchanged."""
        self.store.reset_unresolvable_gaps()
        ucs = self.store.get_unresolved_calls(caller_id="reset_a")
        assert len(ucs) == 3  # All still exist


# ===========================================================================
# §4 Schema enum validation — construction-time rejection
# ===========================================================================


class TestSchemaEnumValidation:
    """architecture.md §4: all enum fields must reject invalid values at construction."""

    def test_invalid_resolved_by_raises(self):
        """CallsEdgeProps rejects invalid resolved_by."""
        from codemap_lite.graph.schema import CallsEdgeProps
        with pytest.raises(ValueError, match="resolved_by"):
            CallsEdgeProps(
                resolved_by="magic", call_type="direct",
                call_file="/test.cpp", call_line=1,
            )

    def test_invalid_call_type_on_edge_raises(self):
        """CallsEdgeProps rejects invalid call_type."""
        from codemap_lite.graph.schema import CallsEdgeProps
        with pytest.raises(ValueError, match="call_type"):
            CallsEdgeProps(
                resolved_by="llm", call_type="unknown",
                call_file="/test.cpp", call_line=1,
            )

    def test_invalid_call_type_on_uc_raises(self):
        """UnresolvedCallNode rejects invalid call_type."""
        from codemap_lite.graph.schema import UnresolvedCallNode
        with pytest.raises(ValueError, match="call_type"):
            UnresolvedCallNode(
                caller_id="x", call_expression="f()",
                call_file="/test.cpp", call_line=1, call_type="unknown",
                source_code_snippet="f();", var_name=None, var_type=None,
            )

    def test_invalid_uc_status_raises(self):
        """UnresolvedCallNode rejects invalid status."""
        from codemap_lite.graph.schema import UnresolvedCallNode
        with pytest.raises(ValueError, match="status"):
            UnresolvedCallNode(
                caller_id="x", call_expression="f()",
                call_file="/test.cpp", call_line=1, call_type="direct",
                source_code_snippet="f();", var_name=None, var_type=None,
                status="invalid",
            )

    def test_invalid_source_point_status_raises(self):
        """SourcePointNode rejects invalid status."""
        from codemap_lite.graph.schema import SourcePointNode
        with pytest.raises(ValueError, match="status"):
            SourcePointNode(
                id="sp_bad", entry_point_kind="api",
                reason="test", function_id="fn_x",
                module="test", status="invalid",
            )

    def test_valid_resolved_by_values(self):
        """All 5 valid resolved_by values are accepted."""
        from codemap_lite.graph.schema import CallsEdgeProps, VALID_RESOLVED_BY
        for rb in VALID_RESOLVED_BY:
            props = CallsEdgeProps(
                resolved_by=rb, call_type="direct",
                call_file="/test.cpp", call_line=1,
            )
            assert props.resolved_by == rb

    def test_valid_call_types(self):
        """All 3 valid call_type values are accepted."""
        from codemap_lite.graph.schema import CallsEdgeProps, VALID_CALL_TYPES
        for ct in VALID_CALL_TYPES:
            props = CallsEdgeProps(
                resolved_by="llm", call_type=ct,
                call_file="/test.cpp", call_line=1,
            )
            assert props.call_type == ct

    def test_valid_uc_statuses(self):
        """All 2 valid UC statuses are accepted."""
        from codemap_lite.graph.schema import UnresolvedCallNode, VALID_UC_STATUSES
        for st in VALID_UC_STATUSES:
            uc = UnresolvedCallNode(
                caller_id="x", call_expression="f()",
                call_file="/test.cpp", call_line=1, call_type="direct",
                source_code_snippet="f();", var_name=None, var_type=None,
                status=st,
            )
            assert uc.status == st

    def test_valid_source_point_statuses(self):
        """All 4 valid SourcePoint statuses are accepted."""
        from codemap_lite.graph.schema import SourcePointNode, VALID_SOURCE_POINT_STATUSES
        for st in VALID_SOURCE_POINT_STATUSES:
            sp = SourcePointNode(
                id=f"sp_{st}", entry_point_kind="api",
                reason="test", function_id="fn_x",
                module="test", status=st,
            )
            assert sp.status == st

    def test_reason_categories_match_architecture(self):
        """VALID_REASON_CATEGORIES matches architecture.md §3 exactly."""
        from codemap_lite.graph.schema import VALID_REASON_CATEGORIES
        expected = {
            "gate_failed", "agent_error", "subprocess_timeout",
            "subprocess_crash", "agent_exited_without_edge",
        }
        assert VALID_REASON_CATEGORIES == expected


# ===========================================================================
# §8 Stats endpoint — extended keys (unresolved_by_status, source_points_by_status)
# ===========================================================================


class TestStatsExtendedKeys:
    """architecture.md §8: /stats returns extended aggregation buckets."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, RepairLogNode,
            SourcePointNode, UnresolvedCallNode,
        )
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        # Functions
        self.store.create_function(FunctionNode(
            id="ext_a", signature="void A()", name="A",
            file_path="/test/ext.cpp", start_line=1, end_line=10, body_hash="a",
        ))
        self.store.create_function(FunctionNode(
            id="ext_b", signature="void B()", name="B",
            file_path="/test/ext.cpp", start_line=20, end_line=30, body_hash="b",
        ))
        # Edge
        self.store.create_calls_edge("ext_a", "ext_b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/test/ext.cpp", call_line=5,
        ))
        # UCs: 1 pending, 1 unresolvable
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="ext_a", call_expression="c()",
            call_file="/test/ext.cpp", call_line=8, call_type="indirect",
            source_code_snippet="c();", var_name="c", var_type="C*",
            status="pending",
        ))
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="ext_a", call_expression="d()",
            call_file="/test/ext.cpp", call_line=9, call_type="virtual",
            source_code_snippet="d();", var_name="d", var_type="D*",
            status="unresolvable", retry_count=3,
            last_attempt_reason="gate_failed: no edges",
        ))
        # SourcePoints: 1 running, 1 complete
        self.store.create_source_point(SourcePointNode(
            id="sp_ext_1", entry_point_kind="public_api",
            reason="test", function_id="ext_a", module="test",
            status="running",
        ))
        self.store.create_source_point(SourcePointNode(
            id="sp_ext_2", entry_point_kind="callback",
            reason="test2", function_id="ext_b", module="test",
            status="complete",
        ))
        # RepairLog
        self.store.create_repair_log(RepairLogNode(
            caller_id="ext_a", callee_id="ext_b",
            call_location="/test/ext.cpp:5",
            repair_method="llm", llm_response="resp",
            timestamp="2026-05-15T00:00:00Z", reasoning_summary="r",
        ))
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_stats_has_unresolved_by_status(self):
        """Stats includes unresolved_by_status with pending/unresolvable keys."""
        data = self.client.get("/api/v1/stats").json()
        assert "unresolved_by_status" in data
        ubs = data["unresolved_by_status"]
        assert "pending" in ubs
        assert "unresolvable" in ubs
        assert ubs["pending"] == 1
        assert ubs["unresolvable"] == 1

    def test_stats_has_unresolved_by_category(self):
        """Stats includes unresolved_by_category with all 5 category keys."""
        data = self.client.get("/api/v1/stats").json()
        assert "unresolved_by_category" in data
        ubc = data["unresolved_by_category"]
        expected_keys = {
            "gate_failed", "agent_error", "subprocess_crash",
            "subprocess_timeout", "agent_exited_without_edge",
        }
        for key in expected_keys:
            assert key in ubc, f"Missing category key: {key}"
        # Our unresolvable UC has "gate_failed: no edges"
        assert ubc["gate_failed"] == 1

    def test_stats_has_source_points_by_status(self):
        """Stats includes source_points_by_status with all 4 status keys."""
        data = self.client.get("/api/v1/stats").json()
        assert "source_points_by_status" in data
        spbs = data["source_points_by_status"]
        expected_keys = {"pending", "running", "complete", "partial_complete"}
        for key in expected_keys:
            assert key in spbs, f"Missing source_point status key: {key}"
        assert spbs["running"] == 1
        assert spbs["complete"] == 1

    def test_stats_has_total_repair_logs(self):
        """Stats includes total_repair_logs count."""
        data = self.client.get("/api/v1/stats").json()
        assert "total_repair_logs" in data
        assert data["total_repair_logs"] == 1

    def test_stats_has_total_llm_edges(self):
        """Stats includes total_llm_edges count."""
        data = self.client.get("/api/v1/stats").json()
        assert "total_llm_edges" in data
        assert data["total_llm_edges"] == 1

    def test_stats_bucket_sums_match_totals(self):
        """Sum of resolved_by buckets == total_calls."""
        data = self.client.get("/api/v1/stats").json()
        bucket_sum = sum(data["calls_by_resolved_by"].values())
        assert bucket_sum == data["total_calls"]

    def test_stats_uc_status_sum_matches_total(self):
        """Sum of unresolved_by_status == total_unresolved."""
        data = self.client.get("/api/v1/stats").json()
        status_sum = sum(data["unresolved_by_status"].values())
        assert status_sum == data["total_unresolved"]


# ===========================================================================
# §7 Incremental cascade — CastEngine real data
# ===========================================================================


CASTENGINE_DIR = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")


class TestIncrementalCascadeCastEngine:
    """architecture.md §7: incremental update cascade with real CastEngine data.

    Tests that modifying a file triggers:
    1. File re-parsed
    2. Old functions from that file deleted + rebuilt
    3. Edges pointing to/from deleted functions removed
    4. UCs regenerated for removed edges
    """

    @pytest.fixture(scope="class")
    def castengine_store(self):
        if not CASTENGINE_DIR.exists():
            pytest.skip("CastEngine directory not available")
        store = InMemoryGraphStore()
        orch = PipelineOrchestrator(store=store, target_dir=CASTENGINE_DIR)
        orch.run_full_analysis()
        return store

    def test_functions_have_file_path(self, castengine_store):
        """All functions have a valid file_path (needed for incremental)."""
        fns = castengine_store.list_functions()
        for fn in fns[:100]:
            assert fn.file_path, f"Function {fn.id} has empty file_path"
            assert fn.file_path.endswith((".cpp", ".h", ".c", ".hpp"))

    def test_functions_grouped_by_file(self, castengine_store):
        """Functions are properly grouped by file (incremental deletes by file)."""
        fns = castengine_store.list_functions()
        by_file: dict[str, int] = defaultdict(int)
        for fn in fns:
            by_file[fn.file_path] += 1
        # CastEngine has 100+ files with functions
        assert len(by_file) >= 100
        # Some files have many functions (headers with overloads)
        max_fns = max(by_file.values())
        assert max_fns >= 10, f"Max functions per file: {max_fns}"

    def test_edges_reference_valid_functions(self, castengine_store):
        """All edges reference functions that exist in the store."""
        fns = {fn.id for fn in castengine_store.list_functions()}
        edges = castengine_store.list_calls_edges()
        dangling_callers = 0
        dangling_callees = 0
        for e in edges:
            if e.caller_id not in fns:
                dangling_callers += 1
            if e.callee_id not in fns:
                dangling_callees += 1
        # No dangling references allowed
        assert dangling_callers == 0, (
            f"{dangling_callers} edges have caller_id not in functions"
        )
        assert dangling_callees == 0, (
            f"{dangling_callees} edges have callee_id not in functions"
        )

    def test_incremental_delete_functions_by_file(self, castengine_store):
        """Simulating incremental: deleting functions from a file removes them."""
        fns = castengine_store.list_functions()
        by_file: dict[str, list] = defaultdict(list)
        for fn in fns:
            by_file[fn.file_path].append(fn)

        # Pick a file with multiple functions
        target_file = None
        for fp, file_fns in by_file.items():
            if 3 <= len(file_fns) <= 20:
                target_file = fp
                break
        if target_file is None:
            pytest.skip("No suitable file found")

        target_fn_ids = {fn.id for fn in by_file[target_file]}
        original_fn_count = len(fns)

        # Delete functions from this file
        for fn_id in target_fn_ids:
            castengine_store.delete_function(fn_id)

        remaining_fns = castengine_store.list_functions()
        assert len(remaining_fns) == original_fn_count - len(target_fn_ids)

        # Verify no function from target file remains
        for fn in remaining_fns:
            assert fn.id not in target_fn_ids

    def test_incremental_cascade_removes_edges(self, castengine_store):
        """Deleting a function cascades to remove its edges."""
        # Use a fresh store for this test
        store = InMemoryGraphStore()
        orch = PipelineOrchestrator(store=store, target_dir=CASTENGINE_DIR)
        orch.run_full_analysis()

        fns = store.list_functions()
        edges = store.list_calls_edges()

        # Find a function that has outgoing edges
        caller_ids = {e.caller_id for e in edges}
        target_fn = None
        for fn in fns:
            if fn.id in caller_ids:
                target_fn = fn
                break
        if target_fn is None:
            pytest.skip("No function with outgoing edges")

        # Count edges from this function
        edges_from_target = [e for e in edges if e.caller_id == target_fn.id]
        assert len(edges_from_target) > 0

        # Delete the function — InMemoryGraphStore now cascades (mirrors Neo4j DETACH DELETE)
        store.delete_function(target_fn.id)

        # Edges from this function should be gone
        remaining_edges = store.list_calls_edges()
        edges_from_deleted = [e for e in remaining_edges if e.caller_id == target_fn.id]
        assert len(edges_from_deleted) == 0, (
            f"{len(edges_from_deleted)} edges still reference deleted function"
        )

    def test_incremental_cascade_removes_ucs(self, castengine_store):
        """Deleting a function cascades to remove its UnresolvedCalls."""
        store = InMemoryGraphStore()
        orch = PipelineOrchestrator(store=store, target_dir=CASTENGINE_DIR)
        orch.run_full_analysis()

        ucs = store.get_unresolved_calls()
        # Find a caller_id that has UCs
        uc_caller_ids = {uc.caller_id for uc in ucs}
        fns = {fn.id: fn for fn in store.list_functions()}

        target_caller = None
        for cid in uc_caller_ids:
            if cid in fns:
                target_caller = cid
                break
        if target_caller is None:
            pytest.skip("No function with UCs found")

        ucs_from_target = [uc for uc in ucs if uc.caller_id == target_caller]
        assert len(ucs_from_target) > 0

        # Delete the function
        store.delete_function(target_caller)

        # UCs from this function should be gone
        remaining_ucs = store.get_unresolved_calls()
        ucs_from_deleted = [uc for uc in remaining_ucs if uc.caller_id == target_caller]
        assert len(ucs_from_deleted) == 0, (
            f"{len(ucs_from_deleted)} UCs still reference deleted function"
        )

"""Tests for the FastAPI REST API layer."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from codemap_lite.analysis.feedback_store import CounterExample, FeedbackStore
from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    RepairLogNode,
    UnresolvedCallNode,
)


def get_test_client() -> tuple[TestClient, InMemoryGraphStore]:
    store = InMemoryGraphStore()
    app = create_app(store=store)
    return TestClient(app), store


class TestHealthCheck:
    def test_health_check(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


class TestFilesEndpoint:
    def test_get_files_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/files")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    def test_get_files_with_data(self) -> None:
        client, store = get_test_client()
        f = FileNode(file_path="src/main.py", hash="abc123", primary_language="python")
        store.create_file(f)
        resp = client.get("/api/v1/files")
        assert resp.status_code == 200
        files = resp.json()["items"]
        assert len(files) == 1
        assert files[0]["file_path"] == "src/main.py"


class TestFunctionsEndpoint:
    def test_get_functions_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/functions")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    def test_get_functions_filtered_by_file(self) -> None:
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="def foo()",
            name="foo",
            file_path="src/a.py",
            start_line=1,
            end_line=3,
            body_hash="h1",
        )
        fn2 = FunctionNode(
            signature="def bar()",
            name="bar",
            file_path="src/b.py",
            start_line=1,
            end_line=5,
            body_hash="h2",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        resp = client.get("/api/v1/functions", params={"file": "src/a.py"})
        assert resp.status_code == 200
        funcs = resp.json()["items"]
        assert len(funcs) == 1
        assert funcs[0]["name"] == "foo"

    def test_create_function_then_get(self) -> None:
        client, store = get_test_client()
        fn = FunctionNode(
            signature="def hello()",
            name="hello",
            file_path="src/main.py",
            start_line=10,
            end_line=15,
            body_hash="xyz",
            id="func-001",
        )
        store.create_function(fn)
        resp = client.get("/api/v1/functions/func-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "hello"
        assert data["id"] == "func-001"

    def test_get_function_not_found(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/functions/nonexistent")
        assert resp.status_code == 404


class TestCallersCalleesEndpoint:
    def _setup_graph(self, store: InMemoryGraphStore) -> None:
        self.fn_a = FunctionNode(
            signature="def a()", name="a", file_path="f.py",
            start_line=1, end_line=3, body_hash="ha", id="a",
        )
        self.fn_b = FunctionNode(
            signature="def b()", name="b", file_path="f.py",
            start_line=5, end_line=8, body_hash="hb", id="b",
        )
        self.fn_c = FunctionNode(
            signature="def c()", name="c", file_path="f.py",
            start_line=10, end_line=12, body_hash="hc", id="c",
        )
        store.create_function(self.fn_a)
        store.create_function(self.fn_b)
        store.create_function(self.fn_c)
        # a -> b -> c
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="f.py", call_line=2,
        ))
        store.create_calls_edge("b", "c", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct", call_file="f.py", call_line=6,
        ))

    def test_get_callers(self) -> None:
        client, store = get_test_client()
        self._setup_graph(store)
        resp = client.get("/api/v1/functions/b/callers")
        assert resp.status_code == 200
        callers = resp.json()["items"]
        assert len(callers) == 1
        assert callers[0]["id"] == "a"

    def test_get_callees(self) -> None:
        client, store = get_test_client()
        self._setup_graph(store)
        resp = client.get("/api/v1/functions/b/callees")
        assert resp.status_code == 200
        callees = resp.json()["items"]
        assert len(callees) == 1
        assert callees[0]["id"] == "c"

    def test_get_call_chain(self) -> None:
        client, store = get_test_client()
        self._setup_graph(store)
        resp = client.get("/api/v1/functions/a/call-chain", params={"depth": 5})
        assert resp.status_code == 200
        data = resp.json()
        node_ids = [n["id"] for n in data["nodes"]]
        assert "a" in node_ids
        assert "b" in node_ids
        assert "c" in node_ids

    def test_get_call_chain_depth_limited(self) -> None:
        client, store = get_test_client()
        self._setup_graph(store)
        resp = client.get("/api/v1/functions/a/call-chain", params={"depth": 1})
        assert resp.status_code == 200
        data = resp.json()
        node_ids = [n["id"] for n in data["nodes"]]
        assert "a" in node_ids
        assert "b" in node_ids
        # c should NOT be reachable at depth=1
        assert "c" not in node_ids

    def test_get_callers_nonexistent_function_returns_404(self) -> None:
        """architecture.md §8: callers endpoint must return 404 for
        non-existent function, not 200 with empty list."""
        client, _ = get_test_client()
        resp = client.get("/api/v1/functions/nonexistent/callers")
        assert resp.status_code == 404

    def test_get_callees_nonexistent_function_returns_404(self) -> None:
        """architecture.md §8: callees endpoint must return 404 for
        non-existent function."""
        client, _ = get_test_client()
        resp = client.get("/api/v1/functions/nonexistent/callees")
        assert resp.status_code == 404

    def test_get_call_chain_nonexistent_function_returns_404(self) -> None:
        """architecture.md §8: call-chain endpoint must return 404 for
        non-existent function."""
        client, _ = get_test_client()
        resp = client.get("/api/v1/functions/nonexistent/call-chain")
        assert resp.status_code == 404


class TestAnalyzeEndpoint:
    def test_analyze_trigger(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"

    def test_analyze_trigger_incremental(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze", json={"mode": "incremental"})
        assert resp.status_code == 202

    def test_analyze_trigger_conflict_returns_409(self) -> None:
        """architecture.md §8: double-spawn of analysis must return 409."""
        client, _ = get_test_client()
        # First POST sets state to "running"
        resp1 = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp1.status_code == 202
        # Second POST should detect conflict via real state transition
        resp2 = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp2.status_code == 409
        assert "already running" in resp2.json()["detail"]

    def test_analyze_trigger_spawns_background_task(self) -> None:
        """architecture.md §8: POST /analyze with settings triggers pipeline."""
        from unittest.mock import patch, MagicMock
        from codemap_lite.config.settings import Settings

        store = InMemoryGraphStore()
        settings = Settings()
        app = create_app(store=store, settings=settings)
        client = TestClient(app)

        with patch(
            "codemap_lite.api.routes.analyze._run_analysis_background"
        ) as mock_run:
            resp = client.post("/api/v1/analyze", json={"mode": "full"})
            assert resp.status_code == 202
            mock_run.assert_called_once()

    def test_analyze_trigger_invalid_mode(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze", json={"mode": "invalid"})
        assert resp.status_code == 422

    def test_analyze_trigger_missing_mode(self) -> None:
        """architecture.md §8: POST /analyze requires 'mode' field."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze", json={})
        assert resp.status_code == 422

    def test_analyze_repair(self) -> None:
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze/repair")
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"

    def test_analyze_repair_conflict_returns_409(self) -> None:
        """architecture.md §8: double-spawn must return 409 Conflict."""
        client, store = get_test_client()
        # First call sets state to "repairing" (no settings → demo mode)
        resp1 = client.post("/api/v1/analyze/repair")
        assert resp1.status_code == 202
        # Second call should detect conflict via real state transition
        resp2 = client.post("/api/v1/analyze/repair")
        assert resp2.status_code == 409
        assert "already running" in resp2.json()["detail"]

    def test_analyze_repair_accepts_source_ids_filter(self) -> None:
        """architecture.md §3: repair can target specific source points."""
        client, _ = get_test_client()
        resp = client.post(
            "/api/v1/analyze/repair",
            json={"source_ids": ["src1", "src2"]},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"

    def test_analyze_status(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data
        assert "progress" in data
        # sources[] is always present (empty when no target_dir / no
        # progress files yet) — architecture.md §3, ADR #52.
        assert data["sources"] == []

    def test_analyze_status_includes_timestamps_after_repair(self) -> None:
        """architecture.md §8: status should expose started_at/completed_at."""
        from codemap_lite.config.settings import Settings
        client, _ = get_test_client()
        client.app.state.settings = Settings()
        # Simulate a completed repair session
        client.app.state.analyze_state = {
            "state": "idle",
            "progress": 0.0,
            "started_at": "2026-05-13T10:00:00+00:00",
            "completed_at": "2026-05-13T10:05:00+00:00",
        }
        resp = client.get("/api/v1/analyze/status")
        data = resp.json()
        assert data["started_at"] == "2026-05-13T10:00:00+00:00"
        assert data["completed_at"] == "2026-05-13T10:05:00+00:00"

    def test_analyze_status_aggregates_progress_files(self, tmp_path) -> None:
        store = InMemoryGraphStore()
        app = create_app(store=store, target_dir=tmp_path)
        client = TestClient(app)

        repair_root = tmp_path / "logs" / "repair"
        (repair_root / "src_001").mkdir(parents=True)
        (repair_root / "src_001" / "progress.json").write_text(
            json.dumps({"gaps_fixed": 2, "gaps_total": 5, "current_gap": "gap_003"}),
            encoding="utf-8",
        )
        (repair_root / "src_002").mkdir(parents=True)
        (repair_root / "src_002" / "progress.json").write_text(
            json.dumps({"gaps_fixed": 3, "gaps_total": 3, "current_gap": None}),
            encoding="utf-8",
        )

        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        data = resp.json()
        sources = {s["source_id"]: s for s in data["sources"]}
        assert set(sources.keys()) == {"src_001", "src_002"}
        assert sources["src_001"]["gaps_fixed"] == 2
        assert sources["src_001"]["gaps_total"] == 5
        assert sources["src_001"]["current_gap"] == "gap_003"
        assert sources["src_002"]["gaps_fixed"] == 3
        assert sources["src_002"]["current_gap"] is None
        # Overall progress is (2+3) / (5+3) = 0.625
        assert data["progress"] == pytest.approx(0.625)

    def test_analyze_status_ignores_unreadable_progress(self, tmp_path) -> None:
        store = InMemoryGraphStore()
        app = create_app(store=store, target_dir=tmp_path)
        client = TestClient(app)

        repair_root = tmp_path / "logs" / "repair"
        (repair_root / "src_bad").mkdir(parents=True)
        (repair_root / "src_bad" / "progress.json").write_text(
            "not json {{", encoding="utf-8"
        )
        (repair_root / "src_ok").mkdir(parents=True)
        (repair_root / "src_ok" / "progress.json").write_text(
            json.dumps({"gaps_fixed": 1, "gaps_total": 2, "current_gap": "g"}),
            encoding="utf-8",
        )

        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        sources = {s["source_id"]: s for s in resp.json()["sources"]}
        assert "src_bad" not in sources

    def test_analyze_status_skips_malformed_numeric_fields(self, tmp_path) -> None:
        """architecture.md §3: progress.json with non-numeric gaps_fixed/
        gaps_total must be skipped gracefully, not crash the endpoint."""
        store = InMemoryGraphStore()
        app = create_app(store=store, target_dir=tmp_path)
        client = TestClient(app)

        repair_root = tmp_path / "logs" / "repair"
        (repair_root / "src_bad").mkdir(parents=True)
        (repair_root / "src_bad" / "progress.json").write_text(
            json.dumps({"gaps_fixed": "not_a_number", "gaps_total": 5}),
            encoding="utf-8",
        )
        (repair_root / "src_ok").mkdir(parents=True)
        (repair_root / "src_ok" / "progress.json").write_text(
            json.dumps({"gaps_fixed": 1, "gaps_total": 2}),
            encoding="utf-8",
        )

        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        sources = {s["source_id"]: s for s in resp.json()["sources"]}
        # Malformed entry skipped, valid entry preserved
        assert "src_bad" not in sources
        assert "src_ok" in sources
        assert sources["src_ok"]["gaps_fixed"] == 1
        assert sources["src_ok"]["gaps_total"] == 2

    def test_analyze_status_extended_progress_fields(self, tmp_path) -> None:
        """architecture.md §3 + ADR #52: progress.json extended fields
        (state, attempt, max_attempts, gate_result, edges_written, last_error)
        must be passed through to the /analyze/status response."""
        store = InMemoryGraphStore()
        app = create_app(store=store, target_dir=tmp_path)
        client = TestClient(app)

        repair_root = tmp_path / "logs" / "repair"
        (repair_root / "src_001").mkdir(parents=True)
        (repair_root / "src_001" / "progress.json").write_text(
            json.dumps({
                "gaps_fixed": 2,
                "gaps_total": 5,
                "current_gap": "gap_003",
                "state": "running",
                "attempt": 2,
                "max_attempts": 3,
                "gate_result": "failed",
                "edges_written": 4,
                "last_error": "gate_failed: 3 pending GAPs remain",
            }),
            encoding="utf-8",
        )

        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        sources = {s["source_id"]: s for s in resp.json()["sources"]}
        s = sources["src_001"]
        assert s["state"] == "running"
        assert s["attempt"] == 2
        assert s["max_attempts"] == 3
        assert s["gate_result"] == "failed"
        assert s["edges_written"] == 4
        assert s["last_error"] == "gate_failed: 3 pending GAPs remain"

    def test_analyze_background_incremental_exposes_affected_source_ids(self) -> None:
        """architecture.md §7 step 5: after incremental analysis, the analyze
        state must expose affected_source_ids so the frontend/CLI knows which
        sources need re-repair."""
        from unittest.mock import patch, MagicMock
        from codemap_lite.api.routes.analyze import _run_analysis_background

        store = InMemoryGraphStore()
        app = create_app(store=store)

        # Mock PipelineOrchestrator to return affected_source_ids
        mock_result = MagicMock()
        mock_result.files_scanned = 3
        mock_result.functions_found = 10
        mock_result.direct_calls = 5
        mock_result.unresolved_calls = 2
        mock_result.success = True
        mock_result.affected_source_ids = ["src_001", "src_002"]

        mock_orch = MagicMock()
        mock_orch.run_incremental_analysis.return_value = mock_result

        mock_settings = MagicMock()
        mock_settings.project.target_dir = "/tmp/test"

        with patch(
            "codemap_lite.pipeline.orchestrator.PipelineOrchestrator",
            return_value=mock_orch,
        ):
            _run_analysis_background(app, mock_settings, "incremental")

        state = app.state.analyze_state
        assert "affected_source_ids" in state.get("result", {}), (
            "architecture.md §7 step 5: incremental analysis result must "
            "expose affected_source_ids for re-repair trigger"
        )
        assert state["result"]["affected_source_ids"] == ["src_001", "src_002"]


class TestSourcePointsEndpoint:
    def test_get_source_points_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/source-points")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    def test_get_source_point_reachable(self) -> None:
        client, store = get_test_client()
        fn = FunctionNode(
            signature="def entry()", name="entry", file_path="main.py",
            start_line=1, end_line=5, body_hash="h1", id="entry-1",
        )
        store.create_function(fn)
        resp = client.get("/api/v1/source-points/entry-1/reachable")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data

    def test_get_source_point_reachable_full_schema(self) -> None:
        """architecture.md §8: GET /source-points/{id}/reachable must return
        {nodes: [...], edges: [...], unresolved: [...]} with proper field shapes."""
        client, store = get_test_client()
        caller = FunctionNode(
            signature="void caller()", name="caller", file_path="a.cpp",
            start_line=1, end_line=10, body_hash="hc", id="caller-1",
        )
        callee = FunctionNode(
            signature="void callee()", name="callee", file_path="b.cpp",
            start_line=1, end_line=5, body_hash="hd", id="callee-1",
        )
        store.create_function(caller)
        store.create_function(callee)
        store.create_calls_edge(
            "caller-1", "callee-1",
            CallsEdgeProps(
                resolved_by="symbol_table", call_type="direct",
                call_file="a.cpp", call_line=5,
            ),
        )
        gap = UnresolvedCallNode(
            caller_id="caller-1", call_expression="fp()",
            call_file="a.cpp", call_line=8, call_type="indirect",
            source_code_snippet="fp();", var_name="fp", var_type="void(*)()",
        )
        store.create_unresolved_call(gap)

        resp = client.get("/api/v1/source-points/caller-1/reachable")
        assert resp.status_code == 200
        data = resp.json()

        # Must have all three top-level keys
        assert set(data.keys()) >= {"nodes", "edges", "unresolved"}

        # Nodes must have required fields
        assert len(data["nodes"]) >= 1
        node = data["nodes"][0]
        for field in ("id", "name", "signature", "file_path"):
            assert field in node, f"node missing field: {field}"

        # Edges must have caller/callee/props
        assert len(data["edges"]) >= 1
        edge = data["edges"][0]
        assert "caller_id" in edge or "source" in edge  # accept either naming

        # Unresolved must have caller_id and call_expression
        assert len(data["unresolved"]) >= 1
        gap_data = data["unresolved"][0]
        assert "caller_id" in gap_data
        assert "call_expression" in gap_data

    def test_source_points_filter_by_module(self) -> None:
        """architecture.md §5: source points can be filtered by module."""
        client, _ = get_test_client()
        client.app.state.source_points = [
            {"id": "sp1", "entry_point_kind": "callback_registration", "module": "audio::mixer"},
            {"id": "sp2", "entry_point_kind": "entry_point", "module": "video::decoder"},
            {"id": "sp3", "entry_point_kind": "callback_registration", "module": "audio::output"},
        ]
        # Filter by module substring
        resp = client.get("/api/v1/source-points?module=audio")
        assert resp.status_code == 200
        data = resp.json()["items"]
        assert len(data) == 2
        ids = {e["id"] for e in data}
        assert ids == {"sp1", "sp3"}

    def test_source_points_filter_by_kind(self) -> None:
        """architecture.md §8: source points can be filtered by kind."""
        client, _ = get_test_client()
        client.app.state.source_points = [
            {"id": "sp1", "entry_point_kind": "callback_registration", "module": "m1"},
            {"id": "sp2", "entry_point_kind": "entry_point", "module": "m2"},
        ]
        resp = client.get("/api/v1/source-points?kind=entry_point")
        assert resp.status_code == 200
        data = resp.json()["items"]
        assert len(data) == 1
        assert data[0]["id"] == "sp2"

    def test_source_points_summary(self) -> None:
        """architecture.md §8: GET /source-points/summary returns total and by_kind."""
        client, _ = get_test_client()
        client.app.state.source_points = [
            {"id": "sp1", "entry_point_kind": "callback_registration", "module": "m1"},
            {"id": "sp2", "entry_point_kind": "entry_point", "module": "m2"},
            {"id": "sp3", "entry_point_kind": "callback_registration", "module": "m3"},
        ]
        resp = client.get("/api/v1/source-points/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["by_kind"]["callback_registration"] == 2
        assert data["by_kind"]["entry_point"] == 1

    def test_source_points_summary_empty(self) -> None:
        """Summary with no source points returns total=0 and empty by_kind."""
        client, _ = get_test_client()
        resp = client.get("/api/v1/source-points/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["by_kind"] == {}

    def test_source_points_status_enrichment(self) -> None:
        """architecture.md §4: source points expose status from graph store."""
        from codemap_lite.graph.schema import SourcePointNode

        client, store = get_test_client()
        # Create source point in graph store with status
        sp = SourcePointNode(
            id="sp1", entry_point_kind="callback_registration",
            reason="test", function_id="func-1", module="m1", status="complete",
        )
        store.create_source_point(sp)
        # Set raw codewiki_lite entries
        client.app.state.source_points = [
            {"id": "sp1", "entry_point_kind": "callback_registration",
             "module": "m1", "function_id": "func-1"},
            {"id": "sp2", "entry_point_kind": "entry_point",
             "module": "m2", "function_id": "func-2"},
        ]
        resp = client.get("/api/v1/source-points")
        assert resp.status_code == 200
        items = resp.json()["items"]
        # sp1 should have status from graph store
        sp1 = next(i for i in items if i["id"] == "sp1")
        assert sp1["status"] == "complete"
        # sp2 has no graph store entry, defaults to "pending"
        sp2 = next(i for i in items if i["id"] == "sp2")
        assert sp2["status"] == "pending"

    def test_source_points_filter_by_status(self) -> None:
        """architecture.md §8: source points can be filtered by status."""
        from codemap_lite.graph.schema import SourcePointNode

        client, store = get_test_client()
        store.create_source_point(SourcePointNode(
            id="sp1", entry_point_kind="cb", reason="", function_id="f1",
            module="", status="complete",
        ))
        store.create_source_point(SourcePointNode(
            id="sp2", entry_point_kind="cb", reason="", function_id="f2",
            module="", status="pending",
        ))
        client.app.state.source_points = [
            {"id": "sp1", "function_id": "f1", "entry_point_kind": "cb", "module": ""},
            {"id": "sp2", "function_id": "f2", "entry_point_kind": "cb", "module": ""},
        ]
        resp = client.get("/api/v1/source-points?status=complete")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["function_id"] == "f1"

    def test_source_points_summary_includes_by_status(self) -> None:
        """architecture.md §8: summary includes by_status breakdown."""
        from codemap_lite.graph.schema import SourcePointNode

        client, store = get_test_client()
        store.create_source_point(SourcePointNode(
            id="sp1", entry_point_kind="cb", reason="", function_id="f1",
            module="", status="complete",
        ))
        client.app.state.source_points = [
            {"id": "sp1", "function_id": "f1", "entry_point_kind": "cb", "module": ""},
            {"id": "sp2", "function_id": "f2", "entry_point_kind": "ep", "module": ""},
        ]
        resp = client.get("/api/v1/source-points/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "by_status" in data
        assert data["by_status"]["complete"] == 1
        assert data["by_status"]["pending"] == 1

    def test_get_source_point_by_id(self) -> None:
        """architecture.md §8: GET /source-points/{id} returns individual SP."""
        from codemap_lite.graph.schema import SourcePointNode

        client, store = get_test_client()
        store.create_source_point(SourcePointNode(
            id="sp1", entry_point_kind="cb", reason="test reason",
            function_id="f1", module="audio", status="running",
        ))
        resp = client.get("/api/v1/source-points/sp1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "sp1"
        assert data["status"] == "running"
        assert data["entry_point_kind"] == "cb"

    def test_get_source_point_not_found(self) -> None:
        """GET /source-points/{id} returns 404 for unknown id."""
        client, _ = get_test_client()
        resp = client.get("/api/v1/source-points/nonexistent")
        assert resp.status_code == 404

    def test_stats_includes_source_points_by_status(self) -> None:
        """architecture.md §8: stats includes source_points_by_status."""
        from codemap_lite.graph.schema import SourcePointNode

        client, store = get_test_client()
        store.create_source_point(SourcePointNode(
            id="sp1", entry_point_kind="cb", reason="",
            function_id="f1", module="", status="complete",
        ))
        store.create_source_point(SourcePointNode(
            id="sp2", entry_point_kind="cb", reason="",
            function_id="f2", module="", status="pending",
        ))
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "source_points_by_status" in data
        assert data["source_points_by_status"]["complete"] == 1
        assert data["source_points_by_status"]["pending"] == 1
        assert data["source_points_by_status"]["running"] == 0
    def _setup_edge(self, store: InMemoryGraphStore) -> None:
        fn1 = FunctionNode(
            signature="void f()", name="f", file_path="a.c",
            start_line=1, end_line=5, body_hash="h1", id="func-1",
        )
        fn2 = FunctionNode(
            signature="void g()", name="g", file_path="a.c",
            start_line=10, end_line=15, body_hash="h2", id="func-2",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge(
            "func-1", "func-2",
            CallsEdgeProps(
                resolved_by="llm", call_type="indirect",
                call_file="a.c", call_line=3,
            ),
        )

    def test_post_review(self) -> None:
        client, store = get_test_client()
        self._setup_edge(store)
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "func-1",
            "callee_id": "func-2",
            "call_file": "a.c",
            "call_line": 3,
            "verdict": "correct",
            "comment": "Looks correct",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["caller_id"] == "func-1"
        assert data["verdict"] == "correct"
        assert "id" in data

    def test_get_reviews(self) -> None:
        client, store = get_test_client()
        self._setup_edge(store)
        client.post("/api/v1/reviews", json={
            "caller_id": "func-1",
            "callee_id": "func-2",
            "call_file": "a.c",
            "call_line": 3,
            "verdict": "correct",
        })
        resp = client.get("/api/v1/reviews")
        assert resp.status_code == 200
        reviews = resp.json()["items"]
        assert len(reviews) == 1

    def test_update_review(self) -> None:
        client, store = get_test_client()
        self._setup_edge(store)
        create_resp = client.post("/api/v1/reviews", json={
            "caller_id": "func-1",
            "callee_id": "func-2",
            "call_file": "a.c",
            "call_line": 3,
            "verdict": "correct",
            "comment": "Initial",
        })
        review_id = create_resp.json()["id"]
        resp = client.put(f"/api/v1/reviews/{review_id}", json={
            "comment": "Updated",
            "status": "approved",
        })
        assert resp.status_code == 200
        assert resp.json()["comment"] == "Updated"

    def test_delete_review(self) -> None:
        client, store = get_test_client()
        self._setup_edge(store)
        create_resp = client.post("/api/v1/reviews", json={
            "caller_id": "func-1",
            "callee_id": "func-2",
            "call_file": "a.c",
            "call_line": 3,
            "verdict": "correct",
            "comment": "To delete",
        })
        review_id = create_resp.json()["id"]
        resp = client.delete(f"/api/v1/reviews/{review_id}")
        assert resp.status_code == 204

    def test_delete_review_not_found_returns_404(self) -> None:
        """architecture.md §8: DELETE /reviews/{id} must return 404 for
        non-existent review."""
        client, _ = get_test_client()
        resp = client.delete("/api/v1/reviews/nonexistent-id-999")
        assert resp.status_code == 404

    def test_post_edge(self) -> None:
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="def x()", name="x", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="x",
        )
        fn2 = FunctionNode(
            signature="def y()", name="y", file_path="f.py",
            start_line=5, end_line=8, body_hash="h2", id="y",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        resp = client.post("/api/v1/edges", json={
            "caller_id": "x",
            "callee_id": "y",
            "resolved_by": "llm",
            "call_type": "direct",
            "call_file": "f.py",
            "call_line": 2,
        })
        assert resp.status_code == 201

    def test_post_edge_nonexistent_caller_returns_404(self) -> None:
        """architecture.md §8: edges must reference valid Function nodes."""
        client, store = get_test_client()
        fn2 = FunctionNode(
            signature="def y()", name="y", file_path="f.py",
            start_line=5, end_line=8, body_hash="h2", id="y",
        )
        store.create_function(fn2)
        resp = client.post("/api/v1/edges", json={
            "caller_id": "nonexistent",
            "callee_id": "y",
            "resolved_by": "llm",
            "call_type": "direct",
            "call_file": "f.py",
            "call_line": 2,
        })
        assert resp.status_code == 404

    def test_post_edge_nonexistent_callee_returns_404(self) -> None:
        """architecture.md §8: edges must reference valid Function nodes."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="def x()", name="x", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="x",
        )
        store.create_function(fn1)
        resp = client.post("/api/v1/edges", json={
            "caller_id": "x",
            "callee_id": "nonexistent",
            "resolved_by": "llm",
            "call_type": "direct",
            "call_file": "f.py",
            "call_line": 2,
        })
        assert resp.status_code == 404
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="def x()", name="x", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="x",
        )
        store.create_function(fn1)
        resp = client.delete("/api/v1/edges/x")
        assert resp.status_code == 204

    def test_post_edge_invalid_resolved_by_returns_422(self) -> None:
        """architecture.md §8: resolved_by must be one of the allowed enum values."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/edges", json={
            "caller_id": "x",
            "callee_id": "y",
            "resolved_by": "magic",
            "call_type": "direct",
            "call_file": "f.py",
            "call_line": 2,
        })
        assert resp.status_code == 422

    def test_post_edge_invalid_call_type_returns_422(self) -> None:
        """architecture.md §8: call_type must be one of {direct, indirect, virtual}."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/edges", json={
            "caller_id": "x",
            "callee_id": "y",
            "resolved_by": "llm",
            "call_type": "unknown",
            "call_file": "f.py",
            "call_line": 2,
        })
        assert resp.status_code == 422


class TestEdgeCentricReview:
    """architecture.md §5 审阅交互 + §8 REST API:
    POST /api/v1/reviews must be edge-centric — accept caller_id, callee_id,
    call_file, call_line, and verdict (correct/incorrect). When verdict=correct,
    record approval. When verdict=incorrect, trigger the 4-step error flow."""

    def _setup_edge(self, store: InMemoryGraphStore) -> None:
        """Create two functions and an LLM-resolved edge between them."""
        fn1 = FunctionNode(
            signature="void dispatch()", name="dispatch", file_path="src/main.c",
            start_line=10, end_line=20, body_hash="h1", id="fn_dispatch",
        )
        fn2 = FunctionNode(
            signature="void handler()", name="handler", file_path="src/handler.c",
            start_line=1, end_line=5, body_hash="h2", id="fn_handler",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge(
            "fn_dispatch", "fn_handler",
            CallsEdgeProps(
                resolved_by="llm", call_type="indirect",
                call_file="src/main.c", call_line=15,
            ),
        )

    def test_review_mark_correct_records_approval(self) -> None:
        """architecture.md §5: 标记正确 → record approval on the edge."""
        client, store = get_test_client()
        self._setup_edge(store)

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.c",
            "call_line": 15,
            "verdict": "correct",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["verdict"] == "correct"
        assert data["caller_id"] == "fn_dispatch"
        assert data["callee_id"] == "fn_handler"
        assert "id" in data

    def test_review_mark_incorrect_triggers_error_flow(self) -> None:
        """architecture.md §5: 标记错误 → delete edge + RepairLog + regenerate UC."""
        client, store = get_test_client()
        self._setup_edge(store)

        # Also create a RepairLog for this edge
        from codemap_lite.graph.schema import RepairLogNode
        store.create_repair_log(RepairLogNode(
            caller_id="fn_dispatch",
            callee_id="fn_handler",
            call_location="src/main.c:15",
            repair_method="llm",
            llm_response="analysis",
            timestamp="2026-05-14T00:00:00Z",
            reasoning_summary="dispatch calls handler",
        ))

        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.c",
            "call_line": 15,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["verdict"] == "incorrect"

        # Edge should be deleted
        edges = store.list_calls_edges()
        assert len(edges) == 0

        # RepairLog should be deleted
        logs = store.get_repair_logs(
            caller_id="fn_dispatch", callee_id="fn_handler"
        )
        assert len(logs) == 0

        # UnresolvedCall should be regenerated
        ucs = store.get_unresolved_calls(status="pending")
        assert len(ucs) == 1
        assert ucs[0].caller_id == "fn_dispatch"
        assert ucs[0].call_file == "src/main.c"
        assert ucs[0].call_line == 15
        # architecture.md §5: regenerated UC must have retry_count=0
        # so the repair agent treats it as a fresh GAP.
        assert ucs[0].retry_count == 0
        assert ucs[0].status == "pending"

    def test_review_nonexistent_edge_returns_404(self) -> None:
        """Cannot review an edge that doesn't exist."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "no_such",
            "callee_id": "no_such",
            "call_file": "x.c",
            "call_line": 1,
            "verdict": "correct",
        })
        assert resp.status_code == 404

    def test_review_invalid_verdict_returns_422(self) -> None:
        """Verdict must be 'correct' or 'incorrect'."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "fn_dispatch",
            "callee_id": "fn_handler",
            "call_file": "src/main.c",
            "call_line": 15,
            "verdict": "maybe",
        })
        assert resp.status_code == 422

    def test_review_incorrect_with_correct_target_creates_counter_example(self) -> None:
        """architecture.md §5: when verdict=incorrect AND correct_target is
        provided, the review endpoint must create a counter-example in the
        FeedbackStore. This is the primary mechanism for building the
        counter-example library from reviewer feedback."""
        from codemap_lite.analysis.feedback_store import FeedbackStore
        import tempfile

        store = InMemoryGraphStore()
        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path
            feedback_store = FeedbackStore(storage_dir=Path(tmpdir))
            app = create_app(store=store, feedback_store=feedback_store)
            client = TestClient(app)

            # Setup edge
            fn1 = FunctionNode(
                signature="void dispatch()", name="dispatch",
                file_path="src/main.c", start_line=10, end_line=20,
                body_hash="h1", id="fn_dispatch",
            )
            fn2 = FunctionNode(
                signature="void handler()", name="handler",
                file_path="src/handler.c", start_line=1, end_line=5,
                body_hash="h2", id="fn_handler",
            )
            store.create_function(fn1)
            store.create_function(fn2)
            store.create_calls_edge(
                "fn_dispatch", "fn_handler",
                CallsEdgeProps(
                    resolved_by="llm", call_type="indirect",
                    call_file="src/main.c", call_line=15,
                ),
            )

            # Mark incorrect WITH correct_target
            resp = client.post("/api/v1/reviews", json={
                "caller_id": "fn_dispatch",
                "callee_id": "fn_handler",
                "call_file": "src/main.c",
                "call_line": 15,
                "verdict": "incorrect",
                "correct_target": "fn_real_handler",
            })
            assert resp.status_code == 201

            # Counter-example should have been created
            examples = feedback_store.list_all()
            assert len(examples) == 1, (
                "architecture.md §5: correct_target provided → counter-example must be created"
            )
            assert examples[0].wrong_target == "fn_handler"
            assert examples[0].correct_target == "fn_real_handler"


class TestFeedbackEndpoint:
    def test_get_feedback_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/feedback")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    def test_get_feedback_with_store(self, tmp_path) -> None:
        # Seed a FeedbackStore on disk, then wire it into create_app so
        # GET /api/v1/feedback surfaces the structured entries
        # (architecture.md §3 反馈机制 + §8).
        store_dir = tmp_path / ".codemap_lite" / "feedback"
        feedback_store = FeedbackStore(storage_dir=store_dir)
        feedback_store.add(
            CounterExample(
                call_context="dispatch_event(handler, evt)",
                wrong_target="logger.warn",
                correct_target="on_event",
                pattern="dispatch_event callbacks must match signature EventHandler",
            )
        )
        feedback_store.add(
            CounterExample(
                call_context="table[idx](ctx)",
                wrong_target="fallback_noop",
                correct_target="action_commit",
                pattern="vtable index resolution must honour ctx.role",
            )
        )

        graph_store = InMemoryGraphStore()
        app = create_app(store=graph_store, feedback_store=feedback_store)
        client = TestClient(app)

        resp = client.get("/api/v1/feedback")
        assert resp.status_code == 200
        data = resp.json()["items"]
        assert len(data) == 2
        patterns = {item["pattern"] for item in data}
        assert "dispatch_event callbacks must match signature EventHandler" in patterns
        assert "vtable index resolution must honour ctx.role" in patterns
        first = next(
            item for item in data
            if item["pattern"] == "dispatch_event callbacks must match signature EventHandler"
        )
        assert first["call_context"] == "dispatch_event(handler, evt)"
        assert first["wrong_target"] == "logger.warn"
        assert first["correct_target"] == "on_event"

    def test_post_feedback_persists_to_store(self, tmp_path) -> None:
        """POST /api/v1/feedback routes the CounterExample into FeedbackStore.

        Closes the write half of the feedback loop (architecture.md §5
        审阅交互): after a human marks a repair wrong and fills the correct
        target, the generalized reason lands in the store and the next
        repair round picks it up via ``RepairOrchestrator``.
        """
        store_dir = tmp_path / ".codemap_lite" / "feedback"
        feedback_store = FeedbackStore(storage_dir=store_dir)

        app = create_app(store=InMemoryGraphStore(), feedback_store=feedback_store)
        client = TestClient(app)

        payload = {
            "call_context": "dispatcher->handle(req)",
            "wrong_target": "legacy_handler",
            "correct_target": "modern_handler",
            "pattern": "dispatcher vtable resolution must prefer modern_handler",
        }
        resp = client.post("/api/v1/feedback", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        # Response echoes the example plus the dedup signal fields
        # (architecture.md §3 反馈机制 steps 3-5).
        for key, value in payload.items():
            assert data[key] == value
        assert data["deduplicated"] is False
        assert data["total"] == 1

        # Round-trips through GET and through the underlying store
        stored = feedback_store.list_all()
        assert len(stored) == 1
        assert stored[0].pattern == payload["pattern"]

        listing = client.get("/api/v1/feedback").json()["items"]
        assert len(listing) == 1
        assert listing[0]["correct_target"] == "modern_handler"

    def test_post_feedback_dedupes_by_pattern(self, tmp_path) -> None:
        """Posting the same pattern twice does not duplicate entries.

        FeedbackStore.add() merges by pattern (architecture.md §3 反馈机制
        step 4 "相似 → 总结合并"); the HTTP layer inherits that contract
        and surfaces it via ``deduplicated: true`` on the second response
        so the reviewer knows their submission broadened an existing rule.
        """
        feedback_store = FeedbackStore(
            storage_dir=tmp_path / ".codemap_lite" / "feedback"
        )
        app = create_app(store=InMemoryGraphStore(), feedback_store=feedback_store)
        client = TestClient(app)

        payload = {
            "call_context": "cb(x)",
            "wrong_target": "wrong_cb",
            "correct_target": "right_cb",
            "pattern": "callback must be selected by x.role",
        }
        first = client.post("/api/v1/feedback", json=payload)
        assert first.status_code == 201
        assert first.json()["deduplicated"] is False
        assert first.json()["total"] == 1

        second = client.post("/api/v1/feedback", json=payload)
        assert second.status_code == 201
        assert second.json()["deduplicated"] is True
        assert second.json()["total"] == 1

        assert client.get("/api/v1/feedback").json()["total"] == 1

    def test_post_feedback_requires_all_fields(self, tmp_path) -> None:
        """Missing a required field → 422 (Pydantic validation)."""
        feedback_store = FeedbackStore(
            storage_dir=tmp_path / ".codemap_lite" / "feedback"
        )
        app = create_app(store=InMemoryGraphStore(), feedback_store=feedback_store)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/feedback",
            json={"call_context": "foo()", "wrong_target": "a", "correct_target": "b"},
        )
        assert resp.status_code == 422

    def test_post_feedback_without_store_returns_503(self) -> None:
        """No store wired → 503 so the UI can surface a clear error."""
        client, _ = get_test_client()
        resp = client.post(
            "/api/v1/feedback",
            json={
                "call_context": "foo()",
                "wrong_target": "a",
                "correct_target": "b",
                "pattern": "p",
            },
        )
        assert resp.status_code == 503

    def test_post_feedback_rejects_same_wrong_and_correct_target(self, tmp_path) -> None:
        """A counter-example where wrong_target == correct_target is nonsensical.

        architecture.md §3 反馈机制: counter-examples encode (错误目标, 正确目标)
        as a correction pair. If both are the same, it's not a correction.
        """
        feedback_store = FeedbackStore(
            storage_dir=tmp_path / ".codemap_lite" / "feedback"
        )
        app = create_app(store=InMemoryGraphStore(), feedback_store=feedback_store)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/feedback",
            json={
                "call_context": "foo()",
                "wrong_target": "same_target",
                "correct_target": "same_target",
                "pattern": "p",
            },
        )
        assert resp.status_code == 422

    def test_post_feedback_rejects_empty_fields(self, tmp_path) -> None:
        """architecture.md §5: counter-example fields must be non-empty.

        Empty strings are semantically invalid — a counter-example with
        empty call_context or empty targets provides no useful signal to
        the repair agent.
        """
        feedback_store = FeedbackStore(
            storage_dir=tmp_path / ".codemap_lite" / "feedback"
        )
        app = create_app(store=InMemoryGraphStore(), feedback_store=feedback_store)
        client = TestClient(app)

        # Empty call_context
        resp = client.post(
            "/api/v1/feedback",
            json={
                "call_context": "",
                "wrong_target": "a",
                "correct_target": "b",
                "pattern": "p",
            },
        )
        assert resp.status_code == 422

        # Empty pattern
        resp = client.post(
            "/api/v1/feedback",
            json={
                "call_context": "ctx",
                "wrong_target": "a",
                "correct_target": "b",
                "pattern": "",
            },
        )
        assert resp.status_code == 422

    def test_post_feedback_response_includes_deduplicated_and_total(self, tmp_path) -> None:
        """architecture.md §8: POST /feedback response must include
        'deduplicated' (bool) and 'total' (int) signal fields so the
        frontend can immediately show whether the submission broadened
        an existing rule or opened a new one.
        """
        feedback_store = FeedbackStore(
            storage_dir=tmp_path / ".codemap_lite" / "feedback"
        )
        app = create_app(store=InMemoryGraphStore(), feedback_store=feedback_store)
        client = TestClient(app)

        payload = {
            "call_context": "src/main.cpp:42",
            "wrong_target": "wrong_func",
            "correct_target": "right_func",
            "pattern": "fn_ptr -> wrong_func at main.cpp:42",
        }

        # First submission: new entry
        resp1 = client.post("/api/v1/feedback", json=payload)
        assert resp1.status_code == 201
        data1 = resp1.json()
        assert "deduplicated" in data1
        assert "total" in data1
        assert data1["deduplicated"] is False  # new entry, not merged
        assert data1["total"] == 1

        # Second submission with same pattern: deduplicated
        resp2 = client.post("/api/v1/feedback", json=payload)
        assert resp2.status_code == 201
        data2 = resp2.json()
        assert data2["deduplicated"] is True  # merged into existing
        assert data2["total"] == 1  # still 1 after merge

    def test_review_incorrect_with_correct_target_creates_feedback(self, tmp_path) -> None:
        """architecture.md §5 审阅交互: marking an edge as incorrect with
        correct_target must create a counter-example that is visible via
        GET /feedback. This tests the full end-to-end flow:
        POST /reviews (verdict=incorrect, correct_target) → GET /feedback.
        """
        feedback_store = FeedbackStore(
            storage_dir=tmp_path / ".codemap_lite" / "feedback"
        )
        app = create_app(store=InMemoryGraphStore(), feedback_store=feedback_store)
        client = TestClient(app)
        store = app.state.store

        # Setup: create two functions and a CALLS edge between them
        fn1 = FunctionNode(
            id="caller1", name="caller", signature="void caller()",
            file_path="a.cpp", start_line=1, end_line=5, body_hash="h1",
        )
        fn2 = FunctionNode(
            id="callee1", name="callee", signature="void callee()",
            file_path="b.cpp", start_line=1, end_line=5, body_hash="h2",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge("caller1", "callee1", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="a.cpp", call_line=3,
        ))

        # Mark edge as incorrect with correct_target
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "caller1",
            "callee_id": "callee1",
            "call_file": "a.cpp",
            "call_line": 3,
            "verdict": "incorrect",
            "correct_target": "real_callee",
        })
        assert resp.status_code == 201

        # Verify counter-example is visible via GET /feedback
        feedback_resp = client.get("/api/v1/feedback")
        assert feedback_resp.status_code == 200
        examples = feedback_resp.json()["items"]
        assert len(examples) == 1
        ex = examples[0]
        assert ex["wrong_target"] == "callee1"
        assert ex["correct_target"] == "real_callee"
        assert "a.cpp:3" in ex["call_context"]
        client, store = get_test_client()
        fn = FunctionNode(
            signature="def a()", name="a", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="a",
        )
        store.create_function(fn)
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_functions"] == 1
        assert "total_files" in data
        assert "total_calls" in data
        assert "total_unresolved" in data
        # architecture.md §8 convenience: llm-repaired edge backlog count
        assert "total_llm_edges" in data
        # New breakdown surfaces GAP lifecycle on the Dashboard without
        # drilling into ReviewQueue (architecture.md §3 UnresolvedCall 生命周期).
        assert "unresolved_by_status" in data
        assert data["unresolved_by_status"] == {"pending": 0, "unresolvable": 0}
        # Breakdown by CallsEdgeProps.resolved_by (architecture.md §4 +
        # §5 审阅对象：单条 CALLS 边，特别是 resolved_by='llm' 的).
        # All 5 keys must always be present (architecture.md §8).
        assert "calls_by_resolved_by" in data
        assert data["calls_by_resolved_by"] == {
            "symbol_table": 0, "signature": 0, "dataflow": 0, "context": 0, "llm": 0,
        }
        # Counter-example library size (architecture.md §3 反馈机制 + §8).
        # Without a wired FeedbackStore the field is present and 0 so
        # the left-nav chip can render deterministically (北极星 #5).
        assert "total_feedback" in data
        assert data["total_feedback"] == 0
        # RepairLog count (architecture.md §4 + §8). Surfaces total LLM
        # repair volume so the Dashboard can advertise cumulative repair
        # provenance without hitting /repair-logs.
        assert "total_repair_logs" in data
        assert data["total_repair_logs"] == 0
        # Source points count (architecture.md §8).
        assert "total_source_points" in data
        assert data["total_source_points"] == 0

    def test_get_stats_total_source_points(self) -> None:
        """/stats reports total_source_points from app.state.source_points."""
        client, _ = get_test_client()
        client.app.state.source_points = [
            {"id": "sp1", "entry_point_kind": "callback_registration", "module": "m1"},
            {"id": "sp2", "entry_point_kind": "entry_point", "module": "m2"},
        ]
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        assert resp.json()["total_source_points"] == 2

    def test_get_stats_total_feedback_with_store(self, tmp_path) -> None:
        """/stats reports `total_feedback` from the wired FeedbackStore so
        the left-nav Feedback label can show a live count chip without
        mounting FeedbackLog (architecture.md §3 反馈机制 + §8; 北极星 #5
        状态透明度 + 候选优化方向 #4 进度与可观测性)."""
        store_dir = tmp_path / ".codemap_lite" / "feedback"
        feedback_store = FeedbackStore(storage_dir=store_dir)
        feedback_store.add(
            CounterExample(
                call_context="dispatch(handler)",
                wrong_target="noop",
                correct_target="on_event",
                pattern="dispatch handler must match EventHandler",
            )
        )
        feedback_store.add(
            CounterExample(
                call_context="vtable[i](ctx)",
                wrong_target="fallback",
                correct_target="commit",
                pattern="vtable resolution honours ctx.role",
            )
        )
        graph_store = InMemoryGraphStore()
        app = create_app(store=graph_store, feedback_store=feedback_store)
        client = TestClient(app)
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        assert resp.json()["total_feedback"] == 2

    def test_get_stats_unresolved_by_status(self) -> None:
        """/stats buckets UnresolvedCall nodes by `status` so the Dashboard
        can distinguish retryable pending GAPs from agent-abandoned ones
        (architecture.md §3: retry_count ≥ 3 → status="unresolvable")."""
        client, store = get_test_client()
        fn = FunctionNode(
            signature="def a()", name="a", file_path="f.py",
            start_line=1, end_line=3, body_hash="h1", id="caller",
        )
        store.create_function(fn)
        store.create_unresolved_call(
            UnresolvedCallNode(
                caller_id="caller", call_expression="fp()", call_file="f.py",
                call_line=2, call_type="indirect", source_code_snippet="fp()",
                var_name=None, var_type=None, id="g1", status="pending",
                retry_count=1,
            )
        )
        store.create_unresolved_call(
            UnresolvedCallNode(
                caller_id="caller", call_expression="gp()", call_file="f.py",
                call_line=3, call_type="indirect", source_code_snippet="gp()",
                var_name=None, var_type=None, id="g2", status="unresolvable",
                retry_count=3,
            )
        )
        store.create_unresolved_call(
            UnresolvedCallNode(
                caller_id="caller", call_expression="hp()", call_file="f.py",
                call_line=4, call_type="indirect", source_code_snippet="hp()",
                var_name=None, var_type=None, id="g3", status="unresolvable",
                retry_count=3,
            )
        )
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_unresolved"] == 3
        assert data["total_llm_edges"] == 0  # no CALLS edges created
        assert data["unresolved_by_status"] == {"pending": 1, "unresolvable": 2}

    def test_get_stats_calls_by_resolved_by(self) -> None:
        """/stats buckets CALLS edges by `resolved_by` so the Dashboard
        can surface the llm-repaired edge backlog without drilling into
        ReviewQueue (architecture.md §4 CALLS 边属性 + §5 审阅对象：
        单条 CALLS 边，特别是 resolved_by='llm' 的)."""
        client, store = get_test_client()
        for fid in ("a", "b", "c", "d"):
            store.create_function(
                FunctionNode(
                    signature=f"def {fid}()", name=fid, file_path="f.py",
                    start_line=1, end_line=3, body_hash=f"h-{fid}", id=fid,
                )
            )
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="f.py", call_line=2,
        ))
        store.create_calls_edge("a", "c", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.py", call_line=3,
        ))
        store.create_calls_edge("b", "d", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.py", call_line=4,
        ))
        store.create_calls_edge("c", "d", CallsEdgeProps(
            resolved_by="signature", call_type="indirect",
            call_file="f.py", call_line=5,
        ))
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 4
        assert data["total_llm_edges"] == 2  # §8 convenience
        assert data["calls_by_resolved_by"] == {
            "symbol_table": 1,
            "llm": 2,
            "signature": 1,
            "dataflow": 0,
            "context": 0,
        }

    def test_get_stats_unresolved_by_category(self) -> None:
        """/stats buckets UnresolvedCall nodes by the `<category>:` prefix
        of last_attempt_reason so the Dashboard can show a per-category
        chip row telling reviewers whether the agent-abandoned backlog
        is dominated by LLM stalls (subprocess_timeout) vs hook crashes
        (agent_error) vs gate misses (gate_failed) — architecture.md §3
        Retry 审计字段 4 档 + §5 drill-down 契约 (category chip row)."""
        client, store = get_test_client()
        store.create_function(
            FunctionNode(
                signature="def a()", name="a", file_path="f.py",
                start_line=1, end_line=3, body_hash="h1", id="caller",
            )
        )
        # One of each of the 4 §3 categories + one without any audit
        # stamp (never retried yet) → should bucket to "none".
        categorized = [
            ("g1", 10, "gate_failed: remaining pending GAPs"),
            ("g2", 20, "agent_error: exit 1"),
            ("g3", 30, "subprocess_crash: FileNotFoundError: no such binary"),
            ("g4", 40, "subprocess_timeout: 0.2s"),
        ]
        for gid, line, reason in categorized:
            store.create_unresolved_call(
                UnresolvedCallNode(
                    caller_id="caller", call_expression="fp()", call_file="f.py",
                    call_line=line, call_type="indirect", source_code_snippet="fp()",
                    var_name=None, var_type=None, id=gid, status="pending",
                    retry_count=1, last_attempt_reason=reason,
                    last_attempt_timestamp="2026-05-13T00:00:00+00:00",
                )
            )
        # Second subprocess_timeout so we can verify counts aggregate
        # (not just that keys are present).
        store.create_unresolved_call(
            UnresolvedCallNode(
                caller_id="caller", call_expression="fp()", call_file="f.py",
                call_line=50, call_type="indirect", source_code_snippet="fp()",
                var_name=None, var_type=None, id="g5", status="pending",
                retry_count=2,
                last_attempt_reason="subprocess_timeout: 5.0s",
                last_attempt_timestamp="2026-05-13T00:01:00+00:00",
            )
        )
        # Never-retried GAP: no audit stamp → bucket "none".
        store.create_unresolved_call(
            UnresolvedCallNode(
                caller_id="caller", call_expression="hp()", call_file="f.py",
                call_line=60, call_type="indirect", source_code_snippet="hp()",
                var_name=None, var_type=None, id="g6", status="pending",
                retry_count=0,
            )
        )
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_unresolved"] == 6
        assert data["total_llm_edges"] == 0
        assert data["unresolved_by_category"] == {
            "gate_failed": 1,
            "agent_error": 1,
            "subprocess_crash": 1,
            "subprocess_timeout": 2,
            "agent_exited_without_edge": 0,
            "none": 1,
        }

    def test_get_stats_unresolved_by_category_empty(self) -> None:
        """Empty store still returns all category keys with 0 so the
        frontend doesn't have to guard against undefined."""
        client, _ = get_test_client()
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        assert resp.json()["unresolved_by_category"] == {
            "gate_failed": 0, "agent_error": 0, "subprocess_crash": 0,
            "subprocess_timeout": 0, "agent_exited_without_edge": 0, "none": 0,
        }


class TestNoPrivateAttrLeak:
    """Regression: routes must not reach into store._files / ._functions /
    ._calls_edges / ._unresolved_calls. A Protocol-only fake (no private
    dicts) must work — this is what Neo4jGraphStore looks like."""

    def _make_protocol_store(self):
        """Minimal fake that only exposes public Protocol methods."""
        from dataclasses import dataclass
        from codemap_lite.graph.neo4j_store import _CallsEdge

        class _ProtocolOnlyStore:
            def list_files(self):
                return [FileNode(file_path="a.cpp", hash="h", primary_language="cpp")]

            def list_functions(self, file_path=None):
                fn = FunctionNode(
                    signature="void f()", name="f", file_path="a.cpp",
                    start_line=1, end_line=5, body_hash="bh",
                )
                if file_path and file_path != "a.cpp":
                    return []
                return [fn]

            def list_calls_edges(self):
                return [_CallsEdge(
                    caller_id="f1", callee_id="f2",
                    props=CallsEdgeProps(
                        resolved_by="llm", call_type="indirect",
                        call_file="a.cpp", call_line=10,
                    ),
                )]

            def count_stats(self):
                return {
                    "total_functions": 1, "total_files": 1,
                    "total_calls": 1, "total_unresolved": 0,
                    "total_llm_edges": 1,
                    "total_repair_logs": 0,
                    "unresolved_by_status": {"pending": 0, "unresolvable": 0},
                    "unresolved_by_category": {},
                    "calls_by_resolved_by": {"llm": 1},
                }

            def get_unresolved_calls(self, caller_id=None, status=None, category=None, limit=None, offset=0):
                return []

            def count_unresolved_calls(self, caller_id=None, status=None, category=None):
                return 0

            def get_callers(self, fid):
                return []

            def get_callees(self, fid):
                return []

            def get_function_by_id(self, fid):
                return None

            def get_reachable_subgraph(self, sid, max_depth=50):
                return {"nodes": [], "edges": [], "unresolved": []}

            def get_repair_logs(self, limit=100, offset=0):
                return []

        return _ProtocolOnlyStore()

    def test_stats_no_private_attrs(self) -> None:
        store = self._make_protocol_store()
        app = create_app(store=store)
        client = TestClient(app)
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        assert resp.json()["total_functions"] == 1
        assert resp.json()["calls_by_resolved_by"] == {"llm": 1}

    def test_list_files_no_private_attrs(self) -> None:
        store = self._make_protocol_store()
        app = create_app(store=store)
        client = TestClient(app)
        resp = client.get("/api/v1/files")
        assert resp.status_code == 200
        assert resp.json()["items"][0]["file_path"] == "a.cpp"

    def test_list_functions_no_private_attrs(self) -> None:
        store = self._make_protocol_store()
        app = create_app(store=store)
        client = TestClient(app)
        resp = client.get("/api/v1/functions")
        assert resp.status_code == 200
        assert resp.json()["items"][0]["name"] == "f"

    def test_unresolved_calls_no_private_attrs(self) -> None:
        store = self._make_protocol_store()
        app = create_app(store=store)
        client = TestClient(app)
        resp = client.get("/api/v1/unresolved-calls")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestUnresolvedCallsFiltering:
    """architecture.md §5 line 371-372: ReviewQueue needs ?caller=, ?status=,
    ?category= filters on GET /api/v1/unresolved-calls."""

    def test_filter_by_caller(self) -> None:
        client, store = get_test_client()
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="func_a", call_expression="ptr(x)",
            call_file="a.cpp", call_line=5, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None, id="g1",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="func_b", call_expression="cb(y)",
            call_file="b.cpp", call_line=10, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None, id="g2",
        ))
        resp = client.get("/api/v1/unresolved-calls?caller=func_a")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["caller_id"] == "func_a"

    def test_filter_by_status(self) -> None:
        client, store = get_test_client()
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f1", call_expression="x()",
            call_file="a.cpp", call_line=1, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g1", status="pending",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f2", call_expression="y()",
            call_file="b.cpp", call_line=2, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g2", status="unresolvable", retry_count=3,
        ))
        resp = client.get("/api/v1/unresolved-calls?status=unresolvable")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "g2"

    def test_filter_by_category(self) -> None:
        client, store = get_test_client()
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f1", call_expression="x()",
            call_file="a.cpp", call_line=1, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g1", last_attempt_reason="gate_failed: remaining GAPs",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f2", call_expression="y()",
            call_file="b.cpp", call_line=2, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g2", last_attempt_reason="agent_error: exit 1",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f3", call_expression="z()",
            call_file="c.cpp", call_line=3, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g3",  # no last_attempt_reason → "none" category
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="f4", call_expression="w()",
            call_file="d.cpp", call_line=4, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
            id="g4", last_attempt_reason="agent_exited_without_edge",
        ))
        # Filter by gate_failed
        resp = client.get("/api/v1/unresolved-calls?category=gate_failed")
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["id"] == "g1"
        # Filter by "none" (no audit stamp)
        resp = client.get("/api/v1/unresolved-calls?category=none")
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["id"] == "g3"
        # Filter by standalone category (no colon in reason)
        resp = client.get("/api/v1/unresolved-calls?category=agent_exited_without_edge")
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["id"] == "g4"

    def test_pagination_limit_offset(self) -> None:
        """architecture.md §8: GET /unresolved-calls supports ?limit= and ?offset=."""
        client, store = get_test_client()
        # Create 5 gaps
        for i in range(5):
            store.create_unresolved_call(UnresolvedCallNode(
                caller_id="f1", call_expression=f"fn{i}()",
                call_file="a.cpp", call_line=i + 1, call_type="indirect",
                source_code_snippet="", var_name=None, var_type=None, id=f"g{i}",
            ))
        # Default: all 5
        resp = client.get("/api/v1/unresolved-calls")
        assert resp.json()["total"] == 5
        assert len(resp.json()["items"]) == 5

        # Limit to 2
        resp = client.get("/api/v1/unresolved-calls?limit=2")
        assert resp.json()["total"] == 5  # total is unaffected
        assert len(resp.json()["items"]) == 2

        # Offset 3, limit 2 → only 2 remaining
        resp = client.get("/api/v1/unresolved-calls?offset=3&limit=10")
        assert resp.json()["total"] == 5
        assert len(resp.json()["items"]) == 2


def _make_repair_log(
    *,
    caller_id: str = "func_a",
    callee_id: str = "func_b",
    call_location: str = "foo.cpp:42",
    log_id: str | None = None,
    reasoning_summary: str = "vtable resolved via static analysis",
) -> RepairLogNode:
    kwargs: dict = dict(
        caller_id=caller_id,
        callee_id=callee_id,
        call_location=call_location,
        repair_method="llm",
        llm_response="agent stdout",
        timestamp="2026-05-13T12:00:00+00:00",
        reasoning_summary=reasoning_summary,
    )
    if log_id is not None:
        kwargs["id"] = log_id
    return RepairLogNode(**kwargs)


class TestRepairLogsEndpoint:
    """architecture.md §4 RepairLog schema + §8 GET /repair-logs +
    ADR #51 属性引用契约 — the (caller_id, callee_id, call_location)
    triple locates the matching CALLS edge so the frontend
    CallGraphView can render an audit panel for any selected
    `resolved_by='llm'` edge."""

    def test_list_all_repair_logs_empty(self) -> None:
        client, _ = get_test_client()
        resp = client.get("/api/v1/repair-logs")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"total": 0, "items": []}

    def test_list_returns_persisted_logs(self) -> None:
        client, store = get_test_client()
        log = _make_repair_log(log_id="r1")
        store.create_repair_log(log)
        resp = client.get("/api/v1/repair-logs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["id"] == "r1"
        assert body["items"][0]["caller_id"] == "func_a"
        assert body["items"][0]["callee_id"] == "func_b"
        assert body["items"][0]["call_location"] == "foo.cpp:42"
        assert body["items"][0]["repair_method"] == "llm"
        assert body["items"][0]["reasoning_summary"].startswith("vtable")

    def test_filter_by_triple_locates_single_log(self) -> None:
        client, store = get_test_client()
        store.create_repair_log(
            _make_repair_log(call_location="foo.cpp:42", log_id="r1")
        )
        store.create_repair_log(
            _make_repair_log(call_location="foo.cpp:99", log_id="r2")
        )
        resp = client.get(
            "/api/v1/repair-logs",
            params={
                "caller": "func_a",
                "callee": "func_b",
                "location": "foo.cpp:42",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["id"] == "r1"

    def test_filter_by_caller_only(self) -> None:
        client, store = get_test_client()
        store.create_repair_log(_make_repair_log(caller_id="func_a", log_id="r1"))
        store.create_repair_log(_make_repair_log(caller_id="func_z", log_id="r2"))
        resp = client.get("/api/v1/repair-logs", params={"caller": "func_a"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == "r1"

    def test_pagination_limit_and_offset(self) -> None:
        """architecture.md §8: GET /repair-logs supports limit/offset pagination."""
        client, store = get_test_client()
        for i in range(5):
            store.create_repair_log(
                _make_repair_log(log_id=f"r{i}", call_location=f"f.cpp:{i}")
            )
        # Default returns all (limit=100)
        resp = client.get("/api/v1/repair-logs")
        assert resp.json()["total"] == 5
        assert len(resp.json()["items"]) == 5

        # limit=2 returns first 2
        resp = client.get("/api/v1/repair-logs", params={"limit": 2})
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2

        # offset=3 skips first 3
        resp = client.get("/api/v1/repair-logs", params={"offset": 3})
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2

        # limit + offset
        resp = client.get("/api/v1/repair-logs", params={"limit": 2, "offset": 1})
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2

        # Invalid: negative offset → 422
        resp = client.get("/api/v1/repair-logs", params={"offset": -1})
        assert resp.status_code == 422

        # Invalid: limit=0 → 422
        resp = client.get("/api/v1/repair-logs", params={"limit": 0})
        assert resp.status_code == 422

    def test_total_repair_logs_in_stats(self) -> None:
        """/stats reports `total_repair_logs` so the Dashboard can show
        cumulative llm-repair volume without hitting /repair-logs
        (architecture.md §8 stats契约)."""
        client, store = get_test_client()
        # Empty case still surfaces the field.
        empty = client.get("/api/v1/stats").json()
        assert empty["total_repair_logs"] == 0

        store.create_repair_log(_make_repair_log(log_id="r1"))
        store.create_repair_log(
            _make_repair_log(log_id="r2", call_location="foo.cpp:99")
        )
        populated = client.get("/api/v1/stats").json()
        assert populated["total_repair_logs"] == 2


class TestEdgeCreation:
    """POST /api/v1/edges — manual edge creation with duplicate guard."""

    def test_create_edge_rejects_duplicate(self) -> None:
        """architecture.md §4: CALLS edges are unique by (caller_id, callee_id,
        call_file, call_line). Submitting a duplicate must return 409 Conflict."""
        client, store = get_test_client()
        store.create_function(FunctionNode(
            id="fn_a", name="a", signature="void a()",
            file_path="x.c", start_line=1, end_line=5, body_hash="h1",
        ))
        store.create_function(FunctionNode(
            id="fn_b", name="b", signature="void b()",
            file_path="x.c", start_line=10, end_line=15, body_hash="h2",
        ))
        body = {
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "direct",
            "call_file": "x.c",
            "call_line": 3,
        }
        # First creation succeeds
        resp = client.post("/api/v1/edges", json=body)
        assert resp.status_code == 201

        # Duplicate must be rejected
        resp2 = client.post("/api/v1/edges", json=body)
        assert resp2.status_code == 409

    def test_create_edge_deletes_corresponding_unresolved_call(self) -> None:
        """architecture.md §3: creating a CALLS edge must delete the matching
        UnresolvedCall (same behavior as icsl_tools.write_edge). When a
        reviewer manually resolves a GAP via POST /edges, the GAP should
        disappear from the backlog."""
        from codemap_lite.graph.schema import UnresolvedCallNode

        client, store = get_test_client()
        store.create_function(FunctionNode(
            id="fn_a", name="a", signature="void a()",
            file_path="x.c", start_line=1, end_line=5, body_hash="h1",
        ))
        store.create_function(FunctionNode(
            id="fn_b", name="b", signature="void b()",
            file_path="x.c", start_line=10, end_line=15, body_hash="h2",
        ))
        # Pre-existing UnresolvedCall for this call site
        store.create_unresolved_call(UnresolvedCallNode(
            id="uc1", caller_id="fn_a", call_expression="b()",
            call_file="x.c", call_line=3, call_type="indirect",
            source_code_snippet="b();", var_name=None, var_type=None,
            retry_count=0, status="pending",
        ))
        assert len(store.get_unresolved_calls(caller_id="fn_a")) == 1

        # Create edge resolving this GAP
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "indirect",
            "call_file": "x.c",
            "call_line": 3,
        })
        assert resp.status_code == 201

        # UnresolvedCall must be deleted
        remaining = store.get_unresolved_calls(caller_id="fn_a")
        assert len(remaining) == 0, (
            "architecture.md §3: creating edge must delete matching UnresolvedCall"
        )

    def test_create_edge_nonexistent_caller_returns_404(self) -> None:
        """architecture.md §8: edges must reference valid Function nodes."""
        client, store = get_test_client()
        store.create_function(FunctionNode(
            id="fn_b", name="b", signature="void b()",
            file_path="x.c", start_line=10, end_line=15, body_hash="h2",
        ))
        resp = client.post("/api/v1/edges", json={
            "caller_id": "nonexistent",
            "callee_id": "fn_b",
            "resolved_by": "llm",
            "call_type": "direct",
            "call_file": "x.c",
            "call_line": 3,
        })
        assert resp.status_code == 404
        assert "Caller" in resp.json()["detail"]

    def test_create_edge_nonexistent_callee_returns_404(self) -> None:
        """architecture.md §8: edges must reference valid Function nodes."""
        client, store = get_test_client()
        store.create_function(FunctionNode(
            id="fn_a", name="a", signature="void a()",
            file_path="x.c", start_line=1, end_line=5, body_hash="h1",
        ))
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "nonexistent",
            "resolved_by": "llm",
            "call_type": "direct",
            "call_file": "x.c",
            "call_line": 3,
        })
        assert resp.status_code == 404
        assert "Callee" in resp.json()["detail"]


class TestEdgeDeletion:
    """architecture.md §5 审阅交互: '标记错误时 → 立即删除该 CALLS 边 + 对应
    RepairLog'. DELETE /api/v1/edges must target a specific edge by
    (caller_id, callee_id, call_file, call_line), not bulk-delete."""

    def test_delete_specific_edge(self) -> None:
        """Deleting a specific edge by its identifying tuple."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        fn3 = FunctionNode(
            signature="void c()", name="c", file_path="f.cpp",
            start_line=20, end_line=25, body_hash="h3", id="c",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_function(fn3)
        # Two edges from a
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="direct",
            call_file="f.cpp", call_line=3,
        ))
        store.create_calls_edge("a", "c", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="f.cpp", call_line=4,
        ))
        assert len(store.list_calls_edges()) == 2

        # Delete only the a→b edge
        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "a",
                "callee_id": "b",
                "call_file": "f.cpp",
                "call_line": 3,
            },
        )
        assert resp.status_code == 204

        # Only a→c should remain
        remaining = store.list_calls_edges()
        assert len(remaining) == 1
        assert remaining[0].callee_id == "c"

    def test_delete_edge_not_found_returns_404(self) -> None:
        """Deleting a non-existent edge returns 404."""
        client, store = get_test_client()
        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "x",
                "callee_id": "y",
                "call_file": "f.cpp",
                "call_line": 1,
            },
        )
        assert resp.status_code == 404

    def test_delete_edge_also_deletes_repair_log(self) -> None:
        """architecture.md §5 line 326: '立即删除该 CALLS 边 + 对应 RepairLog'.
        When an edge is deleted, the corresponding RepairLog must also be removed."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.cpp", call_line=3,
        ))
        store.create_repair_log(RepairLogNode(
            id="rl1", caller_id="a", callee_id="b",
            call_location="f.cpp:3",
            repair_method="llm", llm_response="resolved",
            timestamp="2026-05-14T00:00:00Z", reasoning_summary="test",
        ))

        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "a",
                "callee_id": "b",
                "call_file": "f.cpp",
                "call_line": 3,
            },
        )
        assert resp.status_code == 204

        logs = store.get_repair_logs(caller_id="a", callee_id="b")
        assert len(logs) == 0, (
            "architecture.md §5: deleting edge must also delete RepairLog"
        )

    def test_delete_edge_regenerates_unresolved_call(self) -> None:
        """architecture.md §5 line 327: '重新生成 UnresolvedCall 节点（retry_count=0）'.
        After deleting an edge, a new UnresolvedCall must be created for the caller."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.cpp", call_line=3,
        ))

        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "a",
                "callee_id": "b",
                "call_file": "f.cpp",
                "call_line": 3,
            },
        )
        assert resp.status_code == 204

        gaps = store.get_unresolved_calls(caller_id="a")
        assert len(gaps) == 1, (
            "architecture.md §5: deleting edge must regenerate UnresolvedCall"
        )
        gap = gaps[0]
        assert gap.caller_id == "a"
        assert gap.call_file == "f.cpp"
        assert gap.call_line == 3
        assert gap.call_type == "indirect"
        assert gap.retry_count == 0
        assert gap.status == "pending"

    def test_delete_edge_triggers_async_repair(self) -> None:
        """architecture.md §5 line 328: '触发 Agent 重新修复该 source 点（异步）'.

        When settings are available on app.state, deleting an edge must
        schedule a background repair task for the affected source.
        """
        from unittest.mock import patch, MagicMock

        store = InMemoryGraphStore()
        # Create a minimal settings mock
        settings = MagicMock()
        settings.agent.backend = "claudecode"
        settings.agent.max_concurrency = 1
        settings.agent.subprocess_timeout_seconds = None
        settings.project.target_dir = "/tmp/test"
        settings.neo4j.uri = "bolt://localhost:7687"
        settings.neo4j.user = "neo4j"
        settings.neo4j.password = ""

        app = create_app(store=store, settings=settings)
        client = TestClient(app)

        # Set up an edge to delete
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.cpp", call_line=3,
        ))

        # Patch _trigger_repair_for_source to verify it's called
        with patch(
            "codemap_lite.api.routes.review._trigger_repair_for_source"
        ) as mock_trigger:
            resp = client.request(
                "DELETE", "/api/v1/edges",
                json={
                    "caller_id": "a",
                    "callee_id": "b",
                    "call_file": "f.cpp",
                    "call_line": 3,
                },
            )
            assert resp.status_code == 204
            # Background task should have been called with settings + caller_id
            mock_trigger.assert_called_once_with(settings, "a")

    def test_delete_edges_for_function_bulk(self) -> None:
        """architecture.md §7: DELETE /edges/{function_id} bulk-deletes all
        edges touching a function (used by incremental invalidation)."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        fn3 = FunctionNode(
            signature="void c()", name="c", file_path="g.cpp",
            start_line=1, end_line=5, body_hash="h3", id="c",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_function(fn3)
        # a→b, b→c, c→a (cycle)
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="f.cpp", call_line=3,
        ))
        store.create_calls_edge("b", "c", CallsEdgeProps(
            resolved_by="signature", call_type="direct",
            call_file="f.cpp", call_line=12,
        ))
        store.create_calls_edge("c", "a", CallsEdgeProps(
            resolved_by="dataflow", call_type="indirect",
            call_file="g.cpp", call_line=4,
        ))
        assert len(store.list_calls_edges()) == 3

        # Delete all edges touching function "b" (a→b and b→c)
        resp = client.delete("/api/v1/edges/b")
        assert resp.status_code == 204

        # Only c→a should remain
        remaining = store.list_calls_edges()
        assert len(remaining) == 1
        assert remaining[0].caller_id == "c"
        assert remaining[0].callee_id == "a"

    def test_delete_edge_resets_source_point_to_pending(self) -> None:
        """architecture.md §5: deleting an edge must reset the caller's
        SourcePoint status to 'pending' so the frontend reflects that the
        source needs re-processing (same behavior as review verdict=incorrect)."""
        from codemap_lite.graph.schema import SourcePointNode

        client, store = get_test_client()

        # Setup: caller + callee + LLM edge + SourcePoint=complete
        store.create_function(FunctionNode(
            id="src_fn", name="src_fn", signature="void src_fn()",
            file_path="x.cpp", start_line=1, end_line=10, body_hash="h1",
        ))
        store.create_function(FunctionNode(
            id="tgt_fn", name="tgt_fn", signature="void tgt_fn()",
            file_path="x.cpp", start_line=20, end_line=30, body_hash="h2",
        ))
        store.create_calls_edge("src_fn", "tgt_fn", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="x.cpp", call_line=5,
        ))
        store.create_source_point(SourcePointNode(
            id="src_fn", function_id="src_fn",
            entry_point_kind="callback_registration",
            reason="test", status="complete",
        ))

        # Verify initial state
        sp = store.get_source_point("src_fn")
        assert sp is not None
        assert sp.status == "complete"

        # Delete the edge
        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "src_fn",
                "callee_id": "tgt_fn",
                "call_file": "x.cpp",
                "call_line": 5,
            },
        )
        assert resp.status_code == 204

        # SourcePoint must be reset to "pending"
        sp_after = store.get_source_point("src_fn")
        assert sp_after is not None
        assert sp_after.status == "pending", (
            "architecture.md §5: DELETE /edges must reset SourcePoint to pending"
        )

    """architecture.md §8: /api/v1/stats must return unresolved_by_category
    with all 5 keys always present, and calls_by_resolved_by with all 5
    resolved_by values."""

    def test_stats_unresolved_by_category_all_keys_present(self) -> None:
        """Even with no unresolved calls, all category keys must appear with 0."""
        client, store = get_test_client()
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "unresolved_by_category" in data
        cats = data["unresolved_by_category"]
        expected_keys = {"gate_failed", "agent_error", "subprocess_crash",
                         "subprocess_timeout", "agent_exited_without_edge", "none"}
        assert set(cats.keys()) == expected_keys, (
            f"unresolved_by_category must always have all 6 keys, got {set(cats.keys())}"
        )
        # All should be 0 when no unresolved calls exist
        for k in expected_keys:
            assert cats[k] == 0

    def test_stats_calls_by_resolved_by_all_keys_present(self) -> None:
        """Even with no calls edges, all resolved_by keys must appear with 0."""
        client, store = get_test_client()
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "calls_by_resolved_by" in data
        resolved = data["calls_by_resolved_by"]
        expected_keys = {"symbol_table", "signature", "dataflow", "context", "llm"}
        assert set(resolved.keys()) == expected_keys, (
            f"calls_by_resolved_by must always have all 5 keys, got {set(resolved.keys())}"
        )
        for k in expected_keys:
            assert resolved[k] == 0

    def test_stats_category_bucketing_correct(self) -> None:
        """Verify category extraction from last_attempt_reason prefix."""
        client, store = get_test_client()
        # Add unresolved calls with different reasons
        fn = FunctionNode(
            id="f1", signature="void f()", name="f",
            file_path="a.c", start_line=1, end_line=5, body_hash="h",
        )
        store.create_function(fn)
        reasons = [
            ("gap_1", 10, "gate_failed: remaining pending GAPs"),
            ("gap_2", 20, "agent_error: exit 1"),
            ("gap_3", 30, "subprocess_timeout: 30s"),
            ("gap_4", 40, "subprocess_crash: OSError: No such file"),
            ("gap_5", 50, None),  # no reason → "none" bucket
        ]
        for gap_id, line, reason in reasons:
            gap = UnresolvedCallNode(
                id=gap_id, caller_id="f1", call_expression="x()",
                call_file="a.c", call_line=line, call_type="indirect",
                source_code_snippet="x();", var_name=None, var_type=None,
                last_attempt_reason=reason,
            )
            store.create_unresolved_call(gap)

        resp = client.get("/api/v1/stats")
        data = resp.json()
        cats = data["unresolved_by_category"]
        assert cats["gate_failed"] == 1
        assert cats["agent_error"] == 1
        assert cats["subprocess_timeout"] == 1
        assert cats["subprocess_crash"] == 1
        assert cats["none"] == 1


class TestReviewResetsSourcePointStatus:
    """architecture.md §5: marking an edge incorrect triggers re-repair.
    The SourcePoint status must be reset to 'pending' so the frontend
    reflects that the source needs re-processing."""

    def test_review_incorrect_resets_source_point_to_pending(self) -> None:
        from codemap_lite.graph.schema import SourcePointNode

        client, store = get_test_client()

        # Setup: source function + callee + LLM edge + SourcePoint=complete
        store.create_function(FunctionNode(
            id="caller_fn", name="caller", signature="void caller()",
            file_path="src/a.c", start_line=1, end_line=10, body_hash="h1",
        ))
        store.create_function(FunctionNode(
            id="callee_fn", name="callee", signature="void callee()",
            file_path="src/b.c", start_line=1, end_line=10, body_hash="h2",
        ))
        store.create_calls_edge("caller_fn", "callee_fn", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="src/a.c", call_line=5,
        ))
        store.create_repair_log(RepairLogNode(
            caller_id="caller_fn", callee_id="callee_fn",
            call_location="src/a.c:5", repair_method="llm",
            llm_response="analysis", timestamp="2026-05-14T00:00:00Z",
            reasoning_summary="test",
        ))
        store.create_source_point(SourcePointNode(
            id="caller_fn", function_id="caller_fn",
            entry_point_kind="callback_registration",
            reason="test", status="complete",
        ))

        # Verify initial state
        sp = store.get_source_point("caller_fn")
        assert sp.status == "complete"

        # Mark edge as incorrect
        resp = client.post("/api/v1/reviews", json={
            "caller_id": "caller_fn",
            "callee_id": "callee_fn",
            "call_file": "src/a.c",
            "call_line": 5,
            "verdict": "incorrect",
        })
        assert resp.status_code == 201

        # SourcePoint must be reset to "pending"
        sp_after = store.get_source_point("caller_fn")
        assert sp_after is not None
        assert sp_after.status == "pending", (
            "architecture.md §5: SourcePoint must reset to 'pending' when "
            "a review marks an edge incorrect and triggers re-repair"
        )


class TestReviewCounterExampleIntegration:
    """Integration tests for the review→counter-example→repair flow.

    architecture.md §5 + §3: marking an edge incorrect with correct_target
    creates a counter-example that persists into the next repair cycle.
    """

    def test_counter_example_persists_across_repair_cycles(self) -> None:
        """architecture.md §3 反馈机制: counter-examples created by review
        must be available to subsequent repair runs via render_markdown().
        """
        import tempfile
        from pathlib import Path
        from codemap_lite.analysis.feedback_store import FeedbackStore

        store = InMemoryGraphStore()
        with tempfile.TemporaryDirectory() as tmpdir:
            feedback_store = FeedbackStore(storage_dir=Path(tmpdir))
            app = create_app(store=store, feedback_store=feedback_store)
            client = TestClient(app)

            # Setup: create an LLM-resolved edge
            fn1 = FunctionNode(
                signature="void caller()", name="caller",
                file_path="a.cpp", start_line=1, end_line=10,
                body_hash="h1", id="fn_caller",
            )
            fn2 = FunctionNode(
                signature="void wrong_callee()", name="wrong_callee",
                file_path="b.cpp", start_line=1, end_line=10,
                body_hash="h2", id="fn_wrong",
            )
            store.create_function(fn1)
            store.create_function(fn2)
            store.create_calls_edge(
                "fn_caller", "fn_wrong",
                CallsEdgeProps(
                    resolved_by="llm", call_type="indirect",
                    call_file="a.cpp", call_line=5,
                ),
            )

            # Act: reviewer marks edge incorrect with correct_target
            resp = client.post("/api/v1/reviews", json={
                "caller_id": "fn_caller",
                "callee_id": "fn_wrong",
                "call_file": "a.cpp",
                "call_line": 5,
                "verdict": "incorrect",
                "correct_target": "fn_correct",
            })
            assert resp.status_code == 201

            # Verify: counter-example is in the store
            examples = feedback_store.list_all()
            assert len(examples) == 1

            # Verify: render_markdown() produces content the agent can read
            md = feedback_store.render_markdown()
            assert "fn_wrong" in md, "Wrong target must appear in counter-example markdown"
            assert "fn_correct" in md, "Correct target must appear in counter-example markdown"

            # Verify: a fresh FeedbackStore instance (simulating next repair cycle)
            # can still read the counter-example
            fresh_store = FeedbackStore(storage_dir=Path(tmpdir))
            fresh_examples = fresh_store.list_all()
            assert len(fresh_examples) == 1
            assert fresh_examples[0].correct_target == "fn_correct"

    def test_review_returns_counter_example_dedup_status(self) -> None:
        """architecture.md §3: when a duplicate counter-example is created,
        the review response should indicate deduplicated=true so the frontend
        can show "已合并到现有规则" instead of "反例已保存".
        """
        import tempfile
        from pathlib import Path
        from codemap_lite.analysis.feedback_store import FeedbackStore

        store = InMemoryGraphStore()
        with tempfile.TemporaryDirectory() as tmpdir:
            feedback_store = FeedbackStore(storage_dir=Path(tmpdir))
            app = create_app(store=store, feedback_store=feedback_store)
            client = TestClient(app)

            # Setup: create edge
            fn1 = FunctionNode(
                signature="void caller()", name="caller",
                file_path="a.cpp", start_line=1, end_line=10,
                body_hash="h1", id="fn_caller2",
            )
            fn2 = FunctionNode(
                signature="void wrong()", name="wrong",
                file_path="b.cpp", start_line=1, end_line=10,
                body_hash="h2", id="fn_wrong2",
            )
            store.create_function(fn1)
            store.create_function(fn2)

            def _create_edge_and_review():
                store.create_calls_edge(
                    "fn_caller2", "fn_wrong2",
                    CallsEdgeProps(
                        resolved_by="llm", call_type="indirect",
                        call_file="a.cpp", call_line=10,
                    ),
                )
                return client.post("/api/v1/reviews", json={
                    "caller_id": "fn_caller2",
                    "callee_id": "fn_wrong2",
                    "call_file": "a.cpp",
                    "call_line": 10,
                    "verdict": "incorrect",
                    "correct_target": "fn_right",
                })

            # First review: new counter-example
            resp1 = _create_edge_and_review()
            assert resp1.status_code == 201
            data1 = resp1.json()
            assert data1.get("counter_example_deduplicated") is False

            # Second review with same pattern: should be deduplicated
            resp2 = _create_edge_and_review()
            assert resp2.status_code == 201
            data2 = resp2.json()
            assert data2.get("counter_example_deduplicated") is True


class TestEdgeCreateResolvedByValidation:
    """architecture.md §4: CALLS.resolved_by ∈
    {symbol_table, signature, dataflow, context, llm}.

    The POST /edges endpoint must reject values outside this enum.
    """

    def test_valid_resolved_by_values_accepted(self) -> None:
        """All 5 architecture-defined resolved_by values must be accepted."""
        for rb in ("symbol_table", "signature", "dataflow", "context", "llm"):
            client, store = get_test_client()
            store.create_function(FunctionNode(
                id="fn_a", name="a", signature="void a()",
                file_path="x.c", start_line=1, end_line=5, body_hash="h1",
            ))
            store.create_function(FunctionNode(
                id="fn_b", name="b", signature="void b()",
                file_path="x.c", start_line=10, end_line=15, body_hash="h2",
            ))
            resp = client.post("/api/v1/edges", json={
                "caller_id": "fn_a",
                "callee_id": "fn_b",
                "resolved_by": rb,
                "call_type": "direct",
                "call_file": "x.c",
                "call_line": 3,
            })
            assert resp.status_code == 201, f"resolved_by='{rb}' should be accepted"

    def test_manual_resolved_by_rejected(self) -> None:
        """architecture.md §4: 'manual' is NOT a valid resolved_by value.

        The API must reject it with 422 (validation error), not crash with 500.
        """
        client, store = get_test_client()
        store.create_function(FunctionNode(
            id="fn_a", name="a", signature="void a()",
            file_path="x.c", start_line=1, end_line=5, body_hash="h1",
        ))
        store.create_function(FunctionNode(
            id="fn_b", name="b", signature="void b()",
            file_path="x.c", start_line=10, end_line=15, body_hash="h2",
        ))
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "manual",
            "call_type": "direct",
            "call_file": "x.c",
            "call_line": 3,
        })
        assert resp.status_code == 422, (
            "resolved_by='manual' must be rejected by API validation (422), "
            f"got {resp.status_code}"
        )

    def test_unknown_resolved_by_rejected(self) -> None:
        """Completely unknown resolved_by values must be rejected."""
        client, store = get_test_client()
        store.create_function(FunctionNode(
            id="fn_a", name="a", signature="void a()",
            file_path="x.c", start_line=1, end_line=5, body_hash="h1",
        ))
        store.create_function(FunctionNode(
            id="fn_b", name="b", signature="void b()",
            file_path="x.c", start_line=10, end_line=15, body_hash="h2",
        ))
        resp = client.post("/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "resolved_by": "magic",
            "call_type": "direct",
            "call_file": "x.c",
            "call_line": 3,
        })
        assert resp.status_code == 422


class TestEdgeDeletionBugFixes:
    """Tests for §5 HIGH-severity bugs: call_type order + EdgeDelete correct_target."""

    def test_delete_edge_preserves_original_call_type(self) -> None:
        """§5 Bug #4: delete_edge must validate edge exists BEFORE capturing
        call_type, then use the edge's actual call_type (not fallback 'indirect')
        when regenerating the UnresolvedCall."""
        client, store = get_test_client()
        fn1 = FunctionNode(
            signature="void a()", name="a", file_path="f.cpp",
            start_line=1, end_line=5, body_hash="h1", id="a",
        )
        fn2 = FunctionNode(
            signature="void b()", name="b", file_path="f.cpp",
            start_line=10, end_line=15, body_hash="h2", id="b",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        # Create edge with call_type="virtual" (not "indirect")
        store.create_calls_edge("a", "b", CallsEdgeProps(
            resolved_by="llm", call_type="virtual",
            call_file="f.cpp", call_line=3,
        ))

        resp = client.request(
            "DELETE", "/api/v1/edges",
            json={
                "caller_id": "a",
                "callee_id": "b",
                "call_file": "f.cpp",
                "call_line": 3,
            },
        )
        assert resp.status_code == 204

        gaps = store.get_unresolved_calls(caller_id="a")
        assert len(gaps) == 1
        # Must preserve the original call_type="virtual", not fallback "indirect"
        assert gaps[0].call_type == "virtual", (
            "regenerated UC must preserve original call_type from deleted edge"
        )

    def test_delete_edge_with_correct_target_creates_counter_example(self) -> None:
        """§5 Bug #7: DELETE /edges with correct_target must create a
        counter-example in FeedbackStore (same as review verdict=incorrect
        with correct_target per architecture.md §5)."""
        from codemap_lite.analysis.feedback_store import FeedbackStore
        import tempfile

        store = InMemoryGraphStore()
        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path
            feedback_store = FeedbackStore(storage_dir=Path(tmpdir))
            app = create_app(store=store, feedback_store=feedback_store)
            client = TestClient(app)

            fn1 = FunctionNode(
                signature="void src()", name="src", file_path="x.cpp",
                start_line=1, end_line=10, body_hash="h1", id="fn_src",
            )
            fn2 = FunctionNode(
                signature="void wrong()", name="wrong", file_path="x.cpp",
                start_line=20, end_line=30, body_hash="h2", id="fn_wrong",
            )
            store.create_function(fn1)
            store.create_function(fn2)
            store.create_calls_edge("fn_src", "fn_wrong", CallsEdgeProps(
                resolved_by="llm", call_type="indirect",
                call_file="x.cpp", call_line=5,
            ))

            # Delete edge WITH correct_target
            resp = client.request(
                "DELETE", "/api/v1/edges",
                json={
                    "caller_id": "fn_src",
                    "callee_id": "fn_wrong",
                    "call_file": "x.cpp",
                    "call_line": 5,
                    "correct_target": "fn_real_target",
                },
            )
            assert resp.status_code == 204

            # Counter-example must have been created
            examples = feedback_store.list_all()
            assert len(examples) == 1, (
                "§5: DELETE /edges with correct_target must create counter-example"
            )
            assert examples[0].wrong_target == "fn_wrong"
            assert examples[0].correct_target == "fn_real_target"


class TestReviewIdempotency:
    """architecture.md §5: review operations should handle edge-already-deleted
    gracefully (idempotent delete semantics)."""

    def _setup_edge(self, store: InMemoryGraphStore) -> None:
        store.create_function(FunctionNode(
            id="fn_a", name="a", signature="void a()",
            file_path="x.c", start_line=1, end_line=5, body_hash="h1",
        ))
        store.create_function(FunctionNode(
            id="fn_b", name="b", signature="void b()",
            file_path="x.c", start_line=10, end_line=15, body_hash="h2",
        ))
        store.create_calls_edge(
            "fn_a", "fn_b",
            CallsEdgeProps(
                resolved_by="llm", call_type="indirect",
                call_file="x.c", call_line=3,
            ),
        )

    def test_delete_edge_twice_returns_404_on_second(self) -> None:
        """Deleting an already-deleted edge should return 404, not 500."""
        client, store = get_test_client()
        self._setup_edge(store)

        # First delete succeeds
        resp1 = client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "call_file": "x.c",
            "call_line": 3,
        })
        assert resp1.status_code == 204

        # Second delete: edge no longer exists → 404
        resp2 = client.request("DELETE", "/api/v1/edges", json={
            "caller_id": "fn_a",
            "callee_id": "fn_b",
            "call_file": "x.c",
            "call_line": 3,
        })
        assert resp2.status_code == 404


class TestSourcePointKindAlias:
    """architecture.md §8: source-points response must include 'kind' alias."""

    def test_source_points_response_includes_kind_field(self) -> None:
        """Frontend uses p.kind but backend stores entry_point_kind."""
        store = InMemoryGraphStore()
        app = create_app(store=store)
        app.state.source_points = [
            {"id": "sp1", "function_id": "fn1", "entry_point_kind": "callback", "module": "mod", "reason": "test"},
        ]
        client = TestClient(app)
        resp = client.get("/api/v1/source-points")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["kind"] == "callback"
        assert items[0]["entry_point_kind"] == "callback"


class TestAnalyzeStateNaming:
    """architecture.md §8: analyze state must use 'running' not 'analyzing'."""

    def test_analyze_trigger_sets_running_state(self) -> None:
        """Frontend checks status?.state === 'running' to disable buttons."""
        client, _ = get_test_client()
        resp = client.post("/api/v1/analyze", json={"mode": "full"})
        assert resp.status_code == 202
        status_resp = client.get("/api/v1/analyze/status")
        assert status_resp.json()["state"] == "running"

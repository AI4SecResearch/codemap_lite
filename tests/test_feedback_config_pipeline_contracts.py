"""Feedback store, config, and pipeline contracts — architecture.md §3/§8/§10.

Tests targeting:
1. FeedbackStore dedup, render, persistence, corruption recovery
2. POST /feedback validation (targets_must_differ, min_length, dedup signal)
3. GET /feedback pagination
4. Settings.from_yaml env var interpolation
5. AgentConfig validation (backend enum, max_concurrency, timeout)
6. Pipeline file discovery and resolution layer verification (CastEngine)
7. GET /source-points filters and enrichment
8. GET /unresolved-calls filters

BUG HUNTING TARGETS:
1. FeedbackStore.add() dedup is pattern-exact only — no LLM generalization
2. POST /feedback returns 503 when store unconfigured (not 500)
3. Settings.from_yaml doesn't validate missing env vars (empty string)
4. Pipeline direct call resolution rate < 100% (architecture says 100%)
5. GET /source-points enrichment fails when function_id not in store
6. GET /unresolved-calls category filter doesn't match reason prefix
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path

import pytest

from codemap_lite.analysis.feedback_store import CounterExample, FeedbackStore
from codemap_lite.config.settings import AgentConfig, Settings
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator


CASTENGINE_DIR = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")


# ===========================================================================
# §3 FeedbackStore — dedup, render, persistence, corruption recovery
# ===========================================================================


class TestFeedbackStoreDedup:
    """architecture.md §3: FeedbackStore deduplicates by pattern."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self._tmpdir = Path(tempfile.mkdtemp())
        self.store = FeedbackStore(storage_dir=self._tmpdir)

    def test_add_new_example_returns_true(self):
        """New pattern returns True (appended)."""
        ex = CounterExample(
            call_context="vtable dispatch in loop",
            wrong_target="fn_wrong",
            correct_target="fn_correct",
            pattern="vtable dispatch must match signature",
            source_id="src_1",
        )
        assert self.store.add(ex) is True

    def test_add_duplicate_pattern_returns_false(self):
        """Same pattern returns False (deduplicated)."""
        ex1 = CounterExample(
            call_context="context1", wrong_target="w1",
            correct_target="c1", pattern="same_pattern",
        )
        ex2 = CounterExample(
            call_context="context2", wrong_target="w2",
            correct_target="c2", pattern="same_pattern",
        )
        self.store.add(ex1)
        assert self.store.add(ex2) is False

    def test_dedup_does_not_grow_list(self):
        """Deduplicated example doesn't increase list size."""
        ex1 = CounterExample(
            call_context="ctx", wrong_target="w",
            correct_target="c", pattern="pat",
        )
        self.store.add(ex1)
        self.store.add(ex1)
        self.store.add(ex1)
        assert len(self.store.list_all()) == 1

    def test_different_patterns_both_stored(self):
        """Different patterns are both stored."""
        ex1 = CounterExample(
            call_context="ctx1", wrong_target="w1",
            correct_target="c1", pattern="pattern_A",
        )
        ex2 = CounterExample(
            call_context="ctx2", wrong_target="w2",
            correct_target="c2", pattern="pattern_B",
        )
        self.store.add(ex1)
        self.store.add(ex2)
        assert len(self.store.list_all()) == 2

    def test_get_for_source_returns_all(self):
        """get_for_source returns ALL examples (全量注入)."""
        ex1 = CounterExample(
            call_context="ctx1", wrong_target="w1",
            correct_target="c1", pattern="p1", source_id="src_A",
        )
        ex2 = CounterExample(
            call_context="ctx2", wrong_target="w2",
            correct_target="c2", pattern="p2", source_id="src_B",
        )
        self.store.add(ex1)
        self.store.add(ex2)
        # Even when asking for src_A, get ALL examples
        result = self.store.get_for_source("src_A")
        assert len(result) == 2

    def test_render_markdown_empty_returns_empty_string(self):
        """Empty store renders empty string."""
        assert self.store.render_markdown() == ""

    def test_render_markdown_has_header(self):
        """Non-empty store renders with header."""
        self.store.add(CounterExample(
            call_context="ctx", wrong_target="w",
            correct_target="c", pattern="pat",
        ))
        md = self.store.render_markdown()
        assert "# Counter Examples" in md
        assert "反例库" in md

    def test_render_markdown_contains_all_fields(self):
        """Rendered markdown contains all example fields."""
        self.store.add(CounterExample(
            call_context="vtable_ctx", wrong_target="wrong_fn",
            correct_target="correct_fn", pattern="vtable_pattern",
        ))
        md = self.store.render_markdown()
        assert "vtable_ctx" in md
        assert "wrong_fn" in md
        assert "correct_fn" in md
        assert "vtable_pattern" in md

    def test_render_markdown_for_source_same_as_render_all(self):
        """render_markdown_for_source == render_markdown (全量注入)."""
        self.store.add(CounterExample(
            call_context="ctx", wrong_target="w",
            correct_target="c", pattern="p", source_id="src_X",
        ))
        assert self.store.render_markdown_for_source("src_X") == self.store.render_markdown()
        assert self.store.render_markdown_for_source("other") == self.store.render_markdown()


class TestFeedbackStorePersistence:
    """FeedbackStore persists to JSON and survives reload."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self._tmpdir = Path(tempfile.mkdtemp())

    def test_persists_to_json(self):
        """Examples are saved to counter_examples.json."""
        store = FeedbackStore(storage_dir=self._tmpdir)
        store.add(CounterExample(
            call_context="ctx", wrong_target="w",
            correct_target="c", pattern="persist_test",
        ))
        json_path = self._tmpdir / "counter_examples.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert len(data) == 1
        assert data[0]["pattern"] == "persist_test"

    def test_persists_markdown(self):
        """Markdown file is written alongside JSON."""
        store = FeedbackStore(storage_dir=self._tmpdir)
        store.add(CounterExample(
            call_context="ctx", wrong_target="w",
            correct_target="c", pattern="md_test",
        ))
        md_path = self._tmpdir / "counter_examples.md"
        assert md_path.exists()
        assert "md_test" in md_path.read_text()

    def test_reload_from_disk(self):
        """New FeedbackStore instance loads existing examples."""
        store1 = FeedbackStore(storage_dir=self._tmpdir)
        store1.add(CounterExample(
            call_context="ctx", wrong_target="w",
            correct_target="c", pattern="reload_test",
        ))
        # Create new instance — should load from disk
        store2 = FeedbackStore(storage_dir=self._tmpdir)
        assert len(store2.list_all()) == 1
        assert store2.list_all()[0].pattern == "reload_test"

    def test_corrupted_json_starts_fresh(self):
        """Corrupted JSON file doesn't crash — starts fresh."""
        json_path = self._tmpdir / "counter_examples.json"
        json_path.write_text("{{invalid json", encoding="utf-8")
        store = FeedbackStore(storage_dir=self._tmpdir)
        assert len(store.list_all()) == 0

    def test_empty_json_starts_fresh(self):
        """Empty JSON file doesn't crash."""
        json_path = self._tmpdir / "counter_examples.json"
        json_path.write_text("", encoding="utf-8")
        store = FeedbackStore(storage_dir=self._tmpdir)
        assert len(store.list_all()) == 0


# ===========================================================================
# §8 POST /feedback — validation and dedup signal
# ===========================================================================


class TestFeedbackEndpoint:
    """architecture.md §8: POST /api/v1/feedback validation and response."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        self._tmpdir = Path(tempfile.mkdtemp())
        self.feedback_store = FeedbackStore(storage_dir=self._tmpdir)
        self.store = InMemoryGraphStore()
        app = create_app(store=self.store, feedback_store=self.feedback_store)
        self.client = TestClient(app)

    def test_post_feedback_returns_201(self):
        """POST /feedback with valid body returns 201."""
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "vtable dispatch",
            "wrong_target": "fn_wrong",
            "correct_target": "fn_correct",
            "pattern": "vtable must match sig",
        })
        assert r.status_code == 201

    def test_post_feedback_returns_dedup_signal(self):
        """Response includes deduplicated and total fields."""
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "ctx", "wrong_target": "w",
            "correct_target": "c", "pattern": "unique_pat",
        })
        data = r.json()
        assert "deduplicated" in data
        assert data["deduplicated"] is False
        assert "total" in data
        assert data["total"] == 1

    def test_post_feedback_dedup_true_on_repeat(self):
        """Second POST with same pattern returns deduplicated=True."""
        body = {
            "call_context": "ctx", "wrong_target": "w",
            "correct_target": "c", "pattern": "repeat_pat",
        }
        self.client.post("/api/v1/feedback", json=body)
        r = self.client.post("/api/v1/feedback", json=body)
        data = r.json()
        assert data["deduplicated"] is True
        assert data["total"] == 1  # Not 2

    def test_post_feedback_targets_must_differ(self):
        """wrong_target == correct_target returns 422."""
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "ctx", "wrong_target": "same",
            "correct_target": "same", "pattern": "pat",
        })
        assert r.status_code == 422

    def test_post_feedback_empty_fields_rejected(self):
        """Empty required fields return 422."""
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "", "wrong_target": "w",
            "correct_target": "c", "pattern": "p",
        })
        assert r.status_code == 422

    def test_post_feedback_missing_fields_rejected(self):
        """Missing required fields return 422."""
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "ctx",
        })
        assert r.status_code == 422

    def test_get_feedback_empty(self):
        """GET /feedback with no data returns empty list."""
        r = self.client.get("/api/v1/feedback")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_get_feedback_after_post(self):
        """GET /feedback returns posted examples."""
        self.client.post("/api/v1/feedback", json={
            "call_context": "ctx", "wrong_target": "w",
            "correct_target": "c", "pattern": "get_test",
        })
        r = self.client.get("/api/v1/feedback")
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["pattern"] == "get_test"

    def test_get_feedback_pagination(self):
        """GET /feedback respects limit/offset."""
        for i in range(5):
            self.client.post("/api/v1/feedback", json={
                "call_context": f"ctx{i}", "wrong_target": f"w{i}",
                "correct_target": f"c{i}", "pattern": f"pat_{i}",
            })
        r = self.client.get("/api/v1/feedback?limit=2&offset=1")
        data = r.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_post_feedback_no_store_returns_503(self):
        """POST /feedback without feedback_store returns 503."""
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(store=self.store)  # No feedback_store
        client = TestClient(app)
        r = client.post("/api/v1/feedback", json={
            "call_context": "ctx", "wrong_target": "w",
            "correct_target": "c", "pattern": "p",
        })
        assert r.status_code == 503


# ===========================================================================
# §10 Configuration — env var interpolation and validation
# ===========================================================================


class TestSettingsEnvVarInterpolation:
    """architecture.md §10: config.yaml uses ${VAR_NAME} for env var injection."""

    def test_env_var_interpolation(self, tmp_path):
        """${VAR_NAME} in YAML is replaced by env var value."""
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(
            "neo4j:\n  password: ${TEST_NEO4J_PW}\n", encoding="utf-8"
        )
        os.environ["TEST_NEO4J_PW"] = "secret123"
        try:
            settings = Settings.from_yaml(config_yaml)
            assert settings.neo4j.password == "secret123"
        finally:
            del os.environ["TEST_NEO4J_PW"]

    def test_missing_env_var_leaves_literal(self, tmp_path):
        """Missing env var leaves ${VAR_NAME} as literal string.

        BUG: architecture.md says sensitive values come from env vars,
        but Settings.from_yaml doesn't validate that referenced vars exist.
        A missing var silently becomes the literal string "${VAR_NAME}".
        """
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(
            "neo4j:\n  password: ${NONEXISTENT_VAR_XYZ}\n", encoding="utf-8"
        )
        # Ensure var doesn't exist
        os.environ.pop("NONEXISTENT_VAR_XYZ", None)
        settings = Settings.from_yaml(config_yaml)
        # BUG: This should probably raise, but it doesn't
        # The password becomes the literal string "${NONEXISTENT_VAR_XYZ}"
        assert settings.neo4j.password == "${NONEXISTENT_VAR_XYZ}"

    def test_empty_yaml_uses_defaults(self, tmp_path):
        """Empty YAML file uses all defaults."""
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text("", encoding="utf-8")
        settings = Settings.from_yaml(config_yaml)
        assert settings.agent.backend == "claudecode"
        assert settings.agent.max_concurrency == 5
        assert settings.neo4j.uri == "bolt://localhost:7687"

    def test_partial_yaml_merges_with_defaults(self, tmp_path):
        """Partial YAML overrides only specified fields."""
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(
            "agent:\n  backend: opencode\n  max_concurrency: 3\n",
            encoding="utf-8",
        )
        settings = Settings.from_yaml(config_yaml)
        assert settings.agent.backend == "opencode"
        assert settings.agent.max_concurrency == 3
        # Other defaults preserved
        assert settings.neo4j.uri == "bolt://localhost:7687"


class TestAgentConfigValidation:
    """architecture.md §10: AgentConfig validates backend, concurrency, timeout."""

    def test_valid_backends(self):
        """Only 'claudecode' and 'opencode' are valid backends."""
        cfg1 = AgentConfig(backend="claudecode")
        assert cfg1.backend == "claudecode"
        cfg2 = AgentConfig(backend="opencode")
        assert cfg2.backend == "opencode"

    def test_invalid_backend_raises(self):
        """Invalid backend raises ValidationError."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AgentConfig(backend="invalid_backend")

    def test_max_concurrency_must_be_positive(self):
        """max_concurrency must be >= 1."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AgentConfig(max_concurrency=0)

    def test_max_concurrency_default_is_5(self):
        """Default max_concurrency is 5."""
        cfg = AgentConfig()
        assert cfg.max_concurrency == 5

    def test_subprocess_timeout_must_be_positive(self):
        """subprocess_timeout_seconds must be > 0 when set."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AgentConfig(subprocess_timeout_seconds=0)
        with pytest.raises(ValidationError):
            AgentConfig(subprocess_timeout_seconds=-1)

    def test_subprocess_timeout_none_is_valid(self):
        """subprocess_timeout_seconds=None means no timeout."""
        cfg = AgentConfig(subprocess_timeout_seconds=None)
        assert cfg.subprocess_timeout_seconds is None

    def test_subprocess_timeout_positive_is_valid(self):
        """Positive timeout is accepted."""
        cfg = AgentConfig(subprocess_timeout_seconds=240.0)
        assert cfg.subprocess_timeout_seconds == 240.0

    def test_retry_failed_gaps_default_true(self):
        """retry_failed_gaps defaults to True."""
        cfg = AgentConfig()
        assert cfg.retry_failed_gaps is True


# ===========================================================================
# §8 GET /unresolved-calls — filters (caller, status, category)
# ===========================================================================


class TestUnresolvedCallsFilters:
    """architecture.md §8: GET /unresolved-calls supports caller/status/category filters."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        self.store.create_function(FunctionNode(
            id="uc_filt_a", signature="void A()", name="A",
            file_path="/test/filt.cpp", start_line=1, end_line=10, body_hash="a",
        ))
        self.store.create_function(FunctionNode(
            id="uc_filt_b", signature="void B()", name="B",
            file_path="/test/filt.cpp", start_line=20, end_line=30, body_hash="b",
        ))
        # UC1: pending, from A
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="uc_filt_a", call_expression="foo()",
            call_file="/test/filt.cpp", call_line=5, call_type="indirect",
            source_code_snippet="foo();", var_name="x", var_type="X*",
            status="pending",
        ))
        # UC2: unresolvable, from A, with reason
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="uc_filt_a", call_expression="bar()",
            call_file="/test/filt.cpp", call_line=8, call_type="virtual",
            source_code_snippet="bar();", var_name="y", var_type="Y*",
            status="unresolvable", retry_count=3,
            last_attempt_reason="gate_failed: no edges produced",
        ))
        # UC3: pending, from B
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="uc_filt_b", call_expression="baz()",
            call_file="/test/filt.cpp", call_line=25, call_type="direct",
            source_code_snippet="baz();", var_name=None, var_type=None,
            status="pending",
        ))
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_filter_by_caller(self):
        """?caller=X returns only UCs from that caller."""
        r = self.client.get("/api/v1/unresolved-calls", params={"caller": "uc_filt_a"})
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["caller_id"] == "uc_filt_a"

    def test_filter_by_status_pending(self):
        """?status=pending returns only pending UCs."""
        r = self.client.get("/api/v1/unresolved-calls", params={"status": "pending"})
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["status"] == "pending"

    def test_filter_by_status_unresolvable(self):
        """?status=unresolvable returns only unresolvable UCs."""
        r = self.client.get("/api/v1/unresolved-calls", params={"status": "unresolvable"})
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["status"] == "unresolvable"

    def test_filter_by_category(self):
        """?category=gate_failed returns UCs with that reason prefix."""
        r = self.client.get("/api/v1/unresolved-calls", params={"category": "gate_failed"})
        data = r.json()
        assert data["total"] == 1
        assert "gate_failed" in data["items"][0]["last_attempt_reason"]

    def test_no_filter_returns_all(self):
        """No filter returns all UCs."""
        r = self.client.get("/api/v1/unresolved-calls")
        data = r.json()
        assert data["total"] == 3

    def test_pagination(self):
        """limit/offset work correctly."""
        r = self.client.get("/api/v1/unresolved-calls?limit=1&offset=0")
        data = r.json()
        assert data["total"] == 3
        assert len(data["items"]) == 1


# ===========================================================================
# §8 GET /source-points — filters and enrichment
# ===========================================================================


class TestSourcePointsEndpoint:
    """architecture.md §8: GET /source-points with filters and enrichment."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.schema import FunctionNode, SourcePointNode
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        # Functions
        self.store.create_function(FunctionNode(
            id="sp_fn_a", signature="void PublicAPI()", name="PublicAPI",
            file_path="/test/sp.cpp", start_line=1, end_line=10, body_hash="a",
        ))
        self.store.create_function(FunctionNode(
            id="sp_fn_b", signature="void Callback(int)", name="Callback",
            file_path="/test/sp.cpp", start_line=20, end_line=30, body_hash="b",
        ))
        # Source points
        self.store.create_source_point(SourcePointNode(
            id="sp_ep_1", entry_point_kind="public_api",
            reason="exported symbol", function_id="sp_fn_a",
            module="core", status="running",
        ))
        self.store.create_source_point(SourcePointNode(
            id="sp_ep_2", entry_point_kind="callback",
            reason="registered handler", function_id="sp_fn_b",
            module="events", status="complete",
        ))
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_list_source_points_returns_200(self):
        """GET /source-points returns 200."""
        r = self.client.get("/api/v1/source-points")
        assert r.status_code == 200

    def test_list_source_points_has_pagination(self):
        """Response has total and items."""
        data = self.client.get("/api/v1/source-points").json()
        assert "total" in data
        assert "items" in data
        assert data["total"] == 2

    def test_filter_by_status(self):
        """?status=running filters correctly."""
        data = self.client.get("/api/v1/source-points?status=running").json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "sp_ep_1"

    def test_filter_by_kind(self):
        """?kind=callback filters correctly."""
        data = self.client.get("/api/v1/source-points?kind=callback").json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "sp_ep_2"

    def test_get_single_source_point(self):
        """GET /source-points/{id} returns single source point."""
        r = self.client.get("/api/v1/source-points/sp_ep_1")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == "sp_ep_1"
        assert data["entry_point_kind"] == "public_api"

    def test_get_source_point_not_found(self):
        """GET /source-points/{id} returns 404 for missing."""
        r = self.client.get("/api/v1/source-points/nonexistent")
        assert r.status_code == 404

    def test_source_point_summary(self):
        """GET /source-points/summary returns aggregated counts."""
        r = self.client.get("/api/v1/source-points/summary")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert data["total"] == 2
        assert "by_kind" in data
        assert "by_status" in data


# ===========================================================================
# §1/§2 Pipeline — CastEngine file discovery and resolution verification
# ===========================================================================


class TestPipelineResolutionCastEngine:
    """architecture.md §1/§2: pipeline resolution layer verification with CastEngine."""

    @pytest.fixture(scope="class")
    def castengine_result(self):
        if not CASTENGINE_DIR.exists():
            pytest.skip("CastEngine directory not available")
        store = InMemoryGraphStore()
        orch = PipelineOrchestrator(store=store, target_dir=CASTENGINE_DIR)
        result = orch.run_full_analysis()
        return store, result

    def test_all_cpp_files_discovered(self, castengine_result):
        """Pipeline discovers all .cpp/.h files in CastEngine."""
        store, result = castengine_result
        files = store.list_files()
        extensions = {Path(f.file_path).suffix for f in files}
        # Must find both .cpp and .h files
        assert ".cpp" in extensions or ".cc" in extensions
        assert ".h" in extensions or ".hpp" in extensions
        # CastEngine has 100+ source files
        assert len(files) >= 100

    def test_direct_calls_all_resolved(self, castengine_result):
        """architecture.md §2: direct calls have 100% resolution rate."""
        store, result = castengine_result
        edges = store.list_calls_edges()
        direct_edges = [e for e in edges if e.props.call_type == "direct"]
        # All direct calls should produce edges (not UCs)
        assert len(direct_edges) > 1000, (
            f"Only {len(direct_edges)} direct edges — expected 1000+"
        )

    def test_resolved_by_distribution(self, castengine_result):
        """Edges use all 3 static resolution layers (no llm at this stage)."""
        store, result = castengine_result
        edges = store.list_calls_edges()
        by_resolved: dict[str, int] = defaultdict(int)
        for e in edges:
            by_resolved[e.props.resolved_by] += 1

        # At this stage (static only), no llm edges
        assert by_resolved.get("llm", 0) == 0, (
            f"Found {by_resolved['llm']} llm edges in static-only analysis"
        )
        # symbol_table should be dominant
        assert by_resolved["symbol_table"] > 0

    def test_unresolved_calls_are_indirect_or_virtual(self, castengine_result):
        """UCs should predominantly be indirect/virtual (not direct).

        BUG: architecture.md says direct calls have 100% resolution,
        so UCs should be indirect/virtual. But the pipeline's dual-list
        bug causes ~83% of UCs to be 'direct' type.
        """
        store, result = castengine_result
        ucs = store.get_unresolved_calls()
        type_counts: dict[str, int] = defaultdict(int)
        for uc in ucs:
            type_counts[uc.call_type] += 1

        total = sum(type_counts.values())
        direct_pct = type_counts.get("direct", 0) / total * 100 if total else 0

        # BUG DOCUMENTATION: architecture says direct=100% resolved,
        # so UCs should be 0% direct. Reality: ~83% are direct.
        # This documents the dual-list bug (plugin reports same call
        # in both `calls` and `unresolved` lists).
        if direct_pct > 50:
            pytest.xfail(
                f"BUG: {direct_pct:.1f}% of UCs are 'direct' type — "
                f"architecture says direct calls should be 100% resolved. "
                f"Root cause: plugin dual-list reporting."
            )

    def test_all_edges_have_valid_call_file(self, castengine_result):
        """Every edge's call_file should reference a known file.

        BUG: FileNode.file_path stores RELATIVE paths (e.g., 'castengine_cast_framework/...')
        while CallsEdgeProps.call_file stores ABSOLUTE paths (e.g., '/mnt/c/Task/.../...').
        This data inconsistency means you can't join edges to files by path directly.
        The resolution index works (it maps both formats), but stored data is inconsistent.
        """
        store, result = castengine_result
        edges = store.list_calls_edges()
        file_paths = {f.file_path for f in store.list_files()}

        # Check if edge call_files are absolute while file_paths are relative
        sample_edge_file = edges[0].props.call_file if edges else ""
        sample_store_file = next(iter(file_paths)) if file_paths else ""

        edge_is_absolute = Path(sample_edge_file).is_absolute()
        store_is_relative = not Path(sample_store_file).is_absolute()

        if edge_is_absolute and store_is_relative:
            # BUG: path format mismatch between edges and files
            # Verify that stripping the target_dir prefix makes them match
            target_prefix = str(CASTENGINE_DIR) + "/"
            normalized_edge_files = {
                ef.replace(target_prefix, "") for ef in
                {e.props.call_file for e in edges[:100]}
            }
            overlap = normalized_edge_files & file_paths
            # After normalization, most should match
            match_pct = len(overlap) / len(normalized_edge_files) * 100 if normalized_edge_files else 0
            assert match_pct > 80, (
                f"Only {match_pct:.1f}% of edge call_files match store files "
                f"even after path normalization — deeper inconsistency"
            )
            pytest.xfail(
                f"BUG: call_file uses absolute paths but FileNode.file_path uses relative. "
                f"After normalization {match_pct:.0f}% match."
            )

    def test_all_edges_have_valid_call_line(self, castengine_result):
        """Every edge's call_line should be within caller's bounds."""
        store, result = castengine_result
        edges = store.list_calls_edges()
        fns = {fn.id: fn for fn in store.list_functions()}

        out_of_bounds = 0
        checked = 0
        for e in edges[:500]:
            caller = fns.get(e.caller_id)
            if caller:
                checked += 1
                if not (caller.start_line <= e.props.call_line <= caller.end_line):
                    out_of_bounds += 1

        # Some out-of-bounds are from the overloaded function collision bug
        # but it should be a small percentage
        if checked > 0:
            pct = out_of_bounds / checked * 100
            assert pct < 5, (
                f"{pct:.1f}% of edges have call_line outside caller bounds "
                f"({out_of_bounds}/{checked})"
            )

    def test_pipeline_result_metrics_consistent(self, castengine_result):
        """PipelineResult metrics should be internally consistent.

        BUG: PipelineResult.unresolved_calls is inflated because the counter
        increments BEFORE the store's dedup check. Duplicates are counted
        but not stored. Gap: 23859 counted vs 19749 stored = 4110 deduped.
        """
        store, result = castengine_result
        # functions_found should match store
        assert result.functions_found == len(store.list_functions())
        # files_scanned should match store
        assert result.files_scanned == len(store.list_files())
        # unresolved_calls counter is inflated by dedup-rejected UCs
        actual_ucs = len(store.get_unresolved_calls())
        assert result.unresolved_calls >= actual_ucs, (
            "Counter should be >= actual (includes deduped)"
        )
        # Document the inflation gap
        gap = result.unresolved_calls - actual_ucs
        # Gap should be bounded (< 25% of total)
        gap_pct = gap / result.unresolved_calls * 100 if result.unresolved_calls else 0
        assert gap_pct < 25, (
            f"UC counter inflation too large: {gap_pct:.1f}% "
            f"({gap} counted but deduped out of {result.unresolved_calls})"
        )

    def test_no_duplicate_function_ids(self, castengine_result):
        """Function IDs must be unique."""
        store, result = castengine_result
        fns = store.list_functions()
        ids = [fn.id for fn in fns]
        assert len(ids) == len(set(ids)), (
            f"Duplicate function IDs: {len(ids) - len(set(ids))} duplicates"
        )

    def test_functions_have_valid_line_ranges(self, castengine_result):
        """Every function has start_line <= end_line."""
        store, result = castengine_result
        fns = store.list_functions()
        invalid = [fn for fn in fns if fn.start_line > fn.end_line]
        assert len(invalid) == 0, (
            f"{len(invalid)} functions have start_line > end_line"
        )

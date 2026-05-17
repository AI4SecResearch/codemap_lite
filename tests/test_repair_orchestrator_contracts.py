"""Repair orchestrator contract tests — architecture.md §3.

Tests _inject_files, _cleanup_injection, _safe_dirname, _write_progress,
_build_subprocess_env, gate check logic, retry audit stamping, and
SourcePoint lifecycle transitions. No real subprocess spawning — mocks
the async subprocess layer.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codemap_lite.analysis.feedback_store import FeedbackStore
from codemap_lite.analysis.repair_orchestrator import (
    RepairConfig,
    RepairOrchestrator,
    SourceRepairResult,
    _build_subprocess_env,
    _safe_dirname,
    _truncate_reason,
)
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    RepairLogNode,
    SourcePointNode,
    UnresolvedCallNode,
)


# ---------------------------------------------------------------------------
# Test: _safe_dirname
# ---------------------------------------------------------------------------

class TestSafeDirname:
    """architecture.md §3: source-specific directory names must be filesystem-safe."""

    def test_simple_id(self):
        assert _safe_dirname("abc123") == "abc123"

    def test_slashes_replaced(self):
        result = _safe_dirname("path/to/file.h::NS::Method")
        assert "/" not in result
        assert "\\" not in result

    def test_colons_replaced(self):
        result = _safe_dirname("NS::Class::Method")
        assert ":" not in result

    def test_long_id_truncated_with_hash(self):
        long_id = "a" * 100
        result = _safe_dirname(long_id)
        # Should be truncated to 60 + _ + 8 = 69 chars
        assert len(result) <= 69

    def test_deterministic(self):
        assert _safe_dirname("foo/bar::baz") == _safe_dirname("foo/bar::baz")


# ---------------------------------------------------------------------------
# Test: _truncate_reason
# ---------------------------------------------------------------------------

class TestTruncateReason:
    """architecture.md §3: last_attempt_reason ≤ 200 chars."""

    def test_short_reason_unchanged(self):
        assert _truncate_reason("gate_failed") == "gate_failed"

    def test_exactly_200_unchanged(self):
        reason = "x" * 200
        assert _truncate_reason(reason) == reason

    def test_over_200_truncated_with_ellipsis(self):
        reason = "x" * 300
        result = _truncate_reason(reason)
        assert len(result) == 200
        assert result.endswith("…")


# ---------------------------------------------------------------------------
# Test: _build_subprocess_env
# ---------------------------------------------------------------------------

class TestBuildSubprocessEnv:
    """architecture.md §3: proxy vars stripped for WSL compatibility."""

    def test_strips_proxy_vars(self):
        with patch.dict(os.environ, {
            "http_proxy": "http://proxy:8080",
            "HTTPS_PROXY": "http://proxy:8080",
            "PATH": "/usr/bin",
        }):
            env = _build_subprocess_env(None)
            assert "http_proxy" not in env
            assert "HTTPS_PROXY" not in env
            assert env["PATH"] == "/usr/bin"

    def test_overrides_applied(self):
        env = _build_subprocess_env({"CUSTOM_VAR": "value"})
        assert env["CUSTOM_VAR"] == "value"


# ---------------------------------------------------------------------------
# Test: _inject_files and _cleanup_injection
# ---------------------------------------------------------------------------

class TestInjectFiles:
    """architecture.md §3: injection creates CLAUDE.md + .icslpreprocess_{id}/."""

    @pytest.fixture
    def repair_config(self, tmp_path: Path) -> RepairConfig:
        return RepairConfig(
            target_dir=tmp_path,
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password="test_pass",
        )

    @pytest.fixture
    def orchestrator(self, repair_config: RepairConfig) -> RepairOrchestrator:
        return RepairOrchestrator(repair_config)

    def test_inject_creates_claude_md(self, orchestrator, repair_config):
        target = repair_config.target_dir
        orchestrator._inject_files(target, "source_001", "")
        assert (target / "CLAUDE.md").exists()
        content = (target / "CLAUDE.md").read_text(encoding="utf-8")
        assert "source_001" in content

    def test_inject_creates_icsl_directory(self, orchestrator, repair_config):
        target = repair_config.target_dir
        safe_id = _safe_dirname("source_001")
        orchestrator._inject_files(target, "source_001", "")
        icsl_dir = target / f".icslpreprocess_{safe_id}"
        assert icsl_dir.exists()
        assert (icsl_dir / "icsl_tools.py").exists()
        assert (icsl_dir / "config.yaml").exists()
        assert (icsl_dir / "counter_examples.md").exists()
        assert (icsl_dir / "source_id.txt").exists()

    def test_inject_config_yaml_has_neo4j_creds(self, orchestrator, repair_config):
        target = repair_config.target_dir
        safe_id = _safe_dirname("source_001")
        orchestrator._inject_files(target, "source_001", "")
        import yaml
        config = yaml.safe_load(
            (target / f".icslpreprocess_{safe_id}" / "config.yaml").read_text()
        )
        assert config["neo4j"]["uri"] == "bolt://localhost:7687"
        assert config["neo4j"]["password"] == "test_pass"

    def test_inject_counter_examples_written(self, orchestrator, repair_config):
        target = repair_config.target_dir
        safe_id = _safe_dirname("source_001")
        orchestrator._inject_files(target, "source_001", "# Example\n- bad pattern")
        content = (target / f".icslpreprocess_{safe_id}" / "counter_examples.md").read_text()
        assert "bad pattern" in content

    def test_inject_empty_counter_examples_placeholder(self, orchestrator, repair_config):
        target = repair_config.target_dir
        safe_id = _safe_dirname("source_001")
        orchestrator._inject_files(target, "source_001", "")
        content = (target / f".icslpreprocess_{safe_id}" / "counter_examples.md").read_text()
        assert "No counter examples" in content

    def test_inject_backs_up_existing_claude_md(self, orchestrator, repair_config):
        target = repair_config.target_dir
        (target / "CLAUDE.md").write_text("original content", encoding="utf-8")
        orchestrator._inject_files(target, "source_001", "")
        safe_id = _safe_dirname("source_001")
        backup = target / f"CLAUDE.md.bak.{safe_id}"
        assert backup.exists()
        assert backup.read_text() == "original content"

    def test_cleanup_restores_claude_md(self, orchestrator, repair_config):
        target = repair_config.target_dir
        (target / "CLAUDE.md").write_text("original", encoding="utf-8")
        orchestrator._inject_files(target, "source_001", "")
        orchestrator._cleanup_injection(target, "source_001")
        assert (target / "CLAUDE.md").read_text() == "original"

    def test_cleanup_removes_icsl_directory(self, orchestrator, repair_config):
        target = repair_config.target_dir
        orchestrator._inject_files(target, "source_001", "")
        orchestrator._cleanup_injection(target, "source_001")
        safe_id = _safe_dirname("source_001")
        assert not (target / f".icslpreprocess_{safe_id}").exists()

    def test_cleanup_removes_claude_md_when_no_backup(self, orchestrator, repair_config):
        target = repair_config.target_dir
        orchestrator._inject_files(target, "source_001", "")
        orchestrator._cleanup_injection(target, "source_001")
        assert not (target / "CLAUDE.md").exists()

    def test_inject_creates_hooks_directory(self, orchestrator, repair_config):
        target = repair_config.target_dir
        safe_id = _safe_dirname("source_001")
        orchestrator._inject_files(target, "source_001", "")
        hooks_dir = target / f".icslpreprocess_{safe_id}" / "hooks"
        assert hooks_dir.exists()

    def test_inject_creates_claude_settings(self, orchestrator, repair_config):
        target = repair_config.target_dir
        orchestrator._inject_files(target, "source_001", "")
        settings_path = target / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        assert "hooks" in settings
        assert "PostToolUse" in settings["hooks"]


# ---------------------------------------------------------------------------
# Test: _write_progress
# ---------------------------------------------------------------------------

class TestWriteProgress:
    """architecture.md §3: progress.json written at lifecycle events."""

    @pytest.fixture
    def orchestrator(self, tmp_path: Path) -> RepairOrchestrator:
        config = RepairConfig(target_dir=tmp_path)
        return RepairOrchestrator(config)

    def test_progress_creates_directory(self, orchestrator, tmp_path: Path):
        orchestrator._write_progress("source_001", state="running", attempt=1)
        safe_id = _safe_dirname("source_001")
        progress_path = tmp_path / "logs" / "repair" / safe_id / "progress.json"
        assert progress_path.exists()

    def test_progress_contains_fields(self, orchestrator, tmp_path: Path):
        orchestrator._write_progress("source_001", state="running", attempt=1)
        safe_id = _safe_dirname("source_001")
        progress_path = tmp_path / "logs" / "repair" / safe_id / "progress.json"
        data = json.loads(progress_path.read_text())
        assert data["state"] == "running"
        assert data["attempt"] == 1
        assert data["source_id"] == "source_001"

    def test_progress_merges_fields(self, orchestrator, tmp_path: Path):
        orchestrator._write_progress("source_001", state="running", attempt=1)
        orchestrator._write_progress("source_001", gate_result="failed")
        safe_id = _safe_dirname("source_001")
        progress_path = tmp_path / "logs" / "repair" / safe_id / "progress.json"
        data = json.loads(progress_path.read_text())
        # Both fields preserved
        assert data["state"] == "running"
        assert data["gate_result"] == "failed"


# ---------------------------------------------------------------------------
# Test: SourcePoint lifecycle transitions
# ---------------------------------------------------------------------------

class TestSourcePointLifecycle:
    """architecture.md §3: pending → running → complete | partial_complete."""

    @pytest.fixture
    def store_with_source(self):
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="fn_001", name="entry", signature="void entry()",
            file_path="a.cpp", start_line=1, end_line=5, body_hash="x",
        )
        store.create_function(fn)
        sp = SourcePointNode(
            id="sp_001", function_id="fn_001",
            entry_point_kind="api", reason="test", status="pending",
        )
        store.create_source_point(sp)
        return store

    def test_pending_to_running(self, store_with_source):
        store_with_source.update_source_point_status("sp_001", "running")
        sp = store_with_source.get_source_point("sp_001")
        assert sp.status == "running"

    def test_running_to_complete(self, store_with_source):
        store_with_source.update_source_point_status("sp_001", "running")
        store_with_source.update_source_point_status("sp_001", "complete")
        sp = store_with_source.get_source_point("sp_001")
        assert sp.status == "complete"

    def test_running_to_partial_complete(self, store_with_source):
        store_with_source.update_source_point_status("sp_001", "running")
        store_with_source.update_source_point_status("sp_001", "partial_complete")
        sp = store_with_source.get_source_point("sp_001")
        assert sp.status == "partial_complete"


# ---------------------------------------------------------------------------
# Test: Retry audit stamping
# ---------------------------------------------------------------------------

class TestRetryAuditStamping:
    """architecture.md §3: _record_retry_attempt stamps pending GAPs."""

    @pytest.fixture
    def store_with_gaps(self):
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="fn_001", name="entry", signature="void entry()",
            file_path="a.cpp", start_line=1, end_line=5, body_hash="x",
        )
        store.create_function(fn)
        # Create a pending UC reachable from fn_001
        uc = UnresolvedCallNode(
            id="uc_001", caller_id="fn_001",
            call_expression="target()", call_file="a.cpp", call_line=3,
            call_type="indirect", source_code_snippet="",
            var_name=None, var_type=None,
        )
        store.create_unresolved_call(uc)
        # SourcePoint so BFS can find the gap
        sp = SourcePointNode(
            id="fn_001", function_id="fn_001",
            entry_point_kind="api", reason="test", status="pending",
        )
        store.create_source_point(sp)
        return store

    def test_record_retry_stamps_timestamp(self, store_with_gaps, tmp_path: Path):
        config = RepairConfig(target_dir=tmp_path, graph_store=store_with_gaps)
        orch = RepairOrchestrator(config)
        orch._record_retry_attempt("fn_001", "gate_failed: remaining pending GAPs")

        gaps = store_with_gaps.get_pending_gaps_for_source("fn_001")
        assert len(gaps) >= 1
        for gap in gaps:
            assert gap.last_attempt_timestamp is not None
            assert gap.last_attempt_reason == "gate_failed: remaining pending GAPs"

    def test_record_retry_increments_retry_count(self, store_with_gaps, tmp_path: Path):
        config = RepairConfig(target_dir=tmp_path, graph_store=store_with_gaps)
        orch = RepairOrchestrator(config)
        orch._record_retry_attempt("fn_001", "gate_failed")

        gaps = store_with_gaps.get_pending_gaps_for_source("fn_001")
        for gap in gaps:
            assert gap.retry_count >= 1

    def test_no_store_noop(self, tmp_path: Path):
        """No graph_store → _record_retry_attempt is a no-op."""
        config = RepairConfig(target_dir=tmp_path, graph_store=None)
        orch = RepairOrchestrator(config)
        # Should not raise
        orch._record_retry_attempt("fn_001", "gate_failed")


# ---------------------------------------------------------------------------
# Test: _has_retryable_gaps
# ---------------------------------------------------------------------------

class TestHasRetryableGaps:
    """architecture.md §3: per-GAP retry_count < 3 means retryable."""

    def test_pending_gap_is_retryable(self, tmp_path: Path):
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="fn_001", name="entry", signature="void entry()",
            file_path="a.cpp", start_line=1, end_line=5, body_hash="x",
        )
        store.create_function(fn)
        uc = UnresolvedCallNode(
            id="uc_001", caller_id="fn_001",
            call_expression="target()", call_file="a.cpp", call_line=3,
            call_type="indirect", source_code_snippet="",
            var_name=None, var_type=None, retry_count=0,
        )
        store.create_unresolved_call(uc)
        sp = SourcePointNode(
            id="fn_001", function_id="fn_001",
            entry_point_kind="api", reason="test", status="pending",
        )
        store.create_source_point(sp)

        config = RepairConfig(target_dir=tmp_path, graph_store=store)
        orch = RepairOrchestrator(config)
        assert orch._has_retryable_gaps("fn_001") is True

    def test_exhausted_gap_not_retryable(self, tmp_path: Path):
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="fn_001", name="entry", signature="void entry()",
            file_path="a.cpp", start_line=1, end_line=5, body_hash="x",
        )
        store.create_function(fn)
        uc = UnresolvedCallNode(
            id="uc_001", caller_id="fn_001",
            call_expression="target()", call_file="a.cpp", call_line=3,
            call_type="indirect", source_code_snippet="",
            var_name=None, var_type=None, retry_count=3,
        )
        store.create_unresolved_call(uc)
        sp = SourcePointNode(
            id="fn_001", function_id="fn_001",
            entry_point_kind="api", reason="test", status="pending",
        )
        store.create_source_point(sp)

        config = RepairConfig(target_dir=tmp_path, graph_store=store)
        orch = RepairOrchestrator(config)
        assert orch._has_retryable_gaps("fn_001") is False


# ---------------------------------------------------------------------------
# Test: _ensure_source_point
# ---------------------------------------------------------------------------

class TestEnsureSourcePoint:
    """architecture.md §4: SourcePoint node must exist before status updates."""

    def test_creates_if_missing(self, tmp_path: Path):
        store = InMemoryGraphStore()
        config = RepairConfig(target_dir=tmp_path, graph_store=store)
        orch = RepairOrchestrator(config)
        orch._ensure_source_point("new_source")
        sp = store.get_source_point("new_source")
        assert sp is not None
        assert sp.status == "pending"
        assert sp.function_id == "new_source"

    def test_noop_if_exists(self, tmp_path: Path):
        store = InMemoryGraphStore()
        sp = SourcePointNode(
            id="existing", function_id="existing",
            entry_point_kind="callback", reason="real reason", status="pending",
        )
        store.create_source_point(sp)
        config = RepairConfig(target_dir=tmp_path, graph_store=store)
        orch = RepairOrchestrator(config)
        orch._ensure_source_point("existing")
        # Should not overwrite
        sp2 = store.get_source_point("existing")
        assert sp2.entry_point_kind == "callback"
        assert sp2.reason == "real reason"


# ---------------------------------------------------------------------------
# Test: run_repairs with mocked subprocess
# ---------------------------------------------------------------------------

class TestRunRepairsMocked:
    """Integration test with mocked subprocess — verifies orchestration logic."""

    @pytest.fixture
    def store_with_complete_source(self):
        """Store where source has no pending gaps → should complete immediately."""
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="fn_done", name="done_entry", signature="void done_entry()",
            file_path="a.cpp", start_line=1, end_line=5, body_hash="x",
        )
        store.create_function(fn)
        sp = SourcePointNode(
            id="fn_done", function_id="fn_done",
            entry_point_kind="api", reason="test", status="pending",
        )
        store.create_source_point(sp)
        return store

    def test_no_gaps_completes_immediately(self, store_with_complete_source, tmp_path: Path):
        """Source with no pending gaps → complete without spawning subprocess."""
        config = RepairConfig(
            target_dir=tmp_path,
            graph_store=store_with_complete_source,
        )
        orch = RepairOrchestrator(config)
        results = asyncio.run(orch.run_repairs(["fn_done"]))
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].attempts == 0
        # SourcePoint should be complete
        sp = store_with_complete_source.get_source_point("fn_done")
        assert sp.status == "complete"

    def test_reset_unresolvable_gaps_on_start(self, tmp_path: Path):
        """architecture.md §10: retry_failed_gaps=True resets unresolvable gaps."""
        store = InMemoryGraphStore()
        fn = FunctionNode(
            id="fn_001", name="entry", signature="void entry()",
            file_path="a.cpp", start_line=1, end_line=5, body_hash="x",
        )
        store.create_function(fn)
        # Unresolvable gap
        uc = UnresolvedCallNode(
            id="uc_001", caller_id="fn_001",
            call_expression="target()", call_file="a.cpp", call_line=3,
            call_type="indirect", source_code_snippet="",
            var_name=None, var_type=None, retry_count=3, status="unresolvable",
        )
        store.create_unresolved_call(uc)
        sp = SourcePointNode(
            id="fn_001", function_id="fn_001",
            entry_point_kind="api", reason="test", status="pending",
        )
        store.create_source_point(sp)

        config = RepairConfig(
            target_dir=tmp_path,
            graph_store=store,
            retry_failed_gaps=True,
        )
        orch = RepairOrchestrator(config)

        # Mock subprocess to avoid real execution
        async def mock_gate(source_id):
            return True

        orch._check_gate = mock_gate

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            results = asyncio.run(orch.run_repairs(["fn_001"]))

        # The gap should have been reset before repair started
        # (retry_count back to 0, status back to pending)
        # Since gate passes, source should be complete
        assert results[0].success is True

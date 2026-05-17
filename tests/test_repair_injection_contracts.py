"""Repair Agent injection contracts — architecture.md §3.

Tests CLAUDE.md generation, file injection/cleanup, progress.json schema,
subprocess env isolation, and counter-example injection.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from codemap_lite.agent.claude_md_template import generate_claude_md
from codemap_lite.analysis.feedback_store import CounterExample, FeedbackStore
from codemap_lite.analysis.repair_orchestrator import (
    RepairConfig,
    RepairOrchestrator,
    _build_subprocess_env,
    _safe_dirname,
    _truncate_reason,
)


# ---------------------------------------------------------------------------
# §3: CLAUDE.md content structure
# ---------------------------------------------------------------------------


class TestClaudeMdGeneration:
    """architecture.md §3: CLAUDE.md template must include role, tools,
    counter-examples reference, and termination conditions."""

    def test_contains_role_section(self):
        md = generate_claude_md("src_001")
        assert "## Role" in md
        assert "src_001" in md

    def test_contains_tools_section_with_three_commands(self):
        md = generate_claude_md("src_001")
        assert "## Tools" in md
        assert "query-reachable" in md
        assert "write-edge" in md
        assert "check-complete" in md

    def test_contains_counter_examples_section(self):
        md = generate_claude_md("src_001")
        assert "## Counter Examples" in md or "counter_examples" in md
        assert "MANDATORY" in md

    def test_contains_termination_conditions(self):
        md = generate_claude_md("src_001")
        assert "## Termination Conditions" in md
        # architecture.md §3: 4 termination conditions
        assert "系统库" in md or "system" in md.lower()
        assert "环" in md or "cycle" in md.lower()

    def test_contains_reasoning_capture_section(self):
        """architecture.md §3: --llm-response and --reasoning-summary mandatory."""
        md = generate_claude_md("src_001")
        assert "--llm-response" in md
        assert "--reasoning-summary" in md
        assert "MANDATORY" in md

    def test_icsl_tools_path_uses_source_specific_dir(self):
        """architecture.md §3: source-specific .icslpreprocess_{safe_id}/."""
        md = generate_claude_md("src_001")
        safe = _safe_dirname("src_001")
        assert f".icslpreprocess_{safe}/icsl_tools.py" in md

    def test_custom_paths_override(self):
        md = generate_claude_md(
            "src_001",
            neo4j_config_path="custom/config.yaml",
            counter_examples_path="custom/ce.md",
        )
        assert "custom/config.yaml" in md
        assert "custom/ce.md" in md

    def test_source_id_with_special_chars(self):
        """Source IDs from codewiki_lite may contain :: and /."""
        sid = "module/file.h::NS::Class::Method"
        md = generate_claude_md(sid)
        assert sid in md  # Raw source_id appears in the template


# ---------------------------------------------------------------------------
# §3: File injection and cleanup
# ---------------------------------------------------------------------------


class TestInjectionAndCleanup:
    """architecture.md §3: inject files before agent, cleanup after."""

    @pytest.fixture
    def target_dir(self, tmp_path):
        d = tmp_path / "target"
        d.mkdir()
        return d

    @pytest.fixture
    def orchestrator(self, target_dir, tmp_path):
        config = RepairConfig(
            target_dir=target_dir,
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password="test_pass",
        )
        return RepairOrchestrator(config)

    def test_inject_creates_claude_md(self, orchestrator, target_dir):
        orchestrator._inject_files(target_dir, "src_001", "")
        assert (target_dir / "CLAUDE.md").exists()
        content = (target_dir / "CLAUDE.md").read_text()
        assert "src_001" in content

    def test_inject_creates_source_specific_icsl_dir(self, orchestrator, target_dir):
        safe = _safe_dirname("src_001")
        orchestrator._inject_files(target_dir, "src_001", "")
        icsl_dir = target_dir / f".icslpreprocess_{safe}"
        assert icsl_dir.exists()
        assert (icsl_dir / "icsl_tools.py").exists()
        assert (icsl_dir / "config.yaml").exists()
        assert (icsl_dir / "counter_examples.md").exists()
        assert (icsl_dir / "source_id.txt").exists()

    def test_inject_writes_counter_examples(self, orchestrator, target_dir):
        ce_content = "# Counter Examples\n## 反例 1: pattern\n"
        orchestrator._inject_files(target_dir, "src_001", ce_content)
        safe = _safe_dirname("src_001")
        ce_path = target_dir / f".icslpreprocess_{safe}" / "counter_examples.md"
        assert ce_path.read_text() == ce_content

    def test_inject_empty_counter_examples_gets_stub(self, orchestrator, target_dir):
        orchestrator._inject_files(target_dir, "src_001", "")
        safe = _safe_dirname("src_001")
        ce_path = target_dir / f".icslpreprocess_{safe}" / "counter_examples.md"
        assert "No counter examples yet" in ce_path.read_text()

    def test_inject_creates_hooks_dir(self, orchestrator, target_dir):
        orchestrator._inject_files(target_dir, "src_001", "")
        safe = _safe_dirname("src_001")
        hooks_dir = target_dir / f".icslpreprocess_{safe}" / "hooks"
        assert hooks_dir.exists()

    def test_inject_creates_claude_settings_json(self, orchestrator, target_dir):
        orchestrator._inject_files(target_dir, "src_001", "")
        settings_path = target_dir / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        assert "hooks" in settings
        assert "PostToolUse" in settings["hooks"]
        assert "Notification" in settings["hooks"]

    def test_inject_backs_up_existing_claude_md(self, orchestrator, target_dir):
        original = "# Original CLAUDE.md"
        (target_dir / "CLAUDE.md").write_text(original)
        orchestrator._inject_files(target_dir, "src_001", "")
        safe = _safe_dirname("src_001")
        backup = target_dir / f"CLAUDE.md.bak.{safe}"
        assert backup.exists()
        assert backup.read_text() == original

    def test_cleanup_restores_original_claude_md(self, orchestrator, target_dir):
        original = "# Original CLAUDE.md"
        (target_dir / "CLAUDE.md").write_text(original)
        orchestrator._inject_files(target_dir, "src_001", "")
        orchestrator._cleanup_injection(target_dir, "src_001")
        assert (target_dir / "CLAUDE.md").read_text() == original

    def test_cleanup_removes_claude_md_if_no_backup(self, orchestrator, target_dir):
        orchestrator._inject_files(target_dir, "src_001", "")
        orchestrator._cleanup_injection(target_dir, "src_001")
        assert not (target_dir / "CLAUDE.md").exists()

    def test_cleanup_removes_icsl_dir(self, orchestrator, target_dir):
        orchestrator._inject_files(target_dir, "src_001", "")
        safe = _safe_dirname("src_001")
        orchestrator._cleanup_injection(target_dir, "src_001")
        assert not (target_dir / f".icslpreprocess_{safe}").exists()

    def test_concurrent_sources_use_separate_dirs(self, orchestrator, target_dir):
        """architecture.md §3: source 间并发 — no file collision."""
        orchestrator._inject_files(target_dir, "src_A", "")
        orchestrator._inject_files(target_dir, "src_B", "")
        safe_a = _safe_dirname("src_A")
        safe_b = _safe_dirname("src_B")
        assert (target_dir / f".icslpreprocess_{safe_a}").exists()
        assert (target_dir / f".icslpreprocess_{safe_b}").exists()
        # Each has its own source_id.txt
        assert (target_dir / f".icslpreprocess_{safe_a}" / "source_id.txt").read_text() == "src_A"
        assert (target_dir / f".icslpreprocess_{safe_b}" / "source_id.txt").read_text() == "src_B"

    def test_config_yaml_contains_neo4j_credentials(self, orchestrator, target_dir):
        orchestrator._inject_files(target_dir, "src_001", "")
        safe = _safe_dirname("src_001")
        import yaml
        config = yaml.safe_load(
            (target_dir / f".icslpreprocess_{safe}" / "config.yaml").read_text()
        )
        assert config["neo4j"]["uri"] == "bolt://localhost:7687"
        assert config["neo4j"]["user"] == "neo4j"
        assert config["neo4j"]["password"] == "test_pass"


# ---------------------------------------------------------------------------
# §3: progress.json schema
# ---------------------------------------------------------------------------


class TestProgressJsonSchema:
    """architecture.md §3 + ADR #52: progress.json schema contract."""

    @pytest.fixture
    def target_dir(self, tmp_path):
        d = tmp_path / "target"
        d.mkdir()
        return d

    @pytest.fixture
    def orchestrator(self, target_dir):
        config = RepairConfig(target_dir=target_dir)
        return RepairOrchestrator(config)

    def test_write_progress_creates_file(self, orchestrator, target_dir):
        orchestrator._write_progress("src_001", state="running", attempt=1)
        safe = _safe_dirname("src_001")
        path = target_dir / "logs" / "repair" / safe / "progress.json"
        assert path.exists()

    def test_write_progress_schema_fields(self, orchestrator, target_dir):
        orchestrator._write_progress(
            "src_001",
            state="running",
            attempt=1,
            max_attempts=3,
            gate_result="pending",
            gaps_total=10,
        )
        safe = _safe_dirname("src_001")
        path = target_dir / "logs" / "repair" / safe / "progress.json"
        data = json.loads(path.read_text())
        # Required fields per architecture.md §3
        assert data["source_id"] == "src_001"
        assert data["state"] == "running"
        assert data["attempt"] == 1
        assert data["max_attempts"] == 3
        assert data["gate_result"] == "pending"
        assert data["gaps_total"] == 10

    def test_write_progress_merges_with_existing(self, orchestrator, target_dir):
        """architecture.md §3: Hook-written fields preserved on merge."""
        orchestrator._write_progress("src_001", state="running")
        orchestrator._write_progress("src_001", gaps_fixed=3)
        safe = _safe_dirname("src_001")
        path = target_dir / "logs" / "repair" / safe / "progress.json"
        data = json.loads(path.read_text())
        assert data["state"] == "running"
        assert data["gaps_fixed"] == 3

    def test_write_progress_preserves_source_id(self, orchestrator, target_dir):
        """source_id is always set (even if not in fields)."""
        orchestrator._write_progress("complex/id::NS::Fn", state="running")
        safe = _safe_dirname("complex/id::NS::Fn")
        path = target_dir / "logs" / "repair" / safe / "progress.json"
        data = json.loads(path.read_text())
        assert data["source_id"] == "complex/id::NS::Fn"


# ---------------------------------------------------------------------------
# §3: Subprocess environment isolation
# ---------------------------------------------------------------------------


class TestSubprocessEnv:
    """architecture.md §3: proxy vars stripped for WSL."""

    def test_strips_proxy_vars(self):
        with patch.dict("os.environ", {
            "http_proxy": "http://proxy:8080",
            "HTTPS_PROXY": "http://proxy:8080",
            "all_proxy": "socks5://proxy:1080",
            "HOME": "/home/user",
            "PATH": "/usr/bin",
        }, clear=True):
            env = _build_subprocess_env(None)
            assert "http_proxy" not in env
            assert "HTTPS_PROXY" not in env
            assert "all_proxy" not in env
            assert env["HOME"] == "/home/user"
            assert env["PATH"] == "/usr/bin"

    def test_overrides_merged(self):
        with patch.dict("os.environ", {"HOME": "/home/user"}, clear=True):
            env = _build_subprocess_env({"OPENAI_API_KEY": "sk-test"})
            assert env["OPENAI_API_KEY"] == "sk-test"
            assert env["HOME"] == "/home/user"


# ---------------------------------------------------------------------------
# §3: _safe_dirname edge cases
# ---------------------------------------------------------------------------


class TestSafeDirname:
    """Source IDs from codewiki_lite may contain unsafe chars."""

    def test_slashes_replaced(self):
        assert "/" not in _safe_dirname("module/file.h::NS::Method")

    def test_colons_replaced(self):
        assert ":" not in _safe_dirname("module/file.h::NS::Method")

    def test_long_ids_truncated_with_hash(self):
        long_id = "a" * 100
        safe = _safe_dirname(long_id)
        assert len(safe) <= 69  # 60 + 1 + 8

    def test_short_ids_unchanged_structure(self):
        safe = _safe_dirname("simple_id")
        assert safe == "simple_id"

    def test_uniqueness_for_similar_ids(self):
        """Two IDs that differ only after char 60 get different safe names."""
        base = "a" * 60
        id1 = base + "_variant_1"
        id2 = base + "_variant_2"
        assert _safe_dirname(id1) != _safe_dirname(id2)


# ---------------------------------------------------------------------------
# §3: _truncate_reason
# ---------------------------------------------------------------------------


class TestTruncateReason:
    """architecture.md §3: last_attempt_reason ≤ 200 chars."""

    def test_short_reason_unchanged(self):
        assert _truncate_reason("gate_failed") == "gate_failed"

    def test_exactly_200_unchanged(self):
        r = "x" * 200
        assert _truncate_reason(r) == r

    def test_201_truncated_with_ellipsis(self):
        r = "x" * 201
        result = _truncate_reason(r)
        assert len(result) == 200
        assert result.endswith("…")

    def test_long_reason_truncated(self):
        r = "a" * 500
        result = _truncate_reason(r)
        assert len(result) == 200


# ---------------------------------------------------------------------------
# §3: Counter-example injection into repair agent
# ---------------------------------------------------------------------------


class TestCounterExampleInjection:
    """architecture.md §3 反馈机制: counter-examples injected before each launch."""

    @pytest.fixture
    def target_dir(self, tmp_path):
        d = tmp_path / "target"
        d.mkdir()
        return d

    def test_feedback_store_renders_for_injection(self, tmp_path):
        store = FeedbackStore(storage_dir=tmp_path / "fb")
        store.add(CounterExample(
            call_context="dispatch.cpp:42",
            wrong_target="WrongHandler::handle",
            correct_target="CorrectHandler::handle",
            pattern="vtable dispatch at dispatch.cpp",
            source_id="src_001",
        ))
        md = store.render_markdown_for_source("src_001")
        assert "WrongHandler::handle" in md
        assert "CorrectHandler::handle" in md
        assert "vtable dispatch" in md

    def test_injection_with_real_feedback(self, target_dir, tmp_path):
        fb_store = FeedbackStore(storage_dir=tmp_path / "fb")
        fb_store.add(CounterExample(
            call_context="x.cpp:10",
            wrong_target="bad",
            correct_target="good",
            pattern="test pattern",
            source_id="s1",
        ))
        config = RepairConfig(
            target_dir=target_dir,
            feedback_store=fb_store,
        )
        orch = RepairOrchestrator(config)
        ce_md = fb_store.render_markdown_for_source("src_001")
        orch._inject_files(target_dir, "src_001", ce_md)
        safe = _safe_dirname("src_001")
        ce_path = target_dir / f".icslpreprocess_{safe}" / "counter_examples.md"
        content = ce_path.read_text()
        assert "bad" in content
        assert "good" in content

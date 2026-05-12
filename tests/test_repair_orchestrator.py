"""Tests for Repair Orchestrator — subprocess management + gate checking."""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codemap_lite.analysis.repair_orchestrator import (
    RepairOrchestrator,
    RepairConfig,
    SourceRepairResult,
)


@pytest.fixture
def repair_config(tmp_path):
    return RepairConfig(
        target_dir=tmp_path / "target_code",
        backend="claudecode",
        command="echo",
        args=["done"],
        max_concurrency=2,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
    )


@pytest.fixture
def orchestrator(repair_config):
    return RepairOrchestrator(config=repair_config)


def test_repair_config_creation(repair_config):
    assert repair_config.backend == "claudecode"
    assert repair_config.max_concurrency == 2


def test_orchestrator_creates_injection_files(orchestrator, tmp_path):
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="src_001",
        counter_examples="# No examples yet",
    )

    assert (target_dir / "CLAUDE.md").exists()
    assert (target_dir / ".claude" / "settings.json").exists()
    assert (target_dir / ".icslpreprocess" / "config.yaml").exists()
    assert (target_dir / ".icslpreprocess" / "counter_examples.md").exists()


def test_orchestrator_cleans_injection_files(orchestrator, tmp_path):
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="src_001",
        counter_examples="",
    )
    orchestrator._cleanup_injection(target_dir)

    assert not (target_dir / "CLAUDE.md").exists()
    assert not (target_dir / ".icslpreprocess").exists()


def test_orchestrator_backs_up_existing_claude_md(orchestrator, tmp_path):
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()
    existing_claude_md = target_dir / "CLAUDE.md"
    existing_claude_md.write_text("# Original content")

    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="src_001",
        counter_examples="",
    )

    # Original should be backed up
    backup = target_dir / "CLAUDE.md.bak"
    assert backup.exists()
    assert backup.read_text() == "# Original content"

    # After cleanup, original should be restored
    orchestrator._cleanup_injection(target_dir)
    assert existing_claude_md.exists()
    assert existing_claude_md.read_text() == "# Original content"
    assert not backup.exists()


def test_build_subprocess_command(orchestrator):
    cmd = orchestrator._build_command(source_id="src_001")
    assert "echo" in cmd[0]
    assert "done" in cmd


@pytest.mark.asyncio
async def test_orchestrator_respects_concurrency_limit(repair_config, tmp_path):
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()
    repair_config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        command="sleep",
        args=["0.1"],
        max_concurrency=2,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
    )
    orchestrator = RepairOrchestrator(config=repair_config)

    # Mock gate checker to always pass
    orchestrator._check_gate = AsyncMock(return_value=True)

    source_ids = ["src_001", "src_002", "src_003", "src_004"]
    results = await orchestrator.run_repairs(source_ids)

    # All should complete (gate passes)
    assert len(results) == 4


@pytest.mark.asyncio
async def test_orchestrator_retries_on_gate_failure(repair_config, tmp_path):
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()
    repair_config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        command="echo",
        args=["done"],
        max_concurrency=2,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
    )
    orchestrator = RepairOrchestrator(config=repair_config)

    # Gate fails first 2 times, passes on 3rd
    call_count = {"n": 0}

    async def mock_gate(source_id):
        call_count["n"] += 1
        return call_count["n"] >= 3

    orchestrator._check_gate = mock_gate

    results = await orchestrator.run_repairs(["src_001"])
    assert results[0].attempts == 3
    assert results[0].success is True

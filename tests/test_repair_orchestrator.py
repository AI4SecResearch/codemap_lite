"""Tests for Repair Orchestrator — subprocess management + gate checking."""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codemap_lite.analysis.feedback_store import CounterExample, FeedbackStore
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
    # Closes Known gap #1: icsl_tools.py must ship to the target dir so the
    # agent CLI invocations declared in claude_md_template work end-to-end.
    injected = target_dir / ".icslpreprocess" / "icsl_tools.py"
    assert injected.exists()
    # Sanity-check that what we copied is the real module, not a stub.
    content = injected.read_text(encoding="utf-8")
    assert "def query_reachable" in content
    assert "def write_edge" in content
    assert "def check_complete" in content
    assert "__main__" in content


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


@pytest.mark.asyncio
async def test_orchestrator_injects_feedback_store_counter_examples(tmp_path):
    """Counter examples from FeedbackStore must land in .icslpreprocess/counter_examples.md.

    architecture.md §3 反馈机制 step 4: "更新 counter_examples.md（最新反例库）".
    """
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    store = FeedbackStore(storage_dir=tmp_path / "feedback")
    store.add(
        CounterExample(
            call_context="dispatcher->handle(req)",
            wrong_target="legacy_handler",
            correct_target="modern_handler",
            pattern="dispatcher vtable resolution must prefer modern_handler",
        )
    )

    config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        command="echo",
        args=["done"],
        max_concurrency=1,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        feedback_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)

    captured: dict[str, str] = {}
    orig_inject = orchestrator._inject_files

    def spy(target_dir, source_id, counter_examples):
        captured["ce"] = counter_examples
        orig_inject(
            target_dir=target_dir,
            source_id=source_id,
            counter_examples=counter_examples,
        )
        # Snapshot the written file before _cleanup_injection removes it.
        captured["on_disk"] = (
            target_dir / ".icslpreprocess" / "counter_examples.md"
        ).read_text(encoding="utf-8")

    orchestrator._inject_files = spy  # type: ignore[assignment]
    orchestrator._check_gate = AsyncMock(return_value=True)

    await orchestrator.run_repairs(["src_001"])

    # Rendered markdown passed to _inject_files
    assert "dispatcher->handle(req)" in captured["ce"]
    assert "legacy_handler" in captured["ce"]
    assert "modern_handler" in captured["ce"]
    assert "dispatcher vtable resolution" in captured["ce"]
    # And the same content hit .icslpreprocess/counter_examples.md
    assert "dispatcher->handle(req)" in captured["on_disk"]
    assert "modern_handler" in captured["on_disk"]


@pytest.mark.asyncio
async def test_orchestrator_falls_back_when_feedback_store_missing(tmp_path):
    """No FeedbackStore → counter_examples.md keeps the stub so agent injection
    still succeeds; backwards-compatible with existing RepairConfig callers."""
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        command="echo",
        args=["done"],
        max_concurrency=1,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        # feedback_store deliberately omitted
    )
    orchestrator = RepairOrchestrator(config=config)

    captured: dict[str, str] = {}
    orig_inject = orchestrator._inject_files

    def spy(target_dir, source_id, counter_examples):
        captured["ce"] = counter_examples
        orig_inject(
            target_dir=target_dir,
            source_id=source_id,
            counter_examples=counter_examples,
        )

    orchestrator._inject_files = spy  # type: ignore[assignment]
    orchestrator._check_gate = AsyncMock(return_value=True)

    await orchestrator.run_repairs(["src_001"])

    assert captured["ce"] == ""


@pytest.mark.asyncio
async def test_orchestrator_stamps_retry_audit_on_gate_failure(tmp_path):
    """Failed gate check → pending GAPs on the source must get
    ``last_attempt_timestamp`` + ``last_attempt_reason`` stamped by the
    orchestrator so the frontend GapDetail surfaces "last attempt failed
    at <ts> because <reason>" without reading JSONL logs
    (architecture.md §3 Retry 审计字段).
    """
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    store = InMemoryGraphStore()
    caller = FunctionNode(
        signature="void src_001(int)",
        name="src_001",
        file_path="foo.cpp",
        start_line=1,
        end_line=10,
        body_hash="h",
        id="src_001",
    )
    store.create_function(caller)
    gap = UnresolvedCallNode(
        caller_id="src_001",
        call_expression="fn_ptr(x)",
        call_file="foo.cpp",
        call_line=7,
        call_type="indirect",
        source_code_snippet="fn_ptr(x);",
        var_name="fn_ptr",
        var_type="void (*)(int)",
    )
    store.create_unresolved_call(gap)

    config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        command="echo",
        args=["done"],
        max_concurrency=1,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=False)

    results = await orchestrator.run_repairs(["src_001"])

    assert results[0].success is False
    assert results[0].attempts == 3
    stamped = store._unresolved_calls[gap.id]
    assert stamped.last_attempt_timestamp is not None
    # ISO-8601 UTC string — the orchestrator uses datetime.now(timezone.utc).isoformat()
    assert "T" in stamped.last_attempt_timestamp
    assert stamped.last_attempt_reason is not None
    assert stamped.last_attempt_reason.startswith("gate_failed:")


@pytest.mark.asyncio
async def test_orchestrator_noop_retry_stamp_when_graph_store_missing(tmp_path):
    """Without a graph_store, retry stamping must noop silently so
    existing callers that don't wire Neo4j stay green."""
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        command="echo",
        args=["done"],
        max_concurrency=1,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        # graph_store deliberately omitted
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=False)

    results = await orchestrator.run_repairs(["src_001"])

    # Should not raise and should still run the full retry budget.
    assert results[0].success is False
    assert results[0].attempts == 3


@pytest.mark.asyncio
async def test_orchestrator_handles_subprocess_spawn_failure(tmp_path):
    """Missing CLI binary → each attempt must stamp ``subprocess_crash``
    and retry loop must keep going through the full 3-attempt budget
    (architecture.md §3 Retry 审计字段: 非门禁失败同样记账).

    Before this fix, the FileNotFoundError from asyncio.create_subprocess_exec
    bubbled out of ``_run_single_repair`` on the first attempt — no stamp,
    no retry, the whole source silently died. Now the exception is caught
    per-attempt, stamped with the ``subprocess_crash`` category, and the
    while loop continues so ReviewQueue.GapDetail surfaces the failure.
    """
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    store = InMemoryGraphStore()
    caller = FunctionNode(
        signature="void src_001(int)",
        name="src_001",
        file_path="foo.cpp",
        start_line=1,
        end_line=10,
        body_hash="h",
        id="src_001",
    )
    store.create_function(caller)
    gap = UnresolvedCallNode(
        caller_id="src_001",
        call_expression="fn_ptr(x)",
        call_file="foo.cpp",
        call_line=7,
        call_type="indirect",
        source_code_snippet="fn_ptr(x);",
        var_name="fn_ptr",
        var_type="void (*)(int)",
    )
    store.create_unresolved_call(gap)

    config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        # Intentionally unreachable path so create_subprocess_exec raises
        # FileNotFoundError on every attempt.
        command="/nonexistent-binary-codemap-test-xyz",
        args=["done"],
        max_concurrency=1,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)
    # Gate should never be consulted since spawn fails first; make this
    # explicit so a regression that skips past the except branch fails loud.
    gate_mock = AsyncMock(return_value=True)
    orchestrator._check_gate = gate_mock

    results = await orchestrator.run_repairs(["src_001"])

    assert results[0].success is False
    assert results[0].attempts == 3
    gate_mock.assert_not_called()
    stamped = store._unresolved_calls[gap.id]
    assert stamped.last_attempt_timestamp is not None
    assert stamped.last_attempt_reason is not None
    assert stamped.last_attempt_reason.startswith("subprocess_crash:")
    # ≤200-char cap from architecture.md §3 Retry 审计字段.
    assert len(stamped.last_attempt_reason) <= 200

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


@pytest.mark.asyncio
async def test_orchestrator_stamps_agent_error_on_nonzero_exit(tmp_path):
    """Agent subprocess exits non-zero → stamp ``agent_error: exit <N>``
    and skip the gate check. Before this fix, a crashed-but-spawned agent
    fell through to ``_check_gate`` and got mis-stamped as ``gate_failed``,
    hiding the real root cause from ReviewQueue.GapDetail
    (architecture.md §3 Retry 审计字段: non-gate failures must record the
    matching category, not gate_failed).
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
        # ``sh -c 'exit 7'`` reliably returns non-zero across platforms
        # without depending on a missing binary (which would be the
        # subprocess_crash branch instead).
        command="sh",
        args=["-c", "exit 7"],
        max_concurrency=1,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)
    # Gate must not be consulted — non-zero exit short-circuits before it.
    gate_mock = AsyncMock(return_value=True)
    orchestrator._check_gate = gate_mock

    results = await orchestrator.run_repairs(["src_001"])

    assert results[0].success is False
    assert results[0].attempts == 3
    gate_mock.assert_not_called()
    stamped = store._unresolved_calls[gap.id]
    assert stamped.last_attempt_timestamp is not None
    assert stamped.last_attempt_reason is not None
    assert stamped.last_attempt_reason.startswith("agent_error:")
    assert "exit 7" in stamped.last_attempt_reason
    # Must never be mis-classified as gate_failed (regression guard).
    assert not stamped.last_attempt_reason.startswith("gate_failed:")
    # ≤200-char cap from architecture.md §3 Retry 审计字段.
    assert len(stamped.last_attempt_reason) <= 200


@pytest.mark.asyncio
async def test_orchestrator_stamps_subprocess_timeout_on_hung_agent(tmp_path):
    """Hung agent subprocess → wall-clock timeout must kill the process,
    stamp ``subprocess_timeout: <N>s`` per attempt, and keep the retry loop
    alive through the full 3-attempt budget.

    architecture.md §3 超时护栏 makes this the 4th (and last) audit
    category. Before this guard, a wedged CLI would occupy the source's
    whole retry budget with no UI signal — nothing in GapDetail, nothing
    in Dashboard; the only surface was a stuck progress.json. Now the
    orchestrator enforces wall-clock fairness and the failure lands in
    ReviewQueue as red subprocess_timeout alongside agent_error /
    subprocess_crash / gate_failed (architecture.md §3 Retry 审计字段:
    四档 category 完整落地).
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
        # ``sh -c 'sleep 5; :'`` reliably hangs for 5 s; the prompt
        # appended by _build_command lands as ``$0`` and is ignored.
        # (Plain ``sleep 5 <prompt>`` would error out as an invalid
        # interval, landing in agent_error instead of timeout — exactly
        # the regression this test is guarding against.)
        command="sh",
        args=["-c", "sleep 5; :"],
        max_concurrency=1,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        graph_store=store,
        # Small enough to trigger quickly, large enough to avoid CI flake.
        subprocess_timeout_seconds=0.2,
    )
    orchestrator = RepairOrchestrator(config=config)
    # Gate must not be consulted — timeout short-circuits before it.
    gate_mock = AsyncMock(return_value=True)
    orchestrator._check_gate = gate_mock

    results = await orchestrator.run_repairs(["src_001"])

    assert results[0].success is False
    assert results[0].attempts == 3
    gate_mock.assert_not_called()
    stamped = store._unresolved_calls[gap.id]
    assert stamped.last_attempt_timestamp is not None
    assert stamped.last_attempt_reason is not None
    assert stamped.last_attempt_reason.startswith("subprocess_timeout:")
    assert "0.2s" in stamped.last_attempt_reason
    # Must never collapse into neighbouring categories (regression guard).
    assert not stamped.last_attempt_reason.startswith("gate_failed:")
    assert not stamped.last_attempt_reason.startswith("agent_error:")
    assert not stamped.last_attempt_reason.startswith("subprocess_crash:")
    # ≤200-char cap from architecture.md §3 Retry 审计字段.
    assert len(stamped.last_attempt_reason) <= 200


@pytest.mark.asyncio
async def test_orchestrator_no_timeout_when_not_configured(tmp_path):
    """Backwards compatibility: without ``subprocess_timeout_seconds``,
    proc.communicate() must run without asyncio.wait_for so existing
    callers preserve the ``不限时，Agent 自然完成`` contract from
    architecture.md §3.
    """
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
        # subprocess_timeout_seconds deliberately omitted (default None)
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=True)

    results = await orchestrator.run_repairs(["src_001"])

    assert results[0].success is True
    assert results[0].attempts == 1


# ---- _check_gate subprocess wiring (architecture.md §3 门禁机制) -------------


@pytest.mark.asyncio
async def test_check_gate_invokes_icsl_tools_check_complete_subprocess(orchestrator):
    """architecture.md §3 门禁机制: _check_gate must subprocess-exec
    ``python .icslpreprocess/icsl_tools.py check-complete --source <id>``
    in target_dir and parse ``{"complete": bool}`` from stdout.
    """
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(
            b'{"complete": true, "remaining_gaps": 0, "pending_gap_ids": []}\n',
            b"",
        )
    )

    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)
    ) as spawn:
        passed = await orchestrator._check_gate("src_001")

    assert passed is True
    cmd_args = spawn.call_args.args
    assert "check-complete" in cmd_args
    assert "--source" in cmd_args
    assert "src_001" in cmd_args
    # Subprocess must run in target_dir so .icslpreprocess/ resolves.
    assert spawn.call_args.kwargs["cwd"] == str(orchestrator._config.target_dir)


@pytest.mark.asyncio
async def test_check_gate_returns_false_when_complete_is_false(orchestrator):
    """architecture.md §3 门禁机制: pending GAPs ⇒ gate fails ⇒ retry."""
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(
            b'{"complete": false, "remaining_gaps": 2, "pending_gap_ids": ["g1","g2"]}\n',
            b"",
        )
    )
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        passed = await orchestrator._check_gate("src_001")

    assert passed is False


@pytest.mark.asyncio
async def test_check_gate_returns_false_on_nonzero_exit(orchestrator):
    """Subprocess crash / config error must not silently pass the gate.

    architecture.md §3 Retry 审计字段 expects gate failures to keep the
    retry budget moving — a non-zero exit cannot be misread as success.
    """
    fake_proc = MagicMock()
    fake_proc.returncode = 3
    fake_proc.communicate = AsyncMock(
        return_value=(b'{"error":"store_not_available"}\n', b"")
    )
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        passed = await orchestrator._check_gate("src_001")

    assert passed is False


@pytest.mark.asyncio
async def test_check_gate_returns_false_on_malformed_json(orchestrator):
    """Malformed CLI output ⇒ gate fails (better safe than mis-pass)."""
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"not json at all", b""))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        passed = await orchestrator._check_gate("src_001")

    assert passed is False


@pytest.mark.asyncio
async def test_check_gate_returns_false_on_spawn_failure(orchestrator):
    """Missing python interpreter / icsl_tools.py file ⇒ gate fails."""
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("no python")),
    ):
        passed = await orchestrator._check_gate("src_001")

    assert passed is False


# ---- progress.json writing (architecture.md §3 + ADR #52) --------------------


@pytest.mark.asyncio
async def test_orchestrator_writes_progress_json(tmp_path):
    """Orchestrator must write progress.json at key lifecycle events so
    the frontend can poll /api/v1/analyze/status and show per-source
    state, attempt count, gate result, and edges written
    (architecture.md §3 进度通信机制 + ADR #52).
    """
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
    )
    orchestrator = RepairOrchestrator(config=config)
    # Gate always fails → 3 attempts → final state="failed"
    orchestrator._check_gate = AsyncMock(return_value=False)

    results = await orchestrator.run_repairs(["src_progress"])

    assert results[0].success is False
    assert results[0].attempts == 3

    # Verify progress.json was written
    progress_path = target_dir / "logs" / "repair" / "src_progress" / "progress.json"
    assert progress_path.exists()
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    assert data["state"] == "failed"
    assert data["attempt"] == 3
    assert data["max_attempts"] == 3
    assert data["gate_result"] == "failed"
    assert "last_error" in data
    assert "gate_failed" in data["last_error"]


@pytest.mark.asyncio
async def test_orchestrator_progress_shows_succeeded_on_gate_pass(tmp_path):
    """When gate passes on first attempt, progress.json must show
    state=succeeded and gate_result=passed."""
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
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=True)

    results = await orchestrator.run_repairs(["src_pass"])

    assert results[0].success is True
    progress_path = target_dir / "logs" / "repair" / "src_pass" / "progress.json"
    assert progress_path.exists()
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    assert data["state"] == "succeeded"
    assert data["gate_result"] == "passed"
    assert data["attempt"] == 1


def test_inject_files_copies_hooks_and_source_id(orchestrator, tmp_path):
    """Bug #1/#3: hooks must be copied to .icslpreprocess/hooks/ and
    source_id.txt must exist so hook scripts can identify the source."""
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="src_hook_test",
        counter_examples="",
    )

    hooks_dir = target_dir / ".icslpreprocess" / "hooks"
    assert hooks_dir.is_dir()
    assert (hooks_dir / "log_notification.py").exists()
    assert (hooks_dir / "log_tool_use.py").exists()

    # Verify hook scripts have __main__ entry points (Bug #7)
    for hook_file in ("log_notification.py", "log_tool_use.py"):
        content = (hooks_dir / hook_file).read_text(encoding="utf-8")
        assert '__name__' in content and '__main__' in content, (
            f"{hook_file} missing __main__ entry point"
        )

    # source_id.txt must contain the source_id (Bug #3)
    sid_path = target_dir / ".icslpreprocess" / "source_id.txt"
    assert sid_path.exists()
    assert sid_path.read_text(encoding="utf-8") == "src_hook_test"

    # .claude/settings.json must reference the hook paths
    settings_path = target_dir / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "hooks" in settings
    assert any(
        "log_tool_use.py" in h.get("command", "")
        for h in settings["hooks"].get("PostToolUse", [])
    )
    assert any(
        "log_notification.py" in h.get("command", "")
        for h in settings["hooks"].get("Notification", [])
    )


# ---------------------------------------------------------------------------
# Architecture §3 full lifecycle: retry_count → unresolvable transition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_stamps_audit_fields_on_graph_store(tmp_path):
    """architecture.md §3 Retry 审计字段: after gate failure, orchestrator
    stamps last_attempt_timestamp + last_attempt_reason on each pending GAP
    reachable from the source. retry_count increments each time."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import (
        FunctionNode,
        CallsEdgeProps,
        UnresolvedCallNode,
    )

    store = InMemoryGraphStore()
    # Create a source function and a GAP
    store.create_function(FunctionNode(
        id="src_a", name="entry", signature="void entry()",
        file_path="a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="callee_b", name="target", signature="void target()",
        file_path="b.cpp", start_line=1, end_line=5, body_hash="h2",
    ))
    # Edge from src_a → callee_b so BFS reaches callee_b
    store.create_calls_edge("src_a", "callee_b", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="a.cpp", call_line=5,
    ))
    # GAP on src_a
    gap = UnresolvedCallNode(
        caller_id="src_a",
        call_expression="fn_ptr(x)",
        call_file="a.cpp",
        call_line=7,
        call_type="indirect",
        source_code_snippet="fn_ptr(x);",
        var_name="fn_ptr",
        var_type="void (*)(int)",
    )
    store.create_unresolved_call(gap)

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
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)
    # Gate always fails → 3 retries → GAP becomes unresolvable
    orchestrator._check_gate = AsyncMock(return_value=False)

    results = await orchestrator.run_repairs(["src_a"])

    assert results[0].success is False
    assert results[0].attempts == 3

    # Verify retry_count was incremented to 3 and status is unresolvable
    updated_gap = store._unresolved_calls[gap.id]
    assert updated_gap.retry_count == 3
    assert updated_gap.status == "unresolvable"
    assert updated_gap.last_attempt_reason == "gate_failed: remaining pending GAPs"
    assert updated_gap.last_attempt_timestamp is not None


@pytest.mark.asyncio
async def test_gate_pass_does_not_stamp_retry(tmp_path):
    """architecture.md §3: when gate passes on first attempt, no retry
    audit fields should be stamped and retry_count stays at 0."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import (
        FunctionNode,
        CallsEdgeProps,
        UnresolvedCallNode,
    )

    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="src_x", name="entry", signature="void entry()",
        file_path="x.cpp", start_line=1, end_line=10, body_hash="hx",
    ))
    gap = UnresolvedCallNode(
        caller_id="src_x",
        call_expression="cb()",
        call_file="x.cpp",
        call_line=5,
        call_type="indirect",
        source_code_snippet="cb();",
        var_name="cb",
        var_type="void (*)()",
    )
    store.create_unresolved_call(gap)

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
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=True)

    results = await orchestrator.run_repairs(["src_x"])

    assert results[0].success is True
    assert results[0].attempts == 1

    # GAP should NOT have been stamped
    updated_gap = store._unresolved_calls[gap.id]
    assert updated_gap.retry_count == 0
    assert updated_gap.last_attempt_timestamp is None


@pytest.mark.asyncio
async def test_subprocess_timeout_stamps_correct_category(tmp_path):
    """architecture.md §3 超时护栏: subprocess_timeout stamps
    'subprocess_timeout: <N>s' as the reason category."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="src_t", name="entry", signature="void entry()",
        file_path="t.cpp", start_line=1, end_line=10, body_hash="ht",
    ))
    gap = UnresolvedCallNode(
        caller_id="src_t",
        call_expression="slow()",
        call_file="t.cpp",
        call_line=3,
        call_type="indirect",
        source_code_snippet="slow();",
        var_name="slow",
        var_type="void (*)()",
    )
    store.create_unresolved_call(gap)

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        command="python",
        args=["-c", "import time; time.sleep(60)"],  # Will be killed by timeout
        max_concurrency=1,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        subprocess_timeout_seconds=0.2,
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)

    results = await orchestrator.run_repairs(["src_t"])

    assert results[0].success is False
    # All 3 attempts should have timed out
    updated_gap = store._unresolved_calls[gap.id]
    assert updated_gap.retry_count == 3
    assert updated_gap.status == "unresolvable"
    assert "subprocess_timeout" in (updated_gap.last_attempt_reason or "")


@pytest.mark.asyncio
async def test_check_gate_real_subprocess_returns_false_on_neo4j_error(tmp_path):
    """Integration: _check_gate spawns real subprocess with injected files.
    When Neo4j is unreachable, check-complete exits non-zero → gate returns False.
    This validates the full orchestrator→subprocess→icsl_tools path."""
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        command="echo",
        args=["done"],
        max_concurrency=1,
        neo4j_uri="bolt://localhost:99999",  # Unreachable
        neo4j_user="neo4j",
        neo4j_password="bad",
    )
    orchestrator = RepairOrchestrator(config=config)

    # Inject files so .icslpreprocess/icsl_tools.py exists
    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="src_gate_test",
        counter_examples="",
    )

    # Call the real _check_gate (not mocked) — it spawns a subprocess
    result = await orchestrator._check_gate("src_gate_test")

    # Should return False because Neo4j connection fails
    assert result is False


@pytest.mark.asyncio
async def test_check_gate_returns_false_when_icsl_tools_missing(tmp_path):
    """_check_gate must return False (not crash) when .icslpreprocess/
    icsl_tools.py doesn't exist — e.g. if cleanup ran early."""
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
    )
    orchestrator = RepairOrchestrator(config=config)

    # Don't inject files — icsl_tools.py won't exist
    result = await orchestrator._check_gate("src_missing")
    assert result is False


@pytest.mark.asyncio
async def test_retry_stamps_all_pending_gaps_independently(tmp_path):
    """architecture.md §3 Retry 审计字段: when gate fails, ALL pending
    GAPs reachable from the source must have retry_count incremented
    independently. Each GAP tracks its own retry budget."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="src_multi", name="entry", signature="void entry()",
        file_path="m.cpp", start_line=1, end_line=20, body_hash="hm",
    ))
    # Two distinct GAPs for the same source
    gap_a = UnresolvedCallNode(
        caller_id="src_multi",
        call_expression="fp1()",
        call_file="m.cpp",
        call_line=5,
        call_type="indirect",
        source_code_snippet="fp1();",
        var_name="fp1",
        var_type="void (*)()",
    )
    gap_b = UnresolvedCallNode(
        caller_id="src_multi",
        call_expression="fp2()",
        call_file="m.cpp",
        call_line=10,
        call_type="indirect",
        source_code_snippet="fp2();",
        var_name="fp2",
        var_type="void (*)()",
    )
    store.create_unresolved_call(gap_a)
    store.create_unresolved_call(gap_b)

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
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=False)

    results = await orchestrator.run_repairs(["src_multi"])

    assert results[0].success is False
    # Both GAPs must have been stamped 3 times (max retries)
    updated_a = store._unresolved_calls[gap_a.id]
    updated_b = store._unresolved_calls[gap_b.id]
    assert updated_a.retry_count == 3
    assert updated_a.status == "unresolvable"
    assert updated_b.retry_count == 3
    assert updated_b.status == "unresolvable"
    # Both must have timestamps
    assert updated_a.last_attempt_timestamp is not None
    assert updated_b.last_attempt_timestamp is not None


@pytest.mark.asyncio
async def test_retry_reason_format_matches_category_prefix(tmp_path):
    """architecture.md §3 Retry 审计字段: last_attempt_reason must follow
    format '<category>: <summary>' where category ∈ {gate_failed,
    agent_error, subprocess_crash, subprocess_timeout}."""
    import re

    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    VALID_CATEGORIES = {
        "gate_failed", "agent_error", "subprocess_crash", "subprocess_timeout"
    }
    reason_pattern = re.compile(
        r"^(gate_failed|agent_error|subprocess_crash|subprocess_timeout): .+$"
    )

    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="src_fmt", name="entry", signature="void entry()",
        file_path="f.cpp", start_line=1, end_line=10, body_hash="hf",
    ))
    gap = UnresolvedCallNode(
        caller_id="src_fmt",
        call_expression="x()",
        call_file="f.cpp",
        call_line=3,
        call_type="indirect",
        source_code_snippet="x();",
        var_name="x",
        var_type="void (*)()",
    )
    store.create_unresolved_call(gap)

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    # Test gate_failed category
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

    await orchestrator.run_repairs(["src_fmt"])

    updated = store._unresolved_calls[gap.id]
    reason = updated.last_attempt_reason
    assert reason is not None, "reason must be set after gate failure"
    assert reason_pattern.match(reason), (
        f"reason '{reason}' does not match '<category>: <summary>' format"
    )
    # Verify category is one of the valid ones
    category = reason.split(":")[0]
    assert category in VALID_CATEGORIES, f"unknown category: {category}"


@pytest.mark.asyncio
async def test_retry_audit_timestamp_is_iso8601_utc(tmp_path):
    """architecture.md §3 line 116: last_attempt_timestamp must be
    ISO-8601 UTC string."""
    import re

    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="src_ts", name="entry", signature="void entry()",
        file_path="ts.cpp", start_line=1, end_line=10, body_hash="hts",
    ))
    gap = UnresolvedCallNode(
        caller_id="src_ts",
        call_expression="y()",
        call_file="ts.cpp",
        call_line=3,
        call_type="indirect",
        source_code_snippet="y();",
        var_name="y",
        var_type="void (*)()",
    )
    store.create_unresolved_call(gap)

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
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=False)

    await orchestrator.run_repairs(["src_ts"])

    updated = store._unresolved_calls[gap.id]
    ts = updated.last_attempt_timestamp
    assert ts is not None
    # ISO-8601 with UTC offset
    iso_pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    assert re.match(iso_pattern, ts), f"not ISO-8601: {ts}"
    assert "+00:00" in ts or ts.endswith("Z"), f"not UTC: {ts}"

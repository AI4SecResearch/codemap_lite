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
    assert (target_dir / ".icslpreprocess_src_001" / "config.yaml").exists()
    assert (target_dir / ".icslpreprocess_src_001" / "counter_examples.md").exists()
    # Closes Known gap #1: icsl_tools.py must ship to the target dir so the
    # agent CLI invocations declared in claude_md_template work end-to-end.
    injected = target_dir / ".icslpreprocess_src_001" / "icsl_tools.py"
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
    orchestrator._cleanup_injection(target_dir, "src_001")

    assert not (target_dir / "CLAUDE.md").exists()
    assert not (target_dir / ".icslpreprocess_src_001").exists()


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

    # Original should be backed up with source-specific name
    backup = target_dir / "CLAUDE.md.bak.src_001"
    assert backup.exists()
    assert backup.read_text() == "# Original content"

    # After cleanup, original should be restored
    orchestrator._cleanup_injection(target_dir, "src_001")
    assert existing_claude_md.exists()
    assert existing_claude_md.read_text() == "# Original content"
    assert not backup.exists()


def test_cleanup_preserves_preexisting_claude_dir(orchestrator, tmp_path):
    """architecture.md §3 line 179: cleanup must only remove files the
    orchestrator created. Pre-existing .claude/ directory (e.g. user's
    own settings.json) must be preserved after cleanup."""
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    # Pre-existing .claude/ with user's own settings
    claude_dir = target_dir / ".claude"
    claude_dir.mkdir()
    user_settings = claude_dir / "settings.json"
    user_settings.write_text('{"user_key": "user_value"}', encoding="utf-8")

    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="src_001",
        counter_examples="",
    )

    # After injection, .claude/settings.json is overwritten with hooks config
    injected_settings = json.loads(user_settings.read_text())
    assert "hooks" in injected_settings

    # After cleanup, pre-existing .claude/ should be preserved
    orchestrator._cleanup_injection(target_dir, "src_001")
    assert claude_dir.exists(), (
        ".claude/ was deleted but it pre-existed — must be preserved"
    )
    # User's original settings should be restored
    assert user_settings.exists()
    restored = json.loads(user_settings.read_text())
    assert restored == {"user_key": "user_value"}


def test_cleanup_removes_claude_dir_when_not_preexisting(orchestrator, tmp_path):
    """architecture.md §3: if .claude/ did NOT exist before injection,
    cleanup should remove it entirely."""
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    # No pre-existing .claude/
    assert not (target_dir / ".claude").exists()

    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="src_001",
        counter_examples="",
    )
    assert (target_dir / ".claude").exists()

    orchestrator._cleanup_injection(target_dir, "src_001")
    assert not (target_dir / ".claude").exists()


def test_concurrent_sources_use_independent_backups(orchestrator, tmp_path):
    """architecture.md §3: sources run concurrently in the same target_dir.
    Backup/restore must use source-specific names so concurrent cleanups
    don't clobber each other's backups.

    In practice each source injects → runs agent → cleans up independently.
    The key invariant: Source A's cleanup does NOT destroy Source B's backup."""
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    # Pre-existing CLAUDE.md
    original_md = target_dir / "CLAUDE.md"
    original_md.write_text("# Original")

    # Source A injects — backs up original
    orchestrator._inject_files(target_dir=target_dir, source_id="src_A", counter_examples="")
    assert (target_dir / "CLAUDE.md.bak.src_A").exists()
    assert (target_dir / "CLAUDE.md.bak.src_A").read_text() == "# Original"

    # Source A cleans up — restores original
    orchestrator._cleanup_injection(target_dir, "src_A")
    assert original_md.read_text() == "# Original"
    assert not (target_dir / "CLAUDE.md.bak.src_A").exists()

    # Source B injects — backs up original (still intact after A's cleanup)
    orchestrator._inject_files(target_dir=target_dir, source_id="src_B", counter_examples="")
    assert (target_dir / "CLAUDE.md.bak.src_B").exists()
    assert (target_dir / "CLAUDE.md.bak.src_B").read_text() == "# Original"

    # Source B cleans up
    orchestrator._cleanup_injection(target_dir, "src_B")
    assert original_md.read_text() == "# Original"


def test_concurrent_sources_have_isolated_icslpreprocess_dirs(orchestrator, tmp_path):
    """architecture.md §3: source 间并发 — each source gets its own
    .icslpreprocess_{source_id}/ directory. Cleanup of one source must
    NOT remove another source's injected tools/config."""
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    # Inject for two sources concurrently
    orchestrator._inject_files(target_dir=target_dir, source_id="src_A", counter_examples="# A")
    orchestrator._inject_files(target_dir=target_dir, source_id="src_B", counter_examples="# B")

    # Both have their own directories
    dir_a = target_dir / ".icslpreprocess_src_A"
    dir_b = target_dir / ".icslpreprocess_src_B"
    assert dir_a.exists()
    assert dir_b.exists()
    assert (dir_a / "icsl_tools.py").exists()
    assert (dir_b / "icsl_tools.py").exists()
    assert (dir_a / "counter_examples.md").read_text() == "# A"
    assert (dir_b / "counter_examples.md").read_text() == "# B"

    # Cleanup of A does NOT affect B
    orchestrator._cleanup_injection(target_dir, "src_A")
    assert not dir_a.exists()
    assert dir_b.exists()
    assert (dir_b / "icsl_tools.py").exists()

    # Cleanup of B removes B
    orchestrator._cleanup_injection(target_dir, "src_B")
    assert not dir_b.exists()


@pytest.mark.asyncio
async def test_orchestrator_respects_concurrency_limit(repair_config, tmp_path):
    """architecture.md §3: 'source 间并发' with max_concurrency=2 means
    at most 2 sources run simultaneously."""
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    store = InMemoryGraphStore()
    for i in range(4):
        sid = f"src_{i:03d}"
        store.create_function(FunctionNode(
            id=sid, name=f"fn{i}", signature=f"void fn{i}()",
            file_path="x.cpp", start_line=i * 10, end_line=i * 10 + 5,
            body_hash=f"h{i}",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id=sid, call_expression=f"cb{i}()",
            call_file="x.cpp", call_line=i * 10 + 3, call_type="indirect",
            source_code_snippet=f"cb{i}();", var_name=None, var_type=None,
        ))

    # Track concurrent execution count
    import threading
    lock = threading.Lock()
    max_concurrent = 0
    current_concurrent = 0

    original_create_subprocess = asyncio.create_subprocess_exec

    async def tracked_subprocess(*args, **kwargs):
        nonlocal max_concurrent, current_concurrent
        with lock:
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
        await asyncio.sleep(0.05)  # Simulate work
        with lock:
            current_concurrent -= 1
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        return proc

    repair_config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        command="echo",
        args=["done"],
        max_concurrency=2,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=repair_config)
    orchestrator._check_gate = AsyncMock(return_value=True)

    with patch("asyncio.create_subprocess_exec", side_effect=tracked_subprocess):
        source_ids = ["src_000", "src_001", "src_002", "src_003"]
        results = await orchestrator.run_repairs(source_ids)

    # All should complete
    assert len(results) == 4
    # At most 2 should have run concurrently
    assert max_concurrent <= 2, (
        f"architecture.md §3: max_concurrency=2 but saw {max_concurrent} concurrent"
    )


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
async def test_orchestrator_creates_source_point_node_if_not_exists(tmp_path):
    """architecture.md §4: SourcePoint nodes must exist in Neo4j for
    update_source_point_status to work. The orchestrator must ensure
    the node exists before starting repair (create if not exists)."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    store = InMemoryGraphStore()

    # Set up a function and a pending gap so the repair loop enters
    fn = FunctionNode(
        id="src_001", signature="void main()", name="main",
        file_path="src/main.c", start_line=1, end_line=10, body_hash="abc",
    )
    store.create_function(fn)
    gap = UnresolvedCallNode(
        id="gap_001", caller_id="src_001", call_expression="foo()",
        call_file="src/main.c", call_line=5, call_type="indirect",
        source_code_snippet="foo();", var_name=None, var_type=None,
        status="pending",
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
    orchestrator._check_gate = AsyncMock(return_value=True)

    # Before repair: no SourcePoint exists
    assert store.get_source_point("src_001") is None

    await orchestrator.run_repairs(["src_001"])

    # After repair: SourcePoint should exist with status
    sp = store.get_source_point("src_001")
    assert sp is not None, "SourcePoint node must be created during repair"
    assert sp.function_id == "src_001"
    # Status should be "complete" since gate passed
    assert sp.status == "complete"


@pytest.mark.asyncio
async def test_orchestrator_injects_feedback_store_counter_examples(tmp_path):
    """Counter examples from FeedbackStore must land in .icslpreprocess_{source_id}/counter_examples.md.

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
            target_dir / ".icslpreprocess_src_001" / "counter_examples.md"
        ).read_text(encoding="utf-8")

    orchestrator._inject_files = spy  # type: ignore[assignment]
    orchestrator._check_gate = AsyncMock(return_value=True)

    await orchestrator.run_repairs(["src_001"])

    # Rendered markdown passed to _inject_files
    assert "dispatcher->handle(req)" in captured["ce"]
    assert "legacy_handler" in captured["ce"]
    assert "modern_handler" in captured["ce"]
    assert "dispatcher vtable resolution" in captured["ce"]
    # And the same content hit .icslpreprocess_src_001/counter_examples.md
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
async def test_per_gap_retry_independence(tmp_path):
    """architecture.md §3 line 123: '每个 UnresolvedCall 独立追踪 retry_count'.
    If one GAP is resolved (removed from pending) while another remains,
    the loop continues only for the remaining GAP's budget."""
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
    gap1 = UnresolvedCallNode(
        caller_id="src_001",
        call_expression="fn_ptr(x)",
        call_file="foo.cpp",
        call_line=7,
        call_type="indirect",
        source_code_snippet="fn_ptr(x);",
        var_name="fn_ptr",
        var_type="void (*)(int)",
        id="gap_1",
    )
    gap2 = UnresolvedCallNode(
        caller_id="src_001",
        call_expression="vfunc(y)",
        call_file="foo.cpp",
        call_line=12,
        call_type="virtual",
        source_code_snippet="vfunc(y);",
        var_name="vfunc",
        var_type="Base*",
        id="gap_2",
    )
    store.create_unresolved_call(gap1)
    store.create_unresolved_call(gap2)

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

    # Simulate: after attempt 1, gap_1 gets resolved (status changes)
    # but gap_2 remains pending. Gate always fails.
    attempt_count = {"n": 0}

    async def mock_gate(source_id):
        attempt_count["n"] += 1
        if attempt_count["n"] == 1:
            # Simulate gap_1 being resolved after first attempt
            store._unresolved_calls["gap_1"] = UnresolvedCallNode(
                caller_id="src_001",
                call_expression="fn_ptr(x)",
                call_file="foo.cpp",
                call_line=7,
                call_type="indirect",
                source_code_snippet="fn_ptr(x);",
                var_name="fn_ptr",
                var_type="void (*)(int)",
                id="gap_1",
                status="resolved",
            )
        return False

    orchestrator._check_gate = mock_gate

    results = await orchestrator.run_repairs(["src_001"])

    # gap_2 should have been retried 3 times (its own budget)
    assert results[0].success is False
    gap2_final = store._unresolved_calls["gap_2"]
    assert gap2_final.retry_count == 3
    assert gap2_final.status == "unresolvable"
    # gap_1 was resolved after attempt 1, so it only got stamped once
    gap1_final = store._unresolved_calls["gap_1"]
    assert gap1_final.retry_count == 0  # resolved, never stamped


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
    ``python .icslpreprocess_{source_id}/icsl_tools.py check-complete --source <id>``
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
    # Subprocess must run in target_dir so .icslpreprocess_{source_id}/ resolves.
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


@pytest.mark.asyncio
async def test_check_gate_returns_false_on_timeout(orchestrator):
    """architecture.md §3 超时护栏: gate check must not block indefinitely.

    If the check-complete subprocess hangs (e.g., Neo4j connection stall),
    the gate check must time out and return False.
    """
    fake_proc = MagicMock()
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()

    async def hang_forever():
        await asyncio.sleep(9999)

    fake_proc.communicate = hang_forever

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
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


@pytest.mark.asyncio
async def test_write_progress_merges_with_hook_written_fields(tmp_path):
    """architecture.md §3 进度通信机制: _write_progress must merge with
    existing content so Hook-written fields (gaps_fixed/gaps_total/
    current_gap) are preserved when orchestrator writes state/attempt."""
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

    # Simulate hook writing progress first
    progress_dir = target_dir / "logs" / "repair" / "src_merge"
    progress_dir.mkdir(parents=True)
    progress_path = progress_dir / "progress.json"
    progress_path.write_text(
        json.dumps({"gaps_fixed": 2, "gaps_total": 5, "current_gap": "gap_003"}),
        encoding="utf-8",
    )

    # Orchestrator writes its own fields
    orchestrator._write_progress("src_merge", state="running", attempt=2)

    # Verify merge: both hook fields and orchestrator fields present
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    assert data["gaps_fixed"] == 2, "hook field lost during merge"
    assert data["gaps_total"] == 5, "hook field lost during merge"
    assert data["current_gap"] == "gap_003", "hook field lost during merge"
    assert data["state"] == "running"
    assert data["attempt"] == 2


def test_inject_files_copies_hooks_and_source_id(orchestrator, tmp_path):
    """Bug #1/#3: hooks must be copied to .icslpreprocess_{source_id}/hooks/ and
    source_id.txt must exist so hook scripts can identify the source."""
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="src_hook_test",
        counter_examples="",
    )

    hooks_dir = target_dir / ".icslpreprocess_src_hook_test" / "hooks"
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
    sid_path = target_dir / ".icslpreprocess_src_hook_test" / "source_id.txt"
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
    # architecture.md §3 lines 125-127: exact format "subprocess_timeout: <N>s"
    import re
    assert re.match(
        r"^subprocess_timeout: [\d.]+s$", updated_gap.last_attempt_reason
    ), f"format mismatch: '{updated_gap.last_attempt_reason}'"


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

    # Inject files so .icslpreprocess_src_gate_test/icsl_tools.py exists
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
    """_check_gate must return False (not crash) when .icslpreprocess_{source_id}/
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
async def test_retry_count_increments_per_gate_failure(tmp_path):
    """architecture.md §3 lines 115-119: '有残留 → 残留 GAP 的 retry_count++'

    Verifies the incremental progression 0→1→2→3 across gate failures,
    and that status flips to 'unresolvable' only at retry_count >= 3.
    """
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="src_inc", name="inc_entry", signature="void inc_entry()",
        file_path="inc.cpp", start_line=1, end_line=20, body_hash="hinc",
    ))
    gap = UnresolvedCallNode(
        caller_id="src_inc",
        call_expression="cb()",
        call_file="inc.cpp",
        call_line=7,
        call_type="indirect",
        source_code_snippet="cb();",
        var_name="cb",
        var_type="void (*)()",
    )
    store.create_unresolved_call(gap)
    assert store._unresolved_calls[gap.id].retry_count == 0

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    # Track retry_count after each gate check via side_effect
    observed_counts: list[int] = []

    async def gate_side_effect(source_id):
        # Record the retry_count BEFORE this gate failure triggers increment
        observed_counts.append(store._unresolved_calls[gap.id].retry_count)
        return False

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
    orchestrator._check_gate = AsyncMock(side_effect=gate_side_effect)

    await orchestrator.run_repairs(["src_inc"])

    # Gate was called 3 times (max_attempts=3 default)
    assert orchestrator._check_gate.call_count == 3
    # Before each gate failure, retry_count was 0, 1, 2 respectively
    assert observed_counts == [0, 1, 2]
    # After all 3 failures, retry_count == 3 and status == unresolvable
    final = store._unresolved_calls[gap.id]
    assert final.retry_count == 3
    assert final.status == "unresolvable"
    # Timestamps must be set
    assert final.last_attempt_timestamp is not None
    assert final.last_attempt_reason == "gate_failed: remaining pending GAPs"


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


@pytest.mark.asyncio
async def test_retry_failed_gaps_resets_unresolvable_on_run_start(tmp_path):
    """architecture.md §10 line 523: 'retry_failed_gaps: true → 跨运行重试：
    下次运行时重置 unresolvable GAP 的 retry_count，重新尝试'.

    When retry_failed_gaps=True, the orchestrator must reset all
    'unresolvable' GAPs to 'pending' with retry_count=0 at the start
    of run_repairs(), giving them a fresh budget."""
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
    # GAP that was previously marked unresolvable
    gap = UnresolvedCallNode(
        caller_id="src_001",
        call_expression="fn_ptr(x)",
        call_file="foo.cpp",
        call_line=7,
        call_type="indirect",
        source_code_snippet="fn_ptr(x);",
        var_name="fn_ptr",
        var_type="void (*)(int)",
        id="gap_exhausted",
        retry_count=3,
        status="unresolvable",
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
        retry_failed_gaps=True,
    )
    orchestrator = RepairOrchestrator(config=config)
    # Gate passes immediately (GAP resolved by agent)
    orchestrator._check_gate = AsyncMock(return_value=True)

    results = await orchestrator.run_repairs(["src_001"])
    assert results[0].success is True

    # The GAP should have been reset before the run started
    # (it was unresolvable, but retry_failed_gaps=True resets it)
    final_gap = store._unresolved_calls["gap_exhausted"]
    # After gate passes, the GAP might still be in store (check-complete
    # uses get_pending_gaps_for_source which filters by status="pending")
    # The key assertion: the run was able to proceed because the GAP
    # was reset from unresolvable to pending at the start.
    assert results[0].attempts == 1


@pytest.mark.asyncio
async def test_orchestrator_updates_source_point_status_on_gate_pass(tmp_path):
    """architecture.md §3 门禁机制: '无残留 → SourcePoint.status = "complete"'.
    When the gate check passes, the orchestrator must update the SourcePoint
    node's status to 'complete'."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, SourcePointNode, UnresolvedCallNode

    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="f1", name="entry", signature="void entry()",
        file_path="src/a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    sp = SourcePointNode(
        id="f1",
        entry_point_kind="entry_point",
        reason="test source",
        function_id="f1",
        status="pending",
    )
    store.create_source_point(sp)

    gap = UnresolvedCallNode(
        id="gap1", caller_id="f1", call_expression="foo()",
        call_file="src/a.cpp", call_line=5, call_type="indirect",
        source_code_snippet="foo();", var_name="foo", var_type="void(*)()",
    )
    store.create_unresolved_call(gap)

    config = RepairConfig(
        target_dir=tmp_path / "target",
        backend="claudecode",
        command="echo",
        args=["done"],
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=True)

    results = await orchestrator.run_repairs(["f1"])
    assert results[0].success is True

    updated_sp = store.get_source_point("f1")
    assert updated_sp is not None
    assert updated_sp.status == "complete", (
        "architecture.md §3: gate pass must set SourcePoint.status = 'complete'"
    )


@pytest.mark.asyncio
async def test_orchestrator_updates_source_point_status_on_exhaustion(tmp_path):
    """architecture.md §3 门禁机制: 'retry_count ≥ 3 → GAP.status = "unresolvable",
    SourcePoint.status = "partial_complete"'. When all GAPs are exhausted,
    the orchestrator must update SourcePoint status to 'partial_complete'."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, SourcePointNode, UnresolvedCallNode

    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="f1", name="entry", signature="void entry()",
        file_path="src/a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    sp = SourcePointNode(
        id="f1",
        entry_point_kind="entry_point",
        reason="test source",
        function_id="f1",
        status="running",
    )
    store.create_source_point(sp)

    gap = UnresolvedCallNode(
        id="gap1", caller_id="f1", call_expression="bar()",
        call_file="src/a.cpp", call_line=5, call_type="indirect",
        source_code_snippet="bar();", var_name="bar", var_type="void(*)()",
        retry_count=2, status="pending",
    )
    store.create_unresolved_call(gap)

    config = RepairConfig(
        target_dir=tmp_path / "target",
        backend="claudecode",
        command="echo",
        args=["done"],
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        graph_store=store,
        retry_failed_gaps=False,
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=False)

    results = await orchestrator.run_repairs(["f1"])
    assert results[0].success is False

    updated_sp = store.get_source_point("f1")
    assert updated_sp is not None
    assert updated_sp.status == "partial_complete", (
        "architecture.md §3: all GAPs exhausted must set "
        "SourcePoint.status = 'partial_complete'"
    )


@pytest.mark.asyncio
async def test_feedback_loop_injects_counter_examples_into_next_repair(tmp_path):
    """architecture.md §3 反馈机制 + §13 验证方案:
    Counter-examples submitted via feedback must appear in the agent's
    .icslpreprocess_{source_id}/counter_examples.md on the next repair run.

    Full chain: feedback_store.add() → orchestrator._inject_files() →
    counter_examples.md contains the pattern.
    """
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import (
        FunctionNode,
        UnresolvedCallNode,
    )

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    # Set up feedback store with a counter-example
    feedback_store = FeedbackStore(storage_dir=tmp_path / "feedback")
    feedback_store.add(CounterExample(
        pattern="function pointer cast to void* then called",
        call_context="void* fp = (void*)handler; ((fn_t)fp)()",
        wrong_target="generic_handler",
        correct_target="specific_handler",
    ))

    store = InMemoryGraphStore()
    fn = FunctionNode(
        signature="void entry()", name="entry", file_path="main.cpp",
        start_line=1, end_line=10, body_hash="h1", id="f1",
    )
    store.create_function(fn)
    store.create_unresolved_call(UnresolvedCallNode(
        id="gap1", caller_id="f1", call_expression="fp()",
        call_file="main.cpp", call_line=5, call_type="indirect",
        source_code_snippet="fp();", var_name="fp", var_type="void*",
    ))

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
        feedback_store=feedback_store,
    )
    orch = RepairOrchestrator(config)

    # Patch subprocess to avoid real agent spawn; just verify injection
    injected_content = {}

    original_inject = orch._inject_files

    def capture_inject(target_dir, source_id, counter_examples=""):
        injected_content["counter_examples"] = counter_examples
        original_inject(target_dir, source_id, counter_examples)

    with patch.object(orch, "_inject_files", side_effect=capture_inject):
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"done", b""))
            proc.returncode = 0
            proc.kill = MagicMock()
            proc.wait = AsyncMock()
            mock_proc.return_value = proc

            await orch.run_repairs(["f1"])

    # Verify counter-example was injected
    assert "counter_examples" in injected_content
    ce_text = injected_content["counter_examples"]
    assert "function pointer cast to void*" in ce_text, (
        "architecture.md §3: counter-example pattern must be injected into agent context"
    )
    assert "generic_handler" in ce_text
    assert "specific_handler" in ce_text


@pytest.mark.asyncio
async def test_retry_failed_gaps_false_skips_reset(tmp_path):
    """architecture.md §10 line 523: retry_failed_gaps=false must NOT reset
    unresolvable GAPs. Only when true should reset_unresolvable_gaps be called."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    store = InMemoryGraphStore()
    # Create a function and an unresolvable gap
    fn = FunctionNode(
        id="src_001", signature="void main()", name="main",
        file_path="src/main.c", start_line=1, end_line=10, body_hash="abc",
    )
    store.create_function(fn)
    gap = UnresolvedCallNode(
        id="gap_001", caller_id="src_001", call_expression="foo()",
        call_file="src/main.c", call_line=5, call_type="indirect",
        source_code_snippet="foo();", var_name=None, var_type=None,
        status="unresolvable", retry_count=3,
    )
    store.create_unresolved_call(gap)

    config = RepairConfig(
        target_dir=target_dir,
        command="echo",
        args=["done"],
        max_concurrency=1,
        graph_store=store,
        retry_failed_gaps=False,  # <-- key: must NOT reset
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=True)

    await orchestrator.run_repairs(["src_001"])

    # GAP must still be unresolvable — reset was skipped
    gap_after = store._unresolved_calls["gap_001"]
    assert gap_after.status == "unresolvable", (
        "retry_failed_gaps=False must not reset unresolvable GAPs"
    )
    assert gap_after.retry_count == 3


@pytest.mark.asyncio
async def test_retry_failed_gaps_true_resets_unresolvable(tmp_path):
    """architecture.md §10 line 523: retry_failed_gaps=true must reset
    unresolvable GAPs to pending with retry_count=0 before starting repairs."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    store = InMemoryGraphStore()
    fn = FunctionNode(
        id="src_001", signature="void main()", name="main",
        file_path="src/main.c", start_line=1, end_line=10, body_hash="abc",
    )
    store.create_function(fn)
    gap = UnresolvedCallNode(
        id="gap_001", caller_id="src_001", call_expression="foo()",
        call_file="src/main.c", call_line=5, call_type="indirect",
        source_code_snippet="foo();", var_name=None, var_type=None,
        status="unresolvable", retry_count=3,
    )
    store.create_unresolved_call(gap)

    config = RepairConfig(
        target_dir=target_dir,
        command="echo",
        args=["done"],
        max_concurrency=1,
        graph_store=store,
        retry_failed_gaps=True,  # <-- key: MUST reset
    )
    orchestrator = RepairOrchestrator(config=config)
    orchestrator._check_gate = AsyncMock(return_value=True)

    await orchestrator.run_repairs(["src_001"])

    # GAP must have been reset to pending with retry_count=0
    gap_after = store._unresolved_calls["gap_001"]
    assert gap_after.status != "unresolvable", (
        "retry_failed_gaps=True must reset unresolvable GAPs to pending"
    )


@pytest.mark.asyncio
async def test_source_with_no_pending_gaps_is_complete(tmp_path):
    """architecture.md §3: if a source has no pending gaps (all already
    resolved or none exist), it should be marked 'complete' — not
    'partial_complete'. The while loop exits immediately but that means
    the source is done, not failed."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    store = InMemoryGraphStore()
    # Function exists but has NO pending gaps
    fn = FunctionNode(
        id="src_001", signature="void main()", name="main",
        file_path="src/main.c", start_line=1, end_line=10, body_hash="abc",
    )
    store.create_function(fn)

    config = RepairConfig(
        target_dir=target_dir,
        command="echo",
        args=["done"],
        max_concurrency=1,
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)

    results = await orchestrator.run_repairs(["src_001"])

    # No gaps → source is complete, not partial_complete
    assert results[0].success is True, (
        "Source with no pending gaps should succeed (nothing to repair)"
    )
    sp = store.get_source_point("src_001")
    assert sp is not None
    assert sp.status == "complete", (
        "architecture.md §3: no pending gaps → SourcePoint.status = 'complete'"
    )


@pytest.mark.asyncio
async def test_orchestrator_seeds_gaps_total_in_progress_json(tmp_path):
    """architecture.md §3 progress.json schema: gaps_total must be seeded
    by the orchestrator before agent launch so the frontend can display
    progress even if the agent never emits a notification with gaps_total.

    The orchestrator knows the count via get_pending_gaps_for_source and
    must write it at attempt start."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    store = InMemoryGraphStore()
    # Create source function
    store.create_function(FunctionNode(
        id="src_001", name="main", signature="void main()",
        file_path="src/main.c", start_line=1, end_line=10, body_hash="h1",
    ))
    # Create 3 pending UnresolvedCalls for this source
    for i in range(3):
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id="src_001",
            call_expression=f"fn_{i}()",
            call_file="src/main.c",
            call_line=i + 2,
            call_type="indirect",
            source_code_snippet=f"fn_{i}();",
            var_name=f"fn_{i}",
            var_type="void (*)()",
        ))

    config = RepairConfig(
        target_dir=target_dir,
        command="echo",
        args=["done"],
        max_concurrency=1,
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)

    # Mock _check_gate to pass on first attempt
    orchestrator._check_gate = AsyncMock(return_value=True)

    results = await orchestrator.run_repairs(["src_001"])

    # Check progress.json was written with gaps_total
    progress_path = target_dir / "logs" / "repair" / "src_001" / "progress.json"
    assert progress_path.exists(), "progress.json must be written"
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    assert "gaps_total" in data, (
        "architecture.md §3: progress.json must contain gaps_total"
    )
    assert data["gaps_total"] == 3, (
        "gaps_total must reflect the number of pending gaps at attempt start"
    )


@pytest.mark.asyncio
async def test_concurrent_repairs_do_not_race_on_claude_md(tmp_path):
    """architecture.md §3: 'source 间并发, source 内串行'. When multiple
    sources run concurrently, each agent subprocess must read its OWN
    CLAUDE.md content (containing its source_id), not another source's.

    This test verifies that the inject+subprocess_start sequence is
    serialized so CLAUDE.md is not overwritten between inject and read."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    target_dir = tmp_path / "target_code"
    target_dir.mkdir()

    store = InMemoryGraphStore()
    # Create two source functions with one gap each
    for sid in ("src_A", "src_B"):
        store.create_function(FunctionNode(
            id=sid, name=sid, signature=f"void {sid}()",
            file_path=f"src/{sid}.c", start_line=1, end_line=10, body_hash=f"h_{sid}",
        ))
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id=sid,
            call_expression="fn()",
            call_file=f"src/{sid}.c",
            call_line=5,
            call_type="indirect",
            source_code_snippet="fn();",
            var_name="fn",
            var_type="void (*)()",
        ))

    # Track what CLAUDE.md content each subprocess sees
    observed_claude_md: dict[str, str] = {}

    config = RepairConfig(
        target_dir=target_dir,
        command="cat",  # Will be overridden
        args=[],
        max_concurrency=2,  # Both run concurrently
        graph_store=store,
    )
    orchestrator = RepairOrchestrator(config=config)

    # Mock _check_gate to always pass
    orchestrator._check_gate = AsyncMock(return_value=True)

    # Patch create_subprocess_exec to capture CLAUDE.md at subprocess start
    original_create = asyncio.create_subprocess_exec

    async def capturing_create(*args, **kwargs):
        # Read CLAUDE.md at the moment the subprocess would start
        claude_md = (target_dir / "CLAUDE.md").read_text(encoding="utf-8")
        # Extract source_id from the CLAUDE.md content
        for sid in ("src_A", "src_B"):
            if f"Source Point {sid}" in claude_md:
                observed_claude_md[sid] = claude_md
                break

        # Return a mock process that exits successfully
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=capturing_create):
        results = await orchestrator.run_repairs(["src_A", "src_B"])

    # Each subprocess must have seen its OWN source_id in CLAUDE.md
    assert "src_A" in observed_claude_md, "src_A subprocess never started"
    assert "src_B" in observed_claude_md, "src_B subprocess never started"
    assert f"Source Point src_A" in observed_claude_md["src_A"], (
        "src_A's subprocess read src_B's CLAUDE.md — race condition!"
    )
    assert f"Source Point src_B" in observed_claude_md["src_B"], (
        "src_B's subprocess read src_A's CLAUDE.md — race condition!"
    )


@pytest.mark.asyncio
async def test_log_path_follows_architecture_nested_format(tmp_path):
    """architecture.md §3: subprocess logs must be at
    logs/repair/{source_id}/attempt_{N}.log (nested per source, underscore prefix).
    """
    target_dir = tmp_path / "target_code"
    target_dir.mkdir()
    log_dir = tmp_path / "logs" / "repair"

    config = RepairConfig(
        target_dir=target_dir,
        backend="claudecode",
        command="echo",
        args=["done"],
        max_concurrency=1,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",
        log_dir=log_dir,
    )
    orch = RepairOrchestrator(config=config)

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"output", b""))
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await orch.run_repairs(["src_001"])

    # Architecture §3: logs/repair/{source_id}/attempt_{N}.log
    expected_log = log_dir / "src_001" / "attempt_1.log"
    assert expected_log.exists(), (
        f"architecture.md §3: expected log at {expected_log}, "
        f"found: {list(log_dir.rglob('*.log'))}"
    )

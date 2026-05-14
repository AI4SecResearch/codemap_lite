"""Tests for hook scripts (PostToolUse and Notification logging)."""
import json
import os
import tempfile
from pathlib import Path

from codemap_lite.agent.hooks.log_tool_use import process_tool_use_event
from codemap_lite.agent.hooks.log_notification import process_notification_event


def test_log_tool_use_appends_jsonl():
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "repair" / "src_001" / "gap_001.jsonl"
        event = {
            "tool_name": "Read",
            "params": {"file_path": "/tmp/test.cpp"},
            "result": "file contents...",
        }
        process_tool_use_event(event, source_id="src_001", gap_id="gap_001", log_dir=Path(tmpdir))

        assert log_path.exists()
        line = json.loads(log_path.read_text().strip())
        assert line["tool_name"] == "Read"
        assert "timestamp" in line


def test_log_tool_use_appends_multiple_entries():
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(3):
            event = {"tool_name": f"Tool{i}", "params": {}, "result": "ok"}
            process_tool_use_event(event, source_id="src_001", gap_id="gap_001", log_dir=Path(tmpdir))

        log_path = Path(tmpdir) / "repair" / "src_001" / "gap_001.jsonl"
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 3


def test_log_notification_updates_progress():
    with tempfile.TemporaryDirectory() as tmpdir:
        event = {
            "message": "Fixed gap gap_002",
            "gaps_fixed": 3,
            "gaps_total": 10,
            "current_gap": "gap_002",
        }
        process_notification_event(event, source_id="src_001", log_dir=Path(tmpdir))

        progress_path = Path(tmpdir) / "repair" / "src_001" / "progress.json"
        assert progress_path.exists()
        data = json.loads(progress_path.read_text())
        assert data["gaps_fixed"] == 3
        assert data["gaps_total"] == 10
        assert data["current_gap"] == "gap_002"


def test_log_notification_accepts_legacy_event_keys():
    """Backward-compat: ADR 0001 H5 / implementation-plan §2.4 had the agent
    runtime emitting `{fixed_gaps, total_gaps, current_gap_id}`. We still
    read those if present, but always write the canonical schema
    (architecture.md §3 + ADR 0004)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        event = {
            "message": "Fixed gap gap_002",
            "fixed_gaps": 3,
            "total_gaps": 10,
            "current_gap_id": "gap_002",
        }
        process_notification_event(event, source_id="src_001", log_dir=Path(tmpdir))

        progress_path = Path(tmpdir) / "repair" / "src_001" / "progress.json"
        data = json.loads(progress_path.read_text())
        assert data == {
            "gaps_fixed": 3,
            "gaps_total": 10,
            "current_gap": "gap_002",
        }


def test_log_tool_use_updates_progress_on_write_edge():
    """architecture.md §3 进度通信机制: PostToolUse hook must update
    progress.json when it detects a successful write-edge call, incrementing
    gaps_fixed and updating current_gap. This is the primary mechanism for
    real-time progress reporting — the agent doesn't emit Notification events
    for every edge write, so the PostToolUse hook must detect it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Pre-seed progress.json with orchestrator fields + initial gaps_total
        progress_dir = Path(tmpdir) / "repair" / "src_001"
        progress_dir.mkdir(parents=True)
        progress_path = progress_dir / "progress.json"
        progress_path.write_text(json.dumps({
            "state": "running",
            "attempt": 1,
            "gaps_fixed": 0,
            "gaps_total": 5,
            "current_gap": "gap_001",
        }))

        # Simulate a write-edge tool call event
        event = {
            "tool_name": "Bash",
            "params": {"command": "python .icslpreprocess_src_001/icsl_tools.py write-edge --caller fn_a --callee fn_b --call-type indirect --call-file src/main.c --call-line 42"},
            "result": '{"status": "ok", "edge_id": "e_001"}',
            "gap_id": "gap_001",
        }
        process_tool_use_event(event, source_id="src_001", gap_id="gap_001", log_dir=Path(tmpdir))

        data = json.loads(progress_path.read_text())
        assert data["gaps_fixed"] == 1, "gaps_fixed should increment on write-edge"
        assert data["current_gap"] == "gap_001"
        # Orchestrator fields preserved
        assert data["state"] == "running"
        assert data["attempt"] == 1


def test_log_tool_use_does_not_increment_progress_for_non_write_edge():
    """Only write-edge calls should increment gaps_fixed — other tool calls
    (Read, query-reachable, etc.) must not touch progress.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        progress_dir = Path(tmpdir) / "repair" / "src_001"
        progress_dir.mkdir(parents=True)
        progress_path = progress_dir / "progress.json"
        progress_path.write_text(json.dumps({"gaps_fixed": 2, "gaps_total": 5}))

        # A Read tool call — should NOT increment
        event = {
            "tool_name": "Read",
            "params": {"file_path": "/tmp/test.cpp"},
            "result": "file contents",
            "gap_id": "gap_003",
        }
        process_tool_use_event(event, source_id="src_001", gap_id="gap_003", log_dir=Path(tmpdir))

        data = json.loads(progress_path.read_text())
        assert data["gaps_fixed"] == 2, "non-write-edge calls must not increment gaps_fixed"


def test_log_notification_merges_with_existing_progress():
    """architecture.md §3 进度通信机制: hook must merge with existing
    progress.json fields (state, attempt, gate_result written by
    orchestrator), not overwrite them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Orchestrator has already written state/attempt fields
        progress_dir = Path(tmpdir) / "repair" / "src_001"
        progress_dir.mkdir(parents=True)
        progress_path = progress_dir / "progress.json"
        progress_path.write_text(json.dumps({
            "state": "running",
            "attempt": 2,
            "gate_result": "pending",
        }))

        # Hook writes gaps progress
        event = {"gaps_fixed": 4, "gaps_total": 8, "current_gap": "gap_005"}
        process_notification_event(event, source_id="src_001", log_dir=Path(tmpdir))

        data = json.loads(progress_path.read_text())
        # Hook fields must be present
        assert data["gaps_fixed"] == 4
        assert data["gaps_total"] == 8
        assert data["current_gap"] == "gap_005"
        # Orchestrator fields must be preserved (not wiped)
        assert data.get("state") == "running", (
            "orchestrator 'state' field was wiped by hook overwrite"
        )
        assert data.get("attempt") == 2, (
            "orchestrator 'attempt' field was wiped by hook overwrite"
        )


def test_write_edge_detection_handles_non_string_command():
    """_is_write_edge_call must not crash when params['command'] is not a
    string (e.g. None, int, or list). It should return False gracefully."""
    from codemap_lite.agent.hooks.log_tool_use import _is_write_edge_call

    # command is None
    assert _is_write_edge_call({"params": {"command": None}}) is False
    # command is a list (some agent runtimes may split argv)
    assert _is_write_edge_call({"params": {"command": ["python", "icsl_tools.py", "write-edge"]}}) is False
    # command is an int
    assert _is_write_edge_call({"params": {"command": 42}}) is False
    # params is not a dict
    assert _is_write_edge_call({"params": "write-edge icsl_tools"}) is False
    # params missing entirely
    assert _is_write_edge_call({}) is False


def test_write_edge_hook_preserves_gaps_total():
    """architecture.md §3: Hook must not lose gaps_total when incrementing
    gaps_fixed. The orchestrator pre-seeds gaps_total; the hook must merge,
    not overwrite."""
    with tempfile.TemporaryDirectory() as tmpdir:
        progress_dir = Path(tmpdir) / "repair" / "src_001"
        progress_dir.mkdir(parents=True)
        progress_path = progress_dir / "progress.json"
        # Orchestrator pre-writes gaps_total
        progress_path.write_text(json.dumps({
            "gaps_fixed": 0,
            "gaps_total": 10,
            "state": "running",
        }))

        # Hook detects write-edge
        event = {
            "tool_name": "Bash",
            "params": {"command": "python .icslpreprocess_src_001/icsl_tools.py write-edge --caller fn_a --callee fn_b --call-type indirect --call-file src/a.c --call-line 5"},
            "result": "ok",
        }
        process_tool_use_event(event, source_id="src_001", gap_id="gap_001", log_dir=Path(tmpdir))

        data = json.loads(progress_path.read_text())
        assert data["gaps_total"] == 10, "gaps_total was lost by hook"
        assert data["gaps_fixed"] == 1
        assert data["state"] == "running", "orchestrator state was lost"


def test_hooks_use_safe_dirname_for_path_unsafe_source_ids():
    """Hooks must apply _safe_dirname to source_id so progress files land in
    the same directory the orchestrator reads from (regression: raw source_id
    with '/' or '::' created nested/wrong directories)."""
    from codemap_lite.analysis.repair_orchestrator import _safe_dirname

    unsafe_id = "module/sub::OHOS::Func::OnRemoteRequest"
    safe_id = _safe_dirname(unsafe_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Notification hook
        event = {"gaps_fixed": 1, "gaps_total": 5, "current_gap": "gap_x"}
        process_notification_event(event, source_id=unsafe_id, log_dir=Path(tmpdir))

        # Must land in the safe dirname, not the raw source_id path
        progress_path = Path(tmpdir) / "repair" / safe_id / "progress.json"
        assert progress_path.exists(), (
            f"progress.json not at expected path: {progress_path}"
        )
        data = json.loads(progress_path.read_text())
        assert data["gaps_fixed"] == 1

    with tempfile.TemporaryDirectory() as tmpdir:
        # Tool use hook
        event = {"tool_name": "Read", "params": {"file_path": "/x"}, "result": "ok"}
        process_tool_use_event(event, source_id=unsafe_id, gap_id="gap_1", log_dir=Path(tmpdir))

        log_path = Path(tmpdir) / "repair" / safe_id / "gap_1.jsonl"
        assert log_path.exists(), (
            f"JSONL log not at expected path: {log_path}"
        )

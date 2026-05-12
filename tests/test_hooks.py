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
            "fixed_gaps": 3,
            "total_gaps": 10,
            "current_gap_id": "gap_002",
        }
        process_notification_event(event, source_id="src_001", log_dir=Path(tmpdir))

        progress_path = Path(tmpdir) / "repair" / "src_001" / "progress.json"
        assert progress_path.exists()
        data = json.loads(progress_path.read_text())
        assert data["fixed_gaps"] == 3
        assert data["total_gaps"] == 10
        assert data["current_gap_id"] == "gap_002"

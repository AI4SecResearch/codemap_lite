"""PostToolUse hook — logs agent tool usage to JSONL files."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _is_write_edge_call(event: dict[str, Any]) -> bool:
    """Detect if this tool use event is a successful write-edge invocation."""
    # The agent calls write-edge via Bash tool with icsl_tools.py
    command = ""
    params = event.get("params", {})
    if isinstance(params, dict):
        command = params.get("command", "")
    return "write-edge" in command and "icsl_tools" in command


def _update_progress_on_write_edge(
    source_id: str, gap_id: str, log_dir: Path
) -> None:
    """Increment gaps_fixed in progress.json after a successful write-edge.

    architecture.md §3 进度通信机制: the PostToolUse hook is the primary
    mechanism for real-time progress reporting because the agent doesn't
    emit Notification events for every edge write.
    """
    progress_path = log_dir / "repair" / source_id / "progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if progress_path.exists():
        try:
            existing = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    existing["gaps_fixed"] = existing.get("gaps_fixed", 0) + 1
    existing["current_gap"] = gap_id
    progress_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")


def process_tool_use_event(
    event: dict[str, Any],
    source_id: str,
    gap_id: str,
    log_dir: Path,
) -> None:
    """Append a tool use event to the JSONL log for a specific GAP."""
    log_path = log_dir / "repair" / source_id / f"{gap_id}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "tool_name": event.get("tool_name", ""),
        "params": event.get("params", {}),
        "result_summary": str(event.get("result", ""))[:500],
        "timestamp": time.time(),
    }

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Update progress.json when a write-edge call is detected
    if _is_write_edge_call(event):
        _update_progress_on_write_edge(source_id, gap_id, log_dir)


if __name__ == "__main__":
    import sys

    # Invoked by Claude Code / opencode hook system.
    # Reads JSON event from stdin; source_id from ../source_id.txt (relative
    # to hooks/ dir inside .icslpreprocess_{source_id}/);
    # gap_id from the event payload (falls back to "unknown").
    cwd = Path.cwd()
    # Resolve source_id.txt relative to this script's parent directory
    source_id_path = Path(__file__).resolve().parent.parent / "source_id.txt"
    if not source_id_path.exists():
        sys.exit(0)  # Graceful no-op if not in a repair context
    source_id = source_id_path.read_text(encoding="utf-8").strip()

    # Read event from stdin (Claude Code hook protocol passes JSON on stdin)
    try:
        event = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    gap_id = event.get("gap_id", "unknown")
    log_dir = cwd / "logs"
    process_tool_use_event(event=event, source_id=source_id, gap_id=gap_id, log_dir=log_dir)

"""PostToolUse hook — logs agent tool usage to JSONL files."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


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

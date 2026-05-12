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

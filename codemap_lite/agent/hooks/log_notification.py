"""Notification hook — updates progress.json for orchestrator polling."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


def _safe_dirname(source_id: str) -> str:
    """Convert source_id to filesystem-safe directory name.

    Must match codemap_lite.analysis.repair_orchestrator._safe_dirname
    exactly so hooks write to the same directory the orchestrator reads.
    """
    safe = re.sub(r"[/\\:]+", "_", source_id)
    safe = re.sub(r"_+", "_", safe).strip("_")
    if len(safe) > 60:
        h = hashlib.sha1(source_id.encode()).hexdigest()[:8]
        safe = safe[:60] + "_" + h
    return safe


def process_notification_event(
    event: dict[str, Any],
    source_id: str,
    log_dir: Path,
) -> None:
    """Update progress.json with current repair status."""
    safe_id = _safe_dirname(source_id)
    progress_path = log_dir / "repair" / safe_id / "progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)

    # Canonical progress.json schema (architecture.md §3 + ADR 0004).
    # Accept both the canonical keys and the legacy `fixed_gaps/total_gaps/
    # current_gap_id` names from ADR 0001 H5 + implementation-plan §2.4 so
    # an agent runtime emitting either shape still writes a readable file;
    # always emit the canonical schema that /api/v1/analyze/status +
    # SourceProgress consume.
    progress = {
        "gaps_fixed": event.get("gaps_fixed", event.get("fixed_gaps", 0)),
        "gaps_total": event.get("gaps_total", event.get("total_gaps", 0)),
        "current_gap": event.get("current_gap", event.get("current_gap_id", "")),
    }

    # Merge with existing content so orchestrator-written fields
    # (state, attempt, gate_result) are preserved (architecture.md §3
    # 进度通信机制: bidirectional merge between hook and orchestrator).
    existing: dict[str, Any] = {}
    if progress_path.exists():
        try:
            existing = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    existing.update(progress)
    progress_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    # Invoked by Claude Code / opencode hook system.
    # Reads JSON event from stdin; source_id from ../source_id.txt (relative
    # to hooks/ dir inside .icslpreprocess_{source_id}/);
    # log_dir defaults to cwd/logs (architecture.md §3 进度通信机制).
    cwd = Path.cwd()
    # Resolve source_id.txt relative to this script's parent directory
    # (works when copied to .icslpreprocess_{source_id}/hooks/)
    source_id_path = Path(__file__).resolve().parent.parent / "source_id.txt"
    if not source_id_path.exists():
        sys.exit(0)  # Graceful no-op if not in a repair context
    source_id = source_id_path.read_text(encoding="utf-8").strip()

    # Read event from stdin (Claude Code hook protocol passes JSON on stdin)
    try:
        event = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    log_dir = cwd / "logs"
    process_notification_event(event=event, source_id=source_id, log_dir=log_dir)

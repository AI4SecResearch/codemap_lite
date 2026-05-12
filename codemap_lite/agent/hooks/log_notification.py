"""Notification hook — updates progress.json for orchestrator polling."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def process_notification_event(
    event: dict[str, Any],
    source_id: str,
    log_dir: Path,
) -> None:
    """Update progress.json with current repair status."""
    progress_path = log_dir / "repair" / source_id / "progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)

    progress = {
        "fixed_gaps": event.get("fixed_gaps", 0),
        "total_gaps": event.get("total_gaps", 0),
        "current_gap_id": event.get("current_gap_id", ""),
    }

    progress_path.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")

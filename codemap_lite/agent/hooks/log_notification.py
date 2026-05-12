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

    progress_path.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")

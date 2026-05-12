"""Analysis trigger endpoints."""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel


class AnalyzeMode(str, Enum):
    """Valid analysis modes."""

    full = "full"
    incremental = "incremental"


class AnalyzeRequest(BaseModel):
    """Request body for triggering analysis."""

    mode: AnalyzeMode


def _read_source_progress(target_dir: Path | None) -> list[dict[str, Any]]:
    """Aggregate per-source progress files into a list of rows.

    Reads ``<target>/logs/repair/*/progress.json`` (the hook
    artefact specified in architecture.md §3 / ADR #52) and returns
    one row per source point with ``source_id`` / ``gaps_fixed`` /
    ``gaps_total`` / ``current_gap``.  Unreadable or missing files
    are skipped silently — the endpoint should degrade gracefully
    when ``repair`` has not run yet.
    """
    if target_dir is None:
        return []
    repair_root = target_dir / "logs" / "repair"
    if not repair_root.exists():
        return []

    rows: list[dict[str, Any]] = []
    for pf in sorted(repair_root.glob("*/progress.json")):
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append(
            {
                "source_id": pf.parent.name,
                "gaps_fixed": int(data.get("gaps_fixed", 0) or 0),
                "gaps_total": int(data.get("gaps_total", 0) or 0),
                "current_gap": data.get("current_gap"),
            }
        )
    return rows


def create_analyze_router() -> APIRouter:
    """Create the analyze router."""
    router = APIRouter(tags=["analyze"])

    @router.post("/analyze", status_code=202)
    def trigger_analyze(request: Request, body: AnalyzeRequest) -> dict[str, Any]:
        request.app.state.analyze_state = {
            "state": "running",
            "mode": body.mode.value,
            "progress": 0.0,
        }
        return {"status": "accepted", "mode": body.mode.value}

    @router.post("/analyze/repair", status_code=202)
    def trigger_repair(request: Request) -> dict[str, Any]:
        request.app.state.analyze_state = {
            "state": "repairing",
            "progress": 0.0,
        }
        return {"status": "accepted", "action": "repair"}

    @router.get("/analyze/status")
    def get_status(request: Request) -> dict[str, Any]:
        """Return analysis state and per-source repair progress.

        Legacy keys (``state`` / ``progress`` / ``mode``) are
        preserved for backwards compatibility.  ``sources`` is the
        aggregated view specified in architecture.md §3 (Repair
        Agent progress file contract) and ADR #52.
        """
        base = dict(request.app.state.analyze_state)
        target_dir = getattr(request.app.state, "target_dir", None)
        sources = _read_source_progress(target_dir)
        base["sources"] = sources
        # Derive an overall progress estimate from the hook files
        # when we have any (gaps_fixed / gaps_total across sources).
        if sources:
            total = sum(s["gaps_total"] for s in sources)
            fixed = sum(s["gaps_fixed"] for s in sources)
            if total > 0:
                base["progress"] = round(fixed / total, 4)
        return base

    return router

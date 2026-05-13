"""Analysis trigger endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)


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
        try:
            gaps_fixed = int(data.get("gaps_fixed", 0) or 0)
            gaps_total = int(data.get("gaps_total", 0) or 0)
        except (ValueError, TypeError):
            # Malformed numeric fields — skip this file gracefully
            # (architecture.md §3: "Unreadable or missing files are skipped")
            continue
        rows.append(
            {
                "source_id": pf.parent.name,
                "gaps_fixed": gaps_fixed,
                "gaps_total": gaps_total,
                "current_gap": data.get("current_gap"),
                "attempt": data.get("attempt"),
                "max_attempts": data.get("max_attempts"),
                "gate_result": data.get("gate_result"),
                "edges_written": data.get("edges_written"),
                "state": data.get("state"),
                "last_error": data.get("last_error"),
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
    async def trigger_repair(request: Request) -> dict[str, Any]:
        """Trigger repair agents in a background task.

        architecture.md §8: POST /api/v1/analyze/repair spawns the
        RepairOrchestrator asynchronously. Returns 202 immediately;
        progress is polled via GET /api/v1/analyze/status.
        """
        settings = getattr(request.app.state, "settings", None)
        if settings is None:
            # No settings wired (test / demo mode) — set state but don't spawn.
            request.app.state.analyze_state = {
                "state": "repairing",
                "progress": 0.0,
            }
            return {"status": "accepted", "action": "repair"}

        # Prevent double-spawn
        current = request.app.state.analyze_state
        if current.get("state") == "repairing":
            return {"status": "already_running", "action": "repair"}

        request.app.state.analyze_state = {
            "state": "repairing",
            "progress": 0.0,
        }

        async def _run_repair() -> None:
            try:
                from codemap_lite.analysis.feedback_store import FeedbackStore
                from codemap_lite.analysis.repair_orchestrator import (
                    RepairConfig,
                    RepairOrchestrator,
                )
                from codemap_lite.analysis.source_point_client import SourcePointClient
                from codemap_lite.graph.neo4j_store import Neo4jGraphStore

                target_dir = Path(settings.project.target_dir)
                feedback_store = getattr(request.app.state, "feedback_store", None)
                if feedback_store is None:
                    feedback_store = FeedbackStore(
                        storage_dir=target_dir / ".codemap_lite" / "feedback"
                    )

                graph_store = Neo4jGraphStore(
                    uri=settings.neo4j.uri,
                    user=settings.neo4j.user,
                    password=settings.neo4j.password,
                )

                # Resolve agent backend command
                backend = settings.agent.backend
                if backend == "claudecode":
                    command = settings.agent.claudecode.command
                    args = list(settings.agent.claudecode.args)
                elif backend == "opencode":
                    command = settings.agent.opencode.command
                    args = list(settings.agent.opencode.args)
                else:
                    logger.error("Unknown agent backend: %s", backend)
                    request.app.state.analyze_state = {"state": "idle", "progress": 0.0}
                    return

                # Fetch source points
                client = SourcePointClient(base_url=settings.codewiki_lite.base_url)
                try:
                    source_points = await client.fetch()
                except Exception:
                    source_points = []

                if not source_points:
                    logger.warning("No source points to repair")
                    request.app.state.analyze_state = {"state": "idle", "progress": 0.0}
                    return

                orch = RepairOrchestrator(
                    RepairConfig(
                        target_dir=target_dir,
                        backend=backend,
                        command=command,
                        args=args,
                        max_concurrency=settings.agent.max_concurrency,
                        neo4j_uri=settings.neo4j.uri,
                        neo4j_user=settings.neo4j.user,
                        neo4j_password=settings.neo4j.password,
                        feedback_store=feedback_store,
                        graph_store=graph_store,
                    )
                )

                source_ids = [sp.function_id for sp in source_points]
                await orch.run_repairs(source_ids)
            except Exception as exc:
                logger.exception("Repair background task failed: %s", exc)
            finally:
                request.app.state.analyze_state = {"state": "idle", "progress": 0.0}

        asyncio.ensure_future(_run_repair())
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

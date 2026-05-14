"""Analysis trigger endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AnalyzeMode(str, Enum):
    """Valid analysis modes."""

    full = "full"
    incremental = "incremental"


class AnalyzeRequest(BaseModel):
    """Request body for triggering analysis."""

    mode: AnalyzeMode


class RepairRequest(BaseModel):
    """Optional request body for POST /analyze/repair.

    architecture.md §3: repair can target specific source points.
    If source_ids is omitted or empty, all source points are repaired.
    """

    source_ids: list[str] = Field(default_factory=list)


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
                "source_id": data.get("source_id", pf.parent.name),
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


def _run_analysis_background(app: Any, settings: Any, mode: str) -> None:
    """Run the pipeline analysis in a background task.

    architecture.md §8: POST /api/v1/analyze triggers full/incremental.
    """
    try:
        from codemap_lite.graph.neo4j_store import Neo4jGraphStore
        from codemap_lite.pipeline.orchestrator import PipelineOrchestrator

        target_dir = Path(settings.project.target_dir)
        graph_store = Neo4jGraphStore(
            uri=settings.neo4j.uri,
            user=settings.neo4j.user,
            password=settings.neo4j.password,
        )
        orch = PipelineOrchestrator(target_dir=target_dir, store=graph_store)

        if mode == "incremental":
            result = orch.run_incremental_analysis()
        else:
            result = orch.run_full_analysis()

        app.state.analyze_state = {
            "state": "idle",
            "progress": 1.0,
            "mode": mode,
            "started_at": app.state.analyze_state.get("started_at"),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "result": {
                "files_scanned": result.files_scanned,
                "functions_found": result.functions_found,
                "direct_calls": result.direct_calls,
                "unresolved_calls": result.unresolved_calls,
                "success": result.success,
                "affected_source_ids": getattr(result, "affected_source_ids", []),
            },
        }
    except Exception as exc:
        logger.error("Background analysis failed: %s", exc)
        app.state.analyze_state = {
            "state": "idle",
            "progress": 0.0,
            "started_at": app.state.analyze_state.get("started_at"),
            "error": str(exc),
        }


def create_analyze_router() -> APIRouter:
    """Create the analyze router."""
    router = APIRouter(tags=["analyze"])

    @router.post("/analyze", status_code=202)
    def trigger_analyze(
        request: Request, body: AnalyzeRequest, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        """Trigger full or incremental analysis.

        architecture.md §8: POST /api/v1/analyze triggers the pipeline
        asynchronously. Returns 202 immediately; progress is polled via
        GET /api/v1/analyze/status.
        """
        # Prevent double-spawn
        current = request.app.state.analyze_state
        if current.get("state") in ("running", "repairing"):
            raise HTTPException(
                status_code=409,
                detail="Analysis is already running",
            )

        request.app.state.analyze_state = {
            "state": "running",
            "mode": body.mode.value,
            "progress": 0.0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }

        settings = getattr(request.app.state, "settings", None)
        if settings is not None:
            background_tasks.add_task(
                _run_analysis_background, request.app, settings, body.mode.value
            )

        return {"status": "accepted", "mode": body.mode.value}

    @router.post("/analyze/repair", status_code=202)
    async def trigger_repair(
        request: Request, body: RepairRequest | None = None
    ) -> dict[str, Any]:
        """Trigger repair agents in a background task.

        architecture.md §8: POST /api/v1/analyze/repair spawns the
        RepairOrchestrator asynchronously. Returns 202 immediately;
        progress is polled via GET /api/v1/analyze/status.

        Optional body.source_ids filters which source points to repair.
        """
        requested_source_ids = body.source_ids if body else []
        settings = getattr(request.app.state, "settings", None)

        # Prevent double-spawn (architecture.md §8: 409 Conflict)
        current = request.app.state.analyze_state
        if current.get("state") == "repairing":
            raise HTTPException(
                status_code=409,
                detail="Repair is already running",
            )

        if settings is None:
            # No settings wired (test / demo mode) — set state but don't spawn.
            request.app.state.analyze_state = {
                "state": "repairing",
                "progress": 0.0,
            }
            return {"status": "accepted", "action": "repair"}

        request.app.state.analyze_state = {
            "state": "repairing",
            "progress": 0.0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }

        async def _run_repair() -> None:
            _success = False
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
                        retry_failed_gaps=settings.agent.retry_failed_gaps,
                        subprocess_timeout_seconds=settings.agent.subprocess_timeout_seconds,
                    )
                )

                source_ids = [sp.function_id for sp in source_points]
                # Filter to requested source_ids if provided
                if requested_source_ids:
                    source_ids = [
                        sid for sid in source_ids if sid in set(requested_source_ids)
                    ]
                await orch.run_repairs(source_ids)
                _success = True
            except Exception as exc:
                logger.exception("Repair background task failed: %s", exc)
            finally:
                request.app.state.analyze_state = {
                    "state": "idle",
                    "progress": 1.0 if _success else 0.0,
                    "started_at": request.app.state.analyze_state.get("started_at"),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }

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

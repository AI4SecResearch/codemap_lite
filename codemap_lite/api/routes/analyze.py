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
        from codemap_lite.analysis.source_point_client import SourcePointClient

        target_dir = Path(settings.project.target_dir)
        graph_store = Neo4jGraphStore(
            uri=settings.neo4j.uri,
            user=settings.neo4j.user,
            password=settings.neo4j.password,
        )
        source_client = SourcePointClient(
            base_url=settings.codewiki_lite.base_url
        )
        orch = PipelineOrchestrator(
            target_dir=target_dir,
            store=graph_store,
            source_point_client=source_client,
        )

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

                # Determine which source_ids to repair:
                # - If user provided explicit source_ids, use them directly
                #   (these should be Neo4j 12-char hash IDs from the frontend).
                # - Otherwise, resolve codewiki_lite long-path IDs to Neo4j
                #   12-char hashes by looking up Function nodes by name.
                # - Fallback: if codewiki_lite is empty, use Neo4j SourcePoint nodes.
                if requested_source_ids:
                    source_ids = list(requested_source_ids)
                elif source_points:
                    # codewiki_lite returns long-path IDs (file::ns::class::method).
                    # Orchestrator needs Neo4j Function.id (12-char sha1 hash).
                    # Look up each function by name in Neo4j.
                    source_ids = []
                    for sp in source_points:
                        func_name = sp.function_id.split("::")[-1] if "::" in sp.function_id else sp.function_id
                        # Extract file stem from the long path (before ::)
                        file_hint = sp.function_id.split("::")[0] if "::" in sp.function_id else ""
                        cypher = (
                            "MATCH (f:Function) WHERE f.name = $name RETURN f.id, f.file_path"
                        )
                        with graph_store._get_driver().session() as session:
                            records = list(session.run(cypher, name=func_name))
                        if records:
                            # Prefer match by file path similarity
                            best = records[0]
                            if file_hint:
                                for r in records:
                                    if file_hint.split("/")[-1].replace(".h", "") in (r["f.file_path"] or ""):
                                        best = r
                                        break
                            source_ids.append(best["f.id"])
                        else:
                            logger.warning("Cannot resolve source %s to Neo4j Function", sp.function_id)
                else:
                    # Fallback: use Neo4j SourcePoint nodes when codewiki_lite
                    # is unavailable (architecture.md §8 graceful degradation).
                    try:
                        store_sps = graph_store.list_source_points()
                        source_ids = [
                            sp.function_id for sp in store_sps if sp.function_id
                        ]
                    except Exception:
                        source_ids = []
                    if not source_ids:
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
                        log_dir=target_dir / "logs" / "repair",
                    )
                )

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

        # Enrich with Neo4j-derived gap counts when graph store is available.
        # Use reachable subgraph to count ALL repair logs (depth=1 and deeper),
        # matching the SourceDetail view which uses source_reachable.
        store = getattr(request.app.state, "store", None)
        if store is not None and sources:
            try:
                all_logs = store.get_repair_logs()
            except Exception:
                all_logs = []
            for src in sources:
                sid = src.get("source_id", "")
                if not sid:
                    continue
                try:
                    # Get all function IDs reachable from this source
                    subgraph = store.get_reachable_subgraph(sid, max_depth=50)
                    node_ids = {fn.id for fn in subgraph.get("nodes", [])}
                    node_ids.add(sid)
                    # Count repair logs where caller is in the subgraph
                    repair_count = sum(1 for log in all_logs if log.caller_id in node_ids)
                    # Unresolved calls only for the source function itself (direct GAPs)
                    unresolved_count = len(
                        store.get_unresolved_calls(caller_id=sid, status="pending")
                    )
                    total = repair_count + unresolved_count
                    if total == 0:
                        continue  # No data in graph — keep progress.json values
                    src["gaps_total"] = total
                    src["gaps_fixed"] = repair_count
                except Exception:
                    pass  # Keep progress.json values as fallback

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

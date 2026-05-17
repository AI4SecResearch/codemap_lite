"""RepairLog audit-trail endpoints.

Surfaces ``RepairLogNode`` entries written by the repair agent
(architecture.md §3 修复成功时 + §4 RepairLog schema + ADR #51
属性引用契约). Supports exact-match filtering by ``caller``,
``callee``, and ``location`` so the frontend CallGraphView can
resolve the RepairLog for a selected ``resolved_by='llm'`` CALLS
edge via the ``(caller_id, callee_id, call_location)`` triple.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request


def _safe_dirname(source_id: str) -> str:
    """Convert source_id to a filesystem-safe directory name.

    Must match the implementation in repair_orchestrator.py exactly.
    """
    safe = re.sub(r"[/\\:]+", "_", source_id)
    safe = re.sub(r"_+", "_", safe).strip("_")
    if len(safe) > 60:
        h = hashlib.sha1(source_id.encode()).hexdigest()[:8]
        safe = safe[:60] + "_" + h
    return safe


def create_repair_logs_router() -> APIRouter:
    """Create the repair-logs router."""
    router = APIRouter(tags=["repair-logs"])

    @router.get("/repair-logs")
    def list_repair_logs(
        request: Request,
        caller: str | None = Query(default=None),
        callee: str | None = Query(default=None),
        location: str | None = Query(default=None),
        source: str | None = Query(default=None, description="Filter by source point ID (RepairLog.source_id)"),
        source_reachable: str | None = Query(
            default=None,
            description="Return all repair logs where caller_id is reachable from this source function (BFS through call graph)",
        ),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        store = request.app.state.store

        # source_reachable: get all function IDs in the reachable subgraph,
        # then return repair logs where caller_id is in that set.
        if source_reachable:
            try:
                subgraph = store.get_reachable_subgraph(source_reachable, max_depth=50)
                nodes = subgraph.get("nodes", [])
                node_ids = {getattr(fn, "id", None) or fn.get("id", "") if isinstance(fn, dict) else fn.id for fn in nodes}
                # Include the source itself
                node_ids.add(source_reachable)
                node_ids.discard(None)
                node_ids.discard("")
            except Exception:
                node_ids = {source_reachable}
            all_logs = store.get_repair_logs()
            logs = [log for log in all_logs if log.caller_id in node_ids]
        else:
            logs = store.get_repair_logs(
                caller_id=caller,
                callee_id=callee,
                call_location=location,
                source_id=source,
            )

        total = len(logs)
        items = [asdict(log) for log in logs[offset:offset + limit]]
        return {"total": total, "items": items}

    @router.get("/repair-logs/live")
    def get_live_log(
        request: Request,
        source_id: str = Query(..., description="Source point function ID"),
        tail: int = Query(default=30, ge=1, le=200),
    ) -> dict[str, Any]:
        """Read last N lines from the latest agent attempt log (ADR-0008).

        Returns the tail of ``<target_dir>/logs/repair/<source_id>/attempt_N.log``
        so the frontend can display real-time agent reasoning in the
        source card embedded terminal.
        """
        # Resolve log base from settings.project.target_dir (where orchestrator writes)
        settings = getattr(request.app.state, "settings", None)
        if settings and hasattr(settings, "project") and hasattr(settings.project, "target_dir"):
            log_base = Path(settings.project.target_dir) / "logs" / "repair" / _safe_dirname(source_id)
        elif getattr(request.app.state, "target_dir", None) is not None:
            log_base = Path(request.app.state.target_dir) / "logs" / "repair" / _safe_dirname(source_id)
        else:
            log_base = Path("logs/repair") / _safe_dirname(source_id)

        if not log_base.exists():
            return {"lines": [], "attempt": 0, "finished": False, "source_id": source_id}

        # Find latest attempt log
        log_files = sorted(log_base.glob("attempt_*.log"))
        if not log_files:
            return {"lines": [], "attempt": 0, "finished": False, "source_id": source_id}

        latest = log_files[-1]
        # Extract attempt number from filename
        match = re.search(r"attempt_(\d+)\.log", latest.name)
        attempt = int(match.group(1)) if match else 0

        # Read tail lines
        try:
            content = latest.read_text(encoding="utf-8", errors="replace")
            all_lines = content.splitlines()
            lines = all_lines[-tail:] if len(all_lines) > tail else all_lines
        except OSError:
            lines = []

        # Check if finished via progress.json
        finished = False
        progress_path = log_base / "progress.json"
        if progress_path.exists():
            try:
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
                state = progress.get("state")
                # Explicitly finished states
                if state in ("succeeded", "failed"):
                    finished = True
                # Heuristic: if state is "running" but last_error is set and
                # the log file hasn't been modified in >60s, the orchestrator
                # likely crashed without finalizing. Treat as finished.
                elif state == "running" and progress.get("last_error"):
                    import time
                    try:
                        mtime = progress_path.stat().st_mtime
                        if time.time() - mtime > 60:
                            finished = True
                    except OSError:
                        pass
            except (json.JSONDecodeError, OSError):
                pass

        return {
            "lines": lines,
            "attempt": attempt,
            "finished": finished,
            "source_id": source_id,
        }

    return router

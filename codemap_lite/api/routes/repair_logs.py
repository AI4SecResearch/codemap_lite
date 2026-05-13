"""RepairLog audit-trail endpoints.

Surfaces ``RepairLogNode`` entries written by the repair agent
(architecture.md §3 修复成功时 + §4 RepairLog schema + ADR #51
属性引用契约). Supports exact-match filtering by ``caller``,
``callee``, and ``location`` so the frontend CallGraphView can
resolve the RepairLog for a selected ``resolved_by='llm'`` CALLS
edge via the ``(caller_id, callee_id, call_location)`` triple.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Query, Request


def create_repair_logs_router() -> APIRouter:
    """Create the repair-logs router."""
    router = APIRouter(tags=["repair-logs"])

    @router.get("/repair-logs")
    def list_repair_logs(
        request: Request,
        caller: str | None = Query(default=None),
        callee: str | None = Query(default=None),
        location: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        store = request.app.state.store
        logs = store.get_repair_logs(
            caller_id=caller,
            callee_id=callee,
            call_location=location,
        )
        return [asdict(log) for log in logs[offset:offset + limit]]

    return router

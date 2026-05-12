"""Source points endpoints."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Query, Request


def create_source_points_router() -> APIRouter:
    """Create the source points router."""
    router = APIRouter(tags=["source-points"])

    @router.get("/source-points")
    def list_source_points(
        request: Request,
        kind: str | None = Query(default=None),
        module: str | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        entries = getattr(request.app.state, "source_points", [])
        if kind:
            entries = [e for e in entries if e.get("kind") == kind]
        if module:
            entries = [e for e in entries if module in e.get("module", "")]
        return entries

    @router.get("/source-points/summary")
    def source_points_summary(request: Request) -> dict[str, Any]:
        entries = getattr(request.app.state, "source_points", [])
        by_kind: dict[str, int] = {}
        for e in entries:
            k = e.get("kind", "unknown")
            by_kind[k] = by_kind.get(k, 0) + 1
        return {"total": len(entries), "by_kind": by_kind}

    @router.get("/source-points/{source_id:path}/reachable")
    def get_reachable(request: Request, source_id: str) -> dict[str, Any]:
        store = request.app.state.store
        # Source-point ids from archdoc don't match FunctionNode ids directly.
        # Look up the entry in app.state.source_points and use its resolved
        # function_id for the BFS seed.
        entries = getattr(request.app.state, "source_points", [])
        seed_id = source_id
        for entry in entries:
            if entry.get("id") == source_id:
                resolved = entry.get("function_id")
                if resolved:
                    seed_id = resolved
                break
        subgraph = store.get_reachable_subgraph(seed_id)
        return {
            "nodes": [asdict(n) for n in subgraph["nodes"]],
            "edges": [
                {
                    "caller_id": e.caller_id,
                    "callee_id": e.callee_id,
                    "props": asdict(e.props),
                }
                for e in subgraph["edges"]
            ],
            "unresolved": [asdict(u) for u in subgraph["unresolved"]],
        }

    return router

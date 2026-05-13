"""Graph browsing endpoints."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request


def create_graph_router() -> APIRouter:
    """Create the graph browsing router."""
    router = APIRouter(tags=["graph"])

    @router.get("/files")
    def list_files(request: Request) -> list[dict[str, Any]]:
        store = request.app.state.store
        return [asdict(f) for f in store.list_files()]

    @router.get("/functions")
    def list_functions(
        request: Request, file: str | None = Query(default=None)
    ) -> list[dict[str, Any]]:
        store = request.app.state.store
        return [asdict(f) for f in store.list_functions(file_path=file)]

    @router.get("/functions/{function_id:path}/callers")
    def get_callers(request: Request, function_id: str) -> list[dict[str, Any]]:
        store = request.app.state.store
        if store.get_function_by_id(function_id) is None:
            raise HTTPException(status_code=404, detail="Function not found")
        callers = store.get_callers(function_id)
        return [asdict(f) for f in callers]

    @router.get("/functions/{function_id:path}/callees")
    def get_callees(request: Request, function_id: str) -> list[dict[str, Any]]:
        store = request.app.state.store
        if store.get_function_by_id(function_id) is None:
            raise HTTPException(status_code=404, detail="Function not found")
        callees = store.get_callees(function_id)
        return [asdict(f) for f in callees]

    @router.get("/functions/{function_id:path}/call-chain")
    def get_call_chain(
        request: Request,
        function_id: str,
        depth: int = Query(default=5, ge=1, le=50),
    ) -> dict[str, Any]:
        store = request.app.state.store
        if store.get_function_by_id(function_id) is None:
            raise HTTPException(status_code=404, detail="Function not found")
        subgraph = store.get_reachable_subgraph(function_id, max_depth=depth)
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

    @router.get("/functions/{function_id:path}")
    def get_function(request: Request, function_id: str) -> dict[str, Any]:
        store = request.app.state.store
        fn = store.get_function_by_id(function_id)
        if fn is None:
            raise HTTPException(status_code=404, detail="Function not found")
        return asdict(fn)

    @router.get("/unresolved-calls")
    def list_unresolved_calls(
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        store = request.app.state.store
        all_uc = store.get_unresolved_calls()
        total = len(all_uc)
        items = all_uc[offset:offset + limit]
        return {
            "total": total,
            "items": [asdict(u) for u in items],
        }

    return router

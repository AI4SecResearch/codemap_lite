"""Graph browsing endpoints."""
from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request


def _paginate(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    """Wrap a list in {total, items} pagination per architecture.md §8."""
    total = len(items)
    page = items[offset:offset + limit]
    return {"total": total, "items": page}


def create_graph_router() -> APIRouter:
    """Create the graph browsing router."""
    router = APIRouter(tags=["graph"])

    @router.get("/files")
    def list_files(
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        store = request.app.state.store
        return _paginate([asdict(f) for f in store.list_files()], limit, offset)

    @router.get("/functions")
    def list_functions(
        request: Request,
        file: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=10000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        store = request.app.state.store
        return _paginate([asdict(f) for f in store.list_functions(file_path=file)], limit, offset)

    @router.get("/functions/{function_id:path}/callers")
    def get_callers(
        request: Request,
        function_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        store = request.app.state.store
        if store.get_function_by_id(function_id) is None:
            raise HTTPException(status_code=404, detail="Function not found")
        callers = store.get_callers(function_id)
        return _paginate([asdict(f) for f in callers], limit, offset)

    @router.get("/functions/{function_id:path}/callees")
    def get_callees(
        request: Request,
        function_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        store = request.app.state.store
        if store.get_function_by_id(function_id) is None:
            raise HTTPException(status_code=404, detail="Function not found")
        callees = store.get_callees(function_id)
        return _paginate([asdict(f) for f in callees], limit, offset)

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
        limit: int = Query(default=100, ge=1, le=50000),
        offset: int = Query(default=0, ge=0),
        caller: str | None = Query(default=None),
        status: str | None = Query(default=None),
        category: str | None = Query(default=None),
    ) -> dict[str, Any]:
        store = request.app.state.store
        total = store.count_unresolved_calls(
            caller_id=caller, status=status, category=category,
        )
        items = store.get_unresolved_calls(
            caller_id=caller, status=status, category=category,
            limit=limit, offset=offset,
        )
        return {
            "total": total,
            "items": [asdict(u) for u in items],
        }

    @router.get("/source-code")
    def get_source_code(
        request: Request,
        file: str = Query(..., description="File path (relative to target_dir or absolute)"),
        start: int = Query(..., ge=1, description="Start line (1-based, inclusive)"),
        end: int = Query(..., ge=1, description="End line (1-based, inclusive)"),
    ) -> dict[str, Any]:
        """Read source code snippet from target directory (architecture.md §8)."""
        target_dir: str | None = None
        td = getattr(request.app.state, "target_dir", None)
        if td is not None:
            target_dir = str(td)

        # Resolve path: absolute paths used as-is (graph store stores absolute
        # file_path), relative paths resolved against target_dir.
        if os.path.isabs(file):
            resolved = os.path.realpath(file)
        elif target_dir:
            resolved = os.path.realpath(os.path.join(target_dir, file))
        else:
            raise HTTPException(status_code=500, detail="target_dir not configured and path is relative")

        try:
            with open(resolved, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            raise HTTPException(status_code=404, detail="File not found")

        # Clamp to file bounds
        start_idx = max(0, start - 1)
        end_idx = min(len(lines), end)
        content = "".join(lines[start_idx:end_idx])

        return {
            "file": file,
            "start_line": start_idx + 1,
            "end_line": end_idx,
            "content": content,
        }

    return router

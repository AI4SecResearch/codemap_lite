"""Source points endpoints."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request


def create_source_points_router() -> APIRouter:
    """Create the source points router."""
    router = APIRouter(tags=["source-points"])

    @router.get("/source-points")
    def list_source_points(
        request: Request,
        kind: str | None = Query(default=None),
        module: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """List source points with status from graph store.

        Merges codewiki_lite metadata (app.state.source_points) with
        SourcePointNode status from the graph store (architecture.md §4).
        """
        store = request.app.state.store
        # Build a status lookup from graph store SourcePoints
        sp_status_map: dict[str, str] = {}
        try:
            for sp in store.list_source_points():
                sp_status_map[sp.id] = sp.status
                # Also index by function_id for cross-reference
                if sp.function_id:
                    sp_status_map[sp.function_id] = sp.status
        except Exception:
            pass  # Graceful fallback if store doesn't support it

        entries = getattr(request.app.state, "source_points", [])

        # Build function lookup for enrichment (signature, file, line).
        # Source points reference functions by function_id which is the
        # graph store's FunctionNode.id (architecture.md §4).
        fn_lookup: dict[str, Any] = {}
        # Secondary index: bare function name → FunctionNode (for matching
        # codewiki_lite path-based IDs to graph store hex IDs).
        fn_by_name: dict[str, Any] = {}
        try:
            for fn in store.list_functions():
                fn_lookup[fn.id] = fn
                # Index by bare name (last :: segment) for fallback matching
                bare = fn.name.split("::")[-1] if "::" in fn.name else fn.name
                fn_by_name.setdefault(bare, fn)
        except Exception:
            pass  # Graceful fallback if store is empty

        # Enrich each entry with status from graph store
        enriched = []
        for e in entries:
            item = dict(e)
            # Try matching by id first, then function_id
            sp_id = item.get("id", "")
            func_id = item.get("function_id", "")
            resolved_status = sp_status_map.get(sp_id) or sp_status_map.get(
                func_id, "pending"
            )
            item.setdefault("status", resolved_status)
            # Frontend uses "kind" but backend stores "entry_point_kind" —
            # expose both for compatibility (architecture.md §8).
            if "kind" not in item and "entry_point_kind" in item:
                item["kind"] = item["entry_point_kind"]
            # Enrich with FunctionNode data so frontend can display
            # signature, file, and line (architecture.md §8 source-points
            # response contract).
            fn = fn_lookup.get(func_id) or fn_lookup.get(sp_id)
            if fn is None and "::" in func_id:
                # Fallback: match by bare function name (codewiki_lite uses
                # path-based IDs like "dir/file.h::NS::Class::Method" while
                # graph store uses 12-char hex IDs).
                bare_name = func_id.split("::")[-1]
                fn = fn_by_name.get(bare_name)
            if fn is not None:
                item.setdefault("signature", fn.signature or fn.name)
                item.setdefault("file", fn.file_path)
                item.setdefault("line", fn.start_line)
            else:
                item.setdefault("signature", func_id.split("::")[-1] if "::" in func_id else func_id)
                item.setdefault("file", "")
                item.setdefault("line", 0)
            # Ensure id is set for frontend keying
            item.setdefault("id", func_id)
            enriched.append(item)

        if kind:
            enriched = [e for e in enriched if e.get("entry_point_kind") == kind]
        if module:
            enriched = [e for e in enriched if module in e.get("module", "")]
        if status:
            enriched = [e for e in enriched if e.get("status") == status]
        total = len(enriched)
        items = enriched[offset:offset + limit]
        return {"total": total, "items": items}

    @router.get("/source-points/summary")
    def source_points_summary(request: Request) -> dict[str, Any]:
        """Summary counts by kind and status (architecture.md §8)."""
        store = request.app.state.store
        sp_status_map: dict[str, str] = {}
        try:
            for sp in store.list_source_points():
                sp_status_map[sp.id] = sp.status
                if sp.function_id:
                    sp_status_map[sp.function_id] = sp.status
        except Exception:
            pass

        entries = getattr(request.app.state, "source_points", [])
        by_kind: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for e in entries:
            k = e.get("entry_point_kind", "unknown")
            by_kind[k] = by_kind.get(k, 0) + 1
            sp_id = e.get("id", "")
            s = sp_status_map.get(sp_id) or sp_status_map.get(
                e.get("function_id", ""), "pending"
            )
            by_status[s] = by_status.get(s, 0) + 1
        return {"total": len(entries), "by_kind": by_kind, "by_status": by_status}

    @router.get("/source-points/{source_id}")
    def get_source_point(request: Request, source_id: str) -> dict[str, Any]:
        """Get a single source point with status (architecture.md §8)."""
        store = request.app.state.store
        # Try graph store first
        sp = store.get_source_point(source_id)
        if sp is not None:
            from dataclasses import asdict
            return asdict(sp)
        # Fall back to codewiki_lite entries
        entries = getattr(request.app.state, "source_points", [])
        for entry in entries:
            if entry.get("id") == source_id or entry.get("function_id") == source_id:
                item = dict(entry)
                item.setdefault("status", "pending")
                return item
        raise HTTPException(status_code=404, detail="Source point not found")

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

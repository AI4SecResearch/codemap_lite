"""Source points endpoints."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request


def _progress_status(target_dir: Path | None, function_id: str) -> str | None:
    """Derive source point status from progress.json as fallback.

    When the orchestrator crashes, Neo4j SourcePoint.status may be stale
    ("pending" or "running") while progress.json already reflects the
    terminal state. This function reads progress.json and maps its state
    to the SourcePoint status vocabulary.
    """
    if target_dir is None or not function_id:
        return None
    import hashlib
    import re
    # Replicate _safe_dirname logic inline to avoid circular import
    safe = re.sub(r"[/\\:]+", "_", function_id)
    safe = re.sub(r"_+", "_", safe).strip("_")
    if len(safe) > 60:
        h = hashlib.sha1(function_id.encode()).hexdigest()[:8]
        safe = safe[:60] + "_" + h
    progress_path = target_dir / "logs" / "repair" / safe / "progress.json"
    if not progress_path.exists():
        return None
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    state = data.get("state")
    if state == "succeeded":
        return "complete"
    if state == "failed":
        return "partial_complete"
    if state == "running" and data.get("last_error"):
        # Orchestrator crashed — check if progress file is stale (>60s old)
        import time
        try:
            mtime = progress_path.stat().st_mtime
            if time.time() - mtime > 60:
                return "partial_complete"
        except OSError:
            pass
    if state == "running":
        return "running"
    return None


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

        # architecture.md §8: when codewiki_lite entries are empty, fall back
        # to graph store SourcePointNodes so the endpoint always reflects
        # the actual state of the system.
        if not entries:
            try:
                store_sps = store.list_source_points()
                entries = [
                    {
                        "id": sp.id,
                        "function_id": sp.function_id,
                        "entry_point_kind": sp.entry_point_kind,
                        "reason": sp.reason,
                        "module": sp.module,
                        "status": sp.status,
                    }
                    for sp in store_sps
                ]
            except Exception:
                entries = []

        # Build function lookup for enrichment (signature, file, line).
        # Source points reference functions by function_id which is the
        # graph store's FunctionNode.id (architecture.md §4).
        fn_lookup: dict[str, Any] = {}
        # Secondary indices for matching codewiki_lite path-based IDs
        # (e.g. "dir/file.h::NS::Class::Method") to graph store hex IDs.
        fn_by_name: dict[str, list] = {}  # bare name → [FunctionNode, ...]
        try:
            for fn in store.list_functions():
                fn_lookup[fn.id] = fn
                bare = fn.name.split("::")[-1] if "::" in fn.name else fn.name
                fn_by_name.setdefault(bare, []).append(fn)
        except Exception:
            pass  # Graceful fallback if store is empty

        # Resolve target_dir for progress.json fallback
        settings = getattr(request.app.state, "settings", None)
        target_dir: Path | None = None
        if settings and hasattr(settings, "project") and hasattr(settings.project, "target_dir"):
            target_dir = Path(settings.project.target_dir)

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
            # Fallback: if Neo4j says "pending" or "running" but progress.json
            # shows a terminal state, trust progress.json (orchestrator crash recovery).
            if resolved_status in ("pending", "running"):
                progress_st = _progress_status(target_dir, func_id or sp_id)
                if progress_st and progress_st not in ("pending", "running"):
                    resolved_status = progress_st
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
                # Fallback: match by bare function name + file path.
                # codewiki_lite IDs: "dir/file.h::NS::Class::Method"
                # Neo4j functions: name="Method", file_path="/abs/.../dir/file.cpp"
                # Header (.h) in codewiki_lite often maps to source (.cpp) in Neo4j.
                parts = func_id.split("::")
                bare_name = parts[-1]
                rel_path = parts[0] if parts[0] and "/" in parts[0] else ""
                candidates = fn_by_name.get(bare_name, [])
                if len(candidates) == 1:
                    fn = candidates[0]
                elif candidates and rel_path:
                    import os.path
                    # Extract stem and parent dir for fuzzy file matching
                    rel_stem = os.path.splitext(os.path.basename(rel_path))[0]
                    # Try: file stem match + directory substring match
                    # e.g. rel_path "castengine_.../mirror_player_impl_stub.h"
                    # matches file_path "/.../mirror_player_impl_stub.cpp"
                    # within the same top-level component directory.
                    rel_dir_parts = rel_path.split("/")
                    top_component = rel_dir_parts[0] if rel_dir_parts else ""
                    best = None
                    for c in candidates:
                        if not c.file_path:
                            continue
                        c_stem = os.path.splitext(os.path.basename(c.file_path))[0]
                        if c_stem == rel_stem and top_component and top_component in c.file_path:
                            best = c
                            break
                    if best is None:
                        # Weaker: just match stem
                        for c in candidates:
                            if c.file_path:
                                c_stem = os.path.splitext(os.path.basename(c.file_path))[0]
                                if c_stem == rel_stem:
                                    best = c
                                    break
                    fn = best if best else candidates[0]
                elif candidates:
                    fn = candidates[0]
            if fn is not None:
                item.setdefault("signature", fn.signature or fn.name)
                item.setdefault("file", fn.file_path)
                item.setdefault("line", fn.start_line)
                # Resolve function_id to the Neo4j short hash so the
                # frontend can query GAPs/repair-logs by caller_id.
                # codewiki_lite uses long path IDs; Neo4j uses 12-char hex.
                item["function_id"] = fn.id
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
        # Fall back to graph store when codewiki_lite entries are empty
        if not entries:
            try:
                store_sps = store.list_source_points()
                entries = [
                    {
                        "id": sp.id,
                        "function_id": sp.function_id,
                        "entry_point_kind": sp.entry_point_kind,
                        "module": sp.module,
                        "status": sp.status,
                    }
                    for sp in store_sps
                ]
            except Exception:
                entries = []
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
    def get_reachable(
        request: Request,
        source_id: str,
        depth: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        store = request.app.state.store
        # Resolve the BFS seed function_id from multiple sources:
        # 1. codewiki_lite entries (app.state.source_points)
        # 2. graph store SourcePointNode
        # 3. fallback: use source_id directly as function_id
        entries = getattr(request.app.state, "source_points", [])
        seed_id = source_id
        for entry in entries:
            if entry.get("id") == source_id:
                resolved = entry.get("function_id")
                if resolved:
                    seed_id = resolved
                break
        else:
            # Not found in codewiki_lite entries — check graph store
            sp = store.get_source_point(source_id)
            if sp is not None and sp.function_id:
                seed_id = sp.function_id
            elif store.get_function_by_id(source_id) is None:
                raise HTTPException(status_code=404, detail="Source point not found")
        subgraph = store.get_reachable_subgraph(seed_id, max_depth=depth)
        if not subgraph["nodes"] and not subgraph["edges"]:
            # Seed function not found in graph
            raise HTTPException(status_code=404, detail="Source point not found")
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

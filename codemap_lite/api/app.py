"""FastAPI application factory with dependency injection."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from codemap_lite.analysis.feedback_store import FeedbackStore
from codemap_lite.graph.neo4j_store import GraphStore, InMemoryGraphStore

from codemap_lite.api.routes.graph import create_graph_router
from codemap_lite.api.routes.source_points import create_source_points_router
from codemap_lite.api.routes.analyze import create_analyze_router
from codemap_lite.api.routes.review import create_review_router
from codemap_lite.api.routes.feedback import create_feedback_router
from codemap_lite.api.routes.repair_logs import create_repair_logs_router


def create_app(
    store: GraphStore | None = None,
    target_dir: Path | None = None,
    feedback_store: FeedbackStore | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        store: GraphStore instance for dependency injection.
               Defaults to InMemoryGraphStore if not provided.
        target_dir: Project target directory (``project.target_dir``
            from ``config.yaml``). Wired into ``app.state`` so routes
            can read repair hook artefacts like
            ``logs/repair/{source_id}/progress.json``
            (architecture.md §3, ADR #52). Optional — tests and pure
            in-memory demos can omit it.
        feedback_store: ``FeedbackStore`` backing ``GET /api/v1/feedback``
            (architecture.md §3 反馈机制 + §8). When ``None`` the endpoint
            returns ``[]`` — used by tests and in-memory demos that do not
            persist counter examples.
    """
    if store is None:
        store = InMemoryGraphStore()

    app = FastAPI(
        title="codemap-lite",
        version="0.1.0",
        description="Code call-graph analysis and repair API",
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store state on app for dependency access
    app.state.store = store
    app.state.reviews: dict[str, dict[str, Any]] = {}
    app.state.analyze_state: dict[str, Any] = {"state": "idle", "progress": 0.0}
    app.state.source_points: list[dict[str, Any]] = []
    app.state.analysis_stats: dict[str, Any] = {}
    # Used by /api/v1/analyze/status to aggregate hook-written
    # progress files (architecture.md §3, ADR #52).
    app.state.target_dir = target_dir
    # Backs GET /api/v1/feedback (architecture.md §3 反馈机制 + §8).
    # None is allowed — endpoint gracefully returns [] in that case.
    app.state.feedback_store = feedback_store

    # Health check
    @app.get("/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    # Root redirect to frontend
    @app.get("/")
    def root_redirect():
        return RedirectResponse(url="/static/index.html")

    # Stats endpoint
    @app.get("/api/v1/stats")
    def get_stats(request: Any = None) -> dict[str, Any]:
        s = app.state.store
        stats = getattr(app.state, "analysis_stats", {})
        # Breakdown by UnresolvedCall.status (architecture.md §3 GAP lifecycle:
        # pending → agent repair → node deleted, or 3 retries → "unresolvable").
        # Surface the unresolvable backlog on the Dashboard so reviewers see
        # GAPs the agent has abandoned without drilling into ReviewQueue
        # (北极星指标 #5 状态透明度).
        by_status: dict[str, int] = {}
        for u in s._unresolved_calls.values():
            key = getattr(u, "status", None) or "pending"
            by_status[key] = by_status.get(key, 0) + 1
        # Breakdown of UnresolvedCall by the `<category>:` prefix of
        # `last_attempt_reason` (architecture.md §3 Retry 审计字段 4 档:
        # gate_failed / agent_error / subprocess_crash / subprocess_timeout).
        # Missing / malformed reasons (no colon, never stamped yet) bucket
        # to "none" so the Dashboard chip row can show "25 GAPs have no
        # audit stamp yet" without silently dropping them. Surfaced on
        # the Dashboard per architecture.md §5 drill-down 契约: chip tones
        # mirror GapDetail last-attempt 分色 and each chip links to
        # `/review?category=<cat>` (北极星指标 #5 状态透明度).
        by_category: dict[str, int] = {}
        for u in s._unresolved_calls.values():
            reason = getattr(u, "last_attempt_reason", None)
            if reason and ":" in reason:
                prefix = reason.split(":", 1)[0].strip()
                cat_key = prefix if prefix else "none"
            else:
                cat_key = "none"
            by_category[cat_key] = by_category.get(cat_key, 0) + 1
        # Breakdown of CALLS edges by resolved_by (architecture.md §4 CALLS
        # 边属性: symbol_table / signature / dataflow / context / llm).
        # Surface the llm-repaired edge backlog on the Dashboard — per §5
        # 审阅对象是"单条 CALLS 边（特别是 resolved_by='llm' 的）", so the
        # review-critical population should be visible at the top level
        # without drilling into ReviewQueue (北极星指标 #2 调用链可信度).
        by_resolved: dict[str, int] = {}
        for e in s._calls_edges:
            key = e.props.resolved_by or "unknown"
            by_resolved[key] = by_resolved.get(key, 0) + 1
        # Counter-example library size (architecture.md §3 反馈机制 + §8).
        # Surfaced here so the frontend can render a live count chip on
        # the left-nav "Feedback" label — reviewers see the library grow
        # without mounting FeedbackLog (北极星指标 #5 状态透明度 +
        # 候选优化方向 #4 进度与可观测性). Falls back to 0 when the
        # store is not wired (tests / in-memory demos).
        fb = getattr(app.state, "feedback_store", None)
        total_feedback = len(fb.list_all()) if fb is not None else 0
        # RepairLog count (architecture.md §3 修复成功时 + §4 RepairLog
        # schema + ADR #51). Surfaces total LLM repair activity so the
        # frontend can render a Dashboard StatCard linking into the
        # repair-logs audit endpoint without reviewers manually
        # spelunking the graph (北极星指标 #2 + #5).
        total_repair_logs = len(getattr(s, "_repair_logs", {}))
        return {
            "total_functions": len(s._functions),
            "total_files": len(s._files),
            "total_calls": len(s._calls_edges),
            "total_unresolved": len(s._unresolved_calls),
            "unresolved_by_status": by_status,
            "unresolved_by_category": by_category,
            "calls_by_resolved_by": by_resolved,
            "total_source_points": len(getattr(app.state, "source_points", [])),
            "total_feedback": total_feedback,
            "total_repair_logs": total_repair_logs,
            **stats,
        }

    # Register routers
    app.include_router(create_graph_router(), prefix="/api/v1")
    app.include_router(create_source_points_router(), prefix="/api/v1")
    app.include_router(create_analyze_router(), prefix="/api/v1")
    app.include_router(create_review_router(), prefix="/api/v1")
    app.include_router(create_feedback_router(), prefix="/api/v1")
    app.include_router(create_repair_logs_router(), prefix="/api/v1")

    # Mount static files (standalone HTML frontend)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app

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
        return {
            "total_functions": len(s._functions),
            "total_files": len(s._files),
            "total_calls": len(s._calls_edges),
            "total_unresolved": len(s._unresolved_calls),
            "total_source_points": len(getattr(app.state, "source_points", [])),
            **stats,
        }

    # Register routers
    app.include_router(create_graph_router(), prefix="/api/v1")
    app.include_router(create_source_points_router(), prefix="/api/v1")
    app.include_router(create_analyze_router(), prefix="/api/v1")
    app.include_router(create_review_router(), prefix="/api/v1")
    app.include_router(create_feedback_router(), prefix="/api/v1")

    # Mount static files (standalone HTML frontend)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app

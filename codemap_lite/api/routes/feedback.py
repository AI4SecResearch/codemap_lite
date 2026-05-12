"""Feedback and stats endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request


def create_feedback_router() -> APIRouter:
    """Create the feedback router."""
    router = APIRouter(tags=["feedback"])

    @router.get("/feedback")
    def list_feedback(request: Request) -> list[dict[str, Any]]:
        # In production, this would query the FeedbackStore.
        # For now, return empty list (no feedback store wired yet).
        return []

    @router.get("/stats")
    def get_stats(request: Request) -> dict[str, Any]:
        store = request.app.state.store
        return {
            "functions_count": len(store._functions),
            "files_count": len(store._files),
            "edges_count": len(store._calls_edges),
            "unresolved_count": len(store._unresolved_calls),
        }

    return router

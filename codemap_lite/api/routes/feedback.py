"""Feedback endpoint — browses counter examples from FeedbackStore.

See architecture.md §3 反馈机制 and §8 REST API (``GET /api/v1/feedback``).
The router serializes ``CounterExample`` dataclasses stored by
:class:`codemap_lite.analysis.feedback_store.FeedbackStore` so the frontend
``FeedbackLog`` page (候选优化方向 #5 反例可视化) can render structured
entries instead of raw JSON dumps.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request


def create_feedback_router() -> APIRouter:
    """Create the feedback router."""
    router = APIRouter(tags=["feedback"])

    @router.get("/feedback")
    def list_feedback(request: Request) -> list[dict[str, Any]]:
        """Return every persisted counter example.

        When ``app.state.feedback_store`` is unset (tests, pure in-memory
        demos) the endpoint returns ``[]`` rather than failing — matching
        the pre-wire stub contract so existing clients keep working.
        """
        feedback_store = getattr(request.app.state, "feedback_store", None)
        if feedback_store is None:
            return []
        return [asdict(example) for example in feedback_store.list_all()]

    return router

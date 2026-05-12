"""Feedback endpoints — browse + add counter examples via FeedbackStore.

See architecture.md §3 反馈机制 (counter-example pipeline), §5 审阅交互
(标记错误时 → 可填写正确目标 → 触发反例生成), and §8 REST API
(``GET /api/v1/feedback`` / ``POST /api/v1/feedback``).

The router serializes ``CounterExample`` dataclasses stored by
:class:`codemap_lite.analysis.feedback_store.FeedbackStore` so the frontend
``FeedbackLog`` page (候选优化方向 #5 反例可视化) can render structured
entries, and accepts new counter examples from the review queue so the
feedback loop's write side is reachable from the UI.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from codemap_lite.analysis.feedback_store import CounterExample


class CounterExampleCreate(BaseModel):
    """Request body for ``POST /api/v1/feedback``.

    Mirrors :class:`CounterExample` one-for-one. All four fields are
    required — architecture.md §3 反馈机制 step 1 expects the tuple
    ``(调用上下文, 错误目标, 正确目标)`` plus a generalized ``pattern``.
    """

    call_context: str
    wrong_target: str
    correct_target: str
    pattern: str


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

    @router.post("/feedback", status_code=201)
    def create_feedback(
        request: Request, body: CounterExampleCreate
    ) -> dict[str, Any]:
        """Persist a new counter example.

        Called by the frontend review queue when a human marks a repair as
        wrong and supplies the correct target (architecture.md §5). The
        stored example is auto-rendered into
        ``<target>/.icslpreprocess/counter_examples.md`` before the next
        repair attempt via ``RepairOrchestrator`` (architecture.md §3 step 4).

        Returns 503 when no store is wired — this happens only in tests/
        in-memory demos; production always mounts a persistent store via
        ``cli serve``.
        """
        feedback_store = getattr(request.app.state, "feedback_store", None)
        if feedback_store is None:
            raise HTTPException(
                status_code=503,
                detail="FeedbackStore not configured on this server.",
            )
        example = CounterExample(
            call_context=body.call_context,
            wrong_target=body.wrong_target,
            correct_target=body.correct_target,
            pattern=body.pattern,
        )
        feedback_store.add(example)
        return asdict(example)

    return router

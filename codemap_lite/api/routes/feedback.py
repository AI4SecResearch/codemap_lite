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

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from codemap_lite.analysis.feedback_store import CounterExample


class CounterExampleCreate(BaseModel):
    """Request body for ``POST /api/v1/feedback``.

    Mirrors :class:`CounterExample` one-for-one. All four fields are
    required — architecture.md §3 反馈机制 step 1 expects the tuple
    ``(调用上下文, 错误目标, 正确目标)`` plus a generalized ``pattern``.
    """

    call_context: str = Field(..., min_length=1)
    wrong_target: str = Field(..., min_length=1)
    correct_target: str = Field(..., min_length=1)
    pattern: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def targets_must_differ(self) -> "CounterExampleCreate":
        if self.wrong_target == self.correct_target:
            raise ValueError("wrong_target must differ from correct_target")
        return self


def create_feedback_router() -> APIRouter:
    """Create the feedback router."""
    router = APIRouter(tags=["feedback"])

    @router.get("/feedback")
    def list_feedback(
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """Return persisted counter examples with pagination.

        When ``app.state.feedback_store`` is unset (tests, pure in-memory
        demos) the endpoint returns ``{"total": 0, "items": []}`` rather
        than failing — matching the pre-wire stub contract.
        """
        feedback_store = getattr(request.app.state, "feedback_store", None)
        if feedback_store is None:
            return {"total": 0, "items": []}
        all_items = [asdict(example) for example in feedback_store.list_all()]
        total = len(all_items)
        items = all_items[offset:offset + limit]
        return {"total": total, "items": items}

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

        The response echoes the persisted example plus two signal fields:

        - ``deduplicated``: ``True`` when the submitted pattern matched an
          existing entry and was merged (architecture.md §3 step 4
          "相似 → 总结合并"); ``False`` when it was appended as a new row.
        - ``total``: the current library size after the operation, so the
          UI can show "N counter examples in library" without an extra
          GET round-trip.

        These extras let the reviewer immediately see whether their
        submission broadened an existing rule or opened a new one — 北极星
        指标 #5 (反例命中——是否都在 UI 上可见).

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
        added = feedback_store.add(example)
        return {
            **asdict(example),
            "deduplicated": not added,
            "total": len(feedback_store.list_all()),
        }

    return router

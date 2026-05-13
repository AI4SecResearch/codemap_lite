"""Review and manual edge management endpoints."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from codemap_lite.graph.schema import CallsEdgeProps

logger = logging.getLogger(__name__)


class ReviewCreate(BaseModel):
    """Request body for creating an edge review (architecture.md §5 审阅交互).

    Edge-centric: identifies the CALLS edge by (caller_id, callee_id, call_file, call_line)
    and records a verdict (correct/incorrect).
    """

    caller_id: str
    callee_id: str
    call_file: str
    call_line: int
    verdict: str
    comment: str | None = None
    correct_target: str | None = None  # §5: "可填写正确目标 → 触发反例生成"

    @field_validator("verdict")
    @classmethod
    def validate_verdict(cls, v: str) -> str:
        allowed = {"correct", "incorrect"}
        if v not in allowed:
            raise ValueError(f"verdict must be one of {sorted(allowed)}, got '{v}'")
        return v


class ReviewUpdate(BaseModel):
    """Request body for updating a review."""

    comment: str | None = None
    status: str | None = None


class EdgeCreate(BaseModel):
    """Request body for creating a manual edge."""

    caller_id: str
    callee_id: str
    resolved_by: str
    call_type: str
    call_file: str
    call_line: int

    @field_validator("resolved_by")
    @classmethod
    def validate_resolved_by(cls, v: str) -> str:
        allowed = {"symbol_table", "signature", "dataflow", "context", "llm", "manual"}
        if v not in allowed:
            raise ValueError(
                f"resolved_by must be one of {sorted(allowed)}, got '{v}'"
            )
        return v

    @field_validator("call_type")
    @classmethod
    def validate_call_type(cls, v: str) -> str:
        allowed = {"direct", "indirect", "virtual"}
        if v not in allowed:
            raise ValueError(
                f"call_type must be one of {sorted(allowed)}, got '{v}'"
            )
        return v


class EdgeDelete(BaseModel):
    """Request body for deleting a specific edge (architecture.md §5)."""

    caller_id: str
    callee_id: str
    call_file: str
    call_line: int


def _trigger_repair_for_source(settings: Any, caller_id: str) -> None:
    """Spawn a single-source repair in the background.

    architecture.md §5 line 328: "触发 Agent 重新修复该 source 点（异步）"
    The caller_id is used as the source_id (same convention as cli.py repair
    which passes sp.function_id to run_repairs).
    """
    try:
        from codemap_lite.analysis.feedback_store import FeedbackStore
        from codemap_lite.analysis.repair_orchestrator import (
            RepairConfig,
            RepairOrchestrator,
        )
        from codemap_lite.cli import _backend_subprocess, _build_graph_store

        command, args = _backend_subprocess(settings)
        target_dir = Path(settings.project.target_dir)
        feedback_store = FeedbackStore(
            storage_dir=target_dir / ".codemap_lite" / "feedback"
        )
        graph_store = _build_graph_store(settings)

        orch = RepairOrchestrator(
            RepairConfig(
                target_dir=target_dir,
                backend=settings.agent.backend,
                command=command,
                args=args,
                max_concurrency=1,
                neo4j_uri=settings.neo4j.uri,
                neo4j_user=settings.neo4j.user,
                neo4j_password=settings.neo4j.password,
                feedback_store=feedback_store,
                graph_store=graph_store,
                retry_failed_gaps=False,
                subprocess_timeout_seconds=settings.agent.subprocess_timeout_seconds,
            )
        )
        asyncio.run(orch.run_repairs([caller_id]))
    except Exception as exc:
        logger.warning("Background repair trigger failed for %s: %s", caller_id, exc)


def create_review_router() -> APIRouter:
    """Create the review router."""
    router = APIRouter(tags=["review"])

    @router.get("/reviews")
    def list_reviews(request: Request) -> list[dict[str, Any]]:
        return list(request.app.state.reviews.values())

    @router.post("/reviews", status_code=201)
    def create_review(
        request: Request, body: ReviewCreate, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        """Mark an edge as correct or incorrect (architecture.md §5 审阅交互).

        - verdict=correct: record approval, edge stays
        - verdict=incorrect: delete edge + RepairLog, regenerate UC, trigger repair
        """
        from codemap_lite.graph.schema import UnresolvedCallNode

        store = request.app.state.store

        # Verify the edge exists
        edge_found = False
        call_type = "indirect"
        if hasattr(store, "list_calls_edges"):
            for edge in store.list_calls_edges():
                if (
                    edge.caller_id == body.caller_id
                    and edge.callee_id == body.callee_id
                    and edge.props.call_file == body.call_file
                    and edge.props.call_line == body.call_line
                ):
                    edge_found = True
                    call_type = edge.props.call_type
                    break

        if not edge_found:
            raise HTTPException(status_code=404, detail="Edge not found")

        review_id = str(uuid4())
        review = {
            "id": review_id,
            "caller_id": body.caller_id,
            "callee_id": body.callee_id,
            "call_file": body.call_file,
            "call_line": body.call_line,
            "verdict": body.verdict,
            "comment": body.comment,
        }

        if body.verdict == "incorrect":
            # architecture.md §5 标记错误时 4-step flow:
            # Step 1: Delete the CALLS edge
            store.delete_calls_edge(
                caller_id=body.caller_id,
                callee_id=body.callee_id,
                call_file=body.call_file,
                call_line=body.call_line,
            )
            # Step 2: Delete corresponding RepairLog
            call_location = f"{body.call_file}:{body.call_line}"
            store.delete_repair_logs_for_edge(
                caller_id=body.caller_id,
                callee_id=body.callee_id,
                call_location=call_location,
            )
            # Step 3: Regenerate UnresolvedCall (retry_count=0)
            uc = UnresolvedCallNode(
                caller_id=body.caller_id,
                call_expression="",
                call_file=body.call_file,
                call_line=body.call_line,
                call_type=call_type,
                source_code_snippet="",
                var_name=None,
                var_type=None,
                retry_count=0,
                status="pending",
            )
            store.create_unresolved_call(uc)

            # architecture.md §5: if correct_target provided, create
            # counter-example in FeedbackStore (反例生成).
            if body.correct_target:
                feedback_store = getattr(request.app.state, "feedback_store", None)
                if feedback_store is not None:
                    from codemap_lite.analysis.feedback_store import CounterExample

                    example = CounterExample(
                        call_context=f"{body.call_file}:{body.call_line}",
                        wrong_target=body.callee_id,
                        correct_target=body.correct_target,
                        pattern=f"{body.caller_id} -> {body.callee_id} at {body.call_file}:{body.call_line}",
                    )
                    feedback_store.add(example)

            # Step 4: Trigger async repair
            settings = getattr(request.app.state, "settings", None)
            if settings is not None:
                background_tasks.add_task(
                    _trigger_repair_for_source, settings, body.caller_id
                )

            # Reset SourcePoint status to "pending" so the frontend reflects
            # that this source needs re-processing (architecture.md §5 step 4:
            # "触发 Agent 重新修复该 source 点" implies status must revert).
            if hasattr(store, "update_source_point_status"):
                sp = store.get_source_point(body.caller_id)
                if sp is not None and sp.status != "pending":
                    store.update_source_point_status(body.caller_id, "pending")

        request.app.state.reviews[review_id] = review
        return review

    @router.put("/reviews/{review_id}")
    def update_review(
        request: Request, review_id: str, body: ReviewUpdate
    ) -> dict[str, Any]:
        reviews = request.app.state.reviews
        if review_id not in reviews:
            raise HTTPException(status_code=404, detail="Review not found")
        review = reviews[review_id]
        if body.comment is not None:
            review["comment"] = body.comment
        if body.status is not None:
            review["status"] = body.status
        return review

    @router.delete("/reviews/{review_id}", status_code=204)
    def delete_review(request: Request, review_id: str) -> Response:
        reviews = request.app.state.reviews
        if review_id not in reviews:
            raise HTTPException(status_code=404, detail="Review not found")
        del reviews[review_id]
        return Response(status_code=204)

    @router.post("/edges", status_code=201)
    def create_edge(request: Request, body: EdgeCreate) -> dict[str, Any]:
        store = request.app.state.store
        # Validate that both functions exist (architecture.md §8: edges
        # reference valid Function nodes)
        if store.get_function_by_id(body.caller_id) is None:
            raise HTTPException(status_code=404, detail="Caller function not found")
        if store.get_function_by_id(body.callee_id) is None:
            raise HTTPException(status_code=404, detail="Callee function not found")
        props = CallsEdgeProps(
            resolved_by=body.resolved_by,
            call_type=body.call_type,
            call_file=body.call_file,
            call_line=body.call_line,
        )
        store.create_calls_edge(body.caller_id, body.callee_id, props)
        return {
            "caller_id": body.caller_id,
            "callee_id": body.callee_id,
            "status": "created",
        }

    @router.delete("/edges", status_code=204)
    def delete_edge(
        request: Request, body: EdgeDelete, background_tasks: BackgroundTasks
    ) -> Response:
        """Delete a specific CALLS edge + corresponding RepairLog + regenerate UC.

        architecture.md §5 审阅交互 lines 324-328:
        '标记错误时 → 立即删除该 CALLS 边 + 对应 RepairLog →
        重新生成 UnresolvedCall 节点（retry_count=0） →
        触发 Agent 重新修复该 source 点（异步）'.
        """
        from codemap_lite.graph.schema import UnresolvedCallNode

        store = request.app.state.store

        # Capture edge call_type before deletion (needed for UC regeneration)
        call_type = "indirect"  # default fallback
        if hasattr(store, "list_calls_edges"):
            for edge in store.list_calls_edges():
                if (
                    edge.caller_id == body.caller_id
                    and edge.callee_id == body.callee_id
                    and edge.props.call_file == body.call_file
                    and edge.props.call_line == body.call_line
                ):
                    call_type = edge.props.call_type
                    break

        # Step 1: Delete the CALLS edge
        deleted = store.delete_calls_edge(
            caller_id=body.caller_id,
            callee_id=body.callee_id,
            call_file=body.call_file,
            call_line=body.call_line,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Edge not found")

        # Step 2: Delete corresponding RepairLog (architecture.md §5 line 326)
        call_location = f"{body.call_file}:{body.call_line}"
        store.delete_repair_logs_for_edge(
            caller_id=body.caller_id,
            callee_id=body.callee_id,
            call_location=call_location,
        )

        # Step 3: Regenerate UnresolvedCall (architecture.md §5 line 327)
        uc = UnresolvedCallNode(
            caller_id=body.caller_id,
            call_expression="",
            call_file=body.call_file,
            call_line=body.call_line,
            call_type=call_type,
            source_code_snippet="",
            var_name=None,
            var_type=None,
            retry_count=0,
            status="pending",
        )
        store.create_unresolved_call(uc)

        # Step 4: Trigger async repair (architecture.md §5 line 328)
        settings = getattr(request.app.state, "settings", None)
        if settings is not None:
            background_tasks.add_task(
                _trigger_repair_for_source, settings, body.caller_id
            )

        # Reset SourcePoint status to "pending" (same as review verdict=incorrect)
        if hasattr(store, "update_source_point_status"):
            sp = store.get_source_point(body.caller_id)
            if sp is not None and sp.status != "pending":
                store.update_source_point_status(body.caller_id, "pending")

        return Response(status_code=204)

    @router.delete("/edges/{function_id}", status_code=204)
    def delete_edges_for_function(request: Request, function_id: str) -> Response:
        """Bulk-delete all edges touching a function (used by incremental invalidation)."""
        store = request.app.state.store
        store.delete_calls_edges_for_function(function_id)
        return Response(status_code=204)

    return router

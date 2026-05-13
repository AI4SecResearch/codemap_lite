"""Review and manual edge management endpoints."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from codemap_lite.graph.schema import CallsEdgeProps


class ReviewCreate(BaseModel):
    """Request body for creating a review."""

    function_id: str
    comment: str
    status: str


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


def create_review_router() -> APIRouter:
    """Create the review router."""
    router = APIRouter(tags=["review"])

    @router.get("/reviews")
    def list_reviews(request: Request) -> list[dict[str, Any]]:
        return list(request.app.state.reviews.values())

    @router.post("/reviews", status_code=201)
    def create_review(request: Request, body: ReviewCreate) -> dict[str, Any]:
        review_id = str(uuid4())
        review = {
            "id": review_id,
            "function_id": body.function_id,
            "comment": body.comment,
            "status": body.status,
        }
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
    def delete_edge(request: Request, body: EdgeDelete) -> Response:
        """Delete a specific CALLS edge + corresponding RepairLog + regenerate UC.

        architecture.md §5 审阅交互 lines 326-327:
        '标记错误时 → 立即删除该 CALLS 边 + 对应 RepairLog →
        重新生成 UnresolvedCall 节点（retry_count=0）'.
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

        return Response(status_code=204)

    @router.delete("/edges/{function_id}", status_code=204)
    def delete_edges_for_function(request: Request, function_id: str) -> Response:
        """Bulk-delete all edges touching a function (used by incremental invalidation)."""
        store = request.app.state.store
        store.delete_calls_edges_for_function(function_id)
        return Response(status_code=204)

    return router

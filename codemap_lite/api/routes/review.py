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

    @router.delete("/edges/{function_id}", status_code=204)
    def delete_edges(request: Request, function_id: str) -> Response:
        store = request.app.state.store
        store.delete_calls_edges_for_function(function_id)
        return Response(status_code=204)

    return router

"""Analysis trigger endpoints."""
from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel


class AnalyzeMode(str, Enum):
    """Valid analysis modes."""

    full = "full"
    incremental = "incremental"


class AnalyzeRequest(BaseModel):
    """Request body for triggering analysis."""

    mode: AnalyzeMode


def create_analyze_router() -> APIRouter:
    """Create the analyze router."""
    router = APIRouter(tags=["analyze"])

    @router.post("/analyze", status_code=202)
    def trigger_analyze(request: Request, body: AnalyzeRequest) -> dict[str, Any]:
        request.app.state.analyze_state = {
            "state": "running",
            "mode": body.mode.value,
            "progress": 0.0,
        }
        return {"status": "accepted", "mode": body.mode.value}

    @router.post("/analyze/repair", status_code=202)
    def trigger_repair(request: Request) -> dict[str, Any]:
        request.app.state.analyze_state = {
            "state": "repairing",
            "progress": 0.0,
        }
        return {"status": "accepted", "action": "repair"}

    @router.get("/analyze/status")
    def get_status(request: Request) -> dict[str, Any]:
        return request.app.state.analyze_state

    return router

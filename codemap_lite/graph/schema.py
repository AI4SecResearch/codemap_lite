"""Node and relationship type definitions for the call graph."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4


class NodeType(Enum):
    """Types of nodes in the call graph."""

    FILE = "FILE"
    FUNCTION = "FUNCTION"
    SOURCE_POINT = "SOURCE_POINT"
    UNRESOLVED_CALL = "UNRESOLVED_CALL"
    REPAIR_LOG = "REPAIR_LOG"


class RelationType(Enum):
    """Types of relationships in the call graph."""

    DEFINES = "DEFINES"
    CALLS = "CALLS"
    HAS_GAP = "HAS_GAP"
    IS_SOURCE = "IS_SOURCE"


# --- Node property dataclasses ---


@dataclass(frozen=True)
class FileNode:
    """Properties for a FILE node."""

    file_path: str
    hash: str
    primary_language: str
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class FunctionNode:
    """Properties for a FUNCTION node."""

    signature: str
    name: str
    file_path: str
    start_line: int
    end_line: int
    body_hash: str
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class SourcePointNode:
    """Properties for a SOURCE_POINT node."""

    entry_point_kind: str
    reason: str
    function_id: str
    status: str
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class UnresolvedCallNode:
    """Properties for an UNRESOLVED_CALL node."""

    caller_id: str
    call_expression: str
    call_file: str
    call_line: int
    call_type: str
    source_code_snippet: str
    var_name: str | None
    var_type: str | None
    candidates: list[str] = field(default_factory=list)
    retry_count: int = 0
    status: str = "pending"
    last_attempt_timestamp: str | None = None
    last_attempt_reason: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))

    def __hash__(self) -> int:
        return hash(self.id)


@dataclass(frozen=True)
class RepairLogNode:
    """Properties for a REPAIR_LOG node."""

    caller_id: str
    callee_id: str
    call_location: str
    repair_method: str
    llm_response: str
    timestamp: str
    reasoning_summary: str
    id: str = field(default_factory=lambda: str(uuid4()))


# --- Edge property dataclasses ---


@dataclass(frozen=True)
class CallsEdgeProps:
    """Properties for a CALLS relationship."""

    resolved_by: str
    call_type: str
    call_file: str
    call_line: int


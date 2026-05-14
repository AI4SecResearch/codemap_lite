"""Node and relationship type definitions for the call graph."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

# --- Architecture §4 enum constants ---

VALID_UC_STATUSES = frozenset({"pending", "unresolvable"})
VALID_RESOLVED_BY = frozenset({"symbol_table", "signature", "dataflow", "context", "llm"})
VALID_CALL_TYPES = frozenset({"direct", "indirect", "virtual"})
VALID_SOURCE_POINT_STATUSES = frozenset({"pending", "running", "complete", "partial_complete"})
VALID_REASON_CATEGORIES = frozenset({
    "gate_failed", "agent_error", "subprocess_timeout", "subprocess_crash",
})

# Forward-only transition map: current_status → set of allowed next statuses
_SOURCE_POINT_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running"}),
    "running": frozenset({"complete", "partial_complete"}),
    "complete": frozenset(),
    "partial_complete": frozenset(),
}


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
    module: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if self.status not in VALID_SOURCE_POINT_STATUSES:
            raise ValueError(
                f"SourcePoint.status must be one of {sorted(VALID_SOURCE_POINT_STATUSES)}, "
                f"got '{self.status}'"
            )


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

    def __post_init__(self) -> None:
        if self.status not in VALID_UC_STATUSES:
            raise ValueError(
                f"UnresolvedCall.status must be one of {sorted(VALID_UC_STATUSES)}, "
                f"got '{self.status}'"
            )

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

    def __post_init__(self) -> None:
        if self.resolved_by not in VALID_RESOLVED_BY:
            raise ValueError(
                f"CALLS.resolved_by must be one of {sorted(VALID_RESOLVED_BY)}, "
                f"got '{self.resolved_by}'"
            )
        if self.call_type not in VALID_CALL_TYPES:
            raise ValueError(
                f"CALLS.call_type must be one of {sorted(VALID_CALL_TYPES)}, "
                f"got '{self.call_type}'"
            )


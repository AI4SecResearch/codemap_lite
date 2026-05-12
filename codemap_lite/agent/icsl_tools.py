"""icsl_tools — Agent-side CLI tool for graph query, edge writing, and gate checking.

This module is copied to the target code directory during repair and invoked by the
CLI agent subprocess. It provides three operations:
- query-reachable: Get the reachable subgraph from a source point
- write-edge: Write a CALLS edge + RepairLog, delete the UnresolvedCall
- check-complete: Check if all reachable GAPs are resolved
"""
from __future__ import annotations

import time
from typing import Any, Protocol


class GraphStoreProtocol(Protocol):
    """Protocol for graph store operations needed by icsl_tools."""

    def get_reachable_subgraph(self, source_id: str, max_depth: int = 50) -> dict[str, Any]: ...
    def edge_exists(self, caller_id: str, callee_id: str, call_file: str, call_line: int) -> bool: ...
    def create_calls_edge(self, caller_id: str, callee_id: str, props: dict[str, Any]) -> None: ...
    def create_repair_log(self, log_data: dict[str, Any]) -> None: ...
    def delete_unresolved_call(self, caller_id: str, call_file: str, call_line: int) -> None: ...
    def get_pending_gaps_for_source(self, source_id: str) -> list[dict[str, Any]]: ...


def query_reachable(source_id: str, store: GraphStoreProtocol) -> dict[str, Any]:
    """Query the reachable subgraph from a source point."""
    return store.get_reachable_subgraph(source_id)


def write_edge(
    caller_id: str,
    callee_id: str,
    call_type: str,
    call_file: str,
    call_line: int,
    store: GraphStoreProtocol,
) -> dict[str, Any]:
    """Write a CALLS edge, create RepairLog, and delete the UnresolvedCall."""
    # Check if edge already exists (skip if so)
    if store.edge_exists(caller_id, callee_id, call_file, call_line):
        return {"skipped": True, "reason": "edge already exists"}

    # Create the CALLS edge
    props = {
        "resolved_by": "llm",
        "call_type": call_type,
        "call_file": call_file,
        "call_line": call_line,
    }
    store.create_calls_edge(caller_id, callee_id, props)

    # Create RepairLog
    repair_log = {
        "caller_id": caller_id,
        "callee_id": callee_id,
        "call_location": f"{call_file}:{call_line}",
        "repair_method": "llm",
        "timestamp": time.time(),
    }
    store.create_repair_log(repair_log)

    # Delete the UnresolvedCall
    store.delete_unresolved_call(caller_id, call_file, call_line)

    return {"skipped": False, "edge_created": True}


def check_complete(source_id: str, store: GraphStoreProtocol) -> dict[str, Any]:
    """Check if all reachable GAPs for a source point are resolved."""
    pending = store.get_pending_gaps_for_source(source_id)
    return {
        "complete": len(pending) == 0,
        "remaining_gaps": len(pending),
        "pending_gap_ids": [g["id"] for g in pending],
    }

"""Incremental update — 5-step cascade invalidation logic."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import FunctionNode


@dataclass
class InvalidationResult:
    """Result of invalidating a file."""

    removed_functions: list[str] = field(default_factory=list)
    removed_edges: int = 0
    removed_unresolved_calls: list[str] = field(default_factory=list)
    affected_callers: list[str] = field(default_factory=list)


class IncrementalUpdater:
    """Handles incremental updates with cascade invalidation."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def _get_functions_in_file(self, file_path: str) -> list[FunctionNode]:
        """Get all functions defined in a specific file."""
        if hasattr(self._store, '_functions'):
            return [f for f in self._store._functions.values() if f.file_path == file_path]
        return []

    def invalidate_file(self, file_path: str) -> InvalidationResult:
        """Invalidate all data associated with a changed file.

        5-step cascade:
        1. Find all functions in the file
        2. Find LLM edges pointing TO these functions (from other files)
        3. Delete functions + their edges
        4. Mark affected callers for re-repair
        5. Return affected callers for orchestrator to handle
        """
        result = InvalidationResult()

        # Step 1: Find functions in this file
        functions = self._get_functions_in_file(file_path)
        function_ids = {f.id for f in functions}
        result.removed_functions = list(function_ids)

        # Step 2: Find LLM edges from OTHER functions pointing to functions in this file
        # These need cascade invalidation
        if hasattr(self._store, '_calls_edges'):
            for edge in self._store._calls_edges:
                if edge.callee_id in function_ids and edge.caller_id not in function_ids:
                    if edge.props.resolved_by == "llm":
                        result.affected_callers.append(edge.caller_id)

        # Step 3: Delete functions, their edges, and associated UnresolvedCalls
        # architecture.md §7: "删除旧 Function 节点及关联 CALLS 边 + UnresolvedCall"
        for fid in function_ids:
            self._store.delete_calls_edges_for_function(fid)
            # Delete UnresolvedCalls where this function is the caller
            if hasattr(self._store, '_unresolved_calls'):
                victims = [
                    cid for cid, node in self._store._unresolved_calls.items()
                    if node.caller_id == fid
                ]
                for cid in victims:
                    self._store._unresolved_calls.pop(cid, None)
                result.removed_unresolved_calls.extend(victims)
            self._store.delete_function(fid)

        # Step 4: Remove LLM edges from affected callers that pointed to deleted functions
        # (already handled by delete_calls_edges_for_function above)

        return result

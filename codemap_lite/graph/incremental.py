"""Incremental update — 5-step cascade invalidation logic."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode


@dataclass
class InvalidationResult:
    """Result of invalidating a file."""

    removed_functions: list[str] = field(default_factory=list)
    removed_edges: int = 0
    removed_unresolved_calls: list[str] = field(default_factory=list)
    affected_callers: list[str] = field(default_factory=list)
    regenerated_unresolved_calls: list[str] = field(default_factory=list)


class IncrementalUpdater:
    """Handles incremental updates with cascade invalidation."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def _get_functions_in_file(self, file_path: str) -> list[FunctionNode]:
        """Get all functions defined in a specific file."""
        return self._store.list_functions(file_path=file_path)

    def invalidate_file(self, file_path: str) -> InvalidationResult:
        """Invalidate all data associated with a changed file.

        5-step cascade (architecture.md §7):
        1. Find all functions in the file
        2. Find edges pointing TO these functions (from other files);
           LLM edges → regenerate UnresolvedCall; non-LLM edges →
           mark caller's file for re-parse
        3. Delete functions + their edges + UnresolvedCalls
        4. Mark affected callers for re-repair
        5. Return affected callers for orchestrator to handle
        """
        result = InvalidationResult()

        # Step 1: Find functions in this file
        functions = self._get_functions_in_file(file_path)
        function_ids = {f.id for f in functions}
        result.removed_functions = list(function_ids)

        # Step 2: Find edges from OTHER functions pointing to functions
        # in this file. Capture edge details before deletion so we can
        # regenerate UnresolvedCalls (architecture.md §7 step 3).
        invalidated_llm_edges: list[tuple[str, str, Any]] = []
        for edge in self._store.list_calls_edges():
            if edge.callee_id in function_ids and edge.caller_id not in function_ids:
                if edge.props.resolved_by == "llm":
                    result.affected_callers.append(edge.caller_id)
                    invalidated_llm_edges.append(
                        (edge.caller_id, edge.callee_id, edge.props)
                    )
                else:
                    # Non-LLM edges: the caller's file needs re-parsing
                    # to re-discover the edge via static analysis.
                    result.affected_callers.append(edge.caller_id)

        # Step 3: Delete functions, their edges, and associated UnresolvedCalls
        # architecture.md §7: "删除旧 Function 节点及关联 CALLS 边 + UnresolvedCall"
        # Count total edges before bulk deletion (single pass, not per-function)
        edges_before = len(self._store.list_calls_edges())
        for fid in function_ids:
            self._store.delete_calls_edges_for_function(fid)
            # Delete UnresolvedCalls where this function is the caller
            gaps = self._store.get_unresolved_calls(caller_id=fid)
            for gap in gaps:
                self._store.delete_unresolved_call(
                    caller_id=gap.caller_id,
                    call_file=gap.call_file,
                    call_line=gap.call_line,
                )
                result.removed_unresolved_calls.append(gap.id)
            self._store.delete_function(fid)
        result.removed_edges = edges_before - len(self._store.list_calls_edges())

        # Step 3b: Delete RepairLogs and regenerate UnresolvedCalls for
        # affected LLM callers.
        # architecture.md §7 step 3: "删除该 CALLS 边 + 对应 RepairLog，重新生成 UnresolvedCall"
        affected_source_ids: set[str] = set()
        for caller_id, callee_id, props in invalidated_llm_edges:
            # Delete the RepairLog that documented this LLM repair
            call_location = f"{props.call_file}:{props.call_line}"
            self._store.delete_repair_logs_for_edge(
                caller_id=caller_id,
                callee_id=callee_id,
                call_location=call_location,
            )
            # Regenerate UnresolvedCall so the repair agent can re-attempt
            gap = UnresolvedCallNode(
                caller_id=caller_id,
                call_expression="",  # Original expression not stored on edge
                call_file=props.call_file,
                call_line=props.call_line,
                call_type=props.call_type,
                source_code_snippet="",
                var_name="",
                var_type="",
                retry_count=0,
                status="pending",
            )
            self._store.create_unresolved_call(gap)
            result.regenerated_unresolved_calls.append(gap.id)
            affected_source_ids.add(caller_id)

        # Step 4: Reset SourcePoint status to "pending" for affected sources
        # so the repair orchestrator will re-process them.
        # architecture.md §7 + §3: invalidation regenerates pending GAPs,
        # so the source must transition back to allow re-repair.
        for source_id in affected_source_ids:
            sp = self._store.get_source_point(source_id)
            if sp is not None and getattr(sp, "status", "") != "pending":
                self._store.update_source_point_status(source_id, "pending")

        return result

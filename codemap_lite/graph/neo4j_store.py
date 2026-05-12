"""Graph store protocol and implementations."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    UnresolvedCallNode,
)


@runtime_checkable
class GraphStore(Protocol):
    """Protocol defining the graph storage interface."""

    def create_function(self, node: FunctionNode) -> str: ...

    def create_file(self, node: FileNode) -> str: ...

    def create_calls_edge(
        self, caller_id: str, callee_id: str, props: CallsEdgeProps
    ) -> None: ...

    def create_unresolved_call(self, node: UnresolvedCallNode) -> str: ...

    def get_function_by_id(self, id: str) -> FunctionNode | None: ...

    def get_callers(self, function_id: str) -> list[FunctionNode]: ...

    def get_callees(self, function_id: str) -> list[FunctionNode]: ...

    def get_unresolved_calls(
        self, caller_id: str | None = None, status: str | None = None
    ) -> list[UnresolvedCallNode]: ...

    def update_unresolved_call_retry_state(
        self, call_id: str, timestamp: str, reason: str
    ) -> None: ...

    def delete_function(self, id: str) -> None: ...

    def delete_calls_edges_for_function(self, function_id: str) -> None: ...

    def get_reachable_subgraph(
        self, source_id: str, max_depth: int = 50
    ) -> dict: ...


@dataclass
class _CallsEdge:
    """Internal representation of a CALLS edge."""

    caller_id: str
    callee_id: str
    props: CallsEdgeProps


class InMemoryGraphStore:
    """In-memory implementation of GraphStore for testing."""

    def __init__(self) -> None:
        self._functions: dict[str, FunctionNode] = {}
        self._files: dict[str, FileNode] = {}
        self._calls_edges: list[_CallsEdge] = []
        self._unresolved_calls: dict[str, UnresolvedCallNode] = {}

    def create_function(self, node: FunctionNode) -> str:
        self._functions[node.id] = node
        return node.id

    def create_file(self, node: FileNode) -> str:
        self._files[node.id] = node
        return node.id

    def create_calls_edge(
        self, caller_id: str, callee_id: str, props: CallsEdgeProps
    ) -> None:
        self._calls_edges.append(_CallsEdge(caller_id, callee_id, props))

    def create_unresolved_call(self, node: UnresolvedCallNode) -> str:
        self._unresolved_calls[node.id] = node
        return node.id

    def get_function_by_id(self, id: str) -> FunctionNode | None:
        return self._functions.get(id)

    def get_callers(self, function_id: str) -> list[FunctionNode]:
        caller_ids = [
            e.caller_id for e in self._calls_edges if e.callee_id == function_id
        ]
        return [
            self._functions[cid] for cid in caller_ids if cid in self._functions
        ]

    def get_callees(self, function_id: str) -> list[FunctionNode]:
        callee_ids = [
            e.callee_id for e in self._calls_edges if e.caller_id == function_id
        ]
        return [
            self._functions[cid] for cid in callee_ids if cid in self._functions
        ]

    def get_unresolved_calls(
        self, caller_id: str | None = None, status: str | None = None
    ) -> list[UnresolvedCallNode]:
        results = list(self._unresolved_calls.values())
        if caller_id is not None:
            results = [n for n in results if n.caller_id == caller_id]
        if status is not None:
            results = [n for n in results if n.status == status]
        return results

    def update_unresolved_call_retry_state(
        self, call_id: str, timestamp: str, reason: str
    ) -> None:
        """Stamp the latest retry outcome onto an UnresolvedCall.

        architecture.md §3 Retry 审计字段: each time Orchestrator bumps
        retry_count, it must record when + why so the frontend GapDetail
        can surface the last failed attempt without trawling JSONL logs.
        """
        existing = self._unresolved_calls.get(call_id)
        if existing is None:
            return
        replaced = UnresolvedCallNode(
            caller_id=existing.caller_id,
            call_expression=existing.call_expression,
            call_file=existing.call_file,
            call_line=existing.call_line,
            call_type=existing.call_type,
            source_code_snippet=existing.source_code_snippet,
            var_name=existing.var_name,
            var_type=existing.var_type,
            candidates=list(existing.candidates),
            retry_count=existing.retry_count,
            status=existing.status,
            last_attempt_timestamp=timestamp,
            last_attempt_reason=reason,
            id=existing.id,
        )
        self._unresolved_calls[call_id] = replaced

    def delete_function(self, id: str) -> None:
        self._functions.pop(id, None)

    def delete_calls_edges_for_function(self, function_id: str) -> None:
        self._calls_edges = [
            e
            for e in self._calls_edges
            if e.caller_id != function_id and e.callee_id != function_id
        ]

    def get_reachable_subgraph(
        self, source_id: str, max_depth: int = 50
    ) -> dict:
        """BFS traversal from source_id, collecting nodes, edges, unresolved."""
        visited: set[str] = set()
        nodes: list[FunctionNode] = []
        edges: list[_CallsEdge] = []
        queue: deque[tuple[str, int]] = deque()

        queue.append((source_id, 0))
        visited.add(source_id)

        while queue:
            current_id, depth = queue.popleft()
            fn = self._functions.get(current_id)
            if fn is not None:
                nodes.append(fn)

            if depth >= max_depth:
                continue

            for edge in self._calls_edges:
                if edge.caller_id == current_id:
                    # Defense in depth: skip edges whose callee has no
                    # corresponding FunctionNode — rendering them would
                    # produce dangling targets in the frontend graph.
                    if edge.callee_id not in self._functions:
                        continue
                    edges.append(edge)
                    if edge.callee_id not in visited:
                        visited.add(edge.callee_id)
                        queue.append((edge.callee_id, depth + 1))

        # Collect unresolved calls for all visited nodes
        unresolved = [
            n
            for n in self._unresolved_calls.values()
            if n.caller_id in visited
        ]

        return {"nodes": nodes, "edges": edges, "unresolved": unresolved}


class Neo4jGraphStore:
    """Real Neo4j implementation of GraphStore.

    Structured for production use with the neo4j Python driver.
    Currently raises NotImplementedError — to be wired up when a
    Neo4j instance is available.
    """

    def __init__(self, uri: str, user: str, password: str) -> None:
        self._uri = uri
        self._user = user
        self._password = password

    def create_function(self, node: FunctionNode) -> str:
        raise NotImplementedError("Neo4j driver not yet wired")

    def create_file(self, node: FileNode) -> str:
        raise NotImplementedError("Neo4j driver not yet wired")

    def create_calls_edge(
        self, caller_id: str, callee_id: str, props: CallsEdgeProps
    ) -> None:
        raise NotImplementedError("Neo4j driver not yet wired")

    def create_unresolved_call(self, node: UnresolvedCallNode) -> str:
        raise NotImplementedError("Neo4j driver not yet wired")

    def get_function_by_id(self, id: str) -> FunctionNode | None:
        raise NotImplementedError("Neo4j driver not yet wired")

    def get_callers(self, function_id: str) -> list[FunctionNode]:
        raise NotImplementedError("Neo4j driver not yet wired")

    def get_callees(self, function_id: str) -> list[FunctionNode]:
        raise NotImplementedError("Neo4j driver not yet wired")

    def get_unresolved_calls(
        self, caller_id: str | None = None, status: str | None = None
    ) -> list[UnresolvedCallNode]:
        raise NotImplementedError("Neo4j driver not yet wired")

    def update_unresolved_call_retry_state(
        self, call_id: str, timestamp: str, reason: str
    ) -> None:
        raise NotImplementedError("Neo4j driver not yet wired")

    def delete_function(self, id: str) -> None:
        raise NotImplementedError("Neo4j driver not yet wired")

    def delete_calls_edges_for_function(self, function_id: str) -> None:
        raise NotImplementedError("Neo4j driver not yet wired")

    def get_reachable_subgraph(
        self, source_id: str, max_depth: int = 50
    ) -> dict:
        raise NotImplementedError("Neo4j driver not yet wired")


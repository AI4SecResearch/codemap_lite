"""Query engine for traversing the call graph."""
from __future__ import annotations

from collections import deque

from codemap_lite.graph.neo4j_store import GraphStore
from codemap_lite.graph.schema import FunctionNode


class QueryEngine:
    """High-level query operations over a GraphStore."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    def get_call_chain(
        self, function_id: str, depth: int = 5
    ) -> list[list[FunctionNode]]:
        """Return all call paths from function_id up to the given depth.

        Each path is a list of FunctionNode starting from the source function.
        Uses BFS to discover paths.
        """
        paths: list[list[FunctionNode]] = []
        source = self._store.get_function_by_id(function_id)
        if source is None:
            return paths

        # BFS: queue holds (current_node, path_so_far)
        queue: deque[tuple[FunctionNode, list[FunctionNode]]] = deque()
        queue.append((source, [source]))

        while queue:
            current, path = queue.popleft()
            callees = self._store.get_callees(current.id)

            if not callees:
                # Leaf node — record the path if it has more than just source
                if len(path) > 1:
                    paths.append(path)
                continue

            for callee in callees:
                if callee.id in {n.id for n in path}:
                    # Avoid cycles — record path up to here
                    paths.append(path)
                    continue
                new_path = path + [callee]
                if len(new_path) - 1 >= depth:
                    paths.append(new_path)
                else:
                    queue.append((callee, new_path))

        # If source has callees but all paths were extended, ensure we
        # still return the single-node path if nothing else was found
        if not paths and source is not None:
            paths.append([source])

        return paths

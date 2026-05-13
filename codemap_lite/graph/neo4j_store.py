"""Graph store protocol and implementations."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    RepairLogNode,
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

    def edge_exists(
        self, caller_id: str, callee_id: str, call_file: str, call_line: int
    ) -> bool: ...

    def delete_unresolved_call(
        self, caller_id: str, call_file: str, call_line: int
    ) -> None: ...

    def get_pending_gaps_for_source(
        self, source_id: str
    ) -> list[UnresolvedCallNode]: ...

    def update_unresolved_call_retry_state(
        self, call_id: str, timestamp: str, reason: str
    ) -> None: ...

    def create_repair_log(self, node: RepairLogNode) -> str: ...

    def get_repair_logs(
        self,
        caller_id: str | None = None,
        callee_id: str | None = None,
        call_location: str | None = None,
    ) -> list[RepairLogNode]: ...

    def delete_function(self, id: str) -> None: ...

    def delete_calls_edges_for_function(self, function_id: str) -> None: ...

    def list_files(self) -> list[FileNode]: ...

    def list_functions(
        self, file_path: str | None = None
    ) -> list[FunctionNode]: ...

    def list_calls_edges(self) -> list["_CallsEdge"]: ...

    def count_stats(self) -> dict: ...

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
        self._repair_logs: dict[str, RepairLogNode] = {}

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

    def edge_exists(
        self, caller_id: str, callee_id: str, call_file: str, call_line: int
    ) -> bool:
        """Check whether a CALLS edge for this exact call site already exists.

        architecture.md §3 Agent 内循环 step 2d: before creating a new
        CALLS edge, ``icsl_tools.write_edge`` must skip if the same
        ``(caller_id, callee_id, call_file, call_line)`` quadruple is
        already persisted — this matches the idempotency guard the
        agent-side tool always assumed the store enforced.
        """
        for edge in self._calls_edges:
            if (
                edge.caller_id == caller_id
                and edge.callee_id == callee_id
                and edge.props.call_file == call_file
                and edge.props.call_line == call_line
            ):
                return True
        return False

    def delete_unresolved_call(
        self, caller_id: str, call_file: str, call_line: int
    ) -> None:
        """Remove an UnresolvedCall once its gap has been repaired.

        architecture.md §3 修复成功时 step 3: after creating the CALLS
        edge + RepairLog, delete the UnresolvedCall so ``check-complete``
        can see the source point progress toward zero pending gaps.
        Located by ``(caller_id, call_file, call_line)`` — the same
        triple ``icsl_tools.write_edge`` passes on the CLI.
        """
        victims = [
            call_id
            for call_id, node in self._unresolved_calls.items()
            if node.caller_id == caller_id
            and node.call_file == call_file
            and node.call_line == call_line
        ]
        for call_id in victims:
            self._unresolved_calls.pop(call_id, None)

    def get_pending_gaps_for_source(
        self, source_id: str
    ) -> list[UnresolvedCallNode]:
        """Return pending UnresolvedCalls reachable from ``source_id``.

        architecture.md §3 门禁机制: ``icsl_tools check-complete``
        reports ``remaining_gaps`` = len(this) for a given source point;
        the orchestrator's gate subprocess parses the same list to decide
        whether to declare the source resolved.
        """
        reachable = self.get_reachable_subgraph(source_id)
        unresolved: list[UnresolvedCallNode] = reachable.get("unresolved", [])
        return [n for n in unresolved if n.status == "pending"]

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
            retry_count=existing.retry_count + 1,
            status="unresolvable" if existing.retry_count + 1 >= 3 else existing.status,
            last_attempt_timestamp=timestamp,
            last_attempt_reason=reason,
            id=existing.id,
        )
        self._unresolved_calls[call_id] = replaced

    def create_repair_log(self, node: RepairLogNode) -> str:
        """Persist a RepairLog node (architecture.md §3 修复成功时).

        Called from ``icsl_tools.write_edge`` after a successful LLM
        repair to capture the audit trail (caller_id + callee_id +
        call_location locator per ADR #51, plus repair_method,
        llm_response, timestamp, reasoning_summary).
        """
        self._repair_logs[node.id] = node
        return node.id

    def get_repair_logs(
        self,
        caller_id: str | None = None,
        callee_id: str | None = None,
        call_location: str | None = None,
    ) -> list[RepairLogNode]:
        """Query RepairLog entries with optional exact-match filters.

        The ``(caller_id, callee_id, call_location)`` triple is the
        architecture.md §4 property-reference contract used by
        CallGraphView to locate the RepairLog for a selected
        ``resolved_by='llm'`` CALLS edge.
        """
        results = list(self._repair_logs.values())
        if caller_id is not None:
            results = [r for r in results if r.caller_id == caller_id]
        if callee_id is not None:
            results = [r for r in results if r.callee_id == callee_id]
        if call_location is not None:
            results = [r for r in results if r.call_location == call_location]
        return results

    def delete_function(self, id: str) -> None:
        self._functions.pop(id, None)

    def delete_calls_edges_for_function(self, function_id: str) -> None:
        self._calls_edges = [
            e
            for e in self._calls_edges
            if e.caller_id != function_id and e.callee_id != function_id
        ]

    def list_files(self) -> list[FileNode]:
        return list(self._files.values())

    def list_functions(
        self, file_path: str | None = None
    ) -> list[FunctionNode]:
        fns = list(self._functions.values())
        if file_path is None:
            return fns
        return [f for f in fns if f.file_path == file_path]

    def list_calls_edges(self) -> list[_CallsEdge]:
        return list(self._calls_edges)

    def count_stats(self) -> dict:
        """Summary counts for ``/api/v1/stats`` (architecture.md §8).

        InMemoryStore answers from its private dicts directly. The Neo4j
        impl translates this into a single session-level aggregation
        query so the stats page doesn't need to materialize 23k+ UCs to
        count them.
        """
        by_status: dict[str, int] = {}
        for u in self._unresolved_calls.values():
            key = getattr(u, "status", None) or "pending"
            by_status[key] = by_status.get(key, 0) + 1
        by_category: dict[str, int] = {}
        _VALID_CATEGORIES = {
            "gate_failed", "agent_error", "subprocess_crash", "subprocess_timeout"
        }
        for u in self._unresolved_calls.values():
            reason = getattr(u, "last_attempt_reason", None)
            if reason and ":" in reason:
                prefix = reason.split(":", 1)[0].strip()
                cat_key = prefix if prefix in _VALID_CATEGORIES else "none"
            else:
                cat_key = "none"
            by_category[cat_key] = by_category.get(cat_key, 0) + 1
        by_resolved: dict[str, int] = {}
        for e in self._calls_edges:
            key = e.props.resolved_by or "unknown"
            by_resolved[key] = by_resolved.get(key, 0) + 1
        return {
            "total_functions": len(self._functions),
            "total_files": len(self._files),
            "total_calls": len(self._calls_edges),
            "total_unresolved": len(self._unresolved_calls),
            "total_repair_logs": len(self._repair_logs),
            "unresolved_by_status": by_status,
            "unresolved_by_category": by_category,
            "calls_by_resolved_by": by_resolved,
        }

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

    Each method opens a short-lived session so the store is safe to use
    across threads / asyncio tasks without sharing transactional state.
    The driver itself is created lazily on first use so unit tests that
    never touch Neo4j don't pay the driver-import cost.

    Cypher mirrors architecture.md §4 Neo4j Schema: label conventions
    (``File`` / ``Function`` / ``UnresolvedCall`` / ``RepairLog``),
    relationship types (``DEFINES`` / ``CALLS`` / ``HAS_GAP``), and
    ``CALLS`` edge properties ``{resolved_by, call_type, call_file,
    call_line}`` (the schema column ``location: {file, line}`` is
    flattened to ``call_file`` / ``call_line`` to match
    ``CallsEdgeProps`` and ``edge_exists`` lookups).
    """

    def __init__(self, uri: str, user: str, password: str) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        self._driver = None

    def _get_driver(self):
        """Lazy-construct the Neo4j driver on first use."""
        if self._driver is None:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                self._uri, auth=(self._user, self._password)
            )
        return self._driver

    def close(self) -> None:
        """Release the Neo4j driver connection pool."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def create_function(self, node: FunctionNode) -> str:
        cypher = (
            "MERGE (f:Function {id: $id}) "
            "SET f.signature = $signature, f.name = $name, "
            "    f.file_path = $file_path, f.start_line = $start_line, "
            "    f.end_line = $end_line, f.body_hash = $body_hash "
            "RETURN f.id AS id"
        )
        with self._get_driver().session() as session:
            session.run(
                cypher,
                id=node.id,
                signature=node.signature,
                name=node.name,
                file_path=node.file_path,
                start_line=node.start_line,
                end_line=node.end_line,
                body_hash=node.body_hash,
            ).consume()
        return node.id

    def create_file(self, node: FileNode) -> str:
        cypher = (
            "MERGE (f:File {id: $id}) "
            "SET f.file_path = $file_path, f.hash = $hash, "
            "    f.primary_language = $primary_language"
        )
        with self._get_driver().session() as session:
            session.run(
                cypher,
                id=node.id,
                file_path=node.file_path,
                hash=node.hash,
                primary_language=node.primary_language,
            ).consume()
        return node.id

    def create_calls_edge(
        self, caller_id: str, callee_id: str, props: CallsEdgeProps
    ) -> None:
        cypher = (
            "MATCH (a:Function {id: $caller_id}), (b:Function {id: $callee_id}) "
            "MERGE (a)-[r:CALLS {call_file: $call_file, call_line: $call_line}]->(b) "
            "SET r.resolved_by = $resolved_by, r.call_type = $call_type"
        )
        with self._get_driver().session() as session:
            session.run(
                cypher,
                caller_id=caller_id,
                callee_id=callee_id,
                call_file=props.call_file,
                call_line=props.call_line,
                resolved_by=props.resolved_by,
                call_type=props.call_type,
            ).consume()

    def create_unresolved_call(self, node: UnresolvedCallNode) -> str:
        cypher = (
            "MERGE (u:UnresolvedCall {id: $id}) "
            "SET u.caller_id = $caller_id, u.call_expression = $call_expression, "
            "    u.call_file = $call_file, u.call_line = $call_line, "
            "    u.call_type = $call_type, u.source_code_snippet = $source_code_snippet, "
            "    u.var_name = $var_name, u.var_type = $var_type, "
            "    u.candidates = $candidates, u.retry_count = $retry_count, "
            "    u.status = $status, "
            "    u.last_attempt_timestamp = $last_attempt_timestamp, "
            "    u.last_attempt_reason = $last_attempt_reason "
            "WITH u "
            "MATCH (caller:Function {id: $caller_id}) "
            "MERGE (caller)-[:HAS_GAP]->(u)"
        )
        with self._get_driver().session() as session:
            session.run(
                cypher,
                id=node.id,
                caller_id=node.caller_id,
                call_expression=node.call_expression,
                call_file=node.call_file,
                call_line=node.call_line,
                call_type=node.call_type,
                source_code_snippet=node.source_code_snippet,
                var_name=node.var_name,
                var_type=node.var_type,
                candidates=list(node.candidates),
                retry_count=node.retry_count,
                status=node.status,
                last_attempt_timestamp=node.last_attempt_timestamp,
                last_attempt_reason=node.last_attempt_reason,
            ).consume()
        return node.id

    def get_function_by_id(self, id: str) -> FunctionNode | None:
        cypher = (
            "MATCH (f:Function {id: $id}) RETURN f.id AS id, "
            "f.signature AS signature, f.name AS name, f.file_path AS file_path, "
            "f.start_line AS start_line, f.end_line AS end_line, "
            "f.body_hash AS body_hash"
        )
        with self._get_driver().session() as session:
            record = session.run(cypher, id=id).single()
        if record is None:
            return None
        return FunctionNode(
            signature=record["signature"],
            name=record["name"],
            file_path=record["file_path"],
            start_line=record["start_line"],
            end_line=record["end_line"],
            body_hash=record["body_hash"],
            id=record["id"],
        )

    def get_callers(self, function_id: str) -> list[FunctionNode]:
        cypher = (
            "MATCH (caller:Function)-[:CALLS]->(callee:Function {id: $id}) "
            "RETURN DISTINCT caller.id AS id, caller.signature AS signature, "
            "caller.name AS name, caller.file_path AS file_path, "
            "caller.start_line AS start_line, caller.end_line AS end_line, "
            "caller.body_hash AS body_hash"
        )
        with self._get_driver().session() as session:
            records = list(session.run(cypher, id=function_id))
        return [_record_to_function(r) for r in records]

    def get_callees(self, function_id: str) -> list[FunctionNode]:
        cypher = (
            "MATCH (caller:Function {id: $id})-[:CALLS]->(callee:Function) "
            "RETURN DISTINCT callee.id AS id, callee.signature AS signature, "
            "callee.name AS name, callee.file_path AS file_path, "
            "callee.start_line AS start_line, callee.end_line AS end_line, "
            "callee.body_hash AS body_hash"
        )
        with self._get_driver().session() as session:
            records = list(session.run(cypher, id=function_id))
        return [_record_to_function(r) for r in records]

    def get_unresolved_calls(
        self, caller_id: str | None = None, status: str | None = None
    ) -> list[UnresolvedCallNode]:
        clauses = []
        params: dict = {}
        if caller_id is not None:
            clauses.append("u.caller_id = $caller_id")
            params["caller_id"] = caller_id
        if status is not None:
            clauses.append("u.status = $status")
            params["status"] = status
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        cypher = (
            f"MATCH (u:UnresolvedCall) {where}"
            "RETURN u.id AS id, u.caller_id AS caller_id, "
            "u.call_expression AS call_expression, u.call_file AS call_file, "
            "u.call_line AS call_line, u.call_type AS call_type, "
            "u.source_code_snippet AS source_code_snippet, "
            "u.var_name AS var_name, u.var_type AS var_type, "
            "u.candidates AS candidates, u.retry_count AS retry_count, "
            "u.status AS status, "
            "u.last_attempt_timestamp AS last_attempt_timestamp, "
            "u.last_attempt_reason AS last_attempt_reason"
        )
        with self._get_driver().session() as session:
            records = list(session.run(cypher, **params))
        return [_record_to_unresolved(r) for r in records]

    def edge_exists(
        self, caller_id: str, callee_id: str, call_file: str, call_line: int
    ) -> bool:
        cypher = (
            "MATCH (a:Function {id: $caller_id})-[r:CALLS]->(b:Function {id: $callee_id}) "
            "WHERE r.call_file = $call_file AND r.call_line = $call_line "
            "RETURN count(r) AS c"
        )
        with self._get_driver().session() as session:
            record = session.run(
                cypher,
                caller_id=caller_id,
                callee_id=callee_id,
                call_file=call_file,
                call_line=call_line,
            ).single()
        return bool(record and record["c"] > 0)

    def delete_unresolved_call(
        self, caller_id: str, call_file: str, call_line: int
    ) -> None:
        cypher = (
            "MATCH (u:UnresolvedCall) "
            "WHERE u.caller_id = $caller_id "
            "  AND u.call_file = $call_file "
            "  AND u.call_line = $call_line "
            "DETACH DELETE u"
        )
        with self._get_driver().session() as session:
            session.run(
                cypher,
                caller_id=caller_id,
                call_file=call_file,
                call_line=call_line,
            ).consume()

    def get_pending_gaps_for_source(
        self, source_id: str
    ) -> list[UnresolvedCallNode]:
        # architecture.md §3 门禁机制: intersect the source's reachable
        # caller set with pending UnresolvedCall nodes — the exact list
        # ``check-complete`` reports as ``remaining_gaps``.
        cypher = (
            "MATCH (src:Function {id: $source_id}) "
            "MATCH (src)-[:CALLS*0..]->(caller:Function) "
            "MATCH (caller)-[:HAS_GAP]->(u:UnresolvedCall) "
            "WHERE u.status = 'pending' "
            "RETURN DISTINCT u.id AS id, u.caller_id AS caller_id, "
            "u.call_expression AS call_expression, u.call_file AS call_file, "
            "u.call_line AS call_line, u.call_type AS call_type, "
            "u.source_code_snippet AS source_code_snippet, "
            "u.var_name AS var_name, u.var_type AS var_type, "
            "u.candidates AS candidates, u.retry_count AS retry_count, "
            "u.status AS status, "
            "u.last_attempt_timestamp AS last_attempt_timestamp, "
            "u.last_attempt_reason AS last_attempt_reason"
        )
        with self._get_driver().session() as session:
            records = list(session.run(cypher, source_id=source_id))
        return [_record_to_unresolved(r) for r in records]

    def update_unresolved_call_retry_state(
        self, call_id: str, timestamp: str, reason: str
    ) -> None:
        # architecture.md §3: retry_count++ per GAP; when >= 3 → status = "unresolvable"
        cypher = (
            "MATCH (u:UnresolvedCall {id: $id}) "
            "SET u.last_attempt_timestamp = $timestamp, "
            "    u.last_attempt_reason = $reason, "
            "    u.retry_count = coalesce(u.retry_count, 0) + 1 "
            "WITH u "
            "WHERE u.retry_count >= 3 AND u.status <> 'unresolvable' "
            "SET u.status = 'unresolvable'"
        )
        with self._get_driver().session() as session:
            session.run(
                cypher, id=call_id, timestamp=timestamp, reason=reason
            ).consume()

    def create_repair_log(self, node: RepairLogNode) -> str:
        cypher = (
            "MERGE (r:RepairLog {id: $id}) "
            "SET r.caller_id = $caller_id, r.callee_id = $callee_id, "
            "    r.call_location = $call_location, "
            "    r.repair_method = $repair_method, "
            "    r.llm_response = $llm_response, "
            "    r.timestamp = $timestamp, "
            "    r.reasoning_summary = $reasoning_summary"
        )
        with self._get_driver().session() as session:
            session.run(
                cypher,
                id=node.id,
                caller_id=node.caller_id,
                callee_id=node.callee_id,
                call_location=node.call_location,
                repair_method=node.repair_method,
                llm_response=node.llm_response,
                timestamp=node.timestamp,
                reasoning_summary=node.reasoning_summary,
            ).consume()
        return node.id

    def get_repair_logs(
        self,
        caller_id: str | None = None,
        callee_id: str | None = None,
        call_location: str | None = None,
    ) -> list[RepairLogNode]:
        clauses = []
        params: dict = {}
        if caller_id is not None:
            clauses.append("r.caller_id = $caller_id")
            params["caller_id"] = caller_id
        if callee_id is not None:
            clauses.append("r.callee_id = $callee_id")
            params["callee_id"] = callee_id
        if call_location is not None:
            clauses.append("r.call_location = $call_location")
            params["call_location"] = call_location
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        cypher = (
            f"MATCH (r:RepairLog) {where}"
            "RETURN r.id AS id, r.caller_id AS caller_id, "
            "r.callee_id AS callee_id, r.call_location AS call_location, "
            "r.repair_method AS repair_method, r.llm_response AS llm_response, "
            "r.timestamp AS timestamp, r.reasoning_summary AS reasoning_summary"
        )
        with self._get_driver().session() as session:
            records = list(session.run(cypher, **params))
        return [_record_to_repair_log(r) for r in records]

    def delete_function(self, id: str) -> None:
        cypher = "MATCH (f:Function {id: $id}) DETACH DELETE f"
        with self._get_driver().session() as session:
            session.run(cypher, id=id).consume()

    def delete_calls_edges_for_function(self, function_id: str) -> None:
        cypher = (
            "MATCH (f:Function {id: $id}) "
            "OPTIONAL MATCH (f)-[out:CALLS]->() "
            "OPTIONAL MATCH ()-[in_:CALLS]->(f) "
            "DELETE out, in_"
        )
        with self._get_driver().session() as session:
            session.run(cypher, id=function_id).consume()

    def list_files(self) -> list[FileNode]:
        cypher = (
            "MATCH (f:File) "
            "RETURN f.id AS id, f.file_path AS file_path, "
            "f.hash AS hash, f.primary_language AS primary_language"
        )
        with self._get_driver().session() as session:
            records = list(session.run(cypher))
        return [
            FileNode(
                id=r["id"],
                file_path=r["file_path"],
                hash=r["hash"] or "",
                primary_language=r["primary_language"] or "",
            )
            for r in records
        ]

    def list_functions(
        self, file_path: str | None = None
    ) -> list[FunctionNode]:
        params: dict = {}
        where = ""
        if file_path is not None:
            where = "WHERE f.file_path = $file_path "
            params["file_path"] = file_path
        cypher = (
            f"MATCH (f:Function) {where}"
            "RETURN f.id AS id, f.signature AS signature, f.name AS name, "
            "f.file_path AS file_path, f.start_line AS start_line, "
            "f.end_line AS end_line, f.body_hash AS body_hash"
        )
        with self._get_driver().session() as session:
            records = list(session.run(cypher, **params))
        return [_record_to_function(r) for r in records]

    def list_calls_edges(self) -> list[_CallsEdge]:
        cypher = (
            "MATCH (a:Function)-[r:CALLS]->(b:Function) "
            "RETURN a.id AS caller_id, b.id AS callee_id, "
            "r.resolved_by AS resolved_by, r.call_type AS call_type, "
            "r.call_file AS call_file, r.call_line AS call_line"
        )
        with self._get_driver().session() as session:
            records = list(session.run(cypher))
        edges: list[_CallsEdge] = []
        for r in records:
            props = CallsEdgeProps(
                resolved_by=r["resolved_by"] or "",
                call_type=r["call_type"] or "",
                call_file=r["call_file"] or "",
                call_line=r["call_line"] or 0,
            )
            edges.append(
                _CallsEdge(
                    caller_id=r["caller_id"],
                    callee_id=r["callee_id"],
                    props=props,
                )
            )
        return edges

    def count_stats(self) -> dict:
        """Single-session aggregation for ``/api/v1/stats``.

        architecture.md §8: stats must answer from Neo4j without
        materializing every node. Uses independent ``MATCH … count(*)``
        queries so each label can be indexed separately. Runs 5 cheap
        Cypher statements in one session.
        """
        with self._get_driver().session() as session:
            total_functions = session.run(
                "MATCH (f:Function) RETURN count(f) AS n"
            ).single()["n"]
            total_files = session.run(
                "MATCH (f:File) RETURN count(f) AS n"
            ).single()["n"]
            total_calls = session.run(
                "MATCH ()-[r:CALLS]->() RETURN count(r) AS n"
            ).single()["n"]
            total_unresolved = session.run(
                "MATCH (u:UnresolvedCall) RETURN count(u) AS n"
            ).single()["n"]
            total_repair_logs = session.run(
                "MATCH (r:RepairLog) RETURN count(r) AS n"
            ).single()["n"]
            by_status: dict[str, int] = {
                (row["s"] or "pending"): row["n"]
                for row in session.run(
                    "MATCH (u:UnresolvedCall) "
                    "RETURN coalesce(u.status, 'pending') AS s, count(u) AS n"
                )
            }
            # last_attempt_reason may be absent; bucket missing/malformed
            # to "none" so the Dashboard chip row never silently drops UCs.
            # architecture.md §3: valid categories are {gate_failed,
            # agent_error, subprocess_crash, subprocess_timeout}.
            _VALID_CATEGORIES = {
                "gate_failed", "agent_error", "subprocess_crash", "subprocess_timeout"
            }
            cat_rows = list(session.run(
                "MATCH (u:UnresolvedCall) "
                "RETURN u.last_attempt_reason AS reason, count(u) AS n"
            ))
            by_category: dict[str, int] = {}
            for row in cat_rows:
                reason = row["reason"]
                if reason and ":" in reason:
                    prefix = reason.split(":", 1)[0].strip()
                    cat_key = prefix if prefix in _VALID_CATEGORIES else "none"
                else:
                    cat_key = "none"
                by_category[cat_key] = by_category.get(cat_key, 0) + row["n"]
            by_resolved: dict[str, int] = {
                (row["rb"] or "unknown"): row["n"]
                for row in session.run(
                    "MATCH ()-[r:CALLS]->() "
                    "RETURN coalesce(r.resolved_by, 'unknown') AS rb, "
                    "count(r) AS n"
                )
            }
        return {
            "total_functions": total_functions,
            "total_files": total_files,
            "total_calls": total_calls,
            "total_unresolved": total_unresolved,
            "total_repair_logs": total_repair_logs,
            "unresolved_by_status": by_status,
            "unresolved_by_category": by_category,
            "calls_by_resolved_by": by_resolved,
        }

    def get_reachable_subgraph(
        self, source_id: str, max_depth: int = 50
    ) -> dict:
        # APOC-free variable-length traversal. Bound by max_depth to
        # keep pathological graphs from OOM'ing the driver session.
        cypher = (
            "MATCH (src:Function {id: $source_id}) "
            f"MATCH path = (src)-[:CALLS*0..{int(max_depth)}]->(fn:Function) "
            "WITH collect(DISTINCT fn) AS fns, src "
            "WITH fns + [src] AS nodes "
            "UNWIND nodes AS n "
            "WITH collect(DISTINCT n) AS nodes "
            "OPTIONAL MATCH (a:Function)-[r:CALLS]->(b:Function) "
            "WHERE a IN nodes AND b IN nodes "
            "WITH nodes, collect(DISTINCT {caller_id: a.id, callee_id: b.id, "
            "                              resolved_by: r.resolved_by, "
            "                              call_type: r.call_type, "
            "                              call_file: r.call_file, "
            "                              call_line: r.call_line}) AS edges "
            "OPTIONAL MATCH (c:Function)-[:HAS_GAP]->(u:UnresolvedCall) "
            "WHERE c IN nodes "
            "RETURN nodes, edges, collect(DISTINCT u) AS unresolved"
        )
        with self._get_driver().session() as session:
            record = session.run(
                cypher, source_id=source_id
            ).single()

        if record is None:
            return {"nodes": [], "edges": [], "unresolved": []}

        nodes = [
            FunctionNode(
                signature=n.get("signature", ""),
                name=n.get("name", ""),
                file_path=n.get("file_path", ""),
                start_line=n.get("start_line", 0),
                end_line=n.get("end_line", 0),
                body_hash=n.get("body_hash", ""),
                id=n.get("id", ""),
            )
            for n in record["nodes"]
            if n is not None
        ]
        edges = [
            _CallsEdge(
                caller_id=e["caller_id"],
                callee_id=e["callee_id"],
                props=CallsEdgeProps(
                    resolved_by=e["resolved_by"],
                    call_type=e["call_type"],
                    call_file=e["call_file"],
                    call_line=e["call_line"],
                ),
            )
            for e in record["edges"]
            if e and e.get("caller_id") is not None
        ]
        unresolved = [
            UnresolvedCallNode(
                caller_id=u.get("caller_id"),
                call_expression=u.get("call_expression", ""),
                call_file=u.get("call_file", ""),
                call_line=u.get("call_line", 0),
                call_type=u.get("call_type", ""),
                source_code_snippet=u.get("source_code_snippet", ""),
                var_name=u.get("var_name"),
                var_type=u.get("var_type"),
                candidates=list(u.get("candidates") or []),
                retry_count=u.get("retry_count", 0),
                status=u.get("status", "pending"),
                last_attempt_timestamp=u.get("last_attempt_timestamp"),
                last_attempt_reason=u.get("last_attempt_reason"),
                id=u.get("id", ""),
            )
            for u in record["unresolved"]
            if u is not None
        ]
        return {"nodes": nodes, "edges": edges, "unresolved": unresolved}


# --- Record → dataclass helpers ---------------------------------------------


def _record_to_function(record) -> FunctionNode:
    return FunctionNode(
        signature=record["signature"],
        name=record["name"],
        file_path=record["file_path"],
        start_line=record["start_line"],
        end_line=record["end_line"],
        body_hash=record["body_hash"],
        id=record["id"],
    )


def _record_to_unresolved(record) -> UnresolvedCallNode:
    return UnresolvedCallNode(
        caller_id=record["caller_id"],
        call_expression=record["call_expression"],
        call_file=record["call_file"],
        call_line=record["call_line"],
        call_type=record["call_type"],
        source_code_snippet=record["source_code_snippet"],
        var_name=record["var_name"],
        var_type=record["var_type"],
        candidates=list(record["candidates"] or []),
        retry_count=record["retry_count"] or 0,
        status=record["status"] or "pending",
        last_attempt_timestamp=record["last_attempt_timestamp"],
        last_attempt_reason=record["last_attempt_reason"],
        id=record["id"],
    )


def _record_to_repair_log(record) -> RepairLogNode:
    return RepairLogNode(
        caller_id=record["caller_id"],
        callee_id=record["callee_id"],
        call_location=record["call_location"],
        repair_method=record["repair_method"],
        llm_response=record["llm_response"],
        timestamp=record["timestamp"],
        reasoning_summary=record["reasoning_summary"],
        id=record["id"],
    )


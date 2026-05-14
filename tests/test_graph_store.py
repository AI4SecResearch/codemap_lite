"""Tests for the graph storage layer (Phase 1.7)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    RepairLogNode,
    UnresolvedCallNode,
)
from codemap_lite.graph.neo4j_store import InMemoryGraphStore, Neo4jGraphStore


@pytest.fixture
def store() -> InMemoryGraphStore:
    return InMemoryGraphStore()


@pytest.fixture
def sample_function() -> FunctionNode:
    return FunctionNode(
        signature="def foo(x: int) -> int",
        name="foo",
        file_path="src/main.py",
        start_line=10,
        end_line=15,
        body_hash="abc123",
    )


@pytest.fixture
def sample_function_b() -> FunctionNode:
    return FunctionNode(
        signature="def bar() -> None",
        name="bar",
        file_path="src/main.py",
        start_line=20,
        end_line=30,
        body_hash="def456",
    )


class TestCreateAndGetFunction:
    def test_create_and_get_function(
        self, store: InMemoryGraphStore, sample_function: FunctionNode
    ) -> None:
        node_id = store.create_function(sample_function)
        assert node_id == sample_function.id

        retrieved = store.get_function_by_id(node_id)
        assert retrieved is not None
        assert retrieved.name == "foo"
        assert retrieved.signature == "def foo(x: int) -> int"
        assert retrieved.file_path == "src/main.py"
        assert retrieved.start_line == 10
        assert retrieved.end_line == 15
        assert retrieved.body_hash == "abc123"

    def test_get_function_not_found(self, store: InMemoryGraphStore) -> None:
        assert store.get_function_by_id("nonexistent") is None

    def test_list_functions_all(self, store: InMemoryGraphStore) -> None:
        """list_functions() without filter returns all functions."""
        fn1 = FunctionNode(
            id="f1", name="a", signature="void a()",
            file_path="src/a.cpp", start_line=1, end_line=5, body_hash="h1",
        )
        fn2 = FunctionNode(
            id="f2", name="b", signature="void b()",
            file_path="src/b.cpp", start_line=1, end_line=5, body_hash="h2",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        result = store.list_functions()
        assert len(result) == 2

    def test_list_functions_filtered_by_file_path(self, store: InMemoryGraphStore) -> None:
        """list_functions(file_path=...) returns only functions in that file."""
        fn1 = FunctionNode(
            id="f1", name="a", signature="void a()",
            file_path="src/a.cpp", start_line=1, end_line=5, body_hash="h1",
        )
        fn2 = FunctionNode(
            id="f2", name="b", signature="void b()",
            file_path="src/b.cpp", start_line=1, end_line=5, body_hash="h2",
        )
        fn3 = FunctionNode(
            id="f3", name="c", signature="void c()",
            file_path="src/a.cpp", start_line=10, end_line=15, body_hash="h3",
        )
        store.create_function(fn1)
        store.create_function(fn2)
        store.create_function(fn3)
        result = store.list_functions(file_path="src/a.cpp")
        assert len(result) == 2
        names = {fn.name for fn in result}
        assert names == {"a", "c"}


class TestCallsEdges:
    def test_create_calls_edge_and_get_callees(
        self,
        store: InMemoryGraphStore,
        sample_function: FunctionNode,
        sample_function_b: FunctionNode,
    ) -> None:
        store.create_function(sample_function)
        store.create_function(sample_function_b)

        props = CallsEdgeProps(
            resolved_by="symbol_table",
            call_type="direct",
            call_file="src/main.py",
            call_line=12,
        )
        store.create_calls_edge(sample_function.id, sample_function_b.id, props)

        callees = store.get_callees(sample_function.id)
        assert len(callees) == 1
        assert callees[0].id == sample_function_b.id

    def test_create_calls_edge_and_get_callers(
        self,
        store: InMemoryGraphStore,
        sample_function: FunctionNode,
        sample_function_b: FunctionNode,
    ) -> None:
        store.create_function(sample_function)
        store.create_function(sample_function_b)

        props = CallsEdgeProps(
            resolved_by="symbol_table",
            call_type="direct",
            call_file="src/main.py",
            call_line=12,
        )
        store.create_calls_edge(sample_function.id, sample_function_b.id, props)

        callers = store.get_callers(sample_function_b.id)
        assert len(callers) == 1
        assert callers[0].id == sample_function.id


class TestUnresolvedCalls:
    def test_get_unresolved_calls_by_status(
        self, store: InMemoryGraphStore, sample_function: FunctionNode
    ) -> None:
        store.create_function(sample_function)

        unresolved1 = UnresolvedCallNode(
            caller_id=sample_function.id,
            call_expression="baz()",
            call_file="src/main.py",
            call_line=12,
            call_type="direct",
            source_code_snippet="baz()",
            var_name=None,
            var_type=None,
            candidates=["mod.baz"],
            retry_count=0,
            status="pending",
        )
        unresolved2 = UnresolvedCallNode(
            caller_id=sample_function.id,
            call_expression="qux()",
            call_file="src/main.py",
            call_line=13,
            call_type="direct",
            source_code_snippet="qux()",
            var_name=None,
            var_type=None,
            candidates=[],
            retry_count=1,
            status="unresolvable",
        )
        store.create_unresolved_call(unresolved1)
        store.create_unresolved_call(unresolved2)

        pending = store.get_unresolved_calls(status="pending")
        assert len(pending) == 1
        assert pending[0].call_expression == "baz()"

        all_for_caller = store.get_unresolved_calls(caller_id=sample_function.id)
        assert len(all_for_caller) == 2


class TestDeleteOperations:
    def test_delete_function_removes_node(
        self, store: InMemoryGraphStore, sample_function: FunctionNode
    ) -> None:
        store.create_function(sample_function)
        assert store.get_function_by_id(sample_function.id) is not None

        store.delete_function(sample_function.id)
        assert store.get_function_by_id(sample_function.id) is None

    def test_delete_calls_edges_for_function(
        self,
        store: InMemoryGraphStore,
        sample_function: FunctionNode,
        sample_function_b: FunctionNode,
    ) -> None:
        store.create_function(sample_function)
        store.create_function(sample_function_b)

        props = CallsEdgeProps(
            resolved_by="symbol_table",
            call_type="direct",
            call_file="src/main.py",
            call_line=12,
        )
        store.create_calls_edge(sample_function.id, sample_function_b.id, props)
        assert len(store.get_callees(sample_function.id)) == 1

        store.delete_calls_edges_for_function(sample_function.id)
        assert len(store.get_callees(sample_function.id)) == 0
        assert len(store.get_callers(sample_function_b.id)) == 0


class TestReachableSubgraph:
    def test_get_reachable_subgraph(self, store: InMemoryGraphStore) -> None:
        fn_a = FunctionNode(
            signature="def a()", name="a",
            file_path="f.py", start_line=1, end_line=3, body_hash="h1",
        )
        fn_b = FunctionNode(
            signature="def b()", name="b",
            file_path="f.py", start_line=5, end_line=7, body_hash="h2",
        )
        fn_c = FunctionNode(
            signature="def c()", name="c",
            file_path="f.py", start_line=9, end_line=11, body_hash="h3",
        )
        store.create_function(fn_a)
        store.create_function(fn_b)
        store.create_function(fn_c)

        props = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="f.py", call_line=2,
        )
        store.create_calls_edge(fn_a.id, fn_b.id, props)
        store.create_calls_edge(fn_b.id, fn_c.id, props)

        # Add an unresolved call from fn_c
        unresolved = UnresolvedCallNode(
            caller_id=fn_c.id,
            call_expression="unknown()",
            call_file="f.py",
            call_line=10,
            call_type="indirect",
            source_code_snippet="unknown()",
            var_name=None,
            var_type=None,
            candidates=[],
            retry_count=0,
            status="pending",
        )
        store.create_unresolved_call(unresolved)

        result = store.get_reachable_subgraph(fn_a.id, max_depth=50)

        assert "nodes" in result
        assert "edges" in result
        assert "unresolved" in result

        node_ids = {n.id for n in result["nodes"]}
        assert fn_a.id in node_ids
        assert fn_b.id in node_ids
        assert fn_c.id in node_ids

        assert len(result["edges"]) == 2
        assert len(result["unresolved"]) == 1
        assert result["unresolved"][0].call_expression == "unknown()"

    def test_get_reachable_subgraph_respects_depth(
        self, store: InMemoryGraphStore
    ) -> None:
        fn_a = FunctionNode(
            signature="def a()", name="a",
            file_path="f.py", start_line=1, end_line=3, body_hash="h1",
        )
        fn_b = FunctionNode(
            signature="def b()", name="b",
            file_path="f.py", start_line=5, end_line=7, body_hash="h2",
        )
        fn_c = FunctionNode(
            signature="def c()", name="c",
            file_path="f.py", start_line=9, end_line=11, body_hash="h3",
        )
        store.create_function(fn_a)
        store.create_function(fn_b)
        store.create_function(fn_c)

        props = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="f.py", call_line=2,
        )
        store.create_calls_edge(fn_a.id, fn_b.id, props)
        store.create_calls_edge(fn_b.id, fn_c.id, props)

        result = store.get_reachable_subgraph(fn_a.id, max_depth=1)
        node_ids = {n.id for n in result["nodes"]}
        assert fn_a.id in node_ids
        assert fn_b.id in node_ids
        # fn_c is at depth 2, should not be included
        assert fn_c.id not in node_ids



class TestUpdateUnresolvedCallRetryState:
    """architecture.md §3 Retry 审计字段 — each failed gate check stamps
    last_attempt_timestamp + last_attempt_reason on pending GAPs so the
    frontend can surface them without trawling JSONL logs."""

    def test_stamps_timestamp_and_reason(self, store):
        gap = UnresolvedCallNode(
            caller_id="caller_x",
            call_expression="fn_ptr(x)",
            call_file="foo.cpp",
            call_line=42,
            call_type="indirect",
            source_code_snippet="fn_ptr(x);",
            var_name="fn_ptr",
            var_type="void (*)(int)",
        )
        store.create_unresolved_call(gap)

        store.update_unresolved_call_retry_state(
            call_id=gap.id,
            timestamp="2026-05-13T12:34:56+00:00",
            reason="gate_failed: remaining pending GAPs",
        )
        updated = store._unresolved_calls[gap.id]
        assert updated.last_attempt_timestamp == "2026-05-13T12:34:56+00:00"
        assert updated.last_attempt_reason == "gate_failed: remaining pending GAPs"
        # Non-audit fields are preserved — immutable dataclass round-trip.
        assert updated.caller_id == gap.caller_id
        assert updated.candidates == []
        assert updated.id == gap.id

    def test_missing_id_is_a_noop(self, store):
        # Silent noop so the orchestrator can call this without having
        # to pre-check existence; matches the Neo4j MERGE semantics.
        store.update_unresolved_call_retry_state(
            call_id="does-not-exist",
            timestamp="2026-05-13T12:34:56+00:00",
            reason="gate_failed: irrelevant",
        )
        assert store._unresolved_calls == {}

    def test_retry_count_increments_each_call(self, store):
        """architecture.md §3: retry_count++ per GAP on each failed gate."""
        gap = UnresolvedCallNode(
            caller_id="caller_x",
            call_expression="fn_ptr(x)",
            call_file="foo.cpp",
            call_line=42,
            call_type="indirect",
            source_code_snippet="fn_ptr(x);",
            var_name="fn_ptr",
            var_type="void (*)(int)",
        )
        store.create_unresolved_call(gap)
        assert store._unresolved_calls[gap.id].retry_count == 0

        store.update_unresolved_call_retry_state(
            call_id=gap.id, timestamp="t1", reason="gate_failed: r1"
        )
        assert store._unresolved_calls[gap.id].retry_count == 1
        assert store._unresolved_calls[gap.id].status == "pending"

        store.update_unresolved_call_retry_state(
            call_id=gap.id, timestamp="t2", reason="gate_failed: r2"
        )
        assert store._unresolved_calls[gap.id].retry_count == 2
        assert store._unresolved_calls[gap.id].status == "pending"

    def test_status_becomes_unresolvable_at_3_retries(self, store):
        """architecture.md §3: retry_count >= 3 → status = 'unresolvable'."""
        gap = UnresolvedCallNode(
            caller_id="caller_x",
            call_expression="fn_ptr(x)",
            call_file="foo.cpp",
            call_line=42,
            call_type="indirect",
            source_code_snippet="fn_ptr(x);",
            var_name="fn_ptr",
            var_type="void (*)(int)",
        )
        store.create_unresolved_call(gap)

        for i in range(3):
            store.update_unresolved_call_retry_state(
                call_id=gap.id, timestamp=f"t{i}", reason=f"gate_failed: attempt {i}"
            )

        updated = store._unresolved_calls[gap.id]
        assert updated.retry_count == 3
        assert updated.status == "unresolvable"


class TestRepairLogPersistence:
    """architecture.md §3 修复成功时 + §4 RepairLog schema + ADR #51 —
    每条 LLM 修复都落一行 RepairLog，通过 (caller_id, callee_id,
    call_location) 三元组定位对应的 CALLS 边（不通过关系边）。"""

    def _make_log(
        self,
        caller_id: str = "func_a",
        callee_id: str = "func_b",
        call_location: str = "foo.cpp:42",
        llm_response: str = "agent reply",
        reasoning_summary: str = "indirect call resolved via vtable",
    ) -> RepairLogNode:
        return RepairLogNode(
            caller_id=caller_id,
            callee_id=callee_id,
            call_location=call_location,
            repair_method="llm",
            llm_response=llm_response,
            timestamp="2026-05-13T12:00:00+00:00",
            reasoning_summary=reasoning_summary,
        )

    def test_create_and_retrieve_repair_log(self, store):
        log = self._make_log()
        returned_id = store.create_repair_log(log)
        assert returned_id == log.id
        all_logs = store.get_repair_logs()
        assert len(all_logs) == 1
        assert all_logs[0].id == log.id
        assert all_logs[0].repair_method == "llm"

    def test_filter_by_triple_locates_single_log(self, store):
        # Two LLM-repaired edges in the same file but different sites —
        # the (caller, callee, location) triple should pick exactly one.
        store.create_repair_log(self._make_log(call_location="foo.cpp:42"))
        store.create_repair_log(self._make_log(call_location="foo.cpp:99"))

        hit = store.get_repair_logs(
            caller_id="func_a",
            callee_id="func_b",
            call_location="foo.cpp:42",
        )
        assert len(hit) == 1
        assert hit[0].call_location == "foo.cpp:42"

    def test_filter_by_caller_only(self, store):
        store.create_repair_log(self._make_log(caller_id="func_a"))
        store.create_repair_log(self._make_log(caller_id="func_other"))

        hits = store.get_repair_logs(caller_id="func_a")
        assert len(hits) == 1
        assert hits[0].caller_id == "func_a"

    def test_no_match_returns_empty_list(self, store):
        store.create_repair_log(self._make_log())
        assert store.get_repair_logs(caller_id="nope") == []


class TestAgentSideGapOperations:
    """architecture.md §3 Agent 内循环 — the three methods
    ``icsl_tools`` calls over the GraphStore protocol:
    ``edge_exists`` (idempotency guard before creating a CALLS edge),
    ``delete_unresolved_call`` (清账 after successful repair), and
    ``get_pending_gaps_for_source`` (gate 机制 reads this per source
    point to decide whether to declare completion)."""

    def _make_gap(
        self,
        caller_id: str = "func_a",
        call_file: str = "src/main.cpp",
        call_line: int = 42,
        status: str = "pending",
    ) -> UnresolvedCallNode:
        return UnresolvedCallNode(
            caller_id=caller_id,
            call_expression="ptr->method()",
            call_file=call_file,
            call_line=call_line,
            call_type="indirect",
            source_code_snippet="ptr->method();",
            var_name="ptr",
            var_type="Base*",
            candidates=["Derived::method"],
            status=status,
        )

    def test_edge_exists_true_when_quadruple_matches(
        self, store: InMemoryGraphStore
    ) -> None:
        props = CallsEdgeProps(
            resolved_by="llm",
            call_type="indirect",
            call_file="src/main.cpp",
            call_line=42,
        )
        store.create_calls_edge("func_a", "func_b", props)
        assert store.edge_exists("func_a", "func_b", "src/main.cpp", 42) is True

    def test_edge_exists_false_when_no_match(
        self, store: InMemoryGraphStore
    ) -> None:
        props = CallsEdgeProps(
            resolved_by="llm",
            call_type="indirect",
            call_file="src/main.cpp",
            call_line=42,
        )
        store.create_calls_edge("func_a", "func_b", props)
        # Same caller/callee but a different call site.
        assert store.edge_exists("func_a", "func_b", "src/main.cpp", 99) is False
        # Same location but a different callee.
        assert store.edge_exists("func_a", "func_c", "src/main.cpp", 42) is False

    def test_delete_unresolved_call_removes_matching_gap(
        self, store: InMemoryGraphStore
    ) -> None:
        target = self._make_gap(call_line=42)
        other = self._make_gap(call_line=99)
        store.create_unresolved_call(target)
        store.create_unresolved_call(other)

        store.delete_unresolved_call("func_a", "src/main.cpp", 42)

        remaining = store.get_unresolved_calls()
        assert len(remaining) == 1
        assert remaining[0].call_line == 99

    def test_delete_unresolved_call_noop_when_missing(
        self, store: InMemoryGraphStore
    ) -> None:
        store.create_unresolved_call(self._make_gap(call_line=42))
        # Different file — nothing should match.
        store.delete_unresolved_call("func_a", "src/other.cpp", 42)
        assert len(store.get_unresolved_calls()) == 1

    def test_get_pending_gaps_for_source_returns_only_pending(
        self, store: InMemoryGraphStore
    ) -> None:
        # Set up a tiny reachable subgraph: source -> callee, each with
        # one pending gap; the callee also has one already-resolved gap.
        source = FunctionNode(
            signature="void src()", name="src",
            file_path="m.cpp", start_line=1, end_line=5, body_hash="s",
        )
        callee = FunctionNode(
            signature="void c()", name="c",
            file_path="m.cpp", start_line=7, end_line=9, body_hash="c",
        )
        store.create_function(source)
        store.create_function(callee)
        props = CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="m.cpp", call_line=2,
        )
        store.create_calls_edge(source.id, callee.id, props)

        store.create_unresolved_call(
            self._make_gap(caller_id=source.id, call_line=3, status="pending")
        )
        store.create_unresolved_call(
            self._make_gap(caller_id=callee.id, call_line=8, status="pending")
        )
        store.create_unresolved_call(
            self._make_gap(caller_id=callee.id, call_line=9, status="unresolvable")
        )

        pending = store.get_pending_gaps_for_source(source.id)
        assert len(pending) == 2
        assert all(g.status == "pending" for g in pending)

    def test_get_pending_gaps_for_source_empty_when_all_resolved(
        self, store: InMemoryGraphStore
    ) -> None:
        source = FunctionNode(
            signature="void src()", name="src",
            file_path="m.cpp", start_line=1, end_line=5, body_hash="s",
        )
        store.create_function(source)
        store.create_unresolved_call(
            self._make_gap(caller_id=source.id, status="unresolvable")
        )
        assert store.get_pending_gaps_for_source(source.id) == []


# ---- Neo4jGraphStore (architecture.md §4 Cypher contract) -----------------


class _FakeSession:
    """Stand-in for a neo4j Session — captures cypher + params for assertions
    and returns the next configured result on ``run()``.
    """

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[tuple[str, dict]] = []

    def run(self, cypher, **params):
        self.calls.append((cypher, params))
        if self.results:
            return self.results.pop(0)
        # Default: empty result that supports both .consume() and .single().
        empty = MagicMock()
        empty.single.return_value = None
        empty.__iter__ = lambda self: iter([])
        empty.consume.return_value = None
        return empty

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeDriver:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session

    def close(self):
        pass


def _stub_result(*, single=None, records=None):
    """Build a result mock supporting `.single()`, `.consume()`, iteration."""
    res = MagicMock()
    res.single.return_value = single
    res.consume.return_value = None
    if records is not None:
        res.__iter__ = lambda self: iter(records)
    else:
        res.__iter__ = lambda self: iter([])
    return res


def _patch_driver(store: Neo4jGraphStore, session: _FakeSession):
    """Force the lazy driver to a fake — no real neo4j connection made."""
    store._driver = _FakeDriver(session)


@pytest.fixture
def neo4j_store() -> Neo4jGraphStore:
    return Neo4jGraphStore(uri="bolt://x:7687", user="u", password="p")


def test_neo4j_create_function_uses_merge(neo4j_store):
    session = _FakeSession([_stub_result()])
    _patch_driver(neo4j_store, session)
    fn = FunctionNode(
        signature="int main()", name="main",
        file_path="a.cpp", start_line=1, end_line=10, body_hash="h",
    )
    returned = neo4j_store.create_function(fn)
    assert returned == fn.id
    cypher, params = session.calls[0]
    assert "MERGE (f:Function {id: $id})" in cypher
    assert params["id"] == fn.id
    assert params["signature"] == "int main()"


def test_neo4j_create_calls_edge_uses_merge_on_call_site(neo4j_store):
    session = _FakeSession([_stub_result()])
    _patch_driver(neo4j_store, session)
    neo4j_store.create_calls_edge(
        caller_id="c1",
        callee_id="c2",
        props=CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="x.cpp", call_line=42,
        ),
    )
    cypher, params = session.calls[0]
    # Idempotency guard: MERGE keyed by (call_file, call_line) so the
    # same edge isn't duplicated across reruns (architecture.md §3
    # Agent 内循环 step 2d).
    assert "MERGE (a)-[r:CALLS {call_file: $call_file, call_line: $call_line}]->(b)" in cypher
    assert params["resolved_by"] == "llm"
    assert params["call_line"] == 42


def test_neo4j_edge_exists_returns_true_when_count_positive(neo4j_store):
    record = MagicMock()
    record.__getitem__ = lambda self, k: 1 if k == "c" else None
    session = _FakeSession([_stub_result(single=record)])
    _patch_driver(neo4j_store, session)
    assert neo4j_store.edge_exists("c1", "c2", "x.cpp", 42) is True


def test_neo4j_edge_exists_returns_false_when_zero(neo4j_store):
    record = MagicMock()
    record.__getitem__ = lambda self, k: 0 if k == "c" else None
    session = _FakeSession([_stub_result(single=record)])
    _patch_driver(neo4j_store, session)
    assert neo4j_store.edge_exists("c1", "c2", "x.cpp", 42) is False


def test_neo4j_delete_unresolved_call_emits_detach_delete(neo4j_store):
    session = _FakeSession([_stub_result()])
    _patch_driver(neo4j_store, session)
    neo4j_store.delete_unresolved_call("c1", "x.cpp", 42)
    cypher, params = session.calls[0]
    assert "DETACH DELETE u" in cypher
    assert params == {"caller_id": "c1", "call_file": "x.cpp", "call_line": 42}


def test_neo4j_update_retry_state_writes_audit_fields(neo4j_store):
    session = _FakeSession([_stub_result()])
    _patch_driver(neo4j_store, session)
    neo4j_store.update_unresolved_call_retry_state(
        call_id="g1", timestamp="2026-05-13T12:00:00+00:00",
        reason="gate_failed: still pending",
    )
    cypher, params = session.calls[0]
    assert "u.last_attempt_timestamp = $timestamp" in cypher
    assert "u.last_attempt_reason = $reason" in cypher
    assert params["reason"].startswith("gate_failed: ")


def test_neo4j_get_pending_gaps_for_source_filters_by_status(neo4j_store):
    record = {
        "id": "g1", "caller_id": "c1", "call_expression": "fp()",
        "call_file": "x.cpp", "call_line": 42, "call_type": "indirect",
        "source_code_snippet": "fp();", "var_name": "fp", "var_type": "void(*)()",
        "candidates": [], "retry_count": 0, "status": "pending",
        "last_attempt_timestamp": None, "last_attempt_reason": None,
    }
    rec_obj = MagicMock()
    rec_obj.__getitem__ = lambda self, k: record[k]
    session = _FakeSession([_stub_result(records=[rec_obj])])
    _patch_driver(neo4j_store, session)
    pending = neo4j_store.get_pending_gaps_for_source("src_001")
    assert len(pending) == 1
    assert pending[0].id == "g1"
    cypher, _ = session.calls[0]
    assert "u.status = 'pending'" in cypher
    # BFS via variable-length CALLS path so gates respect reachability.
    # Bounded by max_depth (default 50) to prevent pathological traversals.
    assert "[:CALLS*0.." in cypher


def test_neo4j_close_releases_driver(neo4j_store):
    fake_driver = MagicMock()
    neo4j_store._driver = fake_driver
    neo4j_store.close()
    fake_driver.close.assert_called_once()
    assert neo4j_store._driver is None


def test_neo4j_lazy_driver_construction(neo4j_store):
    """Driver should not be created until first call — keeps unit tests
    that mock RepairOrchestrator from needing real Neo4j connectivity.
    """
    assert neo4j_store._driver is None
    fake_driver = MagicMock()
    fake_session = _FakeSession([_stub_result()])
    fake_driver.session.return_value = fake_session
    with patch(
        "neo4j.GraphDatabase.driver", return_value=fake_driver
    ) as ctor:
        neo4j_store.create_function(
            FunctionNode(
                signature="void f()", name="f",
                file_path="a.cpp", start_line=1, end_line=2, body_hash="h",
            )
        )
    ctor.assert_called_once_with("bolt://x:7687", auth=("u", "p"))


def test_count_stats_unknown_category_bucketed_as_none(store):
    """architecture.md §8: unresolved_by_category keys must be one of
    {gate_failed, agent_error, subprocess_crash, subprocess_timeout, none}.
    Unknown prefixes in last_attempt_reason must be bucketed as 'none'."""
    gap = UnresolvedCallNode(
        caller_id="f1", call_expression="x()", call_file="a.cpp",
        call_line=1, call_type="indirect", source_code_snippet="x();",
        var_name="x", var_type="void(*)()",
        last_attempt_reason="unknown_category: some reason",
    )
    store.create_unresolved_call(gap)

    stats = store.count_stats()
    by_cat = stats["unresolved_by_category"]
    # Unknown category must NOT appear as its own key
    assert "unknown_category" not in by_cat
    # Must be bucketed as "none"
    assert by_cat.get("none", 0) == 1


def test_count_stats_valid_categories_bucketed_correctly(store):
    """architecture.md §8: valid categories are correctly bucketed."""
    for i, cat in enumerate(["gate_failed", "agent_error", "subprocess_crash", "subprocess_timeout"]):
        gap = UnresolvedCallNode(
            caller_id=f"f{i}", call_expression="x()", call_file="a.cpp",
            call_line=i + 1, call_type="indirect", source_code_snippet="x();",
            var_name="x", var_type="void(*)()",
            last_attempt_reason=f"{cat}: some detail",
        )
        store.create_unresolved_call(gap)

    stats = store.count_stats()
    by_cat = stats["unresolved_by_category"]
    assert by_cat["gate_failed"] == 1
    assert by_cat["agent_error"] == 1
    assert by_cat["subprocess_crash"] == 1
    assert by_cat["subprocess_timeout"] == 1


def test_reset_unresolvable_gaps(store):
    """architecture.md §10 line 523: 'retry_failed_gaps: true → 跨运行重试：
    下次运行时重置 unresolvable GAP 的 retry_count，重新尝试'.
    The store must provide a method to reset all unresolvable GAPs."""
    from codemap_lite.graph.schema import UnresolvedCallNode

    # Create a mix of pending and unresolvable GAPs
    gap_pending = UnresolvedCallNode(
        id="gap_p", caller_id="f1", call_expression="foo()",
        call_file="a.cpp", call_line=1, call_type="indirect",
        source_code_snippet="foo();", var_name="foo", var_type="void(*)()",
        retry_count=1, status="pending",
    )
    gap_unresolvable = UnresolvedCallNode(
        id="gap_u", caller_id="f1", call_expression="bar()",
        call_file="a.cpp", call_line=2, call_type="indirect",
        source_code_snippet="bar();", var_name="bar", var_type="void(*)()",
        retry_count=3, status="unresolvable",
        last_attempt_timestamp="2026-05-14T00:00:00Z",
        last_attempt_reason="gate_failed: remaining pending GAPs",
    )
    store.create_unresolved_call(gap_pending)
    store.create_unresolved_call(gap_unresolvable)

    # Reset unresolvable gaps
    store.reset_unresolvable_gaps()

    # The unresolvable gap should now be pending with retry_count=0
    updated = store._unresolved_calls["gap_u"]
    assert updated.status == "pending"
    assert updated.retry_count == 0
    assert updated.last_attempt_timestamp is None
    assert updated.last_attempt_reason is None

    # The pending gap should be unchanged
    unchanged = store._unresolved_calls["gap_p"]
    assert unchanged.status == "pending"
    assert unchanged.retry_count == 1


def test_neo4j_ensure_indexes_runs_on_first_connection(neo4j_store):
    """architecture.md §4 索引: Neo4jGraphStore must create required indexes
    on first connection. Indexes are idempotent (IF NOT EXISTS)."""
    session = _FakeSession([])
    _patch_driver(neo4j_store, session)

    # ensure_indexes hasn't run yet
    assert neo4j_store._indexes_ensured is False

    neo4j_store.ensure_indexes()

    assert neo4j_store._indexes_ensured is True
    # Should have run 8 CREATE INDEX + 4 CREATE CONSTRAINT statements
    # (architecture.md §4: 7 original indexes + idx_repairlog_caller,
    #  plus 4 uniqueness constraints)
    index_calls = [
        c for c in session.calls if "CREATE INDEX" in c[0]
    ]
    assert len(index_calls) == 8, (
        f"architecture.md §4: expected 8 index creation statements, got {len(index_calls)}"
    )

    constraint_calls = [
        c for c in session.calls if "CREATE CONSTRAINT" in c[0]
    ]
    assert len(constraint_calls) == 4, (
        f"expected 4 uniqueness constraints, got {len(constraint_calls)}"
    )

    # Verify specific indexes
    all_cypher = " ".join(c[0] for c in index_calls)
    assert "idx_file_hash" in all_cypher
    assert "idx_function_file" in all_cypher
    assert "idx_function_sig" in all_cypher
    assert "idx_source_kind" in all_cypher
    assert "idx_calls_resolved" in all_cypher
    assert "idx_gap_status" in all_cypher
    assert "idx_gap_caller" in all_cypher
    assert "idx_repairlog_caller" in all_cypher

    # Verify specific constraints
    all_constraint_cypher = " ".join(c[0] for c in constraint_calls)
    assert "uniq_function_id" in all_constraint_cypher
    assert "uniq_file_path" in all_constraint_cypher
    assert "uniq_repairlog_key" in all_constraint_cypher
    assert "uniq_uc_key" in all_constraint_cypher


def test_neo4j_ensure_indexes_is_idempotent(neo4j_store):
    """ensure_indexes() must be safe to call multiple times."""
    session = _FakeSession([])
    _patch_driver(neo4j_store, session)

    neo4j_store.ensure_indexes()
    first_count = len(session.calls)

    neo4j_store.ensure_indexes()
    # No additional calls on second invocation
    assert len(session.calls) == first_count


def test_create_unresolved_call_deduplicates_on_logical_key(store):
    """architecture.md §4: UnresolvedCall is unique by (caller_id, call_file, call_line).

    Calling create_unresolved_call twice for the same logical key must NOT
    produce duplicate nodes — it should update the existing one (MERGE semantics).
    """
    uc1 = UnresolvedCallNode(
        caller_id="func_A",
        call_expression="foo()",
        call_file="src/main.cpp",
        call_line=42,
        call_type="indirect",
        source_code_snippet="foo();",
        var_name=None,
        var_type=None,
        retry_count=2,
        status="pending",
    )
    uc2 = UnresolvedCallNode(
        caller_id="func_A",
        call_expression="foo()",
        call_file="src/main.cpp",
        call_line=42,
        call_type="indirect",
        source_code_snippet="foo();",
        var_name=None,
        var_type=None,
        retry_count=0,  # reset
        status="pending",
    )

    store.create_unresolved_call(uc1)
    store.create_unresolved_call(uc2)

    # Should have exactly 1 UnresolvedCall for this logical key
    gaps = store.get_pending_gaps_for_source("func_A")
    matching = [
        g for g in gaps
        if g.call_file == "src/main.cpp" and g.call_line == 42
    ]
    assert len(matching) == 1, (
        f"Expected 1 UnresolvedCall for (func_A, src/main.cpp, 42), got {len(matching)}"
    )
    # The second call should have updated retry_count to 0
    assert matching[0].retry_count == 0


def test_create_repair_log_deduplicates_on_logical_key(store):
    """architecture.md §4: RepairLog is unique by (caller_id, callee_id, call_location).

    Calling create_repair_log twice for the same logical key must update
    the existing node (MERGE semantics), not create a duplicate.
    """
    log1 = RepairLogNode(
        caller_id="func_A",
        callee_id="func_B",
        call_location="src/main.cpp:42",
        repair_method="llm",
        llm_response="first response",
        timestamp="2026-05-13T00:00:00Z",
        reasoning_summary="first reasoning",
    )
    log2 = RepairLogNode(
        caller_id="func_A",
        callee_id="func_B",
        call_location="src/main.cpp:42",
        repair_method="llm",
        llm_response="second response",
        timestamp="2026-05-13T01:00:00Z",
        reasoning_summary="second reasoning",
    )

    store.create_repair_log(log1)
    store.create_repair_log(log2)

    # Should have exactly 1 RepairLog for this logical key
    logs = store.get_repair_logs(
        caller_id="func_A", callee_id="func_B", call_location="src/main.cpp:42"
    )
    assert len(logs) == 1, (
        f"Expected 1 RepairLog for (func_A, func_B, src/main.cpp:42), got {len(logs)}"
    )
    # The second call should have updated the content
    assert logs[0].llm_response == "second response"
    assert logs[0].reasoning_summary == "second reasoning"


def test_source_point_node_stores_module_field(store):
    """architecture.md §4: SourcePoint schema includes module field."""
    from codemap_lite.graph.schema import SourcePointNode

    sp = SourcePointNode(
        function_id="func_A",
        entry_point_kind="api",
        reason="HTTP handler",
        module="network",
        status="pending",
        id="sp_001",
    )
    store.create_source_point(sp)
    retrieved = store.get_source_point("sp_001")
    assert retrieved is not None
    assert retrieved.module == "network"

    # Module preserved across status update
    store.update_source_point_status("sp_001", "running")
    updated = store.get_source_point("sp_001")
    assert updated.status == "running"
    assert updated.module == "network"


def test_create_calls_edge_preserves_resolved_by_on_duplicate(store):
    """architecture.md §4: resolved_by reflects the FIRST resolution method.

    If an edge is first resolved by symbol_table and then create_calls_edge
    is called again with resolved_by=llm, the original resolved_by must be
    preserved (not overwritten).
    """
    store.create_function(FunctionNode(
        id="f1", name="f1", signature="void f1()",
        file_path="a.c", start_line=1, end_line=5, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="f2", name="f2", signature="void f2()",
        file_path="a.c", start_line=10, end_line=15, body_hash="h2",
    ))

    # First resolution: symbol_table
    props1 = CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="a.c", call_line=3,
    )
    store.create_calls_edge("f1", "f2", props1)

    # Second call with different resolved_by (e.g., LLM also resolves it)
    props2 = CallsEdgeProps(
        resolved_by="llm", call_type="direct",
        call_file="a.c", call_line=3,
    )
    store.create_calls_edge("f1", "f2", props2)

    # resolved_by must still be "symbol_table" (first wins)
    edges = store.list_calls_edges()
    matching = [
        e for e in edges
        if e.caller_id == "f1" and e.callee_id == "f2"
        and e.props.call_file == "a.c" and e.props.call_line == 3
    ]
    assert len(matching) == 1, f"Expected 1 edge, got {len(matching)}"
    assert matching[0].props.resolved_by == "symbol_table", (
        f"resolved_by should be preserved as 'symbol_table', got '{matching[0].props.resolved_by}'"
    )


def test_get_pending_gaps_for_source_includes_source_itself(store):
    """architecture.md §3 门禁机制: get_pending_gaps_for_source must find
    UnresolvedCalls on the source function itself (depth 0), not just
    on callees reachable via CALLS edges.

    This matches Neo4j's [:CALLS*0..] semantics where *0 includes the
    starting node.
    """
    # Create a source function with a gap directly on it
    store.create_function(FunctionNode(
        id="src_self", signature="void src()", name="src",
        file_path="a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_unresolved_call(UnresolvedCallNode(
        id="gap_on_source",
        caller_id="src_self",
        call_expression="ptr->method()",
        call_file="a.cpp",
        call_line=5,
        call_type="indirect",
        source_code_snippet="ptr->method();",
        var_name="ptr",
        var_type="Base*",
        candidates=["Derived::method"],
        status="pending",
    ))

    pending = store.get_pending_gaps_for_source("src_self")
    assert len(pending) == 1
    assert pending[0].id == "gap_on_source"


def test_get_pending_gaps_for_source_multi_hop(store):
    """architecture.md §3: BFS must traverse multiple hops to find all
    pending gaps in the reachable subgraph.

    Graph: src → A → B, with gaps on both A and B.
    """
    store.create_function(FunctionNode(
        id="src_hop", signature="void src()", name="src",
        file_path="a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="func_a", signature="void a()", name="a",
        file_path="a.cpp", start_line=20, end_line=30, body_hash="h2",
    ))
    store.create_function(FunctionNode(
        id="func_b", signature="void b()", name="b",
        file_path="b.cpp", start_line=1, end_line=10, body_hash="h3",
    ))
    # Edges: src → A → B
    store.create_calls_edge("src_hop", "func_a", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="a.cpp", call_line=5,
    ))
    store.create_calls_edge("func_a", "func_b", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="a.cpp", call_line=25,
    ))
    # Gaps on A and B
    store.create_unresolved_call(UnresolvedCallNode(
        id="gap_on_a",
        caller_id="func_a",
        call_expression="x()",
        call_file="a.cpp",
        call_line=22,
        call_type="indirect",
        source_code_snippet="x();",
        var_name=None,
        var_type=None,
        candidates=["X::call"],
        status="pending",
    ))
    store.create_unresolved_call(UnresolvedCallNode(
        id="gap_on_b",
        caller_id="func_b",
        call_expression="y()",
        call_file="b.cpp",
        call_line=5,
        call_type="indirect",
        source_code_snippet="y();",
        var_name=None,
        var_type=None,
        candidates=["Y::call"],
        status="pending",
    ))

    pending = store.get_pending_gaps_for_source("src_hop")
    pending_ids = {g.id for g in pending}
    assert "gap_on_a" in pending_ids, "Gap on hop-1 callee must be found"
    assert "gap_on_b" in pending_ids, "Gap on hop-2 callee must be found"
    assert len(pending) == 2


def test_get_pending_gaps_excludes_non_pending(store):
    """architecture.md §3: only status='pending' gaps are returned.

    Gaps with status='unresolvable' should NOT appear in pending results.
    """
    store.create_function(FunctionNode(
        id="src_status", signature="void src()", name="src",
        file_path="a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_unresolved_call(UnresolvedCallNode(
        id="gap_pending",
        caller_id="src_status",
        call_expression="a()",
        call_file="a.cpp",
        call_line=3,
        call_type="indirect",
        source_code_snippet="a();",
        var_name=None,
        var_type=None,
        candidates=["A"],
        status="pending",
    ))
    store.create_unresolved_call(UnresolvedCallNode(
        id="gap_unresolvable",
        caller_id="src_status",
        call_expression="b()",
        call_file="a.cpp",
        call_line=7,
        call_type="indirect",
        source_code_snippet="b();",
        var_name=None,
        var_type=None,
        candidates=["B"],
        status="unresolvable",
    ))

    pending = store.get_pending_gaps_for_source("src_status")
    assert len(pending) == 1
    assert pending[0].id == "gap_pending"


def test_get_pending_gaps_for_source_handles_cycle(store):
    """architecture.md §3: BFS must terminate on cyclic call graphs.

    Graph: src → A → B → A (cycle). Gap on B.
    BFS must find the gap without infinite loop.
    """
    store.create_function(FunctionNode(
        id="src_cycle", signature="void src()", name="src",
        file_path="a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="func_a_cycle", signature="void a()", name="a",
        file_path="a.cpp", start_line=20, end_line=30, body_hash="h2",
    ))
    store.create_function(FunctionNode(
        id="func_b_cycle", signature="void b()", name="b",
        file_path="b.cpp", start_line=1, end_line=10, body_hash="h3",
    ))
    # Edges: src → A → B → A (cycle)
    store.create_calls_edge("src_cycle", "func_a_cycle", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="a.cpp", call_line=5,
    ))
    store.create_calls_edge("func_a_cycle", "func_b_cycle", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="a.cpp", call_line=25,
    ))
    store.create_calls_edge("func_b_cycle", "func_a_cycle", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="b.cpp", call_line=5,
    ))
    # Gap on B
    store.create_unresolved_call(UnresolvedCallNode(
        id="gap_in_cycle",
        caller_id="func_b_cycle",
        call_expression="callback()",
        call_file="b.cpp",
        call_line=8,
        call_type="indirect",
        source_code_snippet="callback();",
        var_name=None,
        var_type=None,
        status="pending",
    ))

    # Must terminate and find the gap
    pending = store.get_pending_gaps_for_source("src_cycle")
    assert len(pending) == 1
    assert pending[0].id == "gap_in_cycle"


def test_get_reachable_subgraph_handles_cycle(store):
    """BFS in get_reachable_subgraph must terminate on cyclic graphs.

    Graph: A → B → C → A (cycle).
    Must return all 3 nodes and 3 edges without infinite loop.
    """
    store.create_function(FunctionNode(
        id="cyc_a", signature="void a()", name="a",
        file_path="a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="cyc_b", signature="void b()", name="b",
        file_path="a.cpp", start_line=20, end_line=30, body_hash="h2",
    ))
    store.create_function(FunctionNode(
        id="cyc_c", signature="void c()", name="c",
        file_path="a.cpp", start_line=40, end_line=50, body_hash="h3",
    ))
    store.create_calls_edge("cyc_a", "cyc_b", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="a.cpp", call_line=5,
    ))
    store.create_calls_edge("cyc_b", "cyc_c", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="a.cpp", call_line=25,
    ))
    store.create_calls_edge("cyc_c", "cyc_a", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="a.cpp", call_line=45,
    ))

    result = store.get_reachable_subgraph("cyc_a")
    node_ids = {n.id for n in result["nodes"]}
    assert node_ids == {"cyc_a", "cyc_b", "cyc_c"}
    assert len(result["edges"]) == 3


def test_get_reachable_subgraph_self_loop(store):
    """A function that calls itself (recursion) must not cause infinite BFS."""
    store.create_function(FunctionNode(
        id="recursive", signature="void rec()", name="rec",
        file_path="a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_calls_edge("recursive", "recursive", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="a.cpp", call_line=5,
    ))

    result = store.get_reachable_subgraph("recursive")
    assert len(result["nodes"]) == 1
    assert result["nodes"][0].id == "recursive"
    assert len(result["edges"]) == 1
    """architecture.md §4: UC status ∈ {pending, unresolvable}.

    With schema validation in place, invalid status values are rejected
    at UnresolvedCallNode construction time — they can never enter the store.
    """
    with pytest.raises(ValueError):
        UnresolvedCallNode(
            id="gap_weird",
            caller_id="f1",
            call_expression="b()",
            call_file="a.cpp",
            call_line=7,
            call_type="indirect",
            source_code_snippet="b();",
            var_name=None,
            var_type=None,
            candidates=["B"],
            status="some_invalid_status",
        )


def test_get_calls_edge_returns_props(store):
    """architecture.md §5: review endpoint uses get_calls_edge to verify
    an edge exists before operating on it. Must return CallsEdgeProps."""
    store.create_function(FunctionNode(
        id="f1", signature="void f()", name="f",
        file_path="a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="f2", signature="void g()", name="g",
        file_path="a.cpp", start_line=20, end_line=30, body_hash="h2",
    ))
    store.create_calls_edge("f1", "f2", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="a.cpp", call_line=5,
    ))

    props = store.get_calls_edge("f1", "f2", "a.cpp", 5)
    assert props is not None
    assert props.resolved_by == "llm"
    assert props.call_type == "indirect"
    assert props.call_file == "a.cpp"
    assert props.call_line == 5


def test_get_calls_edge_returns_none_for_nonexistent(store):
    """get_calls_edge must return None when no matching edge exists."""
    result = store.get_calls_edge("no_such", "no_such", "x.cpp", 99)
    assert result is None


def test_delete_calls_edge_removes_and_returns_true(store):
    """architecture.md §5 审阅交互: delete_calls_edge must remove the
    specific edge and return True."""
    store.create_function(FunctionNode(
        id="f1", signature="void f()", name="f",
        file_path="a.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="f2", signature="void g()", name="g",
        file_path="a.cpp", start_line=20, end_line=30, body_hash="h2",
    ))
    store.create_calls_edge("f1", "f2", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="a.cpp", call_line=5,
    ))

    deleted = store.delete_calls_edge("f1", "f2", "a.cpp", 5)
    assert deleted is True
    # Edge should be gone
    assert store.get_calls_edge("f1", "f2", "a.cpp", 5) is None
    assert not store.edge_exists("f1", "f2", "a.cpp", 5)


def test_delete_calls_edge_returns_false_for_nonexistent(store):
    """delete_calls_edge must return False when no matching edge exists."""
    deleted = store.delete_calls_edge("no_such", "no_such", "x.cpp", 99)
    assert deleted is False


# ============================================================
# §4 Schema Validation: enum constraints on node/edge fields
# ============================================================


class TestUnresolvedCallStatusEnum:
    """architecture.md §4: UnresolvedCall.status ∈ {"pending", "unresolvable"}."""

    def test_valid_statuses_accepted(self):
        """Both 'pending' and 'unresolvable' must be accepted."""
        uc_pending = UnresolvedCallNode(
            caller_id="f1", call_expression="x()", call_file="a.cpp",
            call_line=1, call_type="indirect", source_code_snippet="x();",
            var_name=None, var_type=None, status="pending",
        )
        assert uc_pending.status == "pending"

        uc_unresolvable = UnresolvedCallNode(
            caller_id="f1", call_expression="x()", call_file="a.cpp",
            call_line=2, call_type="indirect", source_code_snippet="x();",
            var_name=None, var_type=None, status="unresolvable",
        )
        assert uc_unresolvable.status == "unresolvable"

    def test_invalid_status_rejected(self):
        """Invalid status values must raise ValueError at construction time."""
        with pytest.raises(ValueError):
            UnresolvedCallNode(
                caller_id="f1", call_expression="x()", call_file="a.cpp",
                call_line=1, call_type="indirect", source_code_snippet="x();",
                var_name=None, var_type=None, status="running",
            )

    def test_empty_status_rejected(self):
        """Empty string is not a valid status."""
        with pytest.raises(ValueError):
            UnresolvedCallNode(
                caller_id="f1", call_expression="x()", call_file="a.cpp",
                call_line=1, call_type="indirect", source_code_snippet="x();",
                var_name=None, var_type=None, status="",
            )


class TestCallsEdgeResolvedByEnum:
    """architecture.md §4: CALLS.resolved_by ∈
    {"symbol_table", "signature", "dataflow", "context", "llm"}."""

    def test_valid_resolved_by_accepted(self):
        for rb in ("symbol_table", "signature", "dataflow", "context", "llm"):
            props = CallsEdgeProps(
                resolved_by=rb, call_type="direct",
                call_file="a.cpp", call_line=1,
            )
            assert props.resolved_by == rb

    def test_invalid_resolved_by_rejected(self):
        """Invalid resolved_by must raise ValueError."""
        with pytest.raises(ValueError):
            CallsEdgeProps(
                resolved_by="magic", call_type="direct",
                call_file="a.cpp", call_line=1,
            )

    def test_empty_resolved_by_rejected(self):
        with pytest.raises(ValueError):
            CallsEdgeProps(
                resolved_by="", call_type="direct",
                call_file="a.cpp", call_line=1,
            )


class TestCallsEdgeCallTypeEnum:
    """architecture.md §4: CALLS.call_type ∈ {"direct", "indirect", "virtual"}."""

    def test_valid_call_types_accepted(self):
        for ct in ("direct", "indirect", "virtual"):
            props = CallsEdgeProps(
                resolved_by="symbol_table", call_type=ct,
                call_file="a.cpp", call_line=1,
            )
            assert props.call_type == ct

    def test_invalid_call_type_rejected(self):
        with pytest.raises(ValueError):
            CallsEdgeProps(
                resolved_by="symbol_table", call_type="callback",
                call_file="a.cpp", call_line=1,
            )


class TestSourcePointStatusTransitions:
    """architecture.md §4: SourcePoint status transitions are forward-only.

    Valid: pending → running → complete | partial_complete.
    Invalid: running → pending, complete → running, etc.
    """

    def test_forward_transitions_accepted(self, store):
        """pending→running→complete is valid."""
        from codemap_lite.graph.schema import SourcePointNode

        sp = SourcePointNode(
            id="sp1", function_id="f1", entry_point_kind="api",
            reason="handler", status="pending",
        )
        store.create_source_point(sp)

        store.update_source_point_status("sp1", "running")
        assert store.get_source_point("sp1").status == "running"

        store.update_source_point_status("sp1", "complete")
        assert store.get_source_point("sp1").status == "complete"

    def test_forward_to_partial_complete(self, store):
        """pending→running→partial_complete is valid."""
        from codemap_lite.graph.schema import SourcePointNode

        sp = SourcePointNode(
            id="sp2", function_id="f2", entry_point_kind="api",
            reason="handler", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp2", "running")
        store.update_source_point_status("sp2", "partial_complete")
        assert store.get_source_point("sp2").status == "partial_complete"

    def test_backward_transition_rejected(self, store):
        """running→pending must raise ValueError (backward transition)."""
        from codemap_lite.graph.schema import SourcePointNode

        sp = SourcePointNode(
            id="sp3", function_id="f3", entry_point_kind="api",
            reason="handler", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp3", "running")

        with pytest.raises(ValueError):
            store.update_source_point_status("sp3", "pending")

    def test_complete_to_running_rejected(self, store):
        """complete→running must raise ValueError."""
        from codemap_lite.graph.schema import SourcePointNode

        sp = SourcePointNode(
            id="sp4", function_id="f4", entry_point_kind="api",
            reason="handler", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp4", "running")
        store.update_source_point_status("sp4", "complete")

        with pytest.raises(ValueError):
            store.update_source_point_status("sp4", "running")

    def test_invalid_status_value_rejected(self, store):
        """Unknown status value must raise ValueError."""
        from codemap_lite.graph.schema import SourcePointNode

        sp = SourcePointNode(
            id="sp5", function_id="f5", entry_point_kind="api",
            reason="handler", status="pending",
        )
        store.create_source_point(sp)

        with pytest.raises(ValueError):
            store.update_source_point_status("sp5", "cancelled")

    def test_pending_reset_allowed_from_any_state(self, store):
        """architecture.md §7 cascade: invalidate_file resets SourcePoint to
        'pending' regardless of current state. This is the ONLY backward
        transition allowed — it's an explicit reset, not a normal transition.

        The store must accept a force_reset=True parameter for this case.
        """
        from codemap_lite.graph.schema import SourcePointNode

        sp = SourcePointNode(
            id="sp6", function_id="f6", entry_point_kind="api",
            reason="handler", status="pending",
        )
        store.create_source_point(sp)
        store.update_source_point_status("sp6", "running")
        store.update_source_point_status("sp6", "complete")

        # Normal backward transition should fail
        with pytest.raises(ValueError):
            store.update_source_point_status("sp6", "pending")

        # But force_reset=True (used by cascade invalidation) must succeed
        store.update_source_point_status("sp6", "pending", force_reset=True)
        assert store.get_source_point("sp6").status == "pending"


class TestUnresolvedCallReasonFormat:
    """architecture.md §3: last_attempt_reason format is
    '<category>: <summary>' where category ∈
    {gate_failed, agent_error, subprocess_timeout, subprocess_crash},
    and total length ≤ 200 chars."""

    def test_valid_reasons_accepted(self, store):
        """All valid category prefixes must be accepted."""
        from codemap_lite.graph.schema import SourcePointNode

        for i, cat in enumerate([
            "gate_failed", "agent_error",
            "subprocess_timeout", "subprocess_crash",
        ]):
            uc = UnresolvedCallNode(
                id=f"uc_reason_{i}",
                caller_id="f1", call_expression="x()", call_file="a.cpp",
                call_line=i + 10, call_type="indirect",
                source_code_snippet="x();", var_name=None, var_type=None,
                status="pending", retry_count=1,
                last_attempt_timestamp="2026-05-14T00:00:00Z",
                last_attempt_reason=f"{cat}: some detail here",
            )
            store.create_unresolved_call(uc)
            store.update_unresolved_call_retry_state(
                uc.id, "2026-05-14T01:00:00Z", f"{cat}: updated detail"
            )
            updated = store._unresolved_calls[uc.id]
            assert updated.last_attempt_reason == f"{cat}: updated detail"

    def test_invalid_category_rejected(self, store):
        """Reason with invalid category prefix must raise ValueError."""
        uc = UnresolvedCallNode(
            id="uc_bad_reason",
            caller_id="f1", call_expression="x()", call_file="a.cpp",
            call_line=100, call_type="indirect",
            source_code_snippet="x();", var_name=None, var_type=None,
            status="pending",
        )
        store.create_unresolved_call(uc)

        with pytest.raises(ValueError):
            store.update_unresolved_call_retry_state(
                uc.id, "2026-05-14T00:00:00Z", "unknown_cat: bad"
            )

    def test_reason_exceeding_200_chars_rejected(self, store):
        """Reason longer than 200 characters must raise ValueError."""
        uc = UnresolvedCallNode(
            id="uc_long_reason",
            caller_id="f1", call_expression="x()", call_file="a.cpp",
            call_line=101, call_type="indirect",
            source_code_snippet="x();", var_name=None, var_type=None,
            status="pending",
        )
        store.create_unresolved_call(uc)

        long_reason = "gate_failed: " + "x" * 200  # > 200 total
        with pytest.raises(ValueError):
            store.update_unresolved_call_retry_state(
                uc.id, "2026-05-14T00:00:00Z", long_reason
            )

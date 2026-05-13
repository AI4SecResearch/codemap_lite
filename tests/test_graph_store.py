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
            resolved_by="static",
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
            resolved_by="static",
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
            status="resolved",
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
            resolved_by="static",
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
            resolved_by="static", call_type="direct",
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
            resolved_by="static", call_type="direct",
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
            self._make_gap(caller_id=callee.id, call_line=9, status="resolved")
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
            self._make_gap(caller_id=source.id, status="resolved")
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
    assert "[:CALLS*0..]" in cypher


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
    # Should have run 7 CREATE INDEX statements (architecture.md §4)
    index_calls = [
        c for c in session.calls if "CREATE INDEX" in c[0]
    ]
    assert len(index_calls) == 7, (
        f"architecture.md §4: expected 7 index creation statements, got {len(index_calls)}"
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

"""Tests for the graph storage layer (Phase 1.7)."""
from __future__ import annotations

import pytest

from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FileNode,
    FunctionNode,
    RepairLogNode,
    UnresolvedCallNode,
)
from codemap_lite.graph.neo4j_store import InMemoryGraphStore


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

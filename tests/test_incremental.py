"""Tests for incremental update — 5-step cascade logic."""
import pytest
from pathlib import Path

from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import FunctionNode, CallsEdgeProps, UnresolvedCallNode
from codemap_lite.graph.incremental import IncrementalUpdater


@pytest.fixture
def store_with_data():
    """Store pre-populated with functions and edges."""
    store = InMemoryGraphStore()

    # Create functions
    store.create_function(FunctionNode(
        id="f1", name="caller", signature="void caller()",
        file_path="src/a.cpp", start_line=1, end_line=5, body_hash="hash_a1",
    ))
    store.create_function(FunctionNode(
        id="f2", name="callee", signature="void callee()",
        file_path="src/a.cpp", start_line=10, end_line=15, body_hash="hash_a2",
    ))
    store.create_function(FunctionNode(
        id="f3", name="other", signature="void other()",
        file_path="src/b.cpp", start_line=1, end_line=5, body_hash="hash_b1",
    ))

    # Create edges
    store.create_calls_edge("f1", "f2", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct", call_file="src/a.cpp", call_line=3,
    ))
    store.create_calls_edge("f3", "f1", CallsEdgeProps(
        resolved_by="llm", call_type="indirect", call_file="src/b.cpp", call_line=3,
    ))

    return store


def test_invalidate_file_removes_functions(store_with_data):
    updater = IncrementalUpdater(store=store_with_data)
    updater.invalidate_file("src/a.cpp")

    # Functions from a.cpp should be removed
    assert store_with_data.get_function_by_id("f1") is None
    assert store_with_data.get_function_by_id("f2") is None
    # Function from b.cpp should remain
    assert store_with_data.get_function_by_id("f3") is not None


def test_invalidate_file_removes_associated_edges(store_with_data):
    updater = IncrementalUpdater(store=store_with_data)
    updater.invalidate_file("src/a.cpp")

    # Edge from f1→f2 should be gone (both in a.cpp)
    callees = store_with_data.get_callees("f1")
    assert len(callees) == 0


def test_cascade_invalidates_llm_edges_pointing_to_changed_functions(store_with_data):
    updater = IncrementalUpdater(store=store_with_data)

    # Invalidate a.cpp — f3→f1 is an LLM edge pointing to f1 (in a.cpp)
    # This should cascade: the LLM edge from f3→f1 should be invalidated
    invalidated = updater.invalidate_file("src/a.cpp")

    assert "f3" in invalidated.affected_callers


def test_get_functions_in_file(store_with_data):
    updater = IncrementalUpdater(store=store_with_data)
    funcs = updater._get_functions_in_file("src/a.cpp")
    assert len(funcs) == 2
    names = {f.name for f in funcs}
    assert "caller" in names
    assert "callee" in names


def test_invalidate_file_removes_unresolved_calls():
    """architecture.md §7: invalidation must delete UnresolvedCall nodes
    whose caller_id belongs to a deleted function."""
    store = InMemoryGraphStore()

    store.create_function(FunctionNode(
        id="f1", name="caller", signature="void caller()",
        file_path="src/a.cpp", start_line=1, end_line=5, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="f2", name="other", signature="void other()",
        file_path="src/b.cpp", start_line=1, end_line=5, body_hash="h2",
    ))

    # UnresolvedCall belonging to f1 (in a.cpp)
    gap1 = UnresolvedCallNode(
        caller_id="f1",
        call_expression="fn_ptr(x)",
        call_file="src/a.cpp",
        call_line=3,
        call_type="indirect",
        source_code_snippet="fn_ptr(x);",
        var_name="fn_ptr",
        var_type="void (*)(int)",
    )
    store.create_unresolved_call(gap1)

    # UnresolvedCall belonging to f2 (in b.cpp) — should survive
    gap2 = UnresolvedCallNode(
        caller_id="f2",
        call_expression="cb(y)",
        call_file="src/b.cpp",
        call_line=2,
        call_type="indirect",
        source_code_snippet="cb(y);",
        var_name="cb",
        var_type="void (*)(int)",
    )
    store.create_unresolved_call(gap2)

    updater = IncrementalUpdater(store=store)
    result = updater.invalidate_file("src/a.cpp")

    # gap1 should be removed (caller f1 is in a.cpp)
    assert gap1.id in result.removed_unresolved_calls
    assert gap1.id not in store._unresolved_calls

    # gap2 should survive (caller f2 is in b.cpp)
    assert gap2.id in store._unresolved_calls

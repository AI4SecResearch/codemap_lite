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


def test_invalidate_file_reports_removed_edges_count(store_with_data):
    """architecture.md §7: InvalidationResult.removed_edges must report
    the number of CALLS edges deleted during cascade invalidation."""
    updater = IncrementalUpdater(store=store_with_data)

    # Before: 2 edges (f1→f2 and f3→f1)
    assert len(store_with_data.list_calls_edges()) == 2

    result = updater.invalidate_file("src/a.cpp")

    # f1→f2 (both in a.cpp) and f3→f1 (f1 is in a.cpp) should both be deleted
    assert result.removed_edges > 0, (
        "removed_edges must be populated — currently always 0 (bug)"
    )
    # After invalidation, only edges not touching a.cpp functions remain
    remaining = store_with_data.list_calls_edges()
    assert len(remaining) == 0  # both edges touch f1 or f2


def test_cascade_regenerates_unresolved_calls_for_affected_callers():
    """architecture.md §7 step 3: '变更函数的 callers 中如有 LLM 修复的边指向旧函数
    → 删除该 CALLS 边 + 对应 RepairLog，重新生成 UnresolvedCall'.

    When an LLM edge A→B is invalidated because B's file changed, the
    IncrementalUpdater must create a new UnresolvedCall for caller A so
    the repair agent can re-resolve it in the next run."""
    store = InMemoryGraphStore()

    # A (in b.cpp) calls B (in a.cpp) via LLM-resolved edge
    store.create_function(FunctionNode(
        id="A", name="caller_a", signature="void caller_a()",
        file_path="src/b.cpp", start_line=1, end_line=10, body_hash="hA",
    ))
    store.create_function(FunctionNode(
        id="B", name="callee_b", signature="void callee_b()",
        file_path="src/a.cpp", start_line=1, end_line=10, body_hash="hB",
    ))
    store.create_calls_edge("A", "B", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="src/b.cpp", call_line=5,
    ))

    updater = IncrementalUpdater(store=store)
    result = updater.invalidate_file("src/a.cpp")

    # A should be in affected_callers
    assert "A" in result.affected_callers

    # A new UnresolvedCall should be regenerated for caller A
    gaps = store.get_unresolved_calls(caller_id="A")
    assert len(gaps) == 1, (
        "architecture.md §7 step 3: must regenerate UnresolvedCall for "
        "affected callers after LLM edge invalidation"
    )
    assert gaps[0].call_file == "src/b.cpp"
    assert gaps[0].call_line == 5
    assert gaps[0].call_type == "indirect"

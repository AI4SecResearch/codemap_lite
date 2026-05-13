"""Tests for incremental update — 5-step cascade logic."""
import pytest
from pathlib import Path

from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    FunctionNode, CallsEdgeProps, RepairLogNode, UnresolvedCallNode,
)
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
    # architecture.md §7 step 3: regenerated UC must have retry_count=0
    # so the repair agent treats it as a fresh GAP (not a stale retry).
    assert gaps[0].retry_count == 0
    assert gaps[0].status == "pending"


def test_pipeline_incremental_invalidates_modified_files():
    """architecture.md §7 step 2: '变更文件重解析：删除旧 Function 节点及关联
    CALLS 边 + UnresolvedCall，重新解析'. Modified files must be invalidated
    before re-parsing so stale functions from the old version are removed."""
    from unittest.mock import patch, MagicMock
    from codemap_lite.pipeline.orchestrator import PipelineOrchestrator

    store = InMemoryGraphStore()
    # Pre-populate a function that will be "modified" (old version)
    store.create_function(FunctionNode(
        id="f1", name="old_func", signature="void old_func()",
        file_path="src/modified.cpp", start_line=1, end_line=5, body_hash="h_old",
    ))
    # An edge from old_func to another function
    store.create_function(FunctionNode(
        id="f2", name="other", signature="void other()",
        file_path="src/other.cpp", start_line=1, end_line=5, body_hash="h2",
    ))
    store.create_calls_edge("f1", "f2", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="src/modified.cpp", call_line=3,
    ))

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        target_dir = Path(tmpdir)
        orch = PipelineOrchestrator(target_dir=target_dir, store=store)

        # Mock the scanner to report a modified file
        changes = MagicMock()
        changes.added = []
        changes.modified = ["src/modified.cpp"]
        changes.deleted = []

        with patch.object(orch._scanner, "detect_changes", return_value=changes), \
             patch.object(orch._scanner, "scan", return_value=[]), \
             patch.object(orch._scanner, "save_state"):
            result = orch.run_incremental_analysis()

        # The old function should be invalidated (removed before re-parse)
        assert store.get_function_by_id("f1") is None, (
            "architecture.md §7: modified file's old functions must be "
            "invalidated before re-parsing"
        )
        # The edge from old_func should also be gone
        assert len(store.list_calls_edges()) == 0


def test_pipeline_incremental_invalidates_deleted_files():
    """architecture.md §7 step 2: 'deleted files → invalidate all functions
    in that file + cascade'. The PipelineOrchestrator.run_incremental_analysis
    must call IncrementalUpdater.invalidate_file for deleted files."""
    from unittest.mock import patch, MagicMock
    from codemap_lite.pipeline.orchestrator import PipelineOrchestrator

    store = InMemoryGraphStore()
    # Pre-populate a function in a file that will be "deleted"
    store.create_function(FunctionNode(
        id="f1", name="old_func", signature="void old_func()",
        file_path="src/deleted.cpp", start_line=1, end_line=5, body_hash="h1",
    ))

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        target_dir = Path(tmpdir)
        orch = PipelineOrchestrator(target_dir=target_dir, store=store)

        # Mock the scanner to report a deleted file
        changes = MagicMock()
        changes.added = []
        changes.modified = []
        changes.deleted = ["src/deleted.cpp"]

        with patch.object(orch._scanner, "detect_changes", return_value=changes), \
             patch.object(orch._scanner, "scan", return_value=[]), \
             patch.object(orch._scanner, "save_state"):
            result = orch.run_incremental_analysis()

        # The function from the deleted file should be removed
        assert store.get_function_by_id("f1") is None, (
            "architecture.md §7: deleted file's functions must be invalidated"
        )


def test_invalidate_file_marks_non_llm_callers_as_affected():
    """architecture.md §7 step 3: when a function is modified, callers with
    non-LLM edges (symbol_table) pointing to it must be marked as affected
    so their files get re-parsed and the edges are re-discovered."""
    store = InMemoryGraphStore()

    # foo (in a.cpp) calls bar (in b.cpp) via symbol_table
    store.create_function(FunctionNode(
        id="foo", name="foo", signature="void foo()",
        file_path="src/a.cpp", start_line=1, end_line=5, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="bar", name="bar", signature="void bar()",
        file_path="src/b.cpp", start_line=1, end_line=5, body_hash="h2",
    ))
    store.create_calls_edge("foo", "bar", CallsEdgeProps(
        resolved_by="symbol_table", call_type="direct",
        call_file="src/a.cpp", call_line=3,
    ))

    updater = IncrementalUpdater(store=store)
    result = updater.invalidate_file("src/b.cpp")

    # foo should be in affected_callers (its file needs re-parsing)
    assert "foo" in result.affected_callers, (
        "architecture.md §7: non-LLM callers of modified functions must be "
        "marked as affected so their files get re-parsed"
    )
    # The edge should be deleted
    assert len(store.list_calls_edges()) == 0
    # bar should be removed
    assert store.get_function_by_id("bar") is None


def test_cascade_invalidation_deletes_repair_log_for_llm_edges():
    """architecture.md §7 step 3: cascade invalidation must delete RepairLog
    entries for LLM-resolved edges pointing to changed functions.

    Without this, stale RepairLog entries remain in Neo4j and the frontend
    shows ghost audit trails for edges that no longer exist."""
    store = InMemoryGraphStore()

    # Setup: caller 'foo' in file_a.c, callee 'bar' in file_b.c
    foo = FunctionNode(
        id="foo", signature="void foo()", name="foo",
        file_path="file_a.c", start_line=1, end_line=10, body_hash="h1",
    )
    bar = FunctionNode(
        id="bar", signature="void bar()", name="bar",
        file_path="file_b.c", start_line=1, end_line=10, body_hash="h2",
    )
    store.create_function(foo)
    store.create_function(bar)

    # LLM-resolved edge from foo → bar
    store.create_calls_edge("foo", "bar", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="file_a.c", call_line=5,
    ))

    # RepairLog documenting the LLM repair
    repair_log = RepairLogNode(
        id="rl_001",
        caller_id="foo",
        callee_id="bar",
        call_location="file_a.c:5",
        repair_method="llm",
        llm_response="bar is called via function pointer",
        timestamp="2026-05-14T00:00:00Z",
        reasoning_summary="function pointer analysis",
    )
    store.create_repair_log(repair_log)

    # Verify RepairLog exists before invalidation
    logs_before = store.get_repair_logs(caller_id="foo", callee_id="bar")
    assert len(logs_before) == 1

    # Invalidate file_b.c (bar's file changed)
    updater = IncrementalUpdater(store)
    result = updater.invalidate_file("file_b.c")

    # RepairLog must be deleted (architecture.md §7: "删除该 CALLS 边 + 对应 RepairLog")
    logs_after = store.get_repair_logs(caller_id="foo", callee_id="bar")
    assert len(logs_after) == 0, (
        "architecture.md §7: RepairLog for invalidated LLM edge must be deleted"
    )

    # UnresolvedCall must be regenerated
    assert len(result.regenerated_unresolved_calls) == 1
    # foo must be in affected_callers
    assert "foo" in result.affected_callers


def test_invalidation_result_exposes_affected_source_ids():
    """architecture.md §7 step 5: the orchestrator needs to know which
    SourcePoints were affected by cascade invalidation so it can trigger
    re-repair. InvalidationResult must expose affected_source_ids."""
    from codemap_lite.graph.schema import SourcePointNode

    store = InMemoryGraphStore()

    # Two callers in different files, both with LLM edges to target in a.cpp
    store.create_function(FunctionNode(
        id="caller1", name="caller1", signature="void caller1()",
        file_path="src/b.cpp", start_line=1, end_line=5, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="caller2", name="caller2", signature="void caller2()",
        file_path="src/c.cpp", start_line=1, end_line=5, body_hash="h2",
    ))
    store.create_function(FunctionNode(
        id="target", name="target", signature="void target()",
        file_path="src/a.cpp", start_line=1, end_line=5, body_hash="ht",
    ))
    store.create_calls_edge("caller1", "target", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="src/b.cpp", call_line=3,
    ))
    store.create_calls_edge("caller2", "target", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="src/c.cpp", call_line=7,
    ))

    # SourcePoints for both callers
    store.create_source_point(SourcePointNode(
        id="caller1", function_id="caller1",
        entry_point_kind="callback_registration", reason="test", status="complete",
    ))
    store.create_source_point(SourcePointNode(
        id="caller2", function_id="caller2",
        entry_point_kind="entry_point", reason="test", status="complete",
    ))

    updater = IncrementalUpdater(store=store)
    result = updater.invalidate_file("src/a.cpp")

    # Both callers should be in affected_source_ids
    assert hasattr(result, "affected_source_ids"), (
        "InvalidationResult must expose affected_source_ids for orchestrator"
    )
    assert "caller1" in result.affected_source_ids
    assert "caller2" in result.affected_source_ids


def test_invalidate_file_resets_source_point_status_to_pending():
    """architecture.md §7 + §3: when cascade invalidation regenerates
    UnresolvedCalls for a source, the SourcePoint status must be reset
    to 'pending' so the repair orchestrator will re-process it."""
    from codemap_lite.graph.schema import SourcePointNode

    store = InMemoryGraphStore()

    # Source function in file_a.c (the caller)
    store.create_function(FunctionNode(
        id="src_caller", name="src_caller", signature="void src_caller()",
        file_path="file_a.c", start_line=1, end_line=10, body_hash="ha",
    ))
    # Target function in file_b.c (the callee, will be invalidated)
    store.create_function(FunctionNode(
        id="target_fn", name="target_fn", signature="void target_fn()",
        file_path="file_b.c", start_line=1, end_line=10, body_hash="hb",
    ))
    # LLM edge from src_caller → target_fn
    store.create_calls_edge("src_caller", "target_fn", CallsEdgeProps(
        resolved_by="llm", call_type="indirect",
        call_file="file_a.c", call_line=5,
    ))
    # SourcePoint for src_caller marked as "complete"
    store.create_source_point(SourcePointNode(
        id="src_caller",
        function_id="src_caller",
        entry_point_kind="callback_registration",
        reason="test",
        status="complete",
    ))

    # Verify initial state
    sp_before = store.get_source_point("src_caller")
    assert sp_before.status == "complete"

    # Invalidate file_b.c → target_fn deleted → LLM edge invalidated
    updater = IncrementalUpdater(store)
    result = updater.invalidate_file("file_b.c")

    # SourcePoint must be reset to "pending"
    sp_after = store.get_source_point("src_caller")
    assert sp_after is not None
    assert sp_after.status == "pending", (
        "architecture.md §7: SourcePoint status must reset to 'pending' "
        "when its reachable GAPs are invalidated"
    )
    # UnresolvedCall must be regenerated
    assert len(result.regenerated_unresolved_calls) == 1
    assert "src_caller" in result.affected_callers

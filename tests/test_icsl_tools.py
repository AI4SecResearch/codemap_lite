"""Tests for icsl_tools.py — the Agent-side CLI tool for graph operations."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

from codemap_lite.agent.icsl_tools import (
    query_reachable,
    write_edge,
    check_complete,
    query_function,
)


def test_query_reachable_returns_subgraph(mock_graph_store):
    result = query_reachable(source_id="src_001", store=mock_graph_store)
    assert "nodes" in result
    assert "edges" in result
    assert "unresolved" in result


def test_query_reachable_includes_unresolved_calls(mock_graph_store):
    result = query_reachable(source_id="src_001", store=mock_graph_store)
    assert len(result["unresolved"]) > 0
    gap = result["unresolved"][0]
    assert "caller_id" in gap
    assert "call_expression" in gap


def test_write_edge_creates_calls_and_repair_log(mock_graph_store):
    write_edge(
        caller_id="func_001",
        callee_id="func_002",
        call_type="indirect",
        call_file="test.cpp",
        call_line=42,
        store=mock_graph_store,
    )
    # Verify edge was created
    assert mock_graph_store.edges_created[-1] == {
        "caller_id": "func_001",
        "callee_id": "func_002",
        "call_type": "indirect",
        "call_file": "test.cpp",
        "call_line": 42,
        "resolved_by": "llm",
    }
    # Verify repair log was created (typed RepairLogNode now, not dict)
    assert len(mock_graph_store.repair_logs) == 1
    log = mock_graph_store.repair_logs[0]
    assert log.caller_id == "func_001"
    assert log.callee_id == "func_002"
    assert log.call_location == "test.cpp:42"
    assert log.repair_method == "llm"


def test_write_edge_skips_if_exists(mock_graph_store):
    mock_graph_store.existing_edges.add(("func_001", "func_002", "test.cpp", 42))
    result = write_edge(
        caller_id="func_001",
        callee_id="func_002",
        call_type="indirect",
        call_file="test.cpp",
        call_line=42,
        store=mock_graph_store,
    )
    assert result["skipped"] is True


def test_check_complete_returns_true_when_no_pending(mock_graph_store):
    mock_graph_store.pending_gaps = []
    result = check_complete(source_id="src_001", store=mock_graph_store)
    assert result["complete"] is True
    assert result["remaining_gaps"] == 0


def test_check_complete_returns_false_when_pending(mock_graph_store):
    mock_graph_store.pending_gaps = [{"id": "gap_001", "status": "pending"}]
    result = check_complete(source_id="src_001", store=mock_graph_store)
    assert result["complete"] is False
    assert result["remaining_gaps"] == 1


def test_check_complete_accepts_dataclass_pending_gaps():
    """Regression: Neo4jGraphStore + InMemoryGraphStore return
    ``list[UnresolvedCallNode]`` dataclasses, not list[dict]. Prior
    impl crashed with ``TypeError: 'UnresolvedCallNode' object is not
    subscriptable`` against production stores, silently flipping every
    gate check to False. architecture.md §3 门禁机制 requires
    ``check-complete`` to work uniformly across store shapes.
    """

    class _FakeGap:
        def __init__(self, gap_id: str) -> None:
            self.id = gap_id

    class _DataclassStore:
        def get_pending_gaps_for_source(self, source_id):
            return [_FakeGap("gap_001"), _FakeGap("gap_002")]

    result = check_complete(source_id="src_001", store=_DataclassStore())
    assert result["complete"] is False
    assert result["remaining_gaps"] == 2
    assert result["pending_gap_ids"] == ["gap_001", "gap_002"]


# --- Fixtures ---

import pytest


class MockGraphStoreForTools:
    """Mock graph store for testing icsl_tools functions."""

    def __init__(self):
        self.edges_created = []
        self.repair_logs = []
        self.existing_edges = set()
        self.pending_gaps = [
            {
                "id": "gap_001",
                "caller_id": "func_001",
                "call_expression": "ptr->method()",
                "call_file": "test.cpp",
                "call_line": 10,
                "call_type": "indirect",
                "var_name": "ptr",
                "var_type": "Base*",
                "candidates": ["Derived::method"],
                "source_code_snippet": "ptr->method();",
                "status": "pending",
            }
        ]
        self.reachable_nodes = [
            {"id": "func_001", "name": "caller_func", "signature": "void caller_func()", "file_path": "test.cpp"},
            {"id": "func_002", "name": "callee_func", "signature": "void callee_func()", "file_path": "test.cpp"},
        ]
        self.reachable_edges = [
            {"source": "func_001", "target": "func_002", "resolved_by": "symbol_table", "call_type": "direct"},
        ]

    def get_reachable_subgraph(self, source_id, max_depth=50):
        return {
            "nodes": self.reachable_nodes,
            "edges": self.reachable_edges,
            "unresolved": self.pending_gaps,
        }

    def edge_exists(self, caller_id, callee_id, call_file, call_line):
        return (caller_id, callee_id, call_file, call_line) in self.existing_edges

    def create_calls_edge(self, caller_id, callee_id, props):
        # Accept both CallsEdgeProps dataclass and dict (backwards compat)
        if hasattr(props, "call_type"):
            self.edges_created.append({
                "caller_id": caller_id,
                "callee_id": callee_id,
                "call_type": props.call_type,
                "call_file": props.call_file,
                "call_line": props.call_line,
                "resolved_by": props.resolved_by,
            })
        else:
            self.edges_created.append({
                "caller_id": caller_id,
                "callee_id": callee_id,
                "call_type": props.get("call_type", ""),
                "call_file": props.get("call_file", ""),
                "call_line": props.get("call_line", 0),
                "resolved_by": props.get("resolved_by", "llm"),
            })

    def create_repair_log(self, log_data):
        self.repair_logs.append(log_data)

    def delete_unresolved_call(self, caller_id, call_file, call_line):
        self.pending_gaps = [
            g for g in self.pending_gaps
            if not (g["caller_id"] == caller_id and g["call_file"] == call_file and g["call_line"] == call_line)
        ]

    def get_pending_gaps_for_source(self, source_id):
        return [g for g in self.pending_gaps if g["status"] == "pending"]

    def list_functions(self, file_path=None):
        """Return mock function nodes for query-function testing."""
        from types import SimpleNamespace
        fns = [
            SimpleNamespace(
                id="func_001", name="caller_func",
                signature="void caller_func()", file_path="test.cpp",
                start_line=1, end_line=10,
            ),
            SimpleNamespace(
                id="func_002", name="callee_func",
                signature="void callee_func()", file_path="test.cpp",
                start_line=12, end_line=20,
            ),
            SimpleNamespace(
                id="func_003", name="Clear",
                signature="void DataBuffer::Clear()", file_path="data_buffer.h",
                start_line=30, end_line=35,
            ),
        ]
        if file_path is not None:
            return [f for f in fns if f.file_path == file_path]
        return fns


@pytest.fixture
def mock_graph_store():
    return MockGraphStoreForTools()


# --- CLI subprocess tests (closes Known gap #1) ---


def _write_config(tmp_path: Path) -> Path:
    """Create a minimal .icslpreprocess/config.yaml so _load_store succeeds."""
    icsl_dir = tmp_path / ".icslpreprocess"
    icsl_dir.mkdir(parents=True, exist_ok=True)
    config = icsl_dir / "config.yaml"
    config.write_text(
        'neo4j:\n  uri: "bolt://localhost:7687"\n  user: "neo4j"\n  password: "x"\n',
        encoding="utf-8",
    )
    return config


def test_cli_query_reachable_emits_json(tmp_path, monkeypatch, capsys):
    from codemap_lite.agent import icsl_tools

    config = _write_config(tmp_path)
    store = MockGraphStoreForTools()
    monkeypatch.setattr(icsl_tools, "_load_store", lambda _p: store)

    exit_code = icsl_tools.main(
        ["--config", str(config), "query-reachable", "--source", "src_001"]
    )
    assert exit_code == 0

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert set(payload.keys()) == {"nodes", "edges", "unresolved"}
    assert payload["unresolved"][0]["caller_id"] == "func_001"


def test_cli_write_edge_creates_calls_edge(tmp_path, monkeypatch, capsys):
    from codemap_lite.agent import icsl_tools

    config = _write_config(tmp_path)
    store = MockGraphStoreForTools()
    monkeypatch.setattr(icsl_tools, "_load_store", lambda _p: store)

    exit_code = icsl_tools.main(
        [
            "--config",
            str(config),
            "write-edge",
            "--caller",
            "func_001",
            "--callee",
            "func_002",
            "--call-type",
            "indirect",
            "--call-file",
            "test.cpp",
            "--call-line",
            "42",
        ]
    )
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"skipped": False, "edge_created": True}
    assert store.edges_created[-1]["resolved_by"] == "llm"
    assert store.edges_created[-1]["call_line"] == 42
    # Backwards-compat guard: omitting the new --llm-response /
    # --reasoning-summary flags must still produce a RepairLogNode (with
    # empty reasoning fields) so the agent prompt can be rolled out
    # incrementally without breaking existing callers.
    assert len(store.repair_logs) == 1
    log = store.repair_logs[-1]
    assert log.llm_response == ""
    assert log.reasoning_summary == ""


def test_cli_write_edge_forwards_llm_response_and_reasoning(
    tmp_path, monkeypatch, capsys
):
    """--llm-response / --reasoning-summary must land on RepairLogNode.

    This closes the drift between architecture.md §4 RepairLogNode schema
    (llm_response + reasoning_summary) and what the agent CLI actually
    emits — without these flags forwarding through, every llm-repaired
    edge writes an empty reasoning chain and the CallGraphView
    EdgeLlmInspector (architecture.md §5) is structurally starved.
    """
    from codemap_lite.agent import icsl_tools

    config = _write_config(tmp_path)
    store = MockGraphStoreForTools()
    monkeypatch.setattr(icsl_tools, "_load_store", lambda _p: store)

    exit_code = icsl_tools.main(
        [
            "--config",
            str(config),
            "write-edge",
            "--caller",
            "func_001",
            "--callee",
            "func_002",
            "--call-type",
            "indirect",
            "--call-file",
            "test.cpp",
            "--call-line",
            "42",
            "--llm-response",
            "ptr is assigned a DerivedHandler at line 24, so ptr->handle() dispatches to DerivedHandler::handle.",
            "--reasoning-summary",
            "picked DerivedHandler::handle because the ctor at test.cpp:24 binds ptr to DerivedHandler",
        ]
    )
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"skipped": False, "edge_created": True}
    assert len(store.repair_logs) == 1
    log = store.repair_logs[-1]
    assert log.caller_id == "func_001"
    assert log.callee_id == "func_002"
    assert log.call_location == "test.cpp:42"
    assert log.repair_method == "llm"
    assert log.llm_response.startswith("ptr is assigned a DerivedHandler")
    assert log.reasoning_summary.startswith(
        "picked DerivedHandler::handle"
    )


def test_cli_check_complete_returns_status(tmp_path, monkeypatch, capsys):
    from codemap_lite.agent import icsl_tools

    config = _write_config(tmp_path)
    store = MockGraphStoreForTools()
    store.pending_gaps = []
    monkeypatch.setattr(icsl_tools, "_load_store", lambda _p: store)

    exit_code = icsl_tools.main(
        ["--config", str(config), "check-complete", "--source", "src_001"]
    )
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"complete": True, "remaining_gaps": 0, "pending_gap_ids": []}


def test_cli_missing_config_yields_structured_error(tmp_path, capsys):
    from codemap_lite.agent import icsl_tools

    missing = tmp_path / "does_not_exist.yaml"
    exit_code = icsl_tools.main(
        ["--config", str(missing), "check-complete", "--source", "src_001"]
    )
    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"] == "config_not_found"


def test_cli_parses_config_yaml(tmp_path):
    from codemap_lite.agent import icsl_tools

    path = tmp_path / "config.yaml"
    path.write_text(
        'neo4j:\n  uri: "bolt://example:7687"\n  user: "agent"\n  password: "s3cret"\n',
        encoding="utf-8",
    )
    parsed = icsl_tools._parse_config(path)
    assert parsed["neo4j"] == {
        "uri": "bolt://example:7687",
        "user": "agent",
        "password": "s3cret",
    }


def test_cli_subprocess_end_to_end(tmp_path):
    """Launch the injected icsl_tools.py as a real subprocess (mirrors agent flow)."""
    from codemap_lite.analysis.repair_orchestrator import (
        RepairConfig,
        RepairOrchestrator,
    )

    target_dir = tmp_path / "target"
    target_dir.mkdir()
    orchestrator = RepairOrchestrator(
        RepairConfig(
            target_dir=target_dir,
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password="pw",
        )
    )
    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="src_001",
        counter_examples="",
    )

    injected = target_dir / ".icslpreprocess_src_001" / "icsl_tools.py"
    result = subprocess.run(
        [sys.executable, str(injected), "--help"],
        capture_output=True,
        text=True,
        cwd=str(target_dir),
    )
    assert result.returncode == 0
    assert "query-reachable" in result.stdout
    assert "write-edge" in result.stdout
    assert "check-complete" in result.stdout


# --- Integration test with real InMemoryGraphStore ---


def test_write_edge_full_lifecycle_with_real_store():
    """architecture.md §3 修复成功时: write-edge must create CALLS edge,
    create RepairLog, and delete the UnresolvedCall — all in one atomic
    operation. Uses real InMemoryGraphStore (not mock)."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import (
        FunctionNode,
        UnresolvedCallNode,
        CallsEdgeProps,
    )

    store = InMemoryGraphStore()

    # Setup: two functions and an unresolved call
    store.create_function(FunctionNode(
        id="caller_a", name="caller", signature="void caller()",
        file_path="src/a.cpp", start_line=1, end_line=10, body_hash="ha",
    ))
    store.create_function(FunctionNode(
        id="callee_b", name="target", signature="void target()",
        file_path="src/b.cpp", start_line=1, end_line=5, body_hash="hb",
    ))
    gap = UnresolvedCallNode(
        caller_id="caller_a",
        call_expression="fn_ptr(x)",
        call_file="src/a.cpp",
        call_line=5,
        call_type="indirect",
        source_code_snippet="fn_ptr(x);",
        var_name="fn_ptr",
        var_type="void (*)(int)",
    )
    store.create_unresolved_call(gap)

    # Act: write-edge resolves the GAP
    result = write_edge(
        caller_id="caller_a",
        callee_id="callee_b",
        call_type="indirect",
        call_file="src/a.cpp",
        call_line=5,
        store=store,
        llm_response="fn_ptr is assigned DerivedHandler::handle at line 3",
        reasoning_summary="ptr->handle() dispatches to DerivedHandler::handle",
    )

    assert result["skipped"] is False
    assert result["edge_created"] is True

    # Verify: CALLS edge exists
    edges = store.list_calls_edges()
    llm_edges = [e for e in edges if e.props.resolved_by == "llm"]
    assert len(llm_edges) == 1
    assert llm_edges[0].caller_id == "caller_a"
    assert llm_edges[0].callee_id == "callee_b"

    # Verify: RepairLog created with correct fields
    logs = store.get_repair_logs()
    assert len(logs) == 1
    assert logs[0].caller_id == "caller_a"
    assert logs[0].callee_id == "callee_b"
    assert logs[0].call_location == "src/a.cpp:5"
    assert logs[0].repair_method == "llm"
    assert "DerivedHandler" in logs[0].llm_response
    assert "DerivedHandler" in logs[0].reasoning_summary

    # Verify: UnresolvedCall deleted
    remaining = store.get_unresolved_calls(caller_id="caller_a")
    assert len(remaining) == 0

    # Verify: check-complete now returns True
    complete_result = check_complete(source_id="caller_a", store=store)
    assert complete_result["complete"] is True


def test_write_edge_repair_log_timestamp_is_iso8601_utc():
    """architecture.md §4 RepairLog + §3 line 116: timestamp must be
    ISO-8601 UTC string. Verify format and timezone offset."""
    import re

    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="f1", name="a", signature="void a()",
        file_path="x.cpp", start_line=1, end_line=5, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="f2", name="b", signature="void b()",
        file_path="x.cpp", start_line=6, end_line=10, body_hash="h2",
    ))
    store.create_unresolved_call(UnresolvedCallNode(
        caller_id="f1", call_expression="p()", call_file="x.cpp",
        call_line=3, call_type="indirect",
        source_code_snippet="p();", var_name="p", var_type="void(*)()",
    ))

    write_edge(
        caller_id="f1", callee_id="f2", call_type="indirect",
        call_file="x.cpp", call_line=3, store=store,
    )

    logs = store.get_repair_logs()
    assert len(logs) == 1
    ts = logs[0].timestamp
    # Must be ISO-8601 with UTC offset (+00:00 or Z)
    iso_pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    assert re.match(iso_pattern, ts), f"timestamp not ISO-8601: {ts}"
    assert "+00:00" in ts or ts.endswith("Z"), f"timestamp not UTC: {ts}"


def test_write_edge_repair_log_timestamps_are_monotonic():
    """architecture.md §4: multiple RepairLogs must have distinct,
    monotonically increasing timestamps for chronological ordering."""
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    store = InMemoryGraphStore()
    for i in range(3):
        store.create_function(FunctionNode(
            id=f"caller_{i}", name=f"c{i}", signature=f"void c{i}()",
            file_path="x.cpp", start_line=i * 10, end_line=i * 10 + 5,
            body_hash=f"h{i}",
        ))
    store.create_function(FunctionNode(
        id="target", name="t", signature="void t()",
        file_path="y.cpp", start_line=1, end_line=5, body_hash="ht",
    ))
    for i in range(3):
        store.create_unresolved_call(UnresolvedCallNode(
            caller_id=f"caller_{i}", call_expression="t()",
            call_file="x.cpp", call_line=i * 10 + 2, call_type="indirect",
            source_code_snippet="t();", var_name="t", var_type="void(*)()",
        ))

    import time
    for i in range(3):
        write_edge(
            caller_id=f"caller_{i}", callee_id="target",
            call_type="indirect", call_file="x.cpp",
            call_line=i * 10 + 2, store=store,
        )
        time.sleep(0.01)  # ensure distinct timestamps

    logs = store.get_repair_logs()
    assert len(logs) == 3
    timestamps = [log.timestamp for log in logs]
    assert timestamps == sorted(timestamps), "timestamps not monotonic"
    assert len(set(timestamps)) == 3, "timestamps not distinct"


def test_write_edge_repair_log_call_location_matches_edge():
    """Regression: RepairLog.call_location must exactly match the CALLS edge's
    (call_file, call_line) so Neo4j queries can join them.

    Known gap (2026-05-13): 35 LLM edges vs 34 RepairLogs in 7-entry run.
    Root cause: path normalization differences between call_file in CALLS edge
    and call_location in RepairLog. This test ensures they stay consistent.
    """
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode

    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="f1", name="a", signature="void a()",
        file_path="src/module/foo.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_function(FunctionNode(
        id="f2", name="b", signature="void b()",
        file_path="src/module/bar.cpp", start_line=1, end_line=5, body_hash="h2",
    ))
    store.create_unresolved_call(UnresolvedCallNode(
        caller_id="f1", call_expression="b()",
        call_file="src/module/foo.cpp", call_line=5,
        call_type="indirect", source_code_snippet="b();",
        var_name="b", var_type="void(*)()",
    ))

    # Write edge with the same call_file format
    result = write_edge(
        caller_id="f1", callee_id="f2",
        call_type="indirect",
        call_file="src/module/foo.cpp",
        call_line=5,
        store=store,
    )
    assert result["edge_created"] is True

    # Verify: RepairLog.call_location == "{call_file}:{call_line}" from CALLS edge
    edges = store.list_calls_edges()
    llm_edges = [e for e in edges if e.props.resolved_by == "llm"]
    assert len(llm_edges) == 1

    logs = store.get_repair_logs()
    assert len(logs) == 1

    edge = llm_edges[0]
    log = logs[0]
    expected_location = f"{edge.props.call_file}:{edge.props.call_line}"
    assert log.call_location == expected_location, (
        f"RepairLog.call_location ({log.call_location!r}) != "
        f"CALLS edge location ({expected_location!r})"
    )


def test_write_edge_rejects_invalid_call_type(mock_graph_store):
    """architecture.md §4: call_type ∈ {direct, indirect, virtual}.

    write_edge must reject values outside this enum.
    """
    import pytest

    with pytest.raises(ValueError, match="call_type"):
        write_edge(
            caller_id="f1",
            callee_id="f2",
            call_type="unknown",
            call_file="a.cpp",
            call_line=10,
            store=mock_graph_store,
        )


def test_write_edge_accepts_valid_call_types(mock_graph_store):
    """architecture.md §4: all three valid call_type values must be accepted."""
    for ct in ("direct", "indirect", "virtual"):
        result = write_edge(
            caller_id="f1",
            callee_id="f2",
            call_type=ct,
            call_file="a.cpp",
            call_line=10 + hash(ct) % 100,
            store=mock_graph_store,
        )
        assert result["skipped"] is False or result.get("edge_created") is True


def test_write_edge_rejects_nonexistent_caller(mock_graph_store):
    """architecture.md §3: write-edge must validate caller_id is a valid Function node.

    If the caller doesn't exist in the graph, the edge should not be created
    and an error should be returned.
    """
    # Add get_function_by_id to mock — caller "nonexistent" not found
    mock_graph_store.get_function_by_id = lambda fid: (
        None if fid == "nonexistent" else MagicMock()
    )

    result = write_edge(
        caller_id="nonexistent",
        callee_id="func_002",
        call_type="indirect",
        call_file="test.cpp",
        call_line=42,
        store=mock_graph_store,
    )
    assert result.get("error") is not None
    assert "caller" in result["error"].lower()
    assert len(mock_graph_store.edges_created) == 0


def test_write_edge_rejects_nonexistent_callee(mock_graph_store):
    """architecture.md §3: write-edge must validate callee_id is a valid Function node.

    If the callee doesn't exist in the graph, the edge should not be created
    and an error should be returned.
    """
    # Add get_function_by_id to mock — callee "nonexistent" not found
    mock_graph_store.get_function_by_id = lambda fid: (
        None if fid == "nonexistent" else MagicMock()
    )

    result = write_edge(
        caller_id="func_001",
        callee_id="nonexistent",
        call_type="indirect",
        call_file="test.cpp",
        call_line=42,
        store=mock_graph_store,
    )
    assert result.get("error") is not None
    assert "callee" in result["error"].lower()
    assert len(mock_graph_store.edges_created) == 0


def test_write_edge_succeeds_with_valid_caller_and_callee(mock_graph_store):
    """architecture.md §3: write-edge succeeds when both caller and callee exist."""
    # Add get_function_by_id to mock — both exist
    mock_graph_store.get_function_by_id = lambda fid: MagicMock()

    result = write_edge(
        caller_id="func_001",
        callee_id="func_002",
        call_type="indirect",
        call_file="test.cpp",
        call_line=99,
        store=mock_graph_store,
    )
    assert result.get("edge_created") is True
    assert len(mock_graph_store.edges_created) == 1


def test_write_edge_truncates_long_reasoning_summary(mock_graph_store):
    """architecture.md §3 line 125: reasoning_summary ≤ 200 chars.

    write-edge must truncate reasoning_summary that exceeds 200 characters
    rather than rejecting the call (the agent may produce verbose output).
    """
    mock_graph_store.get_function_by_id = lambda fid: MagicMock()
    long_summary = "x" * 300

    result = write_edge(
        caller_id="func_001",
        callee_id="func_002",
        call_type="indirect",
        call_file="test.cpp",
        call_line=200,
        store=mock_graph_store,
        reasoning_summary=long_summary,
    )
    assert result.get("edge_created") is True
    # The RepairLog should have a truncated reasoning_summary
    assert len(mock_graph_store.repair_logs) == 1
    log = mock_graph_store.repair_logs[0]
    stored_summary = log.reasoning_summary if hasattr(log, "reasoning_summary") else log["reasoning_summary"]
    assert len(stored_summary) <= 200
    assert stored_summary.endswith("…") or len(stored_summary) <= 200


# --- query-function tests ---


def test_query_function_by_name(mock_graph_store):
    """query_function finds functions by name substring."""
    result = query_function(name="Clear", store=mock_graph_store)
    assert result["count"] == 1
    assert result["matches"][0]["id"] == "func_003"
    assert result["matches"][0]["name"] == "Clear"


def test_query_function_by_file(mock_graph_store):
    """query_function filters by file path."""
    result = query_function(file_path="data_buffer.h", store=mock_graph_store)
    assert result["count"] == 1
    assert result["matches"][0]["name"] == "Clear"


def test_query_function_by_name_and_file(mock_graph_store):
    """query_function combines name + file filters."""
    result = query_function(name="caller", file_path="test.cpp", store=mock_graph_store)
    assert result["count"] == 1
    assert result["matches"][0]["id"] == "func_001"


def test_query_function_no_match(mock_graph_store):
    """query_function returns empty when no match."""
    result = query_function(name="nonexistent", store=mock_graph_store)
    assert result["count"] == 0
    assert result["matches"] == []


def test_query_function_no_params(mock_graph_store):
    """query_function requires at least one filter."""
    result = query_function(store=mock_graph_store)
    assert "error" in result


def test_gate_complete_true_subprocess(tmp_path):
    """architecture.md §3 门禁机制: exercise the REAL subprocess path
    for check-complete returning complete=True.

    This closes the Known gap "门禁机制尚未真 pass 过" by proving the
    full subprocess flow (spawn → JSON parse → complete=True) works
    when all GAPs for a source are resolved.
    """
    from codemap_lite.analysis.repair_orchestrator import (
        RepairConfig,
        RepairOrchestrator,
    )
    from codemap_lite.graph.neo4j_store import InMemoryGraphStore
    from codemap_lite.graph.schema import FunctionNode, SourcePointNode

    # Setup: a store with a source point but NO pending gaps
    store = InMemoryGraphStore()
    store.create_function(FunctionNode(
        id="func_src", name="entry", signature="void entry()",
        file_path="src/main.cpp", start_line=1, end_line=10, body_hash="h1",
    ))
    store.create_source_point(SourcePointNode(
        id="func_src", function_id="func_src",
        entry_point_kind="api_entry", reason="test", module="test",
        status="pending",
    ))
    # No unresolved calls → gate should pass

    target_dir = tmp_path / "target"
    target_dir.mkdir()

    orchestrator = RepairOrchestrator(
        RepairConfig(
            target_dir=target_dir,
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password="pw",
            graph_store=store,
        )
    )
    # Inject the icsl_tools.py into the working directory
    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="func_src",
        counter_examples="",
    )

    # Run check-complete as a real subprocess (same as _check_gate does)
    injected = target_dir / ".icslpreprocess_func_src" / "icsl_tools.py"
    result = subprocess.run(
        [sys.executable, str(injected), "check-complete", "--source", "func_src"],
        capture_output=True,
        text=True,
        cwd=str(target_dir),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["complete"] is True
    assert payload["remaining_gaps"] == 0
    assert payload["pending_gap_ids"] == []


def test_gate_subprocess_json_contract(tmp_path):
    """architecture.md §3 门禁机制: verify the subprocess JSON contract.

    When Neo4j is unreachable (test environment), check-complete still
    returns valid JSON with the expected schema. This proves the CLI
    plumbing (argparse → function → JSON stdout) works correctly.
    The actual gate logic (pending gaps from Neo4j) is tested via
    in-process tests (test_check_complete_returns_true_when_no_pending,
    test_write_edge_full_lifecycle_with_real_store).
    """
    from codemap_lite.analysis.repair_orchestrator import (
        RepairConfig,
        RepairOrchestrator,
    )

    target_dir = tmp_path / "target"
    target_dir.mkdir()

    orchestrator = RepairOrchestrator(
        RepairConfig(
            target_dir=target_dir,
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password="pw",
        )
    )
    orchestrator._inject_files(
        target_dir=target_dir,
        source_id="gate_test",
        counter_examples="",
    )

    injected = target_dir / ".icslpreprocess_gate_test" / "icsl_tools.py"
    result = subprocess.run(
        [sys.executable, str(injected), "check-complete", "--source", "gate_test"],
        capture_output=True,
        text=True,
        cwd=str(target_dir),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    payload = json.loads(result.stdout)
    # JSON contract: must have these three fields
    assert "complete" in payload
    assert "remaining_gaps" in payload
    assert "pending_gap_ids" in payload
    assert isinstance(payload["complete"], bool)
    assert isinstance(payload["remaining_gaps"], int)
    assert isinstance(payload["pending_gap_ids"], list)

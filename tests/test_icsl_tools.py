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
    # Verify repair log was created
    assert len(mock_graph_store.repair_logs) == 1
    assert mock_graph_store.repair_logs[0]["caller_id"] == "func_001"


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

    injected = target_dir / ".icslpreprocess" / "icsl_tools.py"
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

"""icsl_tools + prompt_builder contract tests — architecture.md §3.

Tests the agent-side CLI tool protocol (write-edge atomicity, check-complete
schema, query-reachable BFS) and prompt_builder (counter-example path,
source-scoped rendering). Uses InMemoryGraphStore.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from codemap_lite.agent.icsl_tools import (
    check_complete,
    query_reachable,
    write_edge,
    _gap_id,
    _parse_config,
)
from codemap_lite.analysis.prompt_builder import build_repair_prompt
from codemap_lite.analysis.repair_orchestrator import _safe_dirname
from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    RepairLogNode,
    SourcePointNode,
    UnresolvedCallNode,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store_with_gap():
    """Store with a caller, callee, SourcePoint, and one pending UC."""
    store = InMemoryGraphStore()
    caller = FunctionNode(
        id="fn_caller", name="dispatch", signature="void dispatch()",
        file_path="src/dispatch.cpp", start_line=10, end_line=20, body_hash="aaa",
    )
    callee = FunctionNode(
        id="fn_callee", name="handler", signature="void handler()",
        file_path="src/handler.cpp", start_line=5, end_line=15, body_hash="bbb",
    )
    store.create_function(caller)
    store.create_function(callee)
    # SourcePoint for caller
    sp = SourcePointNode(
        id="fn_caller", function_id="fn_caller",
        entry_point_kind="api", reason="entry", status="pending",
    )
    store.create_source_point(sp)
    # Pending UC
    uc = UnresolvedCallNode(
        id="uc_001", caller_id="fn_caller",
        call_expression="handler()", call_file="src/dispatch.cpp",
        call_line=15, call_type="indirect", source_code_snippet="fp();",
        var_name="fp", var_type="FuncPtr",
    )
    store.create_unresolved_call(uc)
    return store


# ---------------------------------------------------------------------------
# Test: write_edge atomicity
# ---------------------------------------------------------------------------

class TestWriteEdge:
    """architecture.md §3: write-edge creates edge + RepairLog + deletes UC atomically."""

    def test_creates_calls_edge(self, store_with_gap):
        result = write_edge(
            caller_id="fn_caller", callee_id="fn_callee",
            call_type="indirect", call_file="src/dispatch.cpp", call_line=15,
            store=store_with_gap,
            llm_response="resolved via vtable analysis",
            reasoning_summary="vtable dispatch pattern",
        )
        assert result["edge_created"] is True
        assert result["skipped"] is False

        # Edge exists
        edge = store_with_gap.get_calls_edge(
            "fn_caller", "fn_callee", "src/dispatch.cpp", 15
        )
        assert edge is not None
        assert edge.resolved_by == "llm"
        assert edge.call_type == "indirect"

    def test_creates_repair_log(self, store_with_gap):
        write_edge(
            caller_id="fn_caller", callee_id="fn_callee",
            call_type="indirect", call_file="src/dispatch.cpp", call_line=15,
            store=store_with_gap,
            llm_response="analysis text",
            reasoning_summary="matched signature",
        )
        logs = store_with_gap.get_repair_logs()
        assert len(logs) >= 1
        log = logs[0]
        assert log.caller_id == "fn_caller"
        assert log.callee_id == "fn_callee"
        assert log.call_location == "src/dispatch.cpp:15"
        assert log.reasoning_summary == "matched signature"
        assert log.llm_response == "analysis text"

    def test_deletes_unresolved_call(self, store_with_gap):
        write_edge(
            caller_id="fn_caller", callee_id="fn_callee",
            call_type="indirect", call_file="src/dispatch.cpp", call_line=15,
            store=store_with_gap,
        )
        ucs = store_with_gap.get_unresolved_calls(caller_id="fn_caller")
        matching = [u for u in ucs if u.call_line == 15]
        assert len(matching) == 0

    def test_skips_duplicate_edge(self, store_with_gap):
        write_edge(
            caller_id="fn_caller", callee_id="fn_callee",
            call_type="indirect", call_file="src/dispatch.cpp", call_line=15,
            store=store_with_gap,
        )
        # Second write should skip
        result = write_edge(
            caller_id="fn_caller", callee_id="fn_callee",
            call_type="indirect", call_file="src/dispatch.cpp", call_line=15,
            store=store_with_gap,
        )
        assert result["skipped"] is True

    def test_invalid_call_type_raises(self, store_with_gap):
        with pytest.raises(ValueError, match="call_type"):
            write_edge(
                caller_id="fn_caller", callee_id="fn_callee",
                call_type="magic", call_file="a.cpp", call_line=1,
                store=store_with_gap,
            )

    def test_nonexistent_caller_returns_error(self, store_with_gap):
        result = write_edge(
            caller_id="nonexistent", callee_id="fn_callee",
            call_type="indirect", call_file="a.cpp", call_line=1,
            store=store_with_gap,
        )
        assert "error" in result
        assert "Caller" in result["error"]

    def test_nonexistent_callee_returns_error(self, store_with_gap):
        result = write_edge(
            caller_id="fn_caller", callee_id="nonexistent",
            call_type="indirect", call_file="a.cpp", call_line=1,
            store=store_with_gap,
        )
        assert "error" in result
        assert "Callee" in result["error"]

    def test_reasoning_summary_truncated_at_200(self, store_with_gap):
        """architecture.md §3: reasoning_summary ≤ 200 chars."""
        long_reason = "x" * 300
        write_edge(
            caller_id="fn_caller", callee_id="fn_callee",
            call_type="indirect", call_file="src/dispatch.cpp", call_line=15,
            store=store_with_gap,
            reasoning_summary=long_reason,
        )
        logs = store_with_gap.get_repair_logs()
        assert len(logs[0].reasoning_summary) == 200


# ---------------------------------------------------------------------------
# Test: check_complete schema
# ---------------------------------------------------------------------------

class TestCheckComplete:
    """architecture.md §3: check-complete returns {complete, remaining_gaps, pending_gap_ids}."""

    def test_incomplete_with_pending_gaps(self, store_with_gap):
        result = check_complete("fn_caller", store_with_gap)
        assert result["complete"] is False
        assert result["remaining_gaps"] >= 1
        assert "uc_001" in result["pending_gap_ids"]

    def test_complete_after_resolving_all_gaps(self, store_with_gap):
        # Resolve the gap by writing an edge
        write_edge(
            caller_id="fn_caller", callee_id="fn_callee",
            call_type="indirect", call_file="src/dispatch.cpp", call_line=15,
            store=store_with_gap,
        )
        result = check_complete("fn_caller", store_with_gap)
        assert result["complete"] is True
        assert result["remaining_gaps"] == 0
        assert result["pending_gap_ids"] == []

    def test_schema_fields_present(self, store_with_gap):
        result = check_complete("fn_caller", store_with_gap)
        assert "complete" in result
        assert "remaining_gaps" in result
        assert "pending_gap_ids" in result
        assert isinstance(result["complete"], bool)
        assert isinstance(result["remaining_gaps"], int)
        assert isinstance(result["pending_gap_ids"], list)


# ---------------------------------------------------------------------------
# Test: _gap_id helper
# ---------------------------------------------------------------------------

class TestGapId:
    """_gap_id must handle both dict and dataclass forms."""

    def test_dict_form(self):
        assert _gap_id({"id": "gap_123"}) == "gap_123"

    def test_dataclass_form(self):
        uc = UnresolvedCallNode(
            id="uc_456", caller_id="c", call_expression="x()",
            call_file="a.cpp", call_line=1, call_type="indirect",
            source_code_snippet="", var_name=None, var_type=None,
        )
        assert _gap_id(uc) == "uc_456"


# ---------------------------------------------------------------------------
# Test: query_reachable
# ---------------------------------------------------------------------------

class TestQueryReachable:
    """architecture.md §3: query-reachable returns subgraph with nodes/edges/unresolved."""

    def test_returns_subgraph_structure(self, store_with_gap):
        result = query_reachable("fn_caller", store_with_gap)
        assert "nodes" in result
        assert "edges" in result
        assert "unresolved" in result

    def test_includes_caller_in_nodes(self, store_with_gap):
        result = query_reachable("fn_caller", store_with_gap)
        node_ids = [n.id for n in result["nodes"]]
        assert "fn_caller" in node_ids

    def test_includes_pending_ucs(self, store_with_gap):
        result = query_reachable("fn_caller", store_with_gap)
        assert len(result["unresolved"]) >= 1


# ---------------------------------------------------------------------------
# Test: _parse_config
# ---------------------------------------------------------------------------

class TestParseConfig:
    """Config parser for agent-side YAML (minimal, no PyYAML dependency)."""

    def test_parses_neo4j_section(self, tmp_path: Path):
        config = tmp_path / "config.yaml"
        config.write_text(
            "neo4j:\n  uri: bolt://host:7687\n  user: neo4j\n  password: secret\n",
            encoding="utf-8",
        )
        result = _parse_config(config)
        assert result["neo4j"]["uri"] == "bolt://host:7687"
        assert result["neo4j"]["user"] == "neo4j"
        assert result["neo4j"]["password"] == "secret"

    def test_strips_quotes(self, tmp_path: Path):
        config = tmp_path / "config.yaml"
        config.write_text(
            'neo4j:\n  password: "my pass"\n', encoding="utf-8"
        )
        result = _parse_config(config)
        assert result["neo4j"]["password"] == "my pass"


# ---------------------------------------------------------------------------
# Test: build_repair_prompt
# ---------------------------------------------------------------------------

class TestBuildRepairPrompt:
    """architecture.md §3: prompt references icsl_tools commands."""

    def test_contains_source_id(self):
        prompt = build_repair_prompt("my_source_001")
        assert "my_source_001" in prompt

    def test_contains_query_reachable_command(self):
        prompt = build_repair_prompt("src_001")
        assert "query-reachable" in prompt
        assert "--source src_001" in prompt

    def test_contains_write_edge_command(self):
        prompt = build_repair_prompt("src_001")
        assert "write-edge" in prompt
        assert "--caller" in prompt
        assert "--callee" in prompt
        assert "--call-type" in prompt
        assert "--call-file" in prompt
        assert "--call-line" in prompt

    def test_contains_counter_examples_reference(self):
        """Prompt must tell agent to check counter_examples.md."""
        prompt = build_repair_prompt("src_001")
        assert "counter_examples.md" in prompt

    def test_contains_reasoning_summary_instruction(self):
        """architecture.md §3: agent must pass --reasoning-summary."""
        prompt = build_repair_prompt("src_001")
        assert "--reasoning-summary" in prompt

    def test_contains_llm_response_instruction(self):
        """architecture.md §3: agent must pass --llm-response."""
        prompt = build_repair_prompt("src_001")
        assert "--llm-response" in prompt

    def test_icsl_dir_uses_safe_dirname(self):
        """Prompt uses _safe_dirname for the .icslpreprocess path."""
        source_id = "path/to/file.h::NS::Method"
        prompt = build_repair_prompt(source_id)
        safe_id = _safe_dirname(source_id)
        assert f".icslpreprocess_{safe_id}" in prompt

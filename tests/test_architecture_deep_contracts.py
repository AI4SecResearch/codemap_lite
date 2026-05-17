"""Deep contract tests targeting architecture.md gaps found by audit.

Covers:
- §3 FeedbackStore source-scoped rendering + markdown format
- §3 icsl_tools write-edge atomic lifecycle (edge + RepairLog + UC deletion)
- §3 check-complete JSON schema contract
- §7 Incremental 5-step cascade with SourcePoint reset
- §8 /stats unresolved_by_category prefix parsing
- §3 Prompt builder counter-example injection path
- §3 Repair orchestrator error stamping categories

Run: pytest tests/test_architecture_deep_contracts.py -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


# ===========================================================================
# §3 FeedbackStore — source-scoped rendering + markdown format
# ===========================================================================


class TestFeedbackStoreSourceScoped:
    """architecture.md §3: FeedbackStore renders source-scoped counter-examples."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.analysis.feedback_store import CounterExample, FeedbackStore

        self._tmpdir = Path(tempfile.mkdtemp())
        self.store = FeedbackStore(storage_dir=self._tmpdir)
        # Add examples for two different sources
        self.store.add(CounterExample(
            call_context="/test/a.cpp:10",
            wrong_target="fn_wrong_1",
            correct_target="fn_correct_1",
            pattern="vtable dispatch to wrong impl",
            source_id="source_A",
        ))
        self.store.add(CounterExample(
            call_context="/test/b.cpp:20",
            wrong_target="fn_wrong_2",
            correct_target="fn_correct_2",
            pattern="callback pointer mismatch",
            source_id="source_B",
        ))
        self.store.add(CounterExample(
            call_context="/test/a.cpp:30",
            wrong_target="fn_wrong_3",
            correct_target="fn_correct_3",
            pattern="interface cast error",
            source_id="source_A",
        ))

    def test_get_for_source_returns_all_examples(self):
        """get_for_source returns ALL examples (architecture.md §3: 全量注入)."""
        examples_a = self.store.get_for_source("source_A")
        # 全量注入: all 3 examples visible to any source
        assert len(examples_a) == 3

    def test_get_for_source_returns_all_for_any_source(self):
        """get_for_source returns all examples regardless of source_id."""
        # Even a source that never reported anything sees all examples
        assert len(self.store.get_for_source("nonexistent")) == 3

    def test_render_markdown_for_source_includes_all(self):
        """render_markdown_for_source includes ALL examples (全量注入)."""
        md = self.store.render_markdown_for_source("source_A")
        assert "vtable dispatch to wrong impl" in md
        assert "interface cast error" in md
        assert "callback pointer mismatch" in md  # 全量注入: all visible

    def test_render_markdown_for_source_any_source_sees_all(self):
        """render_markdown_for_source returns all examples for any source."""
        md = self.store.render_markdown_for_source("nonexistent")
        assert "vtable dispatch" in md
        assert "callback pointer" in md

    def test_render_markdown_for_source_empty_id_falls_back_to_all(self):
        """render_markdown_for_source('') returns all examples."""
        md = self.store.render_markdown_for_source("")
        assert "vtable dispatch" in md
        assert "callback pointer" in md
        assert "interface cast" in md

    def test_render_markdown_format_has_headers(self):
        """Markdown has # Counter Examples header and ## per example."""
        md = self.store.render_markdown()
        assert md.startswith("# Counter Examples")
        assert "## 反例 1:" in md
        assert "## 反例 2:" in md

    def test_render_markdown_format_has_fields(self):
        """Each example has call_context, wrong_target, correct_target fields."""
        md = self.store.render_markdown_for_source("source_A")
        assert "**调用上下文**" in md
        assert "**错误目标**" in md
        assert "**正确目标**" in md
        assert "`fn_wrong_1`" in md
        assert "`fn_correct_1`" in md

    def test_persistence_across_reload(self):
        """FeedbackStore persists to JSON and reloads correctly."""
        from codemap_lite.analysis.feedback_store import FeedbackStore

        store2 = FeedbackStore(storage_dir=self._tmpdir)
        assert len(store2.list_all()) == 3
        # 全量注入: get_for_source returns all examples
        assert len(store2.get_for_source("source_A")) == 3

    def test_md_file_written_on_add(self):
        """Adding an example writes counter_examples.md to disk."""
        md_path = self._tmpdir / "counter_examples.md"
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "# Counter Examples" in content


# ===========================================================================
# §3 icsl_tools write-edge — atomic lifecycle
# ===========================================================================


class TestWriteEdgeAtomicLifecycle:
    """architecture.md §3: write-edge creates edge + RepairLog + deletes UC atomically."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, UnresolvedCallNode,
        )

        self.store = InMemoryGraphStore()
        self.store.create_function(FunctionNode(
            id="we_caller", signature="void Caller()", name="Caller",
            file_path="/test/we.cpp", start_line=1, end_line=10, body_hash="c",
        ))
        self.store.create_function(FunctionNode(
            id="we_callee", signature="void Callee()", name="Callee",
            file_path="/test/we.cpp", start_line=20, end_line=30, body_hash="e",
        ))
        # Pre-existing UC at the call site
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="we_caller", call_expression="ptr->method()",
            call_file="/test/we.cpp", call_line=5, call_type="indirect",
            source_code_snippet="ptr->method();", var_name="ptr", var_type="Base*",
        ))

    def test_write_edge_creates_calls_edge(self):
        """write-edge creates a CALLS edge with resolved_by=llm."""
        from codemap_lite.agent.icsl_tools import write_edge

        result = write_edge(
            caller_id="we_caller", callee_id="we_callee",
            call_type="indirect", call_file="/test/we.cpp", call_line=5,
            store=self.store, llm_response="analysis", reasoning_summary="reason",
        )
        assert result["edge_created"] is True
        assert self.store.edge_exists("we_caller", "we_callee", "/test/we.cpp", 5)

    def test_write_edge_creates_repair_log(self):
        """write-edge creates RepairLog with correct fields."""
        from codemap_lite.agent.icsl_tools import write_edge

        write_edge(
            caller_id="we_caller", callee_id="we_callee",
            call_type="indirect", call_file="/test/we.cpp", call_line=5,
            store=self.store, llm_response="vtable analysis",
            reasoning_summary="matched vtable pattern",
        )
        logs = self.store.get_repair_logs(caller_id="we_caller")
        assert len(logs) == 1
        log = logs[0]
        assert log.caller_id == "we_caller"
        assert log.callee_id == "we_callee"
        assert log.call_location == "/test/we.cpp:5"
        assert log.repair_method == "llm"
        assert log.llm_response == "vtable analysis"
        assert log.reasoning_summary == "matched vtable pattern"

    def test_write_edge_deletes_unresolved_call(self):
        """write-edge deletes the matching UnresolvedCall."""
        from codemap_lite.agent.icsl_tools import write_edge

        # Verify UC exists before
        ucs = self.store.get_unresolved_calls(caller_id="we_caller")
        assert len(ucs) == 1

        write_edge(
            caller_id="we_caller", callee_id="we_callee",
            call_type="indirect", call_file="/test/we.cpp", call_line=5,
            store=self.store,
        )

        # UC should be gone
        ucs_after = self.store.get_unresolved_calls(caller_id="we_caller")
        assert len(ucs_after) == 0

    def test_write_edge_repair_log_timestamp_is_utc(self):
        """RepairLog.timestamp is ISO-8601 UTC."""
        from codemap_lite.agent.icsl_tools import write_edge
        from datetime import datetime, timezone

        write_edge(
            caller_id="we_caller", callee_id="we_callee",
            call_type="indirect", call_file="/test/we.cpp", call_line=5,
            store=self.store,
        )
        logs = self.store.get_repair_logs(caller_id="we_caller")
        ts = logs[0].timestamp
        # Should be parseable as ISO-8601
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        # Should be UTC (offset-aware)
        assert parsed.tzinfo is not None

    def test_write_edge_truncates_reasoning_at_200(self):
        """reasoning_summary > 200 chars is truncated with ellipsis."""
        from codemap_lite.agent.icsl_tools import write_edge

        long_reason = "x" * 250
        write_edge(
            caller_id="we_caller", callee_id="we_callee",
            call_type="indirect", call_file="/test/we.cpp", call_line=5,
            store=self.store, reasoning_summary=long_reason,
        )
        logs = self.store.get_repair_logs(caller_id="we_caller")
        assert len(logs[0].reasoning_summary) <= 200
        assert logs[0].reasoning_summary.endswith("…")

    def test_write_edge_empty_llm_response_is_empty_string(self):
        """write-edge without --llm-response stores empty string, not None."""
        from codemap_lite.agent.icsl_tools import write_edge

        write_edge(
            caller_id="we_caller", callee_id="we_callee",
            call_type="indirect", call_file="/test/we.cpp", call_line=5,
            store=self.store,
        )
        logs = self.store.get_repair_logs(caller_id="we_caller")
        assert logs[0].llm_response == ""
        assert logs[0].reasoning_summary == ""


# ===========================================================================
# §3 check-complete — JSON schema contract
# ===========================================================================


class TestCheckCompleteSchema:
    """architecture.md §3: check-complete returns {complete, remaining_gaps, pending_gap_ids}."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, UnresolvedCallNode,
        )

        self.store = InMemoryGraphStore()
        self.store.create_function(FunctionNode(
            id="cc_src", signature="void Src()", name="Src",
            file_path="/test/cc.cpp", start_line=1, end_line=10, body_hash="s",
        ))
        self.store.create_function(FunctionNode(
            id="cc_callee", signature="void Callee()", name="Callee",
            file_path="/test/cc.cpp", start_line=20, end_line=30, body_hash="c",
        ))
        # Edge from src → callee (so callee is reachable)
        self.store.create_calls_edge("cc_src", "cc_callee", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/test/cc.cpp", call_line=5,
        ))
        # UC on src (pending)
        self.uc = UnresolvedCallNode(
            caller_id="cc_src", call_expression="foo()",
            call_file="/test/cc.cpp", call_line=8, call_type="indirect",
            source_code_snippet="foo();", var_name="f", var_type="Foo*",
        )
        self.store.create_unresolved_call(self.uc)

    def test_check_complete_has_required_keys(self):
        """check-complete returns all 3 required keys."""
        from codemap_lite.agent.icsl_tools import check_complete

        result = check_complete("cc_src", self.store)
        assert "complete" in result
        assert "remaining_gaps" in result
        assert "pending_gap_ids" in result

    def test_check_complete_false_with_pending_gaps(self):
        """complete=False when pending gaps exist."""
        from codemap_lite.agent.icsl_tools import check_complete

        result = check_complete("cc_src", self.store)
        assert result["complete"] is False
        assert result["remaining_gaps"] == 1
        assert len(result["pending_gap_ids"]) == 1

    def test_check_complete_true_when_no_gaps(self):
        """complete=True when all gaps resolved."""
        from codemap_lite.agent.icsl_tools import check_complete

        # Delete the UC
        self.store.delete_unresolved_call("cc_src", "/test/cc.cpp", 8)
        result = check_complete("cc_src", self.store)
        assert result["complete"] is True
        assert result["remaining_gaps"] == 0
        assert result["pending_gap_ids"] == []

    def test_check_complete_ignores_unresolvable_gaps(self):
        """check-complete only counts status=pending, not unresolvable."""
        # Mark the UC as unresolvable
        self.store.update_unresolved_call_retry_state(
            self.uc.id, "2026-05-15T00:00:00Z", "gate_failed: no edges"
        )
        self.store.update_unresolved_call_retry_state(
            self.uc.id, "2026-05-15T01:00:00Z", "gate_failed: no edges"
        )
        self.store.update_unresolved_call_retry_state(
            self.uc.id, "2026-05-15T02:00:00Z", "gate_failed: no edges"
        )
        # Now status=unresolvable, retry_count=3
        from codemap_lite.agent.icsl_tools import check_complete

        result = check_complete("cc_src", self.store)
        # Should be complete since only pending gaps count
        assert result["complete"] is True
        assert result["remaining_gaps"] == 0


# ===========================================================================
# §7 Incremental — full 5-step cascade with SourcePoint reset
# ===========================================================================


class TestIncrementalFullCascade:
    """architecture.md §7: Full 5-step cascade invalidation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import (
            CallsEdgeProps, FunctionNode, RepairLogNode,
            SourcePointNode, UnresolvedCallNode,
        )

        self.store = InMemoryGraphStore()
        # File A: functions fn_a1, fn_a2
        self.store.create_function(FunctionNode(
            id="inc_a1", signature="void A1()", name="A1",
            file_path="/test/file_a.cpp", start_line=1, end_line=10, body_hash="a1",
        ))
        self.store.create_function(FunctionNode(
            id="inc_a2", signature="void A2()", name="A2",
            file_path="/test/file_a.cpp", start_line=20, end_line=30, body_hash="a2",
        ))
        # File B: function fn_b1 (caller of fn_a1 via LLM edge)
        self.store.create_function(FunctionNode(
            id="inc_b1", signature="void B1()", name="B1",
            file_path="/test/file_b.cpp", start_line=1, end_line=10, body_hash="b1",
        ))
        # File C: function fn_c1 (caller of fn_a2 via static edge)
        self.store.create_function(FunctionNode(
            id="inc_c1", signature="void C1()", name="C1",
            file_path="/test/file_c.cpp", start_line=1, end_line=10, body_hash="c1",
        ))
        # Edges: B1→A1 (llm), C1→A2 (symbol_table), A1→A2 (direct)
        self.store.create_calls_edge("inc_b1", "inc_a1", CallsEdgeProps(
            resolved_by="llm", call_type="indirect",
            call_file="/test/file_b.cpp", call_line=5,
        ))
        self.store.create_calls_edge("inc_c1", "inc_a2", CallsEdgeProps(
            resolved_by="symbol_table", call_type="direct",
            call_file="/test/file_c.cpp", call_line=5,
        ))
        self.store.create_calls_edge("inc_a1", "inc_a2", CallsEdgeProps(
            resolved_by="dataflow", call_type="indirect",
            call_file="/test/file_a.cpp", call_line=5,
        ))
        # RepairLog for the LLM edge B1→A1
        self.store.create_repair_log(RepairLogNode(
            caller_id="inc_b1", callee_id="inc_a1",
            call_location="/test/file_b.cpp:5",
            repair_method="llm", llm_response="vtable",
            timestamp="2026-05-15T00:00:00Z",
            reasoning_summary="vtable dispatch",
        ))
        # SourcePoint for B1 (status=complete)
        self.store.create_source_point(SourcePointNode(
            id="sp_inc_b1", entry_point_kind="public_api",
            reason="test", function_id="inc_b1", module="test",
            status="complete",
        ))
        # SourcePoint for C1 (status=complete)
        self.store.create_source_point(SourcePointNode(
            id="sp_inc_c1", entry_point_kind="callback",
            reason="test", function_id="inc_c1", module="test",
            status="complete",
        ))

    def test_invalidate_removes_functions_in_file(self):
        """Step 1-3: Functions in changed file are deleted."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store, target_dir="/test")
        result = updater.invalidate_file("/test/file_a.cpp")
        assert "inc_a1" in result.removed_functions
        assert "inc_a2" in result.removed_functions
        # Functions should be gone from store
        assert self.store.get_function_by_id("inc_a1") is None
        assert self.store.get_function_by_id("inc_a2") is None

    def test_invalidate_removes_edges(self):
        """Step 3: All edges to/from changed functions are deleted."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store, target_dir="/test")
        result = updater.invalidate_file("/test/file_a.cpp")
        # All 3 edges should be gone
        assert result.removed_edges == 3
        assert not self.store.edge_exists("inc_b1", "inc_a1", "/test/file_b.cpp", 5)
        assert not self.store.edge_exists("inc_c1", "inc_a2", "/test/file_c.cpp", 5)
        assert not self.store.edge_exists("inc_a1", "inc_a2", "/test/file_a.cpp", 5)

    def test_invalidate_deletes_repair_log_for_llm_edges(self):
        """Step 3b: RepairLog for LLM edges pointing to changed file is deleted."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store, target_dir="/test")
        updater.invalidate_file("/test/file_a.cpp")
        logs = self.store.get_repair_logs(caller_id="inc_b1")
        assert len(logs) == 0

    def test_invalidate_regenerates_uc_for_llm_callers(self):
        """Step 3b: UC regenerated for cross-file LLM callers."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store, target_dir="/test")
        result = updater.invalidate_file("/test/file_a.cpp")
        assert len(result.regenerated_unresolved_calls) >= 1
        # B1 should have a new UC (it had an LLM edge to A1)
        ucs = self.store.get_unresolved_calls(caller_id="inc_b1")
        assert len(ucs) == 1
        uc = ucs[0]
        assert uc.call_file == "/test/file_b.cpp"
        assert uc.call_line == 5
        assert uc.retry_count == 0
        assert uc.status == "pending"

    def test_invalidate_does_not_regenerate_uc_for_static_callers(self):
        """Step 3b: Non-LLM callers do NOT get UC regenerated."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store, target_dir="/test")
        updater.invalidate_file("/test/file_a.cpp")
        # C1 had a symbol_table edge to A2 — should NOT get a UC
        ucs = self.store.get_unresolved_calls(caller_id="inc_c1")
        assert len(ucs) == 0

    def test_invalidate_resets_source_point_status(self):
        """Step 4: Affected SourcePoints reset to pending."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store, target_dir="/test")
        updater.invalidate_file("/test/file_a.cpp")
        # B1's SourcePoint should be reset (it had LLM edge to changed file)
        sp_b1 = self.store.get_source_point("sp_inc_b1")
        assert sp_b1.status == "pending"

    def test_invalidate_reports_affected_source_ids(self):
        """Step 5: affected_source_ids populated for orchestrator."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store, target_dir="/test")
        result = updater.invalidate_file("/test/file_a.cpp")
        # B1 is affected (LLM edge to changed file)
        assert "inc_b1" in result.affected_source_ids

    def test_invalidate_non_llm_caller_source_point_also_reset(self):
        """Step 4: Even non-LLM callers' SourcePoints are reset (reachable subgraph changed)."""
        from codemap_lite.graph.incremental import IncrementalUpdater

        updater = IncrementalUpdater(self.store, target_dir="/test")
        updater.invalidate_file("/test/file_a.cpp")
        # C1's SourcePoint should also be reset (its callee was deleted)
        sp_c1 = self.store.get_source_point("sp_inc_c1")
        assert sp_c1.status == "pending"


# ===========================================================================
# §8 /stats — unresolved_by_category prefix parsing
# ===========================================================================


class TestStatsCategoryBucketing:
    """architecture.md §8: unresolved_by_category parses last_attempt_reason prefix."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        self.store.create_function(FunctionNode(
            id="cat_fn", signature="void F()", name="F",
            file_path="/test/cat.cpp", start_line=1, end_line=10, body_hash="f",
        ))
        # Create UCs with different last_attempt_reason categories
        reasons = [
            ("gate_failed: no edges produced", 10),
            ("agent_error: exit code 1", 20),
            ("subprocess_timeout: 30s", 30),
            ("subprocess_crash: FileNotFoundError", 40),
            ("agent_exited_without_edge", 50),
            (None, 60),  # no reason → "none" bucket
            ("gate_failed: second failure", 70),
        ]
        for reason, line in reasons:
            uc = UnresolvedCallNode(
                caller_id="cat_fn", call_expression="x()",
                call_file="/test/cat.cpp", call_line=line, call_type="indirect",
                source_code_snippet="x();", var_name="x", var_type="X*",
                last_attempt_reason=reason,
            )
            self.store.create_unresolved_call(uc)
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_stats_has_unresolved_by_category(self):
        """GET /stats returns unresolved_by_category."""
        r = self.client.get("/api/v1/stats")
        data = r.json()
        assert "unresolved_by_category" in data

    def test_category_bucketing_correct(self):
        """Categories are correctly parsed from last_attempt_reason prefix."""
        r = self.client.get("/api/v1/stats")
        cats = r.json()["unresolved_by_category"]
        assert cats["gate_failed"] == 2  # two gate_failed entries
        assert cats["agent_error"] == 1
        assert cats["subprocess_timeout"] == 1
        assert cats["subprocess_crash"] == 1
        assert cats["agent_exited_without_edge"] == 1
        assert cats["none"] == 1  # the one with None reason

    def test_category_keys_always_present(self):
        """All 5 category keys + 'none' are always present (even if 0)."""
        # Create a store with no UCs
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from fastapi.testclient import TestClient

        empty_store = InMemoryGraphStore()
        app = create_app(store=empty_store)
        client = TestClient(app)
        r = client.get("/api/v1/stats")
        cats = r.json()["unresolved_by_category"]
        expected_keys = {
            "gate_failed", "agent_error", "subprocess_crash",
            "subprocess_timeout", "agent_exited_without_edge", "none",
        }
        for key in expected_keys:
            assert key in cats, f"Missing category key: {key}"
            assert cats[key] == 0


# ===========================================================================
# §3 Prompt builder — counter-example injection path
# ===========================================================================


class TestPromptBuilderCounterExamplePath:
    """architecture.md §3: Prompt references counter_examples.md for agent injection."""

    def test_prompt_references_counter_examples_md(self):
        """build_repair_prompt mentions counter_examples.md path."""
        from codemap_lite.analysis.prompt_builder import build_repair_prompt

        prompt = build_repair_prompt("source_123")
        assert "counter_examples.md" in prompt

    def test_prompt_contains_write_edge_with_reasoning_flags(self):
        """Prompt instructs agent to pass --llm-response and --reasoning-summary."""
        from codemap_lite.analysis.prompt_builder import build_repair_prompt

        prompt = build_repair_prompt("source_123")
        assert "--llm-response" in prompt
        assert "--reasoning-summary" in prompt

    def test_prompt_contains_query_reachable_step(self):
        """Prompt instructs agent to run query-reachable first."""
        from codemap_lite.analysis.prompt_builder import build_repair_prompt

        prompt = build_repair_prompt("source_123")
        assert "query-reachable" in prompt
        assert "--source source_123" in prompt

    def test_prompt_contains_check_complete_reference(self):
        """Prompt mentions check-complete for gate verification."""
        from codemap_lite.analysis.prompt_builder import build_repair_prompt

        prompt = build_repair_prompt("source_123")
        assert "check-complete" in prompt

    def test_prompt_uses_safe_dirname_for_icsl_dir(self):
        """Prompt uses _safe_dirname for .icslpreprocess directory."""
        from codemap_lite.analysis.prompt_builder import build_repair_prompt

        # Source with path-unsafe characters
        prompt = build_repair_prompt("dir/file.h::NS::Method")
        assert ".icslpreprocess_" in prompt
        # Should not contain raw '/' or '::'
        lines = [l for l in prompt.split("\n") if "icslpreprocess" in l]
        for line in lines:
            # The directory name should be sanitized
            if "icslpreprocess_" in line:
                # Extract the dir name
                import re
                match = re.search(r"\.icslpreprocess_(\S+)/", line)
                if match:
                    dirname = match.group(1)
                    assert "/" not in dirname
                    assert "::" not in dirname


# ===========================================================================
# §3 Repair orchestrator — error stamping categories
# ===========================================================================


class TestRepairOrchestratorErrorStamping:
    """architecture.md §3: Orchestrator stamps correct categories on failure."""

    def test_truncate_reason_within_limit(self):
        """Reasons ≤200 chars pass through unchanged."""
        from codemap_lite.analysis.repair_orchestrator import _truncate_reason

        reason = "gate_failed: no edges produced"
        assert _truncate_reason(reason) == reason

    def test_truncate_reason_over_limit(self):
        """Reasons >200 chars are truncated with ellipsis."""
        from codemap_lite.analysis.repair_orchestrator import _truncate_reason

        long_reason = "gate_failed: " + "x" * 200
        result = _truncate_reason(long_reason)
        assert len(result) == 200
        assert result.endswith("…")

    def test_safe_dirname_sanitizes_path_chars(self):
        """_safe_dirname removes /, \\, : from source IDs."""
        from codemap_lite.analysis.repair_orchestrator import _safe_dirname

        assert "/" not in _safe_dirname("dir/file.h::NS::Method")
        assert "\\" not in _safe_dirname("C:\\path\\file.cpp")
        assert "::" not in _safe_dirname("ns::class::method")

    def test_safe_dirname_truncates_long_ids(self):
        """_safe_dirname truncates IDs > 60 chars with hash suffix."""
        from codemap_lite.analysis.repair_orchestrator import _safe_dirname

        long_id = "a" * 100
        result = _safe_dirname(long_id)
        assert len(result) <= 69  # 60 + 1 underscore + 8 hash chars

    def test_build_subprocess_env_strips_proxy_vars(self):
        """_build_subprocess_env removes proxy environment variables."""
        from codemap_lite.analysis.repair_orchestrator import _build_subprocess_env
        import os

        # Temporarily set proxy vars
        old = os.environ.get("http_proxy")
        os.environ["http_proxy"] = "http://proxy:8080"
        try:
            env = _build_subprocess_env(None)
            assert "http_proxy" not in env
        finally:
            if old is None:
                os.environ.pop("http_proxy", None)
            else:
                os.environ["http_proxy"] = old

    def test_build_subprocess_env_applies_overrides(self):
        """_build_subprocess_env merges override dict."""
        from codemap_lite.analysis.repair_orchestrator import _build_subprocess_env

        env = _build_subprocess_env({"CUSTOM_VAR": "value"})
        assert env["CUSTOM_VAR"] == "value"


# ===========================================================================
# §8 GET /api/v1/unresolved-calls — filtering contract
# ===========================================================================


class TestUnresolvedCallsEndpointFiltering:
    """architecture.md §8: GET /unresolved-calls supports caller, status, category filters."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from codemap_lite.graph.schema import FunctionNode, UnresolvedCallNode
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        self.store.create_function(FunctionNode(
            id="uc_fn_a", signature="void A()", name="A",
            file_path="/test/uc.cpp", start_line=1, end_line=10, body_hash="a",
        ))
        self.store.create_function(FunctionNode(
            id="uc_fn_b", signature="void B()", name="B",
            file_path="/test/uc.cpp", start_line=20, end_line=30, body_hash="b",
        ))
        # UCs with different statuses and reasons
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="uc_fn_a", call_expression="x()",
            call_file="/test/uc.cpp", call_line=5, call_type="indirect",
            source_code_snippet="x();", var_name="x", var_type="X*",
            status="pending",
        ))
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="uc_fn_a", call_expression="y()",
            call_file="/test/uc.cpp", call_line=8, call_type="virtual",
            source_code_snippet="y();", var_name="y", var_type="Y*",
            status="unresolvable", retry_count=3,
            last_attempt_reason="gate_failed: no edges",
        ))
        self.store.create_unresolved_call(UnresolvedCallNode(
            caller_id="uc_fn_b", call_expression="z()",
            call_file="/test/uc.cpp", call_line=25, call_type="indirect",
            source_code_snippet="z();", var_name="z", var_type="Z*",
            status="pending",
        ))
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_list_all_unresolved_calls(self):
        """GET /unresolved-calls returns all with {total, items}."""
        r = self.client.get("/api/v1/unresolved-calls")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_filter_by_caller(self):
        """GET /unresolved-calls?caller=X filters by caller_id."""
        r = self.client.get("/api/v1/unresolved-calls", params={"caller": "uc_fn_a"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["caller_id"] == "uc_fn_a"

    def test_filter_by_status(self):
        """GET /unresolved-calls?status=pending filters by status."""
        r = self.client.get("/api/v1/unresolved-calls", params={"status": "pending"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["status"] == "pending"

    def test_filter_by_category(self):
        """GET /unresolved-calls?category=gate_failed filters by reason prefix."""
        r = self.client.get("/api/v1/unresolved-calls", params={"category": "gate_failed"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["last_attempt_reason"].startswith("gate_failed")

    def test_unresolved_call_has_all_fields(self):
        """Each UC item has all required fields."""
        r = self.client.get("/api/v1/unresolved-calls")
        item = r.json()["items"][0]
        required_fields = {
            "id", "caller_id", "call_expression", "call_file", "call_line",
            "call_type", "status", "retry_count",
        }
        for field in required_fields:
            assert field in item, f"Missing field: {field}"


# ===========================================================================
# §8 POST /api/v1/feedback — counter-example creation via API
# ===========================================================================


class TestFeedbackEndpointContract:
    """architecture.md §8: POST /feedback creates counter-example, GET /feedback lists them."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.analysis.feedback_store import FeedbackStore
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from fastapi.testclient import TestClient

        self._tmpdir = Path(tempfile.mkdtemp())
        self.feedback_store = FeedbackStore(storage_dir=self._tmpdir)
        self.store = InMemoryGraphStore()
        app = create_app(store=self.store, feedback_store=self.feedback_store)
        self.client = TestClient(app)

    def test_post_feedback_creates_example(self):
        """POST /feedback creates a counter-example and returns 201."""
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "/test/fb.cpp:10",
            "wrong_target": "fn_wrong",
            "correct_target": "fn_correct",
            "pattern": "vtable dispatch error",
            "source_id": "src_1",
        })
        assert r.status_code == 201
        data = r.json()
        assert data["call_context"] == "/test/fb.cpp:10"
        assert data["wrong_target"] == "fn_wrong"
        assert data["correct_target"] == "fn_correct"
        assert data["deduplicated"] is False

    def test_post_feedback_deduplication(self):
        """POST /feedback with same pattern returns deduplicated=True."""
        body = {
            "call_context": "/test/fb.cpp:10",
            "wrong_target": "fn_wrong",
            "correct_target": "fn_correct",
            "pattern": "same pattern",
            "source_id": "src_1",
        }
        self.client.post("/api/v1/feedback", json=body)
        r = self.client.post("/api/v1/feedback", json=body)
        assert r.status_code == 201
        assert r.json()["deduplicated"] is True

    def test_post_feedback_validates_targets_differ(self):
        """POST /feedback rejects wrong_target == correct_target."""
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "/test/fb.cpp:10",
            "wrong_target": "same_fn",
            "correct_target": "same_fn",
            "pattern": "test",
        })
        assert r.status_code == 422

    def test_get_feedback_returns_paginated(self):
        """GET /feedback returns {total, items} with pagination."""
        # Add 3 examples
        for i in range(3):
            self.client.post("/api/v1/feedback", json={
                "call_context": f"/test/fb.cpp:{i}",
                "wrong_target": f"wrong_{i}",
                "correct_target": f"correct_{i}",
                "pattern": f"pattern_{i}",
            })
        r = self.client.get("/api/v1/feedback")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_get_feedback_pagination_limit_offset(self):
        """GET /feedback respects limit and offset."""
        for i in range(5):
            self.client.post("/api/v1/feedback", json={
                "call_context": f"/test/fb.cpp:{i}",
                "wrong_target": f"wrong_{i}",
                "correct_target": f"correct_{i}",
                "pattern": f"pattern_{i}",
            })
        r = self.client.get("/api/v1/feedback", params={"limit": 2, "offset": 1})
        data = r.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_post_feedback_returns_total(self):
        """POST /feedback response includes total library size."""
        self.client.post("/api/v1/feedback", json={
            "call_context": "/test/fb.cpp:1",
            "wrong_target": "w1", "correct_target": "c1", "pattern": "p1",
        })
        r = self.client.post("/api/v1/feedback", json={
            "call_context": "/test/fb.cpp:2",
            "wrong_target": "w2", "correct_target": "c2", "pattern": "p2",
        })
        assert r.json()["total"] == 2

    def test_post_feedback_no_store_returns_503(self):
        """POST /feedback without FeedbackStore returns 503."""
        from codemap_lite.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(store=self.store)  # no feedback_store
        client = TestClient(app)
        r = client.post("/api/v1/feedback", json={
            "call_context": "/test/fb.cpp:1",
            "wrong_target": "w", "correct_target": "c", "pattern": "p",
        })
        assert r.status_code == 503


# ===========================================================================
# §8 POST /api/v1/analyze — trigger analysis + status polling
# ===========================================================================


class TestAnalyzeEndpointContract:
    """architecture.md §8: POST /analyze triggers async analysis, GET /analyze/status polls."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from codemap_lite.api.app import create_app
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        from fastapi.testclient import TestClient

        self.store = InMemoryGraphStore()
        app = create_app(store=self.store)
        self.client = TestClient(app)

    def test_post_analyze_returns_202(self):
        """POST /analyze returns 202 Accepted."""
        r = self.client.post("/api/v1/analyze", json={"mode": "full"})
        assert r.status_code == 202
        assert r.json()["status"] == "accepted"
        assert r.json()["mode"] == "full"

    def test_post_analyze_incremental(self):
        """POST /analyze mode=incremental is accepted."""
        r = self.client.post("/api/v1/analyze", json={"mode": "incremental"})
        assert r.status_code == 202
        assert r.json()["mode"] == "incremental"

    def test_post_analyze_invalid_mode(self):
        """POST /analyze with invalid mode returns 422."""
        r = self.client.post("/api/v1/analyze", json={"mode": "invalid"})
        assert r.status_code == 422

    def test_post_analyze_double_spawn_409(self):
        """POST /analyze while running returns 409."""
        self.client.post("/api/v1/analyze", json={"mode": "full"})
        r = self.client.post("/api/v1/analyze", json={"mode": "full"})
        assert r.status_code == 409

    def test_get_analyze_status(self):
        """GET /analyze/status returns state + progress + sources."""
        r = self.client.get("/api/v1/analyze/status")
        assert r.status_code == 200
        data = r.json()
        assert "state" in data
        assert "progress" in data
        assert "sources" in data

    def test_post_analyze_repair_returns_202(self):
        """POST /analyze/repair returns 202."""
        r = self.client.post("/api/v1/analyze/repair", json={"source_ids": []})
        assert r.status_code == 202
        assert r.json()["action"] == "repair"


# ===========================================================================
# §4 CallsEdgeProps validation — __post_init__
# ===========================================================================


class TestCallsEdgePropsValidation:
    """architecture.md §4: CallsEdgeProps validates resolved_by and call_type."""

    def test_valid_resolved_by_values(self):
        """All 5 resolved_by values are accepted."""
        from codemap_lite.graph.schema import CallsEdgeProps

        for rb in ["symbol_table", "signature", "dataflow", "context", "llm"]:
            props = CallsEdgeProps(
                resolved_by=rb, call_type="direct",
                call_file="/test.cpp", call_line=1,
            )
            assert props.resolved_by == rb

    def test_invalid_resolved_by_raises(self):
        """Invalid resolved_by raises ValueError."""
        from codemap_lite.graph.schema import CallsEdgeProps

        with pytest.raises(ValueError, match="resolved_by"):
            CallsEdgeProps(
                resolved_by="magic", call_type="direct",
                call_file="/test.cpp", call_line=1,
            )

    def test_valid_call_type_values(self):
        """All 3 call_type values are accepted."""
        from codemap_lite.graph.schema import CallsEdgeProps

        for ct in ["direct", "indirect", "virtual"]:
            props = CallsEdgeProps(
                resolved_by="llm", call_type=ct,
                call_file="/test.cpp", call_line=1,
            )
            assert props.call_type == ct

    def test_invalid_call_type_raises(self):
        """Invalid call_type raises ValueError."""
        from codemap_lite.graph.schema import CallsEdgeProps

        with pytest.raises(ValueError, match="call_type"):
            CallsEdgeProps(
                resolved_by="llm", call_type="unknown",
                call_file="/test.cpp", call_line=1,
            )

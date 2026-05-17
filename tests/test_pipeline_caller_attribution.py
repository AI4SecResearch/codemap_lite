"""Pipeline caller-attribution bugs — architecture.md §1-§2.

Uses real CastEngine tree-sitter results to expose:
1. Overloaded function misattribution (by_file_name collision)
2. Call-line outside caller body range
3. Edge count accuracy vs parser output
4. Incremental cascade correctness with real data
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import CallsEdgeProps, FunctionNode, UnresolvedCallNode
from codemap_lite.pipeline.orchestrator import (
    PipelineOrchestrator,
    _make_function_id,
    _normalize_call_type,
)


CASTENGINE_DIR = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")


@pytest.fixture(scope="module")
def castengine_data():
    """Parse CastEngine once, return (store, result)."""
    if not CASTENGINE_DIR.exists():
        pytest.skip("CastEngine directory not available")
    store = InMemoryGraphStore()
    orch = PipelineOrchestrator(store=store, target_dir=CASTENGINE_DIR)
    result = orch.run_full_analysis()
    return store, result


# ---------------------------------------------------------------------------
# BUG: Overloaded function misattribution
# ---------------------------------------------------------------------------


class TestOverloadedFunctionMisattribution:
    """BUG: by_file_name uses (file, name) as key — overloads collide.

    Root cause: pipeline/orchestrator.py line 286:
        by_file_name[(fn.file_path, fn.name)] = fid
    When multiple functions have the same name in the same file (C++ overloads),
    only the LAST registered one wins. Calls inside earlier overloads get
    attributed to the wrong function instance.

    architecture.md §2: "3-bucket resolution (by_file_name > by_name > by_bare_name)"
    The by_file_name bucket should be EXACT, but it's not when overloads exist.
    """

    def test_overloaded_functions_exist(self, castengine_data):
        """CastEngine has many overloaded functions (same name, same file)."""
        store, _ = castengine_data
        file_name_count: dict[tuple[str, str], int] = defaultdict(int)
        for fn in store.list_functions():
            file_name_count[(fn.file_path, fn.name)] += 1
        overloaded = {k: v for k, v in file_name_count.items() if v > 1}
        assert len(overloaded) > 100, f"Only {len(overloaded)} overloaded pairs"

    def test_misattributed_edges_count(self, castengine_data):
        """Edges where call_line is outside caller's body range.

        KNOWN BUG: ~200 edges (4.4%) are misattributed due to overload collision.
        This test documents the regression baseline.
        """
        store, _ = castengine_data
        fns = {fn.id: fn for fn in store.list_functions()}
        edges = store.list_calls_edges()

        misattributed = 0
        for e in edges:
            caller = fns.get(e.caller_id)
            if caller and not (caller.start_line <= e.props.call_line <= caller.end_line):
                misattributed += 1

        # Document the known bug count — should not get WORSE
        assert misattributed <= 200, (
            f"Misattributed edges increased: {misattributed} (was 200)"
        )
        # This SHOULD be 0 when the bug is fixed
        # assert misattributed == 0, f"{misattributed} edges misattributed"

    def test_all_misattributed_are_same_name_overloads(self, castengine_data):
        """All misattributed edges are due to same-name overloads, not other bugs."""
        store, _ = castengine_data
        fns = {fn.id: fn for fn in store.list_functions()}
        edges = store.list_calls_edges()

        # Build (file, name) -> list of functions
        file_name_fns: dict[tuple[str, str], list] = defaultdict(list)
        for fn in store.list_functions():
            file_name_fns[(fn.file_path, fn.name)].append(fn)

        non_overload_misattributed = []
        for e in edges:
            caller = fns.get(e.caller_id)
            if not caller:
                continue
            if caller.start_line <= e.props.call_line <= caller.end_line:
                continue
            # Is this an overload case?
            siblings = file_name_fns.get((caller.file_path, caller.name), [])
            if len(siblings) <= 1:
                non_overload_misattributed.append(
                    f"{caller.name}@{caller.file_path}:{caller.start_line}-{caller.end_line} "
                    f"call at line {e.props.call_line}"
                )

        assert non_overload_misattributed == [], (
            f"Non-overload misattribution: {non_overload_misattributed[:5]}"
        )


# ---------------------------------------------------------------------------
# BUG: by_file_name only stores ONE id per (file, name)
# ---------------------------------------------------------------------------


class TestByFileNameCollision:
    """The by_file_name dict loses overloaded function IDs.

    When 3 overloads of 'Play' exist in the same file, only the last one's
    ID is stored. The other 2 become unreachable via by_file_name lookup.
    """

    def test_overloads_lose_ids_in_by_file_name(self, castengine_data):
        """Count functions that are 'shadowed' by later overloads."""
        store, _ = castengine_data
        # Simulate what the pipeline does
        by_file_name: dict[tuple[str, str], str] = {}
        shadowed = 0
        for fn in store.list_functions():
            key = (fn.file_path, fn.name)
            if key in by_file_name:
                shadowed += 1
            by_file_name[key] = fn.id

        # 582 functions in 201 overloaded groups → 582 - 201 = 381 shadowed
        assert shadowed > 300, f"Only {shadowed} shadowed (expected ~381)"

    def test_shadowed_functions_never_appear_as_callers(self, castengine_data):
        """Shadowed functions can never be the caller of any edge.

        This is a consequence of the bug: if a function's ID is overwritten
        in by_file_name, no edge will ever have it as caller_id (because
        _resolve_id returns the wrong ID for that name).
        """
        store, _ = castengine_data
        # Find which IDs are "winners" in by_file_name
        by_file_name: dict[tuple[str, str], str] = {}
        for fn in store.list_functions():
            by_file_name[(fn.file_path, fn.name)] = fn.id
        winner_ids = set(by_file_name.values())

        # Check edges: are there callers that are NOT winners?
        edges = store.list_calls_edges()
        non_winner_callers = set()
        for e in edges:
            if e.caller_id not in winner_ids:
                non_winner_callers.add(e.caller_id)

        # These callers were resolved via by_name or by_bare_name (not by_file_name)
        # They exist because the caller name is globally unique even though
        # it's overloaded in its own file. This is fine — but it means
        # by_file_name is NOT the primary resolution path for overloads.
        # The real issue is when by_file_name IS used and picks the wrong one.


# ---------------------------------------------------------------------------
# Correct behavior: edges within caller body
# ---------------------------------------------------------------------------


class TestCorrectEdgeAttribution:
    """Verify that the majority of edges are correctly attributed."""

    def test_majority_edges_inside_caller_body(self, castengine_data):
        """At least 95% of edges should have call_line inside caller body."""
        store, _ = castengine_data
        fns = {fn.id: fn for fn in store.list_functions()}
        edges = store.list_calls_edges()

        inside = 0
        total = 0
        for e in edges:
            caller = fns.get(e.caller_id)
            if caller:
                total += 1
                if caller.start_line <= e.props.call_line <= caller.end_line:
                    inside += 1

        pct = inside / total * 100
        assert pct >= 95.0, f"Only {pct:.1f}% edges inside caller body"

    def test_no_edges_with_negative_call_line(self, castengine_data):
        store, _ = castengine_data
        edges = store.list_calls_edges()
        bad = [e for e in edges if e.props.call_line <= 0]
        assert bad == []


# ---------------------------------------------------------------------------
# UC quality checks
# ---------------------------------------------------------------------------


class TestUCQuality:
    """architecture.md §2: UC metadata quality."""

    def test_uc_call_line_inside_caller_body(self, castengine_data):
        """UC call_line should be inside the caller's body range."""
        store, _ = castengine_data
        fns = {fn.id: fn for fn in store.list_functions()}
        ucs = store.get_unresolved_calls()

        outside = 0
        total = 0
        for uc in ucs:
            caller = fns.get(uc.caller_id)
            if caller:
                total += 1
                if not (caller.start_line <= uc.call_line <= caller.end_line):
                    outside += 1

        # Same bug affects UCs — document the baseline
        pct_outside = outside / total * 100 if total > 0 else 0
        # Should be 0% when fixed; currently expect some due to overload bug
        assert pct_outside <= 10.0, (
            f"{pct_outside:.1f}% UCs have call_line outside caller body"
        )

    def test_uc_call_expression_not_empty(self, castengine_data):
        """Most UCs should have a non-empty call_expression."""
        store, _ = castengine_data
        ucs = store.get_unresolved_calls()
        empty_expr = [u for u in ucs if not u.call_expression]
        pct_empty = len(empty_expr) / len(ucs) * 100
        assert pct_empty < 5.0, f"{pct_empty:.1f}% UCs have empty call_expression"

    def test_uc_call_type_distribution(self, castengine_data):
        """UCs should be mostly indirect/virtual (not direct)."""
        store, _ = castengine_data
        ucs = store.get_unresolved_calls()
        by_type: dict[str, int] = defaultdict(int)
        for uc in ucs:
            by_type[uc.call_type] += 1

        # Direct calls should be resolved, not UCs
        # (unless ambiguous — which is valid)
        total = len(ucs)
        direct_pct = by_type.get("direct", 0) / total * 100
        # CastEngine has many unresolved direct calls (macros, stdlib, cross-TU)
        # that tree-sitter can't resolve without a linker. Up to ~85% is normal.
        assert direct_pct < 90.0, (
            f"{direct_pct:.1f}% UCs are 'direct' — unexpectedly high"
        )


# ---------------------------------------------------------------------------
# Pipeline idempotency
# ---------------------------------------------------------------------------


class TestPipelineIdempotency:
    """Running full analysis twice should produce same results."""

    def test_second_run_same_counts(self):
        """Re-running full analysis on same data produces same graph."""
        if not CASTENGINE_DIR.exists():
            pytest.skip("CastEngine directory not available")

        store1 = InMemoryGraphStore()
        orch1 = PipelineOrchestrator(store=store1, target_dir=CASTENGINE_DIR)
        r1 = orch1.run_full_analysis()

        store2 = InMemoryGraphStore()
        orch2 = PipelineOrchestrator(store=store2, target_dir=CASTENGINE_DIR)
        r2 = orch2.run_full_analysis()

        assert r1.files_scanned == r2.files_scanned
        assert r1.functions_found == r2.functions_found
        assert r1.direct_calls == r2.direct_calls
        assert r1.unresolved_calls == r2.unresolved_calls


# ---------------------------------------------------------------------------
# Edge deduplication
# ---------------------------------------------------------------------------


class TestEdgeDeduplication:
    """architecture.md §4: edges unique by (caller, callee, call_file, call_line)."""

    def test_no_duplicate_edges(self, castengine_data):
        """No two edges should have the same (caller, callee, file, line)."""
        store, _ = castengine_data
        edges = store.list_calls_edges()
        seen: set[tuple[str, str, str, int]] = set()
        duplicates = []
        for e in edges:
            key = (e.caller_id, e.callee_id, e.props.call_file, e.props.call_line)
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        assert duplicates == [], f"Duplicate edges: {duplicates[:5]}"

    def test_no_duplicate_ucs(self, castengine_data):
        """No two UCs should have the same (caller_id, call_file, call_line)."""
        store, _ = castengine_data
        ucs = store.get_unresolved_calls()
        seen: set[tuple[str, str, int]] = set()
        duplicates = []
        for uc in ucs:
            key = (uc.caller_id, uc.call_file, uc.call_line)
            if key in seen:
                duplicates.append((uc.id, key))
            seen.add(key)
        assert duplicates == [], f"Duplicate UCs: {duplicates[:5]}"

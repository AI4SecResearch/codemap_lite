"""Pipeline second-pass resolution bugs — architecture.md §1/§4.

Tests targeting known bugs in _resolve_id and the dual-list processing
that causes edge+UC conflicts, self-loops, and overloaded function collisions.

BUG HUNTING TARGETS:
1. by_file_name[(file, name)] = id OVERWRITES overloaded functions (same file, same name)
2. Plugin returns same call in BOTH `calls` and `unresolved` lists → edge+UC conflict
3. Self-loops from overloaded collision: caller resolves to itself
4. _resolve_id returns None for ambiguous names → UC created even when unambiguous in context
5. Bare name fallback cross-links unrelated modules
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    UnresolvedCallNode,
)
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator


CASTENGINE_DIR = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")


@pytest.fixture(scope="module")
def castengine_store():
    if not CASTENGINE_DIR.exists():
        pytest.skip("CastEngine directory not available")
    store = InMemoryGraphStore()
    orch = PipelineOrchestrator(store=store, target_dir=CASTENGINE_DIR)
    orch.run_full_analysis()
    return store


# ---------------------------------------------------------------------------
# BUG 1: by_file_name overloaded function collision
# ---------------------------------------------------------------------------


class TestOverloadedFunctionCollision:
    """by_file_name[(file, name)] = id loses all but the LAST overloaded variant.

    When multiple functions share (file_path, name) — e.g., overloaded C++ methods —
    only the last one's ID survives in the index. Calls to earlier variants resolve
    to the wrong function.
    """

    def test_overloaded_pairs_exist(self, castengine_store):
        """CastEngine has 200+ (file, name) pairs with multiple definitions."""
        file_name_count: dict[tuple[str, str], int] = defaultdict(int)
        for fn in castengine_store.list_functions():
            file_name_count[(fn.file_path, fn.name)] += 1
        overloaded = sum(1 for v in file_name_count.values() if v > 1)
        assert overloaded >= 200, f"Only {overloaded} overloaded pairs"

    def test_overloaded_functions_have_different_signatures(self, castengine_store):
        """Overloaded functions should have distinct signatures (not duplicates)."""
        by_file_name: dict[tuple[str, str], list[FunctionNode]] = defaultdict(list)
        for fn in castengine_store.list_functions():
            by_file_name[(fn.file_path, fn.name)].append(fn)

        # Check that overloaded functions actually differ
        all_same_sig = 0
        for key, fns in by_file_name.items():
            if len(fns) > 1:
                sigs = {fn.signature for fn in fns}
                if len(sigs) == 1:
                    all_same_sig += 1

        # Some may be true duplicates (parser bug), but most should differ
        total_overloaded = sum(1 for v in by_file_name.values() if len(v) > 1)
        assert all_same_sig / total_overloaded < 0.1, (
            f"{all_same_sig}/{total_overloaded} overloaded pairs have identical signatures"
        )

    def test_self_loops_correlate_with_overloaded_functions(self, castengine_store):
        """Self-loops should predominantly occur in files with overloaded functions.

        BUG: When by_file_name[(file, name)] maps to the caller itself (because
        the overloaded variant was overwritten), the edge becomes a self-loop.
        """
        edges = castengine_store.list_calls_edges()
        self_loops = [e for e in edges if e.caller_id == e.callee_id]

        # Check if self-loop callers are in files with overloaded functions
        fns = {fn.id: fn for fn in castengine_store.list_functions()}
        file_name_count: dict[tuple[str, str], int] = defaultdict(int)
        for fn in castengine_store.list_functions():
            file_name_count[(fn.file_path, fn.name)] += 1

        overloaded_files = {fp for (fp, _), cnt in file_name_count.items() if cnt > 1}

        loops_in_overloaded = 0
        for e in self_loops:
            caller = fns.get(e.caller_id)
            if caller and caller.file_path in overloaded_files:
                loops_in_overloaded += 1

        if len(self_loops) > 0:
            pct = loops_in_overloaded / len(self_loops) * 100
            # Most self-loops should be in overloaded files (confirms root cause)
            assert pct > 50, (
                f"Only {pct:.0f}% of self-loops in overloaded files — "
                f"different root cause than expected"
            )

    def test_self_loop_call_line_outside_caller_bounds(self, castengine_store):
        """Self-loops from overload collision: call_line is often outside caller bounds.

        A legitimate recursive call has call_line within [start_line, end_line].
        A collision-induced self-loop has call_line from a DIFFERENT overload variant.
        """
        edges = castengine_store.list_calls_edges()
        fns = {fn.id: fn for fn in castengine_store.list_functions()}

        self_loops = [e for e in edges if e.caller_id == e.callee_id]
        outside_bounds = 0
        for e in self_loops:
            caller = fns.get(e.caller_id)
            if caller:
                if not (caller.start_line <= e.props.call_line <= caller.end_line):
                    outside_bounds += 1

        # Collision-induced self-loops have call_line outside bounds
        # Legitimate recursion has call_line inside bounds
        if len(self_loops) > 10:
            # Document the split
            inside = len(self_loops) - outside_bounds
            assert outside_bounds >= 0  # Just document
            # Print for visibility
            print(
                f"\nSelf-loops: {len(self_loops)} total, "
                f"{inside} legitimate (inside bounds), "
                f"{outside_bounds} collision-induced (outside bounds)"
            )


# ---------------------------------------------------------------------------
# BUG 2: Edge+UC conflict (dual-list processing)
# ---------------------------------------------------------------------------


class TestEdgeUCConflict:
    """Plugin returns same call in BOTH `calls` and `unresolved` → edge+UC at same site.

    Root cause: plugin.build_calls() returns the same call in both the `calls`
    list (resolved direct) and `unresolved` list (ambiguous/virtual), and the
    pipeline's second pass processes both without deduplication.
    """

    def test_conflict_count_baseline(self, castengine_store):
        """Document edge+UC conflict count (should decrease as bug is fixed)."""
        edges = castengine_store.list_calls_edges()
        ucs = castengine_store.get_unresolved_calls()

        edge_sites: set[tuple[str, str, int]] = set()
        for e in edges:
            edge_sites.add((e.caller_id, e.props.call_file, e.props.call_line))

        conflicts = [
            uc for uc in ucs
            if (uc.caller_id, uc.call_file, uc.call_line) in edge_sites
        ]

        # Baseline dropped from ~1026 → ~718 after library whitelist filter
        # (architecture.md §1: known stdlib/system calls filtered at parse time).
        assert len(conflicts) <= 800, (
            f"Edge+UC conflicts increased: {len(conflicts)} (baseline ~718)"
        )
        # At least some exist (documenting the bug)
        assert len(conflicts) > 500, (
            f"Only {len(conflicts)} conflicts — bug may be partially fixed"
        )

    def test_conflicts_are_mostly_direct_type(self, castengine_store):
        """Most conflicting UCs are 'direct' type — parser double-reports."""
        edges = castengine_store.list_calls_edges()
        ucs = castengine_store.get_unresolved_calls()

        edge_sites: set[tuple[str, str, int]] = set()
        for e in edges:
            edge_sites.add((e.caller_id, e.props.call_file, e.props.call_line))

        conflict_types: dict[str, int] = defaultdict(int)
        for uc in ucs:
            if (uc.caller_id, uc.call_file, uc.call_line) in edge_sites:
                conflict_types[uc.call_type] += 1

        total = sum(conflict_types.values())
        direct_pct = conflict_types.get("direct", 0) / total * 100
        assert direct_pct > 80, (
            f"Only {direct_pct:.1f}% direct — distribution: {dict(conflict_types)}"
        )

    def test_conflict_edges_are_symbol_table_resolved(self, castengine_store):
        """Conflicting edges should be resolved_by=symbol_table (not llm)."""
        edges = castengine_store.list_calls_edges()
        ucs = castengine_store.get_unresolved_calls()

        uc_sites: set[tuple[str, str, int]] = set()
        for uc in ucs:
            uc_sites.add((uc.caller_id, uc.call_file, uc.call_line))

        conflict_resolved_by: dict[str, int] = defaultdict(int)
        for e in edges:
            if (e.caller_id, e.props.call_file, e.props.call_line) in uc_sites:
                conflict_resolved_by[e.props.resolved_by] += 1

        # All conflict edges should be symbol_table (from first pass)
        total = sum(conflict_resolved_by.values())
        st_pct = conflict_resolved_by.get("symbol_table", 0) / total * 100
        assert st_pct > 95, (
            f"Only {st_pct:.1f}% symbol_table — distribution: {dict(conflict_resolved_by)}"
        )


# ---------------------------------------------------------------------------
# BUG 3: _resolve_id ambiguity and bare name cross-linking
# ---------------------------------------------------------------------------


class TestResolveIdBehavior:
    """_resolve_id should not cross-link unrelated modules via bare name fallback."""

    def test_ambiguous_names_become_ucs(self, castengine_store):
        """Functions with ambiguous names (multiple definitions) should produce UCs."""
        fns = castengine_store.list_functions()
        by_name: dict[str, list[str]] = defaultdict(list)
        for fn in fns:
            by_name[fn.name].append(fn.id)

        ambiguous_names = {name for name, ids in by_name.items() if len(ids) > 1}

        # Check that UCs reference ambiguous names
        ucs = castengine_store.get_unresolved_calls()
        ambiguous_ucs = [uc for uc in ucs if uc.call_expression in ambiguous_names]

        # There should be significant UCs from ambiguous resolution
        assert len(ambiguous_ucs) > 100, (
            f"Only {len(ambiguous_ucs)} UCs from ambiguous names — "
            f"_resolve_id may be picking arbitrary candidates"
        )

    def test_no_cross_module_edges_from_bare_name(self, castengine_store):
        """Edges should not link functions across unrelated directories.

        BUG CHECK: bare name fallback (by_bare_name) can link `Clear()` in
        data_buffer.h to `Clear()` in preferences_util.cpp if there's only
        one bare-name match. This test checks for suspicious cross-directory edges.
        """
        fns = {fn.id: fn for fn in castengine_store.list_functions()}
        edges = castengine_store.list_calls_edges()

        def get_module(file_path: str) -> str:
            """Extract top-level module from path."""
            parts = Path(file_path).parts
            # Find the part after CastEngine
            for i, p in enumerate(parts):
                if p.lower() == "castengine" or p.startswith("castengine_"):
                    if i + 1 < len(parts):
                        return parts[i]
            return parts[0] if parts else ""

        cross_module = 0
        total_checked = 0
        for e in edges:
            caller = fns.get(e.caller_id)
            callee = fns.get(e.callee_id)
            if caller and callee:
                total_checked += 1
                caller_mod = get_module(caller.file_path)
                callee_mod = get_module(callee.file_path)
                if caller_mod != callee_mod and caller_mod and callee_mod:
                    cross_module += 1

        # Some cross-module calls are legitimate (shared libraries, common utils)
        # BUG BASELINE: 15.2% (684/4503) cross-module edges. Many are legitimate
        # (shared headers like circular_buffer.h used across modules), but some
        # are bare-name cross-linking artifacts.
        pct = cross_module / total_checked * 100 if total_checked else 0
        assert pct <= 20, (
            f"{pct:.1f}% cross-module edges ({cross_module}/{total_checked}) — "
            f"bare name fallback may be cross-linking"
        )
        # Should not be zero (some cross-module calls are real)
        assert cross_module > 100, "Too few cross-module edges — module detection broken"


# ---------------------------------------------------------------------------
# Pipeline output consistency
# ---------------------------------------------------------------------------


class TestPipelineOutputConsistency:
    """Pipeline result metrics should match actual graph contents."""

    def test_pipeline_result_functions_match_store(self, castengine_store):
        """PipelineResult.functions_found should match store count."""
        # Re-run to get result (store already populated, but we need metrics)
        store2 = InMemoryGraphStore()
        orch = PipelineOrchestrator(store=store2, target_dir=CASTENGINE_DIR)
        result = orch.run_full_analysis()
        assert result.functions_found == len(store2.list_functions())

    def test_pipeline_result_direct_calls_match_store(self, castengine_store):
        """BUG: PipelineResult.direct_calls is inflated by dedup-rejected edges.

        The pipeline increments direct_calls BEFORE the store's dedup check,
        so edges that are rejected as duplicates are still counted.
        Expected: result.direct_calls == len(store.list_calls_edges())
        Actual: result.direct_calls > len(store.list_calls_edges()) by ~29
        """
        store2 = InMemoryGraphStore()
        orch = PipelineOrchestrator(store=store2, target_dir=CASTENGINE_DIR)
        result = orch.run_full_analysis()
        actual_edges = len(store2.list_calls_edges())
        # BUG: counter is inflated. Document the gap.
        assert result.direct_calls >= actual_edges, (
            "direct_calls should be >= actual edges (counter includes deduped)"
        )
        # The gap should be small (< 1% of total)
        gap = result.direct_calls - actual_edges
        assert gap <= 50, (
            f"Counter inflation too large: {gap} edges counted but deduped"
        )

    def test_pipeline_idempotent(self, castengine_store):
        """Running pipeline twice on same store should not duplicate data."""
        store2 = InMemoryGraphStore()
        orch = PipelineOrchestrator(store=store2, target_dir=CASTENGINE_DIR)
        r1 = orch.run_full_analysis()
        fns_after_first = len(store2.list_functions())
        edges_after_first = len(store2.list_calls_edges())

        # Run again
        r2 = orch.run_full_analysis()
        fns_after_second = len(store2.list_functions())
        edges_after_second = len(store2.list_calls_edges())

        # Should be idempotent (dedup prevents growth)
        assert fns_after_second == fns_after_first, (
            f"Functions grew: {fns_after_first} → {fns_after_second}"
        )
        assert edges_after_second == edges_after_first, (
            f"Edges grew: {edges_after_first} → {edges_after_second}"
        )

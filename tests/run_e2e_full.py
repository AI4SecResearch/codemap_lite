"""Full E2E test script — runs complete pipeline on CastEngine with archdoc source points.

Usage:
    python -m tests.run_e2e_full

This script:
1. Parses all 715 CastEngine C++ files
2. Loads 146 source points from archdoc-query output
3. Stores everything in InMemoryGraphStore
4. Starts FastAPI server on port 8000 for frontend viewing
"""
import json
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from codemap_lite.graph.neo4j_store import InMemoryGraphStore
from codemap_lite.graph.schema import (
    FunctionNode, FileNode, CallsEdgeProps, SourcePointNode, UnresolvedCallNode,
)
from codemap_lite.parsing.cpp.plugin import CppPlugin
from codemap_lite.parsing.file_scanner import FileScanner
from codemap_lite.parsing.plugin_registry import PluginRegistry
from codemap_lite.parsing.types import CallType
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator

CASTENGINE_ROOT = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")
ARCHDOC_OUTPUT = Path("/mnt/c/Task/codewiki_lite/archdoc-out-castengine-naming2")


def _resolve_source_points_to_functions(
    store: InMemoryGraphStore, entries: list[dict]
) -> int:
    """Resolve each archdoc entry to a FunctionNode.id and write it into the entry.

    Archdoc entries have (file=<repo-relative>, signature=<bare-name>, line=<decl-line>).
    FunctionNodes are keyed by id=<abs-file>:<bare-name>:<start-line>.

    Matching strategy:
      1. Build index: (abs_file_posix, name) -> list[FunctionNode]
      2. For each entry, compute abs_file = CASTENGINE_ROOT / entry["file"]
      3. Among candidates, pick the one whose start_line is closest to entry["line"].
    """
    from collections import defaultdict

    index: dict[tuple[str, str], list] = defaultdict(list)
    for fn in store._functions.values():
        key = (str(Path(fn.file_path).resolve()).replace("\\", "/"), fn.name)
        index[key].append(fn)

    resolved = 0
    for entry in entries:
        abs_path = str((CASTENGINE_ROOT / entry["file"]).resolve()).replace("\\", "/")
        sig = entry["signature"]
        candidates = index.get((abs_path, sig), [])
        if not candidates:
            entry["function_id"] = None
            continue
        target_line = entry.get("line", 0)
        best = min(candidates, key=lambda fn: abs(fn.start_line - target_line))
        entry["function_id"] = best.id
        resolved += 1

    return resolved


def load_source_points(store: InMemoryGraphStore) -> list[dict]:
    """Load source points from archdoc attack_surface.json and resolve to FunctionNodes."""
    attack_surface = json.loads((ARCHDOC_OUTPUT / "attack_surface.json").read_text())
    entries = attack_surface["entry_points"]

    resolved = _resolve_source_points_to_functions(store, entries)
    print(f"[E2E] Resolved {resolved}/{len(entries)} source points to FunctionNodes")

    for entry in entries:
        sp = SourcePointNode(
            id=entry["id"],
            entry_point_kind=entry["kind"],
            reason=entry["reason"],
            function_id=entry.get("function_id") or entry["id"],
            status="pending",
        )
        # Store as a special node (we'll add to store's internal dict)
        if not hasattr(store, '_source_points'):
            store._source_points = {}
        store._source_points[sp.id] = sp

    return entries


def run_full_analysis(store: InMemoryGraphStore) -> dict:
    """Run full analysis on all CastEngine modules."""
    registry = PluginRegistry()
    registry.register("cpp", CppPlugin())

    print(f"[E2E] Target: {CASTENGINE_ROOT}")
    print(f"[E2E] Starting full analysis...")

    start = time.time()
    orch = PipelineOrchestrator(
        target_dir=CASTENGINE_ROOT,
        store=store,
        registry=registry,
    )
    result = orch.run_full_analysis()
    elapsed = time.time() - start

    stats = {
        "files_scanned": result.files_scanned,
        "functions_found": result.functions_found,
        "direct_calls": result.direct_calls,
        "unresolved_calls": result.unresolved_calls,
        "elapsed_seconds": round(elapsed, 2),
        "errors": result.errors[:10],  # First 10 errors
    }

    print(f"[E2E] Analysis complete in {elapsed:.1f}s")
    print(f"[E2E]   Files scanned: {stats['files_scanned']}")
    print(f"[E2E]   Functions found: {stats['functions_found']}")
    print(f"[E2E]   Direct calls: {stats['direct_calls']}")
    print(f"[E2E]   Unresolved calls (GAPs): {stats['unresolved_calls']}")
    if stats['errors']:
        print(f"[E2E]   Errors (first 10): {len(result.errors)} total")
        for e in stats['errors'][:5]:
            print(f"        - {e[:100]}")

    return stats


def verify_source_point_coverage(store: InMemoryGraphStore, entries: list[dict]) -> dict:
    """Verify how many source points match parsed functions."""
    matched = 0
    unmatched = []

    # Build a lookup of function names
    func_names = set()
    func_by_file = {}
    for fid, fn in store._functions.items():
        func_names.add(fn.name)
        key = fn.file_path.replace("\\", "/")
        if key not in func_by_file:
            func_by_file[key] = set()
        func_by_file[key].add(fn.name)

    for entry in entries:
        sig = entry["signature"]
        file_path = entry["file"]
        # Try exact name match
        if sig in func_names:
            matched += 1
        else:
            unmatched.append({"id": entry["id"], "signature": sig, "file": file_path})

    coverage = {
        "total_source_points": len(entries),
        "matched": matched,
        "unmatched": len(unmatched),
        "coverage_pct": round(matched / len(entries) * 100, 1) if entries else 0,
        "unmatched_samples": unmatched[:10],
    }

    print(f"[E2E] Source point coverage: {coverage['matched']}/{coverage['total_source_points']} ({coverage['coverage_pct']}%)")
    return coverage


def verify_gap_distribution(store: InMemoryGraphStore) -> dict:
    """Check GAP distribution across modules."""
    from collections import Counter
    module_gaps = Counter()

    for uid, uc in store._unresolved_calls.items():
        # Extract module from file path
        file_path = uc.call_file
        parts = file_path.replace("\\", "/").split("/")
        module = "unknown"
        for p in parts:
            if p.startswith("castengine_"):
                module = p
                break
        if module == "unknown":
            # Try to find module from relative path
            for p in parts:
                if "cast" in p.lower() or "wfd" in p.lower() or "wifi" in p.lower():
                    module = p
                    break
        module_gaps[module] += 1

    dist = dict(module_gaps.most_common())
    print(f"[E2E] GAP distribution by module ({len(store._unresolved_calls)} total):")
    for mod, count in module_gaps.most_common():
        print(f"        {mod}: {count}")
    return dist


def start_server(store: InMemoryGraphStore, source_entries: list[dict]):
    """Start FastAPI server with populated store."""
    from codemap_lite.api.app import create_app
    import uvicorn

    app = create_app(store=store)

    # Inject source points into app state for the API
    app.state.source_points = source_entries
    app.state.analysis_stats = {
        "total_functions": len(store._functions),
        "total_calls": len(store._calls_edges),
        "total_unresolved": len(store._unresolved_calls),
        "total_source_points": len(source_entries),
        "total_files": len(store._files),
    }

    print(f"\n[E2E] Starting API server on http://localhost:8000")
    print(f"[E2E] Open http://localhost:8000/static/index.html for frontend")
    print(f"[E2E] API docs: http://localhost:8000/docs")
    print(f"[E2E] Press Ctrl+C to stop\n")

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


def main():
    store = InMemoryGraphStore()

    # Step 1: Full analysis
    print("=" * 60)
    print("[E2E] STEP 1: Full Analysis (all CastEngine modules)")
    print("=" * 60)
    stats = run_full_analysis(store)

    # Step 2: Load source points
    print("\n" + "=" * 60)
    print("[E2E] STEP 2: Load Source Points (archdoc-query)")
    print("=" * 60)
    entries = load_source_points(store)
    print(f"[E2E] Loaded {len(entries)} source points from archdoc")

    # Step 3: Verify coverage
    print("\n" + "=" * 60)
    print("[E2E] STEP 3: Source Point Coverage Verification")
    print("=" * 60)
    coverage = verify_source_point_coverage(store, entries)

    # Step 4: GAP distribution
    print("\n" + "=" * 60)
    print("[E2E] STEP 4: GAP Distribution")
    print("=" * 60)
    gap_dist = verify_gap_distribution(store)

    # Step 5: Start server
    print("\n" + "=" * 60)
    print("[E2E] STEP 5: Starting API Server")
    print("=" * 60)
    start_server(store, entries)


if __name__ == "__main__":
    main()

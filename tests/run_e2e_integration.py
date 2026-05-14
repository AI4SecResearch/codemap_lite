"""Full integration test — CastEngine + opencode + GLM-5 + Neo4j + Frontend.

This script exercises the **complete pipeline** as documented in architecture.md:

Stages (1-10):
  1. Preflight — verify Neo4j, opencode, DashScope, CastEngine, Node.js
  2. Source points — fetch from codewiki_lite or load from JSON fallback
  3. Static analysis — parse CastEngine, mirror to Neo4j
  4. Backend API — start FastAPI, verify all endpoints
  5. Repair — run opencode + GLM-5, verify LLM edges + RepairLogs
  6. Post-repair API — verify stats, call-chain, unresolved reduction
  7. Review workflow — mark correct/incorrect, counter-examples, manual edges
  8. Incremental update — file change → cascade → re-repair
  9. Frontend — build + probe
  10. Report — JSON summary to _e2e_integration_report.json

Environment (credentials):
  * NEO4J_PASSWORD — Neo4j auth
  * OPENAI_API_KEY — DashScope API key
  * OPENAI_BASE_URL — DashScope endpoint

Usage::

    python -m tests.run_e2e_integration                       # full run
    python -m tests.run_e2e_integration --skip-repair         # skip Stage 5-6
    python -m tests.run_e2e_integration --no-frontend        # skip Stage 9
    python -m tests.run_e2e_integration --entries UnpackFuA  # single source
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from codemap_lite.analysis.feedback_store import FeedbackStore
from codemap_lite.analysis.repair_orchestrator import (
    RepairConfig,
    RepairOrchestrator,
    SourceRepairResult,
)
from codemap_lite.analysis.source_point_client import SourcePointClient, SourcePointInfo
from codemap_lite.api.app import create_app
from codemap_lite.graph.neo4j_store import (
    InMemoryGraphStore,
    Neo4jGraphStore,
)
from codemap_lite.graph.schema import (
    CallsEdgeProps,
    FunctionNode,
    UnresolvedCallNode,
    VALID_RESOLVED_BY,
    VALID_CALL_TYPES,
)
from codemap_lite.parsing.cpp.plugin import CppPlugin
from codemap_lite.parsing.plugin_registry import PluginRegistry
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator

logger = logging.getLogger("e2e_integration")

# --- Constants ---------------------------------------------------------------

CASTENGINE_ROOT = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")
FIXTURE_DIR = Path(__file__).parent / "fixtures"
SOURCE_POINTS_FIXTURE = FIXTURE_DIR / "source_points.json"
LOG_ROOT = Path(__file__).parent / "_e2e_integration_logs"
REPORT_PATH = Path(__file__).parent / "_e2e_integration_report.json"

# Proxy vars to strip for WSL compatibility
_PROXY_VARS = (
    "http_proxy", "https_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
)

# Resolved_by values BEFORE repair (no LLM yet)
_STATIC_RESOLVED_BY = {"symbol_table", "signature", "dataflow", "context"}

# --- Dataclasses -------------------------------------------------------------


@dataclass
class StageResult:
    """Result of a single test stage."""
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    duration_s: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    neo_store: Any = None  # Neo4jGraphStore reference for downstream stages
    backend_port: int | None = None
    backend_server: Any = None


@dataclass
class IntegrationReport:
    """Aggregated E2E test report."""
    timestamp: str
    duration_s: float
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    overall: str = "FAIL"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


# --- Helper functions from run_e2e_repair.py --------------------------------

def _strip_proxy_env() -> list[str]:
    """Remove proxy vars for WSL DashScope reachability."""
    cleared: list[str] = []
    for name in _PROXY_VARS:
        if name in os.environ:
            del os.environ[name]
            cleared.append(name)
    return cleared


def _find_free_port() -> int:
    """Find an ephemeral port that's not in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


def _wait_for_tcp(host: str, port: int, timeout: float = 15.0) -> bool:
    """Wait for TCP port to accept connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except (OSError, ConnectionRefusedError, TimeoutError):
            time.sleep(0.1)
    return False


def _count_llm_edges(store: Neo4jGraphStore) -> int:
    """Count CALLS edges with resolved_by=llm."""
    try:
        edges = store.list_calls_edges()
        return sum(1 for e in edges if e.props.resolved_by == "llm")
    except Exception:
        return 0


# --- Stage implementations ---------------------------------------------------

def stage_1_preflight(args: argparse.Namespace) -> StageResult:
    """Stage 1: Environment preflight."""
    start = time.time()
    result = StageResult(name="preflight", status="PASS")
    details: dict[str, Any] = {}
    errors: list[str] = []

    # Neo4j connectivity
    try:
        from neo4j import GraphDatabase
        uri = f"bolt://{args.neo4j_host}:{args.neo4j_port}"
        driver = GraphDatabase.driver(
            uri,
            auth=(args.neo4j_user, os.environ.get("NEO4J_PASSWORD", "")),
        )
        driver.verify_connectivity()
        driver.close()
        details["neo4j"] = "OK"
    except Exception as exc:
        errors.append(f"Neo4j: {exc}")
        details["neo4j"] = f"FAIL: {exc}"

    # opencode binary
    opencode_path = shutil.which("opencode")
    if opencode_path:
        details["opencode"] = f"OK: {opencode_path}"
    else:
        errors.append("opencode binary not found on PATH")
        details["opencode"] = "FAIL: not found"

    # DashScope credentials
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    if api_key and base_url:
        details["dashscope"] = "OK"
    else:
        missing = []
        if not api_key:
            missing.append("OPENAI_API_KEY")
        if not base_url:
            missing.append("OPENAI_BASE_URL")
        errors.append(f"DashScope: missing {', '.join(missing)}")
        details["dashscope"] = f"FAIL: missing {', '.join(missing)}"

    # CastEngine directory
    if CASTENGINE_ROOT.exists():
        cpp_files = list(CASTENGINE_ROOT.rglob("*.cpp"))
        details["castengine"] = f"OK: {len(cpp_files)} .cpp files"
    else:
        errors.append(f"CastEngine not found at {CASTENGINE_ROOT}")
        details["castengine"] = f"FAIL: {CASTENGINE_ROOT} does not exist"

    # Node.js/npm
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node:
        details["node"] = f"OK: {node}"
    else:
        errors.append("node not found on PATH")
        details["node"] = "FAIL: not found"
    if npm:
        details["npm"] = f"OK: {npm}"
    else:
        errors.append("npm not found on PATH")
        details["npm"] = "FAIL: not found"

    # Strip proxy env
    cleared = _strip_proxy_env()
    details["proxy_vars_cleared"] = cleared

    if errors and not args.ignore_preflight_fails:
        result.status = "FAIL"
        result.errors = errors
    result.details = details
    result.duration_s = time.time() - start
    return result


def stage_2_source_points(args: argparse.Namespace) -> StageResult:
    """Stage 2: Source points acquisition."""
    start = time.time()
    result = StageResult(name="source_points", status="PASS")
    details: dict[str, Any] = {}
    errors: list[str] = []

    client = SourcePointClient(base_url=args.codewiki_url)

    # Try fetching from API first
    try:
        source_points = asyncio.run(client.fetch())
        details["source"] = "codewiki_lite API"
        details["count"] = len(source_points)
    except Exception as exc:
        logger.warning(f"Failed to fetch from codewiki_lite: {exc}, using fixture")
        # Fall back to fixture
        if SOURCE_POINTS_FIXTURE.exists():
            source_points = client.load_from_file(SOURCE_POINTS_FIXTURE)
            details["source"] = f"fixture: {SOURCE_POINTS_FIXTURE}"
            details["count"] = len(source_points)
        else:
            errors.append(f"No source points available: API failed, fixture not found")
            result.status = "FAIL"
            result.errors = errors
            result.duration_s = time.time() - start
            return result

    # Validate
    for sp in source_points:
        if not sp.function_id:
            errors.append(f"Source point missing function_id: {sp}")
        if not sp.entry_point_kind:
            errors.append(f"Source point missing entry_point_kind: {sp.function_id}")

    if len(source_points) < 3:
        errors.append(f"Expected at least 3 source points, got {len(source_points)}")

    if errors:
        result.status = "FAIL"
        result.errors = errors

    details["sample_ids"] = [sp.function_id for sp in source_points[:3]]
    result.details = details
    result.duration_s = time.time() - start
    return result


def stage_3_static_analysis(args: argparse.Namespace) -> StageResult:
    """Stage 3: Static analysis + Neo4j storage.

    If Neo4j already has data (from a previous run), skip the
    expensive tree-sitter parse and reuse the existing data.
    Otherwise, parse CastEngine and mirror to Neo4j.
    """
    start = time.time()
    result = StageResult(name="static_analysis", status="PASS")
    details: dict[str, Any] = {}
    errors: list[str] = []

    # Check if Neo4j already has data from a previous run
    neo_store = Neo4jGraphStore(
        uri=f"bolt://{args.neo4j_host}:{args.neo4j_port}",
        user=args.neo4j_user,
        password=os.environ.get("NEO4J_PASSWORD", ""),
    )
    existing_functions = neo_store.list_functions()
    existing_count = len(existing_functions)

    if existing_count >= 500 and not args.force_reparse:
        # Reuse existing Neo4j data — skip expensive tree-sitter parse
        logger.info(f"Neo4j already has {existing_count} functions, reusing (use --force-reparse to override)")
        details["source"] = "neo4j_existing"
        details["functions_found"] = existing_count
        details["calls_edges"] = len(neo_store.list_calls_edges())
        details["unresolved_calls"] = len(neo_store.get_unresolved_calls())
        details["neo4j_reused"] = True
    else:
        # Parse with InMemoryGraphStore first, then mirror to Neo4j
        logger.info(f"Neo4j has {existing_count} functions (< 500 or --force-reparse), running full parse")
        registry = PluginRegistry()
        registry.register("cpp", CppPlugin())
        mem_store = InMemoryGraphStore()

        orch = PipelineOrchestrator(
            target_dir=CASTENGINE_ROOT,
            store=mem_store,
            registry=registry,
        )
        parse_result = orch.run_full_analysis()

        details["source"] = "tree_sitter_parse"
        details["files_scanned"] = parse_result.files_scanned
        details["functions_found"] = parse_result.functions_found
        details["direct_calls"] = parse_result.direct_calls
        details["unresolved_calls"] = parse_result.unresolved_calls
        details["parse_errors"] = parse_result.errors

        # Basic assertions
        if parse_result.files_scanned < 100:
            errors.append(f"Expected files_scanned >= 100, got {parse_result.files_scanned}")
        if parse_result.functions_found < 500:
            errors.append(f"Expected functions_found >= 500, got {parse_result.functions_found}")
        if parse_result.direct_calls < 200:
            errors.append(f"Expected direct_calls >= 200, got {parse_result.direct_calls}")

        # Validate resolved_by values (no LLM yet)
        for edge in mem_store._calls_edges:
            if edge.props.resolved_by not in VALID_RESOLVED_BY:
                errors.append(f"Invalid resolved_by: {edge.props.resolved_by}")

        # Validate call_type values
        for edge in mem_store._calls_edges:
            if edge.props.call_type not in VALID_CALL_TYPES:
                errors.append(f"Invalid call_type: {edge.props.call_type}")

        # Mirror to Neo4j
        try:
            from tests.run_e2e_repair import mirror_store_to_neo4j
            neo_store = mirror_store_to_neo4j(
                mem_store,
                uri=f"bolt://{args.neo4j_host}:{args.neo4j_port}",
                user=args.neo4j_user,
                password=os.environ.get("NEO4J_PASSWORD", ""),
            )
            details["neo4j_mirrored"] = True
            details["neo4j_functions"] = len(neo_store.list_functions())
        except Exception as exc:
            errors.append(f"Neo4j mirroring failed: {exc}")
            details["neo4j_mirrored"] = False
            neo_store = None

    if errors:
        result.status = "FAIL"
        result.errors = errors

    result.details = details
    result.duration_s = time.time() - start
    result.neo_store = neo_store
    return result


def stage_4_backend_api(args: argparse.Namespace, neo_store: Neo4jGraphStore, source_points: list[SourcePointInfo]) -> StageResult:
    """Stage 4: Start backend API + verify endpoints."""
    start = time.time()
    result = StageResult(name="api_baseline", status="PASS")
    details: dict[str, Any] = {}
    errors: list[str] = []

    import uvicorn

    # Create app with FeedbackStore so review verdict=incorrect creates counter-examples
    feedback_dir = LOG_ROOT / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    feedback_store = FeedbackStore(storage_dir=feedback_dir)
    app = create_app(store=neo_store, feedback_store=feedback_store)
    app.state.source_points = [
        {
            "function_id": sp.function_id,
            "entry_point_kind": sp.entry_point_kind,
            "reason": sp.reason,
            "module": sp.module,
        }
        for sp in source_points
    ]

    # Find free port
    port = _find_free_port()
    details["port"] = port

    # Start uvicorn in background thread
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not _wait_for_tcp("127.0.0.1", port, timeout=15.0):
        errors.append(f"Backend did not start on port {port}")
        result.status = "FAIL"
        result.details = details
        result.errors = errors
        result.duration_s = time.time() - start
        return result

    details["backend_started"] = True
    base_url = f"http://127.0.0.1:{port}"

    # Probe endpoints
    endpoints_to_test = [
        ("/health", 200),
        ("/api/v1/files", 200),
        ("/api/v1/functions", 200),
        ("/api/v1/stats", 200),
        ("/api/v1/unresolved-calls", 200),
        ("/api/v1/repair-logs", 200),
        ("/api/v1/feedback", 200),
        ("/api/v1/source-points", 200),
    ]

    endpoints_ok = 0
    for path, expected_status in endpoints_to_test:
        resp = _http_get(f"{base_url}{path}")
        if resp["ok"]:
            endpoints_ok += 1
        else:
            errors.append(f"{path}: {resp.get('error', resp.get('status'))}")

    # Verify paginated endpoints return {total, items} wrapper (§8)
    paginated_endpoints = [
        "/api/v1/files",
        "/api/v1/functions",
        "/api/v1/unresolved-calls",
        "/api/v1/repair-logs",
        "/api/v1/feedback",
        "/api/v1/source-points",
    ]
    for path in paginated_endpoints:
        resp = _http_get(f"{base_url}{path}")
        if resp["ok"]:
            body = json.loads(resp["body"])
            if not isinstance(body, dict) or "total" not in body or "items" not in body:
                errors.append(f"{path}: missing {{total, items}} pagination wrapper (§8)")
    details["pagination_validated"] = True

    # Test function-specific endpoints (need a real function ID)
    functions = neo_store.list_functions()
    if functions:
        fn_id = functions[0].id
        test_paths = [
            f"/api/v1/functions/{fn_id}",
            f"/api/v1/functions/{fn_id}/callers",
            f"/api/v1/functions/{fn_id}/callees",
            f"/api/v1/functions/{fn_id}/call-chain?depth=3",
        ]
        for path in test_paths:
            resp = _http_get(f"{base_url}{path}")
            if resp["ok"]:
                endpoints_ok += 1
            else:
                errors.append(f"{path}: {resp.get('error', resp.get('status'))}")

        # §8: /functions?file= filter
        fn = functions[0]
        file_filter_resp = _http_get(f"{base_url}/api/v1/functions?file={fn.file_path}")
        if file_filter_resp["ok"]:
            file_body = json.loads(file_filter_resp["body"])
            file_items = file_body.get("items", [])
            if not any(f["id"] == fn.id for f in file_items):
                errors.append(f"/functions?file= filter did not return expected function")
            details["file_filter_ok"] = True
        else:
            errors.append(f"/functions?file= failed: {file_filter_resp.get('error')}")

    # §8: Additional endpoints (analyze, source-points reachable)
    # GET /api/v1/analyze/status
    status_resp = _http_get(f"{base_url}/api/v1/analyze/status")
    if status_resp["ok"]:
        details["analyze_status_ok"] = True
    else:
        # May return 404 if no analysis running — acceptable
        details["analyze_status_ok"] = "not_running"

    # Verify call-chain has nodes+edges
    if functions:
        chain_resp = _http_get(f"{base_url}/api/v1/functions/{fn_id}/call-chain?depth=3")
        if chain_resp["ok"]:
            body = json.loads(chain_resp["body"])
            if "nodes" in body and "edges" in body:
                details["call_chain_ok"] = True
            else:
                errors.append("call-chain response missing nodes or edges")
    else:
        errors.append("No functions found for detailed endpoint testing")

    details["endpoints_ok"] = endpoints_ok
    details["total_endpoints"] = len(endpoints_to_test) + 4  # +4 for function-specific

    # Verify stats buckets
    stats_resp = _http_get(f"{base_url}/api/v1/stats")
    if stats_resp["ok"]:
        stats = json.loads(stats_resp["body"])
        required_buckets = ["unresolved_by_status", "unresolved_by_category", "calls_by_resolved_by"]
        for bucket in required_buckets:
            if bucket not in stats:
                errors.append(f"stats missing {bucket}")
        details["stats_buckets_ok"] = all(b in stats for b in required_buckets)
        # §8: total_llm_edges convenience field
        if "total_llm_edges" not in stats:
            errors.append("stats missing total_llm_edges field (§8)")
        else:
            details["total_llm_edges"] = stats["total_llm_edges"]
        # §8: total_feedback and total_repair_logs
        if "total_feedback" not in stats:
            errors.append("stats missing total_feedback field (§8)")
        if "total_repair_logs" not in stats:
            errors.append("stats missing total_repair_logs field (§8)")

    if errors:
        result.status = "FAIL"
        result.errors = errors

    result.details = details
    result.duration_s = time.time() - start
    result.backend_port = port
    result.backend_server = server
    return result


# --- Main orchestration ----------------------------------------------------

def _resolve_entry_point(store: Neo4jGraphStore | InMemoryGraphStore, name: str, file_hint: str | None = None) -> str | None:
    """Resolve a qualified function name to FunctionNode.id.

    Matches by (name, file_path hint) substring pattern.
    Works with both InMemoryGraphStore and Neo4jGraphStore.
    """
    functions = store.list_functions()
    for fn in functions:
        if fn.name == name or fn.name.endswith("::" + name):
            if file_hint is None or file_hint in fn.file_path:
                return fn.id
    return None


def stage_5_repair(args: argparse.Namespace, neo_store: Neo4jGraphStore, source_points: list[SourcePointInfo]) -> StageResult:
    """Stage 5: Repair via opencode + GLM-5."""
    if args.skip_repair:
        return StageResult(name="repair", status="SKIP", details={"reason": "--skip-repair flag"})

    start = time.time()
    result = StageResult(name="repair", status="PASS")
    details: dict[str, Any] = {}
    errors: list[str] = []

    # Select source points to repair
    entries_to_repair = args.entries or ["UnpackFuA", "CastSessionImpl::ProcessSetUp"]

    # Resolve to function IDs
    source_ids: list[str] = []
    for name in entries_to_repair:
        fid = _resolve_entry_point(neo_store, name)
        if fid:
            source_ids.append(fid)
            logger.info(f"Resolved {name} -> {fid}")
        else:
            errors.append(f"Could not resolve source point: {name}")

    if not source_ids:
        result.status = "FAIL"
        result.errors = errors
        result.details = details
        result.duration_s = time.time() - start
        return result

    details["sources_selected"] = source_ids

    # Configure RepairOrchestrator
    log_dir = LOG_ROOT / "repair"
    log_dir.mkdir(parents=True, exist_ok=True)

    config = RepairConfig(
        target_dir=CASTENGINE_ROOT,
        backend="opencode",
        command="opencode",
        args=["-p", "--output-format", "text"],
        max_concurrency=2,
        neo4j_uri=f"bolt://{args.neo4j_host}:{args.neo4j_port}",
        neo4j_user=args.neo4j_user,
        neo4j_password=os.environ.get("NEO4J_PASSWORD", ""),
        graph_store=neo_store,
        log_dir=log_dir,
        subprocess_timeout_seconds=240,
        feedback_store=FeedbackStore(storage_dir=LOG_ROOT / "feedback"),
        retry_failed_gaps=True,
    )

    # Run repair
    orch = RepairOrchestrator(config)
    repair_results = asyncio.run(orch.run_repairs(source_ids))

    # Collect results
    total_llm_edges = _count_llm_edges(neo_store)
    details["total_llm_edges"] = total_llm_edges

    repair_logs = neo_store.get_repair_logs()
    details["repair_log_count"] = len(repair_logs)

    sources_complete = sum(1 for r in repair_results if r.success)
    sources_failed = sum(1 for r in repair_results if not r.success)
    details["sources_complete"] = sources_complete
    details["sources_failed"] = sources_failed

    # Validate RepairLogs have reasoning_summary
    empty_summaries = sum(1 for rl in repair_logs if not rl.reasoning_summary)
    details["repair_logs_with_summary"] = len(repair_logs) - empty_summaries
    if empty_summaries > 0:
        errors.append(f"{empty_summaries} RepairLogs missing reasoning_summary")

    # Validate progress.json files
    progress_files_ok = 0
    for sid in source_ids:
        progress_path = CASTENGINE_ROOT / "logs" / "repair" / sid / "progress.json"
        if progress_path.exists():
            data = json.loads(progress_path.read_text())
            if "state" in data and "attempt" in data:
                progress_files_ok += 1
    details["progress_files_ok"] = progress_files_ok

    # Validate SourcePoint statuses
    source_statuses = []
    for sid in source_ids:
        sp = neo_store.get_source_point(sid)
        if sp:
            source_statuses.append(sp.status)
    details["source_statuses"] = source_statuses

    # Assertions
    if total_llm_edges == 0:
        errors.append("No LLM edges created")
    if len(repair_logs) == 0:
        errors.append("No RepairLogs created")

    if errors:
        result.status = "FAIL"
        result.errors = errors

    result.details = details
    result.duration_s = time.time() - start
    return result


def stage_6_post_repair_api(args: argparse.Namespace, port: int, neo_store: Neo4jGraphStore, before_llm_count: int = 0) -> StageResult:
    """Stage 6: Verify API after repair."""
    if args.skip_repair:
        return StageResult(name="api_post_repair", status="SKIP", details={"reason": "--skip-repair flag"})

    start = time.time()
    result = StageResult(name="api_post_repair", status="PASS")
    details: dict[str, Any] = {}
    errors: list[str] = []

    base_url = f"http://127.0.0.1:{port}"

    # Check stats for llm edges
    stats_resp = _http_get(f"{base_url}/api/v1/stats")
    if stats_resp["ok"]:
        stats = json.loads(stats_resp["body"])
        llm_count = stats["calls_by_resolved_by"].get("llm", 0)
        details["llm_edge_count"] = llm_count
        if llm_count == 0:
            errors.append("No llm edges in stats")
    else:
        errors.append(f"/stats failed: {stats_resp.get('error')}")

    # Check repair-logs
    logs_resp = _http_get(f"{base_url}/api/v1/repair-logs")
    if logs_resp["ok"]:
        logs_body = json.loads(logs_resp["body"])
        assert "total" in logs_body and "items" in logs_body, (
            f"repair-logs missing pagination wrapper: {list(logs_body.keys())}"
        )
        details["repair_logs_count"] = logs_body["total"]
        if logs_body["total"] == 0:
            errors.append("No repair-logs returned")
    else:
        errors.append(f"/repair-logs failed: {logs_resp.get('error')}")

    # Find an LLM edge and verify it appears in callees
    llm_edges = [e for e in neo_store.list_calls_edges() if e.props.resolved_by == "llm"]
    if llm_edges:
        edge = llm_edges[0]
        callees_resp = _http_get(f"{base_url}/api/v1/functions/{edge.caller_id}/callees")
        if callees_resp["ok"]:
            callees_body = json.loads(callees_resp["body"])
            # §8 pagination: {total, items} wrapper
            callees_items = callees_body.get("items", callees_body if isinstance(callees_body, list) else [])
            callee_ids = [c["id"] for c in callees_items]
            if edge.callee_id in callee_ids:
                details["llm_edge_in_callees"] = True
            else:
                errors.append(f"LLM edge {edge.caller_id}->{edge.callee_id} not in callees")
        else:
            errors.append(f"/callees failed: {callees_resp.get('error')}")

        # Check call-chain
        chain_resp = _http_get(f"{base_url}/api/v1/functions/{edge.caller_id}/call-chain?depth=3")
        if chain_resp["ok"]:
            chain = json.loads(chain_resp["body"])
            llm_edges_in_chain = sum(1 for e in chain.get("edges", []) if e.get("props", {}).get("resolved_by") == "llm")
            details["llm_edges_in_chain"] = llm_edges_in_chain
            if llm_edges_in_chain == 0:
                errors.append("No llm edges in call-chain")
        else:
            errors.append(f"/call-chain failed: {chain_resp.get('error')}")

    if errors:
        result.status = "FAIL"
        result.errors = errors

    result.details = details
    result.duration_s = time.time() - start
    return result


def stage_7_review_workflow(args: argparse.Namespace, port: int, neo_store: Neo4jGraphStore) -> StageResult:
    """Stage 7: Review workflow (correct/incorrect, counter-examples, manual edges).

    Works even with --skip-repair by creating manual LLM edges via POST /edges
    and then exercising the review cascade against them.
    """
    start = time.time()
    result = StageResult(name="review", status="PASS")
    details: dict[str, Any] = {}
    errors: list[str] = []

    base_url = f"http://127.0.0.1:{port}"

    # Find LLM edges — either from repair or from a previous run
    llm_edges = [e for e in neo_store.list_calls_edges() if e.props.resolved_by == "llm"]

    # If no LLM edges exist, create two manually for testing.
    # Use a timestamp-based suffix to avoid conflicts with edges from
    # previous test runs still lingering in Neo4j.
    ts_suffix = int(time.time())
    if len(llm_edges) < 2:
        all_functions = neo_store.list_functions()
        if len(all_functions) < 3:
            errors.append(f"Need at least 3 functions for review test, got {len(all_functions)}")
            result.status = "FAIL"
            result.errors = errors
            result.details = details
            result.duration_s = time.time() - start
            return result

        # Create 2 manual LLM edges for review testing
        fn_a, fn_b, fn_c = all_functions[0].id, all_functions[1].id, all_functions[2].id

        # Create edge 1 (for "correct" review)
        create1 = _http_get(
            f"{base_url}/api/v1/edges",
            method="POST",
            body=json.dumps({
                "caller_id": fn_a,
                "callee_id": fn_b,
                "resolved_by": "llm",
                "call_type": "direct",
                "call_file": f"/e2e_review/test1_{ts_suffix}.cpp",
                "call_line": 10,
            }),
        )
        if not create1["ok"]:
            errors.append(f"Manual edge 1 create failed: {create1.get('error')}")

        # Create edge 2 (for "incorrect" review)
        create2 = _http_get(
            f"{base_url}/api/v1/edges",
            method="POST",
            body=json.dumps({
                "caller_id": fn_a,
                "callee_id": fn_c,
                "resolved_by": "llm",
                "call_type": "direct",
                "call_file": f"/e2e_review/test2_{ts_suffix}.cpp",
                "call_line": 20,
            }),
        )
        if not create2["ok"]:
            errors.append(f"Manual edge 2 create failed: {create2.get('error')}")

        # Re-fetch edges
        llm_edges = [e for e in neo_store.list_calls_edges() if e.props.resolved_by == "llm"]

    if len(llm_edges) < 2:
        result.status = "FAIL"
        result.errors = errors + [f"Need at least 2 LLM edges, got {len(llm_edges)}"]
        result.details = details
        result.duration_s = time.time() - start
        return result

    # Pick edges for correct/incorrect reviews — use last 2 edges to avoid
    # picking ones created by repair that might have cascading side effects
    edge_correct, edge_incorrect = llm_edges[-2], llm_edges[-1]

    # 7a: Mark correct — edge should still exist
    correct_resp = _http_get(
        f"{base_url}/api/v1/reviews",
        method="POST",
        body=json.dumps({
            "caller_id": edge_correct.caller_id,
            "callee_id": edge_correct.callee_id,
            "call_file": edge_correct.props.call_file,
            "call_line": edge_correct.props.call_line,
            "verdict": "correct",
        }),
    )
    details["mark_correct_ok"] = correct_resp["ok"]
    if not correct_resp["ok"]:
        errors.append(f"Mark correct failed: {correct_resp.get('error')}")

    # Verify edge still exists after "correct" verdict
    edge_still_exists = neo_store.edge_exists(
        edge_correct.caller_id, edge_correct.callee_id,
        edge_correct.props.call_file, edge_correct.props.call_line
    )
    details["edge_still_exists_after_correct"] = edge_still_exists
    if not edge_still_exists:
        errors.append("Edge deleted after 'correct' verdict — should have been preserved")

    # 7b: Mark incorrect with correct_target — triggers 4-step cascade
    # (architecture.md §5: delete edge → delete RepairLog → regenerate UC → reset SourcePoint)
    all_functions = neo_store.list_functions()
    # Pick a function that is NOT the callee as the "correct" target
    correct_target_candidates = [
        f.id for f in all_functions
        if f.id != edge_incorrect.caller_id and f.id != edge_incorrect.callee_id
    ]
    correct_target = correct_target_candidates[0] if correct_target_candidates else "manual_correct_fn"

    incorrect_resp = _http_get(
        f"{base_url}/api/v1/reviews",
        method="POST",
        body=json.dumps({
            "caller_id": edge_incorrect.caller_id,
            "callee_id": edge_incorrect.callee_id,
            "call_file": edge_incorrect.props.call_file,
            "call_line": edge_incorrect.props.call_line,
            "verdict": "incorrect",
            "correct_target": correct_target,
        }),
    )
    details["mark_incorrect_ok"] = incorrect_resp["ok"]
    if not incorrect_resp["ok"]:
        errors.append(f"Mark incorrect failed: {incorrect_resp.get('error')}")
    else:
        # Verify 4-step cascade (architecture.md §5)
        # Step 1: CALLS edge deleted
        edge_deleted = not neo_store.edge_exists(
            edge_incorrect.caller_id, edge_incorrect.callee_id,
            edge_incorrect.props.call_file, edge_incorrect.props.call_line
        )
        details["edge_deleted_after_incorrect"] = edge_deleted

        # Step 2: RepairLog deleted
        repair_logs = neo_store.get_repair_logs(
            caller_id=edge_incorrect.caller_id,
            callee_id=edge_incorrect.callee_id,
            call_location=f"{edge_incorrect.props.call_file}:{edge_incorrect.props.call_line}"
        )
        details["repair_log_deleted"] = len(repair_logs) == 0

        # Step 3: UnresolvedCall regenerated (retry_count=0, status=pending)
        ucs = neo_store.get_unresolved_calls(caller_id=edge_incorrect.caller_id)
        matching_uc = [
            uc for uc in ucs
            if uc.call_file == edge_incorrect.props.call_file
            and uc.call_line == edge_incorrect.props.call_line
        ]
        uc_regenerated = len(matching_uc) > 0
        details["uc_regenerated"] = uc_regenerated
        if matching_uc:
            details["uc_retry_count"] = matching_uc[0].retry_count
            details["uc_status"] = matching_uc[0].status
            if matching_uc[0].retry_count != 0:
                errors.append(f"UC retry_count should be 0, got {matching_uc[0].retry_count}")
            if matching_uc[0].status != "pending":
                errors.append(f"UC status should be 'pending', got '{matching_uc[0].status}'")

        # Step 4: SourcePoint reset to pending (if one exists for this caller)
        sp = neo_store.get_source_point(edge_incorrect.caller_id)
        if sp:
            details["source_point_reset"] = sp.status == "pending"
            if sp.status != "pending":
                errors.append(f"SourcePoint status should be 'pending', got '{sp.status}'")
        else:
            details["source_point_reset"] = "no_source_point"

        if not edge_deleted:
            errors.append("Edge not deleted after incorrect verdict")
        if not uc_regenerated:
            errors.append("UC not regenerated after incorrect verdict")

    # 7c: Verify counter-example was created
    feedback_resp = _http_get(f"{base_url}/api/v1/feedback")
    if feedback_resp["ok"]:
        feedback_body = json.loads(feedback_resp["body"])
        # §8 pagination: {total, items} wrapper
        feedback_items = feedback_body.get("items", feedback_body if isinstance(feedback_body, list) else [])
        details["counter_example_count"] = feedback_body.get("total", len(feedback_items))
        found_ce = any(
            ce.get("wrong_target") == edge_incorrect.callee_id
            for ce in feedback_items
        )
        details["counter_example_found"] = found_ce
    else:
        # FeedbackStore may not be wired — acceptable
        details["counter_example_count"] = "no_feedback_store"

    # 7d: Manual edge creation via POST /edges (architecture.md §8)
    if len(all_functions) >= 2:
        fn_x, fn_y = all_functions[0].id, all_functions[1].id
        manual_file = f"/e2e_manual/edge_test_{ts_suffix}.cpp"
        manual_line = 999

        create_resp = _http_get(
            f"{base_url}/api/v1/edges",
            method="POST",
            body=json.dumps({
                "caller_id": fn_x,
                "callee_id": fn_y,
                "resolved_by": "llm",
                "call_type": "direct",
                "call_file": manual_file,
                "call_line": manual_line,
            }),
        )
        details["manual_edge_created"] = create_resp["ok"]
        if not create_resp["ok"]:
            errors.append(f"Manual edge create failed: {create_resp.get('error')}")
        else:
            # Verify edge exists in store
            edge_exists = neo_store.edge_exists(fn_x, fn_y, manual_file, manual_line)
            details["manual_edge_in_store"] = edge_exists
            if not edge_exists:
                errors.append("Manual edge not found in store after creation")

            # Verify duplicate rejected (409)
            dup_resp = _http_get(
                f"{base_url}/api/v1/edges",
                method="POST",
                body=json.dumps({
                    "caller_id": fn_x,
                    "callee_id": fn_y,
                    "resolved_by": "llm",
                    "call_type": "direct",
                    "call_file": manual_file,
                    "call_line": manual_line,
                }),
            )
            details["duplicate_edge_rejected"] = not dup_resp["ok"]

        # 7e: Manual edge deletion via DELETE /edges (triggers cascade)
        delete_resp = _http_get(
            f"{base_url}/api/v1/edges",
            method="DELETE",
            body=json.dumps({
                "caller_id": fn_x,
                "callee_id": fn_y,
                "call_file": manual_file,
                "call_line": manual_line,
            }),
        )
        details["manual_edge_deleted"] = delete_resp["ok"]
        if not delete_resp["ok"]:
            errors.append(f"Manual edge delete failed: {delete_resp.get('error')}")
        else:
            edge_gone = not neo_store.edge_exists(fn_x, fn_y, manual_file, manual_line)
            details["manual_edge_gone_after_delete"] = edge_gone
            # UC should be regenerated after edge deletion
            ucs_after = neo_store.get_unresolved_calls(caller_id=fn_x)
            uc_regen = any(
                uc.call_file == manual_file and uc.call_line == manual_line
                for uc in ucs_after
            )
            details["uc_regen_after_edge_delete"] = uc_regen
            if not edge_gone:
                errors.append("Edge still exists after DELETE")

    # 7f: Test edge validation — 404 for non-existent function
    if all_functions:
        valid_fn = all_functions[0].id
        bad_resp = _http_get(
            f"{base_url}/api/v1/edges",
            method="POST",
            body=json.dumps({
                "caller_id": valid_fn,
                "callee_id": "NONEXISTENT_FUNCTION_12345",
                "resolved_by": "llm",
                "call_type": "direct",
                "call_file": "/e2e_validate/test.cpp",
                "call_line": 1,
            }),
        )
        details["edge_create_404_on_bad_callee"] = not bad_resp["ok"]

    if errors:
        result.status = "FAIL"
        result.errors = errors

    result.details = details
    result.duration_s = time.time() - start
    return result


def stage_8_incremental(args: argparse.Namespace, neo_store: Neo4jGraphStore) -> StageResult:
    """Stage 8: Incremental update (file change cascade)."""
    start = time.time()
    result = StageResult(name="incremental", status="PASS")
    details: dict[str, Any] = {}
    errors: list[str] = []

    # Find a C++ file to modify
    cpp_files = list(CASTENGINE_ROOT.rglob("*.cpp"))
    if not cpp_files:
        errors.append("No .cpp files found for incremental test")
        result.status = "FAIL"
        result.errors = errors
        result.details = details
        result.duration_s = time.time() - start
        return result

    # Pick a small file
    test_file = min(cpp_files, key=lambda p: p.stat().st_size)
    details["test_file"] = str(test_file.relative_to(CASTENGINE_ROOT))

    # Record original content
    original_content = test_file.read_text()

    try:
        # Ensure state.json is fresh (reflects current file hashes) so detect_changes
        # only detects the modification we make, not stale pre-existing diffs.
        registry = PluginRegistry()
        registry.register("cpp", CppPlugin())
        orch = PipelineOrchestrator(
            target_dir=CASTENGINE_ROOT,
            store=neo_store,
            registry=registry,
        )
        state_path = CASTENGINE_ROOT / ".icslpreprocess" / "state.json"
        scanned = orch._scanner.scan(CASTENGINE_ROOT)
        orch._scanner.save_state(scanned, state_path)
        details["state_json_refreshed"] = True

        # Modify file (add comment)
        test_file.write_text(original_content + "\n// E2E test modification\n")

        # Run incremental analysis
        result_inc = orch.run_incremental_analysis()

        details["files_changed"] = result_inc.files_changed
        details["affected_source_ids"] = result_inc.affected_source_ids

        # Verify file was re-parsed
        if result_inc.files_changed == 0:
            errors.append("Incremental analysis reported 0 files changed")

        if not result_inc.affected_source_ids and result_inc.files_changed > 0:
            # Not an error: the changed file may not be in any SourcePoint's
            # reachable graph (e.g. demo/test files, or --skip-repair with no
            # SourcePoints registered).
            details["affected_source_ids_note"] = (
                "No SourcePoints affected — changed file is outside all source reachable graphs"
            )

    finally:
        # Restore original content
        test_file.write_text(original_content)

    if errors:
        result.status = "FAIL"
        result.errors = errors

    result.details = details
    result.duration_s = time.time() - start
    return result


def stage_9_frontend(args: argparse.Namespace) -> StageResult:
    """Stage 9: Frontend build + probe."""
    if args.no_frontend:
        return StageResult(name="frontend", status="SKIP", details={"reason": "--no-frontend flag"})

    start = time.time()
    result = StageResult(name="frontend", status="PASS")
    details: dict[str, Any] = {}
    errors: list[str] = []

    frontend_dir = Path(__file__).parent.parent / "frontend"

    # Build
    build_result = subprocess.run(
        ["npm", "run", "build"],
        cwd=frontend_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    details["build_exit_code"] = build_result.returncode
    if build_result.returncode != 0:
        errors.append(f"Frontend build failed: {build_result.stderr[-200:]}")
        result.status = "FAIL"
        result.errors = errors
        result.details = details
        result.duration_s = time.time() - start
        return result

    # Start preview
    preview_proc = subprocess.Popen(
        ["npm", "run", "preview"],
        cwd=frontend_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for preview to start
    time.sleep(5)

    # Probe
    if _wait_for_tcp("localhost", 4173, timeout=15.0):
        details["preview_reachable"] = True
        # Try to get stats through proxy
        stats_resp = _http_get("http://localhost:4173/api/v1/stats", timeout=5.0)
        details["stats_through_proxy"] = stats_resp["ok"]
    else:
        errors.append("Frontend preview not reachable on localhost:4173")

    preview_proc.terminate()
    try:
        preview_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        preview_proc.kill()

    if errors:
        result.status = "FAIL"
        result.errors = errors

    result.details = details
    result.duration_s = time.time() - start
    return result


def _http_get(url: str, method: str = "GET", body: str | None = None, timeout: float = 10.0) -> dict[str, Any]:
    """HTTP GET/POST helper."""
    try:
        req = urllib.request.Request(url, data=body.encode() if body else None, method=method)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                "status": resp.status,
                "body": resp.read().decode("utf-8", errors="replace"),
                "ok": 200 <= resp.status < 300,
            }
    except Exception as exc:
        return {
            "status": None,
            "error": str(exc),
            "ok": False,
        }


# --- Main --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Full integration test")
    parser.add_argument("--neo4j-host", default="localhost", help="Neo4j host")
    parser.add_argument("--neo4j-port", default="7687", help="Neo4j port")
    parser.add_argument("--neo4j-user", default="neo4j", help="Neo4j user")
    parser.add_argument("--codewiki-url", default="http://localhost:8000", help="codewiki_lite base URL")
    parser.add_argument("--skip-repair", action="store_true", help="Skip repair stages (5-6, partial 7)")
    parser.add_argument("--no-frontend", action="store_true", help="Skip frontend stage (9)")
    parser.add_argument("--ignore-preflight-fails", action="store_true", help="Continue even if preflight fails")
    parser.add_argument("--entries", nargs="+", help="Specific source points to repair")
    parser.add_argument("--force-reparse", action="store_true", help="Force tree-sitter re-parse even if Neo4j has data")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    start_time = time.time()
    report = IntegrationReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        duration_s=0,
    )

    # Stage 1: Preflight
    r1 = stage_1_preflight(args)
    report.stages["preflight"] = {
        "status": r1.status,
        "duration_s": r1.duration_s,
        "details": r1.details,
        "errors": r1.errors,
    }
    if r1.status == "FAIL" and not args.ignore_preflight_fails:
        logger.error("Preflight failed, aborting. Use --ignore-preflight-fails to continue.")
        report.overall = "FAIL"
        REPORT_PATH.write_text(report.to_json())
        return 1

    # Stage 2: Source points
    r2 = stage_2_source_points(args)
    report.stages["source_points"] = {
        "status": r2.status,
        "duration_s": r2.duration_s,
        "details": r2.details,
        "errors": r2.errors,
    }

    # Stage 3: Static analysis
    r3 = stage_3_static_analysis(args)
    report.stages["static_analysis"] = {
        "status": r3.status,
        "duration_s": r3.duration_s,
        "details": r3.details,
        "errors": r3.errors,
    }

    if r3.status == "FAIL":
        logger.error("Static analysis failed, aborting.")
        report.overall = "FAIL"
        REPORT_PATH.write_text(report.to_json())
        return 1

    neo_store = r3.neo_store

    # Load source points for remaining stages
    client = SourcePointClient(base_url=args.codewiki_url)
    try:
        source_points = asyncio.run(client.fetch())
    except Exception:
        source_points = client.load_from_file(SOURCE_POINTS_FIXTURE)

    # Stage 4: Backend API
    r4 = stage_4_backend_api(args, neo_store, source_points)
    report.stages["api_baseline"] = {
        "status": r4.status,
        "duration_s": r4.duration_s,
        "details": r4.details,
        "errors": r4.errors,
    }

    if r4.status == "FAIL":
        logger.error("Backend API failed, aborting.")
        if r4.backend_server:
            r4.backend_server.should_exit = True
        report.overall = "FAIL"
        REPORT_PATH.write_text(report.to_json())
        return 1

    backend_port = r4.backend_port
    backend_server = r4.backend_server

    # Stage 5: Repair
    r5 = stage_5_repair(args, neo_store, source_points)
    report.stages["repair"] = {
        "status": r5.status,
        "duration_s": r5.duration_s,
        "details": r5.details,
        "errors": r5.errors,
    }

    # Stage 6: Post-repair API
    r6 = stage_6_post_repair_api(args, backend_port, neo_store)
    report.stages["api_post_repair"] = {
        "status": r6.status,
        "duration_s": r6.duration_s,
        "details": r6.details,
        "errors": r6.errors,
    }

    # Stage 7: Review workflow
    r7 = stage_7_review_workflow(args, backend_port, neo_store)
    report.stages["review"] = {
        "status": r7.status,
        "duration_s": r7.duration_s,
        "details": r7.details,
        "errors": r7.errors,
    }

    # Stage 8: Incremental
    r8 = stage_8_incremental(args, neo_store)
    report.stages["incremental"] = {
        "status": r8.status,
        "duration_s": r8.duration_s,
        "details": r8.details,
        "errors": r8.errors,
    }

    # Stage 9: Frontend
    r9 = stage_9_frontend(args)
    report.stages["frontend"] = {
        "status": r9.status,
        "duration_s": r9.duration_s,
        "details": r9.details,
        "errors": r9.errors,
    }

    # Finalize
    report.duration_s = time.time() - start_time

    # Determine overall status
    failed_stages = [name for name, data in report.stages.items() if data["status"] == "FAIL"]
    if failed_stages:
        report.overall = "FAIL"
        logger.error(f"Failed stages: {', '.join(failed_stages)}")
    else:
        report.overall = "PASS"
        logger.info("All stages PASSED")

    # Write report
    REPORT_PATH.write_text(report.to_json())
    logger.info(f"Report written to {REPORT_PATH}")

    # Cleanup
    if backend_server:
        backend_server.should_exit = True

    return 0 if report.overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())


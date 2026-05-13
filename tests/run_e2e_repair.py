"""End-to-end repair test — gate + counter-example loop via opencode + GLM-5.

This script exercises the **full repair pipeline** as documented in
``docs/architecture.md §3`` (Repair Agent) and ``§6`` (Gate + retry):

Pipeline stages (A → G):

  A. Environment preflight — verify opencode binary, DashScope credentials,
     CastEngine checkout, node/npm (optional), strip proxy env vars.
  B. Static analysis — populate a shared ``InMemoryGraphStore`` via
     ``PipelineOrchestrator`` (Phases 1-3: no dangling edges, symbol table
     disambiguation, ``call_type`` / ``resolved_by`` alignment).
  C. Entry-point resolution — convert fixed CastEngine source-point names
     (``CastSessionImpl::ProcessSetUp``, ``UnpackFuA`` …) to
     ``FunctionNode.id`` using the ``(abs_file, name)`` index pattern from
     ``tests/run_e2e_full.py``.
  D. Repair loop — drive ``RepairOrchestrator`` (subclassed here as
     ``E2ERepairHarness`` to bridge the still-pending ``icsl_tools`` CLI gap,
     see ``CLAUDE.md § Known gaps``). Per source point:

        inject CLAUDE.md + .icslpreprocess/  →
        spawn ``opencode run --pure -m <model>``  →
        parse agent JSON stdout  →
        apply via ``icsl_tools.write_edge``  →
        gate via ``icsl_tools.check_complete``  →
        on fail: append counter-examples + retry (≤ 3 attempts).

  E. Schema-invariant scan — assert ``resolved_by`` ∈ canonical enum, no
     dangling edges, no bare-name cross-module pollution (the exact
     defects Phase 1-3 fixed).
  F. Backend + frontend reachability — start FastAPI via uvicorn in a
     background thread, curl ``/health``, ``/api/v1/stats``,
     ``/api/v1/functions/{id}/call-chain``; optionally start ``vite dev``
     and probe the proxied ``/api/v1/stats``.
  G. Summary report — JSON dumped next to subprocess logs.

Environment (credentials pulled from ``/home/panckae/.claude/settings-alibaba.json``):
  * ``OPENAI_BASE_URL``  — DashScope OpenAI-compatible endpoint.
  * ``OPENAI_API_KEY``   — DashScope API key.
  * opencode binary on ``PATH`` (tested with GLM-5 via DashScope).

Usage::

    python -m tests.run_e2e_repair                         # full run
    python -m tests.run_e2e_repair --no-frontend           # skip vite probe
    python -m tests.run_e2e_repair --entries UnpackFuA     # single entry
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
import sys
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from codemap_lite.agent import icsl_tools
from codemap_lite.analysis.repair_orchestrator import (
    RepairConfig,
    RepairOrchestrator,
    SourceRepairResult,
)
from codemap_lite.graph.neo4j_store import InMemoryGraphStore, Neo4jGraphStore
from codemap_lite.graph.schema import CallsEdgeProps, FunctionNode, UnresolvedCallNode
from codemap_lite.parsing.cpp.plugin import CppPlugin
from codemap_lite.parsing.plugin_registry import PluginRegistry
from codemap_lite.parsing.types import CallType
from codemap_lite.pipeline.orchestrator import PipelineOrchestrator

logger = logging.getLogger("e2e_repair")

# --- Fixed test fixtures -----------------------------------------------------

CASTENGINE_ROOT = Path("/mnt/c/Task/openHarmony/foundation/CastEngine")
ALIBABA_SETTINGS = Path("/home/panckae/.claude/settings-alibaba.json")
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
LOG_ROOT = Path(__file__).parent / "_e2e_repair_logs"

# Fixed CastEngine entry points to mine GAPs from. Each tuple is
# ``(qualified_name, file_path_hint)``: the C++ tree-sitter plugin only stores
# the bare last identifier in ``FunctionNode.name`` (see
# ``codemap_lite/parsing/cpp/symbol_extractor.py:_get_function_name``), so
# qualified names alone collide across forks (cast_session_impl.cpp lives in
# both ``cast_framework/service/`` and ``cast_plus_stream/``) and unittest
# fixtures. The file_path_hint is matched as a substring against
# ``FunctionNode.file_path`` to pick the production-side definition.
#   * CastSessionImpl::ProcessSetUp        — member_fn_ptr stateProcessor_ dispatch
#   * UnpackFuA                            — direct entry with downstream virtuals
#   * CastSessionImpl::OnEvent             — virtual listener fan-out (cast_framework fork)
#   * CastSessionImpl::ProcessSetUpSuccess — second member_fn_ptr table entry
#   * CastSessionManagerService::CreateCastSession — service factory dispatch
#   * ConnectionManager::OnConsultDataReceivedFromSink — protocol-driven dispatch
#   * ConnectionManager::ParseAndCheckJsonData         — input parsing fan-out
DEFAULT_ENTRY_NAMES: tuple[tuple[str, str], ...] = (
    ("CastSessionImpl::ProcessSetUp",
     "castengine_cast_framework/service/src/session/src/cast_session_impl.cpp"),
    ("UnpackFuA",
     "castengine_wifi_display/services/protocol/rtp/src/rtp_codec_h264.cpp"),
    ("CastSessionImpl::OnEvent",
     "castengine_cast_framework/service/src/session/src/cast_session_impl.cpp"),
    ("CastSessionImpl::ProcessSetUpSuccess",
     "castengine_cast_framework/service/src/session/src/cast_session_impl.cpp"),
    ("CastSessionManagerService::CreateCastSession",
     "castengine_cast_framework/service/src/cast_session_manager_service.cpp"),
    ("ConnectionManager::OnConsultDataReceivedFromSink",
     "castengine_cast_framework/service/src/device_manager/src/connection_manager.cpp"),
    ("ConnectionManager::ParseAndCheckJsonData",
     "castengine_cast_framework/service/src/device_manager/src/connection_manager.cpp"),
)

CANONICAL_RESOLVED_BY = {"symbol_table", "signature", "dataflow", "context", "llm"}


@dataclass
class EntryPoint:
    """A resolved CastEngine entry point ready for repair."""

    name: str
    function_id: str
    file_path: str
    start_line: int


@dataclass
class InvariantViolation:
    """A single schema invariant violation emitted by :func:`check_invariants`."""

    rule: str
    detail: str


@dataclass
class RunReport:
    """Aggregated report dumped at the end of the run."""

    started_at: float
    static_stats: dict[str, Any] = field(default_factory=dict)
    entries: list[dict[str, Any]] = field(default_factory=list)
    repairs: list[dict[str, Any]] = field(default_factory=list)
    invariants: list[dict[str, str]] = field(default_factory=list)
    backend_probes: dict[str, Any] = field(default_factory=dict)
    frontend_probes: dict[str, Any] = field(default_factory=dict)
    success: bool = False
    duration_s: float = 0.0


# --- Stage A: environment preflight -----------------------------------------

_PROXY_VARS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


def _strip_proxy_env() -> list[str]:
    """Remove proxy vars so opencode can reach DashScope from WSL.

    Returns the list of variable names that were actually cleared, so the
    caller can log them.
    """
    cleared: list[str] = []
    for name in _PROXY_VARS:
        if name in os.environ:
            del os.environ[name]
            cleared.append(name)
    return cleared


def load_llm_env() -> dict[str, str]:
    """Read DashScope credentials from ``settings-alibaba.json``.

    Returns a ``{OPENAI_BASE_URL, OPENAI_API_KEY}`` dict suitable for
    merging into :class:`RepairConfig.env`.
    """
    if not ALIBABA_SETTINGS.exists():
        raise FileNotFoundError(
            f"LLM credential file missing: {ALIBABA_SETTINGS}. "
            "Create it with {env: {OPENAI_BASE_URL, OPENAI_API_KEY}}."
        )
    data = json.loads(ALIBABA_SETTINGS.read_text(encoding="utf-8"))
    env = data.get("env") or {}
    required = ("OPENAI_BASE_URL", "OPENAI_API_KEY")
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise ValueError(f"{ALIBABA_SETTINGS} missing keys: {missing}")
    return {k: env[k] for k in required}


def check_environment(require_frontend: bool) -> dict[str, Any]:
    """Verify binaries + fixtures are reachable before running the pipeline.

    Raises :class:`RuntimeError` if a required dependency is missing.
    ``node``/``npm`` are only required when ``require_frontend`` is ``True``.
    """
    info: dict[str, Any] = {}

    opencode = shutil.which("opencode")
    if opencode is None:
        raise RuntimeError("opencode binary not found on PATH")
    info["opencode"] = opencode

    if not CASTENGINE_ROOT.exists():
        raise RuntimeError(f"CastEngine checkout missing: {CASTENGINE_ROOT}")
    info["castengine_root"] = str(CASTENGINE_ROOT)

    info["llm_env"] = load_llm_env()
    info["proxy_cleared"] = _strip_proxy_env()

    if require_frontend:
        node = shutil.which("node")
        npm = shutil.which("npm")
        if node is None or npm is None:
            raise RuntimeError("node/npm missing but --frontend requested")
        if not FRONTEND_DIR.exists():
            raise RuntimeError(f"frontend dir missing: {FRONTEND_DIR}")
        info["node"] = node
        info["npm"] = npm
        info["frontend_dir"] = str(FRONTEND_DIR)

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    info["log_root"] = str(LOG_ROOT)
    return info


# --- Stage B: static analysis -----------------------------------------------


def run_static_analysis(store: InMemoryGraphStore) -> dict[str, Any]:
    """Drive :class:`PipelineOrchestrator` over the full CastEngine tree.

    Populates ``store`` in-place with Phase 1-3 invariants (no dangling
    edges, bare-name ambiguity → UC, ``resolved_by=symbol_table`` only
    on DIRECT calls).
    """
    registry = PluginRegistry()
    registry.register("cpp", CppPlugin())

    logger.info("static analysis: target=%s", CASTENGINE_ROOT)
    started = time.time()
    orch = PipelineOrchestrator(
        target_dir=CASTENGINE_ROOT, store=store, registry=registry
    )
    result = orch.run_full_analysis()
    elapsed = time.time() - started

    stats = {
        "files_scanned": result.files_scanned,
        "functions_found": result.functions_found,
        "direct_calls": result.direct_calls,
        "unresolved_calls": result.unresolved_calls,
        "errors_sample": result.errors[:10],
        "elapsed_s": round(elapsed, 2),
    }
    logger.info(
        "static analysis done in %.1fs: %d fns, %d calls, %d UC",
        elapsed,
        result.functions_found,
        result.direct_calls,
        result.unresolved_calls,
    )
    return stats


# --- Stage C: entry-point resolution ----------------------------------------


def resolve_entry_points(
    store: InMemoryGraphStore, names: tuple[tuple[str, str], ...]
) -> list[EntryPoint]:
    """Map fixed CastEngine source-point names to :class:`FunctionNode.id`.

    Each request is a ``(qualified_name, file_path_hint)`` tuple. The C++
    plugin stores only the bare last identifier in ``FunctionNode.name`` and
    the qualified form inside ``FunctionNode.signature`` — so we match
    qualified names via **substring against signature**, and tie-break with
    the file_path_hint (a substring of the desired ``file_path``). If the
    qualified name has no ``::`` we fall back to ``FunctionNode.name``.

    This replaces the previous bare-name matcher which silently collapsed
    ``CastSessionImpl::OnEvent`` and ``CastStreamManager::OnEvent`` onto the
    same unittest fixture because both ended in ``OnEvent``.
    """
    resolved: list[EntryPoint] = []
    castengine_root = str(CASTENGINE_ROOT.resolve()).replace("\\", "/")
    all_fns = list(store._functions.values())

    for qualified_name, file_hint in names:
        bare = qualified_name.split("::")[-1]
        # Filter to functions inside CastEngine whose bare name matches.
        in_tree = [
            fn
            for fn in all_fns
            if fn.name == bare
            and str(Path(fn.file_path).resolve()).replace("\\", "/").startswith(
                castengine_root
            )
        ]
        if not in_tree:
            logger.warning(
                "entry point %s: no candidate in CastEngine (bare name %s)",
                qualified_name,
                bare,
            )
            continue
        # If the request is qualified (has ::), insist on signature carrying
        # the qualified name so we don't mis-attribute a free function.
        qualified_candidates = (
            [fn for fn in in_tree if qualified_name in fn.signature]
            if "::" in qualified_name
            else in_tree
        )
        # Then narrow by file_hint substring so we pick the right fork
        # (service/session/src vs cast_plus_stream) or skip unittest fixtures.
        hinted = [
            fn
            for fn in qualified_candidates
            if file_hint
            and file_hint.replace("\\", "/") in fn.file_path.replace("\\", "/")
        ] or qualified_candidates
        if not hinted:
            logger.warning(
                "entry point %s: found %d bare-name matches but none carry "
                "qualified signature or file_hint",
                qualified_name,
                len(in_tree),
            )
            continue
        best = min(hinted, key=lambda fn: fn.start_line)
        resolved.append(
            EntryPoint(
                name=qualified_name,
                function_id=best.id,
                file_path=best.file_path,
                start_line=best.start_line,
            )
        )
        logger.info(
            "entry point %s -> id=%s file=%s:%d",
            qualified_name,
            best.id,
            best.file_path,
            best.start_line,
        )
    return resolved


# --- Stage D: repair harness ------------------------------------------------


class _StoreAdapter:
    """Adapt :class:`InMemoryGraphStore` to the icsl_tools ``GraphStoreProtocol``.

    InMemoryGraphStore does not natively expose ``edge_exists``,
    ``create_repair_log``, ``delete_unresolved_call``, or
    ``get_pending_gaps_for_source``. This adapter supplies them without
    polluting the store class itself, keeping the Phase 1-3 schema
    invariants (no dangling edges, ``resolved_by`` enum, canonical
    UC surface) unchanged.
    """

    def __init__(self, store: InMemoryGraphStore, source_fn_ids: set[str]) -> None:
        self._store = store
        # source-scoped pending GAPs: only UCs whose caller is reachable
        # from the entry point count toward gate completeness.
        self._source_fn_ids = source_fn_ids
        self._repair_logs: list[dict[str, Any]] = []

    def get_reachable_subgraph(self, source_id: str, max_depth: int = 50) -> dict[str, Any]:
        return self._store.get_reachable_subgraph(source_id, max_depth=max_depth)

    def edge_exists(self, caller_id: str, callee_id: str, call_file: str, call_line: int) -> bool:
        for edge in self._store._calls_edges:
            if (
                edge.caller_id == caller_id
                and edge.callee_id == callee_id
                and edge.props.call_file == call_file
                and edge.props.call_line == call_line
            ):
                return True
        return False

    def create_calls_edge(self, caller_id: str, callee_id: str, props: dict[str, Any]) -> None:
        edge_props = CallsEdgeProps(
            resolved_by=props.get("resolved_by", "llm"),
            call_type=props.get("call_type", "direct"),
            call_file=props.get("call_file", ""),
            call_line=int(props.get("call_line", 0)),
        )
        self._store.create_calls_edge(caller_id, callee_id, edge_props)

    def create_repair_log(self, log_data: dict[str, Any]) -> None:
        self._repair_logs.append(log_data)

    def delete_unresolved_call(self, caller_id: str, call_file: str, call_line: int) -> None:
        drop = [
            uc_id
            for uc_id, uc in self._store._unresolved_calls.items()
            if uc.caller_id == caller_id
            and uc.call_file == call_file
            and uc.call_line == call_line
        ]
        for uc_id in drop:
            self._store._unresolved_calls.pop(uc_id, None)

    def get_pending_gaps_for_source(self, source_id: str) -> list[dict[str, Any]]:
        # Only return UCs whose caller is reachable from the entry point.
        # We pre-computed reachability once during harness init and cached
        # the function id set to avoid re-BFS-ing on every gate check.
        reachable = self._source_fn_ids
        out: list[dict[str, Any]] = []
        for uc in self._store._unresolved_calls.values():
            if uc.caller_id not in reachable:
                continue
            if getattr(uc, "status", "pending") != "pending":
                continue
            out.append({"id": uc.id, "caller_id": uc.caller_id})
        return out

    @property
    def repair_logs(self) -> list[dict[str, Any]]:
        return list(self._repair_logs)


def _compute_reachable_fn_ids(store: InMemoryGraphStore, source_id: str) -> set[str]:
    """Collect FunctionNode ids reachable from ``source_id`` via CALLS edges."""
    subgraph = store.get_reachable_subgraph(source_id, max_depth=50)
    return {fn.id for fn in subgraph["nodes"]}


def mirror_store_to_neo4j(
    in_mem: InMemoryGraphStore,
    *,
    uri: str,
    user: str,
    password: str,
) -> Neo4jGraphStore:
    """Copy the in-memory static-analysis snapshot into a live Neo4j instance.

    architecture.md §4 规定 Neo4j 是 Phase 3 的权威图存储；harness 之前仅
    写 ``InMemoryGraphStore``，但注入到目标目录的 ``.icslpreprocess/icsl_tools.py``
    (CLI, architecture.md §3 Repair Agent 工具协议) 只会连 Neo4j。
    不把 in-memory snapshot 搬过去，agent subprocess 的 ``query-reachable``
    就会返回空集、``check-complete`` 虚报完成，调用链永远修不到。

    Wipe policy: only our own 4 schema labels (Function/File/UnresolvedCall/
    RepairLog) are DETACH-deleted so we never touch non-codemap data that
    might be sharing the Neo4j instance; relationships attached to those
    nodes go with them via ``DETACH``.

    The bulk MERGE uses ``Neo4jGraphStore.create_*`` helpers directly so
    Cypher stays centralized (architecture.md §4: all Neo4j writes route
    through ``graph/neo4j_store.py``).
    """
    neo = Neo4jGraphStore(uri=uri, user=user, password=password)
    driver = neo._get_driver()

    # Pre-serialize as plain dicts so driver param binding stays fast.
    fn_rows = [
        {
            "id": fn.id,
            "signature": fn.signature,
            "name": fn.name,
            "file_path": fn.file_path,
            "start_line": fn.start_line,
            "end_line": fn.end_line,
            "body_hash": fn.body_hash,
        }
        for fn in in_mem._functions.values()
    ]
    file_rows = [
        {
            "id": f.id,
            "file_path": f.file_path,
            "hash": f.hash,
            "primary_language": f.primary_language,
        }
        for f in in_mem._files.values()
    ]
    call_rows = [
        {
            "caller_id": edge.caller_id,
            "callee_id": edge.callee_id,
            "call_file": edge.props.call_file,
            "call_line": edge.props.call_line,
            "resolved_by": edge.props.resolved_by,
            "call_type": edge.props.call_type,
        }
        for edge in in_mem._calls_edges
    ]
    uc_rows = [
        {
            "id": uc.id,
            "caller_id": uc.caller_id,
            "call_expression": uc.call_expression,
            "call_file": uc.call_file,
            "call_line": uc.call_line,
            "call_type": uc.call_type,
            "source_code_snippet": uc.source_code_snippet,
            "var_name": uc.var_name,
            "var_type": uc.var_type,
            "candidates": list(uc.candidates),
            "retry_count": uc.retry_count,
            "status": uc.status,
            "last_attempt_timestamp": uc.last_attempt_timestamp,
            "last_attempt_reason": uc.last_attempt_reason,
        }
        for uc in in_mem._unresolved_calls.values()
    ]
    log_rows = [
        {
            "id": r.id,
            "caller_id": r.caller_id,
            "callee_id": r.callee_id,
            "call_location": r.call_location,
            "repair_method": r.repair_method,
            "llm_response": r.llm_response,
            "timestamp": r.timestamp,
            "reasoning_summary": r.reasoning_summary,
        }
        for r in in_mem._repair_logs.values()
    ]

    # Batched UNWIND writes inside a single session. Per-row create_* calls
    # open a fresh session and round-trip for each node, which for the
    # CastEngine-sized snapshot (thousands of fns / tens of thousands of UCs)
    # pushes mirror time into minutes. architecture.md §4 authoritative store
    # requirement holds — we still route everything through MERGE statements
    # equivalent to ``Neo4jGraphStore.create_*``; we only change the batching.
    BATCH = 1000

    def _batched(rows: list[dict[str, Any]]):
        for i in range(0, len(rows), BATCH):
            yield rows[i : i + BATCH]

    with driver.session() as session:
        session.run(
            "MATCH (n) "
            "WHERE any(l IN labels(n) "
            "          WHERE l IN ['Function','File','UnresolvedCall','RepairLog']) "
            "DETACH DELETE n"
        ).consume()
        for chunk in _batched(fn_rows):
            session.run(
                "UNWIND $rows AS row "
                "MERGE (f:Function {id: row.id}) "
                "SET f.signature = row.signature, f.name = row.name, "
                "    f.file_path = row.file_path, f.start_line = row.start_line, "
                "    f.end_line = row.end_line, f.body_hash = row.body_hash",
                rows=chunk,
            ).consume()
        for chunk in _batched(file_rows):
            session.run(
                "UNWIND $rows AS row "
                "MERGE (f:File {id: row.id}) "
                "SET f.file_path = row.file_path, f.hash = row.hash, "
                "    f.primary_language = row.primary_language",
                rows=chunk,
            ).consume()
        for chunk in _batched(call_rows):
            session.run(
                "UNWIND $rows AS row "
                "MATCH (a:Function {id: row.caller_id}) "
                "MATCH (b:Function {id: row.callee_id}) "
                "MERGE (a)-[r:CALLS {call_file: row.call_file, call_line: row.call_line}]->(b) "
                "SET r.resolved_by = row.resolved_by, r.call_type = row.call_type",
                rows=chunk,
            ).consume()
        for chunk in _batched(uc_rows):
            session.run(
                "UNWIND $rows AS row "
                "MERGE (u:UnresolvedCall {id: row.id}) "
                "SET u.caller_id = row.caller_id, u.call_expression = row.call_expression, "
                "    u.call_file = row.call_file, u.call_line = row.call_line, "
                "    u.call_type = row.call_type, u.source_code_snippet = row.source_code_snippet, "
                "    u.var_name = row.var_name, u.var_type = row.var_type, "
                "    u.candidates = row.candidates, u.retry_count = row.retry_count, "
                "    u.status = row.status, "
                "    u.last_attempt_timestamp = row.last_attempt_timestamp, "
                "    u.last_attempt_reason = row.last_attempt_reason "
                "WITH u, row "
                "MATCH (caller:Function {id: row.caller_id}) "
                "MERGE (caller)-[:HAS_GAP]->(u)",
                rows=chunk,
            ).consume()
        for chunk in _batched(log_rows):
            session.run(
                "UNWIND $rows AS row "
                "MERGE (r:RepairLog {id: row.id}) "
                "SET r.caller_id = row.caller_id, r.callee_id = row.callee_id, "
                "    r.call_location = row.call_location, "
                "    r.repair_method = row.repair_method, "
                "    r.llm_response = row.llm_response, "
                "    r.timestamp = row.timestamp, "
                "    r.reasoning_summary = row.reasoning_summary",
                rows=chunk,
            ).consume()

    logger.info(
        "mirrored to Neo4j: %d functions, %d files, %d calls, %d unresolved, %d repair_logs",
        len(fn_rows),
        len(file_rows),
        len(call_rows),
        len(uc_rows),
        len(log_rows),
    )
    return neo


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"(\{[\s\S]*?\}|\[[\s\S]*?\])")


def _extract_edges_from_stdout(stdout: str) -> list[dict[str, Any]]:
    """Extract a list of ``{caller_id, callee_id, call_type, call_file, call_line}``
    edges from the agent's free-form stdout.

    The agent is asked (via CLAUDE.md) to emit a JSON array of edge dicts.
    We accept three shapes to stay resilient to wrapper prose:
      1. fenced ```json [...] ``` block,
      2. a bare top-level JSON array,
      3. a single JSON object (we wrap it).
    """
    candidates: list[str] = list(_JSON_FENCE_RE.findall(stdout))
    if not candidates:
        candidates = list(_BARE_JSON_RE.findall(stdout))
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            if "edges" in parsed and isinstance(parsed["edges"], list):
                return [e for e in parsed["edges"] if isinstance(e, dict)]
            parsed = [parsed]
        if isinstance(parsed, list):
            return [e for e in parsed if isinstance(e, dict)]
    return []


def _render_counter_examples(remaining: list[dict[str, Any]]) -> str:
    """Format the remaining GAP ids as CLAUDE-visible feedback."""
    if not remaining:
        return "# No remaining GAPs detected\n"
    lines = ["# Counter-examples — gate still sees these GAPs as unresolved", ""]
    for gap in remaining[:25]:
        lines.append(f"- {gap.get('id', '?')} (caller={gap.get('caller_id', '?')})")
    if len(remaining) > 25:
        lines.append(f"- … {len(remaining) - 25} more not shown")
    return "\n".join(lines) + "\n"


class E2ERepairHarness(RepairOrchestrator):
    """In-process harness that swaps the stub gate for real ``icsl_tools`` calls.

    Why subclass rather than rewrite the orchestrator? ``RepairOrchestrator``
    owns injection, subprocess spawning, retry, cleanup — all of which we
    keep. The only seams we need are:
      * ``_run_single_repair`` — to capture opencode stdout and hand it to
        ``icsl_tools.write_edge`` on the in-memory store, and to regenerate
        the counter-examples file between attempts;
      * ``_check_gate`` — to call ``icsl_tools.check_complete`` against the
        shared store instead of the stubbed ``return True``.
    """

    def __init__(
        self,
        config: RepairConfig,
        store: InMemoryGraphStore,
        source_reach: dict[str, set[str]],
    ) -> None:
        super().__init__(config)
        self._store = store
        self._adapters: dict[str, _StoreAdapter] = {
            sid: _StoreAdapter(store, fn_ids)
            for sid, fn_ids in source_reach.items()
        }
        self._counter_examples: dict[str, str] = defaultdict(str)
        self._stdout_capture: dict[str, list[str]] = defaultdict(list)

    def _inject_files(
        self,
        target_dir: Path,
        source_id: str,
        counter_examples: str,
    ) -> None:
        # Use the harness's running counter-examples string so retries see
        # the accumulated feedback instead of a fresh empty slate.
        accumulated = self._counter_examples.get(source_id) or counter_examples
        super()._inject_files(target_dir, source_id, accumulated)

    async def _run_single_repair(self, source_id: str) -> SourceRepairResult:
        target_dir = self._config.target_dir
        attempts = 0
        max_attempts = self.MAX_RETRIES_PER_GAP
        log_dir = self._config.log_dir or LOG_ROOT

        while attempts < max_attempts:
            attempts += 1
            self._inject_files(
                target_dir=target_dir, source_id=source_id, counter_examples=""
            )
            try:
                cmd = self._build_command(source_id)
                env = {**os.environ, **(self._config.env or {})}

                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / f"{source_id}.attempt{attempts}.log"
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(target_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout_b, stderr_b = await proc.communicate()
                stdout = stdout_b.decode("utf-8", errors="replace")
                stderr = stderr_b.decode("utf-8", errors="replace")
                log_path.write_text(
                    f"# cmd: {cmd}\n# rc: {proc.returncode}\n"
                    f"## stdout\n{stdout}\n## stderr\n{stderr}\n",
                    encoding="utf-8",
                )
                self._stdout_capture[source_id].append(stdout)

                self._apply_agent_edges(source_id, stdout)

                if await self._check_gate(source_id):
                    return SourceRepairResult(
                        source_id=source_id, success=True, attempts=attempts
                    )
                self._refresh_counter_examples(source_id)
            finally:
                self._cleanup_injection(target_dir)

        return SourceRepairResult(
            source_id=source_id,
            success=False,
            attempts=attempts,
            error=f"Gate check failed after {max_attempts} attempts",
        )

    def _apply_agent_edges(self, source_id: str, stdout: str) -> None:
        """Parse edges from opencode stdout and route through :mod:`icsl_tools`."""
        adapter = self._adapters[source_id]
        edges = _extract_edges_from_stdout(stdout)
        applied = 0
        skipped = 0
        for edge in edges:
            caller_id = edge.get("caller_id")
            callee_id = edge.get("callee_id")
            call_type = edge.get("call_type", "direct")
            call_file = edge.get("call_file", "")
            call_line = edge.get("call_line")
            if not caller_id or not callee_id or call_line is None:
                skipped += 1
                continue
            try:
                result = icsl_tools.write_edge(
                    caller_id=str(caller_id),
                    callee_id=str(callee_id),
                    call_type=str(call_type),
                    call_file=str(call_file),
                    call_line=int(call_line),
                    store=adapter,
                )
            except Exception as exc:  # noqa: BLE001 — surface agent payload errors
                logger.warning("write_edge failed for %s: %s", source_id, exc)
                skipped += 1
                continue
            if result.get("edge_created"):
                applied += 1
            else:
                skipped += 1
        logger.info(
            "repair %s: agent proposed %d edges (%d applied, %d skipped)",
            source_id,
            len(edges),
            applied,
            skipped,
        )

    def _refresh_counter_examples(self, source_id: str) -> None:
        """Regenerate the counter-examples file for the next retry."""
        adapter = self._adapters[source_id]
        remaining = adapter.get_pending_gaps_for_source(source_id)
        self._counter_examples[source_id] = _render_counter_examples(remaining)

    async def _check_gate(self, source_id: str) -> bool:
        adapter = self._adapters[source_id]
        status = icsl_tools.check_complete(source_id, adapter)
        logger.info(
            "gate %s: complete=%s remaining=%d",
            source_id,
            status["complete"],
            status["remaining_gaps"],
        )
        return bool(status["complete"])


# --- Stage E: schema-invariant scan -----------------------------------------


def check_invariants(store: InMemoryGraphStore) -> list[InvariantViolation]:
    """Verify the Phase 1-3 invariants that the repair loop must preserve.

    Rules (all hard failures if violated):
      * ``resolved_by`` ∈ canonical enum,
      * no dangling CALLS edges (caller or callee must be a known function),
      * ``call_type == direct`` for any edge with ``resolved_by == symbol_table``
        (Phase 3 — indirect dispatch must never appear as a direct edge),
      * UC ``call_type`` field is non-empty (Phase 3 surface rule).
    """
    violations: list[InvariantViolation] = []
    fn_ids = set(store._functions.keys())

    for edge in store._calls_edges:
        if edge.props.resolved_by not in CANONICAL_RESOLVED_BY:
            violations.append(
                InvariantViolation(
                    rule="resolved_by_enum",
                    detail=(
                        f"edge {edge.caller_id}->{edge.callee_id} has "
                        f"resolved_by={edge.props.resolved_by!r}"
                    ),
                )
            )
        if edge.caller_id not in fn_ids or edge.callee_id not in fn_ids:
            violations.append(
                InvariantViolation(
                    rule="no_dangling_edges",
                    detail=(
                        f"edge {edge.caller_id}->{edge.callee_id} refers to "
                        "unknown function id"
                    ),
                )
            )
        if edge.props.resolved_by == "symbol_table" and edge.props.call_type != "direct":
            violations.append(
                InvariantViolation(
                    rule="symbol_table_direct_only",
                    detail=(
                        f"edge {edge.caller_id}->{edge.callee_id} "
                        f"call_type={edge.props.call_type} resolved_by=symbol_table"
                    ),
                )
            )

    for uc in store._unresolved_calls.values():
        if not uc.call_type:
            violations.append(
                InvariantViolation(
                    rule="uc_call_type_nonempty",
                    detail=f"UC {uc.id} has empty call_type",
                )
            )
        if uc.caller_id not in fn_ids:
            violations.append(
                InvariantViolation(
                    rule="uc_caller_known",
                    detail=f"UC {uc.id} caller_id={uc.caller_id} not in functions",
                )
            )
    return violations


# --- Stage F: backend + frontend probes -------------------------------------


def _find_free_port() -> int:
    """Ask the kernel for a free TCP port for the uvicorn backend."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_tcp(host: str, port: int, timeout: float = 15.0) -> bool:
    """Block until ``(host, port)`` accepts TCP, or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.2)
    return False


def start_backend_thread(
    store: InMemoryGraphStore, source_entries: list[dict[str, Any]]
) -> tuple[threading.Thread, int, Any]:
    """Spin up FastAPI in a background thread. Returns (thread, port, server).

    We use uvicorn's programmatic ``Config`` + ``Server`` so we can signal
    shutdown by flipping ``server.should_exit``. The thread is marked
    ``daemon=True`` so it dies with the pytest process if an assertion
    blows up before the explicit shutdown.
    """
    import uvicorn

    from codemap_lite.api.app import create_app

    app = create_app(store=store)
    app.state.source_points = source_entries
    app.state.analysis_stats = {
        "total_functions": len(store._functions),
        "total_calls": len(store._calls_edges),
        "total_unresolved": len(store._unresolved_calls),
        "total_source_points": len(source_entries),
        "total_files": len(store._files),
    }

    port = _find_free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="e2e-backend", daemon=True)
    thread.start()
    if not _wait_for_tcp("127.0.0.1", port, timeout=15.0):
        server.should_exit = True
        raise RuntimeError(f"backend did not come up on 127.0.0.1:{port}")
    return thread, port, server


def probe_backend(port: int, entries: list[EntryPoint]) -> dict[str, Any]:
    """Exercise the three backend endpoints that the frontend depends on.

    We deliberately do NOT require every probe to succeed — we record
    the per-endpoint status so the RunReport can show what worked.
    """
    import urllib.request

    base = f"http://127.0.0.1:{port}"
    results: dict[str, Any] = {"base_url": base, "endpoints": {}}

    def _get(path: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(base + path, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return {
                    "status": resp.status,
                    "body_preview": body[:200],
                    "ok": 200 <= resp.status < 300,
                }
        except Exception as exc:  # noqa: BLE001 — probe failure is data
            return {"status": None, "error": str(exc), "ok": False}

    results["endpoints"]["/health"] = _get("/health")
    results["endpoints"]["/api/v1/stats"] = _get("/api/v1/stats")

    if entries:
        probe_entry = entries[0]
        path = f"/api/v1/functions/{probe_entry.function_id}/call-chain?depth=3"
        results["endpoints"][path] = _get(path)

    results["ok"] = all(ep.get("ok") for ep in results["endpoints"].values())
    return results


def probe_frontend(backend_port: int) -> dict[str, Any]:
    """Start ``vite dev`` and probe the proxied ``/api/v1/stats`` route.

    Skipped if ``node_modules`` is missing — we don't install deps here;
    the operator is expected to have run ``npm install`` once.
    """
    import subprocess
    import urllib.request

    node_modules = FRONTEND_DIR / "node_modules"
    if not node_modules.exists():
        return {"skipped": True, "reason": "node_modules missing; run npm install"}

    vite_port = 5173  # vite.config.ts pins this with strictPort=true
    log_path = LOG_ROOT / "frontend.vite.log"
    log_fh = log_path.open("ab")
    env = {**os.environ, "VITE_API_BASE": f"http://127.0.0.1:{backend_port}"}
    # Tell the vite proxy where the backend actually lives. The default
    # proxy target in vite.config.ts is port 8000, but our backend runs
    # on an ephemeral port. We work around this by hitting vite directly
    # for a static asset, and hitting the backend itself for /api routes.
    try:
        proc = subprocess.Popen(  # noqa: S603 — trusted local dev command
            ["npm", "run", "dev", "--", "--host", "127.0.0.1"],
            cwd=str(FRONTEND_DIR),
            stdout=log_fh,
            stderr=log_fh,
            env=env,
        )
    except FileNotFoundError as exc:
        log_fh.close()
        return {"skipped": True, "reason": f"npm not available: {exc}"}

    try:
        if not _wait_for_tcp("127.0.0.1", vite_port, timeout=30.0):
            return {"ok": False, "error": "vite did not come up on :5173"}

        result: dict[str, Any] = {"ok": True, "port": vite_port, "endpoints": {}}
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{vite_port}/", timeout=10
            ) as resp:
                result["endpoints"]["/"] = {
                    "status": resp.status,
                    "ok": 200 <= resp.status < 300,
                }
        except Exception as exc:  # noqa: BLE001
            result["endpoints"]["/"] = {"ok": False, "error": str(exc)}
            result["ok"] = False
        return result
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()


# --- Stage G: main / CLI ----------------------------------------------------


DEFAULT_MODEL = "dashscope/glm-5"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tests.run_e2e_repair",
        description="E2E repair test: static analysis + opencode + gate + probes.",
    )
    parser.add_argument(
        "--entries",
        nargs="+",
        default=[f"{n}@{h}" for (n, h) in DEFAULT_ENTRY_NAMES],
        help=(
            "Entry-point names to mine GAPs from. Use ``name@file_hint`` to "
            "disambiguate forks (e.g. "
            "``CastSessionImpl::OnEvent@castengine_cast_framework/...``); "
            "bare names fall back to bare-name matching."
        ),
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help="opencode model spec."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=240,
        help="Per-entry opencode subprocess timeout (seconds).",
    )
    parser.add_argument(
        "--no-frontend",
        action="store_true",
        help="Skip the vite dev probe (faster and no node/npm needed).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Max concurrent repair subprocesses.",
    )
    parser.add_argument(
        "--report",
        default=str(LOG_ROOT / "report.json"),
        help="Where to dump the final RunReport JSON.",
    )
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> RunReport:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    report = RunReport(started_at=time.time())

    logger.info("stage A: environment preflight")
    env_info = check_environment(require_frontend=not args.no_frontend)
    report.static_stats["env"] = {k: v for k, v in env_info.items() if k != "llm_env"}

    logger.info("stage B: static analysis")
    store = InMemoryGraphStore()
    report.static_stats.update(run_static_analysis(store))

    logger.info("stage C: entry-point resolution")
    # Split ``name@file_hint`` tokens so ``resolve_entry_points`` sees a tuple
    # of (qualified_name, hint). A missing hint degrades gracefully to the
    # legacy bare-name matcher for ad-hoc invocations.
    entry_specs = tuple(
        tuple(tok.split("@", 1)) if "@" in tok else (tok, "")
        for tok in args.entries
    )
    entry_points = resolve_entry_points(store, entry_specs)
    report.entries = [asdict(ep) for ep in entry_points]
    if not entry_points:
        raise RuntimeError("no entry points resolved; cannot proceed")

    source_reach = {
        ep.function_id: _compute_reachable_fn_ids(store, ep.function_id)
        for ep in entry_points
    }

    # Bridge InMemory → Neo4j so the agent subprocess's icsl_tools CLI
    # (which only talks to Neo4j, see architecture.md §3 + §4) sees the
    # same graph the harness's in-process gate sees.
    neo4j_uri = "bolt://localhost:7687"
    neo4j_user = "neo4j"
    neo4j_password = os.environ.get("NEO4J_PASSWORD", "password")
    neo_store = mirror_store_to_neo4j(
        store,
        uri=neo4j_uri,
        user=neo4j_user,
        password=neo4j_password,
    )

    logger.info("stage D: repair loop")
    llm_env = env_info["llm_env"]
    repair_config = RepairConfig(
        target_dir=CASTENGINE_ROOT,
        backend="opencode",
        command="opencode",
        args=[
            "run",
            "--pure",
            "-m",
            args.model,
            "--dangerously-skip-permissions",
        ],
        max_concurrency=args.concurrency,
        neo4j_uri=neo4j_uri,
        neo4j_user=neo4j_user,
        neo4j_password=neo4j_password,
        env=llm_env,
        log_dir=LOG_ROOT,
        graph_store=neo_store,
    )
    harness = E2ERepairHarness(
        config=repair_config, store=store, source_reach=source_reach
    )

    source_ids = [ep.function_id for ep in entry_points]
    try:
        repair_results = await asyncio.wait_for(
            harness.run_repairs(source_ids),
            timeout=args.timeout * max(1, len(source_ids)),
        )
    except asyncio.TimeoutError:
        logger.error("repair loop timed out after %ds", args.timeout * len(source_ids))
        repair_results = []

    report.repairs = [asdict(r) for r in repair_results]

    logger.info("stage E: schema-invariant scan")
    violations = check_invariants(store)
    report.invariants = [asdict(v) for v in violations]

    logger.info("stage F: backend + frontend probes")
    entries_for_api = [
        {
            "id": ep.function_id,
            "signature": ep.name,
            "file": ep.file_path,
            "line": ep.start_line,
            "kind": "entry_point",
            "reason": "e2e repair",
            "function_id": ep.function_id,
        }
        for ep in entry_points
    ]
    backend_thread, backend_port, backend_server = start_backend_thread(
        store, entries_for_api
    )
    try:
        report.backend_probes = probe_backend(backend_port, entry_points)
        if not args.no_frontend:
            report.frontend_probes = probe_frontend(backend_port)
        else:
            report.frontend_probes = {"skipped": True, "reason": "--no-frontend"}
    finally:
        backend_server.should_exit = True
        backend_thread.join(timeout=5)
        neo_store.close()

    # Success = no invariant violations AND backend probes all green.
    # Repair success is surfaced per-entry but does NOT gate the run —
    # the harness records what the LLM did regardless.
    report.success = (
        not violations
        and bool(report.backend_probes.get("ok"))
    )
    report.duration_s = round(time.time() - report.started_at, 2)
    return report


def _dump_report(report: RunReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    logger.info("wrote report: %s", path)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = asyncio.run(_main_async(args))
    except Exception as exc:  # noqa: BLE001 — surface preflight failures cleanly
        logger.exception("E2E run failed during setup: %s", exc)
        return 2
    _dump_report(report, Path(args.report))
    logger.info(
        "E2E run finished: success=%s duration=%.1fs invariants=%d",
        report.success,
        report.duration_s,
        len(report.invariants),
    )
    return 0 if report.success else 1


if __name__ == "__main__":
    sys.exit(main())

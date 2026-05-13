"""icsl_tools — Agent-side CLI tool for graph query, edge writing, and gate checking.

This module is copied to the target code directory during repair and invoked by the
CLI agent subprocess. It provides three operations:
- query-reachable: Get the reachable subgraph from a source point
- write-edge: Write a CALLS edge + RepairLog, delete the UnresolvedCall
- check-complete: Check if all reachable GAPs are resolved

The module can be invoked in two ways, matching the CLI protocol declared in
``agent/claude_md_template.py`` (see ``docs/architecture.md §3``):

1. In-process Python calls (used by the orchestrator harness and unit tests)::

       from codemap_lite.agent.icsl_tools import query_reachable
       query_reachable("src_001", store)

2. Subprocess CLI (used by the repair agent subprocess at the target dir)::

       python .icslpreprocess/icsl_tools.py query-reachable --source src_001
       python .icslpreprocess/icsl_tools.py write-edge \\
           --caller func_a --callee func_b --call-type indirect \\
           --call-file foo.cpp --call-line 42 \\
           [--llm-response "<raw excerpt>"] \\
           [--reasoning-summary "picked X because Y"]
       python .icslpreprocess/icsl_tools.py check-complete --source src_001

The CLI loads Neo4j connection settings from ``.icslpreprocess/config.yaml``
(relative to the current working directory, which is the target code dir) and
writes a single JSON document to stdout per invocation.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, Protocol


class GraphStoreProtocol(Protocol):
    """Protocol for graph store operations needed by icsl_tools."""

    def get_reachable_subgraph(self, source_id: str, max_depth: int = 50) -> dict[str, Any]: ...
    def edge_exists(self, caller_id: str, callee_id: str, call_file: str, call_line: int) -> bool: ...
    def create_calls_edge(self, caller_id: str, callee_id: str, props: dict[str, Any]) -> None: ...
    def create_repair_log(self, log_data: dict[str, Any]) -> None: ...
    def delete_unresolved_call(self, caller_id: str, call_file: str, call_line: int) -> None: ...
    def get_pending_gaps_for_source(self, source_id: str) -> list[Any]: ...
    # Real stores (InMemoryGraphStore / Neo4jGraphStore) return
    # list[UnresolvedCallNode] dataclasses; the test harness returns
    # list[dict]. ``check_complete`` accepts either via ``_gap_id``.


def query_reachable(source_id: str, store: GraphStoreProtocol) -> dict[str, Any]:
    """Query the reachable subgraph from a source point."""
    return store.get_reachable_subgraph(source_id)


def write_edge(
    caller_id: str,
    callee_id: str,
    call_type: str,
    call_file: str,
    call_line: int,
    store: GraphStoreProtocol,
    llm_response: str = "",
    reasoning_summary: str = "",
) -> dict[str, Any]:
    """Write a CALLS edge, create RepairLog, and delete the UnresolvedCall."""
    # architecture.md §4: call_type ∈ {direct, indirect, virtual}
    _VALID_CALL_TYPES = {"direct", "indirect", "virtual"}
    if call_type not in _VALID_CALL_TYPES:
        raise ValueError(
            f"call_type must be one of {sorted(_VALID_CALL_TYPES)}, got {call_type!r}"
        )

    # Check if edge already exists (skip if so)
    if store.edge_exists(caller_id, callee_id, call_file, call_line):
        return {"skipped": True, "reason": "edge already exists"}

    # Create the CALLS edge
    from codemap_lite.graph.schema import CallsEdgeProps as _CallsEdgeProps

    props = _CallsEdgeProps(
        resolved_by="llm",
        call_type=call_type,
        call_file=call_file,
        call_line=call_line,
    )
    store.create_calls_edge(caller_id, callee_id, props)

    # Create RepairLog (architecture.md §4 RepairLog schema + ADR #51
    # 属性引用契约: caller_id + callee_id + call_location 三元组定位
    # 该边的修复过程, 不通过关系边). The ``llm_response`` +
    # ``reasoning_summary`` fields default to empty strings so legacy
    # callers keep working — the agent prompt now forwards both, but
    # the static-analysis path that pre-creates symbol-table edges
    # does not. Lazy-import ``RepairLogNode`` so the subprocess CLI's
    # ``--help`` works without codemap_lite on sys.path (the in-process
    # repair path already has it).
    from datetime import datetime, timezone

    from codemap_lite.graph.schema import RepairLogNode

    repair_log = RepairLogNode(
        caller_id=caller_id,
        callee_id=callee_id,
        call_location=f"{call_file}:{call_line}",
        repair_method="llm",
        llm_response=llm_response,
        timestamp=datetime.now(timezone.utc).isoformat(),
        reasoning_summary=reasoning_summary,
    )
    store.create_repair_log(repair_log)

    # Delete the UnresolvedCall
    store.delete_unresolved_call(caller_id, call_file, call_line)

    return {"skipped": False, "edge_created": True}


def _gap_id(gap: Any) -> str:
    """Extract the ``id`` field from a pending-gap record.

    architecture.md §3 门禁机制: ``check-complete`` returns
    ``pending_gap_ids``, but the GraphStoreProtocol does not nail down
    whether stores hand back dataclasses or dicts. Real stores
    (InMemoryGraphStore / Neo4jGraphStore) return
    ``UnresolvedCallNode`` dataclasses; the test harness returns
    ``list[dict]``. Both shapes carry an ``id`` — accept either so the
    CLI does not raise ``TypeError: 'UnresolvedCallNode' object is not
    subscriptable`` against production stores.
    """
    if isinstance(gap, dict):
        return gap["id"]
    return gap.id


def check_complete(source_id: str, store: GraphStoreProtocol) -> dict[str, Any]:
    """Check if all reachable GAPs for a source point are resolved."""
    pending = store.get_pending_gaps_for_source(source_id)
    return {
        "complete": len(pending) == 0,
        "remaining_gaps": len(pending),
        "pending_gap_ids": [_gap_id(g) for g in pending],
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


# Default config path: same directory as this script (works when copied to
# .icslpreprocess_{source_id}/ by the orchestrator).
_DEFAULT_CONFIG_PATH = str(Path(__file__).parent / "config.yaml")


def _json_default(obj: Any) -> Any:
    """Serializer for objects emitted by store implementations."""
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _parse_config(config_path: Path) -> dict[str, Any]:
    """Minimal YAML reader for the ``neo4j:`` block.

    We avoid pulling PyYAML into the agent sandbox — the template only needs
    ``neo4j.uri`` / ``neo4j.user`` / ``neo4j.password`` and the file layout is
    fully controlled by ``repair_orchestrator._inject_files``.
    """
    text = config_path.read_text(encoding="utf-8")
    result: dict[str, Any] = {"neo4j": {}}
    section: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and line.endswith(":"):
            section = line[:-1].strip()
            result.setdefault(section, {})
            continue
        if section is None or ":" not in line:
            continue
        key, _, raw_value = line.strip().partition(":")
        value = raw_value.strip().strip('"').strip("'")
        result[section][key.strip()] = value
    return result


def _load_store(config_path: Path) -> GraphStoreProtocol:
    """Build a GraphStore instance from the agent-side config file.

    Separated from CLI plumbing so tests can monkey-patch it.
    """
    cfg = _parse_config(config_path)
    neo4j_cfg = cfg.get("neo4j", {})
    from codemap_lite.graph.neo4j_store import Neo4jGraphStore

    return Neo4jGraphStore(
        uri=neo4j_cfg.get("uri", "bolt://localhost:7687"),
        user=neo4j_cfg.get("user", "neo4j"),
        password=neo4j_cfg.get("password", ""),
    )  # type: ignore[return-value]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="icsl_tools",
        description="Agent-side graph operations for repair subprocess.",
    )
    parser.add_argument(
        "--config",
        default=_DEFAULT_CONFIG_PATH,
        help="Path to YAML config with the neo4j: section (default: %(default)s).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    qr = subparsers.add_parser(
        "query-reachable",
        help="Return the reachable subgraph from a source point as JSON.",
    )
    qr.add_argument("--source", required=True, help="Source point function id.")

    we = subparsers.add_parser(
        "write-edge",
        help="Create a CALLS edge + RepairLog and delete the UnresolvedCall.",
    )
    we.add_argument("--caller", required=True, help="Caller FunctionNode id.")
    we.add_argument("--callee", required=True, help="Callee FunctionNode id.")
    we.add_argument(
        "--call-type",
        required=True,
        choices=["direct", "indirect", "virtual"],
        help="Call type (direct / indirect / virtual).",
    )
    we.add_argument("--call-file", required=True, help="File of the call site.")
    we.add_argument(
        "--call-line", required=True, type=int, help="Line of the call site."
    )
    we.add_argument(
        "--llm-response",
        default="",
        help=(
            "Raw agent stdout/llm reply that produced the resolution; "
            "populates RepairLogNode.llm_response (architecture.md §4). "
            "Leave empty for static/symbol-table edges."
        ),
    )
    we.add_argument(
        "--reasoning-summary",
        default="",
        help=(
            "Human-readable summary of the agent's reasoning chain "
            "(≤200 chars recommended); populates "
            "RepairLogNode.reasoning_summary — surfaced by CallGraphView "
            "EdgeLlmInspector. Leave empty for non-llm paths."
        ),
    )

    cc = subparsers.add_parser(
        "check-complete",
        help="Check whether all reachable GAPs for a source are resolved.",
    )
    cc.add_argument("--source", required=True, help="Source point function id.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch to the matching in-process function.

    Returns a Unix-style exit code. All responses (success or error) are a
    single JSON object on stdout, so the agent can parse them uniformly.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config)

    try:
        store = _load_store(config_path)
    except FileNotFoundError as exc:
        json.dump(
            {"error": "config_not_found", "path": str(config_path), "detail": str(exc)},
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 2

    try:
        if args.command == "query-reachable":
            result = query_reachable(args.source, store)
        elif args.command == "write-edge":
            result = write_edge(
                caller_id=args.caller,
                callee_id=args.callee,
                call_type=args.call_type,
                call_file=args.call_file,
                call_line=args.call_line,
                store=store,
                llm_response=args.llm_response,
                reasoning_summary=args.reasoning_summary,
            )
        elif args.command == "check-complete":
            result = check_complete(args.source, store)
        else:  # pragma: no cover — argparse already enforces required=True
            parser.error(f"unknown command: {args.command}")
            return 2
    except NotImplementedError as exc:
        json.dump(
            {"error": "store_not_available", "detail": str(exc)}, sys.stdout
        )
        sys.stdout.write("\n")
        return 3
    except Exception as exc:  # noqa: BLE001 — CLI boundary, surface as JSON
        json.dump(
            {"error": type(exc).__name__, "detail": str(exc)}, sys.stdout
        )
        sys.stdout.write("\n")
        return 1

    json.dump(result, sys.stdout, default=_json_default)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess tests
    raise SystemExit(main())

"""codemap-lite CLI — Typer application.

Implements the four commands declared in ``docs/architecture.md §9``
(ADR #50): ``analyze`` / ``repair`` / ``serve`` / ``status``. Each
command reads the shared ``config.yaml`` via :class:`Settings`.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

app = typer.Typer(help="codemap-lite: function-level call graph construction + indirect call repair")


def _load_settings(config: str):
    """Load Settings from YAML, exiting with a clear message if missing/invalid."""
    from pydantic import ValidationError

    from codemap_lite.config.settings import Settings

    path = Path(config)
    if not path.exists():
        typer.echo(f"error: config file not found: {path}", err=True)
        raise typer.Exit(code=2)
    try:
        return Settings.from_yaml(path)
    except ValidationError as exc:
        typer.echo(f"error: invalid config: {exc}", err=True)
        raise typer.Exit(code=2)


@app.command()
def analyze(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config.yaml"),
    incremental: bool = typer.Option(False, "--incremental", help="Run incremental analysis"),
    auto_repair: bool = typer.Option(
        False,
        "--auto-repair",
        help="After incremental analysis, automatically trigger repair "
        "for affected sources (architecture.md §7 step 5).",
    ),
):
    """Parse target code and build call graph in Neo4j."""
    from codemap_lite.pipeline.orchestrator import PipelineOrchestrator

    settings = _load_settings(config)
    target_dir = Path(settings.project.target_dir)

    # Build optional source_point_client for §7 step 4
    sp_client = None
    try:
        from codemap_lite.analysis.source_point_client import SourcePointClient
        sp_client = SourcePointClient(base_url=settings.codewiki_lite.base_url)
    except Exception:
        pass  # non-critical: proceed without SP re-fetch

    orch = PipelineOrchestrator(
        target_dir=target_dir,
        source_point_client=sp_client,
    )

    if incremental:
        result = orch.run_incremental_analysis()
        typer.echo(f"Incremental: {result.files_changed} files changed, {result.functions_found} functions updated")
        if result.affected_source_ids:
            typer.echo(f"  {len(result.affected_source_ids)} source(s) need re-repair: {', '.join(result.affected_source_ids[:5])}")

            # architecture.md §7 step 5: auto-trigger repair for affected sources
            if auto_repair:
                from codemap_lite.analysis.feedback_store import FeedbackStore
                from codemap_lite.analysis.repair_orchestrator import (
                    RepairConfig,
                    RepairOrchestrator,
                )

                command, args = _backend_subprocess(settings)
                feedback_store = FeedbackStore(
                    storage_dir=target_dir / ".codemap_lite" / "feedback"
                )
                graph_store = _build_graph_store(settings)

                repair_orch = RepairOrchestrator(
                    RepairConfig(
                        target_dir=target_dir,
                        backend=settings.agent.backend,
                        command=command,
                        args=args,
                        max_concurrency=settings.agent.max_concurrency,
                        neo4j_uri=settings.neo4j.uri,
                        neo4j_user=settings.neo4j.user,
                        neo4j_password=settings.neo4j.password,
                        feedback_store=feedback_store,
                        graph_store=graph_store,
                        subprocess_timeout_seconds=settings.agent.subprocess_timeout_seconds,
                    )
                )
                repair_result = asyncio.run(
                    repair_orch.run_repairs(result.affected_source_ids)
                )
                typer.echo(f"  Repair completed: {len(repair_result.successes)} sources repaired, {len(repair_result.failures)} failed")
    else:
        result = orch.run_full_analysis()
        typer.echo(f"Full: {result.files_scanned} files, {result.functions_found} functions, {result.direct_calls} calls, {result.unresolved_calls} gaps")


def _backend_subprocess(settings) -> tuple[str, list[str]]:
    """Pick (command, args) for the configured agent backend.

    Mirrors ``architecture.md §3 LLM 后端配置`` — backend ∈
    {claudecode, opencode}. Unknown backend is a hard error.
    """
    backend = settings.agent.backend
    if backend == "claudecode":
        cc = settings.agent.claudecode
        return cc.command, list(cc.args)
    if backend == "opencode":
        oc = settings.agent.opencode
        return oc.command, list(oc.args)
    raise typer.BadParameter(f"unknown agent.backend: {backend!r}")


def _build_graph_store(settings):
    """Construct the production GraphStore for retry audit write-back.

    architecture.md §3 Retry 审计字段 requires every gate failure to stamp
    ``last_attempt_timestamp`` / ``last_attempt_reason`` on each pending
    UnresolvedCall. RepairOrchestrator silently noops when ``graph_store``
    is unset, so the CLI must wire one through for the audit fields to
    actually land in Neo4j in production.

    Lazy-imported so unit tests that mock ``RepairOrchestrator`` don't
    need the neo4j driver on the import path.
    """
    from codemap_lite.graph.neo4j_store import Neo4jGraphStore

    return Neo4jGraphStore(
        uri=settings.neo4j.uri,
        user=settings.neo4j.user,
        password=settings.neo4j.password,
    )


@app.command()
def repair(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config.yaml"),
    source_points_file: str = typer.Option(
        None,
        "--source-points-file",
        help="Optional JSON file with source points (offline mode). "
        "When omitted, fetches from codewiki_lite REST API.",
    ),
    log_dir: str = typer.Option(
        None,
        "--log-dir",
        help="Directory to capture subprocess stdout/stderr per attempt.",
    ),
):
    """Run repair agents to resolve indirect calls.

    Fetches source points, instantiates :class:`RepairOrchestrator`
    with the configured agent backend, and runs repairs concurrently
    (``agent.max_concurrency``). Prints a per-source summary at the end.
    """
    from codemap_lite.analysis.feedback_store import FeedbackStore
    from codemap_lite.analysis.repair_orchestrator import (
        RepairConfig,
        RepairOrchestrator,
    )
    from codemap_lite.analysis.source_point_client import SourcePointClient

    settings = _load_settings(config)
    command, args = _backend_subprocess(settings)

    client = SourcePointClient(base_url=settings.codewiki_lite.base_url)
    if source_points_file:
        try:
            source_points = client.load_from_file(Path(source_points_file))
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            typer.echo(f"error: failed to load source points file: {exc}", err=True)
            raise typer.Exit(code=2)
    else:
        try:
            source_points = asyncio.run(client.fetch())
        except Exception as exc:
            typer.echo(
                f"error: failed to fetch source points from "
                f"{settings.codewiki_lite.base_url}: {exc}",
                err=True,
            )
            raise typer.Exit(code=2)

    if not source_points:
        typer.echo("No source points to repair.")
        return

    target_dir = Path(settings.project.target_dir)
    # Share the same persistent store `serve` uses so feedback submitted
    # via the API flows into the next repair run's CLAUDE.md injection
    # (architecture.md §3 反馈机制 step 4).
    feedback_store = FeedbackStore(
        storage_dir=target_dir / ".codemap_lite" / "feedback"
    )

    # architecture.md §3 Retry 审计字段: without a real graph_store the
    # orchestrator silently noops retry-audit stamping, so ReviewQueue
    # never sees "last attempt failed at <ts> because <reason>". Wire
    # the production Neo4jGraphStore here so audit fields land in prod.
    graph_store = _build_graph_store(settings)

    orch = RepairOrchestrator(
        RepairConfig(
            target_dir=target_dir,
            backend=settings.agent.backend,
            command=command,
            args=args,
            max_concurrency=settings.agent.max_concurrency,
            neo4j_uri=settings.neo4j.uri,
            neo4j_user=settings.neo4j.user,
            neo4j_password=settings.neo4j.password,
            log_dir=Path(log_dir) if log_dir else None,
            feedback_store=feedback_store,
            graph_store=graph_store,
            retry_failed_gaps=settings.agent.retry_failed_gaps,
            subprocess_timeout_seconds=settings.agent.subprocess_timeout_seconds,
        )
    )

    source_ids = [sp.function_id for sp in source_points]
    results = asyncio.run(orch.run_repairs(source_ids))

    succeeded = sum(1 for r in results if r.success)
    failed = len(results) - succeeded
    typer.echo(f"Repair summary: {succeeded} succeeded, {failed} failed (of {len(results)} source points)")
    for r in results:
        flag = "OK " if r.success else "FAIL"
        detail = f" — {r.error}" if r.error else ""
        typer.echo(f"  [{flag}] {r.source_id} (attempts={r.attempts}){detail}")


@app.command()
def status(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config.yaml"),
):
    """Show current analysis/repair progress.

    Reads ``<target>/.icslpreprocess/state.json`` (from the last
    ``analyze`` run) and any ``<target>/logs/repair/*/progress.json``
    left by the repair hooks (see ``architecture.md §3 Hook 机制``).
    """
    settings = _load_settings(config)
    target_dir = Path(settings.project.target_dir)

    state_path = target_dir / ".icslpreprocess" / "state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            files = state.get("files", {})
            typer.echo(f"Analysis state: {len(files)} files tracked ({state_path})")
        except (OSError, json.JSONDecodeError) as exc:
            typer.echo(f"Analysis state: unreadable ({exc})")
    else:
        typer.echo("Analysis state: not found (run `codemap-lite analyze` first)")

    repair_root = target_dir / "logs" / "repair"
    if not repair_root.exists():
        typer.echo("Repair progress: no runs yet")
        return

    progress_files = sorted(repair_root.glob("*/progress.json"))
    if not progress_files:
        typer.echo("Repair progress: no source points in progress")
        return

    typer.echo(f"Repair progress: {len(progress_files)} source points tracked")
    for pf in progress_files:
        source_id = pf.parent.name
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            typer.echo(f"  {source_id}: <unreadable>")
            continue
        fixed = data.get("gaps_fixed", 0)
        total = data.get("gaps_total", 0)
        current = data.get("current_gap", "-")
        typer.echo(f"  {source_id}: {fixed}/{total} gaps fixed (current={current})")


@app.command()
def serve(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config.yaml"),
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Host to bind to"),
    port: int = typer.Option(8000, "--port", "-p", help="Port to serve on"),
):
    """Start the FastAPI server (see architecture.md §8 REST API)."""
    import uvicorn

    from codemap_lite.analysis.feedback_store import FeedbackStore
    from codemap_lite.api.app import create_app

    settings = _load_settings(config)

    target_dir = Path(settings.project.target_dir)
    # Persistent counter-example store — distinct from the transient
    # ``.icslpreprocess/`` directory used by repair agents.
    # Backs ``GET /api/v1/feedback`` (architecture.md §3 反馈机制 + §8).
    feedback_store = FeedbackStore(storage_dir=target_dir / ".codemap_lite" / "feedback")

    # architecture.md §8 REST API: stats / graph / call-chain / review
    # routes all read from the same Neo4j the analyze + repair pipelines
    # write to. Without an explicit store, ``create_app`` falls back to
    # ``InMemoryGraphStore`` and every count returns 0 even though Neo4j
    # is fully populated — the frontend looks "disconnected" while the
    # backend is healthy. Wire the production graph store the same way
    # ``repair`` does (cli.py:138).
    graph_store = _build_graph_store(settings)

    # Pass target_dir through to the app so /api/v1/analyze/status can
    # aggregate logs/repair/*/progress.json (architecture.md §3, ADR #52).
    app_instance = create_app(
        store=graph_store,
        target_dir=target_dir,
        feedback_store=feedback_store,
        settings=settings,
    )

    # Populate app.state.source_points so GET /api/v1/source-points returns
    # data (architecture.md §8). Without this, the SourcePointList page is
    # always empty because create_app initializes source_points = [].
    from codemap_lite.analysis.source_point_client import SourcePointClient
    from dataclasses import asdict

    sp_client = SourcePointClient(base_url=settings.codewiki_lite.base_url)
    try:
        sp_list = asyncio.run(sp_client.fetch())
        app_instance.state.source_points = [asdict(sp) for sp in sp_list]
    except Exception:
        # Graceful fallback — source points are optional for the serve path.
        # The endpoint will return [] until the next analyze/repair populates them.
        pass

    uvicorn.run(app_instance, host=host, port=port)


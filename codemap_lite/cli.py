"""codemap-lite CLI — Typer application."""
from pathlib import Path

import typer

app = typer.Typer(help="codemap-lite: function-level call graph construction + indirect call repair")


@app.command()
def analyze(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config.yaml"),
    incremental: bool = typer.Option(False, "--incremental", help="Run incremental analysis"),
):
    """Parse target code and build call graph in Neo4j."""
    from codemap_lite.config.settings import Settings
    from codemap_lite.pipeline.orchestrator import PipelineOrchestrator

    settings = Settings.from_yaml(config)
    orch = PipelineOrchestrator(target_dir=Path(settings.project.target_dir))

    if incremental:
        result = orch.run_incremental_analysis()
        typer.echo(f"Incremental: {result.files_changed} files changed, {result.functions_found} functions updated")
    else:
        result = orch.run_full_analysis()
        typer.echo(f"Full: {result.files_scanned} files, {result.functions_found} functions, {result.direct_calls} calls, {result.unresolved_calls} gaps")


@app.command()
def repair(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config.yaml"),
):
    """Run repair agents to resolve indirect calls."""
    typer.echo(f"Repairing with config={config}")


@app.command()
def status(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config.yaml"),
):
    """Show current analysis/repair progress."""
    typer.echo("Status: idle")


@app.command()
def serve(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config.yaml"),
    port: int = typer.Option(8000, "--port", "-p", help="Port to serve on"),
):
    """Start the FastAPI server."""
    typer.echo(f"Serving on port {port}")


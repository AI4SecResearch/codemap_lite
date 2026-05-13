"""Tests for CLI interface."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codemap_lite import cli as cli_module
from codemap_lite.analysis.repair_orchestrator import SourceRepairResult
from codemap_lite.analysis.source_point_client import SourcePointInfo
from codemap_lite.cli import app

runner = CliRunner()


def test_cli_shows_four_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "analyze" in result.output
    assert "repair" in result.output
    assert "status" in result.output
    assert "serve" in result.output


# --- Helpers -----------------------------------------------------------------


def _write_config(tmp_path: Path, target_subdir: str = "target") -> Path:
    """Write a minimal config.yaml that points at ``tmp_path/<target_subdir>``."""
    target = tmp_path / target_subdir
    target.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "project:\n"
        f'  target_dir: "{target.as_posix()}"\n'
        "neo4j:\n"
        '  uri: "bolt://localhost:7687"\n'
        '  user: "neo4j"\n'
        '  password: "pw"\n'
        "codewiki_lite:\n"
        '  base_url: "http://localhost:7777"\n'
        "agent:\n"
        '  backend: "claudecode"\n'
        "  max_concurrency: 3\n"
        "  claudecode:\n"
        '    command: "claude"\n'
        '    args: ["-p"]\n',
        encoding="utf-8",
    )
    return cfg


# --- repair ------------------------------------------------------------------


def test_repair_missing_config_exits_cleanly(tmp_path):
    result = runner.invoke(app, ["repair", "--config", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 2
    assert "config file not found" in (result.stderr or result.output)


def test_repair_loads_sources_from_file_and_invokes_orchestrator(tmp_path):
    cfg = _write_config(tmp_path)
    sp_path = tmp_path / "sources.json"
    sp_path.write_text(
        json.dumps(
            [
                {"function_id": "src_001", "entry_point_kind": "api", "reason": "", "module": "m"},
                {"function_id": "src_002", "entry_point_kind": "cli", "reason": "", "module": "m"},
            ]
        ),
        encoding="utf-8",
    )

    fake_orch = MagicMock()

    async def _fake_run(ids):
        return [
            SourceRepairResult(source_id=i, success=(i == "src_001"), attempts=1,
                               error=None if i == "src_001" else "gate failed")
            for i in ids
        ]

    fake_orch.run_repairs.side_effect = _fake_run

    with patch.object(cli_module, "RepairOrchestrator", return_value=fake_orch, create=True) as orch_ctor:
        # RepairOrchestrator is imported inside the function — patch the
        # symbol on the imported module instead.
        with patch("codemap_lite.analysis.repair_orchestrator.RepairOrchestrator",
                   return_value=fake_orch):
            result = runner.invoke(
                app,
                ["repair", "--config", str(cfg), "--source-points-file", str(sp_path)],
            )

    assert result.exit_code == 0, result.output
    assert "Repair summary: 1 succeeded, 1 failed" in result.output
    assert "src_001" in result.output and "src_002" in result.output
    fake_orch.run_repairs.assert_called_once()
    assert fake_orch.run_repairs.call_args.args[0] == ["src_001", "src_002"]


def test_repair_wires_graph_store_into_repair_config(tmp_path):
    """architecture.md §3 Retry 审计字段: CLI must thread a real
    graph_store into RepairConfig so last_attempt_{timestamp,reason}
    land on pending GAPs after each gate failure. Without wiring,
    ``_record_retry_attempt`` silently noops and ReviewQueue never
    surfaces the failure context.
    """
    cfg = _write_config(tmp_path)
    sp_path = tmp_path / "sources.json"
    sp_path.write_text(
        json.dumps(
            [{"function_id": "src_001", "entry_point_kind": "api", "reason": "", "module": "m"}]
        ),
        encoding="utf-8",
    )

    fake_orch = MagicMock()

    async def _fake_run(ids):
        return [SourceRepairResult(source_id=i, success=True, attempts=1) for i in ids]

    fake_orch.run_repairs.side_effect = _fake_run

    fake_graph_store = MagicMock(name="Neo4jGraphStore")
    with patch(
        "codemap_lite.cli._build_graph_store", return_value=fake_graph_store
    ) as build_gs:
        with patch(
            "codemap_lite.analysis.repair_orchestrator.RepairOrchestrator",
            return_value=fake_orch,
        ) as orch_ctor:
            result = runner.invoke(
                app,
                ["repair", "--config", str(cfg), "--source-points-file", str(sp_path)],
            )

    assert result.exit_code == 0, result.output
    build_gs.assert_called_once()
    orch_ctor.assert_called_once()
    repair_config = orch_ctor.call_args.args[0]
    assert repair_config.graph_store is fake_graph_store


def test_build_graph_store_returns_neo4j_graph_store(tmp_path):
    """_build_graph_store must return a Neo4jGraphStore wired to
    settings.neo4j.{uri,user,password} (architecture.md §9 tech stack).
    """
    cfg = _write_config(tmp_path)
    settings = cli_module._load_settings(str(cfg))

    with patch(
        "codemap_lite.graph.neo4j_store.Neo4jGraphStore"
    ) as neo4j_ctor:
        cli_module._build_graph_store(settings)

    neo4j_ctor.assert_called_once_with(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="pw",
    )


def test_repair_prints_message_when_no_source_points(tmp_path):
    cfg = _write_config(tmp_path)
    sp_path = tmp_path / "sources.json"
    sp_path.write_text("[]", encoding="utf-8")

    result = runner.invoke(
        app, ["repair", "--config", str(cfg), "--source-points-file", str(sp_path)]
    )
    assert result.exit_code == 0
    assert "No source points to repair." in result.output


def test_repair_rejects_unknown_backend(tmp_path):
    cfg = _write_config(tmp_path)
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace('backend: "claudecode"', 'backend: "wat"'),
        encoding="utf-8",
    )
    sp_path = tmp_path / "sources.json"
    sp_path.write_text(
        json.dumps([{"function_id": "src_001", "entry_point_kind": "api", "reason": "", "module": "m"}]),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["repair", "--config", str(cfg), "--source-points-file", str(sp_path)]
    )
    assert result.exit_code != 0
    # Pydantic ValidationError is caught by _load_settings and echoed
    assert "wat" in result.output or "invalid config" in result.output


# --- status ------------------------------------------------------------------


def test_status_reports_when_no_state(tmp_path):
    cfg = _write_config(tmp_path)
    result = runner.invoke(app, ["status", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "Analysis state: not found" in result.output
    assert "Repair progress: no runs yet" in result.output


def test_status_reports_state_and_progress(tmp_path):
    cfg = _write_config(tmp_path)
    target = tmp_path / "target"

    # Simulate a completed analyze run.
    (target / ".icslpreprocess").mkdir(parents=True, exist_ok=True)
    (target / ".icslpreprocess" / "state.json").write_text(
        json.dumps({"files": {"a.cpp": {"hash": "x"}, "b.cpp": {"hash": "y"}}}),
        encoding="utf-8",
    )

    # Simulate repair hooks having populated progress.json for a source.
    progress_dir = target / "logs" / "repair" / "src_001"
    progress_dir.mkdir(parents=True, exist_ok=True)
    (progress_dir / "progress.json").write_text(
        json.dumps({"gaps_fixed": 2, "gaps_total": 5, "current_gap": "gap_003"}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["status", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "Analysis state: 2 files tracked" in result.output
    assert "src_001: 2/5 gaps fixed" in result.output
    assert "gap_003" in result.output


# --- serve -------------------------------------------------------------------


def test_serve_launches_uvicorn_with_create_app(tmp_path):
    cfg = _write_config(tmp_path)

    fake_uvicorn = MagicMock()
    fake_app = MagicMock(name="FastAPIApp")
    with patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
        with patch("codemap_lite.api.app.create_app", return_value=fake_app) as create_app:
            result = runner.invoke(
                app, ["serve", "--config", str(cfg), "--port", "9123", "--host", "127.0.0.1"]
            )

    assert result.exit_code == 0, result.output
    # target_dir is wired through so /analyze/status can aggregate
    # logs/repair/*/progress.json (architecture.md §3, ADR #52).
    # feedback_store is wired through so GET /api/v1/feedback can
    # browse persisted counter examples (architecture.md §3 反馈机制 + §8).
    create_app.assert_called_once()
    kwargs = create_app.call_args.kwargs
    assert kwargs["target_dir"] == (tmp_path / "target")
    feedback_store = kwargs["feedback_store"]
    # FeedbackStore rooted at <target>/.codemap_lite/feedback — persistent,
    # distinct from the transient .icslpreprocess/ dir.
    assert feedback_store._storage_dir == (
        tmp_path / "target" / ".codemap_lite" / "feedback"
    )
    # architecture.md §8 REST API: serve must wire the same Neo4j store
    # the analyze + repair pipelines write to. Without this, /api/v1/stats
    # and friends return 0 even though Neo4j is fully populated — a silent
    # disconnection between backend and frontend.
    from codemap_lite.graph.neo4j_store import Neo4jGraphStore
    assert isinstance(kwargs["store"], Neo4jGraphStore)
    fake_uvicorn.run.assert_called_once_with(fake_app, host="127.0.0.1", port=9123)


def test_repair_handles_malformed_source_points_file(tmp_path):
    """CLI must exit gracefully when source-points-file is malformed."""
    cfg = _write_config(tmp_path)
    sp_path = tmp_path / "sources.json"
    sp_path.write_text("not valid json {{{", encoding="utf-8")

    result = runner.invoke(
        app, ["repair", "--config", str(cfg), "--source-points-file", str(sp_path)]
    )
    assert result.exit_code == 2
    assert "error" in result.output.lower() or "error" in (result.stderr or "").lower()


def test_repair_handles_fetch_failure_gracefully(tmp_path):
    """CLI must exit gracefully when codewiki_lite is unreachable."""
    from unittest.mock import patch, AsyncMock

    cfg = _write_config(tmp_path)

    with patch(
        "codemap_lite.analysis.source_point_client.SourcePointClient.fetch",
        new_callable=AsyncMock,
        side_effect=Exception("Connection refused"),
    ):
        result = runner.invoke(app, ["repair", "--config", str(cfg)])

    assert result.exit_code == 2
    assert "Connection refused" in result.output or "Connection refused" in (result.stderr or "")

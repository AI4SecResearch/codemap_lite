"""CLI commands contract tests — architecture.md §9.

Tests the Typer CLI commands (analyze, repair, status, serve) using
CliRunner for invocation and mocking for external dependencies.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from typer.testing import CliRunner

from codemap_lite.cli import app


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = """\
project:
  target_dir: "{target_dir}"
  name: test_project

neo4j:
  uri: bolt://localhost:7687
  user: neo4j
  password: test

codewiki_lite:
  base_url: http://localhost:9000

agent:
  backend: opencode
  max_concurrency: 2
  retry_failed_gaps: true
  subprocess_timeout_seconds: 60
  opencode:
    command: opencode
    args: ["-p"]
  claudecode:
    command: claude
    args: ["-p", "--output-format", "text"]

visualization:
  layout: hierarchical

feedback:
  storage_dir: .codemap_lite/feedback
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """Create a minimal config.yaml for CLI tests."""
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    # Create a minimal C++ file so analyze has something to parse
    (target_dir / "test.cpp").write_text(
        "void foo() {}\nvoid bar() { foo(); }\n", encoding="utf-8"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        MINIMAL_CONFIG.format(target_dir=str(target_dir).replace("\\", "/")),
        encoding="utf-8",
    )
    return config_path


# ---------------------------------------------------------------------------
# Test: analyze command
# ---------------------------------------------------------------------------

class TestAnalyzeCommand:
    """architecture.md §9: codemap-lite analyze."""

    def test_missing_config_exits_2(self, tmp_path: Path):
        result = runner.invoke(app, ["analyze", "--config", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 2

    @patch("codemap_lite.cli._build_graph_store")
    def test_full_analysis_runs(self, mock_store, config_file: Path):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        mock_store.return_value = InMemoryGraphStore()
        result = runner.invoke(app, ["analyze", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "Full:" in result.output
        assert "files" in result.output

    @patch("codemap_lite.cli._build_graph_store")
    def test_incremental_after_full(self, mock_store, config_file: Path):
        """Incremental after full analysis → 0 changes (nothing modified)."""
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        store = InMemoryGraphStore()
        mock_store.return_value = store
        # First run full to create state.json
        result = runner.invoke(app, ["analyze", "--config", str(config_file)])
        assert result.exit_code == 0
        # Then run incremental
        result = runner.invoke(app, ["analyze", "--config", str(config_file), "--incremental"])
        assert result.exit_code == 0
        assert "Incremental:" in result.output


# ---------------------------------------------------------------------------
# Test: repair command
# ---------------------------------------------------------------------------

class TestRepairCommand:
    """architecture.md §9: codemap-lite repair."""

    def test_missing_config_exits_2(self, tmp_path: Path):
        result = runner.invoke(app, ["repair", "--config", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 2

    def test_bad_source_points_file_exits_2(self, config_file: Path, tmp_path: Path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json at all {{{", encoding="utf-8")
        with patch("codemap_lite.cli._build_graph_store") as mock_store:
            from codemap_lite.graph.neo4j_store import InMemoryGraphStore
            mock_store.return_value = InMemoryGraphStore()
            result = runner.invoke(app, [
                "repair", "--config", str(config_file),
                "--source-points-file", str(bad_file),
            ])
        assert result.exit_code == 2

    @patch("codemap_lite.cli._build_graph_store")
    def test_empty_source_points_no_repair(self, mock_store, config_file: Path, tmp_path: Path):
        """Empty source points file → 'No source points to repair'."""
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        mock_store.return_value = InMemoryGraphStore()
        sp_file = tmp_path / "empty_sp.json"
        # load_from_file expects a raw list
        sp_file.write_text(json.dumps([]), encoding="utf-8")
        result = runner.invoke(app, [
            "repair", "--config", str(config_file),
            "--source-points-file", str(sp_file),
        ])
        assert result.exit_code == 0
        assert "No source points" in result.output


# ---------------------------------------------------------------------------
# Test: status command
# ---------------------------------------------------------------------------

class TestStatusCommand:
    """architecture.md §9: codemap-lite status."""

    def test_missing_config_exits_2(self, tmp_path: Path):
        result = runner.invoke(app, ["status", "--config", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 2

    def test_no_state_file(self, config_file: Path):
        result = runner.invoke(app, ["status", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "not found" in result.output

    def test_with_progress_files(self, config_file: Path, tmp_path: Path):
        """Status reads progress.json files from logs/repair/."""
        # Parse config to find target_dir
        import yaml
        cfg = yaml.safe_load(config_file.read_text())
        target_dir = Path(cfg["project"]["target_dir"])

        # Create progress file
        progress_dir = target_dir / "logs" / "repair" / "source_001"
        progress_dir.mkdir(parents=True)
        (progress_dir / "progress.json").write_text(
            json.dumps({
                "source_id": "source_001",
                "gaps_fixed": 3,
                "gaps_total": 5,
                "current_gap": "uc_123",
            }),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["status", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "source_001" in result.output
        assert "3/5" in result.output


# ---------------------------------------------------------------------------
# Test: serve command (just validates it starts without crashing)
# ---------------------------------------------------------------------------

class TestServeCommand:
    """architecture.md §9: codemap-lite serve."""

    def test_missing_config_exits_2(self, tmp_path: Path):
        result = runner.invoke(app, ["serve", "--config", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 2

    @patch("uvicorn.run")
    @patch("codemap_lite.cli._build_graph_store")
    def test_serve_starts_uvicorn(self, mock_store, mock_uvicorn, config_file: Path):
        from codemap_lite.graph.neo4j_store import InMemoryGraphStore
        mock_store.return_value = InMemoryGraphStore()
        result = runner.invoke(app, ["serve", "--config", str(config_file), "--port", "9999"])
        assert result.exit_code == 0
        mock_uvicorn.assert_called_once()
        # Verify port was passed
        call_kwargs = mock_uvicorn.call_args
        assert call_kwargs.kwargs.get("port") == 9999 or call_kwargs[1].get("port") == 9999

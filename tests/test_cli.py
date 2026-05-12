"""Tests for CLI interface."""
from typer.testing import CliRunner

from codemap_lite.cli import app

runner = CliRunner()


def test_cli_shows_four_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "analyze" in result.output
    assert "repair" in result.output
    assert "status" in result.output
    assert "serve" in result.output

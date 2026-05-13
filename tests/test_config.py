"""Tests for configuration loading."""
import os
import tempfile
from pathlib import Path

from codemap_lite.config.settings import Settings


def test_settings_from_yaml_loads_all_sections():
    yaml_content = """\
project:
  target_dir: "/tmp/code"
neo4j:
  uri: "bolt://localhost:7687"
  user: "neo4j"
  password: "secret"
codewiki_lite:
  base_url: "http://localhost:9000"
agent:
  backend: "opencode"
  max_concurrency: 3
  retry_failed_gaps: false
  claudecode:
    command: "claude"
    args: ["-p", "--output-format", "text"]
  opencode:
    command: "opencode"
    args: ["-p"]
visualization:
  aggregation: "hierarchical"
feedback:
  model: "qwen-max"
  base_url: "https://example.com/v1"
  api_key: "test-key"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        settings = Settings.from_yaml(f.name)

    os.unlink(f.name)

    assert settings.project.target_dir == "/tmp/code"
    assert settings.neo4j.uri == "bolt://localhost:7687"
    assert settings.neo4j.password == "secret"
    assert settings.codewiki_lite.base_url == "http://localhost:9000"
    assert settings.agent.backend == "opencode"
    assert settings.agent.max_concurrency == 3
    assert settings.agent.retry_failed_gaps is False
    assert settings.visualization.aggregation == "hierarchical"
    assert settings.feedback.model == "qwen-max"


def test_settings_env_var_interpolation():
    os.environ["TEST_NEO4J_PASS"] = "from_env"
    yaml_content = """\
neo4j:
  password: "${TEST_NEO4J_PASS}"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        settings = Settings.from_yaml(f.name)

    os.unlink(f.name)
    del os.environ["TEST_NEO4J_PASS"]

    assert settings.neo4j.password == "from_env"


def test_settings_defaults():
    settings = Settings()
    assert settings.project.target_dir == "."
    assert settings.neo4j.uri == "bolt://localhost:7687"
    assert settings.agent.backend == "claudecode"
    assert settings.agent.max_concurrency == 5
    # architecture.md §3 超时护栏: default None = no timeout
    assert settings.agent.subprocess_timeout_seconds is None


def test_subprocess_timeout_seconds_from_yaml(tmp_path):
    """architecture.md §3 超时护栏 + §10: subprocess_timeout_seconds must be
    configurable via agent section in config.yaml."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "agent:\n"
        "  subprocess_timeout_seconds: 120.5\n"
    )
    settings = Settings.from_yaml(config_file)
    assert settings.agent.subprocess_timeout_seconds == 120.5

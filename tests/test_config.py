"""Tests for configuration loading."""
import os
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from codemap_lite.config.settings import AgentConfig, Settings


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


class TestAgentConfigBackendEnum:
    """architecture.md §10 配置校验: agent.backend must be enum-constrained."""

    def test_valid_backends_accepted(self):
        """Both 'claudecode' and 'opencode' are valid backend values."""
        assert AgentConfig(backend="claudecode").backend == "claudecode"
        assert AgentConfig(backend="opencode").backend == "opencode"

    def test_invalid_backend_raises_validation_error(self):
        """Invalid backend value must raise ValidationError at config load time."""
        with pytest.raises(ValidationError):
            AgentConfig(backend="invalid_backend")

    def test_invalid_backend_in_yaml_raises(self, tmp_path):
        """Loading a YAML with invalid agent.backend must fail validation."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("agent:\n  backend: \"not_a_backend\"\n")
        with pytest.raises(ValidationError):
            Settings.from_yaml(config_file)


class TestAgentConfigConstraints:
    """architecture.md §10: agent config field constraints."""

    def test_max_concurrency_must_be_positive(self):
        """architecture.md §10: max_concurrency must be a positive integer (≥1)."""
        with pytest.raises(ValidationError):
            AgentConfig(max_concurrency=0)
        with pytest.raises(ValidationError):
            AgentConfig(max_concurrency=-1)

    def test_max_concurrency_accepts_valid_values(self):
        assert AgentConfig(max_concurrency=1).max_concurrency == 1
        assert AgentConfig(max_concurrency=10).max_concurrency == 10

    def test_subprocess_timeout_must_be_positive_if_set(self):
        """architecture.md §3 超时护栏: subprocess_timeout_seconds must be > 0 if set."""
        with pytest.raises(ValidationError):
            AgentConfig(subprocess_timeout_seconds=0)
        with pytest.raises(ValidationError):
            AgentConfig(subprocess_timeout_seconds=-5.0)

    def test_subprocess_timeout_none_is_valid(self):
        """None means no timeout (architecture.md §3: Agent 自然完成)."""
        assert AgentConfig(subprocess_timeout_seconds=None).subprocess_timeout_seconds is None

    def test_subprocess_timeout_positive_is_valid(self):
        assert AgentConfig(subprocess_timeout_seconds=120.5).subprocess_timeout_seconds == 120.5


def test_default_config_yaml_matches_architecture_spec():
    """architecture.md §10: default_config.yaml must produce settings that
    match the architecture specification exactly.

    Verifies:
    - agent.backend = "claudecode"
    - agent.max_concurrency = 5
    - agent.retry_failed_gaps = true
    - claudecode.command = "claude", args = ["-p", "--output-format", "text"]
    - opencode.command = "opencode", args = ["-p"]
    - neo4j defaults
    - visualization.aggregation = "hierarchical"
    """
    config_path = Path(__file__).parent.parent / "codemap_lite" / "config" / "default_config.yaml"
    settings = Settings.from_yaml(config_path)

    # §10 agent section
    assert settings.agent.backend == "claudecode"
    assert settings.agent.max_concurrency == 5
    assert settings.agent.retry_failed_gaps is True
    assert settings.agent.subprocess_timeout_seconds is None

    # §10 claudecode nested config
    assert settings.agent.claudecode.command == "claude"
    assert settings.agent.claudecode.args == ["-p", "--output-format", "text"]

    # §10 opencode nested config — architecture specifies args: ["-p"]
    assert settings.agent.opencode.command == "opencode"
    assert settings.agent.opencode.args == ["-p"]

    # §10 neo4j defaults
    assert settings.neo4j.uri == "bolt://localhost:7687"
    assert settings.neo4j.user == "neo4j"

    # §10 visualization
    assert settings.visualization.aggregation == "hierarchical"

"""Configuration management — Pydantic Settings with YAML + env var support."""
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class ProjectConfig(BaseModel):
    target_dir: str = "."


class Neo4jConfig(BaseModel):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = ""


class CodewikiLiteConfig(BaseModel):
    base_url: str = "http://localhost:8000"


class ClaudeCodeConfig(BaseModel):
    command: str = "claude"
    args: list[str] = Field(default_factory=lambda: ["-p", "--output-format", "text"])


class OpenCodeConfig(BaseModel):
    command: str = "opencode"
    args: list[str] = Field(default_factory=lambda: ["-p"])


class AgentConfig(BaseModel):
    backend: str = "claudecode"
    max_concurrency: int = 5
    retry_failed_gaps: bool = True
    claudecode: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)


class VisualizationConfig(BaseModel):
    aggregation: str = "hierarchical"


class FeedbackConfig(BaseModel):
    model: str = "qwen-plus"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""


class Settings(BaseSettings):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    codewiki_lite: CodewikiLiteConfig = Field(default_factory=CodewikiLiteConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    visualization: VisualizationConfig = Field(default_factory=VisualizationConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Settings":
        """Load settings from a YAML file with ${VAR_NAME} env var interpolation."""
        path = Path(path)
        raw = path.read_text(encoding="utf-8")
        expanded = os.path.expandvars(raw)
        data: dict[str, Any] = yaml.safe_load(expanded) or {}
        return cls(**data)


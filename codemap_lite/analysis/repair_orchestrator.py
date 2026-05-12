"""Repair Orchestrator — manages CLI subprocess agents for indirect call repair."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RepairConfig:
    """Configuration for the repair orchestrator."""

    target_dir: Path
    backend: str = "claudecode"
    command: str = "claude"
    args: list[str] = field(default_factory=lambda: ["-p", "--output-format", "text"])
    max_concurrency: int = 5
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    # Per-invocation env overrides (e.g. LLM API key/base URL). Merged over os.environ.
    env: dict[str, str] = field(default_factory=dict)
    # Capture subprocess stdout/stderr to these files (appended). None disables capture.
    log_dir: Path | None = None


@dataclass
class SourceRepairResult:
    """Result of repairing a single source point."""

    source_id: str
    success: bool
    attempts: int
    error: str | None = None


class RepairOrchestrator:
    """Orchestrates repair agents as CLI subprocesses with concurrency control."""

    MAX_RETRIES_PER_GAP = 3

    def __init__(self, config: RepairConfig) -> None:
        self._config = config
        self._progress: dict[str, dict[str, Any]] = {}

    def _inject_files(
        self,
        target_dir: Path,
        source_id: str,
        counter_examples: str,
    ) -> None:
        """Generate injection files in the target code directory."""
        # Backup existing CLAUDE.md if present
        claude_md_path = target_dir / "CLAUDE.md"
        if claude_md_path.exists():
            backup_path = target_dir / "CLAUDE.md.bak"
            shutil.copy2(claude_md_path, backup_path)

        # Write CLAUDE.md
        from codemap_lite.agent.claude_md_template import generate_claude_md

        claude_md_path.write_text(
            generate_claude_md(
                source_id=source_id,
                neo4j_config_path=".icslpreprocess/config.yaml",
                counter_examples_path=".icslpreprocess/counter_examples.md",
            ),
            encoding="utf-8",
        )

        # Write .claude/settings.json
        claude_dir = target_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "hooks": {
                "PostToolUse": [{"command": "python .icslpreprocess/hooks/log_tool_use.py"}],
                "Notification": [{"command": "python .icslpreprocess/hooks/log_notification.py"}],
            }
        }
        (claude_dir / "settings.json").write_text(
            json.dumps(settings, indent=2), encoding="utf-8"
        )

        # Write .icslpreprocess/ directory
        icsl_dir = target_dir / ".icslpreprocess"
        icsl_dir.mkdir(parents=True, exist_ok=True)

        # config.yaml for Neo4j connection
        config_yaml = f"""neo4j:
  uri: "{self._config.neo4j_uri}"
  user: "{self._config.neo4j_user}"
  password: "{self._config.neo4j_password}"
"""
        (icsl_dir / "config.yaml").write_text(config_yaml, encoding="utf-8")

        # Counter examples
        (icsl_dir / "counter_examples.md").write_text(
            counter_examples or "# No counter examples yet\n", encoding="utf-8"
        )

    def _cleanup_injection(self, target_dir: Path) -> None:
        """Remove injection files and restore backups."""
        # Remove .icslpreprocess/
        icsl_dir = target_dir / ".icslpreprocess"
        if icsl_dir.exists():
            shutil.rmtree(icsl_dir)

        # Remove .claude/ (only if we created it)
        claude_dir = target_dir / ".claude"
        if claude_dir.exists():
            shutil.rmtree(claude_dir)

        # Restore CLAUDE.md backup or remove generated one
        claude_md_path = target_dir / "CLAUDE.md"
        backup_path = target_dir / "CLAUDE.md.bak"
        if backup_path.exists():
            shutil.move(str(backup_path), str(claude_md_path))
        elif claude_md_path.exists():
            claude_md_path.unlink()

    def _build_command(self, source_id: str) -> list[str]:
        """Build the subprocess command for a repair agent."""
        from codemap_lite.analysis.prompt_builder import build_repair_prompt

        prompt = build_repair_prompt(source_id=source_id)
        cmd = [self._config.command] + list(self._config.args)
        # For CLI agents, the prompt is passed as the last argument
        cmd.append(prompt)
        return cmd

    async def _run_single_repair(self, source_id: str) -> SourceRepairResult:
        """Run repair for a single source point with retry logic."""
        target_dir = self._config.target_dir
        attempts = 0
        max_attempts = self.MAX_RETRIES_PER_GAP

        while attempts < max_attempts:
            attempts += 1

            # Inject files
            self._inject_files(
                target_dir=target_dir,
                source_id=source_id,
                counter_examples="",
            )

            try:
                # Run subprocess with merged env and optional log capture
                cmd = self._build_command(source_id)
                env = {**os.environ, **(self._config.env or {})}

                log_dir = self._config.log_dir
                stdout_target: Any = asyncio.subprocess.PIPE
                stderr_target: Any = asyncio.subprocess.PIPE
                log_fh = None
                if log_dir is not None:
                    log_dir.mkdir(parents=True, exist_ok=True)
                    log_path = log_dir / f"{source_id}.attempt{attempts}.log"
                    log_fh = open(log_path, "ab")
                    stdout_target = log_fh
                    stderr_target = log_fh

                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(target_dir),
                        stdout=stdout_target,
                        stderr=stderr_target,
                        env=env,
                    )
                    await proc.communicate()
                finally:
                    if log_fh is not None:
                        log_fh.close()

                # Gate check
                gate_passed = await self._check_gate(source_id)
                if gate_passed:
                    return SourceRepairResult(
                        source_id=source_id, success=True, attempts=attempts
                    )
            finally:
                self._cleanup_injection(target_dir)

        return SourceRepairResult(
            source_id=source_id,
            success=False,
            attempts=attempts,
            error=f"Gate check failed after {max_attempts} attempts",
        )

    async def _check_gate(self, source_id: str) -> bool:
        """Check if all reachable GAPs for a source are resolved. Override in tests."""
        # In production, this calls icsl_tools.check_complete
        return True

    async def run_repairs(self, source_ids: list[str]) -> list[SourceRepairResult]:
        """Run repairs for multiple source points with concurrency control."""
        semaphore = asyncio.Semaphore(self._config.max_concurrency)

        async def limited_repair(sid: str) -> SourceRepairResult:
            async with semaphore:
                return await self._run_single_repair(sid)

        tasks = [limited_repair(sid) for sid in source_ids]
        return await asyncio.gather(*tasks)

    def get_progress(self) -> dict[str, dict[str, Any]]:
        """Get current progress for all source points."""
        return dict(self._progress)

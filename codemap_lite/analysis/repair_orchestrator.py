"""Repair Orchestrator — manages CLI subprocess agents for indirect call repair."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codemap_lite.analysis.feedback_store import FeedbackStore
from codemap_lite.graph.neo4j_store import GraphStore


# architecture.md §3 Retry 审计字段: last_attempt_reason ≤ 200 chars.
_MAX_REASON_LEN = 200


def _truncate_reason(reason: str) -> str:
    """Clip reason strings to the architecture-mandated 200-char cap."""
    if len(reason) <= _MAX_REASON_LEN:
        return reason
    return reason[: _MAX_REASON_LEN - 1] + "…"


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
    # Hard wall-clock timeout for a single agent subprocess. None = no timeout
    # (architecture.md §3 超时护栏: default opt-in preserves the "Agent 自然完成"
    # contract). When set, proc.communicate() is wrapped in asyncio.wait_for;
    # on TimeoutError the orchestrator kills the subprocess and stamps
    # `subprocess_timeout: <N>s` per architecture.md §3 Retry 审计字段.
    subprocess_timeout_seconds: float | None = None
    # Counter-example source for agent feedback loop (architecture.md §3
    # 反馈机制 step 4). When set, the latest rendered markdown is written
    # to ``.icslpreprocess/counter_examples.md`` before each agent launch.
    feedback_store: FeedbackStore | None = None
    # GraphStore used to stamp retry audit fields onto failing UnresolvedCalls
    # (architecture.md §3 Retry 审计字段). When set, each gate failure writes
    # last_attempt_timestamp + last_attempt_reason so the frontend GapDetail
    # can surface "last attempt failure reason + time" without reading JSONL.
    graph_store: GraphStore | None = None


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
        # Per-source BFS cache for retry audit write-back. Populated on
        # first gate failure per source; kept for the lifetime of the run
        # since the reachable set does not shrink across retries.
        self._reachable_cache: dict[str, set[str]] = {}

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

        # Copy icsl_tools.py so the agent CLI invocation (see architecture §3
        # Repair Agent tool protocol) is runnable from the target directory.
        from codemap_lite.agent import icsl_tools as _icsl_tools_module

        tools_src = Path(_icsl_tools_module.__file__)
        shutil.copy2(tools_src, icsl_dir / "icsl_tools.py")

        # Copy hook scripts so .claude/settings.json commands resolve.
        # architecture.md §3 进度通信机制: hooks write progress.json +
        # tool-use JSONL from within the agent subprocess.
        from codemap_lite.agent import hooks as _hooks_pkg

        hooks_src_dir = Path(_hooks_pkg.__file__).parent
        hooks_dst_dir = icsl_dir / "hooks"
        hooks_dst_dir.mkdir(parents=True, exist_ok=True)
        for hook_file in ("log_notification.py", "log_tool_use.py"):
            src = hooks_src_dir / hook_file
            if src.exists():
                shutil.copy2(src, hooks_dst_dir / hook_file)

        # Write source_id so hooks can identify which source they serve.
        (icsl_dir / "source_id.txt").write_text(source_id, encoding="utf-8")

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

    def _write_progress(self, source_id: str, **fields: Any) -> None:
        """Write/merge progress fields to ``logs/repair/<source_id>/progress.json``.

        architecture.md §3 进度通信机制 + ADR #52: Orchestrator writes
        progress at key lifecycle events so ``/api/v1/analyze/status``
        (polled by the frontend every 2s) can surface per-source state,
        attempt count, gate result, and edges written.

        Merges with existing content so Hook-written fields
        (gaps_fixed/gaps_total/current_gap) are preserved.
        """
        target_dir = self._config.target_dir
        progress_dir = target_dir / "logs" / "repair" / source_id
        progress_dir.mkdir(parents=True, exist_ok=True)
        path = progress_dir / "progress.json"
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        existing.update(fields)
        path.write_text(
            json.dumps(existing, ensure_ascii=False), encoding="utf-8"
        )

    def _count_edges_written(self, source_id: str) -> int:
        """Count LLM-resolved edges reachable from source_id."""
        store = self._config.graph_store
        if store is None:
            return 0
        try:
            subgraph = store.get_reachable_subgraph(source_id, max_depth=50)
            node_ids = {fn.id for fn in subgraph["nodes"]}
            return sum(
                1
                for e in subgraph["edges"]
                if e.props.resolved_by == "llm" and e.caller_id in node_ids
            )
        except Exception:
            return 0

    async def _run_single_repair(self, source_id: str) -> SourceRepairResult:
        """Run repair for a single source point with retry logic."""
        target_dir = self._config.target_dir
        attempts = 0
        max_attempts = self.MAX_RETRIES_PER_GAP

        while attempts < max_attempts:
            attempts += 1

            # Write progress: attempt starting
            self._write_progress(
                source_id,
                state="running",
                attempt=attempts,
                max_attempts=max_attempts,
                gate_result="pending",
            )

            # Inject files — re-render counter examples each attempt so
            # newly added feedback lands in the next agent launch
            # (architecture.md §3 反馈机制 step 4).
            counter_examples = (
                self._config.feedback_store.render_markdown()
                if self._config.feedback_store is not None
                else ""
            )
            self._inject_files(
                target_dir=target_dir,
                source_id=source_id,
                counter_examples=counter_examples,
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
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            cwd=str(target_dir),
                            stdout=stdout_target,
                            stderr=stderr_target,
                            env=env,
                        )
                        timeout = self._config.subprocess_timeout_seconds
                        if timeout is not None:
                            try:
                                await asyncio.wait_for(
                                    proc.communicate(), timeout=timeout
                                )
                            except asyncio.TimeoutError:
                                # architecture.md §3 超时护栏: kill the hung
                                # agent, stamp subprocess_timeout, continue
                                # the retry loop so the source's budget is
                                # not silently burned by a wedged process.
                                proc.kill()
                                try:
                                    await proc.wait()
                                except Exception:
                                    pass
                                reason = _truncate_reason(
                                    f"subprocess_timeout: {timeout}s"
                                )
                                self._record_retry_attempt(
                                    source_id=source_id,
                                    reason=reason,
                                )
                                self._write_progress(
                                    source_id,
                                    last_error=reason,
                                )
                                continue
                        else:
                            await proc.communicate()
                    except (OSError, FileNotFoundError) as exc:
                        # Subprocess failed to spawn or crashed mid-flight
                        # (e.g. missing CLI binary). Per architecture.md §3
                        # Retry 审计字段, stamp subprocess_crash and keep the
                        # retry loop alive — never let the exception bubble
                        # out and silently kill the source's retry budget.
                        reason = _truncate_reason(
                            f"subprocess_crash: {type(exc).__name__}: {exc}"
                        )
                        self._record_retry_attempt(
                            source_id=source_id,
                            reason=reason,
                        )
                        self._write_progress(source_id, last_error=reason)
                        continue
                finally:
                    if log_fh is not None:
                        log_fh.close()

                # Non-zero exit → Agent ran but failed. Per architecture.md §3
                # Retry 审计字段 ("非门禁失败同样记账"), stamp agent_error
                # instead of letting it silently fall through to the gate
                # check (which would mis-attribute the failure as
                # gate_failed and hide the real signal from reviewers).
                if proc.returncode is not None and proc.returncode != 0:
                    reason = _truncate_reason(
                        f"agent_error: exit {proc.returncode}"
                    )
                    self._record_retry_attempt(
                        source_id=source_id,
                        reason=reason,
                    )
                    self._write_progress(source_id, last_error=reason)
                    continue

                # Gate check
                self._write_progress(source_id, state="gate_checking")
                gate_passed = await self._check_gate(source_id)
                edges = self._count_edges_written(source_id)
                if gate_passed:
                    self._write_progress(
                        source_id,
                        state="succeeded",
                        gate_result="passed",
                        edges_written=edges,
                    )
                    return SourceRepairResult(
                        source_id=source_id, success=True, attempts=attempts
                    )
                # Gate failed — stamp retry audit fields onto every pending
                # UnresolvedCall for this source so ReviewQueue can surface
                # "last attempt failed at <ts> because <reason>"
                # (architecture.md §3 Retry 审计字段).
                self._record_retry_attempt(
                    source_id=source_id,
                    reason="gate_failed: remaining pending GAPs",
                )
                self._write_progress(
                    source_id,
                    gate_result="failed",
                    edges_written=edges,
                    last_error="gate_failed: remaining pending GAPs",
                )
            finally:
                self._cleanup_injection(target_dir)

        self._write_progress(source_id, state="failed")
        return SourceRepairResult(
            source_id=source_id,
            success=False,
            attempts=attempts,
            error=f"Gate check failed after {max_attempts} attempts",
        )

    async def _check_gate(self, source_id: str) -> bool:
        """Check if all reachable GAPs for a source are resolved.

        architecture.md §3 门禁机制: orchestrator invokes the agent-side
        tool CLI ``python .icslpreprocess/icsl_tools.py check-complete
        --source <id>`` in the target directory and parses its JSON
        response ``{"complete": bool, "remaining_gaps": int,
        "pending_gap_ids": [...]}``. Returns True only on
        ``complete: True``; any spawn error, non-zero exit, malformed
        JSON, or missing ``complete`` field falls through to False so
        the retry loop gets another attempt and the gate-failed audit
        stamp lands.

        Kept async + instance-method so tests can still override via
        ``orchestrator._check_gate = AsyncMock(return_value=True)``.
        """
        target_dir = self._config.target_dir
        tool_path = target_dir / ".icslpreprocess" / "icsl_tools.py"
        cmd = [
            sys.executable,
            str(tool_path),
            "check-complete",
            "--source",
            source_id,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(target_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **(self._config.env or {})},
            )
            stdout_bytes, _stderr_bytes = await proc.communicate()
        except (OSError, FileNotFoundError):
            return False
        if proc.returncode != 0:
            return False
        try:
            payload = json.loads(stdout_bytes.decode("utf-8", errors="replace"))
        except (ValueError, json.JSONDecodeError):
            return False
        return bool(payload.get("complete", False))

    def _record_retry_attempt(self, source_id: str, reason: str) -> None:
        """Stamp last_attempt_{timestamp,reason} on every pending GAP for source.

        architecture.md §3 Retry 审计字段: after each failed gate check,
        the orchestrator is responsible for writing the audit fields to
        Neo4j. Silently noop when no graph_store is configured so existing
        tests and stub deployments stay green.
        """
        store = self._config.graph_store
        if store is None:
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        for gap in store.get_unresolved_calls(status="pending"):
            # Limit audit stamp to GAPs actually owned by this source's
            # caller chain — but we don't have cheap reverse lookup here,
            # so stamp all pending GAPs for this source's caller set via
            # the source_id's reachable subgraph.
            if self._is_gap_in_source(store, source_id, gap.caller_id):
                store.update_unresolved_call_retry_state(
                    call_id=gap.id, timestamp=timestamp, reason=reason
                )

    def _is_gap_in_source(
        self, store: GraphStore, source_id: str, caller_id: str
    ) -> bool:
        """Return True if caller_id is reachable from source_id.

        Cached per run to avoid re-BFS for every GAP in the same source.
        """
        reachable = self._reachable_cache.get(source_id)
        if reachable is None:
            subgraph = store.get_reachable_subgraph(source_id)
            reachable = {fn.id for fn in subgraph["nodes"]}
            self._reachable_cache[source_id] = reachable
        return caller_id in reachable

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

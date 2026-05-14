"""FeedbackStore — manages counter examples for repair agent feedback loop."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(frozen=True)
class CounterExample:
    """A generalized counter example from a failed repair."""

    call_context: str
    wrong_target: str
    correct_target: str
    pattern: str
    source_id: str = ""


class FeedbackStore:
    """Stores and manages counter examples, writes them to markdown for agent injection."""

    def __init__(self, storage_dir: Path) -> None:
        self._storage_dir = storage_dir
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._examples: list[CounterExample] = []
        self._load_existing()

    def _json_path(self) -> Path:
        return self._storage_dir / "counter_examples.json"

    def _md_path(self) -> Path:
        return self._storage_dir / "counter_examples.md"

    def _load_existing(self) -> None:
        json_path = self._json_path()
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                self._examples = [CounterExample(**item) for item in data]
            except (json.JSONDecodeError, OSError, TypeError, KeyError):
                # Corrupted store — start fresh rather than crash
                self._examples = []

    def add(self, example: CounterExample) -> bool:
        """Add a counter example. Merges if same pattern already exists.

        Returns ``True`` when the example was appended as a new entry and
        ``False`` when it was deduplicated against an existing pattern
        (architecture.md §3 反馈机制 steps 3-5 "相似 → 总结合并").
        The return value lets the HTTP layer tell the reviewer whether
        their submission landed as a fresh pattern or merged into an
        existing one, closing the observability loop (北极星指标 #5).
        """
        for existing in self._examples:
            if existing.pattern == example.pattern:
                # Same pattern — merge by keeping the existing one
                # (in production, LLM would summarize; here we deduplicate)
                return False

        self._examples.append(example)
        self._save()
        return True

    def list_all(self) -> list[CounterExample]:
        return list(self._examples)

    def get_for_source(self, source_id: str) -> list[CounterExample]:
        """Return counter-examples scoped to a specific source point.

        architecture.md §3 反馈机制: each repair agent should only see
        counter-examples relevant to its own source point's repair history.
        """
        return [e for e in self._examples if e.source_id == source_id]

    def render_markdown_for_source(self, source_id: str) -> str:
        """Render counter-examples filtered by source_id as agent-readable markdown.

        Used by RepairOrchestrator to inject only relevant counter-examples
        into each source's .icslpreprocess directory (architecture.md §3).
        Falls back to render_markdown() (all examples) when source_id is empty.
        """
        if not source_id:
            return self.render_markdown()
        examples = self.get_for_source(source_id)
        if not examples:
            return ""
        lines = ["# Counter Examples (反例库)\n"]
        lines.append("以下是之前修复中出现的错误模式，请避免重复：\n")
        for i, ex in enumerate(examples, 1):
            lines.append(f"## 反例 {i}: {ex.pattern}\n")
            lines.append(f"- **调用上下文**: `{ex.call_context}`")
            lines.append(f"- **错误目标**: `{ex.wrong_target}`")
            lines.append(f"- **正确目标**: `{ex.correct_target}`")
            lines.append("")
        return "\n".join(lines)

    def _save(self) -> None:
        # Save JSON (machine-readable)
        json_path = self._json_path()
        json_path.write_text(
            json.dumps([asdict(e) for e in self._examples], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Generate markdown (agent-readable)
        self._write_md()

    def render_markdown(self) -> str:
        """Render the current counter examples as agent-readable markdown.

        Used both by :meth:`_write_md` (persistent copy next to the JSON
        store) and by :class:`RepairOrchestrator` which injects the latest
        snapshot into ``<target>/.icslpreprocess/counter_examples.md``
        before each agent launch (architecture.md §3 反馈机制 step 4).
        Returns an empty string when no examples exist so callers can
        fall back to the "no counter examples yet" stub.
        """
        if not self._examples:
            return ""

        lines = ["# Counter Examples (反例库)\n"]
        lines.append("以下是之前修复中出现的错误模式，请避免重复：\n")

        for i, ex in enumerate(self._examples, 1):
            lines.append(f"## 反例 {i}: {ex.pattern}\n")
            lines.append(f"- **调用上下文**: `{ex.call_context}`")
            lines.append(f"- **错误目标**: `{ex.wrong_target}`")
            lines.append(f"- **正确目标**: `{ex.correct_target}`")
            lines.append("")

        return "\n".join(lines)

    def _write_md(self) -> None:
        md_path = self._md_path()
        md_path.write_text(self.render_markdown(), encoding="utf-8")

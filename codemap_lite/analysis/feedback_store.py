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
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self._examples = [CounterExample(**item) for item in data]

    def add(self, example: CounterExample) -> None:
        """Add a counter example. Merges if same pattern already exists."""
        for existing in self._examples:
            if existing.pattern == example.pattern:
                # Same pattern — merge by keeping the existing one
                # (in production, LLM would summarize; here we deduplicate)
                return

        self._examples.append(example)
        self._save()

    def list_all(self) -> list[CounterExample]:
        return list(self._examples)

    def _save(self) -> None:
        # Save JSON (machine-readable)
        json_path = self._json_path()
        json_path.write_text(
            json.dumps([asdict(e) for e in self._examples], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Generate markdown (agent-readable)
        self._write_md()

    def _write_md(self) -> None:
        md_path = self._md_path()
        lines = ["# Counter Examples (反例库)\n"]
        lines.append("以下是之前修复中出现的错误模式，请避免重复：\n")

        for i, ex in enumerate(self._examples, 1):
            lines.append(f"## 反例 {i}: {ex.pattern}\n")
            lines.append(f"- **调用上下文**: `{ex.call_context}`")
            lines.append(f"- **错误目标**: `{ex.wrong_target}`")
            lines.append(f"- **正确目标**: `{ex.correct_target}`")
            lines.append("")

        md_path.write_text("\n".join(lines), encoding="utf-8")

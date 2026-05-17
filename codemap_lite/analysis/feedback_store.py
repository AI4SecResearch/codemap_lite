"""FeedbackStore — manages counter examples for repair agent feedback loop."""
from __future__ import annotations

import json
import re
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


def _normalize_pattern(pattern: str) -> str:
    """Normalize a pattern for fuzzy dedup comparison.

    architecture.md §3 反馈机制 step 4: "pattern 中包含具体行号的反例应
    自动去掉行号泛化为模式级规则". This function:
    1. Strips line number references (e.g., ":123", "line 45", "at L42")
    2. Strips file path references (path/to/file.cpp)
    3. Normalizes whitespace
    4. Lowercases for case-insensitive comparison

    Does NOT strip standalone numbers that might be meaningful identifiers.
    """
    s = pattern
    # Remove "line N" / "Line N" / "@lineN" / "at line N" references
    s = re.sub(r"(?:at\s+)?(?:line\s*|L)\d+", "", s, flags=re.IGNORECASE)
    # Remove ":N" line number suffixes (e.g., "foo.cpp:42")
    s = re.sub(r":\d+", "", s)
    # Remove file paths (e.g., "src/module/foo.cpp", "path\to\bar.h")
    s = re.sub(r"[a-zA-Z0-9_/\\.-]+\.[ch]pp\b", "", s)
    s = re.sub(r"[a-zA-Z0-9_/\\.-]+\.[ch]\b", "", s)
    # Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()


def _pattern_similarity(a: str, b: str) -> float:
    """Compute similarity between two normalized patterns.

    Uses token overlap (Jaccard similarity) as a lightweight proxy for
    semantic similarity. Returns 0.0-1.0.
    """
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


# Threshold for fuzzy dedup: patterns with similarity >= this are merged
_SIMILARITY_THRESHOLD = 0.7


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
        """Add a counter example. Merges if same or similar pattern exists.

        Returns ``True`` when the example was appended as a new entry and
        ``False`` when it was deduplicated against an existing pattern
        (architecture.md §3 反馈机制 steps 3-5 "相似 → 总结合并").

        Dedup strategy (two-tier):
        1. Exact match: same pattern string → always merge
        2. Fuzzy match: normalized patterns with Jaccard similarity >= 0.7
           → merge (catches "same bug at different line numbers")

        Full LLM-based semantic similarity is a future enhancement; this
        provides the 80% case (line-number-stripped token overlap).
        """
        norm_new = _normalize_pattern(example.pattern)

        for existing in self._examples:
            # Tier 1: exact match
            if existing.pattern == example.pattern:
                return False
            # Tier 2: fuzzy match on normalized patterns
            norm_existing = _normalize_pattern(existing.pattern)
            if _pattern_similarity(norm_new, norm_existing) >= _SIMILARITY_THRESHOLD:
                return False

        self._examples.append(example)
        self._save()
        return True

    def list_all(self) -> list[CounterExample]:
        return list(self._examples)

    def get_by_index(self, index: int) -> CounterExample | None:
        """Get a counter-example by its 0-based index (used as ID)."""
        if 0 <= index < len(self._examples):
            return self._examples[index]
        return None

    def delete(self, index: int) -> bool:
        """Delete a counter-example by its 0-based index.

        Returns True if deleted, False if index out of range.
        """
        if 0 <= index < len(self._examples):
            self._examples.pop(index)
            self._save()
            return True
        return False

    def update(self, index: int, fields: dict[str, str]) -> bool:
        """Update fields of a counter-example by its 0-based index.

        Supported fields: call_context, wrong_target, correct_target, pattern.
        Returns True if updated, False if index out of range.
        """
        if not (0 <= index < len(self._examples)):
            return False
        existing = self._examples[index]
        updated = CounterExample(
            call_context=fields.get("call_context", existing.call_context),
            wrong_target=fields.get("wrong_target", existing.wrong_target),
            correct_target=fields.get("correct_target", existing.correct_target),
            pattern=fields.get("pattern", existing.pattern),
            source_id=fields.get("source_id", existing.source_id),
        )
        self._examples[index] = updated
        self._save()
        return True

    def get_for_source(self, source_id: str) -> list[CounterExample]:
        """Return counter-examples relevant to a specific source point.

        architecture.md §3 反馈机制: "泛化去重后，全量注入 prompt" — all
        counter-examples are relevant to every source because the same error
        pattern can occur across different source points. The source_id field
        tracks provenance (who reported it) but does NOT restrict visibility.

        Returns all examples that either:
        - Were reported by this source (source_id match), OR
        - Share a pattern that this source might encounter (all of them,
          since patterns are generalized and source-agnostic after dedup).

        In practice this returns all examples — the architecture mandates
        全量注入 (full injection) into every agent's counter_examples.md.
        """
        return list(self._examples)

    def render_markdown_for_source(self, source_id: str) -> str:
        """Render counter-examples as agent-readable markdown.

        architecture.md §3 反馈机制: "泛化去重后，全量注入 prompt" — all
        counter-examples are injected into every agent's prompt regardless
        of which source originally reported them. The source_id parameter
        is kept for API compatibility but does not filter the output.
        """
        return self.render_markdown()

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

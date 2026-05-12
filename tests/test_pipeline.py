"""Tests for Pipeline Orchestrator — full analysis workflow."""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codemap_lite.pipeline.orchestrator import PipelineOrchestrator, PipelineResult
from codemap_lite.parsing.plugin_registry import PluginRegistry
from codemap_lite.parsing.types import FunctionDef, CallEdge, UnresolvedCall, CallType, Symbol, SymbolKind


class FakePlugin:
    """Fake plugin for testing pipeline without tree-sitter."""

    def supported_extensions(self) -> list[str]:
        return [".cpp", ".h"]

    def parse_file(self, file_path: Path) -> list[FunctionDef]:
        import hashlib
        content = file_path.read_text()
        # Simple regex-free extraction: look for "void funcname()" pattern
        import re
        funcs = []
        for i, line in enumerate(content.split("\n"), 1):
            m = re.match(r'\s*(?:void|int|bool)\s+(\w+)\s*\(', line)
            if m and '{' in content[content.find(line):content.find(line)+200]:
                funcs.append(FunctionDef(
                    name=m.group(1),
                    signature=line.strip().rstrip('{').strip(),
                    file_path=file_path,
                    start_line=i,
                    end_line=i + 2,
                    body_hash=hashlib.sha256(line.encode()).hexdigest()[:16],
                ))
        return funcs

    def extract_symbols(self, file_path: Path) -> list[Symbol]:
        return []

    def build_calls(self, file_path: Path, symbols: dict) -> tuple[list[CallEdge], list[UnresolvedCall]]:
        return [], []


@pytest.fixture
def sample_cpp_dir(tmp_path):
    """Create a temp directory with sample C++ files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.cpp").write_text("""void helper() {
}
void main_func() {
    helper();
}
""")
    (src / "utils.cpp").write_text("""void util_func() {
    // implementation
}
""")
    return tmp_path


@pytest.fixture
def fake_registry():
    reg = PluginRegistry()
    reg.register("cpp", FakePlugin())
    return reg


def test_pipeline_orchestrator_full_analysis(sample_cpp_dir, fake_registry):
    """Full analysis: scan → parse → store."""
    orch = PipelineOrchestrator(target_dir=sample_cpp_dir, registry=fake_registry)
    result = orch.run_full_analysis()

    assert result.files_scanned > 0
    assert result.functions_found > 0
    assert result.success is True


def test_pipeline_orchestrator_incremental_no_changes(sample_cpp_dir, fake_registry):
    """Incremental: first run is full, second detects no changes."""
    orch = PipelineOrchestrator(target_dir=sample_cpp_dir, registry=fake_registry)

    # First run
    result1 = orch.run_full_analysis()
    assert result1.files_scanned > 0

    # Second run (incremental, no changes)
    result2 = orch.run_incremental_analysis()
    assert result2.files_changed == 0


def test_pipeline_orchestrator_detects_changes(sample_cpp_dir, fake_registry):
    """After modifying a file, incremental detects the change."""
    orch = PipelineOrchestrator(target_dir=sample_cpp_dir, registry=fake_registry)

    # First run
    orch.run_full_analysis()

    # Modify a file
    (sample_cpp_dir / "src" / "main.cpp").write_text("""void helper() {
}
void new_func() {
}
void main_func() {
    helper();
}
""")

    # Incremental run
    result = orch.run_incremental_analysis()
    assert result.files_changed > 0


def test_pipeline_result_dataclass():
    result = PipelineResult(
        success=True,
        files_scanned=10,
        functions_found=25,
        direct_calls=30,
        unresolved_calls=5,
        files_changed=0,
    )
    assert result.success is True
    assert result.files_scanned == 10

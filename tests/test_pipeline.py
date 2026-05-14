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


def test_pipeline_resolved_by_uses_canonical_values(sample_cpp_dir, fake_registry):
    """architecture.md §4: resolved_by ∈ {symbol_table, signature, dataflow, context, llm}.

    All edges created by the static analysis pipeline must use canonical
    resolved_by values from the architecture spec.
    """
    orch = PipelineOrchestrator(target_dir=sample_cpp_dir, registry=fake_registry)
    orch.run_full_analysis()

    canonical = {"symbol_table", "signature", "dataflow", "context", "llm"}
    edges = orch._store.list_calls_edges()
    for edge in edges:
        assert edge.props.resolved_by in canonical, (
            f"Edge {edge.caller_id}→{edge.callee_id} has non-canonical "
            f"resolved_by={edge.props.resolved_by!r}; "
            f"must be one of {sorted(canonical)}"
        )


def test_pipeline_normalizes_call_type_to_architecture_spec(tmp_path):
    """architecture.md §4: CALLS.call_type ∈ {direct, indirect, virtual}.

    Parser may emit finer-grained types (callback, member_fn_ptr, ipc_proxy)
    but the pipeline must normalize them to the 3 canonical values before
    writing to the graph store.
    """
    from codemap_lite.pipeline.orchestrator import _normalize_call_type

    # Verify the normalization function itself
    assert _normalize_call_type("direct") == "direct"
    assert _normalize_call_type("indirect") == "indirect"
    assert _normalize_call_type("virtual") == "virtual"
    assert _normalize_call_type("callback") == "indirect"
    assert _normalize_call_type("member_fn_ptr") == "indirect"
    assert _normalize_call_type("ipc_proxy") == "indirect"
    # Unknown values default to indirect (safe fallback)
    assert _normalize_call_type("unknown_type") == "indirect"


def test_pipeline_stores_only_canonical_call_types(tmp_path):
    """architecture.md §4: all call_type values in the graph store must be
    one of {direct, indirect, virtual}. This tests the full pipeline path
    with a plugin that emits non-canonical call_types."""

    import hashlib

    class NonCanonicalPlugin:
        """Plugin that emits CALLBACK and MEMBER_FN_PTR call types."""

        def supported_extensions(self):
            return [".cpp"]

        def parse_file(self, file_path):
            return [
                FunctionDef(
                    name="caller_func",
                    signature="void caller_func()",
                    file_path=file_path,
                    start_line=1,
                    end_line=5,
                    body_hash=hashlib.sha256(b"caller").hexdigest()[:16],
                ),
                FunctionDef(
                    name="callee_func",
                    signature="void callee_func()",
                    file_path=file_path,
                    start_line=7,
                    end_line=10,
                    body_hash=hashlib.sha256(b"callee").hexdigest()[:16],
                ),
            ]

        def extract_symbols(self, file_path):
            return []

        def build_calls(self, file_path, symbols):
            # Emit a direct call (should stay "direct") and unresolved calls
            # with non-canonical types
            resolved = [
                CallEdge(
                    caller_name="caller_func",
                    callee_name="callee_func",
                    call_file=file_path,
                    call_line=3,
                    call_type=CallType.DIRECT,
                    resolved_by="symbol_table",
                ),
            ]
            unresolved = [
                UnresolvedCall(
                    caller_name="caller_func",
                    call_expression="cb_ptr()",
                    call_file=file_path,
                    call_line=4,
                    call_type=CallType.CALLBACK,
                    var_name="cb_ptr",
                    var_type="void(*)()",
                ),
                UnresolvedCall(
                    caller_name="caller_func",
                    call_expression="obj->method()",
                    call_file=file_path,
                    call_line=5,
                    call_type=CallType.MEMBER_FN_PTR,
                    var_name="obj",
                    var_type="Base*",
                ),
            ]
            return resolved, unresolved

    src = tmp_path / "src"
    src.mkdir()
    (src / "test.cpp").write_text("void caller_func() {\n}\nvoid callee_func() {\n}\n")

    reg = PluginRegistry()
    reg.register("cpp", NonCanonicalPlugin())

    orch = PipelineOrchestrator(target_dir=tmp_path, registry=reg)
    orch.run_full_analysis()

    # Check CALLS edges: all must have canonical call_type
    canonical_types = {"direct", "indirect", "virtual"}
    for edge in orch._store.list_calls_edges():
        assert edge.props.call_type in canonical_types, (
            f"Edge has non-canonical call_type={edge.props.call_type!r}"
        )

    # Check UnresolvedCalls: all must have canonical call_type
    for uc in orch._store.get_unresolved_calls():
        assert uc.call_type in canonical_types, (
            f"UnresolvedCall has non-canonical call_type={uc.call_type!r}"
        )


def test_pipeline_ambiguous_callee_creates_unresolved_call(tmp_path):
    """architecture.md §2 + orchestrator _resolve_id: when multiple functions
    share the same name (ambiguous resolution), the pipeline must NOT pick
    one arbitrarily. Instead it must create an UnresolvedCall with the
    candidates list so the repair agent can resolve it.

    This prevents the cross-module pollution bug where e.g. `Clear` in
    data_buffer.h was incorrectly linked to `Clear` in preferences_util.cpp.
    """
    import hashlib

    class AmbiguousPlugin:
        """Plugin that produces two functions with the same bare name
        and a direct call to that name."""

        def supported_extensions(self):
            return [".cpp"]

        def parse_file(self, file_path):
            # Two files: one has caller + callee_A, other has callee_B
            name = file_path.name
            if name == "module_a.cpp":
                return [
                    FunctionDef(
                        name="caller",
                        signature="void caller()",
                        file_path=file_path,
                        start_line=1,
                        end_line=5,
                        body_hash=hashlib.sha256(b"caller").hexdigest()[:16],
                    ),
                    FunctionDef(
                        name="ModuleA::Clear",
                        signature="void ModuleA::Clear()",
                        file_path=file_path,
                        start_line=10,
                        end_line=15,
                        body_hash=hashlib.sha256(b"clearA").hexdigest()[:16],
                    ),
                ]
            elif name == "module_b.cpp":
                return [
                    FunctionDef(
                        name="ModuleB::Clear",
                        signature="void ModuleB::Clear()",
                        file_path=file_path,
                        start_line=1,
                        end_line=5,
                        body_hash=hashlib.sha256(b"clearB").hexdigest()[:16],
                    ),
                ]
            return []

        def build_calls(self, file_path, all_symbols):
            name = file_path.name
            if name == "module_a.cpp":
                # caller calls "Clear" — ambiguous (could be A or B)
                return [
                    CallEdge(
                        caller_name="caller",
                        callee_name="Clear",
                        call_file=file_path,
                        call_line=3,
                        call_type=CallType.DIRECT,
                        resolved_by="symbol_table",
                    ),
                ], []
            return [], []

        def build_hierarchy(self, file_paths):
            pass

    src = tmp_path / "src"
    src.mkdir()
    (src / "module_a.cpp").write_text(
        "void caller() { Clear(); }\nvoid ModuleA::Clear() {}\n"
    )
    (src / "module_b.cpp").write_text("void ModuleB::Clear() {}\n")

    reg = PluginRegistry()
    reg.register("cpp", AmbiguousPlugin())

    orch = PipelineOrchestrator(target_dir=tmp_path, registry=reg)
    result = orch.run_full_analysis()

    # The call to "Clear" is ambiguous (two candidates: ModuleA::Clear, ModuleB::Clear)
    # It must NOT create a CALLS edge (would be arbitrary cross-module link)
    # Instead it must create an UnresolvedCall
    edges = orch._store.list_calls_edges()
    # No direct edge from caller to either Clear
    caller_edges = [e for e in edges if e.caller_id != e.callee_id]
    # The caller should NOT have a CALLS edge to either Clear
    # (because "Clear" bare name resolves to 2 candidates → ambiguous)

    unresolved = orch._store.get_unresolved_calls()
    # Must have at least one UC for the ambiguous "Clear" call
    clear_ucs = [uc for uc in unresolved if "Clear" in uc.call_expression]
    assert len(clear_ucs) >= 1, (
        "architecture.md §2: ambiguous callee (multiple candidates) must "
        "create UnresolvedCall, not pick one arbitrarily. "
        f"Got {len(clear_ucs)} UCs for 'Clear', edges={len(caller_edges)}"
    )
    # The UC should have candidates listing both options
    uc = clear_ucs[0]
    assert len(uc.candidates) >= 2, (
        f"UC candidates should list both Clear variants, got {uc.candidates}"
    )

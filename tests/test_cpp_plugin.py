"""Tests for the C++ language plugin."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from codemap_lite.parsing.cpp.plugin import CppPlugin
from codemap_lite.parsing.types import (
    CallType,
    FunctionDef,
    SymbolKind,
    UnresolvedCall,
)

SAMPLE_CPP = '''
#include <functional>

class Base {
public:
    virtual void process(int x) = 0;
};

class Derived : public Base {
public:
    void process(int x) override {
        helper(x);
    }
    void helper(int val) {}
};

void directCall() {
    Derived d;
    d.helper(42);
}

typedef void (*FuncPtr)(int);

void indirectCall(FuncPtr ptr) {
    ptr(10);
}

void callbackUser(std::function<void(int)> cb) {
    cb(5);
}
'''


@pytest.fixture
def plugin() -> CppPlugin:
    """Create a CppPlugin instance."""
    return CppPlugin()


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    """Write sample C++ code to a temporary file."""
    file_path = tmp_path / "sample.cpp"
    file_path.write_text(SAMPLE_CPP)
    return file_path


class TestSupportedExtensions:
    """Tests for CppPlugin.supported_extensions."""

    def test_supported_extensions(self, plugin: CppPlugin) -> None:
        exts = plugin.supported_extensions()
        assert ".cpp" in exts
        assert ".cc" in exts
        assert ".cxx" in exts
        assert ".h" in exts
        assert ".hpp" in exts


class TestParseFile:
    """Tests for CppPlugin.parse_file."""

    def test_parse_file_extracts_functions(
        self, plugin: CppPlugin, sample_file: Path
    ) -> None:
        functions = plugin.parse_file(sample_file)
        names = [f.name for f in functions]
        assert "process" in names
        assert "helper" in names
        assert "directCall" in names
        assert "indirectCall" in names
        assert "callbackUser" in names

    def test_parse_file_function_has_correct_fields(
        self, plugin: CppPlugin, sample_file: Path
    ) -> None:
        functions = plugin.parse_file(sample_file)
        direct_call = next(f for f in functions if f.name == "directCall")
        assert direct_call.file_path == sample_file
        assert direct_call.start_line > 0
        assert direct_call.end_line >= direct_call.start_line
        assert direct_call.signature != ""
        assert direct_call.body_hash != ""

    def test_parse_file_returns_function_def_instances(
        self, plugin: CppPlugin, sample_file: Path
    ) -> None:
        functions = plugin.parse_file(sample_file)
        for f in functions:
            assert isinstance(f, FunctionDef)


class TestExtractSymbols:
    """Tests for CppPlugin.extract_symbols."""

    def test_extract_symbols_finds_classes(
        self, plugin: CppPlugin, sample_file: Path
    ) -> None:
        symbols = plugin.extract_symbols(sample_file)
        class_symbols = [s for s in symbols if s.kind == SymbolKind.CLASS]
        class_names = [s.name for s in class_symbols]
        assert "Base" in class_names
        assert "Derived" in class_names

    def test_extract_symbols_finds_functions(
        self, plugin: CppPlugin, sample_file: Path
    ) -> None:
        symbols = plugin.extract_symbols(sample_file)
        func_symbols = [s for s in symbols if s.kind == SymbolKind.FUNCTION]
        func_names = [s.name for s in func_symbols]
        assert "directCall" in func_names
        assert "indirectCall" in func_names
        assert "callbackUser" in func_names


class TestBuildCalls:
    """Tests for CppPlugin.build_calls."""

    def test_build_calls_finds_direct_calls(
        self, plugin: CppPlugin, sample_file: Path
    ) -> None:
        functions = plugin.parse_file(sample_file)
        symbols = {f.name: f for f in functions}
        direct_calls, _ = plugin.build_calls(sample_file, symbols)
        # d.helper(42) should be a direct call since 'helper' is in symbols
        helper_calls = [c for c in direct_calls if c.callee_name == "helper"]
        assert len(helper_calls) >= 1
        caller_names = [c.caller_name for c in helper_calls]
        assert "directCall" in caller_names or "process" in caller_names

    def test_build_calls_identifies_function_pointer_as_unresolved(
        self, plugin: CppPlugin, sample_file: Path
    ) -> None:
        functions = plugin.parse_file(sample_file)
        symbols = {f.name: f for f in functions}
        _, unresolved = plugin.build_calls(sample_file, symbols)
        # ptr(10) should be unresolved — ptr is a function pointer param
        ptr_calls = [
            u for u in unresolved if "ptr" in u.call_expression
        ]
        assert len(ptr_calls) >= 1
        assert ptr_calls[0].call_type in (CallType.INDIRECT, CallType.CALLBACK)

    def test_build_calls_identifies_std_function_as_unresolved(
        self, plugin: CppPlugin, sample_file: Path
    ) -> None:
        functions = plugin.parse_file(sample_file)
        symbols = {f.name: f for f in functions}
        _, unresolved = plugin.build_calls(sample_file, symbols)
        # cb(5) should be unresolved — cb is a std::function param
        cb_calls = [
            u for u in unresolved if "cb" in u.call_expression
        ]
        assert len(cb_calls) >= 1
        assert cb_calls[0].call_type in (CallType.INDIRECT, CallType.CALLBACK)

    def test_build_calls_direct_call_has_correct_type(
        self, plugin: CppPlugin, sample_file: Path
    ) -> None:
        functions = plugin.parse_file(sample_file)
        symbols = {f.name: f for f in functions}
        direct_calls, _ = plugin.build_calls(sample_file, symbols)
        for call in direct_calls:
            assert call.call_type in (CallType.DIRECT, CallType.VIRTUAL)

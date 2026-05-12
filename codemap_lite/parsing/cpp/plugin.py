"""CppPlugin — LanguagePlugin implementation for C/C++ source files."""
from __future__ import annotations

from pathlib import Path

from codemap_lite.parsing.cpp.call_graph import build_calls as _build_calls
from codemap_lite.parsing.cpp.class_hierarchy import (
    ClassHierarchyIndex,
    build_class_hierarchy,
)
from codemap_lite.parsing.cpp.symbol_extractor import (
    extract_functions,
    extract_symbols as _extract_symbols,
)
from codemap_lite.parsing.types import CallEdge, FunctionDef, Symbol, UnresolvedCall


class CppPlugin:
    """C/C++ language plugin using tree-sitter for parsing.

    Implements the LanguagePlugin protocol defined in base_plugin.py.
    """

    def __init__(self) -> None:
        self._hierarchy: ClassHierarchyIndex | None = None

    def supported_extensions(self) -> list[str]:
        """Return file extensions handled by this plugin."""
        return [".cpp", ".cc", ".cxx", ".h", ".hpp"]

    def parse_file(self, file_path: Path) -> list[FunctionDef]:
        """Parse a C/C++ source file and extract function definitions."""
        source = file_path.read_bytes()
        return extract_functions(source, file_path)

    def extract_symbols(self, file_path: Path) -> list[Symbol]:
        """Extract all symbols (functions, classes, variables) from a file."""
        source = file_path.read_bytes()
        return _extract_symbols(source, file_path)

    def build_hierarchy(self, file_paths: list[Path]) -> None:
        """Build class hierarchy index from all source files.

        Should be called after all files are parsed (first pass) and before
        build_calls (second pass).
        """
        self._hierarchy = ClassHierarchyIndex()
        for fp in file_paths:
            try:
                source = fp.read_bytes()
                classes, fn_ptr_arrays = build_class_hierarchy(source, str(fp))
                for cls in classes:
                    self._hierarchy.add_class(cls)
                for arr in fn_ptr_arrays:
                    self._hierarchy.add_fn_ptr_array(arr)
            except Exception:
                continue

    def build_calls(
        self, file_path: Path, symbols: dict[str, FunctionDef]
    ) -> tuple[list[CallEdge], list[UnresolvedCall]]:
        """Build call edges and identify unresolved calls."""
        source = file_path.read_bytes()
        return _build_calls(source, file_path, symbols, self._hierarchy)

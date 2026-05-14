"""LanguagePlugin protocol — defines the interface for language-specific parsers."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from codemap_lite.parsing.types import CallEdge, FunctionDef, Symbol, UnresolvedCall


@runtime_checkable
class LanguagePlugin(Protocol):
    """Protocol that all language plugins must satisfy.

    Implementations provide language-specific parsing, symbol extraction,
    and call graph construction.
    """

    def parse_file(self, file_path: Path) -> list[FunctionDef]:
        """Parse a source file and extract function definitions.

        Args:
            file_path: Path to the source file.

        Returns:
            List of FunctionDef instances found in the file.
        """
        ...

    def extract_symbols(self, file_path: Path) -> list[Symbol]:
        """Extract all symbols (functions, classes, variables) from a file.

        Args:
            file_path: Path to the source file.

        Returns:
            List of Symbol instances found in the file.
        """
        ...

    def build_calls(
        self, file_path: Path, symbols: dict[str, FunctionDef]
    ) -> tuple[list[CallEdge], list[UnresolvedCall]]:
        """Build call edges and identify unresolved calls.

        Args:
            file_path: Path to the source file.
            symbols: Dict mapping function names to their definitions.

        Returns:
            Tuple of (resolved call edges, unresolved calls).
        """
        ...

    def supported_extensions(self) -> list[str]:
        """Return file extensions this plugin handles.

        Returns:
            List of extensions including the dot (e.g. [".cpp", ".h"]).
        """
        ...

    # Optional extension point (not required for protocol conformance):
    # def build_hierarchy(self, file_paths: list[Path]) -> None:
    #     """Build class/type hierarchy between parse passes (C++ only)."""

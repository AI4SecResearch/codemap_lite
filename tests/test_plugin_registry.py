"""Tests for the plugin registry and LanguagePlugin protocol."""
from __future__ import annotations

from pathlib import Path

import pytest

from codemap_lite.parsing.base_plugin import LanguagePlugin
from codemap_lite.parsing.plugin_registry import PluginRegistry
from codemap_lite.parsing.types import (
    CallEdge,
    CallType,
    FunctionDef,
    Symbol,
    SymbolKind,
    UnresolvedCall,
)


class MockCppPlugin:
    """A mock C++ plugin that satisfies the LanguagePlugin protocol."""

    def parse_file(self, file_path: Path) -> list[FunctionDef]:
        return []

    def extract_symbols(self, file_path: Path) -> list[Symbol]:
        return []

    def build_calls(
        self, file_path: Path, symbols: dict[str, FunctionDef]
    ) -> tuple[list[CallEdge], list[UnresolvedCall]]:
        return ([], [])

    def supported_extensions(self) -> list[str]:
        return [".cpp", ".h", ".hpp"]


class MockJavaPlugin:
    """A mock Java plugin that satisfies the LanguagePlugin protocol."""

    def parse_file(self, file_path: Path) -> list[FunctionDef]:
        return []

    def extract_symbols(self, file_path: Path) -> list[Symbol]:
        return []

    def build_calls(
        self, file_path: Path, symbols: dict[str, FunctionDef]
    ) -> tuple[list[CallEdge], list[UnresolvedCall]]:
        return ([], [])

    def supported_extensions(self) -> list[str]:
        return [".java"]


class TestPluginRegistry:
    """Tests for PluginRegistry."""

    def test_register_and_lookup_plugin(self) -> None:
        """Register a mock plugin and look it up by extension."""
        registry = PluginRegistry()
        plugin = MockCppPlugin()
        registry.register("cpp", plugin)

        result = registry.lookup_by_extension(".cpp")
        assert result is plugin

        result_h = registry.lookup_by_extension(".h")
        assert result_h is plugin

        result_hpp = registry.lookup_by_extension(".hpp")
        assert result_hpp is plugin

    def test_lookup_unknown_extension_returns_none(self) -> None:
        """Looking up an unregistered extension returns None."""
        registry = PluginRegistry()
        plugin = MockCppPlugin()
        registry.register("cpp", plugin)

        result = registry.lookup_by_extension(".py")
        assert result is None

    def test_list_registered_plugins(self) -> None:
        """List all registered plugins by language name."""
        registry = PluginRegistry()
        cpp_plugin = MockCppPlugin()
        java_plugin = MockJavaPlugin()

        registry.register("cpp", cpp_plugin)
        registry.register("java", java_plugin)

        registered = registry.list_plugins()
        assert "cpp" in registered
        assert "java" in registered
        assert registered["cpp"] is cpp_plugin
        assert registered["java"] is java_plugin

    def test_protocol_is_language_agnostic(self) -> None:
        """Verify a mock JavaPlugin satisfies the Protocol without changes."""
        registry = PluginRegistry()
        java_plugin = MockJavaPlugin()

        # Should register and look up without any issues
        registry.register("java", java_plugin)
        result = registry.lookup_by_extension(".java")
        assert result is java_plugin

        # Verify it implements the protocol structurally
        assert isinstance(java_plugin, LanguagePlugin)

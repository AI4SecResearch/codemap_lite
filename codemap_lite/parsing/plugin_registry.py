"""PluginRegistry — manages language plugin registration and lookup."""
from __future__ import annotations

from codemap_lite.parsing.base_plugin import LanguagePlugin


class PluginRegistry:
    """Registry for language plugins.

    Plugins are registered by language name and looked up by file extension.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, LanguagePlugin] = {}
        self._extension_map: dict[str, LanguagePlugin] = {}

    def register(self, language: str, plugin: LanguagePlugin) -> None:
        """Register a plugin for a given language.

        Args:
            language: Language name (e.g. "cpp", "java").
            plugin: Plugin instance satisfying the LanguagePlugin protocol.
        """
        self._plugins[language] = plugin
        for ext in plugin.supported_extensions():
            self._extension_map[ext] = plugin

    def lookup_by_extension(self, extension: str) -> LanguagePlugin | None:
        """Look up a plugin by file extension.

        Args:
            extension: File extension including the dot (e.g. ".cpp").

        Returns:
            The registered plugin, or None if no plugin handles this extension.
        """
        return self._extension_map.get(extension)

    def list_plugins(self) -> dict[str, LanguagePlugin]:
        """List all registered plugins.

        Returns:
            Dict mapping language names to plugin instances.
        """
        return dict(self._plugins)

"""FileScanner — recursively scans directories for source files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from codemap_lite.parsing.types import FileChanges, ScannedFile

DEFAULT_EXTENSIONS: list[str] = [".cpp", ".cc", ".cxx", ".h", ".hpp"]

EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
}


class FileScanner:
    """Scans a target directory for source files and tracks changes."""

    def scan(
        self,
        target_dir: Path,
        extensions: list[str] | None = None,
    ) -> list[ScannedFile]:
        """Recursively scan target_dir for files matching extensions.

        Args:
            target_dir: Root directory to scan.
            extensions: File extensions to include. Defaults to C/C++ extensions.

        Returns:
            List of ScannedFile entries with relative paths, hashes, and language.
        """
        if extensions is None:
            extensions = DEFAULT_EXTENSIONS

        results: list[ScannedFile] = []
        for file_path in sorted(target_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.suffix not in extensions:
                continue

            relative = file_path.relative_to(target_dir).as_posix()
            file_hash = self._compute_hash(file_path)
            language = EXTENSION_LANGUAGE_MAP.get(file_path.suffix, "unknown")

            results.append(
                ScannedFile(
                    file_path=relative,
                    hash=file_hash,
                    primary_language=language,
                )
            )

        return results

    def detect_changes(
        self, target_dir: Path, state_path: Path
    ) -> FileChanges:
        """Compare current scan against saved state to find changes.

        Args:
            target_dir: Root directory to scan.
            state_path: Path to the state.json file.

        Returns:
            FileChanges with added, modified, and deleted file lists.
        """
        old_state = self.load_state(state_path)
        current_files = self.scan(target_dir)

        current_map = {f.file_path: f.hash for f in current_files}

        added: list[str] = []
        modified: list[str] = []
        deleted: list[str] = []

        for path, new_hash in current_map.items():
            if path not in old_state:
                added.append(path)
            elif old_state[path] != new_hash:
                modified.append(path)

        for path in old_state:
            if path not in current_map:
                deleted.append(path)

        return FileChanges(added=added, modified=modified, deleted=deleted)

    def save_state(self, files: list[ScannedFile], state_path: Path) -> None:
        """Persist scan results as a JSON mapping of path to hash.

        Args:
            files: List of scanned files to save.
            state_path: Path to write the state.json file.
        """
        state = {f.file_path: f.hash for f in files}
        state_path.write_text(json.dumps(state, indent=2))

    def load_state(self, state_path: Path) -> dict[str, str]:
        """Load a previously saved state file.

        Args:
            state_path: Path to the state.json file.

        Returns:
            Dict mapping relative file paths to their SHA256 hashes.
            Returns empty dict if the file does not exist.
        """
        if not state_path.exists():
            return {}
        return json.loads(state_path.read_text())

    @staticmethod
    def _compute_hash(file_path: Path) -> str:
        """Compute SHA256 hash of a file's contents."""
        content = file_path.read_bytes()
        return hashlib.sha256(content).hexdigest()

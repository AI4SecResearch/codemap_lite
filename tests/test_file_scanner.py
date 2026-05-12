"""Tests for FileScanner — TDD Phase 1.6."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from codemap_lite.parsing.file_scanner import FileScanner
from codemap_lite.parsing.types import FileChanges, ScannedFile


@pytest.fixture
def scanner() -> FileScanner:
    return FileScanner()


@pytest.fixture
def cpp_tree(tmp_path: Path) -> Path:
    """Create a temp directory with C/C++ source files."""
    (tmp_path / "main.cpp").write_text("int main() { return 0; }")
    (tmp_path / "util.h").write_text("#pragma once")
    (tmp_path / "lib.cc").write_text("void lib() {}")
    (tmp_path / "impl.cxx").write_text("void impl() {}")
    (tmp_path / "header.hpp").write_text("class Foo {};")
    return tmp_path


class TestScan:
    def test_scan_finds_cpp_files(
        self, scanner: FileScanner, cpp_tree: Path
    ) -> None:
        results = scanner.scan(cpp_tree)
        paths = {r.file_path for r in results}
        assert "main.cpp" in paths
        assert "util.h" in paths
        assert "lib.cc" in paths
        assert "impl.cxx" in paths
        assert "header.hpp" in paths
        assert len(results) == 5

    def test_scan_ignores_non_cpp_files(
        self, scanner: FileScanner, tmp_path: Path
    ) -> None:
        (tmp_path / "readme.txt").write_text("hello")
        (tmp_path / "script.py").write_text("print('hi')")
        (tmp_path / "main.cpp").write_text("int main() {}")
        results = scanner.scan(tmp_path)
        paths = {r.file_path for r in results}
        assert "readme.txt" not in paths
        assert "script.py" not in paths
        assert "main.cpp" in paths
        assert len(results) == 1

    def test_scan_computes_sha256(
        self, scanner: FileScanner, tmp_path: Path
    ) -> None:
        content = "int main() { return 42; }"
        (tmp_path / "main.cpp").write_text(content)
        results = scanner.scan(tmp_path)
        expected_hash = hashlib.sha256(content.encode()).hexdigest()
        assert results[0].hash == expected_hash

    def test_scan_sets_primary_language(
        self, scanner: FileScanner, cpp_tree: Path
    ) -> None:
        results = scanner.scan(cpp_tree)
        for r in results:
            assert r.primary_language == "cpp"

    def test_scan_recursive(
        self, scanner: FileScanner, tmp_path: Path
    ) -> None:
        sub = tmp_path / "src" / "core"
        sub.mkdir(parents=True)
        (sub / "engine.cpp").write_text("void run() {}")
        results = scanner.scan(tmp_path)
        paths = {r.file_path for r in results}
        assert "src/core/engine.cpp" in paths

    def test_scan_custom_extensions(
        self, scanner: FileScanner, tmp_path: Path
    ) -> None:
        (tmp_path / "app.py").write_text("print('hi')")
        (tmp_path / "main.cpp").write_text("int main() {}")
        results = scanner.scan(tmp_path, extensions=[".py"])
        paths = {r.file_path for r in results}
        assert "app.py" in paths
        assert "main.cpp" not in paths


class TestDetectChanges:
    def test_detect_changes_new_file(
        self, scanner: FileScanner, cpp_tree: Path, tmp_path: Path
    ) -> None:
        state_path = tmp_path / "state.json"
        changes = scanner.detect_changes(cpp_tree, state_path)
        assert len(changes.added) == 5
        assert changes.modified == []
        assert changes.deleted == []

    def test_detect_changes_modified_file(
        self, scanner: FileScanner, tmp_path: Path
    ) -> None:
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        cpp_file = src_dir / "main.cpp"
        cpp_file.write_text("int main() { return 0; }")

        state_path = tmp_path / "state.json"
        # Save initial state
        files = scanner.scan(src_dir)
        scanner.save_state(files, state_path)

        # Modify the file
        cpp_file.write_text("int main() { return 1; }")

        changes = scanner.detect_changes(src_dir, state_path)
        assert "main.cpp" in changes.modified
        assert changes.added == []
        assert changes.deleted == []

    def test_detect_changes_deleted_file(
        self, scanner: FileScanner, tmp_path: Path
    ) -> None:
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        cpp_file = src_dir / "main.cpp"
        cpp_file.write_text("int main() {}")

        state_path = tmp_path / "state.json"
        files = scanner.scan(src_dir)
        scanner.save_state(files, state_path)

        # Delete the file
        cpp_file.unlink()

        changes = scanner.detect_changes(src_dir, state_path)
        assert "main.cpp" in changes.deleted
        assert changes.added == []
        assert changes.modified == []


class TestState:
    def test_save_and_load_state(
        self, scanner: FileScanner, cpp_tree: Path, tmp_path: Path
    ) -> None:
        state_path = tmp_path / "state.json"
        files = scanner.scan(cpp_tree)
        scanner.save_state(files, state_path)

        loaded = scanner.load_state(state_path)
        assert isinstance(loaded, dict)
        assert len(loaded) == 5
        for f in files:
            assert loaded[f.file_path] == f.hash

    def test_load_state_missing_file(
        self, scanner: FileScanner, tmp_path: Path
    ) -> None:
        state_path = tmp_path / "nonexistent.json"
        loaded = scanner.load_state(state_path)
        assert loaded == {}

    def test_state_json_format(
        self, scanner: FileScanner, tmp_path: Path
    ) -> None:
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "a.cpp").write_text("void a() {}")

        state_path = tmp_path / "state.json"
        files = scanner.scan(src_dir)
        scanner.save_state(files, state_path)

        raw = json.loads(state_path.read_text())
        assert isinstance(raw, dict)
        assert "a.cpp" in raw
        assert len(raw["a.cpp"]) == 64  # SHA256 hex length


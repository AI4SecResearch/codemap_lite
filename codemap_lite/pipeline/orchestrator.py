"""Pipeline Orchestrator — coordinates full analysis workflow."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from codemap_lite.graph.incremental import IncrementalUpdater
from codemap_lite.graph.neo4j_store import InMemoryGraphStore, GraphStore
from codemap_lite.graph.schema import FunctionNode, FileNode, CallsEdgeProps, UnresolvedCallNode
from codemap_lite.parsing.file_scanner import FileScanner
from codemap_lite.parsing.plugin_registry import PluginRegistry
from codemap_lite.parsing.types import CallType


def _make_function_id(file_path: str, name: str, start_line: int) -> str:
    """Generate a short, URL-safe ID for a function node.

    Returns the first 12 hex chars of sha1(file_path:name:start_line).
    This eliminates path separators and colons from IDs so they survive
    HTTP path normalization (which collapses consecutive slashes).
    """
    payload = f"{file_path}:{name}:{start_line}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


@dataclass
class PipelineResult:
    """Result of a pipeline run."""

    success: bool = True
    files_scanned: int = 0
    functions_found: int = 0
    direct_calls: int = 0
    unresolved_calls: int = 0
    files_changed: int = 0
    errors: list[str] = field(default_factory=list)


class PipelineOrchestrator:
    """Orchestrates the full analysis pipeline: scan → parse → store."""

    def __init__(
        self,
        target_dir: Path,
        store: GraphStore | None = None,
        registry: PluginRegistry | None = None,
    ) -> None:
        self._target_dir = target_dir
        self._store = store or InMemoryGraphStore()
        self._scanner = FileScanner()
        self._registry = registry or self._default_registry()
        self._state_path = target_dir / ".icslpreprocess" / "state.json"

    def _default_registry(self) -> PluginRegistry:
        """Create registry with default plugins."""
        registry = PluginRegistry()
        try:
            from codemap_lite.parsing.cpp.plugin import CppPlugin
            registry.register("cpp", CppPlugin())
        except ImportError:
            pass
        return registry

    def run_full_analysis(self) -> PipelineResult:
        """Run full analysis: scan all files, parse, store."""
        result = PipelineResult()

        # Scan files
        scanned = self._scanner.scan(self._target_dir)
        result.files_scanned = len(scanned)

        if not scanned:
            return result

        # Save state for future incremental runs
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._scanner.save_state(scanned, self._state_path)

        # Parse and store
        self._parse_and_store(scanned, result)

        return result

    def run_incremental_analysis(self) -> PipelineResult:
        """Run incremental analysis: only process changed files."""
        result = PipelineResult()

        # Detect changes
        changes = self._scanner.detect_changes(self._target_dir, self._state_path)
        changed_files = changes.added + changes.modified
        result.files_changed = len(changed_files) + len(changes.deleted)

        if not changed_files and not changes.deleted:
            result.success = True
            return result

        # Invalidate deleted and modified files (architecture.md §7 step 2):
        # Remove stale functions/edges before re-parsing modified files.
        updater = IncrementalUpdater(store=self._store)
        affected_caller_files: set[str] = set()
        for deleted_file in changes.deleted:
            inv_result = updater.invalidate_file(deleted_file)
            # Collect files of affected callers for re-parsing
            for caller_id in inv_result.affected_callers:
                fn = self._store.get_function_by_id(caller_id)
                if fn is not None:
                    affected_caller_files.add(fn.file_path)
        for modified_file in changes.modified:
            inv_result = updater.invalidate_file(modified_file)
            for caller_id in inv_result.affected_callers:
                fn = self._store.get_function_by_id(caller_id)
                if fn is not None:
                    affected_caller_files.add(fn.file_path)

        # Also re-parse files containing affected callers so non-LLM edges
        # (symbol_table) are re-discovered (architecture.md §7 step 2).
        for af in affected_caller_files:
            if af not in changed_files:
                changed_files.append(af)

        # Re-scan to update state
        scanned = self._scanner.scan(self._target_dir)
        self._scanner.save_state(scanned, self._state_path)

        # Only parse changed files
        changed_scanned = [f for f in scanned if f.file_path in changed_files]
        result.files_scanned = len(changed_scanned)

        self._parse_and_store(changed_scanned, result)

        return result

    def _parse_and_store(self, scanned_files: list, result: PipelineResult) -> None:
        """Parse scanned files and store results in graph."""
        all_symbols: dict[str, any] = {}

        # First pass: extract all function definitions
        for sf in scanned_files:
            plugin = self._registry.lookup_by_extension(
                Path(sf.file_path).suffix
            )
            if plugin is None:
                continue

            file_path = Path(sf.file_path)
            if not file_path.is_absolute():
                file_path = self._target_dir / file_path

            try:
                # Fix 1: Create FileNode once per file (outside function loop)
                file_node = FileNode(
                    id=sf.file_path,
                    file_path=sf.file_path,
                    hash=sf.hash,
                    primary_language=sf.primary_language,
                )
                self._store.create_file(file_node)

                functions = plugin.parse_file(file_path)
                for func in functions:
                    # Store function node (short hash ID, URL-safe)
                    func_node = FunctionNode(
                        id=_make_function_id(str(func.file_path), func.name, func.start_line),
                        signature=func.signature,
                        name=func.name,
                        file_path=str(func.file_path),
                        start_line=func.start_line,
                        end_line=func.end_line,
                        body_hash=func.body_hash,
                    )
                    self._store.create_function(func_node)
                    # Fix 2: Register both qualified and bare name
                    all_symbols[func.name] = func
                    if "::" in func.name:
                        bare = func.name.split("::")[-1]
                        all_symbols.setdefault(bare, func)
                    result.functions_found += 1
            except Exception as e:
                result.errors.append(f"Error parsing {sf.file_path}: {e}")

        # Build class hierarchy (between first and second pass)
        all_file_paths = []
        for sf in scanned_files:
            fp = Path(sf.file_path)
            if not fp.is_absolute():
                fp = self._target_dir / fp
            all_file_paths.append(fp)

        # Call build_hierarchy on the cpp plugin if available
        cpp_plugin = self._registry.lookup_by_extension(".cpp")
        if cpp_plugin and hasattr(cpp_plugin, "build_hierarchy"):
            cpp_plugin.build_hierarchy(all_file_paths)

        # Build lookup indexes for the second pass.
        #
        # Three buckets with decreasing specificity:
        #   by_file_name  — exact (file, name) → id. Always trustworthy.
        #   by_name       — qualified name → [ids]. Multiple ids = ambiguous.
        #   by_bare_name  — bare name (last ::-segment) → [ids]. Used only
        #                   as a last-resort fallback when the plugin
        #                   dropped qualification. Multiple ids = ambiguous.
        #
        # The previous implementation used `setdefault` on a flat
        # name → id map, which silently picked the first registered
        # definition and cross-linked unrelated modules (e.g. `Clear`
        # in data_buffer.h landing on `Clear` in preferences_util.cpp).
        by_file_name: dict[tuple[str, str], str] = {}
        by_name: dict[str, list[str]] = {}
        by_bare_name: dict[str, list[str]] = {}
        if hasattr(self._store, "_functions"):
            for fid, fn in self._store._functions.items():
                by_file_name[(fn.file_path, fn.name)] = fid
                by_name.setdefault(fn.name, []).append(fid)
                if "::" in fn.name:
                    bare = fn.name.split("::")[-1]
                    by_bare_name.setdefault(bare, []).append(fid)
                else:
                    by_bare_name.setdefault(fn.name, []).append(fid)

        def _resolve_id(call_file: str, name: str) -> str | None:
            """Resolve (file, name) to a single function id.

            Returns None when the lookup is ambiguous or unknown. In
            both cases the caller must emit an UnresolvedCall rather
            than a CALLS edge. We deliberately do NOT pick an arbitrary
            candidate: that reintroduces the cross-module pollution
            Phase 2 exists to eliminate.
            """
            fid = by_file_name.get((call_file, name))
            if fid is not None:
                return fid
            fids = by_name.get(name, [])
            if len(fids) == 1:
                return fids[0]
            if len(fids) > 1:
                return None
            fids = by_bare_name.get(name, [])
            if len(fids) == 1:
                return fids[0]
            return None

        def _candidate_names(name: str) -> list[str]:
            """Return readable candidate function names for UC metadata."""
            fids = by_name.get(name) or by_bare_name.get(name) or []
            names: list[str] = []
            seen: set[str] = set()
            for fid in fids:
                fn = self._store._functions.get(fid) if hasattr(self._store, "_functions") else None
                if fn is None:
                    continue
                if fn.name in seen:
                    continue
                seen.add(fn.name)
                names.append(fn.name)
            return names

        # Second pass: build call edges
        for sf in scanned_files:
            plugin = self._registry.lookup_by_extension(
                Path(sf.file_path).suffix
            )
            if plugin is None:
                continue

            file_path = Path(sf.file_path)
            if not file_path.is_absolute():
                file_path = self._target_dir / file_path

            try:
                calls, unresolved = plugin.build_calls(file_path, all_symbols)

                for call in calls:
                    caller_id = _resolve_id(str(call.call_file), call.caller_name)
                    if caller_id is None:
                        # Nothing sensible to anchor on — the caller
                        # itself isn't a known function.
                        continue

                    # Phase 3: only truly-direct calls become CALLS
                    # edges. VIRTUAL / INDIRECT / CALLBACK calls are
                    # guesses from the plugin and must be surfaced as
                    # UnresolvedCalls so resolved_by=symbol_table never
                    # contradicts call_type=virtual.
                    is_direct = call.call_type == CallType.DIRECT
                    callee_id = _resolve_id(str(call.call_file), call.callee_name) if is_direct else None

                    if not is_direct or callee_id is None:
                        # Ambiguous / indirect — record as UC with the
                        # real candidate list rather than synthesising
                        # a bogus edge.
                        candidates = _candidate_names(call.callee_name) or [call.callee_name]
                        uc_id = f"gap:{call.call_file}:{call.call_line}:{call.callee_name[:50]}"
                        uc_node = UnresolvedCallNode(
                            id=uc_id,
                            caller_id=caller_id,
                            call_expression=call.callee_name,
                            call_file=str(call.call_file),
                            call_line=call.call_line,
                            call_type=call.call_type.value,
                            source_code_snippet="",
                            var_name=None,
                            var_type=None,
                            candidates=candidates,
                        )
                        self._store.create_unresolved_call(uc_node)
                        result.unresolved_calls += 1
                        continue

                    props = CallsEdgeProps(
                        resolved_by=call.resolved_by,
                        call_type=call.call_type.value,
                        call_file=str(call.call_file),
                        call_line=call.call_line,
                    )
                    self._store.create_calls_edge(caller_id, callee_id, props)
                    result.direct_calls += 1

                result.unresolved_calls += len(unresolved)

                # Store unresolved calls in graph
                for uc in unresolved:
                    caller_id = _resolve_id(str(uc.call_file), uc.caller_name)
                    if caller_id is None:
                        # Caller itself isn't a known function — skip.
                        continue

                    uc_node = UnresolvedCallNode(
                        id=f"gap:{uc.call_file}:{uc.call_line}:{uc.call_expression[:50]}",
                        caller_id=caller_id,
                        call_expression=uc.call_expression,
                        call_file=str(uc.call_file),
                        call_line=uc.call_line,
                        call_type=uc.call_type.value,
                        source_code_snippet=uc.source_code_snippet,
                        var_name=uc.var_name,
                        var_type=uc.var_type,
                        candidates=uc.candidates,
                    )
                    self._store.create_unresolved_call(uc_node)
            except Exception as e:
                result.errors.append(f"Error building calls for {sf.file_path}: {e}")

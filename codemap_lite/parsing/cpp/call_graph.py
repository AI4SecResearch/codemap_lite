"""Extract call edges from C/C++ source using tree-sitter."""
from __future__ import annotations

from pathlib import Path

import tree_sitter as ts
import tree_sitter_cpp as tscpp

from codemap_lite.parsing.types import CallEdge, CallType, FunctionDef, UnresolvedCall
from codemap_lite.parsing.cpp.dispatch_classifier import DispatchInfo, classify_call
from codemap_lite.parsing.cpp.class_hierarchy import ClassHierarchyIndex
from codemap_lite.parsing.cpp.library_whitelist import is_library_call

_LANGUAGE = ts.Language(tscpp.language())
_PARSER = ts.Parser(_LANGUAGE)

# Parameter types that indicate indirect calls
_INDIRECT_TYPE_PATTERNS = (
    "std::function",
    "function<",
    "FuncPtr",
    "Callback",
    "Handler",
)

# Suffixes that suggest a variable is a function pointer or callback
_INDIRECT_NAME_PATTERNS = (
    "ptr",
    "callback",
    "cb",
    "handler",
    "fn",
    "func",
)


def build_calls(
    source: bytes,
    file_path: Path,
    symbols: dict[str, FunctionDef],
    hierarchy: ClassHierarchyIndex | None = None,
) -> tuple[list[CallEdge], list[UnresolvedCall]]:
    """Build call edges from C/C++ source.

    Uses dispatch classification + 3-layer resolution:
    1. Dispatch classifier: identify member_fn_ptr, ipc_proxy, virtual, callback patterns.
    2. Signature matching: resolve calls by name against known symbols.
    3. Data flow: track parameter types for pointer targets.
    4. Context inference: use naming patterns to classify indirect calls.

    Args:
        source: Raw source bytes.
        file_path: Path to the source file.
        symbols: Dict mapping function names to their definitions.
        hierarchy: Optional class hierarchy index for candidate resolution.

    Returns:
        Tuple of (resolved direct calls, unresolved/indirect calls).
    """
    tree = _PARSER.parse(source)
    direct_calls: list[CallEdge] = []
    unresolved_calls: list[UnresolvedCall] = []

    # Collect parameter info for indirect call detection
    param_types = _collect_param_types(tree.root_node)

    # Walk all function definitions and find calls within them
    _walk_functions_for_calls(
        tree.root_node,
        source,
        file_path,
        symbols,
        param_types,
        hierarchy,
        direct_calls,
        unresolved_calls,
    )

    # Filter out known library/system calls (architecture.md §1 whitelist)
    unresolved_calls = [
        uc for uc in unresolved_calls
        if not is_library_call(uc.call_expression)
    ]

    return direct_calls, unresolved_calls


def _collect_param_types(root: ts.Node) -> dict[str, str]:
    """Collect parameter name -> type mappings from function definitions.

    This enables detecting when a call target is actually a function pointer
    or std::function parameter.
    """
    param_types: dict[str, str] = {}
    _walk_for_param_types(root, param_types)
    return param_types


def _walk_for_param_types(node: ts.Node, param_types: dict[str, str]) -> None:
    """Recursively find parameter declarations and record their types."""
    if node.type == "parameter_declaration":
        type_node = node.child_by_field_name("type")
        decl_node = node.child_by_field_name("declarator")
        if type_node and decl_node:
            type_text = type_node.text.decode()
            name_text = decl_node.text.decode()
            param_types[name_text] = type_text
    for child in node.children:
        _walk_for_param_types(child, param_types)


def _walk_functions_for_calls(
    node: ts.Node,
    source: bytes,
    file_path: Path,
    symbols: dict[str, FunctionDef],
    param_types: dict[str, str],
    hierarchy: ClassHierarchyIndex | None,
    direct_calls: list[CallEdge],
    unresolved_calls: list[UnresolvedCall],
    current_function: str | None = None,
    local_vars: dict[str, str] | None = None,
) -> None:
    """Walk the AST, tracking which function we're inside, and process calls."""
    if node.type == "function_definition":
        declarator = node.child_by_field_name("declarator")
        if declarator and declarator.type == "function_declarator":
            func_name = _get_func_name_from_declarator(declarator)
            if func_name:
                current_function = func_name
                # Collect local variables for this function body
                body = node.child_by_field_name("body")
                local_vars = _collect_local_vars(body, param_types) if body else {}

    if node.type == "call_expression" and current_function:
        _process_call(
            node,
            source,
            file_path,
            symbols,
            param_types,
            local_vars or {},
            hierarchy,
            current_function,
            direct_calls,
            unresolved_calls,
        )

    for child in node.children:
        _walk_functions_for_calls(
            child,
            source,
            file_path,
            symbols,
            param_types,
            hierarchy,
            direct_calls,
            unresolved_calls,
            current_function,
            local_vars,
        )


def _get_func_name_from_declarator(declarator: ts.Node) -> str | None:
    """Extract function name from a function_declarator node."""
    for child in declarator.children:
        if child.type in ("identifier", "field_identifier"):
            return child.text.decode()
        if child.type == "qualified_identifier":
            parts = child.text.decode().split("::")
            return parts[-1]
    return None


def _process_call(
    call_node: ts.Node,
    source: bytes,
    file_path: Path,
    symbols: dict[str, FunctionDef],
    param_types: dict[str, str],
    local_vars: dict[str, str],
    hierarchy: ClassHierarchyIndex | None,
    caller_name: str,
    direct_calls: list[CallEdge],
    unresolved_calls: list[UnresolvedCall],
) -> None:
    """Process a single call_expression node using dispatch classification.

    Flow:
    1. classify_call() determines the dispatch pattern.
    2. Route based on call_type:
       - MEMBER_FN_PTR → UnresolvedCall + candidates from hierarchy
       - IPC_PROXY → UnresolvedCall
       - VIRTUAL → try symbol lookup, else UnresolvedCall + CHA candidates
       - CALLBACK → UnresolvedCall
       - DIRECT → existing 3-layer resolution
    """
    call_line = call_node.start_point[0] + 1
    call_text = call_node.text.decode()

    # Step 1: Classify the dispatch pattern
    dispatch = classify_call(call_node, local_vars)

    # Step 2: Route based on classification
    if dispatch.call_type == CallType.MEMBER_FN_PTR:
        candidates = []
        if hierarchy and dispatch.array_name:
            candidates = hierarchy.get_fn_ptr_array_candidates(dispatch.array_name)
        unresolved_calls.append(
            UnresolvedCall(
                caller_name=caller_name,
                call_expression=call_text,
                call_file=file_path,
                call_line=call_line,
                call_type=CallType.MEMBER_FN_PTR,
                var_name=dispatch.array_name or call_text,
                var_type="member_fn_ptr_array",
                candidates=candidates,
                source_code_snippet=call_text,
            )
        )
        return

    if dispatch.call_type == CallType.IPC_PROXY:
        unresolved_calls.append(
            UnresolvedCall(
                caller_name=caller_name,
                call_expression=call_text,
                call_file=file_path,
                call_line=call_line,
                call_type=CallType.IPC_PROXY,
                var_name=dispatch.callee_name or "SendRequest",
                var_type="ipc_proxy",
                candidates=[],
                source_code_snippet=call_text,
            )
        )
        return

    if dispatch.call_type == CallType.VIRTUAL:
        callee_name = dispatch.callee_name
        # Try to resolve: is the method in symbols?
        if callee_name and callee_name in symbols:
            direct_calls.append(
                CallEdge(
                    caller_name=caller_name,
                    callee_name=callee_name,
                    call_file=file_path,
                    call_line=call_line,
                    call_type=CallType.VIRTUAL,
                    resolved_by="symbol_table",
                )
            )
        else:
            candidates = []
            if hierarchy and callee_name:
                candidates = hierarchy.get_virtual_candidates(callee_name)
            unresolved_calls.append(
                UnresolvedCall(
                    caller_name=caller_name,
                    call_expression=call_text,
                    call_file=file_path,
                    call_line=call_line,
                    call_type=CallType.VIRTUAL,
                    var_name=dispatch.receiver_name or "",
                    var_type="virtual_dispatch",
                    candidates=candidates,
                    source_code_snippet=call_text,
                )
            )
        return

    if dispatch.call_type == CallType.CALLBACK:
        callee_name = dispatch.callee_name or ""
        var_type = local_vars.get(callee_name, param_types.get(callee_name, ""))
        unresolved_calls.append(
            UnresolvedCall(
                caller_name=caller_name,
                call_expression=call_text,
                call_file=file_path,
                call_line=call_line,
                call_type=CallType.CALLBACK,
                var_name=callee_name,
                var_type=var_type,
                candidates=[],
                source_code_snippet=call_text,
            )
        )
        return

    # DIRECT dispatch — use existing 3-layer resolution
    callee_name = dispatch.callee_name
    if callee_name is None:
        # Cannot resolve — generic indirect
        fn_node = call_node.child_by_field_name("function")
        unresolved_calls.append(
            UnresolvedCall(
                caller_name=caller_name,
                call_expression=call_text,
                call_file=file_path,
                call_line=call_line,
                call_type=CallType.INDIRECT,
                var_name=fn_node.text.decode() if fn_node else call_text,
                var_type="",
                candidates=[],
                source_code_snippet=call_text,
            )
        )
        return

    # Layer 1: Signature matching
    if callee_name in symbols:
        direct_calls.append(
            CallEdge(
                caller_name=caller_name,
                callee_name=callee_name,
                call_file=file_path,
                call_line=call_line,
                call_type=CallType.DIRECT,
                resolved_by="symbol_table",
            )
        )
        return

    # Layer 2: Data flow — parameter with indirect type
    if callee_name in param_types:
        var_type = param_types[callee_name]
        if _is_indirect_type(var_type):
            unresolved_calls.append(
                UnresolvedCall(
                    caller_name=caller_name,
                    call_expression=call_text,
                    call_file=file_path,
                    call_line=call_line,
                    call_type=CallType.CALLBACK,
                    var_name=callee_name,
                    var_type=var_type,
                    candidates=[],
                    source_code_snippet=call_text,
                )
            )
            return

    # Layer 3: Context inference — naming patterns
    if _is_indirect_name(callee_name):
        var_type = param_types.get(callee_name, "")
        unresolved_calls.append(
            UnresolvedCall(
                caller_name=caller_name,
                call_expression=call_text,
                call_file=file_path,
                call_line=call_line,
                call_type=CallType.INDIRECT,
                var_name=callee_name,
                var_type=var_type,
                candidates=[],
                source_code_snippet=call_text,
            )
        )
        return

    # Not in symbols and not indirect — external direct call
    direct_calls.append(
        CallEdge(
            caller_name=caller_name,
            callee_name=callee_name,
            call_file=file_path,
            call_line=call_line,
            call_type=CallType.DIRECT,
            resolved_by="symbol_table",
        )
    )


def _extract_callee_name(fn_node: ts.Node) -> str | None:
    """Extract the callee function name from the 'function' field of a call.

    Returns:
        The callee name string, or None if the call is through a complex
        expression (e.g., parenthesized pointer dereference).
    """
    if fn_node.type == "identifier":
        return fn_node.text.decode()
    if fn_node.type == "field_expression":
        # obj.method or obj->method — extract the field name
        for child in fn_node.children:
            if child.type == "field_identifier":
                return child.text.decode()
    if fn_node.type == "qualified_identifier":
        # Namespace::func or Class::method
        parts = fn_node.text.decode().split("::")
        return parts[-1]
    if fn_node.type == "template_function":
        # template_func<T>(args) — get the name part
        for child in fn_node.children:
            if child.type == "identifier":
                return child.text.decode()
    # Parenthesized or complex expression — cannot resolve statically
    return None


# ---------------------------------------------------------------------------
# Fix 5: Local variable tracking
# ---------------------------------------------------------------------------


def _collect_local_vars(body_node: ts.Node, param_types: dict[str, str]) -> dict[str, str]:
    """Collect local variable declarations and classify their sources.

    Returns dict mapping variable name -> classification:
    - "weak_ptr_lock": assigned from xxx.lock()
    - "pointer": declared as pointer type or from container iteration
    - "shared_ptr": std::shared_ptr variable
    - type string: for callback types (std::function, etc.)
    """
    local_vars: dict[str, str] = {}
    # Include parameter types as local vars
    for name, vtype in param_types.items():
        if "*" in vtype or "shared_ptr" in vtype:
            local_vars[name] = "pointer"
        elif "std::function" in vtype or "Callback" in vtype:
            local_vars[name] = vtype
    _walk_for_local_vars(body_node, local_vars)
    return local_vars


def _walk_for_local_vars(node: ts.Node, local_vars: dict[str, str]) -> None:
    """Walk declarations to classify local variables."""
    if node.type == "declaration":
        _process_declaration(node, local_vars)
    elif node.type == "for_range_loop":
        _process_range_loop(node, local_vars)

    for child in node.children:
        if child.type != "function_definition":  # Don't descend into nested functions
            _walk_for_local_vars(child, local_vars)


def _process_declaration(node: ts.Node, local_vars: dict[str, str]) -> None:
    """Process a declaration node to extract variable type info."""
    # Get the type specifier
    type_text = ""
    for child in node.children:
        if child.type in ("type_identifier", "primitive_type", "qualified_identifier",
                          "template_type", "auto"):
            type_text = child.text.decode()
            break

    # Find init_declarator children
    for child in node.children:
        if child.type == "init_declarator":
            var_name = None
            init_value = None
            for sub in child.children:
                if sub.type == "identifier":
                    var_name = sub.text.decode()
                elif sub.type == "pointer_declarator":
                    # int* ptr = ...
                    for psub in sub.children:
                        if psub.type == "identifier":
                            var_name = psub.text.decode()
                elif sub.type == "call_expression":
                    init_value = sub
                elif sub.type == "field_expression":
                    init_value = sub

            if var_name:
                # Check if initialized from .lock()
                if init_value and _is_lock_call(init_value):
                    local_vars[var_name] = "weak_ptr_lock"
                elif "shared_ptr" in type_text:
                    local_vars[var_name] = "shared_ptr"
                elif "*" in type_text or "pointer" in type_text.lower():
                    local_vars[var_name] = "pointer"
                elif "std::function" in type_text or "Callback" in type_text:
                    local_vars[var_name] = type_text


def _process_range_loop(node: ts.Node, local_vars: dict[str, str]) -> None:
    """Process for-range loop to classify iteration variables as pointers."""
    # for (auto& [k, v] : container) or for (auto& item : container)
    decl = node.child_by_field_name("declarator")
    if decl is None:
        # Try to find structured_binding_declarator
        for child in node.children:
            if child.type == "structured_binding_declarator":
                # All bindings from a container are likely pointers/references
                for sub in child.children:
                    if sub.type == "identifier":
                        local_vars[sub.text.decode()] = "pointer"
            elif child.type == "identifier":
                local_vars[child.text.decode()] = "pointer"


def _is_lock_call(node: ts.Node) -> bool:
    """Check if a node is a call to .lock() (weak_ptr pattern)."""
    if node.type == "call_expression":
        fn = node.child_by_field_name("function")
        if fn and fn.type == "field_expression":
            for child in fn.children:
                if child.type == "field_identifier" and child.text == b"lock":
                    return True
    elif node.type == "field_expression":
        # Direct field expression check
        for child in node.children:
            if child.type == "field_identifier" and child.text == b"lock":
                return True
    return False


def _is_indirect_type(type_text: str) -> bool:
    """Check if a type string indicates an indirect call mechanism."""
    for pattern in _INDIRECT_TYPE_PATTERNS:
        if pattern in type_text:
            return True
    # Check for function pointer syntax: contains (*
    if "(*" in type_text:
        return True
    return False


def _is_indirect_name(name: str) -> bool:
    """Check if a variable name suggests it's a function pointer or callback."""
    name_lower = name.lower()
    for pattern in _INDIRECT_NAME_PATTERNS:
        if pattern in name_lower:
            return True
    return False


def _find_candidates_by_type(
    var_type: str, symbols: dict[str, FunctionDef]
) -> list[str]:
    """Find candidate functions that could match the given type signature.

    This is a simplified signature-matching heuristic: if the type contains
    a recognizable return type and parameter pattern, filter symbols accordingly.
    """
    # For now, return empty list — full signature matching would require
    # parsing the type signature and comparing against function signatures.
    # This is a placeholder for the signature-matching layer.
    return []

"""Extract function definitions and symbols from C/C++ source using tree-sitter."""
from __future__ import annotations

import hashlib
from pathlib import Path

import tree_sitter as ts
import tree_sitter_cpp as tscpp

from codemap_lite.parsing.types import FunctionDef, Symbol, SymbolKind

_LANGUAGE = ts.Language(tscpp.language())
_PARSER = ts.Parser(_LANGUAGE)


def _hash_body(body: bytes) -> str:
    """Compute a short hash of a function body."""
    return hashlib.sha256(body).hexdigest()[:16]


def _is_macro_name(name: str) -> bool:
    """Check if name looks like a macro (ALL_CAPS or UPPER_SNAKE_CASE)."""
    return len(name) > 1 and (name.isupper() or (name == name.upper() and "_" in name))


def _extract_macro_function_name(declarator_node: ts.Node) -> str | None:
    """Try to extract real function name from macro-expanded function definition.

    When tree-sitter parses something like DEFINE_HANDLER(ProcessMessage) { ... },
    the macro name becomes the function name and the real name appears as the first
    parameter. This heuristic detects that pattern.
    """
    # Find the parameter_list child of the declarator
    param_list = None
    for child in declarator_node.children:
        if child.type == "parameter_list":
            param_list = child
            break
    if param_list is None:
        return None

    # Look at the first non-punctuation child of the parameter list
    for child in param_list.children:
        if child.type in ("(", ")", ","):
            continue
        if child.type == "type_identifier":
            candidate = child.text.decode()
            # Accept if it looks like a function/class name (starts uppercase, not ALL_CAPS)
            if candidate and candidate[0].isupper() and not candidate.isupper():
                return candidate
        if child.type == "qualified_identifier":
            # e.g. ClassName::MethodName — use the last segment
            parts = child.text.decode().split("::")
            return parts[-1]
        # Also handle parameter_declaration where the type looks like a name
        if child.type == "parameter_declaration":
            for sub in child.children:
                if sub.type == "type_identifier":
                    candidate = sub.text.decode()
                    if candidate and candidate[0].isupper() and not candidate.isupper():
                        return candidate
                if sub.type == "qualified_identifier":
                    parts = sub.text.decode().split("::")
                    return parts[-1]
        # Stop after first meaningful parameter
        break
    return None


def _get_function_name(declarator_node: ts.Node) -> str:
    """Extract the function name from a function_declarator node.

    Handles plain identifiers, field_identifiers (class methods),
    and qualified identifiers (Namespace::func).
    """
    for child in declarator_node.children:
        if child.type in ("identifier", "field_identifier"):
            return child.text.decode()
        if child.type == "qualified_identifier":
            # Return the last identifier segment
            parts = child.text.decode().split("::")
            return parts[-1]
        if child.type == "destructor_name":
            return child.text.decode()
    # Fallback: use the full declarator text minus params
    text = declarator_node.text.decode()
    paren_idx = text.find("(")
    if paren_idx > 0:
        return text[:paren_idx].strip().split()[-1]
    return text.strip()


def _get_signature(func_node: ts.Node) -> str:
    """Build a human-readable signature from a function_definition node."""
    # Collect return type + declarator (without the body)
    parts: list[str] = []
    for child in func_node.children:
        if child.type == "compound_statement":
            break
        parts.append(child.text.decode())
    return " ".join(parts)


def extract_functions(source: bytes, file_path: Path) -> list[FunctionDef]:
    """Extract all function definitions from C/C++ source code.

    Args:
        source: Raw source bytes.
        file_path: Path to the source file (stored in FunctionDef).

    Returns:
        List of FunctionDef instances.
    """
    tree = _PARSER.parse(source)
    results: list[FunctionDef] = []
    _walk_for_functions(tree.root_node, source, file_path, results)
    return results


def _walk_for_functions(
    node: ts.Node,
    source: bytes,
    file_path: Path,
    results: list[FunctionDef],
) -> None:
    """Recursively walk the tree to find function_definition nodes."""
    if node.type == "function_definition":
        declarator = node.child_by_field_name("declarator")
        if declarator and declarator.type == "function_declarator":
            name = _get_function_name(declarator)
            # Detect macro-expanded function definitions
            if _is_macro_name(name):
                real_name = _extract_macro_function_name(declarator)
                if real_name:
                    name = real_name
            signature = _get_signature(node)
            body_node = node.child_by_field_name("body")
            body_bytes = body_node.text if body_node else b""
            results.append(
                FunctionDef(
                    name=name,
                    signature=signature,
                    file_path=file_path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    body_hash=_hash_body(body_bytes),
                )
            )
    for child in node.children:
        _walk_for_functions(child, source, file_path, results)


def extract_symbols(source: bytes, file_path: Path) -> list[Symbol]:
    """Extract all symbols (functions, classes, variables) from C/C++ source.

    Args:
        source: Raw source bytes.
        file_path: Path to the source file.

    Returns:
        List of Symbol instances.
    """
    tree = _PARSER.parse(source)
    results: list[Symbol] = []
    _walk_for_symbols(tree.root_node, file_path, results)
    return results


def _walk_for_symbols(
    node: ts.Node,
    file_path: Path,
    results: list[Symbol],
) -> None:
    """Recursively walk the tree to find symbols."""
    if node.type == "function_definition":
        declarator = node.child_by_field_name("declarator")
        if declarator and declarator.type == "function_declarator":
            name = _get_function_name(declarator)
            # Detect macro-expanded function definitions
            if _is_macro_name(name):
                real_name = _extract_macro_function_name(declarator)
                if real_name:
                    name = real_name
            results.append(
                Symbol(
                    name=name,
                    kind=SymbolKind.FUNCTION,
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                )
            )
    elif node.type == "class_specifier":
        # Extract class name from type_identifier child
        for child in node.children:
            if child.type == "type_identifier":
                results.append(
                    Symbol(
                        name=child.text.decode(),
                        kind=SymbolKind.CLASS,
                        file_path=file_path,
                        line=node.start_point[0] + 1,
                    )
                )
                break
    elif node.type == "struct_specifier":
        for child in node.children:
            if child.type == "type_identifier":
                results.append(
                    Symbol(
                        name=child.text.decode(),
                        kind=SymbolKind.CLASS,
                        file_path=file_path,
                        line=node.start_point[0] + 1,
                    )
                )
                break

    for child in node.children:
        _walk_for_symbols(child, file_path, results)

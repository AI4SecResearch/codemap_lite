"""Classify C++ call_expression AST nodes into dispatch patterns.

Handles patterns from CastEngine including member function pointers,
IPC proxies, virtual dispatch, chained singleton calls, direct calls,
and callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tree_sitter as ts

from codemap_lite.parsing.types import CallType

# IPC proxy factory method names that indicate remote dispatch.
_IPC_PROXY_FACTORIES: frozenset[str] = frozenset({"Remote", "AsObject"})

# Type substrings that indicate a callback variable.
_CALLBACK_TYPE_HINTS: tuple[str, ...] = (
    "std::function",
    "FuncPtr",
    "Callback",
    "Handler",
    "std::bind",
)


@dataclass(frozen=True)
class DispatchInfo:
    """Result of classifying a call_expression node."""

    call_type: CallType
    callee_name: str | None
    receiver_name: str | None
    array_name: str | None
    is_arrow: bool


def _node_text(node: ts.Node | None) -> str:
    """Safely extract UTF-8 text from a tree-sitter node."""
    if node is None:
        return ""
    try:
        return node.text.decode("utf-8", errors="replace")
    except (AttributeError, UnicodeDecodeError):
        return ""


def _child_by_field(node: ts.Node | None, field: str) -> ts.Node | None:
    """Safely get a child by field name."""
    if node is None:
        return None
    return node.child_by_field_name(field)


def _find_child_by_type(node: ts.Node | None, type_name: str) -> ts.Node | None:
    """Find the first direct child with the given node type."""
    if node is None:
        return None
    for child in node.children:
        if child.type == type_name:
            return child
    return None


def _is_member_fn_ptr(function_node: ts.Node) -> DispatchInfo | None:
    """Detect pattern: (this->*stateProcessor_[msgId])(msg).

    The function position of the call_expression is a parenthesized_expression
    containing a pointer_expression with the ->* operator.
    """
    if function_node.type != "parenthesized_expression":
        return None

    text = _node_text(function_node)
    if "->*" not in text:
        return None

    # Extract the array name from patterns like stateProcessor_[idx]
    array_name: str | None = None
    for child in function_node.children:
        child_text = _node_text(child)
        if "->*" in child_text:
            # Look for subscript_expression inside
            bracket_pos = child_text.find("[")
            if bracket_pos != -1:
                # Find the array identifier before the bracket
                arrow_star_pos = child_text.find("->*")
                if arrow_star_pos != -1:
                    segment = child_text[arrow_star_pos + 3 : bracket_pos].strip()
                    array_name = segment if segment else None
            break

    # Fallback: parse from the full text
    if array_name is None:
        arrow_star_pos = text.find("->*")
        if arrow_star_pos != -1:
            rest = text[arrow_star_pos + 3 :].strip()
            bracket_pos = rest.find("[")
            paren_pos = rest.find(")")
            end = bracket_pos if bracket_pos != -1 else paren_pos
            if end != -1:
                array_name = rest[:end].strip() or None

    return DispatchInfo(
        call_type=CallType.MEMBER_FN_PTR,
        callee_name=None,
        receiver_name="this",
        array_name=array_name,
        is_arrow=True,
    )


def _is_ipc_proxy(function_node: ts.Node) -> DispatchInfo | None:
    """Detect pattern: Remote()->SendRequest(...) or AsObject()->Method(...).

    The function position is a field_expression whose object is itself a
    call_expression with an identifier matching an IPC factory name.
    """
    if function_node.type != "field_expression":
        return None

    obj_node = _child_by_field(function_node, "argument")
    if obj_node is None:
        # tree-sitter cpp grammar uses "argument" for the object in field_expression
        # but some versions use the first named child
        obj_node = function_node.named_children[0] if function_node.named_children else None

    if obj_node is None or obj_node.type != "call_expression":
        return None

    # The inner call's function should be an identifier like Remote or AsObject
    inner_fn = _child_by_field(obj_node, "function")
    if inner_fn is None:
        return None

    inner_name = _node_text(inner_fn)
    # Strip any qualifier prefix (e.g., IPCObjectStub::AsObject)
    base_name = inner_name.rsplit("::", 1)[-1] if "::" in inner_name else inner_name

    if base_name not in _IPC_PROXY_FACTORIES:
        return None

    # The method being called is the field of the outer field_expression
    field_node = _child_by_field(function_node, "field")
    callee = _node_text(field_node) if field_node else None

    # Determine arrow vs dot
    operator_text = ""
    for child in function_node.children:
        if child.type in ("->", "."):
            operator_text = child.type
            break

    return DispatchInfo(
        call_type=CallType.IPC_PROXY,
        callee_name=callee,
        receiver_name=base_name,
        array_name=None,
        is_arrow=operator_text == "->",
    )


def _is_chained_direct(function_node: ts.Node) -> DispatchInfo | None:
    """Detect pattern: GetInstance().Method() or Cls::GetInstance().Method().

    The function position is a field_expression whose object is a call_expression
    with a function name containing typical factory/singleton patterns.
    """
    if function_node.type != "field_expression":
        return None

    obj_node = _child_by_field(function_node, "argument")
    if obj_node is None:
        obj_node = function_node.named_children[0] if function_node.named_children else None

    if obj_node is None or obj_node.type != "call_expression":
        return None

    inner_fn = _child_by_field(obj_node, "function")
    if inner_fn is None:
        return None

    inner_name = _node_text(inner_fn)

    # Already handled IPC proxies — skip those
    base_name = inner_name.rsplit("::", 1)[-1] if "::" in inner_name else inner_name
    if base_name in _IPC_PROXY_FACTORIES:
        return None

    # The method being called
    field_node = _child_by_field(function_node, "field")
    callee = _node_text(field_node) if field_node else None

    # Determine arrow vs dot
    operator_text = ""
    for child in function_node.children:
        if child.type in ("->", "."):
            operator_text = child.type
            break

    return DispatchInfo(
        call_type=CallType.DIRECT,
        callee_name=callee,
        receiver_name=inner_name,
        array_name=None,
        is_arrow=operator_text == "->",
    )


def _classify_field_expression(
    function_node: ts.Node,
    local_vars: dict[str, str],
) -> DispatchInfo:
    """Classify a call whose function is a field_expression (obj.Method or obj->Method).

    Determines VIRTUAL vs DIRECT based on the receiver variable's classification
    in local_vars and whether arrow dispatch is used.
    """
    obj_node = _child_by_field(function_node, "argument")
    if obj_node is None:
        obj_node = function_node.named_children[0] if function_node.named_children else None

    field_node = _child_by_field(function_node, "field")
    callee = _node_text(field_node) if field_node else None

    # Determine arrow vs dot
    is_arrow = False
    for child in function_node.children:
        if child.type == "->":
            is_arrow = True
            break

    receiver_name = _node_text(obj_node) if obj_node else None

    # Classify based on local variable info
    var_class = local_vars.get(receiver_name, "") if receiver_name else ""

    if var_class in ("weak_ptr_lock", "pointer", "shared_ptr"):
        return DispatchInfo(
            call_type=CallType.VIRTUAL,
            callee_name=callee,
            receiver_name=receiver_name,
            array_name=None,
            is_arrow=is_arrow,
        )

    # Arrow dispatch through a pointer is virtual by default
    if is_arrow:
        return DispatchInfo(
            call_type=CallType.VIRTUAL,
            callee_name=callee,
            receiver_name=receiver_name,
            array_name=None,
            is_arrow=True,
        )

    # Dot dispatch on a concrete object is direct
    return DispatchInfo(
        call_type=CallType.DIRECT,
        callee_name=callee,
        receiver_name=receiver_name,
        array_name=None,
        is_arrow=False,
    )


def _is_callback(
    function_node: ts.Node,
    local_vars: dict[str, str],
) -> DispatchInfo | None:
    """Detect callback invocation through std::function, FuncPtr, etc.

    A simple identifier call where the identifier is known to be a callback type.
    """
    if function_node.type != "identifier":
        return None

    name = _node_text(function_node)
    if not name:
        return None

    var_type = local_vars.get(name, "")
    if not var_type:
        return None

    # Check if the variable type matches any callback hint
    for hint in _CALLBACK_TYPE_HINTS:
        if hint in var_type:
            return DispatchInfo(
                call_type=CallType.CALLBACK,
                callee_name=name,
                receiver_name=None,
                array_name=None,
                is_arrow=False,
            )

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_call(
    call_node: ts.Node,
    local_vars: dict[str, str],
) -> DispatchInfo:
    """Classify a C++ call_expression AST node into a dispatch pattern.

    Args:
        call_node: A tree-sitter node of type ``call_expression``.
        local_vars: Mapping of variable names to their source classification.
            Known values: "weak_ptr_lock", "pointer", "shared_ptr",
            or a type string like "std::function<void()>".

    Returns:
        A DispatchInfo describing the dispatch pattern.
    """
    if call_node is None:
        return DispatchInfo(
            call_type=CallType.DIRECT,
            callee_name=None,
            receiver_name=None,
            array_name=None,
            is_arrow=False,
        )

    function_node = _child_by_field(call_node, "function")
    if function_node is None:
        # Fallback: try first named child
        function_node = (
            call_node.named_children[0] if call_node.named_children else None
        )

    if function_node is None:
        return DispatchInfo(
            call_type=CallType.DIRECT,
            callee_name=None,
            receiver_name=None,
            array_name=None,
            is_arrow=False,
        )

    # 1. Member function pointer: (this->*array[idx])(args)
    result = _is_member_fn_ptr(function_node)
    if result is not None:
        return result

    # For field_expression based patterns, check IPC proxy and chained first
    if function_node.type == "field_expression":
        # 2. IPC proxy: Remote()->SendRequest(...)
        result = _is_ipc_proxy(function_node)
        if result is not None:
            return result

        # 3. Chained direct: GetInstance().Method()
        result = _is_chained_direct(function_node)
        if result is not None:
            return result

        # 4. Virtual or direct field dispatch: obj->Method() / obj.Method()
        return _classify_field_expression(function_node, local_vars)

    # 5. Callback: identifier that is a known callback type
    result = _is_callback(function_node, local_vars)
    if result is not None:
        return result

    # 6. Direct call: identifier(args) or qualified_identifier(args)
    callee_name = _node_text(function_node)

    return DispatchInfo(
        call_type=CallType.DIRECT,
        callee_name=callee_name if callee_name else None,
        receiver_name=None,
        array_name=None,
        is_arrow=False,
    )

"""Build a class hierarchy index from C++ source files using tree-sitter.

Enables:
1. Virtual dispatch candidate resolution (find all classes implementing an
   interface method).
2. Member function pointer array candidate resolution (find all
   &ClassName::Method targets in array initializations).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import tree_sitter as ts
import tree_sitter_cpp as tscpp

_LANGUAGE = ts.Language(tscpp.language())
_PARSER = ts.Parser(_LANGUAGE)


@dataclass(frozen=True)
class ClassInfo:
    """Metadata about a single class or struct definition."""

    name: str
    file_path: str
    base_classes: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemberFnPtrArray:
    """A collection of member function pointer targets found in an array or map."""

    array_name: str
    class_name: str
    targets: list[str] = field(default_factory=list)


class ClassHierarchyIndex:
    """Aggregated index of class hierarchies and function pointer arrays.

    Supports querying virtual dispatch candidates and member function pointer
    array targets across an entire codebase.
    """

    def __init__(self) -> None:
        self._classes: dict[str, ClassInfo] = {}
        self._fn_ptr_arrays: dict[str, MemberFnPtrArray] = {}
        self._implementors: dict[str, list[str]] = {}

    def add_class(self, info: ClassInfo) -> None:
        """Register a class and update the implementors mapping."""
        self._classes[info.name] = info
        for base in info.base_classes:
            self._implementors.setdefault(base, []).append(info.name)

    def add_fn_ptr_array(self, array: MemberFnPtrArray) -> None:
        """Register a member function pointer array."""
        self._fn_ptr_arrays[array.array_name] = array

    def get_virtual_candidates(
        self, method_name: str, receiver_type: str | None = None
    ) -> list[str]:
        """Find all classes that could be the target of a virtual dispatch.

        If *receiver_type* is given, returns classes derived from that type
        (including itself) that define *method_name*. Otherwise searches all
        classes that define the method.

        Returns:
            List of qualified names like "ClassName::method_name".
        """
        candidates: list[str] = []

        if receiver_type:
            # Collect the receiver itself plus all transitive derived classes
            to_check = self._collect_derived(receiver_type)
            to_check.add(receiver_type)
            for cls_name in to_check:
                info = self._classes.get(cls_name)
                if info and method_name in info.methods:
                    candidates.append(f"{cls_name}::{method_name}")
        else:
            # No receiver hint — return all classes defining this method
            for cls_name, info in self._classes.items():
                if method_name in info.methods:
                    candidates.append(f"{cls_name}::{method_name}")

        return candidates

    def get_fn_ptr_array_candidates(self, array_name: str) -> list[str]:
        """Return the list of qualified function targets for a given array.

        Returns:
            List of qualified names like "CastSessionImpl::ProcessSetUp".
        """
        array = self._fn_ptr_arrays.get(array_name)
        if array is None:
            return []
        return list(array.targets)

    def _collect_derived(self, base_name: str) -> set[str]:
        """Transitively collect all classes derived from *base_name*."""
        result: set[str] = set()
        queue = list(self._implementors.get(base_name, []))
        while queue:
            cls = queue.pop()
            if cls in result:
                continue
            result.add(cls)
            queue.extend(self._implementors.get(cls, []))
        return result


# ---------------------------------------------------------------------------
# AST extraction helpers
# ---------------------------------------------------------------------------


def build_class_hierarchy(
    source: bytes, file_path: str
) -> tuple[list[ClassInfo], list[MemberFnPtrArray]]:
    """Parse C++ source and extract class hierarchy info and fn-ptr arrays.

    Args:
        source: Raw source bytes of a C++ file.
        file_path: Path to the source file (stored in ClassInfo).

    Returns:
        Tuple of (class infos found, member function pointer arrays found).
    """
    tree = _PARSER.parse(source)
    classes: list[ClassInfo] = []
    fn_ptr_arrays: list[MemberFnPtrArray] = []

    _walk_for_classes(tree.root_node, file_path, classes)
    _walk_for_fn_ptr_arrays(tree.root_node, fn_ptr_arrays)

    return classes, fn_ptr_arrays


# ---------------------------------------------------------------------------
# Class / struct extraction
# ---------------------------------------------------------------------------


def _walk_for_classes(
    node: ts.Node, file_path: str, out: list[ClassInfo]
) -> None:
    """Recursively find class_specifier and struct_specifier nodes."""
    if node.type in ("class_specifier", "struct_specifier"):
        info = _extract_class_info(node, file_path)
        if info is not None:
            out.append(info)
        # Don't recurse into nested classes here — handle them below
        for child in node.children:
            if child.type == "field_declaration_list":
                _walk_for_nested_classes(child, file_path, out)
        return

    for child in node.children:
        _walk_for_classes(child, file_path, out)


def _walk_for_nested_classes(
    node: ts.Node, file_path: str, out: list[ClassInfo]
) -> None:
    """Find nested class/struct definitions inside a class body."""
    for child in node.children:
        if child.type in ("class_specifier", "struct_specifier"):
            info = _extract_class_info(child, file_path)
            if info is not None:
                out.append(info)
            # Recurse for deeply nested classes
            for grandchild in child.children:
                if grandchild.type == "field_declaration_list":
                    _walk_for_nested_classes(grandchild, file_path, out)
        elif child.type == "field_declaration_list":
            _walk_for_nested_classes(child, file_path, out)


def _extract_class_info(node: ts.Node, file_path: str) -> ClassInfo | None:
    """Extract ClassInfo from a class_specifier or struct_specifier node.

    Returns None for forward declarations (no body).
    """
    name: str | None = None
    base_classes: list[str] = []
    methods: list[str] = []
    body: ts.Node | None = None

    for child in node.children:
        if child.type == "type_identifier":
            name = child.text.decode()
        elif child.type == "base_class_clause":
            base_classes = _extract_base_classes(child)
        elif child.type == "field_declaration_list":
            body = child

    # Forward declaration or anonymous struct — skip
    if name is None or body is None:
        return None

    methods = _extract_methods_from_body(body)

    return ClassInfo(
        name=name,
        file_path=file_path,
        base_classes=base_classes,
        methods=methods,
    )


def _extract_base_classes(base_clause: ts.Node) -> list[str]:
    """Extract base class names from a base_class_clause node."""
    bases: list[str] = []
    for child in base_clause.children:
        if child.type == "type_identifier":
            bases.append(child.text.decode())
        elif child.type == "qualified_identifier":
            # e.g., ns::BaseClass — use the full qualified name
            bases.append(child.text.decode())
        # Recurse into access specifier wrappers if present
        elif child.type == "base_class_clause":
            bases.extend(_extract_base_classes(child))
    return bases


def _extract_methods_from_body(body: ts.Node) -> list[str]:
    """Extract method names from a class body (field_declaration_list).

    Looks for:
    - function_definition nodes (inline method definitions)
    - field_declaration nodes that contain function_declarator (declarations)
    """
    methods: list[str] = []
    for child in body.children:
        if child.type == "function_definition":
            name = _method_name_from_function_def(child)
            if name:
                methods.append(name)
        elif child.type == "declaration":
            name = _method_name_from_declaration(child)
            if name:
                methods.append(name)
        elif child.type == "field_declaration":
            name = _method_name_from_declaration(child)
            if name:
                methods.append(name)
        elif child.type in ("access_specifier", "comment", "{", "}", ";"):
            continue
        # template_declaration can wrap a function_definition
        elif child.type == "template_declaration":
            for sub in child.children:
                if sub.type == "function_definition":
                    name = _method_name_from_function_def(sub)
                    if name:
                        methods.append(name)
    return methods


def _method_name_from_function_def(node: ts.Node) -> str | None:
    """Extract method name from a function_definition node."""
    declarator = node.child_by_field_name("declarator")
    if declarator is None:
        return None
    return _name_from_declarator(declarator)


def _method_name_from_declaration(node: ts.Node) -> str | None:
    """Extract method name from a field_declaration or declaration node.

    Only returns a name if the declaration contains a function_declarator,
    indicating it is a method declaration (not a data member).
    """
    declarator = node.child_by_field_name("declarator")
    if declarator is None:
        # Try searching children for a function_declarator
        for child in node.children:
            if child.type == "function_declarator":
                return _name_from_declarator(child)
            # init_declarator wraps the actual declarator
            if child.type == "init_declarator":
                inner = child.child_by_field_name("declarator")
                if inner and inner.type == "function_declarator":
                    return _name_from_declarator(inner)
        return None

    if declarator.type == "function_declarator":
        return _name_from_declarator(declarator)

    # Could be wrapped in reference/pointer declarator
    if declarator.type in ("reference_declarator", "pointer_declarator"):
        for child in declarator.children:
            if child.type == "function_declarator":
                return _name_from_declarator(child)

    return None


def _name_from_declarator(declarator: ts.Node) -> str | None:
    """Extract the simple function name from a declarator node."""
    if declarator.type == "function_declarator":
        for child in declarator.children:
            if child.type == "identifier":
                return child.text.decode()
            if child.type == "field_identifier":
                return child.text.decode()
            if child.type == "qualified_identifier":
                parts = child.text.decode().split("::")
                return parts[-1]
            if child.type == "destructor_name":
                return child.text.decode()
        return None
    if declarator.type in ("identifier", "field_identifier"):
        return declarator.text.decode()
    if declarator.type == "qualified_identifier":
        parts = declarator.text.decode().split("::")
        return parts[-1]
    return None


# ---------------------------------------------------------------------------
# Member function pointer array extraction
# ---------------------------------------------------------------------------


def _walk_for_fn_ptr_arrays(
    node: ts.Node, out: list[MemberFnPtrArray]
) -> None:
    """Walk the AST looking for member function pointer arrays and map assignments."""
    # Strategy 1: Array/variable declarations with initializer lists
    if node.type in ("declaration", "field_declaration"):
        result = _try_extract_fn_ptr_array_from_decl(node)
        if result is not None:
            out.append(result)

    # Strategy 2: Map-style assignments inside function bodies
    # e.g., map_["KEY"] = &ClassName::Method
    if node.type == "function_definition":
        _extract_map_assignments(node, out)

    for child in node.children:
        _walk_for_fn_ptr_arrays(child, out)


def _try_extract_fn_ptr_array_from_decl(
    node: ts.Node,
) -> MemberFnPtrArray | None:
    """Try to extract a MemberFnPtrArray from a declaration node.

    Looks for patterns like:
        std::array<StateProcessor, N> stateProcessor_{
            nullptr,
            &CastSessionImpl::ProcessSetUp,
            ...
        };
    """
    array_name = _get_declarator_name(node)
    if array_name is None:
        return None

    # Find the initializer list
    init_list = _find_child_recursive(node, "initializer_list")
    if init_list is None:
        return None

    targets = _extract_pointer_targets(init_list)
    if not targets:
        return None

    # Determine the common class name from targets
    class_name = _infer_class_name(targets)

    return MemberFnPtrArray(
        array_name=array_name,
        class_name=class_name,
        targets=targets,
    )


def _extract_map_assignments(
    func_node: ts.Node, out: list[MemberFnPtrArray]
) -> None:
    """Extract map-style fn-ptr assignments from a function body.

    Pattern: mapName_["key"] = &ClassName::Method;

    Groups assignments by map name into a single MemberFnPtrArray.
    """
    body = func_node.child_by_field_name("body")
    if body is None:
        return

    # Collect all assignment targets grouped by map name
    map_targets: dict[str, list[str]] = {}
    _walk_for_map_assigns(body, map_targets)

    for map_name, targets in map_targets.items():
        if targets:
            class_name = _infer_class_name(targets)
            out.append(
                MemberFnPtrArray(
                    array_name=map_name,
                    class_name=class_name,
                    targets=targets,
                )
            )


def _walk_for_map_assigns(
    node: ts.Node, map_targets: dict[str, list[str]]
) -> None:
    """Recursively find assignment expressions targeting map entries."""
    if node.type == "expression_statement":
        child = _first_named_child(node)
        if child and child.type == "assignment_expression":
            _try_extract_map_assign(child, map_targets)

    for child in node.children:
        _walk_for_map_assigns(child, map_targets)


def _try_extract_map_assign(
    assign_node: ts.Node, map_targets: dict[str, list[str]]
) -> None:
    """Check if an assignment is a map fn-ptr assignment.

    Pattern: left = subscript_expression (map_[key])
             right = pointer_expression (&Class::Method)
    """
    left = assign_node.child_by_field_name("left")
    right = assign_node.child_by_field_name("right")

    if left is None or right is None:
        return

    # Left side should be a subscript expression: mapName_[...]
    if left.type != "subscript_expression":
        return

    map_name = _get_subscript_base_name(left)
    if map_name is None:
        return

    # Right side should be a pointer expression: &ClassName::Method
    target = _extract_single_pointer_target(right)
    if target is None:
        return

    map_targets.setdefault(map_name, []).append(target)


# ---------------------------------------------------------------------------
# Low-level utility helpers
# ---------------------------------------------------------------------------


def _get_declarator_name(node: ts.Node) -> str | None:
    """Get the variable/field name from a declaration node."""
    declarator = node.child_by_field_name("declarator")
    if declarator is None:
        # Search children for init_declarator or similar
        for child in node.children:
            if child.type == "init_declarator":
                declarator = child.child_by_field_name("declarator")
                break
    if declarator is None:
        return None
    return _plain_name(declarator)


def _plain_name(node: ts.Node) -> str | None:
    """Extract a plain identifier name from various declarator node types."""
    if node.type in ("identifier", "field_identifier"):
        return node.text.decode()
    # Array declarator: name[N]
    if node.type == "array_declarator":
        for child in node.children:
            if child.type in ("identifier", "field_identifier"):
                return child.text.decode()
    # init_declarator wraps the real declarator
    if node.type == "init_declarator":
        inner = node.child_by_field_name("declarator")
        if inner:
            return _plain_name(inner)
    # Qualified name
    if node.type == "qualified_identifier":
        parts = node.text.decode().split("::")
        return parts[-1]
    # Recurse into first identifier child as fallback
    for child in node.children:
        if child.type in ("identifier", "field_identifier"):
            return child.text.decode()
    return None


def _find_child_recursive(node: ts.Node, target_type: str) -> ts.Node | None:
    """Find the first descendant node of the given type (BFS)."""
    queue = list(node.children)
    while queue:
        current = queue.pop(0)
        if current.type == target_type:
            return current
        queue.extend(current.children)
    return None


def _extract_pointer_targets(init_list: ts.Node) -> list[str]:
    """Extract all &ClassName::Method targets from an initializer_list."""
    targets: list[str] = []
    for child in init_list.children:
        target = _extract_single_pointer_target(child)
        if target:
            targets.append(target)
        # Recurse into nested initializer lists
        if child.type == "initializer_list":
            targets.extend(_extract_pointer_targets(child))
    return targets


def _extract_single_pointer_target(node: ts.Node) -> str | None:
    """Extract a qualified name from a pointer_expression like &Class::Method.

    Also handles unary_expression with & operator in some tree-sitter versions.
    """
    if node.type == "pointer_expression":
        # Children: & and the operand (qualified_identifier or field_expression)
        for child in node.children:
            if child.type == "qualified_identifier":
                return child.text.decode()
            if child.type == "field_expression":
                return child.text.decode()
        return None

    if node.type == "unary_expression":
        # Some tree-sitter versions parse &X::Y as unary_expression
        op = node.child_by_field_name("operator")
        operand = node.child_by_field_name("argument")
        if op and op.text == b"&" and operand:
            if operand.type == "qualified_identifier":
                return operand.text.decode()
        return None

    return None


def _get_subscript_base_name(subscript_node: ts.Node) -> str | None:
    """Get the base name from a subscript_expression like mapName_[key]."""
    # The first child is typically the base expression
    for child in subscript_node.children:
        if child.type in ("identifier", "field_identifier"):
            return child.text.decode()
        if child.type == "field_expression":
            # this->mapName_ — get the field
            for sub in child.children:
                if sub.type == "field_identifier":
                    return sub.text.decode()
    return None


def _first_named_child(node: ts.Node) -> ts.Node | None:
    """Return the first named (non-anonymous) child of a node."""
    for child in node.children:
        if child.is_named:
            return child
    return None


def _infer_class_name(targets: list[str]) -> str:
    """Infer the common class name from a list of qualified targets.

    If all targets share the same class prefix (e.g., "CastSessionImpl::X"),
    returns that class name. Otherwise returns an empty string.
    """
    if not targets:
        return ""

    classes: set[str] = set()
    for target in targets:
        parts = target.split("::")
        if len(parts) >= 2:
            classes.add(parts[-2])

    if len(classes) == 1:
        return classes.pop()
    return ""


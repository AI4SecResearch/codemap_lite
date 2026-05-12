"""Data types for the parsing module."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ScannedFile:
    """Represents a scanned source file with its hash and language."""

    file_path: str
    hash: str
    primary_language: str


@dataclass(frozen=True)
class FileChanges:
    """Represents changes detected between two scans."""

    added: list[str]
    modified: list[str]
    deleted: list[str]


class SymbolKind(enum.Enum):
    """Kind of symbol extracted from source code."""

    FUNCTION = "function"
    CLASS = "class"
    VARIABLE = "variable"


class CallType(enum.Enum):
    """Type of function call."""

    DIRECT = "direct"
    INDIRECT = "indirect"
    VIRTUAL = "virtual"
    CALLBACK = "callback"
    MEMBER_FN_PTR = "member_fn_ptr"
    IPC_PROXY = "ipc_proxy"


@dataclass(frozen=True)
class FunctionDef:
    """A parsed function definition."""

    name: str
    signature: str
    file_path: Path
    start_line: int
    end_line: int
    body_hash: str


@dataclass(frozen=True)
class Symbol:
    """A symbol extracted from source code."""

    name: str
    kind: SymbolKind
    file_path: Path
    line: int


@dataclass(frozen=True)
class CallEdge:
    """A resolved call edge between two functions."""

    caller_name: str
    callee_name: str
    call_file: Path
    call_line: int
    call_type: CallType
    resolved_by: str


@dataclass(frozen=True)
class UnresolvedCall:
    """A call that could not be fully resolved."""

    caller_name: str
    call_expression: str
    call_file: Path
    call_line: int
    call_type: CallType
    var_name: str
    var_type: str
    candidates: list[str] = field(default_factory=list)
    source_code_snippet: str = ""

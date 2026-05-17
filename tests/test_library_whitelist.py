"""Tests for library function whitelist filter.

Verifies that known stdlib/system calls are filtered from UnresolvedCalls
(architecture.md §1, CLAUDE.md gap: ~30% false positive reduction).
"""
from __future__ import annotations

import pytest

from codemap_lite.parsing.cpp.library_whitelist import is_library_call, LIBRARY_WHITELIST


class TestLibraryWhitelist:
    """Whitelist correctly identifies library/system calls."""

    def test_stl_methods(self) -> None:
        assert is_library_call("c_str")
        assert is_library_call("push_back")
        assert is_library_call("size")
        assert is_library_call("begin")
        assert is_library_call("empty")

    def test_qualified_stl_methods(self) -> None:
        assert is_library_call("std::vector::push_back")
        assert is_library_call("std::string::c_str")
        assert is_library_call("OHOS::String::size")

    def test_smart_pointer_methods(self) -> None:
        assert is_library_call("promote")
        assert is_library_call("lock")
        assert is_library_call("get")
        assert is_library_call("reset")

    def test_log_macros(self) -> None:
        assert is_library_call("CLOGD")
        assert is_library_call("CLOGE")
        assert is_library_call("HILOGI")
        assert is_library_call("MEDIA_LOGD")

    def test_infra_macros(self) -> None:
        assert is_library_call("RETRUEN_IF_WRONG_TASK")
        assert is_library_call("EXECUTE_SINGLE_STUB_TASK")

    def test_c_stdlib(self) -> None:
        assert is_library_call("memcpy")
        assert is_library_call("strlen")
        assert is_library_call("printf")
        assert is_library_call("malloc")

    def test_sync_primitives(self) -> None:
        assert is_library_call("lock")
        assert is_library_call("notify_one")
        assert is_library_call("wait")
        assert is_library_call("join")

    def test_non_library_calls(self) -> None:
        """User-defined functions should NOT be filtered."""
        assert not is_library_call("OnRemoteRequest")
        assert not is_library_call("HandlePlay")
        assert not is_library_call("DispatchMessage")
        assert not is_library_call("SendEvent")
        assert not is_library_call("ValidateSession")
        assert not is_library_call("CreateMirrorPlayer")

    def test_qualified_non_library(self) -> None:
        assert not is_library_call("OHOS::CastEngine::OnRemoteRequest")
        assert not is_library_call("MirrorPlayer::HandlePlay")

    def test_whitelist_is_frozen(self) -> None:
        """Whitelist should be immutable."""
        assert isinstance(LIBRARY_WHITELIST, frozenset)
        assert len(LIBRARY_WHITELIST) > 50

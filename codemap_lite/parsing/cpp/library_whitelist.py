"""Library function whitelist — filters false-positive UnresolvedCalls.

architecture.md §1 Parsing layer: known standard library functions, system
macros, and smart pointer methods should NOT generate UnresolvedCall nodes.
These are guaranteed to resolve to well-known implementations that don't
need LLM repair.

Gap identified 2026-05-17: ~30% of CastEngine GAPs (118/395 in sample)
are false positives from stdlib/macros. This whitelist eliminates them
at parse time.
"""
from __future__ import annotations

# C++ STL container/string methods
_STL_METHODS: set[str] = {
    "c_str", "data", "size", "length", "empty", "clear",
    "begin", "end", "cbegin", "cend", "rbegin", "rend",
    "front", "back", "at", "find", "rfind",
    "push_back", "pop_back", "push_front", "pop_front",
    "insert", "erase", "emplace", "emplace_back", "emplace_front",
    "reserve", "resize", "capacity", "shrink_to_fit",
    "count", "contains", "lower_bound", "upper_bound",
    "substr", "append", "replace", "compare",
    "swap", "assign", "merge", "splice",
    "top", "pop", "push",
    "first", "second",
    "str", "good", "bad", "fail", "eof",
    "write", "read", "flush", "close", "open",
    "get", "set", "reset", "release",
}

# Smart pointer methods
_SMART_PTR_METHODS: set[str] = {
    "promote", "lock", "get", "reset", "release",
    "use_count", "unique", "expired",
    "make_shared", "make_unique",
}

# OHOS / HiLog / system logging macros
_LOG_MACROS: set[str] = {
    "CLOGD", "CLOGE", "CLOGI", "CLOGW", "CLOGF",
    "HILOGI", "HILOGE", "HILOGD", "HILOGW", "HILOGF",
    "MEDIA_LOGD", "MEDIA_LOGE", "MEDIA_LOGI", "MEDIA_LOGW",
    "LOG_DEBUG", "LOG_INFO", "LOG_WARN", "LOG_ERROR", "LOG_FATAL",
    "SLOGI", "SLOGE", "SLOGD", "SLOGW",
    "DHLOGI", "DHLOGE", "DHLOGD", "DHLOGW",
    "SHARING_LOGD", "SHARING_LOGE", "SHARING_LOGI",
}

# OHOS infrastructure macros
_INFRA_MACROS: set[str] = {
    "RETRUEN_IF_WRONG_TASK", "RETURN_IF_WRONG_TASK",
    "EXECUTE_SINGLE_STUB_TASK",
    "CHECK_AND_RETURN_RET_LOG", "CHECK_AND_RETURN_LOG",
    "LISTENER_FUNC_CHECK", "POINTER_MASK",
}

# C standard library / POSIX
_C_STDLIB: set[str] = {
    "malloc", "calloc", "realloc", "free",
    "memcpy", "memmove", "memset", "memcmp",
    "strlen", "strcpy", "strncpy", "strcat", "strncat", "strcmp", "strncmp",
    "printf", "sprintf", "snprintf", "fprintf", "vprintf", "vsnprintf",
    "scanf", "sscanf", "fscanf",
    "fopen", "fclose", "fread", "fwrite", "fseek", "ftell",
    "atoi", "atol", "atof", "strtol", "strtoul", "strtod",
    "abs", "labs", "div", "ldiv",
    "assert", "static_assert",
    "errno",
}

# Common C++ operators and casts that appear as call expressions
_OPERATORS_AND_CASTS: set[str] = {
    "static_cast", "dynamic_cast", "reinterpret_cast", "const_cast",
    "move", "forward", "swap",
    "make_pair", "make_tuple", "tie",
    "to_string", "stoi", "stol", "stof", "stod",
}

# Thread / sync primitives
_SYNC_PRIMITIVES: set[str] = {
    "lock", "unlock", "try_lock",
    "lock_guard", "unique_lock", "shared_lock",
    "notify_one", "notify_all", "wait", "wait_for", "wait_until",
    "join", "detach", "joinable",
    "sleep_for", "sleep_until", "yield",
}

# Aggregate all into a single lookup set
LIBRARY_WHITELIST: frozenset[str] = frozenset(
    _STL_METHODS
    | _SMART_PTR_METHODS
    | _LOG_MACROS
    | _INFRA_MACROS
    | _C_STDLIB
    | _OPERATORS_AND_CASTS
    | _SYNC_PRIMITIVES
)


def is_library_call(call_expression: str) -> bool:
    """Return True if the call expression is a known library/system call.

    Checks both the full expression and the bare name (last :: segment).
    """
    if call_expression in LIBRARY_WHITELIST:
        return True
    # Check bare name (e.g., "std::vector::push_back" -> "push_back")
    if "::" in call_expression:
        bare = call_expression.rsplit("::", 1)[-1]
        if bare in LIBRARY_WHITELIST:
            return True
    return False

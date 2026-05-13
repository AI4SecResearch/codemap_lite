"""Tests for codewiki_lite REST API client."""
import json
from unittest.mock import AsyncMock, patch

import pytest

from codemap_lite.analysis.source_point_client import (
    SourcePointClient,
    SourcePointInfo,
)


@pytest.fixture
def mock_source_points():
    return [
        {
            "function_id": "func_001",
            "entry_point_kind": "rest_api",
            "reason": "HTTP handler for /api/users",
            "module": "user_service",
        },
        {
            "function_id": "func_002",
            "entry_point_kind": "ipc_handler",
            "reason": "IPC message receiver",
            "module": "cast_framework",
        },
    ]


def test_parse_source_points(mock_source_points):
    client = SourcePointClient(base_url="http://localhost:8000")
    results = client._parse_response(mock_source_points)
    assert len(results) == 2
    assert results[0].function_id == "func_001"
    assert results[0].entry_point_kind == "rest_api"
    assert results[0].module == "user_service"


def test_source_point_info_dataclass():
    info = SourcePointInfo(
        function_id="f1",
        entry_point_kind="rest_api",
        reason="test",
        module="mod",
    )
    assert info.function_id == "f1"
    assert info.entry_point_kind == "rest_api"


def test_client_from_json_file(tmp_path):
    """Test loading source points from a local JSON file (mock mode)."""
    data = [
        {
            "function_id": "func_003",
            "entry_point_kind": "callback",
            "reason": "Timer callback",
            "module": "scheduler",
        }
    ]
    json_file = tmp_path / "sources.json"
    json_file.write_text(json.dumps(data))

    client = SourcePointClient(base_url="http://localhost:8000")
    results = client.load_from_file(json_file)
    assert len(results) == 1
    assert results[0].function_id == "func_003"


def test_parse_response_rejects_non_list():
    """architecture.md §3: if codewiki_lite API returns a dict (error payload)
    instead of a list, _parse_response must raise TypeError, not silently
    iterate over dict keys."""
    client = SourcePointClient(base_url="http://localhost:8000")
    with pytest.raises(TypeError):
        client._parse_response({"error": "Internal server error"})


def test_parse_response_rejects_missing_function_id():
    """architecture.md §3: function_id is required. If missing, _parse_response
    must raise KeyError rather than producing a SourcePointInfo with None."""
    client = SourcePointClient(base_url="http://localhost:8000")
    with pytest.raises(KeyError):
        client._parse_response([{"entry_point_kind": "api"}])


def test_parse_response_defaults_optional_fields():
    """reason and module are optional — should default to empty string."""
    client = SourcePointClient(base_url="http://localhost:8000")
    results = client._parse_response([
        {"function_id": "f1", "entry_point_kind": "callback"}
    ])
    assert results[0].reason == ""
    assert results[0].module == ""

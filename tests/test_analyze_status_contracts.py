"""Analyze status + progress file reading — architecture.md §3/§8.

Tests the /analyze/status endpoint's progress file aggregation,
progress.json schema validation, and the source progress reader.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codemap_lite.api.app import create_app
from codemap_lite.api.routes.analyze import _read_source_progress
from codemap_lite.graph.neo4j_store import InMemoryGraphStore


@pytest.fixture
def store():
    return InMemoryGraphStore()


@pytest.fixture
def client(store):
    app = create_app(store=store)
    return TestClient(app)


# ---------------------------------------------------------------------------
# §3: _read_source_progress
# ---------------------------------------------------------------------------


class TestReadSourceProgress:
    """architecture.md §3 + ADR #52: progress file reader."""

    def test_empty_dir_returns_empty(self, tmp_path):
        assert _read_source_progress(tmp_path) == []

    def test_no_repair_dir_returns_empty(self, tmp_path):
        assert _read_source_progress(tmp_path) == []

    def test_reads_valid_progress_file(self, tmp_path):
        repair_dir = tmp_path / "logs" / "repair" / "src_001"
        repair_dir.mkdir(parents=True)
        (repair_dir / "progress.json").write_text(json.dumps({
            "source_id": "src_001",
            "gaps_fixed": 3,
            "gaps_total": 10,
            "current_gap": "gap_002",
            "attempt": 2,
            "max_attempts": 3,
            "gate_result": "failed",
            "edges_written": 5,
            "state": "running",
            "last_error": "gate_failed: remaining pending GAPs",
        }))
        rows = _read_source_progress(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["source_id"] == "src_001"
        assert row["gaps_fixed"] == 3
        assert row["gaps_total"] == 10
        assert row["current_gap"] == "gap_002"
        assert row["attempt"] == 2
        assert row["gate_result"] == "failed"
        assert row["state"] == "running"

    def test_skips_corrupted_json(self, tmp_path):
        repair_dir = tmp_path / "logs" / "repair" / "bad"
        repair_dir.mkdir(parents=True)
        (repair_dir / "progress.json").write_text("{bad json")
        assert _read_source_progress(tmp_path) == []

    def test_skips_malformed_numeric_fields(self, tmp_path):
        """architecture.md §3: graceful degradation on bad data."""
        repair_dir = tmp_path / "logs" / "repair" / "bad_nums"
        repair_dir.mkdir(parents=True)
        (repair_dir / "progress.json").write_text(json.dumps({
            "source_id": "bad",
            "gaps_fixed": "not_a_number",
            "gaps_total": None,
        }))
        rows = _read_source_progress(tmp_path)
        # Should skip this file gracefully
        assert len(rows) == 0

    def test_multiple_sources_sorted(self, tmp_path):
        for name in ["src_b", "src_a", "src_c"]:
            d = tmp_path / "logs" / "repair" / name
            d.mkdir(parents=True)
            (d / "progress.json").write_text(json.dumps({
                "source_id": name,
                "gaps_fixed": 1,
                "gaps_total": 5,
            }))
        rows = _read_source_progress(tmp_path)
        assert len(rows) == 3
        # Sorted by directory name
        assert rows[0]["source_id"] == "src_a"
        assert rows[1]["source_id"] == "src_b"
        assert rows[2]["source_id"] == "src_c"

    def test_none_target_dir_returns_empty(self):
        assert _read_source_progress(None) == []

    def test_uses_dirname_as_fallback_source_id(self, tmp_path):
        """If source_id not in JSON, use directory name."""
        repair_dir = tmp_path / "logs" / "repair" / "fallback_name"
        repair_dir.mkdir(parents=True)
        (repair_dir / "progress.json").write_text(json.dumps({
            "gaps_fixed": 2,
            "gaps_total": 4,
        }))
        rows = _read_source_progress(tmp_path)
        assert rows[0]["source_id"] == "fallback_name"


# ---------------------------------------------------------------------------
# §8: /analyze/status endpoint
# ---------------------------------------------------------------------------


class TestAnalyzeStatusEndpoint:
    """architecture.md §8: GET /analyze/status returns state + sources."""

    def test_initial_state_idle(self, client):
        resp = client.get("/api/v1/analyze/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "idle"
        assert data["sources"] == []

    def test_state_after_analyze_trigger(self, client):
        client.post("/api/v1/analyze", json={"mode": "full"})
        resp = client.get("/api/v1/analyze/status")
        data = resp.json()
        assert data["state"] == "running"

    def test_progress_derived_from_sources(self, client, store, tmp_path):
        """When sources have progress files, overall progress is computed."""
        # Wire target_dir to app state
        app = client.app
        app.state.target_dir = tmp_path

        # Create progress files
        for i, name in enumerate(["s1", "s2"]):
            d = tmp_path / "logs" / "repair" / name
            d.mkdir(parents=True)
            (d / "progress.json").write_text(json.dumps({
                "source_id": name,
                "gaps_fixed": 5 * (i + 1),
                "gaps_total": 10,
            }))

        resp = client.get("/api/v1/analyze/status")
        data = resp.json()
        assert len(data["sources"]) == 2
        # Progress = (5 + 10) / (10 + 10) = 0.75
        assert data["progress"] == 0.75


# ---------------------------------------------------------------------------
# §8: Analyze double-spawn protection
# ---------------------------------------------------------------------------


class TestAnalyzeDoubleSpawn:
    """architecture.md §8: 409 Conflict prevents concurrent analysis."""

    def test_analyze_while_running_409(self, client):
        client.post("/api/v1/analyze", json={"mode": "full"})
        resp = client.post("/api/v1/analyze", json={"mode": "incremental"})
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"].lower()

    def test_repair_while_repairing_409(self, client):
        client.post("/api/v1/analyze/repair")
        resp = client.post("/api/v1/analyze/repair")
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# §8: Feedback endpoint
# ---------------------------------------------------------------------------


class TestFeedbackEndpoint:
    """architecture.md §8: GET /feedback returns counter-examples."""

    def test_feedback_empty_initially(self, client):
        resp = client.get("/api/v1/feedback")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] == 0


# ---------------------------------------------------------------------------
# §8: Repair logs endpoint
# ---------------------------------------------------------------------------


class TestRepairLogsEndpoint:
    """architecture.md §8: GET /repair-logs returns repair history."""

    def test_repair_logs_empty_initially(self, client):
        resp = client.get("/api/v1/repair-logs")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "items" in data
        assert data["total"] == 0

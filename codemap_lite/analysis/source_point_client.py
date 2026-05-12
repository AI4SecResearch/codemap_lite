"""codewiki_lite REST API client — fetches source points."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourcePointInfo:
    """A source point returned from codewiki_lite API."""

    function_id: str
    entry_point_kind: str
    reason: str
    module: str


class SourcePointClient:
    """Client for codewiki_lite REST API to fetch source points."""

    def __init__(self, base_url: str = "http://localhost:8000") -> None:
        self._base_url = base_url.rstrip("/")

    def _parse_response(self, data: list[dict[str, Any]]) -> list[SourcePointInfo]:
        """Parse raw API response into SourcePointInfo objects."""
        results = []
        for item in data:
            results.append(SourcePointInfo(
                function_id=item["function_id"],
                entry_point_kind=item["entry_point_kind"],
                reason=item.get("reason", ""),
                module=item.get("module", ""),
            ))
        return results

    def load_from_file(self, path: Path) -> list[SourcePointInfo]:
        """Load source points from a local JSON file (mock/offline mode)."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return self._parse_response(data)

    async def fetch(self) -> list[SourcePointInfo]:
        """Fetch source points from the codewiki_lite REST API."""
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/api/v1/source-points")
            resp.raise_for_status()
            return self._parse_response(resp.json())

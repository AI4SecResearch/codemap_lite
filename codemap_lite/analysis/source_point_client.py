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
        if not isinstance(data, list):
            raise TypeError(
                f"Expected list of source points, got {type(data).__name__}"
            )
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

    async def fetch(self, modules: list[str] | None = None) -> list[SourcePointInfo]:
        """Fetch source points from the codewiki_lite REST API.

        Queries ``/modules`` to discover modules, then
        ``/modules/{name}/entries`` for each leaf module to collect
        entry-point functions.

        architecture.md §3 Source 点获取: timeout prevents indefinite hang
        when codewiki_lite is unreachable.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0, proxy=None) as client:
            # Discover modules
            resp = await client.get(f"{self._base_url}/modules")
            resp.raise_for_status()
            all_modules = resp.json()

            # Filter to requested modules, or use all leaf modules
            if modules:
                target_modules = [m for m in all_modules if m["name"] in modules]
            else:
                target_modules = [m for m in all_modules if m.get("is_leaf")]

            # Collect entries from each module
            results: list[SourcePointInfo] = []
            for mod in target_modules:
                mod_name = mod["name"]
                try:
                    entries_resp = await client.get(
                        f"{self._base_url}/modules/{mod_name}/entries"
                    )
                    entries_resp.raise_for_status()
                    entries = entries_resp.json()
                except Exception:
                    continue

                for entry in entries:
                    entry_id = entry.get("id")
                    if not entry_id:
                        continue
                    results.append(SourcePointInfo(
                        function_id=entry_id,
                        entry_point_kind=entry.get("classification") or "entry_point",
                        reason="archdoc-detected",
                        module=mod_name,
                    ))

            return results

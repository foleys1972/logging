"""Client for publishing telemetry to the control plane."""

from __future__ import annotations

import httpx
from typing import Any, Dict, Optional


class ControlPlaneClient:
    def __init__(self, base_url: str, api_token: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self._client = httpx.AsyncClient(timeout=10)

    async def send_snapshot(self, site_id: int, payload: Dict[str, Any]) -> None:
        url = f"{self.base_url}/api/v1/sites/{site_id}/snapshots"
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        response = await self._client.post(url, json=payload, headers=headers)
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()



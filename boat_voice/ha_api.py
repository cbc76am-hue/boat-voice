"""Async HTTP client for Home Assistant REST API."""
from __future__ import annotations

import fnmatch
import logging
from typing import Any

import aiohttp


LOGGER = logging.getLogger(__name__)


class HAClient:
    """Thin async wrapper over the Home Assistant REST API."""

    def __init__(self, url: str, token: str, session: aiohttp.ClientSession) -> None:
        self.url = url.rstrip("/")
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def ping(self) -> bool:
        """Return True if /api/ is reachable and the token is accepted."""
        try:
            async with self._session.get(
                f"{self.url}/api/", headers=self._headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                return resp.status == 200
        except Exception as err:
            LOGGER.warning("HA ping failed: %s", err)
            return False

    async def get_states(self) -> list[dict[str, Any]]:
        """Fetch all entity states."""
        async with self._session.get(
            f"{self.url}/api/states",
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        async with self._session.get(
            f"{self.url}/api/states/{entity_id}",
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            return await resp.json()

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Call a HA service and return the list of affected states."""
        async with self._session.post(
            f"{self.url}/api/services/{domain}/{service}",
            headers=self._headers,
            json=data,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()


def filter_entities(
    states: list[dict[str, Any]],
    include_patterns: list[str],
    exclude_patterns: list[str],
) -> list[dict[str, Any]]:
    """Filter HA states by entity_id glob patterns. If include_patterns is empty,
    all entities pass the include filter (then exclude is applied)."""
    out = []
    for st in states:
        eid = st.get("entity_id", "")
        if include_patterns and not any(
            fnmatch.fnmatchcase(eid, p) for p in include_patterns
        ):
            continue
        if any(fnmatch.fnmatchcase(eid, p) for p in exclude_patterns):
            continue
        out.append(st)
    return out

"""Async HTTP client for Signal K REST API.

Read operations target SK paths (navigation.position etc.) for the boat's
current state. Write operations target the resources API (routes, waypoints,
notes) — these flow through SK's built-in resources-provider plugin and out
via WS deltas to anything subscribed (e.g. the OpenCPN GPX bridge).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiohttp


LOGGER = logging.getLogger(__name__)


class SKClient:
    """Thin async wrapper over the Signal K REST API."""

    def __init__(self, url: str, token: str, session: aiohttp.ClientSession) -> None:
        self.url = url.rstrip("/")
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    @classmethod
    def from_token_file(
        cls, url: str, token_path: str | Path, session: aiohttp.ClientSession
    ) -> "SKClient":
        token = Path(token_path).read_text().strip()
        return cls(url=url, token=token, session=session)

    async def ping(self) -> bool:
        """True if SK responds 200 to a self read."""
        try:
            async with self._session.get(
                f"{self.url}/signalk/v1/api/vessels/self/uuid",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == 200
        except Exception as err:
            LOGGER.warning("SK ping failed: %s", err)
            return False

    async def get_path(self, path: str) -> Any | None:
        """Read a single SK path (dot-notation, e.g. 'environment.tide.heightNow').

        Returns the `value` field from the SK response, or None on miss /
        error. SK exposes scalar paths as `{value, meta, $source, timestamp}`
        and structured paths as nested objects — we always return whatever
        `value` is (or the whole body if no `value` key exists).
        """
        url_path = path.replace(".", "/")
        try:
            async with self._session.get(
                f"{self.url}/signalk/v1/api/vessels/self/{url_path}",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if isinstance(data, dict) and "value" in data:
                    return data["value"]
                return data
        except Exception as err:
            LOGGER.warning("SK get_path(%s) failed: %s", path, err)
            return None

    async def get_position(self) -> tuple[float, float] | None:
        """Return (latitude, longitude) in decimal degrees, or None if not available."""
        try:
            async with self._session.get(
                f"{self.url}/signalk/v1/api/vessels/self/navigation/position",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                v = data.get("value") or {}
                lat = v.get("latitude")
                lon = v.get("longitude")
                if lat is None or lon is None:
                    return None
                return float(lat), float(lon)
        except Exception as err:
            LOGGER.warning("SK get_position failed: %s", err)
            return None

    # -------- resources --------

    async def create_waypoint(
        self, name: str, lat: float, lon: float, description: str = ""
    ) -> str | None:
        """POST a waypoint, return its SK UUID or None on failure."""
        body = {
            "name": name,
            "description": description,
            "feature": {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {},
            },
        }
        return await self._post_resource("waypoints", body)

    async def create_route(
        self,
        name: str,
        coords: list[tuple[float, float]],
        description: str = "",
    ) -> str | None:
        """POST a route. coords is a list of (lat, lon) tuples in order."""
        if len(coords) < 2:
            LOGGER.warning("create_route: need at least 2 points, got %d", len(coords))
            return None
        body = {
            "name": name,
            "description": description,
            "distance": _approx_distance_m(coords),
            "feature": {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[lon, lat] for lat, lon in coords],
                },
                "properties": {},
            },
        }
        return await self._post_resource("routes", body)

    async def list_resources(self, kind: str) -> dict[str, Any]:
        """Return the dict of {uuid: resource} for the given kind (routes/waypoints/etc.)."""
        async with self._session.get(
            f"{self.url}/signalk/v2/api/resources/{kind}",
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def delete_resource(self, kind: str, uuid: str) -> bool:
        """DELETE a resource. Returns True on 200."""
        async with self._session.delete(
            f"{self.url}/signalk/v2/api/resources/{kind}/{uuid}",
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            return resp.status == 200

    async def _post_resource(self, kind: str, body: dict[str, Any]) -> str | None:
        async with self._session.post(
            f"{self.url}/signalk/v2/api/resources/{kind}",
            headers=self._headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 201:
                LOGGER.warning(
                    "SK POST %s failed: %d %s",
                    kind, resp.status, await resp.text(),
                )
                return None
            data = await resp.json()
            return data.get("id")


def _approx_distance_m(coords: list[tuple[float, float]]) -> float:
    """Sum of great-circle hops between consecutive (lat, lon) points, meters.
    Uses the equirectangular approximation — good enough for SK metadata."""
    import math
    R = 6371000.0
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(coords, coords[1:]):
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = phi2 - phi1
        dlam = math.radians(lon2 - lon1)
        x = dlam * math.cos((phi1 + phi2) / 2)
        total += R * math.hypot(x, dphi)
    return round(total, 1)

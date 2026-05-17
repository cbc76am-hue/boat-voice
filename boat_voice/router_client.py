"""Async HTTP client for the local tolly-router routing service.

Mirrors the shape of `sk_api.py`: thin async wrapper over a localhost REST
service. The router lives at `http://127.0.0.1:8090` by default; it accepts a
start + end coordinate and returns a chart-aware route.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp


LOGGER = logging.getLogger(__name__)


_ERROR_MAP = {
    "destination is not on water": "I can't end the route there — that's on land.",
    "start is not on water": "Our starting position isn't on water in the chart data — try a nearby waypoint.",
    "no navigable path found": "I couldn't find a safe water path to that destination.",
    "boat is outside routing coverage area": (
        "We're outside the router's coverage area (Puget Sound and the San Juans)."
    ),
    "destination outside routing coverage area": (
        "That destination is outside the router's coverage area (Puget Sound and the San Juans)."
    ),
    "route distance exceeds limit": "That route is longer than the router will plan in one shot.",
    "router service unreachable": (
        "I couldn't reach the routing service — it may be down."
    ),
}


def humanize_error(err: str) -> str:
    """Map a router error string to a Tolly-friendly version."""
    if not err:
        return "I couldn't plan that route — unknown error."
    key = err.strip().lower()
    return _ERROR_MAP.get(key, f"I couldn't plan that route: {err}.")


class RouterClient:
    """Thin async wrapper over the tolly-router HTTP API."""

    def __init__(
        self,
        url: str,
        session: aiohttp.ClientSession,
        timeout_s: float = 5.0,
    ) -> None:
        self.url = url.rstrip("/")
        self._session = session
        self._timeout_s = float(timeout_s)
        self._headers = {"Content-Type": "application/json"}

    async def ping(self) -> bool:
        """True if the routing service responds 200 on /health with a loaded graph."""
        try:
            async with self._session.get(
                f"{self.url}/health",
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                return bool(data.get("graph_loaded"))
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            LOGGER.warning("Router ping failed: %s", err)
            return False

    async def plan_route(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
    ) -> dict[str, Any]:
        """POST /route. Returns the parsed JSON body.

        On success the dict has `ok=True` plus `waypoints`, `distance_nm`,
        `hazards_near`, `warnings`. On a routing-level failure the service
        still returns HTTP 200 with `ok=False` and an `error` string. Network
        / unreachable failures are converted to `{"ok": False, "error": ...}`
        so callers never have to catch exceptions.
        """
        body = {
            "start": {"lat": float(start_lat), "lon": float(start_lon)},
            "end": {"lat": float(end_lat), "lon": float(end_lon)},
            # forward-compat params: accepted but ignored by router v1
            "optimize": "safe",
        }
        try:
            async with self._session.post(
                f"{self.url}/route",
                headers=self._headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=self._timeout_s + 5),
            ) as resp:
                # Router returns 200 even for ok=false (caller renders error).
                if resp.status != 200:
                    text = await resp.text()
                    LOGGER.warning(
                        "Router /route returned HTTP %d: %s", resp.status, text
                    )
                    return {
                        "ok": False,
                        "error": f"router returned HTTP {resp.status}",
                    }
                return await resp.json()
        except aiohttp.ClientConnectorError as err:
            LOGGER.warning("Router unreachable: %s", err)
            return {"ok": False, "error": "router service unreachable"}
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            LOGGER.warning("Router request failed: %s", err)
            return {"ok": False, "error": f"router request failed: {err}"}

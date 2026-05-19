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
        optimize: str = "safe",
        departure_time: str | None = None,
        depart_window: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /route. Returns the parsed JSON body.

        Args:
            start_lat / start_lon / end_lat / end_lon: endpoints.
            optimize: one of "safe" (v1 distance), "time" (v2 spatio-
                      temporal min-time), "fuel" (== time for fixed-STW
                      cruise), or "depart_window" (sweep candidate
                      departures and return the best).
            departure_time: ISO8601 (UTC, e.g. "2026-05-18T14:00:00Z"),
                            used by "time" and "fuel" modes.  Defaults
                            to "now" if omitted.
            depart_window: required for optimize="depart_window":
                           ``{"earliest": ..., "latest": ..., "step_minutes": 15}``.

        On success the dict has `ok=True` plus mode-specific fields:
            - safe: `waypoints`, `distance_nm`, `hazards_near`, `warnings`
            - time / fuel: above + `optimize_mode`, `departure_time`,
              `arrival_time`, `duration_minutes`, `fuel_gallons`,
              optional `legs`
            - depart_window: `optimize_mode="depart_window"`,
              `candidates_evaluated`, `best` (a route-shaped dict),
              `alternatives` (departure_time + duration + fuel only)

        On a routing-level failure the service still returns HTTP 200
        with `ok=False`. Network / unreachable failures are converted
        to `{"ok": False, "error": ...}` so callers never have to catch
        exceptions.
        """
        body: dict[str, Any] = {
            "start": {"lat": start_lat, "lon": start_lon},
            "end": {"lat": end_lat, "lon": end_lon},
            "optimize": optimize,
        }
        if departure_time is not None:
            body["departure_time"] = departure_time
        if depart_window is not None:
            body["depart_window"] = depart_window
        # depart_window: up to 24 candidates × ~8s.  time/fuel: 60-120s for
        # long routes against the 25m raster.  safe: typically <5s.
        if optimize == "depart_window":
            client_timeout = self._timeout_s + 200
        elif optimize in ("time", "fuel"):
            client_timeout = self._timeout_s + 90
        else:
            client_timeout = self._timeout_s + 30
        try:
            async with self._session.post(
                f"{self.url}/route",
                headers=self._headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=client_timeout),
            ) as resp:
                return await resp.json()
        except aiohttp.ClientConnectorError as err:
            LOGGER.warning("Router unreachable: %s", err)
            return {"ok": False, "error": "router service unreachable"}
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            LOGGER.warning("Router request failed: %s", err)
            return {"ok": False, "error": f"router request failed: {err}"}

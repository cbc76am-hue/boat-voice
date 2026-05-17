"""Async client for OpenCPN's REST RemoteControl API.

Posts GPX bodies to OpenCPN's HTTPS-on-8443 endpoint to insert/update
waypoints and routes in the Route & Mark Manager. All failures are SOFT —
callers log and continue. `_RESULT_NAMES` maps the integer `result` field in
OpenCPN's JSON reply; only 0 (NoError) is success.
"""
from __future__ import annotations

import asyncio
import json
import logging
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp


LOGGER = logging.getLogger(__name__)


_RESULT_NAMES = {
    0: "NoError",
    1: "GenericError",
    2: "ObjectRejected",
    3: "DuplicateRejected",
    4: "RouteInsertError",
    5: "NewPinRequested",
    6: "ObjectParseError",
}


class OpenCPNRestClient:
    """Thin async wrapper over OpenCPN's REST RemoteControl API."""

    def __init__(
        self,
        url: str,
        source: str,
        apikey: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Construct a client.

        Note: ``session`` is accepted for API compatibility but the client
        will use its OWN internal session with ``TCPConnector(force_close=True)``.
        OpenCPN's REST server doesn't handle HTTP keep-alive cleanly — the
        FIRST request on a reused socket succeeds, subsequent ones come back
        as result=6 (ObjectParseError).  Forcing a fresh connection per POST
        (which is what curl does by default) is what makes back-to-back
        pushes reliable.  Empirically tested against OpenCPN 5.10/5.11.
        """
        self.url = url.rstrip("/")
        self.source = source
        self.apikey = apikey
        # TLS verification disabled — OpenCPN ships a self-signed cert.
        self._ssl = False
        # Dedicated session with force-close so every POST gets a fresh socket.
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(force_close=True, ssl=False),
        )
        # Serialize concurrent posts at the client level so asyncio.gather
        # of SK+OpenCPN doesn't fan two requests at OpenCPN at once.
        self._post_lock = asyncio.Lock()
        # Small grace period after a successful push (OpenCPN takes a moment
        # to commit to navobj.db; sending the next request too fast can race).
        self._post_grace_s = 0.5

    async def close(self) -> None:
        """Close the internal aiohttp session.  Idempotent."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    @classmethod
    def from_credentials_file(
        cls,
        credentials_path: str | Path,
        session: aiohttp.ClientSession | None = None,
    ) -> "OpenCPNRestClient":
        """Load `{url, source, apikey}` from a JSON file (per the project layout).

        ``session`` is accepted for API compatibility but ignored — see the
        constructor docstring for why this client owns its own session.
        """
        creds = json.loads(Path(credentials_path).read_text())
        return cls(
            url=creds["url"],
            source=creds["source"],
            apikey=creds["apikey"],
        )

    async def ping(self) -> bool:
        """True if OpenCPN's REST endpoint answers /api/ping with result=0."""
        try:
            params = {"apikey": self.apikey, "source": self.source}
            async with self._session.get(
                f"{self.url}/api/ping?{urlencode(params)}",
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
                return int(data.get("result", -1)) == 0
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            LOGGER.warning("OpenCPN REST ping failed: %s", err)
            return False

    async def push_waypoint(
        self,
        name: str,
        lat: float,
        lon: float,
        description: str = "",
        activate: bool = True,
    ) -> bool:
        """Insert/update a single waypoint in OpenCPN. Returns True on result=0."""
        gpx = _build_waypoint_gpx(name, lat, lon, description)
        return await self._post_gpx(gpx, activate=activate)

    async def push_route(
        self,
        name: str,
        coords: list[tuple[float, float]],
        description: str = "",
        activate: bool = True,
    ) -> bool:
        """Insert a route as GPX. coords is a list of (lat, lon) tuples in order."""
        gpx = _build_route_gpx(name, coords, description)
        return await self._post_gpx(gpx, activate=activate)

    async def _post_gpx(self, gpx_body: str, activate: bool) -> bool:
        """POST /api/rx_object with a GPX body. Returns True only on result=0.

        Serialized via a lock and followed by a brief grace delay so that
        rapid back-to-back pushes (asyncio.gather'd SK+OpenCPN pairs, or
        sequential PlanRoute calls) don't hit OpenCPN's busy-queue path
        where it returns result=6 (misleading "ObjectParseError" — really
        "previous object still processing"). Retries once after the grace
        if the first attempt comes back as ObjectParseError.
        """
        async with self._post_lock:
            for attempt in (1, 2):
                ok, code = await self._post_gpx_once(gpx_body, activate)
                if ok:
                    await asyncio.sleep(self._post_grace_s)
                    return True
                if code != 6 or attempt == 2:
                    return False
                LOGGER.info(
                    "OpenCPN REST returned ObjectParseError; retrying after %.1fs",
                    self._post_grace_s,
                )
                await asyncio.sleep(self._post_grace_s)
            return False

    async def _post_gpx_once(
        self, gpx_body: str, activate: bool
    ) -> tuple[bool, int]:
        params = {
            "apikey": self.apikey,
            "source": self.source,
            "force": "1",
            "activate": "1" if activate else "0",
        }
        url = f"{self.url}/api/rx_object?{urlencode(params)}"
        try:
            async with self._session.post(
                url,
                data=gpx_body.encode("utf-8"),
                headers={"Content-Type": "application/gpx+xml"},
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    LOGGER.warning(
                        "OpenCPN REST POST returned HTTP %d: %s",
                        resp.status, body[:200],
                    )
                    return False, -1
                data = await resp.json(content_type=None)
                code = int(data.get("result", -1))
                if code == 0:
                    return True, 0
                name = _RESULT_NAMES.get(code, f"unknown-{code}")
                LOGGER.warning(
                    "OpenCPN REST rejected object: result=%d (%s)", code, name
                )
                return False, code
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            LOGGER.warning(
                "OpenCPN REST POST failed: %s (%r)", type(err).__name__, err
            )
            return False, -1


_GPX_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<gpx version="1.1" creator="tolly-voice" '
    'xmlns="http://www.topografix.com/GPX/1/1" '
    'xmlns:opencpn="http://www.opencpn.org">'
)
_GPX_FOOTER = "</gpx>"


def _build_waypoint_gpx(
    name: str, lat: float, lon: float, description: str
) -> str:
    parts = [
        _GPX_HEADER,
        f'<wpt lat="{float(lat):.7f}" lon="{float(lon):.7f}">',
        f"<name>{escape(name)}</name>",
    ]
    if description:
        parts.append(f"<desc>{escape(description)}</desc>")
    parts.append("<sym>diamond</sym>")
    parts.append("</wpt>")
    parts.append(_GPX_FOOTER)
    return "".join(parts)


def _build_route_gpx(
    name: str, coords: list[tuple[float, float]], description: str
) -> str:
    parts = [_GPX_HEADER, "<rte>", f"<name>{escape(name)}</name>"]
    if description:
        parts.append(f"<desc>{escape(description)}</desc>")
    for idx, (lat, lon) in enumerate(coords, start=1):
        parts.append(
            f'<rtept lat="{float(lat):.7f}" lon="{float(lon):.7f}">'
            f"<name>WP{idx}</name>"
            "</rtept>"
        )
    parts.append("</rte>")
    parts.append(_GPX_FOOTER)
    return "".join(parts)

#!/usr/bin/env python3
"""SK Resources -> OpenCPN layers/ GPX bridge.

Polls Signal K's resources API every few seconds. For each route or waypoint
seen, writes a GPX file under ~/.opencpn/layers/. When a resource disappears
from SK, removes the corresponding GPX file.

OpenCPN only scans the layers directory at startup or when the user clicks
"Reload Layers" in Options. After Tolly creates a new mark, you'll need to
trigger that once for it to appear on the chart.

Usage:
  python3 sk_opencpn_gpx_bridge.py
Reads token from ~/.config/tolly-nav-sim/sk-token (or BOAT_VOICE_SK_TOKEN env).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.sax.saxutils as xss
from pathlib import Path


LOGGER = logging.getLogger("sk_opencpn_bridge")

SK_URL = os.environ.get("SK_URL", "http://127.0.0.1:3000")
LAYERS_DIR = Path(os.environ.get("OPENCPN_LAYERS", "~/.opencpn/layers")).expanduser()
TOKEN_PATH = Path(os.environ.get(
    "BOAT_VOICE_SK_TOKEN",
    "/home/boat/.config/boat-voice/sk-token",
))
POLL_INTERVAL_S = float(os.environ.get("BRIDGE_POLL_S", "5"))

PREFIX = "sk-"  # filename prefix; only files we manage start with this


def load_token() -> str:
    return TOKEN_PATH.read_text().strip()


def http_get(url: str, token: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gpx_for_waypoint(uuid: str, wp: dict) -> str:
    name = xss.escape(wp.get("name") or uuid[:8])
    desc = xss.escape(wp.get("description") or "")
    coords = (wp.get("feature") or {}).get("geometry", {}).get("coordinates") or [0, 0]
    lon, lat = coords[0], coords[1]
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<gpx version="1.1" creator="sk-opencpn-bridge"
     xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:opencpn="http://www.opencpn.org">
  <wpt lat="{lat}" lon="{lon}">
    <name>{name}</name>
    <desc>{desc}</desc>
    <sym>diamond</sym>
    <extensions><opencpn:guid>{uuid}</opencpn:guid></extensions>
  </wpt>
</gpx>
"""


def gpx_for_route(uuid: str, rt: dict) -> str:
    name = xss.escape(rt.get("name") or uuid[:8])
    desc = xss.escape(rt.get("description") or "")
    coords = (rt.get("feature") or {}).get("geometry", {}).get("coordinates") or []
    pts = "\n".join(
        f'    <rtept lat="{c[1]}" lon="{c[0]}"><name>{name} {i+1}</name></rtept>'
        for i, c in enumerate(coords)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<gpx version="1.1" creator="sk-opencpn-bridge"
     xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:opencpn="http://www.opencpn.org">
  <rte>
    <name>{name}</name>
    <desc>{desc}</desc>
    <extensions><opencpn:guid>{uuid}</opencpn:guid></extensions>
{pts}
  </rte>
</gpx>
"""


def fname(kind: str, uuid: str) -> Path:
    return LAYERS_DIR / f"{PREFIX}{kind}-{uuid}.gpx"


def write_if_changed(path: Path, content: str) -> bool:
    """Write content to path. Return True if file changed, False if identical."""
    if path.exists() and path.read_text() == content:
        return False
    path.write_text(content)
    return True


def opencpn_running() -> bool:
    """True if at least one opencpn process is running."""
    try:
        subprocess.run(
            ["pgrep", "-x", "opencpn"],
            check=True, capture_output=True, timeout=2,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def opencpn_open(gpx_path: Path) -> None:
    """Best-effort hint to a running OpenCPN to import the new GPX file.

    OpenCPN's wxIPC --open mechanism is unreliable on Wayland/PipeWire setups
    (the wxClient::MakeConnection handshake to ~/.opencpn/opencpn-ipc often
    fails silently with exit 255). When it doesn't work, the GPX file still
    lives under ~/.opencpn/layers/ and gets picked up on the next OpenCPN
    restart or manual Options → Settings → Layers → Reload click. So we try
    --open opportunistically and log at DEBUG, not WARNING, on failure —
    nothing user-visible breaks if it doesn't work.
    """
    if not opencpn_running():
        LOGGER.debug("opencpn not running; --open skipped for %s", gpx_path.name)
        return
    try:
        result = subprocess.run(
            ["opencpn", "--open", str(gpx_path)],
            timeout=5, capture_output=True, text=True,
        )
        if result.returncode == 0:
            LOGGER.info("opencpn IPC accepted %s", gpx_path.name)
        else:
            LOGGER.debug(
                "opencpn --open exit %d (layer file in place; manual reload needed)",
                result.returncode,
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        LOGGER.debug("opencpn --open unavailable; layer file in place")


def reconcile_kind(token: str, kind: str, gpx_renderer) -> tuple[int, int, int]:
    """Sync one resource type (routes or waypoints).

    Returns (added, removed, unchanged) counts.
    """
    added = removed = unchanged = 0
    try:
        resources = http_get(f"{SK_URL}/signalk/v2/api/resources/{kind}", token)
    except urllib.error.URLError as e:
        LOGGER.warning("SK %s fetch failed: %s", kind, e)
        return (0, 0, 0)

    seen_uuids = set(resources.keys())

    for uuid, body in resources.items():
        path = fname(kind[:-1], uuid)  # 'routes' -> 'route', 'waypoints' -> 'waypoint'
        gpx = gpx_renderer(uuid, body)
        if write_if_changed(path, gpx):
            added += 1
            LOGGER.info("wrote %s", path.name)
            opencpn_open(path)
        else:
            unchanged += 1

    # Remove orphaned GPX files (UUID no longer in SK).
    glob_prefix = f"{PREFIX}{kind[:-1]}-"
    for f in LAYERS_DIR.glob(f"{glob_prefix}*.gpx"):
        uuid = f.name[len(glob_prefix):-4]
        if uuid not in seen_uuids:
            try:
                f.unlink()
                removed += 1
                LOGGER.info("removed %s", f.name)
            except OSError as e:
                LOGGER.warning("unlink %s failed: %s", f.name, e)

    return added, removed, unchanged


_running = True


def _stop(*_):
    global _running
    _running = False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    LAYERS_DIR.mkdir(parents=True, exist_ok=True)
    LOGGER.info("bridge starting: %s -> %s (poll %.1fs)", SK_URL, LAYERS_DIR, POLL_INTERVAL_S)

    try:
        token = load_token()
    except OSError as e:
        LOGGER.error("token load failed: %s", e)
        return 2

    while _running:
        try:
            a1, r1, u1 = reconcile_kind(token, "waypoints", gpx_for_waypoint)
            a2, r2, u2 = reconcile_kind(token, "routes", gpx_for_route)
            if (a1 + r1 + a2 + r2) > 0:
                LOGGER.info(
                    "tick: waypoints +%d/-%d/=%d; routes +%d/-%d/=%d",
                    a1, r1, u1, a2, r2, u2,
                )
        except Exception:
            LOGGER.exception("reconcile cycle failed; continuing")

        # Interruptible sleep
        for _ in range(int(POLL_INTERVAL_S * 10)):
            if not _running:
                break
            time.sleep(0.1)

    LOGGER.info("bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

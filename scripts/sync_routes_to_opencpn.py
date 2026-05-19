#!/usr/bin/env python3
"""Re-push routes from Signal K into OpenCPN's Route & Mark Manager via REST.

Usage:
    sync_routes_to_opencpn.py              # push everything in SK that's not in OpenCPN
    sync_routes_to_opencpn.py --all        # push every SK route, even duplicates by name
    sync_routes_to_opencpn.py --dry-run    # show what would be pushed, push nothing
    sync_routes_to_opencpn.py --name 'Hiram M. Chittenden Locks'  # push only that one

Why this exists: boat-voice pushes routes to SK + OpenCPN in parallel
during a voice route plan.  If OpenCPN is hung, mid-restart, or simply
not running, the OpenCPN push is logged-and-dropped while SK still has
it.  This script reconciles SK -> OpenCPN any time the operator needs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import ssl
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from boat_voice.opencpn_rest import OpenCPNRestClient  # noqa: E402
from boat_voice.sk_api import SKClient  # noqa: E402

import aiohttp  # noqa: E402

SK_URL = "http://127.0.0.1:3000"
SK_TOKEN_PATH = Path.home() / ".config" / "boat-voice" / "sk-token"
OPENCPN_CREDS = Path.home() / ".config" / "boat-voice" / "opencpn-rest.json"
OPENCPN_BASE = "https://127.0.0.1:8443"


def list_opencpn_routes() -> set[str]:
    """Names of routes already in OpenCPN's R&M Manager."""
    creds = json.loads(OPENCPN_CREDS.read_text())
    url = (f"{OPENCPN_BASE}/api/list-routes"
           f"?source={creds['source']}&apikey={creds['apikey']}")
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(url, context=ctx, timeout=10) as r:
        body = r.read().decode()
    m = re.search(r'"routes":\s*"(\[\[.*\]\])"', body)
    if not m:
        return set()
    return {name for _guid, name in json.loads(m.group(1))}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true",
                    help="push every SK route, even if a route by the same name "
                         "is already in OpenCPN")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be pushed, push nothing")
    ap.add_argument("--name", default=None,
                    help="push only the route(s) with this exact SK name")
    args = ap.parse_args()

    if not SK_TOKEN_PATH.is_file():
        print(f"ERROR: SK token not found at {SK_TOKEN_PATH}", file=sys.stderr)
        return 2
    if not OPENCPN_CREDS.is_file():
        print(f"ERROR: OpenCPN creds not found at {OPENCPN_CREDS}", file=sys.stderr)
        return 2

    existing = set() if args.all else list_opencpn_routes()
    if existing:
        print(f"OpenCPN already has: {sorted(existing)}\n")

    session = aiohttp.ClientSession()
    sk = SKClient(SK_URL, SK_TOKEN_PATH.read_text().strip(), session)
    opencpn = OpenCPNRestClient.from_credentials_file(str(OPENCPN_CREDS), session)

    sk_routes = await sk.list_resources("routes")
    print(f"SK has {len(sk_routes)} routes")

    pushed = skipped = failed = 0
    for uuid, rt in sk_routes.items():
        name = (rt.get("name") or "").strip()
        if not name:
            print(f"  SKIP (no name)  uuid={uuid[:8]}")
            skipped += 1
            continue
        if args.name and name != args.name:
            continue
        if name in existing:
            print(f"  SKIP (already in OpenCPN): {name!r}")
            skipped += 1
            continue
        coords = ((rt.get("feature") or {}).get("geometry", {}).get("coordinates")
                  or [])
        if len(coords) < 2:
            print(f"  SKIP (only {len(coords)} points): {name!r}")
            skipped += 1
            continue
        # GeoJSON stores [lon, lat]; push_route wants [(lat, lon), ...].
        pairs = [(float(c[1]), float(c[0])) for c in coords]
        desc = rt.get("description") or "Re-sync from Signal K"
        if args.dry_run:
            print(f"  WOULD PUSH: {name!r}  ({len(pairs)} waypoints)")
            continue
        print(f"  pushing {name!r}  ({len(pairs)} waypoints)... ",
              end="", flush=True)
        ok = await opencpn.push_route(name, pairs, desc)
        if ok:
            pushed += 1
            print("OK")
        else:
            failed += 1
            print("FAILED (OpenCPN rejected the push)")

    await session.close()
    print(f"\nDone: pushed={pushed} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

#!/usr/bin/env python3
"""NOAA tide + tidal-current predictions -> Signal K (with on-disk cache).

Once per hour, polls the NOAA CO-OPS API for tide predictions at La Conner
(Swinomish Channel) and tidal-current predictions at Skagit Bay channel SW
of Hope Island, then publishes the upcoming highs/lows and next slack/max
events onto the SK bus under `environment.tide.*` and
`environment.currents.*`.

Why these two stations:
  - 9448558  La Conner, Swinomish Channel  (48.3917 N, -122.4970 W)
      Subordinate tide station literally inside Swinomish Channel about a
      mile from Shelter Bay. Closer match than the Anacortes ferry-terminal
      stations which sit on the far side of Fidalgo Island. Has tide
      predictions (verified against the API on 2026-05-17).
  - PUG1628  Skagit Bay channel, SW of Hope Island  (48.398 N, -122.580 W)
      The exit current the boat actually encounters leaving Swinomish
      Channel into Skagit Bay -- closer + more boat-relevant than the
      Rosario Strait stations. Returns currents_predictions data
      (verified 2026-05-17). PUG1530 from the brief is actually in Hale
      Passage in south Puget Sound and is not the right station for us.

Cache:
  ~/.cache/tolly-tide-publisher/last.json -- the most recent successful
  NOAA payload (raw responses for both products). Re-loaded on startup so
  the boat has tide data immediately at sea even if the next refresh fails.
  Refresh is forced if the cache is older than CACHE_STALE_AFTER_S.

SK paths written (all under $source="tolly-tide-publisher"):
  environment.tide.heightNow              float, meters
  environment.tide.heightHigh             float, meters
  environment.tide.timeHigh               string ISO 8601 UTC
  environment.tide.heightLow              float, meters
  environment.tide.timeLow                string ISO 8601 UTC
  environment.tide.station                string
  environment.tide.builtAt                string ISO 8601 UTC

  environment.currents.station            string
  environment.currents.timeNextSlackBefore string ISO 8601 UTC
  environment.currents.timeNextMaxFlood   string ISO 8601 UTC
  environment.currents.maxFloodKnots      float, knots (positive magnitude)
  environment.currents.timeNextMaxEbb     string ISO 8601 UTC
  environment.currents.maxEbbKnots        float, knots (positive magnitude)
  environment.currents.builtAt            string ISO 8601 UTC
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import websocket  # python3-websocket (websocket-client 1.7.0)
except ImportError:
    websocket = None

# ----- config ---------------------------------------------------------------

SK_WS_URL = "ws://127.0.0.1:3000/signalk/v1/stream?subscribe=none"
SK_TOKEN_FILE = os.path.expanduser("~/.config/tolly-tide-publisher/sk-token")
SK_SOURCE = "tolly-tide-publisher"

CACHE_DIR = Path(os.path.expanduser("~/.cache/tolly-tide-publisher"))
CACHE_FILE = CACHE_DIR / "last.json"
CACHE_STALE_AFTER_S = 24 * 3600        # 24 h
REFRESH_INTERVAL_S = 3600              # 1 h

TIDE_STATION_ID = "9448558"
TIDE_STATION_NAME = "La Conner, Swinomish Channel"
CURRENTS_STATION_ID = "PUG1628"
CURRENTS_STATION_NAME = "Skagit Bay channel, SW of Hope Island"

NOAA_BASE = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
NOAA_APP = "tolly-tide-publisher"
# NOAA station local time is Pacific. lst_ldt = "local standard / local
# daylight" = America/Los_Angeles for these stations.
STATION_TZ = ZoneInfo("America/Los_Angeles")

FT_TO_M = 0.3048

HTTP_TIMEOUT_S = 15
WS_TIMEOUT_S = 5


# ----- SK delta publisher (adapted from tolly_nav_sim) ----------------------


class SkDeltaPublisher:
    """Maintains a SK WebSocket connection and pushes deltas.

    Supports sending a single value or a batch of {path: value} entries in
    one delta. Reconnects on failure. Silently no-ops if the websocket
    library or token is missing.
    """

    def __init__(self, url: str, token_path: str, source: str):
        self.url = url
        self.source = source
        self.token = self._load_token(token_path)
        self.ws: "websocket.WebSocket | None" = None

    @staticmethod
    def _load_token(path: str) -> str | None:
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError as e:
            print(f"sk: token load fail ({e}); SK pushes disabled", flush=True)
            return None

    def _connect(self) -> bool:
        if websocket is None or self.token is None:
            return False
        try:
            self.ws = websocket.create_connection(
                self.url,
                header=[f"Authorization: Bearer {self.token}"],
                timeout=WS_TIMEOUT_S,
            )
            print(f"sk: connected {self.url}", flush=True)
            return True
        except Exception as e:
            print(f"sk: connect fail ({e})", flush=True)
            self.ws = None
            return False

    def send_batch(self, values: dict[str, Any]) -> bool:
        """Send a single delta with multiple {path: value} pairs.

        Returns True on send success. Skips values that are None.
        """
        clean = [
            {"path": p, "value": v}
            for p, v in values.items()
            if v is not None
        ]
        if not clean:
            return False
        if self.ws is None and not self._connect():
            return False
        delta = {
            "updates": [
                {
                    "$source": self.source,
                    "values": clean,
                }
            ]
        }
        try:
            assert self.ws is not None
            self.ws.send(json.dumps(delta))
            return True
        except Exception as e:
            print(f"sk: send fail ({e}); will reconnect", flush=True)
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
            return False

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


# ----- NOAA fetch -----------------------------------------------------------


def _http_get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": NOAA_APP})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        body = resp.read().decode("utf-8")
    data = json.loads(body)
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"NOAA error: {data['error']}")
    return data


def fetch_tide_predictions(station_id: str) -> dict[str, Any]:
    """Pull a 48-hour window of hi/lo tide predictions starting today."""
    today = datetime.now(STATION_TZ).date()
    begin = today.strftime("%Y%m%d")
    end = (today + timedelta(days=2)).strftime("%Y%m%d")
    params = {
        "product": "predictions",
        "application": NOAA_APP,
        "begin_date": begin,
        "end_date": end,
        "datum": "MLLW",
        "station": station_id,
        "time_zone": "lst_ldt",
        "units": "english",
        "interval": "hilo",
        "format": "json",
    }
    url = f"{NOAA_BASE}?{urllib.parse.urlencode(params)}"
    return _http_get_json(url)


def fetch_current_predictions(station_id: str) -> dict[str, Any]:
    """Pull a 48-hour window of MAX_SLACK current predictions starting today."""
    today = datetime.now(STATION_TZ).date()
    begin = today.strftime("%Y%m%d")
    end = (today + timedelta(days=2)).strftime("%Y%m%d")
    params = {
        "product": "currents_predictions",
        "application": NOAA_APP,
        "begin_date": begin,
        "end_date": end,
        "station": station_id,
        "time_zone": "lst_ldt",
        "units": "english",
        "interval": "MAX_SLACK",
        "format": "json",
    }
    url = f"{NOAA_BASE}?{urllib.parse.urlencode(params)}"
    return _http_get_json(url)


# ----- shaping --------------------------------------------------------------


def _station_local_to_utc(t_str: str) -> datetime:
    """NOAA returns 'YYYY-MM-DD HH:MM' in lst_ldt -- treat as America/Los_Angeles."""
    naive = datetime.strptime(t_str, "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=STATION_TZ).astimezone(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shape_tide(payload: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Turn a raw predictions payload into the SK-facing dict.

    height_now is linearly interpolated between the bracketing hi/lo
    predictions -- good enough for "is it going up or down right now"
    awareness, not for piloting.
    """
    preds = payload.get("predictions") or []
    if not preds:
        return {}

    events: list[tuple[datetime, float, str]] = []
    for p in preds:
        try:
            t = _station_local_to_utc(p["t"])
            v_ft = float(p["v"])
            kind = p.get("type", "")
        except (KeyError, ValueError):
            continue
        events.append((t, v_ft, kind))
    events.sort(key=lambda e: e[0])

    # find next high and next low after `now`
    next_high = next((e for e in events if e[2] == "H" and e[0] >= now), None)
    next_low = next((e for e in events if e[2] == "L" and e[0] >= now), None)

    # interpolate "height now" between the most recent past event and the
    # next future event regardless of type
    past = [e for e in events if e[0] <= now]
    future = [e for e in events if e[0] > now]
    if past and future:
        t0, v0_ft, _ = past[-1]
        t1, v1_ft, _ = future[0]
        span = (t1 - t0).total_seconds()
        if span > 0:
            frac = (now - t0).total_seconds() / span
            now_ft = v0_ft + (v1_ft - v0_ft) * frac
        else:
            now_ft = v0_ft
    elif future:
        now_ft = future[0][1]
    elif past:
        now_ft = past[-1][1]
    else:
        now_ft = None

    out: dict[str, Any] = {
        "station": TIDE_STATION_NAME,
        "builtAt": _iso_utc(now),
    }
    if now_ft is not None:
        out["heightNow_m"] = round(now_ft * FT_TO_M, 3)
        out["heightNow_ft"] = round(now_ft, 2)
    if next_high is not None:
        out["heightHigh_m"] = round(next_high[1] * FT_TO_M, 3)
        out["heightHigh_ft"] = round(next_high[1], 2)
        out["timeHigh"] = _iso_utc(next_high[0])
    if next_low is not None:
        out["heightLow_m"] = round(next_low[1] * FT_TO_M, 3)
        out["heightLow_ft"] = round(next_low[1], 2)
        out["timeLow"] = _iso_utc(next_low[0])
    return out


def shape_currents(payload: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Turn currents_predictions into SK-facing dict.

    Velocity_Major is signed: positive = flood, negative = ebb. We publish
    the magnitudes (always positive) plus a separate signed field for
    debugging/expansion later.
    """
    inner = payload.get("current_predictions") or {}
    items = inner.get("cp") or []
    if not items:
        return {}

    events: list[tuple[datetime, str, float]] = []  # (t, type, signed knots)
    for p in items:
        try:
            t = _station_local_to_utc(p["Time"])
            kind = str(p.get("Type", "")).lower()
            v = float(p.get("Velocity_Major") or 0.0)
        except (KeyError, ValueError):
            continue
        events.append((t, kind, v))
    events.sort(key=lambda e: e[0])

    future = [e for e in events if e[0] >= now]

    next_slack = next((e for e in future if e[1] == "slack"), None)
    next_flood = next((e for e in future if e[1] == "flood"), None)
    next_ebb = next((e for e in future if e[1] == "ebb"), None)

    out: dict[str, Any] = {
        "station": CURRENTS_STATION_NAME,
        "builtAt": _iso_utc(now),
    }
    if next_slack is not None:
        out["timeNextSlackBefore"] = _iso_utc(next_slack[0])
    if next_flood is not None:
        out["timeNextMaxFlood"] = _iso_utc(next_flood[0])
        out["maxFloodKnots"] = round(abs(next_flood[2]), 2)
    if next_ebb is not None:
        out["timeNextMaxEbb"] = _iso_utc(next_ebb[0])
        out["maxEbbKnots"] = round(abs(next_ebb[2]), 2)
    return out


def to_sk_deltas(tide: dict[str, Any], currents: dict[str, Any]) -> dict[str, Any]:
    """Map shaped dicts into the literal SK paths we publish."""
    out: dict[str, Any] = {}
    if tide:
        if "heightNow_m" in tide:
            out["environment.tide.heightNow"] = tide["heightNow_m"]
        if "heightHigh_m" in tide:
            out["environment.tide.heightHigh"] = tide["heightHigh_m"]
        if "timeHigh" in tide:
            out["environment.tide.timeHigh"] = tide["timeHigh"]
        if "heightLow_m" in tide:
            out["environment.tide.heightLow"] = tide["heightLow_m"]
        if "timeLow" in tide:
            out["environment.tide.timeLow"] = tide["timeLow"]
        out["environment.tide.station"] = tide.get("station")
        out["environment.tide.builtAt"] = tide.get("builtAt")
    if currents:
        out["environment.currents.station"] = currents.get("station")
        if "timeNextSlackBefore" in currents:
            out["environment.currents.timeNextSlackBefore"] = currents["timeNextSlackBefore"]
        if "timeNextMaxFlood" in currents:
            out["environment.currents.timeNextMaxFlood"] = currents["timeNextMaxFlood"]
        if "maxFloodKnots" in currents:
            out["environment.currents.maxFloodKnots"] = currents["maxFloodKnots"]
        if "timeNextMaxEbb" in currents:
            out["environment.currents.timeNextMaxEbb"] = currents["timeNextMaxEbb"]
        if "maxEbbKnots" in currents:
            out["environment.currents.maxEbbKnots"] = currents["maxEbbKnots"]
        out["environment.currents.builtAt"] = currents.get("builtAt")
    return out


# ----- cache ----------------------------------------------------------------


def cache_load() -> dict[str, Any] | None:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def cache_save(blob: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(blob, f, indent=2)
    tmp.replace(CACHE_FILE)


def cache_age_s(blob: dict[str, Any] | None) -> float | None:
    if not blob:
        return None
    ts = blob.get("fetched_at")
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds()


# ----- main loop ------------------------------------------------------------


_running = True


def _stop(*_):
    global _running
    _running = False


def refresh_and_publish(sk: SkDeltaPublisher) -> tuple[bool, dict[str, Any]]:
    """Fetch + shape + publish. Returns (success, blob).

    On any NOAA failure we return (False, {}) so the caller can fall back
    to a cached blob if available.
    """
    now_utc = datetime.now(timezone.utc)
    try:
        tide_raw = fetch_tide_predictions(TIDE_STATION_ID)
    except Exception as e:
        print(f"noaa tide fetch fail: {e}", flush=True)
        return False, {}
    try:
        currents_raw = fetch_current_predictions(CURRENTS_STATION_ID)
    except Exception as e:
        print(f"noaa currents fetch fail: {e}", flush=True)
        return False, {}

    tide_shaped = shape_tide(tide_raw, now_utc)
    currents_shaped = shape_currents(currents_raw, now_utc)
    deltas = to_sk_deltas(tide_shaped, currents_shaped)
    if deltas:
        ok = sk.send_batch(deltas)
        print(
            f"publish: paths={len(deltas)} ok={ok} tide_now_m={deltas.get('environment.tide.heightNow')}",
            flush=True,
        )

    blob = {
        "fetched_at": _iso_utc(now_utc),
        "tide_station_id": TIDE_STATION_ID,
        "currents_station_id": CURRENTS_STATION_ID,
        "tide_raw": tide_raw,
        "currents_raw": currents_raw,
        "tide_shaped": tide_shaped,
        "currents_shaped": currents_shaped,
    }
    cache_save(blob)
    return True, blob


def publish_from_cache(sk: SkDeltaPublisher, blob: dict[str, Any]) -> None:
    """Re-shape the cached raw payload against current time, then publish.

    Reshaping (vs. publishing the cached shape verbatim) keeps "heightNow"
    fresh and rolls the next-high/next-low forward as time passes -- the
    raw payload covers 48 h so we get value out of the cache for ~2 days.
    """
    now_utc = datetime.now(timezone.utc)
    tide_raw = blob.get("tide_raw") or {}
    currents_raw = blob.get("currents_raw") or {}
    tide_shaped = shape_tide(tide_raw, now_utc)
    currents_shaped = shape_currents(currents_raw, now_utc)
    deltas = to_sk_deltas(tide_shaped, currents_shaped)
    if deltas:
        ok = sk.send_batch(deltas)
        print(
            f"publish (cache): paths={len(deltas)} ok={ok} cache_age_s={int(cache_age_s(blob) or -1)}",
            flush=True,
        )


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(
        f"tolly_tide_publisher: tide={TIDE_STATION_ID} currents={CURRENTS_STATION_ID}"
        f" refresh={REFRESH_INTERVAL_S}s",
        flush=True,
    )
    sk = SkDeltaPublisher(SK_WS_URL, SK_TOKEN_FILE, SK_SOURCE)

    last_fetch_at = 0.0

    # On boot: publish from cache immediately (so SK has something while we
    # wait on the network), then decide whether to fetch.
    cached = cache_load()
    if cached:
        publish_from_cache(sk, cached)

    age = cache_age_s(cached)
    needs_fetch = (
        cached is None or age is None or age > CACHE_STALE_AFTER_S
    )
    if needs_fetch:
        ok, blob = refresh_and_publish(sk)
        if ok:
            last_fetch_at = time.time()
            cached = blob
        elif cached is None:
            print("no cache and NOAA unreachable; will retry shortly", flush=True)
    else:
        # cache is fresh; next forced fetch is one interval after now
        last_fetch_at = time.time() - (REFRESH_INTERVAL_S - 60)

    # Steady-state loop: tick every 30 s. Refresh every REFRESH_INTERVAL_S.
    # On non-refresh ticks we still re-publish from cache so heightNow rolls
    # forward and any SK consumer reading the path always sees a recent
    # timestamp.
    SHAPE_TICK_S = 60
    last_shape_at = 0.0
    while _running:
        time.sleep(1.0)
        now = time.time()
        if now - last_fetch_at >= REFRESH_INTERVAL_S:
            ok, blob = refresh_and_publish(sk)
            if ok:
                last_fetch_at = now
                cached = blob
                last_shape_at = now
            else:
                # back off but not by much -- try again in 5 min
                last_fetch_at = now - (REFRESH_INTERVAL_S - 300)
                if cached is not None:
                    publish_from_cache(sk, cached)
                    last_shape_at = now
        elif now - last_shape_at >= SHAPE_TICK_S and cached is not None:
            publish_from_cache(sk, cached)
            last_shape_at = now

    sk.close()
    print("tolly_tide_publisher: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tool declarations + dispatch for Tolly."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import yaml

from .ha_api import HAClient, filter_entities
from .opencpn_rest import OpenCPNRestClient
from .router_client import RouterClient, humanize_error
from .sk_api import SKClient


LOGGER = logging.getLogger(__name__)

# Tool names that are not declared at the type-level here but handled in the
# session class itself (because they mutate session state, not HA state):
SESSION_LOCAL_TOOLS = {"set_conversation_mode"}

_NO_GPS_MSG = "No GPS fix is available from Signal K right now."


def _not_configured(service: str) -> dict[str, Any]:
    return {"error": f"{service} is not configured for this Tolly instance."}


def get_tool_declarations(entity_cheatsheet: str) -> list[dict[str, Any]]:
    """Return Gemini FunctionDeclaration dicts for all Tolly tools."""
    return [
        {
            "name": "GetLiveContext",
            "description": (
                "Snapshot the current state of all sensors and devices Tolly is "
                "allowed to see. Call this BEFORE answering any question about the "
                "boat's current state, conditions, or values."
            ),
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "GetDateTime",
            "description": "Current local date and time.",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "HassTurnOn",
            "description": (
                "Turn on a Home Assistant entity by id. You MUST use exact entity_id "
                f"values. Known entities: {entity_cheatsheet}"
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "entity_id": {"type": "STRING"},
                },
                "required": ["entity_id"],
            },
        },
        {
            "name": "HassTurnOff",
            "description": (
                "Turn off a Home Assistant entity by id. You MUST use exact entity_id "
                f"values. Known entities: {entity_cheatsheet}"
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "entity_id": {"type": "STRING"},
                },
                "required": ["entity_id"],
            },
        },
        {
            "name": "set_conversation_mode",
            "description": (
                "Toggle long-conversation behavior. Set active=true when the user "
                "says 'let's chat', false when 'stop', 'done', 'goodbye', or topic "
                "concludes."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "active": {"type": "BOOLEAN"},
                },
                "required": ["active"],
            },
        },
        {
            "name": "DiagnoseSelf",
            "description": (
                "Run an end-to-end self-test: mic device, speaker device, HA REST "
                "reachable, Gemini API reachable, configured model exists, entity "
                "exposure count. Returns a brief status report. Call this when the "
                "user says 'diagnose', 'self-test', 'are you working', or similar."
            ),
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "CreateWaypoint",
            "description": (
                "Save a navigation waypoint into Signal K. If the user says 'here' "
                "or 'at our position' or omits coordinates, leave latitude and "
                "longitude unset — they default to the boat's current GPS position. "
                "Confirm the saved name back to the user."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING", "description": "Short label for the waypoint."},
                    "description": {"type": "STRING", "description": "Optional longer note."},
                    "latitude": {"type": "NUMBER", "description": "Decimal degrees, positive N. Omit to use current position."},
                    "longitude": {"type": "NUMBER", "description": "Decimal degrees, positive E (negative for W). Omit to use current position."},
                },
                "required": ["name"],
            },
        },
        {
            "name": "ListWaypoints",
            "description": "List all saved waypoints. Use when the user asks 'what waypoints have we saved' or to verify a save.",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "DeleteWaypoint",
            "description": (
                "Delete a waypoint by its UUID. Get the UUID from ListWaypoints first. "
                "If the user names a waypoint to delete, look it up before calling this."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "uuid": {"type": "STRING", "description": "SK UUID of the waypoint."},
                },
                "required": ["uuid"],
            },
        },
        {
            "name": "CreateRoute",
            "description": (
                "Save a navigation route (ordered list of points). Each point is "
                "(latitude, longitude) in decimal degrees. The route needs at least "
                "two points. If the user wants the route to start at the current "
                "position, prepend a synthetic point — call GetCurrentPosition first "
                "and use that as the first coordinate."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "description": {"type": "STRING"},
                    "points": {
                        "type": "ARRAY",
                        "description": "Ordered points as [[lat, lon], [lat, lon], ...].",
                        "items": {
                            "type": "ARRAY",
                            "items": {"type": "NUMBER"},
                        },
                    },
                },
                "required": ["name", "points"],
            },
        },
        {
            "name": "ListRoutes",
            "description": "List all saved routes.",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "DeleteRoute",
            "description": "Delete a route by its UUID. Get the UUID from ListRoutes first.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "uuid": {"type": "STRING"},
                },
                "required": ["uuid"],
            },
        },
        {
            "name": "GetCurrentPosition",
            "description": (
                "Read the boat's current GPS position from Signal K. Returns "
                "latitude, longitude in decimal degrees. Use this when planning a "
                "route that starts at the boat or when the user asks 'where are we'."
            ),
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "GetTidesAndCurrents",
            "description": (
                "Read the current pre-cached NOAA tide and tidal-current "
                "predictions for the boat's home area (La Conner / Skagit "
                "Bay). Use this FIRST for any question about tides, currents, "
                "slack water, or tide-driven departure timing — the data is "
                "refreshed hourly from NOAA and cached locally so it works "
                "offline at sea. Only fall back to Google Search if this tool "
                "errors out, or for non-tide questions like weather forecast."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "time_window_hours": {
                        "type": "NUMBER",
                        "description": "How far ahead to consider (default 12). Currently informational only — the publisher always returns the next high/low and next slack/flood/ebb regardless.",
                    },
                },
            },
        },
        {
            "name": "PlanRoute",
            "description": (
                "Compute a safe water route from the boat's current position to a "
                "destination using NOAA chart data, depth, and hazard avoidance. "
                "Use this when the user says 'plan a route to <X>' or 'route us "
                "to <X>' or 'how do we get to <X>'. Returns a draft route that "
                "the user must visually review on the chart before navigating "
                "from it. The router only covers Puget Sound and the San Juan "
                "Islands (lat 47-49, lon -124.5 to -122)."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "destination_lat": {
                        "type": "NUMBER",
                        "description": "Destination latitude in decimal degrees, positive N.",
                    },
                    "destination_lon": {
                        "type": "NUMBER",
                        "description": "Destination longitude in decimal degrees, negative W.",
                    },
                    "destination_name": {
                        "type": "STRING",
                        "description": "Human-readable destination name, used as the route name.",
                    },
                    "start_lat": {
                        "type": "NUMBER",
                        "description": "Optional start latitude. Omit to use the boat's current GPS position from Signal K.",
                    },
                    "start_lon": {
                        "type": "NUMBER",
                        "description": "Optional start longitude. Omit to use the boat's current GPS position.",
                    },
                },
                "required": ["destination_lat", "destination_lon", "destination_name"],
            },
        },
    ]


def build_entity_cheatsheet(entities: list[dict[str, Any]]) -> str:
    """Build 'Friendly Name=entity_id' cheatsheet from live HA states."""
    if not entities:
        return "(no entities currently exposed)"
    parts = []
    for st in entities:
        eid = st.get("entity_id", "")
        name = st.get("attributes", {}).get("friendly_name") or eid
        parts.append(f"{name}={eid}")
    return ", ".join(parts)


async def fetch_exposed_states(
    ha: HAClient,
    include_patterns: list[str],
    exclude_patterns: list[str],
) -> list[dict[str, Any]]:
    """Fetch all HA states, then filter to the exposed set."""
    states = await ha.get_states()
    return filter_entities(states, include_patterns, exclude_patterns)


async def dispatch_tool(
    name: str,
    args: dict[str, Any],
    ha: HAClient,
    include_patterns: list[str],
    exclude_patterns: list[str],
    healthz_provider,
    sk: SKClient | None = None,
    router: RouterClient | None = None,
    opencpn: OpenCPNRestClient | None = None,
) -> dict[str, Any]:
    """Execute a tool call and return the response dict for Gemini.

    healthz_provider is a zero-arg async callable returning a dict (for DiagnoseSelf).
    set_conversation_mode is handled by the caller, not here.
    sk is optional — SK-backed tools return an error message if not configured.
    router is optional — PlanRoute returns an error message if not configured.
    opencpn is optional — when present, voice-created waypoints/routes are also
    pushed to OpenCPN's Route & Mark Manager (best-effort, never blocks SK).
    """
    try:
        if name == "GetLiveContext":
            exposed = await fetch_exposed_states(ha, include_patterns, exclude_patterns)
            entries = []
            for st in exposed:
                attrs = st.get("attributes", {}) or {}
                entry = {
                    "entity_id": st.get("entity_id"),
                    "state": st.get("state"),
                    "name": attrs.get("friendly_name") or st.get("entity_id"),
                }
                if "unit_of_measurement" in attrs:
                    entry["unit"] = str(attrs["unit_of_measurement"])
                entries.append(entry)
            return {"context": yaml.safe_dump(entries, default_flow_style=False)}

        if name == "GetDateTime":
            now = datetime.now()
            return {"datetime": now.strftime("%A, %B %d, %Y at %I:%M %p")}

        if name in ("HassTurnOn", "HassTurnOff"):
            entity_id = args.get("entity_id", "")
            if not entity_id:
                return {"error": "entity_id is required"}
            state = await ha.get_state(entity_id)
            if state is None:
                return {
                    "error": (
                        f"Entity {entity_id} not found. "
                        "Call GetLiveContext to see valid entity IDs."
                    )
                }
            service = "turn_on" if name == "HassTurnOn" else "turn_off"
            await ha.call_service(
                "homeassistant", service, {"entity_id": entity_id}
            )
            friendly = (state.get("attributes") or {}).get("friendly_name", entity_id)
            verb = "Turned on" if service == "turn_on" else "Turned off"
            return {"result": f"{verb} {friendly} ({entity_id})"}

        if name == "DiagnoseSelf":
            health = await healthz_provider()
            return _diagnose_self_summary(health)

        if name in (
            "CreateWaypoint", "ListWaypoints", "DeleteWaypoint",
            "CreateRoute", "ListRoutes", "DeleteRoute",
            "GetCurrentPosition",
        ):
            if sk is None:
                return _not_configured("Signal K")
            return await _dispatch_sk_tool(name, args, sk, opencpn)

        if name == "GetTidesAndCurrents":
            if sk is None:
                return _not_configured("Signal K")
            return await _dispatch_tide_tool(args, sk)

        if name == "PlanRoute":
            if sk is None:
                return _not_configured("Signal K")
            if router is None:
                return _not_configured("Routing service")
            return await _dispatch_router_tool(name, args, sk, router, opencpn)

    except Exception as err:
        LOGGER.exception("Tool %s failed", name)
        return {"error": f"{name} failed: {err}"}

    return {"error": f"Unknown tool: {name}"}


async def _dispatch_sk_tool(
    name: str,
    args: dict[str, Any],
    sk: SKClient,
    opencpn: OpenCPNRestClient | None = None,
) -> dict[str, Any]:
    if name == "GetCurrentPosition":
        pos = await sk.get_position()
        if pos is None:
            return {"error": "Current position is not available from Signal K."}
        lat, lon = pos
        return {"latitude": lat, "longitude": lon}

    if name == "CreateWaypoint":
        wp_name = (args.get("name") or "").strip()
        if not wp_name:
            return {"error": "Waypoint name is required."}
        lat = args.get("latitude")
        lon = args.get("longitude")
        if lat is None or lon is None:
            pos = await sk.get_position()
            if pos is None:
                return {"error": _NO_GPS_MSG}
            lat, lon = pos
        description = args.get("description", "") or ""
        sk_task = asyncio.create_task(
            sk.create_waypoint(wp_name, float(lat), float(lon), description)
        )
        ocpn_task = asyncio.create_task(
            _push_to_opencpn_waypoint(opencpn, wp_name, float(lat), float(lon), description)
        )
        uuid = await sk_task
        await ocpn_task
        if uuid is None:
            return {"error": "Signal K rejected the waypoint."}
        return {
            "result": f"Saved waypoint '{wp_name}' at {lat:.5f}, {lon:.5f}",
            "uuid": uuid,
        }

    if name == "ListWaypoints":
        wps = await sk.list_resources("waypoints")
        if not wps:
            return {"waypoints": [], "count": 0}
        out = []
        for uuid, wp in wps.items():
            coords = (wp.get("feature") or {}).get("geometry", {}).get("coordinates") or [None, None]
            out.append({
                "uuid": uuid,
                "name": wp.get("name", ""),
                "latitude": coords[1],
                "longitude": coords[0],
            })
        return {"waypoints": out, "count": len(out)}

    if name == "DeleteWaypoint":
        uuid = (args.get("uuid") or "").strip()
        if not uuid:
            return {"error": "Waypoint UUID is required."}
        ok = await sk.delete_resource("waypoints", uuid)
        return {"result": "Deleted." if ok else "Delete failed."}

    if name == "CreateRoute":
        rt_name = (args.get("name") or "").strip()
        pts = args.get("points") or []
        if not rt_name or len(pts) < 2:
            return {"error": "Route needs a name and at least 2 points."}
        coords = [(float(p[0]), float(p[1])) for p in pts]
        description = args.get("description", "") or ""
        sk_task = asyncio.create_task(sk.create_route(rt_name, coords, description))
        ocpn_task = asyncio.create_task(
            _push_to_opencpn_route(opencpn, rt_name, coords, description)
        )
        uuid = await sk_task
        await ocpn_task
        if uuid is None:
            return {"error": "Signal K rejected the route."}
        return {
            "result": f"Saved route '{rt_name}' with {len(coords)} points",
            "uuid": uuid,
        }

    if name == "ListRoutes":
        rts = await sk.list_resources("routes")
        if not rts:
            return {"routes": [], "count": 0}
        out = []
        for uuid, rt in rts.items():
            n_points = len((rt.get("feature") or {}).get("geometry", {}).get("coordinates") or [])
            out.append({
                "uuid": uuid,
                "name": rt.get("name", ""),
                "points": n_points,
                "distance_m": rt.get("distance"),
            })
        return {"routes": out, "count": len(out)}

    if name == "DeleteRoute":
        uuid = (args.get("uuid") or "").strip()
        if not uuid:
            return {"error": "Route UUID is required."}
        ok = await sk.delete_resource("routes", uuid)
        return {"result": "Deleted." if ok else "Delete failed."}

    return {"error": f"Unknown SK tool: {name}"}


# ----------------- tide + currents tool -----------------


_M_TO_FT = 3.28084


async def _dispatch_tide_tool(
    args: dict[str, Any],
    sk: SKClient,
) -> dict[str, Any]:
    """Read tide + currents from SK (populated by tolly-tide-publisher).

    Falls back gracefully on missing paths: returns whatever is present.
    Heights come back from SK in meters; we also include feet because the
    boat is reasoned about in feet (charts, depth, freeboard).
    """
    paths = [
        "environment.tide.heightNow",
        "environment.tide.heightHigh",
        "environment.tide.timeHigh",
        "environment.tide.heightLow",
        "environment.tide.timeLow",
        "environment.tide.station",
        "environment.tide.builtAt",
        "environment.currents.station",
        "environment.currents.timeNextSlackBefore",
        "environment.currents.timeNextMaxFlood",
        "environment.currents.maxFloodKnots",
        "environment.currents.timeNextMaxEbb",
        "environment.currents.maxEbbKnots",
        "environment.currents.builtAt",
    ]
    results = await asyncio.gather(*[sk.get_path(p) for p in paths])
    v = dict(zip(paths, results))

    if all(x is None for x in results):
        return {
            "error": (
                "Tide and current data are not available — the tide publisher "
                "may not be running, or it hasn't fetched yet."
            )
        }

    def _m_to_ft(m: Any) -> float | None:
        try:
            return round(float(m) * _M_TO_FT, 2)
        except (TypeError, ValueError):
            return None

    tide_now_m = v["environment.tide.heightNow"]
    tide_hi_m = v["environment.tide.heightHigh"]
    tide_lo_m = v["environment.tide.heightLow"]

    response: dict[str, Any] = {
        "station": v["environment.tide.station"],
        "data_built_at": v["environment.tide.builtAt"],
        "time_window_hours": args.get("time_window_hours", 12),
    }
    if tide_now_m is not None:
        response["height_now_m"] = round(float(tide_now_m), 3)
        response["height_now_ft"] = _m_to_ft(tide_now_m)
    if tide_hi_m is not None or v["environment.tide.timeHigh"] is not None:
        response["next_high"] = {
            "time": v["environment.tide.timeHigh"],
            "height_m": round(float(tide_hi_m), 3) if tide_hi_m is not None else None,
            "height_ft": _m_to_ft(tide_hi_m),
        }
    if tide_lo_m is not None or v["environment.tide.timeLow"] is not None:
        response["next_low"] = {
            "time": v["environment.tide.timeLow"],
            "height_m": round(float(tide_lo_m), 3) if tide_lo_m is not None else None,
            "height_ft": _m_to_ft(tide_lo_m),
        }

    current_block: dict[str, Any] = {
        "station": v["environment.currents.station"],
        "data_built_at": v["environment.currents.builtAt"],
    }
    if v["environment.currents.timeNextSlackBefore"] is not None:
        current_block["next_slack"] = v["environment.currents.timeNextSlackBefore"]
    if v["environment.currents.timeNextMaxFlood"] is not None or v["environment.currents.maxFloodKnots"] is not None:
        current_block["next_max_flood"] = {
            "time": v["environment.currents.timeNextMaxFlood"],
            "knots": v["environment.currents.maxFloodKnots"],
        }
    if v["environment.currents.timeNextMaxEbb"] is not None or v["environment.currents.maxEbbKnots"] is not None:
        current_block["next_max_ebb"] = {
            "time": v["environment.currents.timeNextMaxEbb"],
            "knots": v["environment.currents.maxEbbKnots"],
        }
    if len(current_block) > 2:  # more than just station + builtAt
        response["current"] = current_block

    return response


async def _push_to_opencpn_waypoint(
    opencpn: OpenCPNRestClient | None,
    name: str,
    lat: float,
    lon: float,
    description: str = "",
) -> bool:
    if opencpn is None:
        return False
    ok = await opencpn.push_waypoint(name, float(lat), float(lon), description)
    if not ok:
        LOGGER.warning("OpenCPN REST did not accept waypoint '%s' (SK still has it)", name)
    return ok


async def _push_to_opencpn_route(
    opencpn: OpenCPNRestClient | None,
    name: str,
    coords: list[tuple[float, float]],
    description: str = "",
) -> bool:
    if opencpn is None:
        return False
    ok = await opencpn.push_route(name, coords, description)
    if not ok:
        LOGGER.warning("OpenCPN REST did not accept route '%s' (SK still has it)", name)
    return ok


# ----------------- router-backed tools -----------------


async def _dispatch_router_tool(
    name: str,
    args: dict[str, Any],
    sk: SKClient,
    router: RouterClient,
    opencpn: OpenCPNRestClient | None,
) -> dict[str, Any]:
    if name != "PlanRoute":
        return {"error": f"Unknown router tool: {name}"}

    dest_lat = args.get("destination_lat")
    dest_lon = args.get("destination_lon")
    dest_name = (args.get("destination_name") or "").strip()

    start_lat = args.get("start_lat")
    start_lon = args.get("start_lon")
    if start_lat is None or start_lon is None:
        pos = await sk.get_position()
        if pos is None:
            return {"error": _NO_GPS_MSG}
        start_lat, start_lon = pos

    routed = await router.plan_route(
        float(start_lat), float(start_lon),
        float(dest_lat), float(dest_lon),
    )
    if not routed.get("ok"):
        return {"error": humanize_error(routed.get("error") or "")}

    waypoints = routed.get("waypoints") or []
    coords = [(float(w["lat"]), float(w["lon"])) for w in waypoints]
    distance_nm = float(routed.get("distance_nm") or 0.0)
    hazards_near = int(routed.get("hazards_near") or 0)
    warnings = list(routed.get("warnings") or [])

    # If the router nudged the start to find navigable water (e.g. the boat is
    # in a marina slip the raster marks as no-go), the route polyline doesn't
    # visually connect to the boat's actual position.  Prepend the requested
    # start coord as the first waypoint so the chart shows the full path from
    # where the boat is to the destination.
    start_was_nudged = any("start nudged" in str(w).lower() for w in warnings)
    if start_was_nudged and coords:
        first_lat, first_lon = coords[0]
        if abs(first_lat - float(start_lat)) > 1e-6 or abs(first_lon - float(start_lon)) > 1e-6:
            coords = [(float(start_lat), float(start_lon))] + coords

    description_parts = [f"Planned by tolly-router to {dest_name}."]
    if hazards_near:
        description_parts.append(f"{hazards_near} hazard(s) within 200 m of track.")
    for w in warnings:
        description_parts.append(str(w))
    description = " ".join(description_parts)

    sk_task = asyncio.create_task(sk.create_route(dest_name, coords, description))
    ocpn_task = asyncio.create_task(
        _push_to_opencpn_route(opencpn, dest_name, coords, description)
    )
    uuid = await sk_task
    chart_pushed = await ocpn_task
    if uuid is None:
        return {"error": "Signal K rejected the planned route."}

    summary_bits = [
        f"Planned route to {dest_name}: {len(coords)} waypoints,",
        f"{distance_nm:.1f} nautical miles.",
    ]
    if hazards_near:
        plural = "s" if hazards_near != 1 else ""
        summary_bits.append(
            f"{hazards_near} charted hazard{plural} within 200 meters of the track."
        )
    if warnings:
        summary_bits.append("Note: " + "; ".join(str(w) for w in warnings) + ".")
    summary_bits.append(
        "Showing as a draft on the chart — review it before you follow it."
    )
    if not chart_pushed:
        summary_bits.append(
            "(Saved in Signal K; the chart display may need a manual refresh.)"
        )

    return {
        "result": " ".join(summary_bits),
        "uuid": uuid,
        "distance_nm": round(distance_nm, 2),
        "waypoint_count": len(coords),
        "hazards_near": hazards_near,
        "warnings": warnings,
    }


def _diagnose_self_summary(health: dict[str, Any]) -> dict[str, Any]:
    """Turn a /healthz dict into a short human-readable report for spoken output."""
    parts = []
    if health.get("ha_reachable"):
        parts.append("Home Assistant is reachable")
    else:
        parts.append("Home Assistant is NOT reachable")
    if health.get("gemini_reachable"):
        parts.append("Gemini is reachable")
    else:
        parts.append("Gemini is NOT reachable")
    if health.get("model_valid"):
        parts.append(f"model {health.get('model')} is valid")
    else:
        parts.append(f"model {health.get('model')} did NOT validate")
    if health.get("mic"):
        parts.append("microphone is open")
    else:
        parts.append("microphone is NOT open")
    if health.get("speaker"):
        parts.append("speaker is open")
    else:
        parts.append("speaker is NOT open")
    count = health.get("exposed_entity_count")
    if count is not None:
        parts.append(f"{count} entities currently exposed")
    uptime = health.get("uptime_s")
    if uptime is not None:
        parts.append(f"uptime {int(uptime)} seconds")
    return {"report": ". ".join(parts) + "."}

"""Tool declarations + dispatch for Tolly."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import yaml

from .ha_api import HAClient, filter_entities


LOGGER = logging.getLogger(__name__)

# Tool names that are not declared at the type-level here but handled in the
# session class itself (because they mutate session state, not HA state):
SESSION_LOCAL_TOOLS = {"set_conversation_mode"}


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
) -> dict[str, Any]:
    """Execute a tool call and return the response dict for Gemini.

    healthz_provider is a zero-arg async callable returning a dict (for DiagnoseSelf).
    set_conversation_mode is handled by the caller, not here.
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

    except Exception as err:
        LOGGER.exception("Tool %s failed", name)
        return {"error": f"{name} failed: {err}"}

    return {"error": f"Unknown tool: {name}"}


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

"""NOAA Tides + Marine Forecast clients — stubbed for v1, implemented in v1.1.

Per BOAT_VOICE_PLAN.md §10 #20-21, v1 relies on Gemini's built-in google_search
grounding instead. These stubs are here so the tool registry can list them
without crashing if someone wires them up early.
"""
from __future__ import annotations


async def get_tide_predictions(station_id: str, hours_ahead: int = 24) -> dict:
    return {
        "error": "NoaaTides not implemented in v1; use Google Search for tide info.",
    }


async def get_marine_forecast(zone: str) -> dict:
    return {
        "error": "NoaaMarineForecast not implemented in v1; "
        "use Google Search for the marine forecast.",
    }

"""YAML config loader for boat-voice."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path(
    os.environ.get(
        "BOAT_VOICE_CONFIG",
        str(Path.home() / ".config" / "boat-voice" / "config.yaml"),
    )
)


@dataclass
class GeminiConfig:
    api_key: str
    model: str
    fallback_model: str
    voice: str
    thinking_level: str


@dataclass
class HAConfig:
    url: str
    long_lived_token: str


@dataclass
class ServerConfig:
    listen_host: str
    listen_port: int


@dataclass
class AudioConfig:
    input_device: str | None
    output_device: str | None
    sample_rate_in: int
    sample_rate_out: int
    input_chunk_ms: int
    mute_mic_during_playback: bool


@dataclass
class ConnectivityConfig:
    health_check_url: str
    health_check_interval_s: int


@dataclass
class ConversationConfig:
    silence_end_ms: int
    start_sensitivity: str
    end_sensitivity: str
    conversation_mode_default: bool
    conversation_idle_timeout_s: int


@dataclass
class EntityConfig:
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)


@dataclass
class HomePortConfig:
    name: str
    latitude: float
    longitude: float


@dataclass
class NoaaConfig:
    default_tide_station: str
    default_marine_zone: str


@dataclass
class LoggingConfig:
    level: str
    destination: str


@dataclass
class Config:
    gemini: GeminiConfig
    ha: HAConfig
    server: ServerConfig
    audio: AudioConfig
    connectivity: ConnectivityConfig
    conversation: ConversationConfig
    entities: EntityConfig
    home_port: HomePortConfig
    noaa: NoaaConfig
    logging: LoggingConfig


def _get(d: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate the boat-voice config.yaml."""
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}

    return Config(
        gemini=GeminiConfig(
            api_key=_get(raw, "gemini.api_key", ""),
            model=_get(raw, "gemini.model", "gemini-3.1-flash-live-preview"),
            fallback_model=_get(
                raw, "gemini.fallback_model", "gemini-2.5-flash-native-audio-latest"
            ),
            voice=_get(raw, "gemini.voice", "Ursa"),
            thinking_level=_get(raw, "gemini.thinking_level", "minimal"),
        ),
        ha=HAConfig(
            url=_get(raw, "ha.url", "http://localhost:8123").rstrip("/"),
            long_lived_token=_get(raw, "ha.long_lived_token", ""),
        ),
        server=ServerConfig(
            listen_host=_get(raw, "server.listen_host", "127.0.0.1"),
            listen_port=int(_get(raw, "server.listen_port", 8765)),
        ),
        audio=AudioConfig(
            input_device=_get(raw, "audio.input_device"),
            output_device=_get(raw, "audio.output_device"),
            sample_rate_in=int(_get(raw, "audio.sample_rate_in", 16000)),
            sample_rate_out=int(_get(raw, "audio.sample_rate_out", 24000)),
            input_chunk_ms=int(_get(raw, "audio.input_chunk_ms", 100)),
            mute_mic_during_playback=bool(
                _get(raw, "audio.mute_mic_during_playback", True)
            ),
        ),
        connectivity=ConnectivityConfig(
            health_check_url=_get(
                raw,
                "connectivity.health_check_url",
                "https://generativelanguage.googleapis.com/v1beta/models",
            ),
            health_check_interval_s=int(
                _get(raw, "connectivity.health_check_interval_s", 60)
            ),
        ),
        conversation=ConversationConfig(
            silence_end_ms=int(_get(raw, "conversation.silence_end_ms", 1500)),
            start_sensitivity=_get(
                raw,
                "conversation.start_sensitivity",
                "START_SENSITIVITY_HIGH",
            ),
            end_sensitivity=_get(
                raw,
                "conversation.end_sensitivity",
                "END_SENSITIVITY_LOW",
            ),
            conversation_mode_default=bool(
                _get(raw, "conversation.conversation_mode_default", False)
            ),
            conversation_idle_timeout_s=int(
                _get(raw, "conversation.conversation_idle_timeout_s", 60)
            ),
        ),
        entities=EntityConfig(
            include_patterns=list(_get(raw, "entities.include_patterns", []) or []),
            exclude_patterns=list(_get(raw, "entities.exclude_patterns", []) or []),
        ),
        home_port=HomePortConfig(
            name=_get(raw, "home_port.name", "Shelter Bay, La Conner, WA"),
            latitude=float(_get(raw, "home_port.latitude", 48.4045)),
            longitude=float(_get(raw, "home_port.longitude", -122.5061)),
        ),
        noaa=NoaaConfig(
            default_tide_station=_get(raw, "noaa.default_tide_station", "9448043"),
            default_marine_zone=_get(raw, "noaa.default_marine_zone", "PZZ133"),
        ),
        logging=LoggingConfig(
            level=_get(raw, "logging.level", "INFO"),
            destination=_get(raw, "logging.destination", "journald"),
        ),
    )

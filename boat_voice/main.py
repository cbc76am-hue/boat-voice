"""Top-level orchestrator: STT + Claude + Piper voice path."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from .audio import MicStream, SpeakerSink
from .claude_llm import ClaudeLLM
from .config import Config, load_config
from .ha_api import HAClient
from .opencpn_rest import OpenCPNRestClient
from .prompts import build_system_prompt
from .router_client import RouterClient
from .server import build_app
from .sk_api import SKClient
from .stt import WhisperSTT
from .tools import (
    build_entity_cheatsheet,
    dispatch_tool,
    fetch_exposed_states,
    get_tool_declarations_for_claude,
)
from .tts import PiperTTS
from .voice_session import VoiceSession


LOGGER = logging.getLogger("boat_voice")

READY_FLAG_PATH = Path("/tmp/boat-voice.ready")


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)


class Orchestrator:
    """boat-voice runtime: lifecycle, talk loop, healthz."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._http: aiohttp.ClientSession | None = None
        self._ha: HAClient | None = None
        self._sk: SKClient | None = None
        self._router: RouterClient | None = None
        self._opencpn: OpenCPNRestClient | None = None
        self._stt: WhisperSTT | None = None
        self._tts: PiperTTS | None = None
        self._llm: ClaudeLLM | None = None
        self._voice: VoiceSession | None = None
        self._mic: MicStream | None = None
        self._speaker: SpeakerSink | None = None
        self._talk_lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._start_time = time.monotonic()
        self._last_input_at = time.monotonic()
        self._conv_mode = bool(cfg.conversation.conversation_mode_default)
        self._health: dict[str, Any] = {}
        self._entity_cheatsheet = "(not loaded)"

    # -------- lifecycle --------

    async def start(self) -> web.AppRunner:
        _configure_logging(self.cfg.logging.level)
        LOGGER.info(
            "boat-voice starting (LLM=%s, STT=%s, TTS=piper)",
            self.cfg.claude.model, self.cfg.whisper.model_size,
        )

        self._http = aiohttp.ClientSession()
        self._ha = HAClient(self.cfg.ha.url, self.cfg.ha.long_lived_token, self._http)

        try:
            self._sk = SKClient.from_token_file(
                self.cfg.sk.url, self.cfg.sk.token_path, self._http
            )
            LOGGER.info("Signal K configured: %s", self.cfg.sk.url)
        except FileNotFoundError as err:
            LOGGER.warning(
                "Signal K credentials missing (%s); SK-backed tools disabled", err
            )
            self._sk = None

        self._router = RouterClient(
            self.cfg.router.url, self._http,
            timeout_s=self.cfg.router.timeout_s,
        )
        router_ok = await self._router.ping()
        LOGGER.info(
            "tolly-router %s: %s",
            self.cfg.router.url,
            "ready" if router_ok else "NOT ready",
        )

        try:
            self._opencpn = OpenCPNRestClient.from_credentials_file(
                self.cfg.opencpn.rest_credentials_path, self._http
            )
            opencpn_ok = await self._opencpn.ping()
            LOGGER.info(
                "OpenCPN REST %s: %s",
                self._opencpn.url,
                "ready" if opencpn_ok else "NOT ready",
            )
        except FileNotFoundError as err:
            LOGGER.warning(
                "OpenCPN REST credentials missing (%s); chart push disabled", err
            )
            self._opencpn = None

        # Audio devices.
        self._speaker = SpeakerSink(
            device=self.cfg.audio.output_device,
            sample_rate=self.cfg.audio.sample_rate_out,
        )
        try:
            self._speaker.open()
        except Exception as err:
            LOGGER.error("Speaker open failed (continuing degraded): %s", err)

        self._mic = MicStream(
            device=self.cfg.audio.input_device,
            sample_rate=self.cfg.audio.sample_rate_in,
            chunk_ms=self.cfg.audio.input_chunk_ms,
        )
        try:
            self._mic.start()
        except Exception as err:
            LOGGER.error("Mic start failed (continuing degraded): %s", err)

        # Entity cheatsheet for prompt.
        await self._refresh_entity_cheatsheet()

        # Voice-path stack: STT, TTS, Claude.
        self._stt = WhisperSTT(
            model_size=self.cfg.whisper.model_size,
            device=self.cfg.whisper.device,
            compute_type=self.cfg.whisper.compute_type,
        )
        voice_path = self.cfg.piper.voice_path or None
        self._tts = PiperTTS(voice_path=voice_path)

        anthropic_key = self._load_anthropic_key()
        if not anthropic_key:
            LOGGER.error(
                "Anthropic API key missing (path=%s); LLM disabled",
                self.cfg.claude.api_key_path,
            )

        system_prompt = build_system_prompt(
            self._entity_cheatsheet, self.cfg.home_port.name
        )
        tool_decls = get_tool_declarations_for_claude(self._entity_cheatsheet)
        self._llm = ClaudeLLM(
            api_key=anthropic_key or "",
            model=self.cfg.claude.model,
            fallback_model=self.cfg.claude.fallback_model,
            system_prompt=system_prompt,
            tools=tool_decls,
            max_history_messages=self.cfg.claude.max_history_messages,
        )

        self._voice = VoiceSession(
            stt=self._stt,
            tts=self._tts,
            llm=self._llm,
            mic=self._mic,
            speaker=self._speaker,
            mic_sample_rate=self.cfg.audio.sample_rate_in,
            speaker_sample_rate=self.cfg.audio.sample_rate_out,
        )

        # Load STT + TTS models in background so /talk doesn't pay the cost.
        asyncio.create_task(self._warm_voice_session(), name="voice_warm")

        # Start the aiohttp server.
        app = build_app(
            on_talk=self._on_talk,
            on_conversation_mode=self._on_conversation_mode,
            healthz_provider=self._healthz,
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(
            runner, self.cfg.server.listen_host, self.cfg.server.listen_port
        )
        await site.start()
        LOGGER.info(
            "boat-voice listening on http://%s:%d",
            self.cfg.server.listen_host, self.cfg.server.listen_port,
        )

        asyncio.create_task(self._health_loop(), name="health_loop")

        try:
            READY_FLAG_PATH.write_text(str(int(time.time())))
        except Exception as err:
            LOGGER.warning("Could not write ready flag: %s", err)

        return runner

    async def stop(self) -> None:
        self._cancel.set()
        if self._voice is not None:
            await self._voice.close()
        if self._mic is not None:
            self._mic.stop()
        if self._speaker is not None:
            self._speaker.close()
        if self._http is not None:
            await self._http.close()
        try:
            READY_FLAG_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    # -------- setup helpers --------

    def _load_anthropic_key(self) -> str | None:
        # Prefer env override (systemd EnvironmentFile pattern); else file.
        env_key = os.environ.get("ANTHROPIC_API_KEY")
        if env_key:
            return env_key
        path = Path(self.cfg.claude.api_key_path)
        if not path.is_file():
            return None
        try:
            return path.read_text().strip()
        except OSError as err:
            LOGGER.error("Could not read Anthropic key at %s: %s", path, err)
            return None

    async def _warm_voice_session(self) -> None:
        if self._voice is None:
            return
        try:
            await self._voice.start()
            LOGGER.info("voice session warmed (STT + TTS loaded)")
        except Exception as err:
            LOGGER.exception("voice session warm failed: %s", err)

    async def _refresh_entity_cheatsheet(self) -> None:
        assert self._ha is not None
        try:
            states = await fetch_exposed_states(
                self._ha,
                self.cfg.entities.include_patterns,
                self.cfg.entities.exclude_patterns,
            )
        except Exception as err:
            LOGGER.warning("Could not refresh entity cheatsheet: %s", err)
            return
        self._entity_cheatsheet = build_entity_cheatsheet(states)
        LOGGER.info("Loaded %d exposed entities for system prompt.", len(states))

    # -------- HTTP callbacks --------

    async def _on_talk(self) -> None:
        asyncio.create_task(self._talk_session(), name="talk_session")

    async def _on_conversation_mode(self, active: bool) -> None:
        self._conv_mode = bool(active)
        LOGGER.info("conversation_mode -> %s (external)", active)
        if active and not self._talk_lock.locked():
            asyncio.create_task(self._talk_session(), name="talk_session_conv")

    async def _talk_session(self) -> None:
        if self._voice is None:
            LOGGER.warning("No voice session; ignoring /talk")
            return
        if self._talk_lock.locked():
            LOGGER.info("/talk arrived while previous talk still running; ignored")
            return

        async with self._talk_lock:
            # Ack tone — tells operator we heard the button press.
            if self._speaker is not None and self._speaker.is_open:
                try:
                    await self._speaker.play_tone()
                except Exception as err:
                    LOGGER.warning("ack tone failed: %s", err)

            self._last_input_at = time.monotonic()
            while not self._cancel.is_set():
                try:
                    user_text = await self._voice.turn(self._dispatch_tool)
                except Exception as err:
                    LOGGER.exception("voice turn failed: %s", err)
                    break

                if user_text:
                    self._last_input_at = time.monotonic()
                    self._check_conv_mode_signals(user_text)

                if not self._conv_mode:
                    break
                idle = time.monotonic() - self._last_input_at
                if idle > self.cfg.conversation.conversation_idle_timeout_s:
                    LOGGER.info(
                        "Conversation idle %ds — exiting conversation mode",
                        int(idle),
                    )
                    self._conv_mode = False
                    break

    # Word-boundary regexes — substring match trips on "we stopped at..." etc.
    _CONV_ENTER_RE = re.compile(
        r"\b(let's chat|let's talk|keep talking|conversation mode)\b",
        re.IGNORECASE,
    )
    _CONV_EXIT_RE = re.compile(
        r"\b(stop|done|goodbye|thanks tolly|thank you tolly|exit conversation)\b",
        re.IGNORECASE,
    )

    def _check_conv_mode_signals(self, user_text: str) -> None:
        text = user_text.strip()
        enter = self._CONV_ENTER_RE.search(text)
        if enter:
            self._conv_mode = True
            LOGGER.info("conv-mode -> True (match=%r in '%s')", enter.group(0), text[:60])
            return
        if not self._conv_mode:
            return
        exit_m = self._CONV_EXIT_RE.search(text)
        # Require the exit trigger to be in the LAST sentence — protects
        # against false positives like "we'll stop at the fuel dock first".
        if exit_m:
            last_sentence = re.split(r"[.!?]+", text)[-1].strip() or text
            if self._CONV_EXIT_RE.search(last_sentence):
                self._conv_mode = False
                LOGGER.info("conv-mode -> False (match=%r in '%s')",
                            exit_m.group(0), text[:60])

    # -------- tool dispatch --------

    async def _dispatch_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self._ha is not None
        return await dispatch_tool(
            name, args, self._ha,
            self.cfg.entities.include_patterns,
            self.cfg.entities.exclude_patterns,
            healthz_provider=self._healthz,
            sk=self._sk, router=self._router, opencpn=self._opencpn,
        )

    # -------- healthz --------

    async def _healthz(self) -> dict[str, Any]:
        ha_ok = await self._ha.ping() if self._ha is not None else False
        claude_ok = await self._llm.ping() if self._llm is not None else False

        stt_ready = bool(self._stt and self._stt.is_loaded) if self._stt else False
        tts_ready = bool(self._tts and self._tts.is_loaded) if self._tts else False

        exposed_count = 0
        if self._ha is not None and ha_ok:
            try:
                states = await fetch_exposed_states(
                    self._ha,
                    self.cfg.entities.include_patterns,
                    self.cfg.entities.exclude_patterns,
                )
                exposed_count = len(states)
            except Exception:
                exposed_count = 0

        mic_ok = bool(self._mic and self._mic.is_open)
        spk_ok = bool(self._speaker and self._speaker.is_open)
        healthy = (ha_ok and claude_ok and stt_ready and tts_ready
                   and mic_ok and spk_ok)

        out: dict[str, Any] = {
            "healthy": healthy,
            "mic": mic_ok,
            "speaker": spk_ok,
            "ha_reachable": ha_ok,
            "claude_reachable": claude_ok,
            "model": self.cfg.claude.model,
            "model_valid": claude_ok,
            "stt_ready": stt_ready,
            "tts_ready": tts_ready,
            "exposed_entity_count": exposed_count,
            "conv_mode_active": bool(self._conv_mode),
            "uptime_s": int(time.monotonic() - self._start_time),
        }
        self._health = out
        return out

    async def _health_loop(self) -> None:
        interval = self.cfg.connectivity.health_check_interval_s
        while not self._cancel.is_set():
            try:
                await self._healthz()
            except Exception as err:
                LOGGER.debug("health refresh failed: %s", err)
            await asyncio.sleep(interval)


async def _async_main() -> None:
    cfg_path_env = os.environ.get("BOAT_VOICE_CONFIG")
    cfg_path = Path(cfg_path_env) if cfg_path_env else None
    cfg = load_config(cfg_path) if cfg_path else load_config()
    orch = Orchestrator(cfg)
    runner = await orch.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await orch.stop()


def run() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass

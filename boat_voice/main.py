"""Top-level orchestrator: glue config, audio, Gemini, HA, server together."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from .audio import MicStream, SpeakerSink
from .config import Config, load_config
from .gemini import GeminiLiveSession
from .ha_api import HAClient
from .opencpn_rest import OpenCPNRestClient
from .prompts import build_system_prompt
from .router_client import RouterClient
from .server import build_app
from .sk_api import SKClient
from .tools import (
    build_entity_cheatsheet,
    dispatch_tool,
    fetch_exposed_states,
    get_tool_declarations,
)


LOGGER = logging.getLogger("boat_voice")

READY_FLAG_PATH = Path("/tmp/boat-voice.ready")


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    # systemd-journald captures stderr cleanly; keep format minimal there.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    # Avoid duplicate handlers on re-init.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)


class Orchestrator:
    """The whole boat-voice runtime: lifecycle, talk loop, healthz."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._http: aiohttp.ClientSession | None = None
        self._ha: HAClient | None = None
        self._sk: SKClient | None = None
        self._router: RouterClient | None = None
        self._opencpn: OpenCPNRestClient | None = None
        self._session: GeminiLiveSession | None = None
        self._mic: MicStream | None = None
        self._speaker: SpeakerSink | None = None
        self._talk_lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._start_time = time.monotonic()
        self._last_input_at = time.monotonic()
        # Health cache, refreshed by background loop + on each /healthz call.
        self._health: dict[str, Any] = {}
        # Model that was actually accepted at startup (may differ from cfg if fallback fired).
        self._effective_model = cfg.gemini.model
        # Entity cheatsheet used for the live system prompt.
        self._entity_cheatsheet = "(not loaded)"

    # -------- lifecycle --------

    async def start(self) -> web.AppRunner:
        _configure_logging(self.cfg.logging.level)
        LOGGER.info("boat-voice starting (model=%s)", self.cfg.gemini.model)

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
            self.cfg.router.url,
            self._http,
            timeout_s=self.cfg.router.timeout_s,
        )
        router_ok = await self._router.ping()
        LOGGER.info(
            "tolly-router %s: %s",
            self.cfg.router.url,
            "ready" if router_ok else "NOT ready (graph not loaded or down)",
        )

        try:
            self._opencpn = OpenCPNRestClient.from_credentials_file(
                self.cfg.opencpn.rest_credentials_path, self._http
            )
            opencpn_ok = await self._opencpn.ping()
            LOGGER.info(
                "OpenCPN REST %s: %s",
                self._opencpn.url,
                "ready" if opencpn_ok else "NOT ready (paired? or OpenCPN down)",
            )
        except FileNotFoundError as err:
            LOGGER.warning(
                "OpenCPN REST credentials missing (%s); chart push disabled", err
            )
            self._opencpn = None

        # Verify Gemini model exists; fall back if needed.
        await self._verify_model()

        # Open audio devices (probe + start mic capture).
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

        # Cache entity cheatsheet for system prompt.
        await self._refresh_entity_cheatsheet()

        # Build the Gemini session (not connected yet — opens lazily on first /talk).
        self._session = self._build_session()

        # Default conversation-mode state.
        if self._session is not None:
            self._session.set_conversation_mode(
                self.cfg.conversation.conversation_mode_default
            )

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

        # Background health refresher.
        asyncio.create_task(self._health_loop(), name="health_loop")

        # Write the ready flag (Phase H §41).
        try:
            READY_FLAG_PATH.write_text(str(int(time.time())))
        except Exception as err:
            LOGGER.warning("Could not write ready flag: %s", err)

        return runner

    async def stop(self) -> None:
        self._cancel.set()
        if self._session is not None:
            await self._session.close()
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

    # -------- session / model --------

    def _build_session(self) -> GeminiLiveSession:
        system_prompt = build_system_prompt(
            self._entity_cheatsheet, self.cfg.home_port.name
        )
        tool_decls = get_tool_declarations(self._entity_cheatsheet)
        return GeminiLiveSession(
            api_key=self.cfg.gemini.api_key,
            model=self._effective_model,
            voice=self.cfg.gemini.voice,
            system_prompt=system_prompt,
            tool_declarations=tool_decls,
            tool_dispatcher=self._dispatch_tool,
            input_sample_rate=self.cfg.audio.sample_rate_in,
            silence_end_ms=self.cfg.conversation.silence_end_ms,
            start_sensitivity=self.cfg.conversation.start_sensitivity,
            end_sensitivity=self.cfg.conversation.end_sensitivity,
            thinking_level=self.cfg.gemini.thinking_level,
            on_conversation_mode_change=self._sync_conv_mode_to_ha,
        )

    async def _sync_conv_mode_to_ha(self, active: bool) -> None:
        """When Gemini toggles conv-mode internally, mirror it to the HA toggle.

        HA's state-change trigger doesn't fire when turn_on/off is called on an
        already-matching state, so this won't loop back through the automation.
        """
        if self._ha is None:
            return
        service = "turn_on" if active else "turn_off"
        try:
            await self._ha.call_service(
                "input_boolean", service,
                {"entity_id": "input_boolean.tolly_conversation_mode"},
            )
            LOGGER.info("HA input_boolean.tolly_conversation_mode -> %s", active)
        except Exception as err:
            LOGGER.warning("conv-mode sync to HA failed: %s", err)

    async def _verify_model(self) -> None:
        """Confirm configured model exists, fall back to cfg.gemini.fallback_model if not."""
        assert self._http is not None
        cfg = self.cfg.gemini
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"?key={cfg.api_key}"
        )
        try:
            async with self._http.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as err:
            LOGGER.warning(
                "Model verification failed (network?): %s — continuing with %s",
                err, cfg.model,
            )
            self._effective_model = cfg.model
            return

        live_models = [
            m["name"].split("/")[-1]
            for m in data.get("models", [])
            if "bidiGenerateContent" in m.get("supportedGenerationMethods", [])
        ]
        wanted = cfg.model
        if wanted in live_models:
            LOGGER.info("Gemini model %s verified.", wanted)
            self._effective_model = wanted
            return
        LOGGER.error(
            "Configured model %s is NOT available on Gemini Live. Available: %s",
            wanted, live_models,
        )
        if cfg.fallback_model in live_models:
            LOGGER.warning("Falling back to %s", cfg.fallback_model)
            self._effective_model = cfg.fallback_model
            await self._notify_ha(
                "boat-voice model fallback",
                f"Configured model '{wanted}' missing; using '{cfg.fallback_model}' instead.",
            )
            return
        LOGGER.error(
            "Fallback %s is also missing; will use %s anyway and let connect fail loudly.",
            cfg.fallback_model, wanted,
        )
        self._effective_model = wanted
        await self._notify_ha(
            "boat-voice model unavailable",
            f"Model '{wanted}' and fallback '{cfg.fallback_model}' both missing.",
        )

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
        # Fire-and-forget: HA's REST command is non-blocking; the actual talk
        # runs in a background task so the HTTP response returns immediately.
        asyncio.create_task(self._talk_session(), name="talk_session")

    async def _on_conversation_mode(self, active: bool) -> None:
        if self._session is not None:
            self._session.set_conversation_mode(active)
        LOGGER.info("conversation_mode -> %s (external)", active)
        if active and not self._talk_lock.locked():
            # Kick off a session so the user can just start talking.
            asyncio.create_task(self._talk_session(), name="talk_session_conv")

    async def _talk_session(self) -> None:
        """Run one or more turns under the talk lock."""
        if self._session is None:
            LOGGER.warning("No session built; ignoring /talk")
            return
        if self._talk_lock.locked():
            LOGGER.info("/talk arrived while previous talk still running; ignored")
            return

        async with self._talk_lock:
            if not self._session.connected:
                try:
                    await self._session.connect()
                except Exception as err:
                    LOGGER.error("Gemini connect failed: %s", err)
                    return

            # Ack tone: tells the user Gemini is listening.
            # Fires once per /talk session (not per turn in conversation mode).
            if self._speaker is not None:
                try:
                    await self._speaker.play_tone()
                except Exception as err:
                    LOGGER.warning("ack tone failed: %s", err)

            self._last_input_at = time.monotonic()
            while not self._cancel.is_set():
                ok = await self._run_one_turn()
                if not ok:
                    break
                if self._session.last_input_transcription:
                    self._last_input_at = time.monotonic()
                if not self._session.conversation_mode:
                    break
                idle = time.monotonic() - self._last_input_at
                if idle > self.cfg.conversation.conversation_idle_timeout_s:
                    LOGGER.info(
                        "Conversation idle %ds — exiting conversation mode", int(idle)
                    )
                    self._session.set_conversation_mode(False)
                    break

    async def _run_one_turn(self) -> bool:
        """Stream mic to Gemini, stream Gemini's audio to the speaker as it arrives."""
        assert self._session is not None and self._mic is not None
        assert self._speaker is not None
        self._mic.drain()
        self._session.clear_turn_complete()

        playback = self._speaker.start_stream()
        first_chunk = False

        def _on_audio_chunk(pcm: bytes) -> None:
            nonlocal first_chunk
            if not first_chunk:
                first_chunk = True
                if self.cfg.audio.mute_mic_during_playback and self._mic is not None:
                    self._mic.set_muted(True)
            playback.feed_nowait(pcm)

        self._session.set_audio_chunk_handler(_on_audio_chunk)

        receive_task = asyncio.create_task(
            self._session.receive_loop(), name="gemini_receive"
        )

        async def _mic_forward() -> None:
            async for chunk in self._mic.chunks():
                if self._session is None or self._session.responding:
                    break
                if not self._session.connected:
                    break
                try:
                    await self._session.send_audio(chunk)
                except Exception as err:
                    LOGGER.warning("Mic forward stopped: %s", err)
                    break

        mic_task = asyncio.create_task(_mic_forward(), name="mic_forward")

        try:
            await self._session.wait_for_turn_complete(timeout=60)
        finally:
            mic_task.cancel()
            try:
                await mic_task
            except (asyncio.CancelledError, Exception):
                pass
            if not receive_task.done():
                receive_task.cancel()
                try:
                    await receive_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._session.set_audio_chunk_handler(None)

        try:
            await playback.finish()
        finally:
            if self.cfg.audio.mute_mic_during_playback and self._mic is not None:
                self._mic.set_muted(False)
        return True

    # -------- tool dispatch --------

    async def _dispatch_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self._ha is not None
        return await dispatch_tool(
            name,
            args,
            self._ha,
            self.cfg.entities.include_patterns,
            self.cfg.entities.exclude_patterns,
            healthz_provider=self._healthz,
            sk=self._sk,
            router=self._router,
            opencpn=self._opencpn,
        )

    # -------- healthz --------

    async def _healthz(self) -> dict[str, Any]:
        ha_ok = await self._ha.ping() if self._ha is not None else False

        gemini_ok = False
        model_valid = False
        try:
            assert self._http is not None
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models"
                f"?key={self.cfg.gemini.api_key}"
            )
            async with self._http.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                gemini_ok = resp.status == 200
                if gemini_ok:
                    data = await resp.json()
                    live = [
                        m["name"].split("/")[-1]
                        for m in data.get("models", [])
                        if "bidiGenerateContent" in m.get("supportedGenerationMethods", [])
                    ]
                    model_valid = self._effective_model in live
        except Exception:
            gemini_ok = False

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
        healthy = ha_ok and gemini_ok and model_valid and mic_ok and spk_ok

        out: dict[str, Any] = {
            "healthy": healthy,
            "mic": mic_ok,
            "speaker": spk_ok,
            "ha_reachable": ha_ok,
            "gemini_reachable": gemini_ok,
            "model": self._effective_model,
            "model_valid": model_valid,
            "exposed_entity_count": exposed_count,
            "conv_mode_active": bool(
                self._session and self._session.conversation_mode
            ),
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

    # -------- HA notifications --------

    async def _notify_ha(self, title: str, message: str) -> None:
        if self._ha is None:
            return
        try:
            await self._ha.call_service(
                "persistent_notification",
                "create",
                {"title": title, "message": message},
            )
        except Exception as err:
            LOGGER.debug("HA notify failed: %s", err)


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

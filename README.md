# boat-voice — Tolly

Voice assistant for a small boat.  Push-to-talk button in Home Assistant →
speak into the laptop mic → response comes out the speakers.  Built for a
1974 Tollycraft 34 but the architecture is generic for any boat with
Signal K + HA + OpenCPN.

```
mic 16 kHz → webrtcvad capture
           → Whisper STT (local, faster-whisper)
           → Claude (cloud, with tool calling)
           → tool dispatch (HA / SK / OpenCPN / marine-router)
           → Claude (second round with tool results)
           → Piper TTS (local, en_US-amy-medium)
           → speaker 24 kHz
```

All speech recognition and synthesis runs on the boat-laptop's CPU.  The
only cloud dependency is the Claude API for the LLM brain.  When the boat
is offline at sea, the local stack still functions but Claude turns fail
gracefully ("I'm offline right now") rather than hanging.

## What Tolly can do

- Read live state from Home Assistant (boat sensors, switches, depth, wind).
- Read tide + tidal-current predictions cached locally from NOAA (works
  offline at sea).
- Plan chart-aware routes via [marine-router][marine-router] — pass a
  destination by NAME, the router resolves it against a 46-entry Salish
  Sea gazetteer + your saved Signal K waypoints + OpenStreetMap fallback.
- Multi-leg routes with via points ("plan a route to Roche Harbor with a
  stop at Sucia").
- Departure-window optimization ("when should we leave for Friday Harbor
  today?") — sweeps candidate departures and picks the best vs tides.
- Automatic Swinomish channel exit selection based on tidal current
  direction at departure time.
- Read/write Signal K waypoints and routes (`CreateWaypoint`,
  `ListWaypoints`, `CreateRoute`, etc.).
- Push planned routes into OpenCPN's Route & Mark Manager via OpenCPN's
  REST RemoteControl, so what Tolly plans appears immediately on the
  chart for visual review.
- Control HA entities (`HassTurnOn`, `HassTurnOff`).

[marine-router]: https://github.com/cbc76am-hue/marine-router

## Quick reference

```bash
# is it running?
systemctl --user status boat-voice

# live logs
journalctl --user -u boat-voice -f

# restart after editing ~/.config/boat-voice/config.yaml
systemctl --user restart boat-voice

# health
curl http://127.0.0.1:8765/healthz | jq

# fire a one-shot turn
curl -X POST http://127.0.0.1:8765/talk

# what audio devices does PortAudio see?
./venv/bin/python -m boat_voice.devices_dump
```

## Layout

```
boat-voice/
├── pyproject.toml
├── venv/                   # virtualenv (gitignored)
├── scripts/
│   └── sync_routes_to_opencpn.py  # re-sync SK routes -> OpenCPN if a push failed
└── boat_voice/
    ├── __main__.py         # entry: python -m boat_voice
    ├── main.py             # orchestrator (lifecycle, talk loop, healthz)
    ├── server.py           # aiohttp /talk /conversation_mode /healthz
    ├── voice_session.py    # per-turn pipeline: VAD → STT → LLM → TTS → playback
    ├── stt.py              # Whisper wrapper (faster-whisper)
    ├── tts.py              # Piper wrapper (auto-downloads default voice)
    ├── claude_llm.py       # anthropic SDK wrapper, tool loop, history mgmt
    ├── audio.py            # sounddevice mic + speaker
    ├── tools.py            # tool declarations + dispatch (LLM-agnostic)
    ├── ha_api.py           # async HA REST client
    ├── sk_api.py           # async Signal K REST + WS delta publisher
    ├── router_client.py    # async marine-router HTTP client
    ├── opencpn_rest.py     # async OpenCPN REST RemoteControl client
    ├── prompts.py          # Tolly system prompt
    ├── config.py           # YAML loader
    └── devices_dump.py     # debug helper for PortAudio device discovery
```

## Configuration

Copy `config.example.yaml` to `~/.config/boat-voice/config.yaml` (mode 600),
fill in:

- `claude.api_key_path` — path to a file (mode 600) containing your
  Anthropic API key.  Get one at console.anthropic.com.
- `ha.long_lived_token` — Home Assistant long-lived access token.
- `sk.token_path` — Signal K JWT.  Generate via
  `signalk-generate-token -u <user> -e 10y -s ~/.signalk/security.json`.
- `opencpn.rest_credentials_path` — OpenCPN REST pairing JSON
  (`{url, source, apikey}`).  Created via OpenCPN's PIN-pairing
  handshake (see comments in `config.example.yaml`).

`whisper.model_size` defaults to `small.en` (244 MB, downloads on first
run, accurate on marine vocabulary).  `base.en` is faster but mangles
place names ("Mukilteo" → "Muppa Tio") — not recommended.

`piper.voice_path` empty triggers auto-download of `en_US-amy-medium`
(~63 MB) to `~/.config/boat-voice/piper-voice/` on first run.

## First-run sequence on a fresh boat

1. Install Signal K Server (separate project; runs on `:3000`).
2. Install Home Assistant (separate project; runs on `:8123`).
3. Install OpenCPN 5.14.0+ with REST RemoteControl enabled.
4. Clone + build the [marine-router][marine-router].
5. Clone this repo, create venv, `pip install -e .`.
6. Copy `config.example.yaml`, fill in tokens.
7. Drop the systemd unit file (`deploy/systemd/boat-voice.service`) into
   `~/.config/systemd/user/` and `systemctl --user enable --now boat-voice`.
8. Wire HA's "Tolly" dashboard to POST `/talk` on a button press.

The deep version of this checklist lives in `/home/boat/NOTES.md` (boat-
specific) and a sanitized version in `deploy/` (this repo).

## Caveats

- Per-turn latency is 2-3 s for chat, 5-10 s for route planning.  The
  router's chart-aware A* dominates route latency — there's a parallel
  worker pool on the router side that helps for `depart_window` mode.
- Whisper hallucinates on noisy input.  Initial-prompt biasing toward
  marine vocabulary helps a lot but doesn't eliminate it.
- All planned routes are **drafts** — Tolly never sends to autopilot.
  Human verification on the chart is required before navigating.
- Coverage is the Salish Sea (Puget Sound + San Juans).  Other regions
  need new gazetteer entries + an ENC chart rebuild on the router side.

## License

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

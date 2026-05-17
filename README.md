# boat-voice — Tolly

Standalone Python service giving the boat a voice assistant called **Tolly**.
Press a button in Home Assistant → speak into the laptop mic → Gemini Live
responds through the laptop speakers.

Tools available to Tolly:

- Read live boat state from Home Assistant (sensors, switches, etc.).
- Read/write Signal K waypoints and routes (`CreateWaypoint`, `ListWaypoints`,
  `DeleteWaypoint`, `CreateRoute`, `ListRoutes`, `DeleteRoute`,
  `GetCurrentPosition`).
- Plan chart-aware routes by calling out to
  [marine-router](https://github.com/cbc76am-hue/marine-router) — Tolly
  resolves a named destination ("Friday Harbor") to coordinates and asks
  the router for a navigable polyline (`PlanRoute`).
- Push waypoints + routes into OpenCPN's Route & Mark Manager via OpenCPN's
  REST RemoteControl API, so what Tolly creates appears immediately on the
  chart for visual review.

## Quick reference

```bash
# is it running?
systemctl --user status boat-voice

# live logs
journalctl --user -u boat-voice -f

# restart after editing ~/.config/boat-voice/config.yaml
systemctl --user restart boat-voice

# what's the health state?
curl http://127.0.0.1:8765/healthz | jq

# fire a one-shot turn
curl -X POST http://127.0.0.1:8765/talk

# what audio devices does PortAudio see?
./venv/bin/python -m boat_voice.devices_dump
```

## Layout

```
boat-voice/
├── pyproject.toml          # deps: google-genai, sounddevice, aiohttp, pyyaml, numpy
├── venv/                   # virtualenv (gitignored)
└── boat_voice/
    ├── __main__.py         # entry: python -m boat_voice
    ├── main.py             # orchestrator (lifecycle, talk loop, healthz)
    ├── server.py           # aiohttp /talk /conversation_mode /healthz
    ├── gemini.py           # GeminiLiveSession (WSS, tools, session_resumption)
    ├── audio.py            # sounddevice mic + speaker
    ├── tools.py            # tool declarations + dispatch
    ├── ha_api.py           # async HA REST client
    ├── sk_api.py           # async Signal K REST + WS delta publisher
    ├── router_client.py    # async marine-router client + error humanizer
    ├── opencpn_rest.py     # async OpenCPN REST RemoteControl client
    ├── prompts.py          # Tolly system prompt
    ├── config.py           # YAML loader
    ├── noaa.py             # v1.1 stub
    └── devices_dump.py     # debug helper
```

## Config

`~/.config/boat-voice/config.yaml` (mode 600, owner `boat`). Contains the
Gemini API key and HA long-lived token inline. Don't commit it.

Sibling secret files in the same directory:

- `sk-token` — Signal K JWT for waypoint/route reads + writes. Generate via
  `signalk-generate-token -u <user> -e 10y -s ~/.signalk/security.json`.
- `opencpn-rest.json` — OpenCPN REST RemoteControl pairing artifact
  (`{url, source, apikey}`). Created via the PIN-pairing flow on first
  ping; see [the OpenCPN REST docs][1] for the pairing handshake.

[1]: http://opencpn.github.io/OpenCPN/api-docs/classAbstractRestServer.html

## v1 caveats

- No NOAA tides/weather direct API yet — Google Search grounding covers it.
- No wake word ("Hey Tolly") — push-to-talk via HA button only.
- No local-LLM fallback — internet down = Tolly down.
- One audio output target (the laptop). Multi-room is v1.1+.
- PlanRoute coverage is Puget Sound + San Juans (per marine-router's scope).
- LLM-generated routes are drafts — Tolly never sends to autopilot. Human
  reviews on the chart before navigating from any route.

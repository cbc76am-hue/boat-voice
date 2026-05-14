"""aiohttp HTTP server exposing /talk, /conversation_mode, /healthz."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from aiohttp import web


LOGGER = logging.getLogger(__name__)


HealthzProvider = Callable[[], Awaitable[dict]]


def build_app(
    on_talk: Callable[[], Awaitable[None]],
    on_conversation_mode: Callable[[bool], Awaitable[None]],
    healthz_provider: HealthzProvider,
) -> web.Application:
    app = web.Application()

    async def talk(_request: web.Request) -> web.Response:
        try:
            await on_talk()
        except Exception as err:
            LOGGER.exception("/talk failed")
            return web.json_response({"ok": False, "error": str(err)}, status=500)
        return web.json_response({"ok": True})

    async def conversation_mode(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            data = {}
        active = bool(data.get("active", False))
        try:
            await on_conversation_mode(active)
        except Exception as err:
            LOGGER.exception("/conversation_mode failed")
            return web.json_response({"ok": False, "error": str(err)}, status=500)
        return web.json_response({"ok": True, "active": active})

    async def healthz(_request: web.Request) -> web.Response:
        health = await healthz_provider()
        status = 200 if health.get("healthy") else 503
        return web.json_response(health, status=status)

    app.router.add_post("/talk", talk)
    app.router.add_post("/conversation_mode", conversation_mode)
    app.router.add_get("/healthz", healthz)
    return app

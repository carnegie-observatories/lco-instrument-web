"""The instrument's control WebSocket, consumed as a read-only client.

Speaks the instrument protocol (docs/plans/ws-migration-plan.md § WebSocket
protocol): JSON frames with a ``type`` discriminator. The bridge subscribes to
the ``exposure`` and ``readout`` topics (they feed the status channel — events
are broadcast to every client regardless) and waits for the
``exposure_complete`` event, whose ``fits_path`` (local absolute, by contract)
is handed to the frame source.

The bridge sends **no ``cmd`` frames, ever** — it is a read-only presence on
the control WS, the way gcamweb never touches gcam's command port.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import aiohttp
from aiohttp import WSMsgType

log = logging.getLogger("imageweb.control")

TOPICS = ["exposure", "readout"]
RETRY_MIN_S = 2.0
RETRY_MAX_S = 30.0


class ControlClient:
    """One instrument's control WS: reconnecting reader, topic cache, event hook."""

    def __init__(self, name: str, host: str, port: int,
                 on_image: Callable[[dict], Awaitable[None] | None]):
        self.name, self.host, self.port = name, host, port
        self.on_image = on_image
        self.state = "connecting"
        self.app: str | None = None
        self.version: str | None = None
        self.topics: dict[str, dict] = {}   # latest `state` per subscribed topic
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        return {
            "control": self.state,
            "app": self.app,
            "version": self.version,
            "exposure": self.topics.get("exposure"),
            "readout": self.topics.get("readout"),
        }

    def _set_state(self, state: str) -> None:
        if state != self.state:
            log.info("%s: control ws://%s:%d %s", self.name, self.host, self.port, state)
            self.state = state

    # -- the reader -----------------------------------------------------------

    async def _handle(self, m: dict) -> None:
        t = m.get("type")
        if t == "hello":
            self.app = m.get("app")
            self.version = m.get("version")
            self._set_state("connected")
        elif t == "state":
            self.topics[m.get("topic", "")] = m.get("data") or {}
        elif t == "event" and m.get("name") == "exposure_complete":
            data = m.get("data") or {}
            log.info("%s: exposure_complete %s", self.name, data)
            r = self.on_image(data)
            if asyncio.iscoroutine(r):
                await r
        # acks and other events are none of the bridge's business

    async def _run(self) -> None:
        delay = RETRY_MIN_S
        assert self._session is not None
        while True:
            try:
                async with self._session.ws_connect(
                        f"ws://{self.host}:{self.port}/", heartbeat=20, max_msg_size=0) as ws:
                    self._set_state("waiting for hello")
                    await ws.send_json({"type": "subscribe", "topics": TOPICS})
                    delay = RETRY_MIN_S
                    async for msg in ws:
                        if msg.type != WSMsgType.TEXT:
                            continue
                        try:
                            m = msg.json()
                        except ValueError:
                            continue
                        await self._handle(m)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError) as e:
                self._set_state(f"unreachable ({e.__class__.__name__})")
            else:
                self._set_state("disconnected")
            self.topics.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_S)

    def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

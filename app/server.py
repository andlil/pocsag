"""Web server: the page, a WebSocket for live messages and settings, JSON API.

WebSocket /api/ws (JSON text messages)
  server -> browser
    {"type": "hello", "config": {...}, "status": {...}, "messages": [...]}
    {"type": "message", "message": {...}}       a page was decoded
    {"type": "status", ...}                     receiver state, twice a second
    {"type": "config", "config": {...}}         settings changed
    {"type": "error", "message": "..."}         a request was refused
  browser -> server
    {"type": "config", "changes": {"freq": 169800000, "charset": "sv"}}

GET /api/messages?limit=N   latest N messages, oldest first
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web

from .receiver import Receiver
from .sources import make_source
from .store import Config, ConfigError, MessageLog

log = logging.getLogger("pocsag")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
HISTORY_SENT = 500


def env(name, default):
    return os.environ.get(name, default)


def client_address(request):
    return request.headers.get("X-Forwarded-For", request.remote or "?").split(",")[0].strip()


class App:
    def __init__(self):
        data = env("DATA_DIR", "/data")
        self.config = Config(data)
        self.messages = MessageLog(data)
        self.clients = set()
        self.loop = None
        self.status = {"type": "status", "state": "starting", "freq": self.config["freq"]}
        self.receiver = Receiver(
            lambda: make_source(env("SDR_DEVICE", ""), env("SDR_GAIN", "auto"),
                                float(env("SDR_PPM", "0"))),
            self.config["freq"],
            on_message=lambda m: self.loop.call_soon_threadsafe(self._on_message, m),
            on_status=lambda s: self.loop.call_soon_threadsafe(self._on_status, s),
        )

    async def startup(self, _app):
        self.loop = asyncio.get_running_loop()
        self.receiver.begin()

    async def shutdown(self, _app):
        await asyncio.gather(*(ws.close() for ws in list(self.clients)), return_exceptions=True)
        await asyncio.to_thread(self.receiver.shutdown)

    # ---- outgoing ----------------------------------------------------------------

    def _broadcast(self, msg):
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(self._send(ws, data))

    @staticmethod
    async def _send(ws, data):
        try:
            await ws.send_str(data)
        except (ConnectionError, RuntimeError):
            pass                            # closed; the handler cleans up

    def _on_status(self, status):
        self.status = status
        self._broadcast(status)

    def _on_message(self, msg):
        msg = self.messages.add(msg)
        log.info("RIC %d/%s %s: %s", msg["ric"], "ABCD"[msg["func"]], msg["type"], msg["text"])
        self._broadcast({"type": "message", "message": msg})

    # ---- incoming ----------------------------------------------------------------

    def _on_request(self, ws, raw):
        try:
            req = json.loads(raw)
            if req.get("type") != "config" or not isinstance(req.get("changes"), dict):
                raise ConfigError(f"unknown request {req.get('type')!r}")
            old_freq = self.config["freq"]
            new = self.config.update(req["changes"], self.receiver.check_frequency)
            if new["freq"] != old_freq:
                log.info("frequency set to %.4f MHz", new["freq"] / 1e6)
                self.receiver.set_frequency(new["freq"])
            self._broadcast({"type": "config", "config": new})
        except (ConfigError, ValueError, TypeError) as e:
            asyncio.ensure_future(self._send(ws, json.dumps({"type": "error", "message": str(e)})))

    async def websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=20)    # keeps proxies from timing out
        await ws.prepare(request)
        who = client_address(request)
        log.info("%s: connected", who)
        self.clients.add(ws)
        await ws.send_str(json.dumps({
            "type": "hello", "config": self.config.values, "status": self.status,
            "messages": self.messages.latest(HISTORY_SENT),
        }, ensure_ascii=False))
        try:
            async for m in ws:
                if m.type == web.WSMsgType.TEXT:
                    self._on_request(ws, m.data)
        finally:
            self.clients.discard(ws)
            log.info("%s: disconnected", who)
        return ws

    async def api_messages(self, request):
        try:
            limit = max(1, min(int(request.query.get("limit", "100")), 2000))
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a number") from None
        return web.json_response(self.messages.latest(limit),
                                 dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def index(_request):
    return web.FileResponse(WEB_DIR / "index.html")


def main():
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = App()
    web_app = web.Application()
    web_app.on_startup.append(app.startup)
    web_app.on_shutdown.append(app.shutdown)
    web_app.router.add_get("/", index)
    web_app.router.add_get("/api/ws", app.websocket)
    web_app.router.add_get("/api/messages", app.api_messages)
    web_app.router.add_static("/static/", WEB_DIR)
    web.run_app(web_app, host=env("HOST", "0.0.0.0"), port=int(env("PORT", "8080")),
                access_log=None)


if __name__ == "__main__":
    main()

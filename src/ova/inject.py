#!/usr/bin/env python3
"""External text / wake entrance for the voice dialogue (stdlib only).

The microphone path is 唤醒 -> 录音(VAD) -> ASR -> ``_playback_from_text``. A
showroom keyboard (or any local tool) can enter the same path through two HTTP
endpoints served by the ``ova-wake`` process:

    POST /inject  {"text": "...", "lang": "zh"}
        The text counts as a recognized utterance: stop / continue / showroom
        intro / normal question, all decided by the existing routing. Audio
        that is currently playing is stopped first, exactly like a barge-in.
        Answers with the routing verdict ``{"ok", "routed", "detail"}``.
    POST /wake    {}
        Counts as one wake-word hit: a normal dialogue round starts
        (listen -> ASR -> answer). Answers ``{"ok": true}``.

The listener is a stdlib ``ThreadingHTTPServer`` on a daemon thread. It binds
loopback by default (``WAKE_INJECT_HOST`` / ``WAKE_INJECT_PORT``; port 0 turns
the entrance off) and adds no third-party dependency.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

from ova.config import svc_event
from ova.dialogue import inject_text, trigger_wake

LOG = logging.getLogger("ova.inject")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090


class InjectHandler(BaseHTTPRequestHandler):
    """``POST /inject`` and ``POST /wake``; anything else is 404."""

    server_version = "ova-inject/1.0"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_json()
            if path == "/inject":
                self._inject(body)
            elif path == "/wake":
                trigger_wake()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": f"unknown path {path}"})
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the service
            LOG.error("INJECT_REQUEST_ERROR %s: %s", type(exc).__name__, exc)
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _inject(self, body: dict) -> None:
        text = str(body.get("text") or "").strip()
        if not text:
            self._json(400, {"ok": False, "error": "text is required"})
            return
        raw_lang = body.get("lang")
        lang = str(raw_lang).strip() if raw_lang is not None else None
        report = inject_text(text, lang or None)
        self._json(200, {
            "ok": True,
            "routed": report.get("routed", "idle"),
            "detail": report.get("detail", ""),
        })

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"body must be UTF-8 JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        return body

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("INJECT_HTTP %s", fmt % args)


def make_server(host: str = DEFAULT_HOST,
                port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Build the server without serving (tests use port 0 for an ephemeral one)."""
    return ThreadingHTTPServer((host, port), InjectHandler)


def start_inject_server(cfg: dict) -> ThreadingHTTPServer | None:
    """Start the external entrance on a daemon thread; None when disabled."""
    host = str(cfg.get("inject_host", DEFAULT_HOST))
    port = int(cfg.get("inject_port", DEFAULT_PORT))
    if port <= 0:
        LOG.info("INJECT_DISABLED inject_port=%s", port)
        return None
    server = make_server(host, port)
    threading.Thread(target=server.serve_forever, name="ova-inject",
                     daemon=True).start()
    LOG.info("INJECT_READY host=%s port=%d", host, server.server_address[1])
    svc_event("system",
              f"外部文本入口已开启: http://{host}:{server.server_address[1]}",
              "info")
    return server
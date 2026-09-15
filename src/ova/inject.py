#!/usr/bin/env python3
"""External text / wake / language entrance for the dialogue (stdlib only).

The microphone path is 唤醒 -> 录音(VAD) -> ASR -> ``_playback_from_text``. A
showroom keyboard (or any local tool) can enter the same path through three HTTP
endpoints served by the ``ova-wake`` process:

    POST /inject  {"text": "...", "lang": "zh"}
        The text counts as a recognized utterance: stop / continue / showroom
        intro / normal question, all decided by the existing routing. Audio
        that is currently playing is stopped first, exactly like a barge-in.
        Answers with the routing verdict ``{"ok", "routed", "detail"}``.
    POST /wake    {}
        Counts as one wake-word hit: a normal dialogue round starts
        (listen -> ASR -> answer). Answers ``{"ok": true}``.
    POST /lang    {} / {"toggle": true} / {"lang": "en"}
        Switches the dialogue language of the whole conversation (knob
        long-press): the choice is stored in ``dialogue_lang_file`` and every
        later turn — intros and LLM answers — follows it. Answers
        ``{"ok", "lang", "previous"}``.
    GET  /lang    (no body)
        Reads the language currently in force: ``{"ok": true, "lang": "zh"}``.

The listener is a stdlib ``ThreadingHTTPServer`` on a daemon thread. It binds
loopback by default (``WAKE_INJECT_HOST`` / ``WAKE_INJECT_PORT``; port 0 turns
the entrance off) and adds no third-party dependency. The resolved config is
bound into the handler class by :func:`make_server` (no module global).
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

from ova.config import svc_event
from ova.dialogue import inject_text, trigger_wake
from ova import lang as ova_lang

LOG = logging.getLogger("ova.inject")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090


class InjectHandler(BaseHTTPRequestHandler):
    """``POST /inject``, ``POST /wake``, ``POST|GET /lang``; else 404.

    ``cfg`` is the resolved configuration of the ``ova-wake`` process, bound
    per server by :func:`make_server` (never a module-level global).
    """

    server_version = "ova-inject/1.0"
    cfg: dict = {}

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_json()
            if path == "/inject":
                self._inject(body)
            elif path == "/wake":
                trigger_wake()
                self._json(200, {"ok": True})
            elif path == "/lang":
                self._lang(body)
            else:
                self._json(404, {"ok": False, "error": f"unknown path {path}"})
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the service
            LOG.error("INJECT_REQUEST_ERROR %s: %s", type(exc).__name__, exc)
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/lang":
                self._json(200, {"ok": True, "lang": ova_lang.current(self.cfg)})
            else:
                self._json(404, {"ok": False, "error": f"unknown path {path}"})
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

    def _lang(self, body: dict) -> None:
        """Switch the conversation language: ``{}``/``{"toggle": true}`` flips,
        ``{"lang": "en"}`` sets it outright."""
        raw = body.get("lang")
        if raw is None:
            previous, new = ova_lang.toggle(self.cfg)
        else:
            previous, new = ova_lang.set_lang(self.cfg, raw)
        self._json(200, {"ok": True, "lang": new, "previous": previous})

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
                port: int = DEFAULT_PORT,
                cfg: dict | None = None) -> ThreadingHTTPServer:
    """Build the server without serving (tests use port 0 for an ephemeral one).

    ``cfg`` is bound into the handler class, so handlers read the same resolved
    configuration as the wake loop without touching a module-level global.
    """
    bound = dict(cfg or {})

    class BoundHandler(InjectHandler):
        cfg = bound

    return ThreadingHTTPServer((host, port), BoundHandler)


def start_inject_server(cfg: dict) -> ThreadingHTTPServer | None:
    """Start the external entrance on a daemon thread; None when disabled."""
    host = str(cfg.get("inject_host", DEFAULT_HOST))
    port = int(cfg.get("inject_port", DEFAULT_PORT))
    if port <= 0:
        LOG.info("INJECT_DISABLED inject_port=%s", port)
        return None
    server = make_server(host, port, cfg)
    threading.Thread(target=server.serve_forever, name="ova-inject",
                     daemon=True).start()
    LOG.info("INJECT_READY host=%s port=%d lang=%s", host,
             server.server_address[1], ova_lang.current(cfg))
    svc_event("system",
              f"外部文本入口已开启: http://{host}:{server.server_address[1]}",
              "info")
    return server
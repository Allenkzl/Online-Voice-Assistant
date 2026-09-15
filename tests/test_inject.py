#!/usr/bin/env python3
"""External inject entrance tests: routing, barge-in, HTTP — no hardware.

Nothing here needs a microphone, a speaker, the wake model or the network:
``aplay`` is a shell script on PATH that only logs its arguments and sleeps,
and the Hey-Jarvis barge-in monitor is patched out. The fake main loop stands
in for ``ova.wake.main()``, which cannot run without an audio device.

Run with pytest or directly:

    python3 tests/test_inject.py
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import types
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("OVA_HOME", str(ROOT))

# --- stub openwakeword (imported by ova.wake, unused by these tests) ---------
if "openwakeword" not in sys.modules:
    _oww = types.ModuleType("openwakeword")
    _oww_model = types.ModuleType("openwakeword.model")

    class _Model:  # noqa: D401 - minimal stand-in
        def __init__(self, *a, **k):
            pass

        def predict(self, *a, **k):
            return {}

        def reset(self):
            pass

    _oww_model.Model = _Model
    _oww.model = _oww_model
    sys.modules["openwakeword"] = _oww
    sys.modules["openwakeword.model"] = _oww_model

from ova import dialogue as dlg                                   # noqa: E402
from ova import inject as inj                                     # noqa: E402
from ova import lang as ova_lang                                  # noqa: E402
from ova import wake                                              # noqa: E402
from ova.dialogue import (                                        # noqa: E402
    InjectedTurn,
    PlaybackState,
    inject_text,
    play_interruptible,
)
from ova.engines.base import Reply                                # noqa: E402

ASSETS = ROOT / "assets"


# --- fakes ------------------------------------------------------------------

class FakeBackend:
    """No speaker: records the local prompt files it was asked to play."""

    output_device = "fake_sink"

    def __init__(self):
        self.played: list[Path] = []

    def play_file(self, path, timeout=None):
        self.played.append(Path(path))

    def played_names(self) -> list[str]:
        return [p.name for p in self.played]


class FakeEngine:
    """Records the turn it was asked to answer and returns a real WAV."""

    def __init__(self, needs_transcript=True):
        self.name = "fake"
        self.needs_transcript = needs_transcript
        self.calls: list[tuple] = []
        self.cfgs: list[dict] = []       # 每次 respond 拿到的 cfg（看 reply_lang）

    def respond(self, samples, text, cfg):
        self.calls.append((samples, text))
        self.cfgs.append(dict(cfg or {}))
        return Reply(audio_path=ROOT / "tests" / "asr_zh_smart_retail.wav",
                     text="好的。", transcript=text, timeout_s=42.0,
                     temporary=False, meta={"engine": self.name})


class FakeAsr:
    """Only exists so the lazy ASR load is skipped in tests."""

    def transcribe(self, samples):  # noqa: ARG002 - unused in these tests
        return "介绍智慧零售"


class FakeMainLoop:
    """Stand-in for the ova-wake idle loop, which needs a microphone.

    It repeats the two dispatch calls ``wake.main()`` makes when text arrives
    through the inject entrance.
    """

    def __init__(self, backend, root, cfg, engine, asr=None):
        self.backend, self.root, self.cfg = backend, root, cfg
        self.engine = engine
        self.asr = asr if asr is not None else FakeAsr()
        self.turns: list[InjectedTurn] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        dlg.INJECT.clear()
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=15)

    def _run(self) -> None:
        while not self._stop.is_set():
            turn = dlg.INJECT.take()
            if turn is None:
                time.sleep(0.02)
                continue
            self.asr, self.engine = wake.answer_injected_turn(
                self.backend, self.root, self.cfg, turn, self.asr, self.engine)
            self.turns.append(turn)

    def wait_turn(self, count: int = 1, timeout_s: float = 15.0) -> None:
        """Block until the loop finished ``count`` turns (audio included)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if len(self.turns) >= count:
                return
            time.sleep(0.02)
        raise AssertionError(f"main loop finished {len(self.turns)}/{count} turns")


# --- helpers ----------------------------------------------------------------

@contextlib.contextmanager
def patch(obj, **attrs):
    """Temporarily replace module attributes (keeps the real ones intact)."""
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def no_microphone():
    """Playback without the Hey-Jarvis monitor (no capture device here)."""
    with patch(dlg, _barge_monitor=lambda *a, **k: None,
               _get_barge_model=lambda *a, **k: object()):
        yield


@contextlib.contextmanager
def fake_aplay(duration_s: float = 1.2):
    """Put an ``aplay`` on PATH that logs its args and blocks; yields the log."""
    tmp = tempfile.TemporaryDirectory(prefix="ova_fake_aplay_")
    bindir = Path(tmp.name) / "bin"
    bindir.mkdir()
    log = Path(tmp.name) / "aplay.log"
    exe = bindir / "aplay"
    exe.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nsleep {duration_s}\n',
                   encoding="utf-8")
    exe.chmod(0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bindir}{os.pathsep}{old_path}"
    try:
        yield log
    finally:
        os.environ["PATH"] = old_path
        tmp.cleanup()


@contextlib.contextmanager
def inject_server(cfg: dict | None = None):
    """Serve the real handler on an ephemeral loopback port; yields the port.

    ``cfg`` is bound into the handler exactly like ``start_inject_server()``
    does, so ``/lang`` reads/writes the cfg of this test run.
    """
    server = inj.make_server("127.0.0.1", 0, cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@contextlib.contextmanager
def capture_logs():
    """Collect log messages of every ova logger."""
    messages: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record):  # noqa: D102 - logging API
            messages.append(record.getMessage())

    handler = _Handler()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield messages
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def post(port: int, path: str, payload: dict | None = None,
         raw: str | None = None) -> tuple[int, dict]:
    body = raw if raw is not None else json.dumps(payload or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("POST", path, body=body.encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        conn.close()


def get(port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        conn.close()


def free_port() -> int:
    """A currently free loopback port (used for the real main loop)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_wav(path: Path, seconds: float = 3.0, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


def aplay_lines(log: Path) -> list[str]:
    if not log.is_file():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def wait_for_aplay(log: Path, count: int, timeout_s: float = 5.0) -> list[str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        lines = aplay_lines(log)
        if len(lines) >= count:
            return lines
        time.sleep(0.02)
    raise AssertionError(f"expected {count} aplay launch(es), got {aplay_lines(log)}")


def speaking_state(cfg: dict) -> bool:
    return json.loads(Path(cfg["speaking_state_file"]).read_text(
        encoding="utf-8"))["speaking"]


@contextlib.contextmanager
def temp_cfg(**extra):
    """Config with both state files (speaking / language) in a throwaway path."""
    with tempfile.TemporaryDirectory(prefix="ova_inject_cfg_") as tmp:
        cfg = {
            "speaking_state_file": str(Path(tmp) / "speaking.state"),
            "dialogue_lang": "zh",
            "dialogue_lang_file": str(Path(tmp) / "ova_lang.state"),
        }
        cfg.update(extra)
        yield cfg


# --- HTTP entrance ----------------------------------------------------------

def test_inject_stop_word_routes_stop_and_stays_idle():
    """Nothing playing: the stop word routes to stop, no audio, no engine."""
    backend, engine = FakeBackend(), FakeEngine()
    with temp_cfg() as cfg, inject_server() as port, capture_logs() as logs:
        with FakeMainLoop(backend, ASSETS, cfg, engine) as loop:
            status, body = post(port, "/inject", {"text": "停止"})
            loop.wait_turn()
    assert status == 200
    assert body == {"ok": True, "routed": "stop", "detail": "text=停止"}
    assert engine.calls == []
    assert backend.played_names() == []          # no fallback prompt either
    assert dlg.INJECT.take() is None             # queue drained
    assert dlg.INJECT.interrupted() is False     # no stale interrupt left
    assert any(msg.startswith("INJECT_TEXT text=停止") for msg in logs)
    assert any("INJECT_ROUTED routed=stop" in msg for msg in logs)


def test_inject_showroom_text_plays_prerecorded_wav():
    """A topic keyword plays the local solution audio, never the engine."""
    backend, engine = FakeBackend(), FakeEngine()
    with temp_cfg() as cfg, fake_aplay(1.0) as log, no_microphone():
        with inject_server() as port, capture_logs() as logs:
            with FakeMainLoop(backend, ASSETS, cfg, engine) as loop:
                status, body = post(port, "/inject",
                                    {"text": "介绍一下智慧零售"})
                lines = wait_for_aplay(log, 1)
                loop.wait_turn()
        assert status == 200
        assert body["ok"] is True
        assert body["routed"] == "solution_intro"
        assert body["detail"] == "smart_retail:zh"
        assert any("smart_retail_zh.wav" in line for line in lines)
        assert any("SOLUTION_INTRO id=smart_retail lang=zh" in msg
                   for msg in logs)
    assert engine.calls == []                    # intros cost no cloud call
    assert backend.played_names() == []


def test_inject_lang_selects_the_requested_variant():
    """An explicit lang picks the other language of the same exhibit."""
    backend, engine = FakeBackend(), FakeEngine()
    with temp_cfg() as cfg, fake_aplay(1.0) as log, no_microphone():
        with inject_server() as port:
            with FakeMainLoop(backend, ASSETS, cfg, engine) as loop:
                _, body = post(port, "/inject",
                               {"text": "介绍一下智慧零售", "lang": "en"})
                lines = wait_for_aplay(log, 1)
                loop.wait_turn()
    assert body["routed"] == "solution_intro" and body["detail"] == "smart_retail:en"
    assert any("smart_retail_en.wav" in line for line in lines)

    # No lang at all: the matched keyword decides (voice behaviour unchanged).
    route: dict = {}
    state = dlg._playback_from_text(backend, ASSETS, asr=FakeAsr(), cfg={},
                                    text="introduce smart retail", samples=None,
                                    engine=engine, route=route)
    assert state is not None and state.path.name == "smart_retail_en.wav"
    assert route["routed"] == "solution_intro"


def test_explicit_inject_lang_beats_the_switched_language():
    """显式 lang 最优先：切到英文后仍可单独要一份中文讲解。"""
    backend, engine = FakeBackend(), FakeEngine()
    with temp_cfg() as cfg:
        ova_lang.set_lang(cfg, "en")
        route: dict = {}
        state = dlg._playback_from_text(backend, ASSETS, asr=FakeAsr(), cfg=cfg,
                                        text="介绍一下智慧零售", samples=None,
                                        engine=engine, lang="zh", route=route)
    assert state is not None and state.path.name == "smart_retail_zh.wav"
    assert route["detail"] == "smart_retail:zh"


def test_switched_language_overrides_the_keyword_language():
    """旋钮切换过语言后，中文关键词也播英文讲解；引擎拿到 reply_lang。"""
    backend, engine = FakeBackend(), FakeEngine()
    with temp_cfg() as cfg, fake_aplay(1.0) as log, no_microphone():
        with inject_server(cfg) as port, capture_logs() as logs:
            with FakeMainLoop(backend, ASSETS, cfg, engine) as loop:
                status, body = post(port, "/lang", {"lang": "en"})
                _, intro = post(port, "/inject", {"text": "介绍一下智慧零售"})
                wait_for_aplay(log, 1)
                _, chat = post(port, "/inject", {"text": "今天天气怎么样"})
                loop.wait_turn()        # 第二条注入在播放循环里被取走
                lines = aplay_lines(log)
    assert status == 200 and body == {"ok": True, "lang": "en", "previous": "zh"}
    assert intro["routed"] == "solution_intro"
    assert intro["detail"] == "smart_retail:en"      # 不是关键词的中文变体
    assert "smart_retail_en.wav" in lines[0]
    assert chat["routed"] == "chat"
    assert engine.cfgs[0]["reply_lang"] == "en"      # 语言透传到引擎
    assert any("DIALOGUE_LANG lang=en source=current" in msg for msg in logs)
    assert any("LANG_SET previous=zh lang=en" in msg for msg in logs)


def test_inject_plain_question_answers_without_audio():
    """A question reaches the engine even though there is no recording."""
    backend, engine = FakeBackend(), FakeEngine()
    with temp_cfg() as cfg, fake_aplay(1.0), no_microphone():
        with inject_server() as port:
            with FakeMainLoop(backend, ASSETS, cfg, engine) as loop:
                status, body = post(port, "/inject", {"text": "今天天气怎么样"})
                loop.wait_turn()
    assert status == 200
    assert body["routed"] == "chat" and body["detail"] == "engine=fake"
    assert len(engine.calls) == 1
    got_samples, got_text = engine.calls[0]
    assert got_samples is None and got_text == "今天天气怎么样"


def test_inject_during_playback_interrupts_and_routes_the_new_text():
    """Second inject stops the first playback and plays the new route."""
    backend, engine = FakeBackend(), FakeEngine()
    with temp_cfg() as cfg, fake_aplay(3.0) as log, no_microphone():
        with inject_server() as port, capture_logs() as logs:
            with FakeMainLoop(backend, ASSETS, cfg, engine) as loop:
                _, first = post(port, "/inject", {"text": "介绍一下智慧零售"})
                wait_for_aplay(log, 1)
                assert speaking_state(cfg) is True       # watchdog sees speech
                _, second = post(port, "/inject", {"text": "介绍一下应急救灾"})
                loop.wait_turn()
                assert speaking_state(cfg) is False
                lines = aplay_lines(log)
    assert first["detail"] == "smart_retail:zh"
    assert second["routed"] == "solution_intro"
    assert second["detail"] == "emergency_response:zh"
    assert len(lines) == 2, lines
    assert "smart_retail_zh.wav" in lines[0] and "emergency_response_zh.wav" in lines[1]
    assert any("INJECT_INTERRUPT name=智慧零售讲解" in msg for msg in logs)
    assert any("INJECT_NEXT text=介绍一下应急救灾" in msg for msg in logs)
    assert engine.calls == []


def test_inject_stop_interrupts_playback_and_returns_to_idle():
    """The stop key cuts the running audio and leaves nothing queued."""
    backend, engine = FakeBackend(), FakeEngine()
    with temp_cfg() as cfg, fake_aplay(3.0) as log, no_microphone():
        with inject_server() as port, capture_logs() as logs:
            with FakeMainLoop(backend, ASSETS, cfg, engine) as loop:
                post(port, "/inject", {"text": "介绍一下智慧空间"})
                wait_for_aplay(log, 1)
                status, body = post(port, "/inject", {"text": "停止"})
                loop.wait_turn()
        assert status == 200
        assert body["routed"] == "stop"
        assert len(aplay_lines(log)) == 1            # no further playback
        assert any("INJECT_INTERRUPT name=智慧空间讲解" in msg for msg in logs)
        assert any("BARGE_STOP text=停止" in msg for msg in logs)
        assert speaking_state(cfg) is False
    assert dlg.INJECT.take() is None


def test_play_interruptible_flags_external_interrupt():
    """The playback result tells an injected stop apart from a barge-in."""
    backend = FakeBackend()
    with tempfile.TemporaryDirectory(prefix="ova_inject_wav_") as tmp:
        wav = make_wav(Path(tmp) / "reply.wav")
        state = PlaybackState(path=wav, name="回答", timeout_s=30.0)
        with temp_cfg() as cfg, fake_aplay(3.0) as log, no_microphone():
            with capture_logs() as logs:
                results: list = []
                player = threading.Thread(
                    target=lambda: results.append(
                        play_interruptible(backend, state, cfg)))
                player.start()
                wait_for_aplay(log, 1)
                dlg.INJECT.push(InjectedTurn(text="停止"))
                player.join(timeout=10)
                assert not player.is_alive()
                dlg.INJECT.clear()      # the pushed turn has no consumer here
    result = results[0]
    assert result.completed is False
    assert result.interrupted is True
    assert result.external is True
    assert any("INJECT_INTERRUPT name=回答" in msg for msg in logs)


def test_post_wake_triggers_one_dialogue_round():
    """POST /wake raises exactly one pending wake for the main loop."""
    backend, engine = FakeBackend(), FakeEngine()
    with temp_cfg() as cfg, fake_aplay(1.0):
        with inject_server() as port, capture_logs() as logs:
            dlg.INJECT.clear()
            status, body = post(port, "/wake")
            assert dlg.INJECT.take_wake() is True     # one round is pending
            assert dlg.INJECT.take_wake() is False    # and only one
            states = []
            with patch(dlg,
                       listen_question=lambda *a, **k: np.zeros(1600, dtype=np.int16),
                       _run_playback_loop=lambda b, r, a, c, state, **k:
                       states.append(state)):
                dlg.run_dialogue_round(backend, ASSETS, asr=FakeAsr(), cfg=cfg,
                                       engine=engine)
    assert status == 200 and body == {"ok": True}
    assert any("WAKE_TRIGGERED source=external" in msg for msg in logs)
    assert [s.name for s in states] == ["智慧零售讲解"]   # the round really ran


def test_wake_main_loop_serves_the_entrance():
    """The real ``wake.main()`` idle loop: inject, interrupt, /wake, state file.

    Only the device/model layers are faked (silent capture, blocking aplay,
    stub wake model, stub engine) — the loop, the polling and the routing are
    the production ones.
    """
    played: list[str] = []
    results: list[tuple[str, tuple]] = []
    driver_error: list[str] = []
    stop_loop = threading.Event()

    class Capture:                 # silent microphone, stops the loop on demand
        def __init__(self, backend): pass

        def read(self):
            if stop_loop.is_set():
                raise KeyboardInterrupt
            return np.zeros((1280, 2), dtype=np.int16)

        def close(self): pass

        def __enter__(self): return self

        def __exit__(self, *exc): pass

    class Backend(FakeBackend):    # records ack/fallback without a device
        def __init__(self, cfg):
            super().__init__()
            self.cfg = cfg

        def play_file(self, path, timeout=None):
            super().play_file(path, timeout)
            played.append(Path(path).name)

    class Model:
        models = {"hey_jarvis_v0.1": object()}

        def reset(self): pass

    def driver(port: int, log: Path) -> None:
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:      # wait for INJECT_READY
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.05)
            results.append(("intro", post(port, "/inject", {"text": "介绍一下智慧零售"})))
            wait_for_aplay(log, 1)
            results.append(("stop", post(port, "/inject", {"text": "停止"})))
            results.append(("chat", post(port, "/inject", {"text": "今天天气怎么样"})))
            wait_for_aplay(log, 2)
            results.append(("wake", post(port, "/wake")))
            wait_for_aplay(log, 3, timeout_s=10.0)
            results.append(("lang", post(port, "/lang", {"lang": "en"})))
            results.append(("lang_get", get(port, "/lang")))
        except BaseException as exc:  # noqa: BLE001 - reported by the assertions
            driver_error.append(f"{type(exc).__name__}: {exc}")
        finally:
            stop_loop.set()

    with tempfile.TemporaryDirectory(prefix="ova_main_loop_") as tmp:
        port = free_port()
        state_file = str(Path(tmp) / "speaking.state")
        lang_file = str(Path(tmp) / "ova_lang.state")
        old_env = {k: os.environ.get(k) for k in (
            "WAKE_DIALOGUE", "WAKE_INJECT_HOST", "WAKE_INJECT_PORT",
            "WAKE_RESPONSES_DIR", "WAKE_SPEAKING_STATE_FILE",
            "WAKE_DIALOGUE_LANG", "WAKE_DIALOGUE_LANG_FILE")}
        os.environ.update({
            "WAKE_DIALOGUE": "1",
            "WAKE_INJECT_HOST": "127.0.0.1",
            "WAKE_INJECT_PORT": str(port),
            "WAKE_RESPONSES_DIR": str(ASSETS),
            "WAKE_SPEAKING_STATE_FILE": state_file,
            "WAKE_DIALOGUE_LANG": "zh",
            "WAKE_DIALOGUE_LANG_FILE": lang_file,
        })
        try:
            with fake_aplay(1.5) as log, capture_logs() as logs, patch(
                    wake,
                    Capture=Capture,
                    AlsaBackend=Backend,
                    load_model=lambda *a, **k: Model(),
                    score_frame=lambda *a, **k: 0.0,
                    ensure_dialogue_engine=lambda cfg, asr=None, engine=None:
                    (FakeAsr(), engine or FakeEngine()),
            ), patch(dlg, _barge_monitor=lambda *a, **k: None,
                     _get_barge_model=lambda *a, **k: object(),
                     listen_question=lambda *a, **k: np.zeros(1600, dtype=np.int16)):
                threading.Thread(target=driver, args=(port, log),
                                 daemon=True).start()
                code = wake.main([])
                # the fake-aplay log disappears with its temporary directory
                lines = [line.split()[-1].rsplit("/", 1)[-1]
                         for line in aplay_lines(log)]
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        assert driver_error == [], driver_error
        assert code == 0
        bodies = dict(results)
        assert bodies["intro"][1] == {
            "ok": True, "routed": "solution_intro", "detail": "smart_retail:zh"}
        assert bodies["stop"][1] == {
            "ok": True, "routed": "stop", "detail": "text=停止"}
        assert bodies["chat"][1]["routed"] == "chat"
        assert bodies["wake"][1] == {"ok": True}
        # /lang 走的是 start_inject_server() 绑定的 cfg（生产路径）
        assert bodies["lang"][1] == {"ok": True, "lang": "en", "previous": "zh"}
        assert bodies["lang_get"][1] == {"ok": True, "lang": "en"}
        assert ova_lang.current({"dialogue_lang_file": lang_file}) == "en"

        assert lines == ["smart_retail_zh.wav",          # inject: intro (cut short)
                         "asr_zh_smart_retail.wav",      # inject: engine reply
                         "smart_retail_zh.wav"]          # /wake round routes ASR text
        assert played == ["response.wav"]                # only the wake chime
        assert any("INJECT_INTERRUPT name=智慧零售讲解" in msg for msg in logs)
        assert any("WAKE_TRIGGERED source=external" in msg for msg in logs)
        assert any("INJECT_READY host=127.0.0.1 port=%d lang=zh" % port in msg
                   for msg in logs)
        assert speaking_state({"speaking_state_file": state_file}) is False
    assert dlg.INJECT.take() is None


def test_unknown_path_and_bad_body_are_rejected():
    with temp_cfg(), inject_server() as port:
        status, body = post(port, "/nope")
        assert status == 404 and body["ok"] is False and "unknown path" in body["error"]

        status, body = post(port, "/inject", {})
        assert status == 400 and "text is required" in body["error"]

        status, body = post(port, "/inject", raw="{not json")
        assert status == 400 and "JSON" in body["error"]


# --- language entrance ------------------------------------------------------

def test_post_lang_toggles_and_sets():
    """旋钮长按 = POST /lang {}（切换）；直设 = {"lang": "en"}。"""
    with temp_cfg() as cfg, inject_server(cfg) as port, capture_logs() as logs:
        status, body = post(port, "/lang")
        assert status == 200 and body == {"ok": True, "lang": "en", "previous": "zh"}
        assert ova_lang.current(cfg) == "en"

        status, body = post(port, "/lang", {"toggle": True})
        assert status == 200 and body == {"ok": True, "lang": "zh", "previous": "en"}

        status, body = post(port, "/lang", {"lang": "ENGLISH"})
        assert status == 200 and body == {"ok": True, "lang": "en", "previous": "zh"}
        assert ova_lang.current(cfg) == "en"

        status, body = post(port, "/lang", {"lang": " 中文 "})
        assert status == 200 and body == {"ok": True, "lang": "zh", "previous": "en"}
        assert ova_lang.current(cfg) == "zh"
    assert any("LANG_SET previous=zh lang=en" in msg for msg in logs)


def test_get_lang_reports_the_current_language():
    with temp_cfg() as cfg, inject_server(cfg) as port:
        status, body = get(port, "/lang")
        assert status == 200 and body == {"ok": True, "lang": "zh"}

        post(port, "/lang", {"lang": "en"})
        status, body = get(port, "/lang")
        assert status == 200 and body == {"ok": True, "lang": "en"}


def test_post_lang_rejects_an_unknown_language():
    with temp_cfg() as cfg, inject_server(cfg) as port:
        status, body = post(port, "/lang", {"lang": "jp"})
        assert status == 400 and body["ok"] is False
        assert "unsupported lang" in body["error"]
        assert ova_lang.current(cfg) == "zh"              # 没被改脏
        assert not Path(cfg["dialogue_lang_file"]).exists()


def test_get_unknown_path_is_404_json():
    with temp_cfg(), inject_server() as port:
        status, body = get(port, "/nope")
        assert status == 404 and body["ok"] is False
        assert "unknown path" in body["error"]


def test_inject_text_reports_queued_while_nothing_consumes_it():
    """No loop running: the text stays queued (routed=idle) instead of hanging."""
    dlg.INJECT.clear()
    report = inject_text("你好", timeout_s=0.05)
    assert report["routed"] == "idle"
    assert "no loop took it" in report["detail"]
    turn = dlg.INJECT.take()
    assert turn is not None and turn.text == "你好"
    assert dlg.INJECT.take() is None
    assert dlg.INJECT.interrupted() is False


def test_inject_port_zero_disables_the_entrance():
    with capture_logs() as logs:
        assert inj.start_inject_server({"inject_port": 0}) is None
    assert any("INJECT_DISABLED" in msg for msg in logs)


def test_env_vars_configure_host_and_port():
    import argparse

    old = {k: os.environ.get(k) for k in ("WAKE_INJECT_HOST", "WAKE_INJECT_PORT")}
    os.environ["WAKE_INJECT_HOST"] = "0.0.0.0"
    os.environ["WAKE_INJECT_PORT"] = "0"
    try:
        cfg = wake.resolve_config(argparse.Namespace(config=None))
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert cfg["inject_host"] == "0.0.0.0"
    assert cfg["inject_port"] == 0          # "0" must reach the server as int 0
    assert wake.DEFAULTS["inject_host"] == "127.0.0.1"
    assert wake.DEFAULTS["inject_port"] == 8090


# --- direct runner (no pytest required) -------------------------------------

def _main() -> int:
    import traceback

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception:  # noqa: BLE001 - report and keep going
            failed += 1
            print(f"FAIL: {test.__name__}")
            traceback.print_exc()
        else:
            print(f"PASS: {test.__name__}")
    print(f"\n{'FAILED' if failed else 'OK'}: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
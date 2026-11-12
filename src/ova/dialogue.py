#!/usr/bin/env python3
"""Single-turn voice dialogue round.

The orchestrator owns everything except the "brain": it records the question,
routes local commands (stop/continue/showroom intros), asks the configured
:mod:`ova.engines` engine for a reply and plays it while listening for barge-in.

    listen_question()               energy VAD, trailing silence or max cap
        -> too quiet?               play fallback_listen, listen once more
    engine.respond(samples, text)   pipeline: local ASR -> Qwen -> Qwen TTS
                                    e2e:      audio -> speech model -> audio
        -> EngineError?             play fallback_net, return to idle
    play reply                      interrupts on "Hey Jarvis", then stop /
                                    continue / next intro via local ASR

Externally injected text (POST /inject, see :mod:`ova.inject`) enters the same
routing without touching the microphone: :func:`inject_text` queues the text,
a running :func:`play_interruptible` stops on it, and either the wake main loop
(idle) or :func:`_run_playback_loop` (playing) routes it exactly like an ASR
transcript. :func:`trigger_wake` requests one wake round (POST /wake).

The dialogue language itself is switchable from the showroom keypad (POST
/lang, see :mod:`ova.lang`) and persists for the whole conversation: it picks
the showroom intro variant and is handed to the engine as ``reply_lang``
(``pipeline`` turns it into the LLM system prompt).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import wave

import numpy as np

from ova.wake import (
    RATE,
    Capture,
    _resolve_runtime_path,
    capture_utterance,
    load_model,
    mono,
    score_frame,
)
from ova.config import svc_event
from ova.engines import Engine, EngineError, Reply, build_engine
from ova.asr import LocalAsr
from ova import lang as ova_lang
from ova.solutions import SolutionIntro, load_solution_intros, match_solution_intro

LOG = logging.getLogger("dialogue")

_BARGE_MODEL = None
_BARGE_MODEL_DIR = None
_BARGE_MODEL_LOCK = threading.Lock()
_COMMAND_ASR = None
_COMMAND_ASR_MODEL_DIR = None
_COMMAND_ASR_LOCK = threading.Lock()


@dataclass
class PlaybackState:
    path: Path
    name: str
    text: str = ""
    offset_s: float = 0.0
    timeout_s: float = 180.0
    cleanup: bool = False
    done_msg: str = "本轮对话完成，回到待唤醒"
    done_lang: str = ""
    stream: Any = None          # 流式回答的 PCM 块迭代器（见 Reply.stream）
    barge_vad: bool = False     # 允许人声直接打断（只给对话回答，不给讲解）


@dataclass
class PlaybackResult:
    completed: bool
    interrupted: bool
    elapsed_s: float = 0.0
    score: float = 0.0
    external: bool = False   # stopped by injected text, not by "Hey Jarvis"
    barge_samples: Any = None   # 人声打断时录到的下一句（含预录）


@dataclass
class BargeCommand:
    """A short utterance recorded right after an interrupt."""

    samples: object = None
    text: str = ""


@dataclass
class InjectedTurn:
    """One utterance that entered from outside (a keyboard, not the microphone).

    The turn travels from the HTTP entrance to whichever loop is running: the
    wake main loop while idle, :func:`_run_playback_loop` while audio plays.
    ``report`` collects the routing verdict, ``done`` releases the HTTP handler
    that is waiting for it.
    """

    text: str
    lang: str | None = None
    report: dict = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)

    def finish(self) -> None:
        self.report.setdefault("routed", "idle")
        self.report.setdefault("detail", "")
        self.done.set()


class InjectControl:
    """Queue + interrupt flag shared by the external entrance and the loops."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: list[InjectedTurn] = []
        self._interrupt = threading.Event()
        self._wake = threading.Event()

    def push(self, turn: InjectedTurn) -> None:
        """Queue a turn and ask a running playback to stop."""
        with self._lock:
            self._turns.append(turn)
            self._interrupt.set()

    def take(self) -> InjectedTurn | None:
        """Pop the oldest queued turn (single consumer: the main thread)."""
        with self._lock:
            turn = self._turns.pop(0) if self._turns else None
            if not self._turns:
                self._interrupt.clear()
            return turn

    def interrupted(self) -> bool:
        """True while injected text is queued and no loop has taken it yet."""
        return self._interrupt.is_set()

    def trigger_wake(self) -> None:
        self._wake.set()

    def take_wake(self) -> bool:
        """Consume one pending external wake (repeats collapse into one)."""
        if self._wake.is_set():
            self._wake.clear()
            return True
        return False

    def clear(self) -> None:
        """Drop queued turns and pending signals (shutdown/tests)."""
        with self._lock:
            self._turns.clear()
            self._interrupt.clear()
        self._wake.clear()


INJECT = InjectControl()
INJECT_RESULT_TIMEOUT_S = 12.0


def inject_text(text: str, lang: str | None = None,
                timeout_s: float = INJECT_RESULT_TIMEOUT_S) -> dict:
    """Route one external utterance exactly like a recognized one.

    The text is queued for the wake main loop (idle) or for the playback loop
    (playing: the current audio stops first); while a dialogue round is
    listening (or in its barge-in command window) the queued turn aborts the
    wait so the main loop can route it at once. Blocks until a loop has taken
    the turn and returns its routing verdict.
    """
    turn = InjectedTurn(text=str(text or "").strip(), lang=lang)
    LOG.info("INJECT_TEXT text=%s lang=%s", turn.text, turn.lang or "-")
    svc_event("inject", f"外部注入: {turn.text}", "info", text=turn.text)
    INJECT.push(turn)
    if not turn.done.wait(timeout_s):
        LOG.warning("INJECT_TIMEOUT text=%s waited=%.0fs", turn.text, timeout_s)
        return {"routed": "idle",
                "detail": f"queued, no loop took it within {timeout_s:.0f}s"}
    report = dict(turn.report)
    LOG.info("INJECT_ROUTED routed=%s detail=%s",
             report.get("routed"), report.get("detail"))
    return report


def trigger_wake() -> None:
    """Ask the wake main loop for one dialogue round (POST /wake)."""
    LOG.info("WAKE_TRIGGERED source=external")
    svc_event("wake", "外部触发：开始一轮对话", "info")
    INJECT.trigger_wake()


STOP_WORDS = ("停止", "停一下", "停下", "别说了", "不要说了", "先停", "stop", "cancel")
CONTINUE_WORDS = ("继续", "接着", "继续讲", "接着讲", "continue", "goon", "go on")


def play_asset(backend, root: Path, name: str, cfg: dict | None = None,
               lang: str | None = None) -> None:
    """Play a fallback prompt in the dialogue language, including injected turns."""
    chosen = lang or ova_lang.current(cfg or {})
    path = root / "fallback" / (name.removesuffix(".wav") + "_en.wav" if chosen == "en" else name)
    if not path.is_file() and chosen == "en":
        LOG.warning("missing English fallback asset %s; using Chinese", path)
        path = root / "fallback" / name
    if not path.is_file():
        LOG.warning("missing fallback asset %s", path)
        return
    LOG.info("PLAY_FALLBACK file=%s", path.name)
    svc_event("dialog", f"提示音: {path.name}", "warn")
    backend.play_file(path)


def _normalise_command(text: str) -> str:
    return "".join(ch.lower() for ch in text if not ch.isspace())


def _is_stop_command(text: str) -> bool:
    query = _normalise_command(text)
    return any(_normalise_command(word) in query for word in STOP_WORDS)


def _is_continue_command(text: str) -> bool:
    query = _normalise_command(text)
    return any(_normalise_command(word) in query for word in CONTINUE_WORDS)


def _get_barge_model(model_dir: str):
    global _BARGE_MODEL, _BARGE_MODEL_DIR
    with _BARGE_MODEL_LOCK:
        if _BARGE_MODEL is None or _BARGE_MODEL_DIR != model_dir:
            _BARGE_MODEL = load_model(Path(model_dir))
            _BARGE_MODEL_DIR = model_dir
        else:
            _BARGE_MODEL.reset()
        return _BARGE_MODEL


def _get_command_asr(asr: LocalAsr | None, cfg: dict) -> LocalAsr | None:
    """Return an ASR instance for short barge-in commands.

    E2E mode should not pay the local ASR load cost before the user speaks.
    It only needs ASR after playback is interrupted, when commands such as
    "停止" and "继续" have to be understood locally.
    """
    if asr is not None:
        return asr
    model_dir = str(cfg.get("asr_model_dir", "models/asr_sense_voice_zh_en_int8"))
    global _COMMAND_ASR, _COMMAND_ASR_MODEL_DIR
    with _COMMAND_ASR_LOCK:
        if _COMMAND_ASR is None or _COMMAND_ASR_MODEL_DIR != model_dir:
            _COMMAND_ASR = LocalAsr(Path(model_dir))
            _COMMAND_ASR_MODEL_DIR = model_dir
        return _COMMAND_ASR


def _wav_duration(path: Path) -> float:
    with wave.open(str(path)) as wav:
        return wav.getnframes() / wav.getframerate()


def _slice_wav(path: Path, start_s: float) -> Path:
    start_s = max(0.0, start_s)
    tmp = tempfile.NamedTemporaryFile(prefix="hjw_resume_", suffix=".wav",
                                      delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    with wave.open(str(path), "rb") as src:
        frame_rate = src.getframerate()
        start_frame = min(src.getnframes(), int(start_s * frame_rate))
        src.setpos(start_frame)
        frames = src.readframes(src.getnframes() - start_frame)
        with wave.open(str(tmp_path), "wb") as out:
            out.setnchannels(src.getnchannels())
            out.setsampwidth(src.getsampwidth())
            out.setframerate(frame_rate)
            out.writeframes(frames)
    return tmp_path


def _write_motion_phase(cfg: dict, phase: str, name: str = "") -> None:
    path = cfg.get("speaking_state_file")
    if not path:
        return
    try:
        payload = {
            "phase": phase,
            "speaking": phase == "speaking",  # legacy bool for older readers
            "name": name,
            "updated_at": time.time(),
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 - state hints must not break speech
        LOG.warning("SPEAKING_STATE_ERROR %s: %s", type(exc).__name__, exc)


def _set_speaking_state(cfg: dict, speaking: bool, name: str = "") -> None:
    _write_motion_phase(cfg, "speaking" if speaking else "idle", name)


def _barge_monitor(backend, cfg: dict, stop_event: threading.Event,
                   interrupt_event: threading.Event, result: dict) -> None:
    try:
        model = _get_barge_model(str(cfg.get("model_dir", "models")))
        threshold = float(cfg.get("barge_in_threshold", 0.30))
        required_hits = int(cfg.get("barge_in_hits", 4))
        log_interval = float(cfg.get("barge_in_log_interval_s", 2.0))
        channel = cfg.get("channel", 0)
        hits = 0
        peak = 0.0
        last_log = time.monotonic()
        LOG.info("BARGE_LISTENING_START threshold=%.2f hits=%d",
                 threshold, required_hits)
        with Capture(backend) as capture:
            warmup_end = time.monotonic() + 0.25
            while not stop_event.is_set():
                samples = mono(capture.read(), channel)
                if time.monotonic() < warmup_end:
                    continue
                score = score_frame(model, samples)
                peak = max(peak, score)
                if score >= threshold:
                    hits += 1
                else:
                    hits = 0
                now = time.monotonic()
                if log_interval > 0 and now - last_log >= log_interval:
                    rms = float(
                        ((samples.astype("float32") / 32768.0) ** 2).mean()
                    ) ** 0.5
                    LOG.info(
                        "BARGE_LISTENING peak_score=%.4f rms=%.5f "
                        "threshold=%.2f hits=%d/%d",
                        peak, rms, threshold, hits, required_hits,
                    )
                    peak = 0.0
                    last_log = now
                if hits >= required_hits:
                    result["score"] = score
                    interrupt_event.set()
                    stop_event.set()
                    return
    except Exception as exc:  # noqa: BLE001 - playback must continue on monitor failure
        result["error"] = f"{type(exc).__name__}: {exc}"
        LOG.warning("BARGE_MONITOR_ERROR %s", result["error"])


def _vad_barge_monitor(backend, cfg, stop_event, interrupt_event, result):
    """Silero VAD on the mic during a reply: sustained speech = barge-in.

    The XVF3800 AEC strips the robot's own voice from the mic stream, so
    speech here is a visitor talking over the reply. On trigger it keeps
    recording until end-of-speech and leaves the utterance (with pre-roll)
    in ``result["samples"]`` so it can be routed as the next question.
    """
    try:
        import sherpa_onnx  # lazy

        model = _resolve_runtime_path(cfg.get("vad_model_path", "models/silero_vad.onnx"))
        conf = sherpa_onnx.VadModelConfig()
        conf.silero_vad.model = str(model)
        conf.silero_vad.threshold = float(cfg.get("barge_in_vad_threshold", 0.6))
        conf.silero_vad.min_silence_duration = float(cfg.get("end_silence_s", 0.8))
        conf.silero_vad.min_speech_duration = float(cfg.get("barge_in_vad_min_s", 0.3))
        conf.silero_vad.max_speech_duration = float(cfg.get("max_question_s", 10.0))
        conf.sample_rate = RATE
        conf.num_threads = 1
        window = int(conf.silero_vad.window_size)
        vad = sherpa_onnx.VoiceActivityDetector(conf, buffer_size_in_seconds=30.0)
        channel = cfg.get("channel", 0)
        preroll_n = int(float(cfg.get("barge_in_vad_preroll_s", 0.5)) * RATE)
        warmup = float(cfg.get("barge_in_vad_warmup_s", 0.4))
        max_s = float(cfg.get("max_question_s", 10.0))
        recent: list[np.ndarray] = []
        recorded: list[np.ndarray] = []
        pending = np.zeros(0, dtype=np.float32)
        triggered_at = None
        start = time.monotonic()
        with Capture(backend) as capture:
            while True:
                if stop_event.is_set() and triggered_at is None:
                    return
                m = mono(capture.read(), channel).astype(np.int16)
                now = time.monotonic()
                if now - start < warmup:
                    continue
                if triggered_at is None:
                    recent.append(m)
                    while sum(len(x) for x in recent) > preroll_n + len(m):
                        recent.pop(0)
                else:
                    recorded.append(m)
                pending = np.concatenate([pending, m.astype(np.float32) / 32768.0])
                while len(pending) >= window:
                    vad.accept_waveform(pending[:window])
                    pending = pending[window:]
                if triggered_at is None:
                    if vad.is_speech_detected():
                        triggered_at = now
                        recorded = list(recent)
                        LOG.info("VAD_BARGE_TRIGGER after=%.2fs", now - start)
                        result["vad"] = True
                        interrupt_event.set()
                    continue
                if not vad.empty() or now - triggered_at >= max_s:
                    break
        samples = np.concatenate(recorded) if recorded else None
        result["samples"] = samples
        if samples is not None:
            try:  # 留档最近一次人声打断的录音，排查误触发
                with wave.open("/tmp/ova_vad_barge_last.wav", "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(RATE)
                    wav.writeframes(samples.astype("<i2").tobytes())
            except OSError:
                pass
        LOG.info("VAD_BARGE_CAPTURED speech_s=%.2fs",
                 0.0 if samples is None else len(samples) / RATE)
    except Exception as exc:  # noqa: BLE001 - playback must continue on monitor failure
        result["error"] = f"{type(exc).__name__}: {exc}"
        LOG.warning("VAD_BARGE_MONITOR_ERROR %s", result["error"])


class _StreamFeeder:
    """Pump a PCM chunk iterator into aplay's stdin; keep a copy for the log file."""

    def __init__(self, proc, stream, stop_event):
        self.proc = proc
        self.stream = stream
        self.stop_event = stop_event
        self.chunks: list[np.ndarray] = []
        self.samples = 0
        self.error: str | None = None
        self.muted = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def mute(self) -> None:
        """Stop sound but keep receiving: the full reply stays resumable."""
        self.muted = True
        self._close_stdin()

    def _close_stdin(self) -> None:
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _run(self):
        try:
            for chunk in self.stream:
                if self.stop_event.is_set():
                    break
                self.chunks.append(chunk)
                self.samples += len(chunk)
                if self.muted:
                    continue
                try:
                    self.proc.stdin.write(np.repeat(chunk, 2).astype("<i2").tobytes())
                    self.proc.stdin.flush()
                except (BrokenPipeError, ValueError, OSError):
                    self.muted = True
        except Exception as exc:  # noqa: BLE001 - play what we already have
            self.error = f"{type(exc).__name__}: {exc}"
            LOG.warning("TTS_STREAM_BROKEN %s", self.error)
        finally:
            close = getattr(self.stream, "close", None)
            if close is not None:
                close()
            self._close_stdin()

    def write_wav(self, path: Path) -> None:
        mono16 = np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.int16)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(np.repeat(mono16, 2).astype("<i2").tobytes())


def _stop_proc(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def play_interruptible(backend, state: PlaybackState, cfg: dict) -> PlaybackResult:
    """Play audio (file or live TTS stream) while listening for barge-in.

    Interrupt sources, highest first: external inject; Hey Jarvis (wake-word
    monitor, all playback); visitor speech (VAD monitor, only when
    ``state.barge_vad`` — dialogue replies, never the narrated intros).
    """
    streaming = state.stream is not None
    use_wake = bool(cfg.get("barge_in", True))
    use_vad = use_wake and state.barge_vad and bool(cfg.get("barge_in_vad", False))
    if not use_wake and not streaming:
        _set_speaking_state(cfg, True, state.name)
        try:
            backend.play_file(state.path, timeout=state.timeout_s)
            return PlaybackResult(completed=True, interrupted=False,
                                  elapsed_s=_wav_duration(state.path))
        finally:
            _set_speaking_state(cfg, False, state.name)

    source_duration = 0.0 if streaming else _wav_duration(state.path)
    if not streaming and state.offset_s >= max(0.0, source_duration - 0.1):
        return PlaybackResult(completed=True, interrupted=False,
                              elapsed_s=source_duration)
    if use_wake:
        try:
            _get_barge_model(str(cfg.get("model_dir", "models")))
        except Exception as exc:  # noqa: BLE001 - preserve playback if monitor cannot load
            LOG.warning("BARGE_MODEL_UNAVAILABLE %s: %s", type(exc).__name__, exc)
            use_wake = False

    play_path = state.path
    tmp_path = None
    start_offset = 0.0
    if not streaming and state.offset_s > 0:
        rewind = float(cfg.get("barge_in_resume_rewind_s", 0.6))
        start_offset = max(0.0, state.offset_s - rewind)
        tmp_path = _slice_wav(state.path, start_offset)
        play_path = tmp_path

    stop_event = threading.Event()
    interrupt_event = threading.Event()
    monitor_result: dict = {}
    vad_result: dict = {}
    threads = []
    if use_wake:
        threads.append(threading.Thread(
            target=_barge_monitor,
            args=(backend, cfg, stop_event, interrupt_event, monitor_result),
            daemon=True))
    vad_thread = None
    if use_vad:
        vad_thread = threading.Thread(
            target=_vad_barge_monitor,
            args=(backend, cfg, stop_event, interrupt_event, vad_result),
            daemon=True)
        threads.append(vad_thread)
    for t in threads:
        t.start()
    _set_speaking_state(cfg, True, state.name)
    feed_stop = threading.Event()
    feeder = None
    if streaming:
        cmd = ["aplay", "-q", "-D", backend.output_device, "-t", "raw",
               "-f", "S16_LE", "-r", str(RATE), "-c", "2"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        feeder = _StreamFeeder(proc, state.stream, feed_stop)
    else:
        cmd = ["aplay", "-q", "-D", backend.output_device, str(play_path)]
        proc = subprocess.Popen(cmd)
    start = time.monotonic()
    last_beat = start

    def elapsed() -> float:
        played = start_offset + time.monotonic() - start
        cap = feeder.samples / RATE if feeder is not None else source_duration
        return min(cap, played)

    try:
        while proc.poll() is None:
            if time.monotonic() - last_beat > 30.0:
                # 长讲解/TTS 期间刷新状态时间戳，动作层依赖它判断 phase 未过期
                _set_speaking_state(cfg, True, state.name)
                last_beat = time.monotonic()
            if INJECT.interrupted():
                feed_stop.set()
                _stop_proc(proc)
                at = elapsed()
                LOG.info("INJECT_INTERRUPT name=%s elapsed=%.2fs", state.name, at)
                svc_event("dialog", f"{state.name}被外部注入打断", "warn",
                          elapsed=round(at, 2))
                return PlaybackResult(completed=False, interrupted=True,
                                      elapsed_s=at, external=True)
            if interrupt_event.is_set():
                vad_barge = vad_result.get("vad") and not monitor_result.get("score")
                if vad_barge and feeder is not None:
                    feeder.mute()       # 误触发时还能续播，流继续收完
                else:
                    feed_stop.set()
                _stop_proc(proc)
                at = elapsed()
                if vad_barge:
                    LOG.info("VAD_BARGE name=%s elapsed=%.2fs", state.name, at)
                    svc_event("dialog", f"{state.name}被访客说话打断", "warn",
                              elapsed=round(at, 2))
                    _write_motion_phase(cfg, "listening")
                    vad_thread.join(timeout=float(cfg.get("max_question_s", 10.0)) + 2.0)
                    return PlaybackResult(completed=False, interrupted=True,
                                          elapsed_s=at,
                                          barge_samples=vad_result.get("samples"))
                LOG.info("PLAYBACK_INTERRUPTED name=%s score=%.4f elapsed=%.2fs",
                         state.name, monitor_result.get("score", 0.0), at)
                svc_event("dialog", f"{state.name}已被唤醒词打断", "warn",
                          score=round(monitor_result.get("score", 0.0), 4),
                          elapsed=round(at, 2))
                return PlaybackResult(completed=False, interrupted=True,
                                      elapsed_s=at,
                                      score=float(monitor_result.get("score", 0.0)))
            if time.monotonic() - start > state.timeout_s:
                feed_stop.set()
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(cmd, state.timeout_s)
            time.sleep(0.05)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)
        total = feeder.samples / RATE if feeder is not None else source_duration
        if feeder is not None:
            LOG.info("TTS_STREAM_DONE audio_s=%.2f", total)
        return PlaybackResult(completed=True, interrupted=False, elapsed_s=total)
    finally:
        stop_event.set()
        feed_stop.set()
        for t in threads:
            t.join(timeout=1.0)
        if feeder is not None:
            feeder.thread.join(timeout=float(cfg.get("tts_stream_drain_s", 15.0)))
            state.stream = None          # 流只能播一次；之后按文件处理
            try:
                feeder.write_wav(state.path)
            except OSError as exc:
                LOG.warning("TTS_STREAM_SAVE_FAILED %s", exc)
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        _set_speaking_state(cfg, False, state.name)


def listen_question(backend, channel=0, end_silence_s=1.2,
                    max_s=15.0, min_speech_s=0.35, delay_s=0.8,
                    stop_check=None):
    """Record one question with the echo-safe VAD (see capture_utterance).

    ``stop_check`` aborts the wait as soon as it returns True (a queued
    external inject); the caller decides what to do with the aborted round.
    """
    t0 = time.monotonic()
    backend_cfg = getattr(backend, "cfg", {}) or {}
    samples = capture_utterance(backend, channel=channel,
                                end_silence_s=end_silence_s, max_s=max_s,
                                min_speech_s=min_speech_s, delay_s=delay_s,
                                stop_check=stop_check,
                                vad_backend=backend_cfg.get("vad_backend", "energy"),
                                vad_model_path=backend_cfg.get(
                                    "vad_model_path", "models/silero_vad.onnx"),
                                vad_threshold=backend_cfg.get("vad_threshold", 0.50),
                                vad_buffer_s=backend_cfg.get("vad_buffer_s", 30.0))
    if samples is None:
        if stop_check is not None and stop_check():
            LOG.info("VAD stopped: injected text pending")
        else:
            LOG.info("VAD no speech")
    else:
        LOG.info("VAD_END speech_s=%.2fs", len(samples) / 16000)
    return samples


def listen_command_after_barge_in(backend, asr: LocalAsr, cfg: dict) -> BargeCommand:
    """Record the short follow-up command; keeps the audio for e2e engines."""
    _write_motion_phase(cfg, "listening")
    samples = listen_question(
        backend,
        channel=cfg.get("channel", 0),
        end_silence_s=cfg.get("end_silence_s", 1.2),
        max_s=cfg.get("barge_in_max_command_s", 6.0),
        min_speech_s=0.2,
        delay_s=cfg.get("barge_in_listen_delay_s", 0.2),
        stop_check=INJECT.interrupted,
    )
    if samples is None:
        if INJECT.interrupted():
            # 队列里有注入：让位，主循环马上路由它。
            LOG.info("BARGE_COMMAND_ABORT_INJECT")
        else:
            LOG.info("BARGE_COMMAND_EMPTY")
            svc_event("dialog", "打断后未听到新指令，回到待唤醒", "warn")
        return BargeCommand()
    try:
        command_asr = _get_command_asr(asr, cfg)
        text = command_asr.transcribe(samples) if command_asr is not None else ""
    except Exception as exc:  # noqa: BLE001 - e2e can still answer the audio command
        LOG.warning("BARGE_COMMAND_ASR_UNAVAILABLE %s: %s",
                    type(exc).__name__, exc)
        text = ""
    LOG.info("BARGE_COMMAND text=%s", text)
    svc_event("asr", f"打断后识别: {text}", "ok", text=text)
    return BargeCommand(samples=samples, text=text)


def _cleanup_state(state: PlaybackState | None) -> None:
    if state is None or not state.cleanup:
        return
    try:
        state.path.unlink()
    except OSError:
        pass


def _state_from_reply(engine: Engine, reply: Reply,
                      cfg: dict | None = None) -> PlaybackState:
    """Wrap an engine reply so the shared playback loop can play it."""
    LOG.info("REPLY_READY engine=%s file=%s text=%s",
             engine.name, reply.audio_path, (reply.text or "")[:80])
    svc_event("dialog", f"{engine.name} 回复就绪", "info",
              **{k: v for k, v in reply.meta.items()
                 if isinstance(v, (int, float, str))})
    return PlaybackState(
        path=reply.audio_path,
        name="回答",
        text=reply.text,
        timeout_s=reply.timeout_s,
        cleanup=reply.temporary,
        done_msg="本轮对话完成，回到待唤醒",
        done_lang=reply.lang,
        stream=reply.stream,
        barge_vad=bool((cfg or {}).get("barge_in_vad", False)),
    )


def _intro_for_lang(intro: SolutionIntro, lang: str | None) -> SolutionIntro:
    """Prefer an explicitly requested language variant of a matched intro.

    Voice commands carry no language field — the matched keyword decides. Text
    injected from outside may name one, and the language switched from the
    showroom keypad (:func:`ova.lang.chosen`) counts as one too; then that
    variant is used when the exhibit has it. While nobody chose a language
    (``chosen()`` is None) the keyword keeps deciding, exactly as before.
    """
    if not lang or lang == intro.language:
        return intro
    for candidate in load_solution_intros():
        if (candidate.id == intro.id and candidate.language == lang
                and candidate.audio_path.is_file()):
            LOG.info("SOLUTION_INTRO_LANG id=%s %s->%s",
                     intro.id, intro.language, candidate.language)
            return candidate
    LOG.warning("SOLUTION_INTRO_LANG_MISSING id=%s want=%s keep=%s",
                intro.id, lang, intro.language)
    return intro


def _playback_from_text(
    backend,
    root: Path,
    asr: LocalAsr,
    cfg: dict,
    text: str,
    samples=None,
    previous: PlaybackState | None = None,
    engine: Engine | None = None,
    lang: str | None = None,
    route: dict | None = None,
) -> PlaybackState | None:
    """Route the turn and ask the engine for the next playback.

    ``text`` is the ASR transcript (may be "" for end-to-end engines) and
    ``samples`` the matching 16 kHz mono audio. ``lang`` is an optional
    language hint for injected text (None lets the persistent dialogue language
    — see :mod:`ova.lang` — and the keywords decide) and ``route`` receives the
    routing verdict (``routed``/``detail``) so external callers can report it.
    Returns None to go idle.
    """
    engine = engine if engine is not None else build_engine(cfg, asr=asr)

    # 本轮对话语言：外部显式传入的 lang 优先，否则用持久化的当前语言
    # （展厅旋钮长按切换，见 docs/dialogue-language-switch-2026-09-15.md）。
    effective = lang or ova_lang.current(cfg)
    LOG.info("DIALOGUE_LANG lang=%s source=%s", effective,
             "explicit" if lang else "current")

    def _mark(value: str, detail: str = "") -> None:
        if route is not None:
            route["routed"] = value
            route["detail"] = detail

    if engine.needs_transcript:
        LOG.info("USER_SAID text=%s", text)
        svc_event("asr", f"识别: {text}", "ok", text=text)
        if _is_stop_command(text):
            LOG.info("BARGE_STOP text=%s", text)
            svc_event("dialog", "收到停止指令，回到待唤醒", "ok")
            _mark("stop", f"text={text}")
            return None

        intro = match_solution_intro(text)
        if intro is not None:
            # 讲解选版：显式 lang 优先，其次现场切换过的对话语言，都没有则按关键词。
            intro = _intro_for_lang(intro, lang or ova_lang.chosen(cfg))
            LOG.info("SOLUTION_INTRO id=%s lang=%s file=%s",
                     intro.id, intro.language, intro.audio_path)
            svc_event("dialog", f"播放{intro.name}讲解({intro.language})",
                      "ok", text=intro.text[:120], lang=intro.language)
            if not intro.audio_path.is_file():
                LOG.error("solution intro audio missing: %s", intro.audio_path)
                _mark("idle", f"{intro.id} audio missing")
                play_asset(backend, root, "fallback_question.wav", cfg, effective)
                return None
            _mark("solution_intro", f"{intro.id}:{intro.language}")
            return PlaybackState(
                path=intro.audio_path,
                name=f"{intro.name}讲解",
                text=intro.text,
                timeout_s=180.0,
                done_msg=f"{intro.name}讲解完成，回到待唤醒",
                done_lang=intro.language,
            )

        if previous is not None and _is_continue_command(text):
            LOG.info("BARGE_CONTINUE name=%s offset=%.2fs", previous.name, previous.offset_s)
            svc_event("dialog", f"继续{previous.name}", "ok",
                      elapsed=round(previous.offset_s, 2))
            _mark("continue", f"{previous.name} offset_s={previous.offset_s:.2f}")
            return previous

    if samples is None and not (text and engine.needs_transcript):
        LOG.warning("ENGINE_NO_AUDIO engine=%s", engine.name)
        _mark("idle", f"no audio for {engine.name}")
        play_asset(backend, root, "fallback_question.wav", cfg, effective)
        return None

    if cfg.get("ack_before_reply", False):
        play_asset(backend, root, "ack_think.wav", cfg, effective)
    try:
        reply = engine.respond(samples, text, {**cfg, "reply_lang": effective})
    except EngineError as exc:
        LOG.error("ENGINE_FAILED engine=%s: %s", engine.name, exc)
        svc_event("dialog", f"引擎失败({engine.name}): {exc}"[:140], "warn")
        _mark("idle", f"engine failed {engine.name}: {exc}"[:120])
        play_asset(backend, root, "fallback_net.wav", cfg, effective)
        return None
    except Exception as exc:  # noqa: BLE001 - a broken engine must not kill the service
        LOG.error("ENGINE_ERROR engine=%s %s: %s",
                  engine.name, type(exc).__name__, exc)
        svc_event("dialog", f"引擎异常({engine.name}): {type(exc).__name__}", "warn")
        _mark("idle", f"engine error {engine.name}: {type(exc).__name__}")
        play_asset(backend, root, "fallback_net.wav", cfg, effective)
        return None
    _mark("chat", f"engine={engine.name}")
    return _state_from_reply(engine, reply, cfg)


def _run_playback_loop(
    backend,
    root: Path,
    asr: LocalAsr,
    cfg: dict,
    state: PlaybackState | None,
    engine: Engine | None = None,
) -> None:
    current = state
    while current is not None:
        try:
            result = play_interruptible(backend, current, cfg)
        except Exception as exc:  # noqa: BLE001 - keep wake service alive
            LOG.error("playback failed: %s", exc)
            _cleanup_state(current)
            play_asset(backend, root, "fallback_question.wav", cfg,
                       current.done_lang)
            return
        current.offset_s = result.elapsed_s
        if result.completed:
            LOG.info("PLAYBACK_DONE name=%s", current.name)
            svc_event("dialog", current.done_msg, "ok", lang=current.done_lang)
            _cleanup_state(current)
            return

        if result.external:
            turn = INJECT.take()
            if turn is not None:
                # Injected text outranks the microphone: route it right away
                # instead of opening a barge-in command window.
                LOG.info("INJECT_NEXT text=%s lang=%s", turn.text, turn.lang or "-")
                try:
                    next_state = _playback_from_text(
                        backend, root, asr, cfg, turn.text, samples=None,
                        previous=current, engine=engine, lang=turn.lang,
                        route=turn.report)
                finally:
                    turn.finish()
                if next_state is not current:
                    _cleanup_state(current)
                current = next_state
                continue

        if result.barge_samples is not None:
            # 访客直接开口打断回答：这句话就是下一问，不再开命令窗口；
            # 被打断的回答作废（不可续播）。
            samples = result.barge_samples
            text = asr.transcribe(samples) if engine is None or engine.needs_transcript else ""
            if engine is not None and engine.needs_transcript and not text:
                # 识别为空 = 误触发（电机/环境声）：回答收完整了，从断点续播。
                LOG.info("VAD_BARGE_FALSE resume name=%s offset=%.2fs",
                         current.name, current.offset_s)
                svc_event("dialog", f"人声打断误触发，继续{current.name}", "info")
                continue
            _cleanup_state(current)
            _write_motion_phase(cfg, "thinking")
            current = _playback_from_text(backend, root, asr, cfg, text,
                                          samples=samples, previous=None,
                                          engine=engine)
            continue

        command = listen_command_after_barge_in(backend, asr, cfg)
        if command.samples is None and not command.text:
            _cleanup_state(current)
            return
        next_state = _playback_from_text(backend, root, asr, cfg,
                                         command.text, samples=command.samples,
                                         previous=current, engine=engine)
        if next_state is not current:
            _cleanup_state(current)
        current = next_state


def run_dialogue_round(backend, root: Path, asr: LocalAsr, cfg: dict,
                       engine: Engine | None = None) -> None:
    """One wake->question->answer cycle. Never raises; plays fallbacks.

    A queued external inject aborts a pending listening wait at once; the idle
    main loop then routes it without waiting for the round's fallbacks.
    """
    engine = engine if engine is not None else build_engine(cfg, asr=asr)
    LOG.info("ENGINE name=%s needs_transcript=%s", engine.name, engine.needs_transcript)
    channel = cfg.get("channel", 0)
    samples = None
    text = ""
    _write_motion_phase(cfg, "listening")
    for attempt in (1, 2):
        samples = listen_question(
            backend, channel=channel,
            end_silence_s=cfg.get("end_silence_s", 1.2),
            max_s=cfg.get("max_question_s", 15.0),
            delay_s=cfg.get("listen_delay_s", 0.8),
            stop_check=INJECT.interrupted,
        )
        if samples is None:
            if INJECT.interrupted():
                # 外部注入在排队：立即收尾本轮，交回主循环马上路由。
                LOG.info("LISTEN_INJECT_PENDING attempt=%d hand over", attempt)
                return
            if attempt == 1:
                LOG.info("EMPTY_LISTEN attempt=%d retry with prompt", attempt)
                play_asset(backend, root, "fallback_listen.wav", cfg)
                continue
            LOG.info("EMPTY_LISTEN attempt=2 give up")
            play_asset(backend, root, "fallback_question.wav", cfg)
            return
        if engine.needs_transcript:
            text = asr.transcribe(samples)
            if not text:
                if attempt == 1:
                    LOG.info("ASR_EMPTY attempt=%d retry", attempt)
                    play_asset(backend, root, "fallback_listen.wav", cfg)
                    continue
                play_asset(backend, root, "fallback_question.wav", cfg)
                return
        break
    else:
        return

    _write_motion_phase(cfg, "thinking")
    state = _playback_from_text(backend, root, asr, cfg, text,
                                samples=samples, engine=engine)
    _run_playback_loop(backend, root, asr, cfg, state, engine=engine)

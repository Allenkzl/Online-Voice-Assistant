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
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import wave

from ova.wake import (
    Capture,
    capture_utterance,
    load_model,
    mono,
    score_frame,
)
from ova.config import svc_event
from ova.engines import Engine, EngineError, Reply, build_engine
from ova.asr import LocalAsr
from ova.solutions import match_solution_intro

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


@dataclass
class PlaybackResult:
    completed: bool
    interrupted: bool
    elapsed_s: float = 0.0
    score: float = 0.0


@dataclass
class BargeCommand:
    """A short utterance recorded right after an interrupt."""

    samples: object = None
    text: str = ""


STOP_WORDS = ("停止", "停一下", "停下", "别说了", "不要说了", "先停", "stop", "cancel")
CONTINUE_WORDS = ("继续", "接着", "继续讲", "接着讲", "continue", "goon", "go on")


def play_asset(backend, root: Path, name: str) -> None:
    """Play a fallback prompt from <root>/fallback/<name> (kept out of the
    random wake-acknowledgement pool that scans <root>/*.wav only)."""
    path = root / "fallback" / name
    if not path.is_file():
        LOG.warning("missing fallback asset %s", path)
        return
    LOG.info("PLAY_FALLBACK file=%s", name)
    svc_event("dialog", f"提示音: {name}", "warn")
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


def _set_speaking_state(cfg: dict, speaking: bool, name: str = "") -> None:
    path = cfg.get("speaking_state_file")
    if not path:
        return
    try:
        payload = {
            "speaking": speaking,
            "name": name,
            "updated_at": time.time(),
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 - state hints must not break speech
        LOG.warning("SPEAKING_STATE_ERROR %s: %s", type(exc).__name__, exc)


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


def play_interruptible(backend, state: PlaybackState, cfg: dict) -> PlaybackResult:
    """Play audio while listening for Hey Jarvis; return on finish or barge-in."""
    if not cfg.get("barge_in", True):
        _set_speaking_state(cfg, True, state.name)
        try:
            backend.play_file(state.path, timeout=state.timeout_s)
            return PlaybackResult(completed=True, interrupted=False,
                                  elapsed_s=_wav_duration(state.path))
        finally:
            _set_speaking_state(cfg, False, state.name)

    source_duration = _wav_duration(state.path)
    if state.offset_s >= max(0.0, source_duration - 0.1):
        return PlaybackResult(completed=True, interrupted=False,
                              elapsed_s=source_duration)
    try:
        _get_barge_model(str(cfg.get("model_dir", "models")))
    except Exception as exc:  # noqa: BLE001 - preserve playback if monitor cannot load
        LOG.warning("BARGE_MODEL_UNAVAILABLE %s: %s", type(exc).__name__, exc)
        backend.play_file(state.path, timeout=state.timeout_s)
        return PlaybackResult(completed=True, interrupted=False,
                              elapsed_s=source_duration)

    play_path = state.path
    tmp_path = None
    if state.offset_s > 0:
        rewind = float(cfg.get("barge_in_resume_rewind_s", 0.6))
        resume_at = max(0.0, state.offset_s - rewind)
        tmp_path = _slice_wav(state.path, resume_at)
        play_path = tmp_path
        start_offset = resume_at
    else:
        start_offset = 0.0

    stop_event = threading.Event()
    interrupt_event = threading.Event()
    monitor_result: dict = {}
    monitor = threading.Thread(
        target=_barge_monitor,
        args=(backend, cfg, stop_event, interrupt_event, monitor_result),
        daemon=True,
    )
    monitor.start()
    _set_speaking_state(cfg, True, state.name)
    proc = subprocess.Popen(
        ["aplay", "-q", "-D", backend.output_device, str(play_path)],
    )
    start = time.monotonic()
    try:
        while proc.poll() is None:
            if interrupt_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                elapsed = min(source_duration,
                              start_offset + time.monotonic() - start)
                LOG.info("PLAYBACK_INTERRUPTED name=%s score=%.4f elapsed=%.2fs",
                         state.name, monitor_result.get("score", 0.0), elapsed)
                svc_event("dialog", f"{state.name}已被唤醒词打断", "warn",
                          score=round(monitor_result.get("score", 0.0), 4),
                          elapsed=round(elapsed, 2))
                return PlaybackResult(
                    completed=False,
                    interrupted=True,
                    elapsed_s=elapsed,
                    score=float(monitor_result.get("score", 0.0)),
                )
            if time.monotonic() - start > state.timeout_s:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(["aplay", str(play_path)],
                                                state.timeout_s)
            time.sleep(0.05)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, ["aplay", str(play_path)])
        return PlaybackResult(completed=True, interrupted=False,
                              elapsed_s=source_duration)
    finally:
        stop_event.set()
        monitor.join(timeout=1.0)
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        _set_speaking_state(cfg, False, state.name)


def listen_question(backend, channel=0, end_silence_s=1.2,
                    max_s=15.0, min_speech_s=0.35, delay_s=0.8):
    """Record one question with the echo-safe VAD (see capture_utterance)."""
    t0 = time.monotonic()
    samples = capture_utterance(backend, channel=channel,
                                end_silence_s=end_silence_s, max_s=max_s,
                                min_speech_s=min_speech_s, delay_s=delay_s)
    if samples is None:
        LOG.info("VAD no speech")
    else:
        LOG.info("VAD_END speech_s=%.2fs", len(samples) / 16000)
    return samples


def listen_command_after_barge_in(backend, asr: LocalAsr, cfg: dict) -> BargeCommand:
    """Record the short follow-up command; keeps the audio for e2e engines."""
    samples = listen_question(
        backend,
        channel=cfg.get("channel", 0),
        end_silence_s=cfg.get("end_silence_s", 1.2),
        max_s=cfg.get("barge_in_max_command_s", 6.0),
        min_speech_s=0.2,
        delay_s=cfg.get("barge_in_listen_delay_s", 0.2),
    )
    if samples is None:
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


def _state_from_reply(engine: Engine, reply: Reply) -> PlaybackState:
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
    )


def _playback_from_text(
    backend,
    root: Path,
    asr: LocalAsr,
    cfg: dict,
    text: str,
    samples=None,
    previous: PlaybackState | None = None,
    engine: Engine | None = None,
) -> PlaybackState | None:
    """Route the turn and ask the engine for the next playback.

    ``text`` is the ASR transcript (may be "" for end-to-end engines) and
    ``samples`` the matching 16 kHz mono audio. Returns None to go idle.
    """
    engine = engine if engine is not None else build_engine(cfg, asr=asr)

    if engine.needs_transcript:
        LOG.info("USER_SAID text=%s", text)
        svc_event("asr", f"识别: {text}", "ok", text=text)
        if _is_stop_command(text):
            LOG.info("BARGE_STOP text=%s", text)
            svc_event("dialog", "收到停止指令，回到待唤醒", "ok")
            return None

        intro = match_solution_intro(text)
        if intro is not None:
            LOG.info("SOLUTION_INTRO id=%s lang=%s file=%s",
                     intro.id, intro.language, intro.audio_path)
            svc_event("dialog", f"播放{intro.name}讲解({intro.language})",
                      "ok", text=intro.text[:120], lang=intro.language)
            if not intro.audio_path.is_file():
                LOG.error("solution intro audio missing: %s", intro.audio_path)
                play_asset(backend, root, "fallback_question.wav")
                return None
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
            return previous

    if samples is None:
        LOG.warning("ENGINE_NO_AUDIO engine=%s", engine.name)
        play_asset(backend, root, "fallback_question.wav")
        return None

    if cfg.get("ack_before_reply", False):
        play_asset(backend, root, "ack_think.wav")
    try:
        reply = engine.respond(samples, text, cfg)
    except EngineError as exc:
        LOG.error("ENGINE_FAILED engine=%s: %s", engine.name, exc)
        svc_event("dialog", f"引擎失败({engine.name}): {exc}"[:140], "warn")
        play_asset(backend, root, "fallback_net.wav")
        return None
    except Exception as exc:  # noqa: BLE001 - a broken engine must not kill the service
        LOG.error("ENGINE_ERROR engine=%s %s: %s",
                  engine.name, type(exc).__name__, exc)
        svc_event("dialog", f"引擎异常({engine.name}): {type(exc).__name__}", "warn")
        play_asset(backend, root, "fallback_net.wav")
        return None
    return _state_from_reply(engine, reply)


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
            play_asset(backend, root, "fallback_question.wav")
            return
        current.offset_s = result.elapsed_s
        if result.completed:
            LOG.info("PLAYBACK_DONE name=%s", current.name)
            svc_event("dialog", current.done_msg, "ok", lang=current.done_lang)
            _cleanup_state(current)
            return

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
    """One wake->question->answer cycle. Never raises; plays fallbacks."""
    engine = engine if engine is not None else build_engine(cfg, asr=asr)
    LOG.info("ENGINE name=%s needs_transcript=%s", engine.name, engine.needs_transcript)
    channel = cfg.get("channel", 0)
    samples = None
    text = ""
    for attempt in (1, 2):
        samples = listen_question(
            backend, channel=channel,
            end_silence_s=cfg.get("end_silence_s", 1.2),
            max_s=cfg.get("max_question_s", 15.0),
            delay_s=cfg.get("listen_delay_s", 0.8),
        )
        if samples is None:
            if attempt == 1:
                LOG.info("EMPTY_LISTEN attempt=%d retry with prompt", attempt)
                play_asset(backend, root, "fallback_listen.wav")
                continue
            LOG.info("EMPTY_LISTEN attempt=2 give up")
            play_asset(backend, root, "fallback_question.wav")
            return
        if engine.needs_transcript:
            text = asr.transcribe(samples)
            if not text:
                if attempt == 1:
                    LOG.info("ASR_EMPTY attempt=%d retry", attempt)
                    play_asset(backend, root, "fallback_listen.wav")
                    continue
                play_asset(backend, root, "fallback_question.wav")
                return
        break
    else:
        return

    state = _playback_from_text(backend, root, asr, cfg, text,
                                samples=samples, engine=engine)
    _run_playback_loop(backend, root, asr, cfg, state, engine=engine)

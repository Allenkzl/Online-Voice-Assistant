#!/usr/bin/env python3
"""Single-turn voice dialogue round: listen -> local ASR -> Qwen -> TTS.

Flow (called by wake_service right after the wake acknowledgement):

    listen_question()           energy VAD, 2 s trailing silence or 15 s cap
        -> empty text?         play fallback_listen, listen once more
    asr (sherpa-onnx, local)
    chat (Qwen online)         -> CloudError?  play fallback_net, abort
    synthesize (qwen3-tts)     -> CloudError?  play fallback_net, abort
    play reply
"""

from __future__ import annotations

from dataclasses import dataclass
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
    svc_event,
)
from ova.llm import SYSTEM_PROMPT, CloudError, chat_once
from ova.tts import synthesize
from ova.asr import LocalAsr
from ova.solutions import match_solution_intro
from ova.tools import WEATHER_TOOL, load_tool_calls, query_weather

LOG = logging.getLogger("dialogue")

_BARGE_MODEL = None
_BARGE_MODEL_DIR = None
_BARGE_MODEL_LOCK = threading.Lock()


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


def _barge_monitor(backend, cfg: dict, stop_event: threading.Event,
                   interrupt_event: threading.Event, result: dict) -> None:
    try:
        model = _get_barge_model(str(cfg.get("model_dir", "models")))
        threshold = float(cfg.get("barge_in_threshold", 0.45))
        required_hits = int(cfg.get("barge_in_hits", 4))
        channel = cfg.get("channel", 0)
        hits = 0
        with Capture(backend) as capture:
            warmup_end = time.monotonic() + 0.25
            while not stop_event.is_set():
                samples = mono(capture.read(), channel)
                if time.monotonic() < warmup_end:
                    continue
                score = score_frame(model, samples)
                if score >= threshold:
                    hits += 1
                else:
                    hits = 0
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
        backend.play_file(state.path, timeout=state.timeout_s)
        return PlaybackResult(completed=True, interrupted=False,
                              elapsed_s=_wav_duration(state.path))

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


def listen_command_after_barge_in(backend, asr: LocalAsr, cfg: dict) -> str:
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
        return ""
    text = asr.transcribe(samples)
    LOG.info("BARGE_COMMAND text=%s", text)
    svc_event("asr", f"打断后识别: {text}", "ok", text=text)
    return text


def _cleanup_state(state: PlaybackState | None) -> None:
    if state is None or not state.cleanup:
        return
    try:
        state.path.unlink()
    except OSError:
        pass


def _playback_from_text(
    backend,
    root: Path,
    asr: LocalAsr,
    cfg: dict,
    text: str,
    previous: PlaybackState | None = None,
) -> PlaybackState | None:
    """Map recognized text to the next playback, or None to return idle."""
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

    play_asset(backend, root, "ack_think.wav")
    try:
        reply = _clip(_ask_with_weather(text))
    except CloudError as exc:
        LOG.error("chat failed: %s", exc)
        play_asset(backend, root, "fallback_net.wav")
        return None
    LOG.info("QWEN_REPLY text=%s", reply[:80])
    svc_event("llm", f"千问回答: {reply[:80]}", "ok", text=reply[:120])

    try:
        wav = synthesize(reply)
    except CloudError as exc:
        LOG.error("tts failed: %s", exc)
        play_asset(backend, root, "fallback_net.wav")
        return None

    tmp = Path(f"/tmp/hjw_reply_{os.getpid()}_{int(time.time() * 1000)}.wav")
    tmp.write_bytes(wav)
    LOG.info("REPLY_START")
    svc_event("tts", "播放回答…", "info")
    return PlaybackState(
        path=tmp,
        name="回答",
        text=reply,
        timeout_s=90.0,
        cleanup=True,
        done_msg="本轮对话完成，回到待唤醒",
    )


def _run_playback_loop(
    backend,
    root: Path,
    asr: LocalAsr,
    cfg: dict,
    state: PlaybackState | None,
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
        if not command:
            _cleanup_state(current)
            return
        next_state = _playback_from_text(backend, root, asr, cfg,
                                         command, previous=current)
        if next_state is not current:
            _cleanup_state(current)
        current = next_state


def _ask_with_weather(text: str) -> str:
    """Qwen with a weather tool: answer plain, or fetch live weather and
    compose a short spoken summary from the fetched facts."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    first = chat_once(messages, tools=[WEATHER_TOOL])
    calls = load_tool_calls(first)
    if not calls:
        reply = (first.get("content") or "").strip()
        if not reply:
            raise CloudError("empty assistant reply")
        return reply
    # Tool round: run every requested tool, then ask Qwen to compose.
    messages.append(first)
    for name, args, call_id in calls:
        if name == "query_weather":
            try:
                result = query_weather(args.get("city_slug", "Hangzhou"))
            except Exception as exc:
                result = f"天气查询失败: {exc}"
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            })
    second = chat_once(messages)
    reply = (second.get("content") or "").strip()
    if not reply:
        raise CloudError("empty assistant reply after tool call")
    LOG.info("QWEN_TOOL_ROUND city=%s", ", ".join(
        str(a.get("city_slug")) for _, a, _ in calls))
    return reply


def _clip(text: str, maxlen: int = 130) -> str:
    """Keep replies short for spoken delivery: cut at a sentence boundary."""
    if len(text) <= maxlen:
        return text
    cut = text[:maxlen]
    for sep in ("。", "！", "？"):
        idx = cut.rfind(sep)
        if idx > maxlen // 2:
            cut = cut[:idx + 1]
            break
    else:
        cut = cut + "。"
    LOG.info("REPLY_CLIPPED len=%d -> %d", len(text), len(cut))
    return cut


def run_dialogue_round(backend, root: Path, asr: LocalAsr, cfg: dict) -> None:
    """One wake->question->answer cycle. Never raises; plays fallbacks."""
    channel = cfg.get("channel", 0)
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

    state = _playback_from_text(backend, root, asr, cfg, text)
    _run_playback_loop(backend, root, asr, cfg, state)

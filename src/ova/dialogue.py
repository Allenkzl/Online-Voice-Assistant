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

import logging
import time
from pathlib import Path

import numpy as np

from ova.wake import capture_utterance, svc_event
from ova.llm import SYSTEM_PROMPT, CloudError, chat_once
from ova.tts import synthesize
from ova.asr import LocalAsr
from ova.tools import WEATHER_TOOL, load_tool_calls, query_weather

LOG = logging.getLogger("dialogue")


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

    LOG.info("USER_SAID text=%s", text)
    svc_event("asr", f"识别: {text}", "ok", text=text)
    # Perceived-latency buffer: answer verbally first, then think.
    play_asset(backend, root, "ack_think.wav")
    try:
        reply = _clip(_ask_with_weather(text))
    except CloudError as exc:
        LOG.error("chat failed: %s", exc)
        play_asset(backend, root, "fallback_net.wav")
        return
    LOG.info("QWEN_REPLY text=%s", reply[:80])
    svc_event("llm", f"千问回答: {reply[:80]}", "ok", text=reply[:120])

    try:
        wav = synthesize(reply)
    except CloudError as exc:
        LOG.error("tts failed: %s", exc)
        play_asset(backend, root, "fallback_net.wav")
        return

    tmp = Path("/tmp/hjw_reply.wav")
    tmp.write_bytes(wav)
    LOG.info("REPLY_START")
    svc_event("tts", "播放回答…", "info")
    try:
        backend.play_file(tmp, timeout=90.0)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    LOG.info("REPLY_DONE")
    svc_event("dialog", "本轮对话完成，回到待唤醒", "ok")

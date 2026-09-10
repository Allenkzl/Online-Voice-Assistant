#!/usr/bin/env python3
"""Semi-online engine: local ASR text -> Qwen chat (tools) -> Qwen TTS.

This is the behaviour the project has always had, moved behind the Engine
interface unchanged: the same log lines, the same console events ("llm" and
"tts" cards) and the same /tmp reply file naming.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from ova.config import svc_event
from ova.engines.base import EngineError, Reply
from ova.llm import SYSTEM_PROMPT, CloudError, chat_once
from ova.tools import WEATHER_TOOL, load_tool_calls, query_weather
from ova.tts import synthesize

LOG = logging.getLogger("dialogue.pipeline")


def clip(text: str, maxlen: int = 130) -> str:
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


def ask_with_weather(text: str) -> str:
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
            except Exception as exc:  # noqa: BLE001 - report failure to the model
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


class PipelineEngine:
    """Local ASR + Qwen chat + Qwen TTS (needs the ASR transcript)."""

    name = "pipeline"
    needs_transcript = True

    def __init__(self, asr=None, cfg: dict | None = None):
        self.asr = asr
        self.cfg = cfg or {}

    def respond(self, samples, text: str, cfg: dict | None = None) -> Reply:
        cfg = cfg or self.cfg
        t0 = time.monotonic()
        try:
            reply_text = clip(ask_with_weather(text))
        except CloudError as exc:
            raise EngineError(f"chat failed: {exc}") from exc
        LOG.info("QWEN_REPLY text=%s", reply_text[:80])
        svc_event("llm", f"千问回答: {reply_text[:80]}", "ok", text=reply_text[:120])

        try:
            wav = synthesize(reply_text)
        except CloudError as exc:
            raise EngineError(f"tts failed: {exc}") from exc

        tmp = Path(f"/tmp/hjw_reply_{os.getpid()}_{int(time.time() * 1000)}.wav")
        tmp.write_bytes(wav)
        LOG.info("REPLY_START")
        svc_event("tts", "播放回答…", "info")
        return Reply(
            audio_path=tmp,
            text=reply_text,
            transcript=text,
            timeout_s=90.0,
            temporary=True,
            meta={
                "engine": self.name,
                "provider": "qwen",
                "elapsed_s": round(time.monotonic() - t0, 2),
            },
        )

#!/usr/bin/env python3
"""End-to-end engine: recorded audio -> speech model -> audio back.

GLM-4-Voice (Zhipu) is a single Chat-Completions call: the utterance goes in
as base64 WAV next to a persona instruction, and the answer comes back twice —
as text in ``message.content`` and as **raw PCM** in ``message.audio['data']``
(44.1 kHz, mono, 16-bit, *no WAV header*), which is resampled to the device's
16 kHz stereo and written to a temporary file for the shared playback loop.

Deliberately not implemented here (see docs/dual-engine-architecture.md):
the model has no function calling, so tools stay on the pipeline engine, and
non-streaming replies mean barge-in can only stop playback, not cancel a
generation still in flight.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from ova.api import CloudError
from ova.audio import device_wav_bytes, mono16k_wav_bytes
from ova.config import svc_event
from ova.engines.base import EngineError, Reply

LOG = logging.getLogger("dialogue.e2e")

DEFAULT_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_MODEL = "glm-4-voice"
DEFAULT_API_KEY_ENV = "ZHIPUAI_API_KEY"
DEFAULT_PCM_RATE = 44100          # verified against the official example
DEFAULT_TIMEOUT_S = 30.0
PRICE_CNY_PER_MTOKENS = 80.0      # 智谱 GLM-4-Voice 原价（元/百万 tokens）

DEFAULT_PERSONA = (
    "你是展厅导览机器人。回答只用一句话：中文不超过25个字，英文不超过12个单词"
    "（约3-4秒语音）。用户说中文就用中文回答，说英文就用英文回答。"
    "不要重复用户的话，不要罗列，不要用列表/表情/markdown。"
)
# Measured 2026-09-10 (see docs/glm-voice-poc-2026-09-10.md): the untuned
# "40 字以内" persona produced 5-11 s of audio; this one keeps replies at
# ~2-4 s, roughly halving request latency and cost.


class GlmVoiceEngine:
    """End-to-end speech-to-speech engine (no local ASR in the path)."""

    name = "e2e"
    needs_transcript = False

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.url = str(cfg.get("glm_voice_url")
                       or os.getenv("GLM_VOICE_URL", DEFAULT_URL))
        self.model = str(cfg.get("glm_voice_model")
                         or os.getenv("GLM_VOICE_MODEL", DEFAULT_MODEL))
        self.persona = str(cfg.get("glm_voice_persona")
                           or os.getenv("GLM_VOICE_PERSONA", DEFAULT_PERSONA))
        self.api_key_env = str(cfg.get("glm_voice_api_key_env")
                               or os.getenv("GLM_VOICE_API_KEY_ENV", DEFAULT_API_KEY_ENV))
        self.timeout_s = float(cfg.get("glm_voice_timeout_s")
                               or os.getenv("GLM_VOICE_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        self.pcm_rate = int(cfg.get("glm_voice_pcm_rate")
                            or os.getenv("GLM_VOICE_PCM_RATE", DEFAULT_PCM_RATE))
        self.playback_timeout_s = float(cfg.get("e2e_playback_timeout_s")
                                        or os.getenv("E2E_PLAYBACK_TIMEOUT_S", 180.0))
        LOG.info("E2E_READY engine=glm-4-voice model=%s url=%s timeout=%.0fs pcm_rate=%d",
                 self.model, self.url, self.timeout_s, self.pcm_rate)

    # -- request ---------------------------------------------------------
    def _payload(self, wav_b64: str, prompt: str) -> dict:
        return {
            "model": self.model,
            "stream": False,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "input_audio",
                     "input_audio": {"data": wav_b64, "format": "wav"}},
                ],
            }],
        }

    def _post(self, payload: dict) -> dict:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise EngineError(f"{self.api_key_env} is not set")
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise EngineError(f"HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise EngineError(f"network error: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise EngineError(f"bad json from {self.model}: {exc}") from exc

    # -- response parsing -------------------------------------------------
    @staticmethod
    def _message(data: dict) -> dict:
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EngineError(f"unexpected response: {str(data)[:200]}") from exc

    def _decode_pcm(self, message: dict) -> bytes:
        audio = message.get("audio") or {}
        raw_b64 = audio.get("data") if isinstance(audio, dict) else None
        if not raw_b64:
            raise EngineError("response has no audio payload")
        try:
            pcm = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise EngineError(f"audio payload is not valid base64: {exc}") from exc
        if not pcm:
            raise EngineError("audio payload is empty")
        return pcm

    @staticmethod
    def _guess_lang(text: str) -> str:
        for ch in text:
            if "\u4e00" <= ch <= "\u9fff":
                return "zh"
            if ch.isascii() and ch.isalpha():
                return "en"
        return ""

    # -- Engine interface -------------------------------------------------
    def respond(self, samples, text: str = "", cfg: dict | None = None) -> Reply:
        cfg = cfg or {}
        t0 = time.monotonic()

        wav = mono16k_wav_bytes(samples)
        wav_b64 = base64.b64encode(wav).decode("ascii")
        prompt = str(cfg.get("glm_voice_persona") or self.persona)
        if text:
            # Only reached if a caller already has a transcript (console tests).
            prompt = f"{prompt}\n用户刚才说：{text}"
        encode_s = time.monotonic() - t0

        t1 = time.monotonic()
        data = self._post(self._payload(wav_b64, prompt))
        request_s = time.monotonic() - t1

        message = self._message(data)
        reply_text = (message.get("content") or "").strip()
        pcm = self._decode_pcm(message)

        t2 = time.monotonic()
        mono = np.frombuffer(pcm, dtype="<i2")
        device_wav = device_wav_bytes(mono, self.pcm_rate)
        tmp = Path(f"/tmp/hjw_e2e_{os.getpid()}_{int(time.time() * 1000)}.wav")
        tmp.write_bytes(device_wav)
        convert_s = time.monotonic() - t2

        audio_s = len(mono) / float(self.pcm_rate or 1)
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens")
        cost = round(float(tokens) * PRICE_CNY_PER_MTOKENS / 1e6, 5) if tokens else None
        elapsed = time.monotonic() - t0
        lang = self._guess_lang(reply_text)

        LOG.info("E2E_REPLY text=%s", reply_text[:80] or "(empty)")
        LOG.info("E2E_LATENCY encode=%.2fs request=%.2fs convert=%.2fs total=%.2fs "
                 "audio=%.2fs", encode_s, request_s, convert_s, elapsed, audio_s)
        if tokens:
            LOG.info("E2E_USAGE prompt=%s completion=%s total=%s cost=¥%s",
                     usage.get("prompt_tokens"), usage.get("completion_tokens"),
                     tokens, cost)
        svc_event("e2e", f"端到端回复({audio_s:.1f}s音频, {elapsed:.1f}s): {reply_text[:80]}",
                  "ok", text=reply_text[:200], lang=lang,
                  request_s=round(request_s, 2), audio_s=round(audio_s, 2))
        if cost is not None:
            svc_event("e2e", f"用量 {tokens} tokens ≈ ¥{cost}", "info",
                      tokens=tokens, cost=cost)

        return Reply(
            audio_path=tmp,
            text=reply_text,
            transcript="",
            lang=lang,
            timeout_s=self.playback_timeout_s,
            temporary=True,
            meta={
                "engine": self.name,
                "provider": "glm-4-voice",
                "encode_s": round(encode_s, 2),
                "request_s": round(request_s, 2),
                "convert_s": round(convert_s, 3),
                "elapsed_s": round(elapsed, 2),
                "audio_s": round(audio_s, 2),
                "tokens": tokens,
                "cost_cny": cost,
            },
        )


def respond_file(wav_path: str | Path, cfg: dict | None = None,
                 persona: str | None = None) -> Reply:
    """Offline helper for scripts/PoC: a 16 kHz WAV file in, a Reply out.

    ``reply.meta`` carries the latency breakdown, token usage and cost;
    ``reply.audio_path`` is a playable 16 kHz stereo WAV. Raises EngineError
    (cloud/format/timeout) on failure.
    """
    import wave

    engine = GlmVoiceEngine(cfg)
    with wave.open(str(wav_path)) as w:
        if w.getsampwidth() != 2:
            raise CloudError(f"{wav_path}: only 16-bit PCM is supported")
        if w.getframerate() != 16000:
            raise CloudError(f"{wav_path}: expected 16 kHz, got {w.getframerate()}")
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        if w.getnchannels() > 1:
            samples = samples.reshape(-1, w.getnchannels())
    local_cfg = dict(cfg or {})
    if persona:
        local_cfg["glm_voice_persona"] = persona
    return engine.respond(samples, "", local_cfg)

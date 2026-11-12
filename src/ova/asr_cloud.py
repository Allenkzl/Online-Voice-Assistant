#!/usr/bin/env python3
"""Cloud speech recognition (DashScope ``qwen3-asr-flash``) with local fallback.

Same interface as :class:`ova.asr.LocalAsr` (``transcribe()`` +
``last_language``), so the dialogue flow does not care which one it gets.
Measured on the CM4 in the showroom: ~0.8 s per 3–7 s utterance versus
3–5 s for local SenseVoice int8 (the CPU is shared with the robot daemon).

Any cloud failure (no key, timeout, DNS, HTTP error) falls back to the local
recognizer for that turn, which is built lazily on the first failure.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path

from ova.api import CloudError, http_json
from ova.asr import content_length
from ova.audio import wav_bytes

LOG = logging.getLogger("dialogue.asr")

ASR_URL = os.getenv(
    "ASR_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
ASR_MODEL = os.getenv("ASR_MODEL", "qwen3-asr-flash")


def _language(message: dict) -> str:
    for item in message.get("annotations") or []:
        if isinstance(item, dict) and item.get("language"):
            return str(item["language"]).lower()
    return ""


class CloudAsr:
    """qwen3-asr-flash recognizer; falls back to local sherpa-onnx on error."""

    def __init__(self, local_model_dir: str | Path | None = None,
                 timeout: float = 6.0, model: str = ASR_MODEL,
                 url: str = ASR_URL, post=http_json):
        self.local_model_dir = Path(local_model_dir) if local_model_dir else None
        self.timeout = timeout
        self.model = model
        self.url = url
        self._post = post
        self._local = None
        self.last_language = ""
        LOG.info("ASR_READY backend=cloud model=%s timeout=%.1fs fallback=%s",
                 model, timeout, self.local_model_dir or "-")

    def _local_asr(self):
        if self._local is None and self.local_model_dir is not None:
            from ova.asr import LocalAsr
            self._local = LocalAsr(self.local_model_dir)
        return self._local

    def _cloud(self, samples_int16) -> tuple[str, str]:
        audio = base64.b64encode(wav_bytes(samples_int16, rate=16000, channels=1)).decode()
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [{
                "type": "input_audio",
                "input_audio": {"data": "data:audio/wav;base64," + audio},
            }]}],
            "stream": False,
            "asr_options": {"enable_itn": True},
        }
        data = self._post(self.url, payload, self.timeout)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CloudError(f"unexpected asr response: {str(data)[:200]}") from exc
        return (message.get("content") or "").strip(), _language(message)

    def transcribe(self, samples_int16) -> str:
        if samples_int16 is None or len(samples_int16) == 0:
            return ""
        t0 = time.monotonic()
        try:
            text, lang = self._cloud(samples_int16)
        except (CloudError, OSError) as exc:  # OSError: socket timeout etc.
            LOG.warning("ASR_CLOUD_FAILED %s; falling back to local", exc)
            local = self._local_asr()
            if local is None:
                self.last_language = ""
                return ""
            text = local.transcribe(samples_int16)
            self.last_language = local.last_language
            return text
        self.last_language = lang
        tag = f" lang={lang}" if lang else ""
        elapsed = time.monotonic() - t0
        if content_length(text) <= 1:
            LOG.info("ASR_RESULT backend=cloud%s elapsed=%.2fs text=%s (dropped: no speech)",
                     tag, elapsed, text or "(empty)")
            return ""
        LOG.info("ASR_RESULT backend=cloud%s elapsed=%.2fs text=%s", tag, elapsed, text)
        return text

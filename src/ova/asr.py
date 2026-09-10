#!/usr/bin/env python3
"""Local speech recognition via sherpa-onnx (Chinese + English, offline).

The model type is auto-detected from the model directory, so switching ASR is
just a matter of pointing ``asr_model_dir`` at another folder:

  * ``sense_voice`` — bilingual, recommended
      ``model.int8.onnx`` | ``model.onnx`` + ``tokens.txt`` whose tokens carry
      language tags (``<|zh|>`` ``<|en|>`` …).
      Model: ``sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17``
      Languages: Chinese, English, Cantonese, Japanese, Korean. Emits
      punctuation (``use_itn``) and exposes the detected language.
  * ``paraformer`` — Chinese only (legacy fallback)
      ``model.int8.onnx`` | ``model.onnx`` + ``tokens.txt`` without language tags.
      Models: ``sherpa-onnx-paraformer-zh-int8-2025-10-07`` (accurate) or
      ``sherpa-onnx-paraformer-zh-small-2024-03-09`` (faster).
  * ``transducer`` — zipformer transducer, e.g. X-ASR zh-en
      ``encoder*.onnx`` + ``decoder*.onnx`` + ``joiner*.onnx`` + ``tokens.txt``.

Model files are downloaded by ``scripts/download_models.sh`` (see README).
Evaluation notes: ``docs/asr-bilingual-models-2026-09-10.md``.
"""

from __future__ import annotations

import logging
import string
import time
import unicodedata
from pathlib import Path

import numpy as np

LOG = logging.getLogger("dialogue.asr")

# SenseVoice ships these tags in tokens.txt; Paraformer does not.
SENSE_VOICE_TAG = "<|zh|>"
MODEL_TYPES = ("auto", "sense_voice", "paraformer", "transducer")


def _model_file(model_dir: Path) -> Path | None:
    """Return the quantized model file when present, else the fp32 one."""
    for name in ("model.int8.onnx", "model.onnx"):
        path = model_dir / name
        if path.is_file():
            return path
    return None


def detect_model_type(model_dir: str | Path) -> str:
    """Guess the sherpa-onnx model family from the files in ``model_dir``."""
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        raise FileNotFoundError(
            f"ASR model directory missing: {model_dir} "
            "(run scripts/download_models.sh)"
        )
    if list(model_dir.glob("encoder*.onnx")) and list(model_dir.glob("joiner*.onnx")):
        return "transducer"
    tokens_path = model_dir / "tokens.txt"
    if not tokens_path.is_file():
        raise FileNotFoundError(
            f"ASR model files missing in {model_dir}: need tokens.txt "
            "(run scripts/download_models.sh)"
        )
    if _model_file(model_dir) is None:
        raise FileNotFoundError(
            f"ASR model files missing in {model_dir}: need "
            f"model.int8.onnx/model.onnx or encoder*.onnx "
            "(run scripts/download_models.sh)"
        )
    if SENSE_VOICE_TAG in tokens_path.read_text(encoding="utf-8", errors="ignore"):
        return "sense_voice"
    return "paraformer"


def _clean_tag(value: str) -> str:
    """``'<|zh|>'`` -> ``'zh'``; anything unexpected -> ``''``."""
    text = (value or "").strip()
    if text.startswith("<|") and text.endswith("|>"):
        return text[2:-2].strip().lower()
    return text.lower() if text else ""


def content_length(text: str) -> int:
    """Count real characters, ignoring spaces and punctuation."""
    return sum(1 for ch in text
               if not ch.isspace() and ch not in string.punctuation
               and not unicodedata.category(ch).startswith("P"))


class LocalAsr:
    """Offline sherpa-onnx recognizer wrapper (16 kHz mono int16 in)."""

    def __init__(self, model_dir: str | Path, num_threads: int = 4,
                 model_type: str = "auto", language: str = "auto",
                 use_itn: bool = True):
        model_dir = Path(model_dir)
        if model_type not in MODEL_TYPES:
            raise ValueError(f"model_type must be one of {MODEL_TYPES}, got {model_type!r}")
        self.model_dir = model_dir
        self.model_type = detect_model_type(model_dir) if model_type == "auto" else model_type
        self.num_threads = num_threads
        self.language = language
        self.use_itn = use_itn
        self.last_language = ""   # filled by transcribe() for SenseVoice models

        import sherpa_onnx  # lazy import: only needed when dialogue is on

        t0 = time.monotonic()
        self.recognizer = self._build(sherpa_onnx)
        LOG.info("ASR_READY model=%s type=%s threads=%d load=%.1fs",
                 model_dir, self.model_type, num_threads, time.monotonic() - t0)

    def _build(self, sherpa_onnx):
        tokens = str(self.model_dir / "tokens.txt")
        common = dict(num_threads=self.num_threads, provider="cpu", debug=False)

        if self.model_type == "sense_voice":
            return sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(_model_file(self.model_dir)), tokens=tokens,
                language=self.language, use_itn=self.use_itn, **common)

        if self.model_type == "paraformer":
            return sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(_model_file(self.model_dir)), tokens=tokens,
                sample_rate=16000, decoding_method="greedy_search", **common)

        encoder = sorted(self.model_dir.glob("encoder*.onnx"))[0]
        decoder = sorted(self.model_dir.glob("decoder*.onnx"))[0]
        joiner = sorted(self.model_dir.glob("joiner*.onnx"))[0]
        return sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(encoder), decoder=str(decoder), joiner=str(joiner),
            tokens=tokens, sample_rate=16000, decoding_method="greedy_search",
            **common)

    @staticmethod
    def _agc(samples_int16, target_rms: float = 0.10, max_gain: float = 8.0):
        """Volume normalization (AGC): lift quiet speech, keep noise low.

        Returns float32 samples in [-1, 1]; no-op on very quiet input so
        background noise is not amplified.
        """
        audio = samples_int16.astype("float32") / 32768.0
        rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
        if 0.003 <= rms <= 0.35:  # ignore digital silence or clipping
            gain = min(target_rms / rms, max_gain)
            audio = np.clip(audio * gain, -1.0, 1.0)
            LOG.info("ASR_AGC rms=%.4f gain=%.1f", rms, gain)
        return audio

    def transcribe(self, samples_int16) -> str:
        """samples_int16: 1-D numpy int16 array at 16 kHz mono.

        Returns ``""`` for silence/noise: both models emit a stray token on
        non-speech (measured 2026-09-10 — SenseVoice ``'그.'``, Paraformer
        ``'嗯'``/``'嗯好的'``), and an empty result is what the dialogue flow
        already treats as "nothing was said".
        """
        if samples_int16 is None or len(samples_int16) == 0:
            return ""
        stream = self.recognizer.create_stream()
        stream.accept_waveform(16000, self._agc(samples_int16))
        self.recognizer.decode_stream(stream)
        result = stream.result
        text = (result.text or "").strip()
        self.last_language = _clean_tag(getattr(result, "lang", ""))
        lang = f" lang={self.last_language}" if self.last_language else ""
        if content_length(text) <= 1:
            LOG.info("ASR_RESULT%s text=%s (dropped: no speech)", lang, text or "(empty)")
            return ""
        LOG.info("ASR_RESULT%s text=%s", lang, text)
        return text

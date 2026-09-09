#!/usr/bin/env python3
"""Local Chinese speech recognition via sherpa-onnx (Paraformer).

Model layout expected under the model directory (from the official
k2-fsa sherpa-onnx release `sherpa-onnx-paraformer-zh-small-2024-03-09`):

    model.int8.onnx   tokens.txt

Download script for this dependency lives in scripts/download_asr_model.sh.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

LOG = logging.getLogger("dialogue.asr")


class LocalAsr:
    """Offline Paraformer recognizer wrapper (16 kHz mono int16 in)."""

    def __init__(self, model_dir: str | Path, num_threads: int = 4):
        model_dir = Path(model_dir)
        model_path = model_dir / "model.int8.onnx"
        if not model_path.is_file():
            model_path = model_dir / "model.onnx"
        tokens_path = model_dir / "tokens.txt"
        if not model_path.is_file() or not tokens_path.is_file():
            raise FileNotFoundError(
                f"ASR model files missing in {model_dir}: need "
                f"model.int8.onnx/model.onnx and tokens.txt "
                "(run scripts/download_asr_model.sh)"
            )
        import sherpa_onnx  # lazy import: only needed when dialogue is on

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(model_path),
            tokens=str(tokens_path),
            num_threads=num_threads,
            sample_rate=16000,
            decoding_method="greedy_search",
            provider="cpu",
        )
        LOG.info("ASR_READY model=%s threads=%d", model_dir, num_threads)

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
        """samples_int16: 1-D numpy int16 array at 16 kHz mono."""
        if samples_int16 is None or len(samples_int16) == 0:
            return ""
        stream = self.recognizer.create_stream()
        stream.accept_waveform(16000, self._agc(samples_int16))
        self.recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        LOG.info("ASR_RESULT text=%s", text or "(empty)")
        return text

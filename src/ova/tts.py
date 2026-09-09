import urllib.error
import urllib.request
"""Qwen speech synthesis client (qwen3-tts-flash, native DashScope API)."""

import io
import os
import wave

import numpy as np

from ova.api import CloudError, http_json

TTS_URL = os.getenv("TTS_URL", "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
TTS_MODEL = os.getenv("TTS_MODEL", "qwen3-tts-flash")
TTS_VOICE = os.getenv("TTS_VOICE", "Cherry")


def _resample_mono16(x: np.ndarray, src_rate: int) -> np.ndarray:
    """Linear-interpolation resample of int16 mono samples (fast, adequate)."""
    if src_rate == 16000:
        return x
    n_out = int(round(len(x) * 16000 / src_rate))
    idx = np.linspace(0, len(x) - 1, n_out)
    lo = idx.astype(np.int64)
    hi = np.minimum(lo + 1, len(x) - 1)
    frac = (idx - lo).astype(np.float32)
    return (x[lo].astype(np.float32) * (1 - frac)
            + x[hi].astype(np.float32) * frac).astype(np.int16)


def synthesize(text: str, timeout: float = 60.0) -> bytes:
    """Synthesize speech; returns a 16 kHz stereo 16-bit WAV payload."""
    payload = {
        "model": TTS_MODEL,
        "input": {"text": text, "voice": TTS_VOICE},
    }
    data = http_json(TTS_URL, payload, timeout)
    try:
        url = data["output"]["audio"]["url"]
    except (KeyError, TypeError) as exc:
        raise CloudError(f"unexpected tts response: {str(data)[:200]}") from exc
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.URLError as exc:
        raise CloudError(f"tts audio download failed: {exc.reason}") from exc
    src_rate = 24000
    with wave.open(io.BytesIO(raw)) as wav:
        src_rate = wav.getframerate()
        if wav.getsampwidth() != 2:
            raise CloudError("tts audio is not 16-bit PCM")
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        if wav.getnchannels() > 1:
            samples = samples.reshape(-1, wav.getnchannels()).mean(axis=1).astype(np.int16)
    mono16 = _resample_mono16(samples, src_rate)
    stereo = np.repeat(mono16, 2)
    buf = io.BytesIO()
    with wave.open(buf, "w") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(stereo.tobytes())
    return buf.getvalue()

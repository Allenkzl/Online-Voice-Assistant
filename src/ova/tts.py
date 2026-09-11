import urllib.error
import urllib.request
"""Qwen speech synthesis client (qwen3-tts-flash, native DashScope API)."""

import io
import os
import wave

import numpy as np

from ova.api import CloudError, http_json
from ova.audio import linear_resample, wav_bytes

TTS_URL = os.getenv("TTS_URL", "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
TTS_MODEL = os.getenv("TTS_MODEL", "qwen3-tts-flash")
TTS_VOICE = os.getenv("TTS_VOICE", "Cherry")


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
    mono16 = linear_resample(samples, src_rate, 16000)
    return wav_bytes(np.repeat(mono16, 2), rate=16000, channels=2)

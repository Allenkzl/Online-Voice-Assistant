import urllib.error
import urllib.request
"""Qwen speech synthesis client (qwen3-tts-flash, native DashScope API)."""

import io
import re
from concurrent.futures import ThreadPoolExecutor
import os
import wave

import numpy as np

from ova.api import CloudError, http_json
from ova.audio import linear_resample, wav_bytes

TTS_URL = os.getenv("TTS_URL", "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
TTS_MODEL = os.getenv("TTS_MODEL", "qwen3-tts-flash")
TTS_VOICE = os.getenv("TTS_VOICE", "Cherry")
# 单次合成请求超时（秒）。原来写死 60s：云端卡住时访客要干等一分钟才听到兜底音。
TTS_TIMEOUT_S = float(os.getenv("TTS_TIMEOUT_S", "60"))
# 多句回答按句并行合成再拼接，总耗时≈最长一句而不是各句之和；0/1 = 关闭。
TTS_PARALLEL = int(os.getenv("TTS_PARALLEL", "4"))
SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+")


def split_sentences(text: str, min_chars: int = 6) -> list[str]:
    """Split a reply at sentence ends; merge tiny fragments into the previous."""
    parts: list[str] = []
    for piece in SENTENCE_RE.split(text):
        piece = (piece or "").strip()
        if not piece:
            continue
        if parts and len(piece) < min_chars:
            parts[-1] = parts[-1] + (" " if piece[0].isascii() else "") + piece
        else:
            parts.append(piece)
    if len(parts) > 1 and len(parts[0]) < min_chars:   # 开头的「你好。」并到下一句
        first = parts.pop(0)
        parts[0] = first + (" " if first.isascii() else "") + parts[0]
    return parts or [text]


def synthesize(text: str, timeout: float | None = None,
               parallel: int | None = None) -> bytes:
    """Synthesize speech; returns a 16 kHz stereo 16-bit WAV payload.

    Multi-sentence text is synthesized sentence by sentence in parallel and
    concatenated (same voice, same format), so latency tracks the longest
    sentence instead of the whole reply. Any sentence failing fails the call.
    """
    timeout = TTS_TIMEOUT_S if timeout is None else timeout
    parallel = TTS_PARALLEL if parallel is None else parallel
    parts = split_sentences(text) if parallel > 1 else [text]
    if len(parts) == 1:
        mono16 = _synthesize_mono16(text, timeout)
    else:
        with ThreadPoolExecutor(max_workers=min(parallel, len(parts))) as pool:
            mono16 = np.concatenate(
                list(pool.map(lambda t: _synthesize_mono16(t, timeout), parts)))
    return wav_bytes(np.repeat(normalise_peak(mono16), 2), rate=16000, channels=2)


def normalise_peak(mono16: np.ndarray) -> np.ndarray:
    """峰值归一到 -1 dBFS：展厅 ALSA 已在 0dB 上限，响度只能在内容层补。

    整段回答统一归一（而非逐句），各句音量保持一致。
    """
    peak = int(np.abs(mono16).max()) if mono16.size else 0
    if peak < 1000:
        return mono16
    return ((mono16.astype(np.float32) * (29127.0 / peak))
            .round().clip(-32768, 32767).astype(np.int16))


def _synthesize_mono16(text: str, timeout: float) -> np.ndarray:
    """One TTS request -> 16 kHz mono int16 samples."""
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
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise CloudError(f"tts audio download failed: {exc}") from exc
    src_rate = 24000
    with wave.open(io.BytesIO(raw)) as wav:
        src_rate = wav.getframerate()
        if wav.getsampwidth() != 2:
            raise CloudError("tts audio is not 16-bit PCM")
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        if wav.getnchannels() > 1:
            samples = samples.reshape(-1, wav.getnchannels()).mean(axis=1).astype(np.int16)
    return linear_resample(samples, src_rate, 16000)

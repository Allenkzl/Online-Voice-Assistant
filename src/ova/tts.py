"""Qwen speech synthesis client (qwen3-tts-flash, native DashScope API)."""

import base64
import http.client
import io
import json
import urllib.error
import urllib.request
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
# 流式合成没法做整段峰值归一，用固定增益 + 限幅（典型 qwen3-tts 峰值约 -6 dBFS）。
TTS_STREAM_GAIN = float(os.getenv("TTS_STREAM_GAIN", "1.4"))
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


class StreamResampler:
    """Stateful linear resampler: feed chunks, get continuous output.

    Keeps the fractional read position and the last source sample between
    chunks, so chunk boundaries do not click.
    """

    def __init__(self, src_rate: int, dst_rate: int = 16000):
        self.step = src_rate / dst_rate
        self.pos = 0.0
        self.tail = np.zeros(0, dtype=np.float32)

    def feed(self, chunk: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.tail, chunk.astype(np.float32)])
        if len(buf) < 2:
            self.tail = buf
            return np.zeros(0, dtype=np.float32)
        n = int(np.floor((len(buf) - 1 - self.pos) / self.step)) + 1
        idx = self.pos + self.step * np.arange(max(n, 0))
        out = np.interp(idx, np.arange(len(buf)), buf)
        nxt = self.pos + self.step * max(n, 0)
        drop = int(np.floor(nxt))
        self.tail = buf[drop:]
        self.pos = nxt - drop
        return out


def _pcm_from_event(raw: bytes, state: dict) -> np.ndarray:
    """Decode one SSE audio chunk; the first one carries a RIFF/WAV header."""
    buf = state.get("carry", b"") + raw
    if not state.get("header_done"):
        if buf[:4] == b"RIFF":
            idx = buf.find(b"data", 12)
            if idx < 0:
                state["carry"] = buf
                return np.zeros(0, dtype=np.int16)
            state["rate"] = int.from_bytes(buf[24:28], "little") or 24000
            state["channels"] = int.from_bytes(buf[22:24], "little") or 1
            buf = buf[idx + 8:]
        state["header_done"] = True
    width = 2 * state.get("channels", 1)
    keep = len(buf) - len(buf) % width
    state["carry"] = buf[keep:]
    samples = np.frombuffer(buf[:keep], dtype="<i2")
    if state.get("channels", 1) > 1:
        samples = samples.reshape(-1, state["channels"]).mean(axis=1)
    return samples


def stream_pcm16(text: str, timeout: float | None = None,
                 gain: float | None = None):
    """Streaming TTS: yield 16 kHz mono int16 chunks as soon as they arrive.

    DashScope SSE (``X-DashScope-SSE: enable``) sends base64 audio pieces;
    the first piece is a WAV header + PCM, the rest raw PCM. Closing the
    generator closes the HTTP connection (barge-in / inject stop it early;
    billing is by input characters, so this saves time, not money).
    """
    timeout = TTS_TIMEOUT_S if timeout is None else timeout
    gain = TTS_STREAM_GAIN if gain is None else gain
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        raise CloudError("DASHSCOPE_API_KEY is not set")
    payload = {"model": TTS_MODEL, "input": {"text": text, "voice": TTS_VOICE}}
    req = urllib.request.Request(
        TTS_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "X-DashScope-SSE": "enable"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise CloudError(f"HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CloudError(f"tts stream failed: {exc}") from exc
    state: dict = {}
    resampler = None
    try:
        for line in resp:
            if not line.startswith(b"data:"):
                continue
            body = line[5:].strip()
            if not body:
                continue
            try:
                event = json.loads(body)
            except ValueError:
                continue
            if event.get("code") and not event.get("output"):
                raise CloudError(f"tts stream error: {str(event)[:200]}")
            output = event.get("output") or {}
            audio = (output.get("audio") or {}).get("data")
            if not audio:
                if output.get("finish_reason") == "stop" or (output.get("audio") or {}).get("url"):
                    return          # 末包：服务端随后会直接断流
                continue
            pcm = _pcm_from_event(base64.b64decode(audio), state)
            if resampler is None and state.get("header_done"):
                resampler = StreamResampler(state.get("rate", 24000))
            if resampler is None or not len(pcm):
                continue
            out = resampler.feed(pcm) * gain
            if len(out):
                yield out.round().clip(-32768, 32767).astype(np.int16)
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        raise CloudError(f"tts stream read failed: {exc}") from exc
    finally:
        resp.close()

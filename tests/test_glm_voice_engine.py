#!/usr/bin/env python3
"""GLM-4-Voice engine tests — completely offline (HTTP is stubbed).

Covers the things that are easy to get wrong: the request shape (base64 WAV +
persona), decoding the **raw PCM** reply, resampling 44.1 kHz -> device format,
usage/cost accounting and error classification.

    python3 tests/test_glm_voice_engine.py
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("OVA_HOME", str(ROOT))

from ova.audio import (device_wav_bytes, linear_resample, mono16k_wav_bytes,  # noqa: E402
                       normalize_loudness)
from ova.engines import build_engine                                         # noqa: E402
from ova.engines import glm_voice as glm                                     # noqa: E402
from ova.engines.base import EngineError                                     # noqa: E402


@contextlib.contextmanager
def patch(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def no_api_key():
    saved = os.environ.pop("ZHIPUAI_API_KEY", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["ZHIPUAI_API_KEY"] = saved


class FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def pcm_b64(seconds: float = 1.0, rate: int = 44100) -> str:
    t = np.arange(int(seconds * rate)) / rate
    tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2")
    return base64.b64encode(tone.tobytes()).decode("ascii")


def canned_reply(text: str = "欢迎来到智慧零售区。", audio: str | None = None,
                 usage: dict | None = None) -> dict:
    message: dict = {"role": "assistant", "content": text}
    if audio is not None:
        message["audio"] = {"data": audio, "id": "abc", "expires_at": 0}
    return {"choices": [{"index": 0, "message": message,
                         "finish_reason": "stop"}],
            "usage": usage if usage is not None else
            {"prompt_tokens": 107, "completion_tokens": 340, "total_tokens": 447}}


def capture_call(payload: dict):
    """Return (fake_urlopen, holder) recording the outgoing request body."""
    holder: dict = {}

    def fake_urlopen(req, timeout=None):
        holder["url"] = req.full_url
        holder["headers"] = dict(req.headers)
        holder["body"] = json.loads(req.data.decode("utf-8"))
        holder["timeout"] = timeout
        return FakeResponse(payload)

    return fake_urlopen, holder


def samples_16k(seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(seconds * 16000)) / 16000
    return (np.sin(2 * np.pi * 220 * t) * 6000).astype(np.int16)


# --- factory wiring ---------------------------------------------------------

def test_factory_builds_e2e_engine():
    engine = build_engine({"engine": "e2e"})
    assert engine.name == "e2e"
    assert engine.needs_transcript is False        # no local ASR is consulted
    assert isinstance(engine, glm.GlmVoiceEngine)


def test_config_and_env_override_defaults():
    with patch(os, environ={**os.environ, "GLM_VOICE_PERSONA": "env persona"}):
        assert glm.GlmVoiceEngine().persona == "env persona"
    cfg = {"glm_voice_persona": "cfg persona", "glm_voice_pcm_rate": 24000}
    engine = glm.GlmVoiceEngine(cfg)
    assert engine.persona == "cfg persona"
    assert engine.pcm_rate == 24000


# --- request shape ----------------------------------------------------------

def test_request_carries_base64_wav_and_persona():
    fake, holder = capture_call(canned_reply(audio=pcm_b64(0.5)))
    engine = glm.GlmVoiceEngine({"glm_voice_persona": "只回答一句话"})
    with patch(urllib.request, urlopen=fake), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "test-key"}):
        reply = engine.respond(samples_16k(0.8), "", {})

    body = holder["body"]
    assert body["model"] == "glm-4-voice" and body["stream"] is False
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "只回答一句话"}
    assert content[1]["input_audio"]["format"] == "wav"
    assert holder["headers"]["Authorization"] == "Bearer test-key"
    assert holder["timeout"] == engine.timeout_s

    # the uploaded audio must be a 16 kHz *mono* WAV of the right length
    uploaded = base64.b64decode(content[1]["input_audio"]["data"])
    with wave.open(str(_write_tmp(uploaded)), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert abs(w.getnframes() / 16000 - 0.8) < 0.02
    reply.audio_path.unlink(missing_ok=True)


def test_transcript_is_appended_when_already_known():
    fake, holder = capture_call(canned_reply(audio=pcm_b64(0.2)))
    engine = glm.GlmVoiceEngine({})
    with patch(urllib.request, urlopen=fake), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "k"}):
        reply = engine.respond(samples_16k(0.3), "介绍一下应急救灾", {})
    prompt = holder["body"]["messages"][0]["content"][0]["text"]
    assert "介绍一下应急救灾" in prompt
    reply.audio_path.unlink(missing_ok=True)


# --- reply decoding ---------------------------------------------------------

def test_reply_audio_is_resampled_to_device_format():
    fake, _ = capture_call(canned_reply(audio=pcm_b64(2.0)))
    engine = glm.GlmVoiceEngine({})
    with patch(urllib.request, urlopen=fake), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "k"}):
        reply = engine.respond(samples_16k(), "", {})

    with wave.open(str(reply.audio_path)) as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 2               # aplay expects stereo here
        assert w.getsampwidth() == 2
        assert abs(w.getnframes() / 16000 - 2.0) < 0.05
    assert reply.text == "欢迎来到智慧零售区。"
    assert reply.temporary is True                  # playback loop cleans it up
    assert reply.timeout_s == 180.0
    reply.audio_path.unlink(missing_ok=True)


def test_meta_reports_latency_tokens_and_cost():
    fake, _ = capture_call(canned_reply(audio=pcm_b64(1.5)))
    engine = glm.GlmVoiceEngine({})
    with patch(urllib.request, urlopen=fake), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "k"}):
        reply = engine.respond(samples_16k(), "", {})

    meta = reply.meta
    for key in ("encode_s", "request_s", "convert_s", "elapsed_s", "audio_s",
                "tokens", "cost_cny"):
        assert key in meta, key
    assert meta["provider"] == "glm-4-voice"
    assert meta["tokens"] == 447
    assert abs(meta["cost_cny"] - 447 * 80 / 1e6) < 1e-9
    assert abs(meta["audio_s"] - 1.5) < 0.05
    assert reply.lang == "zh"
    reply.audio_path.unlink(missing_ok=True)


def test_missing_usage_is_tolerated():
    fake, _ = capture_call({"choices": [{"message": {
        "content": "hello", "audio": {"data": pcm_b64(0.2)}}}]})
    engine = glm.GlmVoiceEngine({})
    with patch(urllib.request, urlopen=fake), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "k"}):
        reply = engine.respond(samples_16k(0.2), "", {})
    assert reply.meta["tokens"] is None and reply.meta["cost_cny"] is None
    assert reply.lang == "en"
    reply.audio_path.unlink(missing_ok=True)


# --- error classification ---------------------------------------------------

def test_missing_api_key_is_reported_clearly():
    engine = glm.GlmVoiceEngine({})
    with no_api_key():
        try:
            engine.respond(samples_16k(0.1), "", {})
        except EngineError as exc:
            assert "ZHIPUAI_API_KEY" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected EngineError")


def test_http_error_is_mapped():
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {}, None)

    engine = glm.GlmVoiceEngine({})
    with patch(urllib.request, urlopen=boom), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "k"}):
        try:
            engine.respond(samples_16k(0.1), "", {})
        except EngineError as exc:
            assert "HTTP 429" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected EngineError")


def test_network_error_is_mapped():
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    engine = glm.GlmVoiceEngine({})
    with patch(urllib.request, urlopen=boom), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "k"}):
        try:
            engine.respond(samples_16k(0.1), "", {})
        except EngineError as exc:
            assert "network error" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected EngineError")


def test_reply_without_audio_is_an_error():
    fake, _ = capture_call(canned_reply(audio=None))
    engine = glm.GlmVoiceEngine({})
    with patch(urllib.request, urlopen=fake), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "k"}):
        try:
            engine.respond(samples_16k(0.1), "", {})
        except EngineError as exc:
            assert "no audio" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected EngineError")


def test_garbage_audio_payload_is_an_error():
    fake, _ = capture_call(canned_reply(audio="!!!not-base64!!!"))
    engine = glm.GlmVoiceEngine({})
    with patch(urllib.request, urlopen=fake), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "k"}):
        try:
            engine.respond(samples_16k(0.1), "", {})
        except EngineError as exc:
            assert "base64" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected EngineError")


def test_unexpected_response_shape_is_an_error():
    fake, _ = capture_call({"error": {"message": "bad request"}})
    engine = glm.GlmVoiceEngine({})
    with patch(urllib.request, urlopen=fake), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "k"}):
        try:
            engine.respond(samples_16k(0.1), "", {})
        except EngineError as exc:
            assert "unexpected response" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected EngineError")


# --- audio helpers ----------------------------------------------------------

def test_device_wav_is_poly_resampled_stereo():
    src = samples_16k(1.0)
    upsampled = np.repeat(src, 3)[: int(44100 * 1.0)]       # stand-in for 44.1k
    wav = device_wav_bytes(upsampled, 44100)
    path = _write_tmp(wav)
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 2
        assert abs(w.getnframes() / 16000 - 1.0) < 0.05
    path.unlink()


def test_linear_resample_matches_expected_length():
    x = np.arange(24000, dtype=np.int16)
    y = linear_resample(x, 24000, 16000)
    assert len(y) == 16000
    assert linear_resample(x, 16000, 16000) is x


def test_mono16k_wav_downsamples_stereo():
    stereo = np.stack([np.arange(1600, dtype=np.int16)] * 2, axis=1)
    wav = mono16k_wav_bytes(stereo)
    path = _write_tmp(wav)
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getnframes() == 1600
    path.unlink()


# --- helpers -----------------------------------------------------------------

def _write_tmp(data: bytes) -> Path:
    import tempfile

    fd, name = tempfile.mkstemp(suffix=".wav")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return Path(name)



# --- loudness normalisation -------------------------------------------------

def test_normalize_lifts_a_quiet_reply_to_the_tts_level():
    quiet = (np.sin(np.linspace(0, 40, 8000)) * 1500).astype(np.int16)   # ~0.046 RMS
    out, gain = normalize_loudness(quiet, target_rms=0.09)
    rms = float(np.sqrt(np.mean((out.astype(np.float32) / 32768.0) ** 2)))
    assert gain > 1.5
    assert 0.07 < rms < 0.11            # lands near the qwen3-tts level
    assert np.max(np.abs(out)) < 32768  # no clipping


def test_normalize_does_not_clip_a_loud_reply():
    loud = (np.sin(np.linspace(0, 40, 8000)) * 32000).astype(np.int16)
    out, gain = normalize_loudness(loud, target_rms=0.09)
    assert gain <= 1.0
    assert np.max(np.abs(out)) <= 32767


def test_normalize_leaves_silence_alone():
    silence = np.zeros(1600, dtype=np.int16)
    out, gain = normalize_loudness(silence)
    assert gain == 1.0 and np.array_equal(out, silence)


def test_engine_normalises_and_reports_gain():
    quiet_pcm = base64.b64encode(
        (np.sin(np.linspace(0, 200, 44100)) * 1200).astype("<i2").tobytes()).decode()
    fake, _ = capture_call(canned_reply(audio=quiet_pcm))
    engine = glm.GlmVoiceEngine({})
    with patch(urllib.request, urlopen=fake), \
            patch(os, environ={**os.environ, "ZHIPUAI_API_KEY": "k"}):
        reply = engine.respond(samples_16k(0.5), "", {})
    assert reply.meta["gain"] > 1.5
    with wave.open(str(reply.audio_path)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    rms = float(np.sqrt(np.mean((x / 32768.0) ** 2)))
    assert 0.06 < rms < 0.12            # audible, close to the TTS reference
    reply.audio_path.unlink(missing_ok=True)


# --- direct runner (no pytest required) -------------------------------------

def _main() -> int:
    import traceback

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception:  # noqa: BLE001 - report and keep going
            failed += 1
            print(f"FAIL: {test.__name__}")
            traceback.print_exc()
        else:
            print(f"PASS: {test.__name__}")
    print(f"\n{'FAILED' if failed else 'OK'}: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())

"""Cloud ASR fallback + parallel sentence TTS (no network, no hardware)."""

import io
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ova import tts  # noqa: E402
from ova.api import CloudError  # noqa: E402
from ova.asr_cloud import CloudAsr  # noqa: E402

SPEECH = (np.sin(np.arange(16000) / 5) * 8000).astype(np.int16)


def test_cloud_asr_parses_text_and_language():
    def post(url, payload, timeout):
        assert payload["model"] == "qwen3-asr-flash"
        assert payload["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,")
        return {"choices": [{"message": {"content": "介绍一下智慧空间",
                "annotations": [{"type": "audio_info", "language": "zh"}]}}]}
    asr = CloudAsr(None, post=post)
    assert asr.transcribe(SPEECH) == "介绍一下智慧空间"
    assert asr.last_language == "zh"


def test_cloud_asr_drops_noise_token():
    asr = CloudAsr(None, post=lambda *a: {"choices": [{"message": {"content": "嗯。"}}]})
    assert asr.transcribe(SPEECH) == ""


def test_cloud_asr_falls_back_to_local():
    class FakeLocal:
        last_language = "en"
        def transcribe(self, samples):
            return "hello"
    def post(*a):
        raise CloudError("network error: timed out")
    asr = CloudAsr(None, post=post)
    asr._local = FakeLocal()
    assert asr.transcribe(SPEECH) == "hello"
    assert asr.last_language == "en"


def test_cloud_asr_failure_without_local_is_silence():
    def post(*a):
        raise TimeoutError("read timed out")
    assert CloudAsr(None, post=post).transcribe(SPEECH) == ""


def test_split_sentences():
    assert tts.split_sentences("你好。我是贾维斯，展区导览机器人！智慧空间在左手边。好的。") == [
        "你好。我是贾维斯，展区导览机器人！", "智慧空间在左手边。好的。"]
    assert tts.split_sentences("Hi. I am Jarvis. Ask me anything.") == ["Hi. I am Jarvis.", "Ask me anything."]
    assert tts.split_sentences("没有句号") == ["没有句号"]


def test_parallel_tts_concatenates_in_order(monkeypatch):
    calls = []
    def fake(text, timeout):
        calls.append(text)
        time.sleep(0.2)
        return np.full(100, len(text), dtype=np.int16)
    monkeypatch.setattr(tts, "_synthesize_mono16", fake)
    t0 = time.monotonic()
    wav = tts.synthesize("第一句话说完了。第二句话也说完了。第三句话结束了。", parallel=4)
    assert time.monotonic() - t0 < 0.5          # parallel, not 3 x 0.2s
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getframerate()) == (2, 16000)
        mono = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")[::2]
    assert list(np.sign(mono[::100])) == [1, 1, 1] and mono[0] < mono[100]  # order kept
    assert len(calls) == 3


def test_tts_failure_propagates(monkeypatch):
    def fake(text, timeout):
        raise CloudError("boom")
    monkeypatch.setattr(tts, "_synthesize_mono16", fake)
    try:
        tts.synthesize("一句。两句话。")
    except CloudError:
        return
    raise AssertionError("expected CloudError")

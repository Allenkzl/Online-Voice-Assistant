"""Streaming TTS decode/resample + playback barge-in routing (no network, no hardware)."""

import base64
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ova import dialogue, tts  # noqa: E402
from ova.engines.base import Reply  # noqa: E402


def _wav_header(rate=24000, channels=1):
    return (b"RIFF" + (0).to_bytes(4, "little") + b"WAVEfmt "
            + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
            + channels.to_bytes(2, "little") + rate.to_bytes(4, "little")
            + (rate * 2 * channels).to_bytes(4, "little")
            + (2 * channels).to_bytes(2, "little") + (16).to_bytes(2, "little")
            + b"data" + (0).to_bytes(4, "little"))


def test_resampler_chunked_equals_whole():
    src = (np.sin(np.arange(24000) / 7) * 10000).astype(np.int16)
    whole = tts.StreamResampler(24000).feed(src)
    r = tts.StreamResampler(24000)
    parts = np.concatenate([r.feed(c) for c in np.array_split(src, 37)])
    assert abs(len(parts) - 16000) <= 1
    n = min(len(whole), len(parts))
    assert np.allclose(whole[:n], parts[:n], atol=1e-3)


def test_pcm_event_strips_header_and_carries_odd_byte():
    pcm = np.arange(10, dtype="<i2").tobytes()
    state = {}
    first = tts._pcm_from_event(_wav_header() + pcm[:5], state)
    rest = tts._pcm_from_event(pcm[5:], state)
    assert state["rate"] == 24000
    assert list(np.concatenate([first, rest])) == list(range(10))


def test_stream_pcm16_parses_sse(monkeypatch):
    pcm = (np.ones(2400) * 1000).astype("<i2").tobytes()
    chunks = [_wav_header() + pcm[:2000], pcm[2000:]]
    lines = [b"id:1\n"] + [
        b"data:" + json.dumps({"output": {"audio": {"data": base64.b64encode(c).decode()}}}).encode() + b"\n"
        for c in chunks] + [b'data:{"output":{"audio":{"data":"","url":"x"}}}\n']

    class Resp(list):
        def close(self):
            self.closed = True
    resp = Resp(lines)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(tts.urllib.request, "urlopen", lambda req, timeout: resp)
    out = np.concatenate(list(tts.stream_pcm16("你好", gain=2.0)))
    assert abs(len(out) - 1600) <= 2
    assert int(out[10]) == 2000
    assert resp.closed


class _Proc:
    def __init__(self):
        self.stdin = self
        self.data = bytearray()
        self.closed = False
    def write(self, b):
        self.data += b
    def flush(self):
        pass
    def close(self):
        self.closed = True


def test_feeder_writes_stereo_and_log_wav(tmp_path):
    import threading
    proc = _Proc()
    chunks = [np.full(160, 5, np.int16), np.full(160, 7, np.int16)]
    f = dialogue._StreamFeeder(proc, iter(chunks), threading.Event())
    f.thread.join(2)
    assert proc.closed and len(proc.data) == 320 * 4 and f.samples == 320
    out = tmp_path / "r.wav"
    f.write_wav(out)
    with wave.open(str(out)) as w:
        assert w.getnchannels() == 2 and w.getnframes() == 320


def test_reply_state_marks_vad_barge_only_when_enabled():
    class E:
        name = "pipeline"
    r = Reply(audio_path=Path("/tmp/x.wav"), text="hi", stream=iter([]))
    on = dialogue._state_from_reply(E(), r, {"barge_in_vad": True})
    off = dialogue._state_from_reply(E(), r, {})
    assert on.barge_vad and on.stream is not None and not off.barge_vad


def test_vad_barge_routes_speech_as_next_question(monkeypatch, tmp_path):
    first = dialogue.PlaybackState(path=tmp_path / "a.wav", name="回答")
    second = dialogue.PlaybackState(path=tmp_path / "b.wav", name="回答")
    speech = np.ones(8000, np.int16)
    results = iter([
        dialogue.PlaybackResult(completed=False, interrupted=True, elapsed_s=1.0,
                                barge_samples=speech),
        dialogue.PlaybackResult(completed=True, interrupted=False, elapsed_s=2.0),
    ])
    monkeypatch.setattr(dialogue, "play_interruptible", lambda b, s, c: next(results))
    seen = {}

    def route(backend, root, asr, cfg, text, samples=None, previous=None, engine=None, **kw):
        seen.update(text=text, samples=samples, previous=previous)
        return second
    monkeypatch.setattr(dialogue, "_playback_from_text", route)
    monkeypatch.setattr(dialogue, "listen_command_after_barge_in",
                        lambda *a: (_ for _ in ()).throw(AssertionError("no command window")))
    monkeypatch.setattr(dialogue, "_write_motion_phase", lambda *a, **k: None)

    class Asr:
        def transcribe(self, s):
            return "今天天气怎么样"

    class Eng:
        name = "pipeline"
        needs_transcript = True
    dialogue._run_playback_loop(None, tmp_path, Asr(), {}, first, engine=Eng())
    assert seen["text"] == "今天天气怎么样" and seen["samples"] is speech
    assert seen["previous"] is None


def test_vad_false_trigger_resumes_reply(monkeypatch, tmp_path):
    state = dialogue.PlaybackState(path=tmp_path / "a.wav", name="回答")
    results = iter([
        dialogue.PlaybackResult(completed=False, interrupted=True, elapsed_s=1.5,
                                barge_samples=np.ones(800, np.int16)),
        dialogue.PlaybackResult(completed=True, interrupted=False, elapsed_s=9.0),
    ])
    played = []

    def play(b, s, c):
        played.append((s, s.offset_s))
        return next(results)
    monkeypatch.setattr(dialogue, "play_interruptible", play)
    monkeypatch.setattr(dialogue, "_playback_from_text",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must resume")))

    class Asr:
        def transcribe(self, s):
            return ""

    class Eng:
        name = "pipeline"
        needs_transcript = True
    dialogue._run_playback_loop(None, tmp_path, Asr(), {}, state, engine=Eng())
    assert [p[0] for p in played] == [state, state] and played[1][1] == 1.5


def test_feeder_mute_keeps_receiving(tmp_path):
    import threading
    proc = _Proc()
    gate = threading.Event()

    def gen():
        yield np.full(160, 1, np.int16)
        gate.wait(2)
        yield np.full(160, 2, np.int16)
    f = dialogue._StreamFeeder(proc, gen(), threading.Event())
    f.mute()
    gate.set()
    f.thread.join(2)
    assert f.samples == 320 and len(proc.data) <= 160 * 4

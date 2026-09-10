#!/usr/bin/env python3
"""Bilingual ASR smoke tests.

Run either with pytest or directly:

    OVA_ASR_MODEL_DIR=models/asr_sense_voice_zh_en_int8 python3 tests/test_asr_bilingual.py

The model-type detection tests always run. The recognition tests need a real
model directory plus the fixture WAVs and are skipped (not failed) when the
model has not been downloaded yet — model binaries are never committed.
"""

from __future__ import annotations

import os
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:  # allow running without `pip install -e .`
    sys.path.insert(0, str(SRC))

os.environ.setdefault("OVA_HOME", str(ROOT))

from ova.asr import LocalAsr, content_length, detect_model_type  # noqa: E402
from ova.solutions import match_solution_intro  # noqa: E402

FIXTURES = {
    "asr_en_smart_retail.wav": ("en", ["smart retail", "welcome"], "en"),
    "asr_zh_smart_retail.wav": ("zh", ["智慧零售"], "zh"),
    # A Chinese sentence containing the English trigger: routed via aliases_en.
    "asr_mixed_smart_retail.wav": ("zh", ["smart retail"], "en"),
}
MODEL_CANDIDATES = (
    "models/asr_sense_voice_zh_en_int8",
    "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
    "models/asr_paraformer_zh_int8",
    "models/asr_paraformer_zh_small",
)


def _model_dir() -> Path:
    env = os.getenv("OVA_ASR_MODEL_DIR")
    if env:
        return Path(env)
    for candidate in MODEL_CANDIDATES:
        path = ROOT / candidate
        if path.is_dir():
            return path
    return ROOT / MODEL_CANDIDATES[0]


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as f:
        assert f.getframerate() == 16000, f"{path}: expected 16 kHz"
        data = f.readframes(f.getnframes())
    return np.frombuffer(data, dtype=np.int16).copy()


def _normalise(text: str) -> str:
    return "".join(text.lower().split())


# --- model detection (no model files needed) -------------------------------

def test_detect_sense_voice(tmp_path):
    (tmp_path / "tokens.txt").write_text("<blk> 0\n<|zh|> 1\n<|en|> 2\n<|Speech|> 3\n", encoding="utf-8")
    (tmp_path / "model.int8.onnx").write_bytes(b"stub")
    assert detect_model_type(tmp_path) == "sense_voice"


def test_detect_paraformer(tmp_path):
    (tmp_path / "tokens.txt").write_text("<blk> 0\n智 1\n慧 2\n", encoding="utf-8")
    (tmp_path / "model.onnx").write_bytes(b"stub")
    assert detect_model_type(tmp_path) == "paraformer"


def test_detect_transducer(tmp_path):
    (tmp_path / "tokens.txt").write_text("a 0\n", encoding="utf-8")
    for name in ("encoder-epoch-99.onnx", "decoder-epoch-99.onnx", "joiner-epoch-99.onnx"):
        (tmp_path / name).write_bytes(b"stub")
    assert detect_model_type(tmp_path) == "transducer"


def test_detect_missing_model_raises(tmp_path):
    try:
        detect_model_type(tmp_path)
    except FileNotFoundError as exc:
        assert "download_models.sh" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected FileNotFoundError for an empty model dir")


def test_content_length_ignores_punctuation():
    assert content_length("그. ") == 1
    assert content_length("嗯") == 1
    assert content_length("停止") == 2
    assert content_length("stop！") == 4
    assert content_length("。，！？") == 0


# --- recognition + routing (needs the real model) --------------------------

def test_no_speech_returns_empty():
    """Silence must not become a phantom command (both models hallucinate)."""
    model_dir = _model_dir()
    if not model_dir.is_dir():
        print(f"SKIP: no ASR model in {model_dir}")
        return
    silence = ROOT / "tests" / "silence.wav"
    if not silence.is_file():
        print("SKIP: missing tests/silence.wav")
        return
    asr = LocalAsr(model_dir)
    assert asr.transcribe(np.zeros(16000 * 3, dtype=np.int16)) == ""
    assert asr.transcribe(_read_wav(silence)) == ""

def test_transcribe_bilingual_and_route():
    model_dir = _model_dir()
    if not model_dir.is_dir():
        print(f"SKIP: no ASR model in {model_dir} (run scripts/download_models.sh)")
        return
    missing = [name for name in FIXTURES if not (ROOT / "tests" / name).is_file()]
    if missing:
        print(f"SKIP: missing fixtures {missing}")
        return

    asr = LocalAsr(model_dir)
    print(f"model={model_dir.name} type={asr.model_type}")

    for name, (lang, must_contain, intro_lang) in FIXTURES.items():
        text = asr.transcribe(_read_wav(ROOT / "tests" / name))
        print(f"  {name}: {text!r} (lang={asr.last_language or '-'})")
        assert text, f"{name}: empty transcription"
        normalised = _normalise(text)
        for needle in must_contain:
            assert _normalise(needle) in normalised, f"{name}: {needle!r} not in {text!r}"
        if asr.model_type == "sense_voice":
            assert asr.last_language == lang, (
                f"{name}: detected language {asr.last_language!r} != {lang!r}")

        # Every trigger — Chinese, English and mixed — must reach the pre-generated
        # intro before today's Chinese-only model made the English path unreachable.
        intro = match_solution_intro(text)
        assert intro is not None, f"{name}: command did not route: {text!r}"
        assert intro.id == "smart_retail", f"{name}: routed to {intro.id}"
        assert intro.language == intro_lang, (
            f"{name}: intro language {intro.language!r} != {intro_lang!r}")
        assert intro.audio_path.is_file(), intro.audio_path


def test_paraformer_fallback_still_chinese(tmp_path):
    """The legacy Chinese model must keep working (rollback path)."""
    legacy = Path(os.getenv("OVA_ASR_LEGACY_MODEL_DIR") or ROOT / "models" / "asr_paraformer_zh_int8")
    if not legacy.is_dir():
        print(f"SKIP: {legacy} not downloaded")
        return
    fixture = ROOT / "tests" / "asr_zh_smart_retail.wav"
    if not fixture.is_file():
        print("SKIP: missing Chinese fixture")
        return
    asr = LocalAsr(legacy)
    assert asr.model_type == "paraformer"
    text = asr.transcribe(_read_wav(fixture))
    print(f"  paraformer fallback: {text!r}")
    assert "智慧零售" in _normalise(text), text


# --- direct runner (no pytest required) ------------------------------------

def _main() -> int:
    import tempfile
    import traceback

    tests = [
        test_detect_sense_voice,
        test_content_length_ignores_punctuation,
        test_no_speech_returns_empty,
        test_detect_paraformer,
        test_detect_transducer,
        test_detect_missing_model_raises,
        test_transcribe_bilingual_and_route,
        test_paraformer_fallback_still_chinese,
    ]
    failed = 0
    for test in tests:
        needs_tmp = "tmp_path" in test.__code__.co_varnames[: test.__code__.co_argcount]
        try:
            if needs_tmp:
                with tempfile.TemporaryDirectory() as tmp:
                    test(Path(tmp))
            else:
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

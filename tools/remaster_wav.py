#!/usr/bin/env python3
"""remaster_wav.py — 把预生成 WAV 重制到实时 TTS 的响度档（target_rms 默认 0.09）。

复用 ova.audio.normalize_loudness（去 DC + 防爆音淡出 + 4:1 压缩 + 峰值限幅 0.97），
与 glm_voice 直播路径同一套管线，保证不破音（数字域）。只处理 16 kHz 16-bit WAV；
双声道文件要求 dual-mono（左右一致），处理后仍写回立体声。

usage: remaster_wav.py <file.wav> [...] [--target-rms 0.09] [--dry-run]
"""
import sys
import wave
from pathlib import Path

sys.path.insert(0, "/home/pollen/ova/src")

import numpy as np  # noqa: E402

from ova.audio import normalize_loudness  # noqa: E402


def load(path: Path):
    with wave.open(str(path), "rb") as w:
        ch, rate, sw = w.getnchannels(), w.getframerate(), w.getsampwidth()
        if sw != 2 or rate != 16000:
            raise ValueError(f"{path}: 需要 16kHz/16bit，实际 rate={rate} width={sw}")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if ch == 2:
        L, R = data[0::2].copy(), data[1::2].copy()
        if not np.array_equal(L, R):
            raise ValueError(f"{path}: 非 dual-mono 立体声，拒绝处理")
        mono = L
    else:
        mono = data
    return mono, ch, rate


def stats(x: np.ndarray) -> str:
    f = x.astype(np.float32) / 32768.0
    peak = float(np.abs(f).max()) if f.size else 0.0
    rms = float(np.sqrt(np.mean(f ** 2))) if f.size else 0.0
    db = lambda v: 20 * np.log10(max(v, 1e-9))
    return f"peak={db(peak):+.1f}dBFS rms={db(rms):+.1f}dBFS"


def main(argv) -> int:
    dry = "--dry-run" in argv
    target = 0.09
    args = []
    it = iter(argv)
    for a in it:
        if a == "--target-rms":
            target = float(next(it))
        elif a.startswith("--"):
            continue
        else:
            args.append(a)
    if not args:
        sys.stderr.write(__doc__)
        return 2
    for name in args:
        path = Path(name)
        mono, ch, rate = load(path)
        before = stats(mono)
        out, gain = normalize_loudness(mono, target, rate=rate)
        print(f"{path}: {before} -> {stats(out)} (gain={gain:.2f})"
              + (" [dry-run]" if dry else ""))
        if not dry:
            frames = np.repeat(out, 2) if ch == 2 else out
            with wave.open(str(path), "wb") as w:
                w.setnchannels(ch)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(frames.astype("<i2").tobytes())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

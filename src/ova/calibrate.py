#!/usr/bin/env python3
"""Calibration helper for the wake-word service.

Two modes (mirrors the manual calibration procedure used to tune the
Reachy Mini deployment):

  --record SECONDS
      Play a short beep through the output device, then record SECONDS of
      stereo audio. Speak the wake phrase N times (about SECONDS/4 times)
      after the beep; each utterance is then analyzed.

  --analyze FILE
      Analyze an existing 16 kHz WAV: score every frame with the wake-word
      model, list utterance events with their peak scores, and report how
      many would have been caught at common thresholds.

Usage examples:
  python tools/calibrate.py --record 30
  python tools/calibrate.py --analyze /tmp/calib.wav
  python tools/calibrate.py --analyze /tmp/calib.wav --thresholds 0.15,0.2,0.25,0.3
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import wave
from pathlib import Path



import numpy as np

from ova.wake import (  # noqa: E402
    AlsaBackend, Capture, FRAME, RATE, load_model, resolve_config,
)

BEEP_HZ = 1000
BEEP_SECONDS = 0.15
EVENT_FLOOR = 0.15        # ignore scores below this when grouping events
EVENT_MIN_GAP_S = 1.5     # separate events at least this far apart


def beep_wav(path: Path) -> None:
    n = int(RATE * BEEP_SECONDS)
    with wave.open(str(path), "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        frames = b"".join(
            struct.pack("<hh", *(int(12000 * math.sin(2 * math.pi * BEEP_HZ * i / RATE)),) * 2)
            for i in range(n)
        )
        w.writeframes(frames)


def record(cfg: dict, seconds: float) -> Path:
    backend = AlsaBackend(cfg)
    beep = Path("/tmp/hjw_beep.wav")
    beep_wav(beep)
    print(f"[1/3] playing beep, then recording {seconds:.0f}s "
          "-> speak the wake phrase repeatedly (one every ~2-3s)")
    backend.play_file(beep)
    blocks = []
    with Capture(backend) as capture:
        end_time = __import__("time").monotonic() + seconds
        while __import__("time").monotonic() < end_time:
            blocks.append(capture.read())
    audio = np.concatenate(blocks)
    out = Path("/tmp/hjw_calib.wav")
    with wave.open(str(out), "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.tobytes())
    print(f"[2/3] saved {out} ({len(audio) / RATE:.1f}s)")
    return out


def analyze(model, path: Path, thresholds) -> None:
    with wave.open(str(path)) as w:
        if w.getframerate() != RATE or w.getsampwidth() != 2:
            raise SystemExit("WAV must be 16 kHz signed 16-bit PCM")
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        if w.getnchannels() > 1:
            audio = audio.reshape(-1, w.getnchannels())
        else:
            audio = audio.reshape(-1, 1)

    frames = len(audio) // FRAME
    for ch in range(audio.shape[1]):
        x = audio[:, ch]
        scores = []
        for i in range(frames):
            f = x[i * FRAME:(i + 1) * FRAME]
            scores.append(max(float(v) for v in model.predict(f).values()))
        model.reset()
        s = np.array(scores)
        times = np.arange(frames) * FRAME / RATE
        events = []
        i = 0
        while i < len(s):
            if s[i] >= EVENT_FLOOR:
                j = i
                while (j + 1 < len(s) and s[j + 1] >= EVENT_FLOOR
                       and (j + 1 - i) * FRAME / RATE < 3.0):
                    j += 1
                events.append((times[i], times[j], s[i:j + 1].max()))
                i = j + int(EVENT_MIN_GAP_S * RATE / FRAME)
            else:
                i += 1
        print(f"--- channel {ch}: {len(events)} utterance event(s), "
              f"global peak {s.max():.4f} ---")
        for t0, t1, peak in events:
            print(f"    t={t0:6.2f}-{t1:6.2f}s  peak={peak:.4f}")
        row = "    thresholds -> "
        for th in thresholds:
            row += f"{th:.2f}:{sum(1 for _, _, p in events if p >= th)}  "
        print(row)

    # Combined advice from both channels
    print("\n建议: 先从 0.2 / 连续3帧(hits=3) 开始；若漏唤醒多再降阈值，"
          "若无人说话时误唤醒则升阈值或加大 hits(4~5)。")


def calibrate_main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", type=float, metavar="SECONDS",
                       help="beep then record N seconds of real voice")
    group.add_argument("--analyze", type=Path, metavar="WAV",
                       help="analyze an existing 16 kHz WAV")
    parser.add_argument("--thresholds", default="0.15,0.2,0.25,0.3",
                        help="comma separated thresholds to report")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)

    cfg = resolve_config(args)
    thresholds = [float(t) for t in args.thresholds.split(",") if t]

    if args.record:
        path = record(cfg, args.record)
        model = load_model(Path(cfg["model_dir"]))
        analyze(model, path, thresholds)
    else:
        model = load_model(Path(cfg["model_dir"]))
        analyze(model, args.analyze, thresholds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

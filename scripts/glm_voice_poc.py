#!/usr/bin/env python3
"""Phase 0 PoC: WAV file -> GLM-4-Voice -> WAV file (no robot, no device).

Answers the questions that decide the architecture — real latency breakdown,
real token usage/cost, the actual output sample rate and the reply quality —
before any of it is wired into the wake service.

    export ZHIPUAI_API_KEY=xxxx
    python3 scripts/glm_voice_poc.py tests/asr_en_smart_retail.wav
    python3 scripts/glm_voice_poc.py --dir tests/wavs --out-dir /tmp/glm_poc
    python3 scripts/glm_voice_poc.py in.wav --rate 24000   # 若听感变调，用它核对采样率

Every run appends a line to the JSONL log (default /tmp/glm_voice_poc.jsonl) so
several attempts can be compared afterwards.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ova.engines.base import EngineError                      # noqa: E402
from ova.engines.glm_voice import GlmVoiceEngine, respond_file  # noqa: E402


def wav_duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / float(w.getframerate() or 1)


def run_one(path: Path, out_dir: Path, persona: str | None, rate: int | None,
            log_path: Path) -> dict:
    cfg: dict = {}
    if rate:
        cfg["glm_voice_pcm_rate"] = rate
    print(f"\n=== {path.name} ({wav_duration(path):.2f}s) ===")
    t0 = time.monotonic()
    try:
        reply = respond_file(path, cfg, persona)
    except EngineError as exc:
        print(f"  FAILED: {exc}")
        record = {"wav": path.name, "ok": False, "error": str(exc)}
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    out = out_dir / f"{path.stem}_reply.wav"
    out.write_bytes(reply.audio_path.read_bytes())
    reply.audio_path.unlink(missing_ok=True)
    wall = time.monotonic() - t0

    meta = reply.meta
    print(f"  回复文本: {reply.text}")
    print(f"  延迟: 编码 {meta['encode_s']}s | 请求 {meta['request_s']}s | "
          f"转码 {meta['convert_s']}s | 合计 {meta['elapsed_s']}s (脚本墙钟 {wall:.2f}s)")
    print(f"  音频: {meta['audio_s']}s @ 44100Hz裸PCM → {out}")
    if meta.get("tokens"):
        print(f"  用量: {meta['tokens']} tokens ≈ ¥{meta['cost_cny']}")
    else:
        print("  用量: 响应里没有 usage 字段")
    print(f"  听感检查: aplay {out}   (若语速/音调不对，改用 --rate 22050/48000 重试)")

    record = {"wav": path.name, "ok": True, "text": reply.text, "lang": reply.lang,
              "out": str(out), **{k: v for k, v in meta.items() if k != "engine"}}
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav", nargs="?", help="16 kHz 单声道/双声道 WAV")
    ap.add_argument("--dir", help="批量模式：处理目录下所有 *.wav")
    ap.add_argument("--out-dir", default="/tmp/glm_voice_poc", help="回复音频输出目录")
    ap.add_argument("--persona", help="覆盖人设提示词")
    ap.add_argument("--rate", type=int, help="覆盖返回 PCM 采样率（默认 44100）")
    ap.add_argument("--log", default="/tmp/glm_voice_poc.jsonl", help="结果 JSONL 日志")
    args = ap.parse_args()

    if not args.wav and not args.dir:
        ap.error("需要给出 wav 文件或 --dir 目录")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log)

    engine = GlmVoiceEngine({"glm_voice_pcm_rate": args.rate} if args.rate else None)
    print(f"model={engine.model} url={engine.url} pcm_rate={engine.pcm_rate} "
          f"timeout={engine.timeout_s:.0f}s")

    targets = ([Path(p) for p in sorted(Path(args.dir).glob("*.wav"))]
               if args.dir else [Path(args.wav)])
    records = [run_one(p, out_dir, args.persona, args.rate, log_path) for p in targets]

    ok = [r for r in records if r.get("ok")]
    print(f"\n完成 {len(ok)}/{len(records)}；日志: {log_path}；音频: {out_dir}")
    if ok:
        reqs = [r["request_s"] for r in ok]
        print(f"请求耗时 min/avg/max = {min(reqs):.2f}/{sum(reqs)/len(reqs):.2f}/{max(reqs):.2f}s")
    return 0 if len(ok) == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())

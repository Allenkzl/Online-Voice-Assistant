"""Shared constants, defaults and the lightweight service-event bridge."""

import json
import os
import time

RATE = 16000          # frames/s used internally
FRAME = 1280          # 80 ms per inference frame (samples)
BLOCK = FRAME * 4     # 80 ms stereo S16_LE bytes

# Project root used for relative asset/model paths (override with OVA_HOME).
ROOT = Path = __import__("pathlib").Path(os.getenv(
    "OVA_HOME", __import__("pathlib").Path(__file__).resolve().parents[2]))

EVENT_FILE = os.getenv("HJV_EVENT_FILE", "")


def svc_event(card: str, msg: str, level: str = "info", **extra) -> None:
    """Append a JSONL line consumed by the debug console (web timeline)."""
    if not EVENT_FILE:
        return
    try:
        line = json.dumps({"t": time.strftime("%H:%M:%S"), "card": card,
                           "level": level, "msg": msg, **extra},
                          ensure_ascii=False)
        with open(EVENT_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

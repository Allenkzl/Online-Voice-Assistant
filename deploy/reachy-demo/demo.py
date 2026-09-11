#!/usr/bin/env python3
"""Reachy Mini showroom ambient motion using official recorded moves.

The motion itself is executed by the Reachy Mini daemon HTTP API. This script
only chooses official low-amplitude recorded moves, keeps a conservative
cadence, and writes enough log heartbeat for the watchdog to supervise it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request


logging.basicConfig(
    level=os.environ.get("DEMO_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("reachy_demo")

DAEMON = os.environ.get("DEMO_DAEMON_URL", "http://localhost:8000").rstrip("/")
IDLE_INTERVAL_S = float(os.environ.get("DEMO_IDLE_INTERVAL_S", "20"))
SPEAKING_INTERVAL_S = float(os.environ.get("DEMO_SPEAKING_INTERVAL_S", "10"))
SPEAKING_STATE_FILE = os.environ.get(
    "DEMO_SPEAKING_STATE_FILE", "/tmp/ova_speaking.state"
)
SPEAKING_STATE_TTL_S = float(os.environ.get("DEMO_SPEAKING_STATE_TTL_S", "300"))
MOVE_TIMEOUT_S = float(os.environ.get("DEMO_MOVE_TIMEOUT_S", "50"))
STUCK_MOVE_WAIT_S = float(os.environ.get("DEMO_STUCK_MOVE_WAIT_S", "65"))
FAILURE_LIMIT = int(os.environ.get("DEMO_FAILURE_LIMIT", "3"))

EMOTIONS = "pollen-robotics/reachy-mini-emotions-library"

# Pick the smallest-feeling official moves from the emotions library. These are
# still more expressive than tiny goto poses, but avoid the largest dance/body
# motion sequences for showroom long-running use.
MOVES = [
    "inquiring2",
    "enthusiastic1",
    "dance1",
    "understanding2",
    "thoughtful2",
    "laughing2",
    "attentive2",
    "displeased1",
    "thoughtful1",
    "come1",
]


def _req(
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float = 15.0,
) -> dict | list:
    url = f"{DAEMON}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"daemon {method} {path}: HTTP {exc.code}") from exc


def _backend_ready() -> bool:
    try:
        _req("GET", "/api/state/full", timeout=3.0)
        return True
    except Exception:
        return False


def _ensure_backend() -> bool:
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{DAEMON}/", timeout=3.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            pass
        time.sleep(2)

    try:
        _req("POST", "/api/daemon/start?wake_up=true", timeout=8.0)
    except Exception as exc:
        log.debug("daemon start request: %s", exc)

    for _ in range(90):
        if _backend_ready():
            return True
        time.sleep(2)
    return False


def _running_moves() -> list:
    try:
        running = _req("GET", "/api/move/running", timeout=5.0)
    except Exception:
        return []
    return running if isinstance(running, list) else []


def _move_running() -> bool:
    return bool(_running_moves())


def _stop_moves(running: list) -> int:
    stopped = 0
    for item in running:
        uuid = (item or {}).get("uuid") if isinstance(item, dict) else str(item)
        if not uuid:
            continue
        try:
            _req("POST", "/api/move/stop", {"uuid": uuid}, timeout=5.0)
            stopped += 1
        except Exception:
            log.warning("failed to stop move %s", uuid, exc_info=True)
    return stopped


def _stop_stuck_moves(max_wait: float = STUCK_MOVE_WAIT_S) -> bool:
    deadline = time.monotonic() + max_wait
    running: list = []
    while time.monotonic() < deadline:
        running = _running_moves()
        if not running:
            return False
        time.sleep(1.0)
    stopped = _stop_moves(running)
    log.warning("force-stopped %d stuck move(s)", stopped)
    return stopped > 0


def _goto(
    *,
    yaw: float = 0.0,
    pitch: float = 0.0,
    roll: float = 0.0,
    antennas: tuple[float, float] | None = None,
    duration: float = 1.0,
) -> None:
    body: dict = {
        "head_pose": {
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "roll": roll,
            "pitch": pitch,
            "yaw": yaw,
        },
        "duration": duration,
        "interpolation": "minjerk",
    }
    if antennas is not None:
        body["antennas"] = list(antennas)
    _req("POST", "/api/move/goto", body, timeout=8.0)


def _head_off_center(deg: float = 8.0) -> bool:
    try:
        state = _req("GET", "/api/state/full", timeout=5.0)
    except Exception:
        return False
    if not isinstance(state, dict):
        return False
    head = state.get("head_pose") or {}
    for axis in ("pitch", "yaw", "roll"):
        if abs(float(head.get(axis, 0.0))) > math.radians(deg):
            return True
    return False


def _neutral(attempts: int = 3) -> bool:
    for attempt in range(attempts):
        try:
            _goto(yaw=0.0, pitch=0.0, roll=0.0, antennas=(0.0, 0.0), duration=1.2)
            time.sleep(1.5)
            if not _head_off_center(6.0):
                return True
        except Exception:
            log.warning("neutral goto attempt %d failed", attempt + 1, exc_info=True)
        time.sleep(0.8)
    return False


def _wait_move_done(timeout: float = MOVE_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _move_running():
            return True
        time.sleep(0.5)
    log.warning("move still running after %.0fs", timeout)
    return False


def _play_move(move: str) -> None:
    _req(
        "POST",
        f"/api/move/play/recorded-move-dataset/{EMOTIONS}/{move}",
        timeout=15.0,
    )
    log.info("playing official move=%s speaking=%s", move, _is_speaking())


def _is_speaking() -> bool:
    try:
        raw = open(SPEAKING_STATE_FILE, encoding="utf-8").read()
        state = json.loads(raw)
    except Exception:
        return False
    if not state.get("speaking"):
        return False
    updated_at = float(state.get("updated_at", 0.0))
    return time.time() - updated_at <= SPEAKING_STATE_TTL_S


def _recover_daemon_backend() -> None:
    log.warning("daemon backend unhealthy; requesting backend restart")
    try:
        _req("POST", "/api/daemon/restart", timeout=8.0)
    except Exception as exc:
        log.warning("daemon backend restart request failed: %s", exc)
    if not _ensure_backend():
        raise RuntimeError("daemon backend did not recover")
    log.info("daemon backend recovered")


def _run_one_move(move: str) -> None:
    if _move_running():
        log.info("a move is still running; skipping this slot")
        return
    _play_move(move)
    finished = _wait_move_done()
    if not finished:
        stopped = _stop_moves(_running_moves())
        log.warning("stopped %d move(s) after timeout", stopped)
    _neutral()


def main() -> None:
    log.info("waiting for daemon backend at %s ...", DAEMON)
    if not _ensure_backend():
        raise SystemExit("daemon backend not ready after ~5min")
    log.info("daemon backend ready")

    try:
        _req("POST", "/api/move/play/wake_up", timeout=15.0)
        log.info("wake_up requested")
        time.sleep(4.0)
        _neutral()
        log.info("neutral pose set")
    except Exception:
        log.warning("wake/neutral failed", exc_info=True)

    failures = 0
    log.info(
        "official motion loop: %d moves, idle=%.0fs speaking=%.0fs",
        len(MOVES),
        IDLE_INTERVAL_S,
        SPEAKING_INTERVAL_S,
    )
    while True:
        deck = list(MOVES)
        random.shuffle(deck)
        for move in deck:
            interval = SPEAKING_INTERVAL_S if _is_speaking() else IDLE_INTERVAL_S
            started_at = time.monotonic()
            try:
                _run_one_move(move)
                failures = 0
            except (TimeoutError, urllib.error.URLError, RuntimeError) as exc:
                failures += 1
                log.warning(
                    "official move %s failed (%d/%d): %s",
                    move,
                    failures,
                    FAILURE_LIMIT,
                    exc,
                    exc_info=True,
                )
                if failures >= FAILURE_LIMIT:
                    _recover_daemon_backend()
                    failures = 0
            elapsed = time.monotonic() - started_at
            if elapsed < interval:
                time.sleep(interval - elapsed)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

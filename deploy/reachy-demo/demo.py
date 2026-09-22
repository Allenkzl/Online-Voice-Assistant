#!/usr/bin/env python3
"""Reachy Mini showroom motion, phase-driven (motion v2).

OVA publishes a dialogue phase (idle/listening/thinking/speaking) to a state
file; this loop maps phases to behaviours on the daemon HTTP API:
listening watches the visitor via the daemon's native face tracking,
thinking glances aside, speaking keeps a restrained recorded-move accent
loop, idle micro-wanders its gaze and turns to look at whoever shows up.
Set DEMO_TRACKING=0 to disable face tracking and fall back to moves only.
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

# Motion v2: perception-first behaviours. listening/idle-attention use the
# daemon's native face tracking (measured +1.3% daemon CPU on CM4); set
# DEMO_TRACKING=0 to fall back to recorded moves only (v1 behaviour).
TRACKING_ENABLED = os.environ.get("DEMO_TRACKING", "1") != "0"
PHASE_TTL_S = float(os.environ.get("DEMO_PHASE_TTL_S", "300"))
GAZE_INTERVAL_S = float(os.environ.get("DEMO_GAZE_INTERVAL_S", "8"))
TRACKING_FACE_LOST_S = float(os.environ.get("DEMO_TRACKING_FACE_LOST_S", "10"))
# Idle face-scan cadence: while nobody is around the tracker stays off and the
# head micro-wanders; every SCAN interval it briefly re-enables tracking just
# to look for a face ("peek"), so approaching visitors are noticed.
SCAN_INTERVAL_S = float(os.environ.get("DEMO_SCAN_INTERVAL_S", "15"))

EMOTIONS = "pollen-robotics/reachy-mini-emotions-library"

# Pick the smallest-feeling official moves from the emotions library. These are
# still more expressive than tiny goto poses, but avoid the largest dance/body
# motion sequences for showroom long-running use.
MOVES = [
    "inquiring2",
    "thoughtful2",
    "laughing2",
    "attentive2",
    "displeased1",
    "thoughtful1",
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
    body_yaw: float | None = None,
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
    if body_yaw is not None:
        body["body_yaw"] = body_yaw
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
            _goto(yaw=0.0, pitch=0.0, roll=0.0, antennas=(0.0, 0.0), body_yaw=0.0, duration=1.2)
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


def _play_move(move: str, phase: str | None = None) -> None:
    _req(
        "POST",
        f"/api/move/play/recorded-move-dataset/{EMOTIONS}/{move}",
        timeout=15.0,
    )
    log.info("playing official move=%s phase=%s", move, phase or _read_phase())


def _read_phase() -> str:
    """Current motion phase from OVA's state file; stale or broken reads -> idle."""
    try:
        state = json.loads(open(SPEAKING_STATE_FILE, encoding="utf-8").read())
    except Exception:
        return "idle"
    updated_at = float(state.get("updated_at", 0.0))
    if time.time() - updated_at > PHASE_TTL_S:
        return "idle"
    phase = state.get("phase")
    if phase in ("listening", "thinking", "speaking", "idle"):
        return phase
    # legacy bool format written by older OVA builds
    return "speaking" if state.get("speaking") else "idle"


def _is_speaking() -> bool:
    return _read_phase() == "speaking"


def _tracking(on: bool) -> bool:
    """Enable/disable the daemon's native face tracking; False when unavailable."""
    if not TRACKING_ENABLED:
        return False
    try:
        if on:
            resp = _req(
                "POST", "/api/media/tracking/enable", {"weight": 1.0}, timeout=8.0
            )
            return bool(resp.get("enabled"))
        _req("POST", "/api/media/tracking/disable", timeout=8.0)
        return True
    except Exception as exc:
        log.warning("tracking %s failed: %s", "enable" if on else "disable", exc)
        return False


def _face_detected() -> bool:
    try:
        resp = _req("GET", "/api/media/tracking/face", timeout=5.0)
        return bool((resp.get("face_target") or {}).get("detected"))
    except Exception:
        return False


def _gaze_wander() -> None:
    """Idle micro-gaze: small absolute targets, hard-bounded well inside limits."""
    _goto(
        yaw=random.uniform(-0.05, 0.05),
        pitch=random.uniform(-0.02, 0.03),
        roll=random.uniform(-0.03, 0.03),
        antennas=(
            random.uniform(-0.06, 0.06),
            random.uniform(-0.06, 0.06),
        ),
        duration=1.4,
    )


def _ensure_motors() -> None:
    """1.11 launcher starts the daemon with --no-wake-up-on-start, so motors stay
    limp until someone asks. For an unattended showroom the demo IS that someone:
    enable torque at startup and after every daemon recovery."""
    try:
        status = _req("GET", "/api/motors/status", timeout=5.0)
        if status.get("mode") in ("enabled", "MotorControlMode.Enabled"):
            return
        _req("POST", "/api/motors/set_mode/enabled", timeout=8.0)
        log.info("motors enabled")
    except Exception as exc:
        log.warning("motor enable failed: %s", exc)


def _recover_daemon_backend() -> None:
    log.warning("daemon backend unhealthy; requesting backend restart")
    try:
        _req("POST", "/api/daemon/restart", timeout=8.0)
    except Exception as exc:
        log.warning("daemon backend restart request failed: %s", exc)
    if not _ensure_backend():
        raise RuntimeError("daemon backend did not recover")
    _ensure_motors()
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
    _ensure_motors()

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
        "motion v2 loop: tracking=%s moves=%d idle=%.0fs speaking=%.0fs",
        TRACKING_ENABLED,
        len(MOVES),
        IDLE_INTERVAL_S,
        SPEAKING_INTERVAL_S,
    )
    deck = list(MOVES)
    random.shuffle(deck)
    deck_i = 0
    last_phase = "idle"
    tracking_on = False
    face_lost_at: float | None = None
    last_gaze = 0.0
    last_scan = 0.0
    last_heartbeat = 0.0
    while True:
        phase = _read_phase()
        started_at = time.monotonic()
        try:
            if time.monotonic() - last_heartbeat > 60.0:
                # Watchdog supervision needs a log heartbeat; idle phases can be
                # silent for minutes otherwise and get needlessly restarted.
                log.info(
                    "heartbeat phase=%s tracking=%s", phase, tracking_on
                )
                last_heartbeat = time.monotonic()
            if phase != last_phase:
                log.info("phase %s -> %s", last_phase, phase)
                if phase == "listening":
                    if not _tracking(True):
                        tracking_on = False
                        _run_one_move("attentive2")
                    else:
                        tracking_on = True
                    face_lost_at = None
                elif phase == "thinking":
                    if tracking_on:
                        _tracking(False)
                        tracking_on = False
                    _goto(yaw=0.06, pitch=0.02, duration=0.8)
                elif phase == "speaking":
                    if tracking_on:
                        _tracking(False)
                        tracking_on = False
                    _neutral()
                last_phase = phase

            if phase == "speaking":
                move = deck[deck_i % len(deck)]
                deck_i += 1
                if deck_i % len(deck) == 0:
                    random.shuffle(deck)
                _run_one_move(move)
            elif phase == "idle":
                if tracking_on:
                    if _face_detected():
                        face_lost_at = None
                    else:
                        now = time.monotonic()
                        if face_lost_at is None:
                            face_lost_at = now
                        elif now - face_lost_at > TRACKING_FACE_LOST_S:
                            _tracking(False)
                            tracking_on = False
                            face_lost_at = None
                else:
                    now = time.monotonic()
                    if now - last_scan > SCAN_INTERVAL_S:
                        last_scan = now
                        if _tracking(True):
                            tracking_on = True
                            face_lost_at = now
                    elif now - last_gaze > GAZE_INTERVAL_S:
                        _gaze_wander()
                        last_gaze = time.monotonic()
            # listening/thinking: one-shot behaviour already applied above
            failures = 0
        except (TimeoutError, urllib.error.URLError, RuntimeError) as exc:
            failures += 1
            log.warning(
                "motion cycle failed (%d/%d): %s",
                failures,
                FAILURE_LIMIT,
                exc,
                exc_info=True,
            )
            if failures >= FAILURE_LIMIT:
                if tracking_on:
                    tracking_on = False
                _recover_daemon_backend()
                failures = 0
        interval = SPEAKING_INTERVAL_S if phase == "speaking" else 1.0
        elapsed = time.monotonic() - started_at
        if elapsed < interval:
            time.sleep(interval - elapsed)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

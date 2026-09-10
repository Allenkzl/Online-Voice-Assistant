#!/usr/bin/env python3
"""Watchdog for Reachy Mini official showroom motion.

Runs outside the demo loop so it can recover cases where the demo process is
alive but the daemon motion API, recorded move queue, or demo heartbeat wedges.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request


logging.basicConfig(
    level=os.environ.get("WATCHDOG_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("reachy_demo_watchdog")

DAEMON = os.environ.get("WATCHDOG_DAEMON_URL", "http://localhost:8000").rstrip("/")
CHECK_INTERVAL_S = float(os.environ.get("WATCHDOG_CHECK_INTERVAL_S", "15"))
API_FAILURE_LIMIT = int(os.environ.get("WATCHDOG_API_FAILURE_LIMIT", "3"))
STUCK_MOVE_S = float(os.environ.get("WATCHDOG_STUCK_MOVE_S", "90"))
LOG_PATH = os.environ.get("WATCHDOG_DEMO_LOG", "/opt/reachy-demo/demo.log")
LOG_STALE_S = float(os.environ.get("WATCHDOG_LOG_STALE_S", "180"))
SERVICE_RESTART_COOLDOWN_S = float(
    os.environ.get("WATCHDOG_SERVICE_RESTART_COOLDOWN_S", "180")
)
DAEMON_RESTART_COOLDOWN_S = float(
    os.environ.get("WATCHDOG_DAEMON_RESTART_COOLDOWN_S", "600")
)


def _req(
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float = 5.0,
) -> dict | list:
    url = f"{DAEMON}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _run_systemctl(*args: str) -> bool:
    cmd = ["systemctl", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return True
    log.warning(
        "%s failed rc=%s stdout=%r stderr=%r",
        " ".join(cmd),
        result.returncode,
        result.stdout.strip(),
        result.stderr.strip(),
    )
    return False


def _service_active(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", name],
        check=False,
    )
    return result.returncode == 0


def _api_ready() -> bool:
    try:
        _req("GET", "/api/state/full", timeout=5.0)
        return True
    except (TimeoutError, urllib.error.URLError, RuntimeError, OSError):
        return False


def _running_moves() -> list:
    running = _req("GET", "/api/move/running", timeout=5.0)
    return running if isinstance(running, list) else []


def _move_key(item: object) -> str:
    if isinstance(item, dict):
        uuid = item.get("uuid")
        if uuid:
            return str(uuid)
        return json.dumps(item, sort_keys=True, ensure_ascii=True)
    return str(item)


def _stop_running_moves(running: list) -> int:
    stopped = 0
    for item in running:
        uuid = item.get("uuid") if isinstance(item, dict) else str(item)
        if not uuid:
            continue
        try:
            _req("POST", "/api/move/stop", {"uuid": uuid}, timeout=5.0)
            stopped += 1
        except Exception:
            log.warning("failed to stop stuck move %s", uuid, exc_info=True)
    return stopped


def _log_stale() -> bool:
    try:
        mtime = os.path.getmtime(LOG_PATH)
    except OSError:
        return True
    return time.time() - mtime > LOG_STALE_S


def _restart_demo(now: float, last_restart: float) -> float:
    if now - last_restart < SERVICE_RESTART_COOLDOWN_S:
        log.warning("demo restart suppressed by cooldown")
        return last_restart
    log.warning("restarting reachy-demo")
    _run_systemctl("restart", "reachy-demo")
    return now


def _restart_daemon_and_demo(now: float, last_daemon_restart: float) -> float:
    if now - last_daemon_restart < DAEMON_RESTART_COOLDOWN_S:
        log.warning("daemon restart suppressed by cooldown")
        return last_daemon_restart
    log.warning("restarting reachy-mini-daemon and reachy-demo")
    _run_systemctl("restart", "reachy-mini-daemon")
    time.sleep(8.0)
    _run_systemctl("restart", "reachy-demo")
    return now


def main() -> None:
    log.info(
        "watchdog started: interval=%.0fs stuck_move=%.0fs log_stale=%.0fs",
        CHECK_INTERVAL_S,
        STUCK_MOVE_S,
        LOG_STALE_S,
    )
    api_failures = 0
    running_since: dict[str, float] = {}
    last_demo_restart = 0.0
    last_daemon_restart = 0.0

    while True:
        now = time.time()

        if not _service_active("reachy-mini-daemon"):
            log.warning("reachy-mini-daemon is not active")
            last_daemon_restart = _restart_daemon_and_demo(now, last_daemon_restart)
            time.sleep(CHECK_INTERVAL_S)
            continue

        if not _service_active("reachy-demo"):
            log.warning("reachy-demo is not active")
            last_demo_restart = _restart_demo(now, last_demo_restart)

        if not _api_ready():
            api_failures += 1
            log.warning("daemon API check failed (%d/%d)", api_failures, API_FAILURE_LIMIT)
            if api_failures >= API_FAILURE_LIMIT:
                last_daemon_restart = _restart_daemon_and_demo(now, last_daemon_restart)
                api_failures = 0
                running_since.clear()
            time.sleep(CHECK_INTERVAL_S)
            continue
        api_failures = 0

        try:
            running = _running_moves()
        except Exception:
            log.warning("failed to inspect running moves", exc_info=True)
            running = []

        current_keys = {_move_key(item) for item in running}
        for key in list(running_since):
            if key not in current_keys:
                running_since.pop(key, None)
        for key in current_keys:
            running_since.setdefault(key, now)

        stuck_keys = [
            key for key, started in running_since.items()
            if now - started > STUCK_MOVE_S
        ]
        if stuck_keys:
            stopped = _stop_running_moves(running)
            log.warning("stuck move detected; stopped=%d keys=%s", stopped, stuck_keys)
            running_since.clear()
            last_demo_restart = _restart_demo(now, last_demo_restart)

        if _log_stale():
            log.warning("demo log is stale: %s", LOG_PATH)
            last_demo_restart = _restart_demo(now, last_demo_restart)

        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()

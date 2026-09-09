"""ALSA audio backend (capture/playback) and mono downmix.

This is the only device-touching layer; a future PortAudio backend should
implement the same interface so macOS/Windows can be supported.
"""

import os
import selectors
import subprocess
import time
from pathlib import Path

import numpy as np

from ova.config import BLOCK, RATE

class AlsaBackend:
    """ALSA capture/playback via external arecord/aplay processes.

    This is the only device-touching layer; a future PortAudio backend
    would implement the same interface.
    """

    INPUT_FALLBACKS = ["reachymini_audio_src", "default", "plughw:0,0"]
    OUTPUT_FALLBACKS = ["reachymini_audio_sink", "default", "plughw:0,0"]

    def __init__(self, cfg: dict, rate: int = RATE):
        self.rate = rate
        self.cfg = cfg
        self.input_device = self._pick("input", cfg["input_device"])
        self.output_device = self._pick("output", cfg["output_device"])
        LOG.info("AUDIO input=%s output=%s", self.input_device, self.output_device)

    def _pick(self, kind: str, preferred: str) -> str:
        if preferred != "auto":
            return preferred
        listing = subprocess.run(
            ["arecord" if kind == "input" else "aplay", "-L"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        names = {line.strip() for line in listing.splitlines()}
        for cand in (self.INPUT_FALLBACKS if kind == "input"
                     else self.OUTPUT_FALLBACKS):
            if cand in names:
                return cand
        return "default"

    def open_capture(self) -> subprocess.Popen:
        """Start raw stereo S16_LE capture at self.rate."""
        return subprocess.Popen(
            ["arecord", "-q", "-D", self.input_device, "-t", "raw",
             "-f", "S16_LE", "-r", str(self.rate), "-c", "2",
             "--buffer-size", "4096", "--period-size", "1024"],
            stdout=subprocess.PIPE,
        )

    def play_file(self, path: Path, timeout: float = 15.0) -> None:
        subprocess.run(
            ["aplay", "-q", "-D", self.output_device, str(path)],
            check=True, timeout=timeout,
        )


class Capture:
    """Read exact 80 ms stereo frames with a timeout on a stalled pipe."""

    def __init__(self, backend: AlsaBackend):
        self.proc = backend.open_capture()
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        os.set_blocking(self.proc.stdout.fileno(), False)

    def read(self) -> np.ndarray:
        data = bytearray()
        deadline = time.monotonic() + 5
        while len(data) < BLOCK:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("No complete microphone frame within 5 seconds")
            chunk = os.read(self.proc.stdout.fileno(), BLOCK - len(data))
            if not chunk:
                raise RuntimeError(
                    f"Microphone pipe closed (exit={self.proc.poll()})"
                )
            data.extend(chunk)
        return np.frombuffer(data, dtype="<i2").reshape(-1, 2)

    def close(self) -> None:
        self.selector.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()

    def __enter__(self) -> "Capture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def mono(stereo: np.ndarray, channel):
    """Downmix to one channel: 0/1 picks a channel, 'mean' averages both."""
    if channel == "mean":
        return stereo.astype(np.float32).mean(axis=1).astype(np.int16)
    return np.ascontiguousarray(stereo[:, int(channel)])



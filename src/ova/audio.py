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


# --- WAV / resampling helpers (shared by TTS and end-to-end engines) --------

def wav_bytes(samples: np.ndarray, rate: int = RATE, channels: int = 1) -> bytes:
    """Wrap raw int16 samples in a WAV container."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.ascontiguousarray(samples, dtype="<i2").tobytes())
    return buf.getvalue()


def linear_resample(samples: np.ndarray, src_rate: int,
                    dst_rate: int = RATE) -> np.ndarray:
    """Linear-interpolation resample of int16 samples (fast, adequate)."""
    if src_rate == dst_rate:
        return samples
    n_out = int(round(len(samples) * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.int16)
    idx = np.linspace(0, len(samples) - 1, n_out)
    lo = idx.astype(np.int64)
    hi = np.minimum(lo + 1, len(samples) - 1)
    frac = (idx - lo).astype(np.float32)
    return (samples[lo].astype(np.float32) * (1 - frac)
            + samples[hi].astype(np.float32) * frac).astype(np.int16)


def resample(samples: np.ndarray, src_rate: int,
             dst_rate: int = RATE) -> np.ndarray:
    """Resample int16 mono; polyphase when scipy is available.

    44.1 kHz -> 16 kHz is a non-integer ratio (160/441), where linear
    interpolation is audibly worse, so the good filter is preferred.
    """
    if src_rate == dst_rate:
        return samples
    try:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(int(src_rate), int(dst_rate))
        out = resample_poly(samples.astype(np.float32),
                            int(dst_rate) // g, int(src_rate) // g)
        return np.clip(np.round(out), -32768, 32767).astype(np.int16)
    except Exception:  # noqa: BLE001 - scipy missing/too old: stay functional
        return linear_resample(samples, src_rate, dst_rate)


def device_wav_bytes(mono_samples: np.ndarray, src_rate: int) -> bytes:
    """Mono samples at any rate -> 16 kHz stereo WAV, ready for aplay."""
    mono16 = resample(mono_samples, src_rate)
    return wav_bytes(np.repeat(mono16, 2), rate=RATE, channels=2)


def mono16k_wav_bytes(audio: np.ndarray) -> bytes:
    """Recorded audio (stereo or mono) -> 16 kHz mono WAV for cloud models."""
    x = np.asarray(audio)
    if x.ndim == 2:                       # stereo -> first channel
        x = x[:, 0]
    return wav_bytes(x.astype(np.int16), rate=RATE, channels=1)


def normalize_loudness(samples_i16: np.ndarray, target_rms: float = 0.09,
                       peak_ceiling: float = 0.97,
                       max_gain: float = 4.0) -> tuple[np.ndarray, float]:
    """Scale int16 samples to a consistent speech level; returns (samples, gain).

    Measured 2026-09-10: a qwen3-tts reply sits at ~0.086 RMS while a
    GLM-4-Voice reply sits at ~0.046 (≈5.5 dB quieter, and brighter), which is
    what made the end-to-end voice sound thin and "robotic" next to the TTS.
    Gain is capped so the peak stays below ``peak_ceiling`` (no clipping).
    """
    x = np.asarray(samples_i16)
    if x.size == 0:
        return x, 1.0
    rms = float(np.sqrt(np.mean((x.astype(np.float32) / 32768.0) ** 2)))
    if rms <= 1e-6:
        return x, 1.0
    gain = min(target_rms / rms, max_gain)
    peak = float(np.max(np.abs(x)) / 32768.0)
    if peak > 0 and peak * gain > peak_ceiling:
        gain = peak_ceiling / peak
    if abs(gain - 1.0) < 0.01:
        return x, 1.0
    return np.clip(np.round(x.astype(np.float32) * gain), -32768, 32767).astype(np.int16), gain



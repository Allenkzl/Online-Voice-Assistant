#!/usr/bin/env python3
"""Engine interface shared by every dialogue brain."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# Engine aliases accepted in the ``engine`` config key.
ENGINE_ALIASES = {
    "pipeline": "pipeline",
    "local": "pipeline",
    "semi": "pipeline",
    "半在线": "pipeline",
    "e2e": "e2e",
    "end2end": "e2e",
    "glm4voice": "e2e",
    "glm-4-voice": "e2e",
    "端到端": "e2e",
}
DEFAULT_ACK = "ack_think.wav"     # optional buffering sound before a slow reply


class EngineError(RuntimeError):
    """Raised when an engine cannot produce a reply (cloud/format/timeout)."""


@dataclass
class Reply:
    """One playable answer produced by an engine."""

    audio_path: Path                     # 16 kHz stereo 16-bit WAV, ready for aplay
    text: str = ""                       # assistant reply text (for logs/console)
    transcript: str = ""                 # what the user said ("" when unknown)
    lang: str = ""                       # detected/expected reply language
    timeout_s: float = 90.0              # playback watchdog for this file
    temporary: bool = True               # delete the file after playback
    meta: dict[str, Any] = field(default_factory=dict)   # latency/usage/cost detail


@runtime_checkable
class Engine(Protocol):
    """A dialogue brain.

    ``needs_transcript`` tells the orchestrator whether to run the local ASR
    first (pipeline: yes; end-to-end: no — that is the whole point).
    """

    name: str
    needs_transcript: bool

    def respond(self, samples, text: str, cfg: dict) -> Reply:
        """samples: int16 mono 16 kHz array; text: ASR text ("" when unused)."""
        ...


def build_engine(cfg: dict, asr=None, engine: str | None = None) -> Engine:
    """Create the engine selected by ``cfg['engine']`` (default ``pipeline``)."""
    raw = str(engine if engine is not None else cfg.get("engine", "pipeline"))
    name = ENGINE_ALIASES.get(raw.strip().lower())
    if name is None:
        raise EngineError(
            f"unknown engine {raw!r}: use 'pipeline' (本地ASR+千问LLM+千问TTS) "
            f"or 'e2e' (端到端语音模型)"
        )
    if name == "pipeline":
        from ova.engines.pipeline import PipelineEngine
        return PipelineEngine(asr=asr, cfg=cfg)
    from ova.engines.glm_voice import GlmVoiceEngine
    return GlmVoiceEngine(cfg=cfg)

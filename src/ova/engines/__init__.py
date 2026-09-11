#!/usr/bin/env python3
"""Dialogue engines: the pluggable "brain" between recording and playback.

Every engine takes the recorded utterance and returns a :class:`Reply` that
points at a playable 16 kHz stereo WAV file. Everything around it — wake word,
VAD recording, echo protection, playback, barge-in, event log, debug console —
is shared, so switching engines never touches those layers.

Engines:

  * ``pipeline`` — semi-online: local ASR text -> Qwen chat (with tools) ->
    Qwen TTS. Needs the transcript, so the local ASR model must be present.
  * ``e2e``      — end-to-end: the recorded audio goes straight to a speech
    model (GLM-4-Voice) which answers with audio; no local ASR in the path.

Pick one with the ``engine`` config key (``WAKE_ENGINE`` env / ``--engine``).
"""

from __future__ import annotations

from ova.engines.base import Engine, EngineError, Reply, build_engine

__all__ = ["Engine", "EngineError", "Reply", "build_engine"]

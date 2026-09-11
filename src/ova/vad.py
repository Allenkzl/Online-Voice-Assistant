"""Voice Activity Detection entrypoint.

The canonical implementation lives in ``ova.wake`` so the wake service,
dialogue loop and console all share the same echo-safe Silero/energy VAD.
"""
from ova.wake import capture_utterance  # noqa: F401  (canonical implementation)

__all__ = ["capture_utterance"]

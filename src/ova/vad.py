"""Voice Activity Detection: capture one utterance with echo-safe VAD."""
from ova.wake import capture_utterance  # noqa: F401  (canonical implementation)

__all__ = ["capture_utterance"]

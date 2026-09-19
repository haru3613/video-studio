"""Provider-neutral TTS interface.

Every provider returns a SynthesisResult whose alignment units are in the
provider's native granularity ("char" for ElevenLabs, "word" for Azure batch).
Downstream SRT building consumes units, never provider response shapes.
"""
from abc import ABC, abstractmethod
from typing import NamedTuple


class AlignmentUnit(NamedTuple):
    text: str
    start: float  # seconds
    end: float    # seconds


class SynthesisResult(NamedTuple):
    audio: bytes
    units: list          # list[AlignmentUnit]
    granularity: str     # "char" | "word" | "none" (no alignment; bench/audio-only)
    metadata: dict | None = None  # provider request id / billed units when available


class ProviderError(RuntimeError):
    """Raised for any provider-side failure (HTTP error, bad response shape)."""


class TTSProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def synthesize(self, text: str, *, voice: str, model: str,
                   stability: float, speed: float) -> SynthesisResult:
        ...

    @abstractmethod
    def credits_for(self, text: str, model: str) -> int:
        """Provider-unit cost used by the budget ledger."""
        ...

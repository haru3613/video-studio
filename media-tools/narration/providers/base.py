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


class ProviderConfirmedFailure(ProviderError):
    """A failure proven to have happened before any billable acceptance."""

    def __init__(self, message: str, *, proof: str):
        super().__init__(message)
        self.proof = proof


class ProviderSubmissionUnknown(ProviderError):
    """The request crossed the submit boundary but acceptance is unknown."""

    def __init__(
        self,
        message: str,
        *,
        provider_request_id: str | None = None,
    ):
        super().__init__(message)
        self.provider_request_id = provider_request_id


class TTSProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def synthesize(self, text: str, *, voice: str, model: str,
                   stability: float, speed: float, **request_context) -> SynthesisResult:
        ...

    @abstractmethod
    def credits_for(self, text: str, model: str) -> int:
        """Provider-unit cost used by the budget ledger."""
        ...

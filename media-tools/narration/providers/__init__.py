from .base import (
    AlignmentUnit,
    ProviderConfirmedFailure,
    ProviderError,
    ProviderSubmissionUnknown,
    SynthesisResult,
    TTSProvider,
)
from .elevenlabs import ElevenLabsProvider

_PROVIDERS = {"elevenlabs": ElevenLabsProvider}


def get_provider(name: str) -> TTSProvider:
    try:
        return _PROVIDERS[name]()
    except KeyError:
        raise ProviderError(f"unknown TTS provider: {name} (known: {sorted(_PROVIDERS)})")

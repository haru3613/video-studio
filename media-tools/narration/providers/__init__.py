from .base import (
    AlignmentUnit,
    ProviderConfirmedFailure,
    ProviderError,
    ProviderSubmissionUnknown,
    SynthesisResult,
    TTSProvider,
)
from importlib import metadata
import os
import re

from .elevenlabs import ElevenLabsProvider

_PROVIDERS = {"elevenlabs": ElevenLabsProvider}
_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _entry_points():
    """Return only reviewed-name provider entry points from installed packages."""
    try:
        points = metadata.entry_points()
        points = points.select(group="video_studio.tts_providers")
    except (AttributeError, TypeError):  # Python 3.9 metadata API
        points = metadata.entry_points().get("video_studio.tts_providers", ())
    return {point.name: point for point in points if _PROVIDER_NAME.fullmatch(point.name)}


def provider_names() -> tuple[str, ...]:
    """Finite provider names: built-ins plus installed package entry points."""
    return tuple(sorted(set(_PROVIDERS) | set(_entry_points())))


def configured_provider(name: str | None) -> str:
    selected = name or os.environ.get("VIDEO_STUDIO_TTS_PROVIDER")
    if not selected:
        raise ProviderError(
            "pass --provider or set VIDEO_STUDIO_TTS_PROVIDER; imported audio and SRT "
            "need no TTS provider"
        )
    if selected not in provider_names():
        raise ProviderError(f"unknown TTS provider: {selected} (known: {list(provider_names())})")
    return selected


def get_provider(name: str | None) -> TTSProvider:
    selected = configured_provider(name)
    factory = _PROVIDERS.get(selected)
    if factory is None:
        try:
            factory = _entry_points()[selected].load()
        except Exception as error:
            raise ProviderError(f"could not load TTS provider {selected!r}: {error}") from error
    try:
        provider = factory()
    except Exception as error:
        raise ProviderError(f"could not initialize TTS provider {selected!r}: {error}") from error
    if not isinstance(provider, TTSProvider) or provider.name != selected:
        raise ProviderError(
            f"TTS provider {selected!r} must construct a TTSProvider with name {selected!r}"
        )
    return provider

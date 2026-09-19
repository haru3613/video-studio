"""ElevenLabs timestamped TTS adapter with explicit submission outcomes.

HTTP pre-charge rejections are distinguished from ambiguous transport/server
failures so the caller can release or retain its durable budget reservation.
"""
import base64
import binascii
import json
import os
import urllib.error
import urllib.request

from .base import (
    AlignmentUnit,
    ProviderConfirmedFailure,
    ProviderError,
    ProviderSubmissionUnknown,
    SynthesisResult,
    TTSProvider,
)

MODEL_CREDITS_PER_CHAR = {"eleven_v3": 1.0, "eleven_flash_v2_5": 0.5, "eleven_turbo_v2_5": 0.5}
# Internal release policy: only responses that prove the provider rejected the
# request before synthesis may release a reservation. Timeout-like or
# state-conflict statuses (notably 408 and 409) are deliberately absent.
CONFIRMED_NO_CHARGE_HTTP_STATUSES = frozenset(
    {400, 401, 402, 403, 404, 413, 422, 429}
)


def load_key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    path = os.getenv("ELEVENLABS_API_KEY_PATH")
    if key and path:
        raise ProviderConfirmedFailure(
            "ERROR: configure only one of ELEVENLABS_API_KEY or ELEVENLABS_API_KEY_PATH",
            proof="local_preflight:conflicting_credential_configuration",
        )
    if path:
        try:
            key = open(os.path.expanduser(path), encoding="utf-8").read().strip()
        except OSError as error:
            raise ProviderConfirmedFailure(
                f"ERROR: cannot read ELEVENLABS_API_KEY_PATH: {error}",
                proof="local_preflight:credential_file_unreadable",
            ) from error
    if not key:
        raise ProviderConfirmedFailure(
            "ERROR: set ELEVENLABS_API_KEY or ELEVENLABS_API_KEY_PATH",
            proof="local_preflight:credential_missing",
        )
    return key


class ElevenLabsProvider(TTSProvider):
    name = "elevenlabs"

    def credits_for(self, text: str, model: str) -> int:
        return max(1, int(len(text) * MODEL_CREDITS_PER_CHAR.get(model, 1.0)))

    def synthesize(self, text, *, voice, model, stability, speed,
                   previous_request_ids=None, next_request_ids=None,
                   previous_text=None, next_text=None,
                   seed=None, pronunciation_dictionary_locators=None,
                   stream=False) -> SynthesisResult:
        key = load_key()
        body = {
            "text": text,
            "model_id": model,
            "voice_settings": {"stability": stability, "similarity_boost": 0.75, "speed": speed},
        }
        if previous_request_ids:
            body["previous_request_ids"] = list(previous_request_ids)[-3:]
        if next_request_ids:
            body["next_request_ids"] = list(next_request_ids)[:3]
        if previous_text:
            body["previous_text"] = previous_text
        if next_text:
            body["next_text"] = next_text
        if seed is not None:
            body["seed"] = seed
        if pronunciation_dictionary_locators:
            body["pronunciation_dictionary_locators"] = list(
                pronunciation_dictionary_locators
            )
        endpoint = "stream/with-timestamps" if stream else "with-timestamps"
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/{endpoint}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json"}, method="POST")
        try:
            response = urllib.request.urlopen(req, timeout=900 if stream else 180)
            headers = getattr(response, "headers", {})
            if stream:
                chunks = [json.loads(line) for line in response if line.strip()]
                if not chunks:
                    raise ProviderSubmissionUnknown(
                        "ERROR: empty streaming response"
                    )
                audio = b"".join(
                    base64.b64decode(chunk.get("audio_base64", ""))
                    for chunk in chunks
                )
                units = []
                for chunk in chunks:
                    al = chunk.get("alignment") or chunk.get("normalized_alignment") or {}
                    chars = al.get("characters", [])
                    starts = al.get("character_start_times_seconds", [])
                    ends = al.get("character_end_times_seconds", [])
                    if not (len(chars) == len(starts) == len(ends)):
                        raise ProviderSubmissionUnknown(
                            "ERROR: malformed streaming alignment",
                            provider_request_id=chunk.get("request_id"),
                        )
                    offset = (
                        units[-1].end
                        if units and starts and starts[0] < units[-1].start
                        else 0.0
                    )
                    units.extend(
                        AlignmentUnit(c, s + offset, e + offset)
                        for c, s, e in zip(chars, starts, ends)
                    )
                d = chunks[-1]
            else:
                d = json.load(response)
                audio = base64.b64decode(d["audio_base64"])
                al = d.get("alignment") or d.get("normalized_alignment") or {}
                chars = al.get("characters", [])
                starts = al.get("character_start_times_seconds", [])
                ends = al.get("character_end_times_seconds", [])
                units = [AlignmentUnit(c, s, e) for c, s, e in zip(chars, starts, ends)]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code in CONFIRMED_NO_CHARGE_HTTP_STATUSES:
                raise ProviderConfirmedFailure(
                    f"ERROR HTTP {e.code}: {detail}",
                    proof=f"provider_http_rejection:{e.code}",
                ) from e
            raise ProviderSubmissionUnknown(
                f"ERROR HTTP {e.code}: {detail}"
            ) from e
        except (
            OSError,
            TimeoutError,
            json.JSONDecodeError,
            binascii.Error,
            KeyError,
            TypeError,
            AttributeError,
        ) as e:
            raise ProviderSubmissionUnknown(
                f"ERROR: ElevenLabs submission outcome unknown: {e}"
            ) from e
        if not units or not audio:
            raise ProviderSubmissionUnknown(
                "ERROR: accepted response had no usable audio/alignment",
                provider_request_id=(
                    d.get("request_id")
                    if isinstance(d, dict)
                    else None
                ),
            )

        def header(*names):
            for name in names:
                if hasattr(headers, "get"):
                    value = headers.get(name)
                    if value is not None:
                        return value
                for key, value in getattr(headers, "items", lambda: [])():
                    if key.lower() == name.lower():
                        return value
            return None

        raw_cost = header("character-cost", "x-character-cost")
        try:
            character_cost = int(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            character_cost = None
        metadata = {
            "request_id": d.get("request_id") or header("request-id", "x-request-id"),
            "history_item_id": d.get("history_item_id") or header(
                "history-item-id", "x-history-item-id"),
            "character_cost": character_cost,
        }
        return SynthesisResult(audio=audio, units=units, granularity="char", metadata=metadata)

    def create_pronunciation_dictionary(self, *, name, rules, description=None) -> dict:
        body = {"name": name, "rules": rules}
        if description:
            body["description"] = description
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/pronunciation-dictionaries/add-from-rules",
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"xi-api-key": load_key(), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            payload = json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            raise ProviderError(f"ERROR HTTP {e.code}: {e.read().decode()[:400]}")
        except (OSError, TimeoutError, json.JSONDecodeError) as e:
            raise ProviderError(f"ERROR: pronunciation dictionary request failed: {e}") from e
        if not isinstance(payload.get("id"), str) or not isinstance(
            payload.get("version_id"), str
        ):
            raise ProviderError("ERROR: invalid pronunciation dictionary response")
        return {
            "pronunciation_dictionary_id": payload["id"],
            "version_id": payload["version_id"],
        }

    def maximum_text_length(self, model: str) -> int:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/models",
            headers={"xi-api-key": load_key()},
            method="GET",
        )
        try:
            payload = json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            raise ProviderError(f"ERROR HTTP {e.code}: {e.read().decode()[:400]}")
        except (OSError, TimeoutError, json.JSONDecodeError) as e:
            raise ProviderError(f"ERROR: model limit request failed: {e}") from e
        for item in payload:
            if item.get("model_id") == model:
                limit = item.get("maximum_text_length_per_request")
                if isinstance(limit, int) and limit > 0:
                    return limit
                break
        raise ProviderError(f"ERROR: no text limit returned for model {model}")

    def subscription_usage(self) -> int:
        """Return ElevenLabs' exact cycle usage counter (read-only API call)."""
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": load_key()},
            method="GET",
        )
        try:
            payload = json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            raise ProviderError(f"ERROR HTTP {e.code}: {e.read().decode()[:400]}")
        try:
            return int(payload["character_count"])
        except (KeyError, TypeError, ValueError):
            raise ProviderError("ERROR: subscription response missing character_count")

    def history_records(self, request_ids) -> dict[str, dict]:
        """Return bounded official history evidence for exact request IDs."""
        wanted = set(request_ids)
        if not wanted:
            return {}
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/history?page_size=100",
            headers={"xi-api-key": load_key()},
            method="GET",
        )
        try:
            payload = json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            raise ProviderError(f"ERROR HTTP {e.code}: {e.read().decode()[:400]}")
        records = {}
        for item in payload.get("history", []):
            request_id = item.get("request_id")
            if request_id not in wanted:
                continue
            try:
                start = int(item["character_count_change_from"])
                end = int(item["character_count_change_to"])
            except (KeyError, TypeError, ValueError):
                continue
            records[request_id] = {
                "request_id": request_id,
                "voice_id": item.get("voice_id"),
                "model_id": item.get("model_id"),
                "text": item.get("text"),
                "character_cost": max(0, end - start),
            }
        return records

    def history_costs(self, request_ids) -> dict[str, int]:
        """Return exact official credit deltas for recent request IDs."""
        return {
            request_id: record["character_cost"]
            for request_id, record in self.history_records(request_ids).items()
        }

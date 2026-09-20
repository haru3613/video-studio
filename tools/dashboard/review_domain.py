"""Shared stdlib review-comment mutation domain for UI, CLI, and MCP."""

from __future__ import annotations

import datetime as dt
import math
import uuid
from collections.abc import Callable
from typing import Any

try:
    from .review_store import ReviewStore
except ImportError:
    from review_store import ReviewStore


MAX_COMMENT_BODY = 5000
SNAPSHOT_KEYS = ("id", "kind", "label", "source", "project", "path", "sha256", "bytes", "url")


class ReviewDomainError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical_uuid(value: object) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return str(parsed) == value


def public_comment(comment: dict) -> dict:
    return {key: value for key, value in comment.items() if not key.startswith("_")}


def validate_timestamp(value: object, asset: dict) -> float | None:
    if asset.get("kind") == "cover":
        if value is not None:
            raise ReviewDomainError("invalid_timestamp")
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ReviewDomainError("invalid_timestamp")
    duration = asset.get("duration_seconds")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(duration)
        or value < 0
        or value > duration
    ):
        raise ReviewDomainError("invalid_timestamp")
    return float(value)


def _same_comment(existing: dict, payload: dict, timestamp: float | None = None) -> bool:
    expected_timestamp = payload.get("timestamp_seconds") if timestamp is None else timestamp
    return (
        existing.get("package_id") == payload.get("package_id")
        and existing.get("asset", {}).get("id") == payload.get("asset_id")
        and existing.get("asset", {}).get("sha256") == payload.get("asset_sha256")
        and existing.get("timestamp_seconds") == expected_timestamp
        and existing.get("body") == str(payload.get("body", "")).strip()
    )


def add_comment(
    store: ReviewStore,
    payload: dict,
    *,
    load_current: Callable[[bool], dict],
    snapshot_fingerprint: Callable[[dict], tuple[str | None, int]],
) -> tuple[dict, str, str]:
    """Append one digest-bound comment, or return its identical retry."""

    required = {
        "client_id",
        "package_id",
        "asset_id",
        "asset_sha256",
        "timestamp_seconds",
        "body",
    }
    if set(payload) != required or not canonical_uuid(payload.get("client_id")):
        raise ReviewDomainError("invalid_input")
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_COMMENT_BODY:
        raise ReviewDomainError("invalid_input")

    existing = next(
        (item for item in store.read_comments() if item.get("client_id") == payload["client_id"]),
        None,
    )
    if existing is not None:
        if not _same_comment(existing, payload):
            raise ReviewDomainError("review_conflict")
        return existing, "review_comment_existing", existing["package_id"]

    current = load_current(True)
    asset = next(
        (item for item in current.get("assets", []) if item.get("id") == payload.get("asset_id")),
        None,
    )
    if (
        asset is None
        or asset.get("sha256") is None
        or payload.get("asset_sha256") != asset.get("sha256")
    ):
        raise ReviewDomainError("review_stale")
    if payload.get("package_id") != current.get("package_id"):
        raise ReviewDomainError("review_conflict")
    timestamp = validate_timestamp(payload.get("timestamp_seconds"), asset)
    snapshot = {key: asset.get(key) for key in SNAPSHOT_KEYS}

    def append(comments: list[dict]) -> tuple[dict, str, str]:
        duplicate = next(
            (item for item in comments if item.get("client_id") == payload["client_id"]),
            None,
        )
        if duplicate is not None:
            if not _same_comment(duplicate, payload, timestamp):
                raise ReviewDomainError("review_conflict")
            return duplicate, "review_comment_existing", duplicate["package_id"]

        latest = load_current(False)
        latest_asset = next(
            (item for item in latest.get("assets", []) if item.get("id") == payload["asset_id"]),
            None,
        )
        if latest_asset is None or latest_asset.get("sha256") != payload["asset_sha256"]:
            raise ReviewDomainError("review_stale")
        if latest.get("package_id") != payload["package_id"]:
            raise ReviewDomainError("review_conflict")
        digest, size = snapshot_fingerprint(snapshot)
        if digest != snapshot["sha256"] or size != snapshot["bytes"]:
            raise ReviewDomainError("review_stale")
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        comment = {
            "id": str(uuid.uuid4()),
            "client_id": payload["client_id"],
            "package_id": payload["package_id"],
            "asset": snapshot,
            "timestamp_seconds": timestamp,
            "body": body.strip(),
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }
        comments.append(comment)
        return comment, "review_comment_added", payload["package_id"]

    return store.update(append)


def resolve_comment(
    store: ReviewStore,
    *,
    comment_id: str,
    status: str,
    expected_package_id: str,
    expected_asset_sha256: str,
) -> tuple[dict, str, str]:
    """Resolve/reopen one exact persisted note version without approval effects."""

    if not canonical_uuid(comment_id) or status not in {"open", "resolved"}:
        raise ReviewDomainError("invalid_input")
    if not isinstance(expected_package_id, str) or not isinstance(expected_asset_sha256, str):
        raise ReviewDomainError("invalid_input")
    if not any(item.get("id") == comment_id for item in store.read_comments()):
        raise ReviewDomainError("comment_not_found")

    def mutate(comments: list[dict]) -> tuple[dict, str, str]:
        comment = next((item for item in comments if item.get("id") == comment_id), None)
        if comment is None:
            raise ReviewDomainError("review_conflict")
        if comment.get("package_id") != expected_package_id:
            raise ReviewDomainError("review_conflict")
        asset = comment.get("asset", {})
        if asset.get("sha256") != expected_asset_sha256:
            raise ReviewDomainError("review_conflict")
        code = "review_comment_unchanged"
        if comment.get("status") != status:
            comment["status"] = status
            comment["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            code = "review_comment_updated"
        return comment, code, expected_package_id

    return store.update(mutate)

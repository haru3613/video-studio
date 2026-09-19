#!/usr/bin/env python3
"""Fail-closed, resumable YouTube upload runner."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_status
import canonical_layout
import upload_attempts
import youtube_credentials


SCHEMA = "haru.youtube_upload.v1"
THUMBNAIL_SCHEMA = "haru.youtube_thumbnail.v1"
RECONCILE_SCHEMA = "haru.youtube_upload_reconcile.v1"
UPLOADS_PLAYLIST = re.compile(r"^UU[A-Za-z0-9_-]{22}$")

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
RUNTIME_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
CHUNK_BYTES = 8 * 1024 * 1024
QUOTA_REASONS = {
    "quotaExceeded",
    "dailyLimitExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}


class UploadError(Exception):
    def __init__(self, code, *, blockers=None):
        super().__init__(code)
        self.code = code
        self.blockers = blockers or []


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UploadError("youtube_upload_state_invalid") from error
    if not isinstance(value, dict):
        raise UploadError("youtube_upload_state_invalid")
    return value


def atomic_json(path, value, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if "youtube-upload-secrets" in path.parts:
        path.parent.chmod(0o700)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(mode)
    os.replace(temporary, path)


def http_request(method, url, headers=None, body=None, timeout=120):
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()
    except (OSError, urllib.error.URLError) as error:
        raise UploadError("youtube_upload_interrupted") from error


def header(headers, name):
    lowered = name.lower()
    return next(
        (value for key, value in headers.items() if key.lower() == lowered),
        None,
    )


def api_error(status, body):
    try:
        reasons = {
            item.get("reason")
            for item in json.loads(body).get("error", {}).get("errors", [])
            if isinstance(item, dict)
        }
    except (AttributeError, json.JSONDecodeError):
        reasons = set()
    if status in {401, 403} and not reasons.intersection(QUOTA_REASONS):
        return "youtube_upload_auth_required"
    if reasons.intersection(QUOTA_REASONS):
        return "youtube_quota_exhausted"
    return "youtube_api_failed"


def resolve_token(reference, expected_channel_id):
    try:
        return youtube_credentials.resolve_and_refresh(reference, expected_channel_id)
    except youtube_credentials.CredentialError as error:
        raise UploadError(error.code) from None

def attempt_call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except upload_attempts.AttemptError as error:
        raise UploadError(error.code) from None


def project_artifact(project, workspace, info):
    path_value = info.get("path") if isinstance(info, dict) else None
    if not isinstance(path_value, str) or not path_value:
        raise UploadError("youtube_publish_preflight_failed")
    path = Path(path_value)
    if not path.is_absolute():
        path = workspace / path
    path = agent_status.project_file(path, project)
    if path is None:
        raise UploadError("youtube_publish_preflight_failed")
    return path


def canonical_channel_id():
    manifest = read_json(Path(__file__).parents[1] / "pipeline/runtime-manifest.json")
    channel_id = manifest.get("youtube_channel_id")
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise UploadError("youtube_upload_state_invalid")
    return channel_id




def preflight(project, workspace):
    status, _artifacts = agent_status.build(project, workspace)
    if (
        status.get("overall_status") != "publish_approved"
        or status.get("blockers")
        or status.get("blocker_details")
    ):
        raise UploadError(
            "youtube_publish_preflight_failed",
            blockers=status.get("blocker_details") or status.get("blockers"),
        )
    approval = status.get("publish_approval") or {}
    if approval.get("state") != "valid":
        raise UploadError("youtube_publish_approval_required")
    approval_path = agent_status.project_file(
        project / "publish/publish-approval.json", project
    )
    if approval_path is None:
        raise UploadError("youtube_publish_approval_required")
    approval_record = read_json(approval_path)
    channel_id = canonical_channel_id()
    approval_digest = approval_record.get("approval_intent_sha256")
    if (
        approval_record.get("schema") != agent_status.SCHEMA_PUBLISH_APPROVAL
        or approval_record.get("project_id") != project.name
        or approval_record.get("channel_id") != channel_id
        or approval_record.get("visibility") != agent_status.PUBLISH_VISIBILITY
        or approval.get("approval_intent_sha256") != approval_digest
        or re.fullmatch(r"[0-9a-f]{64}", approval_digest or "") is None
    ):
        raise UploadError("youtube_publish_approval_required")
    canonical = status.get("canonical_artifacts") or {}
    final_info = canonical.get("final_video") or {}
    final = project_artifact(project, workspace, final_info)
    final_sha = sha256(final)
    if final_sha != final_info.get("sha256"):
        raise UploadError("youtube_publish_sha_mismatch")
    cover = project_artifact(project, workspace, canonical.get("cover") or {})
    if cover.stat().st_size > 2 * 1024 * 1024:
        raise UploadError("youtube_thumbnail_too_large")
    metadata_path = project_artifact(
        project, workspace, canonical.get("publish_metadata") or {}
    )
    metadata = read_json(metadata_path)
    try:
        canonical_layout.validate_publish_metadata(metadata, project.name)
    except ValueError as error:
        raise UploadError("youtube_publish_metadata_invalid") from error
    if (
        final.stat().st_size != approval_record.get("final_bytes")
        or final_sha != approval_record.get("final_sha256")
    ):
        raise UploadError("youtube_publish_approval_required")
    if (
        not isinstance(metadata.get("made_for_kids"), bool)
        or not isinstance(metadata.get("category_id"), str)
        or not re.fullmatch(r"\d+", metadata["category_id"])
    ):
        raise UploadError("youtube_publish_metadata_invalid")
    description = "\n\n".join(
        (
            metadata["description"].strip(),
            metadata["source_statement"].strip(),
            " ".join(metadata["hashtags"]),
        )
    )
    if len(metadata["title"]) > 100 or len(description) > 5000:
        raise UploadError("youtube_publish_metadata_invalid")
    cover_sha = sha256(cover)
    metadata_sha = sha256(metadata_path)
    if (
        cover_sha != approval_record.get("cover_sha256")
        or metadata_sha != approval_record.get("metadata_sha256")
    ):
        raise UploadError("youtube_publish_approval_required")
    return {
        "status": status,
        "approval": approval,
        "final": final,
        "final_sha256": final_sha,
        "cover": cover,
        "cover_sha256": cover_sha,
        "metadata": metadata,
        "metadata_sha256": metadata_sha,
        "description": description,
        "approval_record": approval_record,
        "approval_intent_sha256": approval_digest,
        "channel_id": channel_id,
    }


def request_digest(context, runtime_id, runtime_binary_sha256):
    request = {
        "final_sha256": context["final_sha256"],
        "cover_sha256": context["cover_sha256"],
        "metadata_sha256": context["metadata_sha256"],
        "visibility": agent_status.PUBLISH_VISIBILITY,
        "runtime_id": runtime_id,
        "runtime_binary_sha256": runtime_binary_sha256,
        "approval_intent_sha256": context["approval_intent_sha256"],
        "channel_id": context["channel_id"],
    }
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def parse_video(body):
    try:
        value = json.loads(body)
        video_id = value["id"]
        channel_id = value.get("snippet", {}).get("channelId")
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise UploadError("youtube_api_failed") from error
    if not VIDEO_ID.fullmatch(video_id) or (
        channel_id is not None and not CHANNEL_ID.fullmatch(channel_id)
    ):
        raise UploadError("youtube_api_failed")
    return video_id, channel_id


def next_offset(headers, total):
    range_value = header(headers, "Range")
    if range_value is None:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", range_value)
    if not match:
        raise UploadError("youtube_api_failed")
    offset = int(match.group(1)) + 1
    if offset > total:
        raise UploadError("youtube_api_failed")
    return offset


def initiate(token, context, visibility):
    metadata = context["metadata"]
    body = json.dumps(
        {
            "snippet": {
                "title": metadata["title"].strip(),
                "description": context["description"],
                "tags": [tag.removeprefix("#") for tag in metadata["hashtags"]],
                "categoryId": metadata["category_id"],
            },
            "status": {
                "privacyStatus": visibility,
                "selfDeclaredMadeForKids": metadata["made_for_kids"],
            },
        },
        ensure_ascii=False,
    ).encode()
    query = urllib.parse.urlencode(
        {"uploadType": "resumable", "part": "snippet,status"}
    )
    status, headers, response = http_request(
        "POST",
        f"https://www.googleapis.com/upload/youtube/v3/videos?{query}",
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "Content-Length": str(len(body)),
            "X-Upload-Content-Length": str(context["final"].stat().st_size),
            "X-Upload-Content-Type": "video/mp4",
        },
        body,
    )
    if status not in {200, 201}:
        raise UploadError(api_error(status, response))
    session_url = header(headers, "Location")
    if not valid_session_url(session_url):
        raise UploadError("youtube_api_failed")
    return session_url


def valid_session_url(session_url):
    parsed = urllib.parse.urlparse(session_url or "")
    if parsed.scheme != "https" or not (
        parsed.hostname == "www.googleapis.com"
        or (parsed.hostname or "").endswith(".googleapis.com")
    ):
        return False
    return True


def query_session(token, session_url, total):
    status, headers, body = http_request(
        "PUT",
        session_url,
        {
            "Authorization": f"Bearer {token}",
            "Content-Length": "0",
            "Content-Range": f"bytes */{total}",
        },
        b"",
    )
    if status in {200, 201}:
        return parse_video(body)
    if status == 308:
        return next_offset(headers, total)
    if status in {404, 410}:
        raise UploadError("youtube_upload_session_expired")
    raise UploadError(api_error(status, body))


def upload_chunks(token, session_url, video, offset, before, update):
    total = video.stat().st_size
    with video.open("rb") as handle:
        while offset < total:
            handle.seek(offset)
            body = handle.read(min(CHUNK_BYTES, total - offset))
            end = offset + len(body) - 1
            before(offset)
            status, headers, response = http_request(
                "PUT",
                session_url,
                {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                },
                body,
            )
            if status in {200, 201}:
                return parse_video(response)
            if status in {404, 410}:
                raise UploadError("youtube_upload_session_expired")
            if status != 308:
                raise UploadError(api_error(status, response))
            accepted = next_offset(headers, total)
            if accepted <= offset or accepted > end + 1:
                raise UploadError("youtube_api_failed")
            offset = accepted
            update(offset)
    raise UploadError("youtube_api_failed")


def upload_thumbnail(token, video_id, cover):
    query = urllib.parse.urlencode({"videoId": video_id, "uploadType": "media"})
    body = cover.read_bytes()
    status, _headers, response = http_request(
        "POST",
        f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?{query}",
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "image/png",
            "Content-Length": str(len(body)),
        },
        body,
    )
    if status not in {200, 201}:
        raise UploadError(api_error(status, response))

def readback_from_item(item, context, *, thumbnail_uploaded):
    try:
        video_id = item["id"]
        snippet = item["snippet"]
        remote_status = item["status"]
    except (KeyError, TypeError) as error:
        raise UploadError("youtube_upload_remote_mismatch") from error
    metadata = context["metadata"]
    expected_tags = [tag.removeprefix("#") for tag in metadata["hashtags"]]
    matches_approval = (
        VIDEO_ID.fullmatch(video_id or "") is not None
        and snippet.get("title") == metadata["title"].strip()
        and snippet.get("description") == context["description"]
        and snippet.get("categoryId") == metadata["category_id"]
        and snippet.get("tags", []) == expected_tags
        and remote_status.get("selfDeclaredMadeForKids") == metadata["made_for_kids"]
    )
    thumbnails = snippet.get("thumbnails")
    return {
        "authenticated": True,
        "video_id": video_id,
        "channel_id": snippet.get("channelId"),
        "privacy_status": remote_status.get("privacyStatus"),
        "matches_approval": matches_approval,
        "thumbnail_matches": thumbnail_uploaded
        and isinstance(thumbnails, dict)
        and bool(thumbnails),
    }


def readback_video(token, video_id, context, *, thumbnail_uploaded):
    query = urllib.parse.urlencode(
        {"part": "snippet,status", "id": video_id}
    )
    status, _headers, body = http_request(
        "GET",
        f"https://www.googleapis.com/youtube/v3/videos?{query}",
        {"Authorization": f"Bearer {token}"},
    )
    if status not in {200, 201}:
        raise UploadError(api_error(status, body))
    try:
        payload = json.loads(body)
        items = payload["items"]
        if not isinstance(items, list) or len(items) != 1:
            raise UploadError("youtube_upload_remote_mismatch")
        item = items[0]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise UploadError("youtube_upload_remote_mismatch") from error
    if item.get("id") != video_id:
        raise UploadError("youtube_upload_remote_mismatch")
    return readback_from_item(
        item, context, thumbnail_uploaded=thumbnail_uploaded
    )



def _json_get(token, url):
    status, _headers, body = http_request(
        "GET",
        url,
        {"Authorization": f"Bearer {token}"},
    )
    if status not in {200, 201}:
        raise UploadError(api_error(status, body))
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise UploadError("youtube_api_failed") from error
    if not isinstance(payload, dict):
        raise UploadError("youtube_api_failed")
    return payload


def uploads_playlist_id(token, expected_channel_id):
    query = urllib.parse.urlencode({"part": "id,contentDetails", "mine": "true"})
    payload = _json_get(
        token,
        f"https://www.googleapis.com/youtube/v3/channels?{query}",
    )
    try:
        items = payload["items"]
        if not isinstance(items, list) or len(items) != 1:
            raise UploadError("youtube_upload_channel_mismatch")
        item = items[0]
        if item.get("id") != expected_channel_id:
            raise UploadError("youtube_upload_channel_mismatch")
        playlist_id = item["contentDetails"]["relatedPlaylists"]["uploads"]
    except (KeyError, TypeError) as error:
        raise UploadError("youtube_api_failed") from error
    if not UPLOADS_PLAYLIST.fullmatch(playlist_id or ""):
        raise UploadError("youtube_api_failed")
    return playlist_id


def list_upload_video_ids(token, playlist_id):
    video_ids = []
    page_token = None
    while True:
        query = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": "50",
        }
        if page_token:
            query["pageToken"] = page_token
        payload = _json_get(
            token,
            "https://www.googleapis.com/youtube/v3/playlistItems?"
            + urllib.parse.urlencode(query),
        )
        try:
            items = payload.get("items") or []
            if not isinstance(items, list):
                raise UploadError("youtube_api_failed")
            for item in items:
                video_id = item["contentDetails"]["videoId"]
                if not VIDEO_ID.fullmatch(video_id or ""):
                    raise UploadError("youtube_api_failed")
                video_ids.append(video_id)
            page_token = payload.get("nextPageToken")
        except (KeyError, TypeError) as error:
            raise UploadError("youtube_api_failed") from error
        if not page_token:
            return video_ids


def matching_channel_videos(token, context, video_ids):
    matches = []
    for index in range(0, len(video_ids), 50):
        batch = video_ids[index : index + 50]
        query = urllib.parse.urlencode(
            {"part": "snippet,status", "id": ",".join(batch)}
        )
        payload = _json_get(
            token,
            f"https://www.googleapis.com/youtube/v3/videos?{query}",
        )
        try:
            items = payload.get("items") or []
            if not isinstance(items, list):
                raise UploadError("youtube_api_failed")
        except TypeError as error:
            raise UploadError("youtube_api_failed") from error
        for item in items:
            if not isinstance(item, dict):
                raise UploadError("youtube_api_failed")
            video_id = item.get("id")
            if not VIDEO_ID.fullmatch(video_id or ""):
                raise UploadError("youtube_api_failed")
            readback = readback_from_item(
                item, context, thumbnail_uploaded=True
            )
            if (
                readback.get("matches_approval") is True
                and readback.get("privacy_status") == "unlisted"
                and readback.get("channel_id") == context["channel_id"]
            ):
                matches.append(readback)
    return matches


def consume_restart_override(ref, project_id, publish_intent_id, attempt_generation):
    try:
        import approval_attestation

        return approval_attestation.consume_reconcile(
            ref, project_id, publish_intent_id, attempt_generation
        )
    except ValueError as error:
        raise UploadError("youtube_upload_override_invalid") from error


def reconcile(
    project,
    workspace,
    credential_reference,
    idempotency_key,
    runtime_id,
    runtime_binary_sha256,
    override_attestation_ref=None,
):
    """Read the authenticated channel and close one fenced publish intent.

    This never starts a second session or video. A matching remote video
    completes the intent, including the initial thumbnail. Absence authorizes
    a later restart only when it can be proved, or when an attempt-bound
    human override is consumed. Local ``bytes_confirmed = 0`` is not proof.
    """
    if (
        not KEY.fullmatch(idempotency_key)
        or not RUNTIME_DIGEST.fullmatch(runtime_id)
        or not RUNTIME_DIGEST.fullmatch(runtime_binary_sha256)
        or (
            override_attestation_ref is not None
            and (
                not isinstance(override_attestation_ref, str)
                or not override_attestation_ref
            )
        )
    ):
        raise UploadError("youtube_upload_input_invalid")
    project_input = Path(project)
    workspace = Path(workspace).resolve()
    if project_input.is_symlink() or not project_input.is_dir():
        raise UploadError("youtube_upload_input_invalid")
    project = project_input.resolve()
    context = preflight(project, workspace)
    approval_digest = context["approval_intent_sha256"]
    state = canonical_layout.direct_path(project / ".hvp", project)
    if state is None:
        raise UploadError("youtube_upload_state_invalid")
    state.mkdir(exist_ok=True)
    lock_path = canonical_layout.direct_path(state / "youtube-upload.lock", project)
    if lock_path is None:
        raise UploadError("youtube_upload_state_invalid")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        attempts = attempt_call(upload_attempts.UploadAttemptStore, project)
        try:
            attempt = attempt_call(attempts.get, approval_digest)
        except UploadError as error:
            if error.code == "youtube_upload_state_invalid":
                raise UploadError("youtube_upload_reconcile_absent") from error
            raise
        if attempt["state"] == "complete":
            return {
                "schema": RECONCILE_SCHEMA,
                "ok": True,
                "project": project.name,
                "status": "complete",
                "code": "youtube_upload_reconciled",
                "publish_intent_id": attempt["publish_intent_id"],
                "video_id": attempt["video_id"],
                "final_sha256": context["final_sha256"],
                "visibility": "unlisted",
                "runtime_id": runtime_id,
                "runtime_binary_sha256": runtime_binary_sha256,
                "channel_id": context["channel_id"],
                "override_consumed": False,
            }
        if attempt["state"] in {"prepared", "aborted"}:
            raise UploadError("youtube_upload_reconcile_not_required")

        token = resolve_token(credential_reference, context["channel_id"])
        known_video = attempt.get("video_id")
        if isinstance(known_video, str) and VIDEO_ID.fullmatch(known_video):
            matches = [
                readback_video(
                    token,
                    known_video,
                    context,
                    thumbnail_uploaded=True,
                )
            ]
            if (
                matches[0].get("matches_approval") is not True
                or matches[0].get("privacy_status") != "unlisted"
                or matches[0].get("channel_id") != context["channel_id"]
            ):
                raise UploadError("youtube_upload_remote_mismatch")
        else:
            playlist_id = uploads_playlist_id(token, context["channel_id"])
            video_ids = list_upload_video_ids(token, playlist_id)
            matches = matching_channel_videos(token, context, video_ids)

        if len(matches) > 1:
            raise UploadError("youtube_upload_remote_ambiguous")
        if len(matches) == 1:
            readback = matches[0]
            video_id = readback["video_id"]
            if attempt["state"] == "remote_outcome_unknown":
                attempt_call(
                    attempts.record_video,
                    approval_digest,
                    video_id=video_id,
                    channel_id=context["channel_id"],
                )
            elif attempt["state"] not in {"video_uploaded", "reconciliation_required"}:
                attempt_call(
                    attempts.require_reconciliation,
                    approval_digest,
                    "remote_video_discovered",
                )
            uploaded_at = now()
            bind_approval(
                project, context, video_id, context["channel_id"], uploaded_at
            )
            upload_thumbnail(token, video_id, context["cover"])
            confirmed = readback_video(
                token, video_id, context, thumbnail_uploaded=True
            )
            try:
                completed = attempt_call(
                    attempts.complete, approval_digest, confirmed
                )
            except UploadError as error:
                attempt_call(
                    attempts.require_reconciliation,
                    approval_digest,
                    error.code,
                )
                raise
            return {
                "schema": RECONCILE_SCHEMA,
                "ok": True,
                "project": project.name,
                "status": completed["state"],
                "code": "youtube_upload_reconciled",
                "publish_intent_id": completed["publish_intent_id"],
                "video_id": completed["video_id"],
                "final_sha256": context["final_sha256"],
                "visibility": "unlisted",
                "runtime_id": runtime_id,
                "runtime_binary_sha256": runtime_binary_sha256,
                "channel_id": context["channel_id"],
                "override_consumed": False,
            }

        if attempt.get("media_put_issued") and override_attestation_ref is None:
            attempt_call(
                attempts.require_reconciliation,
                approval_digest,
                "absence_unproved",
            )
            raise UploadError("youtube_upload_absence_unproved")


        evidence = {
            "authenticated": True,
            "authoritative_absence": True,
            "channel_id": context["channel_id"],
            "checked_at": now(),
            "search_window": "attempt-bound",
        }
        override_consumed = False
        if override_attestation_ref is not None:
            consume_restart_override(
                override_attestation_ref,
                project.name,
                attempt["publish_intent_id"],
                attempt["attempt_generation"],
            )
            override_consumed = True
            evidence["override_attestation_ref"] = override_attestation_ref
        elif attempt.get("media_put_issued"):
            raise UploadError("youtube_upload_absence_unproved")
        elif attempt.get("session_post_issued") and attempt_call(
            attempts.session, approval_digest
        ):
            attempt_call(
                attempts.require_reconciliation,
                approval_digest,
                "protected_session_still_present",
            )
            raise UploadError("youtube_upload_absence_unproved")

        if attempt["state"] != "reconciliation_required":
            attempt_call(
                attempts.require_reconciliation,
                approval_digest,
                "authenticated_absence",
            )
        restarted = attempt_call(
            attempts.authorize_restart, approval_digest, evidence
        )
        return {
            "schema": RECONCILE_SCHEMA,
            "ok": True,
            "project": project.name,
            "status": restarted["state"],
            "code": "youtube_upload_restart_authorized",
            "publish_intent_id": restarted["publish_intent_id"],
            "video_id": None,
            "final_sha256": context["final_sha256"],
            "visibility": "unlisted",
            "runtime_id": runtime_id,
            "runtime_binary_sha256": runtime_binary_sha256,
            "channel_id": context["channel_id"],
            "override_consumed": override_consumed,
            "attempt_generation": restarted["attempt_generation"],
        }





def bind_approval(project, context, video_id, channel_id, uploaded_at):
    path = agent_status.project_file(
        project / "publish/publish-approval.json", project
    )
    if path is None:
        raise UploadError("youtube_publish_approval_required")
    approval = read_json(path)
    if (
        approval.get("schema") != agent_status.SCHEMA_PUBLISH_APPROVAL
        or approval.get("approval_intent_sha256")
        != context["approval_intent_sha256"]
        or approval.get("final_sha256") != context["final_sha256"]
        or approval.get("metadata_sha256") != context["metadata_sha256"]
        or approval.get("cover_sha256") != context["cover_sha256"]
        or approval.get("channel_id") != context["channel_id"]
        or approval.get("visibility") != agent_status.PUBLISH_VISIBILITY
    ):
        raise UploadError("youtube_publish_approval_required")
    if channel_id != context["channel_id"]:
        raise UploadError("youtube_upload_channel_mismatch")
    if approval.get("video_id") not in {None, video_id}:
        raise UploadError("youtube_already_uploaded")
    approval.update(
        {
            "video_id": video_id,
            "uploaded_at": uploaded_at,
        }
    )
    atomic_json(path, approval)


def run(
    project,
    workspace,
    credential_reference,
    idempotency_key,
    runtime_id,
    runtime_binary_sha256,
):
    if (
        not KEY.fullmatch(idempotency_key)
        or not RUNTIME_DIGEST.fullmatch(runtime_id)
        or not RUNTIME_DIGEST.fullmatch(runtime_binary_sha256)
    ):
        raise UploadError("youtube_upload_input_invalid")
    project_input = Path(project)
    workspace = Path(workspace).resolve()
    if project_input.is_symlink() or not project_input.is_dir():
        raise UploadError("youtube_upload_input_invalid")
    project = project_input.resolve()
    context = preflight(project, workspace)
    digest = request_digest(context, runtime_id, runtime_binary_sha256)
    approval_digest = context["approval_intent_sha256"]
    visibility = agent_status.PUBLISH_VISIBILITY

    state = canonical_layout.direct_path(project / ".hvp", project)
    if state is None:
        raise UploadError("youtube_upload_state_invalid")
    state.mkdir(exist_ok=True)
    lock_path = canonical_layout.direct_path(state / "youtube-upload.lock", project)
    receipt_path = canonical_layout.direct_path(
        state / "youtube-uploads" / f"{idempotency_key}.json", project
    )
    if lock_path is None or receipt_path is None:
        raise UploadError("youtube_upload_state_invalid")

    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        attempts = attempt_call(upload_attempts.UploadAttemptStore, project)
        attempt = attempt_call(
            attempts.prepare,
            approval_intent_sha256=approval_digest,
            request_sha256=digest,
            target_channel_id=context["channel_id"],
            visibility=visibility,
            caller_key=idempotency_key,
        )
        receipt = read_json(receipt_path) if receipt_path.exists() else None
        if receipt is not None:
            if (
                receipt.get("schema") != SCHEMA
                or receipt.get("project") != project.name
                or receipt.get("idempotency_key") != idempotency_key
                or receipt.get("request_sha256") != digest
                or receipt.get("runtime_id") != runtime_id
                or receipt.get("runtime_binary_sha256") != runtime_binary_sha256
                or receipt.get("approval_intent_sha256") != approval_digest
                or receipt.get("publish_intent_id")
                != attempt["publish_intent_id"]
                or receipt.get("channel_id") != context["channel_id"]
                or receipt.get("visibility") != visibility
            ):
                raise UploadError("youtube_upload_idempotency_conflict")
            if receipt.get("status") == "complete":
                return receipt
        else:
            receipt = {
                "schema": SCHEMA,
                "project": project.name,
                "idempotency_key": idempotency_key,
                "request_sha256": digest,
                "runtime_id": runtime_id,
                "runtime_binary_sha256": runtime_binary_sha256,
                "publish_intent_id": attempt["publish_intent_id"],
                "approval_intent_sha256": approval_digest,
                "status": attempt["state"],
                "final_sha256": context["final_sha256"],
                "final_bytes": context["final"].stat().st_size,
                "metadata_sha256": context["metadata_sha256"],
                "cover_sha256": context["cover_sha256"],
                "channel_id": context["channel_id"],
                "visibility": visibility,
                "bytes_uploaded": attempt["bytes_confirmed"],
                "video_id": attempt["video_id"],
                "youtube_channel_id": attempt["remote_channel_id"],
                "uploaded_at": None,
                "thumbnail_uploaded": False,
                "prepared_at": now(),
                "updated_at": now(),
            }
            atomic_json(receipt_path, receipt)

        if attempt["state"] == "complete":
            receipt.update(
                {
                    "status": "complete",
                    "video_id": attempt["video_id"],
                    "youtube_channel_id": attempt["remote_channel_id"],
                    "thumbnail_uploaded": True,
                    "updated_at": now(),
                }
            )
            atomic_json(receipt_path, receipt)
            return receipt
        if attempt["state"] == "session_creation_unknown":
            if attempt_call(attempts.session, approval_digest) is None:
                raise UploadError("youtube_upload_reconciliation_required")
            attempt = attempt_call(
                attempts.recover_recorded_session, approval_digest
            )
        if attempt["state"] in {
            "remote_outcome_unknown",
            "reconciliation_required",
            "aborted",
        }:
            raise UploadError("youtube_upload_reconciliation_required")

        token = resolve_token(credential_reference, context["channel_id"])
        video_id = attempt.get("video_id")
        channel_id = attempt.get("remote_channel_id")
        if attempt["state"] == "prepared":
            attempt_call(attempts.before_session_post, approval_digest)
            session_url = initiate(token, context, visibility)
            attempt = attempt_call(
                attempts.record_session, approval_digest, session_url
            )
        elif attempt["state"] in {"session_created", "uploading"}:
            session_url = attempt_call(attempts.session, approval_digest)
            if session_url is None:
                attempt_call(
                    attempts.require_reconciliation,
                    approval_digest,
                    "protected_session_missing",
                )
                raise UploadError("youtube_upload_reconciliation_required")
        elif attempt["state"] == "video_uploaded":
            session_url = None
        else:
            raise UploadError("youtube_upload_reconciliation_required")

        if attempt["state"] in {"session_created", "uploading"}:
            def before(offset):
                attempt_call(
                    attempts.before_put,
                    approval_digest,
                    offset=offset,
                )

            def update(offset):
                progress = attempt_call(
                    attempts.record_progress, approval_digest, offset
                )
                receipt["status"] = progress["state"]
                receipt["bytes_uploaded"] = progress["bytes_confirmed"]
                receipt["updated_at"] = now()
                atomic_json(receipt_path, receipt)

            try:
                video_id, channel_id = upload_chunks(
                    token,
                    session_url,
                    context["final"],
                    attempt["bytes_confirmed"],
                    before,
                    update,
                )
            except UploadError as error:
                if error.code == "youtube_upload_session_expired":
                    attempt_call(attempts.session_expired, approval_digest)
                else:
                    attempt_call(
                        attempts.require_reconciliation,
                        approval_digest,
                        error.code,
                    )
                raise
            try:
                attempt = attempt_call(
                    attempts.record_video,
                    approval_digest,
                    video_id=video_id,
                    channel_id=channel_id,
                )
            except UploadError:
                attempt_call(
                    attempts.require_reconciliation,
                    approval_digest,
                    "insert_response_mismatch",
                )
                raise
        if sha256(context["final"]) != context["final_sha256"]:
            attempt_call(
                attempts.require_reconciliation,
                approval_digest,
                "local_final_changed_after_remote_mutation",
            )
            raise UploadError("youtube_publish_sha_mismatch")

        uploaded_at = receipt.get("uploaded_at") or now()
        receipt.update(
            {
                "status": "video_uploaded",
                "bytes_uploaded": context["final"].stat().st_size,
                "video_id": video_id,
                "youtube_channel_id": channel_id,
                "uploaded_at": uploaded_at,
                "updated_at": uploaded_at,
            }
        )
        atomic_json(receipt_path, receipt)
        bind_approval(project, context, video_id, channel_id, uploaded_at)

        if not receipt.get("thumbnail_uploaded"):
            upload_thumbnail(token, video_id, context["cover"])
            receipt["thumbnail_uploaded"] = True
            receipt["updated_at"] = now()
            atomic_json(receipt_path, receipt)
        try:
            readback = readback_video(
                token,
                video_id,
                context,
                thumbnail_uploaded=receipt["thumbnail_uploaded"],
            )
            attempt_call(attempts.complete, approval_digest, readback)
        except UploadError as error:
            attempt_call(
                attempts.require_reconciliation,
                approval_digest,
                error.code,
            )
            raise

        receipt.update({"status": "complete", "updated_at": now()})
        atomic_json(receipt_path, receipt)
        return receipt


def replace_thumbnail(
    project,
    workspace,
    credential_reference,
    idempotency_key,
    updated_by,
    runtime_id,
    runtime_binary_sha256,
):
    if (
        not KEY.fullmatch(idempotency_key)
        or not updated_by.strip()
        or not RUNTIME_DIGEST.fullmatch(runtime_id)
        or not RUNTIME_DIGEST.fullmatch(runtime_binary_sha256)
    ):
        raise UploadError("youtube_upload_input_invalid")
    project_input = Path(project)
    workspace = Path(workspace).resolve()
    if project_input.is_symlink() or not project_input.is_dir():
        raise UploadError("youtube_upload_input_invalid")
    project = project_input.resolve()
    context = preflight(project, workspace)
    video_id = context["approval"].get("video_id")
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        raise UploadError("youtube_thumbnail_video_missing")
    request = {
        "video_id": video_id,
        "cover_sha256": context["cover_sha256"],
        "runtime_id": runtime_id,
        "runtime_binary_sha256": runtime_binary_sha256,
    }
    digest = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    state = canonical_layout.direct_path(project / ".hvp", project)
    if state is None:
        raise UploadError("youtube_upload_state_invalid")
    state.mkdir(exist_ok=True)
    lock_path = canonical_layout.direct_path(state / "youtube-upload.lock", project)
    receipt_path = canonical_layout.direct_path(
        state / "youtube-thumbnails" / f"{idempotency_key}.json", project
    )
    if lock_path is None or receipt_path is None:
        raise UploadError("youtube_upload_state_invalid")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            if (
                receipt.get("schema") != THUMBNAIL_SCHEMA
                or receipt.get("project") != project.name
                or receipt.get("idempotency_key") != idempotency_key
                or receipt.get("request_sha256") != digest
                or receipt.get("runtime_id") != runtime_id
                or receipt.get("runtime_binary_sha256") != runtime_binary_sha256
            ):
                raise UploadError("youtube_upload_idempotency_conflict")
            if receipt.get("status") == "complete":
                return receipt
        token = resolve_token(credential_reference, context["channel_id"])
        upload_thumbnail(token, video_id, context["cover"])
        updated_at = now()
        approval_path = agent_status.project_file(
            project / "publish/publish-approval.json", project
        )
        if approval_path is None:
            raise UploadError("youtube_publish_approval_required")
        approval = read_json(approval_path)
        if approval.get("video_id") != video_id:
            raise UploadError("youtube_thumbnail_video_missing")
        approval.update(
            {
                "thumbnail_sha256": context["cover_sha256"],
                "thumbnail_updated_at": updated_at,
                "thumbnail_updated_by": updated_by.strip(),
            }
        )
        atomic_json(approval_path, approval)
        receipt = {
            "schema": THUMBNAIL_SCHEMA,
            "project": project.name,
            "idempotency_key": idempotency_key,
            "request_sha256": digest,
            "runtime_id": runtime_id,
            "runtime_binary_sha256": runtime_binary_sha256,
            "status": "complete",
            "video_id": video_id,
            "cover_sha256": context["cover_sha256"],
            "updated_by": updated_by.strip(),
            "updated_at": updated_at,
        }
        atomic_json(receipt_path, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description="Upload one approved HVP video")
    parser.add_argument("project")
    parser.add_argument("--workspace", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--credential-reference", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--runtime-id")
    parser.add_argument("--runtime-binary-sha256")
    parser.add_argument("--thumbnail-only", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--override-attestation-ref")
    parser.add_argument("--updated-by")
    args = parser.parse_args()
    try:
        if args.thumbnail_only and args.reconcile:
            raise UploadError("youtube_upload_input_invalid")
        if args.reconcile:
            receipt = reconcile(
                args.project,
                args.workspace,
                args.credential_reference,
                args.idempotency_key,
                args.runtime_id or "",
                args.runtime_binary_sha256 or "",
                args.override_attestation_ref,
            )
        elif args.thumbnail_only:
            receipt = replace_thumbnail(
                args.project,
                args.workspace,
                args.credential_reference,
                args.idempotency_key,
                args.updated_by or "",
                args.runtime_id or "",
                args.runtime_binary_sha256 or "",
            )
        else:
            receipt = run(
                args.project,
                args.workspace,
                args.credential_reference,
                args.idempotency_key,
                args.runtime_id or "",
                args.runtime_binary_sha256 or "",
            )
    except UploadError as error:
        schema = (
            RECONCILE_SCHEMA
            if args.reconcile
            else THUMBNAIL_SCHEMA
            if args.thumbnail_only
            else SCHEMA
        )
        print(
            json.dumps(
                {
                    "schema": schema,
                    "ok": False,
                    "code": error.code,
                    "blockers": error.blockers,
                },
                ensure_ascii=False,
            )
        )
        return 3 if error.code.startswith("youtube_publish") else 5
    print(json.dumps({"ok": True, **receipt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

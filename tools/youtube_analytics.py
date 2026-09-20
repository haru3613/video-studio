#!/usr/bin/env python3
"""Fetch one read-only YouTube Analytics retro and write durable artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from tools.job_contract import RUN_ID, JobError, atomic_write, repo_path, sha256

ENDPOINT = "https://youtubeanalytics.googleapis.com/v2/reports"
FEED_ENDPOINT = "https://www.youtube.com/feeds/videos.xml"
SCOPES = {
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
METRICS = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "likes",
    "comments",
    "shares",
    "subscribersGained",
    "subscribersLost",
)
QUOTA_REASONS = {
    "quotaExceeded",
    "dailyLimitExceeded",
    "dailyLimitExceededUnreg",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}


class RetroError(Exception):
    def __init__(self, status: str, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(path: Path) -> dict[str, Any]:
    path = path.resolve()
    data = path.read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": sha256(data)}


def completed_retro_pair(
    retros_root: Path, video_id: str
) -> tuple[Path, Path] | None:
    retro_path = retros_root / f"{video_id}.md"
    receipt_path = retros_root / f"{video_id}.receipt.json"
    present = [
        path.exists() or path.is_symlink() for path in (retro_path, receipt_path)
    ]
    if not any(present):
        return None
    if (
        not all(present)
        or retro_path.is_symlink()
        or receipt_path.is_symlink()
        or not retro_path.is_file()
        or not receipt_path.is_file()
    ):
        raise RetroError("error", "retro_write_failed")
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RetroError("error", "retro_write_failed") from error
    if (
        not isinstance(receipt, dict)
        or receipt.get("format_version") != 1
        or receipt.get("schema") != "haru.youtube_retro_receipt.v1"
        or receipt.get("status") != "captured_not_interpreted"
        or receipt.get("video_id") != video_id
        or receipt.get("retro") != digest(retro_path)
    ):
        raise RetroError("error", "retro_write_failed")
    return retro_path, receipt_path


def parse_date(value: str) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise RetroError("error", "retro_input_invalid") from error
    if parsed.isoformat() != value:
        raise RetroError("error", "retro_input_invalid")
    return parsed


def parse_timestamp(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise RetroError("error", "youtube_feed_failed") from error
    if parsed.tzinfo is None:
        raise RetroError("error", "youtube_feed_failed")
    return parsed.astimezone(dt.timezone.utc)


def feed(channel_id: str) -> tuple[list[dict[str, Any]], bytes]:
    if not CHANNEL_ID.fullmatch(channel_id):
        raise RetroError("error", "retro_input_invalid")
    request = urllib.request.Request(
        f"{FEED_ENDPOINT}?{urllib.parse.urlencode({'channel_id': channel_id})}",
        headers={"User-Agent": "video-studio/0.1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
        root = ET.fromstring(raw)
    except (ET.ParseError, OSError, urllib.error.URLError) as error:
        raise RetroError("error", "youtube_feed_failed") from error

    atom = "{http://www.w3.org/2005/Atom}"
    yt = "{http://www.youtube.com/xml/schemas/2015}"
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in root.findall(f"{atom}entry"):
        video_id = entry.findtext(f"{yt}videoId")
        published = entry.findtext(f"{atom}published")
        title = entry.findtext(f"{atom}title")
        if (
            not isinstance(video_id, str)
            or not VIDEO_ID.fullmatch(video_id)
            or video_id in seen
            or not isinstance(published, str)
            or not isinstance(title, str)
            or not title.strip()
        ):
            raise RetroError("error", "youtube_feed_failed")
        seen.add(video_id)
        entries.append(
            {
                "video_id": video_id,
                "title": title.strip(),
                "published_at": parse_timestamp(published),
            }
        )
    return entries, raw


def select_due(
    entries: list[dict[str, Any]],
    retros_root: Path,
    as_of: dt.datetime,
    min_age_days: int,
    max_age_days: int,
) -> dict[str, Any] | None:
    if min_age_days < 0 or max_age_days < min_age_days:
        raise RetroError("error", "retro_input_invalid")
    eligible = []
    for entry in entries:
        age = as_of - entry["published_at"]
        if not dt.timedelta(days=min_age_days) <= age <= dt.timedelta(days=max_age_days):
            continue
        video_id = entry["video_id"]
        if completed_retro_pair(retros_root, video_id):
            continue
        eligible.append(entry)
    # ponytail: YouTube's RSS window is enough for the current low-volume
    # channel; switch discovery to the Data API if uploads exceed that window.
    return min(eligible, key=lambda item: item["published_at"], default=None)


def load_token(path_value: str, repo: Path) -> str:
    repo = repo.resolve()
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink():
        raise RetroError("blocked", "youtube_auth_required")
    path = path.resolve()
    try:
        path.relative_to(repo)
    except ValueError:
        pass
    else:
        raise RetroError("blocked", "youtube_auth_required")
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise RetroError("blocked", "youtube_auth_required")
    try:
        secret = json.loads(path.read_text())
        token = secret["access_token"]
        scopes = secret["scopes"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RetroError("blocked", "youtube_auth_required") from error
    if (
        secret.get("format_version") != 1
        or secret.get("type") != "oauth_access_token"
        or not isinstance(token, str)
        or not token.strip()
        or not isinstance(scopes, list)
        or not all(isinstance(scope, str) for scope in scopes)
        or not SCOPES.issubset(scopes)
    ):
        raise RetroError("blocked", "youtube_auth_required")
    expires_at = secret.get("expires_at")
    if expires_at:
        try:
            expiry = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry <= dt.datetime.now(dt.timezone.utc):
                raise RetroError("blocked", "youtube_auth_required")
        except (AttributeError, TypeError, ValueError) as error:
            raise RetroError("blocked", "youtube_auth_required") from error
    return token


def error_reasons(data: bytes) -> set[str]:
    try:
        errors = json.loads(data).get("error", {}).get("errors", [])
        return {
            item["reason"]
            for item in errors
            if isinstance(item, dict) and isinstance(item.get("reason"), str)
        }
    except (AttributeError, json.JSONDecodeError):
        return set()


def query(token: str, video_id: str, start_date: str, end_date: str) -> tuple[dict[str, Any], bytes]:
    parameters = {
        "ids": "channel==MINE",
        "startDate": start_date,
        "endDate": end_date,
        "metrics": ",".join(METRICS),
        "filters": f"video=={video_id}",
    }
    request = urllib.request.Request(
        f"{ENDPOINT}?{urllib.parse.urlencode(parameters)}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raw = error.read()
        if error.code in {401, 403} and not (error_reasons(raw) & QUOTA_REASONS):
            raise RetroError("blocked", "youtube_auth_required") from error
        if error_reasons(raw) & QUOTA_REASONS:
            raise RetroError("blocked", "youtube_quota_exhausted") from error
        raise RetroError("error", "youtube_api_failed") from error
    except (OSError, urllib.error.URLError) as error:
        raise RetroError("error", "youtube_api_failed") from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RetroError("error", "youtube_api_failed") from error
    if not isinstance(payload, dict):
        raise RetroError("error", "youtube_api_failed")
    return payload, raw


def metrics_from(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    headers = payload.get("columnHeaders")
    rows = payload.get("rows", [])
    if (
        payload.get("kind") != "youtubeAnalytics#resultTable"
        or not isinstance(headers, list)
        or not isinstance(rows, list)
        or len(rows) > 1
    ):
        raise RetroError("error", "youtube_api_failed")
    if len(headers) != len(METRICS) or any(
        not isinstance(header, dict)
        or header.get("name") != name
        or header.get("columnType") != "METRIC"
        or header.get("dataType") not in {"INTEGER", "FLOAT"}
        for header, name in zip(headers, METRICS)
    ):
        raise RetroError("error", "youtube_api_failed")
    if not rows:
        return "no_rows", {name: None for name in METRICS}
    if not isinstance(rows[0], list) or len(rows[0]) != len(METRICS):
        raise RetroError("error", "youtube_api_failed")
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        for value in rows[0]
    ):
        raise RetroError("error", "youtube_api_failed")
    return "reported", dict(zip(METRICS, rows[0]))


def markdown(
    video_id: str,
    start_date: str,
    end_date: str,
    fetched_at: str,
    data_status: str,
    metrics: dict[str, Any],
) -> bytes:
    rows = "\n".join(
        f"| `{name}` | {json.dumps(value, ensure_ascii=False)} |"
        for name, value in metrics.items()
    )
    return (
        "---\n"
        "schema: haru.youtube_retro.v1\n"
        "format_version: 1\n"
        f"video_id: {video_id}\n"
        f"start_date: {start_date}\n"
        f"end_date: {end_date}\n"
        f"fetched_at: {fetched_at}\n"
        f"data_status: {data_status}\n"
        "---\n\n"
        f"# YouTube retro: {video_id}\n\n"
        "## Reported metrics\n\n"
        "| Metric | Value |\n|---|---:|\n"
        f"{rows}\n\n"
        "## Learning receipt\n\n"
        "- status: `captured_not_interpreted`\n"
        "- interpretation: pending human review; this client does not invent conclusions.\n"
    ).encode()


def fetch_retro(arguments: argparse.Namespace, repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    if not VIDEO_ID.fullmatch(arguments.video_id) or arguments.output_id != arguments.video_id:
        raise RetroError("error", "retro_input_invalid")
    start = parse_date(arguments.start_date)
    end = parse_date(arguments.end_date)
    if start > end:
        raise RetroError("error", "retro_input_invalid")
    try:
        retros_root = repo_path(repo, arguments.retros_root, {})
        policy_path = repo_path(repo, arguments.policy_file, {})
    except JobError as error:
        raise RetroError("error", "retro_input_invalid") from error
    if not policy_path.is_file() or policy_path.is_symlink():
        raise RetroError("error", "retro_input_invalid")
    policy = digest(policy_path)

    token = load_token(arguments.oauth_token_file, repo)
    payload, raw = query(token, arguments.video_id, arguments.start_date, arguments.end_date)
    data_status, values = metrics_from(payload)
    fetched_at = now()
    retro_path = retros_root / f"{arguments.video_id}.md"
    receipt_path = retros_root / f"{arguments.video_id}.receipt.json"
    retro = markdown(
        arguments.video_id,
        arguments.start_date,
        arguments.end_date,
        fetched_at,
        data_status,
        values,
    )
    atomic_write(retro_path, retro)
    retro_digest = digest(retro_path)
    receipt = {
        "format_version": 1,
        "schema": "haru.youtube_retro_receipt.v1",
        "status": "captured_not_interpreted",
        "video_id": arguments.video_id,
        "start_date": arguments.start_date,
        "end_date": arguments.end_date,
        "fetched_at": fetched_at,
        "data_status": data_status,
        "query": {
            "endpoint": ENDPOINT,
            "ids": "channel==MINE",
            "filters": f"video=={arguments.video_id}",
            "metrics": list(METRICS),
        },
        "policy": policy,
        "response_sha256": sha256(raw),
        "retro": retro_digest,
    }
    atomic_write(
        receipt_path,
        json.dumps(receipt, ensure_ascii=False, indent=2).encode() + b"\n",
    )
    if not retro_path.is_file() or not receipt_path.is_file():
        raise RetroError("error", "retro_write_failed")
    return {
        "format_version": 1,
        "status": "ok",
        "code": "retro_written",
        "video_id": arguments.video_id,
        "retro_path": str(retro_path),
        "receipt_path": str(receipt_path),
    }


def run_due_retro(arguments: argparse.Namespace, repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    if not RUN_ID.fullmatch(arguments.run_id):
        raise RetroError("error", "retro_input_invalid")
    as_of_date = parse_date(arguments.as_of_date)
    as_of = dt.datetime.combine(as_of_date, dt.time.min, tzinfo=dt.timezone.utc)
    try:
        retros_root = repo_path(repo, arguments.retros_root, {})
        run_receipts_root = repo_path(repo, arguments.run_receipts_root, {})
    except JobError as error:
        raise RetroError("error", "retro_input_invalid") from error

    receipt_path = run_receipts_root / f"{arguments.run_id}.json"
    receipt = None
    if receipt_path.is_symlink():
        raise RetroError("error", "retro_write_failed")
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RetroError("error", "retro_write_failed") from error
        if (
            not isinstance(receipt, dict)
            or receipt.get("format_version") != 1
            or receipt.get("schema") != "haru.youtube_due_retro_receipt.v1"
            or receipt.get("run_id") != arguments.run_id
            or receipt.get("as_of_date") != arguments.as_of_date
            or receipt.get("status") not in {"running", "ok"}
            or not isinstance(receipt.get("artifacts"), list)
        ):
            raise RetroError("error", "retro_write_failed")

    selected = receipt.get("selected") if receipt else None
    if receipt is None:
        entries, raw = feed(arguments.channel_id)
        selected = select_due(
            entries,
            retros_root,
            as_of,
            arguments.min_age_days,
            arguments.max_age_days,
        )
        receipt = {
            "format_version": 1,
            "schema": "haru.youtube_due_retro_receipt.v1",
            "status": "running" if selected else "ok",
            "run_id": arguments.run_id,
            "as_of_date": arguments.as_of_date,
            "feed_sha256": sha256(raw),
            "selected": (
                {
                    "video_id": selected["video_id"],
                    "title": selected["title"],
                    "published_at": selected["published_at"].isoformat(),
                }
                if selected
                else None
            ),
            "artifacts": [],
        }
        if selected is None:
            receipt["code"] = "no_pending_retros"
        atomic_write(
            receipt_path,
            json.dumps(receipt, ensure_ascii=False, indent=2).encode() + b"\n",
        )
        selected = receipt["selected"]

    if selected is None:
        if (
            receipt.get("code") != "no_pending_retros"
            or receipt.get("artifacts") != []
        ):
            raise RetroError("error", "retro_write_failed")
    else:
        if (
            not isinstance(selected, dict)
            or not VIDEO_ID.fullmatch(str(selected.get("video_id", "")))
            or not isinstance(selected.get("title"), str)
            or not selected["title"].strip()
            or not isinstance(selected.get("published_at"), str)
        ):
            raise RetroError("error", "retro_write_failed")
        video_id = selected["video_id"]
        published_at = parse_timestamp(selected["published_at"])
        completed = completed_retro_pair(retros_root, video_id)
        result = None
        if completed:
            retro_path, learning_path = completed
            result = {
                "code": "retro_written",
                "retro_path": str(retro_path),
                "receipt_path": str(learning_path),
            }
        if result is None:
            result = fetch_retro(
                argparse.Namespace(
                    video_id=video_id,
                    output_id=video_id,
                    start_date=published_at.date().isoformat(),
                    end_date=arguments.as_of_date,
                    oauth_token_file=arguments.oauth_token_file,
                    retros_root=arguments.retros_root,
                    policy_file=arguments.policy_file,
                ),
                repo,
            )
        receipt["status"] = "ok"
        receipt["code"] = result["code"]
        receipt["selected"] = {
            "video_id": video_id,
            "title": selected["title"],
            "published_at": published_at.isoformat(),
            "retro": digest(Path(result["retro_path"])),
            "learning_receipt": digest(Path(result["receipt_path"])),
        }
        receipt["artifacts"] = [
            receipt["selected"]["retro"],
            receipt["selected"]["learning_receipt"],
        ]

    atomic_write(
        receipt_path,
        json.dumps(receipt, ensure_ascii=False, indent=2).encode() + b"\n",
    )
    return {
        "format_version": 1,
        "status": "ok",
        "code": receipt["code"],
        "run_receipt_path": str(receipt_path),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch-retro")
    fetch.add_argument("--video-id", required=True)
    fetch.add_argument("--output-id", required=True)
    fetch.add_argument("--start-date", required=True)
    fetch.add_argument("--end-date", required=True)
    fetch.add_argument("--oauth-token-file", required=True)
    fetch.add_argument("--retros-root", default="retros")
    fetch.add_argument("--policy-file", default="jobs/prompts/retro.md")
    fetch.add_argument("--repo-root", type=Path)
    due = commands.add_parser("run-due-retro")
    due.add_argument("--channel-id", required=True)
    due.add_argument("--run-id", required=True)
    due.add_argument("--as-of-date", required=True)
    due.add_argument("--oauth-token-file", required=True)
    due.add_argument("--min-age-days", type=int, default=3)
    due.add_argument("--max-age-days", type=int, default=60)
    due.add_argument("--retros-root", default="retros")
    due.add_argument("--run-receipts-root", default=".hvp/retro-runs")
    due.add_argument("--policy-file", default="jobs/prompts/retro.md")
    due.add_argument("--repo-root", type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    repo = (arguments.repo_root or Path(__file__).resolve().parent.parent).resolve()
    try:
        result = (
            fetch_retro(arguments, repo)
            if arguments.command == "fetch-retro"
            else run_due_retro(arguments, repo)
        )
        exit_code = 0
    except RetroError as error:
        result = {
            "format_version": 1,
            "status": error.status,
            "code": error.code,
        }
        exit_code = 3 if error.status == "blocked" else 2
    except OSError:
        result = {
            "format_version": 1,
            "status": "error",
            "code": "retro_write_failed",
        }
        exit_code = 2
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

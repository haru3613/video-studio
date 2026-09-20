"""Explicit, fail-closed organization for the dashboard overview."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit


CATALOG_SCHEMA = "haru.dashboard_catalog.v1"
GROUP_TYPES = {"video", "library", "experiment", "unclassified"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
HERE = Path(__file__).resolve().parent
DEFAULT_CATALOG = HERE.parent.parent / "config" / "dashboard-catalog.json"


class CatalogError(ValueError):
    """The catalog is not safe to apply partially."""


def _key(source: str, name: str) -> tuple[str, str]:
    return source, name


def _valid_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\x00" not in value
        and "/" not in value
        and "\\" not in value
        and value not in {".", ".."}
    )


def _member(value: object, roots: dict[str, Path]) -> dict:
    if not isinstance(value, dict):
        raise CatalogError("member must be an object")
    source, name = value.get("source"), value.get("name")
    if not _valid_component(source) or not _valid_component(name):
        raise CatalogError("member has an invalid source or name")
    if source not in roots:
        raise CatalogError("member has an unknown source")
    label = value.get("label")
    if label is not None and not isinstance(label, str):
        raise CatalogError("member label must be a string")
    return {"source": source, "name": name, "label": label}


def _load_catalog(path: Path, roots: dict[str, Path]) -> list[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError("could not read catalog") from exc
    if not isinstance(raw, dict) or raw.get("schema") != CATALOG_SCHEMA:
        raise CatalogError("unexpected catalog schema")
    groups = raw.get("groups")
    if not isinstance(groups, list):
        raise CatalogError("catalog groups must be a list")

    ids: set[str] = set()
    claimed: set[tuple[str, str]] = set()
    normalized = []
    for group in groups:
        if not isinstance(group, dict):
            raise CatalogError("group must be an object")
        group_id = group.get("id")
        if not _valid_component(group_id) or group_id in ids:
            raise CatalogError("group ids must be unique safe components")
        ids.add(group_id)
        group_type = group.get("type")
        if not isinstance(group_type, str) or group_type not in GROUP_TYPES:
            raise CatalogError("group type is invalid")
        for field in ("title", "collection", "reason"):
            if not isinstance(group.get(field), str):
                raise CatalogError(f"group {field} must be a string")
        members_raw = group.get("members")
        if not isinstance(members_raw, list) or not members_raw:
            raise CatalogError("group must have members")
        members = [_member(item, roots) for item in members_raw]
        member_keys = [_key(item["source"], item["name"]) for item in members]
        if len(set(member_keys)) != len(member_keys) or claimed.intersection(member_keys):
            raise CatalogError("a source/name belongs to more than one group")
        claimed.update(member_keys)
        primary = _member(group.get("primary"), roots)
        primary_key = _key(primary["source"], primary["name"])
        if primary_key not in member_keys:
            raise CatalogError("primary must be a group member")
        cover = group.get("cover")
        if cover is not None and not isinstance(cover, str):
            raise CatalogError("cover must be a string or null")
        keywords = group.get("keywords", [])
        if not isinstance(keywords, list) or any(not isinstance(item, str) for item in keywords):
            raise CatalogError("keywords must be a list of strings")
        normalized.append(
            {
                "id": group_id,
                "title": group["title"],
                "type": group_type,
                "collection": group["collection"],
                "primary": primary,
                "members": members,
                "cover": cover,
                "reason": group["reason"],
                "keywords": keywords,
            }
        )
    return normalized


def _safe_project_url(source: str, name: str) -> str:
    return "/#/project/" + quote(source, safe="") + "/" + quote(name, safe="")


def _card(project: dict, label: str | None = None) -> dict:
    card = dict(project)
    source, name = card.get("source"), card.get("name")
    if isinstance(source, str) and isinstance(name, str):
        card["url"] = _safe_project_url(source, name)
    if label is not None:
        card["label"] = label
    return card


def _cover_url(value: str | None, roots: dict[str, Path]) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    raw_parts = parsed.path.split("/")
    if len(raw_parts) < 5 or raw_parts[0] or raw_parts[1] != "media":
        return None
    parts = [unquote(part) for part in raw_parts[2:]]
    source, project, *relative_parts = parts
    if source not in roots or not _valid_component(source) or not _valid_component(project):
        return None
    if not relative_parts or any(
        not _valid_component(part) or part.startswith(".") for part in relative_parts
    ):
        return None
    relative = PurePosixPath(*relative_parts)
    if relative.suffix.lower() not in IMAGE_EXTS:
        return None
    root = Path(roots[source])
    candidate = root
    try:
        root_resolved = root.resolve(strict=True)
        for part in (project, *relative.parts):
            candidate = candidate / part
            if candidate.is_symlink():
                return None
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError):
        return None
    if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
        return None
    try:
        if resolved.stat().st_size <= 0:
            return None
    except OSError:
        return None
    return "/media/" + quote(source, safe="") + "/" + quote(project, safe="") + "/" + "/".join(quote(part, safe="") for part in relative.parts)


def _unclassified(project: dict) -> dict:
    source, name = project.get("source"), project.get("name")
    return {
        "id": "unclassified-" + quote(str(source), safe="") + "-" + quote(str(name), safe=""),
        "title": str(name),
        "type": "unclassified",
        "collection": "待整理",
        "primary": _card(project),
        "members": [_card(project)],
        "cover": None,
        "reason": "尚未指定分類與歸屬。",
        "keywords": [],
    }


def build_overview(
    projects: list[dict], roots: dict[str, Path], catalog_path: Path | None = None
) -> dict:
    """Organize scanned project cards without changing the scan or filesystem."""
    warnings: list[str] = []
    discovered: dict[tuple[str, str], dict] = {}
    for project in projects:
        if not isinstance(project, dict):
            warnings.append("ignored malformed project card")
            continue
        source, name = project.get("source"), project.get("name")
        if not isinstance(source, str) or not isinstance(name, str):
            warnings.append("ignored project card without source/name")
            continue
        key = _key(source, name)
        if key in discovered:
            warnings.append(f"duplicate discovered project: {source}/{name}")
            continue
        discovered[key] = project

    try:
        if catalog_path is None and not DEFAULT_CATALOG.exists():
            catalog = []  # Optional operator grouping is absent in a fresh install.
        else:
            catalog = _load_catalog(Path(catalog_path) if catalog_path else DEFAULT_CATALOG, roots)
    except CatalogError as exc:
        warnings.append(f"dashboard catalog ignored: {exc}")
        groups = [_unclassified(project) for project in discovered.values()]
        return _result(groups, len(discovered), warnings)

    groups: list[dict] = []
    claimed: set[tuple[str, str]] = set()
    for catalog_group in catalog:
        available = []
        for member in catalog_group["members"]:
            key = _key(member["source"], member["name"])
            project = discovered.get(key)
            if project is None:
                warnings.append(f"catalog group {catalog_group['id']} member missing: {member['source']}/{member['name']}")
                continue
            claimed.add(key)
            available.append(_card(project, member["label"]))
        if not available:
            warnings.append(f"catalog group {catalog_group['id']} omitted: no members found")
            continue
        primary_key = _key(catalog_group["primary"]["source"], catalog_group["primary"]["name"])
        primary_project = discovered.get(primary_key)
        primary = _card(primary_project, catalog_group["primary"]["label"]) if primary_project else None
        if primary is None:
            warnings.append(f"catalog group {catalog_group['id']} primary missing: {primary_key[0]}/{primary_key[1]}")
        group = dict(catalog_group)
        group["members"] = available
        group["primary"] = primary
        group["cover"] = _cover_url(catalog_group["cover"], roots)
        if catalog_group["cover"] is not None and group["cover"] is None:
            warnings.append(f"catalog group {catalog_group['id']} cover unavailable")
        groups.append(group)

    groups.extend(_unclassified(project) for key, project in discovered.items() if key not in claimed)
    return _result(groups, len(discovered), warnings)


def _result(groups: list[dict], source_count: int, warnings: list[str]) -> dict:
    counts = {kind: 0 for kind in ("video", "library", "experiment", "unclassified")}
    for group in groups:
        counts[group["type"]] += 1
    return {"groups": groups, "counts": counts, "source_count": source_count, "warnings": warnings}

#!/usr/bin/env python3
"""Create an offline, digest-bound Taiwan Mandarin pronunciation plan."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
from pathlib import Path


SCHEMA = "haru.pronunciation_plan.v1"
OVERRIDES_SCHEMA = "haru.pronunciation_overrides.v1"


class PlanError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_overrides(path: Path | None) -> tuple[list[dict], str | None]:
    if path is None:
        return [], None
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError("invalid_pronunciation_overrides", str(exc)) from exc
    if data.get("schema") != OVERRIDES_SCHEMA or not isinstance(data.get("terms"), list):
        raise PlanError(
            "invalid_pronunciation_overrides",
            f"expected {OVERRIDES_SCHEMA} with a terms array",
        )
    for item in data["terms"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("term"), str)
            or not item["term"]
            or not isinstance(item.get("spoken"), str)
            or not item["spoken"]
            or len(item["term"]) != len(item["spoken"])
        ):
            raise PlanError(
                "invalid_pronunciation_overrides",
                "each term needs non-empty, same-length term and spoken strings",
            )
    return data["terms"], sha256_bytes(raw)


def context(text: str, start: int, end: int, radius: int = 12) -> str:
    return text[max(0, start - radius):min(len(text), end + radius)]


def model_identity(model_dir: Path, bert_model: Path, package_version: str) -> dict:
    version_path = model_dir / "version"
    onnx_path = model_dir / "g2pw.onnx"
    if not version_path.is_file() or not onnx_path.is_file() or not bert_model.is_dir():
        raise PlanError(
            "g2p_model_unavailable",
            "model-dir needs version and g2pw.onnx; bert-model must be a local directory",
        )
    try:
        return {
            "name": "g2pw",
            "package_version": package_version,
            "model_version": version_path.read_text(encoding="utf-8").strip(),
            "model_sha256": sha256_file(onnx_path),
            "bert_model": str(bert_model.resolve()),
        }
    except OSError as exc:
        raise PlanError("g2p_model_unavailable", str(exc)) from exc


def build_plan(
    text: str,
    source_bytes: bytes,
    converter,
    engine: dict,
    overrides: list[dict],
    overrides_sha256: str | None,
) -> dict:
    converted = converter(text)
    phonemes = converted[0] if converted and isinstance(converted[0], list) else converted
    if not isinstance(phonemes, list) or len(phonemes) != len(text):
        raise PlanError("g2p_invalid_result", "g2pW output does not align with input text")

    polyphonic = set(converter.chars)
    groups: dict[tuple[str, str | None], list[int]] = {}
    for index, char in enumerate(text):
        if char in polyphonic:
            groups.setdefault((char, phonemes[index]), []).append(index)
    review_items = []
    for (char, phoneme), indices in groups.items():
        first = indices[0]
        review_items.append(
            {
                "reason": "polyphone",
                "status": "needs_review",
                "term": char,
                "index": first,
                "end": first + 1,
                "indices": indices,
                "occurrences": len(indices),
                "context": context(text, first, first + 1),
                "bopomofo": phoneme,
            }
        )
    override_count = 0
    for override in overrides:
        start = 0
        while (index := text.find(override["term"], start)) >= 0:
            end = index + len(override["term"])
            candidate_text = text[:index] + override["spoken"] + text[end:]
            candidate_result = converter(candidate_text)
            candidate_phonemes = (
                candidate_result[0]
                if candidate_result and isinstance(candidate_result[0], list)
                else candidate_result
            )
            if not isinstance(candidate_phonemes, list) or len(candidate_phonemes) != len(text):
                raise PlanError("g2p_invalid_result", "override output does not align with input text")
            target_bopomofo = phonemes[index:end]
            spoken_bopomofo = candidate_phonemes[index:end]
            review_items.append(
                {
                    "reason": "project_override",
                    "status": "configured",
                    "term": override["term"],
                    "spoken": override["spoken"],
                    "target_bopomofo": target_bopomofo,
                    "spoken_bopomofo": spoken_bopomofo,
                    "g2p_match": target_bopomofo == spoken_bopomofo,
                    "index": index,
                    "end": end,
                    "context": context(text, index, end),
                    **({"note": override["reason"]} if override.get("reason") else {}),
                }
            )
            override_count += 1
            start = end
    review_items.sort(key=lambda item: (item["index"], item["reason"], item["term"]))

    return {
        "schema": SCHEMA,
        "source": {
            "sha256": sha256_bytes(source_bytes),
            "bytes": len(source_bytes),
            "characters": len(text),
        },
        "engine": engine,
        "overrides_sha256": overrides_sha256,
        "phonemes": [
            {"index": index, "text": char, "bopomofo": phoneme}
            for index, (char, phoneme) in enumerate(zip(text, phonemes))
        ],
        "review_items": review_items,
        "summary": {
            "polyphone_count": sum(item["reason"] == "polyphone" for item in review_items),
            "polyphone_occurrences": sum(
                item.get("occurrences", 0)
                for item in review_items
                if item["reason"] == "polyphone"
            ),
            "override_count": override_count,
            "review_required": bool(review_items),
        },
    }


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def run(args: argparse.Namespace) -> dict:
    text_path = Path(args.text_file)
    try:
        source_bytes = text_path.read_bytes()
        text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanError("invalid_narration", str(exc)) from exc
    if not text.strip():
        raise PlanError("invalid_narration", "narration is empty")

    model_dir = Path(args.model_dir) if args.model_dir else None
    bert_model = Path(args.bert_model) if args.bert_model else None
    if model_dir is None or bert_model is None:
        raise PlanError(
            "g2p_model_unavailable",
            "pass --model-dir and --bert-model or set "
            "VIDEO_STUDIO_G2PW_MODEL_DIR and VIDEO_STUDIO_G2PW_BERT_MODEL",
        )

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        import g2pw
        from g2pw import G2PWConverter
    except ImportError as exc:
        raise PlanError("g2p_runtime_unavailable", str(exc)) from exc

    try:
        runtime_version = importlib.metadata.version("g2pw")
    except importlib.metadata.PackageNotFoundError:
        runtime_version = getattr(g2pw, "__version__", "unknown")
    engine = model_identity(model_dir, bert_model, runtime_version)
    overrides, overrides_sha256 = load_overrides(Path(args.overrides) if args.overrides else None)
    try:
        converter = G2PWConverter(
            model_dir=str(model_dir),
            model_source=str(bert_model),
            style="bopomofo",
            num_workers=1,
            turnoff_tqdm=True,
            enable_non_tradional_chinese=True,
        )
        plan = build_plan(text, source_bytes, converter, engine, overrides, overrides_sha256)
    except PlanError:
        raise
    except Exception as exc:
        raise PlanError("g2p_execution_failed", str(exc)) from exc

    out = Path(args.out)
    atomic_write_json(out, plan)
    return {
        "schema_version": 1,
        "outcome": "ok",
        "code": "pronunciation_plan_created",
        "data": {"path": str(out), "sha256": sha256_bytes(out.read_bytes())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text-file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--overrides")
    parser.add_argument(
        "--model-dir",
        default=(
            os.environ.get("VIDEO_STUDIO_G2PW_MODEL_DIR")
            or os.environ.get("HARU_G2PW_MODEL_DIR")
        ),
    )
    parser.add_argument(
        "--bert-model",
        default=(
            os.environ.get("VIDEO_STUDIO_G2PW_BERT_MODEL")
            or os.environ.get("HARU_G2PW_BERT_MODEL")
        ),
    )
    args = parser.parse_args()
    try:
        result = run(args)
    except PlanError as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "outcome": "error",
                    "code": exc.code,
                    "data": {"message": str(exc)},
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

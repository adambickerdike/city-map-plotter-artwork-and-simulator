#!/usr/bin/env python3
"""Merge disjoint F1 acquisition manifests without weakening their byte locks.

This enables parallel event acquisition with cloned base manifests. Duplicate
source IDs must be byte-identical as manifest records; a disagreement is an
error, never a last-writer-wins choice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _record_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def merge_manifests(values: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not values:
        raise ValueError("At least one source manifest is required")
    first = values[0]
    identity = (
        first.get("schema_version"),
        first.get("contract_id"),
        first.get("season"),
        first.get("freeze"),
    )
    sources: dict[str, dict[str, Any]] = {}
    errors: dict[tuple[str, str], dict[str, Any]] = {}
    generated_values: list[str] = []
    event_document_flags: list[bool] = []
    for manifest in values:
        candidate = (
            manifest.get("schema_version"),
            manifest.get("contract_id"),
            manifest.get("season"),
            manifest.get("freeze"),
        )
        if candidate != identity:
            raise ValueError("F1 source manifests do not share one frozen identity")
        if manifest.get("network_fallback_for_rendering") is not False:
            raise ValueError("A source manifest permits live rendering fallback")
        if manifest.get("official_images_acquired") is not False:
            raise ValueError("A source manifest claims official images were acquired")
        event_document_flags.append(
            bool(manifest.get("official_event_documents_acquired", False))
        )
        generated_values.append(str(manifest.get("generated_at") or ""))
        for source_value in manifest.get("sources", []):
            if not isinstance(source_value, dict) or not source_value.get("id"):
                raise ValueError("A source manifest contains a malformed source record")
            source = dict(source_value)
            source_id = str(source["id"])
            previous = sources.get(source_id)
            if previous is not None and _record_bytes(previous) != _record_bytes(source):
                raise ValueError(
                    f"Source {source_id!r} differs between acquisition manifests"
                )
            sources[source_id] = source
        for error_value in manifest.get("acquisition_errors", []):
            if not isinstance(error_value, dict):
                raise ValueError("A source manifest contains a malformed error record")
            error = dict(error_value)
            key = (str(error.get("source_id") or ""), str(error.get("stage") or ""))
            errors[key] = error

    # A successful record supersedes an earlier failed attempt for the same
    # source identity, while failures for sources still absent remain visible.
    unresolved_errors = [
        error
        for (source_id, _), error in errors.items()
        if source_id not in sources
    ]
    return {
        "schema_version": identity[0],
        "contract_id": identity[1],
        "season": identity[2],
        "freeze": identity[3],
        "generated_at": max(generated_values),
        "network_fallback_for_rendering": False,
        "official_images_acquired": False,
        "official_event_documents_acquired": any(event_document_flags),
        "sources": sorted(sources.values(), key=lambda item: str(item["id"])),
        "acquisition_errors": sorted(
            unresolved_errors,
            key=lambda item: (str(item.get("source_id")), str(item.get("stage"))),
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("manifest", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Require the merged bytes to match an existing --output.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifests = [_load(path.resolve()) for path in args.manifest]
        result = merge_manifests(manifests)
        payload = _canonical(result)
        output = args.output.resolve()
        if args.check:
            if not output.is_file() or output.read_bytes() != payload:
                raise ValueError("Merged manifest does not match the existing output")
            print(f"deterministic manifest match: {hashlib.sha256(payload).hexdigest()}")
            return 0
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
        print(
            f"wrote {output}: {len(result['sources'])} sources, "
            f"{len(result['acquisition_errors'])} unresolved errors, "
            f"sha256={hashlib.sha256(payload).hexdigest()}"
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"merge-f1-source-manifests: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

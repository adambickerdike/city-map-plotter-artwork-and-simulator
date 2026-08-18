#!/usr/bin/env python3
"""Freeze the reviewed ranked-university OSM responses as a source contract.

The ranked-series renderer was already frozen, but a fresh clone previously
queried live Overpass data.  This maintainer tool converts one fully reviewed
v2.1.3 release into a subject-keyed, hash-bound set of saved JSON responses so
the public batch path can render the same acquired geometry without network
access.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RELEASE = (
    ROOT / "review-output/university-memorabilia-ranked-2026-v2.1.3"
)
DEFAULT_OUTPUT = (
    ROOT
    / "contracts/university-memorabilia-v2.1/source-snapshots"
)
EXPECTED_SUBJECT_COUNT = 50
CONTRACT_ID = "university-memorabilia-ranked-2026-osm-snapshots-v1"


class SourceContractError(RuntimeError):
    """Raised when reviewed source evidence cannot be frozen exactly."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                return json.load(stream)
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceContractError(f"Could not read JSON {path}: {exc}") from exc


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceContractError(f"{label} must be a JSON object.")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceContractError(f"{label} must be non-empty text.")
    return value.strip()


def _verbatim_text(value: object, label: str) -> str:
    """Validate text while retaining hash-significant whitespace."""

    if not isinstance(value, str) or not value.strip():
        raise SourceContractError(f"{label} must be non-empty text.")
    return value


def _release_manifest_path(release: Path, item: dict[str, Any]) -> Path:
    collection_id = _text(item.get("collection_id"), "batch collection_id")
    subject_id = _text(item.get("subject_id"), "batch subject_id")
    position = item.get("position")
    if not isinstance(position, int) or isinstance(position, bool) or position <= 0:
        raise SourceContractError(f"Invalid batch position for {subject_id!r}.")
    return release / collection_id / f"{position:03d}-{subject_id}.plot.json"


def _copy_exact(source: Path, destination: Path) -> None:
    if destination.exists():
        if not destination.is_file() or _sha256(destination) != _sha256(source):
            raise SourceContractError(
                f"Frozen source already exists with different bytes: {destination}"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_source_contract(release: Path, output: Path) -> dict[str, Any]:
    report_path = release / "ranked-universities.batch.json"
    report = _object(_read_json(report_path), "ranked university batch report")
    raw_items = report.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != EXPECTED_SUBJECT_COUNT:
        raise SourceContractError(
            "The reviewed batch must contain exactly "
            f"{EXPECTED_SUBJECT_COUNT} ranked-university items."
        )

    output.mkdir(parents=True, exist_ok=True)
    snapshot_dir = output / "overpass"
    snapshot_dir.mkdir(exist_ok=True)
    entries: list[dict[str, Any]] = []
    seen_subjects: set[str] = set()

    for raw_item in raw_items:
        item = _object(raw_item, "ranked university batch item")
        subject_id = _text(item.get("subject_id"), "batch subject_id")
        if subject_id in seen_subjects:
            raise SourceContractError(f"Duplicate subject in batch: {subject_id}")
        seen_subjects.add(subject_id)

        plot_manifest_path = _release_manifest_path(release, item)
        plot_manifest = _object(
            _read_json(plot_manifest_path), f"plot manifest for {subject_id}"
        )
        source = _object(plot_manifest.get("source"), f"source for {subject_id}")
        provenance = _object(
            source.get("provenance"), f"source provenance for {subject_id}"
        )
        original_cache = Path(
            _text(source.get("cache_path"), f"cache path for {subject_id}")
        )
        source_path = release / "source-cache/overpass" / original_cache.name
        if not source_path.is_file():
            raise SourceContractError(
                f"Reviewed source response is missing for {subject_id}: {source_path}"
            )
        source_sha256 = _sha256(source_path)
        recorded_source_sha256 = _text(
            provenance.get("source_file_sha256"),
            f"recorded source hash for {subject_id}",
        )
        if source_sha256 != recorded_source_sha256:
            raise SourceContractError(
                f"Source response hash differs from reviewed manifest for {subject_id}."
            )

        response = _object(_read_json(source_path), f"Overpass response for {subject_id}")
        canonical_sha256 = _stable_digest(response)
        recorded_canonical_sha256 = _text(
            provenance.get("canonical_source_data_sha256"),
            f"canonical source hash for {subject_id}",
        )
        if canonical_sha256 != recorded_canonical_sha256:
            raise SourceContractError(
                f"Canonical response hash differs for {subject_id}."
            )
        query = _verbatim_text(
            provenance.get("overpass_query"), f"Overpass query for {subject_id}"
        )
        query_sha256 = hashlib.sha256(query.encode("utf-8")).hexdigest()
        if query_sha256 != _text(
            provenance.get("overpass_query_sha256"),
            f"Overpass query hash for {subject_id}",
        ):
            raise SourceContractError(f"Overpass query hash differs for {subject_id}.")

        destination = snapshot_dir / f"{subject_id}.json.gz"
        _copy_exact(source_path, destination)
        osm3s = response.get("osm3s")
        osm3s_timestamp = (
            osm3s.get("timestamp_osm_base") if isinstance(osm3s, dict) else None
        )
        entries.append(
            {
                "subject_id": subject_id,
                "path": destination.relative_to(output).as_posix(),
                "size_bytes": destination.stat().st_size,
                "sha256": source_sha256,
                "canonical_json_sha256": canonical_sha256,
                "query_sha256": query_sha256,
                "osm_base_timestamp": _text(
                    osm3s_timestamp or source.get("timestamp"),
                    f"OSM base timestamp for {subject_id}",
                ),
                "extent_wgs84": _object(
                    plot_manifest.get("extent_wgs84"),
                    f"render extent for {subject_id}",
                ),
            }
        )

    unexpected = sorted(
        path.name
        for path in snapshot_dir.glob("*.json.gz")
        if path.stem.removesuffix(".json") not in seen_subjects
    )
    if unexpected:
        raise SourceContractError(
            "Source contract contains unreferenced response files: "
            + ", ".join(unexpected)
        )

    payload: dict[str, Any] = {
        "schema_version": 1,
        "id": CONTRACT_ID,
        "status": "review-only-pinned-source",
        "as_of": "2026-08-03",
        "subject_count": len(entries),
        "license": {
            "data": "Open Database License (ODbL) 1.0",
            "attribution": "© OpenStreetMap contributors",
            "copyright_url": "https://www.openstreetmap.org/copyright",
            "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        },
        "entries": entries,
    }
    manifest = {**payload, "cohort_sha256": _stable_digest(payload)}
    manifest_path = output / "source-manifest.json"
    _atomic_json(manifest_path, manifest)
    checksum_lines = [
        f"{entry['sha256']}  {entry['path']}" for entry in entries
    ]
    checksum_lines.append(
        f"{_sha256(manifest_path)}  {manifest_path.name}"
    )
    _atomic_text(output / "CHECKSUMS.sha256", "\n".join(checksum_lines) + "\n")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the exact 50 reviewed university Overpass responses into a "
            "subject-keyed, hash-bound source contract."
        )
    )
    parser.add_argument("--release-dir", type=Path, default=DEFAULT_RELEASE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_source_contract(
            args.release_dir.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
        )
        print(
            json.dumps(
                {
                    "manifest": str(
                        (args.output_dir / "source-manifest.json").resolve()
                    ),
                    "subject_count": manifest["subject_count"],
                    "cohort_sha256": manifest["cohort_sha256"],
                },
                indent=2,
            )
        )
        return 0
    except (OSError, SourceContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

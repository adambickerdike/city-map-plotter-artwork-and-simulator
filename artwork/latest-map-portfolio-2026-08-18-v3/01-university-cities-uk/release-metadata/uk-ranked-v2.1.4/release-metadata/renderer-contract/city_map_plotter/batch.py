"""Deterministic, resumable catalog collection export planning.

This module does not render maps itself.  It builds ordinary ``mapplot export``
argument vectors so batch operation inherits the exact same acquisition,
fidelity, pen-profile, validation, and manifest behavior as a single export.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from xml.etree import ElementTree as ET

from . import __version__
from .catalog import Catalog, CatalogCollection, CatalogEntry, CatalogSubject
from .geometry import (
    POSTER_PRESETS,
    Layout,
    crop_bbox_to_aspect,
    expand_bbox_to_aspect,
    load_plate_format,
    make_layout,
    make_poster_layout,
    poster_plate_format_id,
)
from .models import BoundingBox, MapPlotterError
from .osm import DEFAULT_OVERPASS_URL, load_overpass_file
from .pens import ACTUAL_PENS_PROFILE, load_pen_inventory, resolve_pen_inventory
from .styles import DEFAULT_FAMILIES, enabled_layer_ids, parse_families
from .themes import (
    SeriesTheme,
    expand_theme_export_args,
    resolve_subject_copy,
    resolve_theme_styles,
    resolved_theme_contract,
    theme_from_export_args,
)


BATCH_SCHEMA_VERSION = 3
ARTIFACT_CONTRACT_SCHEMA_VERSION = 3
SOURCE_COHORT_SCHEMA_VERSION = 1
SOURCE_COHORT_POLICY_ID = "city-map-source-cohort-v1"
GENERATOR_ID = f"city-map-plotter {__version__}"
OSM_ATTRIBUTION = (
    "Map data © OpenStreetMap contributors — https://www.openstreetmap.org/copyright"
)

# A collection owns subject, extent, title, and destination identity.  Allowing
# these options through the shared export tail would make filenames ambiguous,
# reuse one JSON crop for unrelated cities, or remove the mandatory marathon
# basemap disclosure.
RESERVED_EXPORT_OPTIONS = frozenset(
    {
        "--bbox",
        "--center",
        "--place",
        "--country-code",
        "--subject",
        "--catalog-file",
        "--input-json",
        "--output",
        "-o",
        "--manifest",
        "--title",
        "--subtitle",
        "--detail",
        "--help",
        "-h",
    }
)


def _option_name(token: str) -> str:
    if token.startswith("-o") and not token.startswith("--"):
        return "-o"
    return token.split("=", maxsplit=1)[0]


def normalise_export_args(values: Sequence[str]) -> tuple[str, ...]:
    """Validate the ordinary export options shared by every batch item."""

    result = list(values)
    if result and result[0] == "--":
        result.pop(0)
    if "--" in result:
        raise MapPlotterError(
            "--export-args accepts one ordinary export option tail; remove the "
            "additional '--' separator."
        )
    result = list(expand_theme_export_args(result))
    reserved = sorted(
        {
            _option_name(token)
            for token in result
            if token.startswith("-") and _option_name(token) in RESERVED_EXPORT_OPTIONS
        }
    )
    if reserved:
        raise MapPlotterError(
            "Collection export controls subject, extent, title, and output paths; "
            f"remove reserved export option(s): {', '.join(reserved)}."
        )
    return tuple(result)


def _dependency_fingerprint(option: str, path: Path) -> dict[str, Any]:
    """Hash one dependency while detecting a concurrent in-place mutation."""

    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise MapPlotterError(
            f"Could not fingerprint batch dependency {path}: {exc}"
        ) from exc
    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_signature != after_signature:
        raise MapPlotterError(
            f"Batch dependency {path} changed while it was being hashed. Retry "
            "with immutable input files."
        )
    return {
        "option": option,
        "path": str(path),
        "size_bytes": after.st_size,
        "sha256": digest.hexdigest(),
    }


def _export_dependency_fingerprints(argv: Sequence[object]) -> list[dict[str, Any]]:
    """Hash mutable external inputs by content, never pathname alone."""

    records: list[dict[str, Any]] = []
    for option in ("--style", "--pen-inventory", "--input-pbf", "--input-json"):
        value = _argv_value(argv, option)
        if value is None:
            continue
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise MapPlotterError(
                f"Batch dependency {option} path {path} is not a file."
            )
        records.append(_dependency_fingerprint(option, path))
    return records


def _dependency_fingerprints_are_valid(value: object) -> bool:
    if not isinstance(value, list):
        return False
    seen: set[str] = set()
    for record in value:
        if not isinstance(record, dict) or set(record) != {
            "option",
            "path",
            "size_bytes",
            "sha256",
        }:
            return False
        option = record.get("option")
        path = record.get("path")
        size = record.get("size_bytes")
        digest = record.get("sha256")
        if (
            option
            not in {"--style", "--pen-inventory", "--input-pbf", "--input-json"}
            or option in seen
            or not isinstance(path, str)
            or not path
            or not Path(path).is_absolute()
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return False
        seen.add(option)
    return True


def _verify_dependency_fingerprints(records: object) -> None:
    """Re-hash every planned dependency immediately before a render."""

    if not isinstance(records, list):
        raise MapPlotterError("Batch dependency fingerprints are malformed.")
    for record in records:
        if not isinstance(record, dict):
            raise MapPlotterError("Batch dependency fingerprint is malformed.")
        option = record.get("option")
        raw_path = record.get("path")
        if (
            option
            not in {"--style", "--pen-inventory", "--input-pbf", "--input-json"}
            or not isinstance(raw_path, str)
            or not raw_path
        ):
            raise MapPlotterError("Batch dependency fingerprint is malformed.")
        path = Path(raw_path)
        if not path.is_file():
            raise MapPlotterError(
                f"Planned batch dependency {option} is no longer a file: {path}."
            )
        current = _dependency_fingerprint(str(option), path)
        if current != record:
            raise MapPlotterError(
                f"Planned batch dependency {option} changed after the plan was "
                f"created: {path}. Build a new plan before rendering."
            )


def _has_option(values: Sequence[str], option: str) -> bool:
    return any(_option_name(value) == option for value in values)


def _stable_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _argv_value(argv: Sequence[object], option: str) -> str | None:
    """Return argparse's effective last value for a long option."""

    result: str | None = None
    for index, raw_token in enumerate(argv):
        token = str(raw_token)
        if token == option and index + 1 < len(argv):
            result = str(argv[index + 1])
        elif token.startswith(option + "="):
            result = token.split("=", maxsplit=1)[1]
    return result


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _manifest_extent(value: object, *, subject_id: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != {
        "west",
        "south",
        "east",
        "north",
    }:
        raise MapPlotterError(
            f"Pinned source entry {subject_id!r} extent_wgs84 must contain "
            "exactly west, south, east, and north."
        )
    if any(
        isinstance(value[key], bool) or not isinstance(value[key], (int, float))
        for key in ("west", "south", "east", "north")
    ):
        raise MapPlotterError(
            f"Pinned source entry {subject_id!r} extent_wgs84 values must be numbers."
        )
    try:
        bbox = BoundingBox(
            west=float(value["west"]),
            south=float(value["south"]),
            east=float(value["east"]),
            north=float(value["north"]),
        )
    except (TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Pinned source entry {subject_id!r} has an invalid extent_wgs84."
        ) from exc
    return bbox.as_dict()


def _load_pinned_json_source_manifest(
    path: Path,
    *,
    selected_subject_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load and fully verify a per-subject saved-Overpass source cohort."""

    resolved_manifest = path.expanduser().resolve()
    if not resolved_manifest.is_file():
        raise MapPlotterError(
            f"Pinned JSON source manifest {resolved_manifest} is not a file."
        )
    before = _dependency_fingerprint("--source-manifest", resolved_manifest)
    try:
        raw_manifest = resolved_manifest.read_bytes()
        value = json.loads(raw_manifest)
    except OSError as exc:
        raise MapPlotterError(
            f"Could not read pinned JSON source manifest {resolved_manifest}: {exc}"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MapPlotterError(
            f"Pinned JSON source manifest {resolved_manifest} is invalid JSON: {exc}"
        ) from exc
    after = _dependency_fingerprint("--source-manifest", resolved_manifest)
    if before != after or hashlib.sha256(raw_manifest).hexdigest() != before["sha256"]:
        raise MapPlotterError(
            f"Pinned JSON source manifest {resolved_manifest} changed while it "
            "was being validated. Retry with immutable input files."
        )
    if not isinstance(value, dict):
        raise MapPlotterError(
            f"Pinned JSON source manifest {resolved_manifest} must contain an object."
        )
    required_top_level = {
        "schema_version",
        "id",
        "license",
        "entries",
        "cohort_sha256",
    }
    missing_top_level = sorted(required_top_level - set(value))
    if missing_top_level:
        raise MapPlotterError(
            f"Pinned JSON source manifest {resolved_manifest} is missing: "
            f"{', '.join(missing_top_level)}."
        )
    if value.get("schema_version") != 1:
        raise MapPlotterError(
            "Pinned JSON source manifests must use schema_version 1."
        )
    manifest_id = value.get("id")
    if not isinstance(manifest_id, str) or not manifest_id.strip():
        raise MapPlotterError("Pinned JSON source manifest id must be non-empty.")
    license_record = value.get("license")
    if not isinstance(license_record, dict) or not license_record:
        raise MapPlotterError(
            "Pinned JSON source manifest license must be a non-empty object."
        )
    declared_cohort_sha256 = value.get("cohort_sha256")
    logical_payload = {
        key: item for key, item in value.items() if key != "cohort_sha256"
    }
    if (
        not _is_sha256(declared_cohort_sha256)
        or declared_cohort_sha256 != _stable_digest(logical_payload)
    ):
        raise MapPlotterError(
            "Pinned JSON source manifest cohort_sha256 does not match its "
            "canonical logical content."
        )
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list):
        raise MapPlotterError("Pinned JSON source manifest entries must be a list.")

    required_entry_fields = {
        "subject_id",
        "path",
        "size_bytes",
        "sha256",
        "canonical_json_sha256",
        "query_sha256",
        "osm_base_timestamp",
        "extent_wgs84",
    }
    manifest_root = resolved_manifest.parent
    selected = set(selected_subject_ids)
    entries_by_subject: dict[str, dict[str, Any]] = {}
    selected_records: dict[str, dict[str, Any]] = {}
    for position, raw_entry in enumerate(raw_entries, 1):
        if not isinstance(raw_entry, dict):
            raise MapPlotterError(
                f"Pinned JSON source manifest entry {position} must be an object."
            )
        missing_fields = sorted(required_entry_fields - set(raw_entry))
        if missing_fields:
            raise MapPlotterError(
                f"Pinned JSON source manifest entry {position} is missing: "
                f"{', '.join(missing_fields)}."
            )
        subject_id = raw_entry.get("subject_id")
        if not isinstance(subject_id, str) or not subject_id.strip():
            raise MapPlotterError(
                f"Pinned JSON source manifest entry {position} has no subject_id."
            )
        if subject_id in entries_by_subject:
            raise MapPlotterError(
                f"Pinned JSON source manifest has duplicate subject_id "
                f"{subject_id!r}."
            )
        declared_path = raw_entry.get("path")
        if not isinstance(declared_path, str) or not declared_path.strip():
            raise MapPlotterError(
                f"Pinned source entry {subject_id!r} path must be non-empty."
            )
        relative_path = Path(declared_path)
        if relative_path.is_absolute():
            raise MapPlotterError(
                f"Pinned source entry {subject_id!r} path must be relative to "
                "the source manifest."
            )
        resolved_path = (manifest_root / relative_path).resolve()
        if not resolved_path.is_relative_to(manifest_root):
            raise MapPlotterError(
                f"Pinned source entry {subject_id!r} path escapes the source "
                "manifest directory."
            )
        size_bytes = raw_entry.get("size_bytes")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            raise MapPlotterError(
                f"Pinned source entry {subject_id!r} size_bytes is invalid."
            )
        for field in ("sha256", "canonical_json_sha256", "query_sha256"):
            if not _is_sha256(raw_entry.get(field)):
                raise MapPlotterError(
                    f"Pinned source entry {subject_id!r} {field} must be a "
                    "lowercase SHA-256 digest."
                )
        timestamp = raw_entry.get("osm_base_timestamp")
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise MapPlotterError(
                f"Pinned source entry {subject_id!r} osm_base_timestamp must be "
                "non-empty."
            )
        extent = _manifest_extent(
            raw_entry.get("extent_wgs84"), subject_id=subject_id
        )
        normalized = {
            "subject_id": subject_id,
            "manifest_path": declared_path,
            "path": str(resolved_path),
            "size_bytes": size_bytes,
            "sha256": raw_entry["sha256"],
            "canonical_json_sha256": raw_entry["canonical_json_sha256"],
            "query_sha256": raw_entry["query_sha256"],
            "osm_base_timestamp": timestamp,
            "extent_wgs84": extent,
            "record_sha256": _stable_digest(raw_entry),
        }
        entries_by_subject[subject_id] = normalized
        if subject_id not in selected:
            continue

        actual = _dependency_fingerprint("--input-json", resolved_path)
        if (
            actual["size_bytes"] != size_bytes
            or actual["sha256"] != raw_entry["sha256"]
        ):
            raise MapPlotterError(
                f"Pinned source snapshot for {subject_id!r} does not match its "
                "declared size_bytes and sha256."
            )
        acquisition = load_overpass_file(resolved_path)
        current = _dependency_fingerprint("--input-json", resolved_path)
        if current != actual:
            raise MapPlotterError(
                f"Pinned source snapshot for {subject_id!r} changed while it "
                "was being validated. Retry with immutable input files."
            )
        canonical = json.dumps(
            acquisition.data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != raw_entry["canonical_json_sha256"]:
            raise MapPlotterError(
                f"Pinned source snapshot for {subject_id!r} does not match "
                "canonical_json_sha256."
            )
        osm3s = acquisition.data.get("osm3s")
        actual_timestamp = (
            osm3s.get("timestamp_osm_base") if isinstance(osm3s, dict) else None
        )
        if actual_timestamp != timestamp:
            raise MapPlotterError(
                f"Pinned source snapshot for {subject_id!r} has OSM base "
                f"timestamp {actual_timestamp!r}, expected {timestamp!r}."
            )
        embedded = acquisition.data.get("mapplot_acquisition")
        if isinstance(embedded, dict):
            embedded_query_sha256 = embedded.get("overpass_query_sha256")
            if (
                embedded_query_sha256 is not None
                and embedded_query_sha256 != raw_entry["query_sha256"]
            ):
                raise MapPlotterError(
                    f"Pinned source snapshot for {subject_id!r} does not match "
                    "query_sha256."
                )
        selected_records[subject_id] = normalized

    missing_subjects = [
        subject_id
        for subject_id in dict.fromkeys(selected_subject_ids)
        if subject_id not in selected_records
    ]
    if missing_subjects:
        raise MapPlotterError(
            "Pinned JSON source manifest has no verified entry for selected "
            f"subject(s): {', '.join(missing_subjects)}."
        )
    ordered_records = [
        selected_records[subject_id]
        for subject_id in dict.fromkeys(selected_subject_ids)
    ]
    json_set = {
        "manifest": {
            "path": str(resolved_manifest),
            "size_bytes": before["size_bytes"],
            "sha256": before["sha256"],
            "schema_version": 1,
            "id": manifest_id,
            "cohort_sha256": declared_cohort_sha256,
        },
        "license": license_record,
        "entries": ordered_records,
    }
    payload: dict[str, Any] = {
        "schema_version": SOURCE_COHORT_SCHEMA_VERSION,
        "policy_id": SOURCE_COHORT_POLICY_ID,
        "mode": "pinned-input-json-set",
        "pinned": True,
        "production_eligible": False,
        "cohort_id": f"osm-json-set-sha256:{declared_cohort_sha256}",
        "json_set": json_set,
        "reason": (
            "Every selected subject is bound to exact saved Overpass JSON bytes, "
            "but saved JSON cohorts remain review-only under the production "
            "source policy."
        ),
    }
    cohort = {**payload, "sha256": _stable_digest(payload)}
    return cohort, selected_records


def _json_set_entry(
    source_cohort: dict[str, Any], subject_id: object
) -> dict[str, Any] | None:
    json_set = source_cohort.get("json_set")
    if not isinstance(json_set, dict) or not isinstance(subject_id, str):
        return None
    entries = json_set.get("entries")
    if not isinstance(entries, list):
        return None
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("subject_id") == subject_id
    ]
    return matches[0] if len(matches) == 1 else None


def _source_cohort(
    argv: Sequence[object],
    dependency_fingerprints: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Describe whether all items share pinned OSM bytes or a live endpoint."""

    json_records = [
        record
        for record in dependency_fingerprints
        if record.get("option") == "--input-json"
    ]
    if json_records:
        raise MapPlotterError(
            "Per-subject --input-json sources must be bound through the batch "
            "--source-manifest option."
        )
    pbf_records = [
        record
        for record in dependency_fingerprints
        if record.get("option") == "--input-pbf"
    ]
    if len(pbf_records) > 1:
        raise MapPlotterError("A batch plan may bind at most one --input-pbf source.")
    if pbf_records:
        record = pbf_records[0]
        content_sha256 = str(record["sha256"])
        payload: dict[str, Any] = {
            "schema_version": SOURCE_COHORT_SCHEMA_VERSION,
            "policy_id": SOURCE_COHORT_POLICY_ID,
            "mode": "pinned-input-pbf",
            "pinned": True,
            "production_eligible": True,
            "cohort_id": f"osm-pbf-sha256:{content_sha256}",
            "pbf": {
                "path": record["path"],
                "size_bytes": record["size_bytes"],
                "content_sha256": content_sha256,
            },
        }
    else:
        payload = {
            "schema_version": SOURCE_COHORT_SCHEMA_VERSION,
            "policy_id": SOURCE_COHORT_POLICY_ID,
            "mode": "live-overpass-unpinned",
            "pinned": False,
            "production_eligible": False,
            "cohort_id": None,
            "overpass": {
                "endpoint": _argv_value(argv, "--overpass-url")
                or DEFAULT_OVERPASS_URL,
            },
            "reason": (
                "The live Overpass response has no immutable source snapshot "
                "fingerprint at planning time."
            ),
        }
    return {**payload, "sha256": _stable_digest(payload)}


def source_cohort_is_valid(value: object) -> bool:
    """Return whether a source-cohort record is canonical and self-authenticating."""

    if not isinstance(value, dict):
        return False
    digest = value.get("sha256")
    payload = {key: item for key, item in value.items() if key != "sha256"}
    if (
        value.get("schema_version") != SOURCE_COHORT_SCHEMA_VERSION
        or value.get("policy_id") != SOURCE_COHORT_POLICY_ID
        or not isinstance(digest, str)
        or digest != _stable_digest(payload)
    ):
        return False
    mode = value.get("mode")
    if mode == "pinned-input-pbf":
        pbf = value.get("pbf")
        if set(value) != {
            "schema_version",
            "policy_id",
            "mode",
            "pinned",
            "production_eligible",
            "cohort_id",
            "pbf",
            "sha256",
        } or not isinstance(pbf, dict) or set(pbf) != {
            "path",
            "size_bytes",
            "content_sha256",
        }:
            return False
        content_sha256 = pbf.get("content_sha256")
        return (
            value.get("pinned") is True
            and value.get("production_eligible") is True
            and value.get("cohort_id") == f"osm-pbf-sha256:{content_sha256}"
            and isinstance(pbf.get("path"), str)
            and bool(pbf["path"])
            and isinstance(pbf.get("size_bytes"), int)
            and not isinstance(pbf.get("size_bytes"), bool)
            and pbf["size_bytes"] >= 0
            and isinstance(content_sha256, str)
            and len(content_sha256) == 64
            and all(character in "0123456789abcdef" for character in content_sha256)
        )
    if mode == "pinned-input-json-set":
        if set(value) != {
            "schema_version",
            "policy_id",
            "mode",
            "pinned",
            "production_eligible",
            "cohort_id",
            "json_set",
            "reason",
            "sha256",
        }:
            return False
        json_set = value.get("json_set")
        if not isinstance(json_set, dict) or set(json_set) != {
            "manifest",
            "license",
            "entries",
        }:
            return False
        manifest = json_set.get("manifest")
        license_record = json_set.get("license")
        entries = json_set.get("entries")
        if (
            not isinstance(manifest, dict)
            or set(manifest)
            != {
                "path",
                "size_bytes",
                "sha256",
                "schema_version",
                "id",
                "cohort_sha256",
            }
            or not isinstance(license_record, dict)
            or not license_record
            or not isinstance(entries, list)
            or not entries
        ):
            return False
        manifest_path = manifest.get("path")
        manifest_size = manifest.get("size_bytes")
        manifest_id = manifest.get("id")
        manifest_cohort_sha256 = manifest.get("cohort_sha256")
        if (
            not isinstance(manifest_path, str)
            or not manifest_path
            or not Path(manifest_path).is_absolute()
            or not isinstance(manifest_size, int)
            or isinstance(manifest_size, bool)
            or manifest_size < 0
            or not _is_sha256(manifest.get("sha256"))
            or manifest.get("schema_version") != 1
            or not isinstance(manifest_id, str)
            or not manifest_id.strip()
            or not _is_sha256(manifest_cohort_sha256)
            or value.get("cohort_id")
            != f"osm-json-set-sha256:{manifest_cohort_sha256}"
        ):
            return False
        seen_subjects: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "subject_id",
                "manifest_path",
                "path",
                "size_bytes",
                "sha256",
                "canonical_json_sha256",
                "query_sha256",
                "osm_base_timestamp",
                "extent_wgs84",
                "record_sha256",
            }:
                return False
            subject_id = entry.get("subject_id")
            entry_path = entry.get("path")
            declared_path = entry.get("manifest_path")
            entry_size = entry.get("size_bytes")
            timestamp = entry.get("osm_base_timestamp")
            if (
                not isinstance(subject_id, str)
                or not subject_id
                or subject_id in seen_subjects
                or not isinstance(entry_path, str)
                or not entry_path
                or not Path(entry_path).is_absolute()
                or not isinstance(declared_path, str)
                or not declared_path
                or Path(declared_path).is_absolute()
                or not isinstance(entry_size, int)
                or isinstance(entry_size, bool)
                or entry_size < 0
                or not all(
                    _is_sha256(entry.get(field))
                    for field in (
                        "sha256",
                        "canonical_json_sha256",
                        "query_sha256",
                        "record_sha256",
                    )
                )
                or not isinstance(timestamp, str)
                or not timestamp.strip()
            ):
                return False
            try:
                _manifest_extent(entry.get("extent_wgs84"), subject_id=subject_id)
            except MapPlotterError:
                return False
            seen_subjects.add(subject_id)
        return (
            value.get("pinned") is True
            and value.get("production_eligible") is False
            and isinstance(value.get("reason"), str)
            and bool(str(value["reason"]).strip())
        )
    if mode == "live-overpass-unpinned":
        overpass = value.get("overpass")
        return (
            set(value)
            == {
                "schema_version",
                "policy_id",
                "mode",
                "pinned",
                "production_eligible",
                "cohort_id",
                "overpass",
                "reason",
                "sha256",
            }
            and value.get("pinned") is False
            and value.get("production_eligible") is False
            and value.get("cohort_id") is None
            and isinstance(overpass, dict)
            and set(overpass) == {"endpoint"}
            and isinstance(overpass.get("endpoint"), str)
            and bool(overpass["endpoint"].strip())
        )
    return False


def _argv_boolean(
    argv: Sequence[object],
    *,
    enabled: Sequence[str],
    disabled: Sequence[str],
    default: bool,
) -> bool:
    """Resolve the last BooleanOptionalAction spelling in an argument vector."""

    result = default
    enabled_names = set(enabled)
    disabled_names = set(disabled)
    for raw_token in argv:
        name = _option_name(str(raw_token))
        if name in enabled_names:
            result = True
        elif name in disabled_names:
            result = False
    return result


def _argv_float(
    argv: Sequence[object], option: str, *, default: float | None
) -> float | None:
    value = _argv_value(argv, option)
    if value is None:
        return default
    try:
        result = float(value)
    except ValueError as exc:
        raise MapPlotterError(f"{option} must be a number.") from exc
    if not math.isfinite(result):
        raise MapPlotterError(f"{option} must be finite.")
    return result


@lru_cache(maxsize=1)
def renderer_format_fingerprint() -> dict[str, Any]:
    """Fingerprint installed renderer source and the binding format resource.

    A version string alone cannot protect resume after an uncommitted renderer
    or format-spec change.  Hashing the installed Python source tree and the
    packaged format contract makes that change part of the immutable plan ID.
    """

    package_dir = Path(__file__).resolve().parent
    source_digest = hashlib.sha256()
    source_count = 0
    try:
        for source_path in sorted(package_dir.rglob("*.py")):
            relative = source_path.relative_to(package_dir).as_posix()
            source_digest.update(relative.encode("utf-8"))
            source_digest.update(b"\0")
            source_digest.update(source_path.read_bytes())
            source_digest.update(b"\0")
            source_count += 1
        format_path = package_dir / "data" / "format-v1.json"
        format_payload = format_path.read_bytes()
        format_document = json.loads(format_payload)
        theme_path = package_dir / "data" / "themes-v1.json"
        theme_payload = theme_path.read_bytes()
        theme_document = json.loads(theme_payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise MapPlotterError(
            f"Could not fingerprint renderer source and format contract: {exc}"
        ) from exc
    if source_count == 0:
        raise MapPlotterError("Could not fingerprint renderer source: no Python files.")
    if not isinstance(format_document, dict):
        raise MapPlotterError("Packaged format contract must contain a JSON object.")
    if not isinstance(theme_document, dict):
        raise MapPlotterError("Packaged theme contract must contain a JSON object.")
    fingerprint = {
        "generator": GENERATOR_ID,
        "source_file_count": source_count,
        "source_tree_sha256": source_digest.hexdigest(),
        "format_resource": "city_map_plotter/data/format-v1.json",
        "format_id": format_document.get("id"),
        "format_sha256": hashlib.sha256(format_payload).hexdigest(),
        "theme_resource": "city_map_plotter/data/themes-v1.json",
        "theme_catalog_id": theme_document.get("id"),
        "theme_catalog_sha256": hashlib.sha256(theme_payload).hexdigest(),
    }
    return {**fingerprint, "sha256": _stable_digest(fingerprint)}


def _planned_poster_format_id(preset: str, argv: Sequence[object]) -> str | None:
    """Mirror the export CLI's plate selection for a planned batch entry."""

    if preset not in POSTER_PRESETS:
        return None
    orientation = _argv_value(argv, "--orientation")
    format_id = poster_plate_format_id(preset, orientation=orientation)
    if format_id is None:
        raise MapPlotterError(f"Poster preset {preset!r} has no binding plate format.")
    return format_id


def _planned_bbox(
    subject: CatalogSubject,
    argv: Sequence[object],
    *,
    preset: str,
    extent_fit: str,
    theme_format_id: str | None = None,
) -> BoundingBox:
    radius_km = _argv_float(argv, "--radius-km", default=subject.preview_radius_km)
    assert radius_km is not None
    bbox = BoundingBox.around(subject.latitude, subject.longitude, radius_km)
    preset_format_id = _planned_poster_format_id(preset, argv)
    if preset_format_id is not None:
        if theme_format_id is not None and theme_format_id != preset_format_id:
            raise MapPlotterError(
                f"Theme plate format {theme_format_id!r} is incompatible with "
                f"preset {preset!r}, which is bound to {preset_format_id!r}."
            )
        map_field_aspect = float(
            load_plate_format(preset_format_id)["map_field_aspect"]
        )
        return (
            expand_bbox_to_aspect(bbox, map_field_aspect)
            if extent_fit == "contain"
            else crop_bbox_to_aspect(bbox, map_field_aspect)
        )
    return bbox


def _planned_layout(
    bbox: BoundingBox,
    argv: Sequence[object],
    *,
    preset: str,
) -> Layout:
    poster_format_id = _planned_poster_format_id(preset, argv)
    if poster_format_id is not None:
        return make_poster_layout(bbox, preset=preset, format_id=poster_format_id)
    margin_mm = _argv_float(argv, "--margin-mm", default=10.0)
    assert margin_mm is not None
    return make_layout(
        bbox,
        paper_name=_argv_value(argv, "--paper") or "A4",
        orientation=_argv_value(argv, "--orientation") or "auto",
        margin_mm=margin_mm,
        width_mm=_argv_float(argv, "--width-mm", default=None),
        height_mm=_argv_float(argv, "--height-mm", default=None),
    )


def _purpose_label(subject: CatalogSubject) -> str:
    return {
        "campus": "UNIVERSITY CAMPUS",
        "student_city": "STUDENT CITY",
        "city_preview": "CITY BASEMAP PREVIEW",
    }[subject.map_purpose]


def _planned_title(
    subject: CatalogSubject,
    argv: Sequence[object],
    *,
    preset: str,
    theme: SeriesTheme | None = None,
    layout: Layout | None = None,
) -> str:
    if theme is not None:
        if layout is None:
            raise MapPlotterError("A themed title requires its resolved plate layout.")
        return resolve_subject_copy(theme, subject, layout).title
    explicit = _argv_value(argv, "--title")
    title = explicit or subject.name
    # The ordinary single-export path deliberately shortens implicit A5 place
    # names at the first comma.  The contract must model that exact behavior.
    if preset in POSTER_PRESETS and explicit is None:
        title = title.split(",", maxsplit=1)[0]
    return title


def _planned_design_contract(
    argv: Sequence[object],
) -> tuple[SeriesTheme | None, dict[str, Any] | None]:
    theme = theme_from_export_args(argv)
    if theme is None:
        return None, None
    families = parse_families(
        _argv_value(argv, "--layers") or ",".join(DEFAULT_FAMILIES)
    )
    styles = resolve_theme_styles(theme, enabled_layer_ids(families))
    inventory_path = _argv_value(argv, "--pen-inventory")
    inventory = (
        load_pen_inventory(Path(inventory_path).expanduser())
        if inventory_path is not None
        else resolve_pen_inventory(
            _argv_value(argv, "--pen-profile") or ACTUAL_PENS_PROFILE
        )
    )
    if inventory is None:
        raise MapPlotterError(
            f"Theme {theme.id!r} requires a concrete physical pen inventory."
        )
    return (
        theme,
        resolved_theme_contract(
            theme,
            styles=styles,
            inventory=inventory,
            stock_tone=_argv_value(argv, "--stock-tone") or "light",
        ),
    )


def _ranked_university_identity(entry: CatalogEntry) -> dict[str, Any] | None:
    """Return the exact ranked-cohort fields that the artifact digest must bind."""

    required = ("rank", "rank_number", "tied", "edition", "ranking_name")
    if not all(key in entry.attributes for key in required):
        return None
    identity = {key: entry.attributes[key] for key in required}
    if "score" in entry.attributes:
        identity["score"] = entry.attributes["score"]
    return identity


def _artifact_contract(
    *,
    collection: CatalogCollection,
    entry: CatalogEntry,
    subject: CatalogSubject,
    argv: Sequence[object],
    output: Path,
    manifest: Path,
    png: Path | None,
    png_dpi: float | None,
    renderer_fingerprint: dict[str, Any],
    theme: SeriesTheme | None,
    design_contract: dict[str, Any] | None,
    dependency_fingerprints: list[dict[str, Any]],
    source_cohort: dict[str, Any],
) -> dict[str, Any]:
    preset = _argv_value(argv, "--preset") or "standard"
    detail_profile = _argv_value(argv, "--detail-profile") or "faithful"
    poster_layout = _argv_value(argv, "--poster-layout")
    extent_fit = _argv_value(argv, "--extent-fit") or "contain"
    road_style = _argv_value(argv, "--road-style") or (
        "centreline" if detail_profile != "plot" else "multi"
    )
    simplify_mm = _argv_float(
        argv,
        "--simplify-mm",
        default=0.04 if detail_profile != "plot" else 0.08,
    )
    assert simplify_mm is not None
    families = parse_families(
        _argv_value(argv, "--layers") or ",".join(DEFAULT_FAMILIES)
    )
    attribution_mode = _argv_value(argv, "--attribution-mode") or "embedded"
    external_placement = _argv_value(argv, "--external-attribution-placement")
    if external_placement is not None:
        external_placement = external_placement.strip()
    scale_bar = _argv_boolean(
        argv,
        enabled=("--scale-bar",),
        disabled=("--no-scale-bar",),
        default=True,
    )
    scale_detail = _argv_boolean(
        argv,
        enabled=("--scale-detail",),
        disabled=("--no-scale-detail",),
        default=True,
    )
    optimise = _argv_boolean(
        argv,
        enabled=("--optimise", "--optimize"),
        disabled=("--no-optimise", "--no-optimize"),
        default=True,
    )
    production_requested = _argv_boolean(
        argv,
        enabled=("--production",),
        disabled=(),
        default=False,
    )
    bbox = _planned_bbox(
        subject,
        argv,
        preset=preset,
        extent_fit=extent_fit,
        theme_format_id=theme.format_id if theme is not None else None,
    )
    layout = _planned_layout(bbox, argv, preset=preset)
    title = _planned_title(
        subject,
        argv,
        preset=preset,
        theme=theme,
        layout=layout,
    )
    subject_copy = (
        resolve_subject_copy(theme, subject, layout) if theme is not None else None
    )
    detail_lines: list[str] = []
    purpose: str | None = None
    coordinate: str | None = None
    if subject_copy is not None:
        purpose = subject_copy.details[0] if subject_copy.details else None
        coordinate = next(
            (
                line
                for line in subject_copy.details
                if " / " in line and line[-1:] in {"E", "W"}
            ),
            None,
        )
        detail_lines.extend(subject_copy.details)
    elif preset in POSTER_PRESETS:
        latitude, longitude = layout.bbox.center
        latitude_label = f"{abs(latitude):.4f} {'N' if latitude >= 0 else 'S'}"
        longitude_label = f"{abs(longitude):.4f} {'E' if longitude >= 0 else 'W'}"
        purpose = _purpose_label(subject)
        coordinate = f"{latitude_label} / {longitude_label}"
        detail_lines.extend((purpose, coordinate))
        if scale_detail:
            detail_lines.append(f"APPROX SCALE 1:{round(layout.scale_denominator):d}")

    memorabilia = None
    if poster_layout == "university-memorabilia":
        personalisation = {
            "person_name": _argv_value(argv, "--person-name") or "",
            "degree": _argv_value(argv, "--degree") or "",
            "honours": _argv_value(argv, "--honours") or "",
            "years": _argv_value(argv, "--years") or "",
        }
        memorabilia = {
            "layout": "university-memorabilia",
            "coordinates": coordinate,
            "personalisation": personalisation,
            "blank_template": not any(personalisation.values()),
        }
        # This composition owns its coordinates and blank personalisation
        # zones; the legacy poster detail list is deliberately not drawn.
        detail_lines = []

    raster = None
    if png is not None:
        assert png_dpi is not None
        raster = {
            "format": "PNG",
            "path": str(png),
            "dpi": png_dpi,
            "width_px": round(layout.page.width_mm * png_dpi / 25.4),
            "height_px": round(layout.page.height_mm * png_dpi / 25.4),
            "background": "opaque white",
        }
    ranked_identity = _ranked_university_identity(entry)
    return {
        "schema_version": ARTIFACT_CONTRACT_SCHEMA_VERSION,
        "renderer_fingerprint_sha256": renderer_fingerprint["sha256"],
        "dependency_fingerprints": dependency_fingerprints,
        "source_cohort": source_cohort,
        "source_cohort_sha256": source_cohort["sha256"],
        "identity": {
            "collection_id": collection.id,
            "position": entry.position,
            "subject_id": subject.id,
            "subject_name": subject.name,
            "subject_kind": subject.kind,
            "map_purpose": subject.map_purpose,
            "title": title,
            **(
                {"visible_title": title, **ranked_identity}
                if ranked_identity is not None
                else {}
            ),
            **({"subtitle": subject_copy.subtitle} if subject_copy is not None else {}),
        },
        "artifacts": {
            "svg": str(output),
            "manifest": str(manifest),
            "png": str(png) if png is not None else None,
        },
        "extent_wgs84": bbox.as_dict(),
        "families": list(families),
        "rendering": {
            "preset": preset,
            **({"poster_layout": poster_layout} if poster_layout is not None else {}),
            "detail_profile": detail_profile,
            "road_style": road_style,
            "simplify_tolerance_mm": simplify_mm,
            "extent_fit": extent_fit,
            "travel_optimisation_enabled": optimise,
            "visible_attribution": attribution_mode == "embedded",
            "attribution_mode": attribution_mode,
            "external_attribution_placement": external_placement,
            "scale_bar": scale_bar,
            "scale_detail": scale_detail,
            "north_mark": True,
            "production_requested": production_requested,
        },
        "details": {
            "lines": detail_lines,
            "purpose": purpose,
            "coordinate": coordinate,
            **(
                {
                    "copy_policy_id": subject_copy.policy_id,
                    "copy_rule_id": subject_copy.rule_id,
                }
                if subject_copy is not None
                else {}
            ),
        },
        **({"memorabilia": memorabilia} if memorabilia is not None else {}),
        "page": {
            "paper": layout.page.name,
            "orientation": layout.page.orientation,
            "width_mm": layout.page.width_mm,
            "height_mm": layout.page.height_mm,
            "margin_mm": layout.margin_mm,
            "map_bounds_mm": {
                "x": round(layout.map_x_mm, 3),
                "y": round(layout.map_y_mm, 3),
                "width": round(layout.map_width_mm, 3),
                "height": round(layout.map_height_mm, 3),
            },
        },
        "raster": raster,
        "design_contract": design_contract,
    }


def _subject_filename(position: int, subject_id: str) -> str:
    return f"{position:03d}-{subject_id}.svg"


def build_batch_plan(
    catalog: Catalog,
    *,
    collection_ids: Sequence[str],
    output_dir: Path,
    catalog_file: Path | None,
    export_args: Sequence[str],
    source_manifest: Path | None = None,
    limit: int | None = None,
    title_mode: str = "subject",
    png_dpi: float | None = None,
) -> dict[str, Any]:
    """Build a stable plan of normal single-subject export invocations."""

    if not collection_ids:
        raise MapPlotterError("At least one catalog collection is required.")
    if title_mode not in {"subject", "city"}:
        raise MapPlotterError("Batch title mode must be subject or city.")
    if png_dpi is not None and (
        isinstance(png_dpi, bool) or not math.isfinite(png_dpi) or png_dpi <= 0
    ):
        raise MapPlotterError("PNG DPI must be a finite number greater than zero.")
    if limit is not None and (isinstance(limit, bool) or limit <= 0):
        raise MapPlotterError("--limit must be a positive integer.")
    normalized_args = normalise_export_args(export_args)
    renderer_fingerprint = renderer_format_fingerprint()
    dependency_fingerprints = _export_dependency_fingerprints(normalized_args)
    series_theme, design_contract = _planned_design_contract(normalized_args)
    if series_theme is not None and title_mode != "subject":
        raise MapPlotterError(
            f"Theme {series_theme.id!r} owns purpose-specific title rules; "
            "remove --title-mode city."
        )
    if series_theme is not None and png_dpi is not None:
        themed_png_dpi = float(series_theme.batch["recommended_png_dpi"])
        if abs(png_dpi - themed_png_dpi) > 1e-9:
            raise MapPlotterError(
                f"Theme {series_theme.id!r} fixes preview rasters at "
                f"{themed_png_dpi:g} DPI; received {png_dpi:g}."
            )
    effective_title_mode = "theme" if series_theme is not None else title_mode
    resolved_output = output_dir.expanduser().resolve()
    resolved_catalog = (
        catalog_file.expanduser().resolve() if catalog_file is not None else None
    )
    collections = [
        catalog.collection(collection_id) for collection_id in collection_ids
    ]
    planned_entries = [
        (collection, entry)
        for collection in collections
        for entry in collection.entries
    ]
    if limit is not None:
        planned_entries = planned_entries[:limit]
    planned_subject_ids = list(
        dict.fromkeys(entry.subject_id for _collection, entry in planned_entries)
    )
    resolved_source_manifest = (
        source_manifest.expanduser().resolve()
        if source_manifest is not None
        else None
    )
    source_records: dict[str, dict[str, Any]] = {}
    if resolved_source_manifest is not None:
        if _has_option(normalized_args, "--input-pbf"):
            raise MapPlotterError(
                "Batch --source-manifest cannot be combined with export "
                "--input-pbf; choose one pinned source mode."
            )
        source_cohort, source_records = _load_pinned_json_source_manifest(
            resolved_source_manifest,
            selected_subject_ids=planned_subject_ids,
        )
    else:
        source_cohort = _source_cohort(normalized_args, dependency_fingerprints)
    if _has_option(normalized_args, "--production") and not source_cohort.get(
        "production_eligible"
    ):
        raise MapPlotterError(
            "A production batch requires a pinned --input-pbf source cohort. "
            "Live Overpass and pinned JSON source sets are review-only under "
            "the production source policy."
        )
    selected_subject_ids = list(
        dict.fromkeys(
            entry.subject_id
            for collection in collections
            for entry in collection.entries
        )
    )
    selection_fingerprint = _stable_digest(
        {
            "catalog_version": catalog.version,
            "catalog_as_of": catalog.as_of,
            "collections": [asdict(collection) for collection in collections],
            "subjects": [
                asdict(catalog.subject(subject_id))
                for subject_id in selected_subject_ids
            ],
        }
    )
    items: list[dict[str, Any]] = []
    for collection, entry in planned_entries:
        collection_dir = resolved_output / collection.id
        subject = catalog.subject(entry.subject_id)
        output = collection_dir / _subject_filename(entry.position, subject.id)
        manifest = output.with_suffix(".plot.json")
        png = output.with_suffix(".png") if png_dpi is not None else None
        invocation = ["export", *normalized_args]
        item_dependencies = list(dependency_fingerprints)
        if resolved_catalog is not None:
            invocation.extend(("--catalog-file", str(resolved_catalog)))
        if resolved_source_manifest is not None:
            source_record = source_records[subject.id]
            invocation.extend(("--input-json", str(source_record["path"])))
            item_dependencies.append(
                {
                    "option": "--input-json",
                    "path": source_record["path"],
                    "size_bytes": source_record["size_bytes"],
                    "sha256": source_record["sha256"],
                }
            )
        invocation.extend(("--subject", subject.id, "--output", str(output)))

        visible_title = subject.name
        ranked_identity = _ranked_university_identity(entry)
        if title_mode == "city" and series_theme is None:
            # The reviewed university-memorabilia visual uses uppercase
            # locality names, while ordinary catalog city titles retain
            # their existing case.
            visible_title = (
                subject.city.upper() if ranked_identity is not None else subject.city
            )
            invocation.extend(("--title", visible_title))

        # Marathon catalog records currently have no imported/verified
        # course geometry.  Make the city extent explicit to satisfy the
        # single-export guard, and remove the event name from the artwork
        # title so the output cannot be mistaken for a course map.
        if subject.is_city_preview_only:
            if not _has_option(normalized_args, "--radius-km"):
                invocation.extend(("--radius-km", f"{subject.preview_radius_km:g}"))
            if title_mode != "city" and series_theme is None:
                visible_title = f"{subject.city} City Basemap"
                invocation.extend(("--title", visible_title))

        contract = _artifact_contract(
            collection=collection,
            entry=entry,
            subject=subject,
            argv=invocation,
            output=output,
            manifest=manifest,
            png=png,
            png_dpi=png_dpi,
            renderer_fingerprint=renderer_fingerprint,
            theme=series_theme,
            design_contract=design_contract,
            dependency_fingerprints=item_dependencies,
            source_cohort=source_cohort,
        )
        items.append(
            {
                "collection_id": collection.id,
                "position": entry.position,
                "subject_id": subject.id,
                "subject_name": subject.name,
                "subject_kind": subject.kind,
                "map_purpose": subject.map_purpose,
                **(
                    {"visible_title": visible_title, **ranked_identity}
                    if ranked_identity is not None
                    else {}
                ),
                "output": str(output),
                "manifest": str(manifest),
                **(
                    {"png": str(png), "png_dpi": png_dpi}
                    if png is not None
                    else {}
                ),
                "export_argv": invocation,
                "artifact_contract": contract,
                "artifact_contract_sha256": _stable_digest(contract),
                **(
                    {
                        "course_geometry_included": False,
                        "geometry_status": subject.details.get(
                            "geometry_status", "unverified"
                        ),
                        "route_source_url": subject.details.get("route_source_url"),
                        "product_disclosure": "CITY BASEMAP PREVIEW",
                    }
                    if subject.is_city_preview_only
                    else {}
                ),
            }
        )
    stable_plan = {
        "catalog_version": catalog.version,
        "catalog_as_of": catalog.as_of,
        "catalog_file": str(resolved_catalog) if resolved_catalog else None,
        "collection_ids": [collection.id for collection in collections],
        "collection_as_of": {
            collection.id: collection.as_of for collection in collections
        },
        "selection_sha256": selection_fingerprint,
        "renderer_fingerprint": renderer_fingerprint,
        "dependency_fingerprints": dependency_fingerprints,
        "source_cohort": source_cohort,
        "source_cohort_sha256": source_cohort["sha256"],
        "source_manifest": (
            str(resolved_source_manifest)
            if resolved_source_manifest is not None
            else None
        ),
        "series_theme": design_contract,
        "output_dir": str(resolved_output),
        "export_args": list(normalized_args),
        "title_mode": effective_title_mode,
        "png_dpi": png_dpi,
        "limit": limit,
        "items": items,
    }
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "plan_id": _stable_digest(stable_plan),
        "catalog": {
            "version": catalog.version,
            "as_of": catalog.as_of,
            "file": str(resolved_catalog) if resolved_catalog else "bundled",
            "subject_count": len(catalog.subjects),
            "selection_sha256": selection_fingerprint,
        },
        "renderer_fingerprint": renderer_fingerprint,
        "dependency_fingerprints": dependency_fingerprints,
        "source_cohort": source_cohort,
        "source_cohort_sha256": source_cohort["sha256"],
        "source_manifest": (
            str(resolved_source_manifest)
            if resolved_source_manifest is not None
            else None
        ),
        "series_theme": design_contract,
        "collections": [
            {
                "id": collection.id,
                "title": collection.title,
                "kind": collection.kind,
                "as_of": collection.as_of,
                "catalog_entry_count": len(collection.entries),
            }
            for collection in collections
        ],
        "output_dir": str(resolved_output),
        "export_args": list(normalized_args),
        "title_mode": effective_title_mode,
        "png_dpi": png_dpi,
        "limit": limit,
        "item_count": len(items),
        "marathon_city_basemap_count": sum(
            item["map_purpose"] == "city_preview" for item in items
        ),
        "course_geometry_policy": (
            "No marathon course is claimed or included. Catalog marathon items "
            "are explicitly titled and labelled as city basemap previews until "
            "official route geometry is imported and verified."
        ),
        "items": items,
    }


def default_report_path(plan: dict[str, Any]) -> Path:
    collection_ids = [item["id"] for item in plan["collections"]]
    name = (
        f"{collection_ids[0]}.batch.json"
        if len(collection_ids) == 1
        else "selected-collections.batch.json"
    )
    return Path(str(plan["output_dir"])) / name


def new_batch_report(plan: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        **{key: value for key, value in plan.items() if key != "items"},
        "created_at": now,
        "updated_at": now,
        "items": [
            {
                **item,
                "status": "pending",
                "attempts": 0,
            }
            for item in plan["items"]
        ],
        "summary": {
            "pending": len(plan["items"]),
            "running": 0,
            "completed": 0,
            "failed": 0,
        },
    }


def refresh_report_summary(report: dict[str, Any]) -> None:
    statuses = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    for item in report["items"]:
        status = str(item.get("status", "pending"))
        if status not in statuses:
            raise MapPlotterError(f"Batch report contains invalid status {status!r}.")
        statuses[status] += 1
    report["summary"] = statuses
    report["updated_at"] = datetime.now(UTC).isoformat()


def write_batch_report(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MapPlotterError(f"Could not write batch report {path}: {exc}") from exc


def load_batch_report(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(f"Could not read batch report {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(f"Batch report {path} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise MapPlotterError(f"Batch report {path} must contain a JSON object.")
    if value.get("schema_version") != BATCH_SCHEMA_VERSION:
        raise MapPlotterError(
            f"Batch report {path} has unsupported schema version "
            f"{value.get('schema_version')!r}."
        )
    if value.get("plan_id") != plan.get("plan_id"):
        raise MapPlotterError(
            f"Batch report {path} belongs to a different catalog selection, "
            "output directory, limit, or export option set. Use a different "
            "--report path, or use --no-resume --overwrite to start again."
        )
    if any(
        value.get(key) != expected for key, expected in plan.items() if key != "items"
    ):
        raise MapPlotterError(
            f"Batch report {path} has modified immutable plan metadata. Start "
            "from the original report or use --no-resume --overwrite."
        )
    expected_ids = [item["subject_id"] for item in plan["items"]]
    report_items = value.get("items")
    if (
        not isinstance(report_items, list)
        or [item.get("subject_id") for item in report_items if isinstance(item, dict)]
        != expected_ids
    ):
        raise MapPlotterError(
            f"Batch report {path} item order does not match its plan."
        )
    for expected, actual in zip(plan["items"], report_items, strict=True):
        if not isinstance(actual, dict) or any(
            actual.get(key) != expected_value
            for key, expected_value in expected.items()
        ):
            raise MapPlotterError(
                f"Batch report {path} has modified immutable plan data for "
                f"{expected['subject_id']}. Start from the original report or use "
                "--no-resume --overwrite to create a new one."
            )
    refresh_report_summary(value)
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MapPlotterError(f"Could not hash batch artifact {path}: {exc}") from exc
    return digest.hexdigest()


def _contract_for_item(item: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    contract = item.get("artifact_contract")
    digest = item.get("artifact_contract_sha256")
    if (
        not isinstance(contract, dict)
        or contract.get("schema_version") != ARTIFACT_CONTRACT_SCHEMA_VERSION
        or not isinstance(digest, str)
        or digest != _stable_digest(contract)
    ):
        return None
    identity = contract.get("identity")
    artifacts = contract.get("artifacts")
    source_cohort = contract.get("source_cohort")
    dependency_fingerprints = contract.get("dependency_fingerprints")
    if (
        not isinstance(identity, dict)
        or not isinstance(artifacts, dict)
        or not source_cohort_is_valid(source_cohort)
        or not isinstance(source_cohort, dict)
        or contract.get("source_cohort_sha256") != source_cohort.get("sha256")
        or not _dependency_fingerprints_are_valid(dependency_fingerprints)
    ):
        return None
    export_argv = item.get("export_argv")
    assert isinstance(dependency_fingerprints, list)
    if not isinstance(export_argv, list):
        return None
    if source_cohort.get("mode") == "pinned-input-json-set":
        entry = _json_set_entry(source_cohort, item.get("subject_id"))
        input_json = _argv_value(export_argv, "--input-json")
        if entry is None or input_json is None or _has_option(
            [str(value) for value in export_argv], "--input-pbf"
        ):
            return None
        try:
            input_path_matches = (
                Path(input_json).expanduser().resolve()
                == Path(str(entry["path"])).resolve()
            )
        except (KeyError, OSError, RuntimeError):
            return None
        input_records = [
            record
            for record in dependency_fingerprints
            if record.get("option") == "--input-json"
        ]
        if not input_path_matches or input_records != [
            {
                "option": "--input-json",
                "path": entry.get("path"),
                "size_bytes": entry.get("size_bytes"),
                "sha256": entry.get("sha256"),
            }
        ]:
            return None
    else:
        try:
            expected_source_cohort = _source_cohort(
                export_argv, dependency_fingerprints
            )
        except (KeyError, MapPlotterError, TypeError, ValueError):
            return None
        if expected_source_cohort != source_cohort:
            return None
    expected_identity = {
        "collection_id": item.get("collection_id"),
        "position": item.get("position"),
        "subject_id": item.get("subject_id"),
        "subject_name": item.get("subject_name"),
        "subject_kind": item.get("subject_kind"),
        "map_purpose": item.get("map_purpose"),
    }
    if item.get("ranking_name") is not None:
        expected_identity.update(
            {
                "title": item.get("visible_title"),
                "visible_title": item.get("visible_title"),
                "rank": item.get("rank"),
                "rank_number": item.get("rank_number"),
                "tied": item.get("tied"),
                "edition": item.get("edition"),
                "ranking_name": item.get("ranking_name"),
                **({"score": item.get("score")} if "score" in item else {}),
            }
        )
    if any(identity.get(key) != value for key, value in expected_identity.items()):
        return None
    if (
        artifacts.get("svg") != item.get("output")
        or artifacts.get("manifest") != item.get("manifest")
        or artifacts.get("png") != item.get("png")
    ):
        return None
    return contract, digest


def _batch_binding(contract: dict[str, Any], digest: str) -> dict[str, Any]:
    identity = contract["identity"]
    return {
        "schema_version": ARTIFACT_CONTRACT_SCHEMA_VERSION,
        "sha256": digest,
        "collection_id": identity["collection_id"],
        "position": identity["position"],
        "subject_id": identity["subject_id"],
        "source_cohort_sha256": contract["source_cohort"]["sha256"],
    }


def _assert_item_dependencies_current(item: dict[str, Any]) -> None:
    """Abort before rendering if any effective dependency left its cohort."""

    resolved = _contract_for_item(item)
    if resolved is None:
        raise MapPlotterError(
            f"Batch item {item.get('subject_id')!r} has an invalid artifact contract."
        )
    contract, _digest = resolved
    export_argv = item.get("export_argv")
    assert isinstance(export_argv, list)
    current = _export_dependency_fingerprints(export_argv)
    planned = contract["dependency_fingerprints"]
    if current != planned:
        raise MapPlotterError(
            f"A planned dependency for {item['subject_id']} changed after the batch "
            "plan was created. Build a new plan before rendering."
        )
    source_cohort = contract["source_cohort"]
    if source_cohort.get("mode") == "pinned-input-json-set":
        entry = _json_set_entry(source_cohort, item.get("subject_id"))
        json_set = source_cohort.get("json_set")
        manifest = json_set.get("manifest") if isinstance(json_set, dict) else None
        if entry is None or not isinstance(manifest, dict):
            raise MapPlotterError(
                f"The pinned JSON source cohort for {item['subject_id']} is invalid."
            )
        manifest_path = Path(str(manifest.get("path", "")))
        if not manifest_path.is_file():
            raise MapPlotterError(
                f"The pinned JSON source manifest for {item['subject_id']} is no "
                "longer a file. Build a new plan before rendering."
            )
        manifest_fingerprint = _dependency_fingerprint(
            "--source-manifest", manifest_path
        )
        if (
            manifest_fingerprint.get("size_bytes") != manifest.get("size_bytes")
            or manifest_fingerprint.get("sha256") != manifest.get("sha256")
        ):
            raise MapPlotterError(
                f"The pinned JSON source manifest for {item['subject_id']} changed "
                "after planning. Build a new plan before rendering."
            )
        return
    if _source_cohort(export_argv, current) != source_cohort:
        raise MapPlotterError(
            f"The source cohort for {item['subject_id']} changed after planning. "
            "Build a new plan before rendering."
        )


def _manifest_source_matches_cohort(
    source: object,
    source_cohort: object,
    *,
    subject_id: object = None,
) -> bool:
    if not isinstance(source, dict) or not source_cohort_is_valid(source_cohort):
        return False
    assert isinstance(source_cohort, dict)
    if source_cohort["mode"] == "live-overpass-unpinned":
        overpass = source_cohort.get("overpass")
        assert isinstance(overpass, dict)
        return source.get("endpoint") == overpass.get("endpoint")

    if source_cohort["mode"] == "pinned-input-json-set":
        entry = _json_set_entry(source_cohort, subject_id)
        provenance = source.get("provenance")
        if entry is None or not isinstance(provenance, dict):
            return False
        planned_path = Path(str(entry.get("path", ""))).resolve()
        try:
            cache_path = Path(str(source.get("cache_path", ""))).resolve()
        except (OSError, RuntimeError):
            return False
        return (
            source.get("endpoint") == f"file:{planned_path}"
            and cache_path == planned_path
            and source.get("from_cache") is True
            and source.get("timestamp") == entry.get("osm_base_timestamp")
            and provenance.get("acquisition_mode") == "pinned-json"
            and provenance.get("source_pinned") is True
            and provenance.get("source_file_sha256") == entry.get("sha256")
            and provenance.get("canonical_source_data_sha256")
            == entry.get("canonical_json_sha256")
        )

    pbf = source_cohort.get("pbf")
    provenance = source.get("provenance")
    if not isinstance(pbf, dict) or not isinstance(provenance, dict):
        return False
    planned_path = Path(str(pbf.get("path", ""))).resolve()
    try:
        cache_path = Path(str(source.get("cache_path", ""))).resolve()
        provenance_path = Path(str(provenance.get("source_path", ""))).resolve()
    except (OSError, RuntimeError):
        return False
    planned_sha256 = pbf.get("content_sha256")
    return (
        source.get("endpoint") == f"file:{planned_path}"
        and cache_path == planned_path
        and provenance_path == planned_path
        and provenance.get("format") == "osm.pbf"
        and provenance.get("acquisition_mode") == "pinned-pbf"
        and provenance.get("source_pinned") is True
        and provenance.get("size_bytes") == pbf.get("size_bytes")
        and provenance.get("content_sha256") == planned_sha256
        and provenance.get("source_file_sha256") == planned_sha256
    )


def _atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MapPlotterError(f"Could not bind batch artifact {path}: {exc}") from exc


def bind_artifact_contract(item: dict[str, Any]) -> None:
    """Embed the immutable item-contract digest in renderer artifacts.

    The batch owns this invisible provenance.  PNG rasterisation occurs before
    this binding in the CLI callback, so its source hash is refreshed after the
    metadata-only SVG change; the plotted/rasterised graphics are unchanged.
    """

    resolved = _contract_for_item(item)
    if resolved is None:
        raise MapPlotterError(
            f"Batch item {item.get('subject_id')!r} has an invalid artifact contract."
        )
    contract, digest = resolved
    svg_path = Path(str(item["output"]))
    manifest_path = Path(str(item["manifest"]))
    try:
        root = ET.parse(svg_path).getroot()
        svg_text = svg_path.read_text(encoding="utf-8")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError, json.JSONDecodeError) as exc:
        raise MapPlotterError(
            f"Could not read rendered artifacts for {item['subject_id']}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise MapPlotterError(
            f"Plot manifest for {item['subject_id']} must contain a JSON object."
        )
    source_cohort = contract["source_cohort"]
    if not _manifest_source_matches_cohort(
        manifest.get("source"),
        source_cohort,
        subject_id=item.get("subject_id"),
    ):
        raise MapPlotterError(
            f"Manifest source for {item['subject_id']} does not match the planned "
            "source cohort."
        )

    attribute = "data-batch-contract-sha256"
    existing_svg_digest = root.attrib.get(attribute)
    if existing_svg_digest not in {None, digest}:
        raise MapPlotterError(
            f"SVG for {item['subject_id']} is bound to another batch contract."
        )
    svg_changed = existing_svg_digest is None
    if svg_changed:
        root_start = svg_text.find("<svg")
        if root_start < 0:
            raise MapPlotterError(
                f"SVG for {item['subject_id']} has no unprefixed root element."
            )
        insert_at = root_start + len("<svg")
        svg_text = (
            svg_text[:insert_at] + f' {attribute}="{digest}"' + svg_text[insert_at:]
        )
        _atomic_write_text(svg_path, svg_text)

    expected_binding = _batch_binding(contract, digest)
    existing_manifest_binding = manifest.get("batch_artifact_contract")
    if (
        existing_manifest_binding is not None
        and existing_manifest_binding != expected_binding
    ):
        raise MapPlotterError(
            f"Manifest for {item['subject_id']} is bound to another batch contract."
        )
    existing_source_cohort = manifest.get("batch_source_cohort")
    if existing_source_cohort is not None and existing_source_cohort != source_cohort:
        raise MapPlotterError(
            f"Manifest for {item['subject_id']} is bound to another source cohort."
        )
    manifest["batch_artifact_contract"] = expected_binding
    manifest["batch_source_cohort"] = source_cohort
    raster_exports = manifest.get("raster_exports")
    if isinstance(raster_exports, list) and len(raster_exports) == 1:
        raster_record = raster_exports[0]
        if isinstance(raster_record, dict):
            raster_record["source_svg_sha256"] = file_sha256(svg_path)
    _atomic_write_text(
        manifest_path,
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
    )


def _mapping_has_values(actual: object, expected: dict[str, Any]) -> bool:
    return isinstance(actual, dict) and all(
        actual.get(key) == value for key, value in expected.items()
    )


def _svg_matches_contract(
    root: ET.Element,
    contract: dict[str, Any],
    digest: str,
) -> bool:
    namespace = "{http://www.w3.org/2000/svg}"
    identity = contract.get("identity")
    page = contract.get("page")
    rendering = contract.get("rendering")
    extent = contract.get("extent_wgs84")
    design_contract = contract.get("design_contract")
    if not all(
        isinstance(value, dict) for value in (identity, page, rendering, extent)
    ):
        return False
    assert isinstance(identity, dict)
    assert isinstance(page, dict)
    assert isinstance(rendering, dict)
    if root.attrib.get("data-batch-contract-sha256") != digest:
        return False
    if isinstance(design_contract, dict):
        if (
            root.attrib.get("data-series-theme") != design_contract.get("theme_id")
            or root.attrib.get("data-series-theme-sha256")
            != design_contract.get("theme_sha256")
            or root.attrib.get("data-edition-signature-sha256")
            != design_contract.get("edition_signature_sha256")
        ):
            return False
    elif design_contract is not None:
        return False
    try:
        width_mm = float(root.attrib["width"].removesuffix("mm"))
        height_mm = float(root.attrib["height"].removesuffix("mm"))
        view_box = [float(value) for value in root.attrib["viewBox"].split()]
    except (KeyError, ValueError):
        return False
    if (
        not root.attrib["width"].endswith("mm")
        or not root.attrib["height"].endswith("mm")
        or width_mm != page.get("width_mm")
        or height_mm != page.get("height_mm")
        or view_box != [0.0, 0.0, width_mm, height_mm]
    ):
        return False
    svg_title = root.find(f"{namespace}title")
    metadata = root.find(f"{namespace}metadata")
    if svg_title is None or svg_title.text != identity.get("title") or metadata is None:
        return False
    try:
        svg_metadata = json.loads(metadata.text or "")
    except json.JSONDecodeError:
        return False
    if not _mapping_has_values(
        svg_metadata,
        {
            "preset": rendering.get("preset"),
            "detail_profile": rendering.get("detail_profile"),
            "extent_wgs84": extent,
            "attribution": OSM_ATTRIBUTION,
            **(
                {
                    "theme_id": design_contract.get("theme_id"),
                    "theme_sha256": design_contract.get("theme_sha256"),
                    "edition_signature_sha256": design_contract.get(
                        "edition_signature_sha256"
                    ),
                }
                if isinstance(design_contract, dict)
                else {}
            ),
        },
    ):
        return False
    groups = {
        element.attrib.get("id")
        for element in root.iter()
        if element.tag.endswith("}g") or element.tag == "g"
    }
    has_attribution = "layer-attribution" in groups
    has_scale_bar = any(
        "data-scale-distance-m" in element.attrib for element in root.iter()
    )
    if rendering.get("poster_layout") == "university-memorabilia":
        furniture_matches = {
            "layer-poster_title",
            "layer-poster_compass",
            "layer-poster_coordinates",
            "layer-poster_personalisation",
            "layer-poster_border",
        } <= groups
    else:
        furniture_matches = "layer-map_furniture" in groups
    return (
        furniture_matches
        and has_attribution == rendering.get("visible_attribution")
        and has_scale_bar == rendering.get("scale_bar")
    )


def _manifest_matches_contract(
    manifest: dict[str, Any],
    contract: dict[str, Any],
    digest: str,
) -> bool:
    identity = contract.get("identity")
    rendering = contract.get("rendering")
    details = contract.get("details")
    page = contract.get("page")
    if not all(
        isinstance(value, dict) for value in (identity, rendering, details, page)
    ):
        return False
    assert isinstance(identity, dict)
    assert isinstance(rendering, dict)
    assert isinstance(details, dict)
    assert isinstance(page, dict)
    manifest_rendering = manifest.get("rendering")
    expected_rendering = {
        key: value for key, value in rendering.items() if key != "scale_detail"
    }
    source = manifest.get("source")
    source_cohort = contract.get("source_cohort")
    design_contract = contract.get("design_contract")
    memorabilia = contract.get("memorabilia")
    typography = manifest.get("typography_evidence")
    design_matches = manifest.get("design_contract") == design_contract
    if isinstance(design_contract, dict):
        design_matches = (
            design_matches
            and isinstance(typography, dict)
            and (typography.get("font") == design_contract.get("font"))
        )
    elif typography is not None:
        design_matches = False
    memorabilia_matches = (
        _mapping_has_values(manifest.get("memorabilia"), memorabilia)
        if isinstance(memorabilia, dict)
        else manifest.get("memorabilia") is None
    )
    return (
        manifest.get("schema_version") == 2
        and manifest.get("generator") == GENERATOR_ID
        and manifest.get("title") == identity.get("title")
        and manifest.get("subtitle") == identity.get("subtitle")
        and manifest.get("details") == details.get("lines")
        and manifest.get("extent_wgs84") == contract.get("extent_wgs84")
        and manifest.get("families") == contract.get("families")
        and _mapping_has_values(manifest_rendering, expected_rendering)
        and _mapping_has_values(manifest.get("page"), page)
        and _mapping_has_values(
            source,
            {
                "provider": "OpenStreetMap contributors",
                "license": "ODbL 1.0",
                "attribution": OSM_ATTRIBUTION,
            },
        )
        and _manifest_source_matches_cohort(
            source,
            source_cohort,
            subject_id=identity.get("subject_id"),
        )
        and manifest.get("batch_source_cohort") == source_cohort
        and manifest.get("batch_artifact_contract") == _batch_binding(contract, digest)
        and design_matches
        and memorabilia_matches
    )


def _raster_matches_contract(
    item: dict[str, Any],
    manifest: dict[str, Any],
    contract: dict[str, Any],
    svg_path: Path,
) -> bool:
    expected = contract.get("raster")
    raster_exports = manifest.get("raster_exports")
    if expected is None:
        return "png" not in item and (raster_exports is None or raster_exports == [])
    if not isinstance(expected, dict) or "png" not in item:
        return False
    png_path = Path(str(item["png"]))
    if not png_path.is_file():
        return False
    try:
        header = png_path.read_bytes()[:24]
    except OSError:
        return False
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
        or int.from_bytes(header[16:20], "big") != expected.get("width_px")
        or int.from_bytes(header[20:24], "big") != expected.get("height_px")
    ):
        return False
    record = (
        raster_exports[0]
        if isinstance(raster_exports, list)
        and len(raster_exports) == 1
        and isinstance(raster_exports[0], dict)
        else None
    )
    if record is None or not isinstance(record.get("renderer"), str):
        return False
    try:
        return (
            bool(record["renderer"].strip())
            and _mapping_has_values(record, expected)
            and Path(str(record.get("path"))).resolve() == png_path.resolve()
            and record.get("source_svg_sha256") == file_sha256(svg_path)
            and record.get("png_sha256") == file_sha256(png_path)
        )
    except (KeyError, MapPlotterError):
        return False


def artifacts_are_valid(item: dict[str, Any]) -> bool:
    resolved = _contract_for_item(item)
    if resolved is None:
        return False
    contract, digest = resolved
    svg_path = Path(str(item["output"]))
    manifest_path = Path(str(item["manifest"]))
    if not svg_path.is_file() or not manifest_path.is_file():
        return False
    try:
        root = ET.parse(svg_path).getroot()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError, json.JSONDecodeError):
        return False
    return (
        (root.tag.endswith("}svg") or root.tag == "svg")
        and isinstance(manifest, dict)
        and _svg_matches_contract(root, contract, digest)
        and _manifest_matches_contract(manifest, contract, digest)
        and _raster_matches_contract(item, manifest, contract, svg_path)
    )


def artifact_hashes(item: dict[str, Any]) -> dict[str, str]:
    hashes = {
        "svg_sha256": file_sha256(Path(str(item["output"]))),
        "manifest_sha256": file_sha256(Path(str(item["manifest"]))),
    }
    if "png" in item:
        hashes["png_sha256"] = file_sha256(Path(str(item["png"])))
    return hashes


def completed_artifacts_match(item: dict[str, Any]) -> bool:
    if not artifacts_are_valid(item):
        return False
    expected_keys = ["svg_sha256", "manifest_sha256"]
    if "png" in item:
        expected_keys.append("png_sha256")
    if any(not isinstance(item.get(key), str) for key in expected_keys):
        return False
    hashes = artifact_hashes(item)
    return all(hashes[key] == item[key] for key in expected_keys)


def _artifact_presence(item: dict[str, Any]) -> tuple[bool, bool]:
    paths = [Path(str(item["output"])), Path(str(item["manifest"]))]
    if "png" in item:
        paths.append(Path(str(item["png"])))
    presence = [path.exists() for path in paths]
    return any(presence), all(presence)


def _preflight_batch_paths(
    plan: dict[str, Any],
    report_path: Path,
    *,
    protected_paths: Sequence[Path],
) -> None:
    outputs: list[tuple[str, Path]] = [("batch report", report_path)]
    for item in plan["items"]:
        subject_id = str(item["subject_id"])
        outputs.extend(
            (
                (f"{subject_id} SVG", Path(str(item["output"]))),
                (f"{subject_id} manifest", Path(str(item["manifest"]))),
            )
        )
        if "png" in item:
            outputs.append((f"{subject_id} PNG", Path(str(item["png"]))))
    for index, (left_label, left_path) in enumerate(outputs):
        for right_label, right_path in outputs[index + 1 :]:
            try:
                same_file = os.path.samefile(left_path, right_path)
            except (FileNotFoundError, OSError):
                same_file = left_path.resolve() == right_path.resolve()
            if same_file:
                raise MapPlotterError(
                    f"{left_label} path {left_path} collides with "
                    f"{right_label} path {right_path}."
                )
        for protected_path in protected_paths:
            try:
                same_file = os.path.samefile(left_path, protected_path)
            except (FileNotFoundError, OSError):
                same_file = left_path.resolve() == protected_path.resolve()
            if same_file:
                raise MapPlotterError(
                    f"{left_label} path {left_path} would overwrite protected "
                    f"input {protected_path}."
                )


def _prepare_batch_report(
    plan: dict[str, Any],
    report_path: Path,
    *,
    resume: bool,
    overwrite: bool,
) -> tuple[dict[str, Any], int]:
    if report_path.exists() and not report_path.is_file():
        raise MapPlotterError(f"Batch report path {report_path} is not a file.")
    if report_path.exists() and not overwrite:
        if not resume:
            raise MapPlotterError(
                f"Batch report {report_path} already exists. Use --resume, or "
                "use --no-resume --overwrite to replace its planned artifacts."
            )
        report = load_batch_report(report_path, plan)
    else:
        report = new_batch_report(plan)

    recovered = 0
    for item in report["items"]:
        status = str(item.get("status", "pending"))
        any_artifact, all_artifacts = _artifact_presence(item)

        if overwrite:
            continue
        if status == "completed":
            if completed_artifacts_match(item):
                continue
            if not any_artifact:
                item["status"] = "pending"
                item.pop("completed_at", None)
                item.pop("svg_sha256", None)
                item.pop("manifest_sha256", None)
                item.pop("png_sha256", None)
                continue
            raise MapPlotterError(
                f"Completed batch artifact for {item['subject_id']} was changed, "
                "is incomplete, or is invalid. Refusing to overwrite it during "
                "resume; preserve the edit elsewhere or restart with --overwrite."
            )
        if status == "running":
            if all_artifacts and artifacts_are_valid(item):
                item.update(artifact_hashes(item))
                item["status"] = "completed"
                item["completed_at"] = datetime.now(UTC).isoformat()
                item.pop("error", None)
                recovered += 1
            else:
                item["status"] = "pending"
            continue
        if status == "failed":
            # A caught renderer error is an explicit failed attempt, even when
            # it happened after files were published. Re-render those
            # batch-owned paths; only a process interrupted while ``running``
            # is eligible for artifact recovery.
            item["status"] = "pending"
            continue
        if status == "pending" and any_artifact:
            raise MapPlotterError(
                f"Unowned artifact already exists for pending subject "
                f"{item['subject_id']}. Choose another --output-dir or use "
                "--overwrite explicitly."
            )

    refresh_report_summary(report)
    return report, recovered


@contextmanager
def _exclusive_batch_report_lock(
    report_path: Path, *, plan_id: str, release_root: Path
) -> Iterator[None]:
    """Hold a crash-safe, non-blocking advisory lock for one batch report."""

    lock_path = _batch_report_lock_path(report_path, release_root=release_root)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = lock_path.resolve()
        if lock_path.is_relative_to(release_root.expanduser().resolve()):
            raise MapPlotterError(
                "Batch advisory lock resolved inside the release root."
            )
        lock_stream = lock_path.open("a+", encoding="utf-8")
    except MapPlotterError:
        raise
    except OSError as exc:
        raise MapPlotterError(f"Could not open batch lock {lock_path}: {exc}") from exc
    try:
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MapPlotterError(
                f"Another batch runner already holds {lock_path}. Wait for that "
                "run to finish before resuming this report."
            ) from exc
        lock_stream.seek(0)
        lock_stream.truncate()
        lock_stream.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_id": plan_id,
                    "acquired_at": datetime.now(UTC).isoformat(),
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        lock_stream.flush()
        yield
    finally:
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        finally:
            lock_stream.close()


def _batch_report_lock_path(report_path: Path, *, release_root: Path) -> Path:
    """Return a stable per-user advisory-lock path outside the release tree."""

    resolved_report = report_path.expanduser().resolve()
    digest = hashlib.sha256(os.fsencode(resolved_report)).hexdigest()
    user_id = os.getuid()
    candidates: list[Path] = []
    runtime_directory = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_directory:
        candidates.append(Path(runtime_directory).expanduser())
    cache_directory = os.environ.get("XDG_CACHE_HOME")
    candidates.append(
        Path(cache_directory).expanduser()
        if cache_directory
        else Path.home() / ".cache"
    )
    candidates.append(Path(tempfile.gettempdir()) / f"city-map-plotter-{user_id}")

    resolved_release_root = release_root.expanduser().resolve()
    for candidate in candidates:
        lock_root = (
            candidate.resolve()
            / "city-map-plotter"
            / "batch-locks"
        )
        if not lock_root.is_relative_to(resolved_release_root):
            return lock_root / f"{digest}.lock"
    raise MapPlotterError(
        "Could not place the batch advisory lock outside the release root."
    )


def _execute_batch_plan_unlocked(
    plan: dict[str, Any],
    *,
    report_path: Path,
    render_item: Callable[[dict[str, Any]], dict[str, Any] | None],
    resume: bool = True,
    overwrite: bool = False,
    keep_going: bool = False,
    delay_seconds: float = 2.0,
    delay_between_items: bool = True,
    protected_paths: Sequence[Path] = (),
    progress: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute a planned batch while maintaining a crash-safe report.

    ``render_item`` receives one planned item and must produce its SVG and
    manifest through the normal single-export path.  Unexpected exceptions are
    deliberately allowed to escape with the item left as ``running`` so a
    later invocation can identify and recover an interrupted export.
    """

    if not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise MapPlotterError("--delay-seconds must be a finite non-negative number.")
    resolved_report = report_path.expanduser().resolve()
    resolved_protected = tuple(path.expanduser().resolve() for path in protected_paths)
    _preflight_batch_paths(
        plan,
        resolved_report,
        protected_paths=resolved_protected,
    )
    report, recovered = _prepare_batch_report(
        plan,
        resolved_report,
        resume=resume,
        overwrite=overwrite,
    )
    run_started = datetime.now(UTC).isoformat()
    attempted = 0
    skipped = 0
    failed = 0
    write_batch_report(resolved_report, report)

    def finish_run() -> dict[str, Any]:
        refresh_report_summary(report)
        result = {
            "report": str(resolved_report),
            "plan_id": report["plan_id"],
            "item_count": len(report["items"]),
            "attempted": attempted,
            "skipped": skipped,
            "recovered": recovered,
            "failed_this_run": failed,
            "summary": dict(report["summary"]),
        }
        report["last_run"] = {
            **result,
            "started_at": run_started,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        write_batch_report(resolved_report, report)
        return result

    sleeper = sleep or time.sleep
    for index, item in enumerate(report["items"]):
        if item["status"] == "completed":
            skipped += 1
            continue
        # This is deliberately outside the per-item keep-going exception path:
        # a changed shared PBF, pen inventory, or style invalidates the cohort
        # for every remaining item and must abort the run before another render.
        _assert_item_dependencies_current(item)
        attempted += 1
        item["status"] = "running"
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["started_at"] = datetime.now(UTC).isoformat()
        item.pop("completed_at", None)
        item.pop("error", None)
        item.pop("svg_sha256", None)
        item.pop("manifest_sha256", None)
        item.pop("png_sha256", None)
        refresh_report_summary(report)
        write_batch_report(resolved_report, report)
        if progress is not None:
            progress(
                f"[{index + 1}/{len(report['items'])}] exporting {item['subject_id']}"
            )
        try:
            export_summary = render_item(item)
            bind_artifact_contract(item)
            if not artifacts_are_valid(item):
                raise MapPlotterError(
                    f"Export for {item['subject_id']} did not produce a valid SVG "
                    "and schema-version-2 manifest."
                )
            item.update(artifact_hashes(item))
            item["status"] = "completed"
            item["completed_at"] = datetime.now(UTC).isoformat()
            if export_summary is not None:
                item["export_summary"] = export_summary
            refresh_report_summary(report)
            write_batch_report(resolved_report, report)
        except MapPlotterError as exc:
            failed += 1
            item["status"] = "failed"
            item["failed_at"] = datetime.now(UTC).isoformat()
            item["error"] = str(exc)
            refresh_report_summary(report)
            write_batch_report(resolved_report, report)
            if not keep_going:
                finish_run()
                raise

        has_later_work = any(
            later["status"] != "completed" for later in report["items"][index + 1 :]
        )
        if delay_between_items and delay_seconds > 0 and has_later_work:
            sleeper(delay_seconds)

    result = finish_run()
    return report, result


def execute_batch_plan(
    plan: dict[str, Any],
    *,
    report_path: Path,
    render_item: Callable[[dict[str, Any]], dict[str, Any] | None],
    resume: bool = True,
    overwrite: bool = False,
    keep_going: bool = False,
    delay_seconds: float = 2.0,
    delay_between_items: bool = True,
    protected_paths: Sequence[Path] = (),
    progress: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute one batch while exclusively owning its report and artifacts."""

    resolved_report = report_path.expanduser().resolve()
    release_root = Path(str(plan.get("output_dir", ""))).expanduser().resolve()
    with _exclusive_batch_report_lock(
        resolved_report,
        plan_id=str(plan.get("plan_id", "unknown")),
        release_root=release_root,
    ):
        return _execute_batch_plan_unlocked(
            plan,
            report_path=resolved_report,
            render_item=render_item,
            resume=resume,
            overwrite=overwrite,
            keep_going=keep_going,
            delay_seconds=delay_seconds,
            delay_between_items=delay_between_items,
            protected_paths=protected_paths,
            progress=progress,
            sleep=sleep,
        )

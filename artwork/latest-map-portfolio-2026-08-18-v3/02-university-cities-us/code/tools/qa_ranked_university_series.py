#!/usr/bin/env python3
"""Fail-closed audit for the frozen ranked-university memorabilia series.

This is deliberately a release verifier, not a renderer helper.  It trusts
neither the batch report nor adjacent files: every identity is rebound to the
bundled ranking catalog, every declared byte is re-hashed, and plot geometry
is independently inspected with the frozen v2.1 format contract.

The v2.1.4 release is bound to exact, per-subject saved Overpass responses.
Those pinned responses make the artwork repeatable, but the nominal pen
inventory remains review-only. ``--release-mode production`` therefore fails
closed rather than silently treating reproducible source bytes as a physical
plot calibration.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import gzip
import hashlib
import json
from math import cos, isfinite, pi, radians
import os
from pathlib import Path
import re
import struct
import sys
import tarfile
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree as ET
import zlib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from city_map_plotter.retrace_audit import (  # type: ignore[import-untyped] # noqa: E402
    AUDIT_SCOPE_ALL_PHYSICAL,
    RetraceAuditError,
    audit_svg_retraces,
)
from plotsim import (  # noqa: E402
    Machine,
    _offset_geometry_warnings,
    load_plate,
    order_strokes,
    simulate,
)
from validate_format import validate  # noqa: E402


POLICY_ID = "ranked-university-series-qa-v1"
CATALOG_VERSION = "ranked-universities-2026-v1"
DEFAULT_CATALOG = ROOT / "src/city_map_plotter/data/ranked-universities-2026-v1.json"
EXPECTED_CATALOG_SHA256 = (
    "9a58174a4e13f0ac7a66f9a91d4789d63ca71cdcb7d899d534731deca4f1e5fe"
)
UK_COLLECTION = "uk-times-good-university-guide-2026-top-30"
US_COLLECTION = "us-qs-world-university-rankings-2027-top-20"
COLLECTIONS = (UK_COLLECTION, US_COLLECTION)
COLLECTION_COUNTS = {UK_COLLECTION: 30, US_COLLECTION: 20}
COLLECTION_EDITIONS = {UK_COLLECTION: 2026, US_COLLECTION: 2027}
COLLECTION_COUNTRIES = {UK_COLLECTION: "GB", US_COLLECTION: "US"}

EXPECTED_RENDERER_ARCHIVE_SHA256 = (
    "794e4a44716e3739d22200370203a171cad05a52f79b5f00949c461fa46998f7"
)
EXPECTED_RENDERER_FINGERPRINT_SHA256 = (
    "375d54f1cb29c68227dd5ddff8d05235b8f137d4e433ae31bd5834d611dab5e7"
)
EXPECTED_STYLE_SHA256 = (
    "d5bc3c092d6cc05bbbc9581b5463a043716cfd5a8b237f8df634a44d6b6f7910"
)
EXPECTED_FORMAT_SHA256 = (
    "87cb84c49795b6e95816d18a2516d78909235dfba9416dbd3727cddc1a2f53b2"
)
STYLE_TAR_MEMBER = "renderer-contract/university-memorabilia-v2.json"
FORMAT_TAR_MEMBER = "renderer-contract/city_map_plotter/data/format-v1.json"
SERIES_ID = "university-memorabilia-ranked-2026-v2.1.4"
DERIVATION_ID = "university-memorabilia-v2.1.4-pinned-source-correctness"
DERIVATION_VISUAL_POLICY = "v2.1 parameters and style are unchanged"
DERIVATION_OVERRIDES = {
    "city_map_plotter/cartography.py": "454e60954507f5cc056a14c0d4ddd6b80df4d511860f1edafeabbed49c361dc2",
    "city_map_plotter/batch.py": "25028ecd6009b687207c98af7fcecb0db6b494c1950be900431558e84097030f",
    "city_map_plotter/cli.py": "f0433203d60088243fa4dc796b2b508926ad124c1fb2eaa096177b714b21ff7b",
    "city_map_plotter/completeness.py": "6cef168aae36ad154175ac6137ae019052229ea59889285887fc08d96018d75d",
    "city_map_plotter/svg.py": "755f896b0fa22667e0898a788a4840e89a562f2f9c8c11423c86eab48abaf57c",
}
EXPECTED_DERIVED_RENDERER_TREE_SHA256 = (
    "2197dace775a09bd73d70cc9233be3689f5439f0e10942000ebc191f42065e4c"
)
EXPECTED_DERIVED_RENDERER_FINGERPRINT_SHA256 = (
    "0a2106ff042bcccb7e73ad5fe3d253d0a5c7c18f2ca2425a35ea33aa29f69366"
)
EXPECTED_RENDER_RECIPE_SHA256 = (
    "a3a8e6932fe4d6175e90ec25d5ce30922dad39c434297ef9a6a722a96dd153fc"
)
EXPECTED_SOURCE_MANIFEST_SHA256 = (
    "581d07bc7262664d4b1134ec42b30ddd9a086f9915e067068b4f0fe77e121362"
)
EXPECTED_SOURCE_COHORT_SHA256 = (
    "1a861b085466c23ce2c97ce03b3807459bf96cf3e0c3c0faccbf3079b97cd6d3"
)
SOURCE_CONTRACT_ID = "university-memorabilia-ranked-2026-osm-snapshots-v1"
SOURCE_COHORT_POLICY_ID = "city-map-source-cohort-v1"
SOURCE_COHORT_REASON = (
    "Every selected subject is bound to exact saved Overpass JSON bytes, "
    "but saved JSON cohorts remain review-only under the production source policy."
)
SOURCE_LICENSE = {
    "data": "Open Database License (ODbL) 1.0",
    "attribution": "© OpenStreetMap contributors",
    "copyright_url": "https://www.openstreetmap.org/copyright",
    "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
}
EXPECTED_BASE_FINGERPRINT: dict[str, Any] = {
    "generator": "city-map-plotter 0.2.0",
    "source_file_count": 28,
    "source_tree_sha256": "5d339e1a7ea83405d2d48cac76a55edb6ddd92f419d47087d2c527d429d9945c",
    "format_resource": "city_map_plotter/data/format-v1.json",
    "format_id": "plate-format-v1",
    "format_sha256": EXPECTED_FORMAT_SHA256,
    "theme_resource": "city_map_plotter/data/themes-v1.json",
    "theme_catalog_id": "city-map-plotter-themes-v2",
    "theme_catalog_sha256": "17e2649675de85dffba23ca7d3d06d2483e49c8aede788447a5f365b6992ad98",
    "sha256": EXPECTED_RENDERER_FINGERPRINT_SHA256,
}

EXPECTED_FAMILIES = ["roads", "water", "railways", "parks", "buildings"]
EXPECTED_MAP_BOUNDS = {"x": 12.0, "y": 35.135, "width": 124.0, "height": 128.898}
EXPECTED_EXTERNAL_ATTRIBUTION = (
    "Accompanying product page, packaging, and series attribution file"
)
EXPECTED_PNG = (1480, 2100, 10000, 10000)
EARTH_RADIUS_M = 6_371_008.8
SERIES_RADIUS_M = 2_000.0
SERIES_MAP_FIELD_ASPECT = 0.962
FORBIDDEN_DRAWABLES = frozenset(
    {"text", "line", "polyline", "polygon", "rect", "circle", "ellipse", "use", "image"}
)
ALLOWED_SVG_ELEMENTS = frozenset(
    {"svg", "title", "desc", "metadata", "namedview", "g", "path"}
)
EXPECTED_LAYER_COLORS = {
    "water_areas": "#6eadd0",
    "rivers": "#6eadd0",
    "waterways": "#78b4d3",
    "green_space": "#82b89a",
    "buildings": "#98769f",
    "road_areas": "#a4abb1",
    "roads_local": "#7f8992",
    "roads_other": "#939ca4",
    "paths": "#a2a9af",
    "railways": "#67737d",
    "roads_major": "#cf807a",
    "roads_secondary": "#d99a94",
    "frame": "#17212b",
    "poster_title": "#26333d",
    "poster_compass": "#26333d",
    "poster_border": "#17212b",
    "poster_coordinates": "#58636d",
    "poster_personalisation": "#58636d",
}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
OVERPASS_BBOX_RE = re.compile(
    r"\((-?(?:0|[1-9]\d*)(?:\.\d+)?),"
    r"(-?(?:0|[1-9]\d*)(?:\.\d+)?),"
    r"(-?(?:0|[1-9]\d*)(?:\.\d+)?),"
    r"(-?(?:0|[1-9]\d*)(?:\.\d+)?)\)"
)
CONTACT_NAMES = {
    UK_COLLECTION: "uk-ranked-universities-contact-sheet.png",
    US_COLLECTION: "us-ranked-universities-contact-sheet.png",
}
QA_REPORT_NAMES = frozenset(
    {
        "RANKED_UNIVERSITY_QA_REPORT.json",
        "RANKED_UNIVERSITY_QA_REPORT-pilot.json",
    }
)
FINALIZATION_CHECKSUM_POLICY = (
    "Every release file is declared except root CHECKSUMS.sha256 and the "
    "regenerated root QA reports RANKED_UNIVERSITY_QA_REPORT.json and "
    "RANKED_UNIVERSITY_QA_REPORT-pilot.json."
)


@dataclass(frozen=True)
class RankedRow:
    collection_id: str
    position: int
    subject_id: str
    institution_name: str
    city: str
    country_code: str
    latitude: float
    longitude: float
    rank: str
    rank_number: int
    tied: bool
    edition: int
    score: float | None

    @property
    def title(self) -> str:
        return self.city.upper()

    @property
    def stem(self) -> str:
        return f"{self.position:03d}-{self.subject_id}"

    @property
    def coordinate(self) -> str:
        latitude = f"{abs(self.latitude):.4f} {'N' if self.latitude >= 0 else 'S'}"
        longitude = f"{abs(self.longitude):.4f} {'E' if self.longitude >= 0 else 'W'}"
        return f"{latitude} / {longitude}"


@dataclass(frozen=True)
class PinnedSourceEntry:
    subject_id: str
    manifest_path: str
    path: Path
    size_bytes: int
    sha256: str
    canonical_json_sha256: str
    query_sha256: str
    osm_base_timestamp: str
    extent_wgs84: dict[str, float]
    record_sha256: str

    def cohort_record(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "manifest_path": self.manifest_path,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "canonical_json_sha256": self.canonical_json_sha256,
            "query_sha256": self.query_sha256,
            "osm_base_timestamp": self.osm_base_timestamp,
            "extent_wgs84": self.extent_wgs84,
            "record_sha256": self.record_sha256,
        }


@dataclass(frozen=True)
class PinnedSourceBundle:
    manifest_path: Path
    manifest_size_bytes: int
    entries: dict[str, PinnedSourceEntry]
    artifacts: frozenset[Path]


@dataclass(frozen=True)
class PenContract:
    step: int
    pen_id: str
    ink: str
    nib_mm: float
    allowed_layers: frozenset[str]
    exact_layers: bool = False


PENS = (
    PenContract(1, "blue-0-4", "Blue", 0.40, frozenset({"water_areas", "rivers"})),
    PenContract(2, "blue-0-25", "Blue", 0.25, frozenset({"waterways"}), True),
    PenContract(3, "green-0-25", "Green", 0.25, frozenset({"green_space"}), True),
    PenContract(4, "purple-0-25", "Purple", 0.25, frozenset({"buildings"}), True),
    PenContract(
        5,
        "grey-0-25",
        "Grey",
        0.25,
        frozenset({"road_areas", "roads_local", "roads_other", "paths", "railways"}),
    ),
    PenContract(6, "red-0-4", "Red", 0.40, frozenset({"roads_major"}), True),
    PenContract(7, "red-0-25", "Red", 0.25, frozenset({"roads_secondary"}), True),
    PenContract(
        8,
        "black-0-4",
        "Black",
        0.40,
        frozenset({"frame", "poster_title", "poster_compass"}),
        True,
    ),
    PenContract(9, "black-0-6", "Black", 0.60, frozenset({"poster_border"}), True),
    PenContract(
        10,
        "black-0-25",
        "Black",
        0.25,
        frozenset({"poster_coordinates", "poster_personalisation"}),
        True,
    ),
)
PEN_BY_STEP = {item.step: item for item in PENS}

SVG_NS = "http://www.w3.org/2000/svg"
MAP_NS = "urn:city-map-plotter:metadata"


class AuditInputError(RuntimeError):
    """The audit cannot start because its input is unreadable or unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _file_map(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and item.suffix not in {".pyc", ".pyo"}
    ):
        result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _tree_digest_payloads(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative, payload in sorted(files.items()):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def _archive_file_map(archive: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle.getmembers():
            if not member.isfile() or not member.name.startswith("renderer-contract/"):
                continue
            relative = member.name.removeprefix("renderer-contract/")
            if (
                not relative
                or "__pycache__" in Path(relative).parts
                or Path(relative).suffix in {".pyc", ".pyo"}
            ):
                continue
            stream = bundle.extractfile(member)
            if stream is None:
                raise AuditInputError(
                    f"Could not read renderer archive member {member.name}"
                )
            if relative in result:
                raise AuditInputError(
                    f"Duplicate renderer archive member {member.name}"
                )
            result[relative] = stream.read()
    return result


def _renderer_fingerprint_from_files(files: dict[str, bytes]) -> dict[str, Any]:
    python_files = {
        relative.removeprefix("city_map_plotter/"): payload
        for relative, payload in files.items()
        if relative.startswith("city_map_plotter/") and relative.endswith(".py")
    }
    source_digest = hashlib.sha256()
    for relative, payload in sorted(python_files.items()):
        source_digest.update(relative.encode("utf-8"))
        source_digest.update(b"\0")
        source_digest.update(payload)
        source_digest.update(b"\0")
    format_relative = "city_map_plotter/data/format-v1.json"
    theme_relative = "city_map_plotter/data/themes-v1.json"
    try:
        format_payload = files[format_relative]
        theme_payload = files[theme_relative]
        format_document = json.loads(format_payload)
        theme_document = json.loads(theme_payload)
    except (KeyError, json.JSONDecodeError) as exc:
        raise AuditInputError(
            f"Renderer data contract is missing/invalid: {exc}"
        ) from exc
    fingerprint = {
        "generator": "city-map-plotter 0.2.0",
        "source_file_count": len(python_files),
        "source_tree_sha256": source_digest.hexdigest(),
        "format_resource": format_relative,
        "format_id": format_document.get("id"),
        "format_sha256": hashlib.sha256(format_payload).hexdigest(),
        "theme_resource": theme_relative,
        "theme_catalog_id": theme_document.get("id"),
        "theme_catalog_sha256": hashlib.sha256(theme_payload).hexdigest(),
    }
    return {**fingerprint, "sha256": _stable_digest(fingerprint)}


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditInputError(f"Could not read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AuditInputError(f"JSON document {path} is not an object.")
    return value


def _append(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _is_designated_qa_report(path: Path, root: Path) -> bool:
    return path.parent == root and path.name in QA_REPORT_NAMES


def _transient_kind(path: Path, root: Path) -> str | None:
    relative = path.relative_to(root)
    if "__pycache__" in relative.parts:
        return "__pycache__"
    if path.name == ".lock" or path.suffix == ".lock":
        return ".lock"
    if path.name == ".tmp" or path.suffix == ".tmp":
        return "*.tmp"
    if path.suffix in {".pyc", ".pyo"}:
        return path.suffix
    return None


def _release_hygiene_failures(root: Path) -> list[str]:
    failures: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        transient = _transient_kind(path, root)
        if transient is not None:
            failures.append(
                f"release contains forbidden transient path ({transient}): {relative}"
            )
        if path.name.startswith("RANKED_UNIVERSITY_QA_REPORT") and (
            not _is_designated_qa_report(path, root) or not path.is_file()
        ):
            failures.append(f"release contains ambiguous QA-report alias: {relative}")
    return failures


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and HEX_SHA256.fullmatch(value) is not None


def _validate_pinned_source_manifest(
    manifest_path: Path,
    expected_rows: Sequence[RankedRow],
    failures: list[str],
) -> PinnedSourceBundle | None:
    """Independently validate the copied 50-response source contract."""

    started = len(failures)
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or _sha256(manifest_path) != EXPECTED_SOURCE_MANIFEST_SHA256
    ):
        failures.append("copied university source manifest bytes drift")
        return None
    try:
        manifest = _json(manifest_path)
    except AuditInputError as exc:
        failures.append(str(exc))
        return None
    expected_manifest_keys = {
        "schema_version",
        "id",
        "status",
        "as_of",
        "subject_count",
        "license",
        "entries",
        "cohort_sha256",
    }
    _append(
        set(manifest) == expected_manifest_keys,
        "university source manifest schema drift",
        failures,
    )
    _append(
        manifest.get("schema_version") == 1
        and manifest.get("id") == SOURCE_CONTRACT_ID
        and manifest.get("status") == "review-only-pinned-source"
        and manifest.get("as_of") == "2026-08-03"
        and manifest.get("subject_count") == len(expected_rows),
        "university source manifest identity/count drift",
        failures,
    )
    _append(
        manifest.get("license") == SOURCE_LICENSE,
        "university source manifest ODbL notice drift",
        failures,
    )
    logical = {key: value for key, value in manifest.items() if key != "cohort_sha256"}
    _append(
        manifest.get("cohort_sha256")
        == _stable_digest(logical)
        == EXPECTED_SOURCE_COHORT_SHA256,
        "university source cohort digest drift",
        failures,
    )
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list):
        failures.append("university source manifest entries must be an array")
        return None
    expected_ids = [row.subject_id for row in expected_rows]
    observed_ids = [
        entry.get("subject_id") if isinstance(entry, dict) else None
        for entry in raw_entries
    ]
    _append(
        observed_ids == expected_ids,
        "university source manifest subject order/scope drift",
        failures,
    )

    manifest_root = manifest_path.parent.resolve()
    overpass_root = manifest_root / "overpass"
    entries: dict[str, PinnedSourceEntry] = {}
    artifacts: set[Path] = {manifest_path.resolve()}
    expected_entry_keys = {
        "subject_id",
        "path",
        "size_bytes",
        "sha256",
        "canonical_json_sha256",
        "query_sha256",
        "osm_base_timestamp",
        "extent_wgs84",
    }
    rows_by_id = {row.subject_id: row for row in expected_rows}
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict) or set(raw) != expected_entry_keys:
            failures.append(f"university source entry {index} schema drift")
            continue
        subject_id = raw.get("subject_id")
        if not isinstance(subject_id, str) or subject_id not in rows_by_id:
            failures.append(f"university source entry {index} has unknown subject")
            continue
        relative_text = raw.get("path")
        expected_relative = f"overpass/{subject_id}.json.gz"
        if relative_text != expected_relative:
            failures.append(f"{subject_id}: source snapshot path drift")
            continue
        relative = Path(expected_relative)
        source_path = (manifest_root / relative).resolve()
        if not source_path.is_relative_to(manifest_root):
            failures.append(f"{subject_id}: source snapshot escapes its contract")
            continue
        size = raw.get("size_bytes")
        declared_sha = raw.get("sha256")
        canonical_sha = raw.get("canonical_json_sha256")
        query_sha = raw.get("query_sha256")
        timestamp = raw.get("osm_base_timestamp")
        extent = raw.get("extent_wgs84")
        valid_fields = (
            isinstance(size, int)
            and not isinstance(size, bool)
            and size > 0
            and _is_sha256(declared_sha)
            and _is_sha256(canonical_sha)
            and _is_sha256(query_sha)
            and isinstance(timestamp, str)
            and bool(timestamp)
            and _extent_matches(extent, rows_by_id[subject_id])
        )
        _append(valid_fields, f"{subject_id}: source manifest fields drift", failures)
        if not valid_fields:
            continue
        if not source_path.is_file() or source_path.is_symlink():
            failures.append(f"{subject_id}: source snapshot is missing or a symlink")
            continue
        artifacts.add(source_path)
        _append(
            source_path.stat().st_size == size and _sha256(source_path) == declared_sha,
            f"{subject_id}: compressed source bytes drift",
            failures,
        )
        try:
            with gzip.open(source_path, "rt", encoding="utf-8") as stream:
                response = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(f"{subject_id}: source snapshot is not gzip JSON: {exc}")
            continue
        _append(
            isinstance(response, dict) and _stable_digest(response) == canonical_sha,
            f"{subject_id}: canonical source JSON drift",
            failures,
        )
        osm3s = response.get("osm3s") if isinstance(response, dict) else None
        _append(
            isinstance(osm3s, dict)
            and osm3s.get("timestamp_osm_base") == timestamp
            and "openstreetmap" in str(osm3s.get("copyright", "")).casefold()
            and "odbl" in str(osm3s.get("copyright", "")).casefold()
            and isinstance(response.get("elements"), list),
            f"{subject_id}: source timestamp/licence/payload drift",
            failures,
        )
        assert isinstance(size, int)
        assert isinstance(declared_sha, str)
        assert isinstance(canonical_sha, str)
        assert isinstance(query_sha, str)
        assert isinstance(timestamp, str)
        assert isinstance(extent, dict)
        entries[subject_id] = PinnedSourceEntry(
            subject_id=subject_id,
            manifest_path=expected_relative,
            path=source_path,
            size_bytes=size,
            sha256=declared_sha,
            canonical_json_sha256=canonical_sha,
            query_sha256=query_sha,
            osm_base_timestamp=timestamp,
            extent_wgs84={key: float(value) for key, value in extent.items()},
            record_sha256=_stable_digest(raw),
        )

    actual_sources = {
        path.resolve()
        for path in overpass_root.glob("*.json.gz")
        if path.is_file() and not path.is_symlink()
    }
    expected_sources = {entry.path for entry in entries.values()}
    _append(
        actual_sources == expected_sources and len(entries) == len(expected_rows),
        "university source snapshot inventory drift",
        failures,
    )
    notice = manifest_root / "NOTICE.md"
    checksums_path = manifest_root / "CHECKSUMS.sha256"
    artifacts.update({notice, checksums_path})
    try:
        notice_text = notice.read_text(encoding="utf-8")
    except OSError:
        notice_text = ""
    _append(
        "OpenStreetMap contributors" in notice_text
        and "ODbL" in notice_text
        and "openstreetmap.org/copyright" in notice_text,
        "university source NOTICE is missing its ODbL attribution",
        failures,
    )
    checksum_failures: list[str] = []
    checksum_records = read_checksums(checksums_path, manifest_root, checksum_failures)
    failures.extend(f"source contract {item}" for item in checksum_failures)
    expected_checksums = {
        entry.manifest_path: entry.sha256 for entry in entries.values()
    }
    expected_checksums[manifest_path.name] = EXPECTED_SOURCE_MANIFEST_SHA256
    _append(
        checksum_records == expected_checksums,
        "university source CHECKSUMS inventory drift",
        failures,
    )
    if len(failures) != started:
        return None
    return PinnedSourceBundle(
        manifest_path=manifest_path.resolve(),
        manifest_size_bytes=manifest_path.stat().st_size,
        entries=entries,
        artifacts=frozenset(path.resolve() for path in artifacts),
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None


def _same_number(left: object, right: float, tolerance: float = 1e-6) -> bool:
    value = _number(left)
    return value is not None and abs(value - right) <= tolerance


def _expected_extent(row: RankedRow) -> dict[str, float]:
    """Reproduce the frozen 2 km, A5-contain extent without trusting the batch."""

    latitude_delta = SERIES_RADIUS_M / EARTH_RADIUS_M * 180.0 / pi
    longitude_delta = latitude_delta / max(cos(radians(row.latitude)), 1e-9)
    # BoundingBox.around() is physically square.  A5's map field is slightly
    # narrower than square, so contain-fit expands only the latitude span.
    latitude_delta /= SERIES_MAP_FIELD_ASPECT
    return {
        "west": row.longitude - longitude_delta,
        "south": row.latitude - latitude_delta,
        "east": row.longitude + longitude_delta,
        "north": row.latitude + latitude_delta,
    }


def _extent_matches(value: object, row: RankedRow) -> bool:
    expected = _expected_extent(row)
    return (
        isinstance(value, dict)
        and set(value) == set(expected)
        and all(
            _same_number(value.get(key), expected_value, 1e-10)
            for key, expected_value in expected.items()
        )
    )


def _expected_acquisition_bbox_text(row: RankedRow) -> str:
    extent = _expected_extent(row)
    centre_latitude = (extent["south"] + extent["north"]) / 2.0
    centre_longitude = (extent["west"] + extent["east"]) / 2.0
    latitude_half_span = (extent["north"] - extent["south"]) / 2.0 * 1.06
    longitude_half_span = (extent["east"] - extent["west"]) / 2.0 * 1.06
    return (
        f"{centre_latitude - latitude_half_span:.7f},"
        f"{centre_longitude - longitude_half_span:.7f},"
        f"{centre_latitude + latitude_half_span:.7f},"
        f"{centre_longitude + longitude_half_span:.7f}"
    )


def _query_uses_only_expected_bbox(query: object, row: RankedRow) -> bool:
    if not isinstance(query, str) or not query:
        return False
    observed = {",".join(match) for match in OVERPASS_BBOX_RE.findall(query)}
    return observed == {_expected_acquisition_bbox_text(row)}


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _resolve_release_path(raw: object, root: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise AuditInputError(f"{label} is not a non-empty path.")
    path = Path(raw).expanduser()
    path = path if path.is_absolute() else root / path
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise AuditInputError(f"Could not resolve {label} {path}: {exc}") from exc
    if not _within(resolved, root):
        raise AuditInputError(f"{label} escapes the release root: {resolved}")
    return resolved


def _resolve_pinned_source_path(
    raw: object, root: Path, expected: Path, label: str
) -> Path:
    if isinstance(raw, str) and ".." in Path(raw).parts:
        raise AuditInputError(f"{label} contains forbidden path traversal.")
    resolved = _resolve_release_path(raw, root, label)
    if resolved != expected.resolve():
        raise AuditInputError(f"{label} differs from the pinned subject snapshot.")
    if not resolved.name.endswith(".json.gz") or resolved.is_symlink():
        raise AuditInputError(f"{label} is not a canonical snapshot file.")
    return resolved


def validate_catalog_document(
    document: dict[str, Any], failures: list[str]
) -> list[RankedRow]:
    """Validate and flatten the exact 30 UK + 20 US selection."""

    _append(
        document.get("schema_version") == 1,
        "catalog schema_version must be 1",
        failures,
    )
    _append(
        document.get("catalog_version") == CATALOG_VERSION,
        f"catalog version must be {CATALOG_VERSION!r}",
        failures,
    )
    _append(document.get("as_of") == "2026-08-03", "catalog as_of drift", failures)
    subjects_raw = document.get("subjects")
    collections_raw = document.get("collections")
    if not isinstance(subjects_raw, list) or not isinstance(collections_raw, list):
        failures.append("catalog subjects and collections must be arrays")
        return []
    subjects: dict[str, dict[str, Any]] = {}
    for index, subject in enumerate(subjects_raw):
        if not isinstance(subject, dict) or not isinstance(subject.get("id"), str):
            failures.append(f"catalog subject {index} is malformed")
            continue
        identifier = str(subject["id"])
        if identifier in subjects:
            failures.append(f"duplicate catalog subject {identifier!r}")
        subjects[identifier] = subject
    collection_ids = [
        item.get("id") for item in collections_raw if isinstance(item, dict)
    ]
    _append(
        collection_ids == list(COLLECTIONS),
        f"catalog collection order/scope drift: {collection_ids!r}",
        failures,
    )
    rows: list[RankedRow] = []
    used_subjects: set[str] = set()
    city_centres: dict[tuple[str, str], set[tuple[float, float]]] = defaultdict(set)
    for collection in collections_raw:
        if not isinstance(collection, dict):
            continue
        collection_id = collection.get("id")
        if collection_id not in COLLECTIONS:
            continue
        entries = collection.get("entries")
        if not isinstance(entries, list):
            failures.append(f"{collection_id}: entries are not an array")
            continue
        _append(
            len(entries) == COLLECTION_COUNTS[str(collection_id)],
            f"{collection_id}: expected {COLLECTION_COUNTS[str(collection_id)]} entries",
            failures,
        )
        previous_rank = 0
        for expected_position, entry in enumerate(entries, 1):
            prefix = f"{collection_id}[{expected_position}]"
            if not isinstance(entry, dict):
                failures.append(f"{prefix}: entry is not an object")
                continue
            subject_id = entry.get("subject_id")
            subject = subjects.get(str(subject_id))
            if subject is None:
                failures.append(f"{prefix}: unknown subject {subject_id!r}")
                continue
            _append(
                entry.get("position") == expected_position,
                f"{prefix}: position drift",
                failures,
            )
            _append(
                str(subject_id) not in used_subjects,
                f"{prefix}: duplicate subject selection",
                failures,
            )
            used_subjects.add(str(subject_id))
            location = subject.get("location")
            map_record = subject.get("map")
            details = subject.get("details")
            if not all(
                isinstance(value, dict) for value in (location, map_record, details)
            ):
                failures.append(f"{prefix}: malformed subject location/map/details")
                continue
            assert isinstance(location, dict)
            assert isinstance(map_record, dict)
            assert isinstance(details, dict)
            center = map_record.get("center")
            if (
                not isinstance(center, list)
                or len(center) != 2
                or _number(center[0]) is None
                or _number(center[1]) is None
            ):
                failures.append(f"{prefix}: invalid campus centre")
                continue
            latitude, longitude = float(center[0]), float(center[1])
            rank = entry.get("rank")
            rank_number = entry.get("rank_number")
            tied = entry.get("tied")
            expected_rank = str(rank_number)
            if (
                isinstance(rank_number, int)
                and not isinstance(rank_number, bool)
                and tied is True
            ):
                # Preserve the publisher's actual notation: Times appends the
                # equality marker while QS prefixes it.
                expected_rank = (
                    f"{rank_number}="
                    if collection_id == UK_COLLECTION
                    else f"={rank_number}"
                )
            _append(
                isinstance(rank, str) and rank == expected_rank,
                f"{prefix}: rank/tie encoding drift",
                failures,
            )
            _append(
                isinstance(rank_number, int)
                and not isinstance(rank_number, bool)
                and rank_number >= previous_rank,
                f"{prefix}: invalid or decreasing rank_number",
                failures,
            )
            if isinstance(rank_number, int) and not isinstance(rank_number, bool):
                previous_rank = rank_number
            _append(isinstance(tied, bool), f"{prefix}: tied must be Boolean", failures)
            _append(
                entry.get("edition") == COLLECTION_EDITIONS[str(collection_id)],
                f"{prefix}: ranking edition drift",
                failures,
            )
            institution_name = subject.get("name")
            _append(
                isinstance(institution_name, str)
                and institution_name.strip() != ""
                and entry.get("ranking_name") == institution_name,
                f"{prefix}: institution/ranking name drift",
                failures,
            )
            city = location.get("city")
            country_code = location.get("country_code")
            _append(
                isinstance(city, str) and bool(city.strip()),
                f"{prefix}: city is missing",
                failures,
            )
            _append(
                country_code == COLLECTION_COUNTRIES[str(collection_id)],
                f"{prefix}: country scope drift",
                failures,
            )
            _append(
                subject.get("kind") == "university",
                f"{prefix}: kind is not university",
                failures,
            )
            _append(
                map_record.get("purpose") == "campus",
                f"{prefix}: purpose is not campus",
                failures,
            )
            _append(
                _same_number(map_record.get("preview_radius_km"), 2.0),
                f"{prefix}: radius is not 2 km",
                failures,
            )
            _append(
                isinstance(map_record.get("query"), str)
                and bool(map_record["query"].strip()),
                f"{prefix}: query is missing",
                failures,
            )
            _append(
                details.get("geometry_status") == "seed_point_ready",
                f"{prefix}: campus seed not review-ready",
                failures,
            )
            _append(
                isinstance(details.get("coordinate_provenance"), dict),
                f"{prefix}: coordinate provenance missing",
                failures,
            )
            _append(
                isinstance(details.get("institution_url"), str),
                f"{prefix}: institution URL missing",
                failures,
            )
            score = _number(entry.get("score")) if "score" in entry else None
            if collection_id == US_COLLECTION:
                _append(
                    score is not None and 0 <= score <= 100,
                    f"{prefix}: US score missing/out of range",
                    failures,
                )
            else:
                _append(
                    "score" not in entry, f"{prefix}: unexpected UK score", failures
                )
            if not isinstance(institution_name, str) or not isinstance(city, str):
                continue
            city_key = (str(country_code), city.casefold())
            centre = (latitude, longitude)
            _append(
                centre not in city_centres[city_key],
                f"{prefix}: co-located institutions reuse the same campus centre",
                failures,
            )
            city_centres[city_key].add(centre)
            if (
                isinstance(rank, str)
                and isinstance(rank_number, int)
                and not isinstance(rank_number, bool)
                and isinstance(tied, bool)
            ):
                rows.append(
                    RankedRow(
                        collection_id=str(collection_id),
                        position=expected_position,
                        subject_id=str(subject_id),
                        institution_name=institution_name,
                        city=city,
                        country_code=str(country_code),
                        latitude=latitude,
                        longitude=longitude,
                        rank=rank,
                        rank_number=rank_number,
                        tied=tied,
                        edition=COLLECTION_EDITIONS[str(collection_id)],
                        score=score,
                    )
                )
    _append(
        len(rows) == 50,
        f"catalog resolves {len(rows)} valid rows, expected 50",
        failures,
    )
    _append(
        len(subjects) == 50,
        f"catalog contains {len(subjects)} subjects, expected 50",
        failures,
    )
    return rows


def _report_rows(
    report: dict[str, Any],
    catalog_rows: Sequence[RankedRow],
    allow_incomplete: bool,
    failures: list[str],
) -> list[tuple[RankedRow, dict[str, Any]]]:
    items = report.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        failures.append("batch report items must be an array of objects")
        return []
    typed_items = [item for item in items if isinstance(item, dict)]
    by_identity = {
        (row.collection_id, row.position, row.subject_id): row for row in catalog_rows
    }
    actual_keys: list[tuple[str, int, str]] = []
    for item in typed_items:
        collection_id = item.get("collection_id")
        position = item.get("position")
        subject_id = item.get("subject_id")
        if (
            not isinstance(collection_id, str)
            or not isinstance(position, int)
            or isinstance(position, bool)
            or not isinstance(subject_id, str)
        ):
            failures.append(
                "batch item has malformed collection/position/subject identity"
            )
            actual_keys.append(("", -1, ""))
        else:
            actual_keys.append((collection_id, position, subject_id))
    expected_keys = [
        (row.collection_id, row.position, row.subject_id) for row in catalog_rows
    ]
    if allow_incomplete:
        _append(
            bool(actual_keys),
            "--allow-incomplete still requires at least one item",
            failures,
        )
        expected_index = {key: index for index, key in enumerate(expected_keys)}
        indices = [expected_index.get(key, -1) for key in actual_keys]
        _append(
            all(index >= 0 for index in indices),
            "pilot report contains an item outside the frozen catalog",
            failures,
        )
        _append(
            indices == sorted(set(indices)),
            "pilot report is reordered or duplicates an item",
            failures,
        )
    else:
        _append(
            actual_keys == expected_keys,
            "release must contain the exact 50 catalog rows in frozen order",
            failures,
        )
        _append(
            report.get("item_count") == 50,
            "batch item_count must be exactly 50",
            failures,
        )
        _append(
            report.get("limit") is None,
            "default release must not declare a batch limit",
            failures,
        )
    pairs: list[tuple[RankedRow, dict[str, Any]]] = []
    for key, item in zip(actual_keys, typed_items, strict=True):
        row = by_identity.get(key)  # type: ignore[arg-type]
        if row is None:
            continue
        prefix = row.subject_id
        _append(
            item.get("status") == "completed",
            f"{prefix}: batch item is not completed",
            failures,
        )
        for field, expected in (
            ("subject_name", row.institution_name),
            ("subject_kind", "university"),
            ("map_purpose", "campus"),
            ("rank", row.rank),
            ("rank_number", row.rank_number),
            ("tied", row.tied),
            ("edition", row.edition),
            ("ranking_name", row.institution_name),
            ("visible_title", row.title),
        ):
            _append(
                item.get(field) == expected,
                f"{prefix}: report {field} is not catalog-bound",
                failures,
            )
        if row.score is not None:
            _append(
                _same_number(item.get("score"), row.score),
                f"{prefix}: report score drift",
                failures,
            )
        pairs.append((row, item))
    return pairs


def _tar_contract(archive: Path, failures: list[str]) -> dict[str, Any] | None:
    _append(
        archive.is_file(), f"frozen renderer archive is missing: {archive}", failures
    )
    if not archive.is_file():
        return None
    _append(
        _sha256(archive) == EXPECTED_RENDERER_ARCHIVE_SHA256,
        "renderer archive fingerprint drift",
        failures,
    )
    try:
        with tarfile.open(archive, "r:*") as bundle:
            members = {member.name: member for member in bundle.getmembers()}
            payloads: dict[str, bytes] = {}
            for name, expected_sha in (
                (STYLE_TAR_MEMBER, EXPECTED_STYLE_SHA256),
                (FORMAT_TAR_MEMBER, EXPECTED_FORMAT_SHA256),
            ):
                member = members.get(name)
                if member is None or not member.isfile():
                    failures.append(f"renderer archive lacks regular member {name}")
                    continue
                stream = bundle.extractfile(member)
                if stream is None:
                    failures.append(f"renderer archive member cannot be read: {name}")
                    continue
                payload = stream.read()
                _append(
                    hashlib.sha256(payload).hexdigest() == expected_sha,
                    f"frozen member changed: {name}",
                    failures,
                )
                payloads[name] = payload
    except (OSError, tarfile.TarError) as exc:
        failures.append(f"renderer archive is unreadable: {exc}")
        return None
    raw = payloads.get(FORMAT_TAR_MEMBER)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        failures.append(f"frozen format member is invalid JSON: {exc}")
        return None
    if not isinstance(spec, dict):
        failures.append("frozen format member is not an object")
        return None
    return spec


def inspect_png(path: Path) -> dict[str, Any]:
    """Parse the complete PNG stream, validating every CRC and opacity."""

    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature")
    position = 8
    chunks: list[tuple[bytes, bytes]] = []
    seen_iend = False
    while position < len(raw):
        if position + 12 > len(raw):
            raise ValueError("truncated PNG chunk framing")
        length = struct.unpack(">I", raw[position : position + 4])[0]
        end = position + 12 + length
        if end > len(raw):
            raise ValueError("truncated PNG chunk payload")
        kind = raw[position + 4 : position + 8]
        payload = raw[position + 8 : position + 8 + length]
        if any(
            not (ord("A") <= byte <= ord("Z") or ord("a") <= byte <= ord("z"))
            for byte in kind
        ) or not ord("A") <= kind[2] <= ord("Z"):
            raise ValueError("PNG contains an invalid chunk type")
        if kind[0] & 0x20 == 0 and kind not in {
            b"IHDR",
            b"PLTE",
            b"IDAT",
            b"IEND",
        }:
            raise ValueError(
                f"PNG contains unknown critical chunk {kind.decode('ascii')}"
            )
        declared_crc = struct.unpack(">I", raw[position + 8 + length : end])[0]
        actual_crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        if declared_crc != actual_crc:
            raise ValueError(f"CRC mismatch in {kind.decode('ascii', 'replace')} chunk")
        if seen_iend:
            raise ValueError("bytes/chunks follow IEND")
        chunks.append((kind, payload))
        position = end
        if kind == b"IEND":
            if payload:
                raise ValueError("IEND must be empty")
            seen_iend = True
    if not seen_iend or position != len(raw):
        raise ValueError("PNG lacks a terminal IEND")
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        raise ValueError("PNG lacks one leading IHDR")
    if sum(kind == b"IHDR" for kind, _ in chunks) != 1:
        raise ValueError("PNG has multiple IHDR chunks")
    width, height, bit_depth, colour_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", chunks[0][1])
    )
    if (
        width <= 0
        or height <= 0
        or width > 10_000
        or height > 10_000
        or width * height > 50_000_000
    ):
        raise ValueError("PNG dimensions are invalid or exceed the audit safety bound")
    if (bit_depth, compression, filtering, interlace) != (8, 0, 0, 0):
        raise ValueError(
            "PNG must be non-interlaced 8-bit standard compression/filtering"
        )
    phys = [payload for kind, payload in chunks if kind == b"pHYs"]
    if len(phys) != 1 or len(phys[0]) != 9:
        raise ValueError("PNG must contain exactly one valid pHYs chunk")
    x_ppm, y_ppm, unit = struct.unpack(">IIB", phys[0])
    idat_indexes = [
        index for index, (kind, _payload) in enumerate(chunks) if kind == b"IDAT"
    ]
    if not idat_indexes:
        raise ValueError("PNG has no IDAT payload")
    if idat_indexes != list(range(idat_indexes[0], idat_indexes[-1] + 1)):
        raise ValueError("PNG IDAT chunks are not contiguous")
    phys_index = next(
        index for index, (kind, _payload) in enumerate(chunks) if kind == b"pHYs"
    )
    if phys_index > idat_indexes[0]:
        raise ValueError("PNG pHYs must precede IDAT")
    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channels = channels_by_type.get(colour_type)
    if channels is None:
        raise ValueError(f"unsupported PNG colour type {colour_type}")
    palette_chunks = [
        (index, payload)
        for index, (kind, payload) in enumerate(chunks)
        if kind == b"PLTE"
    ]
    if len(palette_chunks) > 1:
        raise ValueError("PNG has multiple palettes")
    if colour_type in {0, 4} and palette_chunks:
        raise ValueError("grayscale PNG must not contain a palette")
    if palette_chunks and (
        not 3 <= len(palette_chunks[0][1]) <= 768
        or len(palette_chunks[0][1]) % 3
        or palette_chunks[0][0] > idat_indexes[0]
    ):
        raise ValueError("PNG contains an invalid or misplaced palette")
    palette = b""
    if colour_type == 3:
        if len(palette_chunks) != 1:
            raise ValueError("indexed PNG must contain one valid pre-IDAT palette")
        palette = palette_chunks[0][1]
    packed = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    stride = width * channels
    expected_scanline_size = height * (stride + 1)
    try:
        decompressor = zlib.decompressobj()
        scanlines = decompressor.decompress(packed, expected_scanline_size + 1)
        if decompressor.unconsumed_tail or len(scanlines) > expected_scanline_size:
            raise ValueError("PNG decompressed data exceeds its declared dimensions")
        scanlines += decompressor.flush(expected_scanline_size + 1 - len(scanlines))
    except zlib.error as exc:
        raise ValueError(f"PNG IDAT cannot be decompressed: {exc}") from exc
    if (
        len(scanlines) != expected_scanline_size
        or not decompressor.eof
        or decompressor.unused_data
    ):
        raise ValueError("PNG decompressed scanline size is inconsistent")
    transparency_chunks = [
        (index, payload)
        for index, (kind, payload) in enumerate(chunks)
        if kind == b"tRNS"
    ]
    if len(transparency_chunks) > 1:
        raise ValueError("PNG has multiple tRNS chunks")
    if transparency_chunks and transparency_chunks[0][0] > idat_indexes[0]:
        raise ValueError("PNG tRNS must precede IDAT")
    transparency = [payload for _index, payload in transparency_chunks]
    if colour_type == 0 and transparency and len(transparency[0]) != 2:
        raise ValueError("grayscale PNG tRNS must contain one sample")
    if colour_type == 2 and transparency and len(transparency[0]) != 6:
        raise ValueError("RGB PNG tRNS must contain one colour key")
    if colour_type in {4, 6} and transparency:
        raise ValueError("alpha PNG must not contain tRNS")
    if (
        colour_type == 3
        and transparency
        and (not palette_chunks or transparency_chunks[0][0] < palette_chunks[0][0])
    ):
        raise ValueError("indexed PNG tRNS must follow its palette")
    opaque = True
    previous = bytearray(stride)
    offset = 0
    palette_alpha = transparency[0] if colour_type == 3 and transparency else b""
    if colour_type == 3 and len(palette_alpha) > len(palette) // 3:
        raise ValueError("indexed PNG transparency table exceeds its palette")
    if colour_type in {0, 2} and transparency:
        # A tRNS colour key makes the image transparency-capable; the release
        # contract requires an unambiguously opaque white background.
        opaque = False
    has_nonwhite_pixel = False
    for _row in range(height):
        filter_type = scanlines[offset]
        source = scanlines[offset + 1 : offset + 1 + stride]
        offset += stride + 1
        recon = _unfilter_scanline(filter_type, source, previous, channels)
        if colour_type in {4, 6} and any(
            recon[index] != 255 for index in range(channels - 1, stride, channels)
        ):
            opaque = False
        if colour_type == 3 and any(
            index < len(palette_alpha) and palette_alpha[index] != 255
            for index in recon
        ):
            opaque = False
        if colour_type in {0, 4}:
            has_nonwhite_pixel |= any(
                recon[index] != 255 for index in range(0, stride, channels)
            )
        elif colour_type in {2, 6}:
            has_nonwhite_pixel |= any(
                any(recon[index + channel] != 255 for channel in range(3))
                for index in range(0, stride, channels)
            )
        else:
            palette_entries = [
                palette[index : index + 3] for index in range(0, len(palette), 3)
            ]
            if any(index >= len(palette_entries) for index in recon):
                raise ValueError("indexed PNG pixel refers outside its palette")
            has_nonwhite_pixel |= any(
                palette_entries[index] != b"\xff\xff\xff" for index in recon
            )
        previous = recon
    return {
        "width_px": width,
        "height_px": height,
        "x_pixels_per_metre": x_ppm,
        "y_pixels_per_metre": y_ppm,
        "physical_unit": unit,
        "opaque": opaque,
        "has_nonwhite_pixel": has_nonwhite_pixel,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _unfilter_scanline(
    filter_type: int, source: bytes, previous: bytearray, bpp: int
) -> bytearray:
    result = bytearray(len(source))
    for index, value in enumerate(source):
        left = result[index - bpp] if index >= bpp else 0
        above = previous[index]
        upper_left = previous[index - bpp] if index >= bpp else 0
        if filter_type == 0:
            prediction = 0
        elif filter_type == 1:
            prediction = left
        elif filter_type == 2:
            prediction = above
        elif filter_type == 3:
            prediction = (left + above) // 2
        elif filter_type == 4:
            p = left + above - upper_left
            distances = (abs(p - left), abs(p - above), abs(p - upper_left))
            prediction = (left, above, upper_left)[distances.index(min(distances))]
        else:
            raise ValueError(f"unsupported PNG filter type {filter_type}")
        result[index] = (value + prediction) & 0xFF
    return result


def _physical_groups(root: ET.Element) -> dict[int, list[ET.Element]]:
    result: dict[int, list[ET.Element]] = defaultdict(list)
    for group in root.findall(f"{{{SVG_NS}}}g"):
        raw_step = group.get("data-pen-step")
        if raw_step is None:
            continue
        try:
            step = int(raw_step)
        except ValueError:
            step = -1
        result[step].append(group)
    return dict(result)


def _all_paths_are_physically_assigned(root: ET.Element) -> bool:
    top_groups = root.findall(f"{{{SVG_NS}}}g")
    all_paths = root.findall(f".//{{{SVG_NS}}}path")
    assigned_paths = [
        path
        for group in top_groups
        if group.get("data-pen-step") is not None
        for path in group.findall(f".//{{{SVG_NS}}}path")
    ]
    return len(assigned_paths) == len(all_paths) and {
        id(path) for path in assigned_paths
    } == {id(path) for path in all_paths}


def _group_signature(groups: Iterable[ET.Element]) -> str:
    digest = hashlib.sha256()

    def update(element: ET.Element) -> None:
        tag = element.tag.encode("utf-8")
        digest.update(len(tag).to_bytes(8, "big"))
        digest.update(tag)
        for key, value in sorted(element.attrib.items()):
            encoded = f"{key}\0{value}".encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        text = element.text or ""
        if text.isspace():
            text = ""
        encoded_text = text.encode("utf-8")
        digest.update(len(encoded_text).to_bytes(8, "big"))
        digest.update(encoded_text)
        children = list(element)
        digest.update(len(children).to_bytes(8, "big"))
        for child in children:
            update(child)

    for group in groups:
        # ElementTree includes an element's tail in ``tostring``.  The same
        # selected group has different indentation at the end of a full SVG
        # and a one-pen SVG, so tails are deliberately outside the signature.
        update(group)
        digest.update(b"\0")
    return digest.hexdigest()


def split_parity_failures(
    master: ET.Element, split: ET.Element, step: int
) -> list[str]:
    failures: list[str] = []
    master_groups = _physical_groups(master).get(step, [])
    split_groups = _physical_groups(split)
    raw_status = split.get(f"{{{MAP_NS}}}pen-slot-status")
    slot_status = raw_status or "active"
    _append(
        slot_status in {"active", "empty"},
        f"split step {step} has invalid pen-slot status {slot_status!r}",
        failures,
    )
    if slot_status == "empty":
        _append(
            not master_groups,
            f"empty split step {step} has physical groups in master",
            failures,
        )
        _append(
            not split_groups,
            f"empty split step {step} contains physical groups",
            failures,
        )
        _append(
            not split.findall(f".//{{{SVG_NS}}}path"),
            f"empty split step {step} contains drawable paths",
            failures,
        )
        return failures
    _append(
        set(split_groups) == {step},
        f"split step {step} includes another/missing pen step",
        failures,
    )
    actual = split_groups.get(step, [])
    _append(bool(master_groups), f"master has no groups for pen step {step}", failures)
    _append(
        len(actual) == len(master_groups),
        f"split step {step} physical group count drift",
        failures,
    )
    if actual and master_groups:
        _append(
            _group_signature(actual) == _group_signature(master_groups),
            f"split step {step} geometry/metadata drift",
            failures,
        )
    return failures


def read_checksums(path: Path, root: Path, failures: list[str]) -> dict[str, str]:
    records: dict[str, str] = {}
    resolved_targets: set[Path] = set()
    if not path.is_file():
        failures.append("CHECKSUMS.sha256 is missing")
        return records
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        failures.append(f"CHECKSUMS.sha256 is unreadable: {exc}")
        return records
    for line_number, line in enumerate(lines, 1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None:
            failures.append(f"CHECKSUMS.sha256:{line_number}: malformed record")
            continue
        digest, relative = match.groups()
        relative_path = Path(relative)
        canonical = relative_path.as_posix()
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative != canonical
            or canonical in records
        ):
            failures.append(
                f"CHECKSUMS.sha256:{line_number}: unsafe/duplicate path {relative!r}"
            )
            continue
        target = (root / relative_path).resolve()
        if not _within(target, root):
            failures.append(
                f"CHECKSUMS.sha256:{line_number}: path escapes release root"
            )
            continue
        if target in resolved_targets:
            failures.append(
                f"CHECKSUMS.sha256:{line_number}: duplicate resolved target {relative!r}"
            )
            continue
        resolved_targets.add(target)
        records[canonical] = digest
        if not target.is_file():
            failures.append(f"checksum target is missing: {relative}")
        elif _sha256(target) != digest:
            failures.append(f"checksum mismatch: {relative}")
    return records


def _checksum_covers(
    path: Path, root: Path, checksums: dict[str, str], failures: list[str]
) -> None:
    if not _within(path, root):
        failures.append(f"required artifact escapes release root: {path}")
        return
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    if relative not in checksums:
        failures.append(
            f"required artifact is absent from CHECKSUMS.sha256: {relative}"
        )


def _json_path_leaks(value: object, root: Path, label: str) -> list[str]:
    failures: list[str] = []

    def visit(item: object, pointer: str) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                visit(nested, f"{pointer}.{key}")
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                visit(nested, f"{pointer}[{index}]")
        elif isinstance(item, str):
            lowered = item.lower()
            if "/tmp/" in lowered or "/var/tmp/" in lowered:
                failures.append(f"{label}{pointer}: temporary path leaked: {item}")
                return
            if lowered.startswith("file:"):
                raw = item[5:]
                path = Path(raw)
                path = path if path.is_absolute() else root / path
                try:
                    resolved = path.resolve()
                except (OSError, RuntimeError):
                    failures.append(f"{label}{pointer}: unresolvable file URI")
                    return
                if not _within(resolved, root):
                    failures.append(
                        f"{label}{pointer}: foreign absolute path leaked: {item}"
                    )
                return
            if "://" in item or item.startswith("urn:"):
                return
            # Only strings that look like filesystem paths are path-gated.
            if item.startswith("/"):
                try:
                    path = Path(item).resolve()
                except (OSError, RuntimeError):
                    failures.append(f"{label}{pointer}: unresolvable absolute path")
                    return
                if not _within(path, root):
                    failures.append(
                        f"{label}{pointer}: foreign absolute path leaked: {item}"
                    )

    visit(value, "")
    return failures


def _svg_path_leaks(document: ET.Element, root: Path, label: str) -> list[str]:
    values: list[str] = []
    for element in document.iter():
        values.extend(element.attrib.values())
        if element.text:
            values.append(element.text)
        if element.tail:
            values.append(element.tail)
    return _json_path_leaks(values, root, label)


def _svg_active_content_failures(document: ET.Element, label: str) -> list[str]:
    failures: list[str] = []
    for element in document.iter():
        for raw_name, value in element.attrib.items():
            name = raw_name.rsplit("}", 1)[-1].casefold()
            lowered = value.casefold()
            if (
                name == "href"
                or name.startswith("on")
                or "javascript:" in lowered
                or "data:text/html" in lowered
                or "url(" in lowered
            ):
                failures.append(
                    f"{label}: active/external SVG attribute is forbidden: {name}"
                )
    return failures


def _canonical_json_sha256(path: Path) -> str:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _pinned_item_dependency_failures(
    dependencies: object,
    export_argv: object,
    *,
    root: Path,
    entry: PinnedSourceEntry | None,
    subject_id: str,
) -> list[str]:
    failures: list[str] = []
    valid = (
        isinstance(dependencies, list)
        and len(dependencies) == 2
        and all(isinstance(record, dict) for record in dependencies)
        and entry is not None
    )
    style = dependencies[0] if valid else None
    source = dependencies[1] if valid else None
    valid = (
        valid
        and isinstance(style, dict)
        and style.get("option") == "--style"
        and style.get("sha256") == EXPECTED_STYLE_SHA256
        and isinstance(source, dict)
        and source
        == {
            "option": "--input-json",
            "path": str(entry.path),
            "size_bytes": entry.size_bytes,
            "sha256": entry.sha256,
        }
    )
    if valid and isinstance(style, dict):
        try:
            style_path = _resolve_release_path(
                style.get("path"), root, f"{subject_id} item style dependency"
            )
            valid = (
                style_path.is_file()
                and _sha256(style_path) == EXPECTED_STYLE_SHA256
                and style.get("size_bytes") == style_path.stat().st_size
            )
        except AuditInputError as exc:
            failures.append(str(exc))
            valid = False
    _append(
        valid,
        f"{subject_id}: item style/pinned-JSON dependency binding drift",
        failures,
    )
    _append(
        isinstance(export_argv, list)
        and entry is not None
        and sum(str(value) == "--input-json" for value in export_argv) == 1
        and _option_value(export_argv, "--input-json") == str(entry.path)
        and _option_value(export_argv, "--input-pbf") is None,
        f"{subject_id}: item invocation is not bound to its pinned JSON",
        failures,
    )
    return failures


def _audit_svg_and_manifest(
    row: RankedRow,
    item: dict[str, Any],
    root_dir: Path,
    frozen_spec: dict[str, Any] | None,
    release_mode: str,
    expected_renderer_sha256: str | None,
    expected_source_cohort: dict[str, Any] | None,
    expected_source_entry: PinnedSourceEntry | None,
) -> tuple[dict[str, Any], set[Path], list[str], list[str], Path | None]:
    failures: list[str] = []
    advisories: list[str] = []
    artifacts: set[Path] = set()
    collection_dir = root_dir / row.collection_id
    expected_svg = collection_dir / f"{row.stem}.svg"
    expected_manifest = collection_dir / f"{row.stem}.plot.json"
    expected_png = collection_dir / f"{row.stem}.png"
    for key, expected in (
        ("output", expected_svg),
        ("manifest", expected_manifest),
        ("png", expected_png),
    ):
        try:
            declared = _resolve_release_path(
                item.get(key), root_dir, f"{row.subject_id} {key}"
            )
        except AuditInputError as exc:
            failures.append(str(exc))
            continue
        _append(
            declared == expected.resolve(),
            f"{row.subject_id}: {key} filename/path drift",
            failures,
        )
    for path in (expected_svg, expected_manifest, expected_png):
        artifacts.add(path)
        _append(path.is_file(), f"{row.subject_id}: missing {path.name}", failures)
    if not expected_svg.is_file() or not expected_manifest.is_file():
        return (
            {"subject_id": row.subject_id, "passed": False},
            artifacts,
            failures,
            advisories,
            None,
        )
    try:
        manifest = _json(expected_manifest)
    except AuditInputError as exc:
        failures.append(str(exc))
        return (
            {"subject_id": row.subject_id, "passed": False},
            artifacts,
            failures,
            advisories,
            None,
        )
    try:
        svg_root = ET.parse(expected_svg).getroot()
    except (OSError, ET.ParseError) as exc:
        failures.append(f"{row.subject_id}: invalid master SVG: {exc}")
        return (
            {"subject_id": row.subject_id, "passed": False},
            artifacts,
            failures,
            advisories,
            None,
        )
    failures.extend(_json_path_leaks(manifest, root_dir, f"{row.subject_id}.manifest"))
    failures.extend(_svg_path_leaks(svg_root, root_dir, f"{row.subject_id}.svg"))
    failures.extend(_svg_active_content_failures(svg_root, f"{row.subject_id}.svg"))
    _append(
        _extent_matches(manifest.get("extent_wgs84"), row),
        f"{row.subject_id}: manifest extent is not the exact 2 km A5 contain-fit campus extent",
        failures,
    )
    metadata_nodes = svg_root.findall(f"{{{SVG_NS}}}metadata")
    svg_metadata: dict[str, Any] | None = None
    if len(metadata_nodes) == 1 and metadata_nodes[0].text:
        try:
            candidate = json.loads(metadata_nodes[0].text)
            if isinstance(candidate, dict):
                svg_metadata = candidate
        except json.JSONDecodeError:
            pass
    _append(
        svg_metadata is not None
        and _extent_matches(svg_metadata.get("extent_wgs84"), row),
        f"{row.subject_id}: SVG metadata extent is missing or wrong",
        failures,
    )
    if svg_metadata is not None:
        failures.extend(
            _json_path_leaks(svg_metadata, root_dir, f"{row.subject_id}.svg-metadata")
        )
    for key, path in (
        ("svg_sha256", expected_svg),
        ("manifest_sha256", expected_manifest),
        ("png_sha256", expected_png),
    ):
        _append(
            path.is_file() and item.get(key) == _sha256(path),
            f"{row.subject_id}: batch {key} drift",
            failures,
        )

    _append(
        manifest.get("title") == row.title,
        f"{row.subject_id}: manifest title must be city/locality {row.title!r}",
        failures,
    )
    svg_title = (svg_root.findtext(f"{{{SVG_NS}}}title") or "").strip()
    _append(
        svg_title == row.title,
        f"{row.subject_id}: SVG title must be city/locality {row.title!r}",
        failures,
    )
    _append(
        svg_root.get("width") == "148mm", f"{row.subject_id}: SVG width drift", failures
    )
    _append(
        svg_root.get("height") == "210mm",
        f"{row.subject_id}: SVG height drift",
        failures,
    )
    _append(
        svg_root.get("viewBox") == "0 0 148 210",
        f"{row.subject_id}: SVG viewBox drift",
        failures,
    )
    local_tags = [element.tag.rsplit("}", 1)[-1] for element in svg_root.iter()]
    forbidden = sorted(set(local_tags) & FORBIDDEN_DRAWABLES)
    _append(
        not forbidden,
        f"{row.subject_id}: forbidden non-path SVG elements: {forbidden}",
        failures,
    )
    unexpected_elements = sorted(set(local_tags) - ALLOWED_SVG_ELEMENTS)
    _append(
        not unexpected_elements,
        f"{row.subject_id}: unsupported SVG elements: {unexpected_elements}",
        failures,
    )
    _append(
        not any("style" in element.attrib for element in svg_root.iter()),
        f"{row.subject_id}: inline CSS style attributes are forbidden",
        failures,
    )
    _append(
        bool(svg_root.findall(f".//{{{SVG_NS}}}path")),
        f"{row.subject_id}: SVG has no paths",
        failures,
    )

    page = manifest.get("page")
    rendering = manifest.get("rendering")
    memorabilia = manifest.get("memorabilia")
    _append(isinstance(page, dict), f"{row.subject_id}: page record missing", failures)
    _append(
        isinstance(rendering, dict),
        f"{row.subject_id}: rendering record missing",
        failures,
    )
    _append(
        isinstance(memorabilia, dict),
        f"{row.subject_id}: memorabilia record missing",
        failures,
    )
    if isinstance(page, dict):
        _append(
            page.get("paper") == "A5" and page.get("orientation") == "portrait",
            f"{row.subject_id}: page is not A5 portrait",
            failures,
        )
        _append(
            _same_number(page.get("width_mm"), 148.0)
            and _same_number(page.get("height_mm"), 210.0),
            f"{row.subject_id}: page dimensions drift",
            failures,
        )
        bounds = page.get("map_bounds_mm")
        _append(
            isinstance(bounds, dict)
            and all(
                _same_number(bounds.get(key), expected, 0.001)
                for key, expected in EXPECTED_MAP_BOUNDS.items()
            ),
            f"{row.subject_id}: A5 v2.1 map bounds drift",
            failures,
        )
    if isinstance(rendering, dict):
        rendering_expected = {
            "preset": "a5-balanced-poster",
            "poster_layout": "university-memorabilia",
            "detail_profile": "plotter-faithful",
            "water_fill": "dots",
            "landmark_buildings": True,
            "road_style": "centreline",
            "extent_fit": "contain",
            "travel_optimisation_enabled": True,
            "physical_conflict_audit_enabled": True,
            "repeat_passes_explicitly_approved": False,
            "visible_attribution": False,
            "attribution_mode": "external",
            "external_attribution_placement": EXPECTED_EXTERNAL_ATTRIBUTION,
            "scale_bar": False,
            "north_mark": True,
            "stock_tone": "light",
            "pen_profile": "actual-pens",
        }
        for key, value in rendering_expected.items():
            _append(
                rendering.get(key) == value,
                f"{row.subject_id}: rendering {key} drift",
                failures,
            )
        _append(
            _same_number(rendering.get("simplify_tolerance_mm"), 0.04),
            f"{row.subject_id}: simplify tolerance drift",
            failures,
        )
    _append(
        manifest.get("families") == EXPECTED_FAMILIES,
        f"{row.subject_id}: family selection/order drift",
        failures,
    )
    if isinstance(memorabilia, dict):
        _append(
            memorabilia.get("layout") == "university-memorabilia",
            f"{row.subject_id}: memorabilia layout drift",
            failures,
        )
        _append(
            memorabilia.get("coordinates") == row.coordinate,
            f"{row.subject_id}: coordinate footer drift",
            failures,
        )
        _append(
            memorabilia.get("personalisation")
            == {"person_name": "", "degree": "", "honours": "", "years": ""},
            f"{row.subject_id}: personalisation fields must remain blank",
            failures,
        )
        _append(
            memorabilia.get("blank_template") is True,
            f"{row.subject_id}: template is not marked blank",
            failures,
        )

    source = manifest.get("source")
    source_path: Path | None = None
    if not isinstance(source, dict):
        failures.append(f"{row.subject_id}: source record missing")
    else:
        _append(
            source.get("provider") == "OpenStreetMap contributors",
            f"{row.subject_id}: OSM provider drift",
            failures,
        )
        _append(
            source.get("license") == "ODbL 1.0",
            f"{row.subject_id}: ODbL licence drift",
            failures,
        )
        _append(
            "openstreetmap.org/copyright" in str(source.get("attribution", "")),
            f"{row.subject_id}: OSM attribution URL missing",
            failures,
        )
        if expected_source_entry is None:
            failures.append(f"{row.subject_id}: pinned source entry is missing")
        else:
            _append(
                source.get("endpoint") == f"file:{expected_source_entry.path}",
                f"{row.subject_id}: manifest source endpoint drifts from its pinned snapshot",
                failures,
            )
            _append(
                source.get("timestamp") == expected_source_entry.osm_base_timestamp
                and source.get("from_cache") is True,
                f"{row.subject_id}: pinned source timestamp/cache marker drift",
                failures,
            )
            try:
                source_path = _resolve_pinned_source_path(
                    source.get("cache_path"),
                    root_dir,
                    expected_source_entry.path,
                    f"{row.subject_id} pinned source",
                )
            except AuditInputError as exc:
                failures.append(str(exc))
        provenance = source.get("provenance")
        if not isinstance(provenance, dict):
            failures.append(f"{row.subject_id}: source provenance missing")
        elif source_path is not None and expected_source_entry is not None:
            artifacts.add(source_path)
            _append(
                source_path.is_file(),
                f"{row.subject_id}: cached source file missing",
                failures,
            )
            digest = provenance.get("source_file_sha256")
            _append(
                digest == expected_source_entry.sha256,
                f"{row.subject_id}: pinned source SHA-256 drift",
                failures,
            )
            if source_path.is_file():
                _append(
                    source_path.stat().st_size == expected_source_entry.size_bytes
                    and _sha256(source_path) == expected_source_entry.sha256,
                    f"{row.subject_id}: pinned source bytes mismatch",
                    failures,
                )
                canonical_digest = provenance.get("canonical_source_data_sha256")
                _append(
                    canonical_digest == expected_source_entry.canonical_json_sha256,
                    f"{row.subject_id}: canonical pinned source SHA-256 drift",
                    failures,
                )
                try:
                    _append(
                        _canonical_json_sha256(source_path)
                        == expected_source_entry.canonical_json_sha256,
                        f"{row.subject_id}: canonical pinned source digest drift",
                        failures,
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    failures.append(
                        f"{row.subject_id}: pinned source is not readable JSON: {exc}"
                    )
            _append(
                provenance.get("acquisition_mode") == "pinned-json"
                and provenance.get("source_pinned") is True,
                f"{row.subject_id}: provenance contradicts its pinned JSON cohort",
                failures,
            )
    readiness = manifest.get("production_readiness")
    _append(
        isinstance(readiness, dict),
        f"{row.subject_id}: production readiness missing",
        failures,
    )
    if isinstance(readiness, dict):
        _append(
            readiness.get("mode") == "review-only"
            and readiness.get("production_ready") is False,
            f"{row.subject_id}: pinned-JSON plate must remain explicitly review-only",
            failures,
        )

    contract = item.get("artifact_contract")
    contract_digest = item.get("artifact_contract_sha256")
    _append(
        isinstance(contract, dict)
        and isinstance(contract_digest, str)
        and contract_digest == _stable_digest(contract),
        f"{row.subject_id}: batch artifact contract digest drift",
        failures,
    )
    binding = manifest.get("batch_artifact_contract")
    _append(
        manifest.get("batch_source_cohort") == expected_source_cohort,
        f"{row.subject_id}: manifest batch source-cohort binding drift",
        failures,
    )
    if isinstance(contract, dict) and isinstance(contract_digest, str):
        _append(
            contract.get("renderer_fingerprint_sha256") == expected_renderer_sha256,
            f"{row.subject_id}: item contract renderer fingerprint drift",
            failures,
        )
        _append(
            contract.get("source_cohort") == expected_source_cohort
            and contract.get("source_cohort_sha256")
            == (
                expected_source_cohort.get("sha256")
                if isinstance(expected_source_cohort, dict)
                else None
            ),
            f"{row.subject_id}: item contract source-cohort drift",
            failures,
        )
        failures.extend(
            _pinned_item_dependency_failures(
                contract.get("dependency_fingerprints"),
                item.get("export_argv"),
                root=root_dir,
                entry=expected_source_entry,
                subject_id=row.subject_id,
            )
        )
        _append(
            contract.get("artifacts")
            == {
                "svg": str(expected_svg.resolve()),
                "manifest": str(expected_manifest.resolve()),
                "png": str(expected_png.resolve()),
            },
            f"{row.subject_id}: artifact path contract drift",
            failures,
        )
        expected_binding = {
            "schema_version": 3,
            "sha256": contract_digest,
            "collection_id": row.collection_id,
            "position": row.position,
            "subject_id": row.subject_id,
            "source_cohort_sha256": contract.get("source_cohort_sha256"),
        }
        _append(
            binding == expected_binding,
            f"{row.subject_id}: manifest batch binding drift",
            failures,
        )
        _append(
            svg_root.get("data-batch-contract-sha256") == contract_digest,
            f"{row.subject_id}: SVG batch binding drift",
            failures,
        )
        contract_identity = contract.get("identity")
        expected_contract_identity = {
            "collection_id": row.collection_id,
            "position": row.position,
            "subject_id": row.subject_id,
            "subject_name": row.institution_name,
            "subject_kind": "university",
            "map_purpose": "campus",
            "title": row.title,
            "visible_title": row.title,
            "rank": row.rank,
            "rank_number": row.rank_number,
            "tied": row.tied,
            "edition": row.edition,
            "ranking_name": row.institution_name,
            **({"score": row.score} if row.score is not None else {}),
        }
        _append(
            isinstance(contract_identity, dict)
            and all(
                contract_identity.get(key) == value
                for key, value in expected_contract_identity.items()
            ),
            f"{row.subject_id}: artifact contract identity/title drift",
            failures,
        )
        _append(
            contract.get("families") == EXPECTED_FAMILIES,
            f"{row.subject_id}: artifact contract families drift",
            failures,
        )
        contract_rendering = contract.get("rendering")
        expected_contract_rendering = {
            "preset": "a5-balanced-poster",
            "detail_profile": "plotter-faithful",
            "road_style": "centreline",
            "simplify_tolerance_mm": 0.04,
            "extent_fit": "contain",
            "travel_optimisation_enabled": True,
            "visible_attribution": False,
            "attribution_mode": "external",
            "external_attribution_placement": EXPECTED_EXTERNAL_ATTRIBUTION,
            "scale_bar": False,
            "scale_detail": False,
            "north_mark": True,
            "production_requested": False,
        }
        _append(
            isinstance(contract_rendering, dict)
            and all(
                contract_rendering.get(key) == value
                for key, value in expected_contract_rendering.items()
            ),
            f"{row.subject_id}: artifact rendering contract drift",
            failures,
        )
        contract_details = contract.get("details")
        _append(
            isinstance(contract_details, dict)
            and contract_details.get("purpose") == "UNIVERSITY CAMPUS"
            and contract_details.get("coordinate") == row.coordinate,
            f"{row.subject_id}: artifact footer contract drift",
            failures,
        )
        _append(
            contract.get("memorabilia")
            == {
                "layout": "university-memorabilia",
                "coordinates": row.coordinate,
                "personalisation": {
                    "person_name": "",
                    "degree": "",
                    "honours": "",
                    "years": "",
                },
                "blank_template": True,
            },
            f"{row.subject_id}: artifact memorabilia contract drift",
            failures,
        )
        contract_page = contract.get("page")
        contract_bounds = (
            contract_page.get("map_bounds_mm")
            if isinstance(contract_page, dict)
            else None
        )
        _append(
            isinstance(contract_page, dict)
            and contract_page.get("paper") == "A5"
            and contract_page.get("orientation") == "portrait"
            and _same_number(contract_page.get("width_mm"), 148.0)
            and _same_number(contract_page.get("height_mm"), 210.0)
            and isinstance(contract_bounds, dict)
            and all(
                _same_number(contract_bounds.get(key), value, 0.001)
                for key, value in EXPECTED_MAP_BOUNDS.items()
            ),
            f"{row.subject_id}: artifact A5 page contract drift",
            failures,
        )
        contract_raster = contract.get("raster")
        _append(
            isinstance(contract_raster, dict)
            and contract_raster.get("format") == "PNG"
            and _same_number(contract_raster.get("dpi"), 254.0)
            and contract_raster.get("width_px") == 1480
            and contract_raster.get("height_px") == 2100
            and contract_raster.get("background") == "opaque white",
            f"{row.subject_id}: artifact raster contract drift",
            failures,
        )
        contract_extent = contract.get("extent_wgs84")
        _append(
            _extent_matches(contract_extent, row),
            f"{row.subject_id}: artifact extent is not the exact 2 km A5 contain-fit campus extent",
            failures,
        )

    groups = _physical_groups(svg_root)
    top_groups = svg_root.findall(f"{{{SVG_NS}}}g")
    _append(
        _all_paths_are_physically_assigned(svg_root),
        f"{row.subject_id}: a path exists outside a physical pen layer",
        failures,
    )
    _append(
        all(group.get("data-pen-step") is not None for group in top_groups),
        f"{row.subject_id}: a top-level group is not assigned to a pen step",
        failures,
    )
    observed_steps = [
        int(group.get("data-pen-step", "-1"))
        for group in svg_root.findall(f"{{{SVG_NS}}}g")
        if group.get("data-pen-step") is not None
        and str(group.get("data-pen-step", "")).lstrip("-").isdigit()
    ]
    contiguous_steps = [
        step
        for index, step in enumerate(observed_steps)
        if index == 0 or step != observed_steps[index - 1]
    ]
    sequence = manifest.get("pen_sequence")
    _append(
        isinstance(sequence, list) and len(sequence) == 10,
        f"{row.subject_id}: manifest must declare ten inventory pen slots",
        failures,
    )
    sequence_by_step: dict[int, dict[str, Any]] = {}
    active_steps: list[int] = []
    if isinstance(sequence, list):
        for expected_pen, step_record in zip(PENS, sequence, strict=False):
            if not isinstance(step_record, dict):
                failures.append(
                    f"{row.subject_id}: malformed pen step {expected_pen.step}"
                )
                continue
            sequence_by_step[expected_pen.step] = step_record
            logical = (
                set(step_record.get("layers", []))
                if isinstance(step_record.get("layers"), list)
                else set()
            )
            configured = (
                set(step_record.get("configured_layers", []))
                if isinstance(step_record.get("configured_layers"), list)
                else set()
            )
            omitted = (
                set(step_record.get("omitted_layers", []))
                if isinstance(step_record.get("omitted_layers"), list)
                else set()
            )
            empty = not logical
            slot_status = "empty" if empty else "active"
            if not empty:
                active_steps.append(expected_pen.step)
            _append(
                isinstance(step_record.get("layers"), list)
                and len(step_record["layers"]) == len(logical),
                f"{row.subject_id}: malformed logical layers at slot {expected_pen.step}",
                failures,
            )
            _append(
                step_record.get("step") == expected_pen.step,
                f"{row.subject_id}: pen step order drift",
                failures,
            )
            _append(
                step_record.get("pen_id") == expected_pen.pen_id
                and step_record.get("ink") == expected_pen.ink,
                f"{row.subject_id}: physical pen identity drift at step {expected_pen.step}",
                failures,
            )
            _append(
                _same_number(step_record.get("nib_mm"), expected_pen.nib_mm)
                and _same_number(
                    step_record.get("nominal_nib_mm"), expected_pen.nib_mm
                ),
                f"{row.subject_id}: nib drift at step {expected_pen.step}",
                failures,
            )
            _append(
                step_record.get("pen_profile") == "actual-pens",
                f"{row.subject_id}: nonphysical pen profile",
                failures,
            )
            _append(
                step_record.get("strokes") == 1 and step_record.get("passes") == 1,
                f"{row.subject_id}: repeated/offset drawing at step {expected_pen.step}",
                failures,
            )
            _append(
                isinstance(step_record.get("configured_layers"), list)
                and configured == set(expected_pen.allowed_layers)
                and len(step_record["configured_layers"]) == len(configured),
                f"{row.subject_id}: configured layers drift at slot {expected_pen.step}",
                failures,
            )
            _append(
                isinstance(step_record.get("omitted_layers"), list)
                and omitted == configured - logical
                and len(step_record["omitted_layers"]) == len(omitted),
                f"{row.subject_id}: omitted layers drift at slot {expected_pen.step}",
                failures,
            )
            _append(
                step_record.get("empty") is empty
                and step_record.get("slot_status") == slot_status,
                f"{row.subject_id}: slot activity drift at step {expected_pen.step}",
                failures,
            )
            layer_ok = not logical or (
                logical == set(expected_pen.allowed_layers)
                if expected_pen.exact_layers
                else logical <= set(expected_pen.allowed_layers)
            )
            _append(
                layer_ok,
                f"{row.subject_id}: logical layers drift at step {expected_pen.step}: {sorted(logical)}",
                failures,
            )
            svg_groups = groups.get(expected_pen.step, [])
            svg_layers = {
                str(group.get("id", "")).removeprefix("layer-") for group in svg_groups
            }
            _append(
                svg_layers == logical,
                f"{row.subject_id}: SVG/manifest layer drift at step {expected_pen.step}",
                failures,
            )
            if empty:
                zero_metrics = (
                    "path_count",
                    "pen_down_distance_mm",
                    "minimum_plot_seconds",
                    "pen_up_travel_mm",
                    "estimated_plot_seconds_including_pen_up",
                )
                _append(
                    isinstance(step_record.get("path_count"), int)
                    and all(
                        _same_number(step_record.get(key), 0.0) for key in zero_metrics
                    )
                    and step_record.get("layer_settings") == [],
                    f"{row.subject_id}: empty slot {expected_pen.step} has drawing work",
                    failures,
                )
            else:
                _append(
                    isinstance(step_record.get("path_count"), int)
                    and int(step_record["path_count"]) > 0,
                    f"{row.subject_id}: active slot {expected_pen.step} has no paths",
                    failures,
                )
            for group in svg_groups:
                logical_id = str(group.get("id", "")).removeprefix("layer-")
                _append(
                    group.get("data-plot-pen-id") == expected_pen.pen_id,
                    f"{row.subject_id}: SVG pen ID drift at step {expected_pen.step}",
                    failures,
                )
                _append(
                    group.get("stroke") == EXPECTED_LAYER_COLORS.get(logical_id),
                    f"{row.subject_id}: frozen palette drift in {logical_id}",
                    failures,
                )
                _append(
                    group.get("fill") == "none"
                    and group.get("stroke-linecap")
                    == ("butt" if logical_id in {"frame", "poster_border"} else "round")
                    and group.get("stroke-linejoin")
                    == (
                        "miter" if logical_id in {"frame", "poster_border"} else "round"
                    )
                    and group.get("stroke-width") == f"{expected_pen.nib_mm:g}"
                    and group.get("stroke-dasharray") is None
                    and group.get("opacity") is None,
                    f"{row.subject_id}: physical stroke rendering drift in {logical_id}",
                    failures,
                )
                _append(
                    all(
                        path.get("stroke") is None
                        and path.get("fill") is None
                        and path.get("style") is None
                        for path in group.findall(f".//{{{SVG_NS}}}path")
                    ),
                    f"{row.subject_id}: path-level colour/fill override in {logical_id}",
                    failures,
                )
                _append(
                    group.get("data-plot-pen-profile") == "actual-pens",
                    f"{row.subject_id}: SVG pen profile drift",
                    failures,
                )
                _append(
                    group.get("data-plot-strokes") == "1"
                    and group.get("data-plot-passes") == "1",
                    f"{row.subject_id}: SVG declares retracing/offsets",
                    failures,
                )
                try:
                    group_nib = float(group.get("data-plot-nib-mm", "nan"))
                except ValueError:
                    group_nib = float("nan")
                _append(
                    isfinite(group_nib)
                    and abs(group_nib - expected_pen.nib_mm) <= 1e-6,
                    f"{row.subject_id}: SVG nib drift",
                    failures,
                )

    _append(
        sorted(groups) == active_steps and contiguous_steps == active_steps,
        f"{row.subject_id}: physical SVG steps do not match active inventory slots",
        failures,
    )
    plot_summary = manifest.get("plot_summary")
    _append(
        isinstance(plot_summary, dict)
        and plot_summary.get("inventory_pen_slots") == len(PENS)
        and plot_summary.get("physical_pen_steps") == len(active_steps)
        and plot_summary.get("pen_changes") == max(0, len(active_steps) - 1),
        f"{row.subject_id}: inventory/physical pen summary drift",
        failures,
    )

    split_records = manifest.get("pen_files")
    _append(
        isinstance(split_records, list) and len(split_records) == 10,
        f"{row.subject_id}: manifest must declare ten split files",
        failures,
    )
    declared_splits: dict[int, dict[str, Any]] = {}
    if isinstance(split_records, list):
        for record in split_records:
            if isinstance(record, dict) and isinstance(record.get("step"), int):
                declared_splits[int(record["step"])] = record
    for pen in PENS:
        split_path = collection_dir / f"{row.stem}.pen-{pen.step:02d}-{pen.pen_id}.svg"
        artifacts.add(split_path)
        slot_record = sequence_by_step.get(pen.step)
        expected_status = (
            str(slot_record.get("slot_status"))
            if isinstance(slot_record, dict)
            else "invalid"
        )
        expected_path_count = (
            slot_record.get("path_count") if isinstance(slot_record, dict) else None
        )
        _append(
            split_path.is_file(),
            f"{row.subject_id}: missing split file {split_path.name}",
            failures,
        )
        record = declared_splits.get(pen.step)
        _append(
            isinstance(record, dict),
            f"{row.subject_id}: missing split declaration step {pen.step}",
            failures,
        )
        if isinstance(record, dict):
            try:
                declared_path = _resolve_release_path(
                    record.get("path"),
                    root_dir,
                    f"{row.subject_id} split step {pen.step}",
                )
                _append(
                    declared_path == split_path.resolve(),
                    f"{row.subject_id}: split filename drift at step {pen.step}",
                    failures,
                )
            except AuditInputError as exc:
                failures.append(str(exc))
            _append(
                record.get("pen_id") == pen.pen_id,
                f"{row.subject_id}: split pen ID drift",
                failures,
            )
            _append(
                isinstance(slot_record, dict)
                and record.get("configured_layers")
                == slot_record.get("configured_layers")
                and record.get("layers") == slot_record.get("layers")
                and record.get("omitted_layers") == slot_record.get("omitted_layers")
                and record.get("path_count") == expected_path_count
                and isinstance(record.get("empty"), bool)
                and record.get("empty") == slot_record.get("empty")
                and record.get("slot_status") == expected_status,
                f"{row.subject_id}: split slot declaration drift at step {pen.step}",
                failures,
            )
            if split_path.is_file() and isinstance(record.get("sha256"), str):
                _append(
                    _sha256(split_path) == record["sha256"],
                    f"{row.subject_id}: split manifest SHA drift",
                    failures,
                )
        if split_path.is_file():
            try:
                split_root = ET.parse(split_path).getroot()
                failures.extend(
                    _svg_path_leaks(
                        split_root,
                        root_dir,
                        f"{row.subject_id}.split-{pen.step}",
                    )
                )
                failures.extend(
                    _svg_active_content_failures(
                        split_root, f"{row.subject_id}.split-{pen.step}"
                    )
                )
                split_tags = {
                    element.tag.rsplit("}", 1)[-1] for element in split_root.iter()
                }
                _append(
                    split_tags <= ALLOWED_SVG_ELEMENTS
                    and not any(
                        "style" in element.attrib for element in split_root.iter()
                    ),
                    f"{row.subject_id}: split contains unsupported SVG/CSS at step {pen.step}",
                    failures,
                )
                _append(
                    _all_paths_are_physically_assigned(split_root),
                    f"{row.subject_id}: split has an unassigned path at step {pen.step}",
                    failures,
                )
                _append(
                    split_root.get("width") == "148mm"
                    and split_root.get("height") == "210mm"
                    and split_root.get("viewBox") == "0 0 148 210"
                    and (split_root.findtext(f"{{{SVG_NS}}}title") or "").strip()
                    == row.title
                    and split_root.get(f"{{{MAP_NS}}}pen-step") == str(pen.step)
                    and split_root.get(f"{{{MAP_NS}}}physical-pen-id") == pen.pen_id
                    and split_root.get(f"{{{MAP_NS}}}pen-profile") == "actual-pens"
                    and split_root.get(f"{{{MAP_NS}}}pen-slot-status")
                    == expected_status
                    and split_root.get(f"{{{MAP_NS}}}path-count")
                    == str(expected_path_count),
                    f"{row.subject_id}: split root contract drift at step {pen.step}",
                    failures,
                )
                _append(
                    len(split_root.findall(f".//{{{SVG_NS}}}path"))
                    == expected_path_count,
                    f"{row.subject_id}: split path-count drift at step {pen.step}",
                    failures,
                )
                failures.extend(
                    f"{row.subject_id}: {message}"
                    for message in split_parity_failures(svg_root, split_root, pen.step)
                )
            except (OSError, ET.ParseError) as exc:
                failures.append(
                    f"{row.subject_id}: invalid split SVG step {pen.step}: {exc}"
                )

    validator = None
    if frozen_spec is None:
        failures.append(f"{row.subject_id}: frozen format contract is unavailable")
    else:
        try:
            validator = validate(expected_svg, frozen_spec, "a5-portrait")
            failures.extend(
                f"{row.subject_id}: frozen validator: {message}"
                for message in validator.failures
            )
            failures.extend(
                f"{row.subject_id}: frozen validator warning: {message}"
                for message in validator.warnings
            )
            advisories.extend(
                f"{row.subject_id}: {message}" for message in validator.advisories
            )
        except (KeyError, OSError, TypeError, ValueError, ET.ParseError) as exc:
            failures.append(f"{row.subject_id}: frozen validator failed: {exc}")

    retrace_data: dict[str, Any] | None = None
    try:
        retrace = audit_svg_retraces(expected_svg, scope=AUDIT_SCOPE_ALL_PHYSICAL)
        retrace_data = retrace.as_dict()
        structural = [
            message for message in retrace.failures if "retrace" not in message.lower()
        ]
        failures.extend(
            f"{row.subject_id}: all-physical retrace: {message}"
            for message in structural
        )
        if retrace.retrace_length_mm > 1e-6:
            message = f"{row.subject_id}: all-physical retrace is {retrace.retrace_length_mm:.6f} mm"
            (failures if release_mode == "production" else advisories).append(message)
        _append(
            retrace.repeat_pass_count == 0
            and retrace.declared_repeat_length_mm <= 1e-6,
            f"{row.subject_id}: declared repeated physical passes",
            failures,
        )
    except (OSError, ET.ParseError, RetraceAuditError, ValueError) as exc:
        failures.append(f"{row.subject_id}: all-physical retrace audit failed: {exc}")

    plotsim: dict[str, Any] = {}
    try:
        machine = Machine()
        strokes, simulation_page = load_plate(expected_svg, machine)
        offset_warnings = _offset_geometry_warnings(simulation_page)
        _append(
            not offset_warnings,
            f"{row.subject_id}: PlotSim offset warnings: {offset_warnings}",
            failures,
        )
        for mode in ("document", "merged", "optimised"):
            _moves, statistics = simulate(order_strokes(strokes, mode), machine)
            plotsim[mode] = {
                key: round(value, 6) if isinstance(value, float) else value
                for key, value in statistics.items()
            }
            _append(
                statistics.get("pen_changes") == len(active_steps),
                f"{row.subject_id}: PlotSim {mode} physical pen-load count drift",
                failures,
            )
            _append(
                _number(statistics.get("pen_down_mm")) is not None
                and float(statistics["pen_down_mm"]) > 0,
                f"{row.subject_id}: PlotSim {mode} pen-down metric invalid",
                failures,
            )
        ratio = _number(plotsim.get("optimised", {}).get("travel_ratio"))
        _append(
            ratio is not None and ratio <= 2.0,
            f"{row.subject_id}: optimised PlotSim travel ratio exceeds 2.0",
            failures,
        )
        if ratio is not None and ratio > 1.0:
            advisories.append(
                f"{row.subject_id}: optimised PlotSim travel ratio {ratio:.3f} exceeds healthy 1.0"
            )
        manifest_distance = _number(
            (manifest.get("plot_summary") or {}).get("pen_down_distance_mm")
            if isinstance(manifest.get("plot_summary"), dict)
            else None
        )
        simulated_distance = _number(plotsim.get("document", {}).get("pen_down_mm"))
        _append(
            manifest_distance is not None
            and simulated_distance is not None
            and abs(manifest_distance - simulated_distance)
            <= max(0.5, simulated_distance * 0.0001),
            f"{row.subject_id}: PlotSim/manifest pen-down parity drift",
            failures,
        )
    except (OSError, ET.ParseError, KeyError, TypeError, ValueError) as exc:
        failures.append(f"{row.subject_id}: PlotSim failed: {exc}")

    png_data: dict[str, Any] | None = None
    if expected_png.is_file():
        try:
            png_data = inspect_png(expected_png)
            expected_png_tuple = (
                png_data["width_px"],
                png_data["height_px"],
                png_data["x_pixels_per_metre"],
                png_data["y_pixels_per_metre"],
            )
            _append(
                expected_png_tuple == EXPECTED_PNG,
                f"{row.subject_id}: PNG size or 254-DPI pHYs drift: {expected_png_tuple}",
                failures,
            )
            _append(
                png_data.get("physical_unit") == 1,
                f"{row.subject_id}: PNG pHYs unit is not metre",
                failures,
            )
            _append(
                png_data.get("opaque") is True,
                f"{row.subject_id}: PNG contains transparency",
                failures,
            )
            _append(
                png_data.get("has_nonwhite_pixel") is True,
                f"{row.subject_id}: PNG is blank white",
                failures,
            )
            raster_exports = manifest.get("raster_exports")
            raster = (
                raster_exports[0]
                if isinstance(raster_exports, list)
                and len(raster_exports) == 1
                and isinstance(raster_exports[0], dict)
                else None
            )
            _append(
                isinstance(raster, dict)
                and raster.get("format") == "PNG"
                and raster.get("path") == str(expected_png.resolve())
                and _same_number(raster.get("dpi"), 254.0)
                and raster.get("width_px") == 1480
                and raster.get("height_px") == 2100
                and raster.get("background") == "opaque white"
                and raster.get("png_sha256") == png_data["sha256"]
                and raster.get("source_svg_sha256") == _sha256(expected_svg),
                f"{row.subject_id}: manifest raster provenance drift",
                failures,
            )
        except (OSError, ValueError, struct.error) as exc:
            failures.append(f"{row.subject_id}: invalid PNG: {exc}")
    return (
        {
            "collection_id": row.collection_id,
            "position": row.position,
            "subject_id": row.subject_id,
            "institution_name": row.institution_name,
            "rank": row.rank,
            "visible_title": row.title,
            "passed": not failures,
            "failure_count": len(failures),
            "advisory_count": len(advisories),
            "format_checks": validator.checks if validator is not None else 0,
            "retrace": retrace_data,
            "plotsim": plotsim,
            "png": png_data,
        },
        artifacts,
        failures,
        advisories,
        source_path,
    )


def _renderer_derivation(
    renderer: dict[str, Any],
    *,
    archive: Path,
    root: Path,
    report: dict[str, Any],
    failures: list[str],
) -> None:
    expected_keys = {
        "path",
        "tree_sha256",
        "fingerprint",
        "archive",
        "archive_sha256",
        "base_tree_sha256",
        "base_fingerprint",
        "derivation",
    }
    _append(
        set(renderer) == expected_keys,
        f"series renderer schema drift: {sorted(set(renderer) ^ expected_keys)}",
        failures,
    )
    _append(
        renderer.get("archive_sha256") == EXPECTED_RENDERER_ARCHIVE_SHA256,
        "series base renderer archive hash drift",
        failures,
    )
    _append(
        renderer.get("base_fingerprint") == EXPECTED_BASE_FINGERPRINT,
        "series base renderer fingerprint drift",
        failures,
    )
    try:
        base_files = _archive_file_map(archive)
    except (OSError, tarfile.TarError, AuditInputError) as exc:
        failures.append(f"could not independently inspect base renderer archive: {exc}")
        return
    base_tree_sha = _tree_digest_payloads(base_files)
    _append(
        renderer.get("base_tree_sha256") == base_tree_sha,
        "series base renderer tree SHA-256 drift",
        failures,
    )
    try:
        independent_base_fingerprint = _renderer_fingerprint_from_files(base_files)
    except AuditInputError as exc:
        failures.append(str(exc))
        return
    _append(
        independent_base_fingerprint == EXPECTED_BASE_FINGERPRINT,
        "base archive renderer fingerprint no longer matches reviewed v2.1",
        failures,
    )

    try:
        derived_root = _resolve_release_path(
            renderer.get("path"), root, "derived renderer"
        )
    except AuditInputError as exc:
        failures.append(str(exc))
        return
    if not derived_root.is_dir():
        failures.append(f"derived renderer is not a directory: {derived_root}")
        return
    try:
        derived_files = _file_map(derived_root)
    except OSError as exc:
        failures.append(f"could not hash derived renderer tree: {exc}")
        return
    _append(
        set(derived_files) == set(base_files),
        "derived renderer adds or removes files relative to the frozen base",
        failures,
    )
    changed = sorted(
        relative
        for relative in set(base_files) & set(derived_files)
        if base_files[relative] != derived_files[relative]
    )
    _append(
        changed == sorted(DERIVATION_OVERRIDES),
        f"derived renderer changed unapproved files: {changed}",
        failures,
    )
    derived_tree_sha = _tree_digest_payloads(derived_files)
    _append(
        renderer.get("tree_sha256")
        == derived_tree_sha
        == EXPECTED_DERIVED_RENDERER_TREE_SHA256,
        "derived renderer tree SHA-256 drift",
        failures,
    )
    try:
        derived_fingerprint = _renderer_fingerprint_from_files(derived_files)
    except AuditInputError as exc:
        failures.append(str(exc))
        return
    _append(
        renderer.get("fingerprint") == derived_fingerprint
        and derived_fingerprint.get("sha256")
        == EXPECTED_DERIVED_RENDERER_FINGERPRINT_SHA256,
        "derived renderer fingerprint is not independently reproducible",
        failures,
    )
    _append(
        report.get("renderer_fingerprint") == derived_fingerprint,
        "batch report is not bound to the exact derived renderer",
        failures,
    )

    derivation = renderer.get("derivation")
    if not isinstance(derivation, dict):
        failures.append("renderer derivation record is missing")
        return
    _append(
        set(derivation) == {"id", "visual_policy", "overrides"},
        "renderer derivation schema drift",
        failures,
    )
    _append(
        derivation.get("id") == DERIVATION_ID, "renderer derivation ID drift", failures
    )
    _append(
        derivation.get("visual_policy") == DERIVATION_VISUAL_POLICY,
        "renderer derivation visual-policy drift",
        failures,
    )
    overrides = derivation.get("overrides")
    _append(
        isinstance(overrides, list) and len(overrides) == len(DERIVATION_OVERRIDES),
        "renderer derivation must declare exactly the approved overrides",
        failures,
    )
    if (
        not isinstance(overrides, list)
        or len(overrides) != len(DERIVATION_OVERRIDES)
        or not all(isinstance(item, dict) for item in overrides)
    ):
        return
    _append(
        [item.get("path") for item in overrides] == list(DERIVATION_OVERRIDES),
        "renderer override order/path set drift",
        failures,
    )
    for override in overrides:
        assert isinstance(override, dict)
        path = override.get("path")
        _append(
            set(override) == {"path", "source_sha256", "scope"},
            f"renderer override schema drift for {path!r}",
            failures,
        )
        expected_source_sha = DERIVATION_OVERRIDES.get(str(path))
        actual_source_sha = (
            hashlib.sha256(derived_files[str(path)]).hexdigest()
            if str(path) in derived_files
            else None
        )
        _append(
            expected_source_sha is not None
            and override.get("source_sha256")
            == expected_source_sha
            == actual_source_sha,
            f"renderer override source SHA-256 drift for {path!r}",
            failures,
        )
        _append(
            isinstance(override.get("scope"), str) and bool(override["scope"].strip()),
            f"renderer override scope is missing for {path!r}",
            failures,
        )


def _option_value(argv: Sequence[object], option: str) -> str | None:
    value: str | None = None
    for index, token in enumerate(argv):
        text = str(token)
        if text == option and index + 1 < len(argv):
            value = str(argv[index + 1])
        elif text.startswith(option + "="):
            value = text.split("=", 1)[1]
    return value


def _export_contract_failures(
    report: dict[str, Any], *, root: Path, style_path: Path
) -> list[str]:
    failures: list[str] = []
    raw = report.get("export_args")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return ["batch export_args must be an array of strings"]
    argv: list[object] = list(raw)
    required_values = {
        "--radius-km": "2",
        "--preset": "a5-balanced-poster",
        "--poster-layout": "university-memorabilia",
        "--layers": "roads,water,railways,parks,buildings",
        "--water-fill": "dots",
        "--detail-profile": "plotter-faithful",
        "--simplify-mm": "0.04",
        "--road-style": "centreline",
        "--extent-fit": "contain",
        "--pen-profile": "actual-pens",
        "--attribution-mode": "external",
        "--external-attribution-placement": EXPECTED_EXTERNAL_ATTRIBUTION,
    }
    for option, expected in required_values.items():
        _append(
            _option_value(argv, option) == expected,
            f"batch export option {option} must be {expected!r}",
            failures,
        )
    raw_style = _option_value(argv, "--style")
    try:
        declared_style = _resolve_release_path(raw_style, root, "batch style option")
        _append(
            declared_style == style_path,
            "batch style option does not select the frozen series style",
            failures,
        )
    except AuditInputError as exc:
        failures.append(str(exc))
    required_flags = {
        "--landmark-buildings",
        "--no-scale-bar",
        "--no-scale-detail",
        "--optimise",
        "--physical-audit",
        "--split-by-pen",
        "--frame",
    }
    tokens = {str(item).split("=", 1)[0] for item in argv}
    _append(
        required_flags <= tokens,
        f"batch export is missing required flags: {sorted(required_flags - tokens)}",
        failures,
    )
    forbidden = {
        "--production",
        "--allow-repeat-passes",
        "--accept-physical-conflicts",
        "--scale-bar",
        "--scale-detail",
        "--theme",
    }
    _append(
        not (tokens & forbidden),
        f"batch export contains forbidden release flags: {sorted(tokens & forbidden)}",
        failures,
    )
    return failures


def _validate_recipe_binding(
    contract: dict[str, Any], root: Path, failures: list[str]
) -> Path | None:
    binding = contract.get("render_recipe")
    expected = {
        "path": "release-metadata/render-recipe-v2.1.4.json",
        "sha256": EXPECTED_RENDER_RECIPE_SHA256,
    }
    _append(binding == expected, "series render-recipe binding drift", failures)
    if not isinstance(binding, dict):
        return None
    try:
        path = _resolve_release_path(binding.get("path"), root, "render recipe")
    except AuditInputError as exc:
        failures.append(str(exc))
        return None
    if not path.is_file() or path.is_symlink():
        failures.append("copied render recipe is missing or a symlink")
        return None
    _append(
        _sha256(path) == EXPECTED_RENDER_RECIPE_SHA256,
        "copied render recipe bytes drift",
        failures,
    )
    try:
        recipe = _json(path)
    except AuditInputError as exc:
        failures.append(str(exc))
        return path
    dependencies = recipe.get("dependencies")
    renderer = recipe.get("renderer")
    _append(
        recipe.get("schema_version") == 1
        and recipe.get("id") == SERIES_ID
        and recipe.get("status") == "review-only"
        and isinstance(dependencies, dict)
        and dependencies.get("source_manifest_sha256")
        == EXPECTED_SOURCE_MANIFEST_SHA256
        and dependencies.get("source_cohort_sha256") == EXPECTED_SOURCE_COHORT_SHA256
        and isinstance(renderer, dict)
        and renderer.get("derived_tree_sha256") == EXPECTED_DERIVED_RENDERER_TREE_SHA256
        and renderer.get("derived_fingerprint_sha256")
        == EXPECTED_DERIVED_RENDERER_FINGERPRINT_SHA256,
        "render recipe identity/dependency pins drift",
        failures,
    )
    return path


def _expected_pinned_source_cohort(
    bundle: PinnedSourceBundle, selected_subject_ids: Sequence[str]
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "policy_id": SOURCE_COHORT_POLICY_ID,
        "mode": "pinned-input-json-set",
        "pinned": True,
        "production_eligible": False,
        "cohort_id": f"osm-json-set-sha256:{EXPECTED_SOURCE_COHORT_SHA256}",
        "json_set": {
            "manifest": {
                "path": str(bundle.manifest_path),
                "size_bytes": bundle.manifest_size_bytes,
                "sha256": EXPECTED_SOURCE_MANIFEST_SHA256,
                "schema_version": 1,
                "id": SOURCE_CONTRACT_ID,
                "cohort_sha256": EXPECTED_SOURCE_COHORT_SHA256,
            },
            "license": SOURCE_LICENSE,
            "entries": [
                bundle.entries[subject_id].cohort_record()
                for subject_id in selected_subject_ids
                if subject_id in bundle.entries
            ],
        },
        "reason": SOURCE_COHORT_REASON,
    }
    return {**payload, "sha256": _stable_digest(payload)}


def _validate_pinned_source_cohort(
    report: dict[str, Any],
    bundle: PinnedSourceBundle,
    selected_subject_ids: Sequence[str],
    failures: list[str],
) -> dict[str, Any] | None:
    cohort = report.get("source_cohort")
    expected = _expected_pinned_source_cohort(bundle, selected_subject_ids)
    _append(
        len(expected["json_set"]["entries"]) == len(selected_subject_ids),
        "batch source cohort references an unknown selected subject",
        failures,
    )
    _append(
        cohort == expected,
        "batch source cohort is not the exact pinned JSON subject set",
        failures,
    )
    _append(
        report.get("source_cohort_sha256") == expected["sha256"],
        "batch source cohort top-level binding drift",
        failures,
    )
    _append(
        report.get("source_manifest") == str(bundle.manifest_path),
        "batch source-manifest path differs from the copied contract",
        failures,
    )
    return cohort if isinstance(cohort, dict) and cohort == expected else None


def _release_contract(
    root: Path,
    report: dict[str, Any],
    catalog_path: Path,
    failures: list[str],
) -> tuple[
    dict[str, Any] | None,
    Path | None,
    PinnedSourceBundle | None,
    set[Path],
]:
    path = root / "SERIES-CONTRACT.json"
    if not path.is_file():
        failures.append("SERIES-CONTRACT.json is missing")
        return None, None, None, set()
    try:
        contract = _json(path)
    except AuditInputError as exc:
        failures.append(str(exc))
        return None, None, None, set()
    expected_contract_keys = {
        "schema_version",
        "series_id",
        "status",
        "expected_subject_count",
        "collections",
        "catalog",
        "render_recipe",
        "renderer",
        "style",
        "source_contract",
        "output_contract",
    }
    _append(
        set(contract) == expected_contract_keys and contract.get("schema_version") == 1,
        "series contract schema drift",
        failures,
    )
    _append(
        contract.get("series_id") == SERIES_ID,
        "series contract ID/version drift",
        failures,
    )
    _append(
        contract.get("status") == "review-only",
        "series contract must be review-only",
        failures,
    )
    _append(
        contract.get("expected_subject_count") == 50,
        "series contract subject count drift",
        failures,
    )
    _append(
        contract.get("collections") == list(COLLECTIONS),
        "series contract collection order drift",
        failures,
    )
    expected_output_contract = {
        "paper": "A5 portrait",
        "preset": "a5-balanced-poster",
        "poster_layout": "university-memorabilia",
        "families": EXPECTED_FAMILIES,
        "detail_profile": "plotter-faithful",
        "simplify_mm": 0.04,
        "road_style": "centreline",
        "extent_fit": "contain",
        "water_fill": "dots",
        "landmark_buildings": True,
        "radius_km": 2.0,
        "pen_profile": "actual-pens",
        "inventory_pen_slots": 10,
        "empty_pen_slot_policy": (
            "manifest-and-zero-path-split-without-empty-master-group"
        ),
        "split_by_pen": True,
        "optimise_travel": True,
        "physical_audit": True,
        "scale_bar": False,
        "scale_detail": False,
        "north_mark": True,
        "png_dpi": 254,
        "title_policy": "uppercase-city-or-campus-locality",
        "attribution_mode": "external",
        "external_attribution_placement": EXPECTED_EXTERNAL_ATTRIBUTION,
    }
    _append(
        contract.get("output_contract") == expected_output_contract,
        "series output contract drift",
        failures,
    )
    bound_artifacts: set[Path] = set()
    recipe_path = _validate_recipe_binding(contract, root, failures)
    if recipe_path is not None:
        bound_artifacts.add(recipe_path)
    catalog = contract.get("catalog")
    if not isinstance(catalog, dict):
        failures.append("series contract catalog binding missing")
    else:
        try:
            copy = _resolve_release_path(
                catalog.get("path"), root, "series catalog copy"
            )
            _append(
                copy == catalog_path,
                "report and series contract use different catalog copies",
                failures,
            )
            _append(copy.is_file(), "series catalog copy is missing", failures)
            if copy.is_file():
                _append(
                    catalog.get("sha256") == _sha256(copy),
                    "series catalog SHA-256 drift",
                    failures,
                )
                _append(
                    _sha256(copy) == EXPECTED_CATALOG_SHA256
                    and _sha256(DEFAULT_CATALOG) == EXPECTED_CATALOG_SHA256,
                    "release/packaged catalog bytes differ from the frozen ranked cohort",
                    failures,
                )
        except AuditInputError as exc:
            failures.append(str(exc))
    source_binding = contract.get("source_contract")
    expected_source_binding = {
        "mode": "pinned-input-json-set",
        "path": "release-metadata/source-snapshots/source-manifest.json",
        "manifest_sha256": EXPECTED_SOURCE_MANIFEST_SHA256,
        "cohort_sha256": EXPECTED_SOURCE_COHORT_SHA256,
        "subject_count": 50,
        "network_fallback": False,
        "production_eligible": False,
    }
    _append(
        source_binding == expected_source_binding,
        "series pinned-source contract binding drift",
        failures,
    )
    source_bundle: PinnedSourceBundle | None = None
    if isinstance(source_binding, dict):
        try:
            source_manifest = _resolve_release_path(
                source_binding.get("path"), root, "series source manifest"
            )
            catalog_failures: list[str] = []
            catalog_rows = validate_catalog_document(
                _json(catalog_path), catalog_failures
            )
            failures.extend(
                f"source contract catalog binding: {item}" for item in catalog_failures
            )
            if not catalog_failures:
                source_bundle = _validate_pinned_source_manifest(
                    source_manifest, catalog_rows, failures
                )
                if source_bundle is not None:
                    bound_artifacts.update(source_bundle.artifacts)
        except AuditInputError as exc:
            failures.append(str(exc))
    renderer = contract.get("renderer")
    archive: Path | None = None
    if not isinstance(renderer, dict):
        failures.append("series renderer binding missing")
    else:
        try:
            archive = _resolve_release_path(
                renderer.get("archive"), root, "series renderer archive"
            )
        except AuditInputError as exc:
            failures.append(str(exc))
    style = contract.get("style")
    style_path: Path | None = None
    if not isinstance(style, dict):
        failures.append("series style binding missing")
    else:
        _append(
            style.get("sha256") == EXPECTED_STYLE_SHA256,
            "series style fingerprint drift",
            failures,
        )
        try:
            style_path = _resolve_release_path(style.get("path"), root, "series style")
            _append(
                style_path.is_file() and _sha256(style_path) == EXPECTED_STYLE_SHA256,
                "copied series style bytes drift",
                failures,
            )
        except AuditInputError as exc:
            failures.append(str(exc))
    if style_path is not None:
        failures.extend(
            _export_contract_failures(report, root=root, style_path=style_path)
        )
        dependencies = report.get("dependency_fingerprints")
        _append(
            style_path.is_file()
            and isinstance(dependencies, list)
            and len(dependencies) == 1
            and isinstance(dependencies[0], dict)
            and dependencies[0].get("option") == "--style"
            and Path(str(dependencies[0].get("path", ""))).resolve() == style_path
            and dependencies[0].get("sha256") == EXPECTED_STYLE_SHA256
            and dependencies[0].get("size_bytes") == style_path.stat().st_size,
            "batch dependency fingerprint is not exactly the frozen style",
            failures,
        )
    if isinstance(renderer, dict) and archive is not None and archive.is_file():
        _renderer_derivation(
            renderer,
            archive=archive,
            root=root,
            report=report,
            failures=failures,
        )
    _append(
        report.get("title_mode") == "city",
        "batch title_mode must be city",
        failures,
    )
    _append(
        _same_number(report.get("png_dpi"), 254.0),
        "batch PNG DPI must be 254",
        failures,
    )
    if source_bundle is None:
        failures.append("batch pinned-source cohort cannot be verified")
    else:
        raw_items = report.get("items")
        selected_subject_ids = (
            [
                str(item.get("subject_id"))
                for item in raw_items
                if isinstance(item, dict) and isinstance(item.get("subject_id"), str)
            ]
            if isinstance(raw_items, list)
            else []
        )
        _validate_pinned_source_cohort(
            report, source_bundle, selected_subject_ids, failures
        )
    return contract, archive, source_bundle, bound_artifacts


def audit(
    report_path: Path,
    *,
    output_root: Path | None,
    catalog_file: Path,
    allow_incomplete: bool,
    release_mode: str,
) -> tuple[dict[str, Any], Path]:
    report_path = report_path.expanduser().resolve()
    report = _json(report_path)
    if output_root is None:
        raw_root = report.get("output_dir")
        if not isinstance(raw_root, str):
            raise AuditInputError("Batch report has no output_dir; pass --output-root.")
        root = Path(raw_root).expanduser().resolve()
    else:
        root = output_root.expanduser().resolve()
    if not root.is_dir():
        raise AuditInputError(f"Output root is not a directory: {root}")
    if not _within(report_path, root):
        raise AuditInputError(
            "Batch report must be stored inside the audited output root."
        )

    failures: list[str] = []
    advisories: list[str] = []
    item_reports: list[dict[str, Any]] = []
    required_artifacts: set[Path] = {report_path}
    failures.extend(_release_hygiene_failures(root))
    failures.extend(_json_path_leaks(report, root, "batch-report"))
    _append(
        report.get("schema_version") == 3, "batch schema_version must be 3", failures
    )
    _append(
        Path(str(report.get("output_dir", ""))).resolve() == root,
        "batch output_dir drift",
        failures,
    )

    catalog_file = catalog_file.expanduser().resolve()
    _append(
        catalog_file.is_file(), f"catalog file is missing: {catalog_file}", failures
    )
    try:
        declared_catalog = _resolve_release_path(
            (report.get("catalog") or {}).get("file")
            if isinstance(report.get("catalog"), dict)
            else None,
            root,
            "batch catalog",
        )
    except AuditInputError as exc:
        failures.append(str(exc))
        declared_catalog = catalog_file
    _append(
        declared_catalog == catalog_file,
        "--catalog-file and batch catalog binding differ",
        failures,
    )
    required_artifacts.add(catalog_file)
    catalog_document = _json(catalog_file)
    _append(
        _sha256(catalog_file) == EXPECTED_CATALOG_SHA256,
        "ranked catalog SHA-256 drift",
        failures,
    )
    catalog_rows = validate_catalog_document(catalog_document, failures)
    catalog_meta = report.get("catalog")
    _append(
        isinstance(catalog_meta, dict)
        and catalog_meta.get("version") == CATALOG_VERSION
        and catalog_meta.get("as_of") == catalog_document.get("as_of"),
        "batch catalog version/as_of binding drift",
        failures,
    )
    pairs = _report_rows(report, catalog_rows, allow_incomplete, failures)
    report_collections = report.get("collections")
    _append(
        isinstance(report_collections, list)
        and [item.get("id") for item in report_collections if isinstance(item, dict)]
        == list(COLLECTIONS)
        and [
            item.get("catalog_entry_count")
            for item in report_collections
            if isinstance(item, dict)
        ]
        == [30, 20],
        "batch collection identity/count/order drift",
        failures,
    )
    _append(
        report.get("item_count") == len(pairs),
        "batch item_count disagrees with the audited rows",
        failures,
    )
    summary = report.get("summary")
    _append(
        isinstance(summary, dict)
        and summary.get("completed") == len(pairs)
        and summary.get("pending") == 0
        and summary.get("running") == 0
        and summary.get("failed") == 0,
        "batch summary is not an entirely completed cohort",
        failures,
    )

    series_contract, archive, source_bundle, contract_artifacts = _release_contract(
        root, report, catalog_file, failures
    )
    required_artifacts.update({root / "SERIES-CONTRACT.json", root / "ATTRIBUTION.md"})
    required_artifacts.update(contract_artifacts)
    if archive is not None:
        required_artifacts.add(archive)
    frozen_spec = _tar_contract(archive, failures) if archive is not None else None

    if release_mode == "production":
        failures.append(
            "production release is blocked: pinned saved JSON is reproducible but "
            "remains review-only without the required physical calibration/PBF policy"
        )
    for row, item in pairs:
        item_report, artifacts, item_failures, item_advisories, _source_path = (
            _audit_svg_and_manifest(
                row,
                item,
                root,
                frozen_spec,
                release_mode,
                (
                    report["renderer_fingerprint"].get("sha256")
                    if isinstance(report.get("renderer_fingerprint"), dict)
                    else None
                ),
                (
                    report["source_cohort"]
                    if isinstance(report.get("source_cohort"), dict)
                    else None
                ),
                (
                    source_bundle.entries.get(row.subject_id)
                    if source_bundle is not None
                    else None
                ),
            )
        )
        required_artifacts.update(artifacts)
        item_reports.append(item_report)
        failures.extend(item_failures)
        advisories.extend(item_advisories)
    source_cache = root / "source-cache"
    stale_cached_sources = (
        sorted(source_cache.rglob("*.json.gz")) if source_cache.is_dir() else []
    )
    _append(
        not stale_cached_sources,
        "pinned-source release contains unexpected live/cache response files",
        failures,
    )

    attribution = root / "ATTRIBUTION.md"
    if attribution.is_file():
        text = attribution.read_text(encoding="utf-8")
        _append(
            "OpenStreetMap contributors" in text
            and "ODbL" in text
            and "openstreetmap.org/copyright" in text,
            "ATTRIBUTION.md lacks the complete OSM/ODbL attribution",
            failures,
        )
    else:
        failures.append("ATTRIBUTION.md is missing")
    catalog_doc = root / "RANKED-UNIVERSITIES.md"
    required_artifacts.add(catalog_doc)
    if not catalog_doc.is_file():
        failures.append("RANKED-UNIVERSITIES.md is missing")
    else:
        text = catalog_doc.read_text(encoding="utf-8")
        for row, _item in pairs:
            _append(
                row.subject_id in text
                and row.institution_name in text
                and row.rank in text
                and row.collection_id in text,
                f"RANKED-UNIVERSITIES.md does not bind {row.subject_id} name/rank/collection",
                failures,
            )
    observed_contact_sheets = {
        path
        for path in root.rglob("*.png")
        if "contact" in path.name.casefold() and "sheet" in path.name.casefold()
    }
    completed_by_collection = {
        collection: sum(row.collection_id == collection for row, _item in pairs)
        for collection in COLLECTIONS
        if any(row.collection_id == collection for row, _item in pairs)
    }
    expected_contact_sheets = {
        root / CONTACT_NAMES[collection] for collection in completed_by_collection
    }
    expected_contact_dimensions = {
        root / CONTACT_NAMES[collection]: (
            1600,
            444 * ((count + 4) // 5),
        )
        for collection, count in completed_by_collection.items()
    }
    _append(
        observed_contact_sheets == expected_contact_sheets,
        "release contact-sheet inventory/naming does not match its completed collections",
        failures,
    )
    contact_sheets = sorted(expected_contact_sheets)
    required_artifacts.update(contact_sheets)
    for contact_sheet in contact_sheets:
        try:
            contact_png = inspect_png(contact_sheet)
            _append(
                contact_png.get("x_pixels_per_metre") == 10000
                and contact_png.get("y_pixels_per_metre") == 10000
                and contact_png.get("physical_unit") == 1,
                f"contact sheet lacks exact 254-DPI pHYs metadata: {contact_sheet.relative_to(root)}",
                failures,
            )
            _append(
                (
                    contact_png.get("width_px"),
                    contact_png.get("height_px"),
                )
                == expected_contact_dimensions[contact_sheet],
                f"contact sheet grid dimensions do not bind its plate count: {contact_sheet.relative_to(root)}",
                failures,
            )
            _append(
                contact_png.get("opaque") is True
                and contact_png.get("has_nonwhite_pixel") is True,
                f"contact sheet is transparent or blank: {contact_sheet.relative_to(root)}",
                failures,
            )
        except (OSError, ValueError, struct.error) as exc:
            failures.append(
                f"invalid contact-sheet PNG {contact_sheet.relative_to(root)}: {exc}"
            )

    finalization_path = root / "FINALIZATION.json"
    required_artifacts.add(finalization_path)
    if not finalization_path.is_file():
        failures.append("FINALIZATION.json is missing")
    else:
        try:
            finalization = _json(finalization_path)
            failures.extend(_json_path_leaks(finalization, root, "FINALIZATION.json"))
            expected_finalization_keys = {
                "schema_version",
                "status",
                "generated_at",
                "batch_report",
                "completed_plate_count",
                "completed_by_collection",
                "contact_sheets",
                "checksum_policy",
            }
            _append(
                set(finalization) == expected_finalization_keys
                and finalization.get("schema_version") == 1
                and finalization.get("status")
                == ("pilot" if allow_incomplete else "complete-review-series")
                and isinstance(finalization.get("generated_at"), str)
                and bool(finalization.get("generated_at"))
                and finalization.get("batch_report")
                == report_path.relative_to(root).as_posix()
                and finalization.get("completed_plate_count") == len(pairs)
                and finalization.get("completed_by_collection")
                == completed_by_collection
                and finalization.get("contact_sheets")
                == [
                    path.relative_to(root).as_posix()
                    for path in sorted(expected_contact_sheets)
                ]
                and finalization.get("checksum_policy") == FINALIZATION_CHECKSUM_POLICY,
                "FINALIZATION.json does not exactly bind this completed release",
                failures,
            )
        except AuditInputError as exc:
            failures.append(str(exc))

    checksums_path = root / "CHECKSUMS.sha256"
    checksums = read_checksums(checksums_path, root, failures)
    for path in sorted(required_artifacts):
        _checksum_covers(path, root, checksums, failures)
    declared_release_files = set(checksums)
    _append(
        not any(relative in QA_REPORT_NAMES for relative in checksums),
        "CHECKSUMS.sha256 must not include the QA report that this audit rewrites",
        failures,
    )
    expected_plate_files = {
        path.resolve()
        for path in required_artifacts
        if path.suffix in {".svg", ".png"} or path.name.endswith(".plot.json")
    }
    observed_plate_files = {
        path.resolve()
        for path in root.rglob("*")
        if path.is_file()
        and (path.suffix in {".svg", ".png"} or path.name.endswith(".plot.json"))
    }
    for extra in sorted(observed_plate_files - expected_plate_files):
        failures.append(
            f"unbound/stale plate artifact in release: {extra.relative_to(root)}"
        )
    expected_release_paths = {path.resolve() for path in required_artifacts}
    for collection in COLLECTIONS:
        collection_dir = root / collection
        if not collection_dir.is_dir():
            continue
        for path in collection_dir.rglob("*"):
            if path.is_file() and path.resolve() not in expected_release_paths:
                failures.append(
                    f"unbound/stale file in collection directory: {path.relative_to(root)}"
                )
    allowed_root_json = {
        report_path.resolve(),
        (root / "SERIES-CONTRACT.json").resolve(),
        finalization_path.resolve(),
    }
    for path in root.glob("*.json"):
        if _is_designated_qa_report(path, root) or path.resolve() in allowed_root_json:
            continue
        failures.append(f"unbound/stale root JSON artifact: {path.relative_to(root)}")
    # Every release byte, including the derived renderer's Python sources, is
    # part of the checksum inventory.  Only the inventory itself and this
    # regenerated QA report are self-referential exclusions.
    for path in root.rglob("*"):
        if path.is_symlink():
            failures.append(
                f"release contains a forbidden symbolic link: {path.relative_to(root)}"
            )
            continue
        if (
            not path.is_file()
            or path == checksums_path
            or _is_designated_qa_report(path, root)
        ):
            continue
        relative = path.relative_to(root).as_posix()
        _append(
            relative in declared_release_files,
            f"release artifact is undeclared by CHECKSUMS.sha256: {relative}",
            failures,
        )

    report_result = {
        "schema_version": 1,
        "policy_id": POLICY_ID,
        "status": (
            "failed"
            if failures
            else "passed-pilot-review"
            if allow_incomplete
            else "passed-review"
        ),
        "release_mode": release_mode,
        "allow_incomplete": allow_incomplete,
        "production_eligible": False,
        "output_root": str(root),
        "batch_report": str(report_path),
        "catalog": {
            "path": str(catalog_file),
            "version": catalog_document.get("catalog_version"),
            "as_of": catalog_document.get("as_of"),
            "sha256": _sha256(catalog_file),
            "expected_count": 50,
            "audited_count": len(pairs),
        },
        "frozen_contract": {
            "renderer_archive_sha256": EXPECTED_RENDERER_ARCHIVE_SHA256,
            "renderer_fingerprint_sha256": EXPECTED_RENDERER_FINGERPRINT_SHA256,
            "derived_renderer_tree_sha256": EXPECTED_DERIVED_RENDERER_TREE_SHA256,
            "derived_renderer_fingerprint_sha256": (
                EXPECTED_DERIVED_RENDERER_FINGERPRINT_SHA256
            ),
            "style_sha256": EXPECTED_STYLE_SHA256,
            "format_sha256": EXPECTED_FORMAT_SHA256,
            "render_recipe_sha256": EXPECTED_RENDER_RECIPE_SHA256,
            "source_manifest_sha256": EXPECTED_SOURCE_MANIFEST_SHA256,
            "source_cohort_sha256": EXPECTED_SOURCE_COHORT_SHA256,
        },
        "series_contract_present": series_contract is not None,
        "summary": {
            "items": len(item_reports),
            "passed_items": sum(bool(item.get("passed")) for item in item_reports),
            "failures": len(failures),
            "advisories": len(advisories),
        },
        "failures": failures,
        "advisories": advisories,
        "items": item_reports,
    }
    return report_result, root


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_qa_report_path(
    path: Path, root: Path, protected_paths: Iterable[Path]
) -> Path:
    resolved = path.expanduser().resolve()
    if not _within(resolved, root):
        raise AuditInputError("QA report path must be inside the audited output root.")
    if resolved.parent != root.resolve():
        raise AuditInputError("QA report must be written at the release root.")
    if resolved.name not in QA_REPORT_NAMES:
        raise AuditInputError(
            "QA report must use one of the exact reserved root filenames: "
            + ", ".join(sorted(QA_REPORT_NAMES))
        )
    protected = {item.expanduser().resolve() for item in protected_paths}
    if resolved in protected:
        raise AuditInputError(
            "QA report path would overwrite an audited input artifact."
        )
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report", type=Path, help="completed ranked-university batch report"
    )
    parser.add_argument(
        "--output-root", type=Path, help="release root (defaults to report output_dir)"
    )
    parser.add_argument(
        "--catalog-file",
        type=Path,
        help="release-local catalog copy; defaults to report catalog.file",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="permit a non-empty, order-preserving pilot subset",
    )
    parser.add_argument(
        "--release-mode", choices=("review", "production"), default="review"
    )
    parser.add_argument(
        "--qa-report",
        type=Path,
        help="QA JSON path (default: <output-root>/RANKED_UNIVERSITY_QA_REPORT.json)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        batch = _json(args.report.expanduser().resolve())
        root = (
            args.output_root.expanduser().resolve()
            if args.output_root is not None
            else Path(str(batch.get("output_dir", ""))).expanduser().resolve()
        )
        if args.catalog_file is not None:
            catalog = args.catalog_file.expanduser().resolve()
        else:
            catalog_meta = batch.get("catalog")
            if not isinstance(catalog_meta, dict):
                raise AuditInputError(
                    "Batch report lacks catalog metadata; pass --catalog-file."
                )
            catalog = _resolve_release_path(
                catalog_meta.get("file"), root, "batch catalog"
            )
        result, root = audit(
            args.report,
            output_root=args.output_root,
            catalog_file=catalog,
            allow_incomplete=args.allow_incomplete,
            release_mode=args.release_mode,
        )
        qa_candidate = (
            args.qa_report
            if args.qa_report is not None
            else root / "RANKED_UNIVERSITY_QA_REPORT.json"
        )
        qa_path = _validate_qa_report_path(qa_candidate, root, (args.report, catalog))
        _atomic_json(qa_path, result)
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "qa_report": str(qa_path),
                    **result["summary"],
                },
                indent=2,
            )
        )
        return 0 if not result["failures"] else 1
    except (AuditInputError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

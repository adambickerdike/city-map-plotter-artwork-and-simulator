"""Strict coverage and rights audit for named engineering collections.

The audit is a sourcing ledger, not a renderer. It distinguishes a useful
reference from an asset that actually authorises source-derived commercial
artwork, verifies every bundled byte, and fails if status overstates view
coverage. No function in this module creates or completes geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
import re
from typing import Any, NoReturn, Sequence
import unicodedata
import zlib
from xml.etree import ElementTree as ET

from .models import MapPlotterError
from .technical_collections import COLLECTION_CATALOG_PATHS


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "engineering-collections-v2"
RELEASE_BINDING_ROOT = CONTRACT_ROOT / "release-bindings"
RELEASE_EVIDENCE_ROOT = CONTRACT_ROOT / "release-evidence"
AUDIT_PATHS = {
    "cars": CONTRACT_ROOT / "audits" / "cars.json",
    "aircraft": CONTRACT_ROOT / "audits" / "aircraft.json",
    "boats": CONTRACT_ROOT / "audits" / "boats.json",
}
POLICY_ID = "source-only-no-procedural-fallback-v2"
# Unbound export is limited to immutable, project-owned fixtures whose complete
# validated-record digest is reviewed in code. Any edit becomes export-blocked.
UNBOUND_DEMONSTRATOR_RECORD_SHA256 = {
    "cmp-r1-roadster": "6aa0ef54e62a84887538b272dd6e6c4d6fea8ba8ff0f3dd2c30c3b4ea88193c5",
}
# Production bindings need a second, deliberate code-review activation step.
# This is empty because no named v2 engineering subject is ready today.
ACTIVATED_RELEASE_BINDING_SHA256: dict[str, str] = {}
STATUS_VALUES = frozenset(
    {
        "ready",
        "partial",
        "blocked-rights",
        "blocked-geometry",
        "blocked-validation",
    }
)
REQUIRED_VIEWS = {
    "cars": ("side", "front", "rear", "plan"),
    "aircraft": ("side", "front", "plan"),
    "boats": ("side", "plan"),
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STABLE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_RELEASE_FORMATS = (
    "a5-portrait",
    "a5-landscape",
    "a4-portrait",
    "a4-landscape",
    "a3-portrait",
    "a3-landscape",
)
_PERMITTED_TRANSFORMS = frozenset(
    {
        "identity",
        "crop",
        "rotation",
        "translation",
        "uniform-scale",
        "axis-calibrated-scale",
        "coordinate-axis-inversion",
        "source-declared-mirroring",
        "semantic-reclassification",
        "pen-aware-simplification",
    }
)


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid engineering source audit: {message}")


def _binding_fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid engineering release binding: {message}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object.")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array.")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text.")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    result = _text(value, label)
    if _STABLE_ID.fullmatch(result) is None:
        _fail(f"{label} must use lowercase letters, digits and hyphens.")
    return result


def _exact_keys(
    value: dict[str, Any],
    label: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = sorted(required - set(value))
    unexpected = sorted(set(value) - allowed)
    if missing:
        _fail(f"{label} is missing fields: {', '.join(missing)}.")
    if unexpected:
        _fail(f"{label} has unsupported fields: {', '.join(unexpected)}.")


def _https(value: Any, label: str) -> str:
    result = _text(value, label)
    if not result.startswith("https://"):
        _fail(f"{label} must use HTTPS.")
    return result


def _optional_https(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _https(value, label)


def _notes(value: Any, label: str) -> None:
    if isinstance(value, str):
        _text(value, label)
        return
    notes = _array(value, label)
    if not notes:
        _fail(f"{label} must not be empty.")
    for index, note in enumerate(notes):
        _text(note, f"{label}[{index}]")


def _rights(value: Any, label: str) -> dict[str, bool | None]:
    rights = _object(value, label)
    required = {
        "redistribution_allowed",
        "derivatives_allowed",
        "commercial_use_allowed",
    }
    _exact_keys(rights, label, required)
    for key in required:
        if rights[key] is not None and type(rights[key]) is not bool:
            _fail(f"{label}.{key} must be true, false or null.")
    return rights


def _views(value: Any, label: str, required: tuple[str, ...]) -> list[str]:
    views = [
        _identifier(item, f"{label}[{index}]")
        for index, item in enumerate(_array(value, label))
    ]
    if len(views) != len(set(views)):
        _fail(f"{label} repeats a view.")
    unsupported = sorted(set(views) - set(required))
    if unsupported:
        _fail(f"{label} contains unsupported views: {', '.join(unsupported)}.")
    return views


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise MapPlotterError(
            f"Cannot read qualified source asset {path}: {exc}"
        ) from exc
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _binding_fail(f"cannot hash non-canonical JSON data: {exc}")
    return hashlib.sha256(payload).hexdigest()


def _resolved_qualified_asset(
    relative: PurePosixPath,
    *,
    prefixes: tuple[PurePosixPath, ...],
    label: str,
) -> Path:
    """Resolve one qualified asset without following any repository symlink.

    Lexical prefix checks alone are insufficient: an apparently confined path
    can be a symlink to bytes elsewhere.  Qualified evidence must be ordinary
    repository files all the way from the repository root to the leaf.
    """

    repository_root = REPOSITORY_ROOT.resolve()
    cursor = repository_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            _fail(f"{label}.asset_path contains a symlink: {relative.as_posix()}.")
    try:
        resolved = cursor.resolve(strict=True)
    except OSError as exc:
        _fail(f"{label}.asset_path cannot be resolved: {exc}")
    allowed_roots = tuple(
        repository_root.joinpath(*prefix.parts).resolve(strict=False)
        for prefix in prefixes
    )
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        roots = " or ".join(f"{prefix.as_posix()}/" for prefix in prefixes)
        _fail(f"{label}.asset_path resolves outside {roots}.")
    if not resolved.is_file():
        _fail(f"{label}.asset_path is not a regular file: {relative.as_posix()}.")
    return resolved


def _validate_qualified_asset_type(path: Path, *, label: str) -> None:
    """Require an allowed extension and matching, minimally valid file type."""

    suffix = path.suffix.lower()
    if suffix not in {".jp2", ".pdf", ".png", ".svg"}:
        _fail(
            f"{label}.asset_path uses unsupported qualified asset type {suffix!r}; "
            "expected PNG, PDF, JP2 or SVG."
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        _fail(f"{label}.asset_path cannot be inspected: {exc}")
    if suffix == ".png":
        if (
            len(payload) < 45
            or payload[:8] != b"\x89PNG\r\n\x1a\n"
            or int.from_bytes(payload[8:12], "big") != 13
            or payload[12:16] != b"IHDR"
            or zlib.crc32(payload[12:29]) != int.from_bytes(payload[29:33], "big")
            or b"IEND" not in payload[-20:]
        ):
            _fail(f"{label}.asset_path is not a valid PNG container.")
        return
    if suffix == ".pdf":
        if (
            len(payload) < 100
            or not payload.startswith(b"%PDF-")
            or b"%%EOF" not in payload[-1024:]
        ):
            _fail(f"{label}.asset_path is not a complete PDF container.")
        return
    if suffix == ".jp2":
        if len(payload) < 32 or not payload.startswith(
            b"\x00\x00\x00\x0cjP  \r\n\x87\n"
        ):
            _fail(f"{label}.asset_path is not a JPEG 2000 JP2 container.")
        return

    lowered = payload.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered or b"<html" in lowered:
        _fail(f"{label}.asset_path SVG contains forbidden HTML/DTD content.")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        _fail(f"{label}.asset_path is not well-formed SVG XML: {exc}")
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        _fail(
            f"{label}.asset_path SVG root must be the namespaced SVG element."
        )


def _expected_subject_ids(collection: str) -> set[str]:
    path = COLLECTION_CATALOG_PATHS[collection]
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MapPlotterError(
            f"Cannot read collection identity catalog {path}: {exc}"
        ) from exc
    subjects = catalog.get("subjects") if isinstance(catalog, dict) else None
    if not isinstance(subjects, list):
        _fail(f"identity catalog {path.name} has no subjects array.")
    identifiers = {
        str(subject.get("id"))
        for subject in subjects
        if isinstance(subject, dict) and subject.get("id")
    }
    if len(identifiers) != len(subjects):
        _fail(f"identity catalog {path.name} repeats or omits a subject id.")
    return identifiers


def _source(
    raw: Any,
    *,
    label: str,
    collection: str,
    required_views: tuple[str, ...],
    qualified: bool,
) -> tuple[dict[str, Any], list[str]]:
    source = _object(raw, label)
    common = {
        "id",
        "publisher",
        "title",
        "record_url",
        "asset_url",
        "source_type",
        "license",
        "license_url",
        "rights",
        "view_coverage",
        "accuracy_note",
        "notes",
    }
    required = common | ({"asset_path", "asset_sha256"} if qualified else set())
    _exact_keys(source, label, required)
    _identifier(source["id"], f"{label}.id")
    for key in ("publisher", "title", "source_type", "accuracy_note"):
        _text(source[key], f"{label}.{key}")
    _https(source["record_url"], f"{label}.record_url")
    _notes(source["notes"], f"{label}.notes")
    if qualified:
        _text(source["license"], f"{label}.license")
        _https(source["asset_url"], f"{label}.asset_url")
        _https(source["license_url"], f"{label}.license_url")
    else:
        if source["license"] is not None:
            _text(source["license"], f"{label}.license")
        _optional_https(source["asset_url"], f"{label}.asset_url")
        _optional_https(source["license_url"], f"{label}.license_url")
    rights = _rights(source["rights"], f"{label}.rights")
    coverage = _views(source["view_coverage"], f"{label}.view_coverage", required_views)
    if qualified and not coverage:
        _fail(f"{label}.view_coverage must not be empty.")
    if qualified:
        if not all(rights[key] is True for key in rights):
            _fail(
                f"{label} is qualified but does not explicitly allow every required use."
            )
        relative_text = _text(source["asset_path"], f"{label}.asset_path")
        relative = PurePosixPath(relative_text)
        collection_directories = (
            ("boats", "watercraft") if collection == "boats" else (collection,)
        )
        prefixes = tuple(
            PurePosixPath(
                f"contracts/engineering-collections-v2/source_geometry/{directory}"
            )
            for directory in collection_directories
        )
        inside_collection = any(
            relative.parts[: len(prefix.parts)] == prefix.parts for prefix in prefixes
        )
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != relative_text
            or not inside_collection
        ):
            roots = " or ".join(f"{prefix.as_posix()}/" for prefix in prefixes)
            _fail(f"{label}.asset_path must stay inside {roots}.")
        digest = _text(source["asset_sha256"], f"{label}.asset_sha256")
        if _SHA256.fullmatch(digest) is None:
            _fail(f"{label}.asset_sha256 must be a lowercase SHA-256.")
        asset = _resolved_qualified_asset(
            relative,
            prefixes=prefixes,
            label=label,
        )
        _validate_qualified_asset_type(asset, label=label)
        actual = _sha256_file(asset)
        if actual != digest:
            _fail(f"{label}.asset_sha256 changed: expected {digest}, got {actual}.")
    return source, coverage


def load_source_audit(collection: str, path: Path | None = None) -> dict[str, Any]:
    """Load and validate one collection sourcing audit and all bundled bytes."""

    if collection not in AUDIT_PATHS:
        _fail(f"unknown collection {collection!r}.")
    selected = path or AUDIT_PATHS[collection]
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(f"Cannot read source audit {selected}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(f"Invalid source audit JSON {selected}: {exc}") from exc
    audit = _object(payload, "audit")
    _exact_keys(
        audit,
        "audit",
        {
            "schema_version",
            "audit_id",
            "collection",
            "research_as_of",
            "policy_id",
            "subjects",
        },
    )
    if audit["schema_version"] != 1:
        _fail("schema_version must be 1.")
    _identifier(audit["audit_id"], "audit.audit_id")
    if audit["collection"] != collection:
        _fail(f"audit.collection must be {collection!r}.")
    _text(audit["research_as_of"], "audit.research_as_of")
    if audit["policy_id"] != POLICY_ID:
        _fail(f"audit.policy_id must be {POLICY_ID!r}.")
    expected_views = REQUIRED_VIEWS[collection]
    subjects = _array(audit["subjects"], "audit.subjects")
    if not subjects:
        _fail("audit.subjects must not be empty.")
    subject_ids: list[str] = []
    for index, raw_subject in enumerate(subjects):
        label = f"audit.subjects[{index}]"
        subject = _object(raw_subject, label)
        _exact_keys(
            subject,
            label,
            {
                "id",
                "title",
                "required_views",
                "status",
                "qualified_views",
                "qualified_sources",
                "reference_candidates",
                "blockers",
            },
        )
        subject_ids.append(_identifier(subject["id"], f"{label}.id"))
        _text(subject["title"], f"{label}.title")
        required = _views(
            subject["required_views"], f"{label}.required_views", expected_views
        )
        if tuple(required) != expected_views:
            _fail(f"{label}.required_views must be exactly {list(expected_views)!r}.")
        status = _text(subject["status"], f"{label}.status")
        if status not in STATUS_VALUES:
            _fail(f"{label}.status {status!r} is unsupported.")
        qualified_views = _views(
            subject["qualified_views"], f"{label}.qualified_views", expected_views
        )
        qualified_sources = _array(
            subject["qualified_sources"], f"{label}.qualified_sources"
        )
        candidate_sources = _array(
            subject["reference_candidates"], f"{label}.reference_candidates"
        )
        source_ids: list[str] = []
        covered: set[str] = set()
        for source_index, raw_source in enumerate(qualified_sources):
            source, coverage = _source(
                raw_source,
                label=f"{label}.qualified_sources[{source_index}]",
                collection=collection,
                required_views=expected_views,
                qualified=True,
            )
            source_ids.append(str(source["id"]))
            covered.update(coverage)
        for source_index, raw_source in enumerate(candidate_sources):
            source, _ = _source(
                raw_source,
                label=f"{label}.reference_candidates[{source_index}]",
                collection=collection,
                required_views=expected_views,
                qualified=False,
            )
            source_ids.append(str(source["id"]))
        if len(source_ids) != len(set(source_ids)):
            _fail(f"{label} repeats a source id.")
        if not set(qualified_views).issubset(covered):
            _fail(
                f"{label}.qualified_views must be a subset of qualified source coverage {sorted(covered)!r}."
            )
        complete = set(qualified_views) == set(expected_views)
        if (status == "ready") != complete:
            _fail(f"{label}.status may be ready only with every required view.")
        if status == "partial" and (not qualified_views or complete):
            _fail(f"{label}.status partial needs non-empty incomplete coverage.")
        blockers = [
            _text(item, f"{label}.blockers[{blocker_index}]")
            for blocker_index, item in enumerate(
                _array(subject["blockers"], f"{label}.blockers")
            )
        ]
        if status == "ready" and blockers:
            _fail(f"{label}.blockers must be empty when status is ready.")
        if status != "ready" and not blockers:
            _fail(f"{label}.blockers must explain every non-ready state.")
    if len(subject_ids) != len(set(subject_ids)):
        _fail("audit.subjects repeats a subject id.")
    expected_ids = _expected_subject_ids(collection)
    missing = sorted(expected_ids - set(subject_ids))
    unexpected = sorted(set(subject_ids) - expected_ids)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        _fail(
            "audit subject coverage differs from the collection: "
            + "; ".join(details)
            + "."
        )
    return audit


def source_audit_summary(audit: dict[str, Any]) -> dict[str, Any]:
    """Return stable machine-readable coverage counts for one validated audit."""

    subjects = list(audit["subjects"])
    status_counts = {
        status: sum(subject["status"] == status for subject in subjects)
        for status in sorted(STATUS_VALUES)
    }
    required_view_count = sum(len(subject["required_views"]) for subject in subjects)
    qualified_view_count = sum(len(subject["qualified_views"]) for subject in subjects)
    source_covered_view_count = sum(
        len(
            {
                view
                for source in subject["qualified_sources"]
                for view in source["view_coverage"]
            }
        )
        for subject in subjects
    )
    return {
        "collection": audit["collection"],
        "subject_count": len(subjects),
        "status_counts": status_counts,
        "required_view_count": required_view_count,
        "source_covered_view_count": source_covered_view_count,
        "qualified_view_count": qualified_view_count,
        "complete": status_counts["ready"] == len(subjects),
    }


def known_named_subject_collections() -> dict[str, str]:
    """Return every quarantined real-subject id and its collection."""

    result: dict[str, str] = {}
    for collection in sorted(COLLECTION_CATALOG_PATHS):
        for subject_id in sorted(_expected_subject_ids(collection)):
            previous = result.get(subject_id)
            if previous is not None:
                _binding_fail(
                    f"named subject {subject_id!r} occurs in both {previous!r} "
                    f"and {collection!r}."
                )
            result[subject_id] = collection
    return result


def _normalized_identity_alias(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    ascii_text = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    return "".join(character for character in ascii_text if character.isalnum())


@lru_cache(maxsize=1)
def _catalog_identity_aliases() -> tuple[
    dict[str, tuple[str, str]],
    dict[str, set[tuple[str, str]]],
]:
    by_id: dict[str, tuple[str, str]] = {}
    by_alias: dict[str, set[tuple[str, str]]] = {}
    for collection, path in sorted(COLLECTION_CATALOG_PATHS.items()):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _binding_fail(f"cannot read identity catalog {path}: {exc}")
        subjects = payload.get("subjects") if isinstance(payload, dict) else None
        if not isinstance(subjects, list):
            _binding_fail(f"identity catalog {path} has no subjects array.")
        for raw in subjects:
            if not isinstance(raw, dict):
                _binding_fail(f"identity catalog {path} has a malformed subject.")
            subject_id = str(raw.get("id", ""))
            identity = raw.get("identity")
            if (
                _STABLE_ID.fullmatch(subject_id) is None
                or not isinstance(identity, dict)
            ):
                _binding_fail(f"identity catalog {path} has invalid identity data.")
            key = (subject_id, collection)
            by_id[subject_id] = key
            maker = identity.get("manufacturer") or identity.get("builder")
            model = identity.get("model")
            aliases = {
                _normalized_identity_alias(raw.get("title")),
                _normalized_identity_alias(model),
                _normalized_identity_alias(f"{maker or ''} {model or ''}"),
            }
            for alias in aliases - {""}:
                by_alias.setdefault(alias, set()).add(key)
    return by_id, by_alias


def match_named_subject_identity(value: Any) -> tuple[str, str] | None:
    """Resolve canonical collection identity even when a catalog changes its id.

    This deliberately uses exact normalized aliases, not fuzzy matching.  It
    catches relabelled demonstrators while avoiding guesses about unrelated
    owner-supplied subjects.
    """

    if not isinstance(value, dict):
        _binding_fail("named-subject identity probe must be an object.")
    by_id, by_alias = _catalog_identity_aliases()
    matches: set[tuple[str, str]] = set()
    subject_id = value.get("id")
    if isinstance(subject_id, str) and subject_id in by_id:
        matches.add(by_id[subject_id])
    identity = value.get("identity")
    maker: Any = None
    model: Any = None
    if isinstance(identity, dict):
        maker = identity.get("manufacturer") or identity.get("builder")
        model = identity.get("model")
    aliases = {
        _normalized_identity_alias(value.get("title")),
        _normalized_identity_alias(value.get("document_title")),
        _normalized_identity_alias(model),
        _normalized_identity_alias(f"{maker or ''} {model or ''}"),
    }
    for alias in aliases - {""}:
        matches.update(by_alias.get(alias, set()))
    if len(matches) > 1:
        identities = ", ".join(
            f"{collection}/{canonical_id}"
            for canonical_id, collection in sorted(matches)
        )
        _binding_fail(
            "record combines conflicting named collection identities: "
            f"{identities}."
        )
    return next(iter(matches), None)


def release_view_sha256(view: Any) -> str:
    """Hash one exact, already-validated technical record view."""

    return _canonical_sha256(view)


def release_record_sha256(record: Any) -> str:
    """Hash every field in one independently validated technical record."""

    from .technical import validate_technical_record

    return _canonical_sha256(validate_technical_record(record))


def is_approved_unbound_demonstrator(record: Any) -> bool:
    """Return true only for an exact code-reviewed built-in demo record."""

    if not isinstance(record, dict):
        return False
    subject_id = record.get("id")
    expected = UNBOUND_DEMONSTRATOR_RECORD_SHA256.get(subject_id)
    return expected is not None and release_record_sha256(record) == expected


def _checked_release_file_path(
    path: Path,
    *,
    root: Path,
    label: str,
) -> Path:
    lexical = path if path.is_absolute() else REPOSITORY_ROOT / path
    if ".." in lexical.parts:
        _binding_fail(f"{label} path is not normalized: {path}.")
    candidate = lexical.absolute()
    checked_root = root.absolute()
    try:
        relative = candidate.relative_to(checked_root)
    except ValueError:
        _binding_fail(f"{label} path must stay inside {root}.")
    cursor = checked_root
    if cursor.is_symlink():
        _binding_fail(f"{label} root is a symlink: {checked_root}.")
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            _binding_fail(f"{label} path contains a symlink: {path}.")
    try:
        resolved_root = checked_root.resolve(strict=True)
        resolved = cursor.resolve(strict=True)
    except OSError as exc:
        _binding_fail(f"cannot resolve {label} path {path}: {exc}")
    if not resolved.is_relative_to(resolved_root):
        _binding_fail(f"{label} path resolves outside {root}.")
    if not resolved.is_file():
        _binding_fail(f"{label} path is not a regular file: {path}.")
    return resolved


def _checked_release_binding_path(path: Path) -> Path:
    return _checked_release_file_path(
        path,
        root=RELEASE_BINDING_ROOT,
        label="release binding",
    )


def _load_release_binding(path: Path) -> tuple[dict[str, Any], Path]:
    selected = _checked_release_binding_path(path)
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(f"Cannot read engineering release binding {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(f"Invalid engineering release binding JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        _binding_fail(f"binding {path} must be an object.")
    required = {
        "schema_version",
        "policy_id",
        "collection",
        "subject_id",
        "audit_sha256",
        "record_sha256",
        "record_geometry_sha256",
        "views",
    }
    missing = sorted(required - set(payload))
    unexpected = sorted(set(payload) - required)
    if missing:
        _binding_fail(f"binding {path} is missing fields: {', '.join(missing)}.")
    if unexpected:
        _binding_fail(
            f"binding {path} has unsupported fields: {', '.join(unexpected)}."
        )
    if payload["schema_version"] != 1:
        _binding_fail(f"binding {path} schema_version must be 1.")
    if payload["policy_id"] != POLICY_ID:
        _binding_fail(f"binding {path} policy_id must be {POLICY_ID!r}.")
    collection = payload["collection"]
    if collection not in AUDIT_PATHS:
        _binding_fail(f"binding {path} has unknown collection {collection!r}.")
    subject_id = payload["subject_id"]
    if not isinstance(subject_id, str) or _STABLE_ID.fullmatch(subject_id) is None:
        _binding_fail(f"binding {path} has invalid subject_id.")
    expected_path = (
        RELEASE_BINDING_ROOT / str(collection) / f"{subject_id}.json"
    ).absolute()
    if selected != expected_path.resolve(strict=True):
        _binding_fail(
            f"binding for {subject_id!r} must use canonical path {expected_path}."
        )
    activation_key = f"{collection}/{subject_id}"
    activated_digest = ACTIVATED_RELEASE_BINDING_SHA256.get(activation_key)
    actual_binding_digest = _sha256_file(selected)
    if activated_digest is None:
        _binding_fail(
            f"binding {activation_key!r} is not activated by the code-reviewed "
            "release allowlist."
        )
    if actual_binding_digest != activated_digest:
        _binding_fail(
            f"activated binding {activation_key!r} changed: expected "
            f"{activated_digest}, got {actual_binding_digest}."
        )
    for key in ("audit_sha256", "record_sha256", "record_geometry_sha256"):
        value = payload[key]
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            _binding_fail(f"binding {path} field {key} must be a lowercase SHA-256.")
    if not isinstance(payload["views"], list) or not payload["views"]:
        _binding_fail(f"binding {path} views must be a non-empty array.")
    return payload, selected


def _release_report_reference(
    value: Any,
    *,
    label: str,
    collection: str,
    subject_id: str,
    view_type: str,
    report_type: str,
) -> tuple[dict[str, Any], Path]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        _binding_fail(f"{label} must contain exactly path and sha256.")
    relative_text = value["path"]
    digest = value["sha256"]
    if not isinstance(relative_text, str) or not relative_text:
        _binding_fail(f"{label}.path must be non-empty text.")
    relative = PurePosixPath(relative_text)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != relative_text
    ):
        _binding_fail(f"{label}.path must be a canonical repository-relative path.")
    expected = (
        RELEASE_EVIDENCE_ROOT
        / collection
        / subject_id
        / view_type
        / f"{report_type}.json"
    ).absolute()
    candidate = (REPOSITORY_ROOT / Path(*relative.parts)).absolute()
    if candidate != expected:
        _binding_fail(f"{label}.path must be {expected}.")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        _binding_fail(f"{label}.sha256 must be a lowercase SHA-256.")
    selected = _checked_release_file_path(
        candidate,
        root=RELEASE_EVIDENCE_ROOT,
        label=f"{report_type} report",
    )
    actual = _sha256_file(selected)
    if actual != digest:
        _binding_fail(
            f"{label}.sha256 changed: expected {digest}, got {actual}."
        )
    try:
        report = json.loads(selected.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(
            f"Cannot read engineering release report {selected}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Invalid engineering release report JSON {selected}: {exc}"
        ) from exc
    if not isinstance(report, dict):
        _binding_fail(f"{label} report must be an object.")
    return report, selected


def _release_string_list(
    value: Any,
    *,
    label: str,
    allow_empty: bool,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "an array" if allow_empty else "a non-empty array"
        _binding_fail(f"{label} must be {qualifier}.")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _binding_fail(f"{label}[{index}] must be non-empty text.")
        result.append(item)
    if len(result) != len(set(result)):
        _binding_fail(f"{label} repeats an item.")
    return result


def _release_transform_ledger(
    value: Any,
    *,
    label: str,
    allow_empty: bool,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "an array" if allow_empty else "a non-empty array"
        _binding_fail(f"{label} must be {qualifier}.")
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(item, dict) or set(item) != {"operation", "parameters"}:
            _binding_fail(
                f"{item_label} must contain exactly operation and parameters."
            )
        if item["operation"] not in _PERMITTED_TRANSFORMS:
            _binding_fail(
                f"{item_label}.operation {item['operation']!r} is not permitted."
            )
        if not isinstance(item["parameters"], dict):
            _binding_fail(f"{item_label}.parameters must be an object.")
        _canonical_sha256(item["parameters"])
    return value


def _release_number(
    value: Any,
    *,
    label: str,
    positive: bool,
) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        _binding_fail(f"{label} must be a finite number.")
    result = float(value)
    if result < 0 or (positive and result <= 0):
        comparison = "greater than zero" if positive else "non-negative"
        _binding_fail(f"{label} must be {comparison}.")
    return result


def _verify_physical_proof_artifact(
    report: dict[str, Any],
    *,
    label: str,
    collection: str,
    subject_id: str,
    view_type: str,
) -> None:
    relative_text = report["proof_artifact_path"]
    digest = report["proof_artifact_sha256"]
    if not isinstance(relative_text, str) or not relative_text:
        _binding_fail(f"{label}.proof_artifact_path must be non-empty text.")
    relative = PurePosixPath(relative_text)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != relative_text
    ):
        _binding_fail(
            f"{label}.proof_artifact_path must be canonical and repository-relative."
        )
    candidate = (REPOSITORY_ROOT / Path(*relative.parts)).absolute()
    expected_directory = (
        RELEASE_EVIDENCE_ROOT / collection / subject_id / view_type
    ).absolute()
    if (
        candidate.parent != expected_directory
        or not candidate.name.startswith("physical-proof-artifact.")
        or candidate.suffix.lower() not in {".jpeg", ".jpg", ".pdf", ".png"}
    ):
        _binding_fail(
            f"{label}.proof_artifact_path must name physical-proof-artifact.* "
            f"inside {expected_directory}."
        )
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        _binding_fail(
            f"{label}.proof_artifact_sha256 must be a lowercase SHA-256."
        )
    selected = _checked_release_file_path(
        candidate,
        root=RELEASE_EVIDENCE_ROOT,
        label="physical proof artifact",
    )
    actual = _sha256_file(selected)
    if actual != digest:
        _binding_fail(
            f"{label}.proof_artifact_sha256 changed: expected {digest}, got {actual}."
        )
    try:
        proof_bytes = selected.read_bytes()
    except OSError as exc:
        _binding_fail(f"cannot inspect physical proof artifact {selected}: {exc}")
    signature = proof_bytes[:8]
    suffix = selected.suffix.lower()
    signature_matches = (
        (suffix == ".png" and signature == b"\x89PNG\r\n\x1a\n")
        or (suffix in {".jpg", ".jpeg"} and signature.startswith(b"\xff\xd8\xff"))
        or (suffix == ".pdf" and signature.startswith(b"%PDF-"))
    )
    if not signature_matches:
        _binding_fail(
            f"{label}.proof_artifact_path does not contain the declared raster "
            "or PDF physical-test evidence type."
        )
    if suffix == ".png":
        if (
            len(proof_bytes) < 45
            or int.from_bytes(proof_bytes[8:12], "big") != 13
            or proof_bytes[12:16] != b"IHDR"
            or b"IEND" not in proof_bytes[-20:]
        ):
            _binding_fail(f"{label} physical proof PNG has no valid IHDR header.")
        ihdr_crc = int.from_bytes(proof_bytes[29:33], "big")
        if zlib.crc32(proof_bytes[12:29]) != ihdr_crc:
            _binding_fail(f"{label} physical proof PNG has an invalid IHDR CRC.")
        width = int.from_bytes(proof_bytes[16:20], "big")
        height = int.from_bytes(proof_bytes[20:24], "big")
        if width < 256 or height < 256:
            _binding_fail(
                f"{label} physical proof raster must be at least 256 x 256 pixels."
            )
    elif suffix in {".jpg", ".jpeg"}:
        dimensions: tuple[int, int] | None = None
        offset = 2
        start_of_frame_markers = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while offset + 9 <= len(proof_bytes):
            if proof_bytes[offset] != 0xFF:
                offset += 1
                continue
            marker = proof_bytes[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(proof_bytes):
                break
            segment_length = int.from_bytes(proof_bytes[offset : offset + 2], "big")
            if segment_length < 2 or offset + segment_length > len(proof_bytes):
                break
            if marker in start_of_frame_markers and segment_length >= 7:
                height = int.from_bytes(proof_bytes[offset + 3 : offset + 5], "big")
                width = int.from_bytes(proof_bytes[offset + 5 : offset + 7], "big")
                dimensions = (width, height)
                break
            offset += segment_length
        if dimensions is None:
            _binding_fail(f"{label} physical proof JPEG has no frame dimensions.")
        if dimensions[0] < 256 or dimensions[1] < 256:
            _binding_fail(
                f"{label} physical proof raster must be at least 256 x 256 pixels."
            )
    elif len(proof_bytes) < 100 or b"%%EOF" not in proof_bytes[-1024:]:
        _binding_fail(f"{label} physical proof PDF is incomplete.")


def _verify_release_report(
    value: Any,
    *,
    label: str,
    report_type: str,
    collection: str,
    subject_id: str,
    view_id: str,
    view_type: str,
    view_geometry_sha256: str,
    qualified_evidence: list[dict[str, str]],
    source_path_ids: list[str],
    format_id: str,
) -> None:
    report, _ = _release_report_reference(
        value,
        label=label,
        collection=collection,
        subject_id=subject_id,
        view_type=view_type,
        report_type=report_type,
    )
    base_fields = {
        "schema_version",
        "policy_id",
        "report_type",
        "collection",
        "subject_id",
        "view_id",
        "view_type",
        "view_geometry_sha256",
        "qualified_evidence",
        "passed",
    }
    report_fields = {
        "extraction": {
            "method",
            "extraction_sha256",
            "transform_ledger",
            "source_path_ids",
            "omitted_source_path_ids",
        },
        "registration": {
            "transforms",
            "control_points",
            "source_unit",
            "measured_max_error_source_units",
            "tolerance_source_units",
            "measured_max_error_mm",
            "tolerance_mm",
            "sample_count",
            "registration_sha256",
        },
        "physical-proof": {
            "supported_formats",
            "pen_inventory_id",
            "minimum_nib_mm",
            "paper_stock_id",
            "tested_at",
            "unresolved_feature_count",
            "proof_artifact_path",
            "proof_artifact_sha256",
        },
    }
    required = base_fields | report_fields[report_type]
    if set(report) != required:
        _binding_fail(
            f"{label} report must contain exactly {sorted(required)!r}."
        )
    expected = {
        "schema_version": 1,
        "policy_id": POLICY_ID,
        "report_type": report_type,
        "collection": collection,
        "subject_id": subject_id,
        "view_id": view_id,
        "view_type": view_type,
        "view_geometry_sha256": view_geometry_sha256,
        "qualified_evidence": qualified_evidence,
        "passed": True,
    }
    for key, expected_value in expected.items():
        if report[key] != expected_value:
            _binding_fail(
                f"{label} report field {key} does not match the bound view."
            )

    if report_type == "extraction":
        if (
            not isinstance(report["method"], str)
            or _STABLE_ID.fullmatch(report["method"]) is None
        ):
            _binding_fail(f"{label}.method must be a stable method id.")
        if report["extraction_sha256"] != view_geometry_sha256:
            _binding_fail(
                f"{label}.extraction_sha256 must equal the exact view digest."
            )
        _release_transform_ledger(
            report["transform_ledger"],
            label=f"{label}.transform_ledger",
            allow_empty=True,
        )
        reported_paths = _release_string_list(
            report["source_path_ids"],
            label=f"{label}.source_path_ids",
            allow_empty=False,
        )
        if reported_paths != source_path_ids:
            _binding_fail(
                f"{label}.source_path_ids must exactly match the rendered paths."
            )
        omitted = _release_string_list(
            report["omitted_source_path_ids"],
            label=f"{label}.omitted_source_path_ids",
            allow_empty=True,
        )
        if set(omitted) & set(reported_paths):
            _binding_fail(
                f"{label}.omitted_source_path_ids overlaps retained source paths."
            )
        return

    if report_type == "registration":
        transforms = _release_transform_ledger(
            report["transforms"],
            label=f"{label}.transforms",
            allow_empty=False,
        )
        if not isinstance(report["source_unit"], str) or not report["source_unit"].strip():
            _binding_fail(f"{label}.source_unit must be non-empty text.")
        error_source = _release_number(
            report["measured_max_error_source_units"],
            label=f"{label}.measured_max_error_source_units",
            positive=False,
        )
        tolerance_source = _release_number(
            report["tolerance_source_units"],
            label=f"{label}.tolerance_source_units",
            positive=True,
        )
        error_mm = _release_number(
            report["measured_max_error_mm"],
            label=f"{label}.measured_max_error_mm",
            positive=False,
        )
        tolerance_mm = _release_number(
            report["tolerance_mm"],
            label=f"{label}.tolerance_mm",
            positive=True,
        )
        from .pens import ACTUAL_PEN_INVENTORY

        registration_tolerance_ceiling_mm = min(
            pen.nominal_nib_mm for pen in ACTUAL_PEN_INVENTORY.pens
        ) / 2.0
        if tolerance_mm > registration_tolerance_ceiling_mm:
            _binding_fail(
                f"{label}.tolerance_mm exceeds the policy ceiling of "
                f"{registration_tolerance_ceiling_mm:g} mm."
            )
        if error_source > tolerance_source or error_mm > tolerance_mm:
            _binding_fail(f"{label} measured error exceeds its tolerance.")
        sample_count = report["sample_count"]
        if type(sample_count) is not int or sample_count < 3:
            _binding_fail(f"{label}.sample_count must be an integer of at least 3.")
        control_points = report["control_points"]
        if not isinstance(control_points, list) or len(control_points) != sample_count:
            _binding_fail(
                f"{label}.control_points count must equal sample_count."
            )
        control_point_ids: set[str] = set()
        residual_source_values: list[float] = []
        residual_mm_values: list[float] = []
        for index, point in enumerate(control_points):
            point_label = f"{label}.control_points[{index}]"
            required_point_fields = {
                "id",
                "source_point",
                "registered_point",
                "residual_source_units",
                "residual_mm",
            }
            if not isinstance(point, dict) or set(point) != required_point_fields:
                _binding_fail(
                    f"{point_label} must contain exactly "
                    f"{sorted(required_point_fields)!r}."
                )
            point_id = point["id"]
            if not isinstance(point_id, str) or _STABLE_ID.fullmatch(point_id) is None:
                _binding_fail(f"{point_label}.id must be a stable id.")
            if point_id in control_point_ids:
                _binding_fail(f"{label}.control_points repeats id {point_id!r}.")
            control_point_ids.add(point_id)
            checked_coordinates: list[tuple[float, float]] = []
            for field in ("source_point", "registered_point"):
                coordinates = point[field]
                if not isinstance(coordinates, list) or len(coordinates) != 2:
                    _binding_fail(f"{point_label}.{field} must be [x, y].")
                if any(
                    type(coordinate) not in {int, float}
                    or not math.isfinite(float(coordinate))
                    for coordinate in coordinates
                ):
                    _binding_fail(
                        f"{point_label}.{field} coordinates must be finite numbers."
                    )
                checked_coordinates.append(
                    (
                        float(coordinates[0]),
                        float(coordinates[1]),
                    )
                )
            residual_source = _release_number(
                point["residual_source_units"],
                label=f"{point_label}.residual_source_units",
                positive=False,
            )
            residual_mm = _release_number(
                point["residual_mm"],
                label=f"{point_label}.residual_mm",
                positive=False,
            )
            source_point, registered_point = checked_coordinates
            calculated_source_residual = math.hypot(
                source_point[0] - registered_point[0],
                source_point[1] - registered_point[1],
            )
            if not math.isclose(
                residual_source,
                calculated_source_residual,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                _binding_fail(
                    f"{point_label}.residual_source_units does not match its points."
                )
            residual_source_values.append(residual_source)
            residual_mm_values.append(residual_mm)
        if not math.isclose(
            max(residual_source_values), error_source, rel_tol=1e-9, abs_tol=1e-9
        ) or not math.isclose(
            max(residual_mm_values), error_mm, rel_tol=1e-9, abs_tol=1e-9
        ):
            _binding_fail(
                f"{label} measured maximum errors do not match its control points."
            )
        digest = report["registration_sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            _binding_fail(f"{label}.registration_sha256 is invalid.")
        registration_payload = {
            "transforms": transforms,
            "control_points": control_points,
            "source_unit": report["source_unit"],
            "measured_max_error_source_units": report[
                "measured_max_error_source_units"
            ],
            "tolerance_source_units": report["tolerance_source_units"],
            "measured_max_error_mm": report["measured_max_error_mm"],
            "tolerance_mm": report["tolerance_mm"],
            "sample_count": sample_count,
        }
        if digest != _canonical_sha256(registration_payload):
            _binding_fail(
                f"{label}.registration_sha256 does not match its measurements."
            )
        return

    supported_formats = _release_string_list(
        report["supported_formats"],
        label=f"{label}.supported_formats",
        allow_empty=False,
    )
    if any(item not in _RELEASE_FORMATS for item in supported_formats):
        _binding_fail(f"{label}.supported_formats contains an unknown format.")
    if supported_formats != [
        item for item in _RELEASE_FORMATS if item in set(supported_formats)
    ]:
        _binding_fail(f"{label}.supported_formats is not in canonical order.")
    if format_id not in supported_formats:
        _binding_fail(
            f"{label} does not physically prove requested format {format_id!r}."
        )
    from .pens import ACTUAL_PEN_INVENTORY

    if report["pen_inventory_id"] != ACTUAL_PEN_INVENTORY.id:
        _binding_fail(
            f"{label}.pen_inventory_id must be "
            f"{ACTUAL_PEN_INVENTORY.id!r}."
        )
    if (
        not isinstance(report["paper_stock_id"], str)
        or _STABLE_ID.fullmatch(report["paper_stock_id"]) is None
    ):
        _binding_fail(f"{label}.paper_stock_id must be a stable id.")
    minimum_nib_mm = _release_number(
        report["minimum_nib_mm"],
        label=f"{label}.minimum_nib_mm",
        positive=True,
    )
    inventory_minimum_nib_mm = min(
        pen.nominal_nib_mm for pen in ACTUAL_PEN_INVENTORY.pens
    )
    if not math.isclose(
        minimum_nib_mm,
        inventory_minimum_nib_mm,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        _binding_fail(
            f"{label}.minimum_nib_mm must equal the actual inventory minimum "
            f"of {inventory_minimum_nib_mm:g} mm."
        )
    tested_at = report["tested_at"]
    if not isinstance(tested_at, str) or not tested_at.strip():
        _binding_fail(f"{label}.tested_at must be non-empty text.")
    try:
        parsed_tested_at = datetime.fromisoformat(tested_at.replace("Z", "+00:00"))
    except ValueError:
        _binding_fail(f"{label}.tested_at must be an ISO-8601 timestamp.")
    if parsed_tested_at.tzinfo is None:
        _binding_fail(f"{label}.tested_at must include a timezone.")
    if type(report["unresolved_feature_count"]) is not int or report[
        "unresolved_feature_count"
    ] != 0:
        _binding_fail(f"{label}.unresolved_feature_count must be exactly zero.")
    _verify_physical_proof_artifact(
        report,
        label=label,
        collection=collection,
        subject_id=subject_id,
        view_type=view_type,
    )


def _binding_view_records(
    binding: dict[str, Any],
    *,
    required_views: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(binding["views"]):
        label = f"binding.views[{index}]"
        if not isinstance(raw, dict):
            _binding_fail(f"{label} must be an object.")
        required = {
            "view_id",
            "view_type",
            "view_geometry_sha256",
            "qualified_evidence",
            "extraction_report",
            "registration_report",
            "physical_proof_report",
        }
        if set(raw) != required:
            _binding_fail(f"{label} must contain exactly {sorted(required)!r}.")
        view_id = raw["view_id"]
        view_type = raw["view_type"]
        digest = raw["view_geometry_sha256"]
        if not isinstance(view_id, str) or _STABLE_ID.fullmatch(view_id) is None:
            _binding_fail(f"{label}.view_id is invalid.")
        if view_type not in required_views:
            _binding_fail(f"{label}.view_type {view_type!r} is unsupported.")
        if view_type in result:
            _binding_fail(f"binding repeats view type {view_type!r}.")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            _binding_fail(f"{label}.view_geometry_sha256 is invalid.")
        evidence = raw["qualified_evidence"]
        if not isinstance(evidence, list) or not evidence:
            _binding_fail(f"{label}.qualified_evidence must be non-empty.")
        seen: set[tuple[str, str]] = set()
        for evidence_index, item in enumerate(evidence):
            evidence_label = f"{label}.qualified_evidence[{evidence_index}]"
            if not isinstance(item, dict) or set(item) != {
                "source_id",
                "asset_sha256",
            }:
                _binding_fail(
                    f"{evidence_label} must contain source_id and asset_sha256."
                )
            source_id = item["source_id"]
            asset_sha256 = item["asset_sha256"]
            if (
                not isinstance(source_id, str)
                or _STABLE_ID.fullmatch(source_id) is None
            ):
                _binding_fail(f"{evidence_label}.source_id is invalid.")
            if (
                not isinstance(asset_sha256, str)
                or _SHA256.fullmatch(asset_sha256) is None
            ):
                _binding_fail(f"{evidence_label}.asset_sha256 is invalid.")
            pair = (source_id, asset_sha256)
            if pair in seen:
                _binding_fail(f"{label}.qualified_evidence repeats {source_id!r}.")
            seen.add(pair)
        result[str(view_type)] = raw
    if tuple(result) != required_views:
        _binding_fail(
            "binding views must be in exact required order "
            f"{list(required_views)!r}."
        )
    return result


def _verify_named_record_binding(
    record: dict[str, Any],
    *,
    collection: str,
    binding: dict[str, Any],
) -> None:
    from .technical import validate_technical_record

    record = validate_technical_record(record)
    subject_id = str(record.get("id", ""))
    if binding["collection"] != collection or binding["subject_id"] != subject_id:
        _binding_fail(
            f"binding identifies {binding['collection']}/{binding['subject_id']}, "
            f"not {collection}/{subject_id}."
        )
    audit_path = AUDIT_PATHS[collection]
    audit = load_source_audit(collection)
    actual_audit_sha256 = _sha256_file(audit_path)
    if binding["audit_sha256"] != actual_audit_sha256:
        _binding_fail(
            f"{subject_id!r} audit SHA-256 changed: expected "
            f"{binding['audit_sha256']}, got {actual_audit_sha256}."
        )
    audit_subject = next(
        (subject for subject in audit["subjects"] if subject["id"] == subject_id),
        None,
    )
    if audit_subject is None:
        _binding_fail(f"{subject_id!r} is absent from the {collection} audit.")
    if audit_subject["status"] != "ready":
        _binding_fail(
            f"known named subject {subject_id!r} is {audit_subject['status']!r}; "
            "only a ready source-audit subject may render."
        )
    required_views = REQUIRED_VIEWS[collection]
    if tuple(audit_subject["qualified_views"]) != required_views:
        _binding_fail(
            f"ready subject {subject_id!r} does not have exact qualified views "
            f"{list(required_views)!r}."
        )

    record_geometry_sha256 = record.get("geometry_sha256")
    actual_record_sha256 = _canonical_sha256(record)
    if binding["record_sha256"] != actual_record_sha256:
        _binding_fail(
            f"complete validated record for {subject_id!r} does not match its "
            "release binding."
        )
    actual_record_geometry_sha256 = _canonical_sha256(
        {
            "views": record.get("views"),
            "assembly": record.get("assembly"),
            "evolution": record.get("evolution"),
        }
    )
    if record_geometry_sha256 != actual_record_geometry_sha256:
        _binding_fail(f"record {subject_id!r} has an invalid geometry SHA-256.")
    if binding["record_geometry_sha256"] != actual_record_geometry_sha256:
        _binding_fail(
            f"record geometry for {subject_id!r} does not match its release binding."
        )

    record_views = record.get("views")
    if not isinstance(record_views, list):
        _binding_fail(f"record {subject_id!r} has no views array.")
    by_type: dict[str, dict[str, Any]] = {}
    for raw_view in record_views:
        if not isinstance(raw_view, dict):
            _binding_fail(f"record {subject_id!r} contains a malformed view.")
        view_type = str(raw_view.get("type", ""))
        if view_type in by_type:
            _binding_fail(f"record {subject_id!r} repeats view type {view_type!r}.")
        by_type[view_type] = raw_view
    if tuple(by_type) != required_views:
        _binding_fail(
            f"record {subject_id!r} must contain exact required view types "
            f"{list(required_views)!r}."
        )
    binding_views = _binding_view_records(
        binding,
        required_views=required_views,
    )

    audit_sources = {
        str(source["id"]): source for source in audit_subject["qualified_sources"]
    }
    record_sources_raw = record.get("sources")
    if not isinstance(record_sources_raw, list):
        _binding_fail(f"record {subject_id!r} has no sources array.")
    record_sources = {
        str(source.get("id")): source
        for source in record_sources_raw
        if isinstance(source, dict) and source.get("id")
    }
    if len(record_sources_raw) != len(record_sources):
        _binding_fail(
            f"record {subject_id!r} repeats or omits a geometry source id."
        )
    if set(record_sources) != set(audit_sources):
        _binding_fail(
            f"record {subject_id!r} geometry sources must exactly match the "
            "qualified audit sources."
        )
    if record.get("rights_status") not in {"commercial-clear", "owner-supplied"}:
        _binding_fail(f"record {subject_id!r} is not commercially rights-cleared.")

    for source_id, audit_source in audit_sources.items():
        record_source = record_sources[source_id]
        if record_source.get("level") != 4 or record_source.get("kind") not in {
            "verified-repository-reference",
            "verified-repository-vector",
        }:
            _binding_fail(
                f"record source {source_id!r} is not a verified repository source."
            )
        if record_source.get("asset_sha256") != audit_source["asset_sha256"]:
            _binding_fail(
                f"record source {source_id!r} does not match the audited asset hash."
            )
        if record_source.get("asset_path") != audit_source["asset_path"]:
            _binding_fail(
                f"record source {source_id!r} does not use the audited asset path."
            )
        for record_field, audit_field in (
            ("publisher", "publisher"),
            ("url", "asset_url"),
            ("license", "license"),
        ):
            if record_source.get(record_field) != audit_source[audit_field]:
                _binding_fail(
                    f"record source {source_id!r} field {record_field} does not "
                    f"match audited {audit_field}."
                )
        if record_source.get("rights_status") not in {
            "commercial-clear",
            "owner-supplied",
        }:
            _binding_fail(f"record source {source_id!r} is not rights-cleared.")
        if record_source.get("verified_technical") is not True:
            _binding_fail(f"record source {source_id!r} is not verified technical.")
        expected_ids = {
            str(by_type[view_type]["id"])
            for view_type in audit_source["view_coverage"]
        }
        record_view_ids = record_source.get("view_ids", [])
        if (
            not isinstance(record_view_ids, list)
            or len(record_view_ids) != len(set(record_view_ids))
            or set(record_view_ids) != expected_ids
        ):
            _binding_fail(
                f"record source {source_id!r} view_ids do not match audited coverage."
            )

    for view_type in required_views:
        record_view = by_type[view_type]
        binding_view = binding_views[view_type]
        if binding_view["view_id"] != record_view.get("id"):
            _binding_fail(f"binding view id does not match record {view_type!r} view.")
        if binding_view["view_geometry_sha256"] != release_view_sha256(record_view):
            _binding_fail(
                f"binding geometry hash does not match record {view_type!r} view."
            )
        expected_pairs = sorted(
            (source_id, str(source["asset_sha256"]))
            for source_id, source in audit_sources.items()
            if view_type in source["view_coverage"]
        )
        actual_pairs = sorted(
            (str(item["source_id"]), str(item["asset_sha256"]))
            for item in binding_view["qualified_evidence"]
        )
        if not expected_pairs or actual_pairs != expected_pairs:
            _binding_fail(
                f"binding evidence for {subject_id!r} {view_type!r} does not "
                "match the qualified audit sources."
            )
        canonical_evidence = [
            {"source_id": source_id, "asset_sha256": asset_sha256}
            for source_id, asset_sha256 in expected_pairs
        ]
        expected_source_ids = {source_id for source_id, _ in expected_pairs}
        record_view_refs = record_view.get("source_refs", [])
        if (
            not isinstance(record_view_refs, list)
            or len(record_view_refs) != len(set(record_view_refs))
            or set(record_view_refs) != expected_source_ids
        ):
            _binding_fail(
                f"record {view_type!r} source_refs do not match qualified evidence."
            )
        primitives = record_view.get("primitives")
        if not isinstance(primitives, list) or not primitives:
            _binding_fail(f"record {view_type!r} has no source-derived primitives.")
        retained_source_path_ids: set[str] = set()
        for primitive in primitives:
            if not isinstance(primitive, dict):
                _binding_fail(f"record {view_type!r} has a malformed primitive.")
            if primitive.get("evidence") in {"project-authored", "inferred-visible"}:
                _binding_fail(
                    f"record {view_type!r} contains forbidden fallback geometry."
                )
            primitive_refs = primitive.get("source_refs", [])
            refs = set(primitive_refs) if isinstance(primitive_refs, list) else set()
            if (
                not refs
                or len(primitive_refs) != len(refs)
                or not refs.issubset(expected_source_ids)
            ):
                _binding_fail(
                    f"record {view_type!r} primitive references unaudited geometry."
                )
            source_path_ids = primitive.get("source_path_ids")
            if (
                not isinstance(source_path_ids, list)
                or not source_path_ids
                or len(source_path_ids) != len(set(source_path_ids))
            ):
                _binding_fail(
                    f"record {view_type!r} primitive lacks source-path mapping."
                )
            if any(
                not isinstance(source_path_id, str) or not source_path_id.strip()
                for source_path_id in source_path_ids
            ):
                _binding_fail(
                    f"record {view_type!r} primitive has an invalid source-path id."
                )
            retained_source_path_ids.update(source_path_ids)
        canonical_source_path_ids = sorted(retained_source_path_ids)
        for field, report_type in (
            ("extraction_report", "extraction"),
            ("registration_report", "registration"),
            ("physical_proof_report", "physical-proof"),
        ):
            _verify_release_report(
                binding_view[field],
                label=f"binding {subject_id!r} {view_type!r} {field}",
                report_type=report_type,
                collection=collection,
                subject_id=subject_id,
                view_id=str(record_view["id"]),
                view_type=view_type,
                view_geometry_sha256=str(
                    binding_view["view_geometry_sha256"]
                ),
                qualified_evidence=canonical_evidence,
                source_path_ids=canonical_source_path_ids,
                format_id=str(record.get("format_id", "")),
            )


def validate_named_subject_release_bindings(
    records: Sequence[dict[str, Any]],
    binding_paths: Sequence[Path | str] = (),
) -> dict[str, Path]:
    """Fail closed for every known named subject before any file export.

    Only exact code-reviewed built-in demonstrators remain renderable without a
    binding. Every other technical record fails closed. Named car, aircraft and
    boat subjects additionally need an activated canonical checked-in binding
    whose audit, source bytes, reports and exact record all still match.
    """

    bindings: dict[str, tuple[dict[str, Any], Path]] = {}
    for raw_path in binding_paths:
        binding, selected_path = _load_release_binding(Path(raw_path))
        subject_id = str(binding["subject_id"])
        if subject_id in bindings:
            _binding_fail(f"multiple bindings were supplied for {subject_id!r}.")
        bindings[subject_id] = (binding, selected_path)

    selected_ids = {str(record.get("id", "")) for record in records}
    unused = sorted(set(bindings) - selected_ids)
    if unused:
        _binding_fail(
            "release bindings were supplied for unselected subjects: "
            + ", ".join(unused)
            + "."
        )
    for record in records:
        subject_id = str(record.get("id", ""))
        if is_approved_unbound_demonstrator(record):
            continue
        identity_match = match_named_subject_identity(record)
        if identity_match is None:
            _binding_fail(
                f"technical-object record {subject_id!r} is not an immutable "
                "code-reviewed built-in demonstrator and has no supported "
                "canonical v2 release binding; unknown, modified, and "
                "owner-supplied records are export-blocked."
            )
        canonical_subject_id, collection = identity_match
        if subject_id != canonical_subject_id:
            _binding_fail(
                f"record id {subject_id!r} semantically identifies known named "
                f"collection subject {canonical_subject_id!r}; named identities "
                "must use their canonical id and verified v2 release binding."
            )
        binding_entry = bindings.get(subject_id)
        if binding_entry is None:
            audit = load_source_audit(collection)
            audit_subject = next(
                subject for subject in audit["subjects"] if subject["id"] == subject_id
            )
            _binding_fail(
                f"known named collection subject {subject_id!r} cannot render "
                "without a verified v2 release binding; source-audit status is "
                f"{audit_subject['status']!r}."
            )
        binding, _ = binding_entry
        _verify_named_record_binding(
            record,
            collection=collection,
            binding=binding,
        )
    return {
        subject_id: selected_path
        for subject_id, (_, selected_path) in bindings.items()
    }


__all__ = [
    "ACTIVATED_RELEASE_BINDING_SHA256",
    "AUDIT_PATHS",
    "CONTRACT_ROOT",
    "POLICY_ID",
    "RELEASE_BINDING_ROOT",
    "RELEASE_EVIDENCE_ROOT",
    "REQUIRED_VIEWS",
    "STATUS_VALUES",
    "UNBOUND_DEMONSTRATOR_RECORD_SHA256",
    "is_approved_unbound_demonstrator",
    "known_named_subject_collections",
    "load_source_audit",
    "match_named_subject_identity",
    "release_record_sha256",
    "release_view_sha256",
    "source_audit_summary",
    "validate_named_subject_release_bindings",
]

"""Strict, evidence-bearing side-elevation plates for bridges.

The bridge compiler consumes a curated two-dimensional local-metre model.  It
does not derive an elevation from OpenStreetMap, photographs, or a bridge name.
Every plotted member retains its component, evidence, derivation, and source
lineage while the shared niche compiler owns the physical A3 sheet and pens.
"""

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, NoReturn, Sequence

from .furniture import BridgeTypographyLine, bridge_furniture_plan
from .models import MapPlotterError
from .niche_common import (
    ArtworkLayer,
    PlateContext,
    PlateArtwork,
    Rect,
    bridge_pen_id,
    context_for,
    polyline_length_mm,
)
from .vector_path import Affine2D, VectorPath


CATALOG_PATH = Path(__file__).with_name("data") / "bridge-plates-v1.json"
CATALOG_ID = "bridge-plates-v1"
FORMAT_ID = "a3-landscape"
FORMAT_SUBJECT_POLICY = "schematic"
# A source profile must devote most of the structural drawing band to one
# published horizontal recognition unit (normally its main span).  This keeps
# a whole multi-span source sheet from being shrunk into an unreadable strip.
SOURCE_PROFILE_RECOGNITION_MINIMUM_DRAWING_FRACTION = 0.75

BRIDGE_PENS = (
    "grey-0-25",
    "blue-0-25",
    "red-0-25",
    "black-0-25",
    "black-0-4",
    "black-0-6",
    "black-1",
)

SUBJECT_KINDS = frozenset(
    {"suspension", "cable-stayed", "arch", "cantilever", "truss", "beam", "bascule"}
)
SOURCE_KINDS = frozenset(
    {
        "heritage-measured-drawing",
        # A historic engineering publication whose elevation is direct source
        # linework but is not asserted to be a modern survey or as-built record.
        "heritage-published-engineering-drawing",
        "official-engineering-record",
        "licensed-cad",
        "survey",
        "in-house-reconstruction",
        # Dimensions published by the structure's own operator or by a statutory
        # heritage record.  Authoritative for the figures it prints, but it is a
        # fact page rather than a drawing, so it can anchor B1 dimensions and
        # never supplies B0 traced geometry.
        "official-published-dimensions",
        # A third-party engineering structures database.  Kept distinct from an
        # official record so a reviewer can see at a glance that the operator did
        # not publish the figure itself.
        "third-party-engineering-database",
    }
)
EVIDENCE_TIERS = frozenset(
    {
        "B0-source-elevation",
        "B1-published-dimension",
        "B2-inferred-schematic",
        "B3-measured",
    }
)
EVIDENCE_STATUSES = frozenset(
    {
        "dimension-schematic-preview",
        "source-derived-elevation-study",
        "measured-elevation-study",
        "mixed-evidence-elevation-study",
    }
)
RIGHTS_STATUSES = frozenset(
    {"no-known-restrictions", "commercial-clear", "project-authored", "review-required"}
)
FIDELITY_STATUSES = frozenset({"source-profile", "dimension-schematic"})
STRUCTURAL_STYLE_ROLES = (
    "construction",
    "context",
    "fine",
    "secondary",
    "primary",
)
_STABLE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")

Point = tuple[float, float]
Stroke = list[Point]


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid bridge plate data: {message}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object.")
    return value


def _array(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "a non-empty" if nonempty else "an"
        _fail(f"{label} must be {qualifier} array.")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text.")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    result = _text(value, label)
    if _STABLE_ID.fullmatch(result) is None:
        _fail(f"{label} must use lowercase letters, digits, and hyphens.")
    return result


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        _fail(f"{label} must be a finite number.")
    return result


def _keys(
    value: dict[str, Any],
    label: str,
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    unexpected = sorted(set(value) - allowed)
    if missing:
        _fail(f"{label} is missing fields: {', '.join(missing)}.")
    if unexpected:
        _fail(f"{label} has unsupported fields: {', '.join(unexpected)}.")


def _point(value: Any, label: str) -> Point:
    raw = _array(value, label)
    if len(raw) != 2:
        _fail(f"{label} must be [x, y].")
    return (_number(raw[0], f"{label}[0]"), _number(raw[1], f"{label}[1]"))


def _validate_source(source: Any, index: int) -> dict[str, Any]:
    label = f"sources[{index}]"
    value = _object(source, label)
    _keys(
        value,
        label,
        required={
            "id",
            "kind",
            "publisher",
            "license",
            "attribution",
            "use",
            "method",
            "url",
            "asset_sha256",
        },
        optional={
            "asset_url",
            "extract_path",
            "extract_sha256",
            "snapshot_date",
            "record_id",
            "rights_advisory",
            "visible_attribution",
        },
    )
    _identifier(value["id"], f"{label}.id")
    kind = _text(value["kind"], f"{label}.kind")
    if kind not in SOURCE_KINDS:
        _fail(f"{label}.kind {kind!r} is unsupported.")
    for key in ("publisher", "license", "attribution", "use", "method"):
        _text(value[key], f"{label}.{key}")
    for key in ("url", "asset_url"):
        if key in value and not _text(value[key], f"{label}.{key}").startswith(
            "https://"
        ):
            _fail(f"{label}.{key} must use HTTPS.")
    digest = _text(value["asset_sha256"], f"{label}.asset_sha256")
    if _SHA256.fullmatch(digest) is None:
        _fail(f"{label}.asset_sha256 must be a lowercase SHA-256.")
    extract_path = value.get("extract_path")
    extract_digest = value.get("extract_sha256")
    if (extract_path is None) != (extract_digest is None):
        _fail(f"{label} must provide extract_path and extract_sha256 together.")
    if extract_path is not None:
        checked_extract_path = Path(_text(extract_path, f"{label}.extract_path"))
        if (
            checked_extract_path.is_absolute()
            or ".." in checked_extract_path.parts
            or checked_extract_path.as_posix() != str(extract_path).replace("\\", "/")
        ):
            _fail(
                f"{label}.extract_path must be a normalized repository-relative path."
            )
        checked_extract_digest = _text(extract_digest, f"{label}.extract_sha256")
        if _SHA256.fullmatch(checked_extract_digest) is None:
            _fail(f"{label}.extract_sha256 must be a lowercase SHA-256.")
    for key in ("snapshot_date", "record_id", "rights_advisory", "visible_attribution"):
        if key in value:
            _text(value[key], f"{label}.{key}")
    return value


def _validate_primitive(primitive: Any, index: int) -> dict[str, Any]:
    label = f"model.primitives[{index}]"
    value = _object(primitive, label)
    _keys(
        value,
        label,
        required={
            "id",
            "component_id",
            "role",
            "style_role",
            "source_refs",
            "evidence_tier",
            "claim_status",
            "derivation",
        },
        optional={
            "points",
            "path",
            "line_style",
            "tolerance_m",
            "source_locator",
            "profile_import_sha256",
            "trace_view_id",
            "trace_view_role",
        },
    )
    _identifier(value["id"], f"{label}.id")
    _identifier(value["component_id"], f"{label}.component_id")
    _text(value["role"], f"{label}.role")
    style = _text(value["style_role"], f"{label}.style_role")
    if style not in STRUCTURAL_STYLE_ROLES:
        _fail(f"{label}.style_role {style!r} is unsupported.")
    tier = _text(value["evidence_tier"], f"{label}.evidence_tier")
    if tier not in EVIDENCE_TIERS:
        _fail(f"{label}.evidence_tier {tier!r} is unsupported.")
    _text(value["claim_status"], f"{label}.claim_status")
    _text(value["derivation"], f"{label}.derivation")
    refs = _array(value["source_refs"], f"{label}.source_refs", nonempty=True)
    for ref_index, ref in enumerate(refs):
        _identifier(ref, f"{label}.source_refs[{ref_index}]")
    if ("points" in value) == ("path" in value):
        _fail(f"{label} must provide exactly one of points or path.")
    if "points" in value:
        points = _array(value["points"], f"{label}.points", nonempty=True)
        if len(points) < 2:
            _fail(f"{label}.points needs at least two points.")
        checked_points = [
            _point(point, f"{label}.points[{i}]") for i, point in enumerate(points)
        ]
        if any(
            math.isclose(first[0], second[0], abs_tol=1e-12)
            and math.isclose(first[1], second[1], abs_tol=1e-12)
            for first, second in zip(checked_points, checked_points[1:], strict=False)
        ):
            _fail(f"{label}.points contains a zero-length internal segment.")
    else:
        try:
            VectorPath.from_dict(value["path"])
        except (KeyError, TypeError, ValueError) as exc:
            _fail(f"{label}.path is not a canonical line/cubic path: {exc}")
    line_style = value.get("line_style", "solid")
    if line_style not in {"solid", "dashed"}:
        _fail(f"{label}.line_style must be 'solid' or 'dashed'.")
    if (
        "tolerance_m" in value
        and _number(value["tolerance_m"], f"{label}.tolerance_m") <= 0
    ):
        _fail(f"{label}.tolerance_m must be positive.")
    if "source_locator" in value:
        _identifier(value["source_locator"], f"{label}.source_locator")
    if "profile_import_sha256" in value:
        digest = _text(value["profile_import_sha256"], f"{label}.profile_import_sha256")
        if _SHA256.fullmatch(digest) is None:
            _fail(f"{label}.profile_import_sha256 must be a lowercase SHA-256.")
    if ("trace_view_id" in value) != ("trace_view_role" in value):
        _fail(f"{label} must declare trace_view_id and trace_view_role together.")
    if "trace_view_id" in value:
        _identifier(value["trace_view_id"], f"{label}.trace_view_id")
        _identifier(value["trace_view_role"], f"{label}.trace_view_role")
    return value


def _repository_relative_path(value: Any, label: str) -> str:
    text = _text(value, label)
    path = Path(text)
    normalized = text.replace("\\", "/")
    if path.is_absolute() or ".." in path.parts or path.as_posix() != normalized:
        _fail(f"{label} must be a normalized repository-relative path.")
    if not normalized.startswith("contracts/bridges-v1/"):
        _fail(f"{label} must remain inside contracts/bridges-v1/.")
    return normalized


def _validate_fidelity(value: Any) -> dict[str, Any]:
    label = "record.fidelity"
    fidelity = _object(value, label)
    _keys(
        fidelity,
        label,
        required={"status", "release_eligible", "statement"},
        optional={"source_profile"},
    )
    status = _text(fidelity["status"], f"{label}.status")
    if status not in FIDELITY_STATUSES:
        _fail(f"{label}.status {status!r} is unsupported.")
    release_eligible = fidelity["release_eligible"]
    if not isinstance(release_eligible, bool):
        _fail(f"{label}.release_eligible must be boolean.")
    _text(fidelity["statement"], f"{label}.statement")
    profile = fidelity.get("source_profile")
    if status == "dimension-schematic":
        if release_eligible:
            _fail("dimension-schematic records cannot be release eligible.")
        if profile is not None:
            _fail("dimension-schematic records cannot declare a source profile.")
        return fidelity
    profile = _object(profile, f"{label}.source_profile")
    _keys(
        profile,
        f"{label}.source_profile",
        required={
            "importer",
            "source_ref",
            "source_asset_sha256",
            "trace_path",
            "trace_sha256",
            "calibration_path",
            "calibration_sha256",
            "profile_path",
            "profile_sha256",
            "trace_view_id",
            "required_components",
            "trace_tolerance_m",
            "paper_error_limit_mm",
        },
    )
    if profile["importer"] != "bridge-source-profile-v1":
        _fail("record.fidelity.source_profile.importer is unsupported.")
    _identifier(profile["source_ref"], f"{label}.source_profile.source_ref")
    _identifier(profile["trace_view_id"], f"{label}.source_profile.trace_view_id")
    for key in (
        "source_asset_sha256",
        "trace_sha256",
        "calibration_sha256",
        "profile_sha256",
    ):
        digest = _text(profile[key], f"{label}.source_profile.{key}")
        if _SHA256.fullmatch(digest) is None:
            _fail(f"{label}.source_profile.{key} must be a lowercase SHA-256.")
    for key in ("trace_path", "calibration_path", "profile_path"):
        _repository_relative_path(profile[key], f"{label}.source_profile.{key}")
    components = _array(
        profile["required_components"],
        f"{label}.source_profile.required_components",
        nonempty=True,
    )
    checked_components = [
        _identifier(component, f"{label}.source_profile.required_components[{index}]")
        for index, component in enumerate(components)
    ]
    if len(checked_components) != len(set(checked_components)):
        _fail("record.fidelity.source_profile.required_components repeats an ID.")
    if (
        _number(
            profile["trace_tolerance_m"],
            f"{label}.source_profile.trace_tolerance_m",
        )
        <= 0
    ):
        _fail("record.fidelity.source_profile.trace_tolerance_m must be positive.")
    paper_limit = _number(
        profile["paper_error_limit_mm"],
        f"{label}.source_profile.paper_error_limit_mm",
    )
    if paper_limit <= 0 or paper_limit > 0.125 + 1e-12:
        _fail(
            "record.fidelity.source_profile.paper_error_limit_mm must be positive "
            "and no greater than half the finest 0.25 mm pen mark."
        )
    return fidelity


def _validate_dimension(dimension: Any, index: int) -> dict[str, Any]:
    label = f"model.dimensions[{index}]"
    value = _object(dimension, label)
    _keys(
        value,
        label,
        required={
            "id",
            "label",
            "orientation",
            "start",
            "end",
            "value_m",
            "source_ref",
            "evidence_tier",
            "qualifier",
        },
        optional={"tolerance_m"},
    )
    _identifier(value["id"], f"{label}.id")
    _text(value["label"], f"{label}.label")
    orientation = _text(value["orientation"], f"{label}.orientation")
    if orientation not in {"horizontal", "vertical"}:
        _fail(f"{label}.orientation must be horizontal or vertical.")
    start = _point(value["start"], f"{label}.start")
    end = _point(value["end"], f"{label}.end")
    if orientation == "horizontal" and not math.isclose(
        start[1], end[1], rel_tol=0.0, abs_tol=1e-9
    ):
        _fail(f"{label} horizontal endpoints must share one y coordinate.")
    if orientation == "vertical" and not math.isclose(
        start[0], end[0], rel_tol=0.0, abs_tol=1e-9
    ):
        _fail(f"{label} vertical endpoints must share one x coordinate.")
    measured = (
        abs(end[0] - start[0])
        if orientation == "horizontal"
        else abs(end[1] - start[1])
    )
    value_m = _number(value["value_m"], f"{label}.value_m")
    if value_m <= 0:
        _fail(f"{label}.value_m must be positive.")
    tier = _text(value["evidence_tier"], f"{label}.evidence_tier")
    if tier not in EVIDENCE_TIERS:
        _fail(f"{label}.evidence_tier {tier!r} is unsupported.")
    if tier == "B3-measured" and "tolerance_m" not in value:
        _fail(
            f"{label} B3-measured evidence requires an explicit positive tolerance_m."
        )
    tolerance = _number(value.get("tolerance_m", 0.001), f"{label}.tolerance_m")
    if tolerance < 0:
        _fail(f"{label}.tolerance_m cannot be negative.")
    if tier == "B3-measured" and tolerance == 0:
        _fail(
            f"{label} B3-measured evidence requires an explicit positive tolerance_m."
        )
    if not math.isclose(measured, value_m, rel_tol=0.0, abs_tol=tolerance):
        _fail(
            f"{label}.value_m disagrees with its {orientation} endpoints "
            f"({value_m:g} versus {measured:g} m)."
        )
    _identifier(value["source_ref"], f"{label}.source_ref")
    _text(value["qualifier"], f"{label}.qualifier")
    return value


def _geometry_payload(model: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "coordinate_system": model["coordinate_system"],
        "view": model["view"],
        "primitives": model["primitives"],
        "dimensions": model["dimensions"],
    }
    if "framing" in model:
        payload["framing"] = model["framing"]
    return payload


def _geometry_sha256(model: dict[str, Any]) -> str:
    payload = json.dumps(
        _geometry_payload(model),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_bridge_record(record: Any) -> dict[str, Any]:
    """Validate and return an isolated copy of one bridge catalog record."""

    value = _object(record, "record")
    _keys(
        value,
        "record",
        required={
            "id",
            "title",
            "subtitle",
            "subject_kind",
            "format_id",
            "location",
            "sources",
            "evidence",
            "fidelity",
            "rights_status",
            "claim_scope",
            "notes",
            "model",
        },
        optional={"credit_line"},
    )
    _identifier(value["id"], "record.id")
    _text(value["title"], "record.title")
    _text(value["subtitle"], "record.subtitle")
    kind = _text(value["subject_kind"], "record.subject_kind")
    if kind not in SUBJECT_KINDS:
        _fail(f"record.subject_kind {kind!r} is unsupported.")
    if value["format_id"] != FORMAT_ID:
        _fail(f"record.format_id must be {FORMAT_ID!r}.")

    location = _object(value["location"], "record.location")
    _keys(location, "record.location", required={"label", "country_code"})
    _text(location["label"], "record.location.label")
    if (
        re.fullmatch(
            r"[A-Z]{2}", _text(location["country_code"], "record.location.country_code")
        )
        is None
    ):
        _fail("record.location.country_code must be an uppercase ISO alpha-2 code.")

    sources = _array(value["sources"], "record.sources", nonempty=True)
    checked_sources = [
        _validate_source(source, index) for index, source in enumerate(sources)
    ]
    source_ids = [str(source["id"]) for source in checked_sources]
    if len(source_ids) != len(set(source_ids)):
        _fail("record.sources repeats a source id.")

    fidelity = _validate_fidelity(value["fidelity"])

    evidence = _object(value["evidence"], "record.evidence")
    _keys(
        evidence,
        "record.evidence",
        required={
            "status",
            "statement",
            "scale_policy",
            "dimension_policy",
            "omissions",
        },
    )
    status = _text(evidence["status"], "record.evidence.status")
    if status not in EVIDENCE_STATUSES:
        _fail(f"record.evidence.status {status!r} is unsupported.")
    _text(evidence["statement"], "record.evidence.statement")
    if (
        _text(evidence["scale_policy"], "record.evidence.scale_policy")
        != "fit-to-field-equal-scale"
    ):
        _fail("record.evidence.scale_policy must be 'fit-to-field-equal-scale'.")
    if _text(evidence["dimension_policy"], "record.evidence.dimension_policy") not in {
        "published-dimensions",
        "measured-dimensions",
    }:
        _fail("record.evidence.dimension_policy is unsupported.")
    omissions = _array(
        evidence["omissions"], "record.evidence.omissions", nonempty=True
    )
    for index, omission in enumerate(omissions):
        _text(omission, f"record.evidence.omissions[{index}]")

    rights = _text(value["rights_status"], "record.rights_status")
    if rights not in RIGHTS_STATUSES:
        _fail(f"record.rights_status {rights!r} is unsupported.")
    _text(value["claim_scope"], "record.claim_scope")
    if "credit_line" in value:
        _text(value["credit_line"], "record.credit_line")
    notes = _array(value["notes"], "record.notes")
    for index, note in enumerate(notes):
        _text(note, f"record.notes[{index}]")

    model = _object(value["model"], "record.model")
    _keys(
        model,
        "record.model",
        required={
            "coordinate_system",
            "view",
            "primitives",
            "dimensions",
            "geometry_sha256",
        },
        optional={"framing"},
    )
    if model["coordinate_system"] != "local-elevation-metre":
        _fail("record.model.coordinate_system must be 'local-elevation-metre'.")
    if model["view"] != "orthographic-side-elevation":
        _fail("record.model.view must be 'orthographic-side-elevation'.")
    primitives = _array(model["primitives"], "record.model.primitives", nonempty=True)
    checked_primitives = [
        _validate_primitive(item, index) for index, item in enumerate(primitives)
    ]
    dimensions = _array(model["dimensions"], "record.model.dimensions", nonempty=True)
    checked_dimensions = [
        _validate_dimension(item, index) for index, item in enumerate(dimensions)
    ]

    framing = model.get("framing")
    if fidelity["status"] == "source-profile":
        framing = _object(framing, "record.model.framing")
        _keys(
            framing,
            "record.model.framing",
            required={
                "mode",
                "label",
                "trace_view_id",
                "focus_component_ids",
                "recognition_dimension_id",
                "source_x_window_m",
                "source_primitive_count",
                "qualified_source_primitive_count",
                "view_primitive_count",
            },
        )
        if framing["mode"] != "source-derived-main-span-window":
            _fail("record.model.framing.mode is unsupported for a source profile.")
        _text(framing["label"], "record.model.framing.label")
        framing_trace_view = _identifier(
            framing["trace_view_id"], "record.model.framing.trace_view_id"
        )
        if framing_trace_view != fidelity["source_profile"]["trace_view_id"]:
            _fail(
                "record.model.framing.trace_view_id disagrees with the source profile."
            )
        focus_raw = _array(
            framing["focus_component_ids"],
            "record.model.framing.focus_component_ids",
            nonempty=True,
        )
        focus_components = [
            _identifier(
                component,
                f"record.model.framing.focus_component_ids[{index}]",
            )
            for index, component in enumerate(focus_raw)
        ]
        if len(focus_components) != len(set(focus_components)):
            _fail("record.model.framing.focus_component_ids repeats an ID.")
        recognition_dimension_id = _identifier(
            framing["recognition_dimension_id"],
            "record.model.framing.recognition_dimension_id",
        )
        recognition_dimensions = [
            dimension
            for dimension in checked_dimensions
            if dimension["id"] == recognition_dimension_id
        ]
        if len(recognition_dimensions) != 1:
            _fail(
                "record.model.framing.recognition_dimension_id must identify "
                "exactly one model dimension."
            )
        if recognition_dimensions[0]["orientation"] != "horizontal":
            _fail("record.model.framing recognition dimension must be horizontal.")
        window_raw = _array(
            framing["source_x_window_m"],
            "record.model.framing.source_x_window_m",
        )
        if len(window_raw) != 2:
            _fail("record.model.framing.source_x_window_m must be [min_x, max_x].")
        window_min = _number(window_raw[0], "record.model.framing.source_x_window_m[0]")
        window_max = _number(window_raw[1], "record.model.framing.source_x_window_m[1]")
        if window_max <= window_min:
            _fail("record.model.framing.source_x_window_m must have positive width.")
        for key in (
            "source_primitive_count",
            "qualified_source_primitive_count",
            "view_primitive_count",
        ):
            count = framing[key]
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                _fail(f"record.model.framing.{key} must be a positive integer.")
        if framing["view_primitive_count"] != len(checked_primitives):
            _fail("record.model.framing.view_primitive_count disagrees with the model.")
        if (
            framing["source_primitive_count"]
            < framing["qualified_source_primitive_count"]
        ):
            _fail(
                "record.model.framing.source_primitive_count cannot be smaller "
                "than qualified_source_primitive_count."
            )
        component_ids = {
            str(primitive["component_id"]) for primitive in checked_primitives
        }
        missing_focus = sorted(set(focus_components) - component_ids)
        if missing_focus:
            _fail(
                "record.model.framing focus components are absent from the view: "
                + ", ".join(missing_focus)
                + "."
            )
    elif framing is not None:
        _fail("dimension-schematic records cannot declare source-derived framing.")

    primitive_bounds_points: list[Point] = []
    for primitive in checked_primitives:
        if "points" in primitive:
            primitive_bounds_points.extend(
                (float(point[0]), float(point[1])) for point in primitive["points"]
            )
        else:
            bounds = VectorPath.from_dict(primitive["path"]).bounds()
            primitive_bounds_points.extend(
                [(bounds.min_x, bounds.min_y), (bounds.max_x, bounds.max_y)]
            )
    if isinstance(framing, dict):
        window_min = float(framing["source_x_window_m"][0])
        window_max = float(framing["source_x_window_m"][1])
        if any(
            point[0] < window_min - 1e-6 or point[0] > window_max + 1e-6
            for point in primitive_bounds_points
        ):
            _fail("record.model geometry leaves its source-derived framing window.")
        focus_set = set(str(value) for value in framing["focus_component_ids"])
        focus_points: list[Point] = []
        for primitive in checked_primitives:
            if str(primitive["component_id"]) not in focus_set:
                continue
            if "points" in primitive:
                focus_points.extend(
                    (float(point[0]), float(point[1])) for point in primitive["points"]
                )
            else:
                bounds = VectorPath.from_dict(primitive["path"]).bounds()
                focus_points.extend(
                    [(bounds.min_x, bounds.min_y), (bounds.max_x, bounds.max_y)]
                )
        if not focus_points or not (
            math.isclose(
                min(point[0] for point in focus_points),
                window_min,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
            and math.isclose(
                max(point[0] for point in focus_points),
                window_max,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            _fail(
                "record.model.framing window must equal the declared focus-component "
                "extents."
            )
    primitive_x = [point[0] for point in primitive_bounds_points]
    primitive_y = [point[1] for point in primitive_bounds_points]
    primitive_span_x = max(primitive_x) - min(primitive_x)
    primitive_span_y = max(primitive_y) - min(primitive_y)
    anchor_margin_x = max(1.0, primitive_span_x * 0.1)
    anchor_margin_y = max(1.0, primitive_span_y * 0.1)
    for dimension in checked_dimensions:
        for endpoint in (dimension["start"], dimension["end"]):
            x, y = float(endpoint[0]), float(endpoint[1])
            if not (
                min(primitive_x) - anchor_margin_x
                <= x
                <= max(primitive_x) + anchor_margin_x
                and min(primitive_y) - anchor_margin_y
                <= y
                <= max(primitive_y) + anchor_margin_y
            ):
                _fail(
                    f"dimension {dimension['id']!r} leaves the model-supported "
                    "extent and would leave the binding map_field."
                )
    primitive_ids = [str(item["id"]) for item in checked_primitives]
    dimension_ids = [str(item["id"]) for item in checked_dimensions]
    if len(primitive_ids) != len(set(primitive_ids)):
        _fail("record.model.primitives repeats an id.")
    if len(dimension_ids) != len(set(dimension_ids)):
        _fail("record.model.dimensions repeats an id.")
    known_sources = set(source_ids)
    sources_by_id = {str(source["id"]): source for source in checked_sources}
    for primitive in checked_primitives:
        unknown = sorted(set(primitive["source_refs"]) - known_sources)
        if unknown:
            _fail(
                f"primitive {primitive['id']!r} cites unknown sources: {', '.join(unknown)}."
            )
        tier = str(primitive["evidence_tier"])
        claim = str(primitive["claim_status"]).casefold()
        if tier == "B1-published-dimension" and not claim.startswith("published-"):
            _fail(
                f"primitive {primitive['id']!r} uses B1 without a published claim status."
            )
        if tier == "B1-published-dimension" and any(
            sources_by_id[ref]["kind"] == "in-house-reconstruction"
            for ref in primitive["source_refs"]
        ):
            _fail(
                f"primitive {primitive['id']!r} cannot promote an in-house reconstruction to B1."
            )
        if tier == "B2-inferred-schematic" and not any(
            marker in claim for marker in ("inference", "schematic", "not-measured")
        ):
            _fail(
                f"primitive {primitive['id']!r} uses B2 without an explicit inferred/schematic claim status."
            )
        if tier in {"B0-source-elevation", "B3-measured"}:
            allowed_kinds = {
                "heritage-measured-drawing",
                "heritage-published-engineering-drawing",
                "licensed-cad",
                "survey",
                "official-engineering-record",
            }
            cited_kinds = {
                str(sources_by_id[ref]["kind"]) for ref in primitive["source_refs"]
            }
            if not cited_kinds <= allowed_kinds:
                _fail(
                    f"primitive {primitive['id']!r} promotes evidence from an unsupported source kind."
                )
            required_marker = "source" if tier == "B0-source-elevation" else "measured"
            if required_marker not in claim:
                _fail(
                    f"primitive {primitive['id']!r} uses {tier} without a {required_marker!r} claim status."
                )
            if "tolerance_m" not in primitive:
                _fail(
                    f"primitive {primitive['id']!r} uses {tier} without a positive tolerance_m."
                )
    for dimension in checked_dimensions:
        if dimension["source_ref"] not in known_sources:
            _fail(f"dimension {dimension['id']!r} cites an unknown source.")
    expected_dimension_tier = (
        "B1-published-dimension"
        if evidence["dimension_policy"] == "published-dimensions"
        else "B3-measured"
    )
    mismatched_dimensions = [
        str(dimension["id"])
        for dimension in checked_dimensions
        if dimension["evidence_tier"] != expected_dimension_tier
    ]
    if mismatched_dimensions:
        _fail(
            f"record.evidence.dimension_policy requires {expected_dimension_tier}; "
            f"mismatched dimensions: {', '.join(mismatched_dimensions)}."
        )
    supplied_digest = _text(model["geometry_sha256"], "record.model.geometry_sha256")
    if _SHA256.fullmatch(supplied_digest) is None:
        _fail("record.model.geometry_sha256 must be a lowercase SHA-256.")
    if supplied_digest != _geometry_sha256(model):
        _fail(
            "record.model.geometry_sha256 disagrees with the canonical elevation model."
        )

    primitive_tiers = {str(item["evidence_tier"]) for item in checked_primitives}
    if status == "measured-elevation-study" and primitive_tiers != {"B3-measured"}:
        _fail("measured elevation studies may contain only B3-measured primitives.")
    if status == "source-derived-elevation-study" and "B3-measured" in primitive_tiers:
        _fail("source-derived elevation studies cannot silently contain B3 geometry.")
    if status == "mixed-evidence-elevation-study" and len(primitive_tiers) < 2:
        _fail("mixed evidence status requires at least two primitive evidence tiers.")
    fidelity_status = str(fidelity["status"])
    if fidelity_status == "dimension-schematic":
        if status != "dimension-schematic-preview":
            _fail(
                "dimension-schematic records must use the "
                "dimension-schematic-preview evidence status."
            )
        if primitive_tiers != {"B2-inferred-schematic"}:
            _fail(
                "dimension-schematic records may contain only B2 inferred primitives."
            )
    else:
        if status == "dimension-schematic-preview":
            _fail(
                "source-profile records cannot use the dimension-schematic-preview "
                "evidence status."
            )
        profile = _object(fidelity["source_profile"], "record.fidelity.source_profile")
        profile_source = str(profile["source_ref"])
        if profile_source not in sources_by_id:
            _fail("record.fidelity.source_profile cites an unknown source_ref.")
        if (
            profile["source_asset_sha256"]
            != sources_by_id[profile_source]["asset_sha256"]
        ):
            _fail(
                "record.fidelity.source_profile.source_asset_sha256 disagrees "
                "with its source record."
            )
        b0_primitives = [
            primitive
            for primitive in checked_primitives
            if primitive["evidence_tier"] == "B0-source-elevation"
        ]
        if not b0_primitives:
            _fail("source-profile records require B0 source-elevation primitives.")
        required_components = set(profile["required_components"])
        actual_components = {
            str(primitive["component_id"]) for primitive in b0_primitives
        }
        missing_components = sorted(required_components - actual_components)
        if missing_components:
            _fail(
                "source-profile record lacks required B0 components: "
                + ", ".join(missing_components)
                + "."
            )
        for primitive in b0_primitives:
            if "source_locator" not in primitive:
                _fail(f"primitive {primitive['id']!r} is B0 but has no source_locator.")
            if primitive.get("profile_import_sha256") != profile["profile_sha256"]:
                _fail(
                    f"primitive {primitive['id']!r} is not bound to the declared "
                    "profile import digest."
                )
            if profile_source not in primitive["source_refs"]:
                _fail(
                    f"primitive {primitive['id']!r} does not cite its source profile."
                )
            if primitive.get("trace_view_id") != profile["trace_view_id"]:
                _fail(
                    f"primitive {primitive['id']!r} is not bound to the declared "
                    "qualified trace view."
                )
            if not math.isclose(
                float(primitive["tolerance_m"]),
                float(profile["trace_tolerance_m"]),
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                _fail(
                    f"primitive {primitive['id']!r} tolerance differs from the "
                    "qualified trace view."
                )
    return copy.deepcopy(value)


def load_bridge_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the packaged bridge catalog (or a supplied catalog) and fail closed."""

    source_path = path or CATALOG_PATH
    try:
        raw = json.loads(source_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(
            f"Could not read bridge catalog {source_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Bridge catalog {source_path} is not valid JSON: {exc}"
        ) from exc
    root = _object(raw, "catalog")
    _keys(root, "catalog", required={"schema_version", "catalog_id", "subjects"})
    if root["schema_version"] != 1:
        _fail("catalog.schema_version must be 1.")
    if root["catalog_id"] != CATALOG_ID:
        _fail(f"catalog.catalog_id must be {CATALOG_ID!r}.")
    records = _array(root["subjects"], "catalog.subjects", nonempty=True)
    checked = [validate_bridge_record(record) for record in records]
    ids = [str(record["id"]) for record in checked]
    if len(ids) != len(set(ids)):
        _fail("catalog.subjects repeats a record id.")
    return checked


def _all_model_points(record: dict[str, Any]) -> list[Point]:
    points: list[Point] = []
    for primitive in record["model"]["primitives"]:
        if "points" in primitive:
            points.extend(
                (float(point[0]), float(point[1])) for point in primitive["points"]
            )
            continue
        bounds = VectorPath.from_dict(primitive["path"]).bounds()
        points.extend(
            [
                (bounds.min_x, bounds.min_y),
                (bounds.max_x, bounds.max_y),
            ]
        )
    for dimension in record["model"]["dimensions"]:
        points.extend(
            [
                (float(dimension["start"][0]), float(dimension["start"][1])),
                (float(dimension["end"][0]), float(dimension["end"][1])),
            ]
        )
    return points


def _fit_points(points: Sequence[Point], rect: Rect):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max_x - min_x
    span_y = max_y - min_y
    if span_x <= 0 or span_y <= 0:
        _fail("model geometry must span both elevation axes.")
    scale = min(rect.width / span_x, rect.height / span_y)
    used_width = span_x * scale
    used_height = span_y * scale
    offset_x = rect.x + (rect.width - used_width) / 2.0
    offset_y = rect.y + (rect.height - used_height) / 2.0

    def transform(point: Point) -> Point:
        return (
            offset_x + (point[0] - min_x) * scale,
            offset_y + (max_y - point[1]) * scale,
        )

    affine = Affine2D(
        a=scale,
        b=0.0,
        c=0.0,
        d=-scale,
        e=offset_x - min_x * scale,
        f=offset_y + max_y * scale,
    )
    return transform, scale, affine


def _dashed_strokes(points: Sequence[Point], *, nib_mm: float) -> list[Stroke]:
    minimum = 3.0 * nib_mm
    dash = 8.0 * nib_mm
    gap = 4.0 * nib_mm
    result: list[Stroke] = []
    for first, second in zip(points, points[1:], strict=False):
        dx = second[0] - first[0]
        dy = second[1] - first[1]
        length = math.hypot(dx, dy)
        if length + 1e-9 < minimum:
            continue
        ux, uy = dx / length, dy / length
        cursor = 0.0
        while cursor < length - 1e-9:
            plotted = min(dash, length - cursor)
            if plotted + 1e-9 < minimum:
                break
            result.append(
                [
                    (first[0] + cursor * ux, first[1] + cursor * uy),
                    (
                        first[0] + (cursor + plotted) * ux,
                        first[1] + (cursor + plotted) * uy,
                    ),
                ]
            )
            cursor += plotted + gap
    return result


def _inside(rect: Rect, point: Point, tolerance: float = 1e-6) -> bool:
    return (
        rect.left - tolerance <= point[0] <= rect.right + tolerance
        and rect.top - tolerance <= point[1] <= rect.bottom + tolerance
    )


def _dimension_strokes(
    start: Point, end: Point, orientation: str, *, gap_mm: float
) -> list[Stroke]:
    tick = 1.0
    if orientation == "horizontal":
        line_y = max(start[1], end[1]) + gap_mm
        return [
            [start, (start[0], line_y)],
            [end, (end[0], line_y)],
            [(start[0], line_y), (end[0], line_y)],
            [
                (start[0] - tick / 2, line_y + tick / 2),
                (start[0] + tick / 2, line_y - tick / 2),
            ],
            [
                (end[0] - tick / 2, line_y + tick / 2),
                (end[0] + tick / 2, line_y - tick / 2),
            ],
        ]
    line_x = min(start[0], end[0]) - gap_mm
    return [
        [start, (line_x, start[1])],
        [end, (line_x, end[1])],
        [(line_x, start[1]), (line_x, end[1])],
        [
            (line_x - tick / 2, start[1] - tick / 2),
            (line_x + tick / 2, start[1] + tick / 2),
        ],
        [
            (line_x - tick / 2, end[1] - tick / 2),
            (line_x + tick / 2, end[1] + tick / 2),
        ],
    ]


def _compact_credit(record: dict[str, Any]) -> str:
    explicit = record.get("credit_line")
    if explicit:
        return str(explicit)
    sources = record["sources"]
    visible = list(
        dict.fromkeys(
            str(source.get("visible_attribution") or source["attribution"])
            for source in sources
        )
    )
    licences = list(dict.fromkeys(str(source["license"]) for source in sources))
    return f"{' / '.join(visible)} | {' / '.join(licences)}"


def _bridge_details(record: dict[str, Any]) -> tuple[str, str, str]:
    fidelity = str(record["fidelity"]["status"])
    return (
        (
            f"SOURCE PROFILE / {record['subject_kind'].upper()} / A3"
            if fidelity == "source-profile"
            else f"DEVELOPMENT SCHEMATIC / {record['subject_kind'].upper()} / A3"
        ),
        "FIT TO FIELD / EQUAL AXES / NOT A SURVEY",
        (
            "TRACE TOLERANCE AND SOURCES IN MANIFEST"
            if fidelity == "source-profile"
            else "SCHEMATIC PREVIEW / NOT RELEASE ART"
        ),
    )


def _bridge_field_label(record: dict[str, Any]) -> str:
    if record["fidelity"]["status"] == "source-profile":
        framing = record["model"].get("framing", {})
        return f"{framing.get('label', 'SIDE ELEVATION')} / SOURCE PROFILE / TRACE CONTROLLED"
    return "SIDE ELEVATION / DIMENSION SCHEMATIC PREVIEW"


def _add_verified_typography_line(
    layer: ArtworkLayer,
    line: BridgeTypographyLine,
    *,
    source_ref: str | None = None,
    attributes: dict[str, str] | None = None,
) -> None:
    metadata = {
        "data-copy": line.copy,
        "data-cap-height-mm": f"{line.cap_height_mm:g}",
        "data-copy-geometry-sha256": line.geometry_sha256,
        "data-text-block-id": line.block_id,
        "data-text-line-index": str(line.line_index),
        "data-text-zone": line.zone,
        "data-typography-authority": "furniture.bridge_furniture_plan-v1",
        **(attributes or {}),
    }
    layer.add_many(
        line.strokes,
        source_ref=source_ref,
        role=line.role,
        attributes=metadata,
    )


def _bridge_context(format_id: str) -> PlateContext:
    """Select the generated bridge composition without inventing local zones."""

    context = context_for(format_id)
    records = context.plate.get("bridge_zones_mm")
    if not isinstance(records, dict):
        _fail("binding plate format does not publish bridge_zones_mm.")
    zones = {
        name: Rect(
            float(record["x"]),
            float(record["y"]),
            float(record["width"]),
            float(record["height"]),
        )
        for name, record in records.items()
    }
    required = {
        "bridge_field_label",
        "bridge_drawing",
        "bridge_dimension_label",
    }
    if set(zones) != required:
        _fail(
            "binding bridge zone set changed: expected "
            f"{sorted(required)}, got {sorted(zones)}."
        )
    return replace(context, zones={**context.zones, **zones})


def _layout_record(
    record: dict[str, Any], context: PlateContext
) -> tuple[list[ArtworkLayer], dict[str, Any]]:
    plate = context.plate
    gap = float(plate["gap_mm"])
    dimension_labels = [
        str(dimension["label"]) for dimension in record["model"]["dimensions"]
    ]
    furniture_plan = bridge_furniture_plan(
        format_id=FORMAT_ID,
        title=str(record["title"]),
        subtitle=str(record["subtitle"]),
        details=_bridge_details(record),
        credit_lines=[
            line.strip()
            for line in _compact_credit(record).split(" | ")
            if line.strip()
        ],
        dimension_labels=dimension_labels,
        field_label=_bridge_field_label(record),
    )
    planned_drawing = furniture_plan.drawing_zone
    drawing = Rect(
        planned_drawing.x_mm,
        planned_drawing.y_mm,
        planned_drawing.width_mm,
        planned_drawing.height_mm,
    )
    if min(drawing.width, drawing.height) <= 0:
        _fail("bridge elevation bands cannot fit the binding map_field.")

    layers = {
        style: ArtworkLayer(
            f"bridge_{style}",
            f"Bridge {style}",
            bridge_pen_id(plate, style),
        )
        for style in STRUCTURAL_STYLE_ROLES
    }
    dimensions = ArtworkLayer(
        "bridge_dimensions",
        "Dimensions and controls",
        bridge_pen_id(plate, "dimension"),
    )
    copy_layer = ArtworkLayer(
        "bridge_copy",
        "Bridge field copy",
        bridge_pen_id(plate, "copy"),
    )

    model_points = _all_model_points(record)
    transform, scale, path_transform = _fit_points(model_points, drawing)
    recognition_dimension_id: str | None = None
    recognition_dimension_drawing_fraction: float | None = None
    framing = record["model"].get("framing")
    if isinstance(framing, dict):
        recognition_dimension_id = str(framing["recognition_dimension_id"])
        recognition_dimension = next(
            dimension
            for dimension in record["model"]["dimensions"]
            if dimension["id"] == recognition_dimension_id
        )
        recognition_start = transform(
            (
                float(recognition_dimension["start"][0]),
                float(recognition_dimension["start"][1]),
            )
        )
        recognition_end = transform(
            (
                float(recognition_dimension["end"][0]),
                float(recognition_dimension["end"][1]),
            )
        )
        recognition_dimension_drawing_fraction = (
            abs(recognition_end[0] - recognition_start[0]) / drawing.width
        )
        if (
            recognition_dimension_drawing_fraction + 1e-9
            < SOURCE_PROFILE_RECOGNITION_MINIMUM_DRAWING_FRACTION
        ):
            _fail(
                f"source-profile recognition dimension {recognition_dimension_id!r} "
                f"uses only {recognition_dimension_drawing_fraction:.3f} of the "
                "drawing width; choose a tighter source-derived hero view."
            )
    omitted_short: list[str] = []
    for primitive in record["model"]["primitives"]:
        layer = layers[str(primitive["style_role"])]
        transformed_path: VectorPath | None = None
        if "path" in primitive:
            transformed_path = VectorPath.from_dict(primitive["path"]).transformed(
                path_transform
            )
            path_bounds = transformed_path.bounds()
            if not (
                _inside(context.field, (path_bounds.min_x, path_bounds.min_y))
                and _inside(context.field, (path_bounds.max_x, path_bounds.max_y))
            ):
                _fail(f"primitive {primitive['id']!r} leaves the binding map_field.")
            transformed = list(transformed_path.flatten(0.01).points)
        else:
            transformed = [
                transform((float(point[0]), float(point[1])))
                for point in primitive["points"]
            ]
            if not all(_inside(context.field, point) for point in transformed):
                _fail(f"primitive {primitive['id']!r} leaves the binding map_field.")
        strokes = (
            _dashed_strokes(transformed, nib_mm=layer.pen.mark_width_mm)
            if primitive.get("line_style") == "dashed"
            else ([] if transformed_path is not None else [transformed])
        )
        emitted = False
        source_refs = [str(ref) for ref in primitive["source_refs"]]
        attributes = {
            "data-primitive-id": str(primitive["id"]),
            "data-component-id": str(primitive["component_id"]),
            "data-evidence-tier": str(primitive["evidence_tier"]),
            "data-view": "orthographic-side-elevation",
            "data-claim-status": str(primitive["claim_status"]),
            "data-derivation": str(primitive["derivation"]),
            "data-source-refs": "|".join(source_refs),
        }
        if "source_locator" in primitive:
            attributes["data-source-locator"] = str(primitive["source_locator"])
        if "profile_import_sha256" in primitive:
            attributes["data-profile-import-sha256"] = str(
                primitive["profile_import_sha256"]
            )
        if "trace_view_id" in primitive:
            attributes["data-trace-view-id"] = str(primitive["trace_view_id"])
            attributes["data-trace-view-role"] = str(primitive["trace_view_role"])
        if transformed_path is not None and primitive.get("line_style") != "dashed":
            if transformed_path.length(0.001) + 1e-9 >= 3.0 * layer.pen.mark_width_mm:
                emitted = True
                layer.add_path(
                    transformed_path,
                    source_ref="|".join(source_refs),
                    role=str(primitive["role"]),
                    attributes=attributes,
                )
        for stroke in strokes:
            if polyline_length_mm(stroke) + 1e-9 < 3.0 * layer.pen.mark_width_mm:
                continue
            emitted = True
            layer.add(
                stroke,
                source_ref="|".join(source_refs),
                role=str(primitive["role"]),
                attributes=attributes,
            )
        if not emitted:
            omitted_short.append(str(primitive["id"]))

    if omitted_short and record["fidelity"]["status"] == "source-profile":
        _fail(
            "release-eligible source profiles may not lose qualified B0 geometry "
            "to the physical three-nib gate; requalify or decompose: "
            + ", ".join(omitted_short[:8])
            + (" ..." if len(omitted_short) > 8 else "")
        )

    for dimension in record["model"]["dimensions"]:
        start = transform((float(dimension["start"][0]), float(dimension["start"][1])))
        end = transform((float(dimension["end"][0]), float(dimension["end"][1])))
        source_ref = str(dimension["source_ref"])
        evidence_tier = str(dimension["evidence_tier"])
        claim_status = (
            "published-dimension"
            if evidence_tier == "B1-published-dimension"
            else "measured-dimension"
        )
        attributes = {
            "data-dimension-id": str(dimension["id"]),
            "data-component-id": str(dimension["id"]),
            "data-evidence-tier": evidence_tier,
            "data-view": "orthographic-side-elevation",
            "data-claim-status": claim_status,
            "data-dimension-value-m": f"{float(dimension['value_m']):g}",
            "data-dimension-qualifier": str(dimension["qualifier"]),
            "data-source-refs": source_ref,
            "data-derivation": "source-dimension-projection-v1",
        }
        dimension_strokes = _dimension_strokes(
            start,
            end,
            str(dimension["orientation"]),
            gap_mm=gap / 2.0,
        )
        if not all(
            _inside(context.field, point)
            for stroke in dimension_strokes
            for point in stroke
        ):
            _fail(f"dimension {dimension['id']!r} leaves the binding map_field.")
        minimum_dimension_stroke = 3.0 * dimensions.pen.mark_width_mm
        if any(
            polyline_length_mm(stroke) + 1e-9 < minimum_dimension_stroke
            for stroke in dimension_strokes
        ):
            _fail(
                f"dimension {dimension['id']!r} cannot clear the physical "
                "three-nib stroke floor."
            )
        for stroke in dimension_strokes:
            dimensions.add(
                stroke,
                source_ref=source_ref,
                role="dimension",
                attributes=attributes,
            )
    dimension_ids = "|".join(
        str(dimension["id"]) for dimension in record["model"]["dimensions"]
    )
    dimension_sources = "|".join(
        dict.fromkeys(
            str(dimension["source_ref"]) for dimension in record["model"]["dimensions"]
        )
    )
    for line in furniture_plan.text_lines:
        if line.layer_id != "bridge_copy":
            continue
        if line.role == "field-panel-label":
            _add_verified_typography_line(
                copy_layer,
                line,
                attributes={
                    "data-evidence-tier": "mixed",
                    "data-view": "orthographic-side-elevation",
                    "data-claim-status": str(record["evidence"]["statement"]),
                    "data-derivation": "bridge-field-copy-v1",
                },
            )
        elif line.role == "dimension-label":
            _add_verified_typography_line(
                copy_layer,
                line,
                source_ref=dimension_sources,
                attributes={
                    "data-dimension-ids": dimension_ids,
                    "data-evidence-tier": str(
                        record["model"]["dimensions"][0]["evidence_tier"]
                    ),
                    "data-view": "orthographic-side-elevation",
                    "data-claim-status": str(record["evidence"]["dimension_policy"]),
                    "data-source-refs": dimension_sources,
                    "data-derivation": "bridge-dimension-label-v1",
                },
            )

    emitted_layers = [*layers.values(), dimensions, copy_layer]
    emitted_layers = [layer for layer in emitted_layers if layer.records]
    if not any(
        layer.id.startswith("bridge_")
        and layer.id not in {"bridge_dimensions", "bridge_copy"}
        for layer in emitted_layers
    ):
        _fail("bridge model emitted no physical structural geometry.")
    rendering_metadata: dict[str, Any] = {
        "view": "orthographic-side-elevation",
        "scale_policy": "fit-to-field-equal-scale",
        "equal_axis_scale": True,
        "model_to_paper_mm_per_m": round(scale, 9),
        "nominal_scale_denominator": round(1_000.0 / scale, 3),
        "printed_numeric_scale": False,
        "claim_scope": str(record["claim_scope"]),
        "omitted_short_primitive_ids": omitted_short,
        "geometry_sha256": str(record["model"]["geometry_sha256"]),
        "framing": copy.deepcopy(record["model"].get("framing")),
        "fidelity_status": str(record["fidelity"]["status"]),
        "release_eligible": bool(record["fidelity"]["release_eligible"]),
        "fidelity_statement": str(record["fidelity"]["statement"]),
        "typography_authority": "furniture.bridge_furniture_plan-v1",
        "typography_geometry_sha256": {
            f"{line.block_id}:{line.line_index}": line.geometry_sha256
            for line in furniture_plan.text_lines
        },
    }
    if (
        recognition_dimension_id is not None
        and recognition_dimension_drawing_fraction is not None
    ):
        rendering_metadata.update(
            {
                "recognition_dimension_id": recognition_dimension_id,
                "recognition_dimension_drawing_fraction": round(
                    float(recognition_dimension_drawing_fraction), 9
                ),
                "recognition_dimension_minimum_drawing_fraction": (
                    SOURCE_PROFILE_RECOGNITION_MINIMUM_DRAWING_FRACTION
                ),
            }
        )
    return emitted_layers, rendering_metadata


def build_bridge_plate(record: Any, format_id: str | None = None) -> PlateArtwork:
    """Build one source-qualified A3 landscape bridge elevation study."""

    checked = validate_bridge_record(record)
    selected = format_id or str(checked["format_id"])
    if selected != FORMAT_ID:
        raise MapPlotterError(
            f"Standalone bridge elevations currently require {FORMAT_ID}; got {selected!r}."
        )
    context = _bridge_context(selected)
    layers, rendering_metadata = _layout_record(checked, context)
    evidence = checked["evidence"]
    details = _bridge_details(checked)
    sources = tuple(copy.deepcopy(checked["sources"]))
    publishers = " / ".join(
        dict.fromkeys(str(source["publisher"]) for source in sources)
    )
    licences = " / ".join(dict.fromkeys(str(source["license"]) for source in sources))
    snapshots = sorted(
        str(source["snapshot_date"])
        for source in sources
        if source.get("snapshot_date") is not None
    )
    notes = tuple(str(note) for note in checked["notes"])
    notes += tuple(str(item) for item in evidence["omissions"])
    notes += (str(checked["claim_scope"]), str(evidence["statement"]))
    fidelity = checked["fidelity"]
    source_profile = fidelity.get("source_profile")
    if isinstance(source_profile, dict):
        rendering_metadata.update(
            {
                "source_profile_importer": str(source_profile["importer"]),
                "source_profile_sha256": str(source_profile["profile_sha256"]),
                "source_profile_trace_sha256": str(source_profile["trace_sha256"]),
                "source_profile_calibration_sha256": str(
                    source_profile["calibration_sha256"]
                ),
                "source_profile_trace_view_id": str(source_profile["trace_view_id"]),
                "source_profile_trace_tolerance_m": float(
                    source_profile["trace_tolerance_m"]
                ),
                "source_profile_paper_error_limit_mm": float(
                    source_profile["paper_error_limit_mm"]
                ),
                "source_profile_required_components": list(
                    source_profile["required_components"]
                ),
            }
        )
    return PlateArtwork(
        subject_id=str(checked["id"]),
        domain="bridges",
        subject_kind=str(checked["subject_kind"]),
        title=str(checked["title"]),
        subtitle=str(checked["subtitle"]),
        details=details,
        credit_line=_compact_credit(checked),
        scale_status="fit-to-field-equal-scale-not-survey",
        evidence_status=str(evidence["statement"]),
        rights_status=str(checked["rights_status"]),
        sources=sources,
        context=context,
        layers=layers,
        pen_order=BRIDGE_PENS,
        artifact_kind=(
            "standalone-bridge-elevation-study"
            if fidelity["status"] == "source-profile"
            else "bridge-dimension-schematic-preview"
        ),
        rendering_preset="bridge-engineering-a3-landscape-v1",
        format_subject_policy=FORMAT_SUBJECT_POLICY,
        source_provider=publishers,
        source_license=licences,
        data_snapshot=snapshots[-1] if snapshots else "source-record-undated",
        notes=notes,
        catalog_record=checked,
        rendering_metadata=rendering_metadata,
    )


__all__ = [
    "BRIDGE_PENS",
    "CATALOG_ID",
    "CATALOG_PATH",
    "EVIDENCE_TIERS",
    "FORMAT_ID",
    "SOURCE_PROFILE_RECOGNITION_MINIMUM_DRAWING_FRACTION",
    "build_bridge_plate",
    "load_bridge_catalog",
    "validate_bridge_record",
]

"""Source-qualified technical illustrations of engineered objects.

This domain is intentionally a compiler for supplied evidence, not a database
of guessed vehicles.  A record names every visible path's source and evidence
status.  Presets may rearrange or simplify that geometry for a real pen, but
they cannot add model-specific features.  Exploded, section and hidden geometry
fail closed unless a supplied technical source explicitly supports them.
"""

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, cast, Iterable, Literal, NoReturn, Sequence

from shapely.geometry import LineString

from .models import MapPlotterError
from .niche_common import (
    ArtworkLayer,
    PlateArtwork,
    PlateContext,
    Rect,
    add_number_marker,
    add_text,
    context_for,
    polyline_length_mm,
    technical_pen_id,
)
from .stroke_font import text_width_mm
from .technical_assets import circle_path
from .technical_geometry import hatch_polygon, simplify_contour, stipple_polygon
from .vector_path import Affine2D, LineSegment, VectorPath


CATALOG_PATH = Path(__file__).with_name("data") / "technical-objects-v1.json"
CATALOG_ID = "technical-objects-v1"
FORMAT_SUBJECT_POLICY = "schematic"

TECHNICAL_PENS = (
    "grey-0-25",
    "blue-0-25",
    "red-0-25",
    "purple-0-4",
    "black-0-25",
    "black-0-4",
    "black-0-6",
    "black-1",
)

CATEGORIES = frozenset(
    {
        "car",
        "racing-car",
        "motorcycle",
        "bicycle",
        "boat",
        "yacht",
        "rowing-shell",
        "ship",
        "personal-watercraft",
        "aircraft",
        "glider",
        "helicopter",
        "drone",
        "spacecraft",
        "train",
        "locomotive",
        "tram",
        "engine",
        "motor",
        "drivetrain",
        "turbine",
        "machinery",
        "scientific-instrument",
        "camera",
        "watch",
        "tool",
        "architectural-object",
        "product-object",
    }
)

PRESETS = {
    "hero-profile": "HERO PROFILE",
    "three-view-technical-plate": "THREE-VIEW TECHNICAL PLATE",
    "orthographic-collection": "ORTHOGRAPHIC ENGINEERING COLLECTION",
    "blueprint-drawing": "BLUEPRINT DRAWING",
    "exploded-assembly": "EXPLODED ASSEMBLY",
    "cutaway-section": "CUTAWAY / SECTION",
    "patent-plate": "PATENT PLATE",
    "workshop-manual": "WORKSHOP MANUAL",
    "motion-and-engineering": "MOTION AND ENGINEERING",
    "specification-portrait": "SPECIFICATION PORTRAIT",
    "owners-machine": "OWNER'S MACHINE",
    "component-anatomy": "COMPONENT ANATOMY",
    "historic-evolution": "HISTORIC EVOLUTION",
    "minimal-contour": "MINIMAL CONTOUR",
    "shaded-ink-study": "SHADED INK STUDY",
}

SEMANTIC_CLASSES = frozenset(
    {
        "principal_silhouette",
        "major_structural_edges",
        "glazing_openings",
        "panel_seam_lines",
        "mechanical_detail",
        "internal_cutaway_structure",
        "texture_material_hatching",
        "shadow_hatching",
        "construction_geometry",
        "dimensions_leaders",
        "labels_specifications",
        "accent_feature",
        "background_context",
    }
)

SOURCE_KINDS_BY_LEVEL = {
    1: frozenset(
        {
            "supplied-svg",
            "supplied-cad",
            "cad-derived-vector",
            "supplied-technical-drawing",
            "project-authored-parametric-vector",
        }
    ),
    2: frozenset({"supplied-orthographic-photo", "supplied-near-orthographic-photo"}),
    3: frozenset({"supplied-general-photo"}),
    4: frozenset({"verified-repository-vector", "verified-repository-reference"}),
    5: frozenset({"insufficient-reference"}),
}
SOURCE_LEVELS = frozenset(SOURCE_KINDS_BY_LEVEL)
RIGHTS_STATUSES = frozenset(
    {"project-authored", "commercial-clear", "owner-supplied", "review-required"}
)
SOURCE_RIGHTS_STATUSES = frozenset(
    {"project-authored", "commercial-clear", "owner-supplied", "review-required"}
)
EVIDENCE = frozenset(
    {
        "supplied-visible",
        "supplied-hidden",
        "repository-verified",
        "inferred-visible",
        "project-authored",
    }
)
VIEW_TYPES = frozenset(
    {
        "side",
        "elevation",
        "front",
        "rear",
        "plan",
        "three-quarter",
        "section",
        "cutaway",
        "exploded",
        "component-detail",
    }
)
VIEW_SCALE_STATUSES = frozenset(
    {
        "verified-common-scale",
        "dimension-calibrated",
        "dimension-anchored-envelope",
        "not-to-scale",
        "visible-view-only",
    }
)
LINE_STYLES = frozenset({"solid", "dashed", "centre"})
FILL_PATTERNS = frozenset(
    {
        "none",
        "section-hatch",
        "material-hatch",
        "shadow-hatch",
        "cross-hatch",
        "stipple",
    }
)
_STABLE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SHEET_RANK = {"A5": 0, "A4": 1, "A3": 2}
FACT_SOURCE_KINDS = frozenset(
    {
        "official-specification-page",
        "official-history-page",
        "museum-record",
        "public-authority-record",
    }
)

Point = tuple[float, float]
Density = Literal["sparse", "medium", "rich"]
Stroke = list[Point]


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid technical object data: {message}")


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


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{label} must be an integer.")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        _fail(f"{label} must be boolean.")
    return value


def _point(value: Any, label: str) -> Point:
    raw = _array(value, label)
    if len(raw) != 2:
        _fail(f"{label} must be [x, y].")
    return (_number(raw[0], f"{label}[0]"), _number(raw[1], f"{label}[1]"))


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


def _validate_identity(value: Any) -> dict[str, Any]:
    identity = _object(value, "identity")
    _keys(
        identity,
        "identity",
        required={"model"},
        optional={
            "manufacturer",
            "variant",
            "year",
            "configuration",
            "owner",
            "identifiers",
            "dates",
            "location",
            "event",
            "dedication",
            "story",
            "custom_modifications",
            "designer",
            "engineer",
            "builder",
            "service",
            "squadron",
            "class",
            "team",
            "crew",
        },
    )
    _text(identity["model"], "identity.model")
    for key in (
        "manufacturer",
        "variant",
        "year",
        "configuration",
        "owner",
        "location",
        "event",
        "dedication",
        "story",
        "designer",
        "engineer",
        "builder",
        "service",
        "squadron",
        "class",
        "team",
        "crew",
    ):
        if key in identity:
            _text(identity[key], f"identity.{key}")
    identifiers = _array(identity.get("identifiers", []), "identity.identifiers")
    for index, raw in enumerate(identifiers):
        label = f"identity.identifiers[{index}]"
        item = _object(raw, label)
        _keys(item, label, required={"kind", "value", "supplied"})
        _identifier(item["kind"], f"{label}.kind")
        _text(item["value"], f"{label}.value")
        if not _boolean(item["supplied"], f"{label}.supplied"):
            _fail(f"{label} must be marked supplied; identifiers are never guessed.")
    dates = _array(identity.get("dates", []), "identity.dates")
    for index, raw in enumerate(dates):
        label = f"identity.dates[{index}]"
        item = _object(raw, label)
        _keys(item, label, required={"kind", "value", "supplied"})
        _identifier(item["kind"], f"{label}.kind")
        _text(item["value"], f"{label}.value")
        if not _boolean(item["supplied"], f"{label}.supplied"):
            _fail(f"{label} must be marked supplied; dates are never inferred.")
    modifications = _array(
        identity.get("custom_modifications", []), "identity.custom_modifications"
    )
    for index, modification in enumerate(modifications):
        _text(modification, f"identity.custom_modifications[{index}]")
    return identity


def _validate_source(value: Any, index: int) -> dict[str, Any]:
    label = f"sources[{index}]"
    source = _object(value, label)
    _keys(
        source,
        label,
        required={
            "id",
            "level",
            "kind",
            "attribution",
            "visible_credit",
            "license",
            "rights_status",
            "asset_sha256",
            "method",
        },
        optional={
            "asset_path",
            "url",
            "publisher",
            "captured_at",
            "view_ids",
            "verified_technical",
            "rights_cleared_marks",
            "provenance",
        },
    )
    _identifier(source["id"], f"{label}.id")
    level = _integer(source["level"], f"{label}.level")
    if level not in SOURCE_LEVELS:
        _fail(f"{label}.level must be an integer from 1 to 5.")
    kind = _text(source["kind"], f"{label}.kind")
    if kind not in SOURCE_KINDS_BY_LEVEL[level]:
        _fail(f"{label}.kind {kind!r} does not belong to source level {level}.")
    for key in ("attribution", "visible_credit", "license", "method"):
        _text(source[key], f"{label}.{key}")
    rights = _text(source["rights_status"], f"{label}.rights_status")
    if rights not in SOURCE_RIGHTS_STATUSES:
        _fail(f"{label}.rights_status {rights!r} is unsupported.")
    digest = _text(source["asset_sha256"], f"{label}.asset_sha256")
    if _SHA256.fullmatch(digest) is None:
        _fail(f"{label}.asset_sha256 must be a lowercase SHA-256.")
    if "asset_path" in source:
        path_text = _text(source["asset_path"], f"{label}.asset_path")
        path = PurePosixPath(path_text)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != path_text:
            _fail(f"{label}.asset_path must be a normalized relative POSIX path.")
        if level == 4 and not path_text.startswith("contracts/"):
            _fail(f"{label}.asset_path for level 4 must stay inside contracts/.")
    elif level == 4:
        _fail(f"{label}.asset_path is required for a repository reference.")
    if "url" in source and not _text(source["url"], f"{label}.url").startswith(
        "https://"
    ):
        _fail(f"{label}.url must use HTTPS.")
    if "publisher" in source:
        _text(source["publisher"], f"{label}.publisher")
    if "captured_at" in source:
        _text(source["captured_at"], f"{label}.captured_at")
    view_ids = _array(source.get("view_ids", []), f"{label}.view_ids")
    for view_index, view_id in enumerate(view_ids):
        _identifier(view_id, f"{label}.view_ids[{view_index}]")
    verified = source.get("verified_technical", False)
    marks = source.get("rights_cleared_marks", False)
    _boolean(verified, f"{label}.verified_technical")
    _boolean(marks, f"{label}.rights_cleared_marks")
    if level in {2, 3, 5} and verified:
        _fail(f"{label} cannot call a photographic/insufficient source technical.")
    if "provenance" in source:
        provenance = _object(source["provenance"], f"{label}.provenance")
        provenance_kind = provenance.get("kind")
        digest_keys: tuple[str, ...]
        if provenance_kind == "native-pdf-vector":
            _keys(
                provenance,
                f"{label}.provenance",
                required={
                    "kind",
                    "source_asset_sha256",
                    "page",
                    "normalized_svg_sha256",
                    "extraction_geometry_sha256",
                },
            )
            if _integer(provenance["page"], f"{label}.provenance.page") < 1:
                _fail(f"{label}.provenance.page must be positive.")
            digest_keys = (
                "source_asset_sha256",
                "normalized_svg_sha256",
                "extraction_geometry_sha256",
            )
        elif provenance_kind == "binary-centreline-chain":
            _keys(
                provenance,
                f"{label}.provenance",
                required={
                    "kind",
                    "source_asset_sha256",
                    "parent_asset_sha256",
                    "parent_page",
                    "parent_image_index",
                    "upstream_image_sha256",
                    "source_conversion",
                    "source_conversion_tool",
                    "source_conversion_tool_version",
                    "parent_image_extractor",
                    "parent_image_extractor_version",
                    "derived_one_bit_sha256",
                    "extraction_sha256",
                    "geometry_sha256",
                    "provenance_sha256",
                },
            )
            if provenance["parent_page"] is not None and (
                _integer(provenance["parent_page"], f"{label}.provenance.parent_page")
                < 1
            ):
                _fail(f"{label}.provenance.parent_page must be positive.")
            if provenance["parent_image_index"] is not None and (
                _integer(
                    provenance["parent_image_index"],
                    f"{label}.provenance.parent_image_index",
                )
                < 0
            ):
                _fail(f"{label}.provenance.parent_image_index cannot be negative.")
            digest_keys = (
                "source_asset_sha256",
                "derived_one_bit_sha256",
                "extraction_sha256",
                "geometry_sha256",
                "provenance_sha256",
            )
            upstream_digest = provenance["upstream_image_sha256"]
            if upstream_digest is not None and (
                not isinstance(upstream_digest, str)
                or _SHA256.fullmatch(upstream_digest) is None
            ):
                _fail(
                    f"{label}.provenance.upstream_image_sha256 must be null or a lowercase SHA-256."
                )
            parent_digest = provenance["parent_asset_sha256"]
            if parent_digest is not None and (
                not isinstance(parent_digest, str)
                or _SHA256.fullmatch(parent_digest) is None
            ):
                _fail(
                    f"{label}.provenance.parent_asset_sha256 must be null or a lowercase SHA-256."
                )
            parent_values = (
                parent_digest,
                provenance["parent_page"],
                provenance["parent_image_index"],
                provenance["parent_image_extractor"],
                provenance["parent_image_extractor_version"],
            )
            if any(value is not None for value in parent_values) and not all(
                value is not None for value in parent_values
            ):
                _fail(
                    f"{label}.provenance parent asset, page, image index, extractor and version must occur together."
                )
            conversion_values = (
                upstream_digest,
                provenance["source_conversion"],
                provenance["source_conversion_tool"],
                provenance["source_conversion_tool_version"],
            )
            if any(value is not None for value in conversion_values) and not all(
                value is not None for value in conversion_values
            ):
                _fail(
                    f"{label}.provenance upstream image, conversion, tool and version must occur together."
                )
            if parent_digest is not None:
                if upstream_digest is None:
                    _fail(
                        f"{label}.provenance parent PDF requires an upstream extracted image."
                    )
                if provenance["source_asset_sha256"] != parent_digest:
                    _fail(
                        f"{label}.provenance source asset must be the parent PDF when one is present."
                    )
            elif upstream_digest is not None:
                if provenance["source_asset_sha256"] != upstream_digest:
                    _fail(
                        f"{label}.provenance source asset must be the upstream image when no parent exists."
                    )
            elif (
                provenance["source_asset_sha256"]
                != provenance["derived_one_bit_sha256"]
            ):
                _fail(
                    f"{label}.provenance direct binary source asset must match derived_one_bit_sha256."
                )
        else:
            _fail(f"{label}.provenance.kind is unsupported.")
        for digest_key in digest_keys:
            digest_value = provenance[digest_key]
            if (
                not isinstance(digest_value, str)
                or _SHA256.fullmatch(digest_value) is None
            ):
                _fail(f"{label}.provenance.{digest_key} must be a lowercase SHA-256.")
        if provenance["source_asset_sha256"] != source["asset_sha256"]:
            _fail(f"{label}.provenance.source_asset_sha256 must match asset_sha256.")
    return source


def _validate_fact_source(value: Any, index: int) -> dict[str, Any]:
    """Validate a factual citation without pretending that it supplies geometry."""

    label = f"fact_sources[{index}]"
    source = _object(value, label)
    _keys(
        source,
        label,
        required={
            "id",
            "kind",
            "title",
            "publisher",
            "url",
            "attribution",
            "visible_credit",
            "license",
            "rights_status",
            "captured_at",
            "method",
        },
    )
    _identifier(source["id"], f"{label}.id")
    kind = _text(source["kind"], f"{label}.kind")
    if kind not in FACT_SOURCE_KINDS:
        _fail(f"{label}.kind {kind!r} is unsupported.")
    for key in (
        "title",
        "publisher",
        "attribution",
        "visible_credit",
        "license",
        "captured_at",
        "method",
    ):
        _text(source[key], f"{label}.{key}")
    url = _text(source["url"], f"{label}.url")
    if not url.startswith("https://"):
        _fail(f"{label}.url must use HTTPS.")
    rights = _text(source["rights_status"], f"{label}.rights_status")
    if rights not in SOURCE_RIGHTS_STATUSES:
        _fail(f"{label}.rights_status {rights!r} is unsupported.")
    return source


def _validate_history(value: Any, index: int) -> dict[str, Any]:
    label = f"history[{index}]"
    item = _object(value, label)
    _keys(
        item,
        label,
        required={"id", "date", "text", "source_ref", "verified", "selected"},
    )
    _identifier(item["id"], f"{label}.id")
    _text(item["date"], f"{label}.date")
    _text(item["text"], f"{label}.text")
    _identifier(item["source_ref"], f"{label}.source_ref")
    if not _boolean(item["verified"], f"{label}.verified"):
        _fail(f"{label} is unverified; history printed on paper must be sourced.")
    _boolean(item["selected"], f"{label}.selected")
    return item


def _validate_geometry(primitive: dict[str, Any], label: str) -> None:
    forms = [
        name for name in ("path", "points", "circle", "ellipse") if name in primitive
    ]
    if len(forms) != 1:
        _fail(f"{label} must provide exactly one of path, points, circle or ellipse.")
    form = forms[0]
    if form == "path":
        try:
            VectorPath.from_dict(primitive["path"])
        except (KeyError, TypeError, ValueError) as exc:
            _fail(f"{label}.path is not a canonical line/cubic vector path: {exc}")
    elif form == "points":
        points = _array(primitive["points"], f"{label}.points", nonempty=True)
        if len(points) < 2:
            _fail(f"{label}.points needs at least two points.")
        checked = [
            _point(point, f"{label}.points[{index}]")
            for index, point in enumerate(points)
        ]
        if any(first == second for first, second in zip(checked, checked[1:])):
            _fail(f"{label}.points contains a zero-length segment.")
    elif form == "circle":
        circle = _object(primitive["circle"], f"{label}.circle")
        _keys(circle, f"{label}.circle", required={"centre", "radius"})
        _point(circle["centre"], f"{label}.circle.centre")
        if _number(circle["radius"], f"{label}.circle.radius") <= 0:
            _fail(f"{label}.circle.radius must be positive.")
    else:
        ellipse = _object(primitive["ellipse"], f"{label}.ellipse")
        _keys(
            ellipse,
            f"{label}.ellipse",
            required={"centre", "radius_x", "radius_y"},
            optional={"rotation_deg"},
        )
        _point(ellipse["centre"], f"{label}.ellipse.centre")
        if (
            min(
                _number(ellipse["radius_x"], f"{label}.ellipse.radius_x"),
                _number(ellipse["radius_y"], f"{label}.ellipse.radius_y"),
            )
            <= 0
        ):
            _fail(f"{label}.ellipse radii must be positive.")
        if "rotation_deg" in ellipse:
            _number(ellipse["rotation_deg"], f"{label}.ellipse.rotation_deg")


def _validate_primitive(value: Any, view_index: int, index: int) -> dict[str, Any]:
    label = f"views[{view_index}].primitives[{index}]"
    primitive = _object(value, label)
    _keys(
        primitive,
        label,
        required={
            "id",
            "component_id",
            "semantic_class",
            "source_refs",
            "evidence",
            "claim_status",
        },
        optional={
            "path",
            "points",
            "circle",
            "ellipse",
            "feature_kind",
            "confidence",
            "line_style",
            "source_path_ids",
            "fill_pattern",
            "detail_priority",
            "protected_mark",
            "rights_cleared",
            "minimum_sheet",
        },
    )
    _identifier(primitive["id"], f"{label}.id")
    _identifier(primitive["component_id"], f"{label}.component_id")
    semantic = _text(primitive["semantic_class"], f"{label}.semantic_class")
    if semantic not in SEMANTIC_CLASSES:
        _fail(f"{label}.semantic_class {semantic!r} is unsupported.")
    refs = _array(primitive["source_refs"], f"{label}.source_refs", nonempty=True)
    for ref_index, ref in enumerate(refs):
        _identifier(ref, f"{label}.source_refs[{ref_index}]")
    evidence = _text(primitive["evidence"], f"{label}.evidence")
    if evidence not in EVIDENCE:
        _fail(f"{label}.evidence {evidence!r} is unsupported.")
    _text(primitive["claim_status"], f"{label}.claim_status")
    _validate_geometry(primitive, label)
    if "feature_kind" in primitive:
        _identifier(primitive["feature_kind"], f"{label}.feature_kind")
    if "confidence" in primitive:
        confidence = _number(primitive["confidence"], f"{label}.confidence")
        if not 0 <= confidence <= 1:
            _fail(f"{label}.confidence must be between zero and one.")
    if "source_path_ids" in primitive:
        source_path_ids = _array(
            primitive["source_path_ids"], f"{label}.source_path_ids", nonempty=True
        )
        for source_path_index, source_path_id in enumerate(source_path_ids):
            _identifier(
                source_path_id,
                f"{label}.source_path_ids[{source_path_index}]",
            )
    line_style = primitive.get("line_style", "solid")
    if line_style not in LINE_STYLES:
        _fail(f"{label}.line_style {line_style!r} is unsupported.")
    fill_pattern = primitive.get("fill_pattern", "none")
    if fill_pattern not in FILL_PATTERNS:
        _fail(f"{label}.fill_pattern {fill_pattern!r} is unsupported.")
    priority = primitive.get("detail_priority", "normal")
    if priority not in {"identity", "normal", "optional"}:
        _fail(f"{label}.detail_priority must be identity, normal or optional.")
    protected = primitive.get("protected_mark", False)
    cleared = primitive.get("rights_cleared", False)
    _boolean(protected, f"{label}.protected_mark")
    _boolean(cleared, f"{label}.rights_cleared")
    if protected and (not cleared or semantic != "accent_feature"):
        _fail(
            f"{label} protected marks require explicit clearance and the accent_feature class."
        )
    if "minimum_sheet" in primitive:
        minimum_sheet = _text(primitive["minimum_sheet"], f"{label}.minimum_sheet")
        if minimum_sheet not in _SHEET_RANK:
            _fail(f"{label}.minimum_sheet must be A5, A4 or A3.")
    return primitive


def _validate_dimension(value: Any, view_index: int, index: int) -> dict[str, Any]:
    label = f"views[{view_index}].dimensions[{index}]"
    dimension = _object(value, label)
    _keys(
        dimension,
        label,
        required={
            "id",
            "start",
            "end",
            "label",
            "value",
            "unit",
            "source_ref",
            "verified",
            "qualifier",
        },
    )
    _identifier(dimension["id"], f"{label}.id")
    start = _point(dimension["start"], f"{label}.start")
    end = _point(dimension["end"], f"{label}.end")
    if start == end:
        _fail(f"{label} cannot have equal endpoints.")
    _text(dimension["label"], f"{label}.label")
    _text(dimension["value"], f"{label}.value")
    _text(dimension["unit"], f"{label}.unit")
    _identifier(dimension["source_ref"], f"{label}.source_ref")
    if not _boolean(dimension["verified"], f"{label}.verified"):
        _fail(f"{label} is unverified; omit it instead of drawing a pseudo-dimension.")
    _text(dimension["qualifier"], f"{label}.qualifier")
    return dimension


def _validate_callout(value: Any, view_index: int, index: int) -> dict[str, Any]:
    label = f"views[{view_index}].callouts[{index}]"
    callout = _object(value, label)
    _keys(
        callout,
        label,
        required={
            "id",
            "component_id",
            "anchor",
            "label_position",
            "label",
            "source_ref",
        },
    )
    _identifier(callout["id"], f"{label}.id")
    _identifier(callout["component_id"], f"{label}.component_id")
    _point(callout["anchor"], f"{label}.anchor")
    _point(callout["label_position"], f"{label}.label_position")
    _text(callout["label"], f"{label}.label")
    _identifier(callout["source_ref"], f"{label}.source_ref")
    return callout


def _validate_raster_sampling(value: Any, label: str) -> dict[str, Any]:
    sampling = _object(value, label)
    _keys(
        sampling,
        label,
        required={
            "source_unit",
            "source_crop_size_px",
            "useful_subject_bbox",
            "maximum_projected_half_pixel_mm",
            "method",
        },
    )
    if sampling["source_unit"] != "source-pixel":
        _fail(f"{label}.source_unit must be source-pixel.")
    _identifier(sampling["method"], f"{label}.method")
    crop_size = _array(sampling["source_crop_size_px"], f"{label}.source_crop_size_px")
    if len(crop_size) != 2:
        _fail(f"{label}.source_crop_size_px must be [width, height].")
    crop_width, crop_height = (
        _integer(item, f"{label}.source_crop_size_px[{index}]")
        for index, item in enumerate(crop_size)
    )
    if crop_width <= 0 or crop_height <= 0:
        _fail(f"{label}.source_crop_size_px must be positive.")
    bbox = _array(sampling["useful_subject_bbox"], f"{label}.useful_subject_bbox")
    if len(bbox) != 4:
        _fail(f"{label}.useful_subject_bbox must be x,y,width,height.")
    x, y, width, height = (
        _integer(item, f"{label}.useful_subject_bbox[{index}]")
        for index, item in enumerate(bbox)
    )
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        _fail(
            f"{label}.useful_subject_bbox needs a non-negative origin and positive size."
        )
    if x + width > crop_width or y + height > crop_height:
        _fail(f"{label}.useful_subject_bbox must stay inside source_crop_size_px.")
    maximum = _number(
        sampling["maximum_projected_half_pixel_mm"],
        f"{label}.maximum_projected_half_pixel_mm",
    )
    if not 0 < maximum <= 0.125:
        _fail(
            f"{label}.maximum_projected_half_pixel_mm must be greater than zero and no more than 0.125."
        )
    return sampling


def _validate_view(value: Any, index: int) -> dict[str, Any]:
    label = f"views[{index}]"
    view = _object(value, label)
    _keys(
        view,
        label,
        required={
            "id",
            "type",
            "label",
            "unit",
            "axis_direction",
            "scale_status",
            "source_refs",
            "primitives",
            "dimensions",
            "callouts",
        },
        optional={"chronology", "raster_sampling"},
    )
    _identifier(view["id"], f"{label}.id")
    view_type = _text(view["type"], f"{label}.type")
    if view_type not in VIEW_TYPES:
        _fail(f"{label}.type {view_type!r} is unsupported.")
    _text(view["label"], f"{label}.label")
    _identifier(view["unit"], f"{label}.unit")
    if view["axis_direction"] not in {"y-up", "y-down"}:
        _fail(f"{label}.axis_direction must be 'y-up' or 'y-down'.")
    scale_status = _text(view["scale_status"], f"{label}.scale_status")
    if scale_status not in VIEW_SCALE_STATUSES:
        _fail(f"{label}.scale_status {scale_status!r} is unsupported.")
    refs = _array(view["source_refs"], f"{label}.source_refs", nonempty=True)
    for ref_index, ref in enumerate(refs):
        _identifier(ref, f"{label}.source_refs[{ref_index}]")
    primitives = _array(view["primitives"], f"{label}.primitives", nonempty=True)
    checked_primitives = [
        _validate_primitive(primitive, index, primitive_index)
        for primitive_index, primitive in enumerate(primitives)
    ]
    ids = [str(primitive["id"]) for primitive in checked_primitives]
    if len(ids) != len(set(ids)):
        _fail(f"{label}.primitives repeats an id.")
    dimensions = _array(view["dimensions"], f"{label}.dimensions")
    checked_dimensions = [
        _validate_dimension(dimension, index, dimension_index)
        for dimension_index, dimension in enumerate(dimensions)
    ]
    dimension_ids = [str(dimension["id"]) for dimension in checked_dimensions]
    if len(dimension_ids) != len(set(dimension_ids)):
        _fail(f"{label}.dimensions repeats an id.")
    callouts = _array(view["callouts"], f"{label}.callouts")
    checked_callouts = [
        _validate_callout(callout, index, callout_index)
        for callout_index, callout in enumerate(callouts)
    ]
    callout_ids = [str(callout["id"]) for callout in checked_callouts]
    if len(callout_ids) != len(set(callout_ids)):
        _fail(f"{label}.callouts repeats an id.")
    if "chronology" in view:
        chronology = _object(view["chronology"], f"{label}.chronology")
        _keys(chronology, f"{label}.chronology", required={"year", "label"})
        _text(chronology["year"], f"{label}.chronology.year")
        _text(chronology["label"], f"{label}.chronology.label")
    if "raster_sampling" in view:
        if view["unit"] != "source-pixel":
            _fail(f"{label}.unit must be source-pixel when raster_sampling is present.")
        sampling = _validate_raster_sampling(
            view["raster_sampling"], f"{label}.raster_sampling"
        )
        x, y, width, height = sampling["useful_subject_bbox"]
        for primitive_index, primitive in enumerate(checked_primitives):
            bounds = _primitive_path(primitive).bounds()
            if (
                bounds.min_x < x - 1e-9
                or bounds.min_y < y - 1e-9
                or bounds.max_x > x + width - 1 + 1e-9
                or bounds.max_y > y + height - 1 + 1e-9
            ):
                _fail(
                    f"{label}.primitives[{primitive_index}] leaves the declared useful source-pixel bounds."
                )
    return view


def _validate_specification(value: Any, index: int) -> dict[str, Any]:
    label = f"specifications[{index}]"
    specification = _object(value, label)
    _keys(
        specification,
        label,
        required={"id", "label", "value", "unit", "source_ref", "verified", "selected"},
    )
    _identifier(specification["id"], f"{label}.id")
    _text(specification["label"], f"{label}.label")
    _text(specification["value"], f"{label}.value")
    _text(specification["unit"], f"{label}.unit")
    _identifier(specification["source_ref"], f"{label}.source_ref")
    if not _boolean(specification["verified"], f"{label}.verified"):
        _fail(f"{label} is unverified; specifications on paper must be exact.")
    _boolean(specification["selected"], f"{label}.selected")
    return specification


def _validate_style(value: Any) -> dict[str, Any]:
    style = _object(value, "style")
    _keys(
        style,
        "style",
        required={"density"},
        optional={
            "selected_view_ids",
            "show_dimensions",
            "show_callouts",
            "scale_policy",
            "accent_component_ids",
            "deduplication_policy",
            "sub_pen_policy",
        },
    )
    if style["density"] not in {"sparse", "medium", "rich"}:
        _fail("style.density must be sparse, medium or rich.")
    for key in ("selected_view_ids", "accent_component_ids"):
        values = _array(style.get(key, []), f"style.{key}")
        for index, value_item in enumerate(values):
            _identifier(value_item, f"style.{key}[{index}]")
    for key in ("show_dimensions", "show_callouts"):
        if key in style:
            _boolean(style[key], f"style.{key}")
    if "scale_policy" in style and style["scale_policy"] not in {
        "shared-when-verified",
        "fit-each-view",
    }:
        _fail("style.scale_policy is unsupported.")
    if style.get("deduplication_policy", "approximate") not in {
        "approximate",
        "exact",
        "none",
    }:
        _fail("style.deduplication_policy must be approximate, exact or none.")
    if style.get("sub_pen_policy", "report") not in {"report", "fail"}:
        _fail("style.sub_pen_policy must be report or fail.")
    return style


def _geometry_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "views": record["views"],
        "assembly": record.get("assembly"),
        "evolution": record.get("evolution"),
    }


def technical_geometry_sha256(record: dict[str, Any]) -> str:
    payload = json.dumps(
        _geometry_payload(record),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_technical_record(record: Any) -> dict[str, Any]:
    """Validate the strict record and every source/evidence relationship."""

    checked = copy.deepcopy(_object(record, "record"))
    _keys(
        checked,
        "record",
        required={
            "schema_version",
            "kind",
            "id",
            "category",
            "format_id",
            "preset",
            "title",
            "subtitle",
            "identity",
            "source_level",
            "claim_scope",
            "rights_status",
            "sources",
            "views",
            "specifications",
            "style",
            "notes",
            "geometry_sha256",
        },
        optional={
            "highlighted_components",
            "assembly",
            "evolution",
            "excluded_features",
            "generic_category_explicit",
            "context",
            "fact_sources",
            "history",
            "collection",
        },
    )
    if checked["schema_version"] != 1 or checked["kind"] != "technical-object":
        _fail("record must use technical-object schema version 1.")
    _identifier(checked["id"], "record.id")
    category = _text(checked["category"], "record.category")
    if category not in CATEGORIES:
        _fail(f"record.category {category!r} is unsupported.")
    format_id = _text(checked["format_id"], "record.format_id")
    if format_id not in {
        "a5-portrait",
        "a5-landscape",
        "a4-portrait",
        "a4-landscape",
        "a3-portrait",
        "a3-landscape",
    }:
        _fail("technical objects use the six binding A5, A4 or A3 formats only.")
    preset = _text(checked["preset"], "record.preset")
    if preset not in PRESETS:
        _fail(f"record.preset {preset!r} is unsupported.")
    _text(checked["title"], "record.title")
    _text(checked["subtitle"], "record.subtitle")
    identity = _validate_identity(checked["identity"])
    source_level = _integer(checked["source_level"], "record.source_level")
    if source_level not in SOURCE_LEVELS:
        _fail("record.source_level must be an integer from 1 to 5.")
    _text(checked["claim_scope"], "record.claim_scope")
    rights = _text(checked["rights_status"], "record.rights_status")
    if rights not in RIGHTS_STATUSES:
        _fail(f"record.rights_status {rights!r} is unsupported.")
    sources = [
        _validate_source(source, index)
        for index, source in enumerate(
            _array(checked["sources"], "record.sources", nonempty=True)
        )
    ]
    source_ids = [str(source["id"]) for source in sources]
    if len(source_ids) != len(set(source_ids)):
        _fail("record.sources repeats a source id.")
    if source_level != min(int(source["level"]) for source in sources):
        _fail(
            "record.source_level must equal the best (lowest-numbered) supplied source level."
        )
    fact_sources = [
        _validate_fact_source(source, index)
        for index, source in enumerate(
            _array(checked.get("fact_sources", []), "record.fact_sources")
        )
    ]
    fact_source_ids = [str(source["id"]) for source in fact_sources]
    if len(fact_source_ids) != len(set(fact_source_ids)):
        _fail("record.fact_sources repeats a source id.")
    if set(fact_source_ids) & set(source_ids):
        _fail("geometry and factual sources must not share an id.")
    views = [
        _validate_view(view, index)
        for index, view in enumerate(
            _array(checked["views"], "record.views", nonempty=True)
        )
    ]
    view_ids = [str(view["id"]) for view in views]
    if len(view_ids) != len(set(view_ids)):
        _fail("record.views repeats a view id.")
    source_by_id = {str(source["id"]): source for source in sources}
    fact_source_by_id = {str(source["id"]): source for source in fact_sources}
    all_source_ids = set(source_by_id) | set(fact_source_by_id)
    all_components: set[str] = set()
    protected_mark_count = 0
    for view in views:
        for ref in view["source_refs"]:
            if ref not in source_by_id:
                _fail(f"view {view['id']!r} references unknown source {ref!r}.")
        for primitive in view["primitives"]:
            all_components.add(str(primitive["component_id"]))
            for ref in primitive["source_refs"]:
                if ref not in source_by_id:
                    _fail(
                        f"primitive {primitive['id']!r} references unknown source {ref!r}."
                    )
            primitive_sources = [
                source_by_id[str(ref)] for ref in primitive["source_refs"]
            ]
            evidence = str(primitive["evidence"])
            hidden = (
                evidence == "supplied-hidden"
                or primitive["semantic_class"] == "internal_cutaway_structure"
            )
            if hidden and not all(
                int(source["level"]) in {1, 4}
                and bool(source.get("verified_technical"))
                for source in primitive_sources
            ):
                _fail(
                    f"primitive {primitive['id']!r} claims hidden/internal geometry without a verified technical source."
                )
            if source_level == 3 and evidence not in {
                "supplied-visible",
                "inferred-visible",
            }:
                _fail("Level 3 photographs may contribute visible geometry only.")
            if primitive.get("protected_mark"):
                protected_mark_count += 1
                if not all(
                    source.get("rights_cleared_marks") for source in primitive_sources
                ):
                    _fail(
                        f"primitive {primitive['id']!r} uses a mark whose sources are not rights-cleared."
                    )
        for dimension in view["dimensions"]:
            if dimension["source_ref"] not in all_source_ids:
                _fail(f"dimension {dimension['id']!r} references an unknown source.")
        for callout in view["callouts"]:
            if callout["source_ref"] not in source_by_id:
                _fail(f"callout {callout['id']!r} references an unknown source.")
            if callout["component_id"] not in all_components and not any(
                callout["component_id"] == primitive["component_id"]
                for candidate_view in views
                for primitive in candidate_view["primitives"]
            ):
                _fail(f"callout {callout['id']!r} names an unknown component.")
    specifications = [
        _validate_specification(specification, index)
        for index, specification in enumerate(
            _array(checked["specifications"], "record.specifications")
        )
    ]
    specification_ids = [str(item["id"]) for item in specifications]
    if len(specification_ids) != len(set(specification_ids)):
        _fail("record.specifications repeats an id.")
    for specification in specifications:
        if specification["source_ref"] not in all_source_ids:
            _fail(
                f"specification {specification['id']!r} references an unknown source."
            )
    history = [
        _validate_history(item, index)
        for index, item in enumerate(
            _array(checked.get("history", []), "record.history")
        )
    ]
    history_ids = [str(item["id"]) for item in history]
    if len(history_ids) != len(set(history_ids)):
        _fail("record.history repeats an id.")
    for item in history:
        if item["source_ref"] not in all_source_ids:
            _fail(f"history {item['id']!r} references an unknown source.")
    if "collection" in checked:
        collection = _object(checked["collection"], "record.collection")
        _keys(
            collection,
            "record.collection",
            required={"id", "template", "template_version", "recipe_sha256"},
            optional={"geometry_release_ready", "geometry_review_status"},
        )
        _identifier(collection["id"], "record.collection.id")
        _identifier(collection["template"], "record.collection.template")
        _text(collection["template_version"], "record.collection.template_version")
        recipe_digest = _text(
            collection["recipe_sha256"], "record.collection.recipe_sha256"
        )
        if _SHA256.fullmatch(recipe_digest) is None:
            _fail("record.collection.recipe_sha256 must be a lowercase SHA-256.")
        if "geometry_release_ready" in collection:
            _boolean(
                collection["geometry_release_ready"],
                "record.collection.geometry_release_ready",
            )
        if "geometry_review_status" in collection:
            _identifier(
                collection["geometry_review_status"],
                "record.collection.geometry_review_status",
            )
    style = _validate_style(checked["style"])
    selected_view_ids = list(style.get("selected_view_ids", []))
    unknown_selected = sorted(set(selected_view_ids) - set(view_ids))
    if unknown_selected:
        _fail(
            "style.selected_view_ids contains unknown views: "
            + ", ".join(unknown_selected)
            + "."
        )
    notes = _array(checked["notes"], "record.notes")
    for index, note in enumerate(notes):
        _text(note, f"record.notes[{index}]")
    highlighted = _array(
        checked.get("highlighted_components", []), "record.highlighted_components"
    )
    for index, component in enumerate(highlighted):
        component_id = _identifier(component, f"record.highlighted_components[{index}]")
        if component_id not in all_components:
            _fail(
                f"record.highlighted_components names unknown component {component_id!r}."
            )
    excluded = _array(checked.get("excluded_features", []), "record.excluded_features")
    for index, raw in enumerate(excluded):
        label = f"record.excluded_features[{index}]"
        item = _object(raw, label)
        _keys(item, label, required={"kind", "description", "reason"})
        if item["kind"] not in {
            "logo",
            "badge",
            "livery",
            "sponsor-graphic",
            "background-texture",
            "unsupported-detail",
        }:
            _fail(f"{label}.kind is unsupported.")
        _text(item["description"], f"{label}.description")
        _text(item["reason"], f"{label}.reason")
    generic = checked.get("generic_category_explicit", False)
    _boolean(generic, "record.generic_category_explicit")
    if source_level == 5:
        if not generic:
            _fail(
                "INSUFFICIENT_REFERENCE: level 5 needs explicit generic-category authorization or a better source."
            )
        if identity.get("manufacturer") or preset != "minimal-contour":
            _fail("Level 5 output must be an unbranded generic minimal contour.")
    if source_level == 3 and preset not in {
        "hero-profile",
        "owners-machine",
        "specification-portrait",
        "minimal-contour",
        "shaded-ink-study",
    }:
        _fail("Level 3 general photographs support visible-view portraits only.")
    _validate_preset_requirements(checked, views, specifications, all_components)
    geometry_digest = _text(checked["geometry_sha256"], "record.geometry_sha256")
    if _SHA256.fullmatch(geometry_digest) is None:
        _fail("record.geometry_sha256 must be a lowercase SHA-256.")
    actual_digest = technical_geometry_sha256(checked)
    if geometry_digest != actual_digest:
        _fail(
            "record.geometry_sha256 disagrees with the complete views/assembly/evolution geometry."
        )
    if protected_mark_count and rights not in {"commercial-clear", "owner-supplied"}:
        _fail(
            "Protected marks cannot be emitted while the record rights status requires review."
        )
    return checked


def _validate_preset_requirements(
    record: dict[str, Any],
    views: Sequence[dict[str, Any]],
    specifications: Sequence[dict[str, Any]],
    components: set[str],
) -> None:
    preset = str(record["preset"])
    types = {str(view["type"]) for view in views}
    highlighted = list(record.get("highlighted_components", []))
    if preset == "three-view-technical-plate" and (
        len(views) < 3
        or not ({"side", "elevation"} & types)
        or not ({"front", "rear"} & types)
        or "plan" not in types
    ):
        _fail(
            "three-view-technical-plate needs side/elevation, front/rear and plan views."
        )
    if preset == "orthographic-collection":
        category = str(record["category"])
        if category in {"car", "racing-car"}:
            required_types = {"side", "front", "rear", "plan"}
        elif category in {"aircraft", "glider"}:
            required_types = {"side", "front", "plan"}
        elif category in {
            "boat",
            "yacht",
            "rowing-shell",
            "ship",
            "personal-watercraft",
        }:
            required_types = {"side", "plan"}
        else:
            required_types = set()
        missing = sorted(required_types - types)
        if missing:
            _fail(
                "orthographic-collection is missing required "
                + category
                + " views: "
                + ", ".join(missing)
                + "."
            )
        if not 2 <= len(views) <= 4:
            _fail("orthographic-collection supports two to four supplied views.")
    if preset == "exploded-assembly":
        assembly = _object(record.get("assembly"), "record.assembly")
        _keys(
            assembly,
            "record.assembly",
            required={"source_ref", "verified", "component_order", "axis"},
        )
        _identifier(assembly["source_ref"], "record.assembly.source_ref")
        if not _boolean(assembly["verified"], "record.assembly.verified"):
            _fail("exploded-assembly requires verified supplied assembly geometry.")
        if assembly["axis"] not in {"x", "y", "multiple"}:
            _fail("record.assembly.axis is unsupported.")
        order = _array(
            assembly["component_order"],
            "record.assembly.component_order",
            nonempty=True,
        )
        for index, component in enumerate(order):
            component_id = _identifier(
                component, f"record.assembly.component_order[{index}]"
            )
            if component_id not in components:
                _fail(f"record.assembly names unknown component {component_id!r}.")
        if "exploded" not in types:
            _fail(
                "exploded-assembly needs a supplied exploded view; components are not invented or displaced automatically."
            )
    if preset == "cutaway-section":
        internal = any(
            primitive["semantic_class"] == "internal_cutaway_structure"
            and primitive["evidence"]
            in {"supplied-hidden", "repository-verified", "project-authored"}
            for view in views
            for primitive in view["primitives"]
        )
        if not internal or not ({"section", "cutaway"} & types):
            _fail(
                "cutaway-section requires a supplied section/cutaway with verified internal geometry."
            )
    if preset == "workshop-manual" and not 2 <= len(highlighted) <= 5:
        _fail("workshop-manual requires two to five highlighted components.")
    if preset == "component-anatomy" and len(highlighted) != 1:
        _fail("component-anatomy requires exactly one highlighted component.")
    if preset == "specification-portrait" and not any(
        item["selected"] for item in specifications
    ):
        _fail(
            "specification-portrait requires at least one selected verified specification."
        )
    if preset == "owners-machine":
        identity = record["identity"]
        if not (
            identity.get("owner")
            or identity.get("dedication")
            or identity.get("identifiers")
        ):
            _fail(
                "owners-machine requires an owner, supplied identifier or dedication."
            )
    if preset == "historic-evolution":
        evolution = _array(record.get("evolution"), "record.evolution", nonempty=True)
        if not 3 <= len(evolution) <= 8:
            _fail("historic-evolution requires three to eight chronological items.")
        view_ids = {str(view["id"]) for view in views}
        previous_key: tuple[int, str] | None = None
        seen: set[str] = set()
        for index, raw in enumerate(evolution):
            label = f"record.evolution[{index}]"
            item = _object(raw, label)
            _keys(item, label, required={"view_id", "year", "label"})
            view_id = _identifier(item["view_id"], f"{label}.view_id")
            if view_id not in view_ids or view_id in seen:
                _fail(f"{label}.view_id must name one unused supplied view.")
            seen.add(view_id)
            year_text = _text(item["year"], f"{label}.year")
            item_label = _text(item["label"], f"{label}.label")
            numeric = int(year_text) if year_text.isdigit() else 999_999
            key = (numeric, year_text.casefold())
            if previous_key is not None and key < previous_key:
                _fail("record.evolution must already be chronological.")
            previous_key = key
            if not item_label:
                _fail(f"{label}.label is empty.")
    if preset == "motion-and-engineering":
        context = _object(record.get("context"), "record.context")
        _keys(context, "record.context", required={"kind", "layer_ids", "claim_scope"})
        if context["kind"] not in {
            "route",
            "circuit",
            "map",
            "elevation",
            "performance-trace",
            "flight-path",
        }:
            _fail("record.context.kind is unsupported.")
        layer_ids = _array(
            context["layer_ids"], "record.context.layer_ids", nonempty=True
        )
        for index, layer_id in enumerate(layer_ids):
            _identifier(layer_id, f"record.context.layer_ids[{index}]")
        _text(context["claim_scope"], "record.context.claim_scope")


def load_technical_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    selected = path or CATALOG_PATH
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(
            f"Cannot read technical object catalog {selected}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Invalid technical object catalog JSON {selected}: {exc}"
        ) from exc
    wrapper = _object(payload, "catalog")
    _keys(wrapper, "catalog", required={"schema_version", "catalog_id", "subjects"})
    if wrapper["schema_version"] != 1 or wrapper["catalog_id"] != CATALOG_ID:
        _fail(f"catalog must be schema 1 / {CATALOG_ID}.")
    records = [
        validate_technical_record(record)
        for record in _array(wrapper["subjects"], "catalog.subjects", nonempty=True)
    ]
    ids = [str(record["id"]) for record in records]
    if len(ids) != len(set(ids)):
        _fail("catalog repeats a record id.")
    repository_root = Path(__file__).resolve().parents[2]
    catalog_root = selected.resolve().parent
    for record in records:
        for source in record["sources"]:
            if "asset_path" not in source:
                continue
            relative = PurePosixPath(str(source["asset_path"]))
            if int(source["level"]) == 4:
                allowed_root = (repository_root / "contracts").resolve()
                asset = repository_root.joinpath(*relative.parts)
            else:
                allowed_root = catalog_root
                asset = catalog_root.joinpath(*relative.parts)
            try:
                asset.resolve(strict=False).relative_to(allowed_root)
            except ValueError:
                _fail(
                    f"source {source['id']!r} asset leaves its allowed catalog/contracts directory."
                )
            if not asset.is_file():
                _fail(f"source asset does not exist: {source['asset_path']}.")
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            if digest != source["asset_sha256"]:
                _fail(
                    f"source {source['id']!r} SHA-256 changed: expected "
                    f"{source['asset_sha256']}, got {digest}."
                )
    return records


def _technical_context(format_id: str) -> PlateContext:
    context = context_for(format_id)
    raw = context.plate.get("technical_zones_mm")
    if not isinstance(raw, dict):
        _fail("binding plate format does not publish technical_zones_mm.")
    required = {
        "technical_field",
        "technical_top",
        "technical_bottom_left",
        "technical_bottom_right",
        "technical_left",
        "technical_right_top",
        "technical_right_bottom",
    }
    if set(raw) != required:
        _fail("binding technical zone set changed unexpectedly.")
    zones = {
        name: Rect(
            float(value["x"]),
            float(value["y"]),
            float(value["width"]),
            float(value["height"]),
        )
        for name, value in raw.items()
    }
    return replace(context, zones={**context.zones, **zones})


def _primitive_path(primitive: dict[str, Any]) -> VectorPath:
    if "path" in primitive:
        return VectorPath.from_dict(primitive["path"])
    if "points" in primitive:
        points = [(float(point[0]), float(point[1])) for point in primitive["points"]]
        closed = len(points) > 2 and points[0] == points[-1]
        if closed:
            points.pop()
        return VectorPath(
            start=points[0],
            segments=tuple(LineSegment(point) for point in points[1:]),
            closed=closed,
        )
    if "circle" in primitive:
        circle = primitive["circle"]
        return circle_path(
            (float(circle["centre"][0]), float(circle["centre"][1])),
            float(circle["radius"]),
        )
    ellipse = primitive["ellipse"]
    path = circle_path(
        (float(ellipse["centre"][0]), float(ellipse["centre"][1])),
        float(ellipse["radius_x"]),
        float(ellipse["radius_y"]),
    )
    rotation = float(ellipse.get("rotation_deg", 0.0))
    if rotation == 0:
        return path
    centre_x = float(ellipse["centre"][0])
    centre_y = float(ellipse["centre"][1])
    angle = math.radians(rotation)
    cosine, sine = math.cos(angle), math.sin(angle)
    return path.transformed(
        Affine2D(
            a=cosine,
            b=sine,
            c=-sine,
            d=cosine,
            e=centre_x - cosine * centre_x + sine * centre_y,
            f=centre_y - sine * centre_x - cosine * centre_y,
        )
    )


def _view_bounds(
    view: dict[str, Any], *, component_filter: set[str] | None = None
) -> tuple[float, float, float, float]:
    paths = [
        _primitive_path(primitive)
        for primitive in view["primitives"]
        if component_filter is None or primitive["component_id"] in component_filter
    ]
    if not paths:
        _fail(f"view {view['id']!r} has no geometry for the requested component set.")
    bounds = [path.bounds() for path in paths]
    return (
        min(item.min_x for item in bounds),
        min(item.min_y for item in bounds),
        max(item.max_x for item in bounds),
        max(item.max_y for item in bounds),
    )


def _view_fit_bounds(
    view: dict[str, Any], *, component_filter: set[str] | None = None
) -> tuple[float, float, float, float]:
    sampling = view.get("raster_sampling")
    if sampling is None or component_filter is not None:
        return _view_bounds(view, component_filter=component_filter)
    x, y, width, height = (float(value) for value in sampling["useful_subject_bbox"])
    # Pixel coordinates address centres. Fit the full sampled support, including
    # the half-pixel uncertainty on every outer edge, so the recorded width and
    # height are the dimensions that actually drive placement.
    return (x - 0.5, y - 0.5, x + width - 0.5, y + height - 0.5)


def _fit_matrix(
    view: dict[str, Any],
    rect: Rect,
    *,
    fixed_scale: float | None = None,
    component_filter: set[str] | None = None,
) -> tuple[Affine2D, float]:
    min_x, min_y, max_x, max_y = _view_fit_bounds(
        view, component_filter=component_filter
    )
    width = max(max_x - min_x, 1e-12)
    height = max(max_y - min_y, 1e-12)
    natural_scale = min(rect.width / width, rect.height / height)
    scale = natural_scale if fixed_scale is None else fixed_scale
    raster_sampling = view.get("raster_sampling")
    if raster_sampling is not None:
        maximum_scale = 2.0 * float(raster_sampling["maximum_projected_half_pixel_mm"])
        if fixed_scale is None:
            scale = min(scale, maximum_scale)
        elif scale > maximum_scale + 1e-9:
            _fail(
                f"view {view['id']!r} exceeds its raster half-pixel sampling limit at the requested shared scale."
            )
    if (
        scale <= 0
        or width * scale > rect.width + 1e-8
        or height * scale > rect.height + 1e-8
    ):
        _fail(
            f"view {view['id']!r} cannot fit its assigned panel at the requested scale."
        )
    used_width = width * scale
    used_height = height * scale
    offset_x = rect.x + (rect.width - used_width) / 2.0
    offset_y = rect.y + (rect.height - used_height) / 2.0
    if view["axis_direction"] == "y-up":
        return (
            Affine2D(
                a=scale,
                d=-scale,
                e=offset_x - min_x * scale,
                f=offset_y + max_y * scale,
            ),
            scale,
        )
    return (
        Affine2D(
            a=scale,
            d=scale,
            e=offset_x - min_x * scale,
            f=offset_y - min_y * scale,
        ),
        scale,
    )


def _drawing_rect(panel: Rect, context: PlateContext) -> Rect:
    gap = float(context.plate["gap_mm"])
    # The actual cap-height floor is applied by ``add_text``.  The reserved
    # band is derived from the published attribution cap plus half the plate
    # gap, with no private paper constant.
    top_reserve = float(context.plate["type_scale_mm"]["attribution"]) + gap / 2.0
    horizontal = gap / 2.0
    if panel.width <= 2 * horizontal or panel.height <= top_reserve + horizontal:
        _fail("technical view panel is too small after its derived copy reserve.")
    return Rect(
        panel.x + horizontal,
        panel.y + top_reserve,
        panel.width - 2 * horizontal,
        panel.height - top_reserve - horizontal,
    )


def _add_panel_label(
    layer: ArtworkLayer,
    copy_text: str,
    panel: Rect,
    context: PlateContext,
    *,
    role: str = "view-label",
) -> None:
    cap = float(context.plate["type_scale_mm"]["attribution"])
    add_text(
        layer,
        copy_text,
        x_mm=panel.x,
        y_mm=panel.y,
        preferred_cap_mm=cap,
        maximum_width_mm=panel.width,
        minimum_cap_mm=8.0 * layer.pen.mark_width_mm,
        allow_horizontal_condense=True,
        role=role,
    )


def _dashed_strokes(
    points: Sequence[Point], *, nib_mm: float, centre: bool = False
) -> list[Stroke]:
    dash = (10.0 if centre else 7.0) * nib_mm
    gap = (4.0 if centre else 3.0) * nib_mm
    result: list[Stroke] = []
    drawing = True
    remaining = dash
    for start, end in zip(points, points[1:]):
        length = math.dist(start, end)
        if length <= 1e-12:
            continue
        ux, uy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
        cursor = 0.0
        while cursor < length - 1e-9:
            take = min(remaining, length - cursor)
            if drawing and take + 1e-9 >= 3.0 * nib_mm:
                result.append(
                    [
                        (start[0] + ux * cursor, start[1] + uy * cursor),
                        (
                            start[0] + ux * (cursor + take),
                            start[1] + uy * (cursor + take),
                        ),
                    ]
                )
            cursor += take
            remaining -= take
            if remaining <= 1e-9:
                drawing = not drawing
                remaining = dash if drawing else gap
    return result


def _layer_map(context: PlateContext) -> dict[str, ArtworkLayer]:
    return {
        semantic: ArtworkLayer(
            f"object_{semantic}",
            semantic.replace("_", " ").title(),
            technical_pen_id(context.plate, semantic),
        )
        for semantic in sorted(SEMANTIC_CLASSES)
    }


def _record_attributes(
    view: dict[str, Any], primitive: dict[str, Any], preset: str
) -> dict[str, str]:
    return {
        "data-primitive-id": str(primitive["id"]),
        "data-component-id": str(primitive["component_id"]),
        "data-semantic-class": str(primitive["semantic_class"]),
        "data-evidence": str(primitive["evidence"]),
        "data-claim-status": str(primitive["claim_status"]),
        "data-source-refs": "|".join(str(ref) for ref in primitive["source_refs"]),
        "data-view-id": str(view["id"]),
        "data-view-type": str(view["type"]),
        "data-composition-preset": preset,
        **(
            {"data-feature-kind": str(primitive["feature_kind"])}
            if primitive.get("feature_kind")
            else {}
        ),
        **(
            {"data-confidence": f"{float(primitive['confidence']):.6f}"}
            if primitive.get("confidence") is not None
            else {}
        ),
    }


def _emit_primitive(
    layers: dict[str, ArtworkLayer],
    view: dict[str, Any],
    primitive: dict[str, Any],
    matrix: Affine2D,
    *,
    preset: str,
    density: str,
) -> tuple[bool, int]:
    semantic = str(primitive["semantic_class"])
    layer = layers[semantic]
    path = _primitive_path(primitive).transformed(matrix)
    attributes = _record_attributes(view, primitive, preset)
    source_ref = "|".join(str(ref) for ref in primitive["source_refs"])
    minimum = 3.0 * layer.pen.mark_width_mm
    line_style = primitive.get("line_style", "solid")
    emitted = False
    if line_style == "solid":
        if path.length(0.001) + 1e-9 >= minimum:
            layer.add_path(
                path,
                source_ref=source_ref,
                role=semantic,
                attributes=attributes,
            )
            emitted = True
    else:
        flattened = list(path.flatten(0.02).points)
        for stroke in _dashed_strokes(
            flattened,
            nib_mm=layer.pen.mark_width_mm,
            centre=line_style == "centre",
        ):
            layer.add(
                stroke,
                source_ref=source_ref,
                role=semantic,
                attributes={**attributes, "data-line-style": str(line_style)},
            )
            emitted = True
    hatch_count = 0
    pattern = primitive.get("fill_pattern", "none")
    if pattern != "none":
        flattened = list(path.flatten(0.03).points)
        if flattened[0] != flattened[-1]:
            _fail(f"primitive {primitive['id']!r} requests hatching on an open path.")
        if pattern == "stipple":
            hatch_semantic = "texture_material_hatching"
            hatch_strokes = stipple_polygon(
                flattened,
                nib_mm=layers[hatch_semantic].pen.mark_width_mm,
                density=cast(Density, density),
            )
        else:
            hatch_semantic = (
                "shadow_hatching"
                if pattern == "shadow-hatch"
                else "texture_material_hatching"
            )
            hatch_strokes = hatch_polygon(
                flattened,
                nib_mm=layers[hatch_semantic].pen.mark_width_mm,
                density=cast(Density, density),
                angle_deg=45.0,
                cross_hatch=pattern == "cross-hatch",
            )
        hatch_layer = layers[hatch_semantic]
        for stroke in hatch_strokes:
            hatch_layer.add(
                stroke,
                source_ref=source_ref,
                role=hatch_semantic,
                attributes={
                    **attributes,
                    "data-hatch-pattern": str(pattern),
                    "data-hatch-source-primitive": str(primitive["id"]),
                },
            )
        hatch_count = len(hatch_strokes)
    return emitted, hatch_count


def _deduplicate_layer(
    layer: ArtworkLayer, *, policy: str = "approximate"
) -> tuple[ArtworkLayer, int]:
    if policy == "none":
        return layer, 0
    result = ArtworkLayer(layer.id, layer.label, layer.pen_id)
    exact_keys: set[str] = set()
    retained: list[LineString] = []
    omitted = 0
    tolerance = 0.35 * layer.pen.mark_width_mm
    for record in layer.records:
        if policy == "exact":
            key = (
                record.vector_path.canonical_json()
                if record.vector_path is not None
                else json.dumps(record.points, separators=(",", ":"))
            )
            if key in exact_keys:
                omitted += 1
                continue
            exact_keys.add(key)
            result.records.append(copy.deepcopy(record))
            continue
        geometry = LineString(record.points)
        if any(geometry.hausdorff_distance(other) <= tolerance for other in retained):
            omitted += 1
            continue
        retained.append(geometry)
        result.records.append(copy.deepcopy(record))
    return result, omitted


def _render_view(
    layers: dict[str, ArtworkLayer],
    view: dict[str, Any],
    panel: Rect,
    context: PlateContext,
    *,
    preset: str,
    density: str,
    fixed_scale: float | None = None,
    component_filter: set[str] | None = None,
    label: str | None = None,
    show_dimensions: bool = True,
    show_callouts: bool = True,
    fail_sub_pen: bool = False,
) -> tuple[Affine2D, float, list[str], int]:
    labels = layers["labels_specifications"]
    scale_suffix = (
        " / NTS"
        if view["scale_status"] in {"not-to-scale", "visible-view-only"}
        else ""
    )
    _add_panel_label(
        labels,
        (label or str(view["label"])) + scale_suffix,
        panel,
        context,
    )
    drawing = _drawing_rect(panel, context)
    if show_dimensions and view["dimensions"]:
        dimension_reserve = float(
            context.plate["type_scale_mm"]["attribution"]
        ) + float(context.plate["gap_mm"])
        if drawing.height <= dimension_reserve:
            _fail("technical view panel cannot reserve its dimension-label band.")
        drawing = Rect(
            drawing.x,
            drawing.y,
            drawing.width,
            drawing.height - dimension_reserve,
        )
    matrix, scale = _fit_matrix(
        view,
        drawing,
        fixed_scale=fixed_scale,
        component_filter=component_filter,
    )
    omitted: list[str] = []
    hatch_count = 0
    sheet = str(context.plate["sheet"])
    for primitive in view["primitives"]:
        if (
            component_filter is not None
            and primitive["component_id"] not in component_filter
        ):
            continue
        minimum_sheet = str(primitive.get("minimum_sheet", "A5"))
        if _SHEET_RANK[sheet] < _SHEET_RANK[minimum_sheet]:
            if primitive.get("detail_priority", "normal") == "identity":
                _fail(
                    f"identity primitive {primitive['id']!r} cannot be hidden by format LOD."
                )
            omitted.append(str(primitive["id"]))
            continue
        emitted, generated_hatches = _emit_primitive(
            layers,
            view,
            primitive,
            matrix,
            preset=preset,
            density=density,
        )
        hatch_count += generated_hatches
        if not emitted:
            if fail_sub_pen or primitive.get("detail_priority", "normal") == "identity":
                _fail(
                    f"source primitive {primitive['id']!r} falls below its physical three-nib floor."
                )
            omitted.append(str(primitive["id"]))
    if show_dimensions:
        _emit_dimensions(layers, view, matrix, panel, context)
    if show_callouts:
        _emit_callouts(layers, view, matrix, panel, context)
    return matrix, scale, omitted, hatch_count


def _transform_point(matrix: Affine2D, raw: Sequence[float]) -> Point:
    return matrix.apply((float(raw[0]), float(raw[1])))


def _emit_dimensions(
    layers: dict[str, ArtworkLayer],
    view: dict[str, Any],
    matrix: Affine2D,
    panel: Rect,
    context: PlateContext,
) -> None:
    dimension_layer = layers["dimensions_leaders"]
    copy_layer = layers["labels_specifications"]
    for dimension in view["dimensions"]:
        start = _transform_point(matrix, dimension["start"])
        end = _transform_point(matrix, dimension["end"])
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        minimum = 3.0 * dimension_layer.pen.mark_width_mm
        if length + 1e-9 < minimum:
            _fail(
                f"verified dimension {dimension['id']!r} is too short to plot legibly."
            )
        ux, uy = dx / length, dy / length
        px, py = -uy, ux
        head = 5.0 * dimension_layer.pen.mark_width_mm
        spread = 2.5 * dimension_layer.pen.mark_width_mm
        strokes = [
            [start, end],
            [
                (
                    start[0] + ux * head + px * spread,
                    start[1] + uy * head + py * spread,
                ),
                start,
                (
                    start[0] + ux * head - px * spread,
                    start[1] + uy * head - py * spread,
                ),
            ],
            [
                (end[0] - ux * head + px * spread, end[1] - uy * head + py * spread),
                end,
                (end[0] - ux * head - px * spread, end[1] - uy * head - py * spread),
            ],
        ]
        attributes = {
            "data-dimension-id": str(dimension["id"]),
            "data-dimension-value": str(dimension["value"]),
            "data-dimension-unit": str(dimension["unit"]),
            "data-dimension-qualifier": str(dimension["qualifier"]),
            "data-source-refs": str(dimension["source_ref"]),
            "data-claim-status": "verified-supplied-dimension",
            "data-view-id": str(view["id"]),
        }
        for stroke in strokes:
            if polyline_length_mm(stroke) + 1e-9 >= minimum:
                dimension_layer.add(
                    stroke,
                    source_ref=str(dimension["source_ref"]),
                    role="dimension",
                    attributes=attributes,
                )
        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        copy_text = f"{dimension['label']} / {dimension['value']} {dimension['unit']}"
        cap = float(context.plate["type_scale_mm"]["attribution"])
        gap = float(context.plate["gap_mm"])
        label_y = (
            min(panel.bottom - cap, midpoint[1] + gap / 2.0)
            if abs(dx) >= abs(dy)
            else max(panel.y, midpoint[1] - cap - gap / 2.0)
        )
        add_text(
            copy_layer,
            copy_text,
            x_mm=midpoint[0],
            y_mm=label_y,
            preferred_cap_mm=cap,
            maximum_width_mm=panel.width,
            anchor="middle",
            minimum_cap_mm=8.0 * copy_layer.pen.mark_width_mm,
            allow_horizontal_condense=True,
            source_ref=str(dimension["source_ref"]),
            role="dimension-label",
            attributes=attributes,
        )


def _emit_callouts(
    layers: dict[str, ArtworkLayer],
    view: dict[str, Any],
    matrix: Affine2D,
    panel: Rect,
    context: PlateContext,
) -> None:
    leader_layer = layers["dimensions_leaders"]
    copy_layer = layers["labels_specifications"]
    for callout in view["callouts"]:
        anchor = _transform_point(matrix, callout["anchor"])
        label_point = _transform_point(matrix, callout["label_position"])
        leader = [anchor, label_point]
        if polyline_length_mm(leader) < 3.0 * leader_layer.pen.mark_width_mm:
            _fail(f"callout {callout['id']!r} leader is physically too short.")
        attributes = {
            "data-callout-id": str(callout["id"]),
            "data-component-id": str(callout["component_id"]),
            "data-source-refs": str(callout["source_ref"]),
            "data-claim-status": "source-supplied-callout",
            "data-view-id": str(view["id"]),
        }
        leader_layer.add(
            leader,
            source_ref=str(callout["source_ref"]),
            role="callout-leader",
            attributes=attributes,
        )
        add_text(
            copy_layer,
            str(callout["label"]),
            x_mm=min(max(label_point[0], panel.left), panel.right),
            y_mm=min(max(label_point[1], panel.top), panel.bottom),
            preferred_cap_mm=float(context.plate["type_scale_mm"]["attribution"]),
            maximum_width_mm=max(panel.right - label_point[0], panel.width / 2.0),
            minimum_cap_mm=8.0 * copy_layer.pen.mark_width_mm,
            allow_horizontal_condense=True,
            source_ref=str(callout["source_ref"]),
            role="callout-label",
            attributes=attributes,
        )


def _selected_views(record: dict[str, Any]) -> list[dict[str, Any]]:
    selected_ids = list(record["style"].get("selected_view_ids", []))
    if not selected_ids:
        return list(record["views"])
    by_id = {str(view["id"]): view for view in record["views"]}
    return [by_id[view_id] for view_id in selected_ids]


def _view_by_preference(
    views: Sequence[dict[str, Any]], preferences: Sequence[str]
) -> dict[str, Any]:
    for kind in preferences:
        for view in views:
            if view["type"] == kind:
                return view
    return views[0]


def _combined_bottom(context: PlateContext) -> Rect:
    left = context.zones["technical_bottom_left"]
    right = context.zones["technical_bottom_right"]
    return Rect(left.x, left.y, right.right - left.x, left.height)


def _grid_panels(rect: Rect, count: int, gap: float) -> list[Rect]:
    columns = 2 if count <= 4 else 3
    rows = math.ceil(count / columns)
    width = (rect.width - gap * (columns - 1)) / columns
    height = (rect.height - gap * (rows - 1)) / rows
    if min(width, height) <= 0:
        _fail("technical grid cannot fit the requested number of supplied views.")
    return [
        Rect(
            rect.x + (index % columns) * (width + gap),
            rect.y + (index // columns) * (height + gap),
            width,
            height,
        )
        for index in range(count)
    ]


def _common_scale(
    assignments: Sequence[tuple[dict[str, Any], Rect]],
    context: PlateContext,
) -> float | None:
    if not assignments:
        return None
    units = {str(view["unit"]) for view, _ in assignments}
    if len(units) != 1 or any(
        view["scale_status"] not in {"verified-common-scale", "dimension-calibrated"}
        for view, _ in assignments
    ):
        return None
    scales = []
    for view, panel in assignments:
        drawing = _drawing_rect(panel, context)
        min_x, min_y, max_x, max_y = _view_fit_bounds(view)
        scales.append(
            min(
                drawing.width / max(max_x - min_x, 1e-12),
                drawing.height / max(max_y - min_y, 1e-12),
            )
        )
    return min(scales)


def _add_blueprint_grid(layers: dict[str, ArtworkLayer], context: PlateContext) -> int:
    layer = layers["construction_geometry"]
    field = context.zones["technical_field"]
    grid_top = _drawing_rect(field, context).top
    spacing = float(context.plate["gap_mm"])
    attributes = {
        "data-claim-status": "non-dimensional-reference-grid",
        "data-semantic-class": "construction_geometry",
        "data-composition-preset": "blueprint-drawing",
    }
    count = 0
    x = field.left
    while x <= field.right + 1e-9:
        layer.add(
            [(x, grid_top), (x, field.bottom)],
            role="reference-grid",
            attributes=attributes,
        )
        count += 1
        x += spacing
    y = grid_top
    while y <= field.bottom + 1e-9:
        layer.add(
            [(field.left, y), (field.right, y)],
            role="reference-grid",
            attributes=attributes,
        )
        count += 1
        y += spacing
    cap = float(context.plate["type_scale_mm"]["attribution"])
    add_text(
        layers["labels_specifications"],
        "REFERENCE GRID / NON-DIMENSIONAL",
        x_mm=field.right,
        y_mm=field.top,
        preferred_cap_mm=cap,
        maximum_width_mm=field.width / 2.0,
        minimum_cap_mm=8.0 * layers["labels_specifications"].pen.mark_width_mm,
        allow_horizontal_condense=True,
        anchor="end",
        role="grid-disclaimer",
    )
    return count


def _wrap_copy(
    values: Sequence[str],
    *,
    maximum_width_mm: float,
    cap_height_mm: float,
    maximum_lines: int,
) -> list[str]:
    result: list[str] = []
    for value in values:
        words = value.split()
        if not words:
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if (
                text_width_mm(candidate, cap_height_mm=cap_height_mm)
                <= maximum_width_mm
            ):
                current = candidate
            else:
                result.append(current)
                current = word
        result.append(current)
    expanded: list[str] = []
    for line in result:
        if text_width_mm(line, cap_height_mm=cap_height_mm) <= maximum_width_mm:
            expanded.append(line)
            continue
        remaining = line
        while remaining:
            low, high = 1, len(remaining)
            fit = 0
            while low <= high:
                middle = (low + high) // 2
                if (
                    text_width_mm(remaining[:middle], cap_height_mm=cap_height_mm)
                    <= maximum_width_mm
                ):
                    fit = middle
                    low = middle + 1
                else:
                    high = middle - 1
            if fit == 0:
                _fail(f"copy {line!r} cannot fit its technical copy panel.")
            expanded.append(remaining[:fit])
            remaining = remaining[fit:]
    if len(expanded) > maximum_lines:
        _fail(
            f"technical copy needs {len(expanded)} lines but its named zone permits {maximum_lines}; shorten it."
        )
    return expanded


def _add_copy_block(
    layer: ArtworkLayer,
    values: Sequence[str],
    rect: Rect,
    context: PlateContext,
    *,
    source_ref: str | None,
    role: str,
) -> list[str]:
    cap = max(
        float(context.plate["type_scale_mm"]["detail"]),
        8.0 * layer.pen.mark_width_mm,
    )
    step = cap + 3.0 * layer.pen.mark_width_mm
    maximum_lines = max(1, int((rect.height + 1e-9) // step))
    lines = _wrap_copy(
        values,
        maximum_width_mm=rect.width,
        cap_height_mm=cap,
        maximum_lines=maximum_lines,
    )
    total_height = cap + step * (len(lines) - 1)
    start_y = rect.y + max((rect.height - total_height) / 2.0, 0.0)
    for index, line in enumerate(lines):
        add_text(
            layer,
            line,
            x_mm=rect.x,
            y_mm=start_y + index * step,
            preferred_cap_mm=cap,
            maximum_width_mm=rect.width,
            minimum_cap_mm=8.0 * layer.pen.mark_width_mm,
            allow_horizontal_condense=True,
            source_ref=source_ref,
            role=role,
            attributes={"data-copy-line-index": str(index + 1)},
        )
    return lines


def _owner_copy(record: dict[str, Any]) -> list[str]:
    identity = record["identity"]
    lines: list[str] = []
    if identity.get("owner"):
        lines.append(f"OWNER / {identity['owner']}")
    for identifier in identity.get("identifiers", []):
        lines.append(
            f"{str(identifier['kind']).replace('-', ' ').upper()} / {identifier['value']}"
        )
    for date_record in identity.get("dates", []):
        lines.append(
            f"{str(date_record['kind']).replace('-', ' ').upper()} / {date_record['value']}"
        )
    if identity.get("dedication"):
        lines.append(f"DEDICATION / {identity['dedication']}")
    if identity.get("story"):
        lines.append(f"STORY / {identity['story']}")
    return lines


def _spec_copy(record: dict[str, Any]) -> list[str]:
    return [
        (
            f"{item['label']} / {item['value']}"
            if item["unit"] == "display"
            else f"{item['label']} / {item['value']} {item['unit']}"
        )
        for item in record["specifications"]
        if item["selected"]
    ]


def _history_copy(record: dict[str, Any]) -> list[str]:
    return [
        f"{item['date']} / {item['text']}"
        for item in record.get("history", [])
        if item["selected"]
    ]


def _engineering_copy(
    record: dict[str, Any], context: PlateContext
) -> tuple[list[str], str]:
    """Return a size-bounded, fully sourced facts/history block."""

    sheet = str(context.plate["sheet"])
    # A3 is the collector master and has enough physical copy depth for the
    # complete selected specification set plus two sourced milestones.  A4
    # retains a useful performance/configuration fact after the dimensions;
    # A5 deliberately keeps the compact essentials.
    spec_limit = {"A5": 2, "A4": 4, "A3": 5}[sheet]
    history_limit = {"A5": 1, "A4": 1, "A3": 2}[sheet]
    selected_specs = [item for item in record["specifications"] if item["selected"]][
        :spec_limit
    ]
    selected_history = [item for item in record.get("history", []) if item["selected"]][
        :history_limit
    ]
    identity = record["identity"]
    model_copy = " / ".join(
        str(value)
        for value in (identity.get("manufacturer"), identity["model"])
        if value
    )
    values = [f"MODEL / {model_copy}"]
    values.extend(
        (
            f"{item['label']} / {item['value']}"
            if item["unit"] == "display"
            else f"{item['label']} / {item['value']} {item['unit']}"
        )
        for item in selected_specs
    )
    history_character_limit = {"A5": 72, "A4": 100, "A3": 280}[sheet]
    for item in selected_history:
        text = str(item["text"])
        if len(text) > history_character_limit:
            shortened = text[: history_character_limit - 3].rsplit(" ", 1)[0]
            text = (shortened or text[: history_character_limit - 3]) + "..."
        values.append(f"{item['date']} / {text}")
    refs = list(
        dict.fromkeys(
            str(item["source_ref"]) for item in (*selected_specs, *selected_history)
        )
    )
    return values, "|".join(refs)


def _orthographic_panels(
    record: dict[str, Any], context: PlateContext
) -> tuple[list[tuple[dict[str, Any], Rect]], Rect]:
    """Derive a category-aware 2/3/4-view composition inside the binding field."""

    field = context.zones["technical_field"]
    gap = float(context.plate["gap_mm"])
    sheet = str(context.plate["sheet"])
    # A3 carries five selected specifications and two sourced milestones.  Its
    # copy rail therefore receives a little more depth than the compact tiers;
    # the remaining 75% still leaves the largest drawing field in the system.
    data_fraction = {"A5": 0.26, "A4": 0.23, "A3": 0.25}[sheet]
    data_height = field.height * data_fraction
    drawing = Rect(field.x, field.y, field.width, field.height - data_height - gap)
    data = Rect(field.x, drawing.bottom + gap, field.width, data_height)
    views = _selected_views(record)
    by_type = {str(view["type"]): view for view in views}
    category = str(record["category"])

    if category in {"car", "racing-car"}:
        top_height = drawing.height * 0.55
        top = Rect(drawing.x, drawing.y, drawing.width, top_height)
        lower = Rect(
            drawing.x,
            top.bottom + gap,
            drawing.width,
            drawing.height - top.height - gap,
        )
        lower_width = (lower.width - 2.0 * gap) / 3.0
        lower_panels = [
            Rect(
                lower.x + index * (lower_width + gap),
                lower.y,
                lower_width,
                lower.height,
            )
            for index in range(3)
        ]
        return [
            (by_type["side"], top),
            (by_type["plan"], lower_panels[0]),
            (by_type["front"], lower_panels[1]),
            (by_type["rear"], lower_panels[2]),
        ], data
    if category in {"aircraft", "glider"}:
        top_height = drawing.height * 0.55
        top = Rect(drawing.x, drawing.y, drawing.width, top_height)
        lower = Rect(
            drawing.x,
            top.bottom + gap,
            drawing.width,
            drawing.height - top.height - gap,
        )
        lower_panels = _grid_panels(lower, 2, gap)
        return [
            (by_type["plan"], top),
            (by_type["side"], lower_panels[0]),
            (by_type["front"], lower_panels[1]),
        ], data
    if category in {
        "boat",
        "yacht",
        "rowing-shell",
        "ship",
        "personal-watercraft",
    }:
        height = (drawing.height - gap) / 2.0
        return [
            (by_type["side"], Rect(drawing.x, drawing.y, drawing.width, height)),
            (
                by_type["plan"],
                Rect(drawing.x, drawing.y + height + gap, drawing.width, height),
            ),
        ], data
    panels = _grid_panels(drawing, len(views), gap)
    return list(zip(views, panels, strict=True)), data


def _render_workshop(
    layers: dict[str, ArtworkLayer],
    record: dict[str, Any],
    view: dict[str, Any],
    context: PlateContext,
    omitted: list[str],
) -> int:
    main_panel = context.zones["technical_left"]
    matrix, _, main_omitted, hatch_count = _render_view(
        layers,
        view,
        main_panel,
        context,
        preset=str(record["preset"]),
        density=str(record["style"]["density"]),
        show_dimensions=bool(record["style"].get("show_dimensions", True)),
        show_callouts=bool(record["style"].get("show_callouts", True)),
        fail_sub_pen=record["style"].get("sub_pen_policy", "report") == "fail",
    )
    omitted.extend(main_omitted)
    right_top = context.zones["technical_right_top"]
    right_bottom = context.zones["technical_right_bottom"]
    right = Rect(
        right_top.x, right_top.y, right_top.width, right_bottom.bottom - right_top.y
    )
    components = list(record["highlighted_components"])
    gap = float(context.plate["gap_mm"])
    slot_height = (right.height - gap * (len(components) - 1)) / len(components)
    construction = layers["construction_geometry"]
    for index, component in enumerate(components):
        slot = Rect(
            right.x, right.y + index * (slot_height + gap), right.width, slot_height
        )
        radius = min(slot.width, slot.height) / 2.0 - gap / 2.0
        if radius <= 0:
            _fail("workshop detail circles cannot fit the named right-hand field.")
        centre = slot.centre
        circle = circle_path(centre, radius)
        construction.add_path(
            circle,
            role="detail-circle",
            attributes={
                "data-component-id": str(component),
                "data-detail-index": str(index + 1),
            },
        )
        detail_panel = Rect(
            centre[0] - radius + gap / 2.0,
            centre[1] - radius + gap / 2.0,
            2.0 * radius - gap,
            2.0 * radius - gap,
        )
        detail_matrix, _, detail_omitted, detail_hatches = _render_view(
            layers,
            view,
            detail_panel,
            context,
            preset=str(record["preset"]),
            density=str(record["style"]["density"]),
            component_filter={str(component)},
            label=f"DETAIL {index + 1} / {str(component).replace('-', ' ')}",
            show_dimensions=False,
            show_callouts=False,
            fail_sub_pen=record["style"].get("sub_pen_policy", "report") == "fail",
        )
        del detail_matrix
        omitted.extend(detail_omitted)
        hatch_count += detail_hatches
        component_points = [
            matrix.apply(point)
            for primitive in view["primitives"]
            if primitive["component_id"] == component
            for point in _primitive_path(primitive).flatten(0.05).points
        ]
        anchor = (
            sum(point[0] for point in component_points) / len(component_points),
            sum(point[1] for point in component_points) / len(component_points),
        )
        direction_x, direction_y = centre[0] - anchor[0], centre[1] - anchor[1]
        distance = math.hypot(direction_x, direction_y)
        end = (
            centre[0] - direction_x / distance * radius,
            centre[1] - direction_y / distance * radius,
        )
        leader = [anchor, end]
        if polyline_length_mm(leader) >= 3.0 * construction.pen.mark_width_mm:
            construction.add(
                leader,
                role="detail-leader",
                attributes={
                    "data-component-id": str(component),
                    "data-detail-index": str(index + 1),
                },
            )
    return hatch_count


def compose_motion_context(
    layers: dict[str, ArtworkLayer],
    context_artwork: PlateArtwork,
    target: Rect,
    *,
    layer_ids: Sequence[str],
) -> int:
    """Reuse another generator's factual strokes inside a technical plate."""

    if context_artwork.domain == "technical-objects":
        _fail("motion context must come from another existing generator domain.")
    selected = [layer for layer in context_artwork.layers if layer.id in set(layer_ids)]
    missing = sorted(set(layer_ids) - {layer.id for layer in selected})
    if missing:
        _fail("motion context is missing requested layers: " + ", ".join(missing) + ".")
    records = [record for layer in selected for record in layer.records]
    if not records:
        _fail("motion context selected no factual geometry.")
    source = context_artwork.context.field
    scale = min(target.width / source.width, target.height / source.height)
    used_width, used_height = source.width * scale, source.height * scale
    matrix = Affine2D(
        a=scale,
        d=scale,
        e=target.x + (target.width - used_width) / 2.0 - source.x * scale,
        f=target.y + (target.height - used_height) / 2.0 - source.y * scale,
    )
    destination = layers["background_context"]
    count = 0
    for source_layer in selected:
        for record in source_layer.records:
            attributes = {
                **record.attributes,
                "data-context-domain": context_artwork.domain,
                "data-context-subject-id": context_artwork.subject_id,
                "data-context-layer-id": source_layer.id,
                "data-claim-status": "reused-factual-context",
            }
            if record.vector_path is not None:
                path = record.vector_path.transformed(matrix)
                if path.length(0.001) >= 3.0 * destination.pen.mark_width_mm:
                    destination.add_path(
                        path,
                        source_ref=record.source_ref,
                        role="background-context",
                        attributes=attributes,
                    )
                    count += 1
            else:
                points = [matrix.apply(point) for point in record.points]
                points = simplify_contour(
                    points, tolerance=0.5 * destination.pen.mark_width_mm
                )
                if polyline_length_mm(points) >= 3.0 * destination.pen.mark_width_mm:
                    destination.add(
                        points,
                        source_ref=record.source_ref,
                        role="background-context",
                        attributes=attributes,
                    )
                    count += 1
    if count == 0:
        _fail("motion context vanished at the selected physical scale.")
    return count


def _layout_record(
    record: dict[str, Any],
    context: PlateContext,
    *,
    context_artwork: PlateArtwork | None,
) -> tuple[list[ArtworkLayer], dict[str, Any]]:
    layers = _layer_map(context)
    views = _selected_views(record)
    preset = str(record["preset"])
    density = str(record["style"]["density"])
    show_dimensions = bool(record["style"].get("show_dimensions", True))
    show_callouts = bool(record["style"].get("show_callouts", True))
    fail_sub_pen = record["style"].get("sub_pen_policy", "report") == "fail"
    omitted: list[str] = []
    hatch_count = 0
    common_scale: float | None = None
    panel_records: list[dict[str, Any]] = []
    data_block_record: dict[str, Any] | None = None

    def render_assignment(
        view: dict[str, Any],
        panel: Rect,
        *,
        fixed_scale: float | None = None,
        label: str | None = None,
        component_filter: set[str] | None = None,
    ) -> Affine2D:
        nonlocal hatch_count
        matrix, scale, view_omitted, view_hatches = _render_view(
            layers,
            view,
            panel,
            context,
            preset=preset,
            density=density,
            fixed_scale=fixed_scale,
            component_filter=component_filter,
            label=label,
            show_dimensions=show_dimensions,
            show_callouts=show_callouts,
            fail_sub_pen=fail_sub_pen,
        )
        omitted.extend(view_omitted)
        hatch_count += view_hatches
        panel_record: dict[str, Any] = {
            "view_id": str(view["id"]),
            "view_type": str(view["type"]),
            "panel_mm": panel.as_dict(),
            "model_to_paper_mm_per_unit": round(scale, 9),
            "source_scale_status": str(view["scale_status"]),
        }
        panel_records.append(panel_record)
        raster_sampling = view.get("raster_sampling")
        if raster_sampling is not None:
            bbox = raster_sampling["useful_subject_bbox"]
            source_width = float(bbox[2])
            source_height = float(bbox[3])
            drawing = _drawing_rect(panel, context)
            min_x, min_y, max_x, max_y = _view_fit_bounds(
                view, component_filter=component_filter
            )
            natural_scale = min(
                drawing.width / max(max_x - min_x, 1e-12),
                drawing.height / max(max_y - min_y, 1e-12),
            )
            panel_record["raster_sampling"] = {
                "method": str(raster_sampling["method"]),
                "useful_subject_bbox_source_px": list(bbox),
                "source_pixel_to_paper_mm": round(scale, 9),
                "projected_half_pixel_mm": round(scale / 2.0, 9),
                "maximum_projected_half_pixel_mm": float(
                    raster_sampling["maximum_projected_half_pixel_mm"]
                ),
                "placed_useful_subject_mm": [
                    round(source_width * scale, 6),
                    round(source_height * scale, 6),
                ],
                "unclamped_panel_fit_mm_per_source_pixel": round(natural_scale, 9),
                "sampling_limit_applied": (
                    fixed_scale is None
                    and natural_scale
                    > 2.0 * float(raster_sampling["maximum_projected_half_pixel_mm"])
                    + 1e-9
                ),
                "scale_limiter": (
                    "shared-scale"
                    if fixed_scale is not None
                    else (
                        "sampling-limit"
                        if natural_scale
                        > 2.0
                        * float(raster_sampling["maximum_projected_half_pixel_mm"])
                        + 1e-9
                        else "panel-fit"
                    )
                ),
            }
        return matrix

    if preset == "orthographic-collection":
        assignments, data_panel = _orthographic_panels(record, context)
        for view, panel in assignments:
            render_assignment(view, panel, label=str(view["label"]))
        data_values, data_source_refs = _engineering_copy(record, context)
        if not data_values:
            _fail(
                "orthographic-collection requires selected specifications or history."
            )
        emitted_copy = _add_copy_block(
            layers["labels_specifications"],
            data_values,
            data_panel,
            context,
            source_ref=data_source_refs or None,
            role="engineering-facts-history",
        )
        data_block_record = {
            "panel_mm": data_panel.as_dict(),
            "line_count": len(emitted_copy),
            "lines": emitted_copy,
            "source_refs": data_source_refs.split("|") if data_source_refs else [],
        }
    elif preset == "three-view-technical-plate":
        side = _view_by_preference(views, ("side", "elevation"))
        front = _view_by_preference(views, ("front", "rear"))
        plan = _view_by_preference(views, ("plan",))
        assignments = [
            (side, context.zones["technical_top"]),
            (front, context.zones["technical_bottom_left"]),
            (plan, context.zones["technical_bottom_right"]),
        ]
        if (
            record["style"].get("scale_policy", "shared-when-verified")
            == "shared-when-verified"
        ):
            common_scale = _common_scale(assignments, context)
        for view, panel in assignments:
            render_assignment(view, panel, fixed_scale=common_scale)
    elif preset == "patent-plate":
        selected = views[:4]
        panels = _grid_panels(
            context.zones["technical_field"],
            len(selected),
            float(context.plate["gap_mm"]),
        )
        for index, (view, panel) in enumerate(
            zip(selected, panels, strict=True), start=1
        ):
            render_assignment(view, panel, label=str(view["label"]))
            marker = (
                panel.right - float(context.plate["gap_mm"]) / 2.0,
                panel.y + float(context.plate["gap_mm"]) / 2.0,
            )
            add_number_marker(
                layers["construction_geometry"],
                layers["labels_specifications"],
                marker,
                str(index),
                radius_mm=5.0 * layers["construction_geometry"].pen.mark_width_mm,
            )
    elif preset == "historic-evolution":
        by_id = {str(view["id"]): view for view in views}
        evolution = list(record["evolution"])
        panels = _grid_panels(
            context.zones["technical_field"],
            len(evolution),
            float(context.plate["gap_mm"]),
        )
        assignments = [
            (by_id[str(item["view_id"])], panel)
            for item, panel in zip(evolution, panels, strict=True)
        ]
        if (
            record["style"].get("scale_policy", "shared-when-verified")
            == "shared-when-verified"
        ):
            common_scale = _common_scale(assignments, context)
        for item, (view, panel) in zip(evolution, assignments, strict=True):
            render_assignment(
                view,
                panel,
                fixed_scale=common_scale,
                label=f"{item['year']} / {item['label']}",
            )
    elif preset == "workshop-manual":
        view = _view_by_preference(
            views, ("side", "elevation", "three-quarter", "plan")
        )
        hatch_count += _render_workshop(layers, record, view, context, omitted)
    elif preset in {
        "specification-portrait",
        "owners-machine",
        "motion-and-engineering",
    }:
        view = _view_by_preference(
            views, ("side", "elevation", "three-quarter", "plan")
        )
        render_assignment(view, context.zones["technical_top"])
        bottom = _combined_bottom(context)
        if preset == "specification-portrait":
            _add_copy_block(
                layers["labels_specifications"],
                _spec_copy(record),
                bottom,
                context,
                source_ref="|".join(
                    dict.fromkeys(
                        str(item["source_ref"])
                        for item in record["specifications"]
                        if item["selected"]
                    )
                ),
                role="specification-block",
            )
        elif preset == "owners-machine":
            _add_copy_block(
                layers["accent_feature"],
                _owner_copy(record),
                bottom,
                context,
                source_ref=None,
                role="owner-personalisation",
            )
        else:
            if context_artwork is None:
                _fail(
                    "motion-and-engineering requires a PlateArtwork from an existing factual generator."
                )
            compose_motion_context(
                layers,
                context_artwork,
                bottom,
                layer_ids=list(record["context"]["layer_ids"]),
            )
    elif preset == "component-anatomy":
        component = str(record["highlighted_components"][0])
        view = next(
            (
                candidate
                for candidate in views
                if any(
                    primitive["component_id"] == component
                    for primitive in candidate["primitives"]
                )
            ),
            views[0],
        )
        render_assignment(
            view,
            context.zones["technical_field"],
            component_filter={component},
            label=f"COMPONENT / {component.replace('-', ' ')}",
        )
    else:
        preferred = {
            "exploded-assembly": ("exploded",),
            "cutaway-section": ("section", "cutaway"),
        }.get(preset, ("side", "elevation", "three-quarter", "plan", "front"))
        view = _view_by_preference(views, preferred)
        if preset == "blueprint-drawing":
            _add_blueprint_grid(layers, context)
        render_assignment(view, context.zones["technical_field"])

    deduplicated_layers: list[ArtworkLayer] = []
    duplicate_count = 0
    deduplication_policy = str(
        record["style"].get("deduplication_policy", "approximate")
    )
    for layer in layers.values():
        if not layer.records:
            continue
        deduplicated, count = _deduplicate_layer(layer, policy=deduplication_policy)
        duplicate_count += count
        if deduplicated.records:
            deduplicated_layers.append(deduplicated)
    principal_present = any(
        layer.id == "object_principal_silhouette" for layer in deduplicated_layers
    )
    component_structure_present = any(
        layer.id
        in {
            "object_major_structural_edges",
            "object_mechanical_detail",
            "object_internal_cutaway_structure",
        }
        for layer in deduplicated_layers
    )
    if not principal_present and not (
        preset == "component-anatomy" and component_structure_present
    ):
        _fail(
            "technical object emitted no identity-carrying silhouette or "
            "component structure; model identity is unsupported."
        )
    omitted_ids = sorted(set(omitted))
    sheet_rank = _SHEET_RANK[str(context.plate["sheet"])]
    format_lod_ids = sorted(
        {
            str(primitive["id"])
            for view in views
            for primitive in view["primitives"]
            if _SHEET_RANK[str(primitive.get("minimum_sheet", "A5"))] > sheet_rank
        }
        if preset == "orthographic-collection"
        else set()
    )
    sub_pen_ids = sorted(set(omitted_ids) - set(format_lod_ids))
    collection = record.get("collection")
    collection_geometry_ready = not isinstance(collection, dict) or bool(
        collection.get("geometry_release_ready", True)
    )
    geometry_blockers: list[str] = []
    if sub_pen_ids:
        geometry_blockers.append(
            f"{len(sub_pen_ids)} source primitive(s) fall below the resolved physical three-nib floor"
        )
    if not collection_geometry_ready:
        review_status = str(
            collection.get("geometry_review_status", "illustrative-review-only")
        )
        geometry_blockers.append(
            "collection geometry remains "
            + review_status.replace("-", " ")
            + "; model-specific contour verification is not claimed"
        )
    return deduplicated_layers, {
        "composition_preset": preset,
        "composition_name": PRESETS[preset],
        "source_level": int(record["source_level"]),
        "claim_scope": str(record["claim_scope"]),
        "visible_geometry_only": int(record["source_level"]) in {2, 3},
        "common_scale_guaranteed": common_scale is not None,
        "common_model_to_paper_mm_per_unit": (
            round(common_scale, 9) if common_scale is not None else None
        ),
        "view_panels": panel_records,
        "engineering_data_block": data_block_record,
        "density": density,
        "omitted_sub_pen_primitive_ids": sub_pen_ids,
        "omitted_format_lod_primitive_ids": format_lod_ids,
        "source_geometry_release_ready": not geometry_blockers,
        "source_geometry_release_blockers": geometry_blockers,
        "collection_geometry_review_status": (
            str(collection.get("geometry_review_status"))
            if isinstance(collection, dict) and collection.get("geometry_review_status")
            else None
        ),
        "duplicate_contours_removed": duplicate_count,
        "deduplication_policy": deduplication_policy,
        "sub_pen_policy": str(record["style"].get("sub_pen_policy", "report")),
        "generated_hatch_stroke_count": hatch_count,
        "excluded_features": copy.deepcopy(record.get("excluded_features", [])),
        "geometry_sha256": str(record["geometry_sha256"]),
        "technical_zone_authority": "plate-format-v1.technical_zones_mm",
        "technical_pen_authority": "plate-format-v1.technical_pen_roles",
        "format_lod": {
            "sheet": str(context.plate["sheet"]),
            "policy": "identity-always; optional-details-by-minimum-sheet; three-nib-floor",
        },
        "raw_photographic_edge_map_emitted": False,
        "unsupported_hidden_geometry_generated": False,
    }


def _details(record: dict[str, Any], context: PlateContext) -> tuple[str, str, str]:
    level = int(record["source_level"])
    if record["preset"] == "orthographic-collection":
        return (
            f"{len(record['views'])}-VIEW {str(record['category']).replace('-', ' ').upper()} / ORIGINAL VECTOR",
            "FACT-SOURCED / VIEWS FIT INDEPENDENTLY",
            "ILLUSTRATIVE / NOT CERTIFIED",
        )
    scale_statuses = {str(view["scale_status"]) for view in record["views"]}
    scale = (
        "DIMENSION-ANCHORED / INDEPENDENT VIEW FIT"
        if record["preset"] == "orthographic-collection"
        else (
            "VERIFIED SCALE"
            if scale_statuses <= {"verified-common-scale", "dimension-calibrated"}
            else "MIXED SCALE / SEE VIEWS"
        )
    )
    identifiers = list(record["identity"].get("identifiers", []))
    identity_copy = (
        f"{identifiers[0]['kind'].replace('-', ' ').upper()} / {identifiers[0]['value']}"
        if identifiers
        else f"MODEL / {record['identity']['model']}"
    )
    return (
        f"{PRESETS[str(record['preset'])]} / {str(record['category']).replace('-', ' ').upper()}",
        f"LEVEL {level} / {scale}",
        identity_copy,
    )


def _credit_line(
    sources: Sequence[dict[str, Any]], fact_sources: Sequence[dict[str, Any]] = ()
) -> str:
    geometry_credits = list(
        dict.fromkeys(str(source["visible_credit"]) for source in sources)
    )
    fact_publishers = list(
        dict.fromkeys(str(source["publisher"]) for source in fact_sources)
    )
    if fact_publishers:
        return "ORIGINAL STUDY / SOURCED FACTS"
    return geometry_credits[0] if geometry_credits else "ORIGINAL STUDY"


def build_technical_plate(
    record: Any,
    format_id: str | None = None,
    preset: str | None = None,
    density: str | None = None,
    *,
    context_artwork: PlateArtwork | None = None,
    release_binding: Path | str | None = None,
) -> PlateArtwork:
    """Compile one engineered object through the shared physical plate exporter.

    Known real-subject collection ids may compile to an in-memory review, but
    they are tagged as export-blocked unless their exact record is independently
    revalidated against a canonical v2 release binding.  The binding path is
    evidence to re-check, not a caller-controlled trust flag.
    """

    candidate = copy.deepcopy(_object(record, "record"))
    if format_id is not None:
        candidate["format_id"] = format_id
    if preset is not None:
        candidate["preset"] = preset
    if density is not None:
        candidate.setdefault("style", {})["density"] = density
    # Variant overrides alter composition metadata, not source geometry, so the
    # content digest remains the record's view/assembly/evolution digest.
    checked = validate_technical_record(candidate)
    # Local import avoids the technical -> collections -> technical import
    # cycle while keeping this public Python entry point fail-closed.
    from .technical_source_audit import (
        is_approved_unbound_demonstrator,
        match_named_subject_identity,
        validate_named_subject_release_bindings,
    )

    approved_demonstrator = is_approved_unbound_demonstrator(checked)
    identity_match = match_named_subject_identity(checked)
    known_subject_id = identity_match[0] if identity_match is not None else None
    known_collection = identity_match[1] if identity_match is not None else None
    release_authorizations: dict[str, Path] = {}
    if release_binding is not None:
        release_authorizations = validate_named_subject_release_bindings(
            [checked],
            (release_binding,),
        )
    context = _technical_context(str(checked["format_id"]))
    layers, rendering = _layout_record(
        checked,
        context,
        context_artwork=context_artwork,
    )
    authorized_path = release_authorizations.get(str(checked["id"]))
    technical_release_authorized = approved_demonstrator or authorized_path is not None
    rendering["technical_release_authorized"] = technical_release_authorized
    rendering["technical_release_mode"] = (
        "built-in-demonstrator"
        if approved_demonstrator
        else ("v2-binding" if authorized_path is not None else "blocked")
    )
    rendering["technical_release_binding_path"] = (
        str(authorized_path) if authorized_path is not None else None
    )
    rendering["technical_release_artwork_sha256"] = None
    if known_collection is not None:
        rendering["named_source_release_collection"] = known_collection
        rendering["named_source_release_subject_id"] = known_subject_id
        rendering["named_source_release_authorized"] = authorized_path is not None
        rendering["named_source_release_binding_path"] = (
            str(authorized_path) if authorized_path is not None else None
        )
        rendering["named_source_release_artwork_sha256"] = None
    geometry_sources = tuple(copy.deepcopy(checked["sources"]))
    fact_sources = tuple(copy.deepcopy(checked.get("fact_sources", [])))
    sources = (*geometry_sources, *fact_sources)
    if context_artwork is not None and checked["preset"] == "motion-and-engineering":
        sources = (*sources, *copy.deepcopy(context_artwork.sources))
    notes = tuple(str(note) for note in checked["notes"])
    notes += (
        str(checked["claim_scope"]),
        f"SOURCE LEVEL {checked['source_level']} TRUTHFULNESS POLICY APPLIED",
    )
    source_provider = " / ".join(
        dict.fromkeys(
            str(source.get("publisher") or source["attribution"])
            for source in (*checked["sources"], *checked.get("fact_sources", []))
        )
    )
    source_license = " / ".join(
        dict.fromkeys(
            str(source["license"])
            for source in (*checked["sources"], *checked.get("fact_sources", []))
        )
    )
    snapshot_values = [
        str(source["captured_at"])
        for source in (*checked["sources"], *checked.get("fact_sources", []))
        if source.get("captured_at")
    ]
    protected_marks = any(
        primitive.get("protected_mark")
        for view in checked["views"]
        for primitive in view["primitives"]
    )
    variant_id = None
    if preset is not None or density is not None or format_id is not None:
        variant_id = "-".join(
            part
            for part in (
                str(checked["preset"]),
                str(checked["style"]["density"]),
                str(checked["format_id"]),
            )
            if part
        )
    artwork = PlateArtwork(
        subject_id=str(checked["id"]),
        variant_id=variant_id,
        domain="technical-objects",
        subject_kind=str(checked["category"]),
        title=str(checked["title"]),
        subtitle=str(checked["subtitle"]),
        details=_details(checked, context),
        credit_line=_credit_line(list(checked["sources"]), list(fact_sources)),
        scale_status=(
            "illustrative-dimension-anchored-envelopes"
            if checked["preset"] == "orthographic-collection"
            and all(
                view["scale_status"] == "dimension-anchored-envelope"
                for view in checked["views"]
            )
            else (
                "verified-or-dimension-calibrated-where-marked"
                if all(
                    view["scale_status"]
                    in {"verified-common-scale", "dimension-calibrated"}
                    for view in checked["views"]
                )
                else "mixed-scale-explicit-nts"
            )
        ),
        evidence_status=(
            "project-authored-illustrative"
            if checked["preset"] == "orthographic-collection"
            and all(
                source["kind"] == "project-authored-parametric-vector"
                for source in checked["sources"]
            )
            else f"source-level-{checked['source_level']}"
        ),
        rights_status=str(checked["rights_status"]),
        sources=sources,
        context=context,
        layers=layers,
        pen_order=TECHNICAL_PENS,
        # Source qualification is a release decision made by the v2 evidence
        # contract, not something a free-form catalog may self-assert. Keep
        # this generic artifact label truthful for fixtures, owner-supplied
        # work and non-released studies alike.
        artifact_kind="engineered-object-plate",
        rendering_preset=f"technical-object-{checked['preset']}-v1",
        format_subject_policy=FORMAT_SUBJECT_POLICY,
        source_provider=source_provider,
        source_license=source_license,
        data_snapshot=snapshot_values[-1]
        if snapshot_values
        else "supplied-source-undated",
        notes=notes,
        catalog_record=checked,
        rendering_metadata=rendering,
        rights_metadata={
            "logos_or_trade_dress_used": bool(protected_marks),
            "owner_supplied_identifiers_used": bool(
                checked["identity"].get("identifiers")
            ),
            "excluded_mark_count": sum(
                item["kind"] in {"logo", "badge", "livery", "sponsor-graphic"}
                for item in checked.get("excluded_features", [])
            ),
        },
    )
    if technical_release_authorized:
        from .niche_common import _technical_artwork_release_sha256

        artwork_sha256 = _technical_artwork_release_sha256(artwork)
        artwork.rendering_metadata["technical_release_artwork_sha256"] = (
            artwork_sha256
        )
        if known_collection is not None:
            artwork.rendering_metadata["named_source_release_artwork_sha256"] = (
                artwork_sha256
            )
    return artwork


__all__ = [
    "CATALOG_ID",
    "CATALOG_PATH",
    "CATEGORIES",
    "PRESETS",
    "SEMANTIC_CLASSES",
    "TECHNICAL_PENS",
    "build_technical_plate",
    "compose_motion_context",
    "load_technical_catalog",
    "technical_geometry_sha256",
    "validate_technical_record",
]

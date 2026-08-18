"""Strict, evidence-aware standalone architectural pen plates.

The renderer deliberately consumes an already-curated local-metre model.  It
does not acquire photographs, invent facades, or turn an OSM footprint into an
unqualified survey.  Plan geometry, vertical evidence, and every plotted path
retain their evidence tier in both the SVG and the manifest catalog record.
"""

from __future__ import annotations

import copy
from datetime import date
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, NoReturn, Sequence

from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    Polygon,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .models import MapPlotterError
from .niche_common import (
    ArtworkLayer,
    PlateArtwork,
    Rect,
    context_for,
    polyline_length_mm,
    text_strokes_fit,
)


CATALOG_PATH = Path(__file__).with_name("data") / "architecture-plates-v1.json"
CATALOG_ID = "architecture-plates-v1"
FORMAT_ID = "a3-portrait"
FORMAT_SUBJECT_POLICY = "schematic"
ARCHITECTURE_PENS = (
    "grey-0-25",
    "blue-0-25",
    "blue-0-4",
    "red-0-25",
    "black-0-4",
    "black-1",
)

SUBJECT_KINDS = frozenset({"stadium", "landmark", "building", "house"})
SOURCE_KINDS = frozenset(
    {"openstreetmap", "official-plan", "survey", "lidar", "bim", "in-house-metric"}
)
EVIDENCE_TIERS = frozenset(
    {
        "T0-source-plan",
        "T1-tagged-massing",
        "T2-inferred",
        "T3-measured",
        "T3-authored-metric",
    }
)
HEIGHT_BASIS_BY_TIER = {
    "T1-tagged-massing": "tagged",
    "T2-inferred": "inferred",
    "T3-measured": "measured",
    "T3-authored-metric": "authored-dimensional-model",
}
EVIDENCE_STATUSES = frozenset(
    {
        "source-derived-architectural-study",
        "authored-metric-concept",
        "measured-architectural-study",
        "mixed-evidence-architectural-study",
    }
)
DIMENSION_POLICIES = frozenset(
    {
        "approximate-source-derived",
        "withheld-not-surveyed",
        "author-specified-concept-dimensions-only",
        "measured-dimensions",
    }
)
VERTICAL_POLICIES = frozenset(
    {
        "none",
        "explicit-osm-tags-only",
        "inferred-massing",
        "authored-dimensional-model-only",
        "measured-vertical-model",
        "mixed-vertical-evidence",
    }
)
VERTICAL_DISPLAY_POLICIES = frozenset(
    {
        "diagrammatic-footprint-extrusion",
        "height-reference-only",
        "coverage-gated",
        "authored-concept-massing",
        "measured-massing",
    }
)
RIGHTS_STATUSES = frozenset(
    {
        "odbl-attribution-required",
        "project-authored",
        "commercial-clear",
        "review-required",
    }
)
_STABLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")

Point = tuple[float, float]
Stroke = list[Point]


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid architecture plate data: {message}")


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
        _fail(f"{label} must be a stable ASCII identifier.")
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


def _validate_source(source: Any, index: int) -> dict[str, Any]:
    label = f"sources[{index}]"
    value = _object(source, label)
    required = {
        "id",
        "kind",
        "publisher",
        "license",
        "attribution",
        "use",
        "method",
        "geometry_sha256",
    }
    optional = {"url", "snapshot_date", "snapshot_path", "snapshot_sha256"}
    _keys(value, label, required=required, optional=optional)
    _identifier(value["id"], f"{label}.id")
    kind = _text(value["kind"], f"{label}.kind")
    if kind not in SOURCE_KINDS:
        _fail(f"{label}.kind {kind!r} is unsupported.")
    for key in ("publisher", "license", "attribution", "use", "method"):
        _text(value[key], f"{label}.{key}")
    digest = _text(value["geometry_sha256"], f"{label}.geometry_sha256")
    if _SHA256.fullmatch(digest) is None:
        _fail(f"{label}.geometry_sha256 must be a lowercase SHA-256.")

    url = value.get("url")
    if kind != "in-house-metric" and url is None:
        _fail(f"{label}.url is required for non-authored sources.")
    if url is not None and not _text(url, f"{label}.url").startswith("https://"):
        _fail(f"{label}.url must use HTTPS.")

    snapshot_path = value.get("snapshot_path")
    snapshot_sha = value.get("snapshot_sha256")
    if (snapshot_path is None) != (snapshot_sha is None):
        _fail(f"{label}.snapshot_path and snapshot_sha256 must appear together.")
    if kind == "openstreetmap" and snapshot_path is None:
        _fail(f"{label} must bind the pinned OpenStreetMap snapshot and SHA-256.")
    if snapshot_path is not None:
        path_text = _text(snapshot_path, f"{label}.snapshot_path")
        pure = PurePosixPath(path_text)
        if pure.is_absolute() or ".." in pure.parts or path_text != pure.as_posix():
            _fail(f"{label}.snapshot_path must be a safe relative POSIX path.")
        sha_text = _text(snapshot_sha, f"{label}.snapshot_sha256")
        if _SHA256.fullmatch(sha_text) is None:
            _fail(f"{label}.snapshot_sha256 must be a lowercase SHA-256.")
    if "snapshot_date" in value:
        raw_date = _text(value["snapshot_date"], f"{label}.snapshot_date")
        try:
            date.fromisoformat(raw_date)
        except ValueError as exc:
            raise MapPlotterError(
                f"Invalid architecture plate data: {label}.snapshot_date must be ISO-8601."
            ) from exc
    return value


def _validate_ring(ring: Any, label: str) -> dict[str, Any]:
    value = _object(ring, label)
    _keys(
        value,
        label,
        required={"role", "points", "source_ref", "evidence_tier"},
    )
    role = _text(value["role"], f"{label}.role")
    if role not in {"outer", "inner"}:
        _fail(f"{label}.role must be 'outer' or 'inner'.")
    _text(value["source_ref"], f"{label}.source_ref")
    tier = _text(value["evidence_tier"], f"{label}.evidence_tier")
    if tier not in EVIDENCE_TIERS:
        _fail(f"{label}.evidence_tier {tier!r} is unsupported.")
    points_raw = _array(value["points"], f"{label}.points", nonempty=True)
    if len(points_raw) < 4:
        _fail(f"{label}.points needs three vertices plus an explicit closure.")
    points: list[Point] = []
    for point_index, point_raw in enumerate(points_raw):
        point = _array(point_raw, f"{label}.points[{point_index}]")
        if len(point) != 2:
            _fail(f"{label}.points[{point_index}] must be [x, y].")
        points.append(
            (
                _number(point[0], f"{label}.points[{point_index}][0]"),
                _number(point[1], f"{label}.points[{point_index}][1]"),
            )
        )
    if points[0] != points[-1]:
        _fail(f"{label}.points must be explicitly closed.")
    unique = set(points[:-1])
    if len(unique) < 3:
        _fail(f"{label}.points must contain at least three distinct vertices.")
    twice_area = sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, points[1:], strict=False)
    )
    if abs(twice_area) <= 1e-9:
        _fail(f"{label}.points encloses zero area.")
    if (role == "outer" and twice_area < 0) or (role == "inner" and twice_area > 0):
        _fail(f"{label}.points winding disagrees with its {role!r} role.")
    linear_ring = LinearRing(points)
    polygon = Polygon(points)
    if not linear_ring.is_simple or not polygon.is_valid:
        _fail(f"{label}.points self-intersects or is otherwise invalid.")
    return value


def _validate_height(height: Any, label: str) -> dict[str, Any]:
    value = _object(height, label)
    _keys(value, label, required={"value_m", "basis", "evidence_tier"})
    height_m = _number(value["value_m"], f"{label}.value_m")
    if height_m <= 0:
        _fail(f"{label}.value_m must be positive.")
    tier = _text(value["evidence_tier"], f"{label}.evidence_tier")
    if tier not in HEIGHT_BASIS_BY_TIER:
        _fail(f"{label}.evidence_tier cannot support vertical geometry.")
    basis = _text(value["basis"], f"{label}.basis")
    expected = HEIGHT_BASIS_BY_TIER[tier]
    if basis != expected:
        _fail(f"{label}.basis must be {expected!r} for {tier}.")
    return value


def _validate_component(component: Any, label: str) -> dict[str, Any]:
    value = _object(component, label)
    _keys(value, label, required={"id", "name", "rings"}, optional={"height"})
    _identifier(value["id"], f"{label}.id")
    _text(value["name"], f"{label}.name")
    rings = _array(value["rings"], f"{label}.rings", nonempty=True)
    checked = [
        _validate_ring(ring, f"{label}.rings[{index}]")
        for index, ring in enumerate(rings)
    ]
    if not any(ring["role"] == "outer" for ring in checked):
        _fail(f"{label}.rings must include at least one outer ring.")
    outer_polygons = [
        Polygon(ring["points"]) for ring in checked if ring["role"] == "outer"
    ]
    inner_polygons = [
        Polygon(ring["points"]) for ring in checked if ring["role"] == "inner"
    ]
    for outer_index, outer in enumerate(outer_polygons):
        for other in outer_polygons[outer_index + 1 :]:
            if outer.intersection(other).area > 1e-9:
                _fail(f"{label}.rings contains overlapping outer rings.")
    for inner in inner_polygons:
        containers = sum(inner.within(outer) for outer in outer_polygons)
        if containers != 1:
            _fail(f"{label}.rings inner ring must lie within exactly one outer ring.")
    for inner_index, inner in enumerate(inner_polygons):
        for other in inner_polygons[inner_index + 1 :]:
            if inner.intersection(other).area > 1e-9:
                _fail(f"{label}.rings contains overlapping inner rings.")
    if "height" in value:
        _validate_height(value["height"], f"{label}.height")
    return value


def _geometry_payload(model: dict[str, Any]) -> dict[str, Any]:
    """Return the exact whole-model digest payload used by the catalog builder."""

    components = [model["primary"], *model.get("components", [])]
    return {
        "coordinate_system": model["coordinate_system"],
        "components": [
            {
                "id": component["id"],
                "rings": [
                    {
                        "role": ring["role"],
                        "source_ref": ring["source_ref"],
                        "evidence_tier": ring["evidence_tier"],
                        "points": ring["points"],
                    }
                    for ring in component["rings"]
                ],
                **({"height": component["height"]} if "height" in component else {}),
            }
            for component in components
        ],
    }


def _geometry_sha256(model: dict[str, Any]) -> str:
    payload = json.dumps(
        _geometry_payload(model),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_architecture_record(record: Any) -> dict[str, Any]:
    """Validate and return an isolated copy of one strict architecture record."""

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
            "rights_status",
            "notes",
            "model",
        },
    )
    _identifier(value["id"], "record.id")
    _text(value["title"], "record.title")
    _text(value["subtitle"], "record.subtitle")
    category = _text(value["subject_kind"], "record.subject_kind")
    if category not in SUBJECT_KINDS:
        _fail(f"record.subject_kind {category!r} is unsupported.")
    if value["format_id"] != FORMAT_ID:
        _fail(f"record.format_id must be {FORMAT_ID!r}; other sheets are not released.")

    location = _object(value["location"], "record.location")
    _keys(location, "record.location", required={"label", "country_code"})
    _text(location["label"], "record.location.label")
    country = _text(location["country_code"], "record.location.country_code")
    if re.fullmatch(r"[A-Z]{2}", country) is None:
        _fail("record.location.country_code must be an uppercase ISO alpha-2 code.")

    sources = _array(value["sources"], "record.sources", nonempty=True)
    checked_sources = [
        _validate_source(source, index) for index, source in enumerate(sources)
    ]
    source_ids = [source["id"] for source in checked_sources]
    if len(source_ids) != len(set(source_ids)):
        _fail("record.sources repeats a source id.")

    evidence = _object(value["evidence"], "record.evidence")
    _keys(
        evidence,
        "record.evidence",
        required={
            "status",
            "dimension_policy",
            "vertical_policy",
            "vertical_display_policy",
            "statement",
        },
    )
    status = _text(evidence["status"], "record.evidence.status")
    dimensions = _text(evidence["dimension_policy"], "record.evidence.dimension_policy")
    vertical = _text(evidence["vertical_policy"], "record.evidence.vertical_policy")
    vertical_display = _text(
        evidence["vertical_display_policy"],
        "record.evidence.vertical_display_policy",
    )
    _text(evidence["statement"], "record.evidence.statement")
    if status not in EVIDENCE_STATUSES:
        _fail(f"record.evidence.status {status!r} is unsupported.")
    if dimensions not in DIMENSION_POLICIES:
        _fail(f"record.evidence.dimension_policy {dimensions!r} is unsupported.")
    if vertical not in VERTICAL_POLICIES:
        _fail(f"record.evidence.vertical_policy {vertical!r} is unsupported.")
    if vertical_display not in VERTICAL_DISPLAY_POLICIES:
        _fail(
            "record.evidence.vertical_display_policy "
            f"{vertical_display!r} is unsupported."
        )

    rights = _text(value["rights_status"], "record.rights_status")
    if rights not in RIGHTS_STATUSES:
        _fail(f"record.rights_status {rights!r} is unsupported.")
    notes = _array(value["notes"], "record.notes")
    for index, note in enumerate(notes):
        _text(note, f"record.notes[{index}]")

    model = _object(value["model"], "record.model")
    _keys(
        model,
        "record.model",
        required={"coordinate_system", "primary"},
        optional={"components"},
    )
    if model["coordinate_system"] != "local-metre":
        _fail("record.model.coordinate_system must be 'local-metre'.")
    primary = _validate_component(model["primary"], "record.model.primary")
    components_raw = model.get("components", [])
    components = _array(components_raw, "record.model.components")
    checked_components = [
        _validate_component(component, f"record.model.components[{index}]")
        for index, component in enumerate(components)
    ]
    component_ids = [primary["id"], *(item["id"] for item in checked_components)]
    if len(component_ids) != len(set(component_ids)):
        _fail("record.model repeats a component id.")

    all_components = [primary, *checked_components]
    expected_geometry_sha = _geometry_sha256(model)
    for source in checked_sources:
        if source["geometry_sha256"] != expected_geometry_sha:
            _fail(
                f"record source {source['id']!r}.geometry_sha256 disagrees with "
                "the complete local-metre model."
            )
    ring_tiers = {
        str(ring["evidence_tier"])
        for component in all_components
        for ring in component["rings"]
    }
    height_tiers = {
        component["height"]["evidence_tier"]
        for component in all_components
        if "height" in component
    }
    has_heights = bool(height_tiers)
    if vertical == "none" and has_heights:
        _fail("record.evidence.vertical_policy is 'none' but heights are present.")
    if vertical != "none" and not has_heights:
        _fail("record.evidence.vertical_policy claims massing but no height exists.")
    expected_vertical_tiers = {
        "explicit-osm-tags-only": {"T1-tagged-massing"},
        "inferred-massing": {"T2-inferred"},
        "authored-dimensional-model-only": {"T3-authored-metric"},
        "measured-vertical-model": {"T3-measured"},
    }
    if (
        vertical in expected_vertical_tiers
        and height_tiers != expected_vertical_tiers[vertical]
    ):
        _fail(
            f"record.evidence.vertical_policy {vertical!r} requires only "
            f"{sorted(expected_vertical_tiers[vertical])} heights."
        )
    if vertical == "mixed-vertical-evidence" and len(height_tiers) < 2:
        _fail("mixed vertical evidence requires at least two height evidence tiers.")
    if dimensions == "approximate-source-derived" and not ring_tiers <= {
        "T0-source-plan",
        "T1-tagged-massing",
    }:
        _fail("approximate source-derived dimensions require only T0/T1 plan rings.")
    if dimensions == "author-specified-concept-dimensions-only":
        if ring_tiers != {"T3-authored-metric"}:
            _fail("authored concept dimensions require only T3-authored-metric rings.")
        if not any(source["kind"] == "in-house-metric" for source in checked_sources):
            _fail("authored concept dimensions require an in-house-metric source.")
    if dimensions == "measured-dimensions" and ring_tiers != {"T3-measured"}:
        _fail("measured dimensions require only T3-measured plan rings.")
    if status == "authored-metric-concept" and ring_tiers != {"T3-authored-metric"}:
        _fail("authored-metric-concept status requires T3-authored-metric plan rings.")
    if (
        status == "authored-metric-concept"
        and vertical_display != "authored-concept-massing"
    ):
        _fail("authored metric concepts require authored-concept-massing display.")
    if status == "measured-architectural-study" and ring_tiers != {"T3-measured"}:
        _fail("measured study status requires T3-measured plan rings.")
    if (
        status == "measured-architectural-study"
        and vertical_display != "measured-massing"
    ):
        _fail("measured studies require measured-massing display.")
    if status == "source-derived-architectural-study" and not ring_tiers <= {
        "T0-source-plan",
        "T1-tagged-massing",
        "T2-inferred",
    }:
        _fail("source-derived study status cannot contain T3 plan rings.")
    if status == "source-derived-architectural-study" and vertical_display not in {
        "diagrammatic-footprint-extrusion",
        "height-reference-only",
        "coverage-gated",
    }:
        _fail("source-derived studies require a qualified source display policy.")
    if (
        status == "mixed-evidence-architectural-study"
        and len(ring_tiers | height_tiers) < 2
    ):
        _fail("mixed evidence status requires at least two evidence tiers.")
    return copy.deepcopy(value)


def load_architecture_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the packaged catalog (or a supplied catalog) and fail closed."""

    source_path = path or CATALOG_PATH
    try:
        raw = json.loads(source_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(
            f"Could not read architecture catalog {source_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Architecture catalog {source_path} is not valid JSON: {exc}"
        ) from exc
    root = _object(raw, "catalog")
    _keys(root, "catalog", required={"schema_version", "catalog_id", "subjects"})
    if root["schema_version"] != 1:
        _fail("catalog.schema_version must be 1.")
    if root["catalog_id"] != CATALOG_ID:
        _fail(f"catalog.catalog_id must be {CATALOG_ID!r}.")
    subjects = _array(root["subjects"], "catalog.subjects", nonempty=True)
    checked = [validate_architecture_record(record) for record in subjects]
    ids = [record["id"] for record in checked]
    if len(ids) != len(set(ids)):
        _fail("catalog.subjects repeats a record id.")
    return checked


def _components(record: dict[str, Any]) -> list[dict[str, Any]]:
    model = record["model"]
    return [model["primary"], *model.get("components", [])]


def _all_model_points(components: Sequence[dict[str, Any]]) -> list[Point]:
    return [
        (float(point[0]), float(point[1]))
        for component in components
        for ring in component["rings"]
        for point in ring["points"]
    ]


STANDARD_PLAN_DENOMINATORS = (
    50.0,
    100.0,
    200.0,
    500.0,
    1_000.0,
    1_250.0,
    2_000.0,
    2_500.0,
    5_000.0,
    10_000.0,
    20_000.0,
    50_000.0,
)
VERTICAL_RIB_MIN_SPACING_MM = 0.8
PARTIAL_VERTICAL_COVERAGE_THRESHOLD = 0.3
FULL_VERTICAL_COVERAGE_THRESHOLD = 0.8


def _fit_points(
    points: Sequence[Point],
    rect: Rect,
    *,
    invert_y: bool = True,
    fixed_scale: float | None = None,
):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max_x - min_x
    span_y = max_y - min_y
    if span_x <= 0 or span_y <= 0:
        _fail("model geometry must span both local-metre axes.")
    fit_scale = min(rect.width / span_x, rect.height / span_y)
    scale = fit_scale if fixed_scale is None else fixed_scale
    if not math.isfinite(scale) or scale <= 0 or scale > fit_scale + 1e-9:
        _fail("selected plan scale does not fit its drawing panel.")
    used_width = span_x * scale
    used_height = span_y * scale
    offset_x = rect.x + (rect.width - used_width) / 2
    offset_y = rect.y + (rect.height - used_height) / 2

    def transform(point: Point) -> Point:
        x, y = point
        relative_y = max_y - y if invert_y else y - min_y
        return (
            offset_x + (x - min_x) * scale,
            offset_y + relative_y * scale,
        )

    return transform, scale


def _standard_plan_denominator(points: Sequence[Point], rect: Rect) -> float:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    required = max(1_000.0 * span_x / rect.width, 1_000.0 * span_y / rect.height)
    for denominator in STANDARD_PLAN_DENOMINATORS:
        if denominator + 1e-9 >= required:
            return denominator
    _fail(f"model needs a plan scale beyond 1:{STANDARD_PLAN_DENOMINATORS[-1]:g}.")


def _component_plan_area(component: dict[str, Any]) -> float:
    return sum(
        (1.0 if ring["role"] == "outer" else -1.0) * abs(Polygon(ring["points"]).area)
        for ring in component["rings"]
    )


def _vertical_coverage_fraction(
    primary: dict[str, Any], vertical_components: Sequence[dict[str, Any]]
) -> float:
    primary_area = _component_plan_area(primary)
    if primary_area <= 1e-9:
        _fail("primary component has no usable plan area.")
    covered = sum(_component_plan_area(component) for component in vertical_components)
    return min(max(covered / primary_area, 0.0), 1.0)


def _vertical_view_status(
    policy: str,
    *,
    has_vertical: bool,
    coverage: float,
) -> str:
    if not has_vertical:
        return "not-applicable"
    if policy == "height-reference-only":
        return "omitted-roof-form-unmodeled"
    if policy == "coverage-gated" and coverage < PARTIAL_VERTICAL_COVERAGE_THRESHOLD:
        return "omitted-partial-height-evidence"
    if policy == "authored-concept-massing":
        return "authored-concept-massing-emitted"
    if policy == "measured-massing":
        return "measured-massing-emitted"
    if coverage < FULL_VERTICAL_COVERAGE_THRESHOLD:
        return "partial-diagrammatic-extrusion-emitted"
    return "diagrammatic-extrusion-emitted"


def _evidence_attributes(
    *,
    tier: str,
    view: str,
    component_id: str,
    geometry_role: str,
    claim: str,
) -> dict[str, str]:
    return {
        "data-evidence-tier": tier,
        "data-view": view,
        "data-component-id": component_id,
        "data-model-role": geometry_role,
        "data-claim-status": claim,
    }


def _tier_pen(tier: str, *, massing: bool) -> str:
    if tier == "T2-inferred":
        return "grey-0-25"
    if tier in {"T0-source-plan", "T1-tagged-massing"}:
        return "blue-0-4" if massing else "blue-0-25"
    return "black-0-4"


def _tier_claim(tier: str) -> str:
    return {
        "T0-source-plan": "source-derived-approximate",
        "T1-tagged-massing": "source-tagged-unverified",
        "T2-inferred": "inferred-not-measured",
        "T3-measured": "measured-source",
        "T3-authored-metric": "authored-concept-not-as-built",
    }[tier]


def _dashed_strokes(points: Sequence[Point], *, nib_mm: float = 0.25) -> list[Stroke]:
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
            remaining = length - cursor
            plotted = min(dash, remaining)
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


def _add_geometry(
    layers: dict[str, ArtworkLayer],
    *,
    pen_id: str,
    points: Sequence[Point],
    source_ref: str,
    role: str,
    attributes: dict[str, str],
    dashed: bool,
) -> None:
    layer = layers[pen_id]
    strokes = _dashed_strokes(points) if dashed else [list(points)]
    for stroke in strokes:
        if polyline_length_mm(stroke) + 1e-9 < 3.0 * layer.pen.mark_width_mm:
            continue
        layer.add(
            stroke,
            source_ref=source_ref,
            role=role,
            attributes=attributes,
        )


def _linear_parts(geometry: BaseGeometry) -> list[LineString]:
    """Return only drawable line parts from a Shapely overlay result."""

    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        result: list[LineString] = []
        for child in geometry.geoms:
            result.extend(_linear_parts(child))
        return result
    return []


def _deduplicate_layer(layer: ArtworkLayer) -> ArtworkLayer:
    """Remove coincident same-pen ink while retaining the first claim owner.

    Adjacent building components commonly share a wall, and an overall OSM
    envelope can repeat parts of component outlines. Plotting both is a wet-ink
    repeat pass even when the SVG paths have different source identities. The
    first emitted claim retains the shared segment; later records retain only
    their unique remainder. The complete multi-source model remains in the
    manifest catalog record.
    """

    result = ArtworkLayer(layer.id, layer.label, layer.pen_id)
    seen: BaseGeometry = GeometryCollection()
    minimum = 3.0 * layer.pen.mark_width_mm
    for record in layer.records:
        line = LineString(record.points)
        if record.source_ref:
            for retained in result.records:
                if retained.source_ref == record.source_ref:
                    continue
                retained_line = LineString(retained.points)
                if retained_line.intersection(line).length <= 1e-9:
                    continue
                coincident = {
                    value
                    for value in retained.attributes.get(
                        "data-coincident-source-refs", ""
                    ).split("|")
                    if value
                }
                coincident.add(record.source_ref)
                retained.attributes["data-coincident-source-refs"] = "|".join(
                    sorted(coincident)
                )
        remainder = line if seen.is_empty else line.difference(seen)
        for part in _linear_parts(remainder):
            points = [(float(x), float(y)) for x, y in part.coords]
            if polyline_length_mm(points) + 1e-9 < minimum:
                continue
            result.add(
                points,
                source_ref=record.source_ref,
                role=record.role,
                sequence=record.sequence,
                attributes=record.attributes,
            )
        seen = unary_union((seen, line))
    return result


def _add_evidence_text(
    layer: ArtworkLayer,
    text: str,
    *,
    x_mm: float,
    y_mm: float,
    cap_mm: float,
    maximum_width_mm: float,
    role: str,
    attributes: dict[str, str],
    anchor: str = "start",
) -> None:
    for stroke in text_strokes_fit(
        text,
        x_mm=x_mm,
        y_mm=y_mm,
        preferred_cap_mm=cap_mm,
        maximum_width_mm=maximum_width_mm,
        pen_id=layer.pen_id,
        anchor=anchor,
    ):
        layer.add(stroke, role=role, attributes=attributes)


def _dimension_copy(policy: str, width_m: float, depth_m: float) -> str | None:
    if policy == "withheld-not-surveyed":
        return None
    if policy == "approximate-source-derived":
        return f"APPROX E-W {width_m:.1f} M / N-S {depth_m:.1f} M"
    if policy == "author-specified-concept-dimensions-only":
        return f"DESIGN X {width_m:.2f} M / Y {depth_m:.2f} M / NOT AS-BUILT"
    return f"MEASURED X {width_m:.2f} M / Y {depth_m:.2f} M"


def _height_copy(component: dict[str, Any]) -> str:
    height = component["height"]
    name = str(component["name"]).upper()
    value = float(height["value_m"])
    basis = height["basis"]
    if basis == "tagged":
        qualifier = "TAGGED / UNVERIFIED"
    elif basis == "inferred":
        qualifier = "INFERRED / NOT MEASURED"
    elif basis == "authored-dimensional-model":
        qualifier = "DESIGN / NOT AS-BUILT"
    else:
        qualifier = "MEASURED"
    return f"{name}: {value:.2f} M / {qualifier}"


def _add_dimensions(
    layers: dict[str, ArtworkLayer],
    *,
    bounds: tuple[float, float, float, float],
    panel: Rect,
    copy: str,
    policy: str,
    tier: str,
    cap_mm: float,
) -> None:
    min_x, min_y, max_x, max_y = bounds
    red = layers["red-0-25"]
    black = layers["black-0-4"]
    gap = 3.0
    base_y = min(panel.bottom - cap_mm - 1.0, max_y + gap)
    side_x = min(panel.right - 1.0, max_x + gap)
    tick = 4.0 * red.pen.mark_width_mm
    claim = {
        "approximate-source-derived": "approximate",
        "author-specified-concept-dimensions-only": ("authored-concept-not-as-built"),
        "measured-dimensions": "measured-source",
    }.get(policy, "withheld")
    attributes = _evidence_attributes(
        tier=tier,
        view="plan",
        component_id="model-envelope",
        geometry_role="dimension",
        claim=claim,
    )
    for stroke in (
        [(min_x, max_y), (min_x, base_y)],
        [(max_x, max_y), (max_x, base_y)],
        [(min_x, base_y), (max_x, base_y)],
        [(max_x, min_y), (side_x, min_y)],
        [(max_x, max_y), (side_x, max_y)],
        [(side_x, min_y), (side_x, max_y)],
        [(min_x - tick / 2, base_y + tick / 2), (min_x + tick / 2, base_y - tick / 2)],
        [(max_x - tick / 2, base_y + tick / 2), (max_x + tick / 2, base_y - tick / 2)],
        [(side_x - tick / 2, min_y + tick / 2), (side_x + tick / 2, min_y - tick / 2)],
        [(side_x - tick / 2, max_y + tick / 2), (side_x + tick / 2, max_y - tick / 2)],
    ):
        if polyline_length_mm(stroke) + 1e-9 >= 3.0 * red.pen.mark_width_mm:
            red.add(stroke, role="dimension", attributes=attributes)
    _add_evidence_text(
        black,
        copy,
        x_mm=(min_x + max_x) / 2,
        y_mm=base_y + 1.0,
        cap_mm=cap_mm,
        maximum_width_mm=max(panel.width - 2.0, 1.0),
        anchor="middle",
        role="dimension-label",
        attributes=attributes,
    )


def _vertical_rib_is_distinct(
    base: Point,
    top: Point,
    accepted: Sequence[tuple[Point, Point]],
    *,
    minimum_spacing_mm: float,
) -> bool:
    candidate_low, candidate_high = sorted((base[1], top[1]))
    for other_base, other_top in accepted:
        if abs(base[0] - other_base[0]) + 1e-9 >= minimum_spacing_mm:
            continue
        other_low, other_high = sorted((other_base[1], other_top[1]))
        if min(candidate_high, other_high) - max(candidate_low, other_low) > 1e-9:
            return False
    return True


def _visible_base_segments(ring: dict[str, Any], base: Sequence[Point]) -> list[Stroke]:
    """Select front-facing base edges for the fixed southwest axon camera."""

    if ring["role"] != "outer":
        return []
    model_points = [(float(point[0]), float(point[1])) for point in ring["points"]]
    result: list[Stroke] = []
    for model_first, model_second, paper_first, paper_second in zip(
        model_points,
        model_points[1:],
        base,
        base[1:],
        strict=False,
    ):
        dx = model_second[0] - model_first[0]
        dy = model_second[1] - model_first[1]
        # A canonical outer is counter-clockwise, so (dy, -dx) is its outward
        # horizontal normal. The fixed camera is southwest of the model.
        faces_camera = dx - dy > 1e-9
        if faces_camera:
            result.append([paper_first, paper_second])
    return result


def _layout_record(
    record: dict[str, Any],
) -> tuple[list[ArtworkLayer], dict[str, Any]]:
    context = context_for(FORMAT_ID)
    plate = context.plate
    gap = float(plate["gap_mm"])
    cap = float(plate["type_scale_mm"]["attribution"])
    inner = context.field.inset(gap)
    components = _components(record)
    vertical_components = [
        component for component in components if "height" in component
    ]
    has_vertical = bool(vertical_components)
    vertical_coverage = (
        _vertical_coverage_fraction(components[0], vertical_components)
        if has_vertical
        else 0.0
    )
    display_policy = str(record["evidence"]["vertical_display_policy"])
    vertical_status = _vertical_view_status(
        display_policy,
        has_vertical=has_vertical,
        coverage=vertical_coverage,
    )
    emits_massing = vertical_status.endswith("-emitted")
    height_lines = tuple(_height_copy(component) for component in vertical_components)

    if emits_massing:
        available_height = inner.height - gap
        axon_height = available_height * 0.46
        plan_height = available_height - axon_height
        axon_panel = Rect(inner.x, inner.y, inner.width, axon_height)
        plan_panel = Rect(
            inner.x,
            inner.y + axon_height + gap,
            inner.width,
            plan_height,
        )
    else:
        axon_panel = None
        plan_panel = inner

    layers = {
        "grey-0-25": ArtworkLayer(
            "inferred_geometry", "Inferred geometry", "grey-0-25"
        ),
        "blue-0-25": ArtworkLayer("source_plan", "Source-derived plan", "blue-0-25"),
        "blue-0-4": ArtworkLayer("tagged_massing", "Tagged massing", "blue-0-4"),
        "red-0-25": ArtworkLayer("dimensions", "Dimensions and controls", "red-0-25"),
        "black-0-4": ArtworkLayer(
            "architecture_copy", "Measured geometry and copy", "black-0-4"
        ),
    }
    copy_layer = layers["black-0-4"]
    dimension_policy = record["evidence"]["dimension_policy"]
    model_points = _all_model_points(components)
    world_x = [point[0] for point in model_points]
    world_y = [point[1] for point in model_points]
    width_m = max(world_x) - min(world_x)
    depth_m = max(world_y) - min(world_y)
    dimension_copy = _dimension_copy(dimension_policy, width_m, depth_m)

    omitted_copy: str | None = None
    if vertical_status == "omitted-roof-form-unmodeled":
        omitted_copy = (
            "HEIGHT REFERENCES ONLY / ROOF FORM NOT MODELLED / MASSING OMITTED"
        )
    elif vertical_status == "omitted-partial-height-evidence":
        omitted_copy = (
            f"PARTIAL HEIGHT EVIDENCE {vertical_coverage * 100:.1f} PCT / "
            "MASSING OMITTED"
        )
    omitted_lines = [omitted_copy, *height_lines] if omitted_copy else []
    label_band = cap + gap / 2.0 + len(omitted_lines) * (cap + 0.9)
    dimension_band = cap + gap if dimension_copy is not None else 0.0
    right_band = 2.0 * gap if dimension_copy is not None else 0.0
    plan_rect = Rect(
        plan_panel.x,
        plan_panel.y + label_band,
        plan_panel.width - right_band,
        plan_panel.height - label_band - dimension_band,
    )
    if min(plan_rect.width, plan_rect.height) <= 0:
        _fail("plan and dimension panels cannot fit the binding map_field.")
    standard_scale = dimension_policy in {
        "author-specified-concept-dimensions-only",
        "measured-dimensions",
    }
    if standard_scale:
        denominator = _standard_plan_denominator(model_points, plan_rect)
        plan_transform, plan_scale = _fit_points(
            model_points,
            plan_rect,
            fixed_scale=1_000.0 / denominator,
        )
        scale_display = "standard-numeric"
    else:
        plan_transform, plan_scale = _fit_points(model_points, plan_rect)
        denominator = 1_000.0 / plan_scale
        scale_display = "fit-to-field-approximate"

    plan_label = "PLAN / LOCAL-METRE SOURCE GEOMETRY"
    if dimension_policy == "approximate-source-derived":
        plan_label = "PLAN / SOURCE GEOMETRY / FIT TO FIELD / APPROX / NOT A SURVEY"
    elif dimension_policy == "withheld-not-surveyed":
        plan_label += " / DIMENSIONS WITHHELD"
    elif dimension_policy == "author-specified-concept-dimensions-only":
        plan_label += " / CONCEPT / NOT AS-BUILT"
    label_attributes = {
        "data-evidence-tier": "mixed",
        "data-view": "plan",
        "data-model-role": "panel-label",
        "data-claim-status": record["evidence"]["statement"],
    }
    _add_evidence_text(
        copy_layer,
        plan_label,
        x_mm=plan_panel.x,
        y_mm=plan_panel.y,
        cap_mm=cap,
        maximum_width_mm=plan_panel.width,
        role="field-panel-label",
        attributes=label_attributes,
    )
    for index, line in enumerate(omitted_lines, start=1):
        component = (
            vertical_components[index - 2]
            if index >= 2 and index - 2 < len(vertical_components)
            else None
        )
        tier = (
            str(component["height"]["evidence_tier"])
            if component is not None
            else "mixed"
        )
        _add_evidence_text(
            copy_layer,
            line,
            x_mm=plan_panel.x,
            y_mm=plan_panel.y + index * (cap + 0.9),
            cap_mm=cap,
            maximum_width_mm=plan_panel.width,
            role=("height-reference-label" if component else "field-panel-label"),
            attributes={
                "data-evidence-tier": tier,
                "data-view": "height-reference",
                "data-component-id": (
                    str(component["id"]) if component else "model-envelope"
                ),
                "data-model-role": ("height-label" if component else "panel-label"),
                "data-claim-status": (
                    _tier_claim(tier) if component else vertical_status
                ),
            },
        )

    transformed_plan_points: list[Point] = []
    envelope_tier = str(components[0]["rings"][0]["evidence_tier"])
    for component in components:
        for ring in component["rings"]:
            tier = str(ring["evidence_tier"])
            transformed = [
                plan_transform((float(point[0]), float(point[1])))
                for point in ring["points"]
            ]
            transformed_plan_points.extend(transformed)
            attributes = _evidence_attributes(
                tier=tier,
                view="plan",
                component_id=str(component["id"]),
                geometry_role=str(ring["role"]),
                claim=_tier_claim(tier),
            )
            _add_geometry(
                layers,
                pen_id=_tier_pen(tier, massing=False),
                points=transformed,
                source_ref=str(ring["source_ref"]),
                role=f"plan-{ring['role']}",
                attributes=attributes,
                dashed=tier == "T2-inferred",
            )

    if dimension_copy is not None:
        tx = [point[0] for point in transformed_plan_points]
        ty = [point[1] for point in transformed_plan_points]
        _add_dimensions(
            layers,
            bounds=(min(tx), min(ty), max(tx), max(ty)),
            panel=plan_panel,
            copy=dimension_copy,
            policy=dimension_policy,
            tier=envelope_tier,
            cap_mm=cap,
        )

    if axon_panel is not None:
        info_lines = 1 + len(height_lines)
        info_height = info_lines * cap + max(info_lines - 1, 0) * 0.9 + gap / 2.0
        axon_rect = Rect(
            axon_panel.x,
            axon_panel.y + info_height,
            axon_panel.width,
            axon_panel.height - info_height,
        )
        if min(axon_rect.width, axon_rect.height) <= 0:
            _fail("vertical evidence labels cannot fit the axonometric panel.")
        if vertical_status in {
            "diagrammatic-extrusion-emitted",
            "partial-diagrammatic-extrusion-emitted",
        }:
            prefix = (
                f"PARTIAL DIAGRAMMATIC EXTRUSION {vertical_coverage * 100:.1f} PCT"
                if vertical_status == "partial-diagrammatic-extrusion-emitted"
                else "DIAGRAMMATIC EXTRUSION"
            )
            view_copy = f"{prefix} / NTS / ROOF AND FACADE FORM NOT SHOWN"
            projection_status = "footprint-extrusion-not-roof-or-facade-form"
        elif vertical_status == "authored-concept-massing-emitted":
            view_copy = "AXONOMETRIC CONCEPT MASSING / NTS / NOT AS-BUILT"
            projection_status = "authored-concept-massing-not-as-built"
        else:
            view_copy = "AXONOMETRIC MASSING / NTS / MEASURED SOURCE"
            projection_status = "measured-source-massing"
        _add_evidence_text(
            copy_layer,
            view_copy,
            x_mm=axon_panel.x,
            y_mm=axon_panel.y,
            cap_mm=cap,
            maximum_width_mm=axon_panel.width,
            role="field-panel-label",
            attributes={
                "data-evidence-tier": "mixed",
                "data-view": "axonometric-massing",
                "data-model-role": "panel-label",
                "data-claim-status": projection_status,
            },
        )
        for index, (component, line) in enumerate(
            zip(vertical_components, height_lines, strict=True)
        ):
            height = component["height"]
            tier = str(height["evidence_tier"])
            _add_evidence_text(
                copy_layer,
                line,
                x_mm=axon_panel.x,
                y_mm=axon_panel.y + (index + 1) * (cap + 0.9),
                cap_mm=cap,
                maximum_width_mm=axon_panel.width,
                role="massing-height-label",
                attributes=_evidence_attributes(
                    tier=tier,
                    view="axonometric-massing",
                    component_id=str(component["id"]),
                    geometry_role="height-label",
                    claim=_tier_claim(tier),
                ),
            )

        projected: dict[str, list[tuple[dict[str, Any], list[Point], list[Point]]]] = {}
        flat_context: list[tuple[dict[str, Any], dict[str, Any], list[Point]]] = []
        all_projected: list[Point] = []
        cosine = math.cos(math.radians(30.0))
        sine = math.sin(math.radians(30.0))
        if vertical_status == "partial-diagrammatic-extrusion-emitted":
            for component in components:
                if "height" in component:
                    continue
                for ring in component["rings"]:
                    base = [
                        (
                            (float(raw[0]) - float(raw[1])) * cosine,
                            (float(raw[0]) + float(raw[1])) * sine,
                        )
                        for raw in ring["points"]
                    ]
                    flat_context.append((component, ring, base))
                    all_projected.extend(base)
        for component in vertical_components:
            height_m = float(component["height"]["value_m"])
            component_projected: list[
                tuple[dict[str, Any], list[Point], list[Point]]
            ] = []
            for ring in component["rings"]:
                base = []
                top = []
                for raw in ring["points"]:
                    x, y = float(raw[0]), float(raw[1])
                    base.append(((x - y) * cosine, (x + y) * sine))
                    top.append(((x - y) * cosine, (x + y) * sine + height_m))
                component_projected.append((ring, base, top))
                all_projected.extend(base)
                all_projected.extend(top)
            projected[str(component["id"])] = component_projected
        axon_transform, _ = _fit_points(all_projected, axon_rect)
        for component, ring, base_raw in flat_context:
            tier = str(ring["evidence_tier"])
            _add_geometry(
                layers,
                pen_id=_tier_pen(tier, massing=False),
                points=[axon_transform(point) for point in base_raw],
                source_ref=str(ring["source_ref"]),
                role="massing-unheighted-context",
                attributes={
                    **_evidence_attributes(
                        tier=tier,
                        view="axonometric-massing",
                        component_id=str(component["id"]),
                        geometry_role="unheighted-plan-context",
                        claim="height-unavailable-flat-context",
                    ),
                    "data-height-status": "unavailable",
                    "data-projection-status": "flat-plan-context-only",
                },
                dashed=tier == "T2-inferred",
            )
        accepted_verticals: dict[str, list[tuple[Point, Point]]] = {}
        for component in vertical_components:
            height = component["height"]
            tier = str(height["evidence_tier"])
            pen_id = _tier_pen(tier, massing=True)
            for ring, base_raw, top_raw in projected[str(component["id"])]:
                base = [axon_transform(point) for point in base_raw]
                top = [axon_transform(point) for point in top_raw]
                attributes = {
                    **_evidence_attributes(
                        tier=tier,
                        view="axonometric-massing",
                        component_id=str(component["id"]),
                        geometry_role="massing-wireframe",
                        claim=_tier_claim(tier),
                    ),
                    "data-height-basis": str(height["basis"]),
                    "data-height-m": f"{float(height['value_m']):g}",
                    "data-projection-status": projection_status,
                }
                source_ref = str(ring["source_ref"])
                dashed = tier == "T2-inferred"
                if tier != "T1-tagged-massing":
                    _add_geometry(
                        layers,
                        pen_id=pen_id,
                        points=base,
                        source_ref=source_ref,
                        role="massing-base",
                        attributes=attributes,
                        dashed=dashed,
                    )
                else:
                    for visible_segment in _visible_base_segments(ring, base):
                        _add_geometry(
                            layers,
                            pen_id=pen_id,
                            points=visible_segment,
                            source_ref=source_ref,
                            role="massing-visible-base",
                            attributes={
                                **attributes,
                                "data-visibility-status": (
                                    "front-facing-edge-fixed-axon-camera"
                                ),
                            },
                            dashed=False,
                        )
                _add_geometry(
                    layers,
                    pen_id=pen_id,
                    points=top,
                    source_ref=source_ref,
                    role="massing-top",
                    attributes=attributes,
                    dashed=dashed,
                )
                pen_verticals = accepted_verticals.setdefault(pen_id, [])
                for base_point, top_point in zip(base[:-1], top[:-1], strict=True):
                    if not _vertical_rib_is_distinct(
                        base_point,
                        top_point,
                        pen_verticals,
                        minimum_spacing_mm=VERTICAL_RIB_MIN_SPACING_MM,
                    ):
                        continue
                    pen_verticals.append((base_point, top_point))
                    _add_geometry(
                        layers,
                        pen_id=pen_id,
                        points=[base_point, top_point],
                        source_ref=source_ref,
                        role="massing-vertical",
                        attributes={
                            **attributes,
                            "data-min-rib-spacing-mm": (
                                f"{VERTICAL_RIB_MIN_SPACING_MM:g}"
                            ),
                        },
                        dashed=dashed,
                    )

    emitted_layers = [
        _deduplicate_layer(layer) for layer in layers.values() if layer.records
    ]
    emitted_layers = [layer for layer in emitted_layers if layer.records]
    return emitted_layers, {
        "vertical_view_status": vertical_status,
        "vertical_display_policy": display_policy,
        "vertical_coverage_fraction": round(vertical_coverage, 6),
        "vertical_rib_min_spacing_mm": VERTICAL_RIB_MIN_SPACING_MM,
        "plan_scale_denominator": round(denominator, 3),
        "plan_scale_display": scale_display,
        "dimension_axes": (
            "east-west-north-south-envelope"
            if dimension_policy == "approximate-source-derived"
            else "local-x-y-envelope"
        ),
    }


def _source_provider(sources: Sequence[dict[str, Any]]) -> str:
    kinds = {source["kind"] for source in sources}
    values: list[str] = []
    if "openstreetmap" in kinds:
        values.append("OpenStreetMap contributors")
    if "in-house-metric" in kinds:
        values.append("in-house authored geometry")
    values.extend(
        source["publisher"]
        for source in sources
        if source["kind"] not in {"openstreetmap", "in-house-metric"}
        and source["publisher"] not in values
    )
    return " / ".join(values)


def build_architecture_plate(
    record: Any,
    format_id: str | None = None,
) -> PlateArtwork:
    """Build one A3 portrait standalone architecture study."""

    checked = validate_architecture_record(record)
    selected = format_id or str(checked["format_id"])
    if selected != FORMAT_ID:
        raise MapPlotterError(
            f"Standalone architecture currently requires {FORMAT_ID}; got {selected!r}."
        )
    layers, rendering_metadata = _layout_record(checked)
    evidence = checked["evidence"]
    dimensions = evidence["dimension_policy"]
    if dimensions == "approximate-source-derived":
        dimension_detail = "APPROX E-W / N-S ENVELOPE / NOT A SURVEY"
    elif dimensions == "withheld-not-surveyed":
        dimension_detail = "DIMENSIONS WITHHELD / NOT A SURVEY"
    elif dimensions == "author-specified-concept-dimensions-only":
        dimension_detail = "CONCEPT DIMENSIONS / NOT AS-BUILT"
    else:
        dimension_detail = "MEASURED DIMENSIONS"
    vertical_status = str(rendering_metadata["vertical_view_status"])
    view_detail = {
        "diagrammatic-extrusion-emitted": "DIAGRAMMATIC EXTRUSION NTS",
        "partial-diagrammatic-extrusion-emitted": (
            "PARTIAL DIAGRAMMATIC EXTRUSION NTS"
        ),
        "authored-concept-massing-emitted": "CONCEPT MASSING NTS",
        "measured-massing-emitted": "MEASURED MASSING NTS",
        "omitted-roof-form-unmodeled": "MASSING OMITTED / ROOF FORM NOT MODELLED",
        "omitted-partial-height-evidence": (
            "MASSING OMITTED / PARTIAL HEIGHT EVIDENCE"
        ),
        "not-applicable": "MASSING NOT AVAILABLE",
    }[vertical_status]
    scale_copy = (
        f"PLAN 1:{float(rendering_metadata['plan_scale_denominator']):.0f}"
        if rendering_metadata["plan_scale_display"] == "standard-numeric"
        else "PLAN FIT TO FIELD"
    )
    details = (
        f"ARCHITECTURAL STUDY / {checked['subject_kind'].upper()} / A3",
        f"{scale_copy} / {view_detail}",
        dimension_detail,
    )
    sources = tuple(copy.deepcopy(checked["sources"]))
    visible_credits = list(
        dict.fromkeys(
            f"{source['attribution']} / {source['license']}" for source in sources
        )
    )
    credit = " / ".join(visible_credits)
    snapshot_dates = sorted(
        str(source["snapshot_date"])
        for source in sources
        if source.get("snapshot_date") is not None
    )
    licenses = " / ".join(dict.fromkeys(str(source["license"]) for source in sources))
    return PlateArtwork(
        subject_id=str(checked["id"]),
        domain="architecture",
        subject_kind=str(checked["subject_kind"]),
        title=str(checked["title"]),
        subtitle=str(checked["subtitle"]),
        details=details,
        credit_line=credit,
        scale_status=(
            "source-derived-approximate"
            if dimensions == "approximate-source-derived"
            else "declared-local-metre-model"
        ),
        evidence_status=str(evidence["statement"]),
        rights_status=str(checked["rights_status"]),
        sources=sources,
        context=context_for(selected),
        layers=layers,
        pen_order=ARCHITECTURE_PENS,
        artifact_kind="standalone-architecture-technical-study",
        rendering_preset="architecture-a3-portrait-v2",
        format_subject_policy=FORMAT_SUBJECT_POLICY,
        source_provider=_source_provider(list(sources)),
        source_license=licenses,
        data_snapshot=snapshot_dates[-1] if snapshot_dates else "authored-model-v1",
        notes=tuple(str(note) for note in checked["notes"])
        + (str(evidence["statement"]),),
        catalog_record=checked,
        rendering_metadata=rendering_metadata,
    )


__all__ = [
    "ARCHITECTURE_PENS",
    "CATALOG_ID",
    "CATALOG_PATH",
    "FORMAT_ID",
    "build_architecture_plate",
    "load_architecture_catalog",
    "validate_architecture_record",
]

"""Quarantined v1 parametric collections for audit and regression tests.

The collection catalogues deliberately separate two kinds of evidence:

* official pages support printed facts and concise history;
* the project's own parameter sets support original illustrative linework.

No manufacturer drawing, photograph, logo or livery was traced. The resulting
views are not the proper outlines of the named objects. Public loaders fail by
default; callers preserving old review manifests must opt in explicitly. New
artwork uses source-qualified records and never falls back to this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Sequence
import unicodedata

from .models import MapPlotterError
from .technical import technical_geometry_sha256, validate_technical_record
from .vector_path import CubicSegment, LineSegment, VectorPath


DATA_DIR = Path(__file__).with_name("data")
COLLECTION_CATALOG_PATHS = {
    "cars": DATA_DIR / "technical-cars-v1.json",
    "aircraft": DATA_DIR / "technical-aircraft-recipes-v1.json",
    "boats": DATA_DIR / "watercraft-technical-plates-v1.json",
}
COLLECTION_IDS = frozenset(COLLECTION_CATALOG_PATHS)
FORMAT_IDS = (
    "a5-portrait",
    "a5-landscape",
    "a4-portrait",
    "a4-landscape",
    "a3-portrait",
    "a3-landscape",
)
TEMPLATE_VERSION = "parametric-orthographic-v1"
_RETIRED_MESSAGE = (
    "parametric-orthographic-v1 is retired illustrative geometry. It cannot "
    "supply the outline of a named real object. Import complete source-qualified "
    "views instead; missing geometry must remain blocked."
)


def _fail(message: str) -> None:
    raise MapPlotterError(f"Invalid technical collection data: {message}")


def _canonical_sha(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _plot_text(value: Any) -> str:
    replacements = {
        "×": "X",
        "–": "-",
        "—": "-",
        "’": "'",
        "“": '"',
        "”": '"',
        "°": " DEG",
    }
    text = "".join(replacements.get(character, character) for character in str(value))
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        _fail(f"{label} must be a finite number.")
    return result


def _path(
    start: tuple[float, float],
    segments: Sequence[
        tuple[str, tuple[float, float]]
        | tuple[
            str,
            tuple[float, float],
            tuple[float, float],
            tuple[float, float],
        ]
    ],
    *,
    closed: bool = False,
) -> dict[str, Any]:
    built: list[LineSegment | CubicSegment] = []
    for segment in segments:
        if segment[0] == "L":
            built.append(LineSegment(segment[1]))
        elif segment[0] == "C":
            built.append(CubicSegment(segment[1], segment[2], segment[3]))
        else:
            _fail(f"unsupported parametric segment {segment[0]!r}.")
    return VectorPath(start=start, segments=tuple(built), closed=closed).to_dict()


def _smooth_closed(points: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """Return a periodic Catmull-Rom-to-cubic contour through authored points."""

    values = list(points)
    if len(values) > 1 and values[0] == values[-1]:
        values.pop()
    if len(values) < 3:
        _fail("a smooth closed contour needs at least three points.")
    segments: list[Any] = []
    count = len(values)
    for index, current in enumerate(values):
        previous = values[(index - 1) % count]
        following = values[(index + 1) % count]
        after = values[(index + 2) % count]
        control_1 = (
            current[0] + (following[0] - previous[0]) / 6.0,
            current[1] + (following[1] - previous[1]) / 6.0,
        )
        control_2 = (
            following[0] - (after[0] - current[0]) / 6.0,
            following[1] - (after[1] - current[1]) / 6.0,
        )
        segments.append(("C", control_1, control_2, following))
    # The final cubic ends at the start point, so the contour is geometrically
    # closed without asking VectorPath to add a second straight closing edge.
    return _path(values[0], segments, closed=False)


def _primitive(
    identifier: str,
    component: str,
    semantic: str,
    geometry: dict[str, Any],
    *,
    priority: str = "normal",
    minimum_sheet: str = "A5",
    feature_kind: str | None = None,
    line_style: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": identifier,
        "component_id": component,
        "semantic_class": semantic,
        "source_refs": ["original-parametric-geometry"],
        "evidence": "project-authored",
        "claim_status": "original-dimension-conditioned-illustrative-geometry",
        **geometry,
        "detail_priority": priority,
        "minimum_sheet": minimum_sheet,
    }
    if feature_kind is not None:
        result["feature_kind"] = feature_kind
    if line_style is not None:
        result["line_style"] = line_style
    return result


def _line(
    identifier: str,
    component: str,
    semantic: str,
    points: Sequence[tuple[float, float]],
    **kwargs: Any,
) -> dict[str, Any]:
    return _primitive(
        identifier,
        component,
        semantic,
        {"points": [[float(x), float(y)] for x, y in points]},
        **kwargs,
    )


def _curve(
    identifier: str,
    component: str,
    semantic: str,
    start: tuple[float, float],
    segments: Sequence[Any],
    *,
    closed: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    return _primitive(
        identifier,
        component,
        semantic,
        {"path": _path(start, segments, closed=closed)},
        **kwargs,
    )


def _ellipse(
    identifier: str,
    component: str,
    semantic: str,
    centre: tuple[float, float],
    radius_x: float,
    radius_y: float,
    *,
    rotation_deg: float = 0.0,
    **kwargs: Any,
) -> dict[str, Any]:
    return _primitive(
        identifier,
        component,
        semantic,
        {
            "ellipse": {
                "centre": [float(centre[0]), float(centre[1])],
                "radius_x": float(radius_x),
                "radius_y": float(radius_y),
                "rotation_deg": float(rotation_deg),
            }
        },
        **kwargs,
    )


def _circle(
    identifier: str,
    component: str,
    semantic: str,
    centre: tuple[float, float],
    radius: float,
    **kwargs: Any,
) -> dict[str, Any]:
    return _primitive(
        identifier,
        component,
        semantic,
        {"circle": {"centre": [float(centre[0]), float(centre[1])], "radius": radius}},
        **kwargs,
    )


def _fact_kind(raw: str) -> str:
    normalized = raw.casefold()
    if "museum" in normalized:
        return "museum-record"
    if any(word in normalized for word in ("government", "authority", "raf", "navy")):
        return "public-authority-record"
    if any(word in normalized for word in ("history", "heritage", "press")):
        return "official-history-page"
    return "official-specification-page"


def _normalize_fact_sources(subject: dict[str, Any]) -> list[dict[str, Any]]:
    raw_sources = subject.get("fact_sources", subject.get("sources", []))
    if not isinstance(raw_sources, list) or not raw_sources:
        _fail(f"subject {subject.get('id')!r} has no factual sources.")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, dict):
            _fail(f"fact source {index} must be an object.")
        url = str(raw.get("url", ""))
        if not url.startswith("https://"):
            _fail(f"fact source {raw.get('id')!r} must use HTTPS.")
        publisher = _plot_text(raw.get("publisher", "OFFICIAL SOURCE")).strip()
        result.append(
            {
                "id": str(raw["id"]),
                "kind": _fact_kind(
                    str(raw.get("source_kind", raw.get("kind", "spec")))
                ),
                "title": str(raw.get("title", publisher)),
                "publisher": publisher,
                "url": url,
                "attribution": f"Facts transcribed from {publisher}",
                "visible_credit": f"FACTS / {publisher.upper()}",
                "license": "Factual citation only; webpage artwork is not reproduced",
                "rights_status": "review-required",
                "captured_at": str(
                    raw.get("captured_at", raw.get("accessed", "2026-08-09"))
                ),
                "method": "Manual transcription of the specifically cited displayed fact",
            }
        )
    ids = [item["id"] for item in result]
    if len(ids) != len(set(ids)):
        _fail(f"subject {subject.get('id')!r} repeats a factual source id.")
    return result


def _source_ids(subject: dict[str, Any]) -> set[str]:
    return {item["id"] for item in _normalize_fact_sources(subject)}


def _normalize_specifications(subject: dict[str, Any]) -> list[dict[str, Any]]:
    raw_specs = subject.get("specifications", subject.get("verified_facts", []))
    if not isinstance(raw_specs, list):
        _fail(f"subject {subject.get('id')!r} specifications must be an array.")
    known_sources = _source_ids(subject)
    result: list[dict[str, Any]] = []
    for raw in raw_specs:
        if not isinstance(raw, dict):
            _fail("specification must be an object.")
        refs = raw.get("source_refs", [raw.get("source_ref")])
        source_ref = next((str(ref) for ref in refs if ref), "")
        if source_ref not in known_sources:
            _fail(
                f"subject {subject.get('id')!r} specification {raw.get('id')!r} "
                f"references unknown factual source {source_ref!r}."
            )
        display = raw.get("display")
        value = _plot_text(
            display if display is not None else raw.get("value", "")
        ).strip()
        unit = (
            "display"
            if display is not None
            else _plot_text(raw.get("unit", "")).strip()
        )
        if "verified" in raw:
            verified = bool(raw["verified"])
        elif "verification" in raw:
            verification = str(raw["verification"]).casefold()
            verified = "verified" in verification and "unverified" not in verification
        else:
            verified = True
        result.append(
            {
                "id": str(raw["id"]),
                "label": _plot_text(raw["label"]),
                "value": value,
                "unit": unit or "—",
                "source_ref": source_ref,
                "verified": verified,
                "selected": bool(raw.get("selected", raw.get("display", True))),
            }
        )
    return result


def _normalize_history(subject: dict[str, Any]) -> list[dict[str, Any]]:
    known_sources = _source_ids(subject)
    result: list[dict[str, Any]] = []
    raw_history = subject.get("history", [])
    if isinstance(raw_history, dict):
        raw_history = [raw_history]
    if not isinstance(raw_history, list):
        _fail(f"subject {subject.get('id')!r} history must be an object or array.")
    for index, raw in enumerate(raw_history):
        if not isinstance(raw, dict):
            _fail(f"subject {subject.get('id')!r} history item must be an object.")
        refs = raw.get("source_refs", [raw.get("source_ref")])
        source_ref = next((str(ref) for ref in refs if ref), "")
        if source_ref not in known_sources:
            _fail(
                f"subject {subject.get('id')!r} history references unknown source {source_ref!r}."
            )
        result.append(
            {
                "id": f"milestone-{index + 1}",
                "date": _plot_text(raw.get("date", "HISTORY")),
                "text": _plot_text(raw["text"]),
                "source_ref": source_ref,
                "verified": True,
                "selected": bool(raw.get("selected", True)),
            }
        )
    return result


def _millimetres(value: float, unit: str) -> float:
    unit_key = unit.casefold().replace(" ", "")
    if unit_key in {"mm", "millimetre", "millimetres"}:
        return value
    if unit_key in {"cm", "centimetre", "centimetres"}:
        return value * 10.0
    if unit_key in {"m", "metre", "metres"}:
        return value * 1000.0
    if unit_key in {"ft", "feet"}:
        return value * 304.8
    _fail(f"cannot convert dimension unit {unit!r} to millimetres.")
    raise AssertionError


def _dimensions(subject: dict[str, Any], category: str) -> dict[str, float | None]:
    raw_dimensions = subject.get("dimensions_mm")
    if isinstance(raw_dimensions, dict):
        raw_aliases = {
            "overall_length": "length",
            "overall_length_mm": "length",
            "length_overall": "length",
            "length_overall_mm": "length",
            "loa": "length",
            "hull_length_mm": "length",
            "extreme_length_mm": "length",
            "overall_width": "width",
            "overall_width_mm": "width",
            "beam_mm": "beam",
            "maximum_beam_mm": "beam",
            "overall_height": "height",
            "overall_height_mm": "height",
            "geometric_wingspan": "wingspan",
            "wingspan_mm": "wingspan",
            "wheelbase_mm": "wheelbase",
            "draft_mm": "draft",
            "draught_mm": "draft",
            "maximum_draft_mm": "draft",
        }
        result = {
            raw_aliases.get(str(key), str(key)): (
                None if value is None else _finite(value, f"dimensions_mm.{key}")
            )
            for key, value in raw_dimensions.items()
            if value is None
            or (isinstance(value, (int, float)) and not isinstance(value, bool))
        }
    else:
        result = {}
    facts = subject.get("specifications", subject.get("verified_facts", []))
    aliases = {
        "overall-length": "length",
        "length-overall": "length",
        "loa": "length",
        "length": "length",
        "overall-width": "width",
        "width": "width",
        "beam": "beam",
        "wingspan": "wingspan",
        "geometric-wingspan": "wingspan",
        "overall-height": "height",
        "height": "height",
        "wheelbase": "wheelbase",
        "draft": "draft",
        "draught": "draft",
    }
    for fact in facts:
        key = aliases.get(str(fact.get("id", "")).casefold())
        if key is None or key in result:
            continue
        value = fact.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = _millimetres(float(value), str(fact.get("unit", "mm")))
    if category in {"car", "racing-car"}:
        required = ("length", "width", "height")
    elif category == "aircraft":
        required = ("length", "wingspan")
    else:
        required = ("length", "beam")
    if category in {"boat", "yacht", "ship", "personal-watercraft"} and not result.get(
        "length"
    ):
        # A deliberately uncalibrated recipe may still produce an explicitly
        # NTS collector study.  The normalized fallback fixes only internal
        # layout proportions; no dimension or scale claim is emitted.
        result.update(
            {
                "length": 1000.0,
                "beam": 360.0,
                "_length_calibrated": 0.0,
                "_beam_calibrated": 0.0,
            }
        )
    elif category in {"boat", "yacht", "ship", "personal-watercraft"}:
        result["_length_calibrated"] = 1.0
        result["_beam_calibrated"] = 1.0 if result.get("beam") else 0.0
        if not result.get("beam"):
            result["beam"] = float(result["length"]) * 0.30
    elif category in {"car", "racing-car"}:
        result["_height_calibrated"] = 1.0 if result.get("height") else 0.0
        if not result.get("height") and result.get("length"):
            result["height"] = float(result["length"]) * 0.29
        result["_length_calibrated"] = 1.0
        result["_beam_calibrated"] = 1.0
    else:
        result["_length_calibrated"] = 1.0
        result["_beam_calibrated"] = 1.0
    missing = [key for key in required if not result.get(key)]
    if missing:
        _fail(
            f"subject {subject.get('id')!r} lacks required dimension(s): "
            + ", ".join(missing)
            + "."
        )
    return result


def _raw_view_controls(subject: dict[str, Any], view_type: str) -> dict[str, Any]:
    shape = subject.get(
        "shape", subject.get("shape_parameters", subject.get("geometry_recipe", {}))
    )
    raw_views = shape.get("views", shape.get("view_recipes", []))
    for raw in raw_views:
        if raw.get("type", raw.get("id")) == view_type:
            controls = raw.get("controls", {})
            return copy.deepcopy(controls) if isinstance(controls, dict) else {}
    return {}


def _view_controls(subject: dict[str, Any], view_type: str) -> dict[str, float]:
    return {
        str(key): float(value)
        for key, value in _raw_view_controls(subject, view_type).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _template(subject: dict[str, Any]) -> str:
    shape = subject.get(
        "shape", subject.get("shape_parameters", subject.get("geometry_recipe", {}))
    )
    return str(shape.get("template", "generic-engineered-object"))


def _dimension(
    identifier: str,
    start: tuple[float, float],
    end: tuple[float, float],
    label: str,
    value_mm: float,
    source_ref: str,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "start": list(start),
        "end": list(end),
        "label": label,
        "value": f"{value_mm / 1000.0:.3f}".rstrip("0").rstrip("."),
        "unit": "m",
        "source_ref": source_ref,
        "verified": True,
        "qualifier": "published overall dimension",
    }


def _dimension_source(subject: dict[str, Any], aliases: Iterable[str]) -> str:
    specs = subject.get("specifications", subject.get("verified_facts", []))
    wanted = {alias.casefold() for alias in aliases}
    for spec in specs:
        if str(spec.get("id", "")).casefold() in wanted:
            refs = spec.get("source_refs", [spec.get("source_ref")])
            source_ref = next((str(ref) for ref in refs if ref), "")
            if source_ref:
                return source_ref
    _fail(f"subject {subject.get('id')!r} has no source for {sorted(wanted)}.")
    raise AssertionError


def _view(
    view_id: str,
    view_type: str,
    primitives: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    *,
    scale_status: str = "dimension-calibrated",
) -> dict[str, Any]:
    return {
        "id": view_id,
        "type": view_type,
        "label": {
            "plan": "TOP / PLAN",
            "side": "SIDE",
            "front": "FRONT",
            "rear": "REAR",
        }[view_type],
        "unit": "millimetre",
        "axis_direction": "y-up",
        "scale_status": scale_status,
        "source_refs": ["original-parametric-geometry"],
        "primitives": primitives,
        "dimensions": dimensions,
        "callouts": [],
    }


def _car_views(
    subject: dict[str, Any], dims: dict[str, float | None]
) -> list[dict[str, Any]]:
    length = float(dims["length"])
    width = float(dims["width"])
    height = float(dims["height"])
    wheelbase = float(dims.get("wheelbase") or length * 0.59)
    subject_id = str(subject["id"])
    authored_profiles = {
        "300-sl": (0.40, 0.73, 0.84, 0.41, 0.47),
        "e-type": (0.48, 0.79, 0.82, 0.39, 0.45),
        "f40": (0.34, 0.69, 0.70, 0.34, 0.43),
        "countach": (0.36, 0.68, 0.66, 0.31, 0.42),
        "porsche-911": (0.29, 0.71, 0.83, 0.44, 0.55),
        "mx-5": (0.38, 0.73, 0.73, 0.39, 0.45),
        "mustang": (0.36, 0.70, 0.82, 0.44, 0.50),
        "corvette": (0.35, 0.72, 0.70, 0.34, 0.45),
        "supra": (0.34, 0.72, 0.76, 0.38, 0.48),
        "nissan-z": (0.34, 0.71, 0.77, 0.39, 0.47),
        "audi-r8": (0.34, 0.70, 0.71, 0.35, 0.44),
        "civic-type-r": (0.25, 0.78, 0.88, 0.48, 0.56),
        "golf-gti": (0.23, 0.80, 0.91, 0.49, 0.58),
        "mini-cooper": (0.20, 0.82, 0.94, 0.51, 0.60),
        "tesla-model-3": (0.24, 0.78, 0.88, 0.43, 0.52),
    }
    family = next(
        (token for token in authored_profiles if token in subject_id), "generic"
    )
    profile = authored_profiles.get(family, (0.31, 0.72, 0.84, 0.42, 0.49))
    # These are project-authored proportion families, not sampled body surfaces.
    # Values deliberately remain low-order: enough to make each subject legible
    # across four views without implying manufacturer/CAD accuracy.
    plan_profiles = {
        # nose, front shoulder, waist, rear shoulder, tail half-width
        "300-sl": (0.10, 0.42, 0.47, 0.46, 0.28),
        "e-type": (0.07, 0.36, 0.44, 0.48, 0.22),
        "f40": (0.18, 0.45, 0.49, 0.50, 0.34),
        "countach": (0.14, 0.43, 0.48, 0.50, 0.38),
        "porsche-911": (0.16, 0.42, 0.47, 0.50, 0.32),
        "mx-5": (0.14, 0.43, 0.49, 0.47, 0.27),
        "mustang": (0.18, 0.44, 0.48, 0.49, 0.36),
        "corvette": (0.14, 0.45, 0.49, 0.50, 0.35),
        "supra": (0.12, 0.44, 0.48, 0.50, 0.31),
        "nissan-z": (0.13, 0.42, 0.47, 0.49, 0.30),
        "audi-r8": (0.13, 0.45, 0.50, 0.49, 0.34),
        "civic-type-r": (0.21, 0.45, 0.48, 0.46, 0.38),
        "golf-gti": (0.22, 0.43, 0.46, 0.45, 0.40),
        "mini-cooper": (0.24, 0.42, 0.45, 0.44, 0.42),
        "tesla-model-3": (0.12, 0.43, 0.49, 0.48, 0.30),
    }
    end_profiles = {
        # base, shoulder y/x, glass sill/half, roof half/y
        "300-sl": (0.18, 0.51, 0.47, 0.63, 0.28, 0.16, 0.88),
        "e-type": (0.18, 0.48, 0.46, 0.60, 0.25, 0.10, 0.84),
        "f40": (0.15, 0.39, 0.49, 0.52, 0.25, 0.13, 0.71),
        "countach": (0.14, 0.36, 0.49, 0.48, 0.23, 0.10, 0.66),
        "porsche-911": (0.18, 0.50, 0.49, 0.62, 0.30, 0.14, 0.87),
        "mx-5": (0.18, 0.48, 0.46, 0.58, 0.28, 0.12, 0.74),
        "mustang": (0.19, 0.54, 0.48, 0.64, 0.30, 0.15, 0.84),
        "corvette": (0.14, 0.39, 0.49, 0.52, 0.25, 0.12, 0.70),
        "supra": (0.17, 0.46, 0.48, 0.58, 0.27, 0.11, 0.78),
        "nissan-z": (0.18, 0.48, 0.47, 0.59, 0.28, 0.12, 0.79),
        "audi-r8": (0.15, 0.40, 0.49, 0.53, 0.26, 0.12, 0.72),
        "civic-type-r": (0.20, 0.58, 0.48, 0.69, 0.34, 0.19, 0.91),
        "golf-gti": (0.21, 0.60, 0.47, 0.70, 0.35, 0.21, 0.93),
        "mini-cooper": (0.22, 0.64, 0.46, 0.73, 0.37, 0.24, 0.96),
        "tesla-model-3": (0.19, 0.55, 0.47, 0.67, 0.34, 0.18, 0.89),
    }
    plan_profile = plan_profiles.get(family, (0.16, 0.43, 0.48, 0.48, 0.32))
    end_profile = end_profiles.get(family, (0.18, 0.50, 0.47, 0.62, 0.29, 0.14, 0.84))
    is_upright_hatch = family in {"golf-gti", "mini-cooper"}
    is_fast_hatch = family == "civic-type-r"
    is_roadster = family == "mx-5"
    has_rear_wing = family in {"f40", "civic-type-r"}
    side_controls = _view_controls(subject, "side")
    cabin_start = side_controls.get("cabin_start_x", profile[0])
    cabin_end = side_controls.get("cabin_end_x", profile[1])
    roof = side_controls.get(
        "roof_height_fraction",
        profile[2],
    )
    nose = side_controls.get("nose_height_fraction", profile[3])
    tail = side_controls.get("tail_height_fraction", profile[4])
    ground = height * 0.10
    sill = height * 0.20
    wheel_radius = min(height * 0.235, length * 0.105)
    front_x = length * (0.50 - wheelbase / (2.0 * length))
    rear_x = front_x + wheelbase
    roof_y = height * roof
    body = _curve(
        "side-body-envelope",
        "body",
        "principal_silhouette",
        (0.015 * length, sill + 0.10 * height),
        [
            (
                "C",
                (0.01 * length, 0.30 * height),
                (0.04 * length, nose * height),
                (0.12 * length, nose * height),
            ),
            (
                "C",
                (0.20 * length, 0.45 * height),
                ((cabin_start - 0.05) * length, 0.56 * height),
                (cabin_start * length, 0.68 * height),
            ),
            (
                "C",
                ((cabin_start + 0.08) * length, roof_y),
                ((cabin_end - 0.09) * length, roof_y),
                (cabin_end * length, 0.67 * height),
            ),
            (
                "C",
                ((cabin_end + 0.06) * length, 0.58 * height),
                (0.88 * length, tail * height),
                (0.975 * length, 0.43 * height),
            ),
            (
                "C",
                (0.995 * length, 0.36 * height),
                (0.99 * length, 0.23 * height),
                (0.94 * length, sill),
            ),
            ("L", (0.07 * length, sill)),
            (
                "C",
                (0.03 * length, sill),
                (0.015 * length, 0.24 * height),
                (0.015 * length, sill + 0.10 * height),
            ),
        ],
        closed=True,
        priority="identity",
    )
    if is_upright_hatch:
        body = _curve(
            "side-body-envelope",
            "body",
            "principal_silhouette",
            (0.015 * length, sill + 0.10 * height),
            [
                (
                    "C",
                    (0.02 * length, 0.34 * height),
                    (0.09 * length, nose * height),
                    (0.18 * length, nose * height),
                ),
                (
                    "C",
                    (0.23 * length, 0.50 * height),
                    ((cabin_start - 0.02) * length, 0.62 * height),
                    ((cabin_start + 0.06) * length, 0.86 * height),
                ),
                (
                    "C",
                    (0.40 * length, roof_y),
                    ((cabin_end - 0.06) * length, roof_y),
                    ((cabin_end + 0.03) * length, 0.90 * height),
                ),
                (
                    "C",
                    (0.94 * length, 0.83 * height),
                    (0.98 * length, 0.70 * height),
                    (0.98 * length, 0.54 * height),
                ),
                ("L", (0.98 * length, sill)),
                ("L", (0.07 * length, sill)),
                (
                    "C",
                    (0.03 * length, sill),
                    (0.015 * length, 0.24 * height),
                    (0.015 * length, sill + 0.10 * height),
                ),
            ],
            closed=True,
            priority="identity",
        )
    elif is_fast_hatch:
        body = _curve(
            "side-body-envelope",
            "body",
            "principal_silhouette",
            (0.015 * length, sill + 0.10 * height),
            [
                (
                    "C",
                    (0.02 * length, 0.34 * height),
                    (0.08 * length, nose * height),
                    (0.16 * length, nose * height),
                ),
                (
                    "C",
                    (0.22 * length, 0.50 * height),
                    ((cabin_start + 0.03) * length, 0.70 * height),
                    ((cabin_start + 0.10) * length, roof_y),
                ),
                (
                    "C",
                    (0.50 * length, roof_y),
                    (0.70 * length, 0.95 * roof_y),
                    (0.88 * length, 0.67 * height),
                ),
                (
                    "C",
                    (0.96 * length, 0.58 * height),
                    (0.99 * length, 0.42 * height),
                    (0.97 * length, sill),
                ),
                ("L", (0.07 * length, sill)),
                (
                    "C",
                    (0.03 * length, sill),
                    (0.015 * length, 0.24 * height),
                    (0.015 * length, sill + 0.10 * height),
                ),
            ],
            closed=True,
            priority="identity",
        )
    elif is_roadster:
        body = _curve(
            "side-body-envelope",
            "body",
            "principal_silhouette",
            (0.015 * length, sill + 0.10 * height),
            [
                (
                    "C",
                    (0.02 * length, 0.32 * height),
                    (0.07 * length, nose * height),
                    (0.18 * length, nose * height),
                ),
                (
                    "C",
                    (0.28 * length, 0.48 * height),
                    ((cabin_start - 0.03) * length, 0.53 * height),
                    (cabin_start * length, 0.54 * height),
                ),
                ("L", ((cabin_start + 0.045) * length, 0.71 * height)),
                ("L", ((cabin_start + 0.075) * length, 0.47 * height)),
                ("L", ((cabin_end - 0.07) * length, 0.47 * height)),
                (
                    "C",
                    ((cabin_end - 0.02) * length, 0.53 * height),
                    (0.88 * length, tail * height),
                    (0.975 * length, 0.42 * height),
                ),
                (
                    "C",
                    (0.995 * length, 0.34 * height),
                    (0.99 * length, 0.23 * height),
                    (0.94 * length, sill),
                ),
                ("L", (0.07 * length, sill)),
                (
                    "C",
                    (0.03 * length, sill),
                    (0.015 * length, 0.24 * height),
                    (0.015 * length, sill + 0.10 * height),
                ),
            ],
            closed=True,
            priority="identity",
        )
    glasshouse: dict[str, Any]
    if is_roadster:
        glasshouse = _line(
            "side-open-cockpit",
            "glasshouse",
            "glazing_openings",
            [
                (cabin_start * length, 0.54 * height),
                ((cabin_start + 0.045) * length, 0.71 * height),
                ((cabin_start + 0.075) * length, 0.47 * height),
                ((cabin_end - 0.07) * length, 0.47 * height),
            ],
            priority="identity",
        )
    elif is_upright_hatch:
        glasshouse = _curve(
            "side-glasshouse",
            "glasshouse",
            "glazing_openings",
            ((cabin_start + 0.02) * length, 0.65 * height),
            [
                (
                    "C",
                    ((cabin_start + 0.08) * length, 0.88 * height),
                    (0.42 * length, 0.90 * height),
                    (0.55 * length, 0.90 * height),
                ),
                ("L", ((cabin_end - 0.03) * length, 0.88 * height)),
                ("L", ((cabin_end + 0.015) * length, 0.65 * height)),
                ("L", ((cabin_start + 0.02) * length, 0.65 * height)),
            ],
            closed=True,
            priority="identity",
        )
    else:
        glasshouse = _curve(
            "side-glasshouse",
            "glasshouse",
            "glazing_openings",
            ((cabin_start + 0.025) * length, 0.66 * height),
            [
                (
                    "C",
                    ((cabin_start + 0.10) * length, 0.84 * height),
                    ((cabin_end - 0.10) * length, 0.84 * height),
                    ((cabin_end - 0.02) * length, 0.66 * height),
                ),
                ("L", ((cabin_start + 0.025) * length, 0.66 * height)),
            ],
            closed=True,
            priority="identity",
        )
    side: list[dict[str, Any]] = [body]
    side.extend(
        [
            glasshouse,
            _line(
                "side-belt-line",
                "body",
                "major_structural_edges",
                [(0.10 * length, 0.48 * height), (0.92 * length, 0.48 * height)],
            ),
            _line(
                "side-door-front",
                "door",
                "panel_seam_lines",
                [(0.47 * length, 0.20 * height), (0.46 * length, 0.65 * height)],
                minimum_sheet="A4",
            ),
            _line(
                "side-door-rear",
                "door",
                "panel_seam_lines",
                [(0.70 * length, 0.20 * height), (0.69 * length, 0.60 * height)],
                minimum_sheet="A4",
            ),
            _line(
                "side-bonnet",
                "bonnet",
                "panel_seam_lines",
                [(0.12 * length, nose * height), (cabin_start * length, 0.57 * height)],
                minimum_sheet="A4",
            ),
            _line(
                "side-centre-line",
                "construction",
                "construction_geometry",
                [(0, ground), (length, ground)],
                line_style="centre",
                minimum_sheet="A3",
            ),
        ]
    )
    for prefix, centre in (("front", front_x), ("rear", rear_x)):
        side.append(
            _circle(
                f"side-{prefix}-tyre",
                f"{prefix}-wheel",
                "major_structural_edges",
                (centre, ground + wheel_radius),
                wheel_radius,
                priority="identity",
            )
        )
        side.append(
            _circle(
                f"side-{prefix}-rim",
                f"{prefix}-wheel",
                "mechanical_detail",
                (centre, ground + wheel_radius),
                wheel_radius * 0.62,
                minimum_sheet="A4",
            )
        )
        side.append(
            _circle(
                f"side-{prefix}-hub",
                f"{prefix}-wheel",
                "mechanical_detail",
                (centre, ground + wheel_radius),
                wheel_radius * 0.16,
                minimum_sheet="A3",
            )
        )
        side.append(
            _curve(
                f"side-{prefix}-wheel-arch",
                f"{prefix}-wheel-arch",
                "major_structural_edges",
                (centre - 1.04 * wheel_radius, sill),
                [
                    (
                        "C",
                        (centre - 0.88 * wheel_radius, sill + 1.48 * wheel_radius),
                        (centre + 0.88 * wheel_radius, sill + 1.48 * wheel_radius),
                        (centre + 1.04 * wheel_radius, sill),
                    )
                ],
                priority="identity",
            )
        )
        for spoke in range(5):
            angle = math.radians(-90.0 + spoke * 72.0)
            start = (
                centre + math.cos(angle) * wheel_radius * 0.20,
                ground + wheel_radius + math.sin(angle) * wheel_radius * 0.20,
            )
            end = (
                centre + math.cos(angle) * wheel_radius * 0.54,
                ground + wheel_radius + math.sin(angle) * wheel_radius * 0.54,
            )
            side.append(
                _line(
                    f"side-{prefix}-spoke-{spoke + 1}",
                    f"{prefix}-wheel",
                    "mechanical_detail",
                    [start, end],
                    minimum_sheet="A3",
                )
            )
    side.extend(
        [
            _line(
                "side-headlamp",
                "lighting",
                "accent_feature",
                [(0.035 * length, 0.40 * height), (0.11 * length, 0.42 * height)],
                minimum_sheet="A4",
            ),
            _line(
                "side-tail-lamp",
                "lighting",
                "accent_feature",
                [(0.93 * length, 0.43 * height), (0.98 * length, 0.39 * height)],
                minimum_sheet="A4",
            ),
            _line(
                "side-sill-detail",
                "body",
                "mechanical_detail",
                [(0.11 * length, 0.18 * height), (0.89 * length, 0.18 * height)],
                minimum_sheet="A3",
            ),
        ]
    )
    if "300-sl" in subject_id:
        side.extend(
            [
                _line(
                    "side-gullwing-forward-cut",
                    "door",
                    "panel_seam_lines",
                    [(0.50 * length, roof_y * 0.98), (0.46 * length, 0.48 * height)],
                    minimum_sheet="A4",
                ),
                _line(
                    "side-gullwing-rear-cut",
                    "door",
                    "panel_seam_lines",
                    [(0.58 * length, roof_y * 0.98), (0.69 * length, 0.48 * height)],
                    minimum_sheet="A4",
                ),
            ]
        )
    if any(token in subject_id for token in ("f40", "countach", "r8", "corvette")):
        side.append(
            _curve(
                "side-intake",
                "cooling",
                "mechanical_detail",
                (0.69 * length, 0.43 * height),
                [
                    (
                        "C",
                        (0.74 * length, 0.33 * height),
                        (0.80 * length, 0.33 * height),
                        (0.82 * length, 0.45 * height),
                    )
                ],
                minimum_sheet="A4",
            )
        )
    if any(token in subject_id for token in ("f40", "civic-type-r")):
        side.append(
            _line(
                "side-rear-wing",
                "aero",
                "accent_feature",
                [(0.79 * length, 0.59 * height), (0.96 * length, 0.60 * height)],
                priority="identity",
            )
        )

    def end_view(view_type: str) -> list[dict[str, Any]]:
        rear = view_type == "rear"
        prefix = "rear" if rear else "front"
        (
            body_base,
            shoulder_y,
            shoulder_half,
            glass_sill,
            glass_half,
            roof_half,
            roof_top,
        ) = end_profile
        if rear:
            body_base += 0.01
            shoulder_y += 0.035 if is_upright_hatch else 0.015
            shoulder_half = min(0.495, shoulder_half + 0.012)
            glass_sill += 0.025 if is_upright_hatch else 0.012
            glass_half += 0.025 if is_upright_hatch else 0.010
            roof_top -= 0.005 if is_upright_hatch else 0.015
        body_points = [
            (-0.50 * width, body_base * height),
            (-0.50 * width, (body_base + 0.13) * height),
            (-shoulder_half * width, shoulder_y * height),
            (-glass_half * width, glass_sill * height),
            (-roof_half * width, roof_top * height),
            (0, min(0.985, roof_top + 0.018) * height),
            (roof_half * width, roof_top * height),
            (glass_half * width, glass_sill * height),
            (shoulder_half * width, shoulder_y * height),
            (0.50 * width, (body_base + 0.13) * height),
            (0.50 * width, body_base * height),
        ]
        glass_points = [
            (-glass_half * width, glass_sill * height),
            (-roof_half * width, roof_top * height),
            (0, min(0.98, roof_top + 0.012) * height),
            (roof_half * width, roof_top * height),
            (glass_half * width, glass_sill * height),
        ]
        primitives = [
            _primitive(
                f"{prefix}-body-envelope",
                "body",
                "principal_silhouette",
                {"path": _smooth_closed(body_points)},
                priority="identity",
            ),
            _primitive(
                f"{prefix}-glass",
                "glasshouse",
                "glazing_openings",
                {"path": _smooth_closed(glass_points)},
                priority="identity",
            ),
            _line(
                f"{prefix}-centre",
                "construction",
                "construction_geometry",
                [(0, 0.12 * height), (0, 0.92 * height)],
                line_style="centre",
                minimum_sheet="A3",
            ),
        ]
        front_lamp_style = {
            "300-sl": "oval",
            "e-type": "round",
            "f40": "slit",
            "countach": "slit",
            "porsche-911": "round",
            "mx-5": "slit",
            "mustang": "angular",
            "corvette": "angular",
            "supra": "angular",
            "nissan-z": "slit",
            "audi-r8": "angular",
            "civic-type-r": "angular",
            "golf-gti": "slit",
            "mini-cooper": "round",
            "tesla-model-3": "slit",
        }.get(family, "oval")
        rear_lamp_style = {
            "f40": "four-round",
            "mustang": "three-bars",
            "porsche-911": "light-bar",
            "nissan-z": "light-bar",
            "golf-gti": "angular",
            "mini-cooper": "upright",
            "tesla-model-3": "boomerang",
            "civic-type-r": "angular",
            "countach": "light-bar",
        }.get(family, "oval")
        lamp_style = rear_lamp_style if rear else front_lamp_style
        lamp_y = (0.49 if rear else 0.45) * height
        if lamp_style == "four-round":
            for side_name, sign in (("left", -1.0), ("right", 1.0)):
                for position, offset in (("outer", 0.36), ("inner", 0.24)):
                    primitives.append(
                        _circle(
                            f"rear-{side_name}-{position}-lamp",
                            "lighting",
                            "accent_feature",
                            (sign * offset * width, lamp_y),
                            0.035 * width,
                            minimum_sheet="A4",
                        )
                    )
        elif lamp_style == "three-bars":
            for side_name, sign in (("left", -1.0), ("right", 1.0)):
                for index in range(3):
                    x = sign * (0.27 + index * 0.055) * width
                    primitives.append(
                        _line(
                            f"rear-{side_name}-lamp-bar-{index + 1}",
                            "lighting",
                            "accent_feature",
                            [
                                (x, lamp_y - 0.055 * height),
                                (x, lamp_y + 0.055 * height),
                            ],
                            minimum_sheet="A4",
                        )
                    )
        elif lamp_style == "upright":
            for side_name, sign in (("left", -1.0), ("right", 1.0)):
                primitives.append(
                    _line(
                        f"rear-{side_name}-upright-lamp",
                        "lighting",
                        "accent_feature",
                        [
                            (sign * 0.42 * width, lamp_y - 0.075 * height),
                            (sign * 0.31 * width, lamp_y - 0.045 * height),
                            (sign * 0.31 * width, lamp_y + 0.045 * height),
                            (sign * 0.42 * width, lamp_y + 0.075 * height),
                        ],
                        minimum_sheet="A4",
                    )
                )
        elif lamp_style == "light-bar":
            primitives.append(
                _line(
                    "rear-light-bar",
                    "lighting",
                    "accent_feature",
                    [
                        (-0.43 * width, lamp_y),
                        (-0.16 * width, lamp_y + 0.025 * height),
                        (0, lamp_y + 0.015 * height),
                        (0.16 * width, lamp_y + 0.025 * height),
                        (0.43 * width, lamp_y),
                    ],
                    minimum_sheet="A4",
                )
            )
        elif lamp_style in {"angular", "boomerang", "slit"}:
            rise = 0.045 if lamp_style == "boomerang" else 0.025
            span = 0.17 if lamp_style == "slit" else 0.20
            for side_name, sign in (("left", -1.0), ("right", 1.0)):
                primitives.append(
                    _line(
                        f"{prefix}-{side_name}-lamp",
                        "lighting",
                        "accent_feature",
                        [
                            (sign * 0.44 * width, lamp_y),
                            (sign * 0.34 * width, (lamp_y / height + rise) * height),
                            (sign * span * width, lamp_y),
                        ],
                        minimum_sheet="A4",
                    )
                )
        else:
            radius_x = 0.072 if lamp_style == "round" else 0.115
            radius_y = 0.060 if lamp_style == "round" else 0.040
            lamp_x = 0.33 if lamp_style == "round" else 0.34
            for side_name, sign in (("left", -1.0), ("right", 1.0)):
                primitives.append(
                    _ellipse(
                        f"{prefix}-{side_name}-lamp",
                        "lighting",
                        "accent_feature",
                        (sign * lamp_x * width, lamp_y),
                        radius_x * width,
                        radius_y * height,
                        minimum_sheet="A4",
                    )
                )
        if rear:
            primitives.append(
                _line(
                    "rear-diffuser",
                    "aero",
                    "mechanical_detail",
                    [
                        (-0.36 * width, (body_base + 0.03) * height),
                        (0, max(0.11, body_base - 0.02) * height),
                        (0.36 * width, (body_base + 0.03) * height),
                    ],
                    minimum_sheet="A4",
                )
            )
            exhaust_layout = {
                "300-sl": (-0.29,),
                "e-type": (-0.08, 0.08),
                "f40": (-0.10, 0.0, 0.10),
                "countach": (-0.18, -0.06, 0.06, 0.18),
                "porsche-911": (-0.29, 0.29),
                "mx-5": (-0.31, -0.22),
                "mustang": (-0.35, -0.27, 0.27, 0.35),
                "corvette": (-0.34, -0.26, 0.26, 0.34),
                "supra": (-0.31, 0.31),
                "nissan-z": (-0.32, 0.32),
                "audi-r8": (-0.31, 0.31),
                "civic-type-r": (-0.09, 0.0, 0.09),
                "golf-gti": (-0.31, 0.31),
                "mini-cooper": (-0.08, 0.08),
                "tesla-model-3": (),
            }.get(family, (-0.28, 0.28))
            for index, offset in enumerate(exhaust_layout, start=1):
                primitives.append(
                    _circle(
                        f"rear-exhaust-{index}",
                        "exhaust",
                        "mechanical_detail",
                        (offset * width, (body_base + 0.035) * height),
                        (0.026 if len(exhaust_layout) >= 3 else 0.035) * width,
                        minimum_sheet="A3",
                    )
                )
            if has_rear_wing:
                wing_y = (0.84 if family == "f40" else 0.965) * height
                primitives.extend(
                    [
                        _line(
                            "rear-wing-main",
                            "aero",
                            "accent_feature",
                            [(-0.46 * width, wing_y), (0.46 * width, wing_y)],
                            priority="identity",
                        ),
                        _line(
                            "rear-wing-struts",
                            "aero",
                            "major_structural_edges",
                            [
                                (-0.28 * width, wing_y),
                                (-0.24 * width, shoulder_y * height),
                                (0.24 * width, shoulder_y * height),
                                (0.28 * width, wing_y),
                            ],
                            priority="identity",
                        ),
                    ]
                )
        else:
            if family == "tesla-model-3":
                primitives.append(
                    _curve(
                        "front-lower-cooling-slot",
                        "cooling",
                        "mechanical_detail",
                        (-0.18 * width, 0.23 * height),
                        [
                            (
                                "C",
                                (-0.10 * width, 0.19 * height),
                                (0.10 * width, 0.19 * height),
                                (0.18 * width, 0.23 * height),
                            )
                        ],
                        minimum_sheet="A4",
                    )
                )
            elif family == "e-type":
                primitives.append(
                    _ellipse(
                        "front-oval-intake",
                        "cooling",
                        "mechanical_detail",
                        (0, 0.28 * height),
                        0.18 * width,
                        0.075 * height,
                        minimum_sheet="A4",
                    )
                )
            elif family in {"f40", "countach", "corvette", "audi-r8"}:
                for side_name, sign in (("left", -1.0), ("right", 1.0)):
                    primitives.append(
                        _curve(
                            f"front-{side_name}-intake",
                            "cooling",
                            "mechanical_detail",
                            (sign * 0.43 * width, 0.25 * height),
                            [
                                (
                                    "C",
                                    (sign * 0.34 * width, 0.17 * height),
                                    (sign * 0.24 * width, 0.18 * height),
                                    (sign * 0.20 * width, 0.28 * height),
                                )
                            ],
                            minimum_sheet="A4",
                        )
                    )
            else:
                opening_id = {
                    "mustang": "front-grille-opening",
                    "golf-gti": "front-upper-grille",
                    "civic-type-r": "front-main-cooling-opening",
                }.get(family, "front-lower-intake")
                opening_half = 0.34 if family in {"mustang", "civic-type-r"} else 0.25
                primitives.append(
                    _curve(
                        opening_id,
                        "cooling",
                        "mechanical_detail",
                        (-opening_half * width, 0.26 * height),
                        [
                            (
                                "C",
                                (-0.16 * width, 0.17 * height),
                                (0.16 * width, 0.17 * height),
                                (opening_half * width, 0.26 * height),
                            )
                        ],
                        minimum_sheet="A4",
                    )
                )
        return primitives

    plan_controls = _view_controls(subject, "plan")
    nose_half, front_shoulder, default_waist, rear_shoulder, tail_half = plan_profile
    waist = plan_controls.get("waist_half_width_fraction", default_waist)
    plan_body_points = [
        (0, -nose_half * width),
        (0.07 * length, -0.31 * width),
        (0.22 * length, -front_shoulder * width),
        (0.50 * length, -waist * width),
        (0.80 * length, -rear_shoulder * width),
        (length, -tail_half * width),
        (length, tail_half * width),
        (0.80 * length, rear_shoulder * width),
        (0.50 * length, waist * width),
        (0.22 * length, front_shoulder * width),
        (0.07 * length, 0.31 * width),
        (0, nose_half * width),
    ]
    if is_upright_hatch:
        glass_front, glass_mid, glass_rear = 0.30, 0.37, 0.35
    elif is_fast_hatch:
        glass_front, glass_mid, glass_rear = 0.28, 0.36, 0.33
    elif is_roadster:
        glass_front, glass_mid, glass_rear = 0.23, 0.31, 0.25
    elif family == "porsche-911":
        glass_front, glass_mid, glass_rear = 0.27, 0.35, 0.31
    elif family in {"f40", "countach", "corvette", "audi-r8"}:
        glass_front, glass_mid, glass_rear = 0.22, 0.32, 0.27
    elif family == "tesla-model-3":
        glass_front, glass_mid, glass_rear = 0.29, 0.38, 0.33
    else:
        glass_front, glass_mid, glass_rear = 0.26, 0.34, 0.29
    cabin_mid = (cabin_start + cabin_end) * 0.5
    plan_glass_points = [
        (cabin_start * length, -glass_front * width),
        (cabin_mid * length, -glass_mid * width),
        (cabin_end * length, -glass_rear * width),
        (cabin_end * length, glass_rear * width),
        (cabin_mid * length, glass_mid * width),
        (cabin_start * length, glass_front * width),
    ]
    plan: list[dict[str, Any]] = [
        _primitive(
            "plan-body-envelope",
            "body",
            "principal_silhouette",
            {"path": _smooth_closed(plan_body_points)},
            priority="identity",
        ),
        _primitive(
            "plan-glasshouse",
            "glasshouse",
            "glazing_openings",
            {"path": _smooth_closed(plan_glass_points)},
            priority="identity",
        ),
        _line(
            "plan-centre",
            "construction",
            "construction_geometry",
            [(0, 0), (length, 0)],
            line_style="centre",
            minimum_sheet="A3",
        ),
        _line(
            "plan-bonnet-seam",
            "bonnet",
            "panel_seam_lines",
            [
                (0.10 * length, -0.25 * width),
                (cabin_start * length, -glass_front * width),
            ],
            minimum_sheet="A4",
        ),
        _line(
            "plan-bonnet-seam-right",
            "bonnet",
            "panel_seam_lines",
            [
                (0.10 * length, 0.25 * width),
                (cabin_start * length, glass_front * width),
            ],
            minimum_sheet="A4",
        ),
        _line(
            "plan-roof-divider",
            "glasshouse",
            "major_structural_edges",
            [
                (cabin_mid * length, -glass_mid * width),
                (cabin_mid * length, glass_mid * width),
            ],
            minimum_sheet="A4",
        ),
        _ellipse(
            "plan-left-mirror",
            "mirrors",
            "mechanical_detail",
            ((cabin_start + 0.035) * length, -0.53 * width),
            0.035 * length,
            0.045 * width,
            minimum_sheet="A3",
        ),
        _ellipse(
            "plan-right-mirror",
            "mirrors",
            "mechanical_detail",
            ((cabin_start + 0.035) * length, 0.53 * width),
            0.035 * length,
            0.045 * width,
            minimum_sheet="A3",
        ),
    ]
    if is_upright_hatch or is_fast_hatch:
        plan.append(
            _line(
                "plan-hatch-seam",
                "rear-hatch",
                "panel_seam_lines",
                [
                    ((cabin_end + 0.035) * length, -glass_rear * width),
                    ((cabin_end + 0.035) * length, glass_rear * width),
                ],
                minimum_sheet="A4",
            )
        )
    if family == "porsche-911":
        for index, station in enumerate((0.82, 0.85, 0.88), start=1):
            plan.append(
                _line(
                    f"plan-rear-deck-vent-{index}",
                    "powertrain-deck",
                    "mechanical_detail",
                    [
                        (station * length, -0.20 * width),
                        (station * length, 0.20 * width),
                    ],
                    minimum_sheet="A3",
                )
            )
    if family == "tesla-model-3":
        plan.append(
            _line(
                "plan-panoramic-roof-divider",
                "glasshouse",
                "major_structural_edges",
                [
                    (0.66 * length, -glass_mid * width),
                    (0.66 * length, glass_mid * width),
                ],
                minimum_sheet="A4",
            )
        )
    if has_rear_wing:
        plan.extend(
            [
                _line(
                    "plan-rear-wing",
                    "aero",
                    "accent_feature",
                    [
                        (0.88 * length, -0.51 * width),
                        (0.88 * length, 0.51 * width),
                    ],
                    priority="identity",
                ),
                _line(
                    "plan-rear-wing-chord",
                    "aero",
                    "major_structural_edges",
                    [
                        (0.91 * length, -0.48 * width),
                        (0.91 * length, 0.48 * width),
                    ],
                    priority="identity",
                ),
            ]
        )
    length_source = _dimension_source(subject, ("overall-length", "length"))
    width_source = _dimension_source(subject, ("overall-width", "width"))
    vertical_scale = (
        "dimension-anchored-envelope"
        if bool(dims.get("_height_calibrated", 1.0))
        else "not-to-scale"
    )
    return [
        _view(
            "side",
            "side",
            side,
            [
                _dimension(
                    "side-overall-length",
                    (0, 0),
                    (length, 0),
                    "LENGTH",
                    length,
                    length_source,
                )
            ],
            scale_status=vertical_scale,
        ),
        _view(
            "front",
            "front",
            end_view("front"),
            [
                _dimension(
                    "front-overall-width",
                    (-width / 2, 0),
                    (width / 2, 0),
                    "WIDTH",
                    width,
                    width_source,
                )
            ],
            scale_status=vertical_scale,
        ),
        _view("rear", "rear", end_view("rear"), [], scale_status=vertical_scale),
        _view(
            "plan",
            "plan",
            plan,
            [],
            scale_status="dimension-anchored-envelope",
        ),
    ]


def _aircraft_polygon(
    identifier: str,
    component: str,
    semantic: str,
    points: Sequence[tuple[float, float]],
    **kwargs: Any,
) -> dict[str, Any]:
    """Build one closed, project-authored aircraft contour."""

    values = list(points)
    if len(values) < 3:
        _fail(f"aircraft polygon {identifier!r} needs at least three points.")
    return _primitive(
        identifier,
        component,
        semantic,
        {
            "path": _path(
                values[0],
                [("L", point) for point in values[1:]],
                closed=True,
            )
        },
        **kwargs,
    )


def _aircraft_family(subject: dict[str, Any]) -> str:
    """Classify only from the authored subject identifier and recipe template."""

    subject_id = str(subject["id"]).casefold()
    template = _template(subject).casefold()
    if "lancaster" in subject_id or all(
        token in template for token in ("four-engine", "twin-tail", "bomber")
    ):
        return "lancaster-bomber"
    if any(token in subject_id for token in ("spitfire", "p51d", "mustang")) or any(
        token in template for token in ("taildragger", "elliptical-wing")
    ):
        return "wwii-single-seat"
    if any(token in subject_id for token in ("f16", "f35", "typhoon")) or any(
        token in template
        for token in ("delta-fighter", "twin-tail-fighter", "canard-fighter")
    ):
        return "modern-jet"
    if any(
        token in template
        for token in ("turbofan", "widebody", "single-aisle", "double-deck")
    ):
        return "airliner"
    _fail(
        f"aircraft subject {subject.get('id')!r} has unsupported template {template!r}."
    )
    raise AssertionError


def _aircraft_dimensioned_views(
    subject: dict[str, Any],
    dims: dict[str, float | None],
    *,
    side: list[dict[str, Any]],
    front: list[dict[str, Any]],
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    length = float(dims["length"])
    span = float(dims["wingspan"])
    known_height = dims.get("height")
    height = float(known_height or length * 0.24)
    length_source = _dimension_source(subject, ("overall-length", "length"))
    span_source = _dimension_source(subject, ("wingspan", "geometric-wingspan"))
    scale_status = "dimension-anchored-envelope" if known_height else "not-to-scale"
    return [
        _view(
            "side",
            "side",
            side,
            [
                _dimension(
                    "side-overall-length",
                    (0, 0.05 * height),
                    (length, 0.05 * height),
                    "LENGTH",
                    length,
                    length_source,
                )
            ],
            scale_status=scale_status,
        ),
        _view(
            "front",
            "front",
            front,
            [
                _dimension(
                    "front-wingspan",
                    (-span / 2, 0.03 * height),
                    (span / 2, 0.03 * height),
                    "SPAN",
                    span,
                    span_source,
                )
            ],
            scale_status=scale_status,
        ),
        _view(
            "plan",
            "plan",
            plan,
            [],
            scale_status="dimension-anchored-envelope",
        ),
    ]


def _aircraft_wwii_single_views(
    subject: dict[str, Any], dims: dict[str, float | None]
) -> list[dict[str, Any]]:
    """Author distinct Spitfire and Mustang collector-study geometry."""

    subject_id = str(subject["id"]).casefold()
    spitfire = "spitfire" in subject_id
    length = float(dims["length"])
    span = float(dims["wingspan"])
    height = float(dims.get("height") or length * 0.24)
    plan_c = _view_controls(subject, "plan")
    side_c = _view_controls(subject, "side")
    front_c = _view_controls(subject, "front")
    fuselage_half = plan_c.get("fuselage_half_width_fraction", 0.05) * span
    cockpit_station = plan_c.get("cockpit_station_y", 0.48) * length
    tail_station = plan_c.get("tailplane_station_y", 0.82) * length
    wing_root_lead = plan_c.get("wing_root_leading_y", 0.33) * length
    wing_tip_lead = plan_c.get("wing_tip_leading_y", 0.43) * length
    wing_root_trail = plan_c.get("wing_root_trailing_y", 0.66) * length
    wing_tip_trail = plan_c.get("wing_tip_trailing_y", 0.55) * length

    if spitfire:
        wing = _curve(
            "plan-elliptical-wing",
            "wing",
            "principal_silhouette",
            (-fuselage_half, wing_root_lead),
            [
                (
                    "C",
                    (-0.23 * span, 0.30 * length),
                    (-0.47 * span, 0.38 * length),
                    (-span / 2, 0.49 * length),
                ),
                (
                    "C",
                    (-0.47 * span, 0.59 * length),
                    (-0.22 * span, 0.69 * length),
                    (-fuselage_half, wing_root_trail),
                ),
                ("L", (fuselage_half, wing_root_trail)),
                (
                    "C",
                    (0.22 * span, 0.69 * length),
                    (0.47 * span, 0.59 * length),
                    (span / 2, 0.49 * length),
                ),
                (
                    "C",
                    (0.47 * span, 0.38 * length),
                    (0.23 * span, 0.30 * length),
                    (fuselage_half, wing_root_lead),
                ),
                ("L", (-fuselage_half, wing_root_lead)),
            ],
            closed=True,
            priority="identity",
        )
    else:
        wing = _aircraft_polygon(
            "plan-tapered-laminar-wing",
            "wing",
            "principal_silhouette",
            [
                (-fuselage_half, wing_root_lead),
                (-span / 2, wing_tip_lead),
                (-0.48 * span, wing_tip_trail),
                (-fuselage_half, wing_root_trail),
                (fuselage_half, wing_root_trail),
                (0.48 * span, wing_tip_trail),
                (span / 2, wing_tip_lead),
                (fuselage_half, wing_root_lead),
            ],
            priority="identity",
        )

    plan: list[dict[str, Any]] = [
        _curve(
            "plan-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, 0),
            [
                (
                    "C",
                    (-0.45 * fuselage_half, 0.025 * length),
                    (-fuselage_half, 0.18 * length),
                    (-fuselage_half, 0.48 * length),
                ),
                (
                    "C",
                    (-0.92 * fuselage_half, 0.72 * length),
                    (-0.42 * fuselage_half, 0.94 * length),
                    (0, length),
                ),
                (
                    "C",
                    (0.42 * fuselage_half, 0.94 * length),
                    (0.92 * fuselage_half, 0.72 * length),
                    (fuselage_half, 0.48 * length),
                ),
                (
                    "C",
                    (fuselage_half, 0.18 * length),
                    (0.45 * fuselage_half, 0.025 * length),
                    (0, 0),
                ),
            ],
            closed=True,
            priority="identity",
        ),
        wing,
        _aircraft_polygon(
            "plan-tailplane",
            "tailplane",
            "major_structural_edges",
            [
                (-0.45 * fuselage_half, tail_station),
                (-0.17 * span, tail_station + 0.07 * length),
                (-0.15 * span, tail_station + 0.11 * length),
                (0.15 * span, tail_station + 0.11 * length),
                (0.17 * span, tail_station + 0.07 * length),
                (0.45 * fuselage_half, tail_station),
            ],
            priority="identity",
        ),
        _ellipse(
            "plan-raised-canopy" if spitfire else "plan-bubble-canopy",
            "cockpit",
            "glazing_openings",
            (0, cockpit_station),
            0.62 * fuselage_half,
            0.095 * length,
            priority="identity",
        ),
        _line(
            "plan-propeller-plane",
            "propulsion",
            "mechanical_detail",
            [(-0.11 * span, 0.025 * length), (0.11 * span, 0.025 * length)],
            priority="identity",
        ),
        _line(
            "plan-centre-line",
            "construction",
            "construction_geometry",
            [(0, 0), (0, length)],
            line_style="centre",
            minimum_sheet="A3",
        ),
        _line(
            "plan-left-aileron",
            "flight-controls",
            "panel_seam_lines",
            [(-0.12 * span, 0.57 * length), (-0.42 * span, 0.54 * length)],
            minimum_sheet="A4",
        ),
        _line(
            "plan-right-aileron",
            "flight-controls",
            "panel_seam_lines",
            [(0.12 * span, 0.57 * length), (0.42 * span, 0.54 * length)],
            minimum_sheet="A4",
        ),
    ]
    if spitfire:
        plan.extend(
            [
                _ellipse(
                    "plan-left-radiator",
                    "radiator",
                    "mechanical_detail",
                    (-0.20 * span, 0.56 * length),
                    0.045 * span,
                    0.035 * length,
                    minimum_sheet="A4",
                ),
                _ellipse(
                    "plan-right-radiator",
                    "radiator",
                    "mechanical_detail",
                    (0.20 * span, 0.56 * length),
                    0.045 * span,
                    0.035 * length,
                    minimum_sheet="A4",
                ),
            ]
        )
    else:
        plan.append(
            _ellipse(
                "plan-ventral-radiator-scoop",
                "radiator",
                "mechanical_detail",
                (0, 0.63 * length),
                0.55 * fuselage_half,
                0.065 * length,
                priority="identity",
            )
        )

    body_y = 0.40 * height
    body_depth = side_c.get("body_depth_fraction", 0.44) * height
    cockpit_start = side_c.get("cockpit_start_x", 0.40) * length
    cockpit_end = side_c.get("cockpit_end_x", 0.58) * length
    cowling_end = side_c.get("cowling_end_x", 0.39) * length
    side: list[dict[str, Any]] = [
        _curve(
            "side-fuselage",
            "fuselage",
            "principal_silhouette",
            (0.015 * length, body_y),
            [
                (
                    "C",
                    (0.035 * length, body_y + 0.24 * body_depth),
                    (0.16 * length, body_y + 0.34 * body_depth),
                    (cowling_end, body_y + 0.28 * body_depth),
                ),
                (
                    "C",
                    (0.57 * length, body_y + 0.22 * body_depth),
                    (0.84 * length, body_y + 0.12 * body_depth),
                    (length, body_y),
                ),
                (
                    "C",
                    (0.91 * length, body_y - 0.20 * body_depth),
                    (0.63 * length, body_y - 0.38 * body_depth),
                    (0.34 * length, body_y - 0.33 * body_depth),
                ),
                (
                    "C",
                    (0.13 * length, body_y - 0.27 * body_depth),
                    (0.035 * length, body_y - 0.18 * body_depth),
                    (0.015 * length, body_y),
                ),
            ],
            closed=True,
            priority="identity",
        ),
        _aircraft_polygon(
            "side-rounded-fin" if spitfire else "side-mustang-fin",
            "fin",
            "principal_silhouette",
            [
                (0.80 * length, body_y + 0.14 * body_depth),
                (0.87 * length, 0.94 * height),
                (0.94 * length, body_y + 0.10 * body_depth),
            ],
            priority="identity",
        ),
        _line(
            "side-wing-root",
            "wing",
            "major_structural_edges",
            [
                (side_c.get("wing_root_leading_x", 0.35) * length, body_y),
                (
                    side_c.get("wing_root_trailing_x", 0.66) * length,
                    body_y - 0.09 * height,
                ),
            ],
            priority="identity",
        ),
        _line(
            "side-tailplane",
            "tailplane",
            "major_structural_edges",
            [(0.77 * length, body_y + 0.06 * height), (0.96 * length, body_y)],
            priority="identity",
        ),
        _curve(
            "side-raised-canopy" if spitfire else "side-bubble-canopy",
            "cockpit",
            "glazing_openings",
            (cockpit_start, body_y + 0.24 * body_depth),
            [
                (
                    "C",
                    (
                        cockpit_start + 0.035 * length,
                        body_y + (0.82 if spitfire else 0.92) * body_depth,
                    ),
                    (cockpit_end - 0.03 * length, body_y + 0.86 * body_depth),
                    (cockpit_end, body_y + 0.22 * body_depth),
                ),
                ("L", (cockpit_start, body_y + 0.24 * body_depth)),
            ],
            closed=True,
            priority="identity",
        ),
        _ellipse(
            "side-propeller-disc",
            "propulsion",
            "mechanical_detail",
            (0.027 * length, body_y),
            0.012 * length,
            min(0.105 * span, 0.34 * height),
            priority="identity",
        ),
        _line(
            "side-propeller-axis",
            "propulsion",
            "mechanical_detail",
            [(0.002 * length, body_y), (0.08 * length, body_y)],
            priority="identity",
        ),
        _line(
            "side-cowling-seam",
            "powerplant",
            "panel_seam_lines",
            [
                (cowling_end, body_y - 0.24 * body_depth),
                (cowling_end, body_y + 0.27 * body_depth),
            ],
            minimum_sheet="A4",
        ),
        _line(
            "side-exhaust-stack",
            "powerplant",
            "mechanical_detail",
            [
                (0.16 * length, body_y + 0.17 * body_depth),
                (0.30 * length, body_y + 0.15 * body_depth),
            ],
            minimum_sheet="A3",
        ),
        _line(
            "side-main-gear-strut",
            "landing-gear",
            "mechanical_detail",
            [
                (0.43 * length, body_y - 0.25 * body_depth),
                (0.40 * length, 0.14 * height),
            ],
            minimum_sheet="A4",
        ),
        _circle(
            "side-main-gear-wheel",
            "landing-gear",
            "mechanical_detail",
            (0.40 * length, 0.11 * height),
            0.025 * height,
            minimum_sheet="A4",
        ),
        _circle(
            "side-tailwheel",
            "landing-gear",
            "mechanical_detail",
            (0.90 * length, 0.10 * height),
            0.016 * height,
            minimum_sheet="A4",
        ),
        _line(
            "side-datum",
            "construction",
            "construction_geometry",
            [(0, body_y), (length, body_y)],
            line_style="centre",
            minimum_sheet="A3",
        ),
    ]
    if spitfire:
        side.append(
            _ellipse(
                "side-wing-radiator",
                "radiator",
                "mechanical_detail",
                (0.54 * length, body_y - 0.31 * body_depth),
                0.055 * length,
                0.07 * height,
                minimum_sheet="A4",
            )
        )
    else:
        side.append(
            _curve(
                "side-ventral-radiator-scoop",
                "radiator",
                "major_structural_edges",
                (0.51 * length, body_y - 0.31 * body_depth),
                [
                    (
                        "C",
                        (0.56 * length, body_y - 0.62 * body_depth),
                        (0.66 * length, body_y - 0.62 * body_depth),
                        (0.70 * length, body_y - 0.28 * body_depth),
                    )
                ],
                priority="identity",
            )
        )

    fuselage_half_front = front_c.get("fuselage_half_width_fraction", 0.052) * span
    wing_root_y = front_c.get("wing_root_y", 0.43) * height
    wing_tip_y = front_c.get("wing_tip_y", 0.49) * height
    propeller_radius = min(
        front_c.get("propeller_radius_fraction", 0.105) * span,
        0.37 * height,
    )
    gear_offset = front_c.get("gear_offset_fraction", 0.17) * span
    front: list[dict[str, Any]] = [
        _ellipse(
            "front-engine-cowling",
            "fuselage",
            "principal_silhouette",
            (0, 0.43 * height),
            fuselage_half_front,
            0.29 * height,
            priority="identity",
        ),
        _line(
            "front-wing",
            "wing",
            "principal_silhouette",
            [
                (-span / 2, wing_tip_y),
                (-fuselage_half_front, wing_root_y),
                (fuselage_half_front, wing_root_y),
                (span / 2, wing_tip_y),
            ],
            priority="identity",
        ),
        _circle(
            "front-propeller-disc",
            "propulsion",
            "mechanical_detail",
            (0, 0.43 * height),
            propeller_radius,
            priority="identity",
        ),
        _line(
            "front-fin",
            "fin",
            "major_structural_edges",
            [(0, 0.53 * height), (0, 0.95 * height)],
            priority="identity",
        ),
        _curve(
            "front-canopy",
            "cockpit",
            "glazing_openings",
            (-0.48 * fuselage_half_front, 0.56 * height),
            [
                (
                    "C",
                    (-0.25 * fuselage_half_front, 0.71 * height),
                    (0.25 * fuselage_half_front, 0.71 * height),
                    (0.48 * fuselage_half_front, 0.56 * height),
                )
            ],
            priority="identity",
        ),
        _line(
            "front-left-main-gear",
            "landing-gear",
            "mechanical_detail",
            [(-gear_offset, wing_root_y), (-0.82 * gear_offset, 0.12 * height)],
            minimum_sheet="A4",
        ),
        _line(
            "front-right-main-gear",
            "landing-gear",
            "mechanical_detail",
            [(gear_offset, wing_root_y), (0.82 * gear_offset, 0.12 * height)],
            minimum_sheet="A4",
        ),
        _circle(
            "front-left-wheel",
            "landing-gear",
            "mechanical_detail",
            (-0.82 * gear_offset, 0.10 * height),
            0.022 * height,
            minimum_sheet="A4",
        ),
        _circle(
            "front-right-wheel",
            "landing-gear",
            "mechanical_detail",
            (0.82 * gear_offset, 0.10 * height),
            0.022 * height,
            minimum_sheet="A4",
        ),
        _line(
            "front-centre",
            "construction",
            "construction_geometry",
            [(0, 0.05 * height), (0, height)],
            line_style="centre",
            minimum_sheet="A3",
        ),
    ]
    if spitfire:
        front.extend(
            [
                _ellipse(
                    "front-left-radiator",
                    "radiator",
                    "mechanical_detail",
                    (-0.19 * span, wing_root_y - 0.06 * height),
                    0.042 * span,
                    0.045 * height,
                    minimum_sheet="A4",
                ),
                _ellipse(
                    "front-right-radiator",
                    "radiator",
                    "mechanical_detail",
                    (0.19 * span, wing_root_y - 0.06 * height),
                    0.042 * span,
                    0.045 * height,
                    minimum_sheet="A4",
                ),
            ]
        )
    else:
        front.append(
            _ellipse(
                "front-radiator-intake",
                "radiator",
                "mechanical_detail",
                (0, 0.19 * height),
                0.65 * fuselage_half_front,
                0.055 * height,
                priority="identity",
            )
        )
    return _aircraft_dimensioned_views(
        subject,
        dims,
        side=side,
        front=front,
        plan=plan,
    )


def _aircraft_lancaster_views(
    subject: dict[str, Any], dims: dict[str, float | None]
) -> list[dict[str, Any]]:
    """Author the Lancaster's four-engine and twin-tail identity explicitly."""

    length = float(dims["length"])
    span = float(dims["wingspan"])
    height = float(dims.get("height") or length * 0.24)
    plan_c = _view_controls(subject, "plan")
    side_c = _view_controls(subject, "side")
    front_c = _view_controls(subject, "front")
    fuselage_half = plan_c.get("fuselage_half_width_fraction", 0.034) * span
    inboard = plan_c.get("inboard_engine_offset_fraction", 0.16) * span
    outboard = plan_c.get("outboard_engine_offset_fraction", 0.32) * span
    engine_offsets = (-outboard, -inboard, inboard, outboard)
    wing_root_lead = plan_c.get("wing_root_leading_y", 0.29) * length
    wing_tip_lead = plan_c.get("wing_tip_leading_y", 0.37) * length
    wing_root_trail = plan_c.get("wing_root_trailing_y", 0.61) * length
    wing_tip_trail = plan_c.get("wing_tip_trailing_y", 0.55) * length
    tail_station = plan_c.get("tailplane_station_y", 0.80) * length
    plan: list[dict[str, Any]] = [
        _curve(
            "plan-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, 0),
            [
                (
                    "C",
                    (-0.62 * fuselage_half, 0.02 * length),
                    (-fuselage_half, 0.14 * length),
                    (-fuselage_half, 0.55 * length),
                ),
                (
                    "C",
                    (-0.82 * fuselage_half, 0.76 * length),
                    (-0.32 * fuselage_half, 0.95 * length),
                    (0, length),
                ),
                (
                    "C",
                    (0.32 * fuselage_half, 0.95 * length),
                    (0.82 * fuselage_half, 0.76 * length),
                    (fuselage_half, 0.55 * length),
                ),
                (
                    "C",
                    (fuselage_half, 0.14 * length),
                    (0.62 * fuselage_half, 0.02 * length),
                    (0, 0),
                ),
            ],
            closed=True,
            priority="identity",
        ),
        _aircraft_polygon(
            "plan-straight-tapered-wing",
            "wing",
            "principal_silhouette",
            [
                (-fuselage_half, wing_root_lead),
                (-span / 2, wing_tip_lead),
                (-0.49 * span, wing_tip_trail),
                (-fuselage_half, wing_root_trail),
                (fuselage_half, wing_root_trail),
                (0.49 * span, wing_tip_trail),
                (span / 2, wing_tip_lead),
                (fuselage_half, wing_root_lead),
            ],
            priority="identity",
        ),
        _aircraft_polygon(
            "plan-tailplane",
            "tailplane",
            "major_structural_edges",
            [
                (-0.5 * fuselage_half, tail_station),
                (-0.24 * span, tail_station + 0.05 * length),
                (-0.22 * span, tail_station + 0.11 * length),
                (0.22 * span, tail_station + 0.11 * length),
                (0.24 * span, tail_station + 0.05 * length),
                (0.5 * fuselage_half, tail_station),
            ],
            priority="identity",
        ),
        _aircraft_polygon(
            "plan-left-tail-fin",
            "left-tail-fin",
            "principal_silhouette",
            [
                (-0.17 * span, 0.78 * length),
                (-0.135 * span, 0.78 * length),
                (-0.12 * span, 0.95 * length),
                (-0.16 * span, 0.93 * length),
            ],
            priority="identity",
        ),
        _aircraft_polygon(
            "plan-right-tail-fin",
            "right-tail-fin",
            "principal_silhouette",
            [
                (0.17 * span, 0.78 * length),
                (0.135 * span, 0.78 * length),
                (0.12 * span, 0.95 * length),
                (0.16 * span, 0.93 * length),
            ],
            priority="identity",
        ),
        _ellipse(
            "plan-greenhouse-cockpit",
            "cockpit",
            "glazing_openings",
            (0, 0.14 * length),
            0.88 * fuselage_half,
            0.10 * length,
            priority="identity",
        ),
        _line(
            "plan-bomb-bay-centre",
            "bomb-bay",
            "panel_seam_lines",
            [(0, 0.25 * length), (0, 0.66 * length)],
            priority="identity",
        ),
        _line(
            "plan-centre-line",
            "construction",
            "construction_geometry",
            [(0, 0), (0, length)],
            line_style="centre",
            minimum_sheet="A3",
        ),
    ]
    for index, offset in enumerate(engine_offsets, start=1):
        plan.extend(
            [
                _ellipse(
                    f"plan-engine-nacelle-{index}",
                    "propulsion",
                    "major_structural_edges",
                    (offset, 0.43 * length),
                    0.024 * span,
                    0.11 * length,
                    priority="identity",
                ),
                _ellipse(
                    f"plan-propeller-disc-{index}",
                    "propulsion",
                    "mechanical_detail",
                    (offset, 0.34 * length),
                    min(0.062 * span, 0.35 * height),
                    0.009 * length,
                    priority="identity",
                ),
            ]
        )

    body_y = 0.39 * height
    body_depth = side_c.get("body_depth_fraction", 0.44) * height
    side: list[dict[str, Any]] = [
        _curve(
            "side-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, body_y),
            [
                (
                    "C",
                    (0.025 * length, body_y + 0.44 * body_depth),
                    (0.10 * length, body_y + 0.54 * body_depth),
                    (0.18 * length, body_y + 0.43 * body_depth),
                ),
                (
                    "C",
                    (0.50 * length, body_y + 0.32 * body_depth),
                    (0.82 * length, body_y + 0.16 * body_depth),
                    (length, body_y),
                ),
                (
                    "C",
                    (0.89 * length, body_y - 0.22 * body_depth),
                    (0.58 * length, body_y - 0.42 * body_depth),
                    (0.22 * length, body_y - 0.40 * body_depth),
                ),
                (
                    "C",
                    (0.08 * length, body_y - 0.34 * body_depth),
                    (0.015 * length, body_y - 0.20 * body_depth),
                    (0, body_y),
                ),
            ],
            closed=True,
            priority="identity",
        ),
        _aircraft_polygon(
            "side-near-tail-fin",
            "left-tail-fin",
            "principal_silhouette",
            [
                (0.79 * length, body_y + 0.11 * body_depth),
                (0.84 * length, 0.95 * height),
                (0.91 * length, body_y + 0.07 * body_depth),
            ],
            priority="identity",
        ),
        _aircraft_polygon(
            "side-far-tail-fin",
            "right-tail-fin",
            "major_structural_edges",
            [
                (0.82 * length, body_y + 0.09 * body_depth),
                (0.88 * length, 0.86 * height),
                (0.94 * length, body_y + 0.05 * body_depth),
            ],
            priority="identity",
        ),
        _line(
            "side-wing-root",
            "wing",
            "major_structural_edges",
            [
                (
                    side_c.get("wing_root_leading_x", 0.31) * length,
                    body_y + 0.08 * height,
                ),
                (
                    side_c.get("wing_root_trailing_x", 0.61) * length,
                    body_y - 0.03 * height,
                ),
            ],
            priority="identity",
        ),
        _line(
            "side-tailplane",
            "tailplane",
            "major_structural_edges",
            [
                (0.76 * length, body_y + 0.08 * height),
                (0.97 * length, body_y + 0.02 * height),
            ],
            priority="identity",
        ),
        _curve(
            "side-greenhouse-nose",
            "cockpit",
            "glazing_openings",
            (0.01 * length, body_y + 0.03 * height),
            [
                (
                    "C",
                    (0.03 * length, body_y + 0.31 * body_depth),
                    (0.08 * length, body_y + 0.45 * body_depth),
                    (0.12 * length, body_y + 0.37 * body_depth),
                ),
                ("L", (0.06 * length, body_y - 0.10 * body_depth)),
            ],
            priority="identity",
        ),
        _line(
            "side-bomb-bay-seam",
            "bomb-bay",
            "panel_seam_lines",
            [
                (
                    side_c.get("bomb_bay_start_x", 0.25) * length,
                    body_y - 0.35 * body_depth,
                ),
                (
                    side_c.get("bomb_bay_end_x", 0.66) * length,
                    body_y - 0.37 * body_depth,
                ),
            ],
            priority="identity",
        ),
        _circle(
            "side-dorsal-turret",
            "turret",
            "mechanical_detail",
            (0.53 * length, body_y + 0.39 * body_depth),
            0.055 * height,
            minimum_sheet="A4",
        ),
        _line(
            "side-datum",
            "construction",
            "construction_geometry",
            [(0, body_y), (length, body_y)],
            line_style="centre",
            minimum_sheet="A3",
        ),
    ]
    for index, station in enumerate((0.37, 0.50), start=1):
        side.extend(
            [
                _ellipse(
                    f"side-visible-engine-{index}",
                    "propulsion",
                    "major_structural_edges",
                    (station * length, body_y - 0.03 * height),
                    0.045 * length,
                    0.12 * height,
                    priority="identity",
                ),
                _ellipse(
                    f"side-visible-propeller-disc-{index}",
                    "propulsion",
                    "mechanical_detail",
                    ((station - 0.035) * length, body_y),
                    0.009 * length,
                    min(0.062 * span, 0.35 * height),
                    priority="identity",
                ),
            ]
        )

    front_half = front_c.get("fuselage_half_width_fraction", 0.034) * span
    wing_root_y = front_c.get("wing_root_y", 0.43) * height
    wing_tip_y = front_c.get("wing_tip_y", 0.51) * height
    front_inboard = front_c.get("inboard_engine_offset_fraction", 0.16) * span
    front_outboard = front_c.get("outboard_engine_offset_fraction", 0.32) * span
    front_offsets = (-front_outboard, -front_inboard, front_inboard, front_outboard)
    propeller_radius = min(
        front_c.get("propeller_radius_fraction", 0.075) * span,
        0.37 * height,
    )
    front: list[dict[str, Any]] = [
        _ellipse(
            "front-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, 0.42 * height),
            front_half,
            0.28 * height,
            priority="identity",
        ),
        _line(
            "front-mid-wing",
            "wing",
            "principal_silhouette",
            [
                (-span / 2, wing_tip_y),
                (-front_half, wing_root_y),
                (front_half, wing_root_y),
                (span / 2, wing_tip_y),
            ],
            priority="identity",
        ),
        _aircraft_polygon(
            "front-left-tail-fin",
            "left-tail-fin",
            "principal_silhouette",
            [
                (-0.18 * span, 0.48 * height),
                (-0.15 * span, 0.93 * height),
                (-0.11 * span, 0.48 * height),
            ],
            priority="identity",
        ),
        _aircraft_polygon(
            "front-right-tail-fin",
            "right-tail-fin",
            "principal_silhouette",
            [
                (0.18 * span, 0.48 * height),
                (0.15 * span, 0.93 * height),
                (0.11 * span, 0.48 * height),
            ],
            priority="identity",
        ),
        _curve(
            "front-greenhouse-glazing",
            "cockpit",
            "glazing_openings",
            (-0.68 * front_half, 0.52 * height),
            [
                (
                    "C",
                    (-0.30 * front_half, 0.68 * height),
                    (0.30 * front_half, 0.68 * height),
                    (0.68 * front_half, 0.52 * height),
                )
            ],
            priority="identity",
        ),
        _line(
            "front-centre",
            "construction",
            "construction_geometry",
            [(0, 0.05 * height), (0, height)],
            line_style="centre",
            minimum_sheet="A3",
        ),
    ]
    for index, offset in enumerate(front_offsets, start=1):
        front.extend(
            [
                _ellipse(
                    f"front-engine-nacelle-{index}",
                    "propulsion",
                    "major_structural_edges",
                    (offset, wing_root_y - 0.03 * height),
                    0.023 * span,
                    0.10 * height,
                    priority="identity",
                ),
                _circle(
                    f"front-propeller-disc-{index}",
                    "propulsion",
                    "mechanical_detail",
                    (offset, wing_root_y - 0.01 * height),
                    propeller_radius,
                    priority="identity",
                ),
            ]
        )
    return _aircraft_dimensioned_views(
        subject,
        dims,
        side=side,
        front=front,
        plan=plan,
    )


def _aircraft_modern_jet_views(
    subject: dict[str, Any], dims: dict[str, float | None]
) -> list[dict[str, Any]]:
    """Author type-specific F-16, F-35 and Typhoon schematic geometry."""

    subject_id = str(subject["id"]).casefold()
    is_f16 = "f16" in subject_id
    is_f35 = "f35" in subject_id
    is_typhoon = "typhoon" in subject_id
    if sum((is_f16, is_f35, is_typhoon)) != 1:
        _fail(f"modern jet subject {subject.get('id')!r} is not classified exactly.")
    length = float(dims["length"])
    span = float(dims["wingspan"])
    height = float(dims.get("height") or length * 0.24)
    plan_c = _view_controls(subject, "plan")
    side_c = _view_controls(subject, "side")
    front_c = _view_controls(subject, "front")
    fuselage_half = plan_c.get("fuselage_half_width_fraction", 0.08) * span
    wing_root_lead = plan_c.get("wing_root_leading_y", 0.33) * length
    wing_tip_lead = plan_c.get("wing_tip_leading_y", 0.50) * length
    wing_root_trail = plan_c.get("wing_root_trailing_y", 0.72) * length
    wing_tip_trail = plan_c.get("wing_tip_trailing_y", 0.63) * length
    cockpit_station = plan_c.get("cockpit_station_y", 0.30) * length

    if is_f16:
        plan_fuselage = _curve(
            "plan-f16-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, 0),
            [
                (
                    "C",
                    (-0.25 * fuselage_half, 0.035 * length),
                    (-0.82 * fuselage_half, 0.18 * length),
                    (-fuselage_half, 0.40 * length),
                ),
                (
                    "C",
                    (-0.80 * fuselage_half, 0.72 * length),
                    (-0.32 * fuselage_half, 0.94 * length),
                    (0, length),
                ),
                (
                    "C",
                    (0.32 * fuselage_half, 0.94 * length),
                    (0.80 * fuselage_half, 0.72 * length),
                    (fuselage_half, 0.40 * length),
                ),
                (
                    "C",
                    (0.82 * fuselage_half, 0.18 * length),
                    (0.25 * fuselage_half, 0.035 * length),
                    (0, 0),
                ),
            ],
            closed=True,
            priority="identity",
        )
        plan_wing = _aircraft_polygon(
            "plan-cropped-delta-wing",
            "wing",
            "principal_silhouette",
            [
                (-fuselage_half, wing_root_lead),
                (-span / 2, wing_tip_lead),
                (-0.48 * span, wing_tip_trail),
                (-fuselage_half, wing_root_trail),
                (fuselage_half, wing_root_trail),
                (0.48 * span, wing_tip_trail),
                (span / 2, wing_tip_lead),
                (fuselage_half, wing_root_lead),
            ],
            priority="identity",
        )
        plan_tail = _aircraft_polygon(
            "plan-all-moving-tailplanes",
            "tailplane",
            "major_structural_edges",
            [
                (-0.48 * fuselage_half, 0.74 * length),
                (-0.27 * span, 0.82 * length),
                (-0.23 * span, 0.90 * length),
                (0.23 * span, 0.90 * length),
                (0.27 * span, 0.82 * length),
                (0.48 * fuselage_half, 0.74 * length),
            ],
            priority="identity",
        )
        plan_identity = [
            _aircraft_polygon(
                "plan-single-fin-chord",
                "fin",
                "major_structural_edges",
                [
                    (-0.12 * fuselage_half, 0.66 * length),
                    (0, 0.88 * length),
                    (0.12 * fuselage_half, 0.66 * length),
                ],
                priority="identity",
            ),
            _line(
                "plan-ventral-intake-shoulders",
                "intake",
                "major_structural_edges",
                [
                    (-0.72 * fuselage_half, 0.27 * length),
                    (0, 0.36 * length),
                    (0.72 * fuselage_half, 0.27 * length),
                ],
                priority="identity",
            ),
        ]
    elif is_f35:
        plan_fuselage = _aircraft_polygon(
            "plan-f35-faceted-fuselage",
            "fuselage",
            "principal_silhouette",
            [
                (0, 0),
                (-0.42 * fuselage_half, 0.08 * length),
                (-fuselage_half, 0.31 * length),
                (-0.78 * fuselage_half, 0.68 * length),
                (-0.36 * fuselage_half, 0.94 * length),
                (0, length),
                (0.36 * fuselage_half, 0.94 * length),
                (0.78 * fuselage_half, 0.68 * length),
                (fuselage_half, 0.31 * length),
                (0.42 * fuselage_half, 0.08 * length),
            ],
            priority="identity",
        )
        plan_wing = _aircraft_polygon(
            "plan-trapezoid-stealth-wing",
            "wing",
            "principal_silhouette",
            [
                (-fuselage_half, wing_root_lead),
                (-span / 2, wing_tip_lead),
                (-0.47 * span, wing_tip_trail),
                (-fuselage_half, wing_root_trail),
                (fuselage_half, wing_root_trail),
                (0.47 * span, wing_tip_trail),
                (span / 2, wing_tip_lead),
                (fuselage_half, wing_root_lead),
            ],
            priority="identity",
        )
        plan_tail = _aircraft_polygon(
            "plan-f35-tailplanes",
            "tailplane",
            "major_structural_edges",
            [
                (-0.55 * fuselage_half, 0.70 * length),
                (-0.28 * span, 0.77 * length),
                (-0.23 * span, 0.88 * length),
                (0.23 * span, 0.88 * length),
                (0.28 * span, 0.77 * length),
                (0.55 * fuselage_half, 0.70 * length),
            ],
            priority="identity",
        )
        plan_identity = [
            _aircraft_polygon(
                "plan-left-canted-fin",
                "left-fin",
                "principal_silhouette",
                [
                    (-0.12 * span, 0.64 * length),
                    (-0.055 * span, 0.85 * length),
                    (-0.02 * span, 0.67 * length),
                ],
                priority="identity",
            ),
            _aircraft_polygon(
                "plan-right-canted-fin",
                "right-fin",
                "principal_silhouette",
                [
                    (0.12 * span, 0.64 * length),
                    (0.055 * span, 0.85 * length),
                    (0.02 * span, 0.67 * length),
                ],
                priority="identity",
            ),
            _line(
                "plan-left-side-intake",
                "intake",
                "major_structural_edges",
                [
                    (-0.78 * fuselage_half, 0.29 * length),
                    (-0.72 * fuselage_half, 0.47 * length),
                ],
                priority="identity",
            ),
            _line(
                "plan-right-side-intake",
                "intake",
                "major_structural_edges",
                [
                    (0.78 * fuselage_half, 0.29 * length),
                    (0.72 * fuselage_half, 0.47 * length),
                ],
                priority="identity",
            ),
        ]
    else:
        plan_fuselage = _curve(
            "plan-typhoon-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, 0),
            [
                (
                    "C",
                    (-0.30 * fuselage_half, 0.04 * length),
                    (-0.90 * fuselage_half, 0.21 * length),
                    (-fuselage_half, 0.43 * length),
                ),
                (
                    "C",
                    (-0.75 * fuselage_half, 0.76 * length),
                    (-0.35 * fuselage_half, 0.95 * length),
                    (0, length),
                ),
                (
                    "C",
                    (0.35 * fuselage_half, 0.95 * length),
                    (0.75 * fuselage_half, 0.76 * length),
                    (fuselage_half, 0.43 * length),
                ),
                (
                    "C",
                    (0.90 * fuselage_half, 0.21 * length),
                    (0.30 * fuselage_half, 0.04 * length),
                    (0, 0),
                ),
            ],
            closed=True,
            priority="identity",
        )
        plan_wing = _aircraft_polygon(
            "plan-full-delta-wing",
            "wing",
            "principal_silhouette",
            [
                (-fuselage_half, wing_root_lead),
                (-span / 2, wing_tip_lead),
                (-0.48 * span, wing_tip_trail),
                (-fuselage_half, wing_root_trail),
                (fuselage_half, wing_root_trail),
                (0.48 * span, wing_tip_trail),
                (span / 2, wing_tip_lead),
                (fuselage_half, wing_root_lead),
            ],
            priority="identity",
        )
        plan_tail = _aircraft_polygon(
            "plan-single-fin-chord",
            "fin",
            "major_structural_edges",
            [
                (-0.14 * fuselage_half, 0.66 * length),
                (0, 0.91 * length),
                (0.14 * fuselage_half, 0.66 * length),
            ],
            priority="identity",
        )
        canard_lead = plan_c.get("canard_leading_y", 0.27) * length
        canard_trail = plan_c.get("canard_trailing_y", 0.38) * length
        plan_identity = [
            _aircraft_polygon(
                "plan-left-canard",
                "canard",
                "principal_silhouette",
                [
                    (-0.45 * fuselage_half, canard_lead),
                    (-0.24 * span, canard_trail - 0.02 * length),
                    (-0.42 * fuselage_half, canard_trail),
                ],
                priority="identity",
            ),
            _aircraft_polygon(
                "plan-right-canard",
                "canard",
                "principal_silhouette",
                [
                    (0.45 * fuselage_half, canard_lead),
                    (0.24 * span, canard_trail - 0.02 * length),
                    (0.42 * fuselage_half, canard_trail),
                ],
                priority="identity",
            ),
            _ellipse(
                "plan-left-exhaust",
                "exhaust",
                "mechanical_detail",
                (-0.34 * fuselage_half, 0.93 * length),
                0.22 * fuselage_half,
                0.035 * length,
                priority="identity",
            ),
            _ellipse(
                "plan-right-exhaust",
                "exhaust",
                "mechanical_detail",
                (0.34 * fuselage_half, 0.93 * length),
                0.22 * fuselage_half,
                0.035 * length,
                priority="identity",
            ),
        ]

    plan: list[dict[str, Any]] = [
        plan_fuselage,
        plan_wing,
        plan_tail,
        *plan_identity,
        _ellipse(
            "plan-canopy",
            "cockpit",
            "glazing_openings",
            (0, cockpit_station),
            0.55 * fuselage_half,
            0.09 * length,
            priority="identity",
        ),
        _line(
            "plan-centre-line",
            "construction",
            "construction_geometry",
            [(0, 0), (0, length)],
            line_style="centre",
            minimum_sheet="A3",
        ),
        _line(
            "plan-left-control-surface",
            "flight-controls",
            "panel_seam_lines",
            [(-0.12 * span, 0.62 * length), (-0.40 * span, 0.62 * length)],
            minimum_sheet="A4",
        ),
        _line(
            "plan-right-control-surface",
            "flight-controls",
            "panel_seam_lines",
            [(0.12 * span, 0.62 * length), (0.40 * span, 0.62 * length)],
            minimum_sheet="A4",
        ),
    ]

    body_y = 0.42 * height
    body_depth = side_c.get("body_depth_fraction", 0.43) * height
    if is_f16:
        side_fuselage = _curve(
            "side-f16-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, body_y),
            [
                (
                    "C",
                    (0.07 * length, body_y + 0.23 * body_depth),
                    (0.18 * length, body_y + 0.30 * body_depth),
                    (0.31 * length, body_y + 0.27 * body_depth),
                ),
                ("L", (0.91 * length, body_y + 0.10 * body_depth)),
                ("L", (length, body_y)),
                ("L", (0.89 * length, body_y - 0.15 * body_depth)),
                (
                    "C",
                    (0.63 * length, body_y - 0.30 * body_depth),
                    (0.20 * length, body_y - 0.25 * body_depth),
                    (0, body_y),
                ),
            ],
            closed=True,
            priority="identity",
        )
        side_identity = [
            _aircraft_polygon(
                "side-single-swept-fin",
                "fin",
                "principal_silhouette",
                [
                    (0.66 * length, body_y + 0.13 * body_depth),
                    (0.74 * length, 0.96 * height),
                    (0.84 * length, body_y + 0.08 * body_depth),
                ],
                priority="identity",
            ),
            _line(
                "side-cropped-delta-root",
                "wing",
                "major_structural_edges",
                [(0.37 * length, body_y), (0.68 * length, body_y - 0.08 * height)],
                priority="identity",
            ),
            _line(
                "side-all-moving-tailplane",
                "tailplane",
                "major_structural_edges",
                [
                    (0.73 * length, body_y + 0.02 * height),
                    (0.94 * length, body_y - 0.02 * height),
                ],
                priority="identity",
            ),
            _curve(
                "side-bubble-canopy",
                "cockpit",
                "glazing_openings",
                (0.20 * length, body_y + 0.20 * body_depth),
                [
                    (
                        "C",
                        (0.25 * length, body_y + 0.83 * body_depth),
                        (0.39 * length, body_y + 0.80 * body_depth),
                        (0.44 * length, body_y + 0.18 * body_depth),
                    )
                ],
                priority="identity",
            ),
            _curve(
                "side-ventral-intake",
                "intake",
                "principal_silhouette",
                (0.27 * length, body_y - 0.20 * body_depth),
                [
                    (
                        "C",
                        (0.30 * length, body_y - 0.64 * body_depth),
                        (0.39 * length, body_y - 0.64 * body_depth),
                        (0.43 * length, body_y - 0.23 * body_depth),
                    )
                ],
                priority="identity",
            ),
            _ellipse(
                "side-single-exhaust-ring",
                "exhaust",
                "mechanical_detail",
                (0.94 * length, body_y - 0.02 * height),
                0.022 * length,
                0.10 * height,
                priority="identity",
            ),
        ]
    elif is_f35:
        side_fuselage = _aircraft_polygon(
            "side-f35-faceted-fuselage",
            "fuselage",
            "principal_silhouette",
            [
                (0, body_y),
                (0.08 * length, body_y + 0.24 * body_depth),
                (0.34 * length, body_y + 0.34 * body_depth),
                (0.79 * length, body_y + 0.19 * body_depth),
                (length, body_y),
                (0.87 * length, body_y - 0.18 * body_depth),
                (0.43 * length, body_y - 0.34 * body_depth),
                (0.12 * length, body_y - 0.23 * body_depth),
            ],
            priority="identity",
        )
        side_identity = [
            _aircraft_polygon(
                "side-near-canted-fin",
                "left-fin",
                "principal_silhouette",
                [
                    (0.63 * length, body_y + 0.13 * body_depth),
                    (0.70 * length, 0.91 * height),
                    (0.79 * length, body_y + 0.09 * body_depth),
                ],
                priority="identity",
            ),
            _aircraft_polygon(
                "side-far-canted-fin",
                "right-fin",
                "major_structural_edges",
                [
                    (0.69 * length, body_y + 0.11 * body_depth),
                    (0.76 * length, 0.82 * height),
                    (0.84 * length, body_y + 0.07 * body_depth),
                ],
                priority="identity",
            ),
            _line(
                "side-trapezoid-wing-root",
                "wing",
                "major_structural_edges",
                [
                    (0.34 * length, body_y + 0.02 * height),
                    (0.70 * length, body_y - 0.08 * height),
                ],
                priority="identity",
            ),
            _line(
                "side-f35-tailplane",
                "tailplane",
                "major_structural_edges",
                [(0.70 * length, body_y), (0.94 * length, body_y - 0.04 * height)],
                priority="identity",
            ),
            _aircraft_polygon(
                "side-faceted-canopy",
                "cockpit",
                "glazing_openings",
                [
                    (0.19 * length, body_y + 0.18 * body_depth),
                    (0.25 * length, body_y + 0.74 * body_depth),
                    (0.37 * length, body_y + 0.68 * body_depth),
                    (0.41 * length, body_y + 0.17 * body_depth),
                ],
                priority="identity",
            ),
            _line(
                "side-near-intake",
                "intake",
                "major_structural_edges",
                [
                    (0.28 * length, body_y - 0.05 * body_depth),
                    (0.46 * length, body_y - 0.28 * body_depth),
                ],
                priority="identity",
            ),
            _line(
                "side-far-intake",
                "intake",
                "mechanical_detail",
                [
                    (0.31 * length, body_y + 0.04 * body_depth),
                    (0.45 * length, body_y - 0.15 * body_depth),
                ],
                priority="identity",
            ),
            _ellipse(
                "side-single-exhaust-ring",
                "exhaust",
                "mechanical_detail",
                (0.94 * length, body_y - 0.02 * height),
                0.023 * length,
                0.11 * height,
                priority="identity",
            ),
        ]
    else:
        side_fuselage = _curve(
            "side-typhoon-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, body_y),
            [
                (
                    "C",
                    (0.07 * length, body_y + 0.24 * body_depth),
                    (0.19 * length, body_y + 0.31 * body_depth),
                    (0.33 * length, body_y + 0.27 * body_depth),
                ),
                ("L", (0.91 * length, body_y + 0.10 * body_depth)),
                ("L", (length, body_y)),
                ("L", (0.90 * length, body_y - 0.19 * body_depth)),
                (
                    "C",
                    (0.62 * length, body_y - 0.35 * body_depth),
                    (0.19 * length, body_y - 0.25 * body_depth),
                    (0, body_y),
                ),
            ],
            closed=True,
            priority="identity",
        )
        side_identity = [
            _aircraft_polygon(
                "side-single-swept-fin",
                "fin",
                "principal_silhouette",
                [
                    (0.68 * length, body_y + 0.12 * body_depth),
                    (0.75 * length, 0.95 * height),
                    (0.84 * length, body_y + 0.08 * body_depth),
                ],
                priority="identity",
            ),
            _line(
                "side-full-delta-root",
                "wing",
                "major_structural_edges",
                [
                    (0.35 * length, body_y + 0.02 * height),
                    (0.79 * length, body_y - 0.09 * height),
                ],
                priority="identity",
            ),
            _line(
                "side-canard",
                "canard",
                "principal_silhouette",
                [
                    (
                        side_c.get("canard_station_x", 0.31) * length,
                        body_y + 0.06 * height,
                    ),
                    (0.43 * length, body_y + 0.02 * height),
                ],
                priority="identity",
            ),
            _curve(
                "side-raised-canopy",
                "cockpit",
                "glazing_openings",
                (0.20 * length, body_y + 0.19 * body_depth),
                [
                    (
                        "C",
                        (0.25 * length, body_y + 0.78 * body_depth),
                        (0.38 * length, body_y + 0.73 * body_depth),
                        (0.43 * length, body_y + 0.17 * body_depth),
                    )
                ],
                priority="identity",
            ),
            _aircraft_polygon(
                "side-central-intake",
                "intake",
                "principal_silhouette",
                [
                    (0.31 * length, body_y - 0.12 * body_depth),
                    (0.37 * length, body_y - 0.58 * body_depth),
                    (0.49 * length, body_y - 0.25 * body_depth),
                ],
                priority="identity",
            ),
            _ellipse(
                "side-left-exhaust",
                "exhaust",
                "mechanical_detail",
                (0.92 * length, body_y + 0.02 * height),
                0.022 * length,
                0.075 * height,
                priority="identity",
            ),
            _ellipse(
                "side-right-exhaust",
                "exhaust",
                "mechanical_detail",
                (0.94 * length, body_y - 0.055 * height),
                0.022 * length,
                0.075 * height,
                priority="identity",
            ),
        ]
    side = [
        side_fuselage,
        *side_identity,
        _line(
            "side-chine-datum",
            "fuselage",
            "panel_seam_lines",
            [
                (0.12 * length, body_y + 0.05 * body_depth),
                (0.82 * length, body_y - 0.02 * body_depth),
            ],
            minimum_sheet="A4",
        ),
        _line(
            "side-centre-line",
            "construction",
            "construction_geometry",
            [(0, body_y), (length, body_y)],
            line_style="centre",
            minimum_sheet="A3",
        ),
    ]

    front_half = front_c.get("fuselage_half_width_fraction", 0.09) * span
    wing_root_y = front_c.get("wing_root_y", 0.42) * height
    wing_tip_y = front_c.get("wing_tip_y", 0.48) * height
    if is_f16:
        front_fuselage = _aircraft_polygon(
            "front-f16-fuselage",
            "fuselage",
            "principal_silhouette",
            [
                (0, 0.83 * height),
                (-0.62 * front_half, 0.65 * height),
                (-front_half, 0.39 * height),
                (-0.55 * front_half, 0.19 * height),
                (0.55 * front_half, 0.19 * height),
                (front_half, 0.39 * height),
                (0.62 * front_half, 0.65 * height),
            ],
            priority="identity",
        )
        front_identity = [
            _ellipse(
                "front-ventral-intake",
                "intake",
                "principal_silhouette",
                (0, 0.19 * height),
                front_c.get("intake_half_width_fraction", 0.045) * span,
                0.075 * height,
                priority="identity",
            ),
            _line(
                "front-single-fin",
                "fin",
                "major_structural_edges",
                [(0, 0.57 * height), (0, 0.98 * height)],
                priority="identity",
            ),
        ]
    elif is_f35:
        front_fuselage = _aircraft_polygon(
            "front-f35-angular-fuselage",
            "fuselage",
            "principal_silhouette",
            [
                (0, 0.84 * height),
                (-0.55 * front_half, 0.69 * height),
                (-front_half, 0.43 * height),
                (-0.72 * front_half, 0.19 * height),
                (0.72 * front_half, 0.19 * height),
                (front_half, 0.43 * height),
                (0.55 * front_half, 0.69 * height),
            ],
            priority="identity",
        )
        intake_offset = front_c.get("intake_offset_fraction", 0.065) * span
        front_identity = [
            _aircraft_polygon(
                "front-left-side-intake",
                "intake",
                "major_structural_edges",
                [
                    (-intake_offset - 0.035 * span, 0.38 * height),
                    (-intake_offset, 0.27 * height),
                    (-intake_offset + 0.025 * span, 0.40 * height),
                ],
                priority="identity",
            ),
            _aircraft_polygon(
                "front-right-side-intake",
                "intake",
                "major_structural_edges",
                [
                    (intake_offset + 0.035 * span, 0.38 * height),
                    (intake_offset, 0.27 * height),
                    (intake_offset - 0.025 * span, 0.40 * height),
                ],
                priority="identity",
            ),
            _line(
                "front-left-canted-fin",
                "left-fin",
                "principal_silhouette",
                [(-0.07 * span, 0.54 * height), (-0.14 * span, 0.95 * height)],
                priority="identity",
            ),
            _line(
                "front-right-canted-fin",
                "right-fin",
                "principal_silhouette",
                [(0.07 * span, 0.54 * height), (0.14 * span, 0.95 * height)],
                priority="identity",
            ),
        ]
    else:
        front_fuselage = _aircraft_polygon(
            "front-typhoon-fuselage",
            "fuselage",
            "principal_silhouette",
            [
                (0, 0.84 * height),
                (-0.55 * front_half, 0.66 * height),
                (-front_half, 0.39 * height),
                (-0.60 * front_half, 0.18 * height),
                (0.60 * front_half, 0.18 * height),
                (front_half, 0.39 * height),
                (0.55 * front_half, 0.66 * height),
            ],
            priority="identity",
        )
        canard_offset = front_c.get("canard_offset_fraction", 0.13) * span
        front_identity = [
            _aircraft_polygon(
                "front-central-intake",
                "intake",
                "principal_silhouette",
                [
                    (
                        -front_c.get("intake_half_width_fraction", 0.065) * span,
                        0.33 * height,
                    ),
                    (0, 0.18 * height),
                    (
                        front_c.get("intake_half_width_fraction", 0.065) * span,
                        0.33 * height,
                    ),
                ],
                priority="identity",
            ),
            _line(
                "front-canards",
                "canard",
                "principal_silhouette",
                [
                    (-canard_offset, 0.58 * height),
                    (0, 0.54 * height),
                    (canard_offset, 0.58 * height),
                ],
                priority="identity",
            ),
            _line(
                "front-single-fin",
                "fin",
                "major_structural_edges",
                [(0, 0.55 * height), (0, 0.97 * height)],
                priority="identity",
            ),
        ]
    front: list[dict[str, Any]] = [
        front_fuselage,
        _line(
            "front-wing",
            "wing",
            "principal_silhouette",
            [
                (-span / 2, wing_tip_y),
                (-front_half, wing_root_y),
                (front_half, wing_root_y),
                (span / 2, wing_tip_y),
            ],
            priority="identity",
        ),
        *front_identity,
        _curve(
            "front-canopy",
            "cockpit",
            "glazing_openings",
            (-0.46 * front_half, 0.58 * height),
            [
                (
                    "C",
                    (-0.22 * front_half, 0.73 * height),
                    (0.22 * front_half, 0.73 * height),
                    (0.46 * front_half, 0.58 * height),
                )
            ],
            priority="identity",
        ),
        _line(
            "front-centre",
            "construction",
            "construction_geometry",
            [(0, 0.05 * height), (0, height)],
            line_style="centre",
            minimum_sheet="A3",
        ),
    ]
    return _aircraft_dimensioned_views(
        subject,
        dims,
        side=side,
        front=front,
        plan=plan,
    )


def _aircraft_airliner_identity_details(
    subject: dict[str, Any],
    *,
    length: float,
    span: float,
    height: float,
    body_y: float,
    body_depth: float,
    wing_y: float,
    tip_y: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return small type-recognition cues without copying proprietary drawings."""

    subject_id = str(subject["id"]).casefold()
    side: list[dict[str, Any]] = []
    front: list[dict[str, Any]] = []
    plan: list[dict[str, Any]] = []
    if subject_id == "airbus-a320neo":
        side.append(
            _line(
                "side-sharklet-profile",
                "wingtip-device",
                "major_structural_edges",
                [
                    (0.61 * length, body_y - 0.01 * height),
                    (0.625 * length, body_y + 0.12 * height),
                ],
                priority="identity",
            )
        )
        front.extend(
            [
                _line(
                    "front-left-sharklet",
                    "wingtip-device",
                    "major_structural_edges",
                    [(-span / 2, tip_y), (-0.485 * span, tip_y + 0.12 * height)],
                    priority="identity",
                ),
                _line(
                    "front-right-sharklet",
                    "wingtip-device",
                    "major_structural_edges",
                    [(span / 2, tip_y), (0.485 * span, tip_y + 0.12 * height)],
                    priority="identity",
                ),
            ]
        )
        plan.extend(
            [
                _line(
                    "plan-left-sharklet",
                    "wingtip-device",
                    "major_structural_edges",
                    [(-span / 2, 0.48 * length), (-0.485 * span, 0.55 * length)],
                    priority="identity",
                ),
                _line(
                    "plan-right-sharklet",
                    "wingtip-device",
                    "major_structural_edges",
                    [(span / 2, 0.48 * length), (0.485 * span, 0.55 * length)],
                    priority="identity",
                ),
            ]
        )
    elif subject_id == "boeing-737-8":
        side.extend(
            [
                _line(
                    "side-flattened-nacelle-chine",
                    "propulsion",
                    "major_structural_edges",
                    [
                        (0.405 * length, body_y - 0.22 * height),
                        (0.53 * length, body_y - 0.22 * height),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-split-winglet-profile",
                    "wingtip-device",
                    "major_structural_edges",
                    [
                        (0.62 * length, body_y - 0.02 * height),
                        (0.635 * length, body_y + 0.12 * height),
                        (0.645 * length, body_y - 0.08 * height),
                    ],
                    priority="identity",
                ),
            ]
        )
        for side_name, sign in (("left", -1.0), ("right", 1.0)):
            front.append(
                _line(
                    f"front-{side_name}-split-winglet",
                    "wingtip-device",
                    "major_structural_edges",
                    [
                        (sign * span / 2, tip_y),
                        (sign * 0.485 * span, tip_y + 0.12 * height),
                        (sign * 0.49 * span, tip_y - 0.07 * height),
                    ],
                    priority="identity",
                )
            )
            plan.append(
                _line(
                    f"plan-{side_name}-split-winglet",
                    "wingtip-device",
                    "major_structural_edges",
                    [
                        (sign * span / 2, 0.50 * length),
                        (sign * 0.485 * span, 0.55 * length),
                        (sign * 0.49 * span, 0.46 * length),
                    ],
                    priority="identity",
                )
            )
    elif subject_id == "airbus-a350-900":
        side.append(
            _curve(
                "side-mask-cockpit-outline",
                "cockpit",
                "glazing_openings",
                (0.04 * length, body_y + 0.25 * body_depth),
                [
                    (
                        "C",
                        (0.065 * length, body_y + 0.57 * body_depth),
                        (0.115 * length, body_y + 0.53 * body_depth),
                        (0.14 * length, body_y + 0.26 * body_depth),
                    )
                ],
                priority="identity",
            )
        )
        for side_name, sign in (("left", -1.0), ("right", 1.0)):
            front.append(
                _curve(
                    f"front-{side_name}-curved-wingtip",
                    "wingtip",
                    "major_structural_edges",
                    (sign * span / 2, tip_y),
                    [
                        (
                            "C",
                            (sign * 0.495 * span, tip_y + 0.02 * height),
                            (sign * 0.485 * span, tip_y + 0.10 * height),
                            (sign * 0.475 * span, tip_y + 0.13 * height),
                        )
                    ],
                    priority="identity",
                )
            )
            plan.append(
                _curve(
                    f"plan-{side_name}-curved-wingtip",
                    "wingtip",
                    "major_structural_edges",
                    (sign * span / 2, 0.52 * length),
                    [
                        (
                            "C",
                            (sign * 0.49 * span, 0.56 * length),
                            (sign * 0.47 * span, 0.61 * length),
                            (sign * 0.44 * span, 0.63 * length),
                        )
                    ],
                    priority="identity",
                )
            )
    elif subject_id == "boeing-787-9":
        side.extend(
            [
                _line(
                    "side-nacelle-chevron",
                    "propulsion",
                    "mechanical_detail",
                    [
                        (0.515 * length, body_y - 0.18 * height),
                        (0.525 * length, body_y - 0.14 * height),
                        (0.535 * length, body_y - 0.18 * height),
                        (0.545 * length, body_y - 0.14 * height),
                    ],
                    priority="identity",
                ),
                _curve(
                    "side-four-pane-cockpit",
                    "cockpit",
                    "glazing_openings",
                    (0.045 * length, body_y + 0.25 * body_depth),
                    [
                        (
                            "C",
                            (0.07 * length, body_y + 0.54 * body_depth),
                            (0.115 * length, body_y + 0.50 * body_depth),
                            (0.14 * length, body_y + 0.25 * body_depth),
                        )
                    ],
                    priority="identity",
                ),
            ]
        )
        for side_name, sign in (("left", -1.0), ("right", 1.0)):
            front.append(
                _curve(
                    f"front-{side_name}-flexed-raked-tip",
                    "wingtip",
                    "major_structural_edges",
                    (sign * span / 2, tip_y),
                    [
                        (
                            "C",
                            (sign * 0.49 * span, tip_y + 0.04 * height),
                            (sign * 0.47 * span, tip_y + 0.15 * height),
                            (sign * 0.44 * span, tip_y + 0.18 * height),
                        )
                    ],
                    priority="identity",
                )
            )
            plan.append(
                _line(
                    f"plan-{side_name}-raked-wingtip",
                    "wingtip",
                    "major_structural_edges",
                    [
                        (sign * span / 2, 0.54 * length),
                        (sign * 0.43 * span, 0.66 * length),
                    ],
                    priority="identity",
                )
            )
    elif subject_id == "airbus-a380-800":
        front.append(
            _curve(
                "front-double-deck-crown",
                "upper-deck",
                "glazing_openings",
                (-0.035 * span, 0.62 * height),
                [
                    (
                        "C",
                        (-0.018 * span, 0.76 * height),
                        (0.018 * span, 0.76 * height),
                        (0.035 * span, 0.62 * height),
                    )
                ],
                priority="identity",
            )
        )
        plan.append(
            _line(
                "plan-full-length-upper-deck-datum",
                "upper-deck",
                "major_structural_edges",
                [(-0.015 * span, 0.10 * length), (-0.015 * span, 0.89 * length)],
                priority="identity",
            )
        )
    elif subject_id == "boeing-747-8-intercontinental":
        front.append(
            _ellipse(
                "front-upper-deck-crown",
                "upper-deck",
                "major_structural_edges",
                (0, 0.69 * height),
                0.027 * span,
                0.10 * height,
                priority="identity",
            )
        )
        plan.append(
            _aircraft_polygon(
                "plan-forward-upper-deck-envelope",
                "upper-deck",
                "major_structural_edges",
                [
                    (-0.018 * span, 0.08 * length),
                    (-0.025 * span, 0.28 * length),
                    (0, 0.39 * length),
                    (0.025 * span, 0.28 * length),
                    (0.018 * span, 0.08 * length),
                ],
                priority="identity",
            )
        )
    else:
        _fail(f"airliner subject {subject.get('id')!r} has no identity detail recipe.")
    return side, front, plan


def _aircraft_views(
    subject: dict[str, Any], dims: dict[str, float | None]
) -> list[dict[str, Any]]:
    family = _aircraft_family(subject)
    if family == "wwii-single-seat":
        return _aircraft_wwii_single_views(subject, dims)
    if family == "lancaster-bomber":
        return _aircraft_lancaster_views(subject, dims)
    if family == "modern-jet":
        return _aircraft_modern_jet_views(subject, dims)
    if family != "airliner":
        _fail(f"aircraft subject {subject.get('id')!r} has no geometry family.")
    length = float(dims["length"])
    span = float(dims["wingspan"])
    known_height = dims.get("height")
    height = float(known_height or length * 0.24)
    template = _template(subject).casefold()
    plan_c = _view_controls(subject, "plan")
    side_c = _view_controls(subject, "side")
    front_c = _view_controls(subject, "front")
    is_prop = any(
        word in template for word in ("prop", "piston", "wwii", "bomber", "taildragger")
    ) or any(word in str(subject["id"]) for word in ("spitfire", "p51"))
    is_fighter = "fighter" in template or "interceptor" in template
    engine_count = (
        4
        if any(
            word in template
            for word in ("four-engine", "four-engined", "a380", "747", "lancaster")
        )
        else 2
    )
    if is_prop and is_fighter:
        engine_count = 1
    elif "single-engine" in template or "f-16" in subject["id"]:
        engine_count = 1
    fuselage_half = plan_c.get("fuselage_half_width_fraction", 0.045) * span
    wing_root_lead = plan_c.get("wing_root_leading_y", 0.33) * length
    wing_tip_lead = plan_c.get("wing_tip_leading_y", 0.49) * length
    wing_root_trail = plan_c.get("wing_root_trailing_y", 0.61) * length
    wing_tip_trail = plan_c.get("wing_tip_trailing_y", 0.57) * length
    if is_fighter:
        wing_root_lead, wing_tip_lead = 0.28 * length, 0.49 * length
        wing_root_trail, wing_tip_trail = 0.72 * length, 0.60 * length
    if "elliptical-wing" in template:
        wing_primitive = _curve(
            "plan-wing-envelope",
            "wing",
            "principal_silhouette",
            (-fuselage_half, wing_root_lead),
            [
                (
                    "C",
                    (-0.20 * span, 0.30 * length),
                    (-0.48 * span, 0.41 * length),
                    (-span / 2.0, 0.50 * length),
                ),
                (
                    "C",
                    (-0.48 * span, 0.59 * length),
                    (-0.20 * span, 0.68 * length),
                    (-fuselage_half, wing_root_trail),
                ),
                ("L", (fuselage_half, wing_root_trail)),
                (
                    "C",
                    (0.20 * span, 0.68 * length),
                    (0.48 * span, 0.59 * length),
                    (span / 2.0, 0.50 * length),
                ),
                (
                    "C",
                    (0.48 * span, 0.41 * length),
                    (0.20 * span, 0.30 * length),
                    (fuselage_half, wing_root_lead),
                ),
                ("L", (-fuselage_half, wing_root_lead)),
            ],
            closed=True,
            priority="identity",
        )
    else:
        wing_primitive = _line(
            "plan-wing-envelope",
            "wing",
            "principal_silhouette",
            [
                (-fuselage_half, wing_root_lead),
                (-span / 2.0, wing_tip_lead),
                (-span / 2.0, wing_tip_trail),
                (-fuselage_half, wing_root_trail),
                (fuselage_half, wing_root_trail),
                (span / 2.0, wing_tip_trail),
                (span / 2.0, wing_tip_lead),
                (fuselage_half, wing_root_lead),
            ],
            priority="identity",
        )
    plan: list[dict[str, Any]] = [
        _curve(
            "plan-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, 0),
            [
                (
                    "C",
                    (-fuselage_half, 0.05 * length),
                    (-fuselage_half, 0.83 * length),
                    (-0.45 * fuselage_half, 0.94 * length),
                ),
                (
                    "C",
                    (-0.20 * fuselage_half, 0.985 * length),
                    (-0.10 * fuselage_half, length),
                    (0, length),
                ),
                (
                    "C",
                    (0.10 * fuselage_half, length),
                    (0.20 * fuselage_half, 0.985 * length),
                    (0.45 * fuselage_half, 0.94 * length),
                ),
                (
                    "C",
                    (fuselage_half, 0.83 * length),
                    (fuselage_half, 0.05 * length),
                    (0, 0),
                ),
            ],
            closed=True,
            priority="identity",
        ),
        wing_primitive,
        _line(
            "plan-tailplane",
            "tailplane",
            "major_structural_edges",
            [
                (-0.55 * fuselage_half, 0.81 * length),
                (-0.19 * span, 0.89 * length),
                (-0.16 * span, 0.93 * length),
                (0.16 * span, 0.93 * length),
                (0.19 * span, 0.89 * length),
                (0.55 * fuselage_half, 0.81 * length),
            ],
            priority="identity",
        ),
        _line(
            "plan-centre-line",
            "construction",
            "construction_geometry",
            [(0, 0), (0, length)],
            line_style="centre",
            minimum_sheet="A3",
        ),
        _line(
            "plan-left-aileron",
            "flight-controls",
            "panel_seam_lines",
            [(-0.12 * span, 0.56 * length), (-0.42 * span, 0.56 * length)],
            minimum_sheet="A4",
        ),
        _line(
            "plan-right-aileron",
            "flight-controls",
            "panel_seam_lines",
            [(0.12 * span, 0.56 * length), (0.42 * span, 0.56 * length)],
            minimum_sheet="A4",
        ),
        _line(
            "plan-left-spar",
            "wing",
            "mechanical_detail",
            [(-0.08 * span, 0.47 * length), (-0.43 * span, 0.53 * length)],
            minimum_sheet="A3",
        ),
        _line(
            "plan-right-spar",
            "wing",
            "mechanical_detail",
            [(0.08 * span, 0.47 * length), (0.43 * span, 0.53 * length)],
            minimum_sheet="A3",
        ),
    ]
    if engine_count > 1:
        offsets = [
            span * factor
            for factor in (
                (-0.28, 0.28) if engine_count == 2 else (-0.36, -0.18, 0.18, 0.36)
            )
        ]
        for index, offset in enumerate(offsets, start=1):
            plan.append(
                _ellipse(
                    f"plan-engine-{index}",
                    "propulsion",
                    "mechanical_detail",
                    (offset, plan_c.get("engine_station_y", 0.48) * length),
                    0.035 * span,
                    0.09 * length,
                    minimum_sheet="A4",
                )
            )

    body_depth = side_c.get("body_depth_fraction", 0.30) * height
    body_y = height * 0.35
    side: list[dict[str, Any]] = [
        _curve(
            "side-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, body_y),
            [
                (
                    "C",
                    (0.02 * length, body_y + 0.45 * body_depth),
                    (0.08 * length, body_y + 0.55 * body_depth),
                    (0.15 * length, body_y + 0.50 * body_depth),
                ),
                ("L", (0.88 * length, body_y + 0.35 * body_depth)),
                (
                    "C",
                    (0.95 * length, body_y + 0.25 * body_depth),
                    (0.99 * length, body_y + 0.08 * body_depth),
                    (length, body_y),
                ),
                (
                    "C",
                    (0.98 * length, body_y - 0.18 * body_depth),
                    (0.91 * length, body_y - 0.32 * body_depth),
                    (0.80 * length, body_y - 0.35 * body_depth),
                ),
                ("L", (0.12 * length, body_y - 0.45 * body_depth)),
                (
                    "C",
                    (0.05 * length, body_y - 0.40 * body_depth),
                    (0.01 * length, body_y - 0.18 * body_depth),
                    (0, body_y),
                ),
            ],
            closed=True,
            priority="identity",
        ),
        _line(
            "side-fin",
            "fin",
            "principal_silhouette",
            [
                (0.82 * length, body_y + 0.32 * body_depth),
                (0.89 * length, 0.96 * height),
                (0.95 * length, body_y + 0.28 * body_depth),
            ],
            priority="identity",
        ),
        _line(
            "side-wing-root",
            "wing",
            "major_structural_edges",
            [(0.34 * length, body_y), (0.63 * length, body_y - 0.05 * height)],
            priority="identity",
        ),
        _line(
            "side-tailplane",
            "tailplane",
            "major_structural_edges",
            [
                (0.79 * length, body_y + 0.08 * body_depth),
                (0.96 * length, body_y + 0.04 * body_depth),
            ],
            priority="identity",
        ),
        _curve(
            "side-cockpit",
            "cockpit",
            "glazing_openings",
            (0.055 * length, body_y + 0.26 * body_depth),
            [
                (
                    "C",
                    (0.08 * length, body_y + 0.53 * body_depth),
                    (0.13 * length, body_y + 0.50 * body_depth),
                    (0.16 * length, body_y + 0.31 * body_depth),
                )
            ],
            priority="identity",
        ),
        _line(
            "side-window-datum",
            "cabin",
            "glazing_openings",
            [
                (0.18 * length, body_y + 0.22 * body_depth),
                (0.77 * length, body_y + 0.19 * body_depth),
            ],
            priority="identity",
        ),
        _line(
            "side-datum",
            "construction",
            "construction_geometry",
            [(0, body_y), (length, body_y)],
            line_style="centre",
            minimum_sheet="A3",
        ),
    ]
    if is_prop:
        side.extend(
            [
                _ellipse(
                    "side-propeller-disc",
                    "propulsion",
                    "mechanical_detail",
                    (0.045 * length, body_y),
                    0.012 * length,
                    0.31 * height,
                    priority="identity",
                ),
                _line(
                    "side-propeller-axis",
                    "propulsion",
                    "mechanical_detail",
                    [(0.01 * length, body_y), (0.08 * length, body_y)],
                    priority="identity",
                ),
            ]
        )
    else:
        visible_engine_count = 2 if engine_count == 4 else 1
        engine_station = side_c.get("engine_axis_x", 0.47)
        for index in range(visible_engine_count):
            side.append(
                _ellipse(
                    f"side-engine-nacelle-{index + 1}",
                    "propulsion",
                    "mechanical_detail",
                    (
                        (engine_station + index * 0.10) * length,
                        body_y - (0.20 + index * 0.02) * height,
                    ),
                    0.065 * length,
                    0.085 * height,
                    minimum_sheet="A4",
                )
            )
    window_count = 12 if not is_fighter else 2
    for index in range(window_count):
        x = (0.20 + index * (0.55 / max(window_count - 1, 1))) * length
        side.append(
            _line(
                f"side-window-{index + 1}",
                "cabin",
                "glazing_openings",
                [
                    (x, body_y + 0.22 * body_depth),
                    (x + 0.008 * length, body_y + 0.22 * body_depth),
                ],
                minimum_sheet="A3",
            )
        )
    if not is_fighter:
        for index, station in enumerate((0.17, 0.34, 0.68, 0.78), start=1):
            side.append(
                _line(
                    f"side-door-{index}",
                    "cabin",
                    "panel_seam_lines",
                    [
                        (station * length, body_y - 0.12 * body_depth),
                        (station * length, body_y + 0.30 * body_depth),
                    ],
                    minimum_sheet="A4",
                )
            )
    if "a380" in str(subject["id"]):
        side.append(
            _line(
                "side-upper-deck-window-datum",
                "upper-deck",
                "glazing_openings",
                [
                    (0.13 * length, body_y + 0.37 * body_depth),
                    (0.78 * length, body_y + 0.33 * body_depth),
                ],
                priority="identity",
            )
        )
    elif "747" in str(subject["id"]):
        side.append(
            _curve(
                "side-upper-deck-hump",
                "upper-deck",
                "major_structural_edges",
                (0.08 * length, body_y + 0.48 * body_depth),
                [
                    (
                        "C",
                        (0.15 * length, body_y + 0.72 * body_depth),
                        (0.27 * length, body_y + 0.66 * body_depth),
                        (0.33 * length, body_y + 0.48 * body_depth),
                    )
                ],
                priority="identity",
            )
        )
    if not is_prop:
        for index, station in enumerate((0.30, 0.66), start=1):
            side.extend(
                [
                    _line(
                        f"side-gear-strut-{index}",
                        "landing-gear",
                        "mechanical_detail",
                        [
                            (station * length, body_y - 0.30 * body_depth),
                            (station * length, 0.12 * height),
                        ],
                        minimum_sheet="A3",
                    ),
                    _circle(
                        f"side-gear-wheel-{index}",
                        "landing-gear",
                        "mechanical_detail",
                        (station * length, 0.10 * height),
                        0.025 * height,
                        minimum_sheet="A3",
                    ),
                ]
            )

    front_fuselage_half = front_c.get("fuselage_half_width_fraction", 0.055) * span
    wing_y = front_c.get("wing_root_y", 0.42) * height
    tip_y = front_c.get("wing_tip_y", 0.50) * height
    front: list[dict[str, Any]] = [
        _ellipse(
            "front-fuselage",
            "fuselage",
            "principal_silhouette",
            (0, 0.45 * height),
            front_fuselage_half,
            0.38 * height,
            priority="identity",
        ),
        _line(
            "front-wing",
            "wing",
            "principal_silhouette",
            [
                (-span / 2.0, tip_y),
                (-front_fuselage_half, wing_y),
                (front_fuselage_half, wing_y),
                (span / 2.0, tip_y),
            ],
            priority="identity",
        ),
        _line(
            "front-fin",
            "fin",
            "major_structural_edges",
            [(0, 0.52 * height), (0, 0.98 * height)],
            priority="identity",
        ),
        _line(
            "front-centre",
            "construction",
            "construction_geometry",
            [(0, 0.05 * height), (0, height)],
            line_style="centre",
            minimum_sheet="A3",
        ),
        _curve(
            "front-cockpit-glazing",
            "cockpit",
            "glazing_openings",
            (-0.52 * front_fuselage_half, 0.61 * height),
            [
                (
                    "C",
                    (-0.25 * front_fuselage_half, 0.72 * height),
                    (0.25 * front_fuselage_half, 0.72 * height),
                    (0.52 * front_fuselage_half, 0.61 * height),
                )
            ],
            priority="identity",
        ),
    ]
    if engine_count > 1:
        offsets = [
            span * factor
            for factor in (
                (-0.28, 0.28) if engine_count == 2 else (-0.36, -0.18, 0.18, 0.36)
            )
        ]
        for index, offset in enumerate(offsets, start=1):
            front.append(
                _circle(
                    f"front-engine-{index}",
                    "propulsion",
                    "mechanical_detail",
                    (offset, wing_y - 0.08 * height),
                    0.035 * span,
                    minimum_sheet="A4",
                )
            )
    elif is_prop:
        front.append(
            _circle(
                "front-propeller-disc",
                "propulsion",
                "mechanical_detail",
                (0, 0.45 * height),
                0.31 * height,
                priority="identity",
            )
        )

    side_identity, front_identity, plan_identity = _aircraft_airliner_identity_details(
        subject,
        length=length,
        span=span,
        height=height,
        body_y=body_y,
        body_depth=body_depth,
        wing_y=wing_y,
        tip_y=tip_y,
    )
    side.extend(side_identity)
    front.extend(front_identity)
    plan.extend(plan_identity)
    return _aircraft_dimensioned_views(
        subject,
        dims,
        side=side,
        front=front,
        plan=plan,
    )


_BOAT_IDENTITY_SUBJECT_IDS = frozenset(
    {
        "riva-aquarama-special",
        "bluebird-k7",
        "rnli-shannon-class",
        "uscg-47-foot-motor-lifeboat",
        "ac75-foiling-monohull",
        "j-class-endeavour",
        "pt-109",
        "cutty-sark",
        "ss-great-britain",
        "rms-queen-mary",
    }
)


def _boat_closed_line(
    identifier: str,
    component: str,
    semantic: str,
    points: Sequence[tuple[float, float]],
    **kwargs: Any,
) -> dict[str, Any]:
    """Return a plotter-safe closed polyline for an external boat feature."""

    values = list(points)
    if not values:
        _fail(f"boat feature {identifier!r} has no points.")
    if values[0] != values[-1]:
        values.append(values[0])
    return _line(identifier, component, semantic, values, **kwargs)


def _boat_side_identity_details(
    subject_id: str,
    length: float,
    beam: float,
    waterline: float,
) -> list[dict[str, Any]]:
    """Authored external details that prevent the ten boats sharing one body."""

    deck = max(1.24 * waterline, 0.052 * length)
    details: list[dict[str, Any]] = []

    if subject_id == "riva-aquarama-special":
        details.extend(
            [
                _curve(
                    "side-riva-wraparound-windscreen",
                    "windscreen",
                    "glazing_openings",
                    (0.29 * length, deck),
                    [
                        (
                            "C",
                            (0.31 * length, 0.125 * length),
                            (0.40 * length, 0.145 * length),
                            (0.47 * length, 0.118 * length),
                        ),
                        ("L", (0.50 * length, deck)),
                    ],
                    priority="identity",
                ),
                _curve(
                    "side-riva-open-cockpit",
                    "cockpit",
                    "major_structural_edges",
                    (0.38 * length, 0.103 * length),
                    [
                        (
                            "C",
                            (0.48 * length, 0.080 * length),
                            (0.60 * length, 0.080 * length),
                            (0.67 * length, 0.101 * length),
                        )
                    ],
                    priority="identity",
                ),
                _line(
                    "side-riva-sunpad",
                    "aft-deck",
                    "major_structural_edges",
                    [
                        (0.67 * length, 0.101 * length),
                        (0.91 * length, 0.080 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-riva-chine",
                    "hull",
                    "panel_seam_lines",
                    [
                        (0.08 * length, 0.045 * length),
                        (0.34 * length, 0.018 * length),
                        (0.89 * length, 0.022 * length),
                    ],
                    minimum_sheet="A4",
                ),
                _boat_closed_line(
                    "side-riva-integrated-stern-step",
                    "stern",
                    "mechanical_detail",
                    [
                        (0.91 * length, 0.054 * length),
                        (0.99 * length, 0.048 * length),
                        (0.99 * length, 0.025 * length),
                        (0.93 * length, 0.025 * length),
                    ],
                    minimum_sheet="A4",
                ),
            ]
        )
        for index, station in enumerate((0.72, 0.77, 0.82, 0.87), start=1):
            details.append(
                _line(
                    f"side-riva-engine-vent-{index}",
                    "engine-bay",
                    "mechanical_detail",
                    [
                        (station * length, 0.060 * length),
                        ((station + 0.025) * length, 0.056 * length),
                    ],
                    minimum_sheet="A3",
                )
            )
        return details

    if subject_id == "bluebird-k7":
        details.extend(
            [
                _ellipse(
                    "side-bluebird-canopy",
                    "cockpit",
                    "glazing_openings",
                    (0.50 * length, 0.115 * length),
                    0.085 * length,
                    0.043 * length,
                    rotation_deg=-5.0,
                    priority="identity",
                ),
                _boat_closed_line(
                    "side-bluebird-tail-fin",
                    "tail",
                    "accent_feature",
                    [
                        (0.78 * length, 0.080 * length),
                        (0.84 * length, 0.205 * length),
                        (0.91 * length, 0.075 * length),
                    ],
                    priority="identity",
                ),
                _ellipse(
                    "side-bluebird-engine-intake",
                    "engine",
                    "mechanical_detail",
                    (0.68 * length, 0.092 * length),
                    0.027 * length,
                    0.019 * length,
                    minimum_sheet="A4",
                ),
                _boat_closed_line(
                    "side-bluebird-jet-nozzle",
                    "jet",
                    "accent_feature",
                    [
                        (0.91 * length, 0.082 * length),
                        (0.995 * length, 0.067 * length),
                        (0.995 * length, 0.040 * length),
                        (0.91 * length, 0.049 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-bluebird-forward-sponson-runner",
                    "hydroplane",
                    "principal_silhouette",
                    [
                        (0.15 * length, 0.015 * length),
                        (0.50 * length, 0.006 * length),
                        (0.61 * length, 0.022 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-bluebird-tailplane",
                    "tail",
                    "major_structural_edges",
                    [
                        (0.77 * length, 0.105 * length),
                        (0.95 * length, 0.100 * length),
                    ],
                    minimum_sheet="A4",
                ),
            ]
        )
        for index, station in enumerate((0.24, 0.40, 0.58, 0.74), start=1):
            details.append(
                _line(
                    f"side-bluebird-panel-station-{index}",
                    "body",
                    "panel_seam_lines",
                    [
                        (station * length, 0.035 * length),
                        (station * length, 0.070 * length),
                    ],
                    minimum_sheet="A3",
                )
            )
        return details

    if subject_id == "rnli-shannon-class":
        details.extend(
            [
                _boat_closed_line(
                    "side-shannon-wheelhouse",
                    "wheelhouse",
                    "principal_silhouette",
                    [
                        (0.29 * length, deck),
                        (0.35 * length, 0.205 * length),
                        (0.63 * length, 0.195 * length),
                        (0.70 * length, deck),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "side-shannon-upper-helm",
                    "upper-helm",
                    "accent_feature",
                    [
                        (0.43 * length, 0.205 * length),
                        (0.46 * length, 0.252 * length),
                        (0.58 * length, 0.248 * length),
                        (0.61 * length, 0.202 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-shannon-roof-rail",
                    "rails",
                    "mechanical_detail",
                    [
                        (0.38 * length, 0.266 * length),
                        (0.64 * length, 0.258 * length),
                    ],
                    minimum_sheet="A4",
                ),
                _line(
                    "side-shannon-mast",
                    "antenna-mast",
                    "major_structural_edges",
                    [
                        (0.53 * length, 0.251 * length),
                        (0.53 * length, 0.315 * length),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "side-shannon-aft-working-deck",
                    "working-deck",
                    "panel_seam_lines",
                    [
                        (0.72 * length, deck),
                        (0.94 * length, 0.060 * length),
                        (0.94 * length, 0.085 * length),
                        (0.74 * length, 0.090 * length),
                    ],
                    minimum_sheet="A4",
                ),
                _line(
                    "side-shannon-waterjet",
                    "waterjet",
                    "accent_feature",
                    [
                        (0.90 * length, 0.020 * length),
                        (1.01 * length, 0.018 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-shannon-spray-rail",
                    "hull",
                    "major_structural_edges",
                    [
                        (0.05 * length, 0.088 * length),
                        (0.34 * length, 0.052 * length),
                        (0.92 * length, 0.048 * length),
                    ],
                    minimum_sheet="A4",
                ),
            ]
        )
        for index in range(4):
            x0 = (0.36 + index * 0.065) * length
            details.append(
                _boat_closed_line(
                    f"side-shannon-window-{index + 1}",
                    "wheelhouse",
                    "glazing_openings",
                    [
                        (x0, 0.150 * length),
                        (x0 + 0.045 * length, 0.153 * length),
                        (x0 + 0.040 * length, 0.187 * length),
                        (x0 + 0.005 * length, 0.190 * length),
                    ],
                    minimum_sheet="A4",
                )
            )
        return details

    if subject_id == "uscg-47-foot-motor-lifeboat":
        details.extend(
            [
                _boat_closed_line(
                    "side-uscg-pilothouse",
                    "pilothouse",
                    "principal_silhouette",
                    [
                        (0.34 * length, deck),
                        (0.39 * length, 0.225 * length),
                        (0.66 * length, 0.218 * length),
                        (0.72 * length, deck),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-uscg-protective-rail",
                    "rails",
                    "major_structural_edges",
                    [
                        (0.29 * length, 0.244 * length),
                        (0.69 * length, 0.242 * length),
                        (0.75 * length, 0.190 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-uscg-aft-working-deck-rail",
                    "rails",
                    "mechanical_detail",
                    [
                        (0.72 * length, 0.095 * length),
                        (0.95 * length, 0.086 * length),
                    ],
                    minimum_sheet="A4",
                ),
                _line(
                    "side-uscg-mast",
                    "mast",
                    "major_structural_edges",
                    [
                        (0.55 * length, 0.240 * length),
                        (0.55 * length, 0.325 * length),
                    ],
                    priority="identity",
                ),
                _ellipse(
                    "side-uscg-radar",
                    "mast",
                    "mechanical_detail",
                    (0.55 * length, 0.307 * length),
                    0.047 * length,
                    0.008 * length,
                    minimum_sheet="A4",
                ),
                _line(
                    "side-uscg-deep-v-chine",
                    "hull",
                    "major_structural_edges",
                    [
                        (0.04 * length, 0.082 * length),
                        (0.32 * length, 0.035 * length),
                        (0.94 * length, 0.038 * length),
                    ],
                    minimum_sheet="A4",
                ),
                _line(
                    "side-uscg-twin-screw",
                    "propulsion",
                    "accent_feature",
                    [
                        (0.83 * length, 0.015 * length),
                        (0.98 * length, -0.005 * length),
                    ],
                    priority="identity",
                ),
            ]
        )
        for index in range(4):
            x0 = (0.405 + index * 0.06) * length
            details.append(
                _boat_closed_line(
                    f"side-uscg-window-{index + 1}",
                    "pilothouse",
                    "glazing_openings",
                    [
                        (x0, 0.158 * length),
                        (x0 + 0.040 * length, 0.158 * length),
                        (x0 + 0.036 * length, 0.201 * length),
                        (x0 + 0.004 * length, 0.201 * length),
                    ],
                    minimum_sheet="A4",
                )
            )
        for index, station in enumerate((0.32, 0.40, 0.48, 0.64, 0.72), start=1):
            details.append(
                _line(
                    f"side-uscg-rail-stanchion-{index}",
                    "rails",
                    "mechanical_detail",
                    [
                        (station * length, 0.215 * length),
                        (station * length, 0.244 * length),
                    ],
                    minimum_sheet="A3",
                )
            )
        return details

    if subject_id == "pt-109":
        details.extend(
            [
                _boat_closed_line(
                    "side-pt109-deckhouse",
                    "deckhouse",
                    "principal_silhouette",
                    [
                        (0.32 * length, deck),
                        (0.38 * length, 0.145 * length),
                        (0.59 * length, 0.138 * length),
                        (0.65 * length, deck),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "side-pt109-cockpit",
                    "cockpit",
                    "major_structural_edges",
                    [
                        (0.58 * length, deck),
                        (0.60 * length, 0.102 * length),
                        (0.72 * length, 0.098 * length),
                        (0.75 * length, deck),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-pt109-mast",
                    "mast",
                    "major_structural_edges",
                    [
                        (0.49 * length, 0.14 * length),
                        (0.49 * length, 0.225 * length),
                    ],
                    minimum_sheet="A4",
                ),
                _line(
                    "side-pt109-hard-chine",
                    "hull",
                    "major_structural_edges",
                    [
                        (0.04 * length, 0.060 * length),
                        (0.35 * length, 0.025 * length),
                        (0.96 * length, 0.028 * length),
                    ],
                    priority="identity",
                ),
            ]
        )
        for index, offset in enumerate((0.0, 0.014, 0.030, 0.044), start=1):
            details.append(
                _boat_closed_line(
                    f"side-pt109-torpedo-tube-{index}",
                    "torpedo-tube",
                    "mechanical_detail",
                    [
                        ((0.22 + offset) * length, (0.070 + offset) * length),
                        ((0.64 + offset) * length, (0.064 + offset) * length),
                        ((0.66 + offset) * length, (0.074 + offset) * length),
                        ((0.24 + offset) * length, (0.081 + offset) * length),
                    ],
                    minimum_sheet="A4",
                )
            )
        for index, station in enumerate((0.20, 0.79), start=1):
            details.append(
                _ellipse(
                    f"side-pt109-gun-ring-{index}",
                    "gun-platform",
                    "accent_feature",
                    (station * length, 0.102 * length),
                    0.036 * length,
                    0.011 * length,
                    priority="identity",
                )
            )
        for index, station in enumerate((0.72, 0.79, 0.86), start=1):
            details.append(
                _line(
                    f"side-pt109-engine-vent-{index}",
                    "engine-bay",
                    "panel_seam_lines",
                    [
                        (station * length, 0.054 * length),
                        ((station + 0.045) * length, 0.052 * length),
                    ],
                    minimum_sheet="A3",
                )
            )
        return details

    if subject_id == "ac75-foiling-monohull":
        mast_x = 0.46 * length
        mast_top = 0.61 * length
        details.extend(
            [
                _line(
                    "side-ac75-mast",
                    "rig",
                    "major_structural_edges",
                    [(mast_x, deck), (mast_x, mast_top)],
                    priority="identity",
                ),
                _curve(
                    "side-ac75-twin-skin-main",
                    "rig",
                    "principal_silhouette",
                    (mast_x, 0.59 * length),
                    [
                        ("L", (mast_x, 0.075 * length)),
                        (
                            "C",
                            (0.62 * length, 0.080 * length),
                            (0.77 * length, 0.165 * length),
                            (0.72 * length, 0.36 * length),
                        ),
                        (
                            "C",
                            (0.66 * length, 0.47 * length),
                            (0.57 * length, 0.56 * length),
                            (mast_x, 0.59 * length),
                        ),
                    ],
                    closed=True,
                    priority="identity",
                ),
                _line(
                    "side-ac75-bowsprit",
                    "rig",
                    "major_structural_edges",
                    [(-0.11 * length, deck), (0.08 * length, deck)],
                    priority="identity",
                ),
                _line(
                    "side-ac75-lowered-foil",
                    "foil",
                    "accent_feature",
                    [
                        (0.57 * length, 0.052 * length),
                        (0.62 * length, -0.120 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-ac75-foil-wing",
                    "foil",
                    "accent_feature",
                    [
                        (0.57 * length, -0.120 * length),
                        (0.69 * length, -0.120 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-ac75-rudder-elevator",
                    "rudder",
                    "accent_feature",
                    [
                        (0.91 * length, deck),
                        (0.94 * length, -0.085 * length),
                        (0.89 * length, -0.085 * length),
                        (0.99 * length, -0.085 * length),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "side-ac75-cockpit-pod",
                    "cockpit",
                    "major_structural_edges",
                    [
                        (0.52 * length, deck),
                        (0.55 * length, 0.093 * length),
                        (0.76 * length, 0.084 * length),
                        (0.80 * length, deck),
                    ],
                    minimum_sheet="A4",
                ),
            ]
        )
        return details

    if subject_id == "j-class-endeavour":
        mast_x = 0.47 * length
        mast_top = 0.69 * length
        details.extend(
            [
                _line(
                    "side-jclass-mast",
                    "rig",
                    "major_structural_edges",
                    [(mast_x, deck), (mast_x, mast_top)],
                    priority="identity",
                ),
                _curve(
                    "side-jclass-mainsail",
                    "rig",
                    "principal_silhouette",
                    (mast_x, 0.67 * length),
                    [
                        ("L", (mast_x, 0.075 * length)),
                        ("L", (0.83 * length, 0.085 * length)),
                        (
                            "C",
                            (0.72 * length, 0.32 * length),
                            (0.60 * length, 0.55 * length),
                            (mast_x, 0.67 * length),
                        ),
                    ],
                    closed=True,
                    priority="identity",
                ),
                _boat_closed_line(
                    "side-jclass-jib",
                    "rig",
                    "major_structural_edges",
                    [
                        (mast_x, 0.60 * length),
                        (0.035 * length, deck),
                        (0.42 * length, 0.075 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-jclass-boom",
                    "rig",
                    "major_structural_edges",
                    [(mast_x, 0.078 * length), (0.85 * length, 0.087 * length)],
                    minimum_sheet="A4",
                ),
                _boat_closed_line(
                    "side-jclass-deep-keel",
                    "keel",
                    "accent_feature",
                    [
                        (0.45 * length, 0.005 * length),
                        (0.50 * length, -0.155 * length),
                        (0.62 * length, -0.150 * length),
                        (0.66 * length, 0.008 * length),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "side-jclass-cockpit",
                    "cockpit",
                    "mechanical_detail",
                    [
                        (0.69 * length, deck),
                        (0.72 * length, 0.082 * length),
                        (0.84 * length, 0.076 * length),
                        (0.86 * length, deck),
                    ],
                    minimum_sheet="A4",
                ),
            ]
        )
        return details

    if subject_id == "cutty-sark":
        deck_y = max(deck, 0.078 * length)
        masts = (("fore", 0.27, 0.61), ("main", 0.50, 0.72), ("mizzen", 0.71, 0.58))
        details.extend(
            [
                _line(
                    "side-cutty-bowsprit",
                    "rig",
                    "major_structural_edges",
                    [
                        (-0.17 * length, deck_y + 0.008 * length),
                        (0.16 * length, deck_y),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "side-cutty-stern-house",
                    "superstructure",
                    "major_structural_edges",
                    [
                        (0.76 * length, deck_y),
                        (0.77 * length, 0.130 * length),
                        (0.90 * length, 0.122 * length),
                        (0.91 * length, deck_y),
                    ],
                    priority="identity",
                ),
            ]
        )
        for mast_name, station, top_fraction in masts:
            mast_x = station * length
            mast_top = top_fraction * length
            details.append(
                _line(
                    f"side-cutty-{mast_name}-mast",
                    "rig",
                    "major_structural_edges",
                    [(mast_x, deck_y), (mast_x, mast_top)],
                    priority="identity",
                )
            )
            for tier, (low, high, half_width) in enumerate(
                ((0.09, 0.22, 0.075), (0.25, 0.37, 0.058), (0.40, 0.49, 0.040)),
                start=1,
            ):
                y0 = deck_y + low * (mast_top - deck_y)
                y1 = deck_y + high * (mast_top - deck_y)
                details.extend(
                    [
                        _line(
                            f"side-cutty-{mast_name}-yard-{tier}",
                            "rig",
                            "major_structural_edges",
                            [
                                (mast_x - half_width * length, y1),
                                (mast_x + half_width * length, y1),
                            ],
                            minimum_sheet="A5" if tier == 1 else "A4",
                        ),
                        _boat_closed_line(
                            f"side-cutty-{mast_name}-sail-{tier}",
                            "rig",
                            "mechanical_detail",
                            [
                                (mast_x - half_width * length, y0),
                                (mast_x + half_width * length, y0),
                                (mast_x + 0.78 * half_width * length, y1),
                                (mast_x - 0.78 * half_width * length, y1),
                            ],
                            minimum_sheet=(
                                "A5" if tier == 1 else ("A4" if tier == 2 else "A3")
                            ),
                            priority="identity" if tier == 1 else "normal",
                        ),
                    ]
                )
            details.extend(
                [
                    _line(
                        f"side-cutty-{mast_name}-forestay",
                        "rig",
                        "mechanical_detail",
                        [
                            (mast_x, mast_top),
                            ((station - 0.18) * length, deck_y),
                        ],
                        minimum_sheet="A4",
                    ),
                    _line(
                        f"side-cutty-{mast_name}-backstay",
                        "rig",
                        "mechanical_detail",
                        [
                            (mast_x, mast_top),
                            ((station + 0.17) * length, deck_y),
                        ],
                        minimum_sheet="A4",
                    ),
                ]
            )
        for index, station in enumerate((0.35, 0.43, 0.60), start=1):
            details.append(
                _boat_closed_line(
                    f"side-cutty-cargo-hatch-{index}",
                    "deck-equipment",
                    "panel_seam_lines",
                    [
                        (station * length, deck_y),
                        ((station + 0.055) * length, deck_y),
                        ((station + 0.055) * length, deck_y + 0.025 * length),
                        (station * length, deck_y + 0.025 * length),
                    ],
                    minimum_sheet="A3",
                )
            )
        return details

    if subject_id == "ss-great-britain":
        deck_y = max(deck, 0.082 * length)
        mast_stations = (0.17, 0.30, 0.43, 0.61, 0.75, 0.87)
        details.extend(
            [
                _boat_closed_line(
                    "side-ssgb-funnel",
                    "funnel",
                    "accent_feature",
                    [
                        (0.50 * length, deck_y),
                        (0.49 * length, 0.225 * length),
                        (0.55 * length, 0.225 * length),
                        (0.56 * length, deck_y),
                    ],
                    priority="identity",
                ),
                _ellipse(
                    "side-ssgb-propeller",
                    "propulsion",
                    "accent_feature",
                    (0.94 * length, 0.005 * length),
                    0.032 * length,
                    0.046 * length,
                    priority="identity",
                ),
                _line(
                    "side-ssgb-rudder",
                    "steering",
                    "major_structural_edges",
                    [
                        (0.975 * length, 0.057 * length),
                        (0.985 * length, -0.045 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-ssgb-upper-deck",
                    "superstructure",
                    "major_structural_edges",
                    [
                        (0.15 * length, 0.105 * length),
                        (0.90 * length, 0.102 * length),
                    ],
                    priority="identity",
                ),
            ]
        )
        for index, station in enumerate(mast_stations, start=1):
            mast_top = (0.39 + 0.035 * (1 - abs(3.5 - index) / 3.5)) * length
            details.extend(
                [
                    _line(
                        f"side-ssgb-mast-{index}",
                        "rig",
                        "major_structural_edges",
                        [(station * length, deck_y), (station * length, mast_top)],
                        priority="identity",
                    ),
                    _line(
                        f"side-ssgb-stay-{index}",
                        "rig",
                        "mechanical_detail",
                        [
                            (station * length, mast_top),
                            (
                                (station + (0.10 if index < 4 else -0.10)) * length,
                                deck_y,
                            ),
                        ],
                        minimum_sheet="A4",
                    ),
                ]
            )
        for index, station in enumerate(
            (0.20, 0.25, 0.34, 0.39, 0.65, 0.70, 0.79, 0.84), start=1
        ):
            details.append(
                _circle(
                    f"side-ssgb-porthole-{index}",
                    "openings",
                    "glazing_openings",
                    (station * length, 0.066 * length),
                    0.006 * length,
                    minimum_sheet="A3",
                )
            )
        return details

    if subject_id == "rms-queen-mary":
        details.extend(
            [
                _boat_closed_line(
                    "side-queen-mary-lower-superstructure",
                    "superstructure",
                    "principal_silhouette",
                    [
                        (0.19 * length, deck),
                        (0.26 * length, 0.175 * length),
                        (0.83 * length, 0.170 * length),
                        (0.88 * length, deck),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "side-queen-mary-upper-superstructure",
                    "superstructure",
                    "major_structural_edges",
                    [
                        (0.28 * length, 0.175 * length),
                        (0.34 * length, 0.235 * length),
                        (0.72 * length, 0.228 * length),
                        (0.79 * length, 0.170 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-queen-mary-bridge-wing",
                    "bridge",
                    "major_structural_edges",
                    [
                        (0.285 * length, 0.232 * length),
                        (0.36 * length, 0.246 * length),
                        (0.40 * length, 0.232 * length),
                    ],
                    priority="identity",
                ),
                _line(
                    "side-queen-mary-forward-mast",
                    "mast",
                    "major_structural_edges",
                    [
                        (0.31 * length, 0.232 * length),
                        (0.31 * length, 0.335 * length),
                    ],
                    minimum_sheet="A4",
                ),
                _line(
                    "side-queen-mary-aft-mast",
                    "mast",
                    "major_structural_edges",
                    [
                        (0.73 * length, 0.224 * length),
                        (0.73 * length, 0.305 * length),
                    ],
                    minimum_sheet="A4",
                ),
            ]
        )
        for index, station in enumerate((0.41, 0.54, 0.67), start=1):
            details.extend(
                [
                    _boat_closed_line(
                        f"side-queen-mary-funnel-{index}",
                        "funnel",
                        "accent_feature",
                        [
                            ((station - 0.025) * length, 0.222 * length),
                            ((station - 0.018) * length, 0.330 * length),
                            ((station + 0.022) * length, 0.330 * length),
                            ((station + 0.028) * length, 0.222 * length),
                        ],
                        priority="identity",
                    ),
                    _ellipse(
                        f"side-queen-mary-funnel-cap-{index}",
                        "funnel",
                        "major_structural_edges",
                        (station * length, 0.330 * length),
                        0.020 * length,
                        0.006 * length,
                        minimum_sheet="A4",
                    ),
                ]
            )
        for index, station in enumerate(
            (0.31, 0.36, 0.42, 0.48, 0.60, 0.66, 0.72, 0.77), start=1
        ):
            details.append(
                _ellipse(
                    f"side-queen-mary-lifeboat-{index}",
                    "lifeboats",
                    "mechanical_detail",
                    (station * length, 0.188 * length),
                    0.022 * length,
                    0.007 * length,
                    minimum_sheet="A3",
                )
            )
        for index, station in enumerate(
            (
                0.16,
                0.21,
                0.26,
                0.32,
                0.38,
                0.44,
                0.50,
                0.56,
                0.62,
                0.68,
                0.74,
                0.80,
                0.86,
            ),
            start=1,
        ):
            details.append(
                _circle(
                    f"side-queen-mary-porthole-{index}",
                    "openings",
                    "glazing_openings",
                    (station * length, 0.094 * length),
                    0.0048 * length,
                    minimum_sheet="A3",
                )
            )
        return details

    # Future watercraft retain an honest generic external study rather than
    # silently failing compilation, but named release subjects never use it.
    details.extend(
        [
            _boat_closed_line(
                "side-generic-wheelhouse",
                "wheelhouse",
                "principal_silhouette",
                [
                    (0.36 * length, deck),
                    (0.42 * length, 0.18 * length),
                    (0.68 * length, 0.17 * length),
                    (0.74 * length, deck),
                ],
                priority="identity",
            ),
            _line(
                "side-generic-rail",
                "rails",
                "mechanical_detail",
                [(0.10 * length, 1.38 * waterline), (0.90 * length, 1.35 * waterline)],
                minimum_sheet="A4",
            ),
        ]
    )
    return details


def _boat_plan_identity_details(
    subject_id: str,
    length: float,
    beam: float,
) -> list[dict[str, Any]]:
    """Return deterministic plan-view equipment and deck-layout signatures."""

    details: list[dict[str, Any]] = []

    if subject_id == "riva-aquarama-special":
        details.extend(
            [
                _curve(
                    "plan-riva-wraparound-windscreen",
                    "windscreen",
                    "glazing_openings",
                    (0.29 * length, -0.28 * beam),
                    [
                        (
                            "C",
                            (0.34 * length, -0.38 * beam),
                            (0.44 * length, -0.38 * beam),
                            (0.49 * length, 0),
                        ),
                        (
                            "C",
                            (0.44 * length, 0.38 * beam),
                            (0.34 * length, 0.38 * beam),
                            (0.29 * length, 0.28 * beam),
                        ),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-riva-cockpit",
                    "cockpit",
                    "major_structural_edges",
                    [
                        (0.42 * length, -0.30 * beam),
                        (0.66 * length, -0.30 * beam),
                        (0.69 * length, 0.30 * beam),
                        (0.42 * length, 0.30 * beam),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-riva-sunpad",
                    "aft-deck",
                    "major_structural_edges",
                    [
                        (0.69 * length, -0.33 * beam),
                        (0.91 * length, -0.25 * beam),
                        (0.91 * length, 0.25 * beam),
                        (0.69 * length, 0.33 * beam),
                    ],
                    priority="identity",
                ),
                _line(
                    "plan-riva-stern-step",
                    "stern",
                    "mechanical_detail",
                    [(0.94 * length, -0.28 * beam), (0.94 * length, 0.28 * beam)],
                    minimum_sheet="A4",
                ),
            ]
        )
        for index, y in enumerate((-0.15, 0.15), start=1):
            details.extend(
                [
                    _ellipse(
                        f"plan-riva-front-seat-{index}",
                        "cockpit",
                        "mechanical_detail",
                        (0.49 * length, y * beam),
                        0.028 * length,
                        0.075 * beam,
                        minimum_sheet="A4",
                    ),
                    _boat_closed_line(
                        f"plan-riva-twin-engine-hatch-{index}",
                        "engine-bay",
                        "panel_seam_lines",
                        [
                            (0.73 * length, (y - 0.11) * beam),
                            (0.88 * length, (y - 0.11) * beam),
                            (0.88 * length, (y + 0.11) * beam),
                            (0.73 * length, (y + 0.11) * beam),
                        ],
                        minimum_sheet="A3",
                    ),
                ]
            )
        return details

    if subject_id == "bluebird-k7":
        details.extend(
            [
                _curve(
                    "plan-bluebird-central-fuselage",
                    "fuselage",
                    "principal_silhouette",
                    (0.04 * length, 0),
                    [
                        (
                            "C",
                            (0.25 * length, -0.08 * beam),
                            (0.72 * length, -0.11 * beam),
                            (0.96 * length, -0.035 * beam),
                        ),
                        ("L", (length, 0)),
                        (
                            "C",
                            (0.72 * length, 0.11 * beam),
                            (0.25 * length, 0.08 * beam),
                            (0.04 * length, 0),
                        ),
                    ],
                    closed=True,
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-bluebird-port-sponson",
                    "hydroplane",
                    "principal_silhouette",
                    [
                        (0.16 * length, -0.47 * beam),
                        (0.24 * length, -0.75 * beam),
                        (0.52 * length, -0.72 * beam),
                        (0.59 * length, -0.20 * beam),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-bluebird-starboard-sponson",
                    "hydroplane",
                    "principal_silhouette",
                    [
                        (0.16 * length, 0.47 * beam),
                        (0.24 * length, 0.75 * beam),
                        (0.52 * length, 0.72 * beam),
                        (0.59 * length, 0.20 * beam),
                    ],
                    priority="identity",
                ),
                _ellipse(
                    "plan-bluebird-canopy",
                    "cockpit",
                    "glazing_openings",
                    (0.49 * length, 0),
                    0.085 * length,
                    0.095 * beam,
                    priority="identity",
                ),
                _line(
                    "plan-bluebird-tailplane",
                    "tail",
                    "accent_feature",
                    [(0.84 * length, -0.52 * beam), (0.84 * length, 0.52 * beam)],
                    priority="identity",
                ),
                _line(
                    "plan-bluebird-tail-fin",
                    "tail",
                    "major_structural_edges",
                    [(0.75 * length, 0), (0.96 * length, 0)],
                    minimum_sheet="A4",
                ),
                _ellipse(
                    "plan-bluebird-jet-nozzle",
                    "jet",
                    "mechanical_detail",
                    (0.965 * length, 0),
                    0.018 * length,
                    0.055 * beam,
                    minimum_sheet="A4",
                ),
            ]
        )
        return details

    if subject_id == "rnli-shannon-class":
        details.extend(
            [
                _boat_closed_line(
                    "plan-shannon-wheelhouse",
                    "wheelhouse",
                    "principal_silhouette",
                    [
                        (0.28 * length, -0.24 * beam),
                        (0.36 * length, -0.34 * beam),
                        (0.66 * length, -0.31 * beam),
                        (0.70 * length, 0.31 * beam),
                        (0.36 * length, 0.34 * beam),
                        (0.28 * length, 0.24 * beam),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-shannon-upper-helm",
                    "upper-helm",
                    "accent_feature",
                    [
                        (0.43 * length, -0.16 * beam),
                        (0.59 * length, -0.15 * beam),
                        (0.60 * length, 0.15 * beam),
                        (0.43 * length, 0.16 * beam),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-shannon-aft-working-deck",
                    "working-deck",
                    "panel_seam_lines",
                    [
                        (0.72 * length, -0.34 * beam),
                        (0.94 * length, -0.34 * beam),
                        (0.94 * length, 0.34 * beam),
                        (0.72 * length, 0.34 * beam),
                    ],
                    minimum_sheet="A4",
                ),
                _line(
                    "plan-shannon-port-side-deck",
                    "side-deck",
                    "major_structural_edges",
                    [(0.18 * length, -0.41 * beam), (0.90 * length, -0.41 * beam)],
                    minimum_sheet="A4",
                ),
                _line(
                    "plan-shannon-starboard-side-deck",
                    "side-deck",
                    "major_structural_edges",
                    [(0.18 * length, 0.41 * beam), (0.90 * length, 0.41 * beam)],
                    minimum_sheet="A4",
                ),
                _ellipse(
                    "plan-shannon-waterjet-port",
                    "waterjet",
                    "accent_feature",
                    (0.965 * length, -0.18 * beam),
                    0.020 * length,
                    0.055 * beam,
                    priority="identity",
                ),
                _ellipse(
                    "plan-shannon-waterjet-starboard",
                    "waterjet",
                    "accent_feature",
                    (0.965 * length, 0.18 * beam),
                    0.020 * length,
                    0.055 * beam,
                    priority="identity",
                ),
            ]
        )
        return details

    if subject_id == "uscg-47-foot-motor-lifeboat":
        details.extend(
            [
                _boat_closed_line(
                    "plan-uscg-pilothouse",
                    "pilothouse",
                    "principal_silhouette",
                    [
                        (0.33 * length, -0.27 * beam),
                        (0.40 * length, -0.34 * beam),
                        (0.69 * length, -0.32 * beam),
                        (0.72 * length, 0.32 * beam),
                        (0.40 * length, 0.34 * beam),
                        (0.33 * length, 0.27 * beam),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-uscg-aft-well",
                    "working-deck",
                    "major_structural_edges",
                    [
                        (0.73 * length, -0.31 * beam),
                        (0.93 * length, -0.34 * beam),
                        (0.93 * length, 0.34 * beam),
                        (0.73 * length, 0.31 * beam),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-uscg-forward-well",
                    "working-deck",
                    "panel_seam_lines",
                    [
                        (0.12 * length, -0.20 * beam),
                        (0.28 * length, -0.30 * beam),
                        (0.28 * length, 0.30 * beam),
                        (0.12 * length, 0.20 * beam),
                    ],
                    minimum_sheet="A4",
                ),
            ]
        )
        for side_name, sign in (("port", -1.0), ("starboard", 1.0)):
            details.extend(
                [
                    _line(
                        f"plan-uscg-propulsion-axis-{side_name}",
                        "propulsion",
                        "mechanical_detail",
                        [
                            (0.75 * length, sign * 0.18 * beam),
                            (1.01 * length, sign * 0.18 * beam),
                        ],
                        minimum_sheet="A4",
                    ),
                    _circle(
                        f"plan-uscg-propeller-{side_name}",
                        "propulsion",
                        "accent_feature",
                        (0.96 * length, sign * 0.18 * beam),
                        0.065 * beam,
                        priority="identity",
                    ),
                    _line(
                        f"plan-uscg-rudder-{side_name}",
                        "steering",
                        "major_structural_edges",
                        [
                            (0.98 * length, sign * 0.14 * beam),
                            (0.98 * length, sign * 0.24 * beam),
                        ],
                        minimum_sheet="A4",
                    ),
                ]
            )
        return details

    if subject_id == "pt-109":
        details.extend(
            [
                _boat_closed_line(
                    "plan-pt109-deckhouse",
                    "deckhouse",
                    "principal_silhouette",
                    [
                        (0.32 * length, -0.19 * beam),
                        (0.58 * length, -0.22 * beam),
                        (0.62 * length, 0.15 * beam),
                        (0.36 * length, 0.20 * beam),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-pt109-cockpit",
                    "cockpit",
                    "major_structural_edges",
                    [
                        (0.56 * length, -0.13 * beam),
                        (0.72 * length, -0.13 * beam),
                        (0.72 * length, 0.13 * beam),
                        (0.56 * length, 0.13 * beam),
                    ],
                    priority="identity",
                ),
            ]
        )
        tube_records = (
            ("port-forward", 0.18, 0.56, -0.34),
            ("port-aft", 0.46, 0.84, -0.38),
            ("starboard-forward", 0.18, 0.56, 0.34),
            ("starboard-aft", 0.46, 0.84, 0.38),
        )
        for name, start, end, y_fraction in tube_records:
            details.append(
                _boat_closed_line(
                    f"plan-pt109-torpedo-tube-{name}",
                    "torpedo-tube",
                    "mechanical_detail",
                    [
                        (start * length, (y_fraction - 0.035) * beam),
                        (end * length, (y_fraction - 0.035) * beam),
                        ((end + 0.025) * length, (y_fraction + 0.035) * beam),
                        ((start + 0.025) * length, (y_fraction + 0.035) * beam),
                    ],
                    priority="identity",
                )
            )
        for name, station in (("forward", 0.20), ("aft", 0.78)):
            details.append(
                _circle(
                    f"plan-pt109-gun-ring-{name}",
                    "gun-platform",
                    "accent_feature",
                    (station * length, 0),
                    0.085 * beam,
                    priority="identity",
                )
            )
        for index, station in enumerate((0.70, 0.78, 0.86), start=1):
            details.append(
                _boat_closed_line(
                    f"plan-pt109-engine-hatch-{index}",
                    "engine-bay",
                    "panel_seam_lines",
                    [
                        (station * length, -0.14 * beam),
                        ((station + 0.055) * length, -0.14 * beam),
                        ((station + 0.055) * length, 0.14 * beam),
                        (station * length, 0.14 * beam),
                    ],
                    minimum_sheet="A3",
                )
            )
        return details

    if subject_id == "ac75-foiling-monohull":
        details.extend(
            [
                _line(
                    "plan-ac75-bowsprit",
                    "rig",
                    "major_structural_edges",
                    [(-0.11 * length, 0), (0.12 * length, 0)],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-ac75-cockpit-port",
                    "cockpit",
                    "major_structural_edges",
                    [
                        (0.47 * length, -0.30 * beam),
                        (0.79 * length, -0.25 * beam),
                        (0.80 * length, -0.10 * beam),
                        (0.49 * length, -0.12 * beam),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-ac75-cockpit-starboard",
                    "cockpit",
                    "major_structural_edges",
                    [
                        (0.47 * length, 0.30 * beam),
                        (0.79 * length, 0.25 * beam),
                        (0.80 * length, 0.10 * beam),
                        (0.49 * length, 0.12 * beam),
                    ],
                    priority="identity",
                ),
                _line(
                    "plan-ac75-traveller",
                    "rig",
                    "mechanical_detail",
                    [(0.80 * length, -0.31 * beam), (0.80 * length, 0.31 * beam)],
                    minimum_sheet="A4",
                ),
                _line(
                    "plan-ac75-rudder-elevator",
                    "rudder",
                    "accent_feature",
                    [(0.93 * length, -0.46 * beam), (0.93 * length, 0.46 * beam)],
                    priority="identity",
                ),
            ]
        )
        for index, station in enumerate((0.38, 0.62), start=1):
            details.append(
                _line(
                    f"plan-ac75-foil-arm-{index}",
                    "foils",
                    "accent_feature",
                    [
                        (station * length, -0.82 * beam),
                        (station * length, 0.82 * beam),
                    ],
                    priority="identity",
                )
            )
            for side_name, sign in (("port", -1.0), ("starboard", 1.0)):
                details.append(
                    _line(
                        f"plan-ac75-foil-wing-{index}-{side_name}",
                        "foils",
                        "accent_feature",
                        [
                            ((station - 0.075) * length, sign * 0.82 * beam),
                            ((station + 0.075) * length, sign * 0.82 * beam),
                        ],
                        priority="identity",
                    )
                )
        return details

    if subject_id == "j-class-endeavour":
        details.extend(
            [
                _circle(
                    "plan-jclass-mast",
                    "rig",
                    "major_structural_edges",
                    (0.47 * length, 0),
                    0.016 * beam,
                    priority="identity",
                ),
                _line(
                    "plan-jclass-boom",
                    "rig",
                    "major_structural_edges",
                    [(0.47 * length, 0), (0.84 * length, 0)],
                    priority="identity",
                ),
                _ellipse(
                    "plan-jclass-cockpit",
                    "cockpit",
                    "glazing_openings",
                    (0.74 * length, 0),
                    0.095 * length,
                    0.17 * beam,
                    priority="identity",
                ),
                _line(
                    "plan-jclass-foredeck-spine",
                    "deck",
                    "major_structural_edges",
                    [(0.08 * length, 0), (0.44 * length, 0)],
                    minimum_sheet="A4",
                ),
            ]
        )
        for index, station in enumerate((0.22, 0.34, 0.56), start=1):
            details.append(
                _boat_closed_line(
                    f"plan-jclass-deck-hatch-{index}",
                    "deck-equipment",
                    "panel_seam_lines",
                    [
                        (station * length, -0.11 * beam),
                        ((station + 0.06) * length, -0.11 * beam),
                        ((station + 0.06) * length, 0.11 * beam),
                        (station * length, 0.11 * beam),
                    ],
                    minimum_sheet="A4",
                )
            )
        for index, station in enumerate((0.42, 0.52, 0.64, 0.86), start=1):
            details.append(
                _circle(
                    f"plan-jclass-deck-fitting-{index}",
                    "deck-equipment",
                    "mechanical_detail",
                    (station * length, (-0.21 if index % 2 else 0.21) * beam),
                    0.025 * beam,
                    minimum_sheet="A3",
                )
            )
        return details

    if subject_id == "cutty-sark":
        mast_stations = (0.27, 0.50, 0.71)
        details.extend(
            [
                _line(
                    "plan-cutty-bowsprit",
                    "rig",
                    "major_structural_edges",
                    [(-0.17 * length, 0), (0.18 * length, 0)],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-cutty-stern-house",
                    "superstructure",
                    "major_structural_edges",
                    [
                        (0.76 * length, -0.22 * beam),
                        (0.90 * length, -0.20 * beam),
                        (0.90 * length, 0.20 * beam),
                        (0.76 * length, 0.22 * beam),
                    ],
                    priority="identity",
                ),
            ]
        )
        for index, station in enumerate(mast_stations, start=1):
            details.extend(
                [
                    _circle(
                        f"plan-cutty-mast-{index}",
                        "rig",
                        "major_structural_edges",
                        (station * length, 0),
                        0.018 * beam,
                        priority="identity",
                    ),
                    _line(
                        f"plan-cutty-yard-{index}",
                        "rig",
                        "major_structural_edges",
                        [
                            (station * length, -0.54 * beam),
                            (station * length, 0.54 * beam),
                        ],
                        minimum_sheet="A4",
                    ),
                ]
            )
        for index, station in enumerate((0.34, 0.42, 0.59), start=1):
            details.append(
                _boat_closed_line(
                    f"plan-cutty-cargo-hatch-{index}",
                    "deck-equipment",
                    "panel_seam_lines",
                    [
                        (station * length, -0.18 * beam),
                        ((station + 0.06) * length, -0.18 * beam),
                        ((station + 0.06) * length, 0.18 * beam),
                        (station * length, 0.18 * beam),
                    ],
                    minimum_sheet="A4",
                )
            )
        for index, station in enumerate((0.21, 0.82), start=1):
            details.append(
                _circle(
                    f"plan-cutty-capstan-{index}",
                    "deck-equipment",
                    "mechanical_detail",
                    (station * length, 0),
                    0.045 * beam,
                    minimum_sheet="A3",
                )
            )
        return details

    if subject_id == "ss-great-britain":
        mast_stations = (0.17, 0.30, 0.43, 0.61, 0.75, 0.87)
        for index, station in enumerate(mast_stations, start=1):
            details.append(
                _circle(
                    f"plan-ssgb-mast-{index}",
                    "rig",
                    "major_structural_edges",
                    (station * length, 0),
                    0.018 * beam,
                    priority="identity",
                )
            )
        details.extend(
            [
                _ellipse(
                    "plan-ssgb-funnel",
                    "funnel",
                    "accent_feature",
                    (0.53 * length, 0),
                    0.030 * length,
                    0.11 * beam,
                    priority="identity",
                ),
                _line(
                    "plan-ssgb-propeller-axis",
                    "propulsion",
                    "mechanical_detail",
                    [(0.82 * length, 0), (1.01 * length, 0)],
                    minimum_sheet="A4",
                ),
                _line(
                    "plan-ssgb-rudder",
                    "steering",
                    "major_structural_edges",
                    [(0.96 * length, -0.12 * beam), (0.96 * length, 0.12 * beam)],
                    priority="identity",
                ),
            ]
        )
        for index, station in enumerate((0.22, 0.35, 0.65, 0.78), start=1):
            details.append(
                _boat_closed_line(
                    f"plan-ssgb-hatch-{index}",
                    "deck-equipment",
                    "panel_seam_lines",
                    [
                        (station * length, -0.17 * beam),
                        ((station + 0.07) * length, -0.17 * beam),
                        ((station + 0.07) * length, 0.17 * beam),
                        (station * length, 0.17 * beam),
                    ],
                    minimum_sheet="A4",
                )
            )
        return details

    if subject_id == "rms-queen-mary":
        details.extend(
            [
                _boat_closed_line(
                    "plan-queen-mary-superstructure",
                    "superstructure",
                    "principal_silhouette",
                    [
                        (0.22 * length, -0.30 * beam),
                        (0.33 * length, -0.36 * beam),
                        (0.79 * length, -0.32 * beam),
                        (0.87 * length, -0.20 * beam),
                        (0.87 * length, 0.20 * beam),
                        (0.79 * length, 0.32 * beam),
                        (0.33 * length, 0.36 * beam),
                        (0.22 * length, 0.30 * beam),
                    ],
                    priority="identity",
                ),
                _boat_closed_line(
                    "plan-queen-mary-bridge",
                    "bridge",
                    "major_structural_edges",
                    [
                        (0.27 * length, -0.39 * beam),
                        (0.35 * length, -0.43 * beam),
                        (0.38 * length, 0.43 * beam),
                        (0.27 * length, 0.39 * beam),
                    ],
                    priority="identity",
                ),
            ]
        )
        for index, station in enumerate((0.41, 0.54, 0.67), start=1):
            details.append(
                _ellipse(
                    f"plan-queen-mary-funnel-{index}",
                    "funnel",
                    "accent_feature",
                    (station * length, 0),
                    0.024 * length,
                    0.105 * beam,
                    priority="identity",
                )
            )
        for index, station in enumerate((0.34, 0.40, 0.46, 0.61, 0.67, 0.73), start=1):
            for side_name, sign in (("port", -1.0), ("starboard", 1.0)):
                details.append(
                    _ellipse(
                        f"plan-queen-mary-lifeboat-{index}-{side_name}",
                        "lifeboats",
                        "mechanical_detail",
                        (station * length, sign * 0.32 * beam),
                        0.022 * length,
                        0.035 * beam,
                        minimum_sheet="A3",
                    )
                )
        for index, station in enumerate((0.18, 0.23, 0.82), start=1):
            details.append(
                _boat_closed_line(
                    f"plan-queen-mary-cargo-hatch-{index}",
                    "deck-equipment",
                    "panel_seam_lines",
                    [
                        (station * length, -0.14 * beam),
                        ((station + 0.04) * length, -0.14 * beam),
                        ((station + 0.04) * length, 0.14 * beam),
                        (station * length, 0.14 * beam),
                    ],
                    minimum_sheet="A4",
                )
            )
        return details

    return [
        _curve(
            "plan-generic-cabin",
            "superstructure",
            "glazing_openings",
            (0.36 * length, 0),
            [
                (
                    "C",
                    (0.43 * length, -0.28 * beam),
                    (0.68 * length, -0.28 * beam),
                    (0.75 * length, 0),
                ),
                (
                    "C",
                    (0.68 * length, 0.28 * beam),
                    (0.43 * length, 0.28 * beam),
                    (0.36 * length, 0),
                ),
            ],
            closed=True,
            priority="identity",
        )
    ]


def _boat_views(
    subject: dict[str, Any], dims: dict[str, float | None]
) -> list[dict[str, Any]]:
    length = float(dims["length"])
    beam = float(dims["beam"])
    draft = float(dims.get("draft") or beam * 0.32)
    template = _template(subject).casefold()
    subject_id = str(subject["id"])
    subject_category = str(subject.get("category", "boat"))
    sail = (
        subject_category == "yacht"
        or "cutty-sark" in subject_id
        or any(word in template for word in ("sail", "yacht", "cutter"))
    ) and "motor" not in template
    liner = (subject_category == "ship" and "cutty-sark" not in subject_id) or any(
        word in template for word in ("liner", "ferry")
    )
    profile_height = beam * (2.25 if sail else (1.45 if liner else 1.05))
    waterline = draft
    side_controls = _raw_view_controls(subject, "side")
    side_points = side_controls.get("silhouette_points")
    if isinstance(side_points, list) and len(side_points) >= 4:
        authored_vertical_scale = 0.32 * length
        scaled_side_points = [
            (
                float(point[0]) * length,
                (float(point[1]) + 0.12) * authored_vertical_scale,
            )
            for point in side_points
        ]
        side_envelope = _primitive(
            "side-hull-envelope",
            "hull",
            "principal_silhouette",
            {"path": _smooth_closed(scaled_side_points)},
            priority="identity",
        )
        authored_top = max(point[1] for point in scaled_side_points)
        waterline = 0.12 * authored_vertical_scale
        profile_height = max(profile_height, authored_top * (2.0 if sail else 1.25))
    else:
        side_envelope = _curve(
            "side-hull-envelope",
            "hull",
            "principal_silhouette",
            (0, waterline),
            [
                (
                    "C",
                    (0.05 * length, 0.15 * draft),
                    (0.22 * length, 0),
                    (0.48 * length, 0),
                ),
                (
                    "C",
                    (0.76 * length, 0),
                    (0.94 * length, 0.20 * draft),
                    (length, 1.15 * waterline),
                ),
                (
                    "C",
                    (0.83 * length, 1.32 * waterline),
                    (0.18 * length, 1.30 * waterline),
                    (0, waterline),
                ),
            ],
            closed=True,
            priority="identity",
        )
    side: list[dict[str, Any]] = [
        side_envelope,
        _curve(
            "side-sheer",
            "deck",
            "major_structural_edges",
            (0.03 * length, 1.12 * waterline),
            [
                (
                    "C",
                    (0.30 * length, 1.28 * waterline),
                    (0.72 * length, 1.23 * waterline),
                    (0.98 * length, 1.20 * waterline),
                )
            ],
            priority="identity",
        ),
        _line(
            "side-waterline",
            "hull",
            "construction_geometry",
            [(0.02 * length, waterline), (0.98 * length, waterline)],
            line_style="centre",
            minimum_sheet="A3",
        ),
    ]
    if subject_id in _BOAT_IDENTITY_SUBJECT_IDS:
        side.extend(_boat_side_identity_details(subject_id, length, beam, waterline))
    elif sail:
        if "cutty-sark" in subject_id:
            deck_y = max(waterline * 1.28, 0.19 * length)
            mast_records = (
                ("fore", 0.30, 0.70),
                ("main", 0.52, 0.78),
                ("mizzen", 0.72, 0.66),
            )
            for mast_name, station, top_fraction in mast_records:
                mast_x = station * length
                mast_top = top_fraction * length
                side.append(
                    _line(
                        f"side-{mast_name}-mast",
                        "rig",
                        "major_structural_edges",
                        [(mast_x, deck_y), (mast_x, mast_top)],
                        priority="identity",
                    )
                )
                for yard_index, fraction in enumerate((0.46, 0.60, 0.73), start=1):
                    yard_y = deck_y + (mast_top - deck_y) * fraction
                    half_yard = (0.095 - 0.016 * yard_index) * length
                    side.append(
                        _line(
                            f"side-{mast_name}-yard-{yard_index}",
                            "rig",
                            "major_structural_edges",
                            [
                                (mast_x - half_yard, yard_y),
                                (mast_x + half_yard, yard_y),
                            ],
                            minimum_sheet="A4",
                        )
                    )
                side.append(
                    _line(
                        f"side-{mast_name}-lower-sail",
                        "rig",
                        "principal_silhouette",
                        [
                            (mast_x - 0.075 * length, deck_y + 0.09 * length),
                            (mast_x + 0.075 * length, deck_y + 0.09 * length),
                            (mast_x + 0.055 * length, deck_y + 0.25 * length),
                            (mast_x - 0.055 * length, deck_y + 0.25 * length),
                            (mast_x - 0.075 * length, deck_y + 0.09 * length),
                        ],
                        priority="identity",
                    )
                )
            side.extend(
                [
                    _line(
                        "side-bowsprit",
                        "rig",
                        "major_structural_edges",
                        [
                            (-0.13 * length, deck_y + 0.02 * length),
                            (0.15 * length, deck_y),
                        ],
                        priority="identity",
                    ),
                    _line(
                        "side-headstay",
                        "rig",
                        "mechanical_detail",
                        [
                            (-0.10 * length, deck_y + 0.02 * length),
                            (0.30 * length, 0.70 * length),
                        ],
                        minimum_sheet="A4",
                    ),
                    _line(
                        "side-main-backstay",
                        "rig",
                        "mechanical_detail",
                        [(0.52 * length, 0.78 * length), (0.96 * length, deck_y)],
                        minimum_sheet="A4",
                    ),
                    _line(
                        "side-deckhouse",
                        "superstructure",
                        "mechanical_detail",
                        [
                            (0.76 * length, deck_y),
                            (0.76 * length, deck_y + 0.055 * length),
                            (0.88 * length, deck_y + 0.055 * length),
                            (0.88 * length, deck_y),
                        ],
                        minimum_sheet="A4",
                    ),
                ]
            )
        else:
            mast_x = 0.46 * length
            mast_top = profile_height
            side.extend(
                [
                    _line(
                        "side-mast",
                        "rig",
                        "major_structural_edges",
                        [(mast_x, 1.24 * waterline), (mast_x, mast_top)],
                        priority="identity",
                    ),
                    _line(
                        "side-main-sail",
                        "rig",
                        "principal_silhouette",
                        [
                            (mast_x, mast_top * 0.96),
                            (mast_x, 1.40 * waterline),
                            (0.80 * length, 1.42 * waterline),
                            (mast_x, mast_top * 0.96),
                        ],
                        priority="identity",
                    ),
                    _line(
                        "side-forestay",
                        "rig",
                        "major_structural_edges",
                        [(mast_x, mast_top * 0.95), (0.96 * length, 1.24 * waterline)],
                        minimum_sheet="A4",
                    ),
                    _line(
                        "side-jib",
                        "rig",
                        "major_structural_edges",
                        [
                            (mast_x, mast_top * 0.90),
                            (0.94 * length, 1.27 * waterline),
                            (0.57 * length, 1.30 * waterline),
                        ],
                        minimum_sheet="A4",
                    ),
                ]
            )
    else:
        cabin_start = 0.34 if liner else 0.42
        cabin_end = 0.78 if liner else 0.73
        cabin_top = profile_height * (0.82 if liner else 0.72)
        side.append(
            _line(
                "side-superstructure",
                "superstructure",
                "principal_silhouette",
                [
                    (cabin_start * length, 1.26 * waterline),
                    (cabin_start * length, cabin_top),
                    (cabin_end * length, cabin_top),
                    (cabin_end * length, 1.26 * waterline),
                ],
                priority="identity",
            )
        )
        window_count = 10 if liner else 5
        for index in range(window_count):
            x = (
                cabin_start + (index + 0.5) * (cabin_end - cabin_start) / window_count
            ) * length
            side.append(
                _circle(
                    f"side-window-{index + 1}",
                    "openings",
                    "glazing_openings",
                    (x, cabin_top * 0.88),
                    0.012 * length,
                    minimum_sheet="A3",
                )
            )
        side.append(
            _line(
                "side-rail",
                "deck",
                "mechanical_detail",
                [(0.10 * length, 1.38 * waterline), (0.90 * length, 1.35 * waterline)],
                minimum_sheet="A4",
            )
        )
        if "queen-mary" in subject_id:
            for index, station in enumerate((0.43, 0.55, 0.67), start=1):
                side.append(
                    _line(
                        f"side-funnel-{index}",
                        "superstructure",
                        "accent_feature",
                        [
                            ((station - 0.025) * length, profile_height * 0.82),
                            ((station - 0.018) * length, profile_height * 1.08),
                            ((station + 0.022) * length, profile_height * 1.08),
                            ((station + 0.025) * length, profile_height * 0.82),
                        ],
                        priority="identity",
                    )
                )
            side.extend(
                [
                    _line(
                        "side-upper-promenade",
                        "superstructure",
                        "major_structural_edges",
                        [
                            (0.24 * length, profile_height * 0.72),
                            (0.82 * length, profile_height * 0.72),
                        ],
                        priority="identity",
                    ),
                    _line(
                        "side-lifeboat-deck",
                        "superstructure",
                        "mechanical_detail",
                        [
                            (0.27 * length, profile_height * 0.64),
                            (0.80 * length, profile_height * 0.64),
                        ],
                        minimum_sheet="A4",
                    ),
                ]
            )
        elif "ss-great-britain" in subject_id:
            side.extend(
                [
                    _line(
                        "side-steam-funnel",
                        "superstructure",
                        "accent_feature",
                        [
                            (0.48 * length, profile_height * 0.72),
                            (0.47 * length, profile_height),
                            (0.53 * length, profile_height),
                            (0.54 * length, profile_height * 0.72),
                        ],
                        priority="identity",
                    ),
                    _line(
                        "side-foremast",
                        "rig",
                        "major_structural_edges",
                        [
                            (0.30 * length, 1.3 * waterline),
                            (0.30 * length, 1.5 * profile_height),
                        ],
                        priority="identity",
                    ),
                    _line(
                        "side-mainmast",
                        "rig",
                        "major_structural_edges",
                        [
                            (0.70 * length, 1.3 * waterline),
                            (0.70 * length, 1.45 * profile_height),
                        ],
                        priority="identity",
                    ),
                ]
            )

    plan_controls = _raw_view_controls(subject, "plan")
    half_profile = plan_controls.get("half_beam_profile")
    if isinstance(half_profile, list) and len(half_profile) >= 3:
        maximum = max(abs(float(point[1])) for point in half_profile) or 1.0
        starboard = [
            (float(point[0]) * length, float(point[1]) / maximum * beam / 2.0)
            for point in half_profile
        ]
        port = [(x, -y) for x, y in reversed(starboard)]
        plan_envelope = _primitive(
            "plan-hull-envelope",
            "hull",
            "principal_silhouette",
            {"path": _smooth_closed([*starboard, *port])},
            priority="identity",
        )
    else:
        plan_envelope = _curve(
            "plan-hull-envelope",
            "hull",
            "principal_silhouette",
            (0, 0),
            [
                (
                    "C",
                    (0.13 * length, -beam / 2),
                    (0.65 * length, -beam / 2),
                    (0.92 * length, -0.28 * beam),
                ),
                (
                    "C",
                    (0.97 * length, -0.16 * beam),
                    (0.995 * length, -0.04 * beam),
                    (length, 0),
                ),
                (
                    "C",
                    (0.995 * length, 0.04 * beam),
                    (0.97 * length, 0.16 * beam),
                    (0.92 * length, 0.28 * beam),
                ),
                ("C", (0.65 * length, beam / 2), (0.13 * length, beam / 2), (0, 0)),
            ],
            closed=True,
            priority="identity",
        )
    plan: list[dict[str, Any]] = [
        plan_envelope,
        _curve(
            "plan-deck-line",
            "deck",
            "major_structural_edges",
            (0.08 * length, 0),
            [
                (
                    "C",
                    (0.25 * length, -0.38 * beam),
                    (0.70 * length, -0.38 * beam),
                    (0.90 * length, 0),
                ),
                (
                    "C",
                    (0.70 * length, 0.38 * beam),
                    (0.25 * length, 0.38 * beam),
                    (0.08 * length, 0),
                ),
            ],
            closed=True,
            priority="identity",
        ),
        _line(
            "plan-centre",
            "construction",
            "construction_geometry",
            [(0, 0), (length, 0)],
            line_style="centre",
            minimum_sheet="A3",
        ),
    ]
    if subject_id in _BOAT_IDENTITY_SUBJECT_IDS:
        plan.extend(_boat_plan_identity_details(subject_id, length, beam))
    elif sail:
        mast_stations = (0.30, 0.52, 0.72) if "cutty-sark" in subject_id else (0.46,)
        for index, station in enumerate(mast_stations, start=1):
            plan.extend(
                [
                    _circle(
                        f"plan-mast-{index}",
                        "rig",
                        "major_structural_edges",
                        (station * length, 0),
                        0.015 * beam,
                        priority="identity",
                    ),
                    _line(
                        f"plan-yard-{index}",
                        "rig",
                        "major_structural_edges",
                        [
                            (station * length, -0.42 * beam),
                            (station * length, 0.42 * beam),
                        ],
                        minimum_sheet="A4",
                    ),
                ]
            )
        if "cutty-sark" not in subject_id:
            plan.append(
                _line(
                    "plan-boom",
                    "rig",
                    "major_structural_edges",
                    [(0.46 * length, 0), (0.79 * length, 0)],
                    minimum_sheet="A4",
                )
            )
        if "ac75" in subject_id:
            for index, station in enumerate((0.38, 0.62), start=1):
                plan.append(
                    _line(
                        f"plan-foil-arm-{index}",
                        "foils",
                        "accent_feature",
                        [
                            (station * length, -0.72 * beam),
                            (station * length, 0.72 * beam),
                        ],
                        priority="identity",
                    )
                )
    else:
        plan.append(
            _curve(
                "plan-cabin",
                "superstructure",
                "glazing_openings",
                (0.38 * length, 0),
                [
                    (
                        "C",
                        (0.44 * length, -0.29 * beam),
                        (0.69 * length, -0.29 * beam),
                        (0.76 * length, 0),
                    ),
                    (
                        "C",
                        (0.69 * length, 0.29 * beam),
                        (0.44 * length, 0.29 * beam),
                        (0.38 * length, 0),
                    ),
                ],
                closed=True,
                priority="identity",
            )
        )
        plan.append(
            _line(
                "plan-aft-deck",
                "deck",
                "panel_seam_lines",
                [(0.76 * length, -0.31 * beam), (0.76 * length, 0.31 * beam)],
                minimum_sheet="A4",
            )
        )
        if "bluebird-k7" in subject_id:
            plan.extend(
                [
                    _line(
                        "plan-left-sponson",
                        "hydroplane",
                        "principal_silhouette",
                        [
                            (0.15 * length, -0.75 * beam),
                            (0.55 * length, -0.75 * beam),
                            (0.72 * length, -0.30 * beam),
                        ],
                        priority="identity",
                    ),
                    _line(
                        "plan-right-sponson",
                        "hydroplane",
                        "principal_silhouette",
                        [
                            (0.15 * length, 0.75 * beam),
                            (0.55 * length, 0.75 * beam),
                            (0.72 * length, 0.30 * beam),
                        ],
                        priority="identity",
                    ),
                    _line(
                        "plan-tail-fin",
                        "hydroplane",
                        "accent_feature",
                        [(0.78 * length, 0), (0.98 * length, 0)],
                        minimum_sheet="A4",
                    ),
                ]
            )
        elif "pt-109" in subject_id:
            for index, station in enumerate((0.28, 0.55, 0.78), start=1):
                plan.append(
                    _circle(
                        f"plan-gun-station-{index}",
                        "deck-equipment",
                        "mechanical_detail",
                        (station * length, 0),
                        0.07 * beam,
                        minimum_sheet="A4",
                    )
                )

    # The catalogue's vessel dimensions remain verified display facts, but the
    # project-authored contours are explicitly illustrative.  Dimension arrows
    # would imply that local view geometry is a recovered lines plan, so both
    # views stay NTS until source-qualified geometry exists.
    return [
        _view("side", "side", side, [], scale_status="not-to-scale"),
        _view("plan", "plan", plan, [], scale_status="not-to-scale"),
    ]


def load_collection_catalog(collection: str) -> dict[str, Any]:
    if collection not in COLLECTION_CATALOG_PATHS:
        _fail(
            f"unknown collection {collection!r}; choose {', '.join(sorted(COLLECTION_IDS))}."
        )
    path = COLLECTION_CATALOG_PATHS[collection]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(
            f"Cannot read technical collection {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Invalid technical collection JSON {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        _fail(f"{path.name} must be a schema-version-1 object.")
    subjects = payload.get("subjects")
    if not isinstance(subjects, list) or not subjects:
        _fail(f"{path.name} must contain subjects.")
    ids = [str(subject.get("id")) for subject in subjects if isinstance(subject, dict)]
    if len(ids) != len(subjects) or len(ids) != len(set(ids)):
        _fail(f"{path.name} has missing or duplicate subject ids.")
    return payload


def _category(subject: dict[str, Any], catalog: dict[str, Any]) -> str:
    raw = str(subject.get("category", catalog.get("category", "")))
    if raw == "cars":
        return "car"
    if raw in {"boats", "watercraft"}:
        return "boat"
    return raw


def _identity(subject: dict[str, Any]) -> dict[str, Any]:
    raw = subject.get("identity", {})
    if not isinstance(raw, dict) or not raw.get("model"):
        _fail(f"subject {subject.get('id')!r} needs identity.model.")
    allowed = {
        "manufacturer",
        "model",
        "variant",
        "year",
        "configuration",
        "designer",
        "engineer",
        "builder",
        "service",
        "class",
    }
    result = {
        key: _plot_text(value) for key, value in raw.items() if key in allowed and value
    }
    return result


def _compact_title(subject: dict[str, Any]) -> str:
    aliases = {
        "mercedes-benz-300-sl-coupe-w198-1954": "300 SL",
        "jaguar-e-type-series-1-fhc-1961": "E-TYPE",
        "ferrari-f40-1987": "F40",
        "lamborghini-countach-lpi-800-4-2022": "COUNTACH",
        "porsche-911-carrera-992-2-2025": "911",
        "mazda-mx-5-miata-sport-2024": "MX-5",
        "ford-mustang-gt-fastback-2024": "MUSTANG",
        "chevrolet-corvette-stingray-coupe-2024": "CORVETTE",
        "toyota-gr-supra-rz-manual-2025": "GR SUPRA",
        "nissan-z-performance-manual-2024": "NISSAN Z",
        "audi-r8-coupe-v10-performance-quattro-2023": "R8",
        "honda-civic-type-r-2023": "TYPE R",
        "volkswagen-golf-gti-2025": "GOLF GTI",
        "mini-cooper-s-3-door-2024": "MINI",
        "tesla-model-3-performance-2024": "MODEL 3",
        "airbus-a320neo": "A320NEO",
        "airbus-a350-900": "A350-900",
        "airbus-a380-800": "A380-800",
        "boeing-737-8": "737-8",
        "boeing-787-9": "787-9",
        "boeing-747-8-intercontinental": "747-8I",
        "supermarine-spitfire-mk-vc": "SPITFIRE",
        "avro-lancaster-i": "AVRO 683",
        "north-american-p51d-mustang": "P-51D",
        "lockheed-martin-f16c": "F-16C",
        "lockheed-martin-f35a": "F-35A",
        "eurofighter-typhoon-fgr4": "TYPHOON",
        "riva-aquarama-special": "RIVA",
        "bluebird-k7": "BLUEBIRD",
        "rnli-shannon-class": "SHANNON",
        "uscg-47-foot-motor-lifeboat": "USCG 47",
        "ac75-foiling-monohull": "AC75",
        "j-class-endeavour": "J-CLASS",
        "pt-109": "PT-109",
        "cutty-sark": "CUTTY",
        "ss-great-britain": "SSGB",
        "rms-queen-mary": "QUEEN MARY",
    }
    subject_id = str(subject["id"])
    if subject_id in aliases:
        return aliases[subject_id]
    identity = _identity(subject)
    model = str(identity["model"]).upper()
    variant = str(identity.get("variant", "")).upper()
    candidate = (
        variant
        if model.casefold() in variant.casefold()
        else f"{model} {variant}".strip()
    )
    return candidate if variant and len(candidate) <= 28 else model


def compile_collection_subject(
    subject: dict[str, Any],
    *,
    catalog: dict[str, Any],
    format_id: str = "a3-landscape",
    allow_retired_illustrative: bool = False,
) -> dict[str, Any]:
    """Replay one retired review recipe only with an explicit audit opt-in."""

    if not allow_retired_illustrative:
        raise MapPlotterError(_RETIRED_MESSAGE)

    if format_id not in FORMAT_IDS:
        _fail(f"unsupported format {format_id!r}.")
    category = _category(subject, catalog)
    dims = _dimensions(subject, category)
    if category in {"car", "racing-car"}:
        views = _car_views(subject, dims)
    elif category == "aircraft":
        views = _aircraft_views(subject, dims)
    elif category in {"boat", "yacht", "ship", "personal-watercraft"}:
        views = _boat_views(subject, dims)
    else:
        _fail(
            f"collection subject {subject.get('id')!r} has unsupported category {category!r}."
        )

    recipe_payload = {
        "subject_id": subject["id"],
        "category": category,
        "dimensions_mm": dims,
        "shape": subject.get(
            "shape",
            subject.get("shape_parameters", subject.get("geometry_recipe", {})),
        ),
        "template_version": TEMPLATE_VERSION,
    }
    recipe_sha = _canonical_sha(recipe_payload)
    fact_sources = _normalize_fact_sources(subject)
    record: dict[str, Any] = {
        "schema_version": 1,
        "kind": "technical-object",
        "id": str(subject["id"]),
        "category": category,
        "format_id": format_id,
        "preset": "orthographic-collection",
        "title": _compact_title(subject),
        "subtitle": (
            "4-VIEW / ILLUSTRATIVE STUDY"
            if category in {"car", "racing-car"}
            else (
                "3-VIEW / ILLUSTRATIVE STUDY"
                if category == "aircraft"
                else "SIDE + PLAN / ILLUSTRATIVE STUDY"
            )
        ),
        "identity": _identity(subject),
        "source_level": 1,
        "claim_scope": str(
            subject.get("claim_scope")
            or "Original parametric illustration constrained only by cited overall dimensions; local contours are not exact."
        ),
        "rights_status": "project-authored",
        "sources": [
            {
                "id": "original-parametric-geometry",
                "level": 1,
                "kind": "project-authored-parametric-vector",
                "attribution": "City Map Plotter original parametric compiler",
                "visible_credit": "ORIGINAL PARAMETRIC VECTOR STUDY",
                "license": "Project-authored",
                "rights_status": "project-authored",
                "asset_sha256": recipe_sha,
                "method": f"{TEMPLATE_VERSION}; normalized controls expanded deterministically without tracing",
                "view_ids": [str(view["id"]) for view in views],
                "verified_technical": False,
                "rights_cleared_marks": False,
            }
        ],
        "fact_sources": fact_sources,
        "views": views,
        "specifications": _normalize_specifications(subject),
        "history": _normalize_history(subject),
        "style": {
            "density": "rich" if format_id.startswith("a3-") else "medium",
            "show_dimensions": not format_id.startswith("a5-"),
            "show_callouts": False,
            "scale_policy": "fit-each-view",
            "deduplication_policy": "approximate",
            "sub_pen_policy": "report",
        },
        "notes": [
            "ILLUSTRATIVE / NOT CERTIFIED / NOT FOR MAINTENANCE OR FABRICATION.",
            "Official pages support printed facts only; they do not supply the drawn contours.",
            *[str(item) for item in subject.get("limitations", [])],
        ],
        "excluded_features": [
            {
                "kind": "logo",
                "description": "Manufacturer badges, logos, livery and sponsor marks",
                "reason": "The collection uses unbranded original linework.",
            }
        ],
        "collection": {
            "id": str(catalog["catalog_id"]),
            "template": TEMPLATE_VERSION,
            "template_version": "1.0.0",
            "recipe_sha256": recipe_sha,
            # The compiler can render and format-validate this original recipe,
            # but that is deliberately distinct from independently verified,
            # model-specific source geometry suitable for commercial release.
            "geometry_release_ready": False,
            "geometry_review_status": "illustrative-review-only",
        },
        "geometry_sha256": "",
    }
    record["geometry_sha256"] = technical_geometry_sha256(record)
    return validate_technical_record(record)


def load_collection_records(
    collection: str,
    *,
    format_id: str = "a3-landscape",
    allow_retired_illustrative: bool = False,
) -> list[dict[str, Any]]:
    if not allow_retired_illustrative:
        raise MapPlotterError(_RETIRED_MESSAGE)
    catalog = load_collection_catalog(collection)
    return [
        compile_collection_subject(
            copy.deepcopy(subject),
            catalog=catalog,
            format_id=format_id,
            allow_retired_illustrative=True,
        )
        for subject in catalog["subjects"]
    ]


def load_all_collection_records(
    *,
    format_id: str = "a3-landscape",
    allow_retired_illustrative: bool = False,
) -> list[dict[str, Any]]:
    if not allow_retired_illustrative:
        raise MapPlotterError(_RETIRED_MESSAGE)
    records = [
        record
        for collection in ("cars", "aircraft", "boats")
        for record in load_collection_records(
            collection,
            format_id=format_id,
            allow_retired_illustrative=True,
        )
    ]
    ids = [str(record["id"]) for record in records]
    if len(ids) != len(set(ids)):
        _fail("collection subject ids must be globally unique.")
    return records


def compile_retired_illustrative_collection_subject(
    subject: dict[str, Any],
    *,
    catalog: dict[str, Any],
    format_id: str = "a3-landscape",
) -> dict[str, Any]:
    """Replay v1 geometry for regression/audit work, never product artwork."""

    return compile_collection_subject(
        subject,
        catalog=catalog,
        format_id=format_id,
        allow_retired_illustrative=True,
    )


def load_retired_illustrative_collection_records(
    collection: str,
    *,
    format_id: str = "a3-landscape",
) -> list[dict[str, Any]]:
    """Load v1 geometry for regression/audit work, never product artwork."""

    return load_collection_records(
        collection,
        format_id=format_id,
        allow_retired_illustrative=True,
    )


def load_all_retired_illustrative_collection_records(
    *, format_id: str = "a3-landscape"
) -> list[dict[str, Any]]:
    """Load all v1 geometry for regression/audit work, never product artwork."""

    return load_all_collection_records(
        format_id=format_id,
        allow_retired_illustrative=True,
    )


__all__ = [
    "COLLECTION_CATALOG_PATHS",
    "COLLECTION_IDS",
    "FORMAT_IDS",
    "TEMPLATE_VERSION",
    "compile_collection_subject",
    "compile_retired_illustrative_collection_subject",
    "load_all_collection_records",
    "load_all_retired_illustrative_collection_records",
    "load_collection_catalog",
    "load_collection_records",
    "load_retired_illustrative_collection_records",
]

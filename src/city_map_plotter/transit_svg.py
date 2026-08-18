"""Physical, editable SVG output for geographic transit-network plates."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
import hashlib
import json
from math import atan2, ceil, cos, floor, hypot, isfinite, log10, pi, sin, tan
from pathlib import Path
import shutil
import subprocess
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree as ET

from shapely.errors import GEOSException
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point as ShapelyPoint,
    Polygon,
    box,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from . import __version__
from .display_font import display_font_contract, display_text, display_text_width_mm
from .models import MapPlotterError
from .niche_common import PlateContext, Rect
from .pens import ACTUAL_PEN_INVENTORY
from .stroke_font import stroke_text, text_width_mm
from .svgkit import (
    INKSCAPE_NS,
    MAP_NS,
    SODIPODI_NS,
    SVG_COORDINATE_QUANTUM_MM,
    format_measurement,
    path_data,
    physical_group_attributes,
    plot_path_attributes,
    svg_tag,
)
from .transit import TransitLine, TransitNetwork, TransitSource
from .transit_composition import aspect_aware_map_field
from .transit_extent import NAMED_OPERATOR_KINDS
from .transit_topology import (
    LABEL_CLEARANCE_MM,
    LABEL_FRAME_CLEARANCE_MM,
    LABEL_TO_LABEL_GAP_MM,
    SCALE_ROUTE_TARGETS_MM,
    NATIVE_OWNED_NIB_PROMOTION_POLICY_VERSION,
    PlannedContextStroke,
    PlannedRouteStroke,
    RouteWidthPlan,
    TransitPlan,
    assemble_context_trails,
    build_transit_plan,
)


Point = tuple[float, float]
Stroke = list[Point]


@dataclass(frozen=True, slots=True)
class _WaterSurface:
    """One source-qualified polygonal water surface in paper space."""

    geometry: Polygon
    feature_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    source_objects: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _WaterDot:
    centre: Point
    points: tuple[Point, ...]
    feature_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    source_objects: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PhysicalRouteExpansion:
    """Physical paths plus the machine-auditable method used to obtain them."""

    paths: tuple[tuple[int, float, tuple[Point, ...]], ...]
    offset_method: str
    exact_local_union_certified: bool
    local_physical_union_width_status: str
    stroke_index_complete: bool
    segment_offset_coverage_certified: bool
    maximum_segment_offset_coverage_error_mm: float
    maximum_segment_normal_error_mm: float
    maximum_join_sampling_error_mm: float
    hairpin_join_count: int
    cyclic_path_status: str = "not-applicable"
    source_excursion_bound_certified: bool = False
    maximum_source_excursion_upper_bound_mm: float = 0.0
    source_excursion_bound_mm: float = 0.0


MINIMUM_VISIBLE_CREDIT_CAP_MM = 2.0
ODBL_LICENCE_NAME = "Open Data Commons Open Database Licence 1.0"
ODBL_LICENCE_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
ODBL_VISIBLE_LICENCE_URI = "opendatacommons.org/licenses/odbl/1-0/"
OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"

# Reuse the established university/city-map physical stipple.  A plotter
# cannot reproduce a CSS fill or a pen tap reliably, so every dot is an
# explicit closed octagon.  Its path length clears the three-nib floor of the
# actual Blue 0.25 pen.
WATER_TREATMENT_POLICY_VERSION = "transit-water-treatment-v1"
WATER_DOT_SPACING_MM = 2.2
WATER_DOT_DIAMETER_MM = 0.32
WATER_DOT_VERTICES = 8
WATER_DOT_NIB_MM = 0.25
WATER_DOT_INK_BUDGET_FIELD_FRACTION = 0.02
WATER_DOT_SERIALIZATION_ALLOWANCE_MM = 0.005
WATER_LINE_INTERIOR_SUPPRESSION_MM = 0.005

# Context is subtracted from the actual physical route-pass envelope, not from
# a globally inflated logical corridor.  The tenth-millimetre safety margin
# closes the declared 0.20 mm inter-line gap when two route envelopes meet but
# never adds a broad paper-coloured gutter around an unrelated single route.
ROUTE_CONTEXT_CLEARANCE_MM = 0.10
ROUTE_BAND_POLICY_VERSION = "transit-physical-route-band-v2"
TRANSPORT_DETAIL_EXCEPTION_KINDS = frozenset(
    {
        "roads-major",
        "roads-secondary",
        "roads-local",
        "roads-other",
        "road-areas",
        "paths",
        "railways",
    }
)
TRANSPORT_DETAIL_EXCEPTION_SCALE_TIERS = frozenset(
    {
        "compact-network",
        "urban-network",
    }
)
PAPER_SCALE_CONTEXT_FLOOR_TIERS = frozenset(
    {
        "regional-network",
        "national-network",
    }
)
MINIMUM_NONDEGENERATE_CONTEXT_LENGTH_MM = 1e-6
TRANSPORT_DETAIL_EXCEPTION_POLICY_VERSION = (
    "transit-scale-aware-source-backed-subfloor-transport-v2"
)
CONTEXT_PHYSICAL_FLOOR_POLICY_VERSION = (
    "transit-university-parity-paper-scale-context-floor-v1"
)
MINIMUM_ABSOLUTE_CONTEXT_LENGTH_MM = 0.5
MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM = 1e-6
ROUTE_DETAIL_EXCEPTION_POLICY_VERSION = "transit-source-backed-subfloor-route-v1"
ROUTE_RECORD_ID_POLICY_VERSION = "transit-physical-route-record-v1"
SHORT_ROUTE_OFFSET_METHOD = "short-fragment-rigid-normal-offset"
STANDARD_ROUTE_OFFSET_METHOD = "geos-offset-curve"
SEGMENT_NORMAL_ROUTE_OFFSET_METHOD = "source-segment-normal-smooth-join-review-offset"
CYCLIC_OFFSET_STATUS_NOT_APPLICABLE = "not-applicable"
CYCLIC_OFFSET_STATUS_CERTIFIED = (
    "certified-closed-one-continuous-path-per-stroke-index"
)
PHYSICAL_UNION_WIDTH_SCOPE = "straight-locally-parallel-runs"
CERTIFIED_LOCAL_UNION_STATUS = "certified-within-declared-scope"
NOMINAL_LOCAL_UNION_STATUS = "nominal-review-required-not-exactly-certified"
HAIRPIN_MINIMUM_TURN_RADIANS = 150.0 * pi / 180.0
JOIN_MAXIMUM_STEP_RADIANS = pi / 24.0
ROUTE_SEGMENT_OFFSET_SAMPLE_FRACTIONS = (0.25, 0.5, 0.75)
ROUTE_SEGMENT_OFFSET_COVERAGE_TOLERANCE_MM = 5e-6
SVG_COORDINATE_QUANTIZATION_BUDGET_MM = hypot(
    SVG_COORDINATE_QUANTUM_MM / 2.0,
    SVG_COORDINATE_QUANTUM_MM / 2.0,
)
SERIALIZED_SEGMENT_OFFSET_COVERAGE_TOLERANCE_MM = (
    SVG_COORDINATE_QUANTIZATION_BUDGET_MM
    + ROUTE_SEGMENT_OFFSET_COVERAGE_TOLERANCE_MM
    + 5e-6
)
# The tangent-matched cubic used for an inside turn has controls at most
# 4|offset|/3 along the incident tangents.  Those controls are therefore at
# most 5|offset|/3 from the source vertex; a Bezier lies in its control hull.
# Circular outside joins remain exactly |offset| from the vertex.  This gives
# the review fallback a finite physical envelope even when a tight cyclic
# inside offset has no simple mathematical parallel curve.
SEGMENT_NORMAL_SOURCE_EXCURSION_FACTOR = 5.0 / 3.0
SEGMENT_NORMAL_SOURCE_EXCURSION_TOLERANCE_MM = 1e-9

CONTEXT_STYLE: dict[str, tuple[str, str, float]] = {
    # The established university/marathon house palette is the one source of
    # truth for map hierarchy. Transit adds routes above it; it does not invent
    # a pale substitute basemap.
    "roads-major": ("red-0-4", "#CF807A", 0.4),
    # National motorway/trunk orientation is intentionally a hairline.  It is
    # not the same semantic or physical hierarchy as an urban major road.
    "roads-strategic": ("red-0-25", "#CF807A", 0.25),
    "roads-secondary": ("red-0-25", "#D99A94", 0.25),
    "roads-local": ("grey-0-25", "#7F8992", 0.25),
    "roads-other": ("grey-0-25", "#939CA4", 0.25),
    "road-areas": ("grey-0-25", "#A4ABB1", 0.25),
    "paths": ("grey-0-25", "#A2A9AF", 0.25),
    "railways": ("grey-0-25", "#67737D", 0.25),
    "water-lines": ("blue-0-25", "#78B4D3", 0.25),
    "water-areas": ("blue-0-4", "#6EADD0", 0.4),
    "coastline": ("blue-0-4", "#6EADD0", 0.4),
    "green-space": ("green-0-25", "#82B89A", 0.25),
    "buildings": ("purple-0-25", "#98769F", 0.25),
    "boundaries": ("grey-0-25", "#939CA4", 0.25),
}

# At national A3 distance the 0.40 mm blue coastline is the only immediate
# geographic silhouette beneath the route.  Use a less washed-out preview for
# that role so the 150 dpi proof reflects the visibility of the owned Blue
# 0.40 pen.  This changes screen simulation only: the physical pen, nib and
# emitted geometry remain exactly those in CONTEXT_STYLE.
NATIONAL_OPERATOR_CONTEXT_PREVIEW_OVERRIDES_BY_SCALE = {
    "national-network": {"coastline": "#4F8EAD"},
}

CONTEXT_KIND_ALIASES = {
    "roads_major": "roads-major",
    "roads_strategic": "roads-strategic",
    "roads_secondary": "roads-secondary",
    "roads_local": "roads-local",
    "roads_other": "roads-other",
    "water_lines": "water-lines",
    "water_areas": "water-areas",
    "rivers": "water-lines",
    "waterways": "water-lines",
    "green_space": "green-space",
    "boundary": "boundaries",
    "road_areas": "road-areas",
}

CONTEXT_BY_SCALE = {
    "compact-network": frozenset(CONTEXT_STYLE),
    "urban-network": frozenset(CONTEXT_STYLE),
    "regional-network": frozenset(
        {
            "roads-major",
            "roads-strategic",
            "roads-secondary",
            "railways",
            "water-lines",
            "water-areas",
            "coastline",
            "green-space",
            "buildings",
            "boundaries",
        }
    ),
    "national-network": frozenset(
        {
            "roads-major",
            "roads-strategic",
            "railways",
            "water-lines",
            "water-areas",
            "coastline",
            "boundaries",
        }
    ),
}

# A named operator is already the transport hierarchy on the sheet. Drawing
# every physical railway underneath it repeats the subject without proving
# service ownership. At regional paper scale, primary-road linework has the
# same problem: it is denser and physically heavier than the operator route,
# while motorways/trunk roads alone retain useful orientation. At national
# scale even that road network and inland-water detail compete with the route
# and the country outline, so the backdrop becomes a geographic skeleton of
# coast and boundaries only. This is deterministic selection by semantic role,
# not silent geometry deletion: every excluded source feature remains in the
# contract and is accounted for by the scale-omission ledger.
NATIONAL_OPERATOR_CONTEXT_BY_SCALE = {
    "compact-network": CONTEXT_BY_SCALE["compact-network"],
    "urban-network": frozenset(
        {
            "roads-major",
            "roads-strategic",
            "roads-secondary",
            "water-lines",
            "water-areas",
            "coastline",
            "green-space",
            "boundaries",
        }
    ),
    "regional-network": frozenset(
        {
            "roads-strategic",
            "water-lines",
            "water-areas",
            "coastline",
            "boundaries",
        }
    ),
    "national-network": frozenset(
        {
            "coastline",
            "boundaries",
        }
    ),
}
NATIONAL_OPERATOR_KINDS = frozenset({"national-operator", "national-operator-overview"})


def _allowed_context_for(network: TransitNetwork, scale_tier: str) -> frozenset[str]:
    """Return the truthful paper-scale context vocabulary for one plate."""

    policy = (
        NATIONAL_OPERATOR_CONTEXT_BY_SCALE
        if network.kind in NATIONAL_OPERATOR_KINDS
        else CONTEXT_BY_SCALE
    )
    try:
        return policy[scale_tier]
    except KeyError as exc:  # pragma: no cover - scale_tier is an internal enum.
        raise MapPlotterError(f"Unknown transit scale tier {scale_tier!r}.") from exc


def _context_preview_for(network: TransitNetwork, scale_tier: str, kind: str) -> str:
    """Return a role-aware proof colour without changing physical pen data."""

    default = CONTEXT_STYLE[kind][1]
    if network.kind not in NATIONAL_OPERATOR_KINDS:
        return default
    return NATIONAL_OPERATOR_CONTEXT_PREVIEW_OVERRIDES_BY_SCALE.get(scale_tier, {}).get(
        kind, default
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _length(points: Sequence[Point]) -> float:
    return sum(
        hypot(second[0] - first[0], second[1] - first[1])
        for first, second in zip(points, points[1:])
    )


def _serialized_points(points: Iterable[Sequence[float]]) -> tuple[Point, ...]:
    """Return the canonical 0.001 mm coordinates written to the master SVG.

    Quantisation can collapse many adjacent source vertices onto the same paper
    coordinate, especially on national plates.  Re-emitting those zero-length
    commands makes files unnecessarily large and can make a real plotter dwell
    on one point.  Canonical SVG geometry therefore removes only *consecutive*
    duplicates after quantisation; non-consecutive returns and a closing vertex
    remain untouched because they carry real topology.
    """

    output: list[Point] = []
    for point in points:
        serialized = (float(f"{point[0]:.3f}"), float(f"{point[1]:.3f}"))
        if not output or output[-1] != serialized:
            output.append(serialized)
    return tuple(output)


def _context_physical_floor_mm(kind: str) -> float:
    return max(
        MINIMUM_ABSOLUTE_CONTEXT_LENGTH_MM,
        3.0 * _context_nib_mm(kind),
    )


def _context_nib_mm(kind: str) -> float:
    """Resolve the binding mark width from the pen actually emitted."""

    pen_id = CONTEXT_STYLE[kind][0]
    return float(_actual_pen(pen_id).mark_width_mm)


def _retains_subfloor_transport_context(*, scale_tier: str, kind: str) -> bool:
    return (
        scale_tier in TRANSPORT_DETAIL_EXCEPTION_SCALE_TIERS
        and kind in TRANSPORT_DETAIL_EXCEPTION_KINDS
    )


def _counted_noun(count: int, singular: str) -> str:
    return f"{count} {singular if count == 1 else singular + 'S'}"


def _circle(centre: Point, radius: float, segments: int = 28) -> Stroke:
    return [
        (
            centre[0] + radius * cos(2.0 * pi * index / segments),
            centre[1] + radius * sin(2.0 * pi * index / segments),
        )
        for index in range(segments + 1)
    ]


def _actual_pen(pen_id: str) -> Any:
    matches = [pen for pen in ACTUAL_PEN_INVENTORY.pens if pen.identity == pen_id]
    if len(matches) != 1:
        raise MapPlotterError(f"Transit furniture asks for unknown pen {pen_id!r}.")
    return matches[0]


def _physical_attributes(
    *,
    pen_id: str,
    ink: str,
    nib_mm: float,
    calibration_state: str,
    match_status: str,
) -> dict[str, str]:
    attributes = physical_group_attributes(
        ink=ink,
        nib_mm=nib_mm,
        nominal_nib_mm=nib_mm,
        strokes=1,
        passes=1,
        plotted_width_mm=nib_mm,
        requested_width_mm=nib_mm,
        width_fit_mode="single-nib",
        pen_profile=ACTUAL_PEN_INVENTORY.id,
        pen_id=pen_id,
        calibration_state=calibration_state,
    )
    # Keep the singular v1 spellings while shared physical metadata migrates
    # consumers to `data-plot-strokes`/`data-plot-passes`.
    attributes.update(
        {
            "data-plot-stroke-count": "1",
            "data-plot-pass-count": "1",
            "data-plot-colour-match": match_status,
        }
    )
    return attributes


def _append_path(
    group: ET.Element,
    points: Sequence[Point],
    *,
    attributes: dict[str, str] | None = None,
) -> ET.Element:
    serialized = _serialized_points(points)
    if len(serialized) < 2:
        raise MapPlotterError(
            "Transit SVG cannot emit a path that collapses below two distinct "
            "0.001 mm coordinates."
        )
    values = {
        "d": path_data(list(serialized)),
        "fill": "none",
        "vector-effect": "non-scaling-stroke",
    }
    values.update(attributes or {})
    return ET.SubElement(group, svg_tag("path"), values)


def _fit_display_text(
    text: str,
    zone: Rect,
    *,
    preferred_height_mm: float,
    minimum_height_mm: float = 3.2,
) -> list[Stroke]:
    words = text.upper().split()
    if not words:
        return []
    candidates: list[tuple[float, list[str]]] = []
    all_sets: list[list[str]] = [[" ".join(words)]]
    all_sets.extend(
        [" ".join(words[:index]), " ".join(words[index:])]
        for index in range(1, len(words))
    )
    for lines in all_sets:
        line_gap = 0.18 * preferred_height_mm
        height_fit = (zone.height - line_gap * (len(lines) - 1)) / len(lines)
        width_fit = min(
            preferred_height_mm
            * zone.width
            / max(display_text_width_mm(line, height_mm=preferred_height_mm), 1e-9)
            for line in lines
        )
        resolved = min(preferred_height_mm, height_fit, width_fit)
        if resolved + 1e-9 >= minimum_height_mm:
            candidates.append((resolved, lines))
    if not candidates:
        raise MapPlotterError(f"Transit title {text!r} cannot fit its binding zone.")
    height, lines = max(candidates, key=lambda item: (item[0], -len(item[1])))
    gap = 0.18 * height
    block = len(lines) * height + (len(lines) - 1) * gap
    y = zone.y + (zone.height - block) / 2.0
    result: list[Stroke] = []
    for index, line in enumerate(lines):
        result.extend(
            display_text(
                line,
                x_mm=zone.centre[0],
                y_mm=y + index * (height + gap),
                height_mm=height,
                anchor="middle",
            )
        )
    return result


def _fit_stroke_text(
    text: str,
    *,
    x: float,
    y: float,
    preferred_cap_mm: float,
    maximum_width_mm: float,
    anchor: str = "start",
    minimum_cap_mm: float = 2.0,
) -> tuple[list[Stroke], float]:
    natural = text_width_mm(text, cap_height_mm=preferred_cap_mm)
    cap = min(
        preferred_cap_mm, preferred_cap_mm * maximum_width_mm / max(natural, 1e-9)
    )
    if cap + 1e-9 < minimum_cap_mm:
        raise MapPlotterError(
            f"Transit copy {text!r} needs a {cap:.3f} mm cap, below the "
            f"{minimum_cap_mm:g} mm physical floor."
        )
    return stroke_text(text, x_mm=x, y_mm=y, height_mm=cap, anchor=anchor), cap


def _wrap_stroke_text(
    text: str,
    *,
    cap_height_mm: float,
    maximum_width_mm: float,
) -> list[str]:
    """Wrap exact visible credit copy without shrinking below its nib floor."""

    words = text.upper().split()
    if not words:
        return []

    # Source and licence URIs are required visible copy for some ODbL
    # Produced Works.  A URI is one whitespace token, but making the entire
    # attribution zone wide enough for every possible URI would destroy the
    # composition.  Break an overlong token without deleting or inserting any
    # characters, preferring URI punctuation as the physical line boundary.
    # Concatenating the emitted chunks therefore recovers the exact token.
    expanded_words: list[tuple[str, bool]] = []
    for word in words:
        if text_width_mm(word, cap_height_mm=cap_height_mm) <= maximum_width_mm:
            expanded_words.append((word, False))
            continue
        remaining = word
        first_chunk = True
        while remaining:
            maximum_end = 0
            for end in range(1, len(remaining) + 1):
                if (
                    text_width_mm(remaining[:end], cap_height_mm=cap_height_mm)
                    <= maximum_width_mm
                ):
                    maximum_end = end
                else:
                    break
            if maximum_end == 0:
                raise MapPlotterError(
                    f"Transit credit token {remaining!r} cannot fit its zone."
                )
            if maximum_end < len(remaining):
                preferred = [
                    index
                    for index in range(1, maximum_end + 1)
                    if remaining[index - 1] in "/._-:?=&"
                ]
                # Avoid tiny fragments when the only punctuation is near the
                # beginning (for example ``HTTPS://``).
                sensible = [index for index in preferred if index >= maximum_end // 2]
                end = sensible[-1] if sensible else maximum_end
            else:
                end = maximum_end
            expanded_words.append((remaining[:end], not first_chunk))
            remaining = remaining[end:]
            first_chunk = False

    lines: list[str] = []
    current = ""
    for word, continuation in expanded_words:
        if not current:
            current = word
            continue
        candidate = f"{current}{'' if continuation else ' '}{word}"
        if text_width_mm(candidate, cap_height_mm=cap_height_mm) <= maximum_width_mm:
            current = candidate
            continue
        lines.append(current)
        current = word
    lines.append(current)
    return lines


def _is_odbl_source(source: TransitSource) -> bool:
    licence = source.licence.casefold()
    return "odbl" in licence or (
        "open database" in licence and ("licence" in licence or "license" in licence)
    )


def _visible_credit_records(
    sources: Sequence[TransitSource],
) -> tuple[list[dict[str, Any]], str]:
    """Build exact, physical credit copy plus ODbL Produced Work notices.

    ODbL 1.0 section 4.3 requires a public Produced Work to identify the
    database and the licence.  A pen plot cannot carry hyperlinks, so both
    URIs are emitted as visible copy.  Publisher-specified attribution is
    preserved verbatim before those renderer-supplied notices.
    """

    records: list[dict[str, Any]] = []
    visible_entries: list[str] = []
    for source in sources:
        parts = [source.attribution]
        odbl = _is_odbl_source(source)
        attribution_lower = source.attribution.casefold()
        if (
            "openstreetmap" in attribution_lower
            and "openstreetmap.org" not in attribution_lower
        ):
            parts.append(OSM_COPYRIGHT_URL)
        if odbl:
            current = " / ".join(parts).casefold()
            if source.url.casefold() not in current:
                parts.append(f"DATABASE SOURCE {source.id}: {source.url}")
            current = " / ".join(parts).casefold()
            if (
                "odbl" not in current
                or "1.0" not in current
                or "opendatacommons.org/licenses/odbl/" not in current
            ):
                parts.append(f"ODbL 1.0: {ODBL_VISIBLE_LICENCE_URI}")
        visible_text = " / ".join(parts)
        visible_lower = visible_text.casefold()
        requires_osm_url = "openstreetmap" in attribution_lower
        record = {
            "source_id": source.id,
            "publisher_attribution": source.attribution,
            "visible_text": visible_text,
            "publisher_attribution_preserved": source.attribution in visible_text,
            "odbl": odbl,
            "database_uri_visible": not odbl or source.url.casefold() in visible_lower,
            "odbl_licence_notice_visible": not odbl
            or (
                "odbl" in visible_lower
                and "1.0" in visible_lower
                and "opendatacommons.org/licenses/odbl/" in visible_lower
            ),
            "osm_credit_uri_visible": not requires_osm_url
            or "openstreetmap.org" in visible_lower,
        }
        record["complete"] = all(
            (
                record["publisher_attribution_preserved"],
                record["database_uri_visible"],
                record["odbl_licence_notice_visible"],
                record["osm_credit_uri_visible"],
            )
        )
        records.append(record)
        if visible_text not in visible_entries:
            visible_entries.append(visible_text)
    return records, " / ".join(visible_entries)


def _rights_gate(
    network: TransitNetwork,
    *,
    visible_credit_records: Sequence[dict[str, Any]],
    visible_credit_cap_mm: float,
) -> tuple[dict[str, Any], list[str]]:
    """Return a fail-closed rights ledger and its production blockers."""

    permission_required = sorted(
        source.id
        for source in network.sources
        if source.commercial_reuse_status == "permission-required"
    )
    review_required = sorted(
        source.id
        for source in network.sources
        if source.commercial_reuse_status in {"review-required", "unknown"}
    )
    expired = sorted(
        source.id
        for source in network.sources
        if source.valid_to is not None and source.valid_to < date.today().isoformat()
    )
    odbl_sources = sorted(
        source.id for source in network.sources if _is_odbl_source(source)
    )
    odbl_without_version = sorted(
        source.id
        for source in network.sources
        if _is_odbl_source(source) and "1.0" not in source.licence.casefold()
    )
    incomplete_credit_sources = sorted(
        str(record["source_id"])
        for record in visible_credit_records
        if record.get("complete") is not True
    )
    credit_meets_floor = visible_credit_cap_mm + 1e-9 >= MINIMUM_VISIBLE_CREDIT_CAP_MM

    blockers: list[str] = []
    if permission_required:
        blockers.append(
            "source permission is required before commercial production: "
            + ", ".join(permission_required)
        )
    if review_required:
        blockers.append(
            "commercial rights review is incomplete for sources: "
            + ", ".join(review_required)
        )
    if expired:
        blockers.append("source validity has expired: " + ", ".join(expired))
    if odbl_without_version:
        blockers.append(
            "ODbL source licence text does not declare version 1.0: "
            + ", ".join(odbl_without_version)
        )
    if odbl_sources:
        blockers.append(
            "ODbL public-use and distribution obligations require release review "
            "(Produced Work notice and, when triggered, Derivative Database "
            "share-alike/access): " + ", ".join(odbl_sources)
        )
    if incomplete_credit_sources or not credit_meets_floor:
        detail = (
            ", ".join(incomplete_credit_sources)
            if incomplete_credit_sources
            else "physical cap-height floor"
        )
        blockers.append("visible source credit is incomplete or illegible: " + detail)

    credit_by_id = {
        str(record["source_id"]): record for record in visible_credit_records
    }
    source_assessments: list[dict[str, Any]] = []
    for source in network.sources:
        source_blockers: list[str] = []
        if source.id in permission_required:
            source_blockers.append("permission-required")
        if source.id in review_required:
            source_blockers.append("rights-review-required")
        if source.id in expired:
            source_blockers.append("source-expired")
        if source.id in odbl_sources:
            source_blockers.append("odbl-distribution-review-required")
        if source.id in incomplete_credit_sources:
            source_blockers.append("visible-credit-incomplete")
        source_assessments.append(
            {
                "source_id": source.id,
                "commercial_reuse_status": source.commercial_reuse_status,
                "licence": source.licence,
                "attribution": source.attribution,
                "odbl": source.id in odbl_sources,
                "visible_credit_complete": bool(
                    credit_by_id.get(source.id, {}).get("complete", False)
                ),
                "production_blocking": bool(source_blockers),
                "blocker_codes": source_blockers,
            }
        )

    rights = {
        "policy_version": "transit-rights-gate-v1",
        "acquisition_gate": {
            "status": "not-carried-by-render-contract",
            "enabled_means": "source acquisition and contract compilation only",
            "confers_commercial_clearance": False,
            "confers_sellable_production_approval": False,
        },
        "operator_reference_policy": {
            "diagram_geometry_traced": False,
            "logo_or_trade_dress_used": False,
            "permission_required_source_ids": permission_required,
            "permission_required_remains_production_blocking": bool(
                permission_required
            ),
        },
        "odbl": {
            "applies": bool(odbl_sources),
            "source_ids": odbl_sources,
            "licence_name": ODBL_LICENCE_NAME,
            "licence_url": ODBL_LICENCE_URL,
            "artifact_classification_for_gate": "Produced Work",
            "public_use_notice": {
                "required": bool(odbl_sources),
                "status": "satisfied-in-visible-artifact"
                if odbl_sources and not incomplete_credit_sources and credit_meets_floor
                else ("not-applicable" if not odbl_sources else "blocked"),
            },
            "public_derivative_database_share_alike": {
                "conditional": True,
                "status": "release-review-required"
                if odbl_sources
                else "not-applicable",
            },
            "derivative_database_access_or_alteration_file": {
                "conditional": True,
                "status": "release-review-required"
                if odbl_sources
                else "not-applicable",
            },
            "database_redistribution_notice_and_keep-open": {
                "conditional": True,
                "status": "release-review-required"
                if odbl_sources
                else "not-applicable",
            },
        },
        "visible_credit": {
            "complete": not incomplete_credit_sources and credit_meets_floor,
            "minimum_cap_height_mm": MINIMUM_VISIBLE_CREDIT_CAP_MM,
            "actual_cap_height_mm": visible_credit_cap_mm,
            "source_ids": [source.id for source in network.sources],
            "incomplete_source_ids": incomplete_credit_sources,
        },
        "permission_required_source_ids": permission_required,
        "review_required_source_ids": review_required,
        "expired_source_ids": expired,
        "source_assessments": source_assessments,
        "rights_ready_for_production": not blockers,
    }
    return rights, blockers


def _line_pen(line: TransitLine, width_plan: RouteWidthPlan) -> dict[str, Any]:
    fit = width_plan.fit
    preview = fit.pen.preview_color or line.colour.display_hex
    return {
        "plot_key": fit.pen.identity,
        "pen_id": fit.pen.identity,
        "ink": fit.pen.ink,
        "nib_mm": fit.pen.mark_width_mm,
        "nominal_nib_mm": fit.pen.nominal_nib_mm,
        "calibration_state": fit.pen.calibration_state,
        "match_status": line.pen.match_status,
        "preview": preview,
        "label": f"{fit.pen.ink} {fit.pen.nominal_nib_mm:g} mm",
        "requested_width_mm": fit.requested_width_mm,
        "plotted_width_mm": fit.plotted_width_mm,
        "stroke_count": fit.stroke_count,
        "offset_pitch_mm": fit.offset_pitch_mm,
        "width_fit_mode": fit.mode,
        "width_fit_error_mm": fit.width_error_mm,
    }


def _route_physical_union_width_mm(width_plan: RouteWidthPlan) -> float:
    """Width of the actual adjacent-pass union on a straight run."""

    fit = width_plan.fit
    return fit.pen.mark_width_mm + (fit.stroke_count - 1) * fit.offset_pitch_mm


def _route_adjacent_overlap_mm(width_plan: RouteWidthPlan) -> float | None:
    """Physical overlap of neighbouring passes, or ``None`` for one pass."""

    fit = width_plan.fit
    if fit.stroke_count == 1:
        return None
    return fit.pen.mark_width_mm - fit.offset_pitch_mm


def _route_band_evidence(
    width_plan: RouteWidthPlan,
) -> dict[str, float | str | None]:
    overlap_mm = _route_adjacent_overlap_mm(width_plan)
    return {
        "physical_union_width_mm": round(_route_physical_union_width_mm(width_plan), 6),
        "physical_union_width_scope": PHYSICAL_UNION_WIDTH_SCOPE,
        "adjacent_pass_overlap_mm": (
            round(overlap_mm, 6) if overlap_mm is not None else None
        ),
    }


def _physical_pen_colour_collisions(
    network: TransitNetwork,
    width_plans: dict[str, RouteWidthPlan],
) -> list[dict[str, Any]]:
    """Return physical pens asked to stand in for distinct reference colours."""

    grouped: dict[str, list[TransitLine]] = {}
    for line in network.lines:
        grouped.setdefault(
            _line_pen(line, width_plans[line.id])["plot_key"], []
        ).append(line)
    result: list[dict[str, Any]] = []
    for plot_key, lines in sorted(grouped.items()):
        display_colours = sorted({line.colour.display_hex.upper() for line in lines})
        if len(display_colours) < 2:
            continue
        ordered = sorted(lines, key=lambda line: (line.order, line.id))
        physical = _line_pen(ordered[0], width_plans[ordered[0].id])
        result.append(
            {
                "plot_key": plot_key,
                "physical_pen_id": physical["pen_id"],
                "ink": physical["ink"],
                "nominal_nib_mm": physical["nominal_nib_mm"],
                "line_ids": [line.id for line in ordered],
                "reference_display_colours": display_colours,
                "display_colour_to_physical_pen": [
                    {
                        "line_id": line.id,
                        "display_colour": line.colour.display_hex.upper(),
                        "physical_pen_id": physical["pen_id"],
                        "physical_ink": physical["ink"],
                        "physical_nib_mm": physical["nominal_nib_mm"],
                    }
                    for line in ordered
                ],
                "resolution_status": "requires-distinct-calibrated-physical-pens",
            }
        )
    return result


def _builtin_pen(pen_id: str, preview: str | None = None) -> dict[str, Any]:
    pen = _actual_pen(pen_id)
    return {
        "plot_key": pen.identity,
        "pen_id": pen.identity,
        "ink": pen.ink,
        "nib_mm": pen.mark_width_mm,
        "calibration_state": pen.calibration_state,
        "match_status": "nominal-unmeasured",
        "preview": preview or pen.preview_color or "#18181B",
        "label": pen.label,
    }


def _black_pen_for_width(nib_mm: float, *, preview: str = "#24282B") -> dict[str, Any]:
    matches = [
        pen
        for pen in ACTUAL_PEN_INVENTORY.pens
        if pen.ink.casefold() == "black" and abs(pen.mark_width_mm - nib_mm) <= 1e-9
    ]
    if len(matches) != 1:
        raise MapPlotterError(
            f"Transit format asks for an unavailable Black {nib_mm:g} mm pen."
        )
    return _builtin_pen(matches[0].identity, preview)


def _format_role_width(
    context: PlateContext,
    role: str,
    *,
    map_linework: bool = False,
) -> float:
    ladder_name = "map_linework_nib_mm" if map_linework else "nib_roles_mm"
    ladder = context.plate.get(ladder_name, {})
    try:
        return float(ladder[role])
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Transit format {context.format_id!r} has no {ladder_name}.{role}."
        ) from exc


def _context_key(value: str) -> str:
    return CONTEXT_KIND_ALIASES.get(value, value)


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry] if not geometry.is_empty and geometry.area > 1e-9 else []
    if isinstance(geometry, MultiPolygon):
        return [
            item for item in geometry.geoms if not item.is_empty and item.area > 1e-9
        ]
    if isinstance(geometry, GeometryCollection):
        return [polygon for item in geometry.geoms for polygon in _polygon_parts(item)]
    return []


def _line_parts(geometry: BaseGeometry) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [geometry] if not geometry.is_empty else []
    if isinstance(geometry, MultiLineString):
        return [item for item in geometry.geoms if not item.is_empty]
    if isinstance(geometry, GeometryCollection):
        return [line for item in geometry.geoms for line in _line_parts(item)]
    return []


def _physical_route_paths(
    points: Sequence[Point],
    width_plan: RouteWidthPlan,
    *,
    route_record_id: str | None = None,
    line_id: str | None = None,
) -> list[tuple[int, float, tuple[Point, ...]]]:
    """Expand one logical route centreline into honest adjacent pen strokes."""

    return list(
        _physical_route_expansion(
            points,
            width_plan,
            route_record_id=route_record_id,
            line_id=line_id,
        ).paths
    )


def _physical_route_expansion(
    points: Sequence[Point],
    width_plan: RouteWidthPlan,
    *,
    route_record_id: str | None = None,
    line_id: str | None = None,
) -> _PhysicalRouteExpansion:
    """Expand a route and retain exact, independently auditable method evidence."""

    centreline = LineString(points)
    # A route edge is evidence, not optional decoration.  The general
    # three-nib handling floor therefore cannot delete a non-degenerate route
    # fragment: very short fragments remain physical paths and are labelled as
    # review-required by the caller.  This is the route equivalent of the
    # source-backed transport-context detail exception.
    if centreline.length <= MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM:
        return _PhysicalRouteExpansion(
            paths=(),
            offset_method=STANDARD_ROUTE_OFFSET_METHOD,
            exact_local_union_certified=False,
            local_physical_union_width_status=NOMINAL_LOCAL_UNION_STATUS,
            stroke_index_complete=False,
            segment_offset_coverage_certified=False,
            maximum_segment_offset_coverage_error_mm=0.0,
            maximum_segment_normal_error_mm=0.0,
            maximum_join_sampling_error_mm=0.0,
            hairpin_join_count=0,
        )
    output: list[tuple[int, float, tuple[Point, ...]]] = []
    indexed_offsets = list(enumerate(width_plan.fit.offset_positions()))
    # Draw outside-in. This changes no geometry or pen count, but lets each
    # later interior pass cover the antialiased inside edge of the earlier pass
    # in SVG/PNG previews. Stroke indexes retain their canonical offset order.
    indexed_offsets.sort(key=lambda item: (-abs(item[1]), item[1]))
    minimum_length_mm = 3.0 * width_plan.fit.pen.mark_width_mm
    if centreline.length + 1e-9 < minimum_length_mm:
        normal_x, normal_y = _short_route_rigid_normal(points)
        rigid_paths = tuple(
            (
                stroke_index,
                distance,
                tuple(
                    (
                        float(x + normal_x * distance),
                        float(y + normal_y * distance),
                    )
                    for x, y in points
                ),
            )
            for stroke_index, distance in indexed_offsets
        )
        rigid_coverage_error_mm = _maximum_segment_offset_coverage_error_mm(
            points, rigid_paths
        )
        return _PhysicalRouteExpansion(
            paths=rigid_paths,
            offset_method=SHORT_ROUTE_OFFSET_METHOD,
            exact_local_union_certified=False,
            local_physical_union_width_status=NOMINAL_LOCAL_UNION_STATUS,
            stroke_index_complete=True,
            segment_offset_coverage_certified=(
                rigid_coverage_error_mm <= ROUTE_SEGMENT_OFFSET_COVERAGE_TOLERANCE_MM
            ),
            maximum_segment_offset_coverage_error_mm=rigid_coverage_error_mm,
            maximum_segment_normal_error_mm=0.0,
            maximum_join_sampling_error_mm=0.0,
            hairpin_join_count=0,
        )
    for stroke_index, distance in indexed_offsets:
        if abs(distance) <= 1e-9:
            pieces = [centreline]
        else:
            try:
                shifted = centreline.offset_curve(
                    distance, quad_segs=4, join_style="round"
                )
            except GEOSException:  # pragma: no cover - defensive GEOS failure
                pieces = []
            else:
                pieces = _line_parts(shifted)
        for piece in pieces:
            if piece.length <= MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM:
                continue
            output.append(
                (
                    stroke_index,
                    distance,
                    tuple((float(x), float(y)) for x, y in piece.coords),
                )
            )
    expected_indexes = {index for index, _ in indexed_offsets}
    actual_indexes = {index for index, _, _ in output}
    path_counts_by_index = {
        index: sum(path_index == index for path_index, _, _ in output)
        for index in expected_indexes
    }
    geos_coverage_error_mm = _maximum_segment_offset_coverage_error_mm(points, output)
    if (
        actual_indexes == expected_indexes
        and all(count == 1 for count in path_counts_by_index.values())
        and geos_coverage_error_mm <= ROUTE_SEGMENT_OFFSET_COVERAGE_TOLERANCE_MM
    ):
        return _PhysicalRouteExpansion(
            paths=tuple(output),
            offset_method=STANDARD_ROUTE_OFFSET_METHOD,
            exact_local_union_certified=True,
            local_physical_union_width_status=CERTIFIED_LOCAL_UNION_STATUS,
            stroke_index_complete=True,
            segment_offset_coverage_certified=True,
            maximum_segment_offset_coverage_error_mm=geos_coverage_error_mm,
            maximum_segment_normal_error_mm=0.0,
            maximum_join_sampling_error_mm=0.0,
            hairpin_join_count=0,
        )

    try:
        fallback = _segment_normal_review_expansion(points, indexed_offsets)
    except ValueError as exc:
        fallback = None
        fallback_error = str(exc)
    else:
        fallback_error = "source-segment review expansion returned no result"
    if fallback is not None:
        return fallback

    record_context = ", ".join(
        item
        for item in (
            f"record {route_record_id}" if route_record_id else None,
            f"line {line_id}" if line_id else None,
            f"length {centreline.length:.9f} mm",
        )
        if item is not None
    )
    missing_indexes = sorted(expected_indexes - actual_indexes)
    missing_offsets = [
        round(distance, 9)
        for index, distance in indexed_offsets
        if index in set(missing_indexes)
    ]
    raise MapPlotterError(
        "Physical route offset coverage failed for a non-short route "
        f"({record_context}): missing indexes {missing_indexes} at offsets "
        f"{missing_offsets} mm, unexpected indexes "
        f"{sorted(actual_indexes - expected_indexes)}, maximum shifted "
        f"source-segment sample error {geos_coverage_error_mm:.9f} mm; "
        f"physical path counts by stroke index {path_counts_by_index}; "
        f"bounded source-segment fallback unavailable: {fallback_error}."
    )


def _maximum_segment_offset_coverage_error_mm(
    points: Sequence[Point],
    paths: Sequence[tuple[int, float, tuple[Point, ...]]],
) -> float:
    """Measure planned shifted source-segment samples against emitted pieces."""

    by_stroke_index: dict[int, list[LineString]] = {}
    offsets_by_stroke_index: dict[int, float] = {}
    for stroke_index, distance, physical_points in paths:
        if len(physical_points) < 2:
            continue
        existing_distance = offsets_by_stroke_index.setdefault(stroke_index, distance)
        if abs(existing_distance - distance) > 1e-12:
            return float("inf")
        geometry = LineString(physical_points)
        if not geometry.is_empty and geometry.length > (
            MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM
        ):
            by_stroke_index.setdefault(stroke_index, []).append(geometry)
    if not by_stroke_index:
        return float("inf")

    source_segments: list[tuple[Point, Point, Point]] = []
    for first, second in zip(points, points[1:]):
        delta_x = second[0] - first[0]
        delta_y = second[1] - first[1]
        length = hypot(delta_x, delta_y)
        if length <= MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM:
            continue
        source_segments.append((first, second, (-delta_y / length, delta_x / length)))
    if not source_segments:
        return float("inf")

    maximum_error_mm = 0.0
    for stroke_index, pieces in by_stroke_index.items():
        distance = offsets_by_stroke_index[stroke_index]
        emitted = unary_union(pieces)
        for first, second, normal in source_segments:
            delta_x = second[0] - first[0]
            delta_y = second[1] - first[1]
            for fraction in ROUTE_SEGMENT_OFFSET_SAMPLE_FRACTIONS:
                expected = ShapelyPoint(
                    first[0] + delta_x * fraction + normal[0] * distance,
                    first[1] + delta_y * fraction + normal[1] * distance,
                )
                maximum_error_mm = max(maximum_error_mm, expected.distance(emitted))
    return maximum_error_mm


def _segment_normal_review_expansion(
    points: Sequence[Point],
    indexed_offsets: Sequence[tuple[int, float]],
) -> _PhysicalRouteExpansion | None:
    """Return a complete, physically bounded source-segment offset.

    Each source segment is translated by its own signed unit normal. Outside
    joins use a tangent circular arc. Inside joins use a sampled cubic whose
    endpoint derivatives match both source-segment directions. Every planned
    pass is one continuous pen-down polyline; no source segment is trimmed. A
    simple closed source cycle gets the same construction at its final/first
    join, yielding one explicitly closed pen-down path for every stroke index.

    This preserves every source segment and makes topologically joined,
    rounded plot paths, but their physical union is deliberately labelled
    nominal/review-required rather than being misrepresented as exact.
    """

    cleaned: list[Point] = []
    for point in points:
        candidate = (float(point[0]), float(point[1]))
        if not all(isfinite(value) for value in candidate):
            raise ValueError("source route contains a non-finite coordinate")
        if (
            not cleaned
            or hypot(candidate[0] - cleaned[-1][0], candidate[1] - cleaned[-1][1])
            > MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM
        ):
            cleaned.append(candidate)
    if len(cleaned) < 2:
        raise ValueError("fewer than two distinct source vertices")
    is_cyclic = (
        hypot(
            cleaned[0][0] - cleaned[-1][0],
            cleaned[0][1] - cleaned[-1][1],
        )
        <= MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM
    )
    if is_cyclic:
        # Normalize the seam exactly before constructing the final/first join.
        # The bounded fallback deliberately supports only a simple source
        # cycle.  A self-crossing or zero-area walk has no unambiguous inside,
        # so it continues to fail closed instead of inventing a traversal.
        cleaned[-1] = cleaned[0]
        if len(cleaned) < 4:
            raise ValueError("closed route fallback needs at least three segments")
        source_cycle = LineString(cleaned)
        source_polygon = Polygon(cleaned)
        if not source_cycle.is_ring or not source_cycle.is_simple:
            raise ValueError(
                "closed route fallback requires a simple non-self-intersecting cycle"
            )
        if (
            not source_polygon.is_valid
            or source_polygon.area <= MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM**2
        ):
            raise ValueError("closed route fallback requires a valid non-zero-area cycle")

    expected_indexes = {stroke_index for stroke_index, _ in indexed_offsets}
    if len(expected_indexes) != len(indexed_offsets):
        raise ValueError("planned stroke indexes are not unique")
    if not indexed_offsets:
        raise ValueError("no planned physical stroke indexes")
    if any(not isfinite(distance) for _, distance in indexed_offsets):
        raise ValueError("planned physical offset is non-finite")

    paths: list[tuple[int, float, tuple[Point, ...]]] = []
    maximum_normal_error_mm = 0.0
    maximum_chord_error_mm = 0.0
    maximum_source_excursion_upper_bound_mm = 0.0
    maximum_source_excursion_bound_mm = 0.0
    hairpin_count = 0
    for stroke_index, distance in indexed_offsets:
        physical_pieces: tuple[tuple[Point, ...], ...]
        if abs(distance) <= 1e-9:
            physical_pieces = (tuple(cleaned),)
            normal_error_mm = 0.0
            chord_error_mm = 0.0
            path_hairpin_count = 0
            source_excursion_upper_bound_mm = 0.0
        else:
            (
                physical_pieces,
                normal_error_mm,
                chord_error_mm,
                path_hairpin_count,
                source_excursion_upper_bound_mm,
            ) = _segment_normal_smooth_join_offset(cleaned, distance)
        for physical_piece in physical_pieces:
            if _length(physical_piece) <= MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM:
                raise ValueError(f"offset {distance:g} mm became degenerate")
            if any(
                not isfinite(value)
                for physical_point in physical_piece
                for value in physical_point
            ):
                raise ValueError(f"offset {distance:g} mm became non-finite")
            if is_cyclic and (
                hypot(
                    physical_piece[0][0] - physical_piece[-1][0],
                    physical_piece[0][1] - physical_piece[-1][1],
                )
                > MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM
            ):
                raise ValueError(f"offset {distance:g} mm did not close cyclically")
            source_excursion_bound_mm = (
                abs(distance) * SEGMENT_NORMAL_SOURCE_EXCURSION_FACTOR
                + SEGMENT_NORMAL_SOURCE_EXCURSION_TOLERANCE_MM
            )
            if source_excursion_upper_bound_mm > source_excursion_bound_mm:
                raise ValueError(
                    f"offset {distance:g} mm exceeded its bounded source "
                    "excursion by "
                    f"{source_excursion_upper_bound_mm - source_excursion_bound_mm:.9f} mm"
                )
            paths.append((stroke_index, distance, physical_piece))
            maximum_source_excursion_upper_bound_mm = max(
                maximum_source_excursion_upper_bound_mm,
                source_excursion_upper_bound_mm,
            )
            maximum_source_excursion_bound_mm = max(
                maximum_source_excursion_bound_mm, source_excursion_bound_mm
            )
        maximum_normal_error_mm = max(maximum_normal_error_mm, normal_error_mm)
        maximum_chord_error_mm = max(maximum_chord_error_mm, chord_error_mm)
        hairpin_count += path_hairpin_count

    actual_indexes = {stroke_index for stroke_index, _, _ in paths}
    path_counts_by_index = {
        stroke_index: sum(
            path_stroke_index == stroke_index
            for path_stroke_index, _, _ in paths
        )
        for stroke_index in expected_indexes
    }
    if actual_indexes != expected_indexes or any(
        count != 1 for count in path_counts_by_index.values()
    ):
        raise ValueError(
            "source-segment fallback did not produce exactly one continuous "
            "path for every planned stroke index"
        )

    coverage_error_mm = _maximum_segment_offset_coverage_error_mm(points, paths)
    if coverage_error_mm > ROUTE_SEGMENT_OFFSET_COVERAGE_TOLERANCE_MM:
        raise ValueError(
            "source-segment fallback failed shifted-sample coverage by "
            f"{coverage_error_mm:.9f} mm"
        )
    return _PhysicalRouteExpansion(
        paths=tuple(paths),
        offset_method=SEGMENT_NORMAL_ROUTE_OFFSET_METHOD,
        exact_local_union_certified=False,
        local_physical_union_width_status=NOMINAL_LOCAL_UNION_STATUS,
        stroke_index_complete=True,
        segment_offset_coverage_certified=True,
        maximum_segment_offset_coverage_error_mm=coverage_error_mm,
        maximum_segment_normal_error_mm=maximum_normal_error_mm,
        maximum_join_sampling_error_mm=maximum_chord_error_mm,
        hairpin_join_count=hairpin_count,
        cyclic_path_status=(
            CYCLIC_OFFSET_STATUS_CERTIFIED
            if is_cyclic
            else CYCLIC_OFFSET_STATUS_NOT_APPLICABLE
        ),
        source_excursion_bound_certified=True,
        maximum_source_excursion_upper_bound_mm=(
            maximum_source_excursion_upper_bound_mm
        ),
        source_excursion_bound_mm=maximum_source_excursion_bound_mm,
    )


def _segment_normal_smooth_join_offset(
    points: Sequence[Point], distance: float
) -> tuple[tuple[tuple[Point, ...], ...], float, float, int, float]:
    """Construct one continuous complete-segment path with smooth joins.

    Every shifted source segment is retained end-to-end. Outside joins use a
    tangent circular arc. Inside joins use a sampled cubic whose endpoint
    derivatives match the incoming and outgoing source-segment directions.
    Coincident endpoints are concatenated so each stroke index needs one
    pen-down path. For a simple explicitly closed source path the last segment
    is also joined to the first, and the emitted seam is normalized exactly.
    Self-overlap is retained rather than simplified away.
    """

    is_cyclic = (
        len(points) >= 2
        and hypot(
            points[0][0] - points[-1][0],
            points[0][1] - points[-1][1],
        )
        <= MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM
    )

    directions: list[Point] = []
    normals: list[Point] = []
    for first, second in zip(points, points[1:]):
        delta_x = second[0] - first[0]
        delta_y = second[1] - first[1]
        length = hypot(delta_x, delta_y)
        if length <= MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM:
            raise ValueError("repeated source vertex survived normalization")
        direction = (delta_x / length, delta_y / length)
        directions.append(direction)
        normals.append((-direction[1], direction[0]))

    def shifted(point: Point, normal: Point) -> Point:
        return (
            point[0] + normal[0] * distance,
            point[1] + normal[1] * distance,
        )

    def cross(first: Point, second: Point) -> float:
        return first[0] * second[1] - first[1] * second[0]

    def subtract(first: Point, second: Point) -> Point:
        return (first[0] - second[0], first[1] - second[1])

    def point_segment_distance(point: Point, first: Point, second: Point) -> float:
        delta_x = second[0] - first[0]
        delta_y = second[1] - first[1]
        denominator = delta_x * delta_x + delta_y * delta_y
        if denominator <= 1e-24:
            return hypot(point[0] - first[0], point[1] - first[1])
        fraction = max(
            0.0,
            min(
                1.0,
                ((point[0] - first[0]) * delta_x + (point[1] - first[1]) * delta_y)
                / denominator,
            ),
        )
        nearest = (
            first[0] + fraction * delta_x,
            first[1] + fraction * delta_y,
        )
        return hypot(point[0] - nearest[0], point[1] - nearest[1])

    def cubic_point(
        start: Point,
        first_control: Point,
        second_control: Point,
        end: Point,
        fraction: float,
    ) -> Point:
        inverse = 1.0 - fraction
        return (
            inverse**3 * start[0]
            + 3.0 * inverse**2 * fraction * first_control[0]
            + 3.0 * inverse * fraction**2 * second_control[0]
            + fraction**3 * end[0],
            inverse**3 * start[1]
            + 3.0 * inverse**2 * fraction * first_control[1]
            + 3.0 * inverse * fraction**2 * second_control[1]
            + fraction**3 * end[1],
        )

    segment_pieces: list[tuple[Point, ...]] = []
    for index, (first, second) in enumerate(zip(points, points[1:])):
        segment_pieces.append(
            (
                shifted(first, normals[index]),
                shifted(second, normals[index]),
            )
        )

    join_pieces: dict[int, tuple[Point, ...]] = {}
    hairpin_count = 0
    maximum_chord_error_mm = 0.0
    # Distance to an incident source vertex is a conservative upper bound on
    # distance to the complete source line.  Computing it while each join's
    # source vertex is known avoids an O(source vertices x emitted samples)
    # global nearest-distance audit on large national route records.
    maximum_source_excursion_upper_bound_mm = abs(distance)
    join_count = len(directions) if is_cyclic else max(0, len(directions) - 1)
    for index in range(join_count):
        following_index = (index + 1) % len(directions)
        previous = directions[index]
        following = directions[following_index]
        vertex = points[index + 1]
        incoming = shifted(vertex, normals[index])
        outgoing = shifted(vertex, normals[following_index])
        turn_cross = cross(previous, following)
        turn_dot = previous[0] * following[0] + previous[1] * following[1]
        if abs(turn_cross) <= 1e-12 and turn_dot < 0.0:
            # Make the mathematically ambiguous exact reversal deterministic.
            turn_radians = pi
        else:
            turn_radians = atan2(turn_cross, turn_dot)
        if abs(turn_radians) <= 1e-12:
            continue

        if turn_radians * distance > 0.0:
            # A complete inside offset cannot use the circular short arc: its
            # start tangent points against the incoming source segment. This
            # local cubic keeps both full segments and is C1 at both ends.
            control_length = (
                4.0 * abs(distance) * abs(tan(abs(turn_radians) / 4.0)) / 3.0
            )
            first_control = (
                incoming[0] + previous[0] * control_length,
                incoming[1] + previous[1] * control_length,
            )
            second_control = (
                outgoing[0] - following[0] * control_length,
                outgoing[1] - following[1] * control_length,
            )
            step_count = max(32, ceil(abs(turn_radians) / JOIN_MAXIMUM_STEP_RADIANS))
            join = tuple(
                cubic_point(
                    incoming,
                    first_control,
                    second_control,
                    outgoing,
                    step / step_count,
                )
                for step in range(step_count + 1)
            )
            for step in range(step_count):
                midpoint = cubic_point(
                    incoming,
                    first_control,
                    second_control,
                    outgoing,
                    (step + 0.5) / step_count,
                )
                maximum_chord_error_mm = max(
                    maximum_chord_error_mm,
                    point_segment_distance(midpoint, join[step], join[step + 1]),
                )
            join_pieces[index] = join
            maximum_source_excursion_upper_bound_mm = max(
                maximum_source_excursion_upper_bound_mm,
                max(
                    hypot(point[0] - vertex[0], point[1] - vertex[1])
                    for point in join
                ),
            )
            if abs(turn_radians) + 1e-12 >= HAIRPIN_MINIMUM_TURN_RADIANS:
                hairpin_count += 1
            continue

        # Outside of the bend: the circular short sweep is tangent to both
        # translated source segments.
        sweep_radians = turn_radians
        start_angle = atan2(incoming[1] - vertex[1], incoming[0] - vertex[0])
        step_count = max(1, ceil(abs(sweep_radians) / JOIN_MAXIMUM_STEP_RADIANS))
        actual_step = abs(sweep_radians) / step_count
        maximum_chord_error_mm = max(
            maximum_chord_error_mm,
            abs(distance) * (1.0 - cos(actual_step / 2.0)),
        )
        arc: list[Point] = [incoming]
        for step in range(1, step_count + 1):
            angle = start_angle + sweep_radians * step / step_count
            arc.append(
                (
                    vertex[0] + abs(distance) * cos(angle),
                    vertex[1] + abs(distance) * sin(angle),
                )
            )
        arc[-1] = outgoing
        if _length(arc) > MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM:
            join_pieces[index] = tuple(arc)
            maximum_source_excursion_upper_bound_mm = max(
                maximum_source_excursion_upper_bound_mm,
                max(
                    hypot(point[0] - vertex[0], point[1] - vertex[1])
                    for point in arc
                ),
            )

    maximum_normal_error_mm = 0.0
    for index, segment in enumerate(segment_pieces):
        for candidate in segment:
            from_source = subtract(candidate, points[index])
            signed_normal_mm = cross(directions[index], from_source)
            maximum_normal_error_mm = max(
                maximum_normal_error_mm,
                abs(signed_normal_mm - distance),
            )
    combined: list[Point] = []
    for index, segment in enumerate(segment_pieces):
        for point in segment:
            if (
                not combined
                or hypot(point[0] - combined[-1][0], point[1] - combined[-1][1])
                > MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM
            ):
                combined.append(point)
        for point in join_pieces.get(index, ()):
            if (
                not combined
                or hypot(point[0] - combined[-1][0], point[1] - combined[-1][1])
                > MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM
            ):
                combined.append(point)
    if is_cyclic:
        if len(combined) < 4:
            raise ValueError("cyclic offset became degenerate")
        seam_error_mm = hypot(
            combined[0][0] - combined[-1][0],
            combined[0][1] - combined[-1][1],
        )
        if seam_error_mm > MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM:
            raise ValueError(
                f"cyclic offset seam remained open by {seam_error_mm:.9f} mm"
            )
        combined[-1] = combined[0]
    return (
        (tuple(combined),),
        maximum_normal_error_mm,
        maximum_chord_error_mm,
        hairpin_count,
        maximum_source_excursion_upper_bound_mm,
    )


def _short_route_rigid_normal(points: Sequence[Point]) -> Point:
    """Return one deterministic unit normal for a short route fragment."""

    start_x, start_y = points[0]
    end_x, end_y = points[-1]
    direction_x = end_x - start_x
    direction_y = end_y - start_y
    direction_length = hypot(direction_x, direction_y)
    if direction_length <= MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM:
        longest_length = 0.0
        for first, second in zip(points, points[1:]):
            candidate_x = second[0] - first[0]
            candidate_y = second[1] - first[1]
            candidate_length = hypot(candidate_x, candidate_y)
            if candidate_length > longest_length:
                direction_x = candidate_x
                direction_y = candidate_y
                longest_length = candidate_length
        direction_length = longest_length
    if direction_length <= MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM:
        raise MapPlotterError(
            "Cannot derive a physical offset normal for a degenerate route record."
        )
    return (-direction_y / direction_length, direction_x / direction_length)


def _route_record_id(record: PlannedRouteStroke) -> str:
    payload = json.dumps(
        {
            "line_id": record.line_id,
            "start_node_id": record.start_node_id,
            "end_node_id": record.end_node_id,
            "representative_edge_ids": list(record.edge_ids),
            "source_membership_edge_ids": list(record.source_membership_edge_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "route-record-" + hashlib.sha256(payload).hexdigest()[:24]


def _actual_emitted_route_paths_by_line(
    root: ET.Element,
    line_ids: Sequence[str],
) -> dict[str, tuple[ET.Element, ...]]:
    """Return actual physical route paths, excluding furniture samples."""

    known = set(line_ids)
    paths: dict[str, list[ET.Element]] = {line_id: [] for line_id in line_ids}
    for path in root.iter(svg_tag("path")):
        line_id = path.get("data-transit-line-id")
        # Legend samples deliberately carry their line identity but are not
        # route evidence.  Only plot paths have the physical nib attribute.
        if line_id in known and path.get("data-plot-nib-mm") is not None:
            paths[line_id].append(path)
    return {line_id: tuple(items) for line_id, items in paths.items()}


def _actual_emitted_route_memberships_by_line(
    root: ET.Element,
    line_ids: Sequence[str],
) -> dict[str, frozenset[str]]:
    """Read authoritative source-edge membership from emitted SVG paths."""

    return {
        line_id: frozenset(
            edge_id
            for path in paths
            for edge_id in path.get("data-transit-edge-ids", "").split()
        )
        for line_id, paths in _actual_emitted_route_paths_by_line(
            root, line_ids
        ).items()
    }


def _actual_emitted_route_records_by_line(
    root: ET.Element,
    line_ids: Sequence[str],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Group actual physical SVG paths by their stable logical record ID."""

    records: dict[str, dict[str, dict[str, Any]]] = {
        line_id: {} for line_id in line_ids
    }
    for line_id, paths in _actual_emitted_route_paths_by_line(root, line_ids).items():
        for path in paths:
            record_id = path.get("data-transit-route-record-id")
            if not record_id:
                raise MapPlotterError(
                    f"Physical SVG route path for {line_id} lacks a route-record ID."
                )
            try:
                stroke_index = int(path.get("data-physical-stroke-index", ""))
                stroke_count = int(path.get("data-physical-stroke-count", ""))
            except ValueError as exc:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} has invalid stroke evidence."
                ) from exc
            method = path.get("data-physical-offset-method")
            width_scope = path.get("data-physical-union-width-scope")
            local_width_status = path.get("data-local-physical-union-width-status")
            exact_local_union = path.get("data-exact-local-union-certified")
            stroke_index_complete = path.get("data-stroke-index-complete")
            segment_coverage_certified = path.get(
                "data-segment-offset-coverage-certified"
            )
            cyclic_path_status = path.get("data-cyclic-path-status")
            source_excursion_bound_certified = path.get(
                "data-source-excursion-bound-certified"
            )
            review_required = path.get("data-review-required")
            continuous_per_stroke = path.get(
                "data-one-continuous-path-per-stroke-index"
            )
            pen_lift_within_stroke = path.get("data-pen-lift-within-stroke-index")
            record = records[line_id].setdefault(
                record_id,
                {
                    "stroke_indexes": set(),
                    "declared_stroke_count": stroke_count,
                    "physical_path_count": 0,
                    "offset_method": method,
                    "physical_union_width_scope": width_scope,
                    "local_physical_union_width_status": local_width_status,
                    "exact_local_union_certified": exact_local_union,
                    "stroke_index_complete": stroke_index_complete,
                    "segment_offset_coverage_certified": (segment_coverage_certified),
                    "cyclic_path_status": cyclic_path_status,
                    "source_excursion_bound_certified": (
                        source_excursion_bound_certified
                    ),
                    "review_required": review_required,
                    "continuous_per_stroke": continuous_per_stroke,
                    "pen_lift_within_stroke": pen_lift_within_stroke,
                },
            )
            if record["declared_stroke_count"] != stroke_count:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on stroke count."
                )
            if record["offset_method"] != method:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on offset method."
                )
            if record["physical_union_width_scope"] != width_scope:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on width scope."
                )
            if record["local_physical_union_width_status"] != local_width_status:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on local "
                    "width status."
                )
            if record["exact_local_union_certified"] != exact_local_union:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on exact "
                    "local union certification."
                )
            if record["stroke_index_complete"] != stroke_index_complete:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on "
                    "stroke-index completeness."
                )
            if (
                record["segment_offset_coverage_certified"]
                != segment_coverage_certified
            ):
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on "
                    "segment-offset coverage."
                )
            if record["cyclic_path_status"] != cyclic_path_status:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on cyclic "
                    "path status."
                )
            if (
                record["source_excursion_bound_certified"]
                != source_excursion_bound_certified
            ):
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on source-"
                    "excursion certification."
                )
            if record["review_required"] != review_required:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on review status."
                )
            if record["continuous_per_stroke"] != continuous_per_stroke:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on continuity."
                )
            if record["pen_lift_within_stroke"] != pen_lift_within_stroke:
                raise MapPlotterError(
                    f"Physical SVG route record {record_id} disagrees on pen lifts."
                )
            record["stroke_indexes"].add(stroke_index)
            record["physical_path_count"] += 1
    return records


def _water_surface_records(
    strokes: Sequence[PlannedContextStroke],
    *,
    field: Rect,
    apply_visible_area_floor: bool,
) -> tuple[list[_WaterSurface], dict[str, Any]]:
    """Build only source-declared, valid closed water polygons.

    OSM multipolygon inner rings are separate context records.  They are
    assigned only to a valid containing outer from the same source object;
    an unassigned inner is never promoted to water.  Open/clipped banks stay
    drawable as bank lines but cannot silently become a stipple surface.
    """

    candidates = [
        stroke for stroke in strokes if _context_key(stroke.kind) == "water-areas"
    ]
    outers: dict[tuple[str, str], list[_WaterSurface]] = {}
    inners: dict[tuple[str, str], list[_WaterSurface]] = {}
    closed_count = 0
    invalid_count = 0
    open_count = 0
    source_valid_surface_count = 0
    map_field_clipped_surface_count = 0
    physical_floor_rejected_surface_ids: list[str] = []
    physical_floor_rejection_entries: list[dict[str, Any]] = []
    for stroke in sorted(candidates, key=lambda item: item.feature_id):
        points = _serialized_points(stroke.points)
        if len(points) < 4 or points[0] != points[-1]:
            open_count += 1
            continue
        closed_count += 1
        polygon = Polygon(points)
        if polygon.is_empty or polygon.area <= 1e-9 or not polygon.is_valid:
            invalid_count += 1
            continue
        source_root = stroke.source_object.split("#", 1)[0]
        key = (stroke.source_ref, source_root)
        is_inner = "#inner:" in stroke.source_object
        record = _WaterSurface(
            geometry=polygon,
            feature_ids=(stroke.feature_id,),
            source_refs=(stroke.source_ref,),
            source_objects=(stroke.source_object,),
        )
        target = inners if is_inner else outers
        target.setdefault(key, []).append(record)

    surfaces: list[_WaterSurface] = []
    field_geometry = box(field.left, field.top, field.right, field.bottom)
    minimum_area_mm2 = (2.0 * _context_nib_mm("water-areas")) ** 2
    assigned_inner_ids: set[str] = set()
    for key, outer_records in sorted(outers.items()):
        inner_records = inners.get(key, [])
        for outer in outer_records:
            assigned = [
                inner
                for inner in inner_records
                if outer.geometry.covers(inner.geometry.representative_point())
            ]
            assigned_inner_ids.update(
                feature_id for inner in assigned for feature_id in inner.feature_ids
            )
            geometry: BaseGeometry = outer.geometry
            if assigned:
                geometry = geometry.difference(
                    unary_union([inner.geometry for inner in assigned])
                )
            lineage_feature_ids = tuple(
                sorted(
                    {
                        *outer.feature_ids,
                        *(
                            feature_id
                            for inner in assigned
                            for feature_id in inner.feature_ids
                        ),
                    }
                )
            )
            lineage_source_refs = tuple(
                sorted(
                    {
                        *outer.source_refs,
                        *(
                            source_ref
                            for inner in assigned
                            for source_ref in inner.source_refs
                        ),
                    }
                )
            )
            lineage_source_objects = tuple(
                sorted(
                    {
                        *outer.source_objects,
                        *(
                            source_object
                            for inner in assigned
                            for source_object in inner.source_objects
                        ),
                    }
                )
            )
            source_parts = _polygon_parts(geometry)
            source_valid_surface_count += len(source_parts)
            for source_part_index, source_polygon in enumerate(source_parts):
                visible = (
                    source_polygon.intersection(field_geometry)
                    if apply_visible_area_floor
                    else source_polygon
                )
                if not source_polygon.equals(visible):
                    map_field_clipped_surface_count += 1
                for visible_part_index, visible_polygon in enumerate(
                    _polygon_parts(visible)
                ):
                    exterior = _serialized_points(visible_polygon.exterior.coords)
                    holes = [
                        _serialized_points(interior.coords)
                        for interior in visible_polygon.interiors
                    ]
                    serialized_polygon = Polygon(exterior, holes)
                    if (
                        serialized_polygon.is_empty
                        or not serialized_polygon.is_valid
                        or serialized_polygon.area <= 1e-9
                    ):
                        invalid_count += 1
                        continue
                    if serialized_polygon.area + 1e-12 < minimum_area_mm2:
                        physical_floor_rejected_surface_ids.extend(lineage_feature_ids)
                        physical_floor_rejection_entries.append(
                            {
                                "surface_id": (
                                    f"{outer.feature_ids[0]}-visible-surface-"
                                    f"{source_part_index}-{visible_part_index}"
                                ),
                                "represented_feature_ids": list(lineage_feature_ids),
                                "source_refs": list(lineage_source_refs),
                                "source_objects": list(lineage_source_objects),
                                "measured_serialized_area_mm2": round(
                                    serialized_polygon.area, 6
                                ),
                                "required_minimum_area_mm2": round(minimum_area_mm2, 6),
                                "reason": "below-minimum-visible-water-surface-area",
                            }
                        )
                        continue
                    surfaces.append(
                        _WaterSurface(
                            geometry=serialized_polygon,
                            feature_ids=lineage_feature_ids,
                            source_refs=lineage_source_refs,
                            source_objects=lineage_source_objects,
                        )
                    )

    surface_feature_ids = sorted(
        {feature_id for surface in surfaces for feature_id in surface.feature_ids}
    )
    unassigned_inner_ids = sorted(
        {
            feature_id
            for records in inners.values()
            for record in records
            for feature_id in record.feature_ids
        }
        - assigned_inner_ids
    )
    return surfaces, {
        "input_bank_feature_count": len(candidates),
        "closed_ring_count": closed_count,
        "open_or_clipped_ring_count": open_count,
        "invalid_closed_ring_count": invalid_count,
        "physical_floor_policy_version": CONTEXT_PHYSICAL_FLOOR_POLICY_VERSION,
        "visible_area_floor_active": apply_visible_area_floor,
        "source_valid_surface_polygon_count": source_valid_surface_count,
        "map_field_clipped_surface_count": map_field_clipped_surface_count,
        "physical_floor_rejected_visible_surface_count": len(
            physical_floor_rejection_entries
        ),
        "physical_floor_rejected_visible_surface_feature_ids": sorted(
            set(physical_floor_rejected_surface_ids)
        ),
        "physical_floor_rejection_ledger_sha256": hashlib.sha256(
            json.dumps(
                physical_floor_rejection_entries,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "stipple_and_centreline_suppression_use_only_clipped_floor_eligible_surfaces": True,
        "valid_surface_polygon_count": len(surfaces),
        "valid_surface_lineage_feature_count": len(surface_feature_ids),
        "valid_surface_lineage_sha256": hashlib.sha256(
            json.dumps(surface_feature_ids, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "unassigned_inner_ring_count": len(unassigned_inner_ids),
        "unassigned_inner_ring_feature_ids": unassigned_inner_ids,
        "open_and_invalid_rings_remain_bank_outlines": True,
    }


def _suppress_water_lines_inside_surfaces(
    strokes: Sequence[PlannedContextStroke],
    water_surface: BaseGeometry,
) -> tuple[list[PlannedContextStroke], dict[str, str], dict[str, Any]]:
    """Remove a water centreline only on proven polygonal water interior."""

    suppression_surface = (
        water_surface.buffer(-WATER_LINE_INTERIOR_SUPPRESSION_MM)
        if not water_surface.is_empty
        else water_surface
    )
    output: list[PlannedContextStroke] = []
    disposition_by_feature_id: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    input_count = partial_count = full_count = unchanged_count = 0
    input_length = emitted_length = suppressed_length = 0.0
    for stroke in strokes:
        if _context_key(stroke.kind) != "water-lines":
            output.append(stroke)
            continue
        input_count += 1
        geometry = LineString(stroke.points)
        original_length = geometry.length
        input_length += original_length
        difference = (
            geometry.difference(suppression_surface)
            if not suppression_surface.is_empty
            else geometry
        )
        pieces = [piece for piece in _line_parts(difference) if piece.length > 1e-9]
        retained_length = sum(piece.length for piece in pieces)
        removed_length = max(0.0, original_length - retained_length)
        if removed_length <= 1e-6:
            disposition = "plain-linear-water-no-area-overlap"
            unchanged_count += 1
            pieces = [geometry]
            retained_length = original_length
            removed_length = 0.0
        elif retained_length <= 1e-6:
            disposition = "fully-suppressed-inside-sourced-water-area"
            full_count += 1
            pieces = []
            retained_length = 0.0
            removed_length = original_length
        else:
            disposition = "partially-suppressed-inside-sourced-water-area"
            partial_count += 1
        emitted_length += retained_length
        suppressed_length += removed_length
        emitted_ids: list[str] = []
        for piece_index, piece in enumerate(pieces):
            feature_id = (
                stroke.feature_id
                if disposition == "plain-linear-water-no-area-overlap"
                else f"{stroke.feature_id}-outside-water-area-{piece_index}"
            )
            emitted_ids.append(feature_id)
            disposition_by_feature_id[feature_id] = disposition
            output.append(
                replace(
                    stroke,
                    feature_id=feature_id,
                    kind="water-lines",
                    points=tuple((float(x), float(y)) for x, y in piece.coords),
                    represented_feature_ids=stroke.feature_ids,
                    represented_source_refs=stroke.source_refs,
                    represented_source_objects=stroke.source_objects,
                )
            )
        entries.append(
            {
                "feature_id": stroke.feature_id,
                "source_ref": stroke.source_ref,
                "source_object": stroke.source_object,
                "disposition": disposition,
                "input_length_mm": round(original_length, 6),
                "emitted_length_mm": round(retained_length, 6),
                "suppressed_length_mm": round(removed_length, 6),
                "emitted_feature_ids": emitted_ids,
            }
        )
    return (
        output,
        disposition_by_feature_id,
        {
            "policy": "subtract-line-only-from-valid-sourced-water-polygon-interior",
            "interior_epsilon_mm": WATER_LINE_INTERIOR_SUPPRESSION_MM,
            "input_path_count": input_count,
            "unchanged_plain_line_count": unchanged_count,
            "partially_suppressed_path_count": partial_count,
            "fully_suppressed_path_count": full_count,
            "input_length_mm": round(input_length, 6),
            "emitted_before_knockout_length_mm": round(emitted_length, 6),
            "suppressed_inside_area_length_mm": round(suppressed_length, 6),
            "length_accounting_error_mm": round(
                abs(input_length - emitted_length - suppressed_length), 9
            ),
            "sourced_area_geometry_required": True,
            "source_width_inference_used": False,
            "narrow_linear_water_without_sourced_width": "plain-blue-0-25-line",
            "fully_suppressed_feature_ids": sorted(
                entry["feature_id"]
                for entry in entries
                if entry["disposition"] == "fully-suppressed-inside-sourced-water-area"
            ),
            "entries": entries,
            "ledger_sha256": hashlib.sha256(
                json.dumps(entries, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
        },
    )


def _context_knockout_geometry(plan: TransitPlan) -> BaseGeometry:
    cutters: list[BaseGeometry] = []
    route_envelopes: list[BaseGeometry] = []
    width_plans = plan.route_width_by_line
    for stroke in plan.route_strokes:
        width_plan = width_plans[stroke.line_id]
        radius_mm = width_plan.fit.pen.mark_width_mm / 2.0 + ROUTE_CONTEXT_CLEARANCE_MM
        for _, _, physical_points in _physical_route_paths(stroke.points, width_plan):
            route_envelopes.append(
                LineString(physical_points).buffer(
                    radius_mm,
                    cap_style="round",
                    join_style="round",
                )
            )
    if route_envelopes:
        cutters.append(unary_union(route_envelopes))
    for mark in plan.station_marks:
        cutters.append(
            box(
                mark.point[0] - 1.25,
                mark.point[1] - 1.25,
                mark.point[0] + 1.25,
                mark.point[1] + 1.25,
            )
        )
    cutters.extend(box(*label.bounds) for label in plan.station_labels)
    return unary_union(cutters) if cutters else GeometryCollection()


def _water_stipple(
    surfaces: Sequence[_WaterSurface],
    *,
    field: Rect,
    knockout: BaseGeometry,
    enabled: bool = True,
) -> tuple[list[_WaterDot], dict[str, Any]]:
    """Create deterministic physical closed dots inside sourced water only."""

    dot_path = tuple(
        _circle((0.0, 0.0), WATER_DOT_DIAMETER_MM / 2.0, WATER_DOT_VERTICES)
    )
    dot_path_length = _length(dot_path)
    minimum_dot_path_length = 3.0 * WATER_DOT_NIB_MM
    if dot_path_length + 1e-9 < minimum_dot_path_length:
        raise MapPlotterError("Transit water dot is below the three-nib floor.")
    ink_inset = (
        WATER_DOT_DIAMETER_MM / 2.0
        + WATER_DOT_NIB_MM / 2.0
        + WATER_DOT_SERIALIZATION_ALLOWANCE_MM
    )
    field_area_mm2 = field.width * field.height
    ink_budget_mm2 = WATER_DOT_INK_BUDGET_FIELD_FRACTION * field_area_mm2
    if not enabled:
        return [], {
            "enabled": False,
            "disabled_reason": "water-areas-excluded-by-scale-vocabulary",
            "method": "no stipple when sourced water-area context is scale-omitted",
            "physical_pen_id": "blue-0-25",
            "physical_nib_mm": WATER_DOT_NIB_MM,
            "spacing_mm": WATER_DOT_SPACING_MM,
            "diameter_mm": WATER_DOT_DIAMETER_MM,
            "vertices_per_dot": WATER_DOT_VERTICES,
            "minimum_dot_path_length_mm": minimum_dot_path_length,
            "actual_dot_path_length_mm": round(dot_path_length, 6),
            "bank_and_ink_inset_mm": ink_inset,
            "route_station_label_knockout_clearance_mm": ink_inset,
            "candidate_dot_path_count": 0,
            "dot_path_count": 0,
            "omitted_budget_dot_path_count": 0,
            "candidate_represented_feature_count": 0,
            "represented_feature_count": 0,
            "unrepresented_candidate_feature_count": 0,
            "represented_source_count": 0,
            "pen_down_distance_mm": 0.0,
            "estimated_ink_area_mm2": 0.0,
            "ink_budget_field_fraction": WATER_DOT_INK_BUDGET_FIELD_FRACTION,
            "ink_budget_mm2": round(ink_budget_mm2, 6),
            "all_dot_centres_in_inset_sourced_water": True,
            "all_dots_clear_route_station_label_knockout": True,
            "white_knockout_ink_used": False,
        }
    field_geometry = box(
        field.left + ink_inset,
        field.top + ink_inset,
        field.right - ink_inset,
        field.bottom - ink_inset,
    )
    safe_surfaces: list[tuple[_WaterSurface, BaseGeometry]] = []
    for surface in surfaces:
        safe = surface.geometry.buffer(-ink_inset).intersection(field_geometry)
        if not safe.is_empty:
            safe_surfaces.append((surface, safe))
    eligible: BaseGeometry = (
        unary_union([geometry for _surface, geometry in safe_surfaces])
        if safe_surfaces
        else GeometryCollection()
    )
    if not eligible.is_empty and not knockout.is_empty:
        eligible = eligible.difference(knockout.buffer(ink_inset))

    candidates: list[_WaterDot] = []
    if not eligible.is_empty:
        row_step = WATER_DOT_SPACING_MM * 0.8660254037844386
        min_x, min_y, max_x, max_y = eligible.bounds
        first_row = ceil((min_y - field.top) / row_step)
        last_row = floor((max_y - field.top) / row_step)
        for row in range(first_row, last_row + 1):
            y_mm = field.top + row * row_step
            offset = WATER_DOT_SPACING_MM / 2.0 if row % 2 else 0.0
            first_column = ceil((min_x - field.left - offset) / WATER_DOT_SPACING_MM)
            last_column = floor((max_x - field.left - offset) / WATER_DOT_SPACING_MM)
            for column in range(first_column, last_column + 1):
                x_mm = field.left + offset + column * WATER_DOT_SPACING_MM
                point = ShapelyPoint(x_mm, y_mm)
                if not eligible.covers(point):
                    continue
                represented = [
                    surface
                    for surface, safe_geometry in safe_surfaces
                    if safe_geometry.covers(point)
                ]
                if not represented:
                    continue
                candidates.append(
                    _WaterDot(
                        centre=(x_mm, y_mm),
                        points=tuple(
                            _circle(
                                (x_mm, y_mm),
                                WATER_DOT_DIAMETER_MM / 2.0,
                                WATER_DOT_VERTICES,
                            )
                        ),
                        feature_ids=tuple(
                            sorted(
                                {
                                    feature_id
                                    for surface in represented
                                    for feature_id in surface.feature_ids
                                }
                            )
                        ),
                        source_refs=tuple(
                            sorted(
                                {
                                    source_ref
                                    for surface in represented
                                    for source_ref in surface.source_refs
                                }
                            )
                        ),
                        source_objects=tuple(
                            sorted(
                                {
                                    source_object
                                    for surface in represented
                                    for source_object in surface.source_objects
                                }
                            )
                        ),
                    )
                )

    maximum_dot_count = max(
        1, floor(ink_budget_mm2 / (dot_path_length * WATER_DOT_NIB_MM))
    )
    selected_indices = set(range(len(candidates)))
    if len(candidates) > maximum_dot_count:
        first_index_by_feature: dict[str, int] = {}
        for index, dot in enumerate(candidates):
            for feature_id in dot.feature_ids:
                first_index_by_feature.setdefault(feature_id, index)
        reserved = sorted(set(first_index_by_feature.values()))
        if len(reserved) > maximum_dot_count:
            selected_indices = {
                reserved[floor(index * len(reserved) / maximum_dot_count)]
                for index in range(maximum_dot_count)
            }
        else:
            selected_indices = set(reserved)
            remaining = [
                index
                for index in range(len(candidates))
                if index not in selected_indices
            ]
            slots = maximum_dot_count - len(selected_indices)
            if slots:
                selected_indices.update(
                    remaining[floor(index * len(remaining) / slots)]
                    for index in range(slots)
                )
    selected = [candidates[index] for index in sorted(selected_indices)]
    candidate_features = {
        feature_id for dot in candidates for feature_id in dot.feature_ids
    }
    selected_features = {
        feature_id for dot in selected for feature_id in dot.feature_ids
    }
    selected_sources = {
        source_ref for dot in selected for source_ref in dot.source_refs
    }
    return selected, {
        "enabled": True,
        "method": "staggered plotter-safe closed octagons inside sourced polygons",
        "physical_pen_id": "blue-0-25",
        "physical_nib_mm": WATER_DOT_NIB_MM,
        "spacing_mm": WATER_DOT_SPACING_MM,
        "diameter_mm": WATER_DOT_DIAMETER_MM,
        "vertices_per_dot": WATER_DOT_VERTICES,
        "minimum_dot_path_length_mm": minimum_dot_path_length,
        "actual_dot_path_length_mm": round(dot_path_length, 6),
        "bank_and_ink_inset_mm": ink_inset,
        "route_station_label_knockout_clearance_mm": ink_inset,
        "candidate_dot_path_count": len(candidates),
        "dot_path_count": len(selected),
        "omitted_budget_dot_path_count": len(candidates) - len(selected),
        "candidate_represented_feature_count": len(candidate_features),
        "represented_feature_count": len(selected_features),
        "unrepresented_candidate_feature_count": len(
            candidate_features - selected_features
        ),
        "represented_source_count": len(selected_sources),
        "pen_down_distance_mm": round(len(selected) * dot_path_length, 6),
        "estimated_ink_area_mm2": round(
            len(selected) * dot_path_length * WATER_DOT_NIB_MM, 6
        ),
        "ink_budget_field_fraction": WATER_DOT_INK_BUDGET_FIELD_FRACTION,
        "ink_budget_mm2": round(ink_budget_mm2, 6),
        "all_dot_centres_in_inset_sourced_water": all(
            eligible.covers(ShapelyPoint(dot.centre)) for dot in selected
        ),
        "all_dots_clear_route_station_label_knockout": True,
        "white_knockout_ink_used": False,
    }


def _context_knockout(
    strokes: Sequence[PlannedContextStroke],
    plan: TransitPlan,
    *,
    allowed: frozenset[str],
    field: Rect,
    knockout: BaseGeometry | None = None,
) -> tuple[list[PlannedContextStroke], list[str], dict[str, Any]]:
    knockout = _context_knockout_geometry(plan) if knockout is None else knockout
    field_geometry = box(field.left, field.top, field.right, field.bottom)
    explicit_field_clip = plan.scale_tier in PAPER_SCALE_CONTEXT_FLOOR_TIERS
    output: list[PlannedContextStroke] = []
    omissions: list[str] = []
    omission_entries: list[dict[str, Any]] = []

    def record_omission(
        stroke: PlannedContextStroke,
        *,
        child_id: str,
        reason: str,
        points: Sequence[Point] = (),
        measured_length_mm: float | None = None,
        required_length_mm: float | None = None,
        measured_area_mm2: float | None = None,
        required_area_mm2: float | None = None,
    ) -> None:
        serialized = _serialized_points(points)
        geometry_payload = path_data(list(serialized)) if len(serialized) >= 2 else ""
        omission_entries.append(
            {
                "omission_id": child_id,
                "reason": reason,
                "kind": _context_key(stroke.kind),
                "represented_feature_ids": list(stroke.feature_ids),
                "source_refs": list(stroke.source_refs),
                "source_objects": list(stroke.source_objects),
                "measured_serialized_length_mm": (
                    None if measured_length_mm is None else round(measured_length_mm, 6)
                ),
                "required_effective_length_floor_mm": (
                    None if required_length_mm is None else round(required_length_mm, 6)
                ),
                "measured_serialized_area_mm2": (
                    None if measured_area_mm2 is None else round(measured_area_mm2, 6)
                ),
                "required_minimum_area_mm2": (
                    None if required_area_mm2 is None else round(required_area_mm2, 6)
                ),
                "serialized_geometry_sha256": hashlib.sha256(
                    geometry_payload.encode("ascii")
                ).hexdigest(),
            }
        )

    counts: dict[str, Any] = {
        "policy_version": CONTEXT_PHYSICAL_FLOOR_POLICY_VERSION,
        "scale_tier": plan.scale_tier,
        "serialized_coordinate_precision_mm": SVG_COORDINATE_QUANTUM_MM,
        "minimum_length_rule": "max(0.5 mm, 3 x physical nib width)",
        "closed_area_rule": "minimum area = (2 x physical nib width)^2",
        "input": 0,
        "emitted": 0,
        "scale_omitted": 0,
        "map_field_clip_omitted": 0,
        "fully_removed_by_map_field_clip": 0,
        "partially_clipped_by_map_field": 0,
        "physical_floor_omitted": 0,
        "physical_area_floor_omitted": 0,
        "degenerate_omitted": 0,
        "fully_removed_by_knockout": 0,
        "sub_three_nib_transport_emitted": 0,
        "knockout_fragments": 0,
        "per_kind": {},
    }
    for stroke in strokes:
        counts["input"] += 1
        kind = _context_key(stroke.kind)
        per_kind = counts["per_kind"].setdefault(
            kind,
            {
                "input": 0,
                "emitted": 0,
                "scale_omitted": 0,
                "map_field_clip_omitted": 0,
                "fully_removed_by_map_field_clip": 0,
                "partially_clipped_by_map_field": 0,
                "physical_floor_omitted": 0,
                "physical_area_floor_omitted": 0,
                "degenerate_omitted": 0,
                "fully_removed_by_knockout": 0,
                "sub_three_nib_transport_emitted": 0,
            },
        )
        per_kind["input"] += 1
        if kind not in allowed or kind not in CONTEXT_STYLE:
            omissions.append(stroke.feature_id)
            counts["scale_omitted"] += 1
            per_kind["scale_omitted"] += 1
            record_omission(
                stroke,
                child_id=stroke.feature_id,
                reason="excluded-by-scale-vocabulary",
                points=stroke.points,
            )
            continue
        geometry = LineString(stroke.points)
        clipped = (
            geometry.intersection(field_geometry) if explicit_field_clip else geometry
        )
        clipped_parts = sorted(
            _line_parts(clipped),
            key=lambda item: path_data(list(_serialized_points(item.coords))),
        )
        outside_parts = (
            sorted(
                _line_parts(geometry.difference(field_geometry)),
                key=lambda item: path_data(list(_serialized_points(item.coords))),
            )
            if explicit_field_clip
            else []
        )
        if outside_parts:
            counts["map_field_clip_omitted"] += len(outside_parts)
            per_kind["map_field_clip_omitted"] += len(outside_parts)
            if clipped_parts:
                counts["partially_clipped_by_map_field"] += 1
                per_kind["partially_clipped_by_map_field"] += 1
            for outside_index, outside in enumerate(outside_parts):
                record_omission(
                    stroke,
                    child_id=f"{stroke.feature_id}-outside-map-field-{outside_index}",
                    reason="outside-map-field",
                    points=tuple((float(x), float(y)) for x, y in outside.coords),
                )
        if not clipped_parts:
            # A zero-length source or an upstream numerical collapse can have
            # neither a linear intersection nor a linear outside remainder.
            # Keep that fail-closed disposition explicit so every represented
            # source feature remains auditable even when there is no path to
            # draw or fingerprint after clipping.
            if not outside_parts:
                serialized_points = _serialized_points(stroke.points)
                measured_length_mm = _length(serialized_points)
                counts["degenerate_omitted"] += 1
                per_kind["degenerate_omitted"] += 1
                record_omission(
                    stroke,
                    child_id=f"{stroke.feature_id}-empty-map-field-linework",
                    reason="degenerate-before-or-during-map-field-clipping",
                    points=serialized_points,
                    measured_length_mm=measured_length_mm,
                    required_length_mm=_context_physical_floor_mm(kind),
                )
            else:
                counts["fully_removed_by_map_field_clip"] += 1
                per_kind["fully_removed_by_map_field_clip"] += 1
            continue
        clipped_geometry: BaseGeometry = (
            clipped_parts[0]
            if len(clipped_parts) == 1
            else MultiLineString(clipped_parts)
        )
        difference = (
            clipped_geometry.difference(knockout)
            if not knockout.is_empty
            else clipped_geometry
        )
        pieces = sorted(
            _line_parts(difference),
            key=lambda item: path_data(list(_serialized_points(item.coords))),
        )
        if not pieces:
            counts["fully_removed_by_knockout"] += len(clipped_parts)
            per_kind["fully_removed_by_knockout"] += len(clipped_parts)
            for clipped_index, clipped_part in enumerate(clipped_parts):
                record_omission(
                    stroke,
                    child_id=f"{stroke.feature_id}-knockout-part-{clipped_index}",
                    reason="fully-removed-by-route-station-label-knockout",
                    points=tuple((float(x), float(y)) for x, y in clipped_part.coords),
                )
        minimum_length_mm = _context_physical_floor_mm(kind)
        for piece_index, piece in enumerate(pieces):
            coords = tuple((float(x), float(y)) for x, y in piece.coords)
            serialized_coords = _serialized_points(coords)
            piece_length_mm = _length(serialized_coords)
            child_id = f"{stroke.feature_id}-part-{piece_index}"
            if (
                len(serialized_coords) < 2
                or piece.is_empty
                or piece_length_mm <= MINIMUM_NONDEGENERATE_CONTEXT_LENGTH_MM
            ):
                counts["degenerate_omitted"] += 1
                per_kind["degenerate_omitted"] += 1
                record_omission(
                    stroke,
                    child_id=child_id,
                    reason="degenerate-after-serialization-or-knockout",
                    points=serialized_coords,
                    measured_length_mm=piece_length_mm,
                    required_length_mm=minimum_length_mm,
                )
                continue
            is_subfloor = piece_length_mm + 1e-9 < minimum_length_mm
            retains_subfloor = _retains_subfloor_transport_context(
                scale_tier=plan.scale_tier,
                kind=kind,
            )
            if is_subfloor and not retains_subfloor:
                counts["physical_floor_omitted"] += 1
                per_kind["physical_floor_omitted"] += 1
                record_omission(
                    stroke,
                    child_id=child_id,
                    reason="below-minimum-serialized-length",
                    points=serialized_coords,
                    measured_length_mm=piece_length_mm,
                    required_length_mm=minimum_length_mm,
                )
                continue
            nib_mm = _context_nib_mm(kind)
            is_closed_area = (
                stroke.geometry_type == "area-ring"
                and len(serialized_coords) >= 4
                and serialized_coords[0] == serialized_coords[-1]
            )
            area_mm2: float | None = None
            minimum_area_mm2: float | None = None
            if is_closed_area:
                polygon = Polygon(serialized_coords)
                if polygon.is_valid:
                    area_mm2 = polygon.area
                    minimum_area_mm2 = (2.0 * nib_mm) ** 2
            if (
                area_mm2 is not None
                and minimum_area_mm2 is not None
                and area_mm2 + 1e-12 < minimum_area_mm2
            ):
                counts["physical_floor_omitted"] += 1
                counts["physical_area_floor_omitted"] += 1
                per_kind["physical_floor_omitted"] += 1
                per_kind["physical_area_floor_omitted"] += 1
                record_omission(
                    stroke,
                    child_id=child_id,
                    reason="below-minimum-serialized-closed-area",
                    points=serialized_coords,
                    measured_length_mm=piece_length_mm,
                    required_length_mm=minimum_length_mm,
                    measured_area_mm2=area_mm2,
                    required_area_mm2=minimum_area_mm2,
                )
                continue
            output.append(
                replace(
                    stroke,
                    feature_id=child_id,
                    kind=kind,
                    points=serialized_coords,
                    represented_feature_ids=stroke.feature_ids,
                    represented_source_refs=stroke.source_refs,
                    represented_source_objects=stroke.source_objects,
                )
            )
            counts["emitted"] += 1
            per_kind["emitted"] += 1
            if is_subfloor:
                counts["sub_three_nib_transport_emitted"] += 1
                per_kind["sub_three_nib_transport_emitted"] += 1
        counts["knockout_fragments"] += max(0, len(pieces) - len(clipped_parts))
    omission_payload = json.dumps(
        omission_entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    counts["omission_ledger"] = {
        "schema_version": 1,
        "measurement_scope": (
            "Every context trail or post-knockout child deliberately omitted; "
            "measurements and geometry fingerprints use the serialized 0.001 mm "
            "master-SVG coordinates."
        ),
        "entry_count": len(omission_entries),
        "ordered_evidence_sha256": hashlib.sha256(omission_payload).hexdigest(),
        "entries": omission_entries,
    }
    counts["candidate_disposition_count"] = counts["emitted"] + len(omission_entries)
    counts["explicit_map_field_clipping_active"] = explicit_field_clip
    counts["local_unexplained_omission_count"] = 0
    counts["unexplained_omission_count"] = 0
    return output, omissions, counts


def _nice_scale_bar(plan: TransitPlan, maximum_mm: float = 34.0) -> tuple[float, str]:
    real_m = maximum_mm / plan.projector.scale_mm_per_m
    exponent = floor(log10(max(real_m, 1e-9)))
    base = 10.0**exponent
    candidates = [value * base for value in (1.0, 2.0, 5.0, 10.0)]
    selected = max(value for value in candidates if value <= real_m + 1e-9)
    paper_mm = selected * plan.projector.scale_mm_per_m
    label = f"{selected / 1000:g} KM" if selected >= 1000.0 else f"{selected:g} M"
    return paper_mm, label


def render_transit_plate(
    network: TransitNetwork,
    *,
    station_label_policy: str = "key",
    generated_at: str | None = None,
    allow_route_only: bool = False,
) -> tuple[ET.Element, dict[str, Any]]:
    if not network.context and not allow_route_only:
        raise MapPlotterError(
            "Transit plate rendering requires pinned geographic context. "
            "Attach the house context first; set allow_route_only=True only "
            "for an explicitly diagnostic route-only proof."
        )
    if network.kind in NAMED_OPERATOR_KINDS:
        inferred_coastlines = [
            feature.id
            for feature in network.context
            if _context_key(feature.kind) == "coastline"
            and not (
                feature.source_tag_map.get("authored_geometry") == "true"
                and feature.source_tag_map.get("natural") == "coastline"
                and not feature.source_ref.startswith("os-open-zoomstack-")
            )
        ]
        if inferred_coastlines:
            raise MapPlotterError(
                "Named operator rendering forbids inferred or unverified coastline "
                "geometry; attach the pinned authored OSM natural=coastline pack "
                f"(offending features: {inferred_coastlines[:8]})."
            )
    generated_at = generated_at or datetime.now(UTC).isoformat()
    context = PlateContext.load(network.format_id)
    map_composition = aspect_aware_map_field(network, context.field)
    map_field = map_composition.effective_field
    route_target_maximum_width_mm = float(
        context.plate.get("map_linework_nib_mm", {}).get("heavy", 1.0)
    )
    route_target_width_override_mm = (
        0.4 if network.kind == "national-operator-overview" else None
    )
    plan = build_transit_plan(
        network,
        map_field,
        station_label_policy=station_label_policy,
        route_target_width_mm=route_target_width_override_mm,
        route_target_maximum_width_mm=route_target_maximum_width_mm,
        projector_margin_fraction=map_composition.projector_margin_fraction,
    )
    if any(
        abs(actual - expected) > 1e-6
        for actual, expected in zip(
            (
                plan.projector.rect.x,
                plan.projector.rect.y,
                plan.projector.rect.width,
                plan.projector.rect.height,
            ),
            (
                map_composition.geographic_viewport.x,
                map_composition.geographic_viewport.y,
                map_composition.geographic_viewport.width,
                map_composition.geographic_viewport.height,
            ),
            strict=True,
        )
    ):
        raise MapPlotterError(
            "Transit projector did not preserve the aspect-aware geographic viewport."
        )
    route_width_plans = plan.route_width_by_line
    physical_pen_colour_collisions = _physical_pen_colour_collisions(
        network, route_width_plans
    )
    allowed_context = _allowed_context_for(network, plan.scale_tier)
    water_surfaces, water_surface_stats = _water_surface_records(
        plan.context_strokes,
        field=map_field,
        apply_visible_area_floor=(plan.scale_tier in PAPER_SCALE_CONTEXT_FLOOR_TIERS),
    )
    water_surface_geometry: BaseGeometry = (
        unary_union([surface.geometry for surface in water_surfaces])
        if water_surfaces
        else GeometryCollection()
    )
    context_before_knockout, water_line_dispositions, water_line_stats = (
        _suppress_water_lines_inside_surfaces(
            plan.context_strokes,
            water_surface_geometry,
        )
    )
    assembled_context, context_assembly_stats = assemble_context_trails(
        context_before_knockout,
        simplify_mm=0.04,
    )
    knockout = _context_knockout_geometry(plan)
    context_strokes, context_omitted, context_stats = _context_knockout(
        assembled_context,
        plan,
        allowed=allowed_context,
        field=map_field,
        knockout=knockout,
    )
    water_dots, water_stipple_stats = _water_stipple(
        water_surfaces,
        field=map_field,
        knockout=knockout,
        enabled="water-areas" in allowed_context,
    )
    candidate_context_feature_ids = {
        feature_id
        for stroke in plan.context_strokes
        for feature_id in stroke.feature_ids
    }
    emitted_context_feature_ids = {
        feature_id for stroke in context_strokes for feature_id in stroke.feature_ids
    }
    floor_and_clip_omitted_feature_ids = {
        feature_id
        for entry in context_stats["omission_ledger"]["entries"]
        for feature_id in entry["represented_feature_ids"]
    }
    fully_suppressed_water_feature_ids = set(
        water_line_stats["fully_suppressed_feature_ids"]
    )
    accounted_context_feature_ids = (
        emitted_context_feature_ids
        | floor_and_clip_omitted_feature_ids
        | fully_suppressed_water_feature_ids
    )
    unexplained_context_feature_ids = sorted(
        candidate_context_feature_ids - accounted_context_feature_ids
    )
    unexpected_context_feature_ids = sorted(
        accounted_context_feature_ids - candidate_context_feature_ids
    )
    context_stats["source_feature_accounting"] = {
        "candidate_feature_count": len(candidate_context_feature_ids),
        "candidate_feature_ids_sha256": hashlib.sha256(
            json.dumps(
                sorted(candidate_context_feature_ids), separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest(),
        "emitted_feature_count": len(emitted_context_feature_ids),
        "floor_clip_or_knockout_omitted_feature_count": len(
            floor_and_clip_omitted_feature_ids
        ),
        "fully_suppressed_water_feature_count": len(fully_suppressed_water_feature_ids),
        "accounted_feature_count": len(accounted_context_feature_ids),
        "unexplained_feature_ids": unexplained_context_feature_ids,
        "unexpected_feature_ids": unexpected_context_feature_ids,
        "complete": not (
            unexplained_context_feature_ids or unexpected_context_feature_ids
        ),
    }
    context_stats["unexplained_omission_count"] = len(unexplained_context_feature_ids)
    if unexplained_context_feature_ids or unexpected_context_feature_ids:
        raise MapPlotterError(
            "Transit context source-feature accounting failed after paper-scale "
            "selection."
        )
    root = ET.Element(
        svg_tag("svg"),
        {
            "width": f"{context.page.width:g}mm",
            "height": f"{context.page.height:g}mm",
            "viewBox": f"0 0 {context.page.width:g} {context.page.height:g}",
            "version": "1.1",
        },
    )
    definitions = ET.SubElement(root, svg_tag("defs"))
    map_clip = ET.SubElement(
        definitions,
        svg_tag("clipPath"),
        {"id": "transit-map-field-clip", "clipPathUnits": "userSpaceOnUse"},
    )
    ET.SubElement(
        map_clip,
        svg_tag("rect"),
        {
            "x": format_measurement(map_field.left),
            "y": format_measurement(map_field.top),
            "width": format_measurement(map_field.width),
            "height": format_measurement(map_field.height),
        },
    )
    ET.SubElement(root, svg_tag("title")).text = network.name
    ET.SubElement(root, svg_tag("desc")).text = (
        "Geographic passenger-network pen plate. It is an original rendering "
        "of source-qualified route geometry, not a copy of an operator diagram."
    )
    metadata_payload = {
        "schema_version": 1,
        "network_id": network.id,
        "contract_sha256": network.contract_sha256,
        "geometry_mode": network.geometry_mode,
        "scale_tier": plan.scale_tier,
        "map_composition": map_composition.as_dict(),
        "sources": [source.as_dict() for source in network.sources],
    }
    metadata = ET.SubElement(
        root,
        svg_tag("metadata"),
        {
            f"{{{MAP_NS}}}generator": f"city-map-plotter {__version__}",
            f"{{{MAP_NS}}}domain": "transit",
            f"{{{MAP_NS}}}network": network.id,
            f"{{{MAP_NS}}}contract-sha256": network.contract_sha256,
        },
    )
    metadata.text = json.dumps(metadata_payload, sort_keys=True, separators=(",", ":"))
    ET.SubElement(
        root,
        f"{{{SODIPODI_NS}}}namedview",
        {
            "id": "namedview-mapplot-transit",
            "pagecolor": "#FCFBF7",
            "showborder": "true",
            f"{{{INKSCAPE_NS}}}document-units": "mm",
        },
    )

    physical_groups: dict[str, ET.Element] = {}
    pen_records: dict[str, dict[str, Any]] = {}

    def group_for(pen: dict[str, Any], label: str) -> ET.Element:
        key = str(pen["plot_key"])
        if key in physical_groups:
            return physical_groups[key]
        group = ET.SubElement(
            root,
            svg_tag("g"),
            {
                "id": f"layer-pen-{key}",
                f"{{{INKSCAPE_NS}}}groupmode": "layer",
                f"{{{INKSCAPE_NS}}}label": label,
                "fill": "none",
                "stroke-linecap": "round",
                "stroke-linejoin": "round",
                "data-physical-pen-key": key,
                **_physical_attributes(
                    pen_id=str(pen["pen_id"]),
                    ink=str(pen["ink"]),
                    nib_mm=float(pen["nib_mm"]),
                    calibration_state=str(pen["calibration_state"]),
                    match_status=str(pen["match_status"]),
                ),
            },
        )
        physical_groups[key] = group
        pen_records[key] = {
            **pen,
            "logical_layers": [],
            "path_count": 0,
            "pen_down_distance_mm": 0.0,
        }
        return group

    logical_counter = 0

    def logical_group(
        pen: dict[str, Any], logical_id: str, label: str, preview: str
    ) -> ET.Element:
        nonlocal logical_counter
        parent = group_for(pen, f"Physical pen — {pen['label']}")
        logical_counter += 1
        child = ET.SubElement(
            parent,
            svg_tag("g"),
            {
                "id": f"logical-{logical_id}",
                "data-logical-layer": logical_id,
                "data-logical-label": label,
                "stroke": preview,
                "stroke-width": format_measurement(float(pen["nib_mm"])),
            },
        )
        pen_records[str(pen["plot_key"])]["logical_layers"].append(logical_id)
        return child

    # The context is intentionally quiet.  Paths are genuinely cut away under
    # lines, stations and labels; no white "halo" ink is emitted.
    for kind in sorted(allowed_context):
        records = [stroke for stroke in context_strokes if stroke.kind == kind]
        if not records:
            continue
        pen_id, _default_preview, _ = CONTEXT_STYLE[kind]
        preview = _context_preview_for(network, plan.scale_tier, kind)
        pen = _builtin_pen(pen_id, preview)
        group = logical_group(pen, f"context-{kind}", f"Context — {kind}", preview)
        group.set("clip-path", "url(#transit-map-field-clip)")
        for context_record in records:
            context_length_mm = _length(context_record.points)
            context_minimum_mm = _context_physical_floor_mm(kind)
            is_subfloor_transport_exception = (
                _retains_subfloor_transport_context(
                    scale_tier=plan.scale_tier,
                    kind=kind,
                )
                and context_length_mm + 1e-9 < context_minimum_mm
            )
            path_attributes = {
                "data-transit-context-id": context_record.feature_id,
                "data-source-ref": context_record.source_ref,
                "data-source-object": context_record.source_object,
                "data-represented-feature-ids": ";".join(context_record.feature_ids),
                "data-source-refs": ";".join(context_record.source_refs),
                "data-source-objects": ";".join(context_record.source_objects),
                "data-physical-length-mm": format_measurement(context_length_mm),
                "data-three-nib-minimum-mm": format_measurement(context_minimum_mm),
                "data-effective-physical-floor-mm": format_measurement(
                    context_minimum_mm
                ),
                "data-physical-floor-status": (
                    "review-required-source-backed-transport-detail-exception"
                    if is_subfloor_transport_exception
                    else "passes-three-nib-floor"
                ),
            }
            if is_subfloor_transport_exception:
                path_attributes.update(
                    {
                        "data-review-required": "true",
                        "data-review-reason": "sub-three-nib-transport-detail",
                        "data-source-backed-detail-exception": "true",
                        "data-detail-exception-policy": (
                            TRANSPORT_DETAIL_EXCEPTION_POLICY_VERSION
                        ),
                    }
                )
            if kind == "water-areas":
                path_attributes.update(
                    {
                        "data-role": "water-area-bank-outline",
                        "data-water-area-fill": "closed-dot-stipple",
                    }
                )
            elif kind == "water-lines":
                disposition = next(
                    (
                        value
                        for feature_id, value in water_line_dispositions.items()
                        if context_record.feature_id == feature_id
                        or context_record.feature_id.startswith(f"{feature_id}-part-")
                    ),
                    "plain-linear-water-no-area-overlap",
                )
                path_attributes.update(
                    {
                        "data-role": "water-linear-context",
                        "data-water-area-suppression": disposition,
                        "data-water-width-inferred": "false",
                    }
                )
            _append_path(
                group,
                context_record.points,
                attributes=path_attributes,
            )
            item = pen_records[pen["plot_key"]]
            item["path_count"] += 1
            item["pen_down_distance_mm"] += _length(context_record.points)

    if water_dots:
        water_pen = _builtin_pen("blue-0-25", CONTEXT_STYLE["water-areas"][1])
        water_group = logical_group(
            water_pen,
            "water-stipple",
            "Water areas — physical closed-dot stipple",
            CONTEXT_STYLE["water-areas"][1],
        )
        water_group.set("clip-path", "url(#transit-map-field-clip)")
        for dot_index, dot in enumerate(water_dots):
            _append_path(
                water_group,
                dot.points,
                attributes={
                    "data-role": "water-area-stipple-dot",
                    "data-logical-layer": "water-stipple",
                    "data-water-dot-index": str(dot_index),
                    "data-water-dot-diameter-mm": format_measurement(
                        WATER_DOT_DIAMETER_MM
                    ),
                    "data-water-dot-path-length-mm": format_measurement(
                        _length(dot.points)
                    ),
                    "data-represented-feature-ids": ";".join(dot.feature_ids),
                    "data-source-refs": ";".join(dot.source_refs),
                    "data-source-objects": ";".join(dot.source_objects),
                    "data-water-width-inferred": "false",
                    "data-knockout-clear": "true",
                },
            )
            item = pen_records[water_pen["plot_key"]]
            item["path_count"] += 1
            item["pen_down_distance_mm"] += _length(dot.points)

    route_detail_exception_records: list[dict[str, Any]] = []
    route_offset_exception_records: list[dict[str, Any]] = []
    expected_route_records_by_line: dict[str, dict[str, dict[str, Any]]] = {
        line.id: {} for line in network.lines
    }
    for line in sorted(network.lines, key=lambda item: (item.order, item.id)):
        width_plan = route_width_plans[line.id]
        pen = _line_pen(line, width_plan)
        group = logical_group(
            pen,
            f"route-{line.id}",
            f"Route — {line.name}",
            str(pen["preview"]),
        )
        group.set("data-route-reference-colour", line.colour.display_hex)
        group.set("data-route-preview-colour", str(pen["preview"]))
        group.set("data-route-preview-source", "physical-pen-inventory")
        group.set("data-route-preview-is-physical-ink-claim", "false")
        group.set(
            "data-route-requested-width-mm",
            format_measurement(pen["requested_width_mm"]),
        )
        group.set(
            "data-route-plotted-width-mm", format_measurement(pen["plotted_width_mm"])
        )
        group.set("data-route-stroke-count", str(pen["stroke_count"]))
        group.set(
            "data-route-offset-pitch-mm", format_measurement(pen["offset_pitch_mm"])
        )
        group.set(
            "data-route-physical-union-width-mm",
            format_measurement(_route_physical_union_width_mm(width_plan)),
        )
        group.set("data-route-physical-union-width-scope", PHYSICAL_UNION_WIDTH_SCOPE)
        adjacent_overlap_mm = _route_adjacent_overlap_mm(width_plan)
        group.set(
            "data-route-adjacent-overlap-mm",
            (
                "not-applicable"
                if adjacent_overlap_mm is None
                else format_measurement(adjacent_overlap_mm)
            ),
        )
        group.set("data-route-width-fit-mode", str(pen["width_fit_mode"]))
        promotion = width_plan.native_owned_nib_promotion
        group.set(
            "data-native-owned-nib-promotion",
            "true" if promotion is not None else "false",
        )
        group.set(
            "data-native-owned-nib-promotion-policy",
            (
                str(promotion["policy_version"])
                if promotion is not None
                else "not-applicable"
            ),
        )
        group.set(
            "data-native-owned-nib-baseline-width-mm",
            (
                format_measurement(promotion["baseline_scale_target_width_mm"])
                if promotion is not None
                else "not-applicable"
            ),
        )
        group.set(
            "data-native-owned-nib-resolved-width-mm",
            (
                format_measurement(promotion["resolved_native_width_mm"])
                if promotion is not None
                else "not-applicable"
            ),
        )
        group.set(
            "data-native-owned-nib-product-id",
            str(promotion["product_id"]) if promotion is not None else "not-applicable",
        )
        group.set(
            "data-native-owned-nib-operator-key",
            str(promotion["operator_key"])
            if promotion is not None
            else "not-applicable",
        )
        group.set("clip-path", "url(#transit-map-field-clip)")
        for route_record in [
            item for item in plan.route_strokes if item.line_id == line.id
        ]:
            route_record_id = _route_record_id(route_record)
            if route_record_id in expected_route_records_by_line[line.id]:
                raise MapPlotterError(
                    f"Duplicate physical route-record identity {route_record_id}."
                )
            expansion = _physical_route_expansion(
                route_record.points,
                width_plan,
                route_record_id=route_record_id,
                line_id=line.id,
            )
            physical_paths = list(expansion.paths)
            logical_length_mm = _length(route_record.points)
            three_nib_minimum_mm = 3.0 * width_plan.fit.pen.mark_width_mm
            is_subfloor_route_exception = (
                logical_length_mm > MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM
                and logical_length_mm + 1e-9 < three_nib_minimum_mm
            )
            record_review_required = not expansion.exact_local_union_certified
            offset_method = expansion.offset_method
            if expansion.exact_local_union_certified and (
                offset_method != STANDARD_ROUTE_OFFSET_METHOD
                or not expansion.segment_offset_coverage_certified
            ):
                raise MapPlotterError(
                    f"Route {route_record_id} has an invalid exact-width claim."
                )
            if (
                is_subfloor_route_exception
                and offset_method != SHORT_ROUTE_OFFSET_METHOD
            ):
                raise MapPlotterError(
                    f"Sub-three-nib route {route_record_id} did not use the "
                    "declared rigid-normal method."
                )
            rigid_normal = (
                _short_route_rigid_normal(route_record.points)
                if is_subfloor_route_exception
                else None
            )
            expected_stroke_indexes = set(range(width_plan.fit.stroke_count))
            actual_stroke_indexes = {
                stroke_index for stroke_index, _, _ in physical_paths
            }
            physical_path_counts_by_stroke_index = {
                stroke_index: sum(
                    path_stroke_index == stroke_index
                    for path_stroke_index, _, _ in physical_paths
                )
                for stroke_index in expected_stroke_indexes
            }
            if actual_stroke_indexes != expected_stroke_indexes:
                raise MapPlotterError(
                    f"Physical route stroke-index coverage failed for "
                    f"{route_record_id}: expected "
                    f"{sorted(index + 1 for index in expected_stroke_indexes)}, got "
                    f"{sorted(index + 1 for index in actual_stroke_indexes)}."
                )
            if any(
                count != 1 for count in physical_path_counts_by_stroke_index.values()
            ):
                raise MapPlotterError(
                    f"Physical route continuity failed for {route_record_id}: "
                    f"path counts by stroke index "
                    f"{physical_path_counts_by_stroke_index}."
                )
            expected_route_records_by_line[line.id][route_record_id] = {
                "expected_stroke_indexes": tuple(
                    sorted(index + 1 for index in expected_stroke_indexes)
                ),
                "offset_method": offset_method,
                "logical_length_mm": logical_length_mm,
                "exact_local_union_certified": (expansion.exact_local_union_certified),
                "local_physical_union_width_status": (
                    expansion.local_physical_union_width_status
                ),
                "stroke_index_complete": expansion.stroke_index_complete,
                "segment_offset_coverage_certified": (
                    expansion.segment_offset_coverage_certified
                ),
                "cyclic_path_status": expansion.cyclic_path_status,
                "source_excursion_bound_certified": (
                    expansion.source_excursion_bound_certified
                ),
                "maximum_segment_offset_coverage_error_mm": (
                    expansion.maximum_segment_offset_coverage_error_mm
                ),
                "review_required": record_review_required,
                "one_continuous_path_per_stroke_index": True,
            }
            if is_subfloor_route_exception:
                assert rigid_normal is not None
                route_detail_exception_records.append(
                    {
                        "route_record_id": route_record_id,
                        "line_id": line.id,
                        "source_membership_edge_ids": list(
                            route_record.source_membership_edge_ids
                        ),
                        "representative_edge_ids": list(route_record.edge_ids),
                        "logical_length_mm": round(logical_length_mm, 6),
                        "three_nib_minimum_mm": round(three_nib_minimum_mm, 6),
                        "emitted_physical_path_count": len(physical_paths),
                        "expected_stroke_indexes": sorted(
                            index + 1 for index in expected_stroke_indexes
                        ),
                        "emitted_stroke_indexes": sorted(
                            index + 1 for index in actual_stroke_indexes
                        ),
                        "stroke_index_coverage_complete": True,
                        "physical_offset_method": offset_method,
                        "physical_union_width_scope": PHYSICAL_UNION_WIDTH_SCOPE,
                        "local_physical_union_width_status": (
                            expansion.local_physical_union_width_status
                        ),
                        "exact_local_union_certified": False,
                        "stroke_index_complete": expansion.stroke_index_complete,
                        "segment_offset_coverage_certified": (
                            expansion.segment_offset_coverage_certified
                        ),
                        "maximum_segment_offset_coverage_error_mm": round(
                            expansion.maximum_segment_offset_coverage_error_mm,
                            12,
                        ),
                        "rigid_normal_vector": [
                            round(rigid_normal[0], 9),
                            round(rigid_normal[1], 9),
                        ],
                        "emitted": bool(physical_paths),
                        "review_required": True,
                    }
                )
            if offset_method == SEGMENT_NORMAL_ROUTE_OFFSET_METHOD:
                route_offset_exception_records.append(
                    {
                        "route_record_id": route_record_id,
                        "line_id": line.id,
                        "source_membership_edge_ids": list(
                            route_record.source_membership_edge_ids
                        ),
                        "representative_edge_ids": list(route_record.edge_ids),
                        "logical_length_mm": round(logical_length_mm, 6),
                        "physical_offset_method": offset_method,
                        "physical_union_width_scope": PHYSICAL_UNION_WIDTH_SCOPE,
                        "local_physical_union_width_status": (
                            expansion.local_physical_union_width_status
                        ),
                        "exact_local_union_certified": False,
                        "stroke_index_complete": expansion.stroke_index_complete,
                        "segment_offset_coverage_certified": (
                            expansion.segment_offset_coverage_certified
                        ),
                        "maximum_segment_offset_coverage_error_mm": round(
                            expansion.maximum_segment_offset_coverage_error_mm,
                            12,
                        ),
                        "maximum_segment_normal_error_mm": round(
                            expansion.maximum_segment_normal_error_mm, 12
                        ),
                        "maximum_join_sampling_error_mm": round(
                            expansion.maximum_join_sampling_error_mm, 12
                        ),
                        "cyclic_path_status": expansion.cyclic_path_status,
                        "source_excursion_bound_certified": (
                            expansion.source_excursion_bound_certified
                        ),
                        "maximum_source_excursion_upper_bound_mm": round(
                            expansion.maximum_source_excursion_upper_bound_mm, 12
                        ),
                        "source_excursion_bound_mm": round(
                            expansion.source_excursion_bound_mm, 12
                        ),
                        "hairpin_join_count": (expansion.hairpin_join_count),
                        "emitted_physical_path_count": len(physical_paths),
                        "review_required": True,
                    }
                )
            for stroke_index, physical_offset_mm, physical_points in physical_paths:
                physical_tags = {
                    "plot:stroke-index": str(stroke_index + 1),
                    "plot:stroke-count": str(width_plan.fit.stroke_count),
                    "plot:pass-index": "1",
                    "plot:pass-count": "1",
                    "plot:requested-width-mm": format_measurement(
                        width_plan.fit.requested_width_mm
                    ),
                    "plot:plotted-width-mm": format_measurement(
                        width_plan.fit.plotted_width_mm
                    ),
                    "plot:offset-pitch-mm": format_measurement(
                        width_plan.fit.offset_pitch_mm
                    ),
                    "plot:width-fit-mode": width_plan.fit.mode,
                    "plot:pen-id": width_plan.fit.pen.identity,
                    "plot:nib-mm": format_measurement(width_plan.fit.pen.mark_width_mm),
                }
                _append_path(
                    group,
                    physical_points,
                    attributes={
                        "data-transit-line-id": line.id,
                        "data-transit-route-record-id": route_record_id,
                        "data-transit-edge-ids": " ".join(
                            route_record.source_membership_edge_ids
                        ),
                        "data-transit-representative-edge-ids": " ".join(
                            route_record.edge_ids
                        ),
                        "data-transit-start-node-id": route_record.start_node_id,
                        "data-transit-end-node-id": route_record.end_node_id,
                        "data-source-refs": " ".join(route_record.source_refs),
                        "data-maximum-lane-offset-mm": format_measurement(
                            route_record.maximum_lane_offset_mm
                        ),
                        "data-simplification-tolerance-mm": format_measurement(
                            route_record.simplification_tolerance_mm
                        ),
                        "data-logical-centreline-length-mm": format_measurement(
                            logical_length_mm
                        ),
                        "data-three-nib-minimum-mm": format_measurement(
                            three_nib_minimum_mm
                        ),
                        "data-physical-floor-status": (
                            "review-required-source-backed-route-detail-exception"
                            if is_subfloor_route_exception
                            else "passes-three-nib-floor"
                        ),
                        "data-review-required": (
                            "true" if record_review_required else "false"
                        ),
                        "data-source-backed-detail-exception": (
                            "true" if is_subfloor_route_exception else "false"
                        ),
                        "data-detail-exception-policy": (
                            ROUTE_DETAIL_EXCEPTION_POLICY_VERSION
                            if is_subfloor_route_exception
                            else "not-applicable"
                        ),
                        "data-physical-offset-method": offset_method,
                        "data-physical-union-width-scope": (PHYSICAL_UNION_WIDTH_SCOPE),
                        "data-local-physical-union-width-status": (
                            expansion.local_physical_union_width_status
                        ),
                        "data-exact-local-union-certified": (
                            "true" if expansion.exact_local_union_certified else "false"
                        ),
                        "data-stroke-index-complete": (
                            "true" if expansion.stroke_index_complete else "false"
                        ),
                        "data-one-continuous-path-per-stroke-index": "true",
                        "data-pen-lift-within-stroke-index": "false",
                        "data-segment-offset-coverage-certified": (
                            "true"
                            if expansion.segment_offset_coverage_certified
                            else "false"
                        ),
                        "data-cyclic-path-status": expansion.cyclic_path_status,
                        "data-source-excursion-bound-certified": (
                            "true"
                            if expansion.source_excursion_bound_certified
                            else "false"
                        ),
                        "data-maximum-segment-offset-coverage-error-mm": (
                            format_measurement(
                                expansion.maximum_segment_offset_coverage_error_mm
                            )
                        ),
                        "data-maximum-segment-normal-error-mm": (
                            format_measurement(
                                expansion.maximum_segment_normal_error_mm
                            )
                        ),
                        "data-maximum-join-sampling-error-mm": (
                            format_measurement(expansion.maximum_join_sampling_error_mm)
                        ),
                        "data-maximum-source-excursion-upper-bound-mm": (
                            format_measurement(
                                expansion.maximum_source_excursion_upper_bound_mm
                            )
                        ),
                        "data-source-excursion-bound-mm": format_measurement(
                            expansion.source_excursion_bound_mm
                        ),
                        "data-hairpin-join-count": str(expansion.hairpin_join_count),
                        "data-rigid-normal-x": (
                            format_measurement(rigid_normal[0])
                            if rigid_normal is not None
                            else "not-applicable"
                        ),
                        "data-rigid-normal-y": (
                            format_measurement(rigid_normal[1])
                            if rigid_normal is not None
                            else "not-applicable"
                        ),
                        "data-physical-stroke-index": str(stroke_index + 1),
                        "data-physical-stroke-count": str(width_plan.fit.stroke_count),
                        "data-physical-offset-mm": format_measurement(
                            physical_offset_mm
                        ),
                        "data-native-owned-nib-promotion": (
                            "true" if promotion is not None else "false"
                        ),
                        "data-native-owned-nib-promotion-policy": (
                            str(promotion["policy_version"])
                            if promotion is not None
                            else "not-applicable"
                        ),
                        "data-native-owned-nib-baseline-width-mm": (
                            format_measurement(
                                promotion["baseline_scale_target_width_mm"]
                            )
                            if promotion is not None
                            else "not-applicable"
                        ),
                        "data-native-owned-nib-resolved-width-mm": (
                            format_measurement(promotion["resolved_native_width_mm"])
                            if promotion is not None
                            else "not-applicable"
                        ),
                        **plot_path_attributes(physical_tags),
                    },
                )
                item = pen_records[pen["plot_key"]]
                item["path_count"] += 1
                item["pen_down_distance_mm"] += _length(physical_points)
        line_expected_records = expected_route_records_by_line[line.id].values()
        line_has_nominal_records = any(
            not record["exact_local_union_certified"]
            for record in line_expected_records
        )
        group.set(
            "data-route-exact-local-union-certified",
            "false" if line_has_nominal_records else "true",
        )
        group.set(
            "data-route-local-physical-union-width-status",
            (
                "contains-nominal-review-required-records"
                if line_has_nominal_records
                else CERTIFIED_LOCAL_UNION_STATUS
            ),
        )

    # Furniture follows the same physical role ladder as the university and
    # marathon plates.  The selected format, rather than this renderer,
    # decides which owned nib carries each role.
    station_pen = _black_pen_for_width(
        _format_role_width(context, "hairline", map_linework=True)
    )
    station_group = logical_group(
        station_pen, "transit-stations", "Stations", "#24282B"
    )
    for mark in plan.station_marks:
        radius = 1.0 if mark.tier in {"terminal", "interchange"} else 0.68
        circles = [_circle(mark.point, radius)]
        if mark.tier == "interchange":
            circles.append(_circle(mark.point, radius + 0.62))
        for circle in circles:
            station_attributes = {
                "data-transit-station-id": mark.node_id,
                "data-transit-lines": " ".join(mark.line_ids),
                "data-transit-station-tier": mark.tier,
                "data-station-association-status": mark.association_status,
            }
            if isfinite(mark.displacement_mm):
                station_attributes["data-station-displacement-mm"] = format_measurement(
                    mark.displacement_mm
                )
            _append_path(
                station_group,
                circle,
                attributes=station_attributes,
            )
            pen_records[station_pen["plot_key"]]["path_count"] += 1
            pen_records[station_pen["plot_key"]]["pen_down_distance_mm"] += _length(
                circle
            )

    copy_pen = _black_pen_for_width(_format_role_width(context, "text"))
    labels_group = logical_group(
        copy_pen, "transit-labels", "Station labels and copy", "#24282B"
    )
    for label in plan.station_labels:
        strokes = stroke_text(
            label.text,
            x_mm=label.x_mm,
            y_mm=label.y_mm,
            height_mm=label.cap_height_mm,
            anchor=label.anchor,
        )
        for stroke in strokes:
            _append_path(
                labels_group,
                stroke,
                attributes={
                    "data-transit-station-label": label.node_id,
                    "data-label-collision-status": "placed-clear",
                },
            )
            pen_records[copy_pen["plot_key"]]["path_count"] += 1
            pen_records[copy_pen["plot_key"]]["pen_down_distance_mm"] += _length(stroke)

    # Binding map frame, north mark and scale bar.
    frame_pen = _black_pen_for_width(
        _format_role_width(context, "primary", map_linework=True)
    )
    frame_group = logical_group(
        frame_pen, "transit-frame", "Map frame and scale", "#24282B"
    )
    frame = [
        (map_field.left, map_field.top),
        (map_field.right, map_field.top),
        (map_field.right, map_field.bottom),
        (map_field.left, map_field.bottom),
        (map_field.left, map_field.top),
    ]
    _append_path(frame_group, frame, attributes={"data-role": "map-frame"})
    pen_records[frame_pen["plot_key"]]["path_count"] += 1
    pen_records[frame_pen["plot_key"]]["pen_down_distance_mm"] += _length(frame)
    north_x = map_field.right - 8.0
    north_y = map_field.top + 11.0
    north_strokes = [
        [(north_x, north_y + 5.0), (north_x, north_y - 4.0)],
        [
            (north_x, north_y - 4.0),
            (north_x - 1.7, north_y - 0.6),
            (north_x, north_y - 1.5),
            (north_x + 1.7, north_y - 0.6),
            (north_x, north_y - 4.0),
        ],
    ]
    for stroke in north_strokes:
        _append_path(frame_group, stroke, attributes={"data-role": "north-mark"})
        pen_records[frame_pen["plot_key"]]["path_count"] += 1
        pen_records[frame_pen["plot_key"]]["pen_down_distance_mm"] += _length(stroke)
    north_copy, _ = _fit_stroke_text(
        "N",
        x=north_x,
        y=north_y + 5.8,
        preferred_cap_mm=2.8,
        maximum_width_mm=5.0,
        anchor="middle",
    )
    for stroke in north_copy:
        _append_path(labels_group, stroke, attributes={"data-role": "north-label"})
        pen_records[copy_pen["plot_key"]]["path_count"] += 1
        pen_records[copy_pen["plot_key"]]["pen_down_distance_mm"] += _length(stroke)
    bar_mm, bar_label = _nice_scale_bar(plan)
    bar_x = map_field.left + 7.0
    bar_y = map_field.bottom - 7.0
    scale_strokes = [
        [(bar_x, bar_y), (bar_x + bar_mm, bar_y)],
        [(bar_x, bar_y - 1.2), (bar_x, bar_y + 1.2)],
        [(bar_x + bar_mm, bar_y - 1.2), (bar_x + bar_mm, bar_y + 1.2)],
    ]
    for stroke in scale_strokes:
        _append_path(frame_group, stroke, attributes={"data-role": "scale-bar"})
        pen_records[frame_pen["plot_key"]]["path_count"] += 1
        pen_records[frame_pen["plot_key"]]["pen_down_distance_mm"] += _length(stroke)
    bar_copy, _ = _fit_stroke_text(
        bar_label,
        x=bar_x,
        y=bar_y + 2.0,
        preferred_cap_mm=2.1,
        maximum_width_mm=max(bar_mm, 14.0),
    )
    for stroke in bar_copy:
        _append_path(labels_group, stroke, attributes={"data-role": "scale-label"})
        pen_records[copy_pen["plot_key"]]["path_count"] += 1
        pen_records[copy_pen["plot_key"]]["pen_down_distance_mm"] += _length(stroke)

    # Reproduce the binding sheet border from the shared university/city-map
    # format contract.  It remains independent from the clipped map-field
    # frame and is emitted as real one-pass geometry.
    border = context.plate.get("border", {})
    border_style = str(border.get("style", "none"))
    outer = border.get("outer")
    if border_style != "none" and isinstance(outer, dict):
        border_role = str(border.get("nib_role", "heavy"))
        border_pen = _black_pen_for_width(_format_role_width(context, border_role))
        border_group = logical_group(
            border_pen,
            "transit-sheet-border",
            "Binding sheet border",
            "#24282B",
        )

        def border_rect(
            x: float, y: float, width: float, height: float, role: str
        ) -> None:
            points = [
                (x, y),
                (x + width, y),
                (x + width, y + height),
                (x, y + height),
                (x, y),
            ]
            _append_path(border_group, points, attributes={"data-role": role})
            pen_records[border_pen["plot_key"]]["path_count"] += 1
            pen_records[border_pen["plot_key"]]["pen_down_distance_mm"] += _length(
                points
            )

        outer_x = float(outer["x"])
        outer_y = float(outer["y"])
        outer_width = float(outer["width"])
        outer_height = float(outer["height"])
        border_rect(outer_x, outer_y, outer_width, outer_height, "sheet-border-outer")
        if border_style == "double":
            inset = float(border.get("inner_offset_mm", 3.0))
            border_rect(
                outer_x + inset,
                outer_y + inset,
                outer_width - 2.0 * inset,
                outer_height - 2.0 * inset,
                "sheet-border-inner",
            )

    # Side rail follows the A3/A4 binding format rather than operator trade dress.
    title_role = str(context.plate.get("type_nib_role", {}).get("title", "heavy"))
    title_pen = _black_pen_for_width(_format_role_width(context, title_role))
    title_group = logical_group(title_pen, "transit-title", "Title", "#24282B")
    for stroke in _fit_display_text(
        network.name,
        context.zones["title"],
        preferred_height_mm=float(context.plate["type_scale_mm"]["title"]),
    ):
        _append_path(title_group, stroke, attributes={"data-role": "title"})
        pen_records[title_pen["plot_key"]]["path_count"] += 1
        pen_records[title_pen["plot_key"]]["pen_down_distance_mm"] += _length(stroke)
    subtitle = (
        "REVIEW PROOF / NOT OFFICIAL OR COMPLETE"
        if network.kind in NATIONAL_OPERATOR_KINDS
        else f"GEOGRAPHIC {network.kind.upper().replace('-', ' ')}"
    )
    subtitle_strokes, _ = _fit_stroke_text(
        subtitle,
        x=context.zones["subtitle"].left,
        y=context.zones["subtitle"].top + 1.5,
        preferred_cap_mm=3.0,
        maximum_width_mm=context.zones["subtitle"].width,
    )
    for stroke in subtitle_strokes:
        _append_path(
            labels_group,
            stroke,
            attributes={"data-role": "subtitle", "data-copy": subtitle},
        )
        pen_records[copy_pen["plot_key"]]["path_count"] += 1
        pen_records[copy_pen["plot_key"]]["pen_down_distance_mm"] += _length(stroke)

    legend_top = context.zones["detail"].top + context.zones["detail"].height + 5.0
    legend_bottom = context.zones["attribution"].top - 7.0
    ordered_legend_lines = sorted(network.lines, key=lambda item: (item.order, item.id))
    collided_line_ids = {
        line_id
        for collision in physical_pen_colour_collisions
        for line_id in collision["line_ids"]
    }
    legend_entry_records: list[dict[str, Any]] = []

    def emit_legend_entry(
        line: TransitLine,
        *,
        x: float,
        y: float,
        column_width: float,
        column_index: int,
        row_index: int,
        sample_length_mm: float,
        label_cap_mm: float,
        label_y: float,
        mapping_cap_mm: float | None,
        mapping_y: float | None,
    ) -> None:
        width_plan = route_width_plans[line.id]
        pen = _line_pen(line, width_plan)
        route_group = physical_groups[pen["plot_key"]]
        display_hex = line.colour.display_hex.upper()
        mapping_copy = (
            f"{display_hex} > {str(pen['ink']).upper()} "
            f"{float(pen['nominal_nib_mm']):g} MM"
        )
        collision = line.id in collided_line_ids
        # Put the legend sample in the same logical colour/physical pen group.
        logical = ET.SubElement(
            route_group,
            svg_tag("g"),
            {
                "id": f"logical-legend-{line.id}",
                "data-logical-layer": f"legend-{line.id}",
                "stroke": str(pen["preview"]),
                "stroke-width": format_measurement(width_plan.fit.pen.mark_width_mm),
                "data-route-reference-colour": line.colour.display_hex,
                "data-route-preview-colour": str(pen["preview"]),
                "data-route-preview-source": "physical-pen-inventory",
                "data-route-preview-is-physical-ink-claim": "false",
                "data-legend-column": str(column_index + 1),
                "data-legend-row": str(row_index + 1),
                "data-display-colour-to-physical-pen": (
                    f"{display_hex}->{pen['pen_id']}"
                ),
                "data-physical-pen-colour-collision": (
                    "true" if collision else "false"
                ),
            },
        )
        sample_paths = _physical_route_paths(
            [
                (x, y),
                (x + sample_length_mm, y),
            ],
            width_plan,
        )
        for stroke_index, physical_offset_mm, sample in sample_paths:
            _append_path(
                logical,
                sample,
                attributes={
                    "data-role": "legend-sample",
                    "data-transit-line-id": line.id,
                    "data-physical-stroke-index": str(stroke_index + 1),
                    "data-physical-stroke-count": str(width_plan.fit.stroke_count),
                    "data-physical-offset-mm": format_measurement(physical_offset_mm),
                    "data-legend-column": str(column_index + 1),
                    "data-legend-row": str(row_index + 1),
                    "data-reference-display-colour": display_hex,
                    "data-physical-pen-id": str(pen["pen_id"]),
                    "data-physical-pen-colour-collision": (
                        "true" if collision else "false"
                    ),
                },
            )
        pen_records[pen["plot_key"]]["logical_layers"].append(f"legend-{line.id}")
        pen_records[pen["plot_key"]]["path_count"] += len(sample_paths)
        pen_records[pen["plot_key"]]["pen_down_distance_mm"] += sum(
            _length(sample) for _, _, sample in sample_paths
        )
        label_copy = (
            f"C {line.short_name.upper()}"
            if mapping_cap_mm is not None and collision
            else line.short_name.upper()
        )
        label_x = x + sample_length_mm + (3.0 if mapping_cap_mm is not None else 4.0)
        label_width = column_width - (label_x - x)
        copy, _ = _fit_stroke_text(
            label_copy,
            x=label_x,
            y=label_y,
            preferred_cap_mm=label_cap_mm,
            maximum_width_mm=label_width,
        )
        for stroke in copy:
            _append_path(
                labels_group,
                stroke,
                attributes={
                    "data-role": "legend-label",
                    "data-transit-line-id": line.id,
                    "data-copy": label_copy,
                    "data-legend-column": str(column_index + 1),
                    "data-legend-row": str(row_index + 1),
                    "data-physical-pen-colour-collision": (
                        "true" if collision else "false"
                    ),
                },
            )
            pen_records[copy_pen["plot_key"]]["path_count"] += 1
            pen_records[copy_pen["plot_key"]]["pen_down_distance_mm"] += _length(stroke)

        if mapping_cap_mm is not None and mapping_y is not None:
            mapping, _ = _fit_stroke_text(
                mapping_copy,
                x=label_x,
                y=mapping_y,
                preferred_cap_mm=mapping_cap_mm,
                maximum_width_mm=label_width,
                minimum_cap_mm=MINIMUM_VISIBLE_CREDIT_CAP_MM,
            )
            for stroke in mapping:
                _append_path(
                    labels_group,
                    stroke,
                    attributes={
                        "data-role": "legend-display-to-physical-pen",
                        "data-transit-line-id": line.id,
                        "data-copy": mapping_copy,
                        "data-reference-display-colour": display_hex,
                        "data-physical-pen-id": str(pen["pen_id"]),
                        "data-legend-column": str(column_index + 1),
                        "data-legend-row": str(row_index + 1),
                        "data-physical-pen-colour-collision": (
                            "true" if collision else "false"
                        ),
                    },
                )
                pen_records[copy_pen["plot_key"]]["path_count"] += 1
                pen_records[copy_pen["plot_key"]]["pen_down_distance_mm"] += _length(
                    stroke
                )

        legend_entry_records.append(
            {
                "line_id": line.id,
                "column": column_index + 1,
                "row": row_index + 1,
                "display_colour": display_hex,
                "physical_pen_id": str(pen["pen_id"]),
                "physical_ink": str(pen["ink"]),
                "physical_nib_mm": float(pen["nominal_nib_mm"]),
                "mapping_copy": mapping_copy if mapping_cap_mm is not None else None,
                "physical_pen_colour_collision": collision,
            }
        )

    is_operator_overview = network.kind == "national-operator-overview"
    collision_disclosure_lines: list[str] = []
    if is_operator_overview:
        column_count = 2 if len(ordered_legend_lines) > 12 else 1
        row_count = max(1, ceil(len(ordered_legend_lines) / column_count))
        column_gap_mm = 4.0 if column_count > 1 else 0.0
        column_width_mm = (
            context.zones["detail"].width - column_gap_mm * (column_count - 1)
        ) / column_count
        disclosure_cap_mm = MINIMUM_VISIBLE_CREDIT_CAP_MM
        if physical_pen_colour_collisions:
            collision_disclosure_lines = _wrap_stroke_text(
                "C = DISTINCT DISPLAY COLOURS SHARE ONE PHYSICAL PEN; "
                "HEX > INK NIB MAPPING IS SHOWN; SEE MANIFEST FOR GROUPS",
                cap_height_mm=disclosure_cap_mm,
                maximum_width_mm=context.zones["detail"].width,
            )
        disclosure_step_mm = disclosure_cap_mm + 0.8
        disclosure_height_mm = (
            len(collision_disclosure_lines) * disclosure_step_mm + 1.0
            if collision_disclosure_lines
            else 0.0
        )
        available_for_entries = max(
            legend_bottom - legend_top - disclosure_height_mm,
            1.0,
        )
        row = min(9.5, available_for_entries / row_count)
        if row + 1e-9 < 7.2:
            raise MapPlotterError(
                f"{network.name} has too many overview legend entries for "
                f"{network.format_id}; use a larger/alternate composition or "
                "consolidate declared display groups."
            )
        legend_cap = max(2.0, min(2.5, row * 0.28))
        mapping_cap = MINIMUM_VISIBLE_CREDIT_CAP_MM
        for index, line in enumerate(ordered_legend_lines):
            column_index = index // row_count
            row_index = index % row_count
            x = context.zones["detail"].left + column_index * (
                column_width_mm + column_gap_mm
            )
            row_top = legend_top + row_index * row
            text_block_height = legend_cap + 0.6 + mapping_cap
            label_y = row_top + max(0.2, (row - text_block_height) / 2.0)
            emit_legend_entry(
                line,
                x=x,
                y=row_top + row * 0.5,
                column_width=column_width_mm,
                column_index=column_index,
                row_index=row_index,
                sample_length_mm=8.0,
                label_cap_mm=legend_cap,
                label_y=label_y,
                mapping_cap_mm=mapping_cap,
                mapping_y=label_y + legend_cap + 0.6,
            )
        legend_entries_bottom = legend_top + row_count * row
        for line_index, disclosure_line in enumerate(collision_disclosure_lines):
            disclosure_y = legend_entries_bottom + 1.0 + line_index * disclosure_step_mm
            disclosure, _ = _fit_stroke_text(
                disclosure_line,
                x=context.zones["detail"].left,
                y=disclosure_y,
                preferred_cap_mm=disclosure_cap_mm,
                maximum_width_mm=context.zones["detail"].width,
                minimum_cap_mm=MINIMUM_VISIBLE_CREDIT_CAP_MM,
            )
            for stroke in disclosure:
                _append_path(
                    labels_group,
                    stroke,
                    attributes={
                        "data-role": "legend-colour-collision-disclosure",
                        "data-copy": disclosure_line,
                        "data-collision-group-count": str(
                            len(physical_pen_colour_collisions)
                        ),
                    },
                )
                pen_records[copy_pen["plot_key"]]["path_count"] += 1
                pen_records[copy_pen["plot_key"]]["pen_down_distance_mm"] += _length(
                    stroke
                )
        legend_content_bottom = legend_entries_bottom + disclosure_height_mm
        legend_layout = {
            "policy_version": "transit-national-operator-overview-legend-v1",
            "mode": (
                "two-column-display-to-physical-pen"
                if column_count == 2
                else "single-column-display-to-physical-pen"
            ),
            "entry_count": len(ordered_legend_lines),
            "column_count": column_count,
            "row_count": row_count,
            "row_height_mm": round(row, 6),
            "label_cap_height_mm": round(legend_cap, 6),
            "mapping_cap_height_mm": mapping_cap,
            "minimum_cap_height_mm": MINIMUM_VISIBLE_CREDIT_CAP_MM,
            "collision_marker": "C",
            "collision_disclosure_required": bool(physical_pen_colour_collisions),
            "collision_disclosure_visible": bool(collision_disclosure_lines),
            "collision_disclosure_lines": collision_disclosure_lines,
            "entries": legend_entry_records,
        }
    else:
        column_count = 1
        row_count = len(ordered_legend_lines)
        available = max(legend_bottom - legend_top, 1.0)
        row = min(8.0, available / max(row_count, 1))
        legend_cap = max(2.0, min(3.0, row * 0.42))
        if row + 1e-9 < 4.8:
            raise MapPlotterError(
                f"{network.name} has too many line entries for {network.format_id}; "
                "use a larger/alternate composition or consolidate declared display groups."
            )
        for index, line in enumerate(ordered_legend_lines):
            y = legend_top + index * row + row * 0.5
            emit_legend_entry(
                line,
                x=context.zones["detail"].left,
                y=y,
                column_width=context.zones["detail"].width,
                column_index=0,
                row_index=index,
                sample_length_mm=12.0,
                label_cap_mm=legend_cap,
                label_y=y - legend_cap * 0.5,
                mapping_cap_mm=None,
                mapping_y=None,
            )
        legend_content_bottom = legend_top + row_count * row
        legend_layout = {
            "policy_version": "transit-standard-single-column-legend-v1",
            "mode": "single-column-line-name",
            "entry_count": len(ordered_legend_lines),
            "column_count": 1,
            "row_count": row_count,
            "row_height_mm": round(row, 6),
            "label_cap_height_mm": round(legend_cap, 6),
            "mapping_cap_height_mm": None,
            "minimum_cap_height_mm": 2.0,
            "collision_marker": None,
            "collision_disclosure_required": False,
            "collision_disclosure_visible": False,
            "collision_disclosure_lines": [],
            "entries": legend_entry_records,
        }

    details = (
        f"SNAPSHOT {network.snapshot}",
        f"SCALE 1:{plan.projector.scale_denominator:,.0f}".replace(",", " "),
        f"{_counted_noun(len(network.lines), 'LINE')} / "
        f"{_counted_noun(len(plan.station_marks), 'STATION')}",
    )
    detail_zone = context.zones["detail"]
    for index, value in enumerate(details):
        copy, _ = _fit_stroke_text(
            value,
            x=detail_zone.left,
            y=detail_zone.top + index * 6.5,
            preferred_cap_mm=2.45,
            maximum_width_mm=detail_zone.width,
        )
        for stroke in copy:
            _append_path(
                labels_group,
                stroke,
                attributes={"data-role": "detail", "data-copy": value},
            )
            pen_records[copy_pen["plot_key"]]["path_count"] += 1
            pen_records[copy_pen["plot_key"]]["pen_down_distance_mm"] += _length(stroke)

    visible_credit_records, attribution_text = _visible_credit_records(network.sources)
    incomplete_credit_ids = [
        str(record["source_id"])
        for record in visible_credit_records
        if record.get("complete") is not True
    ]
    if incomplete_credit_ids:
        raise MapPlotterError(
            f"{network.name} cannot emit complete source credit for: "
            + ", ".join(incomplete_credit_ids)
        )
    attr_zone = context.zones["attribution"]
    credit_cap = MINIMUM_VISIBLE_CREDIT_CAP_MM
    credit_gap = 0.85
    credit_lines = _wrap_stroke_text(
        attribution_text,
        cap_height_mm=credit_cap,
        maximum_width_mm=attr_zone.width,
    )
    credit_top = legend_content_bottom + 5.0
    credit_bottom = attr_zone.bottom
    credit_line_step = credit_cap + credit_gap
    if credit_top + len(credit_lines) * credit_line_step > credit_bottom + 1e-9:
        raise MapPlotterError(
            f"{network.name} source credits need {len(credit_lines)} physical lines; "
            "use a larger/alternate composition or shorter binding attribution."
        )
    attribution_role = str(
        context.plate.get("type_nib_role", {}).get("attribution", "hairline")
    )
    attribution_pen = _black_pen_for_width(
        _format_role_width(context, attribution_role)
    )
    attribution_group = logical_group(
        attribution_pen,
        "transit-attribution",
        "Source attribution",
        "#24282B",
    )
    for line_index, credit_line in enumerate(credit_lines):
        credit, _ = _fit_stroke_text(
            credit_line,
            x=attr_zone.left,
            y=credit_top + line_index * credit_line_step,
            preferred_cap_mm=credit_cap,
            maximum_width_mm=attr_zone.width,
            minimum_cap_mm=credit_cap,
        )
        for stroke in credit:
            _append_path(
                attribution_group,
                stroke,
                attributes={
                    "data-role": "attribution",
                    "data-attribution-cap-height-mm": format_measurement(credit_cap),
                    "data-attribution-complete": "true",
                    "data-attribution-source-ids": " ".join(
                        source.id for source in network.sources
                    ),
                },
            )
            pen_records[attribution_pen["plot_key"]]["path_count"] += 1
            pen_records[attribution_pen["plot_key"]]["pen_down_distance_mm"] += _length(
                stroke
            )

    pen_sequence: list[dict[str, Any]] = []
    for step, (key, pen_record) in enumerate(pen_records.items(), start=1):
        group = physical_groups[key]
        group.set("data-pen-step", str(step))
        group.set(f"{{{INKSCAPE_NS}}}label", f"{step:02d} — {pen_record['label']}")
        pen_sequence.append(
            {
                "step": step,
                "plot_key": key,
                "pen_id": pen_record["pen_id"],
                "ink": pen_record["ink"],
                "nib_mm": pen_record["nib_mm"],
                "calibration_state": pen_record["calibration_state"],
                "colour_match": pen_record["match_status"],
                "logical_layers": list(dict.fromkeys(pen_record["logical_layers"])),
                "path_count": pen_record["path_count"],
                "pen_down_distance_mm": round(pen_record["pen_down_distance_mm"], 1),
                "instruction": f"Load {pen_record['label']} and plot step {step:02d}.",
            }
        )

    ordered_lines = sorted(network.lines, key=lambda item: (item.order, item.id))
    ordered_line_ids = [line.id for line in ordered_lines]
    actual_route_paths_by_line = _actual_emitted_route_paths_by_line(
        root, ordered_line_ids
    )
    actual_route_records_by_line = _actual_emitted_route_records_by_line(
        root, ordered_line_ids
    )
    route_stroke_coverage_line_results: list[dict[str, Any]] = []
    for line_id in ordered_line_ids:
        expected_records = expected_route_records_by_line[line_id]
        actual_records = actual_route_records_by_line[line_id]
        if set(actual_records) != set(expected_records):
            raise MapPlotterError(
                f"Actual SVG route-record parity failed for {line_id}: "
                f"{len(set(expected_records) - set(actual_records))} missing, "
                f"{len(set(actual_records) - set(expected_records))} unexpected."
            )
        split_path_count = 0
        for route_record_id, expected_record in expected_records.items():
            actual_record = actual_records[route_record_id]
            expected_indexes = set(expected_record["expected_stroke_indexes"])
            actual_indexes = set(actual_record["stroke_indexes"])
            if actual_indexes != expected_indexes:
                raise MapPlotterError(
                    f"Actual SVG stroke-index coverage failed for {route_record_id}: "
                    f"expected {sorted(expected_indexes)}, got "
                    f"{sorted(actual_indexes)}."
                )
            if actual_record["declared_stroke_count"] != len(expected_indexes):
                raise MapPlotterError(
                    f"Actual SVG stroke-count declaration failed for {route_record_id}."
                )
            if actual_record["offset_method"] != expected_record["offset_method"]:
                raise MapPlotterError(
                    f"Actual SVG offset-method evidence failed for {route_record_id}."
                )
            if actual_record["physical_union_width_scope"] != (
                PHYSICAL_UNION_WIDTH_SCOPE
            ):
                raise MapPlotterError(
                    f"Actual SVG width-scope evidence failed for {route_record_id}."
                )
            if (
                actual_record["local_physical_union_width_status"]
                != (expected_record["local_physical_union_width_status"])
            ):
                raise MapPlotterError(
                    f"Actual SVG local-width status failed for {route_record_id}."
                )
            expected_exact = (
                "true" if expected_record["exact_local_union_certified"] else "false"
            )
            if actual_record["exact_local_union_certified"] != expected_exact:
                raise MapPlotterError(
                    f"Actual SVG exact-width certification failed for "
                    f"{route_record_id}."
                )
            expected_stroke_complete = (
                "true" if expected_record["stroke_index_complete"] else "false"
            )
            if actual_record["stroke_index_complete"] != expected_stroke_complete:
                raise MapPlotterError(
                    f"Actual SVG stroke completeness failed for {route_record_id}."
                )
            expected_segment_coverage = (
                "true"
                if expected_record["segment_offset_coverage_certified"]
                else "false"
            )
            if actual_record["segment_offset_coverage_certified"] != (
                expected_segment_coverage
            ):
                raise MapPlotterError(
                    f"Actual SVG segment-offset coverage claim failed for "
                    f"{route_record_id}."
                )
            if actual_record["cyclic_path_status"] != expected_record[
                "cyclic_path_status"
            ]:
                raise MapPlotterError(
                    f"Actual SVG cyclic-path status failed for {route_record_id}."
                )
            expected_source_bound = (
                "true"
                if expected_record["source_excursion_bound_certified"]
                else "false"
            )
            if actual_record["source_excursion_bound_certified"] != (
                expected_source_bound
            ):
                raise MapPlotterError(
                    f"Actual SVG source-excursion certification failed for "
                    f"{route_record_id}."
                )
            expected_review = "true" if expected_record["review_required"] else "false"
            if actual_record["review_required"] != expected_review:
                raise MapPlotterError(
                    f"Actual SVG review-status evidence failed for {route_record_id}."
                )
            if (
                actual_record["continuous_per_stroke"] != "true"
                or actual_record["pen_lift_within_stroke"] != "false"
                or actual_record["physical_path_count"] != len(expected_indexes)
            ):
                raise MapPlotterError(
                    f"Actual SVG no-pen-lift continuity failed for {route_record_id}."
                )
            split_path_count += max(
                0,
                int(actual_record["physical_path_count"]) - len(actual_indexes),
            )
        route_stroke_coverage_line_results.append(
            {
                "line_id": line_id,
                "expected_logical_record_count": len(expected_records),
                "actual_logical_record_count": len(actual_records),
                "expected_stroke_index_memberships": sum(
                    len(record["expected_stroke_indexes"])
                    for record in expected_records.values()
                ),
                "actual_stroke_index_memberships": sum(
                    len(record["stroke_indexes"]) for record in actual_records.values()
                ),
                "physical_path_count": sum(
                    int(record["physical_path_count"])
                    for record in actual_records.values()
                ),
                "one_continuous_path_per_stroke_index": True,
                "pen_lift_within_stroke_index_count": 0,
                "split_offset_extra_path_count": split_path_count,
                "fallback_record_count": sum(
                    record["offset_method"]
                    in {
                        SHORT_ROUTE_OFFSET_METHOD,
                        SEGMENT_NORMAL_ROUTE_OFFSET_METHOD,
                    }
                    for record in expected_records.values()
                ),
                "rigid_fallback_record_count": sum(
                    record["offset_method"] == SHORT_ROUTE_OFFSET_METHOD
                    for record in expected_records.values()
                ),
                "segment_normal_fallback_record_count": sum(
                    record["offset_method"] == SEGMENT_NORMAL_ROUTE_OFFSET_METHOD
                    for record in expected_records.values()
                ),
                "nominal_review_required_record_count": sum(
                    not record["exact_local_union_certified"]
                    for record in expected_records.values()
                ),
                "segment_offset_coverage_certified_record_count": sum(
                    record["segment_offset_coverage_certified"]
                    for record in expected_records.values()
                ),
                "segment_offset_coverage_uncertified_record_count": sum(
                    not record["segment_offset_coverage_certified"]
                    for record in expected_records.values()
                ),
                "incomplete_record_count": 0,
                "all_records_complete": True,
            }
        )
    actual_route_memberships_by_line = _actual_emitted_route_memberships_by_line(
        root, ordered_line_ids
    )
    expected_route_memberships_by_line = {
        line.id: frozenset(
            edge.id for edge in network.edges if line.id in edge.line_ids
        )
        for line in ordered_lines
    }
    route_membership_mismatches = {
        line_id: {
            "missing": sorted(
                expected_route_memberships_by_line[line_id]
                - actual_route_memberships_by_line[line_id]
            ),
            "unexpected": sorted(
                actual_route_memberships_by_line[line_id]
                - expected_route_memberships_by_line[line_id]
            ),
        }
        for line_id in ordered_line_ids
        if actual_route_memberships_by_line[line_id]
        != expected_route_memberships_by_line[line_id]
    }
    if route_membership_mismatches:
        summary = "; ".join(
            f"{line_id}: {len(record['missing'])} missing, "
            f"{len(record['unexpected'])} unexpected"
            for line_id, record in route_membership_mismatches.items()
        )
        raise MapPlotterError(
            "Actual emitted SVG route membership parity failed: " + summary
        )

    line_records: list[dict[str, Any]] = []
    for line in ordered_lines:
        line_strokes = [
            route_stroke
            for route_stroke in plan.route_strokes
            if route_stroke.line_id == line.id
        ]
        planned_edge_ids = {
            edge_id
            for route_stroke in line_strokes
            for edge_id in route_stroke.source_membership_edge_ids
        }
        expected = expected_route_memberships_by_line[line.id]
        actually_emitted = actual_route_memberships_by_line[line.id]
        line_records.append(
            {
                "id": line.id,
                "name": line.name,
                "display_colour": line.colour.as_dict(),
                "render_preview_colour": _line_pen(line, route_width_plans[line.id])[
                    "preview"
                ],
                "declared_physical_pen": line.pen.as_dict(),
                "physical_pen": route_width_plans[line.id].fit.pen.as_dict(),
                "route_width_fit": route_width_plans[line.id].fit.as_dict(),
                "native_owned_nib_promotion": (
                    route_width_plans[line.id].native_owned_nib_promotion
                ),
                **_route_band_evidence(route_width_plans[line.id]),
                "source_ref": line.source_ref,
                "source_edge_count": len(expected),
                "planned_edge_count": len(planned_edge_ids),
                "planned_edge_parity": planned_edge_ids == expected,
                "emitted_edge_count": len(actually_emitted),
                "edge_parity": actually_emitted == expected,
                "emitted_edge_membership_source": (
                    "actual SVG physical route path data-transit-edge-ids union"
                ),
                "logical_path_count": len(line_strokes),
                "actual_svg_logical_record_count": len(
                    actual_route_records_by_line[line.id]
                ),
                "all_logical_records_have_complete_stroke_index_coverage": True,
                "physical_path_count": len(actual_route_paths_by_line[line.id]),
                "logical_centreline_distance_mm": round(
                    sum(_length(route_stroke.points) for route_stroke in line_strokes),
                    1,
                ),
                "physical_pen_down_distance_mm": round(
                    sum(
                        _length(physical_points)
                        for route_stroke in line_strokes
                        for _, _, physical_points in _physical_route_paths(
                            route_stroke.points, route_width_plans[line.id]
                        )
                    ),
                    1,
                ),
            }
        )
    colour_blockers = [
        line.id for line in network.lines if line.pen.match_status != "exact-measured"
    ]
    rights, rights_blocking_reasons = _rights_gate(
        network,
        visible_credit_records=visible_credit_records,
        visible_credit_cap_mm=credit_cap,
    )
    blocking_reasons = [
        "the built-in studio pens are nominal and unmeasured on the selected stock",
    ]
    if colour_blockers:
        blocking_reasons.append(
            "line colours without an exact measured physical pen: "
            + ", ".join(colour_blockers)
        )
    if physical_pen_colour_collisions:
        blocking_reasons.append(
            "physical pens assigned to multiple distinct reference colours: "
            + "; ".join(
                f"{record['plot_key']} ({', '.join(record['line_ids'])})"
                for record in physical_pen_colour_collisions
            )
        )
    blocking_reasons.extend(rights_blocking_reasons)
    if plan.short_route_strokes:
        blocking_reasons.append(
            "source-backed route detail below the three-nib floor is retained "
            "and requires physical review"
        )
    if route_offset_exception_records:
        blocking_reasons.append(
            "one or more non-short partial or collapsed offsets require "
            "source-segment smooth-join review; local physical union is "
            "nominal, not exact"
        )
    if plan.station_association_issues:
        blocking_reasons.append(
            "one or more station source positions could not be associated with their route geometry"
        )
    if context_stats["sub_three_nib_transport_emitted"]:
        blocking_reasons.append(
            "source-backed transport context detail below the three-nib floor "
            "is retained for completeness and requires physical review"
        )
    declared_break_count = sum(
        len(pattern.continuity_breaks) for pattern in network.service_patterns
    )
    if declared_break_count:
        blocking_reasons.append(
            "source route relations contain declared continuity gaps; no connector was invented"
        )
    manifest = {
        "schema_version": 1,
        "generator": f"city-map-plotter {__version__}",
        "generated_at": generated_at,
        "artifact_kind": "geographic-transit-network-pen-map",
        "artifact_id": network.id,
        "domain": "transit",
        "network": {
            "id": network.id,
            "name": network.name,
            "kind": network.kind,
            "scope": network.scope,
            "snapshot": network.snapshot,
            "validity_status": network.validity_status,
            "geometry_mode": network.geometry_mode,
            "contract_sha256": network.contract_sha256,
        },
        "sources": [source.as_dict() for source in network.sources],
        "rights": rights,
        "page": {
            "format_id": network.format_id,
            "width_mm": context.page.width,
            "height_mm": context.page.height,
            "map_bounds_mm": map_field.as_dict(),
            "format_map_bounds_mm": context.field.as_dict(),
            "zones_mm": {name: rect.as_dict() for name, rect in context.zones.items()},
        },
        "rendering": {
            "view_kind": "geographic-network",
            "map_composition": map_composition.as_dict(),
            "scale_tier": plan.scale_tier,
            "scale_denominator": round(plan.projector.scale_denominator),
            "station_label_policy": station_label_policy,
            "station_label_policy_audit": plan.station_label_policy_audit,
            "station_label_count": len(plan.station_labels),
            "omitted_station_label_ids": list(plan.omitted_station_labels),
            "legend_layout": legend_layout,
            "line_geometry_simplification_tolerance_mm": plan.simplification_tolerance_mm,
            "route_width_policy": {
                "policy_version": ROUTE_BAND_POLICY_VERSION,
                "requested_width_mm": plan.route_target_width_mm,
                "physical_union_width_scope": PHYSICAL_UNION_WIDTH_SCOPE,
                "scale_target_width_mm": SCALE_ROUTE_TARGETS_MM[plan.scale_tier],
                "format_maximum_width_mm": plan.route_target_maximum_width_mm,
                "network_kind_override": (
                    "national-operator-overview-single-pass"
                    if route_target_width_override_mm is not None
                    else None
                ),
                "scale_targets_mm": dict(SCALE_ROUTE_TARGETS_MM),
                "native_owned_nib_promotion_policy": {
                    "policy_version": NATIVE_OWNED_NIB_PROMOTION_POLICY_VERSION,
                    "scope": "exact-dated-registry-product-binding-only",
                    "generic_automatic_widening_allowed": False,
                    "unowned_nib_synthesis_allowed": False,
                    "colour_substitution_allowed": False,
                    "format_maximum_must_be_respected": True,
                    "one_pass_required": True,
                    "promotion_count": sum(
                        width_plan.native_owned_nib_promotion is not None
                        for width_plan in plan.route_width_plans
                    ),
                },
                "native_owned_nib_promotions": [
                    width_plan.native_owned_nib_promotion
                    for width_plan in plan.route_width_plans
                    if width_plan.native_owned_nib_promotion is not None
                ],
                "method": (
                    "one owned nib when available; otherwise adjacent parallel "
                    "overlapping strokes with no same-centre retrace; passes "
                    "emit outside-in so interior paths cover earlier preview edges"
                ),
                "preview_band_method": (
                    "the same separate physical paths in plot order; no digital "
                    "underlay, fill, or additional preview-only stroke"
                ),
                "width_certification_rule": (
                    "physical_union_width_mm is exact only on straight, locally "
                    "parallel runs; rigid short fragments and source-segment "
                    "smooth-join fallbacks are nominal and review-required"
                ),
                "segment_offset_coverage_gate": {
                    "required_for_standard_exact_claim": True,
                    "sample_fractions_per_source_segment": list(
                        ROUTE_SEGMENT_OFFSET_SAMPLE_FRACTIONS
                    ),
                    "in_memory_gate_tolerance_mm": (
                        ROUTE_SEGMENT_OFFSET_COVERAGE_TOLERANCE_MM
                    ),
                    "svg_coordinate_quantum_mm": SVG_COORDINATE_QUANTUM_MM,
                    "svg_coordinate_quantization_budget_mm": round(
                        SVG_COORDINATE_QUANTIZATION_BUDGET_MM, 9
                    ),
                    "serialized_audit_tolerance_mm": round(
                        SERIALIZED_SEGMENT_OFFSET_COVERAGE_TOLERANCE_MM, 9
                    ),
                    "all_planned_stroke_indexes_required": True,
                    "nonempty_but_partial_geos_offsets_are_rejected": True,
                },
                "detail_exception_policy": {
                    "policy_version": ROUTE_DETAIL_EXCEPTION_POLICY_VERSION,
                    "rule": (
                        "retain every non-degenerate source-backed route fragment; "
                        "mark fragments below three nib widths as review-required"
                    ),
                    "minimum_nondegenerate_length_mm": (
                        MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM
                    ),
                    "physical_offset_method": SHORT_ROUTE_OFFSET_METHOD,
                    "normal_policy": (
                        "unit normal of endpoint chord; longest non-zero segment "
                        "when the chord is closed or coincident"
                    ),
                    "geometry_policy": (
                        "rigidly translate the complete sourced centreline once "
                        "at every planned offset; never same-centre retrace"
                    ),
                    "physical_union_width_scope": PHYSICAL_UNION_WIDTH_SCOPE,
                    "local_physical_union_width_status": (NOMINAL_LOCAL_UNION_STATUS),
                    "exact_local_union_certified": False,
                    "sub_three_nib_record_count": len(route_detail_exception_records),
                    "all_sub_three_nib_records_emitted": all(
                        record["emitted"] for record in route_detail_exception_records
                    ),
                    "review_required": bool(route_detail_exception_records),
                },
                "segment_normal_offset_exception_policy": {
                    "physical_offset_method": SEGMENT_NORMAL_ROUTE_OFFSET_METHOD,
                    "trigger": (
                        "GEOS loses a planned stroke index or fails deterministic "
                        "shifted source-segment sample coverage"
                    ),
                    "geometry_policy": (
                        "translate each explicit source segment by its signed "
                        "normal end-to-end; use tangent circular arcs outside "
                        "turns and tangent-matched sampled cubics inside turns; "
                        "concatenate one pen-down path per index sampled from "
                        "the C1 construction, retain self-overlap, and close a "
                        "simple source cycle through the same bounded final/first "
                        "join; reject non-simple, zero-area, non-finite, or "
                        "unbounded fallback geometry"
                    ),
                    "cyclic_path_status": CYCLIC_OFFSET_STATUS_CERTIFIED,
                    "cyclic_source_requirement": (
                        "simple non-self-intersecting valid non-zero-area cycle"
                    ),
                    "source_excursion_factor": (
                        SEGMENT_NORMAL_SOURCE_EXCURSION_FACTOR
                    ),
                    "source_excursion_evidence": (
                        "maximum distance to each join's incident source vertex; "
                        "a conservative construction-derived upper bound on "
                        "distance to the complete source line"
                    ),
                    "source_excursion_tolerance_mm": (
                        SEGMENT_NORMAL_SOURCE_EXCURSION_TOLERANCE_MM
                    ),
                    "source_excursion_bound_certified_for_all_records": all(
                        record["source_excursion_bound_certified"]
                        for record in route_offset_exception_records
                    ),
                    "physical_union_width_scope": PHYSICAL_UNION_WIDTH_SCOPE,
                    "local_physical_union_width_status": (NOMINAL_LOCAL_UNION_STATUS),
                    "exact_local_union_certified": False,
                    "record_count": len(route_offset_exception_records),
                    "review_required": bool(route_offset_exception_records),
                },
                "line_fits": [
                    {
                        "line_id": width_plan.line_id,
                        **width_plan.fit.as_dict(),
                        "native_owned_nib_promotion": (
                            width_plan.native_owned_nib_promotion
                        ),
                        **_route_band_evidence(width_plan),
                    }
                    for width_plan in plan.route_width_plans
                ],
            },
            "shared_corridor_lane_pitch_mm": plan.lane_pitch_mm,
            "shared_corridor_minimum_clearance_mm": plan.lane_clearance_mm,
            "shared_corridor_policy": (
                "deterministic global line order; symmetric lanes remain offset "
                "through compatible degree-two boundaries and taper to the exact "
                "node only at termini, true junctions, or membership/order changes; "
                "sustained paper-scale near-coincident paths of the same display "
                "line are represented once with source-membership provenance"
            ),
            "station_label_collision_policy": {
                "method": (
                    "deterministic 32-position whole-label search; omit when no "
                    "candidate is clear; never clip or abbreviate"
                ),
                "obstacles": [
                    "physical route envelopes",
                    "every station symbol",
                    "map frame",
                    "north-mark region",
                    "scale-bar region",
                    "previously placed station labels",
                ],
                "route_clearance_mm": LABEL_CLEARANCE_MM,
                "frame_clearance_mm": LABEL_FRAME_CLEARANCE_MM,
                "inter_label_gap_mm": LABEL_TO_LABEL_GAP_MM,
                "all_emitted_labels_clear": True,
            },
            "context_policy": sorted(allowed_context),
            "context_selection": {
                "policy_version": "transit-semantic-scale-context-v3",
                "network_policy": (
                    "named-operator-geographic-orientation"
                    if network.kind in NATIONAL_OPERATOR_KINDS
                    else "general-house-transit"
                ),
                "scale_tier": plan.scale_tier,
                "allowed_kinds": sorted(allowed_context),
                "preview_overrides": {
                    kind: {
                        "preview_colour": _context_preview_for(
                            network, plan.scale_tier, kind
                        ),
                        "physical_pen_id": CONTEXT_STYLE[kind][0],
                        "physical_nib_mm": _context_nib_mm(kind),
                        "display_only": True,
                    }
                    for kind in sorted(allowed_context)
                    if _context_preview_for(network, plan.scale_tier, kind)
                    != CONTEXT_STYLE[kind][1]
                },
                "named_route_remains_complete": True,
                "generic_physical_rail_proves_operator_service": False,
                "selection_rule": {
                    tier: {
                        "allowed_kinds": sorted(kinds),
                        "complete_supported_house_vocabulary": (
                            kinds == frozenset(CONTEXT_STYLE)
                        ),
                    }
                    for tier, kinds in (
                        NATIONAL_OPERATOR_CONTEXT_BY_SCALE
                        if network.kind in NATIONAL_OPERATOR_KINDS
                        else CONTEXT_BY_SCALE
                    ).items()
                },
            },
            "context_scale_omitted_trail_ids": context_omitted,
            "context_omission_ids": [
                entry["omission_id"]
                for entry in context_stats["omission_ledger"]["entries"]
            ]
            + [
                f"water-line-fully-suppressed:{feature_id}"
                for feature_id in water_line_stats["fully_suppressed_feature_ids"]
            ],
            "context_omitted_feature_ids": sorted(
                {
                    feature_id
                    for entry in context_stats["omission_ledger"]["entries"]
                    for feature_id in entry["represented_feature_ids"]
                }
                | set(water_line_stats["fully_suppressed_feature_ids"])
            ),
            "context_topology": context_assembly_stats,
            "context_knockout": {
                "method": (
                    "true geometric subtraction of the exact emitted physical "
                    "route-pass union plus local clearance; no white halo ink"
                ),
                "route_envelope_source": "emitted physical offset paths",
                "route_clearance_mm": ROUTE_CONTEXT_CLEARANCE_MM,
                "global_maximum_lane_inflation_used": False,
                "transport_detail_exception_policy": {
                    "policy_version": TRANSPORT_DETAIL_EXCEPTION_POLICY_VERSION,
                    "kinds": sorted(TRANSPORT_DETAIL_EXCEPTION_KINDS),
                    "applicable_scale_tiers": sorted(
                        TRANSPORT_DETAIL_EXCEPTION_SCALE_TIERS
                    ),
                    "current_scale_tier": plan.scale_tier,
                    "active_for_current_scale": (
                        plan.scale_tier in TRANSPORT_DETAIL_EXCEPTION_SCALE_TIERS
                    ),
                    "rule": (
                        "compact and urban sheets retain non-degenerate "
                        "source-backed road, path, and railway fragments after "
                        "exact knockout; regional and national sheets use the "
                        "university/marathon serialized paper-scale floor"
                    ),
                    "invented_join_count": 0,
                    "non_transport_floor_still_applies": True,
                    "review_required": bool(
                        context_stats["sub_three_nib_transport_emitted"]
                    ),
                },
                **context_stats,
            },
            "water_treatment": {
                "policy_version": WATER_TREATMENT_POLICY_VERSION,
                "physical_semantics": (
                    "mapped Blue 0.40 water-area banks plus Blue 0.25 closed-dot "
                    "stipple; "
                    "unwidth-sourced linear water remains a plain Blue 0.25 line"
                ),
                "bank_outlines": {
                    **water_surface_stats,
                    "physical_pen_id": CONTEXT_STYLE["water-areas"][0],
                    "physical_nib_mm": _context_nib_mm("water-areas"),
                    "emitted_after_knockout_path_count": sum(
                        stroke.kind == "water-areas" for stroke in context_strokes
                    ),
                    "emitted_after_knockout_length_mm": round(
                        sum(
                            _length(stroke.points)
                            for stroke in context_strokes
                            if stroke.kind == "water-areas"
                        ),
                        6,
                    ),
                    "source_boundaries_not_replaced_by_fill": True,
                },
                "area_stipple": water_stipple_stats,
                "water_line_suppression": {
                    **water_line_stats,
                    "emitted_after_knockout_path_count": sum(
                        stroke.kind == "water-lines" for stroke in context_strokes
                    ),
                    "emitted_after_knockout_length_mm": round(
                        sum(
                            _length(stroke.points)
                            for stroke in context_strokes
                            if stroke.kind == "water-lines"
                        ),
                        6,
                    ),
                },
                "source_lineage": {
                    "bank_paths": "data-source-ref and data-source-object",
                    "dot_paths": (
                        "data-represented-feature-ids, data-source-refs and "
                        "data-source-objects"
                    ),
                    "linear_paths": "data-source-ref and data-source-object",
                    "retained_on_every_emitted_path": True,
                },
                "knockout": {
                    "method": "geometric exclusion before path emission",
                    "obstacles": [
                        "route envelopes",
                        "station symbols",
                        "station labels",
                    ],
                    "white_ink_used": False,
                },
                "source_width_inference_used": False,
            },
            "north_mark": True,
            "scale_bar": True,
            "display_font": display_font_contract(),
            "operator_diagram_traced": False,
            "operator_logo_used": False,
            "visible_source_credit": {
                "text": attribution_text,
                "line_count": len(credit_lines),
                "cap_height_mm": credit_cap,
                "minimum_cap_height_mm": MINIMUM_VISIBLE_CREDIT_CAP_MM,
                "complete": rights["visible_credit"]["complete"],
                "source_records": visible_credit_records,
                "truncated": False,
            },
        },
        "topology_qa": {
            "node_count": len(network.nodes),
            "station_count": len(plan.station_marks),
            "edge_count": len(network.edges),
            "service_pattern_count": len(network.service_patterns),
            "source_edge_memberships": sum(
                len(edge.line_ids) for edge in network.edges
            ),
            "planned_edge_memberships": plan.emitted_edge_memberships,
            "planned_edge_membership_parity": plan.emitted_edge_memberships
            == sum(len(edge.line_ids) for edge in network.edges),
            "emitted_edge_memberships": sum(
                len(edge_ids) for edge_ids in actual_route_memberships_by_line.values()
            ),
            "edge_membership_parity": all(
                actual_route_memberships_by_line[line_id]
                == expected_route_memberships_by_line[line_id]
                for line_id in ordered_line_ids
            ),
            "emitted_edge_membership_source": (
                "actual SVG physical route path data-transit-edge-ids union"
            ),
            "actual_emitted_membership_validation": "passed",
            "declared_pattern_break_count": declared_break_count,
            "all_service_patterns_continuous": declared_break_count == 0,
            "stroke_join_identity": "shared normalized graph node ID, never coordinate coincidence",
            "station_association_method": (
                "nearest point on a source-member edge for the station's declared lines; "
                "maximum 2.0 mm paper displacement"
            ),
            "station_association_issue_count": len(plan.station_association_issues),
            "station_association_issues": list(plan.station_association_issues),
            "maximum_station_displacement_mm": round(
                max(
                    (
                        mark.displacement_mm
                        for mark in plan.station_marks
                        if isfinite(mark.displacement_mm)
                    ),
                    default=0.0,
                ),
                6,
            ),
            "short_route_strokes": list(plan.short_route_strokes),
            "route_detail_exception": {
                "policy_version": ROUTE_DETAIL_EXCEPTION_POLICY_VERSION,
                "minimum_nondegenerate_length_mm": (
                    MINIMUM_NONDEGENERATE_ROUTE_LENGTH_MM
                ),
                "sub_three_nib_record_count": len(route_detail_exception_records),
                "sub_three_nib_membership_count": len(
                    {
                        edge_id
                        for record in route_detail_exception_records
                        for edge_id in record["source_membership_edge_ids"]
                    }
                ),
                "all_records_emitted": all(
                    record["emitted"] for record in route_detail_exception_records
                ),
                "review_required": bool(route_detail_exception_records),
                "records": route_detail_exception_records,
            },
            "route_offset_exception": {
                "physical_offset_method": SEGMENT_NORMAL_ROUTE_OFFSET_METHOD,
                "physical_union_width_scope": PHYSICAL_UNION_WIDTH_SCOPE,
                "local_physical_union_width_status": NOMINAL_LOCAL_UNION_STATUS,
                "exact_local_union_certified": False,
                "record_count": len(route_offset_exception_records),
                "all_records_emitted": all(
                    record["emitted_physical_path_count"] > 0
                    for record in route_offset_exception_records
                ),
                "cyclic_fallback_record_count": sum(
                    record["cyclic_path_status"] == CYCLIC_OFFSET_STATUS_CERTIFIED
                    for record in route_offset_exception_records
                ),
                "source_excursion_bound_certified_for_all_records": all(
                    record["source_excursion_bound_certified"]
                    for record in route_offset_exception_records
                ),
                "review_required": bool(route_offset_exception_records),
                "records": route_offset_exception_records,
            },
            "route_physical_stroke_coverage": {
                "policy_version": ROUTE_RECORD_ID_POLICY_VERSION,
                "record_identity": (
                    "SHA-256 prefix over line, start/end nodes, representative "
                    "and source-membership edge IDs"
                ),
                "evidence_source": (
                    "actual SVG physical paths grouped by data-transit-route-record-id"
                ),
                "expected_logical_record_count": sum(
                    len(records) for records in expected_route_records_by_line.values()
                ),
                "actual_logical_record_count": sum(
                    len(records) for records in actual_route_records_by_line.values()
                ),
                "expected_stroke_index_memberships": sum(
                    int(record["expected_stroke_index_memberships"])
                    for record in route_stroke_coverage_line_results
                ),
                "actual_stroke_index_memberships": sum(
                    int(record["actual_stroke_index_memberships"])
                    for record in route_stroke_coverage_line_results
                ),
                "physical_path_count": sum(
                    int(record["physical_path_count"])
                    for record in route_stroke_coverage_line_results
                ),
                "split_offset_extra_path_count": sum(
                    int(record["split_offset_extra_path_count"])
                    for record in route_stroke_coverage_line_results
                ),
                "fallback_record_count": sum(
                    int(record["fallback_record_count"])
                    for record in route_stroke_coverage_line_results
                ),
                "rigid_fallback_record_count": sum(
                    int(record["rigid_fallback_record_count"])
                    for record in route_stroke_coverage_line_results
                ),
                "segment_normal_fallback_record_count": sum(
                    int(record["segment_normal_fallback_record_count"])
                    for record in route_stroke_coverage_line_results
                ),
                "nominal_review_required_record_count": sum(
                    int(record["nominal_review_required_record_count"])
                    for record in route_stroke_coverage_line_results
                ),
                "segment_offset_coverage_certified_record_count": sum(
                    int(record["segment_offset_coverage_certified_record_count"])
                    for record in route_stroke_coverage_line_results
                ),
                "segment_offset_coverage_uncertified_record_count": sum(
                    int(record["segment_offset_coverage_uncertified_record_count"])
                    for record in route_stroke_coverage_line_results
                ),
                "segment_offset_sample_fractions": list(
                    ROUTE_SEGMENT_OFFSET_SAMPLE_FRACTIONS
                ),
                "segment_offset_coverage_tolerance_mm": (
                    ROUTE_SEGMENT_OFFSET_COVERAGE_TOLERANCE_MM
                ),
                "serialized_segment_offset_coverage_tolerance_mm": round(
                    SERIALIZED_SEGMENT_OFFSET_COVERAGE_TOLERANCE_MM, 9
                ),
                "fallback_route_record_ids": sorted(
                    {
                        record["route_record_id"]
                        for record in (
                            *route_detail_exception_records,
                            *route_offset_exception_records,
                        )
                    }
                ),
                "incomplete_record_count": 0,
                "all_records_complete": True,
                "all_records_one_continuous_path_per_stroke_index": True,
                "pen_lift_within_stroke_index_count": 0,
                "line_results": route_stroke_coverage_line_results,
            },
            "same_line_corridor_generalization": plan.same_line_corridor_audit,
            "line_results": line_records,
            "omissions": [dict(item) for item in network.omissions],
        },
        "colour_contract": {
            "reference_preview_is_not_a_physical-ink_claim": True,
            "physical_pen_colour_collisions": physical_pen_colour_collisions,
            "overview_plate_collision_disclosure": {
                "applicable": is_operator_overview,
                "required": (
                    is_operator_overview and bool(physical_pen_colour_collisions)
                ),
                "visible": legend_layout["collision_disclosure_visible"],
                "marker": legend_layout["collision_marker"],
                "mapping_format": (
                    "DISPLAY HEX > PHYSICAL INK NIB" if is_operator_overview else None
                ),
                "collision_group_count": len(physical_pen_colour_collisions),
                "collided_line_ids": sorted(collided_line_ids),
            },
            "line_colours": [
                {
                    "line_id": line.id,
                    "colour": line.colour.as_dict(),
                    "render_preview_colour": _line_pen(
                        line, route_width_plans[line.id]
                    )["preview"],
                    "render_preview_source": "physical-pen-inventory",
                    "physical_pen": {
                        **route_width_plans[line.id].fit.pen.as_dict(),
                        "plot_key": route_width_plans[line.id].fit.pen.identity,
                    },
                    "declared_pen": line.pen.as_dict(),
                    "route_width_fit": route_width_plans[line.id].fit.as_dict(),
                }
                for line in sorted(
                    network.lines, key=lambda item: (item.order, item.id)
                )
            ],
        },
        "pen_sequence": pen_sequence,
        "production_readiness": {
            "production_ready": False,
            "mode": "review-only",
            "acquisition_enabled_is_production_approval": False,
            "rights_ready": rights["rights_ready_for_production"],
            "rights_blocking_reasons": rights_blocking_reasons,
            "physical_pen_colour_collision_count": len(physical_pen_colour_collisions),
            "blocking_reasons": blocking_reasons,
        },
        "warnings": [
            "REVIEW OUTPUT ONLY — calibrate every selected pen on the exact stock and speed before plotting.",
            "Route colours are evidence-labelled reference colours; the manifest records whether an exact physical ink exists.",
            "This original geographic rendering uses no operator logo, roundel, copied typography, or traced schematic geometry.",
        ],
        "outputs": {},
    }
    return root, manifest


def _pen_only_tree(root: ET.Element, plot_key: str) -> ET.Element:
    result = copy.deepcopy(root)
    for child in list(result):
        if (
            child.tag == svg_tag("g")
            and child.get("data-physical-pen-key")
            and child.get("data-physical-pen-key") != plot_key
        ):
            result.remove(child)
    return result


def _rasterize(svg_path: Path, png_path: Path, *, dpi: float) -> None:
    inkscape = shutil.which("inkscape")
    if inkscape is None:
        raise MapPlotterError("PNG export requires Inkscape on PATH.")
    result = subprocess.run(
        [
            inkscape,
            str(svg_path),
            "--export-type=png",
            "--export-area-page",
            f"--export-dpi={dpi:g}",
            "--export-background=#FCFBF7",
            "--export-background-opacity=255",
            f"--export-filename={png_path}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise MapPlotterError(
            "Inkscape PNG export failed: " + (result.stderr or result.stdout).strip()
        )


def write_transit_plate(
    network: TransitNetwork,
    output_dir: Path,
    *,
    station_label_policy: str = "key",
    png: bool = True,
    png_dpi: float = 180.0,
    split_pens: bool = True,
    generated_at: str | None = None,
    allow_route_only: bool = False,
) -> dict[str, Any]:
    if not network.context and not allow_route_only:
        raise MapPlotterError(
            "Transit plate writing requires pinned geographic context. "
            "Attach the house context first; set allow_route_only=True only "
            "for an explicitly diagnostic route-only proof."
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    root, manifest = render_transit_plate(
        network,
        station_label_policy=station_label_policy,
        generated_at=generated_at,
        allow_route_only=allow_route_only,
    )
    ET.indent(root, space="  ")
    svg_path = output_dir / f"{network.id}.svg"
    manifest_path = output_dir / f"{network.id}.plot.json"
    ET.ElementTree(root).write(svg_path, encoding="utf-8", xml_declaration=True)
    pen_files: list[dict[str, Any]] = []
    if split_pens:
        for record in manifest["pen_sequence"]:
            step = int(record["step"])
            plot_key = str(record["plot_key"])
            pen_path = output_dir / f"{network.id}.pen-{step:02d}-{plot_key}.svg"
            pen_root = _pen_only_tree(root, plot_key)
            ET.indent(pen_root, space="  ")
            ET.ElementTree(pen_root).write(
                pen_path, encoding="utf-8", xml_declaration=True
            )
            pen_files.append(
                {
                    "step": step,
                    "plot_key": plot_key,
                    "path": str(pen_path.resolve()),
                    "sha256": _sha256(pen_path),
                }
            )
    outputs: dict[str, Any] = {
        "svg": {"path": str(svg_path.resolve()), "sha256": _sha256(svg_path)},
        "manifest": {"path": str(manifest_path.resolve())},
        "pen_files": pen_files,
    }
    if png:
        png_path = output_dir / f"{network.id}.png"
        _rasterize(svg_path, png_path, dpi=png_dpi)
        outputs["png"] = {
            "path": str(png_path.resolve()),
            "dpi": png_dpi,
            "sha256": _sha256(png_path),
        }
    manifest["outputs"] = outputs
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    outputs["manifest"]["sha256"] = _sha256(manifest_path)
    return outputs

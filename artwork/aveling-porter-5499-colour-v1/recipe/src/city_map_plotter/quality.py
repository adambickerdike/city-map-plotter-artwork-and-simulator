from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import isfinite
from typing import Any

from shapely.geometry import LineString
from shapely.strtree import STRtree

from .models import LayerStyle, PlotStroke


SVG_COORDINATE_PRECISION_MM = 0.001


def ground_metres(page_mm: float, scale_denominator: float) -> float:
    """Convert a physical page distance to its approximate ground distance."""

    if not isfinite(page_mm) or page_mm < 0:
        raise ValueError("page_mm must be a finite non-negative number")
    if not isfinite(scale_denominator) or scale_denominator <= 0:
        raise ValueError("scale_denominator must be a finite positive number")
    return page_mm * scale_denominator / 1_000


@dataclass(frozen=True)
class _PhysicalLine:
    index: int
    layer: str
    source_identity: str
    line: LineString
    half_width_mm: float


@dataclass(frozen=True)
class _ConflictScan:
    conflict_count: int
    pair_counts: dict[str, int]
    minimum_gap_mm: float | None
    candidate_pairs_evaluated: int
    truncated: bool


def _physical_lines(
    strokes: list[PlotStroke], styles: list[LayerStyle]
) -> list[_PhysicalLine]:
    style_by_layer = {style.id: style for style in styles}
    result: list[_PhysicalLine] = []
    for index, stroke in enumerate(strokes):
        style = style_by_layer.get(stroke.layer)
        if style is None or len(stroke.points) < 2:
            continue
        line = LineString(stroke.points)
        if line.is_empty or line.length <= 1e-9:
            continue
        assert style.nib_mm is not None
        raw_mark_width = stroke.tags.get("plot:nib-mm", str(style.nib_mm))
        try:
            mark_width_mm = float(raw_mark_width)
        except (TypeError, ValueError):
            mark_width_mm = float(style.nib_mm)
        source_identity = stroke.tags.get("source-refs", "")
        if not source_identity and stroke.osm_id not in {
            "",
            "multiple",
            "unknown",
        }:
            source_identity = f"{stroke.osm_type}/{stroke.osm_id}"
        result.append(
            _PhysicalLine(
                index=index,
                layer=stroke.layer,
                source_identity=source_identity,
                line=line,
                half_width_mm=mark_width_mm / 2,
            )
        )
    return result


def _separation_outside_junctions(
    left: _PhysicalLine, right: _PhysicalLine, separable_gap_mm: float
) -> float | None:
    """Measure separation after excluding ordinary point junctions.

    A global ``distance == 0`` test hides long, close approaches whenever two
    streets share one endpoint.  Remove a small physical neighbourhood around
    point intersections, then test what remains. Linear overlap is not an
    ordinary junction and is reported as a zero-gap conflict.
    """

    intersection = left.line.intersection(right.line)
    if intersection.is_empty:
        return float(left.line.distance(right.line))
    if intersection.length > 1e-9:
        return 0.0
    exclusion = intersection.buffer(max(0.05, 2 * separable_gap_mm))
    left_remainder = left.line.difference(exclusion)
    right_remainder = right.line.difference(exclusion)
    if left_remainder.is_empty or right_remainder.is_empty:
        return None
    return float(left_remainder.distance(right_remainder))


def _close_path_conflicts(
    strokes: list[PlotStroke],
    styles: list[LayerStyle],
    *,
    maximum_candidate_pairs: int,
) -> _ConflictScan:
    """Count distinct paths whose emitted pen marks may physically coalesce.

    This is intentionally a proximity metric, not an orientation classifier:
    parallel streets, acute approaches, and perpendicular near-misses can all
    merge on paper. It is diagnostic only and never removes geometry.
    """

    records = _physical_lines(strokes, styles)
    if len(records) < 2:
        return _ConflictScan(0, {}, None, 0, False)

    geometries = [record.line for record in records]
    tree = STRtree(geometries)
    maximum_half_width = max(record.half_width_mm for record in records)
    conflicts = 0
    pair_counts: Counter[str] = Counter()
    minimum_gap: float | None = None
    candidate_pairs_evaluated = 0
    truncated = False
    for left_index, left in enumerate(records):
        search_radius = left.half_width_mm + maximum_half_width
        candidate_indices = tree.query(left.line.buffer(search_radius))
        for right_index_value in candidate_indices:
            right_index = int(right_index_value)
            if right_index <= left_index:
                continue
            candidate_pairs_evaluated += 1
            if candidate_pairs_evaluated > maximum_candidate_pairs:
                truncated = True
                break
            right = records[right_index]
            # Multiple fragments from the same source are not independent
            # physical features and should not inflate the conflict count.
            if left.source_identity and left.source_identity == right.source_identity:
                continue
            separable_gap = left.half_width_mm + right.half_width_mm
            distance = _separation_outside_junctions(left, right, separable_gap)
            if distance is None:
                continue
            if distance >= separable_gap:
                continue
            conflicts += 1
            layer_pair = "/".join(sorted((left.layer, right.layer)))
            pair_counts[layer_pair] += 1
            minimum_gap = (
                distance if minimum_gap is None else min(minimum_gap, distance)
            )
        if truncated:
            break
    return _ConflictScan(
        conflicts,
        dict(sorted(pair_counts.items())),
        minimum_gap,
        min(candidate_pairs_evaluated, maximum_candidate_pairs),
        truncated,
    )


def physical_resolution_report(
    strokes: list[PlotStroke],
    styles: list[LayerStyle],
    *,
    scale_denominator: float,
    simplify_mm: float,
    detect_conflicts: bool = True,
    maximum_candidate_pairs: int = 2_000_000,
) -> tuple[dict[str, Any], list[str]]:
    """Describe physical fidelity without silently changing the geometry."""

    if not isfinite(simplify_mm) or simplify_mm < 0:
        raise ValueError("simplify_mm must be a finite non-negative number")
    if (
        isinstance(maximum_candidate_pairs, bool)
        or not isinstance(maximum_candidate_pairs, int)
        or maximum_candidate_pairs <= 0
    ):
        raise ValueError("maximum_candidate_pairs must be a positive integer")
    one_mm_ground = ground_metres(1.0, scale_denominator)
    plotted_widths_by_layer: dict[str, list[float]] = {}
    nib_widths_by_layer: dict[str, list[float]] = {}
    nominal_nibs_by_layer: dict[str, list[float]] = {}
    for stroke in strokes:
        value = stroke.tags.get("plot:plotted-width-mm")
        if value is None:
            continue
        try:
            plotted_widths_by_layer.setdefault(stroke.layer, []).append(float(value))
        except ValueError:
            continue
        try:
            nib_widths_by_layer.setdefault(stroke.layer, []).append(
                float(stroke.tags["plot:nib-mm"])
            )
            nominal_nibs_by_layer.setdefault(stroke.layer, []).append(
                float(
                    stroke.tags.get("plot:nominal-nib-mm", stroke.tags["plot:nib-mm"])
                )
            )
        except (KeyError, ValueError):
            pass
    layers: list[dict[str, Any]] = []
    for style in styles:
        assert style.nib_mm is not None
        nib_options = sorted(
            set(nib_widths_by_layer.get(style.id, [float(style.nib_mm)]))
        )
        nominal_options = sorted(
            set(nominal_nibs_by_layer.get(style.id, [float(style.nib_mm)]))
        )
        effective_nib = max(nib_options)
        effective_width = max(
            plotted_widths_by_layer.get(style.id, [style.plotted_width_mm])
        )
        layers.append(
            {
                "id": style.id,
                "nib_mm": round(effective_nib, 4),
                "nib_options_mm": [round(value, 4) for value in nib_options],
                "nominal_nib_options_mm": [
                    round(value, 4) for value in nominal_options
                ],
                "plotted_width_mm": round(effective_width, 4),
                "nib_ground_width_m": round(
                    ground_metres(effective_nib, scale_denominator), 3
                ),
                "plotted_ground_width_m": round(
                    ground_metres(effective_width, scale_denominator), 3
                ),
            }
        )

    if detect_conflicts:
        conflict_scan = _close_path_conflicts(
            strokes,
            styles,
            maximum_candidate_pairs=maximum_candidate_pairs,
        )
    else:
        conflict_scan = None
    report: dict[str, Any] = {
        "scale_denominator": round(scale_denominator),
        "ground_metres_per_page_mm": round(one_mm_ground, 3),
        "requested_simplify_mm": round(simplify_mm, 4),
        "requested_simplify_ground_m": round(
            ground_metres(simplify_mm, scale_denominator), 3
        ),
        "svg_coordinate_precision_mm": SVG_COORDINATE_PRECISION_MM,
        "svg_coordinate_precision_ground_m": round(
            ground_metres(SVG_COORDINATE_PRECISION_MM, scale_denominator), 3
        ),
        "conflict_scan_performed": detect_conflicts,
        "below_nib_separation_pair_count": (
            conflict_scan.conflict_count if conflict_scan is not None else None
        ),
        "below_nib_separation_pairs_by_layers": (
            conflict_scan.pair_counts if conflict_scan is not None else {}
        ),
        # Backwards-compatible aliases retained for existing manifest readers.
        "close_parallel_pair_count": (
            conflict_scan.conflict_count if conflict_scan is not None else None
        ),
        "close_parallel_pairs_by_layers": (
            conflict_scan.pair_counts if conflict_scan is not None else {}
        ),
        "candidate_pairs_evaluated": (
            conflict_scan.candidate_pairs_evaluated if conflict_scan is not None else 0
        ),
        "conflict_scan_pair_limit": maximum_candidate_pairs,
        "conflict_scan_truncated": (
            conflict_scan.truncated if conflict_scan is not None else False
        ),
        "minimum_close_gap_mm": (
            round(conflict_scan.minimum_gap_mm, 4)
            if conflict_scan is not None and conflict_scan.minimum_gap_mm is not None
            else None
        ),
        # Deprecated alias retained for schema-version-2 manifest readers. It
        # may be zero when independent source paths overlap exactly.
        "minimum_nonzero_close_gap_mm": (
            round(conflict_scan.minimum_gap_mm, 4)
            if conflict_scan is not None and conflict_scan.minimum_gap_mm is not None
            else None
        ),
        "layers": layers,
    }

    warnings: list[str] = []
    if conflict_scan is not None and conflict_scan.conflict_count:
        warnings.append(
            f"Detected {conflict_scan.conflict_count} pairs of distinct nearby paths whose "
            "physical pen marks may merge; geometry was retained."
        )
    if conflict_scan is not None and conflict_scan.truncated:
        warnings.append(
            "The physical conflict scan reached its explicit "
            f"{maximum_candidate_pairs:,}-candidate safety limit; reported conflict "
            "counts are lower bounds and geometry was not changed."
        )
    widest_ground = max(
        (float(layer["plotted_ground_width_m"]) for layer in layers), default=0.0
    )
    if widest_ground >= 25:
        warnings.append(
            "At this scale the widest plotted mark represents "
            f"approximately {widest_ground:.1f} m on the ground; consider a "
            "larger sheet, tighter crop, centreline roads, or a finer nib."
        )
    if report["requested_simplify_ground_m"] >= 10:
        warnings.append(
            "The requested simplification tolerance represents at least 10 m "
            "on the ground at this scale."
        )
    return report, warnings

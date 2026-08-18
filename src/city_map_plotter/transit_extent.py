"""Deterministic geographic extents for named UK operator plates.

The physical map field and the geographic extent are separate contracts.  A
wide regional operator must not force a shallow physical frame, and the UK
overview must not lose detached northern islands merely because no selected
service route reaches them.  This module supplies the shared renderer and
Zoomstack-acquisition rule for those extents.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .models import BoundingBox, MapPlotterError


TRANSIT_PROJECTION_EXTENT_POLICY_VERSION = "transit-projection-extent-v1"
NAMED_OPERATOR_KINDS = frozenset(
    {"national-operator", "national-operator-overview"}
)
COMPLETE_GB_BOUNDS_WGS84 = (-8.75, 49.75, 2.0, 61.0)
DEFAULT_OPERATOR_CONTEXT_PADDING_FRACTION = 0.065


@dataclass(frozen=True, slots=True)
class TransitProjectionExtent:
    """One source extent expanded to the target paper viewport aspect."""

    base_bounds: BoundingBox
    expanded_bounds: BoundingBox
    target_metric_aspect: float
    base_metric_aspect: float
    expanded_metric_aspect: float
    expanded_axis: str
    source_basis: str
    route_padding_fraction: float
    complete_gb_context_required: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_version": TRANSIT_PROJECTION_EXTENT_POLICY_VERSION,
            "source_basis": self.source_basis,
            "base_bounds_wgs84": self.base_bounds.as_dict(),
            "expanded_bounds_wgs84": self.expanded_bounds.as_dict(),
            "target_metric_aspect": round(self.target_metric_aspect, 9),
            "base_metric_aspect": round(self.base_metric_aspect, 9),
            "expanded_metric_aspect": round(self.expanded_metric_aspect, 9),
            "expanded_axis": self.expanded_axis,
            "route_padding_fraction": self.route_padding_fraction,
            "route_geometry_included": True,
            "complete_gb_context_required": self.complete_gb_context_required,
            "complete_gb_bounds_wgs84": (
                list(COMPLETE_GB_BOUNDS_WGS84)
                if self.complete_gb_context_required
                else None
            ),
            "northern_ireland_geometry_included": False,
            "uniform_geographic_scale": True,
            "route_coordinate_crop_applied": False,
        }


def _padded_bounds(bounds: BoundingBox, fraction: float) -> BoundingBox:
    if not 0.0 <= fraction <= 0.25:
        raise MapPlotterError("Operator context padding must be in [0, 0.25].")
    if fraction == 0.0:
        return bounds
    lon_pad = max((bounds.east - bounds.west) * fraction, 0.002)
    lat_pad = max((bounds.north - bounds.south) * fraction, 0.002)
    return BoundingBox(
        max(-180.0, bounds.west - lon_pad),
        max(-85.0, bounds.south - lat_pad),
        min(180.0, bounds.east + lon_pad),
        min(85.0, bounds.north + lat_pad),
    )


def _union_bounds(first: BoundingBox, second: BoundingBox) -> BoundingBox:
    return BoundingBox(
        min(first.west, second.west),
        min(first.south, second.south),
        max(first.east, second.east),
        max(first.north, second.north),
    )


def _bounded_interval(
    centre: float,
    span: float,
    *,
    minimum: float,
    maximum: float,
) -> tuple[float, float]:
    if span <= 0.0 or span > maximum - minimum + 1e-12:
        raise MapPlotterError("Requested transit projection extent exceeds WGS84 limits.")
    lower = centre - span / 2.0
    upper = centre + span / 2.0
    if lower < minimum:
        upper += minimum - lower
        lower = minimum
    if upper > maximum:
        lower -= upper - maximum
        upper = maximum
    return lower, upper


def _expand_to_metric_aspect(
    bounds: BoundingBox, target_metric_aspect: float
) -> tuple[BoundingBox, str]:
    if (
        not isfinite(target_metric_aspect)
        or target_metric_aspect <= 0.0
    ):
        raise MapPlotterError("Transit target metric aspect must be positive.")
    current = bounds.approximate_width_m / bounds.approximate_height_m
    if abs(current - target_metric_aspect) <= 1e-12:
        return bounds, "none"
    latitude, longitude = bounds.center
    if current > target_metric_aspect:
        latitude_span = (bounds.north - bounds.south) * (
            current / target_metric_aspect
        )
        south, north = _bounded_interval(
            latitude,
            latitude_span,
            minimum=-85.0,
            maximum=85.0,
        )
        expanded = BoundingBox(bounds.west, south, bounds.east, north)
        axis = "latitude"
    else:
        longitude_span = (bounds.east - bounds.west) * (
            target_metric_aspect / current
        )
        west, east = _bounded_interval(
            longitude,
            longitude_span,
            minimum=-180.0,
            maximum=180.0,
        )
        expanded = BoundingBox(west, bounds.south, east, bounds.north)
        axis = "longitude"
    expanded_aspect = (
        expanded.approximate_width_m / expanded.approximate_height_m
    )
    if abs(expanded_aspect - target_metric_aspect) > 1e-8:
        raise MapPlotterError(
            "Transit projection extent did not reach the requested metric aspect."
        )
    return expanded, axis


def named_operator_projection_extent(
    *,
    network_kind: str,
    route_bounds: BoundingBox,
    target_metric_aspect: float,
    padding_fraction: float = DEFAULT_OPERATOR_CONTEXT_PADDING_FRACTION,
) -> TransitProjectionExtent:
    """Return the shared projector/acquisition extent for a named operator."""

    if network_kind not in NAMED_OPERATOR_KINDS:
        raise MapPlotterError(
            "Named-operator projection extent requires a named operator network."
        )
    padded_route = _padded_bounds(route_bounds, padding_fraction)
    complete_gb = network_kind == "national-operator-overview"
    if complete_gb:
        base = _union_bounds(
            padded_route,
            BoundingBox(*COMPLETE_GB_BOUNDS_WGS84),
        )
        source_basis = (
            "complete-gb-bounds-plus-padded-route-geometry"
            if padding_fraction > 0.0
            else "complete-gb-bounds-plus-route-geometry"
        )
    else:
        base = padded_route
        source_basis = (
            "padded-route-geometry"
            if padding_fraction > 0.0
            else "route-geometry"
        )
    expanded, axis = _expand_to_metric_aspect(base, target_metric_aspect)
    return TransitProjectionExtent(
        base_bounds=base,
        expanded_bounds=expanded,
        target_metric_aspect=target_metric_aspect,
        base_metric_aspect=(
            base.approximate_width_m / base.approximate_height_m
        ),
        expanded_metric_aspect=(
            expanded.approximate_width_m / expanded.approximate_height_m
        ),
        expanded_axis=axis,
        source_basis=source_basis,
        route_padding_fraction=padding_fraction,
        complete_gb_context_required=complete_gb,
    )


__all__ = [
    "COMPLETE_GB_BOUNDS_WGS84",
    "DEFAULT_OPERATOR_CONTEXT_PADDING_FRACTION",
    "NAMED_OPERATOR_KINDS",
    "TRANSIT_PROJECTION_EXTENT_POLICY_VERSION",
    "TransitProjectionExtent",
    "named_operator_projection_extent",
]

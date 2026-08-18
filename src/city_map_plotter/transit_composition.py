"""Aspect-aware map-field composition for geographic transit plates.

Ordinary local diagrams may use a snug route-aspect frame.  Named national
operator plates instead retain the complete format-owned map field: their
route is fitted with one uniform scale and the acquired geographic context is
expanded to the field's metric aspect.  This avoids both the shallow c2c-style
strip and a stretched or cropped route.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import MapPlotterError
from .niche_common import Rect
from .transit import TransitNetwork
from .transit_topology import DEFAULT_PROJECTOR_MARGIN_FRACTION, projector_for


ASPECT_COMPOSITION_POLICY_VERSION = "transit-aspect-field-v2"
DEFAULT_FURNITURE_MARGIN_FRACTION = DEFAULT_PROJECTOR_MARGIN_FRACTION
MINIMUM_FURNITURE_MARGIN_MM = 6.0
FULL_FIELD_NETWORK_KINDS = frozenset(
    {"national-operator", "national-operator-overview"}
)


@dataclass(frozen=True, slots=True)
class AspectAwareMapField:
    """One effective field and its undistorted geographic viewport."""

    available_field: Rect
    effective_field: Rect
    geographic_viewport: Rect
    source_width_m: float
    source_height_m: float
    furniture_margin_mm: float
    projector_margin_fraction: float
    limiting_axis: str
    field_strategy: str
    context_aspect_expansion_required: bool
    projection_extent_policy: dict[str, object] | None

    @property
    def source_aspect(self) -> float:
        return self.source_width_m / self.source_height_m

    @property
    def viewport_aspect(self) -> float:
        return self.geographic_viewport.width / self.geographic_viewport.height

    @property
    def effective_field_aspect(self) -> float:
        return self.effective_field.width / self.effective_field.height

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_version": ASPECT_COMPOSITION_POLICY_VERSION,
            "available_field_mm": self.available_field.as_dict(),
            "effective_field_mm": self.effective_field.as_dict(),
            "geographic_viewport_mm": self.geographic_viewport.as_dict(),
            "source_width_m": round(self.source_width_m, 3),
            "source_height_m": round(self.source_height_m, 3),
            "source_aspect": round(self.source_aspect, 9),
            "viewport_aspect": round(self.viewport_aspect, 9),
            "effective_field_aspect": round(self.effective_field_aspect, 9),
            "furniture_margin_mm": round(self.furniture_margin_mm, 6),
            "projector_margin_fraction": round(
                self.projector_margin_fraction, 9
            ),
            "limiting_axis": self.limiting_axis,
            "field_strategy": self.field_strategy,
            "context_aspect_expansion_required": (
                self.context_aspect_expansion_required
            ),
            "projection_extent_aspect_matches_viewport": (
                abs(self.source_aspect - self.viewport_aspect) <= 1e-9
            ),
            "projection_extent_policy": self.projection_extent_policy,
            "centred_in_available_field": True,
            "uniform_geographic_scale": True,
            "coordinate_distortion_applied": False,
            "route_coordinate_crop_applied": False,
        }


def aspect_aware_map_field(
    network: TransitNetwork,
    available_field: Rect,
    *,
    minimum_furniture_margin_mm: float = MINIMUM_FURNITURE_MARGIN_MM,
    furniture_margin_fraction: float = DEFAULT_FURNITURE_MARGIN_FRACTION,
) -> AspectAwareMapField:
    """Return the deterministic physical field and geographic viewport.

    Named national operator plates keep the complete format field and inset it
    by the furniture margin.  The route is uniformly fitted and centred in
    that viewport; context acquisition is responsible for expanding its clip
    bounds to the viewport's metric aspect.  Other network kinds preserve the
    original snug route-aspect frame.  Neither path crops or independently
    scales route axes.
    """

    if minimum_furniture_margin_mm <= 0.0:
        raise MapPlotterError("Transit furniture margin must be positive.")
    if not 0.0 <= furniture_margin_fraction < 0.25:
        raise MapPlotterError(
            "Transit furniture margin fraction must be in [0, 0.25)."
        )

    probe = projector_for(network, available_field, margin_fraction=0.0)
    margin_mm = max(
        minimum_furniture_margin_mm,
        min(available_field.width, available_field.height)
        * furniture_margin_fraction,
    )
    available_width = available_field.width - 2.0 * margin_mm
    available_height = available_field.height - 2.0 * margin_mm
    if available_width <= 0.0 or available_height <= 0.0:
        raise MapPlotterError(
            "The selected format cannot retain the minimum transit furniture margin."
        )

    horizontal_scale = available_width / probe.source_width_m
    vertical_scale = available_height / probe.source_height_m
    scale_mm_per_m = min(horizontal_scale, vertical_scale)
    if scale_mm_per_m <= 0.0:
        raise MapPlotterError("Transit aspect composition produced no usable scale.")

    route_viewport_width = probe.source_width_m * scale_mm_per_m
    route_viewport_height = probe.source_height_m * scale_mm_per_m
    if network.kind in FULL_FIELD_NETWORK_KINDS:
        effective = available_field
        viewport = effective.inset(margin_mm)
        effective_width = effective.width
        effective_height = effective.height
        field_strategy = "full-format-field-context-expanded-to-viewport-aspect"
    else:
        effective_width = route_viewport_width + 2.0 * margin_mm
        effective_height = route_viewport_height + 2.0 * margin_mm
        effective = Rect(
            available_field.x + (available_field.width - effective_width) / 2.0,
            available_field.y + (available_field.height - effective_height) / 2.0,
            effective_width,
            effective_height,
        )
        viewport = effective.inset(margin_mm)
        field_strategy = "snug-route-aspect-field"
    projector_margin_fraction = margin_mm / min(
        effective.width, effective.height
    )
    if projector_margin_fraction >= 0.25:
        raise MapPlotterError(
            "The transit route aspect is too extreme for this format and its "
            "minimum furniture margin; select an alternate composition."
        )

    final_projector = projector_for(
        network,
        effective,
        margin_fraction=projector_margin_fraction,
    )
    source_width_m = final_projector.source_width_m
    source_height_m = final_projector.source_height_m
    aspect_error = abs(
        viewport.width / viewport.height - source_width_m / source_height_m
    )
    if network.kind not in FULL_FIELD_NETWORK_KINDS and aspect_error > 1e-9:
        raise MapPlotterError(
            "Transit aspect composition changed the projected geographic aspect."
        )
    epsilon = 1e-7
    if network.kind in FULL_FIELD_NETWORK_KINDS:
        limiting_axis = "both"
    elif abs(horizontal_scale - vertical_scale) <= epsilon:
        limiting_axis = "both"
    elif horizontal_scale < vertical_scale:
        limiting_axis = "width"
    else:
        limiting_axis = "height"

    return AspectAwareMapField(
        available_field=available_field,
        effective_field=effective,
        geographic_viewport=viewport,
        source_width_m=source_width_m,
        source_height_m=source_height_m,
        furniture_margin_mm=margin_mm,
        projector_margin_fraction=projector_margin_fraction,
        limiting_axis=limiting_axis,
        field_strategy=field_strategy,
        context_aspect_expansion_required=(
            final_projector.extent_policy is not None
            and final_projector.extent_policy.expanded_axis != "none"
        ),
        projection_extent_policy=(
            final_projector.extent_policy.as_dict()
            if final_projector.extent_policy is not None
            else None
        ),
    )


__all__ = [
    "ASPECT_COMPOSITION_POLICY_VERSION",
    "AspectAwareMapField",
    "DEFAULT_FURNITURE_MARGIN_FRACTION",
    "FULL_FIELD_NETWORK_KINDS",
    "MINIMUM_FURNITURE_MARGIN_MM",
    "aspect_aware_map_field",
]

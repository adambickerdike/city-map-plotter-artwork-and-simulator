from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, isfinite, pi, radians
from typing import Any


EARTH_RADIUS_M = 6_371_008.8


class MapPlotterError(RuntimeError):
    """A user-facing error raised by the map compiler."""


@dataclass(frozen=True)
class BoundingBox:
    """A WGS84 bounding box in west, south, east, north order."""

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        if not all(
            isfinite(value) for value in (self.west, self.south, self.east, self.north)
        ):
            raise MapPlotterError("Bounding-box coordinates must be finite numbers.")
        if not (-180 <= self.west <= 180 and -180 <= self.east <= 180):
            raise MapPlotterError("Longitude must be between -180 and 180 degrees.")
        if not (-85 <= self.south <= 85 and -85 <= self.north <= 85):
            raise MapPlotterError("Latitude must be between -85 and 85 degrees.")
        if self.west >= self.east:
            raise MapPlotterError(
                "The west coordinate must be smaller than east; antimeridian areas "
                "are not supported yet."
            )
        if self.south >= self.north:
            raise MapPlotterError("The south coordinate must be smaller than north.")

    @property
    def center(self) -> tuple[float, float]:
        return ((self.south + self.north) / 2, (self.west + self.east) / 2)

    @property
    def approximate_width_m(self) -> float:
        latitude, _ = self.center
        return EARTH_RADIUS_M * radians(self.east - self.west) * cos(radians(latitude))

    @property
    def approximate_height_m(self) -> float:
        return EARTH_RADIUS_M * radians(self.north - self.south)

    @property
    def approximate_area_km2(self) -> float:
        return self.approximate_width_m * self.approximate_height_m / 1_000_000

    @classmethod
    def around(
        cls, latitude: float, longitude: float, radius_km: float
    ) -> "BoundingBox":
        if not all(isfinite(value) for value in (latitude, longitude, radius_km)):
            raise MapPlotterError("Center and radius must be finite numbers.")
        if radius_km <= 0:
            raise MapPlotterError("Radius must be greater than zero.")
        if not (-85 <= latitude <= 85 and -180 <= longitude <= 180):
            raise MapPlotterError(
                "Center latitude/longitude is outside the supported range."
            )
        angular = radius_km * 1_000 / EARTH_RADIUS_M
        latitude_delta = angular * 180 / pi
        longitude_delta = latitude_delta / max(cos(radians(latitude)), 1e-9)
        return cls(
            west=longitude - longitude_delta,
            south=latitude - latitude_delta,
            east=longitude + longitude_delta,
            north=latitude + latitude_delta,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "west": self.west,
            "south": self.south,
            "east": self.east,
            "north": self.north,
        }


@dataclass(frozen=True)
class LayerStyle:
    id: str
    label: str
    pen: str
    stroke: str
    stroke_width_mm: float
    order: int
    enabled: bool = True
    # These fields are appended so existing positional constructors remain valid.
    # ``stroke_width_mm`` is retained as a backwards-compatible alias for
    # ``nib_mm``; new code should use the machine-readable physical fields below.
    ink: str | None = None
    nib_mm: float | None = None
    strokes: int = 1
    passes: int = 1

    def __post_init__(self) -> None:
        resolved_ink = self.ink.strip() if isinstance(self.ink, str) else ""
        if not resolved_ink:
            resolved_ink = self._ink_from_pen_label(self.pen)
        if not resolved_ink:
            raise MapPlotterError("A layer style must identify its physical ink.")

        resolved_nib = self.stroke_width_mm if self.nib_mm is None else self.nib_mm
        if (
            isinstance(resolved_nib, bool)
            or not isinstance(resolved_nib, (int, float))
            or not isfinite(resolved_nib)
            or resolved_nib <= 0
        ):
            raise MapPlotterError("A layer style nib width must be greater than zero.")
        if (
            isinstance(self.strokes, bool)
            or not isinstance(self.strokes, int)
            or not 1 <= self.strokes <= 6
        ):
            raise MapPlotterError("A layer style must use between 1 and 6 strokes.")
        if (
            isinstance(self.passes, bool)
            or not isinstance(self.passes, int)
            or not 1 <= self.passes <= 4
        ):
            raise MapPlotterError("A layer style must use between 1 and 4 passes.")

        # Frozen dataclasses may still normalise aliases during construction.
        object.__setattr__(self, "ink", resolved_ink)
        object.__setattr__(self, "nib_mm", float(resolved_nib))
        object.__setattr__(self, "stroke_width_mm", float(resolved_nib))

    @staticmethod
    def _ink_from_pen_label(pen: str) -> str:
        """Infer legacy ink identity from labels such as ``Black 0.25``."""

        label = pen.strip() if isinstance(pen, str) else ""
        prefix, separator, suffix = label.rpartition(" ")
        if separator:
            try:
                float(suffix)
            except ValueError:
                pass
            else:
                return prefix.strip()
        return label

    @staticmethod
    def _nib_from_pen_label(pen: str) -> float | None:
        """Read the optional numeric nib suffix used by legacy pen labels."""

        label = pen.strip() if isinstance(pen, str) else ""
        _, separator, suffix = label.rpartition(" ")
        if not separator:
            return None
        try:
            nib_mm = float(suffix)
        except ValueError:
            return None
        return nib_mm if isfinite(nib_mm) and nib_mm > 0 else None

    @property
    def plotted_width_mm(self) -> float:
        """Physical mark width achieved by overlapping parallel strokes."""

        assert self.nib_mm is not None
        return self.nib_mm + (self.strokes - 1) * 0.85 * self.nib_mm

    @property
    def physical_pen_identity(self) -> tuple[str, float]:
        """Stable key for deciding whether an operator must change pens."""

        assert self.ink is not None and self.nib_mm is not None
        return (self.ink.casefold(), round(self.nib_mm, 6))

    @property
    def physical_pen_label(self) -> str:
        """Human label derived from the same fields that drive plot geometry."""

        assert self.ink is not None and self.nib_mm is not None
        return f"{self.ink} {self.nib_mm:g}"


@dataclass
class MapFeature:
    """One canonical source geometry before cartographic generalisation.

    The optional OSM provenance fields are populated by the local PBF reader.
    They deliberately live outside ``tags`` so source metadata cannot be
    mistaken for user-editable OpenStreetMap tags.  Overpass JSON does not
    expose all of this information, so the defaults preserve the existing
    offline/HTTP behaviour.
    """

    layer: str
    points: list[tuple[float, float]]  # (latitude, longitude)
    osm_type: str
    osm_id: str
    part: str = "0"
    tags: dict[str, str] = field(default_factory=dict)
    geometry_type: str = "line"
    ring_role: str | None = None
    outer_ring_part: str | None = None
    node_refs: tuple[str, ...] = ()
    relation_members: tuple[tuple[str, str, str], ...] = ()
    osm_version: int | None = None
    osm_timestamp: str | None = None
    osm_changeset: int | None = None
    osm_uid: int | None = None
    osm_user: str | None = None

    @property
    def name(self) -> str | None:
        return self.tags.get("name") or self.tags.get("ref")


@dataclass
class PlotStroke:
    """One physically drawable, page-space stroke compiled from map geometry."""

    layer: str
    points: list[tuple[float, float]]  # page millimetres
    osm_type: str = "compiled"
    osm_id: str = "multiple"
    part: str = "0"
    tags: dict[str, str] = field(default_factory=dict)
    name: str | None = None
    smooth: bool = False


@dataclass(frozen=True)
class Page:
    width_mm: float
    height_mm: float
    name: str
    orientation: str


@dataclass
class AcquisitionResult:
    data: dict[str, Any]
    endpoint: str
    query: str | None
    cache_path: str | None
    from_cache: bool
    # A local PBF reader may return canonical features directly because a PBF
    # is a binary entity stream, not an Overpass-shaped JSON document.
    features: list[MapFeature] | None = None
    # Deterministic source and extraction metadata for the output manifest.
    source_metadata: dict[str, Any] = field(default_factory=dict)

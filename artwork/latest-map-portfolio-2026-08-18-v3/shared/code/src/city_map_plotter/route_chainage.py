"""Distance-aware sampling for hiking-route maps and elevation profiles.

The hiking renderer needs the same factual station to appear twice: once as a
marker on the geographic route and once at the corresponding distance on the
elevation profile.  This module provides that shared measurement layer.  It
deliberately knows nothing about SVG layout, paper size, or pen styling.

Input coordinates are WGS84 ``(longitude, latitude[, elevation_m])`` values,
matching the hiking catalog.  Horizontal distances and intermediate positions
follow great-circle segments on the same mean-earth sphere used elsewhere in
the project.  Elevation is interpolated only when both endpoints of the
containing source segment have a finite height; missing evidence is reported
as missing rather than silently bridged or extrapolated.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from enum import StrEnum
from math import asin, atan2, cos, degrees, isfinite, radians, sin, sqrt
from typing import Any, Iterable, Sequence

from .course import EARTH_RADIUS_M

_DISTANCE_EPSILON_M = 1e-6


class ElevationStatus(StrEnum):
    """Provenance of a height attached to a chainage sample."""

    OBSERVED = "observed"
    INTERPOLATED = "interpolated"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RoutePoint:
    """One validated WGS84 route vertex in catalog coordinate order."""

    longitude: float
    latitude: float
    elevation_m: float | None = None

    @property
    def lon_lat(self) -> tuple[float, float]:
        return (self.longitude, self.latitude)


RoutePointLike = RoutePoint | Sequence[float | int | None]


@dataclass(frozen=True, slots=True)
class ProfileSample:
    """A source route vertex located on the distance axis of a profile."""

    vertex_index: int
    distance_m: float
    route_fraction: float
    longitude: float
    latitude: float
    elevation_m: float | None
    elevation_status: ElevationStatus

    @property
    def distance_km(self) -> float:
        return self.distance_m / 1_000.0

    @property
    def profile_point(self) -> tuple[float, float] | None:
        """Return factual ``(distance_m, elevation_m)`` profile coordinates."""

        if self.elevation_m is None:
            return None
        return (self.distance_m, self.elevation_m)


@dataclass(frozen=True, slots=True)
class ChainageStation:
    """One map/profile station interpolated at an exact route distance."""

    station_id: str
    distance_m: float
    route_fraction: float
    longitude: float
    latitude: float
    elevation_m: float | None
    elevation_status: ElevationStatus
    source_vertex_before: int
    source_vertex_after: int
    source_segment_fraction: float

    @property
    def distance_km(self) -> float:
        return self.distance_m / 1_000.0

    @property
    def map_point(self) -> tuple[float, float]:
        return (self.longitude, self.latitude)

    @property
    def profile_point(self) -> tuple[float, float] | None:
        """Return the matching unscaled profile point, if height is available."""

        if self.elevation_m is None:
            return None
        return (self.distance_m, self.elevation_m)

    def as_metadata(self) -> dict[str, str | int | float | None]:
        """Return JSON-safe evidence shared by the map and profile markers."""

        return {
            "station_id": self.station_id,
            "distance_m": self.distance_m,
            "distance_km": self.distance_km,
            "route_fraction": self.route_fraction,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "elevation_m": self.elevation_m,
            "elevation_status": self.elevation_status.value,
            "source_vertex_before": self.source_vertex_before,
            "source_vertex_after": self.source_vertex_after,
            "source_segment_fraction": self.source_segment_fraction,
        }

    def as_svg_attributes(self) -> dict[str, str]:
        """Return identical ``data-*`` attributes for both rendered instances.

        A renderer can add its own role (for example ``map-station`` or
        ``profile-station``), while these shared values make the two instances
        mechanically linkable in SVG QA.
        """

        attributes = {
            "data-chainage-id": self.station_id,
            "data-chainage-m": _format_number(self.distance_m, 3),
            "data-distance-km": _format_number(self.distance_km, 6),
            "data-route-fraction": _format_number(self.route_fraction, 9),
            "data-longitude": _format_number(self.longitude, 9),
            "data-latitude": _format_number(self.latitude, 9),
            "data-elevation-status": self.elevation_status.value,
            "data-source-vertex-before": str(self.source_vertex_before),
            "data-source-vertex-after": str(self.source_vertex_after),
            "data-source-segment-fraction": _format_number(
                self.source_segment_fraction, 9
            ),
        }
        if self.elevation_m is not None:
            attributes["data-elevation-m"] = _format_number(self.elevation_m, 3)
        return attributes


@dataclass(frozen=True, slots=True)
class RouteChainage:
    """Validated route vertices with a cumulative geodesic distance axis."""

    points: tuple[RoutePoint, ...]
    cumulative_distance_m: tuple[float, ...]
    total_distance_m: float
    segment_start_indices: tuple[int, ...] = (0,)

    @classmethod
    def from_points(cls, points: Iterable[RoutePointLike]) -> RouteChainage:
        """Validate points and build their cumulative great-circle distances.

        A single point and a route made entirely of repeated points are valid
        but marked as degenerate through :attr:`is_degenerate`.  An empty route
        cannot identify even one map position and is rejected explicitly.
        """

        materialised = tuple(points)
        if not materialised:
            raise ValueError("route chainage requires at least one point")
        return cls.from_segments((materialised,))

    @classmethod
    def from_segments(
        cls, segments: Iterable[Iterable[RoutePointLike]]
    ) -> RouteChainage:
        """Build one distance axis without inventing links across source breaks.

        Distance accumulates inside each supplied segment.  The first point of
        a later segment shares the previous segment's terminal chainage, so a
        geographic source gap is represented as a profile break rather than a
        fictitious straight-line climb or distance.
        """

        normalised_segments: list[tuple[RoutePoint, ...]] = []
        point_index = 0
        for segment_index, segment in enumerate(segments):
            checked = tuple(
                _normalise_route_point(point, point_index + local_index)
                for local_index, point in enumerate(segment)
            )
            if not checked:
                raise ValueError(f"route segment {segment_index} is empty")
            normalised_segments.append(checked)
            point_index += len(checked)
        if not normalised_segments:
            raise ValueError("route chainage requires at least one point")

        normalised: list[RoutePoint] = []
        cumulative: list[float] = []
        starts: list[int] = []
        total = 0.0
        for segment in normalised_segments:
            starts.append(len(normalised))
            for local_index, point in enumerate(segment):
                if local_index:
                    total += geodesic_distance_m(
                        segment[local_index - 1].lon_lat, point.lon_lat
                    )
                normalised.append(point)
                cumulative.append(total)
        return cls(
            points=tuple(normalised),
            cumulative_distance_m=tuple(cumulative),
            total_distance_m=total,
            segment_start_indices=tuple(starts),
        )

    @property
    def is_degenerate(self) -> bool:
        return self.total_distance_m <= _DISTANCE_EPSILON_M

    @property
    def has_any_elevation(self) -> bool:
        return any(point.elevation_m is not None for point in self.points)

    @property
    def has_complete_elevation(self) -> bool:
        return all(point.elevation_m is not None for point in self.points)

    @property
    def elevation_extent_m(self) -> tuple[float, float] | None:
        values = [
            point.elevation_m
            for point in self.points
            if point.elevation_m is not None
        ]
        if not values:
            return None
        return (min(values), max(values))

    def profile_samples(self) -> tuple[ProfileSample, ...]:
        """Locate every source vertex on a non-uniform geodesic distance axis."""

        samples = []
        for index, (point, distance_m) in enumerate(
            zip(self.points, self.cumulative_distance_m)
        ):
            samples.append(
                ProfileSample(
                    vertex_index=index,
                    distance_m=distance_m,
                    route_fraction=self._route_fraction(distance_m),
                    longitude=point.longitude,
                    latitude=point.latitude,
                    elevation_m=point.elevation_m,
                    elevation_status=(
                        ElevationStatus.OBSERVED
                        if point.elevation_m is not None
                        else ElevationStatus.UNAVAILABLE
                    ),
                )
            )
        return tuple(samples)

    def profile_runs(self) -> tuple[tuple[ProfileSample, ...], ...]:
        """Split source profile samples wherever elevation evidence is absent.

        Single-point runs are retained.  They are useful as station evidence,
        although a line renderer will normally draw only runs with two or more
        samples.
        """

        result: list[tuple[ProfileSample, ...]] = []
        current: list[ProfileSample] = []
        segment_starts = set(self.segment_start_indices[1:])
        for sample in self.profile_samples():
            if sample.vertex_index in segment_starts and current:
                result.append(tuple(current))
                current = []
            if sample.elevation_m is None:
                if current:
                    result.append(tuple(current))
                    current = []
                continue
            current.append(sample)
        if current:
            result.append(tuple(current))
        return tuple(result)

    def station_at_distance(
        self,
        distance_m: float,
        *,
        station_id: str | None = None,
        clamp: bool = False,
    ) -> ChainageStation:
        """Interpolate a station at an exact distance from the route start.

        Values outside the route are rejected by default because silently
        moving a factual kilometre marker would be misleading.  ``clamp`` is
        available for UI cursors and similar non-factual presentation uses.
        """

        requested = _finite_float(distance_m, "distance_m")
        if clamp:
            requested = min(max(requested, 0.0), self.total_distance_m)
        elif (
            requested < -_DISTANCE_EPSILON_M
            or requested > self.total_distance_m + _DISTANCE_EPSILON_M
        ):
            raise ValueError(
                f"distance_m must be within 0..{self.total_distance_m:.6f}, "
                f"not {requested!r}"
            )
        distance = min(max(requested, 0.0), self.total_distance_m)
        resolved_id = _validate_station_id(
            station_id or _distance_station_id(distance)
        )

        if self.is_degenerate:
            return self._vertex_station(0, resolved_id, 0.0)
        if distance <= _DISTANCE_EPSILON_M:
            return self._vertex_station(0, resolved_id, 0.0)
        if self.total_distance_m - distance <= _DISTANCE_EPSILON_M:
            return self._vertex_station(
                len(self.points) - 1, resolved_id, self.total_distance_m
            )

        after = bisect_left(self.cumulative_distance_m, distance)
        if (
            after < len(self.cumulative_distance_m)
            and abs(self.cumulative_distance_m[after] - distance)
            <= _DISTANCE_EPSILON_M
        ):
            return self._vertex_station(after, resolved_id, distance)

        before = max(0, after - 1)
        segment_distance = (
            self.cumulative_distance_m[after]
            - self.cumulative_distance_m[before]
        )
        if segment_distance <= _DISTANCE_EPSILON_M:
            # Consecutive duplicate coordinates can produce a zero-length
            # segment at a bisect boundary.  Use the nearest exact source
            # vertex instead of dividing by zero or inventing a direction.
            return self._vertex_station(after, resolved_id, distance)

        segment_fraction = (
            distance - self.cumulative_distance_m[before]
        ) / segment_distance
        start = self.points[before]
        end = self.points[after]
        longitude, latitude = interpolate_geodesic(
            start.lon_lat, end.lon_lat, segment_fraction
        )
        if start.elevation_m is not None and end.elevation_m is not None:
            elevation_m = start.elevation_m + segment_fraction * (
                end.elevation_m - start.elevation_m
            )
            elevation_status = ElevationStatus.INTERPOLATED
        else:
            elevation_m = None
            elevation_status = ElevationStatus.UNAVAILABLE
        return ChainageStation(
            station_id=resolved_id,
            distance_m=distance,
            route_fraction=self._route_fraction(distance),
            longitude=longitude,
            latitude=latitude,
            elevation_m=elevation_m,
            elevation_status=elevation_status,
            source_vertex_before=before,
            source_vertex_after=after,
            source_segment_fraction=segment_fraction,
        )

    def station_at_fraction(
        self, fraction: float, *, station_id: str | None = None
    ) -> ChainageStation:
        """Interpolate a station at a fraction in the inclusive range 0..1."""

        route_fraction = _finite_float(fraction, "fraction")
        if not 0.0 <= route_fraction <= 1.0:
            raise ValueError(f"fraction must be within 0..1, not {route_fraction!r}")
        resolved_id = station_id or _fraction_station_id(route_fraction)
        return self.station_at_distance(
            self.total_distance_m * route_fraction,
            station_id=resolved_id,
        )

    def stations_at_distances(
        self,
        distances_m: Iterable[float],
        *,
        station_ids: Iterable[str] | None = None,
        clamp: bool = False,
    ) -> tuple[ChainageStation, ...]:
        """Create a stable, uniquely identified batch of distance stations."""

        distances = tuple(distances_m)
        identifiers = _batch_station_ids(
            station_ids,
            tuple(_distance_station_id(float(distance)) for distance in distances),
        )
        return tuple(
            self.station_at_distance(
                distance,
                station_id=identifier,
                clamp=clamp,
            )
            for distance, identifier in zip(distances, identifiers)
        )

    def stations_at_fractions(
        self,
        fractions: Iterable[float],
        *,
        station_ids: Iterable[str] | None = None,
    ) -> tuple[ChainageStation, ...]:
        """Create a stable, uniquely identified batch of fractional stations."""

        values = tuple(fractions)
        identifiers = _batch_station_ids(
            station_ids,
            tuple(_fraction_station_id(float(fraction)) for fraction in values),
        )
        return tuple(
            self.station_at_fraction(fraction, station_id=identifier)
            for fraction, identifier in zip(values, identifiers)
        )

    def _route_fraction(self, distance_m: float) -> float:
        if self.is_degenerate:
            return 0.0
        return distance_m / self.total_distance_m

    def _vertex_station(
        self, vertex_index: int, station_id: str, distance_m: float
    ) -> ChainageStation:
        point = self.points[vertex_index]
        return ChainageStation(
            station_id=station_id,
            distance_m=distance_m,
            route_fraction=self._route_fraction(distance_m),
            longitude=point.longitude,
            latitude=point.latitude,
            elevation_m=point.elevation_m,
            elevation_status=(
                ElevationStatus.OBSERVED
                if point.elevation_m is not None
                else ElevationStatus.UNAVAILABLE
            ),
            source_vertex_before=vertex_index,
            source_vertex_after=vertex_index,
            source_segment_fraction=0.0,
        )


def geodesic_distance_m(
    start: tuple[float, float], end: tuple[float, float]
) -> float:
    """Great-circle distance between WGS84 ``(longitude, latitude)`` points."""

    lon1, lat1 = _validated_lon_lat(start, "start")
    lon2, lat2 = _validated_lon_lat(end, "end")
    latitude_1 = radians(lat1)
    latitude_2 = radians(lat2)
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = radians(lon2 - lon1)
    value = sin(delta_latitude / 2.0) ** 2 + (
        cos(latitude_1)
        * cos(latitude_2)
        * sin(delta_longitude / 2.0) ** 2
    )
    central_angle = 2.0 * asin(min(1.0, sqrt(value)))
    return EARTH_RADIUS_M * central_angle


def interpolate_geodesic(
    start: tuple[float, float],
    end: tuple[float, float],
    fraction: float,
) -> tuple[float, float]:
    """Return a point at ``fraction`` along the segment's great-circle arc."""

    lon1, lat1 = _validated_lon_lat(start, "start")
    lon2, lat2 = _validated_lon_lat(end, "end")
    ratio = _finite_float(fraction, "fraction")
    if not 0.0 <= ratio <= 1.0:
        raise ValueError(f"fraction must be within 0..1, not {ratio!r}")
    if ratio == 0.0:
        return (lon1, lat1)
    if ratio == 1.0:
        return (lon2, lat2)

    latitude_1 = radians(lat1)
    latitude_2 = radians(lat2)
    longitude_1 = radians(lon1)
    longitude_2 = radians(lon2)
    delta_longitude = longitude_2 - longitude_1
    value = sin((latitude_2 - latitude_1) / 2.0) ** 2 + (
        cos(latitude_1)
        * cos(latitude_2)
        * sin(delta_longitude / 2.0) ** 2
    )
    central_angle = 2.0 * asin(min(1.0, sqrt(value)))
    if central_angle <= 1e-15:
        return (lon1, lat1)

    # Follow the initial great-circle bearing for the requested angular
    # distance.  Unlike planar longitude interpolation this takes the short
    # path across the antimeridian and remains geodesically proportional.
    bearing = atan2(
        sin(delta_longitude) * cos(latitude_2),
        cos(latitude_1) * sin(latitude_2)
        - sin(latitude_1) * cos(latitude_2) * cos(delta_longitude),
    )
    angular_distance = central_angle * ratio
    latitude = asin(
        sin(latitude_1) * cos(angular_distance)
        + cos(latitude_1) * sin(angular_distance) * cos(bearing)
    )
    longitude = longitude_1 + atan2(
        sin(bearing) * sin(angular_distance) * cos(latitude_1),
        cos(angular_distance) - sin(latitude_1) * sin(latitude),
    )
    longitude_degrees = ((degrees(longitude) + 180.0) % 360.0) - 180.0
    return (longitude_degrees, degrees(latitude))


def _normalise_route_point(point: RoutePointLike, index: int) -> RoutePoint:
    if isinstance(point, RoutePoint):
        longitude = _finite_float(point.longitude, f"point {index} longitude")
        latitude = _finite_float(point.latitude, f"point {index} latitude")
        elevation = _optional_elevation(point.elevation_m)
    else:
        if isinstance(point, (str, bytes)):
            raise ValueError(f"route point {index} must be a coordinate sequence")
        try:
            values = tuple(point)
        except TypeError as exc:
            raise ValueError(
                f"route point {index} must be a coordinate sequence"
            ) from exc
        if len(values) not in (2, 3):
            raise ValueError(
                f"route point {index} must contain longitude, latitude, and "
                "optional elevation"
            )
        longitude = _finite_float(values[0], f"point {index} longitude")
        latitude = _finite_float(values[1], f"point {index} latitude")
        elevation = _optional_elevation(values[2] if len(values) == 3 else None)
    _validate_coordinate_range(longitude, latitude, f"route point {index}")
    return RoutePoint(longitude, latitude, elevation)


def _validated_lon_lat(
    point: tuple[float, float], label: str
) -> tuple[float, float]:
    if len(point) != 2:
        raise ValueError(f"{label} must contain longitude and latitude")
    longitude = _finite_float(point[0], f"{label} longitude")
    latitude = _finite_float(point[1], f"{label} latitude")
    _validate_coordinate_range(longitude, latitude, label)
    return (longitude, latitude)


def _validate_coordinate_range(
    longitude: float, latitude: float, label: str
) -> None:
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"{label} longitude must be within -180..180")
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"{label} latitude must be within -90..90")


def _finite_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _optional_elevation(value: Any) -> float | None:
    if value is None:
        return None
    try:
        elevation = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("elevation must be a number or null") from exc
    # NaN/inf elevations occur in partially sampled DEM data.  Treating them
    # as absent lets the route and its valid profile sections remain usable.
    return elevation if isfinite(elevation) else None


def _validate_station_id(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("station_id must be a non-empty string without edge spaces")
    return value


def _distance_station_id(distance_m: float) -> str:
    return f"chainage-m{int(round(distance_m * 1_000.0)):012d}"


def _fraction_station_id(fraction: float) -> str:
    return f"chainage-f{int(round(fraction * 1_000_000.0)):07d}"


def _batch_station_ids(
    supplied: Iterable[str] | None, defaults: tuple[str, ...]
) -> tuple[str, ...]:
    identifiers = defaults if supplied is None else tuple(supplied)
    if len(identifiers) != len(defaults):
        raise ValueError("station_ids must contain one identifier per station")
    checked = tuple(_validate_station_id(identifier) for identifier in identifiers)
    if len(set(checked)) != len(checked):
        raise ValueError("station_ids must be unique within a station batch")
    return checked


def _format_number(value: float, decimal_places: int) -> str:
    rendered = f"{value:.{decimal_places}f}".rstrip("0").rstrip(".")
    return "0" if rendered in ("", "-0") else rendered

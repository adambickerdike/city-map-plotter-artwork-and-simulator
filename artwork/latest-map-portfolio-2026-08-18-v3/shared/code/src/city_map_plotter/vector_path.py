"""Canonical line-and-cubic path geometry for plotter artwork.

The bridge artwork pipeline needs a small interchange format that preserves
Bezier controls until the final SVG is emitted.  This module deliberately
models one SVG-style subpath only: a start point, an ordered sequence of line
or cubic segments, and an optional closing edge.  It has no paper, pen, style,
or Shapely dependency.

All public geometry is immutable and finite.  Adaptive operations require an
explicit tolerance so callers cannot unknowingly depend on a hidden drawing
resolution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import copysign, hypot, isfinite, sqrt
from typing import TypeAlias, cast


Point: TypeAlias = tuple[float, float]
"""A finite Cartesian point expressed as ``(x, y)``."""

_MAX_ADAPTIVE_DEPTH = 40
_SCHEMA_VERSION = 1
_DOCUMENT_KIND = "bridge-vector-path"


class VectorPathError(ValueError):
    """Raised when vector path geometry or its serialized form is invalid."""


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VectorPathError(f"{label} must be a finite real number.")
    number = float(value)
    if not isfinite(number):
        raise VectorPathError(f"{label} must be a finite real number.")
    # Canonicalize negative zero for stable SVG, JSON, equality, and hashing.
    return 0.0 if number == 0.0 else number


def _point(value: object, label: str) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise VectorPathError(f"{label} must contain exactly two coordinates.")
    return (
        _finite_number(value[0], f"{label}.x"),
        _finite_number(value[1], f"{label}.y"),
    )


def _json_point(value: object, label: str) -> Point:
    if not isinstance(value, list):
        raise VectorPathError(f"{label} must be a two-item JSON array.")
    return _point(value, label)


def _positive_tolerance(value: object) -> float:
    tolerance = _finite_number(value, "tolerance")
    if tolerance <= 0.0:
        raise VectorPathError("tolerance must be greater than zero.")
    return tolerance


def _distance(first: Point, second: Point) -> float:
    distance = hypot(second[0] - first[0], second[1] - first[1])
    if not isfinite(distance):
        raise VectorPathError("Geometry is too large to measure as finite floats.")
    return distance


@dataclass(frozen=True, slots=True)
class Affine2D:
    """An SVG-compatible affine transform.

    The six coefficients apply as ``x' = a*x + c*y + e`` and
    ``y' = b*x + d*y + f``.
    """

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def __post_init__(self) -> None:
        for name in ("a", "b", "c", "d", "e", "f"):
            object.__setattr__(
                self,
                name,
                _finite_number(getattr(self, name), f"Affine2D.{name}"),
            )

    def apply(self, point: Point) -> Point:
        """Return ``point`` transformed without flattening any curves."""

        x, y = _point(point, "point")
        return _point(
            (
                self.a * x + self.c * y + self.e,
                self.b * x + self.d * y + self.f,
            ),
            "transformed point",
        )


@dataclass(frozen=True, slots=True)
class LineSegment:
    """A straight segment from the preceding point to ``to``."""

    to: Point

    def __post_init__(self) -> None:
        object.__setattr__(self, "to", _point(self.to, "LineSegment.to"))


@dataclass(frozen=True, slots=True)
class CubicSegment:
    """A cubic Bezier segment from the preceding point to ``to``."""

    control_1: Point
    control_2: Point
    to: Point

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "control_1",
            _point(self.control_1, "CubicSegment.control_1"),
        )
        object.__setattr__(
            self,
            "control_2",
            _point(self.control_2, "CubicSegment.control_2"),
        )
        object.__setattr__(self, "to", _point(self.to, "CubicSegment.to"))


PathSegment: TypeAlias = LineSegment | CubicSegment


@dataclass(frozen=True, slots=True)
class PathBounds:
    """The exact axis-aligned bounds of line and cubic path geometry."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def __post_init__(self) -> None:
        for name in ("min_x", "min_y", "max_x", "max_y"):
            object.__setattr__(
                self,
                name,
                _finite_number(getattr(self, name), f"PathBounds.{name}"),
            )
        if self.min_x > self.max_x or self.min_y > self.max_y:
            raise VectorPathError("PathBounds minima must not exceed maxima.")

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y


@dataclass(frozen=True, slots=True)
class FlattenedPath:
    """A polyline approximation and the tolerance used to produce it.

    ``maximum_depth`` records the deepest deterministic De Casteljau split
    needed by any source cubic.  It is evidence about the approximation, not
    an input that changes the algorithm.
    """

    points: tuple[Point, ...]
    tolerance: float
    maximum_depth: int

    def __post_init__(self) -> None:
        if not isinstance(self.points, (list, tuple)):
            raise VectorPathError("FlattenedPath.points must be a point sequence.")
        points = tuple(
            _point(point, f"FlattenedPath.points[{index}]")
            for index, point in enumerate(self.points)
        )
        if len(points) < 2:
            raise VectorPathError("A flattened path must contain at least two points.")
        if any(first == second for first, second in zip(points, points[1:])):
            raise VectorPathError(
                "A flattened path must not contain zero-length segments."
            )
        object.__setattr__(self, "points", points)
        object.__setattr__(self, "tolerance", _positive_tolerance(self.tolerance))
        if isinstance(self.maximum_depth, bool) or not isinstance(
            self.maximum_depth, int
        ):
            raise VectorPathError("maximum_depth must be a non-negative integer.")
        if self.maximum_depth < 0:
            raise VectorPathError("maximum_depth must be a non-negative integer.")

    @property
    def length(self) -> float:
        """Return the exact length of the flattened polyline."""

        return sum(
            _distance(first, second)
            for first, second in zip(self.points, self.points[1:])
        )


@dataclass(frozen=True, slots=True)
class VectorPath:
    """One canonical subpath made exclusively from line and cubic segments."""

    start: Point
    segments: tuple[PathSegment, ...]
    closed: bool = False

    def __post_init__(self) -> None:
        start = _point(self.start, "VectorPath.start")
        if not isinstance(self.segments, (list, tuple)):
            raise VectorPathError("VectorPath.segments must be a segment sequence.")
        segments = tuple(self.segments)
        if not segments:
            raise VectorPathError("A vector path must contain at least one segment.")
        if type(self.closed) is not bool:
            raise VectorPathError("VectorPath.closed must be a boolean.")

        current = start
        for index, segment in enumerate(segments):
            if type(segment) is LineSegment:
                line = cast(LineSegment, segment)
                if line.to == current:
                    raise VectorPathError(
                        f"VectorPath.segments[{index}] is a zero-length line."
                    )
                current = line.to
            elif type(segment) is CubicSegment:
                cubic = cast(CubicSegment, segment)
                if (
                    cubic.control_1 == current
                    and cubic.control_2 == current
                    and cubic.to == current
                ):
                    raise VectorPathError(
                        f"VectorPath.segments[{index}] is a zero-length cubic."
                    )
                current = cubic.to
            else:
                raise VectorPathError(
                    f"VectorPath.segments[{index}] must be a LineSegment or "
                    "CubicSegment."
                )

        object.__setattr__(self, "start", start)
        object.__setattr__(self, "segments", segments)

    @property
    def end(self) -> Point:
        """Return the explicit endpoint before any implicit closing edge."""

        return self.segments[-1].to

    def transformed(self, matrix: Affine2D) -> VectorPath:
        """Apply one affine matrix to every endpoint and cubic control point."""

        if type(matrix) is not Affine2D:
            raise VectorPathError("matrix must be an Affine2D.")
        transformed_segments: list[PathSegment] = []
        for segment in self.segments:
            if type(segment) is LineSegment:
                line = cast(LineSegment, segment)
                transformed_segments.append(LineSegment(matrix.apply(line.to)))
            else:
                cubic = cast(CubicSegment, segment)
                transformed_segments.append(
                    CubicSegment(
                        control_1=matrix.apply(cubic.control_1),
                        control_2=matrix.apply(cubic.control_2),
                        to=matrix.apply(cubic.to),
                    )
                )
        return VectorPath(
            start=matrix.apply(self.start),
            segments=tuple(transformed_segments),
            closed=self.closed,
        )

    def bounds(self) -> PathBounds:
        """Return true curve bounds, including every interior cubic extremum."""

        points = [self.start]
        current = self.start
        for segment in self.segments:
            if type(segment) is LineSegment:
                line = cast(LineSegment, segment)
                points.append(line.to)
                current = line.to
                continue

            cubic = cast(CubicSegment, segment)
            geometry = (current, cubic.control_1, cubic.control_2, cubic.to)
            parameters = {0.0, 1.0}
            parameters.update(
                _cubic_extrema_parameters(
                    current[0], cubic.control_1[0], cubic.control_2[0], cubic.to[0]
                )
            )
            parameters.update(
                _cubic_extrema_parameters(
                    current[1], cubic.control_1[1], cubic.control_2[1], cubic.to[1]
                )
            )
            points.extend(_cubic_point(geometry, value) for value in sorted(parameters))
            current = cubic.to

        return PathBounds(
            min_x=min(point[0] for point in points),
            min_y=min(point[1] for point in points),
            max_x=max(point[0] for point in points),
            max_y=max(point[1] for point in points),
        )

    def length(self, tolerance: float) -> float:
        """Return path length within the explicitly supplied absolute tolerance.

        A cubic's chord and control polygon are lower and upper length bounds.
        Adaptive subdivision splits the caller's error allowance between child
        curves, so the sum of returned segment estimates remains within
        ``tolerance`` of the mathematical path length.  Lines are exact.
        """

        requested_tolerance = _positive_tolerance(tolerance)
        cubic_count = sum(type(segment) is CubicSegment for segment in self.segments)
        cubic_tolerance = requested_tolerance / cubic_count if cubic_count else 0.0

        total = 0.0
        current = self.start
        for segment in self.segments:
            if type(segment) is LineSegment:
                line = cast(LineSegment, segment)
                total += _distance(current, line.to)
                current = line.to
                continue

            cubic = cast(CubicSegment, segment)
            total += _adaptive_cubic_length(
                (current, cubic.control_1, cubic.control_2, cubic.to),
                cubic_tolerance,
                depth=0,
            )
            current = cubic.to

        if self.closed and current != self.start:
            total += _distance(current, self.start)
        if not isfinite(total):
            raise VectorPathError("Geometry is too large to measure as finite floats.")
        return total

    def flatten(self, tolerance: float) -> FlattenedPath:
        """Approximate cubics with lines no farther than ``tolerance`` away.

        De Casteljau subdivision is deterministic and visits left branches
        before right branches.  The convex-hull distance test bounds every
        source curve point against its corresponding emitted chord.
        """

        requested_tolerance = _positive_tolerance(tolerance)
        points = [self.start]
        maximum_depth = 0
        current = self.start
        for segment in self.segments:
            if type(segment) is LineSegment:
                line = cast(LineSegment, segment)
                points.append(line.to)
                current = line.to
                continue

            cubic = cast(CubicSegment, segment)
            maximum_depth = max(
                maximum_depth,
                _flatten_cubic(
                    (current, cubic.control_1, cubic.control_2, cubic.to),
                    requested_tolerance,
                    points,
                    depth=0,
                ),
            )
            current = cubic.to

        if self.closed and points[-1] != self.start:
            points.append(self.start)
        return FlattenedPath(
            points=tuple(points),
            tolerance=requested_tolerance,
            maximum_depth=maximum_depth,
        )

    def to_svg_path_data(self) -> str:
        """Serialize with absolute ``M``, ``L``, ``C``, and optional ``Z``."""

        chunks = [f"M {_format_point(self.start)}"]
        for segment in self.segments:
            if type(segment) is LineSegment:
                line = cast(LineSegment, segment)
                chunks.append(f"L {_format_point(line.to)}")
            else:
                cubic = cast(CubicSegment, segment)
                chunks.append(
                    "C "
                    f"{_format_point(cubic.control_1)} "
                    f"{_format_point(cubic.control_2)} "
                    f"{_format_point(cubic.to)}"
                )
        if self.closed:
            chunks.append("Z")
        return " ".join(chunks)

    def to_dict(self) -> dict[str, object]:
        """Return the canonical, versioned JSON-compatible document."""

        encoded_segments: list[dict[str, object]] = []
        for segment in self.segments:
            if type(segment) is LineSegment:
                line = cast(LineSegment, segment)
                encoded_segments.append(
                    {"kind": "line", "to": [line.to[0], line.to[1]]}
                )
            else:
                cubic = cast(CubicSegment, segment)
                encoded_segments.append(
                    {
                        "kind": "cubic",
                        "control_1": [cubic.control_1[0], cubic.control_1[1]],
                        "control_2": [cubic.control_2[0], cubic.control_2[1]],
                        "to": [cubic.to[0], cubic.to[1]],
                    }
                )
        return {
            "schema_version": _SCHEMA_VERSION,
            "kind": _DOCUMENT_KIND,
            "start": [self.start[0], self.start[1]],
            "segments": encoded_segments,
            "closed": self.closed,
        }

    @classmethod
    def from_dict(cls, document: object) -> VectorPath:
        """Load a strictly validated canonical dictionary."""

        payload = _strict_object(
            document,
            {"schema_version", "kind", "start", "segments", "closed"},
            "vector path document",
        )
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != _SCHEMA_VERSION
        ):
            raise VectorPathError(
                f"schema_version must be the integer {_SCHEMA_VERSION}."
            )
        if payload["kind"] != _DOCUMENT_KIND:
            raise VectorPathError(f"kind must be {_DOCUMENT_KIND!r}.")
        if type(payload["closed"]) is not bool:
            raise VectorPathError("closed must be a JSON boolean.")
        raw_segments = payload["segments"]
        if not isinstance(raw_segments, list):
            raise VectorPathError("segments must be a JSON array.")

        segments: list[PathSegment] = []
        for index, raw_segment in enumerate(raw_segments):
            label = f"segments[{index}]"
            if type(raw_segment) is not dict:
                raise VectorPathError(f"{label} must be a JSON object.")
            segment_payload = cast(dict[object, object], raw_segment)
            kind = segment_payload.get("kind")
            if kind == "line":
                line = _strict_object(raw_segment, {"kind", "to"}, label)
                segments.append(LineSegment(_json_point(line["to"], f"{label}.to")))
            elif kind == "cubic":
                cubic = _strict_object(
                    raw_segment,
                    {"kind", "control_1", "control_2", "to"},
                    label,
                )
                segments.append(
                    CubicSegment(
                        control_1=_json_point(cubic["control_1"], f"{label}.control_1"),
                        control_2=_json_point(cubic["control_2"], f"{label}.control_2"),
                        to=_json_point(cubic["to"], f"{label}.to"),
                    )
                )
            else:
                raise VectorPathError(f"{label}.kind must be 'line' or 'cubic'.")

        return cls(
            start=_json_point(payload["start"], "start"),
            segments=tuple(segments),
            closed=cast(bool, payload["closed"]),
        )

    @classmethod
    def from_json(cls, serialized: str) -> VectorPath:
        """Load canonical path JSON, rejecting duplicate keys and constants."""

        if not isinstance(serialized, str):
            raise VectorPathError("serialized path JSON must be text.")
        try:
            document = json.loads(
                serialized,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except VectorPathError:
            raise
        except (json.JSONDecodeError, TypeError, UnicodeError) as error:
            raise VectorPathError(f"Invalid vector path JSON: {error}") from error
        return cls.from_dict(document)

    def canonical_json(self) -> str:
        """Return compact, key-sorted JSON suitable for content addressing."""

        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    def canonical_sha256(self) -> str:
        """Return the SHA-256 digest of :meth:`canonical_json`."""

        return hashlib.sha256(self.canonical_json().encode("ascii")).hexdigest()


_CubicGeometry: TypeAlias = tuple[Point, Point, Point, Point]


def _midpoint(first: Point, second: Point) -> Point:
    return ((first[0] + second[0]) * 0.5, (first[1] + second[1]) * 0.5)


def _split_cubic(curve: _CubicGeometry) -> tuple[_CubicGeometry, _CubicGeometry]:
    p0, p1, p2, p3 = curve
    p01 = _midpoint(p0, p1)
    p12 = _midpoint(p1, p2)
    p23 = _midpoint(p2, p3)
    p012 = _midpoint(p01, p12)
    p123 = _midpoint(p12, p23)
    midpoint = _midpoint(p012, p123)
    return (p0, p01, p012, midpoint), (midpoint, p123, p23, p3)


def _adaptive_cubic_length(
    curve: _CubicGeometry,
    tolerance: float,
    *,
    depth: int,
) -> float:
    p0, p1, p2, p3 = curve
    lower_bound = _distance(p0, p3)
    upper_bound = _distance(p0, p1) + _distance(p1, p2) + _distance(p2, p3)
    if (upper_bound - lower_bound) * 0.5 <= tolerance:
        return (upper_bound + lower_bound) * 0.5
    if depth >= _MAX_ADAPTIVE_DEPTH:
        raise VectorPathError(
            "Cubic length could not meet the requested tolerance within "
            f"{_MAX_ADAPTIVE_DEPTH} subdivisions."
        )
    left, right = _split_cubic(curve)
    child_tolerance = tolerance * 0.5
    return _adaptive_cubic_length(
        left, child_tolerance, depth=depth + 1
    ) + _adaptive_cubic_length(right, child_tolerance, depth=depth + 1)


def _distance_to_segment(point: Point, start: Point, end: Point) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    squared_length = dx * dx + dy * dy
    if not isfinite(squared_length):
        raise VectorPathError("Geometry is too large to flatten as finite floats.")
    if squared_length == 0.0:
        return _distance(point, start)
    projection = (
        (point[0] - start[0]) * dx + (point[1] - start[1]) * dy
    ) / squared_length
    projection = min(1.0, max(0.0, projection))
    nearest = (start[0] + projection * dx, start[1] + projection * dy)
    return _distance(point, nearest)


def _flatten_cubic(
    curve: _CubicGeometry,
    tolerance: float,
    points: list[Point],
    *,
    depth: int,
) -> int:
    p0, p1, p2, p3 = curve
    if p0 == p1 == p2 == p3:
        # Rounding can collapse a deeply subdivided leaf.  It carries no new
        # geometry, so emitting its endpoint would create a zero-length line.
        return depth
    is_flat = (
        p0 != p3
        and max(
            _distance_to_segment(p1, p0, p3),
            _distance_to_segment(p2, p0, p3),
        )
        <= tolerance
    )
    if is_flat:
        if points[-1] != p3:
            points.append(p3)
        return depth
    if depth >= _MAX_ADAPTIVE_DEPTH:
        raise VectorPathError(
            "Cubic flattening could not meet the requested tolerance within "
            f"{_MAX_ADAPTIVE_DEPTH} subdivisions."
        )
    left, right = _split_cubic(curve)
    left_depth = _flatten_cubic(left, tolerance, points, depth=depth + 1)
    right_depth = _flatten_cubic(right, tolerance, points, depth=depth + 1)
    return max(left_depth, right_depth)


def _cubic_point(curve: _CubicGeometry, parameter: float) -> Point:
    p0, p1, p2, p3 = curve
    inverse = 1.0 - parameter
    inverse_squared = inverse * inverse
    parameter_squared = parameter * parameter
    result = (
        inverse_squared * inverse * p0[0]
        + 3.0 * inverse_squared * parameter * p1[0]
        + 3.0 * inverse * parameter_squared * p2[0]
        + parameter_squared * parameter * p3[0],
        inverse_squared * inverse * p0[1]
        + 3.0 * inverse_squared * parameter * p1[1]
        + 3.0 * inverse * parameter_squared * p2[1]
        + parameter_squared * parameter * p3[1],
    )
    return _point(result, "evaluated cubic point")


def _cubic_extrema_parameters(
    p0: float,
    p1: float,
    p2: float,
    p3: float,
) -> tuple[float, ...]:
    # The factor of three in the Bezier derivative is irrelevant to its roots.
    quadratic = -p0 + 3.0 * p1 - 3.0 * p2 + p3
    linear = 2.0 * (p0 - 2.0 * p1 + p2)
    constant = p1 - p0
    if not all(isfinite(value) for value in (quadratic, linear, constant)):
        raise VectorPathError("Geometry is too large to bound as finite floats.")

    # Normalize before solving.  This avoids overflow/underflow in the
    # discriminant without an absolute epsilon that would erase extrema on a
    # legitimately tiny drawing.
    scale = max(abs(quadratic), abs(linear), abs(constant))
    if scale == 0.0:
        return ()
    quadratic /= scale
    linear /= scale
    constant /= scale
    if quadratic == 0.0:
        if linear == 0.0:
            return ()
        root = -constant / linear
        return (root,) if 0.0 < root < 1.0 else ()

    discriminant = linear * linear - 4.0 * quadratic * constant
    if discriminant < 0.0:
        return ()
    root_discriminant = sqrt(discriminant)
    q = -0.5 * (linear + copysign(root_discriminant, linear))
    roots = (
        (-linear / (2.0 * quadratic),) if q == 0.0 else (q / quadratic, constant / q)
    )
    return tuple(sorted({root for root in roots if 0.0 < root < 1.0}))


def _format_number(value: float) -> str:
    if value == 0.0:
        return "0"
    serialized = repr(value)
    return serialized[:-2] if serialized.endswith(".0") else serialized


def _format_point(point: Point) -> str:
    return f"{_format_number(point[0])},{_format_number(point[1])}"


def _strict_object(
    value: object,
    expected_keys: set[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise VectorPathError(f"{label} must be a JSON object.")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw):
        raise VectorPathError(f"{label} keys must be strings.")
    payload = cast(dict[str, object], value)
    actual_keys = set(payload)
    missing = sorted(expected_keys - actual_keys)
    extra = sorted(actual_keys - expected_keys)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {missing!r}")
        if extra:
            details.append(f"unexpected {extra!r}")
        raise VectorPathError(f"{label} has {', '.join(details)}.")
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VectorPathError(f"Duplicate JSON key {key!r}.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise VectorPathError(f"Invalid non-finite JSON constant {value!r}.")


def vector_path_from_json(serialized: str) -> VectorPath:
    """Module-level counterpart to :meth:`VectorPath.from_json`."""

    return VectorPath.from_json(serialized)


def canonical_path_json(path: VectorPath) -> str:
    """Return stable canonical JSON for ``path``."""

    if type(path) is not VectorPath:
        raise VectorPathError("path must be a VectorPath.")
    return path.canonical_json()


def canonical_path_sha256(path: VectorPath) -> str:
    """Return a stable content digest for ``path``."""

    if type(path) is not VectorPath:
        raise VectorPathError("path must be a VectorPath.")
    return path.canonical_sha256()


__all__ = [
    "Affine2D",
    "CubicSegment",
    "FlattenedPath",
    "LineSegment",
    "PathBounds",
    "PathSegment",
    "Point",
    "VectorPath",
    "VectorPathError",
    "canonical_path_json",
    "canonical_path_sha256",
    "vector_path_from_json",
]

"""Evidence-conscious geometry cleanup for engineered-object artwork.

The routines in this module deliberately stop short of claiming that computer
vision has recovered a technical drawing.  Raster edges are candidates.  They
are perspective-corrected, linked, simplified, de-duplicated, classified and
quality-gated before a caller may turn them into plate geometry.  Hidden or
section geometry is not generated here at all.

All coordinates are ordinary Cartesian pairs.  Functions that depend on a
physical output accept an explicit nib width or tolerance; there is no hidden
pixel or page scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import math
from pathlib import Path
from statistics import median
from typing import Iterable, Iterator, Literal, Sequence

from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Point as ShapelyPoint, Polygon
from shapely.geometry.base import BaseGeometry

from .models import MapPlotterError
from .vector_path import CubicSegment, LineSegment, VectorPath


Point = tuple[float, float]
Stroke = list[Point]
GrayImage = tuple[tuple[int, ...], ...]


class TechnicalGeometryError(MapPlotterError):
    """Raised when source geometry cannot support an honest object drawing."""


@dataclass(frozen=True, slots=True)
class PerspectiveTransform:
    """Projective transform with the final 3x3 coefficient fixed to one."""

    a: float
    b: float
    c: float
    d: float
    e: float
    f: float
    g: float
    h: float

    def apply(self, point: Point) -> Point:
        x, y = _finite_point(point, "perspective point")
        denominator = self.g * x + self.h * y + 1.0
        if abs(denominator) <= 1e-12:
            raise TechnicalGeometryError(
                "Perspective correction maps a source point to infinity."
            )
        return (
            (self.a * x + self.b * y + self.c) / denominator,
            (self.d * x + self.e * y + self.f) / denominator,
        )

    def inverse(self) -> "PerspectiveTransform":
        matrix = (
            (self.a, self.b, self.c),
            (self.d, self.e, self.f),
            (self.g, self.h, 1.0),
        )
        inverse = _invert_3x3(matrix)
        scale = inverse[2][2]
        if abs(scale) <= 1e-12:
            raise TechnicalGeometryError(
                "Perspective correction has no finite normalized inverse."
            )
        normalized = tuple(tuple(value / scale for value in row) for row in inverse)
        return PerspectiveTransform(
            a=normalized[0][0],
            b=normalized[0][1],
            c=normalized[0][2],
            d=normalized[1][0],
            e=normalized[1][1],
            f=normalized[1][2],
            g=normalized[2][0],
            h=normalized[2][1],
        )


@dataclass(frozen=True, slots=True)
class ContourCandidate:
    """One intentional reconstruction candidate with explicit confidence."""

    points: tuple[Point, ...]
    semantic_class: str
    feature_kind: str
    confidence: float
    closed: bool
    source_stage: str


@dataclass(frozen=True, slots=True)
class RasterReconstruction:
    """Cleaned visible-view geometry and auditable quality measurements."""

    width_px: int
    height_px: int
    contours: tuple[ContourCandidate, ...]
    foreground_fraction: float
    raw_edge_pixel_count: int
    retained_contour_count: int
    discarded_component_count: int
    perspective_corrected: bool
    quality_status: Literal["usable-visible-portrait", "insufficient-reference"]
    limitations: tuple[str, ...]


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TechnicalGeometryError(f"{label} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise TechnicalGeometryError(f"{label} must be a finite number.")
    return number


def _finite_point(value: Sequence[float], label: str) -> Point:
    if len(value) != 2:
        raise TechnicalGeometryError(f"{label} must contain two coordinates.")
    return (
        _finite_number(value[0], f"{label}.x"),
        _finite_number(value[1], f"{label}.y"),
    )


def _solve_linear(matrix: list[list[float]], values: list[float]) -> list[float]:
    """Solve a small dense system with deterministic pivoted elimination."""

    size = len(values)
    if size == 0 or len(matrix) != size or any(len(row) != size for row in matrix):
        raise TechnicalGeometryError("Perspective system must be square and non-empty.")
    augmented = [list(row) + [float(value)] for row, value in zip(matrix, values)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-12:
            raise TechnicalGeometryError(
                "Perspective reference points are degenerate or nearly collinear."
            )
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0.0:
                continue
            augmented[row] = [
                current - factor * pivot_value
                for current, pivot_value in zip(
                    augmented[row], augmented[column], strict=True
                )
            ]
    return [augmented[index][-1] for index in range(size)]


def _invert_3x3(
    matrix: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(determinant) <= 1e-12:
        raise TechnicalGeometryError("Perspective correction matrix is singular.")
    inverse = (
        (e * i - f * h, c * h - b * i, b * f - c * e),
        (f * g - d * i, a * i - c * g, c * d - a * f),
        (d * h - e * g, b * g - a * h, a * e - b * d),
    )
    return (
        (
            inverse[0][0] / determinant,
            inverse[0][1] / determinant,
            inverse[0][2] / determinant,
        ),
        (
            inverse[1][0] / determinant,
            inverse[1][1] / determinant,
            inverse[1][2] / determinant,
        ),
        (
            inverse[2][0] / determinant,
            inverse[2][1] / determinant,
            inverse[2][2] / determinant,
        ),
    )


def perspective_transform(
    source_points: Sequence[Sequence[float]],
    target_points: Sequence[Sequence[float]],
) -> PerspectiveTransform:
    """Return the homography mapping four source references to four targets."""

    if len(source_points) != 4 or len(target_points) != 4:
        raise TechnicalGeometryError(
            "Perspective correction requires exactly four source and target points."
        )
    source = [
        _finite_point(point, f"source_points[{index}]")
        for index, point in enumerate(source_points)
    ]
    target = [
        _finite_point(point, f"target_points[{index}]")
        for index, point in enumerate(target_points)
    ]
    if len(set(source)) != 4 or len(set(target)) != 4:
        raise TechnicalGeometryError("Perspective reference points must be distinct.")
    matrix: list[list[float]] = []
    values: list[float] = []
    for (x, y), (u, v) in zip(source, target, strict=True):
        matrix.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        values.append(u)
        matrix.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        values.append(v)
    coefficients = _solve_linear(matrix, values)
    return PerspectiveTransform(*coefficients)


def correct_perspective_points(
    strokes: Iterable[Sequence[Sequence[float]]],
    source_points: Sequence[Sequence[float]],
    target_points: Sequence[Sequence[float]],
) -> list[Stroke]:
    """Correct vector candidates without resampling their topology."""

    transform = perspective_transform(source_points, target_points)
    return [
        [transform.apply(_finite_point(point, "stroke point")) for point in stroke]
        for stroke in strokes
    ]


def correct_perspective_image(
    image: GrayImage,
    source_points: Sequence[Sequence[float]],
    *,
    output_width: int,
    output_height: int,
) -> GrayImage:
    """Nearest-neighbour projective correction for candidate generation.

    This is intentionally not an image enhancement or final-art renderer.  It
    exists so edge candidates are measured in a corrected reference plane.
    """

    _validate_gray_image(image)
    if output_width < 2 or output_height < 2:
        raise TechnicalGeometryError("Corrected image dimensions must be at least 2.")
    targets = (
        (0.0, 0.0),
        (float(output_width - 1), 0.0),
        (float(output_width - 1), float(output_height - 1)),
        (0.0, float(output_height - 1)),
    )
    inverse = perspective_transform(source_points, targets).inverse()
    source_height = len(image)
    source_width = len(image[0])
    rows: list[tuple[int, ...]] = []
    for y in range(output_height):
        row: list[int] = []
        for x in range(output_width):
            source_x, source_y = inverse.apply((float(x), float(y)))
            ix = min(max(int(round(source_x)), 0), source_width - 1)
            iy = min(max(int(round(source_y)), 0), source_height - 1)
            row.append(image[iy][ix])
        rows.append(tuple(row))
    return tuple(rows)


def _validate_gray_image(image: GrayImage) -> None:
    if not image or not image[0]:
        raise TechnicalGeometryError("A raster reference cannot be empty.")
    width = len(image[0])
    if any(len(row) != width for row in image):
        raise TechnicalGeometryError("Raster rows must all have the same width.")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
        for row in image
        for value in row
    ):
        raise TechnicalGeometryError("Raster values must be 8-bit grayscale integers.")


def _pgm_tokens(payload: bytes) -> Iterator[bytes]:
    token = bytearray()
    in_comment = False
    for value in payload:
        if in_comment:
            if value in {10, 13}:
                in_comment = False
            continue
        if value == 35:
            if token:
                yield bytes(token)
                token.clear()
            in_comment = True
            continue
        if chr(value).isspace():
            if token:
                yield bytes(token)
                token.clear()
            continue
        token.append(value)
    if token:
        yield bytes(token)


def _read_ascii_pgm(payload: bytes) -> GrayImage:
    tokens = list(_pgm_tokens(payload))
    if len(tokens) < 4 or tokens[0] != b"P2":
        raise TechnicalGeometryError("Only well-formed ASCII P2 PGM is accepted here.")
    try:
        width, height, maximum = map(int, tokens[1:4])
        samples = [int(value) for value in tokens[4:]]
    except ValueError as exc:
        raise TechnicalGeometryError("PGM header or samples are not integers.") from exc
    if width < 2 or height < 2 or width * height > 25_000_000:
        raise TechnicalGeometryError("PGM dimensions are invalid or exceed 25 MP.")
    if maximum <= 0 or maximum > 65_535 or len(samples) != width * height:
        raise TechnicalGeometryError("PGM sample count or maximum value is invalid.")
    if any(not 0 <= sample <= maximum for sample in samples):
        raise TechnicalGeometryError("PGM samples fall outside the declared range.")
    scaled = [round(sample * 255.0 / maximum) for sample in samples]
    return tuple(
        tuple(scaled[row * width : (row + 1) * width]) for row in range(height)
    )


def load_grayscale_image(path: Path) -> GrayImage:
    """Load a local reference without any network or metadata interpretation."""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise TechnicalGeometryError(
            f"Cannot read raster reference {path}: {exc}"
        ) from exc
    if payload.startswith(b"P2"):
        return _read_ascii_pgm(payload)
    try:
        image_module = import_module("PIL.Image")
    except ModuleNotFoundError as exc:
        raise TechnicalGeometryError(
            "PNG/JPEG/TIFF references require Pillow; ASCII P2 PGM works without it."
        ) from exc
    try:
        with image_module.open(path) as opened:
            if opened.width * opened.height > 25_000_000:
                raise TechnicalGeometryError(
                    "Raster reference exceeds the 25 MP limit."
                )
            grayscale = opened.convert("L")
            pixels = list(grayscale.getdata())
            width, height = grayscale.size
    except (OSError, ValueError) as exc:
        raise TechnicalGeometryError(
            f"Cannot decode raster reference {path}: {exc}"
        ) from exc
    return tuple(
        tuple(int(value) for value in pixels[row * width : (row + 1) * width])
        for row in range(height)
    )


def automatic_reference_quad(
    image: GrayImage, *, difference_threshold: int = 18
) -> tuple[Point, Point, Point, Point]:
    """Select the foreground bounding quadrilateral as a conservative default."""

    mask = foreground_mask(image, difference_threshold=difference_threshold)
    foreground = [
        (x, y) for y, row in enumerate(mask) for x, present in enumerate(row) if present
    ]
    if not foreground:
        raise TechnicalGeometryError(
            "No foreground could be isolated for auto framing."
        )
    xs = [point[0] for point in foreground]
    ys = [point[1] for point in foreground]
    left, right = float(min(xs)), float(max(xs))
    top, bottom = float(min(ys)), float(max(ys))
    if right - left < 2 or bottom - top < 2:
        raise TechnicalGeometryError("Auto-selected reference bounds are too small.")
    return ((left, top), (right, top), (right, bottom), (left, bottom))


def foreground_mask(
    image: GrayImage,
    *,
    difference_threshold: int = 18,
    polarity: Literal["different", "darker", "lighter"] = "different",
) -> tuple[tuple[bool, ...], ...]:
    """Separate likely subject pixels from the median image-border tone."""

    _validate_gray_image(image)
    if not 1 <= difference_threshold <= 255:
        raise TechnicalGeometryError("Foreground difference threshold must be 1..255.")
    height, width = len(image), len(image[0])
    border = [*image[0], *image[-1]]
    border.extend(row[0] for row in image[1:-1])
    border.extend(row[-1] for row in image[1:-1])
    background = float(median(border))

    def selected(value: int) -> bool:
        if polarity == "darker":
            return value <= background - difference_threshold
        if polarity == "lighter":
            return value >= background + difference_threshold
        return abs(value - background) >= difference_threshold

    return tuple(
        tuple(selected(image[y][x]) for x in range(width)) for y in range(height)
    )


def sobel_edge_mask(
    image: GrayImage, *, threshold: float = 80.0
) -> tuple[tuple[bool, ...], ...]:
    """Generate photographic edge candidates; never return plate artwork."""

    _validate_gray_image(image)
    if threshold <= 0:
        raise TechnicalGeometryError("Sobel threshold must be positive.")
    height, width = len(image), len(image[0])
    result = [[False] * width for _ in range(height)]
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            gx = (
                -image[y - 1][x - 1]
                + image[y - 1][x + 1]
                - 2 * image[y][x - 1]
                + 2 * image[y][x + 1]
                - image[y + 1][x - 1]
                + image[y + 1][x + 1]
            )
            gy = (
                -image[y - 1][x - 1]
                - 2 * image[y - 1][x]
                - image[y - 1][x + 1]
                + image[y + 1][x - 1]
                + 2 * image[y + 1][x]
                + image[y + 1][x + 1]
            )
            result[y][x] = math.hypot(gx, gy) >= threshold
    return tuple(tuple(row) for row in result)


def _mask_boundary_segments(mask: Sequence[Sequence[bool]]) -> list[Stroke]:
    height, width = len(mask), len(mask[0])
    segments: list[Stroke] = []
    for y in range(height):
        for x in range(width):
            if not mask[y][x]:
                continue
            left = x == 0 or not mask[y][x - 1]
            right = x == width - 1 or not mask[y][x + 1]
            top = y == 0 or not mask[y - 1][x]
            bottom = y == height - 1 or not mask[y + 1][x]
            if top:
                segments.append([(float(x), float(y)), (float(x + 1), float(y))])
            if right:
                segments.append(
                    [(float(x + 1), float(y)), (float(x + 1), float(y + 1))]
                )
            if bottom:
                segments.append(
                    [(float(x + 1), float(y + 1)), (float(x), float(y + 1))]
                )
            if left:
                segments.append([(float(x), float(y + 1)), (float(x), float(y))])
    return segments


def link_contours(
    strokes: Iterable[Sequence[Sequence[float]]], *, max_gap: float = 1.5
) -> list[Stroke]:
    """Link open candidate fragments by their nearest compatible endpoints."""

    if max_gap < 0:
        raise TechnicalGeometryError("Contour-link gap cannot be negative.")
    pending = [
        [_finite_point(point, "contour point") for point in stroke]
        for stroke in strokes
        if len(stroke) >= 2
    ]
    result: list[Stroke] = []
    while pending:
        current = pending.pop(0)
        changed = True
        while changed and pending:
            changed = False
            best: tuple[float, int, str] | None = None
            for index, candidate in enumerate(pending):
                distances = {
                    "tail-head": math.dist(current[-1], candidate[0]),
                    "tail-tail": math.dist(current[-1], candidate[-1]),
                    "head-tail": math.dist(current[0], candidate[-1]),
                    "head-head": math.dist(current[0], candidate[0]),
                }
                mode, distance = min(distances.items(), key=lambda item: item[1])
                proposal = (distance, index, mode)
                if best is None or proposal < best:
                    best = proposal
            if best is None or best[0] > max_gap + 1e-9:
                continue
            _, index, mode = best
            candidate = pending.pop(index)
            if mode == "tail-head":
                current.extend(
                    candidate[1:] if current[-1] == candidate[0] else candidate
                )
            elif mode == "tail-tail":
                reversed_candidate = list(reversed(candidate))
                current.extend(
                    reversed_candidate[1:]
                    if current[-1] == reversed_candidate[0]
                    else reversed_candidate
                )
            elif mode == "head-tail":
                current = (
                    candidate[:-1] + current
                    if candidate[-1] == current[0]
                    else candidate + current
                )
            else:
                reversed_candidate = list(reversed(candidate))
                current = (
                    reversed_candidate[:-1] + current
                    if reversed_candidate[-1] == current[0]
                    else reversed_candidate + current
                )
            changed = True
        result.append(_remove_adjacent_duplicates(current))
    return result


def close_contour_gaps(
    strokes: Iterable[Sequence[Sequence[float]]], *, maximum_gap: float
) -> list[Stroke]:
    """Close only endpoint gaps inside an explicit source-space tolerance."""

    if maximum_gap < 0:
        raise TechnicalGeometryError("Gap-closing tolerance cannot be negative.")
    result: list[Stroke] = []
    for raw in strokes:
        stroke = [_finite_point(point, "contour point") for point in raw]
        if len(stroke) < 2:
            continue
        if stroke[0] != stroke[-1] and math.dist(stroke[0], stroke[-1]) <= maximum_gap:
            stroke.append(stroke[0])
        result.append(stroke)
    return result


def _remove_adjacent_duplicates(points: Sequence[Point]) -> Stroke:
    result: Stroke = []
    for point in points:
        if not result or point != result[-1]:
            result.append(point)
    return result


def _distance_to_line(point: Point, start: Point, end: Point) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    squared = dx * dx + dy * dy
    if squared <= 1e-18:
        return math.dist(point, start)
    parameter = max(
        0.0,
        min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / squared),
    )
    projection = (start[0] + parameter * dx, start[1] + parameter * dy)
    return math.dist(point, projection)


def simplify_contour(points: Sequence[Sequence[float]], *, tolerance: float) -> Stroke:
    """Ramer-Douglas-Peucker simplification with explicit source tolerance."""

    if tolerance < 0:
        raise TechnicalGeometryError("Simplification tolerance cannot be negative.")
    source = _remove_adjacent_duplicates(
        [_finite_point(point, "contour point") for point in points]
    )
    if len(source) <= 2 or tolerance == 0:
        return source
    closed = source[0] == source[-1]
    working = source[:-1] if closed else source
    if closed and len(working) >= 3:
        centre = (
            sum(point[0] for point in working) / len(working),
            sum(point[1] for point in working) / len(working),
        )
        start_index = max(
            range(len(working)), key=lambda index: math.dist(working[index], centre)
        )
        working = working[start_index:] + working[:start_index]
        working.append(working[0])

    def reduce(segment: Sequence[Point]) -> Stroke:
        if len(segment) <= 2:
            return list(segment)
        distances = [
            _distance_to_line(point, segment[0], segment[-1]) for point in segment[1:-1]
        ]
        maximum = max(distances, default=0.0)
        if maximum <= tolerance:
            return [segment[0], segment[-1]]
        split = distances.index(maximum) + 1
        return reduce(segment[: split + 1])[:-1] + reduce(segment[split:])

    simplified = reduce(working)
    if closed and simplified[0] != simplified[-1]:
        simplified.append(simplified[0])
    return simplified


def smooth_contour(points: Sequence[Sequence[float]], *, iterations: int = 1) -> Stroke:
    """Corner-cut a candidate while preserving closure and open endpoints."""

    if iterations < 0 or iterations > 4:
        raise TechnicalGeometryError("Smoothing iterations must be between 0 and 4.")
    result = [_finite_point(point, "contour point") for point in points]
    closed = len(result) > 2 and result[0] == result[-1]
    for _ in range(iterations):
        if len(result) < 3:
            break
        source = result[:-1] if closed else result
        refined: Stroke = [] if closed else [source[0]]
        pair_count = len(source) if closed else len(source) - 1
        for index in range(pair_count):
            first = source[index]
            second = source[(index + 1) % len(source)]
            refined.extend(
                [
                    (
                        0.75 * first[0] + 0.25 * second[0],
                        0.75 * first[1] + 0.25 * second[1],
                    ),
                    (
                        0.25 * first[0] + 0.75 * second[0],
                        0.25 * first[1] + 0.75 * second[1],
                    ),
                ]
            )
        if closed:
            refined.append(refined[0])
        else:
            refined.append(source[-1])
        result = refined
    return result


def fit_smooth_path(
    points: Sequence[Sequence[float]], *, closed: bool | None = None
) -> VectorPath:
    """Fit a Catmull-Rom-derived cubic path through intentional contour points."""

    source = _remove_adjacent_duplicates(
        [_finite_point(point, "curve point") for point in points]
    )
    if len(source) < 2:
        raise TechnicalGeometryError(
            "Curve fitting needs at least two distinct points."
        )
    inferred_closed = len(source) > 2 and source[0] == source[-1]
    is_closed = inferred_closed if closed is None else bool(closed)
    base = source[:-1] if inferred_closed else source
    if len(base) < 3:
        return VectorPath(
            start=base[0],
            segments=tuple(LineSegment(point) for point in base[1:]),
            closed=is_closed,
        )
    segments: list[CubicSegment] = []
    count = len(base)
    segment_count = count if is_closed else count - 1
    for index in range(segment_count):
        p0 = base[(index - 1) % count] if is_closed or index > 0 else base[index]
        p1 = base[index]
        p2 = base[(index + 1) % count]
        p3 = base[(index + 2) % count] if is_closed or index + 2 < count else p2
        control_1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
        control_2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
        segments.append(CubicSegment(control_1, control_2, p2))
    return VectorPath(start=base[0], segments=tuple(segments), closed=is_closed)


def assist_symmetry(
    strokes: Iterable[Sequence[Sequence[float]]],
    *,
    axis_x: float,
    mode: Literal["mirror-missing", "average-pairs"] = "mirror-missing",
    tolerance: float,
    asymmetric_indices: Iterable[int] = (),
) -> list[Stroke]:
    """Assist explicitly symmetric sources while preserving declared asymmetry."""

    axis = _finite_number(axis_x, "symmetry axis")
    if tolerance < 0:
        raise TechnicalGeometryError("Symmetry tolerance cannot be negative.")
    source = [
        [_finite_point(point, "symmetry point") for point in stroke]
        for stroke in strokes
    ]
    asymmetric = set(asymmetric_indices)
    if any(index < 0 or index >= len(source) for index in asymmetric):
        raise TechnicalGeometryError("Asymmetric contour index is out of range.")
    result = [list(stroke) for stroke in source]

    def mirrored(stroke: Sequence[Point]) -> Stroke:
        return [(2.0 * axis - x, y) for x, y in reversed(stroke)]

    for index, stroke in enumerate(source):
        if index in asymmetric:
            continue
        reflected = mirrored(stroke)
        match = next(
            (
                candidate_index
                for candidate_index, candidate in enumerate(source)
                if candidate_index != index
                and candidate_index not in asymmetric
                and _stroke_hausdorff(candidate, reflected) <= tolerance
            ),
            None,
        )
        if match is None and mode == "mirror-missing":
            result.append(reflected)
        elif match is not None and mode == "average-pairs" and index < match:
            first = _resample_stroke(stroke, max(len(stroke), len(source[match])))
            second = _resample_stroke(mirrored(source[match]), len(first))
            averaged = [
                ((left[0] + right[0]) / 2.0, (left[1] + right[1]) / 2.0)
                for left, right in zip(first, second, strict=True)
            ]
            result[index] = averaged
            result[match] = mirrored(averaged)
    return result


def _resample_stroke(stroke: Sequence[Point], count: int) -> Stroke:
    if count < 2:
        raise TechnicalGeometryError("Stroke resampling needs at least two points.")
    if len(stroke) < 2:
        return list(stroke)
    lengths = [0.0]
    for first, second in zip(stroke, stroke[1:]):
        lengths.append(lengths[-1] + math.dist(first, second))
    total = lengths[-1]
    if total <= 1e-12:
        return [stroke[0]] * count
    result: Stroke = []
    segment = 0
    for index in range(count):
        target = total * index / (count - 1)
        while segment + 1 < len(lengths) and lengths[segment + 1] < target:
            segment += 1
        if segment + 1 >= len(stroke):
            result.append(stroke[-1])
            continue
        span = lengths[segment + 1] - lengths[segment]
        fraction = 0.0 if span <= 1e-12 else (target - lengths[segment]) / span
        first, second = stroke[segment], stroke[segment + 1]
        result.append(
            (
                first[0] + fraction * (second[0] - first[0]),
                first[1] + fraction * (second[1] - first[1]),
            )
        )
    return result


def _stroke_hausdorff(first: Sequence[Point], second: Sequence[Point]) -> float:
    left = LineString(first)
    right = LineString(second)
    return float(left.hausdorff_distance(right))


def remove_background_edges(
    strokes: Iterable[Sequence[Sequence[float]]],
    foreground_polygon: Sequence[Sequence[float]],
    *,
    minimum_inside_fraction: float = 0.7,
) -> list[Stroke]:
    """Drop texture/background candidates lying mostly outside a supplied mask."""

    if not 0.0 <= minimum_inside_fraction <= 1.0:
        raise TechnicalGeometryError("Inside fraction must lie between zero and one.")
    polygon = Polygon(
        [
            _finite_point(point, "foreground polygon point")
            for point in foreground_polygon
        ]
    )
    if not polygon.is_valid or polygon.area <= 0:
        raise TechnicalGeometryError(
            "Foreground polygon must be a valid non-zero ring."
        )
    retained: list[Stroke] = []
    for raw in strokes:
        stroke = [_finite_point(point, "edge point") for point in raw]
        if len(stroke) < 2:
            continue
        inside = sum(polygon.covers(ShapelyPoint(point)) for point in stroke)
        if inside / len(stroke) >= minimum_inside_fraction:
            retained.append(stroke)
    return retained


def eliminate_duplicate_contours(
    strokes: Iterable[Sequence[Sequence[float]]], *, tolerance: float
) -> list[Stroke]:
    """Remove same-direction, reversed and nearly coincident contour repeats."""

    if tolerance < 0:
        raise TechnicalGeometryError("Duplicate tolerance cannot be negative.")
    retained: list[Stroke] = []
    for raw in strokes:
        stroke = _remove_adjacent_duplicates(
            [_finite_point(point, "contour point") for point in raw]
        )
        if len(stroke) < 2:
            continue
        if any(_stroke_hausdorff(stroke, other) <= tolerance for other in retained):
            continue
        retained.append(stroke)
    return retained


def dimension_aware_transform(
    strokes: Iterable[Sequence[Sequence[float]]],
    *,
    source_start: Sequence[float],
    source_end: Sequence[float],
    verified_distance: float,
    output_unit_per_verified_unit: float = 1.0,
) -> list[Stroke]:
    """Scale image/vector coordinates from one supplied reference dimension."""

    start = _finite_point(source_start, "dimension source_start")
    end = _finite_point(source_end, "dimension source_end")
    distance = _finite_number(verified_distance, "verified distance")
    output_scale = _finite_number(output_unit_per_verified_unit, "output unit scale")
    source_distance = math.dist(start, end)
    if distance <= 0 or output_scale <= 0 or source_distance <= 1e-12:
        raise TechnicalGeometryError(
            "Dimension-aware transform needs positive source and verified distances."
        )
    scale = distance * output_scale / source_distance
    angle = -math.atan2(end[1] - start[1], end[0] - start[0])
    cosine, sine = math.cos(angle), math.sin(angle)
    result: list[Stroke] = []
    for raw in strokes:
        transformed: Stroke = []
        for point in raw:
            x, y = _finite_point(point, "dimension-aware point")
            local_x, local_y = x - start[0], y - start[1]
            transformed.append(
                (
                    scale * (local_x * cosine - local_y * sine),
                    scale * (local_x * sine + local_y * cosine),
                )
            )
        result.append(transformed)
    return result


def occlusion_aware_simplify(
    strokes: Iterable[Sequence[Sequence[float]]],
    occluders: Iterable[Sequence[Sequence[float]]],
    *,
    tolerance: float,
) -> list[Stroke]:
    """Subtract supplied visible occluders; never synthesize hidden continuations."""

    polygons = [
        Polygon([_finite_point(point, "occluder point") for point in polygon])
        for polygon in occluders
    ]
    if any(not polygon.is_valid for polygon in polygons):
        raise TechnicalGeometryError("Occluder polygons must be valid.")
    result: list[Stroke] = []
    for raw in strokes:
        stroke = simplify_contour(raw, tolerance=tolerance)
        if len(stroke) < 2:
            continue
        geometry: BaseGeometry = LineString(stroke)
        for polygon in polygons:
            geometry = geometry.difference(polygon)
        for line in _linear_parts(geometry):
            points = [(float(x), float(y)) for x, y in line.coords]
            if len(points) >= 2:
                result.append(points)
    return result


def _linear_parts(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    return [
        child
        for child in getattr(geometry, "geoms", ())
        if isinstance(child, LineString)
    ]


def hatch_polygon(
    ring: Sequence[Sequence[float]],
    *,
    nib_mm: float,
    density: Literal["sparse", "medium", "rich"] = "medium",
    angle_deg: float = 45.0,
    cross_hatch: bool = False,
) -> list[Stroke]:
    """Create nib-aware conventional hatching clipped to a supplied region."""

    nib = _finite_number(nib_mm, "hatch nib")
    if nib <= 0:
        raise TechnicalGeometryError("Hatch nib must be positive.")
    spacing_factor = {"sparse": 10.0, "medium": 7.0, "rich": 5.0}[density]
    polygon = Polygon([_finite_point(point, "hatch ring point") for point in ring])
    if not polygon.is_valid or polygon.area <= 0:
        raise TechnicalGeometryError("Hatch region must be a valid closed polygon.")

    def one_direction(angle: float) -> list[Stroke]:
        rotated = affinity.rotate(polygon, -angle, origin="centroid")
        min_x, min_y, max_x, max_y = rotated.bounds
        spacing = spacing_factor * nib
        diagonal = math.hypot(max_x - min_x, max_y - min_y)
        y = math.floor(min_y / spacing) * spacing
        strokes: list[Stroke] = []
        while y <= max_y + spacing:
            line = LineString([(min_x - diagonal, y), (max_x + diagonal, y)])
            clipped = line.intersection(rotated)
            restored = affinity.rotate(clipped, angle, origin=polygon.centroid)
            for part in _linear_parts(restored):
                points = [(float(x), float(y_value)) for x, y_value in part.coords]
                if LineString(points).length + 1e-9 >= 3.0 * nib:
                    strokes.append(points)
            y += spacing
        return strokes

    result = one_direction(angle_deg)
    if cross_hatch:
        result.extend(one_direction(angle_deg + 90.0))
    return eliminate_duplicate_contours(result, tolerance=nib * 0.25)


def stipple_polygon(
    ring: Sequence[Sequence[float]],
    *,
    nib_mm: float,
    density: Literal["sparse", "medium", "rich"] = "medium",
) -> list[Stroke]:
    """Return deterministic small plotted loops rather than grayscale dots."""

    nib = _finite_number(nib_mm, "stipple nib")
    polygon = Polygon([_finite_point(point, "stipple ring point") for point in ring])
    if nib <= 0 or not polygon.is_valid or polygon.area <= 0:
        raise TechnicalGeometryError("Stipple region and nib must be valid.")
    spacing_factor = {"sparse": 14.0, "medium": 10.0, "rich": 7.0}[density]
    spacing = spacing_factor * nib
    # A 12-segment loop is slightly shorter than its ideal circumference, so
    # give it explicit headroom above the universal three-nib path floor.
    radius = 3.1 * nib / (2.0 * math.pi)
    segments = 12
    min_x, min_y, max_x, max_y = polygon.bounds
    result: list[Stroke] = []
    row = 0
    y = min_y + spacing / 2.0
    while y <= max_y:
        x = min_x + spacing / 2.0 + (spacing / 2.0 if row % 2 else 0.0)
        while x <= max_x:
            centre = ShapelyPoint(x, y)
            if polygon.buffer(-radius).covers(centre):
                points = [
                    (
                        x + radius * math.cos(2.0 * math.pi * index / segments),
                        y + radius * math.sin(2.0 * math.pi * index / segments),
                    )
                    for index in range(segments)
                ]
                result.append([*points, points[0]])
            x += spacing
        row += 1
        y += spacing
    return result


def feature_group_candidates(
    strokes: Iterable[Sequence[Sequence[float]]],
    *,
    category: str,
) -> list[tuple[str, str, float]]:
    """Classify visible candidates into category-aware semantic groups.

    The return tuple is ``(feature_kind, semantic_class, confidence)`` in the
    same order as the supplied strokes.  It deliberately does not invent a
    missing feature; it only names visible geometry.
    """

    supported = {
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
    if category not in supported:
        raise TechnicalGeometryError(f"Unsupported technical category {category!r}.")
    materialized = [
        [_finite_point(point, "feature point") for point in stroke]
        for stroke in strokes
    ]
    if not materialized:
        return []
    all_points = [point for stroke in materialized for point in stroke]
    min_x = min(point[0] for point in all_points)
    max_x = max(point[0] for point in all_points)
    min_y = min(point[1] for point in all_points)
    max_y = max(point[1] for point in all_points)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    result: list[tuple[str, str, float]] = []
    wheeled = category in {
        "car",
        "racing-car",
        "motorcycle",
        "bicycle",
        "train",
        "locomotive",
        "tram",
    }
    for stroke in materialized:
        xs = [point[0] for point in stroke]
        ys = [point[1] for point in stroke]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)
        closed = len(stroke) > 3 and math.dist(stroke[0], stroke[-1]) <= 1e-6
        centre_y = (min(ys) + max(ys)) / 2.0
        aspect = width / max(height, 1e-9)
        radial_confidence = _radial_confidence(stroke) if closed else 0.0
        relative_length = LineString(stroke).length / math.hypot(span_x, span_y)
        if wheeled and radial_confidence >= 0.78 and centre_y >= min_y + 0.52 * span_y:
            result.append(
                ("wheel-tyre-rim", "major_structural_edges", radial_confidence)
            )
        elif (
            category in {"boat", "yacht", "rowing-shell", "ship", "personal-watercraft"}
            and aspect >= 3.0
            and centre_y >= min_y + 0.45 * span_y
        ):
            result.append(("hull-sheer-waterline", "principal_silhouette", 0.78))
        elif (
            category in {"aircraft", "glider", "helicopter", "drone", "spacecraft"}
            and aspect >= 2.5
        ):
            result.append(("wing-fuselage-planform", "principal_silhouette", 0.74))
        elif (
            category
            in {
                "engine",
                "motor",
                "drivetrain",
                "turbine",
                "machinery",
                "watch",
                "camera",
                "scientific-instrument",
            }
            and radial_confidence >= 0.8
        ):
            result.append(
                ("circular-mechanical-feature", "mechanical_detail", radial_confidence)
            )
        elif closed and min(ys) <= min_y + 0.6 * span_y:
            result.append(("glazing-or-opening", "glazing_openings", 0.66))
        elif relative_length >= 0.65:
            result.append(("principal-visible-contour", "principal_silhouette", 0.7))
        else:
            result.append(("visible-secondary-edge", "panel_seam_lines", 0.55))
    return result


def _radial_confidence(stroke: Sequence[Point]) -> float:
    points = stroke[:-1] if stroke and stroke[0] == stroke[-1] else stroke
    if len(points) < 8:
        return 0.0
    centre = (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )
    radii = [math.dist(point, centre) for point in points]
    mean = sum(radii) / len(radii)
    if mean <= 1e-12:
        return 0.0
    deviation = math.sqrt(sum((radius - mean) ** 2 for radius in radii) / len(radii))
    return max(0.0, min(1.0, 1.0 - deviation / mean))


def simplify_for_plotting(
    strokes: Iterable[Sequence[Sequence[float]]],
    *,
    nib_mm: float,
    source_units_per_mm: float,
    preserve_closed: bool = True,
) -> tuple[list[Stroke], int]:
    """Apply physical simplification, duplicate removal and the three-nib gate."""

    nib = _finite_number(nib_mm, "plot nib")
    units_per_mm = _finite_number(source_units_per_mm, "source units per mm")
    if nib <= 0 or units_per_mm <= 0:
        raise TechnicalGeometryError("Plot nib and source scale must be positive.")
    tolerance = 0.5 * nib * units_per_mm
    minimum_length = 3.0 * nib * units_per_mm
    simplified: list[Stroke] = []
    omitted = 0
    for raw in strokes:
        source = [_finite_point(point, "plot cleanup point") for point in raw]
        was_closed = len(source) > 2 and source[0] == source[-1]
        result = simplify_contour(source, tolerance=tolerance)
        if preserve_closed and was_closed and result and result[0] != result[-1]:
            result.append(result[0])
        if len(result) < 2 or LineString(result).length + 1e-9 < minimum_length:
            omitted += 1
            continue
        simplified.append(result)
    deduplicated = eliminate_duplicate_contours(
        simplified, tolerance=0.35 * nib * units_per_mm
    )
    omitted += len(simplified) - len(deduplicated)
    return deduplicated, omitted


def reconstruct_raster_reference(
    image: GrayImage,
    *,
    category: str,
    perspective_source_points: Sequence[Sequence[float]] | None = None,
    auto_perspective: bool = False,
    foreground_difference: int = 18,
    edge_threshold: float = 80.0,
    minimum_component_pixels: int = 8,
    smoothing_iterations: int = 1,
) -> RasterReconstruction:
    """Turn a photograph into cleaned *visible-view* illustration candidates."""

    _validate_gray_image(image)
    working = image
    if perspective_source_points is not None and auto_perspective:
        raise TechnicalGeometryError(
            "Choose supplied perspective points or automatic reference points, not both."
        )
    selected_perspective_points = perspective_source_points
    if auto_perspective:
        selected_perspective_points = automatic_reference_quad(
            image, difference_threshold=foreground_difference
        )
    perspective_corrected = selected_perspective_points is not None
    if selected_perspective_points is not None:
        working = correct_perspective_image(
            image,
            selected_perspective_points,
            output_width=len(image[0]),
            output_height=len(image),
        )
    foreground = foreground_mask(working, difference_threshold=foreground_difference)
    edge_mask = sobel_edge_mask(working, threshold=edge_threshold)
    foreground_count = sum(value for row in foreground for value in row)
    edge_count = sum(value for row in edge_mask for value in row)
    total = len(working) * len(working[0])

    silhouette_segments = _mask_boundary_segments(foreground)
    silhouette = link_contours(silhouette_segments, max_gap=0.01)
    edge_components = _connected_components(edge_mask)
    discarded_components = sum(
        len(component) < minimum_component_pixels for component in edge_components
    )
    edge_strokes = [
        _component_axis_stroke(component)
        for component in edge_components
        if len(component) >= minimum_component_pixels
    ]
    candidates = [*silhouette, *edge_strokes]
    candidates = close_contour_gaps(candidates, maximum_gap=2.0)
    candidates = [
        smooth_contour(
            simplify_contour(stroke, tolerance=0.8),
            iterations=smoothing_iterations,
        )
        for stroke in candidates
        if len(stroke) >= 2
    ]
    candidates = eliminate_duplicate_contours(candidates, tolerance=1.25)
    diagonal = math.hypot(len(working[0]), len(working))
    candidates = [
        stroke
        for stroke in candidates
        if LineString(stroke).length >= max(4.0, 0.025 * diagonal)
    ]
    classifications = feature_group_candidates(candidates, category=category)
    contours = tuple(
        ContourCandidate(
            points=tuple(stroke),
            semantic_class=semantic_class,
            feature_kind=feature_kind,
            confidence=confidence,
            closed=len(stroke) > 2 and stroke[0] == stroke[-1],
            source_stage="perspective-corrected-linked-smoothed-candidate-v1",
        )
        for stroke, (feature_kind, semantic_class, confidence) in zip(
            candidates, classifications, strict=True
        )
    )
    foreground_fraction = foreground_count / total
    long_contours = sum(
        LineString(contour.points).length >= 0.15 * diagonal for contour in contours
    )
    limitations: list[str] = [
        "Only geometry visible in the supplied raster view is represented.",
        "No hidden, reverse-side, internal or section geometry was inferred.",
    ]
    usable = (
        0.005 <= foreground_fraction <= 0.85
        and len(contours) >= 2
        and long_contours >= 1
        and edge_count <= int(total * 0.65)
    )
    if not usable:
        limitations.append(
            "Reference quality gate failed: supply a cleaner view, mask, or vector source."
        )
    return RasterReconstruction(
        width_px=len(working[0]),
        height_px=len(working),
        contours=contours if usable else (),
        foreground_fraction=round(foreground_fraction, 6),
        raw_edge_pixel_count=edge_count,
        retained_contour_count=len(contours) if usable else 0,
        discarded_component_count=discarded_components,
        perspective_corrected=perspective_corrected,
        quality_status=(
            "usable-visible-portrait" if usable else "insufficient-reference"
        ),
        limitations=tuple(limitations),
    )


def _connected_components(
    mask: Sequence[Sequence[bool]],
) -> list[list[tuple[int, int]]]:
    height, width = len(mask), len(mask[0])
    remaining = {(x, y) for y in range(height) for x in range(width) if mask[y][x]}
    components: list[list[tuple[int, int]]] = []
    while remaining:
        start = min(remaining, key=lambda point: (point[1], point[0]))
        remaining.remove(start)
        stack = [start]
        component: list[tuple[int, int]] = []
        while stack:
            point = stack.pop()
            component.append(point)
            x, y = point
            neighbours = [
                (x + dx, y + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dx or dy
            ]
            for neighbour in sorted(neighbours, reverse=True):
                if neighbour in remaining:
                    remaining.remove(neighbour)
                    stack.append(neighbour)
        components.append(component)
    return components


def _component_axis_stroke(component: Sequence[tuple[int, int]]) -> Stroke:
    """Reduce a noisy connected edge component to an ordered centreline."""

    centre_x = sum(point[0] for point in component) / len(component)
    centre_y = sum(point[1] for point in component) / len(component)
    xx = sum((point[0] - centre_x) ** 2 for point in component)
    yy = sum((point[1] - centre_y) ** 2 for point in component)
    xy = sum((point[0] - centre_x) * (point[1] - centre_y) for point in component)
    angle = 0.5 * math.atan2(2.0 * xy, xx - yy)
    direction = (math.cos(angle), math.sin(angle))
    ordered = sorted(
        component,
        key=lambda point: (
            (point[0] - centre_x) * direction[0] + (point[1] - centre_y) * direction[1],
            point[1],
            point[0],
        ),
    )
    bucket_count = min(64, max(2, round(math.sqrt(len(ordered)))))
    result: Stroke = []
    for bucket in range(bucket_count):
        start = len(ordered) * bucket // bucket_count
        end = len(ordered) * (bucket + 1) // bucket_count
        values = ordered[start:end]
        if not values:
            continue
        result.append(
            (
                sum(point[0] for point in values) / len(values),
                sum(point[1] for point in values) / len(values),
            )
        )
    return _remove_adjacent_duplicates(result)


__all__ = [
    "ContourCandidate",
    "GrayImage",
    "PerspectiveTransform",
    "RasterReconstruction",
    "TechnicalGeometryError",
    "assist_symmetry",
    "automatic_reference_quad",
    "close_contour_gaps",
    "correct_perspective_image",
    "correct_perspective_points",
    "dimension_aware_transform",
    "eliminate_duplicate_contours",
    "feature_group_candidates",
    "fit_smooth_path",
    "foreground_mask",
    "hatch_polygon",
    "link_contours",
    "load_grayscale_image",
    "occlusion_aware_simplify",
    "perspective_transform",
    "reconstruct_raster_reference",
    "remove_background_edges",
    "simplify_contour",
    "simplify_for_plotting",
    "smooth_contour",
    "sobel_edge_mask",
    "stipple_polygon",
]

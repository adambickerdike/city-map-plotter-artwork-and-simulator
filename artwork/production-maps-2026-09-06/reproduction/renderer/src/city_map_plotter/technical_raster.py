"""Strict, source-replayable extraction from pinned one-bit drawings.

This module is deliberately narrower than :mod:`technical_geometry`.  It does
not treat a raster as a photograph to be interpreted or cleaned up.  The input
must already be a one-bit technical drawing whose exact bytes, dimensions,
polarity and crop are supplied by the caller.

The retained source ink is thinned with Zhang--Suen, every undirected skeleton
edge is chained exactly once, and a chain is simplified only when the proposed
segment remains on retained source ink.  There is no smoothing, gap closure,
symmetry assistance, feature inference or approximate de-duplication here.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import import_module
import json
import math
from pathlib import Path
import re
from typing import Iterable, Iterator, NoReturn, Sequence, TypeAlias

import numpy as np
from numpy.typing import NDArray

from .models import MapPlotterError


BoolMask: TypeAlias = NDArray[np.bool_]
PixelPoint: TypeAlias = tuple[int, int]
PixelEdge: TypeAlias = tuple[PixelPoint, PixelPoint]

MAX_SIMPLIFICATION_TOLERANCE_PX = 0.25
_SHA256 = re.compile(r"[0-9a-f]{64}")


class TechnicalRasterError(MapPlotterError):
    """Raised when a raster source or extraction recipe is not exact."""


def _fail(message: str) -> NoReturn:
    raise TechnicalRasterError(message)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{label} must be an integer.")
    return int(value)


@dataclass(frozen=True, slots=True, order=True)
class PixelRect:
    """An integer half-open rectangle in source-pixel coordinates."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            object.__setattr__(self, name, _integer(getattr(self, name), name))
        if self.x < 0 or self.y < 0:
            _fail("Pixel rectangle origin cannot be negative.")
        if self.width <= 0 or self.height <= 0:
            _fail("Pixel rectangle width and height must be positive.")

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def record(self) -> dict[str, int]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class BinaryCentrelinePath:
    """One crop-local source-ink path; closed paths do not repeat the start."""

    points: tuple[PixelPoint, ...]
    closed: bool = False

    def __post_init__(self) -> None:
        if type(self.closed) is not bool:
            _fail("Binary centreline path closed must be boolean.")
        minimum = 3 if self.closed else 2
        if len(self.points) < minimum:
            _fail(
                f"A {'closed' if self.closed else 'open'} centreline path needs "
                f"at least {minimum} points."
            )
        checked: list[PixelPoint] = []
        for index, point in enumerate(self.points):
            if not isinstance(point, tuple) or len(point) != 2:
                _fail(f"Centreline point {index} must be an (x, y) tuple.")
            checked.append(
                (
                    _integer(point[0], f"points[{index}].x"),
                    _integer(point[1], f"points[{index}].y"),
                )
            )
        if any(first == second for first, second in zip(checked, checked[1:])):
            _fail("A centreline path cannot contain adjacent duplicate points.")
        if self.closed and checked[0] == checked[-1]:
            _fail("Closed centreline paths must not repeat their first point.")

    def edges(self) -> Iterator[PixelEdge]:
        yield from zip(self.points, self.points[1:])
        if self.closed:
            yield (self.points[-1], self.points[0])

    def record(self) -> dict[str, object]:
        return {
            "closed": self.closed,
            "points": [[x, y] for x, y in self.points],
        }


@dataclass(frozen=True, slots=True)
class SourceInkReplayAudit:
    """Exact result of sampling output paths back onto a retained ink mask."""

    sample_count: int
    off_ink_sample_count: int
    failed_path_indices: tuple[int, ...]

    @property
    def passed(self) -> bool:
        return self.off_ink_sample_count == 0 and not self.failed_path_indices

    def record(self) -> dict[str, object]:
        return {
            "sample_count": self.sample_count,
            "off_ink_sample_count": self.off_ink_sample_count,
            "failed_path_indices": list(self.failed_path_indices),
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class BinaryExtractionStats:
    """Accounting for every deterministic stage of a binary extraction."""

    source_ink_pixels: int
    rectangle_exclusion_pixels: int
    supplied_mask_exclusion_pixels: int
    union_exclusion_pixels: int
    excluded_source_ink_pixels: int
    retained_source_ink_pixels: int
    thinning_iterations: int
    skeleton_pixels: int
    isolated_skeleton_pixels: int
    skeleton_edge_count: int
    traced_skeleton_edge_count: int
    raw_path_count: int
    raw_vertex_count: int
    simplified_path_count: int
    simplification_fallback_path_count: int
    output_path_count: int
    output_vertex_count: int
    output_segment_count: int
    replay_sample_count: int
    replay_off_ink_sample_count: int
    simplification_tolerance_px: float

    def record(self) -> dict[str, int | float]:
        return {
            "source_ink_pixels": self.source_ink_pixels,
            "rectangle_exclusion_pixels": self.rectangle_exclusion_pixels,
            "supplied_mask_exclusion_pixels": self.supplied_mask_exclusion_pixels,
            "union_exclusion_pixels": self.union_exclusion_pixels,
            "excluded_source_ink_pixels": self.excluded_source_ink_pixels,
            "retained_source_ink_pixels": self.retained_source_ink_pixels,
            "thinning_iterations": self.thinning_iterations,
            "skeleton_pixels": self.skeleton_pixels,
            "isolated_skeleton_pixels": self.isolated_skeleton_pixels,
            "skeleton_edge_count": self.skeleton_edge_count,
            "traced_skeleton_edge_count": self.traced_skeleton_edge_count,
            "raw_path_count": self.raw_path_count,
            "raw_vertex_count": self.raw_vertex_count,
            "simplified_path_count": self.simplified_path_count,
            "simplification_fallback_path_count": (
                self.simplification_fallback_path_count
            ),
            "output_path_count": self.output_path_count,
            "output_vertex_count": self.output_vertex_count,
            "output_segment_count": self.output_segment_count,
            "replay_sample_count": self.replay_sample_count,
            "replay_off_ink_sample_count": self.replay_off_ink_sample_count,
            "simplification_tolerance_px": self.simplification_tolerance_px,
        }


@dataclass(frozen=True, slots=True)
class BinaryCentrelineExtraction:
    """Content-addressed output ready for a technical-record adapter."""

    source_path: Path
    source_sha256: str
    source_size_px: tuple[int, int]
    crop: PixelRect
    retained_ink_bbox: PixelRect
    ink_is_one: bool
    exclusion_rectangles: tuple[PixelRect, ...]
    supplied_exclusion_mask_sha256: str | None
    retained_ink_sha256: str
    skeleton_sha256: str
    paths: tuple[BinaryCentrelinePath, ...]
    stats: BinaryExtractionStats
    geometry_sha256: str
    extraction_sha256: str

    def record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "method": "pinned-one-bit-zhang-suen-centreline-v1",
            "source": {
                "path": str(self.source_path),
                "sha256": self.source_sha256,
                "size_px": list(self.source_size_px),
                "crop": self.crop.record(),
                "retained_ink_bbox_crop_px": self.retained_ink_bbox.record(),
                "ink_is_one": self.ink_is_one,
            },
            "exclusions": {
                "rectangles_source_px": [
                    rectangle.record() for rectangle in self.exclusion_rectangles
                ],
                "supplied_mask_sha256": self.supplied_exclusion_mask_sha256,
            },
            "retained_ink_sha256": self.retained_ink_sha256,
            "skeleton_sha256": self.skeleton_sha256,
            "statistics": self.stats.record(),
            "paths": [path.record() for path in self.paths],
            "geometry_sha256": self.geometry_sha256,
            "extraction_sha256": self.extraction_sha256,
        }


def sha256_file(path: Path) -> str:
    """Hash one local source without following metadata or external content."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise TechnicalRasterError(f"Cannot read raster source {path}: {exc}") from exc
    return digest.hexdigest()


def _expected_size(value: tuple[int, int]) -> tuple[int, int]:
    if not isinstance(value, tuple) or len(value) != 2:
        _fail("expected_size_px must be a (width, height) tuple.")
    width = _integer(value[0], "expected_size_px.width")
    height = _integer(value[1], "expected_size_px.height")
    if width <= 0 or height <= 0:
        _fail("Expected raster dimensions must be positive.")
    return width, height


def _pnm_tokens(payload: bytes) -> Iterator[bytes]:
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


def _read_ascii_pbm(payload: bytes) -> BoolMask:
    """Read strict P1 PBM for dependency-free tests and pinned ASCII sources."""

    tokens = list(_pnm_tokens(payload))
    if len(tokens) < 3 or tokens[0] != b"P1":
        _fail("ASCII binary raster must be a well-formed P1 PBM.")
    try:
        width, height = int(tokens[1]), int(tokens[2])
        samples = [int(value) for value in tokens[3:]]
    except ValueError as exc:
        raise TechnicalRasterError(
            "PBM dimensions and pixels must be integers."
        ) from exc
    if width <= 0 or height <= 0 or len(samples) != width * height:
        _fail("PBM dimensions or sample count are invalid.")
    if any(sample not in {0, 1} for sample in samples):
        _fail("PBM pixels must be exactly zero or one.")
    return np.asarray(samples, dtype=np.bool_).reshape((height, width))


def validate_crop_bounds(crop: PixelRect, source_size_px: tuple[int, int]) -> None:
    """Fail unless ``crop`` is wholly contained by the verified source."""

    width, height = _expected_size(source_size_px)
    if crop.right > width or crop.bottom > height:
        _fail(
            "Raster crop leaves the verified source bounds: "
            f"crop={crop.record()}, source={width}x{height}."
        )


def load_verified_binary_image(
    path: Path,
    *,
    expected_sha256: str,
    expected_size_px: tuple[int, int],
) -> tuple[BoolMask, str]:
    """Load exact mode-1 pixels after hash, mode and dimension validation."""

    if (
        not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        _fail("expected_sha256 must be one lowercase SHA-256 digest.")
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        _fail(
            "Raster source SHA-256 changed: "
            f"expected {expected_sha256}, got {actual_sha256}."
        )
    expected_width, expected_height = _expected_size(expected_size_px)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise TechnicalRasterError(f"Cannot read binary raster {path}: {exc}") from exc
    if payload.startswith(b"P1"):
        values = _read_ascii_pbm(payload)
        actual_height, actual_width = values.shape
        if (actual_width, actual_height) != (expected_width, expected_height):
            _fail(
                "Raster source dimensions changed: expected "
                f"{expected_width}x{expected_height}, got "
                f"{actual_width}x{actual_height}."
            )
        return values.copy(), actual_sha256
    if payload[:2] in {b"P2", b"P3", b"P5", b"P6"}:
        _fail(
            "Binary technical extraction accepts one-bit PBM or Pillow mode '1' "
            "only; thresholding a grayscale or colour source is a separate "
            "reviewed step."
        )
    try:
        image_module = import_module("PIL.Image")
        unidentified_error = import_module("PIL").UnidentifiedImageError
    except ModuleNotFoundError as exc:
        raise TechnicalRasterError(
            "PNG/TIFF one-bit sources require Pillow; strict ASCII P1 PBM works "
            "without it."
        ) from exc
    try:
        with image_module.open(path) as image:
            image.load()
            if image.mode != "1":
                _fail(
                    "Binary technical extraction accepts Pillow mode '1' only; "
                    "thresholding a grayscale or colour source is a separate reviewed step."
                )
            if image.size != (expected_width, expected_height):
                _fail(
                    "Raster source dimensions changed: expected "
                    f"{expected_width}x{expected_height}, got "
                    f"{image.width}x{image.height}."
                )
            values = np.asarray(image, dtype=np.bool_).copy()
    except (OSError, unidentified_error) as exc:
        raise TechnicalRasterError(
            f"Cannot decode binary raster {path}: {exc}"
        ) from exc
    return values, actual_sha256


def _validate_boolean_mask(
    value: BoolMask | Sequence[Sequence[bool]],
    *,
    expected_shape: tuple[int, int],
    label: str,
) -> BoolMask:
    array = np.asarray(value)
    if array.dtype != np.bool_:
        _fail(f"{label} must use boolean pixels; implicit thresholding is forbidden.")
    if array.ndim != 2 or array.shape != expected_shape:
        _fail(f"{label} must have crop shape {expected_shape}, got {array.shape}.")
    return np.asarray(array, dtype=np.bool_).copy()


def _mask_sha256(mask: BoolMask) -> str:
    height, width = mask.shape
    digest = hashlib.sha256()
    digest.update(f"binary-mask-v1:{width}x{height}:".encode("ascii"))
    digest.update(np.packbits(mask.reshape(-1), bitorder="big").tobytes())
    return digest.hexdigest()


def _zhang_suen(binary: BoolMask) -> tuple[BoolMask, int]:
    """Thin retained ink without adding or connecting a source pixel."""

    # A false border prevents numpy roll from treating opposite crop edges as
    # neighbours while leaving every source pixel in its original coordinate.
    image = np.pad(binary.astype(np.uint8), 1, constant_values=0)
    iterations = 0
    changed = True
    while changed:
        iterations += 1
        changed = False
        for phase in (0, 1):
            p2 = np.roll(image, 1, 0)
            p3 = np.roll(np.roll(image, 1, 0), -1, 1)
            p4 = np.roll(image, -1, 1)
            p5 = np.roll(np.roll(image, -1, 0), -1, 1)
            p6 = np.roll(image, -1, 0)
            p7 = np.roll(np.roll(image, -1, 0), 1, 1)
            p8 = np.roll(image, 1, 1)
            p9 = np.roll(np.roll(image, 1, 0), 1, 1)
            neighbours = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            transitions = sum(
                item.astype(np.uint8)
                for item in (
                    (p2 == 0) & (p3 == 1),
                    (p3 == 0) & (p4 == 1),
                    (p4 == 0) & (p5 == 1),
                    (p5 == 0) & (p6 == 1),
                    (p6 == 0) & (p7 == 1),
                    (p7 == 0) & (p8 == 1),
                    (p8 == 0) & (p9 == 1),
                    (p9 == 0) & (p2 == 1),
                )
            )
            base = (
                (image == 1)
                & (neighbours >= 2)
                & (neighbours <= 6)
                & (transitions == 1)
            )
            if phase == 0:
                remove = base & ((p2 * p4 * p6) == 0) & ((p4 * p6 * p8) == 0)
            else:
                remove = base & ((p2 * p4 * p8) == 0) & ((p2 * p6 * p8) == 0)
            remove[[0, -1], :] = False
            remove[:, [0, -1]] = False
            if np.any(remove):
                image[remove] = 0
                changed = True
    return image[1:-1, 1:-1].astype(np.bool_), iterations


def _edge(first: PixelPoint, second: PixelPoint) -> PixelEdge:
    return (first, second) if first < second else (second, first)


def _skeleton_adjacency(skeleton: BoolMask) -> dict[PixelPoint, list[PixelPoint]]:
    ys, xs = np.nonzero(skeleton)
    pixels = set(zip(xs.tolist(), ys.tolist()))
    offsets = (
        (-1, -1),
        (0, -1),
        (1, -1),
        (-1, 0),
        (1, 0),
        (-1, 1),
        (0, 1),
        (1, 1),
    )
    adjacency: dict[PixelPoint, list[PixelPoint]] = {}
    for point in sorted(pixels):
        neighbours: list[PixelPoint] = []
        for dx, dy in offsets:
            candidate = (point[0] + dx, point[1] + dy)
            if candidate not in pixels:
                continue
            # A diagonal touching an orthogonal neighbour is not a second
            # source edge.  This is the same no-corner-cut graph used by the
            # qualified bridge extractor.
            if (
                dx
                and dy
                and (
                    (point[0] + dx, point[1]) in pixels
                    or (point[0], point[1] + dy) in pixels
                )
            ):
                continue
            neighbours.append(candidate)
        adjacency[point] = sorted(neighbours)
    return adjacency


def _local_pairings(
    neighbours: tuple[PixelPoint, ...],
) -> list[tuple[tuple[tuple[PixelPoint, PixelPoint], ...], tuple[PixelPoint, ...]]]:
    if len(neighbours) < 2:
        return [((), neighbours)]
    first = neighbours[0]
    results: list[
        tuple[tuple[tuple[PixelPoint, PixelPoint], ...], tuple[PixelPoint, ...]]
    ] = []
    if len(neighbours) % 2:
        for pairs, unpaired in _local_pairings(neighbours[1:]):
            results.append((pairs, (first, *unpaired)))
    for index in range(1, len(neighbours)):
        second = neighbours[index]
        remaining = neighbours[1:index] + neighbours[index + 1 :]
        for pairs, unpaired in _local_pairings(remaining):
            results.append((((first, second), *pairs), unpaired))
    return results


def _pairing_score(
    point: PixelPoint, pairs: tuple[tuple[PixelPoint, PixelPoint], ...]
) -> float:
    score = 0.0
    for first, second in pairs:
        first_vector = (first[0] - point[0], first[1] - point[1])
        second_vector = (second[0] - point[0], second[1] - point[1])
        score -= (
            first_vector[0] * second_vector[0] + first_vector[1] * second_vector[1]
        ) / (math.hypot(*first_vector) * math.hypot(*second_vector))
    return score


def _graph_paths(
    skeleton: BoolMask,
) -> tuple[list[list[PixelPoint]], int, int, int]:
    """Chain every undirected skeleton edge exactly once."""

    adjacency = _skeleton_adjacency(skeleton)
    transitions: dict[PixelPoint, dict[PixelPoint, PixelPoint]] = {}
    for point, neighbours in adjacency.items():
        candidates = _local_pairings(tuple(neighbours))
        pairs, _ = max(
            candidates,
            key=lambda candidate: (_pairing_score(point, candidate[0]), candidate),
        )
        mapping: dict[PixelPoint, PixelPoint] = {}
        for first, second in pairs:
            mapping[first] = second
            mapping[second] = first
        transitions[point] = mapping

    unused = {
        _edge(point, neighbour)
        for point, neighbours in adjacency.items()
        for neighbour in neighbours
    }
    skeleton_edge_count = len(unused)
    isolated_pixels = sum(not neighbours for neighbours in adjacency.values())
    result: list[list[PixelPoint]] = []

    def trace(first: PixelPoint, second: PixelPoint) -> list[PixelPoint]:
        chain = [first, second]
        unused.remove(_edge(first, second))
        previous, current = first, second
        while previous in transitions[current]:
            following = transitions[current][previous]
            next_edge = _edge(current, following)
            if next_edge not in unused:
                break
            unused.remove(next_edge)
            chain.append(following)
            previous, current = current, following
        return chain

    starts: list[tuple[PixelPoint, PixelPoint]] = []
    for first, second in sorted(unused):
        if second not in transitions[first]:
            starts.append((first, second))
        if first not in transitions[second]:
            starts.append((second, first))
    for first, second in starts:
        if _edge(first, second) in unused:
            result.append(trace(first, second))
    while unused:
        first, second = min(unused)
        result.append(trace(first, second))
    if unused:  # pragma: no cover - guarded by the traversal above
        _fail(f"Skeleton chaining left {len(unused)} unused source edges.")

    traced_edge_count = sum(len(path) - 1 for path in result)
    if traced_edge_count != skeleton_edge_count:
        _fail(
            "Skeleton edge accounting failed: "
            f"expected {skeleton_edge_count}, traced {traced_edge_count}."
        )
    return result, skeleton_edge_count, traced_edge_count, isolated_pixels


def _distance_to_segment(
    point: PixelPoint, start: PixelPoint, end: PixelPoint
) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    squared = dx * dx + dy * dy
    if squared == 0:
        return math.dist(point, start)
    fraction = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / squared,
        ),
    )
    projection = (start[0] + fraction * dx, start[1] + fraction * dy)
    return math.dist(point, projection)


def _rdp_open(points: Sequence[PixelPoint], tolerance: float) -> list[PixelPoint]:
    if len(points) <= 2 or tolerance == 0:
        return list(points)
    retained = [False] * len(points)
    retained[0] = retained[-1] = True
    pending = [(0, len(points) - 1)]
    while pending:
        start_index, end_index = pending.pop()
        if end_index - start_index <= 1:
            continue
        distances = [
            _distance_to_segment(points[index], points[start_index], points[end_index])
            for index in range(start_index + 1, end_index)
        ]
        maximum = max(distances, default=0.0)
        if maximum <= tolerance:
            continue
        split = start_index + distances.index(maximum) + 1
        retained[split] = True
        pending.append((split, end_index))
        pending.append((start_index, split))
    return [point for point, keep in zip(points, retained, strict=True) if keep]


def _simplify_path(points: Sequence[PixelPoint], tolerance: float) -> list[PixelPoint]:
    if len(points) <= 2 or tolerance == 0:
        return list(points)
    if points[0] != points[-1]:
        return _rdp_open(points, tolerance)
    ring = list(points[:-1])
    if len(ring) < 3:
        return list(points)
    anchor = 0
    opposite = max(
        range(1, len(ring)),
        key=lambda index: (math.dist(ring[anchor], ring[index]), ring[index]),
    )
    first_arc = _rdp_open(ring[: opposite + 1], tolerance)
    second_arc = _rdp_open([*ring[opposite:], ring[0]], tolerance)
    return [*first_arc, *second_arc[1:]]


def _segment_samples(first: PixelPoint, second: PixelPoint) -> Iterator[PixelPoint]:
    distance = math.hypot(second[0] - first[0], second[1] - first[1])
    count = max(1, math.ceil(2.0 * distance))
    previous: PixelPoint | None = None
    for index in range(count + 1):
        fraction = index / count
        point = (
            int(math.floor(first[0] + (second[0] - first[0]) * fraction + 0.5)),
            int(math.floor(first[1] + (second[1] - first[1]) * fraction + 0.5)),
        )
        if point != previous:
            yield point
            previous = point


def audit_paths_on_source_ink(
    paths: Sequence[BinaryCentrelinePath], retained_ink: BoolMask
) -> SourceInkReplayAudit:
    """Sample every emitted segment back onto an explicit crop-local ink mask."""

    mask = _validate_boolean_mask(
        retained_ink,
        expected_shape=retained_ink.shape,
        label="retained_ink",
    )
    height, width = mask.shape
    sample_count = 0
    off_ink_count = 0
    failures: list[int] = []
    for path_index, path in enumerate(paths):
        path_failed = False
        for first, second in path.edges():
            for x, y in _segment_samples(first, second):
                sample_count += 1
                if x < 0 or y < 0 or x >= width or y >= height or not mask[y, x]:
                    off_ink_count += 1
                    path_failed = True
        if path_failed:
            failures.append(path_index)
    return SourceInkReplayAudit(
        sample_count=sample_count,
        off_ink_sample_count=off_ink_count,
        failed_path_indices=tuple(failures),
    )


def _canonical_path(points: Sequence[PixelPoint]) -> BinaryCentrelinePath:
    if len(points) < 2:
        _fail("Skeleton traversal produced a path with fewer than two points.")
    closed = len(points) > 2 and points[0] == points[-1]
    base = tuple(points[:-1] if closed else points)
    if not closed:
        reverse = tuple(reversed(base))
        return BinaryCentrelinePath(min(base, reverse), closed=False)

    minimum = min(base)
    candidates: list[tuple[PixelPoint, ...]] = []
    for sequence in (base, tuple(reversed(base))):
        for index, point in enumerate(sequence):
            if point == minimum:
                candidates.append(sequence[index:] + sequence[:index])
    return BinaryCentrelinePath(min(candidates), closed=True)


def geometry_sha256(paths: Sequence[BinaryCentrelinePath]) -> str:
    """Digest canonical centreline geometry without paths or timestamps."""

    payload = {
        "schema_version": 1,
        "kind": "binary-source-centrelines",
        "paths": [path.record() for path in paths],
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()


def extract_binary_centrelines(
    source_path: Path,
    *,
    expected_sha256: str,
    expected_size_px: tuple[int, int],
    crop: PixelRect,
    ink_is_one: bool,
    exclusion_rectangles: Iterable[PixelRect] = (),
    exclusion_mask: BoolMask | Sequence[Sequence[bool]] | None = None,
    simplification_tolerance_px: float = MAX_SIMPLIFICATION_TOLERANCE_PX,
) -> BinaryCentrelineExtraction:
    """Extract deterministic centrelines from a verified one-bit source crop.

    ``exclusion_rectangles`` are expressed in full-source coordinates.
    ``exclusion_mask`` is crop-local and uses ``True`` for pixels to remove.
    The caller must state the one-bit polarity; it is never inferred.
    """

    if type(ink_is_one) is not bool:
        _fail("ink_is_one must be explicitly true or false.")
    if (
        isinstance(simplification_tolerance_px, bool)
        or not isinstance(simplification_tolerance_px, (int, float))
        or not math.isfinite(float(simplification_tolerance_px))
    ):
        _fail("simplification_tolerance_px must be finite.")
    tolerance = float(simplification_tolerance_px)
    if not 0.0 <= tolerance <= MAX_SIMPLIFICATION_TOLERANCE_PX:
        _fail(
            "simplification_tolerance_px must be between 0 and "
            f"{MAX_SIMPLIFICATION_TOLERANCE_PX}."
        )

    source_size = _expected_size(expected_size_px)
    validate_crop_bounds(crop, source_size)
    source_bits, source_sha256 = load_verified_binary_image(
        source_path,
        expected_sha256=expected_sha256,
        expected_size_px=source_size,
    )
    crop_bits = source_bits[crop.y : crop.bottom, crop.x : crop.right]
    source_ink = crop_bits if ink_is_one else ~crop_bits
    source_ink = np.asarray(source_ink, dtype=np.bool_).copy()

    raw_rectangles = tuple(exclusion_rectangles)
    if any(not isinstance(rectangle, PixelRect) for rectangle in raw_rectangles):
        _fail("Every exclusion rectangle must be a PixelRect.")
    if len(set(raw_rectangles)) != len(raw_rectangles):
        _fail("Exclusion rectangles must not be repeated.")
    rectangles = tuple(sorted(raw_rectangles))
    rectangle_mask = np.zeros(source_ink.shape, dtype=np.bool_)
    for rectangle in rectangles:
        if (
            rectangle.x < crop.x
            or rectangle.y < crop.y
            or rectangle.right > crop.right
            or rectangle.bottom > crop.bottom
        ):
            _fail(
                "Exclusion rectangle leaves the source crop: "
                f"rectangle={rectangle.record()}, crop={crop.record()}."
            )
        left, top = rectangle.x - crop.x, rectangle.y - crop.y
        rectangle_mask[
            top : top + rectangle.height,
            left : left + rectangle.width,
        ] = True

    supplied_mask: BoolMask | None = None
    if exclusion_mask is not None:
        supplied_mask = _validate_boolean_mask(
            exclusion_mask,
            expected_shape=source_ink.shape,
            label="exclusion_mask",
        )
    union_exclusion = rectangle_mask.copy()
    if supplied_mask is not None:
        union_exclusion |= supplied_mask
    retained_ink = source_ink & ~union_exclusion
    if not np.any(retained_ink):
        _fail("The crop and exclusions retain no source ink.")
    retained_y, retained_x = np.nonzero(retained_ink)
    retained_bbox = PixelRect(
        int(retained_x.min()),
        int(retained_y.min()),
        int(retained_x.max() - retained_x.min() + 1),
        int(retained_y.max() - retained_y.min() + 1),
    )

    skeleton, thinning_iterations = _zhang_suen(retained_ink)
    raw_paths, edge_count, traced_edge_count, isolated_pixels = _graph_paths(skeleton)
    if edge_count == 0 or not raw_paths:
        _fail(
            "The retained source has no drawable skeleton edges; isolated pixels "
            "cannot become a pen path."
        )

    simplified_paths: list[BinaryCentrelinePath] = []
    simplified_count = 0
    fallback_count = 0
    raw_vertex_count = sum(len(path) for path in raw_paths)
    for raw_path in raw_paths:
        candidate = _simplify_path(raw_path, tolerance)
        candidate_path = _canonical_path(candidate)
        candidate_audit = audit_paths_on_source_ink([candidate_path], retained_ink)
        if not candidate_audit.passed:
            fallback_count += 1
            candidate_path = _canonical_path(raw_path)
            raw_audit = audit_paths_on_source_ink([candidate_path], retained_ink)
            if not raw_audit.passed:  # pragma: no cover - skeleton invariant
                _fail("An unsimplified skeleton edge failed source-ink replay.")
        elif len(candidate) < len(raw_path):
            simplified_count += 1
        simplified_paths.append(candidate_path)

    # Ordering is canonical, but paths are intentionally not de-duplicated.
    paths = tuple(sorted(simplified_paths, key=lambda item: (item.points, item.closed)))
    replay = audit_paths_on_source_ink(paths, retained_ink)
    if not replay.passed:
        _fail("Final binary centrelines failed retained source-ink replay.")

    stats = BinaryExtractionStats(
        source_ink_pixels=int(np.count_nonzero(source_ink)),
        rectangle_exclusion_pixels=int(np.count_nonzero(rectangle_mask)),
        supplied_mask_exclusion_pixels=(
            int(np.count_nonzero(supplied_mask)) if supplied_mask is not None else 0
        ),
        union_exclusion_pixels=int(np.count_nonzero(union_exclusion)),
        excluded_source_ink_pixels=int(np.count_nonzero(source_ink & union_exclusion)),
        retained_source_ink_pixels=int(np.count_nonzero(retained_ink)),
        thinning_iterations=thinning_iterations,
        skeleton_pixels=int(np.count_nonzero(skeleton)),
        isolated_skeleton_pixels=isolated_pixels,
        skeleton_edge_count=edge_count,
        traced_skeleton_edge_count=traced_edge_count,
        raw_path_count=len(raw_paths),
        raw_vertex_count=raw_vertex_count,
        simplified_path_count=simplified_count,
        simplification_fallback_path_count=fallback_count,
        output_path_count=len(paths),
        output_vertex_count=sum(len(path.points) for path in paths),
        output_segment_count=sum(1 for path in paths for _ in path.edges()),
        replay_sample_count=replay.sample_count,
        replay_off_ink_sample_count=replay.off_ink_sample_count,
        simplification_tolerance_px=tolerance,
    )
    geometry_digest = geometry_sha256(paths)
    supplied_mask_digest = (
        _mask_sha256(supplied_mask) if supplied_mask is not None else None
    )
    retained_digest = _mask_sha256(retained_ink)
    skeleton_digest = _mask_sha256(skeleton)
    digest_payload = {
        "schema_version": 1,
        "method": "pinned-one-bit-zhang-suen-centreline-v1",
        "source_sha256": source_sha256,
        "source_size_px": list(source_size),
        "crop": crop.record(),
        "retained_ink_bbox_crop_px": retained_bbox.record(),
        "ink_is_one": ink_is_one,
        "exclusion_rectangles": [item.record() for item in rectangles],
        "supplied_exclusion_mask_sha256": supplied_mask_digest,
        "retained_ink_sha256": retained_digest,
        "skeleton_sha256": skeleton_digest,
        "statistics": stats.record(),
        "geometry_sha256": geometry_digest,
    }
    extraction_digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    return BinaryCentrelineExtraction(
        source_path=source_path,
        source_sha256=source_sha256,
        source_size_px=source_size,
        crop=crop,
        retained_ink_bbox=retained_bbox,
        ink_is_one=ink_is_one,
        exclusion_rectangles=rectangles,
        supplied_exclusion_mask_sha256=supplied_mask_digest,
        retained_ink_sha256=retained_digest,
        skeleton_sha256=skeleton_digest,
        paths=paths,
        stats=stats,
        geometry_sha256=geometry_digest,
        extraction_sha256=extraction_digest,
    )


__all__ = [
    "BinaryCentrelineExtraction",
    "BinaryCentrelinePath",
    "BinaryExtractionStats",
    "BoolMask",
    "MAX_SIMPLIFICATION_TOLERANCE_PX",
    "PixelRect",
    "SourceInkReplayAudit",
    "TechnicalRasterError",
    "audit_paths_on_source_ink",
    "extract_binary_centrelines",
    "geometry_sha256",
    "load_verified_binary_image",
    "sha256_file",
    "validate_crop_bounds",
]

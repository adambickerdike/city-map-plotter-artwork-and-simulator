"""Original, deterministic abstract artwork for physical pen plotting.

The catalog describes twenty-five compositions, but it does not contain traced
or imported artwork.  Every mark is regenerated here from a named mathematical
grammar and a pinned seed, then fitted directly to the selected plate's physical
map field.  The result is deliberately centreline-only geometry: colour and
weight come from real studio pens rather than SVG fills or simulated brushes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib import resources
import json
import math
import random
from typing import Any, Callable, Sequence

from .models import MapPlotterError
from .niche_common import (
    ArtworkLayer,
    PEN_ORDER,
    PENS_BY_ID,
    PlateArtwork,
    Point,
    Rect,
    Stroke,
    circle_stroke,
    context_for,
    ellipse_stroke,
    polyline_length_mm,
)


CATALOG_RESOURCE = "data/abstract-art-v1.json"
ALGORITHM_VERSION = 1

ALGORITHMS = (
    "curl-field",
    "magnetic-dipoles",
    "folded-horizon",
    "chromatic-shear",
    "braided-silence",
    "coral-growth",
    "cellular-cathedral",
    "reaction-garden",
    "fault-bloom",
    "foam-eclipse",
    "lissajous-choir",
    "prime-rain",
    "resonance-contours",
    "torus-knot",
    "moire-reliquary",
    "gravity-loom",
    "event-horizon",
    "ghost-truchet",
    "chromatic-misregister",
    "phase-collapse",
    "face-tidal-witness",
    "face-profile-crosswind",
    "face-double-exposure",
    "face-frequency-veil",
    "face-nocturnal-chorus",
)

FACE_ALGORITHMS = frozenset(value for value in ALGORITHMS if value.startswith("face-"))
_PARAMETER_KEYS: dict[str, frozenset[str]] = {
    "curl-field": frozenset({"streamline_count", "step_mm", "stock_tone"}),
    "magnetic-dipoles": frozenset({"trace_count", "step_mm", "stock_tone"}),
    "folded-horizon": frozenset({"line_count", "stock_tone"}),
    "chromatic-shear": frozenset({"curve_count", "stock_tone"}),
    "braided-silence": frozenset({"strand_count", "stock_tone"}),
    "coral-growth": frozenset({"colony_count", "rings_per_colony", "stock_tone"}),
    "cellular-cathedral": frozenset({"site_count", "nested_depth", "stock_tone"}),
    "reaction-garden": frozenset({"contour_levels", "stock_tone"}),
    "fault-bloom": frozenset({"ray_count", "echo_count", "stock_tone"}),
    "foam-eclipse": frozenset({"circle_count", "stock_tone"}),
    "lissajous-choir": frozenset({"curve_count", "stock_tone"}),
    "prime-rain": frozenset({"walk_count", "horizon_count", "stock_tone"}),
    "resonance-contours": frozenset({"contour_levels", "stock_tone"}),
    "torus-knot": frozenset({"torus_p", "torus_q", "offset_count", "stock_tone"}),
    "moire-reliquary": frozenset({"ring_count", "spoke_count", "stock_tone"}),
    "gravity-loom": frozenset({"particle_count", "integration_steps", "stock_tone"}),
    "event-horizon": frozenset({"ray_count", "stock_tone"}),
    "ghost-truchet": frozenset(
        {"grid_columns", "grid_rows", "dropout_ratio", "stock_tone"}
    ),
    "chromatic-misregister": frozenset({"contour_levels", "offsets_mm", "stock_tone"}),
    "phase-collapse": frozenset({"wave_count", "stock_tone"}),
    "face-tidal-witness": frozenset({"scanline_count", "stock_tone"}),
    "face-profile-crosswind": frozenset({"trace_count", "stock_tone"}),
    "face-double-exposure": frozenset({"lines_per_profile", "stock_tone"}),
    "face-frequency-veil": frozenset({"scanline_count", "stock_tone"}),
    "face-nocturnal-chorus": frozenset(
        {
            "face_count",
            "streamlines_per_face",
            "binder_wave_count",
            "stock_tone",
        }
    ),
}
_CATALOG_KEYS = {
    "id",
    "title",
    "subtitle",
    "algorithm",
    "seed",
    "format_id",
    "palette",
    "statement",
    "parameters",
}


@dataclass(frozen=True)
class AbstractPiece:
    """One validated recipe in the pinned abstract-art catalog."""

    id: str
    title: str
    subtitle: str
    algorithm: str
    seed: int
    format_id: str
    palette: tuple[str, ...]
    statement: str
    parameters: dict[str, Any]
    catalog_index: int

    @property
    def is_face(self) -> bool:
        return self.algorithm in FACE_ALGORITHMS

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "subtitle": self.subtitle,
            "algorithm": self.algorithm,
            "seed": self.seed,
            "format_id": self.format_id,
            "palette": list(self.palette),
            "statement": self.statement,
            "parameters": dict(self.parameters),
        }


def _catalog_error(message: str) -> MapPlotterError:
    return MapPlotterError(f"Abstract-art catalog is invalid: {message}")


def _validate_parameters(
    piece_id: str,
    algorithm: str,
    parameters: object,
    palette_size: int,
) -> dict[str, Any]:
    """Reject unknown, non-finite, or physically unreasonable recipe values."""

    if not isinstance(parameters, dict):
        raise _catalog_error(f"piece {piece_id!r} parameters must be an object")
    expected = _PARAMETER_KEYS[algorithm]
    if set(parameters) != expected:
        raise _catalog_error(
            f"piece {piece_id!r} parameters must contain exactly {sorted(expected)}"
        )
    if parameters.get("stock_tone") != "light":
        raise _catalog_error(
            f"piece {piece_id!r} stock_tone must be 'light' for this inventory"
        )
    integer_names = expected - {
        "stock_tone",
        "step_mm",
        "dropout_ratio",
        "offsets_mm",
    }
    for name in integer_names:
        value = parameters[name]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= 5000
        ):
            raise _catalog_error(
                f"piece {piece_id!r} parameter {name!r} must be an integer from 1 to 5000"
            )
    if "step_mm" in expected:
        value = parameters["step_mm"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.05 <= float(value) <= 10.0
        ):
            raise _catalog_error(
                f"piece {piece_id!r} step_mm must be finite and between 0.05 and 10"
            )
    if "dropout_ratio" in expected:
        value = parameters["dropout_ratio"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) < 0.8
        ):
            raise _catalog_error(
                f"piece {piece_id!r} dropout_ratio must be finite in [0, 0.8)"
            )
    if "offsets_mm" in expected:
        offsets = parameters["offsets_mm"]
        if (
            not isinstance(offsets, list)
            or len(offsets) != palette_size
            or any(
                not isinstance(offset, list)
                or len(offset) != 2
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or abs(float(value)) > 5.0
                    for value in offset
                )
                for offset in offsets
            )
        ):
            raise _catalog_error(
                f"piece {piece_id!r} offsets_mm must provide one finite <=5 mm pair per palette pen"
            )
    return dict(parameters)


@lru_cache(maxsize=1)
def load_abstract_catalog() -> tuple[AbstractPiece, ...]:
    """Load and strictly validate the bundled twenty-five-piece collection."""

    resource = resources.files("city_map_plotter").joinpath(CATALOG_RESOURCE)
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _catalog_error(str(exc)) from exc
    if not isinstance(payload, dict):
        raise _catalog_error("the root must be an object")
    if payload.get("schema_version") != 1:
        raise _catalog_error("schema_version must equal 1")
    if payload.get("catalog_id") != "abstract-art-v1":
        raise _catalog_error("catalog_id must equal 'abstract-art-v1'")
    records = payload.get("pieces")
    if not isinstance(records, list) or len(records) != 25:
        raise _catalog_error("pieces must contain exactly 25 records")

    pieces: list[AbstractPiece] = []
    ids: set[str] = set()
    algorithms: set[str] = set()
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict) or set(record) != _CATALOG_KEYS:
            raise _catalog_error(
                f"piece {index} must contain exactly {sorted(_CATALOG_KEYS)}"
            )
        piece_id = record["id"]
        if (
            not isinstance(piece_id, str)
            or not piece_id
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                for character in piece_id
            )
        ):
            raise _catalog_error(f"piece {index} has a non-file-safe id")
        if piece_id in ids:
            raise _catalog_error(f"piece id {piece_id!r} is repeated")
        algorithm = record["algorithm"]
        if algorithm not in ALGORITHMS:
            raise _catalog_error(
                f"piece {piece_id!r} has unknown algorithm {algorithm!r}"
            )
        if algorithm in algorithms:
            raise _catalog_error(f"algorithm {algorithm!r} is repeated")
        if not isinstance(record["seed"], int) or isinstance(record["seed"], bool):
            raise _catalog_error(f"piece {piece_id!r} seed must be an integer")
        if record["format_id"] not in {"a3-portrait", "a3-landscape"}:
            raise _catalog_error(f"piece {piece_id!r} must use an A3 plate")
        palette = record["palette"]
        if (
            not isinstance(palette, list)
            or not 2 <= len(palette) <= 5
            or len(set(palette)) != len(palette)
            or any(pen_id not in PENS_BY_ID for pen_id in palette)
            or any(pen_id not in PEN_ORDER for pen_id in palette)
        ):
            raise _catalog_error(f"piece {piece_id!r} has an invalid physical palette")
        for key in ("title", "subtitle", "statement"):
            if not isinstance(record[key], str) or not record[key].strip():
                raise _catalog_error(f"piece {piece_id!r} needs non-empty {key}")
        parameters = _validate_parameters(
            piece_id,
            algorithm,
            record["parameters"],
            len(palette),
        )
        pieces.append(
            AbstractPiece(
                id=piece_id,
                title=record["title"].strip(),
                subtitle=record["subtitle"].strip(),
                algorithm=algorithm,
                seed=record["seed"],
                format_id=record["format_id"],
                palette=tuple(palette),
                statement=record["statement"].strip(),
                parameters=parameters,
                catalog_index=index,
            )
        )
        ids.add(piece_id)
        algorithms.add(algorithm)
    if tuple(piece.algorithm for piece in pieces) != ALGORITHMS:
        raise _catalog_error("piece order must follow the canonical algorithm sequence")
    return tuple(pieces)


def list_abstract_pieces() -> list[dict[str, Any]]:
    return [piece.as_dict() for piece in load_abstract_catalog()]


def _piece_by_id(piece_id: str) -> AbstractPiece:
    for piece in load_abstract_catalog():
        if piece.id == piece_id:
            return piece
    raise MapPlotterError(f"Unknown abstract-art piece {piece_id!r}.")


def _point(rect: Rect, u: float, v: float) -> Point:
    return (rect.x + rect.width * u, rect.y + rect.height * v)


def _inside(rect: Rect, point: Point, epsilon: float = 1e-9) -> bool:
    return (
        rect.left - epsilon <= point[0] <= rect.right + epsilon
        and rect.top - epsilon <= point[1] <= rect.bottom + epsilon
    )


def _clip_segment(
    rect: Rect, first: Point, second: Point
) -> tuple[Point, Point] | None:
    """Liang-Barsky clip one line segment to ``rect``."""

    x0, y0 = first
    dx, dy = second[0] - x0, second[1] - y0
    p = (-dx, dx, -dy, dy)
    q = (x0 - rect.left, rect.right - x0, y0 - rect.top, rect.bottom - y0)
    low, high = 0.0, 1.0
    for denominator, numerator in zip(p, q):
        if abs(denominator) <= 1e-15:
            if numerator < 0:
                return None
            continue
        ratio = numerator / denominator
        if denominator < 0:
            low = max(low, ratio)
        else:
            high = min(high, ratio)
        if low > high:
            return None
    return (
        (x0 + low * dx, y0 + low * dy),
        (x0 + high * dx, y0 + high * dy),
    )


def _clip_polyline(rect: Rect, points: Sequence[Point]) -> list[Stroke]:
    """Clip a sampled polyline and preserve each contiguous in-field run."""

    if len(points) < 2:
        return []
    runs: list[Stroke] = []
    current: Stroke = []
    for first, second in zip(points, points[1:]):
        clipped = _clip_segment(rect, first, second)
        if clipped is None:
            if len(current) >= 2:
                runs.append(current)
            current = []
            continue
        start, end = clipped
        if (
            current
            and math.hypot(current[-1][0] - start[0], current[-1][1] - start[1]) <= 1e-6
        ):
            if end != current[-1]:
                current.append(end)
        else:
            if len(current) >= 2:
                runs.append(current)
            current = [start, end]
        if not _inside(rect, second):
            if len(current) >= 2:
                runs.append(current)
            current = []
    if len(current) >= 2:
        runs.append(current)
    return runs


def _split_by_mask(
    points: Sequence[Point], include: Callable[[Point], bool]
) -> list[Stroke]:
    runs: list[Stroke] = []
    current: Stroke = []
    for point in points:
        if include(point):
            current.append(point)
        else:
            if len(current) >= 2:
                runs.append(current)
            current = []
    if len(current) >= 2:
        runs.append(current)
    return runs


def _warped_ring(
    centre: Point,
    radius_x: float,
    radius_y: float,
    *,
    phase: float,
    harmonics: Sequence[tuple[int, float]],
    samples: int = 180,
) -> Stroke:
    values: Stroke = []
    for index in range(samples):
        angle = 2.0 * math.pi * index / samples
        ripple = 1.0 + sum(
            amplitude * math.sin(order * angle + phase * (order + 1))
            for order, amplitude in harmonics
        )
        values.append(
            (
                centre[0] + radius_x * ripple * math.cos(angle),
                centre[1] + radius_y * ripple * math.sin(angle),
            )
        )
    return [*values, values[0]]


def _integrate_field(
    rect: Rect,
    start: Point,
    field: Callable[[float, float, int], Point],
    *,
    step_mm: float,
    steps: int,
    direction: float = 1.0,
) -> Stroke:
    points: Stroke = [start]
    x, y = start
    for index in range(steps):
        vx, vy = field(x, y, index)
        magnitude = math.hypot(vx, vy)
        if not math.isfinite(magnitude) or magnitude <= 1e-12:
            break
        next_point = (
            x + direction * step_mm * vx / magnitude,
            y + direction * step_mm * vy / magnitude,
        )
        if not _inside(rect, next_point):
            clipped = _clip_segment(rect, (x, y), next_point)
            if clipped is not None and clipped[1] != points[-1]:
                points.append(clipped[1])
            break
        points.append(next_point)
        x, y = next_point
    return points


def _complete_streamline(
    rect: Rect,
    start: Point,
    field: Callable[[float, float, int], Point],
    *,
    step_mm: float = 1.35,
    steps: int = 150,
) -> Stroke:
    backward = _integrate_field(
        rect, start, field, step_mm=step_mm, steps=steps // 2, direction=-1.0
    )
    forward = _integrate_field(
        rect, start, field, step_mm=step_mm, steps=steps // 2, direction=1.0
    )
    return [*reversed(backward[1:]), *forward]


def _contour_paths(
    function: Callable[[float, float], float],
    rect: Rect,
    levels: Sequence[float],
    *,
    nx: int = 58,
    ny: int = 58,
) -> list[tuple[int, Stroke]]:
    """Extract stitched line contours without introducing a raster asset."""

    xs = [rect.left + rect.width * index / nx for index in range(nx + 1)]
    ys = [rect.top + rect.height * index / ny for index in range(ny + 1)]
    values = [[function(x, y) for x in xs] for y in ys]
    output: list[tuple[int, Stroke]] = []
    for level_index, level in enumerate(levels):
        segments: list[tuple[Point, Point]] = []
        for row in range(ny):
            for column in range(nx):
                corners = (
                    ((xs[column], ys[row]), values[row][column]),
                    ((xs[column + 1], ys[row]), values[row][column + 1]),
                    ((xs[column + 1], ys[row + 1]), values[row + 1][column + 1]),
                    ((xs[column], ys[row + 1]), values[row + 1][column]),
                )
                crossings: list[tuple[int, Point]] = []
                for edge, (first_index, second_index) in enumerate(
                    ((0, 1), (1, 2), (2, 3), (3, 0))
                ):
                    first_point, first_value = corners[first_index]
                    second_point, second_value = corners[second_index]
                    first_above = first_value >= level
                    second_above = second_value >= level
                    if first_above == second_above:
                        continue
                    denominator = second_value - first_value
                    ratio = (
                        0.5
                        if abs(denominator) <= 1e-15
                        else (level - first_value) / denominator
                    )
                    crossings.append(
                        (
                            edge,
                            (
                                first_point[0]
                                + ratio * (second_point[0] - first_point[0]),
                                first_point[1]
                                + ratio * (second_point[1] - first_point[1]),
                            ),
                        )
                    )
                if len(crossings) == 2:
                    segments.append((crossings[0][1], crossings[1][1]))
                elif len(crossings) == 4:
                    centre_value = sum(value for _, value in corners) / 4.0
                    by_edge = {edge: point for edge, point in crossings}
                    pattern = sum(
                        (1 << corner_index)
                        for corner_index, (_, value) in enumerate(corners)
                        if value >= level
                    )
                    isolate_zero_and_two = (
                        pattern == 0b0101 and centre_value >= level
                    ) or (pattern == 0b1010 and centre_value < level)
                    pairs = (
                        ((0, 1), (2, 3)) if isolate_zero_and_two else ((0, 3), (1, 2))
                    )
                    for first_edge, second_edge in pairs:
                        segments.append((by_edge[first_edge], by_edge[second_edge]))
        output.extend((level_index, path) for path in _stitch_segments(segments))
    return output


def _stitch_segments(segments: Sequence[tuple[Point, Point]]) -> list[Stroke]:
    if not segments:
        return []

    def key(point: Point) -> tuple[int, int]:
        return (round(point[0] * 10000), round(point[1] * 10000))

    endpoint_map: dict[tuple[int, int], set[int]] = {}
    for index, segment in enumerate(segments):
        endpoint_map.setdefault(key(segment[0]), set()).add(index)
        endpoint_map.setdefault(key(segment[1]), set()).add(index)
    unused = set(range(len(segments)))
    result: list[Stroke] = []
    while unused:
        initial = unused.pop()
        path = [segments[initial][0], segments[initial][1]]
        for at_front in (False, True):
            while True:
                endpoint = path[0] if at_front else path[-1]
                candidates = endpoint_map.get(key(endpoint), set()) & unused
                if not candidates:
                    break
                selected = min(candidates)
                unused.remove(selected)
                first, second = segments[selected]
                other = second if key(first) == key(endpoint) else first
                if at_front:
                    path.insert(0, other)
                else:
                    path.append(other)
        result.append(path)
    return result


def _offset_polyline(points: Sequence[Point], offset_mm: float) -> Stroke:
    if len(points) < 2:
        return list(points)
    result: Stroke = []
    for index, point in enumerate(points):
        previous = points[max(0, index - 1)]
        following = points[min(len(points) - 1, index + 1)]
        dx = following[0] - previous[0]
        dy = following[1] - previous[1]
        magnitude = max(math.hypot(dx, dy), 1e-12)
        result.append(
            (
                point[0] - dy * offset_mm / magnitude,
                point[1] + dx * offset_mm / magnitude,
            )
        )
    return result


def _ellipse_value(
    point: Point, centre: Point, radius_x: float, radius_y: float
) -> float:
    return ((point[0] - centre[0]) / radius_x) ** 2 + (
        (point[1] - centre[1]) / radius_y
    ) ** 2


def _generator_curl_field(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    centres = [
        (*_point(rect, 0.28, 0.34), 1.35),
        (*_point(rect, 0.70, 0.40), -1.15),
        (*_point(rect, 0.52, 0.72), 0.92),
    ]

    def field(x: float, y: float, step: int) -> Point:
        vx, vy = 0.44, 0.06 * math.sin(step * 0.08)
        for cx, cy, strength in centres:
            dx, dy = x - cx, y - cy
            radius = dx * dx + dy * dy + 90.0
            vx += -strength * dy * 100.0 / radius
            vy += strength * dx * 100.0 / radius
        return vx, vy

    output = {pen: [] for pen in palette}
    requested = max(24, int(parameters.get("streamline_count", 130)))
    columns = max(4, round(math.sqrt(requested * rect.width / rect.height)))
    rows = math.ceil(requested / columns)
    step_mm = float(parameters.get("step_mm", 1.25))
    emitted = 0
    for row in range(rows):
        for column in range(columns):
            if emitted >= requested:
                break
            start = _point(
                rect,
                (column + 0.45 + rng.uniform(-0.15, 0.15)) / columns,
                (row + 0.5 + rng.uniform(-0.18, 0.18)) / rows,
            )
            stroke = _complete_streamline(
                rect, start, field, step_mm=step_mm, steps=180
            )
            output[palette[(row + 2 * column) % len(palette)]].append(stroke)
            emitted += 1
    return output


def _generator_magnetic_dipoles(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    poles = [
        (*_point(rect, 0.31, 0.48), 1.0),
        (*_point(rect, 0.69, 0.52), -1.0),
        (*_point(rect, 0.52, 0.24), 0.55),
    ]

    def field(x: float, y: float, __: int) -> Point:
        vx = vy = 0.0
        for px, py, charge in poles:
            dx, dy = x - px, y - py
            scale = charge / max((dx * dx + dy * dy) ** 1.5, 35.0)
            vx += dx * scale
            vy += dy * scale
        return vx, vy

    output = {pen: [] for pen in palette}
    trace_count = max(24, int(parameters.get("trace_count", 126)))
    step_mm = float(parameters.get("step_mm", 1.1))
    for index in range(trace_count):
        angle = 2 * math.pi * index / trace_count + rng.uniform(-0.015, 0.015)
        pole = poles[index % len(poles)]
        start = (pole[0] + 9.0 * math.cos(angle), pole[1] + 9.0 * math.sin(angle))
        stroke = _complete_streamline(rect, start, field, step_mm=step_mm, steps=210)
        runs = _split_by_mask(
            stroke,
            lambda point: all(
                math.hypot(point[0] - p[0], point[1] - p[1]) > 4.0 for p in poles
            ),
        )
        output[palette[index % len(palette)]].extend(runs)
    for index, pole in enumerate(poles):
        for radius in (2.4, 4.1, 6.0):
            output[palette[(index + 1) % len(palette)]].append(
                circle_stroke((pole[0], pole[1]), radius, segments=48)
            )
    return output


def _generator_folded_horizon(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    folds = [(0.22, 0.10, 0.12), (0.50, -0.15, 0.075), (0.76, 0.12, 0.11)]
    rows = max(24, int(parameters.get("line_count", 118)))
    for row in range(rows):
        base = (row + 0.5) / rows
        points: Stroke = []
        phase = rng.uniform(-0.18, 0.18)
        for sample in range(241):
            u = sample / 240
            displacement = 0.012 * math.sin(10 * math.pi * u + phase + row * 0.035)
            for centre, amplitude, width in folds:
                distance = (u - centre) / width
                displacement += (
                    amplitude
                    * math.exp(-distance * distance)
                    * math.sin(math.pi * (base + u))
                )
            crease = (
                0.035
                * math.tanh((u - 0.57) * 42.0)
                * math.exp(-(((base - 0.58) / 0.27) ** 2))
            )
            points.append(_point(rect, u, base + displacement + crease))
        for run in _clip_polyline(rect, points):
            output[palette[(row // 9) % len(palette)]].append(run)
    return output


def _generator_chromatic_shear(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    curve_count = max(24, int(parameters.get("curve_count", 108)))
    for index in range(curve_count):
        band = index / max(curve_count - 1, 1)
        phase = 2 * math.pi * band + rng.uniform(-0.05, 0.05)
        points: Stroke = []
        for sample in range(190):
            t = sample / 189
            u = t + 0.12 * math.sin(math.pi * t) * math.sin(phase + 4.5 * math.pi * t)
            v = (
                band
                + 0.24 * (t - 0.5) * math.sin(math.pi * band)
                + 0.045 * math.sin(8 * math.pi * t + phase)
            )
            points.append(_point(rect, u, v))
        output[palette[index % len(palette)]].extend(_clip_polyline(rect, points))
    return output


def _generator_braided_silence(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    per_family = max(12, int(parameters.get("strand_count", 124)) // 2)
    for family in range(2):
        for index in range(per_family):
            position = (index + 0.5) / per_family
            points: Stroke = []
            for sample in range(210):
                t = sample / 209
                if family == 0:
                    u = t
                    v = position + 0.052 * math.sin(8 * math.pi * t + position * 11)
                else:
                    u = position + 0.052 * math.cos(8 * math.pi * t + position * 11)
                    v = t
                points.append(_point(rect, u, v))
            period = 13 + (index % 5)
            runs: list[Stroke] = []
            current: Stroke = []
            for sample, point in enumerate(points):
                over = ((sample // period) + index + family) % 2 == family
                keep = over or sample % period > 4
                if keep and _inside(rect, point):
                    current.append(point)
                else:
                    if len(current) >= 2:
                        runs.append(current)
                    current = []
            if len(current) >= 2:
                runs.append(current)
            output[palette[(index + family) % len(palette)]].extend(runs)
    return output


def _generator_coral_growth(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    colonies = [
        (_point(rect, 0.30, 0.42), 0.28),
        (_point(rect, 0.68, 0.57), 0.32),
        (_point(rect, 0.52, 0.30), 0.18),
    ]
    colonies = colonies[
        : max(1, min(len(colonies), int(parameters.get("colony_count", 3))))
    ]
    rings_per_colony = max(12, int(parameters.get("rings_per_colony", 41)))
    for colony_index, (centre, relative_radius) in enumerate(colonies):
        maximum = min(rect.width, rect.height) * relative_radius
        phases = [rng.uniform(0, 2 * math.pi) for _ in range(4)]
        for ring_index in range(1, rings_per_colony + 1):
            radius = maximum * ring_index / (rings_per_colony + 1)
            ring = _warped_ring(
                centre,
                radius,
                radius * (0.82 + 0.06 * math.sin(ring_index * 0.4)),
                phase=phases[ring_index % len(phases)] + ring_index * 0.07,
                harmonics=((3, 0.035), (5, 0.024), (8, 0.015)),
                samples=210,
            )
            mask_phase = (ring_index + colony_index) % 7
            runs = _split_by_mask(
                ring,
                lambda point, c=centre, phase=mask_phase: (
                    int(
                        (math.atan2(point[1] - c[1], point[0] - c[0]) + math.pi)
                        * 10
                        / math.pi
                    )
                    + phase
                )
                % 11
                != 0,
            )
            for run in runs:
                output[palette[(ring_index + colony_index) % len(palette)]].extend(
                    _clip_polyline(rect, run)
                )
    return output


def _clip_polygon_halfplane(
    polygon: Sequence[Point], nx: float, ny: float, limit: float
) -> Stroke:
    if not polygon:
        return []
    result: Stroke = []
    for first, second in zip(polygon, [*polygon[1:], polygon[0]]):
        first_value = nx * first[0] + ny * first[1] - limit
        second_value = nx * second[0] + ny * second[1] - limit
        first_inside = first_value <= 1e-9
        second_inside = second_value <= 1e-9
        if first_inside:
            result.append(first)
        if first_inside != second_inside:
            ratio = first_value / (first_value - second_value)
            result.append(
                (
                    first[0] + ratio * (second[0] - first[0]),
                    first[1] + ratio * (second[1] - first[1]),
                )
            )
    return result


def _generator_cellular_cathedral(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    sites: list[Point] = []
    attempts = 0
    site_count = max(12, int(parameters.get("site_count", 40)))
    minimum_spacing = min(rect.width, rect.height) * max(
        0.035, 0.66 / math.sqrt(site_count)
    )
    while len(sites) < site_count and attempts < 20000:
        attempts += 1
        candidate = _point(rect, rng.uniform(0.025, 0.975), rng.uniform(0.025, 0.975))
        if all(
            math.hypot(candidate[0] - x, candidate[1] - y) >= minimum_spacing
            for x, y in sites
        ):
            sites.append(candidate)
    output = {pen: [] for pen in palette}
    bounds = [
        (rect.left, rect.top),
        (rect.right, rect.top),
        (rect.right, rect.bottom),
        (rect.left, rect.bottom),
    ]
    for site_index, site in enumerate(sites):
        polygon: Stroke = list(bounds)
        for other in sites:
            if other == site:
                continue
            nx, ny = other[0] - site[0], other[1] - site[1]
            limit = (other[0] ** 2 + other[1] ** 2 - site[0] ** 2 - site[1] ** 2) / 2.0
            polygon = _clip_polygon_halfplane(polygon, nx, ny, limit)
            if not polygon:
                break
        if len(polygon) < 3:
            continue
        centre = (
            sum(point[0] for point in polygon) / len(polygon),
            sum(point[1] for point in polygon) / len(polygon),
        )
        depth_count = max(1, int(parameters.get("nested_depth", 3)))
        scales = [
            0.96 - depth * 0.22
            for depth in range(depth_count)
            if 0.96 - depth * 0.22 > 0.18
        ]
        for depth, scale in enumerate(scales):
            inset = [
                (
                    centre[0] + scale * (point[0] - centre[0]),
                    centre[1] + scale * (point[1] - centre[1]),
                )
                for point in polygon
            ]
            output[palette[(site_index + depth) % len(palette)]].append(
                [*inset, inset[0]]
            )
    return output


def _generator_reaction_garden(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    phase = rng.uniform(0, 2 * math.pi)

    def scalar(x: float, y: float) -> float:
        u = (x - rect.left) / rect.width
        v = (y - rect.top) / rect.height
        seed = math.sin(15.0 * u + 4.2 * math.sin(9.0 * v + phase))
        seed += 0.8 * math.cos(17.0 * v - 3.4 * math.sin(7.0 * u - phase))
        seed += 0.45 * math.sin(26.0 * (u + v) + 2.0 * math.sin(8.0 * (u - v)))
        return math.tanh(seed)

    output = {pen: [] for pen in palette}
    level_count = max(7, int(parameters.get("contour_levels", 17)))
    levels = [
        -0.88 + index * 1.76 / max(level_count - 1, 1) for index in range(level_count)
    ]
    for level_index, stroke in _contour_paths(scalar, rect, levels, nx=70, ny=70):
        output[palette[level_index % len(palette)]].append(stroke)
    return output


def _generator_fault_bloom(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    centre = _point(rect, 0.49, 0.52)
    output = {pen: [] for pen in palette}
    ray_count = max(24, int(parameters.get("ray_count", 92)))
    for ray in range(ray_count):
        angle = 2 * math.pi * ray / ray_count + rng.uniform(-0.018, 0.018)
        points = [centre]
        for step in range(1, 85):
            radius = step * min(rect.width, rect.height) / 72
            bend = 0.04 * math.sin(step * 0.22 + ray * 1.7)
            point = (
                centre[0] + radius * math.cos(angle + bend),
                centre[1] + 0.88 * radius * math.sin(angle + bend),
            )
            points.append(point)
            if not _inside(rect, point):
                break
        output[palette[ray % len(palette)]].extend(_clip_polyline(rect, points))
    echo_count = max(8, int(parameters.get("echo_count", 53)))
    for ring_index in range(1, echo_count + 1):
        ring = _warped_ring(
            centre,
            ring_index * rect.width / 105,
            ring_index * rect.height / 112,
            phase=ring_index * 0.09,
            harmonics=((5, 0.025), (11, 0.012)),
            samples=220,
        )
        runs = _split_by_mask(
            ring,
            lambda point, i=ring_index: (
                int(
                    (math.atan2(point[1] - centre[1], point[0] - centre[0]) + math.pi)
                    * 18
                )
                + i
            )
            % 17
            > 1,
        )
        for run in runs:
            output[palette[(ring_index + 1) % len(palette)]].extend(
                _clip_polyline(rect, run)
            )
    return output


def _generator_foam_eclipse(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    centre = _point(rect, 0.56, 0.48)
    eclipse_radius = min(rect.width, rect.height) * 0.19
    circles: list[tuple[Point, float]] = []
    attempts = 0
    circle_count = max(40, int(parameters.get("circle_count", 245)))
    while len(circles) < circle_count and attempts < max(28000, circle_count * 120):
        attempts += 1
        radius = min(rect.width, rect.height) * (0.009 + 0.035 * rng.random() ** 2)
        candidate = (
            rng.uniform(rect.left + radius, rect.right - radius),
            rng.uniform(rect.top + radius, rect.bottom - radius),
        )
        if (
            math.hypot(candidate[0] - centre[0], candidate[1] - centre[1])
            < eclipse_radius + radius + 1.1
        ):
            continue
        if any(
            math.hypot(candidate[0] - point[0], candidate[1] - point[1])
            < radius + other_radius + 0.8
            for point, other_radius in circles
        ):
            continue
        circles.append((candidate, radius))
    output = {pen: [] for pen in palette}
    for index, (point, radius) in enumerate(circles):
        output[palette[(index + int(radius * 10)) % len(palette)]].append(
            circle_stroke(point, radius, segments=max(24, int(radius * 8)))
        )
    for index, radius in enumerate(
        (
            eclipse_radius - 4.0,
            eclipse_radius,
            eclipse_radius + 4.0,
            eclipse_radius + 8.0,
        )
    ):
        output[palette[index % len(palette)]].append(
            circle_stroke(centre, radius, segments=150)
        )
    return output


def _generator_lissajous_choir(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    curve_count = max(12, int(parameters.get("curve_count", 72)))
    for index in range(curve_count):
        phase = index * math.pi * (math.sqrt(5.0) - 1.0) + rng.uniform(-0.01, 0.01)
        # Equal Lissajous amplitudes give every voice an identical quantized
        # tangent at the extrema; two such tangents can become the same plotted
        # millimetre after 0.001 mm serialization.  A pinned sub-percent drift
        # keeps the choir visually coherent while guaranteeing separate marks.
        amplitude_drift = rng.uniform(-0.006, 0.006)
        points: Stroke = []
        for sample in range(720):
            t = 2 * math.pi * sample / 719
            u = 0.5 + (0.459 + amplitude_drift) * math.sin(3 * t + phase)
            v = 0.5 + (0.459 - 0.7 * amplitude_drift) * math.sin(
                4 * t + phase * math.sqrt(2.0) + index * 0.013
            )
            points.append(_point(rect, u, v))
        output[palette[index % len(palette)]].append(points)
    return output


def _primes(count: int) -> list[int]:
    values: list[int] = []
    candidate = 2
    while len(values) < count:
        if all(
            candidate % divisor for divisor in range(2, int(math.sqrt(candidate)) + 1)
        ):
            values.append(candidate)
        candidate += 1
    return values


def _generator_prime_rain(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    primes = _primes(max(24, int(parameters.get("walk_count", 113))))
    for index, prime in enumerate(primes):
        base = (index + 0.5) / len(primes)
        points: Stroke = []
        phase = rng.random()
        for sample in range(150):
            v = sample / 149
            modular = ((sample * prime) % 37) / 36 - 0.5
            u = (
                base
                + 0.0065 * modular
                + 0.008 * math.sin(12 * math.pi * v + phase * 2 * math.pi)
            )
            points.append(_point(rect, u, v))
        output[palette[prime % len(palette)]].extend(_clip_polyline(rect, points))
    horizon_count = max(4, int(parameters.get("horizon_count", 17)))
    for row in range(horizon_count):
        v = (row + 0.5) / horizon_count
        points = [
            _point(rect, sample / 220, v + 0.008 * math.sin(sample * 0.31 + row))
            for sample in range(221)
        ]
        output[palette[row % len(palette)]].extend(_clip_polyline(rect, points))
    return output


def _generator_resonance_contours(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    phase = rng.uniform(-0.3, 0.3)

    def scalar(x: float, y: float) -> float:
        u = (x - rect.left) / rect.width
        v = (y - rect.top) / rect.height
        return (
            math.sin(3 * math.pi * u) * math.sin(5 * math.pi * v)
            + 0.62 * math.sin(7 * math.pi * u + phase) * math.sin(2 * math.pi * v)
            + 0.28 * math.cos(9 * math.pi * (u - v))
        )

    output = {pen: [] for pen in palette}
    level_count = max(7, int(parameters.get("contour_levels", 21)))
    levels = [
        -1.42 + index * 2.84 / max(level_count - 1, 1) for index in range(level_count)
    ]
    for level_index, stroke in _contour_paths(scalar, rect, levels, nx=74, ny=68):
        output[palette[level_index % len(palette)]].append(stroke)
    return output


def _generator_torus_knot(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    torus_p = max(2, int(parameters.get("torus_p", 3)))
    torus_q = max(3, int(parameters.get("torus_q", 7)))
    base: Stroke = []
    for sample in range(1200):
        t = 2 * math.pi * sample / 1199
        radius = 1.0 + 0.38 * math.cos(torus_q * t)
        x = radius * math.cos(torus_p * t)
        y = 0.78 * radius * math.sin(torus_p * t) + 0.28 * math.sin(torus_q * t)
        base.append((x, y))
    xs, ys = [p[0] for p in base], [p[1] for p in base]
    scale = (
        min(rect.width / (max(xs) - min(xs)), rect.height / (max(ys) - min(ys))) * 0.91
    )
    centre = rect.centre
    fitted = [(centre[0] + scale * x, centre[1] + scale * y) for x, y in base]
    output = {pen: [] for pen in palette}
    offset_count = max(5, int(parameters.get("offset_count", 37)))
    for index in range(offset_count):
        offset = (index - (offset_count - 1) / 2) * 0.62
        stroke = _offset_polyline(fitted, offset)
        output[palette[index % len(palette)]].extend(_clip_polyline(rect, stroke))
    return output


def _generator_moire_reliquary(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    centre = rect.centre
    half_width, half_height = rect.width * 0.35, rect.height * 0.44

    def in_monolith(point: Point) -> bool:
        u = abs((point[0] - centre[0]) / half_width)
        v = abs((point[1] - centre[1]) / half_height)
        return u**4 + v**4 <= 1.0

    ring_count = max(24, int(parameters.get("ring_count", 91)))
    for ring_index in range(1, ring_count + 1):
        radius = ring_index * min(rect.width, rect.height) / 105
        points: Stroke = []
        for sample in range(280):
            angle = 2 * math.pi * sample / 279
            warp = 1 + 0.06 * math.sin(9 * angle + ring_index * 0.17)
            points.append(
                (
                    centre[0] + radius * warp * math.cos(angle),
                    centre[1] + radius * 0.82 * warp * math.sin(angle),
                )
            )
        for run in _split_by_mask(points, in_monolith):
            output[palette[ring_index % len(palette)]].append(run)
    spoke_count = max(16, int(parameters.get("spoke_count", 76)))
    for spoke in range(spoke_count):
        angle = math.pi * spoke / max(spoke_count - 1, 1) + 0.08 * math.sin(spoke * 0.7)
        points = []
        for sample in range(180):
            radius = (
                -max(rect.width, rect.height) * 0.55
                + sample * max(rect.width, rect.height) * 1.1 / 179
            )
            points.append(
                (
                    centre[0] + radius * math.cos(angle),
                    centre[1] + radius * math.sin(angle),
                )
            )
        for run in _split_by_mask(points, in_monolith):
            output[palette[(spoke + 1) % len(palette)]].extend(
                _clip_polyline(rect, run)
            )
    return output


def _generator_gravity_loom(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    centre = rect.centre
    scale = min(rect.width, rect.height)
    particle_count = max(24, int(parameters.get("particle_count", 132)))
    integration_steps = max(40, int(parameters.get("integration_steps", 240)))
    for particle in range(particle_count):
        angle = 2 * math.pi * particle / particle_count
        x = centre[0] + 0.46 * rect.width * math.cos(angle)
        y = centre[1] + 0.46 * rect.height * math.sin(angle)
        vx = -0.35 * math.sin(angle) + rng.uniform(-0.05, 0.05)
        vy = 0.35 * math.cos(angle) + rng.uniform(-0.05, 0.05)
        points: Stroke = [(x, y)]
        for step in range(integration_steps):
            time = step / max(integration_steps - 1, 1)
            attractors = [
                (
                    centre[0] + 0.18 * rect.width * math.cos(2 * math.pi * time),
                    centre[1] + 0.12 * rect.height * math.sin(2 * math.pi * time),
                    1.0,
                ),
                (
                    centre[0] + 0.24 * rect.width * math.cos(2 * math.pi * time + 2.2),
                    centre[1] + 0.20 * rect.height * math.sin(2 * math.pi * time + 2.2),
                    0.8,
                ),
                (
                    centre[0] + 0.14 * rect.width * math.cos(4 * math.pi * time + 4.1),
                    centre[1] + 0.27 * rect.height * math.sin(2 * math.pi * time + 4.1),
                    -0.35,
                ),
            ]
            ax = ay = 0.0
            for target_x, target_y, mass in attractors:
                dx, dy = target_x - x, target_y - y
                distance2 = dx * dx + dy * dy + 120.0
                factor = mass * scale / (distance2**1.5)
                ax += dx * factor
                ay += dy * factor
            vx = 0.992 * vx + ax
            vy = 0.992 * vy + ay
            x += vx
            y += vy
            points.append((x, y))
            if not _inside(rect, (x, y)):
                break
        output[palette[particle % len(palette)]].extend(_clip_polyline(rect, points))
    return output


def _generator_event_horizon(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    centre = _point(rect, 0.53, 0.49)
    radius_x, radius_y = rect.width * 0.17, rect.height * 0.14
    output = {pen: [] for pen in palette}
    ray_count = max(24, int(parameters.get("ray_count", 136)))
    for ray in range(ray_count):
        base_y = rect.top + rect.height * (ray + 0.5) / ray_count
        points: Stroke = []
        deflection = 0.0
        for sample in range(260):
            x = rect.left + rect.width * sample / 259
            dx = (x - centre[0]) / radius_x
            dy = (base_y - centre[1]) / radius_y
            deflection += 0.00042 * rect.height * dy / (dx * dx + dy * dy + 0.16)
            y = (
                base_y
                + deflection
                + 0.012
                * rect.height
                * math.sin(6 * math.pi * sample / 259 + ray * 0.07)
            )
            points.append((x, y))
        runs = _split_by_mask(
            points,
            lambda point: _ellipse_value(point, centre, radius_x, radius_y) >= 1.0,
        )
        for run in runs:
            output[palette[ray % len(palette)]].extend(_clip_polyline(rect, run))
    for index, scale in enumerate((1.0, 1.12, 1.27, 1.48)):
        output[palette[index % len(palette)]].append(
            ellipse_stroke(
                centre,
                radius_x * scale,
                radius_y * scale,
                segments=180,
                rotation_deg=-8,
            )
        )
    return output


def _warp_tile_point(rect: Rect, point: Point) -> Point:
    u = (point[0] - rect.left) / rect.width
    v = (point[1] - rect.top) / rect.height
    return (
        point[0] + 3.0 * math.sin(2 * math.pi * v) + 1.3 * math.sin(8 * math.pi * u),
        point[1] + 2.4 * math.sin(2 * math.pi * u) - 1.0 * math.cos(7 * math.pi * v),
    )


def _generator_ghost_truchet(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    columns = max(6, int(parameters.get("grid_columns", 19)))
    rows = max(6, int(parameters.get("grid_rows", 18)))
    dropout = min(0.65, max(0.0, float(parameters.get("dropout_ratio", 0.17))))
    cell_w, cell_h = rect.width / columns, rect.height / rows
    for row in range(rows):
        for column in range(columns):
            if rng.random() < dropout:
                continue
            orientation = rng.randrange(2)
            x0, y0 = rect.left + column * cell_w, rect.top + row * cell_h
            centres = (
                ((x0, y0), (x0 + cell_w, y0 + cell_h))
                if orientation == 0
                else ((x0 + cell_w, y0), (x0, y0 + cell_h))
            )
            for arc_index, centre in enumerate(centres):
                points: Stroke = []
                if orientation == 0:
                    angles = (0, 90) if arc_index == 0 else (180, 270)
                else:
                    angles = (90, 180) if arc_index == 0 else (270, 360)
                for sample in range(22):
                    angle = math.radians(
                        angles[0] + (angles[1] - angles[0]) * sample / 21
                    )
                    raw = (
                        centre[0] + cell_w * math.cos(angle),
                        centre[1] + cell_h * math.sin(angle),
                    )
                    points.append(_warp_tile_point(rect, raw))
                output[palette[(row + column + arc_index) % len(palette)]].extend(
                    _clip_polyline(rect, points)
                )
    return output


def _generator_chromatic_misregister(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    phase = rng.uniform(0, 2 * math.pi)

    def scalar(x: float, y: float) -> float:
        u = (x - rect.left) / rect.width
        v = (y - rect.top) / rect.height
        return (
            math.sin(10 * u + 2.4 * math.sin(8 * v + phase))
            + 0.72 * math.cos(12 * v - 3 * u)
            + 0.35 * math.sin(19 * (u + v))
        )

    offsets = [
        (float(offset[0]), float(offset[1])) for offset in parameters["offsets_mm"]
    ]
    maximum_shift = max(abs(value) for offset in offsets for value in offset)
    widest_pen = max(PENS_BY_ID[pen_id].mark_width_mm for pen_id in palette)
    base_rect = rect.inset(maximum_shift + widest_pen)
    level_count = max(7, int(parameters.get("contour_levels", 16)))
    contours = _contour_paths(
        scalar,
        base_rect,
        [
            -1.35 + index * 2.70 / max(level_count - 1, 1)
            for index in range(level_count)
        ],
        nx=66,
        ny=66,
    )
    output = {pen: [] for pen in palette}
    for pen_index, pen in enumerate(palette):
        dx, dy = offsets[pen_index]
        for level_index, stroke in contours:
            if (level_index + pen_index) % max(
                2, len(palette) - 1
            ) == 0 or pen_index == 1:
                shifted = [(x + dx, y + dy) for x, y in stroke]
                output[pen].extend(_clip_polyline(rect, shifted))
    return output


def _generator_phase_collapse(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    rows = max(24, int(parameters.get("wave_count", 112)))
    for row in range(rows):
        v = (row + 0.5) / rows
        points: Stroke = []
        quantization = 1 + int(v * 11)
        phase = rng.uniform(-0.06, 0.06)
        for sample in range(260):
            u = sample / 259
            wave = math.sin(2 * math.pi * (4.0 + 9.0 * v) * u + phase + v * 3)
            collapsed = round(wave * quantization) / quantization
            y = v + (0.018 + 0.022 * v) * ((1 - v) * wave + v * collapsed)
            points.append(_point(rect, u, y))
        if v < 0.36:
            runs = [points]
        else:
            interval = max(8, int(27 - 15 * v))
            runs = []
            current: Stroke = []
            for sample, point in enumerate(points):
                keep = (sample // interval + row) % 3 != 0
                if keep and _inside(rect, point):
                    current.append(point)
                else:
                    if len(current) >= 2:
                        runs.append(current)
                    current = []
            if len(current) >= 2:
                runs.append(current)
        for run in runs:
            output[palette[(row // 8) % len(palette)]].extend(_clip_polyline(rect, run))
    return output


def _front_face_relief(u: float, v: float) -> float:
    """Anonymous parametric relief; it is not fitted to a real person."""

    head = math.exp(-((u / 0.72) ** 4 + ((v + 0.02) / 0.95) ** 4) * 1.5)
    eyes = -0.55 * math.exp(-(((u - 0.27) / 0.13) ** 2 + ((v + 0.24) / 0.10) ** 2))
    eyes += -0.55 * math.exp(-(((u + 0.27) / 0.13) ** 2 + ((v + 0.24) / 0.10) ** 2))
    nose = 0.75 * math.exp(-((u / 0.11) ** 2 + ((v - 0.02) / 0.33) ** 2))
    mouth = -0.48 * math.exp(-((u / 0.32) ** 2 + ((v - 0.42) / 0.08) ** 2))
    cheek = 0.18 * (
        math.exp(-(((u - 0.36) / 0.25) ** 2 + ((v - 0.08) / 0.28) ** 2))
        + math.exp(-(((u + 0.36) / 0.25) ** 2 + ((v - 0.08) / 0.28) ** 2))
    )
    return head + eyes + nose + mouth + cheek


def _face_outline(
    rect: Rect, centre: Point, radius_x: float, radius_y: float
) -> Stroke:
    points: Stroke = []
    for index in range(220):
        angle = 2 * math.pi * index / 219
        taper = 1.0 - 0.16 * max(math.sin(angle), 0.0) ** 2
        points.append(
            (
                centre[0] + radius_x * taper * math.cos(angle),
                centre[1] + radius_y * math.sin(angle),
            )
        )
    return points


def _sample_curve(points: Sequence[Point], samples_per_leg: int = 18) -> Stroke:
    """Interpolate a compact control polyline into a fluid plotted gesture."""

    if len(points) < 2:
        return list(points)
    result: Stroke = []
    for first, second in zip(points, points[1:]):
        for index in range(samples_per_leg):
            ratio = index / samples_per_leg
            eased = ratio * ratio * (3.0 - 2.0 * ratio)
            result.append(
                (
                    first[0] + eased * (second[0] - first[0]),
                    first[1] + eased * (second[1] - first[1]),
                )
            )
    result.append(points[-1])
    return result


def _front_feature_strokes(
    centre: Point,
    radius_x: float,
    radius_y: float,
    *,
    expression: float = 0.0,
) -> list[Stroke]:
    """Build one invented face from wavy, non-identifying feature gestures."""

    def p(u: float, v: float) -> Point:
        return (centre[0] + radius_x * u, centre[1] + radius_y * v)

    strokes: list[Stroke] = []
    # Brows and almond-shaped eyes deliberately differ on the two sides; a
    # perfectly mirrored mask reads like an icon rather than a human presence.
    for side in (-1.0, 1.0):
        eyebrow = _sample_curve(
            [
                p(side * 0.48, -0.34 - 0.025 * side),
                p(side * 0.29, -0.40 + 0.02 * expression),
                p(side * 0.11, -0.34 + 0.018 * side),
            ],
            20,
        )
        strokes.append(eyebrow)
        eye_centre = p(side * 0.285, -0.235 + 0.018 * side)
        eye_width, eye_height = radius_x * 0.18, radius_y * 0.055
        almond: Stroke = []
        for index in range(81):
            angle = 2 * math.pi * index / 80
            # sin(angle) supplies pointed inner and outer corners.
            almond.append(
                (
                    eye_centre[0] + eye_width * math.cos(angle),
                    eye_centre[1]
                    + eye_height * math.sin(angle) * abs(math.sin(angle)) ** 0.35,
                )
            )
        strokes.append(almond)
        strokes.append(
            circle_stroke(eye_centre, min(radius_x, radius_y) * 0.038, segments=36)
        )
        strokes.append(
            circle_stroke(eye_centre, min(radius_x, radius_y) * 0.015, segments=24)
        )

    strokes.extend(
        [
            _sample_curve(
                [p(-0.03, -0.30), p(0.025, -0.10), p(-0.035, 0.12), p(0.02, 0.26)],
                22,
            ),
            _sample_curve(
                [p(-0.18, 0.27), p(-0.06, 0.31), p(0.02, 0.28), p(0.17, 0.30)],
                18,
            ),
            _sample_curve(
                [
                    p(-0.31, 0.45),
                    p(-0.12, 0.40 - expression * 0.03),
                    p(0.0, 0.44),
                    p(0.13, 0.40 + expression * 0.03),
                    p(0.30, 0.45),
                ],
                18,
            ),
            _sample_curve(
                [
                    p(-0.29, 0.46),
                    p(-0.12, 0.53 + expression * 0.04),
                    p(0.0, 0.55),
                    p(0.14, 0.52 - expression * 0.02),
                    p(0.29, 0.46),
                ],
                18,
            ),
            _sample_curve([p(-0.48, 0.18), p(-0.42, 0.33), p(-0.31, 0.39)], 18),
            _sample_curve([p(0.49, 0.17), p(0.43, 0.31), p(0.32, 0.39)], 18),
        ]
    )
    return strokes


def _front_feature_void(u: float, v: float) -> bool:
    eyes = min(
        ((u - 0.285) / 0.19) ** 2 + ((v + 0.235) / 0.075) ** 2,
        ((u + 0.285) / 0.19) ** 2 + ((v + 0.235) / 0.075) ** 2,
    )
    mouth = (u / 0.33) ** 2 + ((v - 0.47) / 0.075) ** 2
    return eyes < 1.0 or mouth < 1.0


def _generator_face_tidal_witness(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    centre = rect.centre
    radius_x, radius_y = rect.width * 0.34, rect.height * 0.43
    rows = max(64, int(parameters.get("scanline_count", 224)))
    for row in range(rows):
        base_v = -1.10 + 2.20 * (row + 0.5) / rows
        points: Stroke = []
        local_values: list[tuple[Point, float, float]] = []
        for sample in range(320):
            local_u = -1.34 + 2.68 * sample / 319
            relief = _front_face_relief(local_u, base_v)
            x = centre[0] + radius_x * local_u
            carrier = math.sin(9.5 * local_u + row * 0.071)
            y = centre[1] + radius_y * (
                base_v
                + 0.018 * math.sin(4.2 * local_u + row * 0.035)
                + 0.095 * relief * carrier
            )
            x += radius_x * 0.032 * relief * math.sin(row * 0.135)
            point = (x, y)
            points.append(point)
            local_values.append((point, local_u, base_v))
        # The eyes and lips are held as untouched paper inside the current;
        # their own contour gestures are drawn later with a separate pen.
        runs: list[Stroke] = []
        current: Stroke = []
        for point, local_u, local_v in local_values:
            if not _front_feature_void(local_u, local_v):
                current.append(point)
            else:
                if len(current) >= 2:
                    runs.append(current)
                current = []
        if len(current) >= 2:
            runs.append(current)
        for run in runs:
            output[palette[(row // 14) % len(palette)]].extend(
                _clip_polyline(rect, run)
            )
    for index, scale in enumerate((0.975, 1.0, 1.025)):
        output[palette[-1]].append(
            _face_outline(rect, centre, radius_x * scale, radius_y * scale)
        )
    features = _front_feature_strokes(centre, radius_x, radius_y, expression=0.16)
    for index, stroke in enumerate(features):
        output[palette[(index + 1) % len(palette)]].append(stroke)
    return output


def _profile_boundary(v: float, facing: float = 1.0) -> float:
    forehead = 0.17 * math.exp(-(((v + 0.55) / 0.30) ** 2))
    nose = 0.36 * math.exp(-(((v + 0.10) / 0.12) ** 2))
    lips = 0.22 * math.exp(-(((v - 0.22) / 0.075) ** 2))
    chin = 0.17 * math.exp(-(((v - 0.52) / 0.17) ** 2))
    return facing * (0.03 + forehead + nose + lips + chin)


def _profile_stroke(
    rect: Rect, centre: Point, radius_x: float, radius_y: float, facing: float = 1.0
) -> Stroke:
    points: Stroke = []
    for sample in range(180):
        v = -0.88 + 1.76 * sample / 179
        u = _profile_boundary(v, facing)
        points.append((centre[0] + radius_x * u, centre[1] + radius_y * v))
    return points


def _profile_polygon(
    centre: Point, radius_x: float, radius_y: float, facing: float = 1.0
) -> Stroke:
    """Return a closed, invented head/profile mask facing left or right."""

    crown: Stroke = []
    for sample in range(120):
        angle = math.radians(135.0 + (310.0 - 135.0) * sample / 119)
        u = -0.05 + 0.60 * math.cos(angle)
        v = -0.02 + 0.82 * math.sin(angle)
        crown.append((centre[0] + radius_x * u * facing, centre[1] + radius_y * v))
    front: Stroke = []
    for sample in range(150):
        v = -0.65 + 1.18 * sample / 149
        u = 0.18 * facing + 0.76 * _profile_boundary(v, facing)
        front.append((centre[0] + radius_x * u, centre[1] + radius_y * v))
    jaw = _sample_curve(
        [
            (centre[0] + radius_x * 0.36 * facing, centre[1] + radius_y * 0.54),
            (centre[0] + radius_x * 0.19 * facing, centre[1] + radius_y * 0.64),
            (centre[0] - radius_x * 0.04 * facing, centre[1] + radius_y * 0.69),
            (centre[0] - radius_x * 0.27 * facing, centre[1] + radius_y * 0.64),
            (centre[0] - radius_x * 0.47 * facing, centre[1] + radius_y * 0.56),
        ],
        18,
    )
    return [*crown, *front, *jaw, crown[0]]


def _point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current[1] > y) != (previous[1] > y):
            crossing = (previous[0] - current[0]) * (y - current[1]) / (
                previous[1] - current[1]
            ) + current[0]
            if x < crossing:
                inside = not inside
        previous = current
    return inside


def _profile_features(
    centre: Point, radius_x: float, radius_y: float, facing: float = 1.0
) -> list[Stroke]:
    def p(u: float, v: float) -> Point:
        return (centre[0] + radius_x * u * facing, centre[1] + radius_y * v)

    eye = p(0.20, -0.24)
    return [
        ellipse_stroke(
            eye,
            radius_x * 0.095,
            radius_y * 0.035,
            segments=48,
            rotation_deg=-8 * facing,
        ),
        circle_stroke(eye, min(radius_x, radius_y) * 0.022, segments=24),
        _sample_curve([p(0.31, -0.35), p(0.20, -0.39), p(0.08, -0.35)], 18),
        _sample_curve(
            [
                p(-0.27, -0.16),
                p(-0.38, 0.02),
                p(-0.30, 0.19),
                p(-0.16, 0.02),
                p(-0.27, -0.16),
            ],
            20,
        ),
        _sample_curve([p(0.43, 0.22), p(0.53, 0.25), p(0.43, 0.29)], 18),
        _sample_curve([p(0.18, 0.48), p(0.02, 0.58), p(-0.12, 0.60)], 18),
    ]


def _generator_face_profile_crosswind(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    centre = _point(rect, 0.49, 0.50)
    radius_x, radius_y = rect.width * 0.43, rect.height * 0.54
    silhouette = _profile_polygon(centre, radius_x, radius_y)
    rows = max(64, int(parameters.get("trace_count", 206)))
    for row in range(rows):
        v = -1.0 + 2.0 * (row + 0.5) / rows
        points: Stroke = []
        for sample in range(320):
            u = -1.18 + 2.36 * sample / 319
            boundary = 0.18 + 0.76 * _profile_boundary(v)
            distance = u - boundary
            refraction = (
                0.11
                * math.exp(-((distance / 0.23) ** 2))
                * math.sin(15 * distance + v * 7)
            )
            x = centre[0] + radius_x * u
            y = centre[1] + radius_y * (
                v + refraction + 0.014 * math.sin(10 * u + row * 0.09)
            )
            points.append((x, y))
        # Currents outside the head remain pale and sparse; denser coloured
        # runs inside the mask make the likeness emerge from a change in wind.
        inside_runs = _split_by_mask(
            points, lambda point: _point_in_polygon(point, silhouette)
        )
        outside_runs = _split_by_mask(
            points, lambda point: not _point_in_polygon(point, silhouette)
        )
        output[palette[(row // 12) % len(palette)]].extend(inside_runs)
        if row % 3 == 0:
            for run in outside_runs:
                if len(run) >= 2:
                    output[palette[0]].extend(_clip_polyline(rect, run))
    for scale in (0.985, 1.0, 1.015):
        scaled = [
            (centre[0] + (x - centre[0]) * scale, centre[1] + (y - centre[1]) * scale)
            for x, y in silhouette
        ]
        output[palette[-1]].append(scaled)
    for index, stroke in enumerate(_profile_features(centre, radius_x, radius_y)):
        output[palette[(index + 1) % len(palette)]].append(stroke)
    return output


def _generator_face_double_exposure(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    left = _point(rect, 0.38, 0.50)
    right = _point(rect, 0.62, 0.50)
    radius_x, radius_y = rect.width * 0.36, rect.height * 0.53
    for family, (centre, facing) in enumerate(((left, 1.0), (right, -1.0))):
        silhouette = _profile_polygon(centre, radius_x, radius_y, facing)
        line_count = max(48, int(parameters.get("lines_per_profile", 164)))
        for line in range(line_count):
            offset = -1.0 + 2.0 * (line + 0.5) / line_count
            points: Stroke = []
            for sample in range(280):
                t = -1.12 + 2.24 * sample / 279
                if family == 0:
                    u, v = t, offset + 0.045 * math.sin(11 * t + line * 0.10)
                else:
                    u, v = offset + 0.045 * math.cos(11 * t + line * 0.10), t
                points.append((centre[0] + radius_x * u, centre[1] + radius_y * v))
            runs = _split_by_mask(
                points,
                lambda point, polygon=silhouette: _point_in_polygon(point, polygon),
            )
            for run in runs:
                output[palette[(line + 2 * family) % len(palette)]].extend(
                    _clip_polyline(rect, run)
                )
        for scale in (0.985, 1.0, 1.015):
            outline = [
                (
                    centre[0] + (x - centre[0]) * scale,
                    centre[1] + (y - centre[1]) * scale,
                )
                for x, y in silhouette
            ]
            output[palette[(family + 1) % len(palette)]].append(outline)
        for index, stroke in enumerate(
            _profile_features(centre, radius_x, radius_y, facing)
        ):
            output[palette[(index + family) % len(palette)]].append(stroke)
    return output


def _generator_face_frequency_veil(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    centre = rect.centre
    radius_x, radius_y = rect.width * 0.35, rect.height * 0.43
    output = {pen: [] for pen in palette}
    rows = max(72, int(parameters.get("scanline_count", 286)))
    for row in range(rows):
        v = -0.98 + 1.96 * (row + 0.5) / rows
        points: Stroke = []
        local_points: list[tuple[Point, float, float]] = []
        for sample in range(340):
            u = -1.03 + 2.06 * sample / 339
            relief = _front_face_relief(u, v)
            amplitude = 0.008 + 0.055 * max(0.0, min(1.4, relief))
            shifted_v = v + amplitude * math.sin(24 * u + row * 0.17)
            point = (centre[0] + radius_x * u, centre[1] + radius_y * shifted_v)
            points.append(point)
            local_points.append((point, u, v))
        runs: list[Stroke] = []
        current: Stroke = []
        for point, u, local_v in local_points:
            in_head = u * u + (local_v / 0.98) ** 2 <= 1.0
            if in_head and not _front_feature_void(u, local_v):
                current.append(point)
            else:
                if len(current) >= 2:
                    runs.append(current)
                current = []
        if len(current) >= 2:
            runs.append(current)
        output[palette[(row // 13) % len(palette)]].extend(runs)
    for index, scale in enumerate((0.97, 1.0, 1.03)):
        output[palette[-1]].append(
            _face_outline(rect, centre, radius_x * scale, radius_y * scale)
        )
    for index, stroke in enumerate(
        _front_feature_strokes(centre, radius_x, radius_y, expression=-0.10)
    ):
        output[palette[index % len(palette)]].append(stroke)
    return output


def _generator_face_nocturnal_chorus(
    rect: Rect, rng: random.Random, palette: tuple[str, ...], parameters: dict[str, Any]
) -> dict[str, list[Stroke]]:
    output = {pen: [] for pen in palette}
    faces = [
        (_point(rect, 0.24, 0.51), rect.width * 0.19, rect.height * 0.36, -0.13),
        (_point(rect, 0.51, 0.47), rect.width * 0.21, rect.height * 0.41, 0.18),
        (_point(rect, 0.78, 0.53), rect.width * 0.18, rect.height * 0.34, -0.22),
    ]
    faces = faces[: max(1, min(3, int(parameters.get("face_count", 3))))]
    for face_index, (centre, radius_x, radius_y, expression) in enumerate(faces):
        base_columns = max(32, int(parameters.get("streamlines_per_face", 76)))
        columns = int(round(base_columns * 1.20)) if face_index == 1 else base_columns
        for column in range(columns):
            u = -1.0 + 2.0 * (column + 0.5) / columns
            points: Stroke = []
            for sample in range(250):
                v = -0.98 + 1.96 * sample / 249
                relief = _front_face_relief(u, v)
                shifted_u = u + 0.052 * relief * math.sin(
                    10 * v + column * 0.11 + face_index
                )
                points.append(
                    (centre[0] + radius_x * shifted_u, centre[1] + radius_y * v)
                )
            runs = _split_by_mask(
                points,
                lambda point, c=centre, rx=radius_x, ry=radius_y: _ellipse_value(
                    point, c, rx, ry
                )
                <= 1.0,
            )
            # Metallics become rhythmic highlights, not contiguous one-mm fill.
            if column % 11 == 0:
                pen = palette[
                    1 + (column // 11 + face_index) % max(1, len(palette) - 1)
                ]
            else:
                pen = palette[0]
            output[pen].extend(runs)
        for scale in (0.98, 1.0, 1.02):
            output[palette[(face_index + 1) % len(palette)]].append(
                _face_outline(rect, centre, radius_x * scale, radius_y * scale)
            )
        for feature_index, stroke in enumerate(
            _front_feature_strokes(centre, radius_x, radius_y, expression=expression)
        ):
            output[palette[(feature_index + face_index + 1) % len(palette)]].append(
                stroke
            )
    # A few long waves bind the separate presences into one chorus.
    binder_count = max(4, int(parameters.get("binder_wave_count", 24)))
    for row in range(binder_count):
        points = [
            _point(
                rect,
                sample / 279,
                0.08
                + row * 0.84 / max(binder_count - 1, 1)
                + 0.012 * math.sin(sample * 0.13 + row * 0.6),
            )
            for sample in range(280)
        ]
        output[palette[0]].extend(_clip_polyline(rect, points))
    return output


Generator = Callable[
    [Rect, random.Random, tuple[str, ...], dict[str, Any]], dict[str, list[Stroke]]
]

_GENERATORS: dict[str, Generator] = {
    "curl-field": _generator_curl_field,
    "magnetic-dipoles": _generator_magnetic_dipoles,
    "folded-horizon": _generator_folded_horizon,
    "chromatic-shear": _generator_chromatic_shear,
    "braided-silence": _generator_braided_silence,
    "coral-growth": _generator_coral_growth,
    "cellular-cathedral": _generator_cellular_cathedral,
    "reaction-garden": _generator_reaction_garden,
    "fault-bloom": _generator_fault_bloom,
    "foam-eclipse": _generator_foam_eclipse,
    "lissajous-choir": _generator_lissajous_choir,
    "prime-rain": _generator_prime_rain,
    "resonance-contours": _generator_resonance_contours,
    "torus-knot": _generator_torus_knot,
    "moire-reliquary": _generator_moire_reliquary,
    "gravity-loom": _generator_gravity_loom,
    "event-horizon": _generator_event_horizon,
    "ghost-truchet": _generator_ghost_truchet,
    "chromatic-misregister": _generator_chromatic_misregister,
    "phase-collapse": _generator_phase_collapse,
    "face-tidal-witness": _generator_face_tidal_witness,
    "face-profile-crosswind": _generator_face_profile_crosswind,
    "face-double-exposure": _generator_face_double_exposure,
    "face-frequency-veil": _generator_face_frequency_veil,
    "face-nocturnal-chorus": _generator_face_nocturnal_chorus,
}


def _canonical_geometry(layers: Sequence[ArtworkLayer]) -> bytes:
    payload = [
        {
            "pen_id": layer.pen_id,
            "strokes": [
                [[round(x, 6), round(y, 6)] for x, y in record.points]
                for record in layer.records
            ],
        }
        for layer in layers
        if layer.records
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _nearest_endpoint_order(strokes: Sequence[Stroke], start: Point) -> list[Stroke]:
    """Order freely generated marks without changing their visible geometry."""

    remaining = [(index, list(stroke)) for index, stroke in enumerate(strokes)]
    ordered: list[Stroke] = []
    current = start
    while remaining:
        best_position = 0
        best_reverse = False
        best_key: tuple[float, int, int] | None = None
        for position, (original_index, stroke) in enumerate(remaining):
            start_distance = (stroke[0][0] - current[0]) ** 2 + (
                stroke[0][1] - current[1]
            ) ** 2
            end_distance = (stroke[-1][0] - current[0]) ** 2 + (
                stroke[-1][1] - current[1]
            ) ** 2
            reverse = end_distance < start_distance
            key = (min(start_distance, end_distance), original_index, int(reverse))
            if best_key is None or key < best_key:
                best_key = key
                best_position = position
                best_reverse = reverse
        _, selected = remaining.pop(best_position)
        if best_reverse:
            selected.reverse()
        ordered.append(selected)
        current = selected[-1]
    return ordered


def geometry_sha256(artwork: PlateArtwork) -> str:
    """Return a stable digest of art layers, excluding format furniture."""

    return hashlib.sha256(_canonical_geometry(artwork.layers)).hexdigest()


def _parameter_sha256(piece: AbstractPiece) -> str:
    payload = {
        "algorithm": piece.algorithm,
        "algorithm_version": ALGORITHM_VERSION,
        "seed": piece.seed,
        "format_id": piece.format_id,
        "palette": piece.palette,
        "parameters": piece.parameters,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_abstract_artwork(piece_id: str) -> PlateArtwork:
    """Regenerate one catalog piece as plotter-safe physical geometry."""

    piece = _piece_by_id(piece_id)
    context = context_for(piece.format_id)
    # The binding named field is the complete composition boundary. Individual
    # grammars may create internal negative space, but the plate engine must not
    # introduce a private page-layout inset outside format-v1.json.
    art_rect = context.field
    generated = _GENERATORS[piece.algorithm](
        art_rect,
        random.Random(piece.seed),
        piece.palette,
        piece.parameters,
    )
    layers: list[ArtworkLayer] = []
    candidate_count = 0
    filtered_count = 0
    vertex_count = 0
    for pen_id in piece.palette:
        layer = ArtworkLayer(
            id=f"art-{pen_id.replace('-', '_')}",
            label=f"Abstract artwork / {PENS_BY_ID[pen_id].label}",
            pen_id=pen_id,
        )
        minimum_length = 3.0 * layer.pen.mark_width_mm
        accepted: list[Stroke] = []
        for candidate in generated.get(pen_id, []):
            candidate_count += 1
            if len(candidate) < 2:
                filtered_count += 1
                continue
            if any(not _inside(art_rect, point, epsilon=0.002) for point in candidate):
                raise MapPlotterError(
                    f"{piece.id}/{piece.algorithm} emitted geometry outside its art field."
                )
            if not all(math.isfinite(value) for point in candidate for value in point):
                raise MapPlotterError(
                    f"{piece.id}/{piece.algorithm} emitted non-finite geometry."
                )
            if polyline_length_mm(candidate) + 1e-9 < minimum_length:
                filtered_count += 1
                continue
            accepted.append(list(candidate))
        for candidate in _nearest_endpoint_order(
            accepted, (art_rect.left, art_rect.top)
        ):
            layer.add(
                candidate,
                source_ref=f"abstract-engine-v{ALGORITHM_VERSION}",
                role=("anonymous-face-line" if piece.is_face else "generative-line"),
                attributes={
                    "data-algorithm": piece.algorithm,
                    "data-project-authored": "true",
                },
            )
            vertex_count += len(candidate)
        if layer.records:
            layers.append(layer)
    if not layers or sum(len(layer.records) for layer in layers) < 12:
        raise MapPlotterError(f"{piece.id} did not produce enough plottable geometry.")

    parameter_digest = _parameter_sha256(piece)
    geometry_digest = hashlib.sha256(_canonical_geometry(layers)).hexdigest()
    portrait_metadata: dict[str, Any] = {}
    if piece.is_face:
        portrait_metadata = {
            "portrait_mode": "anonymous-parametric",
            "recognisable_person": None,
            "portrait_source_image": None,
            "identity_claim": "none",
            "identity_intent": (
                "not sourced from or intended to identify a real person"
            ),
        }
    return PlateArtwork(
        subject_id=piece.id,
        domain="abstract-art",
        subject_kind=(
            "anonymous-abstract-face" if piece.is_face else "generative-abstract"
        ),
        title=piece.title.upper(),
        subtitle=f"ABSTRACT STUDY {piece.catalog_index:02d} / 25",
        details=(
            f"GRAMMAR / {piece.algorithm.upper()}",
            f"EDITION SEED / {piece.seed}",
            f"ARTWORK PALETTE / {len(piece.palette)} PENS",
        ),
        credit_line="PROJECT-AUTHORED GENERATIVE GEOMETRY",
        scale_status="not-to-scale",
        evidence_status="project-authored-deterministic",
        rights_status="project-authored",
        sources=(
            {
                "id": "abstract-engine-v1",
                "kind": "project-authored-algorithm",
                "title": "City Map Plotter abstract geometry engine",
                "license": "project-authored",
                "geometry_use": "all artwork centre-lines",
            },
        ),
        context=context,
        layers=layers,
        artifact_kind="abstract-pen-art",
        rendering_preset=f"abstract-{piece.algorithm}-v1",
        format_subject_policy="abstract-pen-art",
        source_provider="project-authored deterministic algorithm",
        source_license="project-authored",
        data_snapshot="2026-08-09",
        notes=(
            "Original mathematical composition; no reference artwork geometry was traced or imported.",
            "Artwork is clipped to the binding named map field; negative space belongs to each grammar.",
            "Ink coverage is reported as an advisory and is not used to cull the composition.",
        ),
        catalog_record=piece.as_dict(),
        rendering_metadata={
            "abstract_art": {
                "catalog_id": "abstract-art-v1",
                "catalog_index": piece.catalog_index,
                "algorithm": piece.algorithm,
                "algorithm_version": ALGORITHM_VERSION,
                "seed": piece.seed,
                "palette": list(piece.palette),
                "parameters": dict(piece.parameters),
                "stock_tone": str(piece.parameters.get("stock_tone", "light")),
                "parameter_sha256": parameter_digest,
                "geometry_sha256": geometry_digest,
                "candidate_strokes": candidate_count,
                "filtered_sub_three_nib_strokes": filtered_count,
                "plottable_strokes": sum(len(layer.records) for layer in layers),
                "vertex_count": vertex_count,
                "art_field_mm": art_rect.as_dict(),
                "solid_fills_used": False,
                "raster_sources_used": False,
                "reference_geometry_imported": False,
                "stroke_order": "nearest-endpoint-within-physical-pen",
                **portrait_metadata,
            }
        },
    )


def build_all_abstract_artworks() -> tuple[PlateArtwork, ...]:
    return tuple(build_abstract_artwork(piece.id) for piece in load_abstract_catalog())

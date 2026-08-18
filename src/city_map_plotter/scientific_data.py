"""Truth-preserving numerical utilities for scientific pen artwork.

The functions in this module deliberately stop before page styling.  They keep
source indices, explicit gaps, axis transforms, and contour levels available so
the academic renderer can record exactly what happened in its manifest.
Nothing here smooths, imputes, or silently changes an axis scale.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping, Sequence

from .models import MapPlotterError


Point = tuple[float, float]
NumericValue = float | None


def _tolist(value: Any) -> Any:
    """Accept NumPy-compatible values without taking a NumPy dependency."""

    converter = getattr(value, "tolist", None)
    if callable(converter):
        return converter()
    return value


def coerce_numeric_sequence(
    value: Any,
    label: str,
    *,
    allow_missing: bool = True,
) -> tuple[NumericValue, ...]:
    """Return finite floats while retaining explicit/NaN missing samples.

    JSON ``null`` and IEEE NaN are treated as missing observations.  Infinity
    is rejected because it is neither a plottable value nor an unambiguous
    missing-data marker.
    """

    value = _tolist(value)
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise MapPlotterError(f"{label} must be a numeric sequence.")
    result: list[NumericValue] = []
    for index, raw in enumerate(value):
        raw = _tolist(raw)
        if raw is None:
            if not allow_missing:
                raise MapPlotterError(f"{label}[{index}] may not be missing.")
            result.append(None)
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise MapPlotterError(f"{label}[{index}] must be a number or null.")
        number = float(raw)
        if math.isnan(number):
            if not allow_missing:
                raise MapPlotterError(f"{label}[{index}] may not be NaN.")
            result.append(None)
        elif not math.isfinite(number):
            raise MapPlotterError(f"{label}[{index}] may not be infinite.")
        else:
            result.append(0.0 if number == 0.0 else number)
    if not result:
        raise MapPlotterError(f"{label} must not be empty.")
    return tuple(result)


def coerce_numeric_columns(value: Any, label: str = "table") -> dict[str, tuple[NumericValue, ...]]:
    """Accept a mapping of column arrays or a NumPy structured-style mapping."""

    value = _tolist(value)
    if not isinstance(value, Mapping) or not value:
        raise MapPlotterError(f"{label} must be a non-empty mapping of columns.")
    columns: dict[str, tuple[NumericValue, ...]] = {}
    for raw_name, raw_values in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise MapPlotterError(f"{label} column names must be non-empty text.")
        name = raw_name.strip()
        if name in columns:
            raise MapPlotterError(f"{label} repeats column {name!r}.")
        columns[name] = coerce_numeric_sequence(raw_values, f"{label}.{name}")
    lengths = {len(column) for column in columns.values()}
    if len(lengths) != 1:
        raise MapPlotterError(f"{label} columns must have equal lengths.")
    return columns


def finite_runs(
    x_values: Sequence[NumericValue],
    y_values: Sequence[NumericValue],
    *,
    breaks_before: Iterable[int] = (),
) -> tuple[tuple[int, ...], ...]:
    """Split paired data at missing observations and explicit discontinuities."""

    if len(x_values) != len(y_values):
        raise MapPlotterError("Scientific x and y arrays must have equal lengths.")
    explicit = set(breaks_before)
    if any(isinstance(index, bool) or not isinstance(index, int) for index in explicit):
        raise MapPlotterError("Trace discontinuity indices must be integers.")
    if any(index <= 0 or index >= len(x_values) for index in explicit):
        raise MapPlotterError("Trace discontinuity indices must fall inside the trace.")

    runs: list[tuple[int, ...]] = []
    current: list[int] = []
    for index, (x_value, y_value) in enumerate(zip(x_values, y_values, strict=True)):
        if index in explicit and current:
            runs.append(tuple(current))
            current = []
        if x_value is None or y_value is None:
            if current:
                runs.append(tuple(current))
                current = []
            continue
        current.append(index)
    if current:
        runs.append(tuple(current))
    return tuple(run for run in runs if len(run) >= 2)


@dataclass(frozen=True)
class DownsampledTrace:
    """Selected source indices; source values themselves are never rewritten."""

    runs: tuple[tuple[int, ...], ...]
    input_count: int
    finite_count: int
    output_count: int
    target_points_per_run: int
    protected_indices: tuple[int, ...]
    algorithm: str = "indexed-minmax-extrema-discontinuity-v1"

    @property
    def indices(self) -> tuple[int, ...]:
        return tuple(index for run in self.runs for index in run)


def _direction_reversals(indices: Sequence[int], x_values: Sequence[NumericValue]) -> set[int]:
    protected: set[int] = set()
    previous_sign = 0
    for left, middle, right in zip(indices, indices[1:], indices[2:], strict=False):
        left_x = x_values[left]
        middle_x = x_values[middle]
        right_x = x_values[right]
        assert left_x is not None and middle_x is not None and right_x is not None
        before = middle_x - left_x
        after = right_x - middle_x
        before_sign = 1 if before > 0 else -1 if before < 0 else previous_sign
        after_sign = 1 if after > 0 else -1 if after < 0 else before_sign
        if before_sign and after_sign and before_sign != after_sign:
            protected.add(middle)
        previous_sign = after_sign
    return protected


def extrema_preserving_downsample(
    x: Any,
    y: Any,
    *,
    target_points_per_run: int,
    breaks_before: Iterable[int] = (),
    protected_indices: Iterable[int] = (),
) -> DownsampledTrace:
    """Downsample by source index while retaining bucket minima and maxima.

    Every missing-data gap remains a gap.  The first and last observation of
    each run, user-protected observations, and x-direction reversals (important
    for hysteresis branches) are mandatory.  Each remaining index bucket keeps
    both its y minimum and maximum in original order, so a one-sample resonance
    cannot disappear between averages.  The target is therefore a density goal,
    not a hard cap: scientific mandatory points are allowed to exceed it.
    """

    if (
        isinstance(target_points_per_run, bool)
        or not isinstance(target_points_per_run, int)
        or target_points_per_run < 4
    ):
        raise MapPlotterError("Scientific downsampling needs at least four target points per run.")
    x_values = coerce_numeric_sequence(x, "trace.x")
    y_values = coerce_numeric_sequence(y, "trace.y")
    runs = finite_runs(x_values, y_values, breaks_before=breaks_before)
    requested = set(protected_indices)
    if any(isinstance(index, bool) or not isinstance(index, int) for index in requested):
        raise MapPlotterError("Protected trace indices must be integers.")
    if any(index < 0 or index >= len(x_values) for index in requested):
        raise MapPlotterError("Protected trace indices fall outside the trace.")

    selected_runs: list[tuple[int, ...]] = []
    all_protected: set[int] = set(requested)
    for run in runs:
        if len(run) <= target_points_per_run:
            selected_runs.append(run)
            all_protected.update((run[0], run[-1]))
            continue
        mandatory = {run[0], run[-1], *requested.intersection(run)}
        mandatory.update(_direction_reversals(run, x_values))
        all_protected.update(mandatory)

        interior = run[1:-1]
        bucket_count = max(1, (target_points_per_run - 2) // 2)
        selected = set(mandatory)
        for bucket in range(bucket_count):
            start = bucket * len(interior) // bucket_count
            end = (bucket + 1) * len(interior) // bucket_count
            candidates = interior[start:end]
            if not candidates:
                continue
            minimum = min(candidates, key=lambda index: (y_values[index], index))
            maximum = max(candidates, key=lambda index: (y_values[index], -index))
            selected.update((minimum, maximum))
        selected_runs.append(tuple(index for index in run if index in selected))

    return DownsampledTrace(
        runs=tuple(selected_runs),
        input_count=len(x_values),
        finite_count=sum(len(run) for run in runs),
        output_count=sum(len(run) for run in selected_runs),
        target_points_per_run=target_points_per_run,
        protected_indices=tuple(sorted(all_protected)),
    )


@dataclass(frozen=True)
class AxisTransform:
    scale: str
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        if self.scale not in {"linear", "log10"}:
            raise MapPlotterError("Scientific axes support only explicit linear or log10 scales.")
        if not all(math.isfinite(value) for value in (self.minimum, self.maximum)):
            raise MapPlotterError("Scientific axis limits must be finite.")
        if self.minimum >= self.maximum:
            raise MapPlotterError("Scientific axis minimum must be below its maximum.")
        if self.scale == "log10" and self.minimum <= 0:
            raise MapPlotterError("A log10 axis requires a positive minimum.")

    def normalized(self, value: float) -> float:
        if not math.isfinite(value):
            raise MapPlotterError("Cannot project a non-finite scientific value.")
        if self.scale == "log10":
            if value <= 0:
                raise MapPlotterError("A log10 trace contains a non-positive finite value.")
            low = math.log10(self.minimum)
            high = math.log10(self.maximum)
            transformed = math.log10(value)
        else:
            low = self.minimum
            high = self.maximum
            transformed = value
        return (transformed - low) / (high - low)


def uncertainty_hatch_indices(length: int, *, maximum_strokes: int) -> tuple[int, ...]:
    """Select source columns for sparse uncertainty hatching, including ends."""

    if length < 2:
        raise MapPlotterError("An uncertainty envelope needs at least two samples.")
    if isinstance(maximum_strokes, bool) or not isinstance(maximum_strokes, int):
        raise MapPlotterError("Uncertainty hatch count must be an integer.")
    if maximum_strokes < 2:
        raise MapPlotterError("Uncertainty hatching needs at least two strokes.")
    count = min(length, maximum_strokes)
    return tuple(sorted({round(index * (length - 1) / (count - 1)) for index in range(count)}))


@dataclass(frozen=True)
class ContourResult:
    level: float
    paths: tuple[tuple[Point, ...], ...]
    cell_count: int
    ambiguous_cell_count: int
    algorithm: str = "marching-squares-linear-interpolation-asymptotic-v1"


def _finite_grid(value: Any) -> tuple[tuple[NumericValue, ...], ...]:
    value = _tolist(value)
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise MapPlotterError("Scalar-field values must be a rectangular matrix.")
    rows = tuple(
        coerce_numeric_sequence(row, f"scalar.values[{index}]")
        for index, row in enumerate(value)
    )
    if len(rows) < 2 or min((len(row) for row in rows), default=0) < 2:
        raise MapPlotterError("A scalar field needs at least a 2 by 2 matrix.")
    if len({len(row) for row in rows}) != 1:
        raise MapPlotterError("Scalar-field rows must have equal lengths.")
    return rows


def _without_missing(values: Sequence[NumericValue], label: str) -> tuple[float, ...]:
    result: list[float] = []
    for index, value in enumerate(values):
        if value is None:
            raise MapPlotterError(f"{label}[{index}] may not be missing.")
        result.append(value)
    return tuple(result)


def _interpolate(level: float, first: Point, second: Point, a: float, b: float) -> Point:
    if a == b:
        fraction = 0.5
    else:
        fraction = (level - a) / (b - a)
    fraction = min(1.0, max(0.0, fraction))
    return (
        first[0] + fraction * (second[0] - first[0]),
        first[1] + fraction * (second[1] - first[1]),
    )


def _point_key(point: Point) -> tuple[float, float]:
    return (round(point[0], 12), round(point[1], 12))


def _stitch_segments(segments: Sequence[tuple[Point, Point]]) -> tuple[tuple[Point, ...], ...]:
    if not segments:
        return ()
    adjacency: dict[tuple[float, float], list[int]] = {}
    for index, segment in enumerate(segments):
        for point in segment:
            adjacency.setdefault(_point_key(point), []).append(index)
    unused = set(range(len(segments)))
    paths: list[tuple[Point, ...]] = []
    while unused:
        start_index = next(
            (
                index
                for index in sorted(unused)
                if any(len(adjacency[_point_key(point)]) == 1 for point in segments[index])
            ),
            min(unused),
        )
        first, second = segments[start_index]
        if len(adjacency[_point_key(second)]) == 1 and len(adjacency[_point_key(first)]) != 1:
            first, second = second, first
        chain = [first, second]
        unused.remove(start_index)
        while True:
            key = _point_key(chain[-1])
            candidates = [index for index in adjacency.get(key, ()) if index in unused]
            if not candidates:
                break
            next_index = min(candidates)
            left, right = segments[next_index]
            chain.append(right if _point_key(left) == key else left)
            unused.remove(next_index)
        while True:
            key = _point_key(chain[0])
            candidates = [index for index in adjacency.get(key, ()) if index in unused]
            if not candidates:
                break
            next_index = min(candidates)
            left, right = segments[next_index]
            chain.insert(0, right if _point_key(left) == key else left)
            unused.remove(next_index)
        paths.append(tuple(chain))
    return tuple(paths)


def marching_squares(
    values: Any,
    *,
    x: Any,
    y: Any,
    levels: Any,
) -> tuple[ContourResult, ...]:
    """Extract explicitly selected scalar-field levels without raster output."""

    grid = _finite_grid(values)
    x_values = _without_missing(
        coerce_numeric_sequence(x, "scalar.x", allow_missing=False), "scalar.x"
    )
    y_values = _without_missing(
        coerce_numeric_sequence(y, "scalar.y", allow_missing=False), "scalar.y"
    )
    level_values = _without_missing(
        coerce_numeric_sequence(levels, "scalar.levels", allow_missing=False),
        "scalar.levels",
    )
    if len(x_values) != len(grid[0]) or len(y_values) != len(grid):
        raise MapPlotterError("Scalar-field coordinates must match the matrix dimensions.")
    if any(first >= second for first, second in zip(x_values, x_values[1:], strict=False)):
        raise MapPlotterError("Scalar-field x coordinates must be strictly increasing.")
    if any(first >= second for first, second in zip(y_values, y_values[1:], strict=False)):
        raise MapPlotterError("Scalar-field y coordinates must be strictly increasing.")
    if len(set(level_values)) != len(level_values):
        raise MapPlotterError("Scalar-field contour levels must be unique.")

    results: list[ContourResult] = []
    for raw_level in level_values:
        level = raw_level
        segments: list[tuple[Point, Point]] = []
        cells = 0
        ambiguous = 0
        for row in range(len(y_values) - 1):
            for column in range(len(x_values) - 1):
                raw_corners = (
                    grid[row][column],
                    grid[row][column + 1],
                    grid[row + 1][column + 1],
                    grid[row + 1][column],
                )
                if any(value is None for value in raw_corners):
                    continue
                corners = tuple(float(value) for value in raw_corners if value is not None)
                points = (
                    (x_values[column], y_values[row]),
                    (x_values[column + 1], y_values[row]),
                    (x_values[column + 1], y_values[row + 1]),
                    (x_values[column], y_values[row + 1]),
                )
                cells += 1
                crossings: dict[int, Point] = {}
                for edge, (first_index, second_index) in enumerate(
                    ((0, 1), (1, 2), (2, 3), (3, 0))
                ):
                    first_value = corners[first_index]
                    second_value = corners[second_index]
                    if (first_value >= level) == (second_value >= level):
                        continue
                    crossings[edge] = _interpolate(
                        level,
                        points[first_index],
                        points[second_index],
                        first_value,
                        second_value,
                    )
                if len(crossings) == 2:
                    first_edge, second_edge = sorted(crossings)
                    segments.append((crossings[first_edge], crossings[second_edge]))
                elif len(crossings) == 4:
                    ambiguous += 1
                    pattern = sum(
                        (1 << index) for index, value in enumerate(corners) if value >= level
                    )
                    centre_high = sum(corners) / 4.0 >= level
                    isolate_one_and_three = (pattern == 0b0101 and centre_high) or (
                        pattern == 0b1010 and not centre_high
                    )
                    pairs = (
                        ((0, 1), (2, 3))
                        if isolate_one_and_three
                        else ((0, 3), (1, 2))
                    )
                    segments.extend(
                        (crossings[first_edge], crossings[second_edge])
                        for first_edge, second_edge in pairs
                    )
                elif crossings:
                    raise MapPlotterError(
                        "Scalar contour encountered an unresolved level degeneracy; "
                        "choose levels that do not exactly coincide with a flat cell."
                    )
        results.append(
            ContourResult(
                level=level,
                paths=_stitch_segments(segments),
                cell_count=cells,
                ambiguous_cell_count=ambiguous,
            )
        )
    return tuple(results)


def dash_polyline(
    points: Sequence[Point],
    *,
    dash_mm: float,
    gap_mm: float,
    minimum_stroke_mm: float,
) -> tuple[tuple[Point, ...], ...]:
    """Convert one page-space line to physical dash strokes."""

    if len(points) < 2:
        raise MapPlotterError("A dashed scientific line needs at least two points.")
    if not all(math.isfinite(value) and value > 0 for value in (dash_mm, gap_mm, minimum_stroke_mm)):
        raise MapPlotterError("Scientific dash, gap, and minimum stroke must be positive.")
    if dash_mm + 1e-9 < minimum_stroke_mm:
        raise MapPlotterError("Scientific dash length is below the physical three-nib floor.")

    result: list[tuple[Point, ...]] = []
    drawing = True
    remaining = dash_mm
    current: list[Point] = [points[0]]
    cursor = points[0]
    for endpoint in points[1:]:
        dx = endpoint[0] - cursor[0]
        dy = endpoint[1] - cursor[1]
        segment_remaining = math.hypot(dx, dy)
        if segment_remaining <= 1e-12:
            cursor = endpoint
            continue
        ux, uy = dx / segment_remaining, dy / segment_remaining
        while segment_remaining > 1e-12:
            travel = min(remaining, segment_remaining)
            cursor = (cursor[0] + ux * travel, cursor[1] + uy * travel)
            if drawing:
                current.append(cursor)
            remaining -= travel
            segment_remaining -= travel
            if remaining <= 1e-10:
                if drawing and len(current) >= 2:
                    if sum(
                        math.dist(first, second)
                        for first, second in zip(current, current[1:], strict=False)
                    ) + 1e-9 >= minimum_stroke_mm:
                        result.append(tuple(current))
                drawing = not drawing
                remaining = dash_mm if drawing else gap_mm
                current = [cursor]
        cursor = endpoint
    if drawing and len(current) >= 2:
        length = sum(
            math.dist(first, second)
            for first, second in zip(current, current[1:], strict=False)
        )
        if length + 1e-9 >= minimum_stroke_mm:
            result.append(tuple(current))
    return tuple(result)


__all__ = [
    "AxisTransform",
    "ContourResult",
    "DownsampledTrace",
    "coerce_numeric_columns",
    "coerce_numeric_sequence",
    "dash_polyline",
    "extrema_preserving_downsample",
    "finite_runs",
    "marching_squares",
    "uncertainty_hatch_indices",
]

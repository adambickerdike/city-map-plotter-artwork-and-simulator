from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, replace
from math import ceil, hypot, isfinite, sqrt

from .models import MapPlotterError, PlotStroke


Point = tuple[float, float]
BucketKey = Callable[[PlotStroke], Hashable]


@dataclass(frozen=True)
class TimingConfig:
    """Conservative physical timing assumptions for a plot estimate.

    ``lift_seconds`` represents one complete pen-up/pen-down cycle per drawable
    stroke. The safety factor covers acceleration, controller latency, and other
    short pauses that constant-speed distance calculations cannot model.
    """

    draw_speed_mm_s: float = 40.0
    travel_speed_mm_s: float = 100.0
    lift_seconds: float = 0.4
    safety_factor: float = 1.15

    def __post_init__(self) -> None:
        for name in ("draw_speed_mm_s", "travel_speed_mm_s"):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise MapPlotterError(
                    f"{name} must be a finite number greater than zero."
                )
        if not isfinite(self.lift_seconds) or self.lift_seconds < 0:
            raise MapPlotterError("lift_seconds must be a finite, non-negative number.")
        if not isfinite(self.safety_factor) or self.safety_factor < 1:
            raise MapPlotterError(
                "safety_factor must be a finite number of at least 1."
            )


@dataclass(frozen=True)
class PlotMetrics:
    pen_down_distance_mm: float
    pen_up_travel_mm: float
    stroke_count: int
    lift_count: int
    estimated_plot_seconds: float

    @property
    def estimated_plot_minutes(self) -> float:
        return self.estimated_plot_seconds / 60


@dataclass(frozen=True)
class OptimisationReport:
    before: PlotMetrics
    after: PlotMetrics
    bucket_count: int
    reversed_stroke_count: int
    empty_stroke_count: int
    timing: TimingConfig
    fallback_applied: bool = False
    algorithm: str = "exact-nearest-endpoint-with-uniform-grid-and-global-fallback"

    @property
    def pen_up_saved_mm(self) -> float:
        return self.before.pen_up_travel_mm - self.after.pen_up_travel_mm


@dataclass(frozen=True)
class _Choice:
    index: int
    reverse: bool
    distance_squared: float


def _validate_start_point(start_point: Point) -> None:
    if len(start_point) != 2 or not all(isfinite(value) for value in start_point):
        raise MapPlotterError("Plot start point must contain two finite coordinates.")


def _validate_strokes(strokes: Sequence[PlotStroke]) -> None:
    for stroke_index, stroke in enumerate(strokes):
        for point_index, point in enumerate(stroke.points):
            if len(point) != 2 or not all(isfinite(value) for value in point):
                raise MapPlotterError(
                    "Plot stroke coordinates must be finite pairs "
                    f"(stroke {stroke_index}, point {point_index})."
                )


def _distance_squared(left: Point, right: Point) -> float:
    return (right[0] - left[0]) ** 2 + (right[1] - left[1]) ** 2


def _stroke_length(stroke: PlotStroke) -> float:
    return sum(
        hypot(end[0] - start[0], end[1] - start[1])
        for start, end in zip(stroke.points, stroke.points[1:])
    )


def measure_plot(
    strokes: Sequence[PlotStroke],
    *,
    start_point: Point = (0.0, 0.0),
    timing: TimingConfig = TimingConfig(),
) -> PlotMetrics:
    """Measure physical travel without changing stroke order or direction."""

    _validate_start_point(start_point)
    _validate_strokes(strokes)
    cursor = start_point
    pen_down = 0.0
    pen_up = 0.0
    lift_count = 0
    for stroke in strokes:
        if not stroke.points:
            continue
        pen_up += hypot(
            stroke.points[0][0] - cursor[0], stroke.points[0][1] - cursor[1]
        )
        pen_down += _stroke_length(stroke)
        cursor = stroke.points[-1]
        lift_count += 1
    raw_seconds = (
        pen_down / timing.draw_speed_mm_s
        + pen_up / timing.travel_speed_mm_s
        + lift_count * timing.lift_seconds
    )
    return PlotMetrics(
        pen_down_distance_mm=pen_down,
        pen_up_travel_mm=pen_up,
        stroke_count=len(strokes),
        lift_count=lift_count,
        estimated_plot_seconds=raw_seconds * timing.safety_factor,
    )


def _is_closed(stroke: PlotStroke) -> bool:
    return len(stroke.points) > 2 and stroke.points[0] == stroke.points[-1]


def _choice_for(
    index: int, stroke: PlotStroke, cursor: Point
) -> tuple[tuple[float, int, int], _Choice]:
    start_distance = _distance_squared(cursor, stroke.points[0])
    reverse = False
    distance = start_distance
    if len(stroke.points) > 1 and not _is_closed(stroke):
        end_distance = _distance_squared(cursor, stroke.points[-1])
        if end_distance < start_distance:
            reverse = True
            distance = end_distance
    choice = _Choice(index=index, reverse=reverse, distance_squared=distance)
    return (distance, index, int(reverse)), choice


class _EndpointGrid:
    """A removable uniform-grid index over stroke endpoints.

    Nearest searches are exact: grid rings expand until their outer boundary is
    farther than the best endpoint already found. Construction uses O(n) memory.
    Spatially distributed inputs inspect a small local neighbourhood per stroke;
    the documented worst case remains O(n²) for highly clustered or adversarial
    geometry, avoiding an unbounded dependency solely for path ordering.
    """

    def __init__(self, items: dict[int, PlotStroke]) -> None:
        endpoints = [
            point
            for stroke in items.values()
            for point in (
                (stroke.points[0],)
                if len(stroke.points) == 1 or _is_closed(stroke)
                else (stroke.points[0], stroke.points[-1])
            )
        ]
        self._items = dict(items)
        self._side = max(1, ceil(sqrt(len(endpoints))))
        self._minimum_x = min(point[0] for point in endpoints)
        self._maximum_x = max(point[0] for point in endpoints)
        self._minimum_y = min(point[1] for point in endpoints)
        self._maximum_y = max(point[1] for point in endpoints)
        span_x = self._maximum_x - self._minimum_x
        span_y = self._maximum_y - self._minimum_y
        self._cell_width = span_x / self._side if span_x > 0 else 1.0
        self._cell_height = span_y / self._side if span_y > 0 else 1.0
        self._cells: dict[tuple[int, int], set[int]] = {}
        self._index_cells: dict[int, tuple[tuple[int, int], ...]] = {}
        for index, stroke in items.items():
            endpoint_cells = {
                self._cell_for(stroke.points[0]),
                self._cell_for(stroke.points[-1]),
            }
            self._index_cells[index] = tuple(sorted(endpoint_cells))
            for cell in endpoint_cells:
                self._cells.setdefault(cell, set()).add(index)

    def _cell_for(self, point: Point) -> tuple[int, int]:
        column = int((point[0] - self._minimum_x) / self._cell_width)
        row = int((point[1] - self._minimum_y) / self._cell_height)
        return (
            min(max(column, 0), self._side - 1),
            min(max(row, 0), self._side - 1),
        )

    def remove(self, index: int) -> None:
        self._items.pop(index)
        for cell in self._index_cells.pop(index):
            members = self._cells[cell]
            members.discard(index)
            if not members:
                del self._cells[cell]

    def _brute_force(self, cursor: Point) -> _Choice:
        return min(
            (
                _choice_for(index, stroke, cursor)
                for index, stroke in self._items.items()
            ),
            key=lambda item: item[0],
        )[1]

    def nearest(self, cursor: Point) -> _Choice:
        if not self._items:
            raise LookupError("No indexed strokes remain.")
        if not (
            self._minimum_x <= cursor[0] <= self._maximum_x
            and self._minimum_y <= cursor[1] <= self._maximum_y
        ):
            return self._brute_force(cursor)

        centre_column, centre_row = self._cell_for(cursor)
        maximum_radius = max(
            centre_column,
            self._side - 1 - centre_column,
            centre_row,
            self._side - 1 - centre_row,
        )
        seen: set[int] = set()
        best_key: tuple[float, int, int] | None = None
        best_choice: _Choice | None = None
        for radius in range(maximum_radius + 1):
            low_column = max(0, centre_column - radius)
            high_column = min(self._side - 1, centre_column + radius)
            low_row = max(0, centre_row - radius)
            high_row = min(self._side - 1, centre_row + radius)
            for column in range(low_column, high_column + 1):
                for row in range(low_row, high_row + 1):
                    if (
                        max(abs(column - centre_column), abs(row - centre_row))
                        != radius
                    ):
                        continue
                    for index in self._cells.get((column, row), ()):
                        if index in seen:
                            continue
                        seen.add(index)
                        key, choice = _choice_for(index, self._items[index], cursor)
                        if best_key is None or key < best_key:
                            best_key = key
                            best_choice = choice

            if best_choice is None:
                continue
            distances_to_unsearched: list[float] = []
            if low_column > 0:
                left = self._minimum_x + low_column * self._cell_width
                distances_to_unsearched.append(cursor[0] - left)
            if high_column < self._side - 1:
                right = self._minimum_x + (high_column + 1) * self._cell_width
                distances_to_unsearched.append(right - cursor[0])
            if low_row > 0:
                top = self._minimum_y + low_row * self._cell_height
                distances_to_unsearched.append(cursor[1] - top)
            if high_row < self._side - 1:
                bottom = self._minimum_y + (high_row + 1) * self._cell_height
                distances_to_unsearched.append(bottom - cursor[1])
            if (
                not distances_to_unsearched
                or best_choice.distance_squared <= min(distances_to_unsearched) ** 2
            ):
                return best_choice

        return best_choice if best_choice is not None else self._brute_force(cursor)


def _copy_stroke(stroke: PlotStroke, *, reverse: bool) -> PlotStroke:
    points = list(reversed(stroke.points)) if reverse else list(stroke.points)
    return replace(stroke, points=points, tags=dict(stroke.tags))


def _default_bucket_key(stroke: PlotStroke) -> Hashable:
    return stroke.layer


def optimise_strokes(
    strokes: Iterable[PlotStroke],
    *,
    bucket_key: BucketKey | None = None,
    start_point: Point = (0.0, 0.0),
    timing: TimingConfig = TimingConfig(),
) -> tuple[list[PlotStroke], OptimisationReport]:
    """Order strokes by nearest endpoint inside first-seen semantic buckets.

    Paths are never joined, split, or moved between buckets. Open paths may be
    reversed; closed paths retain their winding and starting vertex. The returned
    strokes—including empty strokes—are copies, so callers can safely retain the
    original list and mutable point/tag containers.

    The ``before`` report measures first-seen bucket order with original order
    inside each bucket. This isolates the benefit of nearest-neighbour ordering
    from any pre-existing interleaving of semantic layers. If the deterministic
    greedy route increases pen-up travel, the copied baseline is returned instead.
    """

    source = list(strokes)
    _validate_start_point(start_point)
    _validate_strokes(source)
    resolve_bucket = bucket_key or _default_bucket_key
    buckets: dict[Hashable, list[tuple[int, PlotStroke]]] = {}
    for index, stroke in enumerate(source):
        key = resolve_bucket(stroke)
        try:
            buckets.setdefault(key, []).append((index, stroke))
        except TypeError as exc:
            raise MapPlotterError(
                "Plot optimisation bucket keys must be hashable."
            ) from exc

    baseline = [stroke for items in buckets.values() for _, stroke in items]
    before = measure_plot(baseline, start_point=start_point, timing=timing)
    output: list[PlotStroke] = []
    cursor = start_point
    reversed_count = 0
    empty_count = 0
    for items in buckets.values():
        drawable = {index: stroke for index, stroke in items if stroke.points}
        empty = [(index, stroke) for index, stroke in items if not stroke.points]
        if drawable:
            grid = _EndpointGrid(drawable)
            while drawable:
                choice = grid.nearest(cursor)
                stroke = drawable.pop(choice.index)
                grid.remove(choice.index)
                copied = _copy_stroke(stroke, reverse=choice.reverse)
                output.append(copied)
                cursor = copied.points[-1]
                reversed_count += int(choice.reverse)
        for _, stroke in empty:
            output.append(_copy_stroke(stroke, reverse=False))
            empty_count += 1

    after = measure_plot(output, start_point=start_point, timing=timing)
    fallback_applied = after.pen_up_travel_mm > before.pen_up_travel_mm + 1e-9
    if fallback_applied:
        output = [_copy_stroke(stroke, reverse=False) for stroke in baseline]
        after = before
        reversed_count = 0
    report = OptimisationReport(
        before=before,
        after=after,
        bucket_count=len(buckets),
        reversed_stroke_count=reversed_count,
        empty_stroke_count=empty_count,
        timing=timing,
        fallback_applied=fallback_applied,
    )
    return output, report

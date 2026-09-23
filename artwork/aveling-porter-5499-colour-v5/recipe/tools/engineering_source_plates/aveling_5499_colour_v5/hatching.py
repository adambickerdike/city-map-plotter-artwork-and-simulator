"""Plotter hatching primitives for the No. 5499 hatched colour edition.

Every function is deterministic and works in physical millimetres.  Fill
strokes are produced as separate single-pass paths: no connector is drawn
along a region edge, so a hatch never leaves a stitched border beside the
black outline.  Rows are returned in serpentine order so consecutive strokes
start where the previous one finished, which keeps pen-up travel to about one
hatch pitch.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence

from shapely import prepared
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge

from city_map_plotter.vector_path import CubicSegment, LineSegment, VectorPath

GRID_MM = 0.001


def snap(value: float) -> float:
    """Round onto the binding 0.001 mm SVG coordinate grid."""

    return round(value / GRID_MM) * GRID_MM


def lines_of(geometry: BaseGeometry | None) -> list[LineString]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    out: list[LineString] = []
    for part in getattr(geometry, "geoms", ()):
        out.extend(lines_of(part))
    return out


def polygons_of(geometry: BaseGeometry | None) -> list[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    out: list[Polygon] = []
    for part in getattr(geometry, "geoms", ()):
        out.extend(polygons_of(part))
    return out


def lattice(start: float, stop: float, pitch: float, anchor: float = 0.0) -> list[float]:
    """Uniform offsets on a global lattice, so split cells of one part align."""

    first = math.ceil((start - anchor) / pitch - 1e-9)
    last = math.floor((stop - anchor) / pitch + 1e-9)
    return [anchor + index * pitch for index in range(first, last + 1)]


def graded(start: float, stop: float, coverage: Callable[[float], float], nib: float,
           step: float = 0.005) -> list[float]:
    """Offsets whose local density gives ink coverage ``coverage(u)`` for a nib.

    The integral of ``coverage/nib`` between neighbouring offsets is one, so a
    0.25 mm line at coverage 0.5 lands every 0.5 mm and at 0.125 every 2 mm.
    """

    offsets: list[float] = []
    accumulated = 0.5
    position = start
    steps = int(math.ceil((stop - start) / step))
    for index in range(steps):
        position = start + index * step
        accumulated += max(0.0, coverage(position + step / 2)) / nib * step
        while accumulated >= 1.0:
            accumulated -= 1.0
            offsets.append(position + step)
    return offsets


def _axes(angle_deg: float) -> tuple[tuple[float, float], tuple[float, float]]:
    angle = math.radians(angle_deg)
    direction = (math.cos(angle), math.sin(angle))
    normal = (-direction[1], direction[0])
    return direction, normal


def normal_range(region: BaseGeometry, angle_deg: float) -> tuple[float, float]:
    _, normal = _axes(angle_deg)
    values = [x * normal[0] + y * normal[1]
              for polygon in polygons_of(region) for x, y in polygon.exterior.coords]
    return min(values), max(values)


def clip_parallel(region: BaseGeometry, angle_deg: float,
                  offsets: Iterable[float]) -> list[tuple[float, LineString]]:
    """Clip the lines ``p . normal = u`` (one per offset) to ``region``."""

    if region.is_empty:
        return []
    direction, normal = _axes(angle_deg)
    coordinates = [c for polygon in polygons_of(region) for c in polygon.exterior.coords]
    along = [x * direction[0] + y * direction[1] for x, y in coordinates]
    across = [x * normal[0] + y * normal[1] for x, y in coordinates]
    low, high = min(across), max(across)
    t0, t1 = min(along) - 1.0, max(along) + 1.0
    fast = prepared.prep(region)
    rows: list[tuple[float, LineString]] = []
    for offset in offsets:
        if offset < low or offset > high:
            continue
        line = LineString([
            (normal[0] * offset + direction[0] * t0, normal[1] * offset + direction[1] * t0),
            (normal[0] * offset + direction[0] * t1, normal[1] * offset + direction[1] * t1),
        ])
        if not fast.intersects(line):
            continue
        for piece in lines_of(region.intersection(line)):
            rows.append((offset, piece))
    rows.sort(key=lambda row: (row[0], row[1].coords[0][0] * direction[0]
                               + row[1].coords[0][1] * direction[1]))
    return rows


def serpentine(rows: Sequence[tuple[float, LineString]]) -> list[LineString]:
    """Alternate stroke direction row by row to keep pen-up travel short."""

    out: list[LineString] = []
    flip = False
    previous = None
    for offset, piece in rows:
        if previous is not None and offset != previous:
            flip = not flip
        previous = offset
        coords = list(piece.coords)
        out.append(LineString(coords[::-1] if flip else coords))
    return out


def link(rows: Sequence[tuple[float, LineString]], region: BaseGeometry,
         max_gap: float) -> list[LineString]:
    """Join consecutive hatch rows into zigzags along the region edge.

    A row piece is joined to the nearest unused piece of the next row when the
    straight connector is no longer than ``max_gap`` and lies inside
    ``region`` (the already-eroded fill area), so a connector never reaches
    nearer to black ink than the hatch itself.
    """

    if not rows:
        return []
    offsets = sorted({offset for offset, _ in rows})
    position = {offset: index for index, offset in enumerate(offsets)}
    by_row: list[list[LineString]] = [[] for _ in offsets]
    for offset, piece in rows:
        by_row[position[offset]].append(piece)
    used = [[False] * len(row) for row in by_row]
    inside = prepared.prep(region.buffer(1e-3))
    chains = []
    for row in range(len(by_row)):
        for index in range(len(by_row[row])):
            if used[row][index]:
                continue
            used[row][index] = True
            coords = list(by_row[row][index].coords)
            current = row
            while current + 1 < len(by_row):
                end = coords[-1]
                best = None
                for candidate_index, candidate in enumerate(by_row[current + 1]):
                    if used[current + 1][candidate_index]:
                        continue
                    candidate_coords = list(candidate.coords)
                    for option in (candidate_coords, candidate_coords[::-1]):
                        gap = math.dist(end, option[0])
                        if gap <= max_gap and (best is None or gap < best[0]):
                            best = (gap, candidate_index, option)
                if best is None:
                    break
                gap, candidate_index, option = best
                if gap > 1e-9 and not inside.contains(LineString([end, option[0]])):
                    break
                used[current + 1][candidate_index] = True
                coords.extend(option if gap > 1e-9 else option[1:])
                current += 1
            chains.append(LineString(coords))
    return chains


def arc_angles(centre: tuple[float, float], radius: float,
               piece: LineString) -> tuple[float, float]:
    """Return the start and signed sweep of an arc polyline about ``centre``."""

    cx, cy = centre
    coords = list(piece.coords)
    angles = [math.atan2(y - cy, x - cx) for x, y in coords]
    unwrapped = [angles[0]]
    for angle in angles[1:]:
        delta = angle - unwrapped[-1]
        while delta > math.pi:
            delta -= 2 * math.pi
        while delta < -math.pi:
            delta += 2 * math.pi
        unwrapped.append(unwrapped[-1] + delta)
    return unwrapped[0], unwrapped[-1] - unwrapped[0]


def clip_concentric(region: BaseGeometry, centre: tuple[float, float],
                    radii: Iterable[float], segment_mm: float = 0.15,
                    spans: Callable[[int, float], tuple[float, float]] | None = None
                    ) -> list[tuple[float, LineString, float, float]]:
    """Clip circles (or arcs) about ``centre`` to ``region``.

    ``spans(i, r)`` may return ``(start_angle, sweep)`` in radians to draw only
    part of ring ``i``.  Returns ``(radius, polyline, start_angle, sweep)`` so
    the caller can emit each arc as exact cubic Beziers.
    """

    cx, cy = centre
    fast = prepared.prep(region)
    rows = []
    for ring_index, radius in enumerate(radii):
        if radius <= 0.2:
            continue
        a0, sweep = spans(ring_index, radius) if spans else (0.0, 2 * math.pi)
        count = max(48, int(abs(sweep) * radius / segment_mm))
        ring = LineString([(cx + radius * math.cos(a0 + sweep * i / count),
                            cy + radius * math.sin(a0 + sweep * i / count))
                           for i in range(count + 1)])
        if not fast.intersects(ring):
            continue
        pieces = lines_of(region.intersection(ring))
        if len(pieces) > 1:
            pieces = lines_of(linemerge(pieces))
        for piece in pieces:
            start, sweep = arc_angles(centre, radius, piece)
            rows.append((radius, piece, start, sweep))
    return rows


def radial_extent(region: BaseGeometry, centre: tuple[float, float]) -> tuple[float, float]:
    cx, cy = centre
    from shapely.geometry import Point
    point = Point(cx, cy)
    near = 0.0 if region.contains(point) else region.distance(point)
    far = max(math.hypot(x - cx, y - cy)
              for polygon in polygons_of(region) for x, y in polygon.exterior.coords)
    return near, far


def line_path(piece: LineString) -> VectorPath | None:
    """A snapped straight or polyline stroke; None if rounding collapses it."""

    points: list[tuple[float, float]] = []
    for x, y in piece.coords:
        point = (snap(x), snap(y))
        if not points or point != points[-1]:
            points.append(point)
    if len(points) < 2:
        return None
    return VectorPath(points[0], tuple(LineSegment(p) for p in points[1:]))


def arc_path(centre: tuple[float, float], radius: float, start: float,
             sweep: float) -> VectorPath | None:
    """Exact circular arc as cubic Beziers of at most 45 degrees each."""

    if abs(sweep) * radius < 0.01:
        return None
    full = abs(abs(sweep) - 2 * math.pi) < 1e-3
    if full:
        sweep = math.copysign(2 * math.pi, sweep)
    pieces = max(1, int(math.ceil(abs(sweep) / (math.pi / 4))))
    step = sweep / pieces
    k = 4.0 / 3.0 * math.tan(step / 4.0)
    cx, cy = centre

    def at(angle: float) -> tuple[float, float]:
        return cx + radius * math.cos(angle), cy + radius * math.sin(angle)

    start_point = at(start)
    segments = []
    for index in range(pieces):
        a0 = start + index * step
        a1 = a0 + step
        p0, p3 = at(a0), at(a1)
        c1 = (p0[0] - k * radius * math.sin(a0), p0[1] + k * radius * math.cos(a0))
        c2 = (p3[0] + k * radius * math.sin(a1), p3[1] - k * radius * math.cos(a1))
        segments.append(CubicSegment((snap(c1[0]), snap(c1[1])), (snap(c2[0]), snap(c2[1])),
                                     (snap(p3[0]), snap(p3[1]))))
    start_snapped = (snap(start_point[0]), snap(start_point[1]))
    if full:
        last = segments[-1]
        segments[-1] = CubicSegment(last.control_1, last.control_2, start_snapped)
    return VectorPath(start_snapped, tuple(segments), closed=False)


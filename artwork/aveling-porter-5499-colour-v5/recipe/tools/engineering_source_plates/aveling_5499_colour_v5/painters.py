"""Hatch painters: how one part's paper cells become pen lines.

Every coloured mark in this edition is a single-pass pen line.  A painter
receives a part's paper cells and returns ``Stroke`` records.  Each pen's
area is the part eroded by ``nib/2 + GAP - BASE`` so no coloured ink comes
nearer than ``GAP_MM`` to black ink.

The conventions are those of a coloured engineering drawing:

* one hatch angle per surface, with evenly spaced lines;
* darker materials take extra lines laid between the base lines at the same
  angle, never a crossing mesh;
* cylinders are modelled with graded lines along the barrel, lit from the
  upper left;
* turned parts (hubs, rims, the flywheel) are drawn as evenly spaced
  concentric circles all the way round;
* spokes, bars and other strips are ruled with a fixed number of lines
  running exactly parallel to their edges.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import LineString
from shapely.ops import unary_union

from tools.engineering_source_plates.aveling_5499_colour_v5 import hatching as H
from tools.engineering_source_plates.aveling_5499_colour_v5.inventory import GOLD_NIB_MM, GOLD_PEN
from tools.engineering_source_plates.aveling_5499_colour_v5.regions import BASE_MM, GAP_MM

NIB = {'green-0-25': 0.25, 'red-0-25': 0.25, 'grey-0-25': 0.25, 'black-0-25': 0.25, GOLD_PEN: GOLD_NIB_MM}
MIN_FILL_STROKE_MM = 1.2
EROSION_MARGIN_MM = 0.006   # absorbs polygon-buffer chords and 0.001 mm rounding
STRIP_WIDTH_MM = 2.2        # eroded width at or below which a piece is a strip
STRIP_ASPECT = 2.5
CURVED_STRIP_WIDTH_MM = 1.6  # mean width (2 area / perimeter) of a bent strip
MIN_SHADE_AREA_MM2 = 4.0     # smaller pieces take the base tone only
EDGE_INSET = 0.01            # ruled lines sit this fraction inside the edges


def minimum_length(pen: str) -> float:
    return max(3.0 * NIB[pen], MIN_FILL_STROKE_MM)


def erosion(pen: str) -> float:
    return NIB[pen] / 2 + GAP_MM - BASE_MM + EROSION_MARGIN_MM


def fill_region(cells, pen: str):
    return unary_union(cells).buffer(-erosion(pen), quad_segs=16)


@dataclass
class Stroke:
    pen: str
    role: str
    line: LineString | None = None
    arc: tuple | None = None   # (centre, radius, start, sweep)

    @property
    def planning(self) -> LineString:
        if self.line is not None:
            return self.line
        centre, radius, start, sweep = self.arc
        count = max(8, int(abs(sweep) * radius / 0.1))
        return LineString([(centre[0] + radius * math.cos(start + sweep * i / count),
                            centre[1] + radius * math.sin(start + sweep * i / count))
                           for i in range(count + 1)])

    @property
    def length(self) -> float:
        if self.arc is not None:
            return abs(self.arc[3]) * self.arc[1]
        return self.line.length


def emit(pen, rows, role, floor=None):
    floor = max(minimum_length(pen), floor or 0.0)
    return [Stroke(pen, role, line=p) for p in H.serpentine(rows) if p.length + 1e-9 >= floor]


def brightness_cylinder(v: float, light: float = 0.55) -> float:
    """Lambert brightness across a cylinder; v=-1 faces the light."""

    lz = math.sqrt(1 - light * light)
    return max(0.0, light * (-v) + lz * math.sqrt(max(0.0, 1 - v * v)))


def rect_axis(polygon) -> tuple[float, float, float]:
    """Long-axis angle (canonical, degrees), long side and short side."""

    c = list(polygon.minimum_rotated_rectangle.exterior.coords)
    e1 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
    e2 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
    long_edge, short_edge = (e1, e2) if math.hypot(*e1) >= math.hypot(*e2) else (e2, e1)
    angle = math.degrees(math.atan2(long_edge[1], long_edge[0]))
    while angle <= -90:
        angle += 180
    while angle > 90:
        angle -= 180
    return round(angle, 6), math.hypot(*long_edge), math.hypot(*short_edge)


def is_strip(polygon) -> bool:
    _, long, short = rect_axis(polygon)
    return short <= STRIP_WIDTH_MM and long >= STRIP_ASPECT * max(short, 1e-6)


def is_curved_strip(polygon) -> bool:
    """A thin piece that bends, so no single hatch direction runs along it."""

    return (not is_strip(polygon)
            and 2 * polygon.area / max(polygon.length, 1e-9) <= CURVED_STRIP_WIDTH_MM)


def axis_difference(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def cross_section(piece, direction, normal, t):
    """The longest interval of ``piece`` on the line ``p . direction = t``."""

    far = 1000.0
    line = LineString([(direction[0] * t - normal[0] * far, direction[1] * t - normal[1] * far),
                       (direction[0] * t + normal[0] * far, direction[1] * t + normal[1] * far)])
    best = None
    for part in H.lines_of(piece.intersection(line)):
        us = [x * normal[0] + y * normal[1] for x, y in part.coords]
        interval = (min(us), max(us))
        if best is None or interval[1] - interval[0] > best[1] - best[0]:
            best = interval
    return best


def edge_direction(piece) -> float:
    """The direction of a strip's long straight edges, in degrees.

    Boundary segments are binned by direction, weighted by length; a strip's
    two long sides share one direction and dominate.  This is robust to slanted
    ends, which tilt a minimum bounding rectangle away from the true edges.
    """

    ring = piece.exterior.simplify(0.03)
    coords = list(ring.coords)
    bins = [0.0] * 180
    edges = []
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-6:
            continue
        angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0
        edges.append((angle, length))
        for spread in (-2, -1, 0, 1, 2):
            bins[int(round(angle + spread)) % 180] += length * (1.0 - abs(spread) / 3.0)
    peak = max(range(180), key=lambda b: bins[b])
    # refine: length-weighted circular mean of edges within 4 degrees of the peak
    sx = sy = 0.0
    for angle, length in edges:
        if axis_difference(angle, peak) <= 4.0:
            sx += length * math.cos(math.radians(2 * angle))
            sy += length * math.sin(math.radians(2 * angle))
    refined = math.degrees(math.atan2(sy, sx)) / 2.0 if (sx or sy) else float(peak)
    while refined <= -90:
        refined += 180
    while refined > 90:
        refined -= 180
    return round(refined, 6)


@dataclass
class StripFit:
    """A strip's two long edges as straight lines in its own (t, u) frame."""

    angle: float
    left: tuple      # (u at t = 0, du/dt)
    right: tuple
    width: float     # typical cross-section width
    stations: int    # cross-sections at that width

    def u(self, which, t):
        a, b = self.left if which == 'left' else self.right
        return a + b * t

    def contains(self, point, tolerance=0.2) -> bool:
        direction, normal = H._axes(self.angle)
        t = point[0] * direction[0] + point[1] * direction[1]
        u = point[0] * normal[0] + point[1] * normal[1]
        return self.u('left', t) - tolerance <= u <= self.u('right', t) + tolerance

    def lines(self, piece, count):
        direction, normal = H._axes(self.angle)
        ts = [x * direction[0] + y * direction[1] for x, y in piece.exterior.coords]
        a, b = min(ts) - 1.0, max(ts) + 1.0
        out = []
        for i in range(count):
            f = 0.5 if count == 1 else EDGE_INSET + (1 - 2 * EDGE_INSET) * i / (count - 1)
            ua = self.u('left', a) + f * (self.u('right', a) - self.u('left', a))
            ub = self.u('left', b) + f * (self.u('right', b) - self.u('left', b))
            line = LineString([(direction[0] * a + normal[0] * ua, direction[1] * a + normal[1] * ua),
                               (direction[0] * b + normal[0] * ub, direction[1] * b + normal[1] * ub)])
            out.extend((i, part) for part in H.lines_of(piece.intersection(line)))
        return out


def fit_strip(piece, angle: float | None = None) -> StripFit | None:
    """Fit both long edges of a strip from cross-sections at its typical width."""

    if angle is None:
        angle = edge_direction(piece)
    direction, normal = H._axes(angle)
    ts = [x * direction[0] + y * direction[1] for x, y in piece.exterior.coords]
    t0, t1 = min(ts), max(ts)
    stations = []
    step = 0.3
    t = t0 + step
    while t < t1 - step:
        interval = cross_section(piece, direction, normal, t)
        if interval is not None:
            stations.append((t, interval[0], interval[1]))
        t += step
    if len(stations) < 2:
        return None
    widths = sorted(r - l for _, l, r in stations)
    typical = widths[len(widths) // 2]
    full = [s for s in stations if abs((s[2] - s[1]) - typical) <= 0.04 * typical + 0.02]
    if len(full) < 2:
        full = stations

    def fit(values):
        n = len(values)
        mt = sum(t for t, _ in values) / n
        mv = sum(v for _, v in values) / n
        var = sum((t - mt) ** 2 for t, _ in values)
        slope = 0.0 if var < 1e-9 else sum((t - mt) * (v - mv) for t, v in values) / var
        return (mv - slope * mt, slope)

    return StripFit(angle, fit([(t, l) for t, l, _ in full]), fit([(t, r) for t, _, r in full]),
                    typical, len(full))


def parallel_fit(piece, angle: float, width: float) -> StripFit:
    """Parallel edges ``width`` apart, centred on a piece: for slivers."""

    direction, normal = H._axes(angle)
    c = piece.representative_point()
    u = c.x * normal[0] + c.y * normal[1]
    return StripFit(angle, (u - width / 2, 0.0), (u + width / 2, 0.0), width, 0)


def ruled_lines(piece, count: int, angle: float | None = None):
    """``count`` lines spread evenly from edge to edge of one strip."""

    fit = fit_strip(piece, angle)
    return fit.lines(piece, count) if fit else []


def ruled_set(pieces, count: int):
    """Rule a set of strips (the spokes of one wheel) consistently.

    Pieces at the set's full width are ruled from their own fitted edges.  A
    sliver (a spoke root, or a spoke mostly hidden behind another part) takes
    the edges of the full strip whose band contains it, so its lines continue
    that strip's ruling; with no such strip it is ruled with parallel lines at
    the set's width, never with converging lines.
    """

    fits = [(piece, fit_strip(piece)) for piece in pieces]
    widths = sorted(f.width for _, f in fits if f and f.stations >= 10)
    if not widths:
        return [row for piece, f in fits if f for row in f.lines(piece, count)]
    width = widths[len(widths) // 2]

    def is_full(f):
        return (f is not None and f.stations >= 10 and abs(f.width - width) <= 0.08 * width
                and abs(f.left[1] - f.right[1]) < 0.02)

    full = [f for _, f in fits if is_full(f)]
    rows = []
    for piece, f in fits:
        if not is_full(f):
            point = piece.representative_point()
            owner = next((g for g in full if g.contains((point.x, point.y))), None)
            if owner is not None:
                f = owner
            else:
                f = parallel_fit(piece, f.angle if f else edge_direction(piece), width)
        rows.extend(f.lines(piece, count))
    return rows


def contour_strokes(piece, pen, pitch):
    """Lines parallel to a shape's edges, stepping inwards."""

    out = []
    step = 0
    while step < 60:
        inner = piece.buffer(-step * pitch, quad_segs=16) if step else piece
        rings = [r for q in H.polygons_of(inner) for r in (q.exterior, *q.interiors)]
        if not rings:
            break
        for ring in rings:
            line = LineString(ring.coords)
            if line.length + 1e-9 >= minimum_length(pen):
                out.append(Stroke(pen, 'base', line=line))
        step += 1
    return out


@dataclass
class Painter:
    kind: str
    params: dict = field(default_factory=dict)

    def describe(self) -> dict:
        def plain(value):
            if value is None or isinstance(value, (bool, int, float, str)):
                return value
            if isinstance(value, (list, tuple)):
                return [plain(v) for v in value]
            return f'<{type(value).__name__}>'
        record = {'painter': self.kind}
        for key, value in self.params.items():
            if key == 'mask' and value is not None:
                record[key] = 'lettering-halo'
            elif key == 'outline' and value is not None:
                record[key] = 'casting-silhouette'
            elif not callable(value):
                record[key] = plain(value)
        return record

    def paint(self, cells) -> list[Stroke]:
        return getattr(self, '_' + self.kind)(cells)

    def _interleave(self, area, angle, offsets):
        """Shade lines between the base lines, at the same angle."""

        shade = self.params.get('shade')
        if not shade or area.is_empty or len(offsets) < 2:
            return []
        spen, spitch = shade
        mean_pitch = (offsets[-1] - offsets[0]) / (len(offsets) - 1)
        every = max(1, int(round(spitch / mean_pitch)))
        mids = [(a + b) / 2 for a, b in zip(offsets, offsets[1:])][::every]
        return emit(spen, H.clip_parallel(area, angle, mids), 'shade')

    def _along(self, piece, pen, angle, pitch):
        """Even lines along a strip, with shade lines between them."""

        low, high = H.normal_range(piece, angle)
        middle = (low + high) / 2
        count = max(1, int(math.floor((high - low) / pitch)) + 1)
        offsets = [middle + (i - (count - 1) / 2) * pitch for i in range(count)]
        out = emit(pen, H.clip_parallel(piece, angle, offsets), 'base')
        if piece.area >= MIN_SHADE_AREA_MM2 or is_strip(piece):
            out += self._interleave(piece, angle, offsets)
        return out

    # parallel hatch graded gently along its normal, optional shade lines
    def _flat(self, cells):
        p = self.params
        pen, angle = p['pen'], p['angle']
        low, high = H.normal_range(unary_union(cells), angle)
        c0, c1 = p['coverage']
        span = max(high - low, 1e-6)
        cov = lambda u: c0 + (c1 - c0) * min(1.0, max(0.0, (u - low) / span))
        _, normal = H._axes(angle)
        out, wide, small = [], [], []
        for piece in H.polygons_of(fill_region(cells, pen)):
            u = piece.centroid.x * normal[0] + piece.centroid.y * normal[1]
            if is_strip(piece):
                out += self._along(piece, pen, rect_axis(piece)[0], NIB[pen] / cov(u))
            elif is_curved_strip(piece):
                out += contour_strokes(piece, pen, NIB[pen] / cov(u))
            elif piece.area < MIN_SHADE_AREA_MM2:
                small.append(piece)
            else:
                wide.append(piece)
        offsets = H.graded(low, high, cov, NIB[pen])
        for group, shaded in ((wide, True), (small, False)):
            if group:
                area = unary_union(group)
                out += emit(pen, H.clip_parallel(area, angle, offsets), 'base')
                if shaded:
                    out += self._interleave(area, angle, offsets)
        return out

    # a cylinder seen side-on, lines parallel to its axis
    def _cylinder(self, cells):
        p = self.params
        pen, angle = p['pen'], p['angle']
        a, b = p['span']              # normal-coordinate extent of the whole cylinder
        cmin, cmax = p['coverage']
        centre, radius = (a + b) / 2, (b - a) / 2
        facing = p.get('facing', 1.0)   # +1: lower normal values face the light

        def darkness(u):
            v = max(-1.0, min(1.0, (u - centre) / radius)) * facing
            return 1 - brightness_cylinder(v)

        cov = lambda u: cmin + (cmax - cmin) * darkness(u) ** 1.2
        tiers = p.get('shade_tiers')   # (pen, [(darkness threshold, every nth gap), ...])
        _, normal = H._axes(angle)
        out, wide = [], []
        for piece in H.polygons_of(fill_region(cells, pen)):
            axis = rect_axis(piece)[0]
            if is_strip(piece) and axis_difference(axis, angle) > 20:
                # an end flange crossing the cylinder lines: rule it along its length
                u = piece.centroid.x * normal[0] + piece.centroid.y * normal[1]
                pitch = NIB[pen] / cov(u)
                low, high = H.normal_range(piece, axis)
                count = max(1, int(math.floor((high - low) / pitch)) + 1)
                out += emit(pen, ruled_lines(piece, count, axis), 'base')
            else:
                wide.append(piece)
        if not wide:
            return out
        area = unary_union(wide)
        base = H.graded(a, b, cov, NIB[pen])
        out += emit(pen, H.clip_parallel(area, angle, base), 'base')
        if tiers:
            spen, levels = tiers
            mids = [(u0 + u1) / 2 for u0, u1 in zip(base, base[1:])]
            chosen = []
            for i, m in enumerate(mids):
                for threshold, every in levels:
                    if darkness(m) > threshold and i % every == 0:
                        chosen.append(m)
                        break
            out += emit(spen, H.clip_parallel(area, angle, chosen), 'shade')
        return out

    # bars and columns: even lines along each piece (own axis or fixed angle)
    def _axial(self, cells):
        p = self.params
        pen, pitch = p['pen'], p['pitch']
        out = []
        for piece in H.polygons_of(fill_region(cells, pen)):
            angle = p['angle'] if p.get('angle') is not None else rect_axis(piece)[0]
            if p.get('single'):
                offsets = [sum(H.normal_range(piece, angle)) / 2]
                out += emit(pen, H.clip_parallel(piece, angle, offsets), 'base')
            else:
                out += self._along(piece, pen, angle, pitch)
        return out

    # spokes: a fixed number of lines ruled exactly parallel to both edges;
    # ``pattern`` names the pen of each line across the spoke
    def _ruled(self, cells):
        p = self.params
        pattern = p['pattern']
        pens = sorted(set(pattern), key=pattern.index)
        out = []
        rows = ruled_set(H.polygons_of(fill_region(cells, pens[0])), len(pattern))
        for pen in pens:
            mine = [(i, part) for i, part in rows if pattern[i] == pen]
            out += emit(pen, mine, 'base' if pen == pattern[0] else 'shade')
        return out

    # rings about a centre: evenly spaced full circles, clipped to the part
    def _concentric(self, cells):
        p = self.params
        pen, centre, pitch = p['pen'], p['centre'], p['pitch']
        out = []
        region = fill_region(cells, pen)
        if region.is_empty:
            return out
        near, far = H.radial_extent(region, centre)
        radii = H.lattice(near, far, pitch, p.get('phase', 0.0))
        for radius, piece, start, sweep in H.clip_concentric(region, centre, radii):
            if abs(sweep) * radius + 1e-9 >= minimum_length(pen):
                out.append(Stroke(pen, 'base', arc=(centre, radius, start, sweep)))
        shade = p.get('shade')
        if shade:
            spen, every = shade
            mids = [r + pitch / 2 for r in radii]
            chosen = [r for i, r in enumerate(mids) if i % every == 0]
            for radius, piece, start, sweep in H.clip_concentric(fill_region(cells, spen), centre, chosen):
                if abs(sweep) * radius + 1e-9 >= minimum_length(spen):
                    out.append(Stroke(spen, 'shade', arc=(centre, radius, start, sweep)))
        return out

    # parallel lines on a fixed lattice with an optional knockout mask
    def _masked(self, cells):
        p = self.params
        pen, angle, pitch = p['pen'], p['angle'], p['pitch']
        region = fill_region(cells, pen)
        if p.get('mask') is not None:
            region = region.difference(p['mask'])
        if p.get('open_mm'):
            # drop slivers too narrow to hold a whole ruled line, such as the
            # thin crescent between a curved heading and the plate rim
            region = region.buffer(-p['open_mm'], quad_segs=16).buffer(p['open_mm'], quad_segs=16)
        if region.is_empty:
            return []
        low, high = H.normal_range(region, angle)
        offsets = H.lattice(low, high, pitch, p.get('anchor', 0.0))
        return emit(pen, H.clip_parallel(region, angle, offsets), 'base', p.get('min_length'))

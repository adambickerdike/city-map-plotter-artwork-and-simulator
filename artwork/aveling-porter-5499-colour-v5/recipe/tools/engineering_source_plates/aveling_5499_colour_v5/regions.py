"""Paper cells between the frozen revision-14 outlines.

The black outline of every source path is buffered by its drawn half-width
plus ``BASE_MM``.  Whatever paper remains is split into connected
cells: the enclosed faces of the drawing.  Colour is only ever placed inside a
cell, eroded far enough that its ink edge keeps ``GAP_MM`` of white paper from
the black ink edge.  The two wheel faces are the only cells divided without a
drawn line, at a circle just outside the spoke openings, so the spokes and
their iron rim can take different inks.  The brass bands' inner lines are
drawn in Gold, not Black (see ``BAND_INNER_LINES``), so they do not divide
the paper: each band is one cell between its two black edges.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from shapely import STRtree
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from city_map_plotter.technical_assets import parse_absolute_path_data
from city_map_plotter.vector_path import VectorPath

from tools.engineering_source_plates.aveling_5499_colour_v5.hatching import line_path, polygons_of
from tools.engineering_source_plates.aveling_5499_colour_v5.inventory import BLACK_NIB_MM, BLACK_PEN, GOLD_NIB_MM, GOLD_PEN

NS = '{http://www.w3.org/2000/svg}'
PAGE = (420.0, 297.0)
BASE_MM = 0.15
GAP_MM = 0.22
FLATTEN_MM = 0.005
# The blueprint's White 0.30/0.40/0.50 mm strokes become black lines 0.25,
# 0.40 and 0.60 mm wide, as in colour edition 1.  The engine's own linework is
# drawn one weight heavier, so its outlines stay strong and black beside the
# colour: fine lines 0.40 mm wide, principal outlines 0.60 mm and the roller
# tyres 1.00 mm.  Small or delicate features and small assemblies keep the
# finer weight so they neither close up nor squeeze out their colour.
# Lettering, the Invicta horse, the worksplate and the sheet furniture keep
# their original weights.  The studio's only black pen is 0.25 mm, so every
# heavier line is built from 0.25 mm strokes (see ``outline_strokes``).
WEIGHT_MM = {'fine': 0.25, 'engine': 0.4, 'principal': 0.6, 'silhouette': 1.0}
BASE_WEIGHT = {0.3: 'fine', 0.4: 'engine', 0.5: 'principal'}
HEAVIER_WEIGHT = {0.3: 'engine', 0.4: 'principal', 0.5: 'silhouette'}
BAND_GOLD = 'band-gold'
ENGINE_ROLE = 'side-elevation-component'
FINE_FEATURE_TOKENS = frozenset({'fastener', 'washer', 'rivet', 'bolt', 'hexagon', 'thread', 'pin',
                                 'cotter', 'sight', 'coil', 'key', 'hole'})
# Small assemblies keep their original weights so their colour stays visible
# between the lines: chains, scrapers, valves, fittings and controls.
FINE_COMPONENTS = frozenset({'near-steering-chain', 'far-steering-chain', 'rear-brake-and-scraper',
                             'rear-forward-scraper', 'front-scraper', 'feed-control-and-pipe',
                             'steam-valves-and-lubricator', 'driver-controls', 'motion-top-linkage',
                             'under-boiler-fittings', 'rear-axle-and-drive-pin', 'quadrant-lower-blade',
                             'far-steering-column'})
SMALL_FEATURE_MM = 3.0      # features smaller than this keep the finer weight
# Each brass boiler band is drawn as a black edge either side and an upright
# inner line only 1.08 mm from its front edge: too close for a 0.40 mm Gold
# line to fit beside it with its white gap, which left that side of every band
# white.  These inner lines (by revision-14 model path index) are therefore
# drawn in Gold, unchanged in shape, as one of the band's own gold lines, so
# the whole band reads as a gold bar between its two black edges.
BAND_INNER_LINES = frozenset({'150', '151', '156', '159'})
BAND_COMPONENT = 'boiler-and-smokebox'

# The regulator rod runs from the reversing lever on the platform, behind the
# flywheel, to the front.  Revision 14 drew it sloping behind the lever, level
# at a different height beyond the flywheel, and ending in a shallow diagonal
# that notched into the motion plate beside the lubricator.  This edition
# redraws it as one straight, level rod 1.82 mm thick (106.655 to 108.475 mm)
# from the lever boss to a rounded bend that turns it straight down onto the
# motion plate; the brackets it passes through open to match.  These are the
# only source paths changed, keyed by revision-14 model path index: a new
# ``d``, or None where the old diagonal behind the lubricator pipe is removed.
# The motion plate's top edge, which the old diagonal had notched, now runs
# unbroken from the lubricator's base.
ROD_TOP_Y, ROD_BOTTOM_Y = 106.655, 108.475
GEOMETRY_EDITS = {
    '341': 'M 276.601,106.655 L 296.395,106.655',
    '342': 'M 297.004,108.475 L 277.408,108.475',
    '344': ('M 299.147,104.575 L 300.295,104.575 C 300.439,104.575 300.576,104.629 300.677,104.727 '
            'C 300.778,104.824 300.835,104.957 300.835,105.095 L 300.835,114.455 '
            'C 300.835,114.593 300.778,114.725 300.677,114.822 C 300.576,114.92 300.439,114.975 300.295,114.975 '
            'L 295.436,114.975 C 295.293,114.975 295.156,114.92 295.055,114.822 '
            'C 294.953,114.725 294.897,114.593 294.897,114.455 L 294.897,108.475'),
    '345': ('M 294.897,106.655 L 294.897,105.095 C 294.897,104.957 294.953,104.824 295.055,104.727 '
            'C 295.156,104.629 295.293,104.575 295.436,104.575 L 297.504,104.575'),
    '202': ('M 279.863,108.475 L 279.863,113.935 C 279.863,114.142 279.778,114.34 279.626,114.486 '
            'C 279.525,114.584 279.4,114.653 279.264,114.688'),
    '203': ('M 276.072,105.615 L 279.054,105.615 C 279.268,105.615 279.474,105.697 279.626,105.843 '
            'C 279.778,105.989 279.863,106.188 279.863,106.395 L 279.863,106.655'),
    '206': 'M 182.49,114.195 L 182.49,109.365 C 182.49,107.868 183.703,106.655 185.2,106.655 L 216.2,106.655',
    '209': 'M 215.392,108.475 L 185.2,108.475 C 184.708,108.475 184.31,108.873 184.31,109.365 L 184.31,114.195',
    '194': 'M 174.854,114.195 L 179.539,114.195 L 179.688,114.195 L 192.479,114.195',
    '207': None,
    '208': None,
}


def black_weight(attributes: dict, width: float, path: VectorPath) -> str:
    """The black weight for one source path (see the note above)."""

    base = BASE_WEIGHT[width]
    if attributes.get('data-role') != ENGINE_ROLE:
        return base
    tokens = set(re.split(r'[-:]', attributes.get('data-feature') or ''))
    if attributes.get('data-component') in FINE_COMPONENTS or tokens & FINE_FEATURE_TOKENS:
        return base
    heavy = HEAVIER_WEIGHT[width]
    bounds = path.bounds()
    if max(bounds.width, bounds.height) < SMALL_FEATURE_MM:
        return base
    if path.length(0.001) < 3 * WEIGHT_MM[heavy] + 0.3:
        return base
    return heavy


def is_band_inner_line(attributes: dict) -> bool:
    return attributes.get('data-model-path-index') in BAND_INNER_LINES


def outline_weight(attributes: dict, width: float, path: VectorPath) -> str:
    """The weight of one source path: Gold for a brass band's inner line,
    otherwise its black weight."""

    if is_band_inner_line(attributes):
        bounds = path.bounds()
        if attributes.get('data-component') != BAND_COMPONENT or bounds.width > 1e-6 or bounds.height < 3.0:
            raise ValueError(f"band inner line {attributes.get('data-model-path-index')} is not an upright "
                             f'{BAND_COMPONENT} line')
        return BAND_GOLD
    return black_weight(attributes, width, path)


def stroke_rings(width: float, nib: float = BLACK_NIB_MM, pitch_ratio: float = 0.75):
    """How a black line ``width`` wide is drawn with the ``nib`` pen.

    Returns the distances from the line's own path of the closed loops drawn
    around it, outermost first, and whether the path itself is drawn too.  A
    loop at distance ``d`` is the outline of the path buffered by ``d``; the
    pen following it inks from ``d - nib/2`` to ``d + nib/2`` on both sides.
    Loops are spaced no more than ``pitch_ratio`` nibs apart, so the strokes
    overlap and the line is solid out to exactly ``width / 2`` either side,
    with round ends and joins, like one broad nib.
    """

    half = (width - nib) / 2
    if half <= 1e-9:
        return [], True
    levels = math.ceil(half / (pitch_ratio * nib) - 1e-9)
    rings = [half * (levels - j) / levels for j in range(levels)]
    return rings, rings[-1] > nib / 2 + 1e-9


@dataclass
class SourcePath:
    index: int
    attributes: dict[str, str]
    path: VectorPath
    points: list[tuple[float, float]]
    source_width: float
    weight: str

    @property
    def nib(self) -> float:
        """The width of the ink this outline lays down."""
        return GOLD_NIB_MM if self.weight == BAND_GOLD else WEIGHT_MM[self.weight]

    @property
    def pen_id(self) -> str:
        return GOLD_PEN if self.weight == BAND_GOLD else BLACK_PEN

    @property
    def is_black(self) -> bool:
        return self.weight != BAND_GOLD

    @property
    def line(self) -> LineString:
        return LineString(self.points)

    def get(self, key: str) -> str | None:
        return self.attributes.get(key)


# The sheet frame sits exactly on the edge of the plotter-safe area, so its
# extra weight is built inward from its path rather than either side of it.
INWARD_ROLES = frozenset({'outer-border'})


def inward_band(source: SourcePath, nib: float = BLACK_NIB_MM):
    """The ink band of an inward-built frame: from half a nib outside its
    path to its full width inside."""

    area = Polygon(source.points).buffer(0)
    return area.buffer(nib / 2, quad_segs=16).difference(area.buffer(-(source.nib - nib / 2), quad_segs=16))


def outline_strokes(source: SourcePath) -> list[tuple[str, VectorPath]]:
    """The pen strokes that draw one outline: (kind, path) pairs.

    A line one nib wide is its own path.  A heavier black line is the closed
    loops of ``stroke_rings`` around the path, plus the path itself where the
    loops leave its middle open.  The sheet frame (``INWARD_ROLES``) is its
    path plus inner loops, spaced like the others, out to its full width.
    """

    if not source.is_black:
        return [('path', source.path)]
    if source.get('data-role') in INWARD_ROLES and source.nib > BLACK_NIB_MM:
        area = Polygon(source.points).buffer(0)
        depth = source.nib - BLACK_NIB_MM
        levels = math.ceil(depth / (0.75 * BLACK_NIB_MM) - 1e-9)
        out = [('path', source.path)]
        for j in range(1, levels + 1):
            distance = depth * j / levels
            for polygon in polygons_of(area.buffer(-distance, quad_segs=16)):
                loop = line_path(LineString(polygon.exterior.coords))
                if loop is not None:
                    out.append((f'inset-{distance:.4f}', loop))
        return out
    rings, centre = stroke_rings(source.nib)
    out = []
    for distance in rings:
        outline = source.line.buffer(distance, quad_segs=16)
        for polygon in polygons_of(outline):
            for ring in (polygon.exterior, *polygon.interiors):
                loop = line_path(LineString(ring.coords))
                if loop is not None:
                    out.append((f'loop-{distance:.4f}', loop))
    if centre:
        out.append(('path', source.path))
    return out


def load_source(svg: Path, edits: dict | None = None) -> list[SourcePath]:
    """The revision-14 paths, with this edition's ``GEOMETRY_EDITS`` applied."""

    edits = GEOMETRY_EDITS if edits is None else edits
    tree = ET.parse(svg)
    parents = {child: parent for parent in tree.iter() for child in parent}

    def inherited(element, key):
        while element is not None:
            if element.get(key) is not None:
                return element.get(key)
            element = parents.get(element)
        return None

    out = []
    applied = set()
    for index, element in enumerate(tree.iter(NS + 'path')):
        attributes = dict(element.attrib)
        model = attributes.get('data-model-path-index')
        if model in edits:
            applied.add(model)
            if edits[model] is None:
                continue
            attributes['d'] = edits[model]
        path = parse_absolute_path_data(attributes['d'])
        width = round(float(inherited(element, 'stroke-width')), 3)
        points = [(float(x), float(y)) for x, y in path.flatten(FLATTEN_MM).points]
        out.append(SourcePath(index, attributes, path, points, width, outline_weight(attributes, width, path)))
    if applied != set(edits):
        raise ValueError(f'geometry edits for missing paths: {sorted(set(edits) - applied)}')
    found = sorted(p.get('data-model-path-index') for p in out if p.weight == BAND_GOLD)
    if found != sorted(BAND_INNER_LINES):
        raise ValueError(f'expected the band inner lines {sorted(BAND_INNER_LINES)}, found {found}')
    return out


def black_ink(paths: list[SourcePath], margin: float = 0.0):
    """Buffered black ink: each black outline at its drawn half-width + margin
    (the sheet frame on its inward band)."""

    out = []
    for p in paths:
        if not p.is_black:
            continue
        if p.get('data-role') in INWARD_ROLES and p.nib > BLACK_NIB_MM:
            out.append(inward_band(p).buffer(margin, quad_segs=8))
        else:
            out.append(p.line.buffer(p.nib / 2 + margin, quad_segs=8))
    return out


@dataclass
class CellMap:
    cells: list[Polygon]
    tree: STRtree = field(init=False)

    def __post_init__(self) -> None:
        self.tree = STRtree(self.cells)

    def find(self, point: tuple[float, float]) -> int | None:
        target = Point(point)
        for index in self.tree.query(target):
            if self.cells[int(index)].contains(target):
                return int(index)
        return None


def compute_cells(paths: list[SourcePath]) -> CellMap:
    ink = unary_union(black_ink(paths, BASE_MM))
    paper = box(0.0, 0.0, *PAGE).difference(ink)
    cells = polygons_of(paper)
    # Stable order independent of GEOS traversal: top-left first, then size.
    cells.sort(key=lambda c: (round(c.bounds[1], 3), round(c.bounds[0], 3), -round(c.area, 3)))
    return CellMap(cells)


@dataclass
class WheelFace:
    name: str
    centre: tuple[float, float]
    split_radius_mm: float
    face_cells: list[int]
    opening_cuts: int
    rim: list[Polygon]
    spokes: list[Polygon]


SPLIT_HALF_WIDTH_MM = 0.12


def split_wheel(cellmap: CellMap, paths: list[SourcePath], name: str, face_points, centre,
                split_radius, opening_component) -> WheelFace:
    """Divide wheel-face cells into an iron rim and separate cast spokes.

    A circle just outside the spoke openings separates rim from spokes.  The
    spokes meet around the hub, so a radial cut through the middle of each
    opening separates neighbouring spokes there; each spoke keeps its root.
    """

    cx, cy = centre
    inner = Point(cx, cy).buffer(split_radius - SPLIT_HALF_WIDTH_MM, quad_segs=256)
    outer = Point(cx, cy).buffer(split_radius + SPLIT_HALF_WIDTH_MM, quad_segs=256)
    cuts = []
    for path in paths:
        if path.get('data-component') == opening_component and path.get('data-feature') == 'cast-spoke-opening':
            mx = sum(x for x, _ in path.points) / len(path.points)
            my = sum(y for _, y in path.points) / len(path.points)
            angle = math.atan2(my - cy, mx - cx)
            far = (cx + split_radius * math.cos(angle), cy + split_radius * math.sin(angle))
            cuts.append(LineString([(cx, cy), far]).buffer(SPLIT_HALF_WIDTH_MM, cap_style='flat'))
    cut = unary_union(cuts)
    indices = []
    for point in face_points:
        index = cellmap.find(point)
        if index is None:
            raise ValueError(f'{name}: face point {point} is not inside a cell')
        if index not in indices:
            indices.append(index)
    rim, spokes = [], []
    for index in indices:
        cell = cellmap.cells[index]
        rim.extend(polygons_of(cell.difference(outer)))
        spokes.extend(polygons_of(cell.intersection(inner).difference(cut)))
    rim.sort(key=lambda c: (round(c.bounds[1], 3), round(c.bounds[0], 3)))
    spokes.sort(key=lambda c: math.atan2(c.centroid.y - cy, c.centroid.x - cx))
    return WheelFace(name, centre, split_radius, indices, len(cuts), rim, spokes)

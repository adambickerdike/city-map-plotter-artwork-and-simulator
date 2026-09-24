"""Paper cells between the frozen revision-14 outlines.

The black outline of every source path is buffered by its physical Black pen
half-width plus ``BASE_MM``.  Whatever paper remains is split into connected
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

from tools.engineering_source_plates.aveling_5499_colour_v5.hatching import polygons_of
from tools.engineering_source_plates.aveling_5499_colour_v5.inventory import GOLD_NIB_MM, GOLD_PEN

NS = '{http://www.w3.org/2000/svg}'
PAGE = (420.0, 297.0)
BASE_MM = 0.15
GAP_MM = 0.22
FLATTEN_MM = 0.005
# The blueprint's White 0.30/0.40/0.50 mm strokes become the studio's actual
# Black 0.25/0.40/0.60 mm pens, as in colour edition 1.  The engine's own
# linework is drawn one pen heavier, so its outlines stay strong and black
# beside the colour: fine lines on Black 0.40, principal outlines on Black
# 0.60 and the roller tyres on Black 1.00.  Small or delicate features and
# small assemblies keep the finer pen so they neither close up nor squeeze out
# their colour.  Lettering, the Invicta horse, the worksplate and the sheet
# furniture keep their original weights.
BLACK_PEN_FOR_SOURCE_WIDTH = {0.3: 'black-0-25', 0.4: 'black-0-4', 0.5: 'black-0-6'}
HEAVIER_ENGINE_PEN = {0.3: 'black-0-4', 0.4: 'black-0-6', 0.5: 'black-1'}
BLACK_NIB = {'black-0-25': 0.25, 'black-0-4': 0.4, 'black-0-6': 0.6, 'black-1': 1.0}
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
SMALL_FEATURE_MM = 3.0      # features smaller than this keep the finer pen
# Each brass boiler band is drawn as a black edge either side and an upright
# inner line only 1.08 mm from its front edge: too close for a 0.40 mm Gold
# line to fit beside it with its white gap, which left that side of every band
# white.  These inner lines (by revision-14 model path index) are therefore
# drawn in Gold, unchanged in shape, as one of the band's own gold lines, so
# the whole band reads as a gold bar between its two black edges.
BAND_INNER_LINES = frozenset({'150', '151', '156', '159'})
BAND_COMPONENT = 'boiler-and-smokebox'
OUTLINE_NIB = {**BLACK_NIB, GOLD_PEN: GOLD_NIB_MM}


def black_pen(attributes: dict, width: float, path: VectorPath) -> str:
    """The Black pen for one source path (see the note above)."""

    base = BLACK_PEN_FOR_SOURCE_WIDTH[width]
    if attributes.get('data-role') != ENGINE_ROLE:
        return base
    tokens = set(re.split(r'[-:]', attributes.get('data-feature') or ''))
    if attributes.get('data-component') in FINE_COMPONENTS or tokens & FINE_FEATURE_TOKENS:
        return base
    heavy = HEAVIER_ENGINE_PEN[width]
    bounds = path.bounds()
    if max(bounds.width, bounds.height) < SMALL_FEATURE_MM:
        return base
    if path.length(0.001) < 3 * BLACK_NIB[heavy] + 0.3:
        return base
    return heavy


def is_band_inner_line(attributes: dict) -> bool:
    return attributes.get('data-model-path-index') in BAND_INNER_LINES


def outline_pen(attributes: dict, width: float, path: VectorPath) -> str:
    """The pen for one source path: Gold for a brass band's inner line,
    otherwise its Black pen."""

    if is_band_inner_line(attributes):
        bounds = path.bounds()
        if attributes.get('data-component') != BAND_COMPONENT or bounds.width > 1e-6 or bounds.height < 3.0:
            raise ValueError(f"band inner line {attributes.get('data-model-path-index')} is not an upright "
                             f'{BAND_COMPONENT} line')
        return GOLD_PEN
    return black_pen(attributes, width, path)


@dataclass
class SourcePath:
    index: int
    attributes: dict[str, str]
    path: VectorPath
    points: list[tuple[float, float]]
    source_width: float
    pen_id: str

    @property
    def nib(self) -> float:
        return OUTLINE_NIB[self.pen_id]

    @property
    def is_black(self) -> bool:
        return self.pen_id in BLACK_NIB

    @property
    def line(self) -> LineString:
        return LineString(self.points)

    def get(self, key: str) -> str | None:
        return self.attributes.get(key)


def load_source(svg: Path) -> list[SourcePath]:
    tree = ET.parse(svg)
    parents = {child: parent for parent in tree.iter() for child in parent}

    def inherited(element, key):
        while element is not None:
            if element.get(key) is not None:
                return element.get(key)
            element = parents.get(element)
        return None

    out = []
    for index, element in enumerate(tree.iter(NS + 'path')):
        path = parse_absolute_path_data(element.get('d'))
        width = round(float(inherited(element, 'stroke-width')), 3)
        points = [(float(x), float(y)) for x, y in path.flatten(FLATTEN_MM).points]
        out.append(SourcePath(index, dict(element.attrib), path, points, width,
                              outline_pen(element.attrib, width, path)))
    found = sorted(p.get('data-model-path-index') for p in out if p.pen_id == GOLD_PEN)
    if found != sorted(BAND_INNER_LINES):
        raise ValueError(f'expected the band inner lines {sorted(BAND_INNER_LINES)}, found {found}')
    return out


def black_ink(paths: list[SourcePath], margin: float = 0.0):
    """Buffered black ink: each Black outline at its physical half-width + margin."""

    return [p.line.buffer(p.nib / 2 + margin, quad_segs=8) for p in paths if p.is_black]


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

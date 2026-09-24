"""Resolve the paint plan against the frozen drawing and generate pen lines."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from tools.engineering_source_plates.aveling_5499_colour_v5 import design as D
from tools.engineering_source_plates.aveling_5499_colour_v5 import hatching as H
from tools.engineering_source_plates.aveling_5499_colour_v5.regions import compute_cells, load_source, split_wheel

@dataclass
class Zone:
    name: str
    material: str
    painter_record: dict
    cells: list
    cell_indices: list = field(default_factory=list)
    seeds: tuple = ()
    strokes: list = field(default_factory=list)
    note: str = ''


@dataclass
class Plan:
    source: list
    cellmap: object
    zones: list
    claimed: dict
    wheel_faces: list
    tender_edge_x: float = 0.0
    tender_top_y: float = 0.0

    def strokes(self):
        for zone in self.zones:
            for stroke in zone.strokes:
                yield zone, stroke


def outline_polygon(paths, **match):
    for p in paths:
        if all(p.get(k) == v for k, v in match.items()):
            return Polygon(p.points).buffer(0)
    raise ValueError(f'no source path matches {match}')


def cells_within(cellmap, polygon):
    """Every paper cell whose interior point lies inside ``polygon``."""

    inside = []
    for index, cell in enumerate(cellmap.cells):
        if cell.area < 0.05 or not cell.intersects(polygon):
            continue
        if polygon.contains(cell.representative_point()):
            inside.append(index)
    return inside


def brass_zones(paths):
    """The oval maker's plate on the cylinder block, ruled in Gold.

    The worksplate and Invicta horse at the top right are left uncoloured, as
    black engraving on white paper.
    """

    maker = outline_polygon(paths, **{'data-component': 'cylinder-cover', 'data-feature': 'maker-plate-inner'})
    return [('cylinder-maker-plate', 'brass plate', D.gold_lines(), maker,
             "Level Gold lines across the oval maker's plate on the cylinder block.")]


def tender_top_y(paths, edge_x: float) -> float:
    """The y of the tender's top rim, just beyond its front edge."""

    ys = []
    for p in paths:
        if p.get('data-component') != 'tender-and-water-box' or p.get('data-feature') != 'component-outline':
            continue
        px = [x for x, _ in p.points]
        py = [y for _, y in p.points]
        if max(py) - min(py) < 0.01 and min(px) > edge_x and max(px) < edge_x + 40.0:
            ys.append(py[0])
    if not ys:
        raise ValueError('no horizontal tender top rim found beyond its front edge')
    return min(ys)


def tender_front_edge_x(paths) -> float:
    """The x of the tender's straight front edge, where it runs behind the rear wheel."""

    xs = []
    for p in paths:
        if p.get('data-component') != 'tender-and-water-box' or p.get('data-feature') != 'component-outline':
            continue
        px = [x for x, _ in p.points]
        py = [y for _, y in p.points]
        if max(px) - min(px) < 0.01 and max(py) - min(py) > 3.0 and abs(px[0] - D.REAR_ROLL[0]) < 45.0:
            xs.append(px[0])
    if not xs or max(xs) - min(xs) > 0.01:
        raise ValueError(f'expected one straight tender front edge behind the rear wheel, found {sorted(set(xs))}')
    return xs[0]


def build_plan(svg: Path) -> Plan:
    paths = load_source(svg)
    # the band painter steps out from each band's Gold inner line
    inner_x = sorted({round(p.points[0][0], 3) for p in paths if not p.is_black})
    if inner_x != sorted(D.BAND_INNER_X):
        raise ValueError(f'band inner lines lie at x = {inner_x}, not at {sorted(D.BAND_INNER_X)}')
    cellmap = compute_cells(paths)
    claimed: dict[int, str] = {}
    zones: list[Zone] = []
    faces = []

    def claim(index, owner):
        if index in claimed:
            raise ValueError(f'{owner}: cell {index} already claimed by {claimed[index]}')
        claimed[index] = owner

    for spec, pattern in ((D.REAR_WHEEL, D.REAR_SPOKE_PATTERN), (D.FRONT_WHEEL, D.FRONT_SPOKE_PATTERN)):
        face = split_wheel(cellmap, paths, spec['name'], spec['face_points'], spec['centre'], spec['split'],
                           spec['opening_component'])
        faces.append(face)
        for index in face.face_cells:
            claim(index, spec['name'])
        rim, spokes = D.black_rings(spec['centre']), D.ruled(pattern)
        zones.append(Zone(spec['name'] + '-rim', 'black-painted iron rim', rim.describe(), face.rim,
                          list(face.face_cells), tuple(spec['face_points']),
                          note=f"Face cells outside r = {spec['split'] + 0.12:.2f} mm: close, even Black circles."))
        zones[-1].painter = rim
        zones.append(Zone(spec['name'] + '-spokes', 'green paint', spokes.describe(), face.spokes,
                          list(face.face_cells), tuple(spec['face_points']),
                          note=f"Face cells inside r = {spec['split'] - 0.12:.2f} mm, cut apart through the "
                               f"{face.opening_cuts} spoke openings; {len(pattern)} lines ruled along every spoke."))
        zones[-1].painter = spokes
    # Seen through the rear wheel, a cell is green tender only when it lies
    # beyond the tender's straight front edge and below its top rim; all else
    # there is the black iron of the firebox, frame and platform.
    edge_x = tender_front_edge_x(paths)
    top_y = tender_top_y(paths, edge_x)
    through = {D.BEHIND_REAR_WHEEL_PART: [], D.TENDER_PART: []}
    for seed in D.THROUGH_REAR_WHEEL:
        index = cellmap.find(seed)
        if index is None:
            raise ValueError(f'through-wheel seed {seed} is not inside a paper cell')
        point = cellmap.cells[index].representative_point()
        side = D.TENDER_PART if point.x > edge_x and point.y > top_y else D.BEHIND_REAR_WHEEL_PART
        through[side].append((seed, index))
    for part in D.parts():
        indices = []
        seeds = list(part.seeds) + [seed for seed, _ in through.get(part.name, [])]
        for seed in seeds:
            index = cellmap.find(seed)
            if index is None:
                raise ValueError(f'{part.name}: seed {seed} is not inside a paper cell')
            claim(index, part.name)
            indices.append(index)
        zone = Zone(part.name, part.material, part.painter.describe(),
                    [cellmap.cells[i] for i in indices], indices, tuple(seeds), note=part.note)
        zone.painter = part.painter
        zones.append(zone)
    for name, material, painter, outline, note in brass_zones(paths):
        indices = cells_within(cellmap, outline)
        for index in indices:
            claim(index, name)
        zone = Zone(name, material, painter.describe(), [cellmap.cells[i] for i in indices], indices,
                    (), note=note)
        zone.painter = painter
        zones.append(zone)
    for zone in zones:
        zone.strokes = zone.painter.paint(zone.cells)
    return Plan(paths, cellmap, zones, claimed, faces, edge_x, top_y)

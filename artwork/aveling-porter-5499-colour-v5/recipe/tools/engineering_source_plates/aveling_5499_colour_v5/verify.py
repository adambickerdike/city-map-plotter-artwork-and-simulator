#!/usr/bin/env python3
"""Verify colour edition 5 (pen lines only) against the frozen revision-14 master.

Independent of the builder's bookkeeping, this re-reads the exported SVG and
checks that every source outline is present and unchanged in black, that every
colour line keeps its paper gap from black ink and stays inside the area its
part claims, and that pen files, manifest and plot job agree.

Outline shapes are byte-identical to the source except the documented
regulator-rod redraw (``regions.GEOMETRY_EDITS``).  Every black line is drawn
with the one Black 0.25 mm pen: a heavier weight (``regions.outline_weight``)
as the loops of ``regions.stroke_rings``, whose ink is checked to cover the
weight's full width and nothing beyond it.  The four inner lines of the brass
boiler bands are Gold.
"""
from collections import Counter, defaultdict
import hashlib, json, math, subprocess, sys, xml.etree.ElementTree as ET
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from shapely import STRtree
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from tools.engineering_source_plates.aveling_5499_colour_v5.inventory import (
    BLACK_NIB_MM, BLACK_PEN, BROWN_PEN, EDITION_PEN_INVENTORY, GOLD_NIB_MM, GOLD_PEN)
from city_map_plotter.stroke_font import STROKE_FONT_ID
from city_map_plotter.technical_assets import parse_absolute_path_data
from city_map_plotter.vector_path import LineSegment
from tools.engineering_source_plates.build_aveling_5499_colour_v5 import BASE_SHA, ID
from tools.engineering_source_plates.aveling_5499_colour_v5.design import (
    BAND_CROSSING_LINES, MUST_CARRY, MUST_STAY_PAPER, OPEN_AIR, PEN_ORDER)
from tools.engineering_source_plates.aveling_5499_colour_v5 import hatching as H
from tools.engineering_source_plates.aveling_5499_colour_v5.painters import fill_region, minimum_length
from tools.engineering_source_plates.aveling_5499_colour_v5.plan import build_plan
from tools.engineering_source_plates.aveling_5499_colour_v5.regions import (
    BAND_GOLD, BAND_INNER_LINES, BASE_MM, GAP_MM, GEOMETRY_EDITS, INWARD_ROLES, WEIGHT_MM, is_band_inner_line,
    outline_weight, stroke_rings)

NS = '{http://www.w3.org/2000/svg}'
TOLERANCE_MM = 0.005
HORSE_PART = 'invicta-horse'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
PRESERVED = ['data-role', 'data-feature', 'data-component', 'data-copy', 'data-cap-height-mm',
             'data-tracking-mm', 'data-alignment', 'data-source-ref', 'data-model-path-index']


def is_horse_relief(element) -> bool:
    return (element.get('data-role') == 'invicta-emblem'
            and element.get('data-feature') != 'source-casting-contour')


def verify(package):
    evidence = package / 'evidence'
    base = evidence / 'revision-14-master.svg'
    svg = package / 'artwork' / f'{ID}.svg'
    assert sha(base) == BASE_SHA
    tree = ET.parse(svg)
    parents = {child: parent for parent in tree.iter() for child in parent}

    def inherited(element, key):
        while element is not None:
            if element.get(key) is not None:
                return element.get(key)
            element = parents.get(element)

    base_tree = ET.parse(base)
    base_parents = {child: parent for parent in base_tree.iter() for child in parent}

    def base_width(element):
        while element is not None:
            if element.get('stroke-width') is not None:
                return round(float(element.get('stroke-width')), 3)
            element = base_parents.get(element)

    old_paths = list(base_tree.iter(NS + 'path'))
    assert len(old_paths) == 863
    assert not any(e.tag.split('}')[-1] in {'text', 'image', 'use'} for e in tree.iter())
    assert not any(e.get('transform') for e in tree.iter())
    inventory = {p.identity: p for p in EDITION_PEN_INVENTORY.pens}
    paths = list(tree.iter(NS + 'path'))
    strokes_by_base, fills = defaultdict(list), []
    for element in paths:
        pen_id = inherited(element, 'data-plot-pen-id')
        pen = inventory[pen_id]
        assert inherited(element, 'data-plot-pen-profile') == EDITION_PEN_INVENTORY.id
        assert inherited(element, 'stroke') == pen.preview_color
        assert inherited(element, 'data-plot-ink') == pen.ink
        assert inherited(element, 'fill') == 'none'
        assert inherited(element, 'stroke-linecap') == inherited(element, 'stroke-linejoin') == 'round'
        for key in ['stroke-width', 'data-plot-nib-mm', 'data-plot-nominal-nib-mm', 'data-plot-width-mm',
                    'data-plot-requested-width-mm']:
            assert float(inherited(element, key)) == pen.mark_width_mm
        assert inherited(element, 'data-plot-passes') == inherited(element, 'data-plot-strokes') == '1'
        vector = parse_absolute_path_data(element.get('d'))
        length = vector.length(0.0001)
        assert length + 0.003 >= 3 * pen.mark_width_mm, (element.get('d')[:60], length)
        if element.get('data-base-path-index') is not None:
            strokes_by_base[int(element.get('data-base-path-index'))].append((element, pen_id, vector))
        else:
            assert element.get('data-role') == 'colour-fill'
            assert element.get('data-fill-role') in {'base', 'shade'}
            assert length + 0.003 >= minimum_length(pen_id)
            fills.append((element, pen_id, vector, length))

    # 1. every source outline drawn as documented: its shape unchanged unless
    #    it is one of the geometry edits, its weight from the documented
    #    mapping, drawn with the one Black pen (Gold for the band inner lines)
    #    as exactly the loops and path the weight calls for, their ink covering
    #    the weight's full width and nothing beyond it
    models = {e.get('data-model-path-index'): i for i, e in enumerate(old_paths)}
    assert set(GEOMETRY_EDITS) <= set(models)
    removed = {models[m] for m, d in GEOMETRY_EDITS.items() if d is None}
    assert set(strokes_by_base) == set(range(863)) - removed
    copy_groups = defaultdict(set)
    outlines = {}
    weights = Counter()
    ink_checks = {'worst_ink_beyond_width_mm2': 0.0, 'worst_uncovered_fraction': 0.0}
    for index, original in enumerate(old_paths):
        if index in removed:
            continue
        d = GEOMETRY_EDITS.get(original.get('data-model-path-index')) or original.get('d')
        source = parse_absolute_path_data(d)
        weight = outline_weight(original.attrib, base_width(original), source)
        weights[weight] += 1
        width = GOLD_NIB_MM if weight == BAND_GOLD else WEIGHT_MM[weight]
        pen_id = GOLD_PEN if weight == BAND_GOLD else BLACK_PEN
        drawn = strokes_by_base[index]
        inward = original.get('data-role') in INWARD_ROLES and weight != 'fine'
        rings, centre = ([], True) if weight == BAND_GOLD else stroke_rings(width)
        if inward:
            rings, centre = ['inset'], True
        kinds = Counter(e.get('data-outline-stroke') for e, _, _ in drawn)
        assert kinds['path'] == (1 if centre else 0), (index, kinds)
        assert all(k == 'path' or k.startswith('loop-' if not inward else 'inset-') for k in kinds), (index, kinds)
        for element, pen, vector in drawn:
            assert pen == pen_id, (index, pen)
            assert element.get('data-outline-weight') == weight, index
            for key in PRESERVED:
                assert element.get(key) == original.get(key), (index, key)
            if element.get('data-outline-stroke') == 'path':
                assert element.get('d') == d, (index, 'outline geometry changed')
            if element.get('data-copy'):
                assert float(element.get('data-cap-height-mm')) + 1e-9 >= 8 * inventory[pen].mark_width_mm
                assert all(isinstance(s, LineSegment) for s in vector.segments)
                copy_groups[(element.get('data-role'), element.get('data-copy'))].add(index)
        centreline = LineString(source.flatten(0.0005).points)
        if rings:
            band = centreline.buffer(width / 2, quad_segs=32)
            if inward:
                area = Polygon(centreline.coords).buffer(0)
                band = area.buffer(BLACK_NIB_MM / 2, quad_segs=32).difference(
                    area.buffer(-(width - BLACK_NIB_MM / 2), quad_segs=32))
            ink = unary_union([LineString(v.flatten(0.0005).points).buffer(BLACK_NIB_MM / 2, quad_segs=32)
                               for _, _, v in drawn])
            beyond = ink.difference(band.buffer(0.012)).area
            uncovered = band.difference(ink).area / band.area
            ink_checks['worst_ink_beyond_width_mm2'] = max(ink_checks['worst_ink_beyond_width_mm2'], beyond)
            ink_checks['worst_uncovered_fraction'] = max(ink_checks['worst_uncovered_fraction'], uncovered)
            assert beyond < 1e-3 and uncovered < 0.01, (index, beyond, uncovered)
        outlines[index] = (original, weight, width, centreline)
    prior = json.loads((evidence / 'revision-14-plotting-verification.json').read_text())
    assert prior['master_svg_sha256'] == BASE_SHA and prior['complete_text_block_count'] == 12
    assert prior['font_id'] == STROKE_FONT_ID
    assert prior['font_source_sha256'] == sha(ROOT / 'src/city_map_plotter/stroke_font.py')
    assert {(r['role'], r['copy']): r['stroke_count'] for r in prior['lettering_checks']} == \
        {k: len(v) for k, v in copy_groups.items()}
    assert all(inventory[p.identity].ink != 'Black' or p.identity == BLACK_PEN for p in EDITION_PEN_INVENTORY.pens)

    # 2. colour lines: paper to every black ink edge (horse gold excepted
    #    only from the horse's own relief lines)
    black_lines, black_half, relief, black_models, gold_outlines = [], [], [], [], []
    for index, (original, weight, width, line) in sorted(outlines.items()):
        if weight == BAND_GOLD:
            gold_outlines.append((original, GOLD_PEN, line))
            continue
        black_lines.append(line)
        black_half.append(width / 2)
        relief.append(is_horse_relief(original))
        black_models.append(original.get('data-model-path-index'))
    assert sum(relief) == 39
    tree_index = STRtree(black_lines)
    minimum_gap = defaultdict(lambda: math.inf)
    horse_relief_crossings = 0
    band_crossings = Counter()
    fill_lines = []
    for element, pen_id, vector, length in fills:
        line = LineString(vector.flatten(0.0005).points)
        fill_lines.append(line)
        half = inventory[pen_id].mark_width_mm / 2
        reach = half + GAP_MM + 0.5 + 0.2
        horse = element.get('data-fill-part') == HORSE_PART
        band = element.get('data-fill-part') == 'boiler-bands'
        for j in tree_index.query(line.buffer(reach)):
            j = int(j)
            gap = line.distance(black_lines[j]) - half - black_half[j]
            if horse and relief[j]:
                horse_relief_crossings += gap < GAP_MM
                continue
            # a band's gold runs on under the thin boiler lines that cross
            # the band, as its Gold inner line does; it may cross nothing else
            if band and line.crosses(black_lines[j]):
                assert black_models[j] in BAND_CROSSING_LINES, ('band gold crosses', black_models[j])
                band_crossings[black_models[j]] += 1
                continue
            minimum_gap[pen_id] = min(minimum_gap[pen_id], gap)
    for pen_id, gap in minimum_gap.items():
        assert gap + TOLERANCE_MM >= GAP_MM, (pen_id, gap)

    # 2b. the worksplate and horse at the top right carry no colour at all
    badge_zone = LineString([(262.0, 22.0), (400.0, 86.0)]).envelope
    assert not any(badge_zone.intersects(line) for line in fill_lines), 'colour found on the top-right badges'
    assert horse_relief_crossings == 0

    # 3. every colour line lies inside the area its own part claims, and the
    #    exported lines are exactly the plan's lines
    plan = build_plan(base)
    zone_area = {zone.name: unary_union(zone.cells).buffer(1e-3) for zone in plan.zones}
    # a band's gold also runs through the thin corridors of the boiler lines
    # that cross the band
    zone_area['boiler-bands'] = unary_union(
        [zone_area['boiler-bands']] + [black_lines[j].buffer(black_half[j] + BASE_MM) for j in range(len(black_lines))
                                       if black_models[j] in BAND_CROSSING_LINES])
    horse_outline = next(outlines[i] for i in outlines
                         if outlines[i][0].get('data-role') == 'invicta-emblem'
                         and outlines[i][0].get('data-feature') == 'source-casting-contour')
    horse_silhouette = Polygon(horse_outline[3].coords).buffer(0)
    zone_area[HORSE_PART] = horse_silhouette.buffer(-(horse_outline[2] / 2 + GAP_MM) + 1e-3)
    expected = Counter()
    for zone, stroke in plan.strokes():
        expected[(zone.name, stroke.pen)] += 1
    exported = Counter()
    by_part = defaultdict(list)
    for (element, pen_id, vector, length), line in zip(fills, fill_lines):
        part = element.get('data-fill-part')
        # the horse's area bounds the gold ink itself; every other area bounds
        # the line centre, its erosion already allowing for the nib
        geometry = line.buffer(inventory[pen_id].mark_width_mm / 2) if part == HORSE_PART else line
        assert zone_area[part].contains(geometry), (part, pen_id)
        exported[(part, pen_id)] += 1
        by_part[part].append((pen_id, line))
    assert exported == expected, set(exported.items()) ^ set(expected.items())

    # 3b. the brass bands' inner lines are the only outlines drawn in colour:
    #     exactly the documented four, in Gold.  They meet black ink only
    #     where the blueprint's own lines join or cross them; everywhere else
    #     they keep the white gap from black ink, run inside the bands' paper,
    #     and keep the gap from every other colour
    assert sorted(e.get('data-model-path-index') for e, _, _ in gold_outlines) == sorted(BAND_INNER_LINES)
    assert all(is_band_inner_line(e.attrib) and pen_id == GOLD_PEN for e, pen_id, _ in gold_outlines)
    fill_index = STRtree(fill_lines)
    fill_pens = [pen_id for _, pen_id, _, _ in fills]
    band_joins, gap_to_black, gap_to_colour = 0, math.inf, math.inf
    for element, pen_id, line in gold_outlines:
        half = inventory[pen_id].mark_width_mm / 2
        near = [int(j) for j in tree_index.query(line.buffer(half + GAP_MM + 0.5 + 0.2))]
        met = [j for j in near if line.distance(black_lines[j]) < 0.01]
        joins = unary_union([black_lines[j].buffer(black_half[j] + half + GAP_MM, quad_segs=32) for j in met])
        free = line.difference(joins)
        band_joins += len(met)
        assert met and not free.is_empty
        for j in near:
            gap_to_black = min(gap_to_black, free.distance(black_lines[j]) - half - black_half[j])
        assert zone_area['boiler-bands'].contains(free), element.get('data-model-path-index')
        for j in fill_index.query(line.buffer(half + GAP_MM + 0.5)):
            j = int(j)
            if fill_pens[j] != GOLD_PEN:
                gap_to_colour = min(gap_to_colour, line.distance(fill_lines[j]) - half
                                    - inventory[fill_pens[j]].mark_width_mm / 2)
    assert gap_to_black + TOLERANCE_MM >= GAP_MM, gap_to_black
    assert gap_to_colour + TOLERANCE_MM >= GAP_MM, gap_to_colour

    # 3c. areas reviewed against the side photograph: open air stays paper,
    #     and each part found uncoloured or wrongly coloured carries its ink
    for point in OPEN_AIR:
        index = plan.cellmap.find(point)
        assert index is not None and index not in plan.claimed, ('open air is claimed', point)
        cell = plan.cellmap.cells[index]
        assert not any(cell.intersects(fill_lines[int(j)]) for j in fill_index.query(cell)), ('colour in open air', point)
    for point in MUST_STAY_PAPER:
        index = plan.cellmap.find(point)
        assert index is not None and index not in plan.claimed, ('must stay paper', point)
        cell = plan.cellmap.cells[index]
        assert not any(cell.intersects(fill_lines[int(j)]) for j in fill_index.query(cell)), ('colour on', point)
    for point, pen_id in MUST_CARRY:
        cell = plan.cellmap.cells[plan.cellmap.find(point)]
        assert any(fill_pens[int(j)] == pen_id and cell.intersects(fill_lines[int(j)])
                   for j in fill_index.query(cell)), ('missing colour', point, pen_id)

    # 4. wheel faces: spokes and rim are split without a drawn line, so their
    #    inks must still keep a paper gap between them
    wheel_gaps = {}
    for wheel in ('rear-wheel', 'front-roll'):
        rim = by_part[wheel + '-rim']
        spokes = by_part[wheel + '-spokes']
        rim_index = STRtree([l for _, l in rim])
        gap = math.inf
        for pen_id, line in spokes:
            for j in rim_index.query(line.buffer(1.5)):
                other_pen, other = rim[int(j)]
                gap = min(gap, line.distance(other) - inventory[pen_id].mark_width_mm / 2
                          - inventory[other_pen].mark_width_mm / 2)
        assert gap + TOLERANCE_MM >= GAP_MM, (wheel, gap)
        wheel_gaps[wheel] = round(gap, 4)

    # 5. spokes: every spoke line is straight, and the lines within any one
    #    spoke are parallel, so no ruling fans out
    spoke_lines = {}
    zones = {zone.name: zone for zone in plan.zones}
    for part in ('rear-wheel-spokes', 'front-roll-spokes', 'rear-inner-spokes', 'front-inner-spokes'):
        pieces = H.polygons_of(fill_region(zones[part].cells, 'green-0-25').buffer(1e-3))
        piece_index = STRtree(pieces)
        groups = defaultdict(list)
        for pen_id, line in by_part[part]:
            coords = list(line.coords)
            (x0, y0), (x1, y1) = coords[0], coords[-1]
            chord = LineString([coords[0], coords[-1]])
            assert max(chord.distance(Point(q)) for q in coords) < 0.005, (part, 'spoke line is not straight')
            middle = line.interpolate(0.5, normalized=True)
            owner = next(int(k) for k in piece_index.query(middle) if pieces[int(k)].contains(middle))
            groups[owner].append(math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0)
        spread = 0.0
        for angles in groups.values():
            reference = angles[0]
            spread = max(spread, max(min(abs(a - reference) % 180.0, 180.0 - abs(a - reference) % 180.0)
                                     for a in angles))
        assert spread < 0.5, (part, spread)
        spoke_lines[part] = {'lines': len(by_part[part]), 'pieces': len(groups),
                             'maximum_angle_spread_within_a_spoke_deg': round(spread, 4)}

    # 6. pen files, manifest and plot job
    pen_files = sorted((package / 'artwork').glob('*.pen-*.svg'))
    assert len(pen_files) == len(PEN_ORDER)
    layer_paths = Counter()
    for file in pen_files:
        layer_paths.update(p.get('d') for p in ET.parse(file).iter(NS + 'path'))
    assert layer_paths == Counter(p.get('d') for p in paths)
    manifest = json.loads((package / 'artwork' / f'{ID}.plot.json').read_text())
    baseline = json.loads((evidence / 'revision-14-master.plot.json').read_text())
    assert manifest['page'] == baseline['page']
    assert manifest['rendering']['stock_tone'] == 'light' and manifest['rendering']['paper_preview_color'] == '#ffffff'
    assert manifest['rendering']['pen_profile'] == EDITION_PEN_INVENTORY.id
    assert manifest['rendering']['pen_inventory'] == EDITION_PEN_INVENTORY.as_dict()
    # no grey pen anywhere in this edition's inventory or drawing
    assert not any(pen.ink == 'Grey' for pen in EDITION_PEN_INVENTORY.pens)
    assert not any(inventory[inherited(p, 'data-plot-pen-id')].ink == 'Grey' for p in paths)
    # red-brown: every red part is darkened by Brown lines between its Red lines
    red_parts = Counter(e.get('data-fill-part') for e, pen, _, _ in fills if pen == 'red-0-25')
    brown_parts = Counter(e.get('data-fill-part') for e, pen, _, _ in fills if pen == BROWN_PEN)
    assert set(brown_parts) <= set(red_parts), set(brown_parts) - set(red_parts)
    assert all(brown_parts[part] >= 1 for part in red_parts), [p for p in red_parts if not brown_parts[p]]
    assert inventory[BROWN_PEN].ink == 'Brown' and inventory[BROWN_PEN].mark_width_mm == 0.25
    # no broad gold: every gold mark is a fine line on the edition's gold pen
    gold_marks = [(e, pen) for e, pen, _, _ in fills if inventory[pen].ink == 'Gold']
    assert gold_marks and all(pen == GOLD_PEN for _, pen in gold_marks)
    assert all(inventory[pen].mark_width_mm <= GOLD_NIB_MM for _, pen in gold_marks)
    gold_parts = Counter(e.get('data-fill-part') for e, _ in gold_marks)
    assert all(count >= 2 for count in gold_parts.values()), gold_parts
    sequence = [r['pen_id'] for r in manifest['pen_sequence']]
    assert sequence == list(PEN_ORDER)
    job = json.loads((package / 'plot' / f'{ID}.plotjob.json').read_text())
    assert job['source']['sha256'] == sha(svg) and not job['preflight']['issues']
    assert job['order'] == 'optimised'
    source_checks = []
    for s in json.loads((evidence / 'sources.json').read_text())['sources']:
        for fk, hk in [('file', 'sha256'), ('working_file', 'working_sha256'), ('video_file', 'video_sha256')]:
            if s.get(fk):
                digest = sha(evidence / s[fk])
                assert digest == s[hk]
                source_checks.append({'file': s[fk], 'sha256': digest})
    validation = subprocess.run([sys.executable, str(ROOT / 'tools/validate_format.py'), '--warnings-as-errors', str(svg)],
                                check=True, capture_output=True, text=True)

    fill_counts = Counter(pen for _, pen, _, _ in fills)
    report = {
        'schema': 'aveling-5499-lined-colour-verification-v1', 'master_svg_sha256': sha(svg),
        'base_master_sha256': BASE_SHA, 'all_svg_paths': len(paths), 'source_outline_paths': 863,
        'drawn_source_paths': len(outlines), 'unchanged_source_paths': 863 - len(GEOMETRY_EDITS),
        'geometry_edits': {'reshaped_model_paths': sorted(m for m, d in GEOMETRY_EDITS.items() if d),
                           'removed_model_paths': sorted(m for m, d in GEOMETRY_EDITS.items() if d is None),
                           'what': 'the regulator rod redrawn straight and level with a rounded bend down onto '
                                   'the motion plate'},
        'fill_lines': len(fills), 'dots': 0,
        'fill_lines_by_pen': dict(fill_counts),
        'fill_length_m_by_pen': {pen: round(sum(l for _, p, _, l in fills if p == pen) / 1000, 3) for pen in fill_counts},
        'black_pen': BLACK_PEN,
        'outline_paths_by_weight': dict(sorted(weights.items())),
        'outline_strokes': dict(Counter(e.get('data-outline-stroke').split('-')[0]
                                        for strokes in strokes_by_base.values() for e, _, _ in strokes)),
        'heavy_lines_from_the_fine_black': {'ink_beyond_the_line_width_mm2_worst': round(ink_checks['worst_ink_beyond_width_mm2'], 6),
                                            'line_width_left_uncovered_worst_fraction': round(ink_checks['worst_uncovered_fraction'], 6)},
        'band_gold_crossings_under_black_lines': dict(band_crossings),
        'band_inner_lines': {
            'pen': GOLD_PEN, 'paths': len(gold_outlines),
            'model_path_indices': sorted(BAND_INNER_LINES, key=int), 'shapes_unchanged': True,
            'joins_with_black_lines_as_drawn': band_joins,
            'minimum_white_gap_to_black_ink_away_from_joins_mm': round(gap_to_black, 4),
            'minimum_white_gap_to_other_colours_mm': round(gap_to_colour, 4),
            'lie_inside_the_bands_away_from_joins': True},
        'required_white_gap_mm': GAP_MM,
        'minimum_white_gap_to_black_ink_mm': {pen: round(g, 4) for pen, g in sorted(minimum_gap.items())},
        'grey_pen_used': False,
        'red_brown': {part: {'red_lines': red_parts[part], 'brown_lines': brown_parts[part]} for part in sorted(red_parts)},
        'top_right_badges_uncoloured': not any(e.get('data-fill-part') in {'invicta-horse', 'worksplate'}
                                               for e, _, _, _ in fills),
        'reviewed_areas': {'open_air_left_paper': [list(p) for p in OPEN_AIR],
                           'coloured_as_required': [{'point_mm': list(p), 'pen': pen} for p, pen in MUST_CARRY]},
        'through_rear_wheel': {'tender_front_edge_x_mm': plan.tender_edge_x, 'tender_top_y_mm': plan.tender_top_y},
        'wheel_rim_to_spoke_white_gap_mm': wheel_gaps, 'spoke_lines': spoke_lines,
        'fills_inside_claimed_areas': True, 'exported_fills_match_plan': True,
        'coloured_parts': len([z for z in plan.zones if z.strokes]),
        'coloured_paper_cells': len(plan.claimed), 'paper_cells': len(plan.cellmap.cells),
        'horse_paths_unchanged': 40, 'text_blocks_unchanged': 12,
        'font_id': STROKE_FONT_ID, 'font_source_sha256': prior['font_source_sha256'],
        'minimum_cap_to_nib_ratio': min(float(old_paths[i].get('data-cap-height-mm')) / BLACK_NIB_MM
                                        for group in copy_groups.values() for i in group),
        'pen_sequence': sequence, 'pen_profile': EDITION_PEN_INVENTORY.id, 'single_pass_paths': True, 'white_paper': True,
        'gold': {'pen': GOLD_PEN, 'nib_mm': GOLD_NIB_MM, 'nib_status': 'user-confirmed nominal nib', 'lines_by_part': dict(gold_parts),
                 'band_inner_lines': len(gold_outlines)},
        'all_pen_files_match_master': True, 'format_validation': validation.stdout.strip(),
        'source_hashes': source_checks, 'simulation': job['stats'], 'strict_svg_preflight': job['preflight'],
        'inherited_geometry_proof': 'revision-14-verification.json',
        'inherited_font_proof': 'revision-14-plotting-verification.json',
        'scope': 'Photograph-informed colour illustration using available studio inks, not measured paint '
                 'matching. Vector geometry, clearance and pen-file checks only; no machine was operated.'}
    (evidence / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ['master_svg_sha256', 'all_svg_paths', 'unchanged_source_paths',
                                             'fill_lines', 'minimum_white_gap_to_black_ink_mm',
                                             'top_right_badges_uncoloured', 'outline_paths_by_weight', 'outline_strokes',
                                             'heavy_lines_from_the_fine_black', 'band_gold_crossings_under_black_lines',
                                             'band_inner_lines', 'gold', 'red_brown',
                                             'wheel_rim_to_spoke_white_gap_mm', 'format_validation']}, indent=2))
    return report


if __name__ == '__main__':
    verify(Path(sys.argv[1]))

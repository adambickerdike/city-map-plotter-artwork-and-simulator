"""Audit actual exported pen widths and complete single-stroke lettering."""
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from city_map_plotter.niche_common import ArtworkLayer
from city_map_plotter.pens import WHITE_BLUEPRINT_PEN_INVENTORY
from city_map_plotter.stroke_font import STROKE_FONT_ID, stroke_text
from city_map_plotter.technical_assets import parse_absolute_path_data
from city_map_plotter.vector_path import LineSegment, Affine2D
from tools.engineering_source_plates.aveling_5499_v11.badge import curved_copy

NS = '{http://www.w3.org/2000/svg}'


def audit(package):
    svg = package / 'artwork/aveling-porter-5499-blueprint.svg'
    tree = ET.parse(svg)
    parents = {child: parent for parent in tree.iter() for child in parent}
    paths = list(tree.iter(NS + 'path'))
    inventory = {p.identity: p for p in WHITE_BLUEPRINT_PEN_INVENTORY.pens}
    assert not any(e.get('transform') for e in tree.iter()), 'unresolved scaled widths'
    assert not any(e.tag.split('}')[-1] in ['text', 'image', 'use'] for e in tree.iter())
    assert not any(p.get('data-role') == 'axle-centre-mark' for p in paths)
    assert not any(p.get('data-role') == 'ground-datum' for p in paths)

    def inherited(el, key):
        while el is not None:
            if el.get(key) is not None:
                return el.get(key)
            el = parents.get(el)
        return None

    pen_counts = Counter()
    minimum_lengths = {}
    text_groups = defaultdict(list)
    for el in paths:
        pen_id = inherited(el, 'data-plot-pen-id')
        assert pen_id in inventory, pen_id
        pen = inventory[pen_id]
        nib = pen.mark_width_mm
        assert inherited(el, 'fill') == 'none'
        assert inherited(el, 'data-plot-pen-profile') == WHITE_BLUEPRINT_PEN_INVENTORY.id
        assert inherited(el, 'data-plot-ink') == 'White'
        assert inherited(el, 'stroke-linecap') == inherited(el, 'stroke-linejoin') == 'round'
        for key in ['stroke-width', 'data-plot-nib-mm', 'data-plot-nominal-nib-mm',
                    'data-plot-width-mm', 'data-plot-requested-width-mm']:
            assert float(inherited(el, key)) == nib, (key, el.attrib)
        assert inherited(el, 'data-plot-strokes') == inherited(el, 'data-plot-passes') == '1'
        assert float(inherited(el, 'data-plot-width-fit-error-mm')) == 0
        vector = parse_absolute_path_data(el.get('d'))
        length = vector.length(.0001)
        assert length + .003 >= 3 * nib, (el.attrib, length, nib)
        minimum_lengths[pen_id] = min(length, minimum_lengths.get(pen_id, math.inf))
        pen_counts[pen_id] += 1
        if el.get('data-copy'):
            cap = float(el.get('data-cap-height-mm'))
            assert cap + 1e-9 >= 8 * nib
            assert all(isinstance(segment, LineSegment) for segment in vector.segments)
            text_groups[(el.get('data-role'), el.get('data-copy'))].append(
                (el, list(vector.flatten(.0001).points)))

    def normalise(strokes):
        x = min(p[0] for s in strokes for p in s)
        y = min(p[1] for s in strokes for p in s)
        return [[(a-x, b-y) for a, b in s] for s in strokes]

    def error(a, b):
        if len(a) != len(b):
            return math.inf
        return min(max(math.dist(p, q) for p, q in zip(a, ordered))
                   for ordered in [b, b[::-1]])

    meta = json.loads((package / 'evidence/reconstruction.json').read_text())['metadata']
    lettering = []
    for (role, value), items in text_groups.items():
        first = items[0][0]
        cap = float(first.get('data-cap-height-mm'))
        tracking = float(first.get('data-tracking-mm'))
        pen_id = inherited(first, 'data-plot-pen-id')
        for el, _ in items:
            assert float(el.get('data-cap-height-mm')) == cap
            assert float(el.get('data-tracking-mm')) == tracking
            assert inherited(el, 'data-plot-pen-id') == pen_id
        if first.get('data-alignment') == 'elliptical-arc':
            plate = meta['badge']['curved_copy_baseline_mm']
            layer = ArtworkLayer('audit-copy', 'Audit copy', pen_id)
            curved_copy(layer, value, plate['cx'], plate['cy'],
                        plate['rx'], plate['ry'], cap,tracking=tracking)
            expected = [r.points for r in layer.records]
        else:
            expected = stroke_text(value, x_mm=0, y_mm=0, height_mm=cap, tracking_mm=tracking)
        actual = [stroke for _, stroke in items]
        assert len(actual) == len(expected), (value, 'missing or extra glyph strokes')
        remaining = normalise(expected)
        errors = []
        for stroke in normalise(actual):
            delta, i = min((error(stroke, candidate), i) for i, candidate in enumerate(remaining))
            assert delta <= .0015, (value, 'font geometry mismatch', delta)
            remaining.pop(i)
            errors.append(delta)
        lettering.append({'role': role, 'copy': value, 'font_id': STROKE_FONT_ID,
                          'pen_id': pen_id, 'cap_height_mm': cap,
                          'minimum_cap_mm': 8*inventory[pen_id].mark_width_mm,
                          'stroke_count': len(actual), 'missing_glyph_strokes': 0,
                          'maximum_font_coordinate_error_mm': max(errors)})

    # Check the requested layout adjustment against the published revision.
    previous = package / 'evidence/revision-10-master.svg'
    assert hashlib.sha256(previous.read_bytes()).hexdigest() == 'a802c01dcd2402b56c7df51ac1bff8005935d417879a471bcd1cd2d330edff83'
    old_paths = list(ET.parse(previous).iter(NS+'path'))
    def is_badge(p):return p.get('data-role','').startswith(('invicta-','worksplate-'))
    def is_engine(p):return p.get('data-role')=='side-elevation-component'
    def signature(p):
        return tuple(p.get(k) for k in ['d', 'data-role', 'data-copy', 'data-feature', 'data-component'])
    assert Counter(signature(p) for p in paths if not is_engine(p) and not is_badge(p)) == Counter(signature(p) for p in old_paths if not is_engine(p) and not is_badge(p))
    assert meta['engine_offset_mm']==[0,6]
    engine_transform=Affine2D(e=0,f=6)
    badge=meta['badge'];old_zone=badge['previous_zone_mm'];zone=badge['zone'];factor=badge['uniform_enlargement']
    assert abs(factor-1.1)<1e-9
    badge_transform=Affine2D(a=factor,d=factor,e=zone['x']-factor*old_zone['x'],f=zone['y']-factor*old_zone['y'])
    def controls(p):
        points=[p.start]
        for s in p.segments:
            if not isinstance(s,LineSegment):points.extend([s.control_1,s.control_2])
            points.append(s.to)
        return points
    layout_errors={}
    for name,selected,transform in [('engine',is_engine,engine_transform),('badge',is_badge,badge_transform)]:
        before=[p for p in old_paths if selected(p)];after=[p for p in paths if selected(p)]
        assert len(before)==len(after)
        errors=[]
        for a,b in zip(before,after):
            assert a.get('data-role')==b.get('data-role') and a.get('data-copy')==b.get('data-copy')
            expected=controls(parse_absolute_path_data(a.get('d')).transformed(transform))
            actual=controls(parse_absolute_path_data(b.get('d')))
            assert len(expected)==len(actual)
            errors.extend(math.dist(p,q) for p,q in zip(expected,actual))
        assert max(errors)<.0015,(name,max(errors))
        layout_errors[name]=max(errors)
    assert set(pen_counts) == set(inventory)
    layer_paths = Counter()
    for pen_file in (package/'artwork').glob('*.pen-*.svg'):
        layer_paths.update(p.get('d') for p in ET.parse(pen_file).iter(NS+'path'))
    assert layer_paths == Counter(p.get('d') for p in paths)
    report = {
        'master_svg_sha256': hashlib.sha256(svg.read_bytes()).hexdigest(),
        'wheel_perimeter_guides_absent': True, 'ground_line_absent': True,
        'engine_translation_mm':[0,6],'badge_scale_factor':factor,
        'maximum_layout_coordinate_errors_mm':layout_errors,'footer_and_frame_unchanged_from_v10':True,
        'pen_inventory_id': WHITE_BLUEPRINT_PEN_INVENTORY.id,
        'pen_layers': [{'pen_id': pen_id, 'ink': 'White', 'nominal_nib_mm': inventory[pen_id].nominal_nib_mm,
                       'exported_stroke_width_mm': inventory[pen_id].mark_width_mm,
                       'path_count': pen_counts[pen_id], 'minimum_path_length_mm': minimum_lengths[pen_id]}
                      for pen_id in sorted(pen_counts)],
        'single_pass_paths': True, 'no_live_text_or_raster': True, 'no_filled_font_outlines': True,
        'font_id': STROKE_FONT_ID,
        'font_source_sha256': hashlib.sha256((ROOT/'src/city_map_plotter/stroke_font.py').read_bytes()).hexdigest(),
        'lettering_checks': lettering, 'complete_text_block_count': len(lettering),
        'minimum_cap_to_nib_ratio': min(r['cap_height_mm']/inventory[r['pen_id']].mark_width_mm for r in lettering),
        'all_pen_files_match_master': True,
        'scope': 'Checks use the configured nominal White blueprint inventory; physical mark widths on stock remain unmeasured.'}
    (package/'evidence/plotting-verification.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


if __name__ == '__main__':
    result = audit(Path(sys.argv[1]))
    print(json.dumps(result, indent=2))

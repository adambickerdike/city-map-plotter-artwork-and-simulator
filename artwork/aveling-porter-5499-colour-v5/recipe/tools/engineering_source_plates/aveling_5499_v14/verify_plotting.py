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
from tools.engineering_source_plates.aveling_5499_v14.badge import curved_copy
from tools.engineering_source_plates.aveling_5499_v14.emblem_reliefs import relief_paths
from shapely.geometry import LineString, Polygon

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

    # Only the Invicta horse changes from the hash-pinned published master.
    previous = package / 'evidence/revision-13-master.svg'
    previous_sha='3bc631bbda64fb3b9f331e61102c26d0cb57556d596118b53daf8220485a14b2'
    assert hashlib.sha256(previous.read_bytes()).hexdigest() == previous_sha
    old_paths = list(ET.parse(previous).iter(NS+'path'))
    def is_horse(p):return p.get('data-role')=='invicta-emblem'
    def signature(p):
        return tuple(p.get(k) for k in ['d','data-role','data-copy','data-feature','data-component','data-source-ref'])
    assert Counter(signature(p) for p in paths if not is_horse(p)) == Counter(signature(p) for p in old_paths if not is_horse(p)), 'unrequested engine/plaque/furniture change'
    assert meta['engine_offset_mm']==[0,6]
    badge=meta['badge']
    assert badge['arrangement']=='worksplate-left-horse-right'
    assert abs(badge['worksplate_enlargement_from_v11']-1.2)<1e-9
    def controls(p):
        points=[p.start]
        for segment in p.segments:
            if not isinstance(segment,LineSegment):points.extend([segment.control_1,segment.control_2])
            points.append(segment.to)
        return points

    data=json.loads((Path(__file__).parent/'invicta-emblem.json').read_text())
    photo=package/'evidence/invicta-gwra-2025-reference.jpg'
    assert hashlib.sha256(photo.read_bytes()).hexdigest()==data['source_sha256']==badge['horse_source_sha256']
    rotation=Affine2D(**data['orientation_transform'])
    base=Affine2D(**badge['horse_source_to_base_transform'])
    paper=Affine2D(**badge['horse_base_to_paper_transform'])
    assert data['orientation_clockwise_degrees']==45
    assert abs(math.degrees(math.atan2(rotation.b,rotation.a))-45)<1e-10
    assert abs(rotation.a-rotation.d)<1e-12 and abs(rotation.b+rotation.c)<1e-12
    assert abs(rotation.a**2+rotation.b**2-1)<1e-12
    assert base.a==base.d and paper.a==paper.d
    assert all(t.b==t.c==0 for t in [base,paper])
    expected={name:p.transformed(rotation).transformed(base).transformed(paper) for name,p in relief_paths()}
    assert len(data['paths'])==len(data['source_paths'])==1
    source_outline=parse_absolute_path_data(data['source_paths'][0]).transformed(rotation)
    outline=parse_absolute_path_data(data['paths'][0])
    assert len(controls(source_outline))==len(controls(outline))
    rotation_error=max(math.dist(a,b) for a,b in zip(controls(source_outline),controls(outline)))
    assert rotation_error<.002
    expected['source-casting-contour']=outline.transformed(base).transformed(paper)
    horse={p.get('data-feature'):p for p in paths if is_horse(p)}
    assert len(horse)==len(expected)==40
    assert set(horse)==set(expected)
    errors=[]
    for name,path in expected.items():
        actual=parse_absolute_path_data(horse[name].get('d'))
        assert len(controls(path))==len(controls(actual)),name
        delta=max(math.dist(a,b) for a,b in zip(controls(path),controls(actual)))
        assert delta<.0015,(name,delta)
        errors.append(delta)
    outline_path=parse_absolute_path_data(horse['source-casting-contour'].get('d'))
    assert math.dist(outline_path.start,outline_path.end)<.001
    contour=Polygon(outline_path.flatten(.001).points)
    assert contour.is_valid
    relief_lines={name:LineString(parse_absolute_path_data(el.get('d')).flatten(.001).points)
        for name,el in horse.items() if name!='source-casting-contour'}
    # 0.20 mm contour radius + 0.15 mm relief radius: interior relief ink
    # cannot cross the silhouette. Distinct relief strokes do not intersect.
    contour_clearances={name:line.distance(contour.boundary) for name,line in relief_lines.items()}
    for name,line in relief_lines.items():
        assert contour.covers(line),(name,'relief outside casting')
        assert contour_clearances[name]>=.35,(name,'relief touches outline')
    pair_clearances=[(a,b,la.distance(lb)) for i,(a,la) in enumerate(relief_lines.items())
        for b,lb in list(relief_lines.items())[i+1:]]
    assert min(d for a,b,d in pair_clearances)>=.35
    horse_bounds=[parse_absolute_path_data(el.get('d')).bounds() for el in horse.values()]
    plate=badge['worksplate_ellipse_mm'];slot=badge['horse_slot_mm']
    horse_left=min(b.min_x for b in horse_bounds);horse_right=max(b.max_x for b in horse_bounds)
    horse_mid_y=(min(b.min_y for b in horse_bounds)+max(b.max_y for b in horse_bounds))/2
    assert abs(horse_left-(plate['cx']+plate['rx'])-badge['inter_vignette_gap_mm'])<.001
    assert badge['inter_vignette_gap_mm']>=6
    assert abs(horse_mid_y-plate['cy'])<.001
    assert abs((horse_left+horse_right)/2-slot['cx'])<.001
    assert horse_left>=slot['cx']-slot['width']/2 and horse_right<=slot['cx']+slot['width']/2
    emblem_checks={'source_id':data['source_id'],'source_sha256':data['source_sha256'],
        'orientation_clockwise_degrees':45,'uniform_scaling_only':True,
        'source_rotation_coordinate_error_px':rotation_error,
        'continuous_closed_outline_count':1,'outline_segment_count':len(outline_path.segments),
        'named_relief_count':len(relief_lines),'relief_features':list(relief_lines),
        'maximum_export_coordinate_error_mm':max(errors),
        'minimum_relief_to_contour_centreline_clearance_mm':min(contour_clearances.values()),
        'minimum_relief_pair_centreline_clearance_mm':min(d for a,b,d in pair_clearances),
        'all_relief_ink_inside_outline':True,'no_crossed_relief_strokes':True,
        'slot_mm':slot,'actual_width_mm':horse_right-horse_left,
        'actual_height_mm':max(b.max_y for b in horse_bounds)-min(b.min_y for b in horse_bounds),
        'scope':'Casting outline traced from retained photograph, with selected reliefs redrafted as smooth pen-readable curves. Orientation is an in-plane presentation rotation, not surveyed perspective correction.'}
    assert set(pen_counts) == set(inventory)
    layer_paths = Counter()
    for pen_file in (package/'artwork').glob('*.pen-*.svg'):
        layer_paths.update(p.get('d') for p in ET.parse(pen_file).iter(NS+'path'))
    assert layer_paths == Counter(p.get('d') for p in paths)
    report = {
        'master_svg_sha256': hashlib.sha256(svg.read_bytes()).hexdigest(),
        'wheel_perimeter_guides_absent': True, 'ground_line_absent': True,
        'engine_offset_mm':[0,6],'worksplate_enlargement_from_v11':badge['worksplate_enlargement_from_v11'],
        'badge_arrangement':badge['arrangement'],'badge_vertical_centre_error_mm':horse_mid_y-plate['cy'],
        'baseline_revision':13,'baseline_svg_sha256':previous_sha,
        'all_non_horse_paths_unchanged_from_v13':True,
        'unchanged_non_horse_path_count':sum(not is_horse(p) for p in paths),
        'only_changed_role':'invicta-emblem','emblem_checks':emblem_checks,
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

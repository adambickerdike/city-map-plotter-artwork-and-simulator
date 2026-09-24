#!/usr/bin/env python3
"""Colour the final revision-14 drawing with pen lines inside its black outlines."""
import argparse, hashlib, json, sys
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from city_map_plotter.niche_common import ArtworkLayer, PlateArtwork, PlateContext, Rect, write_plate
from tools.engineering_source_plates.aveling_5499_colour_v5.inventory import BROWN_PEN, EDITION_PEN_INVENTORY, GOLD_NIB_MM, GOLD_PEN, PENS
from tools.engineering_source_plates.aveling_5499_colour_v5 import hatching as H
from tools.engineering_source_plates.aveling_5499_colour_v5.design import PEN_ORDER
from tools.engineering_source_plates.aveling_5499_colour_v5.painters import minimum_length
from tools.engineering_source_plates.aveling_5499_colour_v5.plan import build_plan
from tools.engineering_source_plates.aveling_5499_colour_v5.regions import BASE_MM, GAP_MM

ID = 'aveling-porter-5499-colour-hatched'
DEFAULT_OUTPUT = ROOT / 'examples/technical-objects/aveling-porter-5499-colour-v5'
BASE_SHA = 'f4106404c710e89bda8e2b32e4cfd92d9ff1c82b4f30c9ec7cbba89621e33678'
EDITION_DATE = '2026-09-24'
OUTLINE_LAYERS = {'black-0-25': ('outline-fine', 'Fine details, fittings, lettering and the badges'),
                  'black-0-4': ('outline-engine', 'Engine linework'),
                  'black-0-6': ('outline-principal', 'Principal engine outlines and sheet frame'),
                  'black-1': ('outline-silhouette', 'Roller tyre silhouettes'),
                  GOLD_PEN: ('outline-brass', 'Inner lines of the brass boiler bands, drawn in Gold')}
FILL_LAYERS = {GOLD_PEN: ('fill-brass', "Brass: boiler bands, valves and the cylinder maker's plate"),
               'green-0-25': ('fill-green', 'Green paint'),
               'red-0-25': ('fill-red', 'Red of the red-brown fork and scrapers'),
               BROWN_PEN: ('fill-brown', 'Brown lines darkening the red-brown fork and scrapers'),
               'black-0-25': ('fill-black', 'Black iron hatching: wheels, flywheel, chimney, smokebox, headstock, firebox, fittings; shade lines')}
SKIP_ATTRIBUTES = {'d', 'data-logical-layer', 'data-role', 'data-source-ref', 'data-sequence'}


class EditionLayer(ArtworkLayer):
    """A layer whose pen comes from this edition's inventory (fine gold)."""

    @property
    def pen(self):
        return PENS[self.pen_id]


def fill_path(stroke):
    if stroke.arc is not None:
        centre, radius, start, sweep = stroke.arc
        return H.arc_path(centre, radius, start, sweep)
    return H.line_path(stroke.line)


def build(output, dpi):
    evidence = output / 'evidence'
    base = evidence / 'revision-14-master.svg'
    assert hashlib.sha256(base.read_bytes()).hexdigest() == BASE_SHA
    source_manifest = json.loads((evidence / 'revision-14-master.plot.json').read_text())
    facts = json.loads((evidence / 'revision-14-sources.json').read_text())
    for source in facts['sources']:
        for fk, hk in [('file', 'sha256'), ('working_file', 'working_sha256'), ('video_file', 'video_sha256')]:
            if source.get(fk):
                assert hashlib.sha256((evidence / source[fk]).read_bytes()).hexdigest() == source[hk]
    context = PlateContext.load('a3-landscape')
    custom = {k: Rect(**v) for k, v in source_manifest['page']['zones_mm'].items() if k.startswith('heritage_')}
    context = replace(context, zones={**context.zones, **custom}, field=custom['heritage_drawing'])

    plan = build_plan(base)
    layers = {}
    for pen, (layer_id, label) in FILL_LAYERS.items():
        layers[layer_id] = EditionLayer(layer_id, label, pen)
    for pen, (layer_id, label) in OUTLINE_LAYERS.items():
        layers[layer_id] = EditionLayer(layer_id, label, pen)

    # Within the shared Black 0.25 mm pen the fills come first and the fine
    # outlines last, so linework is always laid over the colour.
    fill_records = []
    counts = defaultdict(Counter)
    lengths = defaultdict(float)
    for zone, stroke in plan.strokes():
        path = fill_path(stroke)
        if path is None:
            continue
        length = path.length(0.001)
        if length + 1e-9 < minimum_length(stroke.pen):
            continue
        layer = layers[FILL_LAYERS[stroke.pen][0]]
        attributes = {'data-fill-part': zone.name, 'data-fill-material': zone.material,
                      'data-fill-role': stroke.role}
        layer.add_path(path, role='colour-fill', source_ref=f'fill:{zone.name}', attributes=attributes)
        counts[zone.name][stroke.pen] += 1
        lengths[stroke.pen] += length
        fill_records.append((zone.name, stroke.pen))
    for source in plan.source:
        layer = layers[OUTLINE_LAYERS[source.pen_id][0]]
        attributes = {k: v for k, v in source.attributes.items() if k not in SKIP_ATTRIBUTES}
        attributes['data-base-path-index'] = str(source.index)
        layer.add_path(source.path, role=source.get('data-role'), source_ref=source.get('data-source-ref'),
                       sequence=source.index, attributes=attributes)

    zones_record = []
    for zone in plan.zones:
        zones_record.append({
            'part': zone.name, 'material': zone.material, 'painter': zone.painter_record,
            'seed_points_mm': [list(s) for s in zone.seeds], 'cell_count': len(zone.cells),
            'cell_area_mm2': round(sum(c.area for c in zone.cells), 3),
            'strokes_by_pen': dict(counts[zone.name]), 'note': zone.note})
    fill_plan = {
        'schema': 'aveling-5499-lined-colour-plan-v2', 'base_master_sha256': BASE_SHA,
        'paper': 'white', 'inventory': EDITION_PEN_INVENTORY.id,
        'gold_pen': f"Gold {GOLD_NIB_MM:.2f} mm, the studio's gold (user-confirmed); every gold part is several fine lines",
        'marks': 'single-pass pen lines only; no dots or fills',
        'clearance_model': {'cell_offset_beyond_black_ink_mm': BASE_MM, 'white_paper_between_inks_mm': GAP_MM,
                            'fill_erosion_mm': 'nib/2 + white gap - cell offset',
                            'minimum_fill_stroke_mm': "max(3 x nib, 1.2); ruled Gold on the maker's plate at least 2.0",
                            'gold_line_spacing_mm': 0.5,
                            'boiler_band_line_spacing_mm': 'even from edge to edge, about 0.42, stepping out from '
                                                           "the band's own Gold inner line",
                            'strokes': 'every hatch line is a separate stroke with clean ends, in serpentine order'},
        'lighting': 'Upper-left light: graded parallel lines model the boiler, smokebox and chimney as cylinders. '
                    'The black iron wheels, hubs and flywheel are close, even Black circles all round; flat plates '
                    'darken slightly to the lower right.',
        'outline_weights': 'Engine linework one pen heavier than the blueprint: 0.30 -> Black 0.40, 0.40 -> Black 0.60, '
                           'roller tyres 0.50 -> Black 1.00. Fasteners, chains, small fittings, scrapers, controls, '
                           'lettering, the badges and the sheet frame keep their original weights. The four '
                           'inner lines of the brass boiler bands are drawn in Gold, unchanged in shape.',
        'wheel_faces': [{'name': f.name, 'centre_mm': list(f.centre), 'split_radius_mm': f.split_radius_mm,
                         'opening_cuts': f.opening_cuts, 'spoke_pieces': len(f.spokes), 'rim_pieces': len(f.rim)}
                        for f in plan.wheel_faces],
        'spokes': 'Every spoke of a wheel ruled with the same number of lines parallel to its edges: '
                  'five on the rear wheel, four on the front roll.',
        'through_the_wheels': 'Rear wheel: everything seen between the spokes up to the tender front edge '
                              '(x = %.3f mm) and above its top rim is black iron in Black lines about 0.69 mm apart, '
                              'lighter than the black wheel rims; the tender '
                              'beyond that edge continues in green. Front roll: a hollow drum, so the spaces '
                              'between its spokes stay paper.' % plan.tender_edge_x,
        'badges': 'The worksplate and Invicta horse at the top right are uncoloured black engraving.',
        'paper_cell_count': len(plan.cellmap.cells), 'coloured_cell_count': len(plan.claimed),
        'parts': zones_record,
        'fill_strokes_by_pen': dict(Counter(pen for _, pen in fill_records)),
        'fill_length_m_by_pen': {k: round(v / 1000, 3) for k, v in lengths.items()},
        'unfilled_by_design': ['sheet margins and background paper', 'the inside of the front roll, seen between its spokes',
                               'the open air under the regulator rod on both sides of the flywheel, and between the lubricator pedestal and its pipe',
                               'the worksplate and Invicta horse at the top right',
                               'lettering, footer and rivet heads', 'chain links and spring coils'],
        'colour_scope': 'Photograph-informed paint groups translated to available studio inks; not measured colour matching.'}

    sources = facts['sources'] + [{'id': 'user-lined-colour-edition', 'publisher': 'User', 'date': EDITION_DATE,
        'supports': ['Colour the latest blueprint drawing in with fine lines inside a maintained black outline, optimised for colour pen plotting',
                     'Revision: lines not dots on the horse and both nameplates; even wheel shading; colour the engine seen through the rear wheel; consistent spoke lines; a more professional finish',
                     'Revision: rear scraper red with green around it; blacker wheels and flywheel; blacker outlines; black through the rear wheel up to the straight vertical line; worksplate and horse at the top right without colour',
                     'Revision: the black behind the rear wheel lighter; no broad gold pen, so every gold part built from multiple fine lines',
                     'Revision: no grey pen, so the iron tones are drawn in black line hatching with the same look',
                     'Revision: the red pen is too bright, so the red parts are darkened with the studio 0.25 mm brown pen alternating with the red lines',
                     'Revision: fill the whole of each gold boiler band with colour, not only the side to the right of its inner line',
                     'Revision: leave the air gap under the regulator rod (handle, behind the flywheel, to the front) and between the lubricator pipes unfilled; the boiler beside the flywheel green, not black; the gold band nearest the flywheel up to the top; colour the top-right spoke of the front roll']}]
    notes = [n.replace('White 0.30/0.40/0.50 mm pens on blue stock.', 'The source blueprint used White 0.30/0.40/0.50 mm pens on blue stock.') for n in facts['notes']]
    notes += [
        'Colour edition 5: every one of the 863 revision-14 paths is retained unchanged in shape. 859 are black outlines; engine linework is drawn one pen heavier than the blueprint (Black 0.40/0.60 mm, roller tyres Black 1.00 mm); fasteners, chains, small fittings, scrapers, lettering, badges and frame keep Black 0.25/0.40/0.60 mm. The inner lines of the three brass boiler bands (four paths) are drawn in Gold.',
        'Colour is added only as single-pass pen lines inside the enclosed paper cells, kept at least 0.22 mm clear of black ink. The Gold band inner lines meet black ink only at their ends and where they cross the boiler lines, as the blueprint draws them.',
        'Open air stays paper: under the regulator rod on both sides of the flywheel and between the lubricator pedestal and its pipe. The boiler barrel seen between the motion plate and the pump rod beside the flywheel is green, and the gold band nearest the flywheel runs up to the motion plate.',
        'The wheels, hubs and flywheel are black iron drawn as close, even Black circles. Everything seen through the rear wheel up to the tender front edge is black iron in Black lines about 0.69 mm apart, lighter than the black wheels; the green tender continues beyond that edge.',
        'Spokes are ruled with a fixed number of lines exactly parallel to their edges. Cylinders are graded lines under upper-left light. The fork, scrapers, scraper mounts and chain spring bar are red-brown: Red lines with a Brown line in every gap, 0.30 mm apart, so the red reads darker and warmer.',
        'Black-painted iron (chimney, smokebox, headstock, firebox, fittings and controls) is hatched in Black alone, its tone set by line spacing; the studio has no grey pen.',
        'The worksplate and Invicta horse at the top right are left as black engraving without colour. The boiler bands, valves, whistle and the maker plate on the cylinder are brass, built from fine lines with the studio Gold 0.40 mm pen; there is no broad gold nib. Each boiler band is a solid gold bar of upright lines evenly spaced about 0.42 mm apart from one black edge to the other, its inner line drawn in Gold as one of them; the valves, whistle and maker plate are lined 0.50 mm apart.',
    ]
    meta = dict(json.loads((evidence / 'revision-14-reconstruction.json').read_text())['metadata'])
    meta.update(paper_preview_color='#ffffff', physical_inks=['Gold', 'Green', 'Red', 'Brown', 'Black'], gold_nib_mm=GOLD_NIB_MM,
                colour_edition='lined-colour-v5', base_master_sha256=BASE_SHA,
                colour_plan='evidence/fill-plan.json', geometry_record='evidence/revision-14-reconstruction.json',
                current_geometry_scope='All 863 frozen revision-14 path shapes unchanged: black outlines with the engine linework one pen heavier, and the four brass-band inner lines in Gold; pen-line colour added inside.',
                base_geometry_path_count=len(plan.source), unchanged_path_count=len(plan.source),
                fill_stroke_count=len(fill_records), white_gap_mm=GAP_MM)
    meta.pop('physical_ink', None)
    art = PlateArtwork(subject_id=ID, domain='heritage-engine-portrait', subject_kind='steam-road-roller',
        title='AVELING & PORTER', subtitle='R6 STEAM ROAD ROLLER', details=('WORKS No. 5499 / 1904', 'REG. BS 8711'),
        credit_line=source_manifest['source']['attribution'], scale_status='interpreted-side-elevation-not-to-scale',
        evidence_status='source-referenced-analytic-drafting', rights_status=source_manifest['rights']['status'],
        rights_metadata={'logos_or_trade_dress_used': True}, sources=tuple(sources), context=context,
        layers=list(layers.values()), pen_order=PEN_ORDER, artifact_kind='interpreted-engine-elevation',
        rendering_preset='heritage-engine-lined-colour-v5', format_subject_policy='user-authorised-single-elevation-bottom-strip',
        rendering_metadata=meta, svg_metadata={'side_elevation': meta}, notes=tuple(notes), include_standard_furniture=False,
        preview_background='#ffffff', stock_tone='light', visible_attribution=False, pen_inventory=EDITION_PEN_INVENTORY,
        data_snapshot=EDITION_DATE)
    result = write_plate(art, output / 'artwork', png_dpi=dpi, generated_at=f'{EDITION_DATE}T00:00:00+00:00')
    (evidence / 'sources.json').write_text(json.dumps({**facts, 'sources': sources, 'notes': notes}, indent=2) + '\n')
    (evidence / 'fill-plan.json').write_text(json.dumps(fill_plan, indent=2) + '\n')
    print(json.dumps({'master': result['svg']['path'], 'outline_paths': len(plan.source),
                      'fill_strokes': fill_plan['fill_strokes_by_pen'], 'fill_metres': fill_plan['fill_length_m_by_pen']}, indent=2))
    return plan


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    p.add_argument('--png-dpi', type=float, default=254)
    args = p.parse_args()
    build(args.output_dir, args.png_dpi)

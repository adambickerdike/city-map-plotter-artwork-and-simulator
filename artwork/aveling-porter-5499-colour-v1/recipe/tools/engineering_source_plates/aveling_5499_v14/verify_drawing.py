#!/usr/bin/env python3
"""Check the exported plate's alignment, local joins, circles and smooth fillets.

These checks concern drawing consistency, not surveyed engine dimensions.
"""
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from city_map_plotter.technical_assets import parse_absolute_path_data
from city_map_plotter.vector_path import LineSegment, Affine2D
from shapely.geometry import LineString, Point
from shapely.affinity import affine_transform
from shapely.ops import unary_union
from shapely.strtree import STRtree
from tools.engineering_source_plates.aveling_5499_v14.drafting import circle, rect, region, visible_paths
from tools.engineering_source_plates.aveling_5499_v14.engine import wheel, return_pipe_mask, side_components
from tools.engineering_source_plates.aveling_5499_v14.verify_plotting import audit

package = Path(sys.argv[1])
evidence = package / 'evidence'
svg = package / 'artwork/aveling-porter-5499-blueprint.svg'
record = json.loads((evidence / 'reconstruction.json').read_text())
manifest = json.loads(svg.with_suffix('.plot.json').read_text())
g, meta = record['geometry'], record['metadata']
m = meta['view_transform']
scale = m['a']

def paper(q):
    return (q[0] * scale + m['e'], q[1] * scale + m['f'])

tree = ET.parse(svg)
paths = list(tree.iter('{http://www.w3.org/2000/svg}path'))
assert not any(e.tag.split('}')[-1] in ['image', 'text', 'rect', 'circle', 'ellipse', 'polygon'] for e in tree.iter())
engine = [p for p in paths if p.get('data-role') == 'side-elevation-component']
vectors = [parse_absolute_path_data(e.get('d')) for e in engine]
lines = [LineString(p.flatten(.002).points) for p in vectors]
assert len(engine) == len(record['retained'])
assert {p.get('data-view') for p in engine} == {'side'}
assert m['a'] == m['d'] and m['b'] == m['c'] == 0
assert meta['view_count'] == 1 and not any(meta[k] for k in ['front_view', 'canopy', 'horse_on_engine', 'callout_leaders'])
parents={child:parent for parent in tree.iter() for child in parent}
rim_pen_checks=[]
for el in engine:
    if not el.get('data-feature','').startswith('fitted-circular-rim'):continue
    node=el
    while node is not None and node.get('stroke-width') is None:node=parents.get(node)
    assert node is not None and float(node.get('stroke-width'))==.5
    rim_pen_checks.append({'component':el.get('data-component'),'line_width_mm':.5})

def selected(component, feature=None):
    parts = [p for p, e in zip(lines, engine) if e.get('data-component') == component
             and (feature is None or e.get('data-feature', '').startswith(feature))]
    assert parts, (component, feature)
    return unary_union(parts)

# Check every open engine-contour endpoint, excluding its own path. The only
# permitted unattached ends are the two free legs of the photographed cotter.
# 0.20 mm is below the combined half-width of even two 0.30 mm pen strokes.
spatial = STRtree(lines)
endpoints = []
for i, p in enumerate(vectors):
    if math.dist(p.start, p.segments[-1].to) < .001:
        continue
    for end, q in [('start', p.start), ('end', p.segments[-1].to)]:
        ids = [int(j) for j in spatial.query(Point(q).buffer(4)) if int(j) != i]
        assert ids, ('isolated endpoint', i, end)
        distance, j = min((lines[j].distance(Point(q)), j) for j in ids)
        intentional = engine[i].get('data-feature') == 'cotter-with-free-tip' and end == 'end'
        assert distance <= .20 or intentional, (engine[i].attrib, end, q, distance)
        endpoints.append({'component': engine[i].get('data-component'), 'feature': engine[i].get('data-feature'),
                          'end': end, 'point_mm': q, 'nearest_gap_mm': distance,
                          'intentional_free_tip': intentional})

joint_checks = []
for ca, fa, cb, fb in [
    ('front-casting', None, 'front-kingpin-and-fork', None),
    ('front-kingpin-and-fork', None, 'front-scraper', None),
    ('front-scraper', None, 'near-steering-chain', None),
    ('steering-gear-housing', None, 'near-steering-chain', None),
    ('cylinder-cover', None, 'motion-bed-and-bearings', None),
    ('boiler-and-smokebox', None, 'cylinder-cover', None),
    ('boiler-and-smokebox', None, 'rear-frame-and-platform', None),
    ('solid-flywheel', None, 'motion-bed-and-bearings', None),
    ('rear-frame-and-platform', None, 'tender-and-water-box', None),
    ('rear-axle-and-drive-pin', None, 'rear-driving-roll', None),
    ('feed-control-and-pipe', 'feed-valve-spindle', 'feed-control-and-pipe', 'feed-lever-boss-b'),
    ('rear-brake-and-scraper', 'brake-spindle', 'rear-brake-and-scraper', 'brake-link-boss-b'),
]:
    gap = selected(ca, fa).distance(selected(cb, fb))
    assert gap < .002, (ca, fa, cb, fb, gap)
    joint_checks.append({'components': [ca, cb], 'features': [fa, fb], 'minimum_emitted_line_gap_mm': gap})
chain_runs=[]
for component in ['near-steering-chain','far-steering-chain']:
    ink=selected(component).buffer(.149)
    count=len(ink.geoms) if hasattr(ink,'geoms') else 1
    # At the pipe, the far chain's last link exposes two separate arcs before
    # disappearing into the housing; those are intentional occluded fragments.
    assert count==(3 if component=='far-steering-chain' else 1),(component,count)
    chain_runs.append({'component':component,'connected_visible_runs':count})
chain_groups=len(chain_runs)
pipe_mask=affine_transform(return_pipe_mask(),[scale,0,0,scale,m['e'],m['f']])
pipe_intrusion=selected('far-steering-chain').intersection(pipe_mask.buffer(-.002)).length
assert pipe_intrusion<.001,('far chain crosses return pipe',pipe_intrusion)
old_tree=ET.parse(evidence/'revision-9-master.svg')
old_far=unary_union([LineString(parse_absolute_path_data(p.get('d')).flatten(.0001).points)
    for p in old_tree.iter('{http://www.w3.org/2000/svg}path') if p.get('data-component')=='far-steering-chain'])
old_far=affine_transform(old_far,[1,0,0,1,*meta['engine_offset_mm']])
old_intrusion=old_far.intersection(pipe_mask.buffer(-.002)).length
assert old_intrusion>.5,('expected prior pipe crossing',old_intrusion)
pipe_gap=selected('far-steering-chain').distance(selected('under-boiler-fittings','return-pipe'))
assert pipe_gap<.002,('far chain must stop at pipe boundary',pipe_gap)

# Edge-on wheel: no face ellipses or visible face spokes. Its actual long rim
# edges must be perpendicular to the shaft after the side-plane projection.
handwheel_checks={}
assert not g['steering_handwheel']['visible_face']
assert not any(e.get('data-feature','').startswith(('steering-wheel-inner-rim','steering-wheel-spoke','steering-wheel-centre')) for e in engine)
rim=selected('driver-controls','steering-wheel-edge-rim')
hub=selected('driver-controls','steering-wheel-hub')
assert rim.distance(hub)<.025
shaft=selected('far-steering-column','steering-column-shaft')
shaft_gap=shaft.distance(unary_union([rim,hub]))
assert shaft_gap<.025
handwheel_checks['shaft_to_occluding_handwheel_gap_mm']=shaft_gap
a,b=map(paper,[g['steering_column']['wheel_centre'],g['steering_column']['far_winder']])
assert b[0]<a[0] and b[1]>a[1]
shaft_angle=math.degrees(math.atan2(b[1]-a[1],a[0]-b[0]))
assert 20<shaft_angle<40
shaft_axis=LineString([a,b])
shaft_paths=[p for p,e in zip(vectors,engine) if e.get('data-component')=='far-steering-column']
shaft_deviations=[shaft_axis.distance(Point(q)) for p in shaft_paths for q in p.flatten(.001).points]
assert max(shaft_deviations)<2.7*scale+.002
assert math.dist(a,paper(g['detail_centres']['steering_wheel']))<.001
assert shaft.length>4
rear_centre=paper(g['rear_axle']);rear_radius=g['rear_radius']*scale
rear_clearance=min(math.dist(q,rear_centre)-rear_radius for p in shaft_paths for q in p.flatten(.0001).points)
assert rear_clearance>=-.025
handwheel_checks['minimum_shaft_rear_silhouette_clearance_mm']=rear_clearance
handwheel_checks['visible_shaft_edge_length_mm']=shaft.length
handwheel_checks['shaft_descent_angle_degrees']=shaft_angle
old_centre=paper(g['steering_handwheel']['previous_centre'])
assert 1<old_centre[0]-a[0]<6 and abs(old_centre[1]-a[1])<.001
handwheel_checks['forward_shift_mm']=old_centre[0]-a[0]
edge_errors=[]
for p,e in zip(vectors,engine):
    if e.get('data-feature')!='steering-wheel-edge-rim':continue
    at=p.start
    for segment in p.segments:
        if isinstance(segment,LineSegment) and math.dist(at,segment.to)>3:
            d=(segment.to[0]-at[0],segment.to[1]-at[1]);axis=(b[0]-a[0],b[1]-a[1])
            error=abs(sum(x*y for x,y in zip(d,axis))/(math.hypot(*d)*math.hypot(*axis)))
            assert error<.001
            edge_errors.append(error)
        at=segment.to
assert edge_errors
handwheel_checks['edge_on_profile']=True
handwheel_checks['maximum_rim_shaft_normal_dot_error']=max(edge_errors)

trigger_gap=selected('driver-controls','quadrant-squeeze-handle').distance(selected('driver-controls','quadrant-release-hinge'))
assert trigger_gap<.025
release=g['quadrant_release']
assert release['moving_grip_tip'][0]<release['hinge'][0]<release['fixed_grip_tip'][0]
assert max(release['moving_grip_tip'][1],release['fixed_grip_tip'][1])<release['hinge'][1]
assert release['hinge'][1]-release['previous_hinge'][1]>=10
release_ratio=math.dist(release['hinge'],release['moving_grip_tip'])/math.dist(release['previous_hinge'],release['previous_moving_grip_tip'])
assert 1.3<release_ratio<1.7

# The new blade shares the accepted handle axis, meets the quadrant at one
# end and disappears behind the actual foreground wheel at the other. Its
# concealed construction end is fully within the existing opaque rear frame.
lower=g['quadrant_lower_blade']
blade=selected('quadrant-lower-blade')
contact,handle,hidden=map(paper,[lower['contact'],lower['handle'],lower['concealed_end']])
a=(handle[0]-contact[0],handle[1]-contact[1]);b=(hidden[0]-contact[0],hidden[1]-contact[1])
axis_cross=abs(a[0]*b[1]-a[1]*b[0])
assert axis_cross<1e-8 and sum(x*y for x,y in zip(a,b))<0
contact_gap=blade.distance(selected('driver-controls'))
wheel_gap=blade.distance(selected('rear-driving-roll','fitted-circular-rim'))
assert contact_gap<.002 and wheel_gap<.002
assert blade.bounds[3]>contact[1]+2
components={c.name:c for c in side_components()}
frame_mask=components['rear-frame-and-platform'].mask
assert frame_mask.covers(Point(lower['concealed_end']).buffer(lower['half_width_reference_units']+1))
opaque=unary_union([components[k].mask for k in lower['hidden_by']])
opaque=affine_transform(opaque,[scale,0,0,scale,m['e'],m['f']])
blade_intrusion=blade.intersection(opaque.buffer(-.025)).length
assert blade_intrusion<.001
assert all(isinstance(s,LineSegment) for p,e in zip(vectors,engine)
           if e.get('data-component')=='quadrant-lower-blade' for s in p.segments)
lower_blade_checks={'same_axis_as_upper_handle':True,'contact_gap_mm':contact_gap,
    'rear_rim_occlusion_gap_mm':wheel_gap,'visible_edge_length_mm':blade.length,
    'extension_below_quadrant_contact_mm':blade.bounds[3]-contact[1],
    'ink_inside_opaque_wheel_or_frame_mm':blade_intrusion,
    'concealed_end_inside_existing_frame':True,'unseen_pivot_not_exposed':True}

routes=g['steering_chain_routes']
assert routes['near']['gear'][1]>routes['far']['gear'][1]
assert routes['near']['front']==routes['far']['front']
assert routes['near']['side']=='near' and routes['far']['side']=='far'
assert routes['near']['front'][0]<g['front_axle'][0]-.5*g['front_radius']
spring_join=selected('near-steering-chain','front-spring-chain-eye').distance(selected('front-scraper','spring-output-clevis'))
assert spring_join<.025
near_exit_distance=selected('near-steering-chain').distance(Point(paper(routes['near']['gear'])))
assert near_exit_distance<.8
far_to_casing=selected('far-steering-chain').distance(selected('steering-gear-housing'))
assert far_to_casing<.025
far_exit_distance=selected('far-steering-chain').distance(Point(paper(routes['far']['gear'])))
assert far_exit_distance>2
route_checks={'near_uses_lower_exit':True,'far_uses_upper_opposite_exit':True,
              'matching_leading_spring_attachments':True,'spring_eye_join_mm':spring_join,
              'connected_visible_runs':chain_runs,'near_chain_to_lower_exit_mm':near_exit_distance,
              'far_chain_to_foreground_casing_mm':far_to_casing,
              'far_attachment_hidden_inside_casing_mm':far_exit_distance}

fit=g['feed_fitting_clearance']
fb=selected('feed-control-and-pipe','feed-fitting-outline').bounds
left_clear=fb[0]-paper((fit['adjacent_band_inside_edges'][0],0))[0]
right_clear=paper((fit['adjacent_band_inside_edges'][1],0))[0]-fb[2]
assert min(left_clear,right_clear)>3
assert abs(left_clear-right_clear)<.5
assert (fb[2]-fb[0])>2*fit['previous_half_width']*scale*1.15
assert fit['centre'][0]<fit['previous_centre'][0]
fitting_checks={'left_band_clearance_mm':left_clear,'right_band_clearance_mm':right_clear,'width_mm':fb[2]-fb[0]}

# Inner spokes must be visibly present but cannot draw through the opaque
# outer cast spokes, hubs or rims. Inspect the emitted strokes against each
# outer face's analytic mask, with a sub-nib allowance for curve flattening.
spoke_occlusion_checks=[]
for key,outer_id,inner_id,count,phase in [('rear','rear-driving-roll','rear-inner-spokes',10,8),
                                       ('front','front-roll-end','front-inner-spokes',6,16)]:
    c=wheel(outer_id,*g[key+'_axle'],g[key+'_radius'],count,phase)
    mask=affine_transform(c.mask,[scale,0,0,scale,m['e'],m['f']]).buffer(-.025)
    inner=selected(inner_id)
    intrusion=inner.intersection(mask).length
    assert intrusion < .001, (inner_id,intrusion)
    assert inner.length > 10
    arrangement=g['wheel_spoke_arrangement'][key]
    assert arrangement['spokes_per_set']==count
    assert g['wheel_rims'][key]['radial_depth']>g['wheel_rims'][key]['previous_radial_depth']
    expected_outer=(g[key+'_radius']-g['wheel_rims'][key]['radial_depth'])*scale
    window_points=[q for p,e in zip(vectors,engine) if e.get('data-component')==outer_id and e.get('data-feature')=='cast-spoke-opening' for q in p.flatten(.001).points]
    maximum_radius=max(math.dist(q,paper(g[key+'_axle'])) for q in window_points)
    assert abs(maximum_radius-expected_outer)<.002
    assert arrangement['outer_root_sweep_degrees'] > 0 > arrangement['inner_root_sweep_degrees']
    spoke_occlusion_checks.append({'component':inner_id,'visible_length_mm':inner.length,'intrusion_into_outer_casting_mm':intrusion})

# Circles are checked after projection, clipping and SVG quantisation. A
# whole-circle feature may be emitted as several arcs; all arcs must fit.
circle_checks = []
for constraint in g['circular_constraints']:
    matches = [p for p, e in zip(vectors, engine) if e.get('data-component') == constraint['component']
               and e.get('data-feature') == constraint['feature']]
    if not matches:  # A fully occluded/sub-nib feature is not an emitted circle.
        continue
    centre, radius = paper(constraint['centre']), constraint['radius'] * scale
    error = max(abs(math.dist(q, centre) - radius) for p in matches for q in p.flatten(.0001).points)
    assert error < .001, (constraint, error)
    circle_checks.append({'component': constraint['component'], 'feature': constraint['feature'],
                          'centre_mm': centre, 'radius_mm': radius, 'maximum_radial_error_mm': error})
for key in ['front', 'rear']:
    assert abs(paper(g[key + '_axle'])[1] + g[key + '_radius'] * scale - meta['ground_y_mm']) < .001

# Check the exact tangencies AND the quantised export. Very short handles at
# hidden-line cuts can change angle when rounded by 0.0005 mm; the rigorous
# angular bound depends on handle length, not an arbitrary one-degree limit.
def tangencies(p):
    at = p.start
    tangents = []
    for seg in p.segments:
        if isinstance(seg, LineSegment):
            a = b = (seg.to[0] - at[0], seg.to[1] - at[1])
        else:
            a = next((q[0] - at[0], q[1] - at[1]) for q in [seg.control_1, seg.control_2, seg.to] if math.dist(q, at) > 1e-8)
            b = next((seg.to[0] - q[0], seg.to[1] - q[1]) for q in [seg.control_2, seg.control_1, at] if math.dist(q, seg.to) > 1e-8)
        tangents.append((a, b))
        at = seg.to
    pairs = list(zip(tangents, tangents[1:]))
    if math.dist(p.start, p.segments[-1].to) < .001:
        pairs.append((tangents[-1], tangents[0]))
    return pairs

def angle_error(v,w):
    return abs(math.degrees(math.atan2(v[0]*w[1]-v[1]*w[0],v[0]*w[0]+v[1]*w[1])))

def control_points(p):
    points=[p.start]
    for s in p.segments:
        if not isinstance(s,LineSegment):points.extend([s.control_1,s.control_2])
        points.append(s.to)
    return points

tangent_errors=[];exact_tangent_errors=[];coordinate_errors=[];quantisation_bounds=[]
construction={str(r['index']):r for r in record['retained']}
for p,el in zip(vectors,engine):
    if el.get('data-feature')!='cast-spoke-opening':continue
    source=parse_absolute_path_data(construction[el.get('data-model-path-index')]['construction_path']).transformed(Affine2D(**m))
    src_points,out_points=control_points(source),control_points(p)
    assert len(src_points)==len(out_points)
    for a,b in zip(src_points,out_points):
        error=math.dist(a,b)
        assert error<=math.sqrt(2)*.0005+1e-8,error
        coordinate_errors.append(error)
    for a,b in tangencies(source):
        error=angle_error(a[1],b[0])
        assert error<.00001,error
        exact_tangent_errors.append(error)
    for a,b in tangencies(p):
        v, w = a[1], b[0]
        error=angle_error(v,w)
        bound=sum(math.degrees(math.asin(min(1,math.sqrt(2)*.001/math.hypot(*q)))) for q in [v,w])+.00001
        assert error<=bound,(error,bound)
        quantisation_bounds.append(bound)
        tangent_errors.append(error)

# The replacement is a vertical continuation of the control housing, not a
# diagonal shoulder. Its quarter-circle has matching line/arc tangents, and
# visible exported pieces must lie on that same analytic outline.
shoulder=g['firebox_shoulder']
assert abs(shoulder['upright_x']-shoulder['control_housing_rear_x'])<1e-9
assert shoulder['upright_top_y']<shoulder['housing_bottom_y']
# The intervening rear rim/spokes intentionally conceal sections of this joint.
# Both ends must meet that occluding wheel, rather than a visible free tip.
assert selected('rear-frame-and-platform','continuous-firebox-frame').distance(selected('rear-driving-roll'))<.002
assert selected('driver-controls').distance(selected('rear-driving-roll'))<.002
shape=parse_absolute_path_data(shoulder['construction_path'])
assert isinstance(shape.segments[0],LineSegment)
assert abs(shape.start[0]-shape.segments[0].to[0])<1e-9
shoulder_tangencies=[angle_error(a[1],b[0]) for a,b in tangencies(shape)]
assert max(shoulder_tangencies)<.00001
assert len(shape.segments)==4
assert isinstance(shape.segments[-1],LineSegment)
arc_start=shape.segments[0].to;arc_end=shape.segments[-2].to
arc_centre=shoulder['corner_centre'];r=shoulder['corner_radius']
assert math.dist(arc_start,(arc_centre[0]-r,arc_centre[1]))<1e-9
assert math.dist(arc_end,(arc_centre[0],arc_centre[1]+r))<1e-9
centre=paper(shoulder['corner_centre']);radius=shoulder['corner_radius']*scale
upright_x=paper((shoulder['upright_x'],0))[0]
corner_errors=[];visible_upright=[]
for path,element in zip(vectors,engine):
    if element.get('data-feature')!='continuous-firebox-frame':continue
    at=path.start
    for segment in path.segments:
        if isinstance(segment,LineSegment):
            if abs(at[0]-upright_x)<.001 and abs(segment.to[0]-upright_x)<.001:
                visible_upright.append(math.dist(at,segment.to))
        else:
            piece=type(path)(at,(segment,))
            corner_errors.extend(abs(math.dist(q,centre)-radius) for q in piece.flatten(.0001).points)
        at=segment.to
assert visible_upright and sum(visible_upright)>5
assert corner_errors and max(corner_errors)<.001
shoulder_checks={'upper_housing_alignment_error_mm':abs(shoulder['upright_x']-shoulder['control_housing_rear_x'])*scale,
    'visible_vertical_length_mm':sum(visible_upright),'corner_radius_mm':radius,
    'maximum_exact_tangent_error_degrees':max(shoulder_tangencies),
    'maximum_exported_corner_radial_error_mm':max(corner_errors),
    'diagonal_shoulder_replaced':True}

footer = manifest['page']['zones_mm']['heritage_footer']
drawing = manifest['page']['zones_mm']['heritage_drawing']
bounds = [p.bounds() for p in vectors]
engine_box = [min(b.min_x for b in bounds), min(b.min_y for b in bounds), max(b.max_x for b in bounds), max(b.max_y for b in bounds)]
centre_errors = [(engine_box[0] + engine_box[2]) / 2 - drawing['x'] - drawing['width'] / 2,
                 (engine_box[1] + engine_box[3]) / 2 - drawing['y'] - drawing['height'] / 2 - meta['engine_offset_mm'][1]]
assert max(abs(v) for v in centre_errors) < .001
for b in bounds:
    assert drawing['x'] <= b.min_x and b.max_x <= drawing['x'] + drawing['width']
    assert drawing['y'] <= b.min_y and b.max_y <= drawing['y'] + drawing['height']

copy = [p for p in paths if p.get('data-copy')]
copy_checks = []
for cell in meta['footer_cells']:
    for value, baseline in zip(cell['values'], cell['baselines']):
        items = [p for p in copy if p.get('data-copy') == value and p.get('data-role','').startswith('footer-')]
        assert items and all(p.get('data-role').startswith('footer-') for p in items)
        boxes = [parse_absolute_path_data(p.get('d')).bounds() for p in items]
        box = [min(b.min_x for b in boxes), min(b.min_y for b in boxes), max(b.max_x for b in boxes), max(b.max_y for b in boxes)]
        centre_error = (box[0] + box[2]) / 2 - cell['centre_x']
        assert abs(centre_error) < .001 and abs(box[3] - baseline) < .001, (value, box)
        assert cell['x'] < box[0] and box[2] < cell['x'] + cell['width']
        assert footer['y'] < box[1] and box[3] < footer['y'] + footer['height']
        copy_checks.append({'value': value, 'centre_error_mm': centre_error, 'baseline_mm': box[3]})
assert not any(p.get('data-role') == 'engine-maker-serial' for p in paths)
footer_words={p.get('data-copy') for p in copy if p.get('data-role','').startswith('footer-')}
assert footer_words=={'AVELING & PORTER','R6 STEAM ROAD ROLLER','WORKS No. 5499','REG. BS 8711','1904'}
assert len(meta['footer_cells'])==3
assert all(p.get('data-role').startswith('footer-') or p.get('data-role') == 'worksplate-copy' for p in copy)
assert meta['horse'] and not meta['engine_plate_lettering'] and meta['worksplate_enlargement']
# The separately enlarged identity pair must stay inside the named corner zone
# and clear the engine, with every requested word preserved as vector copy.
badge = [p for p in paths if p.get('data-role', '').startswith(('worksplate-', 'invicta-'))]
assert badge
badge_paths = [parse_absolute_path_data(p.get('d')) for p in badge]
badge_ink = unary_union([LineString(p.flatten(.001).points) for p in badge_paths])
bz = manifest['page']['zones_mm']['heritage_badge']
for p in badge_paths:
    b = p.bounds()
    assert bz['x'] <= b.min_x and b.max_x <= bz['x'] + bz['width']
    assert bz['y'] <= b.min_y and b.max_y <= bz['y'] + bz['height']
clearance = badge_ink.distance(unary_union(lines))
assert clearance > 2, ('badge/engine clearance', clearance)
plate_words = {p.get('data-copy') for p in copy if p.get('data-role') == 'worksplate-copy'}
assert plate_words == set(meta['badge']['worksplate_copy'])
assert not any('6882' in value for value in plate_words)
# Every far-chain point in the visible SVG must be outside the whole front
# roller silhouette, rather than showing through its near-side spoke openings.
front_centre = paper(g['front_axle']);front_radius = g['front_radius'] * scale
far_paths = [p for p,e in zip(vectors,engine) if e.get('data-component') == 'far-steering-chain']
far_min = min(math.dist(q,front_centre)-front_radius for p in far_paths for q in p.flatten(.0001).points)
assert far_min >= -.025, ('far chain crosses front drum', far_min)
assert any(p.get('data-component') == 'rear-forward-scraper' for p in engine)
scraper_gap = selected('rear-forward-scraper', 'scraper-blade').distance(selected('rear-driving-roll', 'fitted-circular-rim'))
assert scraper_gap < .025
# Taper is read from the retained construction path, before foreground collars
# clip it, and its emitted edges must get wider as y decreases.
barrel = [p for p,e in zip(vectors,engine) if e.get('data-feature') == 'upward-widening-chimney-barrel']
assert barrel
barrel_points = [q for p in barrel for q in p.flatten(.001).points]
ymin=min(q[1] for q in barrel_points);ymax=max(q[1] for q in barrel_points)
upper=[q[0] for q in barrel_points if q[1] < ymin+.5]
lower=[q[0] for q in barrel_points if q[1] > ymax-.5]
upper_width=max(upper)-min(upper);lower_width=max(lower)-min(lower)
assert upper_width > lower_width + 2

axis_checks = []
for component, feature, centre in [
    ('rear-brake-and-scraper', 'brake-t-handle', 'brake_handle'),
    ('rear-brake-and-scraper', 'brake-spindle', 'brake_handle'),
    ('feed-control-and-pipe', 'feed-fitting-outline', 'feed_fitting'),
    ('feed-control-and-pipe', 'feed-valve-spindle', 'feed_fitting'),
    ('cylinder-cover', 'cylinder-front-cover', 'maker_plate'),
    ('driver-controls', 'steering-wheel-hub', 'steering_wheel'),
]:
    box = selected(component, feature).bounds
    error = (box[0] + box[2]) / 2 - paper(g['detail_centres'][centre])[0]
    assert abs(error) < .001, (feature, error)
    axis_checks.append({'feature': feature, 'axis_error_mm': error})

# Regression for the fault that discarded short fragments at a closed path's
# arbitrary authoring seam. The visible right semicircle is one intact path.
semicircle = visible_paths(circle(0, 0, 1), region(rect(-2, -2, 2, 4)))
assert len(semicircle) == 1 and abs(semicircle[0].length(.0001) - math.pi) < .001

source_checks = []
for s in json.loads((evidence / 'sources.json').read_text())['sources']:
    for fk, hk in [('file', 'sha256'), ('working_file', 'working_sha256'), ('video_file', 'video_sha256')]:
        if s.get(fk):
            digest = hashlib.sha256((evidence / s[fk]).read_bytes()).hexdigest()
            assert digest == s[hk]
            source_checks.append({'file': s[fk], 'sha256': digest})
master_hash = hashlib.sha256(svg.read_bytes()).hexdigest()
job = json.loads((package / 'plot/aveling-porter-5499.plotjob.json').read_text())
assert job['preflight']['source_sha256'] == master_hash and not job['preflight']['issues']
check = subprocess.run([sys.executable, str(ROOT / 'tools/validate_format.py'), '--warnings-as-errors', str(svg)], text=True, capture_output=True, check=True)
plotting_checks = audit(package)
report = {'schema': 'aveling-5499-detail-verification-v14', 'master_svg_sha256': master_hash,
    'plotting_checks': plotting_checks,
    'return_pipe_occlusion_checks':{'previous_chain_inside_pipe_length_mm':old_intrusion,'final_chain_inside_pipe_length_mm':pipe_intrusion,'chain_to_pipe_boundary_gap_mm':pipe_gap,'far_chain_visible_fragments':3,'near_chain_visible_runs':1},
    'view_count': 1, 'uniform_projection': True, 'no_raster_or_live_text': True,
    'engine_width_mm': engine_box[2] - engine_box[0], 'engine_height_mm': engine_box[3] - engine_box[1],
    'engine_paths': len(engine), 'all_svg_paths': len(paths), 'engine_centre_errors_mm': centre_errors,
    'footer_alignment_checks': copy_checks, 'detail_axis_checks': axis_checks,
    'endpoint_check_count': len(endpoints), 'endpoint_tolerance_mm': .20,
    'unexplained_endpoint_gaps': [], 'intentional_free_tips': [p for p in endpoints if p['intentional_free_tip']],
    'maximum_other_endpoint_gap_mm': max(p['nearest_gap_mm'] for p in endpoints if not p['intentional_free_tip']),
    'emitted_joint_checks': joint_checks, 'connected_steering_chain_runs': chain_groups,
    'steering_handwheel_checks':handwheel_checks,'staggered_spoke_occlusion_checks':spoke_occlusion_checks,
    'steering_chain_route_checks':route_checks,'quadrant_release_hinge_gap_mm':trigger_gap,'quadrant_release_length_ratio_to_v7':release_ratio,
    'quadrant_lower_blade_checks':lower_blade_checks,
    'oval_fitting_checks':fitting_checks,'printed_footer_words':sorted(footer_words),
    'roller_rim_pen_checks':rim_pen_checks,'firebox_shoulder_checks':shoulder_checks,
    'circular_feature_checks': circle_checks, 'tangent_join_check_count': len(tangent_errors),
    'maximum_spoke_join_tangent_error_degrees': max(tangent_errors), 'closed_contour_seam_regression_passed': True,
    'maximum_exact_spoke_tangent_error_degrees':max(exact_tangent_errors),
    'maximum_spoke_coordinate_rounding_error_mm':max(coordinate_errors),
    'exported_tangents_within_calculated_grid_error_bounds':True,
    'source_hashes': source_checks, 'format_validation': check.stdout.strip(),
    'badge_clearance_from_engine_mm':clearance, 'worksplate_text':sorted(plate_words),
    'far_chain_minimum_front_roll_clearance_mm':far_min, 'forward_scraper_blade_gap_mm':scraper_gap,
    'chimney_upper_width_mm':upper_width,'chimney_lower_width_mm':lower_width,
    'strict_svg_preflight': job['preflight'], 'simulation': job['stats'],
    'physical_execution_allowed': job['safety']['execution_allowed'],
    'scope': 'Exported geometric alignment, smoothness and visible joins are checked. Separate closed details need not touch. Proportions remain interpreted, not surveyed or factory dimensions.'}
(evidence / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({k: report[k] for k in ['engine_paths', 'all_svg_paths', 'engine_centre_errors_mm',
    'endpoint_check_count', 'unexplained_endpoint_gaps', 'maximum_other_endpoint_gap_mm',
    'tangent_join_check_count', 'maximum_spoke_join_tangent_error_degrees', 'format_validation']}, indent=2))

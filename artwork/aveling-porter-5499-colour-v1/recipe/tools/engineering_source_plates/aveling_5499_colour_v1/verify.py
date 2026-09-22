#!/usr/bin/env python3
"""Verify the colour edition against the frozen, already-audited revision 14."""
from collections import Counter,defaultdict
import hashlib,json,math,subprocess,sys,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from city_map_plotter.pens import ACTUAL_PEN_INVENTORY
from city_map_plotter.technical_assets import parse_absolute_path_data
from city_map_plotter.stroke_font import STROKE_FONT_ID
from city_map_plotter.vector_path import LineSegment
from shapely.geometry import LineString
from tools.engineering_source_plates.build_aveling_5499_colour import BASE_SHA,ID
from tools.engineering_source_plates.aveling_5499_colour_v1.palette import GOLD_BAND_INDICES,assign
NS='{http://www.w3.org/2000/svg}'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()


def verify(package):
    evidence=package/'evidence';base=evidence/'revision-14-master.svg';svg=package/'artwork'/f'{ID}.svg'
    assert sha(base)==BASE_SHA
    previous=list(ET.parse(base).iter(NS+'path'))
    tree=ET.parse(svg);paths=list(tree.iter(NS+'path'))
    parents={child:parent for parent in tree.iter() for child in parent}
    previous_tree=ET.parse(base);old_parents={child:parent for parent in previous_tree.iter() for child in parent}
    old_paths=list(previous_tree.iter(NS+'path'))
    def inherited(el,key,parents=parents):
        while el is not None:
            if el.get(key) is not None:return el.get(key)
            el=parents.get(el)
    inventory={p.identity:p for p in ACTUAL_PEN_INVENTORY.pens}
    assert len(paths)==len(previous)==863
    assert not any(e.tag.split('}')[-1] in {'text','image','use'} for e in tree.iter())
    assert not any(e.get('transform') for e in tree.iter())
    mapped={int(p.get('data-base-path-index')):p for p in paths}
    assert set(mapped)==set(range(863))
    nib_counts=Counter();roles=Counter();minimum={};adapted=[];unchanged=0;copy_groups=defaultdict(list)
    for i,original in enumerate(old_paths):
        final=mapped[i];v=parse_absolute_path_data(final.get('d'));before=parse_absolute_path_data(original.get('d'))
        pen_id=inherited(final,'data-plot-pen-id');pen=inventory[pen_id];nib=pen.mark_width_mm
        expected_pen,paint_role=assign(original.attrib,float(inherited(original,'stroke-width',old_parents)))
        assert pen_id==expected_pen and final.get('data-colour-role')==paint_role
        assert inherited(final,'data-plot-pen-profile')=='actual-pens'
        assert inherited(final,'stroke')==pen.preview_color
        assert inherited(final,'data-plot-ink')==pen.ink
        assert inherited(final,'fill')=='none'
        assert inherited(final,'stroke-linecap')==inherited(final,'stroke-linejoin')=='round'
        for key in ['stroke-width','data-plot-nib-mm','data-plot-nominal-nib-mm','data-plot-width-mm','data-plot-requested-width-mm']:
            assert float(inherited(final,key))==nib
        assert inherited(final,'data-plot-passes')==inherited(final,'data-plot-strokes')=='1'
        assert float(inherited(final,'data-plot-width-fit-error-mm'))==0
        length=v.length(.0001)
        assert length+.003>=3*nib,(i,length,nib)
        minimum[pen_id]=min(minimum.get(pen_id,math.inf),length)
        nib_counts[pen_id]+=1;roles[paint_role]+=1
        for key in ['data-role','data-feature','data-component','data-copy','data-cap-height-mm','data-tracking-mm','data-alignment','data-source-ref','data-model-path-index']:
            assert final.get(key)==original.get(key),(i,key)
        if final.get('data-colour-end-inset-mm'):
            assert int(final.get('data-model-path-index')) in GOLD_BAND_INDICES
            assert pen_id=='gold-1' and abs(float(final.get('data-colour-end-inset-mm'))-.8)<1e-12
            assert len(v.segments)==len(before.segments)==1
            assert isinstance(v.segments[0],LineSegment) and isinstance(before.segments[0],LineSegment)
            assert v.start[0]==v.end[0]==before.start[0]==before.end[0]
            start_delta=math.dist(v.start,before.start);end_delta=math.dist(v.end,before.end)
            assert abs(start_delta-.8)<1e-9 and abs(end_delta-.8)<1e-9
            assert min(before.start[1],before.end[1])<min(v.start[1],v.end[1])<max(v.start[1],v.end[1])<max(before.start[1],before.end[1])
            adapted.append({'base_path_index':i,'model_path_index':int(final.get('data-model-path-index')),
                'start_inset_mm':start_delta,'end_inset_mm':end_delta,'remaining_length_mm':length,
                'gold_cap_to_original_boundary_ink_clearance_mm':start_delta-.5-.2})
        else:
            assert final.get('d')==original.get('d'),(i,'unrequested geometry change')
            unchanged+=1
        if final.get('data-copy'):
            cap=float(final.get('data-cap-height-mm'));assert cap+1e-9>=8*nib
            assert all(isinstance(s,LineSegment) for s in v.segments)
            copy_groups[(final.get('data-role'),final.get('data-copy'))].append(final)
    assert unchanged==860 and len(adapted)==3
    assert {r['model_path_index'] for r in adapted}==GOLD_BAND_INDICES
    assert set(inventory[p].ink for p in nib_counts)=={'Black','Green','Red','Gold'}
    assert len(nib_counts)==8
    assert sum(p.get('data-role')=='side-elevation-component' for p in paths)==622
    assert sum(p.get('data-role')=='invicta-emblem' for p in paths)==40
    assert not any(p.get('data-role') in {'ground-datum','axle-centre-mark'} for p in paths)
    assert not any(inherited(p,'data-plot-ink')=='White' for p in paths)
    # Revision 14's complete font audit is inherited through byte-identical
    # glyph paths, with the new nib/cap checks above performed independently.
    prior=json.loads((evidence/'revision-14-plotting-verification.json').read_text())
    assert prior['master_svg_sha256']==BASE_SHA and prior['complete_text_block_count']==12
    assert prior['font_id']==STROKE_FONT_ID
    assert prior['font_source_sha256']==sha(ROOT/'src/city_map_plotter/stroke_font.py')
    assert len(copy_groups)==12
    assert {(r['role'],r['copy']):r['stroke_count'] for r in prior['lettering_checks']}=={k:len(v) for k,v in copy_groups.items()}
    # Gold contours retain clear white paper to the adjacent fine black rim.
    def selected(component,feature=None,role=None):
        return [LineString(parse_absolute_path_data(p.get('d')).flatten(.0001).points) for p in paths
            if (component is None or p.get('data-component')==component) and
               (feature is None or p.get('data-feature')==feature) and
               (role is None or p.get('data-role')==role)]
    plate_outer=selected(None,role='worksplate-outline')[0];plate_inner=selected(None,role='worksplate-inner-rim')[0]
    cylinder_outer=selected('cylinder-cover','maker-plate-outer')[0];cylinder_inner=selected('cylinder-cover','maker-plate-inner')[0]
    gold_gaps={'corner_worksplate_rims_mm':plate_outer.distance(plate_inner)-.5-.125,
        'cylinder_worksplate_rims_mm':cylinder_outer.distance(cylinder_inner)-.5-.125}
    assert min(gold_gaps.values())>.1
    layer_paths=Counter()
    pen_files=sorted((package/'artwork').glob('*.pen-*.svg'));assert len(pen_files)==8
    for file in pen_files:layer_paths.update(p.get('d') for p in ET.parse(file).iter(NS+'path'))
    assert layer_paths==Counter(p.get('d') for p in paths)
    manifest=json.loads((package/'artwork'/f'{ID}.plot.json').read_text())
    baseline=json.loads((evidence/'revision-14-master.plot.json').read_text())
    assert manifest['page']==baseline['page']
    assert manifest['rendering']['stock_tone']=='light' and manifest['rendering']['paper_preview_color']=='#ffffff'
    assert manifest['rendering']['pen_profile']=='actual-pens'
    assert manifest['rendering']['pen_inventory']==ACTUAL_PEN_INVENTORY.as_dict()
    sequence=[r['pen_id'] for r in manifest['pen_sequence']]
    assert len(sequence)==len(set(sequence))==8
    job=json.loads((package/'plot'/f'{ID}.plotjob.json').read_text())
    assert job['source']['sha256']==sha(svg) and not job['preflight']['issues']
    assert job['order']=='optimised'
    source_checks=[]
    for s in json.loads((evidence/'sources.json').read_text())['sources']:
        for fk,hk in [('file','sha256'),('working_file','working_sha256'),('video_file','video_sha256')]:
            if s.get(fk):
                digest=sha(evidence/s[fk]);assert digest==s[hk]
                source_checks.append({'file':s[fk],'sha256':digest})
    validation=subprocess.run([sys.executable,str(ROOT/'tools/validate_format.py'),'--warnings-as-errors',str(svg)],check=True,capture_output=True,text=True)
    report={'schema':'aveling-5499-colour-verification-v1','master_svg_sha256':sha(svg),'base_master_sha256':BASE_SHA,
        'all_svg_paths':863,'engine_paths':622,'unchanged_source_paths':unchanged,'gold_band_endpoint_insets':adapted,
        'all_engine_contours_unchanged':True,'horse_paths_unchanged':40,'text_blocks_unchanged':12,
        'inherited_geometry_proof':'revision-14-verification.json','inherited_font_proof':'revision-14-plotting-verification.json',
        'font_id':STROKE_FONT_ID,'font_source_sha256':prior['font_source_sha256'],
        'complete_lettering_preserved':True,'minimum_cap_to_new_nib_ratio':min(float(v[0].get('data-cap-height-mm'))/inventory[inherited(v[0],'data-plot-pen-id')].mark_width_mm for v in copy_groups.values()),
        'pen_layers':[{'pen_id':p,'ink':inventory[p].ink,'nib_mm':inventory[p].mark_width_mm,'paths':nib_counts[p],'minimum_length_mm':minimum[p]} for p in sequence],
        'pen_profile':'actual-pens','paint_roles':dict(roles),'single_pass_paths':True,
        'gold_rim_clearances':gold_gaps,'white_paper':True,'all_pen_files_match_master':True,
        'format_validation':validation.stdout.strip(),'source_hashes':source_checks,
        'simulation':job['stats'],'strict_svg_preflight':job['preflight'],
        'scope':'Photograph-informed colour illustration using available studio inks, not measured paint matching. Physical geometry/pen-file checks; no machine operated.'}
    (evidence/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ['master_svg_sha256','all_svg_paths','unchanged_source_paths','text_blocks_unchanged','gold_rim_clearances','format_validation']},indent=2))
    return report

if __name__=='__main__':verify(Path(sys.argv[1]))

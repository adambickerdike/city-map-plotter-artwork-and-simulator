#!/usr/bin/env python3
"""Create a physical colour-pen edition from the final revision-14 source SVG."""
import argparse,hashlib,json,sys,xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import replace
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from city_map_plotter.niche_common import ArtworkLayer,PlateArtwork,PlateContext,Rect,write_plate
from city_map_plotter.pens import ACTUAL_PEN_INVENTORY
from city_map_plotter.technical_assets import parse_absolute_path_data
from tools.engineering_source_plates.aveling_5499_colour_v1.palette import PEN_ORDER,assign,adapt_path

ID='aveling-porter-5499-colour'
DEFAULT_OUTPUT=ROOT/'examples/technical-objects/aveling-porter-5499-colour-v1'
BASE_SHA='f4106404c710e89bda8e2b32e4cfd92d9ff1c82b4f30c9ec7cbba89621e33678'
NS='{http://www.w3.org/2000/svg}'

def build(output,dpi):
    evidence=output/'evidence';base=evidence/'revision-14-master.svg'
    assert hashlib.sha256(base.read_bytes()).hexdigest()==BASE_SHA
    source_manifest=json.loads((evidence/'revision-14-master.plot.json').read_text())
    facts=json.loads((evidence/'revision-14-sources.json').read_text())
    for source in facts['sources']:
        for fk,hk in [('file','sha256'),('working_file','working_sha256'),('video_file','video_sha256')]:
            if source.get(fk):assert hashlib.sha256((evidence/source[fk]).read_bytes()).hexdigest()==source[hk]
    tree=ET.parse(base);parents={child:parent for parent in tree.iter() for child in parent}
    def inherited(el,key):
        while el is not None:
            if el.get(key) is not None:return el.get(key)
            el=parents.get(el)
    context=PlateContext.load('a3-landscape')
    custom={k:Rect(**v) for k,v in source_manifest['page']['zones_mm'].items() if k.startswith('heritage_')}
    context=replace(context,zones={**context.zones,**custom},field=custom['heritage_drawing'])
    layers={pen:ArtworkLayer('colour-'+pen,pen.replace('-',' ').title(),pen) for pen in PEN_ORDER}
    plan=[]
    for i,el in enumerate(tree.iter(NS+'path')):
        previous_width=float(inherited(el,'stroke-width'))
        pen_id,paint_role=assign(el.attrib,previous_width)
        source_path=parse_absolute_path_data(el.get('d'));path,inset=adapt_path(source_path,paint_role)
        attributes={k:v for k,v in el.attrib.items() if k not in {'d','data-logical-layer','data-role','data-source-ref','data-sequence'}}
        attributes.update({'data-colour-role':paint_role,'data-base-path-index':str(i)})
        if inset:attributes['data-colour-end-inset-mm']=str(inset)
        layers[pen_id].add_path(path,role=el.get('data-role'),source_ref=el.get('data-source-ref'),sequence=i,attributes=attributes)
        assert path.length(.001)>=3*layers[pen_id].pen.mark_width_mm
        plan.append({'base_path_index':i,'component':el.get('data-component'),'feature':el.get('data-feature'),
            'model_path_index':el.get('data-model-path-index'),'role':el.get('data-role'),'paint_role':paint_role,
            'pen_id':pen_id,'nib_mm':layers[pen_id].pen.mark_width_mm,'source_path':el.get('d'),
            'endpoint_inset_mm':inset})
    assert len(plan)==863
    sources=facts['sources']+[{'id':'user-colour-edition-v1','publisher':'User','date':'2026-09-22',
        'supports':['Colour version of the latest refined Aveling & Porter drawing, without blueprint styling']}]
    notes=[n.replace('White 0.30/0.40/0.50 mm pens on blue stock.', 'The source blueprint used White 0.30/0.40/0.50 mm pens on blue stock.') for n in facts['notes']]
    notes += ['Colour edition 1 uses the final revision-14 geometry on white paper. The actual-engine photographs guide green cladding and wheel spokes, dark metalwork, red-brown frames/scrapers and brass trim. Studio Green, Red, Black and Gold inks are an illustrative palette, not measured paint matches.',
        'Fine engine, emblem and lettering paths use actual 0.25/0.40 mm coloured or black pens; outer roller contours and outer frame use Black 0.60 mm. Brass accents use the actual Gold 1.00 mm pen. The detailed horse remains fine black engraving because a 1 mm gold pen would close its smallest details.',
        'All 863 source paths are retained. Three straight boiler-band centre-lines have their endpoints inset by 0.800 mm to keep the broader gold nib within the original strap edges. All other 860 paths, including the engine contours, horse and lettering, remain identical. The short upper band fragment stays fine black.',
        'The accepted A3 composition, footer wording, plaque to the left of the horse, lowered engine, connected quadrant lever, wheel/chain occlusions, and lack of ground/perimeter guides are preserved.']
    meta=dict(json.loads((evidence/'revision-14-reconstruction.json').read_text())['metadata'])
    meta.update(paper_preview_color='#ffffff',physical_inks=['Black','Green','Red','Gold'],
        colour_edition='photograph-informed-studio-inks-v1',base_master_sha256=BASE_SHA,
        colour_plan='evidence/colour-plan.json',geometry_record='evidence/revision-14-reconstruction.json',
        current_geometry_scope='Frozen revision-14 contours, with three gold strap-centre endpoint insets only.',
        base_geometry_path_count=863,unchanged_path_count=860,adapted_gold_band_path_count=3)
    meta.pop('physical_ink',None)
    art=PlateArtwork(subject_id=ID,domain='heritage-engine-portrait',subject_kind='steam-road-roller',
        title='AVELING & PORTER',subtitle='R6 STEAM ROAD ROLLER',details=('WORKS No. 5499 / 1904','REG. BS 8711'),
        credit_line=source_manifest['source']['attribution'],scale_status='interpreted-side-elevation-not-to-scale',
        evidence_status='source-referenced-analytic-drafting',rights_status=source_manifest['rights']['status'],
        rights_metadata={'logos_or_trade_dress_used':True},sources=tuple(sources),context=context,layers=list(layers.values()),
        pen_order=PEN_ORDER,artifact_kind='interpreted-engine-elevation',rendering_preset='heritage-engine-colour-v1',
        format_subject_policy='user-authorised-single-elevation-bottom-strip',rendering_metadata=meta,
        svg_metadata={'side_elevation':meta},notes=tuple(notes),include_standard_furniture=False,
        preview_background='#ffffff',stock_tone='light',visible_attribution=False,pen_inventory=ACTUAL_PEN_INVENTORY,
        data_snapshot='2026-09-22')
    result=write_plate(art,output/'artwork',png_dpi=dpi,generated_at='2026-09-22T00:00:00+00:00')
    (evidence/'sources.json').write_text(json.dumps({**facts,'sources':sources,'notes':notes},indent=2)+'\n')
    (evidence/'colour-plan.json').write_text(json.dumps({'base_master_sha256':BASE_SHA,'paper':'white','inventory':'actual-pens','colour_scope':'Observed material and paint groups translated to the available studio inks; not measured colour matching.','assignments':plan,'pen_counts':dict(Counter(p['pen_id'] for p in plan))},indent=2)+'\n')
    print(json.dumps({'master':result['svg']['path'],'path_count':len(plan),'pen_counts':dict(Counter(p['pen_id'] for p in plan)),'unchanged_source_paths':860,'gold_band_insets':3},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,default=DEFAULT_OUTPUT);p.add_argument('--png-dpi',type=float,default=254)
    args=p.parse_args();build(args.output_dir,args.png_dpi)

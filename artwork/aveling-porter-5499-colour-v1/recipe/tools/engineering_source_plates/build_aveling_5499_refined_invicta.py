#!/usr/bin/env python3
"""Build the detailed, centred single-elevation A3 plate of No. 5499."""
import argparse, hashlib, json, sys
from dataclasses import replace
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from city_map_plotter.niche_common import ArtworkLayer, PlateArtwork, PlateContext, Rect, rectangle_stroke, write_plate
from city_map_plotter.pens import WHITE_BLUEPRINT_PEN_INVENTORY
from city_map_plotter.vector_path import Affine2D, VectorPath, LineSegment, CubicSegment
from tools.engineering_source_plates.aveling_5499_v14.lettering import text
from tools.engineering_source_plates.aveling_5499_v14.engine import model
from tools.engineering_source_plates.aveling_5499_v14.badge import add_badge

DATA=Path(__file__).with_name('aveling_5499_v14')
DEFAULT_OUTPUT=ROOT/'examples/technical-objects/aveling-porter-5499-blueprint-v14'
ID='aveling-porter-5499-blueprint'
BLUE='#113b58'
HORSE_ENLARGEMENT=1.65
WORKSPLATE_ENLARGEMENT=1.32

def physical_path(path):
    # Quantise on the shared SVG emitter's grid before length/nib checks.
    def point(p):return tuple(round(v,3) for v in p)
    start=point(path.start);at=start;segments=[]
    for seg in path.segments:
        end=point(seg.to)
        if isinstance(seg,LineSegment):
            if end!=at:segments.append(LineSegment(end))
        else:
            a,b=point(seg.control_1),point(seg.control_2)
            if not at==a==b==end:segments.append(CubicSegment(a,b,end))
        at=end
    return VectorPath(start,tuple(segments)) if segments else None

def page_context():
    """User-authorised single-object composition; stock format data is untouched.

    The normal rail zones remain defined but unused. Only the additional named
    heritage zones are used by this plate, derived from stock inset/type/gap.
    """
    context=PlateContext.load('a3-landscape');p=context.plate
    inset=p['content_inset_mm'];gap=p['gap_mm'];detail=p['type_scale_mm']['detail']
    content=context.page.inset(inset)
    footer_height=3*gap+detail
    footer=Rect(content.x,content.bottom-footer_height,content.width,footer_height)
    drawing=Rect(content.x,content.y,content.width,footer.y-content.y-gap)
    badge=Rect(drawing.right-22*gap,drawing.y,22*gap,10*gap)
    zones={**context.zones,'heritage_drawing':drawing,'heritage_footer':footer,'heritage_badge':badge}
    return replace(context,field=drawing,zones=zones)

def build(output,dpi):
    evidence=output/'evidence';facts=json.loads((DATA/'sources.json').read_text())
    for src in facts['sources']:
        for fk,hk in [('file','sha256'),('working_file','working_sha256'),('video_file','video_sha256')]:
            if src.get(fk):assert hashlib.sha256((evidence/src[fk]).read_bytes()).hexdigest()==src[hk],src[fk]
    records,geometry=model();context=page_context();p=context.plate
    gap=p['gap_mm'];field=context.zones['heritage_drawing'];footer=context.zones['heritage_footer']
    fine=ArtworkLayer('mechanical-detail','Mechanical detail','white-0-3')
    outlines=ArtworkLayer('engine-outlines','Continuous side-elevation contours','white-0-4')
    roller_outlines=ArtworkLayer('roller-rims','Road-roller outer rims','white-0-5')
    smallcopy=ArtworkLayer('small-identification','Compact lower-edge identification','white-0-3')
    copy=ArtworkLayer('engine-identity','Engine identity in bottom title block','white-0-4')
    frame=ArtworkLayer('sheet-frame','Double sheet frame','white-0-5')
    frame.add(rectangle_stroke(context.safe),role='outer-border')
    fine.add(rectangle_stroke(context.safe.inset(p['border']['inner_offset_mm'])),role='inner-border')
    # One drawing, fitted uniformly across the full content width. No rail or callouts.
    bs=[r['path'].bounds() for r in records]
    box=(min(b.min_x for b in bs),min(b.min_y for b in bs),max(b.max_x for b in bs),max(b.max_y for b in bs))
    width,height=box[2]-box[0],box[3]-box[1]
    scale=min((field.width-outlines.pen.mark_width_mm)/width,(field.height-2*gap)/height)
    m=Affine2D(a=scale,d=scale,e=field.centre[0]-(box[0]+box[2])/2*scale,f=field.centre[1]-(box[1]+box[3])/2*scale+gap)
    baseline=geometry['ground']*scale+m.f
    retained=[];omitted=[]
    for i,record in enumerate(records):
        paper=physical_path(record['path'].transformed(m))
        layer={'outline':outlines,'roller':roller_outlines,'fine':fine}[record['weight']]
        info={k:v for k,v in record.items() if k!='path'}
        info.update(index=i,construction_path=record['path'].to_svg_path_data(),paper_path=paper.to_svg_path_data() if paper else '',length_mm=paper.length(.005) if paper else 0)
        if info['length_mm']<3*layer.pen.mark_width_mm+.01:
            omitted.append(info);continue
        layer.add_path(paper,source_ref=record['source'],role='side-elevation-component',sequence=len(retained),attributes={
          'data-view':'side','data-component':record['component'],'data-feature':record['feature'],
          'data-construction':'source-referenced-analytic-drafting','data-model-path-index':str(i)})
        retained.append(info)
    # User-requested identity details occupy the existing clear top-right
    # space. The small on-engine plate keeps its contour but no lettering.
    badge_meta=add_badge(smallcopy,outlines,context.zones['heritage_badge'],horse_enlargement=HORSE_ENLARGEMENT,plate_enlargement=WORKSPLATE_ENLARGEMENT,gap=gap)
    # Wheel-perimeter centre guides removed at the user's request.
    # Small information strip along the lower edge, as explicitly requested.
    fine.add([(footer.x,footer.y),(footer.right,footer.y)],role='footer-rule')
    divisions=[footer.x,footer.x+30*gap,footer.x+50*gap,footer.right]
    for x in divisions[1:-1]:fine.add([(x,footer.y),(x,footer.bottom)],role='footer-divider')
    x0,x1,x2,x3=divisions
    first_baseline=footer.y+gap+p['type_scale_mm']['detail'];second_baseline=footer.bottom-gap/2
    cells=[(x0,x1,'AVELING & PORTER','R6 STEAM ROAD ROLLER',p['type_scale_mm']['detail'],3.0,'maker','type','beamish-2021','beamish-2021'),
      (x1,x2,'WORKS No. 5499','REG. BS 8711',3.2,3.0,'works-number','registration','beamish-2021','international-steam-2024'),
      (x2,x3,'1904',None,p['type_scale_mm']['detail'],3.0,'year',None,'beamish-2021',None)]
    footer_records=[]
    for a,b,top,bottom,topcap,bottomcap,toprole,bottomrole,topsource,bottomsource in cells:
        top_baseline=first_baseline if bottom else footer.centre[1]+topcap/2
        text(copy,top,a+gap,top_baseline-topcap,b-a-2*gap,topcap,'footer-'+toprole,topsource,centred=True)
        values,baselines=[top],[top_baseline]
        if bottom:
            text(smallcopy,bottom,a+gap,second_baseline-bottomcap,b-a-2*gap,bottomcap,'footer-'+bottomrole,bottomsource,centred=True)
            values.append(bottom);baselines.append(second_baseline)
        footer_records.append({'x':a,'width':b-a,'centre_x':(a+b)/2,'values':values,'baselines':baselines})
    meta={
      'method':'source-referenced single-elevation drafting','poster_layout':'heritage-single-elevation-footer-v1',
      'layout_override':{'authority':'explicit user request','request':'single large side drawing, compact bottom strip, enlarged worksplate left of enlarged Invicta horse in clear upper-right space',
                         'unused_standard_zones':['title','subtitle','detail','furniture','attribution','map_field'],
                         'drawing_zone':'heritage_drawing','copy_zones':['heritage_footer','heritage_badge'],'stock_format_file_changed':False},
      'scale_mm_per_reference_unit':scale,'view_transform':{k:getattr(m,k) for k in ['a','b','c','d','e','f']},
      'ground_y_mm':baseline,'engine_bounds_mm':[box[0]*scale+m.e,box[1]*scale+m.f,box[2]*scale+m.e,box[3]*scale+m.f],
      'drawing_horizontally_centred_in_field':True,'engine_offset_mm':[0,gap],'footer_cells':footer_records,
      'removed_footer_copy':['7 TONS','DAVID BICKERDIKE','SIDE ELEVATION / NTS'],
      'ground_line':False,'wheel_perimeter_centre_guides':False,'view_count':1,'front_view':False,'canopy':False,'horse':True,'horse_on_engine':False,'callout_leaders':False,
      'badge':badge_meta,'engine_plate_lettering':False,'worksplate_enlargement':True,
      'merl_reference_role':'Fowler S1021 composition and drafting language only; not a 5499 factory drawing',
      'engine_source_edge_maps_used':False,'emblem_source_contour_trace':True,'inferred_proportions':True,'surveyed_dimensions':False,
      'geometry_record':'evidence/reconstruction.json','retained_component_paths':len(retained),'omitted_sub_nib_fragments':len(omitted),
      'engine_coordinate_serialization_grid_mm':.001,
      'paper_preview_color':BLUE,'preview_background_is_not_plot_geometry':True,'physical_ink':'White'}
    reconstruction_source={'id':'reconstruction-constraints','publisher':'Reference-based interpretive drawing','supports':['Single side elevation','Smooth curves and joined mechanical components'],
      'qualification':'Inferred proportions. Not a factory drawing or dimensional survey.'}
    art=PlateArtwork(subject_id=ID,domain='heritage-engine-portrait',subject_kind='steam-road-roller',title='AVELING & PORTER',subtitle='R6 STEAM ROAD ROLLER',
      details=('WORKS No. 5499 / 1904','REG. BS 8711'),
      credit_line='Engine photographs: Benjamin Matthews; Terry Pinnegar. Drafting: The MERL / Fowler S1021. Invicta and worksplate: GW Railwayana Auctions.',
      scale_status='interpreted-side-elevation-not-to-scale',evidence_status='source-referenced-analytic-drafting',rights_status='private-review-only',
      sources=tuple(facts['sources']+[reconstruction_source]),context=context,layers=[fine,smallcopy,outlines,roller_outlines,copy,frame],
      pen_order=('white-0-3','white-0-4','white-0-5'),artifact_kind='interpreted-engine-elevation',
      rendering_preset='heritage-engine-white-blueprint-v14',format_subject_policy='user-authorised-single-elevation-bottom-strip',
      rendering_metadata=meta,svg_metadata={'side_elevation':meta},notes=tuple(facts['notes']),include_standard_furniture=False,
      preview_background=BLUE,stock_tone='dark',visible_attribution=False,pen_inventory=WHITE_BLUEPRINT_PEN_INVENTORY)
    result=write_plate(art,output/'artwork',png_dpi=dpi,generated_at='2026-09-21T00:00:00+00:00')
    (evidence/'sources.json').write_text(json.dumps(facts,indent=2)+'\n')
    (evidence/'reconstruction.json').write_text(json.dumps({'geometry':geometry,'metadata':meta,'retained':retained,'omitted':omitted},indent=2)+'\n')
    print(json.dumps({'master':result['svg']['path'],'component_paths':len(retained),'sub_nib_omissions':len(omitted),'engine_bounds_mm':meta['engine_bounds_mm'],'scale':scale},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,default=DEFAULT_OUTPUT);p.add_argument('--png-dpi',type=float,default=254)
    args=p.parse_args();build(args.output_dir,args.png_dpi)

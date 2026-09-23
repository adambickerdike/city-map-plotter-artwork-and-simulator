"""Source-based Invicta casting and legible, separately enlarged worksplate."""
import json
from bisect import bisect_left
from math import cos, sin, hypot, pi
from pathlib import Path
from city_map_plotter.vector_path import Affine2D
from city_map_plotter.technical_assets import parse_absolute_path_data
from city_map_plotter.stroke_font import stroke_text
from city_map_plotter.niche_common import ArtworkLayer, Rect, reliable_vector_strokes
from .drafting import circle, ellipse
from .emblem_reliefs import relief_paths
from .lettering import text

HERE=Path(__file__).resolve().parent

def curved_copy(layer,value,cx,cy,rx,ry,cap,tracking=None):
    """Individually rotated stroke glyphs on an elliptical baseline."""
    glyphs=[];cursor=0
    tracking=layer.pen.mark_width_mm*1.5 if tracking is None else tracking
    for ch in value:
        if ch==' ':cursor+=cap*.7;continue
        strokes=stroke_text(ch,x_mm=0,y_mm=-cap,height_mm=cap)
        xs=[x for s in strokes for x,y in s];left,right=min(xs),max(xs)
        width=right-left
        glyphs.append((cursor+width/2,[[(x-left-width/2,y) for x,y in s] for s in strokes]))
        cursor+=width+tracking
    total=cursor-tracking
    ts=[(210+i*120/1200)*pi/180 for i in range(1201)]
    pts=[(cx+rx*cos(t),cy+ry*sin(t)) for t in ts];ds=[0]
    for a,b in zip(pts,pts[1:]):ds.append(ds[-1]+hypot(a[0]-b[0],a[1]-b[1]))
    assert total<ds[-1],(value,total,ds[-1])
    out=[]
    for x,strokes in glyphs:
        distance=(ds[-1]-total)/2+x;j=max(1,bisect_left(ds,distance))
        f=(distance-ds[j-1])/(ds[j]-ds[j-1]);t=ts[j-1]*(1-f)+ts[j]*f
        px,py=cx+rx*cos(t),cy+ry*sin(t);ux,uy=-rx*sin(t),ry*cos(t);norm=hypot(ux,uy);ux/=norm;uy/=norm
        out += [[(px+u*ux-v*uy,py+u*uy+v*ux) for u,v in s] for s in strokes]
    layer.add_many(reliable_vector_strokes(out,nib_mm=layer.pen.mark_width_mm),role='worksplate-copy',source_ref='gwra-worksplate-6882',
       attributes={'data-copy':value,'data-cap-height-mm':str(cap),'data-tracking-mm':str(tracking),'data-alignment':'elliptical-arc'})

def _add_badge(detail,outline,zone):
    data=json.loads((HERE/'invicta-emblem.json').read_text());b=data['bounds_px']
    cx=zone.centre[0];horse_height=zone.width*.46;horse_top=zone.y+.3
    factor=horse_height/(b[3]-b[1]);m=Affine2D(a=factor,d=factor,e=cx-(b[0]+b[2])/2*factor,f=horse_top-b[1]*factor)
    for i,d in enumerate(data['paths']):
        outline.add_path(parse_absolute_path_data(d).transformed(m),role='invicta-emblem',source_ref=data['source_id'],
          attributes={'data-feature':'source-casting-contour','data-source-path':str(i)})
    rotation=Affine2D(**data['orientation_transform'])
    for i,(name,path) in enumerate(relief_paths()):
        detail.add_path(path.transformed(rotation).transformed(m),role='invicta-emblem',source_ref=data['source_id'],
          attributes={'data-feature':name,'data-source-path':str(i),'data-construction':'photo-referenced-cubic-relief'})
    # Oval aspect ratio from the auction's 9.75 x 7.25 inch plate. This is an
    # enlarged identity vignette, not a claim about 5499's plate dimensions.
    rx=zone.width/2-.4;ry=rx*7.25/9.75;cy=zone.y+54.4
    outline.add_path(ellipse(cx,cy,rx,ry),role='worksplate-outline',source_ref='gwra-worksplate-6882')
    detail.add_path(ellipse(cx,cy,rx-1.15,ry-1.15),role='worksplate-inner-rim',source_ref='gwra-worksplate-6882')
    for side in [-1,1]:
        detail.add_path(circle(cx+side*(rx-4.1),cy+2,.60),role='worksplate-fixing',source_ref='gwra-worksplate-6882')
    curved_copy(detail,'BY ROYAL LETTERS PATENT',cx,cy,rx-3.2,ry-5.2,2.4)
    rows=[('No. 5499',cy-11.0,3.2),('AVELING & PORTER',cy-4.3,3.2),
          ('LIMITED',cy+1.7,2.4),('ROCHESTER',cy+7.2,2.4),('KENT',cy+12.1,2.4),('ENGLAND',cy+16.2,2.4)]
    for value,y,cap in rows:
        text(detail,value,cx-rx+3,y,2*rx-6,cap,'worksplate-copy','user-worksplate-correction' if value=='No. 5499' else 'gwra-worksplate-6882',centred=True)
    return {'zone':{'x':zone.x,'y':zone.y,'width':zone.width,'height':zone.height},
      'centre_x_mm':cx,'horse_height_mm':horse_height,'horse_source_sha256':data['source_sha256'],
      'horse_source_id':data['source_id'],'horse_orientation_clockwise_degrees':data['orientation_clockwise_degrees'],
      'horse_orientation_scope':data['orientation_scope'],'horse_relief_count':len(relief_paths()),
      'horse_source_to_base_transform':{k:getattr(m,k) for k in ['a','b','c','d','e','f']},
      'worksplate_ellipse_mm':{'cx':cx,'cy':cy,'rx':rx,'ry':ry},
      'worksplate_copy':['BY ROYAL LETTERS PATENT']+[r[0] for r in rows],
      'plate_adaptation':'GWRA No.6882 layout/oval reference, adapted to user-confirmed No.5499. Lettering redrawn for pen legibility.'}

def add_badge(detail,outline,zone,*,horse_enlargement=1.65,plate_enlargement=1.32,gap=6):
    """Place separately enlarged source vignettes side by side, at fixed nibs."""
    base=Rect(0,0,10*gap,13*gap)
    fine=ArtworkLayer('badge-fine','Badge fine',detail.pen_id)
    edges=ArtworkLayer('badge-edges','Badge edges',outline.pen_id)
    meta=_add_badge(fine,edges,base)
    b=json.loads((HERE/'invicta-emblem.json').read_text())['bounds_px']
    horse_height=meta['horse_height_mm']*horse_enlargement
    horse_width=horse_height*(b[2]-b[0])/(b[3]-b[1])
    edge_inset=outline.pen.mark_width_mm/2
    # Retain the accepted revision-13 horse slot and plaque anchor exactly.
    # The new casting is uniformly fitted within that slot, without stretching.
    accepted_aspect=843.6794730748218/848.7673090914119
    horse_slot_width=horse_height*accepted_aspect
    assert horse_width<=horse_slot_width
    horse_cx=zone.right-edge_inset-horse_slot_width/2
    cy=zone.centre[1]
    plate=meta['worksplate_ellipse_mm']
    plate_rx=plate['rx']*plate_enlargement
    plate_cx=horse_cx-horse_slot_width/2-gap-plate_rx
    assert plate_cx-plate_rx-edge_inset>=zone.x
    assert 2*plate['ry']*plate_enlargement+2*edge_inset<=zone.height
    assert horse_height+2*edge_inset<=zone.height
    original_horse_cy=.3+meta['horse_height_mm']/2
    transforms={
        'horse':Affine2D(a=horse_enlargement,d=horse_enlargement,
            e=horse_cx-base.centre[0]*horse_enlargement,
            f=cy-original_horse_cy*horse_enlargement),
        'worksplate':Affine2D(a=plate_enlargement,d=plate_enlargement,
            e=plate_cx-plate['cx']*plate_enlargement,
            f=cy-plate['cy']*plate_enlargement)}
    for source,target in [(fine,detail),(edges,outline)]:
        for record in source.records:
            key='horse' if record.role=='invicta-emblem' else 'worksplate'
            transform=transforms[key];factor=transform.a
            attributes=dict(record.attributes)
            for name in ['data-cap-height-mm','data-tracking-mm']:
                if name in attributes:attributes[name]=str(float(attributes[name])*factor)
            kwargs=dict(source_ref=record.source_ref,role=record.role,
                        sequence=record.sequence,attributes=attributes)
            if record.vector_path is not None:
                target.add_path(record.vector_path.transformed(transform),**kwargs)
            else:
                target.add([transform.apply(p) for p in record.points],**kwargs)
    # Retain exact transforms from the published stacked layout for the audit.
    previous_scale=1.10;previous_origin=(330,24)
    previous_transforms={}
    for key,transform in transforms.items():
        ratio=transform.a/previous_scale
        previous_transforms[key]={'a':ratio,'d':ratio,
            'e':transform.e-ratio*previous_origin[0],
            'f':transform.f-ratio*previous_origin[1]}
    meta.update(zone={'x':zone.x,'y':zone.y,'width':zone.width,'height':zone.height},
        arrangement='worksplate-left-horse-right',centre_y_mm=cy,
        centre_x_mm=(plate_cx-plate_rx+horse_cx+horse_width/2)/2,
        horse_centre_mm=[horse_cx,cy],horse_height_mm=horse_height,
        horse_width_mm=horse_width,inter_vignette_gap_mm=gap+(horse_slot_width-horse_width)/2,
        horse_slot_mm={'cx':horse_cx,'cy':cy,'width':horse_slot_width,'height':horse_height},
        horse_base_to_paper_transform={k:getattr(transforms['horse'],k) for k in ['a','b','c','d','e','f']},
        worksplate_enlargement_from_v11=plate_enlargement/previous_scale,
        worksplate_transform_from_v11=previous_transforms['worksplate'],
        worksplate_ellipse_mm={'cx':plate_cx,'cy':cy,'rx':plate_rx,
                              'ry':plate['ry']*plate_enlargement},
        curved_copy_baseline_mm={'cx':plate_cx,'cy':cy,
            'rx':(plate['rx']-3.2)*plate_enlargement,
            'ry':(plate['ry']-5.2)*plate_enlargement})
    return meta

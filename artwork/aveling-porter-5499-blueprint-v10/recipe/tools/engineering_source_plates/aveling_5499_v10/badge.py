"""Source-based Invicta casting and legible, separately enlarged worksplate."""
import json
from bisect import bisect_left
from math import cos, sin, hypot, pi
from pathlib import Path
from city_map_plotter.vector_path import Affine2D
from city_map_plotter.technical_assets import parse_absolute_path_data
from city_map_plotter.stroke_font import stroke_text
from city_map_plotter.niche_common import reliable_vector_strokes
from .drafting import Path as DraftPath, circle, ellipse
from .lettering import text

HERE=Path(__file__).resolve().parent

def curved_copy(layer,value,cx,cy,rx,ry,cap):
    """Individually rotated stroke glyphs on an elliptical baseline."""
    glyphs=[];cursor=0;tracking=layer.pen.mark_width_mm*1.5
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

def add_badge(detail,outline,zone):
    data=json.loads((HERE/'invicta-emblem.json').read_text());b=data['bounds_px']
    cx=zone.centre[0];horse_height=zone.width*.46;horse_top=zone.y+.3
    factor=horse_height/(b[3]-b[1]);m=Affine2D(a=factor,d=factor,e=cx-(b[0]+b[2])/2*factor,f=horse_top-b[1]*factor)
    for i,d in enumerate(data['paths']):
        outline.add_path(parse_absolute_path_data(d).transformed(m),role='invicta-emblem',source_ref='invicta-preston',
          attributes={'data-feature':'source-casting-contour','data-source-path':str(i)})
    # Selected sculpted reliefs follow the retained photograph; they are
    # separated by pen-safe distances rather than copying specular highlights.
    reliefs=[
      DraftPath(482,102).curve(484,127,464,144,460,162),
      DraftPath(331,184).curve(356,201,388,186,414,190).curve(444,195,460,218,488,214),
      DraftPath(510,145).curve(512,204,477,248,428,265).curve(405,276,385,292,366,316),
      DraftPath(568,186).curve(595,230,594,268,579,298).curve(567,331,561,376,537,412),
      DraftPath(565,222).curve(576,246,581,253,606,263),
      DraftPath(563,280).curve(575,304,587,318,620,321),
      DraftPath(552,343).curve(570,363,580,371,601,378),
      DraftPath(184,346).curve(209,387,203,440,240,477),
      DraftPath(303,478).curve(357,464,387,495,405,530).curve(435,585,469,621,517,646),
      DraftPath(579,608).curve(566,643,547,664,518,678),
      DraftPath(484,704).curve(470,746,462,788,481,836).curve(491,856,512,876,518,896),
      DraftPath(508,760).curve(545,792,596,798,643,776),
      DraftPath(710,697).curve(760,681,812,683,832,721).curve(856,762,794,790,759,798)
        .curve(704,813,686,859,629,878),
      DraftPath(802,746).curve(784,779,745,777,725,800).curve(699,827,671,850,641,853),
    ]
    for i,p in enumerate(reliefs):
        detail.add_path(p.vector().transformed(m),role='invicta-emblem',source_ref='invicta-preston',
          attributes={'data-feature':'source-relief-detail','data-source-path':str(i)})
    detail.add_path(ellipse(447,122,7,5).transformed(m),role='invicta-emblem',source_ref='invicta-preston',attributes={'data-feature':'eye'})
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
      'worksplate_ellipse_mm':{'cx':cx,'cy':cy,'rx':rx,'ry':ry},
      'worksplate_copy':['BY ROYAL LETTERS PATENT']+[r[0] for r in rows],
      'plate_adaptation':'GWRA No.6882 layout/oval reference, adapted to user-confirmed No.5499. Lettering redrawn for pen legibility.'}

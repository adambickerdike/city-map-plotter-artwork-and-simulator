"""Pen-safe stroke lettering for the source-reconstructed sheet."""
from city_map_plotter.stroke_font import stroke_text,text_width_mm
from city_map_plotter.niche_common import reliable_vector_strokes


def text(layer,value,x,y,width,cap=3.2,role='view-label',source=None,centred=False):
    tracking=layer.pen.mark_width_mm
    natural=text_width_mm(value,cap_height_mm=cap,tracking_mm=tracking)
    if natural>width:
        base=text_width_mm(value,cap_height_mm=cap)
        cap*=max(0,(width-(len(value)-1)*tracking))/base
    if cap<8*layer.pen.mark_width_mm-1e-6:
        raise ValueError(f'Text too small: {value}')
    strokes=stroke_text(value,x_mm=x,y_mm=y,height_mm=cap,tracking_mm=tracking)
    if centred:
        xs=[q[0] for stroke in strokes for q in stroke]
        dx=x+width/2-(min(xs)+max(xs))/2
        strokes=[[(px+dx,py) for px,py in stroke] for stroke in strokes]
    layer.add_many(reliable_vector_strokes(strokes,nib_mm=layer.pen.mark_width_mm),source_ref=source,role=role,
      attributes={'data-copy':value,'data-cap-height-mm':str(cap),'data-tracking-mm':str(tracking),
                  'data-alignment':'centre' if centred else 'left'})


def card(layer,zone,*,label,value,value_cap_mm,source_ids):
    nib=layer.pen.mark_width_mm;label_cap=8*nib;leading=4*nib
    lines=list(value);height=label_cap+3*nib+len(lines)*value_cap_mm+(len(lines)-1)*leading
    y=zone.y+(zone.height-height)/2
    layer.add([(zone.x,zone.y),(zone.right,zone.y)],role='blueprint-fact-rule')
    text(layer,label,zone.x,y,zone.width,label_cap,'blueprint-fact-label',' '.join(source_ids))
    y+=label_cap+3*nib
    for i,value in enumerate(lines):text(layer,value,zone.x,y+i*(value_cap_mm+leading),zone.width,value_cap_mm,'blueprint-fact-value',' '.join(source_ids))

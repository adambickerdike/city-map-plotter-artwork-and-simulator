#!/usr/bin/env python3
"""Freeze an upright outline from the user-selected GWRA casting photograph.

Optional preparation only: normal builds consume reviewed frozen vectors.
"""
import argparse,hashlib,json,re,subprocess,sys,tempfile
from math import cos,sin,pi
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
from PIL import Image,ImageDraw,ImageFilter
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from city_map_plotter.vector_path import Affine2D
from tools.engineering_source_plates.aveling_5499_v14.drafting import Path as DraftPath
HERE=Path(__file__).resolve().parent

def parse_potrace(svg,height):
    paths=[]
    for el in ET.parse(svg).iter('{http://www.w3.org/2000/svg}path'):
        tokens=re.findall(r'[MLCZmlcz]|[-+]?(?:\d+(?:\.\d*)?|\.\d+)',el.get('d'))
        i=0;cmd=None;draft=None;at=(0,0)
        while i<len(tokens):
            if tokens[i].isalpha():
                cmd=tokens[i];i+=1
                if cmd in 'zZ':
                    draft.close();paths.append(draft.vector());at=draft.start;draft=None;continue
            n=6 if cmd in 'cC' else 2;v=list(map(float,tokens[i:i+n]));i+=n
            pts=[(v[j]+(at[0] if cmd.islower() else 0),v[j+1]+(at[1] if cmd.islower() else 0)) for j in range(0,n,2)]
            if cmd in 'mM':
                if draft is not None:paths.append(draft.vector())
                draft=DraftPath(*pts[0]);cmd='l' if cmd=='m' else 'L'
            elif cmd in 'lL':draft.line(*pts[0])
            else:draft.curve(*pts[0],*pts[1],*pts[2])
            at=pts[-1]
        if draft is not None:paths.append(draft.vector())
    return [p.transformed(Affine2D(a=.01,d=-.01,f=height)) for p in paths]

def prepare(source,output,mask_output=None):
    im=Image.open(source).convert('RGB');a=np.asarray(im,dtype=float)
    r,g,b=a[:,:,0],a[:,:,1],a[:,:,2]
    # Select warm brass/green patina and adjacent dark casting edges. Exclude
    # the cool photographic shadow, then fill highlights enclosed by the body.
    warm=(np.maximum(r,g)-b>16)&(g-b>6)
    neighbourhood=np.asarray(Image.fromarray((warm*255).astype('uint8')).filter(ImageFilter.MaxFilter(15)))>0
    foreground=warm|(neighbourhood&(a.max(2)<150))
    mask=Image.fromarray((foreground*255).astype('uint8')).filter(ImageFilter.MedianFilter(5))
    mask=mask.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(7))
    ImageDraw.floodfill(mask,(0,0),128,thresh=0)
    solid=Image.fromarray(np.where(np.asarray(mask)==128,0,255).astype('uint8'))
    solid=solid.filter(ImageFilter.GaussianBlur(5.0)).point(lambda v:255 if v>=128 else 0)
    if mask_output:solid.save(mask_output)
    with tempfile.TemporaryDirectory(prefix='gwra-invicta-') as scratch:
        p=Path(scratch)
        Image.fromarray(255-np.asarray(solid)).convert('1').save(p/'mask.pbm')
        subprocess.run(['potrace',str(p/'mask.pbm'),'-s','-o',str(p/'trace.svg'),
            '--turdsize','180','--opttolerance','5.0','--alphamax','1.0','--unit','100'],check=True)
        paths=parse_potrace(p/'trace.svg',im.height)
    assert len(paths)==1,('unexpected silhouette components',len(paths))
    angle=45.0;theta=angle*pi/180
    rotation=Affine2D(a=cos(theta),b=sin(theta),c=-sin(theta),d=cos(theta))
    oriented=[p.transformed(rotation) for p in paths];boxes=[p.bounds() for p in oriented]
    data={'source_id':'invicta-gwra-2025','source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'source_size_px':list(im.size),'source_url':'https://www.gwra.co.uk/uploads/product-images/2025mar/extralarge/51.jpg',
        'orientation_clockwise_degrees':angle,'orientation_transform':{k:getattr(rotation,k) for k in ['a','b','c','d','e','f']},
        'orientation_scope':'In-plane presentation rotation into an upright rearing pose; not a calibrated perspective reconstruction.',
        'method':'Colour-isolate brass/patina; retain adjacent dark casting edges; fill enclosed highlights; 5 px contour smoothing; Potrace cubic optimisation at 5 source pixels. Sculpted relief details are separately drafted against the same photograph.',
        'bounds_px':[min(b.min_x for b in boxes),min(b.min_y for b in boxes),max(b.max_x for b in boxes),max(b.max_y for b in boxes)],
        'source_paths':[p.to_svg_path_data() for p in paths],'paths':[p.to_svg_path_data() for p in oriented]}
    output.write_text(json.dumps(data,indent=2)+'\n')
    print(json.dumps({'outline_paths':len(paths),'segments':sum(len(p.segments) for p in paths),'oriented_bounds':data['bounds_px']},indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('--output',type=Path,default=HERE/'invicta-emblem.json')
    parser.add_argument('--mask-output',type=Path)
    args=parser.parse_args();prepare(args.source,args.output,args.mask_output)

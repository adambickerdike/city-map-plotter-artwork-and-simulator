#!/usr/bin/env python3
"""Freeze a smooth vector outline from the retained Invicta casting photograph.

Only this optional source preparation step needs Pillow, NumPy and Potrace.
The plate builder uses the reviewed, frozen cubic paths in invicta-emblem.json.
"""
import hashlib,json,re,subprocess,sys,tempfile
from pathlib import Path
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from PIL import Image,ImageDraw,ImageFilter
import numpy as np
from city_map_plotter.vector_path import Affine2D
from tools.engineering_source_plates.aveling_5499_v8.drafting import Path as DraftPath
HERE=Path(__file__).resolve().parent
source=Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'examples/technical-objects/aveling-porter-5499-blueprint-v6/evidence/invicta-horse-reference.jpg'
image=Image.open(source).convert('RGB');a=np.asarray(image,dtype=float)
foreground=(a.min(2)<225)|((a.max(2)-a.min(2)>22)&(a.min(2)<246))
mask=Image.fromarray(np.where(foreground,0,255).astype('uint8')).filter(ImageFilter.MedianFilter(3))
ImageDraw.floodfill(mask,(0,0),128,thresh=0)
solid=np.where(np.asarray(mask)==128,255,0).astype('uint8')
aperture=Image.fromarray(np.where(foreground,0,255).astype('uint8')).copy()
ImageDraw.floodfill(aperture,(756,734),128,thresh=0)
solid[np.asarray(aperture)==128]=255
silhouette=Image.fromarray(solid).filter(ImageFilter.GaussianBlur(2.0)).point(lambda v: 0 if v<128 else 255)
with tempfile.TemporaryDirectory(prefix='invicta-') as scratch:
 p=Path(scratch);silhouette.convert('1').save(p/'mask.pbm')
 command=['potrace',str(p/'mask.pbm'),'-s','-o',str(p/'trace.svg'),'--turdsize','40','--opttolerance','2.5','--alphamax','1','--unit','100']
 subprocess.run(command,check=True)
 tree=ET.parse(p/'trace.svg')
 paths=[]
 for el in tree.iter('{http://www.w3.org/2000/svg}path'):
  tokens=re.findall(r'[MLCZmlcz]|[-+]?(?:\d+(?:\.\d*)?|\.\d+)',el.get('d'));i=0;cmd=None;draft=None;at=(0,0)
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
 transform=Affine2D(a=.01,d=-.01,f=image.height)
 paths=[p.transformed(transform) for p in paths]
 boxes=[p.bounds() for p in paths]
 data={'source_id':'invicta-preston','source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
  'source_size_px':list(image.size),'source_url':'https://prestonservices.co.uk/wp-content/uploads/Invicta_Horse_CLean.jpg',
  'method':'Brass/background separation; fill photographic highlights; preserve reviewed tail aperture; 2 px Gaussian contour smoothing and Potrace 1.16 cubic optimisation at 2.5 source pixels (less than 0.09 mm on paper). Interior relief lines separately interpreted from the same photograph.',
  'bounds_px':[min(b.min_x for b in boxes),min(b.min_y for b in boxes),max(b.max_x for b in boxes),max(b.max_y for b in boxes)],
  'paths':[p.to_svg_path_data() for p in paths]}
 (HERE/'invicta-emblem.json').write_text(json.dumps(data,indent=2)+'\n')
 print(json.dumps({'outline_paths':len(paths),'bounds':data['bounds_px'],'cubic_segments':sum(len(p.segments) for p in paths)},indent=2))

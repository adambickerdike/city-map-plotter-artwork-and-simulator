"""Analytic drafting primitives and curve-preserving hidden-line removal.

Reconstruction coordinates are unitless. Geometry is fitted/regularised at the
component level; no edge-map or skeleton path is consumed by this module.
"""
from dataclasses import dataclass, field
from math import atan2, ceil, cos, hypot, pi, sin, tan
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep
from city_map_plotter.vector_path import VectorPath, LineSegment, CubicSegment


def polar(cx, cy, radius, angle):
    a=angle*pi/180
    return (cx+radius*cos(a),cy+radius*sin(a))


class Path:
    def __init__(self, x, y):
        self.start=(x,y);self.at=self.start;self.segments=[]
    def line(self,x,y):
        if hypot(x-self.at[0],y-self.at[1])>1e-9:
            self.segments.append(LineSegment((x,y)));self.at=(x,y)
        return self
    def curve(self,a,b,c,d,x,y):
        if max(hypot(px-self.at[0],py-self.at[1]) for px,py in [(a,b),(c,d),(x,y)])<1e-9:return self
        self.segments.append(CubicSegment((a,b),(c,d),(x,y)));self.at=(x,y);return self
    def arc(self,cx,cy,r,a,b,ry=None):
        ry=r if ry is None else ry
        pieces=max(1,ceil(abs(b-a)/45))
        for i in range(pieces):
            t0=(a+(b-a)*i/pieces)*pi/180;t1=(a+(b-a)*(i+1)/pieces)*pi/180
            k=4/3*tan((t1-t0)/4)
            p0=(cx+r*cos(t0),cy+ry*sin(t0));p1=(cx+r*cos(t1),cy+ry*sin(t1))
            if hypot(self.at[0]-p0[0],self.at[1]-p0[1])>1e-7:self.line(*p0)
            self.curve(p0[0]-k*r*sin(t0),p0[1]+k*ry*cos(t0),p1[0]+k*r*sin(t1),p1[1]-k*ry*cos(t1),*p1)
        return self
    def close(self):
        return self.line(*self.start)
    def vector(self):
        return VectorPath(self.start,tuple(self.segments))


def line(*points):
    p=Path(*points[0])
    for q in points[1:]:p.line(*q)
    return p.vector()


def polygon(points):
    p=Path(*points[0])
    for q in points[1:]:p.line(*q)
    return p.close().vector()


def circle(x,y,r):
    return Path(x+r,y).arc(x,y,r,0,360).vector()


def ellipse(x,y,rx,ry):
    return Path(x+rx,y).arc(x,y,rx,0,360,ry).vector()


def rect(x,y,w,h,r=0):
    if r==0:return polygon([(x,y),(x+w,y),(x+w,y+h),(x,y+h)])
    return (Path(x+r,y).line(x+w-r,y).arc(x+w-r,y+r,r,-90,0)
        .line(x+w,y+h-r).arc(x+w-r,y+h-r,r,0,90).line(x+r,y+h)
        .arc(x+r,y+h-r,r,90,180).line(x,y+r).arc(x+r,y+r,r,180,270).vector())


def region(path):
    return Polygon(path.flatten(.08).points).buffer(0)


def circular_geometry(path):
    """Recognise a complete analytic circle before any hidden-line clipping."""
    if len(path.segments)!=8 or not all(isinstance(s,CubicSegment) for s in path.segments):return None
    if hypot(path.start[0]-path.segments[-1].to[0],path.start[1]-path.segments[-1].to[1])>1e-6:return None
    b=path.bounds();cx=(b.min_x+b.max_x)/2;cy=(b.min_y+b.max_y)/2;r=(b.max_y-b.min_y)/2
    if r<1e-9 or abs(b.max_x-b.min_x-2*r)>1e-5:return None
    if max(abs(hypot(s.to[0]-cx,s.to[1]-cy)-r) for s in path.segments)>1e-5:return None
    return cx,cy,r


@dataclass
class Component:
    name: str
    source: str
    note: str
    strokes: list=field(default_factory=list)
    mask: object=None
    projector: object=None
    circle_constraints: list=field(default_factory=list)
    def prepared(self,path,feature):
        if self.projector is not None:path=self.projector(path)
        circular=circular_geometry(path)
        if circular:
            feature=f'{feature}:circle-{len(self.circle_constraints):03d}'
            self.circle_constraints.append({'feature':feature,'centre':list(circular[:2]),'radius':circular[2]})
        return path,feature
    def add(self,path,weight='fine',feature='edge'):
        path,feature=self.prepared(path,feature)
        self.strokes.append((path,weight,feature));return path
    def solid(self,path,weight='outline',feature='component-outline'):
        path,feature=self.prepared(path,feature)
        shape=region(path)
        # A later boss, flange or bearing is also in front of earlier geometry
        # within this assembly. Clip those paths as well as other assemblies;
        # otherwise the rod edge runs visibly through its own pivot boss.
        self.strokes=[(part,w,f) for p,w,f in self.strokes
                      for part in visible_paths(p,shape)]
        self.strokes.append((path,weight,feature))
        self.mask=shape if self.mask is None else self.mask.union(shape)
        return path


def mix(a,b,t):return tuple(x+(y-x)*t for x,y in zip(a,b))


def split(c,t):
    a=mix(c[0],c[1],t);b=mix(c[1],c[2],t);d=mix(c[2],c[3],t)
    e=mix(a,b,t);f=mix(b,d,t);g=mix(e,f,t)
    return (c[0],a,e,g),(g,f,d,c[3])


def portion(c,a,b):
    left,_=split(c,b)
    if a==0:return left
    _,part=split(left,a/b)
    return part


def cubic_at(c,t):
    return tuple((1-t)**3*c[0][i]+3*(1-t)**2*t*c[1][i]+3*(1-t)*t*t*c[2][i]+t**3*c[3][i] for i in (0,1))


def visible_paths(path,occlusion):
    """Find mask crossings, then retain original lines and exact cubic portions."""
    if occlusion is None or occlusion.is_empty:return [path]
    if not LineString(path.flatten(.1).points).intersects(occlusion):return [path]
    mask=prep(occlusion);out=[];active=None;at=path.start
    for segment in path.segments:
        is_line=isinstance(segment,LineSegment)
        if is_line:
            c=(at,mix(at,segment.to,1/3),mix(at,segment.to,2/3),segment.to)
        else:c=(at,segment.control_1,segment.control_2,segment.to)
        n=max(12,ceil(sum(hypot(b[0]-a[0],b[1]-a[1]) for a,b in zip(c,c[1:]))/2))
        ts=[i/n for i in range(n+1)]
        inside=[mask.contains(Point(cubic_at(c,t))) for t in ts]
        cuts=[0.0]
        for i in range(n):
            if inside[i]==inside[i+1]:continue
            lo,hi=ts[i],ts[i+1]
            for _ in range(26):
                mid=(lo+hi)/2
                if mask.contains(Point(cubic_at(c,mid)))==inside[i]:lo=mid
                else:hi=mid
            cuts.append((lo+hi)/2)
        cuts.append(1.0)
        for a,b in zip(cuts,cuts[1:]):
            if b-a<1e-9:continue
            if mask.contains(Point(cubic_at(c,(a+b)/2))):
                if active is not None and active.segments:out.append(active.vector())
                active=None
                continue
            q=portion(c,a,b)
            if max(hypot(v[0]-q[0][0],v[1]-q[0][1]) for v in q[1:])<1e-8:continue
            if active is None:active=Path(*q[0])
            elif hypot(active.at[0]-q[0][0],active.at[1]-q[0][1])>1e-5:
                if active.segments:out.append(active.vector())
                active=Path(*q[0])
            if is_line:active.line(*q[-1])
            else:active.curve(*q[1],*q[2],*q[3])
        at=segment.to
    if active is not None and active.segments:out.append(active.vector())
    # A closed contour's arbitrary authoring seam is not a physical break.
    # Join the visible tail and head before the nib-length gate, otherwise a
    # short head fragment can be discarded and leave a false gap in a collar.
    if len(out)>1 and hypot(path.start[0]-path.segments[-1].to[0],path.start[1]-path.segments[-1].to[1])<1e-6:
        end=out[-1].segments[-1].to;start=out[0].start
        if hypot(end[0]-start[0],end[1]-start[1])<1e-6:
            out=[VectorPath(out[-1].start,out[-1].segments+out[0].segments)]+out[1:-1]
    return [p for p in out if p.segments]


def resolve_scene(components):
    cover=None;emitted=[]
    for component in reversed(components):
        for path,weight,feature in component.strokes:
            for visible in visible_paths(path,cover):
                emitted.append({'path':visible,'weight':weight,'feature':feature,
                    'component':component.name,'source':component.source,'note':component.note})
        if component.mask is not None:
            cover=component.mask if cover is None else cover.union(component.mask)
    return list(reversed(emitted))

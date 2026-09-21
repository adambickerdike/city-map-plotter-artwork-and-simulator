"""Single side elevation: reference-photo geometry, archival drafting language.

The MERL Fowler S1021 sheet is a composition/linework reference, NOT a drawing
of 5499. The dimensions below are working-image units, not inches or mm.
Photo stations are corrected to their common side plane. Joints share anchors.
"""
import json
from pathlib import Path as FilePath
from math import acos, asin, atan2, cos, degrees, hypot, pi, sin, sqrt
from shapely.affinity import affine_transform
from shapely.ops import unary_union
from city_map_plotter.vector_path import Affine2D
from .drafting import Component, Path, circle, ellipse, line, polygon, polar, rect, region, resolve_scene, circular_geometry, visible_paths

HERE = FilePath(__file__).resolve().parent
LANDMARKS = json.loads((HERE / 'reference-landmarks.json').read_text())
FITS = {name: item['circular_fit'] for name,item in LANDMARKS['rim_landmarks'].items()}
REAR_R = FITS['rear_roll']['radius']
FRONT_R = FITS['front_roll']['radius']
FLY_R = FITS['flywheel']['radius']
GROUND = FITS['rear_roll']['centre'][1] + REAR_R
FRONT_WIDTH = 302 / (261 / (2 * FRONT_R))
REAR_OUTER = 403 / (313 / (2 * REAR_R)) / 2
YAW = asin((335 - FITS['front_roll']['centre'][0]) / (FRONT_WIDTH / 2))

def sx(x, lateral=0):
    return (x - (REAR_OUTER-lateral)*sin(YAW)) / cos(YAW)

FRONT_X = sx(FITS['front_roll']['centre'][0], FRONT_WIDTH/2)
REAR_X = sx(FITS['rear_roll']['centre'][0], REAR_OUTER)
FRONT_Y, REAR_Y = GROUND-FRONT_R, GROUND-REAR_R
FLY_X, FLY_Y = sx(FITS['flywheel']['centre'][0], 117), FITS['flywheel']['centre'][1]
CHIMNEY_X, CHIMNEY_TOP = sx(512), 344
BOILER_TOP, BOILER_BOTTOM = 668, 846
# End of the centre-plane boiler reaches the same side-elevation station as
# the firebox's outer frame. Separate photo-plane corrections must not leave
# a gap at this real mechanical joint.
BOILER_REAR = 864 + 100*sin(YAW)
# The firebox rear wall continues the control housing's visible rear edge.
# Both belong to the same side plane; the bed flange sits slightly inside it.
FIREBOX_SIDE_X = 1177
FIREBOX_CORNER_RADIUS = 12
# The kingpin, crosshead, fork, front bearing and circular roll share FRONT_X.
KINGPIN_Y, FORK_TOP = 716, 753
STEERING_X, STEERING_Y = sx(805,155), 900
HANDWHEEL_C=(1209,638.5)
HANDWHEEL_MAJOR=38
FAR_WINDER=(STEERING_X-17,STEERING_Y-15)
# The edge-on wheel plane is perpendicular to the shaft in the side elevation.
HANDWHEEL_ANGLE=atan2(sx(HANDWHEEL_C[0],100)-FAR_WINDER[0],(FAR_WINDER[1]-HANDWHEEL_C[1])*cos(YAW))
FEED_X=735
REAR_SPOKES,FRONT_SPOKES=10,6
SPRING_EYE=(FRONT_X-81,FRONT_Y-1)
RELEASE_PIVOT=(1295+(681-602)*15/96,602)
RELEASE_TIP=(1288,568)

PLANES={'rear-frame-and-platform':100,'tender-and-water-box':155,'boiler-and-smokebox':0,
 'under-boiler-fittings':100,'motion-bed-and-bearings':90,'motion-top-linkage':90,
 'cylinder-cover':90,'steam-valves-and-lubricator':90,'feed-control-and-pipe':90,
 'front-casting':0,'driver-controls':100,'rear-brake-and-scraper':185}

def project_path(path,lateral):
    # A circle reconstructed in its side plane must remain circular. Applying
    # the photo's horizontal correction to an already drafted circle distorted
    # every small bolt and boss in revision 4.
    circular=circular_geometry(path)
    if circular:
        x,y,r=circular;return circle(sx(x,lateral),y,r)
    return path.transformed(Affine2D(a=1/cos(YAW),d=1,e=-(REAR_OUTER-lateral)*sin(YAW)/cos(YAW)))

def component(name,note,source='side-photo-2022'):
    projector=(lambda path:project_path(path,PLANES[name])) if name in PLANES else None
    return Component(name,source,note,projector=projector)

def plane(c,lateral):
    assert PLANES[c.name]==lateral
    return c

def capsule(a,b,r):
    angle=degrees(atan2(b[1]-a[1],b[0]-a[0]))
    p=polar(*a,r,angle-90)
    return (Path(*p).line(*polar(*b,r,angle-90)).arc(*b,r,angle-90,angle+90)
            .line(*polar(*a,r,angle+90)).arc(*a,r,angle+90,angle+270).vector())

def unequal_capsule(a,b,ra,rb):
    theta=degrees(atan2(b[1]-a[1],b[0]-a[0]));distance=hypot(b[0]-a[0],b[1]-a[1])
    alpha=degrees(acos((ra-rb)/distance));hi=theta+alpha;lo=theta-alpha
    return (Path(*polar(*a,ra,hi)).line(*polar(*b,rb,hi)).arc(*b,rb,hi,lo)
       .line(*polar(*a,ra,lo)).arc(*a,ra,lo,hi-360).vector())

def oriented_box(cx,cy,width,height,angle):
    a=angle*pi/180
    return rect(-width/2,-height/2,width,height,1).transformed(Affine2D(a=cos(a),b=sin(a),c=-sin(a),d=cos(a),e=cx,f=cy))

def bolts(c, points, radius=3.2, washer=False):
    for x,y in points:
        c.add(circle(x,y,radius),feature='source-fastener-pattern')
        if washer:c.add(circle(x,y,radius+2.5),feature='washer')

def rod(c,a,b,width=6,radius=8,weight='fine',feature='pinned-rod'):
    dx,dy=b[0]-a[0],b[1]-a[1];length=hypot(dx,dy);nx,ny=-dy/length*width/2,dx/length*width/2
    c.solid(polygon([(a[0]+nx,a[1]+ny),(b[0]+nx,b[1]+ny),(b[0]-nx,b[1]-ny),(a[0]-nx,a[1]-ny)]),weight,feature+'-bar')
    for end,p in [('a',a),('b',b)]:
        c.solid(circle(*p,radius),weight,feature+'-boss-'+end)
        c.add(circle(*p,radius*.42),feature=feature+'-pin-'+end)

def opening(cx,cy,outer,root,angle,step,half_width,root_sweep=0):
    """Annular window between inclined cast spokes, with exact tangent fillets.

    Root and rim stations may differ in angle. The straight spoke edges remain
    parallel; four fillet centres are solved as offset-line/circle intersections.
    This preserves mechanical joins without shearing the circular wheel face.
    """
    f=3.0
    def side(theta,sign):
        a=polar(0,0,root,theta+root_sweep);b=polar(0,0,outer,theta)
        d=hypot(b[0]-a[0],b[1]-a[1]);e=((b[0]-a[0])/d,(b[1]-a[1])/d)
        n=(-e[1],e[0]);h=n[0]*a[0]+n[1]*a[1]+sign*(half_width+f)
        def centre(radius):
            t=sqrt(radius*radius-h*h)
            return (t*e[0]+h*n[0],t*e[1]+h*n[1])
        co,ci=centre(outer-f),centre(root+f)
        ao=degrees(atan2(co[1],co[0]));ai=degrees(atan2(ci[1],ci[0]))
        edge=degrees(atan2(-sign*n[1],-sign*n[0]))
        return co,ci,ao,ai,edge,n
    lo,li,la,lb,le,ln=side(0,1)
    ho,hi,ha,hb,he,hn=side(step,-1)
    def onward(a,b):
        while b<a:b+=360
        return b
    ha=onward(la,ha);he=onward(ha,he)
    h_inner=onward(he,hb+180)
    low_inner=lb+180
    while low_inner>h_inner:low_inner-=360
    le=onward(low_inner,le)
    end=onward(le,la+360)
    p=Path(*polar(0,0,outer,la)).arc(0,0,outer,la,ha).arc(*ho,f,ha,he)
    p.line(hi[0]+f*hn[0],hi[1]+f*hn[1]).arc(*hi,f,he,h_inner)
    p.arc(0,0,root,h_inner-180,low_inner-180).arc(*li,f,low_inner,le)
    p.line(lo[0]-f*ln[0],lo[1]-f*ln[1]).arc(*lo,f,le,end)
    t=angle*pi/180
    return p.vector().transformed(Affine2D(a=cos(t),b=sin(t),c=-sin(t),d=cos(t),e=cx,f=cy))

def wheel(name,cx,cy,r,spokes,phase,inner=False):
    c=component(name,'User-requested reduced spoke count with staggered inner spokes and deeper roller rims; source-fitted circular outlines retained. Count and inclinations are illustrative.','user-detail-correction-v8')
    rear=name.startswith('rear')
    rim_depth,lip_depth=(38,8) if rear else (23,7)
    outside=circle(cx,cy,r)
    if not inner:
        c.add(outside,'roller','fitted-circular-rim')
        c.add(circle(cx,cy,r-lip_depth),'outline',feature='rim-lip')
    outer,root=r-rim_depth,48 if rear else 31
    step=360/spokes
    if inner:phase+=step/2
    sweep=step*(-.67 if inner else .22)
    windows=[]
    for n in range(spokes):
        angle=phase+n*step
        w=opening(cx,cy,outer,root,angle,step,6 if rear else 5,sweep)
        c.add(w,feature='cast-spoke-opening');windows.append(region(w))
        if inner:continue
        for delta in [-2.2,2.2]:
            q=polar(cx,cy,r-(rim_depth+lip_depth)/2,angle+delta)
            c.add(circle(*q,4.1 if rear else 2.7),feature='rim-fastener')
            if rear:c.add(polygon([polar(*q,2.1,k*60+30) for k in range(6)]),feature='rim-bolt-hexagon')
    c.mask=region(outside).difference(unary_union(windows))
    if not inner:
        c.add(circle(cx,cy,root-3),feature='hub-casting')
        c.add(circle(cx,cy,root-9),feature='hub-shoulder')
    return c

def linked_chain(c,a,b,control1,control2,pitch=12):
    """Continuous, tangent-aligned alternating links; endpoints hit anchor eyes."""
    def point(t):return tuple((1-t)**3*a[k]+3*(1-t)**2*t*control1[k]+3*(1-t)*t*t*control2[k]+t**3*b[k] for k in [0,1])
    ps=[point(i/500) for i in range(501)];ds=[0]
    for p,q in zip(ps,ps[1:]):ds.append(ds[-1]+hypot(q[0]-p[0],q[1]-p[1]))
    count=max(2,2*round(ds[-1]/pitch/2));spacing=ds[-1]/count;j=0;eyes=[]
    for n in range(count+1):
        target=n*spacing
        while j<499 and ds[j+1]<target:j+=1
        f=(target-ds[j])/(ds[j+1]-ds[j]);q=tuple(ps[j][k]+f*(ps[j+1][k]-ps[j][k]) for k in [0,1])
        dx,dy=ps[j+1][0]-ps[j][0],ps[j+1][1]-ps[j][1];length=hypot(dx,dy)
        if n%2:continue
        u=(dx/length,dy/length);major=spacing*.72
        shape=ellipse(0,0,major,2.8)
        c.add(shape.transformed(Affine2D(a=u[0],b=u[1],c=-u[1],d=u[0],e=q[0],f=q[1])),feature='chain-link-eye')
        eyes.append((q,u,major))
    for (a,u,ra),(b,v,rb) in zip(eyes,eyes[1:]):
        c.add(line((a[0]+u[0]*ra,a[1]+u[1]*ra),(b[0]-v[0]*rb,b[1]-v[1]*rb)),feature='chain-link-connector')

def return_pipe_mask():
    """Opaque tube between the two existing return-pipe edges, in side view."""
    tube=(Path(769,870).line(769,907).arc(783,907,14,180,0).line(797,877)
          .line(791,877).line(791,907).arc(783,907,8,0,180).line(775,870)
          .close().vector())
    return region(project_path(tube,PLANES['under-boiler-fittings']))

def firebox_shoulder():
    """Upright into a tangent 90-degree deck corner, circular after projection."""
    x=FIREBOX_SIDE_X;r=FIREBOX_CORNER_RADIUS
    return (Path(x,650).line(x,757-r)
            .arc(x+r*cos(YAW),757-r,r*cos(YAW),180,90,ry=r)
            .line(1426,757).vector())

def side_components():
    out=[]
    # The shaft belongs to the far side. Its complete centreline aims at the
    # opposite winding gear; only portions clear of foreground metal are inked.
    c=component('far-steering-column','Inclined shaft to the far-side steering gear, hidden by the rear structure including all rear spoke openings.','user-detail-correction-v8')
    handwheel_side=(sx(HANDWHEEL_C[0],100),HANDWHEEL_C[1])
    c.solid(capsule(handwheel_side,FAR_WINDER,2.7),'fine','steering-column-shaft')
    # The rear structure conceals this far-side shaft; it must not reappear
    # through the near wheel's open cast-spoke windows.
    rear_cover=region(circle(REAR_X,REAR_Y,REAR_R))
    c.strokes=[(part,w,f) for path,w,f in c.strokes for part in visible_paths(path,rear_cover)]
    out.append(c)
    # Rear construction visible through the cast wheel openings.
    c=component('rear-frame-and-platform','Firebox upright continues the upper control-housing rear edge, with a tangent circular quarter-turn into the rear deck; rivets retain their observed rows.')
    shoulder=firebox_shoulder()
    frame=Path(864,650).line(*shoulder.start)
    frame.segments.extend(shoulder.segments);frame.at=shoulder.segments[-1].to
    c.solid(frame.line(1426,978).line(1357,998).line(812,998)
      .line(812,985).line(842,953).line(854,894).close().vector(),feature='continuous-firebox-frame')
    c.add(line((814.8125,982),(1416,982)));c.add(line((822.3125,974),(1426,974)))
    c.add(line((876,710),(876,974)));c.add(line((912,710),(912,974)))
    c.add(line((854,761),(1426,761)));c.add(line((854,779),(1426,779)))
    for x in [865,886,913,941]:bolts(c,[(x,y) for y in range(765,967,23)])
    c.add(rect(1260,799,49,111,5));bolts(c,[(1272,y) for y in [817,849,881]])
    out.append(plane(c,100))
    c=component('tender-and-water-box','Photographed flared water-box top, round lower corner, riveted seam and tow outlet.')
    c.solid(Path(1225,758).line(1467,758).line(1455,775).line(1436,786).line(1436,956)
      .curve(1436,985,1417,1001,1392,1001).line(1225,1001).close().vector())
    c.add(Path(1225,769).line(1448,769).line(1428,782).line(1428,952).curve(1428,976,1417,991,1394,991).line(1225,991).close().vector())
    c.add(line((1225,886),(1428,886)));bolts(c,[(x,874) for x in range(1268,1424,20)],2.6)
    c.solid(rect(1250,748,223,10,5))
    c.solid(polygon([(1427,797),(1454,809),(1444,851),(1436,850)]))
    c.solid(rect(1429,922,38,20,6));c.add(circle(1445,932,5))
    c.solid(Path(1462,925).line(1496,925).line(1503,913).line(1510,913).line(1510,943).line(1497,943).line(1497,937).line(1462,937).close().vector())
    c.solid(capsule((1503,909),(1503,927),2.5),'fine');out.append(plane(c,155))
    # Boiler continues under the smoke-box, cylinder foot and rear motion bed.
    c=component('boiler-and-smokebox','Straight barrel edges and continuous flanged smokebox, regularised from the source photograph.')
    c.solid(rect(469,BOILER_TOP,BOILER_REAR-469,BOILER_BOTTOM-BOILER_TOP,4))
    c.add(line((478,BOILER_TOP+7),(BOILER_REAR,BOILER_TOP+7)));c.add(line((478,BOILER_BOTTOM-6),(BOILER_REAR,BOILER_BOTTOM-6)))
    c.add(line((478,BOILER_TOP),(478,BOILER_BOTTOM)))
    for x in [535,696,812]:
        c.add(rect(x,BOILER_TOP,11,BOILER_BOTTOM-BOILER_TOP));c.add(line((x+4,BOILER_TOP),(x+4,BOILER_BOTTOM)))
        c.add(rect(x+2,BOILER_BOTTOM,7,9,1))
    bolts(c,[(484,y) for y in [692,745,800,830]],3)
    out.append(plane(c,0))
    c=component('under-boiler-fittings','Source-visible drain, return loop and two pipe unions below the barrel.')
    c.solid(rect(764,BOILER_BOTTOM-2,16,15,2));c.add(rect(767,859,10,11))
    c.add(Path(769,870).line(769,907).arc(783,907,14,180,0).line(797,877).vector(),feature='return-pipe-outer')
    c.add(Path(775,870).line(775,907).arc(783,907,8,180,0).line(791,877).vector(),feature='return-pipe-inner')
    # Keep the accepted pipe contours; remove far-chain ink inside the tube.
    c.mask=c.mask.union(return_pipe_mask())
    c.add(rect(788,868,12,9,2));c.add(line((792,868),(807,853)))
    c.solid(rect(805,848,20,14,2));c.add(rect(811,861,8,24));c.add(rect(805,884,20,7,2))
    c.add(line((815,891),(815,922),(828,922)));out.append(plane(c,100))
    c=component('motion-bed-and-bearings','One connected bedplate from cylinder flange to the crankshaft bearing; source-visible covers and fasteners.')
    c.solid(rect(667,615,496,85,3));c.add(rect(663,609,505,8,2));c.add(rect(663,699,505,8,2))
    c.add(line((667,625),(1163,625)))
    bolts(c,[(x,691) for x in [687,731,789,824,1109,1151]])
    bolts(c,[(685,631),(685,651),(1144,631),(1144,651)])
    c.add(rect(1134,617,28,54,4));bolts(c,[(1146,626),(1146,661)],3.5)
    out.append(plane(c,90))
    c=component('motion-top-linkage','Source-observed upper guide and valve linkage; joined supports and connected rods.')
    c.solid(Path(704,609).line(744,580).line(1088,580).line(1088,587)
       .line(747,587).line(711,614).close().vector(),'fine')
    c.solid(rect(772,590,14,19,2));c.add(rect(768,586,22,7,2))
    c.solid(rect(1078,576,20,35,3));bolts(c,[(1088,584),(1088,603)],3)
    out.append(plane(c,90))
    c=component('cylinder-cover','Actual rectangular cylinder cover, raised maker plate and bolted foot; no invented side section.')
    c.solid(rect(577,574,96,122,3));c.add(rect(583,605,84,80,2),feature='cylinder-front-cover')
    c.add(line((577,600),(673,600)))
    # The photographed brass worksplate is oval. Its wording is enlarged in
    # the corner badge, rather than reduced to an isolated serial on the engine.
    c.add(ellipse(625,645,29,21.5),feature='maker-plate-outer')
    c.add(ellipse(625,645,26,18.5),feature='maker-plate-inner')
    c.solid(rect(570,694,110,10,2))
    bolts(c,[(587,585),(663,585),(591,613),(659,613),(591,677),(659,677)],3)
    bolts(c,[(625+d,699) for d in [-44,-22,0,22,44]],3.2)
    out.append(plane(c,90))
    c=component('steam-valves-and-lubricator','Paired valve columns, lever, valve block and drain cock read directly from the close source photo.')
    for x in [611,637]:
        c.solid(rect(x-9,568,18,6,1),'fine')
        c.solid(rect(x-5,535,10,33,1),'fine')
        c.solid(polygon([(x-8,531),(x-5,528),(x+5,528),(x+8,531),(x+8,535),(x-8,535)]),'fine')
        c.solid(rect(x-2.5,519,5,9,1),'fine')
    c.solid(rect(621.5,501,7,30,1),'fine');c.solid(rect(620,498,10,4,1),'fine')
    c.solid(capsule((611,519),(663,519),2.5),'fine')
    c.solid(circle(611,519,3.8),'fine');c.solid(circle(637,519,3.8),'fine')
    # Lubricator body and its support are centred on one vertical shaft.
    c.solid(rect(696,579,8,30,1),'fine');c.solid(rect(689,601,22,8,2),'fine')
    c.solid(rect(680,568,40,6,2),'fine');c.solid(rect(686,574,28,5,1),'fine')
    c.solid(rect(683,534,34,34,3),'fine')
    c.add(circle(700,550,10),feature='valve-cover');c.add(circle(700,550,4.5),feature='valve-spindle')
    bolts(c,[(688,561),(712,561)],2.2)
    c.solid(Path(683,547).line(672,547).curve(663,547,663,539,672,539).line(683,539)
       .line(683,542).line(672,542).curve(667,542,667,544,672,544).line(683,544).close().vector(),'fine')
    c.solid(Path(717,551).line(724,551).arc(724,556,5,-90,0).line(729,604)
       .line(726,604).line(726,556).arc(724,556,2,0,-90).line(717,554).close().vector(),'fine')
    c.solid(rect(724,601,7,8,1),'fine')
    # Front lubricator: concentric collar, bowl and a constant-width pipe elbow.
    cx=562.5
    c.solid(rect(cx-5.5,558,11,40,2),'fine');c.solid(rect(cx-8,555,16,4,1),'fine')
    c.solid(circle(cx,606,8),'fine');c.add(circle(cx,606,3.2),feature='lubricator-sight')
    c.solid(Path(cx-3,614).line(cx-3,626).arc(cx+7,626,10,180,90).line(577,636)
       .line(577,632).line(cx+7,632).arc(cx+7,626,6,90,180).line(cx+1,614).close().vector(),'fine')
    out.append(plane(c,90))
    c=component('feed-control-and-pipe','Observed small lever and oval inspection boss on the boiler; the coupling rod reaches its next bearing.')
    c.solid(ellipse(FEED_X,736,34,21),'fine','feed-fitting-outline');c.add(ellipse(FEED_X,736,27,15));c.add(ellipse(FEED_X,736,12,7.5))
    c.solid(capsule((FEED_X,716),(FEED_X,753),3),'fine','feed-valve-spindle')
    # The photographed long linkage continues behind the flywheel. It does
    # not terminate in the unrelated floating rectangular box used in v4.
    end_x=FLY_X*cos(YAW)+(REAR_OUTER-90)*sin(YAW)
    c.solid(capsule((FEED_X,715),(end_x,735),3.5),'fine')
    rod(c,(FEED_X-16,665),(FEED_X,715),12,8,feature='feed-lever')
    out.append(plane(c,90))
    # Source-cast saddle, kingpin and chimney pedestal are one joined structure.
    c=component('front-casting','Aveling winged front casting from both photos; continuous upper/lower contours and rib.')
    king=335
    c.solid(Path(299,615).curve(299,608,306,605,314,605).line(356,605)
      .curve(369,605,371,617,376,630).curve(389,655,420,659,449,659)
      .line(477,659).line(477,648).line(543,648).line(543,749).line(463,749)
      .curve(438,749,437,716,395,716).line(297,716).close().vector())
    c.add(Path(299,614).line(352,614).curve(363,614,365,631,371,642)
      .curve(386,665,416,670,449,670).line(480,670).curve(487,670,490,664,490,657).vector())
    bolts(c,[(463,690),(523,691),(493,719),(464,738),(533,738)],4,True)
    c.solid(rect(335-24.5,598,49,8,2));c.add(rect(335-8.5,582,17,16,2))
    c.solid(capsule((342.5,590),(357,590),2),'fine')
    out.append(plane(c,0))
    c=component('chimney','Source-confirmed upward-widening chimney, rolled cap, bolted collar and continuous saddle pedestal.')
    x=CHIMNEY_X
    c.solid(Path(x-38,355).line(x+38,355).line(x+32,591).line(x-32,591).close().vector(),feature='upward-widening-chimney-barrel')
    c.solid(Path(x-43,344).line(x+43,344).line(x+43,350).line(x+34,356).line(x-34,356).line(x-43,350).close().vector())
    c.add(line((x-43,350),(x+43,350)))
    c.solid(rect(x-43,590,86,11,2));bolts(c,[(x+d,595) for d in [-32,-16,0,16,32]],2.8)
    c.solid(rect(x-47,601,94,10,2))
    c.solid(Path(x-32,611).curve(x-27,626,x-26,640,x-33,657).line(x+33,657)
      .curve(x+26,640,x+27,626,x+32,611).close().vector())
    out.append(c)
    # Rear controls are behind the flywheel and driving wheel.
    c=component('driver-controls','Visible crank bearing, regulator lever and brake quadrant; all rods terminate in bearings or pins.')
    c.solid(rect(1080,612,97,47,3));bolts(c,[(1094,625),(1156,625),(1157,648)],3.8)
    pivot=(1162,582)
    c.solid(rect(pivot[0]-11,572,22,40,2),'fine');bolts(c,[(1162,600)],3)
    c.solid(capsule((1080,586),pivot,3),'fine')
    c.solid(capsule(pivot,(1170,535),3),'fine');c.solid(circle(*pivot,6),'fine');c.add(circle(*pivot,2.5))
    c.solid(capsule((1169.2,539.7),(1171.6,525.6),4.5),'fine')
    # True side view: the rim and spokes collapse to an edge profile. There
    # is no elliptical face or circular face-on hub in this projection.
    wheel_c=HANDWHEEL_C
    angle=HANDWHEEL_ANGLE
    def on_rim(t):return (wheel_c[0]+t*cos(angle),wheel_c[1]+t*sin(angle))
    c.solid(capsule(on_rim(-HANDWHEEL_MAJOR),on_rim(HANDWHEEL_MAJOR),2.4),'fine','steering-wheel-edge-rim')
    c.solid(capsule(on_rim(-4.5),on_rim(4.5),4),'fine','steering-wheel-hub')
    # The square notches are part of the quadrant outline, not floating teeth.
    qx,qy,qr=1180,1010,353
    p=Path(*polar(qx,qy,qr,-92));at=-92
    for n in range(12):
        a=-90+n*1.5;b=a+.7
        p.arc(qx,qy,qr,at,a).line(*polar(qx,qy,qr+5,a)).arc(qx,qy,qr+5,a,b).line(*polar(qx,qy,qr,b));at=b
    p.arc(qx,qy,qr,at,-70).line(*polar(qx,qy,qr-12,-70)).arc(qx,qy,qr-12,-70,-92).close()
    c.solid(p.vector(),'fine')
    lever_a,lever_b=(1295,681),(1310,585)
    c.solid(capsule(lever_a,lever_b,3.5),'fine');c.solid(circle(*lever_a,6),'fine');c.add(circle(*lever_a,2.5))
    angle=degrees(atan2(lever_b[1]-lever_a[1],lever_b[0]-lever_a[0]))-90
    c.solid(capsule((1309.2,590.0),(1312.3,570.3),4.5),'fine')
    # Source-visible trigger diverges from the fixed grip in a small open V.
    # Both limbs share a real hinge; the release is not a floating accessory.
    release_pivot,release_tip=RELEASE_PIVOT,RELEASE_TIP
    c.solid(capsule(release_pivot,release_tip,2),'fine','quadrant-squeeze-handle')
    c.solid(circle(*release_pivot,3.2),'fine','quadrant-release-hinge')
    c.add(circle(*release_pivot,1.6),feature='quadrant-release-pin')
    t=.72;keeper=(lever_a[0]+t*(lever_b[0]-lever_a[0]),lever_a[1]+t*(lever_b[1]-lever_a[1]))
    c.solid(oriented_box(*keeper,16,9,angle),'fine')
    for t in [.27,.52]:c.add(circle(lever_a[0]+t*(lever_b[0]-lever_a[0]),lever_a[1]+t*(lever_b[1]-lever_a[1]),2.3))
    out.append(plane(c,100))
    c=component('solid-flywheel','Solid disc and dished centre of 5499, distinguished from the spoked Fowler reference.')
    c.solid(circle(FLY_X,FLY_Y,FLY_R));c.add(circle(FLY_X,FLY_Y,FLY_R-6))
    c.add(circle(FLY_X,FLY_Y,FLY_R-34),feature='dished-disc-shoulder')
    c.add(circle(FLY_X,FLY_Y,28));c.add(circle(FLY_X,FLY_Y,20));c.add(circle(FLY_X,FLY_Y,10))
    c.add(rect(FLY_X-4,FLY_Y-10,8,4),feature='shaft-key')
    for angle in [-113,7,127]:c.add(circle(*polar(FLY_X,FLY_Y,FLY_R-23,angle),3.3))
    out.append(c)
    # Complete steering box attaches to the frame, with both chain paths connected.
    c=component('steering-gear-housing','Shaped source housing on a bolted plate, axle boss, pipe and connected upper shaft.')
    x,y=STEERING_X,STEERING_Y
    c.solid(Path(x-25,y-37).line(x+18,y-44).line(x+33,y-63).line(x+62,y-63)
      .line(x+62,y+30).line(x-24,y+30).curve(x-45,y+24,x-46,y-16,x-25,y-37).vector())
    c.add(Path(x+40,y-63).line(x+40,y+20).line(x,y+20).vector())
    bolts(c,[(x+50,y-47),(x+50,y+17)],3.2)
    c.solid(circle(x,y,23));c.add(circle(x,y,16));c.add(circle(x,y,7))
    c.add(Path(x-17,y+30).line(x-17,y+45).curve(x-17,y+56,x+30,y+54,x+35,y+43).line(x+35,y+30).vector())
    out.append(c)
    # Forward rear-roll scraper, visible in both actual-engine photographs.
    # Its transverse blade collapses to a tangential edge in a true side view.
    # The matching far-wheel assembly is directly behind this one, not offset
    # to suggest a second perspective view.
    c=component('rear-forward-scraper','Forward rear-wheel scraper from the user photograph: frame bearing, hinged arm, adjusting stay and tangential blade. Far scraper coincides in this projection.','terry-reference')
    hinge=(sx(847,185),862)
    blade=polar(REAR_X,REAR_Y,REAR_R+3,165)
    heel=(blade[0]-4,blade[1]-3)
    upper=(sx(863,185),834)
    lower=(hinge[0]+.86*(heel[0]-hinge[0]),hinge[1]+.86*(heel[1]-hinge[1]))
    c.solid(rect(hinge[0]-8,845,15,79,2),'fine','scraper-bearing-mount')
    bolts(c,[(hinge[0],850),(hinge[0],917)],3)
    c.solid(rect(upper[0]-7,upper[1]-8,14,17,2),'fine','scraper-adjuster-mount')
    rod(c,upper,lower,4.2,5,feature='scraper-adjusting-stay')
    rod(c,hinge,heel,8,7,feature='scraper-hinged-arm')
    # A flat scraping edge is tangent to the roll, not a curved shoe. Its
    # straight inner edge meets the rim exactly at the centre of the blade.
    angle=165*pi/180;normal=(cos(angle),sin(angle));tangent=(-sin(angle),cos(angle))
    contact=polar(REAR_X,REAR_Y,REAR_R,165)
    blade_corners=[(contact[0]+u*tangent[0]+v*normal[0],contact[1]+u*tangent[1]+v*normal[1])
                   for u,v in [(-15.5,0),(15.5,0),(15.5,7),(-15.5,7)]]
    c.solid(polygon(blade_corners),'outline','scraper-blade')
    c.solid(capsule(heel,blade,3),'fine','scraper-blade-clevis')
    c.add(circle(*heel,2.6),feature='scraper-clevis-pin')
    out.append(c)
    out.append(wheel('rear-inner-spokes',REAR_X,REAR_Y,REAR_R,REAR_SPOKES,8,inner=True))
    out.append(wheel('rear-driving-roll',REAR_X,REAR_Y,REAR_R,REAR_SPOKES,8))
    c=component('rear-axle-and-drive-pin','Observed pear-shaped drive plate with continuous tangent outline, axle cap and offset pin.')
    x,y=REAR_X,REAR_Y
    pin=(x+25,y+47)
    c.solid(unequal_capsule((x,y),pin,44,23))
    c.add(circle(x,y,29),feature='axle-cap');c.add(circle(x,y,23),feature='axle-shoulder');c.add(circle(x,y,16),feature='axle-centre')
    c.add(circle(*pin,16),feature='drive-pin-outer');c.add(circle(*pin,10),feature='drive-pin-inner')
    hx,hy=pin[0],pin[1]+10
    c.add(circle(hx,hy,2),feature='split-pin-hole')
    c.add(Path(hx,hy).curve(hx-7,hy-5,hx-17,hy-2,hx-13,hy+5)
       .curve(hx-10,hy+11,hx-3,hy+6,hx,hy).line(hx+20,hy-3).vector(),feature='cotter-with-free-tip')
    c.add(line((hx,hy),(hx+20,hy)),feature='cotter-with-free-tip')
    out.append(c)
    c=component('rear-brake-and-scraper','Actual tank-side brake spindle and triangular scraper linkage; continuous pinned tie rod.')
    bx=1355
    c.solid(rect(bx-4.5,723,9,142,1),'fine','brake-spindle')
    # The far end disappears beneath the opaque drum rim in the photograph;
    # place the continuation within that rim, without inventing a visible pin.
    hidden_x=(REAR_X+REAR_R-15)*cos(YAW)+(REAR_OUTER-185)*sin(YAW)
    rod(c,(hidden_x,REAR_Y-20),(bx,865),9,8,feature='brake-link')
    c.solid(rect(bx-18,739,36,9,3),'fine');c.solid(rect(bx-12,732,24,7,2),'fine')
    c.solid(rect(bx-7,723,14,9,1),'fine')
    c.solid(capsule((bx-34,719),(bx+34,719),4),'fine','brake-t-handle')
    for yy in range(777,847,7):c.add(line((bx-4.5,yy+2),(bx+4.5,yy-2)),feature='brake-screw-thread')
    c.solid(polygon([(1341,899),(1402,899),(1376,922)]));c.add(polygon([(1353,905),(1390,905),(1375,916)]))
    rod(c,(1377,919),(1332,964),6,7)
    c.solid(rect(1307,961,32,10,2))
    # The tank-side brake and its link sit behind the driving wheel. The
    # wheel must occlude them, rather than have the rod drawn across its rim.
    out.insert(next(i for i,v in enumerate(out) if v.name=='rear-inner-spokes'),plane(c,185))
    out.append(wheel('front-inner-spokes',FRONT_X,FRONT_Y,FRONT_R,FRONT_SPOKES,16,inner=True))
    out.append(wheel('front-roll-end',FRONT_X,FRONT_Y,FRONT_R,FRONT_SPOKES,16))
    c=component('front-kingpin-and-fork','Shared vertical axis locks the source casting, swivel collars, fork and axle to one continuous joint.')
    x,y=FRONT_X,FRONT_Y
    c.solid(rect(x-28,KINGPIN_Y,56,19,3));c.add(line((x-28,729),(x+28,729)))
    c.solid(rect(x-23,735,46,18,2));c.add(line((x-23,746),(x+23,746)))
    c.solid(Path(x-21,FORK_TOP).line(x+21,FORK_TOP).line(x+21,y-6)
      .curve(x+28,y+23,x-28,y+23,x-21,y-6).close().vector())
    c.add(line((x-15,y),(x-15,FORK_TOP+10),(x+21,FORK_TOP+10)))
    c.solid(circle(x,y,23));c.add(circle(x,y,15));c.add(circle(x,y,8))
    out.append(c)
    c=component('front-scraper','Level beam and tangential scraper bracket, bolted to the fork; source-visible spring at leading edge.')
    x,y=FRONT_X,FRONT_Y
    c.solid(rect(x-166,y-10,326,17,2))
    c.solid(Path(x+70,y-10).curve(x+75,y-35,x+94,y-41,x+120,y-41).line(x+154,y-41).line(x+154,y-10).close().vector())
    c.add(rect(x+147,y-37,8,27,1));bolts(c,[(x-146,y-3),(x+139,y-3)],3)
    c.solid(rect(x-151,y-7,59,11,3))
    for n in range(8):c.add(ellipse(x-145+n*6.6,y-1.5,2.4,5.5),feature='scraper-spring-coil')
    c.add(rect(x-160,y-5,9,7,1));c.add(rect(x-92,y-5,11,7,1),feature='spring-output-clevis')
    out.append(c)
    c=component('near-steering-chain','Lower winding-gear exit to the leading front spring tensioner output eye. The old trailing-bracket eye is removed.','user-detail-correction-v8')
    x,y=FRONT_X,FRONT_Y;bx,by=STEERING_X,STEERING_Y
    near_front,near_gear=SPRING_EYE,(bx+2,by+23)
    c.solid(circle(*near_front,4),'fine','front-spring-chain-eye');c.add(circle(*near_front,1.8),feature='front-spring-eye-pin')
    linked_chain(c,near_front,near_gear,(x+80,y-1),(bx-100,by+75))
    out.append(c)
    c=component('far-steering-chain','Upper far-side winding exit to the opposite leading spring tensioner. The front roll and near-side hardware obscure its forward section.','user-detail-correction-v8')
    # Same longitudinal attachment station on the opposite face: a strict
    # elevation superposes these eyes, rather than routing across the roll.
    far_front,far_gear=near_front,FAR_WINDER
    linked_chain(c,far_front,far_gear,(x+80,y-1),(bx-100,by+28))
    drum=region(circle(x,y,FRONT_R))
    c.strokes=[(part,w,f) for path,w,f in c.strokes for part in visible_paths(path,drum)]
    # Draw behind ALL intervening parts, including the near winding casing.
    out.insert(0,c)
    return out

def model():
    components=side_components()
    return resolve_scene(components), {
        'ground':GROUND,'chimney_top':CHIMNEY_TOP,
        'front_axle':[FRONT_X,FRONT_Y],'front_radius':FRONT_R,
        'rear_axle':[REAR_X,REAR_Y],'rear_radius':REAR_R,
        'flywheel_centre':[FLY_X,FLY_Y],'flywheel_radius':FLY_R,
        'side_yaw_degrees':degrees(YAW),'front_width_reference_units':FRONT_WIDTH,
        'components':[{'id':c.name,'source':c.source,'construction':c.note} for c in components],
        'circular_constraints':[{'component':c.name,**r} for c in components for r in c.circle_constraints],
        'detail_centres':{
            'maker_plate':[sx(625,90),645],
            'valve_cover':[sx(700,90),550],
            'feed_fitting':[sx(FEED_X,90),736],
            'feed_joint':[sx(FEED_X,90),715],
            'steering_wheel':[sx(HANDWHEEL_C[0],100),HANDWHEEL_C[1]],
            'brake_handle':[sx(1355,185),719],
            'brake_spindle_bottom':[sx(1355,185),865],
        },
        'joint_constraints':[
            {'joint':'front casting / kingpin','x':FRONT_X,'y':KINGPIN_Y},
            {'joint':'kingpin collar / fork','x':FRONT_X,'y':FORK_TOP},
            {'joint':'fork / front axle','x':FRONT_X,'y':FRONT_Y},
            {'joint':'steering chains / steering box','x':STEERING_X,'y':STEERING_Y},
            {'joint':'cylinder foot / motion bed','y':700},
        ],
        'chimney_taper':{'upper_width':76,'lower_width':64,'upper_y':355,'lower_y':591},
        'forward_rear_scraper':{'hinge':[sx(847,185),862],'blade_contact_angle_degrees':165,'far_assembly':'coincident and hidden in strict side projection'},
        'far_chain_occlusion':{'centre':[FRONT_X,FRONT_Y],'radius':FRONT_R,'mask':'complete front-roll silhouette, including drum behind spoke openings'},
        'far_chain_return_pipe_occlusion':{'component':'under-boiler-fittings','features':['return-pipe-outer','return-pipe-inner'],'mask':'opaque material between existing tube walls; far chain hidden, near chain retained'},
        'firebox_shoulder':{'upright_x':sx(FIREBOX_SIDE_X,100),'control_housing_rear_x':sx(1177,100),
            'upright_top_y':650,'housing_bottom_y':659,'deck_y':757,'corner_radius':FIREBOX_CORNER_RADIUS,
            'corner_centre':[sx(FIREBOX_SIDE_X,100)+FIREBOX_CORNER_RADIUS,757-FIREBOX_CORNER_RADIUS],
            'construction_path':project_path(firebox_shoulder(),100).to_svg_path_data(),
            'replaces':'Diagonal shoulder from (1030,710) to (1150,757) in the working-photo plane',
            'qualification':'User-requested interpretation of the partly hidden firebox/frame contour.'},
        'feed_fitting_clearance':{'centre':[sx(FEED_X,90),736],'half_width':34/cos(YAW),'half_height':21,'adjacent_band_inside_edges':[sx(707,0),sx(812,0)],'previous_centre':[sx(760,90),736],'previous_half_width':29/cos(YAW)},
        'steering_handwheel':{'major_axis_degrees':degrees(HANDWHEEL_ANGLE),'major_semiaxis_reference_units':HANDWHEEL_MAJOR,'rim_section_radius_reference_units':2.4,'previous_centre':[sx(1223,100),638.5],'visible_face':False,'visible_spokes':0,'projection':'edge-on rim and hub profile perpendicular to shaft; no elliptical face'},
        'steering_column':{'wheel_centre':[sx(HANDWHEEL_C[0],100),HANDWHEEL_C[1]],'far_winder':list(FAR_WINDER),'side':'far','hidden_by':'foreground engine assemblies and complete rear-assembly silhouette','rear_occlusion':{'centre':[REAR_X,REAR_Y],'radius':REAR_R}},
        'steering_chain_routes':{'near':{'front':list(SPRING_EYE),'gear':[STEERING_X+2,STEERING_Y+23],'side':'near','exit':'lower'},'far':{'front':list(SPRING_EYE),'gear':list(FAR_WINDER),'side':'far','exit':'upper'},'front_attachment_kind':'leading spring tensioner output eye','front_attachment_projection':'Matching spring connectors on opposite faces coincide in strict side elevation; the front roll hides the far attachment.'},
        'quadrant_release':{'hinge':[sx(RELEASE_PIVOT[0],100),RELEASE_PIVOT[1]],'moving_grip_tip':[sx(RELEASE_TIP[0],100),RELEASE_TIP[1]],'fixed_grip_tip':[sx(1312.3,100),570.3],'previous_hinge':[sx(1309.2,100),590.0],'previous_moving_grip_tip':[sx(1293.5,100),568.0],'shape':'Longer open V with pivot farther down the main handle'},
        'wheel_spoke_arrangement':{**{key:{'spokes_per_set':n,'inner_phase_offset_degrees':180/n,'outer_root_sweep_degrees':360/n*.22,'inner_root_sweep_degrees':-360/n*.67} for key,n in [('front',FRONT_SPOKES),('rear',REAR_SPOKES)]},'qualification':'User-requested reduced counts: six front and ten rear spokes per set. Staggered inner detail is retained. Counts and hidden construction are illustrative, not asserted from surveyed geometry.'},
        'wheel_rims':{'rear':{'radial_depth':38,'lip_depth':8,'previous_radial_depth':31},'front':{'radial_depth':23,'lip_depth':7,'previous_radial_depth':17},'outer_line_width_mm':.5},
        'canopy':False,'horse_on_engine':False,'projection':'single reconstructed side elevation',
        'merl_role':'drafting and composition reference only; not Aveling geometry',
        'refinement':'Axis-centred small fittings, exact circular bolt geometry, tangent spoke fillets, located steering-wheel support, centred brake handle, continuous pipe elbows and corrected local occlusion.',
    }

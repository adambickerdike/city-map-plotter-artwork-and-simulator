"""Photograph-informed paint/material roles for the frozen revision-14 drawing.

The palette uses actual studio inks, not measured paint colour matches.
Gold is the real 1 mm pen; fine brass engraving remains Black 0.25/0.40 mm.
"""
from city_map_plotter.vector_path import LineSegment, VectorPath

PEN_ORDER=('gold-1','green-0-25','green-0-4','red-0-25','red-0-4','black-0-25','black-0-4','black-0-6')
GOLD_BAND_INDICES=frozenset({150,156,159})
BAND_EDGE_INDICES=frozenset({149,151,152,153,154,155,157,158,160,161})
FRONT_FORK_INDICES=frozenset({570,571})
FRONT_GREEN_HUB_INDICES=frozenset({565,566,567,568,569})
TENDER_BLACK_INDICES=frozenset(range(106,116))
REAR_RED_SCRAPER_INDICES=frozenset({388,389,391,392,393,394,395,396,397})
KNOWN_COMPONENTS=frozenset({'far-steering-chain','far-steering-column','quadrant-lower-blade',
 'rear-frame-and-platform','tender-and-water-box','boiler-and-smokebox','under-boiler-fittings',
 'motion-bed-and-bearings','motion-top-linkage','cylinder-cover','steam-valves-and-lubricator',
 'feed-control-and-pipe','front-casting','chimney','driver-controls','solid-flywheel',
 'steering-gear-housing','rear-forward-scraper','rear-inner-spokes','rear-driving-roll',
 'rear-axle-and-drive-pin','rear-brake-and-scraper','front-inner-spokes','front-roll-end',
 'front-kingpin-and-fork','front-scraper','near-steering-chain'})


def assign(attributes,previous_width):
    role=attributes.get('data-role','');component=attributes.get('data-component','')
    feature=attributes.get('data-feature','');index=int(attributes.get('data-model-path-index','-1'))
    suffix='0-25' if previous_width<=.3 else '0-4' if previous_width<=.4 else '0-6'
    if role=='worksplate-outline':return 'gold-1','brass-worksplate-rim'
    if role!='side-elevation-component':return 'black-'+suffix,'engraving-and-sheet-furniture'
    assert component in KNOWN_COMPONENTS,component
    if index in TENDER_BLACK_INDICES or feature in {'scraper-bearing-mount','scraper-adjuster-mount'}:
        return 'black-'+suffix,'dark-metal-mount-or-fittings'
    if index in GOLD_BAND_INDICES:return 'gold-1','brass-boiler-band-centre'
    if feature=='maker-plate-outer':return 'gold-1','brass-cylinder-worksplate'
    if feature.startswith(('source-fastener-pattern','washer','rim-fastener','rim-bolt')):
        return 'black-'+suffix,'metal-fastener'
    if component in {'rear-inner-spokes','front-inner-spokes'} or feature=='cast-spoke-opening':
        return 'green-'+suffix,'green-cast-wheel-spokes'
    if index in FRONT_GREEN_HUB_INDICES:return 'green-'+suffix,'green-front-hub'
    if index in FRONT_FORK_INDICES:return 'red-'+suffix,'red-brown-front-fork'
    if component=='front-scraper' and feature not in {'scraper-spring-coil','spring-output-clevis'}:
        return 'red-'+suffix,'red-brown-front-scraper'
    if component=='rear-forward-scraper' or index in REAR_RED_SCRAPER_INDICES:
        return 'red-'+suffix,'red-brown-rear-scraper'
    if component=='boiler-and-smokebox' and index not in BAND_EDGE_INDICES:
        return 'green-'+suffix,'green-boiler-cladding'
    if component=='cylinder-cover' and index in {224,225,226,227}:
        return 'green-'+suffix,'green-cylinder-cover'
    if component=='motion-bed-and-bearings' and feature in {'component-outline','edge'}:
        return 'green-'+suffix,'green-motion-bed-casing'
    if component=='tender-and-water-box' and feature in {'component-outline','edge'}:
        return 'green-'+suffix,'green-rear-water-box'
    return 'black-'+suffix,'dark-metal-mechanics'


def adapt_path(path,paint_role):
    if paint_role!='brass-boiler-band-centre':return path,0.0
    assert len(path.segments)==1 and isinstance(path.segments[0],LineSegment)
    assert abs(path.start[0]-path.end[0])<.001
    # A 1 mm gold nib must sit within the existing strap edges. Preserve the
    # centreline, but leave 0.1 mm clear beyond a 0.40 mm boundary's ink edge.
    inset=round(1.0/2+.4/2+.1,3)
    sign=1 if path.end[1]>path.start[1] else -1
    a=(path.start[0],round(path.start[1]+sign*inset,3))
    b=(path.end[0],round(path.end[1]-sign*inset,3))
    return VectorPath(a,(LineSegment(b),)),inset

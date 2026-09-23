"""Paint plan for colour edition 5 of revision 14: pen lines only.

Each part names paper cells by seed points in page millimetres (any point
strictly inside the cell).  Seeds are checked at build time: every seed must
land in a cell and no cell may be claimed twice.  Materials follow the
retained photographs of No. 5499: green cladding, cylinder, motion plate,
tender and spokes; black-painted iron for chimney, smokebox, headstock,
flywheel, firebox, rims and fittings; red-brown fork and scrapers; brass on the
boiler bands, valves, both worksplates and the Invicta horse.  Studio inks
stand in for those paint groups; they are not measured colour matches.

Through the rear wheel, everything in front of the tender's straight front
edge is the black iron of the firebox and frame, drawn in Black lines a shade
lighter than the black wheels; beyond that edge the green tender continues on
its own hatch.  The front roll is a
hollow drum: the spaces between its spokes stay paper.  The wheels, hubs and
flywheel are black iron, drawn as close Black rings.  The worksplate and horse
at the top right are left as black engraving without colour.
"""
from __future__ import annotations

from dataclasses import dataclass

from tools.engineering_source_plates.aveling_5499_colour_v5.inventory import GOLD_PEN
from tools.engineering_source_plates.aveling_5499_colour_v5.painters import Painter

FRONT_ROLL = (67.361, 200.514)
REAR_ROLL = (298.729, 180.165)
FLYWHEEL = (246.398, 121.139)
BOILER_SPAN = (129.535, 175.816)      # cladding top and bottom, y
CHIMNEY_SPAN = (-124.7, -105.6)       # -x, lines run vertically
PEN_ORDER = (GOLD_PEN, 'green-0-25', 'red-0-25', 'grey-0-25', 'black-0-25', 'black-0-4', 'black-0-6', 'black-1')

# Wheel faces divide just outside the spoke openings (whose outer arcs reach
# r = 44.942 and 28.493 mm) and inside the rivet circles.
REAR_WHEEL = dict(name='rear-wheel', centre=REAR_ROLL, split=45.10, opening_component='rear-driving-roll',
                  face_points=[(298.73, 229.67)])
FRONT_WHEEL = dict(name='front-roll', centre=FRONT_ROLL, split=28.65, opening_component='front-roll-end',
                   face_points=[(44.87, 178.02), (89.85, 178.02), (67.36, 232.31)])

# Every spoke of a wheel is ruled with the same number of green lines, edge
# to edge and 0.54 mm apart: five across a rear spoke, four across a front one.
REAR_SPOKE_PATTERN = ('green-0-25',) * 5
FRONT_SPOKE_PATTERN = ('green-0-25',) * 4
RING_PITCH_MM = 0.56                  # grey turned surfaces
BLACK_RING_PITCH_MM = 0.38            # black iron wheels, hubs and flywheel: two thirds ink
# Every gold part is built from several fine Gold lines 0.50 mm apart: with the
# 0.40 mm nib the ink covers 80% and the part reads as solid brass.
GOLD_PITCH_MM = 0.5
GOLD_MIN_LINE_MM = 2.0                # shorter ruled Gold dashes read as stray marks
# Brass boiler bands: each band's wide strip, between the band edge and its
# off-centre inner line, is filled with fine vertical Gold lines; the narrow
# strip stays white as the band's highlight.  All black band lines are kept.
BAND_STRIP_POINTS = ((123.37, 160.0), (166.84, 160.0), (198.15, 160.0))


def flat(pen, coverage, angle=-45.0, shade=None):
    """Parallel hatch; ``shade`` = (pen, approximate pitch) adds lines between."""
    return Painter('flat', dict(pen=pen, angle=angle, coverage=coverage, shade=shade))


def green_flat():
    return flat('green-0-25', (0.36, 0.44))


def iron_flat():
    return flat('grey-0-25', (0.34, 0.42), shade=('black-0-25', 1.4))


def maroon_flat():
    return flat('red-0-25', (0.44, 0.46), shade=('black-0-25', 1.1))


SCRAPER_RED_PITCH_MM = 0.42


def scraper_red(angle=None):
    """Small red-brown scraper parts: close pure-Red lines, so they read red
    between their black outlines (black shade lines would darken them to black)."""
    if angle is None:
        return axial('red-0-25', SCRAPER_RED_PITCH_MM)
    return flat('red-0-25', (0.6, 0.6), angle=angle)


def axial(pen, pitch, angle=None, shade=None, single=False):
    return Painter('axial', dict(pen=pen, pitch=pitch, angle=angle, shade=shade, single=single))


def rings(pen, centre, pitch=RING_PITCH_MM, shade=None):
    """Evenly spaced full circles; ``shade`` = (pen, every nth gap)."""
    return Painter('concentric', dict(pen=pen, centre=centre, pitch=pitch, shade=shade))


def black_rings(centre):
    """Black-painted turned iron: close, even Black circles."""
    return rings('black-0-25', centre, pitch=BLACK_RING_PITCH_MM)


SHADOW_IRON_COVERAGE = 0.36          # Black lines about 0.69 mm apart


def shadow_iron():
    """Black iron seen through the rear wheel: Black lines at the firebox angle,
    lighter than the black wheel rims so the wheel stands in front of it."""
    return flat('black-0-25', (SHADOW_IRON_COVERAGE, SHADOW_IRON_COVERAGE))


def cylinder(pen, angle, span, coverage, shade_tiers=None, facing=1.0):
    return Painter('cylinder', dict(pen=pen, angle=angle, span=span, coverage=coverage,
                                    shade_tiers=shade_tiers, facing=facing))


def ruled(pattern):
    return Painter('ruled', dict(pattern=pattern))


def gold_lines(angle=0.0, pitch=GOLD_PITCH_MM, mask=None, min_length=GOLD_MIN_LINE_MM, open_mm=0.3):
    return Painter('masked', dict(pen=GOLD_PEN, angle=angle, pitch=pitch, mask=mask, min_length=min_length,
                                  open_mm=open_mm))


def gold_columns():
    """Upright brass parts: fine vertical Gold lines across their width."""
    return axial(GOLD_PEN, GOLD_PITCH_MM, angle=90.0)



@dataclass
class Part:
    name: str
    material: str
    painter: Painter
    seeds: tuple = ()
    note: str = ''


# Cells seen between the rear spokes.  The plan gives each one to the black
# iron behind the wheel or to the green tender, by which side of the tender's
# straight front edge it lies.
THROUGH_REAR_WHEEL = (
    (273, 164), (261, 168), (269, 181), (262, 195), (275, 198), (293, 203), (308, 204),
    (287, 218), (300, 220), (312, 218), (287, 147), (273, 148), (298, 147), (291, 164),
    (303, 163), (266, 156), (289, 156), (305, 156), (279, 156), (266, 152), (281.95, 159.6),
    (289, 213), (275, 213), (311, 213), (282, 205), (300, 213), (276, 217), (290, 210),
    (310, 210), (278, 210), (300, 210), (300, 207), (267, 210), (309, 145), (323, 147),
    (315, 156), (303, 153), (286, 139), (296, 139), (296, 136),
    (331, 179), (328, 200), (323, 163), (336, 166), (336, 192), (316, 162), (315, 191),
    (322, 208), (316, 180), (318, 206), (323, 215), (322, 218))
BEHIND_REAR_WHEEL_PART = 'iron-behind-rear-wheel'
TENDER_PART = 'tender'


def parts():
    P = []
    add = lambda *a, **k: P.append(Part(*a, **k))

    # ---- green livery ------------------------------------------------------
    add('boiler-cladding', 'green paint',
        cylinder('green-0-25', 0.0, BOILER_SPAN, (0.20, 0.45), shade_tiers=('black-0-25', [(0.80, 2)])),
        ((145, 157), (182, 163), (208, 159), (132, 130), (145, 175.2), (179, 175.2), (206, 175.2)),
        'Horizontal cylinder: graded lines parallel to the barrel, darkest below.')
    add('cylinder-block', 'green paint', green_flat(), ((152, 108), (140, 123), (143, 124)))
    add('motion-side-plate', 'green paint', green_flat(), ((197, 128), (189, 117), (195, 115)))
    add(TENDER_PART, 'green paint', green_flat(),
        ((366, 172), (366, 200), (377, 158), (375, 182), (374, 208), (353, 167), (330, 154),
         (331, 152), (377, 171), (354, 198)),
        'Includes the open space beside the rear scraper arm, and the tender seen between the '
        'rear spokes beyond its front edge, on one continuous hatch.')
    add(BEHIND_REAR_WHEEL_PART, 'black paint in shadow', shadow_iron(), (),
        'Firebox, horn plates and frame seen between the rear spokes, up to the tender edge.')
    add('rear-inner-spokes', 'green paint', ruled(REAR_SPOKE_PATTERN),
        ((280, 148), (276, 206), (267, 188), (269, 168), (331, 173), (329, 193), (302, 146),
         (296, 212), (319, 156), (315, 206), (311, 173), (313, 181), (305, 166), (295, 165),
         (287, 170), (287, 188), (285, 180), (293, 194)),
        'The staggered inner spokes seen through the openings, ruled like the outer spokes.')
    add('front-inner-spokes', 'green paint', ruled(FRONT_SPOKE_PATTERN),
        ((52, 188), (63, 221), (48, 208), (83, 213), (83, 194), (61, 207), (70, 210)))

    # ---- red-brown frames and scrapers ------------------------------------
    add('front-fork', 'red-brown paint', axial('red-0-25', 0.55, shade=('black-0-25', 1.1)),
        ((68, 180), (63, 181)))
    add('front-fork-bearing', 'red-brown paint', rings('red-0-25', FRONT_ROLL, 0.5, shade=('black-0-25', 2)),
        ((67, 196), (64.52, 204.39)))
    add('front-scraper-bar', 'red-brown paint', axial('red-0-25', SCRAPER_RED_PITCH_MM, angle=0.0),
        ((90, 200), (27, 200), (25.02, 200.12), (49.42, 201.25), (80.19, 201.75)),
        'The scraper bar and the spring bar the steering chain hooks onto: lines along the bar.')
    add('front-scraper-blade', 'red-brown paint', scraper_red(angle=-45.0), ((96, 194), (106, 194)))
    add('rear-forward-scraper', 'red-brown paint', scraper_red(),
        ((235, 189), (245.36, 196.62), (244, 192), (220.29, 179.98),
         (217.64, 177.35), (220.28, 177.16), (218.87, 187.78),
         (221.97, 171.64), (224.24, 172.71), (222.02, 173.86)),
        'Arm, blade, bearing mount and adjuster mount of the scraper ahead of the rear roll.')
    add('rear-scraper', 'red-brown paint', scraper_red(angle=0.0),
        ((356, 201), (365, 192), (361, 192), (363.37, 194.8), (349, 207)),
        'Triangular bracket, pivot boss, arm and blade; the space between arm and spring rod is tender.')

    # ---- black-painted iron -----------------------------------------------
    add('chimney', 'black paint',
        cylinder('grey-0-25', 90.0, CHIMNEY_SPAN, (0.26, 0.45),
                 shade_tiers=('black-0-25', [(0.62, 2), (0.35, 4)]), facing=-1.0),
        ((115, 79), (115, 121)), 'Vertical cylinder, lit from the left.')
    add('chimney-rings', 'black paint', axial('grey-0-25', 0.55, angle=0.0, shade=('black-0-25', 1.4)),
        ((115, 46), (115.14, 47.58), (115, 113), (109, 111)))
    add('smokebox', 'black paint',
        cylinder('grey-0-25', 0.0, BOILER_SPAN, (0.26, 0.45),
                 shade_tiers=('black-0-25', [(0.62, 2), (0.35, 4)])),
        ((114, 157), (105, 163), (114, 175), (123, 177)))
    add('headstock', 'black paint', iron_flat(),
        ((90, 132), (78, 122), (67, 112), (67, 109), (71.34, 109.28)))
    add('front-axle-end', 'black paint', black_rings(FRONT_ROLL), ((67, 197), (67, 203)))
    add('kingpin-collars', 'black paint', axial('grey-0-25', 0.55, angle=0.0, shade=('black-0-25', 1.4)),
        ((67, 144), (67, 146), (67, 149), (67, 151)))
    add('flywheel-disc', 'black paint', black_rings(FLYWHEEL), ((262, 121),),
        'Turned, dished disc: close, even Black circles.')
    add('flywheel-rim', 'black paint', black_rings(FLYWHEEL), ((275, 121),))
    add('flywheel-hub', 'black paint', black_rings(FLYWHEEL), ((240, 121), (250, 121), (246, 121)))
    add('flywheel-edge', 'black paint', black_rings(FLYWHEEL), ((279, 121),))
    add('firebox-and-hornplates', 'black paint', iron_flat(),
        ((237, 175), (238, 198), (226, 169), (227, 196), (240, 156), (226, 157), (219, 157),
         (219, 166), (225, 150), (219, 150), (232, 152), (219, 142), (214, 203), (229, 210),
         (229, 213), (225, 180), (231, 181), (232, 185)))
    add('rear-tyre', 'iron tyre', black_rings(REAR_ROLL), ((245, 180),))
    add('rear-hub', 'black paint', black_rings(REAR_ROLL),
        ((308, 184), (292, 180), (294, 180), (299, 180), (305, 192), (302, 191), (304, 195), (306, 196)))
    add('front-tyre', 'iron tyre', black_rings(FRONT_ROLL), ((39.26, 182.35), (39.14, 218.49), (92, 178)))
    add('steering-gear', 'black paint', iron_flat(),
        ((213, 186), (200, 190), (202, 190), (205, 190), (207, 200), (214, 199), (216.41, 188.89)))
    add('under-boiler-fittings', 'black paint', iron_flat(),
        ((193, 187), (193, 181), (193, 177), (204, 178), (209, 177)))
    add('cylinder-flange', 'black paint', iron_flat(),
        ((143, 138), (149, 138), (155, 138), (161, 138), (138, 138), (166, 138)))
    add('motion-top', 'black paint', iron_flat(),
        ((205, 112), (187, 111), (183, 109), (194, 112), (191.39, 109.22), (177, 113), (178, 106),
         (172, 104), (172, 106), (172, 109), (172, 113), (170, 115), (180, 113), (179.85, 106.13),
         (169, 99)))
    add('motion-bed-underside', 'black paint', iron_flat(),
        ((189, 139), (193, 141), (173, 139), (208, 139), (208, 141), (198, 139), (198, 141)))
    add('feed-fitting', 'black paint', iron_flat(),
        ((174, 147), (177, 147), (187, 147), (190, 148), (182, 148), (184, 147), (180, 147)))
    add('steel-rods', 'bright steel', axial('grey-0-25', 0.8), ((179, 134), (201, 143), (176, 129), (183, 142)))
    add('rear-platform', 'black paint', iron_flat(),
        ((291, 121), (287, 111), (298, 110), (287, 108), (279.08, 110.87), (278.22, 106.52), (287.31, 114.6),
         (299, 107), (296, 106), (372, 152), (350, 152), (351, 154)))
    add('driver-controls', 'black paint', axial('grey-0-25', 0.6, shade=('black-0-25', 1.4)),
        ((300, 94), (299, 101), (326, 131), (338, 107), (337, 115), (335, 123), (334, 129),
         (336, 118), (334, 107), (334.84, 132.93), (309, 125), (311, 122), (307, 116), (307, 124),
         (356, 143), (356, 149), (356, 147), (356, 145), (356, 154), (356, 176), (357.61, 180.76)))
    add('drawbar', 'black paint', iron_flat(), ((388, 197), (381, 198), (394, 196), (378, 198)))

    # ---- brass: fine Gold lines ----------------------------------------------
    add('boiler-bands', 'brass', gold_columns(), BAND_STRIP_POINTS,
        'Fine vertical Gold lines across the wide strip of each band; the narrow strip stays white.')
    add('safety-valves-and-lubricator', 'brass', gold_columns(), ((148, 99), (155, 99), (135, 106)))
    add('whistle', 'brass', gold_columns(), ((152, 99),))
    return P

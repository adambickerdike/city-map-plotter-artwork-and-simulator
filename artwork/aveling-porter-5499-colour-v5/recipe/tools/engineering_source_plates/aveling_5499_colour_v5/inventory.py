"""The pens this edition is drawn for.

The studio template lists Gold only as a broad 1.00 mm nib, includes Grey
pens and lists Black at 0.25/0.40/0.60/1.00 mm; the studio has none of the
broad nibs.  Its gold is a 0.40 mm pen (confirmed by the user on 2026-09-23),
it has no grey pen (2026-09-24) and no thick black pen: its black is most
likely a single 0.25 mm pen (2026-09-24).  This edition's inventory is
therefore the studio template without Grey and with Black 0.25 mm as its only
black, the broad gold replaced by the 0.40 mm gold.  Every gold part is built
from several fine lines, every iron tone is Black hatching, and every heavier
black line is built from Black 0.25 mm strokes (``regions.OUTLINE_WEIGHTS``).
The studio also has a 0.25 mm brown (2026-09-24), which darkens the red parts
toward the engine's red-brown paint.
Like every studio pen here its nib is nominal, not yet measured on paper.  If
the gold pen changes, set ``GOLD_NIB_MM`` and rebuild; every gold line spacing
and clearance follows from it.
"""
from __future__ import annotations

from city_map_plotter.pens import ACTUAL_PEN_INVENTORY, InventoryProvenance, PenInventory, PhysicalPen

GOLD_NIB_MM = 0.4
GOLD_PREVIEW = '#b88900'
FINE_GOLD = PhysicalPen('Gold', GOLD_NIB_MM, preview_color=GOLD_PREVIEW)
GOLD_PEN = FINE_GOLD.identity
BROWN = PhysicalPen('Brown', 0.25, preview_color='#7b4a2e')
BROWN_PEN = BROWN.identity
BLACK_NIB_MM = 0.25

EDITION_PEN_INVENTORY = PenInventory(
    id='studio-pens-fine-black',
    label='Studio pens: Black 0.25 mm the only black, a 0.40 mm gold in place of the broad gold, a 0.25 mm brown, and no grey',
    pens=tuple(pen for pen in ACTUAL_PEN_INVENTORY.pens
               if pen.ink not in {'Gold', 'Grey'}
               and not (pen.ink == 'Black' and pen.nominal_nib_mm != BLACK_NIB_MM)) + (FINE_GOLD, BROWN),
    provenance=InventoryProvenance(
        recorded_by='User', recorded_at='2026-09-24',
        method=f'User confirmed the studio gold pen is {GOLD_NIB_MM:.2f} mm and a brown pen is 0.25 mm, that there is '
               f'no broad gold, no grey and no thick black pen, and that the black pen is most likely '
               f'{BLACK_NIB_MM:.2f} mm; nominal nibs.'),
)
BLACK_PEN = next(pen.identity for pen in EDITION_PEN_INVENTORY.pens if pen.ink == 'Black')
PENS = {pen.identity: pen for pen in EDITION_PEN_INVENTORY.pens}

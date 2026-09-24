"""The pens this edition is drawn for.

The studio template lists Gold only as a broad 1.00 mm nib and includes Grey
pens; the studio has neither.  Its gold is a 0.40 mm pen (confirmed by the user
on 2026-09-23) and it has no grey pen (2026-09-24).  This edition's inventory
is therefore the studio template without Grey, with the broad gold replaced by
the 0.40 mm gold; every gold part is built from several fine lines and every
iron tone is Black hatching.
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

EDITION_PEN_INVENTORY = PenInventory(
    id='studio-pens-fine-gold',
    label='Studio pens: a 0.40 mm gold in place of the broad gold, and no grey',
    pens=tuple(pen for pen in ACTUAL_PEN_INVENTORY.pens if pen.ink not in {'Gold', 'Grey'}) + (FINE_GOLD,),
    provenance=InventoryProvenance(
        recorded_by='User', recorded_at='2026-09-24',
        method=f'User confirmed the studio gold pen is {GOLD_NIB_MM:.2f} mm, with no broad gold pen and no grey pen; nominal nibs.'),
)
PENS = {pen.identity: pen for pen in EDITION_PEN_INVENTORY.pens}

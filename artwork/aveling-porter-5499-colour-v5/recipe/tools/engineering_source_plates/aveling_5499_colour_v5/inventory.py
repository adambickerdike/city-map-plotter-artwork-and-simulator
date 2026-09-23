"""The pens this edition is drawn for.

The studio template lists Gold only as a broad 1.00 mm nib, which the studio
does not have.  The studio's gold is a 0.40 mm pen (confirmed by the user on
2026-09-23), so this edition's inventory is the studio template with the broad
gold replaced by it, and every gold part is built from several fine lines.
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
    label='Studio pens with a fine gold in place of the broad 1.00 mm gold',
    pens=tuple(pen for pen in ACTUAL_PEN_INVENTORY.pens if pen.ink != 'Gold') + (FINE_GOLD,),
    provenance=InventoryProvenance(
        recorded_by='User', recorded_at='2026-09-23',
        method=f'User confirmed the studio gold pen is {GOLD_NIB_MM:.2f} mm and there is no broad gold pen; nominal nib.'),
)
PENS = {pen.identity: pen for pen in EDITION_PEN_INVENTORY.pens}

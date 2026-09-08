"""Keep the promotional map palette and required advertising copy intact."""
# ruff: noqa: E402
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_plot_room_promotion import PACKAGE, S, verify, verify_artwork


@pytest.fixture
def colour_artwork():
    qa = json.loads((PACKAGE / "colour/04-ink-and-river.json").read_text())
    return (
        ET.parse(PACKAGE / "colour/04-ink-and-river.plot.svg").getroot(),
        ET.parse(PACKAGE / qa["input"]["path"]).getroot(),
        ET.parse(ROOT / qa["palette_source"]["path"]).getroot(),
    )


def test_promotional_collection():
    result = verify()
    assert result["promotional_prints"] == 8
    assert result["website"] == "theplotroom.com"


def test_recoloured_roads_are_rejected(colour_artwork):
    root, source, palette = colour_artwork
    root.find(f"{S}g[@id='layer-roads_local']").set("stroke", "#18181b")
    with pytest.raises(ValueError, match="Original city-map colour"):
        verify_artwork(root, source, palette, "colour")


def test_missing_website_is_rejected(colour_artwork):
    root, source, palette = colour_artwork
    root.remove(root.find(f"{S}g[@id='layer-website']"))
    with pytest.raises(ValueError, match="Website is missing"):
        verify_artwork(root, source, palette, "colour")

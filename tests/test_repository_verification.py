"""Regression tests for the production artwork repository gate."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_repository import (  # noqa: E402
    _html_map_provider_copy,
    _visible_map_provider_copy,
)


def test_visible_provider_copy_on_a_drawn_path_is_rejected(tmp_path: Path) -> None:
    svg = tmp_path / "visible.svg"
    svg.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg">
<path d="M 0 0 L 1 1" data-copy="© OpenStreetMap contributors / ODbL-1.0"/>
</svg>
""",
        encoding="utf-8",
    )
    assert _visible_map_provider_copy(svg) == {
        "© OpenStreetMap contributors / ODbL-1.0"
    }


def test_non_plotted_source_metadata_remains_allowed(tmp_path: Path) -> None:
    svg = tmp_path / "metadata.svg"
    svg.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg">
<metadata>{"provider":"OpenStreetMap contributors","license":"ODbL-1.0"}</metadata>
<path d="M 0 0 L 1 1" data-osm-id="123" data-copy="CITY CENTRE"/>
</svg>
""",
        encoding="utf-8",
    )
    assert _visible_map_provider_copy(svg) == set()


def test_literal_visible_provider_text_is_rejected(tmp_path: Path) -> None:
    svg = tmp_path / "literal-text.svg"
    svg.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg">
<text x="1" y="2">ODbL-1.0</text>
</svg>
""",
        encoding="utf-8",
    )
    assert _visible_map_provider_copy(svg) == {
        "literal SVG text provider/licence reference"
    }


def test_public_html_provider_wording_is_rejected(tmp_path: Path) -> None:
    page = tmp_path / "viewer.html"
    page.write_text(
        "<html><head><title>Live OpenStreetMap sample</title></head></html>",
        encoding="utf-8",
    )
    assert _html_map_provider_copy(page) == {"OpenStreetMap"}

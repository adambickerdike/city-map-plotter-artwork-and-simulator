from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import externalize_portfolio_map_attribution as externalize  # noqa: E402


def test_transform_removes_only_drawn_open_map_credit(
    tmp_path: Path, monkeypatch
) -> None:
    svg = tmp_path / "plate.svg"
    png = tmp_path / "plate.png"
    manifest = tmp_path / "plate.plot.json"
    svg.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg">
  <metadata>{"publisher":"OpenStreetMap contributors"}</metadata>
  <g id="layer-pen-black-0-25">
    <title>Step 1: load Black 0.25; plot 2 paths.</title>
    <g id="logical-plate_attribution">
      <path d="M 0,0 L 1,1" data-role="attribution" data-copy="© OpenStreetMap contributors" />
      <path d="M 1,0 L 0,1" data-role="attribution" data-copy="MAPZEN AWS TERRAIN" />
    </g>
  </g>
</svg>
""",
        encoding="utf-8",
    )
    png.write_bytes(b"source-png-fixture")
    manifest.write_text(
        json.dumps(
            {
                "layers": [
                    {
                        "svg_group_id": "layer-pen-black-0-25",
                        "path_count": 2,
                        "logical_layers": ["plate_attribution"],
                    }
                ],
                "rendering": {"visible_attribution": True},
                "plot_summary": {"pen_down_path_count": 2},
                "production_readiness": {
                    "production_ready": False,
                    "blocking_reasons": [],
                },
                "outputs": {"svg": {}, "png": {}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(externalize, "_rasterise_matching_png", lambda *_: None)

    result = externalize.externalize_triplet(svg, png, manifest)

    assert result is not None
    assert result["removed_path_count"] == 1
    transformed_svg = svg.read_text(encoding="utf-8")
    assert "data-copy=\"© OpenStreetMap contributors\"" not in transformed_svg
    assert "MAPZEN AWS TERRAIN" in transformed_svg
    assert "OpenStreetMap contributors" in transformed_svg  # retained metadata
    assert "plot 1 paths" in transformed_svg
    transformed_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    assert transformed_manifest["layers"][0]["path_count"] == 1
    assert transformed_manifest["rendering"]["visible_attribution"] is True
    assert transformed_manifest["rendering"]["on_page_openstreetmap_reference"] is False
    assert transformed_manifest["presentation_transform"]["source_provenance_retained"] is True
    assert externalize.audit_svgs([svg])["status"] == "passed"

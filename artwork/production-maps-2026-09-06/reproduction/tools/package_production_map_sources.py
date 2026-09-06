#!/usr/bin/env python3
"""Attach exact Newcastle inputs, renderer snapshot and rebuild instructions."""
from __future__ import annotations

from importlib.metadata import version
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT.parent / "city-map-plotter-artwork-and-simulator/artwork/production-maps-2026-09-06"
sys.path.insert(0, str(ROOT / "tools"))
from build_production_map_release import record, sha, write_json  # noqa: E402


def main() -> None:
    handoff = OUTPUT / "reproduction"
    renderer = handoff / "renderer/src/city_map_plotter"
    renderer.mkdir(parents=True, exist_ok=True)
    for path in (ROOT / "src/city_map_plotter").glob("*.py"):
        shutil.copy2(path, renderer / path.name)
    (renderer / "data").mkdir(exist_ok=True)
    for name in ("catalog-v1.json", "format-v1.json", "hershey-serif-medium.json",
                 "ranked-universities-2026-v1.json", "rowing-courses-v1.json", "themes-v1.json"):
        shutil.copy2(ROOT / "src/city_map_plotter/data" / name, renderer / "data" / name)
    for directory in ("styles", "sources", "tools"):
        (handoff / directory).mkdir(exist_ok=True)
    shutil.copy2(ROOT / "styles/university-memorabilia-v2.json", handoff / "styles/university-memorabilia-v2.json")
    for name in ("build_production_map_release.py", "package_production_map_sources.py", "validate_format.py", "build_format_spec.py"):
        shutil.copy2(ROOT / "tools" / name, handoff / "tools" / name)
    source = ROOT / "review-output/transit-v2-house/source-pbf/tyne-and-wear-2026-08-07.osm.pbf"
    target = handoff / "sources" / source.name
    shutil.copy2(source, target)
    recipe = {
        "schema_version": 1, "source_date_epoch": 1788652800,
        "header_layout_policy": "city-header-left-stack-v1",
        "python": "3.13.9", "source": record(target, handoff),
        "style": record(handoff / "styles/university-memorabilia-v2.json", handoff),
        "common_arguments": ["--radius-km", "RADIUS", "--preset", "a3-balanced-poster",
            "--orientation", "portrait", "--layers", "roads,water,railways,parks,buildings",
            "--water-fill", "dots", "--landmark-buildings", "--detail-profile", "plotter-faithful",
            "--simplify-mm", "0.04", "--road-style", "centreline", "--extent-fit", "cover",
            "--pen-profile", "actual-pens", "--no-scale-bar", "--no-scale-detail", "--optimise",
            "--physical-audit", "--split-by-pen", "--frame", "--title", "NEWCASTLE",
            "--attribution-mode", "external", "--external-attribution-placement",
            "Accompanying product page, packaging and ATTRIBUTION.md"],
        "editions": [
            {"id": "newcastle-city-a3-portrait", "center": [54.9790, -1.6110], "radius_km": 1.9,
             "arguments": ["--poster-layout", "city-map"]},
            {"id": "newcastle-university-a3-portrait", "center": [54.9805, -1.6152], "radius_km": 1.25,
             "arguments": ["--poster-layout", "university-memorabilia", "--memorabilia-variant",
                           "clean-personalised", "--person-name", "Newcastle University"]},
        ],
        "renderer_files": [record(p, handoff) for p in sorted(renderer.rglob("*")) if p.is_file()],
    }
    write_json(handoff / "newcastle-recipe.json", recipe)
    (handoff / "requirements.txt").write_text("\n".join(f"{name}=={version(name)}" for name in ("numpy", "shapely", "osmium")) + "\n")
    (handoff / "rebuild_newcastle.py").write_text('''#!/usr/bin/env python3
"""Rebuild both Newcastle editions offline into a new output directory."""
import argparse, hashlib, json, os, pathlib, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=pathlib.Path, required=True)
args = parser.parse_args()
if args.output.exists():
    raise SystemExit("Use a new output directory to preserve the released edition.")
recipe = json.loads((HERE / "newcastle-recipe.json").read_text())
for item in [recipe["source"], recipe["style"], *recipe["renderer_files"]]:
    path = HERE / item["path"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        raise SystemExit(f"Pinned input changed: {path}")
env = os.environ.copy()
env["PYTHONPATH"] = str(HERE / "renderer/src")
env["PYTHONDONTWRITEBYTECODE"] = "1"
env["SOURCE_DATE_EPOCH"] = str(recipe["source_date_epoch"])
for edition in recipe["editions"]:
    common = [str(edition["radius_km"]) if a == "RADIUS" else a for a in recipe["common_arguments"]]
    subprocess.run([sys.executable, "-B", "-m", "city_map_plotter", "export",
        "--center", *map(str, edition["center"]), "--input-pbf", str(HERE / recipe["source"]["path"]),
        "--style", str(HERE / recipe["style"]["path"]), "--output", str(args.output / (edition["id"] + ".svg")),
        *common, *edition["arguments"]], env=env, check=True)
''')
    catalog = json.loads((OUTPUT / "catalog.json").read_text())
    evidence = []
    for row in catalog["artifacts"]:
        if not row["id"].startswith("newcastle-"):
            continue
        manifest = json.loads((OUTPUT / row["manifest"]["path"]).read_text())
        svg = ET.parse(OUTPUT / row["svg"]["path"])
        features = []
        for element in svg.iter():
            title = element.find("{http://www.w3.org/2000/svg}title")
            if title is not None and title.text and element.get("data-osm-source-refs"):
                features.append({"name": title.text, "source_refs": element.get("data-osm-source-refs")})
        names = {x["name"] for x in features}
        required = {"Armstrong Building", "Tyne Bridge"}
        if "university" in row["id"]:
            required |= {"Philip Robinson Library", "Great North Museum: Hancock"}
        if not required <= names:
            raise ValueError(f"Missing Newcastle landmarks: {required - names}")
        cleanup = manifest["rendering"]["cartographic_cleanup"]
        evidence.append({"id": row["id"], "svg_sha256": row["svg"]["sha256"],
            "extent": manifest["extent_wgs84"], "required_landmarks": sorted(required),
            "landmarks_verified": True, "feature_names": sorted(names),
            "source_refs": [x for x in features if x["name"] in required],
            "retention": {k: cleanup[k] for k in ("source_features_projected", "retained_road_features",
                "retained_path_features", "omitted_path_features", "omitted_service_features",
                "omitted_parallel_path_features")}, "source": manifest["source"]["provenance"]})
    write_json(handoff / "NEWCASTLE-GEOMETRY-QA.json", {"status": "passed", "editions": evidence})
    # The city-only outputs use the same exact source cohort already carried
    # with the university release in this repository. Preserve the original
    # city contract and binding ledger without duplicating all source bytes.
    city = ROOT.parent / "uk-city-maps-2026/release"
    for name in ("SERIES-CONTRACT.json", "SOURCE-MAP.json", "QA-REPORT.json"):
        shutil.copy2(city / name, handoff / ("UK-CITY-" + name))
    (handoff / "README.md").write_text(
        "# Reproduction inputs\n\nThe Newcastle recipe contains the exact PBF, renderer, style, environment pins, extent and commands. "
        "Install `requirements.txt` in a CPython 3.13 environment and run `python rebuild_newcastle.py --output /path/to/new/output`. "
        "Rebuilding checks every pinned input and uses no network. City and university headers use `city-header-left-stack-v1`: "
        "name at top left, coordinates below, compass centred beside them at the right. "
        "The packaged customer masters additionally pass through `customer-map-copy-v1` "
        "and have their motion regenerated from the final SVG.\n\n"
        "The 423 original source editions and their source contracts remain in `../../latest-map-portfolio-2026-08-18-v3/`. "
        "The 30 UK city source files are that portfolio's `01-university-cities-uk/contracts/university-memorabilia-v2.1/source-snapshots/`. "
        "Their original binding ledger is `UK-CITY-SOURCE-MAP.json`. The standalone Carlisle and Seaton inputs remain in their named sibling releases. "
        "Nothing depends on a cache outside the repository for plotting or rebuilding Newcastle.\n")
    inventory = {"schema_version": 1, "source_files": [record(p, handoff) for p in sorted(handoff.rglob("*")) if p.is_file() and p.name != "SOURCE-INVENTORY.json"]}
    write_json(handoff / "SOURCE-INVENTORY.json", inventory)
    checksum_paths = sorted(p for p in OUTPUT.rglob("*") if p.is_file() and p.name != "CHECKSUMS.sha256")
    (OUTPUT / "CHECKSUMS.sha256").write_text("".join(f"{sha(p)}  {p.relative_to(OUTPUT).as_posix()}\n" for p in checksum_paths))
    print(json.dumps({"source_files": len(inventory["source_files"]), "newcastle_landmarks": "passed", "checksum_files": len(checksum_paths)}))


if __name__ == "__main__":
    main()

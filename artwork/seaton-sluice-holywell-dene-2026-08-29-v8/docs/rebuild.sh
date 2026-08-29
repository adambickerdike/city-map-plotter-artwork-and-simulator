#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
release_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
repo_root=$(CDPATH= cd -- "$release_dir/../.." && pwd)

master="$release_dir/artwork/seaton-sluice-holywell-dene-a3-landscape.svg"
manifest="$release_dir/artwork/seaton-sluice-holywell-dene-a3-landscape.plot.json"
preview="$release_dir/artwork/seaton-sluice-holywell-dene-a3-landscape.png"
source_json="$release_dir/sources/seaton-sluice-holywell-dene-overpass-2026-08-29.json.gz"
pen_dir="$release_dir/artwork/pen-svgs"
viewer="$release_dir/simulation/seaton-sluice-holywell-dene-plotsim.html"
plotjob="$release_dir/simulation/seaton-sluice-holywell-dene.plotjob.json"
machine_profile="$repo_root/plotter-profiles/axidraw-class-simulation-v1.json"

mkdir -p "$release_dir/artwork" "$pen_dir" "$release_dir/simulation" "$release_dir/qa"
cd "$repo_root"

PYTHONPATH=src .venv/bin/python -m city_map_plotter export \
  --bbox -1.5355 55.0618 -1.4585 55.0876 \
  --input-json "$source_json" \
  --output "$master" \
  --manifest "$manifest" \
  --pen-output-dir "$pen_dir" \
  --preset a3-balanced-poster \
  --orientation landscape \
  --poster-layout city-map \
  --layers roads,water,railways,parks,buildings \
  --style styles/university-memorabilia-v2.json \
  --water-fill dots \
  --landmark-buildings \
  --landmark-ref way/60578761 \
  --detail-profile plotter-faithful \
  --simplify-mm 0.04 \
  --road-style centreline \
  --extent-fit contain \
  --pen-profile actual-pens \
  --no-scale-bar \
  --no-scale-detail \
  --optimise \
  --physical-audit \
  --split-by-pen \
  --frame \
  --title 'SEATON SLUICE' \
  --attribution-mode external \
  --external-attribution-placement 'Accompanying map release ATTRIBUTION.md'

inkscape "$master" \
  --export-type=png \
  --export-filename="$preview" \
  --export-dpi=254

PYTHONPATH=src .venv/bin/python tools/build_plotsim_viewer.py \
  "$master" \
  --machine-profile "$machine_profile" \
  --strict-svg \
  --out "$viewer"

PYTHONPATH=src .venv/bin/python tools/plotter_control.py compile \
  "$master" \
  --profile "$machine_profile" \
  --order optimised \
  --out "$plotjob"

PYTHONPATH=src .venv/bin/python tools/validate_format.py "$master"
PYTHONPATH=src .venv/bin/python "$script_dir/verify_release.py" \
  --write-report "$release_dir/qa/QA-REPORT.json"
PYTHONPATH=src .venv/bin/python "$script_dir/build_checksums.py"
PYTHONPATH=src .venv/bin/python "$script_dir/verify_release.py"

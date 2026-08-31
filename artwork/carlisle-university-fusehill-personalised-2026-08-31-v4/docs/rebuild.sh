#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
release_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
repo_root=$(git -C "$release_dir" rev-parse --show-toplevel)

stem="carlisle-university-fusehill-personalised-a3-portrait"
master="$release_dir/artwork/$stem.svg"
manifest="$release_dir/artwork/$stem.plot.json"
preview="$release_dir/artwork/$stem.png"
source_json="$release_dir/sources/carlisle-city-overpass-2026-08-30.json.gz"
pen_dir="$release_dir/artwork/pen-svgs"
viewer="$release_dir/simulation/carlisle-university-fusehill-personalised-plotsim.html"
plotjob="$release_dir/simulation/carlisle-university-fusehill-personalised.plotjob.json"
machine_profile="$release_dir/simulation/axidraw-class-simulation-v1.json"
expected_source_sha="ea43b048925265b1a541502f1bf9cb7ee060df235f828a9b547d442e0173b47a"

# Freeze SVG and manifest generation metadata at this edition's UTC date so
# repeated offline rebuilds are byte-for-byte reproducible.
export SOURCE_DATE_EPOCH=1788134400

actual_source_sha=$(sha256sum "$source_json" | cut -d ' ' -f 1)
if [[ "$actual_source_sha" != "$expected_source_sha" ]]; then
  echo "Pinned Carlisle source hash mismatch." >&2
  exit 1
fi

mkdir -p "$release_dir/artwork" "$pen_dir" "$release_dir/simulation" "$release_dir/qa"
find "$pen_dir" -maxdepth 1 -type f -name "$stem.pen-*.svg" -delete
cd "$repo_root"

PYTHONPATH=src .venv/bin/python -m city_map_plotter export \
  --bbox -2.9460 54.8815 -2.9065 54.9065 \
  --input-json "$source_json" \
  --output "$master" \
  --manifest "$manifest" \
  --pen-output-dir "$pen_dir" \
  --preset a3-balanced-poster \
  --orientation portrait \
  --poster-layout university-memorabilia \
  --memorabilia-variant clean-personalised \
  --layers roads,water,railways,parks,buildings \
  --style styles/university-memorabilia-v2.json \
  --water-fill dots \
  --landmark-buildings \
  --landmark-ref way/566812584 \
  --landmark-ref way/159842515 \
  --landmark-ref way/159842517 \
  --landmark-ref way/159842520 \
  --landmark-ref way/159842521 \
  --landmark-ref way/159842523 \
  --landmark-ref way/552354002 \
  --landmark-ref way/552356816 \
  --landmark-ref way/1105844149 \
  --landmark-ref way/1105844150 \
  --landmark-ref way/1337966863 \
  --landmark-ref way/1337966864 \
  --landmark-ref way/1337966865 \
  --landmark-ref way/1337966866 \
  --landmark-ref way/1337966867 \
  --landmark-ref way/1337966868 \
  --landmark-ref way/1337966869 \
  --landmark-ref way/1337966870 \
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
  --title 'UNIVERSITY OF CUMBRIA' \
  --person-name 'Stuart R. Nelis' \
  --degree 'BSc Applied Psychology' \
  --years '2024' \
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

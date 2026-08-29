#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
python_bin="$repo_root/.venv/bin/python"

if [[ ! -x "$python_bin" ]]; then
  python_bin=$(command -v python3)
fi

exec "$python_bin" "$repo_root/tools/plotter_studio.py" \
  "$repo_root/artwork/seaton-sluice-holywell-dene-2026-08-29-v8/artwork/seaton-sluice-holywell-dene-a3-landscape.svg" \
  --machine-profile "$repo_root/plotter-profiles/axidraw-class-simulation-v1.json" \
  "$@"

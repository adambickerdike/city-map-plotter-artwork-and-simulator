#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
project_python="$repository_root/.venv/bin/python"

if [[ ! -x "$project_python" ]]; then
  printf '%s\n' "Missing $project_python" >&2
  printf '%s\n' "Create it with: python3 -m venv \"$repository_root/.venv\"" >&2
  printf '%s\n' \
    "Then install: \"$repository_root/.venv/bin/python\" -m pip install -r \"$repository_root/requirements-dev.txt\"" >&2
  exit 2
fi

cd "$repository_root"
exec "$project_python" tools/plotter_studio.py \
  examples/augusta-national/augusta-national.svg \
  --machine-profile plotter-profiles/axidraw-class-simulation-v1.json \
  "$@"


#!/usr/bin/env python3
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

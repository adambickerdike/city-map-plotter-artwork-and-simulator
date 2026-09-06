#!/usr/bin/env python3
"""Build the complete customer map collection from the approved local editions.

This creates a new edition, never edits a frozen source, and derives every pen
file and machine job from the final master. Paths in the catalog are relative
to the release, so a Git clone can be imported on another computer.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import copy
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
from city_map_plotter.production_copy import (  # noqa: E402
    INKSCAPE, SVG, clean_customer_copy, geometry_digest, local_name,
)
from city_map_plotter.production_header import (  # noqa: E402
    HEADER_IDS, is_city_map, refresh_city_header,
)
from plotjob import compile_plot_job, load_device_profile, verify_plot_job, write_plot_job  # noqa: E402
from validate_format import validate  # noqa: E402

RELEASE_ID = "production-maps-2026-09-06"
ARTIFACT_VERSION = 1
ARTWORK_REPO = ROOT.parent / "city-map-plotter-artwork-and-simulator"
PORTFOLIO = ARTWORK_REPO / "artwork/latest-map-portfolio-2026-08-18-v3"
CITY_RELEASE = ROOT.parent / "uk-city-maps-2026/release"
NEWCASTLE = ROOT / "review-output/newcastle-production-2026-09-06"
PROFILE = ARTWORK_REPO / "plotter-profiles/axidraw-class-simulation-v1.json"

for prefix, uri in (("", SVG), ("inkscape", INKSCAPE),
                    ("sodipodi", "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"),
                    ("xlink", "http://www.w3.org/1999/xlink")):
    ET.register_namespace(prefix, uri)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def record(path: Path, output: Path) -> dict:
    return {"path": path.relative_to(output).as_posix(), "sha256": sha(path), "bytes": path.stat().st_size}


def inventory() -> list[dict]:
    rows = []
    catalog = json.loads((PORTFOLIO / "catalog.json").read_text())
    if len(catalog["artifacts"]) != 423:
        raise ValueError("The approved portfolio must contain exactly 423 masters")
    for row in catalog["artifacts"]:
        for kind in ("svg", "png", "manifest"):
            path = PORTFOLIO / row[kind]["path"]
            if not path.is_file() or sha(path) != row[kind]["sha256"]:
                raise ValueError(f"Changed or missing approved input: {path}")
        rows.append({
            "source_svg": str(PORTFOLIO / row["svg"]["path"]),
            "domain": row["domain"], "id": row["artifact_id"] + ("--" + row["format_id"] if row["domain"] == "05-rowing-races" else ""), "title": row["title"],
        })
    city_svgs = sorted((CITY_RELEASE / "uk-times-good-university-guide-2026-top-30").glob("[0-9][0-9][0-9]-*.svg"))
    city_svgs = [x for x in city_svgs if ".pen-" not in x.name]
    if len(city_svgs) != 30:
        raise ValueError("The approved UK city collection must contain exactly 30 masters")
    for svg in city_svgs:
        m = json.loads(svg.with_suffix(".plot.json").read_text())
        rows.append({"source_svg": str(svg), "domain": "08-city-maps-uk", "id": svg.stem, "title": m["title"]})
    for slug, domain in (("seaton-sluice-holywell-dene-2026-08-29-v8", "03-hiking-maps"),
                         ("carlisle-university-fusehill-personalised-2026-08-31-v4", "01-university-cities-uk")):
        sources = list((ARTWORK_REPO / "artwork" / slug / "artwork").glob("*.svg"))
        for svg in sources:
            if ".pen-" in svg.name:
                continue
            m = json.loads(svg.with_suffix(".plot.json").read_text())
            rows.append({"source_svg": str(svg), "domain": domain, "id": svg.stem, "title": m["title"]})
    newcastle = sorted(NEWCASTLE.glob("newcastle-*.svg"))
    newcastle = [p for p in newcastle if ".pen-" not in p.name]
    if not any("city" in p.name for p in newcastle) or not any("university" in p.name for p in newcastle):
        raise ValueError("Both Newcastle city and university masters are required")
    for svg in newcastle:
        rows.append({"source_svg": str(svg), "domain": "01-university-cities-uk" if "university" in svg.name else "08-city-maps-uk", "id": svg.stem, "title": "NEWCASTLE UNIVERSITY" if "university" in svg.name else "NEWCASTLE"})
    keys = [(r["domain"], r["id"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate production artifact IDs")
    return rows


def pen_splits(root: ET.Element, destination: Path, output: Path, job: dict) -> list[dict]:
    """Keep every original group for the selected physical pen, in page units."""
    files = []
    ids = {g["pen"]["id"] for g in job["pen_groups"]}
    svg_ids = {g.get("data-plot-pen-id") for g in root if local_name(g) == "g" and g.get("data-plot-pen-id")}
    if ids != svg_ids:
        raise ValueError(f"Job/SVG pen mismatch: {ids ^ svg_ids}")
    for index, group in enumerate(job["pen_groups"], 1):
        pen_id = group["pen"]["id"]
        split = copy.deepcopy(root)
        for child in list(split):
            if local_name(child) == "g" and child.get("data-plot-pen-id") not in (None, pen_id):
                split.remove(child)
        # Prove the split contains the same drawables as that pen in the master.
        expected = ET.Element(root.tag, root.attrib)
        for child in root:
            if local_name(child) != "g" or child.get("data-plot-pen-id") in (None, pen_id):
                expected.append(child)
        if geometry_digest(expected) != geometry_digest(split):
            raise ValueError(f"Pen split geometry changed: {pen_id}")
        path = destination.parent / "pens" / f"{destination.stem}.pen-{index:02d}-{pen_id}.svg"
        path.parent.mkdir(exist_ok=True)
        ET.ElementTree(split).write(path, encoding="utf-8", xml_declaration=True)
        files.append({"step": index, "pen_id": pen_id, **record(path, output)})
    return files


def _refresh_layer_counts(root: ET.Element, manifest: dict) -> None:
    groups = {e.get("id"): e for e in root.iter() if local_name(e) == "g"}
    layers = []
    for layer in manifest["layers"]:
        group = groups.get(layer.get("svg_group_id"))
        if group is None:
            continue
        count = sum(local_name(e) == "path" for e in group.iter())
        if not count:
            continue
        layer["path_count"] = count
        if "logical_layers" in layer:
            layer["logical_layers"] = [s for s in layer["logical_layers"] if any(e.get("data-logical-layer") == s for e in group.iter())]
        for title in group.findall(f"{{{SVG}}}title"):
            if title.text:
                title.text = re.sub(r"plot \d+ paths?", f"plot {count} paths", title.text)
        layers.append(layer)
    manifest["layers"] = layers


def refresh_presentation_metadata(manifest: dict) -> None:
    original = manifest.get("presentation_transform")
    if original and original.get("id") != "customer-map-copy-v1":
        manifest["upstream_edition"]["original_presentation_transform"] = original
    manifest["presentation_transform"] = {
        "schema_version": 1, "id": "customer-map-copy-v1",
        "scope": "customer-artwork-and-software-import",
        "remaining_visible_attribution_copy_values": [],
        "external_attribution_placement": "Accompanying ATTRIBUTION.md, product page and packaging",
        "source_provenance_retained": True, "source_licence_metadata_retained": True,
        "pen_files_regenerated": True, "machine_metrics_regenerated": True,
        "master_svg_sha256": manifest["outputs"]["svg"]["sha256"],
        "plot_job_sha256": manifest["outputs"]["plot_job"]["sha256"],
        "non_furniture_geometry_sha256": manifest["customer_copy"]["non_furniture_geometry_sha256"],
    }


def build_one(row: dict, output_text: str) -> dict:
    output = Path(output_text)
    folder = output / row["domain"] / row["id"]
    marker = folder / "artifact.json"
    source = Path(row["source_svg"])
    source_manifest = source.with_suffix(".plot.json")
    manifest = json.loads(source_manifest.read_text())
    artifact_version = (4 if is_city_map(manifest) else
                        2 if row["domain"] == "04-marathon-courses" else ARTIFACT_VERSION)
    if marker.is_file() and json.loads(marker.read_text()).get("artifact_version") == artifact_version:
        saved = json.loads(marker.read_text())
        if saved["source"]["svg_sha256"] != sha(Path(row["source_svg"])):
            raise ValueError("Resume input changed")
        for f in [saved[k] for k in ("svg", "png", "manifest", "job")] + saved["pen_files"]:
            if sha(output / f["path"]) != f["sha256"]:
                raise ValueError(f"Changed resumed artifact: {f['path']}")
        return saved
    folder.mkdir(parents=True, exist_ok=True)
    root = ET.parse(source).getroot()
    map_geometry = geometry_digest(root, exclude_furniture=True, exclude_group_ids=HEADER_IDS)
    header = refresh_city_header(root, manifest)
    customer_copy = clean_customer_copy(root, manifest)
    if map_geometry != geometry_digest(root, exclude_furniture=True, exclude_group_ids=HEADER_IDS):
        raise ValueError("Presentation update changed map linework")
    if header:
        header["map_geometry_sha256"] = map_geometry
    _refresh_layer_counts(root, manifest)
    svg = folder / source.name
    ET.ElementTree(root).write(svg, encoding="utf-8", xml_declaration=True)
    raw_profile, machine, _ = load_device_profile(PROFILE)
    job = compile_plot_job(svg, machine, order="optimised", strict_svg=True,
                          profile_binding={"id": raw_profile["id"], "sha256": sha(PROFILE)})
    verify_plot_job(job)
    length_by_layer: Counter = Counter()
    for pen_group in job["pen_groups"]:
        for stroke in pen_group["strokes"]:
            length_by_layer[stroke["layer"]] += stroke["length_mm"]
    for layer in manifest["layers"]:
        label = layer.get("svg_layer_label", "")
        if label in length_by_layer:
            layer["pen_down_distance_mm"] = round(length_by_layer[label], 4)
    machine_errors = [x for x in job["safety"]["findings"] if x["severity"] == "error"]
    if machine_errors:
        raise ValueError(f"Machine geometry errors: {machine_errors}")
    job_path = folder / (svg.stem + ".plotjob.json")
    write_plot_job(job_path, job)
    pen_files = pen_splits(root, svg, output, job)
    png = svg.with_suffix(".png")
    result = subprocess.run([
        "inkscape", str(svg), "--export-type=png", "--export-area-page",
        "--export-dpi=254", "--export-background=white", "--export-background-opacity=255",
        f"--export-filename={png}",
    ], capture_output=True, text=True)
    if result.returncode or not png.is_file():
        raise ValueError(f"Preview render failed: {result.stderr}")
    # Make thumbnail separately: the gallery must not download every full plate.
    thumbnail = folder / "thumbnail.png"
    result = subprocess.run([
        "inkscape", str(svg), "--export-type=png", "--export-area-page",
        "--export-width=480", "--export-background=white", "--export-background-opacity=255",
        f"--export-filename={thumbnail}",
    ], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"Thumbnail render failed: {result.stderr}")
    manifest["upstream_edition"] = {
        "svg_sha256": sha(source), "manifest_sha256": sha(source_manifest),
        "path": os.path.relpath(source, ARTWORK_REPO),
        "original_plot_summary": manifest.get("plot_summary"),
        "original_pen_sequence": manifest.get("pen_sequence"),
        "original_outputs": manifest.get("outputs"),
    }
    manifest["pen_files"] = pen_files
    manifest["outputs"] = {"svg": record(svg, output), "png": record(png, output), "plot_job": record(job_path, output), "pen_files": pen_files}
    manifest["raster_exports"] = [{**record(png, output), "dpi": 254}]
    manifest["plot_summary"] = {"stroke_count": job["geometry"]["stroke_count"], "vertex_count": job["geometry"]["vertex_count"], "pen_loads": len(job["pen_groups"]), "motion_stats": job["stats"], "basis": "Final serialized plot job"}
    manifest["pen_sequence"] = [{
        "step": i, "pen_id": g["pen"]["id"], "pen": g["pen"]["label"],
        "nib_mm": g["pen"]["nib_mm"], "path_count": len(g["strokes"]),
        "pen_down_mm": round(sum(s["length_mm"] for s in g["strokes"]), 4),
        "layers": sorted({s["layer"] for s in g["strokes"]}),
    } for i, g in enumerate(job["pen_groups"], 1)]
    readiness = manifest.setdefault("production_readiness", {})
    for field in ("blocking_reasons", "blockers"):
        if isinstance(readiness.get(field), list):
            readiness[field] = [x for x in readiness[field] if "presentation attribution transform requires regenerated" not in str(x)]
    manifest["digital_release"] = {"id": RELEASE_ID, "software_import_ready": True,
        "customer_copy_clean": True, "plot_job_regenerated": True,
        "physical_execution_allowed": job["safety"]["execution_allowed"]}
    refresh_presentation_metadata(manifest)
    manifest_path = svg.with_suffix(".plot.json")
    write_json(manifest_path, manifest)
    spec = json.loads((ROOT / "docs/format/format-v1.json").read_text())
    report = validate(svg, spec, None)
    if not report.passed:
        write_json(folder / "format-failures.json", report.failures)
        raise ValueError(f"Format validation: {report.failures[:8]}")
    result = {k: row[k] for k in ("id", "domain", "title")}
    if row["title"] == row["id"]:
        result["title"] = manifest["title"]
    result.update({
        "artifact_version": artifact_version,
        "format_id": manifest["page"].get("format_id", manifest["page"]["paper"].lower() + "-" + manifest["page"]["orientation"]),
        "source": manifest["upstream_edition"],
        "svg": record(svg, output), "png": record(png, output), "thumbnail": record(thumbnail, output),
        "manifest": record(manifest_path, output), "job": record(job_path, output), "pen_files": pen_files,
        "qa": {"format": "passed", "format_checks": report.checks, "format_warnings": report.warnings,
               "density_advisories": report.advisories, "strict_svg_preflight": "passed", "job_integrity": "passed",
               "pen_geometry_parity": "passed", "customer_copy": "passed", "map_geometry_unchanged": True,
               **({"city_header": header} if header else {})},
        "visible_copy": customer_copy["final_visible_copy"],
        "stroke_count": job["geometry"]["stroke_count"], "pen_count": len(pen_files),
        "physical_execution_allowed": job["safety"]["execution_allowed"],
    })
    write_json(marker, result)
    return result


def gallery(output: Path, rows: list[dict]) -> None:
    labels = {"01-university-cities-uk": "UK universities", "02-university-cities-us": "US universities",
              "03-hiking-maps": "Hikes", "04-marathon-courses": "Marathons", "05-rowing-races": "Rowing",
              "06-f1-courses": "Formula 1", "07-golf-courses": "Golf", "08-city-maps-uk": "UK cities"}
    cards = []
    for row in sorted(rows, key=lambda r: ("newcastle" not in r["id"], r["domain"], r["title"], r["id"])):
        esc = html.escape
        cards.append(f'<article data-family="{row["domain"]}" data-search="{esc((row["title"]+" "+row["id"]).lower())}">'
                     f'<a href="{esc(row["png"]["path"])}"><img loading="lazy" src="{esc(row["thumbnail"]["path"])}" alt="{esc(row["title"])}"></a>'
                     f'<h2>{esc(row["title"])}</h2><p>{labels[row["domain"]]} · {esc(row["format_id"])}</p>'
                     f'<nav><a href="{esc(row["svg"]["path"])}" download>SVG</a> <a href="{esc(row["png"]["path"])}" download>PNG</a> '
                     f'<a href="{esc(row["job"]["path"])}" download>Plot job</a></nav></article>')
    options = ''.join(f'<option value="{key}">{value}</option>' for key, value in labels.items())
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Map collection</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f3ed;color:#202b2b;font:16px system-ui}header,main{max-width:1600px;margin:auto;padding:32px}h1{font:48px Georgia;margin:0 0 14px}header p{color:#52605b}input,select{padding:12px;border:1px solid #bbb;background:#fff;border-radius:4px;margin:4px}input{width:min(500px,95%)}#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:24px}article{background:white;padding:18px;border:1px solid #deded5}article[hidden]{display:none}img{width:100%;height:320px;object-fit:contain}h2{font:21px Georgia;line-height:1.2}article p{font-size:13px;color:#52605b}a{color:inherit}nav{display:flex;gap:18px;font-size:14px}nav a{text-underline-offset:4px}footer{padding:32px}
</style><header><h1>Map collection</h1><p>Geographic prints, university keepsakes and remarkable courses.</p>
<input id="search" type="search" placeholder="Find a city, university or course" aria-label="Search maps"><select id="family" aria-label="Map family"><option value="">All collections</option>OPTIONS</select><p id="count"></p></header><main id="grid">CARDS</main>
<script>const cards=[...document.querySelectorAll('article')];function filter(){const q=document.querySelector('#search').value.toLowerCase().trim(),f=document.querySelector('#family').value;let n=0;cards.forEach(c=>{c.hidden=!!((f&&c.dataset.family!==f)||(q&&!c.dataset.search.includes(q)));if(!c.hidden)n++});document.querySelector('#count').textContent=n+' maps'}document.querySelector('#search').addEventListener('input',filter);document.querySelector('#family').addEventListener('change',filter);filter();</script></html>'''
    (output / "index.html").write_text(page.replace("OPTIONS", options).replace("CARDS", ''.join(cards)))


def finalize(output: Path, rows: list[dict]) -> None:
    rows.sort(key=lambda x: (x["domain"], x["id"]))
    counts = dict(sorted(Counter(r["domain"] for r in rows).items()))
    write_json(output / "catalog.json", {"schema_version": 1, "release_id": RELEASE_ID,
        "artifact_count": len(rows), "counts_by_domain": counts, "artifacts": rows})
    gallery(output, rows)
    qa = {"status": "passed", "artifacts": len(rows), "counts_by_domain": counts,
          "format_checks": sum(r["qa"]["format_checks"] for r in rows),
          "pen_files": sum(r["pen_count"] for r in rows), "strict_imports": len(rows),
          "customer_copy_failures": 0, "changed_map_geometry": 0,
          "city_headers_verified": sum("city_header" in r["qa"] for r in rows),
          "physical_execution_allowed": all(r["physical_execution_allowed"] for r in rows)}
    write_json(output / "QA-REPORT.json", qa)
    (output / "ATTRIBUTION.md").write_text(
        "# Accompanying source credits\n\nMap data © OpenStreetMap contributors, ODbL 1.0: https://www.openstreetmap.org/copyright\n\n"
        "Terrain credits, route evidence and individual source terms are retained in each plot manifest and the original edition contracts. "
        "Keep this notice and each product's applicable source credits on its product page and accompanying packaging. "
        "The artwork is decorative and is not a navigation or surveying product. Historical circuit reference dates and source limitations remain in the manifests.\n\n"
        "".join(f"- [{r['title']} — {r['id']}]({r['manifest']['path']})\n" for r in rows))
    (output / "README.md").write_text(
        f"# Map collection — {RELEASE_ID}\n\n{len(rows)} final digital masters with matching 254 DPI PNGs, thumbnails, per-pen SVGs and verified plot jobs. "
        "Open [the gallery](index.html) or use [catalog.json](catalog.json) to select a map.\n\n"
        "All map and course geometry is preserved from the approved editions. Source credits and process notes accompany the files rather than being drawn. "
        "Newcastle is newly rendered from the pinned Tyne and Wear extract with the full plotter-faithful house recipe.\n\n"
        "All 84 city-style headers place the name at the top left, coordinates underneath, and a vertically centred compass in the right column. "
        "This includes UK and US university editions, UK city editions and Seaton. Map linework and footer personalisation are preserved.\n\n"
        "Import the master SVG into the plotting studio. Use the ordered files under each map's `pens/` directory for separate physical pen runs. "
        "Every path in the catalog is relative to this directory. The `.plotjob.json` records the final master's SHA-256 and the exact serialized motion.\n\n"
        "Run `python scripts/verify_production_maps.py --full` from the repository root after cloning with Git LFS. "
        "Digital format/import QA passes; the supplied machine profile remains a simulation profile. Physical pen/stock calibration and a plotted proof remain required, "
        "as do any unresolved source-specific release conditions recorded in the original manifests.\n")
    (output / "SOFTWARE_IMPORT.md").write_text(
        "# Plotting software import\n\n1. Clone the artwork repository with Git LFS and run `git lfs pull`.\n"
        "2. Run `python scripts/verify_production_maps.py --full`.\n"
        f"3. Open `artwork/{RELEASE_ID}/index.html` and choose the master SVG.\n"
        "4. Launch `python tools/plotter_studio.py path/to/map.svg --machine-profile plotter-profiles/axidraw-class-simulation-v1.json`, "
        "or drag the SVG into the running studio.\n"
        "5. The corresponding `.plotjob.json` can be inspected with `python tools/plotter_control.py inspect path/to/map.plotjob.json`.\n\n"
        "Use the master or its ordered pen splits, not a thumbnail or contact sheet. The bundled jobs are ready for software import and simulation; "
        "compile for your calibrated device profile before physical execution.\n")
    checksums = [f"{sha(p)}  {p.relative_to(output).as_posix()}" for p in sorted(output.rglob("*")) if p.is_file() and p.name != "CHECKSUMS.sha256"]
    (output / "CHECKSUMS.sha256").write_text("\n".join(checksums) + "\n")
    print(json.dumps(qa, indent=2), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ARTWORK_REPO / "artwork" / RELEASE_ID)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--match")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows = inventory()
    if args.match:
        rows = [r for r in rows if args.match in r["source_svg"]]
    if args.limit:
        rows = rows[:args.limit]
    print(f"Building {len(rows)} maps in {output}", flush=True)
    results, failures = [], []
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(build_one, row, str(output)): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                results.append(future.result())
                print(f"PASS {len(results)}/{len(rows)} {row['id']}", flush=True)
            except Exception as exc:
                failures.append({"id": row["id"], "error": str(exc)})
                print(f"FAIL {row['id']}: {exc}", flush=True)
    write_json(output / "BUILD-FAILURES.json", failures)
    if failures:
        return 1
    if not args.limit and not args.match:
        finalize(output, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

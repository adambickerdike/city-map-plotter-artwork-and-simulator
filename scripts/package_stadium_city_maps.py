#!/usr/bin/env python3
"""Assemble the reviewed city-style stadium exports, source ledger and public gallery."""

from pathlib import Path
import hashlib
import json
import html
import subprocess
import sys
import shutil
import concurrent.futures
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "artwork/uk-stadiums-city-style-2026-09-08"
OLD = ROOT / "artwork/uk-stadiums-overhead-2026-09-08"


def sha(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def dump(p, x):
    p.write_text(json.dumps(x, ensure_ascii=False, indent=2) + "\n")


rows = [
    json.loads((P / r["league"] / r["id"] / "record.json").read_text())
    for r in json.loads((P / "reproduction/stadiums.json").read_text())
]
assert len(rows) == 44
catalog = {
    "schema_version": 1,
    "release_id": P.name,
    "title": "UK football stadiums — city collection style",
    "source_stadium_release": OLD.name,
    "counts": {"premier_league": 20, "championship": 24, "total": 44},
    "paper": "A3 portrait",
    "header_policy": "city-header-left-stack-v1 with city before coordinates",
    "map_style": "university-memorabilia-v2",
    "detail_profile": "plotter-faithful",
    "zoom_out_width_factor": 1.15,
    "stadium_geometry_policy": "All supplied native shell and detail paths retained in geographic registration. Native cubic controls are preserved.",
    "physical_execution_allowed": False,
    "stadiums": rows,
}
dump(P / "catalog.json", catalog)
font = ImageFont.truetype("/usr/share/fonts/noto/NotoSans-Regular.ttf", 19)
titlefont = ImageFont.truetype("/usr/share/fonts/noto/NotoSans-Bold.ttf", 28)
for league, name in [
    ("premier_league", "Premier League selection"),
    ("championship", "Championship selection"),
]:
    group = [r for r in rows if r["league"] == league]
    cols = 5 if len(group) == 20 else 6
    sheet = Image.new("RGB", (cols * 275 + 40, 4 * 440 + 100), "#eeede9")
    d = ImageDraw.Draw(sheet)
    d.text((25, 22), name + " / A3 city collection", fill="#26333d", font=titlefont)
    for i, r in enumerate(group):
        image = Image.open(P / r["files"]["thumbnail"]["path"])
        image.thumbnail((250, 355))
        x = 25 + (i % cols) * 275
        y = 85 + (i // cols) * 440
        sheet.paste(image, (x, y))
        label = r["name"]
        words = label.split()
        lines = [""]
        for word in words:
            if d.textlength((lines[-1] + " " + word).strip(), font=font) > 250:
                lines.append(word)
            else:
                lines[-1] = (lines[-1] + " " + word).strip()
        d.multiline_text(
            (x, y + 365), "\n".join(lines), fill="#26333d", font=font, spacing=3
        )
    sheet.save(P / (league + "-contact-sheet.png"))

style = """*{box-sizing:border-box}body{font-family:system-ui,sans-serif;background:#f0efeb;color:#25313b;margin:0}main{max-width:1500px;margin:auto;padding:40px 26px}h1{font-weight:600;letter-spacing:-1px;font-size:36px}p{line-height:1.6}input,select{padding:12px 16px;border:1px solid #bdc4c4;border-radius:4px;font:inherit;background:white}input{min-width:300px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:32px 24px;margin-top:30px}article[hidden]{display:none}.preview{width:100%;display:block;box-shadow:0 4px 14px #26333d17}h2{font-size:17px;margin:14px 0 5px}article p{font-size:14px;color:#64717a;margin:5px 0 12px}nav{display:flex;gap:12px;flex-wrap:wrap}a{color:#244e6a;text-underline-offset:4px}article nav{font-size:13px}footer{border-top:1px solid #bfc6c5;margin-top:42px;padding-top:20px;font-size:13px}@media(max-width:1050px){.grid{grid-template-columns:repeat(3,1fr)}}@media(max-width:750px){.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:480px){.grid{grid-template-columns:1fr}input{min-width:0;width:100%}}"""
cards = []
for r in rows:
    f = r["files"]
    esc = html.escape
    cards.append(
        f'''<article data-search="{esc((r["name"] + " " + r["city"]).casefold())}" data-league="{r["league"]}"><a href="{f["png"]["path"]}"><img class="preview" loading="lazy" src="{f["thumbnail"]["path"]}" alt="{esc(r["name"])} stadium and city map"></a><h2>{esc(r["name"])}</h2><p>{esc(r["city"])}</p><nav><a href="{f["svg"]["path"]}">A3 SVG</a><a href="{f["png"]["path"]}">PNG</a><a href="{f["detail"]["path"]}">Stadium detail</a><a href="{f["plotjob"]["path"]}">Plot job</a></nav></article>'''
    )
page = (
    """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Football stadiums — The Plot Room city collection</title><style>"""
    + style
    + """</style><main><h1>Football stadiums / the city collection</h1><p>44 grounds in the same detailed map style as our city and university prints.<br>Stadium name, city, coordinates and compass above a wider view of the neighbourhood.</p><nav><a href="premier_league-contact-sheet.png">Premier League overview</a><a href="championship-contact-sheet.png">Championship overview</a><a href="simulation/premier_league.html">Premier League plot preview</a><a href="simulation/championship.html">Championship plot preview</a><a href="SOFTWARE_IMPORT.md">Import guide</a></nav><p><input id="search" placeholder="Find a stadium or city" aria-label="Find a stadium or city"><select id="league" aria-label="Filter collection"><option value="">All 44 grounds</option value="premier_league">Premier League selection</option><option value="championship">Championship selection</option></select></p><div class="grid">"""
    + "".join(cards)
    + """</div><footer><p>A3 portrait, 297 × 420 mm. Full previews are 254 DPI. Print at actual size / 100%.</p><p>Venue geometry retains the source edition's reference-era details. The original standalone stadium drawings and their review notes remain in the source handoff.</p><a href="ATTRIBUTION.md">Source credits</a> · <a href="README.md">File index</a> · <a href="catalog.json">Catalogue</a></footer></main><script>function filter(){const q=document.querySelector('#search').value.toLowerCase(),l=document.querySelector('#league').value;document.querySelectorAll('article').forEach(a=>a.hidden=!a.dataset.search.includes(q)||(l&&a.dataset.league!==l));}document.querySelector('#search').addEventListener('input',filter);document.querySelector('#league').addEventListener('change',filter);</script></html>"""
)
(P / "index.html").write_text(page)
md = """# Football stadiums — city collection style

All **44 uploaded grounds** now use the city/university print treatment: **A3 portrait**, serif stadium name, city and coordinates beneath, and the diamond compass alongside. The map sits below the header.

The mapped width is **15% wider** than the supplied city maps, with additional north/south context from the portrait layout. All original geographic bounds remain visible. The neighbourhood uses the frozen `university-memorabilia-v2` palette, full `plotter-faithful` streets and paths, blue water banks and dots, green park outlines and purple landmarks. The authored stadium is distinguished in Black 0.40/0.25 mm.

Every supplied stadium shell/detail path is retained, including native cubic curves, pitch placement and the latest Etihad v10 detail. Geographic checks compare every endpoint and cubic control point against the original overlay. Context is cut away beneath sourced stadium surfaces, with the removed/clipped features recorded in each manifest. The original 44-ground standalone architecture handoff remains unchanged.

[Visual gallery](index.html) · [Import guide](SOFTWARE_IMPORT.md) · [Catalogue](catalog.json) · [Source credits](ATTRIBUTION.md)

[Premier League overview](premier_league-contact-sheet.png) · [Championship overview](championship-contact-sheet.png)

| Stadium | City | A3 map | Preview | Detail |
|---|---|---|---|---|
"""
for r in rows:
    f = r["files"]
    md += f"| {r['name']} | {r['city']} | [SVG]({f['svg']['path']}) | [PNG]({f['png']['path']}) | [PNG]({f['detail']['path']}) |\n"
md += """
## Checks and physical detail

Each master has a strict SVG preflight, verified A3 layout and header bounds, final pen splits, a SHA-bound plot job, and a 254 DPI PNG. Source hashes, extent expansion and **100% native stadium path/control-point coverage** are checked independently by `scripts/verify_stadium_city_maps.py`.

Some authored roof details are shorter than three nominal nib widths. They are preserved because the request was to retain stadium detail; these exact exceptions remain in `QA.json` and the manifest instead of being silently deleted or declared physically certified. This is a digital artwork and simulation release using nominal pens. It is not permission to execute a physical plotter without its normal calibration and proofing.

The maps use the same dated Great Britain source cohort as the Newcastle city collection: snapshot timestamp **2026-08-06T20:21:21Z**. This is a reproducible house-style edition, not a claim of newer live mapping. Per-ground source extracts and the renderer/style hash ledger are included under `reproduction/`.
"""
(P / "README.md").write_text(md)
(P / "SOFTWARE_IMPORT.md").write_text("""# Import the city-style stadium collection

Pull `main` and fetch these LFS assets:

```bash
git pull --ff-only
git lfs pull --include="artwork/uk-stadiums-city-style-2026-09-08/**,artwork/uk-stadiums-overhead-2026-09-08/**"
python3 scripts/verify_stadium_city_maps.py
```

Open `index.html` locally for the 44-ground gallery. GitHub renders the Markdown index; its HTML links show source until opened locally.

Each catalogue `files` path is relative to the release directory. `svg` is the final A3 master; `png` is its 254 DPI preview; `detail` is cropped from that exact preview; `plotjob` is compiled from the final SVG; `pens` contains absolute-page, registration-matched layers in the compiled load order. The two files in `simulation/` animate the actual exported strokes.

Print at 100% / actual size, A3 portrait 297 × 420 mm. Nominal pen widths remain 0.25/0.40 mm colours and the existing black inventory. The retained tiny architectural details are listed for physical proofing in each QA file. No hardware was operated or enabled.

## Rebuild

The existing frozen renderer is at `artwork/production-maps-2026-09-06/reproduction/renderer`. Its relevant source/style files are hashed in this edition's `reproduction/INPUTS.json`. The original stadium overlays are bound to the unchanged source handoff. Per-ground reference-complete map extracts are included; no live map query is needed.

Use Python 3.13 with the existing production reproduction requirements plus Pillow. From the repository root:

```bash
python3 scripts/build_stadium_city_maps.py --workers 4
python3 scripts/package_stadium_city_maps.py
python3 scripts/verify_stadium_city_maps.py
```

The builder's local intermediate maps are under ignored `build/stadium-house-work/`. Remove only that edition's basemap intermediates before deliberately rebuilding with changed source inputs. Original overlays retain their source editions and reference-era caveats; see the original handoff for stadium reuse in other projects.
""")
shutil.copyfile(OLD / "ATTRIBUTION.md", P / "ATTRIBUTION.md")
# Bind the existing renderer rather than copying/replacing the shared code again.
frozen = ROOT / "artwork/production-maps-2026-09-06/reproduction"
files = sorted(
    p
    for p in (frozen / "renderer/src/city_map_plotter").rglob("*")
    if p.is_file() and "__pycache__" not in str(p) and p.suffix in [".py", ".json"]
) + [frozen / "styles/university-memorabilia-v2.json"]
inputs = {
    "source_snapshot_timestamp": "2026-08-06T20:21:21Z",
    "source_snapshot_parent_sha256": json.loads(
        (P / "reproduction/ACQUISITION.json").read_text()
    )["source_parent_sha256"],
    "extract_method": "osmium extract 1.15.0 / smart / types=multipolygon; complete ways and intersecting multipolygons; all tags retained",
    "renderer_and_style": [
        {"path": str(p.relative_to(ROOT)), "sha256": sha(p)} for p in files
    ],
    "original_stadium_catalog_sha256": sha(OLD / "catalog.json"),
    "sources": [
        {"path": str(p.relative_to(P)), "sha256": sha(p), "bytes": p.stat().st_size}
        for p in sorted((P / "reproduction/sources").glob("*.pbf"))
    ],
}
dump(P / "reproduction/INPUTS.json", inputs)
(P / "simulation").mkdir(exist_ok=True)


def sim(league):
    paths = [str(P / r["files"]["svg"]["path"]) for r in rows if r["league"] == league]
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/build_plotsim_viewer.py"),
            *paths,
            "--strict-svg",
            "--machine-profile",
            str(ROOT / "plotter-profiles/axidraw-class-simulation-v1.json"),
            "--out",
            str(P / "simulation" / f"{league}.html"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    print(result.stdout[-350:], flush=True)


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(sim, ["premier_league", "championship"]))
files = sorted(p for p in P.rglob("*") if p.is_file() and p.name != "CHECKSUMS.sha256")
(P / "CHECKSUMS.sha256").write_text(
    "".join(sha(p) + "  " + str(p.relative_to(P)) + "\n" for p in files)
)
print("Packaged", len(rows), "stadiums and", len(files), "release files", flush=True)

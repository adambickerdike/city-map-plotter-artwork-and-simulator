#!/usr/bin/env python3
"""Finish the original vector stencils with the house palette and a plotted website."""

# ruff: noqa: E402
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artwork/the-plot-room-promotional-a3"
FROZEN = ROOT / "artwork/production-maps-2026-09-06/reproduction/renderer/src"
sys.path[:0] = [str(FROZEN), str(ROOT / "tools")]
from city_map_plotter.stroke_font import stroke_text
from city_map_plotter.svgkit import reliable_vector_strokes, append_vector_strokes
from city_map_plotter.production_header import _bounds
from plotsim import preflight_svg

SVG = "http://www.w3.org/2000/svg"
INK = "http://www.inkscape.org/namespaces/inkscape"
SOD = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
for prefix, uri in [("", SVG), ("inkscape", INK), ("sodipodi", SOD)]:
    ET.register_namespace(prefix, uri)
BLUE, WHITE, BLACK = "#123b63", "#f7f6ee", "#18181b"
WEBSITE = "theplotroom.com"
SOURCE_MAP = (
    ROOT
    / "artwork/production-maps-2026-09-06/08-city-maps-uk/001-uk-university-lse/001-uk-university-lse.svg"
)
CONCEPTS = json.loads((OUT / "reproduction/concepts.json").read_text())
FORMATS = json.loads((FROZEN / "city_map_plotter/data/format-v1.json").read_text())[
    "formats"
]


def tag(name):
    return f"{{{SVG}}}{name}"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def save(root, path):
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def record(path):
    return {
        "path": str(path.relative_to(OUT)),
        "sha256": sha(path),
        "bytes": path.stat().st_size,
    }


def geometry(root):
    return Counter(
        (g.get("id"), p.get("d"))
        for g in root.findall(tag("g"))
        for p in g.iter(tag("path"))
        if g.get("id") != "layer-website"
    )


def build(concept, treatment):
    blueprint = treatment == "blueprint"
    sid = concept["id"]
    original = (
        OUT
        / "reproduction/inputs"
        / ("blueprint" if blueprint else "outlined")
        / f"{sid}.plot.svg"
    )
    root = ET.parse(original).getroot()
    before = geometry(root)
    source_groups = {
        g.get("id"): g for g in ET.parse(SOURCE_MAP).getroot().findall(tag("g"))
    }
    for g in root.findall(tag("g")):
        gid = g.get("id")
        if blueprint:
            assert g.get("stroke") == WHITE and g.get("data-plot-ink") == "White"
            g.set("data-plot-pen-profile", "white-blueprint-pens")
        elif gid != "layer-letter-outlines":
            source = source_groups[gid]
            for key, value in source.attrib.items():
                if (
                    key == "stroke"
                    or key.startswith("data-plot-")
                    or key == f"{{{INK}}}label"
                ):
                    g.set(key, value)
            assert float(g.get("stroke-width")) == float(source.get("stroke-width"))
        else:
            g.set("stroke", BLACK)
            g.set("data-plot-pen-profile", "actual-pens")
        # Pen metadata belongs to the effective layer; earlier source paths retained their old ink tags.
        for p in g.iter(tag("path")):
            for key in list(p.attrib):
                if key.startswith("data-plot-") or key in {"stroke", "stroke-width"}:
                    del p.attrib[key]
    assert geometry(root) == before
    fmt = FORMATS[concept["format"]]
    width, height = fmt["page_mm"]["width"], fmt["page_mm"]["height"]
    inset = fmt["content_inset_mm"]
    nib = fmt["nib_roles_mm"]["text"]
    cap = 2 * fmt["type_scale_mm"]["subtitle"]
    ink, colour = ("White", WHITE) if blueprint else ("Black", BLACK)
    reference = root.find(f"{tag('g')}[@id='layer-letter-outlines']")
    website = ET.Element(tag("g"), dict(reference.attrib))
    website.attrib.update(
        {
            "id": "layer-website",
            f"{{{INK}}}label": f"Website — {ink} {nib:g}",
            "stroke": colour,
            "stroke-width": str(nib),
            "data-plot-ink": ink,
            "data-plot-pen-id": f"{ink.lower()}-{nib:g}".replace(".", "-"),
            "data-copy": WEBSITE,
            "data-cap-height-mm": str(cap),
            "data-plot-pen-profile": "white-blueprint-pens"
            if blueprint
            else "actual-pens",
        }
    )
    for key in (
        "data-plot-nib-mm",
        "data-plot-nominal-nib-mm",
        "data-plot-width-mm",
        "data-plot-requested-width-mm",
    ):
        website.set(key, str(nib))
    strokes = reliable_vector_strokes(
        stroke_text(WEBSITE, x_mm=width / 2, y_mm=0, height_mm=cap, anchor="middle"),
        nib_mm=nib,
    )
    points = [p for stroke in strokes for p in stroke]
    top, bottom = min(p[1] for p in points), max(p[1] for p in points)
    lettering_bottom = _bounds(reference)[3] + float(reference.get("stroke-width")) / 2
    offset = (lettering_bottom + height - inset - (bottom - top)) / 2 - top
    append_vector_strokes(
        website, [[(x, y + offset) for x, y in stroke] for stroke in strokes]
    )
    bounds = _bounds(website)
    assert bounds[0] - nib / 2 >= inset and bounds[2] + nib / 2 <= width - inset
    assert bounds[1] - nib / 2 > lettering_bottom + fmt["gap_mm"]
    assert bounds[3] + nib / 2 <= height - inset
    assert abs((bounds[0] + bounds[2]) / 2 - width / 2) < 0.003
    root.append(website)
    # Keep each physical pen consecutive in the finished drawing.
    pen_order = {}
    for g in root.findall(tag("g")):
        pen_order.setdefault(g.get("data-plot-pen-id"), len(pen_order))
    groups = sorted(
        root.findall(tag("g")), key=lambda g: pen_order[g.get("data-plot-pen-id")]
    )
    for g in groups:
        root.remove(g)
    root.extend(groups)
    root.find(tag("title")).text = f"The Plot Room — {concept['name']} / {treatment}"
    root.find(
        tag("desc")
    ).text = "Real city-map lines inside outlined letters, with theplotroom.com centred below."
    meta = {
        "concept": concept,
        "treatment": treatment,
        "website": WEBSITE,
        "page_mm": [width, height],
        "input": record(original),
        "palette_source": {
            "path": str(SOURCE_MAP.relative_to(ROOT)),
            "sha256": sha(SOURCE_MAP),
        },
        "map_and_letter_outline_geometry_unchanged": True,
        "map_palette": "white-blueprint-pens"
        if blueprint
        else "original-city-map-colours",
        "website_bounds_mm": list(bounds),
        "website_nib_mm": nib,
        "website_font": "plotter-grid-v4",
        "paper_colour": BLUE if blueprint else "#ffffff",
        "physical_execution_allowed": False,
    }
    root.find(tag("metadata")).text = json.dumps(meta)
    dest = OUT / treatment
    dest.mkdir(exist_ok=True)
    plot = dest / f"{sid}.plot.svg"
    save(root, plot)
    pf = preflight_svg(plot)
    assert not pf.errors and pf.metadata_complete
    meta["preflight"] = pf.as_dict()
    display = copy.deepcopy(root)
    if blueprint:
        display.insert(
            0,
            ET.Element(
                tag("rect"),
                {
                    "id": "preview-paper-background",
                    "x": "0",
                    "y": "0",
                    "width": str(width),
                    "height": str(height),
                    "fill": BLUE,
                    "stroke": "none",
                },
            ),
        )
    svg = dest / f"{sid}.svg"
    save(display, svg)
    pens = defaultdict(list)
    for g in groups:
        pens[g.get("data-plot-pen-id")].append(g)
    pen_dir = dest / "pens" / sid
    pen_dir.mkdir(parents=True, exist_ok=True)
    pen_files, split = [], Counter()
    for i, (pen, layers) in enumerate(pens.items(), 1):
        pr = ET.Element(tag("svg"), dict(root.attrib))
        pr.extend(copy.deepcopy(layers))
        pp = pen_dir / f"{i:02d}-{pen}.svg"
        save(pr, pp)
        assert not preflight_svg(pp).errors
        pen_files.append(record(pp))
        split.update(p.get("d") for p in pr.iter(tag("path")))
    assert split == Counter(p.get("d") for p in root.iter(tag("path")))
    png, pdf = dest / f"{sid}.png", dest / f"{sid}.pdf"
    for args in [
        [
            "--export-type=png",
            "--export-dpi=300",
            f"--export-background={meta['paper_colour']}",
            "--export-background-opacity=1",
            f"--export-filename={png}",
        ],
        ["--export-type=pdf", f"--export-filename={pdf}"],
    ]:
        subprocess.run(["inkscape", str(svg), *args], check=True, capture_output=True)
    with Image.open(png) as im:
        assert im.size == ((3508, 4961) if width < height else (4961, 3508))
        meta["png_pixels"] = im.size
        im.thumbnail((1000, 1414))
        preview = dest / f"{sid}.preview.png"
        im.save(preview)
    pdfinfo = subprocess.check_output(["pdfinfo", str(pdf)], text=True)
    dimensions = re.search(r"Page size:\s+([\d.]+) x ([\d.]+)", pdfinfo)
    assert dimensions and all(
        abs(float(a) * 25.4 / 72 - b) < 0.02
        for a, b in zip(dimensions.groups(), [width, height])
    )
    meta["checks"] = {
        "a3_pdf": "passed",
        "geometry_preservation": "passed",
        "website_bounds": "passed",
        "pen_split_parity": "passed",
    }
    qa = dest / f"{sid}.json"
    dump(qa, meta)
    row = {
        "id": sid,
        "name": concept["name"],
        "treatment": treatment,
        "files": {
            k: record(p)
            for k, p in [
                ("svg", svg),
                ("plot_svg", plot),
                ("png", png),
                ("pdf", pdf),
                ("preview", preview),
                ("qa", qa),
            ]
        },
        "pens": pen_files,
    }
    print(
        f"Finished {treatment}/{sid}: original map geometry, website and A3 exports verified",
        flush=True,
    )
    return row


def package(rows):
    catalog = {
        "schema_version": 1,
        "title": "The Plot Room — promotional A3 lettering",
        "website": WEBSITE,
        "exports": rows,
        "physical_execution_allowed": False,
    }
    dump(OUT / "catalog.json", catalog)
    font = ImageFont.truetype("/usr/share/fonts/noto/NotoSans-Bold.ttf", 28)
    for treatment in ("colour", "blueprint"):
        sheet = Image.new("RGB", (1700, 1490), "#eeede9")
        draw = ImageDraw.Draw(sheet)
        draw.text(
            (35, 20), "THE PLOT ROOM / " + treatment.upper(), font=font, fill="#26333d"
        )
        for i, c in enumerate(CONCEPTS):
            with Image.open(OUT / treatment / f"{c['id']}.png") as im:
                im.thumbnail((760, 600))
                x, y = 35 + (i % 2) * 850, 85 + (i // 2) * 700
                sheet.paste(im, (x + (760 - im.width) // 2, y + (600 - im.height) // 2))
                draw.text(
                    (x, y + 625),
                    c["id"][:2] + " / " + c["name"],
                    font=font,
                    fill="#26333d",
                )
        sheet.save(OUT / f"{treatment}-comparison.png")
    sheet = Image.new("RGB", (1700, 1290), "#eeede9")
    draw = ImageDraw.Draw(sheet)
    draw.text((35, 20), "THE PLOT ROOM / COLOUR & BLUEPRINT", font=font, fill="#26333d")
    for i, treatment in enumerate(("colour", "blueprint")):
        with Image.open(OUT / treatment / "04-ink-and-river.png") as im:
            im.thumbnail((780, 1160))
            sheet.paste(im, (35 + i * 850 + (780 - im.width) // 2, 85))
    sheet.save(OUT / "featured-comparison.png")
    cards, table = [], []
    for c in CONCEPTS:
        panels = []
        for t, label in [
            ("colour", "Original map colours / black outlines"),
            ("blueprint", "White pen / dark blue"),
        ]:
            stem = t + "/" + c["id"]
            panels.append(
                f'<article><a href="{stem}.png"><img src="{stem}.preview.png" alt="{html.escape(c["name"])} — {label}"></a><h3>{label}</h3><nav><a href="{stem}.pdf">A3 PDF</a> · <a href="{stem}.png">300 DPI PNG</a> · <a href="{stem}.svg">SVG</a> · <a href="{stem}.plot.svg">Plot SVG</a></nav></article>'
            )
        cards.append(
            f'<section><h2>{c["id"][:2]} / {html.escape(c["name"])}</h2><div class="pair">'
            + "".join(panels)
            + "</div></section>"
        )
        sid = c["id"]
        table.append(
            f"| {sid[:2]} — {c['name']} | [PNG](colour/{sid}.png) · [PDF](colour/{sid}.pdf) | [PNG](blueprint/{sid}.png) · [PDF](blueprint/{sid}.pdf) |"
        )
    page = (
        """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>The Plot Room — promotional lettering</title><style>*{box-sizing:border-box}body{margin:0;background:#eeede9;color:#26333d;font-family:system-ui,sans-serif}main{max-width:1500px;margin:auto;padding:32px}p{line-height:1.6}section{margin-top:38px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:28px}article>a{display:flex;height:630px;align-items:center;justify-content:center}img{max-width:100%;max-height:100%;box-shadow:0 3px 14px #26333d20}a{color:#244e6a}h3{font-size:17px}footer{margin-top:40px;border-top:1px solid #bbb;padding-top:20px}@media(max-width:700px){.pair{grid-template-columns:1fr}main{padding:20px}article>a{height:520px}}</style><main><h1>The Plot Room / A3 promotional prints</h1><p>Original city-map colours inside black-outlined letters, or white pen on dark blue.<br>Every print carries <strong>theplotroom.com</strong> beneath the lettering.</p><nav><a href="colour-comparison.png">Colour overview</a> · <a href="blueprint-comparison.png">Blueprint overview</a> · <a href="simulation/colour.html">Colour simulation</a> · <a href="simulation/blueprint.html">Blueprint simulation</a></nav>"""
        + "".join(cards)
        + """<footer><p>Print the A3 PDFs at actual size / 100%. The blueprint display artwork includes blue paper; plotting SVGs contain only the pen strokes.</p><p>01 and 04 now share the same original colours and stencil; the earlier option numbers are retained.</p><a href="README.md">File guide</a> · <a href="ATTRIBUTION.md">Source credits</a> · <a href="https://theplotroom.com">theplotroom.com</a></footer></main></html>"""
    )
    (OUT / "index.html").write_text(page)
    (OUT / "README.md").write_text(
        """# The Plot Room — A3 promotional writing plots

The colour prints retain the original road, river, path, park and landmark colours from the city map, with **black outlines around the letters**. The blueprint prints use **white pen on dark blue**. All eight exports have **theplotroom.com** centred underneath in plotted lowercase lettering.

[Open the local gallery](index.html) · [Colour overview](colour-comparison.png) · [Blueprint overview](blueprint-comparison.png)

![Colour and blueprint examples](featured-comparison.png)

| Design | Colour | Blueprint |
|---|---|---|
"""
        + "\n".join(table)
        + """

Options 01 and 04 now share the original map colours and identical stencil geometry; their earlier option numbers are retained. Option 02 is landscape, and the others are portrait.

Each design includes an A3 vector PDF, 300 DPI PNG, smaller preview, display SVG, stroke-only `.plot.svg`, per-pen SVGs and a QA record. Print at **actual size / 100%**. The blueprint display SVG/PDF contains the dark blue background; its plotting SVG contains only white pen strokes. These are nominal pen simulations, with no physical machine enabled.

The original letter and map paths are preserved exactly. The website uses the existing vector stroke font. Colour records are checked against the source city SVG; preflight, website placement, pen parity, image dimensions and PDF page dimensions are checked during generation.

The original local studies remain in `build/the-plot-room-a3-outlined-blueprint-2026-09-08/`. The current outputs are here under `artwork/the-plot-room-promotional-a3/`.

## Rebuild and attribution

Run `scripts/build_plot_room_promotion.py` using the existing renderer environment with Pillow and Inkscape installed. The preserved input vectors are in `reproduction/inputs/`; the frozen font and format come from the existing production renderer. No new map download is needed. Run `scripts/verify_plot_room_promotion.py` to verify a downloaded package. Keep [ATTRIBUTION.md](ATTRIBUTION.md) with promotional use.
"""
    )
    (OUT / "simulation").mkdir(exist_ok=True)

    def simulator(treatment):
        target = OUT / "simulation" / f"{treatment}.html"
        paths = [
            str(OUT / row["files"]["plot_svg"]["path"])
            for row in rows
            if row["treatment"] == treatment
        ]
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/build_plotsim_viewer.py"),
                *paths,
                "--strict-svg",
                "--machine-profile",
                str(ROOT / "plotter-profiles/axidraw-class-simulation-v1.json"),
                "--out",
                str(target),
            ],
            check=True,
            capture_output=True,
        )
        colour = BLUE if treatment == "blueprint" else "#ffffff"
        css = f"<style>:root{{--paper:{colour}!important;--paper-edge:{colour}!important;}}</style>"
        target.write_text(target.read_text().replace("</head>", css + "</head>", 1))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(simulator, ("colour", "blueprint")))
    files = sorted(
        p for p in OUT.rglob("*") if p.is_file() and p.name != "CHECKSUMS.sha256"
    )
    (OUT / "CHECKSUMS.sha256").write_text(
        "".join(sha(p) + "  " + str(p.relative_to(OUT)) + "\n" for p in files)
    )
    print("Packaged all eight promotional prints", flush=True)


if __name__ == "__main__":
    rows = [
        build(c, treatment) for treatment in ("colour", "blueprint") for c in CONCEPTS
    ]
    package(rows)

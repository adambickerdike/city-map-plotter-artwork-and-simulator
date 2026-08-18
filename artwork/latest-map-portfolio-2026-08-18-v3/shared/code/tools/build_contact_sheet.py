#!/usr/bin/env python3
"""Compose rendered plates into one series contact sheet.

    python3 tools/build_contact_sheet.py review-output/rowing-heads-v1/*.png \
        --out review-output/rowing-heads-v1/series-contact-sheet.png --columns 2

A series is a design decision, and you cannot review one plate at a time: the
question is whether four sheets look like they belong together. This lays the
rendered plates out on one page so that question can actually be answered.

The sheet is built as an SVG with each plate embedded as a data URI and then
rasterised with Inkscape, which is already a hard dependency of ``--png``. That
keeps the tool free of an imaging library and keeps the page itself inspectable.
"""

from __future__ import annotations

import argparse
import base64
import shutil
import struct
import subprocess
import sys
from pathlib import Path


def png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path} is not a PNG.")
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def build_svg(
    plates: list[Path], *, columns: int, gap: int, margin: int, background: str
) -> str:
    sizes = [png_size(path) for path in plates]
    cell_width = max(width for width, _ in sizes)
    cell_height = max(height for _, height in sizes)
    rows = (len(plates) + columns - 1) // columns
    page_width = margin * 2 + columns * cell_width + (columns - 1) * gap
    page_height = margin * 2 + rows * cell_height + (rows - 1) * gap

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{page_width}" height="{page_height}" '
        f'viewBox="0 0 {page_width} {page_height}">',
        f'<rect width="{page_width}" height="{page_height}" fill="{background}"/>',
    ]
    for index, (path, (width, height)) in enumerate(zip(plates, sizes)):
        column = index % columns
        row = index // columns
        # Centre each plate in its cell so a landscape sheet in a portrait
        # series does not sit hard against one edge.
        x = margin + column * (cell_width + gap) + (cell_width - width) // 2
        y = margin + row * (cell_height + gap) + (cell_height - height) // 2
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append(
            f'<image x="{x}" y="{y}" width="{width}" height="{height}" '
            f'xlink:href="data:image/png;base64,{encoded}"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plates", nargs="+", type=Path, help="Rendered plate PNGs.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--columns", type=int, default=3)
    parser.add_argument("--gap", type=int, default=48)
    parser.add_argument("--margin", type=int, default=48)
    parser.add_argument("--background", default="#ffffff")
    parser.add_argument(
        "--keep-svg",
        action="store_true",
        help="Keep the intermediate SVG next to the PNG.",
    )
    args = parser.parse_args(argv)

    missing = [path for path in args.plates if not path.is_file()]
    if missing:
        raise SystemExit("missing plate(s): " + ", ".join(str(p) for p in missing))
    if args.columns < 1:
        raise SystemExit("--columns must be at least 1")

    svg_path = args.out.with_suffix(".svg")
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(
        build_svg(
            list(args.plates),
            columns=args.columns,
            gap=args.gap,
            margin=args.margin,
            background=args.background,
        ),
        encoding="utf-8",
    )

    inkscape = shutil.which("inkscape")
    if inkscape is None:
        raise SystemExit(
            f"wrote {svg_path}; install Inkscape to rasterise it to {args.out}"
        )
    subprocess.run(
        [
            inkscape,
            "--export-type=png",
            f"--export-filename={args.out}",
            str(svg_path),
        ],
        check=True,
        capture_output=True,
    )
    if not args.keep_svg:
        svg_path.unlink()
    width, height = png_size(args.out)
    print(f"wrote {args.out} ({len(args.plates)} plates, {width}x{height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Make downloadable colour and blueprint bundles from the verified plotting SVGs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from verify_plot_room_promotion import verify

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "artwork/the-plot-room-promotional-a3"
OUT = ROOT / "build/plot-room-pen-downloads-2026-09-09"
TAG = "plot-room-promotional-a3-2026-09-09"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def build():
    verify(PACKAGE)
    rows = json.loads((PACKAGE / "catalog.json").read_text())["exports"]
    OUT.mkdir(parents=True, exist_ok=True)
    report = []
    for treatment in ("colour", "blueprint"):
        selected = [r for r in rows if r["treatment"] == treatment]
        assert len(selected) == 4
        files = {"ATTRIBUTION.md": (PACKAGE / "ATTRIBUTION.md").read_bytes()}
        bundled_rows = []
        for row in selected:
            kept = {role: row["files"][role] for role in ("plot_svg", "preview")}
            for item in [*kept.values(), *row["pens"]]:
                payload = (PACKAGE / item["path"]).read_bytes()
                assert not payload.startswith(
                    b"version https://git-lfs.github.com/spec/v1"
                )
                assert sha(payload) == item["sha256"] and len(payload) == item["bytes"]
                files[item["path"]] = payload
            bundled_rows.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "files": kept,
                    "pens": row["pens"],
                }
            )
        manifest = {
            "schema_version": 1,
            "release_tag": TAG,
            "treatment": treatment,
            "website": "theplotroom.com",
            "exports": bundled_rows,
        }
        files["CATALOG.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
        ink_note = (
            "Use white pens on dark blue paper. The blue background is paper, so it is not a drawn layer in the plotting SVGs."
            if treatment == "blueprint"
            else "The original city-map colours are retained. Letter outlines and the website use black."
        )
        lines = "\n".join(
            f"- [{r['id']} — {r['name']}]({r['files']['plot_svg']['path']})"
            for r in selected
        )
        guide = f"""# The Plot Room — {treatment} pen-plotting files

1. Extract this ZIP.
2. Use **Load SVG / Import SVG** in your pen-plotting software and choose one of the `.plot.svg` files below.
3. Keep the original A3 page dimensions and 100% scale: 297 × 420 mm portrait, or 420 × 297 mm for option 02.

{lines}

The plotting SVG includes all map paths, letter outlines and **theplotroom.com**. If your software uses individual pen files, open the files under `{treatment}/pens/<design>/` in their numbered order. Keep their page origin and size fixed so the layers stay registered.

{ink_note}

The `*.preview.png` images identify each design. Options 01 and 04 share the same stencil and colours; their original option numbers are retained. Keep ATTRIBUTION.md with promotional use. CATALOG.json and CHECKSUMS.sha256 bind the exact plotting files in this ZIP.
"""
        files["IMPORT.md"] = guide.encode()
        files["CHECKSUMS.sha256"] = "".join(
            f"{sha(data)}  {name}\n" for name, data in sorted(files.items())
        ).encode()
        target = OUT / f"the-plot-room-{treatment}-pen-files.zip"
        with zipfile.ZipFile(
            target, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 9, 9, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data, compresslevel=6)
        with zipfile.ZipFile(target) as archive:
            assert archive.testzip() is None
            assert set(archive.namelist()) == set(files)
            for name, data in files.items():
                assert archive.read(name) == data
        report.append(
            {
                "file": target.name,
                "bytes": target.stat().st_size,
                "sha256": sha(target.read_bytes()),
                "plotting_masters": 4,
                "pen_files": sum(len(r["pens"]) for r in selected),
                "verified": True,
            }
        )
    (OUT / "DOWNLOADS.json").write_text(json.dumps(report, indent=2) + "\n")
    (OUT / "SHA256SUMS.txt").write_text(
        "".join(f"{r['sha256']}  {r['file']}\n" for r in report)
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    build()

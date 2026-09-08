#!/usr/bin/env python3
"""Verify the latest 44-ground UK stadium handoff without third-party packages."""

from __future__ import annotations

from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "artwork/uk-stadiums-overhead-2026-09-08"
ROLES = {
    "map_svg",
    "map_manifest",
    "map_preview",
    "detail_preview",
    "stadium_overlay",
    "overlay_manifest",
    "stadium_svg",
    "stadium_preview",
    "stadium_manifest",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError(f"Unsafe package path: {relative}")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing regular file: {relative}")
    if path.read_bytes()[:42].startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise ValueError(f"Unresolved LFS pointer: {relative}; run git lfs pull")
    return path


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.cards = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.cards += tag == "article"
        self.links.extend(value for key, value in attrs if key in {"src", "href"})


def verify(package: Path = PACKAGE) -> dict:
    catalog = json.loads(checked_path(package, "catalog.json").read_text())
    records = catalog["stadiums"]
    counts = Counter(record["league"] for record in records)
    if counts != {"premier_league": 20, "championship": 24}:
        raise ValueError(f"Wrong stadium selection: {counts}")
    if len({r["id"] for r in records}) != 44:
        raise ValueError("Duplicate stadium identifiers")
    expected = set()
    for record in records:
        files = record["files"]
        if set(files) != ROLES or record["physical_execution_allowed"] is not False:
            raise ValueError(f"Invalid import record: {record['id']}")
        for item in files.values():
            path = checked_path(package, item["path"])
            if digest(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
                raise ValueError(f"File changed: {item['path']}")
            expected.add(item["path"])
            if path.suffix == ".png" and not path.read_bytes().startswith(
                b"\x89PNG\r\n\x1a\n"
            ):
                raise ValueError(f"Not a PNG: {path}")
            if path.suffix == ".svg":
                svg = ET.parse(path).getroot()
                if not svg.findall(".//{http://www.w3.org/2000/svg}path"):
                    raise ValueError(f"No native paths: {path}")
                if svg.findall(".//{http://www.w3.org/2000/svg}image"):
                    raise ValueError(f"Embedded reference bitmap: {path}")

        def load(role: str) -> dict:
            return json.loads((package / files[role]["path"]).read_text())

        overlay = load("stadium_overlay")
        manifest = load("stadium_manifest")
        plot = load("map_manifest")
        if (
            manifest["svg_sha256"] != files["stadium_svg"]["sha256"]
            or manifest["overlay_sha256"] != files["stadium_overlay"]["sha256"]
            or plot["svg_sha256"] != files["map_svg"]["sha256"]
            or not manifest["validation"]["passed"]
        ):
            raise ValueError(f"Original source bindings differ: {record['id']}")
        if overlay["georeference"] != record["georeference"]:
            raise ValueError(f"Georeference changed: {record['id']}")
        if overlay["centring"]["centre_error_m"] > 0.000001:
            raise ValueError(f"Field is not centred: {record['id']}")
        paths = overlay["shell_paths"] + overlay["paths"]
        if len({p["id"] for p in paths}) != len(paths):
            raise ValueError(f"Duplicate geometry IDs: {record['id']}")
        if not any(p.get("commands_m") for p in paths):
            raise ValueError(f"Native geometry missing: {record['id']}")
        version = re.search(r"_v(\d+)_A3.svg$", files["map_svg"]["path"])
        if not version or int(version[1]) != record["version"]:
            raise ValueError(f"Version mismatch: {record['id']}")
    etihad = next(r for r in records if r["id"] == "manchester_city")
    if etihad["version"] != 10:
        raise ValueError("The final Etihad v10 crown fix is missing")
    galleries = {
        "index.html": 44,
        "Premier_League_Stadium_Maps/index.html": 20,
        "Championship_Stadium_Maps/index.html": 24,
    }
    for relative, count in galleries.items():
        page = checked_path(package, relative)
        parser = Links()
        parser.feed(page.read_text())
        if parser.cards != count:
            raise ValueError(f"Wrong gallery count: {relative}")
        for link in parser.links:
            url = urlsplit(link)
            if url.scheme or not url.path:
                continue
            target = (page.parent / unquote(url.path)).resolve()
            if not target.is_relative_to(package.resolve()) or not target.is_file():
                raise ValueError(f"Broken gallery link: {relative}: {link}")
    inventory = checked_path(package, "CHECKSUMS.sha256").read_text().splitlines()
    listed = set()
    for line in inventory:
        checksum, relative = line.split("  ", 1)
        if relative in listed or digest(checked_path(package, relative)) != checksum:
            raise ValueError(f"Bad inventory entry: {relative}")
        listed.add(relative)
    actual = {
        p.relative_to(package).as_posix()
        for p in package.rglob("*")
        if p.is_file() and p.name != "CHECKSUMS.sha256"
    }
    if listed != actual or not expected.issubset(listed):
        raise ValueError("Checksum inventory is incomplete or includes extra files")
    if any("Before_" in p or ".stadium_aerial_references" in p for p in actual):
        raise ValueError("Private references or superseded drafts included")
    return {
        "status": "passed",
        "stadiums": 44,
        "counts": dict(counts),
        "source_artwork_files": len(expected),
        "checksum_files": len(listed),
        "galleries": len(galleries),
        "etihad_version": 10,
        "physical_execution_allowed": False,
    }


def main() -> int:
    try:
        print(json.dumps(verify(), indent=2))
    except (ValueError, OSError, KeyError, ET.ParseError) as exc:
        print(f"verify_uk_stadiums: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

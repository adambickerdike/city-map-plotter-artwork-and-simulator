"""HTTP backend for the interactive framing UI.

The server is intentionally a thin, standard-library shell around the real
CLI.  ``POST /api/export`` validates a browser frame into an explicit
``mapplot export`` argument list and runs it as a subprocess job; the browser
polls the job, then fetches the finished SVG and manifest.  Nothing here
renders map geometry itself, so the web path cannot drift from the audited
pipeline.

The server binds loopback by default and serves only files it created inside
one per-job directory; job identifiers are unguessable UUIDs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..cartography import DETAIL_PROFILE_CHOICES, WATER_FILL_CHOICES
from ..geometry import (
    PAPER_SIZES_MM,
    POSTER_PRESET_FORMAT_IDS,
    load_plate_format,
)
from ..models import BoundingBox, MapPlotterError
from ..styles import (
    DEFAULT_FAMILIES,
    DEFAULT_STYLES,
    MAP_LINEWORK_NIB_ROLES,
    POSTER_STYLE_OVERRIDES,
)

ALL_FAMILIES = ("roads", "water", "railways", "parks", "buildings", "boundaries")
ORIENTATIONS = ("portrait", "landscape")
EXTENT_FITS = ("contain", "cover")
# The standard (non-poster) layout reserves this attribution footer; the
# browser viewfinder must use the same constant to stay paper-true.
STANDARD_FOOTER_MM = 5.0
DEFAULT_STANDARD_MARGIN_MM = 10.0
# Mirrors the CLI's public-Overpass safety default rather than replacing it:
# failing in the browser is friendlier than failing after a job launch.
DEFAULT_MAX_AREA_KM2 = 500.0
MAX_TEXT_LENGTH = 120
MAX_DETAIL_LINES = 3
MAX_REQUEST_BYTES = 64 * 1024
LOG_LIMIT_LINES = 4000


def _sheet_formats(preset: str) -> dict[str, str]:
    sheet = POSTER_PRESET_FORMAT_IDS[preset].removesuffix("-portrait")
    return {
        orientation: f"{sheet}-{orientation}" for orientation in ORIENTATIONS
    }


_NOMINATIM_URL = "https://nominatim.openstreetmap.org"
_NOMINATIM_MIN_INTERVAL_S = 1.1
_nominatim_lock = threading.Lock()
_nominatim_last_call = 0.0
_reverse_cache: dict[tuple[float, float, int], dict[str, Any]] = {}


def _nominatim_json(path: str, params: dict[str, str]) -> Any:
    """One throttled, identified Nominatim request.

    All browser lookups route through here so the public service always sees
    the configured application User-Agent and at most one request per second,
    matching the usage policy the rest of the project follows.
    """

    user_agent = os.environ.get("CITY_MAP_PLOTTER_USER_AGENT", "").strip()
    if len(user_agent) < 8:
        raise RequestError(
            "Place lookup needs CITY_MAP_PLOTTER_USER_AGENT set before "
            "starting the server."
        )
    global _nominatim_last_call
    with _nominatim_lock:
        wait = _NOMINATIM_MIN_INTERVAL_S - (time.monotonic() - _nominatim_last_call)
        if wait > 0:
            time.sleep(wait)
        _nominatim_last_call = time.monotonic()
    url = f"{_NOMINATIM_URL}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MapPlotterError(f"Place lookup failed: {exc}") from exc


def _title_preference(zoom: int) -> tuple[str, ...]:
    """Which address rank names the frame, by how tight the frame is.

    A village-scale frame should say the village or suburb; a city-scale frame
    the town or city; a regional frame the city or county.  Town always
    outranks city at settlement scales because UK metropolitan boroughs arrive
    as "city" ("North Tyneside") while the real settlement is the "town"
    ("Whitley Bay").
    """

    if zoom >= 15:
        return ("village", "suburb", "neighbourhood", "quarter", "hamlet",
                "town", "city")
    if zoom >= 13:
        return ("town", "city", "village", "hamlet", "suburb", "municipality")
    return ("city", "town", "municipality", "county")


def reverse_locate(
    latitude: float, longitude: float, zoom: int = 14
) -> dict[str, Any]:
    """Name the settlement a frame is centred on, for the smart title."""

    zoom = max(3, min(16, int(zoom)))
    key = (round(latitude, 3), round(longitude, 3), zoom)
    cached = _reverse_cache.get(key)
    if cached is not None:
        return cached
    data = _nominatim_json(
        "/reverse",
        {
            "format": "jsonv2",
            "lat": f"{latitude:.5f}",
            "lon": f"{longitude:.5f}",
            "zoom": str(zoom),
        },
    )
    address = data.get("address", {}) if isinstance(data, dict) else {}
    title = next(
        (
            address[field]
            for field in _title_preference(zoom)
            if address.get(field)
        ),
        address.get("county", ""),
    )
    region_parts = [
        address[field]
        for field in ("state", "county", "country")
        if address.get(field) and address[field] != title
    ]
    result = {"title": title, "region": " / ".join(region_parts[:2])}
    _reverse_cache[key] = result
    return result


def geocode_search(query: str) -> list[dict[str, Any]]:
    """Top place matches for the search box, throttled and identified."""

    data = _nominatim_json(
        "/search", {"format": "jsonv2", "limit": "5", "q": query}
    )
    results = []
    for item in data if isinstance(data, list) else []:
        box = item.get("boundingbox")
        if not isinstance(box, list) or len(box) != 4:
            continue
        results.append(
            {
                "name": item.get("display_name", ""),
                "boundingbox": [float(value) for value in box],
            }
        )
    return results


def style_catalog(styles_dir: Path | None = None) -> dict[str, Any]:
    """Named palette files the operator's real plates were rendered with.

    Each ``styles/*.json`` becomes a selectable entry: its per-layer screen
    colour and nib (explicit ``nib_mm`` or the width in a pen label such as
    ``Red 0.4``) drive the preview, and its path is passed to the export as
    ``--style``.  Only names from this catalog are accepted by the export
    endpoint, so the browser can never point the CLI at an arbitrary file.
    """

    directory = styles_dir if styles_dir is not None else Path("styles")
    catalog: dict[str, Any] = {}
    if not directory.is_dir():
        return catalog
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_layers = data.get("layers")
        if not isinstance(raw_layers, dict):
            continue
        layers: dict[str, Any] = {}
        for layer_id, spec in raw_layers.items():
            if not isinstance(spec, dict):
                continue
            entry: dict[str, Any] = {}
            if isinstance(spec.get("stroke"), str):
                entry["color"] = spec["stroke"]
            nib = spec.get("nib_mm", spec.get("stroke_width_mm"))
            if not isinstance(nib, (int, float)):
                pen = spec.get("pen", spec.get("ink", ""))
                match = re.search(r"(\d+(?:\.\d+)?)\s*$", str(pen))
                nib = float(match.group(1)) if match else None
            if isinstance(nib, (int, float)):
                entry["nib_mm"] = float(nib)
            if entry:
                layers[layer_id] = entry
        if layers:
            catalog[path.stem] = {"path": str(path), "layers": layers}
    return catalog


def _ink_from_pen_label(pen: str) -> str | None:
    prefix, separator, suffix = pen.strip().rpartition(" ")
    if not separator:
        return None
    try:
        float(suffix)
    except ValueError:
        return None
    return prefix or None


def poster_palette_style(source: Path, destination: Path) -> Path:
    """Reduce a style file to palette-only overrides for a poster export.

    The plate contract's position is that overrides carry copy, palette and
    order while widths come from the sheet's own linework ladder, so an A4
    poster gets A4 pens.  A raw style file would pin its (typically A5) pen
    widths on every sheet; this derivation keeps each layer's screen colour
    and physical ink but drops every width field, letting the plate resolve
    sheet-correct nibs.
    """

    data = json.loads(source.read_text(encoding="utf-8"))
    layers: dict[str, Any] = {}
    for layer_id, spec in (data.get("layers") or {}).items():
        if not isinstance(spec, dict):
            continue
        kept = {
            key: value
            for key, value in spec.items()
            if key in ("label", "stroke", "order", "strokes", "passes",
                       "enabled", "ink")
        }
        if "ink" not in kept and isinstance(spec.get("pen"), str):
            ink = _ink_from_pen_label(spec["pen"])
            if ink:
                kept["ink"] = ink
        if kept:
            layers[layer_id] = kept
    destination.write_text(
        json.dumps({"layers": layers}, indent=1), encoding="utf-8"
    )
    return destination


def _pen_model() -> dict[str, Any]:
    """The per-layer pen plan the live preview must obey.

    Widths are not invented for the screen: standard exports use each layer's
    default style nib, and poster plates resolve the layer's semantic role
    through the sheet's own ``map_linework_nib_mm`` table, so A4 gets A4 pens.
    """

    layers: dict[str, Any] = {}
    for style in DEFAULT_STYLES:
        if style.id == "race_course":
            continue
        layers[style.id] = {
            "role": MAP_LINEWORK_NIB_ROLES[style.id],
            "standard_nib_mm": style.stroke_width_mm,
            "color": style.stroke,
            "poster_color": POSTER_STYLE_OVERRIDES.get(style.id, {}).get(
                "stroke", style.stroke
            ),
        }
    return layers


def build_ui_config() -> dict[str, Any]:
    """Everything the browser needs to draw a paper-true viewfinder."""

    presets: dict[str, Any] = {}
    for preset in sorted(POSTER_PRESET_FORMAT_IDS):
        formats = _sheet_formats(preset)
        entry: dict[str, Any] = {
            "map_field_aspect": {},
            "map_field_mm": {},
            "page_mm": {},
            "zones_mm": {},
            "border": {},
            "map_linework_nib_mm": {},
            "memorabilia_zones_mm": {},
        }
        for orientation, format_id in formats.items():
            plate = load_plate_format(format_id)
            entry["map_field_aspect"][orientation] = plate["map_field_aspect"]
            entry["map_field_mm"][orientation] = plate["zones_mm"]["map_field"]
            entry["page_mm"][orientation] = plate["page_mm"]
            entry["zones_mm"][orientation] = plate["zones_mm"]
            entry["border"][orientation] = {
                "outer": plate["border"]["outer"],
                "inner_offset_mm": plate["border"]["inner_offset_mm"],
                "nib_role": plate["border"].get("nib_role", "heavy"),
            }
            entry["map_linework_nib_mm"][orientation] = plate[
                "map_linework_nib_mm"
            ]
            entry["memorabilia_zones_mm"][orientation] = plate.get(
                "memorabilia_zones_mm"
            )
        presets[preset] = entry
    user_agent = os.environ.get("CITY_MAP_PLOTTER_USER_AGENT", "").strip()
    return {
        "papers": {name: list(size) for name, size in PAPER_SIZES_MM.items()},
        "poster_presets": presets,
        "layers": list(ALL_FAMILIES),
        "default_layers": list(DEFAULT_FAMILIES),
        "pen_model": _pen_model(),
        "styles": style_catalog(),
        "detail_profiles": sorted(DETAIL_PROFILE_CHOICES),
        "orientations": list(ORIENTATIONS),
        "standard_footer_mm": STANDARD_FOOTER_MM,
        "default_margin_mm": DEFAULT_STANDARD_MARGIN_MM,
        "max_area_km2": DEFAULT_MAX_AREA_KM2,
        "user_agent_configured": len(user_agent) >= 8,
    }


@dataclass(frozen=True)
class ExportRequest:
    """A validated browser frame, ready to become CLI arguments."""

    bbox: BoundingBox
    mode: str
    paper: str
    orientation: str
    margin_mm: float
    preset: str | None
    extent_fit: str
    layers: tuple[str, ...]
    detail_profile: str
    title: str | None
    subtitle: str | None
    details: tuple[str, ...]
    frame: bool
    landmark_buildings: bool
    water_fill: str
    style_path: Path | None
    poster_layout: str
    person_fields: tuple[tuple[str, str], ...]
    attribution_mode: str
    attribution_placement: str | None
    user_agent: str | None
    input_json: Path | None

    def argv(self, output_svg: Path) -> list[str]:
        argv = [
            "export",
            "--bbox",
            f"{self.bbox.west:.6f}",
            f"{self.bbox.south:.6f}",
            f"{self.bbox.east:.6f}",
            f"{self.bbox.north:.6f}",
            "--output",
            str(output_svg),
            "--layers",
            ",".join(self.layers),
            "--detail-profile",
            self.detail_profile,
            "--orientation",
            self.orientation,
        ]
        if self.mode == "poster":
            assert self.preset is not None
            argv += ["--preset", self.preset, "--extent-fit", self.extent_fit]
            if self.poster_layout != "classic":
                argv += ["--poster-layout", self.poster_layout]
            for flag, value in self.person_fields:
                argv += [flag, value]
        else:
            argv += ["--paper", self.paper, "--margin-mm", f"{self.margin_mm:g}"]
            if self.frame:
                argv.append("--frame")
        if self.title:
            argv += ["--title", self.title]
        if self.subtitle:
            argv += ["--subtitle", self.subtitle]
        for line in self.details:
            argv += ["--detail", line]
        if self.landmark_buildings and "buildings" in self.layers:
            argv.append("--landmark-buildings")
        if self.water_fill != "none":
            argv += ["--water-fill", self.water_fill]
        if self.style_path is not None:
            argv += ["--style", str(self.style_path)]
        if self.attribution_mode == "external":
            assert self.attribution_placement is not None
            argv += [
                "--attribution-mode", "external",
                "--external-attribution-placement", self.attribution_placement,
            ]
        if self.input_json is not None:
            argv += ["--input-json", str(self.input_json)]
        else:
            # City-scale all-road acquisitions regularly need more than the
            # default 120 s HTTP budget before Overpass answers.
            argv += ["--timeout", "240"]
        if self.user_agent:
            argv += ["--user-agent", self.user_agent]
        return argv


class RequestError(MapPlotterError):
    """A browser request that must be rejected with HTTP 400."""


def _text_field(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError(f"{key} must be a string.")
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_TEXT_LENGTH:
        raise RequestError(f"{key} must be at most {MAX_TEXT_LENGTH} characters.")
    return value


def validate_export_request(
    payload: dict[str, Any], *, max_area_km2: float = DEFAULT_MAX_AREA_KM2
) -> ExportRequest:
    if not isinstance(payload, dict):
        raise RequestError("The export request body must be a JSON object.")

    raw_bbox = payload.get("bbox")
    if (
        not isinstance(raw_bbox, list)
        or len(raw_bbox) != 4
        or not all(isinstance(value, (int, float)) for value in raw_bbox)
    ):
        raise RequestError("bbox must be [west, south, east, north] numbers.")
    bbox = BoundingBox(*(float(value) for value in raw_bbox))
    if bbox.approximate_area_km2 > max_area_km2:
        raise RequestError(
            f"The frame covers {bbox.approximate_area_km2:.0f} km2, above the "
            f"{max_area_km2:.0f} km2 public-service safety limit. Zoom in, or "
            "run the CLI directly with --allow-large-area and a local PBF."
        )

    mode = payload.get("mode", "standard")
    if mode not in ("standard", "poster"):
        raise RequestError("mode must be 'standard' or 'poster'.")

    orientation = payload.get("orientation", "portrait")
    if orientation not in ORIENTATIONS:
        raise RequestError("orientation must be 'portrait' or 'landscape'.")

    preset: str | None = None
    paper = "A4"
    margin_mm = DEFAULT_STANDARD_MARGIN_MM
    if mode == "poster":
        preset = payload.get("preset")
        if preset not in POSTER_PRESET_FORMAT_IDS:
            raise RequestError(
                "preset must be one of: "
                + ", ".join(sorted(POSTER_PRESET_FORMAT_IDS))
                + "."
            )
    else:
        paper = str(payload.get("paper", "A4")).upper()
        if paper not in PAPER_SIZES_MM:
            raise RequestError(
                "paper must be one of: " + ", ".join(PAPER_SIZES_MM) + "."
            )
        raw_margin = payload.get("margin_mm", DEFAULT_STANDARD_MARGIN_MM)
        if not isinstance(raw_margin, (int, float)) or not 0 <= raw_margin <= 50:
            raise RequestError("margin_mm must be a number between 0 and 50.")
        margin_mm = float(raw_margin)

    extent_fit = payload.get("extent_fit", "contain")
    if extent_fit not in EXTENT_FITS:
        raise RequestError("extent_fit must be 'contain' or 'cover'.")

    raw_layers = payload.get("layers", list(DEFAULT_FAMILIES))
    if not isinstance(raw_layers, list) or not raw_layers:
        raise RequestError("layers must be a non-empty list of family names.")
    layers = tuple(dict.fromkeys(str(layer) for layer in raw_layers))
    unknown = [layer for layer in layers if layer not in ALL_FAMILIES]
    if unknown:
        raise RequestError(
            "Unknown layer families: "
            + ", ".join(unknown)
            + ". Choose from: "
            + ", ".join(ALL_FAMILIES)
            + "."
        )

    detail_profile = payload.get("detail_profile", "faithful")
    if detail_profile not in DETAIL_PROFILE_CHOICES:
        raise RequestError(
            "detail_profile must be one of: "
            + ", ".join(sorted(DETAIL_PROFILE_CHOICES))
            + "."
        )

    raw_details = payload.get("details", [])
    if not isinstance(raw_details, list) or len(raw_details) > MAX_DETAIL_LINES:
        raise RequestError(f"details must be a list of at most {MAX_DETAIL_LINES} lines.")
    details = tuple(
        line
        for line in (_text_field({"detail": item}, "detail") for item in raw_details)
        if line is not None
    )

    poster_layout = payload.get("poster_layout", "classic")
    if poster_layout not in ("classic", "university-memorabilia"):
        raise RequestError(
            "poster_layout must be 'classic' or 'university-memorabilia'."
        )
    if poster_layout == "university-memorabilia":
        if mode != "poster":
            raise RequestError(
                "The university-memorabilia layout requires poster mode."
            )
        if orientation != "portrait":
            raise RequestError(
                "The university-memorabilia layout is a portrait composition."
            )

    person_fields: list[tuple[str, str]] = []
    for key, flag in (
        ("person_name", "--person-name"),
        ("degree", "--degree"),
        ("honours", "--honours"),
        ("years", "--years"),
    ):
        value = _text_field(payload, key)
        if value is not None:
            person_fields.append((flag, value))

    attribution_mode = payload.get("attribution_mode", "embedded")
    if attribution_mode not in ("embedded", "external"):
        raise RequestError("attribution_mode must be 'embedded' or 'external'.")
    attribution_placement = _text_field(payload, "attribution_placement")
    if attribution_mode == "external" and attribution_placement is None:
        raise RequestError(
            "External attribution requires describing where the OpenStreetMap "
            "credit will actually accompany the artwork (for example a product "
            "page caption)."
        )

    water_fill = payload.get("water_fill", "none")
    if water_fill not in WATER_FILL_CHOICES:
        raise RequestError(
            "water_fill must be one of: " + ", ".join(sorted(WATER_FILL_CHOICES)) + "."
        )

    style_path: Path | None = None
    style_name = _text_field(payload, "style")
    if style_name is not None:
        catalog = style_catalog()
        if style_name not in catalog:
            raise RequestError(
                "Unknown style. Available styles: "
                + (", ".join(sorted(catalog)) or "none")
                + "."
            )
        style_path = Path(catalog[style_name]["path"])

    input_json: Path | None = None
    raw_input_json = _text_field(payload, "input_json")
    if raw_input_json is not None:
        input_json = Path(raw_input_json).expanduser()
        if input_json.suffix not in (".json", ".gz"):
            raise RequestError("input_json must point to a .json or .json.gz file.")
        if not input_json.is_file():
            raise RequestError(f"input_json file not found: {input_json}")

    user_agent = _text_field(payload, "user_agent")
    env_agent = os.environ.get("CITY_MAP_PLOTTER_USER_AGENT", "").strip()
    if input_json is None and not user_agent and len(env_agent) < 8:
        raise RequestError(
            "Live acquisition needs an identifying User-Agent with contact "
            "details. Fill in the contact field, or set "
            "CITY_MAP_PLOTTER_USER_AGENT before starting the server."
        )

    return ExportRequest(
        bbox=bbox,
        mode=mode,
        paper=paper,
        orientation=orientation,
        margin_mm=margin_mm,
        preset=preset,
        extent_fit=extent_fit,
        layers=layers,
        detail_profile=detail_profile,
        title=_text_field(payload, "title"),
        subtitle=_text_field(payload, "subtitle"),
        details=details,
        frame=bool(payload.get("frame", True)),
        landmark_buildings=bool(payload.get("landmark_buildings", False)),
        water_fill=water_fill,
        style_path=style_path,
        poster_layout=poster_layout,
        person_fields=tuple(person_fields),
        attribution_mode=attribution_mode,
        attribution_placement=attribution_placement,
        user_agent=user_agent,
        input_json=input_json,
    )


def _redacted(argv: list[str]) -> list[str]:
    public = list(argv)
    for index, token in enumerate(public):
        if token == "--user-agent" and index + 1 < len(public):
            public[index + 1] = "[redacted contact]"
    return public


@dataclass
class ExportJob:
    job_id: str
    directory: Path
    public_argv: list[str]
    created_at: str
    status: str = "running"
    returncode: int | None = None
    log: list[str] = field(default_factory=list)
    process: subprocess.Popen[str] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def svg_path(self) -> Path:
        return self.directory / "map.svg"

    @property
    def manifest_path(self) -> Path:
        return self.directory / "map.plot.json"

    def describe(self, *, log_tail: int = 60) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.job_id,
                "status": self.status,
                "returncode": self.returncode,
                "created_at": self.created_at,
                "argv": self.public_argv,
                "log_lines": len(self.log),
                "log_tail": self.log[-log_tail:],
                "svg_ready": self.status == "succeeded",
            }


class JobManager:
    """Launches one export subprocess per job and tracks its lifecycle."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self._jobs: dict[str, ExportJob] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> ExportJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [job.describe(log_tail=0) for job in reversed(jobs)]

    def start(self, request: ExportRequest) -> ExportJob:
        """Run ``python -m city_map_plotter export`` for ``request`` as a job."""

        job_id = uuid.uuid4().hex
        directory = self.output_root / job_id
        directory.mkdir(parents=True, exist_ok=True)
        if request.mode == "poster" and request.style_path is not None:
            request = replace(
                request,
                style_path=poster_palette_style(
                    request.style_path, directory / "palette-style.json"
                ),
            )
        cli_argv = request.argv(directory / "map.svg")
        job = ExportJob(
            job_id=job_id,
            directory=directory,
            public_argv=_redacted(cli_argv),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        job.process = subprocess.Popen(
            [sys.executable, "-m", "city_map_plotter", *cli_argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with self._lock:
            self._jobs[job_id] = job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    def cancel(self, job: ExportJob) -> None:
        with job.lock:
            process = job.process
            running = job.status == "running"
        if running and process is not None and process.poll() is None:
            process.terminate()
            with job.lock:
                job.status = "cancelled"

    def _pump(self, job: ExportJob) -> None:
        process = job.process
        assert process is not None and process.stdout is not None
        for line in process.stdout:
            with job.lock:
                job.log.append(line.rstrip("\n"))
                if len(job.log) > LOG_LIMIT_LINES:
                    del job.log[: len(job.log) - LOG_LIMIT_LINES]
        returncode = process.wait()
        with job.lock:
            job.returncode = returncode
            if job.status != "cancelled":
                if returncode == 0 and job.svg_path.is_file():
                    job.status = "succeeded"
                else:
                    job.status = "failed"


def _index_html() -> bytes:
    resource = files("city_map_plotter.webui").joinpath("static", "index.html")
    return resource.read_bytes()


class WebUiHandler(BaseHTTPRequestHandler):
    server: "WebUiServer"  # narrowed for attribute access

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.server.verbose:
            super().log_message(format, *args)

    # -- helpers ---------------------------------------------------------

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, *, status: int) -> None:
        self._send_json({"error": message}, status=status)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send_error_json("Artifact not available yet.", status=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _job_for_path(self, parts: list[str]) -> ExportJob | None:
        job = self.server.jobs.get(parts[2]) if len(parts) >= 3 else None
        if job is None:
            self._send_error_json("Unknown job.", status=404)
        return job

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0]
        parts = [part for part in path.split("/") if part]
        if path in ("/", "/index.html"):
            body = _index_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/config":
            self._send_json(self.server.config)
            return
        if path == "/api/reverse":
            query = urllib.parse.parse_qs(
                self.path.partition("?")[2], keep_blank_values=False
            )
            try:
                latitude = float(query["lat"][0])
                longitude = float(query["lon"][0])
                zoom = int(query.get("zoom", ["14"])[0])
            except (KeyError, ValueError, IndexError):
                self._send_error_json("lat and lon are required.", status=400)
                return
            try:
                self._send_json(reverse_locate(latitude, longitude, zoom))
            except RequestError as error:
                self._send_error_json(str(error), status=400)
            except MapPlotterError as error:
                self._send_error_json(str(error), status=502)
            return
        if path == "/api/geocode":
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            text = (query.get("q") or [""])[0].strip()
            if not 1 <= len(text) <= 200:
                self._send_error_json("q is required.", status=400)
                return
            try:
                self._send_json({"results": geocode_search(text)})
            except RequestError as error:
                self._send_error_json(str(error), status=400)
            except MapPlotterError as error:
                self._send_error_json(str(error), status=502)
            return
        if path == "/api/jobs":
            self._send_json({"jobs": self.server.jobs.list_jobs()})
            return
        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            job = self._job_for_path(parts)
            if job is not None:
                self._send_json({"job": job.describe()})
            return
        if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
            job = self._job_for_path(parts)
            if job is None:
                return
            if parts[3] == "svg":
                self._send_file(job.svg_path, "image/svg+xml; charset=utf-8")
                return
            if parts[3] == "manifest":
                self._send_file(job.manifest_path, "application/json; charset=utf-8")
                return
        self._send_error_json("Not found.", status=404)

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0]
        parts = [part for part in path.split("/") if part]
        if path == "/api/export":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._send_error_json("Request body missing or too large.", status=413)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_error_json("Request body must be UTF-8 JSON.", status=400)
                return
            try:
                request = validate_export_request(payload)
            except MapPlotterError as error:
                self._send_error_json(str(error), status=400)
                return
            job = self.server.jobs.start(request)
            self._send_json({"job": job.describe()}, status=202)
            return
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
            cancel_job = self._job_for_path(parts)
            if cancel_job is not None:
                self.server.jobs.cancel(cancel_job)
                self._send_json({"job": cancel_job.describe()})
            return
        self._send_error_json("Not found.", status=404)


class WebUiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        output_root: Path,
        verbose: bool = False,
    ) -> None:
        super().__init__(address, WebUiHandler)
        self.jobs = JobManager(output_root)
        self.config = build_ui_config()
        self.verbose = verbose


def build_server(
    host: str = "127.0.0.1",
    port: int = 8747,
    *,
    output_root: Path | None = None,
    verbose: bool = False,
) -> WebUiServer:
    root = output_root if output_root is not None else Path("output") / "webui"
    return WebUiServer((host, port), output_root=root, verbose=verbose)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mapplot-web",
        description=(
            "Serve the local framing UI: a live world map with a paper-true "
            "viewfinder that exports through the ordinary mapplot pipeline."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address.")
    parser.add_argument("--port", type=int, default=8747, help="Bind port.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output") / "webui",
        help="Directory that receives one subdirectory per export job.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the UI in the default browser.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Log every HTTP request."
    )
    args = parser.parse_args(argv)

    server = build_server(
        args.host, args.port, output_root=args.output_dir, verbose=args.verbose
    )
    port = server.server_address[1]
    url = f"http://{args.host}:{port}/"
    print(f"mapplot-web serving {url}")
    print(f"export jobs are written under {args.output_dir}/<job-id>/")
    if not os.environ.get("CITY_MAP_PLOTTER_USER_AGENT", "").strip():
        print(
            "note: CITY_MAP_PLOTTER_USER_AGENT is not set; live exports will "
            "need a contact filled into the UI."
        )
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

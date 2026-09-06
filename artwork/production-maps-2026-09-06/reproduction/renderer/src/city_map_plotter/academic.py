"""Rights-aware academic, scientific, and engineering pen-art generators.

This domain consumes customer-supplied data and already-cleared local assets.
It does not fetch publisher PDFs, scrape figures, infer scientific values, or
invent institutional branding.  Every renderer returns the existing
``PlateArtwork`` interchange, so page formats, vector typography, physical pen
layers, previews, and plot manifests remain owned by the established system.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping, NoReturn, Sequence

from shapely.geometry import LineString, MultiLineString, Polygon

from .models import MapPlotterError
from .niche_common import (
    ArtworkLayer,
    NormalizedCanvas,
    PlateArtwork,
    PlateContext,
    Rect,
    arrow_strokes,
    circle_stroke,
    context_for,
    ellipse_stroke,
    normalize_points,
    polyline_length_mm,
    rectangle_stroke,
)
from .scientific_assets import (
    SVGLinework,
    extract_svg_linework,
    fit_vector_paths,
    load_csv_columns,
    load_json_asset,
    resolve_local_asset,
)
from .scientific_data import (
    AxisTransform,
    coerce_numeric_sequence,
    dash_polyline,
    extrema_preserving_downsample,
    finite_runs,
    marching_squares,
    uncertainty_hatch_indices,
)
from .scientific_math import MATH_LAYOUT_ID, layout_math
from .stroke_font import stroke_text, text_width_mm
from .svgkit import reliable_vector_strokes
from .vector_path import VectorPath, VectorPathError


ACADEMIC_SCHEMA_VERSION = 1
ACADEMIC_CATALOG_ID = "academic-artwork-v1"
ACADEMIC_PENS = (
    "grey-0-25",
    "green-0-25",
    "green-0-4",
    "blue-0-25",
    "blue-0-4",
    "red-0-25",
    "red-0-4",
    "purple-0-25",
    "purple-0-4",
    "black-0-25",
    "black-0-4",
    "black-0-6",
    "black-1",
)

_STABLE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FORMATS = frozenset(
    {
        "a5-portrait",
        "a5-landscape",
        "a4-portrait",
        "a4-landscape",
        "a3-portrait",
        "a3-landscape",
    }
)
_SOURCE_KINDS = frozenset(
    {
        "tabular-data",
        "vector-figure",
        "equation",
        "schematic",
        "device-geometry",
        "scalar-field",
        "microscopy-segmentation",
        "molecular-coordinates",
        "crystal-structure",
        "campus-map",
        "bibliographic-metadata",
        "patent-drawing",
        "user-text",
        "project-authored",
    }
)
_RIGHTS_BASES = frozenset(
    {
        "user-owned",
        "author-supplied",
        "institutionally-permitted",
        "public-domain",
        "open-license",
        "other-cleared",
        "project-authored",
    }
)
_RIGHTS_STATUSES = frozenset({"commercial-clear", "project-authored", "review-required"})
_COMPOSITION_MODES = frozenset(
    {
        "reconstructed-from-data",
        "rights-cleared-vector-reinterpretation",
        "direct-reproduction",
        "original-academic-composition",
    }
)
_TITLE_LAYOUTS = frozenset(
    {"auto", "compact-title", "wrapped-title", "side-title", "title-block"}
)
_ELEMENT_KINDS = frozenset(
    {
        "graph",
        "equation",
        "schematic",
        "device",
        "structure",
        "scalar-field",
        "vector",
        "text-block",
        "timeline",
        "campus",
        "patent-views",
        "raster-review",
    }
)


@dataclass(frozen=True)
class AcademicPreset:
    id: str
    label: str
    commercial_use: str
    composition: str
    allowed_kinds: frozenset[str]
    required_any: frozenset[str]
    minimum_elements: int
    maximum_elements: int
    required_metadata: tuple[str, ...] = ()
    default_format: str = "a4-portrait"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "commercial_use": self.commercial_use,
            "composition": self.composition,
            "allowed_kinds": sorted(self.allowed_kinds),
            "required_any": sorted(self.required_any),
            "minimum_elements": self.minimum_elements,
            "maximum_elements": self.maximum_elements,
            "required_metadata": list(self.required_metadata),
            "default_format": self.default_format,
        }


_ALL_SCIENCE = frozenset(_ELEMENT_KINDS - {"raster-review"})
ACADEMIC_PRESETS: dict[str, AcademicPreset] = {
    "paper-frontispiece": AcademicPreset(
        "paper-frontispiece",
        "Paper Frontispiece",
        "Author, supervisor, defence, or publication gift",
        "frontispiece",
        _ALL_SCIENCE,
        frozenset({"graph", "vector", "scalar-field", "equation", "device", "structure"}),
        1,
        3,
        ("authors", "year", "doi"),
    ),
    "figure-as-art": AcademicPreset(
        "figure-as-art",
        "Figure as Art",
        "Rights-cleared scientific figure re-layout",
        "figure",
        _ALL_SCIENCE,
        frozenset({"graph", "vector", "scalar-field", "schematic"}),
        1,
        6,
        default_format="a4-landscape",
    ),
    "graph-as-landscape": AcademicPreset(
        "graph-as-landscape",
        "Graph as Landscape",
        "Spectra, loops, waveforms, and quantitative records",
        "hero",
        frozenset({"graph", "text-block"}),
        frozenset({"graph"}),
        1,
        3,
        default_format="a4-landscape",
    ),
    "equation-centrepiece": AcademicPreset(
        "equation-centrepiece",
        "Equation Centrepiece",
        "One equation or compact derivation with definitions",
        "centred",
        frozenset({"equation", "text-block", "graph", "schematic"}),
        frozenset({"equation"}),
        1,
        4,
    ),
    "device-blueprint": AcademicPreset(
        "device-blueprint",
        "Device Blueprint",
        "Chip, circuit, optical, mechanical, or fluidic design",
        "technical",
        frozenset({"device", "vector", "text-block"}),
        frozenset({"device", "vector"}),
        1,
        4,
        default_format="a3-landscape",
    ),
    "experimental-path": AcademicPreset(
        "experimental-path",
        "Experimental Path",
        "Factually connected experimental setup",
        "flow",
        frozenset({"schematic", "text-block"}),
        frozenset({"schematic"}),
        1,
        3,
        ("researcher", "date"),
        "a3-landscape",
    ),
    "thesis-portrait": AcademicPreset(
        "thesis-portrait",
        "Thesis Portrait",
        "Thesis or dissertation milestone artwork",
        "thesis",
        _ALL_SCIENCE,
        frozenset({"graph", "equation", "schematic", "device", "structure", "scalar-field", "vector"}),
        1,
        3,
        ("author", "degree", "institution", "year", "supervisors"),
        "a3-portrait",
    ),
    "graduation-coordinates": AcademicPreset(
        "graduation-coordinates",
        "Graduation Coordinates",
        "Campus geometry with degree and subject motif",
        "graduation",
        frozenset({"campus", "vector", "equation", "device", "structure", "text-block"}),
        frozenset({"campus"}),
        1,
        4,
        ("graduate_name", "degree", "institution", "date", "coordinates"),
    ),
    "research-journey": AcademicPreset(
        "research-journey",
        "Research Journey",
        "Career, project, or publication timeline",
        "timeline",
        frozenset({"timeline", "graph", "equation", "vector", "text-block"}),
        frozenset({"timeline"}),
        1,
        6,
        default_format="a3-landscape",
    ),
    "patent-invention-plate": AcademicPreset(
        "patent-invention-plate",
        "Patent and Invention Plate",
        "Generic technical plate for supplied invention geometry",
        "patent",
        frozenset({"patent-views", "vector", "device", "text-block"}),
        frozenset({"patent-views", "vector", "device"}),
        1,
        5,
        ("inventors", "patent_number"),
        "a3-landscape",
    ),
    "molecular-crystal-structure": AcademicPreset(
        "molecular-crystal-structure",
        "Molecular or Crystal Structure",
        "Structure, projection, unit cell, and selected sites",
        "structure",
        frozenset({"structure", "text-block"}),
        frozenset({"structure"}),
        1,
        3,
    ),
    "microscopy-contour-study": AcademicPreset(
        "microscopy-contour-study",
        "Microscopy Contour Study",
        "Supplied segmentation or scalar data as contours",
        "contour",
        frozenset({"scalar-field", "vector", "text-block"}),
        frozenset({"scalar-field", "vector"}),
        1,
        4,
        default_format="a4-landscape",
    ),
    "publication-collection": AcademicPreset(
        "publication-collection",
        "Publication Collection",
        "Three to twelve related research results",
        "collection",
        _ALL_SCIENCE,
        frozenset({"graph", "equation", "vector", "device", "structure", "scalar-field"}),
        3,
        12,
        default_format="a3-landscape",
    ),
    "laboratory-identity": AcademicPreset(
        "laboratory-identity",
        "Laboratory Identity",
        "Original non-logo artwork for a research field",
        "identity",
        _ALL_SCIENCE,
        frozenset({"schematic", "device", "equation", "structure", "graph", "campus"}),
        2,
        6,
        ("laboratory",),
        "a3-landscape",
    ),
    "scientific-minimalism": AcademicPreset(
        "scientific-minimalism",
        "Scientific Minimalism",
        "Fast-to-plot single intellectual motif",
        "minimal",
        _ALL_SCIENCE,
        frozenset({"graph", "equation", "structure", "device", "vector"}),
        1,
        1,
        default_format="a5-portrait",
    ),
    "captioned-result": AcademicPreset(
        "captioned-result",
        "Captioned Result",
        "Hero result with subordinate supplied caption",
        "captioned",
        _ALL_SCIENCE,
        frozenset({"graph", "vector", "scalar-field", "schematic", "device", "structure"}),
        2,
        3,
    ),
}


@dataclass(frozen=True)
class LoadedAcademicRecord:
    record: dict[str, Any]
    source_path: Path
    assets: dict[str, Any]
    asset_evidence: dict[str, dict[str, Any]]


def _fail(message: str) -> NoReturn:
    raise MapPlotterError(f"Invalid academic artwork data: {message}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object.")
    return value


def _array(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "a non-empty" if nonempty else "an"
        _fail(f"{label} must be {qualifier} array.")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text.")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    result = _text(value, label)
    if _STABLE_ID.fullmatch(result) is None:
        _fail(f"{label} must use lowercase letters, digits, and single hyphens.")
    return result


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        _fail(f"{label} must be a finite number.")
    return result


def _canonical_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail(f"content is not canonical JSON: {exc}")
    return hashlib.sha256(encoded).hexdigest()


def _reject_json_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"input JSON repeats object key {key!r}.")
        result[key] = value
    return result


def _validate_source(value: Any, index: int) -> dict[str, Any]:
    label = f"sources[{index}]"
    source = _object(value, label)
    for key in ("id", "kind", "rights_basis", "license", "attribution", "method"):
        if key not in source:
            _fail(f"{label} is missing {key!r}.")
    _identifier(source["id"], f"{label}.id")
    kind = _text(source["kind"], f"{label}.kind")
    if kind not in _SOURCE_KINDS:
        _fail(f"{label}.kind {kind!r} is unsupported.")
    rights_basis = _text(source["rights_basis"], f"{label}.rights_basis")
    if rights_basis not in _RIGHTS_BASES:
        _fail(f"{label}.rights_basis {rights_basis!r} is unsupported.")
    for key in ("license", "attribution", "method"):
        _text(source[key], f"{label}.{key}")
    if source.get("branding") not in {None, False}:
        _fail(f"{label} requests branding; logos, crests, seals, and trade dress are not generated.")
    asset_path = source.get("asset_path")
    asset_sha256 = source.get("asset_sha256")
    if (asset_path is None) != (asset_sha256 is None):
        _fail(f"{label}.asset_path and asset_sha256 must appear together.")
    if asset_path is not None:
        asset_text = _text(asset_path, f"{label}.asset_path")
        suffix = Path(asset_text).suffix.casefold()
        if suffix not in {".csv", ".json", ".svg"}:
            _fail(f"{label}.asset_path must be CSV, JSON, or SVG; publisher PDFs and raster screenshots are not ingested.")
        digest = _text(asset_sha256, f"{label}.asset_sha256")
        if _SHA256.fullmatch(digest) is None:
            _fail(f"{label}.asset_sha256 must be a lowercase SHA-256.")
    if "url" in source:
        url = _text(source["url"], f"{label}.url")
        if not url.startswith("https://"):
            _fail(f"{label}.url must use HTTPS.")
    if "reproduction_permission" in source and not isinstance(
        source["reproduction_permission"], bool
    ):
        _fail(f"{label}.reproduction_permission must be boolean.")
    return source


def _validate_transformation(value: Any, label: str) -> dict[str, Any]:
    transformation = _object(value, label)
    for key in ("id", "description", "parameters", "changes_values"):
        if key not in transformation:
            _fail(f"{label} is missing {key!r}.")
    _identifier(transformation["id"], f"{label}.id")
    _text(transformation["description"], f"{label}.description")
    _object(transformation["parameters"], f"{label}.parameters")
    if not isinstance(transformation["changes_values"], bool):
        _fail(f"{label}.changes_values must be boolean.")
    if transformation["changes_values"]:
        _fail(
            f"{label} asks to change scientific values; this renderer supports only coordinate-preserving representation transforms."
        )
    forbidden = {"smooth", "smoothing", "interpolate_missing", "impute"}
    requested = forbidden.intersection(transformation["parameters"])
    if requested:
        _fail(
            f"{label}.parameters requests unsupported value-changing operation(s): "
            + ", ".join(sorted(requested))
            + "."
        )
    return transformation


def _validate_element(value: Any, index: int, source_ids: set[str]) -> dict[str, Any]:
    label = f"elements[{index}]"
    element = _object(value, label)
    for key in ("id", "kind", "source_refs", "transformation"):
        if key not in element:
            _fail(f"{label} is missing {key!r}.")
    _identifier(element["id"], f"{label}.id")
    kind = _text(element["kind"], f"{label}.kind")
    if kind not in _ELEMENT_KINDS:
        _fail(f"{label}.kind {kind!r} is unsupported.")
    refs = _array(element["source_refs"], f"{label}.source_refs", nonempty=True)
    checked_refs = [_identifier(ref, f"{label}.source_refs[{i}]") for i, ref in enumerate(refs)]
    unknown = sorted(set(checked_refs) - source_ids)
    if unknown:
        _fail(f"{label}.source_refs names unknown source(s): {', '.join(unknown)}.")
    _validate_transformation(element["transformation"], f"{label}.transformation")
    if element.get("branding") not in {None, False}:
        _fail(f"{label} requests protected branding; use institution names as plain factual text.")
    if "input_sha256" in element:
        supplied = _text(element["input_sha256"], f"{label}.input_sha256")
        if _SHA256.fullmatch(supplied) is None:
            _fail(f"{label}.input_sha256 must be a lowercase SHA-256.")
        payload = {key: item for key, item in element.items() if key != "input_sha256"}
        if supplied != _canonical_sha256(payload):
            _fail(f"{label}.input_sha256 disagrees with its canonical element payload.")
    return element


def _metadata_present(metadata: Mapping[str, Any], key: str) -> bool:
    value = metadata.get(key)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return bool(value)
    return value is not None


def validate_academic_record(value: Any) -> dict[str, Any]:
    """Validate one customer-supplied composition without loading external assets."""

    record = copy.deepcopy(_object(value, "record"))
    required = {
        "schema_version",
        "id",
        "preset",
        "format_id",
        "title",
        "subtitle",
        "credit_line",
        "rights_status",
        "composition_mode",
        "metadata",
        "sources",
        "elements",
    }
    missing = sorted(required - set(record))
    if missing:
        _fail("record is missing fields: " + ", ".join(missing) + ".")
    if record["schema_version"] != ACADEMIC_SCHEMA_VERSION:
        _fail(f"record.schema_version must be {ACADEMIC_SCHEMA_VERSION}.")
    _identifier(record["id"], "record.id")
    preset_id = _text(record["preset"], "record.preset")
    try:
        preset = ACADEMIC_PRESETS[preset_id]
    except KeyError as exc:
        _fail(f"record.preset {preset_id!r} is unknown.")
        raise AssertionError from exc
    format_id = _text(record["format_id"], "record.format_id")
    if format_id not in _FORMATS:
        _fail(f"record.format_id {format_id!r} is not a binding plate format.")
    _text(record["title"], "record.title")
    _text(record["subtitle"], "record.subtitle")
    credit_line = _text(record["credit_line"], "record.credit_line")
    if not 1 <= len([line for line in credit_line.split(" | ") if line.strip()]) <= 2:
        _fail("record.credit_line must contain one or two visible lines separated by ' | '.")
    rights_status = _text(record["rights_status"], "record.rights_status")
    if rights_status not in _RIGHTS_STATUSES:
        _fail(f"record.rights_status {rights_status!r} is unsupported.")
    mode = _text(record["composition_mode"], "record.composition_mode")
    if mode not in _COMPOSITION_MODES:
        _fail(f"record.composition_mode {mode!r} is unsupported.")
    title_layout = record.get("title_layout", "auto")
    if title_layout not in _TITLE_LAYOUTS:
        _fail(f"record.title_layout {title_layout!r} is unsupported.")
    if "plate_title" in record:
        _text(record["plate_title"], "record.plate_title")
    metadata = _object(record["metadata"], "record.metadata")
    for key in preset.required_metadata:
        if not _metadata_present(metadata, key):
            _fail(f"preset {preset_id!r} requires record.metadata.{key}.")
    sources = _array(record["sources"], "record.sources", nonempty=True)
    checked_sources = [_validate_source(source, index) for index, source in enumerate(sources)]
    source_ids = [str(source["id"]) for source in checked_sources]
    if len(source_ids) != len(set(source_ids)):
        _fail("record.sources repeats a source ID.")
    if mode == "direct-reproduction" and not any(
        source.get("reproduction_permission") is True for source in checked_sources
    ):
        _fail("direct reproduction requires an explicitly permitted source.")
    elements = _array(record["elements"], "record.elements", nonempty=True)
    checked_elements = [
        _validate_element(element, index, set(source_ids))
        for index, element in enumerate(elements)
    ]
    element_ids = [str(element["id"]) for element in checked_elements]
    if len(element_ids) != len(set(element_ids)):
        _fail("record.elements repeats an element ID.")
    count = len(checked_elements)
    if not preset.minimum_elements <= count <= preset.maximum_elements:
        _fail(
            f"preset {preset_id!r} needs {preset.minimum_elements} to "
            f"{preset.maximum_elements} elements; found {count}."
        )
    kinds = {str(element["kind"]) for element in checked_elements}
    unsupported = sorted(kinds - preset.allowed_kinds - {"raster-review"})
    if unsupported:
        _fail(f"preset {preset_id!r} does not admit element kind(s): {', '.join(unsupported)}.")
    if not kinds.intersection(preset.required_any):
        _fail(
            f"preset {preset_id!r} requires at least one of: "
            + ", ".join(sorted(preset.required_any))
            + "."
        )
    if preset_id == "captioned-result" and "text-block" not in kinds:
        _fail("captioned-result requires a text-block element containing the supplied caption.")
    if preset_id == "paper-frontispiece" and "text-block" not in kinds and not any(
        _metadata_present(metadata, key)
        for key in (
            "significance_statement",
            "result_statement",
            "caption",
            "abstract_excerpt",
        )
    ):
        _fail(
            "paper-frontispiece requires a supplied caption or significance/result statement."
        )
    if (
        preset_id == "equation-centrepiece"
        and "text-block" not in kinds
        and not _metadata_present(metadata, "variable_definitions")
    ):
        _fail(
            "equation-centrepiece requires supplied variable_definitions or a text-block definition panel."
        )
    if preset_id == "experimental-path" and not any(
        _metadata_present(metadata, key) for key in ("institution", "laboratory")
    ):
        _fail("experimental-path requires a supplied institution or laboratory name.")
    if preset_id == "patent-invention-plate" and not any(
        _metadata_present(metadata, key) for key in ("filing_date", "grant_date")
    ):
        _fail("patent-invention-plate requires a supplied filing_date or grant_date.")
    if preset_id == "graduation-coordinates" and "campus" not in kinds:
        _fail("graduation-coordinates requires a campus element sourced through cleared geometry.")
    if any(element["kind"] == "raster-review" for element in checked_elements):
        record["review_required"] = True
        record["limitations"] = [
            "Raster screenshots are not production inputs. Supply underlying data, segmentation, or rights-cleared vectors for a reviewed reconstruction."
        ]
    else:
        record["review_required"] = False
        record["limitations"] = []
    details = record.get("details", [])
    if not isinstance(details, list) or len(details) > 3:
        _fail("record.details must contain at most three plate-detail lines.")
    for index, detail in enumerate(details):
        _text(detail, f"record.details[{index}]")
    notes = record.get("notes", [])
    if not isinstance(notes, list):
        _fail("record.notes must be an array.")
    for index, note in enumerate(notes):
        _text(note, f"record.notes[{index}]")
    return record


def load_academic_record(path: Path) -> LoadedAcademicRecord:
    """Load one JSON specification and its explicitly referenced local assets."""

    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_json_duplicates,
            parse_constant=lambda value: (_fail(f"input JSON may not contain {value}.")),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MapPlotterError(f"Could not read academic artwork input {path}: {exc}") from exc
    checked = validate_academic_record(raw)
    assets: dict[str, Any] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for source in checked["sources"]:
        asset_path = source.get("asset_path")
        if asset_path is None:
            continue
        resolved = resolve_local_asset(path, str(asset_path))
        digest = str(source["asset_sha256"])
        suffix = resolved.suffix.casefold()
        loaded: Any
        if suffix == ".csv":
            loaded = load_csv_columns(resolved, digest)
            loader = "strict-numeric-csv-v1"
        elif suffix == ".json":
            loaded = load_json_asset(resolved, digest)
            loader = "strict-json-v1"
        elif suffix == ".svg":
            loaded = extract_svg_linework(resolved, digest)
            loader = "strict-svg-linework-v1"
        else:  # pragma: no cover - validation rejects this first.
            _fail(f"unsupported asset suffix {suffix!r}.")
        source_id = str(source["id"])
        assets[source_id] = loaded
        evidence[source_id] = {
            "path": str(asset_path),
            "sha256": digest,
            "loader": loader,
        }
    return LoadedAcademicRecord(checked, path.resolve(), assets, evidence)


def academic_presets() -> tuple[dict[str, Any], ...]:
    return tuple(ACADEMIC_PRESETS[key].as_dict() for key in ACADEMIC_PRESETS)


def _pen_for_role(context: PlateContext, role: str) -> str:
    text_nib = float(context.plate["nib_roles_mm"]["text"])
    annotation = "black-0-25" if text_nib <= 0.25 else "black-0-4"
    mapping = {
        "axis": "black-0-25",
        "measured": "black-0-4",
        "fit": "red-0-25",
        "comparison": "blue-0-25",
        "reference": "purple-0-25",
        "uncertainty": "grey-0-25",
        "annotation": annotation,
        "math": annotation,
        "accent": "red-0-4" if context.plate["sheet"] != "A5" else "red-0-25",
        "structure": "blue-0-4" if context.plate["sheet"] != "A5" else "blue-0-25",
        "component": "black-0-4",
        "optical": "blue-0-25",
        "electrical": "red-0-25",
        "fluidic": "green-0-25",
        "mechanical": "black-0-25",
        "data": "purple-0-25",
        "control": "grey-0-25",
    }
    try:
        return mapping[role]
    except KeyError as exc:
        _fail(f"unknown academic semantic pen role {role!r}.")
        raise AssertionError from exc


def _attributes(element: Mapping[str, Any], *, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    transformation = element["transformation"]
    payload = {key: value for key, value in element.items() if key != "input_sha256"}
    result = {
        "data-academic-element": str(element["id"]),
        "data-source-refs": ",".join(str(ref) for ref in element["source_refs"]),
        "data-transformation": str(transformation["id"]),
        "data-input-sha256": _canonical_sha256(payload),
    }
    result.update(extra or {})
    return result


def _source_asset(element: Mapping[str, Any], assets: Mapping[str, Any]) -> Any | None:
    found = [assets[ref] for ref in element["source_refs"] if ref in assets]
    if len(found) > 1:
        _fail(f"element {element['id']!r} references multiple loaded assets; combine them explicitly in JSON.")
    return found[0] if found else None


def _working_field(context: PlateContext) -> Rect:
    return context.field.inset(float(context.plate["gap_mm"]))


def _grid_slots(rect: Rect, count: int, gap: float) -> list[Rect]:
    if count <= 0:
        return []
    if count == 1:
        return [rect]
    columns = math.ceil(math.sqrt(count * rect.width / rect.height))
    columns = min(count, max(1, columns))
    rows = math.ceil(count / columns)
    width = (rect.width - gap * (columns - 1)) / columns
    height = (rect.height - gap * (rows - 1)) / rows
    if min(width, height) <= 2 * gap:
        _fail("selected format cannot carry this many scientific panels at a physical size.")
    return [
        Rect(
            rect.x + column * (width + gap),
            rect.y + row * (height + gap),
            width,
            height,
        )
        for row in range(rows)
        for column in range(columns)
    ][:count]


def _slots_for(
    preset: AcademicPreset,
    elements: Sequence[Mapping[str, Any]],
    rect: Rect,
    gap: float,
    context: PlateContext,
) -> list[Rect]:
    if preset.composition == "minimal":
        return [rect.inset(gap)]
    if preset.composition == "captioned":
        captions = [index for index, element in enumerate(elements) if element["kind"] == "text-block"]
        if len(captions) != 1 or captions[0] != len(elements) - 1:
            _fail("captioned-result must place its one text-block caption last.")
        cap = float(context.plate["type_scale_mm"]["detail"])
        line_height = cap + 4.0 * 0.25
        caption_element = elements[-1]
        requested_lines = int(caption_element.get("maximum_lines", 4))
        caption_height = requested_lines * line_height + 2 * gap
        if caption_height >= rect.height - 4 * gap:
            _fail("caption block consumes the scientific hero field at this format.")
        hero = Rect(rect.x, rect.y, rect.width, rect.height - caption_height - gap)
        caption = Rect(rect.x, hero.bottom + gap, rect.width, caption_height)
        hero_slots = _grid_slots(hero, len(elements) - 1, gap)
        return [*hero_slots, caption]
    if preset.composition in {"flow", "timeline", "technical", "patent"}:
        if len(elements) == 1:
            return [rect]
        width = (rect.width - gap * (len(elements) - 1)) / len(elements)
        if width <= 2 * gap:
            _fail("selected landscape flow has too many panels for the format.")
        return [Rect(rect.x + index * (width + gap), rect.y, width, rect.height) for index in range(len(elements))]
    return _grid_slots(rect, len(elements), gap)


def _add_physical_text(
    layer: ArtworkLayer,
    text: str,
    *,
    x: float,
    y: float,
    cap: float,
    maximum_width: float,
    role: str,
    source_ref: str | None,
    attributes: Mapping[str, str] | None = None,
    angle_deg: float = 0.0,
) -> float:
    natural_width = text_width_mm(text, cap_height_mm=cap)
    fitted_cap = min(cap, cap * maximum_width / natural_width) if natural_width else cap
    floor = 8.0 * layer.pen.mark_width_mm
    if fitted_cap + 1e-9 < floor:
        _fail(f"text {text!r} cannot fit above the {floor:g} mm physical cap-height floor.")
    strokes = stroke_text(
        text,
        x_mm=x,
        y_mm=y,
        height_mm=fitted_cap,
        angle_deg=angle_deg,
    )
    strokes = reliable_vector_strokes(strokes, nib_mm=layer.pen.mark_width_mm)
    layer.add_many(
        strokes,
        source_ref=source_ref,
        role=role,
        attributes=dict(attributes or {}),
    )
    return fitted_cap


def _wrap_lines(text: str, *, cap: float, width: float) -> list[str]:
    paragraphs = text.split("\n")
    lines: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        if text_width_mm(current, cap_height_mm=cap) > width:
            _fail(f"unbreakable text token {current!r} exceeds its physical text block.")
        for word in words[1:]:
            candidate = f"{current} {word}"
            if text_width_mm(candidate, cap_height_mm=cap) <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
                if text_width_mm(current, cap_height_mm=cap) > width:
                    _fail(f"unbreakable text token {current!r} exceeds its physical text block.")
        lines.append(current)
    return lines


def _render_text_block(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    del assets
    text = _text(element.get("text"), f"element {element['id']}.text")
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    rect = slot.inset(min(gap, min(slot.width, slot.height) / 6))
    layer = artwork.layer(
        f"text_{element['id']}",
        f"Academic text: {element['id']}",
        _pen_for_role(context, "annotation"),
    )
    preferred = float(context.plate["type_scale_mm"]["detail"])
    floor = 8.0 * layer.pen.mark_width_mm
    maximum_lines = element.get("maximum_lines")
    if maximum_lines is not None and (
        isinstance(maximum_lines, bool) or not isinstance(maximum_lines, int) or maximum_lines < 1
    ):
        _fail(f"element {element['id']}.maximum_lines must be a positive integer.")
    selected_cap = preferred
    while selected_cap + 1e-9 >= floor:
        lines = _wrap_lines(text, cap=selected_cap, width=rect.width)
        line_gap = 4.0 * layer.pen.mark_width_mm
        height = len(lines) * selected_cap + max(len(lines) - 1, 0) * line_gap
        if height <= rect.height + 1e-9 and (
            maximum_lines is None or len(lines) <= maximum_lines
        ):
            break
        selected_cap -= 0.05
    else:
        _fail(f"text block {element['id']!r} cannot fit without microscopic lettering.")
    y = rect.y + (rect.height - height) / 2
    attributes = _attributes(element)
    for line_index, line in enumerate(lines):
        if not line:
            continue
        _add_physical_text(
            layer,
            line,
            x=rect.x,
            y=y + line_index * (selected_cap + line_gap),
            cap=selected_cap,
            maximum_width=rect.width,
            role=str(element.get("text_role", "caption")),
            source_ref=str(element["source_refs"][0]),
            attributes={**attributes, "data-text-line-index": str(line_index)},
        )
    return {
        "element_id": element["id"],
        "kind": "text-block",
        "line_count": len(lines),
        "cap_height_mm": round(selected_cap, 6),
        "source_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "line_breaking": "word-boundary-physical-fit-v1",
    }


def _resolved_values(raw: Any, asset: Any, label: str) -> tuple[float | None, ...]:
    if isinstance(raw, str):
        if not isinstance(asset, Mapping) or raw not in asset:
            _fail(f"{label} names missing asset column {raw!r}.")
        raw = asset[raw]
    return coerce_numeric_sequence(raw, label)


def _sample(values: Sequence[float | None], index: int, label: str) -> float:
    value = values[index]
    if value is None:
        _fail(f"{label}[{index}] is missing where a finite sample is required.")
    return value


def _axis_transform(value: Any, label: str) -> tuple[AxisTransform, dict[str, Any]]:
    axis = _object(value, label)
    for key in ("scale", "minimum", "maximum", "label", "unit"):
        if key not in axis:
            _fail(f"{label} is missing {key!r}.")
    scale = _text(axis["scale"], f"{label}.scale")
    minimum = _number(axis["minimum"], f"{label}.minimum")
    maximum = _number(axis["maximum"], f"{label}.maximum")
    _text(axis["label"], f"{label}.label")
    _text(axis["unit"], f"{label}.unit")
    transform = AxisTransform(scale, minimum, maximum)
    ticks = axis.get("ticks", [])
    if not isinstance(ticks, list):
        _fail(f"{label}.ticks must be an array.")
    seen: set[float] = set()
    for index, tick_value in enumerate(ticks):
        tick = _object(tick_value, f"{label}.ticks[{index}]")
        if "value" not in tick or "label" not in tick:
            _fail(f"{label}.ticks[{index}] needs value and label.")
        numeric = _number(tick["value"], f"{label}.ticks[{index}].value")
        _text(tick["label"], f"{label}.ticks[{index}].label")
        if numeric in seen:
            _fail(f"{label}.ticks repeats value {numeric:g}.")
        seen.add(numeric)
        normalized = transform.normalized(numeric)
        if not -1e-12 <= normalized <= 1 + 1e-12:
            _fail(f"{label}.ticks[{index}] lies outside the explicit axis limits.")
    return transform, axis


def _axis_copy(axis: Mapping[str, Any]) -> str:
    return f"{axis['label']} [{axis['unit']}]"


def _graph_rect(slot: Rect, context: PlateContext, annotation_nib: float) -> Rect:
    gap = float(context.plate["gap_mm"])
    annotation_cap = max(
        8.0 * annotation_nib,
        float(context.plate["type_scale_mm"]["attribution"]),
    )
    left = gap + annotation_cap * 2.2
    right = gap
    top = gap + annotation_cap * 1.5
    bottom = gap + annotation_cap * 2.0
    if slot.width <= left + right or slot.height <= top + bottom:
        _fail("graph panel is too small for physical axes, labels, and units.")
    return Rect(slot.x + left, slot.y + top, slot.width - left - right, slot.height - top - bottom)


def _project_graph_point(
    x: float,
    y: float,
    x_axis: AxisTransform,
    y_axis: AxisTransform,
    rect: Rect,
    label: str,
) -> tuple[float, float]:
    x_normalized = x_axis.normalized(x)
    y_normalized = y_axis.normalized(y)
    if not (-1e-9 <= x_normalized <= 1 + 1e-9) or not (
        -1e-9 <= y_normalized <= 1 + 1e-9
    ):
        _fail(f"{label} lies outside the supplied axis limits; cropping must be an explicit reviewed choice.")
    return (
        rect.x + min(1.0, max(0.0, x_normalized)) * rect.width,
        rect.bottom - min(1.0, max(0.0, y_normalized)) * rect.height,
    )


def _add_graph_axes(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    x_axis: AxisTransform,
    x_spec: Mapping[str, Any],
    y_axis: AxisTransform,
    y_spec: Mapping[str, Any],
) -> Rect:
    context = artwork.context
    axis_layer = artwork.layer(
        f"axes_{element['id']}",
        f"Axes: {element['id']}",
        _pen_for_role(context, "axis"),
    )
    annotation = artwork.layer(
        f"annotations_{element['id']}",
        f"Scientific labels: {element['id']}",
        _pen_for_role(context, "annotation"),
    )
    rect = _graph_rect(slot, context, annotation.pen.mark_width_mm)
    attributes = _attributes(element)
    axis_layer.add(
        [(rect.left, rect.top), (rect.left, rect.bottom), (rect.right, rect.bottom)],
        source_ref=str(element["source_refs"][0]),
        role="axes",
        attributes=attributes,
    )
    tick_length = 3.0 * axis_layer.pen.mark_width_mm
    cap = max(
        8.0 * annotation.pen.mark_width_mm,
        float(context.plate["type_scale_mm"]["attribution"]),
    )
    for axis_name, transform, spec in (
        ("x", x_axis, x_spec),
        ("y", y_axis, y_spec),
    ):
        ticks = spec.get("ticks", [])
        minimum_spacing = cap * 1.8
        positions: list[float] = []
        for index, tick in enumerate(ticks):
            normalized = transform.normalized(float(tick["value"]))
            if axis_name == "x":
                coordinate = rect.left + normalized * rect.width
                positions.append(coordinate)
                axis_layer.add(
                    [(coordinate, rect.bottom), (coordinate, rect.bottom - tick_length)],
                    source_ref=str(element["source_refs"][0]),
                    role="x-tick",
                    attributes={**attributes, "data-tick-index": str(index)},
                )
                label = str(tick["label"])
                label_width = text_width_mm(label, cap_height_mm=cap)
                _add_physical_text(
                    annotation,
                    label,
                    x=coordinate - label_width / 2,
                    y=rect.bottom + annotation.pen.mark_width_mm * 2,
                    cap=cap,
                    maximum_width=max(label_width, cap),
                    role="x-tick-label",
                    source_ref=str(element["source_refs"][0]),
                    attributes={**attributes, "data-tick-index": str(index)},
                )
            else:
                coordinate = rect.bottom - normalized * rect.height
                positions.append(coordinate)
                axis_layer.add(
                    [(rect.left, coordinate), (rect.left + tick_length, coordinate)],
                    source_ref=str(element["source_refs"][0]),
                    role="y-tick",
                    attributes={**attributes, "data-tick-index": str(index)},
                )
                label = str(tick["label"])
                label_width = text_width_mm(label, cap_height_mm=cap)
                _add_physical_text(
                    annotation,
                    label,
                    x=rect.left - annotation.pen.mark_width_mm * 2 - label_width,
                    y=coordinate - cap / 2,
                    cap=cap,
                    maximum_width=max(label_width, cap),
                    role="y-tick-label",
                    source_ref=str(element["source_refs"][0]),
                    attributes={**attributes, "data-tick-index": str(index)},
                )
        if any(
            second - first + 1e-9 < minimum_spacing
            for first, second in zip(sorted(positions), sorted(positions)[1:], strict=False)
        ):
            _fail(f"element {element['id']} has overlapping supplied {axis_name}-tick labels at physical size.")
    x_copy = _axis_copy(x_spec)
    x_width = text_width_mm(x_copy, cap_height_mm=cap)
    _add_physical_text(
        annotation,
        x_copy,
        x=rect.right - min(x_width, rect.width),
        y=slot.bottom - cap - annotation.pen.mark_width_mm,
        cap=cap,
        maximum_width=rect.width,
        role="x-axis-label",
        source_ref=str(element["source_refs"][0]),
        attributes=attributes,
    )
    _add_physical_text(
        annotation,
        _axis_copy(y_spec),
        x=rect.left,
        y=slot.top + annotation.pen.mark_width_mm,
        cap=cap,
        maximum_width=rect.width,
        role="y-axis-label",
        source_ref=str(element["source_refs"][0]),
        attributes=attributes,
    )
    return rect


def _trace_runs(
    trace: Mapping[str, Any],
    element: Mapping[str, Any],
    x_values: Sequence[float | None],
    y_values: Sequence[float | None],
) -> tuple[tuple[int, ...], ...]:
    breaks = trace.get("breaks_before", [])
    if not isinstance(breaks, list):
        _fail(f"trace {trace.get('id')!r}.breaks_before must be an array.")
    transformation = element["transformation"]
    if transformation["id"] == "extrema-preserving-downsample-v1":
        parameters = transformation["parameters"]
        target = parameters.get("max_points_per_run")
        if isinstance(target, bool) or not isinstance(target, int):
            _fail("extrema-preserving-downsample-v1 requires integer max_points_per_run.")
        protected = trace.get("protected_indices", [])
        if not isinstance(protected, list):
            _fail(f"trace {trace.get('id')!r}.protected_indices must be an array.")
        if "error_y" in trace and parameters.get("downsample_error_bars") is not True:
            _fail("downsampling a trace with error bars requires explicit downsample_error_bars=true.")
        return extrema_preserving_downsample(
            x_values,
            y_values,
            target_points_per_run=target,
            breaks_before=breaks,
            protected_indices=protected,
        ).runs
    if transformation["id"] not in {
        "identity",
        "axis-transform-only-v1",
        "uncertainty-hatching-v1",
        "multi-panel-relayout-v1",
    }:
        _fail(f"graph element uses unsupported transformation {transformation['id']!r}.")
    return finite_runs(x_values, y_values, breaks_before=breaks)


def _render_graph(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    x_axis, x_spec = _axis_transform(element.get("x_axis"), f"element {element['id']}.x_axis")
    y_axis, y_spec = _axis_transform(element.get("y_axis"), f"element {element['id']}.y_axis")
    rect = _add_graph_axes(artwork, element, slot, x_axis, x_spec, y_axis, y_spec)
    traces = _array(element.get("traces"), f"element {element['id']}.traces", nonempty=True)
    trace_ids: list[str] = []
    semantic_encodings: set[tuple[str, str, str]] = set()
    evidence: list[dict[str, Any]] = []
    asset = _source_asset(element, assets)
    attributes = _attributes(element)
    for trace_index, raw_trace in enumerate(traces):
        trace = _object(raw_trace, f"element {element['id']}.traces[{trace_index}]")
        trace_id = _identifier(trace.get("id"), f"trace[{trace_index}].id")
        if trace_id in trace_ids:
            _fail(f"graph element {element['id']!r} repeats trace {trace_id!r}.")
        trace_ids.append(trace_id)
        semantic_role = _text(trace.get("semantic_role"), f"trace {trace_id}.semantic_role")
        if semantic_role not in {"measured", "fit", "comparison", "reference"}:
            _fail(f"trace {trace_id}.semantic_role is unsupported.")
        line_style = _text(trace.get("line_style", "solid"), f"trace {trace_id}.line_style")
        if line_style not in {"solid", "dashed", "markers", "solid-markers"}:
            _fail(f"trace {trace_id}.line_style is unsupported.")
        marker = _text(trace.get("marker", "circle"), f"trace {trace_id}.marker")
        if marker not in {"circle", "cross", "square"}:
            _fail(f"trace {trace_id}.marker is unsupported.")
        encoding = (semantic_role, line_style, marker)
        if encoding in semantic_encodings:
            _fail(f"graph element {element['id']!r} repeats an indistinguishable trace encoding.")
        semantic_encodings.add(encoding)
        x_values = _resolved_values(trace.get("x"), asset, f"trace {trace_id}.x")
        y_values = _resolved_values(trace.get("y"), asset, f"trace {trace_id}.y")
        if len(x_values) != len(y_values):
            _fail(f"trace {trace_id} x/y arrays have different lengths.")
        runs = _trace_runs(trace, element, x_values, y_values)
        if not runs:
            _fail(f"trace {trace_id} has no two-point finite run.")
        layer = artwork.layer(
            f"{semantic_role}_{element['id']}_{trace_id}",
            f"{semantic_role.title()} trace: {trace_id}",
            _pen_for_role(artwork.context, semantic_role),
        )
        selected = {index for run in runs for index in run}
        error_bar_count = 0
        zero_error_bar_count = 0
        uncertainty_hatch_count = 0
        zero_width_uncertainty_hatch_count = 0
        uncertainty_run_count = 0
        uncertainty_singleton_count = 0
        for run_index, run in enumerate(runs):
            points = [
                _project_graph_point(
                    _sample(x_values, index, f"trace {trace_id}.x"),
                    _sample(y_values, index, f"trace {trace_id}.y"),
                    x_axis,
                    y_axis,
                    rect,
                    f"trace {trace_id}[{index}]",
                )
                for index in run
                if x_values[index] is not None and y_values[index] is not None
            ]
            path_attributes = {
                **attributes,
                "data-trace-id": trace_id,
                "data-source-index-start": str(run[0]),
                "data-source-index-end": str(run[-1]),
            }
            if line_style in {"solid", "solid-markers"}:
                layer.add(
                    points,
                    source_ref=str(element["source_refs"][0]),
                    role=f"{semantic_role}-trace",
                    sequence=run_index,
                    attributes=path_attributes,
                )
            elif line_style == "dashed":
                dashes = dash_polyline(
                    points,
                    dash_mm=6.0 * layer.pen.mark_width_mm,
                    gap_mm=3.0 * layer.pen.mark_width_mm,
                    minimum_stroke_mm=3.0 * layer.pen.mark_width_mm,
                )
                layer.add_many(
                    dashes,
                    source_ref=str(element["source_refs"][0]),
                    role=f"{semantic_role}-trace-dashed",
                    sequence=run_index,
                    attributes=path_attributes,
                )
        if line_style in {"markers", "solid-markers"}:
            radius = 2.0 * layer.pen.mark_width_mm
            for index in sorted(selected):
                x_value = x_values[index]
                y_value = y_values[index]
                assert x_value is not None and y_value is not None
                x_page, y_page = _project_graph_point(
                    x_value,
                    y_value,
                    x_axis,
                    y_axis,
                    rect,
                    f"trace {trace_id}[{index}]",
                )
                if marker == "circle":
                    strokes = [circle_stroke((x_page, y_page), radius, segments=16)]
                elif marker == "cross":
                    strokes = [
                        [(x_page - radius, y_page - radius), (x_page + radius, y_page + radius)],
                        [(x_page - radius, y_page + radius), (x_page + radius, y_page - radius)],
                    ]
                else:
                    strokes = [rectangle_stroke(Rect(x_page - radius, y_page - radius, 2 * radius, 2 * radius))]
                layer.add_many(
                    strokes,
                    source_ref=str(element["source_refs"][0]),
                    role=f"{semantic_role}-marker",
                    attributes={**attributes, "data-trace-id": trace_id, "data-source-index": str(index)},
                )
        if "error_y" in trace:
            errors = _resolved_values(trace["error_y"], asset, f"trace {trace_id}.error_y")
            if len(errors) != len(y_values):
                _fail(f"trace {trace_id}.error_y length differs from y.")
            error_layer = artwork.layer(
                f"error_bars_{element['id']}_{trace_id}",
                f"Error bars: {trace_id}",
                _pen_for_role(artwork.context, "uncertainty"),
            )
            cap_half = 1.5 * error_layer.pen.mark_width_mm
            for index in sorted(selected):
                if errors[index] is None or x_values[index] is None or y_values[index] is None:
                    continue
                error = _sample(errors, index, f"trace {trace_id}.error_y")
                if error < 0:
                    _fail(f"trace {trace_id}.error_y[{index}] is negative.")
                if error == 0:
                    x_page, y_page = _project_graph_point(
                        _sample(x_values, index, f"trace {trace_id}.x"),
                        _sample(y_values, index, f"trace {trace_id}.y"),
                        x_axis,
                        y_axis,
                        rect,
                        f"trace {trace_id} zero error[{index}]",
                    )
                    error_layer.add(
                        (
                            (x_page - cap_half, y_page),
                            (x_page + cap_half, y_page),
                        ),
                        source_ref=str(element["source_refs"][0]),
                        role="zero-error-bar",
                        attributes={
                            **attributes,
                            "data-trace-id": trace_id,
                            "data-source-index": str(index),
                            "data-zero-error": "true",
                        },
                    )
                    error_bar_count += 1
                    zero_error_bar_count += 1
                    continue
                x_page, low_page = _project_graph_point(
                    _sample(x_values, index, f"trace {trace_id}.x"),
                    _sample(y_values, index, f"trace {trace_id}.y") - error,
                    x_axis,
                    y_axis,
                    rect,
                    f"trace {trace_id} lower error[{index}]",
                )
                _, high_page = _project_graph_point(
                    _sample(x_values, index, f"trace {trace_id}.x"),
                    _sample(y_values, index, f"trace {trace_id}.y") + error,
                    x_axis,
                    y_axis,
                    rect,
                    f"trace {trace_id} upper error[{index}]",
                )
                minimum_error_length = 3.0 * error_layer.pen.mark_width_mm
                physical_error_length = abs(high_page - low_page)
                if physical_error_length + 1e-9 < minimum_error_length:
                    _fail(
                        f"trace {trace_id}.error_y[{index}] projects to "
                        f"{physical_error_length:.3f} mm, below the "
                        f"{minimum_error_length:g} mm pen floor; use a larger "
                        "format or an explicitly reviewed representation."
                    )
                error_layer.add_many(
                    (
                        ((x_page, low_page), (x_page, high_page)),
                        ((x_page - cap_half, low_page), (x_page + cap_half, low_page)),
                        ((x_page - cap_half, high_page), (x_page + cap_half, high_page)),
                    ),
                    source_ref=str(element["source_refs"][0]),
                    role="error-bar",
                    attributes={**attributes, "data-trace-id": trace_id, "data-source-index": str(index)},
                )
                error_bar_count += 1
        if "uncertainty" in trace:
            uncertainty = _object(trace["uncertainty"], f"trace {trace_id}.uncertainty")
            lower = _resolved_values(uncertainty.get("lower"), asset, f"trace {trace_id}.uncertainty.lower")
            upper = _resolved_values(uncertainty.get("upper"), asset, f"trace {trace_id}.uncertainty.upper")
            if len(lower) != len(y_values) or len(upper) != len(y_values):
                _fail(f"trace {trace_id} uncertainty boundaries must match y length.")
            hatch_count = uncertainty.get("hatch_strokes")
            if isinstance(hatch_count, bool) or not isinstance(hatch_count, int):
                _fail(f"trace {trace_id}.uncertainty.hatch_strokes must be an integer.")
            uncertainty_layer = artwork.layer(
                f"uncertainty_{element['id']}_{trace_id}",
                f"Uncertainty: {trace_id}",
                _pen_for_role(artwork.context, "uncertainty"),
            )
            uncertainty_runs: list[tuple[int, ...]] = []
            for trace_run in runs:
                current: list[int] = []
                for index in trace_run:
                    if lower[index] is None or upper[index] is None:
                        if current:
                            uncertainty_runs.append(tuple(current))
                        current = []
                        continue
                    lower_value = _sample(
                        lower, index, f"trace {trace_id}.uncertainty.lower"
                    )
                    upper_value = _sample(
                        upper, index, f"trace {trace_id}.uncertainty.upper"
                    )
                    if lower_value > upper_value:
                        _fail(
                            f"trace {trace_id} uncertainty lower[{index}] exceeds "
                            "the supplied upper boundary."
                        )
                    current.append(index)
                if current:
                    uncertainty_runs.append(tuple(current))
            if not uncertainty_runs:
                _fail(f"trace {trace_id} has no finite uncertainty samples.")
            uncertainty_run_count = len(uncertainty_runs)
            uncertainty_singleton_count = sum(
                len(boundary_run) == 1 for boundary_run in uncertainty_runs
            )
            for boundary_name, boundary in (("lower", lower), ("upper", upper)):
                for boundary_run_index, boundary_run in enumerate(uncertainty_runs):
                    if len(boundary_run) < 2:
                        continue
                    boundary_points = [
                        _project_graph_point(
                            _sample(x_values, index, f"trace {trace_id}.x"),
                            _sample(
                                boundary,
                                index,
                                f"trace {trace_id}.uncertainty.{boundary_name}",
                            ),
                            x_axis,
                            y_axis,
                            rect,
                            f"trace {trace_id} {boundary_name}[{index}]",
                        )
                        for index in boundary_run
                    ]
                    uncertainty_layer.add(
                        boundary_points,
                        source_ref=str(element["source_refs"][0]),
                        role=f"uncertainty-{boundary_name}-boundary",
                        sequence=boundary_run_index,
                        attributes={
                            **attributes,
                            "data-trace-id": trace_id,
                            "data-uncertainty-run": str(boundary_run_index),
                        },
                    )
            for boundary_run_index, boundary_run in enumerate(uncertainty_runs):
                hatch_positions = (
                    (0,)
                    if len(boundary_run) == 1
                    else uncertainty_hatch_indices(
                        len(boundary_run), maximum_strokes=hatch_count
                    )
                )
                for hatch_index in hatch_positions:
                    index = boundary_run[hatch_index]
                    first = _project_graph_point(
                        _sample(x_values, index, f"trace {trace_id}.x"),
                        _sample(lower, index, f"trace {trace_id}.uncertainty.lower"),
                        x_axis,
                        y_axis,
                        rect,
                        "uncertainty lower",
                    )
                    second = _project_graph_point(
                        _sample(x_values, index, f"trace {trace_id}.x"),
                        _sample(upper, index, f"trace {trace_id}.uncertainty.upper"),
                        x_axis,
                        y_axis,
                        rect,
                        "uncertainty upper",
                    )
                    hatch_length = polyline_length_mm([first, second])
                    minimum_hatch_length = 3.0 * uncertainty_layer.pen.mark_width_mm
                    if hatch_length <= 1e-9:
                        zero_width_uncertainty_hatch_count += 1
                        continue
                    if hatch_length + 1e-9 < minimum_hatch_length:
                        _fail(
                            f"trace {trace_id} uncertainty at source index {index} "
                            f"projects to {hatch_length:.3f} mm, below the "
                            f"{minimum_hatch_length:g} mm pen floor; use a larger "
                            "format or different explicit uncertainty representation."
                        )
                    uncertainty_layer.add(
                        [first, second],
                        source_ref=str(element["source_refs"][0]),
                        role="uncertainty-hatch",
                        attributes={
                            **attributes,
                            "data-trace-id": trace_id,
                            "data-source-index": str(index),
                            "data-uncertainty-run": str(boundary_run_index),
                        },
                    )
                    uncertainty_hatch_count += 1
        evidence.append(
            {
                "trace_id": trace_id,
                "semantic_role": semantic_role,
                "line_style": line_style,
                "input_samples": len(x_values),
                "finite_samples": sum(len(run) for run in finite_runs(x_values, y_values, breaks_before=trace.get("breaks_before", []))),
                "emitted_source_samples": len(selected),
                "run_count": len(runs),
                "error_bar_count": error_bar_count,
                "zero_error_bar_count": zero_error_bar_count,
                "uncertainty_hatches_emitted": uncertainty_hatch_count,
                "zero_width_uncertainty_hatches_not_drawn": zero_width_uncertainty_hatch_count,
                "uncertainty_run_count": uncertainty_run_count,
                "uncertainty_singleton_count": uncertainty_singleton_count,
                "missing_regions_preserved": True,
                "values_changed": False,
            }
        )
    annotations = element.get("annotations", [])
    if not isinstance(annotations, list):
        _fail(f"element {element['id']}.annotations must be an array.")
    annotation_layer = artwork.layer(
        f"point_annotations_{element['id']}",
        f"Point annotations: {element['id']}",
        _pen_for_role(artwork.context, "annotation"),
    )
    annotation_cap = max(8 * annotation_layer.pen.mark_width_mm, float(artwork.context.plate["type_scale_mm"]["attribution"]))
    for index, raw_annotation in enumerate(annotations):
        annotation = _object(raw_annotation, f"element {element['id']}.annotations[{index}]")
        x_value = _number(annotation.get("x"), f"annotation[{index}].x")
        y_value = _number(annotation.get("y"), f"annotation[{index}].y")
        label = _text(annotation.get("label"), f"annotation[{index}].label")
        point = _project_graph_point(x_value, y_value, x_axis, y_axis, rect, f"annotation[{index}]")
        leader_end = (min(rect.right, point[0] + 4 * annotation_layer.pen.mark_width_mm), max(rect.top, point[1] - 4 * annotation_layer.pen.mark_width_mm))
        annotation_layer.add(
            [point, leader_end],
            source_ref=str(element["source_refs"][0]),
            role="annotation-leader",
            attributes={**attributes, "data-annotation-index": str(index)},
        )
        _add_physical_text(
            annotation_layer,
            label,
            x=leader_end[0] + annotation_layer.pen.mark_width_mm,
            y=max(slot.top, leader_end[1] - annotation_cap),
            cap=annotation_cap,
            maximum_width=max(rect.right - leader_end[0], annotation_cap),
            role="annotation-label",
            source_ref=str(element["source_refs"][0]),
            attributes={**attributes, "data-annotation-index": str(index)},
        )
    return {
        "element_id": element["id"],
        "kind": "graph",
        "x_axis": {"scale": x_axis.scale, "minimum": x_axis.minimum, "maximum": x_axis.maximum, "label": x_spec["label"], "unit": x_spec["unit"]},
        "y_axis": {"scale": y_axis.scale, "minimum": y_axis.minimum, "maximum": y_axis.maximum, "label": y_spec["label"], "unit": y_spec["unit"]},
        "traces": evidence,
        "annotations": len(annotations),
        "smoothing": "none",
        "missing_values_imputed": False,
    }


def _render_equation(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    asset = _source_asset(element, assets)
    expression = element.get("expression", asset)
    if expression is None:
        _fail(f"equation element {element['id']!r} has no structured expression.")
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    rect = slot.inset(min(gap, min(slot.width, slot.height) / 6))
    primary = artwork.layer(
        f"equation_{element['id']}",
        f"Equation: {element['id']}",
        _pen_for_role(context, "math"),
    )
    accent = artwork.layer(
        f"equation_accent_{element['id']}",
        f"Equation accents: {element['id']}",
        _pen_for_role(context, "accent"),
    )
    cap = float(context.plate["type_scale_mm"]["title"])
    layout = layout_math(
        expression,
        cap_height_mm=cap,
        nib_mm=max(primary.pen.mark_width_mm, accent.pen.mark_width_mm),
        maximum_width_mm=rect.width,
        maximum_height_mm=rect.height,
    )
    x = rect.x + (rect.width - layout.width_mm) / 2
    y = rect.y + (rect.height - layout.height_mm) / 2
    attributes = _attributes(element, extra={"data-expression-sha256": layout.expression_sha256})
    for stroke in layout.placed(x, y):
        target = accent if stroke.pen_role == "accent" else primary
        target.add(
            stroke.points,
            source_ref=str(element["source_refs"][0]),
            role=stroke.semantic_role,
            attributes=attributes,
        )
    return {
        "element_id": element["id"],
        "kind": "equation",
        "renderer": MATH_LAYOUT_ID,
        "expression_sha256": layout.expression_sha256,
        "requested_cap_height_mm": layout.requested_cap_height_mm,
        "effective_cap_height_mm": round(layout.effective_cap_height_mm, 6),
        "arbitrary_line_breaks": False,
        "symbols_preserved": True,
    }


def _required_numeric(raw: Any, label: str) -> tuple[float, ...]:
    values = coerce_numeric_sequence(raw, label, allow_missing=False)
    result: list[float] = []
    for index, value in enumerate(values):
        if value is None:
            _fail(f"{label}[{index}] may not be missing.")
        result.append(value)
    return tuple(result)


def _scalar_value(raw: Any, asset: Any, label: str) -> Any:
    if isinstance(raw, str):
        if not isinstance(asset, Mapping) or raw not in asset:
            _fail(f"{label} names missing asset member {raw!r}.")
        return asset[raw]
    return raw


def _render_scalar_field(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    asset = _source_asset(element, assets)
    values = _scalar_value(element.get("values"), asset, f"element {element['id']}.values")
    x_raw = _scalar_value(element.get("x"), asset, f"element {element['id']}.x")
    y_raw = _scalar_value(element.get("y"), asset, f"element {element['id']}.y")
    levels_raw = _scalar_value(element.get("levels"), asset, f"element {element['id']}.levels")
    x_values = _required_numeric(x_raw, f"element {element['id']}.x")
    y_values = _required_numeric(y_raw, f"element {element['id']}.y")
    levels = _required_numeric(levels_raw, f"element {element['id']}.levels")
    contours = marching_squares(values, x=x_values, y=y_values, levels=levels)
    if not any(contour.paths for contour in contours):
        _fail(f"scalar-field element {element['id']!r} produces no selected contours.")
    mode = _text(element.get("mode", "sparse"), f"element {element['id']}.mode")
    if mode not in {"sparse", "rich"}:
        _fail(f"element {element['id']}.mode must be sparse or rich.")
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    rect = slot.inset(min(gap, min(slot.width, slot.height) / 6))
    span_x = x_values[-1] - x_values[0]
    span_y = y_values[-1] - y_values[0]
    if span_x <= 0 or span_y <= 0:
        _fail(f"scalar-field element {element['id']!r} has degenerate coordinates.")
    scale = min(rect.width / span_x, rect.height / span_y)
    used_width = span_x * scale
    used_height = span_y * scale
    offset_x = rect.x + (rect.width - used_width) / 2
    offset_y = rect.y + (rect.height - used_height) / 2

    def project(point: tuple[float, float]) -> tuple[float, float]:
        return (
            offset_x + (point[0] - x_values[0]) * scale,
            offset_y + (y_values[-1] - point[1]) * scale,
        )

    pen_roles = ("comparison", "structure", "reference", "measured")
    attributes = _attributes(element)
    level_records: list[dict[str, Any]] = []
    for level_index, contour in enumerate(contours):
        semantic_role = pen_roles[level_index % len(pen_roles)]
        layer = artwork.layer(
            f"contour_{element['id']}_{level_index}",
            f"Contour {contour.level:g}: {element['id']}",
            _pen_for_role(context, semantic_role),
        )
        emitted = 0
        for path_index, path in enumerate(contour.paths):
            page_path = [project(point) for point in path]
            length = polyline_length_mm(page_path)
            minimum = 3.0 * layer.pen.mark_width_mm
            if length + 1e-9 < minimum:
                _fail(
                    f"contour level {contour.level:g} contains a {length:.3f} mm component below the {minimum:g} mm pen floor; use a larger format or different explicit levels."
                )
            layer.add(
                page_path,
                source_ref=str(element["source_refs"][0]),
                role="selected-contour-level",
                sequence=path_index,
                attributes={
                    **attributes,
                    "data-contour-level": f"{contour.level:.12g}",
                    "data-contour-index": str(path_index),
                },
            )
            emitted += 1
        level_records.append(
            {
                "level": contour.level,
                "path_count": emitted,
                "cell_count": contour.cell_count,
                "ambiguous_cell_count": contour.ambiguous_cell_count,
                "algorithm": contour.algorithm,
            }
        )
    scale_bar = element.get("scale_bar")
    if scale_bar is not None:
        bar = _object(scale_bar, f"element {element['id']}.scale_bar")
        length_data = _number(bar.get("length"), f"element {element['id']}.scale_bar.length")
        if length_data <= 0:
            _fail(f"element {element['id']}.scale_bar.length must be positive.")
        unit = _text(bar.get("unit"), f"element {element['id']}.scale_bar.unit")
        label = _text(bar.get("label", f"{length_data:g} {unit}"), f"element {element['id']}.scale_bar.label")
        length_mm = length_data * scale
        if length_mm >= used_width:
            _fail(f"element {element['id']} scale bar exceeds the plotted field width.")
        bar_layer = artwork.layer(
            f"scale_{element['id']}",
            f"Scale bar: {element['id']}",
            _pen_for_role(context, "axis"),
        )
        annotation = artwork.layer(
            f"scale_label_{element['id']}",
            f"Scale label: {element['id']}",
            _pen_for_role(context, "annotation"),
        )
        bar_x = offset_x
        bar_y = offset_y + used_height - gap / 2
        tick = 3.0 * bar_layer.pen.mark_width_mm
        bar_layer.add(
            [(bar_x, bar_y), (bar_x + length_mm, bar_y)],
            source_ref=str(element["source_refs"][0]),
            role="scale-bar",
            attributes=attributes,
        )
        bar_layer.add_many(
            (
                ((bar_x, bar_y - tick / 2), (bar_x, bar_y + tick / 2)),
                ((bar_x + length_mm, bar_y - tick / 2), (bar_x + length_mm, bar_y + tick / 2)),
            ),
            source_ref=str(element["source_refs"][0]),
            role="scale-bar-tick",
            attributes=attributes,
        )
        cap = max(8 * annotation.pen.mark_width_mm, float(context.plate["type_scale_mm"]["attribution"]))
        _add_physical_text(
            annotation,
            label,
            x=bar_x,
            y=max(slot.top, bar_y - cap - tick),
            cap=cap,
            maximum_width=max(length_mm * 2, cap),
            role="scale-label",
            source_ref=str(element["source_refs"][0]),
            attributes=attributes,
        )
    return {
        "element_id": element["id"],
        "kind": "scalar-field",
        "mode": mode,
        "selected_levels": list(levels),
        "level_records": level_records,
        "raster_emitted": False,
        "values_changed": False,
        "missing_cells_skipped_not_imputed": True,
        "scale_bar_preserved": scale_bar is not None,
    }


def _inline_vector_paths(element: Mapping[str, Any]) -> list[tuple[VectorPath, str, str | None]]:
    raw_paths = _array(element.get("paths"), f"element {element['id']}.paths", nonempty=True)
    result: list[tuple[VectorPath, str, str | None]] = []
    for index, raw in enumerate(raw_paths):
        role = "unclassified"
        element_id: str | None = None
        path_payload = raw
        if isinstance(raw, dict) and "path" in raw:
            path_payload = raw["path"]
            role = str(raw.get("role", "unclassified"))
            element_id = str(raw["id"]) if raw.get("id") is not None else None
        try:
            path = VectorPath.from_dict(path_payload)
        except (VectorPathError, TypeError, ValueError) as exc:
            raise MapPlotterError(
                f"Invalid academic artwork data: element {element['id']}.paths[{index}] is not a canonical vector path: {exc}"
            ) from exc
        result.append((path, role, element_id))
    return result


def _vector_semantic(candidate: str) -> str:
    return {
        "axis": "axis",
        "tick": "axis",
        "trace": "measured",
        "fit": "fit",
        "error-bar": "uncertainty",
        "uncertainty": "uncertainty",
        "marker": "measured",
        "annotation": "annotation",
        "legend": "annotation",
        "panel": "reference",
        "unclassified": "component",
    }.get(candidate, "component")


def _render_vector(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    asset = _source_asset(element, assets)
    if isinstance(asset, SVGLinework):
        source_paths = [
            (record.path, record.candidate_role, record.element_id)
            for record in asset.paths
        ]
        parser = asset.parser
        source_digest = asset.content_sha256
        classification = asset.classification
    else:
        source_paths = _inline_vector_paths(element)
        parser = "canonical-vector-path-v1"
        source_digest = _canonical_sha256(element.get("paths"))
        classification = "explicit-role-or-unclassified-v1"
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    rect = slot.inset(min(gap, min(slot.width, slot.height) / 6))
    fitted = fit_vector_paths((path for path, _, _ in source_paths), rect)
    attributes = _attributes(element, extra={"data-vector-source-sha256": source_digest})
    role_counts: dict[str, int] = {}
    for index, (fitted_path, (_, candidate, source_element_id)) in enumerate(
        zip(fitted, source_paths, strict=True)
    ):
        semantic = _vector_semantic(candidate)
        layer = artwork.layer(
            f"vector_{element['id']}_{semantic}",
            f"Vector {semantic}: {element['id']}",
            _pen_for_role(context, semantic),
        )
        layer.add_path(
            fitted_path,
            source_ref=str(element["source_refs"][0]),
            role=f"vector-{candidate}",
            sequence=index,
            attributes={
                **attributes,
                "data-source-element-id": source_element_id or "",
                "data-vector-classification": candidate,
            },
        )
        role_counts[candidate] = role_counts.get(candidate, 0) + 1
    if element["kind"] == "campus":
        coordinates = _text(
            element.get("coordinates_label"),
            f"campus element {element['id']}.coordinates_label",
        )
        label_layer = artwork.layer(
            f"campus_coordinates_{element['id']}",
            f"Campus coordinates: {element['id']}",
            _pen_for_role(context, "annotation"),
        )
        cap = max(8 * label_layer.pen.mark_width_mm, float(context.plate["type_scale_mm"]["attribution"]))
        _add_physical_text(
            label_layer,
            coordinates,
            x=rect.left,
            y=rect.bottom - cap,
            cap=cap,
            maximum_width=rect.width,
            role="campus-coordinates",
            source_ref=str(element["source_refs"][0]),
            attributes=attributes,
        )
    return {
        "element_id": element["id"],
        "kind": element["kind"],
        "path_count": len(fitted),
        "source_sha256": source_digest,
        "parser": parser,
        "classification": classification,
        "candidate_role_counts": role_counts,
        "raster_elements_emitted": 0,
        "source_curves_preserved": True,
    }


def _normalized_point(value: Any, label: str, canvas: NormalizedCanvas) -> tuple[float, float]:
    raw = _array(value, label)
    if len(raw) != 2:
        _fail(f"{label} must be [x, y].")
    x = _number(raw[0], f"{label}[0]")
    y = _number(raw[1], f"{label}[1]")
    if not 0 <= x <= 100 or not 0 <= y <= 100:
        _fail(f"{label} must use normalized 0..100 coordinates.")
    return canvas.point(x, y)


def _component_strokes(
    component_type: str,
    rect: Rect,
    nib_mm: float,
) -> list[list[tuple[float, float]]]:
    centre = rect.centre
    if component_type in {"box", "sample", "detector", "controller", "amplifier", "stage"}:
        strokes = [rectangle_stroke(rect)]
        if component_type == "sample":
            strokes.extend(
                [
                    [(rect.left, rect.bottom), (rect.right, rect.top)],
                    [(rect.left, rect.centre[1]), (rect.centre[0], rect.top)],
                    [(rect.centre[0], rect.bottom), (rect.right, rect.centre[1])],
                ]
            )
        elif component_type == "detector":
            strokes.append(circle_stroke(centre, min(rect.width, rect.height) * 0.22, segments=16))
        elif component_type == "amplifier":
            strokes.append(
                [
                    (rect.left + nib_mm, rect.top + nib_mm),
                    (rect.right - nib_mm, centre[1]),
                    (rect.left + nib_mm, rect.bottom - nib_mm),
                    (rect.left + nib_mm, rect.top + nib_mm),
                ]
            )
        return strokes
    if component_type in {"source", "pump"}:
        radius = min(rect.width, rect.height) / 2
        strokes = [circle_stroke(centre, radius, segments=20)]
        if component_type == "source":
            ray = max(3 * nib_mm, radius * 0.7)
            strokes.extend(
                [
                    [(centre[0] - ray, centre[1]), (centre[0] + ray, centre[1])],
                    [(centre[0], centre[1] - ray), (centre[0], centre[1] + ray)],
                ]
            )
        else:
            strokes.extend(arrow_strokes([(centre[0] - radius * 0.5, centre[1]), (centre[0] + radius * 0.5, centre[1])], head_mm=max(3 * nib_mm, radius * 0.35)))
        return strokes
    if component_type == "lens":
        return [ellipse_stroke(centre, rect.width / 2, rect.height / 2, segments=32)]
    if component_type == "mirror":
        return [
            [(rect.left, rect.bottom), (rect.right, rect.top)],
            [(rect.left, rect.bottom - 3 * nib_mm), (rect.left + 3 * nib_mm, rect.bottom)],
            [(rect.right - 3 * nib_mm, rect.top), (rect.right, rect.top + 3 * nib_mm)],
        ]
    if component_type == "beam-splitter":
        return [
            rectangle_stroke(rect),
            [(rect.left, rect.bottom), (rect.right, rect.top)],
        ]
    if component_type == "resistor":
        y = centre[1]
        return [
            [
                (rect.left, y),
                (rect.left + rect.width * 0.18, y),
                (rect.left + rect.width * 0.28, rect.top),
                (rect.left + rect.width * 0.43, rect.bottom),
                (rect.left + rect.width * 0.58, rect.top),
                (rect.left + rect.width * 0.73, rect.bottom),
                (rect.left + rect.width * 0.82, y),
                (rect.right, y),
            ]
        ]
    if component_type == "capacitor":
        return [
            [(rect.left, centre[1]), (rect.centre[0] - 2 * nib_mm, centre[1])],
            [(rect.centre[0] - 2 * nib_mm, rect.top), (rect.centre[0] - 2 * nib_mm, rect.bottom)],
            [(rect.centre[0] + 2 * nib_mm, rect.top), (rect.centre[0] + 2 * nib_mm, rect.bottom)],
            [(rect.centre[0] + 2 * nib_mm, centre[1]), (rect.right, centre[1])],
        ]
    if component_type == "valve":
        return [
            [(rect.left, rect.top), (rect.centre[0], rect.centre[1]), (rect.left, rect.bottom), (rect.left, rect.top)],
            [(rect.right, rect.top), (rect.centre[0], rect.centre[1]), (rect.right, rect.bottom), (rect.right, rect.top)],
        ]
    _fail(f"unsupported schematic component type {component_type!r}.")


def _render_schematic(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    del assets
    components = _array(
        element.get("components"), f"element {element['id']}.components", nonempty=True
    )
    connections = _array(
        element.get("connections"), f"element {element['id']}.connections"
    )
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    rect = slot.inset(min(gap, min(slot.width, slot.height) / 6))
    canvas = NormalizedCanvas(rect)
    component_layer = artwork.layer(
        f"components_{element['id']}",
        f"Experimental components: {element['id']}",
        _pen_for_role(context, "component"),
    )
    label_layer = artwork.layer(
        f"component_labels_{element['id']}",
        f"Component labels: {element['id']}",
        _pen_for_role(context, "annotation"),
    )
    attributes = _attributes(element)
    ports: dict[tuple[str, str], tuple[float, float]] = {}
    component_ids: set[str] = set()
    component_records: list[dict[str, Any]] = []
    for index, raw_component in enumerate(components):
        component = _object(raw_component, f"element {element['id']}.components[{index}]")
        component_id = _identifier(component.get("id"), f"component[{index}].id")
        if component_id in component_ids:
            _fail(f"schematic element {element['id']!r} repeats component {component_id!r}.")
        component_ids.add(component_id)
        component_type = _text(component.get("type"), f"component {component_id}.type")
        label = _text(component.get("label"), f"component {component_id}.label")
        centre = _normalized_point(component.get("position"), f"component {component_id}.position", canvas)
        size = _array(component.get("size"), f"component {component_id}.size")
        if len(size) != 2:
            _fail(f"component {component_id}.size must be [width, height].")
        width = canvas.dx(_number(size[0], f"component {component_id}.size[0]"))
        height = canvas.dy(_number(size[1], f"component {component_id}.size[1]"))
        if width <= 0 or height <= 0:
            _fail(f"component {component_id}.size must be positive.")
        component_rect = Rect(centre[0] - width / 2, centre[1] - height / 2, width, height)
        if not (
            rect.left <= component_rect.left
            and component_rect.right <= rect.right
            and rect.top <= component_rect.top
            and component_rect.bottom <= rect.bottom
        ):
            _fail(f"component {component_id!r} leaves its scientific panel.")
        component_layer.add_many(
            _component_strokes(component_type, component_rect, component_layer.pen.mark_width_mm),
            source_ref=str(element["source_refs"][0]),
            role=f"component:{component_type}",
            attributes={**attributes, "data-component-id": component_id},
        )
        raw_ports = _object(component.get("ports"), f"component {component_id}.ports")
        if not raw_ports:
            _fail(f"component {component_id}.ports must not be empty.")
        for port_name, point in raw_ports.items():
            port_id = _identifier(port_name, f"component {component_id} port")
            normalized = _array(point, f"component {component_id}.ports.{port_id}")
            if len(normalized) != 2:
                _fail(f"component {component_id}.ports.{port_id} must be a relative [x, y].")
            px = _number(normalized[0], f"component {component_id}.ports.{port_id}[0]")
            py = _number(normalized[1], f"component {component_id}.ports.{port_id}[1]")
            if not -0.5 <= px <= 0.5 or not -0.5 <= py <= 0.5:
                _fail(f"component {component_id}.ports.{port_id} must lie inside relative -0.5..0.5 bounds.")
            ports[(component_id, port_id)] = (
                centre[0] + px * width,
                centre[1] + py * height,
            )
        cap = max(8 * label_layer.pen.mark_width_mm, float(context.plate["type_scale_mm"]["attribution"]))
        _add_physical_text(
            label_layer,
            label,
            x=component_rect.left,
            y=max(rect.top, component_rect.top - cap - label_layer.pen.mark_width_mm),
            cap=cap,
            maximum_width=max(component_rect.width, cap),
            role="component-label",
            source_ref=str(element["source_refs"][0]),
            attributes={**attributes, "data-component-id": component_id},
        )
        component_records.append(
            {"id": component_id, "type": component_type, "port_count": len(raw_ports)}
        )
    connection_ids: set[str] = set()
    path_type_counts: dict[str, int] = {}
    for index, raw_connection in enumerate(connections):
        connection = _object(raw_connection, f"element {element['id']}.connections[{index}]")
        connection_id = _identifier(connection.get("id"), f"connection[{index}].id")
        if connection_id in connection_ids:
            _fail(f"schematic element {element['id']!r} repeats connection {connection_id!r}.")
        connection_ids.add(connection_id)
        path_type = _text(connection.get("path_type"), f"connection {connection_id}.path_type")
        if path_type not in {"optical", "electrical", "fluidic", "mechanical", "data", "control"}:
            _fail(f"connection {connection_id}.path_type is unsupported.")
        endpoints: list[tuple[str, str]] = []
        for endpoint_name in ("from", "to"):
            endpoint = _object(connection.get(endpoint_name), f"connection {connection_id}.{endpoint_name}")
            component_id = _identifier(endpoint.get("component"), f"connection {connection_id}.{endpoint_name}.component")
            port_id = _identifier(endpoint.get("port"), f"connection {connection_id}.{endpoint_name}.port")
            if (component_id, port_id) not in ports:
                _fail(f"connection {connection_id}.{endpoint_name} names missing port {component_id}.{port_id}.")
            endpoints.append((component_id, port_id))
        if endpoints[0] == endpoints[1]:
            _fail(f"connection {connection_id} connects a port to itself.")
        points = [ports[endpoints[0]]]
        vias = connection.get("via", [])
        if not isinstance(vias, list):
            _fail(f"connection {connection_id}.via must be an array.")
        points.extend(
            _normalized_point(via, f"connection {connection_id}.via[{via_index}]", canvas)
            for via_index, via in enumerate(vias)
        )
        destination = ports[endpoints[1]]
        routing = _text(connection.get("routing", "straight"), f"connection {connection_id}.routing")
        if routing not in {"straight", "orthogonal", "explicit"}:
            _fail(f"connection {connection_id}.routing is unsupported.")
        if routing == "orthogonal" and not vias:
            points.append((destination[0], points[-1][1]))
        points.append(destination)
        if any(first == second for first, second in zip(points, points[1:], strict=False)):
            _fail(f"connection {connection_id} contains a zero-length routed segment.")
        layer = artwork.layer(
            f"path_{path_type}_{element['id']}",
            f"{path_type.title()} paths: {element['id']}",
            _pen_for_role(context, path_type),
        )
        directed = connection.get("direction", "forward")
        if directed not in {"forward", "bidirectional", "none"}:
            _fail(f"connection {connection_id}.direction is unsupported.")
        path_attributes = {**attributes, "data-connection-id": connection_id, "data-path-type": path_type}
        dashed = path_type in {"fluidic", "data", "control"}
        if dashed:
            layer.add_many(
                dash_polyline(
                    points,
                    dash_mm=6 * layer.pen.mark_width_mm,
                    gap_mm=3 * layer.pen.mark_width_mm,
                    minimum_stroke_mm=3 * layer.pen.mark_width_mm,
                ),
                source_ref=str(element["source_refs"][0]),
                role=f"{path_type}-path-dashed",
                attributes=path_attributes,
            )
        else:
            layer.add(
                points,
                source_ref=str(element["source_refs"][0]),
                role=f"{path_type}-path",
                attributes=path_attributes,
            )
        head = max(4 * layer.pen.mark_width_mm, float(context.plate["gap_mm"]) / 2)
        if directed in {"forward", "bidirectional"}:
            layer.add(
                arrow_strokes(points[-2:], head_mm=head)[1],
                source_ref=str(element["source_refs"][0]),
                role=f"{path_type}-direction",
                attributes=path_attributes,
            )
        if directed == "bidirectional":
            layer.add(
                arrow_strokes(list(reversed(points[:2])), head_mm=head)[1],
                source_ref=str(element["source_refs"][0]),
                role=f"{path_type}-direction",
                attributes=path_attributes,
            )
        path_type_counts[path_type] = path_type_counts.get(path_type, 0) + 1
    return {
        "element_id": element["id"],
        "kind": "schematic",
        "components": component_records,
        "connection_count": len(connections),
        "path_type_counts": path_type_counts,
        "topology_preserved": True,
        "connections_inferred": False,
        "routing": "caller-supplied-vias-or-explicit-orthogonal-v1",
    }


def _linear_parts(geometry: Any) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    if hasattr(geometry, "geoms"):
        return [part for child in geometry.geoms for part in _linear_parts(child)]
    return []


def _hatched_polygon(
    polygon: Polygon,
    *,
    angle_deg: float,
    spacing_mm: float,
    minimum_length_mm: float,
) -> tuple[list[list[tuple[float, float]]], int]:
    if spacing_mm <= 0:
        _fail("device hatch spacing must be positive.")
    angle = math.radians(angle_deg)
    direction = (math.cos(angle), math.sin(angle))
    normal = (-direction[1], direction[0])
    corners = [
        (polygon.bounds[0], polygon.bounds[1]),
        (polygon.bounds[0], polygon.bounds[3]),
        (polygon.bounds[2], polygon.bounds[1]),
        (polygon.bounds[2], polygon.bounds[3]),
    ]
    offsets = [x * normal[0] + y * normal[1] for x, y in corners]
    along = [x * direction[0] + y * direction[1] for x, y in corners]
    low_offset = min(offsets) - spacing_mm
    high_offset = max(offsets) + spacing_mm
    low_along = min(along) - max(polygon.bounds[2] - polygon.bounds[0], polygon.bounds[3] - polygon.bounds[1])
    high_along = max(along) + max(polygon.bounds[2] - polygon.bounds[0], polygon.bounds[3] - polygon.bounds[1])
    result: list[list[tuple[float, float]]] = []
    omitted = 0
    index = 0
    offset = low_offset
    while offset <= high_offset + 1e-9:
        start = (direction[0] * low_along + normal[0] * offset, direction[1] * low_along + normal[1] * offset)
        end = (direction[0] * high_along + normal[0] * offset, direction[1] * high_along + normal[1] * offset)
        intersection = polygon.intersection(LineString((start, end)))
        for part in _linear_parts(intersection):
            points = [(float(x), float(y)) for x, y in part.coords]
            if polyline_length_mm(points) + 1e-9 < minimum_length_mm:
                omitted += 1
            else:
                result.append(points)
        index += 1
        offset = low_offset + index * spacing_mm
    return result, omitted


def _render_device(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    del assets
    material_layers = _array(
        element.get("layers"), f"element {element['id']}.layers", nonempty=True
    )
    scale_label = _text(element.get("scale_label"), f"element {element['id']}.scale_label")
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    rect = slot.inset(min(gap, min(slot.width, slot.height) / 6))
    canvas = NormalizedCanvas(rect)
    attributes = _attributes(element)
    layer_ids: set[str] = set()
    material_records: list[dict[str, Any]] = []
    for index, raw_material in enumerate(material_layers):
        material = _object(raw_material, f"element {element['id']}.layers[{index}]")
        material_id = _identifier(material.get("id"), f"device layer[{index}].id")
        if material_id in layer_ids:
            _fail(f"device element {element['id']!r} repeats layer {material_id!r}.")
        layer_ids.add(material_id)
        material_name = _text(material.get("material"), f"device layer {material_id}.material")
        semantic_role = _text(material.get("semantic_role", "structure"), f"device layer {material_id}.semantic_role")
        if semantic_role not in {"structure", "comparison", "reference", "fit"}:
            _fail(f"device layer {material_id}.semantic_role is unsupported.")
        raw_polygon = _array(material.get("polygon"), f"device layer {material_id}.polygon", nonempty=True)
        points = [
            _normalized_point(point, f"device layer {material_id}.polygon[{point_index}]", canvas)
            for point_index, point in enumerate(raw_polygon)
        ]
        if len(points) < 3:
            _fail(f"device layer {material_id}.polygon needs at least three points.")
        polygon = Polygon(points)
        if not polygon.is_valid or polygon.area <= 0:
            _fail(f"device layer {material_id}.polygon is invalid or degenerate.")
        outline = artwork.layer(
            f"device_{element['id']}_{material_id}",
            f"Device material {material_name}",
            _pen_for_role(context, semantic_role),
        )
        outline.add(
            [*points, points[0]],
            source_ref=str(element["source_refs"][0]),
            role="material-boundary",
            attributes={**attributes, "data-material-id": material_id, "data-material": material_name},
        )
        hatch_angle = _number(material.get("hatch_angle_deg"), f"device layer {material_id}.hatch_angle_deg")
        hatch_spacing = _number(material.get("hatch_spacing_mm"), f"device layer {material_id}.hatch_spacing_mm")
        hatches, omitted = _hatched_polygon(
            polygon,
            angle_deg=hatch_angle,
            spacing_mm=hatch_spacing,
            minimum_length_mm=3 * outline.pen.mark_width_mm,
        )
        outline.add_many(
            hatches,
            source_ref=str(element["source_refs"][0]),
            role="material-section-hatch",
            attributes={**attributes, "data-material-id": material_id, "data-material": material_name},
        )
        material_records.append(
            {
                "id": material_id,
                "material": material_name,
                "semantic_role": semantic_role,
                "hatch_angle_deg": hatch_angle,
                "hatch_spacing_mm": hatch_spacing,
                "hatch_strokes": len(hatches),
                "sub-nib-derived-hatches_omitted": omitted,
            }
        )
    label_layer = artwork.layer(
        f"device_labels_{element['id']}",
        f"Device material legend: {element['id']}",
        _pen_for_role(context, "annotation"),
    )
    cap = max(8 * label_layer.pen.mark_width_mm, float(context.plate["type_scale_mm"]["attribution"]))
    legend_lines = [f"{record['id']}: {record['material']}" for record in material_records]
    line_gap = 4 * label_layer.pen.mark_width_mm
    if len(legend_lines) * (cap + line_gap) > rect.height:
        _fail(f"device element {element['id']!r} material legend cannot fit at physical size.")
    for index, legend in enumerate(legend_lines):
        _add_physical_text(
            label_layer,
            legend,
            x=rect.left,
            y=rect.top + index * (cap + line_gap),
            cap=cap,
            maximum_width=rect.width,
            role="material-legend",
            source_ref=str(element["source_refs"][0]),
            attributes={**attributes, "data-material-id": material_records[index]["id"]},
        )
    _add_physical_text(
        label_layer,
        scale_label,
        x=rect.left,
        y=rect.bottom - cap,
        cap=cap,
        maximum_width=rect.width,
        role="device-scale",
        source_ref=str(element["source_refs"][0]),
        attributes=attributes,
    )
    return {
        "element_id": element["id"],
        "kind": "device",
        "material_layers": material_records,
        "scale_label": scale_label,
        "fills_emitted": False,
        "section_representation": "boundary-lines-and-explicit-hatching",
        "material_identity_preserved": True,
    }


def _point3(value: Any, label: str) -> tuple[float, float, float]:
    raw = _array(value, label)
    if len(raw) != 3:
        _fail(f"{label} must be [x, y, z].")
    return (
        _number(raw[0], f"{label}[0]"),
        _number(raw[1], f"{label}[1]"),
        _number(raw[2], f"{label}[2]"),
    )


def _dot3(first: Sequence[float], second: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(first, second, strict=True))


def _projection(value: Any, label: str) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float], str]:
    projection = _object(value, label)
    projection_type = _text(projection.get("type"), f"{label}.type")
    if projection_type == "isometric":
        return (
            (math.sqrt(3) / 2, -math.sqrt(3) / 2, 0.0),
            (0.5, 0.5, -1.0),
            (1.0, 1.0, 1.0),
            projection_type,
        )
    if projection_type != "orthographic-matrix":
        _fail(f"{label}.type must be isometric or orthographic-matrix.")
    matrix = _array(projection.get("matrix"), f"{label}.matrix")
    if len(matrix) != 2:
        _fail(f"{label}.matrix must have two rows.")
    rows = tuple(_point3(row, f"{label}.matrix[{index}]") for index, row in enumerate(matrix))
    depth = _point3(projection.get("depth_vector"), f"{label}.depth_vector")
    if math.sqrt(_dot3(depth, depth)) <= 1e-12:
        _fail(f"{label}.depth_vector must be non-zero.")
    return (rows[0], rows[1], depth, projection_type)


def _render_structure(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    asset = _source_asset(element, assets)
    payload: Mapping[str, Any]
    if "atoms" in element:
        payload = element
    elif isinstance(asset, Mapping):
        payload = asset
    else:
        _fail(f"structure element {element['id']!r} has no coordinates.")
    atoms = _array(payload.get("atoms"), f"element {element['id']}.atoms", nonempty=True)
    bonds = _array(payload.get("bonds"), f"element {element['id']}.bonds")
    projection_x, projection_y, depth_vector, projection_type = _projection(
        element.get("projection"), f"element {element['id']}.projection"
    )
    atom_positions: dict[str, tuple[float, float, float]] = {}
    atom_records: dict[str, dict[str, Any]] = {}
    projected: dict[str, tuple[float, float]] = {}
    depths: dict[str, float] = {}
    for index, raw_atom in enumerate(atoms):
        atom = _object(raw_atom, f"element {element['id']}.atoms[{index}]")
        atom_id = _identifier(atom.get("id"), f"atom[{index}].id")
        if atom_id in atom_positions:
            _fail(f"structure element {element['id']!r} repeats atom {atom_id!r}.")
        element_name = _text(atom.get("element"), f"atom {atom_id}.element")
        position = _point3(atom.get("position"), f"atom {atom_id}.position")
        atom_positions[atom_id] = position
        atom_records[atom_id] = {"id": atom_id, "element": element_name, "label": atom.get("label")}
        projected[atom_id] = (_dot3(projection_x, position), _dot3(projection_y, position))
        depths[atom_id] = _dot3(depth_vector, position)
    if len(projected) < 2 or len(set(projected.values())) < 2:
        _fail(f"structure element {element['id']!r} needs at least two distinct projected sites.")
    unit_cell = element.get("unit_cell_edges", [])
    if not isinstance(unit_cell, list):
        _fail(f"element {element['id']}.unit_cell_edges must be an array.")
    projected_cell_edges: list[
        tuple[tuple[float, float], tuple[float, float]]
    ] = []
    for index, raw_edge in enumerate(unit_cell):
        edge_points = _array(raw_edge, f"unit_cell_edges[{index}]")
        if len(edge_points) != 2:
            _fail(f"unit_cell_edges[{index}] must contain two 3D points.")
        points3 = (
            _point3(edge_points[0], f"unit_cell_edges[{index}][0]"),
            _point3(edge_points[1], f"unit_cell_edges[{index}][1]"),
        )
        projected_cell_edges.append(
            (
                (_dot3(projection_x, points3[0]), _dot3(projection_y, points3[0])),
                (_dot3(projection_x, points3[1]), _dot3(projection_y, points3[1])),
            )
        )
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    rect = slot.inset(min(gap, min(slot.width, slot.height) / 6))
    combined_projected = [
        *projected.values(),
        *(point for edge in projected_cell_edges for point in edge),
    ]
    fitted_points = normalize_points(
        combined_projected, rect, preserve_aspect=True, invert_y=True
    )
    page_positions = {
        atom_id: fitted_points[index] for index, atom_id in enumerate(projected)
    }
    cell_offset = len(projected)
    fitted_cell_edges = [
        (
            fitted_points[cell_offset + 2 * index],
            fitted_points[cell_offset + 2 * index + 1],
        )
        for index in range(len(projected_cell_edges))
    ]
    median_depth = sorted(depths.values())[len(depths) // 2]
    attributes = _attributes(element)
    bond_ids: set[str] = set()
    bond_class_counts: dict[str, int] = {}
    for index, raw_bond in enumerate(bonds):
        bond = _object(raw_bond, f"element {element['id']}.bonds[{index}]")
        bond_id = _identifier(bond.get("id"), f"bond[{index}].id")
        if bond_id in bond_ids:
            _fail(f"structure element {element['id']!r} repeats bond {bond_id!r}.")
        bond_ids.add(bond_id)
        endpoints = _array(bond.get("atoms"), f"bond {bond_id}.atoms")
        if len(endpoints) != 2:
            _fail(f"bond {bond_id}.atoms must name two sites.")
        first = _identifier(endpoints[0], f"bond {bond_id}.atoms[0]")
        second = _identifier(endpoints[1], f"bond {bond_id}.atoms[1]")
        if first == second or first not in page_positions or second not in page_positions:
            _fail(f"bond {bond_id} names invalid or repeated atom endpoints.")
        bond_class = _text(bond.get("class"), f"bond {bond_id}.class")
        hidden = bond.get("hidden", False)
        if not isinstance(hidden, bool):
            _fail(f"bond {bond_id}.hidden must be boolean.")
        average_depth = (depths[first] + depths[second]) / 2
        semantic = "reference" if hidden or average_depth < median_depth else "structure"
        layer = artwork.layer(
            f"bonds_{element['id']}_{semantic}",
            f"Structure bonds {semantic}: {element['id']}",
            _pen_for_role(context, semantic),
        )
        points = [page_positions[first], page_positions[second]]
        if hidden:
            layer.add_many(
                dash_polyline(
                    points,
                    dash_mm=6 * layer.pen.mark_width_mm,
                    gap_mm=3 * layer.pen.mark_width_mm,
                    minimum_stroke_mm=3 * layer.pen.mark_width_mm,
                ),
                source_ref=str(element["source_refs"][0]),
                role="hidden-bond",
                attributes={**attributes, "data-bond-id": bond_id, "data-bond-class": bond_class},
            )
        else:
            layer.add(
                points,
                source_ref=str(element["source_refs"][0]),
                role="bond",
                attributes={**attributes, "data-bond-id": bond_id, "data-bond-class": bond_class},
            )
        bond_class_counts[bond_class] = bond_class_counts.get(bond_class, 0) + 1
    atom_layer = artwork.layer(
        f"atoms_{element['id']}",
        f"Atomic sites: {element['id']}",
        _pen_for_role(context, "component"),
    )
    label_layer = artwork.layer(
        f"site_labels_{element['id']}",
        f"Selected site labels: {element['id']}",
        _pen_for_role(context, "annotation"),
    )
    radius = element.get("atom_radius_mm", 2.0 * atom_layer.pen.mark_width_mm)
    radius_mm = _number(radius, f"element {element['id']}.atom_radius_mm")
    if radius_mm <= 0:
        _fail(f"element {element['id']}.atom_radius_mm must be positive.")
    selected_labels = 0
    for atom_id in sorted(atom_positions, key=lambda item: (depths[item], item)):
        atom = atom_records[atom_id]
        atom_layer.add(
            circle_stroke(page_positions[atom_id], radius_mm, segments=16),
            source_ref=str(element["source_refs"][0]),
            role="atomic-site",
            attributes={**attributes, "data-atom-id": atom_id, "data-element": str(atom["element"])},
        )
        if atom["label"] is not None:
            label = _text(atom["label"], f"atom {atom_id}.label")
            cap = max(8 * label_layer.pen.mark_width_mm, float(context.plate["type_scale_mm"]["attribution"]))
            point = page_positions[atom_id]
            _add_physical_text(
                label_layer,
                label,
                x=point[0] + radius_mm * 1.5,
                y=max(rect.top, point[1] - cap / 2),
                cap=cap,
                maximum_width=max(rect.right - point[0] - radius_mm * 1.5, cap),
                role="selected-site-label",
                source_ref=str(element["source_refs"][0]),
                attributes={**attributes, "data-atom-id": atom_id},
            )
            selected_labels += 1
    if fitted_cell_edges:
        cell_layer = artwork.layer(
            f"unit_cell_{element['id']}",
            f"Unit cell: {element['id']}",
            _pen_for_role(context, "reference"),
        )
        for index, edge in enumerate(fitted_cell_edges):
            cell_layer.add(
                edge,
                source_ref=str(element["source_refs"][0]),
                role="unit-cell-edge",
                sequence=index,
                attributes=attributes,
            )
    return {
        "element_id": element["id"],
        "kind": "structure",
        "projection": projection_type,
        "atom_count": len(atoms),
        "bond_count": len(bonds),
        "bond_class_counts": bond_class_counts,
        "selected_site_labels": selected_labels,
        "unit_cell_edge_count": len(unit_cell),
        "atomic_positions_inferred": False,
        "bonds_inferred": False,
        "depth_cue": "explicit-projection-depth-semantic-line-hierarchy-v1",
    }


def _render_timeline(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    del assets
    milestones = _array(
        element.get("milestones"), f"element {element['id']}.milestones", nonempty=True
    )
    if len(milestones) < 2:
        _fail(f"timeline element {element['id']!r} needs at least two milestones.")
    checked: list[dict[str, Any]] = []
    previous_key: float | None = None
    ids: set[str] = set()
    for index, raw in enumerate(milestones):
        milestone = _object(raw, f"element {element['id']}.milestones[{index}]")
        milestone_id = _identifier(milestone.get("id"), f"milestone[{index}].id")
        if milestone_id in ids:
            _fail(f"timeline element {element['id']!r} repeats milestone {milestone_id!r}.")
        ids.add(milestone_id)
        sort_key = _number(milestone.get("sort_key"), f"milestone {milestone_id}.sort_key")
        if previous_key is not None and sort_key < previous_key:
            _fail("timeline milestones must already be in factual chronological order; the renderer does not reorder them.")
        previous_key = sort_key
        _text(milestone.get("date"), f"milestone {milestone_id}.date")
        _text(milestone.get("label"), f"milestone {milestone_id}.label")
        checked.append(milestone)
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    rect = slot.inset(min(gap, min(slot.width, slot.height) / 6))
    line_layer = artwork.layer(
        f"timeline_{element['id']}",
        f"Research timeline: {element['id']}",
        _pen_for_role(context, "reference"),
    )
    label_layer = artwork.layer(
        f"timeline_labels_{element['id']}",
        f"Timeline labels: {element['id']}",
        _pen_for_role(context, "annotation"),
    )
    attributes = _attributes(element)
    horizontal = rect.width >= rect.height
    start = (rect.left + gap, rect.centre[1]) if horizontal else (rect.centre[0], rect.top + gap)
    end = (rect.right - gap, rect.centre[1]) if horizontal else (rect.centre[0], rect.bottom - gap)
    line_layer.add(
        [start, end],
        source_ref=str(element["source_refs"][0]),
        role="research-journey-line",
        attributes=attributes,
    )
    cap = max(8 * label_layer.pen.mark_width_mm, float(context.plate["type_scale_mm"]["attribution"]))
    marker_radius = 2 * line_layer.pen.mark_width_mm
    for index, milestone in enumerate(checked):
        fraction = index / (len(checked) - 1)
        point = (
            start[0] + fraction * (end[0] - start[0]),
            start[1] + fraction * (end[1] - start[1]),
        )
        line_layer.add(
            circle_stroke(point, marker_radius, segments=16),
            source_ref=str(element["source_refs"][0]),
            role="milestone-marker",
            attributes={**attributes, "data-milestone-id": str(milestone["id"])},
        )
        copy_text = f"{milestone['date']} / {milestone['label']}"
        if horizontal:
            available = max((end[0] - start[0]) / max(len(checked), 1) - gap, cap)
            label_x = max(rect.left, point[0] - available / 2)
            label_y = point[1] - cap - gap / 2 if index % 2 == 0 else point[1] + gap / 2
            _add_physical_text(
                label_layer,
                copy_text,
                x=label_x,
                y=label_y,
                cap=cap,
                maximum_width=available,
                role="milestone-label",
                source_ref=str(element["source_refs"][0]),
                attributes={**attributes, "data-milestone-id": str(milestone["id"])},
            )
        else:
            available = rect.width / 2 - gap
            label_x = point[0] + gap / 2 if index % 2 == 0 else rect.left
            _add_physical_text(
                label_layer,
                copy_text,
                x=label_x,
                y=point[1] - cap / 2,
                cap=cap,
                maximum_width=available,
                role="milestone-label",
                source_ref=str(element["source_refs"][0]),
                attributes={**attributes, "data-milestone-id": str(milestone["id"])},
            )
    return {
        "element_id": element["id"],
        "kind": "timeline",
        "milestone_count": len(checked),
        "input_order_preserved": True,
        "dates_inferred": False,
        "layout": "equal-order-spacing-not-a-duration-scale",
    }


def _render_patent_views(
    artwork: PlateArtwork,
    element: Mapping[str, Any],
    slot: Rect,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    del assets
    views = _array(element.get("views"), f"element {element['id']}.views", nonempty=True)
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    rect = slot.inset(min(gap, min(slot.width, slot.height) / 6))
    view_slots = _grid_slots(rect, len(views), gap)
    attributes = _attributes(element)
    view_ids: set[str] = set()
    path_count = 0
    for index, (raw_view, view_slot) in enumerate(zip(views, view_slots, strict=True)):
        view = _object(raw_view, f"element {element['id']}.views[{index}]")
        view_id = _identifier(view.get("id"), f"patent view[{index}].id")
        if view_id in view_ids:
            _fail(f"patent element {element['id']!r} repeats view {view_id!r}.")
        view_ids.add(view_id)
        view_label = _text(view.get("label"), f"patent view {view_id}.label")
        raw_paths = _array(view.get("paths"), f"patent view {view_id}.paths", nonempty=True)
        parsed: list[VectorPath] = []
        for path_index, payload in enumerate(raw_paths):
            try:
                parsed.append(VectorPath.from_dict(payload))
            except (VectorPathError, TypeError, ValueError) as exc:
                raise MapPlotterError(
                    f"Invalid academic artwork data: patent view {view_id}.paths[{path_index}] is invalid: {exc}"
                ) from exc
        label_layer = artwork.layer(
            f"patent_labels_{element['id']}",
            f"Patent view labels: {element['id']}",
            _pen_for_role(context, "annotation"),
        )
        cap = max(8 * label_layer.pen.mark_width_mm, float(context.plate["type_scale_mm"]["attribution"]))
        drawing_rect = Rect(view_slot.x, view_slot.y + cap + gap / 2, view_slot.width, view_slot.height - cap - gap / 2)
        fitted = fit_vector_paths(parsed, drawing_rect)
        drawing_layer = artwork.layer(
            f"patent_views_{element['id']}",
            f"Patent views: {element['id']}",
            _pen_for_role(context, "component"),
        )
        for path_index, path in enumerate(fitted):
            drawing_layer.add_path(
                path,
                source_ref=str(element["source_refs"][0]),
                role="numbered-invention-view",
                sequence=path_count,
                attributes={**attributes, "data-view-id": view_id, "data-view-path-index": str(path_index)},
            )
            path_count += 1
        _add_physical_text(
            label_layer,
            view_label,
            x=view_slot.left,
            y=view_slot.top,
            cap=cap,
            maximum_width=view_slot.width,
            role="patent-view-label",
            source_ref=str(element["source_refs"][0]),
            attributes={**attributes, "data-view-id": view_id},
        )
    return {
        "element_id": element["id"],
        "kind": "patent-views",
        "view_count": len(views),
        "path_count": path_count,
        "official_document_layout_copied": False,
        "view_numbers_and_labels_supplied": True,
    }


def _render_field_title(
    artwork: PlateArtwork,
    record: Mapping[str, Any],
    working: Rect,
) -> tuple[Rect, dict[str, Any]]:
    canonical = str(record["title"])
    plate_title = str(record.get("plate_title", canonical))
    if plate_title == canonical:
        return working, {
            "mode": "plate-title-zone",
            "canonical_title_drawn_in_field": False,
        }
    requested = str(record.get("title_layout", "auto"))
    mode = "wrapped-title" if requested == "auto" else requested
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    layer = artwork.layer(
        "academic_full_title",
        "Canonical academic title",
        _pen_for_role(context, "annotation"),
    )
    preferred = float(context.plate["type_scale_mm"]["detail"])
    floor = 8 * layer.pen.mark_width_mm
    attributes = {
        "data-copy": canonical,
        "data-title-layout": mode,
        "data-title-sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    if mode == "side-title":
        cap = preferred
        natural = text_width_mm(canonical, cap_height_mm=cap)
        fitted = min(cap, cap * (working.height - 2 * gap) / natural)
        if fitted + 1e-9 < floor:
            _fail("side-title cannot fit the canonical title above the physical type floor; supply a compact plate title and use wrapped-title/title-block.")
        _add_physical_text(
            layer,
            canonical,
            x=working.left + fitted,
            y=working.top + gap,
            cap=fitted,
            maximum_width=working.height - 2 * gap,
            role="canonical-side-title",
            source_ref=None,
            attributes=attributes,
            angle_deg=90.0,
        )
        strip = fitted + gap
        if strip + gap >= working.width:
            _fail("side-title leaves no physical scientific field.")
        return (
            Rect(working.x + strip + gap, working.y, working.width - strip - gap, working.height),
            {
                "mode": mode,
                "canonical_title_drawn_in_field": True,
                "line_count": 1,
                "cap_height_mm": round(fitted, 6),
            },
        )
    maximum_lines = {
        "compact-title": 2,
        "wrapped-title": 4,
        "title-block": 6,
    }[mode]
    cap = preferred
    while cap + 1e-9 >= floor:
        lines = _wrap_lines(canonical, cap=cap, width=working.width)
        line_gap = 4 * layer.pen.mark_width_mm
        height = len(lines) * cap + max(len(lines) - 1, 0) * line_gap
        if len(lines) <= maximum_lines and height + gap < working.height / 2:
            break
        cap -= 0.05
    else:
        _fail(
            f"{mode} cannot fit the canonical title above the physical type floor; use side-title or a larger format."
        )
    for index, line in enumerate(lines):
        width = text_width_mm(line, cap_height_mm=cap)
        x = working.centre[0] - width / 2 if mode == "title-block" else working.left
        _add_physical_text(
            layer,
            line,
            x=x,
            y=working.top + index * (cap + line_gap),
            cap=cap,
            maximum_width=working.width,
            role="canonical-field-title",
            source_ref=None,
            attributes={**attributes, "data-title-line-index": str(index)},
        )
    consumed = height + gap
    return (
        Rect(working.x, working.y + consumed, working.width, working.height - consumed),
        {
            "mode": mode,
            "canonical_title_drawn_in_field": True,
            "line_count": len(lines),
            "cap_height_mm": round(cap, 6),
        },
    )


def _metadata_copy(metadata: Mapping[str, Any], key: str) -> str | None:
    raw = metadata.get(key)
    if raw is None:
        return None
    if isinstance(raw, str):
        return _text(raw, f"record.metadata.{key}")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = [
            _text(value, f"record.metadata.{key}[{index}]")
            for index, value in enumerate(raw)
        ]
        if not values:
            return None
        return " / ".join(values)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        _fail(
            f"record.metadata.{key} must be text, a text array, or a finite number."
        )
    number = _number(raw, f"record.metadata.{key}")
    return f"{number:g}"


def _preset_metadata_paragraphs(
    record: Mapping[str, Any],
    preset: AcademicPreset,
) -> tuple[tuple[str, str], ...]:
    metadata = _object(record["metadata"], "record.metadata")

    def required(key: str) -> str:
        value = _metadata_copy(metadata, key)
        if value is None:  # pragma: no cover - preset validation checks this first.
            _fail(f"preset {preset.id!r} requires record.metadata.{key}.")
        return value

    def optional(key: str) -> str | None:
        return _metadata_copy(metadata, key)

    if preset.id == "paper-frontispiece":
        statement = (
            optional("significance_statement")
            or optional("result_statement")
            or optional("caption")
            or optional("abstract_excerpt")
        )
        assert statement is not None
        paragraphs = [
            ("authors", f"AUTHORS / {required('authors')}"),
            ("publication", f"YEAR / {required('year')} / DOI / {required('doi')}"),
            ("significance", f"RESULT / {statement}"),
        ]
        citation_parts = [
            value
            for value in (
                optional("journal"),
                optional("volume"),
                optional("pages"),
                optional("article_number"),
            )
            if value is not None
        ]
        if citation_parts:
            paragraphs.append(("citation", " / ".join(citation_parts)))
        return tuple(paragraphs)
    if preset.id == "equation-centrepiece":
        researcher = optional("researcher") or optional("author")
        date = optional("date") or optional("year")
        variables = optional("variable_definitions")
        assert variables is not None
        return tuple(
            item
            for item in (
                ("researcher", f"RESEARCHER / {researcher}")
                if researcher is not None
                else None,
                ("date", f"DATE / {date}") if date is not None else None,
                ("variable-definitions", f"VARIABLES / {variables}"),
            )
            if item is not None
        )
    if preset.id == "experimental-path":
        organisation = optional("laboratory") or optional("institution")
        return tuple(
            item
            for item in (
                ("researcher", f"RESEARCHER / {required('researcher')}"),
                ("organisation", f"LAB / INSTITUTION / {organisation}")
                if organisation is not None
                else None,
                ("date", f"DATE / {required('date')}"),
            )
            if item is not None
        )
    if preset.id == "thesis-portrait":
        return (
            ("author", f"AUTHOR / {required('author')}"),
            ("degree", f"DEGREE / {required('degree')}"),
            (
                "institution-year",
                f"INSTITUTION / {required('institution')} / YEAR / {required('year')}",
            ),
            ("supervisors", f"SUPERVISORS / {required('supervisors')}"),
        )
    if preset.id == "graduation-coordinates":
        institution = optional("institution")
        department = optional("department")
        organisation = " / ".join(
            value for value in (institution, department) if value is not None
        )
        return tuple(
            item
            for item in (
                ("graduate", f"GRADUATE / {required('graduate_name')}"),
                ("degree", f"DEGREE / {required('degree')}"),
                ("institution-department", organisation) if organisation else None,
                (
                    "date-coordinates",
                    f"DATE / {required('date')} / COORDINATES / {required('coordinates')}",
                ),
            )
            if item is not None
        )
    if preset.id == "patent-invention-plate":
        dates = [
            value
            for value in (optional("filing_date"), optional("grant_date"))
            if value is not None
        ]
        return tuple(
            item
            for item in (
                ("inventors", f"INVENTORS / {required('inventors')}"),
                ("patent-number", f"PATENT / {required('patent_number')}"),
                ("filing-grant-dates", " / ".join(dates)) if dates else None,
            )
            if item is not None
        )
    if preset.id == "laboratory-identity":
        institution = optional("institution")
        return tuple(
            item
            for item in (
                ("laboratory", f"LABORATORY / {required('laboratory')}"),
                ("institution", f"INSTITUTION / {institution}")
                if institution is not None
                else None,
            )
            if item is not None
        )
    return ()


def _render_preset_metadata(
    artwork: PlateArtwork,
    record: Mapping[str, Any],
    preset: AcademicPreset,
    working: Rect,
) -> tuple[Rect, dict[str, Any]]:
    paragraphs = _preset_metadata_paragraphs(record, preset)
    if not paragraphs:
        return working, {
            "drawn": False,
            "field_ids": [],
            "authorship_and_list_order_preserved": True,
        }
    context = artwork.context
    gap = float(context.plate["gap_mm"])
    layer = artwork.layer(
        "academic_preset_metadata",
        f"Factual metadata: {preset.id}",
        _pen_for_role(context, "annotation"),
    )
    preferred = float(context.plate["type_scale_mm"]["detail"])
    floor = 8.0 * layer.pen.mark_width_mm
    selected_cap = preferred
    selected_lines: list[tuple[str, str]] = []
    line_gap = 4.0 * layer.pen.mark_width_mm
    selected_height = 0.0
    while selected_cap + 1e-9 >= floor:
        candidate_lines = [
            (field_id, line)
            for field_id, paragraph in paragraphs
            for line in _wrap_lines(
                paragraph,
                cap=selected_cap,
                width=working.width,
            )
        ]
        candidate_height = len(candidate_lines) * selected_cap + max(
            len(candidate_lines) - 1, 0
        ) * line_gap
        if candidate_height + 3.0 * gap < working.height:
            selected_lines = candidate_lines
            selected_height = candidate_height
            break
        selected_cap -= 0.05
    else:
        _fail(
            f"preset {preset.id!r} metadata cannot fit as one uniformly scaled "
            "group above the physical text floor; use a larger format."
        )
    metadata_sha256 = _canonical_sha256(record["metadata"])
    y = working.top
    for line_index, (field_id, line) in enumerate(selected_lines):
        _add_physical_text(
            layer,
            line,
            x=working.left,
            y=y + line_index * (selected_cap + line_gap),
            cap=selected_cap,
            maximum_width=working.width,
            role="preset-factual-metadata",
            source_ref=None,
            attributes={
                "data-copy": line,
                "data-metadata-field": field_id,
                "data-metadata-line-index": str(line_index),
                "data-metadata-sha256": metadata_sha256,
            },
        )
    rule_y = y + selected_height + gap / 2.0
    layer.add(
        [(working.left, rule_y), (working.right, rule_y)],
        role="preset-metadata-rule",
        attributes={"data-metadata-sha256": metadata_sha256},
    )
    consumed = selected_height + gap
    return (
        Rect(
            working.x,
            working.y + consumed,
            working.width,
            working.height - consumed,
        ),
        {
            "drawn": True,
            "field_ids": [field_id for field_id, _paragraph in paragraphs],
            "logical_paragraph_count": len(paragraphs),
            "rendered_line_count": len(selected_lines),
            "cap_height_mm": round(selected_cap, 6),
            "metadata_sha256": metadata_sha256,
            "uniform_group_fit": True,
            "authorship_and_list_order_preserved": True,
        },
    )


Renderer = Callable[
    [PlateArtwork, Mapping[str, Any], Rect, Mapping[str, Any]], dict[str, Any]
]
_RENDERERS: dict[str, Renderer] = {
    "graph": _render_graph,
    "equation": _render_equation,
    "schematic": _render_schematic,
    "device": _render_device,
    "structure": _render_structure,
    "scalar-field": _render_scalar_field,
    "vector": _render_vector,
    "campus": _render_vector,
    "text-block": _render_text_block,
    "timeline": _render_timeline,
    "patent-views": _render_patent_views,
}


def _default_details(record: Mapping[str, Any], preset: AcademicPreset) -> tuple[str, ...]:
    supplied = tuple(str(value) for value in record.get("details", []))
    if supplied:
        return supplied
    metadata = record["metadata"]
    year = metadata.get("year") or metadata.get("date")
    composition_copy = {
        "reconstructed-from-data": "DATA RECONSTRUCTION",
        "rights-cleared-vector-reinterpretation": "CLEARED VECTOR RELAYOUT",
        "direct-reproduction": "PERMITTED REPRODUCTION",
        "original-academic-composition": "ORIGINAL COMPOSITION",
    }
    preset_copy = {
        "paper-frontispiece": "PAPER FRONTISPIECE",
        "figure-as-art": "FIGURE AS ART",
        "graph-as-landscape": "GRAPH LANDSCAPE",
        "equation-centrepiece": "EQUATION CENTREPIECE",
        "device-blueprint": "DEVICE BLUEPRINT",
        "experimental-path": "EXPERIMENTAL PATH",
        "thesis-portrait": "THESIS PORTRAIT",
        "graduation-coordinates": "GRADUATION COORDINATES",
        "research-journey": "RESEARCH JOURNEY",
        "patent-invention-plate": "INVENTION PLATE",
        "molecular-crystal-structure": "MOLECULAR / CRYSTAL",
        "microscopy-contour-study": "MICROSCOPY CONTOURS",
        "publication-collection": "PUBLICATION SERIES",
        "laboratory-identity": "LABORATORY ART",
        "scientific-minimalism": "SCIENTIFIC MINIMALISM",
        "captioned-result": "CAPTIONED RESULT",
    }
    factual = composition_copy[str(record["composition_mode"])]
    if year is not None:
        factual = f"{factual} / {year}"
    return (
        preset_copy[preset.id],
        factual,
        "VALUES / UNITS PRESERVED",
    )


def _record_input_sha256(record: Mapping[str, Any]) -> str:
    return _canonical_sha256(
        {
            key: value
            for key, value in record.items()
            if key not in {"review_required", "limitations"}
        }
    )


def build_academic_plate(
    value: Any,
    format_id: str | None = None,
) -> PlateArtwork:
    """Build one layered academic artwork on an existing binding plate."""

    if isinstance(value, LoadedAcademicRecord):
        checked = validate_academic_record(value.record)
        assets = value.assets
        asset_evidence = copy.deepcopy(value.asset_evidence)
        specification_path: str | None = str(value.source_path)
    else:
        checked = validate_academic_record(value)
        assets = {}
        asset_evidence = {}
        specification_path = None
    if checked["review_required"]:
        raise MapPlotterError(
            "Academic artwork review required: raster screenshots are not converted "
            "to production linework. Supply underlying data, segmentation, or a "
            "rights-cleared vector asset."
        )
    selected_format = format_id or str(checked["format_id"])
    if selected_format != checked["format_id"]:
        _fail(
            f"format override {selected_format!r} disagrees with the reviewed record format {checked['format_id']!r}."
        )
    context = context_for(selected_format)
    preset = ACADEMIC_PRESETS[str(checked["preset"])]
    sources = tuple(copy.deepcopy(checked["sources"]))
    source_provider = " / ".join(
        dict.fromkeys(str(source["attribution"]) for source in sources)
    )
    source_license = " / ".join(
        dict.fromkeys(str(source["license"]) for source in sources)
    )
    snapshot = str(checked.get("data_snapshot", "customer-supplied-undated"))
    plate_title = str(checked.get("plate_title", checked["title"]))
    artwork = PlateArtwork(
        subject_id=str(checked["id"]),
        domain="academic",
        subject_kind=str(checked["preset"]),
        title=plate_title,
        document_title=str(checked["title"]),
        subtitle=str(checked["subtitle"]),
        details=_default_details(checked, preset),
        credit_line=str(checked["credit_line"]),
        scale_status="explicit-source-coordinate-systems-and-declared-representation-transforms",
        evidence_status="customer-supplied-content-with-recorded-source-and-transformation-evidence",
        rights_status=str(checked["rights_status"]),
        sources=sources,
        context=context,
        layers=[],
        pen_order=ACADEMIC_PENS,
        artifact_kind=f"academic-{checked['preset']}",
        rendering_preset=f"academic-{checked['preset']}-v1",
        format_subject_policy="academic-scientific-artwork",
        source_provider=source_provider,
        source_license=source_license,
        data_snapshot=snapshot,
        notes=tuple(str(note) for note in checked.get("notes", [])),
        catalog_record=checked,
    )
    working, title_evidence = _render_field_title(
        artwork, checked, _working_field(context)
    )
    working, preset_metadata_evidence = _render_preset_metadata(
        artwork,
        checked,
        preset,
        working,
    )
    elements = list(checked["elements"])
    slots = _slots_for(
        preset,
        elements,
        working,
        float(context.plate["gap_mm"]),
        context,
    )
    element_evidence: list[dict[str, Any]] = []
    for element, slot in zip(elements, slots, strict=True):
        kind = str(element["kind"])
        try:
            renderer = _RENDERERS[kind]
        except KeyError as exc:  # pragma: no cover - raster review is rejected above.
            _fail(f"no academic renderer exists for element kind {kind!r}.")
            raise AssertionError from exc
        evidence = renderer(artwork, element, slot, assets)
        evidence["slot_mm"] = slot.as_dict()
        evidence["source_refs"] = list(element["source_refs"])
        evidence["input_sha256"] = _canonical_sha256(
            {key: item for key, item in element.items() if key != "input_sha256"}
        )
        evidence["transformation"] = copy.deepcopy(element["transformation"])
        element_evidence.append(evidence)
    artwork.rendering_metadata = {
        "academic_schema_version": ACADEMIC_SCHEMA_VERSION,
        "academic_catalog_id": ACADEMIC_CATALOG_ID,
        "academic_preset": preset.as_dict(),
        "composition_mode": checked["composition_mode"],
        "title_layout": title_evidence,
        "preset_metadata_block": preset_metadata_evidence,
        "canonical_title": checked["title"],
        "plate_title": plate_title,
        "metadata": copy.deepcopy(checked["metadata"]),
        "input_record_sha256": _record_input_sha256(checked),
        "specification_path": specification_path,
        "asset_evidence": asset_evidence,
        "elements": element_evidence,
        "scientific_integrity": {
            "source_values_changed": False,
            "smoothing": "none",
            "missing_values_imputed": False,
            "log_axes_linearized": False,
            "fits_kept_distinct_from_measurements": True,
            "raster_production_elements": 0,
            "data_and_transform_metadata_recorded": True,
        },
        "rights_policy": {
            "network_acquisition": False,
            "publisher_pdf_scraping": False,
            "logos_crests_seals_or_trade_dress": False,
            "source_rights_bases": sorted(
                {str(source["rights_basis"]) for source in sources}
            ),
            "direct_reproduction": checked["composition_mode"]
            == "direct-reproduction",
        },
        "no_raster_in_final_svg": True,
        "semantic_layer_model": [
            "measured-data",
            "fit",
            "uncertainty",
            "axes",
            "annotations",
            "structure",
            "text",
            "accent",
        ],
    }
    return artwork


__all__ = [
    "ACADEMIC_CATALOG_ID",
    "ACADEMIC_PENS",
    "ACADEMIC_PRESETS",
    "ACADEMIC_SCHEMA_VERSION",
    "AcademicPreset",
    "LoadedAcademicRecord",
    "academic_presets",
    "build_academic_plate",
    "load_academic_record",
    "validate_academic_record",
]

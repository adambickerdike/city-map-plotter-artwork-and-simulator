#!/usr/bin/env python3
"""Read-only, offline reproducibility checks for the canonical map renderer.

This command deliberately does not render, acquire data, or create a temporary
tree.  It binds the reviewed university recipe to its frozen renderer, source
responses, catalog, style, format, font, and geometry-library environment.  It
also guards the generic high-detail defaults used outside that frozen cohort.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import gzip
import hashlib
from importlib import metadata
import json
import math
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Sequence


# Importing the current renderer is part of the default-invariant check, but the
# checker itself must not leave bytecode behind in a fresh clone.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SRC = ROOT / "src"
for search_path in (str(TOOLS), str(SRC)):
    if search_path not in sys.path:
        sys.path.insert(0, search_path)

import build_ranked_university_series as ranked_builder  # noqa: E402


RECIPE_PATH = ranked_builder.FROZEN_RENDER_RECIPE
RECIPE_VERSION = RECIPE_PATH.stem.removeprefix("render-recipe-")
RECIPE_ID = f"university-memorabilia-ranked-2026-{RECIPE_VERSION}"
SOURCE_CONTRACT_ID = "university-memorabilia-ranked-2026-osm-snapshots-v1"
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}")


class ReproducibilityError(RuntimeError):
    """A committed reproducibility contract is missing or inconsistent."""


@dataclass(frozen=True)
class CheckRecord:
    name: str
    status: str
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {**self.detail, "name": self.name, "status": self.status}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReproducibilityError(f"Could not hash {path}: {exc}") from exc
    return digest.hexdigest()


def _stable_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReproducibilityError(f"Could not read JSON {path}: {exc}") from exc


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReproducibilityError(f"{label} must be a JSON object.")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReproducibilityError(f"{label} must be a JSON array.")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReproducibilityError(f"{label} must be non-empty text.")
    return value.strip()


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ReproducibilityError(
            f"{label} must be an integer greater than or equal to {minimum}."
        )
    return value


def _number(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ReproducibilityError(f"{label} must be a finite number.")
    return float(value)


def _digest(value: object, label: str) -> str:
    digest = _text(value, label)
    if HEX_SHA256.fullmatch(digest) is None:
        raise ReproducibilityError(f"{label} must be a lowercase SHA-256 digest.")
    return digest


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        parts: list[str] = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if unexpected:
            parts.append("unexpected " + ", ".join(unexpected))
        raise ReproducibilityError(f"{label} fields are invalid: {'; '.join(parts)}.")


def _safe_file(base: Path, raw_path: object, label: str) -> Path:
    relative_text = _text(raw_path, f"{label} path")
    relative = PurePosixPath(relative_text)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise ReproducibilityError(f"{label} path is not a canonical relative path.")
    path = (base / Path(*relative.parts)).resolve()
    resolved_base = base.resolve()
    if not path.is_relative_to(resolved_base):
        raise ReproducibilityError(f"{label} path escapes its contract root.")
    if not path.is_file() or path.is_symlink():
        raise ReproducibilityError(
            f"{label} is missing, not a file, or a symlink: {path}"
        )
    return path


def _repository_file(
    contract_root: Path,
    repository_root: Path,
    raw_path: object,
    label: str,
) -> Path:
    relative_text = _text(raw_path, f"{label} path")
    relative = PurePosixPath(relative_text)
    if relative.is_absolute():
        raise ReproducibilityError(f"{label} path must be relative.")
    path = (contract_root / Path(*relative.parts)).resolve()
    if not path.is_relative_to(repository_root.resolve()):
        raise ReproducibilityError(f"{label} path escapes the repository.")
    if not path.is_file() or path.is_symlink():
        raise ReproducibilityError(
            f"{label} is missing, not a file, or a symlink: {path}"
        )
    return path


def _load_recipe(path: Path = RECIPE_PATH) -> dict[str, Any]:
    if path.resolve() != ranked_builder.FROZEN_RENDER_RECIPE.resolve():
        raise ReproducibilityError(
            "Render recipe path differs from build_ranked_university_series.py."
        )
    recipe_sha256 = _sha256_file(path)
    if recipe_sha256 != ranked_builder.EXPECTED_RENDER_RECIPE_SHA256:
        raise ReproducibilityError(
            "Render recipe digest changed: "
            f"observed {recipe_sha256}, expected "
            f"{ranked_builder.EXPECTED_RENDER_RECIPE_SHA256}."
        )
    recipe = _object(_read_json(path), "render recipe")
    _exact_keys(
        recipe,
        {
            "schema_version",
            "id",
            "status",
            "description",
            "renderer",
            "dependencies",
            "environment",
            "cohort",
            "export",
            "fidelity_invariant",
        },
        "render recipe",
    )
    if recipe.get("schema_version") != 1 or recipe.get("id") != RECIPE_ID:
        raise ReproducibilityError(
            f"Render recipe schema or ID is not the frozen {RECIPE_VERSION} contract."
        )
    if recipe.get("status") != "review-only":
        raise ReproducibilityError("Render recipe must remain explicitly review-only.")
    _text(recipe.get("description"), "render recipe description")
    return recipe


def _validate_declared_file(
    *,
    base: Path,
    repository_root: Path,
    block: dict[str, Any],
    path_key: str,
    digest_key: str,
    label: str,
) -> Path:
    path = _repository_file(base, repository_root, block.get(path_key), label)
    expected = _digest(block.get(digest_key), f"{label} declared digest")
    actual = _sha256_file(path)
    if actual != expected:
        raise ReproducibilityError(
            f"{label} digest changed: observed {actual}, expected {expected}."
        )
    return path


def _catalog_subject_ids(catalog_path: Path) -> tuple[str, ...]:
    catalog = _object(_read_json(catalog_path), "ranked university catalog")
    subjects = _list(catalog.get("subjects"), "ranked university catalog subjects")
    result: list[str] = []
    for index, raw_subject in enumerate(subjects):
        subject = _object(raw_subject, f"catalog subject {index}")
        result.append(_text(subject.get("id"), f"catalog subject {index} ID"))
    if len(result) != len(set(result)):
        raise ReproducibilityError("Ranked university catalog contains duplicate IDs.")
    return tuple(result)


def _validate_extent(value: object, label: str) -> None:
    extent = _object(value, label)
    _exact_keys(extent, {"west", "south", "east", "north"}, label)
    west = _number(extent["west"], f"{label} west")
    south = _number(extent["south"], f"{label} south")
    east = _number(extent["east"], f"{label} east")
    north = _number(extent["north"], f"{label} north")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ReproducibilityError(f"{label} is not an ordered WGS84 extent.")


def _validate_snapshot_manifest(
    manifest_path: Path,
    *,
    expected_subject_ids: Sequence[str],
    expected_cohort_sha256: str,
    expected_contract_id: str = SOURCE_CONTRACT_ID,
) -> dict[str, Any]:
    """Validate a source manifest and every compressed response it owns."""

    manifest = _object(_read_json(manifest_path), "university source manifest")
    _exact_keys(
        manifest,
        {
            "schema_version",
            "id",
            "status",
            "as_of",
            "subject_count",
            "license",
            "entries",
            "cohort_sha256",
        },
        "university source manifest",
    )
    if (
        manifest.get("schema_version") != 1
        or manifest.get("id") != expected_contract_id
    ):
        raise ReproducibilityError("University source manifest schema or ID changed.")
    if manifest.get("status") != "review-only-pinned-source":
        raise ReproducibilityError(
            "University source manifest must remain review-only-pinned-source."
        )
    _text(manifest.get("as_of"), "university source manifest as_of")

    expected_ids = tuple(expected_subject_ids)
    subject_count = _integer(
        manifest.get("subject_count"), "university source subject_count", minimum=1
    )
    if subject_count != len(expected_ids):
        raise ReproducibilityError(
            f"University source subject_count is {subject_count}, expected {len(expected_ids)}."
        )

    license_block = _object(manifest.get("license"), "source manifest license")
    _exact_keys(
        license_block,
        {"data", "attribution", "copyright_url", "license_url"},
        "source manifest license",
    )
    if (
        license_block.get("data") != "Open Database License (ODbL) 1.0"
        or license_block.get("attribution") != "© OpenStreetMap contributors"
        or license_block.get("copyright_url")
        != "https://www.openstreetmap.org/copyright"
        or license_block.get("license_url")
        != "https://opendatacommons.org/licenses/odbl/1-0/"
    ):
        raise ReproducibilityError("University source ODbL notice changed.")

    entries = _list(manifest.get("entries"), "university source entries")
    if len(entries) != subject_count:
        raise ReproducibilityError(
            f"University source manifest has {len(entries)} entries, expected {subject_count}."
        )
    entry_ids = tuple(
        _text(_object(entry, "source entry").get("subject_id"), "source subject ID")
        for entry in entries
    )
    if entry_ids != expected_ids:
        raise ReproducibilityError(
            "University source subjects are not the exact ranked catalog cohort/order."
        )

    manifest_root = manifest_path.parent.resolve()
    expected_files: set[Path] = set()
    compressed_bytes = 0
    for index, raw_entry in enumerate(entries):
        entry = _object(raw_entry, f"source entry {index}")
        _exact_keys(
            entry,
            {
                "subject_id",
                "path",
                "size_bytes",
                "sha256",
                "canonical_json_sha256",
                "query_sha256",
                "osm_base_timestamp",
                "extent_wgs84",
            },
            f"source entry {index}",
        )
        subject_id = entry_ids[index]
        expected_relative = f"overpass/{subject_id}.json.gz"
        if entry.get("path") != expected_relative:
            raise ReproducibilityError(
                f"{subject_id} source path is not the canonical subject-keyed path."
            )
        source_path = _safe_file(manifest_root, entry["path"], f"{subject_id} source")
        expected_files.add(source_path)

        expected_size = _integer(
            entry.get("size_bytes"), f"{subject_id} source size", minimum=1
        )
        actual_size = source_path.stat().st_size
        if actual_size != expected_size:
            raise ReproducibilityError(
                f"{subject_id} source size changed: {actual_size}, expected {expected_size}."
            )
        compressed_bytes += actual_size

        expected_file_sha = _digest(entry.get("sha256"), f"{subject_id} source SHA")
        actual_file_sha = _sha256_file(source_path)
        if actual_file_sha != expected_file_sha:
            raise ReproducibilityError(
                f"{subject_id} compressed source SHA changed: {actual_file_sha}, "
                f"expected {expected_file_sha}."
            )

        try:
            with gzip.open(source_path, "rt", encoding="utf-8") as stream:
                response = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReproducibilityError(
                f"{subject_id} source is not valid gzip JSON: {exc}"
            ) from exc
        response_object = _object(response, f"{subject_id} Overpass response")
        expected_canonical_sha = _digest(
            entry.get("canonical_json_sha256"), f"{subject_id} canonical JSON SHA"
        )
        actual_canonical_sha = _stable_digest(response_object)
        if actual_canonical_sha != expected_canonical_sha:
            raise ReproducibilityError(
                f"{subject_id} canonical decoded JSON SHA changed: "
                f"{actual_canonical_sha}, expected {expected_canonical_sha}."
            )
        _digest(entry.get("query_sha256"), f"{subject_id} query SHA")

        osm3s = _object(response_object.get("osm3s"), f"{subject_id} osm3s")
        timestamp = _text(
            osm3s.get("timestamp_osm_base"), f"{subject_id} OSM base timestamp"
        )
        if timestamp != entry.get("osm_base_timestamp"):
            raise ReproducibilityError(f"{subject_id} OSM base timestamp changed.")
        copyright_text = _text(osm3s.get("copyright"), f"{subject_id} copyright")
        if (
            "openstreetmap" not in copyright_text.casefold()
            or "odbl" not in copyright_text.casefold()
        ):
            raise ReproducibilityError(
                f"{subject_id} response does not retain its OpenStreetMap ODbL notice."
            )
        if not isinstance(response_object.get("elements"), list):
            raise ReproducibilityError(
                f"{subject_id} response elements must be an array."
            )
        _validate_extent(entry.get("extent_wgs84"), f"{subject_id} extent")

    overpass_root = manifest_root / "overpass"
    actual_files = {
        path.resolve()
        for path in overpass_root.glob("*.json.gz")
        if path.is_file() and not path.is_symlink()
    }
    if actual_files != expected_files:
        missing = sorted(path.name for path in expected_files - actual_files)
        unexpected = sorted(path.name for path in actual_files - expected_files)
        raise ReproducibilityError(
            "University source file inventory differs from the manifest: "
            f"missing={missing}, unexpected={unexpected}."
        )

    declared_cohort = _digest(
        manifest.get("cohort_sha256"), "source manifest cohort SHA"
    )
    payload = dict(manifest)
    del payload["cohort_sha256"]
    computed_cohort = _stable_digest(payload)
    expected_cohort = _digest(expected_cohort_sha256, "recipe source cohort SHA")
    if declared_cohort != computed_cohort or declared_cohort != expected_cohort:
        raise ReproducibilityError(
            "University source cohort digest changed: "
            f"declared={declared_cohort}, computed={computed_cohort}, "
            f"recipe={expected_cohort}."
        )
    return {
        "contract_id": manifest["id"],
        "subject_count": subject_count,
        "compressed_bytes": compressed_bytes,
        "cohort_sha256": declared_cohort,
    }


def _renderer_source_digest(
    renderer_root: Path,
    *,
    replacements: dict[Path, Path | bytes] | None = None,
) -> tuple[str, int]:
    replacements = replacements or {}
    package_root = renderer_root / "city_map_plotter"
    digest = hashlib.sha256()
    count = 0
    for path in sorted(package_root.rglob("*.py")):
        relative_package = path.relative_to(package_root).as_posix()
        relative_renderer = path.relative_to(renderer_root)
        replacement = replacements.get(relative_renderer)
        if isinstance(replacement, bytes):
            payload = replacement
        elif isinstance(replacement, Path):
            payload = replacement.read_bytes()
        else:
            payload = path.read_bytes()
        digest.update(relative_package.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        count += 1
    if count == 0:
        raise ReproducibilityError("Frozen renderer contains no Python source files.")
    return digest.hexdigest(), count


def _literal_assignment(path: Path, name: str) -> object:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise ReproducibilityError(f"Could not parse {path}: {exc}") from exc
    values: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            values.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            values.append(node.value)
    if len(values) != 1:
        raise ReproducibilityError(f"{path} must assign {name} exactly once.")
    value = values[0]
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
        and len(value.args) == 1
        and not value.keywords
    ):
        value = value.args[0]
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError) as exc:
        raise ReproducibilityError(f"{path} {name} is not a literal contract.") from exc


def _renderer_fingerprint(
    renderer_root: Path,
    *,
    replacements: dict[Path, Path | bytes] | None = None,
) -> dict[str, Any]:
    package_root = renderer_root / "city_map_plotter"
    source_sha256, source_count = _renderer_source_digest(
        renderer_root, replacements=replacements
    )
    format_path = package_root / "data/format-v1.json"
    theme_path = package_root / "data/themes-v1.json"
    format_payload = format_path.read_bytes()
    theme_payload = theme_path.read_bytes()
    format_document = _object(json.loads(format_payload), "frozen format resource")
    theme_document = _object(json.loads(theme_payload), "frozen theme resource")
    version = _text(
        _literal_assignment(package_root / "__init__.py", "__version__"),
        "frozen renderer version",
    )
    fingerprint = {
        "generator": f"city-map-plotter {version}",
        "source_file_count": source_count,
        "source_tree_sha256": source_sha256,
        "format_resource": "city_map_plotter/data/format-v1.json",
        "format_id": format_document.get("id"),
        "format_sha256": _sha256_bytes(format_payload),
        "theme_resource": "city_map_plotter/data/themes-v1.json",
        "theme_catalog_id": theme_document.get("id"),
        "theme_catalog_sha256": _sha256_bytes(theme_payload),
    }
    return {**fingerprint, "sha256": _stable_digest(fingerprint)}


def _validate_renderer_contract(
    recipe: dict[str, Any], recipe_path: Path = RECIPE_PATH
) -> dict[str, Any]:
    contract_root = recipe_path.parent.resolve()
    renderer = _object(recipe.get("renderer"), "recipe renderer")
    _exact_keys(
        renderer,
        {
            "archive",
            "archive_sha256",
            "base_tree_sha256",
            "derived_tree_sha256",
            "derived_fingerprint_sha256",
        },
        "recipe renderer",
    )
    dependencies = _object(recipe.get("dependencies"), "recipe dependencies")
    _exact_keys(
        dependencies,
        {
            "ranked_catalog",
            "ranked_catalog_sha256",
            "style",
            "style_sha256",
            "format",
            "format_sha256",
            "display_font",
            "display_font_sha256",
            "source_manifest",
            "source_manifest_sha256",
            "source_cohort_sha256",
        },
        "recipe dependencies",
    )

    archive = _validate_declared_file(
        base=contract_root,
        repository_root=ROOT,
        block=renderer,
        path_key="archive",
        digest_key="archive_sha256",
        label="frozen renderer archive",
    )
    catalog = _validate_declared_file(
        base=contract_root,
        repository_root=ROOT,
        block=dependencies,
        path_key="ranked_catalog",
        digest_key="ranked_catalog_sha256",
        label="ranked university catalog",
    )
    style = _validate_declared_file(
        base=contract_root,
        repository_root=ROOT,
        block=dependencies,
        path_key="style",
        digest_key="style_sha256",
        label="university style",
    )
    format_path = _validate_declared_file(
        base=contract_root,
        repository_root=ROOT,
        block=dependencies,
        path_key="format",
        digest_key="format_sha256",
        label="frozen format",
    )
    font_path = _validate_declared_file(
        base=contract_root,
        repository_root=ROOT,
        block=dependencies,
        path_key="display_font",
        digest_key="display_font_sha256",
        label="frozen display font",
    )
    source_manifest = _validate_declared_file(
        base=contract_root,
        repository_root=ROOT,
        block=dependencies,
        path_key="source_manifest",
        digest_key="source_manifest_sha256",
        label="university source manifest",
    )

    builder_pairs = {
        "archive_sha256": ranked_builder.EXPECTED_ARCHIVE_SHA256,
        "base_tree_sha256": ranked_builder.EXPECTED_BASE_RENDERER_TREE_SHA256,
        "derived_tree_sha256": ranked_builder.EXPECTED_DERIVED_RENDERER_TREE_SHA256,
        "derived_fingerprint_sha256": (
            ranked_builder.EXPECTED_DERIVED_RENDERER_FINGERPRINT
        ),
    }
    for key, builder_value in builder_pairs.items():
        recipe_value = _digest(renderer.get(key), f"recipe renderer {key}")
        if recipe_value != builder_value:
            raise ReproducibilityError(
                f"Recipe {key} differs from build_ranked_university_series.py."
            )
    dependency_builder_pairs = {
        "ranked_catalog_sha256": ranked_builder.EXPECTED_CATALOG_SHA256,
        "style_sha256": ranked_builder.EXPECTED_STYLE_SHA256,
        "source_manifest_sha256": (ranked_builder.EXPECTED_SOURCE_MANIFEST_SHA256),
        "source_cohort_sha256": ranked_builder.EXPECTED_SOURCE_COHORT_SHA256,
    }
    for key, builder_value in dependency_builder_pairs.items():
        recipe_value = _digest(dependencies.get(key), f"recipe dependency {key}")
        if recipe_value != builder_value:
            raise ReproducibilityError(
                f"Recipe {key} differs from build_ranked_university_series.py."
            )

    frozen_renderer = archive.parent / "renderer-contract"
    if frozen_renderer.resolve() != ranked_builder.FROZEN_RENDERER.resolve():
        raise ReproducibilityError(
            "Recipe archive does not bind the builder renderer tree."
        )
    if archive.resolve() != ranked_builder.FROZEN_ARCHIVE.resolve():
        raise ReproducibilityError(
            "Recipe archive path differs from the builder renderer archive."
        )
    if catalog.resolve() != ranked_builder.CATALOG.resolve():
        raise ReproducibilityError(
            "Recipe catalog path differs from the builder catalog."
        )
    if style.resolve() != ranked_builder.FROZEN_STYLE.resolve():
        raise ReproducibilityError("Recipe style path differs from the builder style.")
    if source_manifest.resolve() != ranked_builder.FROZEN_SOURCE_MANIFEST.resolve():
        raise ReproducibilityError(
            "Recipe source manifest path differs from the builder source manifest."
        )

    base_tree = ranked_builder._tree_digest(frozen_renderer)
    if base_tree != renderer["base_tree_sha256"]:
        raise ReproducibilityError(
            f"Frozen base renderer tree changed: observed {base_tree}."
        )
    override_files = {
        Path("city_map_plotter/cartography.py"): (
            ranked_builder.PATCHED_CARTOGRAPHY,
            ranked_builder.EXPECTED_PATCHED_CARTOGRAPHY_SHA256,
        ),
        Path("city_map_plotter/batch.py"): (
            ranked_builder.PATCHED_BATCH,
            ranked_builder.EXPECTED_PATCHED_BATCH_SHA256,
        ),
        Path("city_map_plotter/cli.py"): (
            ranked_builder.PATCHED_CLI,
            ranked_builder.EXPECTED_PATCHED_CLI_SHA256,
        ),
        Path("city_map_plotter/completeness.py"): (
            ranked_builder.PATCHED_COMPLETENESS,
            ranked_builder.EXPECTED_PATCHED_COMPLETENESS_SHA256,
        ),
    }
    replacements: dict[Path, Path | bytes] = {}
    for relative, (path, expected) in override_files.items():
        actual = _sha256_file(path)
        if actual != expected:
            raise ReproducibilityError(
                f"Frozen override {path.name} changed: observed {actual}."
            )
        replacements[relative] = path

    base_svg = frozen_renderer / "city_map_plotter/svg.py"
    if _sha256_file(base_svg) != ranked_builder.EXPECTED_BASE_SVG_SHA256:
        raise ReproducibilityError("Frozen base SVG source changed.")
    try:
        patched_svg = ranked_builder._patched_svg_payload(
            base_svg, ranked_builder.PATCHED_SVG
        )
    except ranked_builder.SeriesBuildError as exc:
        raise ReproducibilityError(
            f"Could not reproduce frozen SVG patch: {exc}"
        ) from exc
    patched_svg_sha = _sha256_bytes(patched_svg)
    if patched_svg_sha != ranked_builder.EXPECTED_PATCHED_SVG_SHA256:
        raise ReproducibilityError(
            f"Derived SVG digest changed: observed {patched_svg_sha}."
        )
    replacements[Path("city_map_plotter/svg.py")] = patched_svg

    derived_tree = ranked_builder._tree_digest(
        frozen_renderer, replacements=replacements
    )
    if derived_tree != renderer["derived_tree_sha256"]:
        raise ReproducibilityError(
            f"Derived renderer tree changed: observed {derived_tree}."
        )
    base_fingerprint = _renderer_fingerprint(frozen_renderer)
    if base_fingerprint["sha256"] != ranked_builder.EXPECTED_RENDERER_FINGERPRINT:
        raise ReproducibilityError("Frozen base renderer fingerprint changed.")
    derived_fingerprint = _renderer_fingerprint(
        frozen_renderer, replacements=replacements
    )
    if derived_fingerprint["sha256"] != renderer["derived_fingerprint_sha256"]:
        raise ReproducibilityError(
            "Derived renderer fingerprint changed: observed "
            f"{derived_fingerprint['sha256']}."
        )

    return {
        "catalog": catalog,
        "source_manifest": source_manifest,
        "format": format_path,
        "display_font": font_path,
        "archive_sha256": renderer["archive_sha256"],
        "base_tree_sha256": base_tree,
        "derived_tree_sha256": derived_tree,
        "derived_fingerprint_sha256": derived_fingerprint["sha256"],
    }


def _option_value(command: Sequence[str], option: str) -> str:
    if command.count(option) != 1:
        raise ReproducibilityError(
            f"Reviewed builder command must contain {option} exactly once."
        )
    index = command.index(option)
    if index + 1 >= len(command):
        raise ReproducibilityError(
            f"Reviewed builder command has no value for {option}."
        )
    return command[index + 1]


def _frozen_family_layers(path: Path) -> dict[str, set[str]]:
    raw = _literal_assignment(path, "FAMILIES")
    if not isinstance(raw, dict):
        raise ReproducibilityError("Frozen FAMILIES contract must be a dictionary.")
    result: dict[str, set[str]] = {}
    for family, layers in raw.items():
        if (
            not isinstance(family, str)
            or not isinstance(layers, set)
            or not all(isinstance(layer, str) for layer in layers)
        ):
            raise ReproducibilityError("Frozen FAMILIES contract is malformed.")
        result[family] = set(layers)
    return result


def _validate_generic_fidelity(recipe: dict[str, Any]) -> dict[str, Any]:
    from city_map_plotter import cartography, cli, styles

    invariant = _object(recipe.get("fidelity_invariant"), "fidelity invariant")
    _exact_keys(
        invariant,
        {
            "generic_default",
            "reviewed_physical_profile",
            "shared_cartographic_selection",
            "shared_default_simplify_mm",
            "shared_default_road_style",
        },
        "fidelity invariant",
    )
    export = _object(recipe.get("export"), "recipe export")
    reviewed_families_raw = _list(export.get("families"), "recipe export families")
    reviewed_families = tuple(
        _text(value, "recipe export family") for value in reviewed_families_raw
    )
    if len(reviewed_families) != len(set(reviewed_families)):
        raise ReproducibilityError("Recipe export families contain duplicates.")

    args = cli._parser().parse_args(
        [
            "export",
            "--bbox",
            "-1.01",
            "53.95",
            "-0.99",
            "53.97",
            "--output",
            "unused-reproducibility-check.svg",
        ]
    )
    generic_default = _text(invariant.get("generic_default"), "generic default")
    if args.detail_profile != generic_default or generic_default != "faithful":
        raise ReproducibilityError(
            f"Generic export default is {args.detail_profile!r}, expected faithful."
        )
    expected_road_style = _text(
        invariant.get("shared_default_road_style"), "shared road style"
    )
    expected_simplify = _number(
        invariant.get("shared_default_simplify_mm"), "shared simplify_mm"
    )
    if cli._resolved_road_style(generic_default, None) != expected_road_style:
        raise ReproducibilityError(
            "Generic faithful no longer resolves centreline roads."
        )
    if cli._resolved_simplify_mm(generic_default, None) != expected_simplify:
        raise ReproducibilityError("Generic faithful no longer resolves 0.04 mm.")

    reviewed_profile = _text(
        invariant.get("reviewed_physical_profile"), "reviewed physical profile"
    )
    if export.get("detail_profile") != reviewed_profile:
        raise ReproducibilityError("Recipe export and fidelity profile disagree.")
    if export.get("road_style") != expected_road_style:
        raise ReproducibilityError("Recipe export and fidelity road style disagree.")
    if float(export.get("simplify_mm", -1)) != expected_simplify:
        raise ReproducibilityError("Recipe export and fidelity simplify_mm disagree.")
    if invariant.get("shared_cartographic_selection") is not True:
        raise ReproducibilityError("Recipe must assert shared cartographic selection.")

    if generic_default not in cartography.SOURCE_COMPLETE_DETAIL_PROFILES:
        raise ReproducibilityError("Generic faithful is not source-complete.")
    if reviewed_profile not in cartography.SOURCE_COMPLETE_DETAIL_PROFILES:
        raise ReproducibilityError("Reviewed plotter-faithful is not source-complete.")
    if not {generic_default, reviewed_profile}.issubset(
        cartography.FULL_CARTOGRAPHY_DETAIL_PROFILES
    ):
        raise ReproducibilityError(
            "Generic and reviewed high-detail profiles no longer share full selection."
        )

    frozen_cartography = ranked_builder.PATCHED_CARTOGRAPHY
    for name in (
        "ROAD_LAYERS",
        "AREA_OUTLINE_LAYERS",
        "SOURCE_COMPLETE_DETAIL_PROFILES",
        "SURFACE_CENTRELINE_WATERWAYS",
    ):
        reviewed_value = _literal_assignment(frozen_cartography, name)
        current_value = getattr(cartography, name)
        if not isinstance(reviewed_value, (set, frozenset)) or not set(
            current_value
        ).issuperset(reviewed_value):
            raise ReproducibilityError(
                f"Current high-detail {name} is narrower than the reviewed renderer."
            )

    frozen_styles = ranked_builder.FROZEN_RENDERER / "city_map_plotter/styles.py"
    reviewed_family_map = _frozen_family_layers(frozen_styles)
    try:
        reviewed_layers = set().union(
            *(reviewed_family_map[family] for family in reviewed_families)
        )
    except KeyError as exc:
        raise ReproducibilityError(
            f"Recipe names a family absent from the reviewed renderer: {exc.args[0]}"
        ) from exc
    current_layers = styles.enabled_layer_ids(reviewed_families)
    if not current_layers.issuperset(reviewed_layers):
        raise ReproducibilityError(
            "Current layer-family selection is narrower than the reviewed selection: "
            + ", ".join(sorted(reviewed_layers - current_layers))
        )
    return {
        "generic_default": generic_default,
        "reviewed_profile": reviewed_profile,
        "road_style": expected_road_style,
        "simplify_mm": expected_simplify,
        "reviewed_families": list(reviewed_families),
        "reviewed_layer_count": len(reviewed_layers),
        "current_layer_count": len(current_layers),
    }


def _parse_requirements(path: Path) -> tuple[dict[str, str], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReproducibilityError(
            f"Could not read requirements file {path}: {exc}"
        ) from exc
    pins: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", line)
        if match is None:
            raise ReproducibilityError(
                f"Requirements line {line_number} is not an exact package pin."
            )
        name = match.group(1).casefold().replace("_", "-")
        if name in pins:
            raise ReproducibilityError(f"Requirements repeat package {name}.")
        pins[name] = match.group(2)
    if not pins:
        raise ReproducibilityError("University requirements contain no package pins.")
    return pins, text


def _validate_environment(
    recipe: dict[str, Any], recipe_path: Path = RECIPE_PATH
) -> dict[str, Any]:
    environment = _object(recipe.get("environment"), "recipe environment")
    _exact_keys(
        environment,
        {"python", "numpy", "shapely", "geos", "inkscape_for_png", "requirements"},
        "recipe environment",
    )
    expected = {
        key: _text(environment.get(key), f"recipe environment {key}")
        for key in ("python", "numpy", "shapely", "geos", "inkscape_for_png")
    }
    requirements_path = _repository_file(
        recipe_path.parent,
        ROOT,
        environment.get("requirements"),
        "university requirements",
    )
    pins, requirements_text = _parse_requirements(requirements_path)
    expected_pins = {"numpy": expected["numpy"], "shapely": expected["shapely"]}
    if pins != expected_pins:
        raise ReproducibilityError(
            f"Requirements pins {pins} do not exactly match recipe {expected_pins}."
        )
    if f"Python {expected['python']}" not in requirements_text:
        raise ReproducibilityError(
            "Requirements do not record the recipe Python version."
        )

    observed_python = platform.python_version()
    if observed_python != expected["python"]:
        raise ReproducibilityError(
            f"Python is {observed_python}, expected {expected['python']}."
        )
    observed_packages: dict[str, str] = {}
    for package_name, expected_version in pins.items():
        try:
            observed = metadata.version(package_name)
        except metadata.PackageNotFoundError as exc:
            raise ReproducibilityError(
                f"Required package {package_name} is not installed."
            ) from exc
        if observed != expected_version:
            raise ReproducibilityError(
                f"{package_name} is {observed}, expected {expected_version}."
            )
        observed_packages[package_name] = observed

    try:
        import shapely
    except ImportError as exc:
        raise ReproducibilityError("Could not import pinned Shapely.") from exc
    observed_geos = str(shapely.geos_version_string)
    if observed_geos != expected["geos"]:
        raise ReproducibilityError(
            f"GEOS is {observed_geos}, expected {expected['geos']}."
        )
    return {
        "python": observed_python,
        "packages": observed_packages,
        "geos": observed_geos,
        "requirements": str(requirements_path.relative_to(ROOT)),
        "expected_inkscape": expected["inkscape_for_png"],
    }


def _probe_inkscape() -> tuple[str | None, str]:
    executable = shutil.which("inkscape")
    if executable is None:
        return None, "Inkscape is not installed on PATH."
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Could not query Inkscape: {exc}"
    output = " ".join((result.stdout or result.stderr).split())
    match = re.search(r"\bInkscape\s+([0-9]+(?:\.[0-9]+){1,3})\b", output)
    if result.returncode != 0 or match is None:
        return None, output or f"Inkscape exited {result.returncode}."
    return match.group(1), output


def _inkscape_check(
    expected: str,
    *,
    strict: bool,
    probe: Callable[[], tuple[str | None, str]] = _probe_inkscape,
) -> CheckRecord:
    observed, raw = probe()
    matches = observed == expected
    detail = {
        "expected": expected,
        "observed": observed,
        "raw": raw,
        "strict": strict,
    }
    if strict and not matches:
        return CheckRecord("inkscape", "fail", detail)
    return CheckRecord("inkscape", "pass" if strict else "info", detail)


def run_checks(
    *,
    strict_tools: bool = False,
    recipe_path: Path = RECIPE_PATH,
    inkscape_probe: Callable[[], tuple[str | None, str]] | None = None,
) -> dict[str, Any]:
    checks: list[CheckRecord] = []
    failures: list[str] = []
    recipe: dict[str, Any] | None = None

    def run(name: str, function: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        try:
            detail = function()
        except (OSError, ValueError, ReproducibilityError) as exc:
            failures.append(f"{name}: {exc}")
            checks.append(CheckRecord(name, "fail", {"message": str(exc)}))
            return None
        checks.append(CheckRecord(name, "pass", detail))
        return detail

    recipe_value = run("recipe", lambda: _load_recipe(recipe_path))
    if recipe_value is not None:
        recipe = recipe_value
        renderer_detail = run(
            "renderer-contract",
            lambda: _validate_renderer_contract(recipe, recipe_path),
        )
        if renderer_detail is not None:
            source_manifest = Path(renderer_detail["source_manifest"])
            catalog = Path(renderer_detail["catalog"])
            dependencies = _object(recipe["dependencies"], "recipe dependencies")
            cohort = _object(recipe["cohort"], "recipe cohort")
            expected_count = _integer(
                cohort.get("subject_count"), "recipe cohort subject_count", minimum=1
            )
            subject_ids = _catalog_subject_ids(catalog)
            if len(subject_ids) != expected_count:
                failures.append(
                    "source-contract: ranked catalog subject count differs from recipe"
                )
                checks.append(
                    CheckRecord(
                        "source-contract",
                        "fail",
                        {
                            "message": (
                                "Ranked catalog subject count differs from recipe: "
                                f"{len(subject_ids)} != {expected_count}."
                            )
                        },
                    )
                )
            else:
                run(
                    "source-contract",
                    lambda: _validate_snapshot_manifest(
                        source_manifest,
                        expected_subject_ids=subject_ids,
                        expected_cohort_sha256=_digest(
                            dependencies.get("source_cohort_sha256"),
                            "recipe source cohort SHA",
                        ),
                    ),
                )
        run("generic-high-detail", lambda: _validate_generic_fidelity(recipe))
        environment_detail = run(
            "python-geometry-environment",
            lambda: _validate_environment(recipe, recipe_path),
        )
        if environment_detail is not None:
            probe = inkscape_probe or _probe_inkscape
            inkscape = _inkscape_check(
                str(environment_detail["expected_inkscape"]),
                strict=strict_tools,
                probe=probe,
            )
            checks.append(inkscape)
            if inkscape.status == "fail":
                failures.append(
                    "inkscape: observed version does not match the strict recipe tool version"
                )

    return {
        "schema_version": 1,
        "checker": "city-map-reproducibility-v1",
        "recipe": str(recipe_path),
        "strict_tools": strict_tools,
        "passed": not failures,
        "checks": [check.as_dict() for check in checks],
        "failures": failures,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify committed map source, renderer, fidelity, and environment "
            "contracts without rendering or network access."
        )
    )
    parser.add_argument(
        "--strict-tools",
        action="store_true",
        help="Require the installed Inkscape version to match the reviewed PNG tool.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the machine-readable report instead of concise check lines.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_checks(strict_tools=args.strict_tools)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        for check in report["checks"]:
            status = str(check["status"]).upper()
            if check["name"] == "inkscape":
                summary = check.get("raw") or "not available"
            elif check["status"] == "fail":
                summary = check.get("message", "failed")
            else:
                summary = "verified"
            print(f"{status:4} {check['name']}: {summary}")
        print(
            "PASS map reproducibility"
            if report["passed"]
            else "FAIL map reproducibility"
        )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

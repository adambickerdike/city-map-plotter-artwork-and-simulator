#!/usr/bin/env python3
"""Build the ranked-university cohort with the frozen v2.1 visual contract."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src/city_map_plotter/data/ranked-universities-2026-v1.json"
CONTRACT_BUNDLE = ROOT / "contracts/university-memorabilia-v2.1"
FROZEN_RENDERER = CONTRACT_BUNDLE / "base/renderer-contract"
FROZEN_ARCHIVE = CONTRACT_BUNDLE / "base/renderer-contract.tar"
FROZEN_STYLE = FROZEN_RENDERER / "university-memorabilia-v2.json"
FROZEN_SOURCE_CONTRACT = CONTRACT_BUNDLE / "source-snapshots"
FROZEN_SOURCE_MANIFEST = FROZEN_SOURCE_CONTRACT / "source-manifest.json"
FROZEN_RENDER_RECIPE = CONTRACT_BUNDLE / "render-recipe-v2.1.4.json"
PATCHED_CARTOGRAPHY = CONTRACT_BUNDLE / "overrides/cartography.py"
PATCHED_BATCH = CONTRACT_BUNDLE / "overrides/batch.py"
PATCHED_CLI = CONTRACT_BUNDLE / "overrides/cli.py"
PATCHED_COMPLETENESS = CONTRACT_BUNDLE / "overrides/completeness.py"
PATCHED_SVG = CONTRACT_BUNDLE / "overrides/svg.py"
DEFAULT_OUTPUT = ROOT / "review-output/university-memorabilia-ranked-2026-v2.1.4"

EXPECTED_CATALOG_SHA256 = (
    "9a58174a4e13f0ac7a66f9a91d4789d63ca71cdcb7d899d534731deca4f1e5fe"
)

UK_COLLECTION = "uk-times-good-university-guide-2026-top-30"
US_COLLECTION = "us-qs-world-university-rankings-2027-top-20"

EXPECTED_ARCHIVE_SHA256 = (
    "794e4a44716e3739d22200370203a171cad05a52f79b5f00949c461fa46998f7"
)
EXPECTED_STYLE_SHA256 = (
    "d5bc3c092d6cc05bbbc9581b5463a043716cfd5a8b237f8df634a44d6b6f7910"
)
EXPECTED_SOURCE_MANIFEST_SHA256 = (
    "581d07bc7262664d4b1134ec42b30ddd9a086f9915e067068b4f0fe77e121362"
)
EXPECTED_SOURCE_COHORT_SHA256 = (
    "1a861b085466c23ce2c97ce03b3807459bf96cf3e0c3c0faccbf3079b97cd6d3"
)
EXPECTED_RENDER_RECIPE_SHA256 = (
    "a3a8e6932fe4d6175e90ec25d5ce30922dad39c434297ef9a6a722a96dd153fc"
)
EXPECTED_RENDERER_FINGERPRINT = (
    "375d54f1cb29c68227dd5ddff8d05235b8f137d4e433ae31bd5834d611dab5e7"
)
EXPECTED_BASE_RENDERER_TREE_SHA256 = (
    "648f15740f25916df854045581adbe20df067bbb55d30ecf8fe3a366d4db286d"
)
# Updated only after the ranked-series correctness patches have passed their
# focused tests and real-data probes. Keeping exact source digests here makes
# the v2.1.4 derivation fail closed if the working renderer later changes for
# an unrelated reason.
EXPECTED_PATCHED_CARTOGRAPHY_SHA256 = (
    "454e60954507f5cc056a14c0d4ddd6b80df4d511860f1edafeabbed49c361dc2"
)
EXPECTED_PATCHED_BATCH_SHA256 = (
    "25028ecd6009b687207c98af7fcecb0db6b494c1950be900431558e84097030f"
)
EXPECTED_PATCHED_CLI_SHA256 = (
    "f0433203d60088243fa4dc796b2b508926ad124c1fb2eaa096177b714b21ff7b"
)
EXPECTED_PATCHED_COMPLETENESS_SHA256 = (
    "6cef168aae36ad154175ac6137ae019052229ea59889285887fc08d96018d75d"
)
EXPECTED_SVG_PATCH_FUNCTIONS_SHA256 = (
    "2e97ed4b022cf45846f40ce7bd6b7320b8e397061f2b5acdcdbbb3e87ddd5adc"
)
EXPECTED_BASE_SVG_SHA256 = (
    "ea5af44c9bb0f20e1f26ab4ce455fa1fcba2be2d69183a2962430893e4a7b227"
)
# Filled from the exact frozen-base backport below. It is deliberately distinct
# from the working SVG file: only the pinned fixed-slot functions and call
# sites are transplanted, so unrelated post-v2.1 renderer features stay out.
EXPECTED_PATCHED_SVG_SHA256 = (
    "755f896b0fa22667e0898a788a4840e89a562f2f9c8c11423c86eab48abaf57c"
)
EXPECTED_DERIVED_RENDERER_TREE_SHA256 = (
    "2197dace775a09bd73d70cc9233be3689f5439f0e10942000ebc191f42065e4c"
)
EXPECTED_DERIVED_RENDERER_FINGERPRINT = (
    "0a2106ff042bcccb7e73ad5fe3d253d0a5c7c18f2ca2425a35ea33aa29f69366"
)
EXTERNAL_ATTRIBUTION = (
    "Accompanying product page, packaging, and series attribution file"
)


class SeriesBuildError(RuntimeError):
    """Raised when a frozen dependency or release target is unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_file(path: Path, expected_sha256: str | None = None) -> None:
    if not path.is_file():
        raise SeriesBuildError(f"Required release dependency is missing: {path}")
    if expected_sha256 is not None:
        actual = _sha256(path)
        if actual != expected_sha256:
            raise SeriesBuildError(
                f"Frozen dependency changed: {path} has {actual}, expected "
                f"{expected_sha256}."
            )


def _copy_exact(source: Path, destination: Path) -> None:
    """Copy once, or verify that an existing release copy is byte-identical."""

    if destination.exists():
        if not destination.is_file() or _sha256(destination) != _sha256(source):
            raise SeriesBuildError(
                f"Release dependency already exists with different bytes: {destination}"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_source_contract_exact(destination: Path) -> Path:
    """Copy and verify the complete subject-keyed saved-JSON source cohort."""

    _assert_file(FROZEN_SOURCE_MANIFEST, EXPECTED_SOURCE_MANIFEST_SHA256)
    _assert_file(FROZEN_RENDER_RECIPE, EXPECTED_RENDER_RECIPE_SHA256)
    try:
        manifest = json.loads(FROZEN_SOURCE_MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SeriesBuildError(f"Frozen source manifest is invalid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SeriesBuildError("Frozen source manifest must contain a JSON object.")
    if (
        manifest.get("subject_count") != 50
        or manifest.get("cohort_sha256") != EXPECTED_SOURCE_COHORT_SHA256
    ):
        raise SeriesBuildError(
            "Frozen source manifest does not identify the reviewed 50-subject cohort."
        )
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != 50:
        raise SeriesBuildError(
            "Frozen source manifest must contain exactly 50 source entries."
        )

    destination.mkdir(parents=True, exist_ok=True)
    _copy_exact(FROZEN_SOURCE_MANIFEST, destination / FROZEN_SOURCE_MANIFEST.name)
    for sidecar_name in ("NOTICE.md", "CHECKSUMS.sha256"):
        sidecar = FROZEN_SOURCE_CONTRACT / sidecar_name
        _assert_file(sidecar)
        _copy_exact(sidecar, destination / sidecar_name)

    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SeriesBuildError("Frozen source manifest entry is not an object.")
        subject_id = entry.get("subject_id")
        relative_text = entry.get("path")
        expected_sha256 = entry.get("sha256")
        if (
            not isinstance(subject_id, str)
            or not subject_id
            or subject_id in seen
            or not isinstance(relative_text, str)
            or not relative_text
            or not isinstance(expected_sha256, str)
        ):
            raise SeriesBuildError("Frozen source manifest entry is malformed.")
        seen.add(subject_id)
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise SeriesBuildError(
                f"Frozen source path escapes its contract: {relative_text!r}."
            )
        source = FROZEN_SOURCE_CONTRACT / relative
        _assert_file(source, expected_sha256)
        _copy_exact(source, destination / relative)
    return destination / FROZEN_SOURCE_MANIFEST.name


def _function_source(source: str, name: str) -> str:
    """Extract one top-level function, including its exact source spelling."""

    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise SeriesBuildError(f"SVG patch source cannot be parsed: {exc}") from exc
    matches = [
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(matches) != 1 or matches[0].end_lineno is None:
        raise SeriesBuildError(
            f"SVG patch source must contain exactly one complete {name} function."
        )
    node = matches[0]
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno]).rstrip() + "\n"


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if source.count(old) != 1:
        raise SeriesBuildError(
            f"Frozen SVG patch anchor {label!r} does not occur exactly once."
        )
    return source.replace(old, new, 1)


def _replace_function_span(
    source: str,
    start_function: str,
    end_function: str,
    replacement: str,
) -> str:
    start_marker = f"def {start_function}("
    end_marker = f"def {end_function}("
    if source.count(start_marker) != 1 or source.count(end_marker) != 1:
        raise SeriesBuildError(
            "Frozen SVG function-span anchors are missing or ambiguous: "
            f"{start_function!r}..{end_function!r}."
        )
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[:start] + replacement.rstrip() + "\n\n\n" + source[end:]


def _patched_svg_payload(base_svg: Path, patch_source: Path) -> bytes:
    """Backport only fixed inventory-slot behavior into the frozen v2.1 SVG."""

    _assert_file(base_svg, EXPECTED_BASE_SVG_SHA256)
    _assert_file(patch_source)
    base = base_svg.read_text(encoding="utf-8")
    reviewed = patch_source.read_text(encoding="utf-8")
    function_names = (
        "_layer_stat_pen_key",
        "_reorder_document_layers_by_pen",
        "_attach_calibration_settings",
        "_pen_layer_setting",
        "_pen_sequence",
    )
    function_sources = {
        name: _function_source(reviewed, name) for name in function_names
    }
    function_payload = "\0".join(
        f"{name}\0{function_sources[name]}" for name in function_names
    ).encode("utf-8")
    if (
        hashlib.sha256(function_payload).hexdigest()
        != EXPECTED_SVG_PATCH_FUNCTIONS_SHA256
    ):
        raise SeriesBuildError("Reviewed fixed-slot function payload changed.")

    reorder_block = "\n\n".join(
        function_sources[name].rstrip()
        for name in ("_layer_stat_pen_key", "_reorder_document_layers_by_pen")
    )
    base = _replace_function_span(
        base,
        "_reorder_document_layers_by_pen",
        "_plot_metrics_dict",
        reorder_block,
    )
    sequence_block = "\n\n".join(
        function_sources[name].rstrip()
        for name in (
            "_attach_calibration_settings",
            "_pen_layer_setting",
            "_pen_sequence",
        )
    )
    base = _replace_function_span(
        base,
        "_attach_calibration_settings",
        "render_svg",
        sequence_block,
    )
    base = _replace_once(
        base,
        "    _reorder_document_layers_by_pen(root, layer_stats)\n",
        (
            '    fixed_inventory_slots = poster_layout == "university-memorabilia"\n'
            "    _reorder_document_layers_by_pen(\n"
            "        root, layer_stats, fixed_inventory_slots=fixed_inventory_slots\n"
            "    )\n"
        ),
        "fixed reorder call",
    )
    base = _replace_once(
        base,
        "    pen_sequence = _pen_sequence(layer_stats, document_layer_ids)\n",
        (
            "    pen_sequence = _pen_sequence(\n"
            "        layer_stats,\n"
            "        document_layer_ids,\n"
            "        fixed_inventory_slots=fixed_inventory_slots,\n"
            "    )\n"
        ),
        "fixed pen sequence call",
    )
    base = _replace_once(
        base,
        (
            '            "physical_pen_steps": len(pen_sequence),\n'
            '            "pen_changes": max(0, len(pen_sequence) - 1),\n'
        ),
        (
            '            "inventory_pen_slots": len(pen_sequence),\n'
            '            "physical_pen_steps": sum(\n'
            '                not bool(step.get("empty", False)) for step in pen_sequence\n'
            "            ),\n"
            '            "pen_changes": max(\n'
            "                0,\n"
            '                sum(not bool(step.get("empty", False)) for step in pen_sequence) - 1,\n'
            "            ),\n"
        ),
        "inventory plot summary",
    )
    base = _replace_once(
        base,
        (
            '        root.set(f"{{{MAP_NS}}}pen-profile", '
            'str(pen.get("pen_profile", "style")))\n'
        ),
        (
            '        root.set(f"{{{MAP_NS}}}pen-profile", '
            'str(pen.get("pen_profile", "style")))\n'
            "        root.set(\n"
            '            f"{{{MAP_NS}}}pen-slot-status",\n'
            '            str(pen.get("slot_status", "active")),\n'
            "        )\n"
            '        root.set(f"{{{MAP_NS}}}path-count", '
            'str(int(pen.get("path_count", 0))))\n'
        ),
        "split root slot metadata",
    )
    base = _replace_once(
        base,
        (
            '                "calibration_substrate": pen.get("calibration_substrate"),\n'
            '                "layers": list(pen["layers"]),\n'
        ),
        (
            '                "calibration_substrate": pen.get("calibration_substrate"),\n'
            '                "configured_layers": list(\n'
            '                    pen.get("configured_layers", pen["layers"])\n'
            "                ),\n"
            '                "layers": list(pen["layers"]),\n'
            '                "omitted_layers": list(pen.get("omitted_layers", [])),\n'
            '                "path_count": int(pen.get("path_count", 0)),\n'
            '                "empty": bool(pen.get("empty", False)),\n'
            '                "slot_status": str(pen.get("slot_status", "active")),\n'
        ),
        "split manifest slot metadata",
    )
    try:
        ast.parse(base)
    except SyntaxError as exc:
        raise SeriesBuildError(f"Patched frozen SVG is invalid Python: {exc}") from exc
    return base.encode("utf-8")


def _tree_digest(
    root: Path,
    *,
    replacements: dict[Path, Path | bytes] | None = None,
) -> str:
    replacements = replacements or {}
    digest = hashlib.sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and item.suffix not in {".pyc", ".pyo"}
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        relative_path = path.relative_to(root)
        payload = replacements.get(relative_path, path)
        payload_digest = (
            hashlib.sha256(payload).digest()
            if isinstance(payload, bytes)
            else bytes.fromhex(_sha256(payload))
        )
        digest.update(payload_digest)
    return digest.hexdigest()


def _copy_renderer_exact(source: Path, destination: Path) -> None:
    patched_svg = _patched_svg_payload(
        source / "city_map_plotter/svg.py", PATCHED_SVG
    )
    replacements: dict[Path, Path | bytes] = {
        Path("city_map_plotter/cartography.py"): PATCHED_CARTOGRAPHY,
        Path("city_map_plotter/batch.py"): PATCHED_BATCH,
        Path("city_map_plotter/cli.py"): PATCHED_CLI,
        Path("city_map_plotter/completeness.py"): PATCHED_COMPLETENESS,
        Path("city_map_plotter/svg.py"): patched_svg,
    }
    expected_digest = _tree_digest(source, replacements=replacements)
    if expected_digest != EXPECTED_DERIVED_RENDERER_TREE_SHA256:
        raise SeriesBuildError(
            "Derived renderer inputs do not match the declared tree digest."
        )
    if destination.exists():
        if not destination.is_dir() or _tree_digest(destination) != expected_digest:
            raise SeriesBuildError(
                f"Derived renderer copy differs from its declared source: {destination}"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    for relative, replacement in replacements.items():
        target = destination / relative
        if isinstance(replacement, bytes):
            target.write_bytes(replacement)
        else:
            shutil.copy2(replacement, target)
    if _tree_digest(destination) != expected_digest:
        raise SeriesBuildError("Derived renderer copy did not reproduce exactly.")


def _renderer_fingerprint(
    renderer_root: Path,
    *,
    expected_sha256: str,
    label: str,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(renderer_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        "-B",
        "-c",
        (
            "import json; "
            "from city_map_plotter.batch import renderer_format_fingerprint; "
            "print(json.dumps(renderer_format_fingerprint(), sort_keys=True))"
        ),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=environment,
    )
    if result.returncode != 0:
        raise SeriesBuildError(
            "Could not fingerprint the frozen renderer: " + result.stderr.strip()
        )
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get("sha256") != expected_sha256:
        raise SeriesBuildError(
            f"{label} renderer fingerprint is not the declared contract: "
            f"{value.get('sha256') if isinstance(value, dict) else value!r}."
        )
    return value


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ranking_catalog_markdown(catalog_path: Path) -> str:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    collections = catalog.get("collections")
    subjects = catalog.get("subjects")
    if not isinstance(collections, list) or not isinstance(subjects, list):
        raise SeriesBuildError("Ranked university catalog has no collections/subjects.")
    subject_by_id = {
        str(subject["id"]): subject
        for subject in subjects
        if isinstance(subject, dict) and isinstance(subject.get("id"), str)
    }
    lines = [
        "# Ranked university plate catalog",
        "",
        "This is the exact ordered cohort used by the batch. Each row produces an ",
        "institution-specific, campus-centred plate; the prominent on-sheet title ",
        "remains the city/locality to preserve the existing memorabilia design.",
        "",
    ]
    for collection in collections:
        if not isinstance(collection, dict):
            raise SeriesBuildError("Ranked university collection is malformed.")
        entries = collection.get("entries")
        if not isinstance(entries, list):
            raise SeriesBuildError("Ranked university collection has no entries.")
        lines.extend((f"## {collection.get('title', collection.get('id'))}", ""))
        source_urls = collection.get("source_urls", [])
        if isinstance(source_urls, list):
            for index, url in enumerate(source_urls, start=1):
                lines.append(f"Ranking source {index}: {url}")
            if source_urls:
                lines.append("")
        include_score = any(
            isinstance(entry, dict) and entry.get("score") is not None
            for entry in entries
        )
        header = (
            "| Collection ID | Plate | Subject ID | Published rank | University | "
            "City / locality |"
        )
        separator = "|---|---:|---|:---:|---|---|"
        if include_score:
            header = (
                "| Collection ID | Plate | Subject ID | Published rank | Score | "
                "University | City / locality |"
            )
            separator = "|---|---:|---|:---:|---:|---|---|"
        lines.extend((header, separator))
        for entry in entries:
            if not isinstance(entry, dict):
                raise SeriesBuildError("Ranked university entry is malformed.")
            subject_id = str(entry.get("subject_id", ""))
            subject = subject_by_id.get(subject_id)
            if subject is None:
                raise SeriesBuildError(
                    f"Ranked entry references unknown subject {subject_id!r}."
                )
            location = subject.get("location", {})
            city = location.get("city", "") if isinstance(location, dict) else ""
            values = [
                str(collection.get("id", "")),
                str(entry.get("position", "")),
                subject_id,
                str(entry.get("rank", "")),
            ]
            if include_score:
                values.append(str(entry.get("score", "")))
            values.extend((str(entry.get("ranking_name", subject["name"])), str(city)))
            lines.append("| " + " | ".join(values) + " |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _prepare_release(output: Path) -> dict[str, Path]:
    _assert_file(CATALOG, EXPECTED_CATALOG_SHA256)
    if output.is_symlink():
        raise SeriesBuildError(f"Release directory must not be a symlink: {output}")
    output.mkdir(parents=True, exist_ok=True)
    metadata = output / "release-metadata"
    metadata.mkdir(exist_ok=True)

    catalog_copy = metadata / CATALOG.name
    archive_copy = metadata / FROZEN_ARCHIVE.name
    recipe_copy = metadata / FROZEN_RENDER_RECIPE.name
    renderer_copy = metadata / "renderer-contract"
    style_copy = renderer_copy / FROZEN_STYLE.name
    source_manifest_copy = _copy_source_contract_exact(
        metadata / "source-snapshots"
    )

    _copy_exact(CATALOG, catalog_copy)
    _copy_exact(FROZEN_ARCHIVE, archive_copy)
    _copy_exact(FROZEN_RENDER_RECIPE, recipe_copy)
    _assert_file(archive_copy, EXPECTED_ARCHIVE_SHA256)
    _assert_file(PATCHED_CARTOGRAPHY, EXPECTED_PATCHED_CARTOGRAPHY_SHA256)
    _assert_file(PATCHED_BATCH, EXPECTED_PATCHED_BATCH_SHA256)
    _assert_file(PATCHED_CLI, EXPECTED_PATCHED_CLI_SHA256)
    _assert_file(PATCHED_COMPLETENESS, EXPECTED_PATCHED_COMPLETENESS_SHA256)
    _assert_file(PATCHED_SVG)
    patched_svg = _patched_svg_payload(
        FROZEN_RENDERER / "city_map_plotter/svg.py", PATCHED_SVG
    )
    if hashlib.sha256(patched_svg).hexdigest() != EXPECTED_PATCHED_SVG_SHA256:
        raise SeriesBuildError("Frozen SVG backport digest is not the declared pin.")
    if _tree_digest(FROZEN_RENDERER) != EXPECTED_BASE_RENDERER_TREE_SHA256:
        raise SeriesBuildError("Frozen base renderer tree changed.")
    base_fingerprint = _renderer_fingerprint(
        FROZEN_RENDERER,
        expected_sha256=EXPECTED_RENDERER_FINGERPRINT,
        label="Frozen base",
    )
    _copy_renderer_exact(FROZEN_RENDERER, renderer_copy)
    _assert_file(style_copy, EXPECTED_STYLE_SHA256)
    fingerprint = _renderer_fingerprint(
        renderer_copy,
        expected_sha256=EXPECTED_DERIVED_RENDERER_FINGERPRINT,
        label="Derived v2.1.4",
    )

    contract = {
        "schema_version": 1,
        "series_id": "university-memorabilia-ranked-2026-v2.1.4",
        "status": "review-only",
        "expected_subject_count": 50,
        "collections": [UK_COLLECTION, US_COLLECTION],
        "catalog": {
            "path": str(catalog_copy.relative_to(output)),
            "sha256": _sha256(catalog_copy),
        },
        "render_recipe": {
            "path": str(recipe_copy.relative_to(output)),
            "sha256": EXPECTED_RENDER_RECIPE_SHA256,
        },
        "renderer": {
            "path": str(renderer_copy.relative_to(output)),
            "tree_sha256": EXPECTED_DERIVED_RENDERER_TREE_SHA256,
            "archive": str(archive_copy.relative_to(output)),
            "archive_sha256": EXPECTED_ARCHIVE_SHA256,
            "base_tree_sha256": EXPECTED_BASE_RENDERER_TREE_SHA256,
            "fingerprint": fingerprint,
            "base_fingerprint": base_fingerprint,
            "derivation": {
                "id": "university-memorabilia-v2.1.4-pinned-source-correctness",
                "visual_policy": "v2.1 parameters and style are unchanged",
                "overrides": [
                    {
                        "path": "city_map_plotter/cartography.py",
                        "source_sha256": EXPECTED_PATCHED_CARTOGRAPHY_SHA256,
                        "scope": (
                            "Preserve crop-boundary slivers for the physical-minimum "
                            "ledger, retain projected landmark-area identity, and "
                            "suppress conceptual closed-bay edges only when exact "
                            "dotted-surface carriers prove representation."
                        ),
                    },
                    {
                        "path": "city_map_plotter/batch.py",
                        "source_sha256": EXPECTED_PATCHED_BATCH_SHA256,
                        "scope": (
                            "Bind published rank fields and the exact visible title "
                            "into the existing per-artifact contract digest, and keep "
                            "the advisory batch lock outside the release tree."
                        ),
                    },
                    {
                        "path": "city_map_plotter/cli.py",
                        "source_sha256": EXPECTED_PATCHED_CLI_SHA256,
                        "scope": (
                            "Accept the verified per-subject source manifest in "
                            "the frozen catalog runner, protect it from output "
                            "collisions, and skip public-service delay when every "
                            "item has an injected saved JSON input."
                        ),
                    },
                    {
                        "path": "city_map_plotter/completeness.py",
                        "source_sha256": EXPECTED_PATCHED_COMPLETENESS_SHA256,
                        "scope": (
                            "Retain and audit complete area-relation way rings while "
                            "recording impossible explicit node ring-members as "
                            "advisories, and accept only exact same-role relation-ring "
                            "deduplication with explicit reindex evidence."
                        ),
                    },
                    {
                        "path": "city_map_plotter/svg.py",
                        "source_sha256": EXPECTED_PATCHED_SVG_SHA256,
                        "scope": (
                            "Keep the canonical ten-slot physical inventory stable "
                            "when a plate has no geometry for one slot, omit empty "
                            "groups from the master, and emit an explicit zero-path "
                            "per-slot SVG without requesting a physical pen load."
                        ),
                    },
                ],
            },
        },
        "style": {
            "path": str(style_copy.relative_to(output)),
            "sha256": EXPECTED_STYLE_SHA256,
        },
        "source_contract": {
            "mode": "pinned-input-json-set",
            "path": str(source_manifest_copy.relative_to(output)),
            "manifest_sha256": EXPECTED_SOURCE_MANIFEST_SHA256,
            "cohort_sha256": EXPECTED_SOURCE_COHORT_SHA256,
            "subject_count": 50,
            "network_fallback": False,
            "production_eligible": False,
        },
        "output_contract": {
            "paper": "A5 portrait",
            "preset": "a5-balanced-poster",
            "poster_layout": "university-memorabilia",
            "families": ["roads", "water", "railways", "parks", "buildings"],
            "detail_profile": "plotter-faithful",
            "simplify_mm": 0.04,
            "road_style": "centreline",
            "extent_fit": "contain",
            "water_fill": "dots",
            "landmark_buildings": True,
            "radius_km": 2.0,
            "pen_profile": "actual-pens",
            "inventory_pen_slots": 10,
            "empty_pen_slot_policy": (
                "manifest-and-zero-path-split-without-empty-master-group"
            ),
            "split_by_pen": True,
            "optimise_travel": True,
            "physical_audit": True,
            "scale_bar": False,
            "scale_detail": False,
            "north_mark": True,
            "png_dpi": 254,
            "title_policy": "uppercase-city-or-campus-locality",
            "attribution_mode": "external",
            "external_attribution_placement": EXTERNAL_ATTRIBUTION,
        },
    }
    _atomic_json(output / "SERIES-CONTRACT.json", contract)
    _atomic_text(
        output / "ATTRIBUTION.md",
        "# Attribution and ranking sources\n\n"
        "Map data © OpenStreetMap contributors, available under ODbL 1.0: "
        "https://www.openstreetmap.org/copyright\n\n"
        "UK selection: The Times and The Sunday Times Good University Guide "
        "2026. Exact rank table provenance is recorded in the bundled ranked "
        "university catalog.\n\n"
        "US selection: QS World University Rankings 2027, filtered to the first "
        "20 United States institutions in official display order. Exact ranks, "
        "ties, scores, and source URLs are recorded in the catalog.\n\n"
        "University and venue names are descriptive and do not imply affiliation "
        "or endorsement. External attribution must accompany every public plate.\n",
    )
    _atomic_text(output / "RANKED-UNIVERSITIES.md", _ranking_catalog_markdown(catalog_copy))
    return {
        "catalog": catalog_copy,
        "renderer": renderer_copy,
        "style": style_copy,
        "source_manifest": source_manifest_copy,
    }


def _build_command(args: argparse.Namespace, dependencies: dict[str, Path]) -> list[str]:
    output = args.output_dir.resolve()
    cache_dir = output / "source-cache"
    report = output / "ranked-universities.batch.json"
    command = [
        sys.executable,
        "-m",
        "city_map_plotter",
        "catalog",
        "--catalog-file",
        str(dependencies["catalog"]),
        "export",
        "--collection",
        UK_COLLECTION,
        "--collection",
        US_COLLECTION,
        "--output-dir",
        str(output),
        "--report",
        str(report),
        "--source-manifest",
        str(dependencies["source_manifest"]),
        "--delay-seconds",
        f"{args.delay_seconds:g}",
        "--png",
        "--png-dpi",
        "254",
        "--title-mode",
        "city",
    ]
    if args.dry_run:
        command.append("--dry-run")
    if args.keep_going:
        command.append("--keep-going")
    if args.overwrite:
        command.append("--overwrite")
    if args.limit is not None:
        command.extend(("--limit", str(args.limit)))

    command.extend(
        (
            "--export-args",
            "--radius-km",
            "2",
            "--preset",
            "a5-balanced-poster",
            "--poster-layout",
            "university-memorabilia",
            "--layers",
            "roads,water,railways,parks,buildings",
            "--style",
            str(dependencies["style"]),
            "--water-fill",
            "dots",
            "--landmark-buildings",
            "--detail-profile",
            "plotter-faithful",
            "--simplify-mm",
            "0.04",
            "--road-style",
            "centreline",
            "--extent-fit",
            "contain",
            "--pen-profile",
            "actual-pens",
            "--no-scale-bar",
            "--no-scale-detail",
            "--optimise",
            "--physical-audit",
            "--split-by-pen",
            "--frame",
            "--attribution-mode",
            "external",
            "--external-attribution-placement",
            EXTERNAL_ATTRIBUTION,
            "--cache-dir",
            str(cache_dir),
            "--user-agent",
            args.user_agent,
            "--timeout",
            f"{args.timeout:g}",
        )
    )
    if args.overpass_url:
        command.extend(("--overpass-url", args.overpass_url))
    return command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build the exact 30-UK/20-US ranked university review series with "
            "the frozen university-memorabilia v2.1 visual contract and the "
            "v2.1.4 ranked-series correctness and pinned-source corrections."
        )
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--keep-going", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--user-agent",
        default="CityMapPlotter/0.2 ranked university memorabilia review build",
    )
    parser.add_argument("--overpass-url")
    parser.add_argument("--print-command", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _assert_file(CATALOG, EXPECTED_CATALOG_SHA256)
        _assert_file(FROZEN_ARCHIVE, EXPECTED_ARCHIVE_SHA256)
        _assert_file(FROZEN_STYLE, EXPECTED_STYLE_SHA256)
        if args.limit is not None and not 1 <= args.limit <= 50:
            raise SeriesBuildError("--limit must be between 1 and 50.")
        if args.delay_seconds < 0:
            raise SeriesBuildError("--delay-seconds must not be negative.")
        if args.timeout <= 0:
            raise SeriesBuildError("--timeout must be greater than zero.")
        if not args.user_agent.strip():
            raise SeriesBuildError("--user-agent must not be empty.")

        args.output_dir = args.output_dir.expanduser().resolve()
        dependencies = _prepare_release(args.output_dir)
        command = _build_command(args, dependencies)
        if args.print_command:
            print(shlex.join(command))
            return 0

        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(dependencies["renderer"])
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(command, cwd=ROOT, env=environment, check=False)
        return result.returncode
    except (OSError, SeriesBuildError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

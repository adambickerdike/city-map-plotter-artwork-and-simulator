"""Semantic fingerprint for code that can change plotted themed artwork."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import platform
import textwrap
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from types import CodeType
from typing import Any


VISUAL_RENDER_CONTRACT_SCHEMA_VERSION = 1
VISUAL_RENDER_POLICY_ID = "city-map-visual-renderer-semantic-ast-v1"
VISUAL_RENDER_MODULES = (
    "cartography",
    "cli",
    "completeness",
    "features",
    "furniture",
    "geometry",
    "ink_budget",
    "models",
    "osm",
    "pbf",
    "pens",
    "physical",
    "plotopt",
    "quality",
    "stroke_font",
    "styles",
    "svg",
    "svgkit",
    "textweight",
    "themes",
    "topology",
)


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _without_docstrings(tree: ast.AST) -> ast.AST:
    """Remove prose-only constants before semantic AST hashing."""

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]
    return tree


@lru_cache(maxsize=4096)
def _semantic_source(source: str) -> tuple[str, tuple[str, ...]]:
    """Parse and canonicalise one source text.

    Memoised on the text itself, so it stays a pure function: identical source
    can only produce an identical fingerprint, and edited or monkeypatched
    source misses the cache by construction. This matters because a long-lived
    process -- the theme studio, a catalog batch -- otherwise re-parses every
    module and every function on every single export, which measured at 4.4 s
    of a 30 s render.
    """

    tree = _without_docstrings(ast.parse(source))
    functions = tuple(
        node.name
        for node in tree.body  # type: ignore[attr-defined]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    return ast.dump(tree, annotate_fields=True, include_attributes=False), functions


def _stable_runtime_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return format(value, ".17g")
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, (tuple, list)):
        return [_stable_runtime_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _stable_runtime_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_stable_runtime_value(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    if isinstance(value, CodeType):
        return _code_fingerprint(value)
    if inspect.isfunction(value):
        return _runtime_function_payload(value)
    return {
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
    }


def _code_fingerprint(code: CodeType) -> dict[str, Any]:
    """Fallback for dynamically defined functions whose source is unavailable."""

    return {
        "bytecode": code.co_code.hex(),
        "constants": [_stable_runtime_value(value) for value in code.co_consts],
        "names": list(code.co_names),
        "variables": list(code.co_varnames),
        "free_variables": list(code.co_freevars),
        "cell_variables": list(code.co_cellvars),
        "argument_counts": [
            code.co_argcount,
            code.co_posonlyargcount,
            code.co_kwonlyargcount,
        ],
        "flags": code.co_flags,
    }


def _runtime_function_payload(value: Any) -> dict[str, Any]:
    unwrapped = inspect.unwrap(value)
    try:
        semantic_source, _ = _semantic_source(
            textwrap.dedent(inspect.getsource(unwrapped))
        )
        implementation: Any = {"semantic_source": semantic_source}
    except (OSError, TypeError, IndentationError, SyntaxError):
        code = getattr(unwrapped, "__code__", None)
        implementation = (
            {"code": _code_fingerprint(code)}
            if isinstance(code, CodeType)
            else {"callable_type": type(unwrapped).__qualname__}
        )
    closure = getattr(unwrapped, "__closure__", None) or ()
    return {
        "implementation": implementation,
        "defaults": _stable_runtime_value(getattr(unwrapped, "__defaults__", None)),
        "keyword_defaults": _stable_runtime_value(
            getattr(unwrapped, "__kwdefaults__", None)
        ),
        "closure": [_stable_runtime_value(cell.cell_contents) for cell in closure],
    }


def _distribution_version(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def _runtime_dependencies() -> dict[str, Any]:
    """Record runtimes whose numeric or PBF behavior can affect an edition."""

    import shapely

    return {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "shapely": {
            "version": _distribution_version("shapely"),
            "geos_version": getattr(shapely, "geos_version_string", None),
        },
        "osmium": {
            "version": _distribution_version("osmium"),
        },
    }


def visual_renderer_contract() -> dict[str, Any]:
    """Bind direct exports to installed visual algorithms, including monkeypatches."""

    module_records: list[dict[str, Any]] = []
    package = files("city_map_plotter")
    for short_name in VISUAL_RENDER_MODULES:
        source = package.joinpath(f"{short_name}.py").read_text(encoding="utf-8")
        semantic_source, function_names = _semantic_source(source)
        module = importlib.import_module(f"city_map_plotter.{short_name}")
        runtime_bindings = {
            name: _runtime_function_payload(getattr(module, name))
            for name in function_names
            if callable(getattr(module, name, None))
        }
        module_records.append(
            {
                "module": short_name,
                "semantic_ast_sha256": hashlib.sha256(
                    semantic_source.encode("utf-8")
                ).hexdigest(),
                "runtime_bindings_sha256": _stable_digest(runtime_bindings),
            }
        )
    invariant = {
        "schema_version": VISUAL_RENDER_CONTRACT_SCHEMA_VERSION,
        "policy_id": VISUAL_RENDER_POLICY_ID,
        "runtime_dependencies": _runtime_dependencies(),
        "modules": module_records,
    }
    return {**invariant, "sha256": _stable_digest(invariant)}

"""Dated, validated Great Britain passenger-operator product registry.

The registry keys map products, not owning companies.  Customer brands remain
stable when a concession changes hands, while current and legacy source tokens
stay explicitly separated.  Geometry compilers may match only
``current_osm_tokens``; legacy tokens are audit exclusions, never aliases.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import resources
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from .models import MapPlotterError


REGISTRY_RESOURCE = "data/gb-passenger-operators-2026-08-08.json"
REGISTRY_REPOSITORY_PATH = f"src/city_map_plotter/{REGISTRY_RESOURCE}"
_STABLE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_ATOC_CODE_RE = re.compile(r"[A-Z0-9]{2}\Z")
_HEX_RE = re.compile(r"#[0-9A-F]{6}\Z")
_TOKEN_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def normalize_operator_token(value: str) -> str:
    """Normalize one explicit operator tag without interpreting route names."""

    return _TOKEN_CLEAN_RE.sub(" ", value.casefold()).strip()


@dataclass(frozen=True, slots=True)
class OperatorPresentation:
    slug: str
    display_hex: str
    colour_status: str
    ink: str
    pen_id: str
    nib_mm: float


@dataclass(frozen=True, slots=True)
class OperatorProduct:
    id: str
    name: str
    atoc_codes: tuple[str, ...]
    legacy_ingestion_codes: tuple[str, ...]
    current_osm_tokens: frozenset[str]
    legacy_osm_tokens: frozenset[str]
    reviewed_relation_allowlist: tuple[int, ...]
    brand_line_groups: tuple[str, ...]
    official_reference_url: str
    claim_scope: str
    format_id: str
    regional_padding_fraction: float
    presentation: OperatorPresentation

    @property
    def operator_key(self) -> str:
        """Return the compact internal evidence key used by frozen audits.

        Multi-brand products deliberately keep a stable ingestion key: GTR is
        keyed by ``SN`` and West Midlands Trains by the legacy feed key ``LM``.
        The customer-facing codes remain the full ``atoc_codes`` tuple.
        """

        return (
            self.legacy_ingestion_codes[0]
            if self.legacy_ingestion_codes
            else self.atoc_codes[0]
        )


@dataclass(frozen=True, slots=True)
class OperatorRegistry:
    schema_version: int
    snapshot: str
    scope: str
    products: tuple[OperatorProduct, ...]
    by_id: Mapping[str, OperatorProduct]
    by_key: Mapping[str, OperatorProduct]
    selector_to_key: Mapping[str, str]
    current_token_to_key: Mapping[str, str]
    legacy_token_to_keys: Mapping[str, tuple[str, ...]]

    def resolve(self, selector: str) -> OperatorProduct:
        normalized = selector.strip().casefold()
        key = self.selector_to_key.get(normalized)
        if key is None:
            raise MapPlotterError(f"Unknown passenger-operator selector {selector!r}.")
        return self.by_key[key]


def _required_string(value: Mapping[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise MapPlotterError(f"Operator registry field {key!r} must be text.")
    return raw.strip()


def _string_tuple(value: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw = value.get(key, [])
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        raise MapPlotterError(f"Operator registry field {key!r} must be a text list.")
    return tuple(item.strip() for item in raw)


def _parse_product(raw: Mapping[str, Any]) -> OperatorProduct:
    product_id = _required_string(raw, "id")
    if _STABLE_ID_RE.fullmatch(product_id) is None:
        raise MapPlotterError(f"Invalid operator product ID {product_id!r}.")
    codes = _string_tuple(raw, "atoc_codes")
    if not codes or any(_ATOC_CODE_RE.fullmatch(code) is None for code in codes):
        raise MapPlotterError(f"Operator {product_id} has invalid ATOC codes.")
    legacy_codes = _string_tuple(raw, "legacy_ingestion_codes")
    if any(_ATOC_CODE_RE.fullmatch(code) is None for code in legacy_codes):
        raise MapPlotterError(f"Operator {product_id} has invalid legacy feed codes.")
    current_tokens_raw = _string_tuple(raw, "current_osm_tokens")
    current_tokens = frozenset(normalize_operator_token(item) for item in current_tokens_raw)
    legacy_tokens = frozenset(
        normalize_operator_token(item) for item in _string_tuple(raw, "legacy_osm_tokens")
    )
    if not current_tokens or "" in current_tokens or "" in legacy_tokens:
        raise MapPlotterError(f"Operator {product_id} has an empty OSM token.")
    overlap = current_tokens.intersection(legacy_tokens)
    if overlap:
        raise MapPlotterError(
            f"Operator {product_id} marks tokens current and legacy: {sorted(overlap)}."
        )
    allowlist_raw = raw.get("reviewed_relation_allowlist", [])
    if not isinstance(allowlist_raw, list) or any(
        not isinstance(item, int) or item <= 0 for item in allowlist_raw
    ):
        raise MapPlotterError(
            f"Operator {product_id} relation allowlist must contain positive IDs."
        )
    presentation_raw = raw.get("presentation")
    if not isinstance(presentation_raw, Mapping):
        raise MapPlotterError(f"Operator {product_id} lacks presentation metadata.")
    display_hex = _required_string(presentation_raw, "hex").upper()
    if _HEX_RE.fullmatch(display_hex) is None:
        raise MapPlotterError(f"Operator {product_id} has invalid display hex.")
    nib_raw = presentation_raw.get("nib_mm")
    if not isinstance(nib_raw, (int, float)) or float(nib_raw) <= 0.0:
        raise MapPlotterError(f"Operator {product_id} has invalid nib width.")
    padding_raw = raw.get("regional_padding_fraction")
    if not isinstance(padding_raw, (int, float)) or not 0.0 <= float(padding_raw) <= 0.25:
        raise MapPlotterError(f"Operator {product_id} has invalid context padding.")
    return OperatorProduct(
        id=product_id,
        name=_required_string(raw, "name"),
        atoc_codes=codes,
        legacy_ingestion_codes=legacy_codes,
        current_osm_tokens=current_tokens,
        legacy_osm_tokens=legacy_tokens,
        reviewed_relation_allowlist=tuple(sorted(set(allowlist_raw))),
        brand_line_groups=_string_tuple(raw, "brand_line_groups"),
        official_reference_url=_required_string(raw, "official_reference_url"),
        claim_scope=_required_string(raw, "claim_scope"),
        format_id=_required_string(raw, "format_id"),
        regional_padding_fraction=float(padding_raw),
        presentation=OperatorPresentation(
            slug=_required_string(presentation_raw, "slug"),
            display_hex=display_hex,
            colour_status=_required_string(presentation_raw, "colour_status"),
            ink=_required_string(presentation_raw, "ink"),
            pen_id=_required_string(presentation_raw, "pen_id"),
            nib_mm=float(nib_raw),
        ),
    )


def load_operator_registry() -> OperatorRegistry:
    try:
        payload = resources.files("city_map_plotter").joinpath(REGISTRY_RESOURCE).read_text(
            encoding="utf-8"
        )
        document = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise MapPlotterError(f"Cannot load passenger-operator registry: {exc}") from exc
    if not isinstance(document, Mapping):
        raise MapPlotterError("Passenger-operator registry must be a JSON object.")
    raw_products = document.get("products")
    if not isinstance(raw_products, list) or not raw_products:
        raise MapPlotterError("Passenger-operator registry has no products.")
    products = tuple(
        _parse_product(raw)
        for raw in raw_products
        if isinstance(raw, Mapping)
    )
    if len(products) != len(raw_products):
        raise MapPlotterError("Passenger-operator registry product must be an object.")
    by_id: dict[str, OperatorProduct] = {}
    by_key: dict[str, OperatorProduct] = {}
    selectors: dict[str, str] = {}
    current_tokens: dict[str, str] = {}
    legacy_tokens: dict[str, set[str]] = {}
    for product in products:
        if product.id in by_id:
            raise MapPlotterError(f"Duplicate operator product ID {product.id!r}.")
        key = product.operator_key
        if key in by_key:
            raise MapPlotterError(f"Duplicate operator evidence key {key!r}.")
        by_id[product.id] = product
        by_key[key] = product
        for selector in (
            product.id,
            product.name,
            key,
            *product.atoc_codes,
            *product.legacy_ingestion_codes,
        ):
            normalized = selector.strip().casefold()
            previous = selectors.get(normalized)
            if previous is not None and previous != key:
                raise MapPlotterError(
                    f"Operator selector {selector!r} is ambiguous between {previous} and {key}."
                )
            selectors[normalized] = key
        for token in product.current_osm_tokens:
            previous = current_tokens.get(token)
            if previous is not None and previous != key:
                raise MapPlotterError(
                    f"Current OSM operator token {token!r} is ambiguous."
                )
            current_tokens[token] = key
        for token in product.legacy_osm_tokens:
            legacy_tokens.setdefault(token, set()).add(key)
    current_legacy_overlap = set(current_tokens).intersection(legacy_tokens)
    if current_legacy_overlap:
        raise MapPlotterError(
            "OSM tokens are current for one product and legacy for another: "
            f"{sorted(current_legacy_overlap)}."
        )
    return OperatorRegistry(
        schema_version=int(document.get("schema_version", 0)),
        snapshot=_required_string(document, "snapshot"),
        scope=_required_string(document, "scope"),
        products=products,
        by_id=MappingProxyType(by_id),
        by_key=MappingProxyType(by_key),
        selector_to_key=MappingProxyType(selectors),
        current_token_to_key=MappingProxyType(current_tokens),
        legacy_token_to_keys=MappingProxyType(
            {key: tuple(sorted(value)) for key, value in legacy_tokens.items()}
        ),
    )


OPERATOR_REGISTRY = load_operator_registry()


def operator_registry_binding() -> dict[str, object]:
    """Return the portable byte binding for the dated product registry."""

    resource = resources.files("city_map_plotter").joinpath(REGISTRY_RESOURCE)
    try:
        digest = hashlib.sha256(resource.read_bytes()).hexdigest()
    except OSError as exc:  # pragma: no cover - packaged resource invariant.
        raise MapPlotterError(
            f"Cannot hash passenger-operator registry {REGISTRY_RESOURCE}: {exc}"
        ) from exc
    return {
        "path": REGISTRY_REPOSITORY_PATH,
        "snapshot": OPERATOR_REGISTRY.snapshot,
        "sha256": digest,
        "schema_version": OPERATOR_REGISTRY.schema_version,
        "product_count": len(OPERATOR_REGISTRY.products),
    }


DEFAULT_OPERATOR_KEYS = frozenset(OPERATOR_REGISTRY.by_key)
OPERATOR_PRESENTATION: Mapping[str, tuple[str, str, str]] = MappingProxyType(
    {
        key: (
            product.presentation.slug,
            product.name,
            product.presentation.display_hex,
        )
        for key, product in OPERATOR_REGISTRY.by_key.items()
    }
)
OPERATOR_PENS: Mapping[str, tuple[str, str, float]] = MappingProxyType(
    {
        key: (
            product.presentation.ink,
            product.presentation.pen_id,
            product.presentation.nib_mm,
        )
        for key, product in OPERATOR_REGISTRY.by_key.items()
    }
)


__all__ = [
    "DEFAULT_OPERATOR_KEYS",
    "OPERATOR_PENS",
    "OPERATOR_PRESENTATION",
    "OPERATOR_REGISTRY",
    "REGISTRY_REPOSITORY_PATH",
    "OperatorPresentation",
    "OperatorProduct",
    "OperatorRegistry",
    "load_operator_registry",
    "normalize_operator_token",
    "operator_registry_binding",
]

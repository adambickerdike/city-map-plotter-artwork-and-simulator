from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from math import isfinite
from pathlib import Path
from typing import Any, Iterable

from .models import MapPlotterError


SCHEMA_VERSION = 1
BUILTIN_CATALOG = "data/catalog-v1.json"
SUBJECT_KINDS = frozenset({"university", "student_city", "marathon"})
_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class CatalogSubject:
    id: str
    kind: str
    name: str
    city: str
    region: str | None
    country: str
    country_code: str
    latitude: float
    longitude: float
    query: str
    preview_radius_km: float
    map_purpose: str
    details: dict[str, Any] = field(default_factory=dict)
    source_urls: tuple[str, ...] = ()

    @property
    def location_label(self) -> str:
        parts = [self.city]
        if self.region:
            parts.append(self.region)
        parts.append(self.country)
        return ", ".join(parts)

    @property
    def is_city_preview_only(self) -> bool:
        return self.map_purpose == "city_preview"


@dataclass(frozen=True)
class CatalogEntry:
    subject_id: str
    position: int
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogCollection:
    id: str
    title: str
    kind: str
    scope: str
    as_of: str
    methodology: str
    source_urls: tuple[str, ...]
    entries: tuple[CatalogEntry, ...]
    audit: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Catalog:
    version: str
    as_of: str
    subjects: tuple[CatalogSubject, ...]
    collections: tuple[CatalogCollection, ...]
    _subject_index: dict[str, CatalogSubject] = field(repr=False)
    _collection_index: dict[str, CatalogCollection] = field(repr=False)

    def subject(self, subject_id: str) -> CatalogSubject:
        try:
            return self._subject_index[subject_id]
        except KeyError as exc:
            choices = ", ".join(sorted(self._subject_index)[:8])
            suffix = f" Examples: {choices}." if choices else ""
            raise MapPlotterError(
                f"Unknown catalog subject {subject_id!r}.{suffix} "
                "Use 'mapplot catalog list' to see valid IDs."
            ) from exc

    def collection(self, collection_id: str) -> CatalogCollection:
        try:
            return self._collection_index[collection_id]
        except KeyError as exc:
            choices = ", ".join(sorted(self._collection_index))
            raise MapPlotterError(
                f"Unknown catalog collection {collection_id!r}. "
                f"Available collections: {choices}."
            ) from exc

    def memberships(self, subject_id: str) -> tuple[tuple[str, CatalogEntry], ...]:
        return tuple(
            (collection.id, entry)
            for collection in self.collections
            for entry in collection.entries
            if entry.subject_id == subject_id
        )


def _error(source: str, path: str, message: str) -> MapPlotterError:
    return MapPlotterError(f"Invalid catalog {source} at {path}: {message}")


def _object(value: Any, source: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(source, path, "expected an object")
    return value


def _list(value: Any, source: str, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _error(source, path, "expected an array")
    return value


def _string(value: Any, source: str, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(source, path, "expected a non-empty string")
    return value.strip()


def _identifier(value: Any, source: str, path: str) -> str:
    identifier = _string(value, source, path)
    if not _ID_PATTERN.fullmatch(identifier):
        raise _error(
            source,
            path,
            "IDs must contain lowercase letters, digits, and single hyphens",
        )
    return identifier


def _urls(value: Any, source: str, path: str) -> tuple[str, ...]:
    urls = tuple(
        _string(item, source, f"{path}[{index}]")
        for index, item in enumerate(_list(value, source, path))
    )
    if not urls:
        raise _error(source, path, "at least one provenance URL is required")
    for index, url in enumerate(urls):
        if not url.startswith("https://"):
            raise _error(source, f"{path}[{index}]", "URL must use HTTPS")
    return urls


def _number(value: Any, source: str, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(source, path, "expected a number")
    result = float(value)
    if not isfinite(result):
        raise _error(source, path, "number must be finite")
    return result


def _parse_subject(value: Any, source: str, index: int) -> CatalogSubject:
    path = f"subjects[{index}]"
    subject = _object(value, source, path)
    subject_id = _identifier(subject.get("id"), source, f"{path}.id")
    kind = _string(subject.get("kind"), source, f"{path}.kind")
    if kind not in SUBJECT_KINDS:
        raise _error(
            source,
            f"{path}.kind",
            f"expected one of {', '.join(sorted(SUBJECT_KINDS))}",
        )

    location = _object(subject.get("location"), source, f"{path}.location")
    region_value = location.get("region")
    if region_value is not None:
        region_value = _string(region_value, source, f"{path}.location.region")
    country_code = _string(
        location.get("country_code"), source, f"{path}.location.country_code"
    )
    if not re.fullmatch(r"[A-Z]{2}", country_code):
        raise _error(
            source, f"{path}.location.country_code", "expected ISO alpha-2 code"
        )

    mapping = _object(subject.get("map"), source, f"{path}.map")
    center = _list(mapping.get("center"), source, f"{path}.map.center")
    if len(center) != 2:
        raise _error(source, f"{path}.map.center", "expected [latitude, longitude]")
    latitude = _number(center[0], source, f"{path}.map.center[0]")
    longitude = _number(center[1], source, f"{path}.map.center[1]")
    if not -85 <= latitude <= 85:
        raise _error(source, f"{path}.map.center[0]", "latitude is out of range")
    if not -180 <= longitude <= 180:
        raise _error(source, f"{path}.map.center[1]", "longitude is out of range")
    radius = _number(
        mapping.get("preview_radius_km"),
        source,
        f"{path}.map.preview_radius_km",
    )
    if not 0 < radius <= 50:
        raise _error(
            source,
            f"{path}.map.preview_radius_km",
            "expected a value greater than 0 and no more than 50",
        )
    map_purpose = _string(
        mapping.get("purpose", "map_subject"), source, f"{path}.map.purpose"
    )
    if map_purpose not in {"campus", "student_city", "city_preview"}:
        raise _error(
            source,
            f"{path}.map.purpose",
            "expected campus, student_city, or city_preview",
        )

    details_value = subject.get("details", {})
    details = _object(details_value, source, f"{path}.details")
    return CatalogSubject(
        id=subject_id,
        kind=kind,
        name=_string(subject.get("name"), source, f"{path}.name"),
        city=_string(location.get("city"), source, f"{path}.location.city"),
        region=region_value,
        country=_string(location.get("country"), source, f"{path}.location.country"),
        country_code=country_code,
        latitude=latitude,
        longitude=longitude,
        query=_string(mapping.get("query"), source, f"{path}.map.query"),
        preview_radius_km=radius,
        map_purpose=map_purpose,
        details=dict(details),
        source_urls=_urls(subject.get("source_urls"), source, f"{path}.source_urls"),
    )


def _parse_collection(value: Any, source: str, index: int) -> CatalogCollection:
    path = f"collections[{index}]"
    collection = _object(value, source, path)
    kind = _string(collection.get("kind"), source, f"{path}.kind")
    if kind not in SUBJECT_KINDS:
        raise _error(
            source,
            f"{path}.kind",
            f"expected one of {', '.join(sorted(SUBJECT_KINDS))}",
        )
    raw_entries = _list(collection.get("entries"), source, f"{path}.entries")
    if not raw_entries:
        raise _error(source, f"{path}.entries", "collection cannot be empty")
    entries: list[CatalogEntry] = []
    for entry_index, raw_entry in enumerate(raw_entries):
        entry_path = f"{path}.entries[{entry_index}]"
        entry = _object(raw_entry, source, entry_path)
        position = entry.get("position")
        if isinstance(position, bool) or not isinstance(position, int) or position < 1:
            raise _error(
                source, f"{entry_path}.position", "expected a positive integer"
            )
        attributes = {
            key: item
            for key, item in entry.items()
            if key not in {"subject_id", "position"}
        }
        entries.append(
            CatalogEntry(
                subject_id=_identifier(
                    entry.get("subject_id"), source, f"{entry_path}.subject_id"
                ),
                position=position,
                attributes=attributes,
            )
        )
    positions = [entry.position for entry in entries]
    if positions != list(range(1, len(entries) + 1)):
        raise _error(
            source,
            f"{path}.entries",
            "positions must be unique, ordered, and contiguous from 1",
        )
    subject_ids = [entry.subject_id for entry in entries]
    if len(subject_ids) != len(set(subject_ids)):
        raise _error(source, f"{path}.entries", "subject IDs must be unique")
    return CatalogCollection(
        id=_identifier(collection.get("id"), source, f"{path}.id"),
        title=_string(collection.get("title"), source, f"{path}.title"),
        kind=kind,
        scope=_string(collection.get("scope"), source, f"{path}.scope"),
        as_of=_string(collection.get("as_of"), source, f"{path}.as_of"),
        methodology=_string(
            collection.get("methodology"), source, f"{path}.methodology"
        ),
        source_urls=_urls(collection.get("source_urls"), source, f"{path}.source_urls"),
        entries=tuple(entries),
        audit=dict(_object(collection.get("audit", {}), source, f"{path}.audit")),
    )


def _read_catalog(path: Path | None) -> tuple[str, str]:
    if path is None:
        source = f"bundled {BUILTIN_CATALOG}"
        try:
            payload = (
                resources.files("city_map_plotter")
                .joinpath(BUILTIN_CATALOG)
                .read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            raise MapPlotterError(f"Could not read {source}: {exc}") from exc
        return source, payload
    source = str(path)
    try:
        return source, path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MapPlotterError(f"Could not read catalog {source}: {exc}") from exc


def load_catalog(path: Path | None = None) -> Catalog:
    source, payload = _read_catalog(path)
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Could not parse catalog JSON from {source}: {exc}"
        ) from exc
    root = _object(raw, source, "$")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise _error(
            source,
            "schema_version",
            f"expected {SCHEMA_VERSION}, got {root.get('schema_version')!r}",
        )
    subjects = tuple(
        _parse_subject(value, source, index)
        for index, value in enumerate(_list(root.get("subjects"), source, "subjects"))
    )
    collections = tuple(
        _parse_collection(value, source, index)
        for index, value in enumerate(
            _list(root.get("collections"), source, "collections")
        )
    )
    if not subjects:
        raise _error(source, "subjects", "catalog cannot be empty")
    if not collections:
        raise _error(source, "collections", "catalog cannot be empty")

    subject_index = {subject.id: subject for subject in subjects}
    collection_index = {collection.id: collection for collection in collections}
    if len(subject_index) != len(subjects):
        raise _error(source, "subjects", "subject IDs must be globally unique")
    if len(collection_index) != len(collections):
        raise _error(source, "collections", "collection IDs must be globally unique")

    referenced: set[str] = set()
    for collection in collections:
        for entry in collection.entries:
            subject = subject_index.get(entry.subject_id)
            if subject is None:
                raise _error(
                    source,
                    f"collections.{collection.id}",
                    f"references unknown subject {entry.subject_id!r}",
                )
            if subject.kind != collection.kind:
                raise _error(
                    source,
                    f"collections.{collection.id}",
                    f"subject {subject.id!r} has kind {subject.kind!r}, not {collection.kind!r}",
                )
            referenced.add(subject.id)
    orphaned = sorted(set(subject_index) - referenced)
    if orphaned:
        raise _error(
            source,
            "subjects",
            f"subjects are not assigned to a collection: {', '.join(orphaned)}",
        )

    return Catalog(
        version=_string(root.get("catalog_version"), source, "catalog_version"),
        as_of=_string(root.get("as_of"), source, "as_of"),
        subjects=subjects,
        collections=collections,
        _subject_index=subject_index,
        _collection_index=collection_index,
    )


def subject_record(catalog: Catalog, subject: CatalogSubject) -> dict[str, Any]:
    memberships = []
    for collection_id, entry in catalog.memberships(subject.id):
        memberships.append(
            {
                "collection_id": collection_id,
                "position": entry.position,
                **entry.attributes,
            }
        )
    return {
        "id": subject.id,
        "kind": subject.kind,
        "name": subject.name,
        "location": {
            "city": subject.city,
            **({"region": subject.region} if subject.region else {}),
            "country": subject.country,
            "country_code": subject.country_code,
        },
        "map": {
            "center": [subject.latitude, subject.longitude],
            "query": subject.query,
            "preview_radius_km": subject.preview_radius_km,
            "purpose": subject.map_purpose,
        },
        "details": subject.details,
        "source_urls": list(subject.source_urls),
        "memberships": memberships,
    }


def select_subjects(
    catalog: Catalog,
    *,
    collection_id: str | None = None,
    kind: str | None = None,
    country_code: str | None = None,
) -> list[tuple[CatalogSubject, CatalogEntry | None]]:
    if kind is not None and kind not in SUBJECT_KINDS:
        raise MapPlotterError(
            f"Unknown subject kind {kind!r}; expected {', '.join(sorted(SUBJECT_KINDS))}."
        )
    normalized_country = country_code.upper() if country_code else None
    if normalized_country is not None and not re.fullmatch(
        r"[A-Z]{2}", normalized_country
    ):
        raise MapPlotterError("--country must be a two-letter ISO country code.")

    if collection_id:
        collection = catalog.collection(collection_id)
        candidates: Iterable[tuple[CatalogSubject, CatalogEntry | None]] = (
            (catalog.subject(entry.subject_id), entry) for entry in collection.entries
        )
    else:
        candidates = (
            (subject, None)
            for subject in sorted(
                catalog.subjects,
                key=lambda item: (item.kind, item.name.casefold(), item.id),
            )
        )
    return [
        (subject, entry)
        for subject, entry in candidates
        if (kind is None or subject.kind == kind)
        and (normalized_country is None or subject.country_code == normalized_country)
    ]

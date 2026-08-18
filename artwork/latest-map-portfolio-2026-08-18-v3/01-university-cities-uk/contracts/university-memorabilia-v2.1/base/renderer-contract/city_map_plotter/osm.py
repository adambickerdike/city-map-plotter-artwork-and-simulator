from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .features import SUPPORTED_LINEAR_WATERWAYS
from .models import AcquisitionResult, BoundingBox, MapPlotterError


DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

_TRANSIENT_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_RETRYABLE_OVERPASS_REMARKS = (
    "timed out",
    "timeout",
    "dispatcher",
    "server is probably too busy",
    "rate limit",
)
_MAX_RESPONSE_BYTES = 128 * 1024 * 1024
_LANDMARK_REF_RE = re.compile(r"^(way|relation)/([1-9][0-9]*)$")


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (
        Path(base) / "city-map-plotter"
        if base
        else Path.home() / ".cache" / "city-map-plotter"
    )


def user_agent_from(value: str | None) -> str:
    user_agent = value or os.environ.get("CITY_MAP_PLOTTER_USER_AGENT") or ""
    if len(user_agent.strip()) < 8:
        raise MapPlotterError(
            "Live OpenStreetMap services require an identifying User-Agent. Set "
            "CITY_MAP_PLOTTER_USER_AGENT, for example: "
            "'CityMapPlotter/0.1 (contact: you@example.com)'."
        )
    return user_agent.strip()


def _value_regex(values: frozenset[str]) -> str:
    return "^(" + "|".join(re.escape(value) for value in sorted(values)) + ")$"


def canonical_landmark_refs(values: Iterable[str]) -> tuple[str, ...]:
    """Validate and deterministically order exact OSM way/relation references."""

    parsed: set[tuple[str, int]] = set()
    for value in values:
        match = _LANDMARK_REF_RE.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise MapPlotterError(
                "Landmark references must use exact lowercase way/<positive-id> or "
                "relation/<positive-id> syntax without whitespace or leading zeroes: "
                f"{value!r}."
            )
        object_type, object_id = match.groups()
        parsed.add((object_type, int(object_id)))

    type_order = {"way": 0, "relation": 1}
    return tuple(
        f"{object_type}/{object_id}"
        for object_type, object_id in sorted(
            parsed,
            key=lambda item: (type_order[item[0]], item[1]),
        )
    )


def build_overpass_query(
    bbox: BoundingBox,
    families: tuple[str, ...],
    timeout_s: int = 90,
    *,
    landmark_buildings_only: bool = False,
    landmark_refs: tuple[str, ...] = (),
) -> str:
    canonical_refs = canonical_landmark_refs(landmark_refs)
    south, west, north, east = bbox.south, bbox.west, bbox.north, bbox.east
    box = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    statements: list[str] = []
    if "roads" in families:
        # Fetch every tagged highway way and apply the canonical semantic
        # classifier locally.  A query-side value allow-list used to omit newly
        # documented road/path values before they could be audited, and it also
        # made cached Overpass input less complete than local PBF input.
        # Overpass's way bbox filter includes a way whose segment crosses the
        # box even when no constituent node lies inside it. Keep the output
        # statement unbounded (``out geom``, not ``out geom(bbox)``) so the
        # selected ways arrive with their complete node geometry before local
        # clipping.
        statements.append(f'way["highway"]({box});')
        # ``area:highway`` is non-routable street-surface micromapping. Fetch
        # its closed ways/relations separately; the renderer keeps the boundary
        # independent from the routable highway centreline graph.
        statements.extend(
            (
                f'way["area:highway"]({box});',
                f'relation["area:highway"]({box});',
            )
        )
    if "water" in families:
        statements.extend(
            (
                f'way["waterway"~"{_value_regex(SUPPORTED_LINEAR_WATERWAYS | {"riverbank"})}"]({box});',
                f'way["natural"="water"]({box});',
                f'relation["natural"="water"]({box});',
                f'way["natural"="bay"]({box});',
                f'relation["natural"="bay"]({box});',
                f'way["natural"="coastline"]({box});',
                f'way["landuse"="reservoir"]({box});',
                f'relation["landuse"="reservoir"]({box});',
                f'way["landuse"="basin"]({box});',
                f'relation["landuse"="basin"]({box});',
                f'way["natural"="wetland"]({box});',
                f'relation["natural"="wetland"]({box});',
                f'way["man_made"~"^(breakwater|pier)$"]({box});',
                f'relation["waterway"="riverbank"]({box});',
            )
        )
    if "railways" in families:
        # Fetch the complete railway key-space, then classify locally. Query-side
        # allow-lists hide newly documented values before the audit can see them.
        # Legacy ``disused:railway=*`` tagging is included because those tracks
        # remain physically present even though regular traffic has ceased.
        statements.extend(
            (
                f'way["railway"]({box});',
                f'way["disused:railway"]({box});',
            )
        )
    if "parks" in families:
        statements.extend(
            (
                f'way["leisure"~"^(nature_reserve|park)$"]({box});',
                f'relation["leisure"~"^(nature_reserve|park)$"]({box});',
                f'way["landuse"~"^(forest|grass|recreation_ground|cemetery|meadow)$"]({box});',
                f'relation["landuse"~"^(forest|grass|recreation_ground|cemetery|meadow)$"]({box});',
                f'way["landuse"~"^(allotments|orchard|village_green)$"]({box});',
                f'relation["landuse"~"^(allotments|orchard|village_green)$"]({box});',
                f'way["natural"~"^(heath|scrub|wood)$"]({box});',
                f'relation["natural"~"^(heath|scrub|wood)$"]({box});',
                f'way["leisure"="golf_course"]({box});',
                f'relation["leisure"="golf_course"]({box});',
            )
        )
    if "buildings" in families:
        if landmark_buildings_only:
            # This is an exact query-side superset of the semantic keys used by
            # ``_landmark_building_role``.  It avoids downloading every house
            # and shed across metropolitan extents while leaving identity,
            # lifecycle, projected-size, quota, and ink-budget decisions to the
            # deterministic local selector.
            building_values = frozenset(
                {
                    "castle",
                    "cathedral",
                    "chapel",
                    "church",
                    "civic",
                    "college",
                    "government",
                    "grandstand",
                    "hospital",
                    "mosque",
                    "palace",
                    "public",
                    "religious",
                    "shrine",
                    "sports_hall",
                    "stadium",
                    "synagogue",
                    "temple",
                    "train_station",
                    "university",
                }
            )
            amenity_values = frozenset(
                {
                    "arts_centre",
                    "college",
                    "courthouse",
                    "hospital",
                    "library",
                    "place_of_worship",
                    "theatre",
                    "townhall",
                    "university",
                }
            )
            tourism_values = frozenset({"gallery", "museum"})
            historic_values = frozenset(
                {"castle", "memorial", "monument", "palace", "tower"}
            )
            heritage_site_values = frozenset({"castle", "palace"})
            for object_type in ("way", "relation"):
                statements.extend(
                    (
                        f'{object_type}["building"~"{_value_regex(building_values)}"]({box});',
                        f'{object_type}["building"]["amenity"~"{_value_regex(amenity_values)}"]({box});',
                        f'{object_type}["building"]["tourism"~"{_value_regex(tourism_values)}"]({box});',
                        f'{object_type}["building"]["healthcare"="hospital"]({box});',
                        f'{object_type}["building"]["historic"~"{_value_regex(historic_values)}"]({box});',
                        f'{object_type}["historic"~"{_value_regex(heritage_site_values)}"]({box});',
                        f'{object_type}["leisure"="stadium"]({box});',
                    )
                )
        else:
            statements.extend(
                (
                    f'way["building"]({box});',
                    f'relation["building"]({box});',
                    f'way["leisure"="stadium"]({box});',
                    f'relation["leisure"="stadium"]({box});',
                )
            )
    if "boundaries" in families:
        statements.extend(
            (
                f'way["boundary"="administrative"]({box});',
                f'relation["boundary"="administrative"]({box});',
            )
        )
    for landmark_ref in canonical_refs:
        object_type, object_id = landmark_ref.split("/", maxsplit=1)
        statements.append(f"{object_type}({object_id});")

    body = "\n  ".join(statements)
    # ``out geom`` embeds geometry for a relation's direct way members, but it
    # does not embed a member relation as another complete relation object.
    # Keep the initial bbox selection in a named set, recurse only from its
    # relation subset, and add the transitive relation descendants to the
    # output.  Each descendant relation then receives its own inline direct-way
    # geometry.  This is deliberately narrower than recursing from the complete
    # selected set: the thousands of independently selected road and railway
    # ways do not cause their nodes to be materialised as top-level results.
    relation_families = {"roads", "water", "parks", "buildings", "boundaries"}
    has_direct_relation = any(
        landmark_ref.startswith("relation/") for landmark_ref in canonical_refs
    )
    if relation_families.intersection(families) or has_direct_relation:
        return (
            f"[out:json][timeout:{timeout_s}];\n"
            f"(\n  {body}\n) -> .selected;\n"
            "rel.selected -> .relation_roots;\n"
            ".relation_roots >> -> .relation_closure;\n"
            "(\n"
            "  .selected;\n"
            "  rel.relation_closure;\n"
            ");\n"
            "out geom qt;\n"
        )

    # Inline geometry keeps the response self-contained. Quadtile ordering is
    # cheaper for Overpass than its default ID sort; final plot order is handled
    # separately by the compiler.  Preserve the compact query for families such
    # as railways that have no relation selector.
    return f"[out:json][timeout:{timeout_s}];\n(\n  {body}\n);\nout geom qt;\n"


def _cache_key(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _overpass_cache_identity(query: str) -> str:
    """Remove execution budget from a query without hiding data semantics."""

    return re.sub(r"\[timeout:\d+\]", "[timeout:*]", query, count=1)


def _overpass_server_timeout(http_timeout_s: int) -> int:
    """Leave headroom for Overpass to return its own timeout response."""

    headroom = max(1, min(60, http_timeout_s // 4))
    return max(1, min(180, http_timeout_s - headroom))


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.suffix.casefold() == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                value = json.load(stream)
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, EOFError, json.JSONDecodeError) as exc:
        raise MapPlotterError(f"Could not read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MapPlotterError(f"Expected a JSON object in {path}.")
    return value


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    if path.suffix.casefold() == ".gz":
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
    else:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _validate_overpass(data: dict[str, Any], source: str) -> dict[str, Any]:
    remark = data.get("remark")
    if remark:
        raise MapPlotterError(
            f"Overpass reported an incomplete result from {source}: {remark}"
        )
    if not isinstance(data.get("elements"), list):
        raise MapPlotterError(
            f"{source} is not an Overpass response with an elements list."
        )
    return data


def _retry_delay(attempt: int) -> float:
    """Return a small bounded exponential delay for a zero-based attempt."""

    return min(2.0 ** (attempt + 1), 30.0)


def _has_retryable_overpass_remark(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    remark = value.get("remark")
    if not isinstance(remark, str):
        return False
    normalized = remark.casefold()
    return any(fragment in normalized for fragment in _RETRYABLE_OVERPASS_REMARKS)


def _decode_response_payload(payload: bytes, content_encoding: str) -> bytes:
    encodings = {
        encoding.strip().casefold()
        for encoding in content_encoding.split(",")
        if encoding.strip()
    }
    if "gzip" not in encodings and "x-gzip" not in encodings:
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise MapPlotterError(
                "Map service response exceeds the 128 MiB safety limit."
            )
        return payload
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            decoded = stream.read(_MAX_RESPONSE_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise MapPlotterError("Map service returned invalid gzip data.") from exc
    if len(decoded) > _MAX_RESPONSE_BYTES:
        raise MapPlotterError(
            "Decompressed map service response exceeds the 128 MiB safety limit."
        )
    return decoded


def _request_json(
    url: str,
    *,
    user_agent: str,
    timeout_s: int,
    data: bytes | None = None,
    transient_retries: int = 0,
) -> dict[str, Any] | list[Any]:
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
        method="POST" if data is not None else "GET",
    )
    for attempt in range(transient_retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
                content_encoding = response.headers.get("Content-Encoding", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", errors="replace").strip()
            if exc.code in _TRANSIENT_HTTP_STATUS and attempt < transient_retries:
                retry_after = exc.headers.get("Retry-After")
                try:
                    delay = (
                        max(float(retry_after), 1.0)
                        if retry_after
                        else _retry_delay(attempt)
                    )
                except ValueError:
                    delay = _retry_delay(attempt)
                if delay > 30:
                    raise MapPlotterError(
                        f"Map service is busy and requested a {delay:.0f}-second delay; try again later."
                    ) from exc
                time.sleep(delay)
                continue
            raise MapPlotterError(
                f"Map service returned HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            if attempt < transient_retries:
                time.sleep(_retry_delay(attempt))
                continue
            raise MapPlotterError(f"Could not reach map service: {exc.reason}") from exc
        except TimeoutError as exc:
            if attempt < transient_retries:
                time.sleep(_retry_delay(attempt))
                continue
            raise MapPlotterError(
                f"Map service timed out after {timeout_s} seconds."
            ) from exc

        payload = _decode_response_payload(payload, content_encoding)
        try:
            result = json.loads(payload)
        except json.JSONDecodeError as exc:
            preview = payload[:300].decode("utf-8", errors="replace")
            raise MapPlotterError(
                f"Map service returned invalid JSON: {preview}"
            ) from exc

        if _has_retryable_overpass_remark(result) and attempt < transient_retries:
            time.sleep(_retry_delay(attempt))
            continue
        return result

    raise MapPlotterError("Map service returned no response.")


def fetch_overpass(
    bbox: BoundingBox,
    families: tuple[str, ...],
    *,
    endpoint: str = DEFAULT_OVERPASS_URL,
    user_agent: str,
    cache_dir: Path,
    timeout_s: int = 120,
    refresh: bool = False,
    landmark_buildings_only: bool = False,
    landmark_refs: tuple[str, ...] = (),
) -> AcquisitionResult:
    canonical_refs = canonical_landmark_refs(landmark_refs)
    query = build_overpass_query(
        bbox,
        families,
        timeout_s=_overpass_server_timeout(timeout_s),
        landmark_buildings_only=landmark_buildings_only,
        landmark_refs=canonical_refs,
    )
    source_metadata = {
        "landmark_ref_acquisition": {
            "requested_refs": list(canonical_refs),
            "direct_selector_count": len(canonical_refs),
        }
    }
    cache_path = (
        cache_dir
        / "overpass"
        / f"{_cache_key(endpoint, _overpass_cache_identity(query))}.json.gz"
    )
    plain_cache_path = cache_path.with_suffix("")
    # Continue to recognise transitional caches keyed by the full query text.
    legacy_cache_path = cache_dir / "overpass" / f"{_cache_key(endpoint, query)}.json"
    if cache_path.exists() and not refresh:
        cached = _validate_overpass(_load_json(cache_path), str(cache_path))
        return AcquisitionResult(
            cached,
            endpoint,
            query,
            str(cache_path),
            True,
            source_metadata=source_metadata,
        )
    if plain_cache_path.exists() and not refresh:
        cached = _validate_overpass(_load_json(plain_cache_path), str(plain_cache_path))
        return AcquisitionResult(
            cached,
            endpoint,
            query,
            str(plain_cache_path),
            True,
            source_metadata=source_metadata,
        )
    if legacy_cache_path.exists() and not refresh:
        cached = _validate_overpass(
            _load_json(legacy_cache_path), str(legacy_cache_path)
        )
        return AcquisitionResult(
            cached,
            endpoint,
            query,
            str(legacy_cache_path),
            True,
            source_metadata=source_metadata,
        )

    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    result = _request_json(
        endpoint,
        user_agent=user_agent,
        timeout_s=timeout_s,
        data=body,
        transient_retries=2,
    )
    if not isinstance(result, dict):
        raise MapPlotterError("Overpass returned an unexpected non-object response.")
    result = _validate_overpass(result, endpoint)
    _atomic_write_json(cache_path, result)
    return AcquisitionResult(
        result,
        endpoint,
        query,
        str(cache_path),
        False,
        source_metadata=source_metadata,
    )


def fetch_course_relation(
    relation_id: int,
    *,
    endpoint: str = DEFAULT_OVERPASS_URL,
    user_agent: str,
    cache_dir: Path,
    timeout_s: int = 180,
    refresh: bool = False,
) -> AcquisitionResult:
    """Fetch one route relation with the full geometry of every member way.

    Cached and validated exactly like a map extract, so a course import is
    reproducible offline and the geometry that was verified is the geometry
    that gets drawn.
    """

    # Imported here because `course` is a consumer of this module's results;
    # keeping the dependency one-way avoids a cycle.
    from .course import build_course_relation_query

    query = build_course_relation_query(relation_id, timeout_s=_overpass_server_timeout(timeout_s))
    cache_path = (
        cache_dir / "overpass" / f"{_cache_key(endpoint, query)}.json.gz"
    )
    if cache_path.exists() and not refresh:
        cached = _validate_overpass(_load_json(cache_path), str(cache_path))
        return AcquisitionResult(cached, endpoint, query, str(cache_path), True)

    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    result = _request_json(
        endpoint,
        user_agent=user_agent,
        timeout_s=timeout_s,
        data=body,
        transient_retries=2,
    )
    if not isinstance(result, dict):
        raise MapPlotterError("Overpass returned an unexpected non-object response.")
    result = _validate_overpass(result, endpoint)
    _atomic_write_json(cache_path, result)
    return AcquisitionResult(result, endpoint, query, str(cache_path), False)


def load_overpass_file(path: Path) -> AcquisitionResult:
    data = _validate_overpass(_load_json(path), str(path))
    return AcquisitionResult(
        data, f"file:{path.resolve()}", None, str(path.resolve()), True
    )


@contextmanager
def _exclusive_lock(path: Path, timeout_s: float = 10.0) -> Iterator[None]:
    """A small portable lock for coordinating rate limits across CLI processes."""

    started = time.monotonic()
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 60:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() - started >= timeout_s:
                raise MapPlotterError(
                    "Timed out waiting for the local Nominatim rate-limit lock."
                )
            time.sleep(0.05)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
                path.unlink(missing_ok=True)
            raise MapPlotterError(
                f"Could not create local rate-limit lock {path}: {exc}"
            ) from exc
    try:
        yield
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


def _respect_nominatim_rate_limit(cache_dir: Path) -> None:
    marker = cache_dir / "nominatim" / "last-request.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(marker.parent / "rate-limit.lock"):
        try:
            previous = float(marker.read_text(encoding="ascii"))
        except (OSError, ValueError):
            previous = 0.0
        remaining = 1.05 - (time.time() - previous)
        if remaining > 0:
            time.sleep(remaining)
        marker.write_text(str(time.time()), encoding="ascii")


def geocode_place(
    place: str,
    *,
    endpoint: str = DEFAULT_NOMINATIM_URL,
    user_agent: str,
    cache_dir: Path,
    timeout_s: int = 30,
    refresh: bool = False,
    country_code: str | None = None,
) -> tuple[BoundingBox, tuple[float, float], str]:
    parameters_value = {
        "q": place,
        "format": "jsonv2",
        "limit": 5,
        "addressdetails": 1,
    }
    if country_code is not None:
        normalized_country = country_code.strip().casefold()
        if not re.fullmatch(r"[a-z]{2}", normalized_country):
            raise MapPlotterError("Nominatim country code must be two letters.")
        parameters_value["countrycodes"] = normalized_country
    parameters = urllib.parse.urlencode(parameters_value)
    url = f"{endpoint}?{parameters}"
    cache_path = (
        cache_dir
        / "nominatim"
        / f"{_cache_key(endpoint, place.casefold(), country_code or '')}.json"
    )
    if cache_path.exists() and not refresh:
        try:
            response = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MapPlotterError(
                f"Could not read cached place result {cache_path}: {exc}"
            ) from exc
    else:
        _respect_nominatim_rate_limit(cache_dir)
        response = _request_json(url, user_agent=user_agent, timeout_s=timeout_s)
        _atomic_write_json(cache_path, response)

    if not isinstance(response, list) or not response:
        raise MapPlotterError(f"No place matched {place!r}.")
    candidates: list[tuple[tuple[float, float, float, float, int], dict[str, Any]]] = []
    query_name = place.split(",", maxsplit=1)[0].strip().casefold()
    preferred_types = {
        "city",
        "town",
        "village",
        "borough",
        "suburb",
        "quarter",
        "university",
    }
    for index, candidate in enumerate(response):
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("boundingbox"), list
        ):
            continue
        candidate_name = str(candidate.get("name", "")).strip().casefold()
        candidate_type = str(candidate.get("type", "")).casefold()
        candidate_class = str(candidate.get("class", "")).casefold()
        try:
            importance = float(candidate.get("importance", 0))
        except (TypeError, ValueError):
            importance = 0.0
        score = (
            1.0 if candidate_name == query_name else 0.0,
            1.0 if candidate_type in preferred_types else 0.0,
            1.0 if candidate_class in {"place", "boundary", "amenity"} else 0.0,
            importance,
            -index,
        )
        candidates.append((score, candidate))
    if not candidates:
        raise MapPlotterError("Nominatim returned no usable bounded place result.")
    selected = max(candidates, key=lambda item: item[0])[1]
    try:
        south, north, west, east = (float(value) for value in selected["boundingbox"])
        center = (float(selected["lat"]), float(selected["lon"]))
        display_name = str(selected["display_name"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError("Nominatim returned an unexpected place result.") from exc
    return BoundingBox(west, south, east, north), center, display_name

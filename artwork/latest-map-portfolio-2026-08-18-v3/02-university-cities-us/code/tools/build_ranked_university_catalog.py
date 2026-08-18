#!/usr/bin/env python3
"""Build the deterministic 2026/2027 ranked-university campus catalog.

The generator is deliberately offline.  It reuses selected UK campus seeds from
the bundled schema-v1 catalog only after checking that file's pinned SHA-256,
and combines them with explicitly reviewed Nominatim results.  Ranking order,
ties, scores, campus coordinates, and provenance are data below rather than
network lookups.

Run from anywhere in the checkout::

    .venv/bin/python tools/build_ranked_university_catalog.py
    .venv/bin/python tools/build_ranked_university_catalog.py --check
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, NoReturn, Sequence


ROOT = Path(__file__).resolve().parent.parent
BASE_CATALOG = ROOT / "src" / "city_map_plotter" / "data" / "catalog-v1.json"
OUTPUT = (
    ROOT
    / "src"
    / "city_map_plotter"
    / "data"
    / "ranked-universities-2026-v1.json"
)

SCHEMA_VERSION = 1
CATALOG_VERSION = "ranked-universities-2026-v1"
AS_OF = "2026-08-03"
PREVIEW_RADIUS_KM = 2.0
BASE_CATALOG_SHA256 = (
    "fac118eab3b597c9629975ac3b57d7be6514032f2c6527252f1ff6eba780295f"
)
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
NOMINATIM_QUERY_DATE = "2026-08-03"
NOMINATIM_USER_AGENT = "CityMapPlotter/0.2 (local pen-plot art batch)"
OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"

UK_COLLECTION_ID = "uk-times-good-university-guide-2026-top-30"
US_COLLECTION_ID = "us-qs-world-university-rankings-2027-top-20"

UK_RANKING_URLS = (
    "https://www.thetimes.com/uk-university-rankings",
    (
        "https://www.thetimes.com/uk-university-rankings/feature-guide/"
        "article/best-good-university-guide-2026-nlg69hm5k"
    ),
    (
        "https://www.fenews.co.uk/fe-voices/"
        "the-times-and-the-sunday-times-good-university-guide-2026/"
    ),
)
US_RANKING_URLS = (
    "https://www.topuniversities.com/qs-top-uni-wur",
    (
        "https://www.qs.com/insights/"
        "qs-world-university-rankings-2027-results-table-excel"
    ),
    (
        "https://www.qs.com/insights/"
        "us-performance-in-the-qs-world-university-rankings-2027"
    ),
)


class CatalogBuildError(RuntimeError):
    """A pinned input or generated catalog failed closed."""


def _fail(message: str) -> NoReturn:
    raise CatalogBuildError(message)


@dataclass(frozen=True)
class RankingRow:
    subject_id: str
    ranking_name: str
    rank: str
    score: float | None = None


@dataclass(frozen=True)
class CampusSeed:
    subject_id: str
    name: str
    city: str
    region: str
    country: str
    country_code: str
    institution_url: str
    latitude: str
    longitude: str
    osm_ref: str
    query: str
    selection_role: str = "bounded_institution_or_campus_feature"
    selection_evidence_url: str | None = None


UK_RANKING: tuple[RankingRow, ...] = (
    RankingRow(
        "uk-university-lse",
        "London School of Economics and Political Science",
        "1",
    ),
    RankingRow("uk-university-st-andrews", "University of St Andrews", "2"),
    RankingRow("uk-university-durham", "Durham University", "3"),
    RankingRow("uk-university-cambridge", "University of Cambridge", "4="),
    RankingRow("uk-university-oxford", "University of Oxford", "4="),
    RankingRow("uk-university-imperial", "Imperial College London", "6"),
    RankingRow("uk-university-bath", "University of Bath", "7"),
    RankingRow("uk-university-warwick", "University of Warwick", "8"),
    RankingRow("uk-university-ucl", "University College London", "9"),
    RankingRow("uk-university-bristol", "University of Bristol", "10"),
    RankingRow("uk-university-strathclyde", "University of Strathclyde", "11"),
    RankingRow("uk-university-loughborough", "Loughborough University", "12"),
    RankingRow("uk-university-sheffield", "University of Sheffield", "13"),
    RankingRow("uk-university-exeter", "University of Exeter", "14"),
    RankingRow("uk-university-lancaster", "Lancaster University", "15"),
    RankingRow("uk-university-birmingham", "University of Birmingham", "16"),
    RankingRow("uk-university-southampton", "University of Southampton", "17"),
    RankingRow("uk-university-liverpool", "University of Liverpool", "18"),
    RankingRow("uk-university-kings", "King's College London", "19"),
    RankingRow("uk-university-york", "University of York", "20"),
    RankingRow("uk-university-queens-belfast", "Queen's University Belfast", "21"),
    RankingRow("uk-university-glasgow", "University of Glasgow", "22"),
    RankingRow("uk-university-aberdeen", "University of Aberdeen", "23="),
    RankingRow("uk-university-dundee", "University of Dundee", "23="),
    RankingRow("uk-university-edinburgh", "University of Edinburgh", "25"),
    RankingRow("uk-university-leeds", "University of Leeds", "26"),
    RankingRow("uk-university-manchester", "University of Manchester", "27"),
    RankingRow("uk-university-cardiff", "Cardiff University", "28="),
    RankingRow("uk-university-leicester", "University of Leicester", "28="),
    RankingRow("uk-university-nottingham", "University of Nottingham", "30"),
)


NEW_UK_CAMPUSES: tuple[CampusSeed, ...] = (
    CampusSeed(
        "uk-university-st-andrews",
        "University of St Andrews",
        "St Andrews",
        "Scotland",
        "United Kingdom",
        "GB",
        "https://www.st-andrews.ac.uk/",
        "56.3398554",
        "-2.8117775",
        "relation/10049408",
        "University of St Andrews, St Andrews, United Kingdom",
    ),
    CampusSeed(
        "uk-university-bath",
        "University of Bath",
        "Bath",
        "England",
        "United Kingdom",
        "GB",
        "https://www.bath.ac.uk/",
        "51.3766938",
        "-2.3234206",
        "way/345133857",
        "University of Bath, Bath, United Kingdom",
    ),
    CampusSeed(
        "uk-university-strathclyde",
        "University of Strathclyde",
        "Glasgow",
        "Scotland",
        "United Kingdom",
        "GB",
        "https://www.strath.ac.uk/",
        "55.8618812",
        "-4.2419566",
        "way/20448384",
        "University of Strathclyde, Glasgow, United Kingdom",
    ),
    CampusSeed(
        "uk-university-loughborough",
        "Loughborough University",
        "Loughborough",
        "England",
        "United Kingdom",
        "GB",
        "https://www.lboro.ac.uk/",
        "52.7638450",
        "-1.2372582",
        "way/9961576",
        "Loughborough University, Loughborough, United Kingdom",
    ),
    CampusSeed(
        "uk-university-lancaster",
        "Lancaster University",
        "Lancaster",
        "England",
        "United Kingdom",
        "GB",
        "https://www.lancaster.ac.uk/",
        "54.0098429",
        "-2.7875768",
        "way/23347159",
        "Lancaster University, Lancaster, United Kingdom",
    ),
    CampusSeed(
        "uk-university-aberdeen",
        "University of Aberdeen",
        "Aberdeen",
        "Scotland",
        "United Kingdom",
        "GB",
        "https://www.abdn.ac.uk/",
        "57.1645542",
        "-2.1018424",
        "relation/8451250",
        "University of Aberdeen, Aberdeen, United Kingdom",
    ),
    CampusSeed(
        "uk-university-dundee",
        "University of Dundee",
        "Dundee",
        "Scotland",
        "United Kingdom",
        "GB",
        "https://www.dundee.ac.uk/",
        "56.4579676",
        "-2.9821483",
        "way/45115495",
        "University of Dundee, Dundee, United Kingdom",
    ),
    CampusSeed(
        "uk-university-leicester",
        "University of Leicester",
        "Leicester",
        "England",
        "United Kingdom",
        "GB",
        "https://le.ac.uk/",
        "52.6087005",
        "-1.1148240",
        "way/294335092",
        "University of Leicester, Leicester, United Kingdom",
    ),
)


US_RANKING: tuple[RankingRow, ...] = (
    RankingRow(
        "us-university-mit",
        "Massachusetts Institute of Technology (MIT)",
        "1",
        100.0,
    ),
    RankingRow("us-university-stanford", "Stanford University", "=2", 99.2),
    RankingRow("us-university-harvard", "Harvard University", "5", 97.4),
    RankingRow(
        "us-university-caltech",
        "California Institute of Technology (Caltech)",
        "7",
        96.6,
    ),
    RankingRow(
        "us-university-pennsylvania", "University of Pennsylvania", "15", 91.7
    ),
    RankingRow("us-university-cornell", "Cornell University", "=16", 91.5),
    RankingRow("us-university-yale", "Yale University", "=16", 91.5),
    RankingRow(
        "us-university-johns-hopkins", "Johns Hopkins University", "=20", 89.7
    ),
    RankingRow(
        "us-university-uc-berkeley",
        "University of California, Berkeley (UCB)",
        "=20",
        89.7,
    ),
    RankingRow("us-university-chicago", "University of Chicago", "24", 89.2),
    RankingRow("us-university-princeton", "Princeton University", "27", 88.9),
    RankingRow("us-university-columbia", "Columbia University", "=43", 84.0),
    RankingRow(
        "us-university-northwestern", "Northwestern University", "=45", 83.8
    ),
    RankingRow(
        "us-university-ucla",
        "University of California, Los Angeles (UCLA)",
        "49",
        82.7,
    ),
    RankingRow(
        "us-university-michigan-ann-arbor",
        "University of Michigan–Ann Arbor",
        "51",
        82.2,
    ),
    RankingRow(
        "us-university-carnegie-mellon",
        "Carnegie Mellon University",
        "55",
        81.4,
    ),
    RankingRow("us-university-nyu", "New York University (NYU)", "58", 80.8),
    RankingRow("us-university-brown", "Brown University", "66", 77.4),
    RankingRow("us-university-duke", "Duke University", "70", 75.8),
    RankingRow(
        "us-university-ut-austin", "University of Texas at Austin", "72", 75.3
    ),
)


US_CAMPUSES: tuple[CampusSeed, ...] = (
    CampusSeed(
        "us-university-mit",
        "Massachusetts Institute of Technology (MIT)",
        "Cambridge",
        "Massachusetts",
        "United States",
        "US",
        "https://www.mit.edu/",
        "42.3582529",
        "-71.0966272",
        "relation/65066",
        "Massachusetts Institute of Technology, Cambridge, Massachusetts, United States",
    ),
    CampusSeed(
        "us-university-stanford",
        "Stanford University",
        "Stanford",
        "California",
        "United States",
        "US",
        "https://www.stanford.edu/",
        "37.4313138",
        "-122.1693654",
        "way/29268613",
        "Stanford University, Stanford, California, United States",
    ),
    CampusSeed(
        "us-university-harvard",
        "Harvard University",
        "Cambridge",
        "Massachusetts",
        "United States",
        "US",
        "https://www.harvard.edu/",
        "42.3657432",
        "-71.1222139",
        "relation/2415825",
        "Harvard University, Cambridge, Massachusetts, United States",
    ),
    CampusSeed(
        "us-university-caltech",
        "California Institute of Technology (Caltech)",
        "Pasadena",
        "California",
        "United States",
        "US",
        "https://www.caltech.edu/",
        "34.1370138",
        "-118.1252883",
        "way/29111188",
        "California Institute of Technology, Pasadena, California, United States",
    ),
    CampusSeed(
        "us-university-pennsylvania",
        "University of Pennsylvania",
        "Philadelphia",
        "Pennsylvania",
        "United States",
        "US",
        "https://www.upenn.edu/",
        "39.9522148",
        "-75.1955729",
        "relation/2594845",
        "University of Pennsylvania, Philadelphia, Pennsylvania, United States",
    ),
    CampusSeed(
        "us-university-cornell",
        "Cornell University",
        "Ithaca",
        "New York",
        "United States",
        "US",
        "https://www.cornell.edu/",
        "42.4529160",
        "-76.4800635",
        "relation/11625564",
        "Cornell University, Ithaca, New York, United States",
    ),
    CampusSeed(
        "us-university-yale",
        "Yale University",
        "New Haven",
        "Connecticut",
        "United States",
        "US",
        "https://www.yale.edu/",
        "41.3104643",
        "-72.9271397",
        "relation/5421111",
        "Cross Campus, New Haven, Connecticut, United States",
        "representative_main_campus_anchor",
        "https://locations.yale.edu/popular-locations-yale",
    ),
    CampusSeed(
        "us-university-johns-hopkins",
        "Johns Hopkins University",
        "Baltimore",
        "Maryland",
        "United States",
        "US",
        "https://www.jhu.edu/",
        "39.3302023",
        "-76.6218536",
        "relation/8043868",
        "Johns Hopkins University Homewood Campus, Baltimore, Maryland, United States",
    ),
    CampusSeed(
        "us-university-uc-berkeley",
        "University of California, Berkeley (UCB)",
        "Berkeley",
        "California",
        "United States",
        "US",
        "https://www.berkeley.edu/",
        "37.8720601",
        "-122.2578318",
        "way/24024507",
        "Sather Tower, Berkeley, California, United States",
        "representative_main_campus_anchor",
        "https://news.berkeley.edu/2015/02/04/campanile-centennial-celebration/",
    ),
    CampusSeed(
        "us-university-chicago",
        "University of Chicago",
        "Chicago",
        "Illinois",
        "United States",
        "US",
        "https://www.uchicago.edu/",
        "41.7913274",
        "-87.6008421",
        "relation/13117436",
        "The University of Chicago, 5801 South Ellis Avenue, Chicago, Illinois, United States",
    ),
    CampusSeed(
        "us-university-princeton",
        "Princeton University",
        "Princeton",
        "New Jersey",
        "United States",
        "US",
        "https://www.princeton.edu/",
        "40.3386752",
        "-74.6583655",
        "relation/6365145",
        "Princeton University, Princeton, New Jersey, United States",
    ),
    CampusSeed(
        "us-university-columbia",
        "Columbia University",
        "New York City",
        "New York",
        "United States",
        "US",
        "https://www.columbia.edu/",
        "40.8077507",
        "-73.9624901",
        "way/732228095",
        "Columbia University, New York City, New York, United States",
    ),
    CampusSeed(
        "us-university-northwestern",
        "Northwestern University",
        "Evanston",
        "Illinois",
        "United States",
        "US",
        "https://www.northwestern.edu/",
        "42.0557157",
        "-87.6752945",
        "relation/2105485",
        "Northwestern University, Evanston, Illinois, United States",
    ),
    CampusSeed(
        "us-university-ucla",
        "University of California, Los Angeles (UCLA)",
        "Los Angeles",
        "California",
        "United States",
        "US",
        "https://www.ucla.edu/",
        "34.0708777",
        "-118.4468503",
        "relation/7493269",
        "University of California Los Angeles, Los Angeles, California, United States",
    ),
    CampusSeed(
        "us-university-michigan-ann-arbor",
        "University of Michigan–Ann Arbor",
        "Ann Arbor",
        "Michigan",
        "United States",
        "US",
        "https://umich.edu/",
        "42.2789918",
        "-83.7353389",
        "way/40976143",
        "Central Campus, Ann Arbor, Michigan, United States",
        "bounded_main_campus_feature",
        (
            "https://campusinvolvement.umich.edu/article/"
            "central-campus-diagnorth-campus-gerstacker-grove"
        ),
    ),
    CampusSeed(
        "us-university-carnegie-mellon",
        "Carnegie Mellon University",
        "Pittsburgh",
        "Pennsylvania",
        "United States",
        "US",
        "https://www.cmu.edu/",
        "40.4439193",
        "-79.9428267",
        "relation/2279034",
        "Carnegie Mellon University, Pittsburgh, Pennsylvania, United States",
    ),
    CampusSeed(
        "us-university-nyu",
        "New York University (NYU)",
        "New York City",
        "New York",
        "United States",
        "US",
        "https://www.nyu.edu/",
        "40.7292053",
        "-73.9950148",
        "way/108156090",
        "New York University, New York City, New York, United States",
    ),
    CampusSeed(
        "us-university-brown",
        "Brown University",
        "Providence",
        "Rhode Island",
        "United States",
        "US",
        "https://www.brown.edu/",
        "41.8186395",
        "-71.4088009",
        "relation/13816239",
        "Brown University, Providence, Rhode Island, United States",
    ),
    CampusSeed(
        "us-university-duke",
        "Duke University",
        "Durham",
        "North Carolina",
        "United States",
        "US",
        "https://www.duke.edu/",
        "36.0001557",
        "-78.9442297",
        "relation/7407432",
        "Duke University, Durham, North Carolina, United States",
    ),
    CampusSeed(
        "us-university-ut-austin",
        "University of Texas at Austin",
        "Austin",
        "Texas",
        "United States",
        "US",
        "https://www.utexas.edu/",
        "30.2851494",
        "-97.7339352",
        "relation/1701848",
        "University of Texas at Austin, Austin, Texas, United States",
    ),
)


def _read_pinned_base(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        _fail(f"could not read base catalog {path}: {exc}")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != BASE_CATALOG_SHA256:
        _fail(
            f"base catalog SHA-256 mismatch: expected {BASE_CATALOG_SHA256}, "
            f"got {digest}"
        )
    try:
        base = json.loads(payload)
    except json.JSONDecodeError as exc:
        _fail(f"could not parse base catalog {path}: {exc}")
    if not isinstance(base, dict) or base.get("schema_version") != SCHEMA_VERSION:
        _fail("base catalog is not schema-v1")
    if not isinstance(base.get("subjects"), list):
        _fail("base catalog subjects must be an array")
    return base


def _unique_urls(*groups: Sequence[str]) -> list[str]:
    result: list[str] = []
    for group in groups:
        for url in group:
            if not url.startswith("https://"):
                _fail(f"provenance URL must use HTTPS: {url!r}")
            if url not in result:
                result.append(url)
    return result


def _osm_url(osm_ref: str) -> str:
    object_type, separator, object_id = osm_ref.partition("/")
    if separator != "/" or object_type not in {"node", "way", "relation"}:
        _fail(f"invalid OSM reference {osm_ref!r}")
    if not object_id.isdigit() or int(object_id) < 1:
        _fail(f"invalid OSM reference {osm_ref!r}")
    return f"https://www.openstreetmap.org/{object_type}/{object_id}"


def _nominatim_subject(
    campus: CampusSeed, ranking_urls: Sequence[str]
) -> dict[str, Any]:
    allowed_selection_roles = {
        "bounded_institution_or_campus_feature",
        "bounded_main_campus_feature",
        "representative_main_campus_anchor",
    }
    if campus.selection_role not in allowed_selection_roles:
        _fail(
            f"invalid campus selection role for {campus.subject_id}: "
            f"{campus.selection_role!r}"
        )
    osm_url = _osm_url(campus.osm_ref)
    return {
        "id": campus.subject_id,
        "kind": "university",
        "name": campus.name,
        "location": {
            "city": campus.city,
            "region": campus.region,
            "country": campus.country,
            "country_code": campus.country_code,
        },
        "map": {
            "center": [float(campus.latitude), float(campus.longitude)],
            "query": campus.query,
            "preview_radius_km": PREVIEW_RADIUS_KM,
            "purpose": "campus",
        },
        "details": {
            "institution_url": campus.institution_url,
            "geometry_status": "seed_point_ready",
            "coordinate_provenance": {
                "provider": "Nominatim / OpenStreetMap",
                "endpoint": NOMINATIM_ENDPOINT,
                "query": campus.query,
                "query_date": NOMINATIM_QUERY_DATE,
                "osm_ref": campus.osm_ref,
                "source_center": [campus.latitude, campus.longitude],
                "selection_role": campus.selection_role,
                "selection_evidence_url": campus.selection_evidence_url,
                "copyright_url": OSM_COPYRIGHT_URL,
            },
        },
        "source_urls": _unique_urls(
            (campus.institution_url,),
            ranking_urls,
            (
                *((campus.selection_evidence_url,) if campus.selection_evidence_url else ()),
                osm_url,
                OSM_COPYRIGHT_URL,
            ),
        ),
    }


def _base_subject(
    base_subject: dict[str, Any], row: RankingRow
) -> dict[str, Any]:
    if base_subject.get("id") != row.subject_id:
        _fail(f"base subject ID mismatch for {row.subject_id}")
    if base_subject.get("name") != row.ranking_name:
        _fail(
            f"base subject name changed for {row.subject_id}: "
            f"expected {row.ranking_name!r}, got {base_subject.get('name')!r}"
        )
    location = base_subject.get("location")
    mapping = base_subject.get("map")
    details = base_subject.get("details")
    source_urls = base_subject.get("source_urls")
    if (
        not isinstance(location, dict)
        or not isinstance(mapping, dict)
        or not isinstance(details, dict)
    ):
        _fail(f"base subject {row.subject_id} has an invalid object")
    if not isinstance(source_urls, list) or not all(
        isinstance(url, str) for url in source_urls
    ):
        _fail(f"base subject {row.subject_id} has invalid source URLs")
    if base_subject.get("kind") != "university" or location.get("country_code") != "GB":
        _fail(f"base subject {row.subject_id} is not a UK university")
    center = mapping.get("center")
    if (
        not isinstance(center, list)
        or len(center) != 2
        or not all(isinstance(value, (int, float)) for value in center)
    ):
        _fail(f"base subject {row.subject_id} has an invalid center")
    wikidata_id = details.get("wikidata_id")
    if not isinstance(wikidata_id, str) or not wikidata_id.startswith("Q"):
        _fail(f"base subject {row.subject_id} lacks a Wikidata ID")

    subject = copy.deepcopy(base_subject)
    subject["map"]["preview_radius_km"] = PREVIEW_RADIUS_KM
    subject["map"]["purpose"] = "campus"
    subject["details"]["coordinate_provenance"] = {
        "provider": "pinned bundled catalog-v1.json / Wikidata P625",
        "base_subject_id": row.subject_id,
        "base_catalog_sha256": BASE_CATALOG_SHA256,
        "wikidata_id": wikidata_id,
        "source_center": [str(center[0]), str(center[1])],
    }
    wikidata_url = f"https://www.wikidata.org/wiki/{wikidata_id}"
    subject["source_urls"] = _unique_urls(
        tuple(source_urls), UK_RANKING_URLS, (wikidata_url, OSM_COPYRIGHT_URL)
    )
    return subject


def _rank_number(display_rank: str) -> int:
    normalized = display_rank.replace("=", "")
    if not normalized.isdigit() or int(normalized) < 1:
        _fail(f"invalid display rank {display_rank!r}")
    return int(normalized)


def _entry(row: RankingRow, position: int, edition: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "subject_id": row.subject_id,
        "position": position,
        "rank": row.rank,
        "rank_number": _rank_number(row.rank),
        "tied": "=" in row.rank,
        "edition": edition,
        "ranking_name": row.ranking_name,
    }
    if row.score is not None:
        entry["score"] = row.score
    return entry


def validate_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on the cohort and schema invariants owned by this builder."""

    if catalog.get("schema_version") != SCHEMA_VERSION:
        _fail("generated catalog must use schema-v1")
    collections = catalog.get("collections")
    subjects = catalog.get("subjects")
    if not isinstance(collections, list) or len(collections) != 2:
        _fail("generated catalog must contain exactly two collections")
    if not isinstance(subjects, list) or len(subjects) != 50:
        _fail("generated catalog must contain exactly 50 subjects")
    subject_ids = [subject.get("id") for subject in subjects]
    if not all(isinstance(subject_id, str) for subject_id in subject_ids):
        _fail("every generated subject needs an ID")
    if len(subject_ids) != len(set(subject_ids)):
        _fail("generated subject IDs must be globally unique")

    expected = {
        UK_COLLECTION_ID: (UK_RANKING, "GB"),
        US_COLLECTION_ID: (US_RANKING, "US"),
    }
    by_id = {subject["id"]: subject for subject in subjects}
    for collection in collections:
        collection_id = collection.get("id")
        if collection_id not in expected:
            _fail(f"unexpected generated collection {collection_id!r}")
        rows, country_code = expected[collection_id]
        entries = collection.get("entries")
        if not isinstance(entries, list) or len(entries) != len(rows):
            _fail(f"{collection_id} has the wrong cohort size")
        if [entry.get("position") for entry in entries] != list(
            range(1, len(rows) + 1)
        ):
            _fail(f"{collection_id} positions are not contiguous display order")
        for position, (row, entry) in enumerate(zip(rows, entries), start=1):
            if entry.get("subject_id") != row.subject_id:
                _fail(f"{collection_id} display order changed at {position}")
            if entry.get("rank") != row.rank:
                _fail(f"{collection_id} rank changed for {row.subject_id}")
            subject = by_id.get(row.subject_id)
            if subject is None:
                _fail(f"{collection_id} references missing {row.subject_id}")
            mapping = subject.get("map")
            location = subject.get("location")
            if not isinstance(mapping, dict) or not isinstance(location, dict):
                _fail(f"{row.subject_id} lacks map/location objects")
            if location.get("country_code") != country_code:
                _fail(f"{row.subject_id} has the wrong country")
            if mapping.get("preview_radius_km") != PREVIEW_RADIUS_KM:
                _fail(f"{row.subject_id} must use a 2.0 km campus radius")
            if mapping.get("purpose") != "campus":
                _fail(f"{row.subject_id} must use campus purpose")
    return catalog


def build_catalog(base_catalog_path: Path = BASE_CATALOG) -> dict[str, Any]:
    """Build the complete catalog from pinned, local definitions."""

    base = _read_pinned_base(base_catalog_path)
    base_subjects = {
        subject.get("id"): subject
        for subject in base["subjects"]
        if isinstance(subject, dict)
    }
    new_uk = {campus.subject_id: campus for campus in NEW_UK_CAMPUSES}
    us_campuses = {campus.subject_id: campus for campus in US_CAMPUSES}
    if len(new_uk) != len(NEW_UK_CAMPUSES) or len(us_campuses) != len(US_CAMPUSES):
        _fail("campus definitions repeat a subject ID")

    uk_subjects: list[dict[str, Any]] = []
    for row in UK_RANKING:
        if row.subject_id in new_uk:
            uk_subjects.append(_nominatim_subject(new_uk[row.subject_id], UK_RANKING_URLS))
            continue
        base_subject = base_subjects.get(row.subject_id)
        if not isinstance(base_subject, dict):
            _fail(f"pinned base catalog lacks {row.subject_id}")
        uk_subjects.append(_base_subject(base_subject, row))

    us_subjects: list[dict[str, Any]] = []
    for row in US_RANKING:
        campus = us_campuses.get(row.subject_id)
        if campus is None:
            _fail(f"U.S. ranking lacks campus seed for {row.subject_id}")
        if campus.name != row.ranking_name:
            _fail(f"U.S. ranking/campus name mismatch for {row.subject_id}")
        us_subjects.append(_nominatim_subject(campus, US_RANKING_URLS))

    catalog: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": CATALOG_VERSION,
        "as_of": AS_OF,
        "coordinate_provenance": {
            "note": (
                "Centres identify institution-specific campus products. They are "
                "framing seeds, not surveyed campus boundaries."
            ),
            "base_catalog": {
                "path": "src/city_map_plotter/data/catalog-v1.json",
                "sha256": BASE_CATALOG_SHA256,
                "coordinate_basis": "Wikidata P625 seed points",
            },
            "nominatim": {
                "endpoint": NOMINATIM_ENDPOINT,
                "query_date": NOMINATIM_QUERY_DATE,
                "user_agent": NOMINATIM_USER_AGENT,
                "selection_rule": (
                    "Manually reviewed bounded institution/campus feature, bounded "
                    "main-campus feature, or documented representative anchor at "
                    "the historic/main campus. Each centre is a framing seed, not a "
                    "surveyed campus boundary; the exact role and OSM ref are recorded "
                    "per subject."
                ),
                "copyright_url": OSM_COPYRIGHT_URL,
            },
        },
        "collections": [
            {
                "id": UK_COLLECTION_ID,
                "title": "The Times and The Sunday Times UK top 30 universities 2026",
                "kind": "university",
                "scope": "United Kingdom",
                "as_of": AS_OF,
                "methodology": (
                    "Exact national top 30 from The Times and The Sunday Times Good "
                    "University Guide 2026. Source ranks, ties, and table display order "
                    "are retained; entries are institutions rather than deduplicated cities."
                ),
                "source_urls": list(UK_RANKING_URLS),
                "entries": [
                    _entry(row, position, 2026)
                    for position, row in enumerate(UK_RANKING, start=1)
                ],
                "audit": {
                    "cohort_size": 30,
                    "ranking_publisher": "Times Media Limited",
                    "ranking_edition": 2026,
                    "online_release_date": "2025-09-19",
                    "checked_on": AS_OF,
                    "ties_preserved": True,
                    "access_note": (
                        "The primary Times table is subscriber-gated; the same-day FE "
                        "News national top-30 table supplies the accessible full transcription."
                    ),
                },
            },
            {
                "id": US_COLLECTION_ID,
                "title": "QS World University Rankings 2027 U.S. top 20",
                "kind": "university",
                "scope": "United States",
                "as_of": AS_OF,
                "methodology": (
                    "Official QS World University Rankings 2027 results filtered to "
                    "United States of America. Global ranks, global ties, scores, and QS "
                    "table display order are retained without domestic re-ranking."
                ),
                "source_urls": list(US_RANKING_URLS),
                "entries": [
                    _entry(row, position, 2027)
                    for position, row in enumerate(US_RANKING, start=1)
                ],
                "audit": {
                    "cohort_size": 20,
                    "ranking_publisher": "QS Quacquarelli Symonds",
                    "ranking_edition": 2027,
                    "ranking_release_date": "2026-06-18",
                    "checked_on": AS_OF,
                    "ties_preserved": True,
                    "cutoff_note": (
                        "The twentieth U.S. institution is UT Austin at global rank 72; "
                        "the next U.S. institution is UIUC at global rank 74."
                    ),
                },
            },
        ],
        "subjects": [*uk_subjects, *us_subjects],
    }
    return validate_catalog(catalog)


def serialize_catalog(catalog: dict[str, Any]) -> str:
    return (
        json.dumps(
            validate_catalog(catalog),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    )


def _write_atomic(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(contents)
        temporary = Path(handle.name)
    temporary.chmod(0o644)
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the generated asset is not exactly current",
    )
    parser.add_argument(
        "--base-catalog",
        type=Path,
        default=BASE_CATALOG,
        help="pinned schema-v1 base catalog",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT,
        help="output path (default: packaged ranked-university catalog)",
    )
    args = parser.parse_args(argv)
    try:
        contents = serialize_catalog(build_catalog(args.base_catalog))
        if args.check:
            if (
                not args.output.is_file()
                or args.output.read_text(encoding="utf-8") != contents
            ):
                print(
                    f"{args.output} is stale; rerun "
                    "tools/build_ranked_university_catalog.py",
                    file=sys.stderr,
                )
                return 1
            print(f"{args.output} is current")
            return 0
        _write_atomic(args.output, contents)
    except (CatalogBuildError, OSError) as exc:
        print(f"ranked-university catalog build failed: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Strict reader for Network Rail Working Timetable XLSX bundles.

The public Working Timetable download is a ZIP containing route-book XLSX
files, not a normalised timetable feed.  Each train is a column and each
worksheet is one route-book/day/direction view.  This module therefore keeps
the observed route-book timing sequences separate: joining disjoint slices
would invent an end-to-end path that the source does not explicitly encode.

Only explicitly labelled operators and passenger train classes are selected.
The archive, every outer member, every workbook, and every worksheet actually
used are SHA-256 audited.  Formulae are never evaluated; a cached result may be
read, while a formula without a cached result is treated as a blank cell.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import io
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
from typing import Collection, IO, Iterable, Mapping, MutableMapping
from xml.etree import ElementTree
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from .models import MapPlotterError


SUPPORTED_OPERATOR_NAMES: Mapping[str, str] = {
    "AW": "Transport for Wales Rail",
    "CC": "c2c",
    "CH": "Chiltern Railways",
    "CS": "Caledonian Sleeper",
    "EM": "East Midlands Railway",
    "ES": "Eurostar",
    "GC": "Grand Central",
    "GR": "London North Eastern Railway",
    "GW": "Great Western Railway",
    "GX": "Gatwick Express",
    "HT": "Hull Trains",
    "HX": "Heathrow Express",
    "IL": "Island Line",
    "LD": "Lumo",
    "LE": "Greater Anglia",
    "LM": "West Midlands Trains (legacy feed code)",
    "LN": "London Northwestern Railway",
    "LO": "London Overground",
    "ME": "Merseyrail",
    "NT": "Northern",
    "SE": "Southeastern",
    "SN": "Southern",
    "SR": "ScotRail",
    "SW": "South Western Railway",
    "SX": "Stansted Express",
    "TL": "Thameslink",
    "TP": "TransPennine Express",
    "VT": "Avanti West Coast",
    "WM": "West Midlands Railway",
    "XC": "CrossCountry",
    "XR": "Elizabeth line",
    "GN": "Great Northern",
}
DEFAULT_OPERATOR_CODES = frozenset(SUPPORTED_OPERATOR_NAMES)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CELL_REFERENCE_RE = re.compile(r"([A-Z]+)([1-9][0-9]*)")
_TID_RE = re.compile(r"[0-9][A-Z0-9]{3}")
_UID_RE = re.compile(r"[A-Z0-9]{6}")
_OPERATOR_RE = re.compile(r"[A-Z0-9]{2}")
_DATE_RANGE_RE = re.compile(
    r"([0-9]{2}/[0-9]{2}/[0-9]{4})\s+to\s+"
    r"([0-9]{2}/[0-9]{2}/[0-9]{4})"
)
_ENDPOINT_RE = re.compile(r"(?s)(.+?)\s*\n\s*([^\n]+)\s*")
_DEPOT_RE = re.compile(
    r"(?:\bDEPOT\b|\bT\.?M\.?D\.?\b|\bE\.?M\.?U\.?D\.?\b|"
    r"\bH\s*S\s*T\s*D\b|\bCARRIAGE\s+(?:SIDINGS?|SDGS)\b|"
    r"\bSTABLING\s+(?:SIDINGS?|SDGS)\b)",
    re.IGNORECASE,
)
_PLACEHOLDERS = frozenset({"", "..", "...", "…"})
_PASSENGER_TID_PREFIXES = frozenset({"1", "2"})

_SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_WORKSHEET_REL_TYPE = f"{_OFFICE_REL_NS}/worksheet"
_SHARED_STRINGS_REL_TYPE = f"{_OFFICE_REL_NS}/sharedStrings"
_S = f"{{{_SPREADSHEET_NS}}}"
_P = f"{{{_PACKAGE_REL_NS}}}"
_RID = f"{{{_OFFICE_REL_NS}}}id"

_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_OUTER_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_OUTER_MEMBER_BYTES = 192 * 1024 * 1024
_MAX_WORKBOOK_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_WORKBOOK_PART_BYTES = 192 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

_HEADER_FIELDS = frozenset(
    {
        "TID",
        "UID",
        "Operator",
        "Origin",
        "Destination",
        "Timing Load",
        "Dates of Operation",
        "Running Days",
        "Service Code",
    }
)
_POINT_FIELD_BY_LABEL = {
    "platform": "platform",
    "arr": "arrival",
    "dep": "departure",
    "pass": "pass_time",
    "running line": "running_line",
    "allowance": "allowance",
}


class WttIntegrityError(MapPlotterError):
    """A source byte or ZIP structure did not match the strict source contract."""


class WttFormatError(MapPlotterError):
    """A pinned workbook could not be interpreted without guessing."""


class WttDisagreementError(MapPlotterError):
    """Two duplicate observations of one timing slice disagree."""


@dataclass(frozen=True, slots=True)
class WttArchiveEntryAudit:
    """Hash and central-directory facts for one outer ZIP entry."""

    path: str
    sha256: str
    byte_count: int
    compressed_byte_count: int
    crc32: str
    is_directory: bool


@dataclass(frozen=True, slots=True)
class WttPartAudit:
    """Hash facts for one workbook XML part that was consumed in full."""

    path: str
    sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class WttSheetAudit:
    """Audit facts for one worksheet, including whether its bytes were all read."""

    name: str
    part_path: str
    byte_count_read: int
    fully_read: bool
    sha256: str | None
    selected_column_count: int


@dataclass(frozen=True, slots=True)
class WttWorkbookAudit:
    """Pin and parse facts for one XLSX route book."""

    archive_path: str
    sha256: str
    byte_count: int
    nested_entry_count: int
    nested_manifest_sha256: str
    consumed_parts: tuple[WttPartAudit, ...]
    sheets: tuple[WttSheetAudit, ...]


@dataclass(frozen=True, slots=True)
class WttEndpoint:
    """The verbatim endpoint name and separately presented departure/arrival time."""

    name: str
    time: str | None
    raw: str


@dataclass(frozen=True, slots=True)
class WttTimingPoint:
    """One timing point exactly as presented in a route-book train column."""

    location: str
    platform: str | None = None
    arrival: str | None = None
    departure: str | None = None
    pass_time: str | None = None
    running_line: str | None = None
    allowance: str | None = None

    def timing_identity(self) -> tuple[str, str | None, str | None, str | None]:
        """Identity used to compare duplicate route-book observations."""

        return (self.location, self.arrival, self.departure, self.pass_time)


@dataclass(frozen=True, slots=True)
class WttColumnProvenance:
    """Exact workbook, worksheet and source column for one observation."""

    workbook_path: str
    workbook_sha256: str
    route_book: str
    sheet_name: str
    sheet_part: str
    sheet_sha256: str
    column: str


@dataclass(frozen=True, slots=True)
class WttRouteSlice:
    """A deduplicated route-book timing sequence and all identical observations."""

    timing_points: tuple[WttTimingPoint, ...]
    provenance: tuple[WttColumnProvenance, ...]


@dataclass(frozen=True, slots=True)
class WttScheduleRecord:
    """One passenger schedule header with non-invented route-book slices."""

    uid: str
    tid: str
    operator_code: str
    operator_name: str
    origin: WttEndpoint
    destination: WttEndpoint
    start_date: date
    end_date: date
    running_days: str
    timing_load: str | None
    service_code: str | None
    route_slices: tuple[WttRouteSlice, ...]


@dataclass(frozen=True, slots=True)
class WttArchiveAudit:
    """Immutable parse/audit summary for the complete source archive."""

    archive_path: Path
    archive_sha256: str
    archive_byte_count: int
    entries: tuple[WttArchiveEntryAudit, ...]
    workbooks: tuple[WttWorkbookAudit, ...]
    workbook_count: int
    worksheet_count: int
    selected_column_appearances: int
    schedule_count: int
    route_slice_count: int
    formula_cells_with_cache: int
    formula_cells_without_cache: int
    excluded_by_reason: tuple[tuple[str, int], ...]
    operator_appearances: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class WttArchive:
    """Passenger schedule records and the source evidence that produced them."""

    schedules: tuple[WttScheduleRecord, ...]
    audit: WttArchiveAudit


@dataclass(frozen=True, slots=True)
class _ScheduleHeader:
    uid: str
    tid: str
    operator_code: str
    origin: WttEndpoint
    destination: WttEndpoint
    start_date: date
    end_date: date
    running_days: str
    timing_load: str | None
    service_code: str | None

    def key(self) -> tuple[object, ...]:
        # Service code and presented endpoints can legitimately vary between
        # WTT route-book sections, so they remain part of the precise identity.
        return (
            self.operator_code,
            self.uid,
            self.tid,
            self.origin.raw,
            self.destination.raw,
            self.start_date,
            self.end_date,
            self.running_days,
            self.timing_load,
            self.service_code,
        )


@dataclass(slots=True)
class _ColumnState:
    header: _ScheduleHeader
    column_number: int
    points: list[WttTimingPoint]


@dataclass(slots=True)
class _SliceObservation:
    points: tuple[WttTimingPoint, ...]
    provenance: list[WttColumnProvenance]


@dataclass(slots=True)
class _ScheduleBuilder:
    header: _ScheduleHeader
    slices: MutableMapping[
        tuple[tuple[str, str | None, str | None, str | None], ...],
        _SliceObservation,
    ]


class _HashingXmlReader:
    """File-like wrapper that hashes bytes and rejects DTD/entity declarations."""

    def __init__(self, stream: IO[bytes], *, source: str) -> None:
        self._stream = stream
        self._source = source
        self._digest = hashlib.sha256()
        self._tail = b""
        self.byte_count = 0
        self.fully_read = False

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        if not data:
            self.fully_read = True
            return data
        probe = (self._tail + data).upper()
        if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
            raise WttFormatError(
                f"Workbook XML part {self._source!r} contains a forbidden DTD/entity."
            )
        self._tail = probe[-16:]
        self._digest.update(data)
        self.byte_count += len(data)
        return data

    def hexdigest(self) -> str | None:
        return self._digest.hexdigest() if self.fully_read else None


def _safe_zip_path(path: str, *, source: str) -> None:
    candidate = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise WttIntegrityError(f"Unsafe ZIP member path {path!r} in {source}.")


def _validate_zip_info(info: ZipInfo, *, source: str, max_bytes: int) -> None:
    _safe_zip_path(info.filename, source=source)
    if info.flag_bits & 0x1:
        raise WttIntegrityError(
            f"Encrypted ZIP member {info.filename!r} is forbidden in {source}."
        )
    if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
        raise WttIntegrityError(
            f"Unsupported compression for {info.filename!r} in {source}."
        )
    unix_mode = info.external_attr >> 16
    if unix_mode and stat.S_ISLNK(unix_mode):
        raise WttIntegrityError(
            f"Symbolic-link ZIP member {info.filename!r} is forbidden in {source}."
        )
    if info.file_size < 0 or info.file_size > max_bytes:
        raise WttIntegrityError(
            f"ZIP member {info.filename!r} in {source} has unsafe size "
            f"{info.file_size}."
        )


def _unique_infos(
    archive: ZipFile,
    *,
    source: str,
    max_member_bytes: int,
    max_total_bytes: int,
) -> tuple[ZipInfo, ...]:
    infos = tuple(archive.infolist())
    seen: set[str] = set()
    total = 0
    for info in infos:
        _validate_zip_info(info, source=source, max_bytes=max_member_bytes)
        if info.filename in seen:
            raise WttIntegrityError(
                f"ZIP archive {source} repeats member name {info.filename!r}."
            )
        seen.add(info.filename)
        total += info.file_size
        if total > max_total_bytes:
            raise WttIntegrityError(
                f"ZIP archive {source} expands beyond the safe "
                f"{max_total_bytes}-byte limit."
            )
    return infos


def _read_and_hash_member(archive: ZipFile, info: ZipInfo) -> tuple[bytes, str]:
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    with archive.open(info) as stream:
        while chunk := stream.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
            chunks.append(chunk)
    payload = b"".join(chunks)
    if len(payload) != info.file_size:
        raise WttIntegrityError(
            f"ZIP member {info.filename!r} produced {len(payload)} bytes, "
            f"not its declared {info.file_size}."
        )
    return payload, digest.hexdigest()


def _xml_root(payload: bytes, *, source: str) -> ElementTree.Element:
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise WttFormatError(f"Workbook XML part {source!r} contains a DTD/entity.")
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise WttFormatError(f"Malformed workbook XML part {source!r}: {exc}") from exc


def _resolve_relationship_target(target: str, *, source: str) -> str:
    if "\\" in target:
        raise WttFormatError(f"Relationship target {target!r} in {source} is unsafe.")
    if target.startswith("/"):
        result = target.lstrip("/")
    else:
        result = posixpath.normpath(posixpath.join("xl", target))
    _safe_zip_path(result, source=source)
    return result


def _relationship_parts(
    workbook_payload: bytes,
    relationships_payload: bytes,
    *,
    source: str,
) -> tuple[tuple[tuple[str, str], ...], str]:
    workbook = _xml_root(workbook_payload, source=f"{source}:xl/workbook.xml")
    relationships = _xml_root(
        relationships_payload, source=f"{source}:xl/_rels/workbook.xml.rels"
    )
    rels: dict[str, tuple[str, str]] = {}
    for relationship in relationships.findall(f"{_P}Relationship"):
        rel_id = relationship.get("Id")
        rel_type = relationship.get("Type")
        target = relationship.get("Target")
        if not rel_id or not rel_type or not target:
            raise WttFormatError(f"Incomplete workbook relationship in {source}.")
        if rel_id in rels:
            raise WttFormatError(f"Duplicate relationship ID {rel_id!r} in {source}.")
        rels[rel_id] = (
            rel_type,
            _resolve_relationship_target(target, source=source),
        )

    sheets_element = workbook.find(f"{_S}sheets")
    if sheets_element is None:
        raise WttFormatError(f"Workbook {source} has no sheets element.")
    sheets: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    for sheet in sheets_element.findall(f"{_S}sheet"):
        name = (sheet.get("name") or "").strip()
        rel_id = sheet.get(_RID)
        if not name or not rel_id or rel_id not in rels:
            raise WttFormatError(f"Workbook {source} has an unresolved worksheet.")
        if name in seen_names:
            raise WttFormatError(f"Workbook {source} repeats sheet name {name!r}.")
        rel_type, part = rels[rel_id]
        if rel_type != _WORKSHEET_REL_TYPE:
            raise WttFormatError(
                f"Workbook relationship {rel_id!r} for {name!r} is not a worksheet."
            )
        seen_names.add(name)
        sheets.append((name, part))

    shared_parts = [
        part for kind, part in rels.values() if kind == _SHARED_STRINGS_REL_TYPE
    ]
    if len(shared_parts) > 1:
        raise WttFormatError(f"Workbook {source} has multiple shared-string parts.")
    shared_part = shared_parts[0] if shared_parts else ""
    return tuple(sheets), shared_part


def _shared_strings(payload: bytes, *, source: str) -> tuple[str, ...]:
    root = _xml_root(payload, source=source)
    if root.tag != f"{_S}sst":
        raise WttFormatError(f"Workbook part {source!r} is not a shared-string table.")
    return tuple(
        "".join(text.text or "" for text in item.iter(f"{_S}t"))
        for item in root.findall(f"{_S}si")
    )


def _column_number(reference: str, *, source: str) -> int:
    match = _CELL_REFERENCE_RE.fullmatch(reference)
    if match is None:
        raise WttFormatError(f"Invalid cell reference {reference!r} in {source}.")
    result = 0
    for character in match.group(1):
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _column_name(number: int) -> str:
    if number < 1:
        raise ValueError("Column numbers are one based.")
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _cell_text(
    cell: ElementTree.Element,
    *,
    shared_strings: tuple[str, ...],
    source: str,
    counters: Counter[str],
) -> str:
    formula = cell.find(f"{_S}f")
    value = cell.find(f"{_S}v")
    if formula is not None:
        if value is None or value.text is None:
            counters["formula-without-cache"] += 1
            return ""
        counters["formula-with-cache"] += 1

    cell_type = cell.get("t", "n")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.iter(f"{_S}t"))
    if value is None or value.text is None:
        return ""
    raw = value.text
    if cell_type == "s":
        try:
            index = int(raw)
        except ValueError as exc:
            raise WttFormatError(
                f"Shared-string cell in {source} has non-integer index {raw!r}."
            ) from exc
        if not 0 <= index < len(shared_strings):
            raise WttFormatError(
                f"Shared-string cell in {source} references missing index {index}."
            )
        return shared_strings[index]
    if cell_type == "b":
        if raw not in {"0", "1"}:
            raise WttFormatError(f"Boolean cell in {source} has value {raw!r}.")
        return "TRUE" if raw == "1" else "FALSE"
    return raw


def _row_values(
    row: ElementTree.Element,
    *,
    shared_strings: tuple[str, ...],
    source: str,
    counters: Counter[str],
) -> dict[int, str]:
    result: dict[int, str] = {}
    for cell in row.findall(f"{_S}c"):
        reference = cell.get("r")
        if not reference:
            raise WttFormatError(f"Cell without a reference in {source}.")
        column = _column_number(reference, source=source)
        if column in result:
            raise WttFormatError(
                f"Row {row.get('r', '?')} in {source} repeats column {column}."
            )
        result[column] = _cell_text(
            cell,
            shared_strings=shared_strings,
            source=source,
            counters=counters,
        ).strip()
    return result


def _optional_value(value: str) -> str | None:
    stripped = value.strip()
    return None if stripped in _PLACEHOLDERS else stripped


def _parse_endpoint(value: str, *, field: str, source: str) -> WttEndpoint:
    raw = value.strip()
    if not raw:
        raise WttFormatError(f"Selected train in {source} has blank {field}.")
    match = _ENDPOINT_RE.fullmatch(raw)
    if match is None:
        return WttEndpoint(name=raw, time=None, raw=raw)
    return WttEndpoint(
        name=" ".join(match.group(1).split()),
        time=match.group(2).strip() or None,
        raw=raw,
    )


def _parse_dates(value: str, *, source: str) -> tuple[date, date]:
    match = _DATE_RANGE_RE.fullmatch(value.strip())
    if match is None:
        raise WttFormatError(
            f"Selected train in {source} has unsupported date range {value!r}."
        )
    try:
        start = datetime.strptime(match.group(1), "%d/%m/%Y").date()
        end = datetime.strptime(match.group(2), "%d/%m/%Y").date()
    except ValueError as exc:
        raise WttFormatError(
            f"Selected train in {source} has invalid date range {value!r}."
        ) from exc
    if end < start:
        raise WttFormatError(
            f"Selected train in {source} has reversed date range {value!r}."
        )
    return start, end


def _exclusion_reason(tid: str, origin: str, destination: str) -> str | None:
    if not tid:
        return "blank-tid"
    prefix = tid[0]
    if prefix == "5":
        return "empty-coaching-stock-class"
    if prefix in {"4", "6", "7"}:
        return "freight-train-class"
    if prefix not in _PASSENGER_TID_PREFIXES:
        return "unsupported-train-class"
    if _DEPOT_RE.search(origin) or _DEPOT_RE.search(destination):
        return "depot-endpoint"
    return None


def _make_header(
    headers: Mapping[str, Mapping[int, str]],
    *,
    column: int,
    source: str,
) -> _ScheduleHeader:
    def required(label: str) -> str:
        value = headers.get(label, {}).get(column, "").strip()
        if not value:
            raise WttFormatError(
                f"Selected column {_column_name(column)} in {source} has no {label}."
            )
        return value

    tid = required("TID")
    uid = required("UID")
    operator = required("Operator")
    if _TID_RE.fullmatch(tid) is None:
        raise WttFormatError(f"Selected train in {source} has invalid TID {tid!r}.")
    if _UID_RE.fullmatch(uid) is None:
        raise WttFormatError(f"Selected train in {source} has invalid UID {uid!r}.")
    if _OPERATOR_RE.fullmatch(operator) is None:
        raise WttFormatError(
            f"Selected train in {source} has invalid operator {operator!r}."
        )
    start, end = _parse_dates(required("Dates of Operation"), source=source)
    return _ScheduleHeader(
        uid=uid,
        tid=tid,
        operator_code=operator,
        origin=_parse_endpoint(required("Origin"), field="origin", source=source),
        destination=_parse_endpoint(
            required("Destination"), field="destination", source=source
        ),
        start_date=start,
        end_date=end,
        running_days=required("Running Days"),
        timing_load=_optional_value(headers.get("Timing Load", {}).get(column, "")),
        service_code=_optional_value(headers.get("Service Code", {}).get(column, "")),
    )


def _finalise_location(
    *,
    location: str | None,
    values: Mapping[int, Mapping[str, str]],
    columns: Mapping[int, _ColumnState],
) -> None:
    if not location:
        return
    for column_number, state in columns.items():
        observed = values.get(column_number, {})
        fields = {
            field: _optional_value(observed.get(field, ""))
            for field in _POINT_FIELD_BY_LABEL.values()
        }
        if not any(fields.values()):
            continue
        state.points.append(WttTimingPoint(location=location, **fields))


def _parse_sheet(
    stream: IO[bytes],
    *,
    source: str,
    shared_strings: tuple[str, ...],
    operator_codes: frozenset[str],
    counters: Counter[str],
) -> tuple[dict[int, _ColumnState], _HashingXmlReader]:
    reader = _HashingXmlReader(stream, source=source)
    headers: dict[str, dict[int, str]] = {}
    columns: dict[int, _ColumnState] = {}
    current_location: str | None = None
    current_values: dict[int, dict[str, str]] = {}
    header_complete = False
    seen_root = False

    try:
        parser = ElementTree.iterparse(reader, events=("start", "end"))
        for event, element in parser:
            if event == "start" and not seen_root:
                seen_root = True
                if element.tag != f"{_S}worksheet":
                    raise WttFormatError(f"Part {source!r} is not a worksheet.")
            if event != "end" or element.tag != f"{_S}row":
                continue
            row = _row_values(
                element,
                shared_strings=shared_strings,
                source=source,
                counters=counters,
            )
            location = row.get(1, "").strip()
            label = row.get(2, "").strip()

            if not header_complete and not location:
                if label:
                    if label in headers:
                        raise WttFormatError(
                            f"Worksheet {source} repeats header label {label!r}."
                        )
                    headers[label] = row
                element.clear()
                continue

            if not header_complete:
                header_complete = True
                operator_row = headers.get("Operator")
                if operator_row is None:
                    counters["sheets-without-operator"] += 1
                    break
                missing = sorted(_HEADER_FIELDS - set(headers))
                if missing:
                    raise WttFormatError(
                        f"Worksheet {source} is missing train headers: "
                        + ", ".join(missing)
                        + "."
                    )
                for column_number, operator in sorted(operator_row.items()):
                    operator = operator.strip()
                    if column_number < 3 or operator not in operator_codes:
                        continue
                    counters[f"operator:{operator}"] += 1
                    tid = headers["TID"].get(column_number, "").strip()
                    origin = headers["Origin"].get(column_number, "")
                    destination = headers["Destination"].get(column_number, "")
                    reason = _exclusion_reason(tid, origin, destination)
                    if reason is not None:
                        counters[f"excluded:{reason}"] += 1
                        continue
                    header = _make_header(
                        headers,
                        column=column_number,
                        source=source,
                    )
                    columns[column_number] = _ColumnState(
                        header=header,
                        column_number=column_number,
                        points=[],
                    )
                    counters["selected-column-appearances"] += 1
                if not columns:
                    break

            if location:
                _finalise_location(
                    location=current_location,
                    values=current_values,
                    columns=columns,
                )
                current_location = " ".join(location.split())
                current_values = {}
            normalised_label = label.casefold()
            point_field = _POINT_FIELD_BY_LABEL.get(normalised_label)
            if point_field is not None:
                for column_number in columns:
                    value = row.get(column_number, "")
                    if value:
                        current_values.setdefault(column_number, {})[point_field] = (
                            value
                        )
            element.clear()
        else:
            if not header_complete:
                raise WttFormatError(f"Worksheet {source} has no timing-point rows.")
            _finalise_location(
                location=current_location,
                values=current_values,
                columns=columns,
            )
    except ElementTree.ParseError as exc:
        raise WttFormatError(f"Malformed worksheet XML {source!r}: {exc}") from exc
    return columns, reader


def _nested_manifest(infos: Iterable[ZipInfo]) -> str:
    digest = hashlib.sha256()
    for info in sorted(infos, key=lambda item: item.filename):
        digest.update(info.filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(info.file_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(info.compress_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(f"{info.CRC:08x}".encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _route_book(path: str) -> str:
    stem = PurePosixPath(path).stem
    candidate = stem.split(" - ", 1)[0].strip()
    return candidate or stem


def _add_observation(
    builders: MutableMapping[tuple[object, ...], _ScheduleBuilder],
    *,
    header: _ScheduleHeader,
    points: tuple[WttTimingPoint, ...],
    provenance: WttColumnProvenance,
) -> None:
    key = header.key()
    builder = builders.get(key)
    if builder is None:
        builder = _ScheduleBuilder(header=header, slices={})
        builders[key] = builder
    elif builder.header != header:
        raise WttDisagreementError(
            f"Duplicate WTT schedule key for {header.uid} has conflicting headers."
        )

    timing_identity = tuple(point.timing_identity() for point in points)
    prior = builder.slices.get(timing_identity)
    if prior is None:
        builder.slices[timing_identity] = _SliceObservation(
            points=points,
            provenance=[provenance],
        )
        return
    if prior.points != points:
        first = prior.provenance[0]
        raise WttDisagreementError(
            "Duplicate WTT timing slice disagrees on platform, running line, or "
            f"allowance for {header.operator_code} {header.uid}/{header.tid}: "
            f"{first.workbook_path} {first.sheet_name} {first.column} versus "
            f"{provenance.workbook_path} {provenance.sheet_name} "
            f"{provenance.column}."
        )
    prior.provenance.append(provenance)


def _parse_workbook(
    payload: bytes,
    *,
    archive_path: str,
    workbook_sha256: str,
    operator_codes: frozenset[str],
    counters: Counter[str],
    builders: MutableMapping[tuple[object, ...], _ScheduleBuilder],
) -> WttWorkbookAudit:
    try:
        workbook = ZipFile(io.BytesIO(payload))
    except BadZipFile as exc:
        raise WttFormatError(
            f"XLSX member {archive_path!r} is not a ZIP file."
        ) from exc
    with workbook:
        infos = _unique_infos(
            workbook,
            source=archive_path,
            max_member_bytes=_MAX_WORKBOOK_PART_BYTES,
            max_total_bytes=_MAX_WORKBOOK_UNCOMPRESSED_BYTES,
        )
        info_by_name = {info.filename: info for info in infos}
        required_parts = {"xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
        missing = sorted(required_parts - set(info_by_name))
        if missing:
            raise WttFormatError(
                f"XLSX member {archive_path!r} is missing: {', '.join(missing)}."
            )

        consumed: list[WttPartAudit] = []

        def read_part(path: str) -> bytes:
            info = info_by_name.get(path)
            if info is None:
                raise WttFormatError(
                    f"XLSX member {archive_path!r} has no required part {path!r}."
                )
            data, digest = _read_and_hash_member(workbook, info)
            consumed.append(
                WttPartAudit(path=path, sha256=digest, byte_count=len(data))
            )
            return data

        workbook_xml = read_part("xl/workbook.xml")
        relationships_xml = read_part("xl/_rels/workbook.xml.rels")
        sheets, shared_path = _relationship_parts(
            workbook_xml,
            relationships_xml,
            source=archive_path,
        )
        shared = (
            _shared_strings(
                read_part(shared_path),
                source=f"{archive_path}:{shared_path}",
            )
            if shared_path
            else ()
        )

        sheet_audits: list[WttSheetAudit] = []
        for sheet_name, sheet_part in sheets:
            info = info_by_name.get(sheet_part)
            if info is None:
                raise WttFormatError(
                    f"Workbook {archive_path!r} references absent sheet {sheet_part!r}."
                )
            source = f"{archive_path}:{sheet_name}"
            with workbook.open(info) as stream:
                columns, reader = _parse_sheet(
                    stream,
                    source=source,
                    shared_strings=shared,
                    operator_codes=operator_codes,
                    counters=counters,
                )
            sheet_digest = reader.hexdigest()
            sheet_audits.append(
                WttSheetAudit(
                    name=sheet_name,
                    part_path=sheet_part,
                    byte_count_read=reader.byte_count,
                    fully_read=reader.fully_read,
                    sha256=sheet_digest,
                    selected_column_count=len(columns),
                )
            )
            counters["worksheet-count"] += 1
            if not columns:
                continue
            if not reader.fully_read or sheet_digest is None:
                raise WttIntegrityError(
                    f"Selected worksheet {source!r} was not read and hashed in full."
                )
            for state in columns.values():
                if not state.points:
                    counters["excluded:no-timing-points"] += 1
                    continue
                _add_observation(
                    builders,
                    header=state.header,
                    points=tuple(state.points),
                    provenance=WttColumnProvenance(
                        workbook_path=archive_path,
                        workbook_sha256=workbook_sha256,
                        route_book=_route_book(archive_path),
                        sheet_name=sheet_name,
                        sheet_part=sheet_part,
                        sheet_sha256=sheet_digest,
                        column=_column_name(state.column_number),
                    ),
                )

    return WttWorkbookAudit(
        archive_path=archive_path,
        sha256=workbook_sha256,
        byte_count=len(payload),
        nested_entry_count=len(infos),
        nested_manifest_sha256=_nested_manifest(infos),
        consumed_parts=tuple(consumed),
        sheets=tuple(sheet_audits),
    )


def _freeze_schedules(
    builders: Mapping[tuple[object, ...], _ScheduleBuilder],
) -> tuple[WttScheduleRecord, ...]:
    records: list[WttScheduleRecord] = []
    for key in sorted(builders, key=lambda item: tuple(str(value) for value in item)):
        builder = builders[key]
        slices: list[WttRouteSlice] = []
        for timing_identity in sorted(
            builder.slices,
            key=lambda sequence: tuple(
                str(value) for point in sequence for value in point
            ),
        ):
            observation = builder.slices[timing_identity]
            provenance = tuple(
                sorted(
                    observation.provenance,
                    key=lambda item: (
                        item.workbook_path,
                        item.sheet_name,
                        item.column,
                    ),
                )
            )
            slices.append(
                WttRouteSlice(
                    timing_points=observation.points,
                    provenance=provenance,
                )
            )
        header = builder.header
        records.append(
            WttScheduleRecord(
                uid=header.uid,
                tid=header.tid,
                operator_code=header.operator_code,
                operator_name=SUPPORTED_OPERATOR_NAMES[header.operator_code],
                origin=header.origin,
                destination=header.destination,
                start_date=header.start_date,
                end_date=header.end_date,
                running_days=header.running_days,
                timing_load=header.timing_load,
                service_code=header.service_code,
                route_slices=tuple(slices),
            )
        )
    return tuple(records)


def parse_wtt_archive(
    path: Path,
    *,
    expected_sha256: str,
    operator_codes: Collection[str] = DEFAULT_OPERATOR_CODES,
) -> WttArchive:
    """Parse a hash-pinned Network Rail WTT XLSX ZIP, failing closed.

    ``expected_sha256`` is mandatory because URLs and filenames do not identify
    immutable WTT bytes.  Operator selection also fails closed: only columns
    carrying an explicit supported operator code can be returned.  TIDs in
    classes 1 and 2 are retained; explicit empty-stock, freight, unsupported
    class and depot-endpoint evidence is counted but excluded.
    """

    path = Path(path)
    if _SHA256_RE.fullmatch(expected_sha256) is None:
        raise WttIntegrityError("WTT expected_sha256 must be one lowercase SHA-256.")
    requested_codes = frozenset(operator_codes)
    unsupported = sorted(requested_codes - DEFAULT_OPERATOR_CODES)
    if unsupported:
        raise WttFormatError(
            "Unsupported WTT operator codes requested: " + ", ".join(unsupported) + "."
        )
    if not requested_codes:
        raise WttFormatError("At least one supported WTT operator code is required.")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise WttIntegrityError(f"Cannot read WTT archive {path}: {exc}") from exc
    if not payload:
        raise WttIntegrityError(f"WTT archive {path} is empty.")
    if len(payload) > _MAX_ARCHIVE_BYTES:
        raise WttIntegrityError(
            f"WTT archive {path} exceeds the {_MAX_ARCHIVE_BYTES}-byte safety limit."
        )
    archive_sha256 = hashlib.sha256(payload).hexdigest()
    if archive_sha256 != expected_sha256:
        raise WttIntegrityError(
            f"WTT archive {path} does not match its SHA-256: expected "
            f"{expected_sha256}, got {archive_sha256}."
        )

    try:
        outer = ZipFile(io.BytesIO(payload))
    except BadZipFile as exc:
        raise WttIntegrityError(
            f"Pinned WTT archive {path} is not a ZIP file."
        ) from exc

    counters: Counter[str] = Counter()
    entries: list[WttArchiveEntryAudit] = []
    workbook_audits: list[WttWorkbookAudit] = []
    builders: dict[tuple[object, ...], _ScheduleBuilder] = {}
    with outer:
        infos = _unique_infos(
            outer,
            source=str(path),
            max_member_bytes=_MAX_OUTER_MEMBER_BYTES,
            max_total_bytes=_MAX_OUTER_UNCOMPRESSED_BYTES,
        )
        for info in infos:
            member, digest = _read_and_hash_member(outer, info)
            entries.append(
                WttArchiveEntryAudit(
                    path=info.filename,
                    sha256=digest,
                    byte_count=len(member),
                    compressed_byte_count=info.compress_size,
                    crc32=f"{info.CRC:08x}",
                    is_directory=info.is_dir(),
                )
            )
            if (
                info.is_dir()
                or info.filename.startswith("__MACOSX/")
                or not info.filename.casefold().endswith(".xlsx")
            ):
                continue
            workbook_audits.append(
                _parse_workbook(
                    member,
                    archive_path=info.filename,
                    workbook_sha256=digest,
                    operator_codes=requested_codes,
                    counters=counters,
                    builders=builders,
                )
            )
            counters["workbook-count"] += 1

    schedules = _freeze_schedules(builders)
    route_slice_count = sum(len(record.route_slices) for record in schedules)
    return WttArchive(
        schedules=schedules,
        audit=WttArchiveAudit(
            archive_path=path.resolve(),
            archive_sha256=archive_sha256,
            archive_byte_count=len(payload),
            entries=tuple(entries),
            workbooks=tuple(workbook_audits),
            workbook_count=counters["workbook-count"],
            worksheet_count=counters["worksheet-count"],
            selected_column_appearances=counters["selected-column-appearances"],
            schedule_count=len(schedules),
            route_slice_count=route_slice_count,
            formula_cells_with_cache=counters["formula-with-cache"],
            formula_cells_without_cache=counters["formula-without-cache"],
            excluded_by_reason=tuple(
                sorted(
                    (key.removeprefix("excluded:"), value)
                    for key, value in counters.items()
                    if key.startswith("excluded:")
                )
            ),
            operator_appearances=tuple(
                sorted(
                    (key.removeprefix("operator:"), value)
                    for key, value in counters.items()
                    if key.startswith("operator:")
                )
            ),
        ),
    )

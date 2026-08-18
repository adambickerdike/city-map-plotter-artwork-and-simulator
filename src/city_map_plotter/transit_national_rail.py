"""Fail-closed foundations for compiling National Rail passenger networks.

This module deliberately stops before geographic map matching.  It validates a
local, hash-pinned source pack and turns the three factual inputs needed by the
future alignment compiler into deterministic Python records:

* an RDG CIF ``.MCA`` full timetable extract;
* its Master Station Names ``.MSN`` file; and
* a DfT NaPTAN access-node CSV snapshot.

PDF route diagrams are not inputs.  Rendering must also remain offline: callers
acquire and review the source bytes first, pin them in a source-pack manifest,
then call the functions below.  Missing files, missing pins, ambiguous schedule
precedence and malformed fixed-width records all fail closed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import io
import json
from math import hypot, isfinite
from pathlib import Path
import re
from typing import Any, Collection, Iterable, Mapping, Sequence

from .models import MapPlotterError


SOURCE_PACK_SCHEMA_VERSION = 1
REQUIRED_SOURCE_ROLES = frozenset({"cif_mca", "msn", "naptan_csv"})
SUPPORTED_ATOC_CODES = frozenset({"GR", "GW", "NT", "SN"})
ATOC_OPERATOR_NAMES: Mapping[str, str] = {
    "GR": "London North Eastern Railway",
    "GW": "Great Western Railway",
    "NT": "Northern",
    "SN": "Southern",
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ROLE_RE = re.compile(r"[a-z][a-z0-9_]*")
_TIPLOC_RE = re.compile(r"[A-Z0-9]{4,7}")
_CRS_RE = re.compile(r"[A-Z0-9]{3}")
_ATOC_RE = re.compile(r"[A-Z0-9]{2}")
_PASSENGER_TRAIN_STATUSES = frozenset({"P", "1"})
_BUS_TRAIN_STATUSES = frozenset({"B", "5"})
_BUS_TRAIN_CATEGORIES = frozenset({"BR", "BS"})
# Network Rail CIF can contain these empty-coaching-stock categories.  The RDG
# DTD feed normally omits them, but an explicit deny-list prevents a future
# Network Rail source from silently becoming passenger service.
_EMPTY_COACHING_STOCK_CATEGORIES = frozenset({"EE", "EL", "ES", "JJ"})
_STP_PRECEDENCE = {"P": 0, "N": 1, "O": 2, "C": 3}
_TOP_LEVEL_IGNORED_CIF_RECORDS = frozenset({"AA", "TA", "TD", "TI"})


def _sha256_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                byte_count += len(chunk)
    except OSError as exc:
        raise MapPlotterError(f"Cannot read pinned National Rail source {path}: {exc}") from exc
    return digest.hexdigest(), byte_count


def _json_without_duplicate_keys(payload: bytes, *, source: Path) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MapPlotterError(
                    f"National Rail source-pack manifest repeats key {key!r}."
                )
            result[key] = value
        return result

    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=object_pairs)
    except MapPlotterError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MapPlotterError(
            f"National Rail source-pack manifest {source} is invalid JSON: {exc}"
        ) from exc


@dataclass(frozen=True, slots=True)
class PinnedSource:
    """One verified source file in a self-contained local source pack."""

    role: str
    path: Path
    sha256: str
    byte_count: int

    def read_bytes(self) -> bytes:
        """Read the source and recheck its pin to close the validation/read gap."""

        actual_sha256, byte_count = _sha256_path(self.path)
        if actual_sha256 != self.sha256 or byte_count != self.byte_count:
            raise MapPlotterError(
                f"Pinned National Rail source {self.role!r} changed after validation: "
                f"expected {self.sha256}/{self.byte_count} bytes, got "
                f"{actual_sha256}/{byte_count} bytes."
            )
        try:
            return self.path.read_bytes()
        except OSError as exc:
            raise MapPlotterError(
                f"Cannot read pinned National Rail source {self.path}: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class NationalRailSourcePack:
    """A validated manifest and its hash-pinned local source files."""

    manifest_path: Path
    manifest_sha256: str
    sources: tuple[PinnedSource, ...]

    def source(self, role: str) -> PinnedSource:
        for item in self.sources:
            if item.role == role:
                return item
        raise MapPlotterError(
            f"National Rail source pack has no required source role {role!r}."
        )

    def read_bytes(self, role: str) -> bytes:
        return self.source(role).read_bytes()


def load_national_rail_source_pack(manifest_path: Path) -> NationalRailSourcePack:
    """Validate a strict local source-pack manifest and every SHA-256 pin.

    Version 1 has the following form::

        {
          "schema_version": 1,
          "sources": {
            "cif_mca": {"path": "RJT....MCA", "sha256": "..."},
            "msn": {"path": "RJT....MSN", "sha256": "..."},
            "naptan_csv": {"path": "NaPTAN.csv", "sha256": "..."}
          }
        }

    Additional source roles may be included for the later TPS/OS alignment
    stage, but every entry must use the same exact ``path``/``sha256`` shape.
    Paths must be relative and remain inside the manifest directory.
    """

    manifest_path = Path(manifest_path)
    try:
        raw_manifest = manifest_path.read_bytes()
    except OSError as exc:
        raise MapPlotterError(
            f"National Rail source-pack manifest is absent or unreadable: "
            f"{manifest_path}: {exc}"
        ) from exc
    document = _json_without_duplicate_keys(raw_manifest, source=manifest_path)
    if not isinstance(document, dict) or set(document) != {"schema_version", "sources"}:
        raise MapPlotterError(
            "National Rail source-pack manifest must contain exactly "
            "schema_version and sources."
        )
    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != SOURCE_PACK_SCHEMA_VERSION:
        raise MapPlotterError(
            f"National Rail source-pack schema_version must be "
            f"{SOURCE_PACK_SCHEMA_VERSION}."
        )
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, dict):
        raise MapPlotterError("National Rail source-pack sources must be an object.")
    missing = sorted(REQUIRED_SOURCE_ROLES - set(raw_sources))
    if missing:
        raise MapPlotterError(
            "National Rail source pack is missing required pinned sources: "
            + ", ".join(missing)
            + "."
        )

    base = manifest_path.parent.resolve()
    sources: list[PinnedSource] = []
    for role in sorted(raw_sources):
        raw_entry = raw_sources[role]
        if not isinstance(role, str) or _ROLE_RE.fullmatch(role) is None:
            raise MapPlotterError(
                f"National Rail source role {role!r} is not a stable identifier."
            )
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"path", "sha256"}:
            raise MapPlotterError(
                f"National Rail source {role!r} must contain exactly path and sha256; "
                "unpinned sources are forbidden."
            )
        relative = raw_entry.get("path")
        expected_sha256 = raw_entry.get("sha256")
        if not isinstance(relative, str) or not relative.strip():
            raise MapPlotterError(
                f"National Rail source {role!r} has no relative file path."
            )
        relative_path = Path(relative)
        if relative_path.is_absolute():
            raise MapPlotterError(
                f"National Rail source {role!r} must use a relative source-pack path."
            )
        if not isinstance(expected_sha256, str) or _SHA256_RE.fullmatch(
            expected_sha256
        ) is None:
            raise MapPlotterError(
                f"National Rail source {role!r} is unpinned: sha256 must be one "
                "lowercase SHA-256 digest."
            )
        path = (base / relative_path).resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise MapPlotterError(
                f"National Rail source {role!r} escapes its source-pack directory."
            ) from exc
        if not path.is_file():
            raise MapPlotterError(
                f"Pinned National Rail source {role!r} is absent: {path}."
            )
        actual_sha256, byte_count = _sha256_path(path)
        if byte_count == 0:
            raise MapPlotterError(
                f"Pinned National Rail source {role!r} is unexpectedly empty."
            )
        if actual_sha256 != expected_sha256:
            raise MapPlotterError(
                f"Pinned National Rail source {role!r} does not match its SHA-256: "
                f"expected {expected_sha256}, got {actual_sha256}."
            )
        sources.append(
            PinnedSource(
                role=role,
                path=path,
                sha256=actual_sha256,
                byte_count=byte_count,
            )
        )
    return NationalRailSourcePack(
        manifest_path=manifest_path.resolve(),
        manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
        sources=tuple(sources),
    )


def _decode_ascii_lines(payload: bytes, *, source_name: str) -> list[str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MapPlotterError(f"{source_name} is not an ASCII fixed-width file.") from exc
    if "\x00" in text:
        raise MapPlotterError(f"{source_name} contains a NUL byte.")
    return text.splitlines()


def _parse_yymmdd(value: str, *, field: str) -> date:
    if re.fullmatch(r"[0-9]{6}", value) is None:
        raise MapPlotterError(f"{field} must be a six-digit yymmdd date.")
    try:
        return datetime.strptime(value, "%y%m%d").date()
    except ValueError as exc:
        raise MapPlotterError(f"{field} is not a valid yymmdd date.") from exc


def _parse_ddmmyy(value: str, *, field: str) -> date:
    if re.fullmatch(r"[0-9]{6}", value) is None:
        raise MapPlotterError(f"{field} must be a six-digit ddmmyy date.")
    try:
        return datetime.strptime(value, "%d%m%y").date()
    except ValueError as exc:
        raise MapPlotterError(f"{field} is not a valid ddmmyy date.") from exc


def _scheduled_time(value: str, *, field: str) -> str | None:
    result = value.strip()
    if not result:
        return None
    if re.fullmatch(r"[0-2][0-9][0-5][0-9]H?", result) is None:
        raise MapPlotterError(f"{field} contains invalid CIF time {result!r}.")
    return result


def _public_time(value: str, *, field: str) -> str | None:
    result = value.strip()
    if not result:
        return None
    if re.fullmatch(r"[0-2][0-9][0-5][0-9]", result) is None:
        raise MapPlotterError(f"{field} contains invalid public time {result!r}.")
    return result


def _activity_codes(value: str) -> tuple[str, ...]:
    return tuple(
        code
        for index in range(0, len(value), 2)
        if (code := value[index : index + 2].strip())
    )


def _location_identity(value: str, *, field: str) -> tuple[str, str | None]:
    tiploc = value[:7].strip()
    suffix = value[7:8].strip() or None
    if _TIPLOC_RE.fullmatch(tiploc) is None:
        raise MapPlotterError(f"{field} contains invalid TIPLOC {tiploc!r}.")
    if suffix is not None and suffix not in set("23456789"):
        raise MapPlotterError(f"{field} contains invalid location suffix {suffix!r}.")
    return tiploc, suffix


@dataclass(frozen=True, slots=True)
class CifHeader:
    source_name: str
    file_identity: str
    extracted_on: date
    update_indicator: str
    extract_start: date
    extract_end: date


@dataclass(frozen=True, slots=True)
class CifLocation:
    kind: str
    tiploc: str
    suffix: str | None
    scheduled_arrival: str | None
    scheduled_departure: str | None
    scheduled_pass: str | None
    public_arrival: str | None
    public_departure: str | None
    activity_codes: tuple[str, ...]
    line_number: int

    @property
    def identity(self) -> str:
        return self.tiploc + (self.suffix or "")


@dataclass(frozen=True, slots=True)
class CifSchedule:
    source_name: str
    line_number: int
    transaction_type: str
    uid: str
    runs_from: date
    runs_to: date
    days_run: tuple[bool, ...]
    bank_holiday_running: str
    train_status: str
    train_category: str
    stp_indicator: str
    atoc_code: str | None
    retail_service_id: str | None
    locations: tuple[CifLocation, ...]

    @property
    def transaction_key(self) -> tuple[str, date, str]:
        # RSPS5046 defines UID/start-date/overlay indicator as the unique key.
        return (self.uid, self.runs_from, self.stp_indicator)

    @property
    def is_cancellation(self) -> bool:
        return self.stp_indicator == "C"

    def runs_on(self, service_date: date) -> bool:
        return (
            self.runs_from <= service_date <= self.runs_to
            and self.days_run[service_date.weekday()]
        )

    def passenger_exclusion_reason(self) -> str | None:
        if self.train_status in _BUS_TRAIN_STATUSES:
            return f"bus-train-status:{self.train_status}"
        if self.train_status not in _PASSENGER_TRAIN_STATUSES:
            return f"non-passenger-train-status:{self.train_status or 'blank'}"
        if self.train_category in _BUS_TRAIN_CATEGORIES:
            return f"bus-train-category:{self.train_category}"
        if self.train_category in _EMPTY_COACHING_STOCK_CATEGORIES:
            return f"empty-coaching-stock:{self.train_category}"
        return None


@dataclass(frozen=True, slots=True)
class CifDocument:
    header: CifHeader
    schedules: tuple[CifSchedule, ...]
    ignored_record_counts: tuple[tuple[str, int], ...]


@dataclass(slots=True)
class _ScheduleBuilder:
    source_name: str
    line_number: int
    transaction_type: str
    uid: str
    runs_from: date
    runs_to: date
    days_run: tuple[bool, ...]
    bank_holiday_running: str
    train_status: str
    train_category: str
    stp_indicator: str
    atoc_code: str | None = None
    retail_service_id: str | None = None
    locations: list[CifLocation] | None = None
    saw_bx: bool = False
    saw_lo: bool = False
    saw_lt: bool = False

    def __post_init__(self) -> None:
        self.locations = []

    def finish(self) -> CifSchedule:
        assert self.locations is not None
        detail_free = self.transaction_type == "D" or self.stp_indicator == "C"
        if detail_free:
            if self.saw_bx or self.saw_lo or self.saw_lt or self.locations:
                raise MapPlotterError(
                    f"{self.source_name}:{self.line_number} deletion/cancellation "
                    "must contain only its BS record."
                )
        elif not (self.saw_bx and self.saw_lo and self.saw_lt):
            raise MapPlotterError(
                f"{self.source_name}:{self.line_number} schedule {self.uid} is "
                "incomplete; BX, LO and LT are mandatory."
            )
        elif len(self.locations) < 2:
            raise MapPlotterError(
                f"{self.source_name}:{self.line_number} schedule {self.uid} has "
                "fewer than two timing locations."
            )
        return CifSchedule(
            source_name=self.source_name,
            line_number=self.line_number,
            transaction_type=self.transaction_type,
            uid=self.uid,
            runs_from=self.runs_from,
            runs_to=self.runs_to,
            days_run=self.days_run,
            bank_holiday_running=self.bank_holiday_running,
            train_status=self.train_status,
            train_category=self.train_category,
            stp_indicator=self.stp_indicator,
            atoc_code=self.atoc_code,
            retail_service_id=self.retail_service_id,
            locations=tuple(self.locations),
        )


def _parse_header(record: str, *, source_name: str) -> CifHeader:
    update_indicator = record[46]
    if update_indicator not in {"F", "U"}:
        raise MapPlotterError(
            f"{source_name} HD update indicator must be F or U, got "
            f"{update_indicator!r}."
        )
    return CifHeader(
        source_name=source_name,
        file_identity=record[2:22].rstrip(),
        extracted_on=_parse_ddmmyy(
            record[22:28], field=f"{source_name} HD extraction date"
        ),
        update_indicator=update_indicator,
        extract_start=_parse_ddmmyy(
            record[48:54], field=f"{source_name} HD extract start"
        ),
        extract_end=_parse_ddmmyy(
            record[54:60], field=f"{source_name} HD extract end"
        ),
    )


def _parse_bs(record: str, *, source_name: str, line_number: int) -> _ScheduleBuilder:
    transaction_type = record[2]
    stp_indicator = record[79]
    if transaction_type not in {"D", "N", "R"}:
        raise MapPlotterError(
            f"{source_name}:{line_number} BS has invalid transaction type "
            f"{transaction_type!r}."
        )
    if stp_indicator not in _STP_PRECEDENCE:
        raise MapPlotterError(
            f"{source_name}:{line_number} BS has invalid STP indicator "
            f"{stp_indicator!r}."
        )
    uid = record[3:9].strip()
    if not uid or re.fullmatch(r"[A-Z0-9]{6}", uid) is None:
        raise MapPlotterError(
            f"{source_name}:{line_number} BS has invalid six-character UID {uid!r}."
        )
    runs_from = _parse_yymmdd(
        record[9:15], field=f"{source_name}:{line_number} BS runs-from"
    )
    runs_to = _parse_yymmdd(
        record[15:21], field=f"{source_name}:{line_number} BS runs-to"
    )
    if runs_from > runs_to:
        raise MapPlotterError(
            f"{source_name}:{line_number} BS runs-from is after runs-to."
        )
    raw_days = record[21:28]
    if re.fullmatch(r"[01]{7}", raw_days) is None or "1" not in raw_days:
        raise MapPlotterError(
            f"{source_name}:{line_number} BS days-run must contain seven 0/1 "
            "flags and at least one running day."
        )
    train_status = record[29].strip()
    train_category = record[30:32].strip()
    if transaction_type != "D" and stp_indicator != "C":
        if not train_status or not train_category:
            raise MapPlotterError(
                f"{source_name}:{line_number} runnable BS needs train status "
                "and category."
            )
    return _ScheduleBuilder(
        source_name=source_name,
        line_number=line_number,
        transaction_type=transaction_type,
        uid=uid,
        runs_from=runs_from,
        runs_to=runs_to,
        days_run=tuple(value == "1" for value in raw_days),
        bank_holiday_running=record[28].strip(),
        train_status=train_status,
        train_category=train_category,
        stp_indicator=stp_indicator,
    )


def _parse_location(record: str, *, source_name: str, line_number: int) -> CifLocation:
    kind = record[:2]
    tiploc, suffix = _location_identity(
        record[2:10], field=f"{source_name}:{line_number} {kind} location"
    )
    if kind == "LO":
        return CifLocation(
            kind=kind,
            tiploc=tiploc,
            suffix=suffix,
            scheduled_arrival=None,
            scheduled_departure=_scheduled_time(
                record[10:15], field=f"{source_name}:{line_number} LO departure"
            ),
            scheduled_pass=None,
            public_arrival=None,
            public_departure=_public_time(
                record[15:19], field=f"{source_name}:{line_number} LO public departure"
            ),
            activity_codes=_activity_codes(record[29:41]),
            line_number=line_number,
        )
    if kind == "LI":
        return CifLocation(
            kind=kind,
            tiploc=tiploc,
            suffix=suffix,
            scheduled_arrival=_scheduled_time(
                record[10:15], field=f"{source_name}:{line_number} LI arrival"
            ),
            scheduled_departure=_scheduled_time(
                record[15:20], field=f"{source_name}:{line_number} LI departure"
            ),
            scheduled_pass=_scheduled_time(
                record[20:25], field=f"{source_name}:{line_number} LI pass"
            ),
            public_arrival=_public_time(
                record[25:29], field=f"{source_name}:{line_number} LI public arrival"
            ),
            public_departure=_public_time(
                record[29:33], field=f"{source_name}:{line_number} LI public departure"
            ),
            activity_codes=_activity_codes(record[42:54]),
            line_number=line_number,
        )
    if kind == "LT":
        return CifLocation(
            kind=kind,
            tiploc=tiploc,
            suffix=suffix,
            scheduled_arrival=_scheduled_time(
                record[10:15], field=f"{source_name}:{line_number} LT arrival"
            ),
            scheduled_departure=None,
            scheduled_pass=None,
            public_arrival=_public_time(
                record[15:19], field=f"{source_name}:{line_number} LT public arrival"
            ),
            public_departure=None,
            activity_codes=_activity_codes(record[25:37]),
            line_number=line_number,
        )
    raise AssertionError(f"Unsupported location kind {kind}")


def parse_cif_mca(payload: bytes, *, source_name: str = "timetable.MCA") -> CifDocument:
    """Parse an RDG fixed-width MCA/CFA timetable document.

    The parser owns schedule records and retains all LO/LI/LT timing locations,
    including non-public pass points.  Association and TIPLOC reference records
    are counted but ignored because their authoritative consumers are separate
    compiler stages.  A CR record is likewise counted while its following LI is
    retained; changes en route never create or remove a physical location.
    """

    header: CifHeader | None = None
    schedules: list[CifSchedule] = []
    ignored: Counter[str] = Counter()
    builder: _ScheduleBuilder | None = None
    saw_trailer = False
    semantic_record_count = 0

    for line_number, record in enumerate(
        _decode_ascii_lines(payload, source_name=source_name), start=1
    ):
        if not record:
            continue
        if record.startswith("/"):
            continue
        semantic_record_count += 1
        if len(record) != 80:
            raise MapPlotterError(
                f"{source_name}:{line_number} CIF record is {len(record)} characters; "
                "exactly 80 are required."
            )
        if any(ord(character) < 32 or ord(character) > 126 for character in record):
            raise MapPlotterError(
                f"{source_name}:{line_number} contains a non-printable CIF character."
            )
        kind = record[:2]
        if saw_trailer:
            raise MapPlotterError(
                f"{source_name}:{line_number} contains a record after the ZZ trailer."
            )
        if kind == "HD":
            if semantic_record_count != 1 or header is not None:
                raise MapPlotterError(f"{source_name} HD must be its first record.")
            header = _parse_header(record, source_name=source_name)
            continue
        if header is None:
            raise MapPlotterError(f"{source_name} has a record before its HD header.")
        if kind == "ZZ":
            if builder is not None:
                schedules.append(builder.finish())
                builder = None
            saw_trailer = True
            continue
        if kind in _TOP_LEVEL_IGNORED_CIF_RECORDS:
            if builder is not None:
                raise MapPlotterError(
                    f"{source_name}:{line_number} {kind} appears inside a schedule."
                )
            ignored[kind] += 1
            continue
        if kind == "BS":
            if builder is not None:
                schedules.append(builder.finish())
            builder = _parse_bs(
                record, source_name=source_name, line_number=line_number
            )
            continue
        if builder is None:
            raise MapPlotterError(
                f"{source_name}:{line_number} {kind} appears outside a BS schedule."
            )
        if builder.transaction_type == "D" or builder.stp_indicator == "C":
            raise MapPlotterError(
                f"{source_name}:{line_number} {kind} follows a detail-free "
                "deletion/cancellation BS."
            )
        assert builder.locations is not None
        if kind == "BX":
            if builder.saw_bx or builder.saw_lo or builder.locations:
                raise MapPlotterError(
                    f"{source_name}:{line_number} BX is duplicated or out of order."
                )
            atoc_code = record[11:13]
            if _ATOC_RE.fullmatch(atoc_code) is None:
                raise MapPlotterError(
                    f"{source_name}:{line_number} BX has invalid ATOC code "
                    f"{atoc_code!r}."
                )
            if record[13] != "Y":
                raise MapPlotterError(
                    f"{source_name}:{line_number} BX applicable-timetable code "
                    "must be Y."
                )
            builder.atoc_code = atoc_code
            builder.retail_service_id = record[14:22].strip() or None
            builder.saw_bx = True
        elif kind == "LO":
            if not builder.saw_bx or builder.saw_lo or builder.locations:
                raise MapPlotterError(
                    f"{source_name}:{line_number} LO is duplicated or out of order."
                )
            builder.locations.append(
                _parse_location(record, source_name=source_name, line_number=line_number)
            )
            builder.saw_lo = True
        elif kind == "LI":
            if not builder.saw_lo or builder.saw_lt:
                raise MapPlotterError(
                    f"{source_name}:{line_number} LI is out of order."
                )
            builder.locations.append(
                _parse_location(record, source_name=source_name, line_number=line_number)
            )
        elif kind == "CR":
            if not builder.saw_lo or builder.saw_lt:
                raise MapPlotterError(
                    f"{source_name}:{line_number} CR is out of order."
                )
            ignored[kind] += 1
        elif kind == "LT":
            if not builder.saw_lo or builder.saw_lt:
                raise MapPlotterError(
                    f"{source_name}:{line_number} LT is duplicated or out of order."
                )
            builder.locations.append(
                _parse_location(record, source_name=source_name, line_number=line_number)
            )
            builder.saw_lt = True
        else:
            raise MapPlotterError(
                f"{source_name}:{line_number} uses unsupported CIF record {kind!r}."
            )

    if header is None:
        raise MapPlotterError(f"{source_name} has no HD header.")
    if not saw_trailer:
        raise MapPlotterError(f"{source_name} is truncated: no ZZ trailer was found.")
    return CifDocument(
        header=header,
        schedules=tuple(schedules),
        ignored_record_counts=tuple(sorted(ignored.items())),
    )


def apply_schedule_transactions(
    documents: Sequence[CifDocument],
) -> tuple[CifSchedule, ...]:
    """Apply one full extract followed by zero or more update documents."""

    if not documents:
        raise MapPlotterError("At least one CIF document is required.")
    if documents[0].header.update_indicator != "F":
        raise MapPlotterError("The first CIF document must be a full (F) extract.")
    state: dict[tuple[str, date, str], CifSchedule] = {}
    for document_index, document in enumerate(documents):
        expected_indicator = "F" if document_index == 0 else "U"
        if document.header.update_indicator != expected_indicator:
            raise MapPlotterError(
                "CIF transaction series must contain one full extract followed "
                "only by update extracts."
            )
        for schedule in document.schedules:
            key = schedule.transaction_key
            if schedule.transaction_type == "N":
                if key in state:
                    raise MapPlotterError(
                        f"CIF N transaction duplicates existing schedule key {key}."
                    )
                state[key] = schedule
            elif schedule.transaction_type == "R":
                if key not in state:
                    raise MapPlotterError(
                        f"CIF R transaction has no existing schedule key {key}."
                    )
                state[key] = schedule
            elif schedule.transaction_type == "D":
                if key not in state:
                    raise MapPlotterError(
                        f"CIF D transaction has no existing schedule key {key}."
                    )
                del state[key]
            else:  # pragma: no cover - parse_cif_mca owns this invariant.
                raise AssertionError(schedule.transaction_type)
    return tuple(
        state[key]
        for key in sorted(state, key=lambda item: (item[0], item[1], item[2]))
    )


@dataclass(frozen=True, slots=True)
class ScheduleExclusion:
    uid: str
    reason: str


@dataclass(frozen=True, slots=True)
class EffectiveScheduleSelection:
    service_date: date
    atoc_codes: tuple[str, ...]
    schedules: tuple[CifSchedule, ...]
    cancelled_uids: tuple[str, ...]
    exclusions: tuple[ScheduleExclusion, ...]


def select_effective_operator_schedules(
    schedules: Iterable[CifSchedule],
    *,
    service_date: date,
    atoc_codes: Collection[str],
) -> EffectiveScheduleSelection:
    """Resolve date/day/STP precedence, then filter passenger operator trains.

    STP precedence is ``C > O > N > P`` per UID.  Cancellations and overlays
    must have a lower permanent candidate on the selected day; ambiguity at the
    winning precedence level is rejected rather than guessed.  Passenger and
    ATOC filtering intentionally happens *after* this resolution.
    """

    normalized_codes = tuple(
        sorted(
            {
                value.strip().upper()
                for value in atoc_codes
                if isinstance(value, str) and value.strip()
            }
        )
    )
    if not normalized_codes:
        raise MapPlotterError("At least one National Rail ATOC code is required.")
    unsupported = sorted(set(normalized_codes) - SUPPORTED_ATOC_CODES)
    if unsupported:
        raise MapPlotterError(
            "This bounded National Rail compiler does not support ATOC code(s): "
            + ", ".join(unsupported)
            + "."
        )

    by_uid: dict[str, list[CifSchedule]] = defaultdict(list)
    for schedule in schedules:
        if schedule.transaction_type == "D":
            raise MapPlotterError(
                "Unapplied CIF D transaction reached service-date selection."
            )
        if schedule.runs_on(service_date):
            by_uid[schedule.uid].append(schedule)

    selected: list[CifSchedule] = []
    cancelled: list[str] = []
    exclusions: list[ScheduleExclusion] = []
    for uid in sorted(by_uid):
        candidates = by_uid[uid]
        winning_rank = max(_STP_PRECEDENCE[item.stp_indicator] for item in candidates)
        winners = [
            item
            for item in candidates
            if _STP_PRECEDENCE[item.stp_indicator] == winning_rank
        ]
        if len(winners) != 1:
            locations = ", ".join(
                f"{item.source_name}:{item.line_number}" for item in winners
            )
            raise MapPlotterError(
                f"CIF UID {uid} has {len(winners)} applicable schedules at the "
                f"same STP precedence on {service_date.isoformat()}: {locations}."
            )
        winner = winners[0]
        lower_permanent = any(item.stp_indicator == "P" for item in candidates)
        if winner.stp_indicator in {"C", "O"} and not lower_permanent:
            raise MapPlotterError(
                f"CIF UID {uid} has STP {winner.stp_indicator} on "
                f"{service_date.isoformat()} without an applicable permanent schedule."
            )
        if winner.is_cancellation:
            cancelled.append(uid)
            continue
        reason = winner.passenger_exclusion_reason()
        if reason is not None:
            exclusions.append(ScheduleExclusion(uid=uid, reason=reason))
            continue
        if winner.atoc_code not in normalized_codes:
            exclusions.append(
                ScheduleExclusion(
                    uid=uid,
                    reason=f"atoc-not-selected:{winner.atoc_code or 'blank'}",
                )
            )
            continue
        selected.append(winner)
    return EffectiveScheduleSelection(
        service_date=service_date,
        atoc_codes=normalized_codes,
        schedules=tuple(selected),
        cancelled_uids=tuple(cancelled),
        exclusions=tuple(exclusions),
    )


@dataclass(frozen=True, slots=True)
class MsnStation:
    name: str
    tiploc: str
    minor_crs: str
    crs: str
    easting_m: int
    northing_m: int
    grid_reference_estimated: bool
    minimum_change_minutes: int
    line_number: int


@dataclass(frozen=True, slots=True)
class MsnDocument:
    source_name: str
    stations: tuple[MsnStation, ...]

    def station_for_tiploc(self, tiploc: str) -> MsnStation:
        matches = [item for item in self.stations if item.tiploc == tiploc]
        if len(matches) != 1:
            raise MapPlotterError(
                f"MSN TIPLOC {tiploc!r} resolves to {len(matches)} stations."
            )
        return matches[0]

    def stations_for_crs(self, crs: str) -> tuple[MsnStation, ...]:
        return tuple(item for item in self.stations if item.crs == crs)


def _msn_grid_coordinate(
    value: str, *, legacy_offset: int, field: str
) -> int:
    if re.fullmatch(r"[0-9]{5}", value) is None:
        raise MapPlotterError(f"{field} must contain five digits.")
    units_100m = int(value) - legacy_offset
    if not 0 <= units_100m <= 9_999:
        raise MapPlotterError(f"{field} is outside the British National Grid range.")
    return units_100m * 100


def parse_msn(payload: bytes, *, source_name: str = "stations.MSN") -> MsnDocument:
    """Parse physical MSN records into a deterministic TIPLOC/CRS crosswalk."""

    saw_header = False
    saw_trailer = False
    stations: list[MsnStation] = []
    seen_tiplocs: set[str] = set()
    for line_number, record in enumerate(
        _decode_ascii_lines(payload, source_name=source_name), start=1
    ):
        if not record or record.startswith("/"):
            continue
        if saw_trailer:
            # The MSNF legacy footer contains variable-format history/CRS usage
            # rows after the explicit END OF MSNF record.  None are station data.
            continue
        if len(record) != 82:
            raise MapPlotterError(
                f"{source_name}:{line_number} MSN record is {len(record)} "
                "characters; exactly 82 are required before END OF MSNF."
            )
        if record.startswith("A") and record[30:42] == "FILE-SPEC=05":
            if saw_header or stations:
                raise MapPlotterError(f"{source_name} repeats or misorders its header.")
            saw_header = True
            continue
        if not saw_header:
            raise MapPlotterError(
                f"{source_name}:{line_number} has data before FILE-SPEC=05."
            )
        if record.startswith("Z") and record[30:41] == "END OF MSNF":
            saw_trailer = True
            continue
        if record.startswith("L"):
            continue
        if not record.startswith("A"):
            raise MapPlotterError(
                f"{source_name}:{line_number} has unsupported pre-trailer MSN "
                f"record type {record[0]!r}."
            )
        name = record[5:31].rstrip()
        tiploc = record[36:43].strip()
        minor_crs = record[43:46].strip()
        crs = record[49:52].strip()
        if not name:
            raise MapPlotterError(f"{source_name}:{line_number} station has no name.")
        if _TIPLOC_RE.fullmatch(tiploc) is None:
            raise MapPlotterError(
                f"{source_name}:{line_number} has invalid TIPLOC {tiploc!r}."
            )
        if tiploc in seen_tiplocs:
            raise MapPlotterError(
                f"{source_name}:{line_number} repeats TIPLOC {tiploc!r}."
            )
        if _CRS_RE.fullmatch(crs) is None:
            raise MapPlotterError(
                f"{source_name}:{line_number} has invalid CRS code {crs!r}."
            )
        if minor_crs and _CRS_RE.fullmatch(minor_crs) is None:
            raise MapPlotterError(
                f"{source_name}:{line_number} has invalid minor CRS {minor_crs!r}."
            )
        estimate = record[57]
        if estimate not in {" ", "E"}:
            raise MapPlotterError(
                f"{source_name}:{line_number} has invalid grid estimate flag "
                f"{estimate!r}."
            )
        raw_change = record[63:65].strip()
        if not raw_change.isdigit() or not 0 <= int(raw_change) <= 99:
            raise MapPlotterError(
                f"{source_name}:{line_number} has invalid minimum change time."
            )
        stations.append(
            MsnStation(
                name=name,
                tiploc=tiploc,
                minor_crs=minor_crs,
                crs=crs,
                easting_m=_msn_grid_coordinate(
                    record[52:57],
                    legacy_offset=10_000,
                    field=f"{source_name}:{line_number} MSN easting",
                ),
                northing_m=_msn_grid_coordinate(
                    record[58:63],
                    legacy_offset=60_000,
                    field=f"{source_name}:{line_number} MSN northing",
                ),
                grid_reference_estimated=estimate == "E",
                minimum_change_minutes=int(raw_change),
                line_number=line_number,
            )
        )
        seen_tiplocs.add(tiploc)
    if not saw_header:
        raise MapPlotterError(f"{source_name} has no FILE-SPEC=05 header.")
    if not saw_trailer:
        raise MapPlotterError(f"{source_name} is truncated: END OF MSNF is absent.")
    if not stations:
        raise MapPlotterError(f"{source_name} contains no physical station records.")
    return MsnDocument(source_name=source_name, stations=tuple(stations))


@dataclass(frozen=True, slots=True)
class NaptanRailEntrance:
    atco_code: str
    common_name: str
    easting_m: int
    northing_m: int
    longitude: float | None
    latitude: float | None
    revision_number: int | None
    line_number: int


@dataclass(frozen=True, slots=True)
class NaptanDocument:
    source_name: str
    rail_entrances: tuple[NaptanRailEntrance, ...]
    input_row_count: int


def _csv_integer(value: str | None, *, field: str) -> int:
    if value is None or re.fullmatch(r"-?[0-9]+", value.strip()) is None:
        raise MapPlotterError(f"{field} must be an integer.")
    return int(value)


def _csv_optional_integer(value: str | None, *, field: str) -> int | None:
    if value is None or not value.strip():
        return None
    return _csv_integer(value, field=field)


def _csv_float(value: str | None, *, field: str) -> float:
    try:
        result = float(value) if value is not None else float("nan")
    except ValueError as exc:
        raise MapPlotterError(f"{field} must be a number.") from exc
    if not isfinite(result):
        raise MapPlotterError(f"{field} must be a finite number.")
    return result


def _csv_optional_float(value: str | None, *, field: str) -> float | None:
    if value is None or not value.strip():
        return None
    return _csv_float(value, field=field)


def parse_naptan_csv(
    payload: bytes, *, source_name: str = "NaPTAN.csv"
) -> NaptanDocument:
    """Parse active NaPTAN RSE access nodes for independent station QA."""

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MapPlotterError(f"{source_name} is not UTF-8 CSV.") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    required_fields = {
        "ATCOCode",
        "CommonName",
        "Easting",
        "Northing",
        "Longitude",
        "Latitude",
        "StopType",
        "RevisionNumber",
        "Status",
    }
    if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise MapPlotterError(f"{source_name} has no unique CSV header.")
    missing = sorted(required_fields - set(reader.fieldnames))
    if missing:
        raise MapPlotterError(
            f"{source_name} is missing NaPTAN column(s): {', '.join(missing)}."
        )
    entrances: list[NaptanRailEntrance] = []
    seen_atco: set[str] = set()
    input_row_count = 0
    try:
        for line_number, row in enumerate(reader, start=2):
            input_row_count += 1
            if None in row:
                raise MapPlotterError(
                    f"{source_name}:{line_number} has more values than header columns."
                )
            if (row.get("StopType") or "").strip().upper() != "RSE":
                continue
            if (row.get("Status") or "").strip().casefold() != "active":
                continue
            atco_code = (row.get("ATCOCode") or "").strip()
            common_name = (row.get("CommonName") or "").strip()
            if not atco_code or not common_name:
                raise MapPlotterError(
                    f"{source_name}:{line_number} active RSE needs ATCOCode and "
                    "CommonName."
                )
            if atco_code in seen_atco:
                raise MapPlotterError(
                    f"{source_name}:{line_number} repeats ATCOCode {atco_code!r}."
                )
            longitude = _csv_optional_float(
                row.get("Longitude"),
                field=f"{source_name}:{line_number} Longitude",
            )
            latitude = _csv_optional_float(
                row.get("Latitude"), field=f"{source_name}:{line_number} Latitude"
            )
            if (longitude is None) != (latitude is None):
                raise MapPlotterError(
                    f"{source_name}:{line_number} has only one WGS84 ordinate."
                )
            if longitude is not None and latitude is not None and not (
                -180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0
            ):
                raise MapPlotterError(
                    f"{source_name}:{line_number} has invalid WGS84 coordinates."
                )
            entrances.append(
                NaptanRailEntrance(
                    atco_code=atco_code,
                    common_name=common_name,
                    easting_m=_csv_integer(
                        row.get("Easting"),
                        field=f"{source_name}:{line_number} Easting",
                    ),
                    northing_m=_csv_integer(
                        row.get("Northing"),
                        field=f"{source_name}:{line_number} Northing",
                    ),
                    longitude=longitude,
                    latitude=latitude,
                    revision_number=_csv_optional_integer(
                        row.get("RevisionNumber"),
                        field=f"{source_name}:{line_number} RevisionNumber",
                    ),
                    line_number=line_number,
                )
            )
            seen_atco.add(atco_code)
    except csv.Error as exc:
        raise MapPlotterError(f"{source_name} contains malformed CSV: {exc}") from exc
    if not entrances:
        raise MapPlotterError(f"{source_name} contains no active RSE rail entrances.")
    return NaptanDocument(
        source_name=source_name,
        rail_entrances=tuple(entrances),
        input_row_count=input_row_count,
    )


def _station_name_key(value: str) -> str:
    normalized = value.casefold().replace("&", " and ")
    normalized = re.sub(r"\b(?:railway|rail)\s+station\b", " ", normalized)
    normalized = re.sub(r"\bstation\b", " ", normalized)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


@dataclass(frozen=True, slots=True)
class StationQaMatch:
    tiploc: str
    crs: str
    atco_code: str
    distance_m: float


@dataclass(frozen=True, slots=True)
class StationQaReport:
    matches: tuple[StationQaMatch, ...]
    unmatched_tiplocs: tuple[str, ...]


def audit_msn_stations_against_naptan(
    msn: MsnDocument,
    naptan: NaptanDocument,
    *,
    maximum_distance_m: float = 1_000.0,
) -> StationQaReport:
    """Name-match MSN stations to nearest NaPTAN entrances in OSGB metres.

    NaPTAN RSE points are entrances, never track-routing nodes.  Consequently
    this function emits QA evidence only and does not alter either coordinate.
    """

    if (
        isinstance(maximum_distance_m, bool)
        or not isinstance(maximum_distance_m, (int, float))
        or not isfinite(float(maximum_distance_m))
        or maximum_distance_m <= 0
    ):
        raise MapPlotterError("NaPTAN QA maximum distance must be positive.")
    by_name: dict[str, list[NaptanRailEntrance]] = defaultdict(list)
    for entrance in naptan.rail_entrances:
        by_name[_station_name_key(entrance.common_name)].append(entrance)
    matches: list[StationQaMatch] = []
    unmatched: list[str] = []
    for station in msn.stations:
        candidates = by_name.get(_station_name_key(station.name), [])
        ranked = sorted(
            (
                hypot(
                    entrance.easting_m - station.easting_m,
                    entrance.northing_m - station.northing_m,
                ),
                entrance.atco_code,
                entrance,
            )
            for entrance in candidates
        )
        if not ranked or ranked[0][0] > maximum_distance_m:
            unmatched.append(station.tiploc)
            continue
        distance_m, _, entrance = ranked[0]
        matches.append(
            StationQaMatch(
                tiploc=station.tiploc,
                crs=station.crs,
                atco_code=entrance.atco_code,
                distance_m=round(distance_m, 3),
            )
        )
    return StationQaReport(
        matches=tuple(matches), unmatched_tiplocs=tuple(unmatched)
    )


@dataclass(frozen=True, slots=True)
class ParsedNationalRailSourcePack:
    source_pack: NationalRailSourcePack
    cif: CifDocument
    msn: MsnDocument
    naptan: NaptanDocument


def parse_national_rail_source_pack(
    source_pack: NationalRailSourcePack,
) -> ParsedNationalRailSourcePack:
    """Parse all three required, already verified source-pack roles."""

    return ParsedNationalRailSourcePack(
        source_pack=source_pack,
        cif=parse_cif_mca(
            source_pack.read_bytes("cif_mca"),
            source_name=source_pack.source("cif_mca").path.name,
        ),
        msn=parse_msn(
            source_pack.read_bytes("msn"),
            source_name=source_pack.source("msn").path.name,
        ),
        naptan=parse_naptan_csv(
            source_pack.read_bytes("naptan_csv"),
            source_name=source_pack.source("naptan_csv").path.name,
        ),
    )


__all__ = [
    "ATOC_OPERATOR_NAMES",
    "CifDocument",
    "CifHeader",
    "CifLocation",
    "CifSchedule",
    "EffectiveScheduleSelection",
    "MsnDocument",
    "MsnStation",
    "NaptanDocument",
    "NaptanRailEntrance",
    "NationalRailSourcePack",
    "ParsedNationalRailSourcePack",
    "PinnedSource",
    "REQUIRED_SOURCE_ROLES",
    "SOURCE_PACK_SCHEMA_VERSION",
    "SUPPORTED_ATOC_CODES",
    "ScheduleExclusion",
    "StationQaMatch",
    "StationQaReport",
    "apply_schedule_transactions",
    "audit_msn_stations_against_naptan",
    "load_national_rail_source_pack",
    "parse_cif_mca",
    "parse_msn",
    "parse_naptan_csv",
    "parse_national_rail_source_pack",
    "select_effective_operator_schedules",
]

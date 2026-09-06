from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from math import ceil, hypot, isfinite
import os
from pathlib import Path
import re
from statistics import median, stdev
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from .models import MapPlotterError
from .stroke_font import stroke_text


SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
MAP_NS = "urn:city-map-plotter:metadata"
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
ET.register_namespace("", SVG_NS)
ET.register_namespace("inkscape", INKSCAPE_NS)
ET.register_namespace("mapplot", MAP_NS)
ET.register_namespace("sodipodi", SODIPODI_NS)

PEN_PROFILE_STYLE = "style"
ACTUAL_PENS_PROFILE = "actual-pens"
WHITE_BLUEPRINT_PENS_PROFILE = "white-blueprint-pens"
PEN_PROFILE_CHOICES = frozenset(
    {PEN_PROFILE_STYLE, ACTUAL_PENS_PROFILE, WHITE_BLUEPRINT_PENS_PROFILE}
)
MAX_PARALLEL_STROKES = 6
DEFAULT_OFFSET_PITCH_RATIO = 0.85
ACTUAL_NIB_LADDER_MM = (0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0)
CUSTOM_INVENTORY_SCHEMA_VERSION = 1
CALIBRATION_SCHEMA_VERSION = 1
CALIBRATION_SPECIMENS_PER_PEN = 10
CALIBRATION_SPECIMEN_LENGTH_MM = 100.0
MAX_CALIBRATION_CV = 0.10

_STABLE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
_STOCK_TONES = frozenset({"light", "mid", "dark"})


def _positive_number(value: Any, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
    ):
        raise MapPlotterError(f"{field_name} must be a positive finite number.")
    return float(value)


def _nonnegative_number(value: Any, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        raise MapPlotterError(f"{field_name} must be a non-negative finite number.")
    return float(value)


def _nonempty_text(value: Any, *, field_name: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise MapPlotterError(f"{field_name} must be non-empty text.")
    return text


def _stable_id(value: Any, *, field_name: str) -> str:
    identity = _nonempty_text(value, field_name=field_name)
    if _STABLE_ID_PATTERN.fullmatch(identity) is None:
        raise MapPlotterError(
            f"{field_name} must use lowercase letters, digits, and hyphens."
        )
    return identity


def _reject_unexpected_fields(
    record: dict[str, Any],
    *,
    allowed: set[str],
    field_name: str,
) -> None:
    unexpected = set(record) - allowed
    if unexpected:
        raise MapPlotterError(
            f"{field_name} has unsupported fields: {', '.join(sorted(unexpected))}."
        )


@dataclass(frozen=True)
class InventoryProvenance:
    recorded_by: str
    recorded_at: str
    method: str | None = None

    def __post_init__(self) -> None:
        recorded_by = _nonempty_text(
            self.recorded_by, field_name="Inventory provenance recorded_by"
        )
        recorded_at = _nonempty_text(
            self.recorded_at, field_name="Inventory provenance recorded_at"
        )
        try:
            datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MapPlotterError(
                "Inventory provenance recorded_at must be an ISO-8601 date or "
                "date-time."
            ) from exc
        method = (
            _nonempty_text(self.method, field_name="Inventory provenance method")
            if self.method is not None
            else None
        )
        object.__setattr__(self, "recorded_by", recorded_by)
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "method", method)

    def as_dict(self) -> dict[str, str]:
        result = {
            "recorded_by": self.recorded_by,
            "recorded_at": self.recorded_at,
        }
        if self.method is not None:
            result["method"] = self.method
        return result


@dataclass(frozen=True)
class StockSpec:
    id: str
    label: str
    tone: str
    finish: str

    def __post_init__(self) -> None:
        stock_id = _stable_id(self.id, field_name="Stock id")
        label = _nonempty_text(self.label, field_name="Stock label")
        tone = _nonempty_text(self.tone, field_name="Stock tone").casefold()
        if tone not in _STOCK_TONES:
            raise MapPlotterError("Stock tone must be light, mid, or dark.")
        finish = _nonempty_text(self.finish, field_name="Stock finish")
        object.__setattr__(self, "id", stock_id)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "tone", tone)
        object.__setattr__(self, "finish", finish)

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "tone": self.tone,
            "finish": self.finish,
        }


@dataclass(frozen=True)
class CalibrationSpecimen:
    id: str
    width_mm: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "id", _stable_id(self.id, field_name="Calibration specimen id")
        )
        object.__setattr__(
            self,
            "width_mm",
            _positive_number(self.width_mm, field_name="Calibration specimen width_mm"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "width_mm": self.width_mm}


@dataclass(frozen=True)
class PenCalibration:
    run_id: str
    stock_id: str
    pen_down_speed: str
    specimens: tuple[CalibrationSpecimen, ...]
    median_width_mm: float
    coefficient_of_variation: float

    def __post_init__(self) -> None:
        run_id = _stable_id(self.run_id, field_name="Pen calibration run_id")
        stock_id = _stable_id(self.stock_id, field_name="Pen calibration stock_id")
        pen_down_speed = _nonempty_text(
            self.pen_down_speed, field_name="Pen calibration pen_down_speed"
        )
        specimens = tuple(self.specimens)
        if len(specimens) != CALIBRATION_SPECIMENS_PER_PEN:
            raise MapPlotterError(
                "A measured pen calibration must contain exactly "
                f"{CALIBRATION_SPECIMENS_PER_PEN} independent specimens."
            )
        specimen_ids = [specimen.id for specimen in specimens]
        if len(specimen_ids) != len(set(specimen_ids)):
            raise MapPlotterError(
                "A measured pen calibration cannot repeat specimen IDs."
            )
        supplied_median = _positive_number(
            self.median_width_mm,
            field_name="Pen calibration median_width_mm",
        )
        supplied_cv = _nonnegative_number(
            self.coefficient_of_variation,
            field_name="Pen calibration coefficient_of_variation",
        )
        widths = [specimen.width_mm for specimen in specimens]
        computed_median = float(median(widths))
        mean_width = sum(widths) / len(widths)
        computed_cv = float(stdev(widths) / mean_width)
        if abs(supplied_median - computed_median) > 1e-6:
            raise MapPlotterError(
                "Pen calibration median_width_mm does not equal the median of "
                "the ten specimen widths."
            )
        if abs(supplied_cv - computed_cv) > 1e-6:
            raise MapPlotterError(
                "Pen calibration coefficient_of_variation does not equal sample "
                "standard deviation divided by the arithmetic mean."
            )
        if supplied_cv > MAX_CALIBRATION_CV + 1e-12:
            raise MapPlotterError(
                "Pen calibration coefficient_of_variation exceeds the 10% "
                "production limit. Recalibrate or mark the pen unmeasured."
            )
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "stock_id", stock_id)
        object.__setattr__(self, "pen_down_speed", pen_down_speed)
        object.__setattr__(self, "specimens", specimens)
        object.__setattr__(self, "median_width_mm", supplied_median)
        object.__setattr__(self, "coefficient_of_variation", supplied_cv)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "stock_id": self.stock_id,
            "pen_down_speed": self.pen_down_speed,
            "specimens": [specimen.as_dict() for specimen in self.specimens],
            "median_width_mm": self.median_width_mm,
            "coefficient_of_variation": self.coefficient_of_variation,
        }


@dataclass(frozen=True)
class PhysicalPen:
    """One real pen, separating its barrel label from its measured mark."""

    ink: str
    nominal_nib_mm: float
    effective_width_mm: float | None = None
    preview_color: str | None = None
    id: str | None = None
    calibration_state: str = "nominal-unmeasured"
    substrate: str | None = None
    calibration: PenCalibration | None = None

    def __post_init__(self) -> None:
        ink = self.ink.strip() if isinstance(self.ink, str) else ""
        if not ink or ink == "*":
            raise MapPlotterError("A physical pen must have a concrete ink colour.")
        nominal = _positive_number(
            self.nominal_nib_mm, field_name="Physical pen nominal_nib_mm"
        )
        effective_supplied = self.effective_width_mm is not None
        effective = (
            nominal
            if not effective_supplied
            else _positive_number(
                self.effective_width_mm,
                field_name="Physical pen effective_width_mm",
            )
        )
        if self.preview_color is not None and (
            not isinstance(self.preview_color, str) or not self.preview_color.strip()
        ):
            raise MapPlotterError("Physical pen preview_color must be non-empty text.")
        pen_id = self.id.strip() if isinstance(self.id, str) else ""
        if not pen_id:
            nib_slug = f"{nominal:.3f}".rstrip("0").rstrip(".").replace(".", "-")
            ink_slug = re.sub(r"[^a-z0-9]+", "-", ink.casefold()).strip("-")
            pen_id = f"{ink_slug}-{nib_slug}"
        pen_id = _stable_id(pen_id, field_name="Physical pen id")
        calibration_state = (
            self.calibration_state.strip()
            if isinstance(self.calibration_state, str)
            else ""
        )
        if calibration_state not in {"nominal-unmeasured", "measured"}:
            raise MapPlotterError(
                "Physical pen calibration_state must be nominal-unmeasured or measured."
            )
        if calibration_state == "measured" and not effective_supplied:
            raise MapPlotterError(
                "A measured physical pen must explicitly provide effective_width_mm."
            )
        if (
            calibration_state == "nominal-unmeasured"
            and effective_supplied
            and abs(effective - nominal) > 1e-9
        ):
            raise MapPlotterError(
                "A non-nominal effective_width_mm must use calibration_state "
                "'measured'."
            )
        substrate = self.substrate.strip() if isinstance(self.substrate, str) else None
        if calibration_state == "measured" and not substrate:
            raise MapPlotterError(
                "A measured physical pen width must identify its calibration substrate."
            )
        if self.calibration is not None:
            if calibration_state != "measured":
                raise MapPlotterError(
                    "Pen calibration evidence requires calibration_state 'measured'."
                )
            if self.calibration.stock_id != substrate:
                raise MapPlotterError(
                    "Pen calibration stock_id must equal the physical pen substrate."
                )
            if abs(self.calibration.median_width_mm - effective) > 1e-6:
                raise MapPlotterError(
                    "Physical pen effective_width_mm must equal the calibration "
                    "median_width_mm."
                )
        object.__setattr__(self, "ink", ink)
        object.__setattr__(self, "nominal_nib_mm", nominal)
        object.__setattr__(self, "effective_width_mm", effective)
        object.__setattr__(self, "id", pen_id)
        object.__setattr__(self, "calibration_state", calibration_state)
        object.__setattr__(self, "substrate", substrate)

    @property
    def identity(self) -> str:
        assert self.id is not None
        return self.id

    @property
    def label(self) -> str:
        return f"{self.ink} {self.nominal_nib_mm:g}"

    @property
    def mark_width_mm(self) -> float:
        assert self.effective_width_mm is not None
        return self.effective_width_mm

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ink": self.ink,
            "nominal_nib_mm": self.nominal_nib_mm,
            "id": self.id,
            "calibration_state": self.calibration_state,
        }
        if self.preview_color is not None:
            result["preview_color"] = self.preview_color
        if self.calibration_state == "measured":
            result["effective_width_mm"] = self.mark_width_mm
            result["substrate"] = self.substrate
        if self.calibration is not None:
            result["calibration"] = self.calibration.as_dict()
        return result


@dataclass(frozen=True)
class PenInventory:
    id: str
    label: str
    pens: tuple[PhysicalPen, ...]
    schema_version: int = CUSTOM_INVENTORY_SCHEMA_VERSION
    provenance: InventoryProvenance | None = None
    stock: StockSpec | None = None

    def __post_init__(self) -> None:
        inventory_id = self.id.strip() if isinstance(self.id, str) else ""
        label = self.label.strip() if isinstance(self.label, str) else ""
        inventory_id = _stable_id(inventory_id, field_name="Pen inventory id")
        if not label:
            raise MapPlotterError("Pen inventory label must be non-empty text.")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != CUSTOM_INVENTORY_SCHEMA_VERSION
        ):
            raise MapPlotterError(
                "Pen inventory schema_version must be the supported integer value "
                f"{CUSTOM_INVENTORY_SCHEMA_VERSION}."
            )
        if not self.pens:
            raise MapPlotterError("A pen inventory must contain at least one pen.")
        identities = [pen.identity for pen in self.pens]
        if len(identities) != len(set(identities)):
            raise MapPlotterError(
                "A pen inventory cannot repeat the same stable pen id."
            )
        object.__setattr__(self, "id", inventory_id)
        object.__setattr__(self, "label", label)
        object.__setattr__(
            self,
            "pens",
            tuple(
                sorted(
                    self.pens,
                    key=lambda pen: (
                        pen.ink.casefold(),
                        pen.nominal_nib_mm,
                        pen.mark_width_mm,
                    ),
                )
            ),
        )

    def pens_for_ink(
        self,
        ink: str,
        *,
        allowed_nibs_mm: Iterable[float] | None = None,
    ) -> tuple[PhysicalPen, ...]:
        requested = ink.strip().casefold()
        allowed = (
            None
            if allowed_nibs_mm is None
            else {round(float(value), 6) for value in allowed_nibs_mm}
        )
        return tuple(
            pen
            for pen in self.pens
            if pen.ink.casefold() == requested
            and (allowed is None or round(pen.nominal_nib_mm, 6) in allowed)
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "label": self.label,
            "pens": [pen.as_dict() for pen in self.pens],
        }
        if self.provenance is not None:
            result["provenance"] = self.provenance.as_dict()
        if self.stock is not None:
            result["stock"] = self.stock.as_dict()
        return result


@dataclass(frozen=True)
class PenWidthFit:
    pen: PhysicalPen
    requested_width_mm: float
    stroke_count: int
    offset_pitch_mm: float
    plotted_width_mm: float
    mode: str

    @property
    def width_error_mm(self) -> float:
        return self.plotted_width_mm - self.requested_width_mm

    def offset_positions(self) -> list[float]:
        centre = (self.stroke_count - 1) / 2
        return [
            (index - centre) * self.offset_pitch_mm
            for index in range(self.stroke_count)
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ink": self.pen.ink,
            "nominal_nib_mm": round(self.pen.nominal_nib_mm, 6),
            "effective_width_mm": round(self.pen.mark_width_mm, 6),
            "requested_width_mm": round(self.requested_width_mm, 6),
            "stroke_count": self.stroke_count,
            "offset_pitch_mm": round(self.offset_pitch_mm, 6),
            "plotted_width_mm": round(self.plotted_width_mm, 6),
            "width_error_mm": round(self.width_error_mm, 6),
            "mode": self.mode,
        }


_PREVIEW_COLORS = {
    "black": "#18181b",
    "blue": "#2563eb",
    "green": "#15803d",
    "grey": "#66717d",
    "purple": "#7e22ce",
    "red": "#dc2626",
    "white": "#b8b8b8",
    "gold": "#b88900",
    "silver": "#7c8794",
}


def _actual_pens() -> tuple[PhysicalPen, ...]:
    common_inks = ("Black", "Blue", "Green", "Grey", "Purple", "Red")
    nibs_by_ink: dict[str, tuple[float, ...]] = {
        ink: (0.25, 0.4) for ink in common_inks
    }
    nibs_by_ink["Black"] += (0.6, 1.0)
    nibs_by_ink["White"] = (0.7, 1.0)
    nibs_by_ink["Gold"] = (1.0,)
    nibs_by_ink["Silver"] = (1.0,)
    return tuple(
        PhysicalPen(
            ink,
            nib,
            preview_color=_PREVIEW_COLORS[ink.casefold()],
        )
        for ink in nibs_by_ink
        for nib in nibs_by_ink[ink]
    )


ACTUAL_PEN_INVENTORY = PenInventory(
    id=ACTUAL_PENS_PROFILE,
    label="Nominal studio template: 0.25/0.4 colours plus specialist broad nibs",
    pens=_actual_pens(),
)

WHITE_BLUEPRINT_PEN_INVENTORY = PenInventory(
    id=WHITE_BLUEPRINT_PENS_PROFILE,
    label="Nominal White paint-pen blueprint set: 0.30/0.40/0.50 mm",
    pens=tuple(
        PhysicalPen(
            "White",
            nib,
            preview_color="#f7f6ee",
        )
        for nib in (0.3, 0.4, 0.5)
    ),
)

BUILTIN_PEN_INVENTORIES: dict[str, PenInventory] = {
    ACTUAL_PENS_PROFILE: ACTUAL_PEN_INVENTORY,
    WHITE_BLUEPRINT_PENS_PROFILE: WHITE_BLUEPRINT_PEN_INVENTORY,
}


def load_pen_inventory(path: Path) -> PenInventory:
    """Load a strict, provenance-bearing custom inventory schema v1."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(f"Could not read pen inventory {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(f"Pen inventory {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise MapPlotterError("A pen inventory JSON file must contain an object.")
    top_level_fields = {
        "schema_version",
        "id",
        "label",
        "provenance",
        "stock",
        "pens",
    }
    _reject_unexpected_fields(
        raw,
        allowed=top_level_fields,
        field_name="Pen inventory",
    )
    missing_top_level = top_level_fields - set(raw)
    if missing_top_level:
        raise MapPlotterError(
            "Pen inventory is missing required fields: "
            f"{', '.join(sorted(missing_top_level))}."
        )
    schema_version = raw["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != CUSTOM_INVENTORY_SCHEMA_VERSION
    ):
        raise MapPlotterError(
            "Pen inventory schema_version must be the supported integer value "
            f"{CUSTOM_INVENTORY_SCHEMA_VERSION}."
        )

    provenance_record = raw["provenance"]
    if not isinstance(provenance_record, dict):
        raise MapPlotterError("Pen inventory provenance must be an object.")
    _reject_unexpected_fields(
        provenance_record,
        allowed={"recorded_by", "recorded_at", "method"},
        field_name="Pen inventory provenance",
    )
    missing_provenance = {"recorded_by", "recorded_at"} - set(provenance_record)
    if missing_provenance:
        raise MapPlotterError(
            "Pen inventory provenance is missing required fields: "
            f"{', '.join(sorted(missing_provenance))}."
        )
    provenance = InventoryProvenance(
        recorded_by=provenance_record["recorded_by"],
        recorded_at=provenance_record["recorded_at"],
        method=provenance_record.get("method"),
    )

    stock_record = raw["stock"]
    if not isinstance(stock_record, dict):
        raise MapPlotterError("Pen inventory stock must be an object.")
    stock_fields = {"id", "label", "tone", "finish"}
    _reject_unexpected_fields(
        stock_record,
        allowed=stock_fields,
        field_name="Pen inventory stock",
    )
    missing_stock = stock_fields - set(stock_record)
    if missing_stock:
        raise MapPlotterError(
            "Pen inventory stock is missing required fields: "
            f"{', '.join(sorted(missing_stock))}."
        )
    stock = StockSpec(
        id=stock_record["id"],
        label=stock_record["label"],
        tone=stock_record["tone"],
        finish=stock_record["finish"],
    )

    records = raw.get("pens")
    if not isinstance(records, list) or not records:
        raise MapPlotterError(
            "A pen inventory JSON file needs a non-empty 'pens' list."
        )
    pens: list[PhysicalPen] = []
    allowed = {
        "id",
        "ink",
        "nominal_nib_mm",
        "effective_width_mm",
        "preview_color",
        "calibration_state",
        "substrate",
        "calibration",
    }
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise MapPlotterError(f"Pen inventory entry {index + 1} must be an object.")
        _reject_unexpected_fields(
            record,
            allowed=allowed,
            field_name=f"Pen inventory entry {index + 1}",
        )
        required_pen_fields = {"id", "ink", "nominal_nib_mm", "calibration_state"}
        missing_pen_fields = required_pen_fields - set(record)
        if missing_pen_fields:
            raise MapPlotterError(
                f"Pen inventory entry {index + 1} is missing required fields: "
                f"{', '.join(sorted(missing_pen_fields))}."
            )
        pen_id = _stable_id(
            record["id"], field_name=f"Pen inventory entry {index + 1} id"
        )
        state = record["calibration_state"]
        if state not in {"nominal-unmeasured", "measured"}:
            raise MapPlotterError(
                f"Pen inventory entry {index + 1} calibration_state must be "
                "nominal-unmeasured or measured."
            )
        preview_color = record.get("preview_color")
        if "preview_color" in record and preview_color is None:
            raise MapPlotterError(
                f"Pen inventory entry {index + 1} preview_color cannot be null; "
                "omit it when unknown."
            )

        calibration: PenCalibration | None = None
        if state == "nominal-unmeasured":
            forbidden = {
                field
                for field in ("effective_width_mm", "substrate", "calibration")
                if field in record
            }
            if forbidden:
                raise MapPlotterError(
                    f"Pen inventory entry {index + 1} is nominal-unmeasured and "
                    "must omit measurement fields: "
                    f"{', '.join(sorted(forbidden))}."
                )
            effective_width = None
            substrate = None
        else:
            measurement_fields = {"effective_width_mm", "substrate", "calibration"}
            missing_measurement = measurement_fields - set(record)
            if missing_measurement:
                raise MapPlotterError(
                    f"Measured pen inventory entry {index + 1} is missing fields: "
                    f"{', '.join(sorted(missing_measurement))}."
                )
            for field in measurement_fields:
                if record[field] is None:
                    raise MapPlotterError(
                        f"Measured pen inventory entry {index + 1} field {field} "
                        "cannot be null."
                    )
            substrate = _stable_id(
                record["substrate"],
                field_name=f"Pen inventory entry {index + 1} substrate",
            )
            if substrate != stock.id:
                raise MapPlotterError(
                    f"Measured pen inventory entry {index + 1} substrate "
                    f"{substrate!r} does not match stock id {stock.id!r}."
                )
            effective_width = _positive_number(
                record["effective_width_mm"],
                field_name=(f"Pen inventory entry {index + 1} effective_width_mm"),
            )
            calibration_record = record["calibration"]
            if not isinstance(calibration_record, dict):
                raise MapPlotterError(
                    f"Measured pen inventory entry {index + 1} calibration must "
                    "be an object."
                )
            calibration_fields = {
                "run_id",
                "stock_id",
                "pen_down_speed",
                "specimens",
                "median_width_mm",
                "coefficient_of_variation",
            }
            _reject_unexpected_fields(
                calibration_record,
                allowed=calibration_fields,
                field_name=f"Pen inventory entry {index + 1} calibration",
            )
            missing_calibration = calibration_fields - set(calibration_record)
            if missing_calibration:
                raise MapPlotterError(
                    f"Pen inventory entry {index + 1} calibration is missing "
                    f"fields: {', '.join(sorted(missing_calibration))}."
                )
            if any(calibration_record[field] is None for field in calibration_fields):
                raise MapPlotterError(
                    f"Pen inventory entry {index + 1} calibration fields cannot "
                    "be null."
                )
            calibration_stock_id = _stable_id(
                calibration_record["stock_id"],
                field_name=(f"Pen inventory entry {index + 1} calibration stock_id"),
            )
            if calibration_stock_id != stock.id:
                raise MapPlotterError(
                    f"Pen inventory entry {index + 1} calibration stock_id does "
                    f"not match stock id {stock.id!r}."
                )
            specimen_records = calibration_record["specimens"]
            if not isinstance(specimen_records, list):
                raise MapPlotterError(
                    f"Pen inventory entry {index + 1} calibration specimens must "
                    "be a list."
                )
            specimens: list[CalibrationSpecimen] = []
            for specimen_index, specimen_record in enumerate(specimen_records, start=1):
                if not isinstance(specimen_record, dict):
                    raise MapPlotterError(
                        f"Pen inventory entry {index + 1} calibration specimen "
                        f"{specimen_index} must be an object."
                    )
                _reject_unexpected_fields(
                    specimen_record,
                    allowed={"id", "width_mm"},
                    field_name=(
                        f"Pen inventory entry {index + 1} calibration specimen "
                        f"{specimen_index}"
                    ),
                )
                if set(specimen_record) != {"id", "width_mm"}:
                    raise MapPlotterError(
                        f"Pen inventory entry {index + 1} calibration specimen "
                        f"{specimen_index} requires id and width_mm."
                    )
                expected_specimen_id = f"{pen_id}-width-{specimen_index:02d}"
                if specimen_record["id"] != expected_specimen_id:
                    raise MapPlotterError(
                        f"Pen inventory entry {index + 1} calibration specimen "
                        f"{specimen_index} must use stable id "
                        f"{expected_specimen_id!r}."
                    )
                specimens.append(
                    CalibrationSpecimen(
                        id=specimen_record["id"],
                        width_mm=specimen_record["width_mm"],
                    )
                )
            calibration = PenCalibration(
                run_id=calibration_record["run_id"],
                stock_id=calibration_stock_id,
                pen_down_speed=calibration_record["pen_down_speed"],
                specimens=tuple(specimens),
                median_width_mm=calibration_record["median_width_mm"],
                coefficient_of_variation=calibration_record["coefficient_of_variation"],
            )
            if abs(calibration.median_width_mm - effective_width) > 1e-6:
                raise MapPlotterError(
                    f"Pen inventory entry {index + 1} effective_width_mm must "
                    "equal its ten-specimen median_width_mm."
                )
        pens.append(
            PhysicalPen(
                ink=record["ink"],
                nominal_nib_mm=record["nominal_nib_mm"],
                effective_width_mm=effective_width,
                preview_color=preview_color,
                id=pen_id,
                calibration_state=state,
                substrate=substrate,
                calibration=calibration,
            )
        )
    return PenInventory(
        schema_version=schema_version,
        id=raw["id"],
        label=raw["label"],
        provenance=provenance,
        stock=stock,
        pens=tuple(pens),
    )


def resolve_pen_inventory(
    profile: str = PEN_PROFILE_STYLE,
    *,
    inventory_path: Path | None = None,
) -> PenInventory | None:
    if inventory_path is not None:
        if profile not in {PEN_PROFILE_STYLE, "custom"}:
            raise MapPlotterError(
                "--pen-inventory cannot be combined with a named --pen-profile."
            )
        return load_pen_inventory(inventory_path)
    if profile == PEN_PROFILE_STYLE:
        return None
    try:
        return BUILTIN_PEN_INVENTORIES[profile]
    except KeyError as exc:
        raise MapPlotterError(
            f"Unknown pen profile {profile!r}. Choose from: "
            f"{', '.join(sorted(PEN_PROFILE_CHOICES))}."
        ) from exc


def fit_pen_width(
    inventory: PenInventory,
    *,
    ink: str,
    requested_width_mm: float,
    allowed_nibs_mm: Iterable[float] | None = None,
) -> PenWidthFit:
    """Choose a real nib first, adding offsets only beyond every single nib.

    For a one-pass fit, absolute width error wins and an exact tie favours the
    narrower nib.  A target wider than every compatible nib uses the broadest
    nib and the fewest overlapping, symmetric offsets that can reach the
    target without gaps.  No repeat-over-the-same-line pass is introduced.
    """

    requested = _positive_number(
        requested_width_mm, field_name="Requested plotted width"
    )
    candidates = inventory.pens_for_ink(ink, allowed_nibs_mm=allowed_nibs_mm)
    if not candidates:
        ladder = ""
        if allowed_nibs_mm is not None:
            ladder = " on the allowed nib ladder " + ", ".join(
                f"{float(value):g}" for value in allowed_nibs_mm
            )
        raise MapPlotterError(
            f"Pen inventory {inventory.id!r} has no {ink} pen{ladder}."
        )
    broadest = max(candidates, key=lambda pen: pen.mark_width_mm)
    broadest_width = broadest.mark_width_mm
    selected = min(
        candidates,
        key=lambda pen: (
            abs(pen.mark_width_mm - requested),
            pen.mark_width_mm > requested,
            pen.mark_width_mm,
            pen.nominal_nib_mm,
        ),
    )
    one_pass_tolerance = max(0.05, requested * 0.15)
    if abs(selected.mark_width_mm - requested) <= one_pass_tolerance + 1e-9:
        achieved = selected.mark_width_mm
        return PenWidthFit(
            pen=selected,
            requested_width_mm=requested,
            stroke_count=1,
            offset_pitch_mm=0.0,
            plotted_width_mm=achieved,
            mode="single-nib",
        )

    if requested <= broadest_width + one_pass_tolerance + 1e-9:
        available = ", ".join(
            f"{pen.nominal_nib_mm:g} nominal/{pen.mark_width_mm:g} effective"
            for pen in candidates
        )
        raise MapPlotterError(
            f"No {ink} pen in inventory {inventory.id!r} fits the requested "
            f"{requested:g} mm width within the {one_pass_tolerance:g} mm one-pass "
            f"tolerance. Available: {available}. Redesign the target to an actual "
            "nib width; offsets are reserved for targets wider than every pen."
        )

    offset_candidates: list[tuple[int, float, str, PhysicalPen, float]] = []
    for pen in candidates:
        effective = pen.mark_width_mm
        for stroke_count in range(2, MAX_PARALLEL_STROKES + 1):
            pitch = (requested - effective) / (stroke_count - 1)
            if effective * 0.5 - 1e-9 <= pitch <= effective * 0.9 + 1e-9:
                assert pen.id is not None
                offset_candidates.append((stroke_count, -effective, pen.id, pen, pitch))
                break
    if not offset_candidates:
        raise MapPlotterError(
            f"Requested {requested:g} mm {ink} mark cannot be constructed from "
            f"inventory {inventory.id!r} using 2-{MAX_PARALLEL_STROKES} parallel "
            "strokes with a safe 0.5-0.9 nib-width pitch. Choose an owned one-pass "
            "width or redesign the target."
        )
    stroke_count, _, _, selected_offset_pen, pitch = min(offset_candidates)
    return PenWidthFit(
        pen=selected_offset_pen,
        requested_width_mm=requested,
        stroke_count=stroke_count,
        offset_pitch_mm=pitch,
        plotted_width_mm=requested,
        mode="parallel-offsets",
    )


def style_pen_width(
    *,
    ink: str,
    nib_mm: float,
    stroke_count: int,
    pitch_ratio: float = DEFAULT_OFFSET_PITCH_RATIO,
) -> PenWidthFit:
    """Represent the legacy style-driven physical plan through the same API."""

    nib = _positive_number(nib_mm, field_name="Style nib width")
    if not 1 <= stroke_count <= MAX_PARALLEL_STROKES:
        raise MapPlotterError(
            f"Physical stroke count must be between 1 and {MAX_PARALLEL_STROKES}."
        )
    pitch = nib * pitch_ratio if stroke_count > 1 else 0.0
    width = nib + (stroke_count - 1) * pitch
    return PenWidthFit(
        pen=PhysicalPen(ink=ink, nominal_nib_mm=nib),
        requested_width_mm=width,
        stroke_count=stroke_count,
        offset_pitch_mm=pitch,
        plotted_width_mm=width,
        mode="style-defined",
    )


def fit_locked_pen_width(
    inventory: PenInventory,
    *,
    ink: str,
    nominal_nib_mm: float,
    stroke_count: int,
    allowed_nibs_mm: Iterable[float] | None = None,
) -> PenWidthFit:
    """Build rank offsets with one explicitly selected physical pen."""

    nominal = _positive_number(nominal_nib_mm, field_name="Locked nominal nib width")
    candidates = [
        pen
        for pen in inventory.pens_for_ink(ink, allowed_nibs_mm=allowed_nibs_mm)
        if abs(pen.nominal_nib_mm - nominal) <= 1e-9
    ]
    if not candidates:
        available = ", ".join(
            f"{pen.nominal_nib_mm:g}"
            for pen in inventory.pens_for_ink(ink, allowed_nibs_mm=allowed_nibs_mm)
        )
        raise MapPlotterError(
            f"Pen inventory {inventory.id!r} has no {ink} {nominal:g} nominal "
            f"pen for single-nib mode. Available nominal nibs: {available or 'none'}."
        )
    if not 1 <= stroke_count <= MAX_PARALLEL_STROKES:
        raise MapPlotterError(
            f"Physical stroke count must be between 1 and {MAX_PARALLEL_STROKES}."
        )
    selected = min(candidates, key=lambda pen: str(pen.id))
    effective = selected.mark_width_mm
    pitch = effective * DEFAULT_OFFSET_PITCH_RATIO if stroke_count > 1 else 0.0
    plotted = effective + (stroke_count - 1) * pitch
    return PenWidthFit(
        pen=selected,
        requested_width_mm=plotted,
        stroke_count=stroke_count,
        offset_pitch_mm=pitch,
        plotted_width_mm=plotted,
        mode=("locked-single-nib" if stroke_count == 1 else "locked-rank-offsets"),
    )


def _svg(tag: str) -> str:
    return f"{{{SVG_NS}}}{tag}"


def _number(value: float) -> str:
    result = f"{value:.3f}".rstrip("0").rstrip(".")
    return result if result else "0"


def _path(points: list[tuple[float, float]]) -> str:
    head, *tail = points
    return " ".join(
        [f"M {_number(head[0])},{_number(head[1])}"]
        + [f"L {_number(x)},{_number(y)}" for x, y in tail]
    )


def _append_vector_label(
    group: ET.Element,
    text: str,
    *,
    x_mm: float,
    y_mm: float,
    height_mm: float,
    nib_mm: float,
    pen_id: str,
    label_id: str,
    max_x_mm: float,
    max_y_mm: float,
) -> None:
    minimum_stroke_mm = max(0.5, 3.0 * nib_mm)
    for index, points in enumerate(
        stroke_text(text, x_mm=x_mm, y_mm=y_mm, height_mm=height_mm), start=1
    ):
        length_mm = sum(
            hypot(end[0] - start[0], end[1] - start[1])
            for start, end in zip(points, points[1:])
        )
        if len(points) < 2 or length_mm + 1e-9 < minimum_stroke_mm:
            raise MapPlotterError(
                f"Calibration label {label_id!r} contains a {length_mm:g} mm "
                f"stroke below the {minimum_stroke_mm:g} mm physical floor for "
                f"the supplied {nib_mm:g} mm label pen."
            )
        if any(
            x < -1e-9 or y < -1e-9 or x > max_x_mm + 1e-9 or y > max_y_mm + 1e-9
            for x, y in points
        ):
            raise MapPlotterError(
                f"Calibration label {label_id!r} does not fit its A3 label zone "
                f"with the supplied {nib_mm:g} mm label pen."
            )
        ET.SubElement(
            group,
            _svg("path"),
            {
                "id": f"label-{label_id}-{index:03d}",
                "d": _path(points),
                "data-label-stroke": str(index),
                "data-plot-pen-id": pen_id,
            },
        )


def _label_height_for_floor(texts: Iterable[str], *, nib_mm: float) -> float:
    """Find a cap height satisfying both binding physical text floors."""

    height_mm = max(2.0, 8.0 * nib_mm)
    minimum_stroke_mm = max(0.5, 3.0 * nib_mm)
    text_list = list(texts)
    for _ in range(8):
        lengths = [
            sum(
                hypot(end[0] - start[0], end[1] - start[1])
                for start, end in zip(points, points[1:])
            )
            for text in text_list
            for points in stroke_text(text, x_mm=0.0, y_mm=0.0, height_mm=height_mm)
        ]
        if not lengths:
            raise MapPlotterError("Calibration labels cannot be empty.")
        shortest = min(lengths)
        if shortest + 1e-9 >= minimum_stroke_mm:
            return height_mm
        height_mm *= (minimum_stroke_mm / shortest) * 1.001
    raise MapPlotterError(
        "No conformant calibration label size could be found for the supplied "
        f"{nib_mm:g} mm label pen."
    )


def _label_bounds(
    text: str,
    *,
    x_mm: float,
    y_mm: float,
    height_mm: float,
) -> tuple[float, float]:
    paths = stroke_text(text, x_mm=x_mm, y_mm=y_mm, height_mm=height_mm)
    points = [point for path in paths for point in path]
    if not points:
        raise MapPlotterError("Calibration labels cannot be empty.")
    return max(x for x, _ in points), max(y for _, y in points)


def _calibration_pen_selection(
    inventory: PenInventory,
    *,
    stock_tone: str,
) -> tuple[tuple[PhysicalPen, ...], list[dict[str, str]]]:
    """Select only pens whose marks can plausibly be measured on this stock."""

    selected: list[PhysicalPen] = []
    excluded: list[dict[str, str]] = []
    for pen in inventory.pens:
        ink = pen.ink.casefold()
        reason: str | None = None
        if stock_tone in {"light", "mid"} and ink == "white":
            reason = f"white ink is not measurably visible on {stock_tone} stock"
        elif stock_tone == "dark" and ink not in {"white", "gold", "silver"}:
            reason = (
                "ordinary/non-opaque ink is not approved on dark stock without "
                "separate opacity evidence"
            )
        if reason is None:
            selected.append(pen)
        else:
            excluded.append(
                {
                    "pen_id": pen.identity,
                    "ink": pen.ink,
                    "reason": reason,
                }
            )
    if not selected:
        raise MapPlotterError(
            f"No pens in inventory {inventory.id!r} are plausibly visible on "
            f"{stock_tone} stock under the conservative calibration policy. "
            "Use a compatible stock or inventory; the card will not invent a "
            "label pen."
        )
    return tuple(selected), excluded


def _calibration_label_pen(pens: Iterable[PhysicalPen]) -> PhysicalPen:
    candidates = list(pens)
    if not candidates:
        raise MapPlotterError(
            "The calibration selection has no supplied label pen; the card will "
            "not invent Black 0.25."
        )
    return min(
        candidates,
        key=lambda pen: (
            pen.mark_width_mm,
            pen.ink.casefold() != "black",
            pen.identity,
        ),
    )


def _calibration_result_schema(
    inventory: PenInventory,
    pens: Iterable[PhysicalPen],
    *,
    run_id: str,
    stock_id: str,
    stock_tone: str,
    pen_down_speed: str,
) -> dict[str, Any]:
    pen_ids = [pen.identity for pen in pens]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "City Map Plotter one-pass pen-width calibration results",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "kind",
            "run_id",
            "inventory_id",
            "stock_id",
            "stock_tone",
            "pen_down_speed",
            "pen_results",
        ],
        "properties": {
            "schema_version": {"const": CALIBRATION_SCHEMA_VERSION},
            "kind": {"const": "pen-width-calibration-results"},
            "run_id": {"const": run_id},
            "inventory_id": {"const": inventory.id},
            "stock_id": {"const": stock_id},
            "stock_tone": {"const": stock_tone},
            "pen_down_speed": {"const": pen_down_speed},
            "pen_results": {
                "type": "array",
                "minItems": len(pen_ids),
                "maxItems": len(pen_ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "pen_id",
                        "specimens",
                        "median_width_mm",
                        "coefficient_of_variation",
                        "approved",
                    ],
                    "properties": {
                        "pen_id": {"enum": pen_ids},
                        "specimens": {
                            "type": "array",
                            "minItems": CALIBRATION_SPECIMENS_PER_PEN,
                            "maxItems": CALIBRATION_SPECIMENS_PER_PEN,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["id", "width_mm"],
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "pattern": (
                                            "^[a-z0-9][a-z0-9-]*-width-(0[1-9]|10)$"
                                        ),
                                    },
                                    "width_mm": {
                                        "type": "number",
                                        "exclusiveMinimum": 0,
                                    },
                                },
                            },
                        },
                        "median_width_mm": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                        },
                        "coefficient_of_variation": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": MAX_CALIBRATION_CV,
                        },
                        "approved": {"const": True},
                    },
                },
            },
        },
    }


def write_pen_calibration_svg(
    output_path: Path,
    inventory: PenInventory = ACTUAL_PEN_INVENTORY,
    *,
    stock_id: str,
    stock_tone: str,
    pen_down_speed: str,
) -> dict[str, Any]:
    """Write an A3 width card with ten independent one-pass lines per pen."""

    stock_id = _stable_id(stock_id, field_name="Calibration stock_id")
    stock_tone = _nonempty_text(
        stock_tone, field_name="Calibration stock_tone"
    ).casefold()
    if stock_tone not in _STOCK_TONES:
        raise MapPlotterError("Calibration stock_tone must be light, mid, or dark.")
    pen_down_speed = _nonempty_text(
        pen_down_speed, field_name="Calibration pen_down_speed"
    )
    if inventory.stock is not None:
        if inventory.stock.id != stock_id:
            raise MapPlotterError(
                f"Calibration stock_id {stock_id!r} does not match inventory stock "
                f"id {inventory.stock.id!r}."
            )
        if inventory.stock.tone != stock_tone:
            raise MapPlotterError(
                f"Calibration stock_tone {stock_tone!r} does not match inventory "
                f"stock tone {inventory.stock.tone!r}."
            )
    selected_pens, excluded_pens = _calibration_pen_selection(
        inventory, stock_tone=stock_tone
    )
    run_id = (
        f"{inventory.id}-{stock_id}-{stock_tone}-width-v{CALIBRATION_SCHEMA_VERSION}"
    )
    label_pen = _calibration_label_pen(selected_pens)
    ordered_pens = (label_pen,) + tuple(
        pen for pen in selected_pens if pen.identity != label_pen.identity
    )

    width_mm, height_mm = 420.0, 297.0
    margin_mm = 12.0
    columns = 2
    rows = ceil(len(ordered_pens) / columns)
    cell_width = (width_mm - 2 * margin_mm) / columns
    card_title = "PEN WIDTH CALIBRATION"
    visual_codes = [f"P{index:02d}" for index in range(1, len(ordered_pens) + 1)]
    label_height_mm = _label_height_for_floor(
        [card_title, *visual_codes], nib_mm=label_pen.mark_width_mm
    )
    grid_top_mm = margin_mm + label_height_mm + 8.0
    cell_height = (height_mm - margin_mm - grid_top_mm) / max(rows, 1)
    broadest_mark_mm = max(pen.mark_width_mm for pen in ordered_pens)
    minimum_sample_pitch_mm = max(1.5, 2.0 * broadest_mark_mm)
    minimum_cell_height_mm = (
        2.0
        + label_height_mm
        + 3.0
        + broadest_mark_mm
        + (CALIBRATION_SPECIMENS_PER_PEN - 1) * minimum_sample_pitch_mm
        + 2.0
    )
    if cell_height + 1e-9 < minimum_cell_height_mm:
        raise MapPlotterError(
            "The calibration inventory cannot be labelled and spaced conformantly "
            "on one A3 sheet: it needs at least "
            f"{minimum_cell_height_mm:.2f} mm per pen row but only "
            f"{cell_height:.2f} mm is available. Split the inventory into smaller "
            "cards or supply a finer visible label pen."
        )
    sample_start_offset_mm = cell_width - 8.0 - CALIBRATION_SPECIMEN_LENGTH_MM
    if sample_start_offset_mm < 34.0:
        raise MapPlotterError(
            "A 100 mm calibration specimen and its conformant label do not fit "
            "the A3 cell width."
        )

    root = ET.Element(
        _svg("svg"),
        {
            "width": f"{_number(width_mm)}mm",
            "height": f"{_number(height_mm)}mm",
            "viewBox": f"0 0 {_number(width_mm)} {_number(height_mm)}",
            "version": "1.1",
        },
    )
    ET.SubElement(root, _svg("title")).text = f"{inventory.label} calibration card"
    metadata = ET.SubElement(root, _svg("metadata"))
    metadata.set(f"{{{MAP_NS}}}pen-profile", inventory.id)
    metadata.set(f"{{{MAP_NS}}}calibration-run-id", run_id)
    metadata.set(f"{{{MAP_NS}}}stock-id", stock_id)
    metadata.set(f"{{{MAP_NS}}}stock-tone", stock_tone)
    ET.SubElement(
        root,
        f"{{{SODIPODI_NS}}}namedview",
        {
            "id": "namedview-pen-calibration",
            "pagecolor": "#ffffff",
            "showborder": "true",
            f"{{{INKSCAPE_NS}}}document-units": "mm",
            f"{{{INKSCAPE_NS}}}showpageshadow": "2",
        },
    )

    labels = ET.SubElement(
        root,
        _svg("g"),
        {
            "id": "layer-calibration-labels",
            f"{{{INKSCAPE_NS}}}groupmode": "layer",
            f"{{{INKSCAPE_NS}}}label": (f"01 — Calibration labels — {label_pen.label}"),
            "fill": "none",
            "stroke": label_pen.preview_color
            or _PREVIEW_COLORS.get(label_pen.ink.casefold(), "#18181b"),
            "stroke-width": _number(label_pen.mark_width_mm),
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
            "data-plot-ink": label_pen.ink,
            "data-plot-pen-id": label_pen.identity,
            "data-plot-nib-mm": _number(label_pen.mark_width_mm),
            "data-plot-nominal-nib-mm": _number(label_pen.nominal_nib_mm),
            "data-plot-effective-width-mm": _number(label_pen.mark_width_mm),
            "data-plot-strokes": "1",
            "data-plot-passes": "1",
            "data-plot-width-mm": _number(label_pen.mark_width_mm),
            "data-plot-cap-height-mm": _number(label_height_mm),
            "data-calibration-schema-version": str(CALIBRATION_SCHEMA_VERSION),
            "data-calibration-run-id": run_id,
            "data-calibration-stock-id": stock_id,
            "data-calibration-stock-tone": stock_tone,
            "data-calibration-pen-down-speed": pen_down_speed,
        },
    )
    _append_vector_label(
        labels,
        card_title,
        x_mm=margin_mm,
        y_mm=margin_mm,
        height_mm=label_height_mm,
        nib_mm=label_pen.mark_width_mm,
        pen_id=label_pen.identity,
        label_id="title",
        max_x_mm=width_mm - margin_mm,
        max_y_mm=grid_top_mm - 3.0,
    )

    samples: list[dict[str, Any]] = []
    pen_records: list[dict[str, Any]] = []
    for index, pen in enumerate(ordered_pens):
        column = index // rows
        row = index % rows
        x = margin_mm + column * cell_width
        y = grid_top_mm + row * cell_height
        sample_start = x + sample_start_offset_mm
        sample_end = sample_start + CALIBRATION_SPECIMEN_LENGTH_MM
        effective = pen.mark_width_mm
        visual_code = visual_codes[index]
        label_max_x, label_max_y = _label_bounds(
            visual_code,
            x_mm=x + 3.0,
            y_mm=y + 2.0,
            height_mm=label_height_mm,
        )
        if label_max_x > sample_start - 3.0 + 1e-9:
            raise MapPlotterError(
                f"Calibration label {visual_code} does not fit before its 100 mm "
                "specimen zone. Split the inventory or supply a finer label pen."
            )
        _append_vector_label(
            labels,
            visual_code,
            x_mm=x + 3.0,
            y_mm=y + 2.0,
            height_mm=label_height_mm,
            nib_mm=label_pen.mark_width_mm,
            pen_id=label_pen.identity,
            label_id=visual_code.casefold(),
            max_x_mm=sample_start - 3.0,
            max_y_mm=min(y + cell_height - 1.0, label_max_y + 1e-6),
        )

        group_id = f"layer-pen-{pen.identity}"
        group = ET.SubElement(
            root,
            _svg("g"),
            {
                "id": group_id,
                f"{{{INKSCAPE_NS}}}groupmode": "layer",
                f"{{{INKSCAPE_NS}}}label": (
                    f"{index + 2:02d} — {visual_code} — {pen.label}"
                ),
                "fill": "none",
                "stroke": pen.preview_color
                or _PREVIEW_COLORS.get(pen.ink.casefold(), "#18181b"),
                "stroke-width": _number(effective),
                "stroke-linecap": "round",
                "stroke-linejoin": "round",
                "data-plot-ink": pen.ink,
                "data-plot-pen-id": pen.identity,
                "data-plot-nib-mm": _number(effective),
                "data-plot-nominal-nib-mm": _number(pen.nominal_nib_mm),
                "data-plot-effective-width-mm": _number(effective),
                "data-plot-strokes": "1",
                "data-plot-passes": "1",
                "data-plot-width-mm": _number(effective),
                "data-calibration-pen": "true",
                "data-calibration-kind": "independent-one-pass-width",
                "data-calibration-schema-version": str(CALIBRATION_SCHEMA_VERSION),
                "data-calibration-run-id": run_id,
                "data-calibration-pen-id": pen.identity,
                "data-calibration-stock-id": stock_id,
                "data-calibration-stock-tone": stock_tone,
                "data-calibration-pen-down-speed": pen_down_speed,
            },
        )
        first_sample_y = y + 2.0 + label_height_mm + 3.0 + effective / 2.0
        last_sample_y = y + cell_height - 2.0 - effective / 2.0
        sample_pitch_mm = (last_sample_y - first_sample_y) / (
            CALIBRATION_SPECIMENS_PER_PEN - 1
        )
        if sample_pitch_mm + 1e-9 < max(1.5, 2.0 * effective):
            raise MapPlotterError(
                f"Pen {pen.identity!r} cannot fit ten independently measurable "
                "specimens in its A3 cell. Split the inventory."
            )
        expected_specimen_ids: list[str] = []
        for specimen_index in range(1, CALIBRATION_SPECIMENS_PER_PEN + 1):
            specimen_id = f"{pen.identity}-width-{specimen_index:02d}"
            expected_specimen_ids.append(specimen_id)
            centre_y = first_sample_y + (specimen_index - 1) * sample_pitch_mm
            path_attributes = {
                "id": f"specimen-{specimen_id}",
                "d": _path([(sample_start, centre_y), (sample_end, centre_y)]),
                "data-calibration-sample": "independent-one-pass-width",
                "data-calibration-specimen-id": specimen_id,
                "data-calibration-schema-version": str(CALIBRATION_SCHEMA_VERSION),
                "data-calibration-run-id": run_id,
                "data-calibration-pen-id": pen.identity,
                "data-calibration-stock-id": stock_id,
                "data-calibration-stock-tone": stock_tone,
                "data-calibration-pen-down-speed": pen_down_speed,
                "data-calibration-length-mm": _number(CALIBRATION_SPECIMEN_LENGTH_MM),
                "data-plot-pen-id": pen.identity,
                "data-stroke-index": "1",
                "data-stroke-count": "1",
                "data-pass-index": "1",
                "data-pass-count": "1",
            }
            ET.SubElement(group, _svg("path"), path_attributes)
            samples.append(
                {
                    "specimen_id": specimen_id,
                    "pen_id": pen.identity,
                    "visual_code": visual_code,
                    "sample": "independent-one-pass-width",
                    "length_mm": CALIBRATION_SPECIMEN_LENGTH_MM,
                    "stroke_count": 1,
                    "pass_count": 1,
                    "stock_id": stock_id,
                    "stock_tone": stock_tone,
                    "pen_down_speed": pen_down_speed,
                }
            )
        pen_records.append(
            {
                "pen_id": pen.identity,
                "visual_code": visual_code,
                "nominal_nib_mm": pen.nominal_nib_mm,
                "current_effective_width_mm": pen.mark_width_mm,
                "expected_specimen_ids": expected_specimen_ids,
            }
        )

    manifest = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "kind": "pen-calibration-card",
        "profile": inventory.as_dict(),
        "calibration": {
            "run_id": run_id,
            "stock_id": stock_id,
            "stock_tone": stock_tone,
            "pen_down_speed": pen_down_speed,
            "kind": "independent-one-pass-width",
            "specimens_per_pen": CALIBRATION_SPECIMENS_PER_PEN,
            "specimen_length_mm": CALIBRATION_SPECIMEN_LENGTH_MM,
            "pass_count": 1,
            "parallel_offsets_included": False,
            "opacity_repeats_included": False,
            "median_is_effective_width": True,
            "coefficient_of_variation_formula": (
                "sample standard deviation / arithmetic mean"
            ),
            "maximum_coefficient_of_variation": MAX_CALIBRATION_CV,
            "label_pen_id": label_pen.identity,
            "label_cap_height_mm": round(label_height_mm, 6),
            "selected_pen_count": len(selected_pens),
            "excluded_pen_count": len(excluded_pens),
        },
        "selection": {
            "policy": "conservative-stock-visibility-v1",
            "selected_pen_ids": [pen.identity for pen in selected_pens],
            "excluded_pens": excluded_pens,
        },
        "page": {
            "paper": "A3",
            "orientation": "landscape",
            "width_mm": width_mm,
            "height_mm": height_mm,
            "margin_mm": margin_mm,
        },
        "pens": pen_records,
        "samples": samples,
        "measurement_instructions": [
            "Plot at 100% scale with no SVG scaling.",
            (
                "Use the recorded stock_id, stock_tone, and pen_down_speed "
                "unchanged for the whole run."
            ),
            (
                "Treat each 100 mm path as one independent, one-pass specimen; "
                "do not retrace it and do not combine adjacent paths."
            ),
            (
                "Allow all marks to dry, then measure the dry width of every "
                "specimen independently at its midpoint."
            ),
            ("For each pen, use the median of all ten widths as effective_width_mm."),
            (
                "Compute coefficient_of_variation as sample standard deviation "
                "divided by arithmetic mean; approve only values <= 0.10."
            ),
            (
                "If CV exceeds 0.10, do not claim calibration_state measured; "
                "inspect flow, mounting, speed, and stock, then repeat the card."
            ),
            (
                "Offset-band and opacity/repeat-pass calibration are separate "
                "experiments and are intentionally absent from this width card."
            ),
        ],
        "result_schema": _calibration_result_schema(
            inventory,
            selected_pens,
            run_id=run_id,
            stock_id=stock_id,
            stock_tone=stock_tone,
            pen_down_speed=pen_down_speed,
        ),
    }
    metadata.text = json.dumps(
        {
            "profile": inventory.id,
            "kind": "pen-calibration-card",
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "run_id": run_id,
            "stock_id": stock_id,
            "stock_tone": stock_tone,
            "pen_down_speed": pen_down_speed,
            "label_pen_id": label_pen.identity,
        },
        separators=(",", ":"),
    )
    ET.indent(root, space="  ")
    temporary = output_path.with_suffix(output_path.suffix + f".{os.getpid()}.tmp")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
        os.replace(temporary, output_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MapPlotterError(
            f"Could not write calibration SVG {output_path}: {exc}"
        ) from exc
    return manifest

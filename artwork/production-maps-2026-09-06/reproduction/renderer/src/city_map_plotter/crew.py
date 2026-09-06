"""The crew: who sat where, and in what.

A crew plate names real people, so the work here is making sure the list is
right before it is set: that the seats a class actually has are all filled, that
nobody is named twice, and that a rig is described in terms the boat supports.

There is deliberately no drawing of the boat. A plan of a racing shell is a
poor picture at poster scale -- long, thin and full of detail too fine to read
-- and the names are the subject. The class is stated in words instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import MapPlotterError


Point = tuple[float, float]
Stroke = list[Point]

CREW_FILE_SCHEMA_VERSION = 1

#: The crew and the result share one grid, so the result can be as long as the
#: largest crew: an eight plus cox.
MAX_RESULT_ROWS = 9

@dataclass(frozen=True)
class BoatClass:
    """One racing class: how many seats it has and what they are called."""

    id: str
    label: str
    rowers: int
    sculling: bool
    coxed: bool

    @property
    def seat_count(self) -> int:
        return self.rowers + (1 if self.coxed else 0)

    @property
    def rig(self) -> str:
        return "sculling" if self.sculling else "sweep"


#: The classes a head race is rowed in.
BOAT_CLASSES: dict[str, BoatClass] = {
    boat.id: boat
    for boat in (
        BoatClass("8+", "Eight", 8, False, True),
        BoatClass("4+", "Coxed four", 4, False, True),
        BoatClass("4-", "Coxless four", 4, False, False),
        BoatClass("4x", "Quad", 4, True, False),
        BoatClass("2-", "Pair", 2, False, False),
        BoatClass("2x", "Double", 2, True, False),
        BoatClass("1x", "Single", 1, True, False),
    )
}


@dataclass(frozen=True)
class Seat:
    """One person in the boat."""

    #: ``"COX"``, or the seat number as text. Bow is 1, stroke is highest.
    position: str
    name: str
    #: ``+1`` stroke side, ``-1`` bow side, ``0`` for a sculler or the cox.
    side: int = 0

    @property
    def is_cox(self) -> bool:
        return self.position.upper() == "COX"


@dataclass(frozen=True)
class Crew:
    """A named crew in a named boat."""

    boat: BoatClass
    seats: tuple[Seat, ...]
    club: str
    event: str
    title: str = ""
    category: str = ""
    #: What happened: for a head, time and position; for a regatta, who was
    #: beaten, by how much, in what time.
    result: tuple[tuple[str, str], ...] = ()
    subtitle: str = ""
    source: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rowers(self) -> tuple[Seat, ...]:
        return tuple(seat for seat in self.seats if not seat.is_cox)

    @property
    def cox(self) -> Seat | None:
        return next((seat for seat in self.seats if seat.is_cox), None)

    def meta_line(self) -> str:
        """Club / event / category, in that order, skipping what is absent."""

        if self.subtitle:
            return self.subtitle.upper()
        parts = [self.club, self.event, self.category]
        return " / ".join(part.upper() for part in parts if part)

    def as_dict(self) -> dict[str, Any]:
        return {
            "boat_class": self.boat.id,
            "boat_label": self.boat.label,
            "club": self.club,
            "event": self.event,
            "rig": self.boat.rig,
            "seats": [
                {
                    "position": seat.position,
                    "name": seat.name,
                    "side": {1: "stroke", -1: "bow", 0: "centre"}[seat.side],
                }
                for seat in self.seats
            ],
            "source": self.source,
        }


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _default_side(boat: BoatClass, index_from_stroke: int, stroke_side: int) -> int:
    """Alternate sides down the boat from whichever side stroke is rigged."""

    if boat.sculling:
        return 0
    return stroke_side if index_from_stroke % 2 == 0 else -stroke_side


def load_crew_file(path: Path | str) -> Crew:
    """Load and validate a hand-written crew file."""

    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(f"Could not read crew file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(f"Crew file {path} is not valid JSON: {exc}") from exc
    return build_crew(raw, source=str(path))


def build_crew(raw: Any, *, source: str = "") -> Crew:
    if not isinstance(raw, dict):
        raise MapPlotterError("A crew file must contain a JSON object.")
    allowed = {
        "schema_version",
        "boat_class",
        "club",
        "event",
        "title",
        "subtitle",
        "category",
        "stroke_side",
        "crew",
        "result",
        "notes",
    }
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise MapPlotterError(
            f"Crew file has unsupported field(s): {', '.join(unexpected)}. "
            f"Supported: {', '.join(sorted(allowed))}."
        )
    if raw.get("schema_version") != CREW_FILE_SCHEMA_VERSION:
        raise MapPlotterError(
            f"Crew file schema_version must be {CREW_FILE_SCHEMA_VERSION}."
        )
    class_id = str(raw.get("boat_class", "")).strip()
    if class_id not in BOAT_CLASSES:
        raise MapPlotterError(
            f"Unknown boat class {class_id!r}. Choose from: "
            + ", ".join(sorted(BOAT_CLASSES))
        )
    boat = BOAT_CLASSES[class_id]

    stroke_side_raw = str(raw.get("stroke_side", "stroke")).strip().casefold()
    if stroke_side_raw not in {"stroke", "bow"}:
        raise MapPlotterError(
            "stroke_side must be 'stroke' or 'bow' — which side stroke's blade "
            "is rigged on."
        )
    stroke_side = 1 if stroke_side_raw == "stroke" else -1

    entries = raw.get("crew")
    if not isinstance(entries, list) or not entries:
        raise MapPlotterError("A crew file must list its crew.")
    named: dict[str, str] = {}
    explicit_sides: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MapPlotterError(f"Crew entry {index} must be an object.")
        extra = sorted(set(entry) - {"position", "name", "side"})
        if extra:
            raise MapPlotterError(
                f"Crew entry {index} has unsupported field(s): {', '.join(extra)}."
            )
        position = str(entry.get("position", "")).strip().upper()
        name = str(entry.get("name", "")).strip()
        if not position or not name:
            raise MapPlotterError(
                f"Crew entry {index} needs both a position and a name."
            )
        if position in named:
            raise MapPlotterError(f"Crew file names seat {position!r} twice.")
        named[position] = name
        if "side" in entry:
            if boat.sculling:
                raise MapPlotterError(
                    f"Seat {position!r} sets a side, but a {boat.id} is sculled: "
                    "both hands, both sides."
                )
            side_value = str(entry["side"]).strip().casefold()
            if side_value not in {"stroke", "bow"}:
                raise MapPlotterError(
                    f"Seat {position!r} side must be 'stroke' or 'bow'."
                )
            explicit_sides[position] = 1 if side_value == "stroke" else -1

    expected = [str(number) for number in range(boat.rowers, 0, -1)]
    if boat.coxed:
        expected = ["COX", *expected]
    missing = [position for position in expected if position not in named]
    surplus = sorted(set(named) - set(expected))
    if missing or surplus:
        detail = "; ".join(
            part
            for part in (
                f"missing {', '.join(missing)}" if missing else "",
                f"unexpected {', '.join(surplus)}" if surplus else "",
            )
            if part
        )
        raise MapPlotterError(
            f"A {boat.id} seats {', '.join(expected)}: {detail}."
        )

    seats: list[Seat] = []
    if boat.coxed:
        seats.append(Seat(position="COX", name=named["COX"], side=0))
    for index, number in enumerate(range(boat.rowers, 0, -1)):
        position = str(number)
        seats.append(
            Seat(
                position=position,
                name=named[position],
                side=explicit_sides.get(
                    position, _default_side(boat, index, stroke_side)
                ),
            )
        )

    result_rows = raw.get("result", [])
    if not isinstance(result_rows, list) or len(result_rows) > MAX_RESULT_ROWS:
        raise MapPlotterError(
            f"result must be a list of at most {MAX_RESULT_ROWS} [label, value] "
            "pairs; the block has one row per line of the crew beside it."
        )
    resolved_fields: list[tuple[str, str]] = []
    for pair in result_rows:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(value, str) and value.strip() for value in pair)
        ):
            raise MapPlotterError(
                "Each result row must be a [label, value] pair of non-empty text."
            )
        resolved_fields.append((pair[0].strip().upper(), pair[1].strip().upper()))

    return Crew(
        boat=boat,
        seats=tuple(seats),
        club=str(raw.get("club", "")).strip(),
        event=str(raw.get("event", "")).strip(),
        title=str(raw.get("title", "")).strip(),
        category=str(raw.get("category", "")).strip(),
        result=tuple(resolved_fields),
        subtitle=str(raw.get("subtitle", "")).strip(),
        source=source,
        notes=tuple(str(note) for note in raw.get("notes", ())),
    )


def seat_label(seat: Seat, boat: BoatClass) -> str:
    """How the seat reads in the crew list: BOW, STROKE, COX or a number."""

    if seat.is_cox:
        return "COX"
    number = int(seat.position)
    if number == 1 and boat.rowers > 1:
        return "BOW"
    if number == boat.rowers and boat.rowers > 1:
        return "STROKE"
    return seat.position


def crew_list_rows(crew: Crew) -> list[tuple[str, str]]:
    """The crew top to bottom in boat order: cox, stroke, down to bow.

    One line per seat. A crew read across columns is a crew you have to
    reassemble in your head; a boat has an order and the list keeps it.
    """

    return [
        (seat_label(seat, crew.boat), seat.name.upper()) for seat in crew.seats
    ]

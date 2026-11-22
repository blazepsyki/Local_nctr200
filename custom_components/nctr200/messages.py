"""NCTR-200 message formats.

Every format here was checked against the controller panel or a manufacturer capture.
Fields marked "candidate" match the recordings but were not confirmed on the panel.
"""

from __future__ import annotations

from dataclasses import dataclass

# Device -> server event IDs. The device resends an unacknowledged event every
# 3-5 s and holds back later events, so every event in this range is acknowledged.
EVENT_IDS = range(0x0100, 0x01FF)

MID_BOOTSTRAP = 0x0000
MID_IDENTITY = 0x0001
MID_PAIR_CODE = 0x0002
MID_KEEPALIVE = 0x0003
MID_POWER = 0x0101
MID_ROOM = 0x0102
MID_HEATING_WATER = 0x0103
MID_ONDOL = 0x0104
MID_BATH = 0x0105
MID_AWAY = 0x0106
MID_SCHEDULE = 0x0107
MID_RESERVATION = 0x0108
MID_ROOM_TEMPERATURE = 0x010A
MID_HOT_WATER_ONLY = 0x0114
MID_HOT_WATER = 0x0115
MID_STATE = 0x01FF

STATE_LENGTH = 103

# 01FF[0] low nibble (candidate). Bath and hot-water-only leave it unchanged.
MODE_ONDOL = "ondol"
MODE_ROOM = "room"
MODE_RESERVATION = "reservation"
MODE_AWAY = "away"
STATUS_MODES = {0: MODE_ONDOL, 1: MODE_ROOM, 2: MODE_RESERVATION, 4: MODE_AWAY}
OVERLAY_BATH = "bath"
OVERLAY_HOT_WATER_ONLY = "hot_water_only"

# Reports naming the active heating mode. One of them answers a power-on command
# instead of an ACK.
MODE_REPORT_IDS = {MID_ROOM, MID_ONDOL, MID_BATH, MID_AWAY, MID_HOT_WATER_ONLY}

# 01FF offsets
OFF_FLAGS = 0
OFF_HEATING_WATER_CURRENT = 10  # candidate; equals 0104 byte 2
OFF_ONDOL_TARGET = 14
OFF_HOT_WATER_TARGET = 15
OFF_ROOM_CURRENT = 16
OFF_ROOM_TARGET = 32
OFF_BURNER_LEVEL = 62  # candidate

ROOM_RANGE = (10, 45)
ONDOL_RANGE = (45, 85)
HOT_WATER_RANGE = (35, 60)
HEATING_WATER_RANGE = (45, 85)
RESERVATION_STOP_RANGE = (1, 24)  # hours
RESERVATION_RUN_RANGE = (10, 90)  # minutes


@dataclass(frozen=True, slots=True)
class StateReport:
    """Named values of a 103-byte 01FF report."""

    flags: int
    heating_water_current: int
    ondol_target: int
    hot_water_target: int
    room_current: int
    room_target: int
    burner_level: int


def parse_state(body: bytes) -> StateReport | None:
    """Return the named values of a state report, or None for another length."""
    if len(body) != STATE_LENGTH:
        return None
    return StateReport(
        flags=body[OFF_FLAGS],
        heating_water_current=body[OFF_HEATING_WATER_CURRENT],
        ondol_target=body[OFF_ONDOL_TARGET],
        hot_water_target=body[OFF_HOT_WATER_TARGET],
        room_current=body[OFF_ROOM_CURRENT],
        room_target=body[OFF_ROOM_TARGET],
        burner_level=body[OFF_BURNER_LEVEL],
    )


def mode_of(flags: int) -> str | None:
    """Heating mode from 01FF[0] (candidate)."""
    return STATUS_MODES.get(flags & 0x0F)


def power_of(flags: int) -> bool:
    """Power state from 01FF[0] (candidate).

    0x80 is set while operating and clear when powered off (0x10). Away mode was
    also seen without it (0x04), so away counts as on.
    """
    return bool(flags & 0x80) or flags & 0x0F == 4


def _check(name: str, value: int, bounds: tuple[int, int]) -> int:
    low, high = bounds
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be {low}..{high}")
    return value


def room_body(target: int) -> bytes:
    """0102: room heating target. Selects room heating mode."""
    return bytes((1, 0, 0, 0, _check("room target", target, ROOM_RANGE), 0))


def power_body(on: bool) -> bytes:
    """0101: power on (01) or off (02)."""
    return bytes((1, 0, 0, 0, 0, 1 if on else 2))


def ondol_body(target: int) -> bytes:
    """0104: ondol target."""
    return bytes((0, 0, 0, 0, _check("ondol target", target, ONDOL_RANGE), 0))


def hot_water_body(target: int) -> bytes:
    """0115: hot water target."""
    return bytes((0, 0, 0, 0, _check("hot water target", target, HOT_WATER_RANGE), 0))


def hot_water_only_body(target: int) -> bytes:
    """0114: hot-water-only operation at the given hot water target."""
    return bytes((0, 0, 0, 0, _check("hot water target", target, HOT_WATER_RANGE), 0))


def heating_water_body(target: int) -> bytes:
    """0103: heating water target."""
    return bytes((0, 0, 0, 0, _check("heating water target", target, HEATING_WATER_RANGE), 0))


def bath_body() -> bytes:
    """0105: bath."""
    return bytes((0, 0, 0, 0, 0x3C, 0))


def away_body() -> bytes:
    """0106: away."""
    return bytes((1, 2, 0, 0, 0, 0))


def reservation_body(stop_hours: int, run_minutes: int) -> bytes:
    """0108: heating reservation (stop hours, run minutes). The order cannot be set."""
    _check("stop hours", stop_hours, RESERVATION_STOP_RANGE)
    _check("run minutes", run_minutes, RESERVATION_RUN_RANGE)
    return bytes((1, 0, 0, 0, stop_hours, run_minutes))


def schedule_body(slots: tuple[int, ...]) -> bytes:
    """0107: 24-hour schedule, room mask 01 + 48 half-hour slots from 00:00 (1 = run)."""
    if len(slots) != 48 or any(slot not in (0, 1) for slot in slots):
        raise ValueError("schedule needs 48 half-hour slots of 0/1")
    return bytes((1, *slots))


def parse_schedule(text: str) -> tuple[int, ...]:
    """'00:00-09:00,18:00-24:00' -> 48 half-hour slots. Boundaries on :00 or :30."""
    slots = [0] * 48
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        try:
            start, end = part.split("-")
        except ValueError:
            raise ValueError(f"schedule range must look like 09:00-18:00: {part}") from None
        first, last = _half_hours(start), _half_hours(end)
        if not 0 <= first < last <= 48:
            raise ValueError(f"schedule range must run forward within 00:00-24:00: {part}")
        for index in range(first, last):
            slots[index] = 1
    return tuple(slots)


def _half_hours(clock: str) -> int:
    try:
        hours, minutes = (int(x) for x in clock.split(":"))
    except ValueError:
        raise ValueError(f"time must look like 09:30: {clock}") from None
    if minutes not in (0, 30) or not 0 <= hours <= 24 or (hours == 24 and minutes):
        raise ValueError(f"schedule times use 30-minute steps: {clock}")
    return hours * 2 + minutes // 30


def slots_to_text(slots: tuple[int, ...]) -> str:
    """48 half-hour slots -> '00:00-09:00,18:00-24:00' ('off' when empty)."""
    runs: list[str] = []
    start: int | None = None
    for index, on in enumerate((*slots, 0)):
        if on and start is None:
            start = index
        elif not on and start is not None:
            runs.append(
                f"{start // 2:02d}:{start % 2 * 30:02d}-{index // 2:02d}:{index % 2 * 30:02d}"
            )
            start = None
    return ",".join(runs) or "off"

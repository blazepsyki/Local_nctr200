"""Known NCTR-200 message formats, decoding and novelty checks.

Evidence levels:
  confirmed  - matched against LCD observations or physical operations
  candidate  - consistent with recordings but not yet confirmed on the LCD

Sources: 2026-11-20/21 field runs (field-20261120-00..05, 94..99,
field-20261121-11..15) and the 2026-10 manufacturer captures.
"""

from dataclasses import dataclass, field

CONFIRMED, CANDIDATE = "confirmed", "candidate"


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    confidence: str
    low: int | None = None
    high: int | None = None
    labels: dict | None = None


@dataclass(frozen=True)
class Spec:
    mid: int
    name: str
    length: int
    fixed: dict = field(default_factory=dict)      # offset -> expected byte
    fields: tuple = ()
    function: str | None = None                     # function reported by this event
    confidence: str = CANDIDATE
    note: str = ""


# Device -> server messages.
DEVICE_SPECS = {s.mid: s for s in (
    Spec(0x0000, "bootstrap", 4, note="device type prefix; full ID is not included"),
    Spec(0x0001, "identity", 8, confidence=CONFIRMED),
    Spec(0x0002, "pair_code_request", 0, confidence=CONFIRMED),
    Spec(0x0101, "power", 6, {0: 1, 1: 0, 2: 0, 3: 0, 4: 0},
         (Field("power", 5, CANDIDATE, labels={1: "on", 2: "off"}),),
         function="power", confidence=CONFIRMED,
         note="byte5=2 on OFF (20261120-02, field-20261122-22 01:37:28); ON is reported "
              "by the resumed mode's message instead of 0101"),
    Spec(0x0102, "room_heating", 6, {0: 1, 3: 0, 5: 0},
         (Field("origin", 1, CONFIRMED, labels={0: "remote", 1: "panel"}),
          Field("room_current", 2, CONFIRMED, 0, 60),
          Field("room_target", 4, CONFIRMED, 10, 45)),
         function="room_heating", confidence=CONFIRMED,
         note="byte1 is 0 in the report that follows a remote 0102 command (field-20261122-21)"),
    Spec(0x0103, "heating_water", 6, {0: 0, 1: 0, 3: 0, 5: 0},
         (Field("heating_water_target", 4, CONFIRMED, 45, 85),),
         function="heating_water", confidence=CANDIDATE,
         note="sent by the manufacturer app together with 0115 from its combined "
              "heating-water / hot-water screen (boiler6, 2026-11-22); 1-degree steps; "
              "not yet reported by the device"),
    Spec(0x0107, "schedule_24h", 49, confidence=CANDIDATE,
         note="1 room-mask byte + 48 half-hour slots from 00:00, 1 = run (boiler6); "
              "not yet reported by the device"),
    Spec(0x0104, "ondol", 6, {0: 0, 1: 0, 3: 0, 5: 0},
         (Field("heating_water_current", 2, CANDIDATE, 0, 100),
          Field("ondol_target", 4, CONFIRMED, 45, 85)),
         function="ondol", confidence=CONFIRMED,
         note="byte2 equals 01FF[10]; resent every 5 s while unacknowledged"),
    Spec(0x0105, "bath", 6, {0: 0, 1: 0, 2: 0, 3: 0, 5: 0},
         (Field("bath_value", 4, CANDIDATE, 50, 60),),
         function="bath", confidence=CONFIRMED,
         note="LCD shows 80 in bath mode; 80 is not transmitted"),
    Spec(0x0106, "away", 6, {0: 1, 1: 1, 2: 0, 3: 0, 4: 0, 5: 0},
         function="away", confidence=CONFIRMED,
         note="entering away; leaving away is reported by the selected mode"),
    # Each press of the reservation button resends this report. Switching the
    # run/stop order on the panel did not change any byte (field-20261122-22).
    Spec(0x0108, "reservation", 6, {0: 1, 1: 0, 2: 0, 3: 0},
         (Field("reservation_stop_hours", 4, CONFIRMED, 1, 24),
          Field("reservation_run_minutes", 5, CONFIRMED, 10, 90)),
         function="reservation", confidence=CONFIRMED),
    Spec(0x010A, "room_temperature", 6, {0: 1, 1: 0, 2: 0, 3: 0, 5: 0},
         (Field("room_current", 4, CONFIRMED, 0, 60),),
         confidence=CONFIRMED,
         note="sent by the device when the current room temperature changes: 22 when "
              "the LCD went 23->22 (11-21 23:39); 23 at 13:01:48 (field-20261122-31), "
              "followed by 01FF[16] 22->23 and LCD 23 / 0102 current 23 at 16:15"),
    Spec(0x0114, "hot_water_only", 6, {0: 0, 1: 0, 2: 0, 3: 0, 5: 0},
         (Field("hot_water_target", 4, CANDIDATE, 35, 60),),
         function="hot_water_only", confidence=CONFIRMED,
         note="panel hot-water-only (heating button 5 s); byte4 60 = hot-water setting "
              "(field-20261122-21 01:18:53)"),
    Spec(0x0115, "hot_water", 6, {0: 0, 1: 0, 2: 0, 3: 0, 5: 0},
         (Field("hot_water_target", 4, CONFIRMED, 35, 60),),
         function="hot_water_setting", confidence=CONFIRMED),
    Spec(0x01FF, "state", 103, confidence=CONFIRMED),
)}

# IDs named in the 2026-10 APK analysis but not yet received. A novel record for
# them carries this hint so the next analysis starts from it.
APK_HINTS = {0x0109: "APK: error report", 0x010B: "APK: room state change",
             0x0110: "APK: heating temperature report", 0x0122: "APK: HOT button"}

STATE_FIELDS = (
    Field("status_flags", 0, CANDIDATE),
    Field("heating_water_current", 10, CANDIDATE, 0, 100),
    Field("temperature_11", 11, CANDIDATE, 0, 100),
    Field("temperature_12", 12, CANDIDATE, 0, 100),
    Field("ondol_target", 14, CONFIRMED, 45, 85),
    Field("hot_water_target", 15, CONFIRMED, 35, 60),
    Field("room_current", 16, CONFIRMED, 0, 60),
    Field("room_target", 32, CONFIRMED, 10, 45),
    Field("burner_level", 62, CANDIDATE),
    Field("counter_66", 66, CANDIDATE),
    Field("counter_69", 69, CANDIDATE),
    Field("counter_72", 72, CANDIDATE),
)
STATE_U16 = (("burner_value_58", 58, CANDIDATE),)
# Offsets that changed in any recording so far (local runs and manufacturer captures).
STATE_VARIABLE = {0, 10, 11, 12, 14, 15, 16, 32, 58, 59, 62, 66, 69, 72, 90}
# Byte values of every other offset in all 103-byte reports observed so far.
STATE_BASELINE = bytes.fromhex(
    "00000000000000000000000000002d3c000000000000000000000000000000000000"
    "00000000000000000000000000002d3c000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000100000000000000000000000000"
    "00")
assert len(STATE_BASELINE) == 103
STATUS_FLAGS_SEEN = {0x04, 0x10, 0x82, 0x84, 0xC1, 0xC2, 0xD0}
# 01FF[0] split (candidates, field-20261122-22 and earlier runs):
#   low nibble = mode: 0 ondol, 1 room heating, 2 reservation, 4 away. Bath and
#          hot-water-only overlay the heating mode and leave it unchanged
#          (0xC1 in room mode 11-21 23:14-23:37 and after the 23:54:29 heating press;
#          0xD0 throughout ondol operation on 11-22)
#   0x80 = operating (clear only while powered off, 0x10)
#   0x40 = active phase: set with reservation "run first" (0xC2), clear with "stop first"
#          (0x82); whether it is the order setting or the current run phase is still open
STATUS_MODES = {0: "ondol", 1: "room_heating", 2: "reservation", 4: "away"}


def _field_value(spec_field, body):
    value = body[spec_field.offset]
    out = {"value": value, "confidence": spec_field.confidence}
    if spec_field.labels:
        out["label"] = spec_field.labels.get(value)
    return out


def parse_schedule(text):
    """'00:00-09:00,18:00-24:00' -> 48 half-hour slots (1 = run). Boundaries on :00/:30."""
    slots = [0] * 48
    for part in text.replace(" ", "").split(","):
        start, end = part.split("-")
        a, b = _half_hours(start), _half_hours(end)
        if not 0 <= a < b <= 48:
            raise ValueError(f"schedule range must run forward within 00:00-24:00: {part}")
        for i in range(a, b):
            slots[i] = 1
    return tuple(slots)


def _half_hours(clock):
    hours, minutes = (int(x) for x in clock.split(":"))
    if minutes not in (0, 30) or not 0 <= hours <= 24 or (hours == 24 and minutes):
        raise ValueError(f"schedule times use 30-minute steps: {clock}")
    return hours * 2 + minutes // 30


def slots_to_text(slots):
    runs, start = [], None
    for i, on in enumerate(list(slots) + [0]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append(f"{start // 2:02d}:{start % 2 * 30:02d}-{i // 2:02d}:{i % 2 * 30:02d}")
            start = None
    return ",".join(runs) or "off"


def decode(mid, body):
    """Return named values for a device message, or None for an unknown ID."""
    spec = DEVICE_SPECS.get(mid)
    if spec is None or len(body) != spec.length:
        return None
    values = {}
    if mid == 0x0107:
        values["schedule_room_mask"] = {"value": body[0], "confidence": CANDIDATE}
        values["schedule_24h"] = {"value": slots_to_text(body[1:]), "confidence": CANDIDATE}
        return values
    if mid == 0x01FF:
        for f in STATE_FIELDS:
            values[f.name] = _field_value(f, body)
        values["status_flags"]["hex"] = f"{body[0]:02X}"
        flags = body[0]
        values["status_mode"] = {"value": flags & 0x0F, "label": STATUS_MODES.get(flags & 0x0F),
                                 "confidence": CANDIDATE}
        values["status_operating"] = {"value": int(bool(flags & 0x80)),
                                      "label": "on" if flags & 0x80 else "off",
                                      "confidence": CANDIDATE}
        values["status_active_phase"] = {"value": int(bool(flags & 0x40)),
                                         "label": "active" if flags & 0x40 else "inactive",
                                         "confidence": CANDIDATE}
        for name, offset, confidence in STATE_U16:
            values[name] = {"value": int.from_bytes(body[offset:offset + 2], "big"),
                            "confidence": confidence}
        return values
    for f in spec.fields:
        values[f.name] = _field_value(f, body)
    if spec.function:
        values["reported_function"] = {"value": spec.function, "confidence": spec.confidence}
    return values


def novelty(mid, body):
    """Reasons why a device message differs from everything recorded so far."""
    spec = DEVICE_SPECS.get(mid)
    if spec is None:
        return ["unknown_id"]
    if len(body) != spec.length:
        return [f"unexpected_length:{len(body)}!={spec.length}"]
    reasons = []
    if mid == 0x01FF:
        changed = [i for i in range(103)
                   if i not in STATE_VARIABLE and body[i] != STATE_BASELINE[i]]
        if changed:
            reasons.append("state_constant_changed:" + ",".join(map(str, changed)))
        if body[0] not in STATUS_FLAGS_SEEN:
            reasons.append(f"new_status_flags:{body[0]:02X}")
        fields = STATE_FIELDS
    else:
        differ = [i for i, expected in spec.fixed.items() if body[i] != expected]
        if differ:
            reasons.append("fixed_bytes_differ:" + ",".join(map(str, differ)))
        fields = spec.fields
    for f in fields:
        value = body[f.offset]
        if f.low is not None and not f.low <= value <= f.high:
            reasons.append(f"out_of_range:{f.name}={value}")
        if f.labels and value not in f.labels:
            reasons.append(f"new_value:{f.name}={value}")
    if spec.confidence == CANDIDATE and mid not in (0x0000,):
        reasons.append("unconfirmed_message")
    return reasons


@dataclass(frozen=True)
class Command:
    feature: str
    mid: int
    enable_flag: str
    report_mid: int
    report_offset: int | None      # None: the report itself is the confirmation
    format_source: str
    fixed_value: int | None = None
    # 01FF offset holding the stored value. The device ACKs remote commands but
    # does not send its own report afterwards, so the next state report is the
    # applied-value evidence (field-20261122-20).
    state_offset: int | None = None

    def body(self, value):
        if self.feature == "temperature":
            return bytes((1, 0, 0, 0, value, 0))
        if self.feature == "power":
            return bytes((1, 0, 0, 0, 0, value))
        if self.feature in ("hot_water", "ondol", "hot_water_only", "heating_water"):
            return bytes((0, 0, 0, 0, value, 0))
        if self.feature == "reservation":
            stop_hours, run_minutes = value
            return bytes((1, 0, 0, 0, stop_hours, run_minutes))
        if self.feature == "schedule":
            return bytes((1,)) + bytes(value)
        if self.feature == "bath":
            return bytes((0, 0, 0, 0, 0x3C, 0))
        if self.feature == "away":
            return bytes((1, 2, 0, 0, 0, 0))
        raise ValueError("unsupported command feature")

    def reported(self, body):
        if self.feature == "reservation":
            return (body[4], body[5])
        if self.feature == "schedule":
            return tuple(body[1:49])
        if self.report_offset is None:
            return 1
        return body[self.report_offset]


COMMANDS = {c.feature: c for c in (
    Command("temperature", 0x0102, "enable_temperature", 0x0102, 4,
            "manufacturer capture 2026-10 (0102 01 00 00 00 TT 00)", state_offset=32),
    Command("power", 0x0101, "enable_power", 0x0101, 5,
            "manufacturer capture 2026-10 (0101 01 00 00 00 00 01/02)"),
    Command("hot_water", 0x0115, "enable_hot_water", 0x0115, 4,
            "manufacturer capture 2026-10 (0115 00 00 00 00 3C 00)", state_offset=15),
    Command("bath", 0x0105, "enable_bath", 0x0105, None,
            "manufacturer capture 2026-10 (0105 00 00 00 00 3C 00)", fixed_value=1),
    Command("away", 0x0106, "enable_away", 0x0106, None,
            "manufacturer capture 2026-10 (0106 01 02 00 00 00 00)", fixed_value=1),
    Command("hot_water_only", 0x0114, "enable_hot_water_only", 0x0114, 4,
            "field-20261122-22: 0114 00 00 00 00 3C 00 ACKed and applied on the panel "
            "(format from the 0114 report)", state_offset=15),
    Command("reservation", 0x0108, "enable_reservation", 0x0108, 4,
            "manufacturer capture boiler6 2026-11-22 (0108 01 00 00 00 03 14); "
            "field-20261122-22 applied and echoed by the panel"),
    Command("ondol", 0x0104, "enable_ondol", 0x0104, 4,
            "manufacturer capture boiler6 2026-11-22 (0104 00 00 00 00 2D 00); "
            "field-20261122-20 LCD and 01FF[14] matched", state_offset=14),
    Command("heating_water", 0x0103, "enable_heating_water", 0x0103, 4,
            "manufacturer capture boiler6 2026-11-22 (0103 00 00 00 00 2D/32 00, sent with "
            "0115); 0103 alone ACKed and applied in field-20261122-27"),
    Command("schedule", 0x0107, "enable_schedule", 0x0107, None,
            "manufacturer capture boiler6 2026-11-22 (0107, 49 bytes: room mask + 48 "
            "half-hour slots); 00:00-23:00 ACKed and applied in field-20261122-27"),
)}

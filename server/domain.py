"""Patients and the outage rules: when an outage starts and ends, who is alerted,
and when the alert escalates. No I/O here: callers pass the clock in and deliver
the returned notifications themselves."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")  # E.164, which is what SMS/voice APIs expect
MAX_CONTACTS = 6
MAX_BACKUP_MINUTES = 48 * 60
MAX_TEXT_LENGTH = 120


class InvalidPatient(ValueError):
    """A patient or contact breaks an invariant. The message is safe to show to staff."""


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidPatient(f"{label} is required")
    if len(value) > MAX_TEXT_LENGTH:
        raise InvalidPatient(f"{label} must be at most {MAX_TEXT_LENGTH} characters")


class Channel(Enum):
    SMS = "sms"
    CALL = "call"


@dataclass(frozen=True)
class Contact:
    role: str
    name: str
    phone: str
    channel: Channel

    def __post_init__(self) -> None:
        _require_text(self.role, "Contact role")
        _require_text(self.name, "Contact name")
        if not PHONE_PATTERN.match(self.phone):
            raise InvalidPatient(f"{self.name}'s phone must look like +16015550123")


@dataclass(frozen=True)
class Patient:
    device_id: str
    device_token: str   # proves a report really comes from this patient's unit
    share_token: str    # opens the family status page; separate so either can be rotated alone
    name: str
    address: str
    lat: float
    lon: float
    equipment: str
    backup_minutes: int
    acuity: int  # 1 = most critical
    contacts: tuple[Contact, ...]  # alerted in this order until someone acknowledges

    def __post_init__(self) -> None:
        if not DEVICE_ID_PATTERN.match(self.device_id):
            raise InvalidPatient("Unit ID must be 3-40 lowercase letters, digits or dashes, like unit-07")
        for label, value in (("Name", self.name), ("Address", self.address), ("Equipment", self.equipment)):
            _require_text(value, label)
        if len(self.device_token) < 16 or len(self.share_token) < 16:
            raise InvalidPatient("tokens must be at least 16 characters")
        if not -90 <= self.lat <= 90 or not -180 <= self.lon <= 180:
            raise InvalidPatient("Location is outside valid latitude/longitude")
        if not 0 <= self.backup_minutes <= MAX_BACKUP_MINUTES:
            raise InvalidPatient(f"Backup minutes must be between 0 and {MAX_BACKUP_MINUTES}")
        if self.acuity not in (1, 2, 3):
            raise InvalidPatient("Acuity must be 1, 2 or 3")
        if not 1 <= len(self.contacts) <= MAX_CONTACTS:
            raise InvalidPatient(f"Add between 1 and {MAX_CONTACTS} contacts")


@dataclass(frozen=True)
class Notification:
    contact: Contact
    message: str


@dataclass
class Outage:
    started_at: datetime
    escalation_step: int
    next_escalation_at: datetime | None
    reply_code: str  # in every alert, so "OK <code>" names exactly this outage
    acknowledged_by: str | None = None

    def is_escalating(self) -> bool:
        return self.acknowledged_by is None and self.next_escalation_at is not None


@dataclass
class Unit:
    patient: Patient
    has_wall_power: bool | None = None  # None until the device first reports
    battery_percent: int | None = None
    last_seen: datetime | None = None
    outage: Outage | None = None
    notified_phones: set[str] = field(default_factory=set)


class UnknownDevice(Exception):
    pass


class ReplyResult(Enum):
    ACKNOWLEDGED = "acknowledged"
    AMBIGUOUS = "ambiguous"      # phone is on several open outages and gave no code
    NOTHING_OPEN = "nothing_open"


@dataclass(frozen=True)
class ReplyOutcome:
    result: ReplyResult
    device_id: str | None = None


REPLY_PATTERN = re.compile(r"^\s*OK\W*(\d{4})?\b", re.IGNORECASE)  # "OK", "ok 4821", "OK4821"; not "okay"


def random_reply_code() -> str:
    return f"{secrets.randbelow(10_000):04d}"


class Monitor:
    """Live state of every unit. `status_link` turns a patient into the URL their
    family can open; the URL format is the caller's business, not this module's."""

    def __init__(self, patients: list[Patient], escalate_after: timedelta, offline_after: timedelta,
                 status_link: Callable[[Patient], str], new_reply_code: Callable[[], str] = random_reply_code):
        self._units: dict[str, Unit] = {}
        self._escalate_after = escalate_after
        self._offline_after = offline_after
        self._status_link = status_link
        self._new_reply_code = new_reply_code
        for patient in patients:
            self.upsert_patient(patient)

    # ---- patient roster -------------------------------------------------

    def patient(self, device_id: str) -> Patient:
        return self._unit(device_id).patient

    def patients(self) -> list[Patient]:
        return sorted((u.patient for u in self._units.values()), key=lambda p: (p.acuity, p.name))

    def has_device(self, device_id: str) -> bool:
        return device_id in self._units

    def upsert_patient(self, patient: Patient) -> None:
        """Add a patient, or replace their details while keeping any live outage state."""
        unit = self._units.get(patient.device_id)
        if unit is None:
            self._units[patient.device_id] = Unit(patient)
        else:
            unit.patient = patient

    def remove_patient(self, device_id: str) -> None:
        self._units.pop(device_id, None)

    # ---- device reports and escalation ---------------------------------

    def report(self, device_id: str, has_wall_power: bool, battery_percent: int | None,
               now: datetime) -> list[Notification]:
        unit = self._unit(device_id)
        unit.last_seen = now
        unit.battery_percent = battery_percent
        unit.has_wall_power = has_wall_power

        if not has_wall_power and unit.outage is None:
            return self._start_outage(unit, now)
        if has_wall_power and unit.outage is not None:
            return self._end_outage(unit, now)
        return []

    def acknowledge(self, device_id: str, by: str) -> bool:
        """Stop escalation for the current outage. False if there is nothing to acknowledge."""
        outage = self._unit(device_id).outage
        if outage is None or outage.acknowledged_by is not None:
            return False
        outage.acknowledged_by = by
        outage.next_escalation_at = None
        return True

    def acknowledge_by_reply(self, phone: str, text: str) -> ReplyOutcome:
        """Handle an SMS reply. "OK 4821" acknowledges the outage with that code, if this
        phone was alerted about it. A bare "OK" works only when exactly one open outage
        alerted this phone; with several, guessing could stop the wrong patient's alerts."""
        match = REPLY_PATTERN.match(text)
        if not match:
            return ReplyOutcome(ReplyResult.NOTHING_OPEN)
        code = match.group(1)
        open_for_phone = [u for u in self._units.values()
                          if u.outage and u.outage.acknowledged_by is None and phone in u.notified_phones]
        if code is not None:
            open_for_phone = [u for u in open_for_phone if u.outage.reply_code == code]
        if not open_for_phone:
            return ReplyOutcome(ReplyResult.NOTHING_OPEN)
        if len(open_for_phone) > 1:
            return ReplyOutcome(ReplyResult.AMBIGUOUS)
        device_id = open_for_phone[0].patient.device_id
        self.acknowledge(device_id, phone)
        return ReplyOutcome(ReplyResult.ACKNOWLEDGED, device_id)

    def tick(self, now: datetime) -> list[Notification]:
        notifications = []
        for unit in self._units.values():
            outage = unit.outage
            if outage is None or not outage.is_escalating() or now < outage.next_escalation_at:
                continue
            outage.escalation_step += 1
            contacts = unit.patient.contacts
            if outage.escalation_step >= len(contacts):
                outage.next_escalation_at = None  # everyone on the list has been alerted
                continue
            outage.next_escalation_at = now + self._escalate_after
            notifications.append(self._alert(unit, contacts[outage.escalation_step], outage, now))
        return notifications

    # ---- read models ------------------------------------------------------

    def snapshot(self, now: datetime) -> list[dict]:
        rows = [self._row(unit, now) for unit in self._units.values()]
        # Most urgent first: active outages by remaining backup, then by acuity.
        return sorted(rows, key=lambda r: (r["status"] != "outage", r["backup_minutes_left"] or 0, r["acuity"]))

    def status(self, device_id: str, now: datetime) -> dict:
        return self._row(self._unit(device_id), now)

    # ---- internals --------------------------------------------------------

    def _unit(self, device_id: str) -> Unit:
        unit = self._units.get(device_id)
        if unit is None:
            raise UnknownDevice(device_id)
        return unit

    def _start_outage(self, unit: Unit, now: datetime) -> list[Notification]:
        unit.outage = Outage(started_at=now, escalation_step=0, next_escalation_at=now + self._escalate_after,
                             reply_code=self._unused_reply_code())
        unit.notified_phones = set()
        return [self._alert(unit, unit.patient.contacts[0], unit.outage, now)]

    def _end_outage(self, unit: Unit, now: datetime) -> list[Notification]:
        minutes = int((now - unit.outage.started_at).total_seconds() // 60)
        message = f"Lifeline: Power is back on at {unit.patient.name}'s home after {minutes} min."
        notifications = [Notification(c, message) for c in unit.patient.contacts if c.phone in unit.notified_phones]
        unit.outage = None
        unit.notified_phones = set()
        return notifications

    def _alert(self, unit: Unit, contact: Contact, outage: Outage, now: datetime) -> Notification:
        unit.notified_phones.add(contact.phone)
        p = unit.patient
        left = self._backup_left(p, outage, now)
        message = (f"Lifeline alert: Power is out at {p.name}'s home, {p.address}. "
                   f"{p.equipment} backup about {left} min. Reply OK {outage.reply_code} if you're responding. "
                   f"Live status: {self._status_link(p)}")
        return Notification(contact, message)

    def _unused_reply_code(self) -> str:
        """Codes must be unique among open outages or "OK <code>" would be ambiguous again."""
        in_use = {u.outage.reply_code for u in self._units.values() if u.outage}
        while (code := self._new_reply_code()) in in_use:
            pass
        return code

    @staticmethod
    def _backup_left(patient: Patient, outage: Outage, now: datetime) -> int:
        elapsed = (now - outage.started_at).total_seconds() / 60
        return max(0, int(patient.backup_minutes - elapsed))

    def _row(self, unit: Unit, now: datetime) -> dict:
        p, outage = unit.patient, unit.outage
        if unit.last_seen is None:
            status = "never_seen"
        elif outage is not None:
            status = "outage"
        elif now - unit.last_seen > self._offline_after:
            status = "offline"
        else:
            status = "ok"
        return {
            "device_id": p.device_id,
            "name": p.name,
            "address": p.address,
            "lat": p.lat,
            "lon": p.lon,
            "equipment": p.equipment,
            "acuity": p.acuity,
            "backup_minutes": p.backup_minutes,
            "status": status,
            "battery_percent": unit.battery_percent,
            "last_seen": unit.last_seen.isoformat() if unit.last_seen else None,
            "outage_started": outage.started_at.isoformat() if outage else None,
            "backup_minutes_left": self._backup_left(p, outage, now) if outage else None,
            "acknowledged_by": outage.acknowledged_by if outage else None,
            "contacts_alerted": [c.name for c in p.contacts if c.phone in unit.notified_phones],
        }


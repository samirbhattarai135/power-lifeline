"""Patients and the outage rules: when an outage starts and ends, who is alerted,
and when the alert escalates. No I/O here: callers pass the clock in and deliver
the returned notifications themselves."""

from __future__ import annotations

import copy
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")  # E.164, which is what SMS/voice APIs expect
MAX_CONTACTS = 6
MAX_BACKUP_MINUTES = 48 * 60
MAX_TEXT_LENGTH = 120
AGENCY_ROLE_WORDS = ("agency", "nurse", "home health")  # roles are free text; these mark care staff


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

    def is_care_staff(self) -> bool:
        return any(word in self.role.lower() for word in AGENCY_ROLE_WORDS)


MAX_PLAN_TEXT_LENGTH = 600
SHORT_BACKUP_MINUTES = 120   # a critical patient with less than this has little time to get help
LOW_UNIT_BATTERY_PERCENT = 30
WEAK_SIGNAL_DBM = -80        # below this the unit's reports start to drop
MIN_RUNNING_AMPS, MAX_RUNNING_AMPS = 0.05, 30.0


@dataclass(frozen=True)
class EmergencyPlan:
    """What the family should do when the power goes out. Every part is optional;
    written by the agency, shown on the family page during outages and storms."""
    supplies: str = ""        # e.g. "2 oxygen tanks in the hall closet, spare battery charged"
    place_name: str = ""      # nearest place with power: hospital, shelter, relative
    place_address: str = ""
    place_phone: str = ""
    steps: str = ""           # the agency's instructions, one per line

    def __post_init__(self) -> None:
        for label, value in (("Backup supplies", self.supplies), ("Place name", self.place_name),
                             ("Place address", self.place_address), ("Place phone", self.place_phone),
                             ("Steps", self.steps)):
            if not isinstance(value, str):
                raise InvalidPatient(f"{label} must be text")
            if len(value) > MAX_PLAN_TEXT_LENGTH:
                raise InvalidPatient(f"{label} must be at most {MAX_PLAN_TEXT_LENGTH} characters")

    def is_empty(self) -> bool:
        return not any((self.supplies, self.place_name, self.place_address, self.place_phone, self.steps))


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
    plan: EmergencyPlan = field(default_factory=EmergencyPlan)
    running_amps_min: float | None = None  # with a current clamp: below this while powered, the machine stopped

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
        if self.running_amps_min is not None and not MIN_RUNNING_AMPS <= self.running_amps_min <= MAX_RUNNING_AMPS:
            raise InvalidPatient(f"Running current must be between {MIN_RUNNING_AMPS} and {MAX_RUNNING_AMPS:g} amps")

    def care_gaps(self) -> list[str]:
        """What in this patient's setup would slow help down in an outage, in staff words."""
        gaps = []
        if self.acuity == 1 and self.backup_minutes < SHORT_BACKUP_MINUTES:
            gaps.append(f"Critical, with under {SHORT_BACKUP_MINUTES // 60} hours of backup")
        if len(self.contacts) == 1:
            gaps.append("Only one person on the call list")
        if not any(c.is_care_staff() for c in self.contacts):
            gaps.append("No nurse or agency on the call list")
        if self.plan.is_empty():
            gaps.append("No emergency plan")
        return gaps




@dataclass(frozen=True)
class Notification:
    contact: Contact
    message: str
    # Set on power-loss alerts so the delivery layer can offer a one-tap "I'm responding":
    # which incident, and which entry in its alerted list this notification is.
    incident_id: str = ""
    alert_index: int = -1
    respond_url: str | None = None  # filled in by the service, never by the rules here


class IncidentKind(Enum):
    POWER = "power"    # wall power lost at the patient's home
    SILENT = "silent"  # the unit stopped reporting: its Wi-Fi, or its own battery, may be down
    STORM = "storm"    # a storm notice sent before any outage (see storm.py)
    MACHINE = "machine"          # the equipment stopped drawing current although the power is on
    HELP = "help"                # someone pressed the help button on the unit
    TEMPERATURE = "temperature"  # the room has been dangerously hot or cold for a while


# Emergencies share one escalation slot per home: alerted in order, acknowledged, ended.
# Everything else is an advisory: told once to care staff, closed when it clears.
EMERGENCY_KINDS = (IncidentKind.POWER, IncidentKind.MACHINE, IncidentKind.HELP)


class Button(Enum):
    ACK = "ack"    # short press: someone at home has it in hand
    HELP = "help"  # long press: the patient needs someone now


AT_HOME = "Someone at home (unit button)"

# ---- thresholds agreed for the demo; revisit with the agency for real patients --------------
MACHINE_STOPPED_AFTER = timedelta(seconds=60)  # how long below running current before alerting
ROOM_TOO_HOT_F = 90.0
ROOM_TOO_COLD_F = 55.0
ROOM_ALERT_AFTER = timedelta(minutes=10)       # how long out of range before alerting
CRITICAL_BACKUP_MINUTES = 15  # backup left when county emergency management is told and 911 is the fallback
BACKUP_LOW_MINUTES = 30  # backup left when the whole call list hears "backup almost out" (half, for shorter backups)


@dataclass(frozen=True)
class Alerted:
    """One person we alerted, kept with the incident so replies, restores and the
    report all agree on who was told and when."""
    name: str
    role: str
    phone: str
    at: datetime


@dataclass(frozen=True)
class UnitHealth:
    """Optional heartbeat extras. Older firmware sends none of them."""
    rssi: int | None = None       # Wi-Fi signal in dBm
    firmware: str | None = None
    uptime_s: int | None = None


@dataclass
class Outage:
    """One incident at one home, from the first alert until it ends. Saved on every change,
    so a restarted server resumes escalation where it stopped instead of starting over."""
    id: str
    kind: IncidentKind
    device_id: str
    patient_name: str   # snapshots: the report must survive the patient being renamed or removed
    equipment: str
    started_at: datetime
    reply_code: str = ""  # power outages: in every alert, so "OK <code>" names exactly this outage
    escalation_step: int = 0
    next_escalation_at: datetime | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    ended_at: datetime | None = None
    alerted: list[Alerted] = field(default_factory=list)
    detail: str = ""     # storm notices: the warning, e.g. "Ice Storm Warning"
    hazard_id: str = ""  # storm notices: which hazard, so a restart doesn't announce it again
    backup_low_at: datetime | None = None  # when everyone was told the patient's backup is almost out

    def is_escalating(self) -> bool:
        return self.acknowledged_by is None and self.next_escalation_at is not None

    def is_unreached(self, now: datetime, answer_within: timedelta) -> bool:
        """Everyone on the list has been alerted, the last of them has had time to answer,
        and nobody has said they're responding: the agency must now report it (42 CFR 484.102(b)(3))."""
        return (self.ended_at is None and self.acknowledged_by is None and self.next_escalation_at is None
                and bool(self.alerted) and now - self.alerted[-1].at >= answer_within)

    def was_alerted(self, phone: str) -> bool:
        return any(a.phone == phone for a in self.alerted)

    def alerted_names(self) -> list[str]:
        return list(dict.fromkeys(a.name for a in self.alerted))


@dataclass(frozen=True)
class SensorReadings:
    """Optional sensors on the unit; None when not fitted or not reported."""
    load_amps: float | None = None     # current clamp on the equipment's cord
    room_temp_f: float | None = None


@dataclass
class Unit:
    patient: Patient
    has_wall_power: bool | None = None  # None until the device first reports
    battery_percent: int | None = None
    last_seen: datetime | None = None
    health: UnitHealth = field(default_factory=UnitHealth)
    readings: SensorReadings = field(default_factory=SensorReadings)
    outage: Outage | None = None   # the open emergency, any of EMERGENCY_KINDS
    silent: Outage | None = None
    room: Outage | None = None     # open temperature advisory
    low_current_since: datetime | None = None
    room_out_of_range_since: datetime | None = None
    # ponytail: kept in memory, so a restart re-sends one "nobody answered" notice; persist it if that matters.
    unreached_told_for: str | None = None  # id of the emergency whose "nobody answered" notice went out
    critical_told_for: str | None = None   # same, for the 15-minute emergency notice (also in memory only)


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


REPLY_SUFFIX = "(text reply)"   # an acknowledgment from someone on the call list, by text...
BUTTON_SUFFIX = "(push button)"  # ...or by the button in their push notification
CALL_LIST_SUFFIXES = (REPLY_SUFFIX, BUTTON_SUFFIX)
REPLY_PATTERN = re.compile(r"^\s*OK\W*(\d{4})?\b", re.IGNORECASE)  # "OK", "ok 4821", "OK4821"; not "okay"


def random_reply_code() -> str:
    return f"{secrets.randbelow(10_000):04d}"


def new_incident_id() -> str:
    return str(uuid.uuid4())


def _alert_index(outage: Outage, phone: str) -> int:
    """Where this phone was first alerted, so a one-tap "I'm responding" names the right person."""
    return next(i for i, a in enumerate(outage.alerted) if a.phone == phone)


class Monitor:
    """Live state of every unit. `status_link` turns a patient into the URL their
    family can open; the URL format is the caller's business, not this module's.

    Every incident that changes is queued; the caller drains the queue with
    `drain_changes()` and saves it. Pass the saved open incidents back in as
    `open_incidents` after a restart."""

    def __init__(self, patients: list[Patient], escalate_after: timedelta, offline_after: timedelta,
                 status_link: Callable[[Patient], str], new_reply_code: Callable[[], str] = random_reply_code,
                 silent_alert_after: timedelta = timedelta(minutes=5),
                 open_incidents: list[Outage] = (), new_id: Callable[[], str] = new_incident_id,
                 county_contacts: tuple[Contact, ...] = ()):
        self._units: dict[str, Unit] = {}
        # County emergency management for patients whose own call list names nobody from the county.
        self._county_contacts = tuple(county_contacts)
        self._escalate_after = escalate_after
        self._offline_after = offline_after  # when the board shows a unit as quiet
        # When someone is alerted about it. Longer than offline_after: a hotspot can take
        # half a minute to reconnect, and a page for every blip teaches people to ignore pages.
        self._silent_alert_after = silent_alert_after
        self._status_link = status_link
        self._new_reply_code = new_reply_code
        self._new_id = new_id
        self._changed: list[Outage] = []
        for patient in patients:
            self.upsert_patient(patient)
        for incident in open_incidents:
            self._restore(incident)

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
               now: datetime, health: UnitHealth | None = None, readings: SensorReadings | None = None,
               button: Button | None = None) -> list[Notification]:
        unit = self._unit(device_id)
        unit.last_seen = now
        unit.battery_percent = battery_percent
        unit.has_wall_power = has_wall_power
        if health is not None:
            unit.health = health
        if readings is not None:
            unit.readings = readings

        notifications = []
        if unit.silent is not None:
            # A power-loss alert below already tells people the unit is back; don't send two.
            notifications += self._end_silence(unit, now, announce=has_wall_power)
        notifications += self._check_power(unit, now)
        notifications += self._check_machine(unit, now)
        notifications += self._check_room(unit, now)
        if button is Button.HELP:
            notifications += self._help_pressed(unit, now)
        elif button is Button.ACK:
            self.acknowledge(device_id, AT_HOME, now)
        return notifications

    def acknowledge(self, device_id: str, by: str, now: datetime) -> bool:
        """Stop escalation for the current outage. False if there is nothing to acknowledge."""
        outage = self._unit(device_id).outage
        if outage is None or outage.acknowledged_by is not None:
            return False
        outage.acknowledged_by = by
        outage.acknowledged_at = now
        outage.next_escalation_at = None
        if outage.kind is IncidentKind.HELP:
            outage.ended_at = now  # a help call is over once someone has it
            self._units[device_id].outage = None
        self._touch(outage)
        return True

    def acknowledge_by_reply(self, phone: str, text: str, now: datetime) -> ReplyOutcome:
        """Handle an SMS reply. "OK 4821" acknowledges the outage with that code, if this
        phone was alerted about it. A bare "OK" works only when exactly one open outage
        alerted this phone; with several, guessing could stop the wrong patient's alerts."""
        match = REPLY_PATTERN.match(text)
        if not match:
            return ReplyOutcome(ReplyResult.NOTHING_OPEN)
        code = match.group(1)
        open_for_phone = [u for u in self._units.values()
                          if u.outage and u.outage.acknowledged_by is None and u.outage.was_alerted(phone)]
        if code is not None:
            open_for_phone = [u for u in open_for_phone if u.outage.reply_code == code]
        if not open_for_phone:
            return ReplyOutcome(ReplyResult.NOTHING_OPEN)
        if len(open_for_phone) > 1:
            return ReplyOutcome(ReplyResult.AMBIGUOUS)
        unit = open_for_phone[0]
        # Record who, not their number: this text shows on the board, the history and the report.
        name = next(a.name for a in unit.outage.alerted if a.phone == phone)
        self.acknowledge(unit.patient.device_id, f"{name} {REPLY_SUFFIX}", now)
        return ReplyOutcome(ReplyResult.ACKNOWLEDGED, unit.patient.device_id)

    def acknowledge_by_button(self, incident_id: str, alert_index: int, now: datetime) -> bool:
        """The "I'm responding" button in one alert. Works only while that outage is open
        and unacknowledged; records the alerted person's name."""
        unit = next((u for u in self._units.values() if u.outage and u.outage.id == incident_id), None)
        if unit is None or not 0 <= alert_index < len(unit.outage.alerted):
            return False
        name = unit.outage.alerted[alert_index].name
        return self.acknowledge(unit.patient.device_id, f"{name} {BUTTON_SUFFIX}", now)

    def tick(self, now: datetime) -> list[Notification]:
        notifications = []
        for unit in self._units.values():
            notifications += self._escalate(unit, now)
            notifications += self._check_backup(unit, now)
            notifications += self._check_critical(unit, now)
            notifications += self._check_unreached(unit, now)
            if self._has_gone_silent(unit, now):
                notifications += self._start_silence(unit, now)
        return notifications

    def drain_changes(self) -> list[Outage]:
        """Copies of every incident changed since the last call, oldest change first."""
        changed, self._changed = self._changed, []
        return [copy.deepcopy(incident) for incident in changed]

    # ---- read models ------------------------------------------------------

    def snapshot(self, now: datetime) -> list[dict]:
        rows = [self._row(unit, now) for unit in self._units.values()]
        # Most urgent first: active outages by remaining backup, then by acuity.
        return sorted(rows, key=lambda r: (r["status"] != "outage", r["backup_minutes_left"] or 0, r["acuity"]))

    def status(self, device_id: str, now: datetime) -> dict:
        return self._row(self._unit(device_id), now)

    # ---- internals: power outages -----------------------------------------

    def _unit(self, device_id: str) -> Unit:
        unit = self._units.get(device_id)
        if unit is None:
            raise UnknownDevice(device_id)
        return unit

    def _check_power(self, unit: Unit, now: datetime) -> list[Notification]:
        emergency = unit.outage
        if not unit.has_wall_power:
            if emergency is not None and emergency.kind is IncidentKind.MACHINE:
                # It stopped because the power went: one power outage, not two incidents.
                self._close(unit, now)
                emergency = None
            return self._start_emergency(unit, IncidentKind.POWER, now) if emergency is None else []
        if emergency is not None and emergency.kind is IncidentKind.POWER:
            return self._end_outage(unit, now)
        return []

    def _check_machine(self, unit: Unit, now: datetime) -> list[Notification]:
        threshold, amps = unit.patient.running_amps_min, unit.readings.load_amps
        if threshold is None or amps is None or not unit.has_wall_power:
            unit.low_current_since = None
            return []
        if amps >= threshold:
            unit.low_current_since = None
            if unit.outage is not None and unit.outage.kind is IncidentKind.MACHINE:
                return self._end_outage(unit, now)
            return []
        unit.low_current_since = unit.low_current_since or now
        if unit.outage is None and now - unit.low_current_since >= MACHINE_STOPPED_AFTER:
            return self._start_emergency(unit, IncidentKind.MACHINE, now)
        return []

    def _help_pressed(self, unit: Unit, now: datetime) -> list[Notification]:
        """Everyone on the list at once: a person asking for help shouldn't wait for escalation."""
        if unit.outage is None:
            self._start_emergency(unit, IncidentKind.HELP, now, alert_first=False)
        outage = unit.outage
        outage.next_escalation_at = None
        outage.escalation_step = len(unit.patient.contacts)
        message = self._help_message(unit, outage)
        notifications = []
        for contact in unit.patient.contacts:
            if not outage.was_alerted(contact.phone) or outage.kind is not IncidentKind.HELP:
                outage.alerted.append(Alerted(contact.name, contact.role, contact.phone, now))
                notifications.append(Notification(contact, message, incident_id=outage.id, alert_index=len(outage.alerted) - 1))
        self._touch(outage)
        return notifications

    def _start_emergency(self, unit: Unit, kind: IncidentKind, now: datetime,
                         alert_first: bool = True) -> list[Notification]:
        p = unit.patient
        unit.outage = Outage(id=self._new_id(), kind=kind, device_id=p.device_id,
                             patient_name=p.name, equipment=p.equipment, started_at=now,
                             reply_code=self._unused_reply_code(), next_escalation_at=now + self._escalate_after)
        self._touch(unit.outage)
        return [self._alert(unit, p.contacts[0], unit.outage, now)] if alert_first else []

    def _close(self, unit: Unit, now: datetime) -> None:
        unit.outage.ended_at = now
        unit.outage.next_escalation_at = None
        self._touch(unit.outage)
        unit.outage = None

    def _escalate(self, unit: Unit, now: datetime) -> list[Notification]:
        outage = unit.outage
        if outage is None or not outage.is_escalating() or now < outage.next_escalation_at:
            return []
        outage.escalation_step += 1
        self._touch(outage)
        contacts = unit.patient.contacts
        if outage.escalation_step >= len(contacts):
            outage.next_escalation_at = None  # everyone on the list has been alerted
            return []
        outage.next_escalation_at = now + self._escalate_after
        return [self._alert(unit, contacts[outage.escalation_step], outage, now)]

    def _end_outage(self, unit: Unit, now: datetime) -> list[Notification]:
        outage = unit.outage
        self._close(unit, now)
        minutes = int((now - outage.started_at).total_seconds() // 60)
        p = unit.patient
        message = (f"Lifeline: Power is back on at {p.name}'s home after {minutes} min." if outage.kind is IncidentKind.POWER
                   else f"Lifeline: {p.name}'s {p.equipment.lower()} is running again.")
        return [Notification(c, message) for c in p.contacts if outage.was_alerted(c.phone)]

    def _alert(self, unit: Unit, contact: Contact, outage: Outage, now: datetime) -> Notification:
        outage.alerted.append(Alerted(contact.name, contact.role, contact.phone, now))
        self._touch(outage)
        p = unit.patient
        left = self._backup_left(p, outage, now)
        if outage.kind is IncidentKind.MACHINE:
            what = (f"{p.name}'s {p.equipment.lower()} has stopped at {p.address}, though the power is on. "
                    f"Switch to backup now; backup about {left} min.")
        elif outage.kind is IncidentKind.HELP:
            return Notification(contact, self._help_message(unit, outage), incident_id=outage.id, alert_index=len(outage.alerted) - 1)
        else:
            what = f"Power is out at {p.name}'s home, {p.address}. {p.equipment} backup about {left} min."
        message = (f"Lifeline alert: {what} Reply OK {outage.reply_code} if you're responding. "
                   f"Live status: {self._status_link(p)}")
        return Notification(contact, message, incident_id=outage.id, alert_index=len(outage.alerted) - 1)

    def _help_message(self, unit: Unit, outage: Outage) -> str:
        p = unit.patient
        return (f"Lifeline alert: The help button was pressed at {p.name}'s home, {p.address}. "
                f"Reply OK {outage.reply_code} if you're responding. Live status: {self._status_link(p)}")

    def _check_backup(self, unit: Unit, now: datetime) -> list[Notification]:
        """Being acknowledged is not being there. When the backup is nearly gone and the power
        (or the machine) is still off, the whole call list hears it once, responders included."""
        outage, p = unit.outage, unit.patient
        if outage is None or outage.kind not in (IncidentKind.POWER, IncidentKind.MACHINE) or outage.backup_low_at is not None:
            return []
        elapsed_minutes = (now - outage.started_at).total_seconds() / 60  # exact: whole minutes would fire a short backup early
        if p.backup_minutes == 0 or elapsed_minutes < p.backup_minutes - min(BACKUP_LOW_MINUTES, p.backup_minutes / 2):
            return []
        left = self._backup_left(p, outage, now)
        outage.backup_low_at = now
        outage.next_escalation_at = None  # everyone is told now; nobody is left to escalate to
        outage.escalation_step = len(p.contacts)
        still = "the power is still out" if outage.kind is IncidentKind.POWER else f"the {p.equipment.lower()} is still stopped"
        place = f", or get {p.name} to {p.plan.place_name}" if p.plan.place_name else ""
        is_open = outage.acknowledged_by is None
        message = (f"Lifeline URGENT: {p.name}'s {p.equipment.lower()} backup has about {max(1, left)} min left and {still} "
                   f"at {p.address}. Make sure someone is with {p.name} now{place}. If nobody can get there in time, call 911. "
                   + (f"Reply OK {outage.reply_code} if you're responding. " if is_open else "")
                   + f"Live status: {self._status_link(p)}")
        notifications = []
        for contact in p.contacts:
            if not outage.was_alerted(contact.phone):
                outage.alerted.append(Alerted(contact.name, contact.role, contact.phone, now))
            index = _alert_index(outage, contact.phone)
            notifications.append(Notification(contact, message, incident_id=outage.id if is_open else "", alert_index=index if is_open else -1))
        self._touch(outage)
        return notifications

    def _county_for(self, patient: Patient) -> list[Contact]:
        own = [c for c in patient.contacts if "county" in c.role.lower()]
        return own or list(self._county_contacts)

    @staticmethod
    def _minutes_left(patient: Patient, outage: Outage, now: datetime) -> float:
        return patient.backup_minutes - (now - outage.started_at).total_seconds() / 60

    def _is_critical(self, unit: Unit, now: datetime) -> bool:
        outage, p = unit.outage, unit.patient
        return (outage is not None and outage.kind in (IncidentKind.POWER, IncidentKind.MACHINE) and p.backup_minutes > 0
                and self._minutes_left(p, outage, now) <= CRITICAL_BACKUP_MINUTES)

    def _check_critical(self, unit: Unit, now: datetime) -> list[Notification]:
        """Fifteen minutes of backup left and the power still out: county emergency management and
        care staff are told once, whoever is responding, with 911 as the fallback. Lifeline can't
        place a 911 call itself; the message and the board's "Call 911" button put it one tap away."""
        outage, p = unit.outage, unit.patient
        if not self._is_critical(unit, now) or unit.critical_told_for == outage.id:
            return []
        unit.critical_told_for = outage.id
        left = max(0, int(self._minutes_left(p, outage, now)))
        still = "the power is still out" if outage.kind is IncidentKind.POWER else f"the {p.equipment.lower()} is still stopped"
        who = f"{outage.acknowledged_by} is responding" if outage.acknowledged_by else "Nobody on the call list has responded"
        message = (f"Lifeline EMERGENCY: {p.name}'s {p.equipment.lower()} has about {left} min of backup left and {still} "
                   f"at {p.address} ({p.lat:.4f}, {p.lon:.4f}). {who}. If nobody is with {p.name} now, call 911. "
                   f"Live status: {self._status_link(p)}")
        recipients = {c.phone: c for c in self._county_for(p) + [c for c in p.contacts if c.is_care_staff()]}
        return [Notification(c, message) for c in recipients.values()]

    def county_digest(self, now: datetime, sent_by: str) -> tuple[list[Notification], list[str]]:
        """One message per county contact listing the homes nobody on the call list has answered,
        for welfare checks. Also returns the names of unreached patients with no county contact."""
        lists: dict[str, tuple[Contact, list[tuple[float, str]]]] = {}
        no_county = []
        for unit in self._units.values():
            outage, p = unit.outage, unit.patient
            if outage is None or not outage.is_unreached(now, self._escalate_after):
                continue
            county = self._county_for(p)
            if not county:
                no_county.append(p.name)
                continue
            out_for = int((now - outage.started_at).total_seconds() // 60)
            left = f", about {max(0, int(self._minutes_left(p, outage, now)))} min of backup left" if p.backup_minutes else ""
            line = f"{p.name}, {p.address} ({p.equipment.lower()}{left}, out {out_for} min)"
            urgency = self._minutes_left(p, outage, now) if p.backup_minutes else float("inf")
            for contact in county:
                lists.setdefault(contact.phone, (contact, []))[1].append((urgency, line))
        notifications = []
        for contact, entries in lists.values():
            lines = [line for _, line in sorted(entries, key=lambda entry: entry[0])]  # least backup first
            numbered = " ".join(f"{i}) {line}." for i, line in enumerate(lines, 1))
            notifications.append(Notification(contact,
                f"Lifeline welfare check request from {sent_by}: nobody on the call list has answered for "
                f"{len(lines)} home{'s' if len(lines) != 1 else ''} with power-dependent patients. {numbered}"))
        return notifications, no_county

    def _check_unreached(self, unit: Unit, now: datetime) -> list[Notification]:
        """Nobody answered: tell care staff once, so someone is sent or the county is asked for a welfare check."""
        outage, p = unit.outage, unit.patient
        if outage is None or unit.unreached_told_for == outage.id or not outage.is_unreached(now, self._escalate_after):
            return []
        unit.unreached_told_for = outage.id
        what = {IncidentKind.POWER: "power-loss", IncidentKind.MACHINE: "machine-stopped", IncidentKind.HELP: "help-button"}[outage.kind]
        minutes = int((now - outage.started_at).total_seconds() // 60)
        after = f" after {minutes} min" if minutes else ""
        message = (f"Lifeline: Nobody has answered the {what} alert for {p.name} at {p.address}{after}, "
                   f"although everyone on the call list was alerted. Send someone now, or ask county emergency management "
                   f"for a welfare check. Reply OK {outage.reply_code} if you're responding. Live status: {self._status_link(p)}")
        alerted = [c for c in p.contacts if outage.was_alerted(c.phone)]
        recipients = [c for c in alerted if c.is_care_staff()] or alerted
        return [Notification(c, message, incident_id=outage.id, alert_index=_alert_index(outage, c.phone)) for c in recipients]

    def _unused_reply_code(self) -> str:
        """Codes must be unique among open outages or "OK <code>" would be ambiguous again."""
        in_use = {u.outage.reply_code for u in self._units.values() if u.outage}
        while (code := self._new_reply_code()) in in_use:
            pass
        return code

    # ---- internals: silent units ------------------------------------------------

    def _has_gone_silent(self, unit: Unit, now: datetime) -> bool:
        # Never-seen units are being set up, and a running power outage already has people
        # on the way, so neither gets a separate "unit is silent" alert.
        return (unit.last_seen is not None and unit.outage is None and unit.silent is None
                and now - unit.last_seen >= self._silent_alert_after)

    def _start_silence(self, unit: Unit, now: datetime) -> list[Notification]:
        p = unit.patient
        unit.silent = Outage(id=self._new_id(), kind=IncidentKind.SILENT, device_id=p.device_id,
                             patient_name=p.name, equipment=p.equipment, started_at=unit.last_seen)
        minutes = max(1, int((now - unit.last_seen).total_seconds() // 60))
        message = (f"Lifeline: The power unit at {p.name}'s home has been silent for {minutes} min. "
                   f"Its Wi-Fi or power may be down. Please check on {p.name}. Live status: {self._status_link(p)}")
        recipients = [c for c in p.contacts if c.is_care_staff()] or [p.contacts[0]]
        for contact in recipients:
            unit.silent.alerted.append(Alerted(contact.name, contact.role, contact.phone, now))
        self._touch(unit.silent)
        return [Notification(c, message) for c in recipients]

    def _end_silence(self, unit: Unit, now: datetime, announce: bool) -> list[Notification]:
        silent = unit.silent
        silent.ended_at = now
        self._touch(silent)
        unit.silent = None
        if not announce:
            return []
        message = f"Lifeline: The power unit at {unit.patient.name}'s home is reporting again, and the power is on."
        return [Notification(c, message) for c in unit.patient.contacts if silent.was_alerted(c.phone)]

    # ---- internals: room temperature ---------------------------------------------

    def _check_room(self, unit: Unit, now: datetime) -> list[Notification]:
        temp = unit.readings.room_temp_f
        out_of_range = temp is not None and not ROOM_TOO_COLD_F <= temp <= ROOM_TOO_HOT_F
        if not out_of_range:
            unit.room_out_of_range_since = None
            return self._end_room(unit, now) if unit.room is not None and temp is not None else []
        unit.room_out_of_range_since = unit.room_out_of_range_since or now
        if unit.room is not None or now - unit.room_out_of_range_since < ROOM_ALERT_AFTER:
            return []
        p = unit.patient
        hot = temp > ROOM_TOO_HOT_F
        unit.room = Outage(id=self._new_id(), kind=IncidentKind.TEMPERATURE, device_id=p.device_id, patient_name=p.name,
                           equipment=p.equipment, started_at=unit.room_out_of_range_since,
                           detail=f"Room {'too hot' if hot else 'too cold'}: {round(temp)}°F")
        message = (f"Lifeline: The room at {p.name}'s home is {round(temp)}°F, "
                   f"{'dangerously hot' if hot else 'dangerously cold'} for someone on {p.equipment.lower()}. "
                   f"Please check on {p.name}. Live status: {self._status_link(p)}")
        recipients = [c for c in p.contacts if c.is_care_staff()] or [p.contacts[0]]
        for contact in recipients:
            unit.room.alerted.append(Alerted(contact.name, contact.role, contact.phone, now))
        self._touch(unit.room)
        return [Notification(c, message) for c in recipients]

    def _end_room(self, unit: Unit, now: datetime) -> list[Notification]:
        room = unit.room
        room.ended_at = now
        self._touch(room)
        unit.room = None
        message = f"Lifeline: The room at {unit.patient.name}'s home is back to a safe temperature."
        return [Notification(c, message) for c in unit.patient.contacts if room.was_alerted(c.phone)]

    # ---- internals: persistence ----------------------------------------------------

    def _touch(self, incident: Outage) -> None:
        if not any(queued is incident for queued in self._changed):
            self._changed.append(incident)

    def _restore(self, incident: Outage) -> None:
        unit = self._units.get(incident.device_id)
        if unit is None or incident.ended_at is not None:
            return  # patient removed since, or already over
        if incident.kind in EMERGENCY_KINDS:
            unit.outage = incident
        elif incident.kind is IncidentKind.SILENT:
            unit.silent = incident
        elif incident.kind is IncidentKind.TEMPERATURE:
            unit.room = incident
        # Storm notices belong to StormWatch, which restores its own.

    # ---- internals: read model ------------------------------------------------------

    @staticmethod
    def _backup_left(patient: Patient, outage: Outage, now: datetime) -> int:
        elapsed = (now - outage.started_at).total_seconds() / 60
        return max(0, int(patient.backup_minutes - elapsed))

    def _row(self, unit: Unit, now: datetime) -> dict:
        p, outage = unit.patient, unit.outage
        # A restored outage has no report yet, but it is still an outage.
        if outage is not None:
            status = "outage"
        elif unit.last_seen is None:
            status = "never_seen"
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
            "rssi": unit.health.rssi,
            "firmware": unit.health.firmware,
            "uptime_s": unit.health.uptime_s,
            "silent_alert_sent": unit.silent is not None,
            "emergency": outage.kind.value if outage else None,
            "load_amps": unit.readings.load_amps,
            "room_temp_f": unit.readings.room_temp_f,
            "room_alert": unit.room.detail if unit.room else None,
            "outage_started": outage.started_at.isoformat() if outage else None,
            "backup_minutes_left": self._backup_left(p, outage, now) if outage else None,
            "acknowledged_by": outage.acknowledged_by if outage else None,
            "contacts_alerted": outage.alerted_names() if outage else [],
            **self._next_alert(unit),
            "unreached": outage.is_unreached(now, self._escalate_after) if outage else False,
            "backup_low": outage.backup_low_at is not None if outage else False,
            "critical": self._is_critical(unit, now),
            "attention": self._attention(unit, status),
        }

    @staticmethod
    def _next_alert(unit: Unit) -> dict:
        """Who the escalation reaches next and when, so staff can call them first. Names only:
        phone numbers are added by the delivery layer for roles allowed to see them."""
        outage, contacts = unit.outage, unit.patient.contacts
        if outage is None or not outage.is_escalating() or outage.escalation_step + 1 >= len(contacts):
            return {"next_alert_at": None, "next_contact": None}
        upcoming = contacts[outage.escalation_step + 1]
        return {"next_alert_at": outage.next_escalation_at.isoformat(),
                "next_contact": {"name": upcoming.name, "role": upcoming.role}}

    @staticmethod
    def _attention(unit: Unit, status: str) -> list[str]:
        """Problems to fix before the next outage. Outages and offline units have their own alarms."""
        problems = unit.patient.care_gaps()
        if status == "never_seen":
            problems.append("Unit has never connected")
        if unit.battery_percent is not None and unit.battery_percent < LOW_UNIT_BATTERY_PERCENT and status == "ok":
            problems.append("Unit battery low")
        if unit.health.rssi is not None and unit.health.rssi < WEAK_SIGNAL_DBM:
            problems.append("Weak Wi-Fi at the unit")
        return problems

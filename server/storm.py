"""Storm Watch: warn caregivers *before* the power goes out.

The map also shows what forecasters see: each home's current conditions and the
outlines of active warnings and watches (WeatherReport carries all three).

Three signals, each turned into a Hazard for one patient's home:
  1. official weather warnings at the home (tornado, ice storm, high wind...)
  2. the hourly forecast crossing outage-prone thresholds (strong gusts, ice build-up)
  3. other Lifeline homes nearby losing power in the last few minutes

Thresholds are heuristics, not a fitted model: research names wind gusts and ice
accretion as the strongest outage predictors, and these cut-offs follow the NWS
severe-wind criterion (58 mph) and the usual ice-storm-warning amount (1/4 inch).

No I/O here. A WeatherSource adapter fetches signals 1 and 2; the caller passes the
live board rows in for signal 3; StormWatch decides who to tell, once per hazard."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from enum import Enum
from typing import Callable, Iterable, Protocol

from domain import Alerted, IncidentKind, Notification, Outage, Patient, new_incident_id


class RiskLevel(Enum):
    ELEVATED = "elevated"  # shown on the board
    HIGH = "high"          # shown on the board and sent to the patient's first contact


@dataclass(frozen=True)
class Hazard:
    id: str          # stable across checks, so each hazard is announced once
    level: RiskLevel
    headline: str    # "Severe Thunderstorm Warning", "Gusts up to 61 mph forecast"
    starts: datetime
    ends: datetime


@dataclass(frozen=True)
class Conditions:
    """Right now at one home, from the hourly forecast grid. Any value may be missing."""
    temperature_f: int | None = None
    wind_mph: int | None = None
    gust_mph: int | None = None
    thunder_percent: int | None = None


@dataclass(frozen=True)
class WarningArea:
    """The outline of one active warning or watch, for drawing on the map."""
    id: str
    event: str
    level: RiskLevel
    ends: datetime
    geometry: dict  # GeoJSON Polygon or MultiPolygon, passed through untouched


@dataclass(frozen=True)
class WeatherReport:
    hazards: list[Hazard] = field(default_factory=list)
    conditions: Conditions | None = None
    areas: list[WarningArea] = field(default_factory=list)


class WeatherSource(Protocol):
    def report_at(self, lat: float, lon: float, now: datetime) -> WeatherReport: ...


class WeatherUnavailable(RuntimeError):
    pass


# ---- signal 1: official warnings ------------------------------------------------

# NWS event names that mean power lines are at risk. Everything else (floods, fog,
# small-craft advisories...) is ignored: Lifeline is about electricity, not weather.
ALERT_LEVELS = {
    "Tornado Warning": RiskLevel.HIGH,
    "Severe Thunderstorm Warning": RiskLevel.HIGH,
    "Extreme Wind Warning": RiskLevel.HIGH,
    "High Wind Warning": RiskLevel.HIGH,
    "Ice Storm Warning": RiskLevel.HIGH,
    "Winter Storm Warning": RiskLevel.HIGH,
    "Blizzard Warning": RiskLevel.HIGH,
    "Hurricane Warning": RiskLevel.HIGH,
    "Tropical Storm Warning": RiskLevel.HIGH,
    "Extreme Cold Warning": RiskLevel.HIGH,      # grid strain: rolling blackouts, as in Feb 2021
    "Extreme Heat Warning": RiskLevel.HIGH,
    "Excessive Heat Warning": RiskLevel.HIGH,    # older name for the same warning
    "Tornado Watch": RiskLevel.ELEVATED,
    "Severe Thunderstorm Watch": RiskLevel.ELEVATED,
    "Hurricane Watch": RiskLevel.ELEVATED,
    "Tropical Storm Watch": RiskLevel.ELEVATED,
    "Winter Storm Watch": RiskLevel.ELEVATED,
    "High Wind Watch": RiskLevel.ELEVATED,
    "Wind Advisory": RiskLevel.ELEVATED,
    "Winter Weather Advisory": RiskLevel.ELEVATED,
    "Extreme Cold Watch": RiskLevel.ELEVATED,
}


def alert_hazard(alert_id: str, event: str, starts: datetime, ends: datetime) -> Hazard | None:
    level = ALERT_LEVELS.get(event)
    if level is None:
        return None
    return Hazard(id=f"alert:{alert_id}", level=level, headline=event, starts=starts, ends=ends)


# ---- signal 2: forecast thresholds ------------------------------------------------

KMH_PER_MPH = 1.609344
MM_PER_INCH = 25.4
GUST_ELEVATED_MPH = 40
GUST_HIGH_MPH = 58
ICE_HIGH_INCHES = 0.25
FORECAST_HORIZON = timedelta(hours=24)


@dataclass(frozen=True)
class ForecastPeriod:
    """One forecast value valid from `starts` for `duration` (NWS gives ranges, not hours)."""
    starts: datetime
    duration: timedelta
    value: float

    @property
    def ends(self) -> datetime:
        return self.starts + self.duration


def assess_forecast(gusts_kmh: Iterable[ForecastPeriod], ice_mm: Iterable[ForecastPeriod],
                    now: datetime) -> list[Hazard]:
    horizon = now + FORECAST_HORIZON
    hazards = []
    upcoming_gusts = [p for p in gusts_kmh if p.ends > now and p.starts < horizon]
    strongest = max(upcoming_gusts, key=lambda p: p.value, default=None)
    if strongest is not None:
        mph = strongest.value / KMH_PER_MPH
        if mph >= GUST_ELEVATED_MPH:
            level = RiskLevel.HIGH if mph >= GUST_HIGH_MPH else RiskLevel.ELEVATED
            windy = [p for p in upcoming_gusts if p.value / KMH_PER_MPH >= GUST_ELEVATED_MPH]
            hazards.append(Hazard(
                id=f"forecast:gust:{level.value}:{windy[0].starts.date().isoformat()}", level=level,
                headline=f"Gusts up to {round(mph)} mph forecast",
                starts=windy[0].starts, ends=max(p.ends for p in windy)))
    icy = [p for p in ice_mm if p.ends > now and p.starts < horizon and p.value > 0]
    total_inches = sum(p.value for p in icy) / MM_PER_INCH
    if total_inches >= ICE_HIGH_INCHES:
        hazards.append(Hazard(
            id=f"forecast:ice:{icy[0].starts.date().isoformat()}", level=RiskLevel.HIGH,
            headline=f"About {total_inches:.2f} in of ice forecast",
            starts=icy[0].starts, ends=max(p.ends for p in icy)))
    return hazards


# ---- signal 3: outages spreading nearby -------------------------------------------

NEARBY_KM = 5.0
NEARBY_WINDOW = timedelta(minutes=30)
NEARBY_MIN_HOMES = 2


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance; plenty precise at neighbourhood scale."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def nearby_outage_hazard(device_id: str, board: list[dict], now: datetime) -> Hazard | None:
    """Power is still on here, but several Lifeline homes close by just lost it."""
    me = next((row for row in board if row["device_id"] == device_id), None)
    if me is None or me["status"] == "outage":
        return None
    here = (me["lat"], me["lon"])
    recent = [row for row in board
              if row["device_id"] != device_id and row["status"] == "outage"
              and now - datetime.fromisoformat(row["outage_started"]) <= NEARBY_WINDOW
              and distance_km(here, (row["lat"], row["lon"])) <= NEARBY_KM]
    if len(recent) < NEARBY_MIN_HOMES:
        return None
    first = min(datetime.fromisoformat(row["outage_started"]) for row in recent)
    miles = round(NEARBY_KM / KMH_PER_MPH)
    return Hazard(id=f"nearby:{first.isoformat()}", level=RiskLevel.HIGH,
                  headline=f"Power is out at {len(recent)} other Lifeline homes within {miles} miles",
                  starts=first, ends=now + NEARBY_WINDOW)


# ---- deciding who to tell ----------------------------------------------------------

@dataclass
class _Watch:
    hazards: list[Hazard] = field(default_factory=list)
    notices: dict[str, Outage] = field(default_factory=dict)  # hazard id -> open storm incident


class StormWatch:
    """Current hazards per home, and the storm notices sent about them. A HIGH hazard
    is announced once to the patient's first contact and kept as a `storm` incident
    (for the history report) until it clears. Pass saved open notices back in after
    a restart so nobody is told twice."""

    def __init__(self, timezone: tzinfo, open_notices: Iterable[Outage] = (),
                 new_id: Callable[[], str] = new_incident_id):
        self._timezone = timezone
        self._new_id = new_id
        self._watches: dict[str, _Watch] = {}
        self._changed: list[Outage] = []
        for notice in open_notices:
            if notice.kind is IncidentKind.STORM and notice.ended_at is None:
                self._watch(notice.device_id).notices[notice.hazard_id] = notice

    def update(self, patient: Patient, hazards: list[Hazard], now: datetime) -> list[Notification]:
        watch = self._watch(patient.device_id)
        watch.hazards = sorted((h for h in hazards if h.ends > now), key=lambda h: (h.level != RiskLevel.HIGH, h.starts))
        current = {h.id for h in watch.hazards}
        for hazard_id in [i for i in watch.notices if i not in current]:
            notice = watch.notices.pop(hazard_id)
            notice.ended_at = max(now, notice.started_at)
            self._changed.append(notice)
        notifications = []
        for hazard in watch.hazards:
            if hazard.level is RiskLevel.HIGH and hazard.id not in watch.notices:
                notifications.append(self._announce(patient, hazard, watch, now))
        return notifications

    def forget(self, device_id: str, now: datetime) -> None:
        """The patient was removed: close their open notices so the history isn't left hanging."""
        watch = self._watches.pop(device_id, None)
        for notice in (watch.notices.values() if watch else []):
            notice.ended_at = max(now, notice.started_at)
            self._changed.append(notice)

    def hazards(self, device_id: str) -> list[Hazard]:
        watch = self._watches.get(device_id)
        return list(watch.hazards) if watch else []

    def drain_changes(self) -> list[Outage]:
        """Copies, so the saver thread never sees a notice mid-change."""
        changed, self._changed = self._changed, []
        return [copy.deepcopy(notice) for notice in changed]

    def _watch(self, device_id: str) -> _Watch:
        return self._watches.setdefault(device_id, _Watch())

    def _announce(self, patient: Patient, hazard: Hazard, watch: _Watch, now: datetime) -> Notification:
        contact = patient.contacts[0]
        until = hazard.ends.astimezone(self._timezone).strftime("%-I:%M %p")
        message = (f"Lifeline storm watch: {hazard.headline} for {patient.name}'s area until {until}. "
                   f"{patient.equipment} backup is about {patient.backup_minutes} min. "
                   "Charge batteries and check supplies now. If the power goes out, we'll alert you right away.")
        notice = Outage(id=self._new_id(), kind=IncidentKind.STORM, device_id=patient.device_id,
                        patient_name=patient.name, equipment=patient.equipment, started_at=now,
                        detail=hazard.headline, hazard_id=hazard.id,
                        alerted=[Alerted(contact.name, contact.role, contact.phone, now)])
        watch.notices[hazard.id] = notice
        self._changed.append(notice)
        return Notification(contact, message)

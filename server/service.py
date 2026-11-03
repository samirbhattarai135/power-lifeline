"""Application use cases: what devices, staff and families can do. Owns the
locking around the Monitor, hands notifications to a Notifier off-thread, and
saves every changed incident off-thread. Storage arrives through the
PatientStore and OutageStore ports, so this module never knows whether data
lives in JSON files or in Supabase."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from domain import REPLY_SUFFIX, InvalidPatient, Monitor, Notification, Outage, Patient, ReplyOutcome, UnitHealth, UnknownDevice
from records import patient_from_form
from storm import StormWatch, WeatherReport, WeatherSource, WeatherUnavailable, nearby_outage_hazard

log = logging.getLogger("lifeline.service")


class PatientStore(Protocol):
    def list_all(self) -> list[Patient]: ...
    def save(self, patient: Patient) -> None: ...
    def delete(self, device_id: str) -> None: ...


class OutageStore(Protocol):
    def list_open(self) -> list[Outage]: ...
    def list_since(self, since: datetime) -> list[Outage]: ...
    def save(self, outage: Outage) -> None: ...


class Notifier(Protocol):
    def send(self, notification: Notification) -> None: ...


class DuplicateDevice(InvalidPatient):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_token() -> str:
    return secrets.token_urlsafe(24)


SAVE_ATTEMPTS = 3
SAVE_BACKOFF_SECONDS = 1.0


class Lifeline:
    def __init__(self, monitor: Monitor, store: PatientStore, notifier: Notifier, outages: OutageStore,
                 storm: StormWatch | None = None, weather: WeatherSource | None = None):
        """Pass `storm` and `weather` together to turn Storm Watch on."""
        self._monitor = monitor
        self._store = store
        self._notifier = notifier
        self._outages = outages
        self._storm = storm
        self._weather_source = weather
        self._weather: dict[str, WeatherReport] = {}  # device_id -> last report, kept if a fetch fails
        self._weather_checked_at: datetime | None = None
        # ponytail: one lock for every unit; fine for hundreds of patients, shard per unit if it grows.
        self._lock = threading.Lock()
        # Serialises roster edits (which do slow store I/O) without blocking device reports.
        self._roster_lock = threading.Lock()
        self._share_index = {p.share_token: p.device_id for p in monitor.patients()}
        self._senders = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")
        # One worker, so saves of the same incident land in the order they happened.
        self._savers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="save")

    # ---- devices ----------------------------------------------------------

    def device_token(self, device_id: str) -> str:
        with self._lock:
            return self._monitor.patient(device_id).device_token

    def report(self, device_id: str, has_wall_power: bool, battery_percent: int | None,
               health: UnitHealth | None = None) -> None:
        with self._lock:
            notifications = self._monitor.report(device_id, has_wall_power, battery_percent, utc_now(), health)
            self._save(self._monitor.drain_changes())
        self._dispatch(notifications)

    def tick(self) -> None:
        now = utc_now()
        with self._lock:
            notifications = self._monitor.tick(now)
            self._save(self._monitor.drain_changes())
            notifications += self._update_storm_watch(now)
        self._dispatch(notifications)

    # ---- storm watch ---------------------------------------------------------

    def refresh_weather(self) -> None:
        """Fetch a weather report for every home. Slow network I/O, so never under the lock;
        tick() combines the hazards with nearby outages and decides who to tell."""
        if self._weather_source is None:
            return
        with self._lock:
            homes = [(p.device_id, p.lat, p.lon) for p in self._monitor.patients()]
        fetched = {}
        for device_id, lat, lon in homes:
            try:
                fetched[device_id] = self._weather_source.report_at(lat, lon, utc_now())
            except WeatherUnavailable as error:
                log.warning("storm watch: no weather for %s this round: %s", device_id, error)
        with self._lock:
            self._weather.update(fetched)
            if fetched:
                self._weather_checked_at = utc_now()

    def weather_map(self) -> dict:
        """Active warning and watch outlines near the homes, as GeoJSON, plus when we last looked."""
        now = utc_now()
        with self._lock:
            areas = {area.id: area for report in self._weather.values() for area in report.areas if area.ends > now}
            checked_at = self._weather_checked_at
        return {
            "checked_at": checked_at.isoformat() if checked_at else None,
            "areas": {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": area.geometry,
                 "properties": {"event": area.event, "level": area.level.value, "ends": area.ends.isoformat()}}
                for area in sorted(areas.values(), key=lambda a: a.level.value)  # "elevated" first, so warnings draw on top
            ]},
        }

    def _update_storm_watch(self, now: datetime) -> list[Notification]:
        """Call with self._lock held."""
        if self._storm is None:
            return []
        board = self._monitor.snapshot(now)
        notifications = []
        # ponytail: every home against every home each second; fine for hundreds, index by area beyond that.
        for patient in self._monitor.patients():
            report = self._weather.get(patient.device_id)
            hazards = list(report.hazards) if report else []
            nearby = nearby_outage_hazard(patient.device_id, board, now)
            if nearby is not None:
                hazards.append(nearby)
            notifications += self._storm.update(patient, hazards, now)
        self._save(self._storm.drain_changes())
        return notifications

    def _hazard_rows(self, device_id: str) -> list[dict]:
        if self._storm is None:
            return []
        return [{"level": h.level.value, "headline": h.headline, "starts": h.starts.isoformat(), "ends": h.ends.isoformat()}
                for h in self._storm.hazards(device_id)]

    # ---- acknowledgments ----------------------------------------------------

    def acknowledge(self, device_id: str, by: str) -> bool:
        with self._lock:
            acknowledged = self._monitor.acknowledge(device_id, by, utc_now())
            self._save(self._monitor.drain_changes())
        return acknowledged

    def acknowledge_by_reply(self, phone: str, text: str) -> ReplyOutcome:
        with self._lock:
            outcome = self._monitor.acknowledge_by_reply(phone, text, utc_now())
            self._save(self._monitor.drain_changes())
        return outcome

    # ---- family status page -------------------------------------------------

    def family_status(self, share_token: str) -> dict | None:
        """What a family member may see: their one patient, no tokens, no other patients."""
        with self._lock:
            device_id = self._share_index.get(share_token)
            if device_id is None:
                return None
            row = self._monitor.status(device_id, utc_now())
            hazards = self._hazard_rows(device_id)
        status = {key: row[key] for key in (
            "name", "equipment", "status", "backup_minutes", "backup_minutes_left",
            "outage_started", "contacts_alerted", "last_seen",
        )}
        status["hazards"] = hazards
        status["acknowledged_by"] = _responder_for_family(row["acknowledged_by"])
        return status

    def family_acknowledge(self, share_token: str) -> bool:
        with self._lock:
            device_id = self._share_index.get(share_token)
            if device_id is None:
                return False
            acknowledged = self._monitor.acknowledge(device_id, "Family (status page)", utc_now())
            self._save(self._monitor.drain_changes())
        return acknowledged

    # ---- staff: live board and roster --------------------------------------

    def snapshot(self) -> list[dict]:
        with self._lock:
            rows = self._monitor.snapshot(utc_now())
            for row in rows:
                row["hazards"] = self._hazard_rows(row["device_id"])
                report = self._weather.get(row["device_id"])
                row["conditions"] = asdict(report.conditions) if report and report.conditions else None
            return rows

    def history(self, days: int, device_id: str | None = None) -> list[Outage]:
        """Incidents that started in the last `days` days, newest first."""
        incidents = self._outages.list_since(utc_now() - timedelta(days=days))
        if device_id is not None:
            incidents = [i for i in incidents if i.device_id == device_id]
        return sorted(incidents, key=lambda i: i.started_at, reverse=True)

    def roster(self) -> list[Patient]:
        with self._lock:
            return self._monitor.patients()

    def add_patient(self, form: dict[str, Any]) -> Patient:
        with self._roster_lock:
            patient = patient_from_form(form, device_token=new_token(), share_token=new_token())
            with self._lock:
                if self._monitor.has_device(patient.device_id):
                    raise DuplicateDevice(f"Unit ID {patient.device_id} is already in use")
            self._store.save(patient)  # persist first: a failed write must not leave a ghost patient
            with self._lock:
                self._monitor.upsert_patient(patient)
                self._share_index[patient.share_token] = patient.device_id
            return patient

    def update_patient(self, device_id: str, form: dict[str, Any]) -> Patient:
        with self._roster_lock:
            with self._lock:
                existing = self._monitor.patient(device_id)
            # Staff edits never change the unit ID or the credentials.
            patient = patient_from_form({**form, "device_id": device_id}, existing.device_token, existing.share_token)
            self._store.save(patient)
            with self._lock:
                self._monitor.upsert_patient(patient)
            return patient

    def rotate_share_link(self, device_id: str) -> Patient:
        """Invalidate a forwarded or leaked family link."""
        with self._roster_lock:
            with self._lock:
                existing = self._monitor.patient(device_id)
            patient = replace(existing, share_token=new_token())
            self._store.save(patient)
            with self._lock:
                self._monitor.upsert_patient(patient)
                self._share_index.pop(existing.share_token, None)
                self._share_index[patient.share_token] = device_id
            return patient

    def remove_patient(self, device_id: str) -> None:
        with self._roster_lock:
            with self._lock:
                existing = self._monitor.patient(device_id)
            self._store.delete(device_id)
            with self._lock:
                self._monitor.remove_patient(device_id)
                self._share_index.pop(existing.share_token, None)
                self._weather.pop(device_id, None)
                if self._storm is not None:
                    self._storm.forget(device_id, utc_now())
                    self._save(self._storm.drain_changes())

    # ---- internals --------------------------------------------------------

    def _dispatch(self, notifications: list[Notification]) -> None:
        for notification in notifications:
            self._senders.submit(self._notifier.send, notification).add_done_callback(_log_send_failure)

    def _save(self, changes: list[Outage]) -> None:
        # Call with self._lock held: queueing in lock order keeps an older snapshot from
        # overwriting a newer one. submit() only enqueues; the write runs on the saver thread.
        for incident in changes:
            self._savers.submit(self._save_with_retry, incident)

    def _save_with_retry(self, incident: Outage) -> None:
        """A failed save must never stop an alert, so it is retried and logged, not raised.
        If every attempt fails, only restart recovery and the report lose this change."""
        for attempt in range(1, SAVE_ATTEMPTS + 1):
            try:
                self._outages.save(incident)
                return
            except Exception as error:  # any storage failure: network, HTTP, disk
                if attempt == SAVE_ATTEMPTS:
                    log.error("could not save incident %s for %s: %s", incident.id, incident.device_id, error)
                    return
                time.sleep(SAVE_BACKOFF_SECONDS * 2 ** (attempt - 1))

    def wait_for_saves(self) -> None:
        """Block until queued saves finish. For tests and orderly shutdown."""
        self._savers.submit(lambda: None).result()


def _responder_for_family(acknowledged_by: str | None) -> str | None:
    """Families see who is responding, not staff emails or phone numbers."""
    if acknowledged_by is None:
        return None
    if acknowledged_by.endswith("(agency)"):
        return "The care agency"
    if acknowledged_by.endswith(REPLY_SUFFIX):
        return "Someone on the call list"
    return "A family member"


def _log_send_failure(future: Future) -> None:
    if future.exception() is not None:
        log.error("notification failed", exc_info=future.exception())


__all__ = ["Lifeline", "PatientStore", "OutageStore", "Notifier", "DuplicateDevice", "UnknownDevice", "InvalidPatient", "utc_now"]

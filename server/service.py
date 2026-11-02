"""Application use cases: what devices, staff and families can do. Owns the
locking around the Monitor and hands notifications to a Notifier off-thread.
Storage arrives through the PatientStore port, so this module never knows
whether patients live in a JSON file or in Supabase."""

from __future__ import annotations

import logging
import secrets
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol

from domain import InvalidPatient, Monitor, Notification, Patient, UnknownDevice
from records import patient_from_form

log = logging.getLogger("lifeline.service")


class PatientStore(Protocol):
    def list_all(self) -> list[Patient]: ...
    def save(self, patient: Patient) -> None: ...
    def delete(self, device_id: str) -> None: ...


class Notifier(Protocol):
    def send(self, notification: Notification) -> None: ...


class DuplicateDevice(InvalidPatient):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_token() -> str:
    return secrets.token_urlsafe(24)


class Lifeline:
    def __init__(self, monitor: Monitor, store: PatientStore, notifier: Notifier):
        self._monitor = monitor
        self._store = store
        self._notifier = notifier
        # ponytail: one lock for every unit; fine for hundreds of patients, shard per unit if it grows.
        self._lock = threading.Lock()
        # Serialises roster edits (which do slow store I/O) without blocking device reports.
        self._roster_lock = threading.Lock()
        self._share_index = {p.share_token: p.device_id for p in monitor.patients()}
        self._senders = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")

    # ---- devices ----------------------------------------------------------

    def device_token(self, device_id: str) -> str:
        with self._lock:
            return self._monitor.patient(device_id).device_token

    def report(self, device_id: str, has_wall_power: bool, battery_percent: int | None) -> None:
        with self._lock:
            notifications = self._monitor.report(device_id, has_wall_power, battery_percent, utc_now())
        self._dispatch(notifications)

    def tick(self) -> None:
        with self._lock:
            notifications = self._monitor.tick(utc_now())
        self._dispatch(notifications)

    # ---- acknowledgments ----------------------------------------------------

    def acknowledge(self, device_id: str, by: str) -> bool:
        with self._lock:
            return self._monitor.acknowledge(device_id, by)

    def acknowledge_by_phone(self, phone: str) -> str | None:
        with self._lock:
            return self._monitor.acknowledge_by_phone(phone)

    # ---- family status page -------------------------------------------------

    def family_status(self, share_token: str) -> dict | None:
        """What a family member may see: their one patient, no tokens, no other patients."""
        with self._lock:
            device_id = self._share_index.get(share_token)
            if device_id is None:
                return None
            row = self._monitor.status(device_id, utc_now())
        status = {key: row[key] for key in (
            "name", "equipment", "status", "backup_minutes", "backup_minutes_left",
            "outage_started", "contacts_alerted", "last_seen",
        )}
        status["acknowledged_by"] = _responder_for_family(row["acknowledged_by"])
        return status

    def family_acknowledge(self, share_token: str) -> bool:
        with self._lock:
            device_id = self._share_index.get(share_token)
            if device_id is None:
                return False
            return self._monitor.acknowledge(device_id, by="Family (status page)")

    # ---- staff: live board and roster --------------------------------------

    def snapshot(self) -> list[dict]:
        with self._lock:
            return self._monitor.snapshot(utc_now())

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

    # ---- internals --------------------------------------------------------

    def _dispatch(self, notifications: list[Notification]) -> None:
        for notification in notifications:
            self._senders.submit(self._notifier.send, notification).add_done_callback(_log_send_failure)


def _responder_for_family(acknowledged_by: str | None) -> str | None:
    """Families see who is responding, not staff emails or phone numbers."""
    if acknowledged_by is None:
        return None
    if acknowledged_by.endswith("(agency)"):
        return "The care agency"
    if acknowledged_by.startswith("+"):
        return "Someone on the call list"
    return "A family member"


def _log_send_failure(future: Future) -> None:
    if future.exception() is not None:
        log.error("notification failed", exc_info=future.exception())


__all__ = ["Lifeline", "PatientStore", "Notifier", "DuplicateDevice", "UnknownDevice", "InvalidPatient", "utc_now"]

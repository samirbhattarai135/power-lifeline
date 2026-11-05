"""Offline adapters for development before Supabase is set up: patients and
incidents in JSON files, and one shared staff password from the environment."""

from __future__ import annotations

import hmac
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from access import Role, StaffAccount
from domain import Outage, Patient
from records import outage_from_record, outage_to_record, patient_from_record, patient_to_record
from staff import PasswordChangeRefused, StaffMember


class JsonFilePatientStore:
    def __init__(self, path: Path):
        self._path = path

    def list_all(self) -> list[Patient]:
        if not self._path.exists():
            return []
        return [patient_from_record(r) for r in json.loads(self._path.read_text())]

    def save(self, patient: Patient) -> None:
        records = {p.device_id: patient_to_record(p) for p in self.list_all()}
        records[patient.device_id] = patient_to_record(patient)
        self._write(list(records.values()))

    def delete(self, device_id: str) -> None:
        self._write([patient_to_record(p) for p in self.list_all() if p.device_id != device_id])

    def _write(self, records: list[dict]) -> None:
        _write_json_atomically(self._path, records)


class JsonFileOutageStore:
    """ponytail: rewrites the whole file per change; fine for a demo, use Supabase beyond that."""

    def __init__(self, path: Path):
        self._path = path

    def list_open(self) -> list[Outage]:
        return [o for o in self._all() if o.ended_at is None]

    def list_since(self, since: datetime) -> list[Outage]:
        return [o for o in self._all() if o.started_at >= since]

    def save(self, outage: Outage) -> None:
        records = {o.id: outage_to_record(o) for o in self._all()}
        records[outage.id] = outage_to_record(outage)
        _write_json_atomically(self._path, list(records.values()))

    def _all(self) -> list[Outage]:
        if not self._path.exists():
            return []
        return [outage_from_record(r) for r in json.loads(self._path.read_text())]


class JsonFileStaffStore:
    def __init__(self, path: Path):
        self._path = path

    def list_all(self) -> list[StaffAccount]:
        if not self._path.exists():
            return []
        return [StaffAccount(r["email"], Role(r["role"])) for r in json.loads(self._path.read_text())]

    def save(self, account: StaffAccount) -> None:
        accounts = {a.email: a for a in self.list_all()} | {account.email: account}
        _write_json_atomically(self._path, [{"email": a.email, "role": a.role.value} for a in accounts.values()])

    def delete(self, email: str) -> None:
        _write_json_atomically(self._path, [{"email": a.email, "role": a.role.value} for a in self.list_all() if a.email != email])


def _write_json_atomically(path: Path, data: list[dict]) -> None:
    # Write-then-rename so a crash mid-write never leaves a half-written file.
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temp_path, path)


class SharedPasswordAuthenticator:
    """Any email plus the one shared password. For local development only."""

    def __init__(self, password: str):
        if len(password) < 8:
            raise ValueError("LIFELINE_ADMIN_PASSWORD must be at least 8 characters")
        self._password = password

    def sign_in(self, email: str, password: str) -> StaffMember | None:
        if email and hmac.compare_digest(password.encode(), self._password.encode()):
            return StaffMember(email=email)
        return None

    def create(self, email: str, temporary_password: str) -> bool:
        return False  # everyone signs in with the shared password; there is no account to make

    def change_password(self, email: str, current_password: str, new_password: str) -> bool:
        raise PasswordChangeRefused("Local mode uses one shared password, set in LIFELINE_ADMIN_PASSWORD.")

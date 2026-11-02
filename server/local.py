"""Offline adapters for development before Supabase is set up: patients in a JSON
file, and one shared staff password from the environment."""

from __future__ import annotations

import hmac
import json
import os
import tempfile
from pathlib import Path

from domain import Patient
from records import patient_from_record, patient_to_record
from staff import StaffMember


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
        # Write-then-rename so a crash mid-write never leaves a half-written roster.
        fd, temp_path = tempfile.mkstemp(dir=self._path.parent, prefix=".patients-")
        with os.fdopen(fd, "w") as handle:
            json.dump(records, handle, indent=2)
        os.replace(temp_path, self._path)


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

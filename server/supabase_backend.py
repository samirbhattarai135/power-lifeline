"""Supabase adapters: the `patients` and `outages` tables through PostgREST, and staff sign-in
through Supabase Auth. Uses the server-only secret key; that key bypasses row
level security, which is why the table has RLS on with no policies (see
supabase/migrations). Never send this key to a browser."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from domain import Outage, Patient
from records import outage_from_record, outage_to_record, patient_from_record, patient_to_record
from staff import StaffMember

log = logging.getLogger("lifeline.supabase")
TIMEOUT_SECONDS = 10


class SupabaseError(RuntimeError):
    pass


class SupabaseUnreachable(SupabaseError):
    """The network failed (connect, TLS handshake or a read timing out); worth retrying."""


class SupabaseClient:
    def __init__(self, project_url: str, secret_key: str):
        if not project_url.startswith("https://"):
            raise ValueError("SUPABASE_URL must start with https://")
        self._base = project_url.rstrip("/")
        self._headers = {"apikey": secret_key, "Content-Type": "application/json"}
        # Legacy service_role keys are JWTs and also go in Authorization;
        # the newer sb_secret_ keys are not JWTs and must only be sent as apikey.
        if secret_key.startswith("eyJ"):
            self._headers["Authorization"] = f"Bearer {secret_key}"

    def request(self, method: str, path: str, body: Any = None,
                extra_headers: dict[str, str] | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self._base + path, data=data, method=method,
                                         headers={**self._headers, **(extra_headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"message": raw[:200].decode(errors="replace")}
        except urllib.error.URLError as error:
            raise SupabaseUnreachable(f"cannot reach Supabase: {error.reason}") from error
        except (TimeoutError, OSError) as error:  # a timeout mid-response isn't wrapped in URLError
            raise SupabaseUnreachable(f"cannot reach Supabase: {error}") from error


class SupabasePatientStore:
    TABLE = "/rest/v1/patients"

    def __init__(self, client: SupabaseClient):
        self._client = client

    def list_all(self) -> list[Patient]:
        status, rows = self._client.request("GET", f"{self.TABLE}?select=*")
        _expect(status, rows, "load patients")
        return [patient_from_record(row) for row in rows]

    def save(self, patient: Patient) -> None:
        status, body = self._client.request(
            "POST", self.TABLE, [patient_to_record(patient)],
            {"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        _expect(status, body, f"save patient {patient.device_id}")

    def delete(self, device_id: str) -> None:
        status, body = self._client.request("DELETE", f"{self.TABLE}?device_id=eq.{urllib.parse.quote(device_id)}")
        _expect(status, body, f"delete patient {device_id}")


class SupabaseOutageStore:
    TABLE = "/rest/v1/outages"

    def __init__(self, client: SupabaseClient):
        self._client = client

    def list_open(self) -> list[Outage]:
        return self._list("ended_at=is.null", "load open incidents")

    def list_since(self, since: datetime) -> list[Outage]:
        return self._list(f"started_at=gte.{urllib.parse.quote(since.isoformat())}&order=started_at.desc",
                          "load incident history")

    def save(self, outage: Outage) -> None:
        status, body = self._client.request(
            "POST", self.TABLE, [outage_to_record(outage)],
            {"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        _expect(status, body, f"save incident {outage.id}")

    def _list(self, query: str, action: str) -> list[Outage]:
        status, rows = self._client.request("GET", f"{self.TABLE}?select=*&{query}")
        _expect(status, rows, action)
        return [outage_from_record(row) for row in rows]


def _expect(status: int, body: Any, action: str) -> None:
    if not 200 <= status < 300:
        detail = body.get("message") if isinstance(body, dict) else body
        raise SupabaseError(f"Supabase could not {action}: HTTP {status} {detail}")


class SupabaseAuthenticator:
    """Email + password accounts from the project's Supabase Auth users."""

    def __init__(self, client: SupabaseClient):
        self._client = client

    def sign_in(self, email: str, password: str) -> StaffMember | None:
        if not email or not password:
            return None
        status, body = self._client.request("POST", "/auth/v1/token?grant_type=password",
                                            {"email": email, "password": password})
        if status == 200 and isinstance(body, dict) and body.get("user"):
            return StaffMember(email=body["user"].get("email", email))
        if status >= 500:
            raise SupabaseError(f"Supabase Auth unavailable: HTTP {status}")
        return None

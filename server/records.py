"""The one place that knows the stored shapes. A patient is a flat dict with a
`contacts` list; both the JSON file and the Supabase `patients` table use it, and
the staff form posts the same shape minus the two tokens. An incident (power
outage or silent unit) is a flat dict with an `alerted` list, stored in `outages`."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from domain import Alerted, Channel, Contact, EmergencyPlan, IncidentKind, InvalidPatient, Outage, Patient


def patient_to_record(patient: Patient) -> dict[str, Any]:
    return {
        "device_id": patient.device_id,
        "device_token": patient.device_token,
        "share_token": patient.share_token,
        "name": patient.name,
        "address": patient.address,
        "lat": patient.lat,
        "lon": patient.lon,
        "equipment": patient.equipment,
        "backup_minutes": patient.backup_minutes,
        "acuity": patient.acuity,
        "contacts": [
            {"role": c.role, "name": c.name, "phone": c.phone, "channel": c.channel.value}
            for c in patient.contacts
        ],
        # Columns from migration 004; left out while unused so saves keep working before it is run.
        **({"plan": asdict(patient.plan)} if not patient.plan.is_empty() else {}),
        **({"running_amps_min": patient.running_amps_min} if patient.running_amps_min is not None else {}),
    }


def patient_from_record(record: dict[str, Any]) -> Patient:
    return patient_from_form(record, record.get("device_token", ""), record.get("share_token", ""))


def patient_from_form(form: dict[str, Any], device_token: str, share_token: str) -> Patient:
    """Parse untrusted input into a Patient. Raises InvalidPatient with a staff-readable message."""
    if not isinstance(form, dict):
        raise InvalidPatient("Expected a patient object")
    try:
        raw_contacts = form["contacts"]
        if not isinstance(raw_contacts, list):
            raise InvalidPatient("Contacts must be a list")
        contacts = tuple(_contact(c) for c in raw_contacts)
        return Patient(
            device_id=_text(form, "device_id").strip().lower(),
            device_token=device_token,
            share_token=share_token,
            name=_text(form, "name").strip(),
            address=_text(form, "address").strip(),
            lat=float(form["lat"]),
            lon=float(form["lon"]),
            equipment=_text(form, "equipment").strip(),
            backup_minutes=int(form["backup_minutes"]),
            acuity=int(form["acuity"]),
            contacts=contacts,
            plan=_plan(form.get("plan")),
            running_amps_min=_optional_float(form.get("running_amps_min")),
        )
    except KeyError as missing:
        raise InvalidPatient(f"Missing field: {missing.args[0]}") from None
    except (TypeError, ValueError) as error:
        if isinstance(error, InvalidPatient):
            raise
        raise InvalidPatient("Check the numbers: location, backup minutes and acuity must be numeric") from None


def _plan(raw: Any) -> EmergencyPlan:
    if raw in (None, ""):
        return EmergencyPlan()
    if not isinstance(raw, dict):
        raise InvalidPatient("The emergency plan must be an object")
    known = {name: raw.get(name, "") for name in EmergencyPlan.__dataclass_fields__}
    return EmergencyPlan(**{name: value.strip() if isinstance(value, str) else value for name, value in known.items()})


def _optional_float(raw: Any) -> float | None:
    return None if raw in (None, "") else float(raw)


def _contact(raw: Any) -> Contact:
    if not isinstance(raw, dict):
        raise InvalidPatient("Each contact must be an object")
    try:
        channel = Channel(raw.get("channel"))
    except ValueError:
        raise InvalidPatient("Contact channel must be 'sms' or 'call'") from None
    phone = "".join(ch for ch in _text(raw, "phone") if ch == "+" or ch.isdigit())
    return Contact(_text(raw, "role").strip(), _text(raw, "name").strip(), phone, channel)


def _text(source: dict[str, Any], key: str) -> str:
    value = source[key]
    if not isinstance(value, str):
        raise InvalidPatient(f"{key} must be text")
    return value


def outage_to_record(outage: Outage) -> dict[str, Any]:
    return {
        "id": outage.id,
        "kind": outage.kind.value,
        "device_id": outage.device_id,
        "patient_name": outage.patient_name,
        "equipment": outage.equipment,
        "started_at": outage.started_at.isoformat(),
        "ended_at": _iso(outage.ended_at),
        "reply_code": outage.reply_code,
        "escalation_step": outage.escalation_step,
        "next_escalation_at": _iso(outage.next_escalation_at),
        "acknowledged_by": outage.acknowledged_by,
        "acknowledged_at": _iso(outage.acknowledged_at),
        "alerted": [{"name": a.name, "role": a.role, "phone": a.phone, "at": a.at.isoformat()} for a in outage.alerted],
        # Storm-only columns come from migration 003; leaving them out of power and silent
        # incidents keeps those saving on a database that only has 002.
        **({"detail": outage.detail, "hazard_id": outage.hazard_id} if outage.kind is IncidentKind.STORM else {}),
    }


def outage_from_record(record: dict[str, Any]) -> Outage:
    return Outage(
        id=record["id"],
        kind=IncidentKind(record["kind"]),
        device_id=record["device_id"],
        patient_name=record["patient_name"],
        equipment=record["equipment"],
        started_at=datetime.fromisoformat(record["started_at"]),
        ended_at=_datetime(record.get("ended_at")),
        reply_code=record.get("reply_code") or "",
        escalation_step=int(record.get("escalation_step") or 0),
        next_escalation_at=_datetime(record.get("next_escalation_at")),
        acknowledged_by=record.get("acknowledged_by"),
        acknowledged_at=_datetime(record.get("acknowledged_at")),
        alerted=[Alerted(a["name"], a["role"], a["phone"], datetime.fromisoformat(a["at"]))
                 for a in record.get("alerted") or []],
        detail=record.get("detail") or "",
        hazard_id=record.get("hazard_id") or "",
    )


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _datetime(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None

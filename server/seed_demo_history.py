"""Fill the incident history with 90 days of synthetic incidents for the patients already
in Supabase, so the History page and its charts have something to show in a demo.

    python3 seed_demo_history.py            # add (re-running replaces the same rows)
    python3 seed_demo_history.py --remove   # take them out again

Everything here is invented: storm names carry "(synthetic)", and none of it is evidence
of how Lifeline performs. Reads SUPABASE_URL and SUPABASE_SECRET_KEY from .env."""

from __future__ import annotations

import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain import BUTTON_SUFFIX, REPLY_SUFFIX, Alerted, IncidentKind, Outage, Patient
from supabase_backend import SupabaseClient, SupabaseOutageStore, SupabasePatientStore, _expect

DAYS = 90
ESCALATE_EVERY = timedelta(minutes=3)
SEED_NAMESPACE = uuid.UUID("6c1b3f9e-2d1a-4c55-9a51-1f0e6c2b7a10")  # ids are stable, so re-runs overwrite
# (days ago, warning, towns hit). Towns are matched against the patient's address.
STORMS = [
    (81, "Severe Thunderstorm Warning", ("Hattiesburg", "Petal", "Purvis")),
    (58, "Tropical Storm Warning", ("Hattiesburg", "Petal", "Purvis", "Lumberton", "Wiggins", "Columbia")),
    (33, "Flash Flood Warning", ("Laurel", "Ellisville", "Seminary", "Sumrall")),
    (11, "Severe Thunderstorm Warning", ("Hattiesburg", "Petal", "Sumrall", "Columbia")),
]


class HistoryFactory:
    def __init__(self, patients: list[Patient], now: datetime):
        self._patients = patients
        self._now = now
        self._random = random.Random(2026)
        self._count = 0
        self.incidents: list[Outage] = []

    def build(self) -> list[Outage]:
        for days_ago, warning, towns in STORMS:
            self._storm(self._now - timedelta(days=days_ago, hours=self._random.uniform(0, 6)), warning, towns)
        for _ in range(14):
            self._emergency(IncidentKind.POWER, self._random.choice(self._patients), self._random_moment(),
                            timedelta(minutes=self._random.uniform(8, 120)))
        for kind, count in ((IncidentKind.MACHINE, 3), (IncidentKind.HELP, 3)):
            for _ in range(count):
                self._emergency(kind, self._random.choice(self._patients), self._random_moment(),
                                timedelta(minutes=self._random.uniform(10, 50)))
        for kind, count in ((IncidentKind.SILENT, 4), (IncidentKind.TEMPERATURE, 3)):
            for _ in range(count):
                self._advisory(kind, self._random.choice(self._patients), self._random_moment())
        return self.incidents

    def _storm(self, arrives: datetime, warning: str, towns: tuple[str, ...]) -> None:
        hit = [p for p in self._patients if any(town in p.address for town in towns)]
        notice_at = arrives - timedelta(hours=self._random.uniform(4, 9))
        for patient in hit:
            first = patient.contacts[0]
            self._add(Outage(id=self._next_id(), kind=IncidentKind.STORM, device_id=patient.device_id,
                             patient_name=patient.name, equipment=patient.equipment, started_at=notice_at,
                             ended_at=arrives + timedelta(hours=6),
                             alerted=[Alerted(first.name, first.role, first.phone, notice_at)],
                             detail=f"{warning} (synthetic)", hazard_id=f"synthetic-{notice_at:%Y%m%d}-{warning}"))
            if self._random.random() < 0.75:
                self._emergency(IncidentKind.POWER, patient, arrives + timedelta(minutes=self._random.uniform(0, 90)),
                                timedelta(minutes=self._random.uniform(45, 360)))

    def _emergency(self, kind: IncidentKind, patient: Patient, start: datetime, lasted: timedelta) -> None:
        """Alert down the call list every few minutes until someone answers or the incident ends."""
        contacts = patient.contacts
        roll = self._random.random()
        answerer = None if roll < 0.08 else min(len(contacts) - 1, 0 if roll < 0.55 else 1 if roll < 0.8 else 2)
        if answerer is not None:
            answer_after = ESCALATE_EVERY * answerer + timedelta(minutes=self._random.uniform(0.5, 2.8))
            if answer_after >= lasted:
                answerer = None  # power came back before anyone answered
        alert_count = answerer + 1 if answerer is not None else min(len(contacts), int(lasted / ESCALATE_EVERY) + 1)
        alerted = [Alerted(c.name, c.role, c.phone, start + ESCALATE_EVERY * i) for i, c in enumerate(contacts[:alert_count])]
        incident = Outage(id=self._next_id(), kind=kind, device_id=patient.device_id, patient_name=patient.name,
                          equipment=patient.equipment, started_at=start, ended_at=start + lasted,
                          escalation_step=alert_count, alerted=alerted)
        if answerer is not None:
            incident.acknowledged_at = start + answer_after
            incident.acknowledged_by = f"{contacts[answerer].name} {self._random.choice((BUTTON_SUFFIX, REPLY_SUFFIX))}"
        self._add(incident)

    def _advisory(self, kind: IncidentKind, patient: Patient, start: datetime) -> None:
        told = [c for c in patient.contacts if c.is_care_staff()] or list(patient.contacts[:1])
        self._add(Outage(id=self._next_id(), kind=kind, device_id=patient.device_id, patient_name=patient.name,
                         equipment=patient.equipment, started_at=start,
                         ended_at=start + timedelta(minutes=self._random.uniform(15, 180)),
                         alerted=[Alerted(c.name, c.role, c.phone, start) for c in told]))

    def _random_moment(self) -> datetime:
        return self._now - timedelta(days=self._random.uniform(1, DAYS - 1))

    def _next_id(self) -> str:
        self._count += 1
        return str(uuid.uuid5(SEED_NAMESPACE, str(self._count)))

    def _add(self, incident: Outage) -> None:
        if incident.ended_at < self._now:  # never leave a synthetic incident open
            self.incidents.append(incident)


def load_env(path: Path) -> None:
    for line in path.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def main() -> None:
    load_env(Path(__file__).with_name(".env"))
    client = SupabaseClient(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    patients = sorted(SupabasePatientStore(client).list_all(), key=lambda p: p.device_id)
    incidents = HistoryFactory(patients, datetime.now(timezone.utc)).build()
    if "--remove" in sys.argv:
        ids = ",".join(i.id for i in incidents)
        status, body = client.request("DELETE", f"/rest/v1/outages?id=in.({ids})")
        _expect(status, body, "remove synthetic history")
        print(f"Removed up to {len(incidents)} synthetic incidents.")
        return
    store = SupabaseOutageStore(client)
    for incident in incidents:
        store.save(incident)
    print(f"Saved {len(incidents)} synthetic incidents over the last {DAYS} days for {len(patients)} patients.")


if __name__ == "__main__":
    main()

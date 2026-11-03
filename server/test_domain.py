"""Run: python3 test_domain.py"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib.error
import urllib.request

import app
from app import incident_view, incidents_csv, unit_health
from domain import (Channel, Contact, IncidentKind, InvalidPatient, Monitor, Notification, Patient, ReplyResult,
                    UnitHealth, UnknownDevice)
from local import JsonFileOutageStore, JsonFilePatientStore, SharedPasswordAuthenticator
from notifiers import NtfyNotifier, TwilioNotifier, is_valid_twilio_signature
from records import outage_from_record, outage_to_record, patient_from_form, patient_from_record, patient_to_record
import service
from service import DuplicateDevice, Lifeline
from staff import LoginThrottle, Sessions, StaffMember
from storm import (Conditions, ForecastPeriod, Hazard, RiskLevel, StormWatch, WarningArea, WeatherReport, WeatherUnavailable,
                   alert_hazard, assess_forecast, nearby_outage_hazard)
from weather import DemoWeather, areas_from_alerts, conditions_from_grid, hazards_from_alerts, hazards_from_grid, parse_duration
from supabase_backend import SupabaseAuthenticator, SupabaseClient, SupabaseError, SupabaseOutageStore, SupabasePatientStore

T0 = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)
ESCALATE = timedelta(minutes=2)
TOKEN = "t" * 20


def a_patient(**overrides) -> Patient:
    fields = dict(
        device_id="unit-01", device_token=TOKEN, share_token="s" * 20, name="Ruth", address="1 Pine St",
        lat=31.3, lon=-89.3, equipment="Oxygen concentrator", backup_minutes=60, acuity=1,
        contacts=(
            Contact("caregiver", "Daughter", "+16015550101", Channel.SMS),
            Contact("agency", "Nurse", "+16015550102", Channel.CALL),
        ),
    )
    fields.update(overrides)
    return Patient(**fields)


def a_form(**overrides) -> dict:
    record = patient_to_record(a_patient())
    record.pop("device_token")
    record.pop("share_token")
    record.update(overrides)
    return record


SILENT_AFTER = timedelta(minutes=5)


def make_monitor(*patients: Patient, codes=None, open_incidents=()) -> Monitor:
    codes = iter(codes or ["1111", "2222", "3333"])
    ids = (f"incident-{n}" for n in range(1000))
    return Monitor(list(patients or [a_patient()]), escalate_after=ESCALATE,
                   offline_after=timedelta(seconds=90), status_link=lambda p: f"https://x/s/{p.share_token}",
                   new_reply_code=lambda: next(codes), silent_alert_after=SILENT_AFTER,
                   open_incidents=list(open_incidents), new_id=lambda: next(ids))


class OutageFlow(unittest.TestCase):
    def test_power_loss_alerts_first_contact_once_with_status_link(self):
        m = make_monitor()
        self.assertEqual(m.report("unit-01", True, 90, T0), [])
        first = m.report("unit-01", False, 90, T0)
        self.assertEqual([n.contact.name for n in first], ["Daughter"])
        self.assertIn("60 min", first[0].message)
        self.assertIn("https://x/s/" + "s" * 20, first[0].message)
        self.assertIn("Reply OK 1111", first[0].message)
        self.assertEqual(m.report("unit-01", False, 89, T0 + timedelta(seconds=30)), [])

    def test_escalates_until_list_exhausted(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        self.assertEqual(m.tick(T0 + ESCALATE - timedelta(seconds=1)), [])
        second = m.tick(T0 + ESCALATE)
        self.assertEqual([n.contact.name for n in second], ["Nurse"])
        self.assertIn("58 min", second[0].message)
        self.assertEqual(m.tick(T0 + 10 * ESCALATE), [])

    def test_sms_ack_stops_escalation(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        self.assertEqual(m.acknowledge_by_reply("+19995550000", "OK", T0).result, ReplyResult.NOTHING_OPEN)
        self.assertEqual(m.acknowledge_by_reply("+16015550101", "okay", T0).result, ReplyResult.NOTHING_OPEN)
        outcome = m.acknowledge_by_reply("+16015550101", "ok", T0)
        self.assertEqual((outcome.result, outcome.device_id), (ReplyResult.ACKNOWLEDGED, "unit-01"))
        self.assertEqual(m.tick(T0 + ESCALATE), [])
        self.assertEqual(m.status("unit-01", T0)["acknowledged_by"], "Daughter (text reply)")

    def test_shared_contact_must_name_the_outage(self):
        nurse = Contact("agency", "Nurse", "+16015550102", Channel.CALL)
        ruth = a_patient(contacts=(nurse,))
        james = a_patient(device_id="unit-02", share_token="j" * 20, name="James", contacts=(nurse,))
        m = make_monitor(ruth, james, codes=["1111", "1111", "2222"])  # duplicate code is re-drawn
        m.report("unit-01", False, 90, T0)
        m.report("unit-02", False, 90, T0)
        self.assertEqual(m.acknowledge_by_reply(nurse.phone, "OK", T0).result, ReplyResult.AMBIGUOUS)
        self.assertIsNone(m.status("unit-01", T0)["acknowledged_by"])
        outcome = m.acknowledge_by_reply(nurse.phone, "OK 2222", T0)
        self.assertEqual((outcome.result, outcome.device_id), (ReplyResult.ACKNOWLEDGED, "unit-02"))
        self.assertIsNone(m.status("unit-01", T0)["acknowledged_by"])
        # Now only Ruth is open for this phone, so a bare OK is unambiguous again.
        self.assertEqual(m.acknowledge_by_reply(nurse.phone, "ok", T0).device_id, "unit-01")

    def test_restore_notifies_only_alerted_contacts(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        restored = m.report("unit-01", True, 90, T0 + timedelta(minutes=5))
        self.assertEqual([n.contact.name for n in restored], ["Daughter"])
        self.assertIn("after 5 min", restored[0].message)
        self.assertEqual(m.status("unit-01", T0)["status"], "ok")

    def test_editing_a_patient_keeps_live_outage(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        m.upsert_patient(a_patient(name="Ruth M."))
        row = m.status("unit-01", T0)
        self.assertEqual((row["name"], row["status"]), ("Ruth M.", "outage"))

    def test_offline_and_unknown(self):
        m = make_monitor()
        self.assertEqual(m.status("unit-01", T0)["status"], "never_seen")
        m.report("unit-01", True, 90, T0)
        self.assertEqual(m.status("unit-01", T0 + timedelta(seconds=91))["status"], "offline")
        with self.assertRaises(UnknownDevice):
            m.report("nope", True, None, T0)


class RestartRecovery(unittest.TestCase):
    def test_restart_mid_outage_resumes_escalation_without_realerting(self):
        before = make_monitor()
        before.report("unit-01", False, 90, T0)
        saved = before.drain_changes()[-1]
        self.assertEqual([a.name for a in saved.alerted], ["Daughter"])

        after = make_monitor(open_incidents=[outage_from_record(outage_to_record(saved))])
        self.assertEqual(after.status("unit-01", T0)["status"], "outage")  # before any heartbeat
        self.assertEqual(after.report("unit-01", False, 88, T0 + timedelta(seconds=30)), [])  # no new alert
        self.assertEqual([n.contact.name for n in after.tick(T0 + ESCALATE)], ["Nurse"])
        restored = after.report("unit-01", True, 88, T0 + ESCALATE + timedelta(minutes=1))
        self.assertEqual(sorted(n.contact.name for n in restored), ["Daughter", "Nurse"])

    def test_reply_after_restart_still_matches_the_code(self):
        before = make_monitor()
        before.report("unit-01", False, 90, T0)
        after = make_monitor(open_incidents=before.drain_changes())
        outcome = after.acknowledge_by_reply("+16015550101", "OK 1111", T0 + timedelta(minutes=1))
        self.assertEqual(outcome.result, ReplyResult.ACKNOWLEDGED)
        self.assertEqual(after.drain_changes()[0].acknowledged_at, T0 + timedelta(minutes=1))

    def test_incidents_of_removed_patients_are_skipped(self):
        before = make_monitor()
        before.report("unit-01", False, 90, T0)
        after = make_monitor(a_patient(device_id="unit-02", share_token="j" * 20), open_incidents=before.drain_changes())
        self.assertEqual(after.status("unit-02", T0)["status"], "never_seen")


class SilentUnits(unittest.TestCase):
    def test_alerts_care_staff_once_then_announces_return(self):
        m = make_monitor()
        m.report("unit-01", True, 90, T0)
        self.assertEqual(m.tick(T0 + SILENT_AFTER - timedelta(seconds=1)), [])
        alerts = m.tick(T0 + SILENT_AFTER)
        self.assertEqual([n.contact.name for n in alerts], ["Nurse"])  # role "agency", not the daughter
        self.assertIn("silent for 5 min", alerts[0].message)
        self.assertEqual(m.tick(T0 + 3 * SILENT_AFTER), [])  # once per silence
        back = m.report("unit-01", True, 90, T0 + 4 * SILENT_AFTER)
        self.assertEqual([n.contact.name for n in back], ["Nurse"])
        self.assertIn("reporting again", back[0].message)
        incident = m.drain_changes()[-1]
        self.assertEqual((incident.kind, incident.started_at), (IncidentKind.SILENT, T0))
        self.assertEqual(incident.ended_at, T0 + 4 * SILENT_AFTER)

    def test_falls_back_to_first_contact_without_care_staff(self):
        family_only = a_patient(contacts=(Contact("Family", "Son", "+16015550109", Channel.SMS),))
        m = make_monitor(family_only)
        m.report("unit-01", True, 90, T0)
        self.assertEqual([n.contact.name for n in m.tick(T0 + SILENT_AFTER)], ["Son"])

    def test_never_seen_and_outage_units_get_no_silent_alert(self):
        m = make_monitor()
        self.assertEqual(m.tick(T0 + 10 * SILENT_AFTER), [])  # never reported
        m.report("unit-01", False, 90, T0)
        m.acknowledge("unit-01", "nurse@agency.org (agency)", T0)
        self.assertEqual(m.tick(T0 + 10 * SILENT_AFTER), [])  # people are already on the way

    def test_power_loss_after_silence_sends_one_alert_not_two(self):
        m = make_monitor()
        m.report("unit-01", True, 90, T0)
        m.tick(T0 + SILENT_AFTER)
        alerts = m.report("unit-01", False, 90, T0 + 2 * SILENT_AFTER)
        self.assertEqual(len(alerts), 1)
        self.assertIn("Power is out", alerts[0].message)


class History(unittest.TestCase):
    def test_incident_view_and_report_rows(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        m.acknowledge("unit-01", "nurse@agency.org (agency)", T0 + timedelta(minutes=4))
        m.report("unit-01", True, 90, T0 + timedelta(minutes=30))
        incident = m.drain_changes()[-1]
        view = incident_view(incident)
        self.assertEqual((view["duration_minutes"], view["response_minutes"]), (30, 4))
        self.assertEqual(view["alerted"], [{"name": "Daughter", "role": "caregiver", "at": T0.isoformat()}])
        self.assertNotIn("+1601", json.dumps(view))  # names and roles only, never phone numbers
        rows = incidents_csv([incident]).splitlines()
        self.assertEqual(len(rows), 2)
        self.assertIn("Power outage,Ruth,unit-01", rows[1])
        self.assertTrue(rows[1].endswith("nurse@agency.org (agency),4"))

    def test_csv_neutralises_spreadsheet_formulas(self):
        m = make_monitor(a_patient(name="=HYPERLINK(1)"))
        m.report("unit-01", False, 90, T0)
        self.assertIn("'=HYPERLINK(1)", incidents_csv(m.drain_changes()))

    def test_unit_health_keeps_good_values_and_drops_bad_ones(self):
        self.assertEqual(unit_health({"rssi": -61, "fw": "0.2.0", "uptime_s": 3600}), UnitHealth(-61, "0.2.0", 3600))
        self.assertEqual(unit_health({"rssi": 12, "fw": "<script>", "uptime_s": True}), UnitHealth())
        m = make_monitor()
        m.report("unit-01", True, 90, T0, UnitHealth(-61, "0.2.0", 3600))
        row = m.status("unit-01", T0)
        self.assertEqual((row["rssi"], row["firmware"], row["uptime_s"]), (-61, "0.2.0", 3600))


CENTRAL = timezone(timedelta(hours=-6))


def hourly(values, start=T0):
    return [ForecastPeriod(start + timedelta(hours=n), timedelta(hours=1), v) for n, v in enumerate(values)]


def a_hazard(level=RiskLevel.HIGH, hazard_id="alert:1", ends=T0 + timedelta(hours=3)):
    return Hazard(hazard_id, level, "Ice Storm Warning", T0, ends)


class StormSignals(unittest.TestCase):
    def test_only_power_threatening_alerts_count(self):
        self.assertEqual(alert_hazard("a", "Tornado Warning", T0, T0).level, RiskLevel.HIGH)
        self.assertEqual(alert_hazard("a", "Wind Advisory", T0, T0).level, RiskLevel.ELEVATED)
        self.assertIsNone(alert_hazard("a", "Small Craft Advisory", T0, T0))

    def test_gust_thresholds(self):
        self.assertEqual(assess_forecast(hourly([30, 60]), [], T0), [])                      # 37 mph: nothing
        (elevated,) = assess_forecast(hourly([30, 70]), [], T0)                              # 43 mph
        self.assertEqual((elevated.level, elevated.headline), (RiskLevel.ELEVATED, "Gusts up to 43 mph forecast"))
        (high,) = assess_forecast(hourly([100]), [], T0)                                     # 62 mph
        self.assertEqual(high.level, RiskLevel.HIGH)
        self.assertEqual(assess_forecast(hourly([100], start=T0 - timedelta(hours=3)), [], T0), [])  # already over
        self.assertEqual(assess_forecast(hourly([100], start=T0 + timedelta(hours=30)), [], T0), [])  # beyond 24 h

    def test_ice_adds_up_over_the_day(self):
        (ice,) = assess_forecast([], hourly([2, 2.5, 2]), T0)  # 6.5 mm is just over a quarter inch
        self.assertEqual((ice.level, ice.headline), (RiskLevel.HIGH, "About 0.26 in of ice forecast"))
        self.assertEqual(assess_forecast([], hourly([2, 2]), T0), [])

    def test_nws_parsing(self):
        self.assertEqual(parse_duration("P1DT6H"), timedelta(days=1, hours=6))
        alerts = {"features": [
            {"properties": {"id": "urn:1", "event": "Ice Storm Warning", "onset": "2026-11-02T12:00:00-06:00",
                            "ends": "2026-11-03T06:00:00-06:00"}},
            {"properties": {"id": "urn:2", "event": "Flood Warning", "onset": "2026-11-02T12:00:00-06:00",
                            "ends": "2026-11-03T06:00:00-06:00"}},
        ]}
        self.assertEqual([h.id for h in hazards_from_alerts(alerts)], ["alert:urn:1"])
        grid = {"properties": {
            "windGust": {"uom": "wmoUnit:km_h-1", "values": [{"validTime": "2026-11-02T04:00:00+00:00/PT2H", "value": 100}]},
            "iceAccumulation": {"uom": "wmoUnit:in", "values": [{"validTime": "2026-11-02T04:00:00+00:00/PT1H", "value": 9}]},
        }}
        self.assertEqual([h.headline for h in hazards_from_grid(grid, T0)], ["Gusts up to 62 mph forecast"])  # odd unit skipped

    def test_conditions_now_from_the_grid(self):
        grid = {"properties": {
            "temperature": {"uom": "wmoUnit:degC", "values": [{"validTime": "2026-11-02T02:00:00+00:00/PT2H", "value": 20}]},
            "windSpeed": {"uom": "wmoUnit:km_h-1", "values": [{"validTime": "2026-11-02T03:00:00+00:00/PT1H", "value": 16.1}]},
            "windGust": {"uom": "wmoUnit:km_h-1", "values": [{"validTime": "2026-11-02T04:00:00+00:00/PT1H", "value": 50}]},
            "probabilityOfThunder": {"values": [{"validTime": "2026-11-02T00:00:00+00:00/PT6H", "value": 30}]},
        }}
        self.assertEqual(conditions_from_grid(grid, T0), Conditions(temperature_f=68, wind_mph=10, gust_mph=None, thunder_percent=30))

    def test_warning_outlines_come_from_the_alert_or_its_zones(self):
        square = {"type": "Polygon", "coordinates": [[[-89, 31], [-88, 31], [-88, 32], [-89, 31]]]}
        times = {"onset": "2026-11-02T12:00:00-06:00", "ends": "2026-11-03T06:00:00-06:00"}
        alerts = {"features": [
            {"geometry": square, "properties": {"id": "urn:1", "event": "Tornado Warning", **times}},
            {"geometry": None, "properties": {"id": "urn:2", "event": "Wind Advisory", **times,
                                              "affectedZones": ["https://api.weather.gov/zones/forecast/MSZ074", "missing"]}},
            {"geometry": square, "properties": {"id": "urn:3", "event": "Flood Warning", **times}},
        ]}
        zones = {"https://api.weather.gov/zones/forecast/MSZ074": square}
        tornado, wind = areas_from_alerts(alerts, zones.get)
        self.assertEqual((tornado.event, tornado.level, tornado.geometry), ("Tornado Warning", RiskLevel.HIGH, square))
        self.assertEqual((wind.level, wind.geometry["type"], len(wind.geometry["coordinates"])),
                         (RiskLevel.ELEVATED, "MultiPolygon", 1))

    def test_outages_spreading_nearby(self):
        def row(device_id, lat, status="ok", started=None):
            return {"device_id": device_id, "lat": lat, "lon": -89.29, "status": status,
                    "outage_started": started.isoformat() if started else None}
        board = [row("unit-01", 31.30), row("unit-02", 31.31, "outage", T0), row("unit-03", 31.32, "outage", T0),
                 row("unit-04", 32.30, "outage", T0)]  # unit-04 is about 110 km away
        hazard = nearby_outage_hazard("unit-01", board, T0 + timedelta(minutes=5))
        self.assertEqual(hazard.headline, "Power is out at 2 other Lifeline homes within 3 miles")
        self.assertIsNone(nearby_outage_hazard("unit-02", board, T0))  # already out: the outage alert covers it
        self.assertIsNone(nearby_outage_hazard("unit-01", board[:2], T0))  # one neighbour is not a pattern
        self.assertIsNone(nearby_outage_hazard("unit-01", board, T0 + timedelta(hours=2)))  # old news


class StormNotices(unittest.TestCase):
    def test_high_hazard_is_announced_once_to_first_contact(self):
        watch = StormWatch(CENTRAL, new_id=lambda: "storm-1")
        (sent,) = watch.update(a_patient(), [a_hazard()], T0)
        self.assertEqual(sent.contact.name, "Daughter")
        self.assertIn("Ice Storm Warning for Ruth's area until 12:00 AM", sent.message)
        self.assertIn("backup is about 60 min", sent.message)
        self.assertEqual(watch.update(a_patient(), [a_hazard()], T0 + timedelta(minutes=5)), [])
        (notice,) = watch.drain_changes()
        self.assertEqual((notice.detail, notice.hazard_id, notice.ended_at), ("Ice Storm Warning", "alert:1", None))

    def test_elevated_is_shown_not_sent_and_clearing_closes_the_notice(self):
        watch = StormWatch(CENTRAL)
        self.assertEqual(watch.update(a_patient(), [a_hazard(RiskLevel.ELEVATED)], T0), [])
        self.assertEqual([h.level for h in watch.hazards("unit-01")], [RiskLevel.ELEVATED])
        watch.update(a_patient(), [a_hazard()], T0)
        watch.update(a_patient(), [], T0 + timedelta(hours=1))
        self.assertEqual(watch.drain_changes()[-1].ended_at, T0 + timedelta(hours=1))
        self.assertEqual(watch.hazards("unit-01"), [])

    def test_demo_storm_survives_a_restart_the_same_day(self):
        first = DemoWeather(started=T0).report_at(31.3, -89.3, T0).hazards[0]
        again = DemoWeather(started=T0 + timedelta(minutes=5)).report_at(31.3, -89.3, T0).hazards[0]
        self.assertEqual(first.id, again.id)

    def test_restart_does_not_announce_again(self):
        before = StormWatch(CENTRAL)
        before.update(a_patient(), [a_hazard()], T0)
        saved = [outage_from_record(outage_to_record(n)) for n in before.drain_changes()]
        after = StormWatch(CENTRAL, open_notices=saved)
        self.assertEqual(after.update(a_patient(), [a_hazard()], T0 + timedelta(minutes=5)), [])


class FakeWeather:
    def __init__(self, hazards, areas=()):
        self.hazards = hazards
        self.areas = list(areas)

    def report_at(self, lat, lon, now):
        if self.hazards is None:
            raise WeatherUnavailable("weather.gov down")
        return WeatherReport(self.hazards, Conditions(temperature_f=31, gust_mph=45), self.areas)


class StormWatchService(unittest.TestCase):
    def test_refresh_then_tick_announces_saves_and_shows_on_board(self):
        sent = []
        notifier = type("Recorder", (), {"send": lambda self, n: sent.append(n)})()
        outages = MemoryOutageStore()
        ends = datetime.now(timezone.utc) + timedelta(hours=2)
        expired = WarningArea("old", "Wind Advisory", RiskLevel.ELEVATED, datetime.now(timezone.utc), {"type": "Polygon"})
        current = WarningArea("now", "Ice Storm Warning", RiskLevel.HIGH, ends, {"type": "Polygon"})
        weather = FakeWeather([a_hazard(ends=ends)], [expired, current])
        lifeline = Lifeline(make_monitor(), MemoryStore(a_patient()), notifier, outages, StormWatch(CENTRAL), weather)
        lifeline.refresh_weather()
        lifeline.tick()
        lifeline.wait_for_saves()
        lifeline._senders.shutdown(wait=True)
        self.assertEqual([n.contact.name for n in sent], ["Daughter"])
        (notice,) = outages.saved.values()
        self.assertEqual(notice.kind, IncidentKind.STORM)
        self.assertEqual(lifeline.snapshot()[0]["hazards"][0]["headline"], "Ice Storm Warning")
        self.assertEqual(lifeline.family_status("s" * 20)["hazards"][0]["level"], "high")
        self.assertEqual(lifeline.snapshot()[0]["conditions"]["temperature_f"], 31)
        weather_map = lifeline.weather_map()
        self.assertIsNotNone(weather_map["checked_at"])
        self.assertEqual([f["properties"]["event"] for f in weather_map["areas"]["features"]], ["Ice Storm Warning"])
        weather.hazards = None  # a failed fetch keeps the last known report instead of clearing it
        lifeline.refresh_weather()
        self.assertEqual(len(lifeline.snapshot()[0]["hazards"]), 1)


class PatientValidation(unittest.TestCase):
    def test_invariants(self):
        for bad in (dict(contacts=()), dict(acuity=7), dict(device_id="Bad ID"), dict(lat=200),
                    dict(backup_minutes=-1), dict(name="  "), dict(share_token="short")):
            with self.subTest(bad=bad), self.assertRaises(InvalidPatient):
                a_patient(**bad)
        with self.assertRaises(InvalidPatient):
            Contact("caregiver", "Son", "601-555-0101", Channel.SMS)  # not E.164

    def test_form_parsing_normalises_and_explains(self):
        form = a_form(device_id=" Unit-07 ", contacts=[{"role": "caregiver", "name": "Son",
                                                        "phone": "+1 (601) 555-0199", "channel": "sms"}])
        patient = patient_from_form(form, TOKEN, TOKEN)
        self.assertEqual((patient.device_id, patient.contacts[0].phone), ("unit-07", "+16015550199"))
        with self.assertRaisesRegex(InvalidPatient, "Missing field: address"):
            patient_from_form({k: v for k, v in form.items() if k != "address"}, TOKEN, TOKEN)
        with self.assertRaisesRegex(InvalidPatient, "channel"):
            patient_from_form(a_form(contacts=[{"role": "r", "name": "n", "phone": "+16015550101",
                                                "channel": "fax"}]), TOKEN, TOKEN)
        with self.assertRaisesRegex(InvalidPatient, "numbers"):
            patient_from_form(a_form(lat="north"), TOKEN, TOKEN)

    def test_record_round_trip(self):
        self.assertEqual(patient_from_record(patient_to_record(a_patient())), a_patient())


class MemoryStore:
    def __init__(self, *patients: Patient):
        self.saved = {p.device_id: p for p in patients}

    def list_all(self):
        return list(self.saved.values())

    def save(self, patient):
        self.saved[patient.device_id] = patient

    def delete(self, device_id):
        self.saved.pop(device_id, None)


class SilentNotifier:
    def send(self, notification):
        pass


class MemoryOutageStore:
    def __init__(self, fail_times=0):
        self.saved = {}
        self.fail_times = fail_times

    def list_open(self):
        return [o for o in self.saved.values() if o.ended_at is None]

    def list_since(self, since):
        return [o for o in self.saved.values() if o.started_at >= since]

    def save(self, outage):
        if self.fail_times:
            self.fail_times -= 1
            raise OSError("database unreachable")
        self.saved[outage.id] = outage


class RosterUseCases(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(a_patient())
        self.outages = MemoryOutageStore()
        self.lifeline = Lifeline(make_monitor(*self.store.list_all()), self.store, SilentNotifier(), self.outages)

    def test_add_generates_distinct_tokens_and_persists(self):
        added = self.lifeline.add_patient(a_form(device_id="unit-02"))
        self.assertNotEqual(added.device_token, added.share_token)
        self.assertGreaterEqual(len(added.share_token), 32)
        self.assertIn("unit-02", self.store.saved)
        with self.assertRaises(DuplicateDevice):
            self.lifeline.add_patient(a_form(device_id="unit-02"))

    def test_update_keeps_credentials(self):
        updated = self.lifeline.update_patient("unit-01", a_form(name="Ruth M.", device_id="ignored"))
        self.assertEqual((updated.device_id, updated.device_token), ("unit-01", TOKEN))

    def test_family_link_scope_and_rotation(self):
        status = self.lifeline.family_status("s" * 20)
        self.assertEqual(status["name"], "Ruth")
        self.assertNotIn("device_token", status)
        self.assertNotIn("address", status)
        rotated = self.lifeline.rotate_share_link("unit-01")
        self.assertIsNone(self.lifeline.family_status("s" * 20))
        self.assertIsNotNone(self.lifeline.family_status(rotated.share_token))

    def test_family_ack_only_during_outage(self):
        self.assertFalse(self.lifeline.family_acknowledge("s" * 20))
        self.lifeline.report("unit-01", False, 90)
        self.assertTrue(self.lifeline.family_acknowledge("s" * 20))
        self.assertEqual(self.lifeline.family_status("s" * 20)["acknowledged_by"], "A family member")

    def test_incidents_are_saved_and_listed(self):
        self.lifeline.report("unit-01", False, 90)
        self.lifeline.acknowledge("unit-01", "nurse@agency.org (agency)")
        self.lifeline.wait_for_saves()
        (incident,) = self.outages.saved.values()
        self.assertEqual(incident.acknowledged_by, "nurse@agency.org (agency)")
        self.assertEqual([i.id for i in self.lifeline.history(days=1)], [incident.id])
        self.assertEqual(self.lifeline.history(days=1, device_id="unit-02"), [])

    def test_failed_save_is_retried_and_never_blocks_alerts(self):
        self.outages.fail_times = 1
        self.addCleanup(setattr, service, "SAVE_BACKOFF_SECONDS", service.SAVE_BACKOFF_SECONDS)
        service.SAVE_BACKOFF_SECONDS = 0
        self.lifeline.report("unit-01", False, 90)
        self.lifeline.wait_for_saves()
        self.assertEqual(len(self.outages.saved), 1)

    def test_remove_revokes_link(self):
        self.lifeline.remove_patient("unit-01")
        self.assertIsNone(self.lifeline.family_status("s" * 20))
        self.assertEqual(self.store.saved, {})


class LocalAdapters(unittest.TestCase):
    def test_json_store_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JsonFilePatientStore(Path(folder) / "patients.json")
            store.save(a_patient())
            store.save(a_patient(name="Ruth M."))
            self.assertEqual([p.name for p in store.list_all()], ["Ruth M."])
            store.delete("unit-01")
            self.assertEqual(store.list_all(), [])

    def test_json_outage_store_round_trip(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        m.report("unit-01", True, 90, T0 + timedelta(minutes=3))
        (ended,) = m.drain_changes()
        with tempfile.TemporaryDirectory() as folder:
            store = JsonFileOutageStore(Path(folder) / "outages.json")
            store.save(ended)
            self.assertEqual(store.list_since(T0), [ended])
            self.assertEqual(store.list_open(), [])
            self.assertEqual(store.list_since(T0 + timedelta(minutes=1)), [])

    def test_example_patients_load(self):
        self.assertEqual(len(JsonFilePatientStore(Path(__file__).parent / "patients.example.json").list_all()), 3)

    def test_shared_password(self):
        auth = SharedPasswordAuthenticator("correct horse")
        self.assertEqual(auth.sign_in("a@b.org", "correct horse"), StaffMember("a@b.org"))
        self.assertIsNone(auth.sign_in("a@b.org", "wrong"))


class StaffSessions(unittest.TestCase):
    def test_sessions(self):
        sessions = Sessions()
        sid = sessions.start(StaffMember("nurse@agency.org"))
        self.assertEqual(sessions.member(sid).email, "nurse@agency.org")
        sessions.end(sid)
        self.assertIsNone(sessions.member(sid))
        self.assertIsNone(sessions.member(None))

    def test_throttle_per_account(self):
        throttle = LoginThrottle()
        for _ in range(LoginThrottle.MAX_PER_ACCOUNT):
            throttle.record_failure("1.2.3.4", "nurse@agency.org")
        self.assertTrue(throttle.is_blocked("1.2.3.4", "nurse@agency.org"))
        self.assertFalse(throttle.is_blocked("1.2.3.4", "other@agency.org"))
        self.assertFalse(throttle.is_blocked("5.6.7.8", "nurse@agency.org"))

    def test_rotating_emails_still_blocks_the_address(self):
        throttle = LoginThrottle()
        for n in range(LoginThrottle.MAX_PER_ADDRESS):
            throttle.record_failure("1.2.3.4", f"guess{n}@x.org")
        self.assertTrue(throttle.is_blocked("1.2.3.4", "fresh@x.org"))
        throttle.record_success("1.2.3.4", "guess0@x.org")  # one success must not clear the address
        self.assertTrue(throttle.is_blocked("1.2.3.4", "fresh@x.org"))

    def test_throttle_memory_is_bounded(self):
        throttle = LoginThrottle()
        throttle.MAX_TRACKED_KEYS = 50
        for n in range(200):
            throttle.record_failure(f"10.0.0.{n}", "a@b.org")
        self.assertLessEqual(len(throttle._failures), 52)


class Webhooks(unittest.TestCase):
    def test_twilio_signature(self):
        # Example values from Twilio's webhook security documentation.
        url = "https://mycompany.com/myapp.php?foo=1&bar=2"
        params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
                  "From": "+12349013030", "To": "+18005551212"}
        self.assertTrue(is_valid_twilio_signature("12345", url, params, "0/KCTR6DLpKmkAf8muzZqo1nDgQ="))
        self.assertFalse(is_valid_twilio_signature("12345", url, params, "bogus"))

    def test_whatsapp_sender_turns_every_alert_into_a_whatsapp_message(self):
        sent = []
        notifier = TwilioNotifier("AC123", "token", "whatsapp:+14155238886")
        notifier._post = lambda resource, fields: sent.append((resource, fields))
        caller = Contact(name="Sam", role="caregiver", phone="+16015550100", channel=Channel.CALL)
        notifier.send(Notification(caller, "Lifeline alert: Power is out. Reply OK 1234"))
        self.assertEqual(sent, [("Messages", {"To": "whatsapp:+16015550100", "From": "whatsapp:+14155238886",
                                              "Body": "Lifeline alert: Power is out. Reply OK 1234"})])


class NtfyPush(unittest.TestCase):
    def test_alert_is_pushed_urgently_to_the_topic_with_full_text(self):
        sent = []
        notifier = NtfyNotifier("lifeline-0123456789abcdef")
        notifier._post = sent.append
        nurse = Contact(name="On-call nurse", role="agency", phone="+16015550100", channel=Channel.CALL)
        notifier.send(Notification(nurse, "Lifeline alert: Power is out. Reply OK 1234"))
        self.assertEqual(sent, [{"topic": "lifeline-0123456789abcdef", "title": "For On-call nurse (agency)",
                                 "message": "Lifeline alert: Power is out. Reply OK 1234",
                                 "priority": 5, "tags": ["rotating_light"]}])

    def test_short_guessable_topic_is_rejected(self):
        with self.assertRaises(ValueError):
            NtfyNotifier("lifeline")

    def test_plain_http_server_is_rejected(self):
        with self.assertRaises(ValueError):
            NtfyNotifier("lifeline-0123456789abcdef", server="http://ntfy.example.com")

class StaffOnlyRoutes(unittest.TestCase):
    def test_history_and_report_need_a_signed_in_staff_member(self):
        app.Handler.sessions = Sessions()
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for path in ("/api/history", "/api/history.csv?days=7", "/api/weather-map"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5)
            self.assertEqual(refused.exception.code, 401)
            refused.exception.close()


class FakeSupabase(BaseHTTPRequestHandler):
    """Records requests and answers like PostgREST / GoTrue would."""
    requests: list = []

    def _answer(self, status, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        FakeSupabase.requests.append((self.command, self.path, self.headers, body))
        return body

    def do_GET(self):
        self._record()
        if self.path.startswith("/rest/v1/outages"):
            m = make_monitor()
            m.report("unit-01", False, 90, T0)
            self._answer(200, [outage_to_record(o) for o in m.drain_changes()])
        else:
            self._answer(200, [patient_to_record(a_patient())])

    def do_POST(self):
        body = self._record()
        if self.path.startswith("/auth/v1/token"):
            ok = body == {"email": "nurse@agency.org", "password": "right"}
            self._answer(200 if ok else 400, {"user": {"email": "nurse@agency.org"}} if ok else {"msg": "Invalid login"})
        elif body[0]["device_id"] == "unit-99":
            self._answer(400, {"message": "violates check constraint"})
        else:
            self._answer(201)

    def do_DELETE(self):
        self._record()
        self._answer(204)

    def log_message(self, *args):
        pass


class SupabaseAdapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeSupabase)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def client(self, key="sb_secret_abc"):
        client = SupabaseClient("https://example.supabase.co", key)
        client._base = f"http://127.0.0.1:{self.server.server_port}"  # point at the fake
        return client

    def setUp(self):
        FakeSupabase.requests = []

    def test_store_requests(self):
        store = SupabasePatientStore(self.client())
        self.assertEqual(store.list_all(), [a_patient()])
        store.save(a_patient())
        store.delete("unit-01")
        (_, get_path, get_headers, _), (_, _, post_headers, post_body), (_, delete_path, _, _) = FakeSupabase.requests
        self.assertEqual(get_path, "/rest/v1/patients?select=*")
        self.assertEqual(get_headers["apikey"], "sb_secret_abc")
        self.assertIsNone(get_headers["Authorization"])  # new secret keys are not JWTs
        self.assertIn("merge-duplicates", post_headers["Prefer"])
        self.assertEqual(post_body[0]["share_token"], "s" * 20)
        self.assertEqual(delete_path, "/rest/v1/patients?device_id=eq.unit-01")

    def test_legacy_jwt_key_also_sent_as_bearer(self):
        SupabasePatientStore(self.client("eyJhbGciOi.legacy")).list_all()
        self.assertEqual(FakeSupabase.requests[0][2]["Authorization"], "Bearer eyJhbGciOi.legacy")

    def test_database_rejection_surfaces(self):
        with self.assertRaisesRegex(SupabaseError, "check constraint"):
            SupabasePatientStore(self.client()).save(a_patient(device_id="unit-99"))

    def test_outage_store_requests(self):
        store = SupabaseOutageStore(self.client())
        (open_incident,) = store.list_open()
        self.assertEqual((open_incident.device_id, open_incident.reply_code), ("unit-01", "1111"))
        store.list_since(T0)
        store.save(open_incident)
        (_, open_path, _, _), (_, since_path, _, _), (_, post_path, post_headers, body) = FakeSupabase.requests
        self.assertEqual(open_path, "/rest/v1/outages?select=*&ended_at=is.null")
        self.assertTrue(since_path.startswith("/rest/v1/outages?select=*&started_at=gte.2026-11-02T03"))
        self.assertEqual((post_path, body[0]["id"]), ("/rest/v1/outages", open_incident.id))
        self.assertIn("merge-duplicates", post_headers["Prefer"])

    def test_auth(self):
        auth = SupabaseAuthenticator(self.client())
        self.assertEqual(auth.sign_in("nurse@agency.org", "right"), StaffMember("nurse@agency.org"))
        self.assertIsNone(auth.sign_in("nurse@agency.org", "wrong"))
        self.assertIsNone(auth.sign_in("", ""))


if __name__ == "__main__":
    unittest.main()

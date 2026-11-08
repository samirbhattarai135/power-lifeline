"""Run: python3 test_domain.py"""

import json
import tempfile
import time
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib.error
import urllib.request

import app
from access import InvalidStaffChange, Permission, Role, StaffAccount, StaffDirectory
from app import button_event, incident_view, incidents_csv, sensor_readings, unit_health
from domain import (Button, Channel, Contact, EmergencyPlan, IncidentKind, SensorReadings, InvalidPatient, Monitor, Notification, Patient, ReplyResult,
                    UnitHealth, UnknownDevice)
from local import JsonFileOutageStore, JsonFilePatientStore, SharedPasswordAuthenticator
from notifiers import NtfyNotifier, TwilioNotifier, is_valid_twilio_signature
from geocode import Match, matches_from_response
from respond import RespondTokens
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


def make_monitor(*patients: Patient, codes=None, open_incidents=(), county=(), assume_powered=False) -> Monitor:
    codes = iter(codes or ["1111", "2222", "3333"])
    ids = (f"incident-{n}" for n in range(1000))
    return Monitor(list(patients or [a_patient()]), escalate_after=ESCALATE,
                   offline_after=timedelta(seconds=90), status_link=lambda p: f"https://x/s/{p.share_token}",
                   new_reply_code=lambda: next(codes), silent_alert_after=SILENT_AFTER,
                   open_incidents=list(open_incidents), new_id=lambda: next(ids), county_contacts=county,
                   assume_powered_until_seen=assume_powered)


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
        (nobody,) = m.tick(T0 + 2 * ESCALATE)  # the list is exhausted: care staff hear nobody answered
        self.assertEqual(nobody.contact.name, "Nurse")
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

    def test_demo_counts_units_that_never_reported_as_powered_until_they_do(self):
        m = make_monitor(a_patient(), a_patient(device_id="unit-02"), assume_powered=True)
        m.report("unit-02", False, 90, T0)  # the real prototype: its own reports always win
        self.assertEqual(m.status("unit-01", T0)["status"], "ok")
        self.assertNotIn("Unit has never connected", m.status("unit-01", T0)["attention"])
        self.assertEqual(m.status("unit-02", T0)["status"], "outage")


def amps(value):
    return SensorReadings(load_amps=value)


class UnitSensorsAndButtons(unittest.TestCase):
    def test_machine_stopped_alerts_after_a_minute_and_clears_when_running(self):
        m = make_monitor(a_patient(running_amps_min=0.5))
        self.assertEqual(m.report("unit-01", True, 90, T0, readings=amps(2.1)), [])
        self.assertEqual(m.report("unit-01", True, 90, T0 + timedelta(seconds=30), readings=amps(0.1)), [])  # not yet
        (alert,) = m.report("unit-01", True, 90, T0 + timedelta(seconds=90), readings=amps(0.1))
        self.assertIn("oxygen concentrator has stopped", alert.message)
        self.assertIn("Reply OK 1111", alert.message)
        self.assertEqual(m.status("unit-01", T0)["emergency"], "machine")
        self.assertEqual([n.contact.name for n in m.tick(T0 + timedelta(seconds=90) + ESCALATE)], ["Nurse"])
        back = m.report("unit-01", True, 90, T0 + timedelta(minutes=10), readings=amps(2.0))
        self.assertTrue(all("running again" in n.message for n in back))

    def test_no_clamp_or_no_power_means_no_machine_alert(self):
        unclamped = make_monitor()
        unclamped.report("unit-01", True, 90, T0, readings=amps(0.0))
        self.assertEqual(unclamped.report("unit-01", True, 90, T0 + timedelta(minutes=5), readings=amps(0.0)), [])
        m = make_monitor(a_patient(running_amps_min=0.5))
        m.report("unit-01", True, 90, T0, readings=amps(0.1))
        m.report("unit-01", True, 90, T0 + timedelta(seconds=90), readings=amps(0.1))  # machine stopped
        (power,) = m.report("unit-01", False, 90, T0 + timedelta(minutes=3), readings=amps(0.0))
        self.assertIn("Power is out", power.message)  # power loss replaces the machine alert
        self.assertEqual(m.status("unit-01", T0)["emergency"], "power")

    def test_help_button_alerts_everyone_at_once_and_ends_when_answered(self):
        m = make_monitor()
        sent = m.report("unit-01", True, 90, T0, button=Button.HELP)
        self.assertEqual([n.contact.name for n in sent], ["Daughter", "Nurse"])
        self.assertIn("help button was pressed", sent[0].message)
        self.assertEqual(m.tick(T0 + ESCALATE - timedelta(seconds=1)), [])  # nobody left to escalate to
        self.assertEqual(m.report("unit-01", True, 90, T0, button=Button.HELP), [])  # pressing again
        self.assertTrue(m.acknowledge_by_button(sent[1].incident_id, sent[1].alert_index, T0 + timedelta(minutes=2)))
        self.assertEqual(m.status("unit-01", T0)["status"], "ok")
        self.assertEqual(m.drain_changes()[-1].ended_at, T0 + timedelta(minutes=2))

    def test_unit_button_acknowledges_as_someone_at_home(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        m.report("unit-01", False, 90, T0 + timedelta(seconds=40), button=Button.ACK)
        self.assertEqual(m.status("unit-01", T0)["acknowledged_by"], "Someone at home (unit button)")
        self.assertEqual(m.tick(T0 + ESCALATE), [])

    def test_room_too_hot_for_ten_minutes_alerts_care_staff_once(self):
        m = make_monitor()
        hot = SensorReadings(room_temp_f=96)
        m.report("unit-01", False, 90, T0, readings=hot)
        m.report("unit-01", False, 90, T0 + timedelta(minutes=5), readings=hot)
        (alert,) = [n for n in m.report("unit-01", False, 90, T0 + timedelta(minutes=10), readings=hot) if "room" in n.message]
        self.assertEqual(alert.contact.name, "Nurse")
        self.assertIn("96°F, dangerously hot", alert.message)
        self.assertEqual(m.report("unit-01", False, 90, T0 + timedelta(minutes=20), readings=hot), [])
        (calm,) = m.report("unit-01", False, 90, T0 + timedelta(minutes=30), readings=SensorReadings(room_temp_f=75))
        self.assertIn("back to a safe temperature", calm.message)

    def test_report_parsing_keeps_good_values_only(self):
        self.assertEqual(sensor_readings({"load_amps": 1.5, "room_temp_f": 71}), SensorReadings(1.5, 71.0))
        self.assertEqual(sensor_readings({"load_amps": -3, "room_temp_f": "hot"}), SensorReadings())
        self.assertEqual((button_event({"event": "help"}), button_event({"event": "dance"})), (Button.HELP, None))


class NobodyAnswered(unittest.TestCase):
    def test_exhausted_list_marks_the_home_unreached_and_tells_care_staff_once(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        m.tick(T0 + ESCALATE)  # Nurse alerted, the last on the list
        self.assertFalse(m.status("unit-01", T0 + ESCALATE)["unreached"])
        (notice,) = m.tick(T0 + 2 * ESCALATE)
        self.assertEqual(notice.contact.name, "Nurse")
        self.assertIn("Nobody has answered the power-loss alert for Ruth", notice.message)
        self.assertIn("welfare check", notice.message)
        self.assertTrue(m.status("unit-01", T0 + 2 * ESCALATE)["unreached"])
        self.assertEqual(m.tick(T0 + 3 * ESCALATE), [])
        self.assertTrue(m.acknowledge_by_button(notice.incident_id, notice.alert_index, T0 + 3 * ESCALATE))
        self.assertEqual(m.status("unit-01", T0)["acknowledged_by"], "Nurse (push button)")
        self.assertFalse(m.status("unit-01", T0 + 3 * ESCALATE)["unreached"])

    def test_family_only_list_hears_it_themselves(self):
        m = make_monitor(a_patient(contacts=(Contact("Family", "Son", "+16015550109", Channel.SMS),)))
        m.report("unit-01", False, 90, T0)
        (notice,) = m.tick(T0 + ESCALATE)
        self.assertEqual(notice.contact.name, "Son")

    def test_unanswered_help_call_is_unreached_too(self):
        m = make_monitor()
        m.report("unit-01", True, 90, T0, button=Button.HELP)
        self.assertFalse(m.status("unit-01", T0)["unreached"])
        (notice,) = m.tick(T0 + ESCALATE)
        self.assertIn("help-button alert", notice.message)


class BackupRunningOut(unittest.TestCase):
    def test_whole_list_hears_once_when_backup_is_nearly_gone_even_after_an_ack(self):
        m = make_monitor(a_patient(contacts=(
            Contact("caregiver", "Daughter", "+16015550101", Channel.SMS),
            Contact("agency", "Nurse", "+16015550102", Channel.CALL),
            Contact("county", "County EMA", "+16015550103", Channel.CALL)),
            plan=EmergencyPlan(place_name="Forrest General")))
        m.report("unit-01", False, 90, T0)
        m.acknowledge("unit-01", "Daughter (text reply)", T0 + timedelta(minutes=1))
        self.assertEqual(m.tick(T0 + timedelta(minutes=29)), [])
        sent = m.tick(T0 + timedelta(minutes=30))
        self.assertEqual([n.contact.name for n in sent], ["Daughter", "Nurse", "County EMA"])
        self.assertIn("backup has about 30 min left and the power is still out", sent[0].message)
        self.assertIn("get Ruth to Forrest General", sent[0].message)
        self.assertIn("call 911", sent[0].message)
        self.assertNotIn("Reply OK", sent[0].message)  # already acknowledged
        self.assertEqual(m.tick(T0 + timedelta(minutes=40)), [])
        self.assertTrue(m.status("unit-01", T0)["backup_low"])
        incident = m.drain_changes()[-1]
        self.assertEqual(incident.backup_low_at, T0 + timedelta(minutes=30))
        self.assertEqual([a.name for a in incident.alerted], ["Daughter", "Nurse", "County EMA"])
        after = make_monitor(open_incidents=[outage_from_record(outage_to_record(incident))])
        self.assertEqual(after.tick(T0 + timedelta(minutes=41)), [])  # not again after a restart

    def test_unanswered_warning_carries_a_button_and_stops_escalation(self):
        m = make_monitor(a_patient(backup_minutes=20))
        m.report("unit-01", False, 90, T0)
        short = make_monitor(a_patient(backup_minutes=2))
        short.report("unit-01", False, 90, T0)
        urgent = lambda ns: [n for n in ns if "URGENT" in n.message]
        self.assertEqual(urgent(short.tick(T0 + timedelta(seconds=1))), [])  # 1.98 min left rounds to 1, but it's not time yet
        self.assertEqual(len(urgent(short.tick(T0 + timedelta(minutes=1)))), 2)
        self.assertFalse(any("URGENT" in n.message for n in m.tick(T0 + timedelta(minutes=9))))
        sent = [n for n in m.tick(T0 + timedelta(minutes=10)) if "URGENT" in n.message]  # half of a short backup
        self.assertEqual([n.contact.name for n in sent], ["Daughter", "Nurse"])
        self.assertIn("Reply OK 1111", sent[0].message)
        self.assertTrue(m.acknowledge_by_button(sent[1].incident_id, sent[1].alert_index, T0 + timedelta(minutes=11)))
        self.assertEqual(m.status("unit-01", T0)["acknowledged_by"], "Nurse (push button)")

    def test_no_backup_or_power_back_means_no_warning(self):
        m = make_monitor(a_patient(backup_minutes=0))
        m.report("unit-01", False, 90, T0)
        m.acknowledge("unit-01", "nurse@agency.org (agency)", T0)
        self.assertEqual(m.tick(T0 + timedelta(hours=2)), [])
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        m.report("unit-01", True, 90, T0 + timedelta(minutes=10))
        self.assertFalse(any("URGENT" in n.message for n in m.tick(T0 + timedelta(minutes=45))))

    def test_warning_stays_out_of_records_until_it_happens_and_room_detail_is_kept(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        self.assertNotIn("backup_low_at", outage_to_record(m.drain_changes()[-1]))
        hot = SensorReadings(room_temp_f=96)
        m.report("unit-01", True, 90, T0, readings=hot)
        m.report("unit-01", True, 90, T0 + timedelta(minutes=10), readings=hot)
        room = next(i for i in m.drain_changes() if i.kind is IncidentKind.TEMPERATURE)
        self.assertEqual(outage_from_record(outage_to_record(room)).detail, "Room too hot: 96°F")


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


class BoardAttention(unittest.TestCase):
    def test_flags_setup_gaps_that_would_slow_help_down(self):
        family_only = a_patient(backup_minutes=90, contacts=(Contact("Family", "Son", "+16015550109", Channel.SMS),))
        self.assertEqual(family_only.care_gaps(), ["Critical, with under 2 hours of backup", "Only one person on the call list",
                                                   "No nurse or agency on the call list", "No emergency plan"])

    def test_a_well_prepared_patient_has_no_gaps(self):
        prepared = a_patient(acuity=2, plan=EmergencyPlan(supplies="2 oxygen tanks"))
        self.assertEqual(prepared.care_gaps(), [])

    def test_unit_problems_join_the_patient_gaps(self):
        m = make_monitor(a_patient(acuity=2, plan=EmergencyPlan(steps="Call the nurse")))
        self.assertEqual(m.status("unit-01", T0)["attention"], ["Unit has never connected"])
        m.report("unit-01", True, 12, T0, UnitHealth(rssi=-85))
        self.assertEqual(m.status("unit-01", T0)["attention"], ["Unit battery low", "Weak Wi-Fi at the unit"])


class NextAlertOnBoard(unittest.TestCase):
    def test_board_says_who_is_alerted_next_and_when(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        row = m.status("unit-01", T0)
        self.assertEqual(row["next_contact"], {"name": "Nurse", "role": "agency"})
        self.assertEqual(row["next_alert_at"], (T0 + ESCALATE).isoformat())
        self.assertNotIn("phone", row["next_contact"])
        m.tick(T0 + ESCALATE)  # the nurse is alerted; nobody is left after her
        self.assertEqual((m.status("unit-01", T0 + ESCALATE)["next_contact"]), None)

    def test_board_says_when_someone_claimed_it_and_when_each_person_was_alerted(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        m.tick(T0 + ESCALATE)
        m.acknowledge("unit-01", "Nurse (agency)", T0 + ESCALATE + timedelta(seconds=5))
        row = m.status("unit-01", T0 + ESCALATE + timedelta(seconds=5))
        self.assertEqual(row["acknowledged_at"], (T0 + ESCALATE + timedelta(seconds=5)).isoformat())
        self.assertEqual([a["name"] for a in row["contacts_alerted_at"]], ["Daughter", "Nurse"])
        self.assertEqual(row["contacts_alerted_at"][0]["at"], T0.isoformat())
        self.assertNotIn("+1", json.dumps(row["contacts_alerted_at"]))

    def test_nothing_is_next_once_someone_responds(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        m.acknowledge("unit-01", "Daughter (text reply)", T0 + timedelta(seconds=5))
        row = m.status("unit-01", T0 + timedelta(seconds=5))
        self.assertEqual((row["next_contact"], row["next_alert_at"]), (None, None))


COUNTY = Contact("county", "Forrest County EMA", "+16015550199", Channel.SMS)


class FifteenMinuteEmergency(unittest.TestCase):
    def test_county_and_care_staff_are_told_once_with_911_as_fallback(self):
        m = make_monitor(a_patient(backup_minutes=60), county=(COUNTY,))
        m.report("unit-01", False, 90, T0)
        m.acknowledge("unit-01", "Daughter (text reply)", T0 + timedelta(minutes=1))
        self.assertFalse(any("EMERGENCY" in n.message for n in m.tick(T0 + timedelta(minutes=44))))
        sent = [n for n in m.tick(T0 + timedelta(minutes=45)) if "EMERGENCY" in n.message]
        self.assertEqual(sorted(n.contact.name for n in sent), ["Forrest County EMA", "Nurse"])
        self.assertIn("call 911", sent[0].message)
        county = next(n for n in sent if n.contact.name == "Forrest County EMA")
        self.assertNotIn("https://x/s/", county.message)  # the family link can acknowledge; outsiders don't get it
        self.assertIn("https://x/s/", next(n for n in sent if n.contact.name == "Nurse").message)
        self.assertIn("Daughter (text reply) is responding", sent[0].message)
        self.assertTrue(m.status("unit-01", T0 + timedelta(minutes=45))["critical"])
        self.assertFalse(any("EMERGENCY" in n.message for n in m.tick(T0 + timedelta(minutes=50))))  # once

    def test_patients_own_county_contact_wins_and_power_back_means_no_alarm(self):
        own = a_patient(backup_minutes=20, contacts=(Contact("caregiver", "Son", "+16015550111", Channel.SMS),
                                                     Contact("County EMA", "Lamar EMA", "+16015550198", Channel.SMS)))
        m = make_monitor(own, county=(COUNTY,))
        m.report("unit-01", False, 90, T0)
        sent = [n for n in m.tick(T0 + timedelta(minutes=5)) if "EMERGENCY" in n.message]
        self.assertEqual([n.contact.name for n in sent], ["Lamar EMA"])
        m2 = make_monitor(a_patient(backup_minutes=20))
        m2.report("unit-01", False, 90, T0)
        m2.report("unit-01", True, 90, T0 + timedelta(minutes=2))
        self.assertFalse(any("EMERGENCY" in n.message for n in m2.tick(T0 + timedelta(minutes=10))))


class CountyWelfareList(unittest.TestCase):
    def test_lists_every_unanswered_home_per_county_contact(self):
        ruth, james = a_patient(), a_patient(device_id="unit-02", name="James", share_token="j" * 20, backup_minutes=30)
        m = make_monitor(ruth, james, county=(COUNTY,))
        for unit in ("unit-01", "unit-02"):
            m.report(unit, False, 90, T0)
        for step in range(1, 4):
            m.tick(T0 + ESCALATE * step)
        now = T0 + ESCALATE * 3
        notes, missing = m.county_digest(now, "nurse@a.org (agency)")
        self.assertEqual(len(notes), 1)
        self.assertIn("2 homes", notes[0].message)
        self.assertIn("1) James", notes[0].message)  # least backup left first
        self.assertIn("2) Ruth", notes[0].message)
        self.assertEqual(missing, [])
        m.acknowledge("unit-02", "Nurse (push button)", now)
        self.assertIn("1 home ", m.county_digest(now, "x")[0][0].message)

    def test_reports_homes_with_no_county_contact(self):
        m = make_monitor()
        m.report("unit-01", False, 90, T0)
        for step in range(1, 4):
            m.tick(T0 + ESCALATE * step)
        self.assertEqual(m.county_digest(T0 + ESCALATE * 3, "x"), ([], ["Ruth"]))


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
        later = m.tick(T0 + 10 * SILENT_AFTER)  # people are already on the way
        self.assertFalse(any("silent" in n.message for n in later))

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
        self.assertTrue(rows[1].endswith("nurse@agency.org (agency),4,"))

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


class AddressLookup(unittest.TestCase):
    def test_census_matches_are_parsed(self):
        body = {"result": {"addressMatches": [
            {"matchedAddress": "118 W PINE ST, HATTIESBURG, MS, 39401", "coordinates": {"x": -89.2908, "y": 31.3266}}]}}
        self.assertEqual(matches_from_response(body), [Match("118 W PINE ST, HATTIESBURG, MS, 39401", 31.3266, -89.2908)])
        self.assertEqual(matches_from_response({"result": {"addressMatches": []}}), [])


class OneTapRespond(unittest.TestCase):
    def test_tokens_round_trip_and_resist_tampering(self):
        tokens = RespondTokens("k" * 40)
        token = tokens.token("incident-0", 1)
        self.assertEqual(tokens.verify(token), ("incident-0", 1))
        self.assertNotIn("+1601", token)
        payload, signature = token.split(".")
        forged = RespondTokens("k" * 40).token("incident-0", 2).split(".")[0] + "." + signature
        self.assertIsNone(tokens.verify(forged))                     # someone else's alert
        self.assertIsNone(RespondTokens("x" * 40).verify(token))      # another server's secret
        self.assertIsNone(tokens.verify("garbage"))
        with self.assertRaises(ValueError):
            RespondTokens("short")

    def test_button_acknowledges_as_that_contact_until_the_outage_ends(self):
        m = make_monitor()
        (alert,) = m.report("unit-01", False, 90, T0)
        self.assertEqual((alert.incident_id, alert.alert_index), ("incident-0", 0))
        self.assertFalse(m.acknowledge_by_button("incident-0", 5, T0))   # no such alert
        self.assertTrue(m.acknowledge_by_button("incident-0", 0, T0))
        self.assertEqual(m.status("unit-01", T0)["acknowledged_by"], "Daughter (push button)")
        self.assertFalse(m.acknowledge_by_button("incident-0", 0, T0))   # already handled
        m.report("unit-01", True, 90, T0 + timedelta(minutes=1))
        self.assertFalse(m.acknowledge_by_button("incident-0", 0, T0))   # outage over

    def test_only_power_alerts_get_a_button_and_ntfy_shows_it(self):
        sent = []
        notifier = type("Recorder", (), {"send": lambda self, n: sent.append(n)})()
        lifeline = Lifeline(make_monitor(), MemoryStore(a_patient()), notifier, MemoryOutageStore(),
                            respond_link=lambda incident_id, index: f"https://x/api/respond/{incident_id}.{index}")
        lifeline.report("unit-01", False, 90)
        lifeline.report("unit-01", True, 90)
        lifeline._senders.shutdown(wait=True)
        self.assertEqual([n.respond_url for n in sent], ["https://x/api/respond/incident-0.0", None])
        self.assertEqual(lifeline.family_status("s" * 20)["acknowledged_by"], None)
        pushed = []
        ntfy = NtfyNotifier("lifeline-0123456789abcdef")
        ntfy._post = pushed.append
        ntfy.send(sent[0])
        ntfy.send(sent[1])
        self.assertEqual(pushed[0]["actions"][0]["url"], "https://x/api/respond/incident-0.0")
        self.assertNotIn("actions", pushed[1])


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

    def test_plan_and_running_current_round_trip_and_stay_out_of_records_until_set(self):
        self.assertNotIn("plan", patient_to_record(a_patient()))
        self.assertNotIn("running_amps_min", patient_to_record(a_patient()))
        planned = patient_from_form(a_form(plan={"supplies": " 2 tanks ", "place_name": "Forrest General", "steps": "Switch to tank"},
                                           running_amps_min="0.8"), TOKEN, TOKEN)
        self.assertEqual((planned.plan.supplies, planned.running_amps_min), ("2 tanks", 0.8))
        self.assertEqual(patient_from_record(patient_to_record(planned)), planned)
        with self.assertRaisesRegex(InvalidPatient, "Running current"):
            patient_from_form(a_form(running_amps_min=99), TOKEN, TOKEN)
        with self.assertRaisesRegex(InvalidPatient, "at most"):
            patient_from_form(a_form(plan={"steps": "x" * 700}), TOKEN, TOKEN)


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

    def test_family_sees_the_plan_only_once_written(self):
        self.assertIsNone(self.lifeline.family_status("s" * 20)["plan"])
        self.lifeline.update_patient("unit-01", a_form(plan={"place_name": "Forrest General"}))
        self.assertEqual(self.lifeline.family_status("s" * 20)["plan"]["place_name"], "Forrest General")

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

    def test_county_view_shows_urgency_without_numbers_or_tokens(self):
        self.lifeline.add_patient(a_form(device_id="unit-02", name="James"))
        self.lifeline.report("unit-02", False, 90)
        self.lifeline.acknowledge("unit-02", "nurse@agency.org (agency)")
        homes = self.lifeline.county_view()
        self.assertEqual([(h["name"], h["status"], h["responder"]) for h in homes],
                         [("James", "outage", "The care agency"), ("Ruth", "never_seen", None)])
        self.assertIsNotNone(homes[0]["responder_at"])
        self.assertEqual(homes[0]["hazard_details"], [])
        family = self.lifeline.family_status("s" * 20)
        self.assertTrue({"next_contact", "next_alert_at", "acknowledged_at", "contacts_alerted_at"} <= family.keys())
        text = json.dumps(homes)
        for secret in ("+1601", TOKEN, "s" * 20, "nurse@agency.org"):
            self.assertNotIn(secret, text)

    def test_alert_lost_to_a_network_drop_is_retried_until_delivered(self):
        self.addCleanup(setattr, service, "SEND_BACKOFF_SECONDS", service.SEND_BACKOFF_SECONDS)
        service.SEND_BACKOFF_SECONDS = 0
        for retryable, expected_tries, expected_delivered in ((True, 3, 1), (False, 1, 0)):
            with self.subTest(retryable=retryable):
                attempts, delivered = [], []

                class Flaky:
                    def send(self, notification):
                        attempts.append(notification)
                        if len(attempts) < 3:
                            raise service.DeliveryFailed("handshake timed out", retryable)
                        delivered.append(notification)

                lifeline = Lifeline(make_monitor(), MemoryStore(a_patient()), Flaky(), MemoryOutageStore())
                lifeline.report("unit-01", False, 90)
                lifeline._senders.shutdown(wait=True)
                self.assertEqual((len(attempts), len(delivered)), (expected_tries, expected_delivered))

    def test_remove_revokes_link(self):
        self.lifeline.remove_patient("unit-01")
        self.assertIsNone(self.lifeline.family_status("s" * 20))
        self.assertEqual(self.store.saved, {})


FORREST = service.CountyFigures(fips="28035", name="Forrest County, MS", power_dependent=1388, home_health=278, oxygen=467)


class FakeCounts:
    def __init__(self):
        self.asked = []

    def county_at(self, lat, lon):
        self.asked.append((lat, lon))
        if lat < 0:
            raise service.CountsUnavailable("offline")
        return FORREST


class CountyContext(unittest.TestCase):
    def test_counties_sit_next_to_the_homes_lifeline_watches(self):
        counts = FakeCounts()
        lifeline = Lifeline(make_monitor(a_patient(), a_patient(device_id="unit-02", share_token="j" * 20),
                                         a_patient(device_id="unit-03", share_token="k" * 20, lat=-1.0)),
                            MemoryStore(), SilentNotifier(), MemoryOutageStore(), counts=counts)
        self.assertEqual(lifeline.county_context(), [])  # nothing looked up yet
        lifeline.refresh_county_figures()
        lifeline.report("unit-02", False, 90)
        (county,) = lifeline.county_context()  # the home whose lookup failed is left out, not guessed
        self.assertEqual((county["name"], county["power_dependent"], county["home_health"]), ("Forrest County, MS", 1388, 278))
        self.assertEqual((county["lifeline_homes"], county["lifeline_outages"]), (2, 1))
        lifeline.refresh_county_figures()
        self.assertEqual(len(counts.asked), 4)  # only the unplaced home is asked again

    def test_emPOWER_answer_is_parsed_and_a_miss_is_unavailable(self):
        from empower import county_from_response
        answer = {"features": [{"attributes": {"NAMELSAD": "Forrest County", "State": "MS", "FIPS_Code": "28035",
                                               "Power_Dependent_Devices_DME": 1388, "Home_Health_Services_Any_DME": 278,
                                               "O2_Services_Any_DME": 467}}]}
        self.assertEqual(county_from_response(answer), FORREST)
        for broken in ({"features": []}, {"error": {"code": 400}}, {"features": [{"attributes": {"State": "MS"}}]}):
            with self.subTest(broken=broken), self.assertRaises(service.CountsUnavailable):
                county_from_response(broken)

    def test_county_sees_unreached_homes_first(self):
        lifeline = Lifeline(make_monitor(a_patient(), a_patient(device_id="unit-02", share_token="j" * 20, name="James", backup_minutes=600)),
                            MemoryStore(), SilentNotifier(), MemoryOutageStore())
        now = service.utc_now()
        lifeline._monitor.report("unit-02", False, 90, now - 10 * ESCALATE)
        lifeline._monitor.tick(now - 9 * ESCALATE)  # James's Nurse, the last on his list
        lifeline._monitor.tick(now - 8 * ESCALATE)  # ...who doesn't answer either
        lifeline._monitor.report("unit-01", False, 90, now)
        homes = lifeline.county_view()
        self.assertEqual([(h["name"], h["unreached"]) for h in homes], [("James", True), ("Ruth", False)])


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
        sessions = Sessions("k" * 40)
        sid = sessions.start(StaffMember("nurse@agency.org"))
        self.assertEqual(sessions.member(sid).email, "nurse@agency.org")
        sessions.end(sid)
        self.assertIsNone(sessions.member(sid))
        self.assertIsNone(sessions.member(None))

    def test_sessions_survive_a_restart_but_not_tampering_or_expiry(self):
        token = Sessions("k" * 40).start(StaffMember("nurse@agency.org"), now=T0)
        restarted = Sessions("k" * 40)  # same secret, fresh process
        self.assertEqual(restarted.member(token, now=T0 + timedelta(hours=1)), StaffMember("nurse@agency.org"))
        self.assertIsNone(restarted.member(token, now=T0 + timedelta(hours=13)))       # expired
        self.assertIsNone(Sessions("x" * 40).member(token, now=T0))                    # other server's secret
        payload, signature = token.split(".")
        forged = Sessions("x" * 40).start(StaffMember("boss@agency.org"), now=T0).split(".")[0] + "." + signature
        self.assertIsNone(restarted.member(forged, now=T0))                            # swapped identity
        with self.assertRaises(ValueError):
            Sessions("short")

    def test_signed_out_cookie_stays_out_even_when_re_encoded(self):
        sessions = Sessions("k" * 40)
        token = sessions.start(StaffMember("nurse@agency.org"))
        payload, signature = token.split(".")
        sessions.end(token)
        # Same bytes, different spellings: stray padding, or a last character whose spare bits differ.
        last = signature[-1]
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        sibling = alphabet[alphabet.index(last) ^ 1]
        for variant in (f"{payload}.{signature}=", f"{payload}.{signature[:-1]}{sibling}", f"{payload}=.{signature}"):
            with self.subTest(variant=variant[-6:]):
                self.assertIsNone(sessions.member(variant))
        self.assertIsNone(sessions.member(token))
        self.assertEqual(sessions.member(sessions.start(StaffMember("nurse@agency.org"))).email, "nurse@agency.org")

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
        app.Handler.sessions = Sessions("k" * 40)
        app.Handler.staff = StaffDirectory(MemoryStaffStore(), [])
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for path in ("/api/history", "/api/history.csv?days=7", "/api/weather-map", "/api/patients"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5)
            self.assertEqual(refused.exception.code, 401)
            refused.exception.close()


class StaticFiles(unittest.TestCase):
    def test_map_library_and_its_images_are_served_and_nothing_outside_static(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/static/"
        for name in ("leaflet.js", "leaflet.css", "images/layers.png", "map.js"):
            with self.subTest(name=name), urllib.request.urlopen(base + name, timeout=5) as response:
                self.assertEqual(response.status, 200)
        for name in ("../app.py", "images/../../.env", "..%2Fapp.py", ".env"):
            with self.subTest(name=name), self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(base + name, timeout=5)
            self.assertEqual(refused.exception.code, 404)
            refused.exception.close()


class HostedDeployment(unittest.TestCase):
    def test_health_check_answers_without_signing_in(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/healthz", timeout=5) as response:
            self.assertEqual((response.status, json.loads(response.read())), (200, {"ok": True}))

    def test_client_address_comes_from_the_proxy_header_only_when_configured(self):
        handler = app.Handler.__new__(app.Handler)
        handler.client_address = ("10.0.0.7", 5000)
        handler.headers = {"Fly-Client-IP": "203.0.113.9"}
        self.assertEqual(handler._client_ip(), "10.0.0.7")
        handler.client_ip_header = "Fly-Client-IP"
        self.assertEqual(handler._client_ip(), "203.0.113.9")
        handler.headers = {}
        self.assertEqual(handler._client_ip(), "10.0.0.7")

    def test_a_spoofed_proxy_header_never_makes_a_remote_caller_local(self):
        handler = app.Handler.__new__(app.Handler)
        handler.client_ip_header = "Fly-Client-IP"
        handler.client_address, handler.headers = ("10.0.0.7", 5000), {"Fly-Client-IP": "127.0.0.1"}
        self.assertFalse(handler._is_local_request())
        handler.client_address, handler.headers = ("127.0.0.1", 5000), {"Fly-Client-IP": "203.0.113.9"}
        self.assertFalse(handler._is_local_request())  # a proxy on this host forwarding an outside caller
        handler.headers = {}
        self.assertTrue(handler._is_local_request())


class MemoryStaffStore:
    def __init__(self, *accounts):
        self.accounts = {a.email: a for a in accounts}

    def list_all(self):
        return list(self.accounts.values())

    def save(self, account):
        self.accounts[account.email] = account

    def delete(self, email):
        self.accounts.pop(email, None)


class FakeAccounts:
    """Stands in for Supabase Auth: remembers passwords so sign-in and changes can be checked."""

    def __init__(self, **passwords):
        self.passwords = dict(passwords)

    def create(self, email, temporary_password):
        if email in self.passwords:
            return False
        self.passwords[email] = temporary_password
        return True

    def change_password(self, email, current_password, new_password):
        if self.passwords.get(email) != current_password:
            return False
        self.passwords[email] = new_password
        return True


class SupabaseLikeAccounts(FakeAccounts):
    """Like SupabaseAuthenticator.create: an existing account gets the temporary password too."""

    def create(self, email, temporary_password):
        self.passwords[email] = temporary_password
        return True


class StaffDirectoryRules(unittest.TestCase):
    def directory(self):
        store = MemoryStaffStore(StaffAccount("nurse@agency.org", Role.NURSE))
        return store, StaffDirectory(store, store.list_all(), bootstrap_admin="Boss@Agency.org")

    def test_bootstrap_admin_and_stored_roles(self):
        _, staff = self.directory()
        self.assertEqual(staff.account("boss@agency.org").role, Role.ADMIN)
        self.assertEqual(staff.account(" NURSE@agency.org ").role, Role.NURSE)
        self.assertIsNone(staff.account("stranger@agency.org"))

    def test_role_changes_are_saved_and_take_effect_at_once(self):
        store, staff = self.directory()
        staff.set_role("boss@agency.org", "County@Forrest.gov", Role.COUNTY)
        self.assertEqual(store.accounts["county@forrest.gov"].role, Role.COUNTY)
        staff.remove("boss@agency.org", "nurse@agency.org")
        self.assertIsNone(staff.account("nurse@agency.org"))

    def test_admins_cannot_lock_themselves_out(self):
        _, staff = self.directory()
        with self.assertRaises(InvalidStaffChange):
            staff.set_role("boss@agency.org", "boss@agency.org", Role.VIEWER)
        with self.assertRaises(InvalidStaffChange):
            staff.remove("boss@agency.org", "boss@agency.org")
        with self.assertRaises(InvalidStaffChange):
            staff.set_role("boss@agency.org", "not-an-email", Role.NURSE)

    def test_each_role_gets_only_its_permissions(self):
        self.assertTrue(StaffAccount("a@b.org", Role.ADMIN).can(Permission.MANAGE_STAFF))
        nurse, viewer, county = (StaffAccount("a@b.org", r) for r in (Role.NURSE, Role.VIEWER, Role.COUNTY))
        self.assertTrue(nurse.can(Permission.RESPOND) and nurse.can(Permission.VIEW_PATIENTS))
        self.assertFalse(nurse.can(Permission.MANAGE_PATIENTS) or nurse.can(Permission.MANAGE_STAFF))
        self.assertFalse(viewer.can(Permission.RESPOND) or viewer.can(Permission.VIEW_PATIENTS))
        self.assertEqual({p for p in Permission if county.can(p)}, {Permission.VIEW_COUNTY})


class RoleEnforcement(unittest.TestCase):
    """Over HTTP: the server, not the page, decides what each role may do."""

    def setUp(self):
        app.Handler.sessions = Sessions("k" * 40)
        app.Handler.public_url = "http://lifeline.test"
        app.Handler.accounts = self.accounts = FakeAccounts(**{"nurse@a.org": "old-password-1"})
        app.Handler.throttle = LoginThrottle()
        app.Handler.lifeline = Lifeline(make_monitor(), MemoryStore(a_patient()), SilentNotifier(), MemoryOutageStore())
        app.Handler.staff = StaffDirectory(MemoryStaffStore(), [
            StaffAccount("admin@a.org", Role.ADMIN), StaffAccount("nurse@a.org", Role.NURSE),
            StaffAccount("viewer@a.org", Role.VIEWER), StaffAccount("county@c.gov", Role.COUNTY)])
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def call(self, email, method, path, body=None):
        headers = {"Content-Type": "application/json"}
        if email:
            headers["Cookie"] = f"{app.SESSION_COOKIE}={app.Handler.sessions.start(StaffMember(email))}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_what_each_role_can_reach(self):
        expected = {  # path: (admin, nurse, viewer, county, signed in without a role)
            ("GET", "/api/status"): (200, 200, 200, 403, 403),
            ("GET", "/api/history"): (200, 200, 200, 403, 403),
            ("GET", "/api/patients"): (200, 200, 403, 403, 403),
            ("GET", "/api/staff"): (200, 403, 403, 403, 403),
            ("GET", "/api/county"): (200, 403, 403, 200, 403),
            ("POST", "/api/patients/unit-01/rotate-link"): (200, 403, 403, 403, 403),
        }
        people = ("admin@a.org", "nurse@a.org", "viewer@a.org", "county@c.gov", "nobody@a.org")
        for (method, path), codes in expected.items():
            for email, code in zip(people, codes):
                with self.subTest(path=path, email=email):
                    self.assertEqual(self.call(email, method, path, {} if method == "POST" else None)[0], code)
        self.assertEqual(self.call(None, "GET", "/api/status")[0], 401)

    def test_nurses_see_call_lists_but_not_unit_tokens_or_family_links(self):
        nurse_view = self.call("nurse@a.org", "GET", "/api/patients")[1]["patients"][0]
        self.assertEqual(nurse_view["contacts"][0]["phone"], "+16015550101")
        self.assertNotIn("device_token", nurse_view)
        self.assertNotIn("share_token", nurse_view)
        self.assertNotIn("status_link", nurse_view)
        admin_view = self.call("admin@a.org", "GET", "/api/patients")[1]["patients"][0]
        self.assertEqual(admin_view["device_token"], TOKEN)

    def test_board_shows_phones_and_plans_only_to_care_staff(self):
        nurse_unit = self.call("nurse@a.org", "GET", "/api/status")[1]["units"][0]
        self.assertEqual(nurse_unit["contacts"][0]["phone"], "+16015550101")
        self.assertIn("plan", nurse_unit)
        self.assertNotIn("device_token", json.dumps(nurse_unit))
        viewer_unit = self.call("viewer@a.org", "GET", "/api/status")[1]["units"][0]
        self.assertNotIn("contacts", viewer_unit)
        self.assertNotIn("plan", viewer_unit)
        self.assertNotIn("+16015550101", json.dumps(viewer_unit))

    def test_only_care_staff_can_send_the_county_list(self):
        self.assertEqual(self.call("viewer@a.org", "POST", "/api/county/notify", {})[0], 403)
        status, body = self.call("nurse@a.org", "POST", "/api/county/notify", {})
        self.assertEqual((status, body["error"]), (409, "Every home has been answered; there is nothing to send."))

    def test_only_responders_can_acknowledge(self):
        app.Handler.lifeline.report("unit-01", False, 90)
        self.assertEqual(self.call("viewer@a.org", "POST", "/api/ack/unit-01", {})[0], 403)
        self.assertEqual(self.call("nurse@a.org", "POST", "/api/ack/unit-01", {}), (200, {"acknowledged": True}))

    def test_admin_manages_roles_and_a_removed_person_loses_access_immediately(self):
        status, added = self.call("admin@a.org", "POST", "/api/staff", {"email": "New@A.org", "role": "viewer"})
        self.assertEqual((status, added["email"]), (201, "new@a.org"))
        self.assertEqual(self.accounts.passwords["new@a.org"], added["temporary_password"])
        self.assertEqual(self.call("new@a.org", "GET", "/api/status")[0], 200)
        self.assertEqual(self.call("admin@a.org", "POST", "/api/staff/remove", {"email": "new@a.org"})[0], 200)
        self.assertEqual(self.call("new@a.org", "GET", "/api/status")[0], 403)
        status, body = self.call("admin@a.org", "PUT", "/api/staff", {"email": "admin@a.org", "role": "nurse"})
        self.assertEqual(status, 400)
        self.assertIn("own admin role", body["error"])

    def test_adding_someone_who_already_has_an_account_keeps_their_password(self):
        status, added = self.call("admin@a.org", "POST", "/api/staff", {"email": "nurse@a.org", "role": "viewer"})
        self.assertEqual((status, added["temporary_password"]), (201, None))
        self.assertEqual(self.accounts.passwords["nurse@a.org"], "old-password-1")
        self.assertEqual(self.call("nurse@a.org", "GET", "/api/me")[1]["role"], "viewer")

    def test_a_refused_self_demotion_through_add_staff_leaves_the_password_alone(self):
        app.Handler.accounts = self.accounts = SupabaseLikeAccounts(**{"admin@a.org": "admin-password-1"})
        status, body = self.call("admin@a.org", "POST", "/api/staff", {"email": "admin@a.org", "role": "viewer"})
        self.assertEqual(status, 400)
        self.assertIn("own admin role", body["error"])
        self.assertEqual(self.accounts.passwords["admin@a.org"], "admin-password-1")

    def test_people_change_their_own_password_with_the_current_one(self):
        change = lambda current, new: self.call("nurse@a.org", "POST", "/api/password", {"current_password": current, "new_password": new})
        self.assertEqual(change("wrong-password", "brand-new-password")[0], 400)
        self.assertEqual(change("old-password-1", "short")[0], 400)
        self.assertEqual(change("old-password-1", "brand-new-password"), (200, {"ok": True}))
        self.assertEqual(self.accounts.passwords["nurse@a.org"], "brand-new-password")
        self.assertEqual(self.call(None, "POST", "/api/password", {})[0], 401)

    def test_me_reports_role_and_permissions(self):
        status, me = self.call("viewer@a.org", "GET", "/api/me")
        self.assertEqual((status, me["role"]), (200, "viewer"))
        self.assertEqual(me["permissions"], ["view_board", "view_history"])


class LandingAndCompression(unittest.TestCase):
    def setUp(self):
        app.Handler.sessions = Sessions("k" * 40)
        app.Handler.staff = StaffDirectory(MemoryStaffStore(), [StaffAccount("nurse@a.org", Role.NURSE)])
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def get(self, path, headers=None):
        with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers or {}), timeout=5) as response:
            return response.status, dict(response.headers), response.read()

    def test_signed_out_visitors_get_the_landing_page_and_staff_get_the_board(self):
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"When the power goes out, someone has to know.", body)
        cookie = f"{app.SESSION_COOKIE}={app.Handler.sessions.start(StaffMember('nurse@a.org'))}"
        self.assertIn(b'id="view-board"', self.get("/", {"Cookie": cookie})[2])

    def test_text_is_gzipped_only_when_the_client_asks(self):
        import gzip as gz
        _, headers, body = self.get("/static/landing.css", {"Accept-Encoding": "gzip, br"})
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertIn(b"--night", gz.decompress(body))
        _, plain_headers, plain = self.get("/static/landing.css")
        self.assertNotIn("Content-Encoding", plain_headers)
        self.assertEqual(gz.decompress(body), plain)
        _, image_headers, _ = self.get("/static/images/hero-night.webp", {"Accept-Encoding": "gzip"})
        self.assertNotIn("Content-Encoding", image_headers)


class RespondRoute(unittest.TestCase):
    def test_forged_button_link_is_refused_and_a_real_one_acknowledges(self):
        app.Handler.respond_tokens = RespondTokens("k" * 40)
        app.Handler.lifeline = Lifeline(make_monitor(), MemoryStore(a_patient()), SilentNotifier(), MemoryOutageStore())
        app.Handler.lifeline.report("unit-01", False, 90)
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/api/respond/"
        forged = RespondTokens("x" * 40).token("incident-0", 0)
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(urllib.request.Request(base + forged, method="POST"), timeout=5)
        self.assertEqual(refused.exception.code, 404)
        refused.exception.close()
        real = app.Handler.respond_tokens.token("incident-0", 0)
        with urllib.request.urlopen(urllib.request.Request(base + real, method="POST"), timeout=5) as response:
            self.assertEqual(json.load(response), {"acknowledged": True})
        self.assertEqual(app.Handler.lifeline.family_status("s" * 20)["acknowledged_by"], "Someone on the call list")


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
        if self.path.startswith("/rest/v1/slow"):
            time.sleep(0.5)  # accepts the connection, then goes quiet mid-reply
            return
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

    def test_a_read_timeout_is_reported_as_unreachable_not_a_crash(self):
        import supabase_backend
        self.addCleanup(setattr, supabase_backend, "TIMEOUT_SECONDS", supabase_backend.TIMEOUT_SECONDS)
        supabase_backend.TIMEOUT_SECONDS = 0.1
        with self.assertRaises(supabase_backend.SupabaseUnreachable):
            self.client().request("GET", "/rest/v1/slow")

    def test_auth(self):
        auth = SupabaseAuthenticator(self.client())
        self.assertEqual(auth.sign_in("nurse@agency.org", "right"), StaffMember("nurse@agency.org"))
        self.assertIsNone(auth.sign_in("nurse@agency.org", "wrong"))
        self.assertIsNone(auth.sign_in("", ""))


if __name__ == "__main__":
    unittest.main()

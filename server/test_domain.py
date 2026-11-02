"""Run: python3 test_domain.py"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain import Channel, Contact, InvalidPatient, Monitor, Patient, ReplyResult, UnknownDevice
from local import JsonFilePatientStore, SharedPasswordAuthenticator
from notifiers import is_valid_twilio_signature
from records import patient_from_form, patient_from_record, patient_to_record
from service import DuplicateDevice, Lifeline
from staff import LoginThrottle, Sessions, StaffMember
from supabase_backend import SupabaseAuthenticator, SupabaseClient, SupabaseError, SupabasePatientStore

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


def make_monitor(*patients: Patient, codes=None) -> Monitor:
    codes = iter(codes or ["1111", "2222", "3333"])
    return Monitor(list(patients or [a_patient()]), escalate_after=ESCALATE,
                   offline_after=timedelta(seconds=90), status_link=lambda p: f"https://x/s/{p.share_token}",
                   new_reply_code=lambda: next(codes))


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
        self.assertEqual(m.acknowledge_by_reply("+19995550000", "OK").result, ReplyResult.NOTHING_OPEN)
        self.assertEqual(m.acknowledge_by_reply("+16015550101", "okay").result, ReplyResult.NOTHING_OPEN)
        outcome = m.acknowledge_by_reply("+16015550101", "ok")
        self.assertEqual((outcome.result, outcome.device_id), (ReplyResult.ACKNOWLEDGED, "unit-01"))
        self.assertEqual(m.tick(T0 + ESCALATE), [])
        self.assertEqual(m.status("unit-01", T0)["acknowledged_by"], "+16015550101")

    def test_shared_contact_must_name_the_outage(self):
        nurse = Contact("agency", "Nurse", "+16015550102", Channel.CALL)
        ruth = a_patient(contacts=(nurse,))
        james = a_patient(device_id="unit-02", share_token="j" * 20, name="James", contacts=(nurse,))
        m = make_monitor(ruth, james, codes=["1111", "1111", "2222"])  # duplicate code is re-drawn
        m.report("unit-01", False, 90, T0)
        m.report("unit-02", False, 90, T0)
        self.assertEqual(m.acknowledge_by_reply(nurse.phone, "OK").result, ReplyResult.AMBIGUOUS)
        self.assertIsNone(m.status("unit-01", T0)["acknowledged_by"])
        outcome = m.acknowledge_by_reply(nurse.phone, "OK 2222")
        self.assertEqual((outcome.result, outcome.device_id), (ReplyResult.ACKNOWLEDGED, "unit-02"))
        self.assertIsNone(m.status("unit-01", T0)["acknowledged_by"])
        # Now only Ruth is open for this phone, so a bare OK is unambiguous again.
        self.assertEqual(m.acknowledge_by_reply(nurse.phone, "ok").device_id, "unit-01")

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


class RosterUseCases(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(a_patient())
        self.lifeline = Lifeline(make_monitor(*self.store.list_all()), self.store, SilentNotifier())

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

    def test_auth(self):
        auth = SupabaseAuthenticator(self.client())
        self.assertEqual(auth.sign_in("nurse@agency.org", "right"), StaffMember("nurse@agency.org"))
        self.assertIsNone(auth.sign_in("nurse@agency.org", "wrong"))
        self.assertIsNone(auth.sign_in("", ""))


if __name__ == "__main__":
    unittest.main()

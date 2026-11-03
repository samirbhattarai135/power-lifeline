"""HTTP delivery layer and composition root.

Three audiences, three trust levels:
  devices  -> POST /api/report               (per-unit token header)
  families -> /s/<share-token>, /api/family/* (unguessable per-patient link)
  staff    -> /, /api/patients, /api/status   (signed-in session cookie)

Run:  python3 app.py   (see README for Supabase and Twilio settings)
"""

from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import threading
import urllib.parse
from datetime import timedelta
from zoneinfo import ZoneInfo
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from domain import DEVICE_ID_PATTERN, IncidentKind, InvalidPatient, Monitor, Outage, Patient, ReplyResult, UnitHealth, UnknownDevice
from local import JsonFileOutageStore, JsonFilePatientStore, SharedPasswordAuthenticator
from notifiers import WHATSAPP_PREFIX, ConsoleNotifier, NtfyNotifier, TwilioNotifier, is_valid_twilio_signature
from records import patient_to_record
from service import DuplicateDevice, Lifeline, Notifier, OutageStore, PatientStore, utc_now
from staff import LoginThrottle, Sessions, StaffAuthenticator, StaffMember
from storm import StormWatch, WeatherSource
from weather import DemoWeather, NwsWeather
from supabase_backend import SupabaseAuthenticator, SupabaseClient, SupabaseError, SupabaseOutageStore, SupabasePatientStore

HERE = Path(__file__).parent
STATIC = HERE / "static"
MAX_BODY_BYTES = 16 * 1024
SESSION_COOKIE = "lifeline_session"
LOOPBACK_ADDRESSES = {"127.0.0.1", "::1"}
FIRMWARE_PATTERN = re.compile(r"^[A-Za-z0-9._+-]{1,32}$")
MAX_UPTIME_SECONDS = 10 * 365 * 24 * 3600
HISTORY_DAYS_DEFAULT, HISTORY_DAYS_MAX = 30, 365
SMS_REPLIES = {
    ReplyResult.ACKNOWLEDGED: "Thanks. You're marked as responding, so we've stopped alerting the rest of the call list.",
    ReplyResult.AMBIGUOUS: "You're on the call list for more than one home without power. "
                           "Reply OK and the 4-digit code from the alert you're responding to, like OK 4821.",
    ReplyResult.NOTHING_OPEN: "No open Lifeline alert matches that reply. Reply OK and the 4-digit code from the alert.",
}
log = logging.getLogger("lifeline")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    # Family links carry a secret in the URL path. Cross-site requests send only our origin,
    # never the path, which keeps the secret private and still satisfies OpenStreetMap's tile policy.
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' https://unpkg.com; "
        "style-src 'self' https://unpkg.com https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https://*.tile.openstreetmap.org https://unpkg.com https://opengeo.ncep.noaa.gov; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Route:
    def __init__(self, method: str, pattern: str, handler: Callable, staff_only: bool = False):
        self.method = method
        self.pattern = re.compile(f"^{pattern}$")
        self.handler = handler
        self.staff_only = staff_only


class Handler(BaseHTTPRequestHandler):
    lifeline: Lifeline
    authenticator: StaffAuthenticator
    sessions: Sessions
    throttle: LoginThrottle
    public_url: str
    twilio_auth_token: str | None
    storm_mode: str = "off"            # on, off or demo; set by main()
    storm_check_seconds: int = 300
    routes: list[Route]

    # ---- dispatch -----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        for route in self.routes:
            match = route.pattern.match(path)
            if route.method != method or not match:
                continue
            try:
                if route.staff_only:
                    self._require_staff(method)
                route.handler(self, *match.groups())
            except HttpError as error:
                self._send_json(error.status, {"error": error.message})
            except InvalidPatient as error:
                status = HTTPStatus.CONFLICT if isinstance(error, DuplicateDevice) else HTTPStatus.BAD_REQUEST
                self._send_json(status, {"error": str(error)})
            except UnknownDevice:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "No patient with that unit ID"})
            except SupabaseError as error:
                log.error("%s", error)
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "The database is unreachable. Try again shortly."})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    # ---- pages and static assets ------------------------------------------

    def page_platform(self) -> None:
        if self._staff() is None:
            self._redirect("/login")
            return
        self._send_file(STATIC / "platform.html")

    def page_login(self) -> None:
        if self._staff() is not None:
            self._redirect("/")
            return
        self._send_file(STATIC / "login.html")

    def page_family(self, _share_token: str) -> None:
        self._send_file(STATIC / "status.html")

    def static_asset(self, name: str) -> None:
        path = (STATIC / name).resolve()
        if path.parent != STATIC.resolve() or not path.is_file():
            raise HttpError(HTTPStatus.NOT_FOUND, "not found")
        self._send_file(path)

    # ---- devices and Twilio -------------------------------------------------

    def api_report(self) -> None:
        data = self._json_body()
        device_id, has_wall_power = data.get("device_id"), data.get("wall_power")
        battery = data.get("battery_percent")
        if not isinstance(device_id, str) or not isinstance(has_wall_power, bool):
            raise HttpError(HTTPStatus.BAD_REQUEST, "expected {device_id: str, wall_power: bool, battery_percent?: int}")
        if not isinstance(battery, int) or isinstance(battery, bool) or not 0 <= battery <= 100:
            battery = None
        if not hmac.compare_digest(self.headers.get("X-Device-Token", ""), self.lifeline.device_token(device_id)):
            raise HttpError(HTTPStatus.UNAUTHORIZED, "bad device token")
        self.lifeline.report(device_id, has_wall_power, battery, unit_health(data))
        self._send_json(HTTPStatus.OK, {"ok": True})

    def api_sms_inbound(self) -> None:
        params = dict(urllib.parse.parse_qsl(self._body().decode(errors="replace")))
        if self.twilio_auth_token:
            if not is_valid_twilio_signature(self.twilio_auth_token, self.public_url + self.path, params,
                                             self.headers.get("X-Twilio-Signature", "")):
                raise HttpError(HTTPStatus.FORBIDDEN, "bad signature")
        elif self.client_address[0] not in LOOPBACK_ADDRESSES:
            # Without Twilio there is no signature to check, so a forged "OK" could silence an
            # alert. Fail closed: only simulated replies from this machine are accepted.
            raise HttpError(HTTPStatus.FORBIDDEN, "SMS replies need Twilio signature checking")
        sender = params.get("From", "").removeprefix(WHATSAPP_PREFIX)  # WhatsApp replies arrive as "whatsapp:+1..."
        outcome = self.lifeline.acknowledge_by_reply(sender, params.get("Body", ""))
        reply = SMS_REPLIES[outcome.result]
        self._send(HTTPStatus.OK, f"<Response><Message>{reply}</Message></Response>".encode(), "application/xml")

    # ---- families -----------------------------------------------------------

    def api_family_status(self, share_token: str) -> None:
        status = self.lifeline.family_status(share_token)
        if status is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "This link is no longer active. Ask the care agency for a new one.")
        self._send_json(HTTPStatus.OK, status)

    def api_family_ack(self, share_token: str) -> None:
        self._json_body()  # JSON-only, so a cross-site form post cannot trigger it
        if self.lifeline.family_status(share_token) is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "This link is no longer active.")
        self._send_json(HTTPStatus.OK, {"acknowledged": self.lifeline.family_acknowledge(share_token)})

    # ---- staff session -----------------------------------------------------

    def api_login(self) -> None:
        data = self._json_body()
        email, password = data.get("email"), data.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            raise HttpError(HTTPStatus.BAD_REQUEST, "Enter your email and password.")
        email = email.strip().lower()
        address = self.client_address[0]
        if self.throttle.is_blocked(address, email):
            raise HttpError(HTTPStatus.TOO_MANY_REQUESTS, "Too many attempts. Wait 15 minutes and try again.")
        member = self.authenticator.sign_in(email, password)
        if member is None:
            self.throttle.record_failure(address, email)
            raise HttpError(HTTPStatus.UNAUTHORIZED, "That email and password don't match a staff account.")
        self.throttle.record_success(address, email)
        session_id = self.sessions.start(member)
        self._send_json(HTTPStatus.OK, {"email": member.email}, cookie=self._session_cookie(session_id))

    def api_logout(self) -> None:
        self.sessions.end(self._session_id())
        self._send_json(HTTPStatus.OK, {"ok": True}, cookie=self._session_cookie("", expire=True))

    def api_me(self) -> None:
        self._send_json(HTTPStatus.OK, {"email": self._staff().email})

    # ---- staff: live board and roster ---------------------------------------

    def api_status(self) -> None:
        self._send_json(HTTPStatus.OK, {"units": self.lifeline.snapshot()})

    def api_staff_ack(self, device_id: str) -> None:
        by = f"{self._staff().email} (agency)"
        self._send_json(HTTPStatus.OK, {"acknowledged": self.lifeline.acknowledge(device_id, by)})

    def api_list_patients(self) -> None:
        self._send_json(HTTPStatus.OK, {"patients": [self._staff_view(p) for p in self.lifeline.roster()]})

    def api_add_patient(self) -> None:
        self._send_json(HTTPStatus.CREATED, self._staff_view(self.lifeline.add_patient(self._json_body())))

    def api_update_patient(self, device_id: str) -> None:
        self._send_json(HTTPStatus.OK, self._staff_view(self.lifeline.update_patient(device_id, self._json_body())))

    def api_rotate_link(self, device_id: str) -> None:
        self._send_json(HTTPStatus.OK, self._staff_view(self.lifeline.rotate_share_link(device_id)))

    def api_delete_patient(self, device_id: str) -> None:
        self.lifeline.remove_patient(device_id)
        self._send_json(HTTPStatus.OK, {"ok": True})

    def api_weather_map(self) -> None:
        self._send_json(HTTPStatus.OK, {"mode": self.storm_mode, "check_every_seconds": self.storm_check_seconds,
                                        **self.lifeline.weather_map()})

    def api_history(self) -> None:
        days, device_id = self._history_query()
        rows = [incident_view(i) for i in self.lifeline.history(days, device_id)]
        self._send_json(HTTPStatus.OK, {"days": days, "incidents": rows})

    def api_history_csv(self) -> None:
        days, device_id = self._history_query()
        body = incidents_csv(self.lifeline.history(days, device_id)).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="lifeline-incidents-last-{days}-days.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _history_query(self) -> tuple[int, str | None]:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            days = int(query.get("days", [HISTORY_DAYS_DEFAULT])[0])
        except ValueError:
            raise HttpError(HTTPStatus.BAD_REQUEST, "days must be a whole number") from None
        if not 1 <= days <= HISTORY_DAYS_MAX:
            raise HttpError(HTTPStatus.BAD_REQUEST, f"days must be between 1 and {HISTORY_DAYS_MAX}")
        device_id = query.get("device_id", [None])[0]
        if device_id is not None and not DEVICE_ID_PATTERN.match(device_id):
            raise HttpError(HTTPStatus.BAD_REQUEST, "Unknown unit ID format")
        return days, device_id

    def _staff_view(self, patient: Patient) -> dict:
        record = patient_to_record(patient)
        record.pop("share_token")
        record["status_link"] = family_link(self.public_url, patient)
        return record

    # ---- request helpers ----------------------------------------------------

    def _session_id(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _staff(self) -> StaffMember | None:
        return self.sessions.member(self._session_id())

    def _require_staff(self, method: str) -> None:
        if self._staff() is None:
            raise HttpError(HTTPStatus.UNAUTHORIZED, "Sign in to continue.")
        if method != "GET":
            self._require_same_origin()

    def _require_same_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            raise HttpError(HTTPStatus.FORBIDDEN, "Cross-site request refused.")

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY_BYTES:
            raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large.")
        return self.rfile.read(length)

    def _json_body(self) -> dict:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Send JSON.")
        try:
            data = json.loads(self._body() or b"{}")
        except ValueError:
            raise HttpError(HTTPStatus.BAD_REQUEST, "Malformed JSON.") from None
        if not isinstance(data, dict):
            raise HttpError(HTTPStatus.BAD_REQUEST, "Expected a JSON object.")
        return data

    def _session_cookie(self, value: str, expire: bool = False) -> str:
        parts = [f"{SESSION_COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Strict"]
        parts.append("Max-Age=0" if expire else "Max-Age=43200")
        # Secure only when this request really arrived over https (ngrok sets X-Forwarded-Proto).
        # Keying it off LIFELINE_PUBLIC_URL would break staff on plain http://<pi-ip>:8080.
        if self.headers.get("X-Forwarded-Proto") == "https":
            parts.append("Secure")
        return "; ".join(parts)

    # ---- response helpers ---------------------------------------------------

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_file(self, path: Path) -> None:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, path.read_bytes(), content_type)

    def _send_json(self, status: HTTPStatus, payload: dict, cookie: str | None = None) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json", cookie)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # Request lines contain family-link tokens; keep them out of logs.
        log.debug("%s %s", self.address_string(), re.sub(r"/(s|api/family)/[^/ ]+", r"/\1/<link>", fmt % args))


Handler.routes = [
    Route("GET", r"/", Handler.page_platform),
    Route("GET", r"/login", Handler.page_login),
    Route("GET", r"/s/([A-Za-z0-9_-]{16,})", Handler.page_family),
    Route("GET", r"/static/([A-Za-z0-9_.-]+)", Handler.static_asset),
    Route("POST", r"/api/report", Handler.api_report),
    Route("POST", r"/api/sms-inbound", Handler.api_sms_inbound),
    Route("GET", r"/api/family/([A-Za-z0-9_-]{16,})", Handler.api_family_status),
    Route("POST", r"/api/family/([A-Za-z0-9_-]{16,})/ack", Handler.api_family_ack),
    Route("POST", r"/api/login", Handler.api_login),
    Route("POST", r"/api/logout", Handler.api_logout),
    Route("GET", r"/api/me", Handler.api_me, staff_only=True),
    Route("GET", r"/api/status", Handler.api_status, staff_only=True),
    Route("POST", r"/api/ack/([a-z0-9-]+)", Handler.api_staff_ack, staff_only=True),
    Route("GET", r"/api/weather-map", Handler.api_weather_map, staff_only=True),
    Route("GET", r"/api/history", Handler.api_history, staff_only=True),
    Route("GET", r"/api/history\.csv", Handler.api_history_csv, staff_only=True),
    Route("GET", r"/api/patients", Handler.api_list_patients, staff_only=True),
    Route("POST", r"/api/patients", Handler.api_add_patient, staff_only=True),
    Route("PUT", r"/api/patients/([a-z0-9-]+)", Handler.api_update_patient, staff_only=True),
    Route("POST", r"/api/patients/([a-z0-9-]+)/rotate-link", Handler.api_rotate_link, staff_only=True),
    Route("DELETE", r"/api/patients/([a-z0-9-]+)", Handler.api_delete_patient, staff_only=True),
]


def family_link(public_url: str, patient: Patient) -> str:
    return f"{public_url}/s/{patient.share_token}"


# ---- presenters ------------------------------------------------------------------

def unit_health(data: dict) -> UnitHealth:
    """Optional heartbeat extras; anything malformed is dropped, never rejected,
    so a firmware bug can't stop the power report that matters."""
    rssi, firmware, uptime = data.get("rssi"), data.get("fw"), data.get("uptime_s")
    return UnitHealth(
        rssi=rssi if _is_int(rssi) and -127 <= rssi <= 0 else None,
        firmware=firmware if isinstance(firmware, str) and FIRMWARE_PATTERN.match(firmware) else None,
        uptime_s=uptime if _is_int(uptime) and 0 <= uptime <= MAX_UPTIME_SECONDS else None,
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _minutes_between(start, end) -> int | None:
    return int((end - start).total_seconds() // 60) if start and end else None


def incident_view(incident: Outage) -> dict:
    """What staff see about an incident. Names and roles of the people alerted, never their numbers."""
    return {
        "id": incident.id,
        "kind": incident.kind.value,
        "device_id": incident.device_id,
        "patient_name": incident.patient_name,
        "equipment": incident.equipment,
        "started_at": incident.started_at.isoformat(),
        "ended_at": incident.ended_at.isoformat() if incident.ended_at else None,
        "duration_minutes": _minutes_between(incident.started_at, incident.ended_at),
        "acknowledged_by": incident.acknowledged_by,
        "acknowledged_at": incident.acknowledged_at.isoformat() if incident.acknowledged_at else None,
        "response_minutes": _minutes_between(incident.started_at, incident.acknowledged_at),
        "alerted": [{"name": a.name, "role": a.role, "at": a.at.isoformat()} for a in incident.alerted],
        "detail": incident.detail,
    }


CSV_COLUMNS = ["Started (UTC)", "Ended (UTC)", "Duration (min)", "Type", "Patient", "Unit", "Equipment",
               "People alerted", "First alert (UTC)", "Responded by", "Response time (min)"]


def incidents_csv(incidents: list[Outage]) -> str:
    """The inspector's evidence of emergency outreach: one row per incident."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for i in incidents:
        writer.writerow([_csv_safe(v) for v in (
            _utc_text(i.started_at),
            _utc_text(i.ended_at) if i.ended_at else "ongoing",
            _blank_if_none(_minutes_between(i.started_at, i.ended_at)),
            _incident_type(i),
            i.patient_name,
            i.device_id,
            i.equipment,
            "; ".join(f"{a.name} ({a.role}) {_utc_text(a.at)}" for a in i.alerted),
            _utc_text(i.alerted[0].at) if i.alerted else "",
            i.acknowledged_by or "",
            _blank_if_none(_minutes_between(i.started_at, i.acknowledged_at)),
        )])
    return out.getvalue()


def _incident_type(incident: Outage) -> str:
    if incident.kind is IncidentKind.POWER:
        return "Power outage"
    if incident.kind is IncidentKind.SILENT:
        return "Unit silent"
    return f"Storm notice: {incident.detail}"


def _blank_if_none(value: object) -> object:
    return "" if value is None else value


def _utc_text(moment) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _csv_safe(value: object) -> object:
    # Spreadsheets run cells starting with these as formulas; names come from staff input.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


# ---- composition root ---------------------------------------------------------

def build_backend() -> tuple[PatientStore, OutageStore, StaffAuthenticator, str]:
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SECRET_KEY")
    if url and key:
        client = SupabaseClient(url, key)
        return SupabasePatientStore(client), SupabaseOutageStore(client), SupabaseAuthenticator(client), "Supabase"
    password = os.environ.get("LIFELINE_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("Set SUPABASE_URL + SUPABASE_SECRET_KEY, or LIFELINE_ADMIN_PASSWORD for local mode.")
    patients_file = Path(os.environ.get("LIFELINE_PATIENTS", HERE / "patients.json"))
    outages_file = Path(os.environ.get("LIFELINE_OUTAGES", HERE / "outages.json"))
    return (JsonFilePatientStore(patients_file), JsonFileOutageStore(outages_file),
            SharedPasswordAuthenticator(password), f"local files {patients_file.name}, {outages_file.name}")


def load_open_incidents(outages: OutageStore) -> list[Outage]:
    """Open incidents from before a restart. Without them, the next heartbeat from a unit
    still in an outage would open a new one and alert the first contact all over again."""
    try:
        return outages.list_open()
    except SupabaseError as error:
        log.error("%s. Alerts still work, but history and restart recovery are off until "
                  "supabase/migrations/002_outages.sql has been run.", error)
        return []


def build_notifier() -> Notifier:
    kind = os.environ.get("LIFELINE_NOTIFIER", "console")
    if kind == "console":
        return ConsoleNotifier()
    if kind == "ntfy":
        try:
            return NtfyNotifier(os.environ.get("NTFY_TOPIC", ""), os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
                                os.environ.get("NTFY_TOKEN", ""))
        except ValueError as error:
            raise SystemExit(f"ntfy: {error}")
    if kind == "twilio":
        return TwilioNotifier(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"], os.environ["TWILIO_FROM"])
    raise SystemExit(f"LIFELINE_NOTIFIER must be 'console', 'ntfy' or 'twilio', got {kind!r}")


def load_dotenv(path: Path) -> None:
    """Minimal .env reader so secrets stay out of shell history. Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_storm_watch(open_incidents: list[Outage]) -> tuple[StormWatch | None, WeatherSource | None]:
    """LIFELINE_STORM_WATCH: on (default, real NWS data), off, or demo (pretend ice storm)."""
    mode = os.environ.get("LIFELINE_STORM_WATCH", "on")
    if mode == "off":
        return None, None
    storm = StormWatch(ZoneInfo(os.environ.get("LIFELINE_TIMEZONE", "America/Chicago")), open_notices=open_incidents)
    if mode == "demo":
        return storm, DemoWeather(started=utc_now())
    if mode != "on":
        raise SystemExit(f"LIFELINE_STORM_WATCH must be 'on', 'off' or 'demo', got {mode!r}")
    user_agent = os.environ.get("NWS_USER_AGENT", "Lifeline power-loss alerts (set NWS_USER_AGENT with a contact)")
    return storm, NwsWeather(user_agent)


def run_weather_checks(lifeline: Lifeline, stop: threading.Event, every_seconds: int) -> None:
    while True:
        try:
            lifeline.refresh_weather()
        except Exception:
            log.exception("storm watch weather check failed")
        if stop.wait(every_seconds):
            return


def run_ticker(lifeline: Lifeline, stop: threading.Event) -> None:
    while not stop.wait(1.0):
        try:
            lifeline.tick()
        except Exception:
            log.exception("escalation tick failed")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(HERE / ".env")
    port = int(os.environ.get("PORT", "8080"))
    public_url = os.environ.get("LIFELINE_PUBLIC_URL", f"http://localhost:{port}").rstrip("/")

    store, outages, authenticator, backend_name = build_backend()
    open_incidents = load_open_incidents(outages)
    # The Monitor is fully restored before the ticker starts or the port opens.
    monitor = Monitor(
        store.list_all(),
        escalate_after=timedelta(seconds=int(os.environ.get("ESCALATE_AFTER_SECONDS", "120"))),
        offline_after=timedelta(seconds=int(os.environ.get("OFFLINE_AFTER_SECONDS", "90"))),
        silent_alert_after=timedelta(seconds=int(os.environ.get("SILENT_ALERT_AFTER_SECONDS", "300"))),
        status_link=lambda patient: family_link(public_url, patient),
        open_incidents=open_incidents,
    )
    storm, weather = build_storm_watch(open_incidents)
    lifeline = Lifeline(monitor, store, build_notifier(), outages, storm, weather)

    Handler.lifeline = lifeline
    Handler.authenticator = authenticator
    Handler.sessions = Sessions()
    Handler.throttle = LoginThrottle()
    Handler.public_url = public_url
    Handler.twilio_auth_token = None
    if os.environ.get("LIFELINE_NOTIFIER") == "twilio":
        if not public_url.startswith("https://"):
            raise SystemExit("Twilio mode needs LIFELINE_PUBLIC_URL set to your public https URL (e.g. ngrok)")
        Handler.twilio_auth_token = os.environ["TWILIO_AUTH_TOKEN"]

    stop = threading.Event()
    threading.Thread(target=run_ticker, args=(lifeline, stop), daemon=True, name="ticker").start()
    Handler.storm_mode = os.environ.get("LIFELINE_STORM_WATCH", "on")
    Handler.storm_check_seconds = int(os.environ.get("STORM_CHECK_SECONDS", "300"))
    if weather is not None:
        every = Handler.storm_check_seconds
        threading.Thread(target=run_weather_checks, args=(lifeline, stop, every), daemon=True, name="weather").start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("Lifeline on http://0.0.0.0:%d · storage: %s · %d patients · %d open incidents restored · storm watch %s",
             port, backend_name, len(monitor.patients()), len(open_incidents), os.environ.get("LIFELINE_STORM_WATCH", "on"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()

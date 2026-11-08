# Lifeline

Real-time power-loss alerts for people who depend on electrical medical equipment
(oxygen concentrators, home ventilators, powered beds).

A small battery-backed unit at the patient's outlet notices when wall power drops and
reports to a server. The server texts or calls the family, escalates through the
patient's call list until someone responds, gives the family a live status page, and
gives the home health agency a platform to watch every patient and manage the roster.

```
wall ──5 V USB adapter──▶ ESP32 (on its own battery)
                            │ wall power lost / restored, heartbeat every 30 s
                            ▼ WiFi (production: LTE-M)
                       server/app.py  (Raspberry Pi or laptop)
                            ├─ escalation: contact 1 → no reply in N min → contact 2 → ...
                            ├─ SMS + voice calls via Twilio (printed to the console without credentials)
                            ├─ family status page   /s/<private link>      (no login, phone-sized)
                            ├─ agency platform      /                      (staff sign-in)
                            └─ storage + staff accounts: Supabase (or a local JSON file)
```

## Who sees what

| Audience | Where | How they get in | What they can do |
|---|---|---|---|
| Power unit | `POST /api/report` | its own unit token | report wall power on/off |
| Family | `/s/<link>` from the alert text | private per-patient link | see that one patient's power, backup left, who was alerted; tap "I'm responding" |
| Agency staff | `/` | email + password, invited by an admin | by role: **admin** (everything, including Staff), **nurse** (board, respond, patients and call lists), **viewer** (board and history, no phone numbers) |
| County emergency management | `/county` | email + password, role **county** | read-only list of homes without power and their backup left; no phone numbers, tokens or family links |
| Anyone | `/` signed out | none | the public landing page |

The family page never shows other patients, unit tokens, staff emails or phone numbers.
A leaked family link can be replaced from the platform ("Make a new family link").

## Quick start (local mode, no Supabase, no Twilio)

```bash
cd server
python3 -m unittest test_domain             # 117 tests, stdlib only
cp patients.example.json patients.json      # 3 synthetic patients
LIFELINE_ADMIN_PASSWORD=pick-something-long ESCALATE_AFTER_SECONDS=20 python3 app.py
```

Open http://localhost:8080 and sign in with any email plus that password. Then pretend to be a unit:

```bash
R=localhost:8080/api/report; J='Content-Type: application/json'
curl -X POST $R -H "$J" -H 'X-Device-Token: change-me-unit-01' -d '{"device_id":"unit-01","wall_power":true,"battery_percent":95}'
curl -X POST $R -H "$J" -H 'X-Device-Token: change-me-unit-01' -d '{"device_id":"unit-01","wall_power":false,"battery_percent":94}'
# console: text to contact 1 with the family link, then contact 2 after 20 s ...
curl -X POST localhost:8080/api/sms-inbound --data-urlencode 'From=+15555550101' --data-urlencode 'Body=OK'
curl -X POST $R -H "$J" -H 'X-Device-Token: change-me-unit-01' -d '{"device_id":"unit-01","wall_power":true,"battery_percent":94}'
```

Python 3.10+, no packages to install. Settings can live in `server/.env` (see `.env.example`).

## Supabase setup (recommended)

1. Create a project at supabase.com.
2. **SQL Editor → New query**: paste `supabase/migrations/001_patients.sql` and run it. It creates the
   `patients` table with database-level checks and turns on row level security with **no** policies,
   so the public (anon/publishable) key can't read or write it. Only the server's secret key can.
   Then run `supabase/migrations/002_outages.sql` the same way. It creates the `outages` table
   (power outages and silent units) behind the same lockdown. The server saves every alert,
   escalation and acknowledgment there, so a restart resumes escalation instead of re-alerting,
   and the History page and CSV report read from it.
   Then run `003_storm_watch.sql` (storm notices), `004_plans_and_sensors.sql` (emergency plans),
   `005_staff_roles.sql` (staff roles; without it only the bootstrap admin can sign in) and
   `006_backup_low_warning.sql` (the "backup almost out" stage) the same way, in order.
3. **Authentication → Sign In / Providers**: turn off "Allow new users to sign up", so only
   accounts you create can sign in.
4. Set `LIFELINE_BOOTSTRAP_ADMIN=<your email>` in `server/.env` and create that one user under
   **Authentication → Users → Add user**. Everyone else is added from the platform's **Staff** page,
   which creates the account with a temporary password and a role. A Supabase user without a
   Lifeline role gets no access.
5. **Project Settings → API Keys**: copy the project URL and the **secret** key (`sb_secret_...`,
   or the legacy `service_role` key) into `server/.env`:

   ```
   SUPABASE_URL=https://<project-ref>.supabase.co
   SUPABASE_SECRET_KEY=sb_secret_...
   ```

6. `python3 app.py`. The log should say `storage: Supabase`. Add patients from the **Patients** view.

The secret key stays on the server. The browser only ever gets an opaque session cookie
(HttpOnly, SameSite=Strict, Secure on https) issued after Supabase Auth confirms the password.

## Hardware

| Part | Notes |
|---|---|
| ESP32 dev board | any; firmware tested to compile for `esp32:esp32:esp32` |
| Battery for the ESP32 | power bank with **pass-through charging**, or 18650 + TP4056 with load sharing + 5 V boost converter |
| Wall-sense adapter | any cheap 5 V USB phone charger, on the *same power strip as the "medical device"* |
| Resistors | 10 kΩ + 20 kΩ (sense divider); 100 kΩ + 100 kΩ (optional battery divider) |
| Lamp | stands in for the oxygen concentrator on stage |

Full schematic: [`hardware/schematic.svg`](hardware/schematic.svg). Wiring (no mains wiring; everything is low voltage):

```
sense adapter 5V ──10k──┬── GPIO34          (≈3.3 V when the wall has power, 0 V when it doesn't)
                        20k
sense adapter GND ──────┴── ESP32 GND       (grounds MUST be shared)

battery + ──100k──┬── GPIO35  (optional)
                 100k
battery − ────────┴── GND
```

Use a USB breakout board or a cut USB cable to get 5V/GND from the sense adapter.

Gotcha: some power banks drop output for a moment when their input disappears, which
resets the ESP32. Test yours by unplugging it. If it resets, use an 18650 + TP4056 module
with load sharing, or put a 1000 µF capacitor across the ESP32's 5V/GND.

## Firmware

```bash
cd firmware/lifeline
cp config.example.h config.h     # WiFi, server URL, and the unit ID + token shown in the platform
arduino-cli compile --fqbn esp32:esp32:esp32 .
arduino-cli upload  --fqbn esp32:esp32:esp32 -p /dev/cu.usbserial-XXXX .
arduino-cli monitor -p /dev/cu.usbserial-XXXX -c baudrate=115200
```

Firmware 0.4.0 talks HTTPS to a hosted server (certificate checked against the ESP32 core's
root bundle; `http://` still works for a laptop server), joins the stronger of two saved Wi-Fi
networks (e.g. home Wi-Fi plus a phone hotspot; WPA2-Enterprise such as eduroam via
`WIFI_EAP_USERNAME`), debounces 2 s of flicker, reports immediately on every change, retries
with backoff (3 s doubling to 30 s) until the server confirms, and sends a heartbeat every 30 s.
Hold the button at power-on to open the setup portal and change the network or server address
without reflashing (saved settings override `config.h`). The server marks a unit **offline**
after 90 s of silence, which also catches a unit whose battery died.

## Storm Watch (warnings before the power goes out)

Every 5 minutes the server asks the National Weather Service (api.weather.gov: free, no key)
about each patient's home: official warnings that threaten power lines (tornado, severe
thunderstorm, high wind, ice storm, winter storm, hurricane, extreme cold or heat) and the
next 24 hours of forecast wind gusts and ice. It also notices when 2+ other Lifeline homes
within 3 miles lost power in the last 30 minutes.

- High risk (warnings, gusts of 58 mph or more, a quarter inch of ice or more, outages
  spreading nearby): the patient's first contact gets one notice per hazard, the board shows
  an amber Storm watch banner, and the notice is kept in the History report.
- Elevated risk (watches, advisories, gusts of 40 mph or more): shown on the board and the
  family page only.

On the live board the map shows NOAA's live weather radar, the outlines of active warnings
and watches near the homes (in National Weather Service colours), and each home's current
temperature; hover a home for wind, gusts and chance of thunder. Both layers can be switched
off in the map's corner menu.

Thresholds are heuristics from the outage research in `docs/research.md`, not a fitted model.
Settings: `LIFELINE_STORM_WATCH=on|off|demo` (`demo` pretends an ice storm covers every home,
for showing it on a clear day), `NWS_USER_AGENT` (NWS asks for an app name plus a contact),
`LIFELINE_TIMEZONE` (default `America/Chicago`), `STORM_CHECK_SECONDS` (default 300).

## Free phone push (ntfy)

No account, no cost: alerts arrive as urgent push notifications with the full text and family link.

1. Install the **ntfy** app (iOS/Android) and subscribe to your topic.
2. In `server/.env`: `LIFELINE_NOTIFIER=ntfy` and `NTFY_TOPIC=lifeline-<long random string>`.
   Anyone who knows the topic can read it, so the name is the secret (16+ characters enforced).

Every alert goes to that one topic, titled with the contact it is for. Use Twilio for
per-person SMS and calls in production.

## Real SMS and calls (Twilio)

1. Twilio trial account. Verify the phones you will demo with (trials can only reach verified numbers).
2. Expose the server so Twilio can deliver SMS replies: `ngrok http 8080`.
3. In the Twilio console, set the number's "A message comes in" webhook to `https://<ngrok>/api/sms-inbound`.
4. Run:

```bash
LIFELINE_NOTIFIER=twilio \
TWILIO_ACCOUNT_SID=AC... TWILIO_AUTH_TOKEN=... TWILIO_FROM=+1... \
LIFELINE_PUBLIC_URL=https://<ngrok-subdomain>.ngrok-free.app \
python3 app.py
```

Replies are checked against Twilio's signature, so `LIFELINE_PUBLIC_URL` must be the exact
public base URL.

**Free option: WhatsApp sandbox.** Twilio's free tier no longer includes a phone number, but
its WhatsApp sandbox needs none. In the console open Messaging > Try it out > Send a WhatsApp
message, send the shown `join <code>` from each demo phone, and set
`TWILIO_FROM=whatsapp:+14155238886` (the sandbox sender shown there). Every alert, calls
included, then arrives as a WhatsApp message. For replies, set the sandbox's "When a message
comes in" URL to `https://<public-url>/api/sms-inbound`. Sandbox sessions expire after a few
days of silence; send the join code again before the demo.
Each alert says "Reply OK 4821" with a code unique to that outage. A bare "OK" works when
only one open alert went to that phone; if the person is on several call lists with open
outages (for example the agency's on-call nurse), Lifeline asks for the code instead of
guessing. Without Twilio configured, simulated replies are accepted only from the server
machine itself. If VOXO's sandbox offers SMS/voice, add a `VoxoNotifier` with the same
`send(notification)` method in `notifiers.py` and select it in `build_notifier()`.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `SUPABASE_URL`, `SUPABASE_SECRET_KEY` | (empty) | use Supabase for patients and staff sign-in |
| `LIFELINE_ADMIN_PASSWORD` | (empty) | local mode only: shared staff password |
| `LIFELINE_PATIENTS` | `patients.json` | local mode only: roster file |
| `LIFELINE_PUBLIC_URL` | `http://localhost:PORT` | base of family links in texts; must be https for Twilio |
| `PORT` | 8080 | HTTP port |
| `ESCALATE_AFTER_SECONDS` | 120 | wait before alerting the next contact (use ~20 on stage) |
| `OFFLINE_AFTER_SECONDS` | 90 | silence before a unit shows as offline |
| `LIFELINE_NOTIFIER` | console | `console`, `ntfy` or `twilio` |
| `LIFELINE_BOOTSTRAP_ADMIN` | (empty) | email that is always an admin (Supabase mode) |
| `LIFELINE_SESSION_SECRET`, `LIFELINE_RESPOND_SECRET` | (random per start) | sign session cookies and one-tap "I'm responding" links; set them so restarts keep people signed in |
| `LIFELINE_COUNTY_CONTACT` | (empty) | `Name\|+1601...\|sms`; county emergency management for the 15-minute emergency alert and welfare-check lists |
| `LIFELINE_STORM_WATCH` | on | `on`, `off` or `demo` |
| `LIFELINE_CLIENT_IP_HEADER` | (empty) | behind a proxy, e.g. `Fly-Client-IP`, so sign-in throttling counts callers, not the proxy |
| `LIFELINE_DEMO_ASSUME_POWER` | (empty) | `1`: units that never reported show as powered (demo with one real unit) |

Health check: `GET /healthz`. Hosting: see [`docs/ops/deploy.md`](docs/ops/deploy.md); smoke test with
`python3 server/smoke_test.py <base-url>`.

## Code map

| File | Job |
|---|---|
| `server/domain.py` | patients, contacts and outage rules (no I/O, fully unit-tested) |
| `server/service.py` | use cases for devices, families and staff; storage port |
| `server/records.py` | the one stored shape of a patient; parses staff form input |
| `server/supabase_backend.py` / `server/local.py` | storage + sign-in adapters |
| `server/staff.py` | sessions and sign-in throttling |
| `server/notifiers.py` | console and Twilio delivery, webhook signature check |
| `server/app.py` | HTTP routes, security headers, composition root |
| `server/access.py` | roles, permissions and the staff directory |
| `server/storm.py`, `server/weather.py` | Storm Watch rules and the National Weather Service adapter |
| `server/static/` | landing page, platform, county page, family page, sign-in (vanilla JS modules, `tokens.css` design tokens) |

## Stage demo

The stage plan, timed script and Q&A live in `docs/pitch/` (demo-day plan in `docs/pitch/demo-day/`,
runbook in `docs/ops/demo-day.md`). Keep a recording of a good run as a backup; the recorder in
`docs/pitch/video/record/` rebuilds the demo video against a synthetic local server.

## Not built yet (and when to add it)

- **LTE-M**: needed for real outages because home Wi-Fi dies too. Swap the Wi-Fi transport for a SIM7080G/Walter board.
- **Real patient data**: needs a HIPAA-eligible plan and a BAA with each vendor (Supabase, Twilio) first. Use synthetic data until then.
- **Machine-running check**: the firmware and server support a clip-on current sensor (SCT-013 on GPIO32) that shows whether the concentrator itself is drawing power; the clamp is not fitted on the demo unit yet.
- **Staff display names**: claimed rows show the email's name part ("nurse.reed"), not a full name.

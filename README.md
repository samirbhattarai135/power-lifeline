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
| Agency staff | `/` | email + password | live board and map, "We're responding", add/edit/remove patients, unit tokens, rotate family links |

The family page never shows other patients, unit tokens, staff emails or phone numbers.
A leaked family link can be replaced from the platform ("Make a new family link").

## Quick start (local mode, no Supabase, no Twilio)

```bash
cd server
python3 test_domain.py                      # 20 tests, stdlib only
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
3. **Authentication → Sign In / Providers**: turn off "Allow new users to sign up", so only
   accounts you create can sign in.
4. **Authentication → Users → Add user**: create one email + password per staff member.
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
| Battery for the ESP32 | power bank with **pass-through charging**, or 18650 + TP4056 with load sharing |
| Wall-sense adapter | any cheap 5 V USB phone charger, on the *same power strip as the "medical device"* |
| Resistors | 10 kΩ + 20 kΩ (sense divider); 100 kΩ + 100 kΩ (optional battery divider) |
| Lamp | stands in for the oxygen concentrator on stage |

Wiring (no mains wiring; everything is low voltage):

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

The unit debounces 2 s of flicker, reports immediately on every change, retries every
3 s until the server confirms, and sends a heartbeat every 30 s. The server marks a
unit **offline** after 90 s of silence, which also catches a unit whose battery died.

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
public base URL. If VOXO's sandbox offers SMS/voice, add a `VoxoNotifier` with the same
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
| `LIFELINE_NOTIFIER` | console | `console` or `twilio` |

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
| `server/static/` | platform, family page, sign-in (vanilla JS modules, `tokens.css` design tokens) |

## Stage demo (60 s)

1. Live board on the projector: "All 4 homes have power". Lamp on, plugged into the power strip with the sense adapter.
2. Switch the strip off. Lamp dies, the headline turns red ("1 home without power"), and a judge's phone gets the text with the family link.
3. The judge opens the link: "Power is out", backup minutes counting down.
4. Nobody responds, so after 20 s the "nurse" phone rings with the spoken alert.
5. The judge taps "I'm responding". The board turns amber and escalation stops.
6. Switch the strip back on. "Power is back on" texts arrive and everything goes green.

Keep a screen recording of a good run as a backup.

## Not built yet (and when to add it)

- **LTE-M**: needed for real outages because home WiFi dies too. Swap the WiFi transport for a SIM7080G/Walter board.
- **Outage history**: live outage state is in memory, so a restart forgets an outage in progress (the next unit report re-opens it). Add an `outages` table when agencies want reports.
- **Staff sessions** are in memory; staff sign in again after a restart.
- **Roles**: every staff account can edit every patient. Add roles if an agency needs read-only staff.
- **Real patient data**: needs a HIPAA-eligible plan and a BAA with each vendor (Supabase, Twilio) first. Use synthetic data until then.
- **Device current sensing**: shows whether the concentrator itself is drawing power. Skipped because it means touching mains wiring.

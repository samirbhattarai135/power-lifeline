// Copy to config.h (git-ignored) and fill in.
#pragma once

// Wi-Fi networks, tried in this order until one connects (2.4 GHz only). After a drop the unit
// starts again from the network it was on. WIFI_NETWORK(name, password, enterprise username):
// the username is only for enterprise Wi-Fi such as eduroam (PEAP); use "" for home Wi-Fi or a
// phone hotspot. Entries still ending in _HERE are skipped. Networks saved on the unit's setup
// screen (hold the button at power-on) are tried before these.
#define WIFI_NETWORKS \
  WIFI_NETWORK("eduroam", "EDUROAM_PASSWORD_HERE", "you@school.edu") \
  WIFI_NETWORK("HOME_WIFI_NAME_HERE", "HOME_WIFI_PASSWORD_HERE", "") \
  WIFI_NETWORK("PHONE_HOTSPOT_NAME_HERE", "PHONE_HOTSPOT_PASSWORD_HERE", "")
// Older single-network style still works: WIFI_SSID / WIFI_PASSWORD (+ WIFI_EAP_USERNAME) and
// WIFI_SSID_2 / WIFI_PASSWORD_2 for a backup.

// Where server/app.py runs. Hosted (HTTPS, certificate checked against the built-in root CA list):
#define SERVER_URL     "https://lifeline-pinebelt.azurewebsites.net/api/report"
// Laptop on the same Wi-Fi/hotspot instead (plain HTTP, local fallback only):
// #define SERVER_URL  "http://192.168.1.50:8080/api/report"
// Use https:// for the hosted server: it redirects http:// and the unit does not follow redirects.
// A server address saved from the setup screen overrides this one.

// Must match device_id / device_token in server/patients.json.
#define DEVICE_ID      "unit-01"
#define DEVICE_TOKEN   "change-me-unit-01"

// GPIO reading the 5 V wall-sense adapter through a 10k/20k divider (input-only pins 34-39 are fine).
#define WALL_SENSE_PIN 34

// Optional: LiPo/18650 voltage through a 100k/100k divider on GPIO35. -1 = not wired
// (demo build runs from laptop USB, so there is no battery to read).
#define BATTERY_ADC_PIN -1
// Calibration knob: multiply measured pin voltage by this to get battery voltage.
// 2.0 for an equal divider; tweak against a multimeter reading.
#define BATTERY_DIVIDER_RATIO 2.0f

#define STATUS_LED_PIN 2

// Optional 128x64 I2C OLED status screen (SSD1306, 4 pins: VCC to 3V3, GND, SCL, SDA).
// ESP32 default I2C pins: SDA 21, SCL 22. -1 = not fitted. Address is usually 0x3C (some 0x3D).
#define OLED_SDA_PIN 21
#define OLED_SCL_PIN 22
// #define OLED_ADDRESS 0x3C

// ---- optional parts: set the GPIO you wired, or -1 if not fitted ----------------------------

// Active buzzer (+ to this pin, - to GND): beeps when the power goes out until someone presses the button.
#define BUZZER_PIN -1
// Push button between this pin and GND (internal pull-up). Short press: "I'm here".
// Hold 3 s: call for help. Hold while powering on: open the phone setup screen.
#define BUTTON_PIN -1
// SCT-013-030 current clamp on the equipment's cord, through a 1.65 V bias (two 10k resistors
// from 3V3 to GND, 10 uF to GND at the midpoint). Must be an ADC1 pin: 32, 33, 36 or 39.
#define CURRENT_SENSOR_PIN -1
#define CURRENT_AMPS_PER_VOLT 30.0f  // 30 for SCT-013-030, 5 for SCT-013-005
#define CURRENT_CALIBRATION 1.0f     // tweak against a clamp meter
// Room temperature: DHT11 (11), DHT22 (22) or DS18B20 (18) data pin.
#define TEMP_SENSOR_PIN -1
#define TEMP_SENSOR_TYPE 11

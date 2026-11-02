// Copy to config.h (git-ignored) and fill in.
#pragma once

#define WIFI_SSID      "your-wifi-or-phone-hotspot"
#define WIFI_PASSWORD  "your-password"

// Raspberry Pi / laptop running server/app.py, reachable from the ESP32.
#define SERVER_URL     "http://192.168.1.50:8080/api/report"

// Must match device_id / device_token in server/patients.json.
#define DEVICE_ID      "unit-01"
#define DEVICE_TOKEN   "change-me-unit-01"

// GPIO reading the 5 V wall-sense adapter through a 10k/20k divider (input-only pins 34-39 are fine).
#define WALL_SENSE_PIN 34

// Optional: LiPo/18650 voltage through a 100k/100k divider. Set to -1 if not wired.
#define BATTERY_ADC_PIN 35
// Calibration knob: multiply measured pin voltage by this to get battery voltage.
// 2.0 for an equal divider; tweak against a multimeter reading.
#define BATTERY_DIVIDER_RATIO 2.0f

#define STATUS_LED_PIN 2

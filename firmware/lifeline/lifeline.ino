// Lifeline unit: watches wall power and reports to the server.
// Runs from its own battery so it keeps reporting after the outlet goes dead.

#include <WiFi.h>
#include <HTTPClient.h>
#include "config.h"

const uint32_t DEBOUNCE_MS       = 2000;   // ignore brownout flicker shorter than this
const uint32_t HEARTBEAT_MS      = 30000;  // server marks a unit offline after ~90 s of silence
const uint32_t RETRY_MS          = 3000;   // retry a failed state-change report this often
const uint32_t WIFI_RETRY_MS     = 30000;  // phone hotspots can take >10 s to finish a handshake

bool stableWallPower   = true;   // debounced reading
bool lastRawWallPower  = true;
uint32_t rawChangedAt  = 0;
uint32_t lastAttemptAt = 0;
uint32_t lastWifiTryAt = 0;
bool changePending     = true;   // server has not yet confirmed the current state
bool sendNow           = true;   // report on boot and right after every state change

bool readWallPower() {
  return digitalRead(WALL_SENSE_PIN) == HIGH;
}

int readBatteryPercent() {
  if (BATTERY_ADC_PIN < 0) return -1;
  float volts = analogReadMilliVolts(BATTERY_ADC_PIN) / 1000.0f * BATTERY_DIVIDER_RATIO;
  // ponytail: linear 3.3-4.2 V LiPo curve; use a lookup table if the % matters for the pitch.
  int percent = (int)((volts - 3.3f) / (4.2f - 3.3f) * 100.0f);
  return constrain(percent, 0, 100);
}

void ensureWifi() {
  static bool announced = false;
  if (WiFi.status() == WL_CONNECTED) {
    if (!announced) Serial.printf("WiFi: connected, IP %s\n", WiFi.localIP().toString().c_str());
    announced = true;
    return;
  }
  announced = false;
  uint32_t now = millis();
  if (lastWifiTryAt != 0 && now - lastWifiTryAt < WIFI_RETRY_MS) return;
  lastWifiTryAt = now;
  // Status 1 = network not found (often 5 GHz only), 4 = connect failed (often wrong password).
  Serial.printf("WiFi: connecting... (last status %d)\n", WiFi.status());
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

bool sendReport(bool wallPower) {
  if (WiFi.status() != WL_CONNECTED) return false;

  char body[160];
  int battery = readBatteryPercent();
  if (battery >= 0) {
    snprintf(body, sizeof(body), "{\"device_id\":\"%s\",\"wall_power\":%s,\"battery_percent\":%d}",
             DEVICE_ID, wallPower ? "true" : "false", battery);
  } else {
    snprintf(body, sizeof(body), "{\"device_id\":\"%s\",\"wall_power\":%s}",
             DEVICE_ID, wallPower ? "true" : "false");
  }

  HTTPClient http;
  http.setTimeout(5000);
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  int status = http.POST((uint8_t *)body, strlen(body));
  http.end();

  Serial.printf("report wall_power=%d battery=%d -> HTTP %d\n", wallPower, battery, status);
  return status == 200;
}

void setup() {
  Serial.begin(115200);
  pinMode(WALL_SENSE_PIN, INPUT);  // divider's lower resistor pulls it low when the adapter is dead
  pinMode(STATUS_LED_PIN, OUTPUT);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  // 201 = network not found (often 5 GHz only), 15 or 202 = usually a wrong password.
  WiFi.onEvent([](WiFiEvent_t, WiFiEventInfo_t info) {
    Serial.printf("WiFi: dropped, reason %d\n", info.wifi_sta_disconnected.reason);
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);

  stableWallPower = lastRawWallPower = readWallPower();
  rawChangedAt = millis();
  ensureWifi();
}

void loop() {
  uint32_t now = millis();
  ensureWifi();

  bool raw = readWallPower();
  if (raw != lastRawWallPower) {
    lastRawWallPower = raw;
    rawChangedAt = now;
  }
  if (raw != stableWallPower && now - rawChangedAt >= DEBOUNCE_MS) {
    stableWallPower = raw;
    changePending = true;
    sendNow = true;
    Serial.printf("wall power %s\n", stableWallPower ? "RESTORED" : "LOST");
  }

  digitalWrite(STATUS_LED_PIN, stableWallPower ? LOW : HIGH);

  bool due = sendNow
          || (changePending && now - lastAttemptAt >= RETRY_MS)
          || now - lastAttemptAt >= HEARTBEAT_MS;
  if (due && WiFi.status() == WL_CONNECTED) {
    sendNow = false;
    lastAttemptAt = now;
    if (sendReport(stableWallPower)) changePending = false;
  }

  delay(50);
}

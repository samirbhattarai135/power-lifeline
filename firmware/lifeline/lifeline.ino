// Lifeline unit: watches wall power and reports to the server.
// Runs from its own battery so it keeps reporting after the outlet goes dead.
//
// Optional parts, each switched off with -1 in config.h:
//   buzzer       local alarm when the power goes out
//   button       short press: "I'm here" (acknowledge); hold 3 s: call for help
//   current clamp SCT-013 on the equipment's cord: is the machine actually running?
//   temperature  DHT11 / DHT22 / DS18B20: is the room safe without heat or AC?
// Wi-Fi, server and unit ID can be set from a phone (setup screen) instead of config.h.
// Tries each known Wi-Fi network in turn until one connects (e.g. eduroam, home Wi-Fi, phone hotspot) and
// talks HTTPS to a hosted server, checking its certificate against the built-in root CA list.

#include <WiFi.h>
#include <vector>
#include <HTTPClient.h>
#include <NetworkClientSecure.h>
#if __has_include("esp_eap_client.h")
#include "esp_eap_client.h"
#endif
#include <Preferences.h>
#include <WebServer.h>
#include <DNSServer.h>
#include "config.h"
#if OLED_SDA_PIN >= 0
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#endif

// ---- defaults for settings older config.h files don't have ------------------------------
#ifndef WIFI_SSID
#define WIFI_SSID ""          // newer config.h files list every network in WIFI_NETWORKS instead
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif
#ifndef WIFI_EAP_USERNAME
#define WIFI_EAP_USERNAME ""  // set only for enterprise Wi-Fi such as eduroam
#endif
#ifndef WIFI_SSID_2
#define WIFI_SSID_2 ""        // backup network, e.g. the phone hotspot; "" = none
#endif
#ifndef WIFI_PASSWORD_2
#define WIFI_PASSWORD_2 ""
#endif
#ifndef OLED_SDA_PIN
#define OLED_SDA_PIN -1        // 128x64 I2C OLED status screen (SSD1306); -1 = not fitted
#endif
#ifndef OLED_SCL_PIN
#define OLED_SCL_PIN -1
#endif
#ifndef OLED_ADDRESS
#define OLED_ADDRESS 0x3C      // most 4-pin modules; some are 0x3D
#endif
#ifndef BUZZER_PIN
#define BUZZER_PIN -1
#endif
#ifndef BUTTON_PIN
#define BUTTON_PIN -1
#endif
#ifndef CURRENT_SENSOR_PIN
#define CURRENT_SENSOR_PIN -1
#endif
#ifndef CURRENT_AMPS_PER_VOLT
#define CURRENT_AMPS_PER_VOLT 30.0f   // SCT-013-030: 30 A gives 1 V
#endif
#ifndef CURRENT_CALIBRATION
#define CURRENT_CALIBRATION 1.0f      // tweak against a clamp meter
#endif
#ifndef TEMP_SENSOR_PIN
#define TEMP_SENSOR_PIN -1
#endif
#ifndef TEMP_SENSOR_TYPE
#define TEMP_SENSOR_TYPE 11           // 11 = DHT11, 22 = DHT22, 18 = DS18B20
#endif

#if TEMP_SENSOR_PIN >= 0
#if TEMP_SENSOR_TYPE == 18
#include <OneWire.h>
#include <DallasTemperature.h>
OneWire oneWire(TEMP_SENSOR_PIN);
DallasTemperature thermometer(&oneWire);
#else
#include <DHT.h>
DHT thermometer(TEMP_SENSOR_PIN, TEMP_SENSOR_TYPE);
#endif
#endif

const char *FIRMWARE_VERSION = "0.6.0";

const uint32_t DEBOUNCE_MS       = 2000;   // ignore brownout flicker shorter than this
const uint32_t HEARTBEAT_MS      = 30000;  // server marks a unit offline after ~90 s of silence
const uint32_t RETRY_MS          = 3000;   // first retry after a failed report; doubles per failure...
const uint32_t RETRY_MAX_MS      = 30000;  // ...up to the heartbeat interval, which would fire anyway
const uint32_t HTTP_TIMEOUT_MS   = 8000;   // a TLS handshake on a slow hotspot takes 1-3 s
const uint32_t WIFI_ATTEMPT_MS   = 15000;  // per network before moving on; hotspots can take >10 s
const uint32_t WIFI_EAP_ATTEMPT_MS = 25000; // enterprise (eduroam) sign-in is slower
const uint32_t HELP_HOLD_MS      = 3000;   // hold the button this long to call for help
const uint32_t ACK_MAX_MS        = 1500;   // a press shorter than this is "I'm here"
const uint32_t SETUP_HOLD_MS     = 3000;   // hold the button at power-on to open the setup screen
const uint32_t ALARM_EVERY_MS    = 15000;  // buzzer repeat during an outage, until someone presses the button
const uint32_t CURRENT_WINDOW_MS = 100;    // six mains cycles at 60 Hz

// ---- settings: phone setup screen first, config.h as the fallback ------------------------

struct Settings {
  String ssid, password, ssid2, password2, serverUrl, deviceId, deviceToken;
};
Settings settings;

// Known Wi-Fi networks in the order they are tried (built in buildWifiNetworks).
struct WifiNetwork {
  String ssid, password, eapUser;  // eapUser set = enterprise (PEAP) sign-in, e.g. eduroam
};
std::vector<WifiNetwork> wifiNetworks;
Preferences prefs;

void loadSettings() {
  prefs.begin("lifeline", true);
  settings.ssid = prefs.getString("ssid", WIFI_SSID);
  settings.password = prefs.getString("password", WIFI_PASSWORD);
  settings.ssid2 = prefs.getString("ssid2", WIFI_SSID_2);
  settings.password2 = prefs.getString("password2", WIFI_PASSWORD_2);
  settings.serverUrl = prefs.getString("server", SERVER_URL);
  settings.deviceId = prefs.getString("device", DEVICE_ID);
  settings.deviceToken = prefs.getString("token", DEVICE_TOKEN);
  prefs.end();
}

bool hasRealSettings() {
  return !wifiNetworks.empty();
}

// ---- state ------------------------------------------------------------------------------------

bool stableWallPower   = true;   // debounced reading
bool lastRawWallPower  = true;
uint32_t rawChangedAt  = 0;
uint32_t lastAttemptAt = 0;
uint32_t nextAttemptInMs = 0;    // after lastAttemptAt; grows while the server can't be reached
uint8_t failedAttempts = 0;
bool changePending     = true;   // server has not yet confirmed the current state
bool sendNow           = true;   // report on boot and right after every state change
const char *pendingEvent = nullptr;  // "ack" or "help", sent with the next report until confirmed

int lastHttpStatus = 0;          // last answer from the server, for the status screen
uint32_t lastReportOkAt = 0;     // 0 = never confirmed
uint32_t powerLostAt = 0;        // when the current outage was detected
bool alarmSilenced = false;      // someone pressed the button during this outage
uint32_t lastAlarmAt = 0;
uint32_t buttonDownAt = 0;
bool helpSentThisPress = false;

// ---- buzzer -----------------------------------------------------------------------------------

void beep(int times, uint32_t lengthMs) {
  if (BUZZER_PIN < 0) return;
  for (int n = 0; n < times; n++) {
    digitalWrite(BUZZER_PIN, HIGH);
    delay(lengthMs);
    digitalWrite(BUZZER_PIN, LOW);
    if (n + 1 < times) delay(lengthMs);
  }
}

void updateAlarm(uint32_t now) {
  if (stableWallPower || alarmSilenced) return;
  if (now - lastAlarmAt >= ALARM_EVERY_MS) {
    lastAlarmAt = now;
    beep(1, 250);
  }
}

// ---- button -----------------------------------------------------------------------------------

void queueEvent(const char *event) {
  // Help outranks "I'm here": never let a later short press overwrite a help call.
  if (pendingEvent != nullptr && strcmp(pendingEvent, "help") == 0) return;
  pendingEvent = event;
  sendNow = true;
}

void updateButton(uint32_t now) {
  if (BUTTON_PIN < 0) return;
  bool pressed = digitalRead(BUTTON_PIN) == LOW;  // wired to ground, internal pull-up
  if (pressed && buttonDownAt == 0) {
    buttonDownAt = now;
    helpSentThisPress = false;
  }
  if (pressed && !helpSentThisPress && now - buttonDownAt >= HELP_HOLD_MS) {
    helpSentThisPress = true;
    queueEvent("help");
    Serial.println("button: HELP");
    beep(2, 150);
  }
  if (!pressed && buttonDownAt != 0) {
    uint32_t held = now - buttonDownAt;
    buttonDownAt = 0;
    if (!helpSentThisPress && held >= 50 && held <= ACK_MAX_MS) {
      queueEvent("ack");
      alarmSilenced = true;
      Serial.println("button: I'M HERE");
      beep(1, 120);
    }
  }
}

// ---- sensors ----------------------------------------------------------------------------------

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

// RMS current through the clamp. The clamp's AC signal rides on a ~1.65 V bias (two equal
// resistors from 3V3 to GND plus a capacitor), so subtract the mean, then take the RMS.
float readLoadAmps() {
  if (CURRENT_SENSOR_PIN < 0) return -1;
  double sum = 0, sumSquares = 0;
  uint32_t samples = 0, start = millis();
  while (millis() - start < CURRENT_WINDOW_MS) {
    double mv = analogReadMilliVolts(CURRENT_SENSOR_PIN);
    sum += mv;
    sumSquares += mv * mv;
    samples++;
  }
  double mean = sum / samples;
  double rmsMv = sqrt(max(0.0, sumSquares / samples - mean * mean));
  return (float)(rmsMv / 1000.0 * CURRENT_AMPS_PER_VOLT * CURRENT_CALIBRATION);
}

float readRoomTempF() {
#if TEMP_SENSOR_PIN >= 0
#if TEMP_SENSOR_TYPE == 18
  thermometer.requestTemperatures();
  float c = thermometer.getTempCByIndex(0);
  return c == DEVICE_DISCONNECTED_C ? NAN : c * 9.0f / 5.0f + 32.0f;
#else
  return thermometer.readTemperature(true);  // NAN if the sensor didn't answer
#endif
#else
  return NAN;
#endif
}

// ---- reporting --------------------------------------------------------------------------------

// Every known network, in the order they are tried: the ones saved on the setup screen first, then
// config.h's WIFI_NETWORKS list (or its older WIFI_SSID / WIFI_SSID_2 pair). The unit tries each for a
// few seconds and moves to the next until one connects; after a drop it starts again from that one.
size_t wifiIndex = 0;
uint32_t wifiAttemptAt = 0;  // 0 = no attempt in progress

// The enterprise username config.h gives for this network name, if any.
String eapUserFor(const String &ssid) {
#ifdef WIFI_NETWORKS
#define WIFI_NETWORK(name, password, eapUser) if (ssid == name) return eapUser;
  WIFI_NETWORKS
#undef WIFI_NETWORK
#endif
  return ssid == WIFI_SSID ? String(WIFI_EAP_USERNAME) : String("");
}

void addWifiNetwork(const String &ssid, const String &password, const String &eapUser) {
  // Empty or still a config.example.h placeholder: skip, so it doesn't cost a connect attempt.
  if (ssid.length() == 0 || ssid == "your-wifi-or-phone-hotspot" || ssid.endsWith("_HERE") || password.endsWith("_HERE")) return;
  for (const WifiNetwork &known : wifiNetworks) if (known.ssid == ssid) return;
  wifiNetworks.push_back({ssid, password, eapUser});
}

void buildWifiNetworks() {
  wifiNetworks.clear();
  addWifiNetwork(settings.ssid, settings.password, eapUserFor(settings.ssid));
  addWifiNetwork(settings.ssid2, settings.password2, eapUserFor(settings.ssid2));
#ifdef WIFI_NETWORKS
#define WIFI_NETWORK(name, password, eapUser) addWifiNetwork(name, password, eapUser);
  WIFI_NETWORKS
#undef WIFI_NETWORK
#endif
}

void beginWifi(const WifiNetwork &network) {
  WiFi.disconnect();
  if (network.eapUser.length() > 0) {
    // PEAP sign-in with a username, no CA check (eduroam and most campuses).
    Serial.printf("WiFi: trying \"%s\" (%u of %u), signing in as %s\n", network.ssid.c_str(),
                  (unsigned)wifiIndex + 1, (unsigned)wifiNetworks.size(), network.eapUser.c_str());
    WiFi.begin(network.ssid.c_str(), WPA2_AUTH_PEAP, network.eapUser.c_str(), network.eapUser.c_str(),
               network.password.c_str());
  } else {
#if __has_include("esp_eap_client.h")
    esp_wifi_sta_enterprise_disable();  // else a home or hotspot (WPA2-PSK) attempt still tries EAP
#endif
    Serial.printf("WiFi: trying \"%s\" (%u of %u)\n", network.ssid.c_str(),
                  (unsigned)wifiIndex + 1, (unsigned)wifiNetworks.size());
    WiFi.begin(network.ssid.c_str(), network.password.c_str());
  }
}

void ensureWifi() {
  static bool announced = false;
  if (WiFi.status() == WL_CONNECTED) {
    if (!announced) Serial.printf("WiFi: connected to \"%s\", IP %s, RSSI %d\n", WiFi.SSID().c_str(),
                                  WiFi.localIP().toString().c_str(), (int)WiFi.RSSI());
    announced = true;
    wifiAttemptAt = 0;  // a later drop retries this network first
    return;
  }
  announced = false;
  if (wifiNetworks.empty()) return;
  uint32_t now = millis();
  if (wifiAttemptAt != 0) {
    uint32_t limit = wifiNetworks[wifiIndex].eapUser.length() > 0 ? WIFI_EAP_ATTEMPT_MS : WIFI_ATTEMPT_MS;
    if (now - wifiAttemptAt < limit) return;  // still trying
    // Status 1 = network not found (often 5 GHz only), 4 = connect failed (often wrong password).
    Serial.printf("WiFi: \"%s\" did not connect (status %d), next network\n",
                  wifiNetworks[wifiIndex].ssid.c_str(), WiFi.status());
    wifiIndex = (wifiIndex + 1) % wifiNetworks.size();
  }
  beginWifi(wifiNetworks[wifiIndex]);
  wifiAttemptAt = now;
}

// The full Mozilla root list built into the ESP32 core: no certificate to update when the host renews
// or changes CA. The core is built without certificate date checks, so no clock (NTP) is needed.
extern const uint8_t rootCaBundleStart[] asm("_binary_x509_crt_bundle_start");
extern const uint8_t rootCaBundleEnd[] asm("_binary_x509_crt_bundle_end");
NetworkClientSecure tlsClient;
NetworkClient plainClient;  // http:// URLs: the laptop server on the same hotspot

uint32_t retryDelayMs(uint8_t failures) {
  uint32_t delayMs = min(RETRY_MAX_MS, RETRY_MS << min<uint8_t>(failures - 1, 4));
  return delayMs - delayMs / 4 + random(delayMs / 2);  // +-25 % jitter
}

bool sendReport(bool wallPower) {
  if (WiFi.status() != WL_CONNECTED) return false;

  char extras[160] = "";
  size_t used = 0;
  int battery = readBatteryPercent();
  if (battery >= 0) used += snprintf(extras + used, sizeof(extras) - used, ",\"battery_percent\":%d", battery);
  float amps = readLoadAmps();
  if (amps >= 0) used += snprintf(extras + used, sizeof(extras) - used, ",\"load_amps\":%.2f", amps);
  float tempF = readRoomTempF();
  if (!isnan(tempF)) used += snprintf(extras + used, sizeof(extras) - used, ",\"room_temp_f\":%.1f", tempF);
  if (pendingEvent != nullptr) used += snprintf(extras + used, sizeof(extras) - used, ",\"event\":\"%s\"", pendingEvent);

  // rssi, fw and uptime_s feed the platform's unit health panel; the server ignores them if malformed.
  char body[384];
  snprintf(body, sizeof(body),
           "{\"device_id\":\"%s\",\"wall_power\":%s%s,\"rssi\":%d,\"fw\":\"%s\",\"uptime_s\":%lu}",
           settings.deviceId.c_str(), wallPower ? "true" : "false", extras, (int)WiFi.RSSI(), FIRMWARE_VERSION,
           (unsigned long)(millis() / 1000));

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  bool isHttps = settings.serverUrl.startsWith("https://");
  if (!http.begin(isHttps ? (NetworkClient &)tlsClient : plainClient, settings.serverUrl)) {
    Serial.printf("report: server address \"%s\" is not a valid URL; fix it in the setup screen\n", settings.serverUrl.c_str());
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Token", settings.deviceToken);
  int status = http.POST((uint8_t *)body, strlen(body));
  http.end();

  // Negative status = no answer (-1 connect/TLS failed, -11 timeout). 301/308 = an http:// URL to an https-only host.
  Serial.printf("report wall_power=%d amps=%.2f temp=%.1f event=%s -> HTTP %d\n", wallPower, amps, tempF,
                pendingEvent ? pendingEvent : "-", status);
  lastHttpStatus = status;
  if (status == 200) {
    pendingEvent = nullptr;
    lastReportOkAt = millis();
  }
  return status == 200;
}

// ---- status screen (optional 128x64 I2C OLED) ----------------------------------------------------
// Shows at a glance: wall power, how long it has been out, Wi-Fi, and whether the server confirmed the
// last report. The whole screen inverts during an outage so it reads from across the room.

#if OLED_SDA_PIN >= 0
Adafruit_SSD1306 oled(128, 64, &Wire, -1);
bool oledReady = false;
#endif

void setupDisplay() {
#if OLED_SDA_PIN >= 0
  Wire.begin(OLED_SDA_PIN, OLED_SCL_PIN);
  oledReady = oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS);
  if (!oledReady) {
    Serial.printf("OLED: no display at 0x%02X on SDA %d / SCL %d\n", OLED_ADDRESS, OLED_SDA_PIN, OLED_SCL_PIN);
    return;
  }
  oled.setTextColor(SSD1306_WHITE);
  oled.clearDisplay();
  oled.setTextSize(2);
  oled.setCursor(0, 16);
  oled.print("Lifeline");
  oled.setTextSize(1);
  oled.setCursor(0, 40);
  oled.printf("v%s starting", FIRMWARE_VERSION);
  oled.display();
#endif
}

String elapsedText(uint32_t ms) {
  uint32_t s = ms / 1000;
  char text[16];
  if (s >= 3600) snprintf(text, sizeof text, "%luh %02lum", (unsigned long)(s / 3600), (unsigned long)(s % 3600 / 60));
  else snprintf(text, sizeof text, "%lu:%02lu", (unsigned long)(s / 60), (unsigned long)(s % 60));
  return String(text);
}

void updateDisplay(uint32_t now) {
#if OLED_SDA_PIN >= 0
  static uint32_t lastDrawAt = 0;
  if (!oledReady || now - lastDrawAt < 500) return;
  lastDrawAt = now;
  bool online = WiFi.status() == WL_CONNECTED;
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setCursor(0, 0);
  oled.print("Lifeline");
  oled.setCursor(56, 0);
  if (online) oled.printf("WiFi %ddBm", (int)WiFi.RSSI());
  else oled.print("No WiFi");

  oled.setTextSize(2);
  oled.setCursor(0, 14);
  oled.print(stableWallPower ? "POWER ON" : "POWER OUT");

  oled.setTextSize(1);
  oled.setCursor(0, 34);
  if (!stableWallPower) oled.printf("Out for %s", elapsedText(now - powerLostAt).c_str());
  else oled.print(online ? WiFi.SSID().substring(0, 21).c_str() : "Connecting...");

  oled.setCursor(0, 46);
  if (lastReportOkAt != 0 && lastHttpStatus == 200) oled.printf("Server OK %s ago", elapsedText(now - lastReportOkAt).c_str());
  else if (lastHttpStatus != 0) oled.printf("Server retrying (%d)", lastHttpStatus);
  else oled.print("Server: waiting");

  oled.setCursor(0, 56);
  if (!stableWallPower && alarmSilenced) oled.print("Button pressed: here");
  else if (!stableWallPower) oled.print("Alerting call list");
  else oled.printf("Unit %s", settings.deviceId.c_str());
  oled.invertDisplay(!stableWallPower);  // the whole screen flips during an outage
  oled.display();
#endif
}

// ---- setup screen (Wi-Fi access point + one web form) ------------------------------------------

WebServer setupServer(80);
DNSServer setupDns;

String htmlEscape(const String &text) {
  String out;
  for (char c : text) {
    if (c == '&') out += "&amp;";
    else if (c == '<') out += "&lt;";
    else if (c == '>') out += "&gt;";
    else if (c == '"') out += "&quot;";
    else out += c;
  }
  return out;
}

String setupField(const char *name, const char *label, const String &value, const char *type, const char *hint) {
  return String("<label>") + label + "<input name=\"" + name + "\" type=\"" + type + "\" value=\"" + htmlEscape(value) +
         "\" autocomplete=\"off\"></label>" + (hint[0] ? String("<small>") + hint + "</small>" : String(""));
}

void showSetupForm() {
  String page =
      "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
      "<title>Lifeline unit setup</title><style>"
      "body{font:17px system-ui,sans-serif;max-width:28rem;margin:2rem auto;padding:0 1rem;color:#1f2d36}"
      "label{display:grid;gap:.25rem;font-weight:700;margin-top:1rem}input{font:inherit;padding:.6rem;border:1px solid #9aa;border-radius:6px}"
      "small{color:#566;display:block;margin-top:.2rem}button{margin-top:1.5rem;width:100%;padding:.8rem;font:700 1rem system-ui;"
      "background:#1f2d36;color:#fff;border:0;border-radius:6px}</style></head><body>"
      "<h1>Set up this Lifeline unit</h1><p>Enter the home's Wi-Fi and the details from the agency's patient page.</p>"
      "<form method=\"POST\" action=\"/save\">" +
      setupField("ssid", "Wi-Fi name", settings.ssid, "text", "2.4 GHz networks only. Type it exactly as your phone shows it.") +
      setupField("password", "Wi-Fi password", "", "password", "Leave blank to keep the saved one.") +
      setupField("ssid2", "Backup Wi-Fi name (optional)", settings.ssid2, "text", "A phone hotspot. Tried after the first, then any listed in config.h.") +
      setupField("password2", "Backup Wi-Fi password", "", "password", "Leave blank to keep the saved one.") +
      setupField("server", "Lifeline server address", settings.serverUrl, "url",
                 "Hosted: https://&lt;name&gt;.fly.dev/api/report. Laptop: http://&lt;laptop IP&gt;:8080/api/report") +
      setupField("device", "Unit ID", settings.deviceId, "text", "") +
      setupField("token", "Unit token", "", "password", "Leave blank to keep the saved one.") +
      "<button type=\"submit\">Save and restart</button></form></body></html>";
  setupServer.send(200, "text/html; charset=utf-8", page);
}

void saveSetupForm() {
  prefs.begin("lifeline", false);
  prefs.putString("ssid", setupServer.arg("ssid"));
  if (setupServer.arg("password").length()) prefs.putString("password", setupServer.arg("password"));
  prefs.putString("ssid2", setupServer.arg("ssid2"));
  if (setupServer.arg("password2").length()) prefs.putString("password2", setupServer.arg("password2"));
  prefs.putString("server", setupServer.arg("server"));
  prefs.putString("device", setupServer.arg("device"));
  if (setupServer.arg("token").length()) prefs.putString("token", setupServer.arg("token"));
  prefs.end();
  setupServer.send(200, "text/html; charset=utf-8",
                   "<!DOCTYPE html><meta name=viewport content=\"width=device-width\"><body style=\"font:17px system-ui;margin:2rem\">"
                   "<h1>Saved</h1><p>The unit is restarting and will join the Wi-Fi now. You can close this page.</p>");
  delay(1500);
  ESP.restart();
}

void runSetupScreen() {
  char name[32];
  snprintf(name, sizeof(name), "Lifeline-Setup-%04X", (uint16_t)(ESP.getEfuseMac() & 0xFFFF));
  WiFi.mode(WIFI_AP);
  WiFi.softAP(name);
  setupDns.start(53, "*", WiFi.softAPIP());  // any address opens the form (captive portal)
  setupServer.on("/save", HTTP_POST, saveSetupForm);
  setupServer.onNotFound(showSetupForm);
  setupServer.begin();
  Serial.printf("setup screen: join Wi-Fi \"%s\", then open http://%s\n", name, WiFi.softAPIP().toString().c_str());
  while (true) {  // stays here until the form is saved, which restarts the unit
    setupDns.processNextRequest();
    setupServer.handleClient();
    digitalWrite(STATUS_LED_PIN, (millis() / 500) % 2);  // slow blink: waiting for setup
    delay(5);
  }
}

bool buttonHeldAtPowerOn() {
  if (BUTTON_PIN < 0) return false;
  uint32_t start = millis();
  while (digitalRead(BUTTON_PIN) == LOW) {
    if (millis() - start >= SETUP_HOLD_MS) return true;
    delay(20);
  }
  return false;
}

// ---- main ---------------------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  setupDisplay();
  pinMode(WALL_SENSE_PIN, INPUT);  // divider's lower resistor pulls it low when the adapter is dead
  pinMode(STATUS_LED_PIN, OUTPUT);
  if (BUZZER_PIN >= 0) pinMode(BUZZER_PIN, OUTPUT);
  if (BUTTON_PIN >= 0) pinMode(BUTTON_PIN, INPUT_PULLUP);
#if TEMP_SENSOR_PIN >= 0
  thermometer.begin();
#endif

  loadSettings();
  buildWifiNetworks();
  if (!hasRealSettings() || buttonHeldAtPowerOn()) runSetupScreen();

  // NVS (the setup screen) wins over config.h, so print what is actually in use. Never the token.
  Serial.printf("Lifeline %s, unit %s, server %s\n", FIRMWARE_VERSION, settings.deviceId.c_str(), settings.serverUrl.c_str());
  tlsClient.setCACertBundle(rootCaBundleStart, rootCaBundleEnd - rootCaBundleStart);
  tlsClient.setHandshakeTimeout(HTTP_TIMEOUT_MS / 1000);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);  // ensureWifi decides which network to try next
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
  updateButton(now);

  bool raw = readWallPower();
  if (raw != lastRawWallPower) {
    lastRawWallPower = raw;
    rawChangedAt = now;
  }
  if (raw != stableWallPower && now - rawChangedAt >= DEBOUNCE_MS) {
    stableWallPower = raw;
    changePending = true;
    sendNow = true;
    alarmSilenced = false;
    if (!stableWallPower) powerLostAt = now;
    Serial.printf("wall power %s\n", stableWallPower ? "RESTORED" : "LOST");
    if (!stableWallPower) {
      beep(3, 150);
      lastAlarmAt = now;
    }
  }
  updateAlarm(now);
  updateDisplay(now);

  digitalWrite(STATUS_LED_PIN, stableWallPower ? LOW : HIGH);

  // The unconfirmed state change (and button event) is kept and re-sent until the server answers 200;
  // a newer change replaces it and goes out at once, whatever the backoff.
  if ((sendNow || now - lastAttemptAt >= nextAttemptInMs) && WiFi.status() == WL_CONNECTED) {
    sendNow = false;
    lastAttemptAt = now;
    if (sendReport(stableWallPower)) {
      changePending = false;
      failedAttempts = 0;
      nextAttemptInMs = HEARTBEAT_MS;
    } else {
      if (failedAttempts < 255) failedAttempts++;
      nextAttemptInMs = retryDelayMs(failedAttempts);
    }
  }

  delay(20);
}

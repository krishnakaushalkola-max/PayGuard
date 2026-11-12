/*
  PayGuard hardware approval key (ESP32 DevKit V1)

  Wiring:  push button between GPIO 4 and GND  (internal pull-up is used)
           LED = built-in LED on GPIO 2

  Library needed (Arduino IDE > Library Manager): "ArduinoJson" by Benoit Blanchon (version 7.x)
  Board: "ESP32 Dev Module" (esp32 core 3.x)

  Behaviour: polls the server every 2 s. When a purchase is waiting, the LED turns on.
  Press the button within 30 s to approve. The device signs "<request_id>:<nonce>:<total_cents>"
  with HMAC-SHA256 using the shared secret. The secret is never sent over the network.
*/
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "mbedtls/md.h"

// ======= EDIT THESE 5 LINES =======
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL    = "http://192.168.1.50:5000";   // your computer's LAN IP + port
const char* DEVICE_ID     = "payguard-esp32-01";          // must equal DEVICE_ID in .env
const char* DEVICE_SECRET = "change-me-to-a-long-random-string";  // must equal DEVICE_SECRET in .env
// ==================================

const int BUTTON_PIN = 4;
const int LED_PIN = 2;

String hmacSha256Hex(const String& key, const String& msg) {
  unsigned char out[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, (const unsigned char*)key.c_str(), key.length());
  mbedtls_md_hmac_update(&ctx, (const unsigned char*)msg.c_str(), msg.length());
  mbedtls_md_hmac_finish(&ctx, out);
  mbedtls_md_free(&ctx);
  const char* hex = "0123456789abcdef";
  String s;
  for (int i = 0; i < 32; i++) { s += hex[out[i] >> 4]; s += hex[out[i] & 15]; }
  return s;
}

bool postJson(const String& path, const String& body, String& response, int& code) {
  HTTPClient http;
  http.setTimeout(8000);
  if (!http.begin(String(SERVER_URL) + path)) return false;
  http.addHeader("Content-Type", "application/json");
  code = http.POST(body);
  response = (code > 0) ? http.getString() : "";
  http.end();
  return code > 0;
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(400); Serial.print("."); }
  Serial.println();
  Serial.print("Connected. ESP32 IP: ");
  Serial.println(WiFi.localIP());
}

void blink(int times, int ms) {
  for (int i = 0; i < times; i++) {
    digitalWrite(LED_PIN, HIGH); delay(ms);
    digitalWrite(LED_PIN, LOW);  delay(ms);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  connectWifi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();

  String resp; int code = 0;
  String body = String("{\"device_id\":\"") + DEVICE_ID + "\"}";
  if (!postJson("/api/device/challenge", body, resp, code) || code != 200) {
    Serial.printf("Challenge failed (HTTP %d). Check SERVER_URL / DEVICE_ID.\n", code);
    delay(3000);
    return;
  }

  JsonDocument doc;
  if (deserializeJson(doc, resp) || !doc["pending"].as<bool>()) { delay(2000); return; }

  int requestId = doc["request_id"];
  long totalCents = doc["total_cents"];
  String nonce = doc["nonce"].as<String>();
  String summary = doc["summary"].as<String>();
  unsigned long deadline = millis() + (unsigned long)(doc["expires_in"].as<int>() - 2) * 1000UL;

  Serial.printf("Approval needed: request #%d  $%.2f  [%s]\n", requestId, totalCents / 100.0, summary.c_str());
  Serial.println("Press the button to APPROVE (within 30 s)...");
  digitalWrite(LED_PIN, HIGH);

  while (millis() < deadline) {
    if (digitalRead(BUTTON_PIN) == LOW) {
      delay(40);  // debounce
      if (digitalRead(BUTTON_PIN) != LOW) continue;
      String msg = String(requestId) + ":" + nonce + ":" + String(totalCents);
      String sig = hmacSha256Hex(DEVICE_SECRET, msg);
      String payload = String("{\"device_id\":\"") + DEVICE_ID + "\",\"request_id\":" + requestId +
                       ",\"nonce\":\"" + nonce + "\",\"signature\":\"" + sig + "\"}";
      String r2; int c2 = 0;
      postJson("/api/device/approve", payload, r2, c2);
      Serial.printf("Approve -> HTTP %d %s\n", c2, r2.c_str());
      digitalWrite(LED_PIN, LOW);
      if (c2 == 200) blink(3, 150); else blink(8, 60);
      while (digitalRead(BUTTON_PIN) == LOW) delay(10);  // wait for release
      return;
    }
    delay(15);
  }
  digitalWrite(LED_PIN, LOW);
  Serial.println("Challenge expired. Waiting for next one.");
}

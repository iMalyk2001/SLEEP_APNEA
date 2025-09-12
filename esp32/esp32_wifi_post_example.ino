#include <WiFi.h>
#include <HTTPClient.h>

// ====== CONFIGURE THESE ======
const char* WIFI_SSID = "YOUR_WIFI";
const char* WIFI_PASS = "YOUR_PASS";
// Set this to your PC's LAN IP running FastAPI
const char* SERVER_URL = "http://213.255.135.190/ingest/batch";
// Paste the Device Key from the web app after login/register
const char* DEVICE_KEY = "U48w0wlke6zb5E2ZCZPfRIk0-VveGdyr";
// Target sampling rate (Hz). 1000 = 1 kHz. Reduce if WiFi posts block too long.
const uint32_t SAMPLE_HZ = 1000;
// Post every N samples (batch). 100 samples @1kHz = one POST every 100 ms
const int BATCH_SIZE = 100;
// ====== END CONFIG ======

const int PIEZO1_PIN = 34;
const int PIEZO2_PIN = 35;

struct Sample { uint32_t ts; float s1; float s2; float s3; };
static Sample buf[1024];
static int idx = 0;

static inline float toMilliVolts(int raw) {
  return (float)raw * 3300.0f / 4095.0f; // rough scale; calibrate as needed
}

static void postBatch(int count) {
  if (count <= 0) return;
  String json = "{\"samples\":[";
  for (int i = 0; i < count; i++) {
    const Sample &s = buf[i];
    json += "{\\\"timestamp\\\":" + String(s.ts) +
            ",\\\"sensor1\\\":" + String(s.s1, 2) +
            ",\\\"sensor2\\\":" + String(s.s2, 2) +
            ",\\\"sensor3\\\":" + String(s.s3, 2) + "}";
    if (i < count - 1) json += ",";
  }
  json += "]}";

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  int code = http.POST((uint8_t*)json.c_str(), json.length());
  Serial.printf("POST %d, sent %d samples\n", code, count);
  http.end();
}

void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.println("\nWiFi connected");

  analogReadResolution(12);
  analogSetPinAttenuation(PIEZO1_PIN, ADC_11db);
  analogSetPinAttenuation(PIEZO2_PIN, ADC_11db);
}

void loop() {
  // Keep WiFi alive
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }

  // Micros-based sampling scheduler
  static uint32_t nextSampleUs = micros();
  const uint32_t intervalUs = 1000000UL / SAMPLE_HZ;

  uint32_t nowUs = micros();
  if ((int32_t)(nowUs - nextSampleUs) >= 0) {
    // Take a sample
    uint32_t ts = millis();
    int raw1 = analogRead(PIEZO1_PIN);
    int raw2 = analogRead(PIEZO2_PIN);
    float mv1 = toMilliVolts(raw1);
    float mv2 = toMilliVolts(raw2);

    if (idx < (int)(sizeof(buf) / sizeof(buf[0]))) {
      buf[idx++] = { ts, mv1, mv2, 0.0f };
    }

    // Schedule next sample time (catch up if we are late)
    nextSampleUs += intervalUs;
    if ((int32_t)(nowUs - nextSampleUs) > 0) {
      nextSampleUs = nowUs + intervalUs;
    }
  }

  // If batch filled, post
  if (idx >= BATCH_SIZE) {
    postBatch(idx);
    idx = 0;
  }

  // Yield briefly to WiFi/OS
  delay(0);
}

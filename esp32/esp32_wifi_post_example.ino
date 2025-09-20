#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPmDNS.h>
#include <WebSocketsClient.h>
#include <Wire.h>
#include <Adafruit_ADS1X15.h>

// WiFi credentials
const char* ssid = "YOUR_WIFI";
const char* password = "YOUR_PASS";

// API details
const char* backendHost = "imalyk"; // mDNS host (without .local)
const char* deviceKey = "IbTvZqhCBKNy0XbqR71-Bo0_TEGPE4Fn";

WebSocketsClient wsClient;
unsigned long lastPingMs = 0;
const unsigned long pingIntervalMs = 15000; // 15s heartbeats

// ADS1015 instance (12-bit, max 3300 SPS)
Adafruit_ADS1015 ads;

// Sampling (ADC) and preprocessing
const uint32_t SAMPLE_HZ = 100;            // 100 Hz ADC sampling
static unsigned long nextSampleUs = 0;
static const uint32_t intervalUs = 1000000UL / SAMPLE_HZ;
static const int AVG_READS = 3;            // per-channel averaging reads

// Preprocess targets
static const float HP_CUTOFF_HZ = 0.05f;    // ~0.05 Hz high-pass
static const float LP_CUTOFF_HZ = 2.0f;     // ~2 Hz low-pass
static const float CLIP_MV = 200.0f;        // limiter threshold (mV)
static const uint32_t DS_HZ = 20;           // downsampled rate (~20 Hz)
static const uint32_t DECIM = SAMPLE_HZ / DS_HZ; // 5
static_assert(DECIM == 5, "SAMPLE_HZ must be divisible by DS_HZ");

// Downsampled batch: 0.5 s windows @ 20 Hz => 10 samples
const uint32_t SAMPLES_PER_BATCH = 10;
static unsigned long baseMs = 0;            // grid base
static unsigned long dsCount = 0;           // downsampled sample index
static uint32_t decimCount = 0;             // decimator counter

// Batch buffer (preprocessed + downsampled)
struct Sample { unsigned long ts; float s1mv; float s2mv; };
static Sample buf[SAMPLES_PER_BATCH];
static int bufIdx = 0;

// Unsent batch queue (best-effort local logging in RAM)
static const int MAX_BATCHES = 60; // 60 * 0.5s = 30 seconds retained
static Sample batchQueue[MAX_BATCHES][SAMPLES_PER_BATCH];
static uint8_t batchSizes[MAX_BATCHES];
static int qHead = 0, qTail = 0, qSize = 0;

// First-order HP/LP states per channel
static float hp_prev_x1 = 0.0f, hp_prev_y1 = 0.0f;
static float hp_prev_x2 = 0.0f, hp_prev_y2 = 0.0f;
static float lp_y1 = 0.0f, lp_y2 = 0.0f;

static inline float onePoleHP(float x, float prev_x, float prev_y, float fc, float fs, float &out_prev_x, float &out_prev_y){
  const float dt = 1.0f / fs;
  const float RC = 1.0f / (2.0f * 3.1415926f * fc);
  const float a = RC / (RC + dt);
  const float y = a * (prev_y + x - prev_x);
  out_prev_x = x; out_prev_y = y; return y;
}

static inline float onePoleLP(float x, float prev_y, float fc, float fs){
  const float dt = 1.0f / fs;
  const float RC = 1.0f / (2.0f * 3.1415926f * fc);
  const float a = dt / (RC + dt);
  return prev_y + a * (x - prev_y);
}

static inline float clip(float x, float lim){
  if (x > lim) return lim; if (x < -lim) return -lim; return x;
}

// Read helper: discard first read after mux switch, then average N reads (converted to mV)
static inline float readAveragedMv(uint8_t channel, int n){
  // Dummy read to allow S/H to settle when source impedance is high
  (void)ads.readADC_SingleEnded(channel);
  float sumMv = 0.0f;
  for(int i=0;i<n;i++){
    int16_t raw = ads.readADC_SingleEnded(channel);
    sumMv += ads.computeVolts(raw) * 1000.0f;
  }
  return sumMv / (float)(n > 0 ? n : 1);
}

// Resolved backend IP via mDNS
IPAddress backendIp;

// --- Filtering / baseline ---
float baseline1 = 0.0f, baseline2 = 0.0f;
const float baselineAlpha = 0.999f;  // high value → slow drift tracking
const float noiseThreshold = 2.0f;   // mV, suppress tiny jitter

static bool resolveBackendHost() {
  IPAddress ip = MDNS.queryHost(backendHost);
  if ((uint32_t)ip != 0) {
    backendIp = ip;
    Serial.print("Resolved backend IP: ");
    Serial.println(backendIp.toString());
    return true;
  }
  Serial.println("Failed to resolve backend host via mDNS");
  return false;
}

void setup() {
  Serial.begin(115200);

  // Connect WiFi
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected");

  // mDNS
  if (!MDNS.begin("esp32")) {
    Serial.println("mDNS responder failed");
  } else {
    Serial.println("mDNS responder started");
  }

  // WebSocket
  resolveBackendHost();
  String wsHost = backendIp.toString();
  if (wsHost.length() == 0 || wsHost == "0.0.0.0") wsHost = String(backendHost) + ".local";
  wsClient.begin(wsHost.c_str(), 8000, String("/ws/device?key=") + deviceKey, "ws");
  wsClient.onEvent([](WStype_t type, uint8_t * payload, size_t length){
    if (type == WStype_CONNECTED) Serial.println("WS connected");
    else if (type == WStype_DISCONNECTED) Serial.println("WS disconnected");
    else if (type == WStype_TEXT) Serial.printf("WS text: %.*s\n", (int)length, (const char*)payload);
  });
  wsClient.setReconnectInterval(3000);

  // ADS1015 init
  Wire.begin();
  if (!ads.begin()) {
    Serial.println("ADS1015 not found");
  } else {
    ads.setGain(GAIN_SIXTEEN);              // ±0.256V (better resolution for neonatal signals)
    ads.setDataRate(RATE_ADS1015_3300SPS);  // max throughput; we sample at 100 Hz
    Serial.println("ADS1015 initialized (PGA=±0.256V, 3300 SPS)");
    // Preprocessing pipeline ready
  }
}

void loop() {
  wsClient.loop();

  // Heartbeat
  unsigned long now = millis();
  if (now - lastPingMs >= pingIntervalMs) {
    lastPingMs = now;
    wsClient.sendTXT("ping");
  }

  // --- Sampling + Preprocessing ---
  unsigned long nowUs = micros();
  if (nextSampleUs == 0) nextSampleUs = nowUs;
  if ((long)(nowUs - nextSampleUs) >= 0) {
    const unsigned long nowMs = millis();

    // Read A0 and A1 with dummy-discard + averaging to improve SNR without hardware changes
    float x1 = readAveragedMv(0, AVG_READS);
    float x2 = readAveragedMv(1, AVG_READS);

    // High-pass (~0.05 Hz)
    float hp1 = onePoleHP(x1, hp_prev_x1, hp_prev_y1, HP_CUTOFF_HZ, (float)SAMPLE_HZ, hp_prev_x1, hp_prev_y1);
    float hp2 = onePoleHP(x2, hp_prev_x2, hp_prev_y2, HP_CUTOFF_HZ, (float)SAMPLE_HZ, hp_prev_x2, hp_prev_y2);

    // Low-pass (~2 Hz)
    lp_y1 = onePoleLP(hp1, lp_y1, LP_CUTOFF_HZ, (float)SAMPLE_HZ);
    lp_y2 = onePoleLP(hp2, lp_y2, LP_CUTOFF_HZ, (float)SAMPLE_HZ);

    // Amplitude limiter
    float y1 = clip(lp_y1, CLIP_MV);
    float y2 = clip(lp_y2, CLIP_MV);

    // Downsample to 20 Hz (decimate by 5) with grid-aligned timestamps
    if (++decimCount >= DECIM) {
      decimCount = 0;
      if (baseMs == 0) baseMs = nowMs;
      const unsigned long gridStepMs = (1000UL / DS_HZ);
      const unsigned long ts_ms = baseMs + (dsCount * gridStepMs);
      dsCount++;

      if (bufIdx < (int)(sizeof(buf)/sizeof(buf[0]))) {
        buf[bufIdx++] = { ts_ms, y1, y2 };
      }
    }

    // Next tick
    nextSampleUs += intervalUs;
    if ((long)(nowUs - nextSampleUs) > 0) nextSampleUs = nowUs + intervalUs;
  }

  // --- Finalize current batch into queue ---
  if (bufIdx >= (int)SAMPLES_PER_BATCH) {
    // Enqueue
    if (qSize < MAX_BATCHES) {
      for (int i = 0; i < (int)SAMPLES_PER_BATCH; i++) batchQueue[qHead][i] = buf[i];
      batchSizes[qHead] = (uint8_t)SAMPLES_PER_BATCH;
      qHead = (qHead + 1) % MAX_BATCHES; qSize++;
    } else {
      // overwrite oldest
      for (int i = 0; i < (int)SAMPLES_PER_BATCH; i++) batchQueue[qTail][i] = buf[i];
      batchSizes[qTail] = (uint8_t)SAMPLES_PER_BATCH;
      qTail = (qTail + 1) % MAX_BATCHES; // size unchanged (full)
    }
    bufIdx = 0;
  }

  // --- Transmit queued batches over Wi-Fi ---
  if (qSize > 0 && WiFi.status() == WL_CONNECTED) {
    const int idx = qTail;
    HTTPClient http;
    String url = String("http://") + (backendIp ? backendIp.toString() : String(backendHost) + ".local") + ":8000/ingest/batch";
    String json = "{\"samples\":[";
    for (int i = 0; i < (int)batchSizes[idx]; i++) {
      const Sample &s = batchQueue[idx][i];
      json += "{\"timestamp_ms\":" + String(s.ts) +
              ",\"sensor1_mV\":" + String(s.s1mv, 2) +
              ",\"sensor2_mV\":" + String(s.s2mv, 2) + "}";
      if (i < (int)batchSizes[idx] - 1) json += ",";
    }
    json += "]}";
    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Key", deviceKey);
    int code = http.POST((uint8_t*)json.c_str(), json.length());
    http.end();
    if (code >= 200 && code < 300) {
      Serial.printf("POST /ingest/batch %d, sent %d samples (queued=%d)\n", code, (int)batchSizes[idx], qSize);
      qTail = (qTail + 1) % MAX_BATCHES; qSize--;
    } else {
      // leave in queue; retry later
      Serial.printf("POST failed %d, will retry; queued=%d\n", code, qSize);
    }
  }
}

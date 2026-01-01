# Breathing Signal Monitoring Web App

Hardware: ESP32 WROOM + 2-3 piezo sensors. Data sent over WiFi via HTTP POST to backend; backend broadcasts via WebSocket to browser frontend.

## Stack
- ESP32: Arduino (HTTP POST batching at 1 kHz)
- Backend: FastAPI + SQLite + WebSocket
- Frontend: HTML + vanilla JS + Chart.js + IndexedDB

## Features
- Auth (register/login) with per-user device key
- Real-time charts (up to 3 sensors), dark medical theme, responsive
- Sample rate meter, connection status
- Browser logging per-user (IndexedDB) and CSV export

## Run (Windows PowerShell)
```powershell
python -m venv .venv
. .venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
python -m uvicorn app.main:app --app-dir backend --reload --port 8000
```
Open `http://localhost:8000` in your browser.

## API
- POST `/auth/register` JSON { username, password } -> { access_token, device_key }
- POST `/auth/login` form (username, password) -> { access_token, device_key }
- POST `/ingest` (single) header `X-Device-Key: <key>` body:
```json
{ "timestamp": 1234567890, "sensor1": 123.45, "sensor2": 98.76, "sensor3": 11.22 }
```
- POST `/ingest/batch` header `X-Device-Key` body:
```json
{ "samples": [ { "timestamp": 123, "sensor1": 1, "sensor2": 2 }, ... ] }
```
- WS `/ws?token=<jwt>`: server broadcasts samples per authenticated user

## ESP32 (Arduino) Example
- Sample sensors at 1 kHz, buffer 50–200 samples, POST batch to reduce overhead.
- Use `X-Device-Key` from the UI after registering/logging in.

```cpp
#include <WiFi.h>
#include <HTTPClient.h>

const char* WIFI_SSID = "YOUR_WIFI";
const char* WIFI_PASS = "YOUR_PASS";
const char* SERVER = "http://YOUR_PC_IP:8000/ingest/batch"; // replace with host IP
const char* DEVICE_KEY = "PASTE_FROM_UI";

struct Sample { uint32_t ts; float s1; float s2; float s3; };
static const int BATCH = 100; // 100 samples per POST
Sample buf[BATCH];
volatile int idx = 0;

hw_timer_t* timer = NULL;
portMUX_TYPE timerMux = portMUX_INITIALIZER_UNLOCKED;

void IRAM_ATTR onTimer(){
	portENTER_CRITICAL_ISR(&timerMux);
	uint32_t t = millis();
	float s1 = analogRead(34); // scale to mV as needed
	float s2 = analogRead(35);
	float s3 = 0; // optional
	buf[idx++] = { t, s1, s2, s3 };
	if(idx >= BATCH) idx = 0; // overwrite if network slow
	portEXIT_CRITICAL_ISR(&timerMux);
}

void setup(){
	Serial.begin(115200);
	WiFi.begin(WIFI_SSID, WIFI_PASS);
	while(WiFi.status()!=WL_CONNECTED){ delay(500); Serial.print("."); }
	Serial.println("\nWiFi connected");

	analogReadResolution(12); // scale to your sensors

	timer = timerBegin(0, 80, true); // 80 MHz / 80 = 1 MHz
	timerAttachInterrupt(timer, &onTimer, true);
	timerAlarmWrite(timer, 1000, true); // 1 kHz
	timerAlarmEnable(timer);
}

void loop(){
	static unsigned long lastPost = 0;
	if(millis() - lastPost >= 100){ // ~10 Hz POST cadence
		lastPost = millis();
		int n = 0;
		noInterrupts(); n = idx; interrupts();
		if(n > 0){
			// Build JSON
			String json = "{\"samples\":[";
			for(int i=0;i<n;i++){
				json += "{\\\"timestamp\\\":" + String(buf[i].ts) + ",\\\"sensor1\\\":" + String(buf[i].s1) + ",\\\"sensor2\\\":" + String(buf[i].s2) + ",\\\"sensor3\\\":" + String(buf[i].s3) + "}";
				if(i < n-1) json += ",";
			}
			json += "]}";

			HTTPClient http;
			http.begin(SERVER);
			http.addHeader("Content-Type", "application/json");
			http.addHeader("X-Device-Key", DEVICE_KEY);
			int code = http.POST((uint8_t*)json.c_str(), json.length());
			http.end();
		}
	}
}

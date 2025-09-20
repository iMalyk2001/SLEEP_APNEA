// breath_pipeline.h (single-header module)
// Hardware assumptions:
// - ADS1015 @ 0x48, SDA=21, SCL=22, VDD=3.3V, common GND
// - Piezo OUT -> 100k series -> ADS A0/A1; ADS inputs have 10nF to GND; optional 1M bleed
// - Default PGA: GAIN_SIXTEEN (±0.256 V) for small neonatal signals; switch to GAIN_TWO if needed
// - Processing fs_proc = 100 Hz; optional diagnostic burst buffer
//
// Neonatal defaults:
// - Resp band ~0.2–3.0 Hz; peak spacing and refractory tuned accordingly
// - Apnea >= 20 s; hypopnea if envelope depressed vs baseline for >= 10 s
//
// Integration example (in your .ino):
//   #include <Adafruit_ADS1X15.h>
//   #include "breath_pipeline.h"
//   Adafruit_ADS1015 ads;
//   BreathPipeline pipeline;
//   void onEvent(const BreathPipeline::Event& ev){ /* queue/send */ }
//   void setup(){
//     Wire.begin(21,22);
//     ads.begin(0x48);
//     ads.setGain(GAIN_SIXTEEN); // ±0.256V default
//     BreathPipeline::Config cfg; cfg.primaryChannel = BreathPipeline::PrimaryChannel::CH2_A1;
//     pipeline.begin(&ads, cfg);
//     pipeline.setEventCallback(onEvent);
//   }
//   void loop(){ pipeline.tick(); }

#pragma once

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_ADS1X15.h>

class BreathPipeline {
public:
	enum class PrimaryChannel : uint8_t { CH1_A0 = 0, CH2_A1 = 1 };

	struct Config {
		uint32_t fsProcHz = 100;           // processing sample rate (Hz)
		bool useADS1115 = false;           // set true if ADS1115 (16-bit) is used
		adsGain_t adsGain = GAIN_SIXTEEN;  // default ±0.256 V
		uint8_t adsChannel1 = 0;           // A0
		uint8_t adsChannel2 = 1;           // A1
		PrimaryChannel primaryChannel = PrimaryChannel::CH2_A1; // Sensor 2 primary

		// Baseline / DC removal (EMA)
		float baselineTauSec = 5.0f;
		// Anti-ring MA
		uint8_t antiRingTaps = 3;          // 3..5 recommended
		// Envelope (rectified EMA)
		float envTauSec = 0.3f;
		// Peak detection
		float minPeakDistanceSec = 0.6f;
		float refractorySec = 0.4f;
		// Adaptive threshold (EMA of envelope peaks)
		float thrEmaTauSec = 60.0f;
		float thrFactor = 0.45f;
		// Hypopnea
		float hypopneaFrac = 0.5f;
		float hypopneaMinSec = 10.0f;
		// Apnea
		float apneaMinSec = 20.0f;
		float recoveryMinSec = 3.0f;
		// Artifact detection
		float railMarginMV = 2.0f;
		float spikeDerivMV = 30.0f;
		float rmsBurstFactor = 3.0f;
		// Burst capacity (diagnostics)
		uint16_t burstFsHz = 1000;
		uint16_t burstPreMs = 3000;
		uint16_t burstPostMs = 3000;
	};

	struct ChannelState {
		static constexpr uint8_t MAX_MA = 8;
		float dcBaseline = 0.0f;
		float maBuf[MAX_MA] = {0};
		uint8_t maIdx = 0;
		uint8_t maFill = 0;
		float env = 0.0f;
		float envBaseline = 0.0f;
		uint32_t lastPeakMs = 0;
		uint32_t lastCrossMs = 0;
		float lastEnvPeak = 0.0f;
	};

	struct Status {
		float bpm = 0.0f;
		bool signalOK = false;
		bool apneaActive = false;
		bool hypopneaActive = false;
		bool artifact = false;
		float envPrimary = 0.0f;
		float envBaselinePrimary = 0.0f;
		float thresholdPrimary = 0.0f;
		float snrEstimate = 0.0f;
	};

	enum class EventType : uint8_t {
		ApneaStart, ApneaEnd, HypopneaStart, HypopneaEnd, ArtifactDetected
	};

	struct Event { EventType type; uint32_t tsMs; uint32_t durationMs; };
	typedef void (*EventCallback)(const Event&);

	static constexpr size_t TELE_CAP = 256;
	struct Telemetry {
		uint32_t tsMs; float bpm; bool signalOK; bool apnea; bool hypopnea; bool artifact; float env; float thr;
	};

public:
	BreathPipeline() {}
	void begin(Adafruit_ADS1015* ads, const Config& cfg) {
		_ads = ads; _cfg = cfg;
		_alphaDC = alphaFromTau(_cfg.baselineTauSec, _cfg.fsProcHz);
		_alphaEnv = alphaFromTau(_cfg.envTauSec, _cfg.fsProcHz);
		_alphaThr = alphaFromTau(_cfg.thrEmaTauSec, _cfg.fsProcHz);
		_intervalUs = 1000000UL / (uint32_t)max((uint32_t)1, _cfg.fsProcHz);
		_nextSampleUs = micros();
		memset(_rrBuf, 0, sizeof(_rrBuf)); _rrIdx = _rrFill = 0; _stat = {};
		_tlHead = _tlTail = 0; _burstActive = false; _burstPostRemain = 0;
		if (_ads) _ads->setGain(_cfg.adsGain);
		_lsb_mV = computeLsbMilliVolts(_cfg.useADS1115, _cfg.adsGain);
	}

	void tick() {
		const uint32_t nowUs = micros();
		if ((int32_t)(nowUs - _nextSampleUs) < 0) return;
		_nextSampleUs += _intervalUs;
		int16_t c0 = _ads ? _ads->readADC_SingleEnded(_cfg.adsChannel1) : 0;
		int16_t c1 = _ads ? _ads->readADC_SingleEnded(_cfg.adsChannel2) : 0;
		const float mv0 = countsToMilliVolts(c0);
		const float mv1 = countsToMilliVolts(c1);
		processOne(_ch1, mv0);
		processOne(_ch2, mv1);
		const bool useCh2 = (_cfg.primaryChannel == PrimaryChannel::CH2_A1);
		ChannelState& P = useCh2 ? _ch2 : _ch1;
		const float mvP = useCh2 ? mv1 : mv0;
		const uint32_t nowMs = millis();
		bool artifact = detectArtifact(P, mvP);
		_stat.artifact = artifact;
		const float base = max(P.envBaseline, 1e-6f);
		const float thr = _cfg.thrFactor * base;
		const float env = P.env;
		const bool above = (env >= thr) && !artifact;
		if (above) P.lastCrossMs = nowMs;
		if (!artifact) peakDetectAndRR(P, nowMs);
		const bool hypoNow = (P.lastEnvPeak < _cfg.hypopneaFrac * base) && !artifact;
		updateHypopneaFSM(nowMs, hypoNow);
		const uint32_t since = nowMs - P.lastCrossMs;
		const bool apneaNow = since >= (uint32_t)(_cfg.apneaMinSec * 1000.0f);
		updateApneaFSM(nowMs, apneaNow);
		_stat.signalOK = (nowMs - P.lastCrossMs) < (uint32_t)(2000);
		_stat.envPrimary = env; _stat.envBaselinePrimary = P.envBaseline; _stat.thresholdPrimary = thr;
		_stat.snrEstimate = base > 1e-6f ? (env / base) : 0.0f;
		pushTele(nowMs);
		handleBurst(nowMs, mv0, mv1);
	}

	Status getStatus() const { return _stat; }
	void setEventCallback(EventCallback cb) { _cb = cb; }
	bool popTelemetry(Telemetry& out) {
		if (_tlHead == _tlTail) return false;
		out = _tele[_tlTail];
		_tlTail = (uint16_t)((_tlTail + 1) % TELE_CAP);
		return true;
	}
	void triggerBurst(uint16_t postMs) { _burstActive = true; _burstPostRemain = postMs; }
	size_t exportBurst(int16_t* ch1Buf, int16_t* ch2Buf, size_t maxSamples) {
		const size_t have = min(_burstFill, maxSamples);
		for (size_t i = 0; i < have; i++) { const size_t idx = (_burstTail + i) % BURST_CAP; ch1Buf[i] = _burstCh1[idx]; ch2Buf[i] = _burstCh2[idx]; }
		return have;
	}
	void updateConfig(const Config& cfg) {
		_cfg = cfg; _alphaDC = alphaFromTau(_cfg.baselineTauSec, _cfg.fsProcHz); _alphaEnv = alphaFromTau(_cfg.envTauSec, _cfg.fsProcHz); _alphaThr = alphaFromTau(_cfg.thrEmaTauSec, _cfg.fsProcHz);
		if (_ads) _ads->setGain(_cfg.adsGain);
		_lsb_mV = computeLsbMilliVolts(_cfg.useADS1115, _cfg.adsGain);
		_intervalUs = 1000000UL / (uint32_t)max((uint32_t)1, _cfg.fsProcHz);
	}

private:
	static constexpr uint8_t RR_WIN = 6;
	static constexpr size_t BURST_CAP = 16000;

	Adafruit_ADS1015* _ads = nullptr;
	Config _cfg;
	ChannelState _ch1, _ch2;
	Status _stat;
	uint32_t _intervalUs = 10000; uint32_t _nextSampleUs = 0;
	float _rrBuf[RR_WIN] = {0}; uint8_t _rrIdx = 0, _rrFill = 0;
	Telemetry _tele[TELE_CAP]; uint16_t _tlHead = 0, _tlTail = 0;
	int16_t _burstCh1[BURST_CAP] = {0}, _burstCh2[BURST_CAP] = {0}; size_t _burstHead = 0, _burstTail = 0, _burstFill = 0; bool _burstActive = false; uint16_t _burstPostRemain = 0;
	float _alphaDC = 0.0f, _alphaEnv = 0.0f, _alphaThr = 0.0f; float _lsb_mV = 0.125f;
	EventCallback _cb = nullptr;

	static float alphaFromTau(float tauSec, uint32_t fs) {
		if (tauSec <= 0.0f) return 1.0f; const float dt = 1.0f / max(1u, fs); return 1.0f - expf(-dt / tauSec);
	}
	float computeLsbMilliVolts(bool ads1115, adsGain_t g) {
		float fsV = 2.048f;
		switch (g) {
			case GAIN_TWOTHIRDS: fsV = 6.144f; break; case GAIN_ONE: fsV = 4.096f; break; case GAIN_TWO: fsV = 2.048f; break;
			case GAIN_FOUR: fsV = 1.024f; break; case GAIN_EIGHT: fsV = 0.512f; break; case GAIN_SIXTEEN: fsV = 0.256f; break;
			default: fsV = 2.048f; break;
		}
		const float lsbV = ads1115 ? (fsV / 32768.0f) : (fsV / 2048.0f);
		return lsbV * 1000.0f;
	}
	float countsToMilliVolts(int16_t counts) const { return (float)counts * _lsb_mV; }
	void processOne(ChannelState& C, float mv) {
		C.dcBaseline = (1.0f - _alphaDC) * C.dcBaseline + _alphaDC * mv;
		const float detr = mv - C.dcBaseline;
		const uint8_t taps = min(C.MAX_MA, max((uint8_t)1, _cfg.antiRingTaps));
		C.maBuf[C.maIdx] = detr; C.maIdx = (uint8_t)((C.maIdx + 1) % taps); if (C.maFill < taps) C.maFill++;
		float ma = 0.0f; for (uint8_t i = 0; i < C.maFill; i++) ma += C.maBuf[i]; ma /= (float)max((uint8_t)1, C.maFill);
		const float rect = fabsf(ma);
		C.env = (1.0f - _alphaEnv) * C.env + _alphaEnv * rect;
		if (C.env > C.envBaseline) { C.envBaseline = (1.0f - _alphaThr) * C.envBaseline + _alphaThr * C.env; C.lastEnvPeak = C.env; }
		else { C.envBaseline = max(C.envBaseline * 0.9995f, C.env * 0.9f); }
	}
	bool detectArtifact(const ChannelState& C, float mv) const {
		const float railMv = railMilliVolts(); if (fabsf(railMv - fabsf(mv)) <= _cfg.railMarginMV) return true;
		static float prevEnv = 0.0f; const float dEnv = C.env - prevEnv; prevEnv = C.env; if (fabsf(dEnv) > _cfg.spikeDerivMV) return true;
		const float base = max(C.envBaseline, 1e-6f); if (C.env > _cfg.rmsBurstFactor * base) return true; return false;
	}
	float railMilliVolts() const {
		switch (_cfg.adsGain) { case GAIN_TWOTHIRDS: return 6144.0f; case GAIN_ONE: return 4096.0f; case GAIN_TWO: return 2048.0f; case GAIN_FOUR: return 1024.0f; case GAIN_EIGHT: return 512.0f; case GAIN_SIXTEEN: return 256.0f; default: return 2048.0f; }
	}
	void peakDetectAndRR(ChannelState& C, uint32_t nowMs) {
		const float thr = _cfg.thrFactor * max(C.envBaseline, 1e-6f);
		const bool above = (C.env >= thr);
		static bool prevAbove = false; static uint32_t lastEventMs = 0; const bool rising = (above && !prevAbove); prevAbove = above;
		const uint32_t minDistMs = (uint32_t)(_cfg.minPeakDistanceSec * 1000.0f);
		const uint32_t refractoryMs = (uint32_t)(_cfg.refractorySec * 1000.0f);
		if (rising) {
			if ((nowMs - C.lastPeakMs) >= minDistMs && (nowMs - lastEventMs) >= refractoryMs) {
				if (C.lastPeakMs != 0) { const float ibiSec = (nowMs - C.lastPeakMs) / 1000.0f; if (ibiSec > 0.2f && ibiSec < 10.0f) { _rrBuf[_rrIdx] = 60.0f / ibiSec; _rrIdx = (uint8_t)((_rrIdx + 1) % RR_WIN); if (_rrFill < RR_WIN) _rrFill++; _stat.bpm = robustBpm(); } }
				C.lastPeakMs = nowMs; C.lastEnvPeak = C.env; lastEventMs = nowMs;
			}
		}
	}
	float robustBpm() const {
		float tmp[RR_WIN]; const uint8_t n = _rrFill; for (uint8_t i = 0; i < n; i++) tmp[i] = _rrBuf[i];
		for (uint8_t i = 0; i < n; i++) { for (uint8_t j = i + 1; j < n; j++) { if (tmp[j] < tmp[i]) { float t = tmp[i]; tmp[i] = tmp[j]; tmp[j] = t; } } }
		if (n == 0) return 0.0f; return (n % 2) ? tmp[n/2] : 0.5f * (tmp[n/2 - 1] + tmp[n/2]);
	}
	void updateApneaFSM(uint32_t nowMs, bool apneaNow) {
		static bool apneaActive = false;
		if (apneaNow && !apneaActive) { apneaActive = true; _stat.apneaActive = true; if (_cb) _cb(Event{ EventType::ApneaStart, nowMs, 0 }); }
		else if (!apneaNow && apneaActive) { apneaActive = false; _stat.apneaActive = false; if (_cb) _cb(Event{ EventType::ApneaEnd, nowMs, 0 }); }
	}
	void updateHypopneaFSM(uint32_t nowMs, bool hypoNow) {
		static uint32_t hypoStartMs = 0; static bool hypoActive = false;
		if (hypoNow) { if (!hypoActive) { if (hypoStartMs == 0) hypoStartMs = nowMs; if ((nowMs - hypoStartMs) >= (uint32_t)(_cfg.hypopneaMinSec * 1000.0f)) { hypoActive = true; _stat.hypopneaActive = true; if (_cb) _cb(Event{ EventType::HypopneaStart, nowMs, 0 }); } } }
		else { hypoStartMs = 0; if (hypoActive) { hypoActive = false; _stat.hypopneaActive = false; if (_cb) _cb(Event{ EventType::HypopneaEnd, nowMs, 0 }); } }
	}
	void pushTele(uint32_t tsMs) {
		const uint16_t next = (uint16_t)((_tlHead + 1) % TELE_CAP); if (next == _tlTail) { _tlTail = (uint16_t)((_tlTail + 1) % TELE_CAP); }
		_tele[_tlHead] = Telemetry{ tsMs, _stat.bpm, _stat.signalOK, _stat.apneaActive, _stat.hypopneaActive, _stat.artifact, _stat.envPrimary, _stat.thresholdPrimary };
		_tlHead = next;
	}
	void handleBurst(uint32_t /*nowMs*/, float mv0, float mv1) {
		const int16_t c0 = (int16_t)roundf(mv0 / _lsb_mV); const int16_t c1 = (int16_t)roundf(mv1 / _lsb_mV); pushBurst(c0, c1);
		if (_burstActive) { const uint32_t stepMs = 1000 / max((uint32_t)1, _cfg.fsProcHz); if (_burstPostRemain > stepMs) _burstPostRemain -= stepMs; else _burstPostRemain = 0; if (_burstPostRemain == 0) _burstActive = false; }
	}
	void pushBurst(int16_t c0, int16_t c1) {
		const size_t need = (size_t)((_cfg.burstPreMs + _cfg.burstPostMs) * (_cfg.fsProcHz / 1000.0f)); const size_t cap = min(BURST_CAP, max((size_t)64, need));
		_burstCh1[_burstHead] = c0; _burstCh2[_burstHead] = c1; _burstHead = (_burstHead + 1) % cap; if (_burstFill < cap) { _burstFill++; } else { _burstTail = (_burstTail + 1) % cap; }
	}
};

// Memory budget (defaults):
// - Telemetry ring: 256 * ~24 bytes ≈ ~6.5 KB
// - Burst ring: 16k * 2ch * 2B ≈ 64 KB
// - States/overhead ≈ < 4 KB
// Total ≈ ~75 KB






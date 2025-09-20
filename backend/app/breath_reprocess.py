"""
Standalone reprocessing module for neonatal chest-movement (respiratory) signals.

Hardware assumptions:
	- ADC: ADS1015 @ I2C 0x48; SDA=21, SCL=22; VDD=3.3V; common ground
	- Sensors: Piezo OUT -> 100k series -> ADS A0/A1; ADS inputs have 10nF to GND; optional 1M bleed
	- Default PGA: ±0.256 V (GAIN_SIXTEEN) for small neonatal signals; allow switching
	- Sampling: default fs=100 Hz (suitable for 0.2–3.0 Hz respiratory band)

Scope:
	- No backend endpoint/route changes. Import and call from tasks/offline QA.
	- Higher-order filters and diagnostics vs. edge; defaults tuned for neonates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.signal import butter, sosfiltfilt


# Gain → full-scale volts mapping per ADS datasheet
GAIN_TO_FS_V: Dict[str, float] = {
	"2/3": 6.144,
	"1": 4.096,
	"2": 2.048,
	"4": 1.024,
	"8": 0.512,
	"16": 0.256,
}


def ads_counts_to_mv(counts: np.ndarray, gain: str = "16", use_ads1115: bool = False) -> np.ndarray:
	"""Convert ADS counts to millivolts for ADS1015/ADS1115.

	counts: raw integer counts from ADC
	gain: string key of PGA setting in GAIN_TO_FS_V
	use_ads1115: True if 16-bit, False if 12-bit ADS1015
	"""
	fs_v = float(GAIN_TO_FS_V.get(gain, 2.048))
	lsb_v = (fs_v / (32768.0 if use_ads1115 else 2048.0))
	return counts.astype(np.float64) * (lsb_v * 1000.0)


@dataclass
class ReprocessCfg:
	fs_hz: float = 100.0
	band_low_hz: float = 0.2
	band_high_hz: float = 3.0
	filter_order: int = 6
	env_tau_sec: float = 0.3
	thr_tau_sec: float = 60.0
	thr_factor: float = 0.45
	min_peak_dist_sec: float = 0.6
	refractory_sec: float = 0.4
	apnea_min_sec: float = 20.0
	hypopnea_frac: float = 0.5
	hypopnea_min_sec: float = 10.0
	artifact_burst_factor: float = 3.0
	snr_min: float = 5.0
	primary: str = "s2"  # "s1" or "s2"; default s2 per system preference


def bandpass_sos(low_hz: float, high_hz: float, fs: float, order: int) -> np.ndarray:
	nyq = 0.5 * fs
	lo = max(1e-6, low_hz / nyq)
	hi = min(0.999, high_hz / nyq)
	return butter(order, [lo, hi], btype="band", output="sos")


def envelope_rectified_ema(y: np.ndarray, fs: float, tau_sec: float) -> np.ndarray:
	"""Rectify and apply EMA envelope."""
	dt = 1.0 / max(1.0, fs)
	alpha = 1.0 - np.exp(-dt / max(1e-3, tau_sec))
	env = np.empty_like(y, dtype=np.float64)
	acc = 0.0
	for i, v in enumerate(np.abs(y)):
		acc = (1.0 - alpha) * acc + alpha * float(v)
		env[i] = acc
	return env


def estimate_snr(x: np.ndarray) -> float:
	"""Robust SNR proxy using power vs. MAD-based noise."""
	if x.size < 16:
		return 0.0
	p_signal = float(np.mean(x ** 2))
	noise = float(np.median(np.abs(x - np.median(x))))
	p_noise = max(1e-12, noise * noise * 2.0)
	return p_signal / p_noise


def _ema(prev: float, value: float, dt: float, tau: float) -> float:
	if tau <= 0:
		return value
	alpha = 1.0 - np.exp(-dt / tau)
	return (1.0 - alpha) * prev + alpha * value


def _detect_peaks_from_env(t_ms: np.ndarray, env: np.ndarray, cfg: ReprocessCfg) -> Tuple[List[int], float]:
	"""Find peaks via threshold crossings on envelope with min distance and refractory.
	Returns (peak_indices, bpm_median)."""
	refr_ms = int(cfg.refractory_sec * 1000.0)
	min_dist_ms = int(cfg.min_peak_dist_sec * 1000.0)
	dt = 1.0 / cfg.fs_hz
	base = 0.0
	prev_above = False
	last_peak_ms = -10**9
	peaks: List[int] = []

	for i in range(env.size):
		v = float(env[i])
		# EMA of peaks baseline (increase only)
		if v > base:
			base = _ema(base, v, dt, cfg.thr_tau_sec)
		thr = cfg.thr_factor * max(1e-6, base)
		above = (v >= thr)
		ts = int(t_ms[i])
		if above and not prev_above:
			if (ts - last_peak_ms) >= min_dist_ms and (ts - last_peak_ms) >= refr_ms:
				peaks.append(i)
				last_peak_ms = ts
		prev_above = above

	bpm = 0.0
	if len(peaks) >= 2:
		ibis = np.diff(np.array([t_ms[p] for p in peaks], dtype=np.float64)) / 1000.0
		bpm_vals = 60.0 / np.maximum(1e-3, ibis)
		bpm = float(np.median(bpm_vals))
	return peaks, bpm


def detect_events(t_ms: np.ndarray, env: np.ndarray, cfg: ReprocessCfg) -> Tuple[float, List[Dict[str, object]]]:
	"""Compute BPM and apnea/hypopnea events strictly from the provided envelope."""
	peaks, bpm = _detect_peaks_from_env(t_ms, env, cfg)
	# Build threshold history for apnea timing
	dt = 1.0 / cfg.fs_hz
	base = 0.0
	last_cross_ts: Optional[int] = None
	thr_hist: List[float] = []
	for i in range(env.size):
		v = float(env[i])
		if v > base:
			base = _ema(base, v, dt, cfg.thr_tau_sec)
		thr = cfg.thr_factor * max(1e-6, base)
		thr_hist.append(thr)
		if v >= thr:
			last_cross_ts = int(t_ms[i])

	events: List[Dict[str, object]] = []
	apnea_active = False
	apnea_start: Optional[int] = None
	hypo_active = False
	hypo_start: Optional[int] = None

	for i in range(env.size):
		ts = int(t_ms[i])
		v = float(env[i])
		thr = float(thr_hist[i])
		if v >= thr:
			last_cross_ts = ts
		apnea_now = (last_cross_ts is None) or ((ts - int(last_cross_ts)) >= int(cfg.apnea_min_sec * 1000.0))
		if apnea_now and not apnea_active:
			apnea_active = True
			apnea_start = ts - int(cfg.apnea_min_sec * 1000.0)
			events.append({"type": "apnea_start", "ts": apnea_start})
		elif (not apnea_now) and apnea_active:
			events.append({"type": "apnea_end", "ts": ts, "duration_ms": ts - int(apnea_start or ts)})
			apnea_active = False
			apnea_start = None

		# Hypopnea: envelope depressed vs a local max over last ~1s
		loc_start = max(0, i - int(cfg.fs_hz))
		loc_base = float(np.max(env[loc_start:i+1])) if i > 0 else float(env[0])
		hypo_now = (v < cfg.hypopnea_frac * max(1e-6, loc_base))
		if hypo_now and not hypo_active and hypo_start is None:
			hypo_start = ts
		if hypo_now and (ts - int(hypo_start or ts)) >= int(cfg.hypopnea_min_sec * 1000.0) and not hypo_active:
			hypo_active = True
			events.append({"type": "hypopnea_start", "ts": int(hypo_start)})
		if hypo_active and not hypo_now:
			events.append({"type": "hypopnea_end", "ts": ts, "duration_ms": ts - int(hypo_start or ts)})
			hypo_active = False
			hypo_start = None

	return bpm, events


def reprocess_stream(
				 t_ms: np.ndarray,
				 s1_mv: np.ndarray,
				 s2_mv: np.ndarray,
				 fs: float = 100.0,
				 primary: str = "s2",
				 cfg: Optional[ReprocessCfg] = None,
		 ) -> Dict[str, object]:
	"""High-fidelity reprocessing of a time-aligned stream (no route changes).

	Returns a dict with bpm, events, snr1/snr2, and the primary envelope.
	"""
	cfg = cfg or ReprocessCfg(fs_hz=fs, primary=primary)
	# Bandpass
	sos = bandpass_sos(cfg.band_low_hz, cfg.band_high_hz, cfg.fs_hz, cfg.filter_order)
	x1 = np.asarray(s1_mv, dtype=np.float64)
	x2 = np.asarray(s2_mv, dtype=np.float64)
	y1 = sosfiltfilt(sos, x1) if x1.size else x1
	y2 = sosfiltfilt(sos, x2) if x2.size else x2
	# Envelope
	env1 = envelope_rectified_ema(y1, cfg.fs_hz, cfg.env_tau_sec)
	env2 = envelope_rectified_ema(y2, cfg.fs_hz, cfg.env_tau_sec)
	# SNR
	snr1 = estimate_snr(y1)
	snr2 = estimate_snr(y2)
	# Primary selection (strictly use s2 by default)
	envP = env2 if cfg.primary.lower() == "s2" else env1
	bpm, events = detect_events(np.asarray(t_ms, dtype=np.float64), envP, cfg)
	return {
		"bpm": bpm,
		"events": events,
		"snr1": float(snr1),
		"snr2": float(snr2),
		"env_primary": envP.tolist() if hasattr(envP, "tolist") else list(envP),
		"cfg": cfg.__dict__,
	}


def read_csv_samples(path: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
	"""Read CSV exported by the frontend: timestamp,sensor1_mV,sensor2_mV,sensor3"""
	data = np.genfromtxt(path, delimiter=",", names=True, dtype=None, encoding=None)
	t = np.asarray(data["timestamp"], dtype=np.float64)
	s1 = np.asarray(data["sensor1_mV"], dtype=np.float64)
	s2 = np.asarray(data["sensor2_mV"], dtype=np.float64)
	return t, s1, s2


if __name__ == "__main__":
	import argparse, json
	parser = argparse.ArgumentParser(description="Respiratory reprocessing (neonatal)")
	parser.add_argument("csv", help="CSV with columns timestamp,sensor1_mV,sensor2_mV")
	parser.add_argument("--fs", type=float, default=100.0)
	parser.add_argument("--primary", choices=["s1","s2"], default="s2")
	args = parser.parse_args()
	t, s1, s2 = read_csv_samples(args.csv)
	res = reprocess_stream(t, s1, s2, fs=args.fs, primary=args.primary)
	print(json.dumps({ "bpm": res["bpm"], "events": res["events"] }, indent=2))






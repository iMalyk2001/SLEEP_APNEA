from typing import List, Optional, Tuple, Dict

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks


def bandpass_filter(x: np.ndarray, fs: float, low_hz: float = 0.1, high_hz: float = 3.0, order: int = 4) -> np.ndarray:
	nyq = 0.5 * fs
	lo = max(1e-6, low_hz / nyq)
	hi = min(0.999, high_hz / nyq)
	b, a = butter(order, [lo, hi], btype="band")
	return filtfilt(b, a, x)


def smooth(x: np.ndarray, win_sec: float, fs: float) -> np.ndarray:
	w = max(1, int(round(win_sec * fs)))
	if w <= 1:
		return x
	k = np.ones(w, dtype=float) / w
	return np.convolve(x, k, mode="same")


def compute_bpm(
	samples: List[float],
	sample_rate_hz: float,
	min_bpm: float = 6.0,
	max_bpm: float = 60.0,
	prominence: float = 0.05,
	distance_sec: float = 0.8,
) -> Optional[Dict[str, object]]:
	"""Compute BPM, breath timestamps, and a confidence score.

	Returns dict: { bpm: float, breaths_ts_idx: List[int], confidence: float }
	Returns None if insufficient data.
	"""
	fs = float(sample_rate_hz)
	N = len(samples)
	if N < int(10 * fs):
		return None

	x = np.asarray(samples, dtype=float)
	x = x - np.nanmean(x)
	# Bandpass 0.1–3.0 Hz
	y = bandpass_filter(x, fs, 0.1, 3.0, order=4)
	# Smooth slightly
	ys = smooth(np.abs(y), win_sec=0.3, fs=fs)

	min_dist = int(max(distance_sec, 60.0 / max(max_bpm, 1.0)) * fs)
	peaks, props = find_peaks(ys, prominence=prominence * (np.nanmax(ys) + 1e-9), distance=min_dist)
	if len(peaks) < 2:
		return None

	ibis = np.diff(peaks) / fs
	bpm_vals = 60.0 / ibis
	bpm = float(np.nanmedian(bpm_vals))
	# Confidence: normalized peak prominence and consistency
	peak_prom = props.get("prominences", np.array([]))
	if peak_prom.size == 0:
		conf = 0.0
	else:
		norm_prom = float(np.nanmean(peak_prom) / (np.nanmax(ys) + 1e-9))
		consistency = float(1.0 / (1.0 + np.nanstd(bpm_vals)))
		conf = max(0.0, min(1.0, 0.5 * norm_prom + 0.5 * consistency))

	return {"bpm": bpm, "breaths_ts_idx": peaks.tolist(), "confidence": conf}



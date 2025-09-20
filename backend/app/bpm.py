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


# Peak-to-peak amplitude thresholding (defaults; tune as needed)
P2P_START_THRESHOLD_MV: float = 20.0  # require >= this to start
P2P_STOP_THRESHOLD_MV: float = 12.0   # drop below this to stop (hysteresis)
P2P_WINDOW_SEC: float = 5.0           # window length in seconds
P2P_REQUIRED_WINDOWS: int = 2         # consecutive windows above threshold


def evaluate_signal_presence(
	samples: List[float],
	sample_rate_hz: float,
	latest_ts_ms: int,
	prev_state: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
	"""Evaluate peak-to-peak signal presence with hysteresis over fixed windows.

	State keys:
	  - signal_ok: bool
	  - above_count: int  (consecutive windows above start threshold)
	  - last_window_index: Optional[int]  (integer index of last evaluated window)
	  - last_p2p_mv: float  (for diagnostics)
	"""
	fs = float(max(1.0, sample_rate_hz))
	window_len = int(max(1, round(P2P_WINDOW_SEC * fs)))
	window_ms = int(P2P_WINDOW_SEC * 1000.0)

	state: Dict[str, object] = {
		"signal_ok": False,
		"above_count": 0,
		"last_window_index": None,
		"last_p2p_mv": 0.0,
	}
	if isinstance(prev_state, dict):
		state.update(prev_state)

	if not samples:
		state.update({"signal_ok": False, "above_count": 0, "last_p2p_mv": 0.0})
		return state

	current_window_index = int(latest_ts_ms // max(1, window_ms))
	if state.get("last_window_index") == current_window_index:
		return state

	tail = samples[-window_len:] if len(samples) >= window_len else samples[:]
	clean = [float(v) for v in tail if isinstance(v, (int, float))]
	if not clean:
		state.update({"signal_ok": False, "above_count": 0, "last_window_index": current_window_index, "last_p2p_mv": 0.0})
		return state

	p2p = float(max(clean) - min(clean))
	signal_ok = bool(state.get("signal_ok", False))
	above_count = int(state.get("above_count", 0))

	if signal_ok:
		if p2p < P2P_STOP_THRESHOLD_MV:
			signal_ok = False
			above_count = 0
	else:
		if p2p >= P2P_START_THRESHOLD_MV:
			above_count += 1
			if above_count >= P2P_REQUIRED_WINDOWS:
				signal_ok = True
		else:
			above_count = 0

	state.update({
		"signal_ok": signal_ok,
		"above_count": above_count,
		"last_window_index": current_window_index,
		"last_p2p_mv": p2p,
	})
	return state



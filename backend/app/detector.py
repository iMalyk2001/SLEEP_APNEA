from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple

import numpy as np

from .dsp import ButterBandpassFilter, SlidingRMS, ema_update


@dataclass
class DetectorConfig:
    fs_hz: float = 250.0
    band_low_hz: float = 0.1
    band_high_hz: float = 10.0
    rms_window_sec: float = 1.0
    rms_update_ms: int = 100
    baseline_capture_sec: float = 45.0
    threshold_factor: float = 0.45
    ema_tau_sec: float = 60.0
    apnea_min_sec: float = 20.0
    hypopnea_min_sec: float = 10.0
    hypopnea_frac: float = 0.5
    recovery_sec: float = 3.0
    artifact_burst_factor: float = 3.0
    artifact_flatline_min_sec: float = 3.0
    snr_min: float = 5.0


@dataclass
class ChannelState:
    bp_filter: ButterBandpassFilter
    rms: SlidingRMS
    baseline_peak: float = 0.0
    baseline_ready: bool = False
    ema_peak: float = 0.0
    last_peak_ts_ms: Optional[int] = None


@dataclass
class DetectorState:
    cfg: DetectorConfig
    ch1: ChannelState
    ch2: ChannelState
    apnea_active: bool = False
    apnea_start_ms: Optional[int] = None
    hypopnea_active: bool = False
    hypopnea_start_ms: Optional[int] = None


def create_state(cfg: DetectorConfig) -> DetectorState:
    ch1 = ChannelState(ButterBandpassFilter(cfg.band_low_hz, cfg.band_high_hz, cfg.fs_hz, order=4), SlidingRMS(cfg.rms_window_sec, cfg.fs_hz))
    ch2 = ChannelState(ButterBandpassFilter(cfg.band_low_hz, cfg.band_high_hz, cfg.fs_hz, order=4), SlidingRMS(cfg.rms_window_sec, cfg.fs_hz))
    return DetectorState(cfg, ch1, ch2)


def _peak_envelope(filtered: np.ndarray, rms: SlidingRMS) -> Tuple[np.ndarray, np.ndarray]:
    env = rms.batch_update(filtered.tolist())
    env_arr = np.array(env, dtype=float)
    return filtered, env_arr


def _estimate_snr(x: np.ndarray) -> float:
    if x.size < 16:
        return 0.0
    p_signal = float(np.mean(x ** 2))
    noise = float(np.median(np.abs(x - np.median(x))))
    p_noise = noise * noise * 2.0
    if p_noise <= 1e-12:
        return 1e9
    return (p_signal / p_noise) if p_signal > 0 else 0.0


def process_block(state: DetectorState, ts_ms: int, ch1_mv: List[float], ch2_mv: List[float]) -> Dict:
    cfg = state.cfg
    x1 = np.array(ch1_mv, dtype=float)
    x2 = np.array(ch2_mv, dtype=float)

    y1 = state.ch1.bp_filter.apply(x1)
    y2 = state.ch2.bp_filter.apply(x2)

    _, env1 = _peak_envelope(y1, state.ch1.rms)
    _, env2 = _peak_envelope(y2, state.ch2.rms)

    if not state.ch1.baseline_ready:
        # Track baselines separately, but decisions will use channel 2 only
        state.ch1.baseline_peak = max(state.ch1.baseline_peak, float(np.max(env1)) if env1.size else 0.0)
        state.ch2.baseline_peak = max(state.ch2.baseline_peak, float(np.max(env2)) if env2.size else 0.0)
        if state.ch1.rms.window_n >= int(cfg.baseline_capture_sec * cfg.fs_hz):
            state.ch1.baseline_ready = True
            state.ch2.baseline_ready = True
            state.ch1.ema_peak = state.ch1.baseline_peak
            state.ch2.ema_peak = state.ch2.baseline_peak

    peak1 = float(np.max(env1)) if env1.size else 0.0
    peak2 = float(np.max(env2)) if env2.size else 0.0
    if state.ch1.baseline_ready:
        dt = max(1.0 / cfg.fs_hz, len(env1) / cfg.fs_hz)
        state.ch1.ema_peak = ema_update(state.ch1.ema_peak, peak1, dt_sec=dt, tau_sec=cfg.ema_tau_sec)
        state.ch2.ema_peak = ema_update(state.ch2.ema_peak, peak2, dt_sec=dt, tau_sec=cfg.ema_tau_sec)

    base1 = max(state.ch1.baseline_peak, 1e-6)
    base2 = max(state.ch2.baseline_peak, 1e-6)
    thr1 = cfg.threshold_factor * base1
    thr2 = cfg.threshold_factor * base2

    # Use only channel 2 for artifact determination
    artifact = False
    if peak2 > cfg.artifact_burst_factor * base2:
        artifact = True

    snr1 = _estimate_snr(y1)
    snr2 = _estimate_snr(y2)
    # Use only channel 2 SNR for gating
    low_snr = (snr2 < cfg.snr_min)

    def last_cross(env: np.ndarray, thr: float) -> Optional[int]:
        idx = None
        for i in range(env.size - 1, -1, -1):
            if env[i] >= thr:
                idx = i
                break
        if idx is None:
            return None
        return ts_ms - int(round((env.size - 1 - idx) * 1000.0 / cfg.fs_hz))

    # Track last crossing for ch2 only
    last1 = last_cross(env1, thr1)
    last2 = last_cross(env2, thr2)

    if last1 is not None:
        state.ch1.last_peak_ts_ms = last1
    if last2 is not None:
        state.ch2.last_peak_ts_ms = last2

    apnea_confirm = False
    suspect = False
    def no_peak_since(last_ts: Optional[int], now_ms: int, min_sec: float) -> bool:
        if last_ts is None:
            return True
        return (now_ms - last_ts) >= int(min_sec * 1000)

    # Determine apnea/hypopnea strictly from channel 2
    no1 = no_peak_since(state.ch1.last_peak_ts_ms, ts_ms, cfg.apnea_min_sec)
    no2 = no_peak_since(state.ch2.last_peak_ts_ms, ts_ms, cfg.apnea_min_sec)
    if no2:
        apnea_confirm = True

    events: List[Dict] = []
    if apnea_confirm and not state.apnea_active and not artifact and not low_snr:
        state.apnea_active = True
        state.apnea_start_ms = ts_ms - int(cfg.apnea_min_sec * 1000)
        events.append({
            "type": "apnea_start",
            "ts": state.apnea_start_ms,
            "suspect": False,
            "low_snr": low_snr,
            "artifact": artifact,
        })
    elif suspect and not state.apnea_active and not artifact and not low_snr:
        events.append({"type": "apnea_suspect", "ts": ts_ms, "low_snr": low_snr, "artifact": artifact})

    hypo1 = (peak1 < cfg.hypopnea_frac * base1)
    hypo2 = (peak2 < cfg.hypopnea_frac * base2)
    if (hypo2) and not state.hypopnea_active and not artifact and not low_snr:
        if state.hypopnea_start_ms is None:
            state.hypopnea_start_ms = ts_ms
        elif ts_ms - state.hypopnea_start_ms >= int(cfg.hypopnea_min_sec * 1000):
            state.hypopnea_active = True
            events.append({"type": "hypopnea_start", "ts": state.hypopnea_start_ms, "low_snr": low_snr, "artifact": artifact})
    else:
        state.hypopnea_start_ms = None

    if state.apnea_active:
        ok2 = (not no2)
        if ok2:
            start = state.apnea_start_ms or ts_ms
            events.append({
                "type": "apnea_end",
                "ts": ts_ms,
                "duration_ms": ts_ms - start,
            })
            state.apnea_active = False
            state.apnea_start_ms = None

    if state.hypopnea_active:
        ok2 = not hypo2
        if ok2:
            start = state.hypopnea_start_ms or ts_ms
            events.append({"type": "hypopnea_end", "ts": ts_ms, "duration_ms": ts_ms - start})
            state.hypopnea_active = False
            state.hypopnea_start_ms = None

    return {
        "env1_peak": peak1,
        "env2_peak": peak2,
        "thr1": thr1,
        "thr2": thr2,
        "baseline1": state.ch1.baseline_peak,
        "baseline2": state.ch2.baseline_peak,
        "ema1": state.ch1.ema_peak,
        "ema2": state.ch2.ema_peak,
        "artifact": artifact,
        "low_snr": low_snr,
        "snr1": snr1,
        "snr2": snr2,
        "events": events,
    }




from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from typing import Deque, List

import numpy as np
from scipy.signal import butter, sosfiltfilt


@dataclass
class CircularBuffer:
    maxlen: int

    def __post_init__(self) -> None:
        self.data: Deque[float] = deque(maxlen=self.maxlen)

    def extend(self, values: List[float]) -> None:
        self.data.extend(values)

    def append(self, value: float) -> None:
        self.data.append(value)

    def as_array(self) -> np.ndarray:
        if not self.data:
            return np.empty(0, dtype=float)
        return np.fromiter(self.data, dtype=float)


def design_butter_bandpass_sos(low_hz: float, high_hz: float, fs_hz: float, order: int = 4) -> np.ndarray:
    nyq = 0.5 * fs_hz
    low = max(1e-6, low_hz / nyq)
    high = min(0.999, high_hz / nyq)
    sos = butter(order, [low, high], btype="band", output="sos")
    return sos


class SlidingRMS:
    def __init__(self, window_seconds: float, fs_hz: float) -> None:
        self.fs_hz = float(fs_hz)
        self.window_n = max(1, int(round(window_seconds * fs_hz)))
        self.buf = deque(maxlen=self.window_n)
        self.sum_sq = 0.0

    def update(self, value: float) -> float:
        v = float(value)
        if len(self.buf) == self.window_n:
            oldest = self.buf[0]
            self.sum_sq -= oldest * oldest
        self.buf.append(v)
        self.sum_sq += v * v
        n = len(self.buf)
        return (self.sum_sq / max(1, n)) ** 0.5

    def batch_update(self, values: List[float]) -> List[float]:
        out: List[float] = []
        for v in values:
            out.append(self.update(v))
        return out


class ButterBandpassFilter:
    def __init__(self, low_hz: float = 0.1, high_hz: float = 10.0, fs_hz: float = 250.0, order: int = 4) -> None:
        self.fs_hz = fs_hz
        self.sos = design_butter_bandpass_sos(low_hz, high_hz, fs_hz, order)

    def apply(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        try:
            padlen = min(3 * (self.sos.shape[0]), max(0, x.size - 1))
            return sosfiltfilt(self.sos, x, padlen=padlen)
        except Exception:
            return x


def ema_update(prev: float, value: float, dt_sec: float, tau_sec: float) -> float:
    if tau_sec <= 0:
        return value
    alpha = 1.0 - np.exp(-dt_sec / tau_sec)
    return (1.0 - alpha) * prev + alpha * value




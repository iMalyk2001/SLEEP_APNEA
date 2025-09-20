from typing import Optional, List, Dict, Deque
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from collections import deque
import logging

from .database import get_db
from .models import User, Sample as SampleModel
from .schemas import SampleIn, BatchSamplesIn
from .ws_manager import UserConnectionManager
from .bpm import compute_bpm, evaluate_signal_presence
from .detector import DetectorConfig, create_state, process_block
from .models import User, Sample as SampleModel, Event
from .dsp import CircularBuffer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])

manager: UserConnectionManager = UserConnectionManager()

# In-memory per-user buffer of last 60 seconds of samples
user_buffers: Dict[int, Deque[dict]] = {}
# Per-user signal presence state for hysteresis/windowing
signal_states: Dict[int, dict] = {}
# Per-user DSP detector state and circular buffers for channels
detect_states: Dict[int, object] = {}
ch1_cb: Dict[int, CircularBuffer] = {}
ch2_cb: Dict[int, CircularBuffer] = {}


def _get_user_by_device_key(db: Session, device_key: str) -> User:
	user = db.query(User).filter(User.device_key == device_key).first()
	if not user:
		logger.warning(f"Invalid device key: {device_key}")
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device key")
	return user


def _get_user_buffer(user_id: int) -> Deque[dict]:
	if user_id not in user_buffers:
		user_buffers[user_id] = deque()
	return user_buffers[user_id]


def _prune_old_samples(buf: Deque[dict], latest_ts_ms: int) -> None:
	cutoff = latest_ts_ms - 120_000
	while buf and (buf[0].get("timestamp_ms") or buf[0].get("timestamp") or 0) < cutoff:
		buf.popleft()


def _estimate_sample_rate_hz(buf: Deque[dict]) -> float:
	"""Estimate sample rate from recent timestamps (ms)."""
	if not buf or len(buf) < 5:
		return 0.0
	# Use last ~3 seconds for stability
	latest_ts = buf[-1].get("timestamp_ms") or buf[-1].get("timestamp") or 0
	cutoff = latest_ts - 3_000
	recent = [s for s in buf if (s.get("timestamp_ms") or s.get("timestamp") or 0) >= cutoff]
	if len(recent) < 5:
		recent = list(buf)
	# Collect consecutive deltas
	times = []
	for s in recent:
		t = s.get("timestamp_ms") or s.get("timestamp") or 0
		if isinstance(t, (int, float)):
			times.append(float(t))
	if len(times) < 5:
		return 0.0
	dts = [max(1.0, t2 - t1) for t1, t2 in zip(times[:-1], times[1:]) if isinstance(t1, float) and isinstance(t2, float)]
	if not dts:
		return 0.0
	# Median dt for robustness
	dts_sorted = sorted(dts)
	median_dt = dts_sorted[len(dts_sorted)//2]
	if median_dt <= 0:
		return 0.0
	return 1000.0 / median_dt


@router.post("/")
async def ingest_sample(
	payload: SampleIn,
	x_device_key: Optional[str] = Header(None, alias="X-Device-Key"),
	db: Session = Depends(get_db),
):
	if not x_device_key:
		logger.warning("Missing X-Device-Key header")
		raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Device-Key header")
	user = _get_user_by_device_key(db, x_device_key)
	# Normalize keys for single sample broadcast (frontend expects timestamp, sensor1/2/3)
	sample_out = {
		"timestamp": payload.timestamp_ms,
		"timestamp_ms": payload.timestamp_ms,
		"sensor1": payload.sensor1_mV,
		"sensor1_mV": payload.sensor1_mV,
		"sensor2": payload.sensor2_mV,
		"sensor2_mV": payload.sensor2_mV,
		"sensor3": payload.sensor3,
	}
	# Append to per-user buffer and prune to last 60s
	buf = _get_user_buffer(user.id)
	buf.append(sample_out)
	_prune_old_samples(buf, payload.timestamp_ms)
	# Estimate fs and compute BPM on sensor2 with signal gating
	fs = _estimate_sample_rate_hz(buf)
	bpm_payload = None
	if fs >= 1.0:
		# Use recent window of sensor2
		values = [s.get("sensor2") for s in buf if isinstance(s.get("sensor2"), (int, float))]
		latest_ts = int(sample_out["timestamp_ms"])  # current sample timestamp
		# Evaluate signal presence with per-user state
		prev_state = signal_states.get(user.id)
		sig_state = evaluate_signal_presence(values, fs, latest_ts, prev_state)
		signal_states[user.id] = sig_state
		res = compute_bpm(values, fs)
		bpm_val = float(res["bpm"]) if (res and sig_state.get("signal_ok")) else 0.0
		bpm_payload = {"type": "bpm", "bpm": bpm_val, "signal_ok": bool(sig_state.get("signal_ok", False)), "confidence": (res.get("confidence", 0.0) if res else 0.0)}
	# Broadcast raw sample and, if available, BPM
	await manager.broadcast_to_user(user.id, {"type": "sample", **{k: sample_out[k] for k in ("timestamp","sensor1","sensor2","sensor3")}})
	if bpm_payload:
		await manager.broadcast_to_user(user.id, bpm_payload)
	# Include bpm and signal_ok in HTTP response
	resp_bpm = float(bpm_payload.get("bpm", 0.0)) if bpm_payload else 0.0
	resp_signal = bool(bpm_payload.get("signal_ok", False)) if bpm_payload else False
	return {"status": "ok", "bpm": resp_bpm, "signal_ok": resp_signal}


@router.post("")
async def ingest_sample_no_slash(
	payload: SampleIn,
	x_device_key: Optional[str] = Header(None, alias="X-Device-Key"),
	db: Session = Depends(get_db),
):
	return await ingest_sample(payload, x_device_key, db)


@router.post("/batch")
async def ingest_batch(
	payload: BatchSamplesIn,
	x_device_key: Optional[str] = Header(None, alias="X-Device-Key"),
	db: Session = Depends(get_db),
):
	logger.info(f"Received batch payload: {payload.dict() if hasattr(payload, 'dict') else payload}")
	if not x_device_key:
		logger.warning("Missing X-Device-Key header")
		raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Device-Key header")
	user = _get_user_by_device_key(db, x_device_key)
	buf = _get_user_buffer(user.id)
	# Initialize detector state and circular buffers per user
	if user.id not in detect_states:
		cfg = DetectorConfig(fs_hz=250.0)
		detect_states[user.id] = create_state(cfg)
		ch1_cb[user.id] = CircularBuffer(int(120 * cfg.fs_hz))
		ch2_cb[user.id] = CircularBuffer(int(120 * cfg.fs_hz))
	count = 0
	latest_ts = 0
	for s in payload.samples:
		# s is now a SampleIn object with fields timestamp_ms, sensor1_mV, etc.
		ts_ms = s.timestamp_ms
		s1_mv = s.sensor1_mV
		s2_mv = s.sensor2_mV

		# Store data with all key variants for compatibility
		sample_out = {
			"timestamp": ts_ms,
			"timestamp_ms": ts_ms,
			"sensor1": s1_mv,
			"sensor1_mV": s1_mv,
			"sensor2": s2_mv,
			"sensor2_mV": s2_mv,
			"sensor3": s.sensor3,
		}
		latest_ts = max(latest_ts, ts_ms)
		buf.append(sample_out)
		# Append to DSP circular buffers
		ch1_cb[user.id].append(float(s1_mv))
		ch2_cb[user.id].append(float(s2_mv))
		count += 1
		# Broadcast raw sample to frontend
		await manager.broadcast_to_user(user.id, {"type": "sample", "timestamp": ts_ms, "sensor1": s1_mv, "sensor2": s2_mv, "sensor3": s.sensor3})
	if latest_ts:
		_prune_old_samples(buf, latest_ts)
	# Persist batch to DB
	rows = []
	for s in payload.samples:
		rows.append(SampleModel(
			user_id=user.id,
			timestamp_ms=s.timestamp_ms,
			sensor1_mV=s.sensor1_mV,
			sensor2_mV=s.sensor2_mV,
			sensor3=s.sensor3,
		))
	if rows:
		db.add_all(rows)
		db.commit()
	# After batch append, compute BPM once using buffer with signal gating (sensor2 only)
	fs = _estimate_sample_rate_hz(buf)
	bpm_payload = None
	if fs >= 1.0:
		values = [s.get("sensor2") for s in buf if isinstance(s.get("sensor2"), (int, float))]
		prev_state = signal_states.get(user.id)
		sig_state = evaluate_signal_presence(values, fs, int(latest_ts), prev_state)
		signal_states[user.id] = sig_state
		res = compute_bpm(values, fs)
		bpm_val = float(res["bpm"]) if (res and sig_state.get("signal_ok")) else 0.0
		bpm_payload = {"type": "bpm", "bpm": bpm_val, "signal_ok": bool(sig_state.get("signal_ok", False)), "confidence": (res.get("confidence", 0.0) if res else 0.0)}
	if bpm_payload:
		await manager.broadcast_to_user(user.id, bpm_payload)

	# Run apnea/hypopnea detection on a ~100 ms block
	if user.id in detect_states and latest_ts:
		det_state = detect_states[user.id]
		fs = det_state.cfg.fs_hz
		block_n = int(max(1, round(0.1 * fs)))
		x1 = ch1_cb[user.id].as_array()
		x2 = ch2_cb[user.id].as_array()
		if x1.size >= block_n and x2.size >= block_n:
			blk1 = x1[-block_n:].tolist()
			blk2 = x2[-block_n:].tolist()
			det = process_block(det_state, int(latest_ts), blk1, blk2)
			# Broadcast metrics for debugging
			await manager.broadcast_to_user(user.id, {
				"type": "env_metrics",
				"ts": int(latest_ts),
				"env1_peak": det["env1_peak"],
				"env2_peak": det["env2_peak"],
				"thr1": det["thr1"],
				"thr2": det["thr2"],
				"artifact": det["artifact"],
				"low_snr": det["low_snr"],
			})
			for ev in det.get("events", []):
				if ev["type"].endswith("_start"):
					await manager.broadcast_to_user(user.id, {"type": ev["type"], "ts": ev["ts"], "suspect": ev.get("suspect", False)})
				elif ev["type"].endswith("_end"):
					ts_end = ev["ts"]
					duration_ms = int(ev.get("duration_ms", 0))
					ts_start = ts_end - duration_ms
					meta = {
						"ts_start": ts_start,
						"ts_end": ts_end,
						"duration_s": duration_ms / 1000.0,
						"event_type": ev["type"].replace("_end", ""),
						"channels": ["AIN1"],
						"baseline_peak": det.get("baseline2", 0.0),
						"threshold_factor": 0.45,
						"sample_rate": 250,
						"pga": "+-2.048V",
						"artifact_flag": bool(det.get("artifact", False))
					}
					db.add(Event(
						user_id=user.id,
						ts_start_ms=ts_start,
						ts_end_ms=ts_end,
						duration_s=duration_ms / 1000.0,
						event_type=meta["event_type"],
						channels=",".join(meta["channels"]),
						baseline_peak=meta["baseline_peak"],
						threshold_factor=meta["threshold_factor"],
						sample_rate=meta["sample_rate"],
						pga=meta["pga"],
						artifact_flag=str(meta["artifact_flag"]),
						meta=meta,
					))
					db.commit()
					await manager.broadcast_to_user(user.id, {"type": ev["type"], **meta})
	# Include bpm and signal_ok in HTTP response
	resp_bpm = float(bpm_payload.get("bpm", 0.0)) if bpm_payload else 0.0
	resp_signal = bool(bpm_payload.get("signal_ok", False)) if bpm_payload else False
	return {"status": "ok", "count": count, "bpm": resp_bpm, "signal_ok": resp_signal}


@router.post("/batch/")
async def ingest_batch_trailing_slash(
	payload: BatchSamplesIn,
	x_device_key: Optional[str] = Header(None, alias="X-Device-Key"),
	db: Session = Depends(get_db),
):
	return await ingest_batch(payload, x_device_key, db)
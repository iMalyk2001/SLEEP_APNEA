from typing import Optional, List, Dict, Deque
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from collections import deque
import logging

from .database import get_db
from .models import User
from .schemas import SampleIn, BatchSamplesIn
from .ws_manager import UserConnectionManager
from .bpm import compute_bpm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])

manager: UserConnectionManager = UserConnectionManager()

# In-memory per-user buffer of last 60 seconds of samples
user_buffers: Dict[int, Deque[dict]] = {}


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
	cutoff = latest_ts_ms - 60_000
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
	# Estimate fs and compute BPM on sensor1
	fs = _estimate_sample_rate_hz(buf)
	bpm_payload = None
	if fs >= 1.0:
		# Use recent window of sensor1
		values = [s.get("sensor1") for s in buf if isinstance(s.get("sensor1"), (int, float))]
		res = compute_bpm(values, fs)
		if res:
			bpm_payload = {"type": "bpm", "bpm": res["bpm"], "confidence": res.get("confidence", 0.0)}
	# Broadcast raw sample and, if available, BPM
	await manager.broadcast_to_user(user.id, {"type": "sample", **{k: sample_out[k] for k in ("timestamp","sensor1","sensor2","sensor3")}})
	if bpm_payload:
		await manager.broadcast_to_user(user.id, bpm_payload)
	return {"status": "ok"}


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
		count += 1
		# Broadcast raw sample to frontend
		await manager.broadcast_to_user(user.id, {"type": "sample", "timestamp": ts_ms, "sensor1": s1_mv, "sensor2": s2_mv, "sensor3": s.sensor3})
	if latest_ts:
		_prune_old_samples(buf, latest_ts)
	# After batch append, compute BPM once using buffer
	fs = _estimate_sample_rate_hz(buf)
	bpm_payload = None
	if fs >= 1.0:
		values = [s.get("sensor1") for s in buf if isinstance(s.get("sensor1"), (int, float))]
		res = compute_bpm(values, fs)
		if res:
			bpm_payload = {"type": "bpm", "bpm": res["bpm"], "confidence": res.get("confidence", 0.0)}
	if bpm_payload:
		await manager.broadcast_to_user(user.id, bpm_payload)
	return {"status": "ok", "count": count}


@router.post("/batch/")
async def ingest_batch_trailing_slash(
	payload: BatchSamplesIn,
	x_device_key: Optional[str] = Header(None, alias="X-Device-Key"),
	db: Session = Depends(get_db),
):
	return await ingest_batch(payload, x_device_key, db)
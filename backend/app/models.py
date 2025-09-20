from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Float, BigInteger, ForeignKey, Index, JSON

from .database import Base


class User(Base):
	__tablename__ = "users"

	id = Column(Integer, primary_key=True, index=True)
	username = Column(String(100), unique=True, nullable=False, index=True)
	hashed_password = Column(String(255), nullable=False)
	device_key = Column(String(128), unique=True, nullable=False, index=True)
	created_at = Column(DateTime, default=datetime.utcnow, nullable=False)



class Sample(Base):
	__tablename__ = "samples"

	id = Column(Integer, primary_key=True)
	user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
	timestamp_ms = Column(BigInteger, index=True, nullable=False)
	sensor1_mV = Column(Float, nullable=False)
	sensor2_mV = Column(Float, nullable=False)
	sensor3 = Column(Float, nullable=True)
	created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

# Composite index to accelerate per-user time queries
Index("ix_samples_user_ts", Sample.user_id, Sample.timestamp_ms)


class Event(Base):
	__tablename__ = "events"

	id = Column(Integer, primary_key=True)
	user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
	ts_start_ms = Column(BigInteger, index=True, nullable=False)
	ts_end_ms = Column(BigInteger, index=True, nullable=False)
	duration_s = Column(Float, nullable=False)
	event_type = Column(String(32), nullable=False)  # apnea_start/end, hypopnea_start/end
	channels = Column(String(32), nullable=False)   # e.g., "AIN0,AIN1"
	baseline_peak = Column(Float, nullable=True)
	threshold_factor = Column(Float, nullable=True)
	sample_rate = Column(Float, nullable=True)
	pga = Column(String(16), nullable=True)
	artifact_flag = Column(String(8), nullable=True)
	meta = Column(JSON, nullable=True)
	created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

Index("ix_events_user_ts", Event.user_id, Event.ts_start_ms)

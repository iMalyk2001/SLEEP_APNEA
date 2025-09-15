from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel, Field, model_validator


class UserCreate(BaseModel):
	username: str = Field(min_length=3, max_length=100)
	password: str = Field(min_length=6, max_length=128)


class Token(BaseModel):
	access_token: str
	token_type: str = "bearer"
	device_key: Optional[str] = None


class SampleIn(BaseModel):
	timestamp_ms: int
	sensor1_mV: float
	sensor2_mV: float
	sensor3: Optional[float] = None

	# Accept legacy field names from frontend/device: timestamp, sensor1, sensor2
	@model_validator(mode='before')
	@classmethod
	def _normalize_legacy_fields(cls, data):
		if isinstance(data, dict):
			if 'timestamp_ms' not in data and 'timestamp' in data:
				data['timestamp_ms'] = data['timestamp']
			if 'sensor1_mV' not in data and 'sensor1' in data:
				data['sensor1_mV'] = data['sensor1']
			if 'sensor2_mV' not in data and 'sensor2' in data:
				data['sensor2_mV'] = data['sensor2']
		return data

	model_config = {
		"extra": "ignore",
	}


class BatchSamplesIn(BaseModel):
	samples: List[SampleIn]





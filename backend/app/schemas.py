from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel, Field


class UserCreate(BaseModel):
	username: str = Field(min_length=3, max_length=100)
	password: str = Field(min_length=6, max_length=128)


class Token(BaseModel):
	access_token: str
	token_type: str = "bearer"
	device_key: Optional[str] = None


class SampleIn(BaseModel):
	timestamp: int
	sensor1: float
	sensor2: float
	sensor3: Optional[float] = None


class BatchSamplesIn(BaseModel):
	samples: List[SampleIn]

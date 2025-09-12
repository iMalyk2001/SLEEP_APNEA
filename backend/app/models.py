from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime

from .database import Base


class User(Base):
	__tablename__ = "users"

	id = Column(Integer, primary_key=True, index=True)
	username = Column(String(100), unique=True, nullable=False, index=True)
	hashed_password = Column(String(255), nullable=False)
	device_key = Column(String(128), unique=True, nullable=False, index=True)
	created_at = Column(DateTime, default=datetime.utcnow, nullable=False)




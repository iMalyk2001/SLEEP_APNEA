from typing import Optional, List
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .database import get_db
from .models import User
from .schemas import SampleIn, BatchSamplesIn
from .ws_manager import UserConnectionManager

router = APIRouter(prefix="/ingest", tags=["ingest"])

manager: UserConnectionManager = UserConnectionManager()


def _get_user_by_device_key(db: Session, device_key: str) -> User:
	user = db.query(User).filter(User.device_key == device_key).first()
	if not user:
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device key")
	return user


@router.post("")
async def ingest_sample(
	payload: SampleIn,
	x_device_key: Optional[str] = Header(None, alias="X-Device-Key"),
	db: Session = Depends(get_db),
):
	if not x_device_key:
		raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Device-Key header")
	user = _get_user_by_device_key(db, x_device_key)
	await manager.broadcast_to_user(user.id, payload.model_dump())
	return {"status": "ok"}


@router.post("/batch")
async def ingest_batch(
	payload: BatchSamplesIn,
	x_device_key: Optional[str] = Header(None, alias="X-Device-Key"),
	db: Session = Depends(get_db),
):
	if not x_device_key:
		raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Device-Key header")
	user = _get_user_by_device_key(db, x_device_key)
	for s in payload.samples:
		await manager.broadcast_to_user(user.id, s.model_dump())
	return {"status": "ok", "count": len(payload.samples)}




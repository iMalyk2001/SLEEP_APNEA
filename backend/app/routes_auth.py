from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
import secrets
import logging

from .database import get_db
from .models import User
from .schemas import UserCreate, Token
from .auth import create_access_token, get_password_hash, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def generate_unique_device_key(db: Session, max_tries: int = 5) -> str:
	for _ in range(max_tries):
		candidate = secrets.token_urlsafe(24)
		if not db.query(User).filter(User.device_key == candidate).first():
			return candidate
	raise RuntimeError("Unable to generate unique device key")


@router.post("/register", response_model=Token)
def register(user_in: UserCreate, db: Session = Depends(get_db)):
	try:
		if db.query(User).filter(User.username == user_in.username).first():
			raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already exists")
		device_key = generate_unique_device_key(db)
		hashed = get_password_hash(user_in.password)
		user = User(username=user_in.username, hashed_password=hashed, device_key=device_key)
		db.add(user)
		db.commit()
		db.refresh(user)
		token = create_access_token({"sub": user.username})
		return {"access_token": token, "token_type": "bearer", "device_key": device_key}  # type: ignore
	except HTTPException:
		raise
	except Exception as e:
		logger.exception("Register failed")
		raise HTTPException(status_code=500, detail=f"Register failed: {type(e).__name__}")


@router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
	user = db.query(User).filter(User.username == form_data.username).first()
	if not user or not verify_password(form_data.password, user.hashed_password):
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
	token = create_access_token({"sub": user.username})
	return {"access_token": token, "token_type": "bearer", "device_key": user.device_key}  # type: ignore



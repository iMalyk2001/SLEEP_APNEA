import os
import logging
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .routes_auth import router as auth_router
from .routes_ingest import router as ingest_router, manager as ws_manager
from .routes_ingest import _get_user_by_device_key
from .auth import decode_token

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Breathing Monitor Backend")

# CORS (allow frontend served by same app; also allow localhost during dev)
app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

Base.metadata.create_all(bind=engine)

# Routers
app.include_router(auth_router)
app.include_router(ingest_router)

# Static frontend
FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def root_index():
	index_path = FRONTEND_DIR / "index.html"
	return FileResponse(str(index_path))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str, db: Session = Depends(get_db)):
	# Token is passed as query param ?token=
	try:
		payload = decode_token(token)
		username = payload.get("sub")
		if not username:
			await websocket.close(code=4401)
			return
		from .models import User
		user = db.query(User).filter(User.username == username).first()
		if not user:
			await websocket.close(code=4401)
			return
	except Exception:
		await websocket.close(code=4401)
		return

	# Accept and register connection once
	await ws_manager.connect(user.id, websocket)
	try:
		while True:
			_ = await websocket.receive_text()
			await websocket.send_json({"type": "pong"})
	except WebSocketDisconnect:
		ws_manager.disconnect(user.id, websocket)
	except Exception:
		ws_manager.disconnect(user.id, websocket)
		try:
			await websocket.close()
		except Exception:
			pass


@app.websocket("/ws/device")
async def device_websocket(websocket: WebSocket, key: str, db: Session = Depends(get_db)):
	# Authenticate device by device key
	try:
		user = _get_user_by_device_key(db, key)
	except Exception:
		await websocket.close(code=4401)
		return

	# Accept connection and broadcast presence
	await websocket.accept()
	try:
		await ws_manager.broadcast_to_user(user.id, {"type": "device_online"})
		# Keep the connection open, receive heartbeats
		while True:
			_ = await websocket.receive_text()
	except WebSocketDisconnect:
		await ws_manager.broadcast_to_user(user.id, {"type": "device_offline"})
	except Exception:
		await ws_manager.broadcast_to_user(user.id, {"type": "device_offline"})
		try:
			await websocket.close()
		except Exception:
			pass


if __name__ == "__main__":
	import uvicorn
	uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
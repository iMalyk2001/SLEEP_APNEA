from typing import Dict, Set
from fastapi import WebSocket


class UserConnectionManager:
	def __init__(self) -> None:
		self.user_connections: Dict[int, Set[WebSocket]] = {}

	async def connect(self, user_id: int, websocket: WebSocket) -> None:
		await websocket.accept()
		if user_id not in self.user_connections:
			self.user_connections[user_id] = set()
		self.user_connections[user_id].add(websocket)

	def disconnect(self, user_id: int, websocket: WebSocket) -> None:
		if user_id in self.user_connections and websocket in self.user_connections[user_id]:
			self.user_connections[user_id].remove(websocket)
			if not self.user_connections[user_id]:
				del self.user_connections[user_id]

	async def broadcast_to_user(self, user_id: int, message: dict) -> None:
		if user_id not in self.user_connections:
			return
		dead: Set[WebSocket] = set()
		for ws in self.user_connections[user_id]:
			try:
				await ws.send_json(message)
			except Exception:
				dead.add(ws)
		for ws in dead:
			self.user_connections[user_id].discard(ws)
		if user_id in self.user_connections and not self.user_connections[user_id]:
			del self.user_connections[user_id]




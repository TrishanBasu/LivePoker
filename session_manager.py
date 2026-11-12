import secrets
import threading
import time
from typing import Dict, Optional, Any

# Unique ID per server process boot; changes if Render restarts the container
SERVER_BOOT_ID = secrets.token_hex(12)


class PlayerSession:
    def __init__(self, session_id: str, token: str, display_name: str, room_code: str):
        self.session_id: str = session_id
        self.token: str = token
        self.display_name: str = display_name
        self.room_code: str = room_code
        self.table_id: Optional[str] = None
        self.socket_sid: Optional[str] = None
        self.connected: bool = True
        self.created_at: float = time.time()
        self.last_heartbeat: float = time.time()
        self.disconnected_at: Optional[float] = None


class SessionManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions_by_id: Dict[str, PlayerSession] = {}
        self.sessions_by_token: Dict[str, PlayerSession] = {}
        self.sid_to_session_id: Dict[str, str] = {}

    def create_session(self, display_name: str, room_code: str) -> PlayerSession:
        with self.lock:
            session_id = secrets.token_hex(12)
            token = secrets.token_urlsafe(32)
            sess = PlayerSession(
                session_id=session_id,
                token=token,
                display_name=display_name,
                room_code=room_code,
            )
            self.sessions_by_id[session_id] = sess
            self.sessions_by_token[token] = sess
            return sess

    def get_by_token(self, token: Optional[str]) -> Optional[PlayerSession]:
        if not token:
            return None
        with self.lock:
            return self.sessions_by_token.get(token)

    def get_by_id(self, session_id: Optional[str]) -> Optional[PlayerSession]:
        if not session_id:
            return None
        with self.lock:
            return self.sessions_by_id.get(session_id)

    def get_by_sid(self, sid: str) -> Optional[PlayerSession]:
        with self.lock:
            session_id = self.sid_to_session_id.get(sid)
            if not session_id:
                return None
            return self.sessions_by_id.get(session_id)

    def bind_socket(self, token: str, sid: str) -> Optional[PlayerSession]:
        with self.lock:
            sess = self.sessions_by_token.get(token)
            if not sess:
                return None
            # Remove stale sid mapping if reconnecting from a refreshed tab
            if sess.socket_sid and sess.socket_sid != sid:
                self.sid_to_session_id.pop(sess.socket_sid, None)

            sess.socket_sid = sid
            sess.connected = True
            sess.disconnected_at = None
            sess.last_heartbeat = time.time()
            self.sid_to_session_id[sid] = sess.session_id
            return sess

    def unbind_socket(self, sid: str) -> Optional[PlayerSession]:
        with self.lock:
            session_id = self.sid_to_session_id.pop(sid, None)
            if not session_id:
                return None
            sess = self.sessions_by_id.get(session_id)
            if sess and sess.socket_sid == sid:
                sess.socket_sid = None
                sess.connected = False
                sess.disconnected_at = time.time()
            return sess

    def record_heartbeat(self, sid: str, token: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            sess = self.get_by_sid(sid)
            if not sess and token:
                sess = self.get_by_token(token)
            if not sess:
                return {"ok": False, "boot_id": SERVER_BOOT_ID}
            sess.last_heartbeat = time.time()
            return {
                "ok": True,
                "boot_id": SERVER_BOOT_ID,
                "session_id": sess.session_id,
                "room_code": sess.room_code,
                "table_id": sess.table_id,
                "server_time": time.time(),
            }

    def invalidate_session(self, session_id: str) -> None:
        with self.lock:
            sess = self.sessions_by_id.pop(session_id, None)
            if sess:
                self.sessions_by_token.pop(sess.token, None)
                if sess.socket_sid:
                    self.sid_to_session_id.pop(sess.socket_sid, None)

    def active_session_count(self) -> int:
        with self.lock:
            return len(self.sessions_by_id)

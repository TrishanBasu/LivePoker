import secrets
import threading
import time
from typing import Dict, Optional, Any, Callable, List
from game_engine import PokerTable, MAX_SEATS


class PrivateRoom:
    def __init__(self, room_code: str, room_name: str, host_session_id: str):
        self.room_code: str = room_code
        self.room_name: str = room_name
        self.host_session_id: str = host_session_id
        self.created_at: float = time.time()
        self.tables: Dict[str, PokerTable] = {}
        self.member_sessions: Dict[str, str] = {}  # session_id -> display_name


class RoomManager:
    def __init__(
        self,
        on_table_updated: Optional[Callable[[str, str], None]] = None,
        on_room_updated: Optional[Callable[[str], None]] = None,
        on_player_evicted: Optional[Callable[[str, str], None]] = None,
    ):
        self.lock = threading.RLock()
        self.rooms: Dict[str, PrivateRoom] = {}
        self._on_table_updated = on_table_updated
        self._on_room_updated = on_room_updated
        self._on_player_evicted = on_player_evicted

    def _generate_room_code(self) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            if code not in self.rooms:
                return code

    def create_room(self, room_name: str, host_session_id: str, host_name: str) -> PrivateRoom:
        with self.lock:
            code = self._generate_room_code()
            clean_name = (room_name or f"{host_name}'s Poker Club").strip()[:40]
            room = PrivateRoom(
                room_code=code,
                room_name=clean_name,
                host_session_id=host_session_id,
            )
            room.member_sessions[host_session_id] = host_name
            self.rooms[code] = room

            # Automatically create Table 1 so the room is ready immediately
            self._create_table_internal(room, "Table 1", 1000, 10, 20)
            return room

    def get_room(self, room_code: str) -> Optional[PrivateRoom]:
        if not room_code:
            return None
        with self.lock:
            return self.rooms.get(room_code.strip().upper())

    def register_member(self, room_code: str, session_id: str, display_name: str) -> bool:
        with self.lock:
            room = self.get_room(room_code)
            if not room:
                return False
            room.member_sessions[session_id] = display_name
            if self._on_room_updated:
                self._on_room_updated(room.room_code)
            return True

    def unregister_member(self, room_code: str, session_id: str) -> None:
        with self.lock:
            room = self.get_room(room_code)
            if not room:
                return
            for table in list(room.tables.values()):
                table.remove_player(session_id, reason="left the room")
            room.member_sessions.pop(session_id, None)

            # If host deliberately leaves and other members remain, transfer host cleanly
            if room.host_session_id == session_id and room.member_sessions:
                next_host = next(iter(room.member_sessions.keys()))
                room.host_session_id = next_host

            if not room.member_sessions:
                for t in room.tables.values():
                    t.close_table()
                self.rooms.pop(room.room_code, None)
            elif self._on_room_updated:
                self._on_room_updated(room.room_code)

    def _create_table_internal(
        self,
        room: PrivateRoom,
        table_name: str,
        starting_chips: int,
        small_blind: int,
        big_blind: int,
    ) -> PokerTable:
        table_id = f"tbl_{secrets.token_hex(6)}"

        def table_changed_cb(tid: str) -> None:
            if self._on_table_updated:
                self._on_table_updated(room.room_code, tid)
            if self._on_room_updated:
                self._on_room_updated(room.room_code)

        def player_removed_cb(tid: str, sid: str) -> None:
            if self._on_player_evicted:
                self._on_player_evicted(tid, sid)

        table = PokerTable(
            table_id=table_id,
            room_code=room.room_code,
            name=table_name.strip()[:32] or f"Table {len(room.tables) + 1}",
            starting_chips=starting_chips,
            small_blind=small_blind,
            big_blind=big_blind,
            on_state_change=table_changed_cb,
            on_player_removed=player_removed_cb,
        )
        room.tables[table_id] = table
        return table

    def create_table(
        self,
        room_code: str,
        actor_session_id: str,
        table_name: str,
        starting_chips: int = 1000,
        small_blind: int = 10,
        big_blind: int = 20,
    ) -> Dict[str, Any]:
        with self.lock:
            room = self.get_room(room_code)
            if not room:
                return {"ok": False, "error": "Room does not exist."}
            if room.host_session_id != actor_session_id:
                return {"ok": False, "error": "Only the room host can create tables."}
            if len(room.tables) >= 12:
                return {"ok": False, "error": "Maximum 12 concurrent tables per room."}
            if starting_chips < 50 or small_blind < 1 or big_blind <= small_blind:
                return {"ok": False, "error": "Invalid starting chips or blinds."}

            table = self._create_table_internal(room, table_name, starting_chips, small_blind, big_blind)
            if self._on_room_updated:
                self._on_room_updated(room.room_code)
            return {"ok": True, "table_id": table.table_id}

    def close_table(self, room_code: str, actor_session_id: str, table_id: str) -> Dict[str, Any]:
        with self.lock:
            room = self.get_room(room_code)
            if not room:
                return {"ok": False, "error": "Room not found."}
            if room.host_session_id != actor_session_id:
                return {"ok": False, "error": "Only the room host can close tables."}
            table = room.tables.pop(table_id, None)
            if not table:
                return {"ok": False, "error": "Table not found."}
            table.close_table()
            if self._on_room_updated:
                self._on_room_updated(room.room_code)
            return {"ok": True}

    def get_table(self, room_code: str, table_id: str) -> Optional[PokerTable]:
        with self.lock:
            room = self.get_room(room_code)
            if not room:
                return None
            return room.tables.get(table_id)

    def serialize_room_lobby(self, room_code: str, viewer_session_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            room = self.get_room(room_code)
            if not room:
                return None

            tables_list = []
            for tid, tbl in room.tables.items():
                with tbl.lock:
                    seated = tbl.seated_players()
                    tables_list.append({
                        "table_id": tid,
                        "name": tbl.name,
                        "player_count": len(seated),
                        "max_players": MAX_SEATS,
                        "starting_chips": tbl.starting_chips,
                        "small_blind": tbl.small_blind,
                        "big_blind": tbl.big_blind,
                        "phase": tbl.phase,
                        "game_started": tbl.game_started,
                        "players": [
                            {
                                "name": p.name,
                                "chips": p.chips,
                                "seat": p.seat + 1,
                                "connected": p.connected,
                            }
                            for p in seated
                        ],
                    })

            return {
                "room_code": room.room_code,
                "room_name": room.room_name,
                "is_host": room.host_session_id == viewer_session_id,
                "host_name": room.member_sessions.get(room.host_session_id, "Host"),
                "member_count": len(room.member_sessions),
                "tables": tables_list,
            }

    def sweep_disconnected_players(self) -> List[str]:
        evicted_all: List[str] = []
        with self.lock:
            for room in list(self.rooms.values()):
                for table in list(room.tables.values()):
                    evicted = table.cleanup_expired_disconnects()
                    evicted_all.extend(evicted)
        return evicted_all

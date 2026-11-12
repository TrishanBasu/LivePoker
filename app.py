import os
import re
import time
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from session_manager import SessionManager, SERVER_BOOT_ID
from room_manager import RoomManager

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "royal-felt-poker-secret-key-2026")

socketio = SocketIO(
    app,
    async_mode="gevent",
    cors_allowed_origins="*",
    ping_interval=25,
    ping_timeout=60,
)

session_manager = SessionManager()


def broadcast_table_state(room_code: str, table_id: str) -> None:
    table = room_manager.get_table(room_code, table_id)
    room = room_manager.get_room(room_code)
    if not table or not room:
        return

    with table.lock:
        seated = table.seated_players()
        for player in seated:
            sess = session_manager.get_by_id(player.session_id)
            if sess and sess.socket_sid:
                payload = table.serialize_for_player(player.session_id)
                payload["is_host"] = (room.host_session_id == player.session_id)
                socketio.emit("table_state", payload, to=sess.socket_sid)


def broadcast_room_lobby(room_code: str) -> None:
    room = room_manager.get_room(room_code)
    if not room:
        return
    with room_manager.lock:
        for member_sid in list(room.member_sessions.keys()):
            sess = session_manager.get_by_id(member_sid)
            if sess and sess.socket_sid:
                lobby_data = room_manager.serialize_room_lobby(room_code, member_sid)
                if lobby_data:
                    socketio.emit("lobby_state", lobby_data, to=sess.socket_sid)


def handle_player_evicted(table_id: str, session_id: str) -> None:
    sess = session_manager.get_by_id(session_id)
    if sess:
        if sess.table_id == table_id:
            sess.table_id = None
        if sess.socket_sid:
            socketio.emit(
                "removed_from_table",
                {"table_id": table_id, "reason": "Removed from table or disconnection timeout."},
                to=sess.socket_sid,
            )


room_manager = RoomManager(
    on_table_updated=broadcast_table_state,
    on_room_updated=broadcast_room_lobby,
    on_player_evicted=handle_player_evicted,
)


def background_maintenance_worker() -> None:
    while True:
        socketio.sleep(10)
        try:
            room_manager.sweep_disconnected_players()
        except Exception:
            pass


socketio.start_background_task(background_maintenance_worker)


def sanitize_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9 _\-]", "", (raw or "")).strip()
    return cleaned[:20]


# -------------------- HTTP ROUTES --------------------

@app.route("/")
def index():
    invite_code = (request.args.get("room") or "").strip().upper()
    return render_template("index.html", invite_code=invite_code, boot_id=SERVER_BOOT_ID)


@app.route("/lobby/<room_code>")
def lobby_page(room_code: str):
    code = room_code.strip().upper()
    room = room_manager.get_room(code)
    if not room:
        return redirect(url_for("index", room=code, expired=1))
    return render_template("lobby.html", room_code=code, boot_id=SERVER_BOOT_ID)


@app.route("/table/<room_code>/<table_id>")
def table_page(room_code: str, table_id: str):
    code = room_code.strip().upper()
    room = room_manager.get_room(code)
    if not room:
        return redirect(url_for("index", room=code, expired=1))
    table = room_manager.get_table(code, table_id)
    if not table:
        return redirect(url_for("lobby_page", room_code=code))
    return render_template("table.html", room_code=code, table_id=table_id, boot_id=SERVER_BOOT_ID)


@app.route("/api/health")
def health_check():
    return jsonify({
        "status": "ok",
        "boot_id": SERVER_BOOT_ID,
        "active_rooms": len(room_manager.rooms),
        "active_sessions": session_manager.active_session_count(),
        "timestamp": time.time(),
    })


@app.route("/api/create_room", methods=["POST"])
def api_create_room():
    data = request.get_json(silent=True) or {}
    display_name = sanitize_name(data.get("display_name", ""))
    room_name = (data.get("room_name") or "").strip()[:40]

    if len(display_name) < 2:
        return jsonify({"ok": False, "error": "Display name must be 2–20 characters."}), 400

    # Create room first with temporary placeholder, then bind actual session
    sess = session_manager.create_session(display_name=display_name, room_code="PENDING")
    room = room_manager.create_room(
        room_name=room_name,
        host_session_id=sess.session_id,
        host_name=display_name,
    )
    sess.room_code = room.room_code

    return jsonify({
        "ok": True,
        "room_code": room.room_code,
        "session_token": sess.token,
        "session_id": sess.session_id,
        "display_name": sess.display_name,
        "boot_id": SERVER_BOOT_ID,
        "redirect_url": url_for("lobby_page", room_code=room.room_code),
    })


@app.route("/api/join_room", methods=["POST"])
def api_join_room():
    data = request.get_json(silent=True) or {}
    room_code = (data.get("room_code") or "").strip().upper()
    display_name = sanitize_name(data.get("display_name", ""))
    existing_token = (data.get("session_token") or "").strip()

    if len(display_name) < 2:
        return jsonify({"ok": False, "error": "Display name must be 2–20 characters."}), 400

    room = room_manager.get_room(room_code)
    if not room:
        return jsonify({"ok": False, "error": "Private room not found or expired."}), 404

    # Check if player already holds a valid session for this exact room
    existing_sess = session_manager.get_by_token(existing_token)
    if existing_sess and existing_sess.room_code == room.room_code:
        return jsonify({
            "ok": True,
            "room_code": room.room_code,
            "session_token": existing_sess.token,
            "session_id": existing_sess.session_id,
            "display_name": existing_sess.display_name,
            "boot_id": SERVER_BOOT_ID,
            "redirect_url": url_for("lobby_page", room_code=room.room_code),
        })

    sess = session_manager.create_session(display_name=display_name, room_code=room.room_code)
    room_manager.register_member(room.room_code, sess.session_id, display_name)

    return jsonify({
        "ok": True,
        "room_code": room.room_code,
        "session_token": sess.token,
        "session_id": sess.session_id,
        "display_name": sess.display_name,
        "boot_id": SERVER_BOOT_ID,
        "redirect_url": url_for("lobby_page", room_code=room.room_code),
    })


# -------------------- SOCKET.IO EVENTS --------------------

@socketio.on("connect")
def on_connect(auth=None):
    token = None
    if isinstance(auth, dict):
        token = auth.get("token")
    emit("server_hello", {"boot_id": SERVER_BOOT_ID, "server_time": time.time()})
    if token:
        _authenticate_socket(token)


def _authenticate_socket(token: str):
    sess = session_manager.bind_socket(token, request.sid)
    if not sess:
        emit("session_invalid", {"reason": "Session expired or server restarted.", "boot_id": SERVER_BOOT_ID})
        return None

    room = room_manager.get_room(sess.room_code)
    if not room:
        session_manager.invalidate_session(sess.session_id)
        emit("session_invalid", {"reason": "Room no longer exists.", "boot_id": SERVER_BOOT_ID})
        return None

    join_room(f"room:{sess.room_code}")
    if sess.table_id:
        table = room_manager.get_table(sess.room_code, sess.table_id)
        if table:
            join_room(f"table:{sess.table_id}")
            table.mark_reconnected(sess.session_id)
        else:
            sess.table_id = None

    emit("authenticated", {
        "session_id": sess.session_id,
        "display_name": sess.display_name,
        "room_code": sess.room_code,
        "table_id": sess.table_id,
        "is_host": room.host_session_id == sess.session_id,
        "boot_id": SERVER_BOOT_ID,
    })
    broadcast_room_lobby(sess.room_code)
    return sess


@socketio.on("authenticate")
def on_authenticate(data):
    token = (data or {}).get("token", "")
    _authenticate_socket(token)


@socketio.on("heartbeat")
def on_heartbeat(data):
    token = (data or {}).get("token")
    res = session_manager.record_heartbeat(request.sid, token)
    emit("heartbeat_ack", res)


@socketio.on("disconnect")
def on_disconnect():
    sess = session_manager.unbind_socket(request.sid)
    if not sess:
        return
    if sess.table_id:
        table = room_manager.get_table(sess.room_code, sess.table_id)
        if table:
            table.mark_disconnected(sess.session_id)
    broadcast_room_lobby(sess.room_code)


@socketio.on("request_lobby_state")
def on_request_lobby_state(data):
    sess = session_manager.get_by_sid(request.sid)
    if not sess:
        emit("session_invalid", {"reason": "Unauthenticated session.", "boot_id": SERVER_BOOT_ID})
        return
    lobby_data = room_manager.serialize_room_lobby(sess.room_code, sess.session_id)
    if lobby_data:
        emit("lobby_state", lobby_data)


@socketio.on("create_table")
def on_create_table(data):
    sess = session_manager.get_by_sid(request.sid)
    if not sess:
        emit("action_error", {"error": "Session expired."})
        return
    data = data or {}
    res = room_manager.create_table(
        room_code=sess.room_code,
        actor_session_id=sess.session_id,
        table_name=data.get("table_name", ""),
        starting_chips=int(data.get("starting_chips", 1000)),
        small_blind=int(data.get("small_blind", 10)),
        big_blind=int(data.get("big_blind", 20)),
    )
    if not res["ok"]:
        emit("action_error", {"error": res["error"]})
    else:
        emit("table_created", {"table_id": res["table_id"]})


@socketio.on("close_table")
def on_close_table(data):
    sess = session_manager.get_by_sid(request.sid)
    if not sess:
        return
    table_id = (data or {}).get("table_id", "")
    res = room_manager.close_table(sess.room_code, sess.session_id, table_id)
    if not res["ok"]:
        emit("action_error", {"error": res["error"]})
    else:
        socketio.emit("table_closed", {"table_id": table_id}, to=f"table:{table_id}")


@socketio.on("join_table")
def on_join_table(data):
    sess = session_manager.get_by_sid(request.sid)
    if not sess:
        emit("session_invalid", {"reason": "Session expired.", "boot_id": SERVER_BOOT_ID})
        return

    table_id = (data or {}).get("table_id", "")
    preferred_seat = (data or {}).get("seat")

    # Prevent a player from sitting at two tables simultaneously in the same room
    if sess.table_id and sess.table_id != table_id:
        old_table = room_manager.get_table(sess.room_code, sess.table_id)
        if old_table and old_table.get_player_by_session(sess.session_id):
            emit("action_error", {"error": "You are already seated at another table. Leave it first."})
            return

    table = room_manager.get_table(sess.room_code, table_id)
    if not table:
        emit("action_error", {"error": "Table not found."})
        return

    res = table.seat_player(sess.session_id, sess.display_name, preferred_seat=preferred_seat)
    if not res["ok"]:
        emit("action_error", {"error": res["error"]})
        return

    sess.table_id = table_id
    join_room(f"table:{table_id}")
    emit("joined_table_ack", {"table_id": table_id, "seat": res["seat"]})
    broadcast_table_state(sess.room_code, table_id)
    broadcast_room_lobby(sess.room_code)


@socketio.on("request_table_state")
def on_request_table_state(data):
    sess = session_manager.get_by_sid(request.sid)
    if not sess:
        emit("session_invalid", {"reason": "Session expired.", "boot_id": SERVER_BOOT_ID})
        return
    table_id = (data or {}).get("table_id") or sess.table_id
    table = room_manager.get_table(sess.room_code, table_id)
    room = room_manager.get_room(sess.room_code)
    if not table or not room:
        emit("action_error", {"error": "Table not found."})
        return

    # Auto-seat if navigating directly via table URL while already authenticated in room
    if not table.get_player_by_session(sess.session_id):
        res = table.seat_player(sess.session_id, sess.display_name)
        if not res["ok"]:
            emit("action_error", {"error": res["error"]})
            return
        sess.table_id = table_id

    join_room(f"table:{table_id}")
    payload = table.serialize_for_player(sess.session_id)
    payload["is_host"] = (room.host_session_id == sess.session_id)
    emit("table_state", payload)


@socketio.on("player_action")
def on_player_action(data):
    sess = session_manager.get_by_sid(request.sid)
    if not sess or not sess.table_id:
        emit("action_error", {"error": "Not seated at an active table."})
        return
    table = room_manager.get_table(sess.room_code, sess.table_id)
    if not table:
        emit("action_error", {"error": "Table no longer exists."})
        return

    data = data or {}
    res = table.process_action(
        session_id=sess.session_id,
        action=data.get("action", ""),
        amount=data.get("amount", 0),
    )
    if not res["ok"]:
        emit("action_error", {"error": res["error"]})


@socketio.on("table_chat")
def on_table_chat(data):
    sess = session_manager.get_by_sid(request.sid)
    if not sess or not sess.table_id:
        return
    table = room_manager.get_table(sess.room_code, sess.table_id)
    if table:
        table.add_chat(sess.session_id, (data or {}).get("text", ""))


@socketio.on("host_control")
def on_host_control(data):
    sess = session_manager.get_by_sid(request.sid)
    if not sess:
        return
    room = room_manager.get_room(sess.room_code)
    if not room or room.host_session_id != sess.session_id:
        emit("action_error", {"error": "Unauthorized: Host privileges required."})
        return

    data = data or {}
    table_id = data.get("table_id") or sess.table_id
    table = room_manager.get_table(sess.room_code, table_id)
    if not table:
        emit("action_error", {"error": "Target table not found."})
        return

    command = data.get("command", "")
    if command == "start_game":
        res = table.start_game()
    elif command == "end_game":
        res = table.end_game()
    elif command == "configure_stakes":
        res = table.configure_stakes(
            starting_chips=int(data.get("starting_chips", table.starting_chips)),
            small_blind=int(data.get("small_blind", table.small_blind)),
            big_blind=int(data.get("big_blind", table.big_blind)),
        )
    elif command == "adjust_chips":
        res = table.host_adjust_chips(
            target_session_id=data.get("target_session_id", ""),
            mode=data.get("mode", "add"),
            amount=int(data.get("amount", 0)),
        )
    elif command == "remove_player":
        target_sid = data.get("target_session_id", "")
        ok = table.remove_player(target_sid, reason="was removed by the host")
        res = {"ok": ok, "error": "Player not found."}
    else:
        res = {"ok": False, "error": "Unknown host command."}

    if not res.get("ok"):
        emit("action_error", {"error": res.get("error", "Host action failed.")})


@socketio.on("leave_table")
def on_leave_table(data=None):
    sess = session_manager.get_by_sid(request.sid)
    if not sess or not sess.table_id:
        return
    table_id = sess.table_id
    table = room_manager.get_table(sess.room_code, table_id)
    if table:
        table.remove_player(sess.session_id, reason="deliberately left the table")
    leave_room(f"table:{table_id}")
    sess.table_id = None
    emit("left_table_ack", {"room_code": sess.room_code})
    broadcast_room_lobby(sess.room_code)


@socketio.on("leave_room_permanently")
def on_leave_room_permanently(data=None):
    sess = session_manager.get_by_sid(request.sid)
    if not sess:
        return
    room_code = sess.room_code
    sid = sess.session_id
    leave_room(f"room:{room_code}")
    if sess.table_id:
        leave_room(f"table:{sess.table_id}")
    room_manager.unregister_member(room_code, sid)
    session_manager.invalidate_session(sid)
    emit("left_room_ack", {})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)

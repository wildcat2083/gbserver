"""
The room registry - the shared game (default_emu) plus the private-room
dict, session lookup, controller-gating for HTTP routes, and the
background idle-room reaper.
"""
import random
import shutil
import threading
import time

from flask import abort, jsonify, request

from config import (
    MAX_ROOMS,
    ROOM_CODE_ALPHABET,
    ROOM_CODE_LENGTH,
    ROOM_IDLE_TIMEOUT_SECONDS,
    ROOM_SAVES_DIR,
    SAVES_DIR,
)
from emulator import Emulator

# --- Room registry ----------------------------------------------------

default_emu = Emulator(SAVES_DIR, auto_stop_when_empty=True)   # the shared game - stops itself when everyone leaves
rooms = {}                          # room code -> Emulator, for private sessions
rooms_lock = threading.Lock()

# Admin-toggleable - lets the shared game be turned off entirely (private
# rooms, which need a semi-secret code, are unaffected) without a code
# change or restart. A dict rather than a plain bool specifically because
# "from rooms import shared_game_state" captures a snapshot of a plain
# variable's value at import time in Python - later reassigning it
# elsewhere would NOT be seen by modules that already imported it that
# way. A dict is itself the shared object every importer holds a
# reference to, so mutating shared_game_state["enabled"] correctly
# propagates everywhere, the same way the rooms dict above already does.
# Resets to enabled on every restart - deliberately not persisted, so a
# routine restart never leaves it silently off with no visible reason.
shared_game_state = {"enabled": True}


def create_room():
    """Creates a new private room and returns its code, or None if
    MAX_ROOMS is already reached. Shared by the regular "create a room"
    HTTP route and the admin "move this client to their own room" action,
    so both go through the exact same capacity check and code-generation
    logic rather than two separate copies of it."""
    with rooms_lock:
        if len(rooms) >= MAX_ROOMS:
            return None
        code = "".join(random.choices(ROOM_CODE_ALPHABET, k=ROOM_CODE_LENGTH))
        while code in rooms:
            code = "".join(random.choices(ROOM_CODE_ALPHABET, k=ROOM_CODE_LENGTH))
        rooms[code] = Emulator(ROOM_SAVES_DIR / code)
        return code


def get_emulator(room_code):
    """Returns the Emulator for this session, or None if the room doesn't exist."""
    if not room_code:
        return default_emu
    with rooms_lock:
        return rooms.get(room_code.upper())


def get_emulator_or_404(room_code):
    emu = get_emulator(room_code)
    if emu is None:
        abort(404, description="That room doesn't exist or has expired.")
    emu.touch()
    return emu


def controller_check(emu):
    """Returns a (response, 403) pair if this request isn't from the current
    controller, or None if it's fine to proceed. Every mutating Settings
    action (play/stop/delete/save/audio-batch) is gated this way - the
    browser sends its client_id both on WS connect and as a header on these
    requests, so the two can be matched even though HTTP requests aren't
    tied to any particular WebSocket connection.

    Uses jsonify + a status code rather than Flask's abort(403) so the
    response body is JSON like every other error in this app - abort()
    would return an HTML error page, which breaks the client's res.json()
    handling."""
    client_id = request.headers.get("X-Client-Id", "")
    if not emu.is_controller_client(client_id):
        return jsonify({"error": "Only the current controller can do that."}), 403
    return None


def _reap_idle_rooms():
    """Background thread: tears down private rooms nobody's connected to
    and that haven't had any activity in a while, so PyBoy instances don't
    pile up forever from links that got created and never joined, or that
    everyone eventually left. Also deletes that room's save directory from
    disk at the same time - a reaped room's save data isn't kept around
    indefinitely, unlike the shared game's."""
    while True:
        time.sleep(60)
        now = time.time()
        with rooms_lock:
            stale = [
                code for code, e in rooms.items()
                if not e.clients and (now - e.last_activity) > ROOM_IDLE_TIMEOUT_SECONDS
            ]
            for code in stale:
                e = rooms.pop(code)
                try:
                    e.shutdown()  # stops the game AND terminates the worker process -
                                  # this room is gone for good, not just its current game
                except Exception as ex:
                    print(f"[warn] error shutting down reaped room {code}: {ex}")
                room_dir = ROOM_SAVES_DIR / code
                try:
                    if room_dir.exists():
                        shutil.rmtree(room_dir)
                except Exception as ex:
                    print(f"[warn] error deleting save dir for reaped room {code}: {ex}")
                print(f"[rooms] reaped idle room {code}")


def start_reaper():
    """Starts the idle-room reaper thread. Called once from app.py at
    startup - kept as an explicit call rather than a bare module-import
    side effect, so importing this module for testing doesn't silently
    spawn a background thread."""
    threading.Thread(target=_reap_idle_rooms, daemon=True).start()

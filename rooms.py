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


default_emu = Emulator(SAVES_DIR, auto_stop_when_empty=True)
rooms = {}
rooms_lock = threading.Lock()


shared_game_state = {"enabled": True}


def create_room():
    with rooms_lock:
        if len(rooms) >= MAX_ROOMS:
            return None
        code = "".join(random.choices(ROOM_CODE_ALPHABET, k=ROOM_CODE_LENGTH))
        while code in rooms:
            code = "".join(random.choices(ROOM_CODE_ALPHABET, k=ROOM_CODE_LENGTH))
        rooms[code] = Emulator(ROOM_SAVES_DIR / code)
        return code


def get_emulator(room_code):
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
    client_id = request.headers.get("X-Client-Id", "")
    if not emu.is_controller_client(client_id):
        return jsonify({"error": "Only the current controller can do that."}), 403
    return None


def _reap_idle_rooms():
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
                    e.shutdown()

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
    threading.Thread(target=_reap_idle_rooms, daemon=True).start()

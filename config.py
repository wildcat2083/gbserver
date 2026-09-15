import os
import sys
import threading
from pathlib import Path

BUTTON_NAMES = {"a", "b", "start", "select", "up", "down", "left", "right"}


KICK_CLOSE_CODE = 4001


SHARED_DISABLED_CLOSE_CODE = 4002


OFFLINE_CLOSE_CODE = 4003


MSG_VIDEO = b"\x01"
MSG_AUDIO = b"\x02"


SOUND_SAMPLE_RATE = 36000


SOUND_VOLUME = 85


AUDIO_BATCH_TICKS = 4


AUTOSAVE_INTERVAL_MINUTES = 5


FAST_FORWARD_SPEED = 4


CHAT_RATE_WINDOW_SECONDS = 10
CHAT_RATE_MAX_MESSAGES = 8

BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
ROMS_DIR = BASE_DIR / "roms"
SAVES_DIR = BASE_DIR / "saves"
ROOM_SAVES_DIR = BASE_DIR / "saves" / "rooms"
ROMS_DIR.mkdir(exist_ok=True)
SAVES_DIR.mkdir(exist_ok=True)
ROOM_SAVES_DIR.mkdir(parents=True, exist_ok=True)


OFFLINE_FLAG_PATH = BASE_DIR / "offline.flag"


ENGINE_OVERRIDES_PATH = ROMS_DIR / "_engine_overrides.json"
_engine_overrides_lock = threading.Lock()


ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH = 6


MAX_ROOMS = int(os.environ.get("GBSERVER_MAX_ROOMS", "6"))


ROOM_IDLE_TIMEOUT_SECONDS = 30 * 60


IDLE_CONTROLLER_TIMEOUT_SECONDS = 2 * 60


NO_INPUT_TIMEOUT_SECONDS = 30

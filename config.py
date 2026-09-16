import os
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

BASE_DIR = Path(__file__).parent
ROMS_DIR = BASE_DIR / "roms"
SAVES_DIR = BASE_DIR / "saves"
ROOM_SAVES_DIR = BASE_DIR / "saves" / "rooms"
ROMS_DIR.mkdir(exist_ok=True)
SAVES_DIR.mkdir(exist_ok=True)
ROOM_SAVES_DIR.mkdir(parents=True, exist_ok=True)


OFFLINE_FLAG_PATH = BASE_DIR / "offline.flag"


# roms/ holds only ROMs (and optional .sym debug symbols next to them).
# Everything the server writes lives under saves/.
ENGINE_OVERRIDES_PATH = SAVES_DIR / "_engine_overrides.json"
MIGRATED_FROM_ROMS_DIR = SAVES_DIR / "_from_roms"
_engine_overrides_lock = threading.Lock()


ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH = 6


MAX_ROOMS = int(os.environ.get("GBSERVER_MAX_ROOMS", "6"))


ROOM_IDLE_TIMEOUT_SECONDS = 30 * 60


IDLE_CONTROLLER_TIMEOUT_SECONDS = 2 * 60


NO_INPUT_TIMEOUT_SECONDS = 30


ROM_EXTENSIONS = (".gb", ".gbc")


MAX_UPLOAD_BYTES = 8 * 1024 * 1024


def safe_rom_name(filename):
    """Validate a client-supplied ROM filename and return it unchanged.

    Rejects anything that isn't a plain basename ending in .gb/.gbc - no
    directory separators, no "..", no absolute paths - so it can never
    escape ROMS_DIR when joined onto it. Raises ValueError otherwise.
    """
    if not isinstance(filename, str) or not filename or "\x00" in filename:
        raise ValueError("invalid ROM filename")
    if "/" in filename or "\\" in filename:
        raise ValueError("invalid ROM filename")
    name = Path(filename).name
    if name != filename or name in (".", ".."):
        raise ValueError("invalid ROM filename")
    if Path(name).suffix.lower() not in ROM_EXTENSIONS:
        raise ValueError("only .gb / .gbc files are supported")
    return name


def safe_rom_path(filename):
    """safe_rom_name() plus a resolved-path containment check."""
    name = safe_rom_name(filename)
    roms_root = ROMS_DIR.resolve()
    path = (roms_root / name).resolve()
    if path.parent != roms_root:
        raise ValueError("invalid ROM filename")
    return path


# Hidden debugger (memory viewer/editor, breakpoints, search).
#   on       - available on every hostname (writes still controller-only)
#   internal - only when reached via one of GBSERVER_INTERNAL_HOSTS
#   off      - disabled entirely
DEBUGGER_MODE = os.environ.get("GBSERVER_DEBUGGER", "on").strip().lower()
if DEBUGGER_MODE not in ("on", "internal", "off"):
    DEBUGGER_MODE = "on"
INTERNAL_HOSTS = {
    h.strip().lower()
    for h in os.environ.get(
        "GBSERVER_INTERNAL_HOSTS",
        "gbserver-internal.wulfpax-labs.com,localhost,127.0.0.1",
    ).split(",")
    if h.strip()
}


def debugger_allowed_for_host(host):
    if DEBUGGER_MODE == "off":
        return False
    if DEBUGGER_MODE == "on":
        return True
    host = (host or "").lower()
    if host.startswith("["):
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host in INTERNAL_HOSTS


def rom_symbols_path(rom_path):
    """The .sym file for a ROM, if one sits beside it (name.sym or name.gb.sym)."""
    rom_path = Path(rom_path)
    for candidate in (rom_path.with_suffix(".sym"), rom_path.with_name(rom_path.name + ".sym")):
        if candidate.is_file():
            return candidate
    return None


def migrate_roms_folder():
    """Move anything the server (or PyBoy) left in roms/ over to saves/.

    - Save states (name.state, or PyBoy's hotkey-style name.gb.state) move to
      saves/name.state, where the server loads them - unless saves/ already has
      one for that ROM, in which case the old copy goes to saves/_from_roms/.
    - Engine overrides move to saves/_engine_overrides.json.
    - Battery RAM / RTC / .sav files and anything else that isn't a ROM or a
      .sym go to saves/_from_roms/ for safekeeping (the server doesn't read them).
    Nothing is ever overwritten or deleted. Returns a list of (src, dst) moves.
    """
    moves = []

    def park(src, dst):
        if dst.exists():
            MIGRATED_FROM_ROMS_DIR.mkdir(parents=True, exist_ok=True)
            dst = MIGRATED_FROM_ROMS_DIR / src.name
            n = 1
            while dst.exists():
                dst = MIGRATED_FROM_ROMS_DIR / f"{src.stem}.{n}{src.suffix}"
                n += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        moves.append((src, dst))

    try:
        entries = [p for p in ROMS_DIR.iterdir() if p.is_file()]
    except OSError:
        return moves

    for p in entries:
        name = p.name
        lower = name.lower()
        if lower.endswith((".gb", ".gbc", ".sym")) or name.startswith("place-roms-here"):
            continue
        try:
            if name == "_engine_overrides.json":
                park(p, ENGINE_OVERRIDES_PATH)
            elif lower.endswith(".state"):
                stem = name[: -len(".state")]
                if stem.lower().endswith((".gb", ".gbc")):
                    stem = Path(stem).stem
                park(p, SAVES_DIR / f"{stem}.state")
            elif lower.endswith((".uploading",)):
                continue
            else:
                MIGRATED_FROM_ROMS_DIR.mkdir(parents=True, exist_ok=True)
                park(p, MIGRATED_FROM_ROMS_DIR / name)
        except OSError as e:
            print(f"[warn] couldn't move {p} out of roms/: {e}")

    for src, dst in moves:
        print(f"[info] moved {src.name} from roms/ to {dst.relative_to(BASE_DIR)}")
    return moves

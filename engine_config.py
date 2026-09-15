import json

from config import ENGINE_OVERRIDES_PATH, ROMS_DIR, _engine_overrides_lock, safe_rom_name


try:
    from boytacean.pyboy import PyBoyV2 as Boytacean
    BOYTACEAN_AVAILABLE = True
except ImportError:
    Boytacean = None
    BOYTACEAN_AVAILABLE = False


def _load_engine_overrides():
    with _engine_overrides_lock:
        if not ENGINE_OVERRIDES_PATH.exists():
            return {}
        try:
            return json.loads(ENGINE_OVERRIDES_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}


def _save_engine_overrides(data):
    with _engine_overrides_lock:
        ENGINE_OVERRIDES_PATH.write_text(json.dumps(data))


def get_engine_for_rom(filename):
    return _load_engine_overrides().get(filename, "pyboy")


def set_engine_for_rom(filename, engine):
    filename = safe_rom_name(filename)
    if not (ROMS_DIR / filename).exists():
        raise ValueError("no such ROM")
    if engine not in ("pyboy", "boytacean"):
        raise ValueError(f'Unknown engine "{engine}" - must be "pyboy" or "boytacean"')
    if engine == "boytacean" and not BOYTACEAN_AVAILABLE:
        raise ValueError("boytacean is not installed on this server")
    overrides = _load_engine_overrides()
    overrides[filename] = engine
    _save_engine_overrides(overrides)

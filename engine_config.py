"""
Optional alternate emulation engine (boytacean) detection, and the
per-ROM engine-choice persistence (which engine a given ROM file should
run on - "pyboy" or "boytacean"), stored as a small flat JSON map.
"""
import json

from config import ENGINE_OVERRIDES_PATH, _engine_overrides_lock

# boytacean is an OPTIONAL alternate engine - opt-in per ROM (see
# ENGINE_OVERRIDES below), never the default. Its Python bindings currently
# expose no audio API at all, so ROMs running on it stream video only.
# It's not installed by default; the server works identically without it,
# with every ROM simply staying on PyBoy.
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
    if engine not in ("pyboy", "boytacean"):
        raise ValueError(f'Unknown engine "{engine}" - must be "pyboy" or "boytacean"')
    if engine == "boytacean" and not BOYTACEAN_AVAILABLE:
        raise ValueError("boytacean is not installed on this server")
    overrides = _load_engine_overrides()
    overrides[filename] = engine
    _save_engine_overrides(overrides)

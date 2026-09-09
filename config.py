"""
Shared constants for the headless Game Boy server - paths, tunables, and
protocol constants used across emulator.py, rooms.py, and routes.py.
Nothing in this module has any behavior of its own.
"""
import os
import threading
from pathlib import Path

BUTTON_NAMES = {"a", "b", "start", "select", "up", "down", "left", "right"}

# WebSocket close code used specifically for admin-initiated kicks (see
# Emulator.kick_client) - distinguishes a deliberate removal from any
# other disconnect reason (network drop, server restart, etc.), so the
# client's own auto-reconnect logic can skip reconnecting for this one
# specific case while still reconnecting normally for everything else.
# 4000-4999 is the range reserved for private/application use by the
# WebSocket spec (RFC 6455 7.4.2).
KICK_CLOSE_CODE = 4001

# WebSocket close code sent when someone tries to connect to the shared
# game while an admin has it turned off (see shared_game_state in
# rooms.py) - lets the client show a clear, specific message instead of
# endlessly retrying a connection that's deliberately unavailable right
# now, the same reasoning as KICK_CLOSE_CODE above.
SHARED_DISABLED_CLOSE_CODE = 4002

# Message type headers for the WebSocket binary protocol (1 byte prefix)
MSG_VIDEO = b"\x01"
MSG_AUDIO = b"\x02"

# PyBoy's sound buffer is fixed at int8 (256 amplitude levels) - this is a
# hardware/emulation-accuracy limitation, not something the sample rate
# affects. 48000 Hz is PyBoy's own tested default (its buffer sizing math
# is validated against it); 24000 also works. Only values where
# rate % 60 == 0 are accepted by PyBoy - e.g. 44800 is NOT valid and will
# fail an internal assertion.
SOUND_SAMPLE_RATE = 24000  # Hz - must divide evenly into 60 (48000 overran on this hardware)

# Sending one WebSocket audio message per emulator tick (~16.7ms) means the
# browser has to splice ~60 tiny AudioBuffers together per second, and any
# scheduling drift at those seams is audible as clicking/graininess. Batching
# a few ticks together before sending cuts the number of seams way down -
# trades a little latency (this many ticks' worth, ~67ms at 4) for much
# smoother playback.
AUDIO_BATCH_TICKS = 4

# How often the running game is auto-saved in the background, in minutes.
# This used to be every 15 seconds, which is more churn than needed - the
# "Save now" button (manual, on demand) and Stop (always saves right
# before closing) cover the moments that actually matter; this periodic
# save is just a safety net against a crash or power loss in between.
AUTOSAVE_INTERVAL_MINUTES = 5

# Fast-forward multiplier, applied via PyBoy's own set_emulation_speed() -
# see the _run_loop docstring for why the loop's own pacing has to change
# in step with this, not just this value alone.
FAST_FORWARD_SPEED = 4

# Chat rate limiting - applied per-connection in add_chat_message(), since
# chat arrives over the WebSocket rather than a regular HTTP request that
# Flask-Limiter's decorators could reach.
CHAT_RATE_WINDOW_SECONDS = 10
CHAT_RATE_MAX_MESSAGES = 8

BASE_DIR = Path(__file__).parent
ROMS_DIR = BASE_DIR / "roms"                    # shared ROM library across every room
SAVES_DIR = BASE_DIR / "saves"                  # the default/shared game's saves - unchanged path
ROOM_SAVES_DIR = BASE_DIR / "saves" / "rooms"   # each private room gets its own subfolder here
ROMS_DIR.mkdir(exist_ok=True)
SAVES_DIR.mkdir(exist_ok=True)
ROOM_SAVES_DIR.mkdir(parents=True, exist_ok=True)


# Per-ROM engine choice ("pyboy" or "boytacean"), shared across every room
# since it's a property of the ROM file itself, not of any one session -
# a small flat JSON map, e.g. {"Some Glitchy Game.gb": "boytacean"}.
# Anything not listed here just uses "pyboy" (the default).
ENGINE_OVERRIDES_PATH = ROMS_DIR / "_engine_overrides.json"
_engine_overrides_lock = threading.Lock()

# Codes avoid 0/O/1/I/L - easy to read and type back when sharing a link.
ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH = 6

# Soft cap on simultaneous private rooms. This is sized for a handful of
# concurrent players on a Pi 5 - each room is a full extra 60fps emulation
# loop plus its own video/audio WebSocket broadcast, so raise this only if
# you've confirmed the hardware keeps up with more at once.
MAX_ROOMS = int(os.environ.get("GBSERVER_MAX_ROOMS", "6"))

# A room with zero connected clients sitting idle this long gets torn down
# automatically, freeing its PyBoy instance. Rooms with clients connected
# are never reaped regardless of this value.
ROOM_IDLE_TIMEOUT_SECONDS = 30 * 60

# How long a client may hold controller status without loading a ROM
# before control automatically passes to the next connected client -
# stops one person occupying the controller slot indefinitely (browsing
# the library, stepping away, etc.) from blocking everyone else from
# starting anything, without needing an admin to notice and intervene
# each time.
IDLE_CONTROLLER_TIMEOUT_SECONDS = 2 * 60
# Complementary to the above, but catches a case that one doesn't: a
# controller connecting to an ALREADY-RUNNING game (a game left playing
# from a previous session, which is the common case for the shared
# room) never touches a single button. The timer above only ever starts
# when nothing is loaded yet, so it never applies to that scenario at
# all - a bot (a link-preview crawler, a scanner, anything that opens
# the WebSocket without ever sending real input) could otherwise sit as
# controller indefinitely with no timeout whatsoever. Deliberately
# shorter than the timeout above - "connected and has never sent a
# single input" is a much stronger signal than "hasn't chosen a ROM
# yet", so it's reasonable to act on it faster.
NO_INPUT_TIMEOUT_SECONDS = 30

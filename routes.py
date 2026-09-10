"""
Every Flask HTTP route and the WebSocket handler. Imports app/sock/limiter
from app.py (the entry point creates them; this module registers routes
onto them via the usual decorators) and the room/emulator logic from
rooms.py and emulator.py.
"""
import json
import random
import io
from pathlib import Path

from flask import jsonify, make_response, render_template, request, send_from_directory, send_file

from app import app, limiter, sock
from config import (
    FAST_FORWARD_SPEED,
    MAX_ROOMS,
    ROOM_CODE_ALPHABET,
    ROOM_CODE_LENGTH,
    ROOM_SAVES_DIR,
    ROMS_DIR,
    SHARED_DISABLED_CLOSE_CODE,
    SOUND_SAMPLE_RATE,
)
from emulator import Emulator
from engine_config import BOYTACEAN_AVAILABLE, get_engine_for_rom, set_engine_for_rom
from rooms import controller_check, create_room, default_emu, get_emulator, get_emulator_or_404, rooms, rooms_lock, shared_game_state

# --- HTTP routes -------------------------------------------------------

@app.route("/")
def index():
    if not shared_game_state["enabled"]:
        response = make_response(render_template(
            "index.html", room_code=None, room_missing=False, shared_disabled=True,
        ))
    else:
        response = make_response(render_template("index.html", room_code=None, room_missing=False))
    # Never cached, at all - specifically because asset_version()'s cache-
    # busting for style.css/app.js only works if THIS page is always
    # fresh. The version number those files are requested with lives in
    # this HTML, computed at render time - if a browser (or a bot's
    # headless one) ever cached this page itself, it would keep
    # requesting style.css/app.js with a stale, embedded old version
    # number forever, silently defeating that whole mechanism. Relevant
    # for bot mitigation specifically: a connection that never re-fetches
    # a genuinely fresh copy of this page can never pick up new
    # protections added here (the click-to-connect gate, for instance).
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/r/<room_code>")
def room_index(room_code):
    room_code = room_code.upper()
    with rooms_lock:
        exists = room_code in rooms
    response = make_response(render_template("index.html", room_code=room_code, room_missing=not exists))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/rooms", methods=["POST"])
@limiter.limit("10 per hour")
def api_create_room():
    code = create_room()
    if code is None:
        return jsonify({
            "error": f"All rooms are full right now (max {MAX_ROOMS} at once). "
                     f"Try again in a bit once one closes, or wait for an idle one to expire."
        }), 429
    return jsonify({"ok": True, "room": code})


@app.route("/api/roms")
@app.route("/r/<room_code>/api/roms")
def api_roms(room_code=None):
    emu = get_emulator_or_404(room_code)
    return jsonify({"roms": emu.rom_library_info(), "current": emu.current_rom_name()})


@app.route("/api/config")
@app.route("/r/<room_code>/api/config")
def api_config(room_code=None):
    emu = get_emulator_or_404(room_code)
    return jsonify({
        "width": 160,
        "height": 144,
        "sample_rate": SOUND_SAMPLE_RATE,
        "current_rom": emu.current_rom_name(),
        "has_save": emu.has_save(),
        "library_total_bytes": emu.library_total_bytes(),
        "audio_batch_ticks": emu.audio_batch_ticks,
        "engine": emu.engine_name,
        "audio_available": emu.engine_name == "pyboy",
        "boytacean_available": BOYTACEAN_AVAILABLE,
        "fast_forward": emu.fast_forward,
        "fast_forward_speed": FAST_FORWARD_SPEED,
    })


@app.route("/api/audio-batch", methods=["GET", "POST"])
@app.route("/r/<room_code>/api/audio-batch", methods=["GET", "POST"])
@limiter.limit("60 per minute")
def api_audio_batch(room_code=None):
    emu = get_emulator_or_404(room_code)
    if request.method == "POST":
        denied = controller_check(emu)
        if denied:
            return denied
        data = request.get_json(force=True)
        ticks = data.get("ticks")
        if not isinstance(ticks, int) or not (1 <= ticks <= 20):
            return jsonify({"error": "ticks must be an integer from 1-20"}), 400
        emu.audio_batch_ticks = ticks
    return jsonify({"ticks": emu.audio_batch_ticks})


@app.route("/api/fast-forward", methods=["GET", "POST"])
@app.route("/r/<room_code>/api/fast-forward", methods=["GET", "POST"])
@limiter.limit("60 per minute")
def api_fast_forward(room_code=None):
    emu = get_emulator_or_404(room_code)
    if request.method == "POST":
        denied = controller_check(emu)
        if denied:
            return denied
        data = request.get_json(force=True)
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled must be true or false"}), 400
        emu.set_fast_forward(enabled)
        emu._notify_fast_forward_status()
    return jsonify({"enabled": emu.fast_forward, "speed": FAST_FORWARD_SPEED})


@app.route("/api/upload", methods=["POST"])
@app.route("/r/<room_code>/api/upload", methods=["POST"])
@limiter.limit("20 per hour")
def api_upload(room_code=None):
    get_emulator_or_404(room_code)  # rooms must exist to upload, even though the library is shared
    f = request.files.get("rom")
    if f is None or f.filename == "":
        return jsonify({"error": "no file"}), 400
    if not (f.filename.lower().endswith(".gb") or f.filename.lower().endswith(".gbc")):
        return jsonify({"error": "only .gb / .gbc files are supported"}), 400
    dest = ROMS_DIR / f.filename
    f.save(dest)
    return jsonify({"ok": True, "filename": f.filename})


@app.route("/api/play", methods=["POST"])
@app.route("/r/<room_code>/api/play", methods=["POST"])
@limiter.limit("30 per minute")
def api_play(room_code=None):
    emu = get_emulator_or_404(room_code)
    denied = controller_check(emu)
    if denied:
        return denied
    data = request.get_json(force=True)
    filename = data.get("filename")
    if not filename:
        return jsonify({"error": "filename required"}), 400
    # "Play selected" starts fresh by default; the client explicitly sends
    # load_save:true for the separate "Resume save" action.
    load_save = bool(data.get("load_save", False))
    try:
        save_load_error = emu.load_rom(filename, load_save=load_save)
    except FileNotFoundError:
        return jsonify({"error": "rom not found"}), 404
    if save_load_error:
        # ROM did start - just without its save applied - so this is a
        # partial success, not a hard failure. The client shows the
        # warning inline rather than blocking on it.
        return jsonify({
            "ok": True,
            "warning": f"Started, but the existing save couldn't be loaded (started fresh instead): {save_load_error}",
        })
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
@app.route("/r/<room_code>/api/stop", methods=["POST"])
@limiter.limit("30 per minute")
def api_stop(room_code=None):
    emu = get_emulator_or_404(room_code)
    denied = controller_check(emu)
    if denied:
        return denied
    emu.stop()
    # has_save is checked AFTER stop() (which itself autosaves as its last
    # step) so the client knows, at the exact right moment, whether it's
    # actually safe to trigger a save-file download - see the bug this
    # fixed: navigating straight to /api/save when nothing exists to
    # download just replaced the whole page with that route's raw JSON
    # 404 error, instead of a file.
    return jsonify({"ok": True, "has_save": emu.has_save()})


@app.route("/api/reset", methods=["POST"])
@app.route("/r/<room_code>/api/reset", methods=["POST"])
@limiter.limit("30 per minute")
def api_reset(room_code=None):
    emu = get_emulator_or_404(room_code)
    denied = controller_check(emu)
    if denied:
        return denied
    try:
        emu.reset()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


def _parse_gameshark_code(code_str):
    """Parses a single GameShark (original Game Boy) cheat code into an
    {"address": int, "value": int} dict.

    Format: 8 hex characters, TTVVAAAA -
      TT   - type/bank byte, not currently used/validated here - the vast
             majority of real GB GameShark codes use 01, a plain RAM write
      VV   - the value to write (1 byte)
      AAAA - the target address, stored LOW BYTE FIRST in the code string
             (e.g. "41D2" in the code means address bytes [0x41, 0xD2],
             which as a 16-bit address is 0xD241) - checked against a
             known, widely-documented code (Pokemon Red's "Infinite
             Master Balls", 01FF41D2, targets the well-documented WRAM
             item-slot address 0xD241, confirming this byte order).

    Raises ValueError with a clear, specific message on anything
    malformed, rather than silently accepting garbage that would end up
    writing to the wrong address entirely.
    """
    original = code_str
    code_str = code_str.strip().upper()
    if len(code_str) != 8 or not all(c in "0123456789ABCDEF" for c in code_str):
        raise ValueError(f'"{original}" is not a valid 8-character hex GameShark code')

    value = int(code_str[2:4], 16)
    addr_low = int(code_str[4:6], 16)
    addr_high = int(code_str[6:8], 16)
    address = (addr_high << 8) | addr_low

    return {"address": address, "value": value}


@app.route("/api/cheats", methods=["POST"])
@app.route("/r/<room_code>/api/cheats", methods=["POST"])
@limiter.limit("30 per minute")
def api_cheats(room_code=None):
    """Replaces the ENTIRE active cheat list wholesale with whatever's in
    this request - not an incremental add/remove. The client is expected
    to send its full current list every time (add one, remove one,
    whatever the UI did locally), which keeps this route - and
    do_set_cheats on the worker side - simple, stateless reassignment
    rather than needing to track a diff."""
    emu = get_emulator_or_404(room_code)
    denied = controller_check(emu)
    if denied:
        return denied
    data = request.get_json(force=True) or {}
    raw_codes = data.get("codes", [])
    if not isinstance(raw_codes, list):
        return jsonify({"error": "codes must be a list of code strings"}), 400
    parsed = []
    for raw in raw_codes:
        try:
            parsed.append(_parse_gameshark_code(raw))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    emu.set_cheats(parsed)
    # Echoes back the decoded address/value for each code, not just "ok" -
    # lets the UI show exactly what a code resolved to, so it can be
    # checked against a known cheat-code database for whatever game is
    # actually loaded.
    return jsonify({"ok": True, "parsed": parsed})


@app.route("/api/rom/<path:filename>/engine", methods=["GET", "POST"])
@app.route("/r/<room_code>/api/rom/<path:filename>/engine", methods=["GET", "POST"])
@limiter.limit("30 per minute")
def api_rom_engine(filename, room_code=None):
    # The engine choice is a property of the ROM file itself (shared across
    # every room), but we still need a real emulator/room to check the
    # controller-gate against for the POST case, and to 404 correctly for
    # an unknown room.
    emu = get_emulator_or_404(room_code)
    if request.method == "POST":
        denied = controller_check(emu)
        if denied:
            return denied
        data = request.get_json(force=True)
        engine = data.get("engine")
        try:
            set_engine_for_rom(filename, engine)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "engine": engine})
    return jsonify({
        "engine": get_engine_for_rom(filename),
        "boytacean_available": BOYTACEAN_AVAILABLE,
    })


@app.route("/api/rom/<path:filename>", methods=["DELETE"])
@app.route("/r/<room_code>/api/rom/<path:filename>", methods=["DELETE"])
@limiter.limit("20 per hour")
def api_delete_rom(filename, room_code=None):
    emu = get_emulator_or_404(room_code)
    denied = controller_check(emu)
    if denied:
        return denied
    try:
        emu.delete_rom(filename)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/save-now", methods=["POST"])
@app.route("/r/<room_code>/api/save-now", methods=["POST"])
@limiter.limit("20 per minute")
def api_save_now(room_code=None):
    emu = get_emulator_or_404(room_code)
    denied = controller_check(emu)
    if denied:
        return denied
    try:
        emu.save_now()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/save", methods=["GET", "POST", "DELETE"])
@app.route("/r/<room_code>/api/save", methods=["GET", "POST", "DELETE"])
@limiter.limit("30 per minute")
def api_save(room_code=None):
    emu = get_emulator_or_404(room_code)

    if request.method == "GET":
        path = emu.save_file_path()
        if path is None:
            return jsonify({"error": "no save available"}), 404
        return send_from_directory(path.parent, path.name, as_attachment=True)

    # POST and DELETE both change the current save - controller-only.
    denied = controller_check(emu)
    if denied:
        return denied

    if request.method == "POST":
        f = request.files.get("save")
        if f is None or f.filename == "":
            return jsonify({"error": "no file"}), 400
        try:
            rom_name, save_load_error = emu.upload_save(f)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if save_load_error:
            # Unlike api_play, the whole point of this action was to apply
            # THIS save - so if it didn't actually load, that's a failure
            # from the user's perspective even though the ROM is running.
            return jsonify({
                "ok": False,
                "error": f"Uploaded, but couldn't be loaded - it may be corrupted or from an incompatible save/ROM version: {save_load_error}",
            }), 400
        # rom_name tells the client which ROM the save actually applied
        # to - the dropdown selection otherwise has no way to know, and
        # silently disables "Resume save" afterward if it was showing a
        # different ROM at upload time (see upload_save's own docstring).
        return jsonify({"ok": True, "rom": rom_name})

    # DELETE
    try:
        emu.delete_save()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/convert-save", methods=["POST"])
@app.route("/r/<room_code>/api/convert-save", methods=["POST"])
@limiter.limit("30 per minute")
def api_convert_save(room_code=None):
    """Boots the target ROM fresh with an uploaded .sav's bytes injected
    as cartridge RAM - the "Convert Save" settings button. Does NOT
    write a .state file itself; the person plays normally afterward
    (navigating any in-game continue/load screen through the regular
    controls) and uses the existing "Save now" once they've reached the
    point they want captured. See Emulator.convert_sav's own docstring
    for the full reasoning."""
    emu = get_emulator_or_404(room_code)
    denied = controller_check(emu)
    if denied:
        return denied
    f = request.files.get("sav")
    if f is None or f.filename == "":
        return jsonify({"error": "no file"}), 400
    try:
        rom_name = emu.convert_sav(f)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    # Same reasoning as api_save's POST handler above - the client needs
    # to know which ROM this actually applied to, to sync its own
    # dropdown/UI state.
    return jsonify({"ok": True, "rom": rom_name})


@app.route("/api/sav", methods=["GET"])
@app.route("/r/<room_code>/api/sav", methods=["GET"])
@limiter.limit("30 per minute")
def api_download_sav(room_code=None):
    """Downloads the current session's cartridge RAM as a .sav file -
    see Emulator.extract_sav's own docstring for how this is actually
    extracted (a momentary, lossless pause/resume on the worker side,
    not a read of some file already sitting on disk - gbserver never
    persists a .sav on its own, only .state).

    Controller-only, unlike the plain .state download above (GET
    /api/save) - that one's a pure read of a static file, but this one
    triggers a real reboot cycle on the live emulator process, so it's
    gated the same as other actions that actually touch the running
    session, not treated as a free read.
    """
    emu = get_emulator_or_404(room_code)
    denied = controller_check(emu)
    if denied:
        return denied
    try:
        sav_bytes = emu.extract_sav()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    rom_name = emu.current_rom_name()
    download_name = (Path(rom_name).stem if rom_name else "save") + ".sav"
    return send_file(
        io.BytesIO(sav_bytes),
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=download_name,
    )



# --- WebSocket ------------------------------------------------------------

def ws_handler(ws, room_code=None):
    emu = get_emulator(room_code)
    if emu is None:
        return  # room doesn't exist (or expired) - close immediately, nothing to stream
    if not room_code and not shared_game_state["enabled"]:
        # The shared game specifically has been turned off by an admin -
        # private rooms (which need a semi-secret code to reach at all)
        # are unaffected. A distinct close code lets a legitimate client
        # show a clear message instead of retrying forever against a
        # connection that's deliberately unavailable right now.
        try:
            ws.close(reason=SHARED_DISABLED_CLOSE_CODE, message="Shared game is currently disabled")
        except Exception:
            pass
        return
    client_id = request.args.get("client_id", "")
    emu.add_client(ws, client_id, remote_addr=request.remote_addr)
    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            # Expect small text messages like "press:a" / "release:left" /
            # "chat:hello everyone" - chat is intentionally NOT gated by
            # is_controller(), unlike button input; everyone connected can
            # chat regardless of who currently holds control.
            if isinstance(msg, str) and ":" in msg:
                action, value = msg.split(":", 1)
                if action == "press":
                    if emu.is_controller(ws):
                        emu.press(value)
                elif action == "release":
                    if emu.is_controller(ws):
                        emu.release(value)
                elif action == "chat":
                    # Payload is JSON: {"name": "...", "text": "..."} - name
                    # is optional (empty/absent falls back to the role-based
                    # "Controller"/"Viewer" label, handled in add_chat_message).
                    try:
                        chat_data = json.loads(value)
                        emu.add_chat_message(
                            ws, chat_data.get("text", ""), chat_data.get("name")
                        )
                    except (json.JSONDecodeError, AttributeError):
                        # Fallback: treat the whole value as plain text, in
                        # case an older client sends the un-JSON-wrapped format.
                        emu.add_chat_message(ws, value)
                elif action == "requestcontrol":
                    emu.request_control(ws)
                elif action == "grantcontrol":
                    emu.grant_control(ws)
    finally:
        emu.remove_client(ws)


# NOTE: flask_sock's route() decorator does not return the original function
# (unlike Flask's own @app.route, which is written specifically to support
# stacking). Chaining two @sock.route(...) decorators on one function silently
# breaks - the outer one ends up registering None as the handler. Registering
# the same function via two separate explicit calls avoids that entirely.
sock.route("/ws")(ws_handler)
sock.route("/r/<room_code>/ws", endpoint="ws_handler_room")(ws_handler)

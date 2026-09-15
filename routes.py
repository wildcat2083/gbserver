import json
import random
import io
from pathlib import Path

from flask import jsonify, make_response, render_template, request, send_from_directory, send_file

from app import app, limiter, sock
from config import (
    FAST_FORWARD_SPEED,
    MAX_ROOMS,
    OFFLINE_CLOSE_CODE,
    OFFLINE_FLAG_PATH,
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


@app.route("/")
def index():
    if not shared_game_state["enabled"]:
        response = make_response(render_template(
            "index.html", room_code=None, room_missing=False, shared_disabled=True,
        ))
    else:
        response = make_response(render_template("index.html", room_code=None, room_missing=False))

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
    get_emulator_or_404(room_code)
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

    load_save = bool(data.get("load_save", False))
    try:
        save_load_error = emu.load_rom(filename, load_save=load_save)
    except FileNotFoundError:
        return jsonify({"error": "rom not found"}), 404
    if save_load_error:

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

    return jsonify({"ok": True, "parsed": parsed})


@app.route("/api/rom/<path:filename>/engine", methods=["GET", "POST"])
@app.route("/r/<room_code>/api/rom/<path:filename>/engine", methods=["GET", "POST"])
@limiter.limit("30 per minute")
def api_rom_engine(filename, room_code=None):

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

            return jsonify({
                "ok": False,
                "error": f"Uploaded, but couldn't be loaded - it may be corrupted or from an incompatible save/ROM version: {save_load_error}",
            }), 400

        return jsonify({"ok": True, "rom": rom_name})

    try:
        emu.delete_save()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/convert-save", methods=["POST"])
@app.route("/r/<room_code>/api/convert-save", methods=["POST"])
@limiter.limit("30 per minute")
def api_convert_save(room_code=None):
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

    return jsonify({"ok": True, "rom": rom_name})


@app.route("/api/sav", methods=["GET"])
@app.route("/r/<room_code>/api/sav", methods=["GET"])
@limiter.limit("30 per minute")
def api_download_sav(room_code=None):
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


def ws_handler(ws, room_code=None):
    if OFFLINE_FLAG_PATH.exists():
        try:
            ws.close(reason=OFFLINE_CLOSE_CODE, message="gbserver is offline")
        except Exception:
            pass
        return
    emu = get_emulator(room_code)
    if emu is None:
        return
    if not room_code and not shared_game_state["enabled"]:

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

            if isinstance(msg, str) and ":" in msg:
                action, value = msg.split(":", 1)
                if action == "press":
                    if emu.is_controller(ws):
                        emu.press(value)
                elif action == "release":
                    if emu.is_controller(ws):
                        emu.release(value)
                elif action == "chat":

                    try:
                        chat_data = json.loads(value)
                        emu.add_chat_message(
                            ws, chat_data.get("text", ""), chat_data.get("name")
                        )
                    except (json.JSONDecodeError, AttributeError):

                        emu.add_chat_message(ws, value)
                elif action == "requestcontrol":
                    emu.request_control(ws)
                elif action == "grantcontrol":
                    emu.grant_control(ws)
    finally:
        emu.remove_client(ws)


sock.route("/ws")(ws_handler)
sock.route("/r/<room_code>/ws", endpoint="ws_handler_room")(ws_handler)

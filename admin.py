import hmac
import json
import os
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import jsonify, render_template, request

from app import app, limiter
from config import BASE_DIR, MAX_ROOMS, OFFLINE_CLOSE_CODE, OFFLINE_FLAG_PATH, safe_rom_name, safe_rom_path
from engine_config import BOYTACEAN_AVAILABLE
from rooms import default_emu, get_emulator, rooms, rooms_lock, create_room, shared_game_state

_INSECURE_ADMIN_TOKENS = {"", "changeme", "change-me", "changeme123", "admin", "password"}
MIN_ADMIN_TOKEN_LENGTH = 32

ADMIN_TOKEN = os.environ.get("GBSERVER_ADMIN_TOKEN", "").strip()
if ADMIN_TOKEN.lower() in _INSECURE_ADMIN_TOKENS or len(ADMIN_TOKEN) < MIN_ADMIN_TOKEN_LENGTH:
    if ADMIN_TOKEN:
        print(
            "[warn] GBSERVER_ADMIN_TOKEN is a placeholder or shorter than "
            f"{MIN_ADMIN_TOKEN_LENGTH} characters - admin API disabled. "
            "Generate one with: openssl rand -hex 32"
        )
    else:
        print("[warn] GBSERVER_ADMIN_TOKEN is not set - admin API disabled.")
    ADMIN_TOKEN = None


CERT_PATHS = {
    "gbserver.wulfpax-labs.com (public)": os.environ.get(
        "GBSERVER_PUBLIC_CERT_PATH",
        "/etc/letsencrypt/live/gbserver.wulfpax-labs.com/cert.pem",
    ),
    "gbserver-internal.wulfpax-labs.com (LAN)": os.environ.get(
        "GBSERVER_INTERNAL_CERT_PATH",
        "/etc/nginx/certs/gbserver.crt",
    ),
}


CERT_WARNING_DAYS = 30
CERT_CRITICAL_DAYS = 7


def _cert_expiry_info(label, path):
    try:
        result = subprocess.run(
            ["sudo", "-n", "openssl", "x509", "-enddate", "-noout", "-in", path],
            capture_output=True, text=True, timeout=5, check=True,
        )
        raw = result.stdout.strip()
        value = raw.split("=", 1)[1].strip()
        expires = datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days_remaining = (expires - datetime.now(timezone.utc)).days
        if days_remaining <= CERT_CRITICAL_DAYS:
            status = "critical"
        elif days_remaining <= CERT_WARNING_DAYS:
            status = "warning"
        else:
            status = "ok"
        return {
            "label": label,
            "expires": expires.strftime("%Y-%m-%d"),
            "days_remaining": days_remaining,
            "status": status,
            "error": None,
        }
    except Exception as e:

        return {
            "label": label,
            "expires": None,
            "days_remaining": None,
            "status": "error",
            "error": str(e),
        }


def _all_cert_expiry_info():
    return [_cert_expiry_info(label, path) for label, path in CERT_PATHS.items()]


def _is_admin_request():
    if not ADMIN_TOKEN:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    provided = auth[len("Bearer "):]
    return hmac.compare_digest(provided, ADMIN_TOKEN)


action_log = deque(maxlen=200)


def _log_action(action, detail=""):
    action_log.appendleft({"ts": time.time(), "action": action, "detail": detail})


BLOCKED_IPS_PATH = BASE_DIR / "blocked_ips.json"


def _load_blocked_ips():
    try:
        return set(json.loads(BLOCKED_IPS_PATH.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def _save_blocked_ips(ips):
    BLOCKED_IPS_PATH.write_text(json.dumps(sorted(ips)))


blocked_ips = _load_blocked_ips()


def _is_offline():
    return OFFLINE_FLAG_PATH.exists()


def _set_offline(offline):
    if offline:
        OFFLINE_FLAG_PATH.write_text("offline")
    else:
        try:
            OFFLINE_FLAG_PATH.unlink()
        except FileNotFoundError:
            pass


OFFLINE_ALLOW_PREFIXES = (
    "/dashboard",
    "/api/dashboard/stats",
    "/api/admin/",
    "/static/",
)


def _offline_gate():
    if not _is_offline():
        return None
    path = request.path
    if any(path == prefix or path.startswith(prefix) for prefix in OFFLINE_ALLOW_PREFIXES):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "gbserver is offline", "offline": True}), 503
    return render_template("offline.html"), 503


@app.before_request
def _reject_blocked_ips():
    if request.remote_addr in blocked_ips:
        return jsonify({"error": "forbidden"}), 403
    blocked = _offline_gate()
    if blocked:
        return blocked


def _emu_stats(emu, include_clients=False):
    with emu.clients_lock:
        clients_snapshot = list(emu.clients.items())
    total_clients = len(clients_snapshot)
    stats = {
        "current_rom": emu.current_rom_name(),
        "engine": emu.engine_name,
        "fast_forward": emu.fast_forward,
        "total_clients": total_clients,

        "viewer_count": max(0, total_clients - 1),
        "has_controller": total_clients > 0,
    }
    if include_clients:
        stats["clients"] = [
            {
                "client_id": cid,
                "role": "controller" if i == 0 else "viewer",
                "remote_addr": emu._client_remote_addrs.get(ws),
            }
            for i, (ws, cid) in enumerate(clients_snapshot)
        ]
    return stats


@app.route("/api/dashboard/stats")
def api_dashboard_stats():
    is_admin = _is_admin_request()
    shared_stats = _emu_stats(default_emu, include_clients=is_admin)

    with rooms_lock:
        room_items = list(rooms.items())

    now = time.time()
    room_stats = []
    for i, (code, emu) in enumerate(room_items, start=1):
        stats = _emu_stats(emu, include_clients=is_admin)
        stats["label"] = f"Room {i}"
        stats["idle_seconds"] = round(now - emu.last_activity, 1)
        if is_admin:
            stats["code"] = code
        room_stats.append(stats)

    payload = {
        "boytacean_available": BOYTACEAN_AVAILABLE,
        "max_rooms": MAX_ROOMS,
        "active_room_count": len(room_stats),
        "shared": shared_stats,
        "rooms": room_stats,
        "is_admin": is_admin,
        "shared_game_enabled": shared_game_state["enabled"],
        "offline": _is_offline(),
    }
    if is_admin:
        payload["blocked_ips"] = sorted(blocked_ips)
        payload["certificates"] = _all_cert_expiry_info()
    return jsonify(payload)


@app.route("/api/admin/shared-game/toggle", methods=["POST"])
@limiter.limit("30 per minute")
def api_admin_toggle_shared_game():
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    data = request.get_json(force=True) or {}
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be true or false"}), 400
    shared_game_state["enabled"] = enabled
    _log_action("enable_shared_game" if enabled else "disable_shared_game")
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/api/admin/offline", methods=["POST"])
@limiter.limit("30 per minute")
def api_admin_offline():
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    _set_offline(True)
    with rooms_lock:
        all_emus = [default_emu] + list(rooms.values())
    for emu in all_emus:
        try:
            emu.close_all_clients(
                close_code=OFFLINE_CLOSE_CODE, message="gbserver is offline"
            )
        except Exception as e:
            print(f"[warn] error disconnecting clients while going offline: {e}")
    _log_action("offline", "server taken offline")
    return jsonify({"ok": True})


@app.route("/api/admin/online", methods=["POST"])
@limiter.limit("30 per minute")
def api_admin_online():
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    _set_offline(False)
    _log_action("online", "server brought back online")
    return jsonify({"ok": True})


@app.route("/api/admin/kick", methods=["POST"])
@limiter.limit("30 per minute")
def api_admin_kick():
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    data = request.get_json(force=True) or {}
    client_id = data.get("client_id")
    room_code = data.get("room")
    if not client_id:
        return jsonify({"error": "client_id is required"}), 400
    emu = get_emulator(room_code)
    if emu is None:
        return jsonify({"error": "no such room"}), 404
    kicked = emu.kick_client(client_id)
    if not kicked:
        return jsonify({"error": "that client is no longer connected"}), 404
    _log_action("kick", f"client_id={client_id} room={room_code or 'shared'}")
    return jsonify({"ok": True})


@app.route("/api/admin/redirect-to-room", methods=["POST"])
@limiter.limit("30 per minute")
def api_admin_redirect():
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    data = request.get_json(force=True) or {}
    client_id = data.get("client_id")
    room_code = data.get("room")
    if not client_id:
        return jsonify({"error": "client_id is required"}), 400
    emu = get_emulator(room_code)
    if emu is None:
        return jsonify({"error": "no such room"}), 404

    if not emu.has_client(client_id):
        return jsonify({"error": "that client is no longer connected"}), 404

    new_code = create_room()
    if new_code is None:
        return jsonify({"error": "all rooms are full right now - can't create a new one"}), 429

    moved = emu.redirect_client(client_id, new_code)
    if not moved:

        with rooms_lock:
            leftover = rooms.pop(new_code, None)
        if leftover is not None:
            try:
                leftover.shutdown()
            except Exception:
                pass
        return jsonify({"error": "that client disconnected just as we tried to move them"}), 404
    _log_action("redirect", f"client_id={client_id} room={room_code or 'shared'} -> {new_code}")
    return jsonify({"ok": True, "new_room": new_code})


@app.route("/api/admin/block-ip", methods=["POST"])
@limiter.limit("30 per minute")
def api_admin_block_ip():
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    data = request.get_json(force=True) or {}
    ip = (data.get("ip") or "").strip()
    if not ip:
        return jsonify({"error": "ip is required"}), 400
    if ip == request.remote_addr:

        return jsonify({"error": "refusing to block your own current IP - this would lock you out with no way back in"}), 400
    blocked_ips.add(ip)
    _save_blocked_ips(blocked_ips)
    _log_action("block_ip", ip)
    return jsonify({"ok": True, "blocked_ips": sorted(blocked_ips)})


@app.route("/api/admin/unblock-ip", methods=["POST"])
@limiter.limit("30 per minute")
def api_admin_unblock_ip():
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    data = request.get_json(force=True) or {}
    ip = (data.get("ip") or "").strip()
    if not ip:
        return jsonify({"error": "ip is required"}), 400
    blocked_ips.discard(ip)
    _save_blocked_ips(blocked_ips)
    _log_action("unblock_ip", ip)
    return jsonify({"ok": True, "blocked_ips": sorted(blocked_ips)})


@app.route("/api/admin/rom/<path:filename>", methods=["DELETE"])
@limiter.limit("30 per minute")
def api_admin_delete_rom(filename):
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    try:
        filename = safe_rom_name(filename)
        rom_path = safe_rom_path(filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not rom_path.exists():
        return jsonify({"error": "no such ROM"}), 404

    with rooms_lock:
        all_emus = [default_emu] + list(rooms.values())
    for emu in all_emus:
        if emu.current_rom_name() == filename:
            try:
                emu.stop()
            except Exception:
                pass

    default_emu.delete_rom(filename)
    _log_action("delete_rom", filename)
    return jsonify({"ok": True})


@app.route("/api/admin/action-log")
def api_admin_action_log():
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    return jsonify({"entries": list(action_log)})


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

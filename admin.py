"""
A read-only developer dashboard - live counts of active rooms, connected
clients (controller + viewers), current ROM/engine per session, etc. -
plus a handful of admin-only moderation actions: kicking or redirecting
a specific client, toggling the shared game on/off, blocking an IP
outright, managing the ROM library, and a log of what's been done.

Deliberately never exposes actual room codes in its output by default -
a room's code is effectively its access link, so leaking it here would
let anyone who finds this dashboard join a "private" room they were never
given the link to. Rooms are shown as anonymized labels ("Room 1", "Room
2") instead, assigned fresh on every request in whatever order the rooms
dict currently iterates - not a stable identity across refreshes, just
enough to tell them apart within one snapshot.

No login system - the dashboard itself is deliberately public (a choice
made when this was first built), but real room codes and every action
below are still gated behind a single shared admin token, set via the
GBSERVER_ADMIN_TOKEN environment variable. If that variable is never set,
the reveal feature and every admin action simply don't exist - there's no
default/guessable token to fall back to (fail closed, not fail open).
"""
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
from config import BASE_DIR, MAX_ROOMS, ROMS_DIR
from engine_config import BOYTACEAN_AVAILABLE
from rooms import default_emu, get_emulator, rooms, rooms_lock, create_room, shared_game_state

ADMIN_TOKEN = os.environ.get("GBSERVER_ADMIN_TOKEN")  # unset = every admin feature below is disabled

# --- TLS certificate expiry monitoring --------------------------------------
# Both hostnames' certificate paths - overridable via env vars in case the
# actual paths ever differ from this deployment's current setup, but
# defaulting to what's actually referenced in gbserver.conf right now.
#
# SETUP NOTE: reading these requires a sudo rule, since this process runs
# as an unprivileged user and Let's Encrypt's directory is root-only by
# default. Add to /etc/sudoers.d/gbserver-certs (via `sudo visudo -f
# /etc/sudoers.d/gbserver-certs`, which validates syntax before saving -
# don't hand-edit the file directly):
#
#   luna ALL=(root) NOPASSWD: /usr/bin/openssl x509 -enddate -noout -in /etc/letsencrypt/live/gbserver.wulfpax-labs.com/cert.pem
#   luna ALL=(root) NOPASSWD: /usr/bin/openssl x509 -enddate -noout -in /etc/nginx/certs/gbserver.crt
#
# (replace "luna" with whatever user actually runs this service, per
# gbserver.service's own User= line). Deliberately two exact-match rules,
# no wildcards - each only ever allows running this one specific,
# read-only command against this one specific file, nothing broader.
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
# certbot's own auto-renewal normally triggers around 30 days before
# expiry - still showing a warning past that point is itself a signal
# that renewal isn't actually happening automatically, not just a
# reminder that time is passing. The internal CA cert isn't
# certbot-managed at all, but the same warning still means the same
# thing for it: time to go reissue it manually.
CERT_WARNING_DAYS = 30
CERT_CRITICAL_DAYS = 7


def _cert_expiry_info(label, path):
    """Reads a certificate's expiry date directly via the openssl CLI
    rather than adding a new Python dependency (the `cryptography`
    package) just for this one thing - openssl is already guaranteed to
    be on this machine, since nginx and certbot both depend on it
    themselves.

    Run via sudo specifically because Let's Encrypt's own directory
    (/etc/letsencrypt/live/ and archive/, which live/ symlinks into) is
    deliberately root-only by default, to protect the private keys
    stored alongside the public certs in the same directory - nginx can
    read it because its master process starts as root, but this service
    runs entirely as an unprivileged user and genuinely cannot read
    anything in there without an explicit, narrowly-scoped sudo rule
    (see the setup note where CERT_PATHS is defined above). -n makes
    sudo fail immediately with a clear error if no matching rule exists,
    rather than hanging forever trying to prompt for a password that
    can never arrive inside a web request with no attached terminal."""
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
        # Missing file, unreadable, unparseable output, openssl not
        # found, whatever - surfaced as its own status rather than
        # silently omitted, since "I can't even check this cert" is
        # itself worth knowing about, not something to hide.
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
    """True only if GBSERVER_ADMIN_TOKEN is actually configured AND the
    request's Authorization header carries a matching Bearer token.
    hmac.compare_digest is used instead of == specifically to avoid a
    timing side-channel that could help an attacker guess the token one
    character at a time."""
    if not ADMIN_TOKEN:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    provided = auth[len("Bearer "):]
    return hmac.compare_digest(provided, ADMIN_TOKEN)


# --- Action log ----------------------------------------------------------
# In-memory only, bounded, most-recent-first - resets on restart. Meant as
# a quick "did I already handle this?" reference for whoever's holding the
# admin token, not a durable audit trail; losing it on restart is an
# acceptable tradeoff for not needing any persistence machinery for
# something this low-stakes (contrast with the IP blocklist below, where
# silently losing a block on restart would be a real problem, not just an
# inconvenience).
action_log = deque(maxlen=200)


def _log_action(action, detail=""):
    action_log.appendleft({"ts": time.time(), "action": action, "detail": detail})


# --- IP blocklist ----------------------------------------------------------
# Unlike the action log above, this DOES persist across restarts - a block
# silently disappearing after a routine `systemctl restart` would be a
# real, easy-to-miss problem (the whole point is "keep this address out
# until I say otherwise"), not just a minor inconvenience.
BLOCKED_IPS_PATH = BASE_DIR / "blocked_ips.json"


def _load_blocked_ips():
    try:
        return set(json.loads(BLOCKED_IPS_PATH.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def _save_blocked_ips(ips):
    BLOCKED_IPS_PATH.write_text(json.dumps(sorted(ips)))


blocked_ips = _load_blocked_ips()


@app.before_request
def _reject_blocked_ips():
    """Runs before every single request app-wide (page loads, API calls,
    and WebSocket upgrade requests alike, since a WS upgrade is still a
    normal Flask request up until the point simple_websocket takes over
    the connection) - rejecting a blocked IP here means it never reaches
    any actual route or the WebSocket handler at all."""
    if request.remote_addr in blocked_ips:
        return jsonify({"error": "forbidden"}), 403


def _emu_stats(emu, include_clients=False):
    """Snapshot of one session's stats - reads the client list under its
    own lock rather than assuming len(emu.clients) is safe to read bare
    from another thread. Per-client details (client_id, role, IP) are
    only ever included for an authenticated admin request - like room
    codes, a specific client's identity isn't public dashboard
    information."""
    with emu.clients_lock:
        clients_snapshot = list(emu.clients.items())
    total_clients = len(clients_snapshot)
    stats = {
        "current_rom": emu.current_rom_name(),
        "engine": emu.engine_name,
        "fast_forward": emu.fast_forward,
        "total_clients": total_clients,
        # The first-connected client is always the controller (see
        # is_controller in emulator.py) - everyone else is a viewer.
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


@app.route("/api/admin/kick", methods=["POST"])
@limiter.limit("30 per minute")
def api_admin_kick():
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    data = request.get_json(force=True) or {}
    client_id = data.get("client_id")
    room_code = data.get("room")  # None/omitted means the shared game
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
    room_code = data.get("room")  # source session - None/omitted means the shared game
    if not client_id:
        return jsonify({"error": "client_id is required"}), 400
    emu = get_emulator(room_code)
    if emu is None:
        return jsonify({"error": "no such room"}), 404

    # Checked BEFORE creating the destination room - otherwise a client
    # who's already gone (or the wrong client_id) leaves an orphaned,
    # empty room behind that nobody was ever actually sent to.
    if not emu.has_client(client_id):
        return jsonify({"error": "that client is no longer connected"}), 404

    new_code = create_room()
    if new_code is None:
        return jsonify({"error": "all rooms are full right now - can't create a new one"}), 429

    moved = emu.redirect_client(client_id, new_code)
    if not moved:
        # Rare race - they disconnected in the brief window between the
        # check above and this send. Clean up the now-pointless room
        # rather than leaving it behind for nobody.
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
        # Blocking your own current IP would lock you out of the
        # dashboard entirely, including the unblock action itself, since
        # _reject_blocked_ips runs before every request app-wide - there'd
        # be no way back in short of SSHing in to edit blocked_ips.json
        # by hand. Almost certainly a mistake if it happens, so refused
        # outright rather than trusted.
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
    """Deletes a ROM (and its save) from the shared library outright -
    unlike the player-facing delete (which only exists as an action a
    session's own controller can take on their own session), this can be
    done by an admin regardless of who's playing what, and stops the ROM
    everywhere it's currently loaded first, not just in one session."""
    if not _is_admin_request():
        return jsonify({"error": "not authorized"}), 403
    rom_path = ROMS_DIR / filename
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

    default_emu.delete_rom(filename)  # delete_rom is filesystem-only once nothing's playing it
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

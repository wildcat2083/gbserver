"""Graceful shutdown hook for the Windows auto-update launcher.

The launcher runs the server as a child process and restarts it when a new
version has been downloaded. Killing the process outright would lose any
progress since the last autosave, so instead it calls this endpoint, which
saves and stops every emulator and then exits.

The route only exists when GBSERVER_SUPERVISOR_TOKEN is set (the launcher
generates a random one per start), and the request must carry that token.
On the Pi deployment the variable is never set, so nothing is registered.
"""

import hmac
import os
import threading
import time

from flask import jsonify, request

from app import app, limiter

SUPERVISOR_TOKEN = os.environ.get("GBSERVER_SUPERVISOR_TOKEN", "")


def _shutdown_everything():
    import rooms as rooms_module

    time.sleep(0.2)  # let the HTTP response go out first
    emulators = [rooms_module.default_emu]
    with rooms_module.rooms_lock:
        emulators.extend(rooms_module.rooms.values())
    for emu in emulators:
        try:
            emu.shutdown()  # the worker saves state before it exits
        except Exception as e:
            print(f"[warn] shutdown of an emulator failed: {e}", flush=True)
    print("[info] saved and stopped all sessions - exiting for update/restart", flush=True)
    os._exit(0)


if len(SUPERVISOR_TOKEN) >= 32:

    @app.route("/api/internal/shutdown", methods=["POST"])
    @limiter.exempt
    def api_internal_shutdown():
        supplied = request.headers.get("X-Supervisor-Token", "")
        if not hmac.compare_digest(supplied, SUPERVISOR_TOKEN):
            return jsonify({"error": "forbidden"}), 403
        threading.Thread(target=_shutdown_everything, daemon=True).start()
        return jsonify({"ok": True}), 202

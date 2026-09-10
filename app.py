"""
Headless Game Boy server - runs PyBoy in the background and streams
video frames to any browser on the network over WebSocket, the same
idea as the ESP32 project but running on a Pi / PC instead.

Supports two modes at once:
  - The default shared game at "/" - everyone who visits plays the same
    session together, exactly as before.
  - Private room sessions at "/r/<CODE>" - each room is its own fully
    independent Emulator/PyBoy instance with its own save-state
    namespace, so multiple people can each play their own ROM at the
    same time without stepping on each other. Rooms are created via
    POST /api/rooms and share the same ROM library on disk.

Run with:  python3 app.py
Then open: http://<this-machine's-ip>:8080/  from any device on the LAN.

This module is the entry point gunicorn actually imports (app:app, see
gunicorn.conf.py) - it creates the Flask app, WebSocket, and rate-limiter
objects, then imports rooms.py (to construct the shared game and start the
idle-room reaper) and routes.py (which registers every route onto the app
object created here). The actual logic lives in config.py, engine_config.py,
emulator.py, rooms.py, and routes.py - kept split out for manageability now
that this had grown into a genuine multi-user platform (dual engines,
rooms, chat, rate limiting) rather than a single-purpose script.
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask import Flask
from flask_sock import Sock
from pathlib import Path
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# Without this, Flask sees every request as coming from nginx itself
# (127.0.0.1, since it's the reverse proxy sitting in front of gunicorn on
# the same machine) - meaning every real internet visitor would share the
# exact same rate-limit bucket, since Flask-Limiter's get_remote_address()
# (below) would return the same IP for literally everyone. ProxyFix makes
# request.remote_addr correctly reflect the real client's IP instead, by
# trusting the X-Forwarded-For header nginx sets - x_for=1 means "trust
# exactly one proxy hop," matching this setup (nginx is the only proxy in
# front of gunicorn; a request with more hops than that in its forwarded-for
# chain would be treated as suspicious/untrusted beyond that first hop).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
sock = Sock(app)

# Rate limiting - this is genuinely internet-facing now (see the dual-cert
# nginx setup), so the mutating/expensive endpoints get real per-IP limits
# rather than trusting every request is well-behaved. Keyed by remote
# address; storage is in-memory (fine for a single-process deployment like
# this one - would need a shared backend like Redis if this were ever
# scaled to multiple worker processes/machines).
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["120 per minute"],  # generous default for normal polling/status calls
    storage_uri="memory://",
)


@limiter.request_filter
def _exempt_localhost(*args, **kwargs):
    """Requests genuinely from the machine itself are exempt from every
    rate limit - real internet abuse could never appear as 127.0.0.1/::1
    here, since ProxyFix (above) already resolves the real client IP for
    anything actually coming through nginx. This just means legitimate
    server-side tooling (load-testing scripts, admin automation, etc.)
    run without hitting limits that only ever existed to stop internet-
    facing abuse in the first place."""
    from flask import request
    return request.remote_addr in ("127.0.0.1", "::1")


def _asset_version(filename):
    """Cache-busting version string for a static asset, based on its own
    file modification time - changes automatically the moment the file is
    redeployed, so the browser can never keep serving a stale cached copy
    after an update (a plain refresh doesn't reliably re-fetch static
    files; this makes it a non-issue rather than relying on remembering to
    hard-refresh every time app.js or style.css changes)."""
    try:
        return int((Path(app.static_folder) / filename).stat().st_mtime)
    except OSError:
        return "0"


app.jinja_env.globals["asset_version"] = _asset_version


import rooms as rooms_module  # noqa: E402 - must come after app/sock/limiter above
rooms_module.start_reaper()

import routes  # noqa: F401,E402 - imported for its side effect: registering every route
import admin  # noqa: F401,E402 - same idea: the read-only dashboard routes

if __name__ == "__main__":
    # host=0.0.0.0 so other devices on the LAN can reach it
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)

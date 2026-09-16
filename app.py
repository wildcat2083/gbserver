from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask import Flask
from flask_sock import Sock
import os
from pathlib import Path
from werkzeug.middleware.proxy_fix import ProxyFix

from config import MAX_UPLOAD_BYTES, migrate_roms_folder

migrate_roms_folder()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


# Behind nginx (the Pi), trust one hop of X-Forwarded-* so rate limits and
# IP blocks see the real client. Serving directly (the Windows build), those
# headers come from the client itself and must be ignored, or anyone could
# spoof their address.
if os.environ.get("GBSERVER_BEHIND_PROXY", "1") != "0":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
sock = Sock(app)


limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["120 per minute"],
    storage_uri="memory://",
)


@limiter.request_filter
def _exempt_localhost(*args, **kwargs):
    from flask import request
    return request.remote_addr in ("127.0.0.1", "::1")


def _asset_version(filename):
    try:
        return int((Path(app.static_folder) / filename).stat().st_mtime)
    except OSError:
        return "0"


app.jinja_env.globals["asset_version"] = _asset_version


import rooms as rooms_module
rooms_module.start_reaper()

import routes
import admin
import supervisor_hooks  # noqa: F401  (inactive unless run by the Windows launcher)

if __name__ == "__main__":

    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)

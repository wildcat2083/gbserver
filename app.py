from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask import Flask
from flask_sock import Sock
from pathlib import Path
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)


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

if __name__ == "__main__":

    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)

import multiprocessing

# The app spawns a multiprocessing worker for emulation. Under a
# PyInstaller build on Windows, freeze_support() must run before any
# process/queue is created (it's a no-op when running as plain
# `python windows/run_windows.py`). It also has to run before we import
# the app module, because importing it constructs the default emulator,
# which starts its worker process.
multiprocessing.freeze_support()

import app  # noqa: E402
from app import app as flask_app  # noqa: E402


if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
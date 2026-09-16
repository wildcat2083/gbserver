"""gbserver auto-updating launcher.

This is what gbserver.exe runs. The exe contains only Python and the
libraries gbserver needs - not gbserver's own code. The code is downloaded
from GitHub and kept up to date:

  1. On start, and every few minutes after, it asks GitHub for the latest
     commit on the tracked branch (default: windows-installer). This uses the
     git protocol's ref list, which isn't subject to the REST API's 60/hour
     rate limit.
  2. If there's a newer commit, it downloads that exact commit's source
     archive, checks it, and unpacks it into its own versioned folder.
  3. It restarts the server on the new version - but only when nobody is
     connected, and always through a save-and-exit request first, so no
     progress is lost.
  4. If the new version fails to start, it rolls back to the previous one
     and won't retry that commit.

It never needs GitHub to run: offline, it runs the newest version it already
has, or the copy bundled with the installer on first run.

Layout under %LOCALAPPDATA%\\gbserver\\:
  app\\<commit>\\      downloaded versions (newest few are kept)
  data\\              roms\\, saves\\, blocked_ips.json - never touched by updates
  launcher.json      optional settings (see DEFAULT_SETTINGS)
  state.json         which version is current, previous, and known-bad
  launcher.log       update and restart history

The same exe also runs the server itself: `gbserver.exe --serve <version dir>`.
"""

import multiprocessing

if __name__ == "__main__":
    # Must run before anything else: under a frozen build, the emulator's
    # worker processes re-launch this exe, and freeze_support() hands them
    # off to the worker code instead of starting another launcher.
    multiprocessing.freeze_support()

import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Bump when the bundled libraries change in a way the code depends on (a new
# package, a major upgrade). The repo declares what it needs in
# windows/runtime.json; versions needing a newer runtime aren't applied.
RUNTIME_VERSION = 1

DEFAULT_SETTINGS = {
    "repo": "wildcat2083/gbserver",
    "branch": "windows-installer",
    "port": 8080,
    "check_interval_minutes": 10,
    # Restart onto a downloaded update only after nobody has been connected
    # for this long.
    "idle_minutes_before_update": 2,
    "keep_versions": 3,
    "git_base_url": "https://github.com",
    "archive_base_url": "https://codeload.github.com",
}

APP_NAME = "gbserver"
HEALTH_TIMEOUT_SECONDS = 90
GRACEFUL_EXIT_TIMEOUT_SECONDS = 60
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# ---------------------------------------------------------------------------
# paths, settings, logging
# ---------------------------------------------------------------------------

def is_frozen():
    return bool(getattr(sys, "frozen", False))


def exe_dir():
    return Path(sys.executable).resolve().parent if is_frozen() else Path(__file__).resolve().parent


def root_dir():
    override = os.environ.get("GBSERVER_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


ROOT = root_dir()
VERSIONS_DIR = ROOT / "app"
DATA_DIR = ROOT / "data"
STATE_PATH = ROOT / "state.json"
SETTINGS_PATH = ROOT / "launcher.json"
LOG_PATH = ROOT / "launcher.log"

_log_lock = threading.Lock()


def log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [launcher] {message}"
    with _log_lock:
        print(line, flush=True)
        try:
            ROOT.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if LOG_PATH.stat().st_size > 2 * 1024 * 1024:
                LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
        except OSError:
            pass


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if not SETTINGS_PATH.exists():
        try:
            ROOT.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps(
                {k: DEFAULT_SETTINGS[k] for k in ("branch", "port", "check_interval_minutes")}, indent=2))
        except OSError:
            pass
    try:
        user = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(user, dict):
            settings.update({k: v for k, v in user.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return settings


def load_state():
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            state.setdefault("bad", [])
            return state
    except (OSError, ValueError):
        pass
    return {"current": None, "previous": None, "bad": []}


def save_state(state):
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _http_get(url, timeout=30, max_bytes=MAX_ARCHIVE_BYTES):
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}-launcher"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"download from {url} is larger than {max_bytes} bytes")
    return data


def remote_commit(settings):
    """Latest commit SHA on the tracked branch, via the git smart-HTTP ref list."""
    url = f"{settings['git_base_url']}/{settings['repo']}.git/info/refs?service=git-upload-pack"
    data = _http_get(url, timeout=20, max_bytes=5 * 1024 * 1024)
    wanted = f"refs/heads/{settings['branch']}".encode()
    for line in data.split(b"\n"):
        # pkt-line: 4 hex length digits, 40-char sha, space, ref name (+ NUL caps on the first)
        line = line.split(b"\x00", 1)[0]
        if len(line) >= 45 and line.endswith(wanted):
            candidate = line[-(len(wanted) + 41):-(len(wanted) + 1)].decode("ascii", "replace")
            if SHA_RE.match(candidate):
                return candidate
    raise ValueError(f"branch {settings['branch']!r} not found in {settings['repo']}")


def download_version(settings, sha):
    """Download and unpack one exact commit into VERSIONS_DIR/<sha>. Returns the path."""
    target = VERSIONS_DIR / sha
    if (target / "app.py").exists():
        return target
    url = f"{settings['archive_base_url']}/{settings['repo']}/zip/{sha}"
    log(f"downloading {sha[:10]} from {settings['repo']}")
    data = _http_get(url, timeout=120)
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{sha[:10]}-", dir=VERSIONS_DIR))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            tops = {n.split("/", 1)[0] for n in names if n}
            if len(tops) != 1:
                raise ValueError("unexpected archive layout")
            top = tops.pop()
            staging_resolved = staging.resolve()
            for info in zf.infolist():
                rel = info.filename[len(top) + 1:]
                if not rel:
                    continue
                dest = (staging / rel).resolve()
                if staging_resolved not in dest.parents and dest != staging_resolved:
                    raise ValueError(f"archive entry escapes its folder: {info.filename}")
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
        check_version_dir(staging)
        (staging / ".commit").write_text(sha)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        staging.replace(target)
        return target
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def check_version_dir(path):
    """Refuse code that isn't a gbserver built for this launcher."""
    if not (path / "app.py").is_file() or not (path / "emu_worker.py").is_file():
        raise ValueError("not a gbserver source tree (app.py/emu_worker.py missing)")
    runtime_file = path / "windows" / "runtime.json"
    if not runtime_file.is_file():
        raise ValueError(
            "this version has no windows/runtime.json, so it predates the auto-update "
            "build (it would write data into its own folder) - push the auto-update "
            "changes to the branch"
        )
    try:
        needed = int(json.loads(runtime_file.read_text(encoding="utf-8")).get("runtime_version", 0))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("windows/runtime.json is unreadable")
    if needed > RUNTIME_VERSION:
        raise RuntimeTooOld(needed)


class RuntimeTooOld(ValueError):
    def __init__(self, needed):
        super().__init__(
            f"this version needs launcher runtime {needed}, but this install has runtime "
            f"{RUNTIME_VERSION} - download and run the latest gbserver installer"
        )
        self.needed = needed


def prune_versions(state, keep):
    keep_set = {state.get("current"), state.get("previous")}
    versions = sorted(
        (p for p in VERSIONS_DIR.glob("*") if p.is_dir() and SHA_RE.match(p.name)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for p in versions[keep:]:
        if p.name not in keep_set:
            shutil.rmtree(p, ignore_errors=True)
    for p in VERSIONS_DIR.glob(".*-*"):
        if p.is_dir() and time.time() - p.stat().st_mtime > 3600:
            shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------------------
# first run: bundled seed + data from older installs
# ---------------------------------------------------------------------------

def install_seed(state):
    """Use the source snapshot bundled with the installer if nothing is installed yet."""
    seed = exe_dir() / "seed"
    info_path = seed / "seed.json"
    if not info_path.exists():
        return state
    try:
        sha = json.loads(info_path.read_text(encoding="utf-8"))["commit"]
    except (OSError, ValueError, KeyError):
        return state
    if not SHA_RE.match(sha):
        return state
    current = state.get("current")
    if current and (VERSIONS_DIR / current / "app.py").exists():
        return state
    src = seed / "source"
    try:
        check_version_dir(src)
    except ValueError as e:
        log(f"bundled copy unusable: {e}")
        return state
    target = VERSIONS_DIR / sha
    if not (target / "app.py").exists():
        log(f"installing bundled copy {sha[:10]}")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(src, target)
        (target / ".commit").write_text(sha)
    state["current"] = sha
    save_state(state)
    return state


def migrate_legacy_data():
    """Move roms/ and saves/ from an older build's program folder into DATA_DIR."""
    old = exe_dir()
    moved = []
    for name in ("roms", "saves"):
        src = old / name
        if not src.is_dir():
            continue
        dst = DATA_DIR / name
        for item in src.rglob("*"):
            if item.is_dir() or item.name.startswith(("place-roms-here", "placeholder-for")):
                continue
            rel = item.relative_to(src)
            target = dst / rel
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(item), str(target))
                moved.append(f"{name}/{rel.as_posix()}")
            except OSError as e:
                log(f"couldn't move {item}: {e}")
    for name in ("blocked_ips.json",):
        src, dst = old / name, DATA_DIR / name
        if src.is_file() and not dst.exists():
            shutil.move(str(src), str(dst))
            moved.append(name)
    if moved:
        log(f"moved {len(moved)} file(s) from the old program folder into {DATA_DIR}")


# ---------------------------------------------------------------------------
# running the server
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, version_dir, port):
        self.version_dir = Path(version_dir)
        self.port = port
        self.token = secrets.token_hex(32)
        self.proc = None
        self.started_at = 0.0

    def start(self):
        env = dict(os.environ)
        env.update({
            "GBSERVER_DATA_DIR": str(DATA_DIR),
            "GBSERVER_SUPERVISOR_TOKEN": self.token,
            "GBSERVER_BEHIND_PROXY": "0",
            "PYTHONUNBUFFERED": "1",
        })
        if is_frozen():
            cmd = [sys.executable, "--serve", str(self.version_dir), str(self.port)]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve()), "--serve", str(self.version_dir), str(self.port)]
        kwargs = {}
        if os.name == "nt":
            # Own process group: Ctrl+C in the console reaches only the launcher,
            # which then saves and stops the server properly.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(cmd, env=env, cwd=str(self.version_dir), **kwargs)
        self.started_at = time.time()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _get_json(self, path, timeout=5):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def wait_healthy(self, timeout=HEALTH_TIMEOUT_SECONDS):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running():
                return False
            try:
                self._get_json("/api/config", timeout=3)
                return True
            except Exception:
                time.sleep(1)
        return False

    def connected_clients(self):
        """Total connected players/viewers across the shared game and all rooms, or None if unknown."""
        try:
            stats = self._get_json("/api/dashboard/stats")
        except Exception:
            return None
        total = int(stats.get("shared", {}).get("total_clients", 0))
        for room in stats.get("rooms", []):
            total += int(room.get("total_clients", 0))
        return total

    def stop(self, reason):
        if not self.running():
            return
        log(f"saving and stopping the server ({reason})")
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/internal/shutdown",
                data=b"{}",
                method="POST",
                headers={"X-Supervisor-Token": self.token, "Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            log(f"graceful shutdown request failed ({e}); the last autosave will be used")
        try:
            self.proc.wait(timeout=GRACEFUL_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            log("server didn't exit in time - terminating it")
            self.kill()

    def kill(self):
        if not self.running():
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, check=False)
            else:
                os.killpg(self.proc.pid, signal.SIGKILL)
        except Exception:
            self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def serve(version_dir, port):
    """Child-process entry: run gbserver from a downloaded source folder."""
    version_dir = Path(version_dir).resolve()
    os.chdir(version_dir)
    sys.path.insert(0, str(version_dir))
    # The emulator worker is a separate process. Windows always uses "spawn";
    # forcing it everywhere keeps behaviour identical when testing elsewhere.
    try:
        multiprocessing.set_start_method("spawn")
    except RuntimeError:
        pass
    commit = (version_dir / ".commit").read_text().strip() if (version_dir / ".commit").exists() else "?"
    print(f"[serve] gbserver {commit[:10]} on http://0.0.0.0:{port}/  (data: {os.environ.get('GBSERVER_DATA_DIR')})", flush=True)
    import app as app_module  # noqa: E402  (imports routes/admin and starts the default emulator)

    app_module.app.run(host="0.0.0.0", port=int(port), debug=False, threaded=True, use_reloader=False)


# ---------------------------------------------------------------------------
# supervisor loop
# ---------------------------------------------------------------------------

class Launcher:
    def __init__(self):
        self.settings = load_settings()
        self.state = load_state()
        self.server = None
        self.pending = None           # sha downloaded and ready, waiting for an idle moment
        self.idle_since = None
        self.stop_requested = threading.Event()
        self.last_check = 0.0
        self.warned_runtime = None

    # -- updates -------------------------------------------------------------

    def check_for_update(self):
        self.last_check = time.time()
        try:
            sha = remote_commit(self.settings)
        except Exception as e:
            log(f"update check failed (running the installed version): {e}")
            return
        if sha in (self.state.get("current"), self.pending):
            return
        if sha in self.state.get("bad", []) or sha == self.warned_runtime:
            return
        try:
            download_version(self.settings, sha)
        except RuntimeTooOld as e:
            if self.warned_runtime != sha:
                log(f"update {sha[:10]} not applied: {e}")
                self.warned_runtime = sha
            return
        except Exception as e:
            log(f"couldn't download update {sha[:10]}: {e}")
            if isinstance(e, ValueError):
                self._mark_bad(sha)
            return
        log(f"update {sha[:10]} downloaded - it will be applied when nobody is connected")
        self.pending = sha

    def _mark_bad(self, sha):
        bad = self.state.setdefault("bad", [])
        if sha not in bad:
            bad.append(sha)
            del bad[:-20]
            save_state(self.state)

    # -- server lifecycle ----------------------------------------------------

    def start_version(self, sha):
        version_dir = VERSIONS_DIR / sha
        self.server = Server(version_dir, int(self.settings["port"]))
        log(f"starting gbserver {sha[:10]} on port {self.settings['port']}")
        self.server.start()
        return self.server.wait_healthy()

    def switch_to(self, sha):
        old = self.state.get("current")
        if self.server:
            self.server.stop(f"updating to {sha[:10]}")
        self.state.update({"current": sha, "previous": old})
        save_state(self.state)
        if self.start_version(sha):
            log(f"now running {sha[:10]}")
            prune_versions(self.state, int(self.settings["keep_versions"]))
            return
        log(f"{sha[:10]} failed to start - rolling back to {old[:10] if old else 'nothing'}")
        if self.server:
            self.server.kill()
        self._mark_bad(sha)
        self.state.update({"current": old, "previous": None})
        save_state(self.state)
        if old:
            self.start_version(old)

    def boot(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "roms").mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "saves").mkdir(parents=True, exist_ok=True)
        log(f"gbserver launcher (runtime {RUNTIME_VERSION}) - tracking {self.settings['repo']}@{self.settings['branch']}")
        log(f"ROMs and saves: {DATA_DIR}")
        migrate_legacy_data()
        self.state = install_seed(self.state)
        current = self.state.get("current")
        if current and not (VERSIONS_DIR / current / "app.py").exists():
            log(f"installed version {current[:10]} is missing - will download")
            current = self.state["current"] = None

        self.check_for_update()
        if self.pending:
            # Nothing is running yet, so there's nobody to wait for - start on
            # the newest version straight away (with the old one as fallback).
            sha, self.pending = self.pending, None
            self.state.update({"current": sha, "previous": current})
            save_state(self.state)
            current = sha

        if not current:
            log("no version installed and GitHub unreachable - retrying every 30 seconds")
            while not current and not self.stop_requested.wait(30):
                self.check_for_update()
                if self.pending:
                    current, self.pending = self.pending, None
                    self.state["current"] = current
                    save_state(self.state)
            if not current:
                return False

        if not self.start_version(current):
            previous = self.state.get("previous")
            log(f"{current[:10]} failed to start")
            self.server.kill()
            if previous and (VERSIONS_DIR / previous / "app.py").exists():
                self._mark_bad(current)
                self.state.update({"current": previous, "previous": None})
                save_state(self.state)
                log(f"rolling back to {previous[:10]}")
                self.start_version(previous)
        return True

    def run(self):
        if not self.boot():
            return
        interval = max(0.1, float(self.settings["check_interval_minutes"])) * 60
        idle_needed = max(0, float(self.settings["idle_minutes_before_update"])) * 60
        crash_times = []
        log(f"open http://127.0.0.1:{self.settings['port']}/  -  dashboard: http://127.0.0.1:{self.settings['port']}/dashboard")
        while not self.stop_requested.wait(5):
            if self.server and not self.server.running():
                crash_times = [t for t in crash_times if time.time() - t < 300] + [time.time()]
                code = self.server.proc.returncode if self.server.proc else "?"
                log(f"server exited unexpectedly (code {code}) - restarting")
                if len(crash_times) >= 3 and self.state.get("previous"):
                    bad, prev = self.state["current"], self.state["previous"]
                    log(f"{bad[:10]} crashed 3 times in 5 minutes - rolling back to {prev[:10]}")
                    self._mark_bad(bad)
                    self.state.update({"current": prev, "previous": None})
                    save_state(self.state)
                    crash_times = []
                time.sleep(min(30, 2 ** len(crash_times)))
                self.start_version(self.state["current"])
                continue

            if time.time() - self.last_check >= interval:
                self.check_for_update()

            if self.pending:
                clients = self.server.connected_clients() if self.server else 0
                if clients == 0:
                    self.idle_since = self.idle_since or time.time()
                    if time.time() - self.idle_since >= idle_needed:
                        sha, self.pending, self.idle_since = self.pending, None, None
                        self.switch_to(sha)
                else:
                    self.idle_since = None

    def shutdown(self):
        self.stop_requested.set()
        if self.server:
            self.server.stop("launcher closing")


def main(argv):
    if len(argv) >= 2 and argv[1] == "--serve":
        serve(argv[2], argv[3] if len(argv) > 3 else DEFAULT_SETTINGS["port"])
        return
    if len(argv) >= 2 and argv[1] == "--check-update":
        settings = load_settings()
        print(remote_commit(settings))
        return

    launcher = Launcher()

    def handle_signal(signum, frame):
        log("stop requested")
        launcher.stop_requested.set()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_signal)
    try:
        launcher.run()
    finally:
        launcher.shutdown()
        log("launcher stopped")


if __name__ == "__main__":
    main(sys.argv)

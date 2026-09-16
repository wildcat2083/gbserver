# gbserver for Windows (auto-updating)

The Windows build never goes stale. `gbserver.exe` contains Python and the
libraries gbserver needs, but not gbserver's own code: it downloads that from
the `windows-installer` branch on GitHub and keeps it current.

## How updates work

- On start, and every 10 minutes, the launcher checks the branch's latest
  commit. It uses git's ref list rather than the GitHub API, so the API's
  60-requests-per-hour limit doesn't apply.
- A new commit is downloaded as that exact commit's source archive and
  unpacked into its own folder.
- The server switches to it once nobody has been connected for 2 minutes.
  Before stopping, the server saves every running game, so no progress is
  lost. If nobody is connected at startup, the newest version is used
  straight away.
- If the new version fails to start, or crashes 3 times within 5 minutes,
  the launcher rolls back to the previous version and skips that commit
  until a newer one is pushed.
- Offline, it runs the newest version it already has. A fresh install
  includes a snapshot of the code, so even the first launch works without
  internet.

**Anything pushed to `windows-installer` reaches every Windows install within
minutes**, so push only what you'd be happy to run.

## Where things live

| Path | What |
|---|---|
| `%LOCALAPPDATA%\Programs\gbserver\` | `gbserver.exe` and its libraries (the installer) |
| `%LOCALAPPDATA%\gbserver\data\` | `roms\`, `saves\`, `blocked_ips.json` - never touched by updates, reinstalls or uninstalling |
| `%LOCALAPPDATA%\gbserver\app\<commit>\` | downloaded code versions (the newest 3 are kept) |
| `%LOCALAPPDATA%\gbserver\launcher.log` | update, restart and rollback history |
| `%LOCALAPPDATA%\gbserver\launcher.json` | settings |

The Start Menu has shortcuts to the data folder and the update log. Installs
from the older bundled build have their `roms\` and `saves\` moved into the
data folder automatically on first launch.

## Settings (`launcher.json`)

Created on first run. Edit it and restart gbserver:

```json
{
  "branch": "windows-installer",
  "port": 8080,
  "check_interval_minutes": 10
}
```

Also accepted: `"repo"` (default `wildcat2083/gbserver`),
`"idle_minutes_before_update"` (default 2) and `"keep_versions"` (default 3).

## Building the installer

Needs 64-bit Python 3.11+, git, and [Inno Setup 6](https://jrsoftware.org/isdl.php).
From the repo root:

```
windows\build_installer.cmd
```

This produces `windows\Output\gbserver-setup.exe`. It bundles an offline
snapshot of the **committed** code (`git archive HEAD`), so commit before
building.

**You only need a new installer when the runtime changes**: when the code
starts using a Python package that isn't bundled, or needs a newer version of
one. In that case:

1. Add the package to `windows\requirements-win.txt`.
2. Bump `RUNTIME_VERSION` in `windows\launcher.py` and `runtime_version` in
   `windows\runtime.json` to the same new number.
3. Build and install the new installer.

Installs with the older runtime keep running their current version and log
that a new installer is needed, instead of breaking.

## Requirements for the branch

The launcher refuses any commit that doesn't include `windows/runtime.json`,
because older code writes ROMs and saves into its own (disposable) folder.
Everything else in the branch is the normal gbserver code. The Windows-specific
pieces are:

- `windows/` - launcher, PyInstaller spec, build scripts, installer script
- `config.py` honours `GBSERVER_DATA_DIR`
- `app.py` skips proxy-header trust when `GBSERVER_BEHIND_PROXY=0` (no nginx in
  front, so those headers could be spoofed)
- `supervisor_hooks.py` - the save-and-exit endpoint the launcher uses before
  restarting; only active when the launcher starts the server

All of these are harmless on the Pi, so `main` carries them too and syncing is
a plain merge.

## Command line

```
gbserver.exe                  run with auto-update (normal use)
gbserver.exe --check-update   print the latest commit on the tracked branch
```

Press Ctrl+C in the console window to save and stop. Closing the window with
the X skips the save; the last autosave (every 5 minutes) is kept.

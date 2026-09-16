# AGENTS.md

## Editing live files — required workflow

Never modify files in this repository (or anywhere on this machine) even when
the user's prompt asks for a fix. Always:

1. Investigate the problem and explain the root cause.
2. Present the exact change you would make.
3. Wait for the user to explicitly approve editing live files before
   touching anything on disk.

This applies to every edit, no matter how small. The only exception is when
the user explicitly says yes to a specific edit.

## Project notes

- Headless Game Boy emulator server (Flask + PyBoy/boytacean worker process)
  that streams video/audio over WebSockets. Themed front end with selectable
  color schemes.
- Static assets are versioned by file mtime (`?v={{ asset_version(...) }}`),
  so CSS/JS changes take effect on the next (hard) page reload — restarting
  the gunicorn service is NOT required for static file changes.
- Deployed via gunicorn (single worker, gthread) behind nginx; dev server:
  `python3 app.py` on port 8080.
- Known Firefox quirk: selector lists containing the unknown
  `:-webkit-full-screen` pseudo-class are dropped entirely by Firefox. Keep
  `:fullscreen` and `:-webkit-full-screen` in separate rules.
- Fullscreen overlay controls (`#gameStage:fullscreen .emulator-controls .ff-btn`)
  are intentionally styled with fixed light-on-dark colors since the
  fullscreen backdrop is always black regardless of theme.
- Windows build (`windows/`) is auto-updating: gbserver.exe is a runtime +
  launcher that downloads this branch from GitHub into
  `%LOCALAPPDATA%\gbserver\app\<commit>` and runs it; ROMs/saves live in
  `%LOCALAPPDATA%\gbserver\data`. Every push to this branch ships to all
  Windows installs. A commit must contain `windows/runtime.json` or the
  launcher ignores it. See `windows/README.md`.

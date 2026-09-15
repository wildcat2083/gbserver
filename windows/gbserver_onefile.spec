# -*- mode: python ; coding: utf-8 -*-
# gbserver PyInstaller spec - one-FILE build.
#
# Produces a single dist/gbserver.exe containing everything except the
# ROM library (roms\ + saves\ are created/copied next to the exe at
# first launch, keeping the exe size down).
#
# Note: the emulator spawns a multiprocessing worker; PyInstaller injects
# a runtime hook (pyi_rth_multiprocessing) so spawn works in one-file mode
# on Windows, but each launch re-extracts the bundle to a temp dir (a few
# seconds slower to start, and the child reuses the same extraction).
# The one-folder build is the more conservative option for this app.
#
# Build with (from the project root):
#   python -m PyInstaller --noconfirm --clean windows/gbserver_onefile.spec
# Output: dist/gbserver.exe


import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH).resolve()))

import spec_common  # noqa: E402

spec_dir, project_root = spec_common.resolve(SPECPATH)

a = Analysis(
    [str(spec_dir / "run_windows.py")],
    **spec_common.analysis_kwargs(SPECPATH),
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# One-file mode: everything goes inside the single EXE (no COLLECT).
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="gbserver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
# -*- mode: python ; coding: utf-8 -*-
# gbserver PyInstaller spec - one-FOLDER build (the recommended, stable mode
# for the multiprocessing emulator worker).
#
# Build with (from the project root):
#   python -m PyInstaller --noconfirm --clean windows/gbserver.spec
# Output: dist/gbserver/gbserver.exe  (plus _internal/, roms\ and saves\
#          are copied next to it afterwards by build_exe.cmd)


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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="gbserver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name="gbserver",
)
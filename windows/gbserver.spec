# -*- mode: python ; coding: utf-8 -*-
# gbserver auto-updating runtime - one-folder build.
#
# Build (from the project root):   windows\build_exe.cmd
# Output: dist\gbserver\gbserver.exe + _internal\
#
# The exe runs windows/launcher.py, which downloads gbserver's code from
# GitHub and keeps it up to date. See spec_common.py for what's bundled.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH).resolve()))

import spec_common  # noqa: E402

spec_dir, project_root = spec_common.resolve(SPECPATH)

a = Analysis(
    [str(spec_dir / "launcher.py")],
    **spec_common.analysis_kwargs(SPECPATH),
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="gbserver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="gbserver",
)

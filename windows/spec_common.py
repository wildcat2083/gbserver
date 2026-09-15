"""Shared path/config logic for the gbserver PyInstaller spec files.

Kept as a plain module so both windows/gbserver.spec (one-folder) and
windows/gbserver_onefile.spec (one-file) stay in sync.
"""

from pathlib import Path

BASE_HIDDEN_IMPORTS = [
    "multiprocessing",
    "multiprocessing.spawn",
    "flask_sock",
    "simple_websocket",
    "flask_limiter",
    "flask_limiter.util",
]


def resolve(spec_path):
    """Return (spec_dir, project_root) absolute Paths.

    PyInstaller >=6 resolves paths inside a spec relative to the spec's
    own directory (SPECPATH), not the CWD. The spec always lives in the
    project's "windows/" subfolder, so the project root is its parent.
    """
    spec_dir = Path(spec_path).resolve()
    project_root = spec_dir.parent
    if not (project_root / "app.py").exists():
        raise SystemExit(
            f"[gbserver.spec] project root not found (expected app.py under {project_root})"
        )
    return spec_dir, project_root


def _collect_pyboy():
    """Pull in PyBoy's submodules and data files.

    PyBoy keeps plug-in window backends, support modules and a default
    ROM (default_rom.gb) inside its package, some reached dynamically.
    Static analysis alone can silently leave those out of a frozen build,
    with the UI working but the emulator never producing video - so
    explicitly collect them.
    """
    try:
        from PyInstaller.utils.hooks import collect_data_files, collect_submodules
    except Exception:
        collect_data_files = collect_submodules = lambda name: []
    return collect_submodules("pyboy"), collect_data_files("pyboy")


def analysis_kwargs(spec_path):
    spec_dir, project_root = resolve(spec_path)
    pyboy_hidden, pyboy_datas = _collect_pyboy()
    return {
        "pathex": [str(project_root)],
        "binaries": [],
        # Flask locates templates/ and static/ relative to its module, so
        # bundling them at the archive root puts them next to the app
        # module inside _internal/ once frozen.
        "datas": [
            (str(project_root / "templates"), "templates"),
            (str(project_root / "static"), "static"),
        ] + pyboy_datas,
        "hiddenimports": BASE_HIDDEN_IMPORTS + pyboy_hidden,
        "hookspath": [],
        "runtime_hooks": [],
        "excludes": ["gunicorn", "setproctitle"],
        "win_no_prefer_redirects": False,
        "win_private_assemblies": False,
        "cipher": None,
        "noarchive": False,
    }
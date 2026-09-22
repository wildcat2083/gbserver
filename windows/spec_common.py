"""Shared PyInstaller configuration for the auto-updating gbserver.exe.

The exe freezes windows/launcher.py plus every library gbserver's code
could import - but none of gbserver's own modules, templates or static
files. Those are downloaded from GitHub at run time, so PyInstaller never
sees their imports. Everything they might need therefore has to be bundled
explicitly:

  * the whole Python standard library (minus GUI/test/dev-only parts), and
  * every package in requirements-win.txt together with all of its
    dependencies, including their data files and native libraries.

If a future version of the code adds a new dependency: add it to
requirements-win.txt, bump RUNTIME_VERSION in launcher.py and
runtime_version in runtime.json, and rebuild the installer.
"""

import importlib.util
import re
import sys
from pathlib import Path

STDLIB_EXCLUDE = {
    "antigravity", "this", "idlelib", "tkinter", "turtle", "turtledemo", "test",
    "lib2to3", "ensurepip", "venv", "pydoc_data", "__phello__", "_testcapi",
    "_testinternalcapi", "_testbuffer", "_testimportmultiple", "_testmultiphase",
    "_xxtestfuzz", "xxsubtype", "_ctypes_test", "unittest", "doctest", "pdb",
    "curses", "_curses", "_curses_panel", "readline", "dbm", "_dbm", "_gdbm",
}

# Build tooling that must never end up inside the runtime.
PACKAGE_EXCLUDE = {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
                   "pywin32-ctypes", "macholib", "setuptools", "pip", "wheel"}


def resolve(spec_path):
    spec_dir = Path(spec_path).resolve()
    project_root = spec_dir.parent
    if not (spec_dir / "launcher.py").exists():
        raise SystemExit(f"[spec] launcher.py not found in {spec_dir}")
    return spec_dir, project_root


def _requirement_names(req_file):
    names = []
    for raw in Path(req_file).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!~\[; ]", line, 1)[0].strip()
        if name:
            names.append(name)
    return names


def _canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependency_closure(names):
    from importlib import metadata

    seen, stack = set(), [_canon(n) for n in names]
    while stack:
        name = stack.pop()
        if name in seen or name in PACKAGE_EXCLUDE:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            print(f"[spec] warning: {name} is not installed in the build environment")
            continue
        seen.add(name)
        for req in dist.requires or []:
            if "extra ==" in req:
                continue
            dep = re.split(r"[<>=!~\[; (]", req, 1)[0].strip()
            if dep:
                stack.append(_canon(dep))
    return seen


def _top_level_modules(dist_names):
    from importlib import metadata

    mapping = metadata.packages_distributions()  # module -> [dist names]
    modules = set()
    for module, dists in mapping.items():
        if any(_canon(d) in dist_names for d in dists) and not module.startswith("_distutils"):
            modules.add(module)
    return sorted(modules)


def _stdlib_imports():
    from PyInstaller.utils.hooks import collect_submodules

    names = []
    for name in sorted(getattr(sys, "stdlib_module_names", ())):
        if name in STDLIB_EXCLUDE or name.startswith("_test"):
            continue
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if spec is None:
            continue
        names.append(name)
        if spec.submodule_search_locations:
            names.extend(
                collect_submodules(
                    name,
                    filter=lambda m: not any(p in STDLIB_EXCLUDE or p in ("tests", "idle_test") for p in m.split(".")),
                    on_error="ignore",
                )
            )
    return names


def analysis_kwargs(spec_path):
    from PyInstaller.utils.hooks import collect_all

    spec_dir, _ = resolve(spec_path)
    dists = _dependency_closure(_requirement_names(spec_dir / "requirements-win.txt"))
    hidden, datas, binaries = [], [], []
    for module in _top_level_modules(dists):
        try:
            d, b, h = collect_all(module)
        except Exception as e:
            print(f"[spec] warning: couldn't collect {module}: {e}")
            continue
        datas += d
        binaries += b
        hidden += [module] + h
    hidden += _stdlib_imports()
    print(f"[spec] bundling {len(dists)} distributions, {len(hidden)} modules")
    return {
        "pathex": [str(spec_dir)],
        "binaries": binaries,
        "datas": datas,
        "hiddenimports": sorted(set(hidden)),
        "hookspath": [],
        "runtime_hooks": [],
        "excludes": sorted(STDLIB_EXCLUDE | {"gunicorn", "setproctitle", "PyInstaller"}),
        "noarchive": False,
    }

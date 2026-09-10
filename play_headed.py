#!/usr/bin/env python3
"""
play_headed.py - Play a Game Boy ROM locally, headed, in a real on-screen
window via PyBoy's own SDL2 support.

This is a completely standalone script - it does NOT import, run, or talk
to any part of gbserver (no worker process, no WebSocket, no web server
involvement at all). It can be run whether gbserver is running or not.

It DOES point at the same roms/ and saves/ folders gbserver already uses,
and uses the exact same save-file naming convention (<rom name>.state,
via PyBoy's save_state()/load_state() - a full emulator snapshot, NOT the
same thing as PyBoy's own separate cartridge-RAM autosave). That's purely
a file-sharing convenience so a save started on the web version can be
resumed here, and vice versa - the two never run at the same time against
the same save file, and there's no live coordination between them.

Requires an actual display to render to - either a monitor plus a running
X11/Wayland session, or SDL2's KMSDRM driver for a direct framebuffer
console with no desktop environment. If PyBoy fails to open a window,
that's almost always a missing/unavailable display, not this script.

Usage:
    python3 play_headed.py <rom_filename_or_path> [--fresh] [--scale N]

Examples:
    python3 play_headed.py pokemon_red.gb
    python3 play_headed.py pokemon_red.gb --fresh      # ignore any existing save
    python3 play_headed.py /some/other/path/game.gb    # a ROM outside the shared library

Default PyBoy controls (verify against your PyBoy version/build if these
don't respond - this script doesn't touch input handling at all, PyBoy's
own SDL2 window handles it internally):
    Arrow keys = D-pad      Z = A       X = B
    Enter      = Start      Backspace  = Select
"""
import argparse
import sys
from pathlib import Path

from pyboy import PyBoy

# Matches config.py in the gbserver project - same shared ROM library and
# saves folder. Deliberately NOT importing gbserver's config module
# itself, to keep this script genuinely standalone (no dependency on the
# web server's codebase, no risk of pulling in Flask/multiprocessing/etc.
# just to read two constants) - if you move gbserver's install location,
# update BASE_DIR below to match.
BASE_DIR = Path(__file__).resolve().parent
ROMS_DIR = BASE_DIR / "roms"
SAVES_DIR = BASE_DIR / "saves"


def save_path_for(rom_path: Path) -> Path:
    # Same naming formula as Emulator._save_path_for in emulator.py:
    # <rom filename without extension>.state, in the shared saves folder.
    return SAVES_DIR / (rom_path.stem + ".state")


def main():
    parser = argparse.ArgumentParser(
        description="Play a Game Boy ROM headed, locally, via PyBoy's own SDL2 window."
    )
    parser.add_argument(
        "rom",
        help="ROM filename (looked up in gbserver's roms/ folder) or a full path to any .gb/.gbc file",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Start fresh, ignoring any existing .state save for this ROM",
    )
    parser.add_argument(
        "--scale", type=int, default=3,
        help="Window scale factor (default: 3)",
    )
    args = parser.parse_args()

    rom_arg = Path(args.rom)
    rom_path = rom_arg if (rom_arg.is_absolute() or rom_arg.exists()) else ROMS_DIR / rom_arg
    if not rom_path.exists():
        print(f"ROM not found: {rom_path}", file=sys.stderr)
        sys.exit(1)

    save_path = save_path_for(rom_path)
    SAVES_DIR.mkdir(exist_ok=True)

    pyboy = PyBoy(
        str(rom_path),
        window="SDL2",
        scale=args.scale,
        sound_emulated=True,
        sound_volume=100,
    )
    pyboy.set_emulation_speed(1)

    if not args.fresh and save_path.exists():
        print(f"Loading existing save: {save_path}")
        with open(save_path, "rb") as f:
            pyboy.load_state(f)
    else:
        print("Starting fresh (no save loaded)")

    print(f"Playing {rom_path.name} - close the window or press Ctrl+C to stop and save")

    try:
        while pyboy.tick():
            pass
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        print(f"Saving to {save_path}")
        with open(save_path, "wb") as f:
            pyboy.save_state(f)
        # save=False here is deliberate - the .state save above already
        # captured everything needed; letting stop() ALSO do its own
        # separate cartridge-RAM autosave would just write an extra,
        # differently-formatted file next to the ROM that gbserver
        # doesn't use or expect.
        pyboy.stop(save=False)


if __name__ == "__main__":
    main()

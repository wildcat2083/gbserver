import argparse
import sys
from pathlib import Path

from pyboy import PyBoy


BASE_DIR = Path(__file__).resolve().parent
ROMS_DIR = BASE_DIR / "roms"
SAVES_DIR = BASE_DIR / "saves"


def save_path_for(rom_path: Path) -> Path:

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

        pyboy.stop(save=False)


if __name__ == "__main__":
    main()

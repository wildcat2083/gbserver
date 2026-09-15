# Hidden debugger

## Opening it

On the player page, enter **Start, Select, Start, Select, A, B, A, B** within
five seconds. Keyboard (Enter/Shift/Z/X), the on-screen buttons, and gamepads
all work. The debugger's code isn't downloaded until the sequence is entered.
Close it with the × or Escape.

The buttons are also sent to the game, so you may pause or open a menu
in-game while entering it.

## Memory

- Hex grid with ASCII column. Scroll the grid, use ▲/▼, **Go**, or the region
  list (ROM0, ROMX, VRAM, SRAM, WRAM, ECHO, OAM, I/O, HRAM) to move.
- Bytes that changed since the last refresh turn red. Frozen bytes are blue,
  watched yellow, breakpoints pink, and the current PC green while stopped.
- Click a byte to select it (arrow keys move the selection). Type two hex
  digits, press Enter, or double-click to edit; typing moves on to the next
  byte, BGB-style. Escape cancels.
- **Poke** writes several bytes at once (`3E 01 C9`).
- ROM ($0000-$7FFF) is read-only - writes there would switch memory banks.

Numbers are hex everywhere; prefix `#` for decimal (`#150`).

## Search

1. Pick 8- or 16-bit and the regions (WRAM + HRAM by default), then **New search**.
2. If you know the value, filter **= value**.
3. If you don't, change it in-game and filter **increased**, **decreased**,
   **changed** or **unchanged**. Repeat until a few addresses remain.

Each filter compares against the values at the previous step. Results can be
opened in the memory view, frozen, or watched.

## Breakpoints, watches, freezes

- **Execution breakpoints** stop on the exact instruction, mid-frame. Enter
  `0150`, `01:4A2F` (bank:address), or a label if a `.sym` file sits next to
  the ROM. For $4000-$7FFF the bank is detected when unambiguous. Supported in
  $0000-$DFFF (PyBoy can't hook HRAM/OAM/I/O).
- **Watches** pause at the end of the frame in which an address changes, or
  crosses a value.
- **Freezes** rewrite a value every frame.

While stopped, the memory view and CPU tab show exact state. **Continue**
resumes; **Step frame** runs one frame and stops again.

## Permissions and safety

- Everyone connected can view memory, registers and use search.
- Only the current controller can edit, freeze, watch, set breakpoints, pause,
  or change registers.
- When control changes hands, or a ROM loads/stops, all breakpoints, watches
  and freezes are cleared and the game resumes - nobody gets stuck in someone
  else's paused session.
- Save states are written with breakpoints temporarily removed, so they never
  contain breakpoint opcodes.
- The debugger needs the `pyboy` engine (not Boytacean).
- `GBSERVER_DEBUGGER=on|internal|off` in `/etc/gbserver.env` controls
  availability. `internal` limits it to `GBSERVER_INTERNAL_HOSTS`.

It's an easter egg, not a secret: the sequence is readable in `static/app.js`.

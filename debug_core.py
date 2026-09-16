"""Debugger core - runs inside the emulator worker process.

Breakpoints use PyBoy's hook mechanism. When one is hit, the hook callback
does not return: it enters a "hold" loop that keeps servicing debugger
commands (memory reads/writes, registers, continue, step) while the CPU is
frozen at the exact instruction, the same way BGB stops mid-frame.

Breakpoints are never installed or removed while PyBoy is inside tick();
changes are recorded in `bps` (desired state) and applied by
sync_breakpoints() at the next frame boundary. PyBoy temporarily removes
the breakpoint it is currently handling, so touching its tables from inside
a hook is not safe.

Watches (value changes) and freezes are evaluated once per frame.
"""

import queue
import time
from contextlib import contextmanager

OPCODE_BRK = 0xDB

MAX_BREAKPOINTS = 32
MAX_WATCHES = 16
MAX_FREEZES = 64
MAX_READ = 0x1000
MAX_WRITE = 256

REGIONS = {
    "vram": (0x8000, 0xA000),
    "sram": (0xA000, 0xC000),
    "wram": (0xC000, 0xE000),
    "oam": (0xFE00, 0xFEA0),
    "io": (0xFF00, 0xFF80),
    "hram": (0xFF80, 0xFFFF),
}

REGISTERS = {"A": 0xFF, "F": 0xF0, "B": 0xFF, "C": 0xFF, "D": 0xFF, "E": 0xFF,
             "HL": 0xFFFF, "SP": 0xFFFF, "PC": 0xFFFF}

WATCH_CONDITIONS = ("change", "eq", "ne", "gt", "lt")

# Commands that can be serviced while held at a breakpoint without
# resuming execution. Anything else (stop, load, save, shutdown...) makes
# the hold release so the main loop can run it safely.
INLINE_COMMANDS = {"press", "release", "set_cheats", "set_fast_forward"}


def _int(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{name} must be an integer from {lo:#x} to {hi:#x}")
    return value


class DebugCore:
    def __init__(self, cmd_queue, out_queue, handle_inline):
        self.cmd_queue = cmd_queue
        self.out_queue = out_queue
        self.handle_inline = handle_inline
        self.pyboy = None
        self.engine = "pyboy"
        self.deferred = []
        self._clear()

    # ---- lifecycle -----------------------------------------------------------

    def _clear(self):
        self.bps = {}          # (bank, addr) -> {"hits": int, "error": str|None}
        self.installed = {}    # (bank, addr) -> original opcode
        self.bp_dirty = False
        self.watches = {}      # id -> {...}
        self.next_watch_id = 1
        self.freezes = {}      # addr -> {"value": int, "size": 1|2}
        self.paused = False
        self.holding = False
        self.step_pending = False
        self.last_break = None
        self.frame = 0

    def _active(self):
        return bool(self.bps or self.watches or self.freezes or self.paused or self.holding)

    def attach(self, pyboy, engine):
        """Call whenever the worker creates a new emulator instance."""
        was_active = self._active()
        self.pyboy = pyboy
        self.engine = engine
        self._clear()
        if was_active:
            self.emit("reset")

    def detach(self):
        self.attach(None, self.engine)

    def emit(self, event):
        self.out_queue.put({"type": "debug", "event": event, "state": self.state()})

    @property
    def usable(self):
        return self.pyboy is not None and self.engine == "pyboy"

    def _require(self):
        if self.pyboy is None:
            raise ValueError("No ROM is running")
        if self.engine != "pyboy":
            raise ValueError("The debugger needs the pyboy engine for this ROM")

    # ---- main-loop integration ----------------------------------------------

    def should_skip_tick(self):
        return self.paused

    def sync_breakpoints(self):
        if not self.bp_dirty or not self.usable:
            return
        pb = self.pyboy
        for key in list(self.installed):
            if key not in self.bps:
                try:
                    pb.hook_deregister(*key)
                except Exception:
                    pass
                del self.installed[key]
        for key, bp in self.bps.items():
            if key in self.installed:
                continue
            try:
                opcode = pb.memory[key[0], key[1]]
                pb.hook_register(key[0], key[1], self._on_hook, key)
                self.installed[key] = opcode
                bp["error"] = None
            except Exception as e:
                bp["error"] = str(e)
        self.bp_dirty = False

    @contextmanager
    def breakpoints_removed(self):
        """Temporarily pull every breakpoint opcode out of memory (for save/load state)."""
        if not self.installed or not self.usable:
            yield
            return
        for key in list(self.installed):
            try:
                self.pyboy.hook_deregister(*key)
            except Exception:
                pass
        self.installed.clear()
        try:
            yield
        finally:
            self.bp_dirty = True
            self.sync_breakpoints()

    def after_frame(self):
        if not self.usable:
            return
        self.frame += 1
        if self.freezes:
            for addr, f in self.freezes.items():
                self._write_value(addr, f["value"], f["size"])
        if self.watches:
            for w in self.watches.values():
                cur = self._read_value(w["addr"], w["size"])
                prev = w["last"]
                w["last"] = cur
                v = w["value"]
                cond = w["cond"]
                if cond == "change":
                    hit = cur != prev
                elif cond == "eq":
                    hit = cur == v and prev != v
                elif cond == "ne":
                    hit = cur != v and prev == v
                elif cond == "gt":
                    hit = cur > v and not prev > v
                else:
                    hit = cur < v and not prev < v
                if hit:
                    w["hits"] += 1
                    self.paused = True
                    self.step_pending = False
                    self.last_break = {
                        "type": "watch", "id": w["id"], "addr": w["addr"], "size": w["size"],
                        "old": prev, "new": cur, "pc": self.pyboy.register_file.PC,
                    }
                    self.emit("break")
                    return
        if self.step_pending:
            self.step_pending = False
            self.paused = True
            self.last_break = {"type": "step", "pc": self.pyboy.register_file.PC}
            self.emit("break")

    # ---- breakpoint hit / hold loop -------------------------------------------

    def _on_hook(self, key):
        bp = self.bps.get(key)
        if bp is None:          # removed while held; uninstalled at next frame boundary
            return
        bp["hits"] += 1
        if self.deferred:
            # A stop/load/save/shutdown is waiting for this frame to finish -
            # don't trap it behind a breakpoint in a tight loop.
            return
        self.step_pending = False
        self.last_break = {"type": "breakpoint", "bank": key[0], "addr": key[1],
                           "pc": self.pyboy.register_file.PC}
        self._hold()

    def _hold(self):
        self.holding = True
        self.paused = False
        self.emit("break")
        try:
            while self.holding:
                try:
                    msg = self.cmd_queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                cmd = msg.get("cmd", "")
                if cmd.startswith("dbg_"):
                    self.handle_and_ack(msg)
                elif cmd in INLINE_COMMANDS:
                    self.handle_inline(msg)
                else:
                    self.deferred.append(msg)
                    self.holding = False
                    self.last_break = None
        finally:
            self.holding = False
        self.emit("resume")

    # ---- memory helpers -----------------------------------------------------

    def _read_value(self, addr, size):
        mem = self.pyboy.memory
        if size == 1:
            return mem[addr]
        return mem[addr] | (mem[(addr + 1) & 0xFFFF] << 8)

    def _write_value(self, addr, value, size):
        mem = self.pyboy.memory
        mem[addr] = value & 0xFF
        if size == 2:
            mem[(addr + 1) & 0xFFFF] = (value >> 8) & 0xFF

    def read(self, start, length):
        end = start + length
        data = bytearray(self.pyboy.memory[start:end])
        # Show the real opcode instead of the breakpoint marker PyBoy writes into memory
        for (bank, addr), opcode in self.installed.items():
            if start <= addr < end and data[addr - start] == OPCODE_BRK:
                try:
                    if self.pyboy.memory[bank, addr] == OPCODE_BRK:
                        data[addr - start] = opcode
                except Exception:
                    pass
        return bytes(data)

    def _guess_rom_bank(self, addr):
        mem = self.pyboy.memory
        size_code = mem[0, 0x148]
        bank_count = (2 << size_code) if size_code <= 8 else 2
        end = min(addr + 32, 0x8000)
        window = self.read(addr, end - addr)
        matches = []
        for b in range(1, bank_count):
            try:
                if bytes(mem[b, addr:end]) == window:
                    matches.append(b)
            except Exception:
                break
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"Couldn't work out which ROM bank ${addr:04X} is in - enter it as BB:{addr:04X}")
        raise ValueError(
            f"${addr:04X} looks identical in {len(matches)} ROM banks - enter it as BB:{addr:04X} "
            f"(e.g. {matches[0]:02X}:{addr:04X})"
        )

    def _jumps_to_itself(self, bank, addr):
        """True for `jr @`, `jr cc,@`, `jp @`, `jp cc,@` - tight self-loops.

        PyBoy's breakpoint stepping never lets a frame finish on these, so a
        breakpoint there would wedge the emulator the first time it needs to
        run past it (e.g. to stop or load a ROM while held).
        """
        try:
            if 0x4000 <= addr < 0x8000:
                code = self.pyboy.memory[bank, addr:addr + 3]
            else:
                code = self.pyboy.memory[addr:min(addr + 3, 0x10000)]
        except Exception:
            return False
        code = list(code) + [0, 0, 0]
        op = code[0]
        if op in (0x18, 0x20, 0x28, 0x30, 0x38) and code[1] == 0xFE:
            return True
        if op in (0xC3, 0xC2, 0xCA, 0xD2, 0xDA) and (code[1] | (code[2] << 8)) == addr:
            return True
        return False

    # ---- state --------------------------------------------------------------

    def registers(self):
        rf = self.pyboy.register_file
        return {"A": rf.A, "F": rf.F, "B": rf.B, "C": rf.C, "D": rf.D, "E": rf.E,
                "HL": rf.HL, "SP": rf.SP, "PC": rf.PC}

    def state(self):
        s = {
            "available": self.usable,
            "engine": self.engine,
            "running": self.pyboy is not None,
            "paused": self.paused or self.holding,
            "holding": self.holding,
            "break": self.last_break,
            "breakpoints": [
                {"bank": k[0], "addr": k[1], "hits": v["hits"], "error": v["error"],
                 "installed": k in self.installed}
                for k, v in sorted(self.bps.items(), key=lambda kv: (kv[0][1], kv[0][0]))
            ],
            "watches": [
                {k: w[k] for k in ("id", "addr", "size", "cond", "value", "hits", "last")}
                for w in self.watches.values()
            ],
            "freezes": [
                {"addr": a, "value": f["value"], "size": f["size"]}
                for a, f in sorted(self.freezes.items())
            ],
            "registers": None,
        }
        if self.usable:
            try:
                s["registers"] = self.registers()
            except Exception:
                pass
        return s

    # ---- command handling ---------------------------------------------------

    def handle_and_ack(self, msg):
        req_id = msg.get("req_id")
        try:
            result = self.handle(msg)
            ack = {"type": "ack", "req_id": req_id, "ok": True, "error": None, "result": result}
        except Exception as e:
            ack = {"type": "ack", "req_id": req_id, "ok": False, "error": str(e)}
        if req_id is not None:
            self.out_queue.put(ack)

    def handle(self, msg):
        cmd = msg["cmd"][len("dbg_"):]

        if cmd == "state":
            return self.state()

        if cmd == "reset":
            was_holding = self.holding
            self.bps.clear()
            self.bp_dirty = True
            self.watches.clear()
            self.freezes.clear()
            self.paused = False
            self.step_pending = False
            self.last_break = None
            self.holding = False
            if not was_holding:
                self.emit("reset")
            return {}

        self._require()

        if cmd == "read":
            start = _int(msg.get("start"), "start", 0, 0xFFFF)
            length = _int(msg.get("length"), "length", 1, MAX_READ)
            length = min(length, 0x10000 - start)
            return {"start": start, "data": self.read(start, length)}

        if cmd == "read_regions":
            out = {}
            for name in msg.get("regions", []):
                if name not in REGIONS:
                    raise ValueError(f"unknown region {name!r}")
                lo, hi = REGIONS[name]
                out[name] = {"start": lo, "data": self.read(lo, hi - lo)}
            return {"regions": out}

        if cmd == "write":
            addr = _int(msg.get("addr"), "addr", 0, 0xFFFF)
            if addr < 0x8000:
                raise ValueError("ROM ($0000-$7FFF) is read-only - writes there would switch memory banks")
            values = msg.get("values")
            if not isinstance(values, list) or not 1 <= len(values) <= MAX_WRITE:
                raise ValueError(f"values must be a list of 1-{MAX_WRITE} bytes")
            if addr + len(values) > 0x10000:
                raise ValueError("write runs past $FFFF")
            values = [_int(v, "byte", 0, 0xFF) for v in values]
            for i in range(len(values)):
                a = addr + i
                if any(k[1] == a for k in self.installed):
                    raise ValueError(f"${a:04X} has a breakpoint on it - remove it before editing")
            for i, v in enumerate(values):
                a = addr + i
                self.pyboy.memory[a] = v
                if a in self.freezes and self.freezes[a]["size"] == 1:
                    self.freezes[a]["value"] = v
            return {}

        if cmd == "set_register":
            name = msg.get("name")
            if name not in REGISTERS:
                raise ValueError("unknown register")
            value = _int(msg.get("value"), name, 0, 0xFFFF) & REGISTERS[name]
            setattr(self.pyboy.register_file, name, value)
            return {"registers": self.registers()}

        if cmd == "bp_add":
            if len(self.bps) >= MAX_BREAKPOINTS:
                raise ValueError(f"at most {MAX_BREAKPOINTS} breakpoints")
            addr = msg.get("addr")
            bank = msg.get("bank")
            if isinstance(addr, str):
                symbol = addr.strip()
                if not symbol or len(symbol) > 128:
                    raise ValueError("invalid symbol")
                try:
                    bank, addr = self.pyboy.symbol_lookup(symbol)
                except Exception:
                    raise ValueError(f'Unknown symbol "{symbol}" (no .sym file next to this ROM?)')
            addr = _int(addr, "addr", 0, 0xFFFF)
            if addr >= 0x8000:
                raise ValueError(
                    "Breakpoints go on ROM code ($0000-$7FFF) only - in RAM they'd overwrite "
                    "game data. To catch a value changing, use a watch instead."
                )
            if bank is None:
                bank = self._guess_rom_bank(addr) if 0x4000 <= addr < 0x8000 else 0
            bank = _int(bank, "bank", 0, 0x1FF)
            if addr < 0x4000 and bank != 0:
                raise ValueError("$0000-$3FFF is always bank 00")
            if self._jumps_to_itself(bank, addr):
                raise ValueError(
                    f"{bank:02X}:{addr:04X} is a jump to itself - PyBoy can't step past a breakpoint "
                    "there. Put it on the instruction before the loop, or use Pause instead."
                )
            key = (bank, addr)
            if key in self.bps:
                raise ValueError(f"breakpoint {bank:02X}:{addr:04X} already exists")
            self.bps[key] = {"hits": 0, "error": None}
            self.bp_dirty = True
            if not self.holding:
                self.sync_breakpoints()
            return {"bank": bank, "addr": addr}

        if cmd == "bp_remove":
            key = (_int(msg.get("bank"), "bank", 0, 0x1FF), _int(msg.get("addr"), "addr", 0, 0xFFFF))
            if self.bps.pop(key, None) is None:
                raise ValueError("no such breakpoint")
            self.bp_dirty = True
            if not self.holding:
                self.sync_breakpoints()
            return {}

        if cmd == "bp_clear":
            self.bps.clear()
            self.bp_dirty = True
            if not self.holding:
                self.sync_breakpoints()
            return {}

        if cmd == "watch_add":
            if len(self.watches) >= MAX_WATCHES:
                raise ValueError(f"at most {MAX_WATCHES} watches")
            addr = _int(msg.get("addr"), "addr", 0, 0xFFFF)
            size = msg.get("size", 1)
            if size not in (1, 2):
                raise ValueError("size must be 1 or 2")
            cond = msg.get("cond", "change")
            if cond not in WATCH_CONDITIONS:
                raise ValueError("unknown condition")
            value = 0 if cond == "change" else _int(msg.get("value"), "value", 0, 0xFF if size == 1 else 0xFFFF)
            wid = self.next_watch_id
            self.next_watch_id += 1
            self.watches[wid] = {"id": wid, "addr": addr, "size": size, "cond": cond, "value": value,
                                 "hits": 0, "last": self._read_value(addr, size)}
            return {"id": wid}

        if cmd == "watch_remove":
            if self.watches.pop(msg.get("id"), None) is None:
                raise ValueError("no such watch")
            return {}

        if cmd == "freeze_set":
            addr = _int(msg.get("addr"), "addr", 0, 0xFFFF)
            if addr < 0x8000:
                raise ValueError("ROM ($0000-$7FFF) can't be frozen")
            size = msg.get("size", 1)
            if size not in (1, 2) or addr + size > 0x10000:
                raise ValueError("size must be 1 or 2")
            value = _int(msg.get("value"), "value", 0, 0xFF if size == 1 else 0xFFFF)
            if addr not in self.freezes and len(self.freezes) >= MAX_FREEZES:
                raise ValueError(f"at most {MAX_FREEZES} frozen addresses")
            self.freezes[addr] = {"value": value, "size": size}
            self._write_value(addr, value, size)
            return {}

        if cmd == "freeze_remove":
            if self.freezes.pop(msg.get("addr"), None) is None:
                raise ValueError("that address isn't frozen")
            return {}

        if cmd == "pause":
            if not self.holding and not self.paused:
                self.paused = True
                self.last_break = {"type": "manual", "pc": self.pyboy.register_file.PC}
                self.emit("break")
            return {}

        if cmd == "continue":
            self.step_pending = False
            if self.holding:
                self.holding = False
                self.last_break = None
            elif self.paused:
                self.paused = False
                self.last_break = None
                self.emit("resume")
            return {}

        if cmd == "step_frame":
            if self.holding:
                self.step_pending = True
                self.holding = False
            elif self.paused:
                self.step_pending = True
                self.paused = False
            else:
                raise ValueError("pause first")
            return {}

        raise ValueError(f"unknown debugger command {cmd!r}")

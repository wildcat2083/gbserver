"""
Runs as its own multiprocessing.Process - one per Emulator session (the
shared game, or a private room). Owns the actual PyBoy/Boytacean instance
completely; nothing outside this process ever holds a direct reference to
it, since that object generally isn't shareable across a process boundary
anyway. All communication happens through two multiprocessing.Queue
objects passed in at startup:

  cmd_queue  - commands FROM the main process TO this worker
  out_queue  - video/audio/status/acks FROM this worker TO the main process

This is the CPU-heavy half of what used to be Emulator._run_loop() and
friends in emulator.py - moved here so each session's tick loop is a
genuinely separate OS process (with its own GIL) rather than a thread
sharing one process's GIL with every other session. See emulator.py's
Emulator class for the main-process side of this - it now proxies these
same operations through the queues instead of calling PyBoy directly.

Command messages (dicts), sent on cmd_queue:
  {"cmd": "load_rom", "filename": ..., "load_save": ..., "engine_name": ...,
   "cgb": ..., "req_id": N}
  {"cmd": "press", "button": ...}
  {"cmd": "release", "button": ...}
  {"cmd": "stop", "req_id": N}
  {"cmd": "set_fast_forward", "enabled": ...}
  {"cmd": "save_now", "req_id": N}
  {"cmd": "save_to_path", "path": ..., "req_id": N}
  {"cmd": "load_from_path", "path": ..., "req_id": N}
  {"cmd": "shutdown"}

Output messages (dicts), sent on out_queue:
  {"type": "video", "data": <compressed bytes>}
  {"type": "audio", "data": <raw bytes>}
  {"type": "status", "current_rom": ..., "rom_stem": ..., "engine": ...,
   "fast_forward": ..., "running": ...}
  {"type": "stopped"}
  {"type": "ack", "req_id": N, "ok": True/False, "error": <str or None>}
"""
import queue
import time
import zlib

import numpy as np

MSG_VIDEO = b"\x01"
MSG_AUDIO = b"\x02"


def run_worker(cmd_queue, out_queue, roms_dir, saves_dir, sound_sample_rate,
                audio_batch_ticks, autosave_interval_minutes, fast_forward_speed):
    """Entry point - this whole function IS the subprocess. Everything
    PyBoy/Boytacean-related lives entirely inside this function's scope;
    nothing it touches is visible to (or shared with) the main process."""
    from pyboy import PyBoy
    try:
        from boytacean.pyboy import PyBoyV2 as Boytacean
        boytacean_available = True
    except ImportError:
        Boytacean = None
        boytacean_available = False

    pyboy = None
    rom_path = None
    engine_name = "pyboy"
    running = False
    fast_forward = False
    applied_fast_forward = False
    audio_accum = bytearray()
    last_frame_raw = None
    last_status_sent = None
    active_cheats = []  # list of {"address": int, "value": int} - GameShark-style RAM
                         # patches, continuously re-applied every tick below (see the
                         # comment where they're applied for why re-applying matters).
                         # Cleared on every ROM load (see do_load_rom) - deliberately not
                         # persisted across a reset/reload, by design.
    frame_no = 0
    autosave_every = 60 * 60 * autosave_interval_minutes

    def send_status():
        nonlocal last_status_sent
        status = {
            "type": "status",
            "current_rom": rom_path.name if rom_path else None,
            "engine": engine_name,
            "fast_forward": fast_forward,
            "running": running,
        }
        if status != last_status_sent:
            out_queue.put(status)
            last_status_sent = dict(status)

    def do_stop(autosave=True):
        nonlocal pyboy, running, last_frame_raw
        running = False
        if pyboy is not None and rom_path is not None:
            if autosave:
                try:
                    save_path = saves_dir / (rom_path.stem + ".state")
                    with open(save_path, "wb") as f:
                        pyboy.save_state(f)
                except Exception as e:
                    print(f"[worker] could not save state on stop: {e}")
            try:
                pyboy.stop(save=False)
            except Exception:
                pass
            pyboy = None
            last_frame_raw = None
            out_queue.put({"type": "stopped"})
        send_status()

    def do_load_rom(filename, load_save):
        nonlocal pyboy, rom_path, engine_name, running, fast_forward
        nonlocal last_frame_raw, audio_accum, frame_no, active_cheats

        # Cheats reset on every load/reload, deliberately - including the
        # same-ROM "Resume save"/Reset cases below, not just switching to
        # a genuinely different ROM. A cheat silently surviving into a
        # fresh load would be surprising, not helpful.
        active_cheats = []

        candidate = roms_dir / filename
        # Whether this is reloading the SAME ROM that's already running
        # (e.g. "Resume save" right after uploading a .state file for it)
        # vs switching to a genuinely different one - checked before
        # do_stop() runs, using its own default (autosave=True) for a real
        # ROM switch, so progress on the outgoing ROM is never lost. But
        # for the same-ROM case, autosaving here would immediately
        # overwrite whatever save is about to be loaded below (an upload
        # that just happened, for instance) with a snapshot of the
        # barely-different current state - the exact bug this is fixing.
        is_same_rom = rom_path is not None and rom_path == candidate
        do_stop(autosave=not is_same_rom)

        if not candidate.exists():
            raise FileNotFoundError(filename)

        save_path = saves_dir / (candidate.stem + ".state")

        use_boytacean = False
        try:
            from engine_config import get_engine_for_rom
            use_boytacean = (
                get_engine_for_rom(filename) == "boytacean" and boytacean_available
            )
        except Exception:
            pass

        if use_boytacean:
            new_pyboy = Boytacean(
                str(candidate),
                window="null",
                sound_emulated=True,
                sound_sample_rate=sound_sample_rate,
                sound_volume=100,
                cgb=True if candidate.suffix.lower() == ".gbc" else None,
            )
            engine_name = "boytacean"
        else:
            new_pyboy = PyBoy(
                str(candidate),
                window="null",
                sound_emulated=True,
                sound_sample_rate=sound_sample_rate,
                sound_volume=100,
            )
            engine_name = "pyboy"
        new_pyboy.set_emulation_speed(1)

        save_load_error = None
        if load_save and save_path.exists():
            try:
                with open(save_path, "rb") as f:
                    new_pyboy.load_state(f)
            except Exception as e:
                save_load_error = str(e)
                print(f"[worker] could not load save state: {e}")

        pyboy = new_pyboy
        rom_path = candidate
        running = True
        fast_forward = False
        audio_accum = bytearray()
        last_frame_raw = None
        frame_no = 0
        send_status()
        return save_load_error

    def do_save_now():
        if pyboy is None or rom_path is None:
            raise ValueError("no ROM is currently loaded")
        save_path = saves_dir / (rom_path.stem + ".state")
        with open(save_path, "wb") as f:
            pyboy.save_state(f)

    def do_save_to_path(target_path):
        if pyboy is None:
            raise ValueError("no ROM is currently loaded")
        with open(target_path, "wb") as f:
            pyboy.save_state(f)

    def do_load_from_path(source_path):
        if pyboy is None:
            raise ValueError("no ROM is currently loaded")
        with open(source_path, "rb") as f:
            pyboy.load_state(f)

    def do_set_cheats(codes):
        """Replaces the entire active cheat list wholesale - the caller
        (Emulator.set_cheats) always sends the FULL current list, not an
        incremental add/remove, so this is just a plain reassignment
        rather than needing to track additions/removals here too."""
        nonlocal active_cheats
        active_cheats = codes

    def grab_frame():
        return pyboy.screen.ndarray.tobytes()

    def grab_audio():
        if engine_name != "pyboy":
            return None
        try:
            arr = pyboy.sound.ndarray
        except Exception:
            return None
        if arr is None or arr.size == 0:
            return None
        return np.ascontiguousarray(arr).tobytes()

    def handle_command(msg):
        cmd = msg.get("cmd")
        req_id = msg.get("req_id")

        def ack(ok=True, error=None):
            if req_id is not None:
                out_queue.put({"type": "ack", "req_id": req_id, "ok": ok, "error": error})

        try:
            if cmd == "load_rom":
                result = do_load_rom(msg["filename"], msg.get("load_save", True))
                out_queue.put({
                    "type": "ack", "req_id": req_id, "ok": True, "error": result,
                })
            elif cmd == "press":
                if pyboy is not None:
                    pyboy.button_press(msg["button"])
            elif cmd == "release":
                if pyboy is not None:
                    pyboy.button_release(msg["button"])
            elif cmd == "stop":
                do_stop()
                ack()
            elif cmd == "set_fast_forward":
                nonlocal_set_fast_forward(msg["enabled"])
            elif cmd == "save_now":
                do_save_now()
                ack()
            elif cmd == "save_to_path":
                do_save_to_path(msg["path"])
                ack()
            elif cmd == "load_from_path":
                do_load_from_path(msg["path"])
                ack()
            elif cmd == "set_cheats":
                do_set_cheats(msg["codes"])
                ack()
        except Exception as e:
            ack(ok=False, error=str(e))

    def nonlocal_set_fast_forward(enabled):
        nonlocal fast_forward
        fast_forward = enabled
        send_status()

    def log_crash(tb):
        # Logged to BOTH stderr (in case gunicorn/systemd is capturing it)
        # and a dedicated file (in case it isn't) - this is purely
        # diagnostic for now: something IS crashing this loop under real
        # production conditions (confirmed by worker processes vanishing
        # from `ps aux` shortly after successfully starting), and nothing
        # was visible anywhere before this addition.
        import sys
        print(f"[worker] CRASHED:\n{tb}", file=sys.stderr, flush=True)
        try:
            with open(saves_dir.parent / "worker_crash.log", "a") as f:
                f.write(f"\n--- worker crash at {time.time()} ---\n{tb}\n")
        except Exception:
            pass

    running_worker = True
    while running_worker:
      try:
        # Idle (nothing loaded): block on the command queue rather than
        # busy-looping, since there's no tick work to do.
        if pyboy is None:
            try:
                msg = cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg.get("cmd") == "shutdown":
                break
            handle_command(msg)
            continue

        # Loaded and ticking: drain any pending commands without blocking,
        # then do exactly one tick - same overall shape as the original
        # _run_loop, just now living in its own process.
        start = time.time()
        try:
            while True:
                msg = cmd_queue.get_nowait()
                if msg.get("cmd") == "shutdown":
                    running_worker = False
                    break
                handle_command(msg)
        except queue.Empty:
            pass
        if not running_worker:
            break
        if pyboy is None:
            continue  # a command (e.g. stop) may have just cleared it

        if fast_forward != applied_fast_forward:
            target = fast_forward_speed if fast_forward else 1
            try:
                pyboy.set_emulation_speed(target)
            except Exception as e:
                print(f"[worker] set_emulation_speed failed: {e}")
            applied_fast_forward = fast_forward

        alive = pyboy.tick(1, True)

        # GameShark-style cheats: re-applied every single tick, not just
        # once - a real GameShark works the same way, continuously
        # forcing its target addresses back to the cheat value, since
        # the game's own code would otherwise overwrite them on its own
        # next update (e.g. decrementing health/ammo normally). Applied
        # AFTER tick() specifically, so this is the last thing to touch
        # that address before the frame gets rendered/read below -
        # applying before tick() would just let the game's own logic
        # immediately overwrite it again within the same frame.
        if active_cheats:
            try:
                for cheat in active_cheats:
                    pyboy.memory[cheat["address"]] = cheat["value"]
            except Exception as e:
                print(f"[worker] failed to apply cheat: {e}")

        frame_bytes = grab_frame()
        audio_bytes = grab_audio()  # always drained, even during fast-forward (buffer overrun otherwise)

        if frame_bytes != last_frame_raw:
            compressed = zlib.compress(frame_bytes, level=1)
            last_frame_raw = frame_bytes
            out_queue.put({"type": "video", "data": compressed})

        if audio_bytes and not fast_forward:
            audio_accum.extend(audio_bytes)
        if (frame_no + 1) % max(1, audio_batch_ticks) == 0 and audio_accum:
            out_queue.put({"type": "audio", "data": bytes(audio_accum)})
            audio_accum = bytearray()

        frame_no += 1
        if frame_no % autosave_every == 0:
            try:
                do_save_now()
            except Exception as e:
                print(f"[worker] autosave failed: {e}")

        if not alive:
            do_stop()

        if fast_forward:
            time.sleep(0.001)
        else:
            elapsed = time.time() - start
            remaining = (1.0 / 60.0) - elapsed
            if remaining > 0:
                time.sleep(remaining)
      except Exception:
        import traceback
        log_crash(traceback.format_exc())
        raise

    # Clean shutdown - make sure the current state is saved, same as a
    # normal stop, rather than just vanishing mid-game.
    if pyboy is not None:
        do_stop()


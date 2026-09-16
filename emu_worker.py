from pathlib import Path
import math
import signal
import queue
import time
import zlib
import io

import numpy as np

MSG_VIDEO = b"\x01"
MSG_AUDIO = b"\x02"


def run_worker(cmd_queue, out_queue, roms_dir, saves_dir, sound_sample_rate,
                sound_volume, audio_batch_ticks, autosave_interval_minutes, fast_forward_speed):
    for name in ("SIGTERM", "SIGINT", "SIGQUIT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, signal.SIG_DFL)

    from pyboy import PyBoy
    from debug_core import DebugCore
    from config import rom_symbols_path
    try:
        from boytacean.pyboy import PyBoyV2 as Boytacean
        boytacean_available = True
    except ImportError:
        Boytacean = None
        boytacean_available = False

    audio_accessor = None

    DC_CUTOFF_HZ = 20.0
    dc_decay = math.exp(-2.0 * math.pi * DC_CUTOFF_HZ / sound_sample_rate)

    dc_last_input = np.zeros(2, dtype=np.float64)
    dc_last_output = np.zeros(2, dtype=np.float64)

    AUDIO_GAIN = 2.0

    next_frame = time.monotonic()

    fps_window_start = time.monotonic()
    fps_frames = 0
    FPS_REPORT_EVERY = 300

    pyboy = None
    rom_path = None
    engine_name = "pyboy"
    running = False
    fast_forward = False
    applied_fast_forward = False
    audio_accum = bytearray()
    last_frame_raw = None
    last_status_sent = None
    active_cheats = []

    frame_no = 0
    autosave_every = 60 * 60 * autosave_interval_minutes

    def make_pyboy(rom_file, **kwargs):
        """Create PyBoy from the ROM's bytes rather than its path.

        Given a path, PyBoy reads and writes name.gb.ram / .rtc / .state files
        beside the ROM on its own. Given bytes, it never touches roms/ - all
        persistence stays in saves/ and is handled by this worker.
        """
        rom_file = Path(rom_file)
        symbols = rom_symbols_path(rom_file)
        return PyBoy(
            io.BytesIO(rom_file.read_bytes()),
            window="null",
            sound_emulated=True,
            sound_sample_rate=sound_sample_rate,
            sound_volume=sound_volume,
            symbols=str(symbols) if symbols else None,
            **kwargs,
        )

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
                    with dbg.breakpoints_removed(), open(save_path, "wb") as f:
                        pyboy.save_state(f)
                except Exception as e:
                    print(f"[worker] could not save state on stop: {e}")
            try:
                pyboy.stop(save=False)
            except Exception:
                pass
            pyboy = None
            dbg.detach()
            last_frame_raw = None
            out_queue.put({"type": "stopped"})
        send_status()

    def do_load_rom(filename, load_save, ram_bytes=None):
        nonlocal pyboy, rom_path, engine_name, running, fast_forward
        nonlocal last_frame_raw, audio_accum, frame_no, active_cheats
        nonlocal next_frame, fps_window_start, fps_frames
        nonlocal dc_last_input, dc_last_output

        active_cheats = []

        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or filename in ("", ".", "..")
        ):
            raise FileNotFoundError(filename)
        candidate = roms_dir / filename

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

        if ram_bytes is not None and use_boytacean:
            raise ValueError(
                "Converting a .sav requires the pyboy engine, not boytacean"
            )

        if use_boytacean:
            new_pyboy = Boytacean(
                str(candidate),
                window="null",
                sound_emulated=True,
                sound_sample_rate=sound_sample_rate,
                sound_volume=sound_volume,
                cgb=True if candidate.suffix.lower() == ".gbc" else None,
            )
            engine_name = "boytacean"
        else:
            extra = {}
            if ram_bytes is not None:
                extra["ram_file"] = io.BytesIO(ram_bytes)
            new_pyboy = make_pyboy(candidate, **extra)
            engine_name = "pyboy"

        new_pyboy.set_emulation_speed(0)

        save_load_error = None

        if ram_bytes is None and load_save and save_path.exists():
            try:
                with open(save_path, "rb") as f:
                    new_pyboy.load_state(f)
            except Exception as e:
                save_load_error = str(e)
                print(f"[worker] could not load save state: {e}")

        pyboy = new_pyboy
        dbg.attach(pyboy, engine_name)
        rom_path = candidate
        running = True
        fast_forward = False
        audio_accum = bytearray()
        last_frame_raw = None
        frame_no = 0

        next_frame = time.monotonic()
        fps_window_start = next_frame
        fps_frames = 0

        dc_last_input = np.zeros(2, dtype=np.float64)
        dc_last_output = np.zeros(2, dtype=np.float64)

        send_status()
        return save_load_error

    def do_save_now():
        if pyboy is None or rom_path is None:
            raise ValueError("no ROM is currently loaded")
        save_path = saves_dir / (rom_path.stem + ".state")
        with dbg.breakpoints_removed(), open(save_path, "wb") as f:
            pyboy.save_state(f)

    def do_extract_sav():
        nonlocal pyboy
        if pyboy is None or rom_path is None:
            raise ValueError("no ROM is currently loaded")
        if engine_name != "pyboy":
            raise ValueError("extracting a .sav requires the pyboy engine")

        snapshot = io.BytesIO()
        with dbg.breakpoints_removed():
            pyboy.save_state(snapshot)
        snapshot.seek(0)

        ram_buf = io.BytesIO()
        # Explicit buffers: PyBoy would otherwise write name.gb.rtc beside the ROM
        pyboy.stop(ram_file=ram_buf, rtc_file=io.BytesIO())
        ram_buf.seek(0)
        sav_bytes = ram_buf.read()

        new_pyboy = make_pyboy(rom_path)
        new_pyboy.load_state(snapshot)
        new_pyboy.set_emulation_speed(0)
        pyboy = new_pyboy
        dbg.attach(pyboy, engine_name)

        return sav_bytes

    def do_save_to_path(target_path):
        if pyboy is None:
            raise ValueError("no ROM is currently loaded")
        with dbg.breakpoints_removed(), open(target_path, "wb") as f:
            pyboy.save_state(f)

    def do_load_from_path(source_path):
        if pyboy is None:
            raise ValueError("no ROM is currently loaded")
        with dbg.breakpoints_removed(), open(source_path, "rb") as f:
            pyboy.load_state(f)

    def do_set_cheats(codes):
        nonlocal active_cheats
        active_cheats = codes

    def grab_frame():
        return pyboy.screen.ndarray.tobytes()

    def grab_audio():
        nonlocal audio_accessor
        if engine_name != "pyboy":
            return None

        if audio_accessor is None:
            try:
                sound = pyboy.sound
                raw = sound.raw_ndarray
                head = sound.raw_buffer_head
                if isinstance(raw, np.ndarray) and isinstance(head, int):
                    audio_accessor = "raw"
                else:
                    audio_accessor = "legacy"
            except Exception:
                audio_accessor = "legacy"
            if audio_accessor == "legacy":
                print(
                    "[worker] pyboy.sound.raw_ndarray/raw_buffer_head unavailable - "
                    "falling back to sound.ndarray, which over-reads the frame "
                    "buffer and will reintroduce clicking"
                )

        try:
            if audio_accessor == "raw":
                n = pyboy.sound.raw_buffer_head // 2
                if n <= 0:
                    return None
                arr = pyboy.sound.raw_ndarray[:n]
            else:
                arr = pyboy.sound.ndarray
        except Exception:
            return None
        if arr is None or arr.size == 0:
            return None

        return dc_block(arr)

    def dc_block(tick_samples):
        nonlocal dc_last_input, dc_last_output
        samples = tick_samples.astype(np.float64)
        sample_count = samples.shape[0]

        deltas = np.empty_like(samples)
        deltas[0] = samples[0] - dc_last_input
        deltas[1:] = samples[1:] - samples[:-1]

        decay_powers = (dc_decay ** np.arange(1, sample_count + 1))[:, None]
        filtered = decay_powers * (
            dc_last_output + np.cumsum(deltas / decay_powers, axis=0)
        )

        dc_last_input = samples[-1].copy()
        dc_last_output = filtered[-1].copy()

        amplified = np.rint(filtered * AUDIO_GAIN)
        return np.clip(amplified, -127, 127).astype(np.int8).tobytes()

    def handle_command(msg):
        cmd = msg.get("cmd")
        req_id = msg.get("req_id")

        def ack(ok=True, error=None):
            if req_id is not None:
                out_queue.put({"type": "ack", "req_id": req_id, "ok": ok, "error": error})

        try:
            if isinstance(cmd, str) and cmd.startswith("dbg_"):
                dbg.handle_and_ack(msg)
            elif cmd == "load_rom":
                result = do_load_rom(msg["filename"], msg.get("load_save", True))
                out_queue.put({
                    "type": "ack", "req_id": req_id, "ok": True, "error": result,
                })
            elif cmd == "load_rom_with_ram":

                result = do_load_rom(msg["filename"], False, ram_bytes=msg["ram_bytes"])
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
            elif cmd == "extract_sav":

                sav_bytes = do_extract_sav()
                out_queue.put({
                    "type": "ack", "req_id": req_id, "ok": True,
                    "error": None, "sav_bytes": sav_bytes,
                })
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

    dbg = DebugCore(cmd_queue, out_queue, handle_command)

    def next_command():
        if dbg.deferred:
            return dbg.deferred.pop(0)
        return cmd_queue.get_nowait()

    def log_crash(tb):

        import sys
        print(f"[worker] CRASHED:\n{tb}", file=sys.stderr, flush=True)
        try:
            with open(saves_dir.parent / "worker_crash.log", "a") as f:
                f.write(f"\n--- worker crash at {time.time()} ---\n{tb}\n")
        except Exception:
            pass

    import multiprocessing as _mp

    parent = _mp.parent_process()
    next_parent_check = 0.0

    running_worker = True
    while running_worker:
      try:
        # If the server process died without shutting us down (killed, crashed),
        # save and exit instead of running on as an orphan.
        now_check = time.monotonic()
        if now_check >= next_parent_check:
            next_parent_check = now_check + 2.0
            if parent is not None and not parent.is_alive():
                print("[worker] server process is gone - saving and exiting", flush=True)
                break

        if pyboy is None:
            try:
                msg = dbg.deferred.pop(0) if dbg.deferred else cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg.get("cmd") == "shutdown":
                break
            handle_command(msg)
            continue

        try:
            while True:
                msg = next_command()
                if msg.get("cmd") == "shutdown":
                    running_worker = False
                    break
                handle_command(msg)
        except queue.Empty:
            pass
        if not running_worker:
            break
        if pyboy is None:
            continue

        if fast_forward != applied_fast_forward:

            next_frame = time.monotonic()
            applied_fast_forward = fast_forward

        if dbg.should_skip_tick():
            time.sleep(0.02)
            next_frame = time.monotonic()
            continue

        dbg.sync_breakpoints()
        alive = pyboy.tick(1, True)
        if pyboy is None:
            # a deferred stop/load ran while held at a breakpoint inside tick()
            continue
        dbg.after_frame()

        if active_cheats:
            try:
                for cheat in active_cheats:
                    pyboy.memory[cheat["address"]] = cheat["value"]
            except Exception as e:
                print(f"[worker] failed to apply cheat: {e}")

        frame_bytes = grab_frame()
        audio_bytes = grab_audio()

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

        fps_frames += 1
        if fps_frames >= FPS_REPORT_EVERY:
            now_t = time.monotonic()
            span = now_t - fps_window_start
            if span > 0:
                print(
                    f"[worker] {fps_frames / span:.2f} fps"
                    f"{' (fast-forward)' if fast_forward else ''}",
                    flush=True,
                )
            fps_window_start = now_t
            fps_frames = 0

        speed_mult = max(1, fast_forward_speed) if fast_forward else 1
        next_frame += 1.0 / (60.0 * speed_mult)
        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.25:

            next_frame = time.monotonic()
      except Exception:
        import traceback
        log_crash(traceback.format_exc())
        raise

    if pyboy is not None:
        do_stop()

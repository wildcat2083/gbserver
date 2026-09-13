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
import math
import queue
import time
import zlib
import io

import numpy as np

MSG_VIDEO = b"\x01"
MSG_AUDIO = b"\x02"


def run_worker(cmd_queue, out_queue, roms_dir, saves_dir, sound_sample_rate,
                sound_volume, audio_batch_ticks, autosave_interval_minutes, fast_forward_speed):
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

    # Set once, on the first grab_audio() call, to whichever accessor
    # this installed PyBoy version actually gets right - see grab_audio
    # below for the full reasoning. None = not probed yet.
    audio_accessor = None

    # DC-blocking high-pass, applied to every tick's audio before it leaves
    # this process. PyBoy's mixer sums four channels of 0-15 and clamps the
    # result to 0..127 (see core/sound.py's sample()), so its output is
    # UNIPOLAR - it never goes negative, and a playing note sits on a DC
    # pedestal of roughly +15 to +30 rather than swinging around zero. Real
    # DMG hardware has a coupling capacitor on the headphone output that
    # removes exactly this; PyBoy's raw buffer models the mixer but not the
    # analogue output stage, so the offset arrives intact.
    #
    # Two things it costs us. Any interruption in playback - a stop, a ROM
    # load, the browser's schedule snapping back - jumps from that pedestal
    # to true zero and back, which is a thump rather than a subtle seam. And
    # because the signal only ever occupies 0..60 of int8's 256 levels, more
    # than three quarters of the range sits unused.
    #
    # 20 Hz is below anything the Game Boy's channels actually produce, so
    # this removes the offset without touching real bass content. One pole:
    #   y[n] = x[n] - x[n-1] + R*y[n-1]
    DC_CUTOFF_HZ = 20.0
    dc_r = math.exp(-2.0 * math.pi * DC_CUTOFF_HZ / sound_sample_rate)
    # Filter state, per channel, carried across ticks so there's no seam at
    # tick boundaries. Reset on ROM load - see do_load_rom.
    dc_prev_x = np.zeros(2, dtype=np.float64)
    dc_prev_y = np.zeros(2, dtype=np.float64)
    # Once centred on zero the signal swings roughly +/-30, so doubling puts
    # it at +/-60 and, on a worst-case full 0->60 square, +/-120 - inside
    # int8 with margin to spare (verified against a synthesised worst case).
    # This adds no information that wasn't already there; the source is only
    # 61 distinct levels either way. What it buys is that those levels are
    # no longer squeezed into a quarter of the transport's range, so the
    # int8 rounding on the way out costs proportionally less.
    AUDIO_GAIN = 2.0

    # This loop, not PyBoy, owns frame pacing now (see set_emulation_speed(0)
    # in do_load_rom and the sleep at the bottom of the loop). next_frame is
    # an ABSOLUTE deadline that accumulates by exactly 1/60 every frame, so a
    # frame that overruns is made up for by the next one sleeping less -
    # PyBoy's own limiter abandons the deficit instead (it does _ftime = now
    # on an overrun), which biased the loop about 0.3% slow and steadily
    # drained the browser's audio cushion until it underran.
    next_frame = time.monotonic()
    # Rolling FPS measurement, logged so a pacing regression is visible in
    # `journalctl -u gbserver -f` rather than having to be inferred from
    # client-side audio symptoms.
    fps_window_start = time.monotonic()
    fps_frames = 0
    FPS_REPORT_EVERY = 300  # frames (~5s at 60fps)

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

    def do_load_rom(filename, load_save, ram_bytes=None):
        nonlocal pyboy, rom_path, engine_name, running, fast_forward
        nonlocal last_frame_raw, audio_accum, frame_no, active_cheats
        nonlocal next_frame, fps_window_start, fps_frames
        nonlocal dc_prev_x, dc_prev_y

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

        # ram_bytes (a "Convert Save" .sav upload - see routes.py/
        # emulator.py) requires the pyboy engine specifically. The caller
        # (Emulator.convert_sav) already checks this before ever sending
        # the command, but this is checked again here too, as a genuine
        # safety net rather than trusting the caller alone - boytacean's
        # constructor isn't confirmed to accept a ram_file the way
        # PyBoy's does, so silently ignoring this mismatch could produce
        # a confusingly-wrong boot instead of a clear error.
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
            pyboy_kwargs = dict(
                window="null",
                sound_emulated=True,
                sound_sample_rate=sound_sample_rate,
                sound_volume=sound_volume,
            )
            if ram_bytes is not None:
                # Injects the uploaded .sav's bytes as cartridge RAM,
                # exactly like inserting a real battery-backed cartridge -
                # PyBoy accepts any file-like object here, so a plain
                # BytesIO wrapper around the already-read bytes is enough,
                # no temp file needed.
                pyboy_kwargs["ram_file"] = io.BytesIO(ram_bytes)
            new_pyboy = PyBoy(str(candidate), **pyboy_kwargs)
            engine_name = "pyboy"
        # 0 = unlimited: PyBoy runs flat out and this loop does the pacing.
        # Leaving this at 1 puts two independent frame limiters in series,
        # and PyBoy's is the one that silently drops missed deadlines.
        new_pyboy.set_emulation_speed(0)

        save_load_error = None
        # ram_bytes and an existing .state load are mutually exclusive by
        # design - "Convert Save" is a deliberate fresh start with special
        # starting RAM injected, the same as "Play selected" is a
        # deliberate fresh start with nothing injected. Loading an
        # existing .state on top of injected RAM would be a confusing,
        # not-really-meaningful combination of two different starting
        # points, so this skips the .state load entirely whenever
        # ram_bytes is present, regardless of what load_save was passed.
        if ram_bytes is None and load_save and save_path.exists():
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
        # Fresh deadline - otherwise the first frame of a new ROM inherits
        # however stale next_frame had become while nothing was loaded, and
        # the loop sprints to "catch up" to a deadline that never applied.
        next_frame = time.monotonic()
        fps_window_start = next_frame
        fps_frames = 0
        # Carrying filter state across a load would settle the new session's
        # first few ms against the previous game's DC level, which isn't a
        # meaningful starting point.
        dc_prev_x = np.zeros(2, dtype=np.float64)
        dc_prev_y = np.zeros(2, dtype=np.float64)

        send_status()
        return save_load_error

    def do_save_now():
        if pyboy is None or rom_path is None:
            raise ValueError("no ROM is currently loaded")
        save_path = saves_dir / (rom_path.stem + ".state")
        with open(save_path, "wb") as f:
            pyboy.save_state(f)

    def do_extract_sav():
        """Returns the current session's cartridge RAM as raw bytes - the
        "Download .sav" button. PyBoy has no way to read this out without
        also stopping the emulator (stop(ram_file=...)), so this
        snapshots the full state first, extracts the RAM via stop(), then
        immediately reboots and restores from that snapshot - a
        near-instant pause/resume from the player's perspective, using
        PyBoy's own serialization to correctly handle any RAM banking
        rather than reading raw memory addresses directly (which would
        silently miss extra banks on larger-RAM cartridges).

        Confirmed via direct testing that this round-trip is genuinely
        lossless - CPU registers match exactly before and after.

        Only meaningful for pyboy - boytacean's stop()/save_state() API
        isn't confirmed to support this same ram_file mechanism.
        """
        nonlocal pyboy
        if pyboy is None or rom_path is None:
            raise ValueError("no ROM is currently loaded")
        if engine_name != "pyboy":
            raise ValueError("extracting a .sav requires the pyboy engine")

        snapshot = io.BytesIO()
        pyboy.save_state(snapshot)
        snapshot.seek(0)

        ram_buf = io.BytesIO()
        pyboy.stop(ram_file=ram_buf)
        ram_buf.seek(0)
        sav_bytes = ram_buf.read()

        new_pyboy = PyBoy(
            str(rom_path),
            window="null",
            sound_emulated=True,
            sound_sample_rate=sound_sample_rate,
            sound_volume=sound_volume,
        )
        new_pyboy.load_state(snapshot)
        new_pyboy.set_emulation_speed(0)  # this loop paces; see do_load_rom
        pyboy = new_pyboy

        return sav_bytes

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
        """Returns this tick's audio as raw interleaved stereo int8 bytes,
        or None if there's nothing valid to send.

        Deliberately does NOT use pyboy.sound.ndarray. That property is
        implemented as:

            raw_ndarray = frombuffer(audiobuffer).reshape(length // 2, 2)
            return raw_ndarray[:audiobuffer_head]

        - but audiobuffer_head advances by TWO per stereo sample (it's a
        flat index into the int8 array; see core/sound.py's sample()),
        while raw_ndarray's first axis is indexed by stereo SAMPLE. So
        the slice asks for twice as many rows as were actually written
        this frame, clamped by the buffer's row count, and the surplus
        rows are stale bytes from an earlier frame - clear_buffer() only
        resets the head, it never zeroes the array.

        At 36 kHz that's one bogus sample appended to every single frame
        (a 60 Hz impulse train - the buzz), growing to 30-200 bogus
        samples on the short frames PyBoy emits when the LCD toggles
        (menu/game transitions - the clicks). PyBoy's own SDL2 output
        path is unaffected because window_sdl2.py treats the head as the
        flat byte count it actually is, which is why play_headed.py
        always sounded clean on the same content.

        Reading raw_buffer_head // 2 rows out of raw_ndarray directly is
        the same thing SDL2 does, just kept in stereo-sample units. Falls
        back to the old accessor only if this PyBoy build doesn't expose
        those attributes at all, so an older/newer install still produces
        audio rather than silence.
        """
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
                n = pyboy.sound.raw_buffer_head // 2   # valid stereo samples this frame
                if n <= 0:
                    return None
                arr = pyboy.sound.raw_ndarray[:n]
            else:
                arr = pyboy.sound.ndarray
        except Exception:
            return None
        if arr is None or arr.size == 0:
            return None
        # .astype() below already copies, which matters here: raw_ndarray is
        # a live view onto the buffer PyBoy overwrites on the next tick.
        return dc_block(arr)

    def dc_block(arr_i8):
        """Removes the DC offset from one tick's samples and scales the
        result up to use more of int8's range. See the DC_CUTOFF_HZ comment
        near the top of run_worker for why this is needed at all.

        The recursion y[n] = R*y[n-1] + d[n] is solved in closed form rather
        than looped in Python, which would be ~72,000 iterations a second on
        the Pi:

            y[n] = R^n * (y[-1] + sum_{k<=n} d[k] * R^-k)

        R^-n is the thing to watch - it grows as the block gets longer. At
        one tick's worth (600 samples at 36 kHz) it reaches about 8, which
        float64 handles with enormous margin; checked against a plain
        reference loop and the two agree to ~1e-13. Filtering per tick
        rather than per batch is what keeps that exponent small, so don't
        move this to the batch send point without re-checking it.
        """
        nonlocal dc_prev_x, dc_prev_y
        x = arr_i8.astype(np.float64)          # (n, 2), copies off the live buffer
        n = x.shape[0]

        d = np.empty_like(x)
        d[0] = x[0] - dc_prev_x
        d[1:] = x[1:] - x[:-1]

        pw = (dc_r ** np.arange(1, n + 1))[:, None]
        y = pw * (dc_prev_y + np.cumsum(d / pw, axis=0))

        dc_prev_x = x[-1].copy()
        dc_prev_y = y[-1].copy()

        return np.clip(np.rint(y * AUDIO_GAIN), -127, 127).astype(np.int8).tobytes()

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
            elif cmd == "load_rom_with_ram":
                # "Convert Save" - see emulator.py's convert_sav(). Always
                # a fresh start (load_save is irrelevant here - do_load_rom
                # skips any existing .state whenever ram_bytes is given,
                # regardless), just with the uploaded .sav's bytes
                # injected as cartridge RAM instead of starting empty.
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
                # Bypasses the generic ack() helper (like load_rom above) -
                # needs to carry the extracted bytes back, not just ok/error.
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
            # Fast-forward is now purely a change of pacing target (see the
            # sleep at the bottom of the loop), not a PyBoy setting - the
            # emulator is already running unlimited. Resync the deadline on
            # the transition so switching speeds doesn't leave next_frame
            # far in the past (a burst of uncapped frames) or far in the
            # future (a stall).
            next_frame = time.monotonic()
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
            # Straight concatenation of this round's ticks, sent as-is.
            # Every sample in here was actually written by the APU this
            # frame (see grab_audio), so there's nothing to detect,
            # repair, or hold back for context: no despiking pass, and no
            # samples deferred to the next batch. Consecutive batches are
            # therefore contiguous by construction - sample N of one
            # batch is immediately followed by sample N+1 of the next,
            # with no boundary the client has to splice around.
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

        # Absolute-deadline pacing. next_frame accumulates rather than being
        # recomputed from "now", so per-frame rounding error doesn't build up
        # and an occasional slow frame (a big zlib compress, a queue that
        # briefly blocks) is absorbed by the next frame sleeping less instead
        # of permanently costing the schedule 1/60s. That drift is what was
        # draining the client's audio cushion until it underran roughly every
        # 19 seconds.
        speed_mult = max(1, fast_forward_speed) if fast_forward else 1
        next_frame += 1.0 / (60.0 * speed_mult)
        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.25:
            # More than a quarter second behind: something genuinely stalled
            # (a long autosave, heavy contention). Resync rather than trying
            # to make it up, which would sprint through frames and produce a
            # burst of audio the client can't absorb anyway.
            next_frame = time.monotonic()
      except Exception:
        import traceback
        log_crash(traceback.format_exc())
        raise

    # Clean shutdown - make sure the current state is saved, same as a
    # normal stop, rather than just vanishing mid-game.
    if pyboy is not None:
        do_stop()


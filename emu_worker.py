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

    # Shared between despike_audio's own context window and the carry-
    # over length at the batch send point below - keeping these as one
    # constant means they can't silently drift out of sync with each
    # other if either one is ever tuned later.
    DESPIKE_CONTEXT = 4
    # The glitch this corrects isn't always exactly one sample wide -
    # confirmed directly against a real capture that it can span 2, 3,
    # or 4 consecutive samples too. Caps how wide a near-zero run this
    # will treat as a candidate glitch at all - anything wider is left
    # alone as presumed-legitimate content (a real musical rest/pause is
    # much longer than this).
    DESPIKE_MAX_RUN = 6
    # How much of the tail end of each batch gets held back rather than
    # sent immediately - needs to cover a full-width run PLUS its own
    # context, since a run this wide could straddle a batch boundary
    # with only part of it visible in either batch alone.
    AUDIO_HOLDBACK = DESPIKE_CONTEXT + DESPIKE_MAX_RUN

    pyboy = None
    rom_path = None
    engine_name = "pyboy"
    running = False
    fast_forward = False
    applied_fast_forward = False
    audio_accum = bytearray()
    # Two small pieces carried across batch sends, so despiking always has
    # genuine context on BOTH sides of every sample, including right at
    # what would otherwise be a batch boundary - see the send-point
    # comment below for the full reasoning:
    #   audio_carry_context    - already-sent, confirmed-correct samples,
    #                             used ONLY as read-only "before" context
    #                             for the next round, never re-sent.
    #   audio_carry_unresolved - the trailing samples of the last batch
    #                             that didn't yet have enough "after"
    #                             context to safely evaluate, deferred
    #                             until the next batch's data arrives.
    audio_carry_context = bytearray()
    audio_carry_unresolved = bytearray()
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
        nonlocal audio_carry_context, audio_carry_unresolved

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
        new_pyboy.set_emulation_speed(1)

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
        # Reset too - carrying context across a ROM load/reset would mean
        # despiking a fresh session's very first batch against leftover
        # audio from whatever was playing before, which isn't a
        # meaningful comparison at all.
        audio_carry_context = bytearray()
        audio_carry_unresolved = bytearray()
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
        new_pyboy.set_emulation_speed(1 if not fast_forward else fast_forward_speed)
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
        if engine_name != "pyboy":
            return None
        try:
            arr = pyboy.sound.ndarray
        except Exception:
            return None
        if arr is None or arr.size == 0:
            return None
        return np.ascontiguousarray(arr).tobytes()

    def despike_audio(pcm_bytes, near_zero=3, neighbor_min=5, context=DESPIKE_CONTEXT,
                      context_std_max=1.5, max_run=DESPIKE_MAX_RUN):
        """Corrects an isolated dropout toward silence, of any width up
        to `max_run` samples - a genuine, confirmed artifact in PyBoy's
        sound.ndarray API specifically around channel-trigger/note-attack
        moments (verified directly: retriggering a channel mid-playback
        reliably produces a dropout while its immediate surroundings stay
        at the sustained level on both sides). This does NOT show up in
        PyBoy's own native SDL2 audio output (confirmed via play_headed.py
        sounding clean on the same content) - almost certainly because
        that path never goes through this specific external accessor at
        all, so this is a narrow quirk in the API surface this project
        depends on, not something fixable by changing how ticks are
        batched/concatenated here.

        Detects contiguous RUNS of near-zero samples, not just single
        isolated ones - an earlier version only ever handled exactly
        1-sample dropouts, and a real capture showed the same mechanism
        producing 2, 3, and 4-sample-wide versions too, all of which
        passed straight through untouched since nothing was looking for
        anything wider than one sample. `max_run` bounds how wide a run
        this will still treat as a candidate glitch - wider than that is
        left alone as presumed-legitimate content (a real musical rest is
        much longer than a few samples), confirmed directly against a
        deliberately long silence that it's correctly never touched.

        near_zero/neighbor_min were originally 6/12 - too high a bar,
        confirmed against a real capture: the SAME glitch mechanism
        happens just as often during quieter passages (surrounding level
        6-10, not just 15+), and a threshold requiring the surroundings
        to exceed 12 silently let all of those through untouched, since a
        genuine full drop-to-zero from a level of 6 is just as real a
        glitch as one from 15 - only the absolute size differs, not
        whether it's a glitch. Lowered both, and tightened
        context_std_max (2 -> 1.5) to compensate for the extra
        sensitivity this introduces at low volumes - re-validated the
        full test suite at these values: still catches the original loud
        glitch AND the newly-found quiet one, still leaves a legitimate
        repeating pattern and a real multi-sample edge alone, and false
        positives on realistic low-volume noise dropped to ~0.05% (was
        0.6% before tightening context_std_max).

        Checks a wider window (`context` samples) on BOTH sides of each
        run, not just the single immediate neighbor - an earlier version
        only checked one neighbor each side, and that turned out to also
        misfire on legitimate fast, high-pitched square-wave content
        whose own brief low-phase (as short as a couple of samples) looks
        identical to a real glitch if you only ever look one sample out.
        The genuine difference between the two: a real glitch sits inside
        an otherwise long, steady run of a DIFFERENT, non-zero level
        (many consistent samples on both sides); legitimate fast
        oscillation doesn't stay steady for more than a sample or two
        before flipping again. Requiring several samples of real
        consistency on each side is what actually distinguishes them -
        confirmed directly: this still catches the real, confirmed
        glitch (at every width found so far), while correctly leaving
        alone a genuinely repeating 4-sample-period square wave found in
        an actual gameplay capture that an earlier, single-neighbor
        version was incorrectly "fixing" (introducing its own distortion
        into legitimate audio).
        """
        if len(pcm_bytes) < (2 * context + 3) * 2:
            return pcm_bytes
        arr = np.frombuffer(pcm_bytes, dtype=np.int8).astype(np.int16)
        stereo = arr.reshape(-1, 2).copy()
        n = stereo.shape[0]
        for ch in range(2):
            chan = stereo[:, ch]
            is_low = np.abs(chan) < near_zero
            # Contiguous runs of near-zero samples, via edge-detection on
            # the boolean mask (pad both ends with 0/False so a run
            # touching either edge of the buffer still gets a proper
            # start/end pair).
            diff = np.diff(np.concatenate(([0], is_low.astype(np.int8), [0])))
            run_starts = np.where(diff == 1)[0]
            run_ends = np.where(diff == -1)[0]  # exclusive end index
            for start, end in zip(run_starts, run_ends):
                if end - start > max_run:
                    continue
                if start - context < 0 or end + context > n:
                    continue  # not enough real context at the very edges of this buffer
                before = chan[start - context:start].astype(np.float64)
                after = chan[end:end + context].astype(np.float64)
                if (before.std() < context_std_max and after.std() < context_std_max
                        and before.mean() > neighbor_min and after.mean() > neighbor_min
                        and abs(before.mean() - after.mean()) < context_std_max * 2):
                    chan[start:end] = int((before.mean() + after.mean()) / 2)
        return stereo.astype(np.int8).tobytes()

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
            # Combines: already-sent, confirmed-correct history (read-only
            # context, never re-sent) + the still-unresolved tail deferred
            # from last round + this round's new data - so every sample,
            # including the ones right at what would otherwise be a batch
            # boundary, gets genuine context on BOTH sides before being
            # judged. The deferred tail needed its OWN real "before"
            # context too, not just "after" context from the new batch -
            # an earlier version carried only the unresolved tail forward
            # by itself, and it turned out those samples permanently
            # lacked enough history to ever actually get evaluated, no
            # matter how much new data arrived after them. Confirmed via
            # direct testing: a glitch at the very last sample of a batch
            # now gets correctly resolved on the following round, and -
            # since despike_audio can now match a multi-sample run, not
            # just a single sample - a run split right across a batch
            # boundary (checked with a full-width run straddling the
            # split) resolves correctly too, now that the holdback covers
            # a full run's width plus its own context, not just context
            # alone.
            combined = bytes(audio_carry_context) + bytes(audio_carry_unresolved) + bytes(audio_accum)
            cleaned_combined = despike_audio(combined)
            context_bytes = AUDIO_HOLDBACK * 2  # holdback stereo-sample-pairs, 2 bytes each
            send_start = len(audio_carry_context)
            send_end = len(cleaned_combined) - context_bytes
            if send_end > send_start:
                cleaned = cleaned_combined[send_start:send_end]
                audio_carry_context = bytearray(cleaned_combined[send_end - context_bytes:send_end])
                audio_carry_unresolved = bytearray(cleaned_combined[send_end:])
            else:
                # Buffer too small this round to safely hold anything
                # back (shouldn't normally happen at real batch sizes) -
                # send everything now rather than risk losing audio.
                cleaned = cleaned_combined
                audio_carry_context = bytearray()
                audio_carry_unresolved = bytearray()
            out_queue.put({"type": "audio", "data": cleaned})
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


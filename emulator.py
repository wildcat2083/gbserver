"""
The Emulator class - one instance per session (the default shared game,
or a private room). Handles video/audio broadcast, save state, chat, and
the controller/viewer client model.

As of this version, the actual PyBoy/Boytacean instance no longer lives
in this process at all - it runs inside a dedicated subprocess (see
emu_worker.py), one per session, so each session's tick loop has its own
OS process (and therefore its own GIL) rather than every session's ticking
competing for one shared process's GIL as threads. This class proxies
PyBoy-related calls (load_rom, press/release, save_now, stop) through two
multiprocessing.Queue objects instead of calling PyBoy directly - nothing
else changes: client connections, chat, controller handoff, and the idle-
room grace timer never touched PyBoy directly in the first place, so all
of that logic is untouched here.
"""
import json
import multiprocessing
import queue
import threading
import time
from collections import deque
from pathlib import Path

from config import (
    AUDIO_BATCH_TICKS,
    AUTOSAVE_INTERVAL_MINUTES,
    BUTTON_NAMES,
    CHAT_RATE_MAX_MESSAGES,
    CHAT_RATE_WINDOW_SECONDS,
    FAST_FORWARD_SPEED,
    IDLE_CONTROLLER_TIMEOUT_SECONDS,
    KICK_CLOSE_CODE,
    MSG_AUDIO,
    MSG_VIDEO,
    NO_INPUT_TIMEOUT_SECONDS,
    ROMS_DIR,
    SOUND_SAMPLE_RATE,
)
from engine_config import get_engine_for_rom
from emu_worker import run_worker

# How long a proxy call waits for the worker's acknowledgment before
# giving up. Loading a ROM (plus a save state, on a Pi's storage) should
# always be well under this; a real timeout here almost certainly means
# the worker process itself has died, not that it's just slow.
ACK_TIMEOUT_SECONDS = 10

# --- Emulator (one instance per session - the default game, or a room) ----

class Emulator:
    def __init__(self, saves_dir, auto_stop_when_empty=False):
        self.saves_dir = saves_dir
        self.saves_dir.mkdir(parents=True, exist_ok=True)
        self.clients = {}             # connected websocket clients, in connection order
                                       # (dict used as an ordered set - the first key is
                                       # the current controller; see is_controller())
        self.clients_lock = threading.Lock()
        self.audio_batch_ticks = AUDIO_BATCH_TICKS  # live-adjustable via /api/audio-batch
        self.last_activity = time.time()  # used to reap abandoned private rooms
        self._last_frame_compressed = None  # cached, for newly-joining clients
        # When True, the ROM is stopped automatically the moment the last
        # connected client disconnects - used for the shared game, so
        # opening the page never just drops you into whatever was left
        # running; you always have to pick a ROM yourself. Left False for
        # private rooms, where quietly continuing to run while you're
        # briefly away (phone locked, laptop closed) is the point.
        self.auto_stop_when_empty = auto_stop_when_empty
        self._empty_grace_seconds = 30  # gives a refresh/brief drop time to reconnect before
                                          # clearing chat, and (for the shared game) auto-stopping
        self._auto_stop_timer = None
        self.chat_history = deque(maxlen=50)  # recent chat, shown to newly-joining clients
        self._chat_rate_limits = {}  # ws -> list of recent message timestamps
        self._client_remote_addrs = {}  # ws -> the IP they connected from - admin-only, for blocking abuse
                                          # at the network level when a client can't be reached any other way
        self.pending_control_requester = None  # ws of whoever last asked for control
        self._idle_controller_timer = None
        self._no_input_timer = None
        self._no_input_tracked_ws = None  # which client's input-silence we're currently timing
        self._controller_has_input = False  # whether that client has sent at least one press since becoming controller

        # Cached copies of the worker's own state, updated whenever a
        # "status" message arrives (see _drain_output) - reading these
        # never needs a round-trip to the worker process.
        self._current_rom_name = None
        self.engine_name = "pyboy"
        self.fast_forward = False
        self._worker_reports_running = False

        # The actual PyBoy/Boytacean instance lives entirely inside this
        # subprocess - see emu_worker.py. Started immediately (rather than
        # lazily on first load_rom) so there's no "is it alive yet" edge
        # case to handle later; it idles cheaply when nothing's loaded.
        self.cmd_queue = multiprocessing.Queue()
        self.out_queue = multiprocessing.Queue()
        self.worker_process = multiprocessing.Process(
            target=run_worker,
            args=(
                self.cmd_queue, self.out_queue, ROMS_DIR, self.saves_dir,
                SOUND_SAMPLE_RATE, AUDIO_BATCH_TICKS, AUTOSAVE_INTERVAL_MINUTES,
                FAST_FORWARD_SPEED,
            ),
            daemon=True,
        )
        self.worker_process.start()

        self._req_id_counter = 0
        self._ack_lock = threading.Lock()
        self._pending_acks = {}   # req_id -> threading.Event
        self._ack_results = {}    # req_id -> the ack message dict

        self._drain_thread = threading.Thread(target=self._drain_output, daemon=True)
        self._drain_thread.start()

    # --- Worker process communication -----------------------------------

    def _next_req_id(self):
        with self._ack_lock:
            self._req_id_counter += 1
            return self._req_id_counter

    def _send_and_wait(self, cmd):
        """Sends a command that needs a synchronous answer (load_rom, stop,
        save_now, etc.) and blocks the calling thread until the worker
        acknowledges it or ACK_TIMEOUT_SECONDS elapses. Returns the ack
        message dict; raises TimeoutError if the worker never responds
        (almost certainly means the worker process has died)."""
        req_id = self._next_req_id()
        cmd = dict(cmd, req_id=req_id)
        event = threading.Event()
        with self._ack_lock:
            self._pending_acks[req_id] = event
        self.cmd_queue.put(cmd)
        got_response = event.wait(timeout=ACK_TIMEOUT_SECONDS)
        with self._ack_lock:
            self._pending_acks.pop(req_id, None)
            result = self._ack_results.pop(req_id, None)
        if not got_response:
            raise TimeoutError(
                f"worker did not respond to {cmd.get('cmd')!r} in time - "
                f"it may have crashed"
            )
        return result

    def _drain_output(self):
        """Runs for the lifetime of this Emulator on its own thread -
        continuously reads whatever the worker process sends back and
        reacts exactly the way the old in-process tick loop used to:
        broadcasting video/audio to clients, updating cached status,
        and resolving any pending synchronous proxy calls."""
        while True:
            try:
                msg = self.out_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            msg_type = msg.get("type")

            if msg_type == "video":
                self._last_frame_compressed = msg["data"]
                self._broadcast(MSG_VIDEO + msg["data"])
            elif msg_type == "audio":
                self._broadcast(MSG_AUDIO + msg["data"])
            elif msg_type == "status":
                self._current_rom_name = msg["current_rom"]
                self.engine_name = msg["engine"]
                self.fast_forward = msg["fast_forward"]
                self._worker_reports_running = msg["running"]
                self._maybe_start_idle_controller_timer()
                self._maybe_start_no_input_timer()
            elif msg_type == "stopped":
                self._last_frame_compressed = None
                self._broadcast_stopped()
                self._maybe_start_idle_controller_timer()  # e.g. game-over autosave-stop
                self._maybe_start_no_input_timer()
            elif msg_type == "ack":
                req_id = msg["req_id"]
                with self._ack_lock:
                    if req_id in self._pending_acks:
                        self._ack_results[req_id] = msg
                        self._pending_acks[req_id].set()

    # --- ROM library (unchanged - never touched PyBoy directly) ---------

    def touch(self):
        self.last_activity = time.time()

    def list_roms(self):
        return sorted(
            p.name for p in list(ROMS_DIR.glob("*.gb")) + list(ROMS_DIR.glob("*.gbc"))
        )

    def rom_library_info(self):
        """Per-ROM metadata for the settings panel: size, whether a save
        exists, and which engine this ROM is set to run on."""
        info = []
        for name in self.list_roms():
            p = ROMS_DIR / name
            save_path = self.saves_dir / (p.stem + ".state")
            info.append({
                "filename": name,
                "size_bytes": p.stat().st_size,
                "has_save": save_path.exists(),
                "engine": get_engine_for_rom(name),
            })
        return info

    def library_total_bytes(self):
        return sum((ROMS_DIR / n).stat().st_size for n in self.list_roms())

    def current_rom_name(self):
        # Only report a ROM as "current" while it is actually running -
        # after Stop, the name would otherwise still show as playing,
        # blocking restart.
        if not self._worker_reports_running:
            return None
        return self._current_rom_name

    def _save_path_for(self, rom_path):
        return self.saves_dir / (rom_path.stem + ".state")

    def delete_rom(self, filename):
        if self._current_rom_name == filename:
            self.stop()
        rom_path = ROMS_DIR / filename
        if rom_path.exists():
            rom_path.unlink()
        save_path = self.saves_dir / (Path(filename).stem + ".state")
        if save_path.exists():
            save_path.unlink()

    # --- PyBoy-related, now proxied to the worker process ---------------

    def load_rom(self, filename, load_save=True):
        """Stop any running emulator and start a fresh one on `filename`.
        With load_save=True (the default), any existing save for this ROM
        is loaded automatically - this is what upload_save() and
        delete_save() rely on. Pass load_save=False to always start clean
        regardless of what's on disk - that's what "Play selected" uses,
        so starting a ROM is a deliberate fresh start; resuming an
        existing save is its own separate, explicit action ("Resume save").

        Returns None if any existing save loaded cleanly (or there was
        none to load, or load_save was False), or an error string if a
        save existed and load_save was True but it failed to load (game
        still starts, just without that save applied)."""
        if not (ROMS_DIR / filename).exists():
            raise FileNotFoundError(filename)
        ack = self._send_and_wait({
            "cmd": "load_rom", "filename": filename, "load_save": load_save,
        })
        if not ack["ok"]:
            raise RuntimeError(ack.get("error") or "load_rom failed in worker")
        # Optimistic update, ahead of the worker's own async status message -
        # cancels the idle-controller timer immediately rather than leaving
        # a stale timer running for however long that status message takes
        # to actually arrive and get processed.
        self._worker_reports_running = True
        self._maybe_start_idle_controller_timer()
        self._maybe_start_no_input_timer()
        return ack.get("error")  # None on clean load; a save-load error string otherwise

    def stop(self):
        self._send_and_wait({"cmd": "stop"})

    def reset(self):
        """Restarts the currently-loaded ROM completely fresh - the same
        thing a physical reset button does on real hardware, or what
        "Play selected" does when first starting a ROM. Discards all
        progress since the ROM was last loaded, but never touches the
        save file on disk either way - saving is always its own,
        separate, deliberate action (Save now / autosave / Resume
        save), never something a reset implicitly does as a side
        effect. Raises ValueError if nothing is currently loaded, since
        there'd be nothing to reset."""
        if self._current_rom_name is None:
            raise ValueError("no ROM is currently loaded")
        self.load_rom(self._current_rom_name, load_save=False)

    def shutdown(self):
        """Stops the game (if running) AND terminates the worker process
        entirely - used when a session is being torn down for good (idle
        room reaping), as opposed to stop(), which just ends the current
        game but leaves the worker process running and ready for another
        ROM to be loaded. Without this, a reaped room's worker process
        would sit around idling forever - one whole OS process per stale
        room, never actually freed."""
        try:
            self.cmd_queue.put({"cmd": "shutdown"})
        except Exception:
            pass
        self.worker_process.join(timeout=5)
        if self.worker_process.is_alive():
            # The worker should always exit cleanly on its own from the
            # shutdown command above - this is just a defensive fallback
            # in case it's stuck for some reason, so a single wedged
            # worker can't block the reaper from freeing everything else.
            self.worker_process.terminate()
            self.worker_process.join(timeout=2)

    def has_save(self):
        if self._current_rom_name is None:
            return False
        return self._save_path_for(ROMS_DIR / self._current_rom_name).exists()

    def save_file_path(self):
        """Path to the current ROM's save file, or None if there's no ROM/save."""
        if self._current_rom_name is None:
            return None
        p = self._save_path_for(ROMS_DIR / self._current_rom_name)
        return p if p.exists() else None

    def upload_save(self, file_storage):
        """Write an uploaded .state file and (re)start the matching ROM
        with it applied - either the currently-loaded ROM, or, if nothing
        is currently playing, whichever ROM's filename matches the
        upload's own name.

        Returns (rom_name, save_load_error): rom_name is whichever ROM
        the save was actually applied to (the caller needs this to sync
        the dropdown selection to match - without it, "Resume save"
        afterward checks whatever ROM the dropdown happened to already
        be showing, which silently disables the button if that isn't the
        same ROM the upload just applied to). save_load_error is None if
        the save applied cleanly, or an error string if it was written
        but PyBoy couldn't actually load it."""
        rom_name = self._current_rom_name

        if rom_name is None:
            stem = Path(file_storage.filename).stem
            candidate_gb = ROMS_DIR / f"{stem}.gb"
            candidate_gbc = ROMS_DIR / f"{stem}.gbc"
            if candidate_gb.exists():
                rom_name = candidate_gb.name
            elif candidate_gbc.exists():
                rom_name = candidate_gbc.name
            else:
                raise ValueError(
                    f'No ROM is currently loaded, and no ROM named "{stem}.gb" or '
                    f'"{stem}.gbc" was found in the library to match this save. '
                    f"Load the matching ROM first, or make sure the .state file's "
                    f"name matches the ROM's filename."
                )

        save_path = self._save_path_for(ROMS_DIR / rom_name)
        file_storage.save(save_path)
        save_load_error = self.load_rom(rom_name)  # (re)starts the ROM with the new save applied
        return rom_name, save_load_error

    def delete_save(self):
        """Deletes the on-disk save for the current ROM only - the
        currently-running game (if any) keeps playing exactly as it was,
        uninterrupted."""
        if self._current_rom_name is None:
            raise ValueError("no ROM is currently loaded")
        save_path = self._save_path_for(ROMS_DIR / self._current_rom_name)
        if save_path.exists():
            save_path.unlink()

    def press(self, button):
        if button not in BUTTON_NAMES:
            return
        # This IS the controller sending real input - see
        # _maybe_start_no_input_timer's docstring for why that
        # specifically matters (a bot holding the connection open without
        # ever pressing anything is exactly what that timer exists to
        # catch, so proving otherwise here needs to permanently clear it
        # for as long as this same client remains controller).
        self._controller_has_input = True
        self._cancel_no_input_timer()
        self.cmd_queue.put({"cmd": "press", "button": button})

    def release(self, button):
        if button not in BUTTON_NAMES:
            return
        self.cmd_queue.put({"cmd": "release", "button": button})

    def save_now(self):
        """Explicit, on-demand save - the "unless a button is pushed" path,
        separate from the periodic background autosave and from the
        always-saves-before-closing behavior of stop(). Raises ValueError
        if nothing is currently running to save."""
        ack = self._send_and_wait({"cmd": "save_now"})
        if not ack["ok"]:
            raise ValueError(ack.get("error") or "save_now failed")

    def set_fast_forward(self, enabled):
        self.cmd_queue.put({"cmd": "set_fast_forward", "enabled": enabled})
        self.fast_forward = enabled  # optimistic local update; the worker's
                                      # own status message will confirm shortly

    # --- Broadcasting (unchanged - never touched PyBoy directly) --------

    def _broadcast_stopped(self):
        """Tells every currently-connected client the game just stopped, so
        anyone watching who *didn't* click Stop themselves also sees the
        screen go blank immediately, instead of staying frozen on the last
        frame until they happen to refresh."""
        with self.clients_lock:
            clients = list(self.clients)
        for ws in clients:
            try:
                ws.send("stopped")
            except Exception:
                pass

    def _broadcast(self, frame_bytes):
        dead = []
        with self.clients_lock:
            clients = list(self.clients)
        for ws in clients:
            try:
                ws.send(frame_bytes)
            except Exception:
                dead.append(ws)
        if dead:
            with self.clients_lock:
                for ws in dead:
                    self.clients.pop(ws, None)
            self._notify_controller_status()  # dropped client may have been the controller

    def add_client(self, ws, client_id=None, remote_addr=None):
        with self.clients_lock:
            self.clients[ws] = client_id
            self._client_remote_addrs[ws] = remote_addr
        self.touch()
        self._notify_controller_status()
        self._cancel_empty_grace_timer()  # someone (re)connected - don't clear/stop out from under them
        self._maybe_start_idle_controller_timer()
        self._maybe_start_no_input_timer()
        # Frames are now only broadcast when the screen actually changes -
        # send this new client the current screen right away so they don't
        # have to wait for the next real change (which might not happen for
        # a while on a static/menu screen).
        if self._last_frame_compressed is not None:
            try:
                ws.send(MSG_VIDEO + self._last_frame_compressed)
            except Exception:
                pass
        # Catch this new client up on recent chat, so joining partway
        # through a conversation isn't just silence.
        for entry in self.chat_history:
            try:
                ws.send("chatmsg:" + json.dumps(entry))
            except Exception:
                pass
        try:
            ws.send("fastforward:1" if self.fast_forward else "fastforward:0")
        except Exception:
            pass

    def remove_client(self, ws):
        with self.clients_lock:
            was_controller = bool(self.clients) and next(iter(self.clients)) is ws
            self.clients.pop(ws, None)
            now_empty = len(self.clients) == 0
        self._chat_rate_limits.pop(ws, None)
        self._client_remote_addrs.pop(ws, None)
        if self.pending_control_requester is ws:
            self.pending_control_requester = None
        if was_controller:
            # The controller may have disconnected mid-press (dropped
            # connection, closed tab) with no release message ever
            # arriving - release everything defensively so a button
            # doesn't stay stuck held for whoever takes over control.
            for button in BUTTON_NAMES:
                self.release(button)
        self._notify_controller_status()
        if now_empty:
            self._schedule_empty_grace_timer()
            self._cancel_idle_controller_timer()  # nobody to be "idle controller" of anymore
        else:
            self._maybe_start_idle_controller_timer()  # a new controller may now need monitoring
            self._maybe_start_no_input_timer()

    def _schedule_empty_grace_timer(self):
        """Starts (or restarts) the grace-period timer for when this
        session sits empty. Gives a browser refresh or brief network drop
        time to reconnect before anything happens - cancelled immediately
        if anyone does reconnect (see add_client). Once the grace period
        actually elapses (see _on_empty_grace_expired), chat history is
        always cleared; the running game is additionally auto-stopped, but
        only for sessions configured for that (auto_stop_when_empty - the
        shared game, not private rooms)."""
        self._cancel_empty_grace_timer()
        timer = threading.Timer(self._empty_grace_seconds, self._on_empty_grace_expired)
        timer.daemon = True
        self._auto_stop_timer = timer
        timer.start()

    def _cancel_empty_grace_timer(self):
        if self._auto_stop_timer is not None:
            self._auto_stop_timer.cancel()
            self._auto_stop_timer = None

    def _on_empty_grace_expired(self):
        """Runs on the timer's own thread once the grace period elapses -
        only actually does anything if the session is still empty; a
        reconnect in the meantime already cancelled this via
        _cancel_empty_grace_timer, but this check is a second guard
        against any race between the two."""
        with self.clients_lock:
            still_empty = len(self.clients) == 0
        if still_empty:
            self.chat_history.clear()
            if self.auto_stop_when_empty and self._worker_reports_running:
                self.stop()  # already autosaves before stopping
        self._auto_stop_timer = None

    def _maybe_start_idle_controller_timer(self):
        """Starts (or restarts) the idle-controller timer if there's
        currently a controller and nothing loaded; cancels it otherwise.
        Called from every place that could change either of those two
        things (a new client connecting, a control handoff, a ROM
        successfully loading, a game stopping) rather than tracking each
        individual transition by hand - simpler and less error-prone than
        enumerating every path that could affect this."""
        with self.clients_lock:
            has_controller = bool(self.clients)
        if has_controller and not self._worker_reports_running:
            self._cancel_idle_controller_timer()
            timer = threading.Timer(
                IDLE_CONTROLLER_TIMEOUT_SECONDS, self._on_idle_controller_timeout
            )
            timer.daemon = True
            self._idle_controller_timer = timer
            timer.start()
        else:
            self._cancel_idle_controller_timer()

    def _cancel_idle_controller_timer(self):
        if self._idle_controller_timer is not None:
            self._idle_controller_timer.cancel()
            self._idle_controller_timer = None

    def _on_idle_controller_timeout(self):
        """Runs on the timer's own thread once the idle window elapses -
        hands control to the next connected client if the current
        controller still hasn't loaded anything, so one person sitting
        idle doesn't permanently block everyone else from starting
        something. The new controller gets the same monitoring restarted
        (see the call at the end) - if the pattern repeats, control keeps
        cycling forward rather than getting stuck on one person."""
        with self.clients_lock:
            self._idle_controller_timer = None
            if not self.clients or self._worker_reports_running:
                return  # nobody connected, or a ROM got loaded in the meantime
            if len(self.clients) < 2:
                return  # nobody else to hand control to
            # Move the current (idle) controller to the back of the line -
            # popping then re-inserting a dict key moves it to the end of
            # iteration order, which is exactly what "no longer first" means
            # here (the same ordering grant_control relies on, just the
            # opposite direction).
            current_ws, current_id = next(iter(self.clients.items()))
            self.clients.pop(current_ws)
            self.clients[current_ws] = current_id
        self._notify_controller_status()
        self._maybe_start_idle_controller_timer()
        self._maybe_start_no_input_timer()

    def _maybe_start_no_input_timer(self):
        """Complementary to _maybe_start_idle_controller_timer above, but
        catches a case that one doesn't: a controller connecting to an
        ALREADY-RUNNING game (the common case for the shared room, since
        it's typically left playing something from a previous session)
        who never sends a single button press. The timer above only
        starts when nothing is loaded yet, so it never applies to that
        scenario at all - without this, a bot (a link-preview crawler
        rendering the page, a network scanner completing the WebSocket
        handshake, anything that opens the connection without ever
        sending real input) could sit as controller indefinitely with no
        timeout whatsoever. Deliberately shorter than the timer above -
        "connected and has never sent a single input" is a much stronger
        signal than "hasn't picked a ROM yet", so it's reasonable to act
        on it faster.

        Tracks which specific client is currently being monitored
        (_no_input_tracked_ws) so that calling this repeatedly from
        unrelated events (some other viewer joining or leaving) doesn't
        keep resetting an already-running countdown for the same
        still-unproven controller - only a genuine change of controller
        resets the monitoring state, and an already-active timer for the
        same controller is left alone rather than restarted from
        scratch."""
        with self.clients_lock:
            current_controller_ws = next(iter(self.clients), None)
            has_other_clients = len(self.clients) >= 2

        if current_controller_ws is None:
            self._cancel_no_input_timer()
            self._no_input_tracked_ws = None
            return

        if current_controller_ws is not self._no_input_tracked_ws:
            # A different (or first-ever) controller - any existing timer
            # belonged to whoever came before them.
            self._no_input_tracked_ws = current_controller_ws
            self._controller_has_input = False
            self._cancel_no_input_timer()

        if self._controller_has_input or not has_other_clients:
            self._cancel_no_input_timer()
            return

        if self._no_input_timer is None:  # don't restart an already-running countdown
            timer = threading.Timer(NO_INPUT_TIMEOUT_SECONDS, self._on_no_input_timeout)
            timer.daemon = True
            self._no_input_timer = timer
            timer.start()

    def _cancel_no_input_timer(self):
        if self._no_input_timer is not None:
            self._no_input_timer.cancel()
            self._no_input_timer = None

    def _on_no_input_timeout(self):
        """Same "move the current controller to the back of the line"
        behavior as _on_idle_controller_timeout, just triggered by a
        different condition (never sent input at all, vs. never loaded a
        ROM)."""
        with self.clients_lock:
            self._no_input_timer = None
            if not self.clients or self._controller_has_input:
                return  # nobody connected, or they've since proven they're active
            if len(self.clients) < 2:
                return  # nobody else to hand control to
            current_ws, current_id = next(iter(self.clients.items()))
            self.clients.pop(current_ws)
            self.clients[current_ws] = current_id
        self._notify_controller_status()
        self._no_input_tracked_ws = None  # force the next call to treat the new controller as fresh
        self._maybe_start_idle_controller_timer()
        self._maybe_start_no_input_timer()

    def kick_client(self, client_id):
        """Forcibly disconnects the client matching `client_id`, if
        currently connected. Used by the admin-only kick feature (see
        admin.py) - the WebSocket handler's own receive loop notices the
        disconnection and runs the exact same cleanup (remove_client,
        controller reassignment, etc.) as any other disconnect, so none
        of that logic needs duplicating here.

        Uses KICK_CLOSE_CODE specifically (rather than a normal closure)
        so the client's own auto-reconnect logic can tell a deliberate
        kick apart from any other disconnect reason and skip reconnecting
        just for this one case - otherwise the client's browser simply
        reconnects within about a second, making the kick look like it
        did nothing at all.

        ws.close() alone isn't enough either way - it only writes a
        WebSocket close frame and sets a local flag, neither of which
        interrupts a DIFFERENT thread that's already blocked inside a
        low-level socket.recv() call waiting for that client to send
        something (which it normally is, most of the time). Actually
        shutting down the underlying OS socket is what reliably forces
        that blocked call to return immediately - but only after a brief
        delay (see below), not right away."""
        with self.clients_lock:
            target_ws = None
            for ws, cid in self.clients.items():
                if cid == client_id:
                    target_ws = ws
                    break
        if target_ws is not None:
            try:
                target_ws.close(reason=KICK_CLOSE_CODE, message="Disconnected by an admin")
            except Exception:
                pass
            # Shutting down the socket immediately after sending the close
            # frame, with no gap at all, was cutting the WebSocket closing
            # handshake short - the client's browser needs a moment to
            # actually receive and process that frame as a CLEAN closure
            # (carrying KICK_CLOSE_CODE) before the underlying connection
            # goes away; skip that gap and the browser sees an ABNORMAL
            # closure instead (code 1006, the code browsers use for a
            # connection lost without completing the close handshake),
            # which the client-side check for KICK_CLOSE_CODE can never
            # match - so it falls through to the normal auto-reconnect
            # path anyway, making the kick look like it did nothing.
            # Runs on its own thread so the admin's own request doesn't
            # wait on this half-second delay.
            def _delayed_shutdown():
                time.sleep(0.5)
                try:
                    import socket as socket_module
                    target_ws.sock.shutdown(socket_module.SHUT_RDWR)
                except Exception:
                    pass  # already closed on its own by then - the common case
            threading.Thread(target=_delayed_shutdown, daemon=True).start()
            return True
        return False

    def redirect_client(self, client_id, room_code):
        """Sends the client matching `client_id` a message telling their
        browser to navigate to a different room, if currently connected.
        Used by the admin "move to a private room" action - unlike kick,
        this doesn't need any close-code trickery to stop an auto-
        reconnect loop, since a full page navigation naturally tears down
        the old WebSocket connection with no lingering JS context left
        behind to try reconnecting to the old session at all."""
        with self.clients_lock:
            target_ws = None
            for ws, cid in self.clients.items():
                if cid == client_id:
                    target_ws = ws
                    break
        if target_ws is None:
            return False
        try:
            target_ws.send(f"redirect:{room_code}")
        except Exception:
            return False
        return True

    def has_client(self, client_id):
        """True if a client with this ID is currently connected - used to
        check a redirect will actually reach someone BEFORE creating the
        room to send them to, so a client who's already gone doesn't
        leave an orphaned, empty room behind with nobody ever sent there."""
        with self.clients_lock:
            return any(cid == client_id for cid in self.clients.values())

    def is_controller(self, ws):
        """Only the earliest-connected still-open client may send button
        input - everyone else is view-only. If the controller disconnects,
        the next-oldest remaining client automatically becomes the
        controller (no one needs to explicitly hand it off)."""
        with self.clients_lock:
            if not self.clients:
                return False
            return next(iter(self.clients)) is ws

    def request_control(self, ws):
        """A viewer asks the current controller to hand over control.
        Notifies only the controller, with a prompt they can accept
        (grant_control) or simply ignore - the requester isn't told
        whether their request was seen or granted, beyond the normal
        controller-status broadcast if/when it actually happens."""
        with self.clients_lock:
            if not self.clients or next(iter(self.clients)) is ws:
                return  # nobody connected, or already the controller
            controller_ws = next(iter(self.clients))
            self.pending_control_requester = ws
        try:
            controller_ws.send("controlrequested:1")
        except Exception:
            pass

    def grant_control(self, granter_ws):
        """The current controller hands control to whoever most recently
        called request_control(). No-op if granter_ws isn't actually the
        controller, or nobody's currently asking (including if they asked
        but have since disconnected)."""
        with self.clients_lock:
            if not self.clients or next(iter(self.clients)) is not granter_ws:
                return
            requester_ws = self.pending_control_requester
            if requester_ws is None or requester_ws not in self.clients:
                self.pending_control_requester = None
                return
            # Move the requester to the front of the ordered clients dict -
            # that's what "being the controller" means everywhere else in
            # this class (is_controller just checks who's first).
            client_id = self.clients.pop(requester_ws)
            reordered = {requester_ws: client_id}
            reordered.update(self.clients)
            self.clients = reordered
            self.pending_control_requester = None
        self._notify_controller_status()
        self._maybe_start_idle_controller_timer()  # the new controller may need monitoring too
        self._maybe_start_no_input_timer()

    def add_chat_message(self, ws, text, name=None):
        """Records and broadcasts a chat message from `ws`. Labeled by
        their current role (controller/viewer) by default, or by their own
        chosen display name if they've set one - unlike button input, chat
        is open to everyone, viewers included. The role is still recorded
        either way, so the UI can keep color-coding by controller/viewer
        even when a custom name is shown.

        Chat arrives over the WebSocket, not a regular HTTP request, so
        Flask-Limiter's route decorators can't reach it - rate limiting
        here is a simple manual sliding window per connection instead."""
        now = time.time()
        recent = self._chat_rate_limits.setdefault(ws, [])
        recent[:] = [t for t in recent if now - t < CHAT_RATE_WINDOW_SECONDS]
        if len(recent) >= CHAT_RATE_MAX_MESSAGES:
            return  # silently drop - no error message, just a no-op like a debounce
        recent.append(now)

        text = text.strip()[:200]  # cap length - this is chat, not a text field
        if not text:
            return
        name = (name or "").strip()[:24]  # cap length - a name, not a text field
        role = "controller" if self.is_controller(ws) else "viewer"
        entry = {"role": role, "name": name, "text": text, "ts": time.time()}
        self.chat_history.append(entry)
        payload = "chatmsg:" + json.dumps(entry)
        dead = []
        with self.clients_lock:
            clients = list(self.clients)
        for client_ws in clients:
            try:
                client_ws.send(payload)
            except Exception:
                dead.append(client_ws)
        if dead:
            with self.clients_lock:
                for client_ws in dead:
                    self.clients.pop(client_ws, None)

    def controller_client_id(self):
        """The client_id (sent by the browser on WS connect) belonging to
        the current controller, or None if nobody's connected. Lets plain
        HTTP requests (Settings actions) be checked against the same
        controller identity as WebSocket button input, even though HTTP
        requests aren't tied to any particular WebSocket connection."""
        with self.clients_lock:
            if not self.clients:
                return None
            first_ws = next(iter(self.clients))
            return self.clients[first_ws]

    def is_controller_client(self, client_id):
        if not client_id:
            return False
        return self.controller_client_id() == client_id

    def _notify_controller_status(self):
        """Tells every connected client, individually, whether *they*
        currently hold control - sent as a small text message alongside
        the usual binary video/audio stream. Also broadcasts the current
        viewer count to everyone here, since this already fires exactly
        when that count could have changed (someone joining, leaving,
        being kicked, control changing hands) - no separate hook needed."""
        with self.clients_lock:
            clients = list(self.clients)
        controller = clients[0] if clients else None
        viewer_count = max(0, len(clients) - 1)
        for ws in clients:
            try:
                ws.send("controller:1" if ws is controller else "controller:0")
                ws.send(f"viewers:{viewer_count}")
            except Exception:
                pass

    def _notify_fast_forward_status(self):
        """Broadcasts the current fast-forward state to every connected
        client, so a viewer's UI reflects it too, not just whoever
        toggled it."""
        with self.clients_lock:
            clients = list(self.clients)
        payload = "fastforward:1" if self.fast_forward else "fastforward:0"
        for ws in clients:
            try:
                ws.send(payload)
            except Exception:
                pass

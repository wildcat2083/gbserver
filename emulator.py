import json
import multiprocessing
import queue
import threading
import time
from collections import deque

import numpy as np
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
    ROOM_SAVES_DIR,
    SAVES_DIR,
    SOUND_SAMPLE_RATE,
    SOUND_VOLUME,
    safe_rom_name,
)
from engine_config import get_engine_for_rom
from debug_core import REGIONS as DEBUG_REGIONS
from emu_worker import run_worker


ACK_TIMEOUT_SECONDS = 10


class _ClientStream:

    MAX_VIDEO_QUEUED = 3
    MAX_AUDIO_QUEUED = 12
    MAX_TOTAL_QUEUED = 128

    def __init__(self, ws, on_dead):
        self.ws = ws
        self._on_dead = on_dead
        self._cv = threading.Condition()
        self._queue = deque()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def send(self, payload, kind="control"):
        with self._cv:
            if self._closed:
                return
            if kind == "video":
                self._trim(kind, self.MAX_VIDEO_QUEUED)
            elif kind == "audio":
                self._trim(kind, self.MAX_AUDIO_QUEUED)
            if len(self._queue) >= self.MAX_TOTAL_QUEUED:

                for i, (k, _) in enumerate(self._queue):
                    if k != "control":
                        del self._queue[i]
                        break
                else:
                    return
            self._queue.append((kind, payload))
            self._cv.notify()

    def _trim(self, kind, limit):
        while sum(1 for k, _ in self._queue if k == kind) >= limit:
            for i, (k, _) in enumerate(self._queue):
                if k == kind:
                    del self._queue[i]
                    break
            else:
                return

    def _run(self):
        while True:
            with self._cv:
                while not self._queue and not self._closed:
                    self._cv.wait()
                if not self._queue:
                    return
                _kind, payload = self._queue.popleft()
            try:
                self.ws.send(payload)
            except Exception:

                self.close()
                try:
                    self._on_dead(self.ws)
                except Exception:
                    pass
                return

    def close(self):
        with self._cv:
            self._closed = True
            self._queue.clear()
            self._cv.notify()


class Emulator:
    def __init__(self, saves_dir, auto_stop_when_empty=False):
        self.saves_dir = saves_dir
        self.saves_dir.mkdir(parents=True, exist_ok=True)
        self.clients = {}

        self.clients_lock = threading.Lock()
        self._streams = {}
        self.audio_batch_ticks = AUDIO_BATCH_TICKS
        self.last_activity = time.time()
        self._last_frame_compressed = None

        self._metrics_buckets = deque()
        self._metrics_lock = threading.Lock()

        self.auto_stop_when_empty = auto_stop_when_empty
        self._empty_grace_seconds = 30

        self._auto_stop_timer = None
        self.chat_history = deque(maxlen=50)
        self._chat_rate_limits = {}
        self._client_remote_addrs = {}

        self.pending_control_requester = None
        self._idle_controller_timer = None
        self._no_input_timer = None
        self._no_input_tracked_ws = None
        self._controller_has_input = False

        self._debug_owner = None
        self._debug_paused = False
        self._debug_search = {}
        self._debug_rate = {}

        self._current_rom_name = None
        self.engine_name = "pyboy"
        self.fast_forward = False
        self._worker_reports_running = False

        self.cmd_queue = multiprocessing.Queue()
        self.out_queue = multiprocessing.Queue()
        self.worker_process = multiprocessing.Process(
            target=run_worker,
            args=(
                self.cmd_queue, self.out_queue, ROMS_DIR, self.saves_dir,
                SOUND_SAMPLE_RATE, SOUND_VOLUME, AUDIO_BATCH_TICKS, AUTOSAVE_INTERVAL_MINUTES,
                FAST_FORWARD_SPEED,
            ),
            daemon=True,
        )
        self.worker_process.start()

        self._req_id_counter = 0
        self._ack_lock = threading.Lock()
        self._pending_acks = {}
        self._ack_results = {}

        self._drain_thread = threading.Thread(target=self._drain_output, daemon=True)
        self._drain_thread.start()

    def _next_req_id(self):
        with self._ack_lock:
            self._req_id_counter += 1
            return self._req_id_counter

    def _send_and_wait(self, cmd):
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
        while True:
            try:
                msg = self.out_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            msg_type = msg.get("type")

            if msg_type == "video":
                self._last_frame_compressed = msg["data"]
                self._metric_observe("video", len(msg["data"]) + 1)
                self._broadcast(MSG_VIDEO + msg["data"], "video")
            elif msg_type == "audio":
                self._metric_observe("audio", len(msg["data"]) + 1)
                self._broadcast(MSG_AUDIO + msg["data"], "audio")
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
                self._maybe_start_idle_controller_timer()
                self._maybe_start_no_input_timer()
            elif msg_type == "debug":
                state = msg.get("state") or {}
                self._debug_paused = bool(state.get("paused"))
                self._broadcast("dbgevt:" + json.dumps({"event": msg.get("event"), "state": state}))
            elif msg_type == "ack":
                req_id = msg["req_id"]
                with self._ack_lock:
                    if req_id in self._pending_acks:
                        self._ack_results[req_id] = msg
                        self._pending_acks[req_id].set()

    def _metric_observe(self, kind, payload_len):
        now = time.monotonic()
        with self.clients_lock:
            viewer_mult = max(1, len(self.clients))
        with self._metrics_lock:
            if self._metrics_buckets and now - self._metrics_buckets[-1]["t"] < 1.0:
                bucket = self._metrics_buckets[-1]
            else:
                bucket = {"t": now, "video_bytes": 0, "audio_bytes": 0, "frames": 0}
                self._metrics_buckets.append(bucket)
            if kind == "video":
                bucket["frames"] += 1
                bucket["video_bytes"] += payload_len * viewer_mult
            else:
                bucket["audio_bytes"] += payload_len * viewer_mult

    def metrics_snapshot(self, window_seconds=10):
        now = time.monotonic()
        with self._metrics_lock:
            while self._metrics_buckets and now - self._metrics_buckets[0]["t"] > window_seconds:
                self._metrics_buckets.popleft()
            buckets = list(self._metrics_buckets)
        if not buckets:
            return {"fps": 0, "video_kbps": 0.0, "audio_kbps": 0.0, "total_kbps": 0.0}
        frames = sum(b["frames"] for b in buckets)
        video = sum(b["video_bytes"] for b in buckets)
        audio = sum(b["audio_bytes"] for b in buckets)
        span = max(1e-6, buckets[-1]["t"] - buckets[0]["t"] + 1.0)
        return {
            "fps": round(frames / span, 1),
            "video_kbps": round(video * 8 / 1000 / span, 1),
            "audio_kbps": round(audio * 8 / 1000 / span, 1),
            "total_kbps": round((video + audio) * 8 / 1000 / span, 1),
        }

    def worker_info(self):
        process = self.worker_process
        crash_stats = None
        try:
            stat = (self.saves_dir.parent / "worker_crash.log").stat()
            crash_stats = {"size_bytes": stat.st_size, "mtime": stat.st_mtime}
        except OSError:
            pass
        return {
            "alive": process.is_alive(),
            "pid": process.pid,
            "exitcode": process.exitcode,
            "crash_log": crash_stats,
        }

    def touch(self):
        self.last_activity = time.time()

    def list_roms(self):
        return sorted(
            p.name for p in list(ROMS_DIR.glob("*.gb")) + list(ROMS_DIR.glob("*.gbc"))
        )

    @staticmethod
    def save_locations_index():
        """Map ROM stem -> where a .state exists: "shared" and/or room codes.

        Scans saves/*.state and saves/rooms/<code>/*.state once, so the
        library listing stays cheap even with hundreds of ROMs.
        """
        index = {}
        for p in SAVES_DIR.glob("*.state"):
            index.setdefault(p.stem, []).append("shared")
        for p in sorted(ROOM_SAVES_DIR.glob("*/*.state")):
            index.setdefault(p.stem, []).append(p.parent.name)
        return index

    def rom_library_info(self):
        info = []
        locations = self.save_locations_index()
        for name in self.list_roms():
            p = ROMS_DIR / name
            save_path = self.saves_dir / (p.stem + ".state")
            info.append({
                "filename": name,
                "size_bytes": p.stat().st_size,
                # has_save: this session's own save (drives Resume on the player page)
                "has_save": save_path.exists(),
                # save_locations: every save for this ROM - shared and any room
                "save_locations": locations.get(p.stem, []),
                "engine": get_engine_for_rom(name),
            })
        return info

    def library_total_bytes(self):
        return sum((ROMS_DIR / n).stat().st_size for n in self.list_roms())

    def current_rom_name(self):

        if not self._worker_reports_running:
            return None
        return self._current_rom_name

    def _save_path_for(self, rom_path):
        return self.saves_dir / (rom_path.stem + ".state")

    def delete_rom(self, filename):
        filename = safe_rom_name(filename)
        if self._current_rom_name == filename:
            self.stop()
        rom_path = ROMS_DIR / filename
        if rom_path.exists():
            rom_path.unlink()
        save_path = self.saves_dir / (Path(filename).stem + ".state")
        if save_path.exists():
            save_path.unlink()

    def load_rom(self, filename, load_save=True):
        try:
            filename = safe_rom_name(filename)
        except ValueError:
            raise FileNotFoundError(filename)
        if not (ROMS_DIR / filename).exists():
            raise FileNotFoundError(filename)
        ack = self._send_and_wait({
            "cmd": "load_rom", "filename": filename, "load_save": load_save,
        })
        if not ack["ok"]:
            raise RuntimeError(ack.get("error") or "load_rom failed in worker")

        self._worker_reports_running = True
        self._maybe_start_idle_controller_timer()
        self._maybe_start_no_input_timer()
        return ack.get("error")

    def stop(self):
        self._send_and_wait({"cmd": "stop"})

    def reset(self):
        if self._current_rom_name is None:
            raise ValueError("no ROM is currently loaded")
        self.load_rom(self._current_rom_name, load_save=False)

    def shutdown(self):
        try:
            self.cmd_queue.put({"cmd": "shutdown"})
        except Exception:
            pass
        self.worker_process.join(timeout=5)
        if self.worker_process.is_alive():

            self.worker_process.terminate()
            self.worker_process.join(timeout=2)

    def has_save(self):
        if self._current_rom_name is None:
            return False
        return self._save_path_for(ROMS_DIR / self._current_rom_name).exists()

    def save_file_path(self):
        if self._current_rom_name is None:
            return None
        p = self._save_path_for(ROMS_DIR / self._current_rom_name)
        return p if p.exists() else None

    def upload_save(self, file_storage):
        rom_name = self._current_rom_name

        if rom_name is None:
            stem = Path(Path(file_storage.filename or "").name).stem
            if not stem or stem in (".", ".."):
                raise ValueError("invalid save filename")
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
        save_load_error = self.load_rom(rom_name)
        return rom_name, save_load_error

    def convert_sav(self, file_storage):
        rom_name = self._current_rom_name

        if rom_name is None:
            stem = Path(Path(file_storage.filename or "").name).stem
            if not stem or stem in (".", ".."):
                raise ValueError("invalid save filename")
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
                    f"Load the matching ROM first, or make sure the .sav file's "
                    f"name matches the ROM's filename."
                )

        from engine_config import get_engine_for_rom
        if get_engine_for_rom(rom_name) == "boytacean":
            raise ValueError(
                "Converting a .sav requires the pyboy engine - switch this "
                "ROM's engine to pyboy first (see the ROM library's engine "
                "selector), then try again."
            )

        sav_bytes = file_storage.read()
        ack = self._send_and_wait({
            "cmd": "load_rom_with_ram", "filename": rom_name, "ram_bytes": sav_bytes,
        })
        if not ack["ok"]:
            raise RuntimeError(ack.get("error") or "load_rom_with_ram failed in worker")
        self._worker_reports_running = True
        self._maybe_start_idle_controller_timer()
        self._maybe_start_no_input_timer()
        return rom_name

    def extract_sav(self):
        ack = self._send_and_wait({"cmd": "extract_sav"})
        if not ack["ok"]:
            raise ValueError(ack.get("error") or "extract_sav failed in worker")
        return ack["sav_bytes"]

    def delete_save(self):
        if self._current_rom_name is None:
            raise ValueError("no ROM is currently loaded")
        save_path = self._save_path_for(ROMS_DIR / self._current_rom_name)
        if save_path.exists():
            save_path.unlink()

    def press(self, button):
        if button not in BUTTON_NAMES:
            return

        self._controller_has_input = True
        self._cancel_no_input_timer()
        self.cmd_queue.put({"cmd": "press", "button": button})

    def release(self, button):
        if button not in BUTTON_NAMES:
            return
        self.cmd_queue.put({"cmd": "release", "button": button})

    def set_cheats(self, codes):
        self.cmd_queue.put({"cmd": "set_cheats", "codes": codes})

    def save_now(self):
        ack = self._send_and_wait({"cmd": "save_now"})
        if not ack["ok"]:
            raise ValueError(ack.get("error") or "save_now failed")

    def set_fast_forward(self, enabled):
        self.cmd_queue.put({"cmd": "set_fast_forward", "enabled": enabled})
        self.fast_forward = enabled

    def rtc_info(self):
        return self._send_and_wait({"cmd": "rtc_info"})

    def rtc_set(self, values):
        ack = self._send_and_wait({"cmd": "rtc_set", "values": values})
        if not ack["ok"]:
            raise ValueError(ack.get("error") or "rtc_set failed")
        return ack

    def _broadcast_stopped(self):
        self._broadcast("stopped")

    def _send(self, ws, payload, kind="control"):
        with self.clients_lock:
            stream = self._streams.get(ws)
        if stream is None:
            return False
        stream.send(payload, kind)
        return True

    def _broadcast(self, payload, kind="control"):
        with self.clients_lock:
            streams = list(self._streams.values())
        for stream in streams:
            stream.send(payload, kind)

    def _on_stream_dead(self, ws):
        with self.clients_lock:
            existed = self.clients.pop(ws, None) is not None
            stream = self._streams.pop(ws, None)
            self._client_remote_addrs.pop(ws, None)
        if stream is not None:
            stream.close()
        if existed:
            self._notify_controller_status()

    def add_client(self, ws, client_id=None, remote_addr=None):
        stream = _ClientStream(ws, self._on_stream_dead)
        with self.clients_lock:
            self.clients[ws] = client_id
            self._client_remote_addrs[ws] = remote_addr
            self._streams[ws] = stream
        self.touch()
        self._notify_controller_status()
        self._cancel_empty_grace_timer()
        self._maybe_start_idle_controller_timer()
        self._maybe_start_no_input_timer()

        if self._last_frame_compressed is not None:
            self._send(ws, MSG_VIDEO + self._last_frame_compressed, "video")

        for entry in self.chat_history:
            self._send(ws, "chatmsg:" + json.dumps(entry))
        self._send(ws, "fastforward:1" if self.fast_forward else "fastforward:0")

    def remove_client(self, ws):
        with self.clients_lock:
            was_controller = bool(self.clients) and next(iter(self.clients)) is ws
            self.clients.pop(ws, None)
            stream = self._streams.pop(ws, None)
            now_empty = len(self.clients) == 0
        if stream is not None:
            stream.close()
        self._chat_rate_limits.pop(ws, None)
        self._client_remote_addrs.pop(ws, None)
        self._debug_search.pop(ws, None)
        self._debug_rate.pop(ws, None)
        if self.pending_control_requester is ws:
            self.pending_control_requester = None
        if was_controller:

            for button in BUTTON_NAMES:
                self.release(button)
        self._notify_controller_status()
        if now_empty:
            self._schedule_empty_grace_timer()
            self._cancel_idle_controller_timer()
        else:
            self._maybe_start_idle_controller_timer()
            self._maybe_start_no_input_timer()

    def _schedule_empty_grace_timer(self):
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
        with self.clients_lock:
            still_empty = len(self.clients) == 0
        if still_empty:
            self.chat_history.clear()
            if self.auto_stop_when_empty and self._worker_reports_running:
                self.stop()
        self._auto_stop_timer = None

    def _maybe_start_idle_controller_timer(self):
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
        with self.clients_lock:
            self._idle_controller_timer = None
            if not self.clients or self._worker_reports_running:
                return
            if len(self.clients) < 2:
                return

            current_ws, current_id = next(iter(self.clients.items()))
            self.clients.pop(current_ws)
            self.clients[current_ws] = current_id
        self._notify_controller_status()
        self._maybe_start_idle_controller_timer()
        self._maybe_start_no_input_timer()

    def _maybe_start_no_input_timer(self):
        with self.clients_lock:
            current_controller_ws = next(iter(self.clients), None)
            has_other_clients = len(self.clients) >= 2

        if current_controller_ws is None:
            self._cancel_no_input_timer()
            self._no_input_tracked_ws = None
            return

        if current_controller_ws is not self._no_input_tracked_ws:

            self._no_input_tracked_ws = current_controller_ws
            self._controller_has_input = False
            self._cancel_no_input_timer()

        if self._controller_has_input or not has_other_clients:
            self._cancel_no_input_timer()
            return

        if self._no_input_timer is None:
            timer = threading.Timer(NO_INPUT_TIMEOUT_SECONDS, self._on_no_input_timeout)
            timer.daemon = True
            self._no_input_timer = timer
            timer.start()

    def _cancel_no_input_timer(self):
        if self._no_input_timer is not None:
            self._no_input_timer.cancel()
            self._no_input_timer = None

    def _on_no_input_timeout(self):
        with self.clients_lock:
            self._no_input_timer = None
            if not self.clients or self._controller_has_input:
                return
            if len(self.clients) < 2:
                return
            current_ws, current_id = next(iter(self.clients.items()))
            self.clients.pop(current_ws)
            self.clients[current_ws] = current_id
        self._notify_controller_status()
        self._no_input_tracked_ws = None
        self._maybe_start_idle_controller_timer()
        self._maybe_start_no_input_timer()

    def kick_client(self, client_id):
        with self.clients_lock:
            target_ws = None
            for ws, cid in self.clients.items():
                if cid == client_id:
                    target_ws = ws
                    break
        if target_ws is not None:

            with self.clients_lock:
                stream = self._streams.get(target_ws)
            if stream is not None:
                stream.close()
            try:
                target_ws.close(reason=KICK_CLOSE_CODE, message="Disconnected by an admin")
            except Exception:
                pass

            def _delayed_shutdown():
                time.sleep(0.5)
                try:
                    import socket as socket_module
                    target_ws.sock.shutdown(socket_module.SHUT_RDWR)
                except Exception:
                    pass
            threading.Thread(target=_delayed_shutdown, daemon=True).start()
            return True
        return False

    def close_all_clients(self, close_code=None, message="Server is going offline"):
        with self.clients_lock:
            targets = list(self.clients.keys())
            streams = [self._streams.get(ws) for ws in targets]
        for stream in streams:
            if stream is not None:
                stream.close()
        for ws in targets:
            try:
                ws.close(reason=close_code, message=message)
            except Exception:
                pass

        def _delayed_shutdown():
            time.sleep(0.5)
            try:
                import socket as socket_module
                for ws in targets:
                    try:
                        ws.sock.shutdown(socket_module.SHUT_RDWR)
                    except Exception:
                        pass
            except Exception:
                pass
        threading.Thread(target=_delayed_shutdown, daemon=True).start()
        return len(targets)

    def redirect_client(self, client_id, room_code):
        with self.clients_lock:
            target_ws = None
            for ws, cid in self.clients.items():
                if cid == client_id:
                    target_ws = ws
                    break
        if target_ws is None:
            return False
        return self._send(target_ws, f"redirect:{room_code}")

    def has_client(self, client_id):
        with self.clients_lock:
            return any(cid == client_id for cid in self.clients.values())

    def is_controller(self, ws):
        with self.clients_lock:
            if not self.clients:
                return False
            return next(iter(self.clients)) is ws

    def request_control(self, ws):
        with self.clients_lock:
            if not self.clients or next(iter(self.clients)) is ws:
                return
            controller_ws = next(iter(self.clients))
            self.pending_control_requester = ws
        self._send(controller_ws, "controlrequested:1")

    def grant_control(self, granter_ws):
        with self.clients_lock:
            if not self.clients or next(iter(self.clients)) is not granter_ws:
                return
            requester_ws = self.pending_control_requester
            if requester_ws is None or requester_ws not in self.clients:
                self.pending_control_requester = None
                return

            client_id = self.clients.pop(requester_ws)
            reordered = {requester_ws: client_id}
            reordered.update(self.clients)
            self.clients = reordered
            self.pending_control_requester = None
        self._notify_controller_status()
        self._maybe_start_idle_controller_timer()
        self._maybe_start_no_input_timer()

    def add_chat_message(self, ws, text, name=None):
        now = time.time()
        recent = self._chat_rate_limits.setdefault(ws, [])
        recent[:] = [t for t in recent if now - t < CHAT_RATE_WINDOW_SECONDS]
        if len(recent) >= CHAT_RATE_MAX_MESSAGES:
            return
        recent.append(now)

        text = text.strip()[:200]
        if not text:
            return
        name = (name or "").strip()[:24]
        role = "controller" if self.is_controller(ws) else "viewer"
        entry = {"role": role, "name": name, "text": text, "ts": time.time()}
        self.chat_history.append(entry)
        self._broadcast("chatmsg:" + json.dumps(entry))

    def controller_client_id(self):
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
        with self.clients_lock:
            clients = list(self.clients)
        controller = clients[0] if clients else None
        viewer_count = max(0, len(clients) - 1)
        if self._debug_owner is not None and self._debug_owner is not controller:
            # Whoever set breakpoints/freezes/pause lost control - never leave the
            # next controller (or the viewers) stuck in someone else's debug session.
            self._debug_owner = None
            try:
                self.cmd_queue.put({"cmd": "dbg_reset"})
            except Exception:
                pass
        for ws in clients:
            self._send(ws, "controller:1" if ws is controller else "controller:0")
            self._send(ws, f"viewers:{viewer_count}")

    def _notify_fast_forward_status(self):
        self._broadcast("fastforward:1" if self.fast_forward else "fastforward:0")


    # ---- hidden debugger -----------------------------------------------------

    DEBUG_READ_OPS = {"state", "read", "search_new", "search_filter", "search_results", "search_reset"}
    DEBUG_CONTROL_OPS = {
        "write", "set_register", "bp_add", "bp_remove", "bp_clear", "watch_add", "watch_remove",
        "freeze_set", "freeze_remove", "pause", "continue", "step_frame", "reset",
    }
    DEBUG_SEARCH_CONDS = ("eq", "ne", "gt", "lt", "changed", "unchanged", "increased", "decreased")
    DEBUG_SEARCH_RESULTS = 100
    DEBUG_RATE_PER_SECOND = 30
    DEBUG_SEARCH_MIN_INTERVAL = 0.2

    def debug_request(self, ws, raw):
        try:
            req = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"id": None, "ok": False, "error": "bad request"}
        if not isinstance(req, dict):
            return {"id": None, "ok": False, "error": "bad request"}
        req_id = req.get("id")
        if not isinstance(req_id, int) or isinstance(req_id, bool):
            req_id = None
        op = req.get("op")
        try:
            result = self._debug_dispatch(ws, op, req)
            return {"id": req_id, "ok": True, **result}
        except (ValueError, TimeoutError) as e:
            return {"id": req_id, "ok": False, "error": str(e)}
        except Exception as e:
            print(f"[warn] debugger op {op!r} failed: {e}")
            return {"id": req_id, "ok": False, "error": "internal error"}

    def _debug_rate_ok(self, ws, op):
        now = time.monotonic()
        rate = self._debug_rate.setdefault(ws, {"stamps": deque(), "last_search": 0.0})
        stamps = rate["stamps"]
        while stamps and now - stamps[0] > 1.0:
            stamps.popleft()
        if len(stamps) >= self.DEBUG_RATE_PER_SECOND:
            return False
        if op in ("search_new", "search_filter"):
            if now - rate["last_search"] < self.DEBUG_SEARCH_MIN_INTERVAL:
                return False
            rate["last_search"] = now
        stamps.append(now)
        return True

    def _debug_call(self, cmd, **kwargs):
        ack = self._send_and_wait(dict(kwargs, cmd="dbg_" + cmd))
        if not ack["ok"]:
            raise ValueError(ack.get("error") or "debugger command failed")
        return ack.get("result") or {}

    def _debug_dispatch(self, ws, op, req):
        if op not in self.DEBUG_READ_OPS and op not in self.DEBUG_CONTROL_OPS:
            raise ValueError("unknown operation")
        if not self._debug_rate_ok(ws, op):
            raise ValueError("slow down")
        self.touch()

        if op in self.DEBUG_CONTROL_OPS:
            if not self.is_controller(ws):
                raise ValueError("Only the current controller can change things - viewers can look around.")
            self._debug_owner = ws
            self._controller_has_input = True
            self._cancel_no_input_timer()

        if op == "state":
            return {"state": self._debug_call("state"), "is_controller": self.is_controller(ws)}

        if op == "read":
            r = self._debug_call("read", start=req.get("start"), length=req.get("length"))
            return {"start": r["start"], "hex": r["data"].hex()}

        if op.startswith("search_"):
            return self._debug_search_op(ws, op, req)

        passthrough = {
            "write": ("addr", "values"),
            "set_register": ("name", "value"),
            "bp_add": ("bank", "addr"),
            "bp_remove": ("bank", "addr"),
            "bp_clear": (),
            "watch_add": ("addr", "size", "cond", "value"),
            "watch_remove": ("id",),
            "freeze_set": ("addr", "value", "size"),
            "freeze_remove": ("addr",),
            "pause": (),
            "continue": (),
            "step_frame": (),
            "reset": (),
        }[op]
        result = self._debug_call(op, **{k: req.get(k) for k in passthrough if k in req})
        if op == "reset":
            self._debug_owner = None
        return {"result": result}

    def _debug_read_regions(self, regions):
        r = self._debug_call("read_regions", regions=regions)
        full = np.zeros(0x10001, dtype=np.int64)
        for info in r["regions"].values():
            start = info["start"]
            arr = np.frombuffer(info["data"], dtype=np.uint8)
            full[start:start + len(arr)] = arr
        return full

    def _debug_values_at(self, full, addrs, size):
        if size == 1:
            return full[addrs]
        return full[addrs] | (full[addrs + 1] << 8)

    def _debug_search_summary(self, st, full=None):
        addrs = st["addrs"][: self.DEBUG_SEARCH_RESULTS]
        if full is None:
            full = self._debug_read_regions(st["regions"])
        cur = self._debug_values_at(full, addrs, st["size"])
        prev = st["prev"][: self.DEBUG_SEARCH_RESULTS]
        return {
            "count": int(len(st["addrs"])),
            "size": st["size"],
            "regions": st["regions"],
            "steps": st["steps"],
            "results": [
                {"addr": int(a), "value": int(v), "prev": int(p)}
                for a, v, p in zip(addrs, cur, prev)
            ],
        }

    def _debug_search_op(self, ws, op, req):
        if op == "search_reset":
            self._debug_search.pop(ws, None)
            return {"count": 0, "results": []}

        if op == "search_new":
            regions = req.get("regions") or ["wram", "hram"]
            if (not isinstance(regions, list) or not regions
                    or any(r not in DEBUG_REGIONS for r in regions)):
                raise ValueError("pick at least one valid region")
            regions = sorted(set(regions), key=list(DEBUG_REGIONS).index)
            size = req.get("size", 1)
            if size not in (1, 2):
                raise ValueError("size must be 1 or 2")
            full = self._debug_read_regions(regions)
            parts = []
            for name in regions:
                lo, hi = DEBUG_REGIONS[name]
                parts.append(np.arange(lo, hi - (size - 1), dtype=np.int64))
            addrs = np.concatenate(parts)
            st = {"size": size, "regions": regions, "addrs": addrs,
                  "prev": self._debug_values_at(full, addrs, size), "steps": 0}
            self._debug_search[ws] = st
            return self._debug_search_summary(st, full)

        st = self._debug_search.get(ws)
        if st is None:
            raise ValueError("start a new search first")

        if op == "search_results":
            return self._debug_search_summary(st)

        cond = req.get("cond")
        if cond not in self.DEBUG_SEARCH_CONDS:
            raise ValueError("unknown condition")
        limit = 0xFF if st["size"] == 1 else 0xFFFF
        value = req.get("value")
        if cond in ("eq", "ne", "gt", "lt"):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
                raise ValueError(f"value must be 0-{limit}")
        full = self._debug_read_regions(st["regions"])
        cur = self._debug_values_at(full, st["addrs"], st["size"])
        prev = st["prev"]
        mask = {
            "eq": lambda: cur == value,
            "ne": lambda: cur != value,
            "gt": lambda: cur > value,
            "lt": lambda: cur < value,
            "changed": lambda: cur != prev,
            "unchanged": lambda: cur == prev,
            "increased": lambda: cur > prev,
            "decreased": lambda: cur < prev,
        }[cond]()
        st["addrs"] = st["addrs"][mask]
        st["prev"] = cur[mask]
        st["steps"] += 1
        return self._debug_search_summary(st, full)

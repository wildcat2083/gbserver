#!/usr/bin/env python3
"""
load_test_rooms.py - creates N real private rooms and genuinely loads a
ROM into each one, so their tick loops actually run - for watching real
CPU/memory load build up in htop.

This replaces an earlier bash+curl version that silently failed: /api/play
requires being recognized as the room's controller (see controller_check
in routes.py), which is determined by who's connected via WebSocket - a
plain curl POST with no prior WS connection just gets rejected. This
version connects via WebSocket first (which is what a real client always
does), then closes that connection immediately after - the room keeps
ticking afterward regardless, since only the shared game auto-stops when
empty; private rooms don't (a deliberate difference built earlier in this
project, so someone can step away from their own room briefly without it
stopping).

Run this ON cm-pi itself (hits localhost, bypassing nginx/network
entirely, so you're purely measuring the Pi's own capacity).

Usage: python3 load_test_rooms.py [number_of_rooms]
"""
import json
import sys
import time
import urllib.error
import urllib.request

try:
    import websocket  # pip install websocket-client --break-system-packages
except ImportError:
    print("Missing dependency - run this first:")
    print("  pip install websocket-client --break-system-packages")
    sys.exit(1)

BASE_URL = "http://127.0.0.1:8080"
WS_URL = "ws://127.0.0.1:8080"


def api_get(path):
    with urllib.request.urlopen(BASE_URL + path) as r:
        return json.loads(r.read())


def api_post(path, data=None, client_id=None):
    headers = {"Content-Type": "application/json"}
    if client_id:
        headers["X-Client-Id"] = client_id
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(data or {}).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def main():
    num_rooms = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    roms = api_get("/api/roms").get("roms", [])
    if not roms:
        print("No ROMs found in your library - upload at least one first.")
        return
    rom_name = roms[0]["filename"]
    print(f"Using ROM: {rom_name}")
    print(f"Creating {num_rooms} rooms, loading it into each...\n")

    for i in range(1, num_rooms + 1):
        client_id = f"loadtest-{i}"
        try:
            result = api_post("/api/rooms")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            print(f"[{i}] Room creation failed ({e.code}): {body}")
            print("Stopping here - if this is the room-limit message, that's itself the answer.")
            break
        room_code = result["room"]

        # Connect via WebSocket just long enough to register as this
        # room's controller (add_client() fires the moment the connection
        # is accepted server-side) - then close it. The room keeps ticking
        # afterward regardless of whether anyone's still connected.
        ws = websocket.create_connection(
            f"{WS_URL}/r/{room_code}/ws?client_id={client_id}", timeout=5
        )
        time.sleep(0.2)  # give the server a moment to actually register the client

        try:
            api_post(
                f"/r/{room_code}/api/play",
                {"filename": rom_name, "load_save": False},
                client_id=client_id,
            )
            print(f"[{i}] Room {room_code} created and playing")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            print(f"[{i}] Room {room_code} created, but failed to load ROM ({e.code}): {body}")
        finally:
            ws.close()

        time.sleep(0.5)  # stagger slightly so you can watch CPU climb in real time

    print()
    print("Done. Watch htop now - press the number keys or check per-core view specifically.")
    print("When finished testing, clear everything with:  sudo systemctl restart gbserver")


if __name__ == "__main__":
    main()

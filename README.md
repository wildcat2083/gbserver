# Headless Game Boy Server (Pi / PC edition)

Same idea as the ESP32 headless Game Boy project — a Game Boy emulator
that streams video over WiFi to any browser on your network — but running
as a normal Python program instead of on ESP32 firmware. No flashing, no
COM ports, no PSRAM limits.

## Setup (quick, for testing on your own machine)

```
pip install -r requirements.txt
python3 app.py
```

Then, from any device on the same network, open:

```
http://<this-machine's-LAN-IP>:8080/
```

(Find the IP with `ipconfig` on Windows, `ip addr` / `ifconfig` on Linux/Mac,
or `hostname -I` on a Raspberry Pi.)

## Production deployment (gunicorn + nginx)

This is the setup for running it as a persistent service — e.g. on a
Raspberry Pi or home server that's always on. Assumes a Debian/Ubuntu-like
system (adjust package manager commands if different).

**1. Copy the project and set up a virtualenv**

```
git clone <this repo, or just copy the folder> gbserver
cd gbserver
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

**2. Sanity check it runs under gunicorn directly**

```
./venv/bin/gunicorn -c gunicorn.conf.py app:app
```
Visit `http://<machine-ip>:8080/` — if that works, Ctrl+C and move on.

⚠️ **Do not raise `workers` above 1 in `gunicorn.conf.py`.** The running
emulator and connected WebSocket clients are in-process state; multiple
worker processes would each get their own empty, disconnected copy. See
the comment at the top of `gunicorn.conf.py` for the full explanation.
Concurrency across many simultaneous viewers is handled by the `gthread`
worker class within that single process instead - not `gevent`, which
was tried first and dropped after a real production crash
(`ValueError: semaphore or lock released too many times`, caused by
gevent's monkey-patching corrupting threading internals when multiple
`multiprocessing.Queue` objects and threads coexist).

**3. Install the systemd service**

Edit `deploy/systemd/gbserver.service` first — set `User`, `WorkingDirectory`,
and `ExecStart` to match your actual username and install path (it assumes
`pi` / `/home/pi/gbserver` as a placeholder).

```
sudo cp deploy/systemd/gbserver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gbserver
sudo systemctl status gbserver     # should show "active (running)"
journalctl -u gbserver -f          # tail logs
```

At this point the app is running on `127.0.0.1:8080`, but only reachable
from the machine itself — that's intentional, nginx is what exposes it.

**4. Install and configure nginx**

This config serves two hostnames with two different certificates via
SNI (see the comment at the top of `deploy/nginx/gbserver.conf`) - an
internet-facing one with a real Let's Encrypt cert, and a LAN-only one
with an internal CA cert. Adjust the hostnames and certificate paths in
`gbserver.conf` to match your own setup before installing it - the ones
committed here are specific to this deployment's actual domains.

```
sudo apt install nginx
sudo mkdir -p /etc/nginx/snippets
sudo cp deploy/nginx/gbserver.conf /etc/nginx/sites-available/gbserver.conf
sudo cp deploy/nginx/snippets/gbserver-locations.conf /etc/nginx/snippets/
sudo ln -s /etc/nginx/sites-available/gbserver.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default   # optional, avoids the default page colliding
sudo nginx -t                                  # check config syntax
sudo systemctl restart nginx
```

Both certificates need to already exist at the paths `gbserver.conf`
references before this will work - issuing a Let's Encrypt cert
(`certbot`) and/or an internal CA cert is a separate step this doesn't
cover. Let's Encrypt certs expire every 90 days - confirm `certbot`'s
own renewal timer is actually installed and enabled
(`systemctl list-timers | grep certbot`), since nothing else here
handles that automatically.

Now open `http://<machine-ip>/` (port 80, no `:8080` needed) from any
device on your network.

**5. (Optional) HTTPS**

Not required on a LAN, but if you want it — e.g. to access this from
outside your home network via a reverse proxy/VPN — use `certbot` for a
real domain, or a self-signed cert for LAN-only HTTPS. Either way, once
TLS terminates at nginx, no changes are needed in `gbserver.conf` besides
the usual `listen 443 ssl` block; the WebSocket `proxy_set_header Upgrade`
lines stay the same.

### Updating the app later

```
cd gbserver
git pull   # or copy over your changed files
sudo systemctl restart gbserver
```

nginx doesn't need restarting for app code changes — only for changes to
`gbserver.conf` itself.

## Using it

1. Open the settings gear (top right) and upload a `.gb` or `.gbc` file
   (use only legally-obtained ROMs).
2. Tap "Play" next to the ROM you want in the ROM library list.
3. Use the on-screen D-pad/A/B or your keyboard (arrow keys, Z/X, Enter, Shift)
   to play.

The settings panel also has:
- **Button haptics** — short vibrate on press (Android / supported browsers)
- **Audio buffer** — how many emulator ticks get batched into one audio
  chunk before sending (lower = snappier but more prone to clicking, higher
  = smoother but a bit more lag); adjustable live
- **Save data** — download the current ROM's save state, upload one, or
  delete it (deleting/uploading reloads the ROM immediately to apply it)
- **Stop emulation** — stops the running ROM; the library and saves are
  untouched, just pick a ROM again to resume

Save states also autosave automatically to `saves/` every ~5 minutes and
restore the next time you load that ROM.

## Notes

- Runs on anything with Python 3: a Raspberry Pi, an old laptop, a
  spare mini PC, or a container on your home server.
- Audio is streamed live over the same WebSocket as video (24kHz stereo
  PCM). Browsers block audio until a user gesture, so tap/click the
  screen once after loading a ROM to unlock sound.
- Only one active game is emulated at a time, but any number of devices
  on the network can connect and watch/play the same session simultaneously
  (unlike the ESP32 version's single-client limit). Note that with audio on,
  every connected client gets its own independently-scheduled audio stream,
  so playback across multiple devices won't be perfectly in sync with each
  other (video and audio stay in sync on a given device).

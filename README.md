# Headless Game Boy Server (Pi / PC edition)

Same idea as the ESP32 headless Game Boy project - a Game Boy emulator
that streams video and audio to any browser - but running as a normal
Python program instead of on ESP32 firmware. No flashing, no COM ports,
no PSRAM limits.

## Setup (quick, for testing on your own machine)

```
pip install -r requirements.txt
python3 app.py
```

Then, from any device on the same network, open:

```
http://<this-machine's-LAN-IP>:8080/
```

(Find the IP with `ipconfig` on Windows, `ip addr` on Linux/Mac, or
`hostname -I` on a Raspberry Pi.)

## Production deployment (gunicorn + nginx)

**Automated:** copy `gbserver.zip` and `deploy/deploy.sh` to the Pi's home
folder and run `bash deploy.sh` as the service user. It backs up, installs
everything below, runs security checks, and rolls back on any failure.
Re-running it is safe. The manual steps follow for reference.


This is the setup for running it as a persistent service on a Raspberry
Pi or home server. Assumes a Debian-like system.

**1. Copy the project and set up a virtualenv**

```
git clone <this repo> gbserver
cd gbserver
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

**2. Sanity check it runs under gunicorn directly**

```
./venv/bin/gunicorn -c gunicorn.conf.py app:app
```

gunicorn binds to `127.0.0.1:8080`, so test from the machine itself
(`curl -I http://127.0.0.1:8080/`), then Ctrl+C.

⚠️ **Do not raise `workers` above 1 in `gunicorn.conf.py`.** The running
emulators and connected WebSocket clients are in-process state; multiple
worker processes would each get their own empty, disconnected copy.
Concurrency across many viewers is handled by the `gthread` worker class
within that single process instead - not `gevent`, which was tried first
and dropped after a production crash (`ValueError: semaphore or lock
released too many times`, caused by gevent's monkey-patching corrupting
threading internals when multiple `multiprocessing.Queue` objects and
threads coexist).

**3. Create the secrets file**

The admin token is never stored in the repo. Generate one and put it in
`/etc/gbserver.env`:

```
sudo install -m 600 -o root -g root deploy/systemd/gbserver.env.example /etc/gbserver.env
openssl rand -hex 32          # copy this
sudo nano /etc/gbserver.env   # paste after GBSERVER_ADMIN_TOKEN=
```

If the token is missing, a placeholder like `changeme`, or shorter than
32 characters, the app logs a warning and the admin API stays disabled.

**4. Install the systemd service**

Edit `deploy/systemd/gbserver.service` first if your user or install path
isn't `luna` / `/home/luna/gbserver`.

```
sudo cp deploy/systemd/gbserver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gbserver
sudo systemctl status gbserver
journalctl -u gbserver -f
```

**5. (Optional) Allow the dashboard's certificate-expiry panel**

The dashboard checks cert expiry with `sudo -n openssl`. The included
sudoers file allows exactly those two commands and nothing else:

```
sudo visudo -cf deploy/sudoers/gbserver-certcheck
sudo install -m 440 -o root -g root deploy/sudoers/gbserver-certcheck /etc/sudoers.d/gbserver-certcheck
```

Edit the username and cert paths in it first if yours differ.

**6. Install and configure nginx**

The nginx config serves two hostnames with two certificates via SNI:

- `gbserver.wulfpax-labs.com` - internet-facing, served through a Cloudflare
  Tunnel (see below). The admin dashboard and admin API return 404, and
  adding or deleting ROMs returns 403.
- `gbserver-internal.wulfpax-labs.com` - LAN-only, internal CA cert,
  everything available.

Adjust hostnames and certificate paths in `deploy_gbserver.conf` to match
your setup before installing.

```
sudo apt install nginx
sudo mkdir -p /etc/nginx/snippets
sudo cp deploy/nginx/deploy_gbserver.conf /etc/nginx/sites-available/gbserver.conf
sudo cp deploy/nginx/deploy_gbserver-locations.conf /etc/nginx/snippets/gbserver-locations.conf
sudo ln -sf /etc/nginx/sites-available/gbserver.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default   # optional
sudo nginx -t
sudo systemctl reload nginx
```

The internal CA certificate must already exist at the path the config
references. The public hostname needs no certificate on the Pi - Cloudflare
handles TLS for visitors.

### Public access without port forwarding (Cloudflare Tunnel)

`gbserver.wulfpax-labs.com` is published through a Cloudflare Tunnel:
`cloudflared` on the Pi keeps an outbound connection to Cloudflare, so the
router needs no port forwarding at all. Visitors reach Cloudflare, which
hands requests to nginx's loopback-only public block on `127.0.0.1:8081`.
That block applies the public restrictions and restores each visitor's real
IP from `CF-Connecting-IP`. `gbserver-internal` is unchanged and LAN-only.

```
# install cloudflared
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared

# create the tunnel (login prints a URL - open it and pick wulfpax-labs.com)
cloudflared tunnel login
cloudflared tunnel create gbserver          # note the tunnel ID it prints

# configure and install it as a service
TUNNEL_ID=<the ID>
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/$TUNNEL_ID.json /etc/cloudflared/
sed "s/TUNNEL_ID/$TUNNEL_ID/g" deploy/cloudflared/config.yml | sudo tee /etc/cloudflared/config.yml >/dev/null
sudo cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
cloudflared tunnel route dns --overwrite-dns gbserver gbserver.wulfpax-labs.com
sudo cloudflared service install
```

Then remove the port forwards from the router. Hide the no-longer-used
public certificate from the dashboard's certificate panel by adding
`GBSERVER_PUBLIC_CERT_PATH=` to `/etc/gbserver.env`.

### Updating the app later

```
cd gbserver
git pull
sudo systemctl restart gbserver
```

nginx only needs `sudo nginx -t && sudo systemctl reload nginx` when the
files under `deploy/nginx/` change (re-copy them first).

## Using it

1. Open the settings gear and upload a `.gb` or `.gbc` file (use only
   legally-obtained ROMs). Uploads are LAN-only when served through the
   nginx config above. Uploading a name that already exists is refused -
   delete the old one first.
2. Tap "Play" next to the ROM you want.
3. Use the on-screen D-pad/A/B, a gamepad, or the keyboard (arrow keys,
   Z/X, Enter, Shift).

The settings panel also has button haptics, an audio buffer slider,
save-state download/upload/delete, `.sav` conversion, GameShark cheats,
fast-forward, and stop emulation.

Save states autosave to `saves/` every few minutes (see
`AUTOSAVE_INTERVAL_MINUTES` in `config.py`) and restore the next time you
load that ROM.

### Rooms, chat, and control

- The shared game at `/` can be watched and played by everyone; the
  first connected client is the controller, and others can request
  control.
- Private rooms live at `/r/<code>/`, with their own emulator and saves.
  Idle rooms are reaped after 30 minutes. `GBSERVER_MAX_ROOMS` caps how
  many exist at once.
- Each session has a small rate-limited chat.

### Admin dashboard (LAN only)

`/dashboard` on the internal hostname, authenticated with the admin
token. It shows sessions and connected clients, and can kick or move
clients, block IPs, toggle the shared game, take the server offline,
manage the ROM library, and show certificate expiry.

### Hidden debugger

There's a BGB-style debugger tucked away as an easter egg: memory viewer and
editor, memory search, execution breakpoints, value watches, freezes, and
CPU registers. See `docs/DEBUGGER.md` for how to open and use it.

Anyone connected can look; only the current controller can change memory,
set breakpoints, or pause. Everything resets when control changes hands.
Set `GBSERVER_DEBUGGER=internal` in `/etc/gbserver.env` to limit it to the
LAN hostname, or `off` to disable it.

## Where files live

- `roms/` holds only ROMs (`.gb`, `.gbc`). The server never writes anything here.
- `symbols/` (optional) holds `.sym` files for the debugger, named after the
  ROM; `GBSERVER_SYMBOLS_DIR` can point elsewhere.
- `saves/` holds everything the server writes: save states (`<rom>.state`),
  per-ROM engine choices (`_engine_overrides.json`), and private rooms'
  saves under `saves/rooms/<code>/`.
- On startup, anything older versions (or PyBoy itself) left in `roms/` is
  moved out automatically: save states go to `saves/`, and battery `.ram`,
  `.rtc` and `.sav` files go to `saves/_from_roms/`. Nothing is overwritten -
  if `saves/` already has a state for that ROM, the old one goes to
  `saves/_from_roms/` too. The log lists each file it moved.

## Notes

- Runs on anything with Python 3.
- Audio streams over the same WebSocket as video as PCM (sample rate is
  `SOUND_SAMPLE_RATE` in `config.py`). Browsers block audio until a user
  gesture, so tap the screen once after loading a ROM.
- Each connected client gets its own independently scheduled audio
  stream, so playback across multiple devices won't be perfectly in sync.
- Runtime state (`blocked_ips.json`, `offline.flag`, ROMs, saves) is
  gitignored and lives only on the server.

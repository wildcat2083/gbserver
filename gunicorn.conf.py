"""
Gunicorn config for the headless Game Boy server.

IMPORTANT: workers MUST stay at 1.

The emulator (the running PyBoy instance, the set of connected WebSocket
clients) lives as in-process global state in app.py. Gunicorn workers are
separate OS processes that do not share memory, so if you raise `workers`
above 1, you'll end up with multiple independent, empty emulators, and
clients will randomly connect to whichever worker process handled their
request — most of them will see nothing running.

Concurrency (many phones/browsers connected at once) is handled WITHIN
that single worker via the gthread worker class, which uses real OS
threads rather than gevent's cooperative greenlets. This used to be
"gevent" - switched after gevent's monkey-patching was confirmed (via a
real production crash, then reproduced and root-caused) to corrupt
threading's internal bookkeeping once enough concurrent threads are in
play, breaking both multiprocessing.Queue (used by the per-room worker
subprocess architecture - see emulator.py/emu_worker.py) and plain
threading.Timer (used for the empty-room grace period) with the same
underlying symptom. flask-sock/simple-websocket has no gevent dependency
either way - it extracts the raw socket directly via gunicorn's own WSGI
environ regardless of worker class, so this switch doesn't affect
WebSocket handling at all. Raise `threads` if you expect a lot of
simultaneous viewers; you do not need more workers for that.

Run with:
    gunicorn -c gunicorn.conf.py app:app
"""

bind = "127.0.0.1:8080"      # nginx proxies to this; not exposed directly
workers = 1                  # see note above — do not change
worker_class = "gthread"
threads = 100                 # max simultaneous client connections (analogous to
                               # gevent's worker_connections, but real OS threads)
timeout = 120                # generous, since some connections are long-lived
graceful_timeout = 10
keepalive = 5

accesslog = "-"               # stdout; let systemd/journald capture it
errorlog = "-"
loglevel = "info"

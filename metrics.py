import time

import psutil

from config import DATA_DIR

_PROCESS_START = time.time()

_last_cpu = {"value": None, "ts": 0.0}


def _cpu_percent():
    now = time.monotonic()
    if _last_cpu["value"] is not None and now - _last_cpu["ts"] < 1.0:
        return _last_cpu["value"]
    try:
        value = psutil.cpu_percent(interval=0.1)
    except Exception:
        return None
    _last_cpu["value"] = value
    _last_cpu["ts"] = time.monotonic()
    return value


def system_metrics():
    base = {
        "available": True,
        "uptime_seconds": round(time.time() - _PROCESS_START, 1),
        "python": "%d.%d.%d" % (__import__("sys").version_info[:3]),
    }
    try:
        mem = psutil.virtual_memory()
        base["memory_percent"] = round(mem.percent, 1)
        base["memory_used_bytes"] = mem.used
        base["memory_total_bytes"] = mem.total
    except Exception:
        pass
    try:
        swap = psutil.swap_memory()
        base["swap_percent"] = round(swap.percent, 1)
    except Exception:
        pass
    try:
        disk = psutil.disk_usage(str(DATA_DIR))
        base["disk_percent"] = round(disk.percent, 1)
        base["disk_used_bytes"] = disk.used
        base["disk_total_bytes"] = disk.total
        base["disk_free_bytes"] = disk.free
    except Exception:
        pass
    cpu = _cpu_percent()
    if cpu is not None:
        base["cpu_percent"] = round(cpu, 1)
    try:
        base["cpu_count"] = psutil.cpu_count(logical=True)
    except Exception:
        pass
    try:
        base["load_avg"] = [round(x, 2) for x in os_loadavg()]
    except Exception:
        base["load_avg"] = None
    return base


def os_loadavg():
    import os
    if hasattr(os, "getloadavg"):
        return os.getloadavg()
    return ()


def gunicorn_workers():
    try:
        from multiprocessing import active_children
        return [p.pid for p in active_children()]
    except Exception:
        return []
"""
Restart bookkeeping for the wake-word listener. Deliberately tiny (no numpy/pyaudio imports) so
server.py can read it for /status while wake_word.py writes it from the listening thread.
"""

import threading
import time

WINDOW_SECONDS = 60.0     # restarts are counted over this long...
UNSTABLE_RESTARTS = 3     # ...and this many of them (or more) mean "wake word unstable"

_lock = threading.Lock()
_restarts = []            # time.monotonic() of each restart, newest last


def _trim(now):
    while _restarts and now - _restarts[0] > WINDOW_SECONDS:
        _restarts.pop(0)


def record_restart():
    with _lock:
        now = time.monotonic()
        _restarts.append(now)
        _trim(now)


def snapshot():
    """{"restarts_last_minute": n, "unstable": bool} - for /status."""
    with _lock:
        _trim(time.monotonic())
        n = len(_restarts)
    return {"restarts_last_minute": n, "unstable": n >= UNSTABLE_RESTARTS}


def reset():
    with _lock:
        _restarts.clear()

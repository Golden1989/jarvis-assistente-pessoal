"""
A rotating log file for the desktop app. Under pythonw (the autostart) nobody sees the console, so
everything the app prints - the [wake_word] and [desktop] lines above all - is also written to
logs/jarvis.log (1 MB per file, 3 old files kept; the folder is in .gitignore).

setup() wraps sys.stdout and sys.stderr in a tee: the original stream still gets everything (when
there is one), and a failure to write the log can never break a print. Werkzeug's per-request
access lines (stderr) are left out - /status is polled all day and would drown the rest.
Note the file holds what was heard and answered ("[desktop] heard/replied"): it stays on this machine.
"""

import logging
import logging.handlers
import re
import sys
import threading
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "jarvis.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

_ACCESS_LINE = re.compile(r'^\S+ - - \[[^\]]+\] "[A-Z]+ ')     # 127.0.0.1 - - [date] "GET /status ..."
_logger = None


class _Tee:
    def __init__(self, original, logger, skip=None):
        self._original, self._logger, self._skip = original, logger, skip
        self._buffer, self._lock = "", threading.Lock()

    def write(self, text):
        try:
            written = self._original.write(text) if self._original is not None else len(text)
        except Exception:
            written = len(text)                      # a dead console must not break printing
        try:
            with self._lock:
                self._buffer += text
                *lines, self._buffer = self._buffer.split("\n")
            for line in lines:
                line = line.rstrip()
                if line and not (self._skip and self._skip.match(line)):
                    self._logger.info(line)
        except Exception:
            pass
        return written

    def flush(self):
        try:
            if self._original is not None:
                self._original.flush()
        except Exception:
            pass

    def __getattr__(self, name):                     # reconfigure, encoding, isatty, ...
        if self._original is None:
            raise AttributeError(name)
        return getattr(self._original, name)


def setup(path=LOG_FILE, max_bytes=MAX_BYTES, backup_count=BACKUP_COUNT):
    """Start logging to `path` (once). Returns the path, or None if the file could not be opened."""
    global _logger
    if _logger is not None:
        return path
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    except Exception:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    _logger = logging.getLogger("jarvis.app")
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    _logger.addHandler(handler)
    sys.stdout = _Tee(sys.stdout, _logger)
    sys.stderr = _Tee(sys.stderr, _logger, skip=_ACCESS_LINE)
    return path

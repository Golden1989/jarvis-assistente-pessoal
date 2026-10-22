"""
Today's cached "good morning" briefing (jarvis_core.py's run_briefing/get_or_run_briefing) -
mirrors daily_spend.py's DailySpend: lazy loading, atomic write, a thread lock, injectable
path/clock, tolerant of a corrupt file, and the same JARVIS_TESTING real-file refusal. See
daily_spend.py for the reasoning behind each of those - it is not repeated here.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = str(_BASE_DIR / "briefing_cache.json")
_DEFAULT_CACHE_FILE = CACHE_FILE


def _today():
    return datetime.now().date().isoformat()


class BriefingCache:
    """Today's cached briefing: {"date", "text", "spoken"}. A new local date (or nothing cached
    yet) makes get() return (None, None), so the caller fetches a fresh one."""

    def __init__(self, path=None, clock=_today):
        self._path = Path(path) if path is not None else Path(CACHE_FILE)
        self._clock = clock
        self._lock = threading.Lock()
        self._loaded = False
        self._date = None
        self._text = None
        self._spoken = None

    def _refuse_if_untested_real_file(self):
        if os.environ.get("JARVIS_TESTING") and str(self._path) == _DEFAULT_CACHE_FILE:
            raise RuntimeError(
                "JARVIS_TESTING is set and this BriefingCache still points at the real "
                "briefing_cache.json - a test must construct it with its own temp `path` first."
            )

    def _ensure_loaded(self):
        """Call with self._lock already held."""
        if self._loaded:
            return
        self._refuse_if_untested_real_file()
        self._load_from_disk()
        self._loaded = True

    def _load_from_disk(self):
        data = None
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                print(f"[briefing] could not read {self._path.name} ({error}) - starting fresh", flush=True)
                data = None
        today = self._clock()
        if (isinstance(data, dict) and data.get("date") == today
                and isinstance(data.get("text"), str) and isinstance(data.get("spoken"), str)):
            self._date, self._text, self._spoken = today, data["text"], data["spoken"]
        else:
            self._date, self._text, self._spoken = today, None, None

    def _save(self):
        """Call with self._lock already held. Atomic (write + os.replace), same as
        daily_spend.py's _save() - and the same tolerance: a failed write is logged and otherwise
        ignored, so a disk problem never stops today's briefing from being served from memory."""
        payload = {"date": self._date, "text": self._text, "spoken": self._spoken}
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as error:
            print(f"[briefing] could not save {self._path.name} ({error}) - keeping today's "
                  "briefing in memory only", flush=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    def _roll_if_new_day(self):
        """Call with self._lock already held, after _ensure_loaded()."""
        today = self._clock()
        if today != self._date:
            self._date, self._text, self._spoken = today, None, None

    def get(self):
        """(text, spoken) for today if already cached, else (None, None)."""
        with self._lock:
            self._ensure_loaded()
            self._roll_if_new_day()
            return self._text, self._spoken

    def set(self, text, spoken):
        with self._lock:
            self._ensure_loaded()
            self._roll_if_new_day()
            self._text, self._spoken = text, spoken
            self._save()

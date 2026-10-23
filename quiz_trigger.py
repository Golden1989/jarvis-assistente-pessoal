"""
Persisted state for the 16:00 daily study-quiz trigger (jarvis_core.py's should_signal_quiz() /
quiz_due_status() / snooze_quiz() / skip_quiz_today()).

Same hardening as daily_spend.py: lazy load, a thread lock, atomic write, an injectable path, and
the JARVIS_TESTING refusal of the real file - see that module for the reasoning, not repeated
here. One difference: there is no injectable CLOCK here, unlike daily_spend.py/briefing_cache.py -
this store holds no "today" field of its own to roll over; it just remembers three timestamps/
dates verbatim, and jarvis_core.py's own functions (which DO take an injectable `now`) are the
ones that compare them against the current time. Losing this file costs nothing - worst case she
is offered the quiz once more than ideal - so a corrupt file is warned about and reset, the same
tolerance daily_spend.py uses, not study_store.py's rename-aside (which is for her own notes).
"""
import json
import os
import threading
from datetime import date, datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent
TRIGGER_FILE = str(_BASE_DIR / "quiz_trigger.json")
_DEFAULT_TRIGGER_FILE = TRIGGER_FILE


def _is_naive_datetime_str(value):
    """True only for a str datetime.fromisoformat() parses AND that has no tzinfo. Everything
    this store's fields get compared against (jarvis_core.py's datetime.now()) is naive local
    time - a tz-aware string would silently compare wrong (or raise) wherever it meets one."""
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is None


def _is_date_str(value):
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


class QuizTriggerStore:
    """
    {"snoozed_until": iso-datetime|None, "skipped_date": "YYYY-MM-DD"|None,
     "last_signaled_at": iso-datetime|None}.

    `path` is injectable: a test passes a temp path instead of ever touching the real file.
    Loading is lazy - constructing one does no disk I/O (see jarvis_core.py's module-level
    singleton), so it never trips the JARVIS_TESTING guard just by existing.
    """

    def __init__(self, path=None):
        self._path = Path(path) if path is not None else Path(TRIGGER_FILE)
        self._lock = threading.Lock()
        self._loaded = False
        self.snoozed_until = None
        self.skipped_date = None
        self.last_signaled_at = None

    def _refuse_if_untested_real_file(self):
        if os.environ.get("JARVIS_TESTING") and str(self._path) == _DEFAULT_TRIGGER_FILE:
            raise RuntimeError(
                "JARVIS_TESTING is set and this QuizTriggerStore still points at the real "
                "quiz_trigger.json - a test must construct it with its own temp `path` first."
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
                if not isinstance(data, dict):
                    raise ValueError("quiz_trigger.json is not an object")
            except (OSError, ValueError) as error:
                print(f"[quiz_trigger] could not read {self._path.name} ({error}) - starting fresh",
                      flush=True)
                data = None
        if isinstance(data, dict):
            # Validated here, once, so every other function in this file (and in jarvis_core.py)
            # can trust these three fields without re-checking: an invalid value becomes None -
            # the same as "nothing recorded" - rather than ever being compared or parsed again.
            self.snoozed_until = self._validated(data.get("snoozed_until"), "snoozed_until",
                                                 _is_naive_datetime_str)
            self.skipped_date = self._validated(data.get("skipped_date"), "skipped_date", _is_date_str)
            self.last_signaled_at = self._validated(data.get("last_signaled_at"), "last_signaled_at",
                                                     _is_naive_datetime_str)
        else:
            self.snoozed_until = None
            self.skipped_date = None
            self.last_signaled_at = None

    @staticmethod
    def _validated(value, field_name, checker):
        """value if `checker(value)` passes, else None - with a one-line warning naming the
        FIELD only, never the value (it came from a file on disk, not something to echo back)."""
        if value is None or checker(value):
            return value
        print(f"[quiz_trigger] invalid {field_name} in quiz_trigger.json - ignoring it", flush=True)
        return None

    def _save(self):
        """Call with self._lock already held. Atomic write, tolerant of a failed save - same as
        daily_spend.py/briefing_cache.py."""
        payload = {"snoozed_until": self.snoozed_until, "skipped_date": self.skipped_date,
                   "last_signaled_at": self.last_signaled_at}
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as error:
            print(f"[quiz_trigger] could not save {self._path.name} ({error}) - keeping this "
                  "session's state in memory only", flush=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    def snapshot(self):
        """{"snoozed_until", "skipped_date", "last_signaled_at"}."""
        with self._lock:
            self._ensure_loaded()
            return {"snoozed_until": self.snoozed_until, "skipped_date": self.skipped_date,
                    "last_signaled_at": self.last_signaled_at}

    def mark_snoozed(self, until_iso):
        with self._lock:
            self._ensure_loaded()
            self.snoozed_until = until_iso
            self._save()

    def mark_skipped(self, today_iso):
        with self._lock:
            self._ensure_loaded()
            self.skipped_date = today_iso
            self._save()

    def mark_signaled(self, now_iso):
        with self._lock:
            self._ensure_loaded()
            self.last_signaled_at = now_iso
            self._save()

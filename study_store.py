"""
Persisted study material for Jarvis's quiz engine (jarvis_core.py's quiz flow) - her own summaries
and topic names, by subject, used later to generate quiz questions.

Same hardening as daily_spend.py / briefing_cache.py: atomic write, a thread lock, injectable
path/clock, and the JARVIS_TESTING refusal of the real file - see those modules for the reasoning
behind each, not repeated here. One difference: entries never expire or reset by date (there is
no "today" rollover here, unlike the daily cap or the cached briefing), and a corrupt file is
renamed aside rather than silently discarded, since this holds her own notes, not just a number.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent
STORE_FILE = str(_BASE_DIR / "study_data.json")
_DEFAULT_STORE_FILE = STORE_FILE

MAX_TEXT_CHARS = 6000   # per entry
MAX_ENTRIES = 200       # total


def _today():
    return datetime.now().date().isoformat()


class TooLongError(ValueError):
    """The text for a new entry is over MAX_TEXT_CHARS - nothing was saved."""


class StoreFullError(ValueError):
    """Already at MAX_ENTRIES - nothing was saved."""


class StudyStore:
    """
    A persisted list of study entries ({id, subject, kind, text, added_date, times_asked,
    last_asked_date, last_result}), saved to study_data.json.

    `path` and `clock` are injectable: a test passes a temp path and a fake clock (a callable
    returning a date string) instead of ever touching the real file or the real system clock.
    Loading is lazy - constructing one does no disk I/O, so the real app's module-level instance
    never trips the JARVIS_TESTING guard just by existing (see jarvis_core.py's _study_store).
    """

    def __init__(self, path=None, clock=_today):
        self._path = Path(path) if path is not None else Path(STORE_FILE)
        self._clock = clock
        self._lock = threading.Lock()
        self._loaded = False
        self._entries = []
        self._next_id = 1

    def _refuse_if_untested_real_file(self):
        if os.environ.get("JARVIS_TESTING") and str(self._path) == _DEFAULT_STORE_FILE:
            raise RuntimeError(
                "JARVIS_TESTING is set and this StudyStore still points at the real "
                "study_data.json - a test must construct it with its own temp `path` first."
            )

    def _ensure_loaded(self):
        """Call with self._lock already held."""
        if self._loaded:
            return
        self._refuse_if_untested_real_file()
        self._load_from_disk()
        self._loaded = True

    def _load_from_disk(self):
        if not self._path.exists():
            self._entries, self._next_id = [], 1
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("study_data.json is not a list")
            for entry in data:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
                    raise ValueError("a study entry is malformed")
        except (OSError, ValueError) as error:
            # Never silently overwritten - these are her own notes. Rename the bad file aside so
            # nothing is lost, start empty, and say so loudly.
            corrupt_path = self._path.with_name(self._path.name + ".corrupt")
            try:
                os.replace(self._path, corrupt_path)
                print(f"[study] {self._path.name} was corrupt ({error}) - renamed to "
                      f"{corrupt_path.name} and starting empty", flush=True)
            except OSError as rename_error:
                print(f"[study] {self._path.name} was corrupt ({error}) and could not be renamed "
                      f"aside ({rename_error}) - starting empty anyway; the file on disk is left "
                      "untouched", flush=True)
            self._entries, self._next_id = [], 1
            return
        self._entries = data
        self._next_id = max((e["id"] for e in data), default=0) + 1

    def _save(self):
        """Call with self._lock already held. Atomic write, tolerant of a failed save - same as
        daily_spend.py/briefing_cache.py."""
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._entries), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as error:
            print(f"[study] could not save {self._path.name} ({error}) - keeping this session's "
                  "entries in memory only", flush=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    def add(self, subject, kind, text):
        """Appends one entry and returns it (a copy). Raises TooLongError/StoreFullError instead
        of ever partially saving - the caller turns those into a fixed reply."""
        with self._lock:
            self._ensure_loaded()
            if len(text) > MAX_TEXT_CHARS:
                raise TooLongError(f"text is longer than {MAX_TEXT_CHARS} characters")
            if len(self._entries) >= MAX_ENTRIES:
                raise StoreFullError(f"already at the {MAX_ENTRIES}-entry cap")
            entry = {
                "id": self._next_id, "subject": subject, "kind": kind, "text": text,
                "added_date": self._clock(), "times_asked": 0,
                "last_asked_date": None, "last_result": None,
            }
            self._entries.append(entry)
            self._next_id += 1
            self._save()
            return dict(entry)

    def undo_last(self):
        """Removes the most recently ADDED entry (highest id), returns a copy of it or None."""
        with self._lock:
            self._ensure_loaded()
            if not self._entries:
                return None
            newest = max(self._entries, key=lambda e: e["id"])
            self._entries = [e for e in self._entries if e is not newest]
            self._save()
            return dict(newest)

    def all(self):
        """A list of copies - the caller can freely inspect/sort without risking the store's own
        state (see select_quiz_entries in jarvis_core.py)."""
        with self._lock:
            self._ensure_loaded()
            return [dict(e) for e in self._entries]

    def counts_by_subject(self):
        """{subject: (count, most_recent_added_date)} - never the contents."""
        with self._lock:
            self._ensure_loaded()
            out = {}
            for e in self._entries:
                count, last = out.get(e["subject"], (0, None))
                count += 1
                if last is None or e["added_date"] > last:
                    last = e["added_date"]
                out[e["subject"]] = (count, last)
            return out

    def update_after_quiz(self, entry_id, result, asked_date):
        with self._lock:
            self._ensure_loaded()
            for e in self._entries:
                if e["id"] == entry_id:
                    e["times_asked"] += 1
                    e["last_asked_date"] = asked_date
                    e["last_result"] = result
                    break
            self._save()

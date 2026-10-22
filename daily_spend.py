"""
Daily spending cap for the Claude API: a persisted running total of today's cost, independent of
ModeState's session cost (jarvis_core.py), which lives only in memory and resets whenever Jarvis
restarts. See jarvis_core.py's DAILY_LIMIT_USD, call_claude() (the one check, right at the start,
before any API call) and "allow more today" (detect_allow_more_command / allow_more_today()).
"""
import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent
SPEND_FILE = str(_BASE_DIR / "spend_log.json")
_DEFAULT_SPEND_FILE = SPEND_FILE  # captured once, at import time - see _refuse_if_untested_real_file


def _today():
    """The machine's LOCAL calendar date, as "YYYY-MM-DD". The injectable default clock - tests
    pass their own (e.g. a list of fixed date strings) to simulate a day rolling over without
    waiting for a real one."""
    return datetime.now().date().isoformat()


class DailySpend:
    """
    Running total of what TODAY (the machine's local date) has spent on the Claude API,
    persisted to a small JSON file (spend_log.json) so it survives Jarvis restarting - unlike
    ModeState's session cost, which lives only in memory. A new local date resets both `spent`
    and `bonus` back to 0.

    `path` and `clock` are injectable: a test passes a temp path and a fake clock (a callable
    taking no arguments, returning a date string) instead of ever touching the real file or the
    real system clock - see tests/test_daily_limit.py.

    Loading is lazy: nothing is read from disk until the first add()/add_bonus()/snapshot() call,
    so simply constructing one - as jarvis_core.py does once, at import time, for the real app -
    never touches the filesystem and never trips the JARVIS_TESTING guard below on its own.
    """

    def __init__(self, path=None, clock=_today):
        self._path = Path(path) if path is not None else Path(SPEND_FILE)
        self._clock = clock
        self._lock = threading.Lock()
        self._loaded = False
        self._date = None
        self.spent = 0.0
        self.bonus = 0.0
        self._warned80 = False

    def _refuse_if_untested_real_file(self):
        """Same idea as spotify_control's guard, for this file: if JARVIS_TESTING is set and this
        instance still points at the real default spend_log.json (no test gave it a temp `path`),
        refuse instead of ever reading or writing the real file."""
        if os.environ.get("JARVIS_TESTING") and str(self._path) == _DEFAULT_SPEND_FILE:
            raise RuntimeError(
                "JARVIS_TESTING is set and this DailySpend still points at the real "
                "spend_log.json - a test must construct it with its own temp `path` first."
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
                print(f"[spend] could not read {self._path.name} ({error}) - starting today at $0", flush=True)
                data = None
        today = self._clock()
        if isinstance(data, dict) and data.get("date") == today:
            try:
                spent = float(data.get("spent"))
                bonus = float(data.get("bonus"))
                # A hand-edited or otherwise corrupted file could hold a non-numeric string, one
                # of JSON's NaN/Infinity extensions, or a negative number - none of those are a
                # sane amount of money spent today, so they're treated the same as a parse error.
                if not (math.isfinite(spent) and spent >= 0 and math.isfinite(bonus) and bonus >= 0):
                    raise ValueError(f"spent={data.get('spent')!r} bonus={data.get('bonus')!r} out of range")
            except (TypeError, ValueError) as error:
                print(f"[spend] invalid spent/bonus in {self._path.name} ({error}) - starting today at $0", flush=True)
                spent, bonus = 0.0, 0.0
            self._date, self.spent, self.bonus = today, spent, bonus
        else:
            self._date = today
            self.spent = 0.0
            self.bonus = 0.0

    def _save(self):
        """Call with self._lock already held. Atomic: write to a temp file, then os.replace() it
        over the real one - a crash or power loss in between leaves the OLD file intact, never a
        half-written spend_log.json. If the write/replace itself fails (disk full, a permissions
        problem, the file locked by something else), the in-memory total - already updated by the
        caller before this runs - stays correct for the rest of THIS run; only persisting it across
        a restart is lost, so the cap keeps working today either way."""
        payload = {"date": self._date, "spent": self.spent, "bonus": self.bonus}
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as error:
            print(f"[spend] could not save spend_log.json ({error}) - keeping today's total in memory only",
                  flush=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    def _roll_if_new_day(self):
        """Call with self._lock already held, after _ensure_loaded()."""
        today = self._clock()
        if today != self._date:
            self._date = today
            self.spent = 0.0
            self.bonus = 0.0
            self._warned80 = False

    def add(self, amount):
        """Adds `amount` (USD) to today's spend and persists it. Called once per API response (see
        jarvis_core._call_claude_loop) - the cap itself is only checked once, at the START of a
        message (call_claude()), so one message already under way can push `spent` past the limit
        without being cut off mid-loop; only the NEXT message is then refused."""
        with self._lock:
            self._ensure_loaded()
            self._roll_if_new_day()
            self.spent += amount
            self._save()

    def add_bonus(self, amount):
        """"allow more today": adds only to today's bonus, never to `spent` - so it does not carry
        into tomorrow once the day rolls over."""
        with self._lock:
            self._ensure_loaded()
            self._roll_if_new_day()
            self.bonus += amount
            self._save()

    def snapshot(self):
        """(spent, bonus) for today, rolling the day over first if needed."""
        with self._lock:
            self._ensure_loaded()
            self._roll_if_new_day()
            return self.spent, self.bonus

    def take_80_percent_notice(self, daily_limit):
        """True once, the first time today's spend reaches 80% of (daily_limit + today's bonus) -
        resets at a new local day, like everything else here."""
        with self._lock:
            self._ensure_loaded()
            self._roll_if_new_day()
            limit = daily_limit + self.bonus
            if self._warned80 or limit <= 0 or self.spent < 0.8 * limit:
                return False
            self._warned80 = True
            return True

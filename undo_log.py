"""
Jarvis's own record of file changes IT made (create/edit/move via file_ops.py) in THIS process run,
so "undo last file change" can find and reverse one of them by name/number - see jarvis_core.py's
_UndoChoiceState and file_ops.undo_entry.

**In-memory only - never written to or read from disk.** Nothing about a file change survives a
restart; only changes made in the current run are ever undoable. `UNDO_LOG_FILE`'s path constant
stays defined and listed in jarvis_core.PROTECTED_PATHS purely defensively (in case a file from
before this module stopped persisting is still sitting on disk, no tool should ever be able to
touch it) - this module itself never opens it.

Pre-edit backups (`backups_dir`) are a SEPARATE, real recovery artifact and are unaffected by any
of this: they are still written to disk on every edit and still pruned by count, same as always -
only this log's own bookkeeping (which entry goes with which backup) is ephemeral.
"""
import hashlib
import os
import threading
import time
from pathlib import Path

UNDO_LOG_FILE = str(Path(r"C:\jarvis_workspace") / "undo_log.json")   # kept only for PROTECTED_PATHS
BACKUPS_DIR = str(Path(r"C:\jarvis_workspace") / "backups")
UNDO_LOG_MAX_ITEMS = 20  # bounds memory/backups-dir growth, independent of disk persistence (there is none)
ACTIONS = {"create", "edit", "move"}


def _is_within(base, target):
    """normpath FIRST, then normcase - a raw string compare would let a path like
    "<base>\\..\\..\\x" pass just because it starts with base's own text; normpath collapses the
    ".." segments (purely lexically) before the containment check runs."""
    base_s = os.path.normcase(os.path.normpath(str(base)))
    target_s = os.path.normcase(os.path.normpath(str(target)))
    return target_s == base_s or target_s.startswith(base_s + os.sep)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class UndoLog:
    def __init__(self, backups_dir=None):
        self._backups_dir = Path(backups_dir) if backups_dir is not None else Path(BACKUPS_DIR)
        self._lock = threading.Lock()
        self._entries = []
        self._next_id = 1

    def record(self, action, target_path, original_path=None, backup_path=None, post_hash=None):
        """Called right after a create/edit/move succeeds - bookkeeping only, never part of
        deciding whether that change itself was allowed. Prunes past UNDO_LOG_MAX_ITEMS, removing
        the pruned entries' backups too (only ever unlinking a backup confirmed to be inside
        backups_dir)."""
        with self._lock:
            entry = {"id": self._next_id, "at": time.time(), "action": action,
                     "target_path": str(target_path),
                     "original_path": str(original_path) if original_path else None,
                     "backup_path": str(backup_path) if backup_path else None, "post_hash": post_hash}
            self._entries.append(entry)
            self._next_id += 1
            self._entries, dropped = self._entries[-UNDO_LOG_MAX_ITEMS:], self._entries[:-UNDO_LOG_MAX_ITEMS]
            for old in dropped:
                backup = old.get("backup_path")
                if backup and _is_within(self._backups_dir, Path(backup)):
                    try:
                        Path(backup).unlink()
                    except OSError:
                        pass

    def all_entries(self):
        """Every entry, newest first, as copies - the named-undo search can match something older
        than the last 5."""
        with self._lock:
            return [dict(e) for e in reversed(self._entries)]

    def recent(self, n):
        """The newest n entries, newest first, as copies - for the bare command's list."""
        return self.all_entries()[:n]

    def remove_entry(self, entry_id):
        """Removes the entry with this id, wherever it sits in the list (not necessarily the
        newest - she can undo any of the last 5, or an older named one) - called only after that
        entry's undo has actually succeeded. Returns True if something was removed."""
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e["id"] != entry_id]
            return len(self._entries) != before

    def new_backup_path(self, original_name):
        """A unique path under backups_dir for a pre-image copy, named like _backup_notes's own
        timestamp+collision-counter scheme in jarvis_core.py."""
        self._backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        candidate = self._backups_dir / f"{stamp}_{original_name}"
        counter = 1
        while candidate.exists():
            candidate = self._backups_dir / f"{stamp}-{counter}_{original_name}"
            counter += 1
        return candidate

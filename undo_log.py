"""
Jarvis's own record of file changes IT made (create/edit/move via file_ops.py), so "undo last file
change" can reverse exactly one step. Lives OUTSIDE the repo and OUTSIDE OneDrive, in
C:\\jarvis_workspace, and is itself hard-vetoed in jarvis_core.PROTECTED_PATHS - no tool Jarvis has,
including its own file tools, may ever read, create, edit, or move undo_log.json or anything under
backups\\.

Same hardening shape as daily_spend.py/study_store.py/quiz_trigger.py (lazy load, a lock, atomic
write, an injectable path, the JARVIS_TESTING refusal of the real file). A corrupt file only costs
one unavailable undo, not her own data, so - like quiz_trigger.py, not study_store.py - it is warned
about and reset rather than renamed aside.

Nothing here is ever trusted blindly. On LOAD, every entry is re-validated strictly: a recognized
action type, every path absolute, an original_path present if and only if the action is "move," a
backup_path (when present) that actually resolves inside backups_dir - never somewhere else - and a
well-formed, lowercase sha256 hex digest - a malformed entry is dropped individually, loudly. The
same backups_dir containment check guards every unlink too: a pruned entry's backup is only ever
removed when it is confirmed to be inside backups_dir. At UNDO time
(see file_ops.undo_last_file_change), the caller re-resolves every logged path through
autonomy_scope.resolve(for_write=True) again and refuses unless the file's CURRENT sha256 still
matches the hash recorded when that change was made - this module only stores and validates the
log; it never touches the filesystem target itself.
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

UNDO_LOG_FILE = str(Path(r"C:\jarvis_workspace") / "undo_log.json")
BACKUPS_DIR = str(Path(r"C:\jarvis_workspace") / "backups")
UNDO_LOG_MAX_ITEMS = 20  # older entries are an audit trail only - just the newest is ever undoable
ACTIONS = {"create", "edit", "move"}
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")   # lowercase only - sha256_of() never produces anything else


def _is_within(base, target):
    """normpath FIRST, then normcase - a raw string compare would let a backup_path like
    "<backups_dir>\\..\\..\\x" pass just because it starts with backups_dir's own text; normpath
    collapses the ".." segments (purely lexically - it never touches the filesystem, so this works
    for a path that doesn't exist yet or ever) before the containment check runs."""
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
    def __init__(self, path=None, backups_dir=None):
        self._path = Path(path) if path is not None else Path(UNDO_LOG_FILE)
        self._backups_dir = Path(backups_dir) if backups_dir is not None else Path(BACKUPS_DIR)
        self._lock = threading.Lock()
        self._loaded = False
        self._entries = []
        self._next_id = 1

    def _refuse_if_untested_real_file(self):
        """Same idea as spotify_control's/daily_spend's guard: if JARVIS_TESTING is set and this
        instance still points at the real undo_log.json (no test gave it a temp `path`), refuse
        instead of ever reading or writing the real file."""
        if os.environ.get("JARVIS_TESTING") and str(self._path) == UNDO_LOG_FILE:
            raise RuntimeError(
                "JARVIS_TESTING is set and this UndoLog still points at the real undo_log.json - "
                "a test must construct it with its own temp `path` first."
            )

    def _ensure_loaded(self):
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
                if not isinstance(data, list):
                    raise ValueError("undo_log.json is not a list")
            except (OSError, ValueError) as error:
                print(f"[undo_log] could not read {self._path.name} ({error}) - starting empty "
                      "(the file on disk is left untouched, overwritten on the next successful "
                      "change)", flush=True)
                data = None
        entries = []
        for i, raw in enumerate(data or []):
            entry = self._validate_entry(raw, i)
            if entry is not None:
                entries.append(entry)
        self._entries = entries
        self._next_id = max((e["id"] for e in entries), default=0) + 1

    def _validate_entry(self, raw, index):
        try:
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            entry_id = raw["id"]
            if not isinstance(entry_id, int):
                raise ValueError("id is not an int")
            action = raw["action"]
            if action not in ACTIONS:
                raise ValueError(f"action {action!r} not recognized")
            target_path = raw["target_path"]
            original_path = raw.get("original_path")
            backup_path = raw.get("backup_path")
            post_hash = raw["post_hash"]
            if not isinstance(target_path, str) or not os.path.isabs(target_path):
                raise ValueError("target_path is not an absolute path")
            if action == "move":
                if not isinstance(original_path, str) or not os.path.isabs(original_path):
                    raise ValueError("a move entry needs an absolute original_path")
            elif original_path is not None:
                raise ValueError(f"a {action} entry must not have an original_path")
            if backup_path is not None:
                if not isinstance(backup_path, str) or not os.path.isabs(backup_path):
                    raise ValueError("backup_path is not an absolute path")
                if not _is_within(self._backups_dir, Path(backup_path)):
                    raise ValueError("backup_path is not inside backups_dir")
            if not isinstance(post_hash, str) or not _SHA256_HEX_RE.fullmatch(post_hash):
                raise ValueError("post_hash is not a 64-char lowercase sha256 hex digest")
            return {"id": entry_id, "at": raw.get("at"), "action": action, "target_path": target_path,
                    "original_path": original_path, "backup_path": backup_path, "post_hash": post_hash}
        except (KeyError, ValueError, TypeError) as error:
            print(f"[undo_log] entry #{index} is invalid ({error}) - dropped", flush=True)
            return None

    def _save(self):
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._entries), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as error:
            print(f"[undo_log] could not save {self._path.name} ({error}) - keeping this "
                  "session's log in memory only", flush=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    def record(self, action, target_path, original_path=None, backup_path=None, post_hash=None):
        """Called right after a create/edit/move succeeds - bookkeeping only, never part of
        deciding whether that change itself was allowed."""
        with self._lock:
            self._ensure_loaded()
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
            self._save()

    def last(self):
        with self._lock:
            self._ensure_loaded()
            return dict(self._entries[-1]) if self._entries else None

    def pop_last(self):
        """Removes and returns the newest entry, or None. Called by the undo command AFTER it has
        already reversed the change (or decided there is nothing to reverse) - see
        file_ops.undo_last_file_change."""
        with self._lock:
            self._ensure_loaded()
            if not self._entries:
                return None
            entry = self._entries.pop()
            self._save()
            return entry

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

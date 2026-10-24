"""
Per-folder file access control for Jarvis's file tools (file_ops.py): a config SHE edits by hand
(autonomy_scope.json) saying which folders Jarvis may look into, and whether it may also write
there. Jarvis never writes this file itself - see jarvis_core.PROTECTED_PATHS, which lists it so no
tool can ever target it.

Unlike every other persisted-file module in this project, this one has no "save" of its own: it is
read-only input, reloaded from disk whenever its mtime changes so an edit she makes takes effect
without a restart. ANY problem - the file missing, not valid JSON, or one entry with a bad shape -
fails CLOSED: that entry (or the whole file) is dropped with a loud warning, never "allow
everything." `resolve()` is the single entry point every file tool must call before touching disk,
for both reads and writes.

This module is intentionally import-free of jarvis_core (it would be circular): subject names and
PROTECTED_PATHS are both passed in by the caller, the same dependency-injection shape
app_launcher.resolve() already uses for its own `resolve_within_allowed` callback.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

CONFIG_FILE = str(Path(__file__).resolve().parent / "autonomy_scope.json")

VALID_MODES = {"read_only", "read_write"}

# Readable directly as text.
PLAIN_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log"}
# Readable only through file_ops.py's extraction subprocess.
EXTRACTABLE_EXTENSIONS = {".pdf", ".docx", ".pptx"}

# Global deny-list: blocked in EVERY scope, read or write, regardless of its declared mode - her
# rule is "blocked even in read_only folders," and a fortiori in read_write ones too (nothing a
# write should be able to smuggle one of these in either). Matched case-insensitively against EVERY
# path component of the resolved path, not just the leaf, so a nested ".git" three folders deep is
# still caught.
DENIED_NAME_PATTERNS = [
    r"^\.env(\..*)?$", r".*\.pem$", r".*\.key$", r"^id_rsa.*$", r"^id_ed25519.*$", r"^id_ecdsa.*$",
    r"^token.*\.json$", r".*credentials.*", r"^\.git$", r"^node_modules$", r"^__pycache__$",
    r"^\.venv$", r"^venv$",
    r"^\.ssh$", r"^\.aws$", r"^\.kube$", r"^\.docker$", r"^\.npmrc$", r"^\.pypirc$", r"^\.netrc$",
    r"^\.gitconfig$", r".*\.pfx$", r".*\.p12$", r".*\.kdbx$", r".*\.ppk$", r".*\.ovpn$", r"^secrets.*$",
    # Common browser-profile artifacts - defense in depth in case a scope is ever widened to
    # somewhere that holds one.
    r"^Login Data$", r"^Cookies$", r"^Web Data$", r"^History$", r"^Local State$",
]
_DENIED_NAME_RE = [re.compile(p, re.IGNORECASE) for p in DENIED_NAME_PATTERNS]

RESERVED_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
}


class ScopeError(Exception):
    """Base for every refusal resolve() raises. The message never contains a path - callers that
    want to show one add it themselves, capped, from the input they already have."""


class OutsideScopeError(ScopeError):
    pass


class ProtectedPathError(ScopeError):
    pass


class DeniedNameError(ScopeError):
    pass


class ExtensionNotAllowedError(ScopeError):
    pass


class ReadOnlyError(ScopeError):
    pass


class InvalidPathError(ScopeError):
    """Windows-specific hardening: UNC paths, alternate data streams, reserved device names,
    trailing dots/spaces - rejected before resolve() ever touches the filesystem."""


class ResolvedTarget:
    __slots__ = ("path", "scope_root", "mode", "subject", "allowed_extensions")

    def __init__(self, path, scope_root, mode, subject, allowed_extensions):
        self.path = path
        self.scope_root = scope_root
        self.mode = mode
        self.subject = subject
        self.allowed_extensions = allowed_extensions


def _check_windows_path_hardening(raw_path):
    """Rejects path SPELLINGS that are dangerous on Windows before any resolve()/filesystem call:
    a UNC path (escapes the local drive silently), an alternate-data-stream colon anywhere after
    the drive letter's own colon (path:stream reads/writes hidden stream data, invisible to a
    normal directory listing), a reserved device name as any path component (opening CON/NUL/COM1
    can hang or talk to a real device instead of a file, with or without an extension - "con.txt"
    is still the CON device historically), and a trailing dot/space on any component (Windows
    silently strips these, so two different-looking strings can be the same file on disk)."""
    text = str(raw_path)
    if text.startswith("\\\\") or text.startswith("//"):
        raise InvalidPathError("UNC paths are not allowed")
    drive, rest = os.path.splitdrive(text)
    if ":" in rest:
        raise InvalidPathError("alternate data streams are not allowed")
    for part in re.split(r"[\\/]+", rest):
        if not part or part in (".", ".."):
            continue
        if part != part.rstrip(" ."):
            raise InvalidPathError("trailing dots/spaces in a path component are not allowed")
        name = part.split(".")[0].upper()
        if name in RESERVED_DEVICE_NAMES:
            raise InvalidPathError(f"'{part}' is a reserved device name")


def _is_within(base, target):
    base_s = os.path.normcase(str(base))
    target_s = os.path.normcase(str(target))
    return target_s == base_s or target_s.startswith(base_s + os.sep)


def _is_denied_name(name):
    return any(rx.match(name) for rx in _DENIED_NAME_RE)


def resolve(raw_path, *, for_write, scopes, protected_paths):
    """The single checkpoint every file tool calls before touching disk. `scopes`: a list of
    {"root": Path, "mode": str, "subject": str|None, "allowed_extensions": set|None} (already
    validated - see AutonomyScope._load). `protected_paths`: an iterable of Path-like values that
    are refused absolutely, regardless of which scope would otherwise match. Raises a ScopeError
    subclass; returns a ResolvedTarget on success."""
    _check_windows_path_hardening(raw_path)
    try:
        target = Path(raw_path).resolve()
    except OSError as error:
        raise InvalidPathError(f"could not resolve this path ({error})")
    for protected in protected_paths:
        if _is_within(protected, target):
            raise ProtectedPathError("that path is protected and can never be read or written")
    for part in target.parts:
        name = part.rstrip("\\/")
        if name and _is_denied_name(name):
            raise DeniedNameError("that path name is on the built-in deny-list")
    matched = None
    for entry in scopes:
        if _is_within(entry["root"], target):
            if matched is None or len(str(entry["root"])) > len(str(matched["root"])):
                matched = entry
    if matched is None:
        raise OutsideScopeError("that path is outside every configured folder")
    allowed_ext = matched.get("allowed_extensions")
    if allowed_ext is not None and target.suffix:
        if target.suffix.lower() not in allowed_ext:
            raise ExtensionNotAllowedError(
                f"'{target.suffix}' is not allowed in this folder")
    if for_write and matched["mode"] != "read_write":
        raise ReadOnlyError("that folder is read-only")
    return ResolvedTarget(target, matched["root"], matched["mode"], matched.get("subject"), allowed_ext)


class AutonomyScope:
    """Loads and validates autonomy_scope.json, re-reading it whenever its mtime changes so an edit
    she makes by hand takes effect without restarting Jarvis."""

    def __init__(self, path=None, protected_paths=None, valid_subjects=None):
        self._path = Path(path) if path is not None else Path(CONFIG_FILE)
        self._protected_paths = [Path(p).resolve() for p in (protected_paths or [])]
        self._valid_subjects = set(valid_subjects or ())
        self._lock = threading.Lock()
        self._mtime = None
        self._scopes = []

    def _reload_if_changed(self):
        try:
            mtime = self._path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return
        self._mtime = mtime
        self._scopes = self._load()

    def _load(self):
        if not self._path.exists():
            print(f"[autonomy] {self._path.name} not found - no folders are allowed until it exists",
                  flush=True)
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            folders = data["folders"]
            if not isinstance(folders, list):
                raise ValueError("'folders' is not a list")
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"[autonomy] {self._path.name} is invalid ({error}) - no folders are allowed "
                  "until it is fixed", flush=True)
            return []
        scopes = []
        for i, entry in enumerate(folders):
            scope = self._validate_entry(entry, i)
            if scope is not None:
                scopes.append(scope)
        return scopes

    def _validate_entry(self, entry, index):
        try:
            if not isinstance(entry, dict):
                raise ValueError("not an object")
            mode = entry["mode"]
            if mode not in VALID_MODES:
                raise ValueError(f"mode {mode!r} is not one of {sorted(VALID_MODES)}")
            subject = entry.get("subject")
            if subject is not None and self._valid_subjects and subject not in self._valid_subjects:
                raise ValueError(f"subject {subject!r} is not a known study subject")
            root = Path(entry["path"]).resolve()
            if not root.is_dir():
                raise ValueError("path does not exist or is not a folder")
            allowed_ext = entry.get("allowed_extensions")
            if allowed_ext is not None:
                if not isinstance(allowed_ext, list) or not all(isinstance(e, str) for e in allowed_ext):
                    raise ValueError("allowed_extensions is not a list of strings")
                allowed_ext = {e.lower() for e in allowed_ext}
            return {"root": root, "mode": mode, "subject": subject, "allowed_extensions": allowed_ext}
        except (KeyError, ValueError, TypeError, OSError) as error:
            print(f"[autonomy] folder entry #{index} is invalid ({error}) - dropped", flush=True)
            return None

    def _current_scopes(self):
        with self._lock:
            self._reload_if_changed()
            return list(self._scopes)

    def resolve(self, raw_path, *, for_write=False):
        return resolve(raw_path, for_write=for_write, scopes=self._current_scopes(),
                       protected_paths=self._protected_paths)

    def describe(self):
        """One line per configured folder - name/mode/subject only, never raises. Used to build
        the live tool descriptions the model sees."""
        scopes = self._current_scopes()
        if not scopes:
            return "(no folders are currently configured - autonomy_scope.json has none)"
        lines = []
        for s in scopes:
            subj = f", subject: {s['subject']}" if s["subject"] else ""
            lines.append(f"- {s['root']} ({s['mode']}{subj})")
        return "\n".join(lines)

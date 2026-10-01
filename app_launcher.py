"""
Local application launcher: a hard allow-list, never free text from the model on a command line.

app_whitelist.json (next to this file, in the project root - outside ALLOWED_FOLDER and outside
every tool that can write anything) lists the only applications Jarvis may open. Jarvis cannot
edit this file: no tool ever targets it, and it isn't inside the folder read_file/list_files can
see. Two entry shapes:
  - "exe":  {"path": "...", "args": [...], "aliases": [...], "accepts_folder": true/false}
            Launched with subprocess.Popen([path, *args, *maybe_a_folder]), shell=False, ALWAYS a
            plain argv list built entirely from this file - the model only ever supplies `name`
            and, for an entry with accepts_folder, a folder to append (resolved by the caller
            against ALLOWED_FOLDER before it ever reaches here).
  - "uri":  {"target": "spotify:", "aliases": [...]}
            Launched with os.startfile(target) - for an app better opened through its own URI
            handler (a Store app with no stable, version-independent .exe path) than a raw path.

Out of scope, enforced at load time as a safety net (this file is human-edited, not model-edited,
but a mistake here should still not become a vulnerability): PowerShell, cmd, anything under
C:\\Windows, the registry/MMC editors, and - specific to VS Code - any argument that would turn
off Workspace Trust.
"""
import json
import os
import subprocess
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent
WHITELIST_FILE = _BASE_DIR / "app_whitelist.json"

# Substrings (checked on the lowercased path/target) that must never appear in a configured entry.
_DENYLIST_PATH_SUBSTRINGS = (
    "powershell", "pwsh.exe", "cmd.exe", "conhost.exe", "wscript.exe", "cscript.exe",
    "\\windows\\", "regedit", "mmc.exe", ".msc", "services.msc", "control.exe",
)
# Arguments that must never appear in ANY "exe" entry's args list (checked case-insensitively).
_DENYLIST_ARGS = ("--disable-workspace-trust", "--disable-trust", "-disable-workspace-trust")


class ConfigError(ValueError):
    """app_whitelist.json itself is malformed or contains a disallowed entry."""


def _validate_entry(name, entry):
    kind = entry.get("type")
    if kind not in ("exe", "uri"):
        raise ConfigError(f"app_whitelist.json: {name!r} has no valid \"type\" (exe/uri).")
    target = entry.get("path") if kind == "exe" else entry.get("target")
    if not target:
        raise ConfigError(f"app_whitelist.json: {name!r} has no {'path' if kind == 'exe' else 'target'}.")
    low = str(target).lower()
    if any(bad in low for bad in _DENYLIST_PATH_SUBSTRINGS):
        raise ConfigError(f"app_whitelist.json: {name!r} points at a disallowed location: {target!r}")
    for arg in entry.get("args", []):
        if arg.lower() in _DENYLIST_ARGS:
            raise ConfigError(f"app_whitelist.json: {name!r} has a disallowed argument: {arg!r}")


def _load_table(path=None):
    """{lowercased name or alias: entry}, each entry carrying its own canonical "_name".
    Missing file -> {} (open_app refuses everything until the file exists) rather than an error,
    so the rest of the app works fine before this feature is configured."""
    path = Path(path) if path is not None else WHITELIST_FILE
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    table = {}
    for name, entry in raw.items():
        _validate_entry(name, entry)
        entry = {**entry, "_name": name}
        for key in [name, *entry.get("aliases", [])]:
            table[key.strip().lower()] = entry
    return table


_cache = None  # {path: table}, so tests can point at a different file without reloading the module


def _table():
    global _cache
    if _cache is None:
        _cache = {}
    key = str(WHITELIST_FILE)
    if key not in _cache:
        _cache[key] = _load_table()
    return _cache[key]


def reload():
    """Drop the cached table - picks up an edited/replaced app_whitelist.json without restarting
    the process. Not called automatically (editing this file is a rare, deliberate admin action)."""
    global _cache
    _cache = None


def allowed_names():
    """Canonical names only (no aliases), stable order - for the tool description."""
    seen = []
    for entry in _table().values():
        if entry["_name"] not in seen:
            seen.append(entry["_name"])
    return seen


def resolve(name, folder, resolve_within_allowed):
    """
    (ok: bool, value) - value is a launch-ready dict on success, or a human-readable refusal
    string on failure. Pure and side-effect-free: nothing is opened here (see launch()).

    `resolve_within_allowed(relative_path) -> Path` is the SAME function read_file/list_files use
    (passed in instead of imported, so this module never needs to know about ALLOWED_FOLDER) -
    raises ValueError for anything that escapes it (".."," an absolute/UNC path, a junction that
    resolves outside it).
    """
    key = (name or "").strip().lower()
    entry = _table().get(key)
    if entry is None:
        names = ", ".join(allowed_names()) or "(none configured)"
        return False, f"'{name}' is not an allowed application. Allowed: {names}."
    canon = entry["_name"]

    resolved_folder = None
    if folder:
        if not entry.get("accepts_folder"):
            return False, f"'{canon}' does not accept a folder argument."
        try:
            resolved_folder = resolve_within_allowed(folder)
        except ValueError:
            return False, "That folder is outside the allowed folder."
        if not resolved_folder.is_dir():
            return False, f"Folder not found: {resolved_folder}"

    if entry["type"] == "exe":
        path = entry["path"]
        if not Path(path).is_file():
            return False, f"'{canon}' is not installed at the configured path ({path})."
        argv = [path, *entry.get("args", [])]
        if resolved_folder is not None:
            argv.append(str(resolved_folder))
        return True, {"kind": "exe", "argv": argv, "display": f"open {canon} ({' '.join(argv)})"}

    return True, {"kind": "uri", "target": entry["target"], "display": f"open {canon} ({entry['target']})"}


def launch(value):
    """Actually starts the app. `value` is the dict resolve() returned on success. Never raises
    for the app failing to run afterward (Popen only confirms the process STARTED); a bad path is
    already caught by resolve()'s is_file() check above."""
    if value["kind"] == "exe":
        subprocess.Popen(value["argv"], shell=False)
    else:
        os.startfile(value["target"])

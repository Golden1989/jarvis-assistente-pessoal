"""
Runs every tests/test_*.py file and reports pass/fail. No real Anthropic/Google call, no real
program ever launched, no personal data or real machine path - see tests/_helpers.py.

Usage:  python run_tests.py            (everything in tests/)
        python run_tests.py test_x.py  (just that one file)

pytest isn't in requirements.txt / installed in this environment, so this is the plain runner;
each file is also a normal Python script you can run directly (`python tests/test_x.py`), and each
test is an ordinary test_* function, so `python -m pytest tests/` works too if you do install it.

Also guards the real gitignored state files (autonomy_scope.json, app_whitelist.json, etc. - see
WATCHED_STATE_FILES/WATCHED_STATE_DIRS below) by fingerprinting them before and after the whole
run and failing loudly (non-zero exit, naming the file - never its contents or hash) if any of
them changed or appeared, on top of the isolation every test is already supposed to provide.
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS_DIR = ROOT / "tests"
# Belt and suspenders: tests/_helpers.py already sets this too, but every subprocess launched here
# gets it explicitly, so google_calendar.get_service()/spotify_control refuse the real API/token
# file even if a test somehow ran without importing _helpers first.
TEST_ENV = {**os.environ, "JARVIS_TESTING": "1"}



# Normal, harmless, gitignored build output that importing the project's own modules creates the
# first time - not a "stray file" in the sense this guard cares about (unlike spotify_token.json,
# which should never be touched by a test at all).
_EXPECTED_SIDE_EFFECTS = {"__pycache__"}


def _root_listing():
    """Top-level names directly in the project root - gitignored ones (spotify_token.json,
    app_whitelist.json, logs/) INCLUDED on purpose: a plain directory scan doesn't consult git at
    all, which is the point - this is exactly how the stray-file incidents this guards against
    would otherwise go unnoticed (a gitignored file never shows up in `git status`)."""
    return set(os.listdir(ROOT)) - _EXPECTED_SIDE_EFFECTS


# --- real state-file guard --------------------------------------------------------------------
# Every test is supposed to isolate itself away from these (tests/_helpers.py's isolate()) - this
# is the backstop for when one doesn't. Read-only: these files' bytes are only ever fed through
# sha256, never printed or otherwise inspected; only (exists, size, mtime, hash) is kept, and only
# the FACT that something changed - never the hash value itself - is ever printed.
WATCHED_STATE_FILES = [
    ROOT / "autonomy_scope.json",
    ROOT / "app_whitelist.json",
    ROOT / "study_data.json",
    ROOT / "quiz_trigger.json",
    ROOT / "notes.txt",
    ROOT / "spend_log.json",
    ROOT / "briefing_cache.json",
    ROOT / "spotify_token.json",
    ROOT / "token.json",
    ROOT / "credentials.json",
    Path(r"C:\jarvis_workspace\undo_log.json"),
]
WATCHED_STATE_DIRS = [
    Path(r"C:\jarvis_workspace\backups"),
]


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_fingerprint(path):
    """(exists, size, mtime_ns, sha256) for one file - all-None past the first slot if missing."""
    if not path.exists():
        return (False, None, None, None)
    stat = path.stat()
    return (True, stat.st_size, stat.st_mtime_ns, _sha256_of(path))


def _dir_fingerprint(path):
    """Same shape as _file_fingerprint, but for a whole directory: the combined size/latest
    mtime/one sha256 over every contained file's (relative path, size, mtime, hash), sorted - so
    adding, removing, or modifying ANYTHING inside changes the fingerprint."""
    if not path.exists():
        return (False, None, None, None)
    total_size, latest_mtime, entries = 0, 0, []
    for sub in sorted(p for p in path.rglob("*") if p.is_file()):
        stat = sub.stat()
        total_size += stat.st_size
        latest_mtime = max(latest_mtime, stat.st_mtime_ns)
        entries.append(f"{sub.relative_to(path)}:{stat.st_size}:{stat.st_mtime_ns}:{_sha256_of(sub)}")
    combined = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return (True, total_size, latest_mtime, combined)


def _snapshot_watched_state():
    snap = {str(f): _file_fingerprint(f) for f in WATCHED_STATE_FILES}
    snap.update({str(d): _dir_fingerprint(d) for d in WATCHED_STATE_DIRS})
    return snap


def _watched_state_changes(before, after):
    """[(label, "appeared"|"disappeared"|"modified"), ...] - names only, never a hash/size/mtime
    value, and never any file content."""
    changes = []
    for key, before_fp in before.items():
        after_fp = after[key]
        if before_fp == after_fp:
            continue
        if not before_fp[0] and after_fp[0]:
            changes.append((key, "appeared"))
        elif before_fp[0] and not after_fp[0]:
            changes.append((key, "disappeared"))
        else:
            changes.append((key, "modified"))
    return changes


def main():
    names = sys.argv[1:]
    files = sorted(TESTS_DIR.glob(names[0]) if names else TESTS_DIR.glob("test_*.py"))
    if not files:
        print("No tests matched."); return 1

    state_before = _snapshot_watched_state()

    results = {}
    stray_by_file = {}
    for f in files:
        print(f"\n=== {f.name} " + "=" * max(0, 60 - len(f.name)))
        before = _root_listing()
        proc = subprocess.run([sys.executable, str(f)], env=TEST_ENV)
        after = _root_listing()
        new_entries = sorted(after - before)
        ok = proc.returncode == 0 and not new_entries
        results[f.name] = ok
        if proc.returncode != 0:
            print(f"!!! {f.name} FAILED (exit {proc.returncode})")
        if new_entries:
            stray_by_file[f.name] = new_entries
            print(f"!!! {f.name} left new entries in the project root (never allowed, even if "
                  f"gitignored): {new_entries}")

    state_changes = _watched_state_changes(state_before, _snapshot_watched_state())

    print("\n" + "=" * 60)
    passed = sum(results.values())
    for name, ok in results.items():
        print(f"{'ok  ' if ok else 'FAIL'}  {name}")
    if stray_by_file:
        print(f"\nSTRAY FILES LEFT IN THE PROJECT ROOT: {stray_by_file}")
        print("These were NOT deleted - inspect and remove them yourself once you've confirmed what they are.")
    if state_changes:
        print("\n!!! REAL STATE FILE(S) CHANGED DURING THIS RUN - some test touched something it "
              "should have isolated away from (never shown: contents or hashes, only the names):")
        for label, kind in state_changes:
            print(f"    {kind}: {label}")
    print(f"\n{passed}/{len(results)} test files passed")
    if state_changes:
        return 1
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

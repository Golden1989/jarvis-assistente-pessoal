"""
Runs every tests/test_*.py file and reports pass/fail. No real Anthropic/Google call, no real
program ever launched, no personal data or real machine path - see tests/_helpers.py.

Usage:  python run_tests.py            (everything in tests/)
        python run_tests.py test_x.py  (just that one file)

pytest isn't in requirements.txt / installed in this environment, so this is the plain runner;
each file is also a normal Python script you can run directly (`python tests/test_x.py`), and each
test is an ordinary test_* function, so `python -m pytest tests/` works too if you do install it.
"""
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


def main():
    names = sys.argv[1:]
    files = sorted(TESTS_DIR.glob(names[0]) if names else TESTS_DIR.glob("test_*.py"))
    if not files:
        print("No tests matched."); return 1

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

    print("\n" + "=" * 60)
    passed = sum(results.values())
    for name, ok in results.items():
        print(f"{'ok  ' if ok else 'FAIL'}  {name}")
    if stray_by_file:
        print(f"\nSTRAY FILES LEFT IN THE PROJECT ROOT: {stray_by_file}")
        print("These were NOT deleted - inspect and remove them yourself once you've confirmed what they are.")
    print(f"\n{passed}/{len(results)} test files passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

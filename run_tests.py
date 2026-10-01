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

TESTS_DIR = Path(__file__).resolve().parent / "tests"
# Belt and suspenders: tests/_helpers.py already sets this too, but every subprocess launched here
# gets it explicitly, so google_calendar.get_service() refuses the real API even if a test somehow
# ran without importing _helpers first.
TEST_ENV = {**os.environ, "JARVIS_TESTING": "1"}


def main():
    names = sys.argv[1:]
    files = sorted(TESTS_DIR.glob(names[0]) if names else TESTS_DIR.glob("test_*.py"))
    if not files:
        print("No tests matched."); return 1

    results = {}
    for f in files:
        print(f"\n=== {f.name} " + "=" * max(0, 60 - len(f.name)))
        proc = subprocess.run([sys.executable, str(f)], env=TEST_ENV)
        results[f.name] = proc.returncode == 0
        if proc.returncode != 0:
            print(f"!!! {f.name} FAILED (exit {proc.returncode})")

    print("\n" + "=" * 60)
    passed = sum(results.values())
    for name, ok in results.items():
        print(f"{'ok  ' if ok else 'FAIL'}  {name}")
    print(f"\n{passed}/{len(results)} test files passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

"""app_log.py: quick coverage (tee to file, rotation, no console needed). Not a full rebuild of the
earlier session's suite (that also covered a dead console and an unwritable path) - just enough to
catch a fast regression. All paths are temp folders, nothing real."""
import io
import sys
import tempfile
from pathlib import Path

from _helpers import jc, run_module_tests

sys.path.insert(0, str(Path(jc.__file__).resolve().parent / "web"))
import app_log


def _fresh_logger():
    if app_log._logger is not None:
        for h in list(app_log._logger.handlers):
            h.close()
            app_log._logger.removeHandler(h)
    app_log._logger = None


def test_tee_writes_to_file_and_keeps_the_original_stream():
    real_out, real_err = sys.stdout, sys.stderr
    tmp_dir = Path(tempfile.mkdtemp())
    console = io.StringIO()
    sys.stdout = sys.stderr = console
    try:
        _fresh_logger()
        log_path = tmp_dir / "logs" / "jarvis.log"
        assert app_log.setup(log_path) == log_path and log_path.parent.is_dir()
        print("[wake_word] a test line", flush=True)
        print("[desktop] another line", flush=True)
        text = log_path.read_text(encoding="utf-8")
        assert "[wake_word] a test line" in text and "[desktop] another line" in text
        assert "[wake_word] a test line" in console.getvalue()   # the original stream still sees it
        lines = [l for l in text.splitlines() if l.strip()]
        import re
        assert all(re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ", l) for l in lines)
    finally:
        _fresh_logger()
        sys.stdout, sys.stderr = real_out, real_err


def test_rotation_keeps_a_bounded_number_of_files():
    real_out, real_err = sys.stdout, sys.stderr
    tmp_dir = Path(tempfile.mkdtemp())
    sys.stdout = sys.stderr = io.StringIO()
    try:
        _fresh_logger()
        log_path = tmp_dir / "jarvis.log"
        app_log.setup(log_path, max_bytes=500, backup_count=2)
        for i in range(200):
            print(f"[wake_word] line {i:04d} " + "x" * 20, flush=True)
        files = list(tmp_dir.glob("jarvis.log*"))
        assert 1 <= len(files) <= 3   # backup_count=2 + the live file
        assert "line 0199" in log_path.read_text(encoding="utf-8")
    finally:
        _fresh_logger()
        sys.stdout, sys.stderr = real_out, real_err


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])

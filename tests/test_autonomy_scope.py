"""
Stage A of the file-autonomy feature: autonomy_scope.py (scope resolution, Windows path
hardening, the deny-list, PROTECTED_PATHS enforcement), file_ops.py (list/read/create/edit/move,
extraction, OneDrive placeholders, size caps), extract_worker.py's zip-bomb hardening, and
undo_log.py (schema validation, hash-verified undo).

Every test is offline and self-contained: no real Anthropic call, no real folder on the developer's
machine, and no assumption about whether pypdf/python-docx/python-pptx happen to be installed in
this environment - a test that reaches the extraction subprocess accepts either the graceful
"library not installed" error OR a graceful "could not open this file" parse error on the
deliberately-fake document content used here, and asserts only that it's never a crash or a hang.
Tests of logic that's independent of those libraries entirely (the zip-bomb checks, the
pagination/windowing math, the pptx shape-text recursion) use fakes instead, so they exercise the
real code regardless of what's installed. Everything lives under a fresh tempfile.mkdtemp() per
test.

Run directly (`python tests/test_autonomy_scope.py`) or via `python run_tests.py`.
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _helpers import (
    R, SR, SU, T, TU, fresh_server, isolate, jc, post_chat, run_module_tests, write_autonomy_scope,
)

import autonomy_scope as asc  # noqa: E402 - after _helpers, which puts the project root on sys.path
import extract_worker as ew  # noqa: E402
import file_ops as fo  # noqa: E402
import undo_log as ul  # noqa: E402

quiet = contextlib.redirect_stdout


def tmp():
    return Path(tempfile.mkdtemp())


def _scope_entry(root, mode="read_only", subject=None, allowed_extensions=None):
    return {"root": Path(root), "mode": mode, "subject": subject,
            "allowed_extensions": {e.lower() for e in allowed_extensions} if allowed_extensions else None}


class _FakeScope:
    """Duck-types AutonomyScope.resolve() with a FIXED scopes/protected_paths list - for
    file_ops.py tests that don't need the config-file/live-reload machinery."""

    def __init__(self, scopes, protected_paths=None):
        self.scopes = scopes
        self.protected_paths = protected_paths or []

    def resolve(self, raw_path, for_write=False):
        return asc.resolve(raw_path, for_write=for_write, scopes=self.scopes,
                           protected_paths=self.protected_paths)


# ============================================================ Windows path hardening

def test_windows_hardening_rejects_unc_paths():
    scopes = [_scope_entry(tmp())]
    for bad in ["\\\\server\\share\\file.txt", "//server/share/file.txt"]:
        try:
            asc.resolve(bad, for_write=False, scopes=scopes, protected_paths=[])
            raise AssertionError(f"should have been refused: {bad}")
        except asc.InvalidPathError:
            pass


def test_windows_hardening_rejects_alternate_data_streams():
    root = tmp()
    scopes = [_scope_entry(root)]
    for bad in [str(root / "file.txt:hidden"), "relative.txt:stream"]:
        try:
            asc.resolve(bad, for_write=False, scopes=scopes, protected_paths=[])
            raise AssertionError(f"should have been refused: {bad}")
        except asc.InvalidPathError:
            pass


def test_windows_hardening_rejects_reserved_device_names_with_or_without_extension():
    root = tmp()
    scopes = [_scope_entry(root)]
    for bad_name in ["CON", "con.txt", "NUL", "nul.md", "COM1", "lpt1.csv"]:
        bad = str(root / bad_name)
        try:
            asc.resolve(bad, for_write=False, scopes=scopes, protected_paths=[])
            raise AssertionError(f"should have been refused: {bad}")
        except asc.InvalidPathError:
            pass


def test_windows_hardening_rejects_trailing_dot_or_space():
    root = tmp()
    scopes = [_scope_entry(root)]
    for bad_name in ["secret.txt.", "secret.txt "]:
        bad = str(root / bad_name)
        try:
            asc.resolve(bad, for_write=False, scopes=scopes, protected_paths=[])
            raise AssertionError(f"should have been refused: {bad}")
        except asc.InvalidPathError:
            pass


# ============================================================ deny-list

DENY_DIR_NAMES = [".git", "node_modules", "__pycache__", ".venv", "venv", ".ssh", ".aws", ".kube", ".docker"]
DENY_FILE_NAMES = [
    ".env", ".env.local", "id_rsa", "id_rsa.pub", "id_ed25519", "id_ecdsa", "token_abc.json",
    "my_credentials.txt", ".npmrc", ".pypirc", ".netrc", ".gitconfig", "cert.pfx", "cert.p12",
    "vault.kdbx", "key.ppk", "config.ovpn", "secrets.json", "secretsfile.txt", "Login Data",
    "Cookies", "Web Data", "History", "Local State", "server.pem", "server.key",
]


def test_deny_list_blocks_directory_names_in_both_modes():
    for mode in ("read_only", "read_write"):
        root = tmp()
        scopes = [_scope_entry(root, mode=mode)]
        for name in DENY_DIR_NAMES:
            bad = str(root / name / "inner.txt")
            try:
                asc.resolve(bad, for_write=False, scopes=scopes, protected_paths=[])
                raise AssertionError(f"should have been refused ({mode}): {bad}")
            except asc.DeniedNameError:
                pass


def test_deny_list_blocks_file_names_in_both_modes():
    for mode in ("read_only", "read_write"):
        root = tmp()
        scopes = [_scope_entry(root, mode=mode)]
        for name in DENY_FILE_NAMES:
            bad = str(root / name)
            try:
                asc.resolve(bad, for_write=False, scopes=scopes, protected_paths=[])
                raise AssertionError(f"should have been refused ({mode}): {bad}")
            except asc.DeniedNameError:
                pass


def test_deny_list_catches_a_nested_match_three_folders_deep():
    root = tmp()
    scopes = [_scope_entry(root)]
    bad = str(root / "a" / "b" / ".git" / "config")
    try:
        asc.resolve(bad, for_write=False, scopes=scopes, protected_paths=[])
        raise AssertionError("should have been refused")
    except asc.DeniedNameError:
        pass


# ============================================================ PROTECTED_PATHS / scope membership

def test_protected_path_wins_even_nested_inside_a_read_write_scope():
    root = tmp()
    protected = root / "undo_log.json"
    scopes = [_scope_entry(root, mode="read_write")]
    for for_write in (False, True):
        try:
            asc.resolve(str(protected), for_write=for_write, scopes=scopes, protected_paths=[protected])
            raise AssertionError(f"should have been refused (for_write={for_write})")
        except asc.ProtectedPathError:
            pass


def test_outside_every_configured_folder_is_refused():
    root, other = tmp(), tmp()
    scopes = [_scope_entry(root)]
    try:
        asc.resolve(str(other / "x.txt"), for_write=False, scopes=scopes, protected_paths=[])
        raise AssertionError("should have been refused")
    except asc.OutsideScopeError:
        pass


def test_read_only_scope_allows_reads_but_refuses_writes():
    root = tmp()
    scopes = [_scope_entry(root, mode="read_only")]
    asc.resolve(str(root / "x.txt"), for_write=False, scopes=scopes, protected_paths=[])  # no raise
    try:
        asc.resolve(str(root / "x.txt"), for_write=True, scopes=scopes, protected_paths=[])
        raise AssertionError("should have been refused")
    except asc.ReadOnlyError:
        pass


def test_allowed_extensions_narrows_a_scope_to_specific_file_types():
    root = tmp()
    scopes = [_scope_entry(root, allowed_extensions=[".py", ".md"])]
    assert asc.resolve(str(root / "core.py"), for_write=False, scopes=scopes, protected_paths=[])
    assert asc.resolve(str(root / "readme.md"), for_write=False, scopes=scopes, protected_paths=[])
    try:
        asc.resolve(str(root / "study_data.json"), for_write=False, scopes=scopes, protected_paths=[])
        raise AssertionError("should have been refused")
    except asc.ExtensionNotAllowedError:
        pass


def test_junction_escaping_a_read_write_scope_resolves_to_its_real_targets_own_rules():
    """A junction inside a read-write root pointing at a read-only root must resolve to the
    read-only root's OWN rules (still refuses a write) - Path.resolve() dereferences it first, the
    same proven technique as the old single-folder _resolve_within_allowed's own junction test."""
    write_root, read_root = tmp(), tmp()
    (read_root / "secret.txt").write_text("not real data", encoding="utf-8")
    junction = write_root / "link_out"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(read_root)],
                          capture_output=True, text=True)
    try:
        if made.returncode != 0:
            print(f"(skipped: could not create a test junction on this machine: {made.stderr.strip()})")
            return
        scopes = [_scope_entry(write_root, mode="read_write"), _scope_entry(read_root, mode="read_only")]
        target = asc.resolve(str(junction / "secret.txt"), for_write=False, scopes=scopes, protected_paths=[])
        assert target.mode == "read_only"
        try:
            asc.resolve(str(junction / "secret.txt"), for_write=True, scopes=scopes, protected_paths=[])
            raise AssertionError("should have been refused for a write")
        except asc.ReadOnlyError:
            pass
    finally:
        if junction.exists() or junction.is_symlink():
            subprocess.run(["cmd", "/c", "rmdir", str(junction)], capture_output=True)


# ============================================================ AutonomyScope: fail-closed loading

def test_missing_config_file_means_zero_scopes():
    work = tmp()
    scope = asc.AutonomyScope(path=work / "autonomy_scope.json", protected_paths=[], valid_subjects=set())
    with quiet(io.StringIO()):
        try:
            scope.resolve(str(work / "x.txt"))
            raise AssertionError("should have been refused")
        except asc.OutsideScopeError:
            pass


def test_malformed_json_means_zero_scopes_with_a_warning():
    work = tmp()
    path = work / "autonomy_scope.json"
    path.write_text("{not json", encoding="utf-8")
    scope = asc.AutonomyScope(path=path, protected_paths=[], valid_subjects=set())
    buf = io.StringIO()
    with quiet(buf):
        try:
            scope.resolve(str(work / "x.txt"))
            raise AssertionError("should have been refused")
        except asc.OutsideScopeError:
            pass
    assert "[autonomy]" in buf.getvalue()


def test_one_bad_entry_among_good_ones_only_drops_that_entry():
    work = tmp()
    good_root = work / "good"
    good_root.mkdir()
    path = work / "autonomy_scope.json"
    path.write_text(json.dumps({"folders": [
        {"path": str(good_root), "mode": "read_only", "subject": None},
        {"path": str(work / "doesnotexist"), "mode": "read_only", "subject": None},
    ]}), encoding="utf-8")
    scope = asc.AutonomyScope(path=path, protected_paths=[], valid_subjects=set())
    buf = io.StringIO()
    with quiet(buf):
        target = scope.resolve(str(good_root / "x.txt"))
    assert target.mode == "read_only"
    assert "[autonomy]" in buf.getvalue()


def test_bad_mode_value_drops_that_entry():
    work = tmp()
    root = work / "f"
    root.mkdir()
    path = work / "autonomy_scope.json"
    path.write_text(json.dumps({"folders": [{"path": str(root), "mode": "readonly", "subject": None}]}),
                    encoding="utf-8")
    scope = asc.AutonomyScope(path=path, protected_paths=[], valid_subjects=set())
    with quiet(io.StringIO()):
        try:
            scope.resolve(str(root / "x.txt"))
            raise AssertionError("should have been refused")
        except asc.OutsideScopeError:
            pass


def test_unknown_subject_drops_that_entry():
    work = tmp()
    root = work / "f"
    root.mkdir()
    path = work / "autonomy_scope.json"
    path.write_text(json.dumps({"folders": [{"path": str(root), "mode": "read_only", "subject": "astrology"}]}),
                    encoding="utf-8")
    scope = asc.AutonomyScope(path=path, protected_paths=[], valid_subjects={"forensics"})
    with quiet(io.StringIO()):
        try:
            scope.resolve(str(root / "x.txt"))
            raise AssertionError("should have been refused")
        except asc.OutsideScopeError:
            pass


def test_live_reload_picks_up_an_edit_without_a_restart():
    work = tmp()
    root = work / "f"
    root.mkdir()
    path = work / "autonomy_scope.json"
    scope = asc.AutonomyScope(path=path, protected_paths=[], valid_subjects=set())
    with quiet(io.StringIO()):
        try:
            scope.resolve(str(root / "x.txt"))
            raise AssertionError("should have been refused before the config exists")
        except asc.OutsideScopeError:
            pass
    path.write_text(json.dumps({"folders": [{"path": str(root), "mode": "read_only", "subject": None}]}),
                    encoding="utf-8")
    target = scope.resolve(str(root / "x.txt"))   # SAME instance - no re-construction, no restart
    assert target.mode == "read_only"


# ============================================================ file_ops: list_files / read_file

def test_list_files_hides_denylisted_names_but_shows_unsupported_types():
    root = tmp()
    (root / "notes.txt").write_text("hi", encoding="utf-8")
    (root / "report.docx").write_bytes(b"not a real docx")
    (root / "evidence.E01").write_bytes(b"\x00")
    (root / "token_abc.json").write_text("{}", encoding="utf-8")
    scope = _FakeScope([_scope_entry(root)])
    listing = fo.list_files(scope, str(root))
    assert "notes.txt" in listing
    assert "report.docx" in listing
    assert "evidence.E01" in listing
    assert "token_abc.json" not in listing


def test_read_file_plain_text_types_work():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    for ext in (".txt", ".md", ".csv", ".log"):
        p = root / f"f{ext}"
        p.write_text("hello world", encoding="utf-8")
        assert fo.read_file(scope, str(p)) == "hello world"


def test_read_file_refuses_unsupported_extension():
    root = tmp()
    p = root / "image.png"
    p.write_bytes(b"\x89PNG")
    scope = _FakeScope([_scope_entry(root)])
    assert "Unsupported" in fo.read_file(scope, str(p))


def test_read_file_offset_pages_past_the_cap():
    root = tmp()
    p = root / "big.txt"
    p.write_text("x" * fo.FILE_READ_MAX_CHARS + "y" * 500, encoding="utf-8")
    scope = _FakeScope([_scope_entry(root)])
    first = fo.read_file(scope, str(p))
    assert "more characters" in first and "offset=" in first
    second = fo.read_file(scope, str(p), offset=fo.FILE_READ_MAX_CHARS)
    assert "y" * 500 in second and "more characters" not in second


def test_read_file_refuses_a_file_over_the_byte_cap_before_opening():
    root = tmp()
    p = root / "f.txt"
    p.write_text("more than ten bytes of text", encoding="utf-8")
    scope = _FakeScope([_scope_entry(root)])
    with mock.patch.object(fo, "FILE_READ_MAX_BYTES", 10):
        result = fo.read_file(scope, str(p))
    assert "too large" in result


def test_read_file_refuses_an_extractable_file_over_the_source_byte_cap():
    root = tmp()
    p = root / "f.pdf"
    p.write_bytes(b"%PDF-1.4 not a real pdf")
    scope = _FakeScope([_scope_entry(root)])
    with mock.patch.object(fo, "FILE_EXTRACT_MAX_SOURCE_BYTES", 1):
        result = fo.read_file(scope, str(p))
    assert "too large" in result


def test_read_file_refuses_a_onedrive_cloud_only_placeholder():
    root = tmp()
    p = root / "f.txt"
    p.write_text("x", encoding="utf-8")
    scope = _FakeScope([_scope_entry(root)])
    with mock.patch.object(fo, "_is_cloud_placeholder", return_value=True):
        result = fo.read_file(scope, str(p))
    assert "cloud-only" in result


def test_read_file_pdf_extraction_failure_gives_a_clear_error_not_a_crash():
    """This fake "PDF" is not a real one - whether pypdf is installed or not, extraction must
    fail cleanly (missing-library or a parse error), never crash or hang the subprocess path."""
    root = tmp()
    p = root / "f.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF")
    scope = _FakeScope([_scope_entry(root)])
    result = fo.read_file(scope, str(p))
    assert "Could not read this file" in result


# ============================================================ file_ops: create_file

def test_create_file_prep_and_do_round_trip():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "notes.md"
    ok, item_input, display = fo.prep_create_file(scope, {"path": str(target), "content": "hello"})
    assert ok and "create" in display
    assert fo.do_create_file(scope, undo, item_input) == f"Created {target.resolve()}."
    assert target.read_text(encoding="utf-8") == "hello"
    assert undo.last()["action"] == "create"


def test_create_file_refuses_if_target_already_exists():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    target = root / "notes.md"
    target.write_text("already here", encoding="utf-8")
    ok, _, display = fo.prep_create_file(scope, {"path": str(target), "content": "new"})
    assert not ok and "already exists" in display


def test_create_file_refuses_against_a_read_only_target_without_touching_disk():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_only")])
    target = root / "notes.md"
    ok, _, _ = fo.prep_create_file(scope, {"path": str(target), "content": "x"})
    assert not ok and not target.exists()


def test_create_file_refuses_every_dangerous_extension():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    for ext in (".bat", ".cmd", ".ps1", ".js", ".vbs", ".py", ".exe", ".lnk", ".dll", ".reg", ".url"):
        ok, _, _ = fo.prep_create_file(scope, {"path": str(root / f"x{ext}"), "content": "x"})
        assert not ok, ext


def test_create_file_accepts_every_writable_extension():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    for ext in (".md", ".txt", ".csv", ".json"):
        ok, _, _ = fo.prep_create_file(scope, {"path": str(root / f"x{ext}"), "content": "x"})
        assert ok, ext


def test_create_file_refuses_content_over_the_size_cap():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    with mock.patch.object(fo, "CREATE_FILE_MAX_CHARS", 5):
        ok, _, display = fo.prep_create_file(scope, {"path": str(root / "x.txt"), "content": "toolong"})
    assert not ok and "too long" in display


def test_create_file_confirm_time_refuses_if_something_appeared_since_it_was_held():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "notes.md"
    ok, item_input, _ = fo.prep_create_file(scope, {"path": str(target), "content": "hi"})
    assert ok
    target.write_text("something else got here first", encoding="utf-8")
    result = fo.do_create_file(scope, undo, item_input)
    assert "Error" in result
    assert target.read_text(encoding="utf-8") == "something else got here first"


# ============================================================ file_ops: edit_file

def test_edit_file_exact_replacement_round_trip_with_a_real_diff_and_a_backup():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "notes.md"
    target.write_text("line one\nline two\nline three\n", encoding="utf-8")
    ok, item_input, display = fo.prep_edit_file(
        scope, {"path": str(target), "old_text": "line two", "new_text": "line TWO"})
    assert ok and "+1/-1" in display
    assert fo.do_edit_file(scope, undo, item_input) == f"Edited {target.resolve()}."
    assert target.read_text(encoding="utf-8") == "line one\nline TWO\nline three\n"
    entry = undo.last()
    assert entry["action"] == "edit"
    assert Path(entry["backup_path"]).read_text(encoding="utf-8") == "line one\nline two\nline three\n"


def test_edit_file_refuses_when_old_text_is_missing():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    target = root / "notes.md"
    target.write_text("hello", encoding="utf-8")
    ok, _, display = fo.prep_edit_file(scope, {"path": str(target), "old_text": "goodbye", "new_text": "x"})
    assert not ok and "not found" in display


def test_edit_file_refuses_when_old_text_matches_more_than_once():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    target = root / "notes.md"
    target.write_text("dup dup", encoding="utf-8")
    ok, _, display = fo.prep_edit_file(scope, {"path": str(target), "old_text": "dup", "new_text": "x"})
    assert not ok and "ambiguous" in display


def test_edit_file_refuses_at_confirm_time_when_old_text_no_longer_matches_once():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "notes.md"
    target.write_text("line one\nline two\n", encoding="utf-8")
    ok, item_input, _ = fo.prep_edit_file(scope, {"path": str(target), "old_text": "line two", "new_text": "x"})
    assert ok
    target.write_text("line one\nsomething else entirely\n", encoding="utf-8")   # changed since prep
    result = fo.do_edit_file(scope, undo, item_input)
    assert "changed since" in result
    assert target.read_text(encoding="utf-8") == "line one\nsomething else entirely\n"


def test_edit_file_allows_a_file_larger_than_the_read_cap_when_the_snippet_fits():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    target = root / "notes.md"
    target.write_text("x" * (fo.FILE_READ_MAX_CHARS + 1000) + "NEEDLE" + "y" * 100, encoding="utf-8")
    ok, _, _ = fo.prep_edit_file(scope, {"path": str(target), "old_text": "NEEDLE", "new_text": "FOUND"})
    assert ok


def test_edit_file_refuses_when_old_text_plus_new_text_exceeds_the_snippet_cap():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    target = root / "notes.md"
    target.write_text("hello", encoding="utf-8")
    with mock.patch.object(fo, "EDIT_SNIPPET_MAX_CHARS", 5):
        ok, _, display = fo.prep_edit_file(scope, {"path": str(target), "old_text": "hello", "new_text": "goodbye"})
    assert not ok and "too large" in display


def test_edit_file_refuses_a_non_writable_extension():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    target = root / "script.py"
    target.write_text("print(1)", encoding="utf-8")
    ok, _, _ = fo.prep_edit_file(scope, {"path": str(target), "old_text": "print(1)", "new_text": "print(2)"})
    assert not ok


def test_edit_file_prep_refuses_a_file_over_the_byte_cap():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    target = root / "big.md"
    target.write_text("hello world", encoding="utf-8")
    with mock.patch.object(fo, "FILE_READ_MAX_BYTES", 5):
        ok, _, display = fo.prep_edit_file(scope, {"path": str(target), "old_text": "hello", "new_text": "hi"})
    assert not ok and "too large" in display


def test_edit_file_confirm_time_also_refuses_a_file_over_the_byte_cap():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "big.md"
    target.write_text("hello world", encoding="utf-8")
    ok, item_input, _ = fo.prep_edit_file(scope, {"path": str(target), "old_text": "hello", "new_text": "hi"})
    assert ok
    with mock.patch.object(fo, "FILE_READ_MAX_BYTES", 5):
        result = fo.do_edit_file(scope, undo, item_input)
    assert "too large" in result
    assert target.read_text(encoding="utf-8") == "hello world"   # untouched


def test_edit_file_refuses_non_utf8_content_instead_of_lossy_decoding():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    target = root / "bad.txt"
    target.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    ok, _, display = fo.prep_edit_file(scope, {"path": str(target), "old_text": "x", "new_text": "y"})
    assert not ok and "not valid UTF-8" in display


def test_edit_file_preserves_crlf_line_endings_exactly():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "crlf.txt"
    target.write_bytes(b"line one\r\nline two\r\nline three\r\n")
    ok, item_input, _ = fo.prep_edit_file(
        scope, {"path": str(target), "old_text": "line two", "new_text": "line TWO"})
    assert ok
    fo.do_edit_file(scope, undo, item_input)
    assert target.read_bytes() == b"line one\r\nline TWO\r\nline three\r\n"   # every \r\n still literal


# ============================================================ file_ops: move_file

def test_move_file_round_trip_between_two_read_write_scopes():
    src_root, dst_root = tmp(), tmp()
    scope = _FakeScope([_scope_entry(src_root, mode="read_write"), _scope_entry(dst_root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    source = src_root / "notes.txt"
    source.write_text("hi", encoding="utf-8")
    dest = dst_root / "notes.txt"
    ok, item_input, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(dest)})
    assert ok
    assert "Moved" in fo.do_move_file(scope, undo, item_input)
    assert not source.exists() and dest.read_text(encoding="utf-8") == "hi"
    entry = undo.last()
    assert entry["action"] == "move" and entry["original_path"] == str(source.resolve())


def test_move_file_refuses_when_source_is_read_only():
    src_root, dst_root = tmp(), tmp()
    scope = _FakeScope([_scope_entry(src_root, mode="read_only"), _scope_entry(dst_root, mode="read_write")])
    source = src_root / "notes.txt"
    source.write_text("hi", encoding="utf-8")
    ok, _, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(dst_root / "notes.txt")})
    assert not ok


def test_move_file_refuses_when_destination_is_read_only():
    src_root, dst_root = tmp(), tmp()
    scope = _FakeScope([_scope_entry(src_root, mode="read_write"), _scope_entry(dst_root, mode="read_only")])
    source = src_root / "notes.txt"
    source.write_text("hi", encoding="utf-8")
    ok, _, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(dst_root / "notes.txt")})
    assert not ok


def test_move_file_refuses_a_non_writable_destination_extension():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    source = root / "notes.txt"
    source.write_text("hi", encoding="utf-8")
    ok, _, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(root / "notes.exe")})
    assert not ok


def test_move_file_never_overwrites_an_existing_destination():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    source = root / "a.txt"
    source.write_text("a", encoding="utf-8")
    dest = root / "b.txt"
    dest.write_text("b", encoding="utf-8")
    ok, _, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(dest)})
    assert not ok and dest.read_text(encoding="utf-8") == "b"


def test_move_file_allows_keeping_any_movable_extension_unchanged():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    for ext in sorted(fo.MOVABLE_EXTENSIONS):
        source = root / f"a{ext}"
        source.write_bytes(b"x")
        ok, _, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(root / f"b{ext}")})
        assert ok, ext


def test_move_file_allows_changing_extension_only_into_a_writable_type():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    source = root / "report.pdf"
    source.write_bytes(b"x")
    ok, _, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(root / "report.txt")})
    assert ok


def test_move_file_refuses_changing_extension_into_a_non_writable_type():
    """.log is movable AS a same-extension move, but is not in WRITABLE_EXTENSIONS, so it can't be
    a destination for an extension CHANGE."""
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    source = root / "report.pdf"
    source.write_bytes(b"x")
    ok, _, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(root / "report.log")})
    assert not ok


def test_move_file_refuses_a_non_movable_source_extension_regardless_of_destination():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    source = root / "script.py"
    source.write_bytes(b"x")
    for dest_ext in (".py", ".txt"):
        ok, _, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(root / f"x{dest_ext}")})
        assert not ok, dest_ext


# ============================================================ extract_worker.py: zip-bomb hardening

def test_extract_worker_refuses_too_many_zip_members():
    path = tmp() / "bomb.docx"
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(ew.MAX_ZIP_MEMBERS + 1):
            zf.writestr(f"part{i}.xml", "x")
    error = ew._check_zip_safety(path)
    assert error is not None and "members" in error


def test_extract_worker_refuses_an_absurd_compression_ratio():
    path = tmp() / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("huge.xml", "0" * 20_000_000)
    assert ew._check_zip_safety(path) is not None


def test_extract_worker_refuses_aggregate_uncompressed_size_over_the_cap():
    path = tmp() / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:   # STORED: compress_size == file_size,
        zf.writestr("a.xml", "x" * 1000)                        # ratio stays 1 - only the aggregate
        zf.writestr("b.xml", "x" * 1000)                        # size cap should be what fires here
    with mock.patch.object(ew, "MAX_ZIP_UNCOMPRESSED_BYTES", 500):
        error = ew._check_zip_safety(path)
    assert error is not None and "large" in error


def test_extract_worker_zip_safety_check_passes_for_an_ordinary_small_zip():
    path = tmp() / "ok.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", "<xml>hello</xml>")
    assert ew._check_zip_safety(path) is None
    # Whether python-docx is installed in THIS environment or not, the zip-safety check must run
    # BEFORE that import attempt, so a normal small zip never gets refused as a zip bomb - it
    # either reaches the "library missing" error (not installed) or fails later trying to parse
    # this fake, non-real docx body (installed) - either way, never a zip-bomb refusal.
    _, _, error = ew.extract_docx(str(path), 1, 30, 1000)
    zip_bomb_markers = ("too many zip members", "compression ratio is implausibly high",
                       "uncompressed content would be too large")
    assert not (error and any(marker in error for marker in zip_bomb_markers))
    assert error is None or error == "python-docx is not installed" or error.startswith("could not open this document")


# ============================================================ extract_worker.py: pagination (page/next_item)

def test_extract_worker_window_computes_next_item_correctly():
    assert ew._window(100, 1, 30) == (0, 30, 31)
    assert ew._window(100, 31, 30) == (30, 60, 61)
    assert ew._window(100, 91, 30) == (90, 100, None)
    assert ew._window(30, 1, 30) == (0, 30, None)


def test_extract_worker_main_refuses_when_extracted_text_is_only_whitespace():
    """extraction succeeding with nothing but whitespace (a scanned/encrypted doc, typically) must
    become a clean ok:false, never an empty ok:true the model would mistake for 'really empty'."""
    fake_path = tmp() / "f.pdf"
    fake_path.write_bytes(b"x")
    fake_extractors = {"pdf": lambda path, start, items, chars: ("   \n  ", None, None)}
    fake_argv = ["extract_worker.py", "--type", "pdf", "--path", str(fake_path),
                "--start-item", "1", "--max-items", "30", "--max-chars", "1000"]
    with mock.patch.object(ew, "EXTRACTORS", fake_extractors), mock.patch.object(sys, "argv", fake_argv):
        buf = io.StringIO()
        with quiet(buf):
            ew.main()
    payload = json.loads(buf.getvalue().strip())
    assert payload["ok"] is False and "scanned or encrypted" in payload["error"]


def test_extract_worker_pptx_shape_text_handles_text_table_group_and_chart():
    """Exercises the shape-text recursion with FAKE shape objects shaped like python-pptx's own
    (text_frame/table/chart/shapes attributes) - this logic takes the MSO_SHAPE_TYPE enum as a
    parameter specifically so it can be tested with controlled fakes, deterministically, whether
    or not python-pptx happens to be installed in this environment."""
    mso = SimpleNamespace(GROUP="GROUP")
    text_shape = SimpleNamespace(shape_type="TEXT", has_text_frame=True,
                                 text_frame=SimpleNamespace(text="hello"))
    table_shape = SimpleNamespace(
        shape_type="TABLE", has_text_frame=False, has_table=True,
        table=SimpleNamespace(rows=[SimpleNamespace(cells=[SimpleNamespace(text="a"), SimpleNamespace(text="b")])]))
    chart_shape = SimpleNamespace(shape_type="CHART", has_text_frame=False, has_table=False, has_chart=True)
    picture_shape = SimpleNamespace(shape_type="PICTURE", has_text_frame=False, has_table=False, has_chart=False)
    group_shape = SimpleNamespace(shape_type="GROUP", shapes=[text_shape, chart_shape])

    assert ew._pptx_shape_text(text_shape, mso) == ("hello", False)
    assert ew._pptx_shape_text(table_shape, mso) == ("a\tb", False)
    assert ew._pptx_shape_text(chart_shape, mso) == ("", True)
    assert ew._pptx_shape_text(picture_shape, mso) == ("", False)
    text, unsupported = ew._pptx_shape_text(group_shape, mso)
    assert text == "hello" and unsupported   # the group's chart child is still reported, not skipped


def test_extract_worker_docx_includes_tables_and_respects_the_paragraph_window():
    """Exercises extract_docx's windowing + table-formatting logic with a FAKE `docx` module
    injected into sys.modules - a controlled, deterministic double regardless of whether the real
    python-docx happens to be installed in this environment, while still going through the real
    zip-safety check first."""
    path = tmp() / "fake.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", "<xml/>")   # just needs to be a valid, safe zip

    fake_docx = types.ModuleType("docx")
    fake_docx.Document = lambda source: SimpleNamespace(
        paragraphs=[SimpleNamespace(text=f"p{i}") for i in range(5)],
        tables=[SimpleNamespace(rows=[SimpleNamespace(
            cells=[SimpleNamespace(text="a"), SimpleNamespace(text="b")])])],
    )
    with mock.patch.dict(sys.modules, {"docx": fake_docx}):
        text, next_item, error = ew.extract_docx(str(path), 1, 3, 10000)
        text2, next_item2, error2 = ew.extract_docx(str(path), 4, 3, 10000)
    assert error is None and error2 is None
    assert "p0" in text and "p1" in text and "p2" in text and "p3" not in text
    assert "[table 1]" in text and "a\tb" in text
    assert next_item == 4
    assert "p3" in text2 and "p4" in text2 and "[table" not in text2   # tables only on the first page
    assert next_item2 is None


def test_extract_uses_the_higher_item_cap_for_docx_not_pdf_or_pptx():
    root = tmp()
    for file_type in ("pdf", "docx", "pptx"):
        p = root / f"f.{file_type}"
        p.write_bytes(b"x")
        fake_result = SimpleNamespace(returncode=0,
                                      stdout=json.dumps({"ok": True, "text": "x", "next_item": None}))
        with mock.patch.object(fo.subprocess, "run", return_value=fake_result) as run:
            fo._extract(file_type, p, 1)
        called_args = run.call_args[0][0]
        max_items_value = called_args[called_args.index("--max-items") + 1]
        expected = str(fo.EXTRACT_MAX_PARAGRAPH_ITEMS if file_type == "docx" else fo.EXTRACT_MAX_ITEMS)
        assert max_items_value == expected, file_type
        assert called_args[called_args.index("--start-item") + 1] == "1"


def test_read_file_forwards_page_and_suggests_page_once_the_window_is_exhausted():
    root = tmp()
    p = root / "f.pdf"
    p.write_bytes(b"%PDF-1.4")
    scope = _FakeScope([_scope_entry(root)])
    with mock.patch.object(fo, "_extract", return_value=("short text", 31, None)) as m:
        result = fo.read_file(scope, str(p), offset=0, page=5)
    assert m.call_args[0][0] == "pdf" and m.call_args[0][2] == 5
    assert "short text" in result
    assert "call again with page=31" in result
    assert "more characters in this extraction" not in result


def test_read_file_suggests_offset_not_page_when_more_chars_remain_in_the_same_window():
    root = tmp()
    p = root / "f.pdf"
    p.write_bytes(b"%PDF-1.4")
    scope = _FakeScope([_scope_entry(root)])
    long_text = "x" * (fo.FILE_READ_MAX_CHARS + 100)
    with mock.patch.object(fo, "_extract", return_value=(long_text, 31, None)):
        result = fo.read_file(scope, str(p), offset=0, page=5)
    assert "more characters in this extraction" in result
    assert "page=31" not in result


# ============================================================ undo_log.py: schema validation

def test_undo_log_schema_validation_drops_every_malformed_entry_individually():
    work = tmp()
    good_target = work / "f.txt"
    good_target.write_text("x", encoding="utf-8")
    good_hash = ul.sha256_of(good_target)
    raw = [
        {"id": 1, "action": "create", "target_path": str(good_target), "post_hash": good_hash},       # valid
        {"id": 2, "action": "banana", "target_path": str(good_target), "post_hash": good_hash},        # bad action
        {"id": 3, "action": "create", "target_path": "relative.txt", "post_hash": good_hash},          # not absolute
        {"id": 4, "action": "create", "target_path": str(good_target), "original_path": "x",
         "post_hash": good_hash},                                                      # create can't have original_path
        {"id": 5, "action": "move", "target_path": str(good_target), "post_hash": good_hash},          # move needs one
        {"id": 6, "action": "create", "target_path": str(good_target), "post_hash": "short"},          # bad hash
        "not even a dict",
    ]
    path = work / "undo_log.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    log = ul.UndoLog(path=path, backups_dir=work / "backups")
    buf = io.StringIO()
    with quiet(buf):
        last = log.last()
    assert last["id"] == 1
    assert buf.getvalue().count("[undo_log]") == 6


def test_undo_log_rejects_a_backup_path_outside_backups_dir():
    work = tmp()
    good_target = work / "f.txt"
    good_target.write_text("x", encoding="utf-8")
    good_hash = ul.sha256_of(good_target)
    outside = tmp() / "not_a_backup.txt"
    outside.write_text("sneaky", encoding="utf-8")
    raw = [{"id": 1, "action": "edit", "target_path": str(good_target), "backup_path": str(outside),
            "post_hash": good_hash}]
    path = work / "undo_log.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    log = ul.UndoLog(path=path, backups_dir=work / "backups")
    buf = io.StringIO()
    with quiet(buf):
        last = log.last()
    assert last is None
    assert "[undo_log]" in buf.getvalue()
    assert outside.exists()   # never touched just for failing validation


def test_undo_log_rejects_a_backup_path_that_escapes_backups_dir_via_dot_dot():
    """A raw-string/normcase-only compare would wrongly accept this: the string literally STARTS
    WITH backups_dir's own text, even though ".." walks it right back out to the parent folder."""
    work = tmp()
    backups_dir = work / "backups"
    backups_dir.mkdir()
    good_target = work / "f.txt"
    good_target.write_text("x", encoding="utf-8")
    good_hash = ul.sha256_of(good_target)
    outside = work / "escaped.txt"
    outside.write_text("sneaky", encoding="utf-8")
    escaping_path = str(backups_dir / ".." / "escaped.txt")
    raw = [{"id": 1, "action": "edit", "target_path": str(good_target), "backup_path": escaping_path,
            "post_hash": good_hash}]
    path = work / "undo_log.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    log = ul.UndoLog(path=path, backups_dir=backups_dir)
    buf = io.StringIO()
    with quiet(buf):
        last = log.last()
    assert last is None
    assert "[undo_log]" in buf.getvalue()
    assert outside.exists()


def test_undo_log_rejects_uppercase_or_malformed_post_hash():
    work = tmp()
    good_target = work / "f.txt"
    good_target.write_text("x", encoding="utf-8")
    good_hash = ul.sha256_of(good_target)
    raw = [
        {"id": 1, "action": "create", "target_path": str(good_target), "post_hash": good_hash.upper()},
        {"id": 2, "action": "create", "target_path": str(good_target), "post_hash": "g" * 64},
    ]
    path = work / "undo_log.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    log = ul.UndoLog(path=path, backups_dir=work / "backups")
    with quiet(io.StringIO()):
        assert log.last() is None


def test_undo_log_never_unlinks_a_pruned_backup_outside_backups_dir():
    """Defense in depth for the pruning path itself - the public record()/load() paths can't
    actually produce a backup_path outside backups_dir (new_backup_path() always stays inside it,
    and _validate_entry refuses one on load), so this seeds one directly in memory to exercise the
    unlink guard on its own."""
    work = tmp()
    log = ul.UndoLog(path=work / "undo_log.json", backups_dir=work / "backups")
    outside = tmp() / "innocent.txt"
    outside.write_text("do not delete me", encoding="utf-8")
    with quiet(io.StringIO()):
        log._ensure_loaded()
    log._entries = [{"id": 1, "at": 0, "action": "create", "target_path": str(work / "x.txt"),
                     "original_path": None, "backup_path": str(outside), "post_hash": "a" * 64}]
    log._next_id = 2
    target = work / "y.txt"
    target.write_text("y", encoding="utf-8")
    with mock.patch.object(ul, "UNDO_LOG_MAX_ITEMS", 1):
        log.record("create", target, post_hash=ul.sha256_of(target))
    assert outside.exists()


def test_undo_log_never_unlinks_a_pruned_backup_that_escapes_via_dot_dot():
    """Same ".." escape as the load-time test above, but exercised against the prune-time unlink
    guard directly (seeded in memory - the public record()/load() paths can't produce this)."""
    work = tmp()
    backups_dir = work / "backups"
    backups_dir.mkdir()
    log = ul.UndoLog(path=work / "undo_log.json", backups_dir=backups_dir)
    outside = work / "escaped.txt"
    outside.write_text("do not delete me", encoding="utf-8")
    escaping_path = str(backups_dir / ".." / "escaped.txt")
    with quiet(io.StringIO()):
        log._ensure_loaded()
    log._entries = [{"id": 1, "at": 0, "action": "create", "target_path": str(work / "x.txt"),
                     "original_path": None, "backup_path": escaping_path, "post_hash": "a" * 64}]
    log._next_id = 2
    target = work / "y.txt"
    target.write_text("y", encoding="utf-8")
    with mock.patch.object(ul, "UNDO_LOG_MAX_ITEMS", 1):
        log.record("create", target, post_hash=ul.sha256_of(target))
    assert outside.exists()


def test_corrupt_undo_log_warns_and_resets_without_crashing():
    work = tmp()
    path = work / "undo_log.json"
    path.write_text("{not a list", encoding="utf-8")
    log = ul.UndoLog(path=path, backups_dir=work / "backups")
    buf = io.StringIO()
    with quiet(buf):
        assert log.last() is None
    assert "[undo_log]" in buf.getvalue()


def test_jarvis_testing_refuses_the_real_undo_log_file():
    log = ul.UndoLog()   # no path override - points at the REAL C:\jarvis_workspace\undo_log.json
    try:
        log.last()
        raise AssertionError("should have refused")
    except RuntimeError as error:
        assert "JARVIS_TESTING" in str(error)


def test_old_backups_are_pruned_past_the_undo_log_item_cap():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "a.md"
    target.write_text("v0", encoding="utf-8")
    for i in range(ul.UNDO_LOG_MAX_ITEMS + 3):
        ok, item_input, _ = fo.prep_edit_file(
            scope, {"path": str(target), "old_text": target.read_text(encoding="utf-8"), "new_text": f"v{i + 1}"})
        assert ok
        fo.do_edit_file(scope, undo, item_input)
    assert len(list((log_dir / "backups").iterdir())) <= ul.UNDO_LOG_MAX_ITEMS


# ============================================================ "undo last file change" (undo_last_file_change)

def test_undo_create_deletes_only_if_hash_still_matches():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "a.txt"
    ok, item_input, _ = fo.prep_create_file(scope, {"path": str(target), "content": "hi"})
    fo.do_create_file(scope, undo, item_input)
    assert "Undone" in fo.undo_last_file_change(scope, undo)
    assert not target.exists()
    assert undo.last() is None


def test_undo_create_refuses_and_does_not_pop_if_file_changed_since():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "a.txt"
    ok, item_input, _ = fo.prep_create_file(scope, {"path": str(target), "content": "hi"})
    fo.do_create_file(scope, undo, item_input)
    target.write_text("someone edited me", encoding="utf-8")
    result = fo.undo_last_file_change(scope, undo)
    assert "changed since" in result and target.exists()
    assert undo.last() is not None


def test_undo_edit_restores_only_if_hash_still_matches():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "a.md"
    target.write_text("before", encoding="utf-8")
    ok, item_input, _ = fo.prep_edit_file(scope, {"path": str(target), "old_text": "before", "new_text": "after"})
    fo.do_edit_file(scope, undo, item_input)
    assert target.read_text(encoding="utf-8") == "after"
    assert "Undone" in fo.undo_last_file_change(scope, undo)
    assert target.read_text(encoding="utf-8") == "before"


def test_undo_edit_refuses_if_file_changed_since_that_edit():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    target = root / "a.md"
    target.write_text("before", encoding="utf-8")
    ok, item_input, _ = fo.prep_edit_file(scope, {"path": str(target), "old_text": "before", "new_text": "after"})
    fo.do_edit_file(scope, undo, item_input)
    target.write_text("something else", encoding="utf-8")
    assert "changed since" in fo.undo_last_file_change(scope, undo)


def test_undo_move_moves_back_only_if_hash_still_matches():
    src_root, dst_root = tmp(), tmp()
    scope = _FakeScope([_scope_entry(src_root, mode="read_write"), _scope_entry(dst_root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    source = src_root / "a.txt"
    source.write_text("hi", encoding="utf-8")
    dest = dst_root / "a.txt"
    ok, item_input, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(dest)})
    fo.do_move_file(scope, undo, item_input)
    assert "Undone" in fo.undo_last_file_change(scope, undo)
    assert source.read_text(encoding="utf-8") == "hi" and not dest.exists()


def test_undo_move_never_overwrites_the_original_path():
    src_root, dst_root = tmp(), tmp()
    scope = _FakeScope([_scope_entry(src_root, mode="read_write"), _scope_entry(dst_root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    source = src_root / "a.txt"
    source.write_text("hi", encoding="utf-8")
    dest = dst_root / "a.txt"
    ok, item_input, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(dest)})
    fo.do_move_file(scope, undo, item_input)
    source.write_text("something new is here now", encoding="utf-8")   # original path occupied again
    result = fo.undo_last_file_change(scope, undo)
    assert "already exists" in result
    assert dest.exists()
    assert source.read_text(encoding="utf-8") == "something new is here now"


def test_undoing_twice_in_a_row_is_a_clean_no_op():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    assert "Nothing to undo" in fo.undo_last_file_change(scope, undo)


def test_repeated_undo_calls_walk_back_one_entry_at_a_time_each_hash_checked():
    """"undo last file change" is whole-message and stateless: it always reverses only the entry
    that is CURRENTLY newest. Saying it again after a successful undo reverses whatever is newest
    NOW (one entry further back), each one independently hash-checked on its own - there is no
    "undo everything" and no multi-step undo in one call."""
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(path=log_dir / "undo_log.json", backups_dir=log_dir / "backups")
    ok1, item1, _ = fo.prep_create_file(scope, {"path": str(root / "a.txt"), "content": "a"})
    fo.do_create_file(scope, undo, item1)
    ok2, item2, _ = fo.prep_create_file(scope, {"path": str(root / "b.txt"), "content": "b"})
    fo.do_create_file(scope, undo, item2)
    assert "Undone" in fo.undo_last_file_change(scope, undo)   # 1st call: undoes "b" only
    assert not (root / "b.txt").exists()
    assert (root / "a.txt").exists()
    assert "Undone" in fo.undo_last_file_change(scope, undo)   # 2nd call: now undoes "a"
    assert not (root / "a.txt").exists()


# ============================================================ jarvis_core.py wiring / ActionGuard

def test_detect_undo_file_change_command_whole_message_only():
    assert jc.detect_undo_file_change_command("undo last file change") is True
    assert jc.detect_undo_file_change_command("please undo last file change now") is False
    assert jc.detect_undo_file_change_command("undo last study entry") is False


def test_new_tool_schemas_have_the_expected_required_fields():
    tools = {t["name"]: t for t in jc.TOOLS}
    assert tools["list_files"]["input_schema"]["required"] == ["path"]
    assert tools["read_file"]["input_schema"]["required"] == ["path"]
    assert set(tools["create_file"]["input_schema"]["required"]) == {"path", "content"}
    assert set(tools["edit_file"]["input_schema"]["required"]) == {"path", "old_text", "new_text"}
    assert set(tools["move_file"]["input_schema"]["required"]) == {"path", "new_path"}


def test_create_file_is_always_held_even_in_a_clean_round():
    work = tmp()
    cl = fresh_server(work, [
        R("tool_use", [TU("c1", "create_file", {"path": str(work / "rw" / "notes.md"), "content": "hi"})]),
        R("end_turn", [T("Needs your yes.")]),
    ])
    write_autonomy_scope([{"path": str(work / "rw"), "mode": "read_write"}])
    with quiet(io.StringIO()):
        r = post_chat("create a note").get_json()
    assert "Pendente / Pending" in r["reply"]
    assert len(jc._guard.pending) == 1
    assert not (work / "rw" / "notes.md").exists()

    cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("Done.")])]
    with quiet(io.StringIO()):
        r2 = post_chat("yes").get_json()
    assert r2["reply"] == "Done." and len(jc._guard.pending) == 0
    assert (work / "rw" / "notes.md").read_text(encoding="utf-8") == "hi"


def test_create_file_refuses_against_read_only_folder_never_held():
    work = tmp()
    fresh_server(work, [
        R("tool_use", [TU("c1", "create_file", {"path": str(work / "ro" / "notes.md"), "content": "hi"})]),
        R("end_turn", [T("Can't do that.")]),
    ])
    write_autonomy_scope([{"path": str(work / "ro"), "mode": "read_only"}])
    with quiet(io.StringIO()):
        post_chat("create a note")
    assert len(jc._guard.pending) == 0
    assert not (work / "ro" / "notes.md").exists()


def test_create_file_confirm_time_revalidates_scope_not_the_hold_time_snapshot():
    """Mirrors test_open_app_executor_revalidates_at_confirm_time: the scope may have changed
    between hold and confirm, so execute() must re-check it, not trust what prep() saw."""
    work = tmp()
    cl = fresh_server(work, [
        R("tool_use", [TU("c1", "create_file", {"path": str(work / "rw" / "notes.md"), "content": "hi"})]),
        R("end_turn", [T("Needs your yes.")]),
    ])
    write_autonomy_scope([{"path": str(work / "rw"), "mode": "read_write"}])
    with quiet(io.StringIO()):
        post_chat("create a note")
    assert len(jc._guard.pending) == 1

    write_autonomy_scope([{"path": str(work / "rw"), "mode": "read_only"}])   # changed since it was held
    cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("ok")])]
    with quiet(io.StringIO()):
        post_chat("yes")
    assert not (work / "rw" / "notes.md").exists()


def test_undo_last_file_change_command_via_chat_makes_no_api_call():
    work = tmp()
    cl = fresh_server(work, [])
    write_autonomy_scope([{"path": str(work / "rw"), "mode": "read_write"}])
    with quiet(io.StringIO()):
        r = post_chat("undo last file change").get_json()
    assert "nothing" in r["reply"].lower()
    assert cl.sent == []


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])

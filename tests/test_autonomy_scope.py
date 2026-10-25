"""
Stage A of the file-autonomy feature: autonomy_scope.py (scope resolution, Windows path
hardening, the deny-list, PROTECTED_PATHS enforcement), file_ops.py (list/read/create/edit/move,
extraction, OneDrive placeholders, size caps), extract_worker.py's zip-bomb hardening, and
undo_log.py's in-memory record/list/remove primitives (list/pick, hash-verified undo behavior
itself lives in tests/test_file_undo.py, alongside the rest of the redesigned undo commands).

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
    R, SR, SU, T, TU, al, fresh_server, isolate, jc, post_chat, run_module_tests, write_autonomy_scope,
    write_whitelist,
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
    undo = ul.UndoLog(backups_dir=log_dir / "backups")
    target = root / "notes.md"
    ok, item_input, display = fo.prep_create_file(scope, {"path": str(target), "content": "hello"})
    assert ok and "create" in display
    assert fo.do_create_file(scope, undo, item_input) == f"Created {target.resolve()}."
    assert target.read_text(encoding="utf-8") == "hello"
    assert undo.recent(1)[0]["action"] == "create"


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
    undo = ul.UndoLog(backups_dir=log_dir / "backups")
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
    undo = ul.UndoLog(backups_dir=log_dir / "backups")
    target = root / "notes.md"
    target.write_text("line one\nline two\nline three\n", encoding="utf-8")
    ok, item_input, display = fo.prep_edit_file(
        scope, {"path": str(target), "old_text": "line two", "new_text": "line TWO"})
    assert ok and "+1/-1" in display
    assert fo.do_edit_file(scope, undo, item_input) == f"Edited {target.resolve()}."
    assert target.read_text(encoding="utf-8") == "line one\nline TWO\nline three\n"
    entry = undo.recent(1)[0]
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
    undo = ul.UndoLog(backups_dir=log_dir / "backups")
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
    undo = ul.UndoLog(backups_dir=log_dir / "backups")
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
    undo = ul.UndoLog(backups_dir=log_dir / "backups")
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
    undo = ul.UndoLog(backups_dir=log_dir / "backups")
    source = src_root / "notes.txt"
    source.write_text("hi", encoding="utf-8")
    dest = dst_root / "notes.txt"
    ok, item_input, _ = fo.prep_move_file(scope, {"path": str(source), "new_path": str(dest)})
    assert ok
    assert "Moved" in fo.do_move_file(scope, undo, item_input)
    assert not source.exists() and dest.read_text(encoding="utf-8") == "hi"
    entry = undo.recent(1)[0]
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


# ============================================================ undo_log.py: in-memory list/remove

def test_undo_log_recent_returns_newest_first_and_no_more_than_asked():
    work = tmp()
    log = ul.UndoLog(backups_dir=work / "backups")
    for i in range(3):
        target = work / f"f{i}.txt"
        target.write_text("x", encoding="utf-8")
        log.record("create", target, post_hash=ul.sha256_of(target))
    recent = log.recent(2)
    assert [e["target_path"] for e in recent] == [str(work / "f2.txt"), str(work / "f1.txt")]


def test_undo_log_all_entries_returns_everything_newest_first():
    work = tmp()
    log = ul.UndoLog(backups_dir=work / "backups")
    for i in range(3):
        target = work / f"f{i}.txt"
        target.write_text("x", encoding="utf-8")
        log.record("create", target, post_hash=ul.sha256_of(target))
    assert [e["target_path"] for e in log.all_entries()] == [
        str(work / "f2.txt"), str(work / "f1.txt"), str(work / "f0.txt")]


def test_undo_log_remove_entry_removes_only_the_named_id_regardless_of_position():
    work = tmp()
    log = ul.UndoLog(backups_dir=work / "backups")
    ids = []
    for i in range(3):
        target = work / f"f{i}.txt"
        target.write_text("x", encoding="utf-8")
        log.record("create", target, post_hash=ul.sha256_of(target))
        ids.append(log.all_entries()[0]["id"])
    middle_id = ids[1]
    assert log.remove_entry(middle_id) is True
    remaining_ids = [e["id"] for e in log.all_entries()]
    assert middle_id not in remaining_ids and len(remaining_ids) == 2


def test_undo_log_remove_entry_unknown_id_is_a_clean_no_op():
    work = tmp()
    log = ul.UndoLog(backups_dir=work / "backups")
    target = work / "f.txt"
    target.write_text("x", encoding="utf-8")
    log.record("create", target, post_hash=ul.sha256_of(target))
    assert log.remove_entry(999999) is False
    assert len(log.all_entries()) == 1


def test_undo_log_is_never_persisted_to_disk():
    """In-memory only, by design - nothing about a file change survives a restart. A fresh
    UndoLog + record()/recent() round trip must never create undo_log.json anywhere near it (the
    temp work dir this test owns), unlike the old disk-backed version."""
    work = tmp()
    log = ul.UndoLog(backups_dir=work / "backups")
    target = work / "f.txt"
    target.write_text("x", encoding="utf-8")
    log.record("create", target, post_hash=ul.sha256_of(target))
    assert log.recent(1)[0]["action"] == "create"
    assert not (work / "undo_log.json").exists()


def test_old_backups_are_pruned_past_the_undo_log_item_cap():
    root = tmp()
    scope = _FakeScope([_scope_entry(root, mode="read_write")])
    log_dir = tmp()
    undo = ul.UndoLog(backups_dir=log_dir / "backups")
    target = root / "a.md"
    target.write_text("v0", encoding="utf-8")
    for i in range(ul.UNDO_LOG_MAX_ITEMS + 3):
        ok, item_input, _ = fo.prep_edit_file(
            scope, {"path": str(target), "old_text": target.read_text(encoding="utf-8"), "new_text": f"v{i + 1}"})
        assert ok
        fo.do_edit_file(scope, undo, item_input)
    assert len(list((log_dir / "backups").iterdir())) <= ul.UNDO_LOG_MAX_ITEMS


# ============================================================ jarvis_core.py wiring / ActionGuard
# (file-undo behavior itself - detection, the choice state, undo_entry - now lives in
# tests/test_file_undo.py, alongside the rest of the redesigned undo commands)

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


# ============================================================ test-helper safety nets
# These simulate "isolate() was never called" by resetting just the one path attribute a moment
# before calling the helper under test - never actually writing anything, since the guard must
# raise BEFORE either helper's first write. The real attribute is restored immediately after,
# inside a finally, so no later test in this file is affected.

def test_write_autonomy_scope_refuses_the_real_config_file_path():
    work = tmp()
    isolate(work)
    real_path = jc._autonomy_scope._path
    jc._autonomy_scope._path = asc.CONFIG_FILE   # simulate a forgotten isolate() call
    try:
        try:
            write_autonomy_scope([{"path": str(work), "mode": "read_write"}])
            raise AssertionError("should have refused")
        except RuntimeError as error:
            assert "write_autonomy_scope" in str(error)
    finally:
        jc._autonomy_scope._path = real_path


def test_write_autonomy_scope_refuses_a_path_outside_the_temp_directory():
    work = tmp()
    isolate(work)
    real_path = jc._autonomy_scope._path
    jc._autonomy_scope._path = Path(r"C:\not_a_real_temp_dir\autonomy_scope.json")
    try:
        try:
            write_autonomy_scope([{"path": str(work), "mode": "read_write"}])
            raise AssertionError("should have refused")
        except RuntimeError:
            pass
    finally:
        jc._autonomy_scope._path = real_path


def test_write_whitelist_refuses_the_real_app_whitelist_path():
    work = tmp()
    isolate(work)
    real_file = al.WHITELIST_FILE
    al.WHITELIST_FILE = al._BASE_DIR / "app_whitelist.json"   # simulate a forgotten isolate() call
    try:
        try:
            write_whitelist({})
            raise AssertionError("should have refused")
        except RuntimeError as error:
            assert "write_whitelist" in str(error)
    finally:
        al.WHITELIST_FILE = real_file


def test_write_whitelist_refuses_a_path_outside_the_temp_directory():
    work = tmp()
    isolate(work)
    real_file = al.WHITELIST_FILE
    al.WHITELIST_FILE = Path(r"C:\not_a_real_temp_dir\app_whitelist.json")
    try:
        try:
            write_whitelist({})
            raise AssertionError("should have refused")
        except RuntimeError:
            pass
    finally:
        al.WHITELIST_FILE = real_file


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])

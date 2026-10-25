"""
The redesigned "undo last file change" commands: the bare list-and-pick flow, the direct named
command, the ~2-minute choice state shared by both, and file_ops.undo_entry's own safety (hash
re-check, no trash - create-undo deletes, edit-undo restores the backup, move-undo refuses rather
than overwrite). Everything here is a fixed, whole-message, NON-model command - no ActionGuard/
Stage B involvement at all (see jarvis_core.py's _UndoChoiceState).

Every test is offline and self-contained: no real Anthropic call, no real folder on the
developer's machine - everything lives under a fresh tempfile.mkdtemp() per test, same convention
as tests/test_autonomy_scope.py. undo_log.py is in-memory only (see test_autonomy_scope.py's own
test_undo_log_* tests for that guarantee directly) - nothing here touches disk for the log itself,
only for the real files/backups being created/edited/moved.

EVERY test that calls write_autonomy_scope (directly, or through _make_create/_make_edit/
_make_move below) calls isolate()/fresh_server() FIRST, in that same test function - never rely
on whatever a previous test happened to leave behind. write_autonomy_scope() itself also refuses
outright if ever called before isolate() repoints jc._autonomy_scope at a temp path (see
_helpers.py) - this file follows that rule, it doesn't lean on the refusal as its real safety net.

Run directly (`python tests/test_file_undo.py`) or via `python run_tests.py`.
"""
import contextlib
import io
import tempfile
from pathlib import Path

from _helpers import (
    R, SU, SR, T, TU, fresh_server, isolate, jc, post_chat, run_module_tests, write_autonomy_scope,
)

import file_ops as fo  # noqa: E402 - after _helpers, which puts the project root on sys.path

quiet = contextlib.redirect_stdout


def tmp():
    return Path(tempfile.mkdtemp())


def _make_create(work, relpath, content="x"):
    """Creates relpath under work (a read_write scope) directly through file_ops, logging one
    "create" entry in jc._undo_log - the same object the undo commands under test read. Caller
    must have already called isolate(work) (or fresh_server) in this same test."""
    write_autonomy_scope([{"path": str(work), "mode": "read_write"}])
    target = work / relpath
    ok, item_input, _ = fo.prep_create_file(jc._autonomy_scope, {"path": str(target), "content": content})
    assert ok, item_input
    result = fo.do_create_file(jc._autonomy_scope, jc._undo_log, item_input)
    assert "Created" in result, result
    return target.resolve()


def _make_edit(target, old_text, new_text):
    ok, item_input, _ = fo.prep_edit_file(jc._autonomy_scope, {"path": str(target), "old_text": old_text, "new_text": new_text})
    assert ok, item_input
    result = fo.do_edit_file(jc._autonomy_scope, jc._undo_log, item_input)
    assert "Edited" in result, result


def _make_move(source, dest):
    ok, item_input, _ = fo.prep_move_file(jc._autonomy_scope, {"path": str(source), "new_path": str(dest)})
    assert ok, item_input
    result = fo.do_move_file(jc._autonomy_scope, jc._undo_log, item_input)
    assert "Moved" in result, result


# ============================================================ detect_undo_file_change_named_command
# Pure functions, no state touched - no isolate() needed.

def test_detect_undo_file_change_command_whole_message_only():
    assert jc.detect_undo_file_change_command("undo last file change") is True
    assert jc.detect_undo_file_change_command("please undo last file change now") is False
    assert jc.detect_undo_file_change_command("undo last study entry") is False


def test_detect_undo_file_change_named_command_returns_the_trailing_filename():
    assert jc.detect_undo_file_change_named_command("undo last file change notes.md") == "notes.md"
    assert jc.detect_undo_file_change_named_command("Undo Last File Change Notes.md") == "Notes.md"
    assert jc.detect_undo_file_change_named_command("undo last file change") is None   # bare - no filename
    assert jc.detect_undo_file_change_named_command("undo last file change   ") is None  # only whitespace follows
    assert jc.detect_undo_file_change_named_command("undo last study entry") is None


# ============================================================ bare command: list, change nothing, open choice

def test_bare_command_lists_up_to_five_and_changes_nothing():
    work = tmp()
    isolate(work)
    paths = [_make_create(work, f"f{i}.md") for i in range(7)]
    reply = jc.handle_undo_file_change_command()
    assert reply.count(".md") >= 5   # at least the 5 listed target paths appear
    for p in paths:
        assert p.exists()   # bare command never touches any file
    assert jc._undo_choice.is_open()
    assert len(jc._undo_choice.entries) == 5


def test_bare_command_on_an_empty_log_says_so_and_opens_nothing():
    fresh_server(tmp())
    assert "Nothing recorded to undo." == jc.handle_undo_file_change_command()
    assert not jc._undo_choice.is_open()


# ============================================================ resolving the choice: name, "undo N", bare number

def test_exact_file_name_in_her_next_message_undoes_just_that_entry():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")
    b = _make_create(work, "b.md")
    jc.handle_undo_file_change_command()
    result = jc.maybe_resolve_pending_file_undo_choice("b.md")
    assert "Undone" in result and not b.exists() and a.exists()
    assert not jc._undo_choice.is_open()


def test_file_name_match_is_case_insensitive():
    work = tmp()
    isolate(work)
    a = _make_create(work, "Notes.MD")
    jc.handle_undo_file_change_command()
    result = jc.maybe_resolve_pending_file_undo_choice("notes.md")
    assert "Undone" in result and not a.exists()


def test_undo_n_undoes_entry_n_of_the_open_list():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")   # oldest
    b = _make_create(work, "b.md")   # newest
    jc.handle_undo_file_change_command()   # list: 1=b (newest), 2=a
    result = jc.maybe_resolve_pending_file_undo_choice("undo 2")
    assert "Undone" in result and not a.exists() and b.exists()


def test_bare_number_alone_does_not_resolve():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")
    jc.handle_undo_file_change_command()
    result = jc.maybe_resolve_pending_file_undo_choice("1")
    assert result is None   # "any other message" - not resolved
    assert not jc._undo_choice.is_open()   # but the choice is still cleared
    assert a.exists()   # and nothing changed


def test_any_unrelated_message_clears_the_choice_without_changing_anything():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")
    jc.handle_undo_file_change_command()
    assert jc.maybe_resolve_pending_file_undo_choice("what's the weather like") is None
    assert not jc._undo_choice.is_open()
    assert a.exists()
    # The name that WOULD have resolved a moment ago no longer does - the choice is gone.
    assert jc.maybe_resolve_pending_file_undo_choice("a.md") is None
    assert a.exists()


def test_choice_expires_after_the_timeout():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")
    jc.handle_undo_file_change_command()
    jc._undo_choice.at -= jc.UNDO_CHOICE_TIMEOUT_SECONDS + 1
    assert jc.maybe_resolve_pending_file_undo_choice("a.md") is None
    assert a.exists()


def test_quiz_active_clears_the_choice_instead_of_colliding_with_grading():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")
    jc.handle_undo_file_change_command()
    jc._quiz.active = True
    try:
        assert jc.maybe_resolve_pending_file_undo_choice("a.md") is None
        assert not jc._undo_choice.is_open()
        assert a.exists()
    finally:
        jc._quiz.active = False


def test_maybe_resolve_returns_none_when_nothing_is_open():
    fresh_server(tmp())
    assert jc.maybe_resolve_pending_file_undo_choice("anything at all") is None


# ============================================================ named command: zero / one / many matches

def test_named_command_zero_matches_changes_nothing():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")
    result = jc.handle_undo_file_change_named_command("nope.md")
    assert "No recorded file change matches" in result
    assert a.exists()
    assert not jc._undo_choice.is_open()


def test_named_command_exactly_one_match_undoes_immediately_no_confirmation():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")
    result = jc.handle_undo_file_change_named_command("a.md")
    assert "Undone" in result and not a.exists()
    assert not jc._undo_choice.is_open()


def test_named_command_more_than_one_match_changes_nothing_and_lists_them():
    """Two DIFFERENT folders can both end up with a file named the same thing."""
    work_a, work_b = tmp(), tmp()
    isolate(work_a)
    write_autonomy_scope([{"path": str(work_a), "mode": "read_write"},
                          {"path": str(work_b), "mode": "read_write"}])
    target_a = work_a / "dup.md"
    ok, item_input, _ = fo.prep_create_file(jc._autonomy_scope, {"path": str(target_a), "content": "a"})
    fo.do_create_file(jc._autonomy_scope, jc._undo_log, item_input)
    target_b = work_b / "dup.md"
    ok, item_input, _ = fo.prep_create_file(jc._autonomy_scope, {"path": str(target_b), "content": "b"})
    fo.do_create_file(jc._autonomy_scope, jc._undo_log, item_input)

    result = jc.handle_undo_file_change_named_command("dup.md")
    assert "More than one matches" in result
    assert target_a.exists() and target_b.exists()   # nothing changed
    assert jc._undo_choice.is_open() and len(jc._undo_choice.entries) == 2

    resolved = jc.maybe_resolve_pending_file_undo_choice("undo 1")
    assert "Undone" in resolved


def test_named_command_searches_every_entry_not_just_the_last_five():
    work = tmp()
    isolate(work)
    old = _make_create(work, "old.md")
    for i in range(5):
        _make_create(work, f"filler{i}.md")   # pushes "old.md" out of the bare command's top-5
    result = jc.handle_undo_file_change_named_command("old.md")
    assert "Undone" in result and not old.exists()


# ============================================================ regression: targeting is always explicit

def test_named_undo_only_ever_affects_the_named_entry():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")
    b = _make_create(work, "b.md")
    c = _make_create(work, "c.md")
    result = jc.handle_undo_file_change_named_command("b.md")
    assert "Undone" in result
    assert a.exists() and c.exists() and not b.exists()
    remaining = [Path(e["target_path"]).name for e in jc._undo_log.all_entries()]
    assert remaining == ["c.md", "a.md"]   # b's entry removed, a and c untouched, order preserved


# ============================================================ undo_entry: hash-mismatch refusals (no trash)

def test_undo_create_really_deletes_when_hash_still_matches():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")
    entry = jc._undo_log.all_entries()[0]
    result = fo.undo_entry(jc._autonomy_scope, jc._undo_log, entry)
    assert "Undone: removed" in result and not a.exists()
    assert jc._undo_log.all_entries() == []


def test_undo_create_refuses_and_leaves_the_entry_if_file_changed_since():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md")
    entry = jc._undo_log.all_entries()[0]
    a.write_text("someone edited me", encoding="utf-8")
    result = fo.undo_entry(jc._autonomy_scope, jc._undo_log, entry)
    assert "changed since" in result and a.exists()
    assert len(jc._undo_log.all_entries()) == 1


def test_undo_edit_restores_the_backup_overwriting_the_current_version():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md", content="before")
    _make_edit(a, "before", "after")
    assert a.read_text(encoding="utf-8") == "after"
    entry = jc._undo_log.all_entries()[0]
    assert entry["action"] == "edit"
    result = fo.undo_entry(jc._autonomy_scope, jc._undo_log, entry)
    assert "Undone: restored" in result
    assert a.read_text(encoding="utf-8") == "before"   # really overwritten - no trash copy kept


def test_undo_edit_refuses_and_leaves_the_entry_if_file_changed_since():
    work = tmp()
    isolate(work)
    a = _make_create(work, "a.md", content="before")
    _make_edit(a, "before", "after")
    entry = jc._undo_log.all_entries()[0]
    a.write_text("something else entirely", encoding="utf-8")
    result = fo.undo_entry(jc._autonomy_scope, jc._undo_log, entry)
    assert "changed since" in result
    # Only the edit entry is in the log here (the create before it was never logged - a.md was
    # made by _make_create, which DOES log a create too, so two entries exist; only the edit's
    # refusal is under test, so just confirm it specifically is still present).
    assert any(e["id"] == entry["id"] for e in jc._undo_log.all_entries())


def test_undo_move_moves_back_when_nothing_is_in_the_way():
    src, dst = tmp(), tmp()
    isolate(src)
    write_autonomy_scope([{"path": str(src), "mode": "read_write"}, {"path": str(dst), "mode": "read_write"}])
    source = src / "a.md"
    source.write_text("hi", encoding="utf-8")   # created directly - the move is the only logged entry
    dest = dst / "a.md"
    _make_move(source, dest)
    entry = jc._undo_log.all_entries()[0]
    result = fo.undo_entry(jc._autonomy_scope, jc._undo_log, entry)
    assert "Undone: moved" in result
    assert source.exists() and not dest.exists()


def test_undo_move_never_overwrites_the_original_path():
    src, dst = tmp(), tmp()
    isolate(src)
    write_autonomy_scope([{"path": str(src), "mode": "read_write"}, {"path": str(dst), "mode": "read_write"}])
    source = src / "a.md"
    source.write_text("hi", encoding="utf-8")
    dest = dst / "a.md"
    _make_move(source, dest)
    entry = jc._undo_log.all_entries()[0]
    source.write_text("something new is here now", encoding="utf-8")   # original path occupied again
    result = fo.undo_entry(jc._autonomy_scope, jc._undo_log, entry)
    assert "already exists" in result
    assert dest.exists() and source.read_text(encoding="utf-8") == "something new is here now"
    assert any(e["id"] == entry["id"] for e in jc._undo_log.all_entries())


# ============================================================ via /chat (fixed, no API call)

def test_undo_commands_via_chat_make_no_api_call():
    work = tmp()
    cl = fresh_server(work, [])
    a = _make_create(work, "a.md")
    with quiet(io.StringIO()):
        r1 = post_chat("undo last file change").get_json()
    assert "a.md" in r1["reply"] and a.exists()
    with quiet(io.StringIO()):
        r2 = post_chat("a.md").get_json()
    assert "Undone" in r2["reply"] and not a.exists()
    assert cl.sent == []


def test_named_undo_command_via_chat_makes_no_api_call():
    work = tmp()
    cl = fresh_server(work, [])
    a = _make_create(work, "a.md")
    with quiet(io.StringIO()):
        r = post_chat("undo last file change a.md").get_json()
    assert "Undone" in r["reply"] and not a.exists()
    assert cl.sent == []


if __name__ == "__main__":
    import sys
    run_module_tests(sys.modules[__name__])

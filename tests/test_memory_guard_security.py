"""
Security-relevant behaviour of the (generalized) memory/action guard, rebuilt here as its own
file because this is the part that matters if it silently regresses: taint timing, expiry, the
"yes" vocabulary, a fabricated confirm argument being ignored, rollback on a failed API call,
update_notes's backup, and mode-command detection (including the negative cases that must NOT
activate anything). Offline, no real path/personal data - see _helpers.py.
"""
import contextlib
import io
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import anthropic
import httpx

from _helpers import R, SR, SU, T, TU, fresh_server, jc, notes_text, post_chat, run_module_tests

quiet = contextlib.redirect_stdout


def tmp():
    return Path(tempfile.mkdtemp())


def api_error(message="simulated API failure"):
    return anthropic.APIError(message, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"), body=None)


# ============================================================ taint timing

def test_taint_applies_to_the_message_right_after_untrusted_content():
    """A write is held in the SAME message that used untrusted content, AND in the very next one
    (TAINT_FOLLOWING_MESSAGES = 1) - but NOT in the message after that."""
    cl = fresh_server(tmp(), [R("tool_use", [SU("s"), SR("s")]), R("end_turn", [T("Found it.")])])
    with quiet(io.StringIO()):
        post_chat("search something")   # round 1: tainted

    cl.script = [R("tool_use", [TU("r", "remember", {"category": "personal", "note": "note B"})]), R("end_turn", [T("ok")])]
    with quiet(io.StringIO()):
        post_chat("remember B")   # round 2: still held (the message right after taint)
    assert len(jc._guard.pending) == 1 and "note B" not in notes_text()

    cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("saved")])]
    with quiet(io.StringIO()):
        post_chat("yes")   # round 3: confirms round 2's hold
    assert "note B" in notes_text()

    cl.script = [R("tool_use", [TU("r", "remember", {"category": "personal", "note": "note C"})]), R("end_turn", [T("ok")])]
    with quiet(io.StringIO()):
        post_chat("remember C")   # round 4: two messages after the search - clean, saved directly
    assert "note C" in notes_text() and len(jc._guard.pending) == 0


# ============================================================ expiry

def test_pending_expires_one_message_later():
    """A held write can only be confirmed in the VERY NEXT message - one more message and it's
    gone, even with a clear "yes" (begin_round() only keeps round-1 items)."""
    cl = fresh_server(tmp(), [R("tool_use", [SU("s"), SR("s"), TU("r", "remember", {"category": "personal", "note": "note D"})]),
                             R("end_turn", [T("Need yes.")])])
    with quiet(io.StringIO()):
        post_chat("(web_search) remember D")
    assert len(jc._guard.pending) == 1

    cl.script = [R("end_turn", [T("Sure, tell me more.")])]
    with quiet(io.StringIO()):
        post_chat("actually let me think about it")   # an unrelated message - still "the next one"...
    assert len(jc._guard.pending) == 1   # ...so the hold is still technically confirmable THIS round

    cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("ok")])]
    with quiet(io.StringIO()):
        post_chat("yes")   # one message too late now - begin_round() already dropped it before this ran
    assert "note D" not in notes_text() and len(jc._guard.pending) == 0


def test_pending_expires_after_600_seconds():
    """PENDING_MAX_AGE_SECONDS: even the very next message doesn't save it if too much real time
    passed (e.g. she stepped away for 10+ minutes between the hold and her "yes")."""
    assert jc.PENDING_MAX_AGE_SECONDS == 600
    cl = fresh_server(tmp(), [R("tool_use", [SU("s"), SR("s"), TU("r", "remember", {"category": "personal", "note": "note E"})]),
                             R("end_turn", [T("Need yes.")])])
    with quiet(io.StringIO()):
        post_chat("(web_search) remember E")
    assert len(jc._guard.pending) == 1

    real_monotonic = time.monotonic()
    with mock.patch.object(jc.time, "monotonic", lambda: real_monotonic + 601):
        cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("ok")])]
        with quiet(io.StringIO()):
            post_chat("yes")
    assert "note E" not in notes_text() and len(jc._guard.pending) == 0

    # under the limit: still confirmable
    cl = fresh_server(tmp(), [R("tool_use", [SU("s"), SR("s"), TU("r", "remember", {"category": "personal", "note": "note F"})]),
                             R("end_turn", [T("Need yes.")])])
    with quiet(io.StringIO()):
        post_chat("(web_search) remember F")
    real_monotonic = time.monotonic()
    with mock.patch.object(jc.time, "monotonic", lambda: real_monotonic + 599):
        cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("ok")])]
        with quiet(io.StringIO()):
            post_chat("yes")
    assert "note F" in notes_text()


# ============================================================ "yes" vocabulary

def test_looks_like_yes_accepts_known_confirmations():
    for text in ["sim", "Sim.", "yes", "Yes!", "yep", "yeah", "ok", "okay", "claro", "confirmo",
                 "pode salvar", "pode gravar", "isso mesmo", "salva isso", "save it", "please save it"]:
        assert jc._looks_like_yes(text), text


def test_looks_like_yes_rejects_everything_else():
    for text in [
        "sim?",                           # a question mark anywhere -> never a confirmation
        "yes?",
        "nao",                            # negation
        "no",
        "not that one",
        "never",
        "don't",
        "yes but not that one",           # extra words outside CONFIRM_VOCAB
        "sim, pode ser",                  # "ser" is not in CONFIRM_VOCAB
        "sim sim sim sim sim sim",        # more than CONFIRM_MAX_WORDS words
        "",
        "the weather is nice today",      # a fabricated/unrelated argument, not a real yes
    ]:
        assert not jc._looks_like_yes(text), text


# ============================================================ confirmation ignores a fabricated argument

def test_confirm_pending_action_ignores_its_tool_input():
    """confirm_pending_action's schema takes no properties - even if a compromised round made the
    model call it with crafted input (as an injection might try), run_tool() does exactly what was
    HELD, nothing the fabricated input says."""
    work = tmp()
    cl = fresh_server(work, [R("tool_use", [SU("s"), SR("s"), TU("r", "remember", {"category": "personal", "note": "the real held note"})]),
                             R("end_turn", [T("Need yes.")])])
    with quiet(io.StringIO()):
        post_chat("(web_search) remember the real held note")
    assert len(jc._guard.pending) == 1

    fabricated = {"note": "a completely different, fabricated note", "category": "exams",
                 "override": True, "tool": "remember", "input": {"note": "injected"}}
    cl.script = [R("tool_use", [TU("c", "confirm_pending_action", fabricated)]), R("end_turn", [T("saved")])]
    with quiet(io.StringIO()):
        post_chat("yes")
    assert "the real held note" in notes_text() and "fabricated" not in notes_text() and "injected" not in notes_text()
    tool_result = cl.sent[-1]["messages"][-1]["content"][0]["content"]
    assert tool_result == "Saved under 'personal'."


# ============================================================ rollback on a failed API call

def test_rollback_on_api_failure():
    """When the API call fails mid-turn, the message history AND the guard's round/taint are
    rolled back as if her message had never been sent - nothing half-applied."""
    work = tmp()
    cl = fresh_server(work, [
        R("tool_use", [SU("s"), SR("s"), TU("r", "remember", {"category": "personal", "note": "should not survive"})]),
        api_error(),
    ])
    checkpoint_round = jc._guard.round
    with quiet(io.StringIO()):
        resp = post_chat("(web_search) remember this, then the API breaks")
    assert resp.status_code == 502
    assert jc._guard.round == checkpoint_round            # begin_round()'s increment was undone
    assert jc._guard.pending == []                        # the hold from the aborted round is gone
    assert "should not survive" not in notes_text()
    import server
    assert server.messages == []                          # the user message itself was rolled back too

    # the NEXT message is unaffected - not treated as tainted, not stuck mid-round
    cl.script = [R("tool_use", [TU("r", "remember", {"category": "personal", "note": "a normal note"})]), R("end_turn", [T("ok")])]
    with quiet(io.StringIO()):
        post_chat("remember a normal note")
    assert "a normal note" in notes_text() and len(jc._guard.pending) == 0


# ============================================================ update_notes: held + backup

def test_update_notes_held_then_backed_up_on_confirm():
    work = tmp()
    cl = fresh_server(work, [])
    jc.NOTES_FILE.write_text("## PERSONAL\n- old line\n", encoding="utf-8")

    cl.script = [R("tool_use", [SU("s"), SR("s"), TU("u", "update_notes", {"new_content": "## PERSONAL\n- old line\n- new line\n"})]),
                R("end_turn", [T("Need yes.")])]
    with quiet(io.StringIO()):
        r = post_chat("(web_search) update my notes").get_json()
    assert "Pendente / Pending" in r["reply"] and len(jc._guard.pending) == 1
    assert "new line" not in notes_text()   # held, not applied yet
    assert not list(jc.NOTES_FILE.parent.glob("notes.txt.*.bak"))   # no backup yet either - nothing happened

    cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("done")])]
    with quiet(io.StringIO()):
        post_chat("yes")
    assert "new line" in notes_text()
    backups = list(jc.NOTES_FILE.parent.glob("notes.txt.*.bak"))
    assert len(backups) == 1 and "old line" in backups[0].read_text(encoding="utf-8") and "new line" not in backups[0].read_text(encoding="utf-8")


def test_update_notes_backup_pruning_keeps_only_the_newest():
    work = tmp()
    fresh_server(work)
    jc.NOTES_FILE.write_text("## PERSONAL\n- v0\n", encoding="utf-8")
    for i in range(jc.NOTES_BACKUP_KEEP + 3):
        jc._backup_notes()
        jc.NOTES_FILE.write_text(f"## PERSONAL\n- v{i + 1}\n", encoding="utf-8")
        time.sleep(0.01)
    backups = list(jc.NOTES_FILE.parent.glob("notes.txt.*.bak"))
    assert len(backups) == jc.NOTES_BACKUP_KEEP


# ============================================================ mode command detection

def test_mode_command_detection_security_cases():
    D = jc.detect_mode_command
    # exit words beat activation - "deactivate serious mode" LEAVES serious mode, never activates it
    assert D("deactivate serious mode") == ("normal", "en")
    assert D("Deactivating serious mode.") == ("normal", "en")
    assert D("please deactivate serious mode") == ("normal", "en")
    # English negation blocks an English command outright - "no serious mode" activates NOTHING
    assert D("no serious mode") is None
    assert D("not serious mode") is None
    assert D("never serious mode") is None
    assert D("don't serious mode") is None
    # a bare question about it is not a command either
    assert D("what is serious mode") is None
    assert D("how do I turn off serious mode") is None
    # activation still works when nothing is blocking it
    assert D("serious mode") == ("serious", "en")
    assert D("modo serio") == ("serious", "pt")
    # the Portuguese "no" (= "in the") is NOT a negation - it still activates
    assert D("entra no modo serio") == ("serious", "pt")


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])

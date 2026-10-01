"""
Modes, cost tracking, refusal/fallback, history trimming and note backups - rebuilt here, at most
10 fast cases, with a fully fake client (no real Anthropic/Google call). See tests/_helpers.py.
"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

from _helpers import R, REFUSAL, SR, SU, T, TU, fresh_server, jc, notes_text, post_chat, run_module_tests

import server  # after _helpers: that's what puts web/ on sys.path

quiet = contextlib.redirect_stdout


def tmp():
    return Path(tempfile.mkdtemp())


# (a)
def test_refusal_falls_back_once_and_sums_usage():
    work = tmp()
    cl = fresh_server(work, [REFUSAL(in_tokens=20, out_tokens=0), R("end_turn", [T("Here you go.")], in_tokens=15, out_tokens=8)])
    server.mode_state.set_mode("serious")
    with quiet(io.StringIO()):
        r = post_chat("do something borderline").get_json()
    assert r["reply"].endswith("Here you go.") and r["fallback"] is True and r["mode"] == "normal"   # D1: stays on the model that accepted
    assert r["reply"].startswith(jc.NOTICES["fallback"]["en"])
    assert len(server.messages) == 2   # the refused response never entered the history
    assert server.messages[0]["content"] == "do something borderline"
    snap = server.mode_state.snapshot()
    assert snap["by_model"]["claude-opus-5"]["input_tokens"] == 20     # the refusal's usage still counts
    assert snap["by_model"]["claude-sonnet-5"]["input_tokens"] == 15 and snap["by_model"]["claude-sonnet-5"]["output_tokens"] == 8


# (b)
def test_double_refusal_raises_and_rolls_back():
    work = tmp()
    cl = fresh_server(work, [REFUSAL(category="violence"), REFUSAL(category="violence")])
    server.mode_state.set_mode("serious")
    with quiet(io.StringIO()):
        r = post_chat("something that always refuses").get_json()
    assert r["refused"] is True and r["refusal_category"] == "violence"
    assert server.messages == []   # her message was rolled back, never stayed in history


# (c)
def test_max_tokens_with_incomplete_tool_use_does_not_run_it():
    work = tmp()
    cl = fresh_server(work, [R("max_tokens", [TU("t1", "remember", {"category": "personal", "note": "should never be saved"})])])
    with quiet(io.StringIO()):
        r = post_chat("say something very long").get_json()
    assert r["truncated"] is True
    assert "should never be saved" not in notes_text()
    assert len(jc._guard.pending) == 0   # the tool never ran - not even held


# (d)
def test_idle_revert_after_15_minutes_warns_once():
    work = tmp()
    clock = [0.0]
    cl = fresh_server(work, [R("end_turn", [T("Still here.")])])
    server.mode_state = jc.ModeState(clock=lambda: clock[0])
    server.mode_state.set_mode("serious")
    clock[0] += jc.SERIOUS_IDLE_MINUTES * 60 + 1
    with quiet(io.StringIO()):
        r1 = post_chat("hello again").get_json()
    notice = jc.NOTICES["idle_revert"]["en"].format(minutes=jc.SERIOUS_IDLE_MINUTES)
    assert r1["mode"] == "normal" and r1["reply"].startswith(notice)

    cl.script = [R("end_turn", [T("Still here.")])]
    with quiet(io.StringIO()):
        r2 = post_chat("one more").get_json()
    assert not r2["reply"].startswith(notice)   # the notice fires once, not on every later message


# (e)
def test_status_and_mode_endpoints_never_keep_serious_alive():
    work = tmp()
    fresh_server(work)
    clock = [0.0]
    server.mode_state = jc.ModeState(clock=lambda: clock[0])
    server.mode_state.set_mode("serious")
    c = server.app.test_client()
    for _ in range(5):   # polling both endpoints repeatedly must NOT reset the idle timer
        clock[0] += 60
        assert c.get("/status").get_json()["mode"] == "serious"
        assert c.get("/mode").get_json()["mode"] == "serious"
    clock[0] = jc.SERIOUS_IDLE_MINUTES * 60 + 1   # past the ORIGINAL deadline, despite all that polling
    assert c.get("/mode").get_json()["mode"] == "normal"
    assert c.get("/status").get_json()["mode"] == "normal"


# (f)
def test_trim_history_never_splits_tool_use_from_tool_result():
    def user(text):
        return {"role": "user", "content": text}

    def asst_one_tool(tool_id):
        return {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "x", "input": {}}]}

    def tool_result(tool_id):
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}

    def asst_two_tools(id_a, id_b):
        return {"role": "assistant", "content": [{"type": "tool_use", "id": id_a, "name": "x", "input": {}},
                                                  {"type": "tool_use", "id": id_b, "name": "y", "input": {}}]}

    def two_tool_results(id_a, id_b):
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": id_a, "content": "ok"},
                                            {"type": "tool_result", "tool_use_id": id_b, "content": "ok"}]}

    def asst_text(s):
        return {"role": "assistant", "content": [{"type": "text", "text": s}]}

    messages = []
    for i in range(6):
        messages.append(user(f"question {i}"))
        if i % 2 == 0:
            messages += [asst_one_tool(f"call{i}"), tool_result(f"call{i}")]
        else:
            messages += [asst_two_tools(f"a{i}", f"b{i}"), two_tool_results(f"a{i}", f"b{i}")]
        messages.append(asst_text(f"answer {i}"))

    for k in (1, 2, 3, 4, 10, 50):   # whatever the cut point, the result is always internally consistent
        trimmed = jc.trim_history(messages, max_exchanges=k)
        assert jc._history_is_consistent(trimmed) or trimmed is messages
        assert trimmed is not messages or k >= 6   # a real cut never mutates/returns the same list object

    assert jc.trim_history(messages, max_exchanges=2)[0]["content"] == "question 4"   # cuts exactly at an exchange boundary


# (g)
def test_backup_rotation_keeps_five_and_ties_break_by_name():
    assert jc.NOTES_BACKUP_KEEP == 5
    work = tmp()
    a = work / "notes.txt.20260101-000000.bak"
    b = work / "notes.txt.20260101-000000-1.bak"
    c = work / "notes.txt.20260101-000001.bak"
    for p in (a, b, c):
        p.write_text("x", encoding="utf-8")
    now = a.stat().st_mtime
    import os
    for p in (a, b, c):
        os.utime(p, (now, now))   # identical mtimes - the tie _backup_sort_key must break by the embedded name
    assert sorted([c, a, b], key=jc._backup_sort_key) == [a, b, c]

    fresh_server(work)
    jc.NOTES_FILE.write_text("## PERSONAL\n- v0\n", encoding="utf-8")
    for i in range(8):
        jc._backup_notes()
        jc.NOTES_FILE.write_text(f"## PERSONAL\n- v{i + 1}\n", encoding="utf-8")
    assert len(list(jc.NOTES_FILE.parent.glob("notes.txt.*.bak"))) == jc.NOTES_BACKUP_KEEP


# (h)
def test_add_usage_sums_per_model_and_charges_max_price_for_unknown_model():
    state = jc.ModeState()
    state.add_usage("claude-sonnet-5", 1_000_000, 1_000_000)
    state.add_usage("claude-sonnet-5", 1_000_000, 0)
    state.add_usage("claude-opus-5", 1_000_000, 1_000_000)
    snap = state.snapshot()
    assert snap["by_model"]["claude-sonnet-5"]["input_tokens"] == 2_000_000
    assert snap["by_model"]["claude-sonnet-5"]["output_tokens"] == 1_000_000
    assert round(snap["by_model"]["claude-sonnet-5"]["cost"], 4) == round(2 * 2.0 + 1 * 10.0, 4)
    assert round(snap["by_model"]["claude-opus-5"]["cost"], 4) == round(1 * 5.0 + 1 * 25.0, 4)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cost = state.add_usage("some-future-model-nobody-configured-yet", 1_000_000, 1_000_000)
    max_in = max(m["input_price"] for m in jc.MODES.values())
    max_out = max(m["output_price"] for m in jc.MODES.values())
    assert round(cost, 4) == round(max_in + max_out, 4)
    assert "unknown model" in buf.getvalue()


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])

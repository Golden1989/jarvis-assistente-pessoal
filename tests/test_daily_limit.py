"""
DailySpend: the persisted daily spending cap on the Claude API (jarvis_core.DAILY_LIMIT_USD) -
atomic/thread-safe persistence to spend_log.json, the once-at-the-top check in call_claude(), the
web_search surcharge, "allow more today", and the 80% one-time warning.

Every test is offline and self-contained: no real Anthropic call, no real spend_log.json ever
touched - everything lives under a fresh tempfile.mkdtemp() per test. See tests/_helpers.py.
"""
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from _helpers import FakeClient, R, T, TU, ds, fresh_server, isolate, jc, post_chat, run_module_tests

import server  # noqa: E402 - after _helpers, which puts web/ on sys.path

quiet = contextlib.redirect_stdout


def tmp():
    return Path(tempfile.mkdtemp())


# ============================================================ DailySpend itself

def test_cost_persists_across_two_instances_same_day():
    work = tmp()
    path = work / "spend_log.json"
    clock = lambda: "2026-01-01"
    a = ds.DailySpend(path=path, clock=clock)
    a.add(0.30)
    a.add(0.05)
    b = ds.DailySpend(path=path, clock=clock)  # a fresh instance, same file, same day
    spent, bonus = b.snapshot()
    assert round(spent, 6) == 0.35 and bonus == 0.0


def test_new_local_day_resets_to_zero():
    work = tmp()
    path = work / "spend_log.json"
    day = ["2026-01-01"]
    a = ds.DailySpend(path=path, clock=lambda: day[0])
    a.add(0.90)
    a.add_bonus(1.0)
    day[0] = "2026-01-02"  # a new day, same file
    b = ds.DailySpend(path=path, clock=lambda: day[0])
    spent, bonus = b.snapshot()
    assert spent == 0.0 and bonus == 0.0


def test_corrupt_file_does_not_crash_and_starts_at_zero():
    work = tmp()
    path = work / "spend_log.json"
    path.write_text("{not json", encoding="utf-8")
    buf = io.StringIO()
    with quiet(buf):
        spend = ds.DailySpend(path=path, clock=lambda: "2026-01-01")
        spent, bonus = spend.snapshot()
    assert spent == 0.0 and bonus == 0.0
    assert "[spend]" in buf.getvalue()


def test_invalid_spent_or_bonus_values_start_today_at_zero_without_crashing():
    cases = [
        '{"date": "2026-01-01", "spent": "abc", "bonus": 0}',   # not a number
        '{"date": "2026-01-01", "spent": NaN, "bonus": 0}',      # json.loads accepts this extension, but it's not a sane amount
        '{"date": "2026-01-01", "spent": -5, "bonus": 0}',       # negative
        '{"date": "2026-01-01", "bonus": 0}',                    # "spent" missing entirely
    ]
    for raw in cases:
        work = tmp()
        path = work / "spend_log.json"
        path.write_text(raw, encoding="utf-8")
        buf = io.StringIO()
        with quiet(buf):
            spend = ds.DailySpend(path=path, clock=lambda: "2026-01-01")
            spent, bonus = spend.snapshot()
        assert spent == 0.0 and bonus == 0.0, raw
        assert "[spend]" in buf.getvalue(), raw


def test_save_failure_does_not_raise_and_keeps_the_total_in_memory():
    work = tmp()
    path = work / "spend_log.json"
    spend = ds.DailySpend(path=path, clock=lambda: "2026-01-01")
    real_replace = ds.os.replace

    def failing_replace(*args, **kwargs):
        raise PermissionError("no access")

    ds.os.replace = failing_replace
    try:
        buf = io.StringIO()
        with quiet(buf):
            spend.add(0.42)  # must not raise
        assert "[spend]" in buf.getvalue() and "could not save" in buf.getvalue()
    finally:
        ds.os.replace = real_replace
    spent, _ = spend.snapshot()
    assert spent == 0.42  # the in-memory total is still correct, even though nothing was persisted
    assert not path.with_name(path.name + ".tmp").exists()  # the leftover .tmp was removed


def test_atomic_write_leaves_no_tmp_file_behind():
    work = tmp()
    path = work / "spend_log.json"
    spend = ds.DailySpend(path=path, clock=lambda: "2026-01-01")
    spend.add(0.10)
    assert path.exists()
    assert not path.with_name(path.name + ".tmp").exists()


def test_allow_more_today_adds_to_bonus_only_and_resets_next_day():
    work = tmp()
    path = work / "spend_log.json"
    day = ["2026-01-01"]
    spend = ds.DailySpend(path=path, clock=lambda: day[0])
    spend.add_bonus(1.0)
    spent, bonus = spend.snapshot()
    assert spent == 0.0 and bonus == 1.0
    day[0] = "2026-01-02"
    spent, bonus = spend.snapshot()
    assert spent == 0.0 and bonus == 0.0


def test_jarvis_testing_refuses_the_real_file():
    assert os.environ.get("JARVIS_TESTING") == "1"
    spend = ds.DailySpend()  # no path given - points at the real default spend_log.json
    try:
        spend.add(0.01)
        raise AssertionError("should have refused to touch the real spend_log.json")
    except RuntimeError as error:
        assert "JARVIS_TESTING" in str(error)


# ============================================================ wired into call_claude()

def test_missing_web_search_usage_field_counts_as_zero_and_does_not_crash():
    work = tmp()
    fresh_server(work, [R("end_turn", [T("ok")], in_tokens=10, out_tokens=5)])
    with quiet(io.StringIO()):
        post_chat("hello")
    spent, _ = jc._daily_spend.snapshot()
    token_cost = (10 / 1_000_000 * jc.MODES["normal"]["input_price"]
                  + 5 / 1_000_000 * jc.MODES["normal"]["output_price"])
    assert round(spent, 6) == round(token_cost, 6)  # no web-search surcharge added


def test_web_search_cost_is_added_to_session_and_daily_total():
    work = tmp()
    response = R("end_turn", [T("ok")], in_tokens=10, out_tokens=5)
    response.usage.server_tool_use = SimpleNamespace(web_search_requests=2)
    fresh_server(work, [response])
    with quiet(io.StringIO()):
        post_chat("search twice")
    spent, _ = jc._daily_spend.snapshot()
    token_cost = (10 / 1_000_000 * jc.MODES["normal"]["input_price"]
                  + 5 / 1_000_000 * jc.MODES["normal"]["output_price"])
    assert round(spent - token_cost, 6) == round(2 * jc.WEB_SEARCH_COST_USD, 6)
    session_cost = server.mode_state.costs["claude-sonnet-5"]["cost"]  # raw, unrounded (snapshot() rounds to 4dp)
    assert round(session_cost - token_cost, 6) == round(2 * jc.WEB_SEARCH_COST_USD, 6)


def test_daily_spend_is_recorded_even_without_a_modestate():
    """call_claude()'s `state` param is optional - future scheduled routines (morning briefing,
    4pm quiz) will call it with state=None. The daily total must still be recorded there too, or
    those calls would silently escape the cap entirely."""
    work = tmp()
    isolate(work)
    client = FakeClient([R("end_turn", [T("ok")], in_tokens=10, out_tokens=5)])
    messages = [{"role": "user", "content": "hello"}]
    with quiet(io.StringIO()):
        jc.call_claude(client, "system prompt", messages, mode="normal", state=None)
    spent, _ = jc._daily_spend.snapshot()
    token_cost = (10 / 1_000_000 * jc.MODES["normal"]["input_price"]
                  + 5 / 1_000_000 * jc.MODES["normal"]["output_price"])
    assert round(spent, 6) == round(token_cost, 6)


def test_at_the_limit_call_claude_refuses_before_any_api_call():
    work = tmp()
    cl = fresh_server(work, [R("end_turn", [T("should never be reached")])])
    jc._daily_spend.add(jc.DAILY_LIMIT_USD)  # exactly at the limit
    with quiet(io.StringIO()):
        resp = post_chat("anything")
    assert resp.status_code == 200  # NOT an error - same as a refusal, so the voice speaks it
    r = resp.get_json()
    assert cl.sent == []  # the fake client was NEVER called
    assert r["daily_limit"] is True
    assert f"${jc.DAILY_LIMIT_USD:.2f}" in r["reply"]
    assert jc._guard.round == 0  # not even a round was started


def test_no_check_in_the_middle_of_a_tool_loop():
    """A message already under way (several tool_use round-trips) must finish even if its own
    cost pushes `spent` past the limit partway through - the cap is only checked at the START of
    the NEXT call_claude(), never between a tool_use and its tool_result."""
    work = tmp()
    cl = fresh_server(work, [
        R("tool_use", [TU("t1", "get_current_datetime")], in_tokens=10, out_tokens=5),
        R("tool_use", [TU("t2", "get_current_datetime")], in_tokens=10, out_tokens=5),
        R("end_turn", [T("done")], in_tokens=10, out_tokens=5),
    ])
    jc._daily_spend.add(jc.DAILY_LIMIT_USD - 0.000001)  # one cent under - the FIRST response alone crosses it
    with quiet(io.StringIO()):
        r = post_chat("do a multi-step thing").get_json()
    assert "done" in r["reply"]
    assert len(cl.sent) == 3  # all three round-trips ran - none were cut off mid-loop

    sent_before = len(cl.sent)
    cl.script = [R("end_turn", [T("should never run")])]
    with quiet(io.StringIO()):
        r2 = post_chat("one more").get_json()
    assert r2.get("daily_limit") is True  # NOW, the next message, it's refused
    assert len(cl.sent) == sent_before  # no new API call was made for it


def test_allow_more_today_lifts_the_cap_for_the_rest_of_the_day():
    work = tmp()
    cl = fresh_server(work, [R("end_turn", [T("ok")])])
    jc._daily_spend.add(jc.DAILY_LIMIT_USD)
    with quiet(io.StringIO()):
        blocked = post_chat("anything").get_json()
    assert blocked["daily_limit"] is True

    with quiet(io.StringIO()):
        r = post_chat("allow more today").get_json()
    assert r["fixed"] is True and cl.sent == []  # no API call for the command itself
    _, bonus = jc._daily_spend.snapshot()
    assert bonus == jc.DAILY_LIMIT_BONUS_USD

    with quiet(io.StringIO()):
        r2 = post_chat("anything").get_json()
    assert r2.get("daily_limit") is not True and len(cl.sent) == 1  # now goes through


def test_detect_allow_more_command_matches_the_whole_message_exactly():
    """Only her ENTIRE message, exactly this phrase (case/punctuation/accents aside) triggers it -
    never a substring of a longer sentence, never a question. _chat()/jarvis.py call this on her
    raw typed/spoken user_input ONLY, before call_claude() ever runs - the model's own reply text
    and anything a tool returns are never passed to it, so neither can ever trigger this."""
    assert jc.detect_allow_more_command("allow more today") == "en"
    assert jc.detect_allow_more_command("Allow More Today!") == "en"  # case/punctuation don't matter
    assert jc.detect_allow_more_command("libera mais hoje") == "pt"
    assert jc.detect_allow_more_command("Jarvis, allow more today please") is None  # extra words - not a bare command
    assert jc.detect_allow_more_command("ok allow more today thanks") is None  # the phrase alone, not embedded in a sentence
    assert jc.detect_allow_more_command("allow more today?") is None  # a question, not a command
    assert jc.detect_allow_more_command("allow more") is None  # not the full phrase
    assert jc.detect_allow_more_command("") is None
    assert jc.detect_allow_more_command(None) is None


def test_eighty_percent_notice_appears_only_once():
    work = tmp()
    fresh_server(work, [R("end_turn", [T("one")]), R("end_turn", [T("two")])])
    jc._daily_spend.add(0.8 * jc.DAILY_LIMIT_USD)
    with quiet(io.StringIO()):
        r1 = post_chat("first").get_json()
    assert jc.NOTICES["daily_limit_warning"]["en"] in r1["reply"]

    with quiet(io.StringIO()):
        r2 = post_chat("second").get_json()
    assert jc.NOTICES["daily_limit_warning"]["en"] not in r2["reply"]


# ============================================================ housekeeping

def test_spend_log_is_gitignored():
    root = Path(ds.SPEND_FILE).resolve().parent
    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "spend_log.json" in gitignore


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])

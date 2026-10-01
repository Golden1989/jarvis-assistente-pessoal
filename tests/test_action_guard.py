"""
ActionGuard (the generalized memory guard) + open_app + stop/cancel + PROTECTED_PATHS.

Every test is offline and self-contained: no real Anthropic or Google call, no real program ever
launched (subprocess.Popen / os.startfile are patched - see _helpers.fake_launcher), no real path
or personal data from any machine - everything lives under a fresh tempfile.mkdtemp() per test.

Run directly (`python tests/test_action_guard.py`) or via `python run_tests.py` from the project
root, which runs every tests/test_*.py file this way.
"""
import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

from _helpers import (
    R, SR, SU, T, TU, al, default_whitelist, fake_google_ok, fake_launcher, fresh_server, gc, jc,
    notes_text, post_cancel, post_chat, run_module_tests, write_whitelist,
)

quiet = contextlib.redirect_stdout


def tmp():
    return Path(tempfile.mkdtemp())


# ============================================================ rename & tool table

def test_rename_and_tool_levels():
    """confirm_pending_note was renamed to confirm_pending_action; TOOL_LEVELS has every tool."""
    isolate_dir = tmp()
    fresh_server(isolate_dir)
    assert "confirm_pending_action" in {t["name"] for t in jc.TOOLS}
    assert "confirm_pending_note" not in {t.get("name") for t in jc.TOOLS}
    assert jc.TOOL_LEVELS == {
        "get_current_datetime": 0, "web_search": 0, "list_files": 0, "read_file": 0,
        "list_calendar_events": 0, "look_at_screen": 0, "confirm_pending_action": 0,
        "remember": 1, "update_notes": 1, "open_app": 1, "create_calendar_event": 1, "spotify": 1,
    }
    open_app_tool = next(t for t in jc.TOOLS if t["name"] == "open_app")
    assert set(open_app_tool["input_schema"]["properties"]) == {"name", "folder"}
    assert open_app_tool["input_schema"]["required"] == ["name"]


# ============================================================ create_calendar_event, level 1

def test_create_calendar_event_runs_directly_when_clean():
    cl = fresh_server(tmp(), [
        R("tool_use", [TU("t1", "create_calendar_event",
                          {"summary": "Dentist", "start": "2026-10-03T14:00:00", "end": "2026-10-03T15:00:00"})]),
        R("end_turn", [T("Booked it.")]),
    ])
    fake_google_ok()
    with quiet(io.StringIO()):
        r = post_chat("book the dentist").get_json()
    assert r["reply"] == "Booked it." and "Pendente" not in r["reply"]
    assert len(jc._guard.pending) == 0


def test_create_calendar_event_held_after_untrusted_content():
    """Held (not created) when the message - or the one right before it - used web_search,
    read_file, list_files, look_at_screen, or list_calendar_events; "yes" then creates it."""
    taint_sources = {
        "web_search": lambda: [SU("s"), SR("s")],
        "read_file": lambda: [TU("rf", "read_file", {"path": "missing.txt"})],       # harmless: file doesn't exist
        "list_files": lambda: [TU("lf", "list_files", {})],                         # harmless: lists the temp ALLOWED_FOLDER
        "look_at_screen": lambda: [TU("sc", "look_at_screen", {})],                  # mocked: no real screenshot
        "list_calendar_events": lambda: [TU("lc", "list_calendar_events", {"days_ahead": 7})],
    }
    for source_name, taint_block in taint_sources.items():
        cl = fresh_server(tmp(), [
            R("tool_use", [*taint_block(),
                           TU("ce", "create_calendar_event",
                              {"summary": "Standup", "start": "2026-10-04T09:00:00", "end": "2026-10-04T09:15:00"})]),
            R("end_turn", [T("I'll need your yes for that.")]),
        ])
        fake_google_ok()
        from unittest import mock
        with mock.patch.object(jc, "_capture_screen", lambda: {"type": "image", "media_type": "image/png", "data": ""}), \
             mock.patch.object(gc, "list_events", lambda *a, **k: "No events in that range."), \
             quiet(io.StringIO()):
            r = post_chat(f"({source_name}) schedule the standup").get_json()
        assert "Pendente / Pending" in r["reply"], (source_name, r["reply"])
        assert '"Standup" - 2026-10-04T09:00:00 to 2026-10-04T09:15:00' in r["reply"], (source_name, r["reply"])
        assert len(jc._guard.pending) == 1

        cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("Booked.")])]
        with quiet(io.StringIO()):
            r2 = post_chat("yes").get_json()
        assert r2["reply"] == "Booked." and len(jc._guard.pending) == 0, (source_name, r2)
        tool_result = cl.sent[-1]["messages"][-1]["content"][0]["content"]
        assert tool_result == "Event created: https://example.invalid/fake-event", (source_name, tool_result)


def test_taint_carries_one_extra_message():
    """TAINT_FOLLOWING_MESSAGES: the message right after a tainted one is held too."""
    cl = fresh_server(tmp(), [R("tool_use", [SU("s"), SR("s")]), R("end_turn", [T("Here's what I found.")])])
    with quiet(io.StringIO()):
        post_chat("search something")
    cl.script = [
        R("tool_use", [TU("ce", "create_calendar_event",
                          {"summary": "Call", "start": "2026-10-05T10:00:00", "end": "2026-10-05T10:30:00"})]),
        R("end_turn", [T("Need your yes.")]),
    ]
    with quiet(io.StringIO()):
        r = post_chat("ok now book a call").get_json()
    assert "Pendente / Pending" in r["reply"]


# ============================================================ open_app

def test_open_app_uri_runs_directly():
    work = tmp()
    cl = fresh_server(work, [R("tool_use", [TU("t1", "open_app", {"name": "spotify"})]), R("end_turn", [T("Opened it.")])])
    default_whitelist(work)
    with fake_launcher() as launched, quiet(io.StringIO()):
        r = post_chat("open spotify").get_json()
    assert r["reply"] == "Opened it." and launched == [("uri", "spotify:")]


def test_open_app_unknown_name_refused():
    work = tmp()
    cl = fresh_server(work, [R("tool_use", [TU("t1", "open_app", {"name": "totally-not-a-real-app"})]),
                             R("end_turn", [T("That's not allowed.")])])
    default_whitelist(work)
    with fake_launcher() as launched, quiet(io.StringIO()):
        post_chat("open totally-not-a-real-app")
    assert len(jc._guard.pending) == 0 and launched == []
    tool_result = cl.sent[-1]["messages"][-1]["content"][0]["content"]
    assert "'totally-not-a-real-app' is not an allowed application" in tool_result


def test_open_app_vscode_alias_case_insensitive_folder():
    """Matching ignores case and goes through an alias; the folder is resolved against
    ALLOWED_FOLDER and appended as its own argv element; the configured args are untouched."""
    work = tmp()
    cl = fresh_server(work, [R("tool_use", [TU("t1", "open_app", {"name": "VS Code", "folder": "demo"})]),
                             R("end_turn", [T("Opened it.")])])
    entries, fake_exe = default_whitelist(work)   # AFTER fresh_server: isolate() just set ALLOWED_FOLDER/WHITELIST_FILE
    project = jc.ALLOWED_FOLDER / "demo"
    project.mkdir(parents=True, exist_ok=True)
    with fake_launcher() as launched, quiet(io.StringIO()):
        r = post_chat("open vs code on my demo project").get_json()
    assert r["reply"] == "Opened it."
    assert launched == [("exe", [fake_exe, "--new-window", str(project)])]
    assert "--disable-workspace-trust" not in launched[0][1]


def test_open_app_vscode_path_traversal_refused():
    """Invalid actions are refused immediately - never held, not even in a tainted round."""
    work = tmp()
    cl = fresh_server(work, [R("tool_use", [SU("s"), SR("s"),
                                           TU("t1", "open_app", {"name": "code", "folder": "..\\..\\outside"})]),
                             R("end_turn", [T("Can't do that.")])])
    default_whitelist(work)
    with fake_launcher() as launched, quiet(io.StringIO()):
        r = post_chat("(web_search) open code on ..\\..\\outside").get_json()
    assert len(jc._guard.pending) == 0 and launched == [] and "Pendente" not in r["reply"]
    tool_result = cl.sent[-1]["messages"][-1]["content"][0]["content"]
    assert "outside the allowed folder" in tool_result


def test_open_app_browser_never_takes_folder_or_url():
    """operagx (the configured browser entry) accepts no folder - so there is no way for the
    model to hand it anything URL-shaped either."""
    work = tmp()
    cl = fresh_server(work, [R("tool_use", [TU("t1", "open_app", {"name": "operagx", "folder": "whatever"})]),
                             R("end_turn", [T("No.")])])
    default_whitelist(work)
    with fake_launcher() as launched, quiet(io.StringIO()):
        post_chat("open opera gx on whatever")
    assert launched == [] and len(jc._guard.pending) == 0
    tool_result = cl.sent[-1]["messages"][-1]["content"][0]["content"]
    assert "does not accept a folder argument" in tool_result

    entries, fake_exe = default_whitelist(work)
    cl = fresh_server(work, [R("tool_use", [TU("t1", "open_app", {"name": "browser"})]), R("end_turn", [T("Opened.")])])
    write_whitelist(entries)
    with fake_launcher() as launched, quiet(io.StringIO()):
        post_chat("open the browser")
    assert launched == [("exe", [fake_exe])]   # exactly the configured argv - nothing appended


def test_confirm_executes_only_actions_actually_held():
    work = tmp()
    cl = fresh_server(work, [
        R("tool_use", [SU("s"), SR("s"), TU("r", "remember", {"category": "personal", "note": "note from test A"}),
                       TU("o", "open_app", {"name": "spotify"})]),
        R("end_turn", [T("Need your yes for both.")]),
    ])
    default_whitelist(work)
    with fake_launcher() as launched, quiet(io.StringIO()):
        r = post_chat("(web_search) remember A and open spotify").get_json()
    assert len(jc._guard.pending) == 2 and "Pendente" in r["reply"] and launched == []

    cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("Done.")])]
    with fake_launcher() as launched, quiet(io.StringIO()):
        post_chat("yes")
    assert launched == [("uri", "spotify:")] and "note from test A" in notes_text() and len(jc._guard.pending) == 0


def test_open_app_executor_revalidates_at_confirm_time():
    """The app table can change between hold and confirm (she could edit app_whitelist.json while
    something is pending) - the executor re-checks it, it doesn't trust the held snapshot."""
    work = tmp()
    cl = fresh_server(work, [R("tool_use", [SU("s"), SR("s"), TU("o", "open_app", {"name": "spotify"})]),
                             R("end_turn", [T("Need yes.")])])
    default_whitelist(work)
    with fake_launcher(), quiet(io.StringIO()):
        post_chat("(web_search) open spotify")
    assert len(jc._guard.pending) == 1

    write_whitelist({"vscode": {"type": "exe", "path": str(work / "fake_programs" / "FakeApp.exe"),
                                "args": [], "accepts_folder": False}})   # spotify removed from the table

    cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("ok")])]
    with fake_launcher() as launched, quiet(io.StringIO()):
        post_chat("yes")
    tool_result = cl.sent[-1]["messages"][-1]["content"][0]["content"]
    assert "not an allowed application" in tool_result and launched == []


def test_prep_open_app_unit_shape():
    work = tmp()
    fresh_server(work)
    default_whitelist(work)
    ok, item, msg = jc._prep_open_app({"name": "nope"})
    assert ok is False and item is None and "not an allowed application" in msg
    ok, item, msg = jc._prep_open_app({"name": "spotify"})
    assert ok is True and item == {"name": "spotify", "folder": None} and msg == "open spotify (spotify:)"


# ============================================================ ActionGuard mechanics / regression

def test_remember_update_notes_regression():
    """remember/update_notes behave exactly as before the rename (held when tainted, refused
    immediately when the note is empty, saved only on a clear yes)."""
    work = tmp()
    cl = fresh_server(work, [R("tool_use", [SU("s"), SR("s"), TU("r", "remember", {"category": "exams", "note": "note from test Y"})]),
                             R("end_turn", [T("Need yes.")])])
    with quiet(io.StringIO()):
        r1 = post_chat("search X and remember Y").get_json()
    assert r1["reply"].count("Pendente / Pending") == 1 and "note from test Y" in r1["reply"]
    assert len(jc._guard.pending) == 1 and "note from test Y" not in notes_text()

    cl.script = [R("tool_use", [TU("c", "confirm_pending_action", {})]), R("end_turn", [T("saved")])]
    with quiet(io.StringIO()):
        r2 = post_chat("sim").get_json()
    assert r2["reply"] == "saved"
    assert "note from test Y" in notes_text()
    tool_result = cl.sent[-1]["messages"][-1]["content"][0]["content"]
    assert tool_result == "Saved under 'exams'."

    cl = fresh_server(work, [R("tool_use", [TU("r", "remember", {"category": "personal", "note": ""})]),
                             R("end_turn", [T("empty")])])
    with quiet(io.StringIO()):
        post_chat("remember nothing")
    assert len(jc._guard.pending) == 0
    tool_result = cl.sent[-1]["messages"][-1]["content"][0]["content"]
    assert "empty note" in tool_result


def test_pending_max_items_enforced():
    work = tmp()
    items = [TU(f"r{i}", "remember", {"category": "personal", "note": f"note {i}"}) for i in range(jc.PENDING_MAX_ITEMS + 2)]
    cl = fresh_server(work, [R("tool_use", [SU("s"), SR("s"), *items]), R("end_turn", [T("ok")])])
    with quiet(io.StringIO()):
        post_chat("(web_search) remember a bunch of things")
    assert len(jc._guard.pending) == jc.PENDING_MAX_ITEMS


def test_mode_commands_unaffected_by_action_guard():
    work = tmp()
    cl = fresh_server(work, [])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = post_chat("serious mode").get_json()
    assert r["reply"] == "Serious mode activating." and r["fixed"] is True and cl.sent == []
    assert "[action]" not in buf.getvalue()


def test_action_log_format():
    """[action] tool=... level=... status=held|executed|refused result=..."""
    work = tmp()
    cl = fresh_server(work, [R("tool_use", [TU("t1", "open_app", {"name": "nope"})]), R("end_turn", [T("no")])])
    default_whitelist(work)
    buf = io.StringIO()
    with fake_launcher(), contextlib.redirect_stdout(buf):
        post_chat("open nope")
    assert '[action] tool=open_app level=1 status=refused result="' in buf.getvalue()

    cl = fresh_server(work, [R("tool_use", [TU("t1", "open_app", {"name": "spotify"})]), R("end_turn", [T("ok")])])
    default_whitelist(work)
    buf = io.StringIO()
    with fake_launcher(), contextlib.redirect_stdout(buf):
        post_chat("open spotify")
    assert '[action] tool=open_app level=1 status=executed result="Opened spotify."' in buf.getvalue()

    cl = fresh_server(work, [R("tool_use", [SU("s"), SR("s"), TU("t1", "open_app", {"name": "spotify"})]),
                             R("end_turn", [T("ok")])])
    default_whitelist(work)
    buf = io.StringIO()
    with fake_launcher(), contextlib.redirect_stdout(buf):
        post_chat("(web_search) open spotify")
    assert '[action] tool=open_app level=1 status=held result="open spotify (spotify:)"' in buf.getvalue()


# ============================================================ stop / cancel

def test_detect_stop_command_whole_message_only():
    assert jc.detect_stop_command("stop") == "en" and jc.detect_stop_command("Stop.") == "en"
    assert jc.detect_stop_command("cancel") == "en" and jc.detect_stop_command("never mind") == "en"
    assert jc.detect_stop_command("cancela") == "pt" and jc.detect_stop_command("pare") == "pt" and jc.detect_stop_command("para") == "pt"
    for text in ["stop the music", "please stop", "cancel my meeting", "I need to stop by the store",
                 "pare de me zoar", "cancela a reuniao", "what should I do", "stop?"]:
        assert jc.detect_stop_command(text) is None, text


def test_stop_command_clears_pending_no_api_call():
    work = tmp()
    cl = fresh_server(work, [])
    with quiet(io.StringIO()):
        r = post_chat("stop").get_json()
    assert r["reply"] == "Nothing to cancel." and r["fixed"] is True and cl.sent == []

    cl = fresh_server(work, [R("tool_use", [SU("s"), SR("s"), TU("r", "remember", {"category": "personal", "note": "note from cancel test"})]),
                             R("end_turn", [T("Need yes.")])])
    with quiet(io.StringIO()):
        post_chat("(web_search) remember this")
    assert len(jc._guard.pending) == 1
    n_sent = len(cl.sent)
    with quiet(io.StringIO()):
        r = post_chat("cancel").get_json()
    assert r["reply"] == "Cancelled." and len(jc._guard.pending) == 0 and len(cl.sent) == n_sent   # no API call
    assert "note from cancel test" not in notes_text()


def test_cancel_endpoint():
    work = tmp()
    fresh_server(work)
    jc._guard.pending.append({"tool": "remember", "input": {"category": "personal", "note": "x"},
                              "display": '[personal] "x"', "base_stamp": None, "round": jc._guard.round,
                              "at": __import__("time").monotonic()})
    r1 = post_cancel().get_json()
    assert r1 == {"had_pending": True} and len(jc._guard.pending) == 0
    r2 = post_cancel().get_json()
    assert r2 == {"had_pending": False}


def test_voice_stop_ends_conversation():
    import json
    from unittest import mock
    web_dir = str(Path(jc.__file__).resolve().parent / "web")
    if web_dir not in sys.path:
        sys.path.insert(0, web_dir)
    import desktop

    work = tmp()
    fresh_server(work, [])

    class Win:
        def evaluate_js(self, js, cb=None):
            pass

    class Resp:
        def __init__(self, data):
            self._data = data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps(self._data).encode()

    def route(req, timeout=None):
        body = json.loads(req.data)
        return Resp(post_chat(body["message"]).get_json())

    spoken = []
    transcripts = iter([("stop", "en"), ("never reached", "en")])
    desktop._window = Win()
    log = io.StringIO()
    with mock.patch.object(desktop.voice_capture, "transcribe", lambda: next(transcripts)), \
         mock.patch.object(desktop.urllib.request, "urlopen", route), \
         mock.patch.object(desktop, "_speak_and_wait", lambda text, lang, timeout=30: spoken.append((text, lang))), \
         mock.patch.object(desktop.wake_word, "pause", lambda: None), \
         mock.patch.object(desktop.wake_word, "resume", lambda: None), \
         contextlib.redirect_stdout(log):
        desktop._handle_wake()
    assert spoken[-1] == ("Nothing to cancel.", "en-US")
    assert "stop/cancel heard - ending conversation" in log.getvalue()
    assert "never reached" not in log.getvalue()   # the 2nd transcript was never consumed


# ============================================================ PROTECTED_PATHS

def test_resolve_within_allowed_rejects_escapes():
    work = tmp()
    fresh_server(work)  # keeps ALLOWED_FOLDER pointed at this test's own temp folder
    for bad in ["..\\..\\outside.txt",
                str(Path(jc.ALLOWED_FOLDER).parent / "outside.txt"),
                r"\\localhost\c$\Windows\win.ini",
                "sub/../../outside.txt"]:
        try:
            jc._resolve_within_allowed(bad)
            raise AssertionError(f"should have been refused: {bad}")
        except ValueError:
            pass


def test_resolve_within_allowed_rejects_junction_to_protected_path():
    """A junction placed INSIDE ALLOWED_FOLDER pointing back out at a protected path must still be
    refused - Path.resolve() dereferences it, so the existing escape check already catches it."""
    work = tmp()
    fresh_server(work)
    protected_target = tmp()  # stands in for a "protected" folder - never the developer's real project
    (protected_target / "secret.txt").write_text("not real data", encoding="utf-8")
    junction = jc.ALLOWED_FOLDER / "link_out"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(protected_target)],
                          capture_output=True, text=True)
    try:
        if made.returncode != 0:
            print(f"(skipped: could not create a test junction on this machine: {made.stderr.strip()})")
            return
        try:
            jc._resolve_within_allowed("link_out\\secret.txt")
            raise AssertionError("a junction inside ALLOWED_FOLDER escaped the check")
        except ValueError:
            pass
    finally:
        if junction.exists() or junction.is_symlink():
            subprocess.run(["cmd", "/c", "rmdir", str(junction)], capture_output=True)
    assert not junction.exists()


def test_protected_paths_disjoint_from_allowed_folder():
    work = tmp()
    fresh_server(work)
    allowed = jc.ALLOWED_FOLDER.resolve()
    for p in jc.PROTECTED_PATHS:
        p = Path(p).resolve()
        assert p != allowed and allowed not in p.parents, p


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])

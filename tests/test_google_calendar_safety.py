"""
Proves the real Google Calendar can't be reached by accident from a test: with JARVIS_TESTING set
(see _helpers.py) and no fake _service installed, get_service() - and therefore create_event,
list_events, next_event, and reading token.json - must refuse instead of ever calling the real API.
"""
import os
import sys
from pathlib import Path

from _helpers import gc, run_module_tests


def test_jarvis_testing_is_set():
    assert os.environ.get("JARVIS_TESTING") == "1"


def test_real_google_call_is_blocked_without_a_fake_service():
    gc._service = None   # exactly what "no substitute installed" looks like
    try:
        gc.get_service()
        raise AssertionError("get_service() should have refused - it would have reached the real API")
    except RuntimeError as error:
        assert "JARVIS_TESTING" in str(error) and "refusing" in str(error)

    # the same guard covers every public function, since they all go through get_service()
    for call in (lambda: gc.create_event("x", "2026-01-01T00:00:00", "2026-01-01T01:00:00"),
                 lambda: gc.list_events("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
                 lambda: gc.next_event()):
        gc._service = None
        gc._next_event_cache = None
        try:
            call()
            raise AssertionError(f"{call} should have refused instead of reaching the real API")
        except RuntimeError as error:
            assert "JARVIS_TESTING" in str(error)


def test_installing_a_fake_service_is_still_allowed():
    """The guard only fires when NOTHING was installed - a test's own fake must keep working."""
    from types import SimpleNamespace
    gc._service = SimpleNamespace(
        events=lambda: SimpleNamespace(insert=lambda **kw: SimpleNamespace(execute=lambda: {"htmlLink": "ok"})))
    assert gc.get_service() is gc._service   # no RuntimeError - a real substitute was installed


def test_dead_credentials_raise_instead_of_blocking_on_a_browser_login():
    """
    Regression: a dead/missing token used to make get_service() fall through to
    InstalledAppFlow.run_local_server(), which opens a browser and BLOCKS - fine for
    calendar_login.py (a human watching the terminal), but it silently froze the live app's one
    request-handling thread when a /chat tool call hit it (this really happened). The default
    (allow_interactive_login=False) must raise NotLoggedIn immediately instead, every time.

    This is the one test in the suite that has to turn JARVIS_TESTING off, since the behavior
    under test is BELOW that guard - two independent safety nets make sure this still can't reach
    anything real even if the fix were broken: TOKEN_FILE and CREDENTIALS_FILE both point at paths
    that don't exist, so even a wrongly-reached interactive flow would fail on a missing file
    before ever opening a browser or a socket.
    """
    import tempfile
    work = Path(tempfile.mkdtemp())
    real_testing = os.environ.pop("JARVIS_TESTING", None)
    real_token_file, real_creds_file, real_service = gc.TOKEN_FILE, gc.CREDENTIALS_FILE, gc._service
    gc.TOKEN_FILE = str(work / "no_such_token.json")
    gc.CREDENTIALS_FILE = str(work / "no_such_credentials.json")
    gc._service = None
    try:
        try:
            gc.get_service(trigger_background_login=False)  # the background-login variants are tested separately below
            raise AssertionError("should have raised NotLoggedIn, not fallen through to a browser login")
        except gc.NotLoggedIn as error:
            assert "calendar_login.py" in str(error)
    finally:
        if real_testing is not None:
            os.environ["JARVIS_TESTING"] = real_testing
        gc.TOKEN_FILE, gc.CREDENTIALS_FILE, gc._service = real_token_file, real_creds_file, real_service


def test_calendar_login_script_opts_into_the_interactive_flow():
    """calendar_login.py is the ONLY caller allowed to block on a browser login."""
    src = (Path(gc.__file__).resolve().parent / "calendar_login.py").read_text(encoding="utf-8")
    assert "get_service(allow_interactive_login=True)" in src


def test_background_login_starts_once_and_never_opens_a_real_browser():
    """
    Regression for the follow-up request: instead of just telling her to run calendar_login.py,
    get_service() should open the login for her automatically, in the background, without ever
    blocking the caller - and never more than one browser tab for one dead login, even if several
    tool calls hit it before she's finished logging in.

    InstalledAppFlow is replaced with a fake (a slow one, so the "already in progress" window is
    observable) - the background thread really runs and really calls run_local_server(), but on
    the FAKE flow, never the real google_auth_oauthlib one. No real browser, no real network.
    """
    import tempfile
    import time as time_mod
    from types import SimpleNamespace
    from unittest import mock

    work = Path(tempfile.mkdtemp())
    real_testing = os.environ.pop("JARVIS_TESTING", None)
    real_token_file, real_creds_file, real_service, real_thread, real_flow_cls, real_build = (
        gc.TOKEN_FILE, gc.CREDENTIALS_FILE, gc._service, gc._login_thread, gc.InstalledAppFlow, gc.build)
    gc.TOKEN_FILE = str(work / "token.json")
    gc.CREDENTIALS_FILE = str(work / "credentials.json")  # never actually read - the fake flow ignores it
    gc._service = None
    gc._login_thread = None
    opened = []

    class FakeCreds:
        valid = True
        def to_json(self):
            return "{}"

    class FakeFlow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            return FakeFlow()
        def run_local_server(self, port=0):
            opened.append(time_mod.monotonic())
            time_mod.sleep(0.3)   # slow on purpose: gives the test a window to see "already in progress"
            return FakeCreds()

    gc.InstalledAppFlow = FakeFlow
    gc.build = lambda *a, **kw: SimpleNamespace(fake_service=True)   # no real discovery-document network call
    try:
        with mock.patch("builtins.open", mock.mock_open()):   # _save creds writes TOKEN_FILE - no real file needed
            try:
                gc.get_service()
                raise AssertionError("should have raised NotLoggedIn while the background login runs")
            except gc.NotLoggedIn as error:
                assert "just opened a browser tab" in str(error)
            assert gc._login_thread is not None and gc._login_thread.is_alive()
            for _ in range(50):           # wait for the background thread to actually reach run_local_server()
                if opened:
                    break
                time_mod.sleep(0.02)
            assert len(opened) == 1   # the fake "browser" really was invoked, exactly once so far

            try:
                gc.get_service()   # a second request, while the background login is still running
                raise AssertionError("should still raise - login isn't done yet")
            except gc.NotLoggedIn as error:
                assert "already waiting" in str(error) or "check for a browser tab" in str(error)
            assert len(opened) == 1   # NOT a second browser tab

            gc._login_thread.join(timeout=5)
            assert not gc._login_thread.is_alive()
            assert gc._service is not None   # the background login succeeded and cached a service
    finally:
        gc.InstalledAppFlow, gc.build = real_flow_cls, real_build
        if real_testing is not None:
            os.environ["JARVIS_TESTING"] = real_testing
        gc.TOKEN_FILE, gc.CREDENTIALS_FILE = real_token_file, real_creds_file
        gc._service, gc._login_thread = real_service, real_thread


def test_status_never_triggers_a_background_login():
    """next_event() (polled passively by /status just from having the dashboard open) must never
    pop a browser tab on its own - only an explicit calendar request she made should."""
    import tempfile
    from unittest import mock
    work = Path(tempfile.mkdtemp())
    real_testing = os.environ.pop("JARVIS_TESTING", None)
    real_token_file, real_creds_file, real_service, real_thread = (
        gc.TOKEN_FILE, gc.CREDENTIALS_FILE, gc._service, gc._login_thread)
    gc.TOKEN_FILE = str(work / "no_such_token.json")
    gc.CREDENTIALS_FILE = str(work / "no_such_credentials.json")
    gc._service = None
    gc._login_thread = None
    try:
        with mock.patch.object(gc, "_start_background_login") as fake_start:
            try:
                gc.next_event()   # NotLoggedIn is expected here - /status is what swallows it (see web/server.py)
                raise AssertionError("should have raised NotLoggedIn - credentials don't exist")
            except gc.NotLoggedIn:
                pass
            fake_start.assert_not_called()
    finally:
        if real_testing is not None:
            os.environ["JARVIS_TESTING"] = real_testing
        gc.TOKEN_FILE, gc.CREDENTIALS_FILE = real_token_file, real_creds_file
        gc._service, gc._login_thread = real_service, real_thread


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])

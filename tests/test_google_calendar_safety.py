"""
Proves the real Google Calendar can't be reached by accident from a test: with JARVIS_TESTING set
(see _helpers.py) and no fake _service installed, get_service() - and therefore create_event,
list_events, next_event, and reading token.json - must refuse instead of ever calling the real API.
"""
import os
import sys

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


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])

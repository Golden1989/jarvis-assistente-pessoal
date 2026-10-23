"""
Pure unit tests for web/quiz_signal.py's quiz_signal_action() - the one piece of Stage B
(the 16:00 quiz-due signal's desktop behavior) that doesn't need a real window, so it's the only
part tested here. The background poll thread, the real pywebview window, and the dashboard
banner all need a real window/browser and are covered by the MANUAL TEST CHECKLIST in the task
report instead.
"""
import sys

from _helpers import run_module_tests  # noqa: E402 - only used for sys.path wiring + the runner

from quiz_signal import quiz_signal_action  # noqa: E402


def test_empty_input_and_widget_showing_expands_and_pulses():
    assert quiz_signal_action(has_unsent_text=False, dashboard_open=False) == "expand_and_pulse"


def test_empty_input_but_dashboard_already_open_pulses_only():
    """Expanding here would call load_url() on a page that's already showing - reloading it and
    wiping the on-screen chat log, and resetting its size/position for nothing."""
    assert quiz_signal_action(has_unsent_text=False, dashboard_open=True) == "pulse_only"


def test_unsent_text_and_widget_showing_pulses_only():
    """Never move/replace the window out from under her mid-typing, even though the widget (not
    the dashboard) is what's currently showing."""
    assert quiz_signal_action(has_unsent_text=True, dashboard_open=False) == "pulse_only"


def test_unsent_text_and_dashboard_open_pulses_only():
    assert quiz_signal_action(has_unsent_text=True, dashboard_open=True) == "pulse_only"


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])

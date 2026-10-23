"""
Pure decision for the 16:00 quiz-due signal (desktop.py's _watch_quiz_due): whether to actually
expand the dashboard window, or just pulse the orb without moving anything.

Deliberately dependency-free (no pywebview, no jarvis_core, no imports at all) so it can be unit
tested without ever touching a real window - see tests/test_quiz_signal.py.
"""


def quiz_signal_action(has_unsent_text, dashboard_open):
    """
    "expand_and_pulse" only when BOTH the chat input is empty AND the small widget (not the
    dashboard) is currently showing. Either other condition alone is enough to hold back:
    - unsent text: never move/replace the window out from under her mid-typing.
    - the dashboard already open: _do_expand() works by calling load_url() on the SAME window -
      calling it again while the dashboard is already showing would reload that page from
      scratch, wiping the on-screen chat log, and would also reset its size/position for no
      reason (both are set unconditionally as part of _do_expand()).
    Either way, "pulse_only" still happens - the persistent banner (see app.js) is what she'll
    see once she opens/looks back at the dashboard herself.
    """
    if not has_unsent_text and not dashboard_open:
        return "expand_and_pulse"
    return "pulse_only"

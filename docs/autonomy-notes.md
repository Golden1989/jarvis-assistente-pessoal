# Notes for the autonomy stage

Open items to revisit when Jarvis is allowed to act on its own, not just answer.

## `create_calendar_event` is a side-effecting action with no guard

Status as of 2026-09-19: **unguarded.**

- The memory-injection guard (`MemoryGuard` in `jarvis_core.py`) holds
  `remember` / `update_notes` for confirmation after a round that used
  untrusted content (web search, `read_file`, `list_files`, screenshots,
  calendar reads). It does **not** cover `create_calendar_event`.
- That tool writes to a real, external system (Google Calendar) with a title,
  times and description supplied by the model. Text from a web page, a file
  or a calendar entry that the model has just read could steer those fields
  (for example, a hidden instruction in a page to create an event).
- Today the exposure is limited: the tool only creates events on her own
  calendar (no attendees, no deletion, no edits), and the model is told to
  act on what she asks. It is still an action taken in the world that she
  never sees before it happens.

Options for the autonomy stage:

1. Reuse the pending-write mechanism: in a tainted round (or the following
   message), hold `create_calendar_event`, show the exact title/start/end in
   the reply text (written by code, not by the model), and create it only when
   she clearly says yes in her next message.
2. Ask for confirmation on every event regardless of taint, if the friction is
   acceptable by voice.
3. Any future side-effecting tool (send message, edit or delete events, write
   files, run commands) should be designed behind the same hold-and-confirm
   gate from the start rather than retrofitted.

The general rule to keep: **untrusted text may inform an answer, but must
never be able to cause an action or a stored fact without her explicit yes.**

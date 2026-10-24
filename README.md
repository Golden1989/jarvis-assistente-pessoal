# Jarvis — personal assistant

An AI assistant to talk with, debate ideas, and think things through. Built in
layers, loosely inspired by JARVIS.

**Where we are now:** well past the original "Level 2" terminal chatbot. Jarvis
runs as a terminal app, a web dashboard, or an always-on-top desktop widget
with voice and a local wake word, all sharing one brain (`jarvis_core.py`).
Besides conversation, memory, and Google Calendar, it can: control Spotify
playback (device-aware, picks the right computer or the Web Player on its
own), run a daily study/quiz engine across a few subjects, write a morning
tech-news briefing, open a small whitelisted set of local apps, enforce a
daily spending cap on the Claude API, and — newest — look into and safely
write to a set of folders you configure yourself (list/read/create/edit/move
files, with an undo command), gated by a hold-and-confirm system for anything
that isn't a plain read. See **Next steps** at the bottom for what's planned
after this.

## Requirements

- Python 3.12+
- An Anthropic API key (https://platform.claude.com/settings/keys)
- Optional, only if you want Spotify control: a Spotify Premium account and
  a Spotify Developer app (see **Spotify setup** below).
- Optional, only if you want `read_file` to open PDF/Word/PowerPoint files:
  `pypdf`, `python-docx`, `python-pptx` (already in `requirements.txt`, but
  install them yourself — see **Setup** step 1 and run `pip-audit`
  afterwards).

## Setup

1. Create the environment and install dependencies:

   ```
   conda create -n jarvis python=3.12
   conda activate jarvis
   pip install -r requirements.txt
   pip-audit
   ```

2. Set the API key as an environment variable (never put the key in the code):

   - **Windows:** Control Panel → "Edit environment variables for your account"
     → new user variable `ANTHROPIC_API_KEY` with the value `sk-ant-...`
   - **Linux/macOS:** `export ANTHROPIC_API_KEY="sk-ant-..."`

   Close and reopen the terminal (and the editor) after creating the variable.

3. Optional, only if you want the file tools to be able to see anything: copy
   `autonomy_scope.example.json` → `autonomy_scope.json` and fill in your real
   folder paths (see **File autonomy** below). Until this file exists,
   `list_files`/`read_file`/`create_file`/`edit_file`/`move_file` have nothing
   to work with.

## Usage

**Terminal:**

```
python jarvis.py
```

Type `quit` to exit. The running session cost is shown after every reply.

**Web UI (JARVIS-style dashboard):**

```
python web/server.py
```

Opens `http://127.0.0.1:5000` in your browser automatically. Local-only -
binds to `127.0.0.1`, not your network. A HUD dashboard around a central
animated 3D core (a hand-rolled canvas wireframe sphere with a heartbeat-style
pulse): side panels show real session usage, available tools (with a ping
when one fires), and your next Google Calendar event; the activity log
doubles as the conversation. Both UIs share the same brain (`jarvis_core.py`)
and tools; use whichever you're in the mood for.

**Desktop widget:**

```
python web/desktop.py
```

Runs the same server, but shows Jarvis as a small always-on-top floating
orb (draggable) instead of opening a browser tab - click it to expand into
the full dashboard, click the "-" in the dashboard's top bar to shrink back
down. Useful so the dashboard itself doesn't get in the way when you ask
Jarvis to look at your screen. Needs `pywebview` (in `requirements.txt`).

Also listens for **"Hey Jarvis"** in the background - fully local (see
"Wake word" below) - and runs a full spoken back-and-forth once it hears it,
no clicking anything.

**Voice, in the browser tab / dashboard:** the mic button uses your
browser's built-in speech recognition (free, Chrome/Edge) - toggle PT/EN
next to it to match what you're about to say. Jarvis speaks replies back
with the browser's built-in text-to-speech, auto-picking the voice language
from the reply. Mute with the speaker icon. No paid API involved on either
side.

**Wake word, in the desktop widget:** "Hey Jarvis" detection runs locally
via `openWakeWord` (`web/wake_word.py`) - no account, no cloud, audio is
scored in memory and discarded. The browser's speech *recognition* doesn't
work inside the embedded widget window (it needs a Google service that
embedded browsers can't reach), so once woken, your actual question is
transcribed locally too, with `faster-whisper` (`web/voice_capture.py`,
auto-detects English/Portuguese). Only the reply is spoken through the
browser (speech *synthesis* works fine there). After replying, it keeps
listening for a follow-up for about 90 seconds before going back to sleep -
say "Hey Jarvis" again any time to wake it back up.

**Modes:** normal (Claude Sonnet) and serious (Claude Opus, for when you want
a more careful answer) - say "serious mode" / "back to normal" (or the
Portuguese equivalents). Serious mode switches itself off after 15 minutes of
silence. A running daily spending cap on the Claude API (`DAILY_LIMIT_USD` in
`jarvis_core.py`) blocks further calls once hit - say "allow more today" to
add $1 and keep going, or it resets at midnight.

### Optional personal context

Create these files in the project root; all are git-ignored, so your
personal details never reach the repository. Any of them can be skipped.

- `personality.txt` — who Jarvis is: tone, humor, how it should behave.
- `profile.txt` — about you: name, pronouns, interests, how you like to be
  addressed.
- `people.txt` — the people you mention often (family, friends), so Jarvis
  has context without you re-explaining every time.
- `notes.txt` — facts Jarvis remembers across sessions, organized under
  `## PROJECTS` / `## EXAMS` / `## PREFERENCES` / `## PERSONAL` / `## LOG`
  headings. You can edit it by hand; Jarvis also writes to it via `remember`
  (add one fact under a category) and `update_notes` (rewrite the whole
  file - used to prune or consolidate, always after confirming with you).
  It periodically checks time-bound entries (exams, project deadlines) and
  asks if they're still relevant instead of carrying stale info forever.

All four are loaded into the system prompt at startup.

### Tools

- `web_search` — a server tool; Anthropic runs the search and returns cited
  results. Billed at $10 per 1,000 searches, on top of token costs. Capped
  at 3 searches per request (see `TOOLS` in `jarvis_core.py`).
- `get_current_datetime` — a custom tool; Jarvis doesn't otherwise know
  today's date, so it calls this to check.
- `remember` / `update_notes` — categorized memory in `notes.txt` (see above)
  that persists into future sessions.
- `list_files` / `read_file` — look inside whatever folders you've configured
  in `autonomy_scope.json` (see **File autonomy** below). Plain text is read
  directly; `.pdf`/`.docx`/`.pptx` are extracted (code-side, in an isolated,
  timeout-bound subprocess) if you installed the optional libraries. Nothing
  outside those configured folders, or matching a built-in deny-list
  (credentials, `.git`, SSH keys, browser profile files, …), is ever reachable
  - enforced in code, not just by a prompt.
- `create_file` / `edit_file` / `move_file` — create a new file, replace an
  exact piece of text in an existing one, or move/rename one, inside whichever
  of your configured folders are marked `read_write`. **Always** held for your
  explicit "yes" first, even in an otherwise clean message - nothing writes
  until you confirm exactly what was shown to you. Say "undo last file
  change" to reverse the single most recent one (only if the file hasn't
  changed since - never a blind overwrite).
- `open_app` — opens one of a small, fixed set of apps you've configured in
  `app_whitelist.json` (copy `app_whitelist.example.json` to set it up). Never
  guesses an app name; an app that accepts a folder argument only ever opens
  one inside `ALLOWED_FOLDER` (`jarvis_core.py`, defaults to `C:\Projetos`).
- `spotify` — play/pause/resume/next/previous/volume/status on Spotify
  Premium (one-time setup: see **Spotify setup** below). Picks the right
  computer automatically when more than one is logged into the same account,
  with a Web Player fallback if the desktop app can't be targeted; asks
  instead of guessing when a search is ambiguous.
- `create_calendar_event` / `list_calendar_events` — read and create events
  on your real Google Calendar (see setup below). Reminders come for free
  from whatever default notification settings you already have on Google
  Calendar - Jarvis doesn't need to manage those itself.
- `look_at_screen` — takes one screenshot of your whole screen when you
  explicitly ask ("look at my screen", "look at this"), never on its own.
  Resized before sending so it stays within Claude's image limits; nothing
  is saved to disk, and Anthropic doesn't retain or train on the image.
- `confirm_pending_action` — how Jarvis confirms a held action after you say
  "yes" (see **Hold and confirm**, below). You never call this directly.

### Hold and confirm

Some actions are too easy to get wrong from a single message, so they're held
instead of running immediately, and only go through once you clearly say
"yes"/"sim" in your *next* message:

- Anything the model proposes right after it used `web_search`, `read_file`,
  `list_files`, `look_at_screen`, or `list_calendar_events` - that round (and
  one more) is treated as having touched untrusted content, so `remember`,
  `update_notes`, `open_app`, `create_calendar_event`, and `spotify` all wait
  for your yes.
- `create_file`, `edit_file`, and `move_file` are held **every single time**,
  clean round or not - there's no fast path for file writes yet.

Say "stop" / "cancel" any time to discard whatever's currently held, no API
call needed. A held action is re-validated from scratch at confirm time, not
trusted from when it was first proposed - if the file changed, the app got
removed from the whitelist, or the folder became read-only in the meantime,
confirming refuses instead of doing something stale.

### Study & quiz

Save a summary or topic under a subject, and Jarvis quizzes you on it:

- `save summary <subject>: <text>` / `salvar resumo <assunto>: <texto>` and
  `add topic <subject>: <text>` / `adicionar tópico <assunto>: <texto>` -
  subjects today: forensics, cyber, networks, data science (aliases, accents
  and case-insensitive - see `STUDY_SUBJECT_ALIASES` in `jarvis_core.py`).
- `study material` - what's saved, and when, never the contents.
- `quiz me` / `iniciar perguntas` - three questions, prioritized toward
  what you got wrong before or haven't been asked yet; `review` / `revisão`
  for a hint first (max twice per question); `end quiz` / `encerrar
  perguntas` to stop early.
- A banner offers the quiz automatically at 16:00 if you haven't done one yet
  that day - `snooze` / `soneca` (15 min) or `skip today` / `pular hoje`.
- `undo last study entry` removes the most recently saved summary/topic.

### Spotify setup (one-time, optional)

1. Create an app at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
   set its Redirect URI to exactly `http://127.0.0.1:8888/callback`.
2. Set `SPOTIFY_CLIENT_ID` as an environment variable (same way as the
   Anthropic key above).
3. Run `python spotify_login.py` once. It opens your browser to approve
   access, then saves `spotify_token.json` for future runs.

`spotify_token.json` is git-ignored - treat it like a password.

### Google Calendar setup (one-time)

1. In [Google Cloud Console](https://console.cloud.google.com/workspace-api),
   create a project and enable the Calendar API.
2. Configure the OAuth consent screen (**Google Auth Platform** → Branding /
   Audience): type **External**, add yourself as a **test user**.
3. **Google Auth Platform** → **Clients** → Create Client → **Desktop app** →
   download the JSON, save it as `credentials.json` in the project root.
4. Run `python calendar_login.py` once. It opens your browser to sign in and
   approve access, then saves `token.json` for future runs.

`credentials.json` and `token.json` are both git-ignored - treat them like
passwords; they grant access to your real calendar.

### File autonomy (new)

Jarvis's file tools only ever reach folders YOU list in `autonomy_scope.json`
(copy `autonomy_scope.example.json` to create it - git-ignored, so your real
folder paths never reach the repo). Each entry says a folder's path, whether
it's `read_only` or `read_write`, and optionally a study subject:

```jsonc
{
  "folders": [
    {"path": "C:\\Caso_Forense", "mode": "read_only", "subject": "forensics"},
    {"path": "C:\\Users\\you\\OneDrive\\Documentos\\ciencia_dados", "mode": "read_write", "subject": "data science"}
  ]
}
```

A few things that are true regardless of what you configure:

- A fixed deny-list (credentials, `.env`, `.git`, SSH/cloud keys, browser
  profile files, …) is blocked everywhere, even inside a `read_write` folder,
  and can't be overridden by the config.
- A set of specific files Jarvis itself depends on (`notes.txt`,
  `study_data.json`, `spotify_token.json`, `token.json`, `autonomy_scope.json`
  itself, its own undo log, …) is hard-blocked too - no tool can ever read or
  write them, however a folder is configured.
- Writing is an allowlist, not just "whatever `read_write` implies": only
  `.md`/`.txt`/`.csv`/`.json` can be created or edited, and `move_file` won't
  move a file to or from a script/executable type - never `.bat`/`.py`/`.exe`/etc.
- Every create/edit/move is recorded in an undo log (`undo_log.json` +
  `backups/`, kept outside the repo and outside OneDrive, in
  `C:\jarvis_workspace`) with a hash of the result. "Undo last file change"
  reverses the single most recent one - but only if the file's current
  content still matches that hash; if it changed since, it refuses and tells
  you instead of overwriting something you didn't expect.
- `edit_file` never rewrites a whole file: it replaces one exact piece of
  text you (or Jarvis) copied from it, and refuses if that text doesn't
  appear in the file exactly once.

This is the foundation step (internally: "Stage A") - see **Next steps**
below for what's intentionally not built yet.

### Helper scripts

- `smoke_test.py` — a single API call, to check that the environment works.
- `list_models.py` — lists the models available to your account.
- `calendar_login.py` — one-time Google Calendar authorization (see above).
- `spotify_login.py` — one-time Spotify authorization (see above).

## Project layout

- `jarvis_core.py` — the shared brain: system prompt, tools, tool-use loop,
  the hold/confirm guard, mode state, study/quiz commands.
- `jarvis.py` — terminal UI.
- `web/` — web UI: `server.py` (Flask), `desktop.py` (floating widget +
  wake word entry point), `wake_word.py` (local "Hey Jarvis" detection),
  `voice_capture.py` (local speech-to-text for the widget), `templates/`,
  `static/` (`app.js`, `widget.js`, `orb.js`, `style.css`).
- `autonomy_scope.py` / `file_ops.py` / `undo_log.py` / `extract_worker.py` —
  the file-autonomy system: per-folder permission config, the actual file
  tools, the undo log, and the isolated PDF/DOCX/PPTX extraction worker.
- `app_launcher.py` — the whitelisted local-app opener behind `open_app`.
- `spotify_control.py` / `spotify_login.py` — Spotify Web API integration.
- `study_store.py` / `quiz_trigger.py` — study-entry storage and the daily
  16:00 quiz-offer trigger.
- `daily_spend.py` — the persisted running total behind the daily spending cap.
- `briefing_cache.py` / `news_sources.py` — the morning news briefing's cache
  and feed fetching.
- `google_calendar.py` / `calendar_login.py` — Google Calendar integration.
- `tests/` — the whole test suite (`run_tests.py` to run it all; no pytest).

## Next steps

- **Stage B of file autonomy**: Confirm/Cancel **buttons** on the dashboard
  for a held file write, as an alternative to saying "yes" out loud -
  designed but not yet built, with its own security requirements (CSRF-style
  Host/Origin + per-session token checks, confirming only the specific item
  shown, never trusting a stale pending-age) written down for when that
  starts.
- Unify `open_app`'s folder argument with `autonomy_scope.json` instead of
  the separate, single-folder `ALLOWED_FOLDER` constant it still uses today.
- Broader, permissioned actions beyond files (running programs/scripts,
  installing things) - with confirmation before anything that executes or
  changes the system, same spirit as the file tools.
- Window shaped like an actual circle (not just a square panel) for the
  widget, via Windows-specific window-region clipping.

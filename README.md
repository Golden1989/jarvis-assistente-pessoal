# Jarvis — personal assistant

An AI assistant to talk with, debate ideas, and think things through. Built in
layers, loosely inspired by JARVIS.

**Level 2 (current):** terminal chatbot with conversation history and tool
use, using the Anthropic API (Claude Sonnet). Jarvis can search the web,
knows the current date/time, remembers facts across sessions, can look into
one local folder, and can read/create events on your Google Calendar.

## Requirements

- Python 3.12+
- An Anthropic API key (https://platform.claude.com/settings/keys)

## Setup

1. Create the environment and install dependencies:

   ```
   conda create -n jarvis python=3.12
   conda activate jarvis
   pip install -r requirements.txt
   ```

2. Set the API key as an environment variable (never put the key in the code):

   - **Windows:** Control Panel → "Edit environment variables for your account"
     → new user variable `ANTHROPIC_API_KEY` with the value `sk-ant-...`
   - **Linux/macOS:** `export ANTHROPIC_API_KEY="sk-ant-..."`

   Close and reopen the terminal (and the editor) after creating the variable.

## Usage

```
python jarvis.py
```

Type `quit` to exit. The running session cost is shown after every reply.

### Optional personal context

Create these files in the project root; all are git-ignored, so your
personal details never reach the repository. Any of them can be skipped.

- `profile.txt` — about you: name, pronouns, interests, how you like to be
  addressed.
- `people.txt` — the people you mention often (family, friends), so Jarvis
  has context without you re-explaining every time.
- `notes.txt` — facts Jarvis should remember across sessions. You can edit
  it by hand, but Jarvis also writes to it itself via the `remember` tool
  whenever you mention something worth keeping (a project, an exam date).

All three are loaded into the system prompt at startup.

### Tools

- `web_search` — a server tool; Anthropic runs the search and returns cited
  results. Billed at $10 per 1,000 searches, on top of token costs. Capped
  at 3 searches per request (see `TOOLS` in `jarvis.py`).
- `get_current_datetime` — a custom tool; Jarvis doesn't otherwise know
  today's date, so it calls this to check.
- `remember` — appends a fact to `notes.txt` so it persists into future
  sessions.
- `list_files` / `read_file` — let Jarvis look inside one folder on your
  disk, set by `ALLOWED_FOLDER` in `jarvis.py` (defaults to `C:\Projetos`).
  Scoped deliberately to one folder rather than the whole disk; change the
  constant if you want it looking elsewhere. Path traversal outside that
  folder (e.g. `..\..\Windows`) is rejected.
- `create_calendar_event` / `list_calendar_events` — read and create events
  on your real Google Calendar (see setup below). Reminders come for free
  from whatever default notification settings you already have on Google
  Calendar - Jarvis doesn't need to manage those itself.

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

### Helper scripts

- `smoke_test.py` — a single API call, to check that the environment works.
- `list_models.py` — lists the models available to your account.
- `calendar_login.py` — one-time Google Calendar authorization (see above).

## Roadmap

- **Level 3:** fixed persona and memory that persists across sessions.

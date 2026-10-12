"""
Google Calendar integration: OAuth login and event read/create.

First-time setup: run `calendar_login.py` once. It opens your browser to
authorize Jarvis, then caches a refresh token in token.json so future runs
don't need to log in again.
"""

import datetime
import os.path
import threading
import time
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Read/write access to events only - not full calendar management (least privilege).
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Anchored to this file's own folder (the project root), NOT the current
# working directory - desktop.py runs with its CWD inside web/, which made
# these unreachable with plain relative paths.
_BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = str(_BASE_DIR / "credentials.json")
TOKEN_FILE = str(_BASE_DIR / "token.json")

# Local timezone used for events you create. Change if you're not in Brazil.
TIMEZONE = "America/Sao_Paulo"

_service = None  # cached Calendar API client for this process
# The Google client (httplib2 underneath) is NOT thread-safe, and Flask serves requests on
# several threads (/status, /chat tools). Two threads inside it at once can crash python.exe
# in OpenSSL (libcrypto, 0xc0000005) - so every use goes through this lock. RLock: get_service()
# is called while the lock is already held.
_lock = threading.RLock()
_login_thread = None  # the background interactive-login thread, if one is currently running
# The dashboard asks /status often and the next event rarely changes: remember the answer for a
# minute. create_event() forgets it, so an event she just created shows up right away.
NEXT_EVENT_CACHE_SECONDS = 60
_next_event_cache = None  # (time.monotonic(), days_ahead, value)


class NotLoggedIn(Exception):
    """Credentials are missing or dead and nobody asked for the interactive login flow - see
    get_service(allow_interactive_login=...). Caught the same way every other Google error already
    is (create_event/list_events/next_event's callers all wrap these calls in try/except)."""


def _start_background_login():
    """
    Starts the SAME interactive OAuth flow calendar_login.py runs (opens her real browser, not
    the embedded widget) on a daemon thread, unless one is already in flight - never a second
    browser tab for one dead login. Never blocks the caller. Returns True if this call is the one
    that just started it, False if one was already running (so the caller can word its message
    accordingly: "I just opened a tab" vs "there's already one waiting").

    Deliberately does NOT hold _lock while waiting on run_local_server() (which can sit there for
    as long as she takes to finish logging in) - only _service/TOKEN_FILE, right at the end, need
    the lock's protection. Holding it the whole time would block any OTHER calendar request made
    while she's still logging in, which is just the original hang again for a second message
    instead of the first.
    """
    global _login_thread, _service
    if _login_thread is not None and _login_thread.is_alive():
        return False

    def run():
        try:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)  # opens her browser; blocks only THIS thread
            global _service
            with _lock:
                with open(TOKEN_FILE, "w", encoding="utf-8") as token:
                    token.write(creds.to_json())
                _service = build("calendar", "v3", credentials=creds)
            print("[calendar] re-authorized in the background", flush=True)
        except Exception as error:
            print(f"[calendar] background login failed: {error}", flush=True)

    _login_thread = threading.Thread(target=run, daemon=True)
    _login_thread.start()
    return True


def get_service(allow_interactive_login=False, trigger_background_login=True):
    """
    Return an authorized Google Calendar API client, authenticating if needed.

    allow_interactive_login=True opens a browser and BLOCKS waiting for her to complete the
    OAuth flow - only calendar_login.py (a script she runs herself, watching the terminal) and
    _start_background_login()'s own thread pass this. Every other caller is code running inside
    the live app (a /chat tool call, /status), where popping a browser and blocking the server's
    one request-handling thread on a login nobody is expecting is a silent hang, not a feature -
    this is exactly what happened: a dead refresh token used to fall through to the interactive
    flow here too, and the whole app froze "thinking" on a browser window she never knew to go
    complete. Default False instead raises NotLoggedIn right away.

    trigger_background_login=True (the default) also fires that browser login automatically, in
    the background, the first time this happens - so NotLoggedIn's own message can tell her it's
    already open rather than just pointing at calendar_login.py. next_event() (used by /status,
    polled constantly just by having the dashboard open) passes False: a passive background check
    should never pop a browser tab on its own, only an actual calendar request she made should.
    """
    global _service
    with _lock:
        if _service is not None:
            return _service

        if os.environ.get("JARVIS_TESTING"):
            # A test runs with JARVIS_TESTING=1 (see tests/_helpers.py, run_tests.py) and did not
            # install a fake _service itself - refuse loudly instead of reading the real
            # token.json/credentials.json and reaching the real Google API. This is the one
            # central checkpoint every public function here goes through, so it covers reading
            # the token file, refreshing it, the interactive login flow, and every real API call.
            raise RuntimeError(
                "JARVIS_TESTING is set and no fake google_calendar._service was installed - "
                "refusing to touch the real Google Calendar/token.json from a test."
            )

        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError:
                    # The refresh token itself is dead (revoked, or - common for an app still in
                    # Google's "Testing" publishing status - expired after about a week), not just
                    # the short-lived access token.
                    creds = None
            if not creds or not creds.valid:
                if not allow_interactive_login:
                    if trigger_background_login and _start_background_login():
                        raise NotLoggedIn(
                            "Google Calendar needs you to log in again - I just opened a browser "
                            "tab for it. Finish logging in there, then ask me again in a bit."
                        )
                    raise NotLoggedIn(
                        "Google Calendar needs you to log in again - check for a browser tab "
                        "already waiting for you, or run calendar_login.py."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)  # opens your browser once
            with open(TOKEN_FILE, "w", encoding="utf-8") as token:
                token.write(creds.to_json())

        _service = build("calendar", "v3", credentials=creds)
        return _service


def create_event(summary, start, end, description=""):
    """
    Create an event on the primary calendar.
    start/end: local datetime strings, e.g. "2026-10-03T14:00:00" (no offset -
    TIMEZONE above is attached separately).
    """
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start, "timeZone": TIMEZONE},
        "end": {"dateTime": end, "timeZone": TIMEZONE},
    }
    global _next_event_cache
    with _lock:
        service = get_service()
        created = service.events().insert(calendarId="primary", body=event).execute()
        _next_event_cache = None
    return created.get("htmlLink", "Event created.")


def list_events(time_min_iso, time_max_iso, max_results=15):
    """List events on the primary calendar between two RFC3339 timestamps."""
    with _lock:
        service = get_service()
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min_iso,
                timeMax=time_max_iso,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    events = result.get("items", [])
    if not events:
        return "No events in that range."
    lines = []
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        lines.append(f"- {start}: {event.get('summary', '(no title)')}")
    return "\n".join(lines)


def next_event(days_ahead=14):
    """
    Return {"summary": ..., "start": ...} for the soonest upcoming event, or
    None if there isn't one in the window. Used by the web UI's status panel.
    Cached for NEXT_EVENT_CACHE_SECONDS (a "no event" answer too); failures are never cached.
    """
    global _next_event_cache
    with _lock:
        cached = _next_event_cache
        if cached and cached[1] == days_ahead and time.monotonic() - cached[0] < NEXT_EVENT_CACHE_SECONDS:
            return cached[2]
        value = _fetch_next_event(days_ahead)
        _next_event_cache = (time.monotonic(), days_ahead, value)
        return value


def _fetch_next_event(days_ahead):
    now = datetime.datetime.now().astimezone()
    time_min = now.isoformat()
    time_max = (now + datetime.timedelta(days=days_ahead)).isoformat()

    with _lock:
        service = get_service(trigger_background_login=False)  # a passive /status check never pops a browser tab
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=1,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    events = result.get("items", [])
    if not events:
        return None
    event = events[0]
    start = event["start"].get("dateTime", event["start"].get("date"))
    return {"summary": event.get("summary", "(no title)"), "start": start}

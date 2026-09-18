"""
Google Calendar integration: OAuth login and event read/create.

First-time setup: run `calendar_login.py` once. It opens your browser to
authorize Jarvis, then caches a refresh token in token.json so future runs
don't need to log in again.
"""

import datetime
import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Read/write access to events only - not full calendar management (least privilege).
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

# Local timezone used for events you create. Change if you're not in Brazil.
TIMEZONE = "America/Sao_Paulo"

_service = None  # cached Calendar API client for this process


def get_service():
    """Return an authorized Google Calendar API client, authenticating if needed."""
    global _service
    if _service is not None:
        return _service

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
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
    service = get_service()
    created = service.events().insert(calendarId="primary", body=event).execute()
    return created.get("htmlLink", "Event created.")


def list_events(time_min_iso, time_max_iso, max_results=15):
    """List events on the primary calendar between two RFC3339 timestamps."""
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
    """
    now = datetime.datetime.now().astimezone()
    time_min = now.isoformat()
    time_max = (now + datetime.timedelta(days=days_ahead)).isoformat()

    service = get_service()
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

"""
Run this ONCE to authorize Jarvis with your Google Calendar.

Opens your browser to sign in and approve access, then saves token.json so
jarvis.py can use your calendar afterward without asking again.
"""

import google_calendar

google_calendar.get_service()
print("Google Calendar authorized. token.json saved.")

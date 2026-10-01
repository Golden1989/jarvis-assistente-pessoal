"""
Spotify playback control: OAuth (Authorization Code + PKCE, no client secret) and a thin REST
client. Playback only this stage - no playlist read/write (see autonomy notes for that stage).

First-time setup: run `spotify_login.py` once. It opens your browser to authorize Jarvis, then
caches a refresh token in spotify_token.json so future runs don't need to log in again.

SPOTIFY_CLIENT_ID must be set in the environment - never written to a file in this project.
"""
import base64
import hashlib
import json
import os
import os.path
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import app_launcher

AUTH_BASE = "https://accounts.spotify.com"
API_BASE = "https://api.spotify.com/v1"
REDIRECT_PORT = 8888
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
# Playback control only - see this stage's plan. Playlist scopes are a separate, later stage.
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"

_BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = str(_BASE_DIR / "spotify_token.json")
_DEFAULT_TOKEN_FILE = TOKEN_FILE  # captured once, at import time, before any test ever repoints it

DEVICE_WAIT_SECONDS = 8       # how long to wait for a device to appear after opening the app
DEVICE_POLL_INTERVAL = 1.0
RETRY_AFTER_MAX_WAIT = 5      # a 429 with Retry-After at or below this is waited out once and retried
MAX_NAME_CHARS = 80           # names from Spotify shown to the model are capped this short

_lock = threading.RLock()
_token = None  # cached dict: access_token, refresh_token, expires_at, scope

# The one seam every real network call goes through. Tests replace this with a fake - see
# tests/_helpers.py. As long as it's still the real urlopen, JARVIS_TESTING refuses to use it.
_opener = urllib.request.urlopen


class SpotifyError(Exception):
    """A clear, user-facing problem: not configured, no Premium, no device, rate-limited, etc."""


def _client_id():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise SpotifyError("SPOTIFY_CLIENT_ID is not set - Spotify control is not configured.")
    return client_id


def generate_pkce_pair():
    """(code_verifier, code_challenge) - token_urlsafe's alphabet (A-Za-z0-9-_) is already a
    strict subset of PKCE's allowed "unreserved" characters, so no further encoding is needed."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def authorize_url(code_challenge, state):
    params = {
        "client_id": _client_id(), "response_type": "code", "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256", "code_challenge": code_challenge, "state": state, "scope": SCOPES,
    }
    return f"{AUTH_BASE}/authorize?{urllib.parse.urlencode(params)}"


def _request(req, timeout=15):
    if os.environ.get("JARVIS_TESTING") and _opener is urllib.request.urlopen:
        # A test runs with JARVIS_TESTING=1 (see tests/_helpers.py) and did not replace _opener
        # with a fake - refuse loudly instead of ever reaching the real Spotify API.
        raise RuntimeError(
            "JARVIS_TESTING is set and no fake HTTP opener was installed for spotify_control - "
            "refusing to touch the real Spotify API from a test."
        )
    return _opener(req, timeout=timeout)


def _token_request(data):
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(f"{AUTH_BASE}/api/token", data=body,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with _request(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _refuse_if_untested_real_file():
    """Same idea as _request()'s guard, for the token FILE instead of the network: if
    JARVIS_TESTING is set and TOKEN_FILE still equals the real default (no test repointed it to a
    temp path - see tests/_helpers.py: isolate()), refuse to read or write it. The real file is
    never touched from a test even if a script forgets to fake the HTTP layer too."""
    if os.environ.get("JARVIS_TESTING") and TOKEN_FILE == _DEFAULT_TOKEN_FILE:
        raise RuntimeError(
            "JARVIS_TESTING is set and spotify_control.TOKEN_FILE still points at the real "
            "spotify_token.json - a test must repoint TOKEN_FILE to a temp path first (see "
            "tests/_helpers.py: isolate()) before this is allowed to read or write a token file."
        )


def _save_token(payload, previous_refresh=None):
    _refuse_if_untested_real_file()
    token = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or previous_refresh,
        "expires_at": time.time() + payload.get("expires_in", 3600),
        "scope": payload.get("scope", SCOPES),
    }
    if not token["refresh_token"]:
        raise SpotifyError("Spotify did not return a refresh token - run spotify_login.py again.")
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token, f)
    global _token
    _token = token
    return token


def exchange_code(code, code_verifier):
    """Used once, by spotify_login.py, right after the browser callback."""
    payload = _token_request({
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": _client_id(), "code_verifier": code_verifier,
    })
    return _save_token(payload)


def _load_token():
    global _token
    if _token is not None:
        return _token
    _refuse_if_untested_real_file()
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        _token = json.load(f)
    return _token


def _refresh(token):
    payload = _token_request({
        "grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": _client_id(),
    })
    return _save_token(payload, previous_refresh=token["refresh_token"])


def get_access_token(margin_seconds=60):
    """A valid bearer token, refreshing first if it's expired or close to it. Raises SpotifyError
    if nobody has run spotify_login.py yet."""
    with _lock:
        token = _load_token()
        if token is None:
            raise SpotifyError("Spotify isn't connected yet - run spotify_login.py once.")
        if time.time() >= token["expires_at"] - margin_seconds:
            token = _refresh(token)
        return token["access_token"]


def _force_refresh():
    with _lock:
        token = _load_token()
        if token is None:
            raise SpotifyError("Spotify isn't connected yet - run spotify_login.py once.")
        return _refresh(token)


def _parse_retry_after(headers):
    try:
        return max(1, int(headers.get("Retry-After", "1"))) if headers else 1
    except (TypeError, ValueError):
        return 1


def _premium_or_scope_message(body):
    message = ""
    if isinstance(body, dict):
        message = ((body.get("error") or {}).get("message") or "") if isinstance(body.get("error"), dict) else ""
    base = "Spotify refused that - this usually means Premium is required or a permission is missing."
    return f"{base} ({_clean_name(message, 120)})" if message else base


def _api_call(method, path, params=None, json_body=None):
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _request(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), resp.headers
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        return error.code, body, error.headers


def _authed_call(method, path, params=None, json_body=None, _refreshed=False, _rate_limited=False):
    """One call, with AT MOST one 401-refresh-retry and one 429-wait-retry - never an open loop."""
    status, body, headers = _api_call(method, path, params, json_body)
    if status == 401 and not _refreshed:
        _force_refresh()
        return _authed_call(method, path, params, json_body, _refreshed=True, _rate_limited=_rate_limited)
    if status == 429 and not _rate_limited:
        retry_after = _parse_retry_after(headers)
        if retry_after <= RETRY_AFTER_MAX_WAIT:
            time.sleep(retry_after)
            return _authed_call(method, path, params, json_body, _refreshed=_refreshed, _rate_limited=True)
        raise SpotifyError(f"Spotify is rate-limiting requests right now - try again in about {retry_after} seconds.")
    if status == 403:
        raise SpotifyError(_premium_or_scope_message(body))
    return status, body


def _with_device_recovery(call):
    """call() -> (status, body). On a 404 that really means "no active device" (confirmed by
    get_devices() being empty - a 404 with devices present is a different problem), opens Spotify,
    waits up to DEVICE_WAIT_SECONDS for a device to appear, transfers playback to it, and retries
    call() exactly once more. No background polling: this is one bounded wait inside one call."""
    status, body = call()
    if status != 404:
        return status, body
    if get_devices():
        return status, body
    app_launcher.launch({"kind": "uri", "target": "spotify:"})
    deadline = time.monotonic() + DEVICE_WAIT_SECONDS
    devices = []
    while time.monotonic() < deadline and not devices:
        time.sleep(DEVICE_POLL_INTERVAL)
        devices = get_devices()
    if not devices:
        raise SpotifyError("Spotify didn't open a playback device in time - open it yourself and try again.")
    transfer_playback(devices[0]["id"])
    return call()


def get_devices():
    status, body = _authed_call("GET", "/me/player/devices")
    if status != 200:
        return []
    return (body or {}).get("devices") or []


def transfer_playback(device_id, play=False):
    status, _ = _authed_call("PUT", "/me/player", json_body={"device_ids": [device_id], "play": play})
    if status not in (200, 204):
        raise SpotifyError(f"Could not switch Spotify playback to this device (HTTP {status}).")


def _clean_name(value, max_len=MAX_NAME_CHARS):
    """Spotify-supplied text (track/artist/playlist/album names) is untrusted data, never an
    instruction: strip control characters and newlines, collapse whitespace, cap the length."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable())
    text = " ".join(text.split())  # also collapses any former \n/\r/\t runs into single spaces
    return text[:max_len]


def _track_summary(item):
    return {
        "uri": item["uri"],
        "name": _clean_name(item.get("name")),
        "artist": _clean_name(", ".join(a.get("name", "") for a in item.get("artists", []))),
    }


def search_tracks(query, limit=5):
    status, body = _authed_call("GET", "/search", params={"q": query, "type": "track", "limit": limit})
    if status != 200:
        raise SpotifyError(f"Spotify search failed (HTTP {status}).")
    items = ((body or {}).get("tracks") or {}).get("items") or []
    return [_track_summary(t) for t in items]


def resolve_play(query):
    """
    {"uri", "display"} for the track to play, or {"ask": "..."} when the model should ask her
    instead of guessing (no match, or the top two results share a name but not an artist).
    Read-only (just a search) - safe to call even while an action is only being held, not done.
    """
    candidates = search_tracks(query)
    if not candidates:
        return {"ask": f'No track found on Spotify for "{_clean_name(query, 120)}".'}
    if len(candidates) >= 2:
        a, b = candidates[0], candidates[1]
        if a["name"].strip().lower() == b["name"].strip().lower() and a["artist"] != b["artist"]:
            listed = "; ".join(f'"{c["name"]}" by {c["artist"]}' for c in candidates[:3])
            return {"ask": f"More than one track matches that, please ask which one: {listed}."}
    top = candidates[0]
    return {"uri": top["uri"], "display": f'"{top["name"]}" by {top["artist"]}'}


def play_uri(uri, display):
    """(result_text, tainted). tainted=True: `display` is a Spotify-supplied name."""
    status, _ = _with_device_recovery(lambda: _authed_call("PUT", "/me/player/play", json_body={"uris": [uri]}))
    if status not in (200, 204):
        raise SpotifyError(f"Could not start playback (HTTP {status}).")
    return f"Playing {display}.", True


def pause():
    status, _ = _with_device_recovery(lambda: _authed_call("PUT", "/me/player/pause"))
    if status not in (200, 204):
        raise SpotifyError(f"Could not pause Spotify (HTTP {status}).")
    return "Paused.", False


def resume():
    status, _ = _with_device_recovery(lambda: _authed_call("PUT", "/me/player/play"))
    if status not in (200, 204):
        raise SpotifyError(f"Could not resume Spotify (HTTP {status}).")
    return "Resumed.", False


def next_track():
    status, _ = _with_device_recovery(lambda: _authed_call("POST", "/me/player/next"))
    if status not in (200, 204):
        raise SpotifyError(f"Could not skip to the next track (HTTP {status}).")
    return "Skipped to the next track.", False


def previous_track():
    status, _ = _with_device_recovery(lambda: _authed_call("POST", "/me/player/previous"))
    if status not in (200, 204):
        raise SpotifyError(f"Could not go back a track (HTTP {status}).")
    return "Back to the previous track.", False


def set_volume(level):
    status, _ = _with_device_recovery(
        lambda: _authed_call("PUT", "/me/player/volume", params={"volume_percent": level}))
    if status not in (200, 204):
        raise SpotifyError(f"Could not set the volume (HTTP {status}).")
    return f"Volume set to {level}%.", False


def get_playback_status():
    """(result_text, tainted). Free read - no guard, no hold - but still tainted=True whenever it
    names a real track, exactly like a web_search result would."""
    status, body = _authed_call("GET", "/me/player")
    if status == 204 or not body or not body.get("item"):
        return "Nothing is playing right now.", False
    item = body["item"]
    name = _clean_name(item.get("name"))
    artist = _clean_name(", ".join(a.get("name", "") for a in item.get("artists", [])))
    state = "Playing" if body.get("is_playing") else "Paused"
    return f'{state}: "{name}" by {artist}.', True

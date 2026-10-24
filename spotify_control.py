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
import socket
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
# A search with no market can come back with tracks flagged unavailable even though they're
# playable in hers - Spotify's own search docs call this out, and since Feb 2026 Dev Mode no
# longer returns available_markets/linked_from at all, so there's no per-track market list to
# check against anyway. This is a personal, single-country deployment, so one fixed market is fine.
SPOTIFY_MARKET = "BR"
TRANSFER_BEFORE_PLAY_WAIT_SECONDS = 1.0  # give an inactive device a moment to wake up after transfer_playback
PLAY_VERIFY_POLL_INTERVAL_SECONDS = 1.0  # how often to re-check playback state after play_uri
PLAY_VERIFY_MAX_POLLS = 4                # ~4s total budget - stop polling early as soon as is_playing is true

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
    """POSTs to the token endpoint (authorization_code or refresh_token grant). Raises
    SpotifyError - never a raw HTTPError/JSONDecodeError - on a rejected or malformed response, so
    callers (get_access_token, _do_spotify's SpotifyError handling) always see a clear message
    instead of a crash: this is exactly how a revoked/expired refresh token would show up."""
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(f"{AUTH_BASE}/api/token", data=body,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with _request(req) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            error_body = json.loads(raw) or {}
            detail = error_body.get("error_description") or error_body.get("error")
        except ValueError:
            detail = None
        raise SpotifyError(
            f"Spotify login failed (HTTP {error.code}){': ' + _clean_name(detail, 120) if detail else ''} - "
            "try spotify_login.py again."
        ) from error
    try:
        return json.loads(raw)
    except ValueError as error:
        raise SpotifyError("Spotify's token endpoint returned something unreadable - try again.") from error


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
            status, headers = resp.status, resp.headers
    except urllib.error.HTTPError as error:
        raw = error.read()
        status, headers = error.code, error.headers
    try:
        # play/pause/next/previous/volume normally answer 204 with an empty body, but a success
        # status is no guarantee of a valid (or any) JSON body in practice - a non-empty-but-not-
        # JSON response here must never crash the whole /chat request (this really happened: an
        # intermediate proxy/CDN hop can still attach a stray byte or two to an officially "empty" 204).
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    return status, body, headers


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


def _choose_play_device_id(devices):
    """None (let Spotify pick - today's old behavior) | a device id to pass explicitly to
    play_uri(). Without an explicit device_id, PUT .../play plays on WHATEVER device Spotify
    considers active - which can silently be her phone or ANOTHER COMPUTER on the same account,
    not this one (this really happened: once with a phone active, once with two PCs on the same
    account and the wrong one picked, and once with the desktop app itself broken for starting
    playback, needing the browser's Web Player instead - the Web Player's device name looks like
    "Web Player (Chrome)" and will never equal this machine's hostname).

    Among non-restricted "Computer" devices, in order:
      (a) a device whose name matches this machine's hostname (socket.gethostname(), compared
          stripped and case-insensitively) - preferring the is_active one if more than one
          matches - UNLESS a Web Player device (name starts with "Web Player", case-insensitive)
          is currently active, in which case that active Web Player wins instead (the desktop app
          can be broken while the Web Player in the browser is what's actually in use);
      (b) otherwise, if exactly one Computer device is active, use it;
      (c) otherwise, if exactly one Computer device is a Web Player, use it;
      (d) otherwise, if exactly one Computer device exists at all, use it (nothing to disambiguate);
      (e) otherwise, several Computers and nothing above picked one - do not guess which is "this"
          one - raise SpotifyError instead.
      otherwise (no Computer devices at all) return None, so the existing 404 recovery in
      _with_device_recovery (open Spotify, wait, transfer, retry) still applies unchanged.

    The device NAME is used only for the hostname/"Web Player" comparisons above - never printed,
    logged, or returned to the model, exactly like every other Spotify-supplied string in this
    file. Only counts and booleans derived from it are ever logged."""
    computers = [d for d in devices if d.get("type") == "Computer" and not d.get("is_restricted")]
    hostname = socket.gethostname().strip().lower()
    hostname_matches = [d for d in computers if str(d.get("name") or "").strip().lower() == hostname]
    web_players = [d for d in computers if str(d.get("name") or "").strip().lower().startswith("web player")]
    active_web_players = [d for d in web_players if d.get("is_active")]
    print(
        f"[spotify] device selection: computers={len(computers)} "
        f"hostname_matched={bool(hostname_matches)} web_player_found={bool(web_players)}",
        flush=True,
    )

    if not computers:
        return None
    if active_web_players:
        return active_web_players[0].get("id")
    if hostname_matches:
        for device in hostname_matches:
            if device.get("is_active"):
                return device.get("id")
        return hostname_matches[0].get("id")
    active_computers = [d for d in computers if d.get("is_active")]
    if len(active_computers) == 1:
        return active_computers[0].get("id")
    if len(web_players) == 1:
        return web_players[0].get("id")
    if len(computers) == 1:
        return computers[0].get("id")
    raise SpotifyError(
        "I found more than one computer on your Spotify account and can't tell which one is this one."
    )


def _confirm_play_result(display):
    """
    (result_text, tainted=True). Called right after play_uri()'s PUT succeeds (200/204) - a
    success status there is NOT proof anything is actually playing on this computer, only that
    Spotify accepted the command. An idle device that was just woken up by transfer_playback can
    take a moment to actually start, so this polls GET /me/player every
    PLAY_VERIFY_POLL_INTERVAL_SECONDS, up to PLAY_VERIFY_MAX_POLLS times, and stops as soon as
    is_playing is true - never waiting out the full budget once playback is confirmed.

    Checks ONLY booleans/numbers - is_playing, the active device's TYPE, volume, whether an item
    is loaded, the disallows count, and whether the device's name matches this machine's hostname
    - never the name or id itself, which stay untrusted and unlogged here exactly as everywhere
    else in this file.

    Never raises: if the verification call itself fails, that is reported as "could not be
    confirmed", not as an exception - a flaky read right after a real play must never turn into a
    crashed /chat request over something this unimportant.
    """
    body = None
    for _attempt in range(PLAY_VERIFY_MAX_POLLS):
        time.sleep(PLAY_VERIFY_POLL_INTERVAL_SECONDS)
        try:
            _, body = _authed_call("GET", "/me/player")
        except Exception as error:
            print(f"[spotify] play verification call failed: {error}", flush=True)
            return f"Spotify accepted the command to play {display}, but playback could not be confirmed.", True
        if body and body.get("is_playing"):
            break

    is_playing = bool(body and body.get("is_playing"))
    device = (body or {}).get("device") or {}
    device_type = device.get("type")
    volume_percent = device.get("volume_percent")
    item_loaded = bool(body and body.get("item"))
    disallows = ((body or {}).get("actions") or {}).get("disallows") or {}
    disallow_count = sum(1 for value in disallows.values() if value)
    hostname = socket.gethostname().strip().lower()
    is_this_computer = device_type == "Computer" and str(device.get("name") or "").strip().lower() == hostname
    print(
        f"[spotify] play verification: is_playing={is_playing} device_type={device_type or 'none'} "
        f"is_this_computer={is_this_computer} item_loaded={item_loaded} "
        f"volume_percent={volume_percent if volume_percent is not None else 'none'} disallows={disallow_count}",
        flush=True,
    )

    if is_playing and is_this_computer:
        return f"Playing {display} on this computer.", True
    if is_playing and device_type == "Computer":
        # Could genuinely be the Web Player in HER OWN browser on this machine - its device name
        # never equals the hostname, so this can't tell the difference and says so honestly
        # instead of claiming "another device" when it might really be right here in the browser.
        return (
            f"Spotify accepted the command to play {display}, but it's playing on another computer "
            "or in the browser.",
            True,
        )
    if is_playing:
        return f"Spotify accepted the command to play {display}, but it's playing on another device.", True
    return (
        f"Spotify accepted the command to play {display}, but it did not start on this computer. "
        "Spotify might be in offline mode, or playback may have gone to another computer on this account.",
        True,
    )


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
        # None (field absent) means "unknown" - treated as playable, same as an explicit True.
        # Only an explicit False means Spotify itself says this track can't be played for her.
        "is_playable": item.get("is_playable"),
    }


def search_tracks(query, limit=5):
    # Dev Mode's own max is 10 - 5 is plenty for resolve_play's ambiguity check either way.
    status, body = _authed_call(
        "GET", "/search", params={"q": query, "type": "track", "limit": limit, "market": SPOTIFY_MARKET}
    )
    if status != 200:
        raise SpotifyError(f"Spotify search failed (HTTP {status}).")
    items = ((body or {}).get("tracks") or {}).get("items") or []
    results = [_track_summary(t) for t in items]
    top_is_playable = results[0]["is_playable"] if results else None
    print(
        f"[spotify] search: results={len(results)} "
        f"top_playable={'unknown' if top_is_playable is None else top_is_playable}",
        flush=True,
    )
    return results


def resolve_play(query):
    """
    {"uri", "display"} for the track to play, or {"ask": "..."} when the model should ask her
    instead of guessing (no match, no PLAYABLE match, or the top two playable results share a
    name but not an artist). Read-only (just a search) - safe to call even while an action is
    only being held, not done.
    """
    candidates = [c for c in search_tracks(query) if c["is_playable"] is not False]
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
    """
    (result_text, tainted). tainted=True: `display` is a Spotify-supplied name.

    Plays on an explicit device when one can be confidently chosen (_choose_play_device_id) -
    PUT .../play with no device_id lets Spotify pick ANY of her devices (phone, another
    computer, ...), which is exactly how a "successful" 204 once silently started playback
    somewhere she wasn't looking. _choose_play_device_id can raise SpotifyError itself when
    there's more than one computer on the account and none matches this machine's hostname -
    that propagates up unchanged, same as any other SpotifyError here.

    An explicitly chosen device that is NOT currently active can just ignore a remote play
    command (this really happened: a clean 204, but is_playing stayed false) - a desktop app
    that's idle needs playback actually transferred to it first. So when the chosen device isn't
    active, this transfers to it (without starting playback yet), waits
    TRANSFER_BEFORE_PLAY_WAIT_SECONDS for it to wake up, then sends the play.

    If the play itself still comes back 404 with an explicit device_id, the device may have gone
    away between the lookup above and the play call - get_devices() is re-read, and if the chosen
    device is still listed, this transfers to it once more and retries the play exactly once
    before giving up with a clear message. The 404 "no active device at ALL" recovery
    (_with_device_recovery: open Spotify, wait, transfer, retry) still applies unchanged.

    The 200/204 itself is still only "Spotify accepted the command" - see _confirm_play_result
    for the actual verification before this ever tells her it's playing.
    """
    devices = get_devices()
    device_id = _choose_play_device_id(devices)
    chosen_device_was_active = None
    if device_id:
        chosen_device_was_active = any(d.get("id") == device_id and d.get("is_active") for d in devices)
        if not chosen_device_was_active:
            transfer_playback(device_id, play=False)
            time.sleep(TRANSFER_BEFORE_PLAY_WAIT_SECONDS)
    print(
        f"[spotify] play: {'explicit Computer device' if device_id else 'no explicit device'} "
        f"chosen_device_was_active={chosen_device_was_active}",
        flush=True,
    )
    params = {"device_id": device_id} if device_id else None

    def _do_play():
        return _authed_call("PUT", "/me/player/play", params=params, json_body={"uris": [uri]})

    status, _ = _with_device_recovery(_do_play)
    if status == 404 and device_id and any(d.get("id") == device_id for d in get_devices()):
        transfer_playback(device_id)
        status, _ = _do_play()
    if status not in (200, 204):
        raise SpotifyError(f"Could not start playback (HTTP {status}).")
    return _confirm_play_result(display)


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

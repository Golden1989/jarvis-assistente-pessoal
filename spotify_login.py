"""
Run this ONCE to authorize Jarvis for Spotify playback control (play/pause/skip/volume - no
playlist access this stage). Opens your browser to sign in and approve, then saves
spotify_token.json so later runs don't need to log in again.

Needs SPOTIFY_CLIENT_ID in the environment, and the app's Redirect URI in the Spotify dashboard
set to exactly http://127.0.0.1:8888/callback.
"""
import secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import spotify_control

LOGIN_TIMEOUT_SECONDS = 120  # give up waiting for the browser round-trip after this long


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        self.server.result = query
        ok = "code" in query
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><p>Jarvis is authorized. You can close this tab.</p></body></html>" if ok else
            b"<html><body><p>Login failed - close this tab and try again.</p></body></html>"
        )

    def log_message(self, *args):
        pass  # keep the terminal quiet - nothing sensitive printed here anyway (no tokens)


def main():
    verifier, challenge = spotify_control.generate_pkce_pair()
    state = secrets.token_urlsafe(16)
    url = spotify_control.authorize_url(challenge, state)

    server = HTTPServer(("127.0.0.1", spotify_control.REDIRECT_PORT), _CallbackHandler)
    server.timeout = LOGIN_TIMEOUT_SECONDS
    server.result = None
    print("Opening your browser to authorize Jarvis for Spotify (playback control only)...")
    webbrowser.open(url)
    server.handle_request()  # blocks for exactly one request, or until LOGIN_TIMEOUT_SECONDS

    if not server.result:
        print(f"No response within {LOGIN_TIMEOUT_SECONDS}s - try again.")
        return
    if "error" in server.result:
        print(f"Spotify login failed: {server.result['error'][0]}")
        return
    if server.result.get("state", [None])[0] != state:
        print("State did not match this login attempt (possible stray request) - try again.")
        return
    if "code" not in server.result:
        print("No authorization code in the callback - try again.")
        return

    spotify_control.exchange_code(server.result["code"][0], verifier)
    print("Spotify authorized. spotify_token.json saved.")


if __name__ == "__main__":
    main()

"""
Shared scaffolding for every test in this folder. No test may use a real path from the developer's
machine, a real username, or any personal data - everything here is a temp folder and fake names.
No test may call the real Anthropic or Google APIs, or actually launch a program.

Each test_* function is independent: call isolate() at its start with its own tempfile.mkdtemp(),
and it gets a clean jarvis_core/google_calendar/app_launcher state with no leftovers from any
other test. Run a whole file with `python tests/test_x.py`, or everything with `python
run_tests.py` from the project root (see run_tests.py for why not pytest - it isn't installed).
"""
import contextlib
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-real")  # never a real key; never used to call the API
os.environ.setdefault("SPOTIFY_CLIENT_ID", "fake-spotify-client-id-not-real")  # same idea
os.environ["JARVIS_TESTING"] = "1"  # google_calendar.get_service() refuses the real API unless a
                                     # test installs its own fake gc._service - see isolate() below

ROOT = Path(__file__).resolve().parent.parent  # the project root, wherever THIS CHECKOUT happens to live
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import jarvis_core as jc  # noqa: E402
import google_calendar as gc  # noqa: E402
import app_launcher as al  # noqa: E402
import spotify_control as sc  # noqa: E402
import server  # noqa: E402 - the Flask app; server.app.testing is set once, below

import urllib.request  # noqa: E402 - identity-compared against sc._opener, see isolate() below

server.app.testing = True


class NeverRealGoogle:
    """Installed as gc._service by isolate(): ANY attribute access means a test tried to reach the
    real Google API - fails loudly and immediately instead of ever silently succeeding against it.
    (This is how an earlier version of this suite accidentally created real Calendar events: a
    reset helper set gc._service back to None, which let the code fall through to a real login.)"""

    def __getattr__(self, name):
        raise AssertionError(f"a test tried to reach the real Google API via .{name}() - "
                             "gc._service must be a fake the test installs itself")


def isolate(tmp_path):
    """
    Point every piece of state this app keeps at a fresh temp folder, never the developer's real
    project files, real ALLOWED_FOLDER, or real Google credentials. Call this first, in every
    test_* function, with its own fresh tmp_path (e.g. Path(tempfile.mkdtemp())).
    """
    tmp_path = Path(tmp_path)
    jc.NOTES_FILE = tmp_path / "notes.txt"
    jc.CONTEXT_FILES = [jc.NOTES_FILE]
    jc.ALLOWED_FOLDER = tmp_path / "allowed"
    jc.ALLOWED_FOLDER.mkdir(parents=True, exist_ok=True)
    jc._guard.reset()
    gc._service = NeverRealGoogle()
    gc._next_event_cache = None
    al.WHITELIST_FILE = tmp_path / "app_whitelist.json"  # does not exist unless a test writes it
    al.reload()
    sc.TOKEN_FILE = str(tmp_path / "spotify_token.json")  # does not exist unless a test seeds it
    sc._token = None
    sc._opener = urllib.request.urlopen   # the REAL one: sc._request() then refuses to use it
                                          # (JARVIS_TESTING) unless a test installs a fake - see fake_spotify()


class FakeClient:
    """A scripted stand-in for anthropic.Anthropic(): .messages.create() returns the next item in
    `script`, in order. An item that is an exception instance is raised instead of returned (for
    rollback/abort_round tests)."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.sent = []
        self.messages = self

    def create(self, **kw):
        self.sent.append({**kw, "messages": list(kw["messages"])})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def T(text):
    return SimpleNamespace(type="text", text=text)


def TU(block_id, name, tool_input=None):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input or {})


def SU(block_id):
    return SimpleNamespace(type="server_tool_use", id=block_id, name="web_search", input={"query": "x"})


def SR(block_id):
    return SimpleNamespace(type="web_search_tool_result", tool_use_id=block_id, content=[])


def R(stop_reason, content, in_tokens=10, out_tokens=5):
    return SimpleNamespace(
        stop_reason=stop_reason, content=content,
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens, output_tokens_details=None),
    )


def REFUSAL(category="disallowed_content", in_tokens=10, out_tokens=0):
    """A model-refusal response, the shape call_claude()/_call_claude_loop() expect."""
    return SimpleNamespace(
        stop_reason="refusal", content=[],
        stop_details=SimpleNamespace(type="refusal", category=category),
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens, output_tokens_details=None),
    )


def fresh_server(tmp_path, script=None):
    """isolate() plus the Flask server's own shared state (messages, mode, the fake client) - for
    any test that goes through /chat or /cancel. Returns the installed FakeClient."""
    isolate(tmp_path)
    server.messages.clear()
    server.mode_state = jc.ModeState()
    server.client = FakeClient(script)
    return server.client


def post_chat(message):
    return server.app.test_client().post("/chat", json={"message": message})


def post_cancel():
    return server.app.test_client().post("/cancel")


def notes_text():
    """jc.NOTES_FILE's content, or "" if nothing has been saved to it yet (it may not exist)."""
    return jc.NOTES_FILE.read_text(encoding="utf-8") if jc.NOTES_FILE.exists() else ""


@contextlib.contextmanager
def fake_launcher():
    """
    Patches app_launcher's subprocess.Popen / os.startfile so open_app can NEVER start a real
    program, and restores the real functions as soon as the `with` block ends (important: these
    are the SAME module objects the rest of the process shares, so leaving them patched would
    affect unrelated code, e.g. a test's own subprocess.run() calls).

    Yields a list that records every (kind, value) that WOULD have been launched.
    """
    launched = []
    real_popen, real_startfile = al.subprocess.Popen, al.os.startfile

    def fake_popen(argv, **kw):
        launched.append(("exe", list(argv)))
        return SimpleNamespace(pid=999999)

    def fake_startfile(target):
        launched.append(("uri", target))

    al.subprocess.Popen = fake_popen
    al.os.startfile = fake_startfile
    try:
        yield launched
    finally:
        al.subprocess.Popen = real_popen
        al.os.startfile = real_startfile


class _FakeSpotifyResponse:
    """What spotify_control._request() gets back on a 2xx - a context manager like urlopen's."""

    def __init__(self, status, body, headers=None):
        import json as _json
        self.status = status
        self._body = _json.dumps(body).encode("utf-8") if body is not None else b""
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _fake_spotify_http_error(status, body=None, headers=None):
    """What urllib raises for a non-2xx: HTTPError IS the file-like object spotify_control reads."""
    import io
    import json as _json
    import urllib.error
    payload = _json.dumps(body).encode("utf-8") if body is not None else b""
    return urllib.error.HTTPError(url="https://api.spotify.com/v1/fake", code=status, msg="error",
                                  hdrs=headers or {}, fp=io.BytesIO(payload))


class FakeSpotifyHTTP:
    """
    Installed as spotify_control._opener - the ONLY seam spotify_control.py makes a real network
    call through, so this is a complete fake of the Spotify HTTP API, never the real thing.

    `script` is a list of (status, body, headers) tuples, or callables request -> (status, body,
    headers), consumed in order, one per call. `calls` records every request made (method, url,
    JSON body) so a test can assert what was actually sent (e.g. the Retry-After wait happened,
    the refreshed token was used, exactly one retry occurred).
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=15):
        import json as _json
        body = None
        if req.data:
            try:
                body = _json.loads(req.data)   # the Spotify Web API calls (json bodies)
            except ValueError:
                body = req.data.decode("ascii")  # the token endpoint (form-encoded), kept as-is
        self.calls.append({
            "method": req.get_method(), "url": req.full_url, "body": body,
            "auth": req.get_header("Authorization"),
        })
        if not self.script:
            raise AssertionError(f"FakeSpotifyHTTP ran out of scripted responses at call #{len(self.calls)}: {self.calls[-1]}")
        item = self.script.pop(0)
        status, body, headers = item(req) if callable(item) else item
        if 200 <= status < 300:
            return _FakeSpotifyResponse(status, body, headers)
        raise _fake_spotify_http_error(status, body, headers)


@contextlib.contextmanager
def fake_spotify(script):
    """`with fake_spotify([...]) as http:` - installs a FakeSpotifyHTTP for the block, restores
    the real urlopen (so JARVIS_TESTING blocks it again) as soon as the block ends."""
    http = FakeSpotifyHTTP(script)
    real_opener = sc._opener
    sc._opener = http
    try:
        yield http
    finally:
        sc._opener = real_opener


def seed_spotify_token(access_token="fake-access-token", refresh_token="fake-refresh-token", expires_in=3600):
    """Pretends spotify_login.py already ran - writes straight to the (temp, see isolate())
    TOKEN_FILE, never the real one, and never a real login. Goes through spotify_control's own
    guard too (belt and suspenders): calling this before isolate() refuses instead of silently
    writing to the real spotify_token.json."""
    import json as _json
    import time as _time
    sc._refuse_if_untested_real_file()
    token = {"access_token": access_token, "refresh_token": refresh_token,
            "expires_at": _time.time() + expires_in, "scope": sc.SCOPES}
    with open(sc.TOKEN_FILE, "w", encoding="utf-8") as f:
        _json.dump(token, f)
    sc._token = token
    return token


def fake_google_ok(link="https://example.invalid/fake-event"):
    """A fake Calendar service whose insert() always succeeds with a fake link. Call AFTER
    isolate() - isolate() installs NeverRealGoogle, which this replaces on purpose."""
    gc._service = SimpleNamespace(
        events=lambda: SimpleNamespace(insert=lambda **kw: SimpleNamespace(execute=lambda: {"htmlLink": link})))


def write_whitelist(entries):
    """Writes `entries` as app_whitelist.json at al.WHITELIST_FILE (a temp path - see isolate())
    and reloads app_launcher's cache. A fake, never-real exe path is created for every "exe" entry
    that doesn't already point at a file that exists, so app_launcher's own is_file() check passes
    without ever touching a real installed program."""
    import json
    for entry in entries.values():
        if entry.get("type") == "exe" and not Path(entry["path"]).exists():
            Path(entry["path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(entry["path"]).write_text("not a real program - a test fixture", encoding="utf-8")
    al.WHITELIST_FILE.write_text(json.dumps(entries), encoding="utf-8")
    al.reload()


def default_whitelist(tmp_path):
    """The standard 3-app table these tests use: spotify (uri), vscode (exe, accepts a folder),
    operagx (exe, never a folder/url) - all fake paths under tmp_path, never a real install."""
    fake_exe = str(Path(tmp_path) / "fake_programs" / "FakeApp.exe")
    entries = {
        "spotify": {"type": "uri", "target": "spotify:", "aliases": []},
        "vscode": {"type": "exe", "path": fake_exe, "args": ["--new-window"], "accepts_folder": True,
                   "aliases": ["vs code", "visual studio code", "code"]},
        "operagx": {"type": "exe", "path": fake_exe, "args": [], "accepts_folder": False,
                    "aliases": ["opera gx", "opera", "gx", "browser"]},
    }
    write_whitelist(entries)
    return entries, fake_exe


def run_module_tests(module):
    """Runs every test_* callable in `module`, in alphabetical order (each is independently
    isolated, so order never matters), prints one line per test, and exits non-zero on failure."""
    names = sorted(n for n in dir(module) if n.startswith("test_") and callable(getattr(module, n)))
    failures = []
    for name in names:
        try:
            getattr(module, name)()
            print(f"ok    {name}")
        except Exception:
            print(f"FAIL  {name}")
            traceback.print_exc()
            failures.append(name)
    print(f"{module.__name__}: {len(names) - len(failures)}/{len(names)} passed" +
          (f" - FAILED: {failures}" if failures else ""))
    if failures:
        sys.exit(1)

"""
Jarvis - desktop entry point.

Runs the Flask server in a background thread and shows Jarvis as a small,
always-on-top floating orb in the corner of the screen (draggable - just
click and hold). Click the orb to expand into the full dashboard; click the
"-" button in the dashboard's top bar to shrink back down.

Also listens for "Hey Jarvis" in the background (fully local, see
wake_word.py). Once heard, Python runs the whole conversation itself
(greet -> record -> transcribe locally with voice_capture.py -> ask Jarvis
via the normal /chat endpoint -> speak the reply) and only asks the browser
page to actually speak text out loud - the browser's own speech
*recognition* doesn't work inside this embedded window, only synthesis does.
"""

import json
import sys
import threading
import time
import tkinter as tk
import urllib.request
from datetime import datetime

import webview

# Windows terminals often default to a legacy codepage (cp1252) that can't
# encode emoji or other Unicode - printing a reply/heard-text containing one
# would otherwise crash this whole background thread mid-conversation.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import voice_capture
import wake_word
from server import app

SERVER_URL = "http://127.0.0.1:5000"

WIDGET_SIZE = 160
DASHBOARD_WIDTH = 980
DASHBOARD_HEIGHT = 760
MARGIN = 24
TASKBAR_ALLOWANCE = 60  # leave room above the Windows taskbar
CONVERSATION_WINDOW_SECONDS = 90  # how long to keep listening for a follow-up
CHAT_TIMEOUT_SECONDS = 180  # was 90: /chat is serialized now, and a serious (Opus) turn with tools can take longer
MODE_POLL_SECONDS = 30  # how often the watcher asks /mode, to catch the idle switch-off

_window = None  # set once create_window() runs, used by Api below
_screen_size = None  # (width, height), computed once on the main thread


class Api:
    """
    Exposed to JavaScript as window.pywebview.api.<method>().
    Both methods navigate the window - but doing that INSIDE the API call
    breaks pywebview's own "call finished" message back to the page that
    made the call (the page is gone by the time it arrives). So we just
    schedule the actual navigation a moment later, after this call has
    already returned successfully.
    """

    def expand(self):
        threading.Timer(0.15, _do_expand).start()
        return True

    def shrink(self):
        threading.Timer(0.15, _do_shrink).start()
        return True


def _do_expand():
    _window.load_url(f"{SERVER_URL}/")
    _window.resize(DASHBOARD_WIDTH, DASHBOARD_HEIGHT)
    _window.move(120, 80)


def _do_shrink():
    _window.load_url(f"{SERVER_URL}/widget")
    _window.resize(WIDGET_SIZE, WIDGET_SIZE)
    x, y = _widget_position()
    _window.move(x, y)


def _widget_position():
    """Bottom-right corner of the screen, clear of the taskbar."""
    screen_w, screen_h = _screen_size
    return screen_w - WIDGET_SIZE - MARGIN, screen_h - WIDGET_SIZE - MARGIN - TASKBAR_ALLOWANCE


def _run_server():
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


def _wait_for_server(timeout=10):
    """Poll the server until it actually answers, so nothing downstream
    tries to use it before Flask has finished starting up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{SERVER_URL}/mode", timeout=0.5)  # not /status: that one calls Google
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _time_greeting(lang):
    hour = datetime.now().hour
    if lang.startswith("pt"):
        if hour < 12:
            return "Bom dia!"
        if hour < 18:
            return "Boa tarde!"
        return "Boa noite!"
    if hour < 12:
        return "Good morning!"
    if hour < 18:
        return "Good afternoon!"
    return "Good evening!"


def _speak_and_wait(text, lang, timeout=30):
    """Tells the browser page to speak text aloud, and blocks until it's
    actually done - so we don't start recording over Jarvis's own voice."""
    done = threading.Event()

    def _on_done(_result=None):
        done.set()

    js = f"new Promise((resolve) => {{ window.speakText({json.dumps(text)}, {json.dumps(lang)}, resolve); }});"
    try:
        _window.evaluate_js(js, _on_done)
    except Exception as error:
        print(f"[desktop] speak_and_wait failed: {error}", flush=True)
        return
    done.wait(timeout)


def _set_thinking(on):
    if _window is None:
        return
    try:
        _window.evaluate_js(f"window.setThinking && window.setThinking({'true' if on else 'false'})")
    except Exception:
        pass  # purely cosmetic - not worth interrupting the conversation over


def _set_mode(mode):
    """Tells the page which mode is on (amber orb/ring in serious mode)."""
    if _window is None or mode not in ("normal", "serious"):
        return
    try:
        _window.evaluate_js(f"window.setMode && window.setMode({json.dumps(mode)})")
    except Exception:
        pass  # purely cosmetic - not worth interrupting the conversation over


def _sync_mode(last_mode):
    """One /mode poll (no chat lock, no touch, no Calendar): pushes the mode to
    the page only when it changed. Returns the mode to remember."""
    try:
        with urllib.request.urlopen(f"{SERVER_URL}/mode", timeout=5) as response:
            mode = json.loads(response.read()).get("mode")
    except Exception:
        return last_mode
    if mode and mode != last_mode:
        _set_mode(mode)
    return mode or last_mode


def _watch_mode():
    """Background thread: catches serious mode switching itself off when idle."""
    last = None
    time.sleep(2)  # let the window load first
    while True:
        last = _sync_mode(last)
        time.sleep(MODE_POLL_SECONDS)


def _ask_jarvis(text, detected_lang=None):
    """
    Reuses the same /chat endpoint the browser itself calls, so behavior
    (tools, memory, everything) matches exactly - no logic duplicated here.
    Returns (reply, mode); (None, None) on any failure.

    A voice conversation is long-running, and the model sometimes "sticks"
    to whatever language it last replied in instead of matching each new
    message - a known instruction-following gap, not something a stronger
    prompt fully eliminates. Here we actually KNOW the spoken language from
    Whisper, so we attach an explicit, unambiguous hint - invisible to her
    since there's no visible transcript in the widget anyway.
    """
    message = text
    if detected_lang and detected_lang.startswith("pt"):
        message = f"{text}\n\n[Reply in Portuguese.]"
    elif detected_lang:
        message = f"{text}\n\n[Reply in English.]"

    try:
        request = urllib.request.Request(
            f"{SERVER_URL}/chat",
            data=json.dumps({"message": message}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read())
        return data.get("reply"), data.get("mode")
    except Exception as error:
        print(f"[desktop] _ask_jarvis failed: {error}", flush=True)
        return None, None


def _handle_wake():
    """Runs the whole spoken conversation. One call per "Hey Jarvis"."""
    print("[desktop] wake word heard - starting conversation", flush=True)
    wake_word.pause()
    try:
        lang = "en-US"  # just for the greeting - Whisper detects what she actually speaks
        _speak_and_wait(_time_greeting(lang), lang)

        deadline = time.monotonic() + CONVERSATION_WINDOW_SECONDS
        while time.monotonic() < deadline:
            _set_thinking(True)
            text, detected_lang = voice_capture.transcribe()
            _set_thinking(False)

            if not text:
                print("[desktop] heard nothing - ending conversation", flush=True)
                break

            print(f"[desktop] heard: {text!r} (lang={detected_lang})", flush=True)

            _set_thinking(True)
            reply, mode = _ask_jarvis(text, detected_lang)
            _set_thinking(False)
            if mode:
                _set_mode(mode)  # e.g. "Serious mode activating." changes the orb right away

            if not reply:
                print("[desktop] _ask_jarvis returned nothing - ending conversation", flush=True)
                break

            print(f"[desktop] replied: {reply!r}", flush=True)

            reply_lang = "pt-BR" if (detected_lang or "").startswith("pt") else "en-US"
            _speak_and_wait(reply, reply_lang)
            deadline = time.monotonic() + CONVERSATION_WINDOW_SECONDS  # reset after each real turn
    finally:
        wake_word.resume()
        print("[desktop] conversation ended, wake word resumed", flush=True)


def _on_wake_word():
    """Runs on the wake-word listening thread - hand the actual conversation
    off to its own thread so the listener loop isn't blocked by it."""
    threading.Thread(target=_handle_wake, daemon=True).start()


def main():
    global _screen_size
    root = tk.Tk()  # computed once, here, on the main thread
    _screen_size = (root.winfo_screenwidth(), root.winfo_screenheight())
    root.destroy()

    threading.Thread(target=_run_server, daemon=True).start()
    threading.Thread(target=voice_capture.warm_up, daemon=True).start()
    _wait_for_server()

    global _window
    x, y = _widget_position()
    _window = webview.create_window(
        "Jarvis",
        f"{SERVER_URL}/widget",
        width=WIDGET_SIZE,
        height=WIDGET_SIZE,
        x=x,
        y=y,
        frameless=True,
        easy_drag=True,
        on_top=True,
        js_api=Api(),
    )

    threading.Thread(target=wake_word.listen_forever, args=(_on_wake_word,), daemon=True).start()
    threading.Thread(target=_watch_mode, daemon=True).start()

    # private_mode=False: without this, pywebview wipes cookies and
    # localStorage (language/mute preferences) every time the app restarts.
    # Persisted under %APPDATA%\pywebview.
    webview.start(private_mode=False)


if __name__ == "__main__":
    main()

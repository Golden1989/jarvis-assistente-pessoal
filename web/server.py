"""
Jarvis - Web UI backend.

Serves the dashboard interface and relays chat messages to jarvis_core. This
is a single-user, LOCAL-ONLY app: it binds to 127.0.0.1 (not your network),
and the conversation lives in memory in one shared list - fine for one
person talking to their own assistant on their own machine.
"""

import sys
import threading
import webbrowser
from pathlib import Path

# jarvis_core.py lives one folder up (the project root) - add it to the
# import path so `import jarvis_core` works when this script runs from web/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic
from flask import Flask, jsonify, render_template, request

import google_calendar
from jarvis_core import (
    FALLBACK_MODE, NOTICES, SERIOUS_IDLE_MINUTES, TOOLS,
    CallInfo, ModeState, ModelRefusedError, SystemPromptProvider,
    call_claude, compose_reply, detect_mode_command, message_language,
    mode_reply, refusal_reply,
)

app = Flask(__name__)

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment - never sent to the browser
prompts = SystemPromptProvider()  # notes.txt is re-checked on every request

messages = []  # the conversation history, shared by every request (single user)
mode_state = ModeState()  # mode, idle timer and per-model cost: shared by the dashboard and the voice widget
# ONE message at a time: mode, cost, MemoryGuard and `messages` are shared state.
# /status deliberately does NOT take this lock (and never calls touch()), so it
# still answers while a long Opus reply is running and can't keep serious mode alive.
chat_lock = threading.Lock()


def _tools_used_since(start_index):
    """Names of tools Claude actually invoked in messages[start_index:] (this turn)."""
    names = []
    for msg in messages[start_index:]:
        if msg["role"] != "assistant":
            continue
        for block in msg["content"]:
            if getattr(block, "type", None) in ("tool_use", "server_tool_use"):
                names.append(block.name)
    return names


def _payload(reply, **extra):
    snap = mode_state.snapshot()
    return {"reply": reply, "mode": snap["mode"], "model": snap["model"],
            "session_cost": snap["cost"], "tools_used": [], "turn_tokens": 0, **extra}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/widget")
def widget():
    """Just the orb, sized to fit the small floating desktop widget window."""
    return render_template("widget.html")


@app.route("/status")
def status():
    # Calendar is best-effort: a dashboard glitch here shouldn't break the page.
    try:
        upcoming = google_calendar.next_event()
    except Exception:
        upcoming = None

    snap = mode_state.snapshot()  # no chat_lock, no touch()
    return jsonify(
        {
            "tools": [t["name"] for t in TOOLS],
            "mode": snap["mode"],
            "model": snap["model"],
            "serious_seconds_left": snap["serious_seconds_left"],
            "cost_alert_step": snap["cost_alert_step"],
            "session": {k: snap[k] for k in ("input_tokens", "output_tokens", "cost", "by_model")},
            "next_event": upcoming,
        }
    )


@app.route("/chat", methods=["POST"])
def chat():
    user_input = (request.json or {}).get("message", "").strip()
    if not user_input:
        return jsonify({"error": "empty message"}), 400
    with chat_lock:
        return _chat(user_input)


def _chat(user_input):
    lang = message_language(user_input)

    # Mode commands: her own words only, BEFORE anything is appended to the
    # history and before MemoryGuard's begin_round. Fixed reply, no API call.
    # set_mode restarts the idle timer, so a command counts as use.
    command = detect_mode_command(user_input)
    if command is not None:
        mode_state.set_mode(command[0])
        return jsonify(_payload(mode_reply(command), fixed=True))

    mode_state.touch()  # expires an idle serious mode first, then counts as use
    mode = mode_state.current()
    idle_notice = None
    if mode_state.take_reverted_notice():
        idle_notice = NOTICES["idle_revert"][lang].format(minutes=SERIOUS_IDLE_MINUTES)

    checkpoint = len(messages)
    messages.append({"role": "user", "content": user_input})
    info = CallInfo()
    try:
        response, in_tokens, out_tokens = call_claude(
            client, prompts.get(), messages, mode=mode, state=mode_state, info=info)
    except ModelRefusedError as refusal:
        del messages[checkpoint:]  # the refused message leaves the history; the mode does not change
        return jsonify(_payload(refusal_reply(lang, mode_state, info), refused=True,
                                refusal_category=refusal.category, fallback=info.fallback))  # HTTP 200: the voice speaks it
    except anthropic.APIError as error:
        del messages[checkpoint:]  # unchanged behavior: undo the message
        return jsonify({"error": str(error)}), 502
    except BaseException:
        del messages[checkpoint:]  # anything unexpected: the history stays consistent, Flask reports it
        raise

    if info.fallback:
        mode_state.set_mode(FALLBACK_MODE)  # D1: the fallback answered, so stay on the model that accepted
    reply = compose_reply(response, info, lang, mode_state)  # the ONLY source of the memory notice
    if idle_notice:
        reply = f"{idle_notice}\n\n{reply}"
    return jsonify(_payload(reply, tools_used=_tools_used_since(checkpoint + 1),
                            turn_tokens=in_tokens + out_tokens,
                            fallback=info.fallback, truncated=info.truncated))


if __name__ == "__main__":
    webbrowser.open("http://127.0.0.1:5000")
    # host=127.0.0.1: reachable only from this machine, never your local network.
    # debug=False: the Flask debugger can execute arbitrary code - never turn
    # it on for anything that touches a real API key or your files/calendar.
    app.run(host="127.0.0.1", port=5000, debug=False)

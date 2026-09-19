"""
Jarvis - Web UI backend.

Serves the dashboard interface and relays chat messages to jarvis_core. This
is a single-user, LOCAL-ONLY app: it binds to 127.0.0.1 (not your network),
and the conversation lives in memory in one shared list - fine for one
person talking to their own assistant on their own machine.
"""

import sys
import webbrowser
from pathlib import Path

# jarvis_core.py lives one folder up (the project root) - add it to the
# import path so `import jarvis_core` works when this script runs from web/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic
from flask import Flask, jsonify, render_template, request

import google_calendar
from jarvis_core import INPUT_PRICE, OUTPUT_PRICE, TOOLS, build_system_prompt, call_claude, extract_text

app = Flask(__name__)

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment - never sent to the browser
system_prompt = build_system_prompt()

messages = []  # the conversation history, shared by every request (single user)
totals = {"input_tokens": 0, "output_tokens": 0}


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


def _session_cost():
    return round(
        totals["input_tokens"] / 1_000_000 * INPUT_PRICE
        + totals["output_tokens"] / 1_000_000 * OUTPUT_PRICE,
        4,
    )


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

    return jsonify(
        {
            "tools": [t["name"] for t in TOOLS],
            "session": {
                "input_tokens": totals["input_tokens"],
                "output_tokens": totals["output_tokens"],
                "cost": _session_cost(),
            },
            "next_event": upcoming,
        }
    )


@app.route("/chat", methods=["POST"])
def chat():
    user_input = (request.json or {}).get("message", "").strip()
    if not user_input:
        return jsonify({"error": "empty message"}), 400

    checkpoint = len(messages)
    messages.append({"role": "user", "content": user_input})

    try:
        response, in_tokens, out_tokens = call_claude(client, system_prompt, messages)
    except anthropic.APIError as error:
        del messages[checkpoint:]
        return jsonify({"error": str(error)}), 502

    tools_used = _tools_used_since(checkpoint + 1)

    totals["input_tokens"] += in_tokens
    totals["output_tokens"] += out_tokens

    return jsonify(
        {
            "reply": extract_text(response),
            "tools_used": tools_used,
            "turn_tokens": in_tokens + out_tokens,
            "session_cost": _session_cost(),
        }
    )


if __name__ == "__main__":
    webbrowser.open("http://127.0.0.1:5000")
    # host=127.0.0.1: reachable only from this machine, never your local network.
    # debug=False: the Flask debugger can execute arbitrary code - never turn
    # it on for anything that touches a real API key or your files/calendar.
    app.run(host="127.0.0.1", port=5000, debug=False)

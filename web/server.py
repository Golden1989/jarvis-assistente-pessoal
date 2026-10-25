"""
Jarvis - Web UI backend.

Serves the dashboard interface and relays chat messages to jarvis_core. This
is a single-user, LOCAL-ONLY app: it binds to 127.0.0.1 (not your network),
and the conversation lives in memory in one shared list - fine for one
person talking to their own assistant on their own machine.
"""

import hmac
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

# jarvis_core.py lives one folder up (the project root) - add it to the
# import path so `import jarvis_core` works when this script runs from web/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic
from flask import Flask, abort, jsonify, render_template, request

import google_calendar
import wake_health
from server_config import HOST, PORT
from jarvis_core import (
    FALLBACK_MODE, NOTICES, QUIZ_SNOOZE_MINUTES, SERIOUS_IDLE_MINUTES, TOOLS,
    BriefingFailedError, CallInfo, DailyLimitError, ModeState, ModelRefusedError, NoBriefingDataError,
    QuizFailedError, QuizNoMaterialError, QuizReviewLimitError, SystemPromptProvider, allow_more_reply,
    allow_more_today, briefing_failed_reply, briefing_unavailable_reply, call_claude,
    cancel_pending_actions, check_and_signal_quiz, compose_reply, confirm_pending_by_id,
    daily_limit_reply, daily_spend_status, detect_add_topic_command, detect_allow_more_command,
    detect_briefing_command, detect_mode_command, detect_quiz_end_command, detect_quiz_review_command,
    detect_quiz_skip_command, detect_quiz_snooze_command, detect_quiz_start_command,
    detect_save_summary_command, detect_stop_command, detect_study_material_command,
    detect_undo_file_change_command, detect_undo_file_change_named_command, detect_undo_study_command,
    discard_pending_by_id, end_quiz_command, get_or_run_briefing, grade_quiz_answer,
    handle_save_entry_command, handle_study_material_command, handle_undo_file_change_command,
    handle_undo_file_change_named_command, handle_undo_study_command, is_quiz_active,
    maybe_resolve_pending_file_undo_choice, message_language, mode_reply, pending_actions_for_status,
    quiz_already_active_reply, quiz_bad_response_reply, quiz_due_status, quiz_limit_hit_reply,
    quiz_no_material_reply, quiz_review_limit_reply, refusal_reply, review_quiz_question,
    skip_quiz_reply, skip_quiz_today, snooze_quiz, snooze_quiz_reply, start_quiz, stop_reply,
)

app = Flask(__name__)

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment - never sent to the browser
prompts = SystemPromptProvider()  # notes.txt is re-checked on every request

# Stage B: a per-process token the dashboard must echo back on /pending/confirm and
# /pending/cancel - regenerated every server start (this line runs once per process), never
# persisted or logged. "Per-session" has no meaning beyond "per-process" here: the whole app is
# already one single global session (`messages`, `mode_state`, `_guard` are each one unguarded
# module instance), not a multi-browser-tab system.
CONFIRM_TOKEN = secrets.token_urlsafe(32)

# Host-only allowlist (checked on GET / and GET /status, which now carry the token and pending
# previews) and the stricter Host-AND-Origin allowlist (checked on the state-changing POST routes).
# Threat model for the Host check: not header spoofing (a browser sets Host to the real
# destination, not an attacker-chosen value) but DNS rebinding - a page on an attacker's domain
# gets that domain re-pointed at 127.0.0.1 after load, so a same-origin request from that page
# lands on this server while Host still reads the attacker's domain name. Origin is the separate,
# CSRF-style defense for POSTs, and is often simply absent on a plain GET navigation - so GET
# routes check Host only, POST routes require Host AND a matching Origin.
_LOCAL_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
_LOCAL_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}


def _is_local_host(req):
    return req.headers.get("Host", "") in _LOCAL_HOSTS


def _is_local_request(req):
    return _is_local_host(req) and req.headers.get("Origin") in _LOCAL_ORIGINS

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
    if not _is_local_host(request):
        abort(403)
    return render_template("index.html", confirm_token=CONFIRM_TOKEN)


@app.route("/widget")
def widget():
    """Just the orb, sized to fit the small floating desktop widget window."""
    return render_template("widget.html")


@app.route("/status")
def status():
    if not _is_local_host(request):
        abort(403)
    # Calendar is best-effort: a dashboard glitch here shouldn't break the page.
    try:
        upcoming = google_calendar.next_event()
    except Exception:
        upcoming = None

    snap = mode_state.snapshot()  # no chat_lock, no touch()
    today_spent, daily_limit = daily_spend_status()
    return jsonify(
        {
            "tools": [t["name"] for t in TOOLS],
            "mode": snap["mode"],
            "model": snap["model"],
            "serious_seconds_left": snap["serious_seconds_left"],
            "cost_alert_step": snap["cost_alert_step"],
            "session": {k: snap[k] for k in ("input_tokens", "output_tokens", "cost", "by_model")},
            "today_spent": round(today_spent, 4),
            "daily_limit": round(daily_limit, 4),
            "next_event": upcoming,
            "wake_word": wake_health.snapshot(),
            "quiz_due": quiz_due_status(),
            "pending": pending_actions_for_status(),
        }
    )


@app.route("/mode")
def mode_endpoint():
    """Tiny read for the desktop watcher and the widget: no chat_lock, no touch()
    (so polling can never keep serious mode alive), no Google Calendar."""
    snap = mode_state.snapshot()
    return jsonify({k: snap[k] for k in ("mode", "model", "serious_seconds_left")})


@app.route("/quiz/check")
def quiz_check():
    """Polled every 60s by desktop.py's background watcher (Stage B) to decide whether to pop the
    dashboard and pulse the orb. No chat_lock (same reasoning as /mode: must never queue behind a
    long chat turn) and no Google Calendar. check_and_signal_quiz() is atomic on its own (a
    dedicated lock in jarvis_core.py), so two near-simultaneous polls can't both fire."""
    return jsonify({"signal": check_and_signal_quiz()})


@app.route("/chat", methods=["POST"])
def chat():
    user_input = (request.json or {}).get("message", "").strip()
    if not user_input:
        return jsonify({"error": "empty message"}), 400
    with chat_lock:
        return _chat(user_input)


@app.route("/cancel", methods=["POST"])
def cancel():
    """Dashboard 'stop' button: discard any held action right now. No API call, no history -
    same chat_lock as /chat just to serialize cleanly against one in flight."""
    with chat_lock:
        had_pending = cancel_pending_actions()
    return jsonify({"had_pending": had_pending})


def _confirm_token_ok(body):
    return hmac.compare_digest(str(body.get("token", "")), CONFIRM_TOKEN)


def _append_dashboard_note_to_history(note):
    """The model's own last stored reply may still say 'Pendente - NOT done yet' (it was written
    before she ever clicked) - a one-shot system-prompt aside can't fix that, so this appends a
    real, persistent user+assistant pair to `messages` instead, right after a successful dashboard
    confirm/cancel. ONLY when the last stored message is an assistant message - if it isn't
    (nothing pending was ever actually held through a model round, or some other edge case),
    nothing is appended, rather than guessing where it belongs. Inserted BEFORE her next real
    message, so call_claude()'s _latest_user_text(messages)/_looks_like_yes() still only ever see
    her own literal next message - never this pair. Plain dicts: _block_get() already handles
    both SDK objects and dicts, the same shape tool-result blocks already use elsewhere."""
    if note is None or not messages or messages[-1]["role"] != "assistant":
        return
    messages.append({"role": "user",
                     "content": "[System note: she used a dashboard button, not a chat message]"})
    messages.append({"role": "assistant", "content": [{"type": "text", "text": note}]})


@app.route("/pending/confirm", methods=["POST"])
def pending_confirm():
    """Dashboard Confirm button for ONE pending item, named by id - never 'confirm whatever is
    pending'. POST only (no route is registered for GET, so there is no state-changing GET path).
    request.get_json(silent=True) never raises on a missing/malformed body - a garbled request
    just gets ok:false/403 instead of a 500. Same chat_lock as /chat/cancel, so a click can't race
    a half-finished chat round."""
    if not _is_local_request(request):
        abort(403)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    if not _confirm_token_ok(body):
        abort(403)
    with chat_lock:
        tool, result, note = confirm_pending_by_id(str(body.get("id", "")))
        _append_dashboard_note_to_history(note)
    return jsonify({"ok": tool is not None, "message": result})


@app.route("/pending/cancel", methods=["POST"])
def pending_cancel():
    """Dashboard Cancel button for ONE pending item, named by id. Same checks/locking as
    /pending/confirm above."""
    if not _is_local_request(request):
        abort(403)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    if not _confirm_token_ok(body):
        abort(403)
    with chat_lock:
        removed, note = discard_pending_by_id(str(body.get("id", "")))
        _append_dashboard_note_to_history(note)
    return jsonify({"ok": removed})


@app.route("/quiz/snooze", methods=["POST"])
def quiz_snooze():
    """Dashboard 'Snooze 15 min' button - same protection as /cancel: chat_lock, no API call,
    no history."""
    with chat_lock:
        snooze_quiz()
    return jsonify({"snoozed_minutes": QUIZ_SNOOZE_MINUTES})


@app.route("/quiz/skip", methods=["POST"])
def quiz_skip():
    """Dashboard 'Skip today' button - same protection as /cancel: chat_lock, no API call,
    no history."""
    with chat_lock:
        skip_quiz_today()
    return jsonify({"skipped": True})


def _chat(user_input):
    # Her very next whole message after "undo last file change" opened a choice (the bare list,
    # or a named command's disambiguation list) either resolves it or clears it - checked FIRST,
    # before anything else, so no other command/quiz-answer handling below can ever see a stale
    # choice still open. Returns None when there was nothing to resolve, in which case user_input
    # falls through to be handled completely normally, exactly as if nothing had been open.
    undo_choice_result = maybe_resolve_pending_file_undo_choice(user_input)
    if undo_choice_result is not None:
        return jsonify(_payload(undo_choice_result, fixed=True))

    lang = message_language(user_input)

    # Mode commands: her own words only, BEFORE anything is appended to the
    # history and before ActionGuard's begin_round. Fixed reply, no API call.
    # set_mode restarts the idle timer, so a command counts as use.
    command = detect_mode_command(user_input)
    if command is not None:
        mode_state.set_mode(command[0])
        return jsonify(_payload(mode_reply(command), fixed=True))

    # "stop"/"cancel" (the WHOLE message): discard anything held, right now, no API call, no
    # history, not an ActionGuard round - same precedence slot as mode commands.
    stop_lang = detect_stop_command(user_input)
    if stop_lang is not None:
        had_pending = cancel_pending_actions()
        return jsonify(_payload(stop_reply(stop_lang, had_pending), fixed=True))

    # "allow more today" (the WHOLE message): her own words only, same precedence slot as the
    # mode/stop commands above - no API call, never triggerable by the model or by tool content.
    allow_lang = detect_allow_more_command(user_input)
    if allow_lang is not None:
        allow_more_today()
        return jsonify(_payload(allow_more_reply(allow_lang), fixed=True))

    # "good morning" / "bom dia" / "refresh briefing" (the WHOLE message): her own words only,
    # same precedence slot as the commands above. Unlike those, this CAN cost (a cache miss calls
    # the API once) - see get_or_run_briefing for the cache/fetch/API-call/history wiring.
    briefing_command = detect_briefing_command(user_input)
    if briefing_command is not None:
        _, force_refresh = briefing_command
        briefing_info = CallInfo()
        try:
            full_text, spoken, was_cached = get_or_run_briefing(
                client, messages, user_input, force_refresh=force_refresh,
                state=mode_state, info=briefing_info)
        except DailyLimitError as limit_error:
            return jsonify(_payload(daily_limit_reply(lang, limit_error.limit), fixed=True,
                                    daily_limit=True))  # HTTP 200: the voice speaks it
        except NoBriefingDataError as error:
            return jsonify(_payload(briefing_unavailable_reply(lang, error.unavailable), fixed=True))
        except BriefingFailedError as error:
            # fixed=False (unlike the other failure replies above): the API WAS actually called
            # and cost real tokens, so the dashboard's usage bar should reflect it - still HTTP
            # 200, so the voice speaks the reply like any other failure message.
            turn_tokens = sum(u["input_tokens"] + u["output_tokens"] for u in briefing_info.usage)
            return jsonify(_payload(briefing_failed_reply(lang, error.reason), fixed=False,
                                    turn_tokens=turn_tokens))
        turn_tokens = sum(u["input_tokens"] + u["output_tokens"] for u in briefing_info.usage)
        return jsonify(_payload(full_text, fixed=was_cached, speak=spoken, turn_tokens=turn_tokens))

    # Study-material save/add/inspect/undo commands - prefix-with-payload or whole-message, her
    # own words only, no API call, same precedence area as the commands above.
    save_parsed = detect_save_summary_command(user_input)
    if save_parsed is not None:
        return jsonify(_payload(handle_save_entry_command(save_parsed, "summary"), fixed=True))
    topic_parsed = detect_add_topic_command(user_input)
    if topic_parsed is not None:
        return jsonify(_payload(handle_save_entry_command(topic_parsed, "topic"), fixed=True))
    if detect_study_material_command(user_input):
        return jsonify(_payload(handle_study_material_command(), fixed=True))
    if detect_undo_study_command(user_input):
        return jsonify(_payload(handle_undo_study_command(), fixed=True))
    named_undo_filename = detect_undo_file_change_named_command(user_input)
    if named_undo_filename is not None:
        return jsonify(_payload(handle_undo_file_change_named_command(named_undo_filename), fixed=True))
    if detect_undo_file_change_command(user_input):
        return jsonify(_payload(handle_undo_file_change_command(), fixed=True))

    # "snooze"/"soneca" and "skip today"/"pular hoje" (the WHOLE message): act on the 16:00
    # trigger itself, not on an in-progress quiz - no API call, same precedence area as the
    # commands above.
    snooze_lang = detect_quiz_snooze_command(user_input)
    if snooze_lang is not None:
        snooze_quiz()
        return jsonify(_payload(snooze_quiz_reply(snooze_lang), fixed=True))
    skip_lang = detect_quiz_skip_command(user_input)
    if skip_lang is not None:
        skip_quiz_today()
        return jsonify(_payload(skip_quiz_reply(skip_lang), fixed=True))

    # "end quiz" always wins over "this message is an answer" - checked before the quiz-start
    # command and before falling into the grader below.
    quiz_end_lang = detect_quiz_end_command(user_input)
    if quiz_end_lang is not None:
        return jsonify(_payload(end_quiz_command(quiz_end_lang), fixed=True))

    quiz_start_lang = detect_quiz_start_command(user_input)
    if quiz_start_lang is not None:
        if is_quiz_active():
            return jsonify(_payload(quiz_already_active_reply(quiz_start_lang), fixed=True))
        quiz_info = CallInfo()
        try:
            question_text = start_quiz(client, user_input, state=mode_state, info=quiz_info)
        except QuizNoMaterialError:
            return jsonify(_payload(quiz_no_material_reply(lang), fixed=True))
        except DailyLimitError as limit_error:
            return jsonify(_payload(daily_limit_reply(lang, limit_error.limit), fixed=True,
                                    daily_limit=True))
        except QuizFailedError:
            turn_tokens = sum(u["input_tokens"] + u["output_tokens"] for u in quiz_info.usage)
            return jsonify(_payload(quiz_bad_response_reply(lang), fixed=False, turn_tokens=turn_tokens))
        turn_tokens = sum(u["input_tokens"] + u["output_tokens"] for u in quiz_info.usage)
        return jsonify(_payload(question_text, fixed=False, turn_tokens=turn_tokens))

    # "review"/"revisao"/"me lembra": consumed ONLY while a quiz is active - otherwise this falls
    # through, unchanged, to normal chat below. Never routed to the grader.
    review_lang = detect_quiz_review_command(user_input)
    if review_lang is not None and is_quiz_active():
        quiz_info = CallInfo()
        try:
            refresher = review_quiz_question(client, state=mode_state, info=quiz_info)
        except QuizReviewLimitError:
            return jsonify(_payload(quiz_review_limit_reply(lang), fixed=True))
        except DailyLimitError as limit_error:
            return jsonify(_payload(daily_limit_reply(lang, limit_error.limit), fixed=True,
                                    daily_limit=True))
        except QuizFailedError:
            turn_tokens = sum(u["input_tokens"] + u["output_tokens"] for u in quiz_info.usage)
            return jsonify(_payload(quiz_bad_response_reply(lang), fixed=False, turn_tokens=turn_tokens))
        turn_tokens = sum(u["input_tokens"] + u["output_tokens"] for u in quiz_info.usage)
        return jsonify(_payload(refresher, fixed=False, turn_tokens=turn_tokens))

    # Every command above has had its chance - ONLY now, with none of them matching, does an
    # active quiz claim this message as an answer to the current question.
    if is_quiz_active():
        quiz_info = CallInfo()
        try:
            reply_text, _ = grade_quiz_answer(client, messages, user_input, state=mode_state, info=quiz_info)
        except DailyLimitError as limit_error:
            # Quiz state is untouched (grade_quiz_answer checks the cap before grading) - she can
            # continue after "allow more today" or tomorrow, the current question is unchanged.
            return jsonify(_payload(quiz_limit_hit_reply(lang, limit_error.limit), fixed=True,
                                    daily_limit=True))
        except QuizFailedError:
            turn_tokens = sum(u["input_tokens"] + u["output_tokens"] for u in quiz_info.usage)
            return jsonify(_payload(quiz_bad_response_reply(lang), fixed=False, turn_tokens=turn_tokens))
        turn_tokens = sum(u["input_tokens"] + u["output_tokens"] for u in quiz_info.usage)
        return jsonify(_payload(reply_text, fixed=False, turn_tokens=turn_tokens))

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
    except DailyLimitError as limit_error:
        del messages[checkpoint:]  # no API call was made; the message leaves no trace in history
        return jsonify(_payload(daily_limit_reply(lang, limit_error.limit), fixed=True,
                                daily_limit=True))  # HTTP 200: the voice speaks it, same as a refusal
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
    webbrowser.open(f"http://{HOST}:{PORT}")
    # host=127.0.0.1: reachable only from this machine, never your local network.
    # debug=False: the Flask debugger can execute arbitrary code - never turn
    # it on for anything that touches a real API key or your files/calendar.
    app.run(host=HOST, port=PORT, debug=False)

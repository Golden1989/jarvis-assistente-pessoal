"""
Jarvis - Level 2 (terminal UI)
Thin terminal wrapper around jarvis_core. For the web UI, see web/server.py.
"""

import sys
from pathlib import Path

import anthropic

from jarvis_core import (
    CONTEXT_FILES,
    EXIT_COMMANDS,
    FALLBACK_MODE,
    NOTICES,
    SERIOUS_IDLE_MINUTES,
    TOOLS,
    CallInfo,
    ModeState,
    ModelRefusedError,
    SystemPromptProvider,
    call_claude,
    compose_reply,
    detect_mode_command,
    message_language,
    mode_reply,
    refusal_reply,
)


def status_text(snap):
    """The terminal's /status: mode, time until the idle switch-off, cost per model."""
    head = f"mode: {snap['mode']} ({snap['model']})"
    left = snap["serious_seconds_left"]
    if left is not None:
        head += f", back to normal in {int(left // 60)}m{int(left % 60):02d}s if idle"
    rows = [f"{model}: {r['input_tokens']} in + {r['output_tokens']} out = US$ {r['cost']:.4f}"
            for model, r in snap["by_model"].items()]
    total = f"total US$ {snap['cost']:.4f} (+ any web searches at $10/1,000)"
    return "[" + " | ".join([head] + rows + [total]) + "]"


def main():
    # Windows terminals often default to a legacy codepage (cp1252) that
    # can't encode emoji or many other Unicode characters Claude might use.
    # Force UTF-8 on stdout so a reply never crashes the whole program.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    messages = []        # the conversation history
    state = ModeState()  # mode, idle timer and per-model cost

    prompts = SystemPromptProvider()  # notes.txt is re-checked on every message

    print("Jarvis (Level 2) - type 'quit' to exit. Say 'serious mode' / 'back to normal' to switch models.")
    loaded = [f.name for f in CONTEXT_FILES if Path(f).exists()]
    if loaded:
        print(f"(context loaded from: {', '.join(loaded)})")
    print(f"(tools: {', '.join(t['name'] for t in TOOLS)})")
    print()

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in EXIT_COMMANDS:  # only the bare word quits
            break

        lang = message_language(user_input)

        # Mode commands ("exit serious mode" is one - it is NOT the bare word
        # "exit"): fixed reply, no API call, no history, not a MemoryGuard round.
        command = detect_mode_command(user_input)
        if command is not None:
            state.set_mode(command[0])
            print(f"\nJarvis: {mode_reply(command)}\n")
            print(status_text(state.snapshot()) + "\n")
            continue

        state.touch()  # expires an idle serious mode first, then counts as use
        mode = state.current()
        idle_notice = None
        if state.take_reverted_notice():
            idle_notice = NOTICES["idle_revert"][lang].format(minutes=SERIOUS_IDLE_MINUTES)

        checkpoint = len(messages)  # so we can cleanly roll back on error
        messages.append({"role": "user", "content": user_input})
        info = CallInfo()

        try:
            response, _, _ = call_claude(client, prompts.get(), messages, mode=mode, state=state, info=info)
        except ModelRefusedError:
            del messages[checkpoint:]  # the refused message leaves the history; the mode does not change
            print(f"\nJarvis: {refusal_reply(lang, state, info)}\n")
            print(status_text(state.snapshot()) + "\n")
            continue
        except anthropic.APIError as error:
            print(f"\n[API call failed: {error}]\n")
            del messages[checkpoint:]
            continue

        if info.fallback:
            state.set_mode(FALLBACK_MODE)  # D1: the fallback answered, so stay on the model that accepted
        text = compose_reply(response, info, lang, state)  # the ONLY source of the memory notice
        if idle_notice:
            text = f"{idle_notice}\n\n{text}"
        print(f"\nJarvis: {text}\n")
        print(status_text(state.snapshot()) + "\n")

    print("See you.")


if __name__ == "__main__":
    main()

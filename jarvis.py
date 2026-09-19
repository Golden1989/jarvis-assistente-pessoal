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
    INPUT_PRICE,
    OUTPUT_PRICE,
    TOOLS,
    SystemPromptProvider,
    call_claude,
    extract_text,
)


def main():
    # Windows terminals often default to a legacy codepage (cp1252) that
    # can't encode emoji or many other Unicode characters Claude might use.
    # Force UTF-8 on stdout so a reply never crashes the whole program.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    messages = []        # the conversation history
    input_tokens = 0     # running totals to track session spend
    output_tokens = 0

    prompts = SystemPromptProvider()  # notes.txt is re-checked on every message

    print("Jarvis (Level 2) - type 'quit' to exit.")
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
        if user_input.lower() in EXIT_COMMANDS:
            break

        checkpoint = len(messages)  # so we can cleanly roll back on error
        messages.append({"role": "user", "content": user_input})

        try:
            response, in_tokens, out_tokens = call_claude(client, prompts.get(), messages)
        except anthropic.APIError as error:
            print(f"\n[API call failed: {error}]\n")
            del messages[checkpoint:]
            continue

        text = extract_text(response)
        print(f"\nJarvis: {text}\n")

        # Update and show the running session spend (tokens only - web
        # search's $10/1,000-searches fee is not reflected here).
        input_tokens += in_tokens
        output_tokens += out_tokens
        cost = (
            input_tokens / 1_000_000 * INPUT_PRICE
            + output_tokens / 1_000_000 * OUTPUT_PRICE
        )
        print(
            f"[session: {input_tokens} input + {output_tokens} output tokens"
            f"  ~US$ {cost:.4f} (+ any web searches at $10/1,000)]\n"
        )

    print("See you.")


if __name__ == "__main__":
    main()

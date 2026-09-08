"""
Jarvis - Level 1
Terminal chatbot with conversation history, using the Anthropic API.
"""

import anthropic
from pathlib import Path

MODEL = "claude-sonnet-5"
MAX_RESPONSE_TOKENS = 1024

# claude-sonnet-5 pricing (US$ per million tokens).
INPUT_PRICE = 2.0
OUTPUT_PRICE = 10.0

# Optional file with information about you. Kept out of Git (.gitignore).
PROFILE_FILE = "profile.txt"

# Base identity - applies even without profile.txt (e.g. a fresh clone).
# Tone, form of address, and personal details live in profile.txt.
BASE_SYSTEM_PROMPT = (
    "You are Jarvis, a personal assistant for conversation, debating ideas, "
    "and thinking things through. Reply in the same language the person writes "
    "in. If a profile section follows, honor its preferences on tone and "
    "how to address the person."
)

EXIT_COMMANDS = {"quit", "exit"}


def build_system_prompt():
    """Combine the base identity with your profile, if the file exists."""
    prompt = BASE_SYSTEM_PROMPT
    path = Path(PROFILE_FILE)
    if path.exists():
        profile = path.read_text(encoding="utf-8").strip()
        if profile:
            prompt += "\n\n# About the person you are talking to\n" + profile
    return prompt


def extract_text(response):
    """The response is a list of blocks; join the text ones."""
    return "\n".join(
        block.text for block in response.content if block.type == "text"
    )


def main():
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    messages = []       # the conversation history
    input_tokens = 0    # running totals to track session spend
    output_tokens = 0

    system_prompt = build_system_prompt()

    print("Jarvis (Level 1) - type 'quit' to exit.")
    if Path(PROFILE_FILE).exists():
        print(f"(profile loaded from {PROFILE_FILE})")
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

        # 1. Add the user's turn to the history.
        messages.append({"role": "user", "content": user_input})

        # 2. Send the ENTIRE history and ask for a reply.
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_RESPONSE_TOKENS,
                system=system_prompt,
                messages=messages,
            )
        except anthropic.APIError as error:
            print(f"\n[API call failed: {error}]\n")
            messages.pop()  # undo the last turn so the history stays consistent
            continue

        # 3. Extract the text and 4. store it so the next round has context.
        text = extract_text(response)
        messages.append({"role": "assistant", "content": text})
        print(f"\nJarvis: {text}\n")

        # 5. Update and show the running session spend.
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        cost = (
            input_tokens / 1_000_000 * INPUT_PRICE
            + output_tokens / 1_000_000 * OUTPUT_PRICE
        )
        print(
            f"[session: {input_tokens} input + {output_tokens} output tokens"
            f"  ~US$ {cost:.4f}]\n"
        )

    print("See you.")


if __name__ == "__main__":
    main()

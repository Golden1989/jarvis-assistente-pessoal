"""
Jarvis - Level 2
Terminal chatbot with conversation history and tool use, using the Anthropic API.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

import google_calendar

MODEL = "claude-sonnet-5"
MAX_RESPONSE_TOKENS = 1024

# claude-sonnet-5 pricing (US$ per million tokens).
INPUT_PRICE = 2.0
OUTPUT_PRICE = 10.0

# Optional local files with information for the system prompt. Kept out of Git.
# notes.txt is special: Jarvis also WRITES to it via the "remember" tool, so
# facts you mention stick around across sessions.
CONTEXT_FILES = ["profile.txt", "people.txt", "notes.txt"]
NOTES_FILE = "notes.txt"

# Folder Jarvis is allowed to look into with list_files/read_file. Deliberately
# scoped to one folder (least privilege) instead of the whole disk - change
# this to wherever you keep the things you want it to see.
ALLOWED_FOLDER = Path(r"C:\Projetos")

# Base identity - applies even with no context files (e.g. a fresh clone).
# Tone, form of address, and personal details live in the context files above.
BASE_SYSTEM_PROMPT = (
    "You are Jarvis, a personal assistant for conversation, debating ideas, "
    "and thinking things through. Language rule, check it on every single "
    "reply: always answer in the same language as the user's LATEST message, "
    "even if the profile/notes context below, earlier turns, or a tool result "
    "is in a different language - the latest message always wins. If profile "
    "or people sections follow, use them: honor the profile's preferences on "
    "tone and how to address the person, and use the people section as "
    "background when they come up in conversation."
)

EXIT_COMMANDS = {"quit", "exit"}

# --- Tools -------------------------------------------------------------
# Two kinds, mixed in the same list:
#  - "web_search" is a server tool: Anthropic runs it and resolves the result
#    inside the same request. We don't execute anything for it.
#  - "get_current_datetime" is a custom (client) tool: WE execute it, in
#    run_tool() below, whenever Claude asks to call it.
TOOLS = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 3,  # hard cap per request - each search costs $10 / 1,000
    },
    {
        "name": "get_current_datetime",
        "description": (
            "Get the current local date and time. Use this whenever you need "
            "to know 'today', 'now', or anything time-relative - you don't "
            "otherwise know the current date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "remember",
        "description": (
            "Save a fact worth remembering in future conversations - a "
            "project the user is working on, an upcoming exam or "
            "appointment, a preference they mentioned. Write it as a short, "
            "clear, self-contained statement. This persists across sessions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "The fact to remember."}
            },
            "required": ["note"],
        },
    },
    {
        "name": "list_files",
        "description": (
            f"List files and folders inside {ALLOWED_FOLDER} (or a subfolder "
            "of it). Use this to see what projects or documents exist before "
            "reading one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subfolder": {
                    "type": "string",
                    "description": "Subfolder path relative to the allowed folder. Empty for the top level.",
                }
            },
        },
    },
    {
        "name": "read_file",
        "description": f"Read the text content of a file inside {ALLOWED_FOLDER}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the allowed folder.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Create an event on the user's Google Calendar. Call "
            "get_current_datetime first if you need to resolve a relative "
            f"date like 'tomorrow'. Times are local ({google_calendar.TIMEZONE})."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Short event title."},
                "start": {
                    "type": "string",
                    "description": "Start, local time, format YYYY-MM-DDTHH:MM:SS.",
                },
                "end": {
                    "type": "string",
                    "description": "End, local time, format YYYY-MM-DDTHH:MM:SS.",
                },
                "description": {"type": "string", "description": "Optional longer description."},
            },
            "required": ["summary", "start", "end"],
        },
    },
    {
        "name": "list_calendar_events",
        "description": "List the user's upcoming Google Calendar events over the next N days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days from now to look, e.g. 7 for the next week.",
                }
            },
            "required": ["days_ahead"],
        },
    },
]

MAX_FILE_CHARS = 5000  # cap what a single read_file call can pull into context


def _resolve_within_allowed(relative_path):
    """Resolve a model-supplied path and refuse anything that escapes ALLOWED_FOLDER."""
    base = ALLOWED_FOLDER.resolve()
    target = (base / relative_path).resolve()
    if target != base and base not in target.parents:
        raise ValueError("path escapes the allowed folder")
    return target


def run_tool(name, tool_input):
    """Execute one of OUR custom tools and return the result as a string."""
    if name == "get_current_datetime":
        now = datetime.now().astimezone()
        return now.strftime("%A, %B %d, %Y - %I:%M %p (%Z)")

    if name == "remember":
        note = tool_input.get("note", "").strip()
        if not note:
            return "Nothing to remember - empty note."
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(NOTES_FILE, "a", encoding="utf-8") as f:
            f.write(f"- [{timestamp}] {note}\n")
        return "Saved."

    if name == "list_files":
        try:
            target = _resolve_within_allowed(tool_input.get("subfolder") or ".")
        except ValueError:
            return "Error: that path is outside the allowed folder."
        if not target.exists():
            return f"Folder not found: {target}"
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return "\n".join(entries) if entries else "(empty folder)"

    if name == "read_file":
        try:
            target = _resolve_within_allowed(tool_input["path"])
        except ValueError:
            return "Error: that path is outside the allowed folder."
        if not target.is_file():
            return f"Not a file: {target}"
        content = target.read_text(encoding="utf-8", errors="replace")
        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + "\n...[truncated]"
        return content

    if name == "create_calendar_event":
        try:
            link = google_calendar.create_event(
                summary=tool_input["summary"],
                start=tool_input["start"],
                end=tool_input["end"],
                description=tool_input.get("description", ""),
            )
            return f"Event created: {link}"
        except Exception as error:  # Google auth/network/format errors, etc.
            return f"Failed to create event: {error}"

    if name == "list_calendar_events":
        try:
            days = int(tool_input.get("days_ahead", 7))
            now = datetime.now().astimezone()
            time_min = now.isoformat()
            time_max = (now + timedelta(days=days)).isoformat()
            return google_calendar.list_events(time_min, time_max)
        except Exception as error:
            return f"Failed to list events: {error}"

    return f"Unknown tool: {name}"


def build_system_prompt():
    """Combine the base identity with any local context files that exist."""
    prompt = BASE_SYSTEM_PROMPT
    for filename in CONTEXT_FILES:
        path = Path(filename)
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                prompt += f"\n\n# {filename}\n" + content
    return prompt


def extract_text(response):
    """The response is a list of blocks; join the text ones for display."""
    return "\n".join(
        block.text for block in response.content if block.type == "text"
    )


def call_claude(client, system_prompt, messages):
    """
    Call the API and resolve any custom tool calls until Claude is done.
    Mutates `messages` in place (appending every assistant/tool round) and
    returns (final_response, total_input_tokens, total_output_tokens).
    """
    total_input = 0
    total_output = 0

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_RESPONSE_TOKENS,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )
        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens

        # Keep the raw content blocks (not just the text) in the history.
        # This preserves tool_use/tool_result pairs and web search citations
        # correctly across turns.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return response, total_input, total_output

        # Claude wants to call one or more of OUR (client) tools. Server
        # tools like web_search are already resolved by the API and won't
        # show up here as "tool_use" blocks.
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = run_tool(block.name, block.input)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result}
                )

        messages.append({"role": "user", "content": tool_results})


def main():
    # Windows terminals often default to a legacy codepage (cp1252) that
    # can't encode emoji or many other Unicode characters Claude might use.
    # Force UTF-8 on stdout so a reply never crashes the whole program.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    messages = []        # the conversation history
    input_tokens = 0     # running totals to track session spend
    output_tokens = 0

    system_prompt = build_system_prompt()

    print("Jarvis (Level 2) - type 'quit' to exit.")
    loaded = [f for f in CONTEXT_FILES if Path(f).exists()]
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
            response, in_tokens, out_tokens = call_claude(client, system_prompt, messages)
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

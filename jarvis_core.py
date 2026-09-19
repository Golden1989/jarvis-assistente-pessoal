"""
Jarvis - core logic (shared by the terminal UI and the web UI).

Anything that isn't specific to how the user talks to Jarvis lives here:
the system prompt, the tools, and the tool-use loop.
"""

import base64
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from PIL import ImageGrab

import google_calendar

MODEL = "claude-sonnet-5"
MAX_RESPONSE_TOKENS = 1024

# Long-edge cap for screenshots sent to Claude. Oversized images inside a
# tool_result are REJECTED by the API rather than auto-resized, so we shrink
# them ourselves before sending. 1568px keeps us within the standard tier.
MAX_SCREENSHOT_EDGE = 1568

# claude-sonnet-5 pricing (US$ per million tokens).
INPUT_PRICE = 2.0
OUTPUT_PRICE = 10.0

# Anchored to this file's own folder (the project root), NOT the current
# working directory - desktop.py runs with its CWD inside web/, which made
# plain relative filenames here silently resolve to the wrong place.
_BASE_DIR = Path(__file__).resolve().parent

# Optional local files with information for the system prompt. Kept out of Git.
# notes.txt is special: Jarvis also WRITES to it via the "remember" and
# "update_notes" tools, so facts you mention stick around across sessions,
# organized under "## CATEGORY" headings.
CONTEXT_FILES = [
    _BASE_DIR / "personality.txt",
    _BASE_DIR / "profile.txt",
    _BASE_DIR / "people.txt",
    _BASE_DIR / "notes.txt",
]
NOTES_FILE = _BASE_DIR / "notes.txt"
NOTE_CATEGORIES = ["projects", "exams", "preferences", "personal", "log"]

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
    "is in a different language - the latest message always wins. If a "
    "personality section follows, that is how you should actually behave - "
    "not a description to summarize back, an identity to inhabit. If profile "
    "or people sections follow, use them: honor the profile's preferences on "
    "tone and how to address the person, and use the people section as "
    "background when they come up in conversation.\n\n"
    "Memory: notes.txt below (if present) is organized under '## PROJECTS', "
    "'## EXAMS', '## PREFERENCES', '## PERSONAL', and '## LOG' headings. Use "
    "the 'remember' tool to file a new fact under the right category. If an "
    "EXAMS or PROJECTS entry looks time-bound and the date has likely passed "
    "(check with get_current_datetime if unsure), ask her briefly whether "
    "it's still relevant before assuming so - never delete or rewrite a note "
    "silently, always confirm first, then use 'update_notes' to apply the "
    "change. Occasionally, after a substantive discussion (not every single "
    "message), log a short dated one-line summary under LOG via 'remember' "
    "so a future session can recall what was actually discussed."
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
            "Save a fact worth remembering in future conversations, filed "
            "under a category: 'projects' (ongoing work), 'exams' (exams, "
            "deadlines, appointments - anything with a date), 'preferences' "
            "(how she likes things done), 'personal' (life context, venting, "
            "nothing task-related), or 'log' (a short dated one-line summary "
            "of a substantive discussion, so a future session can recall it "
            "- use this occasionally, not for every message). Write the note "
            "as a short, clear, self-contained statement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["projects", "exams", "preferences", "personal", "log"],
                },
                "note": {"type": "string", "description": "The fact to remember."},
            },
            "required": ["category", "note"],
        },
    },
    {
        "name": "update_notes",
        "description": (
            "Replace the ENTIRE contents of the memory file. Use this only "
            "to prune, correct, or consolidate notes - e.g. after she "
            "confirms an exam has passed or a project is done, or to "
            "summarize a category that has grown long. Do not use this for "
            "routine additions - use 'remember' for that. Always confirm "
            "with her before removing something, unless she already told "
            "you to. Preserve the '## CATEGORY' section format."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "new_content": {
                    "type": "string",
                    "description": (
                        "The full new contents of the notes file, using "
                        "'## PROJECTS' / '## EXAMS' / '## PREFERENCES' / "
                        "'## PERSONAL' / '## LOG' section headers."
                    ),
                }
            },
            "required": ["new_content"],
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
    {
        "name": "look_at_screen",
        "description": (
            "Take a screenshot of the user's entire screen right now and "
            "look at it. Only call this when she explicitly asks you to "
            "look at her screen or look at something on it - never on your "
            "own initiative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

MAX_FILE_CHARS = 5000  # cap what a single read_file call can pull into context


def _parse_notes():
    """Parse notes.txt into an ordered {category: [lines]} dict."""
    sections = {cat: [] for cat in NOTE_CATEGORIES}
    path = Path(NOTES_FILE)
    if not path.exists():
        return sections

    current = "personal"  # content written before any heading lands here, not lost
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections.setdefault(current, [])
        elif line.startswith("#"):
            continue  # an old instructional comment line - drop it
        else:
            sections.setdefault(current, [])
            sections[current].append(line)
    return sections


def _serialize_notes(sections):
    """Turn a {category: [lines]} dict back into notes.txt text, known categories first."""
    parts = []
    seen = set()
    for cat in NOTE_CATEGORIES:
        seen.add(cat)
        lines = sections.get(cat) or []
        if lines:
            parts.append(f"## {cat.upper()}\n" + "\n".join(lines))
    for cat, lines in sections.items():
        if cat not in seen and lines:
            parts.append(f"## {cat.upper()}\n" + "\n".join(lines))
    return "\n\n".join(parts) + ("\n" if parts else "")


def _add_note(category, note):
    sections = _parse_notes()
    sections.setdefault(category, [])
    timestamp = datetime.now().strftime("%Y-%m-%d")
    sections[category].append(f"- [{timestamp}] {note}")
    Path(NOTES_FILE).write_text(_serialize_notes(sections), encoding="utf-8")


def _capture_screen():
    """
    Grab a screenshot of the whole screen, downscaled to fit MAX_SCREENSHOT_EDGE.
    Returns a dict describing an image (never a plain string) - call_claude()
    checks for this shape and wraps it as an image content block instead of
    plain text in the tool_result.
    """
    image = ImageGrab.grab(all_screens=True)  # every monitor, not just the primary one
    image.thumbnail((MAX_SCREENSHOT_EDGE, MAX_SCREENSHOT_EDGE))  # keeps aspect ratio
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    data = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"type": "image", "media_type": "image/png", "data": data}


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
        category = tool_input.get("category", "personal")
        if category not in NOTE_CATEGORIES:
            category = "personal"
        if not note:
            return "Nothing to remember - empty note."
        _add_note(category, note)
        return f"Saved under '{category}'."

    if name == "update_notes":
        new_content = tool_input.get("new_content", "").strip()
        Path(NOTES_FILE).write_text(new_content + "\n" if new_content else "", encoding="utf-8")
        return "Notes updated."

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

    if name == "look_at_screen":
        try:
            return _capture_screen()
        except Exception as error:
            return f"Failed to capture the screen: {error}"

    return f"Unknown tool: {name}"


def build_system_prompt():
    """Combine the base identity with any local context files that exist."""
    prompt = BASE_SYSTEM_PROMPT
    for path in CONTEXT_FILES:
        path = Path(path)
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                prompt += f"\n\n# {path.name}\n" + content
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
                # Most tools return plain text. look_at_screen returns an
                # image dict instead (see _capture_screen) - wrap it as an
                # image content block rather than stringifying it.
                if isinstance(result, dict) and result.get("type") == "image":
                    content = [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": result["media_type"],
                                "data": result["data"],
                            },
                        }
                    ]
                else:
                    content = result
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": content}
                )

        messages.append({"role": "user", "content": tool_results})

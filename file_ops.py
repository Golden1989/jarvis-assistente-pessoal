"""
Jarvis's file tools: list/read/create/edit/move, gated entirely by autonomy_scope.resolve() and
recorded in undo_log.py. No delete tool exists here, on purpose - see jarvis_core.py's
"undo last file change" whole-message command for how a reversal happens without ever giving the
model (or injected content) a delete capability.

Every function here takes `scope` (an autonomy_scope.AutonomyScope) and, for the write tools,
`undo` (an undo_log.UndoLog) as explicit parameters rather than reaching for a jarvis_core global -
keeps this module testable on its own and import-free of jarvis_core.

create_file/edit_file/move_file follow the same prep/execute split as jarvis_core's own
remember/open_app: `prep_*(scope, tool_input)` validates and builds the confirmation display
(called both for immediate checks and before holding); `do_*(scope, undo, item_input)` re-resolves
from scratch and performs the write - never trusting whatever `prep_*` saw a moment earlier.
"""
import difflib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import autonomy_scope
import undo_log

# --- size/shape caps - named so they're easy to find and tune, never a bare number inline --------
FILE_READ_MAX_CHARS = 15000       # per read_file call; `offset` pages through anything longer
FILE_READ_MAX_BYTES = 5_000_000   # plain-text source size cap, checked before ever opening the file
FILE_EXTRACT_MAX_SOURCE_BYTES = 50_000_000  # pdf/docx/pptx source size cap, checked before extracting
EXTRACT_MAX_ITEMS = 30             # pages (pdf) / slides (pptx) per extraction
# docx paragraphs are typically much smaller than a pdf page or a pptx slide, so this is raised
# high enough that EXTRACT_TOTAL_MAX_CHARS - not this count - is what actually bounds one
# extraction for an ordinary document; it still exists as a backstop against a pathological file
# with an absurd number of near-empty paragraphs.
EXTRACT_MAX_PARAGRAPH_ITEMS = 100_000
# 4x the per-call read cap - the TOTAL an extraction keeps in memory before read_file paginates
# over it with `offset`; re-extracted fresh on every call (no caching) since this is a learning
# project, not optimized for repeated reads of the same huge document.
EXTRACT_TOTAL_MAX_CHARS = FILE_READ_MAX_CHARS * 4
EXTRACTION_TIMEOUT_SECONDS = 20   # a hostile/malformed document is killed, never left to hang

MAX_LIST_ENTRIES = 300            # a directory listing this long gets truncated with a count note

EDIT_SNIPPET_MAX_CHARS = 15000    # len(old_text) + len(new_text) - bounds what a confirmation
                                   # display and the match/replace itself ever have to handle,
                                   # independent of how large the FILE being edited is
CREATE_FILE_MAX_CHARS = 20000     # create_file's `content` argument

# Allowlist, narrower than the globally-readable set in autonomy_scope.py: these are the only
# extensions create_file/edit_file may write, and the only ones move_file may move a file TO -
# never a script or executable type (.bat/.cmd/.ps1/.js/.vbs/.py/.exe/.lnk/.dll/.reg/.url/...),
# whatever a scope's own `mode` says.
WRITABLE_EXTENSIONS = {".md", ".txt", ".csv", ".json"}

# move_file is more permissive than create/edit, since it never introduces new bytes: a file may
# be moved/renamed WITHOUT changing its extension as long as that extension is in this wider set
# (the writable types plus the extractable, read-only-content types plus .log) - changing the
# extension along the way is only allowed when the DESTINATION extension is in the narrower
# WRITABLE_EXTENSIONS above. Either way, a script/executable source or destination is refused.
MOVABLE_EXTENSIONS = WRITABLE_EXTENSIONS | autonomy_scope.EXTRACTABLE_EXTENSIONS | {".log"}

# Windows "Files On-Demand" attributes - set on a OneDrive file that exists only in the cloud.
# Reading it would silently trigger a download (slow, or failing outright if offline) instead of a
# normal local read, so it's refused instead.
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

_EXTRACT_WORKER = Path(__file__).resolve().parent / "extract_worker.py"


def _is_cloud_placeholder(path):
    try:
        attrs = os.stat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & (_FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))


def _paginate(content, offset, next_item=None):
    """Two independent continuation axes, never confused with each other: `offset` pages through
    MORE TEXT already captured by this same extraction window; `next_item` (only offered once
    offset-pagination of THIS window is exhausted) means more pages/slides/paragraph-blocks exist
    beyond the window itself - continuing needs a fresh call with `page=next_item`, not a bigger
    offset, since a bigger offset on the SAME window just re-derives the same capped text."""
    offset = max(0, offset)
    chunk = content[offset:offset + FILE_READ_MAX_CHARS]
    remaining = len(content) - offset - len(chunk)
    if remaining > 0:
        chunk += (f"\n...[{remaining} more characters in this extraction - call again with "
                  f"offset={offset + len(chunk)}]")
    elif next_item is not None:
        chunk += (f"\n...[more pages/slides/paragraph-blocks exist beyond this extraction - call "
                  f"again with page={next_item} and offset=0]")
    return chunk


def _extract(file_type, path, start_item):
    """-> (text, next_item, error). Runs extract_worker.py in its own process, isolated (-I) and
    timeout-bound, so a hostile document can only ever produce a clear refusal here, never a hang
    or a crash in this process. `next_item` is the 1-based item index to resume from with a fresh
    `page=` call, or None if the window already reached the end of the document."""
    max_items = EXTRACT_MAX_PARAGRAPH_ITEMS if file_type == "docx" else EXTRACT_MAX_ITEMS
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(_EXTRACT_WORKER), "--type", file_type, "--path", str(path),
             "--start-item", str(max(1, start_item)), "--max-items", str(max_items),
             "--max-chars", str(EXTRACT_TOTAL_MAX_CHARS)],
            capture_output=True, text=True, timeout=EXTRACTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, None, "extraction took too long and was stopped"
    except OSError as error:
        return None, None, f"could not start extraction ({error})"
    if result.returncode != 0 or not result.stdout.strip():
        return None, None, "extraction failed"
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except ValueError:
        return None, None, "extraction produced an unreadable result"
    if not payload.get("ok"):
        return None, None, payload.get("error", "unknown extraction error")
    return payload.get("text", ""), payload.get("next_item"), None


# --- list_files / read_file: level 0, never held, always tainting ---------------------------------

def list_files(scope, raw_path):
    try:
        target = scope.resolve(raw_path, for_write=False)
    except autonomy_scope.ScopeError as error:
        return f"Error: {error}"
    if not target.path.is_dir():
        return f"Not a folder: {target.path}"
    try:
        children = sorted(target.path.iterdir(), key=lambda p: p.name.lower())
    except OSError as error:
        return f"Could not list this folder ({error})."
    entries, hidden = [], 0
    for child in children:
        try:
            scope.resolve(str(child), for_write=False)
        except (autonomy_scope.DeniedNameError, autonomy_scope.ProtectedPathError):
            hidden += 1
            continue
        except autonomy_scope.ScopeError:
            pass  # still shown - e.g. an extension a scope doesn't allow WRITING is fine to LIST
        if len(entries) < MAX_LIST_ENTRIES:
            entries.append(child.name + ("/" if child.is_dir() else ""))
    shown_total = len(children) - hidden
    if shown_total > MAX_LIST_ENTRIES:
        entries.append(f"...[{shown_total - MAX_LIST_ENTRIES} more not shown]")
    return "\n".join(entries) if entries else "(empty folder)"


def read_file(scope, raw_path, offset=0, page=1):
    try:
        target = scope.resolve(raw_path, for_write=False)
    except autonomy_scope.ScopeError as error:
        return f"Error: {error}"
    path = target.path
    if not path.is_file():
        return f"Not a file: {path}"
    if _is_cloud_placeholder(path):
        return ('This file is a OneDrive cloud-only placeholder - make it "Always keep on this '
                'device" first, then try again.')
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError as error:
        return f"Could not read this file ({error})."
    if ext in autonomy_scope.PLAIN_TEXT_EXTENSIONS:
        if size > FILE_READ_MAX_BYTES:
            return f"This file is too large to read ({size} bytes, cap is {FILE_READ_MAX_BYTES})."
        content = path.read_text(encoding="utf-8", errors="replace")
        return _paginate(content, offset)
    if ext in autonomy_scope.EXTRACTABLE_EXTENSIONS:
        if size > FILE_EXTRACT_MAX_SOURCE_BYTES:
            return (f"This file is too large to extract ({size} bytes, cap is "
                    f"{FILE_EXTRACT_MAX_SOURCE_BYTES}).")
        text, next_item, error = _extract(ext.lstrip("."), path, int(page or 1))
        if error:
            return f"Could not read this file: {error}"
        return _paginate(text, offset, next_item=next_item)
    return f"Unsupported file type '{ext or '(none)'}' - not readable."


def _read_text_strict_for_edit(path):
    """-> (text, error). Used by BOTH prep_edit_file and do_edit_file - the file size is checked
    against FILE_READ_MAX_BYTES before ever opening it (same cap read_file itself uses), and the
    bytes are decoded STRICT utf-8, never errors="replace": a file we are about to WRITE BACK must
    never have had a byte silently swapped for U+FFFD on the way in. Reading raw bytes and
    decoding them directly - never Path.read_text(), which opens in universal-newline text mode -
    means \\r\\n is still literally "\\r\\n" in the returned string, so an edit never converts a
    file's line endings between LF and CRLF as a side effect."""
    try:
        size = path.stat().st_size
    except OSError as error:
        return None, f"Could not read this file ({error})."
    if size > FILE_READ_MAX_BYTES:
        return None, f"This file is too large to edit ({size} bytes, cap is {FILE_READ_MAX_BYTES})."
    try:
        raw = path.read_bytes()
    except OSError as error:
        return None, f"Could not read this file ({error})."
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "This file is not valid UTF-8 text - refusing to edit it."


# --- create_file / edit_file / move_file: level 2, always held ------------------------------------

def prep_create_file(scope, tool_input):
    raw_path = tool_input.get("path")
    content = tool_input.get("content", "")
    try:
        target = scope.resolve(raw_path, for_write=True)
    except autonomy_scope.ScopeError as error:
        return False, None, f"Could not create this file: {error}"
    ext = target.path.suffix.lower()
    if ext not in WRITABLE_EXTENSIONS:
        return False, None, (f"Cannot create a '{ext or '(no extension)'}' file - only "
                             f"{sorted(WRITABLE_EXTENSIONS)} are writable.")
    if target.path.exists():
        return False, None, "That file already exists - use edit_file to change it, not create_file."
    if len(content) > CREATE_FILE_MAX_CHARS:
        return False, None, f"That content is too long (max {CREATE_FILE_MAX_CHARS} characters)."
    preview = "\n".join(content.splitlines()[:5])
    display = f"create {target.path} ({len(content)} chars)"
    if preview:
        display += f'\n   + "{preview[:200]}"'
    return True, {"path": str(target.path), "content": content}, display


def do_create_file(scope, undo, item_input):
    try:
        target = scope.resolve(item_input["path"], for_write=True)
    except autonomy_scope.ScopeError as error:
        return f"Error: could not create this file: {error}"
    if target.path.exists():
        return "Error: that file was created by something else since this was proposed - nothing done."
    tmp = target.path.with_name(target.path.name + ".tmp")
    try:
        tmp.write_text(item_input["content"], encoding="utf-8")
        os.replace(tmp, target.path)
    except OSError as error:
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"Error: could not create this file ({error})."
    undo.record("create", target.path, post_hash=undo_log.sha256_of(target.path))
    return f"Created {target.path}."


def prep_edit_file(scope, tool_input):
    raw_path = tool_input.get("path")
    old_text = tool_input.get("old_text", "")
    new_text = tool_input.get("new_text", "")
    try:
        target = scope.resolve(raw_path, for_write=True)
    except autonomy_scope.ScopeError as error:
        return False, None, f"Could not edit this file: {error}"
    ext = target.path.suffix.lower()
    if ext not in WRITABLE_EXTENSIONS:
        return False, None, (f"Cannot edit a '{ext or '(no extension)'}' file - only "
                             f"{sorted(WRITABLE_EXTENSIONS)} are writable.")
    if not target.path.is_file():
        return False, None, "That file does not exist - use create_file instead."
    if len(old_text) + len(new_text) > EDIT_SNIPPET_MAX_CHARS:
        return False, None, (f"This change is too large to confirm safely (max "
                             f"{EDIT_SNIPPET_MAX_CHARS} combined characters of old_text+new_text).")
    current, error = _read_text_strict_for_edit(target.path)
    if error:
        return False, None, error
    count = current.count(old_text) if old_text else 0
    if count == 0:
        return False, None, "That exact text was not found in the file - nothing to replace."
    if count > 1:
        return False, None, f"That text appears {count} times - too ambiguous to replace automatically."
    diff_lines = list(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm=""))
    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
    display = f"edit {target.path} (+{added}/-{removed} lines)\n" + "\n".join(diff_lines[:40])
    return True, {"path": str(target.path), "old_text": old_text, "new_text": new_text}, display


def do_edit_file(scope, undo, item_input):
    try:
        target = scope.resolve(item_input["path"], for_write=True)
    except autonomy_scope.ScopeError as error:
        return f"Error: could not edit this file: {error}"
    if not target.path.is_file():
        return "Error: that file is gone - nothing done."
    old_text, new_text = item_input["old_text"], item_input["new_text"]
    current, error = _read_text_strict_for_edit(target.path)
    if error:
        return f"Error: {error}"
    if current.count(old_text) != 1:
        return ("Error: the file changed since this edit was proposed (the exact text to replace "
                "no longer matches exactly once) - nothing was changed.")
    backup_path = undo.new_backup_path(target.path.name)
    try:
        shutil.copyfile(target.path, backup_path)
    except OSError as error:
        return f"Error: could not back up this file first ({error}) - nothing was changed."
    new_content = current.replace(old_text, new_text, 1)
    tmp = target.path.with_name(target.path.name + ".tmp")
    try:
        # write_bytes, never write_text: write_text's default newline translation on Windows
        # would turn a bare "\n" back into "\r\n" even if the file originally used plain LF -
        # encode() does no newline translation at all, so whatever line endings
        # _read_text_strict_for_edit preserved come out exactly as they went in.
        tmp.write_bytes(new_content.encode("utf-8"))
        os.replace(tmp, target.path)
    except OSError as error:
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"Error: could not save this edit ({error})."
    undo.record("edit", target.path, backup_path=backup_path, post_hash=undo_log.sha256_of(target.path))
    return f"Edited {target.path}."


def _move_extension_error(src_ext, dst_ext):
    """None if this (source extension, destination extension) pair is allowed, else the refusal
    text. Keeping the same extension is fine for anything in MOVABLE_EXTENSIONS (a file never
    gains new bytes from a move); CHANGING the extension is only fine when the destination is one
    of the narrower WRITABLE_EXTENSIONS. A script/executable source is refused either way."""
    if src_ext not in MOVABLE_EXTENSIONS:
        return (f"Cannot move a '{src_ext or '(no extension)'}' file - only "
                f"{sorted(MOVABLE_EXTENSIONS)} can be moved.")
    if dst_ext != src_ext and dst_ext not in WRITABLE_EXTENSIONS:
        return (f"Cannot move to a '{dst_ext or '(no extension)'}' destination - only "
                f"{sorted(WRITABLE_EXTENSIONS)} (or keeping the same extension) are allowed.")
    return None


def prep_move_file(scope, tool_input):
    try:
        source = scope.resolve(tool_input.get("path"), for_write=True)
    except autonomy_scope.ScopeError as error:
        return False, None, f"Could not move this file: {error}"
    try:
        dest = scope.resolve(tool_input.get("new_path"), for_write=True)
    except autonomy_scope.ScopeError as error:
        return False, None, f"Could not move to that destination: {error}"
    ext_error = _move_extension_error(source.path.suffix.lower(), dest.path.suffix.lower())
    if ext_error:
        return False, None, ext_error
    if not source.path.is_file():
        return False, None, "That file does not exist."
    if dest.path.exists():
        return False, None, "Something already exists at the destination - nothing was moved."
    display = f"move {source.path} -> {dest.path}"
    return True, {"path": str(source.path), "new_path": str(dest.path)}, display


def do_move_file(scope, undo, item_input):
    try:
        source = scope.resolve(item_input["path"], for_write=True)
    except autonomy_scope.ScopeError as error:
        return f"Error: could not move this file: {error}"
    try:
        dest = scope.resolve(item_input["new_path"], for_write=True)
    except autonomy_scope.ScopeError as error:
        return f"Error: could not move to that destination: {error}"
    ext_error = _move_extension_error(source.path.suffix.lower(), dest.path.suffix.lower())
    if ext_error:
        return f"Error: {ext_error}"
    if not source.path.is_file():
        return "Error: that file is gone - nothing done."
    if dest.path.exists():
        return "Error: something already exists at the destination now - nothing was moved."
    try:
        shutil.move(str(source.path), str(dest.path))
    except OSError as error:
        return f"Error: could not move this file ({error})."
    undo.record("move", dest.path, original_path=source.path, post_hash=undo_log.sha256_of(dest.path))
    return f"Moved {source.path} -> {dest.path}."


# --- "undo last file change" - a fixed, whole-message, NON-model command (see jarvis_core.py) -----

def undo_last_file_change(scope, undo):
    """Reverses ONLY the single most recent create/edit/move still in the log - never trusting it
    blindly: every path is re-resolved through scope.resolve(for_write=True) again, and the
    target's CURRENT sha256 must still match what was recorded when that change was made,
    otherwise this refuses and says why instead of reversing something that changed since. Her
    "undo last file change" command is whole-message and stateless: saying it AGAIN after a
    successful undo pops and reverses the entry that is now newest, walking back one entry at a
    time, each one re-hash-checked on its own - there is no "undo everything" or "redo"."""
    entry = undo.last()
    if entry is None:
        return "Nothing to undo - no file changes recorded."
    try:
        if entry["action"] == "create":
            target = scope.resolve(entry["target_path"], for_write=True)
            if not target.path.exists():
                undo.pop_last()
                return f"Nothing to undo - {target.path} is already gone."
            if undo_log.sha256_of(target.path) != entry["post_hash"]:
                return f"Could not undo: {target.path} changed since it was created - nothing was removed."
            target.path.unlink()
            undo.pop_last()
            return f"Undone: removed {target.path} (it was just created)."

        if entry["action"] == "edit":
            target = scope.resolve(entry["target_path"], for_write=True)
            if not target.path.exists() or undo_log.sha256_of(target.path) != entry["post_hash"]:
                return f"Could not undo: {target.path} changed since that edit - nothing was restored."
            if not entry["backup_path"] or not Path(entry["backup_path"]).is_file():
                return "Could not undo: the backup for that edit is missing."
            shutil.copyfile(entry["backup_path"], target.path)
            undo.pop_last()
            return f"Undone: restored {target.path} to before that edit."

        if entry["action"] == "move":
            new_target = scope.resolve(entry["target_path"], for_write=True)
            old_target = scope.resolve(entry["original_path"], for_write=True)
            if not new_target.path.exists() or undo_log.sha256_of(new_target.path) != entry["post_hash"]:
                return f"Could not undo: {new_target.path} changed since that move - nothing was moved back."
            if old_target.path.exists():
                return f"Could not undo: something already exists at {old_target.path} - nothing was moved."
            shutil.move(str(new_target.path), str(old_target.path))
            undo.pop_last()
            return f"Undone: moved {old_target.path} back from {new_target.path}."
    except autonomy_scope.ScopeError as error:
        return f"Could not undo: {error}"
    return "Could not undo: unrecognized action in the log."

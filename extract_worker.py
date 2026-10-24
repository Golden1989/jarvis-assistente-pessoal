"""
Standalone worker invoked via subprocess (with -I and a timeout - see file_ops.py) to extract text
from a PDF/DOCX/PPTX. Runs in its OWN process so a hostile or malformed document can be killed by
the timeout instead of hanging or crashing the main Jarvis process - a document from a forensic
folder is adversarial input, not a trusted file.

Prints exactly ONE line of JSON to stdout and nothing else (no logging, no progress text), so the
parent can always parse it: {"ok": true, "text": "...", "next_item": int|null} or
{"ok": false, "error": "..."}. `next_item` is the 1-based index (page/slide/paragraph-block) to
resume from with a fresh --start-item if there's more beyond this extraction's --max-items window;
null means the window already reached the end of the document. Never raises past main() - any
exception becomes an "ok": false result instead of a traceback, so a crash here can never look like
anything but a clean refusal to the caller.

.docx/.pptx are OOXML zip containers: before handing them to python-docx/python-pptx (which would
otherwise decompress every member into memory), _check_zip_safety inspects the central directory
alone - member count, total uncompressed size, and each member's own compression ratio - and
refuses a would-be zip bomb before a single byte is inflated.
"""
import argparse
import json
import sys
import zipfile

MAX_ZIP_MEMBERS = 2000
MAX_ZIP_UNCOMPRESSED_BYTES = 200_000_000
MAX_ZIP_COMPRESSION_RATIO = 100


def _check_zip_safety(path):
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                return f"too many zip members ({len(infos)})"
            total_uncompressed = 0
            for info in infos:
                total_uncompressed += info.file_size
                if info.compress_size > 0 and info.file_size / info.compress_size > MAX_ZIP_COMPRESSION_RATIO:
                    return "a zip member's compression ratio is implausibly high"
            if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                return "the uncompressed content would be too large"
    except zipfile.BadZipFile as error:
        return f"not a valid zip/office file ({error})"
    return None


def _window(total, start_item, max_items):
    """(start_index, end_index, next_item) - a 0-based [start_index:end_index) slice plus the
    1-based item number to resume from, or None if this window reaches the end."""
    start_index = max(0, start_item - 1)
    end_index = min(total, start_index + max_items)
    next_item = end_index + 1 if end_index < total else None
    return start_index, end_index, next_item


def _cap_chars(text, max_chars):
    return text[:max_chars] if len(text) > max_chars else text


def extract_pdf(path, start_item, max_items, max_chars):
    try:
        import pypdf
    except ImportError:
        return None, None, "pypdf is not installed"
    try:
        reader = pypdf.PdfReader(path)
    except Exception as error:
        return None, None, f"could not open this PDF ({error})"
    pages = reader.pages
    start_index, end_index, next_item = _window(len(pages), start_item, max_items)
    chunks = []
    for page in pages[start_index:end_index]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            chunks.append("")
    return _cap_chars("\n".join(chunks), max_chars), next_item, None


def extract_docx(path, start_item, max_items, max_chars):
    error = _check_zip_safety(path)
    if error:
        return None, None, error
    try:
        import docx
    except ImportError:
        return None, None, "python-docx is not installed"
    try:
        document = docx.Document(path)
    except Exception as error:
        return None, None, f"could not open this document ({error})"
    paragraphs = document.paragraphs
    start_index, end_index, next_item = _window(len(paragraphs), start_item, max_items)
    chunks = [p.text for p in paragraphs[start_index:end_index]]
    # Tables aren't part of `paragraphs`' ordering, and python-docx has no single mixed-order
    # "body items" API worth relying on here - appended once, after the paragraph window, clearly
    # labeled so the model knows these are table cells, not running prose. Only on the FIRST page:
    # tables aren't paginated by paragraph count, and there's no natural "which table belongs with
    # which paragraph window" mapping worth building for this.
    if start_item == 1 and document.tables:
        for t_index, table in enumerate(document.tables):
            rows = ["\t".join(cell.text for cell in row.cells) for row in table.rows]
            chunks.append(f"[table {t_index + 1}]\n" + "\n".join(rows))
    return _cap_chars("\n".join(chunks), max_chars), next_item, None


def _pptx_shape_text(shape, mso_shape_type):
    """(text, saw_unsupported) for one shape, recursing one level into a grouped shape. text is
    "" for a shape type that simply has none (e.g. a plain picture) - saw_unsupported is only True
    for a shape type whose text we deliberately do NOT extract (a chart), so the caller can report
    that honestly instead of silently skipping it."""
    if shape.shape_type == mso_shape_type.GROUP:
        parts, saw_unsupported = [], False
        for sub in shape.shapes:
            sub_text, sub_unsupported = _pptx_shape_text(sub, mso_shape_type)
            if sub_text:
                parts.append(sub_text)
            saw_unsupported = saw_unsupported or sub_unsupported
        return "\n".join(parts), saw_unsupported
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        return shape.text_frame.text, False
    if getattr(shape, "has_table", False) and shape.has_table:
        rows = ["\t".join(cell.text for cell in row.cells) for row in shape.table.rows]
        return "\n".join(rows), False
    if getattr(shape, "has_chart", False) and shape.has_chart:
        return "", True
    return "", False


def extract_pptx(path, start_item, max_items, max_chars):
    error = _check_zip_safety(path)
    if error:
        return None, None, error
    try:
        import pptx
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except ImportError:
        return None, None, "python-pptx is not installed"
    try:
        presentation = pptx.Presentation(path)
    except Exception as error:
        return None, None, f"could not open this presentation ({error})"
    slides = list(presentation.slides)
    start_index, end_index, next_item = _window(len(slides), start_item, max_items)
    chunks, saw_unsupported = [], False
    for slide in slides[start_index:end_index]:
        parts = []
        for shape in slide.shapes:
            text, unsupported = _pptx_shape_text(shape, MSO_SHAPE_TYPE)
            if text:
                parts.append(text)
            saw_unsupported = saw_unsupported or unsupported
        chunks.append("\n".join(parts))
    if saw_unsupported:
        chunks.append("[note: this presentation has chart content whose text was not extracted]")
    return _cap_chars("\n".join(chunks), max_chars), next_item, None


EXTRACTORS = {"pdf": extract_pdf, "docx": extract_docx, "pptx": extract_pptx}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", required=True, choices=sorted(EXTRACTORS))
    parser.add_argument("--path", required=True)
    parser.add_argument("--start-item", type=int, default=1)
    parser.add_argument("--max-items", type=int, required=True)
    parser.add_argument("--max-chars", type=int, required=True)
    args = parser.parse_args()

    try:
        text, next_item, error = EXTRACTORS[args.type](args.path, args.start_item, args.max_items, args.max_chars)
    except Exception as error:  # a hostile/malformed file must never crash this with a traceback
        print(json.dumps({"ok": False, "error": f"extraction failed ({error})"}))
        return
    if error:
        print(json.dumps({"ok": False, "error": error}))
        return
    if not text or not text.strip():
        print(json.dumps({"ok": False, "error": "no extractable text (scanned or encrypted?)"}))
        return
    print(json.dumps({"ok": True, "text": text, "next_item": next_item}))


if __name__ == "__main__":
    main()

"""
Fixed list of trusted tech-news feeds for Jarvis's "good morning" briefing (jarvis_core.py).

Feed URLs (and the host each one is expected to be served from) are HARDCODED CONSTANTS below -
NEVER taken from a message, model output, or a tool result. Verified live (plain GETs made only
to these four sites, nothing saved from the responses) before writing this file:
  - Cisco Talos:      https://feeds.feedburner.com/feedburner/Talos   (articles: blog.talosintelligence.com)
  - BleepingComputer:  https://www.bleepingcomputer.com/feed/         (articles: www.bleepingcomputer.com)
  - The DFIR Report:   https://thedfirreport.com/feed/                (articles: thedfirreport.com)
  - CISA (Alerts):     https://www.cisa.gov/cybersecurity-advisories/alerts.xml (articles: www.cisa.gov)
    (CISA's "all advisories" feed is also real but multiple MB of historical data - far past our
    response-size cap below and not needed for a last-48h briefing, so "Alerts" is used instead.)

Topics covered today: cybersecurity, digital forensics/DFIR. Adding another source - or another
topic, once an AI-news source exists - is a one-line addition to SOURCES; nothing else here or in
jarvis_core.py needs to change for that.
"""
import html
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

SOURCES = [
    {"name": "Cisco Talos", "feed_url": "https://feeds.feedburner.com/feedburner/Talos",
     "host": "feeds.feedburner.com", "allowed_link_hosts": {"blog.talosintelligence.com"}},
    {"name": "BleepingComputer", "feed_url": "https://www.bleepingcomputer.com/feed/",
     "host": "www.bleepingcomputer.com", "allowed_link_hosts": {"www.bleepingcomputer.com"}},
    {"name": "The DFIR Report", "feed_url": "https://thedfirreport.com/feed/",
     "host": "thedfirreport.com", "allowed_link_hosts": {"thedfirreport.com"}},
    {"name": "CISA", "feed_url": "https://www.cisa.gov/cybersecurity-advisories/alerts.xml",
     "host": "www.cisa.gov", "allowed_link_hosts": {"www.cisa.gov"}},
]

FETCH_TIMEOUT_SECONDS = 6
MAX_RESPONSE_BYTES = 1_000_000  # ~1 MB
MAX_AGE_HOURS = 48
MAX_ITEMS_PER_SOURCE = 8
MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 400
MAX_WORKERS = 4  # one worker per source today - see fetch_all()'s docstring

ATOM_NS = "{http://www.w3.org/2005/Atom}"
_UTF_16_32_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")  # LE/BE UTF-16, BE UTF-32 (LE UTF-32 starts with \xff\xfe too)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class FeedError(Exception):
    """A specific, printable reason one source couldn't be fetched - fetch_feed() always turns
    this (and every other failure) into a short string instead of letting it crash the briefing."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Installed on every request's opener: refuses ANY redirect, even to the same host. A feed
    that starts redirecting is a one-line SOURCES update here, never something followed
    automatically - this is what stands in for "no redirects to another host"."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FeedError(f"refused a redirect to {newurl}")


def default_fetcher(url, timeout=FETCH_TIMEOUT_SECONDS):
    """
    The one real-network seam - tests replace this with a fake (see tests/_helpers.py), and under
    JARVIS_TESTING this refuses to run unless a test did. Returns the raw response body (bytes),
    reading at most MAX_RESPONSE_BYTES + 1 - an oversized response is cut off at that point
    regardless of what Content-Length claims, so fetch_feed() can reject it without ever having
    downloaded all of it.
    """
    if os.environ.get("JARVIS_TESTING"):
        raise RuntimeError(
            "JARVIS_TESTING is set and no fake fetcher was passed to fetch_feed/fetch_all - "
            "refusing to touch the real network from a test."
        )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "Jarvis-briefing/1.0"})
    with opener.open(request, timeout=timeout) as response:
        return response.read(MAX_RESPONSE_BYTES + 1)


def _host_matches(url, allowed_hosts):
    """True if `url` is https and its host is EXACTLY one of allowed_hosts - never endswith/
    substring, so "evil-bleepingcomputer.com.attacker.net" can never pass by merely containing or
    ending in a trusted name."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in allowed_hosts


def _text(element):
    return (element.text or "").strip() if element is not None else ""


def _parse_rfc822(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_iso8601(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_rss(root):
    channel = root.find("channel")
    if channel is None:
        return []
    out = []
    for item in channel.findall("item"):
        out.append({
            "title": _text(item.find("title")),
            "link": _text(item.find("link")),
            "summary": _text(item.find("description")),
            "published": _parse_rfc822(_text(item.find("pubDate"))),
        })
    return out


def _parse_atom(root):
    out = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        link_el = entry.find(f"{ATOM_NS}link")
        summary_el = entry.find(f"{ATOM_NS}summary")
        if summary_el is None:
            summary_el = entry.find(f"{ATOM_NS}content")
        out.append({
            "title": _text(entry.find(f"{ATOM_NS}title")),
            "link": link_el.get("href", "") if link_el is not None else "",
            "summary": _text(summary_el),
            "published": _parse_iso8601(_text(entry.find(f"{ATOM_NS}updated"))
                                         or _text(entry.find(f"{ATOM_NS}published"))),
        })
    return out


def _clean_text(value, max_len):
    """Strips HTML tags and control characters, unescapes entities, collapses whitespace
    (including former tags' newlines) to single spaces, and caps the length."""
    text = html.unescape(value or "")
    text = _TAG_RE.sub(" ", text)
    text = _CONTROL_CHARS_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:max_len]


def _filter_and_sanitize(raw_items, source):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    # Undated items (a feed with a malformed pubDate) are dropped rather than assumed recent.
    recent = [it for it in raw_items if it["published"] is not None and it["published"] >= cutoff]
    recent.sort(key=lambda it: it["published"], reverse=True)
    out = []
    for it in recent[:MAX_ITEMS_PER_SOURCE]:
        link = it["link"]
        out.append({
            "source": source["name"],
            "title": _clean_text(it["title"], MAX_TITLE_CHARS),
            "summary": _clean_text(it["summary"], MAX_SUMMARY_CHARS),
            "link": link if _host_matches(link, source["allowed_link_hosts"]) else None,
        })
    return out


def fetch_feed(source, fetcher=default_fetcher):
    """
    (items, error): `error` is a short, printable reason ("timeout", "HTTP 404", "bad XML", ...)
    when nothing could be fetched - in which case `items` is always []; otherwise `error` is None
    and `items` is the sanitized, 48h-filtered, capped list for this one source. Never raises:
    every failure is caught here so one bad feed can never take the whole briefing down with it.
    """
    url = source["feed_url"]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != source["host"]:
        return [], "feed_url does not match the source's expected https host"

    try:
        raw = fetcher(url)
    except FeedError as error:
        return [], str(error)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
        return [], str(error) or error.__class__.__name__
    except Exception as error:  # belt and suspenders - this must never crash the briefing
        return [], f"unexpected error: {error}"

    if len(raw) > MAX_RESPONSE_BYTES:
        return [], f"response too large (> {MAX_RESPONSE_BYTES} bytes)"

    # A NUL byte or a UTF-16/32 BOM means this is not the UTF-8 (or plain ASCII) document every
    # source here is expected to serve - and, checked BEFORE the DOCTYPE string search below,
    # closes a real bypass: "<!DOCTYPE" encoded as UTF-16 has a NUL byte after every character
    # (e.g. b"<\x00!\x00D\x00..."), so that literal byte sequence would never match the ASCII
    # needle below, even though ET.fromstring() (which sniffs the encoding itself) could still
    # parse the document and honor a live DOCTYPE/ENTITY inside it - refusing here closes that
    # gap rather than relying on the DOCTYPE check to catch every encoding.
    if b"\x00" in raw or raw.startswith(_UTF_16_32_BOMS):
        return [], "refused: not UTF-8"

    # Checked on the raw BYTES, case-insensitively, BEFORE any XML parsing - an entity-expansion
    # ("billion laughs") or external-entity attack needs a DOCTYPE/ENTITY declaration to work at
    # all, so refusing documents that have one removes the attack surface without needing a new
    # dependency (defusedxml) just for this.
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return [], "refused: document declares a DOCTYPE/ENTITY"

    try:
        # Parsed from BYTES, not a decoded str: xml.etree/expat raises ValueError on a Unicode
        # str that still has an `encoding=` XML declaration (which is how real-world RSS/Atom
        # almost always starts) - bytes avoids that pitfall and lets expat honor the declared
        # encoding itself.
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        return [], f"bad XML: {error}"

    tag = root.tag.rsplit("}", 1)[-1].lower()  # strip a namespace like "{...}feed" if present
    try:
        if tag == "rss":
            raw_items = _parse_rss(root)
        elif tag == "feed":
            raw_items = _parse_atom(root)
        else:
            return [], f"unrecognized feed root element: {tag!r}"
    except Exception as error:
        return [], f"could not parse feed: {error}"

    return _filter_and_sanitize(raw_items, source), None


def fetch_all(sources=None, fetcher=default_fetcher):
    """
    (items, unavailable) across every source - `items` is a single flat list (already filtered/
    capped/sanitized per source, in no particular cross-source order), `unavailable` is
    [{"name", "reason"}, ...] for sources that failed.

    Fetches concurrently with a small ThreadPoolExecutor: four short, mostly-I/O-wait HTTP GETs
    are exactly the kind of work threads (not processes) are suited for in Python - the GIL is
    released while each one blocks on the network, so they genuinely overlap even though only one
    Python thread runs bytecode at a time. The pool size is capped at MAX_WORKERS (today: 4, one
    per source) - never grows with anything the user or a message could influence.
    """
    sources = sources if sources is not None else SOURCES
    items, unavailable = [], []
    with ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(sources)))) as pool:
        futures = {pool.submit(fetch_feed, source, fetcher): source for source in sources}
        for future, source in futures.items():
            feed_items, error = future.result()
            if error:
                unavailable.append({"name": source["name"], "reason": error})
            else:
                items.extend(feed_items)
    return items, unavailable

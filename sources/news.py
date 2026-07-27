"""
News adapter — pulls Google News RSS feeds and normalizes to the standard item shape.

Feed URL: https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en
Supported query operators: OR, quoted phrases, site:, when:Nd

Each item is normalized to match the legistar adapter output so Stage 1, Stage 2,
dedup, and digest all work unchanged.
"""

import logging
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_FEED_BASE = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def fetch(source_cfg: dict, recent_days: int = 14) -> list[dict]:
    """
    Fetch one Google News RSS query.

    source_cfg keys:
      query        (required) — the search string, passed verbatim to Google
      jurisdiction (optional) — label to use as jurisdiction; defaults to "News"
      name         (optional) — human label used only in log lines

    recent_days: drop any article older than this many days. News is a
    point-in-time event, not a live matter, so it has to age out or a single
    story rides the brief forever. Undated articles are dropped too, since
    without a real date they cannot age and would re-register as NEW every run.

    Returns normalized items:
      {jurisdiction, body, meeting_date, title, url, doc_text, meeting_doc}
    """
    query = source_cfg.get("query", "")
    jurisdiction = source_cfg.get("jurisdiction", "News")
    name = source_cfg.get("name", query[:50])

    feed_url = _FEED_BASE.format(query=quote_plus(query))
    log.info(f"News [{name}]: fetching RSS")

    socket.setdefaulttimeout(15)
    feed = feedparser.parse(feed_url, request_headers={"User-Agent": "CountyWatch/1.0"})
    if feed.bozo and not feed.entries:
        log.warning(f"News [{name}]: feed error — {getattr(feed, 'bozo_exception', '?')}")
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_days)).date()
    entries = feed.entries
    items = []
    dropped_old = 0
    dropped_undated = 0
    for entry in entries:
        title = _clean(entry.get("title", ""))
        if not title:
            continue

        pub = _pub_date(entry)
        if pub is None:
            dropped_undated += 1
            continue
        if pub < cutoff:
            dropped_old += 1
            continue

        url = entry.get("link", "")  # Google link used as-is; redirect resolution skipped

        items.append({
            "jurisdiction": jurisdiction,
            "body": _outlet_name(entry),
            "meeting_date": pub.isoformat(),
            "title": title,
            "url": url,
            "doc_text": _clean(entry.get("summary") or entry.get("description") or ""),
            "meeting_doc": "",
        })
        time.sleep(0.05)

    log.info(f"News [{name}]: {len(entries)} entries, {len(items)} in window "
             f"({dropped_old} too old, {dropped_undated} undated)")
    return items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _outlet_name(entry) -> str:
    """Extract the publication name from a feedparser entry."""
    src = getattr(entry, "source", None)
    if src:
        name = getattr(src, "title", None)
        if name:
            return name
    # Fallback: Google News titles end with " - Publication Name"
    title = entry.get("title", "")
    if " - " in title:
        return title.rsplit(" - ", 1)[-1].strip()
    return "Unknown"


def _pub_date(entry):
    """Return the article's published date, or None if it has none.

    No today-fallback: an undated article can't be aged, and a today-stamp made
    the same story re-register as NEW every run. None means drop it.
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:3], tzinfo=timezone.utc).date()
    except Exception:
        return None


def _clean(text: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    if not text:
        return ""
    return BeautifulSoup(text, "lxml").get_text(" ", strip=True)

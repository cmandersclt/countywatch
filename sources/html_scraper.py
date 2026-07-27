"""
html_scraper.py — adapter for county sites that are not on Legistar.

Covers the Yogi footprint: Payette County, ID (agendas posted inline as HTML
tables) and Malheur County, OR (whole site behind a JavaScript gate that a
plain request cannot pass). fetch() matches the legistar adapter signature and
returns the same normalized item shape, so Stage 1, Stage 2, dedup, and the
digest all work unchanged.

Item shape: {jurisdiction, body, meeting_date, title, url, doc_text, meeting_doc}
For a county agenda we return ONE item per meeting: the whole agenda is the
doc_text, so the Stage 1 keyword scan (title + doc_text) catches Koloma, seismic,
etc. anywhere on the agenda and passes it to Claude.
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_SESSION = requests.Session()
_SESSION.verify = False
_SESSION.headers.update({
    "User-Agent": "CountyWatch/1.0 (public-records monitor; cmanders38@gmail.com)"
})

_MONTHS = ("January|February|March|April|May|June|July|August|September|"
           "October|November|December")
_DATE_RE = re.compile(rf"({_MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", re.IGNORECASE)

# Marker text served by the Malheur site's JavaScript gate.
_JS_WALL = ("javascript is required", "you are being redirected")


def fetch(jurisdiction_cfg: dict, lookback_days: int = 7, lookahead_days: int = 14) -> list[dict]:
    scraper = jurisdiction_cfg.get("scraper")
    name = jurisdiction_cfg["name"]
    if scraper == "payette":
        return _scrape_inline(jurisdiction_cfg, lookback_days, lookahead_days)
    if scraper == "malheur":
        return _scrape_malheur(jurisdiction_cfg, lookback_days, lookahead_days)
    raise ValueError(f"No html scraper wired for {name!r} (scraper={scraper!r})")


# ---------------------------------------------------------------------------
# Inline-HTML agendas (Payette): the current agenda is a table on the page.
# ---------------------------------------------------------------------------

def _scrape_inline(cfg: dict, lookback_days: int, lookahead_days: int) -> list[dict]:
    name = cfg["name"]
    now = datetime.now(timezone.utc)
    date_from = now - timedelta(days=lookback_days)
    date_to = now + timedelta(days=lookahead_days)

    items = []
    for src in cfg.get("sources", []):
        body = src["body"]
        url = src["url"]
        try:
            r = _SESSION.get(url, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log.warning(f"{name}/{body}: fetch failed: {e}")
            raise
        if _is_js_gated(r.text):
            raise RuntimeError(
                f"{name}/{body}: site returned a JavaScript gate at {url}; "
                "a plain request cannot read it (needs a headless browser)."
            )
        item = _extract_agenda(r.text, name, body, url, date_from, date_to)
        if item:
            log.info(f"{name}/{body}: agenda {item['meeting_date']}, "
                     f"{len(item['doc_text'])} chars")
            items.append(item)
        else:
            log.info(f"{name}/{body}: no agenda in window at {url}")
        time.sleep(0.4)
    return items


def _extract_agenda(html: str, name: str, body: str, url: str,
                    date_from: datetime, date_to: datetime) -> dict | None:
    """Pure parser: find the agenda table, read its date, return one item.

    Kept free of network I/O so it can be unit-tested against saved HTML.
    """
    soup = BeautifulSoup(html, "lxml")
    table = _largest_dated_table(soup)
    if table is None:
        return None
    text = table.get_text(" ", strip=True)
    meeting_date = _parse_date(text)
    if not meeting_date:
        return None
    mdt = datetime.strptime(meeting_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if not (date_from <= mdt <= date_to):
        return None
    return {
        "jurisdiction": name,
        "body": body,
        "meeting_date": meeting_date,
        "title": f"{body} agenda ({meeting_date})",
        "url": url,
        "doc_text": text,
        "meeting_doc": text,
    }


def _largest_dated_table(soup: BeautifulSoup):
    """Pick the biggest table that contains a Month DD, YYYY date.

    The agenda is the page's main table; nav and sidebars are lists, not tables.
    Choosing by size + a date match avoids depending on CMS-specific classes.
    """
    best = None
    best_len = 0
    for t in soup.find_all("table"):
        txt = t.get_text(" ", strip=True)
        if _DATE_RE.search(txt) and len(txt) > best_len:
            best = t
            best_len = len(txt)
    return best


# ---------------------------------------------------------------------------
# Malheur: JavaScript-gated. Surface it as a visible error, never a silent skip.
# ---------------------------------------------------------------------------

def _scrape_malheur(cfg: dict, lookback_days: int, lookahead_days: int) -> list[dict]:
    name = cfg["name"]
    src = (cfg.get("sources") or [{}])[0]
    url = src.get("url", "https://www.malheurco.org/")
    try:
        r = _SESSION.get(url, timeout=20)
        text = r.text
    except Exception as e:
        raise RuntimeError(f"{name}: fetch failed for {url}: {e}")
    if _is_js_gated(text):
        raise RuntimeError(
            f"{name}: site is JavaScript-gated at {url}; a plain request only "
            "gets the gate page. Reading Malheur agendas needs a headless "
            "browser (Playwright). KNOWN GAP until that is added."
        )
    # If the gate is ever lifted, fall through to the inline parser.
    return _scrape_inline(cfg, lookback_days, lookahead_days)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_js_gated(html: str) -> bool:
    low = html.lower()
    return any(marker in low for marker in _JS_WALL)


def _parse_date(text: str) -> str | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None

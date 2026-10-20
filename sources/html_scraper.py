"""
html_scraper.py — adapter for county sites that are not on Legistar.

Covers the Yogi footprint: Payette County, ID and Malheur County, OR. fetch()
matches the legistar adapter signature and returns the same normalized item
shape, so Stage 1, Stage 2, dedup, and the digest all work unchanged.

Each Payette source declares a per-source parser:
  (default)   inline agenda table — one item per meeting, the agenda table text
              is the doc_text (used by the Board of Commissioners agenda page).
  notices     public-notices page — the agenda table renders empty by design on
              Payette's P&Z pages, so the real signal (hearing notices naming
              the applicant, request, and parcel) lives on the public-notices
              page as body text. This parser scans that main content text so a
              relevant CUP / hearing notice trips the keyword filter even though
              there is no table to read.

Item shape: {jurisdiction, body, meeting_date, title, url, doc_text, meeting_doc}
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
# Inline-HTML sources (Payette). Each source routes by its 'parser' field:
# default = agenda table, notices = scan main content text.
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
        parser = src.get("parser", "agenda")
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

        if parser == "notices":
            item = _extract_notices(r.text, name, body, url, lookahead_days)
            if item:
                log.info(f"{name}/{body}: notices page, "
                         f"{len(item['doc_text'])} chars, next hearing "
                         f"{item['meeting_date']}")
                items.append(item)
            else:
                log.info(f"{name}/{body}: no readable notice text at {url}")
        else:
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


def _extract_notices(html: str, name: str, body: str, url: str,
                     lookahead_days: int) -> dict | None:
    """Parser for a public-notices page.

    The page carries hearing-notice text (applicant, request, parcel) and a
    hearing-date schedule as body text, not an agenda table. We scan the main
    content so the keyword filter sees the real notice language. The item is
    kept deliberately stable so it settles into ONGOING after day one and only
    resurfaces when the notice text itself changes (state.py fingerprints
    doc_text): fixed title, dated to the next upcoming hearing on the schedule.

    Returns None only if no readable content was found at all (a visible gap).
    """
    text = _main_text(html)
    if not text or len(text) < 40:
        return None

    meeting_date = _next_upcoming_date(text, lookahead_days) or _latest_date(text) \
        or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        "jurisdiction": name,
        "body": body,
        "meeting_date": meeting_date,
        # Stable title (no date) so the item's id does not churn day to day;
        # change tracking keys off the doc_text fingerprint instead.
        "title": f"{body} — public hearing notices",
        "url": url,
        "doc_text": text,
        "meeting_doc": text,
    }


def _main_text(html: str) -> str:
    """Best-effort main-content text with site chrome stripped.

    No dependence on CMS-specific classes: drop the structural chrome tags,
    prefer a <main>/<article>/#content container if present, else fall back to
    the whole body. Nav/menu boilerplate that leaks through is harmless to the
    keyword filter — it does not contain hydrogen/seismic/Koloma/applicant
    language — so the cost of a leak is at most a low-scored repeat, never a
    missed notice.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "header",
                     "footer", "form", "aside", "svg"]):
        tag.decompose()
    for el in soup.find_all(attrs={"role": "navigation"}):
        el.decompose()

    container = (
        soup.find("main")
        or soup.find("article")
        or soup.find(id=re.compile(r"(content|main|page)", re.I))
        or soup.find(class_=re.compile(r"(content|main|page-body|article)", re.I))
        or soup.body
        or soup
    )
    text = container.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:12000]


def _next_upcoming_date(text: str, lookahead_days: int) -> str | None:
    """The soonest date in the text that is today or later. Falls back to None.

    Used to stamp the notices item with the next hearing it pertains to. The
    lookahead argument is advisory only — hearings are monthly, so we do not
    hard-filter the notices item out of the window the way an agenda is.
    """
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=max(lookahead_days, 60))
    cands = []
    for m in _DATE_RE.finditer(text):
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}",
                                  "%B %d %Y").date()
        except ValueError:
            continue
        if today <= d <= horizon:
            cands.append(d)
    return min(cands).isoformat() if cands else None


def _latest_date(text: str) -> str | None:
    """The most recent date found anywhere in the text (for a page with no
    upcoming hearing listed)."""
    found = []
    for m in _DATE_RE.finditer(text):
        try:
            found.append(datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date())
        except ValueError:
            continue
    return max(found).isoformat() if found else None


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

"""
html_scraper.py — adapter for county sites that are not on Legistar.

Covers the Yogi footprint: Payette County and Gem County, ID, and Malheur
County, OR. fetch() matches the legistar adapter signature and returns the same
normalized item shape, so Stage 1, Stage 2, dedup, and the digest all work
unchanged.

Three scraper engines, chosen per jurisdiction by the `scraper` key, and each
source routes further by a per-source `parser`/`mode`:

  payette  inline pages. parser 'agenda' reads the agenda table; parser
           'notices' scans a public-notices page's main text (the P&Z agenda
           page renders empty, so the hearing notices — applicant, request,
           parcel — are read off the notices page instead).

  gem      Revize CMS. A listing page links agenda PDFs (text); we resolve the
           <base>-relative hrefs, pick by filename/label date, fetch and extract.

  malheur  WordPress behind a Sucuri firewall. Same PDF-index approach as Gem
           for the County Court agendas (text PDFs), plus a notices text-scan of
           the Planning Commission page. Sucuri blocks non-browser User-Agents,
           so the shared session sends a browser UA; if a challenge still comes
           back it is raised as a visible source error, never a silent skip.

Item shape: {jurisdiction, body, meeting_date, title, url, doc_text, meeting_doc}
"""

import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, unquote

import requests
import urllib3
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# A real browser User-Agent + Accept headers. Government WordPress/Revize sites
# serve these fine, and crucially it clears Sucuri's basic anti-bot challenge on
# the Malheur site, which blocks requests that don't look like a browser.
_SESSION = requests.Session()
_SESSION.verify = False
_SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/129.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "X-CountyWatch": "public-records monitor; cmanders38@gmail.com",
})

_MONTHS = ("January|February|March|April|May|June|July|August|September|"
           "October|November|December")
_DATE_RE = re.compile(rf"({_MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", re.IGNORECASE)

# Markers of a bot-challenge / interstitial standing in for the real page.
_JS_WALL = ("javascript is required", "you are being redirected",
            "access denied - sucuri", "cloudproxy")


def fetch(jurisdiction_cfg: dict, lookback_days: int = 7, lookahead_days: int = 14) -> list[dict]:
    scraper = jurisdiction_cfg.get("scraper")
    name = jurisdiction_cfg["name"]
    if scraper == "payette":
        return _scrape_inline(jurisdiction_cfg, lookback_days, lookahead_days)
    if scraper in ("gem", "malheur"):
        return _scrape_link_index(jurisdiction_cfg, lookback_days, lookahead_days)
    raise ValueError(f"No html scraper wired for {name!r} (scraper={scraper!r})")


# ---------------------------------------------------------------------------
# Shared fetch with a visible challenge error.
# ---------------------------------------------------------------------------

def _get(url: str, name: str, body: str, timeout: int = 25):
    try:
        r = _SESSION.get(url, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"{name}/{body}: fetch failed for {url}: {e}")
    if _is_js_gated(r.text) and len(r.text) < 4000:
        raise RuntimeError(
            f"{name}/{body}: a bot-challenge/interstitial was returned at {url} "
            "instead of the page (firewall blocked the request). KNOWN GAP.")
    return r


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
        r = _get(url, name, body, timeout=20)

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
    """Pure parser: find the agenda table, read its date, return one item."""
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
        "jurisdiction": name, "body": body, "meeting_date": meeting_date,
        "title": f"{body} agenda ({meeting_date})", "url": url,
        "doc_text": text, "meeting_doc": text,
    }


def _extract_notices(html: str, name: str, body: str, url: str,
                     lookahead_days: int) -> dict | None:
    """Parser for a public-notices page: scan the main content so the keyword
    filter sees the real notice language. Kept deliberately stable (fixed title,
    dated to the next upcoming hearing) so it settles into ONGOING and only
    resurfaces when the notice text itself changes (state.py fingerprints it)."""
    text = _main_text(html)
    if not text or len(text) < 40:
        return None
    meeting_date = _next_upcoming_date(text, lookahead_days) or _latest_date(text) \
        or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "jurisdiction": name, "body": body, "meeting_date": meeting_date,
        "title": f"{body} — public hearing notices", "url": url,
        "doc_text": text, "meeting_doc": text,
    }


def _main_text(html: str) -> str:
    """Best-effort main-content text with site chrome stripped. No dependence on
    CMS-specific classes; nav boilerplate that leaks through is harmless to the
    keyword filter (it has no hydrogen/seismic/applicant language)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "header",
                     "footer", "form", "aside", "svg"]):
        tag.decompose()
    for el in soup.find_all(attrs={"role": "navigation"}):
        el.decompose()
    container = (
        soup.find("main") or soup.find("article")
        or soup.find(id=re.compile(r"(content|main|page)", re.I))
        or soup.find(class_=re.compile(r"(content|main|page-body|article)", re.I))
        or soup.body or soup
    )
    return re.sub(r"\s+", " ", container.get_text(" ", strip=True)).strip()[:12000]


def _next_upcoming_date(text: str, lookahead_days: int) -> str | None:
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
    found = []
    for m in _DATE_RE.finditer(text):
        try:
            found.append(datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date())
        except ValueError:
            continue
    return max(found).isoformat() if found else None


def _largest_dated_table(soup: BeautifulSoup):
    best, best_len = None, 0
    for t in soup.find_all("table"):
        txt = t.get_text(" ", strip=True)
        if _DATE_RE.search(txt) and len(txt) > best_len:
            best, best_len = t, len(txt)
    return best


# ---------------------------------------------------------------------------
# PDF-index engine (Gem + Malheur County Court). A listing page links dated PDFs;
# resolve, date, pick within the window, fetch, extract text.
#
# Per-source config:
#   url        the listing page
#   mode       'pdf_index' (default) or 'notices' (scan the page's main text)
#   pdf_match  regex; only PDF hrefs matching it are considered (skips minutes,
#              boilerplate, scanned notices)
#   pick       'window' -> every match whose date is in the lookback..lookahead
#                          window, newest first;  'latest' -> the single newest
#   max_docs   cap on PDFs fetched per run (default 6)
# ---------------------------------------------------------------------------

def _scrape_link_index(cfg: dict, lookback_days: int, lookahead_days: int) -> list[dict]:
    name = cfg["name"]
    back = max(lookback_days, 35)   # agendas post a few days out, stay up ~a month
    now = datetime.now(timezone.utc).date()
    date_from = now - timedelta(days=back)
    date_to = now + timedelta(days=max(lookahead_days, 45))

    items = []
    for src in cfg.get("sources", []):
        body = src["body"]
        url = src["url"]
        mode = src.get("mode", "pdf_index")
        r = _get(url, name, body)

        if mode == "notices":
            item = _extract_notices(r.text, name, body, url, lookahead_days)
            if item:
                log.info(f"{name}/{body}: notices page, "
                         f"{len(item['doc_text'])} chars")
                items.append(item)
            else:
                log.info(f"{name}/{body}: no readable notice text at {url}")
            time.sleep(0.4)
            continue

        pdf_match = re.compile(src.get("pdf_match", r"\.pdf"), re.I)
        pick = src.get("pick", "window")
        max_docs = int(src.get("max_docs", 6))

        soup = BeautifulSoup(r.text, "lxml")
        base = _page_base(soup, r.url)
        links = _select_pdf_links(soup, base, pdf_match)
        if not links:
            raise RuntimeError(
                f"{name}/{body}: no PDF links matching {pdf_match.pattern!r} at "
                f"{url} (page layout may have changed)")

        chosen = _pick_links(links, pick, date_from, date_to, max_docs)
        if not chosen:
            log.info(f"{name}/{body}: {len(links)} PDFs found, none in window")
            continue

        for ln in chosen:
            try:
                pr = _SESSION.get(ln["url"], timeout=30)
                pr.raise_for_status()
                text = _extract_pdf_text(pr.content)
            except Exception as e:
                raise RuntimeError(f"{name}/{body}: could not read PDF "
                                   f"{ln['url'].split('/')[-1]}: {e}")
            if not text or len(text.strip()) < 40:
                log.warning(f"{name}/{body}: PDF yielded no text (likely a scan): "
                            f"{ln['url']}")
                text = (f"[Agenda/notice PDF posted but no extractable text — "
                        f"likely a scanned image. Open directly: {ln['url']}]")
            mdate = ln["date"] or _parse_date(text) or now.strftime("%Y-%m-%d")
            items.append({
                "jurisdiction": name, "body": body, "meeting_date": mdate,
                "title": f"{body} agenda ({mdate})", "url": ln["url"],
                "doc_text": text, "meeting_doc": text,
            })
            log.info(f"{name}/{body}: agenda {mdate}, {len(text)} chars")
            time.sleep(0.4)
    return items


def _page_base(soup: BeautifulSoup, fetched_url: str) -> str:
    """URL that relative hrefs resolve against. Revize sets a <base> to the site
    root; honor it, else fall back to the fetched page's URL."""
    tag = soup.find("base", href=True)
    if tag and tag["href"].strip():
        return tag["href"].strip()
    return fetched_url


def _select_pdf_links(soup: BeautifulSoup, base: str, pdf_match) -> list[dict]:
    """Every PDF anchor whose href matches pdf_match, resolved to an absolute
    URL, de-duplicated. Date comes from the link TEXT first (e.g. Malheur's
    'October 21, 2026'), then the filename (e.g. Gem's 'September 14 2026
    Agenda.pdf') — whichever yields a Month DD, YYYY."""
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        raw = a["href"].strip()
        if ".pdf" not in raw.lower():
            continue
        if not pdf_match.search(raw):
            continue
        absolute = requests.utils.requote_uri(urljoin(base, raw))
        key = absolute.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        # Decode the filename for date parsing: the absolute URL percent-encodes
        # spaces (October%2013%202026), which the Month DD YYYY regex won't match.
        fname = unquote(key.rsplit("/", 1)[-1])
        label = re.sub(r"\(opens in new window\)", "",
                       a.get_text(" ", strip=True), flags=re.I).strip()
        out.append({
            "url": absolute,
            "label": label,
            "date": _parse_date(label) or _parse_date(fname),
        })
    return out


def _pick_links(links: list[dict], pick: str, date_from, date_to,
                max_docs: int) -> list[dict]:
    if pick == "latest":
        dated = [l for l in links if l["date"]]
        return [max(dated, key=lambda l: l["date"])] if dated else links[:1]
    dated = [l for l in links if l["date"]]
    in_window = [l for l in dated
                 if date_from.isoformat() <= l["date"] <= date_to.isoformat()]
    in_window.sort(key=lambda l: l["date"], reverse=True)
    return in_window[:max_docs]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_js_gated(html: str) -> bool:
    low = html.lower()
    return any(marker in low for marker in _JS_WALL)


def _extract_pdf_text(content: bytes) -> str:
    """Text from a PDF's bytes. '' for a scanned/empty PDF (no text layer),
    which the caller surfaces as a visible gap."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((p.extract_text() or "") for p in reader.pages)[:20000]
    except ImportError:
        log.warning("pypdf not installed — PDF text extraction skipped")
        return ""
    except Exception as e:
        log.warning(f"PDF extraction error: {e}")
        return ""


def _parse_date(text: str) -> str | None:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None

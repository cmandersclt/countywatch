"""
Legistar adapter — works for any Granicus Legistar jurisdiction.

Probes the Legistar Web API first. If the API is enabled for the subdomain,
uses it for events and matter texts. Falls back to scraping the calendar HTML
and linked agenda pages if the API is absent or returns a non-200.
"""

import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Windows Python often lacks the right CA bundle; disable verification for this
# personal tool that only reads public government records.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Track subdomains where /matters/{id}/texts returns 405 so we stop hitting it.
_matter_texts_disabled: set[str] = set()

_SESSION = requests.Session()
_SESSION.verify = False
_SESSION.headers.update({
    "User-Agent": "CountyWatch/1.0 (public-records monitor; cmanders38@gmail.com)"
})

_API_BASE = "https://webapi.legistar.com/v1/{subdomain}"
_SITE_BASE = "https://{subdomain}.legistar.com"


def fetch(jurisdiction_cfg: dict, lookback_days: int = 7, lookahead_days: int = 14) -> list[dict]:
    """
    Return normalized agenda items for a Legistar jurisdiction.
    Item shape: {jurisdiction, body, meeting_date, title, url, doc_text}
    """
    subdomain = jurisdiction_cfg["subdomain"]
    name = jurisdiction_cfg["name"]
    bodies = {b.lower() for b in jurisdiction_cfg.get("bodies", [])}

    now = datetime.now(timezone.utc)
    date_from = now - timedelta(days=lookback_days)
    date_to = now + timedelta(days=lookahead_days)

    api_base = _API_BASE.format(subdomain=subdomain)
    site_base = _SITE_BASE.format(subdomain=subdomain)

    if _api_available(api_base):
        log.info(f"{name}: Legistar Web API is available")
        return _via_api(name, subdomain, bodies, date_from, date_to, api_base, site_base)

    log.info(f"{name}: API not available, falling back to HTML scrape")
    return _via_html(name, bodies, date_from, date_to, site_base)


# ---------------------------------------------------------------------------
# Web API path
# ---------------------------------------------------------------------------

def _api_available(api_base: str) -> bool:
    try:
        r = _SESSION.get(f"{api_base}/events", params={"$top": 1}, timeout=10)
        return r.status_code == 200 and isinstance(r.json(), list)
    except Exception:
        return False


def _via_api(name, subdomain, bodies, date_from, date_to, api_base, site_base):
    items = []

    date_filter = (
        f"EventDate ge datetime'{date_from.strftime('%Y-%m-%dT%H:%M:%S')}' and "
        f"EventDate le datetime'{date_to.strftime('%Y-%m-%dT%H:%M:%S')}'"
    )
    events = _get_json(f"{api_base}/events", params={
        "$filter": date_filter,
        "$orderby": "EventDate desc",
        "$top": 500,
    })
    if not events:
        log.warning(f"{name}: no events returned from API")
        return items

    log.info(f"{name}: {len(events)} events in date window")

    for event in events:
        body_name = event.get("EventBodyName", "")
        if bodies and body_name.lower() not in bodies:
            continue

        event_id = event["EventId"]
        meeting_date = _parse_legistar_date(event.get("EventDate", ""))
        agenda_file_url = event.get("EventAgendaFile") or ""
        meeting_url = (
            agenda_file_url
            or f"{site_base}/MeetingDetail.aspx?ID={event_id}&GUID=&Search="
        )

        log.info(f"  Pulling items: {body_name} {meeting_date} (EventId {event_id})")

        event_items = _get_json(
            f"{api_base}/events/{event_id}/EventItems",
            params={"AgendaNote": 1, "MinutesNote": 1, "Attachments": 1},
        )
        if not event_items:
            continue

        # Fetch the full agenda PDF once per event.
        meeting_doc = _fetch_html_agenda_text(meeting_url, site_base)
        if meeting_doc:
            log.info(f"    PDF: {len(meeting_doc):,} chars from agenda document")

        # Map each item title to its own slice of the agenda PDF, bounded by the
        # NEXT item's position. Without the boundary, a slice runs past its own
        # item into the following ones and Claude ends up scoring an item against
        # text that belongs to the next one down the agenda.
        item_titles = [
            (ei.get("EventItemTitle") or ei.get("EventItemMatterName") or "").strip()
            for ei in event_items
        ]
        section_map = _map_item_sections(meeting_doc, item_titles) if meeting_doc else {}

        for ei in event_items:
            title = (
                ei.get("EventItemTitle")
                or ei.get("EventItemMatterName")
                or ""
            ).strip()
            if not title:
                continue

            doc_text = _build_doc_text(ei, api_base)

            # Enrich with this item's own bounded section from the agenda PDF.
            section = section_map.get(title, "")
            if section:
                doc_text = (doc_text + "\n\n" + section).strip() if doc_text else section

            items.append({
                "jurisdiction": name,
                "body": body_name,
                "meeting_date": meeting_date,
                "title": title,
                "url": meeting_url,
                "doc_text": doc_text,
                "meeting_doc": meeting_doc,  # full PDF; used by Stage 1 broad search
            })

        time.sleep(0.4)

    return items


def _locate_title(pdf_text_lower: str, title: str) -> int | None:
    """Return the start index of an item's title in the agenda PDF, or None.

    Tries progressively shorter prefixes of the title so minor trailing
    differences (punctuation, truncation) still anchor. Requires at least 15
    chars to avoid matching on a generic fragment.
    """
    if not pdf_text_lower or not title:
        return None
    for length in (80, 50, 30):
        key = title[:length].strip().lower()
        if len(key) < 15:
            continue
        idx = pdf_text_lower.find(key)
        if idx != -1:
            return idx
    return None


def _map_item_sections(pdf_text: str, titles: list[str], max_window: int = 3000) -> dict:
    """Return {title: section_text}, each slice bounded by the next item.

    Locate every title in the agenda PDF, sort by position, and give each item
    the text from its own anchor to the start of the next located item (capped
    at max_window). This is the fix for the off-by-one attribution: a fixed
    forward window let each item swallow the next item's text.

    A title that cannot be located gets no section (the API-derived doc_text
    still stands). If an intervening title fails to locate, the previous item's
    slice may reach the next one that did locate, still capped at max_window.
    """
    if not pdf_text or not titles:
        return {}

    pdf_lower = pdf_text.lower()

    located = []  # (idx, title)
    for title in titles:
        if not title:
            continue
        idx = _locate_title(pdf_lower, title)
        if idx is not None:
            located.append((idx, title))

    if not located:
        return {}

    located.sort(key=lambda pair: pair[0])

    sections = {}
    for i, (idx, title) in enumerate(located):
        next_idx = located[i + 1][0] if i + 1 < len(located) else len(pdf_text)
        end = min(next_idx, idx + max_window)
        # First occurrence wins if two items share an identical title (usually
        # generic headers that score 0 anyway).
        sections.setdefault(title, pdf_text[idx:end].strip())
    return sections


def _build_doc_text(ei: dict, api_base: str) -> str:
    """Assemble doc_text from agenda note + matter text."""
    parts = []

    note = (ei.get("EventItemAgendaNote") or "").strip()
    if note:
        parts.append(note)

    matter_id = ei.get("EventItemMatterId")
    if matter_id and api_base not in _matter_texts_disabled:
        matter_text = _fetch_matter_text(api_base, matter_id)
        if matter_text:
            parts.append(matter_text)

    return "\n\n".join(parts)


def _fetch_matter_text(api_base: str, matter_id: int) -> str:
    url = f"{api_base}/matters/{matter_id}/texts"
    try:
        r = _SESSION.get(url, timeout=15)
        if r.status_code == 405:
            log.info(f"Matter texts not available for {api_base} (405) — skipping for this run")
            _matter_texts_disabled.add(api_base)
            return ""
        r.raise_for_status()
        texts = r.json()
    except Exception as e:
        log.warning(f"GET {url} → {e}")
        return ""
    if not texts:
        return ""
    texts.sort(key=lambda t: t.get("MatterTextVersion", ""), reverse=True)
    latest = texts[0]
    return (latest.get("MatterTextPlain") or latest.get("MatterText") or "").strip()


# ---------------------------------------------------------------------------
# HTML fallback path
# ---------------------------------------------------------------------------

def _via_html(name, bodies, date_from, date_to, site_base):
    items = []
    calendar_url = f"{site_base}/Calendar.aspx"

    try:
        r = _SESSION.get(calendar_url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.error(f"{name}: could not fetch calendar page: {e}")
        return items

    soup = BeautifulSoup(r.text, "lxml")

    # Legistar calendar tables use class "rgMasterTable" or similar GridView tables.
    # Each data row has meeting info; header rows can be skipped.
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        cell_texts = [c.get_text(" ", strip=True) for c in cells]

        # Try to identify the body name and date from cells
        body_name = cell_texts[0]
        if bodies and body_name.lower() not in bodies:
            continue

        # Look for a date in any cell
        meeting_date = None
        for text in cell_texts:
            meeting_date = _parse_date_str(text)
            if meeting_date:
                break
        if not meeting_date:
            continue

        meeting_dt = datetime.strptime(meeting_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if not (date_from <= meeting_dt <= date_to):
            continue

        # Find agenda link in this row
        agenda_url = _find_agenda_link(row, site_base)
        if not agenda_url:
            continue

        log.info(f"  HTML: {body_name} {meeting_date} — fetching agenda")
        doc_text = _fetch_html_agenda_text(agenda_url, site_base)

        items.append({
            "jurisdiction": name,
            "body": body_name,
            "meeting_date": meeting_date,
            "title": f"{body_name} Agenda",
            "url": agenda_url,
            "doc_text": doc_text,
        })
        time.sleep(0.5)

    return items


def _find_agenda_link(row, site_base: str) -> str:
    for a in row.find_all("a"):
        href = a.get("href", "")
        text = a.get_text(strip=True).lower()
        if "M=A" in href or "agenda" in text or "view" in text.lower():
            if href.startswith("http"):
                return href
            return f"{site_base}/{href.lstrip('/')}"
    return ""


def _fetch_html_agenda_text(url: str, site_base: str) -> str:
    try:
        r = _SESSION.get(url, timeout=20)
        content_type = r.headers.get("Content-Type", "")
        if "pdf" in content_type.lower():
            return _extract_pdf_text(r.content)

        soup = BeautifulSoup(r.text, "lxml")

        # Look for a PDF link on the page
        for a in soup.find_all("a"):
            href = a.get("href", "")
            if href.lower().endswith(".pdf"):
                pdf_url = href if href.startswith("http") else f"{site_base}/{href.lstrip('/')}"
                try:
                    pr = _SESSION.get(pdf_url, timeout=30)
                    return _extract_pdf_text(pr.content)
                except Exception:
                    pass

        return soup.get_text(" ", strip=True)[:15000]

    except Exception as e:
        log.warning(f"Could not fetch agenda at {url}: {e}")
        return ""


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = [p.extract_text() or "" for p in reader.pages]
        return "\n".join(pages)[:20000]
    except ImportError:
        log.warning("pypdf not installed — PDF text extraction skipped")
        return "[PDF agenda — install pypdf to extract text]"
    except Exception as e:
        log.warning(f"PDF extraction error: {e}")
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, params: dict = None):
    try:
        r = _SESSION.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {url} → {e}")
        return None


def _parse_legistar_date(dt_str: str) -> str:
    """Handle both /Date(ms)/ and ISO 8601 formats from Legistar."""
    if not dt_str:
        return ""
    m = re.match(r"/Date\((-?\d+)", dt_str)
    if m:
        ts = int(m.group(1)) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return dt_str[:10]


def _parse_date_str(text: str) -> str | None:
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None

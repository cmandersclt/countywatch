"""
youtube.py — surface NEW meeting videos from a county / PEG-TV YouTube channel.

Reads a channel's public uploads RSS feed (no API key, no video download) and
normalizes each recent video into the standard item shape. Two kinds matter:

  * "meeting videos" — the title names a tracked body (a MAC, the Board of
    Supervisors, a city council). These are worth surfacing on their own merit:
    the value is simply knowing a meeting recording was posted. They carry
    is_meeting_video=True and bypass Koloma keyword scoring downstream.

  * everything else flows through the normal Stage 1 keyword filter like a news
    item, so a video whose title or description mentions hydrogen / geothermal
    still surfaces even when its body is not one we track.

The channel is identified by a handle (e.g. "LakeCountyPegTV"), which the adapter
resolves to the UC... channel id at run time, or by channel_id directly. A feed
that comes back empty or errors is raised, so a bad handle shows up as a source
error in the brief rather than a silent gap.

Config (one entry under youtube_sources):
  name          label used in logs and as the feed's source name
  jurisdiction  grouping label (defaults to "News" so videos render with news)
  project       oaktree | yogi | none  (carried onto each item)
  handle        channel handle, e.g. "LakeCountyPegTV"   (either this ...
  channel_id    UC... channel id                          ... or this)
  bodies        list of title substrings that mark a tracked meeting video,
                e.g. ["Board of Supervisors", "MATH", "Middletown", "Cobb"]
"""

import logging
import re
import socket
import time
from datetime import datetime, timedelta, timezone

import requests
import feedparser

log = logging.getLogger(__name__)

_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
_CHANNEL_ID_RE = re.compile(r'"(?:channelId|externalId)":"(UC[0-9A-Za-z_-]{20,})"')

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "CountyWatch/1.0 (public-records monitor)"})


def fetch(source_cfg: dict, recent_days: int = 14) -> list[dict]:
    """Return normalized items for the channel's recent uploads.

    Raises RuntimeError if the channel cannot be resolved or the feed is empty,
    so the failure is visible in the brief's source-error line.
    """
    name = source_cfg.get("name", "youtube")
    jurisdiction = source_cfg.get("jurisdiction", "News")
    project = source_cfg.get("project", "none")
    bodies = [b for b in source_cfg.get("bodies", []) if b]

    cid = source_cfg.get("channel_id") or _resolve_handle(source_cfg.get("handle"), name)
    if not cid:
        raise RuntimeError(f"{name}: no channel_id and handle could not be resolved")

    socket.setdefaulttimeout(20)
    feed = feedparser.parse(_FEED.format(cid=cid))
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"{name}: feed error for channel {cid}: "
                           f"{getattr(feed, 'bozo_exception', '?')}")
    if not feed.entries:
        raise RuntimeError(f"{name}: channel {cid} returned no videos "
                           "(wrong handle/channel_id?)")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_days)).date()
    items, dropped_old = [], 0
    for e in feed.entries:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        pub = _pub_date(e)
        if pub is None or pub < cutoff:
            dropped_old += 1
            continue
        body, is_meeting = _classify(title, bodies, name)
        items.append({
            "jurisdiction": jurisdiction,
            "body": body,
            "meeting_date": pub.isoformat(),
            "title": title,
            "url": e.get("link", ""),
            "doc_text": _clean(e),
            "meeting_doc": "",
            "is_meeting_video": is_meeting,
            "project": project,
            "source_type": "youtube",
        })
        time.sleep(0.02)

    log.info(f"YouTube [{name}]: {len(feed.entries)} videos, {len(items)} in window "
             f"({dropped_old} too old), "
             f"{sum(1 for i in items if i['is_meeting_video'])} tracked meetings")
    return items


def _resolve_handle(handle: str, name: str) -> str:
    """Fetch the channel page and pull its UC... id out of the HTML.

    Handles are stable and human-readable; resolving at run time means config
    carries the handle, not an id nobody can eyeball for correctness.
    """
    if not handle:
        return ""
    handle = handle.lstrip("@")
    for url in (f"https://www.youtube.com/@{handle}",
                f"https://www.youtube.com/{handle}",
                f"https://www.youtube.com/c/{handle}",
                f"https://www.youtube.com/user/{handle}"):
        try:
            r = _SESSION.get(url, timeout=20)
            if r.status_code != 200:
                continue
            m = _CHANNEL_ID_RE.search(r.text)
            if m:
                log.info(f"YouTube [{name}]: resolved @{handle} -> {m.group(1)}")
                return m.group(1)
        except Exception as e:
            log.warning(f"YouTube [{name}]: handle resolve attempt failed for {url}: {e}")
    return ""


def _classify(title: str, bodies: list[str], name: str) -> tuple[str, bool]:
    """Return (body_label, is_meeting_video). A title naming a tracked body is a
    meeting video; otherwise it is a plain channel video that must earn its place
    through the keyword filter."""
    low = title.lower()
    for b in bodies:
        if b.lower() in low:
            return f"{name}: {b}", True
    return name, False


def _pub_date(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:3], tzinfo=timezone.utc).date()
    except Exception:
        return None


def _clean(entry) -> str:
    txt = ""
    md = entry.get("media_description") or entry.get("summary") or ""
    if md:
        txt = re.sub(r"\s+", " ", md).strip()
    return txt[:2000]

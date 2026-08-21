"""
state.db — SQLite store for dedup + first-seen tracking.
Each agenda item gets a stable id: SHA-256 of jurisdiction|body|meeting_date|title,
truncated to 16 hex chars. Items are recorded on the first run they appear.

The pipeline no longer discards seen items. Instead, every fetched item is
tagged NEW (first seen within the rolling window) or ONGOING (seen before),
so the daily brief shows the full standing picture and nothing drops off just
because it appeared yesterday.
"""

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# On a server, DATA_DIR points at a persistent volume so NEW/ONGOING tracking
# survives deploys. Unset locally, so behaviour is unchanged on the laptop.
_DATA_DIR = os.environ.get("DATA_DIR")
DB_PATH = (Path(_DATA_DIR) if _DATA_DIR else Path(__file__).parent) / "state.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

NEW_WINDOW_DAYS = 14   # a matter reads NEW for this many days from its first sighting
MATTER_MEMORY_DAYS = 120  # how long a matter is remembered so returns stay connected

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_id      TEXT PRIMARY KEY,
    jurisdiction TEXT NOT NULL,
    body         TEXT NOT NULL,
    meeting_date TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT,
    first_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matter_ids (
    ident      TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    label      TEXT
);
CREATE TABLE IF NOT EXISTS matter_state (
    track_key         TEXT PRIMARY KEY,
    last_score        INTEGER,
    last_meeting_date TEXT,
    last_fingerprint  TEXT,
    label             TEXT,
    updated           TEXT
);
"""


def item_id(item: dict) -> str:
    key = "\x00".join([
        item.get("jurisdiction", ""),
        item.get("body", ""),
        item.get("meeting_date", ""),
        item.get("title", ""),
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# Stable identifiers that survive across meetings: permit/case numbers and APNs.
# These let the same underlying matter be recognised on a later agenda even
# though its meeting date and exact title have changed.
_IDENT_RE = re.compile(
    r'\b('
    r'\d{3}-\d{3}-\d{2,3}'                    # APN, e.g. 115-007-03
    r'|[A-Z]{1,4}[-\s]?\d{2}[-\s]?\d{2,5}'    # UP 21-17, PL-26-357, P22-00307
    r')\b'
)

def _extract_idents(item: dict) -> list[str]:
    """Return stable matter identifiers (permit/case/APN numbers) for an item.

    Threading only on real identifiers keeps the 'first flagged' history claim
    trustworthy: a match means two agenda lines genuinely cite the same number,
    not that their wording happened to overlap. Items with no identifier simply
    don't thread, which is the safe default.
    """
    title = item.get("title", "") or ""
    found = set()
    for m in _IDENT_RE.findall(title):
        norm = re.sub(r'[\s-]', '', m).upper()
        if len(norm) >= 5:
            found.add(norm)
    return sorted(found)


@contextmanager
def _db(path: Path = DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _parse_ts(ts: str, fallback: datetime) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _status_for(first_seen: str, now: datetime, cutoff: datetime) -> str:
    fs = _parse_ts(first_seen, now)
    return "NEW" if fs >= cutoff else "ONGOING"


def dedup_by_id(items: list[dict]) -> list[dict]:
    """Collapse items sharing a stable item_id, keeping the first occurrence.

    The same article routinely arrives from several news feeds in a single run.
    Without this, each copy gets its own Stage 2 call and can land on a different
    score, so the brief shows the same story two or three times. This mirrors the
    cross-run dedup state.db already does, applied within a single run.
    """
    seen = set()
    out = []
    for it in items:
        iid = item_id(it)
        if iid in seen:
            continue
        seen.add(iid)
        out.append(it)
    return out


def record_and_tag(items: list[dict], path: Path = DB_PATH,
                   new_window_days: int = NEW_WINDOW_DAYS) -> list[dict]:
    """
    Persist any not-yet-seen items (first_seen = now) and return ALL items,
    each tagged with 'status' (NEW or ONGOING) and 'first_seen'. Nothing is
    culled — the full fetched set flows downstream.
    """
    if not items:
        return []

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    cutoff = now - timedelta(days=new_window_days)
    ids = [item_id(it) for it in items]

    with _db(path) as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT item_id, first_seen FROM seen_items WHERE item_id IN ({placeholders})",
            ids,
        ).fetchall()
        seen = {r["item_id"]: r["first_seen"] for r in rows}

        to_insert = [
            (
                iid,
                it.get("jurisdiction", ""),
                it.get("body", ""),
                it.get("meeting_date", ""),
                it.get("title", ""),
                it.get("url", ""),
                now_iso,
            )
            for it, iid in zip(items, ids)
            if iid not in seen
        ]
        if to_insert:
            conn.executemany(
                "INSERT OR IGNORE INTO seen_items "
                "(item_id, jurisdiction, body, meeting_date, title, url, first_seen) "
                "VALUES (?,?,?,?,?,?,?)",
                to_insert,
            )

    out = []
    for it, iid in zip(items, ids):
        first_seen = seen.get(iid, now_iso)
        out.append({**it, "first_seen": first_seen})

    # --- matter threads: connect each item to earlier sightings of the same
    # matter (by permit/case/APN number), then set status and first-flagged
    # context off the MATTER rather than the exact agenda line.
    with _db(path) as conn:
        for it in out:
            idents = _extract_idents(it)
            matter_first = None
            if idents:
                q = ",".join("?" * len(idents))
                mrows = conn.execute(
                    f"SELECT first_seen FROM matter_ids WHERE ident IN ({q})", idents
                ).fetchall()
                if mrows:
                    matter_first = min(r["first_seen"] for r in mrows)
                for ident in idents:
                    conn.execute(
                        "INSERT INTO matter_ids (ident, first_seen, last_seen, label) "
                        "VALUES (?,?,?,?) "
                        "ON CONFLICT(ident) DO UPDATE SET last_seen=excluded.last_seen",
                        (ident, matter_first or now_iso, now_iso, (it.get("title") or "")[:120]),
                    )
            it["matter_first_seen"] = matter_first  # None the first time a matter is seen
            basis = matter_first or it["first_seen"]
            it["status"] = _status_for(basis, now, cutoff)
    return out


def tag_status(items: list[dict], path: Path = DB_PATH,
               new_window_days: int = NEW_WINDOW_DAYS) -> list[dict]:
    """
    Pure read: re-attach NEW/ONGOING status to an existing list by looking up
    first_seen in the DB. Safe to call after scoring in case status was dropped.
    """
    if not items:
        return items

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=new_window_days)
    ids = [item_id(it) for it in items]

    with _db(path) as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT item_id, first_seen FROM seen_items WHERE item_id IN ({placeholders})",
            ids,
        ).fetchall()
    fs_map = {r["item_id"]: r["first_seen"] for r in rows}

    out = []
    for it, iid in zip(items, ids):
        first_seen = fs_map.get(iid, now.isoformat())
        # Prefer the matter's first sighting (set upstream) so a returning
        # matter keeps its ONGOING status and does not read as brand new here.
        basis = it.get("matter_first_seen") or first_seen
        out.append({**it, "status": _status_for(basis, now, cutoff),
                    "first_seen": first_seen,
                    "matter_first_seen": it.get("matter_first_seen")})
    return out


def _fingerprint(item: dict) -> str:
    """A stable hash of an item's substance, to detect materials changing
    while it sits on the same meeting."""
    txt = (item.get("doc_text") or item.get("title") or "")
    txt = re.sub(r"\s+", " ", txt).strip().lower()
    return hashlib.sha256(txt.encode()).hexdigest()[:16]


def _track_keys(item: dict) -> list[str]:
    """Keys under which a matter's change-state is stored. Prefer permit/case/
    APN identifiers so the matter tracks across meetings; fall back to the exact
    item id so even an un-numbered agenda line can be recognised as a repeat."""
    idents = _extract_idents(item)
    if idents:
        return ["id:" + i for i in idents]
    return ["iid:" + item_id(item)]


def annotate_changes(items: list[dict], path: Path = DB_PATH) -> list[dict]:
    """Mark each scored item with what changed since its matter was last seen.

    Sets on each item:
      is_repeat      True if this matter has been recorded before
      prev_score     the highest score previously seen for it (or None)
      change_reasons a list of plain reasons it moved (empty if nothing did)

    The digest uses these to keep unchanged low-score repeats in a quiet tail
    while always resurfacing anything that genuinely moved. Runs AFTER scoring.
    """
    if not items:
        return items
    now_iso = datetime.now(timezone.utc).isoformat()
    with _db(path) as conn:
        for it in items:
            keys = _track_keys(it)
            q = ",".join("?" * len(keys))
            rows = conn.execute(
                f"SELECT last_score, last_meeting_date, last_fingerprint "
                f"FROM matter_state WHERE track_key IN ({q})", keys
            ).fetchall()

            score = int(it.get("score", 0) or 0)
            mdate = it.get("meeting_date", "") or ""
            fp = _fingerprint(it)

            prev_scores = [r["last_score"] for r in rows if r["last_score"] is not None]
            prev_dates = {r["last_meeting_date"] for r in rows if r["last_meeting_date"]}
            prev_fps = {r["last_fingerprint"] for r in rows if r["last_fingerprint"]}
            is_repeat = bool(rows)
            prev_score = max(prev_scores) if prev_scores else None

            reasons = []
            if is_repeat:
                if prev_score is not None and score > prev_score:
                    reasons.append(f"score rose from {prev_score} to {score}")
                elif prev_score is not None and score < prev_score:
                    reasons.append(f"score dropped from {prev_score} to {score} (kept visible)")
                if mdate and mdate not in prev_dates:
                    reasons.append(f"now on a new agenda date ({mdate})")
                elif mdate and mdate in prev_dates and fp not in prev_fps:
                    reasons.append("agenda text or materials changed")

            it["is_repeat"] = is_repeat
            it["prev_score"] = prev_score
            it["change_reasons"] = reasons

            for k in keys:
                conn.execute(
                    "INSERT INTO matter_state "
                    "(track_key, last_score, last_meeting_date, last_fingerprint, label, updated) "
                    "VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(track_key) DO UPDATE SET "
                    "last_score=excluded.last_score, "
                    "last_meeting_date=excluded.last_meeting_date, "
                    "last_fingerprint=excluded.last_fingerprint, "
                    "updated=excluded.updated",
                    (k, score, mdate, fp, (it.get("title") or "")[:120], now_iso),
                )
    return items


# --- Legacy helpers kept for compatibility (no longer used by the pipeline) ---

def filter_new(items: list[dict], path: Path = DB_PATH) -> list[dict]:
    """Return only items not already in the database. (Legacy delta behavior.)"""
    if not items:
        return []
    ids = [item_id(it) for it in items]
    with _db(path) as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT item_id FROM seen_items WHERE item_id IN ({placeholders})", ids
        ).fetchall()
    seen = {r["item_id"] for r in rows}
    return [it for it, iid in zip(items, ids) if iid not in seen]


def store(items: list[dict], path: Path = DB_PATH) -> int:
    """Persist items to the database. Returns count inserted. (Legacy.)"""
    if not items:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            item_id(it),
            it.get("jurisdiction", ""),
            it.get("body", ""),
            it.get("meeting_date", ""),
            it.get("title", ""),
            it.get("url", ""),
            now,
        )
        for it in items
    ]
    with _db(path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO seen_items "
            "(item_id, jurisdiction, body, meeting_date, title, url, first_seen) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        after = conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0]
        return after - before

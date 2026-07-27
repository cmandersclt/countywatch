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
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# On a server, DATA_DIR points at a persistent volume so NEW/ONGOING tracking
# survives deploys. Unset locally, so behaviour is unchanged on the laptop.
_DATA_DIR = os.environ.get("DATA_DIR")
DB_PATH = (Path(_DATA_DIR) if _DATA_DIR else Path(__file__).parent) / "state.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

NEW_WINDOW_DAYS = 7  # an item stays tagged NEW for this many days from first_seen

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
"""


def item_id(item: dict) -> str:
    key = "\x00".join([
        item.get("jurisdiction", ""),
        item.get("body", ""),
        item.get("meeting_date", ""),
        item.get("title", ""),
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


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
        out.append({**it, "status": _status_for(first_seen, now, cutoff),
                    "first_seen": first_seen})
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
        out.append({**it, "status": _status_for(first_seen, now, cutoff),
                    "first_seen": first_seen})
    return out


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

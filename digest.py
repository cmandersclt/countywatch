"""
CountyWatch — digest.py
Writes the daily standing brief to a dated markdown file in digests/.

Layout:
  Summary line  (counts: relevant / new this week / source errors)
  Health line   (sources checked, errors, generated timestamp)
  1 · Government      (agenda items, decisions, testimony; grouped by project)
  2 · News & Media    (Koloma / hydrogen / project-area coverage)
  3 · Misc            (anything not a county or a news feed; hidden when empty)

Every item carries a status tag: [NEW] first seen within the rolling window,
[ONGOING] seen before and still live. Pinned matters (parcels / named_matters)
and higher scores sort to the top of each section.
"""

import os
from datetime import datetime, timezone
from pathlib import Path


_PROJECT_ORDER = ["oaktree", "yogi", "none"]
_PROJECT_LABEL = {
    "oaktree": "Oaktree — Lake, Mendocino, Napa, Sonoma",
    "yogi":    "Yogi — Malheur (OR), Payette (ID)",
    "none":    "Other",
}
_PIN_GROUPS = {"parcels", "named_matters"}


def write_digest(items: list[dict], stats: dict = None,
                 output_dir: str = "./digests") -> str:
    """Write a dated markdown digest. Returns the path of the written file."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(output_dir, f"digest-{today}.md")

    with open(path, "w", encoding="utf-8") as f:
        f.write(_render(items, stats or {}, today))

    return path


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def _status_tag(it: dict) -> str:
    s = it.get("status")
    if s == "NEW":
        return "[NEW] "
    if s == "ONGOING":
        return "[ONGOING] "
    return ""


def _sort_key(it: dict):
    pinned = bool(_PIN_GROUPS & set(it.get("matched_groups", [])))
    is_new = it.get("status") == "NEW"
    score = it.get("score", 0)
    # pinned first, then NEW, then higher score
    return (not pinned, not is_new, -score)


def _render(items: list[dict], stats: dict, today: str) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = len(items)
    new_count = sum(1 for it in items if it.get("status") == "NEW")

    errors = stats.get("errors", [])
    err_n = len(errors)
    gov = stats.get("gov_checked", "?")
    news = stats.get("news_checked", "?")

    lines = [
        f"# CountyWatch Brief — {today}",
        "",
        f"**{total} relevant item{_plural(total)} · "
        f"{new_count} new this week · "
        f"{err_n} source error{_plural(err_n)}**",
        "",
        f"_Health: {gov} government source{_plural(gov) if isinstance(gov, int) else ''} "
        f"+ {news} news feed{_plural(news) if isinstance(news, int) else ''} checked. "
        f"{err_n} error{_plural(err_n)}. Generated {now_str}._",
        "",
    ]

    if errors:
        lines += ["> **Source errors this run (results may be incomplete):**"]
        for name, msg in errors:
            short = msg if len(msg) <= 140 else msg[:140] + "…"
            lines += [f"> - **{name}**: {short}"]
        lines += [""]

    lines += ["---", ""]

    gov_items = [it for it in items if it.get("jurisdiction") != "News"]
    news_items = [it for it in items if it.get("jurisdiction") == "News"]

    lines += _government_section(gov_items)
    lines += _news_section(news_items)
    lines += _misc_section([])  # reserved; hidden while empty

    if total == 0:
        lines += [
            "_No relevant items in the current window today. "
            "Sources were checked and are healthy (see Health above)._",
            "",
        ]

    lines += _coverage_section(stats.get("coverage", []))

    return "\n".join(lines)


def _coverage_section(rows: list[dict]) -> list[str]:
    """Every monitored government source and what it returned this run."""
    if not rows:
        return []
    lines = ["## Coverage", "",
             "_Every monitored government source and what it returned this run. "
             "This is the receipt that each county was checked._", ""]
    for r in rows:
        jur = r.get("jur", "")
        body = r.get("body") or ""
        status = r.get("status")
        if status == "error":
            detail = r.get("detail", "")
            short = detail if len(detail) <= 120 else detail[:120] + "…"
            lines.append(f"- **{jur}** — not reached: {short}")
        elif status == "none":
            lines.append(f"- {jur} — {body}: no meeting in window")
        else:  # read
            fl = r.get("flagged", 0)
            flag_txt = f"{fl} flagged" if fl else "nothing flagged"
            date = r.get("date", "")
            date_txt = f" ({date})" if date else ""
            lines.append(f"- {jur} — {body}: agenda read{date_txt}, {flag_txt}")
    lines += ["", "---", ""]
    return lines


def _government_section(items: list[dict]) -> list[str]:
    lines = ["## 1 · Government", ""]
    if not items:
        lines += ["_No government items in the current window._", "", "---", ""]
        return lines

    for project in _PROJECT_ORDER:
        group = [it for it in items if (it.get("project") or "none") == project]
        if not group:
            continue
        lines += [f"### {_PROJECT_LABEL[project]}", ""]
        for it in sorted(group, key=_sort_key):
            lines += _gov_item(it)

    lines += ["---", ""]
    return lines


def _context_line(it: dict) -> list:
    """A short 'seen before' note when this matter has prior history.

    Returns [] when the matter is genuinely new (first flagged this run) so a
    fresh item stays clean, and a one-line continuation note otherwise.
    """
    mfs = it.get("matter_first_seen")
    if not mfs:
        return []
    day = str(mfs)[:10]
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if day >= today:
        return []
    return [f"_First flagged {day}; this matter has appeared before and is still active._", ""]


def _gov_item(it: dict) -> list[str]:
    tag    = _status_tag(it)
    score  = it.get("score", "?")
    jur    = it.get("jurisdiction", "")
    body   = it.get("body", "")
    date   = it.get("meeting_date", "")
    explanation = (it.get("explanation") or it.get("why") or "").strip()
    headline = (it.get("headline") or "").strip() or (it.get("title") or "").strip()
    url    = it.get("url", "")
    pin    = "★ " if _PIN_GROUPS & set(it.get("matched_groups", [])) else ""

    label = f"{jur} / {body}" if body and body != jur else jur

    # Headline first, then a small context line, then the paragraph, then the
    # link. The reader scans top to bottom and understands each item without
    # having to open anything.
    hl = headline if len(headline) <= 160 else headline[:160] + "…"
    block = [f"**{pin}{tag}{hl}**", ""]
    block += [f"_{label} · {date} · score {score}_", ""]
    block += _context_line(it)
    if explanation:
        block += [explanation, ""]
    if url:
        block += [f"[Open agenda]({url})", ""]
    return block


def _news_section(items: list[dict]) -> list[str]:
    lines = ["## 2 · News & Media", ""]
    if not items:
        lines += ["_No news items in the current window._", "", "---", ""]
        return lines

    for it in sorted(items, key=_sort_key):
        tag    = _status_tag(it)
        score  = it.get("score", "?")
        explanation = (it.get("explanation") or it.get("why") or "").strip()
        headline = (it.get("headline") or "").strip() or (it.get("title") or "").strip()
        url    = it.get("url", "")
        date   = it.get("meeting_date", "")
        source = it.get("body", "")

        hl = headline if len(headline) <= 160 else headline[:160] + "…"
        meta_bits = [b for b in (source if source and source != "News" else "", date) if b]
        meta = " · ".join(meta_bits + [f"score {score}"])

        lines += [f"**{tag}{hl}**", ""]
        lines += [f"_{meta}_", ""]
        lines += _context_line(it)
        if explanation:
            lines += [explanation, ""]
        if url:
            lines += [f"[Read article]({url})", ""]

    lines += ["---", ""]
    return lines


def _misc_section(items: list[dict]) -> list[str]:
    if not items:
        return []  # hidden entirely when empty
    lines = ["## 3 · Misc", ""]
    for it in sorted(items, key=_sort_key):
        tag   = _status_tag(it)
        score = it.get("score", "?")
        explanation = (it.get("explanation") or it.get("why") or "").strip()
        headline = (it.get("headline") or "").strip() or (it.get("title") or "").strip()
        url   = it.get("url", "")
        hl = headline if len(headline) <= 160 else headline[:160] + "…"
        lines += [f"**{tag}{hl}**  ·  score {score}", ""]
        if explanation:
            lines += [explanation, ""]
        if url:
            lines += [f"[Open link]({url})", ""]
    lines += ["---", ""]
    return lines


def print_items(items: list[dict], preview_chars: int = 400):
    """Console echo, unchanged in spirit — quick visibility when run by hand."""
    sep = "=" * 70
    new_count = sum(1 for it in items if it.get("status") == "NEW")
    print(f"\n{sep}")
    print(f"  {len(items)} relevant item(s), {new_count} new")
    print(f"{sep}\n")
    for i, item in enumerate(items, 1):
        tag = item.get("status", "")
        score = item.get("score", "?")
        headline = (item.get("headline") or "").strip() or (item.get("title") or "").strip()
        explanation = (item.get("explanation") or item.get("why") or "").strip()
        print(f"[{i:>3}] {tag:<9} {item.get('jurisdiction','')} / {item.get('body','')}  (score {score})")
        print(f"      {headline}")
        if explanation:
            print(f"      {explanation[:preview_chars]}")
        print()
      

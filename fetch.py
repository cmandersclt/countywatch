"""
CountyWatch — fetch.py
Reads config.yaml, runs the appropriate adapter for each enabled jurisdiction,
records first-seen and tags each item NEW/ONGOING (nothing is culled), applies
the Stage 1 keyword prefilter, scores survivors via Claude, and returns the
full standing set plus run-health stats for the daily brief.
"""

import logging
import os
import re
import sys

import yaml
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _load_system_prompt(path: str = "prompts.md") -> str:
    """Extract the first fenced code block from prompts.md."""
    with open(path) as f:
        content = f.read()
    m = re.search(r"```\n(.*?)```", content, re.DOTALL)
    if not m:
        raise ValueError(f"No system prompt found in {path}")
    return m.group(1).strip()


def run(config_path: str = "config.yaml", only: str = None):
    """
    Returns (scored_items, stats). stats carries run-health info for the brief:
    gov_checked, news_checked, errors (list of (source, message)), and counts.
    """
    cfg = load_config(config_path)
    windows = cfg.get("windows", {})
    lookback = windows.get("recent_days", 7)
    lookahead = windows.get("upcoming_days", 14)
    news_recent = windows.get("news_recent_days", 14)
    relevance_cfg = cfg.get("relevance", {})

    from sources.legistar import fetch as legistar_fetch
    from sources.news import fetch as news_fetch
    import state
    import relevance

    all_fetched = []
    errors = []          # list of (source_name, message)
    gov_checked = 0
    news_checked = 0

    for jur in cfg["jurisdictions"]:
        name = jur["name"]

        if only and name != only:
            continue

        if not jur.get("enabled", True):
            log.info(f"Skipping {name} (disabled)")
            continue

        platform = jur.get("platform")
        if platform == "legistar":
            gov_checked += 1
            log.info(f"--- {name} ---")
            try:
                items = legistar_fetch(jur, lookback_days=lookback, lookahead_days=lookahead)
                log.info(f"    {len(items)} fetched")
                all_fetched.extend(items)
            except Exception as e:
                log.error(f"    FAILED: {e}", exc_info=True)
                errors.append((name, str(e)))
        elif platform == "html":
            gov_checked += 1
            log.info(f"--- {name} (html) ---")
            try:
                from sources.html_scraper import fetch as html_fetch
                items = html_fetch(jur, lookback_days=lookback, lookahead_days=lookahead)
                log.info(f"    {len(items)} fetched")
                all_fetched.extend(items)
            except Exception as e:
                # A blocked or broken county surfaces as a source error in the
                # brief's health line, not a silent skip. This is the whole
                # point: a named gap you can see beats a hole you can't.
                log.error(f"    FAILED: {e}")
                errors.append((name, str(e)))
        else:
            log.info(f"Skipping {name} (platform={platform!r}, adapter not built yet)")

    for ns in cfg.get("news_sources", []):
        if only:
            continue  # news runs only in full-pipeline mode
        news_checked += 1
        log.info(f"--- News: {ns['name']} ---")
        try:
            items = news_fetch(ns, recent_days=news_recent)
            log.info(f"    {len(items)} fetched")
            all_fetched.extend(items)
        except Exception as e:
            log.error(f"    FAILED: {e}", exc_info=True)
            errors.append((ns["name"], str(e)))

    # Collapse duplicates within this run before anything downstream. The same
    # article often arrives from several news feeds; without this each copy gets
    # its own Claude call and can land on a different score, so the brief repeats
    # itself. First occurrence wins.
    before_dedup = len(all_fetched)
    all_fetched = state.dedup_by_id(all_fetched)
    collapsed = before_dedup - len(all_fetched)
    if collapsed:
        log.info(f"Dedup: {collapsed} duplicate item(s) collapsed within this run")

    # Record first-seen + tag NEW/ONGOING. Nothing is culled: the full standing
    # set flows downstream so ongoing matters stay visible until they age out.
    annotated = state.record_and_tag(all_fetched)
    new_count = sum(1 for it in annotated if it.get("status") == "NEW")
    log.info(
        f"Status: {len(annotated)} item(s), "
        f"{new_count} new (<= rolling window), "
        f"{len(annotated) - new_count} ongoing"
    )

    # Stage 1 prefilter over the full standing set
    candidates = relevance.prefilter(annotated, relevance_cfg)
    log.info(
        f"Prefilter: {len(annotated)} checked, {len(candidates)} passed, "
        f"{len(annotated) - len(candidates)} dropped"
    )

    # Stage 2 Claude scoring (score-fresh daily)
    system_prompt = _load_system_prompt()
    scored = relevance.score_items(candidates, system_prompt)
    # Re-attach status after scoring in case the scorer rebuilt the dicts
    scored = state.tag_status(scored)
    scored = state.annotate_changes(scored)
    scored.sort(key=lambda it: it.get("score", 0), reverse=True)
    log.info(
        f"Scoring: {len(candidates)} sent to Claude, "
        f"{len(scored)} survived (score >= 1)"
    )

    stats = {
        "gov_checked": gov_checked,
        "news_checked": news_checked,
        "errors": errors,
        "fetched": len(all_fetched),
        "new_count": sum(1 for it in scored if it.get("status") == "NEW"),
        "scored": len(scored),
        "coverage": _build_coverage(cfg, all_fetched, scored, errors),
    }
    return scored, stats


def _build_coverage(cfg: dict, all_fetched: list, kept: list, errors: list) -> list[dict]:
    """Per-source roll call: what each government source returned this run.

    Turns silent absence into an explicit line. A source that was read and had
    nothing looks different from one with no meeting, which looks different from
    one that could not be reached. This is the receipt that the monitoring ran.
    """
    from collections import defaultdict

    err_map = {}
    for name, msg in errors:
        err_map.setdefault(name, msg)

    fetched_by_jur = defaultdict(list)
    for it in all_fetched:
        fetched_by_jur[it.get("jurisdiction")].append(it)

    def _key(it):
        return (it.get("jurisdiction"), it.get("body"),
                it.get("meeting_date"), it.get("title"))

    kept_keys = defaultdict(int)
    for it in kept:
        kept_keys[_key(it)] += 1

    def _matches(cfg_body, item_body):
        a = (cfg_body or "").lower()
        b = (item_body or "").lower()
        return bool(a) and bool(b) and (a in b or b in a)

    rows = []
    for jur in cfg.get("jurisdictions", []):
        name = jur.get("name")
        if jur.get("platform") not in ("legistar", "html") or not jur.get("enabled", True):
            continue

        # A whole-jurisdiction failure covers all its bodies.
        if name in err_map:
            rows.append({"jur": name, "body": None, "status": "error",
                         "detail": err_map[name]})
            continue

        jitems = fetched_by_jur.get(name, [])
        consumed = [False] * len(jitems)

        for src in jur.get("sources", []):
            cbody = src.get("body", "")
            matched = [(i, it) for i, it in enumerate(jitems)
                       if _matches(cbody, it.get("body"))]
            if not matched:
                rows.append({"jur": name, "body": cbody, "status": "none"})
                continue
            bydate = defaultdict(lambda: [0, 0])
            for i, it in matched:
                consumed[i] = True
                d = it.get("meeting_date", "")
                bydate[d][0] += 1
                bydate[d][1] += kept_keys.get(_key(it), 0)
            for d, (_f, fl) in sorted(bydate.items()):
                rows.append({"jur": name, "body": cbody, "status": "read",
                             "date": d, "flagged": fl})

        # Anything read under a body name not in config still gets shown, so
        # nothing that was actually read is ever hidden from the roll call.
        extra = defaultdict(lambda: [0, ""])
        for i, it in enumerate(jitems):
            if consumed[i]:
                continue
            b = it.get("body", "(unknown body)")
            extra[b][0] += kept_keys.get(_key(it), 0)
            extra[b][1] = it.get("meeting_date", "")
        for b, (fl, d) in extra.items():
            rows.append({"jur": name, "body": b, "status": "read",
                         "date": d, "flagged": fl})

    return rows


if __name__ == "__main__":
    import digest
    import mailer
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    cfg = load_config()
    email_cfg = cfg.get("delivery", {}).get("email", {})
    _data_dir = os.environ.get("DATA_DIR")
    if _data_dir:
        output_dir = os.path.join(_data_dir, "digests")
    else:
        output_dir = cfg.get("delivery", {}).get("output_dir", "./digests")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = Path(output_dir)
    digest_path = out / f"digest-{today}.md"
    meta_path = out / f"digest-{today}.meta.json"
    sent_path = out / f"digest-{today}.sent"

    to = email_cfg.get("to") or os.environ.get("GMAIL_ADDRESS")
    prefix = email_cfg.get("subject_prefix", "[CountyWatch]")

    def deliver(subject, body):
        """Send, then mark delivered.

        The marker is written only after the send returns, so a failed send
        leaves no marker and the next run retries. Writing the brief to disk is
        not evidence that anyone received it.
        """
        mailer.send_digest(subject, body, to=to)
        sent_path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        log.info(f"Email sent to {to}: {subject}")

    # Already delivered today.
    if sent_path.exists():
        log.info(f"Brief for {today} already sent. Skipping run.")
        raise SystemExit(0)

    if digest_path.exists():
        # No meta file means this brief predates delivery tracking. Assume it went
        # out and mark it, rather than re-sending old briefs on first upgrade.
        if not meta_path.exists():
            log.info(f"Brief for {today} predates delivery tracking. Marking as sent.")
            sent_path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
            raise SystemExit(0)

        # Written but never delivered: a previous send failed. Re-send the brief we
        # already have instead of re-scraping. state.db has already marked this
        # run's items as seen, so rebuilding would downgrade every NEW to ONGOING
        # and quietly hide the very thing worth reading.
        log.warning(f"Brief for {today} was written but never sent. Re-sending.")
        subject = f"{prefix} Brief {today}"
        try:
            subject = json.loads(meta_path.read_text(encoding="utf-8"))["subject"]
        except Exception as e:
            log.warning(f"Could not read {meta_path.name} ({e}). Using a plain subject.")
        try:
            deliver(subject, digest_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Re-send failed: {e}. The next run will try again.")
            raise SystemExit(1)
        raise SystemExit(0)

    items, stats = run()
    path = digest.write_digest(items, stats=stats, output_dir=output_dir)
    log.info(f"Brief written: {path}")

    # Always send. On a quiet day the health line is the point: it confirms the
    # run happened and the sources are clean, so silence can never mask a failure.
    total = len(items)
    new_count = sum(1 for it in items if it.get("status") == "NEW")
    err_n = len(stats.get("errors", []))
    subject = f"{prefix} Brief {today} | {new_count} new, {total} relevant"
    if err_n:
        subject += " [SOURCE ERRORS]"

    # Record the subject beside the brief so a retry can reproduce it exactly.
    meta_path.write_text(json.dumps({"subject": subject, "to": to}), encoding="utf-8")

    with open(path, encoding="utf-8") as f:
        body = f.read()

    try:
        deliver(subject, body)
    except Exception as e:
        # Loud, and non-zero so Task Scheduler shows a failed run instead of
        # reporting success on a brief nobody received.
        log.error(f"Send failed: {e}")
        log.error(f"Brief saved at {path}. The next run will retry the send.")
        raise SystemExit(1)

    digest.print_items(items)

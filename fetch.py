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

    # YouTube channels

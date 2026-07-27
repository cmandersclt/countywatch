"""
Stage 1 keyword prefilter + Stage 2 Claude API scoring.

Stage 1: compiles keyword_groups from config.yaml into regexes, scans
item title + doc_text, keeps items with at least one hit.

Stage 2: calls Claude to score each Stage-1 survivor 0-3; drops score-0 items.
"""

import json
import logging
import re

log = logging.getLogger(__name__)

_DOC_TEXT_LIMIT = 2000  # chars sent to the API per item


def prefilter(items: list[dict], relevance_cfg: dict) -> list[dict]:
    """
    Return only items that match at least one keyword or parcel.
    Each passing item gains: matched_groups (list[str]).
    """
    patterns = _compile(relevance_cfg)
    if not patterns:
        # No rules configured — pass everything through unlabelled.
        return [{**it, "matched_groups": []} for it in items]

    results = []
    for item in items:
        # Search only the item's own title and bounded section. The full agenda
        # packet (meeting_doc) is identical for every item in a meeting, so
        # including it let one stray keyword flood the entire meeting into
        # Stage 2. Fix #1 gives each item its own text, so scope to that.
        haystack = f"{item.get('title', '')} {item.get('doc_text', '')}".lower()
        matched = [name for name, rx in patterns.items() if rx.search(haystack)]
        if matched:
            results.append({**item, "matched_groups": matched})

    return results


def score_items(items: list[dict], system_prompt: str) -> list[dict]:
    """
    Call the Claude API for each Stage-1 survivor.
    Returns items with score >= 1, each gaining: score, why, project.
    Score-0 items are dropped.
    """
    if not items:
        return []

    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    results = []
    api_errors = 0
    parse_failures = 0
    for item in items:
        user_msg = _build_user_message(item)
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text
            log.debug(f"Claude raw for {item['title']!r}: {raw!r}")
            scored = _parse_response(raw)
        except Exception as e:
            # An API failure means the item was never actually judged. Score it 0
            # so it drops out of the brief instead of surfacing as fake score-1
            # noise. It is not lost: the next daily run re-scores from scratch.
            log.warning(f"Stage 2 API error, scoring 0: {item['title'][:80]!r}: {e}")
            api_errors += 1
            continue

        # A response that could not be parsed is also not a real score. Drop it
        # the same way rather than letting it masquerade as a 1.
        if scored.pop("_failed", False):
            log.warning(f"Stage 2 unparsable response, scoring 0: {item['title'][:80]!r}")
            parse_failures += 1
            continue

        if scored["score"] == 0:
            log.info(f"  Stage 2 dropped (score 0): {item['title']!r}")
            continue

        results.append({**item, **scored})

    if api_errors or parse_failures:
        log.warning(
            f"Stage 2 unscored (counted as 0): {api_errors} API error(s), "
            f"{parse_failures} unparsable. These re-score on the next run."
        )

    return results


def _build_user_message(item: dict) -> str:
    doc = (item.get("doc_text") or "")[:_DOC_TEXT_LIMIT]
    groups = ", ".join(item.get("matched_groups", []))
    return (
        f"County: {item.get('jurisdiction', '')}\n"
        f"Body: {item.get('body', '')}\n"
        f"Meeting date: {item.get('meeting_date', '')}\n"
        f"Item title: {item.get('title', '')}\n"
        f"Item text: {doc}\n"
        f"Keyword groups that matched: {groups}"
    )


def _parse_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON; fall back on any parse failure.

    A parse failure returns score 0 with a "_failed" marker so score_items can
    drop it and count it, rather than letting an unjudged item show up as a 1.
    """
    fallback = {"score": 0, "headline": "", "explanation": "scoring failed, unparsable response",
                "project": "none", "_failed": True}
    text = raw.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if not m:
            log.warning(f"Could not parse JSON: {raw!r}")
            return fallback
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            log.warning(f"Could not parse JSON: {raw!r}")
            return fallback

    return {
        "score": int(parsed.get("score", 0)),
        "headline": str(parsed.get("headline", "")).strip(),
        "explanation": str(parsed.get("explanation", parsed.get("why", ""))).strip()
                       or "no explanation given",
        "project": str(parsed.get("project", "none")).strip() or "none",
    }


def _boundaried(terms: list[str]) -> str:
    r"""Join terms into an alternation, each wrapped in \b word boundaries.

    Without boundaries a short term like "ban" matches inside urban, Albany,
    abandoned, disturbance. \b anchors each term to whole-word matches. All
    configured terms begin and end with word characters (letters or digits),
    so the boundaries behave.
    """
    return "|".join(rf"\b{re.escape(t)}\b" for t in terms)


def _compile(relevance_cfg: dict) -> dict[str, re.Pattern]:
    """Build {group_name: compiled_pattern} from config."""
    patterns = {}

    for group, terms in relevance_cfg.get("keyword_groups", {}).items():
        terms = [t for t in (terms or []) if t]
        if terms:
            rx = re.compile(_boundaried(terms), re.IGNORECASE)
            patterns[group] = rx

    parcels = [str(p) for p in (relevance_cfg.get("parcels") or []) if p]
    if parcels:
        patterns["parcels"] = re.compile(_boundaried(parcels), re.IGNORECASE)

    return patterns

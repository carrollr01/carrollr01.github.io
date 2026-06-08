"""
Stage 4: deterministic dedup, eligibility filtering, significance scoring, and
per-sector selection. No network, no LLM — pure, reproducible, auditable logic.

Eligibility (the hard business rules):
  * announced-only: drop COMPLETED M&A (those were announced earlier).
  * growth-raise floor: a CAPITAL_RAISE needs a DISCLOSED amount strictly above
    config.GROWTH_RAISE_MIN_USD. Undisclosed or smaller rounds are dropped.
  * verified-only: a record must have passed the verifier lens.
"""
from __future__ import annotations
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz

import config
from models import DealRecord, CAPITAL_RAISE, MA_TYPES

_SUFFIXES = r"\b(inc|incorporated|ltd|limited|llc|corp|corporation|plc|group|holdings|co)\b"


def _norm_company(name: str) -> str:
    n = re.sub(r"[^\w\s]", " ", (name or "").lower())
    n = re.sub(_SUFFIXES, " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _same_deal(r: DealRecord, k: DealRecord) -> bool:
    """Are these two records the same transaction?

    Beyond a plain fuzzy match, treat them as one deal when the SAME acquirer/
    investor appears and one target name is contained in the other — this catches
    cases like "T&D Financial Life" vs "T&D Financial Life Insurance" that slip
    just under the fuzzy threshold."""
    if r.deal_type != k.deal_type:
        return False
    a, b = _norm_company(r.target), _norm_company(k.target)
    if not a or not b:
        return False
    ratio = fuzz.token_sort_ratio(a, b)
    if ratio >= config.RECORD_SIM_THRESHOLD * 100:
        return True
    contained = a in b or b in a
    cp_a, cp_b = _norm_company(r.counterparty), _norm_company(k.counterparty)
    same_cp = bool(cp_a) and cp_a == cp_b
    return contained and (same_cp or ratio >= 70)


def dedup_records(records: list[DealRecord]) -> list[DealRecord]:
    """Merge records that are the same deal. Keep the best source + fullest fields."""
    kept: list[DealRecord] = []
    for r in records:
        match = None
        for k in kept:
            if _same_deal(r, k):
                match = k
                break
        if not match:
            kept.append(r)
            continue
        primary, secondary = (r, match) if r.source_tier > match.source_tier else (match, r)
        for f in ("counterparty", "target", "date_announced", "target_country",
                  "ev_text", "amount_text", "valuation_text", "ev_revenue", "ev_ebitda",
                  "target_description", "value_usd_est", "segment", "deal_status"):
            if getattr(primary, f) in (None, "") and getattr(secondary, f) not in (None, ""):
                setattr(primary, f, getattr(secondary, f))
        primary.evidence = {**secondary.evidence, **primary.evidence}
        primary.confidence = max(primary.confidence, secondary.confidence)
        if primary is not match:
            kept[kept.index(match)] = primary
    return kept


def _as_date(s: str | None):
    """Parse a yyyy-mm-dd (or ISO) string to a date, else None."""
    if not s:
        return None
    s = str(s)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def enforce_window(records: list[DealRecord], lookback_days: int | None = None
                   ) -> tuple[list[DealRecord], int]:
    """Keep only deals ANNOUNCED inside the window, and backfill the display date.

    Two jobs, both of which were broken before:
      1. Fill `date_announced` from the article's publish date when the body
         didn't state one (the verifier nulls body-unsupported dates, which is
         why almost every Date cell was blank). The publish date IS a valid
         announcement-date proxy for a fresh story.
      2. Drop stale items: if a STATED announcement date is older than the
         window, the story is a re-report of an old deal (e.g. a deal first
         announced months ago) — exclude it. This is what lets through
         only genuinely new announcements.
    """
    lookback_days = lookback_days or config.LOOKBACK_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date()
    kept, dropped = [], 0
    for r in records:
        stated = _as_date(r.date_announced)
        published = _as_date(r.published_at)
        effective = stated or published
        if effective is not None and effective < cutoff:
            dropped += 1                      # announced before the window -> stale
            continue
        if not r.date_announced and r.published_at:
            r.date_announced = r.published_at[:10]   # backfill the blank Date cell
        kept.append(r)
    return kept, dropped


# Event stages that are NOT a fresh announcement — a deal at one of these stages
# was disclosed earlier, so we drop it (we ingest on announce). "announced" and
# "unknown" are kept; everything on this list is rejected.
NON_ANNOUNCEMENT_STATUSES = {"completed", "regulatory", "rumor"}


def filter_eligible(records: list[DealRecord]) -> tuple[list[DealRecord], dict]:
    """Apply the hard business rules. Returns (eligible, drop_counts)."""
    out, drops = [], Counter()
    for r in records:
        if not r.verified:
            drops["unverified"] += 1
            continue
        status = (r.deal_status or "unknown").lower()
        if status in NON_ANNOUNCEMENT_STATUSES:
            drops[f"stage_{status}"] += 1      # closing / regulatory step / rumor, not an announce
            continue
        if r.deal_type in MA_TYPES:
            out.append(r)
        elif r.deal_type == CAPITAL_RAISE:
            amt = r.amount_usd
            if amt is None:
                drops["raise_amount_undisclosed"] += 1
                continue
            if amt <= config.GROWTH_RAISE_MIN_USD:
                drops["raise_below_floor"] += 1
                continue
            out.append(r)
        else:
            drops["other_type"] += 1
    return out, dict(drops)


def _recency_score(iso_date: str | None) -> float:
    if not iso_date:
        return 0.4
    try:
        d = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except ValueError:
        return 0.4
    age_days = (datetime.now(timezone.utc) - d).days
    return max(0.0, 1.0 - age_days / max(1, config.LOOKBACK_DAYS))


def score(records: list[DealRecord]) -> list[DealRecord]:
    lo, hi = math.log10(config.VALUE_REF_MIN_USD), math.log10(config.VALUE_REF_MAX_USD)
    span = hi - lo
    w = config.RANK_WEIGHTS
    for r in records:
        if r.value_usd_est:
            v = min(1.0, max(0.0, (math.log10(r.value_usd_est) - lo) / span))
        else:
            v = config.UNDISCLOSED_VALUE_BASELINE
        s = w["source"] * (r.source_tier / 3.0)
        rec = w["recency"] * _recency_score(r.published_at or r.date_announced)
        r.significance = round(w["value"] * v + s + rec + 0.05 * r.confidence, 4)
    return records


def select(records: list[DealRecord]) -> tuple[list[DealRecord], list[str]]:
    """Pick deals across sectors WITHOUT inventing any and WITHOUT an upper bound
    per sector — every significant, eligible deal makes the cut.

    Guarantees:
      - at least PER_SEGMENT_MIN per sector that actually has a real deal,
      - NO per-sector ceiling (PER_SEGMENT_MAX = 0 means unlimited),
      - TARGET_TOTAL_MAX is only a high safety ceiling,
      - ordering by significance (so the biggest deals sit at the top).

    Returns (selected, gap_segment_keys) where gaps are sectors with ZERO
    eligible deals. The caller uses gaps to drive more searches (it must not
    fabricate to fill them).
    """
    by_seg: dict[str, list[DealRecord]] = {k: [] for k in config.SEGMENTS}
    for r in records:
        if r.segment in by_seg:
            by_seg[r.segment].append(r)
    for items in by_seg.values():
        items.sort(key=lambda x: x.significance, reverse=True)

    per_seg_cap = config.PER_SEGMENT_MAX if config.PER_SEGMENT_MAX > 0 else float("inf")
    selected: list[DealRecord] = []
    # Pass 1: guarantee one per sector that has anything.
    for seg, items in by_seg.items():
        if items:
            selected.append(items[0])

    # Pass 2: add every remaining eligible deal by significance — no per-sector
    # cap by default — up to the overall safety ceiling.
    pool = sorted((r for items in by_seg.values() for r in items[1:]),
                  key=lambda x: x.significance, reverse=True)
    seg_counts = Counter(r.segment for r in selected)
    for r in pool:
        if len(selected) >= config.TARGET_TOTAL_MAX:
            break
        if seg_counts[r.segment] >= per_seg_cap:
            continue
        selected.append(r)
        seg_counts[r.segment] += 1

    gaps = [seg for seg, items in by_seg.items() if not items]
    selected.sort(key=lambda x: (config.SEGMENTS[x.segment]["label"], x.significance), reverse=False)
    return selected, gaps

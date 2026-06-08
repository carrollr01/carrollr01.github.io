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
from datetime import datetime, timezone

from rapidfuzz import fuzz

import config
from models import DealRecord, CAPITAL_RAISE, MA_TYPES

_SUFFIXES = r"\b(inc|incorporated|ltd|limited|llc|corp|corporation|plc|group|holdings|co)\b"


def _norm_company(name: str) -> str:
    n = re.sub(r"[^\w\s]", " ", (name or "").lower())
    n = re.sub(_SUFFIXES, " ", n)
    return re.sub(r"\s+", " ", n).strip()


def dedup_records(records: list[DealRecord]) -> list[DealRecord]:
    """Merge records that are the same deal. Keep the best source + fullest fields."""
    kept: list[DealRecord] = []
    for r in records:
        rt = _norm_company(r.target)
        match = None
        for k in kept:
            if r.deal_type == k.deal_type and rt and \
               fuzz.token_sort_ratio(rt, _norm_company(k.target)) >= config.RECORD_SIM_THRESHOLD * 100:
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


def filter_eligible(records: list[DealRecord]) -> tuple[list[DealRecord], dict]:
    """Apply the hard business rules. Returns (eligible, drop_counts)."""
    out, drops = [], Counter()
    for r in records:
        if not r.verified:
            drops["unverified"] += 1
            continue
        if r.deal_type in MA_TYPES:
            if r.deal_status == "completed":
                drops["completed_ma"] += 1     # announced earlier — already ingested
                continue
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
    """Pick deals across sectors WITHOUT inventing any.

    Guarantees:
      - at least PER_SEGMENT_MIN per sector that actually has a real deal,
      - at most PER_SEGMENT_MAX per sector,
      - no more than TARGET_TOTAL_MAX overall,
      - filling toward TARGET_TOTAL_MIN by significance.

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

    selected: list[DealRecord] = []
    # Pass 1: guarantee one per sector that has anything.
    for seg, items in by_seg.items():
        if items:
            selected.append(items[0])

    # Pass 2: fill remaining capacity by significance, respecting the per-sector
    # cap and the overall ceiling.
    pool = sorted((r for items in by_seg.values() for r in items[1:]),
                  key=lambda x: x.significance, reverse=True)
    seg_counts = Counter(r.segment for r in selected)
    for r in pool:
        if len(selected) >= config.TARGET_TOTAL_MAX:
            break
        if seg_counts[r.segment] >= config.PER_SEGMENT_MAX:
            continue
        selected.append(r)
        seg_counts[r.segment] += 1

    gaps = [seg for seg, items in by_seg.items() if not items]
    selected.sort(key=lambda x: (config.SEGMENTS[x.segment]["label"], x.significance), reverse=False)
    return selected, gaps

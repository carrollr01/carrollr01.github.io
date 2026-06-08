#!/usr/bin/env python3
"""
Weekly fintech deal sweep — terminal entry point.

  python sweep.py --dry-run
      Hit the RSS feeds only. Prints every candidate headline. No LLM calls,
      no API cost. Use this to see real volume before spending anything.

  python sweep.py
      Full run: ingest -> grounded extract -> VERIFIER LENS -> dedup -> filter
      (announced-only, >$25M raises) -> score -> select-across-sectors with an
      iterative gap-fill loop -> write the two-tab Excel tracker.

  python sweep.py --out tracker.xlsx --since-days 7

Set ANTHROPIC_API_KEY in the environment (or a .env file). The picker tries hard
to fill every sector but NEVER invents a deal: a sector with no real, verified,
in-window deal is reported as a genuine gap and left empty.
"""
from __future__ import annotations

# Use the OS cert store so requests work behind a TLS-inspection proxy. Harmless
# on a normal network; skipped if truststore isn't installed.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import os
from datetime import date

import config
import ingest
import process
import exporter


def _load_dotenv():
    """Minimal .env loader so users can drop ANTHROPIC_API_KEY in a file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _extract_and_verify(cands: list[dict]):
    """Extract + verify every candidate, fanned out across worker threads.

    The two LLM calls per candidate (grounded extract, then adversarial verify)
    are pipelined per item and run concurrently — this is the single biggest
    speedup, since the calls are network-bound. Lazy-imported so --dry-run never
    needs anthropic installed/keyed."""
    import extract
    import verify
    from concurrent.futures import ThreadPoolExecutor, as_completed
    n = len(cands)
    if not n:
        return []
    extract.get_client()        # pre-warm the shared client (avoid a thread race)
    print(f"[llm] extracting + verifying {n} candidates "
          f"({config.LLM_CONCURRENCY} workers)...")

    def _one(c):
        r = extract.extract_one(c)
        return verify.verify_one(r) if r else None

    out, done = [], 0
    with ThreadPoolExecutor(max_workers=config.LLM_CONCURRENCY) as ex:
        for fut in as_completed([ex.submit(_one, c) for c in cands]):
            done += 1
            try:
                r = fut.result()
            except Exception as e:
                print(f"[llm] error: {e}")
                r = None
            if r:
                out.append(r)
            if done % 25 == 0 or done == n:
                print(f"   [llm] {done}/{n} processed, {len(out)} verified deals so far")
    print(f"[llm] {len(out)} verified fintech deals")
    return out


def cmd_dry_run(args):
    feeds = list(config.FEEDS)
    if args.deep:                       # also fire every broaden tier
        for k in config.SEGMENTS:
            for rd in range(config.MAX_GAPFILL_ROUNDS):
                feeds.extend(config.segment_feeds(k, round_idx=rd))
    cands = ingest.collect_candidates(feeds=feeds, lookback_days=args.search_days)
    print("\n--- DRY RUN: candidates (no LLM calls, no cost) ---")
    for i, c in enumerate(cands, 1):
        seg = c.get("feed_segment") or "general"
        print(f"{i:3}. [{seg}] {c['raw_title']}")
    print(f"\nTotal candidates: {len(cands)}")
    print("Run without --dry-run to extract + verify + rank them.")


def cmd_run(args):
    seen_titles: list[str] = []

    # Round 0 — primary feeds across every sector. Collect on the WIDE search net;
    # the strict announcement window is applied by enforce_window() below.
    cands = ingest.collect_candidates(feeds=config.FEEDS, seen_titles=seen_titles,
                                      lookback_days=args.search_days)
    cands = ingest.enrich_candidates(cands)
    all_records = _extract_and_verify(cands)
    all_records = process.dedup_records(all_records)
    all_records, n_stale = process.enforce_window(all_records, args.since_days)
    eligible, drops = process.filter_eligible(all_records)
    if n_stale:
        drops["stale_out_of_window"] = n_stale
    eligible = process.score(eligible)
    selected, gaps = process.select(eligible)

    # Gap-fill loop — the picker does NOT cop out after one pass. For every
    # still-empty sector, escalate through progressively broader searches.
    rounds = args.gapfill_rounds if args.gapfill_rounds is not None else config.MAX_GAPFILL_ROUNDS
    rd = 0
    while gaps and rd < rounds:
        labels = ", ".join(config.SEGMENTS[g]["label"] for g in gaps)
        print(f"\n[gapfill] round {rd + 1}/{rounds} — chasing empty sectors: {labels}")
        feeds = [f for g in gaps for f in config.segment_feeds(g, round_idx=rd)]
        more = ingest.collect_candidates(feeds=feeds, seen_titles=seen_titles,
                                         lookback_days=args.search_days, quiet=True)
        if more:
            more = ingest.enrich_candidates(more)
            new_records = _extract_and_verify(more)
            all_records = process.dedup_records(all_records + new_records)
            all_records, n_stale = process.enforce_window(all_records, args.since_days)
            eligible, drops = process.filter_eligible(all_records)
            if n_stale:
                drops["stale_out_of_window"] = n_stale
            eligible = process.score(eligible)
            selected, gaps = process.select(eligible)
        rd += 1

    out = args.out or f"fintech_deals_{date.today():%Y-%m-%d}.xlsx"
    path, n_ma, n_raise = exporter.export(selected, out)

    # ── summary ────────────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print(f"[sweep] wrote {path}")
    print(f"   M&A tab:            {n_ma}")
    print(f"   Capital Raises tab: {n_raise}")
    print(f"   TOTAL selected:     {len(selected)}  "
          f"(target {config.TARGET_TOTAL_MIN}-{config.TARGET_TOTAL_MAX})")
    covered = {r.segment for r in selected}
    print(f"   Sectors covered:    {len(covered)}/{len(config.SEGMENTS)}")
    if drops:
        print(f"   Dropped (rules):    " +
              ", ".join(f"{k}={v}" for k, v in sorted(drops.items())))
    if gaps:
        print("\n   GENUINE GAPS (no real, verified, in-window deal found — NOT invented):")
        for g in gaps:
            print(f"     • {config.SEGMENTS[g]['label']}  — backfill manually if needed")
    if len(selected) < config.TARGET_TOTAL_MIN:
        print(f"\n   NOTE: found {len(selected)} (< {config.TARGET_TOTAL_MIN}). Nothing was "
              f"fabricated to hit the target — this reflects a quiet week / feed limits.")
    print("=" * 64)


def main():
    _load_dotenv()
    p = argparse.ArgumentParser(description="Weekly fintech deal sweep -> two-tab Excel")
    p.add_argument("--dry-run", action="store_true",
                   help="feeds only; print candidates; no LLM, no cost")
    p.add_argument("--deep", action="store_true",
                   help="(with --dry-run) also fire every broaden-tier query")
    p.add_argument("--since-days", type=int, default=config.LOOKBACK_DAYS,
                   help=f"STRICT announcement window in days — the brief (default "
                        f"{config.LOOKBACK_DAYS}); deals announced before this are dropped")
    p.add_argument("--search-days", type=int, default=config.SEARCH_LOOKBACK_DAYS,
                   help=f"wider candidate-search net in days (default "
                        f"{config.SEARCH_LOOKBACK_DAYS}); recall only, never relaxes --since-days")
    p.add_argument("--gapfill-rounds", type=int, default=None,
                   help=f"max gap-fill search rounds (default {config.MAX_GAPFILL_ROUNDS})")
    p.add_argument("--out", default=None, help="output .xlsx path")
    p.add_argument("--fast", action="store_true",
                   help="use Haiku for extraction (Sonnet still verifies) — much faster/cheaper")
    p.add_argument("--workers", type=int, default=None,
                   help=f"LLM concurrency (default {config.LLM_CONCURRENCY}); lower if rate-limited")
    args = p.parse_args()
    if args.fast:
        config.EXTRACT_MODEL = config.FAST_EXTRACT_MODEL
    if args.workers:
        config.LLM_CONCURRENCY = args.workers
    (cmd_dry_run if args.dry_run else cmd_run)(args)


if __name__ == "__main__":
    main()

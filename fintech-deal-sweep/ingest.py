"""
Stage 1-2: pull candidates from free Google-News RSS, resolve real links,
fetch article bodies, gate, and dedup near-identical headlines BEFORE spending
any LLM tokens.

Gate logic (recall-first):
  * pre_scoped feeds (sector/deal-type queries): keep if a deal word appears.
  * general feeds: keep only if BOTH a fintech hint AND a deal word appear.

Deps: feedparser, requests, trafilatura, rapidfuzz. (googlenewsdecoder optional.)
"""
from __future__ import annotations
import hashlib
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from rapidfuzz import fuzz

import config

UA = {"User-Agent": "Mozilla/5.0 (fintech-deal-sweep; internal research tool)"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _has_deal_word(t: str) -> bool:
    return any(k in t for k in config.MA_KEYWORDS) or any(k in t for k in config.RAISE_KEYWORDS)


def _has_fintech_hint(t: str) -> bool:
    return ("fintech" in t or "financial technology" in t
            or any(h in t for seg in config.SEGMENTS.values() for h in seg["hints"]))


def _excluded(title: str) -> bool:
    t = _norm(title)
    return any(k in t for k in config.EXCLUDE_KEYWORDS)


def _passes_gate(text: str, pre_scoped: bool) -> bool:
    t = _norm(text)
    if pre_scoped:
        return _has_deal_word(t)          # the query already implies the sector
    return _has_fintech_hint(t) and _has_deal_word(t)


def _resolve_url(link: str, source_href: str | None = None) -> str:
    """Turn a Google-News redirect into the real publisher URL. Non-google -> as-is.

    A google.com/rss link in the final sheet is unacceptable (it's not a press
    release and it can't be fetched for full text), so we try hard: the
    maintained decoder, then the legacy base64 path, then an HTTP redirect, and
    finally the publisher link the feed itself supplied. Only if ALL fail do we
    return the original."""
    if "news.google.com" not in link:
        return link
    try:
        from googlenewsdecoder import gnewsdecoder
        out = gnewsdecoder(link, interval=1)
        if out.get("status") and out.get("decoded_url"):
            return out["decoded_url"]
    except Exception:
        pass
    try:
        import base64
        seg = link.split("/articles/")[-1].split("?")[0]
        seg += "=" * (-len(seg) % 4)
        raw = base64.urlsafe_b64decode(seg)
        m = re.search(rb"https?://[^\s\x00-\x1f\"'<>]+", raw)
        if m:
            return m.group(0).decode("utf-8", "ignore")
    except Exception:
        pass
    try:                                            # follow the redirect ourselves
        resp = requests.get(link, headers=UA, timeout=20, allow_redirects=True)
        if resp.url and "news.google.com" not in resp.url:
            return resp.url
    except Exception:
        pass
    if source_href and "news.google.com" not in source_href:
        return source_href                          # the feed's own publisher link
    return link


def _fetch_full_text(url: str, fallback: str) -> str:
    """Fetch + extract the article body. Falls back to the RSS snippet on failure."""
    try:
        import trafilatura
    except ImportError:
        return fallback
    try:
        downloaded = trafilatura.fetch_url(url)
        text = trafilatura.extract(downloaded, include_comments=False) if downloaded else ""
        if text and len(text) > len(fallback):
            return text
    except Exception:
        pass
    try:
        html = requests.get(url, headers=UA, timeout=20).text
        text = trafilatura.extract(html, include_comments=False) or ""
        if len(text) > len(fallback):
            return text
    except Exception:
        pass
    return fallback


def enrich_candidates(cands: list[dict]) -> list[dict]:
    """Resolve Google-News links to the real article and fetch full body text.
    One network round-trip per item — the slow step, used only for real runs.
    Parallelized; failures degrade to the RSS snippet (thinner text, not a crash)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    n = len(cands)
    if not n:
        return cands
    print(f"[enrich] resolving links + fetching {n} articles for full text "
          f"(~1-2 min; this is what lets the model name companies + write descriptions)...")

    def work(c):
        c["source_url"] = _resolve_url(c["source_url"], c.get("source_href"))
        if len(c["body"]) < 800:
            c["body"] = _fetch_full_text(c["source_url"], c["body"])
        return c

    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for _ in as_completed([ex.submit(work, c) for c in cands]):
            done += 1
            if done % 10 == 0 or done == n:
                print(f"   [enrich] {done}/{n}")
    return cands


def collect_candidates(feeds: list[dict] | None = None,
                       seen_titles: list[str] | None = None,
                       lookback_days: int | None = None,
                       quiet: bool = False) -> list[dict]:
    """Pull + gate + title-dedup candidates from the given feeds (default: all).

    `seen_titles` lets callers carry de-dup memory across multiple collection
    rounds (the gap-fill loop), so re-searches never re-surface the same story.
    """
    feeds = feeds if feeds is not None else config.FEEDS
    lookback_days = lookback_days or config.LOOKBACK_DAYS
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    seen_titles = seen_titles if seen_titles is not None else []
    candidates: list[dict] = []
    per_feed_counts = {}

    for feed in feeds:
        if not feed.get("url"):
            continue
        try:
            parsed = feedparser.parse(feed["url"])
        except Exception as e:
            print(f"[warn] {feed['name']}: {e}")
            continue

        kept = 0
        for entry in parsed.entries:
            title = entry.get("title", "")
            pub = None
            if entry.get("published_parsed"):
                pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                if pub < cutoff:
                    continue

            body = entry.get("content", [{}])[0].get("value") if entry.get("content") \
                else entry.get("summary", "")
            body = re.sub(r"<[^>]+>", " ", body or "")

            if _excluded(title):
                continue
            if not _passes_gate(f"{title}\n{body}", feed.get("pre_scoped", False)):
                continue

            nt = _norm(title)
            if any(fuzz.token_sort_ratio(nt, s) >= config.TITLE_SIM_THRESHOLD * 100
                   for s in seen_titles):
                continue
            seen_titles.append(nt)

            src = entry.get("source", {}) or {}
            candidates.append({
                "source_name": feed["name"], "source_tier": feed["tier"],
                "source_url": entry.get("link", ""), "raw_title": title,
                "source_href": src.get("href"),        # publisher link the feed supplied
                "published_at": pub.isoformat() if pub else None,
                "feed_segment": feed.get("segment"),   # hint only; verifier decides
                "body": body,                           # snippet now; enrich() adds full text
            })
            kept += 1
        per_feed_counts[feed["name"]] = (len(parsed.entries), kept)

    if not quiet:
        for name, (raw, kept) in per_feed_counts.items():
            print(f"   feed {name}: {raw} entries -> {kept} kept")
    print(f"[ingest] {len(candidates)} candidates after gate + title-dedup")
    return candidates


def candidate_id(target_or_title: str, deal_type: str) -> str:
    return hashlib.sha1(f"{_norm(target_or_title)}|{deal_type}".encode()).hexdigest()[:16]

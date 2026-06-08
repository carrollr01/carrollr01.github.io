"""
Stage 3: grounded extraction — the first anti-hallucination layer.

The model sees ONE article and extracts from THAT text only. Money figures are
verbatim-or-null and never derived; every populated field carries its source
sentence; a record cannot exist without a named target and a source URL.

A second, independent adversarial pass (verify.py) re-checks every record and
drops anything not fully supported by the text.

Requires: anthropic.  Reads ANTHROPIC_API_KEY from env.
"""
from __future__ import annotations
import json
import re
from typing import Optional

import anthropic

import config
from models import DealRecord, now_iso, parse_money_to_usd

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


_SEGMENT_LIST = "\n".join(f'  - {k}: {v["label"]}' for k, v in config.SEGMENTS.items())

SYSTEM = f"""You extract structured private-market deal data from a SINGLE news \
article or press release. You are a meticulous M&A / financing analyst, and you \
are deeply skeptical.

═══ ABSOLUTE ANTI-HALLUCINATION RULES (non-negotiable) ═══
1. Use ONLY the supplied text. You have NO outside knowledge. Never use memory \
or inference about any company, valuation, revenue, EBITDA, round, or person. \
If you "know" a fact that isn't written here, you must NOT use it.
2. If a field is not EXPLICITLY stated in the text, return null. Never guess, \
never approximate, never round, never fill from context.
3. Money fields (ev, amount, valuation): return a value ONLY if a specific \
figure for THIS deal is written in the text. Copy it VERBATIM, exactly as it \
appears (e.g. "$800 million", "€40M", "$110m"). NEVER derive a figure from \
revenue, a multiple, headcount, ARR, a prior round, or "sources say". If no \
explicit figure is stated, return null.
4. ev_revenue / ev_ebitda: return a multiple ONLY if it is explicitly printed \
in the text (rare). Never compute it. Almost always null.
5. target: the fintech company being ACQUIRED (M&A) or doing the RAISE (round). \
NEVER put the acquirer/investor here. If the target is not actually NAMED \
(e.g. "a Clearwater firm", "a Swedish neobank", "an undisclosed startup"), \
return target=null and is_fintech=false. A deal with no named target is useless.
6. For EVERY non-null field except segment, is_fintech, deal_status and \
target_description, include the EXACT source sentence in "evidence". If you \
cannot quote a sentence that states a field, that field MUST be null.
7. When unsure whether something is stated, treat it as NOT stated -> null. \
Lower your confidence rather than inventing precision.

═══ SCOPE — FINTECH (TECHNOLOGY-LED) DEALS ONLY ═══
This tracker covers DEALS in fintech COMPANIES — financial-TECHNOLOGY/software \
businesses. At least one side of the deal must be a technology-led fintech. Set \
is_fintech=false for anything that is not a fintech-company acquisition or a \
fintech-company funding round, including:
  - a deal where BOTH sides are TRADITIONAL financial institutions with no \
technology angle (e.g. one community/regional bank buying another, a classic \
insurer or wealth-advisory roll-up). A 100-year-old community bank, a \
conventional insurer, or an advisory firm is NOT a fintech target unless the \
article makes clear it is primarily a technology/software platform.
  - a fund/vehicle close or commitment ("X raises $5bn for a private-credit / PE \
/ venture fund") — that is a fundraise BY an investor, not a company deal
  - an asset manager changing its holdings of a public stock ("X raises position \
in / acquires shares of [company]") — these are 13F filings, NOT deals
  - secondary share sales, buybacks, token/crypto transfers, ETF/index changes
  - earnings, partnerships, product launches, hires, or regulatory rulings
If unsure whether the target is technology-led, lower confidence and say so; do \
not pass off a traditional-bank merger as a fintech deal.

═══ deal_type ═══
  STRATEGIC_MA  — acquisition/merger where the buyer is an OPERATING company
  PE_BUYOUT     — acquisition where the buyer is a PRIVATE-EQUITY / financial sponsor
  CAPITAL_RAISE — a PRIMARY funding round (seed / Series / growth equity)
If the article is none of these, set is_fintech=false and deal_type=UNKNOWN.

═══ deal_status (critical: we ONLY want fresh ANNOUNCEMENTS) ═══
Classify what STAGE this article reports. One of:
  announced  — the deal is being NEWLY disclosed, agreed, signed, or launched \
for the first time. For a funding round, a newly disclosed/closed round is \
"announced" (the close IS the announcement).
  completed  — the CLOSING / COMPLETION / consummation of an M&A deal that was \
ANNOUNCED EARLIER ("has completed its previously announced acquisition…").
  regulatory — a REGULATORY / ANTITRUST PROCESS step on a deal that was \
announced earlier: a merger filing or notification, a referral to a competition \
tribunal/commission, the opening of a review or in-depth (Phase II) \
investigation, a clearance/approval/conditional approval, or a prohibition / \
block / challenge. Examples: "CADE submitted the case to the Tribunal", "EU \
opens in-depth probe into X's acquisition", "FTC sues to block", "regulator \
clears the deal". These are NOT the announcement — the deal was announced before.
  rumor      — talks/speculation/"in advanced discussions"/"is exploring" with \
no signed/agreed deal yet.
  unknown    — cannot tell.
Set "announced" ONLY for the first-time disclosure of an agreed deal or a closed \
funding round. If the news is a closing, a regulatory/antitrust step, or mere \
talks, use the matching status above — do NOT call it "announced". Note: a \
genuine announcement that merely MENTIONS it is "subject to regulatory approval" \
is still "announced"; status is "regulatory" only when the regulatory step \
ITSELF is the news.

═══ target_description (house voice) ═══
Write ONE descriptor of what the TARGET does, in this EXACT voice:
  - Begin with a noun phrase naming the company's category, usually led by one \
quality word ("Leading …", "Full-stack …", "Enterprise-grade …", "Provider of \
…", "Platform for …"). NEVER begin with the company name, "It", or "The company".
  - Third person, present tense, factual B2B descriptor. At most one lead \
adjective; no marketing fluff.
  - One line, ~12-25 words, NO closing period.
  - Draw ONLY from the article; never invent capabilities, customers, or metrics.
  Examples of the exact voice:
    "Leading Buy Now Pay Later provider with a fair, transparent credit model designed for account-to-account payments"
    "Provider of new-home construction data, homebuilder software, and residential real estate marketplaces"
    "Financial data layer that sources, structures, and distributes historical financials covering 5,500+ public companies globally"
    "Vendor-financing platform and lender for enterprise technology companies"

═══ segment — choose exactly one key (best fit) or null ═══
{_SEGMENT_LIST}

Return ONLY a JSON object, no prose:
  is_fintech (bool),
  deal_type (string),
  deal_status (string: announced | completed | regulatory | rumor | unknown),
  target (string|null),
  counterparty (string|null — acquirer for M&A, or lead investor(s) for a raise),
  target_country (string|null — the TARGET company's HQ country),
  date_announced (string|null — ISO yyyy-mm-dd of when THIS deal was first \
ANNOUNCED/agreed/signed. If the article is a later write-up that refers back to \
an earlier announcement date, use that ORIGINAL announcement date, not today's. \
Null if no date is given),
  ev (string|null — verbatim enterprise/transaction value; M&A only),
  amount (string|null — verbatim round size; raises only),
  valuation (string|null — verbatim post/pre-money valuation; raises only),
  ev_revenue (string|null), ev_ebitda (string|null),
  target_description (string|null),
  segment (string|null),
  confidence (0..1 — your confidence this is a real, correctly-parsed fintech deal),
  evidence (object mapping each non-null field name -> its verbatim source sentence)."""


def _parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        text = text[s:e + 1]
    return json.loads(text)


def extract_one(cand: dict) -> Optional[DealRecord]:
    msg = get_client().messages.create(
        model=config.EXTRACT_MODEL,
        max_tokens=1600,
        system=SYSTEM,
        messages=[{"role": "user",
                   "content": f"ARTICLE TITLE: {cand['raw_title']}\n\n"
                              f"ARTICLE TEXT:\n{cand['body'][:9000]}"}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        d = _parse_json(text)
    except (json.JSONDecodeError, ValueError):
        print(f"[extract] unparseable JSON for: {cand['raw_title'][:60]}")
        return None

    if not d.get("is_fintech") or d.get("deal_type") in (None, "UNKNOWN"):
        return None

    target = (d.get("target") or "").strip()
    if not target:                                 # no named target -> useless
        return None

    date_announced = d.get("date_announced")
    if not date_announced and cand.get("published_at"):
        date_announced = cand["published_at"][:10]

    value_for_sort = parse_money_to_usd(d.get("ev")) or parse_money_to_usd(d.get("amount"))

    rec = DealRecord(
        source_name=cand["source_name"], source_url=cand["source_url"],
        source_tier=cand["source_tier"], raw_title=cand["raw_title"],
        fetched_at=now_iso(), published_at=cand.get("published_at"),
        deal_type=d["deal_type"], deal_status=(d.get("deal_status") or "unknown").lower(),
        target=target, counterparty=d.get("counterparty"),
        target_country=d.get("target_country"), date_announced=date_announced,
        ev_text=d.get("ev"), amount_text=d.get("amount"), valuation_text=d.get("valuation"),
        ev_revenue=d.get("ev_revenue"), ev_ebitda=d.get("ev_ebitda"),
        target_description=d.get("target_description"), segment=d.get("segment"),
        value_usd_est=value_for_sort,
        confidence=float(d.get("confidence") or 0.0), evidence=d.get("evidence") or {},
    )
    # Carry the article text on the record so the verifier can re-check it against
    # the source. It's a transient attribute, not part of the persisted schema.
    rec.article_text = cand["body"]
    return rec

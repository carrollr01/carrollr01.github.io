"""
Central configuration for the weekly fintech deal sweep.

Everything tunable lives here: the sector taxonomy, the search feeds, the
keyword gates, the scoring weights, and the model ids. No secrets — the
Anthropic key is read from the environment (ANTHROPIC_API_KEY).
"""
from __future__ import annotations
import os
from urllib.parse import quote_plus

# ── window ────────────────────────────────────────────────────────────────
# Only deals ANNOUNCED in this trailing window make the brief. This is the HARD
# gate (a weekly brief = 7 days) and is enforced on the announcement date, never
# loosened. Closings / completions of previously-announced deals are dropped.
LOOKBACK_DAYS = int(os.getenv("SWEEP_LOOKBACK_DAYS", "7"))

# The candidate SEARCH net is deliberately WIDER than the brief window. Google
# indexes/timestamps stories a day or two after the wire, so searching only 7
# days silently misses deals announced late in the window. We pull ~14 days of
# candidates, then the strict LOOKBACK_DAYS filter (enforce_window) keeps only
# those actually ANNOUNCED within the brief. Wider net, same hard gate — this is
# how the sweep "tries harder" without ever bending the 7-day rule.
SEARCH_LOOKBACK_DAYS = int(os.getenv("SWEEP_SEARCH_DAYS", "14"))

# ── growth-raise floor ─────────────────────────────────────────────────────
# Capital raises must have a DISCLOSED amount strictly above this to qualify.
# Undisclosed or smaller rounds are excluded (we never assume a figure).
GROWTH_RAISE_MIN_USD = float(os.getenv("SWEEP_RAISE_MIN_USD", "25000000"))

# ── selection targets ──────────────────────────────────────────────────────
TARGET_TOTAL_MIN = 15          # we aim for at least this many across all sectors
TARGET_TOTAL_MAX = 20          # …and never export more than this
PER_SEGMENT_MIN = 1            # guarantee >=1 per sector *if a real one exists*
PER_SEGMENT_MAX = 3            # cap any single sector so it can't crowd the rest

# How hard the picker tries before admitting a sector is genuinely empty. Each
# round fires additional, progressively broader searches for the still-empty
# sectors (see each sector's "broaden" tiers). More rounds = more recall + more
# LLM spend, but only for sectors that are actually still empty. A truly quiet
# sector still ends as a genuine gap — the rounds widen the search, they never
# lower the bar for what counts as a real, verified, in-window deal.
MAX_GAPFILL_ROUNDS = int(os.getenv("SWEEP_GAPFILL_ROUNDS", "4"))

# ── dedup / similarity ─────────────────────────────────────────────────────
TITLE_SIM_THRESHOLD = 0.86     # near-duplicate headline merge (pre-LLM)
RECORD_SIM_THRESHOLD = 0.88    # same-deal merge (post-extraction)

# ── scoring ────────────────────────────────────────────────────────────────
VALUE_REF_MIN_USD = 5_000_000
VALUE_REF_MAX_USD = 5_000_000_000
UNDISCLOSED_VALUE_BASELINE = 0.35
RANK_WEIGHTS = {"value": 0.5, "source": 0.2, "recency": 0.3}

# ── models ─────────────────────────────────────────────────────────────────
# Extraction reads one article and emits structured JSON. The verifier is an
# independent adversarial second pass. Both are overridable via env.
EXTRACT_MODEL = os.getenv("SWEEP_EXTRACT_MODEL", "claude-sonnet-4-6")
VERIFY_MODEL = os.getenv("SWEEP_VERIFY_MODEL", "claude-sonnet-4-6")
# `--fast` swaps extraction to Haiku (much faster/cheaper); the Sonnet verifier
# still backstops quality, so accuracy holds up while throughput jumps.
FAST_EXTRACT_MODEL = os.getenv("SWEEP_FAST_MODEL", "claude-haiku-4-5-20251001")

# ── performance ────────────────────────────────────────────────────────────
# LLM calls and article fetches are network-bound, so we fan them out. Lower
# LLM_CONCURRENCY if you hit Anthropic rate limits (HTTP 429).
LLM_CONCURRENCY = int(os.getenv("SWEEP_LLM_CONCURRENCY", "10"))
ENRICH_CONCURRENCY = int(os.getenv("SWEEP_ENRICH_CONCURRENCY", "10"))
FEED_CONCURRENCY = int(os.getenv("SWEEP_FEED_CONCURRENCY", "12"))
# Bound how many candidates reach the expensive enrich+LLM stages. The feeds are
# already recency- and gate-filtered; keeping the newest N per feed (and overall)
# is what stops a 600-candidate week from costing 1,200 LLM calls. Gap-fill still
# chases any empty sector, so coverage is preserved.
MAX_CANDIDATES_PER_FEED = int(os.getenv("SWEEP_MAX_PER_FEED", "30"))
MAX_CANDIDATES = int(os.getenv("SWEEP_MAX_CANDIDATES", "300"))

# ── sector taxonomy (the 8 unique sub-sectors, deduped) ────────────────────
# key       : machine id used internally
#   label   : EXACT display string written to the Excel "Sector" column
#   hints   : lowercase tokens used by the ingest gate to spot the sector
#   query   : Google-News search fragment (primary pass)
#   broaden : extra search fragments fired only when the sector is still empty,
#             one list entry per gap-fill round (the picker does NOT give up
#             after one go — it escalates through these)
SEGMENTS: dict[str, dict] = {
    "banking_lending": {
        "label": "Banking & Lending Tech",
        "hints": ["bank", "banking", "lending", "loan", "lender", "neobank",
                  "bnpl", "buy now pay later", "credit", "core banking", "deposit",
                  "underwrite loan", "embedded finance"],
        "query": '(neobank OR "digital bank" OR lending OR "lending platform" OR bnpl OR "core banking")',
        "broaden": [
            '("buy now pay later" OR "consumer lending" OR "credit platform" OR "loan origination")',
            '("embedded finance" OR "banking-as-a-service" OR "small business lending" OR "deposit platform")',
            '("commercial lending" OR "credit union technology" OR "mortgage lender" OR fintech bank)',
            '(lendtech OR "loan management" OR "credit scoring" OR "alternative lending" OR neolender)',
        ],
    },
    "corporate_finance": {
        "label": "Corporate Financial Function",
        "hints": ["accounts payable", "accounts receivable", "spend management",
                  "expense", "procurement", "treasury", "erp", "accounting",
                  "invoicing", "billing", "fp&a", "corporate card", "b2b payments",
                  "accounts automation", "close software"],
        "query": '("spend management" OR "accounts payable" OR procurement OR treasury OR "expense management")',
        "broaden": [
            '("corporate card" OR "b2b payments" OR invoicing OR billing OR "accounts receivable")',
            '(accounting software OR "financial close" OR "fp&a" OR "ERP finance")',
            '("CFO platform" OR "treasury management" OR "spend platform" OR "procure-to-pay")',
            '("expense automation" OR "vendor payments" OR "financial operations" OR "bill pay" software)',
        ],
    },
    "financial_info": {
        "label": "Financial Info & Analytics",
        "hints": ["financial data", "market data", "analytics", "financial information",
                  "data provider", "ratings", "research platform", "financial analytics",
                  "alternative data", "index provider"],
        "query": '("financial data" OR "market data" OR "financial analytics" OR "data provider")',
        "broaden": [
            '("alternative data" OR "investment research" OR "ratings agency" OR "index provider")',
            '("ESG data" OR "credit data" OR "pricing data" OR "reference data")',
            '("financial intelligence" OR "data analytics" OR "research platform" fintech)',
            '("benchmarking data" OR "private market data" OR "regulatory data" OR "data infrastructure" finance)',
        ],
    },
    "insurtech": {
        "label": "InsurTech",
        "hints": ["insurance", "insurtech", "insurer", "underwriting", "claims",
                  "reinsurance", "policy", "broker insurance", "actuarial"],
        "query": '(insurtech OR insurance OR underwriting OR claims OR reinsurance)',
        "broaden": [
            '("insurance platform" OR "digital insurer" OR "claims automation" OR "embedded insurance")',
            '("commercial insurance" OR "life insurance technology" OR "p&c insurance" OR mga)',
            '("insurance broker" OR "policy administration" OR "underwriting platform")',
            '("health insurance technology" OR "insurance distribution" OR "parametric insurance" OR "claims platform")',
        ],
    },
    "payments": {
        "label": "Payments",
        "hints": ["payments", "payment", "paytech", "card", "checkout",
                  "merchant acquiring", "pos", "wallet", "money transfer",
                  "remittance", "payment gateway", "payment processor", "acquirer payments"],
        "query": '(payments OR paytech OR "payment platform" OR checkout OR "money transfer" OR remittance)',
        "broaden": [
            '("payment processor" OR "merchant acquiring" OR "point of sale" OR "digital wallet")',
            '("cross-border payments" OR "payment gateway" OR "real-time payments" OR "card issuing")',
            '("payment orchestration" OR "stablecoin payments" OR "payment infrastructure")',
            '(acquiring OR payouts OR "A2A payments" OR "bill payments" OR "payment API")',
        ],
    },
    "capital_markets": {
        "label": "Capital Markets Tech",
        "hints": ["capital markets", "trading", "brokerage", "exchange", "clearing",
                  "settlement", "post-trade", "order management", "execution",
                  "securities", "market infrastructure"],
        "query": '("capital markets" OR trading OR brokerage OR "post-trade" OR clearing OR settlement)',
        "broaden": [
            '("order management system" OR "execution platform" OR "trading technology" OR "market infrastructure")',
            '("securities settlement" OR "clearing house" OR "exchange technology" OR "fixed income trading")',
            '("prime brokerage" OR "private markets technology" OR "tokenized securities")',
            '("trade surveillance" OR "post-trade technology" OR "FIX connectivity" OR "liquidity platform")',
        ],
    },
    "real_estate_mortgage": {
        "label": "Real Estate & Mortgage Tech",
        "hints": ["mortgage", "real estate", "proptech", "home loan",
                  "title insurance", "property", "real estate finance", "home equity"],
        "query": '(mortgage OR proptech OR "real estate" OR "title insurance")',
        "broaden": [
            '("mortgage technology" OR "home loan platform" OR "real estate finance" OR "home equity")',
            '("property management software" OR "title insurance" OR "closing platform")',
            '("real estate marketplace" OR "construction finance" OR "rent technology")',
            '("loan origination system" OR "appraisal technology" OR "home equity platform" OR "escrow technology")',
        ],
    },
    "asset_wealth": {
        "label": "Asset & Wealth Tech",
        "hints": ["wealth", "wealthtech", "asset management", "wealth management",
                  "investing", "robo-advisor", "portfolio", "brokerage app",
                  "retirement", "ria"],
        "query": '(wealthtech OR "wealth management" OR "asset management" OR "robo-advisor" OR investing)',
        "broaden": [
            '("digital wealth" OR "RIA technology" OR "portfolio management" OR "retirement platform")',
            '("investment platform" OR "private wealth" OR "alternative investments platform")',
            '("brokerage app" OR "advisor technology" OR "asset management technology")',
            '("model portfolios" OR "custody technology" OR TAMP OR "direct indexing" OR "wealthtech platform")',
        ],
    },
}

# ── deal-type keyword gates (ingest, pre-LLM) ──────────────────────────────
MA_KEYWORDS = [
    "acquire", "acquires", "acquired", "acquisition", "acquiring", "merger",
    "merges", "merge", "buyout", "buy-out", "takeover", "take private",
    "takes private", "to buy", "buys", "majority stake", "majority investment",
    "snaps up", "acquire stake",
]
RAISE_KEYWORDS = [
    "raises", "raised", "raise", "funding round", "funding", "series a",
    "series b", "series c", "series d", "series e", "series f", "growth round",
    "growth equity", "growth investment", "secures", "closes round",
    "closes funding", "investment round", "led by", "valuation", "valued at",
    "venture round", "equity round",
]

# Clear non-deal noise we drop at the gate. NOTE: we deliberately do NOT
# blanket-exclude "closes"/"completes" here — for funding rounds a "close" is
# the announcement. The announced-vs-completed M&A call is made by the
# verifier against the actual text, not by crude keywords.
EXCLUDE_KEYWORDS = [
    "partnership", "partners with", "teams up", "collaborat", "integrat",
    "launches", "unveils", "rolls out", "appoints", "names ceo", "hires",
    "promotes", "earnings", "quarterly results", "q1 results", "q2 results",
    "q3 results", "q4 results", "price target", "downgrade", "upgrade",
    "analyst", "13f", "raises stake in shares", "raises position", "etf",
    "index fund", "airdrop", "token sale", "lawsuit", "fined", "regulator",
    "how to", "opinion", "podcast",
]


def gnews_feed_url(query: str) -> str:
    """Build a Google-News RSS search URL. Uses the WIDE search net; the strict
    7-day announcement gate is applied later by process.enforce_window()."""
    q = f"{query} when:{SEARCH_LOOKBACK_DAYS}d"
    return ("https://news.google.com/rss/search?q="
            f"{quote_plus(q)}&hl=en-US&gl=US&ceid=US:en")


_MA_GROUP = '(acquires OR acquisition OR merger OR buyout OR "takes private" OR "to acquire")'
_RAISE_GROUP = ('(raises OR "funding round" OR "Series A" OR "Series B" OR "Series C" '
                'OR "Series D" OR "growth equity" OR "growth round")')


def segment_feeds(seg_key: str, round_idx: int = -1) -> list[dict]:
    """Feeds for one sector. round_idx = -1 -> primary query; 0..n -> broaden tiers."""
    seg = SEGMENTS[seg_key]
    frag = seg["query"] if round_idx < 0 else seg["broaden"][min(round_idx, len(seg["broaden"]) - 1)]
    return [
        {"name": f"gn:{seg_key}:ma", "tier": 2, "pre_scoped": True, "segment": seg_key,
         "url": gnews_feed_url(f"{frag} {_MA_GROUP}")},
        {"name": f"gn:{seg_key}:raise", "tier": 2, "pre_scoped": True, "segment": seg_key,
         "url": gnews_feed_url(f"{frag} {_RAISE_GROUP}")},
    ]


# Primary feed set: every sector (M&A + raise) plus a few broad fintech nets.
FEEDS: list[dict] = []
for _k in SEGMENTS:
    FEEDS.extend(segment_feeds(_k, round_idx=-1))
FEEDS += [
    {"name": "gn:fintech:ma", "tier": 1, "pre_scoped": True, "segment": None,
     "url": gnews_feed_url(f"fintech {_MA_GROUP}")},
    {"name": "gn:fintech:raise", "tier": 1, "pre_scoped": True, "segment": None,
     "url": gnews_feed_url(f"fintech {_RAISE_GROUP}")},
    {"name": "gn:finservtech", "tier": 1, "pre_scoped": True, "segment": None,
     "url": gnews_feed_url('"financial technology" (acquisition OR funding OR "growth equity")')},
]

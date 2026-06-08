"""
Data model + grounding-safe money/date helpers.

Money is stored VERBATIM as written in the source (e.g. "$800 million"). A
parser converts a STATED figure to USD for thresholds, sorting and the "($M)"
display columns only. Converting a stated figure is safe arithmetic; inventing
an unstated one is not — so absent figures stay null everywhere.

No database: the sweep runs end-to-end and writes Excel directly.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# deal_type values
STRATEGIC_MA = "STRATEGIC_MA"
PE_BUYOUT = "PE_BUYOUT"
CAPITAL_RAISE = "CAPITAL_RAISE"
UNKNOWN = "UNKNOWN"

MA_TYPES = (STRATEGIC_MA, PE_BUYOUT)

DEAL_TYPE_LABEL = {
    STRATEGIC_MA: "Strategic M&A",
    PE_BUYOUT: "PE Buyout",
    CAPITAL_RAISE: "Capital Raise",
}


@dataclass
class DealRecord:
    # identity / provenance (grounding) — no source URL, no row
    source_name: str
    source_url: str
    source_tier: int
    raw_title: str
    fetched_at: str
    published_at: Optional[str] = None

    # the schema, matching the two output tabs
    deal_type: str = UNKNOWN
    deal_status: Optional[str] = None     # announced | completed | unknown
    target: Optional[str] = None
    counterparty: Optional[str] = None    # acquirer (M&A) or lead investor(s) (raise)
    target_country: Optional[str] = None
    date_announced: Optional[str] = None
    ev_text: Optional[str] = None         # M&A enterprise/transaction value, VERBATIM
    amount_text: Optional[str] = None     # raise size, VERBATIM
    valuation_text: Optional[str] = None  # raise valuation, VERBATIM
    ev_revenue: Optional[str] = None      # multiple, verbatim, usually None
    ev_ebitda: Optional[str] = None       # multiple, verbatim, usually None
    target_description: Optional[str] = None
    segment: Optional[str] = None

    # machine-only
    value_usd_est: Optional[float] = None  # parsed from ev/amount, for SORTING only
    confidence: float = 0.0
    evidence: dict = field(default_factory=dict)

    # verifier output
    verified: bool = False
    verify_reasons: list = field(default_factory=list)

    # workflow
    significance: float = 0.0

    @property
    def amount_usd(self) -> Optional[float]:
        return parse_money_to_usd(self.amount_text)

    @property
    def ev_usd(self) -> Optional[float]:
        return parse_money_to_usd(self.ev_text)


_MULT = {"trillion": 1e12, "tn": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
         "million": 1e6, "mn": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}


def parse_money_to_usd(text: Optional[str]) -> Optional[float]:
    """'$800 million' -> 8e8. Safe conversion of a STATED figure. None on failure.

    Currency is NOT FX-converted: a magnitude in € or £ is treated at face value,
    which is close enough for the $25M eligibility gate. Returns None if no clear
    figure is present (so undisclosed stays undisclosed)."""
    if not text:
        return None
    m = re.search(r"[\$€£]?\s*([\d,]+(?:\.\d+)?)\s*"
                  r"(trillion|tn|billion|bn|b|million|mn|mm|m|thousand|k)?",
                  text.lower())
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return num * _MULT.get(m.group(2) or "", 1.0)


def millions_value(text: Optional[str]) -> Optional[float]:
    """Numeric value in $M (e.g. 800.0) for an Excel cell, or None if undisclosed."""
    v = parse_money_to_usd(text)
    return round(v / 1e6, 1) if v else None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_date(s) -> str:
    """Normalize a date to '29-May-26'. Unknown/unparseable -> '' (blank cell)."""
    if not s:
        return ""
    s = str(s).strip()
    for f in ("%Y-%m-%d", "%Y/%m/%d", "%d-%b-%y", "%d-%b-%Y", "%B %d, %Y",
              "%b %d, %Y", "%m/%d/%Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, f).strftime("%d-%b-%y")
        except ValueError:
            pass
    return s

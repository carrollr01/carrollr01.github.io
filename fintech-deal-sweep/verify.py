"""
Stage 3b: the VERIFIER LENS — a second, independent, adversarial pass.

The extractor can still slip. This stage hands the *extracted record* plus the
*original article text* to a fresh model whose only job is to catch
hallucinations and ungrounded claims. It cannot add information — it can only:
  - REJECT the whole record (not a real fintech company deal, target unnamed,
    a fund close / 13F / secondary / partnership / earnings, or a COMPLETED M&A
    rather than a fresh announcement), or
  - NULL OUT any field not explicitly supported by a verbatim sentence, or
  - CORRECT a field to a value that is verbatim present in the text.

Anything the verifier can't ground gets removed, never invented. Records that
survive are marked verified=True.

Requires: anthropic.
"""
from __future__ import annotations
import json
import re
from typing import Optional

import config
from extract import get_client, _parse_json
from models import DealRecord, parse_money_to_usd

# Fields the verifier is allowed to correct/null. (Identity/provenance fields and
# machine bookkeeping are off-limits.)
_CHECKABLE = [
    "deal_type", "deal_status", "target", "counterparty", "target_country",
    "date_announced", "ev_text", "amount_text", "valuation_text",
    "ev_revenue", "ev_ebitda", "target_description", "segment",
]

_SEGMENT_LIST = "\n".join(f'  - {k}: {v["label"]}' for k, v in config.SEGMENTS.items())

SYSTEM = f"""You are an INDEPENDENT VERIFICATION ANALYST. Another model extracted \
the structured record below from the article. Assume it may be WRONG or \
HALLUCINATED. Your sole job is to catch anything that is not literally supported \
by the article text. Be adversarial and conservative.

You may NEVER add new information. You may only:
  (a) REJECT the entire record, or
  (b) set a field to null because the text does not explicitly support it, or
  (c) correct a field to a value that appears VERBATIM in the text.

REJECT the whole record (verdict="reject") if ANY of these is true:
  - The target company is not explicitly NAMED in the text.
  - It is not a fintech-COMPANY acquisition or a fintech-COMPANY primary funding \
round. Reject fund/vehicle closes ("raised $Xbn for a fund"), 13F/position \
changes, secondary share sales, buybacks, partnerships, product launches, \
hires, earnings, regulatory news, token/crypto items.
  - deal_type is STRATEGIC_MA or PE_BUYOUT but the text describes the COMPLETION \
of a previously-announced deal (i.e. deal_status should be "completed"). We only \
keep fresh ANNOUNCEMENTS, so reject completed M&A.
  - The target named in the record is actually the acquirer/investor.

Field-level grounding rules:
  - ev_text / amount_text / valuation_text: keep ONLY if that exact figure is \
written in the text for THIS deal. If it was derived, approximated, or pulled \
from a prior round/revenue/multiple, set it to null. Verify the number and units \
match the text character-for-character (allow trivial formatting like "$110m" vs \
"$110 million" only if both denote the same stated figure).
  - ev_revenue / ev_ebitda: keep ONLY if the multiple is explicitly printed.
  - counterparty, target_country, date_announced: keep ONLY if stated; else null.
  - target_description: must be supported by the article and must NOT assert \
capabilities/customers/metrics not in the text; otherwise null it.
  - segment: confirm the best-fit key from this list, else null:
{_SEGMENT_LIST}

Return ONLY JSON:
  verdict: "keep" | "reject",
  reject_reason: string | null,
  corrected: object containing ONLY the fields you changed, mapping field name to \
its corrected value (use null to drop a field). Omit fields you did not change.
  flags: array of short strings noting anything notable (e.g. "amount verbatim ok", \
"nulled unsupported valuation").
Field names you may correct: {", ".join(_CHECKABLE)}."""


def _record_payload(rec: DealRecord) -> dict:
    return {
        "deal_type": rec.deal_type, "deal_status": rec.deal_status,
        "target": rec.target, "counterparty": rec.counterparty,
        "target_country": rec.target_country, "date_announced": rec.date_announced,
        "ev_text": rec.ev_text, "amount_text": rec.amount_text,
        "valuation_text": rec.valuation_text, "ev_revenue": rec.ev_revenue,
        "ev_ebitda": rec.ev_ebitda, "target_description": rec.target_description,
        "segment": rec.segment, "evidence": rec.evidence,
    }


def verify_one(rec: DealRecord) -> Optional[DealRecord]:
    """Return the (possibly corrected) record if it survives, else None."""
    article = getattr(rec, "article_text", "") or rec.raw_title
    user = (f"ARTICLE TITLE: {rec.raw_title}\n\nARTICLE TEXT:\n{article[:9000]}\n\n"
            f"EXTRACTED RECORD (verify against the text above):\n"
            f"{json.dumps(_record_payload(rec), ensure_ascii=False, indent=2)}")

    msg = get_client().messages.create(
        model=config.VERIFY_MODEL, max_tokens=1200, system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        v = _parse_json(text)
    except (json.JSONDecodeError, ValueError):
        # If the verifier's own output is unparseable, fail safe: drop the record.
        print(f"[verify] unparseable verdict for: {rec.target} — dropping")
        return None

    if str(v.get("verdict", "")).lower() != "keep":
        print(f"[verify] REJECT {rec.target!r}: {v.get('reject_reason')}")
        return None

    # Apply corrections — only to checkable fields, only as null or verbatim text.
    corrected = v.get("corrected") or {}
    for f, val in corrected.items():
        if f not in _CHECKABLE:
            continue
        setattr(rec, f, val if val != "" else None)

    # A correction may have removed the target -> the record is now invalid.
    if not (rec.target or "").strip():
        print(f"[verify] REJECT: target nulled during verification")
        return None

    # Recompute the sort value from whatever survived verification.
    rec.value_usd_est = parse_money_to_usd(rec.ev_text) or parse_money_to_usd(rec.amount_text)
    rec.verified = True
    rec.verify_reasons = list(v.get("flags") or [])
    return rec


def verify_all(records: list[DealRecord]) -> list[DealRecord]:
    out = []
    print(f"[verify] running verifier lens over {len(records)} extracted records...")
    for i, r in enumerate(records, 1):
        try:
            vr = verify_one(r)
        except Exception as e:
            print(f"[verify] error on {r.target!r}: {e} — dropping")
            continue
        if vr:
            out.append(vr)
    print(f"[verify] {len(out)}/{len(records)} survived verification")
    return out

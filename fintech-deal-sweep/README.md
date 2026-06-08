# Fintech Deal Sweep

A terminal tool that runs a **weekly sweep** for fintech deals **announced in the
last 7 days**, verifies each one against its source, and writes a clean **two-tab
Excel tracker**:

- **Tab 1 — `M&A`**: strategic acquisitions + PE buyouts
- **Tab 2 — `Capital Raises`**: growth equity / venture rounds **above $25M**

It searches across the eight tracked sub-sectors and tries hard to land at least
one deal in each — but it **never invents a deal**. A sector with no real,
verified, in-window deal is reported as a genuine gap and left blank.

---

## Why it doesn't hallucinate

Three independent layers:

1. **Grounded extraction** (`extract.py`) — the model reads **one article** and
   may use *only* that text. Money figures are copied **verbatim or returned
   null** (never derived from revenue/multiples/prior rounds). No named target →
   no record. Every populated field must carry its source sentence.
2. **Verifier lens** (`verify.py`) — a **second, independent, adversarial pass**
   re-checks each extracted record against the original article. It can only
   *reject* the record or *null out / correct* fields to verbatim text — it can
   never add information. Anything ungrounded is dropped.
3. **Deterministic rules** (`process.py`) — announced-only (completed M&A is
   dropped, since it was announced earlier and already ingested), the **$25M raise
   floor**, and dedup. No LLM, fully auditable.

The picker also **doesn't cop out after one pass**: any still-empty sector
triggers additional, progressively broader search rounds (`--gapfill-rounds`,
default 4) before it's ever called a gap. It also **searches a wider net than the
brief** — candidates are pulled from ~14 days (`--search-days`) so a deal
announced late in the week but indexed a day or two later still surfaces — while
the **strict 7-day announcement gate** (`--since-days`) decides what actually
makes the brief. Wider search, same hard rule: it tries harder without ever
loosening the window or inventing a deal to fill a sector.

---

## Setup

```bash
cd fintech-deal-sweep
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then put your ANTHROPIC_API_KEY in .env
```

## Usage

```bash
# See real volume first — feeds only, no LLM, no cost:
python sweep.py --dry-run
python sweep.py --dry-run --deep      # also fire every broaden-tier query

# Full weekly run -> two-tab Excel:
python sweep.py
python sweep.py --out tracker_2026-06-08.xlsx --since-days 7
```

Output defaults to `fintech_deals_<today>.xlsx`. The console prints a summary:
counts per tab, sectors covered, what was dropped by which rule, and any genuine
gaps.

## The eight tracked sub-sectors

`Banking & Lending Tech` · `Corporate Financial Function` ·
`Financial Info & Analytics` · `InsurTech` · `Payments` ·
`Capital Markets Tech` · `Real Estate & Mortgage Tech` · `Asset & Wealth Tech`

## Output columns (match the precedent sheet exactly)

**M&A:** Sector · Target Country · Deal Type · Date · Target · Acquirer ·
EV ($M) · EV / Revenue · EV / EBITDA · Target Description · Link / Press Release

**Capital Raises:** Sector · Target Country · Date · Target · Lead Investor(s) ·
Amount ($M) · Valuation ($M) · EV / Revenue · EV / EBITDA · Target Description ·
Link / Press Release

Undisclosed cells (often Valuation, EV/Revenue, EV/EBITDA) are left **blank** —
never guessed.

## Tuning

Everything lives in `config.py` (sector taxonomy, search queries, keyword gates,
scoring weights, models) and is overridable via env vars — see `.env.example`.

## Run it weekly

`cron` example (every Monday 07:00):

```cron
0 7 * * 1 cd /path/to/fintech-deal-sweep && /path/to/.venv/bin/python sweep.py --out "weekly_$(date +\%F).xlsx" >> sweep.log 2>&1
```

## Files

| file | role |
|------|------|
| `sweep.py` | CLI entry point + gap-fill orchestration |
| `config.py` | sectors, feeds, gates, weights, models |
| `ingest.py` | RSS pull, link resolve, body fetch, gate, title-dedup |
| `extract.py` | grounded single-article extraction (anti-hallucination layer 1) |
| `verify.py` | independent adversarial verifier lens (layer 2) |
| `process.py` | dedup, eligibility rules, scoring, cross-sector selection |
| `exporter.py` | two-tab styled Excel writer |
| `models.py` | data model + verbatim-safe money/date helpers |

## Notes / limits

- Source is free Google-News RSS, so coverage depends on what's indexed and
  public; a quiet week genuinely yields fewer deals (the tool will say so rather
  than pad the list).
- Currency is not FX-converted for the $25M gate — a magnitude in €/£ is taken at
  face value, which is close enough for eligibility.
- The tool is intentionally stateless. If you want week-over-week de-dup so a deal
  never reappears, persist the selected `(target, deal_type)` keys and filter them
  on the next run.

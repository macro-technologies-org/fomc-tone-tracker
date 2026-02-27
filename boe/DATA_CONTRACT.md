# BoE MPC Tracker — Data Contract

> For Andreas: this documents the `corpus.json` shape so the monorepo can consume it.
> Mirrors the FOMC corpus contract. Same composite formula, same score ranges.

---

## corpus.json Schema

```jsonc
{
  "pill": [
    {
      "date": "2026-02-05",                    // YYYY-MM-DD
      "title": "MPC Minutes Vote Rationale — Huw Pill — 2026-02-05",
      "venue": "MPC Meeting",
      "url": "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/february-2026",
      "text": "First 800 chars of rationale...",
      "score": 10,                              // composite: -100 (dovish) to +100 (hawkish)
      "stance": 12,                             // policy restrictiveness (-100 to +100)
      "balance": 8,                             // inflation vs employment emphasis (-100 to +100)
      "direction": 10,                          // rate path signal (-100 to +100)
      "reason": "Pill sees underlying inflation settling at 2.5%, above target...",
      "keywords": [{"word": "persistence", "type": "hawk"}],
      "model": "claude-sonnet-4-5",
      "source": "mpc_minutes",                  // mpc_minutes | boe_speech | boe_speech_list | tsc_testimony
      "type": "minutes_rationale",              // minutes_rationale | minutes_general | speech | testimony
      "vote": "hold",                           // hold | cut | "" (speeches have no vote)
      "url_hash": "a1b2c3d4e5f6",
      "scraped_at": "2026-02-27T07:30:00Z"
    }
  ]
}
```

## Members (9, all vote every meeting)

| Key | Name | Role | Type |
|-----|------|------|------|
| `bailey` | Andrew Bailey | Governor | Internal |
| `lombardelli` | Clare Lombardelli | DG Monetary Policy | Internal |
| `breeden` | Sarah Breeden | DG Financial Stability | Internal |
| `ramsden` | Dave Ramsden | DG Markets & Banking | Internal |
| `pill` | Huw Pill | Chief Economist | Internal |
| `mann` | Catherine Mann | External Member | External |
| `dhingra` | Swati Dhingra | External Member | External |
| `greene` | Megan Greene | External Member (term ends Jul 2026) | External |
| `taylor` | Alan Taylor | External Member | External |

Former members (appear in historical data): `broadbent`, `haskel`

## Composite Formula

Same as FOMC: `round(0.30 * stance + 0.35 * balance + 0.35 * direction)`

## Scoring Context

- Bank Rate: 3.75% (Feb 2026)
- Neutral rate: ~3.25% (market-implied, MPC doesn't publish explicit)
- Policy gap: +50bp above neutral
- UK CPI: 3.4% (Dec 2025), target 2%

## Extra Fields vs FOMC

| Field | FOMC | BoE | Notes |
|-------|------|-----|-------|
| `type` | Not present | `minutes_rationale`, `speech`, `testimony` | BoE has richer source types |
| `vote` | Not present | `hold`, `cut`, `""` | From MPC minutes — ground truth |
| `source` | `fed_board`, `ny_fed`, etc. | `mpc_minutes`, `boe_speech`, `tsc_testimony` | Different source enum |

## Breaking Changes (ping Andreas first)

Same rules as FOMC corpus:
- Adding, removing, or renaming fields
- Changing score ranges
- Changing member key names
- Changing `source` or `type` enum values

## Non-Breaking (go ahead)

- Adding new speeches/rationales to existing members
- Changing `reason` text, `keywords`, `model` version
- Adding new MPC meeting URLs to the scraper

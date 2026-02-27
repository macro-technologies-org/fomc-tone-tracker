# Deployment Guide — FOMC + BoE Trackers
## Macro Technologies · Jaime's Modules

---

## Org Structure After Deployment

```
macro-technologies-org/
│
├── fomc-tone-tracker/                  ← EXISTING repo (update in place)
│   ├── index.html                      ← 9k-line SPA (unchanged)
│   ├── corpus.json                     ← Root copy (GitHub Pages + subtree)
│   ├── scraper/
│   │   ├── scraper.py                  ← UPDATED: audited v2 + --backfill flag
│   │   ├── corpus.json                 ← Source of truth
│   │   ├── corpus_supplement.json      ← Manual fallback entries
│   │   └── requirements.txt            ← requests, beautifulsoup4, anthropic, lxml
│   └── .github/workflows/
│       └── daily-scrape.yml            ← UPDATED: backfill support added
│
├── boe-mpc-tracker/                    ← NEW repo (same pattern as FOMC)
│   ├── index.html                      ← TBD: BoE SPA (future build)
│   ├── corpus.json                     ← Root copy
│   ├── DATA_CONTRACT.md                ← Schema doc for Andreas
│   ├── scraper/
│   │   ├── scraper.py                  ← BoE scraper: 4 sources + minutes parser
│   │   ├── corpus.json                 ← Source of truth
│   │   ├── corpus_supplement.json      ← Manual fallback entries (create empty)
│   │   └── requirements.txt            ← Same deps as FOMC
│   └── .github/workflows/
│       └── daily-scrape.yml            ← Daily cron @ 07:00 UTC
│
└── macro-technologies-monorepo/        ← ANDREAS'S REPO (no changes needed yet)
    └── ...                             ← Will subtree-pull boe-mpc-tracker when ready
```

---

## Step 1: Update FOMC Repo (fomc-tone-tracker)

These are drop-in replacements. No breaking changes to corpus.json schema.

```bash
cd fomc-tone-tracker

# Replace scraper (audited v2 + --backfill flag)
cp scraper.py scraper/scraper.py

# Replace workflow (adds backfill input)
cp daily-scrape.yml .github/workflows/daily-scrape.yml

# Verify
cd scraper && python scraper.py --dry-run --lookback 3
# Should list recent speeches without scoring

git add -A
git commit -m "Scraper v2: audit fixes + --backfill flag"
git push
```

### What Changed in scraper.py (vs current deployed version)

| Fix | Severity | Detail |
|-----|----------|--------|
| FFR updated to 3.50-3.75% | CRITICAL | Was stale at 4.25-4.50% |
| RSS `<link>` extraction rewritten | CRITICAL | BS4 xml mode was silently failing |
| corpus.json writes to repo root | CRITICAL | GitHub Pages wasn't seeing updates |
| MEMBER_MAP expanded | HIGH | +paulson, +thomas barkin, +alberto g. musalem |
| Smart text extraction | HIGH | Policy keyword window instead of first-N-chars |
| LOOKBACK_DAYS no longer mutated | HIGH | Was causing race conditions |
| Tighter SPEECH/SKIP patterns | HIGH | Fewer false positives |
| O(1) dedup via prebuilt set | MEDIUM | Was O(n) per speech |
| save outside loop + try/finally | MEDIUM | Crash-safe corpus writes |
| Retry backoff capped at 30s | MEDIUM | Was exponential unbounded |
| Entry schema validation | LOW | Catches malformed entries before save |
| --dry-run mode | LOW | Test scraping without API calls |
| --backfill flag | LOW | 365-day lookback for full corpus rebuild |

### What Did NOT Change

- `corpus.json` filename (still `corpus.json`)
- `scraper/scraper.py` filename
- Corpus schema (all fields identical)
- Score ranges (-100 to +100 components, -50 to +50 practical composite)
- Composite formula (0.30 × stance + 0.35 × balance + 0.35 × direction)
- Member key names
- GitHub Actions secret name (`ANTHROPIC_API_KEY`)

---

## Step 2: Create BoE Repo (boe-mpc-tracker)

```bash
# Create new repo in the org
# Go to github.com/macro-technologies-org → New repository
# Name: boe-mpc-tracker
# Private, no template, no README

# Clone and populate
git clone git@github.com:macro-technologies-org/boe-mpc-tracker.git
cd boe-mpc-tracker

# Copy files from this package
mkdir -p scraper .github/workflows
cp scraper.py scraper/scraper.py
cp daily-scrape.yml .github/workflows/daily-scrape.yml
cp DATA_CONTRACT.md .

# Create requirements.txt
echo "requests>=2.31
beautifulsoup4>=4.12
lxml>=5.1
anthropic>=0.40" > scraper/requirements.txt

# Create empty supplement file
echo "{}" > scraper/corpus_supplement.json

# Create empty corpus (scraper will populate)
echo "{}" > corpus.json
echo "{}" > scraper/corpus.json

# Initial commit
git add -A
git commit -m "BoE MPC Tracker: scraper, workflow, data contract"
git push

# Add secret
# Settings → Secrets → Actions → ANTHROPIC_API_KEY
```

### Initial Backfill

```bash
cd scraper
pip install requests beautifulsoup4 lxml anthropic

# Dry run first — verify all 4 sources connect
ANTHROPIC_API_KEY=sk-ant-... python scraper.py --backfill --dry-run

# Full backfill — scores all speeches + 13 sets of minutes
# Expect ~150+ entries, ~$2-4 in Claude API costs
ANTHROPIC_API_KEY=sk-ant-... python scraper.py --backfill
```

### Or via GitHub Actions

Go to Actions → BoE MPC Scraper → Run workflow → set `backfill: true`

---

## Step 3: Tell Andreas

Once both repos are live and corpus.json is populating:

> "BoE tracker is live at `macro-technologies-org/boe-mpc-tracker`.
> Same pattern as FOMC — corpus.json at root, daily scraper via Actions.
> Data contract in DATA_CONTRACT.md. Two extra fields vs FOMC: `type` and `vote`.
> Ready for subtree pull whenever you want to add a BoE nav item."

Andreas will:
1. Add subtree: `git subtree add --prefix=boe-mpc-tracker git@github.com:macro-technologies-org/boe-mpc-tracker.git main --squash`
2. Wire up `boe-tone/page.tsx` in the dashboard
3. Add BoE tools to the AI agent (tool_executor.py)

---

## BoE Scraper: 4 Data Sources

| # | Source | URL | Entries/Run | Unique Feature |
|---|--------|-----|-------------|----------------|
| 1 | BoE Speeches RSS | `bankofengland.co.uk/rss/speeches` | 0-3/day | Primary speech source |
| 2 | BoE Speech Listing | `bankofengland.co.uk/news/speeches` | Backup | HTML fallback for RSS misses |
| 3 | MPC Minutes | `bankofengland.co.uk/monetary-policy-summary-and-minutes/...` | 9/meeting | **Per-member vote rationales** — highest signal |
| 4 | TSC Testimony | `committees.parliament.uk/...` | 0-2/quarter | Quarterly grilling by MPs |

### Minutes Parser — The Key Differentiator

BoE minutes publish named, per-member vote rationales (200-400 words each). The parser:
1. Identifies hold vs cut voters from the vote breakdown paragraph
2. Extracts "Member Name: rationale..." blocks
3. Creates separate corpus entry per member with `type="minutes_rationale"` and `vote="hold"/"cut"`
4. 13 meetings × up to 9 members = **~117 scored entries** from minutes alone

---

## Post-Deployment: Policy Parameter Updates

### After each FOMC decision — update `fomc-tone-tracker/scraper/scraper.py`:

```python
# Lines 56-59
FFR_RANGE     = "3.50-3.75%"     # ← new range
FFR_MIDPOINT  = 3.625            # ← new midpoint
NEUTRAL_RATE  = 3.0              # ← update if SEP median changes
POLICY_GAP_BP = round((FFR_MIDPOINT - NEUTRAL_RATE) * 100)
```

### After each MPC decision — update `boe-mpc-tracker/scraper/scraper.py`:

```python
# Lines 55-63
BANK_RATE      = "3.75%"         # ← new rate
BR_MID         = 3.75
NEUTRAL_RATE   = 3.25
POLICY_GAP_BP  = round((BR_MID - NEUTRAL_RATE) * 100)
LAST_VOTE      = "5-4 hold"     # ← new vote split
LAST_DECISION  = "2026-02-05"   # ← new date
NEXT_MEETING   = "2026-03-19"   # ← next meeting
CPI_LATEST     = "3.4%"         # ← latest CPI

# Also add new minutes URL to MPC_MINUTES_URLS list:
("2026-03-19", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/march-2026"),
```

---

## File Manifest — This Package

```
DEPLOY_README.md                    ← This file

fomc-tone-tracker/                  ← Drop into existing repo
├── scraper/scraper.py              ← Audited v2 (570 lines)
└── .github/workflows/
    └── daily-scrape.yml            ← Updated with backfill (74 lines)

boe-mpc-tracker/                    ← New repo
├── scraper/scraper.py              ← BoE scraper (913 lines)
├── .github/workflows/
│   └── daily-scrape.yml            ← Daily cron (74 lines)
└── DATA_CONTRACT.md                ← Schema for Andreas (80 lines)
```

---

*Macro Technologies · Feb 27, 2026*

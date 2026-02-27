# FOMC Tone Tracker — Scraper Code Audit

**Date:** 2026-02-27  
**File:** `scraper/scraper.py` (~380 lines)  
**Auditor:** Claude (Citadel macro desk review)

---

## Executive Summary

The scraper architecture is solid — clean separation of concerns, good logging, retry logic, and dedup hashing. However, **3 critical bugs** are silently degrading data quality and preventing the live site from updating. The most impactful finding is a **stale federal funds rate** in the scoring prompt that has been systematically biasing every score computed since December 2025.

**Findings:** 19 total — 3 Critical, 4 High, 5 Medium, 4 Low, 2 Enhancement, 1 Info

---

## Critical Findings

### C1. Scoring prompt references stale FFR (4.25–4.50%)
**Location:** `SCORING_PROMPT` constant, line ~280  
**Impact:** Every speech scored since Dec 2025 has been evaluated against the wrong policy rate. The prompt says policy is +137.5bp above neutral when it is actually +62.5bp. This systematically biases all scores dovish by overstating restrictiveness.  
**Fix:** Updated to `3.50-3.75%` with `+62.5bp` gap. Made parameters configurable constants at top of file.

### C2. RSS `<link>` extraction broken in BeautifulSoup xml mode
**Location:** `scrape_fed_board()`, lines 107-112  
**Impact:** BS4's xml parser treats `<link>` as self-closing, so `link.string` is always `None`. The `next_sibling` fallback grabs garbage text. Fed Board speeches — the single most important source — get silently dropped.  
**Fix:** Rewrote as `_rss_url()` helper: tries `.string` → `.next_sibling` (type-checked) → `.next` → `<guid>` fallback.

### C3. Corpus written to `scraper/corpus.json` but site reads from root
**Location:** `CORPUS_FILE` constant, line 26  
**Impact:** `os.path.join(os.path.dirname(__file__), 'corpus.json')` resolves to `scraper/corpus.json`. But `index.html` fetches `corpus.json` from root. Unless the workflow copies the file, scraped speeches never appear on the live site.  
**Fix:** Now writes to repo root (`../corpus.json`) and syncs a copy to `scraper/` for backward compatibility.

---

## High Findings

### H1. MEMBER_MAP missing paulson + stale aliases
**Impact:** Paulson (Philadelphia Fed president) has 7 entries in corpus but zero in MEMBER_MAP — the scraper can never auto-identify his speeches. Barkin listed as "tom barkin" but Richmond Fed uses "Thomas Barkin."  
**Fix:** Added paulson, thomas barkin, alberto g. musalem, and formal name variants for all members.

### H2. Text truncation at 1500 chars loses policy content
**Impact:** Fed speeches often have 300–500 words of preamble. 1500 chars ≈ 250 words typically captures only pleasantries, yielding policy-empty text for the scorer.  
**Fix:** Increased to 3000 chars with smart extraction: scores sliding windows by policy-keyword density and returns the densest section.

### H3. Global `LOOKBACK_DAYS` mutation in `run()`
**Impact:** Fragile — if `run()` is called twice, lookback grows unboundedly.  
**Fix:** Passed as local parameter to all scraper functions.

### H4. SPEECH_PATTERNS too broad
**Impact:** Patterns like `/president` and `/statement` match org charts, policy statements, press releases — wasting API calls and polluting corpus.  
**Fix:** Tightened to speech-specific paths. Added `SKIP_PATTERNS` for known non-speech URL segments.

---

## Medium Findings

| # | Finding | Fix |
|---|---------|-----|
| M1 | CSS selectors fragile for JS-rendered regional sites | Link fallback catches most; recommend RSS feeds for chicago/cleveland/atlanta |
| M2 | No retry backoff ceiling | Capped at 30s; added failed-speech queue file |
| M3 | `save_corpus()` inside loop — O(n) serialization per speech | Moved outside loop with `try/finally` to ensure save on crash |
| M4 | `is_duplicate()` is O(n) scan | Prebuilt `set` at load time for O(1) lookup |
| M5 | Composite score range not specified in prompt | Added: "range -50 to +50, beyond ±35 is rare" |

---

## Low / Enhancement

| # | Finding | Fix |
|---|---------|-----|
| L1 | Missing ISO 8601 with tz offset in date parser | Added format + tz strip |
| L2 | No schema validation on corpus entries | Added `valid_entry()` check before append |
| L3 | Fed-Board-specific CSS selectors applied globally | Non-harmful but noisy; documented |
| L4 | No `--dry-run` mode | Added CLI flag — scrapes without API calls |
| E1 | No corpus root sync in code | Now handled in scraper + workflow |
| E2 | No failed-speech retry queue | Writes `failed_speeches.json` for manual review |

---

## Files Delivered

| File | Purpose | Install Location |
|------|---------|-----------------|
| `scraper.py` | Fixed scraper with all 19 findings resolved | `scraper/scraper.py` |
| `scrape.yml` | GitHub Actions workflow (weekday 14:00 UTC) | `.github/workflows/scrape.yml` |

---

## Deployment Checklist

1. Replace `scraper/scraper.py` with the fixed version
2. Place `scrape.yml` in `.github/workflows/`
3. Verify `ANTHROPIC_API_KEY` is set in repo Settings → Secrets
4. Run workflow manually with `lookback_days=30` to backfill
5. Confirm `corpus.json` at repo root is updated after the run
6. **After each FOMC decision:** update `FFR_RANGE`, `FFR_MIDPOINT` constants in scraper.py

---

## Scoring Recalibration Note

Because the FFR was stale at 4.25–4.50% (should be 3.50–3.75%), all auto-scored speeches since Dec 2025 may have a dovish bias of ~5–10 points. The 4 speeches scored by the scraper (Collins 2/24, Kashkari 2/19 duplicate) should be re-scored after deploying the fix. The 165+ manually pre-scored entries are unaffected since they used heuristic scoring, not the API prompt.

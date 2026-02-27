"""
BoE MPC Speech & Minutes Scraper
Runs daily via GitHub Actions. Fetches new speeches + MPC minutes,
scores with Claude, appends to corpus.json which feeds the
BoE MPC Tone Tracker dashboard.

v1.0 — 2026-02-27
─────────────────
Sources:
  [1] BoE Speeches RSS  — bankofengland.co.uk/rss/speeches
  [2] BoE Speech Listing — bankofengland.co.uk/news/speeches
  [3] MPC Minutes        — bankofengland.co.uk/monetary-policy-summary-and-minutes/
  [4] TSC Testimony      — committees.parliament.uk (MPC members)

Architecture mirrors FOMC scraper (scraper.py) for unified CI/CD.
"""

import os, re, json, time, logging, hashlib, sys, shutil, argparse
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from typing import Optional
import requests
from bs4 import BeautifulSoup

# ── LOGGING ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── CONFIG ─────────────────────────────────────────────────
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
SCORE_MODEL      = "claude-sonnet-4-5"
DEFAULT_LOOKBACK = int(os.getenv("LOOKBACK_DAYS", "7"))

REPO_ROOT       = Path(__file__).resolve().parent.parent
CORPUS_ROOT     = REPO_ROOT / "corpus.json"
CORPUS_SCRAPER  = Path(__file__).resolve().parent / "corpus.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.5",
}

# ── POLICY PARAMETERS — update after each MPC decision ────
BANK_RATE      = "3.75%"
BR_MID         = 3.75
NEUTRAL_RATE   = 3.25     # market-implied (MPC doesn't publish explicit neutral)
POLICY_GAP_BP  = round((BR_MID - NEUTRAL_RATE) * 100)
LAST_VOTE      = "5-4 hold"
LAST_DECISION  = "2026-02-05"
NEXT_MEETING   = "2026-03-19"
CPI_LATEST     = "3.4%"   # Dec 2025

# ══════════════════════════════════════════════════════════════
# MPC MEMBER MAP — 9 voting members
# Unlike FOMC's 19/12 structure, ALL 9 vote at EVERY meeting
# ══════════════════════════════════════════════════════════════
MEMBER_MAP = {
    "bailey":      ["andrew bailey", "bailey", "governor bailey", "the governor"],
    "lombardelli": ["clare lombardelli", "lombardelli", "deputy governor for monetary policy"],
    "breeden":     ["sarah breeden", "breeden", "deputy governor for financial stability"],
    "ramsden":     ["dave ramsden", "ramsden", "sir dave ramsden",
                    "deputy governor for markets and banking",
                    "deputy governor, markets and banking"],
    "pill":        ["huw pill", "pill", "chief economist",
                    "executive director, monetary analysis"],
    "mann":        ["catherine mann", "catherine l mann", "catherine l. mann",
                    "dr catherine mann", "dr mann", "mann"],
    "dhingra":     ["swati dhingra", "dhingra", "dr swati dhingra", "dr dhingra"],
    "greene":      ["megan greene", "greene"],
    "taylor":      ["alan taylor", "taylor", "professor alan taylor", "prof taylor",
                    "professor taylor"],
}

# Former members who appear in older minutes (for historical scraping)
FORMER_MEMBERS = {
    "broadbent":   ["ben broadbent", "broadbent", "deputy governor broadbent"],
    "haskel":      ["jonathan haskel", "haskel", "professor haskel", "prof haskel"],
}

def match_member(text: str) -> Optional[str]:
    """Match a text snippet to an MPC member ID."""
    t = text.lower()
    for mid, names in MEMBER_MAP.items():
        if any(n in t for n in names):
            return mid
    # Check former members too (for historical data)
    for mid, names in FORMER_MEMBERS.items():
        if any(n in t for n in names):
            return mid
    return None

# ══════════════════════════════════════════════════════════════
# DATE PARSER
# ══════════════════════════════════════════════════════════════
DATE_FMTS = [
    "%d %B %Y",     # UK format: 5 February 2026
    "%d %b %Y",     # 5 Feb 2026
    "%B %d, %Y",    # US format: February 5, 2026
    "%b %d, %Y",    # Feb 5, 2026
    "%Y-%m-%d",     # ISO
    "%d/%m/%Y",     # UK date: 05/02/2026
    "%B %Y",        # February 2026
    "%Y-%m-%dT%H:%M:%S",
]

def parse_date(text: str) -> Optional[date]:
    if not text:
        return None
    text = re.sub(r'\s+', ' ', text.strip())
    text = re.sub(r'^\w{3,9},\s*', '', text)           # strip "Thursday, "
    text = re.sub(r'\s+\d{2}:\d{2}(:\d{2})?.*$', '', text)
    text = re.sub(r'(st|nd|rd|th)\s', ' ', text)       # 5th → 5
    text = re.sub(r'[+-]\d{2}:\d{2}$', '', text)
    for fmt in DATE_FMTS:
        try:
            return datetime.strptime(text[:30], fmt).date()
        except ValueError:
            pass
    # Fallback regex
    m = re.search(r'(\d{1,2}\s+\w+\s+\d{4})', text)
    if m:
        return parse_date(m.group(1))
    m = re.search(r'(\w+\s+\d{1,2},?\s+\d{4})', text)
    if m:
        return parse_date(m.group(1))
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None

# ══════════════════════════════════════════════════════════════
# SMART TEXT EXTRACTION — BoE-adapted keyword window
# ══════════════════════════════════════════════════════════════
POLICY_KW = [
    "inflation", "labour market", "labor market", "employment",
    "bank rate", "interest rate", "restrictive", "neutral",
    "mandate", "cut", "hike", "hold", "target", "percent",
    "monetary policy", "price stability", "economy", "growth",
    "disinflation", "tightening", "easing", "mpc", "persistence",
    "services inflation", "wage growth", "pay growth", "slack",
    "output gap", "gdp", "cpi", "pce", "demand", "supply",
    "uncertainty", "tariff", "fiscal", "budget", "sterling",
    "quantitative tightening", "gilt", "sonia",
]

# BoE speech pages have a consistent structure
BOE_TEXT_SELECTORS = [
    "div.page-content",
    "div[class*='article']",
    "div[class*='speech']",
    "div#content",
    "article",
    "main",
    "div.col-sm-8",
]

def _policy_section(full: str, max_chars: int = 3000) -> str:
    """Extract the most policy-dense section of text."""
    if len(full) <= max_chars:
        return full
    best_i, best_s = 0, -1
    fl = full.lower()
    for i in range(0, max(1, len(full) - max_chars), 250):
        chunk = fl[i : i + max_chars]
        s = sum(chunk.count(k) for k in POLICY_KW)
        if s > best_s:
            best_s, best_i = s, i
    return full[best_i : best_i + max_chars].strip()

def fetch_speech_text(url: str) -> str:
    """Fetch and extract policy-relevant text from a BoE speech URL."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # Remove nav/footer clutter
        for tag in soup(["nav", "footer", "header", "script", "style",
                         "aside", "form", "noscript"]):
            tag.decompose()
        # Remove BoE-specific clutter
        for sel in ["div.cookie-banner", "div.related-links",
                    "div.footnotes", "div.breadcrumb", "ul.pagination"]:
            for el in soup.select(sel):
                el.decompose()
        # Try selectors
        for sel in BOE_TEXT_SELECTORS:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 300:
                raw = re.sub(r'\s+', ' ', el.get_text(" ", strip=True)).strip()
                return _policy_section(raw)
        # Fallback to body
        body = soup.find("body")
        if body:
            return _policy_section(
                re.sub(r'\s+', ' ', body.get_text(" ", strip=True)).strip()
            )
    except Exception as e:
        log.warning(f"  Text fetch failed for {url}: {e}")
    return ""

# ══════════════════════════════════════════════════════════════
# SOURCE 1: BOE SPEECHES RSS
# ══════════════════════════════════════════════════════════════
BOE_SPEECHES_RSS = "https://www.bankofengland.co.uk/rss/speeches"
BOE_BASE         = "https://www.bankofengland.co.uk"

# Filter: only MPC-member speeches (skip operational/regulatory)
SKIP_SPEAKERS = [
    "afua kyei", "victoria saporta", "sam woods", "james talbot",
    "rebecca jackson", "sasha mills", "james benford", "laura wallis",
    "gareth sheridan", "nathan sheridan", "geoff sheridan",
]

def scrape_boe_speeches(lookback: int) -> list[dict]:
    """Scrape speeches from BoE RSS feed."""
    speeches = []
    cutoff = date.today() - timedelta(days=lookback)
    try:
        r = requests.get(BOE_SPEECHES_RSS, headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        log.info(f"  BoE Speeches RSS: {len(r.text):,} bytes")

        for item in soup.find_all("item"):
            try:
                title_el = item.find("title")
                if not title_el:
                    continue
                title = title_el.text.strip()

                # Parse date
                pd = item.find("pubDate")
                sd = parse_date(pd.text.strip() if pd else "")
                if not sd or sd < cutoff:
                    continue

                # Get URL
                url = ""
                link_el = item.find("link")
                if link_el:
                    url = (link_el.string or "").strip()
                    if not url:
                        ns = link_el.next_sibling
                        if ns and isinstance(ns, str):
                            url = ns.strip()
                guid_el = item.find("guid")
                if (not url or not url.startswith("http")) and guid_el:
                    url = (guid_el.string or guid_el.text or "").strip()
                if not url.startswith("http"):
                    continue

                # Match member
                desc_el = item.find("description")
                desc = (desc_el.get_text() if desc_el else "") + " " + title
                member_id = match_member(desc)

                # Skip non-MPC speakers
                if not member_id:
                    desc_lower = desc.lower()
                    if any(sk in desc_lower for sk in SKIP_SPEAKERS):
                        continue

                speeches.append(dict(
                    source="boe_speech",
                    member_id=member_id,
                    title=title,
                    date=sd.isoformat(),
                    venue="",
                    url=url,
                    type="speech",
                ))
            except Exception as e:
                log.warning(f"  BoE RSS item error: {e}")
    except Exception as e:
        log.error(f"  BoE Speeches RSS failed: {e}")
    log.info(f"  BoE Speeches: {len(speeches)} found")
    return speeches

# ══════════════════════════════════════════════════════════════
# SOURCE 2: BOE SPEECH LISTING PAGE (fallback / backfill)
# ══════════════════════════════════════════════════════════════
BOE_SPEECH_LIST = "https://www.bankofengland.co.uk/news/speeches"

def scrape_boe_speech_listing(lookback: int) -> list[dict]:
    """Scrape speech listing page as RSS backup."""
    speeches = []
    cutoff = date.today() - timedelta(days=lookback)
    try:
        r = requests.get(BOE_SPEECH_LIST, headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        log.info(f"  BoE Speech List: {len(r.text):,} bytes")

        # BoE speech listing uses a consistent card/list pattern
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "/speech/" not in href.lower():
                continue
            title = a.get_text(strip=True)
            if not title or len(title) < 10:
                continue

            full_url = href if href.startswith("http") else BOE_BASE + href

            # Find date from parent element
            par = a.find_parent(["li", "div", "article"])
            par_text = par.get_text(" ", strip=True) if par else title
            sd = parse_date(par_text)
            if not sd:
                # Try to parse from URL: /speech/2026/february/slug
                m = re.search(r'/speech/(\d{4})/(\w+)/', href)
                if m:
                    try:
                        sd = parse_date(f"1 {m.group(2)} {m.group(1)}")
                    except:
                        pass
            if not sd or sd < cutoff:
                continue

            member_id = match_member(par_text)
            speeches.append(dict(
                source="boe_speech_list",
                member_id=member_id,
                title=title[:200],
                date=sd.isoformat(),
                venue="",
                url=full_url,
                type="speech",
            ))
    except Exception as e:
        log.error(f"  BoE Speech List failed: {e}")
    log.info(f"  BoE Speech List: {len(speeches)} found")
    return speeches

# ══════════════════════════════════════════════════════════════
# SOURCE 3: MPC MINUTES — Individual Vote Rationales
#
# THIS IS THE MOST VALUABLE SOURCE. BoE minutes contain
# named, per-member vote rationales (paragraphs 16-20+).
# Each yields a separate scored corpus entry.
#
# URL pattern: bankofengland.co.uk/monetary-policy-summary-and-minutes/{year}/{month}-{year}
# ══════════════════════════════════════════════════════════════

# Known MPC meeting dates + URLs for the current easing cycle
MPC_MINUTES_URLS = [
    ("2024-08-01", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2024/august-2024"),
    ("2024-09-19", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2024/september-2024"),
    ("2024-11-07", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2024/november-2024"),
    ("2024-12-19", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2024/december-2024"),
    ("2025-02-06", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2025/february-2025"),
    ("2025-03-20", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2025/march-2025"),
    ("2025-05-08", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2025/may-2025"),
    ("2025-06-19", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2025/june-2025"),
    ("2025-08-07", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2025/august-2025"),
    ("2025-09-18", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2025/september-2025"),
    ("2025-11-06", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2025/november-2025"),
    ("2025-12-18", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2025/december-2025"),
    ("2026-02-05", "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/february-2026"),
]

def _extract_vote_rationales(text: str, meeting_date: str) -> list[dict]:
    """
    Parse individual member vote rationales from MPC minutes text.

    BoE minutes structure (post-2024):
    - Paragraphs 16-20+ contain individual vote rationales
    - Format: "Member Name: rationale text..."
    - Members listed alphabetically within each vote group
    - Groups: "voted in favour" (hold) vs "voted against" (cut)

    Returns list of per-member dicts ready for scoring.
    """
    rationales = []
    text_lower = text.lower()

    # First, find the vote breakdown
    # Pattern: "X members (Name1, Name2, ...) voted in favour"
    hold_members = []
    cut_members = []

    # Hold voters
    hold_match = re.search(
        r'(?:five|four|three|six|seven|eight|nine)\s+members?\s*\(([^)]+)\)\s*'
        r'(?:voted in favour|preferred to maintain)',
        text, re.IGNORECASE
    )
    if hold_match:
        hold_members = [n.strip() for n in hold_match.group(1).split(",")]

    # Cut voters
    cut_match = re.search(
        r'(?:five|four|three|six|seven|eight|nine|one|two)\s+members?\s*\(([^)]+)\)\s*'
        r'(?:voted against|preferred to reduce|preferring)',
        text, re.IGNORECASE
    )
    if cut_match:
        cut_members = [n.strip() for n in cut_match.group(1).split(",")]

    log.info(f"  Minutes {meeting_date}: {len(hold_members)} hold, {len(cut_members)} cut")

    # Now extract individual rationales
    # Pattern: "Member Name: Their rationale paragraph..."
    # BoE uses format like "Andrew Bailey: My policy decision..."
    # or "Huw Pill: I do not see a need..."
    member_pattern = re.compile(
        r'(?:^|\n\s*)'
        r'(Andrew Bailey|Clare Lombardelli|Sarah Breeden|Dave Ramsden|'
        r'Huw Pill|Catherine L\.? Mann|Swati Dhingra|Megan Greene|'
        r'Alan Taylor|Ben Broadbent|Jonathan Haskel)'
        r'\s*:\s*(.+?)(?=\n\s*(?:Andrew Bailey|Clare Lombardelli|Sarah Breeden|'
        r'Dave Ramsden|Huw Pill|Catherine|Swati Dhingra|Megan Greene|'
        r'Alan Taylor|Ben Broadbent|Jonathan Haskel)\s*:|$)',
        re.DOTALL | re.IGNORECASE
    )

    for match in member_pattern.finditer(text):
        name = match.group(1).strip()
        rationale = match.group(2).strip()
        # Clean up
        rationale = re.sub(r'\s+', ' ', rationale)
        if len(rationale) < 30:
            continue

        member_id = match_member(name)
        if not member_id:
            continue

        # Determine vote
        vote = "unknown"
        for hm in hold_members:
            if name.lower() in hm.lower() or hm.lower() in name.lower():
                vote = "hold"
                break
        for cm in cut_members:
            if name.lower() in cm.lower() or cm.lower() in name.lower():
                vote = "cut"
                break

        rationales.append(dict(
            member_id=member_id,
            name=name,
            text=rationale[:2000],
            vote=vote,
            date=meeting_date,
        ))
        log.info(f"    {name} ({member_id}) → {vote} | {len(rationale)} chars")

    return rationales

def scrape_mpc_minutes(lookback: int) -> list[dict]:
    """
    Fetch MPC minutes and extract per-member vote rationales.
    Each member's rationale becomes a separate corpus entry.
    """
    speeches = []
    cutoff = date.today() - timedelta(days=lookback)

    for meeting_date, url in MPC_MINUTES_URLS:
        md = date.fromisoformat(meeting_date)
        if md < cutoff:
            continue

        log.info(f"  Fetching minutes: {meeting_date}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # Remove clutter
            for tag in soup(["nav", "footer", "header", "script", "style", "aside"]):
                tag.decompose()

            # Get full text
            content = soup.select_one("div.page-content") or soup.select_one("article") or soup.find("body")
            if not content:
                log.warning(f"    No content found for {meeting_date}")
                continue

            full_text = content.get_text("\n", strip=True)
            log.info(f"    {len(full_text):,} chars")

            # Extract individual rationales
            rationales = _extract_vote_rationales(full_text, meeting_date)

            # Also create a full-minutes entry for composite scoring
            policy_text = _policy_section(full_text, 3000)

            for rat in rationales:
                speeches.append(dict(
                    source="mpc_minutes",
                    member_id=rat["member_id"],
                    title=f"MPC Minutes Vote Rationale — {rat['name']} — {meeting_date}",
                    date=meeting_date,
                    venue="MPC Meeting",
                    url=url,
                    type="minutes_rationale",
                    vote=rat["vote"],
                    raw_text=rat["text"],
                ))

            # Also add a general minutes entry (for non-attributed sections)
            if len(policy_text) > 200:
                speeches.append(dict(
                    source="mpc_minutes",
                    member_id=None,
                    title=f"MPC Minutes — General Discussion — {meeting_date}",
                    date=meeting_date,
                    venue="MPC Meeting",
                    url=url + "#general",
                    type="minutes_general",
                    raw_text=policy_text,
                ))

            time.sleep(2)  # Be polite to BoE servers

        except Exception as e:
            log.error(f"    Minutes fetch failed {meeting_date}: {e}")

    log.info(f"  MPC Minutes: {len(speeches)} entries extracted")
    return speeches

# ══════════════════════════════════════════════════════════════
# SOURCE 4: TREASURY SELECT COMMITTEE TESTIMONY
# ══════════════════════════════════════════════════════════════
TSC_BASE = "https://committees.parliament.uk"
TSC_MPC_URL = "https://committees.parliament.uk/work/68/bank-of-england-monetary-policy-reports/"

def scrape_tsc_testimony(lookback: int) -> list[dict]:
    """Scrape TSC MPC hearings for member testimony."""
    speeches = []
    cutoff = date.today() - timedelta(days=lookback)
    try:
        r = requests.get(TSC_MPC_URL, headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        log.info(f"  TSC page: {len(r.text):,} bytes")

        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "oral-evidence" not in href.lower():
                continue
            title = a.get_text(strip=True)
            if not title or len(title) < 10:
                continue

            full_url = href if href.startswith("http") else TSC_BASE + href

            # Date from parent
            par = a.find_parent(["li", "div", "tr"])
            par_text = par.get_text(" ", strip=True) if par else title
            sd = parse_date(par_text)
            if not sd or sd < cutoff:
                continue

            member_id = match_member(par_text)
            speeches.append(dict(
                source="tsc_testimony",
                member_id=member_id,
                title=f"TSC Testimony — {title[:100]}",
                date=sd.isoformat(),
                venue="Treasury Select Committee",
                url=full_url,
                type="testimony",
            ))
    except Exception as e:
        log.error(f"  TSC scrape failed: {e}")
    log.info(f"  TSC: {len(speeches)} found")
    return speeches

# ══════════════════════════════════════════════════════════════
# SCORING — BoE-calibrated prompt
# ══════════════════════════════════════════════════════════════
SCORING_PROMPT = """You are a quantitative Bank of England policy analyst. Score this MPC speech/testimony on three components anchored to the current policy framework.

NEUTRAL RATE FRAMEWORK:
- Estimated neutral rate: {neutral}% (market-implied, MPC does not publish explicit)
- Current Bank Rate: {bank_rate} (midpoint {br_mid}%)
- Policy is +{gap_bp}bps above neutral = modestly restrictive
- Speaker: {member_name}
- Last MPC vote: {last_vote} on {last_decision}
- UK CPI: {cpi_latest} (target: 2%)

SCORE THREE COMPONENTS (-100 to +100, positive = hawkish):

STANCE_SCORE — How does speaker characterize policy restrictiveness?
  "Significantly/substantially restrictive, need to ease" → -60 to -80
  "Too restrictive, further cuts warranted now" → -30 to -50
  "Modestly restrictive, gradual adjustment" → -10 to -25
  "Appropriate / near right level" → 0 to +20
  "Need to retain restrictiveness / not cut further" → +30 to +70

BALANCE_SCORE — Primary risk emphasis?
  Inflation persistence dominates → +40 to +75
  Inflation risks > demand/growth risks → +15 to +40
  Balanced / two-sided → -10 to +15
  Growth/employment concern > inflation → -15 to -40
  Demand weakness / slack dominates → -40 to -75

DIRECTION_SCORE — Rate path signal?
  Explicit hold or "no need to cut further" → +40 to +75
  Patience, "gradual and careful" → +15 to +40
  Data dependent, balanced → -10 to +15
  Lean toward further cuts → -15 to -40
  Explicit cut preference / multiple cuts needed → -40 to -75

BOE-SPECIFIC CALIBRATION:
- "gradual and careful" = hawkish (Pill's signature phrase)
- "persistence" / "second-round effects" = hawkish
- "services inflation" / "wage growth above target" = hawkish
- "insurance" / "precautionary" = dovish
- "loosening labour market" / "slack building" = dovish
- "demand weakness" / "stagnation" = dovish
- "balance of risks" = neutral unless clearly weighted
- "sufficient evidence" / "not yet warranted" = lean hawkish

{vote_context}

COMPOSITE = round(0.30 * stance + 0.35 * balance + 0.35 * direction)
Composite range: -50 to +50. Scores beyond +/-35 are rare extremes.

Extract 3-4 key signal phrases, label each hawk/dove/neutral.
One sentence rationale referencing the neutral rate framework.

Return ONLY valid JSON:
{{"stance":int,"balance":int,"direction":int,"composite":int,"reason":"string","keywords":[{{"word":"string","type":"hawk|dove|neutral"}}]}}

TEXT:
{text}"""

def score_speech(member_id: str, text: str, claude_client, vote: str = ""):
    """Score a speech/rationale using Claude."""
    if not text or len(text) < 50:
        return None
    name = member_id.replace("_", " ").title() if member_id else "Unknown MPC Member"

    vote_ctx = ""
    if vote:
        vote_ctx = f"IMPORTANT: This member voted to {vote.upper()} at this meeting. The rationale text explains their reasoning."

    prompt = SCORING_PROMPT.format(
        neutral=NEUTRAL_RATE, bank_rate=BANK_RATE, br_mid=BR_MID,
        gap_bp=POLICY_GAP_BP, member_name=name,
        last_vote=LAST_VOTE, last_decision=LAST_DECISION,
        cpi_latest=CPI_LATEST,
        vote_context=vote_ctx,
        text=text[:2800],
    )

    for attempt in range(3):
        try:
            if attempt:
                time.sleep(min(2 ** attempt, 30))
            msg = claude_client.messages.create(
                model=SCORE_MODEL, max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = re.sub(
                r"^```json|^```|```$", "",
                msg.content[0].text.strip(), flags=re.MULTILINE,
            ).strip()
            p = json.loads(raw)
            return dict(
                score=int(p.get("composite", 0)),
                stance=int(p.get("stance", 0)),
                balance=int(p.get("balance", 0)),
                direction=int(p.get("direction", 0)),
                reason=str(p.get("reason", "")),
                keywords=p.get("keywords", []),
                model=SCORE_MODEL,
            )
        except Exception as e:
            log.warning(f"  Score attempt {attempt+1}/3: {e}")
    log.error(f"  SCORING FAILED for {name}")
    return None

# ══════════════════════════════════════════════════════════════
# CORPUS MANAGER — same pattern as FOMC scraper
# ══════════════════════════════════════════════════════════════
def load_corpus():
    for p in [CORPUS_ROOT, CORPUS_SCRAPER]:
        if p.exists():
            with open(p) as f:
                return json.load(f)
    return {}

def save_corpus(corpus):
    with open(CORPUS_ROOT, "w") as f:
        json.dump(corpus, f, indent=2)
    try:
        shutil.copy2(str(CORPUS_ROOT), str(CORPUS_SCRAPER))
    except:
        pass

def url_hash(url):
    return hashlib.md5(url.encode()).hexdigest()[:12]

def build_dedup(corpus):
    s = set()
    for speeches in corpus.values():
        for sp in speeches:
            if sp.get("url"):
                s.add(sp["url"])
                s.add(url_hash(sp["url"]))
            if sp.get("url_hash"):
                s.add(sp["url_hash"])
            s.add((sp.get("date", ""), sp.get("title", "")[:30]))
    return s

def is_dup(dedup, url, dt="", title=""):
    return (url in dedup
            or url_hash(url) in dedup
            or (dt and title and (dt, title[:30]) in dedup))

REQ_FIELDS = {"date", "title", "source", "url", "score", "stance", "balance", "direction"}

def valid_entry(e):
    return not (REQ_FIELDS - set(e.keys()))

# ══════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════
def run(dry_run=False, backfill=False):
    log.info("=" * 60)
    log.info(f"BoE MPC Scraper — {datetime.now(timezone.utc).isoformat()}")
    log.info(f"Bank Rate: {BANK_RATE} | Neutral: {NEUTRAL_RATE}% | "
             f"Gap: +{POLICY_GAP_BP}bp | Dry: {dry_run} | Backfill: {backfill}")
    log.info("=" * 60)

    if not dry_run and not ANTHROPIC_KEY:
        log.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)
    cc = None
    if not dry_run:
        import anthropic
        cc = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    corpus = load_corpus()
    log.info(f"Corpus: {sum(len(v) for v in corpus.values())} entries / "
             f"{len(corpus)} members")

    # Supplement merge (manual corrections)
    sup = Path(__file__).resolve().parent / "corpus_supplement.json"
    if sup.exists():
        try:
            data = json.loads(sup.read_text())
            added = 0
            for mid, sps in data.items():
                ex = corpus.get(mid, [])
                keys = {(s["date"], s["title"][:30]) for s in ex}
                urls = {s.get("url", "") for s in ex}
                for s in sps:
                    k = (s["date"], s["title"][:30])
                    if k in keys or (s.get("url") and s["url"] in urls):
                        continue
                    ex.append(s)
                    keys.add(k)
                    added += 1
                corpus[mid] = ex
            if added:
                log.info(f"Supplement: +{added}")
        except Exception as e:
            log.warning(f"Supplement fail: {e}")

    # Compute lookback
    lookback = DEFAULT_LOOKBACK
    if backfill:
        lookback = 730  # 2 years for full backfill
        log.info(f"BACKFILL MODE: lookback={lookback}d")
    else:
        all_dates = [s["date"] for v in corpus.values() for s in v if s.get("date")]
        if all_dates:
            newest = max(all_dates)
            lookback = max(
                (date.today() - date.fromisoformat(newest)).days + 1,
                DEFAULT_LOOKBACK,
            )
            log.info(f"Newest: {newest} → lookback={lookback}d")

    dedup = build_dedup(corpus)
    log.info(f"Dedup keys: {len(dedup)}")

    # ── COLLECT ──
    all_sp = []
    log.info("\n── BoE Speeches (RSS) ──")
    all_sp.extend(scrape_boe_speeches(lookback))
    time.sleep(1)

    log.info("\n── BoE Speeches (Listing) ──")
    all_sp.extend(scrape_boe_speech_listing(lookback))
    time.sleep(1)

    log.info("\n── MPC Minutes (Vote Rationales) ──")
    all_sp.extend(scrape_mpc_minutes(lookback))
    time.sleep(1)

    log.info("\n── TSC Testimony ──")
    all_sp.extend(scrape_tsc_testimony(lookback))
    time.sleep(1)

    log.info(f"\nTotal candidates: {len(all_sp)}")

    # ── SCORE AND STORE ──
    new_n = scored_n = 0
    fails = []
    try:
        for sp in all_sp:
            if is_dup(dedup, sp["url"], sp.get("date", ""), sp.get("title", "")):
                continue

            log.info(f"\n[NEW] {sp['date']} | {sp.get('member_id', '?')} | "
                     f"{sp.get('type', 'speech')} | {sp['title'][:60]}")

            if dry_run:
                log.info(f"  DRY RUN → {sp['url']}")
                new_n += 1
                continue

            # For minutes rationales, use pre-extracted text
            if sp.get("raw_text"):
                text = sp["raw_text"]
            else:
                text = fetch_speech_text(sp["url"])

            if not text or len(text) < 50:
                log.warning("  No text / too short")
                continue

            sc = score_speech(
                sp.get("member_id", ""),
                text, cc,
                vote=sp.get("vote", ""),
            )
            if not sc:
                fails.append(sp)
                continue

            log.info(f"  → {sc['score']:+d} "
                     f"S:{sc['stance']:+d} B:{sc['balance']:+d} D:{sc['direction']:+d}")

            mid = sp.get("member_id") or "mpc_general"
            if mid not in corpus:
                corpus[mid] = []

            entry = dict(
                date=sp["date"],
                title=sp["title"],
                venue=sp.get("venue", ""),
                url=sp["url"],
                url_hash=url_hash(sp["url"]),
                source=sp["source"],
                type=sp.get("type", "speech"),
                vote=sp.get("vote", ""),
                text=text[:800],
                score=sc["score"],
                stance=sc["stance"],
                balance=sc["balance"],
                direction=sc["direction"],
                reason=sc["reason"],
                keywords=sc["keywords"],
                model=sc["model"],
                scraped_at=datetime.now(timezone.utc).isoformat(),
            )
            if valid_entry(entry):
                corpus[mid].append(entry)
                dedup.add(sp["url"])
                dedup.add(url_hash(sp["url"]))
                dedup.add((sp["date"], sp["title"][:30]))
                new_n += 1
                scored_n += 1
            time.sleep(1.5)
    finally:
        if not dry_run and new_n:
            save_corpus(corpus)
            log.info(f"Saved: {sum(len(v) for v in corpus.values())} entries")

    if fails:
        log.warning(f"\n⚠ {len(fails)} failed:")
        for s in fails:
            log.warning(f"  {s['date']} | {s.get('member_id', '?')} | {s['url']}")
        fp = Path(__file__).resolve().parent / "failed_speeches.json"
        with open(fp, "w") as f:
            json.dump(fails, f, indent=2)

    log.info(f"\n{'=' * 60}")
    log.info(f"Done: {new_n} new · {scored_n} scored · {len(fails)} failed")
    log.info(f"Corpus: {sum(len(v) for v in corpus.values())} total / "
             f"{len(corpus)} members")
    log.info("=" * 60)

if __name__ == "__main__":
    pa = argparse.ArgumentParser(description="BoE MPC Speech & Minutes Scraper")
    pa.add_argument("--dry-run", action="store_true",
                    help="Scrape without scoring")
    pa.add_argument("--backfill", action="store_true",
                    help="Full 2-year backfill (Aug 2024 → present)")
    pa.add_argument("--lookback", type=int,
                    help="Override lookback days")
    a = pa.parse_args()
    if a.lookback:
        DEFAULT_LOOKBACK = a.lookback
    run(dry_run=a.dry_run, backfill=a.backfill)

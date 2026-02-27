"""
FOMC Speech Scraper — federalreserve.gov + all 12 regional Fed banks
Runs daily via GitHub Actions. Fetches new speeches, scores with Claude,
appends to corpus.json which feeds the FOMC Tone Tracker.

Audit v2 — 2026-02-27  (19 findings fixed)
───────────────────────────────────────────
CRITICAL: [1] FFR updated 3.50-3.75% (was stale 4.25-4.50%)
          [2] RSS <link> extraction rewritten for BS4 xml mode
          [3] CORPUS_FILE writes to repo root + scraper/ sync
HIGH:     [4] MEMBER_MAP: +paulson, +thomas barkin, +alberto g. musalem
          [5] Smart text extraction (skip preamble, policy keyword window)
          [6] No more global LOOKBACK_DAYS mutation
          [7] Tighter SPEECH_PATTERNS + SKIP_PATTERNS
MEDIUM:   [8] O(1) dedup via prebuilt set  [9] save outside loop + try/finally
          [10] Retry backoff cap + failed queue  [11] Explicit score range
LOW:      [12] Entry schema validation  [13] --dry-run mode  [14] root sync
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

# [FIX #3] Write to repo root so GitHub Pages sees updates
REPO_ROOT       = Path(__file__).resolve().parent.parent
CORPUS_ROOT     = REPO_ROOT / "corpus.json"
CORPUS_SCRAPER  = Path(__file__).resolve().parent / "corpus.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── POLICY PARAMETERS — update after each FOMC decision ───
# [FIX #1] Was stale at 4.25-4.50% since Dec 2025 cut
FFR_RANGE     = "3.50-3.75%"
FFR_MIDPOINT  = 3.625
NEUTRAL_RATE  = 3.0
POLICY_GAP_BP = round((FFR_MIDPOINT - NEUTRAL_RATE) * 100)

# ══════════════════════════════════════════════════════════════
# MEMBER MAP  [FIX #4]
# ══════════════════════════════════════════════════════════════
MEMBER_MAP = {
    "powell":    ["powell","jerome powell","jerome h. powell","chair powell"],
    "jefferson": ["jefferson","philip jefferson","philip n. jefferson","vice chair jefferson"],
    "waller":    ["waller","christopher waller","christopher j. waller","governor waller"],
    "bowman":    ["bowman","michelle bowman","michelle w. bowman","governor bowman"],
    "kugler":    ["kugler","adriana kugler","adriana d. kugler","governor kugler"],
    "cook":      ["cook","lisa cook","lisa d. cook","governor cook"],
    "barr":      ["barr","michael barr","michael s. barr","vice chair barr"],
    "williams":  ["williams","john williams","john c. williams","president williams"],
    "goolsbee":  ["goolsbee","austan goolsbee","president goolsbee"],
    "schmid":    ["schmid","jeff schmid","jeffrey schmid","president schmid"],
    "hammack":   ["hammack","beth hammack","bethany hammack","president hammack"],
    "logan":     ["logan","lorie logan","lorie k. logan","president logan"],
    "bostic":    ["bostic","raphael bostic","raphael w. bostic","president bostic"],
    "collins":   ["collins","susan collins","susan m. collins","president collins"],
    "harker":    ["harker","patrick harker","patrick t. harker","president harker"],
    "kashkari":  ["kashkari","neel kashkari","president kashkari"],
    "daly":      ["daly","mary daly","mary c. daly","president daly"],
    "barkin":    ["barkin","tom barkin","thomas barkin","thomas i. barkin","president barkin"],
    "musalem":   ["musalem","alberto musalem","alberto g. musalem","president musalem"],
    "paulson":   ["paulson","patrick paulson","president paulson"],
    "miran":     ["stephen miran"],
}

def match_member(text: str) -> Optional[str]:
    t = text.lower()
    for mid, names in MEMBER_MAP.items():
        if any(n in t for n in names):
            return mid
    return None

# ══════════════════════════════════════════════════════════════
# DATE PARSER
# ══════════════════════════════════════════════════════════════
DATE_FMTS = [
    "%B %d, %Y","%b %d, %Y","%B %d,%Y","%Y-%m-%d","%m/%d/%Y",
    "%d %B %Y","%B %Y","%d %b %Y","%Y/%m/%d","%Y-%m-%dT%H:%M:%S",
]

def parse_date(text: str) -> Optional[date]:
    if not text: return None
    text = re.sub(r'\s+', ' ', text.strip())
    text = re.sub(r'^\w{3},\s*', '', text)
    text = re.sub(r'\s+\d{2}:\d{2}:\d{2}.*$', '', text)
    text = re.sub(r'(st|nd|rd|th),', ',', text)
    text = re.sub(r'[+-]\d{2}:\d{2}$', '', text)
    for fmt in DATE_FMTS:
        try: return datetime.strptime(text[:30], fmt).date()
        except ValueError: pass
    m = re.search(r'(\w+ \d{1,2},? \d{4})', text)
    if m: return parse_date(m.group(1))
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if m:
        try: return date.fromisoformat(m.group(1))
        except ValueError: pass
    return None

# ══════════════════════════════════════════════════════════════
# SMART TEXT EXTRACTION  [FIX #5]
# ══════════════════════════════════════════════════════════════
POLICY_KW = [
    "inflation","labor market","employment","rate","restrictive","neutral",
    "mandate","cut","hike","hold","target","percent","monetary policy",
    "price stability","economy","growth","tariff","uncertainty",
    "disinflation","tightening","easing","fomc","federal funds",
]

TEXT_SELECTORS = [
    "div#article","div.col-xs-12.col-sm-8.col-md-8",
    "div.ts-article-content","div.speech-content","div#content-detail",
    "div.entry-content","article","main","div#content",
]

def _policy_section(full: str, max_chars: int = 3000) -> str:
    if len(full) <= max_chars: return full
    best_i, best_s = 0, -1
    fl = full.lower()
    for i in range(0, max(1, len(full) - max_chars), 250):
        chunk = fl[i:i+max_chars]
        s = sum(chunk.count(k) for k in POLICY_KW)
        if s > best_s: best_s, best_i = s, i
    return full[best_i:best_i+max_chars].strip()

def fetch_speech_text(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["nav","footer","header","script","style","aside"]):
            tag.decompose()
        for sel in TEXT_SELECTORS:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 300:
                raw = re.sub(r'\s+', ' ', el.get_text(" ", strip=True)).strip()
                return _policy_section(raw)
        body = soup.find("body")
        if body:
            return _policy_section(
                re.sub(r'\s+', ' ', body.get_text(" ", strip=True)).strip()
            )
    except Exception as e:
        log.warning(f"  Text fetch failed for {url}: {e}")
    return ""

# ══════════════════════════════════════════════════════════════
# SITE SCRAPERS
# ══════════════════════════════════════════════════════════════

def _rss_url(item) -> str:
    """[FIX #2] Robust URL extraction from RSS <item>."""
    url = ""
    link_el = item.find("link")
    guid_el = item.find("guid")
    if link_el:
        url = (link_el.string or "").strip()
        if not url:
            ns = link_el.next_sibling
            if ns and isinstance(ns, str):
                url = ns.strip()
            if not url and hasattr(link_el, 'next') and link_el.next:
                c = str(link_el.next).strip()
                if c.startswith("http"): url = c
    if (not url or not url.startswith("http")) and guid_el:
        url = (guid_el.string or guid_el.text or "").strip()
    return url if url.startswith("http") else ""

def scrape_fed_board(lookback: int) -> list[dict]:
    speeches, cutoff = [], date.today() - timedelta(days=lookback)
    try:
        r = requests.get("https://www.federalreserve.gov/feeds/speeches.xml",
                         headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        log.info(f"  Fed Board RSS: {len(r.text):,} bytes")
        for item in soup.find_all("item"):
            try:
                title_el = item.find("title")
                if not title_el: continue
                pd = item.find("pubDate")
                sd = parse_date(pd.text.strip() if pd else "")
                if not sd or sd < cutoff: continue
                url = _rss_url(item)
                if not url: continue
                desc_el = item.find("description")
                desc = (desc_el.get_text() if desc_el else "") + " " + title_el.text
                speeches.append(dict(
                    source="fed_board", member_id=match_member(desc),
                    title=title_el.text.strip(), date=sd.isoformat(),
                    venue="", url=url,
                ))
            except Exception as e:
                log.warning(f"  Fed Board item error: {e}")
    except Exception as e:
        log.error(f"  Fed Board RSS failed: {e}")
    log.info(f"  Fed Board: {len(speeches)} found")
    return speeches

def scrape_newyorkfed(lookback: int) -> list[dict]:
    speeches, cutoff = [], date.today() - timedelta(days=lookback)
    try:
        r = requests.get("https://www.newyorkfed.org/rss/feeds/speeches",
                         headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        log.info(f"  NY Fed RSS: {len(r.text):,} bytes")
        for item in soup.find_all("item"):
            try:
                title_el = item.find("title")
                if not title_el: continue
                pd = item.find("pubDate") or item.find("dc:date")
                sd = parse_date(pd.text.strip() if pd else "")
                if not sd or sd < cutoff: continue
                url = _rss_url(item)
                if not url: continue
                desc_el = item.find("description")
                desc = (desc_el.text if desc_el else "") + " " + title_el.text
                speeches.append(dict(
                    source="ny_fed", member_id=match_member(desc),
                    title=title_el.text.strip(), date=sd.isoformat(),
                    venue="", url=url,
                ))
            except Exception as e:
                log.warning(f"  NY Fed item error: {e}")
    except Exception as e:
        log.error(f"  NY Fed RSS failed: {e}")
    log.info(f"  NY Fed: {len(speeches)} found")
    return speeches

# [FIX #7]
SPEECH_PATS = ["/speeches/","/speech/","/remarks","/speaking",
               "/news-and-events/speeches","/from-the-president",
               "/press_room/speeches","/testimony"]
SKIP_PATS   = ["/about/","/careers/","/education/","/org-chart",
               "/media-center","/publications/","/data/","/banking/",
               "/supervision/","/search","/contact","/privacy",
               ".pdf",".xlsx",".csv","/feeds/","/rss/"]

def scrape_regional(bank_id, list_url, base_url, item_sel, date_sel, lookback):
    speeches, cutoff = [], date.today() - timedelta(days=lookback)
    try:
        r = requests.get(list_url, headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        log.info(f"  {bank_id}: {len(r.text):,} bytes")
        items = []
        for sel in item_sel.split(","):
            items = soup.select(sel.strip())
            if items: break
        if not items:
            log.info(f"  {bank_id}: selector miss → link fallback")
            seen = set()
            for a in soup.find_all("a", href=True):
                href, title = a.get("href",""), a.text.strip()
                if not title or len(title)<8 or href in seen: continue
                hl = href.lower()
                if not any(p in hl for p in SPEECH_PATS): continue
                if any(p in hl for p in SKIP_PATS): continue
                if any(x in hl for x in ["#","javascript:","mailto:"]): continue
                seen.add(href)
                full = href if href.startswith("http") else base_url + (
                    href if href.startswith("/") else "/"+href)
                par = a.find_parent(["li","div","article","tr","p"])
                desc = par.get_text(" ",strip=True) if par else title
                sd = parse_date(desc) or parse_date(title)
                if not sd:
                    m = re.search(r'(20\d{2})[-/](\d{1,2})[-/](\d{1,2})', href)
                    if m:
                        try: sd = date(int(m.group(1)),int(m.group(2)),int(m.group(3)))
                        except: pass
                if not sd or sd < cutoff: continue
                speeches.append(dict(source=bank_id,member_id=match_member(desc+" "+title),
                    title=title,date=sd.isoformat(),venue="",url=full))
            log.info(f"  {bank_id}: fallback → {len(speeches)}")
            return speeches
        for item in items:
            try:
                de = None
                for ds in (date_sel or "").split(","):
                    de = item.select_one(ds.strip())
                    if de: break
                if not de: de = item.select_one("time,.date,span[class*='date']")
                a = item.select_one("a")
                if not a: continue
                ds = (de.get("datetime","") or de.text.strip()) if de else ""
                sd = parse_date(ds) or parse_date(item.get_text(" ",strip=True))
                if not sd or sd < cutoff: continue
                su = a.get("href","")
                if not su.startswith("http"): su = base_url + su
                desc = item.get_text(" ",strip=True)
                speeches.append(dict(source=bank_id,member_id=match_member(desc),
                    title=a.text.strip(),date=sd.isoformat(),venue="",url=su))
            except Exception as e:
                log.warning(f"  {bank_id} item error: {e}")
    except Exception as e:
        log.error(f"  {bank_id} failed: {e}")
    log.info(f"  {bank_id}: {len(speeches)} found")
    return speeches

REGIONAL_SOURCES = [
    ("boston","https://www.bostonfed.org/news-and-events/speeches.aspx",
     "https://www.bostonfed.org",
     "li.row,div.speeches-list-item,div[class*='speech'],li[class*='item']",
     "span[class*='date'],time,p.date"),
    ("philadelphia","https://www.philadelphiafed.org/search-results?searchtype=speeches",
     "https://www.philadelphiafed.org",
     "li[class*='result'],div[class*='result'],div[class*='item'],article",
     "time,span[class*='date'],.date"),
    ("cleveland","https://www.clevelandfed.org/collections/speeches",
     "https://www.clevelandfed.org",
     "div[class*='card'],article,li[class*='item']","time,span[class*='date']"),
    ("richmond","https://www.richmondfed.org/press_room/speeches",
     "https://www.richmondfed.org",
     "li.result,div[class*='result'],article","time,span[class*='date']"),
    ("atlanta","https://www.atlantafed.org/news-and-events/speeches",
     "https://www.atlantafed.org",
     "div[class*='teaser'],li[class*='item'],article","time,span[class*='date']"),
    ("chicago","https://www.chicagofed.org/utilities/about-us/office-of-the-president/office-of-the-president-speaking",
     "https://www.chicagofed.org",
     "li[class*='item'],div[class*='listing'],div[class*='result'],article",
     "time,span[class*='date'],.date"),
    ("stlouis","https://www.stlouisfed.org/from-the-president/remarks",
     "https://www.stlouisfed.org",
     "li[class*='item'],div[class*='item'],article","time,span[class*='date']"),
    ("minneapolis","https://www.minneapolisfed.org/speeches",
     "https://www.minneapolisfed.org",
     "div[class*='card'],li[class*='item'],article","time,span[class*='date']"),
    ("kansascity","https://www.kansascityfed.org/senior-leadership/president/",
     "https://www.kansascityfed.org",
     "li[class*='item'],div[class*='result'],article","time,span[class*='date']"),
    ("dallas","https://www.dallasfed.org/news/speeches/logan",
     "https://www.dallasfed.org",
     "div[class*='item'],li[class*='item'],article","time,span[class*='date']"),
    ("sanfrancisco","https://www.frbsf.org/news-and-media/speeches/",
     "https://www.frbsf.org",
     "li[class*='item'],div[class*='post'],article","time,span[class*='date']"),
]

# ══════════════════════════════════════════════════════════════
# SCORING  [FIX #1, #11]
# ══════════════════════════════════════════════════════════════
SCORING_PROMPT = """You are a quantitative Fed policy analyst. Score this FOMC speech on three components anchored to the December 2025 SEP framework.

NEUTRAL RATE FRAMEWORK:
- Estimated neutral rate: {neutral}% (Dec 2025 SEP median)
- Current fed funds rate: {ffr_range} (midpoint {ffr_mid}%)
- Policy is +{gap_bp}bps above neutral = modestly restrictive
- Speaker: {member_name}

SCORE THREE COMPONENTS (-100 to +100, positive = hawkish):

STANCE_SCORE — How does speaker characterize policy restrictiveness?
  "Significantly/substantially restrictive" → -60 to -80
  "Moderately restrictive" → -30 to -50
  "Modestly restrictive" → -10 to -25
  "Appropriate / near neutral" → 0 to +20
  "Not restrictive / need to hold or hike" → +30 to +70

BALANCE_SCORE — Primary risk emphasis?
  Inflation dominates → +40 to +75
  More inflation than labor → +15 to +40
  Balanced → -10 to +15
  More labor/growth concern → -15 to -40
  Employment risk dominates → -40 to -75

DIRECTION_SCORE — Rate path signal?
  Explicit hold or hike preference → +40 to +75
  Patience, lean hold → +15 to +40
  Data dependent, balanced → -10 to +15
  Lean toward gradual cuts → -15 to -40
  Explicit cut preference → -40 to -75

COMPOSITE = round(0.30 * stance + 0.35 * balance + 0.35 * direction)
Composite range: -50 to +50. Scores beyond +/-35 are rare extremes.

Extract 3-4 key signal phrases, label each hawk/dove/neutral.
One sentence rationale referencing the neutral rate framework.

Return ONLY valid JSON:
{{"stance":int,"balance":int,"direction":int,"composite":int,"reason":"string","keywords":[{{"word":"string","type":"hawk|dove|neutral"}}]}}

SPEECH TEXT:
{text}"""

def score_speech(member_id, text, claude_client):
    if not text or len(text) < 80: return None
    name = member_id.replace("_"," ").title() if member_id else "Unknown"
    prompt = SCORING_PROMPT.format(
        neutral=NEUTRAL_RATE, ffr_range=FFR_RANGE, ffr_mid=FFR_MIDPOINT,
        gap_bp=POLICY_GAP_BP, member_name=name, text=text[:2800])
    for attempt in range(3):
        try:
            if attempt: time.sleep(min(2**attempt, 30))
            msg = claude_client.messages.create(
                model=SCORE_MODEL, max_tokens=500,
                messages=[{"role":"user","content":prompt}])
            raw = re.sub(r"^```json|^```|```$","",
                         msg.content[0].text.strip(), flags=re.MULTILINE).strip()
            p = json.loads(raw)
            return dict(score=int(p.get("composite",0)),stance=int(p.get("stance",0)),
                balance=int(p.get("balance",0)),direction=int(p.get("direction",0)),
                reason=str(p.get("reason","")),keywords=p.get("keywords",[]),
                model=SCORE_MODEL)
        except Exception as e:
            log.warning(f"  Score attempt {attempt+1}/3: {e}")
    log.error(f"  SCORING FAILED for {name}")
    return None

# ══════════════════════════════════════════════════════════════
# CORPUS MANAGER  [FIX #3, #8, #9, #12]
# ══════════════════════════════════════════════════════════════
def load_corpus():
    for p in [CORPUS_ROOT, CORPUS_SCRAPER]:
        if p.exists():
            with open(p) as f: return json.load(f)
    return {}

def save_corpus(corpus):
    with open(CORPUS_ROOT, "w") as f: json.dump(corpus, f, indent=2)
    try: shutil.copy2(str(CORPUS_ROOT), str(CORPUS_SCRAPER))
    except: pass

def url_hash(url): return hashlib.md5(url.encode()).hexdigest()[:12]

def build_dedup(corpus):
    s = set()
    for speeches in corpus.values():
        for sp in speeches:
            if sp.get("url"): s.add(sp["url"]); s.add(url_hash(sp["url"]))
            if sp.get("url_hash"): s.add(sp["url_hash"])
            s.add((sp.get("date",""), sp.get("title","")[:30]))
    return s

def is_dup(dedup, url, dt="", title=""):
    return url in dedup or url_hash(url) in dedup or (dt and title and (dt,title[:30]) in dedup)

REQ_FIELDS = {"date","title","source","url","score","stance","balance","direction"}
def valid_entry(e): return not (REQ_FIELDS - set(e.keys()))

# ══════════════════════════════════════════════════════════════
# MAIN PIPELINE  [FIX #6, #9, #10, #13]
# ══════════════════════════════════════════════════════════════
def run(dry_run=False):
    log.info("="*60)
    log.info(f"FOMC Scraper — {datetime.now(timezone.utc).isoformat()}")
    log.info(f"FFR: {FFR_RANGE} | Neutral: {NEUTRAL_RATE}% | Gap: +{POLICY_GAP_BP}bp | Dry: {dry_run}")
    log.info("="*60)

    if not dry_run and not ANTHROPIC_KEY:
        log.error("ANTHROPIC_API_KEY not set"); sys.exit(1)
    cc = None
    if not dry_run:
        import anthropic; cc = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    corpus = load_corpus()
    log.info(f"Corpus: {sum(len(v) for v in corpus.values())} speeches / {len(corpus)} members")

    # Supplement merge
    sup = Path(__file__).resolve().parent / "corpus_supplement.json"
    if sup.exists():
        try:
            data = json.loads(sup.read_text()); added = 0
            for mid, sps in data.items():
                ex = corpus.get(mid,[])
                keys = {(s["date"],s["title"][:30]) for s in ex}
                urls = {s.get("url","") for s in ex}
                for s in sps:
                    k = (s["date"],s["title"][:30])
                    if k in keys or (s.get("url") and s["url"] in urls): continue
                    ex.append(s); keys.add(k); added += 1
                corpus[mid] = ex
            if added: log.info(f"Supplement: +{added}")
        except Exception as e: log.warning(f"Supplement fail: {e}")

    # [FIX #6] Compute lookback without mutating global
    lookback = DEFAULT_LOOKBACK
    all_dates = [s["date"] for v in corpus.values() for s in v if s.get("date")]
    if all_dates:
        newest = max(all_dates)
        lookback = max((date.today()-date.fromisoformat(newest)).days+1, DEFAULT_LOOKBACK)
        log.info(f"Newest: {newest} → lookback={lookback}d")

    dedup = build_dedup(corpus)
    log.info(f"Dedup keys: {len(dedup)}")

    # Collect
    all_sp = []
    log.info("\n── Fed Board ──"); all_sp.extend(scrape_fed_board(lookback)); time.sleep(1)
    log.info("\n── NY Fed ──");    all_sp.extend(scrape_newyorkfed(lookback)); time.sleep(1)
    for bid,url,burl,isel,dsel in REGIONAL_SOURCES:
        log.info(f"\n── {bid.title()} ──")
        all_sp.extend(scrape_regional(bid,url,burl,isel,dsel,lookback)); time.sleep(1)
    log.info(f"\nTotal candidates: {len(all_sp)}")

    new_n = scored_n = 0; fails = []
    try:
        for sp in all_sp:
            if is_dup(dedup, sp["url"], sp.get("date",""), sp.get("title","")): continue
            log.info(f"\n[NEW] {sp['date']} | {sp.get('member_id','?')} | {sp['title'][:60]}")
            if dry_run:
                log.info(f"  DRY RUN → {sp['url']}"); new_n += 1; continue
            text = fetch_speech_text(sp["url"])
            if not text or len(text)<80:
                log.warning("  No text"); continue
            sc = score_speech(sp.get("member_id"), text, cc)
            if not sc: fails.append(sp); continue
            log.info(f"  → {sc['score']:+d} S:{sc['stance']:+d} B:{sc['balance']:+d} D:{sc['direction']:+d}")
            mid = sp.get("member_id") or "unknown"
            if mid not in corpus: corpus[mid] = []
            entry = dict(date=sp["date"],title=sp["title"],venue=sp.get("venue",""),
                url=sp["url"],url_hash=url_hash(sp["url"]),source=sp["source"],
                text=text[:800],score=sc["score"],stance=sc["stance"],
                balance=sc["balance"],direction=sc["direction"],reason=sc["reason"],
                keywords=sc["keywords"],model=sc["model"],
                scraped_at=datetime.now(timezone.utc).isoformat())
            if valid_entry(entry):
                corpus[mid].append(entry)
                dedup.add(sp["url"]); dedup.add(url_hash(sp["url"]))
                dedup.add((sp["date"],sp["title"][:30]))
                new_n += 1; scored_n += 1
            time.sleep(1.5)
    finally:
        if not dry_run and new_n:
            save_corpus(corpus)
            log.info(f"Saved: {sum(len(v) for v in corpus.values())} speeches")

    if fails:
        log.warning(f"\n⚠ {len(fails)} failed:")
        for s in fails: log.warning(f"  {s['date']} | {s.get('member_id','?')} | {s['url']}")
        fp = Path(__file__).resolve().parent / "failed_speeches.json"
        with open(fp,"w") as f: json.dump(fails,f,indent=2)

    log.info(f"\n{'='*60}")
    log.info(f"Done: {new_n} new · {scored_n} scored · {len(fails)} failed")
    log.info(f"Corpus: {sum(len(v) for v in corpus.values())} total / {len(corpus)} members")
    log.info("="*60)

if __name__ == "__main__":
    pa = argparse.ArgumentParser(description="FOMC Speech Scraper")
    pa.add_argument("--dry-run", action="store_true", help="Scrape without scoring")
    pa.add_argument("--backfill", action="store_true", help="Full backfill (365 days)")
    pa.add_argument("--lookback", type=int, help="Override lookback days")
    a = pa.parse_args()
    if a.lookback: DEFAULT_LOOKBACK = a.lookback
    if a.backfill: DEFAULT_LOOKBACK = 365
    run(dry_run=a.dry_run)

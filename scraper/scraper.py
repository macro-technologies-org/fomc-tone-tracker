import json
"""
FOMC Speech Scraper — federalreserve.gov + all 12 regional Fed banks
Runs daily via GitHub Actions. Fetches new speeches, scores with Claude,
appends to corpus.json which feeds the FOMC Tone Tracker.
"""

import os, re, json, time, logging, hashlib, sys
from datetime import datetime, date, timedelta
from typing import Optional
import requests
from bs4 import BeautifulSoup
import anthropic

# ── LOGGING ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── CONFIG ─────────────────────────────────────────────────
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
SCORE_MODEL   = "claude-sonnet-4-5"
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))
CORPUS_FILE   = os.path.join(os.path.dirname(__file__), "corpus.json")

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── FOMC MEMBER ID MAP ──────────────────────────────────────
MEMBER_MAP = {
    "powell":      ["powell", "jerome powell"],
    "jefferson":   ["jefferson", "philip jefferson"],
    "williams":    ["williams", "john williams"],
    "waller":      ["waller", "christopher waller"],
    "bowman":      ["bowman", "michelle bowman"],
    "kugler":      ["kugler", "adriana kugler"],
    "cook":        ["cook", "lisa cook"],
    "barr":        ["barr", "michael barr"],
    "miran":       ["miran", "stephen miran"],
    "goolsbee":    ["goolsbee", "austan goolsbee"],
    "schmid":      ["schmid", "jeff schmid"],
    "hammack":     ["hammack", "beth hammack"],
    "logan":       ["logan", "lorie logan"],
    "bostic":      ["bostic", "raphael bostic"],
    "collins":     ["collins", "susan collins"],
    "harker":      ["harker", "patrick harker"],
    "kashkari":    ["kashkari", "neel kashkari"],
    "daly":        ["daly", "mary daly"],
    "barkin":      ["barkin", "tom barkin"],
}

def match_member(text: str) -> Optional[str]:
    t = text.lower()
    for member_id, names in MEMBER_MAP.items():
        if any(n in t for n in names):
            return member_id
    return None


# ══════════════════════════════════════════════════════════════
# DATE PARSER
# ══════════════════════════════════════════════════════════════
DATE_FMTS = [
    "%B %d, %Y", "%b %d, %Y", "%B %d,%Y",
    "%Y-%m-%d", "%m/%d/%Y", "%d %B %Y", "%B %Y",
    "%d %b %Y", "%Y/%m/%d",
]

def parse_date(text: str) -> Optional[date]:
    if not text:
        return None
    text = re.sub(r'\s+', ' ', text.strip())
    # Handle RSS pubDate: "Fri, 07 Feb 2026 00:00:00 EST" → "07 Feb 2026"
    text = re.sub(r'^\w{3},\s*', '', text)            # strip "Fri, "
    text = re.sub(r'\s+\d{2}:\d{2}:\d{2}.*$', '', text)  # strip time+tz
    text = re.sub(r'(st|nd|rd|th),', ',', text)
    for fmt in DATE_FMTS:
        try:
            return datetime.strptime(text[:30], fmt).date()
        except:
            pass
    m = re.search(r'(\w+ \d{1,2},? \d{4})', text)
    if m:
        return parse_date(m.group(1))
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if m:
        try: return date.fromisoformat(m.group(1))
        except: pass
    return None


# ══════════════════════════════════════════════════════════════
# FULL TEXT FETCHER
# ══════════════════════════════════════════════════════════════
def fetch_speech_text(url: str) -> str:
    """Fetch and extract main speech text from a URL."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["nav","footer","header","script","style","aside"]):
            tag.decompose()
        for sel in [
            "div#article",
            "div.col-xs-12.col-sm-8.col-md-8",
            "div.ts-article-content",
            "div.speech-content",
            "div#content-detail",
            "div.entry-content",
            "article",
            "main",
            "div#content",
        ]:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 300:
                text = re.sub(r'\s+', ' ', el.get_text(" ", strip=True)).strip()
                return text[:1500]
        body = soup.find("body")
        if body:
            return re.sub(r'\s+', ' ', body.get_text(" ", strip=True)).strip()[:1500]
    except Exception as e:
        log.warning(f"  Text fetch failed for {url}: {e}")
    return ""


# ══════════════════════════════════════════════════════════════
# SITE SCRAPERS
# ══════════════════════════════════════════════════════════════

def scrape_fed_board() -> list[dict]:
    """Federal Reserve Board — uses RSS feed for reliability."""
    rss_url = "https://www.federalreserve.gov/feeds/speeches.xml"
    speeches = []
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    try:
        r = requests.get(rss_url, headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        log.info(f"  Fed Board RSS: {len(r.text):,} bytes")
        for item in soup.find_all("item"):
            try:
                title   = item.find("title")
                link    = item.find("link")
                pubdate = item.find("pubDate")
                desc_el = item.find("description")
                if not title or not link: continue
                speech_date = parse_date(pubdate.text.strip() if pubdate else "")
                if not speech_date or speech_date < cutoff: continue
                # RSS <link> in BeautifulSoup xml mode can be tricky
                url = ""
                if link.string:
                    url = link.string.strip()
                elif link.next_sibling:
                    url = str(link.next_sibling).strip()
                if not url or not url.startswith("http"):
                    # Try guid as fallback
                    guid = item.find("guid")
                    if guid and guid.text.startswith("http"):
                        url = guid.text.strip()
                if not url or not url.startswith("http"):
                    continue
                desc = (desc_el.get_text() if desc_el else "") + " " + title.text
                speeches.append({
                    "source": "fed_board", "member_id": match_member(desc),
                    "title": title.text.strip(), "date": speech_date.isoformat(),
                    "venue": "", "url": url,
                    "excerpt": desc_el.get_text()[:500] if desc_el else "",
                })
            except Exception as e:
                log.warning(f"  Fed Board RSS item error: {e}")
    except Exception as e:
        log.error(f"  Fed Board RSS failed: {e}")
    log.info(f"  Fed Board: {len(speeches)} found")
    return speeches


def scrape_newyorkfed() -> list[dict]:
    """NY Fed — uses RSS feed for reliability."""
    rss_url = "https://www.newyorkfed.org/rss/feeds/speeches"
    speeches = []
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    try:
        r = requests.get(rss_url, headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        log.info(f"  NY Fed RSS: {len(r.text):,} bytes")
        for item in soup.find_all("item"):
            try:
                title   = item.find("title")
                link    = item.find("link")
                pubdate = item.find("pubDate") or item.find("dc:date")
                desc_el = item.find("description")
                if not title or not link: continue
                speech_date = parse_date(pubdate.text.strip() if pubdate else "")
                if not speech_date or speech_date < cutoff: continue
                url = link.text.strip()
                if not url.startswith("http"):
                    url = link.next_sibling.strip() if link.next_sibling else ""
                desc = (desc_el.text if desc_el else "") + " " + title.text
                speeches.append({
                    "source": "ny_fed", "member_id": match_member(desc),
                    "title": title.text.strip(), "date": speech_date.isoformat(),
                    "venue": "", "url": url,
                })
            except Exception as e:
                log.warning(f"  NY Fed RSS item error: {e}")
    except Exception as e:
        log.error(f"  NY Fed RSS failed: {e}")
    log.info(f"  NY Fed: {len(speeches)} found")
    return speeches


def scrape_regional(bank_id: str, list_url: str, base_url: str,
                    item_sel: str, date_sel: str) -> list[dict]:
    """Generic regional Fed scraper with link-based fallback."""
    speeches = []
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    try:
        r = requests.get(list_url, headers=HEADERS, timeout=45)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        log.info(f"  {bank_id}: {len(r.text):,} bytes")

        items = []
        for sel in item_sel.split(","):
            items = soup.select(sel.strip())
            if items:
                break

        if not items:
            log.info(f"  {bank_id}: no items via selector, using link fallback")
            seen = set()
            SPEECH_PATTERNS = [
                "/speech", "speech/", "/remarks", "/remark", "/talk",
                "/address", "/testimony", "/comment", "/statement",
                "/president", "/speaking", "/presentation",
            ]
            for a in soup.find_all("a", href=True):
                href = a.get("href","")
                title = a.text.strip()
                if not title or len(title) < 8 or href in seen:
                    continue
                href_lower = href.lower()
                # Must look like a speech link
                if not any(x in href_lower for x in SPEECH_PATTERNS):
                    continue
                # Skip nav/menu links
                if any(x in href_lower for x in ["#", "javascript:", "mailto:", "/search", "/about", "/contact"]):
                    continue
                seen.add(href)
                full_url = href if href.startswith("http") else base_url + (href if href.startswith("/") else "/" + href)
                # Look for date in parent elements (up to 4 levels)
                parent = a.find_parent(["li","div","article","tr","p"])
                desc = parent.get_text(" ", strip=True) if parent else title
                # Try progressively wider context for date
                speech_date = parse_date(desc) or parse_date(title)
                if not speech_date:
                    # Try date from URL
                    m = re.search(r'(20\d{2})[-/](\d{1,2})[-/](\d{1,2})', href)
                    if m:
                        try: speech_date = date(int(m.group(1)),int(m.group(2)),int(m.group(3)))
                        except: pass
                if not speech_date or speech_date < cutoff:
                    continue
                speeches.append({
                    "source": bank_id, "member_id": match_member(desc + " " + title),
                    "title": title, "date": speech_date.isoformat(),
                    "venue": "", "url": full_url,
                })
            log.info(f"  {bank_id}: fallback found {len(speeches)}")
            return speeches

        for item in items:
            try:
                date_el = None
                for ds in (date_sel or "").split(","):
                    date_el = item.select_one(ds.strip())
                    if date_el: break
                if not date_el:
                    date_el = item.select_one("time, .date, span[class*='date']")
                a = item.select_one("a")
                if not a: continue
                date_str = (date_el.get("datetime","") or date_el.text.strip()) if date_el else ""
                speech_date = parse_date(date_str) or parse_date(item.get_text(" ", strip=True))
                if not speech_date or speech_date < cutoff: continue
                speech_url = a.get("href","")
                if not speech_url.startswith("http"):
                    speech_url = base_url + speech_url
                desc = item.get_text(" ", strip=True)
                speeches.append({
                    "source": bank_id, "member_id": match_member(desc),
                    "title": a.text.strip(), "date": speech_date.isoformat(),
                    "venue": "", "url": speech_url,
                })
            except Exception as e:
                log.warning(f"  {bank_id} item error: {e}")
    except Exception as e:
        log.error(f"  {bank_id} failed: {e}")
    log.info(f"  {bank_id}: {len(speeches)} found")
    return speeches


# Source registry: (bank_id, url, base_url, item_selectors, date_selectors)
# URLs verified Feb 2026 — using most reliable endpoints per site
REGIONAL_SOURCES = [
    ("boston",       "https://www.bostonfed.org/news-and-events/speeches.aspx",
                     "https://www.bostonfed.org",
                     "li.row, div.speeches-list-item, div[class*='speech'], li[class*='item']",
                     "span[class*='date'], time, p.date"),
    ("philadelphia", "https://www.philadelphiafed.org/search-results?searchtype=speeches",
                     "https://www.philadelphiafed.org",
                     "li[class*='result'], div[class*='result'], div[class*='item'], article",
                     "time, span[class*='date'], .date"),
    ("cleveland",    "https://www.clevelandfed.org/collections/speeches",
                     "https://www.clevelandfed.org",
                     "div[class*='card'], article, li[class*='item']",
                     "time, span[class*='date']"),
    ("richmond",     "https://www.richmondfed.org/press_room/speeches",
                     "https://www.richmondfed.org",
                     "li.result, div[class*='result'], article",
                     "time, span[class*='date']"),
    ("atlanta",      "https://www.atlantafed.org/news-and-events/speeches",
                     "https://www.atlantafed.org",
                     "div[class*='teaser'], li[class*='item'], article",
                     "time, span[class*='date']"),
    ("chicago",      "https://www.chicagofed.org/utilities/about-us/office-of-the-president/office-of-the-president-speaking",
                     "https://www.chicagofed.org",
                     "li[class*='item'], div[class*='listing'], div[class*='result'], article",
                     "time, span[class*='date'], .date"),
    ("stlouis",      "https://www.stlouisfed.org/from-the-president/remarks",  # may timeout on CI
                     "https://www.stlouisfed.org",
                     "li[class*='item'], div[class*='item'], article",
                     "time, span[class*='date']"),
    ("minneapolis",  "https://www.minneapolisfed.org/publications-archive/all-speeches",
                     "https://www.minneapolisfed.org",
                     "div[class*='card'], li[class*='item'], article",
                     "time, span[class*='date']"),
    ("kansascity",   "https://www.kansascityfed.org/senior-leadership/president/",
                     "https://www.kansascityfed.org",
                     "li[class*='item'], div[class*='result'], article",
                     "time, span[class*='date']"),
    ("dallas",       "https://www.dallasfed.org/news/speeches/logan",
                     "https://www.dallasfed.org",
                     "div[class*='item'], li[class*='item'], article",
                     "time, span[class*='date']"),
    ("sanfrancisco", "https://www.frbsf.org/news-and-media/speeches/",
                     "https://www.frbsf.org",
                     "li[class*='item'], div[class*='post'], article",
                     "time, span[class*='date']"),
]


# ══════════════════════════════════════════════════════════════
# SCORING ENGINE
# ══════════════════════════════════════════════════════════════
SCORING_PROMPT = """You are a quantitative Fed policy analyst. Score this FOMC speech on three components anchored to the December 2025 SEP framework.

NEUTRAL RATE FRAMEWORK:
- Estimated neutral rate: 3.0% (Dec 2025 SEP median)
- Current fed funds rate: 4.25-4.50% (midpoint 4.375%)
- Policy is +137.5bps above neutral = moderately restrictive
- Speaker: {member_name}

SCORE THREE COMPONENTS (-100 to +100, positive = hawkish):

STANCE_SCORE — How does speaker characterize policy restrictiveness?
  "Significantly/substantially restrictive" → -60 to -80
  "Moderately restrictive" → -30 to -50
  "Modestly restrictive" → -10 to -25
  "Appropriate / near neutral" → 0 to +20
  "Not restrictive / need to hold" → +30 to +70

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

COMPOSITE = round(0.30 × stance + 0.35 × balance + 0.35 × direction)

Extract 3-4 key signal phrases, label each hawk/dove/neutral.
One sentence rationale referencing the neutral rate framework.

Return ONLY valid JSON, no markdown:
{{"stance":int,"balance":int,"direction":int,"composite":int,"reason":"string","keywords":[{{"word":"string","type":"hawk|dove|neutral"}}]}}

SPEECH TEXT:
{text}"""


def score_speech(member_id: Optional[str], text: str) -> Optional[dict]:
    if not text or len(text) < 50:
        return None
    member_name = member_id.replace("_"," ").title() if member_id else "Unknown FOMC Official"
    prompt = SCORING_PROMPT.format(member_name=member_name, text=text[:1400])
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2 ** attempt)
            msg = claude.messages.create(
                model=SCORE_MODEL, max_tokens=400,
                messages=[{"role":"user","content":prompt}]
            )
            raw = re.sub(r"^```json|^```|```$","",msg.content[0].text.strip(),flags=re.MULTILINE).strip()
            parsed = json.loads(raw)
            return {
                "score":     int(parsed.get("composite",0)),
                "stance":    int(parsed.get("stance",0)),
                "balance":   int(parsed.get("balance",0)),
                "direction": int(parsed.get("direction",0)),
                "reason":    str(parsed.get("reason","")),
                "keywords":  parsed.get("keywords",[]),
                "model":     SCORE_MODEL,
            }
        except Exception as e:
            log.warning(f"  Score attempt {attempt+1} failed: {e}")
    return None


# ══════════════════════════════════════════════════════════════
# CORPUS MANAGER
# ══════════════════════════════════════════════════════════════
def load_corpus() -> dict:
    if os.path.exists(CORPUS_FILE):
        with open(CORPUS_FILE) as f:
            return json.load(f)
    return {}

def save_corpus(corpus: dict):
    with open(CORPUS_FILE, "w") as f:
        json.dump(corpus, f, indent=2)

def url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]

def is_duplicate(corpus: dict, url: str) -> bool:
    h = url_hash(url)
    for speeches in corpus.values():
        for sp in speeches:
            if sp.get("url") == url or sp.get("url_hash") == h:
                return True
    return False


# ══════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════
def run():
    global LOOKBACK_DAYS  # must be at top of function
    log.info("=" * 60)
    log.info(f"FOMC Tone Scraper — {datetime.utcnow().isoformat()}")
    log.info(f"Model: {SCORE_MODEL}")
    log.info("=" * 60)

    corpus = load_corpus()
    existing_count = sum(len(v) for v in corpus.values())
    log.info(f"Existing corpus: {existing_count} speeches across {len(corpus)} members")

    # Merge manual supplement (speeches scraper can't auto-find)
    supplement_path = CORPUS_PATH.parent / "corpus_supplement.json"
    if supplement_path.exists():
        try:
            supplement = json.loads(supplement_path.read_text())
            added = 0
            for member_id, speeches in supplement.items():
                existing = corpus.get(member_id, [])
                existing_keys = {(sp["date"], sp["title"][:30]) for sp in existing}
                existing_urls = {sp.get("url","") for sp in existing}
                for sp in speeches:
                    key = (sp["date"], sp["title"][:30])
                    if key in existing_keys or (sp.get("url") and sp["url"] in existing_urls):
                        continue
                    existing.append(sp)
                    existing_keys.add(key)
                    added += 1
                corpus[member_id] = existing
            if added:
                log.info(f"Supplement: merged {added} manual speeches")
        except Exception as e:
            log.warning(f"Supplement merge failed: {e}")

    # Set lookback to cover from newest speech in corpus to today
    if existing_count > 0:
        all_dates = [
            sp["date"] for speeches in corpus.values()
            for sp in speeches if sp.get("date")
        ]
        if all_dates:
            newest = max(all_dates)
            days_since = (date.today() - date.fromisoformat(newest)).days + 1
            effective_lookback = max(days_since, LOOKBACK_DAYS)
            log.info(f"Newest speech in corpus: {newest} ({days_since} days ago)")
            log.info(f"Effective lookback: {effective_lookback} days")
            LOOKBACK_DAYS = effective_lookback

    # Collect from all sources
    all_speeches = []
    log.info("\n── Fed Board of Governors ──")
    all_speeches.extend(scrape_fed_board())
    time.sleep(1)

    log.info("\n── New York Fed ──")
    all_speeches.extend(scrape_newyorkfed())
    time.sleep(1)

    for bank_id, url, base_url, item_sel, date_sel in REGIONAL_SOURCES:
        log.info(f"\n── {bank_id.title()} Fed ──")
        all_speeches.extend(scrape_regional(bank_id, url, base_url, item_sel, date_sel))
        time.sleep(1)

    log.info(f"\nTotal found: {len(all_speeches)} speeches across all sources")

    # Deduplicate, fetch text, score
    total_new = total_scored = 0
    for sp in all_speeches:
        if is_duplicate(corpus, sp["url"]):
            continue

        log.info(f"\n[NEW] {sp['date']} | {sp.get('member_id','unknown')} | {sp['title'][:60]}")
        text = fetch_speech_text(sp["url"])
        if not text:
            log.warning("  No text — skipping")
            continue

        score = score_speech(sp.get("member_id"), text)
        if not score:
            log.warning("  Score failed — skipping")
            continue

        log.info(f"  Score: {score['score']:+d} | {score['reason'][:80]}")

        member_id = sp.get("member_id") or "unknown"
        if member_id not in corpus:
            corpus[member_id] = []

        corpus[member_id].append({
            "date":       sp["date"],
            "title":      sp["title"],
            "venue":      sp.get("venue",""),
            "url":        sp["url"],
            "url_hash":   url_hash(sp["url"]),
            "source":     sp["source"],
            "text":       text[:800],
            "score":      score["score"],
            "stance":     score["stance"],
            "balance":    score["balance"],
            "direction":  score["direction"],
            "reason":     score["reason"],
            "keywords":   score["keywords"],
            "model":      score["model"],
            "scraped_at": datetime.utcnow().isoformat(),
        })
        total_new += 1
        total_scored += 1
        save_corpus(corpus)
        time.sleep(1.5)

    log.info("\n" + "=" * 60)
    log.info(f"Done: {total_new} new, {total_scored} scored")
    log.info(f"Corpus: {sum(len(v) for v in corpus.values())} total speeches / {len(corpus)} members")
    log.info("=" * 60)


if __name__ == "__main__":
    run()

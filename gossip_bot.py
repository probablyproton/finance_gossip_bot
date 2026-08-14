"""
Finance Gossip Bot — v1 draft.

Aggregates ALREADY-PUBLISHED gossip/drama stories about named financial titans (CEOs,
billionaires, investors) from credible business/gossip press, and posts a short,
clearly-attributed summary + link. See DESIGN.md for the full content policy.

v1 is deliberately zero-LLM: fetch -> gossip-signal filter -> source-quality sort -> dedup
-> template tweet -> post. No fabrication risk from an LLM step until the sourcing model
itself is validated.

Local run:  python gossip_bot.py          (DRY_RUN=true by default, browser visible)
GitHub:     runs on workflow_dispatch, same external-cadence model as the ticker bot this
            was forked from — see .github/workflows/bot.yml.
"""

import os
import re
import json
import time
import random
import logging
import datetime
import tempfile
import requests
import html as html_module
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv("my.env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gossip_bot")

GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")  # not used yet in v1, reserved for a later LLM layer
TITANS_WATCHLIST = [n.strip() for n in os.getenv("TITANS_WATCHLIST", "").split(",") if n.strip()]
DRY_RUN          = os.getenv("DRY_RUN", "true").lower() == "true"
NEWS_FRESHNESS_HOURS = int(os.getenv("NEWS_FRESHNESS_HOURS", "48"))
GOSSIP_SEEN_MEMORY_DAYS = int(os.getenv("GOSSIP_SEEN_MEMORY_DAYS", "14"))
MAX_POSTS_PER_CYCLE = int(os.getenv("MAX_POSTS_PER_CYCLE", "3"))
POST_PACING_SECONDS = int(os.getenv("POST_PACING_SECONDS", "45"))

STATE_FILE   = "state.json"
SESSION_FILE = "twitter_session.json"

# ── Content policy filters (see DESIGN.md) ──────────────────────────────────────────────

# Positive filter: the headline itself has to signal drama/conflict/business intrigue —
# this is the opposite of the ticker bot's "reject clickbait" filter, since here the spicy
# framing IS the content. Still requires a real published article behind it.
# Trimmed after confirmed false positives spammed the feed with routine business news that
# happened to contain a weak signal word attached to a titan's name: "resigns"/"steps down"
# fire on ordinary planned departures, "split" fires on stock-split announcements (these ARE
# CEOs of public companies, so that's a frequent hit), "leaked"/"secret" fire on generic
# product-leak headlines, "controversy"/"apology"/"criticizes" fire on any policy/PR story.
# Kept only words that specifically signal interpersonal conflict/drama, not routine business
# events or general criticism of a decision.
# Expanded with explicit tabloid/personal-life categories per the account's actual mandate
# (see DESIGN.md): divorce, cheating, addiction, personal fights -- the paparazzi of finance,
# not a business-news feed with spicier headlines.
GOSSIP_SIGNAL_RE = re.compile(
    r"\b(feud|lawsuit|sues?|sued|fired|ousted|slams?|blasts?(?:ed)?|"
    r"scandal|divorce|divorc\w+|affair|cheat(?:s|ed|ing)?|mistress|infidelit\w+|"
    r"break-?up|broke\s+up|estranged|custody|prenup|"
    r"addiction|rehab|relapse[ds]?|overdose[ds]?|arrest(?:ed)?|jail(?:ed)?|indict\w+|"
    r"rivalry|clash(?:es)?|accus\w+|"
    r"drama|shake-?up|expos(?:e|es|ed)|rift|fallout|"
    r"blow-?up|meltdown|showdown|spat|snub(?:s|bed)?|brawl)\b",
    re.I,
)

# Negative filter: reject a headline even if it matched a gossip-signal word above, when the
# same headline is clearly framing a resolved BUSINESS or political arrangement rather than
# personal drama. Confirmed live: "Trump And Musk Have Now Turned Their Bitter Feud Into A
# $100 Million Alliance" matched "feud" but is a business/political deal story, not gossip --
# exactly the kind of routine business news dressed up in dramatic language this account
# should never post. This is deliberately the ONLY disambiguation mechanism for words like
# "feud"/"rivalry"/"clash" that are genuinely ambiguous between personal and professional
# conflict -- a real personal feud headline won't also mention a deal/alliance/merger.
BUSINESS_OUTCOME_RE = re.compile(
    r"\b(alliance|merger|acquisition|partnership|joint\s+venture|venture|ipo|buyout|"
    r"takeover|tie-?up|team(?:s|ed)?\s+up|joins?\s+forces|stake|funding\s+round|"
    r"board\s+seat)\b",
    re.I,
)

# Never pull from satire/fabrication sites — a "gossip" bot has a lower bar for spicy
# framing than the ticker bot, which makes real-source discipline even more important here.
BLOCKLIST_SOURCES = {s.lower() for s in [
    "The Onion", "Babylon Bee", "World News Daily Report", "The Daily Currant",
]}

# Soft preference — lead with credible business/gossip press over a generic aggregator
# when multiple outlets covered the same story.
PREFERRED_SOURCES = {s.lower() for s in [
    "Bloomberg", "Reuters", "WSJ", "Wall Street Journal", "Fortune", "Business Insider",
    "Forbes", "CNBC", "Axios", "Puck", "Semafor", "Page Six", "New York Post",
    "Financial Times", "The Information", "Vanity Fair", "Barron", "Barron's",
]}

# Not a credibility judgment — these are legitimate publications — but confirmed live (direct
# fetch returned HTTP 403) that they block plain automated requests, which breaks two things:
# X's own link-preview crawler can't build a rich card (falls back to the site's generic
# logo instead of an article photo), and our own fetch_article_context silently gets nothing.
# Ranked below normal (non-preferred) sources so an equally-good alternative outlet covering
# the same story wins instead, when one exists — the story itself is still fine, only the
# specific link is worse.
CRAWLER_UNFRIENDLY_SOURCES = {s.lower() for s in ["inc.com", "inc"]}


def _source_rank(a: dict) -> int:
    src = (a.get("source") or "").lower()
    if src in PREFERRED_SOURCES:
        return 0
    if src in CRAWLER_UNFRIENDLY_SOURCES:
        return 2
    return 1


def today() -> str:
    return datetime.date.today().isoformat()


def _prune_date_keyed_dict(d: dict, max_age_days: int):
    """Drops entries whose ISO-date value is older than max_age_days — same pattern as the
    ticker bot's dedup memory, so state.json doesn't carry fingerprints forever."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=max_age_days)).isoformat()
    for key in [key for key, seen_date in d.items() if seen_date < cutoff]:
        del d[key]


def _is_gossip_worthy(headline: str) -> bool:
    return bool(GOSSIP_SIGNAL_RE.search(headline)) and not BUSINESS_OUTCOME_RE.search(headline)


_GOOGLE_NEWS_SIG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_GOOGLE_NEWS_TS_RE = re.compile(r'data-n-a-ts="([^"]+)"')


def _resolve_google_news_url(link: str) -> str:
    """Google News RSS links are a redirect wrapper, not the real article — readers who
    click through land on a Google interstitial rather than the actual publisher. Decodes
    it to the real destination via Google's internal batchexecute endpoint (the same call
    the News web UI itself makes to resolve the redirect). Ported from the ticker bot,
    re-verified live against a real redirect URL before shipping.

    Unofficial/reverse-engineered — Google has changed this encoding before and could
    again. Any failure just falls back to the original link rather than blocking the post."""
    if not link or "news.google.com" not in link:
        return link
    try:
        from urllib.parse import urlparse
        article_id = urlparse(link).path.rsplit("/", 1)[-1]
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(link, timeout=10, verify=False, headers=headers,
                             cookies={"SOCS": "CAISHAgBEhJnd3NfMjAyNDAxMDEtMF9SQzIaAmVuIAEaBgiA_LyuBg"})
        resp.raise_for_status()
        sig = _GOOGLE_NEWS_SIG_RE.search(resp.text)
        ts = _GOOGLE_NEWS_TS_RE.search(resp.text)
        if not sig or not ts:
            return link

        inner = json.dumps(["garturlreq", [
            ["en-US", "US", ["FINANCE_TOP_INDICES", "GENESIS_PUBLISHER_SECTION", "WEB_TEST_1_0_0"],
             None, None, 1, 1, "US:en", None, 180, None, None, None, None, None, 0, None, None,
             [1608992183, 723341000]],
            "en-US", "US", 1, [2, 3, 4, 8], 1, 0, "655000234", 0, 0, None, 0],
            article_id, int(ts.group(1)), sig.group(1)])
        payload = {"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}
        resp2 = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                               headers={**headers, "content-type": "application/x-www-form-urlencoded;charset=UTF-8"},
                               data=payload, timeout=10, verify=False)
        resp2.raise_for_status()
        body = resp2.text.split("\n", 1)[-1]
        outer = json.loads(body)
        inner_result = json.loads(outer[0][2])
        resolved = inner_result[1]
        return resolved if isinstance(resolved, str) and resolved.startswith("http") else link
    except Exception as e:
        log.warning("Google News URL resolve failed, keeping original link: %s", e)
        return link


_NAV_JUNK_RE = re.compile(
    r"\b(subscribe|sign up|newsletter|cookie|log\s*in|advertisement|follow us|read more|"
    r"share this|related articles|menu|sections|navigation|Add The .+ on Google)\b",
    re.I,
)

# Real prose is dense with short function words ("the", "of", "a", "his", "but", ...); a
# nav/breadcrumb block strung from Title Case category labels ("US News Metro Long Island
# Politics World News...") has almost none. Confirmed live: NY Post's own markup has TWO
# separate nav/breadcrumb <p> blocks before the real article text, and keyword-blocking alone
# didn't catch both — this catches the shape of the noise instead of naming every instance.
_FUNCTION_WORDS_RE = re.compile(
    r"\b(the|a|an|of|in|to|and|is|was|but|for|with|he|she|his|her|that|on|as|by)\b", re.I)
_MIN_FUNCTION_WORD_HITS = 5


def fetch_article_context(url: str, max_chars: int = 300) -> str:
    """Best-effort extraction of the article's own opening paragraph, so the tweet can carry
    real substance instead of just a headline — confirmed empirically that Google News RSS's
    <description> gives nothing beyond the headline itself (just an HTML restatement), while
    direct outlet feeds (Page Six, NY Post) DO have genuine excerpts. Rather than depending on
    which search path happened to find a story, this fetches the resolved article URL
    directly and works the same way regardless of source. Returns '' on any failure — a fetch
    failure just means a shorter tweet, never fabricated content."""
    if not url:
        return ""
    try:
        resp = requests.get(url, timeout=10, verify=False, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        body = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", resp.text)
        for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", body):
            text = re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]+>", " ", p))).strip()
            if len(text) < 80 or _NAV_JUNK_RE.search(text):
                continue
            if len(_FUNCTION_WORDS_RE.findall(text)) < _MIN_FUNCTION_WORD_HITS:
                continue  # reads like a nav/breadcrumb block, not a real sentence
            return text[:max_chars]
    except Exception as e:
        log.warning("Article context fetch failed for %s: %s", url, e)
    return ""


def _fetch_google_news_rss(query: str, max_items: int, log_label: str) -> list[dict]:
    """Google News RSS search for an arbitrary query string — shared by both the per-person
    watchlist search and the industry-wide topic search below."""
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = requests.get(url, timeout=10, verify=False, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        log.warning("News fetch failed for %s: %s", log_label, e)
        return []

    results = []
    for item in root.findall(".//item")[:max_items]:
        title = item.findtext("title")
        link = item.findtext("link")
        pub_date = item.findtext("pubDate")
        if not title:
            continue
        try:
            parsed = parsedate_to_datetime(pub_date) if pub_date else None
            if parsed is not None and parsed.tzinfo is not None:
                pub_dt = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            else:
                pub_dt = parsed
        except Exception:
            pub_dt = None

        source_el = item.find("source")
        source = None
        if source_el is not None and source_el.text:
            source = source_el.text.strip()
            suffix = f" - {source}"
            if title.lower().endswith(suffix.lower()):
                title = title[: -len(suffix)].strip()

        results.append({"headline": title, "link": link, "source": source, "published": pub_dt})
    return results


def fetch_person_news(name: str, max_items: int = 10) -> list[dict]:
    """Google News RSS search for a named individual on the watchlist."""
    return _fetch_google_news_rss(name, max_items, log_label=name)


# Broad, name-agnostic searches — this is what catches a real story like John Overdeck's
# $6.2B divorce (Two Sigma's co-founder, absolutely a "financial titan" but not someone
# anyone thought to put on a fixed name list in advance). A curated watchlist, no matter how
# broad, always has blind spots; this is the actual fix for that, not just a bigger list.
INDUSTRY_TOPIC_QUERIES = [
    '("hedge fund" OR "private equity" OR "Wall Street" OR "billionaire investor" OR '
    '"fund manager") (divorce OR lawsuit OR sues OR sued OR feud OR fired OR resigns OR '
    'scandal OR fraud OR indicted OR ousted)',
    '("hedge fund" OR "fund founder" OR "fund CEO" OR "fund co-founder") '
    '(court battle OR settlement OR subpoena OR SEC probe)',
]


def fetch_topic_news(query: str, max_items: int = 15) -> list[dict]:
    """Google News RSS search for a broad industry theme, not tied to any specific name."""
    return _fetch_google_news_rss(query, max_items, log_label=f"topic:{query[:40]}...")


# Confirmed live: Google News RSS indexes individual X/Twitter posts when searched with
# site:x.com, and returns the REAL TWEET TEXT as the item's <title> (e.g. resolved a genuine
# Justin Sun tweet about a lawsuit, and a real "feud between Sam Altman and Elon Musk" tweet).
# This finds quote-tweet candidates without touching X's own search/API at all — reuses the
# exact same Google News mechanism already proven for articles, just a different query shape.
_TWEET_PERMALINK_RE = re.compile(r"(?:x\.com|twitter\.com)/(\w+)/status/(\d+)")


def fetch_tweet_candidates(name: str, max_items: int = 15) -> list[dict]:
    """Google News RSS search for a named individual's own tweets or tweets about them."""
    return _fetch_google_news_rss(f"site:x.com {name}", max_items, log_label=f"tweets:{name}")


def fetch_tweet_author_name(tweet_url: str) -> str:
    """X's public oEmbed endpoint (the same one any website uses to embed a tweet) returns
    the author's real display name — e.g. 'H.E. Justin Sun 👨‍🚀 🌞' for @justinsuntron.
    Documented, public, no scraping and no API key. Used to tell 'this IS the titan's own
    tweet' apart from 'someone else gossiping about them' — only the latter is what we
    actually want to quote-tweet. Returns '' on any failure."""
    if not tweet_url:
        return ""
    try:
        resp = requests.get("https://publish.twitter.com/oembed",
                             params={"url": tweet_url}, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return resp.json().get("author_name", "")
    except Exception as e:
        log.warning("oEmbed author lookup failed for %s: %s", tweet_url, e)
        return ""


def _is_self_authored(person_name: str, author_name: str) -> bool:
    """True only if EVERY word in the titan's name appears in the tweet author's display
    name — a partial/coincidental match (just "Justin") isn't enough evidence to exclude a
    genuinely different person's tweet."""
    person_words = set(re.findall(r"[a-z]+", person_name.lower()))
    author_words = set(re.findall(r"[a-z]+", author_name.lower()))
    return bool(person_words) and person_words.issubset(author_words)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load state.json, starting fresh: %s", e)
    return {}


def save_state(state: dict):
    state_dir = os.path.dirname(STATE_FILE) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".state.", suffix=".json.tmp", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _dedup_key(a: dict) -> str:
    # Link-based, not person-based — a topic-search hit has no pre-known "person" attached,
    # and a link is inherently unique per article regardless of which query surfaced it.
    # Falls back to the headline if a feed ever omits a link.
    return (a.get("link") or a["headline"]).strip().lower()


def _passes_gossip_filters(a: dict, cutoff: datetime.datetime, seen: dict) -> bool:
    if not a.get("published") or a["published"] < cutoff:
        return False
    if not _is_gossip_worthy(a["headline"]):
        return False
    if (a.get("source") or "").lower() in BLOCKLIST_SOURCES:
        return False
    return _dedup_key(a) not in seen


def find_gossip_items(state: dict, max_items: int = 1) -> list[dict]:
    """Up to max_items freshest, real, gossip-worthy, not-yet-used stories — either about
    someone on the named watchlist, or surfaced by a broad industry-wide topic search that
    needs no pre-known name at all (see INDUSTRY_TOPIC_QUERIES). One fetch pass regardless
    of max_items — callers wanting multiple posts in a cycle should call this once with
    max_items=N, not call a single-item version N times (would re-fetch all ~59 queries
    per post)."""
    seen = state.setdefault("gossip_seen", {})
    _prune_date_keyed_dict(seen, GOSSIP_SEEN_MEMORY_DAYS)
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=NEWS_FRESHNESS_HOURS)

    candidates = []

    for name in TITANS_WATCHLIST:
        for a in fetch_person_news(name):
            if _passes_gossip_filters(a, cutoff, seen):
                candidates.append({**a, "person": name, "fingerprint": _dedup_key(a)})

    for query in INDUSTRY_TOPIC_QUERIES:
        for a in fetch_topic_news(query):
            if _passes_gossip_filters(a, cutoff, seen):
                candidates.append({**a, "person": "industry-wide", "fingerprint": _dedup_key(a)})

    for name in TITANS_WATCHLIST:
        for a in fetch_tweet_candidates(name):
            if not a.get("published") or a["published"] < cutoff:
                continue
            if not _is_gossip_worthy(a["headline"]):
                continue
            # Cheap text filter passed -- now pay for resolve + oEmbed, only for candidates
            # that already look promising.
            resolved = _resolve_google_news_url(a.get("link") or "")
            m = _TWEET_PERMALINK_RE.search(resolved)
            if not m:
                continue  # not a genuine tweet permalink (a profile/search page, etc.)
            author_name = fetch_tweet_author_name(resolved)
            if _is_self_authored(name, author_name):
                continue  # this is the titan's OWN tweet -- we want others' gossip about them
            # The tweet's numeric status ID, not the full URL string -- confirmed live this
            # was the actual dedup bug: the SAME tweet got quote-tweeted 3 times, because
            # Google's resolution isn't guaranteed to return byte-identical URLs across
            # different searches for the same tweet (domain variant, tracking params, etc.),
            # so a raw-URL fingerprint silently failed to match. A tweet's status ID is
            # permanent and unique regardless of domain/username/query-string variation.
            fp = f"tweet:{m.group(2)}"
            if fp in seen:
                continue
            # Canonical status URL, not the raw resolved string -- confirmed live that a
            # resolved URL can carry a trailing /photo/1 (or similar media-view suffix),
            # which makes X's quote-tweet embed jump straight to the photo lightbox and
            # render with no text/source card at all, just the bare image.
            canonical_url = f"https://x.com/{m.group(1)}/status/{m.group(2)}"
            candidates.append({
                "headline": a["headline"], "link": canonical_url,
                "source": author_name or "a tweet", "published": a["published"],
                "person": name, "fingerprint": fp, "is_tweet": True,
            })

    if not candidates:
        return []

    # Freshest first, then float credible sources above generic aggregators.
    candidates.sort(key=lambda a: a["published"], reverse=True)
    candidates.sort(key=_source_rank)

    # Dedup across candidates themselves — the same underlying story routinely surfaces
    # twice (once per-person, once via the topic search) with a different link AND slightly
    # different headline wording (confirmed live: "Person A sued..." vs "Person A sued...,
    # court filing shows" — an exact-string check misses this entirely), so this needs a
    # real similarity check, not just an exact match.
    picked = []
    for c in candidates:
        if any(_headline_similarity(c["headline"], p["headline"]) >= SAME_STORY_OVERLAP_THRESHOLD
               for p in picked):
            continue
        picked.append(c)
        if len(picked) >= max_items:
            break
    return picked


SAME_STORY_OVERLAP_THRESHOLD = 0.5


def _headline_similarity(a: str, b: str) -> float:
    """Word-overlap ratio (Jaccard-style) between two headlines — catches the same story
    reworded across outlets/searches without needing exact-string equality."""
    wa = set(re.findall(r"[a-z0-9]+", a.lower()))
    wb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


# Recurring column/section branding some trade-press outlets prefix onto EVERY headline in
# that column, regardless of the actual story ("Morning Coffee: Hedge fund divorce dramas...")
# — pure noise once lifted out of the column's usual visual context, not real content, so
# safe to strip (unlike the actual headline text, which per policy we never alter/embellish).
_COLUMN_BRAND_PREFIX_RE = re.compile(
    r"^(Morning Coffee|Opening Bell|Lunch Wrap|Afternoon Coffee|Editor'?s Pick)\s*:\s*",
    re.I,
)


def _clean_headline(headline: str) -> str:
    return _COLUMN_BRAND_PREFIX_RE.sub("", headline).strip()


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _split_sentences(text: str) -> str:
    """Break a multi-sentence headline/excerpt onto separate lines so each thought reads on
    its own instead of running together as one dense paragraph (e.g. "Trump's Truth Social
    Feed Costs Wall Street $100,000 a Month. Now a Lawsuit Could Undercut It" -> two lines).
    Heuristic (sentence-end punctuation followed by a capital letter) — can occasionally
    mis-split on an abbreviation like "Mr." or "U.S.", a known tradeoff for a formatting-only
    pass with no LLM involved."""
    if not text:
        return text
    sentences = _SENTENCE_SPLIT_RE.split(text)
    return "\n\n".join(s.strip() for s in sentences if s.strip())


def generate_tweet(item: dict) -> str:
    """Zero-LLM v1 template — never asserts anything as settled fact, always includes the
    link so readers can verify it themselves. No explicit "(via X)" attribution line — the
    link's own domain (and X's link-preview card) already shows the source, so a separate
    text line just restating it is redundant clutter.

    When item["context"] (the article's own opening paragraph, see fetch_article_context) is
    available, it REPLACES the headline entirely rather than sitting alongside it. Real
    journalistic ledes are self-contained by convention — they name the subject explicitly,
    never lean on the headline for context (confirmed true in every real example seen: "The
    wife of a billionaire hedge fund mogul...", "Representative Maxine Waters..."). Keeping
    the headline as well previously ate ~80+ characters restating scene-setting the context
    already covers more specifically, which is what caused a real tweet to cut off right
    before its actual payoff ("...whether his connections to President Donald Trump played a
    role" got truncated to "...to…"). Falls back to headline-only when no context could be
    fetched."""
    headline = _split_sentences(_clean_headline(item["headline"]))
    context = _split_sentences(item.get("context") or "")
    link = item.get("link") or ""

    # X counts any URL as a fixed ~23 characters (t.co shortening) regardless of its real
    # length. Using the link's raw length here instead severely over-truncated the headline
    # whenever the link was a long Google News redirect blob (confirmed live: a real headline
    # got crushed down to "Wife of hedge…").
    link_budget = (2 + 23) if link else 0  # blank line + 23-char shortened link
    max_len = 280 - link_budget

    body = context if context else headline
    if len(body) > max_len:
        # Cut at the last whole sentence that fits, not just the last whole word -- a
        # word-boundary-only cut can (and did, confirmed live) stop partway through a
        # sentence's own thought, e.g. "...media smear campaign against World Liberty
        # Financial…" trailing off mid-clause. _split_sentences already broke this text into
        # "\n\n"-joined sentences above, so reuse that structure instead of re-splitting.
        sentences = body.split("\n\n")
        kept, total = [], 0
        for s in sentences:
            added = len(s) + (2 if kept else 0)  # account for the "\n\n" joiner
            if total + added > max_len:
                break
            kept.append(s)
            total += added
        if kept:
            body = "\n\n".join(kept) + "…"
        else:
            # Even the first sentence alone doesn't fit -- fall back to a word-boundary cut.
            body = body[:max_len].rsplit(" ", 1)[0] + "…"

    text = body
    if link:
        text += f"\n\n{link}"
    return text


_URL_RE = re.compile(r"https?://\S+")


def _effective_tweet_length(text: str) -> int:
    """X counts any URL as a fixed 23 characters (t.co shortening) regardless of its real
    length — this substitutes every URL with a 23-char placeholder before counting, so the
    result matches what X itself would count against the 280 limit."""
    return len(_URL_RE.sub("x" * 23, text))


def post_tweet(text: str, state: dict) -> bool:
    """Playwright-based posting via a saved session cookie — same mechanism as the ticker
    bot, pointed at this account's own session file."""
    # Defense in depth: verify the ACTUAL effective length right before posting, not just
    # trust generate_tweet's own budget math. A deleted real tweet was found cut off
    # mid-word with no trailing "…" — a shape neither version of generate_tweet's own
    # truncation should produce — so something in this pipeline can still generate an
    # over-budget tweet; this catches that instead of silently posting a broken one.
    effective_len = _effective_tweet_length(text)
    if effective_len > 280:
        log.error("Refusing to post — effective length %d exceeds 280 (X would silently "
                   "truncate this itself, which is likely what caused the mid-word cutoff "
                   "seen before): %r", effective_len, text[:80])
        return False
    # Defense in depth: never post a raw Google News redirect link (a Google interstitial,
    # not the real source) — run_cycle already skips an item whose link failed to resolve,
    # but this guards the posting function itself against any other path that might
    # construct a tweet with an unresolved link.
    if "news.google.com" in text:
        log.error("Refusing to post — text contains an unresolved Google News link instead "
                   "of the real source: %r", text[:80])
        return False
    if DRY_RUN:
        log.info("[DRY RUN] Would post:\n%s", text)
        return True
    if not os.path.exists(SESSION_FILE):
        log.error("No %s found — run login.py first to create one.", SESSION_FILE)
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=SESSION_FILE)
            page = context.new_page()
            page.goto("https://x.com/compose/post")
            page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=15000)
            page.fill('[data-testid="tweetTextarea_0"]', text)
            page.click('[data-testid="tweetButton"]')
            page.wait_for_timeout(3000)
            browser.close()
        log.info("Posted (%d chars):\n%s", len(text), text)
        return True
    except Exception as e:
        log.error("Post failed: %s", e)
        return False


# Confirmed live this was a real problem, not just theoretical: a quote-tweet posted with
# no added text at all reads as a bare, contentless repost (worse when the quoted tweet is
# itself just an image) — not a fabricated claim, just a short reaction cue, never a
# restatement/summary of the quoted tweet's actual content.
QUOTE_TWEET_OPENERS = [
    "Spotted this:",
    "Well, this is a lot:",
    "This just happened:",
    "Worth a look:",
    "Some tea:",
    "This is wild:",
]


def post_quote_tweet(tweet_url: str, state: dict) -> bool:
    """Quote-tweets a genuine, already-public tweet via X's own documented intent URL
    (x.com/intent/tweet?url=...) — this is X's officially supported sharing flow, not
    scraping. Adds a short, neutral reaction opener (never a summary/restatement of the
    quoted tweet's own content) so the post never reads as a bare, textless repost — the
    quoted tweet (someone else's real reaction/gossip about a titan, never their own tweet,
    see _is_self_authored) still carries all the actual substance."""
    opener = random.choice(QUOTE_TWEET_OPENERS)
    if DRY_RUN:
        log.info("[DRY RUN] Would quote-tweet %r + %s", opener, tweet_url)
        return True
    if not os.path.exists(SESSION_FILE):
        log.error("No %s found — run login.py first to create one.", SESSION_FILE)
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=SESSION_FILE)
            page = context.new_page()
            # Both text and url go through the intent URL's own params -- confirmed live
            # this is the ONE reliable way to get both the opener text AND the quoted-tweet
            # embed card in the same post. Do NOT also call .fill() on the textarea: it does
            # a select-all-and-replace on X's contenteditable box, which either duplicated
            # the opener (when it landed after the url param's text already rendered) or, worse,
            # wiped out the still-loading quote-tweet embed entirely (when it landed before
            # the async card render finished) -- confirmed live as bare, sourceless posts.
            intent_url = (f"https://x.com/intent/tweet?url={quote(tweet_url, safe='')}"
                          f"&text={quote(opener, safe='')}")
            page.goto(intent_url)
            page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=15000)
            # Give the async quote-tweet embed card time to actually attach before
            # submitting -- clicking too early risks posting before it's rendered.
            page.wait_for_timeout(3000)
            page.click('[data-testid="tweetButton"]')
            page.wait_for_timeout(3000)
            browser.close()
        log.info("Quote-tweeted (%r): %s", opener, tweet_url)
        return True
    except Exception as e:
        log.error("Quote-tweet failed: %s", e)
        return False


def run_cycle():
    state = load_state()
    items = find_gossip_items(state, max_items=MAX_POSTS_PER_CYCLE)
    if not items:
        log.info("No fresh, gossip-worthy, unused story found this cycle.")
        return

    posted = 0
    for i, item in enumerate(items):
        if item.get("is_tweet"):
            # Already resolved to a genuine tweet permalink during discovery — no article
            # context concept applies here, we're quoting the tweet itself, not summarizing it.
            ok = post_quote_tweet(item["link"], state)
        else:
            # Only resolve the links of items actually being posted, not every candidate
            # scanned — each resolution is an extra request to Google's redirect-decode
            # endpoint.
            item["link"] = _resolve_google_news_url(item.get("link", ""))
            if "news.google.com" in item["link"]:
                # _resolve_google_news_url falls back to the original (unresolved) redirect
                # link on any failure, by design, so a transient failure never blocks a post.
                # But posting that raw Google URL instead of the real source is exactly what
                # must never happen — skip just this one item and try the next candidate,
                # rather than aborting the whole cycle over a single resolution failure.
                log.warning("Skipping (Google News link failed to resolve to a real source "
                            "URL): %s", item["headline"][:70])
                continue
            # Same principle for the context fetch — it's what makes clicking the link
            # unnecessary, but only worth the extra request for items actually being posted.
            item["context"] = fetch_article_context(item["link"])
            ok = post_tweet(generate_tweet(item), state)
        if not ok:
            break  # a post failure (e.g. session expired) — don't keep trying the rest
        posted += 1
        if not DRY_RUN:
            # Only mark seen / persist state on a real post — a dry run must have zero
            # lasting effect, or an item it just previewed would be silently blocked from
            # actually being posted on the next real run.
            state.setdefault("gossip_seen", {})[item["fingerprint"]] = today()
            save_state(state)
            if i < len(items) - 1:
                # Brief pacing gap between multiple posts in one cycle so they don't land
                # seconds apart — reads as paced coverage, not a bot dumping a queue.
                time.sleep(POST_PACING_SECONDS)

    log.info("Posted %d/%d item(s) this cycle (cap: %d).", posted, len(items), MAX_POSTS_PER_CYCLE)


if __name__ == "__main__":
    run_cycle()

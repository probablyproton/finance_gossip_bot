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
#
# LIVING LIST -- expand this whenever a real example (missed gossip OR a new false-positive
# spam pattern) shows up; don't treat this as finished. Grouped by category so new additions
# land in the right place. See "Gossip-signal vocabulary" in DESIGN.md for the running log of
# real examples that justified each addition/removal.
#
# Deliberately excluded: bare "fight(s)" -- far too common in business/legal/regulatory
# headlines ("fights the lawsuit", "fights regulators", "fights a takeover bid"), the exact
# false-positive shape already learned twice over (see "resigns"/"steps down"/"split" above).
# "brawl"/"altercation"/"confrontation" cover the physical-conflict framing instead, since
# those almost never appear in a business-deal headline.
GOSSIP_SIGNAL_RE = re.compile(
    r"\b("
    # Relationship / marriage drama
    r"divorc\w+|affair|cheat(?:s|ed|ing)?|mistress|infidelit\w+|"
    r"break-?up|broke\s+up|split(?:s|ting)?\s+(?:from|with)|estranged|separat(?:ed|ion)|"
    r"custody|prenup|alimony|ex-wife|ex-husband|love\s+child|"
    # Vices / personal struggles
    r"addiction|rehab|relapse[ds]?|overdose[ds]?|sober(?:riety)?|alcoholi\w+|"
    r"substance\s+abuse|dui|intervention|breakdown|"
    # Legal / criminal, personal -- "fined" added per explicit request (a court/regulatory
    # fine levied on the person themselves, not a routine corporate line item)
    r"arrest(?:ed)?|jail(?:ed)?|prison|indict\w+|charged|fined|mugshot|subpoena\w*|"
    r"restraining\s+order|assault(?:ed)?|harass\w+|misconduct|abuse[ds]?|"
    # Family drama
    r"disown\w+|inheritance\s+battle|estate\s+battle|will\s+dispute|"
    # Interpersonal conflict -- bare "fight(s)" still deliberately excluded (see note above),
    # but these unambiguous multi-word phrasings for an actual fight are safe to add: they
    # essentially never appear in a business/regulatory headline the way "fights the lawsuit"
    # does.
    r"feud|rivalry|clash(?:es)?|brawl|blow-?up|meltdown|showdown|spat|"
    r"snub(?:s|bed)?|confrontation|altercation|shouting\s+match|screaming\s+match|"
    r"fist\W?fight|"
    # Public embarrassment / secret exposure / general oddity ("weird things" per explicit
    # request -- kept to the rarer, more specifically tabloid-flavored word to limit noise)
    r"scandal|drama|bizarre|expos(?:e|es|ed)|leaked?\s+(?:photos?|texts?|messages?|audio|video)|"
    r"caught|spotted\s+(?:with|dating)|fling|tryst|hookup|"
    # Career/legal conflict carried over from before -- still genuinely ambiguous between
    # personal and professional, disambiguated by ROUTINE_BUSINESS_RE below
    r"lawsuit|sues?|sued|fired|ousted|slams?|blasts?(?:ed)?|accus\w+|shake-?up|rift|fallout"
    r")\b",
    re.I,
)

# Negative filter: reject a headline even if it matched a gossip-signal word above, when the
# same headline is clearly routine BUSINESS/political/market news dressed up in dramatic
# language, rather than personal drama. Confirmed live: "Trump And Musk Have Now Turned Their
# Bitter Feud Into A $100 Million Alliance" matched "feud" but is a business/political deal
# story, not gossip. This is deliberately the ONLY disambiguation mechanism for words like
# "feud"/"rivalry"/"clash"/"lawsuit" that are genuinely ambiguous between personal and
# professional conflict -- a real personal-drama headline essentially never also reports an
# earnings figure, a stock move, or a deal outcome in the same breath.
#
# Broadened beyond just deal/alliance framing (per "be much stricter") to also catch routine
# financial-reporting and regulatory/market language -- these were previously only screened
# out incidentally by GOSSIP_SIGNAL_RE not matching at all, which isn't reliable once
# "lawsuit"/"fired"/"fallout" etc. are in the positive list.
ROUTINE_BUSINESS_RE = re.compile(
    r"\b(alliance|merger|acquisition|partnership|joint\s+venture|venture|ipo|buyout|"
    r"takeover|tie-?up|team(?:s|ed)?\s+up|joins?\s+forces|stake|funding\s+round|"
    r"board\s+seat|"
    r"earnings|quarterly\s+results?|revenue|q[1-4]\s+results?|guidance|dividend|buyback|"
    r"shares?\s+(?:rose|fell|surged?|dropped?|jumped?|rallied|slid|slumped)|stock\s+price|"
    r"market\s+cap|valuation|layoffs?|job\s+cuts|"
    r"tariff|antitrust|interest\s+rate|monetary\s+policy|rate\s+(?:hike|cut|decision))\b",
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


def _prune_old_signatures(signatures: list, max_age_days: int) -> list:
    """Same pruning idea as _prune_date_keyed_dict, but for the posted_signatures LIST (see
    _story_already_covered) rather than a fingerprint dict."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=max_age_days)).isoformat()
    return [s for s in signatures if s.get("date", "") >= cutoff]


def _is_gossip_worthy(headline: str) -> bool:
    return bool(GOSSIP_SIGNAL_RE.search(headline)) and not ROUTINE_BUSINESS_RE.search(headline)


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


# A single Unicode ellipsis char ("…", one codepoint that renders as three dots) is ALWAYS a
# truncation signal by itself, optionally followed by one more literal period (the real
# observed case "…." -- renders as four dots). Separately, 2+ literal ASCII periods in a row
# ("..", "...") are also always truncation. A LONE single "." is deliberately excluded from
# both -- that's just a normal, complete sentence ending. (First version of this regex
# required 2+ characters from a combined set, which wrongly missed a bare trailing "…" since
# that's only one character -- confirmed by direct test before fixing.)
_TRAILING_TRUNCATION_RE = re.compile(r"(…\.?|\.{2,})\s*$")


def _strip_source_truncation(text: str) -> str:
    """Some sites' static HTML (what a plain requests.get sees, with no JS execution) only
    ever contains a truncated TEASER paragraph -- the rest loads dynamically via JS, or is
    gated behind a paywall -- ending in the SOURCE'S OWN trailing ellipsis, not ours.
    Confirmed live: a post still ended mid-thought ("...though there's been plenty examples
    of that in the past ….") even after generate_tweet's own sentence-boundary truncation
    fix, because the fetched text was already incomplete before generate_tweet ever touched
    it -- and the exact trailing shape ("….", an ellipsis char plus one more literal period)
    didn't match a naive check for just "..." or "…" alone, so the first version of this
    function missed it too; confirmed by direct test before fixing. Keeps only the complete
    sentences before the trailing ellipsis; if none are complete, returns '' so the caller
    falls back to the headline instead of posting a fragment."""
    if not _TRAILING_TRUNCATION_RE.search(text):
        return text
    body = _TRAILING_TRUNCATION_RE.sub("", text).rstrip()
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(body) if s.strip()]
    complete = sentences[:-1]  # the last one is presumably the truncated one
    return " ".join(complete)


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
            if len(text) < 80:
                continue
            # Validate the ACTUAL slice we're about to output, not the full raw <p> text --
            # confirmed live this was a real bug: some sites' markup dumps their entire
            # category/nav list into one oversized <p> block ahead of any real prose (e.g.
            # "Search Home Categories Dallas ... Crime Education Business ..."). The full
            # block could satisfy the function-word-density check on the strength of real
            # sentences buried further in, while the first max_chars characters -- what
            # actually gets posted -- were 100% nav junk with zero function words in it.
            candidate = text[:max_chars]
            if _NAV_JUNK_RE.search(candidate):
                continue
            if len(_FUNCTION_WORDS_RE.findall(candidate)) < _MIN_FUNCTION_WORD_HITS:
                continue  # reads like a nav/breadcrumb block, not a real sentence
            candidate = _strip_source_truncation(candidate)
            if not candidate:
                continue  # this slice was ENTIRELY a truncated teaser -- try the next <p>
            return candidate
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
# Query terms kept in sync with GOSSIP_SIGNAL_RE's actual (personal-drama-focused) vocabulary
# -- previously included "resigns", which was removed from the positive filter as a false-
# positive magnet, so it was wastefully fetching candidates that would just get discarded.
INDUSTRY_TOPIC_QUERIES = [
    '("hedge fund" OR "private equity" OR "Wall Street" OR "billionaire investor" OR '
    '"fund manager") (divorce OR affair OR cheating OR addiction OR rehab OR arrested OR '
    'jailed OR indicted OR fined OR feud OR scandal OR meltdown)',
    '("hedge fund" OR "fund founder" OR "fund CEO" OR "fund co-founder" OR "billionaire") '
    '(court battle OR settlement OR subpoena OR SEC probe OR fraud)',
]

# Google News full-text search matches on the ARTICLE'S BODY, not just its headline -- so a
# query term like "Wall Street" or "addiction" can match because it appears once, in passing,
# somewhere in a totally unrelated piece. Confirmed live: a WSJ "Future of Everything" column
# about a general kratom-addiction societal trend (headline never mentions any person, role,
# or company at all) matched on "addiction" and got posted. Since _is_gossip_worthy only ever
# checks the HEADLINE text, a topic-search hit additionally has to actually reference a
# finance-industry person/role IN ITS OWN HEADLINE -- not rely on the query's terms having
# matched somewhere in the body. Per-person search doesn't need this: a name match already
# guarantees the headline (or at least the story) is about that specific person.
_FINANCE_PERSON_ROLE_RE = re.compile(
    r"\b(CEO|billionaire|hedge fund|private equity|investor|co-founder|founder|chairman|"
    r"executive|fund manager|Wall Street)\b", re.I,
)


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


def _passes_gossip_filters(a: dict, cutoff: datetime.datetime, seen: dict, signatures: list,
                            person: str) -> bool:
    if not a.get("published") or a["published"] < cutoff:
        return False
    if not _is_gossip_worthy(a["headline"]):
        return False
    if (a.get("source") or "").lower() in BLOCKLIST_SOURCES:
        return False
    if _dedup_key(a) in seen:
        return False
    return not _story_already_covered(a["headline"], person, signatures)


def find_gossip_items(state: dict, max_items: int = 1) -> list[dict]:
    """Up to max_items freshest, real, gossip-worthy, not-yet-used stories — either about
    someone on the named watchlist, or surfaced by a broad industry-wide topic search that
    needs no pre-known name at all (see INDUSTRY_TOPIC_QUERIES). One fetch pass regardless
    of max_items — callers wanting multiple posts in a cycle should call this once with
    max_items=N, not call a single-item version N times (would re-fetch all ~59 queries
    per post)."""
    seen = state.setdefault("gossip_seen", {})
    _prune_date_keyed_dict(seen, GOSSIP_SEEN_MEMORY_DAYS)
    # Content-based dedup, separate from (and in addition to) the exact-fingerprint dict
    # above -- catches the SAME underlying story resurfacing via a different link, a
    # different search query, or as a tweet instead of an article, none of which share a
    # fingerprint with each other. Persisted across cycles/runs, not just within one call
    # (unlike the within-cycle-only near-duplicate collapse further below).
    signatures = _prune_old_signatures(state.setdefault("posted_signatures", []), GOSSIP_SEEN_MEMORY_DAYS)
    state["posted_signatures"] = signatures
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=NEWS_FRESHNESS_HOURS)

    candidates = []

    for name in TITANS_WATCHLIST:
        for a in fetch_person_news(name):
            if _passes_gossip_filters(a, cutoff, seen, signatures, name):
                candidates.append({**a, "person": name, "fingerprint": _dedup_key(a)})

    for query in INDUSTRY_TOPIC_QUERIES:
        for a in fetch_topic_news(query):
            if not _FINANCE_PERSON_ROLE_RE.search(a["headline"]):
                continue  # query matched somewhere in the article body, not the headline --
                          # see _FINANCE_PERSON_ROLE_RE for why that's not good enough alone
            if _passes_gossip_filters(a, cutoff, seen, signatures, "industry-wide"):
                candidates.append({**a, "person": "industry-wide", "fingerprint": _dedup_key(a)})

    for name in TITANS_WATCHLIST:
        for a in fetch_tweet_candidates(name):
            if not a.get("published") or a["published"] < cutoff:
                continue
            if not _is_gossip_worthy(a["headline"]):
                continue
            if _story_already_covered(a["headline"], name, signatures):
                continue  # same story as an already-posted article/tweet -- skip before
                          # paying for the resolve + oEmbed calls below
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


def _headline_containment(a: str, b: str) -> float:
    """Like _headline_similarity, but divides by the SMALLER word-set instead of the union --
    robust to comparing texts of very different length/style (a terse news headline vs. a
    tweet's own, much wordier phrasing of the same news), where Jaccard under-counts a real
    duplicate just because one side has extra words. Confirmed by direct test: a genuine
    same-story pair (a news headline vs. the tweet reporting that exact story) scored only
    0.44-0.48 on Jaccard -- under the within-cycle 0.5 threshold -- but 0.70-1.0 on
    containment. Only used for the cross-cycle/cross-medium check below, paired with a
    person-name anchor, since containment alone is too permissive on its own (two DIFFERENT
    people's similarly-phrased divorce headlines also score ~0.7)."""
    wa = set(re.findall(r"[a-z0-9]+", a.lower()))
    wb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


# Cross-cycle/cross-medium dedup thresholds (see _story_already_covered) -- deliberately
# separate from SAME_STORY_OVERLAP_THRESHOLD (the within-cycle, Jaccard-based check above,
# left unchanged since it's already proven on same-cycle candidates, which tend to be
# similarly-worded to begin with). Two tiers: a looser, containment-based bar when both sides
# name the SAME specific titan (safe to be generous -- if two headlines about the identical
# named person share this much vocabulary, it's the same event), and a much stricter bar
# when the person is unknown on either side (an industry-wide topic hit, or a mismatched
# name) -- confirmed by direct test that the loose bar alone would wrongly treat two
# DIFFERENT people's similarly-phrased divorce headlines as the same story.
SAME_STORY_CONTAINMENT_THRESHOLD = 0.6
SAME_STORY_STRICT_CONTAINMENT_THRESHOLD = 0.85


def _is_specific_person(person: str) -> bool:
    return bool(person) and person != "industry-wide"


def _story_already_covered(headline: str, person: str, signatures: list) -> bool:
    """Checked against EVERY story already posted in the last GOSSIP_SEEN_MEMORY_DAYS days
    (state["posted_signatures"]), regardless of cycle or medium. This is the actual fix for
    "the same story posted twice, once as an article link and once as someone tweeting about
    it" -- an article's link-based fingerprint and a tweet's status-ID fingerprint never
    collide with each other even when they're about the literal same news, so fingerprint-
    only dedup can't catch it."""
    for s in signatures:
        same_person = (_is_specific_person(person) and _is_specific_person(s.get("person", ""))
                        and person.lower() == s["person"].lower())
        threshold = SAME_STORY_CONTAINMENT_THRESHOLD if same_person else SAME_STORY_STRICT_CONTAINMENT_THRESHOLD
        if _headline_containment(headline, s["headline"]) >= threshold:
            return True
    return False


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


# Any of these appearing in text about to be posted means something upstream broke -- an
# unformatted template placeholder, a stray repr() of None, or similar -- never a real,
# intentional part of a tweet. Belt-and-braces against "part of a prompt/template leaking
# into a post," which showed up as a real (if different-shaped) problem before.
_TEMPLATE_ARTIFACT_MARKERS = ("{", "}", "None", "undefined", "PLACEHOLDER_HANDLE", "TODO")


class ComposeValidationError(Exception):
    """Raised when the text about to be posted (or, for a quote-tweet, what X's compose box
    actually rendered) fails a sanity check -- distinct from a hard automation/infra failure
    (session expired, browser crash) so callers can skip just this one candidate instead of
    aborting the whole posting cycle."""


def _validate_compose_text(text: str, *, leak_check: str | None = None) -> None:
    """Shared, rigid pre-post gate for anything about to go out, whether it's a plain tweet
    or what got read back from X's own compose box after a quote-tweet intent URL rendered.
    Raises ComposeValidationError with a specific reason instead of returning a bool, so the
    log always says exactly what was wrong. leak_check, when given (a quote-tweet's own
    tweet_url), catches the case where X failed to convert it into an embed card and just
    left the raw URL sitting in the text as plain text instead."""
    if not text or not text.strip():
        raise ComposeValidationError("empty text")
    effective_len = _effective_tweet_length(text)
    if effective_len > 280:
        raise ComposeValidationError(f"effective length {effective_len} exceeds 280")
    if "news.google.com" in text:
        raise ComposeValidationError("contains an unresolved Google News link instead of "
                                      "the real source")
    for marker in _TEMPLATE_ARTIFACT_MARKERS:
        if marker in text:
            raise ComposeValidationError(f"contains template/placeholder artifact {marker!r}")
    # Catches "Worth a look: ... Worth a look:" style duplication -- the same opener/line
    # appearing twice in the same short post, which is exactly what happened when the
    # intent-URL prefill raced our own scripted interaction with the compose box.
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) != len(set(lines)):
        raise ComposeValidationError("contains a duplicated line")
    if leak_check and leak_check in text:
        raise ComposeValidationError(
            "quoted tweet URL is sitting in the compose text as plain text -- the embed "
            "card likely failed to attach")


def post_tweet(text: str, state: dict) -> bool:
    """Playwright-based posting via a saved session cookie — same mechanism as the ticker
    bot, pointed at this account's own session file."""
    # Defense in depth: re-verify the text against every known failure mode right before
    # posting, not just trust generate_tweet's own budget math. A deleted real tweet was once
    # found cut off mid-word with no trailing "…" -- a shape generate_tweet's own truncation
    # should never produce -- so something in this pipeline can still generate a broken
    # tweet; this catches that instead of silently posting it.
    try:
        _validate_compose_text(text)
    except ComposeValidationError as e:
        log.error("Refusing to post — %s: %r", e, text[:80])
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
#
# Category-matched instead of one flat generic pool -- "Worth a look:"/"Spotted this:" on
# EVERY post reads as filler, not engaging. The category is derived from which
# GOSSIP_SIGNAL_RE word actually matched the real headline, so this is still "aggregate,
# never fabricate": we're only describing what KIND of already-published story this is, the
# same signal that qualified it as gossip in the first place, never inventing new detail.
# First matching category wins; keep the generic pool ONLY as a last-resort fallback.
_OPENER_CATEGORIES = [
    (re.compile(r"\b(divorc\w+|affair|cheat(?:s|ed|ing)?|mistress|infidelit\w+|break-?up|"
                r"broke\s+up|split(?:s|ting)?\s+(?:from|with)|estranged|separat(?:ed|ion)|"
                r"custody|prenup|alimony)\b", re.I),
     ["Marriage drama:", "Splitsville:", "Love (and money), gone wrong:"]),
    (re.compile(r"\b(addiction|rehab|relapse[ds]?|overdose[ds]?|sober(?:riety)?|alcoholi\w+|"
                r"substance\s+abuse|dui|intervention)\b", re.I),
     ["A tough one:", "Personal struggle making headlines:", "Not an easy read:"]),
    (re.compile(r"\b(arrest(?:ed)?|jail(?:ed)?|prison|indict\w+|charged|fined|mugshot)\b", re.I),
     ["Legal trouble:", "Uh oh:", "Not a good day in court:"]),
    (re.compile(r"\b(meltdown|blow-?up|showdown|brawl|confrontation|altercation|"
                r"shouting\s+match|screaming\s+match|fist\W?fight|bizarre)\b", re.I),
     ["Things got heated:", "Public meltdown alert:", "This escalated fast:"]),
    (re.compile(r"\b(scandal|expos(?:e|es|ed)|leaked?\s+(?:photos?|texts?|messages?|audio|video)|"
                r"caught|spotted\s+(?:with|dating)|fling|tryst|hookup)\b", re.I),
     ["Scandal watch:", "This is getting messy:", "Well, this came out:"]),
    (re.compile(r"\b(lawsuit|sues?|sued|fired|ousted|feud|rivalry|clash(?:es)?)\b", re.I),
     ["Courtroom drama:", "Legal drama:", "This just got messy:"]),
]
_GENERIC_OPENERS = ["Finance gossip alert:", "The rumor mill is turning:", "This is making the rounds:"]


def _pick_opener(headline: str) -> str:
    for pattern, openers in _OPENER_CATEGORIES:
        if pattern.search(headline):
            return random.choice(openers)
    return random.choice(_GENERIC_OPENERS)  # shouldn't normally happen -- is_gossip_worthy
                                             # already required a GOSSIP_SIGNAL_RE match


def post_quote_tweet(tweet_url: str, headline: str, state: dict) -> bool | str:
    """Quote-tweets a genuine, already-public tweet via X's own documented intent URL
    (x.com/intent/tweet?url=...) — this is X's officially supported sharing flow, not
    scraping. Adds a short, category-matched reaction opener (never a summary/restatement of
    the quoted tweet's own content) so the post never reads as a bare, textless repost — the
    quoted tweet (someone else's real reaction/gossip about a titan, never their own tweet,
    see _is_self_authored) still carries all the actual substance.

    Returns True (posted), False (a real failure -- caller should stop the whole cycle), or
    the string "skip" (this specific candidate's compose render failed validation -- caller
    should move on to the next candidate, not mark this one seen, and not abort the cycle)."""
    opener = _pick_opener(headline)
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
            # Rigid check: read back what X's compose box ACTUALLY rendered, rather than
            # trusting the URL params did what we asked. This is what would have caught both
            # prior incidents (duplicated opener text, and the embed silently failing to
            # attach leaving the bare URL as text) before they ever posted.
            rendered = page.inner_text('[data-testid="tweetTextarea_0"]').strip()
            try:
                _validate_compose_text(rendered, leak_check=tweet_url)
            except ComposeValidationError as e:
                browser.close()
                log.warning("Skipping quote-tweet (compose render failed validation, not a "
                            "hard error) — %s | rendered=%r | %s", e, rendered[:80], tweet_url)
                return "skip"
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
            ok = post_quote_tweet(item["link"], item["headline"], state)
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
        if ok == "skip":
            # This specific candidate's compose render failed validation (see
            # post_quote_tweet) -- not a hard/infra failure, so try the next candidate
            # instead of aborting the whole cycle, and don't mark it seen since it never
            # actually posted.
            continue
        if not ok:
            break  # a real failure (e.g. session expired) — don't keep trying the rest
        posted += 1
        if not DRY_RUN:
            # Only mark seen / persist state on a real post — a dry run must have zero
            # lasting effect, or an item it just previewed would be silently blocked from
            # actually being posted on the next real run.
            state.setdefault("gossip_seen", {})[item["fingerprint"]] = today()
            # Content-based signature, kept alongside the fingerprint dict -- see
            # _story_already_covered. This is what stops the SAME story from posting again
            # later via a different link or as a tweet, which a fingerprint alone can't catch
            # since a link-based and a tweet-ID-based fingerprint never collide.
            state.setdefault("posted_signatures", []).append({
                "headline": item["headline"], "person": item.get("person", ""), "date": today(),
            })
            save_state(state)
            if i < len(items) - 1:
                # Brief pacing gap between multiple posts in one cycle so they don't land
                # seconds apart — reads as paced coverage, not a bot dumping a queue.
                time.sleep(POST_PACING_SECONDS)

    log.info("Posted %d/%d item(s) this cycle (cap: %d).", posted, len(items), MAX_POSTS_PER_CYCLE)


if __name__ == "__main__":
    run_cycle()

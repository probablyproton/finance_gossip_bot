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
import random
import logging
import datetime
import tempfile
import requests
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

STATE_FILE   = "state.json"
SESSION_FILE = "twitter_session.json"

# ── Content policy filters (see DESIGN.md) ──────────────────────────────────────────────

# Positive filter: the headline itself has to signal drama/conflict/business intrigue —
# this is the opposite of the ticker bot's "reject clickbait" filter, since here the spicy
# framing IS the content. Still requires a real published article behind it.
GOSSIP_SIGNAL_RE = re.compile(
    r"\b(feud|lawsuit|sues?|sued|fired|resigns?|steps?\s+down|ousted|slams?|blasts?|"
    r"backlash|scandal|divorce|split(?:s|ting)?|rivalry|clash(?:es)?|criticiz\w+|accus\w+|"
    r"drama|shake-?up|secret|leaked?|expos(?:e|es|ed)|rift|fallout|controvers\w+|apolog\w+|"
    r"blow-?up|meltdown|showdown|spat|snub(?:s|bed)?|brawl|blast(?:s|ed)?)\b",
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


def today() -> str:
    return datetime.date.today().isoformat()


def _prune_date_keyed_dict(d: dict, max_age_days: int):
    """Drops entries whose ISO-date value is older than max_age_days — same pattern as the
    ticker bot's dedup memory, so state.json doesn't carry fingerprints forever."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=max_age_days)).isoformat()
    for key in [key for key, seen_date in d.items() if seen_date < cutoff]:
        del d[key]


def _is_gossip_worthy(headline: str) -> bool:
    return bool(GOSSIP_SIGNAL_RE.search(headline))


def fetch_person_news(name: str, max_items: int = 10) -> list[dict]:
    """Google News RSS search for a named individual — same feed pattern the ticker bot
    used per-symbol, just with a person's name as the query instead of a company name."""
    url = f"https://news.google.com/rss/search?q={quote(name)}&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = requests.get(url, timeout=10, verify=False, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        log.warning("News fetch failed for %s: %s", name, e)
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


def find_gossip_item(state: dict) -> dict | None:
    """Freshest, real, gossip-worthy, not-yet-used story about anyone on the watchlist."""
    if not TITANS_WATCHLIST:
        log.warning("TITANS_WATCHLIST is empty — nothing to look for.")
        return None

    seen = state.setdefault("gossip_seen", {})
    _prune_date_keyed_dict(seen, GOSSIP_SEEN_MEMORY_DAYS)
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=NEWS_FRESHNESS_HOURS)

    candidates = []
    for name in TITANS_WATCHLIST:
        for a in fetch_person_news(name):
            if not a.get("published") or a["published"] < cutoff:
                continue
            if not _is_gossip_worthy(a["headline"]):
                continue
            if (a.get("source") or "").lower() in BLOCKLIST_SOURCES:
                continue
            fp = f"{name.lower()}:{a['headline'].strip().lower()}"
            if fp in seen:
                continue
            candidates.append({**a, "person": name, "fingerprint": fp})

    if not candidates:
        return None

    # Freshest first, then float credible sources above generic aggregators.
    candidates.sort(key=lambda a: a["published"], reverse=True)
    candidates.sort(key=lambda a: 0 if (a.get("source") or "").lower() in PREFERRED_SOURCES else 1)
    return candidates[0]


def generate_tweet(item: dict) -> str:
    """Zero-LLM v1 template — always attributes, never asserts the headline as settled
    fact, always includes the link so readers can verify it themselves."""
    source = item.get("source") or "a recent report"
    headline = item["headline"]
    link = item["link"]

    prefix = f"Word from {source}: "
    suffix = f"\n\n{link}"
    max_len = 280 - len(prefix) - len(suffix)
    if len(headline) > max_len:
        headline = headline[:max_len].rsplit(" ", 1)[0] + "…"
    return prefix + headline + suffix


def post_tweet(text: str, state: dict) -> bool:
    """Playwright-based posting via a saved session cookie — same mechanism as the ticker
    bot, pointed at this account's own session file."""
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


def run_cycle():
    state = load_state()
    item = find_gossip_item(state)
    if not item:
        log.info("No fresh, gossip-worthy, unused story found this cycle.")
        return

    tweet = generate_tweet(item)
    if post_tweet(tweet, state):
        state.setdefault("gossip_seen", {})[item["fingerprint"]] = today()
        save_state(state)


if __name__ == "__main__":
    run_cycle()

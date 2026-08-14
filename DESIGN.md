# Finance Gossip Bot — Design Doc (v1 draft)

## Twitter/X account

**Placeholder — not yet decided.** Everywhere below, `@PLACEHOLDER_HANDLE` stands in for
whatever handle this account actually launches under. Nothing in this repo assumes a
specific name yet.

## Mission

Post about the drama, feuds, and power struggles of the financial world's biggest names —
CEOs, billionaires, hedge fund managers, dealmakers — the "gossip column" of finance
Twitter, distinct from the ticker-price-and-earnings bot this project forked from.

## Content policy (the part that actually matters)

Gossip about real, named, often litigious people is a materially higher-stakes content
category than stock-price commentary. One inaccurate or exaggerated claim about a real
person is a real legal exposure in a way "the market could be pricing this in" never was.
So the core discipline carried over from the original bot — never invent or infer a fact
not present in the source — applies here even more strictly:

1. **Aggregate, never fabricate.** Every post summarizes an already-published story from a
   real, named outlet, with a link to it. The bot (and any LLM step) never originates a
   claim, quote, or scenario of its own.
2. **Attribute, never assert.** Framing is always "according to *Outlet*" / "as first
   reported by *Outlet*" — never stated as settled fact, even when the source itself is
   confident. This protects against a source turning out to be wrong later.
3. **Source quality gate.** Only pulls from credible business/gossip press — Bloomberg,
   Reuters, WSJ, Fortune, Business Insider, Forbes, CNBC, Axios, Puck, Semafor, Page Six
   Business, NY Post Business, Financial Times, Vanity Fair, The Information. A blocklist
   keeps out satire sites, fabrication mills, and pure stock-tout content farms.
4. **Business-relevant, not tabloid-invasive.** In scope: boardroom drama, executive
   departures/firings, lawsuits, feuds, rivalries, PR disasters, wealth/lifestyle stories
   tied to their public business role. Out of scope: anything that's pure personal-life
   intrusion with no business angle, health details, or family details not central to a
   genuine business story.
5. **No financial advice, no market-moving insinuation.** Never frame a story as investment
   guidance, and never imply insider-trading-adjacent claims without the source itself
   making that claim.

If a story doesn't clear all five, it doesn't get posted — better to post less than to post
something that reads as a fabricated or exaggerated claim about a real person.

## Initial watchlist

A starter list of named individuals (the people-equivalent of the ticker bot's
`EU_WATCHLIST`/`US_WATCHLIST`), configured via `TITANS_WATCHLIST` in `my.env` — easy to
expand or trim later:

**Hedge fund managers:** Ray Dalio, Ken Griffin, Bill Ackman, Carl Icahn, David Tepper,
Steve Cohen, Israel Englander, Paul Tudor Jones, Daniel Loeb, David Einhorn,
Stanley Druckenmiller, George Soros, John Paulson, Chase Coleman, Philippe Laffont,
Cliff Asness, Seth Klarman, Michael Burry, Bill Hwang, Nelson Peltz, Larry Robbins,
Howard Marks, Jim Chanos

**Bank CEOs:** Jamie Dimon, David Solomon, Jane Fraser, Brian Moynihan, Charlie Scharf

**Private equity / asset management:** Larry Fink, Stephen Schwarzman, Henry Kravis,
David Rubenstein, Jonathan Gray, Marc Rowan

**Policymakers:** Jerome Powell, Janet Yellen

**Billionaire investors / tech-finance crossover:** Warren Buffett, Bernard Arnault,
Elon Musk, Jeff Bezos, Mark Zuckerberg, Sam Altman, Jensen Huang, Cathie Wood, Bill Gates,
Peter Thiel, Marc Andreessen, Chamath Palihapitiya, Michael Saylor

**Crypto (frequent scandal/legal drama):** Sam Bankman-Fried, Changpeng Zhao, Do Kwon

**Legendary-drama / disgraced figures:** Adam Neumann, Elizabeth Holmes, Rupert Murdoch

**Quant funds:** John Overdeck, David Siegel (Two Sigma co-founders — added after their
$6.2B divorce case, see below, exposed the gap this list alone can't close)

57 names total. Note: a much larger watchlist means proportionally more Google News RSS
requests per cycle (one per name) — still cheap/fast individually, but worth knowing if
run frequency ever needs tuning against rate limits.

### Why a name list alone isn't enough

A real test case: the NY Post story on John Overdeck (Two Sigma co-founder) and his wife
Laura's $6.2B divorce battle was exactly the kind of story this account should catch — but
he wasn't on the watchlist at the time, because no fixed list of names, however broad, can
anticipate every billionaire who might end up in a headline-worthy dispute. Two Sigma alone
manages $80B; there are hundreds of similarly-sized funds and firms whose principals aren't
individually famous enough to make an initial list.

So `gossip_bot.py` runs two parallel search mechanisms, not one:
1. **Per-person search** — the named watchlist above, same as before.
2. **Industry-wide topic search** (`INDUSTRY_TOPIC_QUERIES`) — broad Google News searches
   like `("hedge fund" OR "private equity" OR "Wall Street") (divorce OR lawsuit OR fired OR
   scandal OR ...)`, with no specific name required at all. This is what actually closes the
   blind-spot problem, not just a bigger list. Modeled directly on the ticker bot's
   evergreen-opinion feature, which used the same broad-topic-query pattern for
   sector-level content instead of per-ticker-only search.

A topic-search hit is tagged `person: "industry-wide"` rather than a specific name, and
dedup keys off the article's link rather than a name+headline pair, since a topic hit has no
pre-known person to key off of.

## Cadence and multi-post

Runs on GitHub Actions' own native cron (`schedule: - cron: "*/15 * * * *"` in `bot.yml`) —
no external dispatcher needed, unlike the ticker bot's model. Each cycle can post up to
`MAX_POSTS_PER_CYCLE` (default 3) distinct stories in one run, found from a single fetch pass
(not by re-fetching per post), with a `POST_PACING_SECONDS` gap (default 45s) between
successive posts in the same cycle so they don't land seconds apart. A manual
`workflow_dispatch` run still has the dry-run checkbox; a scheduled cron run always runs live.

Cross-candidate dedup uses a real word-overlap similarity check (`_headline_similarity`,
threshold 0.5), not exact-string matching — confirmed live that the same story routinely
surfaces via both the per-person and topic searches with slightly different wording
("Person A sued..." vs "Person A sued..., court filing shows"), which an exact match misses
entirely.

## Architecture

Reused as-is from the ticker bot (proven, doesn't need reinventing):
- RSS/Google News fetch-and-parse pattern (per-entity query instead of per-ticker)
- Date-keyed fingerprint dedup + pruning (`_prune_date_keyed_dict`-style)
- Source-quality preference sorting
- Google News redirect-URL resolution (`_resolve_google_news_url`) so posts link to the real
  article, not a Google interstitial — re-verified live before porting, since it's an
  unofficial/reverse-engineered endpoint Google could change
- Playwright-based X posting + session-cookie login flow
- `my.env`-driven config, `DRY_RUN` support, GitHub Actions deployment

New / different (no price data exists for a person, so none of this carries over):
- No ticker/price/currency logic at all
- A "gossip-worthy" positive filter (feud/lawsuit/fired/scandal/rivalry-shaped headlines)
  instead of a "reject clickbait" filter — here the spicy framing IS the content, but it
  still has to come from a real article, not be invented
- New system prompt/voice entirely — gossip-columnist tone, not market-analyst tone

## v1 scope (what's actually built right now)

Zero-LLM only: fetch → gossip-signal filter → source-quality sort → dedup → simple
attributed-template tweet → (dry-run by default) post. No Gemini call yet — deliberately,
so the sourcing/filtering model gets validated on its own before adding AI-written
commentary on top of it. Adding an LLM layer (grounded strictly in the fetched article,
same anti-fabrication rules as the ticker bot's `NEWS_EVENT_SYSTEM`) is a natural next step
once this baseline is producing sensible picks.

## Not yet done

- Twitter/X account itself (handle, bio, secrets)
- GitHub Secrets/Variables for this repo (`GEMINI_API_KEY`, `TWITTER_SESSION`,
  `TITANS_WATCHLIST`) — the workflow file exists but nothing is configured yet
- Any LLM-authored commentary layer
- Posting cadence/scheduling tuning (currently a single `run_cycle()` per invocation, same
  external `workflow_dispatch` cadence model as the ticker bot)

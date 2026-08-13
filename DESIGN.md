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

Elon Musk, Warren Buffett, Jamie Dimon, Jeff Bezos, Mark Zuckerberg, Bill Ackman,
Ken Griffin, Larry Fink, Ray Dalio, Carl Icahn, Bernard Arnault, Jensen Huang, Sam Altman,
Cathie Wood, David Solomon

## Architecture

Reused as-is from the ticker bot (proven, doesn't need reinventing):
- RSS/Google News fetch-and-parse pattern (per-entity query instead of per-ticker)
- Date-keyed fingerprint dedup + pruning (`_prune_date_keyed_dict`-style)
- Source-quality preference sorting
- Playwright-based X posting + session-cookie login flow
- `my.env`-driven config, `DRY_RUN` support, GitHub Actions `workflow_dispatch` deployment

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

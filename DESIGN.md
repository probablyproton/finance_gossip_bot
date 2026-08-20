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
2. **Attribute, never assert.** Every post carries the actual publisher's link (and X's own
   link-preview card shows that domain), so the source is always visible — v1 dropped the
   redundant "(via *Outlet*)" text line since it just restated what the link already shows,
   but the underlying principle is unchanged: nothing here is framed as this account's own
   settled claim, only as what a named, linked outlet reported.
3. **Source quality gate.** Only pulls from credible business/gossip press — Bloomberg,
   Reuters, WSJ, Fortune, Business Insider, Forbes, CNBC, Axios, Puck, Semafor, Page Six
   Business, NY Post Business, Financial Times, Vanity Fair, The Information. A blocklist
   keeps out satire sites, fabrication mills, and pure stock-tout content farms.
4. **Personal drama, not routine business/career news in disguise.** This account is the
   paparazzi of finance, not a business-news feed with spicier headlines. In scope: off-work
   personal life -- divorce/custody battles, affairs, addiction/rehab, arrests, lawsuits, and
   personal feuds/fights -- situations these people would rather not be in at all. Out of
   scope, even when dramatically framed: (a) a business deal, merger, or political alliance
   ("feud," "rivalry," "clash" used to describe a deal outcome), and (b) a plain career/
   business event on its own -- getting fired, ousted, a boardroom shake-up, slamming a
   rival's business strategy, an internal rift, market fallout -- none of which are personal
   life, even if framed dramatically. Two real examples that slipped through and had to be
   excluded: "Trump And Musk Have Now Turned Their Bitter Feud Into A $100 Million Alliance"
   (a business/political deal story) and, more generally, headlines like "Exec fired amid
   boardroom shake-up" (pure career news with no personal content at all). `ROUTINE_BUSINESS_RE`
   in `gossip_bot.py` catches case (a) by rejecting a headline that matched a gossip-signal
   word if it ALSO mentions a deal/earnings/stock/regulatory outcome; case (b) is handled by
   simply not including "fired"/"ousted"/"shake-up"/"slams"/"blasts"/"rift"/"fallout" in the
   positive filter at all, since a genuine personal-drama headline doesn't need them --
   lawsuits and genuine interpersonal conflict (feud, rivalry, clash, brawl) stay in scope.
5. **No financial advice, no market-moving insinuation.** Never frame a story as investment
   guidance, and never imply insider-trading-adjacent claims without the source itself
   making that claim.

### Gossip-signal vocabulary — a living list

`GOSSIP_SIGNAL_RE` (the positive keyword filter) and `BUSINESS_OUTCOME_RE` (the negative
filter that catches business/deal framing even when a gossip word matched) are not a
finished product — they get tuned every time a real example shows up, either a spam pattern
that slipped through or a genuine gossip story that got missed. Treat this as a running log,
not a one-time list; append to it rather than silently rewriting history.

Current categories in `GOSSIP_SIGNAL_RE`: relationship/marriage drama (divorce, affair,
cheating, breakup, custody, prenup), vices/personal struggles (addiction, rehab, relapse,
overdose, DUI), legal/criminal-personal (arrest, jail, indictment, assault, harassment,
fined), family drama (disowned, inheritance/estate battles), interpersonal conflict (feud,
rivalry, clash, brawl, confrontation, shouting/screaming match, fistfight), public
embarrassment/oddity (scandal, bizarre, exposed, leaked photos/texts, caught, spotted with),
and lawsuits/legal disputes (lawsuit, sued, countersue, settlement, class action, legal/court
battle, verdict, plaintiff/defendant, deposition, testifies, litigation, accused) — the
lawsuit words stay genuinely ambiguous between a personal dispute and routine business/
contract litigation, disambiguated by `ROUTINE_BUSINESS_RE` (renamed from
`BUSINESS_OUTCOME_RE`), not by removing them. Pure career/business words ("fired", "ousted",
"shake-up", "slams", "blasts", "rift", "fallout") were REMOVED entirely — see learnings below.

Confirmed learnings so far:
- Removed (too weak/generic, spammed the feed with routine business news): "resigns"/"steps
  down" (ordinary planned departures), "split" used alone (stock-split announcements —
  re-added narrowly as `split(s|ting) (from|with)` for marriage splits only), "leaked"/"secret"
  used alone (generic product-leak headlines — re-added narrowly as `leaked (photos|texts|...)`
  for personal-media leaks only), "controversy"/"apology"/"criticizes" (any policy/PR story).
- Deliberately excluded: bare "fight(s)" — even more common in business/legal/regulatory
  headlines ("fights the lawsuit", "fights regulators") than the words already removed above;
  "brawl"/"altercation"/"confrontation"/"shouting match"/"screaming match"/"fistfight" cover
  the physical-conflict framing instead, since those essentially never appear in a
  business-deal headline.
- Added (then renamed/broadened) `ROUTINE_BUSINESS_RE` after "Trump And Musk Have Now Turned
  Their Bitter Feud Into A $100 Million Alliance" matched on "feud" but was a business/
  political deal story — the words "feud"/"rivalry"/"clash"/"lawsuit"/"fired" are kept
  because they're valid gossip signals on their own, but rejected when the same headline also
  reports a deal/alliance/merger outcome, an earnings/revenue figure, a stock-price move, or
  routine regulatory/monetary-policy language (tariffs, rate hikes, antitrust) — a genuine
  personal-drama headline essentially never shares a sentence with those.
- Added "fined" (personal court/regulatory fine) and "bizarre" (the "weird things" category)
  per explicit request; both verified against synthetic test headlines before shipping.
- Expanded lawsuits/legal disputes beyond just "lawsuit"/"sued" per explicit request:
  countersue, settlement/settles, class action, legal/court battle, verdict, plaintiff,
  defendant, deposition, testifies, litigation. Verified against 9 synthetic headlines,
  including that "Company settles antitrust lawsuit with regulators for $500 million" still
  correctly gets rejected by `ROUTINE_BUSINESS_RE` before shipping.
- Moderate-risk words being watched, not yet removed: "charged" (could mean personally
  charged with a crime, or a company charged with a regulatory violation — kept for now since
  per-person search means the headline names the person, not the company, but flag if this
  spams the feed). Same watch now applies to "settlement"/"litigation" for the same reason —
  a routine corporate settlement/litigation story could still slip through if it doesn't also
  trip `ROUTINE_BUSINESS_RE`.
- Removed "fired"/"ousted"/"shake-up"/"slams"/"blasts"/"rift"/"fallout" entirely, per explicit
  instruction to remove finance/business news from the feed altogether and keep it to
  off-work personal issues and lawsuits. These words each describe a pure CAREER/business
  event on their own -- "Exec fired amid boardroom shake-up" needed no deal/earnings/stock
  language at all to pass before, so `ROUTINE_BUSINESS_RE` (which only fires on those) could
  never have caught it. Verified via 15 synthetic headlines (6 business-event negatives, 6
  personal/lawsuit positives, 3 prior regressions) that this didn't disturb anything else
  before shipping.

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

### Cross-cycle, cross-medium dedup — the same story, only ever once

The exact-fingerprint dedup (an article's link, a tweet's status ID) only catches an EXACT
repeat of the same specific link/tweet. It cannot catch "the same underlying story, found
again later via a different link, a different search query, or as a tweet instead of an
article" — those never share a fingerprint with each other even though they're the same
news. Confirmed by direct test: a real article headline and the tweet reporting the same
story score only 0.44-0.48 on the plain word-overlap (Jaccard) check already used for
within-cycle near-duplicates — under that check's own 0.5 threshold — because the tweet's
phrasing is longer and looser than the terse headline.

So `state["posted_signatures"]` persists a `{headline, person, date}` entry for every real
post (pruned the same way as `gossip_seen`), and every new candidate is checked against ALL
of them via `_story_already_covered` before it's even considered, using a **containment**
score (shared-words ÷ the SMALLER headline's word count, not the union) instead of Jaccard --
robust to the length/style mismatch between a headline and a tweet. Containment alone is too
permissive on its own, though: confirmed by direct test that two DIFFERENT titans' similarly-
phrased divorce headlines also score ~0.7 on containment. The fix is a person-name anchor —
a loose threshold (0.6) when both sides name the SAME specific titan (safe to be generous),
and a much stricter threshold (0.85) when the person is unknown on either side (an
industry-wide hit, or a name mismatch), so two different people's similar-shaped stories
don't get wrongly treated as the same one.

### Quote-tweets — a third content type, for exposure

Alongside articles, the bot also finds genuine, already-public tweets to quote-tweet
(`fetch_tweet_candidates`, `post_quote_tweet`). Two things make this ToS-safe rather than
scraping X directly:

1. **Discovery** — confirmed live that Google News RSS indexes individual X posts when
   searched `site:x.com {name}`, returning the REAL tweet text as the item title (verified:
   a genuine Justin Sun tweet about his lawsuit, a real "feud between Sam Altman and Elon
   Musk" tweet). Same Google News mechanism already used for articles, just a different
   query shape — no X search/API touched at all.
2. **Author verification** — X's public oEmbed endpoint (`publish.twitter.com/oembed`, the
   same one any website uses to embed a tweet) returns the real author display name. This is
   what enforces the actual requirement (per explicit instruction): **never quote-tweet a
   titan's own words** — only genuine gossip/commentary from someone else about them.
   `_is_self_authored` requires every word of the titan's name to appear in the tweet
   author's display name before excluding it (confirmed: correctly excludes "H.E. Justin Sun
   👨‍🚀 🌞" for @justinsuntron, would not exclude an unrelated account).

Posting itself uses X's own documented `x.com/intent/tweet?url=...` sharing flow — the
platform's own officially-supported mechanism for quote-tweeting a URL, not automation
scraping X's search or timelines.

**Opener text** is category-matched, not one flat generic pool — "Worth a look:"/"Spotted
this:" on every single post reads as filler, not engaging. `_pick_opener` derives the
category from whichever `GOSSIP_SIGNAL_RE` word actually matched the real headline (marriage
drama, addiction/vices, legal trouble, public meltdown, scandal, or courtroom drama each get
their own small pool), so this is still "aggregate, never fabricate": it only describes what
KIND of already-published story this is — the same signal that qualified it as gossip in the
first place — never inventing new detail.

**Compose-time validation, read from the real rendered page, not assumed.** Two real
incidents (an opener duplicated as "Worth a look: ... Worth a look:", and — worse — the
quote-tweet embed silently failing to attach, posting bare opener text with no source at all)
both happened because the code trusted its own input to X's intent-URL mechanism without
ever checking what actually rendered. `post_quote_tweet` now reads back the compose box's
real text (`page.inner_text`) after a settle delay and runs it through
`_validate_compose_text` — checking for emptiness, over-length, an unresolved Google link, a
duplicated line, a template/placeholder artifact (`{`, `None`, etc. — a defense against
anything code-shaped leaking into a post), and the quoted tweet's own URL sitting in the text
as plain, unconverted content — before ever clicking Post. A validation failure raises
`ComposeValidationError` and the whole candidate is skipped (not marked seen, not treated as
a hard failure) so `run_cycle` tries the next one instead of either posting something broken
or aborting the entire cycle.

## Cadence and multi-post

`bot.yml` deliberately has NO `schedule:` trigger — GitHub Actions' own native cron proved
unreliable at a 15min cadence for the ticker bot too (delayed/skipped runs), so this instead
uses an external **cron-job.org** job that calls the `workflow_dispatch` REST API endpoint
every 15 minutes, same pattern as the ticker bot (which has its own separate cron-job.org job
— do not repoint that one; this bot needs its own).

**cron-job.org job configuration** (a NEW job, separate from the ticker bot's):
- URL: `https://api.github.com/repos/probablyproton/finance_gossip_bot/actions/workflows/bot.yml/dispatches`
- Method: `POST`
- Headers:
  - `Authorization: Bearer <a GitHub Personal Access Token — fine-grained, scoped to just
    this repo, with "Actions: Read and write" permission>`
  - `Accept: application/vnd.github+json`
  - `Content-Type: application/json`
- Request body: `{"ref": "main", "inputs": {"dry_run": false}}`
- Schedule: every 15 minutes

The `dry_run: false` in the body is load-bearing, not optional — the workflow's `dry_run`
input defaults to `true` (so a human casually clicking "Run workflow" in the GitHub UI gets a
safe preview by default), and since every trigger is now `workflow_dispatch`, an external call
that omits `inputs.dry_run` silently falls back to that same default and never actually posts.

Each cycle can post up to `MAX_POSTS_PER_CYCLE` (default 3) distinct stories in one run, found
from a single fetch pass (not by re-fetching per post). Posts within a cycle are always fired
one at a time in a plain sequential loop -- there is no threading/async/concurrency anywhere
in `gossip_bot.py`, so it's structurally impossible for two posts to go out in parallel, which
is exactly the pattern X's own bot-behavior detection flags. They're also spaced apart in TIME
via a `POST_PACING_SECONDS` gap (default 240s / 4 minutes, not seconds) between successive
posts in the same cycle -- `bot.yml` explicitly sets this in its "Write env file" step (it
didn't before, which meant the deployed bot was silently relying on `gossip_bot.py`'s own
fallback default rather than an explicit, reviewed value).

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

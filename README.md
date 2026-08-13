# Finance Gossip Bot (v1 draft)

Posts about the drama, feuds, and power struggles of finance's biggest names — aggregated
from real, already-published stories, never invented. See [DESIGN.md](DESIGN.md) for the
full content policy, watchlist, and architecture rationale.

**Status:** early draft. No Twitter/X account exists yet — everything account-related is a
placeholder. No LLM commentary layer yet — v1 is a zero-LLM aggregator by design, so the
sourcing/filtering logic gets validated before adding AI-written text on top of it.

## Local setup

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example my.env   # then edit my.env with real values
python gossip_bot.py     # DRY_RUN=true by default — logs what it would post, doesn't post
```

To actually post, run `python login.py` once to create `twitter_session.json` (opens a
real browser window — log in manually), then set `DRY_RUN=false` in `my.env`.

## Deploying to GitHub Actions

Not wired up yet — needs, once the account exists:
- Repo secret `GEMINI_API_KEY` (reserved for a future LLM layer, not used by v1's logic yet)
- Repo secret `TWITTER_SESSION` — the base64 string `login.py` prints
- Repo variable `TITANS_WATCHLIST` — comma-separated names

`.github/workflows/bot.yml` is a `workflow_dispatch`-triggered skeleton, same external-cadence
model as the ticker bot this was forked from (no `schedule:` trigger of its own — something
external needs to call the workflow_dispatch API on whatever cadence is decided).

## Files

| File | Purpose |
|---|---|
| `gossip_bot.py` | Fetch → filter → dedup → format → post, one cycle per run |
| `login.py` | One-time (or on session expiry) browser login to capture `twitter_session.json` |
| `state.json` | Committed dedup memory (`gossip_seen`) — persists across ephemeral Action runs |
| `DESIGN.md` | Content policy, watchlist, architecture |

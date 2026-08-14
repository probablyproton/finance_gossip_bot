"""
One-time (or whenever the session expires) helper: opens your actual Brave browser profile
(already logged into X) and saves just the twitter.com/x.com cookies to
twitter_session.json — the file gossip_bot.py's post_tweet() reads to post without logging
in every run.

Uses your REAL, already-authenticated Brave profile rather than automating a fresh login —
X's bot detection blocks/hangs a vanilla Playwright login flow (confirmed on this machine),
so this sidesteps that entirely by reusing a session you already have.

IMPORTANT: close all Brave windows before running this — a Chromium-based browser locks its
profile directory while running, so Playwright can't open the same profile at the same time.

Usage:
    python login.py

Then paste the printed base64 string into this repo's GitHub Secret named TWITTER_SESSION
(placeholder secret name — rename to match whatever's actually configured in
.github/workflows/bot.yml if you change it there).
"""

import base64
import json
import os

from playwright.sync_api import sync_playwright

SESSION_FILE = "twitter_session.json"
BRAVE_PROFILE = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data")
BRAVE_EXE = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe")

with sync_playwright() as p:
    print("Opening your Brave profile — if you're already logged into x.com this should work instantly.")
    print("(If Brave is still open elsewhere, close it first and rerun — the profile can't be shared.)\n")
    context = p.chromium.launch_persistent_context(
        user_data_dir=BRAVE_PROFILE,
        executable_path=BRAVE_EXE,
        headless=False,
        slow_mo=100,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()
    page.goto("https://x.com/home")

    print("If you land on your home feed, you're already logged in — come back here and press Enter.")
    print("If a login page appears instead, log in manually, then come back here and press Enter.")
    input()

    context.storage_state(path=SESSION_FILE)
    context.close()

# Strip down to just the twitter.com/x.com cookies — the full Brave profile's storage_state
# also carries localStorage/sessionStorage and cookies for every other site you're logged
# into, none of which the bot needs or should have sitting in a GitHub Secret.
def _is_x_domain(domain: str) -> bool:
    # A proper suffix match, not a bare substring check — "x.com" is a substring of plenty
    # of unrelated domains that happen to end in some letter + ".com" (fedex.com,
    # equinix.com, mapstogpx.com, avmaxx.com, salaryaftertax.com, account.mapbox.com,
    # wearesbx.com — all confirmed to leak through a naive "x.com" in domain check).
    d = domain.lstrip(".")
    return d == "x.com" or d.endswith(".x.com") or d == "twitter.com" or d.endswith(".twitter.com")


with open(SESSION_FILE, encoding="utf-8") as f:
    session = json.load(f)
twitter_cookies = [c for c in session.get("cookies", []) if _is_x_domain(c.get("domain", ""))]
slim = {"cookies": twitter_cookies, "origins": []}
with open(SESSION_FILE, "w", encoding="utf-8") as f:
    json.dump(slim, f)

print(f"\nSaved session to {SESSION_FILE} ({len(twitter_cookies)} X/Twitter cookies only).")
print("Copy the text below and paste it into GitHub Secrets:")
print("Secret name: TWITTER_SESSION\n")
with open(SESSION_FILE, "rb") as f:
    print(base64.b64encode(f.read()).decode())

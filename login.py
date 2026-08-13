"""
One-time (or whenever the session expires) helper: opens a real browser window, you log
into X/Twitter manually, and it saves the session cookies to twitter_session.json — the
file gossip_bot.py's post_tweet() reads to post without logging in every run.

Usage:
    python login.py

Then paste the printed base64 string into this repo's GitHub Secret named TWITTER_SESSION
(placeholder secret name — rename to match whatever's actually configured in
.github/workflows/bot.yml if you change it there).
"""

import base64

from playwright.sync_api import sync_playwright

SESSION_FILE = "twitter_session.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=50)
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto("https://x.com/login")

    print("Log into X/Twitter in the opened browser window.")
    print("Once you see your home timeline, come back here and press Enter.")
    input()

    context.storage_state(path=SESSION_FILE)
    browser.close()

print(f"\nSaved session to {SESSION_FILE}.")
print("Copy the text below and paste it into GitHub Secrets:")
print("Secret name: TWITTER_SESSION\n")
with open(SESSION_FILE, "rb") as f:
    print(base64.b64encode(f.read()).decode())

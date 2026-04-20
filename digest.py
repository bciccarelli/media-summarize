#!/usr/bin/env python3
"""Daily X + LinkedIn digest: scroll feeds, rank with Gemini, email top items, auto-like on X."""

import asyncio
import json
import logging
import os
import random
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml
from google import genai
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
BROWSER_DIR = BASE_DIR / "browser_data"
LINKEDIN_BROWSER_DIR = BASE_DIR / "browser_data_linkedin"
STATE_PATH = BASE_DIR / "digest_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(BASE_DIR / "digest.log"),
    ],
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Tweet fetching via browser
# ---------------------------------------------------------------------------

async def extract_tweets_from_page(page) -> list[dict]:
    """Extract tweet data from currently visible tweet articles."""
    return await page.evaluate("""
        () => {
            const tweets = [];
            const articles = document.querySelectorAll('article[data-testid="tweet"]');
            for (const article of articles) {
                try {
                    // Tweet text
                    const textEl = article.querySelector('div[data-testid="tweetText"]');
                    const text = textEl ? textEl.innerText : '';

                    // Author handle — find the link with format /@handle
                    let handle = '';
                    const userLinks = article.querySelectorAll('a[href^="/"]');
                    for (const link of userLinks) {
                        const href = link.getAttribute('href');
                        if (href && /^\\/[A-Za-z0-9_]+$/.test(href) && !['/', '/home', '/explore', '/notifications', '/messages', '/search'].includes(href)) {
                            handle = href.slice(1);
                            break;
                        }
                    }

                    // Timestamp and tweet URL
                    const timeEl = article.querySelector('time');
                    const dateStr = timeEl ? timeEl.getAttribute('datetime') : '';
                    const tweetLink = timeEl ? timeEl.closest('a') : null;
                    const url = tweetLink ? 'https://x.com' + tweetLink.getAttribute('href') : '';

                    // Engagement — extract from aria-labels on action buttons
                    let likes = 0, retweets = 0;
                    const likeBtn = article.querySelector('button[data-testid="like"], button[data-testid="unlike"]');
                    if (likeBtn) {
                        const m = likeBtn.getAttribute('aria-label')?.match(/(\\d+)/);
                        if (m) likes = parseInt(m[1]);
                    }
                    const rtBtn = article.querySelector('button[data-testid="retweet"], button[data-testid="unretweet"]');
                    if (rtBtn) {
                        const m = rtBtn.getAttribute('aria-label')?.match(/(\\d+)/);
                        if (m) retweets = parseInt(m[1]);
                    }

                    if (text || url) {
                        tweets.push({ text, handle, date: dateStr, url, likes, retweets });
                    }
                } catch (e) {
                    // Skip malformed tweet
                }
            }
            return tweets;
        }
    """)


async def natural_scroll(page):
    """Scroll down in a human-like way."""
    scroll_px = random.randint(400, 800)
    await page.mouse.wheel(0, scroll_px)
    # Vary the pause: mostly short, occasionally longer (simulating reading)
    if random.random() < 0.15:
        delay = random.uniform(3.0, 6.0)  # "reading" pause
    else:
        delay = random.uniform(1.2, 2.5)
    await asyncio.sleep(delay)


async def fetch_tweets_browser(config: dict, login_mode: bool = False) -> list[dict]:
    """Open X in a browser, scroll the feed, and extract tweets."""
    target = config.get("settings", {}).get("target_tweets", 100)
    headless = not login_mode

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DIR),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.pages[0] if context.pages else await context.new_page()

        log.info("Navigating to X feed")
        await page.goto("https://x.com/home", wait_until="domcontentloaded")

        # If in login mode, wait for user to log in manually
        if login_mode:
            log.info("LOGIN MODE: Please log in to X in the browser window.")
            log.info("After logging in and seeing your feed, press Enter here.")
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("\n>>> Press Enter after you've logged in... ")
            )
            await context.close()
            log.info("Login saved. Run again without --login to fetch your feed.")
            return []

        # Wait for feed to load
        try:
            await page.wait_for_selector(
                'article[data-testid="tweet"]', timeout=15000
            )
        except Exception:
            log.error("Feed did not load — you may need to run with --login first")
            await context.close()
            return []

        log.info(f"Feed loaded — scrolling to collect ~{target} tweets")

        all_tweets: dict[str, dict] = {}  # keyed by URL for dedup
        stale_rounds = 0

        while len(all_tweets) < target and stale_rounds < 5:
            before = len(all_tweets)
            batch = await extract_tweets_from_page(page)
            for t in batch:
                key = t.get("url") or t.get("text", "")[:80]
                if key and key not in all_tweets:
                    all_tweets[key] = t

            new = len(all_tweets) - before
            if new == 0:
                stale_rounds += 1
            else:
                stale_rounds = 0

            log.info(f"  Collected {len(all_tweets)}/{target} tweets (+{new} new)")
            await natural_scroll(page)

        await context.close()

    tweets = list(all_tweets.values())
    log.info(f"Total tweets collected: {len(tweets)}")
    return tweets


# ---------------------------------------------------------------------------
# LinkedIn fetching via browser
# ---------------------------------------------------------------------------

async def extract_linkedin_posts_from_page(page) -> list[dict]:
    """Extract post data from currently visible LinkedIn feed items."""
    return await page.evaluate("""
        () => {
            const posts = [];
            const feed = document.querySelector('[data-testid="mainFeed"]');
            if (!feed) return posts;

            const children = feed.querySelectorAll(':scope > div[data-display-contents]');
            for (const post of children) {
                try {
                    // Post text — from expandable-text-box (first one is the main text)
                    const textEl = post.querySelector('span[data-testid="expandable-text-box"]');
                    const text = textEl ? textEl.innerText.trim() : '';

                    // Author name — extract from control menu button aria-label
                    // e.g. "Open control menu for post by Kyle Macdonald"
                    let name = '';
                    const menuBtn = post.querySelector('button[aria-label^="Open control menu for post by"]');
                    if (menuBtn) {
                        const label = menuBtn.getAttribute('aria-label');
                        name = label.replace('Open control menu for post by ', '');
                    }

                    // Author title — from the profile link text which contains name + degree + title
                    // e.g. "Kyle Macdonald\\n\\n \\n • 1st\\n\\nInvestor | Sil"
                    let title = '';
                    const profileLinks = post.querySelectorAll('a[href*="/in/"]');
                    for (const link of profileLinks) {
                        const linkText = link.innerText.trim();
                        // Find the link that has the author name and title info (multiline)
                        if (linkText.includes('\\n') && linkText.includes(name.split(' ')[0])) {
                            const parts = linkText.split('\\n').map(s => s.trim()).filter(Boolean);
                            // Title is usually after the connection degree marker
                            const degreeIdx = parts.findIndex(p => p.match(/^•\\s*(1st|2nd|3rd)/));
                            if (degreeIdx >= 0 && degreeIdx + 1 < parts.length) {
                                title = parts.slice(degreeIdx + 1).join(' ');
                            } else if (parts.length > 1) {
                                title = parts[parts.length - 1];
                            }
                            break;
                        }
                    }

                    // Profile URL
                    let profileUrl = '';
                    const firstProfileLink = post.querySelector('a[href*="/in/"]');
                    if (firstProfileLink) {
                        profileUrl = firstProfileLink.getAttribute('href').split('?')[0];
                    }

                    // Reactions count — look for link text matching "N reactions"
                    let reactions = 0;
                    const allLinks = post.querySelectorAll('a');
                    for (const link of allLinks) {
                        const lt = link.innerText.trim();
                        const m = lt.match(/^(\\d[\\d,]*)\\s*reactions?/i);
                        if (m) {
                            reactions = parseInt(m[1].replace(/,/g, ''));
                            break;
                        }
                    }

                    // Comments count — from Comment button or nearby text
                    let comments = 0;
                    const allBtns = post.querySelectorAll('button');
                    for (const btn of allBtns) {
                        const lt = (btn.getAttribute('aria-label') || btn.innerText || '').trim();
                        const m = lt.match(/(\\d+)\\s*comment/i);
                        if (m) {
                            comments = parseInt(m[1]);
                            break;
                        }
                    }

                    // Dedup key — use profile URL + first 60 chars of text
                    const dedupKey = (profileUrl || name) + ':' + text.slice(0, 60);

                    if (text || name) {
                        posts.push({
                            text, name, title, profileUrl,
                            url: '',  // LinkedIn doesn't expose post permalink in feed DOM
                            reactions, comments, postType: 'post',
                            dedupKey
                        });
                    }
                } catch (e) {
                    // Skip malformed post
                }
            }
            return posts;
        }
    """)


async def linkedin_natural_scroll(page):
    """Scroll LinkedIn feed — slower and more cautious than X."""
    scroll_px = random.randint(600, 1000)

    # LinkedIn sets body overflow:hidden and scrolls inside <main id="workspace">
    await page.evaluate(f"""
        (() => {{
            const el = document.getElementById('workspace') || document.querySelector('main');
            if (el) el.scrollBy(0, {scroll_px});
            else window.scrollBy(0, {scroll_px});
        }})()
    """)

    # Longer pauses than X, with more frequent "reading" stops
    r = random.random()
    if r < 0.25:
        delay = random.uniform(5.0, 10.0)   # reading a long post
    elif r < 0.40:
        delay = random.uniform(3.0, 5.0)    # skimming
    else:
        delay = random.uniform(2.0, 4.0)    # normal scroll

    await asyncio.sleep(delay)

    # Occasional mouse movement to look natural
    if random.random() < 0.10:
        x = random.randint(200, 800)
        y = random.randint(200, 600)
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.3, 0.8))


async def fetch_linkedin_posts_browser(config: dict, login_mode: bool = False) -> list[dict]:
    """Open LinkedIn in a browser, scroll the feed, and extract posts."""
    target = config.get("settings", {}).get("target_linkedin_posts", 40)
    headless = not login_mode

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(LINKEDIN_BROWSER_DIR),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.pages[0] if context.pages else await context.new_page()

        log.info("Navigating to LinkedIn feed")
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")

        if login_mode:
            log.info("LOGIN MODE: Please log in to LinkedIn in the browser window.")
            log.info("After seeing your feed, press Enter here.")
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("\n>>> Press Enter after LinkedIn login... ")
            )
            await context.close()
            log.info("LinkedIn login saved. Run again without --login-linkedin.")
            return []

        # Wait for feed to load
        try:
            await page.wait_for_selector(
                '[data-testid="mainFeed"]', timeout=20000
            )
        except Exception:
            log.error("LinkedIn feed did not load — run with --login-linkedin first")
            await context.close()
            return []

        # Let the page settle before scrolling
        await asyncio.sleep(random.uniform(2.0, 4.0))

        log.info(f"LinkedIn feed loaded — scrolling to collect ~{target} posts")

        all_posts: dict[str, dict] = {}  # keyed by dedupKey
        stale_rounds = 0

        while len(all_posts) < target and stale_rounds < 8:
            before = len(all_posts)
            batch = await extract_linkedin_posts_from_page(page)
            for post in batch:
                key = post.get("dedupKey") or post.get("text", "")[:80]
                if key and key not in all_posts:
                    all_posts[key] = post

            new = len(all_posts) - before
            if new == 0:
                stale_rounds += 1
            else:
                stale_rounds = 0

            log.info(f"  [LinkedIn] Collected {len(all_posts)}/{target} posts (+{new} new)")

            # Scroll, then wait for new content to load
            await linkedin_natural_scroll(page)
            # Give LinkedIn time to lazy-load new posts after scroll
            await asyncio.sleep(random.uniform(1.0, 2.0))

        await context.close()

    posts = list(all_posts.values())
    log.info(f"Total LinkedIn posts collected: {len(posts)}")
    return posts


# ---------------------------------------------------------------------------
# State (cross-day dedup)
# ---------------------------------------------------------------------------

def load_state(config: dict) -> dict:
    """Load digest_state.json, purging entries older than dedup_window_days."""
    if not STATE_PATH.exists():
        return {"sent_items": []}
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except Exception as e:
        log.warning(f"Failed to read state file: {e}; starting fresh")
        return {"sent_items": []}

    window = config.get("settings", {}).get("dedup_window_days", 7)
    cutoff = (datetime.now() - timedelta(days=window)).strftime("%Y-%m-%d")
    state["sent_items"] = [
        item for item in state.get("sent_items", [])
        if item.get("sent_on", "") >= cutoff
    ]
    return state


def save_state(state: dict):
    """Atomically write the state file."""
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def filter_by_sent_urls(tweets: list[dict], state: dict) -> list[dict]:
    """Drop X tweets already surfaced in a recent digest. LinkedIn dedup is
    handled via the RECENTLY COVERED prompt section instead (no stable URL)."""
    sent_urls = {
        item["url"] for item in state.get("sent_items", [])
        if item.get("platform") == "x" and item.get("url")
    }
    filtered = [t for t in tweets if t.get("url") not in sent_urls]
    dropped = len(tweets) - len(filtered)
    if dropped:
        log.info(f"Dedup dropped {dropped} X tweets seen in last window")
    return filtered


# ---------------------------------------------------------------------------
# Summarization (single Gemini call, JSON output)
# ---------------------------------------------------------------------------

DIGEST_PROMPT = """\
You are curating a daily signal-only digest for an AI engineer / data scientist
who works at startups. Their X + LinkedIn feeds are noisy — only surface items
they should actually act on or know about today.

TODAY'S DATE: {today}

RULES:
- Return AT MOST {max_items} items total across both platforms.
- Only include items scoring {min_score}+ on a 1-10 signal scale. If nothing
  meets the bar, return FEWER items — even zero is acceptable.
- signal_score meaning: 10 = definitely act on this today; 7 = worth their 30
  seconds; 5 = interesting but skippable; <5 = don't include.

WHAT COUNTS AS SIGNAL:
- Research / papers with concrete results (not "we should think about X")
- Tool releases with a clear use-case they could try this week
- Startup hiring DS/ML/AI roles (especially from their LinkedIn network)
- Events / conferences with an RSVP window that hasn't expired
- Funding announcements (implies hiring is about to open)
- Close-connection job moves on LinkedIn (1st-degree moves to new roles)
- Concrete industry shifts (regulation, named model release, acquisition)

WHAT COUNTS AS JUNK (skip):
- Motivational posts, "just shipped!" without a link, engagement-bait polls
- Reshares without commentary
- Generic AI-hype threads or unsubstantiated hot takes
- Events whose date has already passed (check TODAY'S DATE)
- Anything semantically covered in RECENTLY COVERED — even if the URL differs

RECENTLY COVERED (do NOT re-surface these topics):
{recently_covered}

CATEGORIES (use exactly one per item):
Research | Tools | Opportunity | Event | Network Move | Signal

For each item, return:
- platform: "x" or "linkedin"
- url: the exact url from the input data (tweet URL for X, profile URL for LinkedIn)
- author: @handle for X, "Name — Title" for LinkedIn
- category: one of the six above
- summary: ONE sentence, <=25 words, what they need to know / do
- signal_score: integer 1-10
- reason: short phrase (e.g. "concrete benchmark", "1st-degree move", "RSVP Friday")

X POSTS:
{x_data}

LINKEDIN POSTS:
{linkedin_data}
"""


DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "enum": ["x", "linkedin"]},
                    "url": {"type": "string"},
                    "author": {"type": "string"},
                    "category": {"type": "string"},
                    "summary": {"type": "string"},
                    "signal_score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "platform", "url", "author", "category",
                    "summary", "signal_score", "reason",
                ],
            },
        }
    },
    "required": ["items"],
}


def format_x_for_prompt(tweets: list[dict]) -> str:
    if not tweets:
        return "(none)"
    return "\n".join(
        f"@{t.get('handle', 'unknown')}: {t['text']} | {t.get('url', '')} | "
        f"{t.get('likes', 0)} likes | {t.get('retweets', 0)} RTs"
        for t in tweets
    )


def format_linkedin_for_prompt(posts: list[dict]) -> str:
    if not posts:
        return "(none)"
    lines = []
    for p in posts:
        url = p.get("profileUrl") or ""
        lines.append(
            f"{p.get('name', 'Unknown')} — {p.get('title', '')}: {p['text']} | "
            f"{url} | {p.get('reactions', 0)} reactions | "
            f"{p.get('comments', 0)} comments"
        )
    return "\n".join(lines)


def format_recently_covered(state: dict) -> str:
    items = state.get("sent_items", [])
    if not items:
        return "(nothing sent recently)"
    items_sorted = sorted(items, key=lambda i: i.get("sent_on", ""), reverse=True)
    return "\n".join(
        f"[{i.get('sent_on', '?')}] {i.get('category', '?')}: {i.get('summary', '')}"
        for i in items_sorted[:30]
    )


def summarize_combined(tweets: list[dict], posts: list[dict],
                       state: dict, config: dict) -> list[dict]:
    """Single Gemini call — returns a ranked list of digest items (JSON)."""
    settings = config.get("settings", {})
    max_items = settings.get("max_digest_items", 8)
    min_score = settings.get("min_signal_score", 7)

    prompt = DIGEST_PROMPT.format(
        today=datetime.now().strftime("%A, %B %d, %Y"),
        max_items=max_items,
        min_score=min_score,
        recently_covered=format_recently_covered(state),
        x_data=format_x_for_prompt(tweets),
        linkedin_data=format_linkedin_for_prompt(posts),
    )

    gem = config["gemini"]
    client = genai.Client(api_key=gem["api_key"])
    model = gem.get("model", "gemini-2.5-flash")

    log.info(f"Sending {len(prompt)} chars to Gemini ({model})")
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": DIGEST_SCHEMA,
        },
    )

    try:
        data = json.loads(response.text or "{}")
        items = data.get("items", [])
    except json.JSONDecodeError as e:
        log.error(f"Gemini returned invalid JSON: {e}")
        return []

    items = [i for i in items if i.get("signal_score", 0) >= min_score]
    items.sort(key=lambda i: i.get("signal_score", 0), reverse=True)
    items = items[:max_items]

    log.info(f"Gemini surfaced {len(items)} items meeting signal floor")
    return items


# ---------------------------------------------------------------------------
# Render digest → HTML
# ---------------------------------------------------------------------------

CATEGORY_ORDER = ["Research", "Tools", "Opportunity", "Event", "Network Move", "Signal"]


def render_digest_html(items: list[dict]) -> str:
    if not items:
        return ('<p><em>Nothing met the signal bar today. '
                'Quiet day — go touch grass.</em></p>')

    by_cat: dict[str, list[dict]] = {}
    for item in items:
        by_cat.setdefault(item.get("category", "Signal"), []).append(item)

    parts = []
    seen_cats = set()
    ordered_cats = [c for c in CATEGORY_ORDER if c in by_cat] + [
        c for c in by_cat if c not in CATEGORY_ORDER
    ]
    for cat in ordered_cats:
        if cat in seen_cats:
            continue
        seen_cats.add(cat)
        parts.append(
            f'<h2 style="font-size:16px;margin-top:24px;margin-bottom:8px;'
            f'color:#555;">{cat}</h2>'
        )
        parts.append('<ul style="padding-left:20px;margin-top:0;">')
        for item in by_cat[cat]:
            url = item.get("url", "")
            author = item.get("author", "")
            summary = item.get("summary", "")
            score = item.get("signal_score", 0)
            reason = item.get("reason", "")
            link_html = (
                f'<a href="{url}" style="color:#1da1f2;">link</a>'
                if url else ""
            )
            platform_badge = "X" if item.get("platform") == "x" else "LI"
            parts.append(
                f'<li style="margin-bottom:10px;">'
                f'<strong>{summary}</strong><br>'
                f'<span style="color:#888;font-size:13px;">'
                f'{platform_badge} · {author} · {reason} · '
                f'score {score}{" · " + link_html if link_html else ""}'
                f'</span></li>'
            )
        parts.append("</ul>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Auto-like top X posts
# ---------------------------------------------------------------------------

async def like_top_tweets(urls: list[str], config: dict):
    """Open each tweet URL and click the like button with human-like pauses."""
    if not urls:
        return

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DIR),
            headless=True,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.pages[0] if context.pages else await context.new_page()

        for url in urls:
            try:
                log.info(f"Opening for like: {url}")
                await page.goto(url, wait_until="domcontentloaded")

                # Wait for an active (not-yet-liked) like button
                try:
                    await page.wait_for_selector(
                        'button[data-testid="like"]', timeout=10000
                    )
                except Exception:
                    log.info(f"  No like button (already liked or unavailable): {url}")
                    continue

                # Dwell as if reading
                await asyncio.sleep(random.uniform(3.0, 7.0))

                await page.click('button[data-testid="like"]')
                log.info(f"  Liked: {url}")

                # Dwell before next
                await asyncio.sleep(random.uniform(4.0, 9.0))
            except Exception as e:
                log.warning(f"  Like failed for {url}: {e}")
                continue

        await context.close()


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_email_html(summary_html: str, stats: dict) -> str:
    today = datetime.now()
    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, Segoe UI, Arial, sans-serif;
             max-width: 640px; margin: 0 auto; padding: 20px;
             color: #333; line-height: 1.6; font-size: 15px;">
  <h1 style="border-bottom: 2px solid #333; padding-bottom: 10px;
             font-size: 22px; margin-bottom: 5px;">
    Daily Digest &mdash; {today.strftime('%A, %b %d, %Y')}
  </h1>
  <p style="color: #888; font-size: 13px; margin-top: 0;">
    {stats['item_count']} items surfaced from {stats['tweet_count']} tweets + {stats['post_count']} LinkedIn posts
  </p>

  {summary_html}

  <hr style="margin-top: 30px; border: none; border-top: 1px solid #eee;">
  <p style="color: #aaa; font-size: 11px;">
    Generated {today.strftime('%Y-%m-%d %H:%M')}
  </p>
</body>
</html>"""


def send_email(html: str, config: dict):
    email_cfg = config["email"]
    today = datetime.now()
    subject = f"Digest: {today.strftime('%a %b %d')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg["sender"]
    msg["To"] = email_cfg["recipient"]
    msg.attach(MIMEText(html, "html"))

    log.info(f"Sending email to {email_cfg['recipient']}")
    with smtplib.SMTP(email_cfg.get("smtp_server", "smtp.gmail.com"),
                      email_cfg.get("smtp_port", 587)) as server:
        server.starttls()
        server.login(email_cfg["sender"], email_cfg["app_password"])
        server.send_message(msg)

    log.info("Email sent successfully")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    login_x = "--login" in sys.argv
    login_linkedin = "--login-linkedin" in sys.argv
    x_only = "--x-only" in sys.argv
    linkedin_only = "--linkedin-only" in sys.argv
    no_like = "--no-like" in sys.argv
    dry_run = "--dry-run" in sys.argv

    log.info("=== Daily Digest ===")
    config = load_config()

    if login_x:
        await fetch_tweets_browser(config, login_mode=True)
        return
    if login_linkedin:
        await fetch_linkedin_posts_browser(config, login_mode=True)
        return

    state = load_state(config)
    log.info(f"State: {len(state.get('sent_items', []))} recent items in memory")

    tweets = []
    if not linkedin_only:
        tweets = await fetch_tweets_browser(config)
        log.info(f"Fetched {len(tweets)} tweets from "
                 f"{len(set(t.get('handle', '') for t in tweets))} accounts")

    linkedin_posts = []
    if not x_only:
        linkedin_posts = await fetch_linkedin_posts_browser(config)
        log.info(f"Fetched {len(linkedin_posts)} LinkedIn posts from "
                 f"{len(set(p.get('name', '') for p in linkedin_posts))} people")

    if not tweets and not linkedin_posts:
        log.warning("No content collected from any source")

    # Pre-Gemini URL filter (X only; LinkedIn dedup happens in-prompt)
    tweets = filter_by_sent_urls(tweets, state)

    # Single Gemini call → ranked items
    items = summarize_combined(tweets, linkedin_posts, state, config)

    # Render + build email
    summary_html = render_digest_html(items)
    stats = {
        "item_count": len(items),
        "tweet_count": len(tweets),
        "post_count": len(linkedin_posts),
    }
    full_html = build_email_html(summary_html, stats)

    if dry_run:
        log.info("--dry-run: printing HTML instead of sending / liking / saving state")
        print(full_html)
        return

    send_email(full_html, config)

    # Persist today's items to state
    today_str = datetime.now().strftime("%Y-%m-%d")
    for item in items:
        state["sent_items"].append({
            "url": item.get("url", ""),
            "platform": item.get("platform"),
            "summary": item.get("summary"),
            "category": item.get("category"),
            "sent_on": today_str,
        })
    save_state(state)
    log.info(f"Saved {len(items)} new items to state")

    # Auto-like top X tweets
    if not no_like:
        like_count = config.get("settings", {}).get("auto_like_count", 3)
        x_urls = [i["url"] for i in items if i.get("platform") == "x"][:like_count]
        if x_urls:
            log.info(f"Liking top {len(x_urls)} X tweets")
            await like_top_tweets(x_urls, config)
        else:
            log.info("No X items to like")

    log.info("Done!")


if __name__ == "__main__":
    asyncio.run(main())

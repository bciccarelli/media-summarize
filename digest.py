#!/usr/bin/env python3
"""Weekly X/Twitter + LinkedIn digest: scroll feeds with browser, summarize with Gemini, email via Gmail."""

import asyncio
import logging
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
# Summarization
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """\
You are an analyst creating a weekly digest of X/Twitter posts for an AI engineer.
Produce a concise, scannable summary in HTML format (no markdown — raw HTML only).

STRUCTURE YOUR OUTPUT AS FOLLOWS (use <h2> tags for sections):

<h2>Top Highlights</h2>
The 3-5 most important developments this week. Each with a one-sentence summary,
@handle attribution, and link to the original tweet.

<h2>Research & Papers</h2>
New research, papers, benchmarks, or scientific findings.

<h2>Tools & Products</h2>
New tools, product launches, feature announcements, open source releases.

<h2>Industry & Business</h2>
Funding, acquisitions, partnerships, hiring trends, policy/regulation.

<h2>Opinions & Analysis</h2>
Hot takes, threads with analysis, predictions, debates.

<h2>Actionable Items</h2>
Things the reader should DO: try a new tool, read a paper, apply to a job,
sign up for a beta, attend an event. Be specific.

RULES:
- Each item: one sentence summary + @handle attribution + <a href> link
- Skip low-value tweets (casual banter, memes, self-promotion without substance)
- Content should be relevant to AI, machine-learning, data science, or broad but relevant technology trends. 
- Group related tweets from different accounts on the same topic
- If a tweet is a thread, summarize the full thread
- Use HTML formatting: <h2>, <ul>, <li>, <a>, <strong>
- Keep total output under 2000 words
- If a section has no relevant items, omit it entirely
- Do NOT wrap output in ```html code blocks — just output raw HTML

TODAY'S DATE: {today}

TWEET DATA:
{tweet_data}
"""


def format_tweets_for_prompt(tweets: list[dict]) -> str:
    parts = []
    for t in tweets:
        handle = t.get("handle", "unknown")
        parts.append(
            f"@{handle}: {t['text']} | {t.get('date', '')} | {t.get('url', '')} | "
            f"{t.get('likes', 0)} likes | {t.get('retweets', 0)} RTs"
        )
    return "\n".join(parts)


def summarize(tweets: list[dict], config: dict) -> str:
    """Send tweets to Gemini and return HTML summary."""
    tweet_data = format_tweets_for_prompt(tweets)
    if not tweet_data.strip():
        return "<p>No tweets were collected this week.</p>"

    gem = config["gemini"]
    client = genai.Client(api_key=gem["api_key"])
    model = gem.get("model", "gemini-2.5-flash")

    prompt = PROMPT_TEMPLATE.format(tweet_data=tweet_data, today=datetime.now().strftime("%Y-%m-%d"))
    log.info(f"Sending {len(tweet_data)} chars to Gemini ({model})")

    response = client.models.generate_content(model=model, contents=prompt)
    summary = response.text or "<p>Gemini returned no output.</p>"

    log.info(f"Gemini returned {len(summary)} chars")
    return summary


LINKEDIN_PROMPT_TEMPLATE = """\
You are an analyst creating a weekly LinkedIn digest for a data scientist
who works at startups. Focus on ACTIONABLE information from their professional
network.

Produce a concise, scannable summary in HTML format (no markdown — raw HTML only).

STRUCTURE YOUR OUTPUT AS FOLLOWS (use <h2> tags for sections):

<h2>Network Moves</h2>
People changing jobs, getting promoted, starting companies.
Note the person's name, their title, and the move.

<h2>Opportunities</h2>
Job postings, open roles, freelance gigs, collaborations, startup hiring,
funding announcements (which imply hiring). Prioritize data science,
ML, AI, and startup roles.

<h2>Events & Learning</h2>
Conferences, webinars, workshops, courses, meetups. Include dates if visible.

<h2>Industry Signal</h2>
Trends, opinions, analyses, and shared articles about data science, AI/ML,
startups, or relevant tech. Focus on what's shifting in the market.

<h2>Action Items</h2>
Concrete things the reader should DO this week: congratulate someone,
apply to a role, register for an event, read an article, reach out to
a contact. Be specific with names and links.

RULES:
- Each item: one sentence summary + person's name and title + link if available
- Skip low-value content (generic motivational posts, engagement bait,
  polls without substance, reshares without commentary)
- Author titles/roles provide important context — include them
- Group related posts (e.g., multiple people discussing the same topic)
- Use HTML formatting: <h2>, <ul>, <li>, <a>, <strong>
- Keep total output under 1500 words
- If a section has no relevant items, omit it entirely
- Do NOT wrap output in ```html code blocks — just output raw HTML
- Discard events, deadlines, or opportunities whose date has already passed

TODAY'S DATE: {today}

LINKEDIN POST DATA:
{post_data}
"""


def format_linkedin_posts_for_prompt(posts: list[dict]) -> str:
    parts = []
    for p in posts:
        parts.append(
            f"{p.get('name', 'Unknown')} ({p.get('title', '')}) "
            f"[{p.get('postType', 'post')}]: {p['text']} | "
            f"{p.get('url', '')} | "
            f"{p.get('reactions', 0)} reactions | "
            f"{p.get('comments', 0)} comments"
        )
    return "\n".join(parts)


def summarize_linkedin(posts: list[dict], config: dict) -> str:
    """Send LinkedIn posts to Gemini and return HTML summary."""
    post_data = format_linkedin_posts_for_prompt(posts)
    if not post_data.strip():
        return "<p>No LinkedIn posts were collected this week.</p>"

    gem = config["gemini"]
    client = genai.Client(api_key=gem["api_key"])
    model = gem.get("model", "gemini-2.5-flash")

    prompt = LINKEDIN_PROMPT_TEMPLATE.format(post_data=post_data, today=datetime.now().strftime("%Y-%m-%d"))
    log.info(f"Sending {len(post_data)} chars (LinkedIn) to Gemini ({model})")

    response = client.models.generate_content(model=model, contents=prompt)
    summary = response.text or "<p>Gemini returned no output.</p>"

    log.info(f"Gemini returned {len(summary)} chars (LinkedIn)")
    return summary


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_email_html(x_summary: str, linkedin_summary: str,
                     config: dict, x_stats: dict, linkedin_stats: dict) -> str:
    today = datetime.now()
    week_ago = today - timedelta(days=config.get("settings", {}).get("lookback_days", 7))
    date_range = f"{week_ago.strftime('%b %d')} – {today.strftime('%b %d, %Y')}"

    # X section (only if tweets were collected)
    x_section = ""
    if x_stats["total_tweets"] > 0:
        x_section = f"""
  <div style="margin-bottom: 30px;">
    <h1 style="border-bottom: 2px solid #1da1f2; padding-bottom: 10px;
               font-size: 22px; margin-bottom: 5px;">
      X / Twitter &mdash; {date_range}
    </h1>
    <p style="color: #888; font-size: 13px; margin-top: 0;">
      {x_stats['total_tweets']} tweets from {x_stats['unique_handles']} accounts
    </p>
    {x_summary}
  </div>"""

    # LinkedIn section (only if posts were collected)
    linkedin_section = ""
    if linkedin_stats["total_posts"] > 0:
        linkedin_section = f"""
  <div style="margin-bottom: 30px;">
    <h1 style="border-bottom: 2px solid #0077b5; padding-bottom: 10px;
               font-size: 22px; margin-bottom: 5px;">
      LinkedIn &mdash; {date_range}
    </h1>
    <p style="color: #888; font-size: 13px; margin-top: 0;">
      {linkedin_stats['total_posts']} posts from {linkedin_stats['unique_authors']} people
    </p>
    {linkedin_summary}
  </div>"""

    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, Segoe UI, Arial, sans-serif;
             max-width: 640px; margin: 0 auto; padding: 20px;
             color: #333; line-height: 1.6; font-size: 15px;">
  {x_section}
  {linkedin_section}
  <hr style="margin-top: 30px; border: none; border-top: 1px solid #eee;">
  <p style="color: #aaa; font-size: 11px;">
    Generated {today.strftime('%Y-%m-%d %H:%M')}
  </p>
</body>
</html>"""


def send_email(html: str, config: dict):
    """Send the digest email via Gmail SMTP."""
    email_cfg = config["email"]
    today = datetime.now()
    week_ago = today - timedelta(days=config.get("settings", {}).get("lookback_days", 7))
    subject = f"Weekly Digest: {week_ago.strftime('%b %d')} – {today.strftime('%b %d')}"

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

    log.info("=== Weekly Digest ===")
    config = load_config()

    # Handle login modes (one at a time, then exit)
    if login_x:
        await fetch_tweets_browser(config, login_mode=True)
        return
    if login_linkedin:
        await fetch_linkedin_posts_browser(config, login_mode=True)
        return

    # Fetch X (skip if --linkedin-only)
    tweets = []
    if not linkedin_only:
        tweets = await fetch_tweets_browser(config)
        log.info(f"Fetched {len(tweets)} tweets from "
                 f"{len(set(t.get('handle', '') for t in tweets))} accounts")

    # Fetch LinkedIn (skip if --x-only; sequential — less detectable)
    linkedin_posts = []
    if not x_only:
        linkedin_posts = await fetch_linkedin_posts_browser(config)
        log.info(f"Fetched {len(linkedin_posts)} LinkedIn posts from "
                 f"{len(set(p.get('name', '') for p in linkedin_posts))} people")

    if not tweets and not linkedin_posts:
        log.warning("No content collected from any source")

    # Summarize
    x_summary = summarize(tweets, config) if tweets else ""
    li_summary = summarize_linkedin(linkedin_posts, config) if linkedin_posts else ""

    # Combined email
    x_total = len(tweets)
    x_handles = len(set(t.get("handle", "") for t in tweets))
    li_total = len(linkedin_posts)
    li_authors = len(set(p.get("name", "") for p in linkedin_posts))

    x_stats = {"total_tweets": x_total, "unique_handles": x_handles}
    li_stats = {"total_posts": li_total, "unique_authors": li_authors}
    full_html = build_email_html(x_summary, li_summary, config, x_stats, li_stats)
    send_email(full_html, config)

    log.info("Done!")


if __name__ == "__main__":
    asyncio.run(main())

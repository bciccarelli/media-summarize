#!/usr/bin/env python3
"""Daily X digest: scroll feed, rank with Gemini, email top items."""

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
    """Drop tweets already surfaced in a recent digest."""
    sent_urls = {
        item["url"] for item in state.get("sent_items", [])
        if item.get("url")
    }
    filtered = [t for t in tweets if t.get("url") not in sent_urls]
    dropped = len(tweets) - len(filtered)
    if dropped:
        log.info(f"Dedup dropped {dropped} tweets seen in last window")
    return filtered


# ---------------------------------------------------------------------------
# Summarization (single Gemini call, JSON output)
# ---------------------------------------------------------------------------

DIGEST_PROMPT = """\
You are curating a daily digest for an AI Solutions Architect. They build
production interfaces and data pipelines that solve real end-user problems
with AI. Their bar for what's worth reading is EXTREMELY high.

EVERY item must be directly relevant to their work — building AI-powered
interfaces, data pipelines, and end-user solutions. If you can't articulate
how it changes the way they design or ship AI products, skip it. Cool but
unrelated items (robotics, biotech, hardware, gaming, crypto, general tech
news) DO NOT BELONG in this digest no matter how impressive they are.

ONLY surface items that could change their daily workflow. Skip everything
else, even if it's interesting. An empty digest is the right answer most days.

TODAY'S DATE: {today}

THE BAR — only include items in these categories:

1. FRONTIER MODEL RELEASE — a major lab (Anthropic, OpenAI, Google DeepMind,
   Meta, xAI, Mistral, DeepSeek, Qwen) ships a new flagship model or a
   meaningful capability upgrade. Examples: "Claude 4.7 launches", "GPT-5
   ships with native video", "Gemini 3 Pro tops the leaderboards".

2. MAJOR PRODUCT ANNOUNCEMENT that could replace a tool in their stack.
   Examples: "Claude Design launches (Figma replacement powered by AI)",
   "Cursor 2.0 with multi-agent", "Vercel ships v0 enterprise", "Anthropic
   launches Computer Use API GA", "OpenAI launches Agent Builder", "Replit
   acquires X for agent infra".

3. NEW API / CAPABILITY from a major AI provider that meaningfully changes
   how to build with AI. Examples: prompt caching launches, structured
   output goes GA, native multi-modal endpoints, new reasoning modes,
   pricing cuts that change architecture decisions.

4. INFRASTRUCTURE / DEVTOOL that an AI builder would actually use:
   vector DBs, eval frameworks, agent platforms, RAG tooling — but ONLY
   when it's a substantive launch from a known player, not a YC demo.

5. SOTA / FRONTIER RESEARCH that meaningfully shifts what's possible.
   Examples: a new architecture beating transformers on real tasks, a
   capability jump (e.g. "1M-context with no quality loss"), a paper
   from a top lab (Anthropic, DeepMind, OpenAI, FAIR, Stanford, MIT)
   showing a result that changes how systems get built. Skip incremental
   benchmark wins, niche results, or papers nobody is talking about.

6. HARDWARE / PRICING news that could change the economics of their
   projects. Examples: a new NVIDIA chip with major perf-per-dollar
   improvement, GPU availability shifts, inference cost cuts from a
   provider, a new accelerator launch (Cerebras, Groq, AWS Trainium)
   that changes deploy decisions. ONLY include when the cost or
   availability impact is concrete and quantified.

7. LESSONS FROM THE FIELD — AT MOST ONE PER DAY. A specific,
   battle-tested insight from a credible AI practitioner (CTO, staff
   engineer, or founder at a known AI company actually shipping
   production systems) about building real AI products. Must contain
   a concrete, actionable lesson — not vague advice. Example: "After
   shipping RAG to 10M users, we learned that re-ranking matters more
   than embeddings for our use case, here's the eval data". Skip
   anything from anonymous accounts, generic "founder mode" content,
   or thought-leadership without specifics.

EXPLICITLY SKIP (do NOT include, even if highly upvoted):
- Incremental research / benchmark wins on niche tasks
- Hot takes, predictions, "the future of X" threads
- Founder advice, hiring tips, career thoughts
- Funding announcements (unless paired with a product launch)
- "Just shipped a side project" posts unless it's a known team
- Event announcements, conferences, webinars
- Tutorials, "how I built X" threads
- Memes, screenshots, casual banter
- Anything semantically covered in RECENTLY COVERED below

RULES:
- Return AT MOST {max_items} items. Most days return 0-2.
- Only include items scoring {min_score}+ on this scale:
  - 10 = a tool they will install/integrate this week
  - 9 = changes their architecture decisions
  - 8 = they need to know this exists; bar to include
  - <8 = don't include
- If nothing meets the bar, return an empty list. That is correct.
- Prefer the original announcement over commentary about it. If multiple
  posts cover the same launch, pick the most authoritative source.

RECENTLY COVERED (do NOT re-surface these topics):
{recently_covered}

CATEGORIES (use exactly one):
Model Release | Product Launch | API / Capability | Infrastructure | Frontier Research | Hardware / Pricing | Field Lesson

Constraint: AT MOST ONE "Field Lesson" item per digest, ever. If you have
two strong field lessons, pick the better one and drop the other.

For each item:
- url: exact tweet URL from input
- author: @handle (prefer the official company/launcher account)
- category: one of the four above
- summary: ONE sentence, <=25 words, naming the product and what it does
- signal_score: integer 8-10
- reason: short phrase explaining workflow impact (e.g. "replaces Figma",
  "ships native video", "cuts inference cost 5x")

X POSTS:
{x_data}
"""


DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "author": {"type": "string"},
                    "category": {"type": "string"},
                    "summary": {"type": "string"},
                    "signal_score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "url", "author", "category",
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


def format_recently_covered(state: dict) -> str:
    items = state.get("sent_items", [])
    if not items:
        return "(nothing sent recently)"
    items_sorted = sorted(items, key=lambda i: i.get("sent_on", ""), reverse=True)
    return "\n".join(
        f"[{i.get('sent_on', '?')}] {i.get('category', '?')}: {i.get('summary', '')}"
        for i in items_sorted[:30]
    )


def summarize_combined(tweets: list[dict], state: dict, config: dict) -> list[dict]:
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

    # Hard cap: at most 1 Field Lesson per digest
    capped: list[dict] = []
    field_lessons_kept = 0
    for item in items:
        if item.get("category") == "Field Lesson":
            if field_lessons_kept >= 1:
                continue
            field_lessons_kept += 1
        capped.append(item)
    items = capped[:max_items]

    log.info(f"Gemini surfaced {len(items)} items meeting signal floor")
    return items


# ---------------------------------------------------------------------------
# Render digest → HTML
# ---------------------------------------------------------------------------

CATEGORY_ORDER = [
    "Model Release", "Product Launch", "API / Capability",
    "Infrastructure", "Frontier Research", "Hardware / Pricing", "Field Lesson",
]


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
            parts.append(
                f'<li style="margin-bottom:10px;">'
                f'<strong>{summary}</strong><br>'
                f'<span style="color:#888;font-size:13px;">'
                f'{author} · {reason} · '
                f'score {score}{" · " + link_html if link_html else ""}'
                f'</span></li>'
            )
        parts.append("</ul>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Suggested likes (top X items to manually engage with)
# ---------------------------------------------------------------------------

def render_suggested_likes_html(items: list[dict], count: int) -> str:
    """Pick the top N items by signal score and render a quick-action section."""
    ranked = [i for i in items if i.get("url")]
    ranked.sort(key=lambda i: i.get("signal_score", 0), reverse=True)
    picks = ranked[:count]
    if not picks:
        return ""

    lines = [
        '<h2 style="font-size:16px;margin-top:32px;margin-bottom:8px;color:#555;">'
        'Worth a like (manual)</h2>',
        '<p style="color:#888;font-size:13px;margin-top:0;">'
        'Click through and like these to refine your X feed.</p>',
        '<ol style="padding-left:20px;margin-top:8px;">',
    ]
    for item in picks:
        url = item.get("url", "")
        author = item.get("author", "")
        summary = item.get("summary", "")
        lines.append(
            f'<li style="margin-bottom:8px;">'
            f'<a href="{url}" style="color:#1da1f2;">{author}</a> — {summary}'
            f'</li>'
        )
    lines.append("</ol>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_email_html(summary_html: str, suggest_html: str, stats: dict) -> str:
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
    {stats['item_count']} items surfaced from {stats['tweet_count']} tweets
  </p>

  {summary_html}

  {suggest_html}

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
    dry_run = "--dry-run" in sys.argv

    log.info("=== Daily Digest ===")
    config = load_config()

    if login_x:
        await fetch_tweets_browser(config, login_mode=True)
        return

    state = load_state(config)
    log.info(f"State: {len(state.get('sent_items', []))} recent items in memory")

    tweets = await fetch_tweets_browser(config)
    log.info(f"Fetched {len(tweets)} tweets from "
             f"{len(set(t.get('handle', '') for t in tweets))} accounts")

    if not tweets:
        log.warning("No tweets collected")

    tweets = filter_by_sent_urls(tweets, state)

    items = summarize_combined(tweets, state, config)

    if not items:
        log.info("No items met the signal bar — skipping email")
        return

    summary_html = render_digest_html(items)
    suggest_count = config.get("settings", {}).get("suggest_like_count", 3)
    suggest_html = render_suggested_likes_html(items, suggest_count)
    stats = {
        "item_count": len(items),
        "tweet_count": len(tweets),
    }
    full_html = build_email_html(summary_html, suggest_html, stats)

    if dry_run:
        log.info("--dry-run: printing HTML instead of sending / saving state")
        print(full_html)
        return

    send_email(full_html, config)

    today_str = datetime.now().strftime("%Y-%m-%d")
    for item in items:
        state["sent_items"].append({
            "url": item.get("url", ""),
            "summary": item.get("summary"),
            "category": item.get("category"),
            "sent_on": today_str,
        })
    save_state(state)
    log.info(f"Saved {len(items)} new items to state")

    log.info("Done!")


if __name__ == "__main__":
    asyncio.run(main())

import os
import re
import json
import time
import html
import argparse
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urljoin, quote
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from io import BytesIO

import requests
import feedparser
import trafilatura
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageFile
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from exa_py import Exa
from cerebras.cloud.sdk import Cerebras


# ============================================================
# CONFIGURATION
# ============================================================

EXA_API_KEY = os.environ["EXA_API_KEY"]
CEREBRAS_API_KEY = os.environ["CEREBRAS_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

TELEGRAM_CHANNEL = (os.environ.get("TELEGRAM_CHANNEL") or "@TheTechNewsroom").strip()

# Optional. If set, feed-down alerts go here (a private chat/DM with the
# bot, not the public channel). If empty, alerts only go to the run log.
TELEGRAM_ADMIN_CHAT_ID = (os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or "").strip()

NEWS_MODE = (os.environ.get("NEWS_MODE") or "update").strip().lower()

VALID_NEWS_MODES = {"update"}
if NEWS_MODE not in VALID_NEWS_MODES:
    raise ValueError(
        f"Invalid NEWS_MODE={NEWS_MODE!r}; expected one of {sorted(VALID_NEWS_MODES)}"
    )

CEREBRAS_MODEL = os.environ.get(
    "CEREBRAS_MODEL",
    "gpt-oss-120b",
)

POSTED_FILE = "posted_urls.txt"
STATE_FILE = "news_state.json"

BD_TZ = ZoneInfo("Asia/Dhaka")

# Version 1 editorial target: publish only clearly important tech stories.
# All tech stories compete in one ranked pool. Six is a safety cap, not a quota.
MAX_STORIES_PER_RUN = 6
RANKING_POOL_SIZE = 24
DISCOVERY_LOOKBACK_HOURS = 24

# Reliability / quality
POST_DELAY_SECONDS = 3.5
ROLLING_DISCOVERY_HOURS = DISCOVERY_LOOKBACK_HOURS
FUTURE_TOLERANCE_MINUTES = 10
QUEUE_RETENTION_DAYS = 4
EVENT_RETENTION_DAYS = 30
MAX_RSS_CANDIDATES = 240
MAX_EXA_CANDIDATES = 60
MAX_GOOGLE_NEWS_CANDIDATES = 40
THIN_EXCERPT_CHARS = 150
MAX_EXCERPT_ENRICH = 12
MAX_SOURCE_PER_RUN = 99
MAX_RICH_CHARACTERS = 32768

# Lightweight English stopwords used only by the conservative event/entity
# deduplication layer. This is deliberately small so technology entities and
# meaningful short terms such as AI, OS, UI, and VR are retained.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for",
    "from", "with", "by", "at", "as", "is", "are", "was", "were",
    "be", "been", "being", "has", "have", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can",
    "this", "that", "these", "those", "it", "its", "their", "they",
    "them", "he", "she", "his", "her", "we", "our", "you", "your",
    "new", "after", "before", "over", "into", "than", "about", "from",
}

# RSS-first sources. Exa remains a fallback/gap filler.
RSS_FEEDS = [
    {"name": "TechCrunch", "region": "Tech", "url": "https://techcrunch.com/feed/"},
    {"name": "The Verge", "region": "Tech", "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "WIRED", "region": "Tech", "url": "https://www.wired.com/feed/rss"},
    {"name": "Ars Technica", "region": "Tech", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "Engadget", "region": "Tech", "url": "https://www.engadget.com/rss.xml"},
    {"name": "MIT Technology Review", "region": "Tech", "url": "https://www.technologyreview.com/feed/"},
    {"name": "Hacker News", "region": "Tech", "url": "https://news.ycombinator.com/rss"},
    {"name": "VentureBeat", "region": "Tech", "url": "https://venturebeat.com/feed/"},
    {"name": "Techmeme", "region": "Tech", "url": "https://www.techmeme.com/feed.xml"},
    {"name": "TechRadar", "region": "Tech", "url": "https://www.techradar.com/feeds/articletype/news"},
    {"name": "ZDNET", "region": "Tech", "url": "https://www.zdnet.com/news/rss.xml"},
    {"name": "9to5Google", "region": "Tech", "url": "https://9to5google.com/feed/"},
    {"name": "WABetaInfo", "region": "Tech", "url": "https://wabetainfo.com/feed/"},
    {"name": "TestingCatalog", "region": "Tech", "url": "https://www.testingcatalog.com/feed/"},
    {"name": "AI News", "region": "Tech", "url": "https://www.artificialintelligence-news.com/feed/"},
    {"name": "Unite.AI", "region": "Tech", "url": "https://unite.ai/feed/"},
    {"name": "The Decoder", "region": "Tech", "url": "https://the-decoder.com/feed/"},
    {"name": "SiliconANGLE", "region": "Tech", "url": "https://siliconangle.com/feed/"},
    {"name": "Android Authority", "region": "Tech", "url": "https://www.androidauthority.com/feed/"},
    {"name": "MacRumors", "region": "Tech", "url": "https://www.macrumors.com/macrumors.xml"},
]




# ============================================================
# TAXONOMY: TECH NEWS

TOPICS = {
    "Tech": [
        "Consumer Technology",
        "AI Models and Products",
        "Smartphones",
        "Operating Systems",
        "Browsers",
        "Search",
        "Social Platforms",
        "Cloud Platforms",
        "App Stores",
        "Cybersecurity",
        "Privacy",
        "Major Tech Companies",
        "New Products",
        "Technology Industry",
        "Open Source",
        "GitHub Trends",
        "Startups",
        "Y Combinator",
        "Hugging Face",
        "Major Outages",
        "Acquisitions and Mergers",
        "Layoffs and Restructuring",
        "Pricing and Subscriptions",
    ]
}

INSTITUTIONS = [
    "Apple", "Google", "Microsoft", "OpenAI", "Meta", "Amazon", "Anthropic",
    "NVIDIA", "Samsung", "Qualcomm", "Intel", "AMD", "ByteDance", "TikTok",
    "X", "Tesla", "Cloudflare", "GitHub", "GitLab", "Hugging Face", "Y Combinator",
]

SOURCE_NAMES = {
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "wired.com": "WIRED",
    "arstechnica.com": "Ars Technica",
    "engadget.com": "Engadget",
    "technologyreview.com": "MIT Technology Review",
    "news.ycombinator.com": "Hacker News",
    "venturebeat.com": "VentureBeat",
    "techmeme.com": "Techmeme",
    "techradar.com": "TechRadar",
    "zdnet.com": "ZDNET",
    "9to5google.com": "9to5Google",
    "wabetainfo.com": "WABetaInfo",
    "testingcatalog.com": "TestingCatalog",
    "artificialintelligence-news.com": "AI News",
    "unite.ai": "Unite.AI",
    "the-decoder.com": "The Decoder",
    "siliconangle.com": "SiliconANGLE",
    "androidauthority.com": "Android Authority",
    "macrumors.com": "MacRumors",
}

CATEGORY_HASHTAGS = {
    "Consumer Technology": ["#Tech", "#ConsumerTech"],
    "AI Models and Products": ["#AI", "#ArtificialIntelligence"],
    "Smartphones": ["#Smartphones", "#MobileTech"],
    "Operating Systems": ["#OperatingSystems", "#Tech"],
    "Browsers": ["#Browsers", "#Internet"],
    "Search": ["#Search", "#Tech"],
    "Social Platforms": ["#SocialMedia", "#Tech"],
    "Cloud Platforms": ["#Cloud", "#Tech"],
    "App Stores": ["#AppStores", "#Tech"],
    "Cybersecurity": ["#Cybersecurity", "#Security"],
    "Privacy": ["#Privacy", "#Tech"],
    "Major Tech Companies": ["#BigTech", "#Tech"],
    "New Products": ["#Tech", "#NewProduct"],
    "Technology Industry": ["#TechIndustry", "#Tech"],
    "Open Source": ["#OpenSource", "#Tech"],
    "GitHub Trends": ["#GitHub", "#OpenSource"],
    "Startups": ["#Startups", "#Tech"],
    "Y Combinator": ["#YC", "#Startups"],
    "Hugging Face": ["#HuggingFace", "#AI"],
    "Major Outages": ["#Tech", "#Outage"],
    "Acquisitions and Mergers": ["#TechIndustry", "#Mergers"],
    "Layoffs and Restructuring": ["#TechIndustry", "#Layoffs"],
    "Pricing and Subscriptions": ["#Tech", "#Subscriptions"],
}

CATEGORY_GROUPS = {
    "AI": {"AI Models and Products", "Hugging Face"},
    "Platforms": {"Smartphones", "Operating Systems", "Browsers", "Search", "Social Platforms", "Cloud Platforms", "App Stores", "Major Outages"},
    "Security": {"Cybersecurity", "Privacy"},
    "Industry": {"Major Tech Companies", "Technology Industry", "Acquisitions and Mergers", "Layoffs and Restructuring", "Startups", "Y Combinator"},
    "Products and Ecosystem": {"Consumer Technology", "New Products", "Pricing and Subscriptions", "Open Source", "GitHub Trends"},
}

TOPIC_ALIASES = {
    "ai": "AI Models and Products",
    "artificial intelligence": "AI Models and Products",
    "smartphone": "Smartphones",
    "phones": "Smartphones",
    "android": "Operating Systems",
    "ios": "Operating Systems",
    "windows": "Operating Systems",
    "linux": "Operating Systems",
    "browser": "Browsers",
    "search": "Search",
    "social": "Social Platforms",
    "cloud": "Cloud Platforms",
    "app store": "App Stores",
    "security": "Cybersecurity",
    "privacy": "Privacy",
    "outage": "Major Outages",
    "github": "GitHub Trends",
    "open source": "Open Source",
    "startup": "Startups",
    "yc": "Y Combinator",
    "hugging face": "Hugging Face",
    "acquisition": "Acquisitions and Mergers",
    "merger": "Acquisitions and Mergers",
    "layoffs": "Layoffs and Restructuring",
    "subscription": "Pricing and Subscriptions",
}

def canonical_topic(topic, region="Tech"):
    key = safe_text(topic).lower().strip()
    if key in TOPIC_ALIASES:
        return TOPIC_ALIASES[key]
    for item in TOPICS["Tech"]:
        if key == item.lower():
            return item
    patterns = [
        (("ai", "artificial intelligence", "model", "chatgpt", "claude", "gemini"), "AI Models and Products"),
        (("iphone", "pixel", "galaxy", "smartphone", "phone"), "Smartphones"),
        (("android", "ios", "windows", "macos", "linux", "operating system"), "Operating Systems"),
        (("browser", "chrome", "safari", "firefox", "edge"), "Browsers"),
        (("search", "google search"), "Search"),
        (("social", "instagram", "facebook", "tiktok", "x.com"), "Social Platforms"),
        (("cloud", "aws", "azure", "gcp"), "Cloud Platforms"),
        (("app store", "play store"), "App Stores"),
        (("breach", "hack", "vulnerability", "cybersecurity", "malware"), "Cybersecurity"),
        (("privacy", "tracking", "data collection"), "Privacy"),
        (("outage", "down", "service disruption"), "Major Outages"),
        (("github", "repository", "repo", "trendshift"), "GitHub Trends"),
        (("open source", "open-source"), "Open Source"),
        (("startup", "unicorn"), "Startups"),
        (("y combinator", "yc"), "Y Combinator"),
        (("hugging face", "leaderboard"), "Hugging Face"),
        (("acquisition", "acquires", "merge", "merger"), "Acquisitions and Mergers"),
        (("layoff", "job cuts", "restructuring"), "Layoffs and Restructuring"),
        (("subscription", "pricing", "price hike", "price"), "Pricing and Subscriptions"),
        (("launch", "product", "device"), "New Products"),
        (("company", "microsoft", "apple", "google", "amazon", "meta", "openai"), "Major Tech Companies"),
    ]
    for needles, canonical in patterns:
        if any(needle in key for needle in needles):
            return canonical
    return "Consumer Technology"

def category_hashtags(story):
    tags = []
    topic = safe_text(story.get("topic"))
    institution = safe_text(story.get("institution"))
    for tag in CATEGORY_HASHTAGS.get(topic, []):
        if tag not in tags:
            tags.append(tag)
    inst_map = {
        "Apple": "#Apple", "Google": "#Google", "Microsoft": "#Microsoft",
        "OpenAI": "#OpenAI", "Meta": "#Meta", "Amazon": "#Amazon",
        "Anthropic": "#Anthropic", "NVIDIA": "#NVIDIA", "Samsung": "#Samsung",
        "GitHub": "#GitHub", "Hugging Face": "#HuggingFace",
    }
    if institution in inst_map and inst_map[institution] not in tags:
        tags.append(inst_map[institution])
    if "#Tech" not in tags:
        tags.append("#Tech")
    if "#Tech" not in tags[:3]:
        tags = tags[:2] + ["#Tech"]
    return tags[:3]

def coverage_state():
    return STATE.setdefault("category_coverage", {})


def update_category_coverage(story):
    topic = safe_text(story.get("topic"))
    if topic:
        coverage_state()[topic] = now_iso()


def refresh_category_coverage():
    coverage = coverage_state()
    for event in STATE.get("events", {}).values():
        if event.get("status") != "published":
            continue
        published_at = parse_datetime(event.get("published_at"))
        if not published_at or published_at.date() != NOW_BD.date():
            continue
        topic = safe_text(event.get("topic"))
        if topic:
            coverage[topic] = event.get("selected_at", published_at.isoformat())


# ============================================================
# LOGGING + HTTP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("tech-news-bot")

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 50_000_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    )
}

session = requests.Session()
session.headers.update(HEADERS)

retry_policy = Retry(
    total=4,
    connect=4,
    read=4,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=True,
)

adapter = HTTPAdapter(
    max_retries=retry_policy,
    pool_connections=20,
    pool_maxsize=20,
)

session.mount("https://", adapter)
session.mount("http://", adapter)


# ============================================================
# HELPERS
# ============================================================

def safe_text(value):
    return "" if value is None else str(value).strip()


def canonical_url(url):
    raw = safe_text(url)
    if not raw:
        return ""

    parsed = urlparse(raw)

    host = (
        parsed.netloc.lower()
        .removeprefix("www.")
        .removeprefix("amp.")
    )

    path = parsed.path or "/"
    path = path.rstrip("/")
    path = re.sub(r"/amp$", "", path, flags=re.I)
    path = re.sub(r"\.amp$", "", path, flags=re.I)

    return f"{host}{path}"


def normalize_title(title):
    text = safe_text(title).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(text):
    text = normalize_title(text)
    return {
        token
        for token in text.split()
        if len(token) >= 3
    }


def token_jaccard(a, b):
    aa = title_tokens(a)
    bb = title_tokens(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / max(1, len(aa | bb))


def title_similarity(a, b):
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return 0.0
    sequence = SequenceMatcher(None, na, nb).ratio()
    jaccard = token_jaccard(na, nb)
    return max(sequence, jaccard)


def event_similarity(a, b):
    """Cheap event-level similarity without an embedding dependency."""
    sequence = SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()
    jaccard = token_jaccard(a, b)
    return (0.55 * sequence) + (0.45 * jaccard)


def likely_same_event(a, b):
    return (
        title_similarity(a, b) >= 0.90
        or event_similarity(a, b) >= 0.80
    )


def parse_datetime(value):
    raw = safe_text(value)
    if not raw:
        return None

    try:
        dt = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )
        return dt.astimezone(BD_TZ)
    except Exception:
        pass

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )
        return dt.astimezone(BD_TZ)
    except Exception:
        return None


def feed_entry_datetime(entry):
    for key in (
        "published_parsed",
        "updated_parsed",
        "created_parsed",
    ):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(
                    *parsed[:6],
                    tzinfo=timezone.utc,
                ).astimezone(BD_TZ)
            except Exception:
                pass

    for key in (
        "published",
        "updated",
        "created",
    ):
        dt = parse_datetime(
            entry.get(key)
        )
        if dt:
            return dt

    return None


def trim_source_text(text, limit):
    """Trim source text before rendering. Never appends ellipses."""
    text = safe_text(text)
    if len(text) <= limit:
        return text

    trimmed = text[:limit].rstrip()
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0]

    return trimmed.rstrip(" ,:;-/—")


def clean_generated_text(text):
    text = safe_text(text)

    # Prevent visible truncation artifacts.
    text = re.sub(r"\.{2,}", ".", text)
    text = text.replace("\u2026", "")

    # Remove incomplete endings.
    text = re.sub(
        r"\s*[,;:]\s*$",
        "",
        text,
    )
    text = re.sub(
        r"\s*[-—]\s*$",
        "",
        text,
    )

    return text.strip()


def complete_text(text):
    raw = safe_text(text)
    if not raw:
        return False

    # A text that clean_generated_text() would mutilate
    # (trailing dash/comma/colon) is INCOMPLETE.
    if re.search(r"[\s,;:\-—…]+$", raw):
        return False

    text = clean_generated_text(raw)
    if not text:
        return False

    return not text.endswith(
        (",", ";", ":", "-", "—", "…")
    )


def source_name(url):
    domain = (
        urlparse(
            safe_text(url)
        )
        .netloc
        .lower()
        .removeprefix("www.")
    )

    return SOURCE_NAMES.get(
        domain,
        domain or "Source",
    )


def article_region(url):
    return "Tech"


def now_iso():
    return datetime.now(
        BD_TZ
    ).isoformat()


# ============================================================
# STATE: QUEUE + EVENTS + KNOWLEDGE
# ============================================================

def default_state():
    return {
        "feeds": {},
        "queue": {},
        "events": {},
        "event_clusters": {},
        "posted_event_ids": [],
        "recent_titles": [],
    }


def load_state():
    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if not isinstance(
            data,
            dict,
        ):
            return default_state()

        base = default_state()
        base.update(data)

        return base

    except Exception:
        return default_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        tmp,
        STATE_FILE,
    )


def load_posted_urls():
    try:
        with open(
            POSTED_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            return {
                canonical_url(line)
                for line in f
                if safe_text(line)
            }
    except FileNotFoundError:
        return set()


def save_posted_url(canonical):
    if not canonical:
        return

    with open(
        POSTED_FILE,
        "a",
        encoding="utf-8",
    ) as f:
        f.write(
            canonical
            + "\n"
        )


STATE = load_state()
POSTED_URLS = load_posted_urls()


def prune_state():
    cutoff_queue = (
        datetime.now(BD_TZ)
        - timedelta(
            days=QUEUE_RETENTION_DAYS
        )
    )

    cutoff_events = (
        datetime.now(BD_TZ)
        - timedelta(
            days=EVENT_RETENTION_DAYS
        )
    )

    queue = STATE.get(
        "queue",
        {},
    )

    keep_queue = {}

    for key, item in queue.items():
        dt = parse_datetime(
            item.get("last_seen")
            or item.get("published_date")
        )

        if (
            dt
            and dt >= cutoff_queue
        ):
            keep_queue[key] = item

    STATE["queue"] = keep_queue

    events = STATE.get(
        "events",
        {},
    )

    keep_events = {}

    for key, event in events.items():
        dt = parse_datetime(
            event.get("published_at")
            or event.get("selected_at")
        )

        if (
            dt
            and dt >= cutoff_events
        ):
            keep_events[key] = event

    STATE["events"] = keep_events

    titles = STATE.get(
        "recent_titles",
        [],
    )

    STATE["recent_titles"] = titles[-400:]


# ============================================================
# TIME WINDOWS
# ============================================================

NOW_BD = datetime.now(
    BD_TZ
)

TODAY_START = NOW_BD.replace(
    hour=0,
    minute=0,
    second=0,
    microsecond=0,
)

YESTERDAY_START = (
    TODAY_START
    - timedelta(days=1)
)

DISCOVERY_START = (
    NOW_BD
    - timedelta(
        hours=ROLLING_DISCOVERY_HOURS
    )
)
DISCOVERY_END = (
    NOW_BD
    + timedelta(
        minutes=FUTURE_TOLERANCE_MINUTES
    )
)

DISCOVERY_TARGET_PER_REGION = 18


# ============================================================
# CLIENTS
# ============================================================

exa = None
cerebras = None


def get_exa():
    global exa
    if exa is None:
        exa = Exa(api_key=EXA_API_KEY)
    return exa


def get_cerebras():
    global cerebras
    if cerebras is None:
        cerebras = Cerebras(api_key=CEREBRAS_API_KEY)
    return cerebras


# ============================================================
# CANDIDATE FILTERING
# ============================================================

BAD_PATH_RE = re.compile(
    r"/(opinion|editorial|sponsored|"
    r"tag|topic|live-blog|liveblog|"
    r"photo|photos|video)(/|$)",
    re.I,
)

BAD_TITLE_RE = re.compile(
    r"\b(sponsored|advertisement|"
    r"promo|opinion|editorial)\b",
    re.I,
)


def candidate_basic_allowed(item):
    url = safe_text(
        item.get("url")
    )
    title = safe_text(
        item.get("title")
    )
    published = item.get(
        "published_dt"
    )

    if (
        not url
        or not title
        or not published
    ):
        return False

    if BAD_PATH_RE.search(
        urlparse(url).path
    ):
        return False

    if BAD_TITLE_RE.search(
        title
    ):
        return False

    if not (
        DISCOVERY_START
        <= published
        <= DISCOVERY_END
    ):
        return False

    region = safe_text(item.get("region"))
    if region and not allowed_source_for_region(url, region):
        return False

    canonical = canonical_url(
        url
    )

    return bool(
        canonical
    )


def title_duplicate_against_state(title):
    for previous in STATE.get(
        "recent_titles",
        [],
    )[-250:]:
        if title_similarity(
            title,
            previous,
        ) >= 0.88:
            return True

    return False


def title_duplicate_against_list(
    title,
    candidates,
    threshold=0.88,
):
    for candidate in candidates:
        if title_similarity(
            title,
            candidate["title"],
        ) >= threshold:
            return True

    return False


# ============================================================
# RSS INGESTION + PERSISTENT QUEUE
# ============================================================

def extract_entry_image(
    entry,
    page_url,
):
    for key in (
        "media_content",
        "media_thumbnail",
    ):
        for item in entry.get(
            key,
            [],
        ):
            image_url = safe_text(
                item.get("url")
            )

            if image_url:
                return urljoin(
                    page_url,
                    image_url,
                )

    for enclosure in entry.get(
        "enclosures",
        [],
    ):
        href = safe_text(
            enclosure.get("href")
        )

        mime = safe_text(
            enclosure.get("type")
        ).lower()

        if (
            href
            and (
                not mime
                or mime.startswith(
                    "image/"
                )
            )
        ):
            return urljoin(
                page_url,
                href,
            )

    return ""


def queue_candidate(item):
    canonical = item["canonical"]

    existing = STATE["queue"].get(
        canonical
    )

    if existing:
        existing.update(
            {
                "last_seen": now_iso(),
                "image": (
                    item.get("image")
                    or existing.get("image", "")
                ),
            }
        )
        return

    STATE["queue"][canonical] = {
        **item,
        "status": "pending",
        "first_seen": now_iso(),
        "last_seen": now_iso(),
    }


def fetch_rss_feed(
    feed_def,
):
    url = feed_def["url"]

    old = STATE["feeds"].get(
        url,
        {},
    )

    headers = dict(
        HEADERS
    )

    if old.get("etag"):
        headers["If-None-Match"] = old[
            "etag"
        ]

    if old.get(
        "last_modified"
    ):
        headers["If-Modified-Since"] = old[
            "last_modified"
        ]

    try:
        response = session.get(
            url,
            headers=headers,
            timeout=20,
        )

        # 304 means the queue remains intact. The feed is reachable,
        # so this counts as healthy and clears any fail streak.
        if response.status_code == 304:
            logger.info(
                "RSS 304: %s",
                feed_def["name"],
            )
            mark_feed_healthy(feed_def, old)
            return 0

        if response.status_code >= 400:
            logger.warning(
                "RSS %s returned %s",
                feed_def["name"],
                response.status_code,
            )
            mark_feed_failed(feed_def, old)
            return 0

        STATE["feeds"][url] = {
            "etag": response.headers.get(
                "ETag",
                old.get("etag"),
            ),
            "last_modified": response.headers.get(
                "Last-Modified",
                old.get("last_modified"),
            ),
            "last_checked": now_iso(),
            "fail_count": 0,
            "alerted": False,
        }

        parsed = feedparser.parse(
            response.content
        )

        added = 0

        for entry in parsed.entries:
            published_dt = feed_entry_datetime(
                entry
            )

            date_estimated = False

            if not published_dt:
                # Some feeds send a date format we cannot parse.
                # Do not throw the story away: use fetch time instead,
                # and mark it so downstream code knows it is a guess.
                published_dt = datetime.now(
                    BD_TZ
                )
                date_estimated = True

            article_url = urljoin(
                url,
                safe_text(
                    entry.get("link")
                ),
            )

            title = safe_text(
                entry.get("title")
            )

            if not article_url or not title:
                continue

            item = {
                "title": title,
                "url": article_url,
                "canonical": canonical_url(
                    article_url
                ),
                "published_dt": published_dt.isoformat(),
                "published_date": published_dt.isoformat(),
                "source": feed_def["name"],
                "region": feed_def["region"],
                "excerpt": BeautifulSoup(
                    safe_text(
                        entry.get(
                            "summary"
                        )
                        or entry.get(
                            "description"
                        )
                    ),
                    "html.parser",
                ).get_text(
                    " ",
                    strip=True,
                )[:2000],
                "image": extract_entry_image(
                    entry,
                    article_url,
                ),
                "discovery": "rss",
                "date_estimated": date_estimated,
            }

            if not candidate_basic_allowed(
                {
                    **item,
                    "published_dt": published_dt,
                }
            ):
                continue

            if (
                item["canonical"]
                in POSTED_URLS
            ):
                continue

            before = item["canonical"] in STATE[
                "queue"
            ]

            queue_candidate(
                item
            )

            if not before:
                added += 1

        return added

    except Exception as exc:
        logger.warning(
            "RSS failed %s: %s",
            feed_def["name"],
            exc,
        )
        mark_feed_failed(feed_def, old)
        return 0


# Consecutive failed runs before we alert about a broken feed.
FEED_FAIL_ALERT_THRESHOLD = 3


def mark_feed_healthy(feed_def, old):
    STATE["feeds"][feed_def["url"]] = {
        **old,
        "last_checked": now_iso(),
        "fail_count": 0,
        "alerted": False,
    }


def mark_feed_failed(feed_def, old):
    fail_count = int(old.get("fail_count", 0)) + 1

    STATE["feeds"][feed_def["url"]] = {
        **old,
        "last_checked": now_iso(),
        "fail_count": fail_count,
    }

    if fail_count >= FEED_FAIL_ALERT_THRESHOLD and not old.get("alerted"):
        alert_feed_down(feed_def, fail_count)
        STATE["feeds"][feed_def["url"]]["alerted"] = True


def alert_feed_down(feed_def, fail_count):
    """Tell the admin a source has gone quiet, instead of failing silently forever."""
    message = (
        f"Feed down: {feed_def['name']} ({feed_def['region']})\n"
        f"Failed {fail_count} runs in a row.\n"
        f"URL: {feed_def['url']}\n"
        f"It will keep retrying, but this source is not feeding the bot right now."
    )

    if TELEGRAM_ADMIN_CHAT_ID:
        try:
            telegram_call(
                "sendMessage",
                data={
                    "chat_id": TELEGRAM_ADMIN_CHAT_ID,
                    "text": message,
                },
            )
        except Exception as exc:
            logger.warning(
                "Feed-down alert failed to send: %s",
                exc,
            )

    logger.error(message)


def collect_rss():
    added = 0

    for feed_def in RSS_FEEDS:
        added += fetch_rss_feed(
            feed_def
        )

    # Critical: queue is saved together with feed validators.
    # A later 304 cannot erase unposted queued stories.
    save_state(
        STATE
    )

    logger.info(
        "RSS queue additions: %d",
        added,
    )

    return added


# ============================================================
# EXA GAP-FILL DISCOVERY
# ============================================================

# ============================================================
# SOURCE UNIVERSE
# ============================================================
PRIMARY_TECH_DOMAINS = [
    "techcrunch.com", "theverge.com", "wired.com", "arstechnica.com",
    "engadget.com", "technologyreview.com", "news.ycombinator.com", "venturebeat.com",
    "techmeme.com", "techradar.com", "zdnet.com", "9to5google.com",
    "wabetainfo.com", "testingcatalog.com", "artificialintelligence-news.com", "unite.ai",
    "the-decoder.com", "siliconangle.com", "androidauthority.com", "macrumors.com",
]
FALLBACK_TECH_DOMAINS = []
ALL_PRIMARY_DOMAINS = PRIMARY_TECH_DOMAINS
ALL_FALLBACK_DOMAINS = FALLBACK_TECH_DOMAINS
ALL_ALLOWED_DOMAINS = ALL_PRIMARY_DOMAINS

def normalized_domain(url_or_source):
    raw = safe_text(url_or_source).lower()
    if "://" in raw:
        raw = urlparse(raw).netloc
    return raw.split(":")[0].removeprefix("www.").strip().rstrip("/")

def is_domain_allowed(url, domains):
    domain = normalized_domain(url)
    return any(domain == d or domain.endswith("." + d) for d in domains)

def primary_domain_allowed(url, region=None):
    return is_domain_allowed(url, ALL_PRIMARY_DOMAINS)

def fallback_domain_allowed(url, region=None):
    return is_domain_allowed(url, ALL_FALLBACK_DOMAINS)

def allowed_source_for_region(url, region=None):
    return primary_domain_allowed(url, region) or fallback_domain_allowed(url, region)

# ============================================================
# GOOGLE NEWS RSS: FREE GAP FILL
# ============================================================

GOOGLE_NEWS_QUERIES = {
    "Tech": [
        "major consumer technology news AI smartphones operating systems",
        "major AI model product launch technology",
        "major cybersecurity privacy breach technology outage",
        "Apple Google Microsoft OpenAI Meta Amazon major news",
        "major technology industry acquisition layoffs startup unicorn",
        "GitHub trending new tool capability technology",
        "Hugging Face major open model leaderboard technology",
        "Y Combinator major product launch milestone",
    ],
}

GOOGLE_NEWS_LOCALE = {"Tech": ("en-US", "US", "US:en")}


def resolve_google_news_url(link):
    """Google News RSS gives a redirect link, not the publisher URL.
    Follow it once (without downloading the full page) to get the
    real article URL. Return "" if it cannot be resolved safely."""
    try:
        response = session.get(
            link,
            timeout=10,
            allow_redirects=True,
            headers=HEADERS,
            stream=True,
        )
        real_url = safe_text(response.url)
        response.close()

        if not real_url or "news.google.com" in real_url:
            return ""

        return real_url

    except Exception:
        return ""


def google_news_gap_fill(
    region,
    existing_count,
    needed,
):
    # Same thin-coverage trigger as Exa, tried first because it is free.
    if existing_count >= max(
        6,
        needed * 3,
    ):
        return 0

    queries = GOOGLE_NEWS_QUERIES.get("Tech", [])
    hl, gl, ceid = GOOGLE_NEWS_LOCALE.get("Tech", ("en-US", "US", "US:en"))

    added = 0

    for query in queries:
        try:
            feed_url = (
                "https://news.google.com/rss/search?q="
                + quote(f"{query} when:2d")
                + f"&hl={hl}&gl={gl}&ceid={ceid}"
            )

            response = session.get(
                feed_url,
                timeout=15,
                headers=HEADERS,
            )

            if response.status_code >= 400:
                continue

            parsed = feedparser.parse(
                response.content
            )

            for entry in parsed.entries[:6]:
                title = safe_text(
                    entry.get("title")
                )
                link = safe_text(
                    entry.get("link")
                )

                if not title or not link:
                    continue

                real_url = resolve_google_news_url(
                    link
                )

                if not real_url:
                    continue

                published_dt = feed_entry_datetime(
                    entry
                )
                date_estimated = False

                if not published_dt:
                    published_dt = datetime.now(
                        BD_TZ
                    )
                    date_estimated = True

                item = {
                    "title": title,
                    "url": real_url,
                    "canonical": canonical_url(
                        real_url
                    ),
                    "published_dt": published_dt.isoformat(),
                    "published_date": published_dt.isoformat(),
                    "source": source_name(
                        real_url
                    ),
                    "region": region,
                    "excerpt": BeautifulSoup(
                        safe_text(
                            entry.get("summary")
                        ),
                        "html.parser",
                    ).get_text(
                        " ",
                        strip=True,
                    )[:2000],
                    "image": "",
                    "discovery": "google_news",
                    "date_estimated": date_estimated,
                }

                if not primary_domain_allowed(real_url, region):
                    continue

                if not candidate_basic_allowed(
                    {
                        **item,
                        "published_dt": published_dt,
                    }
                ):
                    continue

                if item["canonical"] in POSTED_URLS:
                    continue

                if item["canonical"] in STATE["queue"]:
                    continue

                queue_candidate(
                    item
                )
                added += 1

                if added >= MAX_GOOGLE_NEWS_CANDIDATES:
                    return added

        except Exception as exc:
            logger.warning(
                "Google News gap fill failed %s: %s",
                region,
                exc,
            )

    return added


def exa_gap_fill(region, existing_count, needed, fallback=False):
    if existing_count >= max(12, needed * 3):
        return 0

    domains = FALLBACK_TECH_DOMAINS if fallback else PRIMARY_TECH_DOMAINS
    if not domains:
        return 0
    queries = [
        "latest major consumer technology AI smartphone operating system news",
        "latest major AI model product launch technology",
        "latest major cybersecurity privacy breach outage technology",
        "latest Apple Google Microsoft OpenAI Meta Amazon technology news",
        "latest technology industry acquisition layoffs startup unicorn news",
        "latest trending GitHub new tool capability",
        "latest Hugging Face open model release leaderboard",
        "latest Y Combinator major product launch milestone",
    ]

    added = 0
    for query in queries:
        try:
            results = get_exa().search_and_contents(
                query, type="auto", category="news", num_results=8,
                include_domains=domains,
                start_published_date=DISCOVERY_START.isoformat(),
                end_published_date=DISCOVERY_END.isoformat(),
                contents={"highlights": {"max_characters": 900}},
            )
            for result in results.results:
                url = safe_text(getattr(result, "url", ""))
                title = safe_text(getattr(result, "title", ""))
                published_dt = parse_datetime(getattr(result, "published_date", ""))
                if not url or not title or not published_dt:
                    continue
                if fallback:
                    if not fallback_domain_allowed(url, region):
                        continue
                elif not primary_domain_allowed(url, region):
                    continue
                item = {
                    "title": title, "url": url, "canonical": canonical_url(url),
                    "published_dt": published_dt.isoformat(), "published_date": published_dt.isoformat(),
                    "source": source_name(url), "region": "Tech",
                    "excerpt": safe_text(" ".join(getattr(result, "highlights", []) if isinstance(getattr(result, "highlights", []), list) else str(getattr(result, "highlights", ""))))[:2000],
                    "image": safe_text(getattr(result, "image", "")),
                    "discovery": "exa_fallback" if fallback else "exa",
                    "source_pool": "fallback" if fallback else "primary",
                }
                if not candidate_basic_allowed({**item, "published_dt": published_dt}):
                    continue
                if item["canonical"] in POSTED_URLS or item["canonical"] in STATE["queue"]:
                    continue
                queue_candidate(item)
                added += 1
                if added >= MAX_EXA_CANDIDATES:
                    return added
        except Exception as exc:
            logger.warning("Exa %s discovery failed: %s", "fallback" if fallback else "primary", exc)
    return added


def queue_candidates_for_region(
    region,
):
    count = 0

    for item in STATE[
        "queue"
    ].values():
        if (
            item.get("region")
            == region
            and item.get("status")
            == "pending"
        ):
            published = parse_datetime(
                item.get(
                    "published_date"
                )
            )

            if (
                published
                and DISCOVERY_START
                <= published
                <= DISCOVERY_END
            ):
                count += 1

    return count


# ============================================================
# CANDIDATE NORMALIZATION
# ============================================================

# ============================================================
# VERSION 1 EDITORIAL RANKING
# ============================================================

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "rank": {"type": "integer", "minimum": 1},
                    "score": {"type": "integer", "minimum": 0, "maximum": 10},
                    "important": {"type": "boolean"},
                    "topic": {"type": "string"},
                    "institution": {"type": "string"},
                    "event_key": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "rank", "score", "important", "topic", "institution", "event_key", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ranked"],
    "additionalProperties": False,
}


def enrich_thin_excerpt(item):
    """Best-effort article enrichment. Failure never removes a candidate."""
    try:
        downloaded = trafilatura.fetch_url(item["url"])
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded)
        return safe_text(text)[:1200] if text else None
    except Exception:
        return None


def enrich_thin_excerpts(regional):
    enriched = 0
    for item in regional:
        if enriched >= MAX_EXCERPT_ENRICH:
            break
        excerpt = safe_text(item.get("excerpt", ""))
        if len(excerpt) >= THIN_EXCERPT_CHARS:
            continue
        fuller = enrich_thin_excerpt(item)
        if fuller and len(fuller) > len(excerpt):
            item["excerpt"] = fuller
            enriched += 1
    return regional


def _rank_prompt(region):
    topic_list = ", ".join(TOPICS[region])
    return f"""
You are the editor-in-chief of @TheTechNewsroom.

Rank this batch of tech news candidates by REAL editorial importance in the previous 24 hours.
The channel is for everyday technology users. Do not rank by headline excitement alone.
Do not invent facts. Return EVERY candidate in this batch.

IMPORTANT: There is NO requirement to publish a story from every sector. Diversity is a
selection preference only after importance is established. Never lower a score just because
another story covers the same sector, and never raise a weak story to fill a sector.

Priority areas, when genuinely important:
1. Major AI model releases, frontier capability changes, and AI products with broad impact.
2. Major AI acquisitions, strategic deals, or partnerships that materially affect the industry.
3. Major cybersecurity incidents, privacy incidents, critical vulnerabilities, and outages.
4. Major smartphone, operating-system, browser, search, social, cloud, and app-store changes.
5. Major moves by Apple, Google, Microsoft, OpenAI, Meta, Amazon, NVIDIA and other major tech companies.
6. Important new consumer technology products.
7. Trending GitHub repositories only when the repository is a genuinely useful new tool/capability,
   not ordinary developer churn.
8. Startups reaching unicorn status or shipping something with broad real-world impact.
9. Y Combinator companies only for major product launches or milestones, not routine funding.
10. Hugging Face open-model releases or leaderboard changes that materially move the state of the art.

Normally score low or reject:
- reviews, hands-ons, unboxings, rumors, leaks, speculation
- EV/car/robotaxi/vehicle news
- healthtech, biotech, medtech, medical or pharmaceutical news
- routine startup funding, VC, finance, legal or policy commentary
- energy, batteries, utilities and climate/energy policy
- low-level engineering stories aimed at engineers
- podcasts, webinars, event recordings, opinion/promotional pieces
- minor app features, routine patches and insignificant updates

Scoring:
9-10 exceptional, broad user or industry impact
7-8 clearly important and publishable
4-6 interesting but normally not publishable
0-3 low-value, repetitive, promotional, speculative, niche or excluded

A score of 7+ is required for publication. Return EVERY candidate with:
id, rank, score, important, topic, institution, event_key, reason.
The important field MUST be true only when score >= 7.

Allowed topics:
{topic_list}
"""


def _rank_batch(batch, region, batch_no):
    lines = []
    for idx, item in enumerate(batch, start=1):
        published = item.get("published_date", "")
        age_note = ""
        dt = parse_datetime(published)
        if dt:
            age_hours = max(0.0, (NOW_BD - dt).total_seconds() / 3600)
            age_note = f"Age: {age_hours:.1f} hours"
        lines.append("\n".join([
            f"ID: {idx}",
            f"Title: {item.get('title','')}",
            f"Source: {item.get('source','')}",
            f"Published: {published}",
            age_note,
            f"Description/Excerpt: {trim_source_text(item.get('excerpt',''), 650)}",
            "",
        ]))

    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": _rank_prompt(region)},
                {"role": "user", "content": "\n".join(lines)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": f"tech_news_rank_batch_{batch_no}",
                    "strict": True,
                    "schema": RANK_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=3500,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return data.get("ranked", [])
    except Exception as exc:
        logger.error("Ranking batch %d failed: %s", batch_no, exc)
        return []


def rank_candidates(candidates, region):
    """Rank the discovery pool in bounded LLM batches, then merge globally.

    Batching prevents a large structured response from being truncated. The merged result
    is globally ordered by editorial score, then model rank, then freshness.
    """
    if not candidates:
        return []

    regional = sorted(
        candidates,
        key=lambda x: parse_datetime(x.get("published_date")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:80]

    batch_size = 15
    ranked_rows = []
    for offset in range(0, len(regional), batch_size):
        batch = regional[offset:offset + batch_size]
        logger.info("%s RANK BATCH %d: %d candidates", region, offset // batch_size + 1, len(batch))
        rows = _rank_batch(batch, region, offset // batch_size + 1)
        by_id = {idx: item for idx, item in enumerate(batch, start=1)}
        for row in rows:
            try:
                idx = int(row["id"])
            except Exception:
                continue
            if idx not in by_id:
                continue
            item = dict(by_id[idx])
            score = max(0, min(10, int(row.get("score", 0))))
            item.update({
                "importance_score": score,
                "important": bool(row.get("important")) and score >= 7,
                "topic": canonical_topic(safe_text(row.get("topic")), region),
                "institution": safe_text(row.get("institution")),
                "event_key": safe_text(row.get("event_key")),
                "rank_reason": safe_text(row.get("reason")),
                "batch_rank": int(row.get("rank", 9999)),
            })
            ranked_rows.append(item)

    # A failed/partial batch is recoverable, but never receives an invented importance score.
    # It remains available only for diagnostics, not eligibility.
    ranked_rows.sort(key=lambda x: (
        -x.get("importance_score", 0),
        x.get("batch_rank", 9999),
        -(parse_datetime(x.get("published_date")).timestamp() if parse_datetime(x.get("published_date")) else 0),
    ))

    for rank, item in enumerate(ranked_rows, start=1):
        item["editor_rank"] = rank

    logger.info("%s RANK MODEL ROWS: %d/%d", region, len(ranked_rows), len(regional))
    return ranked_rows


# ============================================================
# VERSION 1 EVENT DEDUPLICATION
# ============================================================

def extract_entities(text):
    words = re.findall(r"[A-Za-z][A-Za-z&'-]{1,}", safe_text(text).lower())
    return {w for w in words if w not in STOPWORDS}


def entity_overlap(a, b):
    ea = extract_entities(f"{a.get('title','')} {a.get('excerpt','')}")
    eb = extract_entities(f"{b.get('title','')} {b.get('excerpt','')}")
    if not ea or not eb:
        return 0.0
    return len(ea & eb) / max(1, min(len(ea), len(eb)))


def event_similarity_v04(a, b):
    title_score = title_similarity(a.get("title", ""), b.get("title", ""))
    entity_score = entity_overlap(a, b)
    return (0.75 * title_score) + (0.25 * entity_score)


def same_event_window(a, b, hours=30):
    da = parse_datetime(a.get("published_date"))
    db = parse_datetime(b.get("published_date"))
    if not da or not db:
        return False
    return abs((da - db).total_seconds()) <= hours * 3600


def cluster_ranked_events(ranked):
    """Conservative event clustering. Uncertain items are always kept."""
    clusters = []
    ordered = sorted(
        ranked,
        key=lambda x: x.get("editor_rank", 9999),
    )
    for item in ordered:
        placed = False
        for cluster in clusters:
            representative = cluster[0]
            item_event_key = safe_text(item.get("event_key"))
            rep_event_key = safe_text(representative.get("event_key"))
            same_key = bool(
                item_event_key
                and rep_event_key
                and item_event_key == rep_event_key
                and (
                    entity_overlap(item, representative) >= 0.25
                    or title_similarity(item.get("title", ""), representative.get("title", "")) >= 0.55
                )
            )
            if same_key or (
                same_event_window(item, representative)
                and event_similarity_v04(item, representative) >= 0.88
            ):
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    output = []
    for index, cluster in enumerate(clusters, start=1):
        representative = cluster[0]
        stable_key = normalize_title(representative.get("title", "")) or representative.get("canonical", "")
        digest = hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:10]
        cluster_id = f"evt_{digest}"
        sources = sorted({safe_text(x.get("source")) for x in cluster if safe_text(x.get("source"))})
        for member in cluster:
            row = dict(member)
            row.update({
                "event_cluster_id": cluster_id,
                "event_cluster_size": len(cluster),
                "event_sources": sources,
                "event_source_count": len(sources),
                "event_confidence": 1.0 if len(cluster) > 1 else 0.6,
            })
            output.append(row)
    return output


def collapse_event_clusters(ranked):
    clustered = cluster_ranked_events(ranked)
    winners = {}
    for item in clustered:
        key = item.get("event_cluster_id") or item.get("canonical")
        old = winners.get(key)
        if old is None:
            winners[key] = item
            continue
        # Preserve the highest editorial rank, then newest story.
        item_key = (
            item.get("editor_rank", 9999),
            -(parse_datetime(item.get("published_date")).timestamp() if parse_datetime(item.get("published_date")) else 0),
        )
        old_key = (
            old.get("editor_rank", 9999),
            -(parse_datetime(old.get("published_date")).timestamp() if parse_datetime(old.get("published_date")) else 0),
        )
        if item_key < old_key:
            winners[key] = item
    return sorted(winners.values(), key=lambda x: x.get("editor_rank", 9999))


def persist_event_cluster_state(ranked):
    clusters = STATE.setdefault("event_clusters", {})
    for item in ranked:
        event_id = item.get("event_cluster_id")
        if not event_id:
            continue
        clusters[event_id] = {
            "event_id": event_id,
            "topic": item.get("topic", ""),
            "region": item.get("region", ""),
            "sources": item.get("event_sources", []),
            "source_count": item.get("event_source_count", 0),
            "confidence": item.get("event_confidence", 0),
            "last_seen": now_iso(),
            "headline": item.get("title", ""),
        }


def remember_posted_event(story):
    event_id = story.get("event_cluster_id") or make_event_id(story)
    ids = STATE.setdefault("posted_event_ids", [])
    if event_id and event_id not in ids:
        ids.append(event_id)
    STATE["posted_event_ids"] = ids[-500:]
    return event_id


# ============================================================
# ARTICLE EXTRACTION
# ============================================================

def find_og_image(
    url,
    page_html=None,
    final_url=None,
):
    try:
        base_url = (
            final_url
            or url
        )

        if page_html is None:
            response = session.get(
                url,
                headers={
                    **HEADERS,
                    "Referer": url,
                },
                timeout=20,
            )

            if response.status_code >= 400:
                return ""

            page_html = response.text
            base_url = response.url

        soup = BeautifulSoup(
            page_html,
            "html.parser",
        )

        for attrs in (
            {"property": "og:image"},
            {"property": "og:image:url"},
            {"name": "twitter:image"},
        ):
            tag = soup.find(
                "meta",
                attrs=attrs,
            )

            if tag and tag.get(
                "content"
            ):
                return urljoin(
                    base_url,
                    safe_text(
                        tag["content"]
                    ),
                )

    except Exception:
        pass

    return ""


def extract_article(
    item,
):
    url = item["url"]

    try:
        response = session.get(
            url,
            headers={
                **HEADERS,
                "Referer": url,
            },
            timeout=25,
        )

        if response.status_code < 400:
            page_html = response.text

            text = trafilatura.extract(
                page_html,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )

            image_url = (
                item.get("image")
                or find_og_image(
                    url,
                    page_html,
                    response.url,
                )
            )

            if text and len(safe_text(text)) >= 500:
                return (
                    safe_text(text),
                    image_url,
                )

    except Exception as exc:
        logger.warning(
            "Local extraction failed %s: %s",
            url,
            exc,
        )

    try:
        result_set = get_exa().get_contents(
            [url],
            text={
                "max_characters": 12000,
            },
        )

        if result_set.results:
            result = result_set.results[0]

            text = safe_text(
                getattr(
                    result,
                    "text",
                    "",
                )
            )

            image_url = (
                item.get("image")
                or safe_text(
                    getattr(
                        result,
                        "image",
                        "",
                    )
                )
            )

            if text:
                return (
                    text,
                    image_url,
                )

    except Exception as exc:
        logger.warning(
            "Exa article fallback failed %s: %s",
            url,
            exc,
        )

    return (
        "",
        item.get("image", ""),
    )


# ============================================================
# STORY + KNOWLEDGE GENERATION
# ============================================================

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "highlights": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 5,
        },
        "why_it_matters": {"type": "string"},
        "whats_next": {"type": "string"},
        "bold_terms": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
        },
    },
    "required": [
        "headline",
        "summary",
        "highlights",
        "why_it_matters",
        "whats_next",
        "bold_terms",
    ],
    "additionalProperties": False,
}


def first_sentence(text):
    text = clean_generated_text(
        text
    )

    # Conservative sentence extraction. Avoids common tech
    # abbreviations and decimals splitting incorrectly.
    protected = {
        "U.S.": "US_SENTINEL",
        "U.K.": "UK_SENTINEL",
        "E.U.": "EU_SENTINEL",
        "No.": "NO_SENTINEL",
        "Inc.": "INC_SENTINEL",
        "Ltd.": "LTD_SENTINEL",
        "Dr.": "DR_SENTINEL",
        "Mr.": "MR_SENTINEL",
        "Mrs.": "MRS_SENTINEL",
        "Ms.": "MS_SENTINEL",
    }

    working = text

    for old, marker in protected.items():
        working = working.replace(
            old,
            marker,
        )

    match = re.search(
        r"(.+?[.!?])(?:\s|$)",
        working,
    )

    if match:
        sentence = match.group(1)
    else:
        sentence = working

    for old, marker in protected.items():
        sentence = sentence.replace(
            marker,
            old,
        )

    return clean_generated_text(
        sentence
    )


def generate_story(
    item,
    article_text,
):
    topic_hint = item.get(
        "topic",
        "",
    )

    prompt = f"""
You are a senior newspaper tech editor and knowledge editor for @TheTechNewsroom.

Create a compact Telegram tech news card from the source article.

Primary topic:
{topic_hint}

Return ONLY valid JSON matching the schema.

PUBLIC CONTENT:
- Headline: 6-14 words, accurate, newspaper style.
- Summary: exactly ONE complete sentence, about 18-28 words.
- Highlights: 3-5 short factual points, choosing the number that best fits the story.
- Why it Matters: 2-4 complete sentences explaining the real-world user, platform, business,
  privacy, security, or industry significance.
- What's Next: 1-2 complete sentences stating what readers should watch for next.
- No repetition between sections.
- No "..." or "…".
- Never end a headline or highlight with an ellipsis.
- No hashtags in generated fields.
- No Markdown or HTML in JSON fields.

BOLD TERMS:
- Include important company names, products, AI models, platforms, figures, prices, dates,
  user counts, policies, vulnerabilities, and technical terms appearing in the generated
  headline, summary or highlights.

The public post must follow this exact order:
Photo
Headline
1-sentence summary
## KEY HIGHLIGHTS
3-5 bullets
## WHY IT MATTERS
2-4 sentences
## WHAT'S NEXT
1-2 sentences
#hashtags
**Source:** Publication
"""

    user = (
        f"REGION: {item['region']}\n"
        f"SOURCE: {item['source']}\n"
        f"TITLE: {item['title']}\n"
        f"DATE: {item['published_date']}\n\n"
        f"ARTICLE:\n{article_text[:12000]}"
    )

    for attempt in range(3):
        try:
            response = get_cerebras().chat.completions.create(
                model=CEREBRAS_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": prompt,
                    },
                    {
                        "role": "user",
                        "content": user,
                    },
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "tech_news_story_v1_0",
                        "strict": True,
                        "schema": STORY_SCHEMA,
                    },
                },
                reasoning_effort="low",
                temperature=0.2,
                max_completion_tokens=1400,
            )

            data = json.loads(
                safe_text(
                    response.choices[0]
                    .message
                    .content
                )
            )

            headline = clean_generated_text(
                data.get(
                    "headline"
                )
            )

            summary = first_sentence(
                data.get(
                    "summary"
                )
            )

            highlights = [
                clean_generated_text(x)
                for x in data.get("highlights", [])
                if clean_generated_text(x)
            ]
            if not (3 <= len(highlights) <= 5):
                raise ValueError("Highlights must contain 3-5 points")

            why_it_matters = clean_generated_text(data.get("why_it_matters"))
            whats_next = clean_generated_text(data.get("whats_next"))
            why_count = len(re.findall(r"(?<=[.!?])\s+", why_it_matters)) + (1 if why_it_matters and why_it_matters[-1] in ".!?" else 0)
            next_count = len(re.findall(r"(?<=[.!?])\s+", whats_next)) + (1 if whats_next and whats_next[-1] in ".!?" else 0)
            if not why_it_matters or not whats_next or not (2 <= why_count <= 4) or not (1 <= next_count <= 2):
                raise ValueError("Invalid Why It Matters or What's Next")

            if (
                not headline
                or not summary
                or not complete_text(headline)
                or not complete_text(summary)
                or any(not complete_text(x) for x in highlights)
                or not complete_text(why_it_matters)
                or not complete_text(whats_next)
            ):
                raise ValueError("Incomplete story")

            story = {
                **item,
                "headline": trim_source_text(headline, 110),
                "summary": trim_source_text(summary, 260),
                "highlights": [trim_source_text(x, 130) for x in highlights],
                "why_it_matters": trim_source_text(why_it_matters, 520),
                "whats_next": trim_source_text(whats_next, 260),
                "bold_terms": [safe_text(x) for x in data.get("bold_terms", []) if safe_text(x)],
            }

            return story

        except Exception as exc:
            logger.warning(
                "Story generation attempt %d failed: %s",
                attempt + 1,
                exc,
            )

            if attempt == 0:
                time.sleep(1)

    return None


# ============================================================
# NUMERIC GROUNDING
# ============================================================

NUMBER_RE = re.compile(
    r"""
    (?:
        (?:US|U\.S\.|HK|HK\$|Tk|BDT|USD|EUR|GBP|JPY|CNY|INR|৳|\$|€|£|¥)
        \s*
    )?
    \d[\d,]*(?:\.\d+)?
    \s*
    (?:
        million|billion|trillion|
        crore|lakh|bn|mn|b|m|k|%
    )?
    """,
    re.I | re.X,
)

YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

_NUMERIC_SCALES = {
    "": 1.0,
    "k": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "trillion": 1e12,
    "t": 1e12,
    "crore": 1e7,
    "lakh": 1e5,
}
_NUMERIC_CURRENCIES = {
    "$": "usd",
    "usd": "usd",
    "tk": "bdt",
    "bdt": "bdt",
    "৳": "bdt",
    "€": "eur",
    "eur": "eur",
    "£": "gbp",
    "gbp": "gbp",
    "¥": "jpy",
    "jpy": "jpy",
    "cny": "cny",
    "inr": "inr",
    "₹": "inr",
    "hk$": "hkd",
    "hk": "hkd",
}

def _numeric_signature(raw):
    text = safe_text(raw).strip().lower()
    if not text:
        return None

    currency = None
    for symbol in sorted(_NUMERIC_CURRENCIES, key=len, reverse=True):
        if text.startswith(symbol):
            currency = _NUMERIC_CURRENCIES[symbol]
            text = text[len(symbol):].strip()
            break

    if text.endswith("%"):
        unit = "%"
        text = text[:-1].strip()
    else:
        m = re.search(r"(trillion|billion|million|crore|lakh|bn|mn|[kmbt])$", text)
        unit = m.group(1) if m else ""
        if m:
            text = text[:m.start()].strip()

    text = text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None

    if unit == "%":
        scale = 1.0
        percent = True
    else:
        scale = _NUMERIC_SCALES.get(unit, 1.0)
        percent = False

    return {
        "value": value * scale,
        "percent": percent,
        "currency": currency,
    }

def normalize_number(raw):
    sig = _numeric_signature(raw)
    if not sig:
        return ""
    value = sig["value"]
    value_key = f"{value:.12g}"
    currency = sig["currency"] or ""
    percent = "%" if sig["percent"] else ""
    return f"{currency}|{percent}|{value_key}"

def numeric_tokens(text):
    tokens = []
    for match in NUMBER_RE.finditer(safe_text(text)):
        token = safe_text(match.group(0)).strip()
        sig = _numeric_signature(token)
        if not sig:
            continue

        # Ignore bare years, but retain years with a financial/percent/unit marker.
        numeric_value = sig["value"]
        if (
            numeric_value.is_integer()
            and YEAR_RE.match(str(int(numeric_value)))
            and not sig["percent"]
            and not sig["currency"]
        ):
            continue

        if token:
            tokens.append(token)
    return tokens

def _numeric_equivalent(source_sig, generated_sig):
    if not source_sig or not generated_sig:
        return False
    if source_sig["percent"] != generated_sig["percent"]:
        return False

    # If both explicitly name currencies, they must agree.
    # If only one names a currency, accept the numeric equivalent because
    # source extraction often drops currency symbols around abbreviated forms.
    if (
        source_sig["currency"]
        and generated_sig["currency"]
        and source_sig["currency"] != generated_sig["currency"]
    ):
        return False

    return abs(source_sig["value"] - generated_sig["value"]) <= max(
        1e-9, abs(source_sig["value"]) * 1e-9
    )

def numeric_grounded(story, article_text):
    source_sigs = [
        _numeric_signature(x)
        for x in numeric_tokens(article_text)
    ]
    source_sigs = [x for x in source_sigs if x]

    generated_text = " ".join(
        [
            story.get("headline", ""),
            story.get("summary", ""),
            *story.get("highlights", []),
        ]
    )

    for token in numeric_tokens(generated_text):
        generated_sig = _numeric_signature(token)
        if not generated_sig:
            continue

        if not any(
            _numeric_equivalent(source_sig, generated_sig)
            for source_sig in source_sigs
        ):
            return False, token

    return True, ""


# ============================================================
# BOLD TERMS
# ============================================================

def derive_bold_terms(
    story,
):
    terms = [
        safe_text(x)
        for x in story.get(
            "bold_terms",
            [],
        )
        if safe_text(x)
    ]

    combined = " ".join(
        [
            story.get(
                "summary",
                "",
            ),
            *story.get(
                "highlights",
                [],
            ),
        ]
    )

    # Financial figures, but do not bold bare years.
    for match in NUMBER_RE.finditer(
        combined
    ):
        token = safe_text(
            match.group(0)
        )

        numeric_only = re.sub(
            r"[^\d.]",
            "",
            token,
        )

        if (
            YEAR_RE.match(
                numeric_only
            )
            and not re.search(
                r"(Tk|BDT|USD|EUR|GBP|JPY|CNY|INR|৳|\$|€|£|¥|%|million|billion|crore|lakh)",
                token,
                re.I,
            )
        ):
            continue

        if token:
            terms.append(
                token
            )

    unique = []
    seen = set()

    for term in sorted(
        terms,
        key=len,
        reverse=True,
    ):
        key = term.lower()

        if (
            len(term) >= 2
            and key not in seen
        ):
            seen.add(key)
            unique.append(term)

    return unique[:16]


def escape_rich_html(
    text,
):
    return html.escape(
        clean_generated_text(text),
        quote=False,
    )


def bold_terms_html(
    text,
    terms,
):
    text = clean_generated_text(
        text
    )

    if not text:
        return ""

    result = text

    # Use letter-only markers to avoid collisions with numeric terms.
    replacements = []

    for index, term in enumerate(
        sorted(
            {
                safe_text(x)
                for x in terms
                if safe_text(x)
            },
            key=len,
            reverse=True,
        )
    ):
        marker = (
            f"__RICHBOLD_{chr(65 + (index % 26))}"
            f"{index // 26}__"
        )

        pattern = re.compile(
            re.escape(term),
            re.I,
        )

        match = pattern.search(
            result
        )

        if match:
            original = match.group(
                0
            )
            result = (
                result[:match.start()]
                + marker
                + result[match.end():]
            )
            replacements.append(
                (
                    marker,
                    original,
                )
            )

    escaped = html.escape(
        result,
        quote=False,
    )

    for marker, original in replacements:
        escaped = escaped.replace(
            marker,
            "<b>"
            + html.escape(
                original,
                quote=False,
            )
            + "</b>",
        )

    return escaped


# ============================================================
# DYNAMIC RICH MESSAGE HTML
# ============================================================

def dynamic_rich_html(story):
    terms = derive_bold_terms(story)
    parts = [
        '<img src="tg://photo?id=newsphoto">',
        "<h1>" + escape_rich_html(story["headline"]) + "</h1>",
        "<p>" + bold_terms_html(story["summary"], terms) + "</p>",
        "<h2>KEY HIGHLIGHTS</h2>",
        "<p>" + "<br>".join(
            "• " + bold_terms_html(point, terms)
            for point in story.get("highlights", [])
        ) + "</p>",
        "<h2>WHY IT MATTERS</h2>",
        "<p>" + bold_terms_html(story.get("why_it_matters", ""), terms) + "</p>",
        "<blockquote expandable><b>WHAT'S NEXT</b><br>"
        + bold_terms_html(story.get("whats_next", ""), terms)
        + "</blockquote>",
    ]

    hashtags = " ".join(category_hashtags(story))
    if hashtags:
        parts.append("<p>" + escape_rich_html(hashtags) + "</p>")

    source = escape_rich_html(story["source"])
    url = html.escape(story["url"], quote=True)
    parts.append(
        "<footer><b>Source:</b> "
        f'<a href="{url}">{source}</a>'
        "</footer>"
    )

    return "\n".join(parts)


def rich_visible_length(text):
    """Return Telegram-visible character count for Rich HTML text.

    Telegram limits the rendered text, not the raw HTML markup, so strip
    tags and decode HTML entities before counting characters.
    """
    no_tags = re.sub(r"<[^>]+>", "", text)
    no_attrs = re.sub(
        r"\[[^\]]+\]\([^)]+\)",
        lambda m: m.group(0).split("]")[0][1:],
        no_tags,
    )
    return len(html.unescape(no_attrs))


def fit_rich_html(story):
    variants = [
        (260, 130, 520, 260),
        (220, 115, 440, 220),
        (190, 100, 380, 200),
        (160, 85, 320, 170),
    ]

    for summary_len, highlight_len, why_len, next_len in variants:
        candidate = dict(story)
        candidate["summary"] = trim_source_text(story["summary"], summary_len)
        candidate["highlights"] = [trim_source_text(x, highlight_len) for x in story.get("highlights", [])]
        candidate["why_it_matters"] = trim_source_text(story.get("why_it_matters", ""), why_len)
        candidate["whats_next"] = trim_source_text(story.get("whats_next", ""), next_len)
        html_text = dynamic_rich_html(candidate)
        if rich_visible_length(html_text) <= MAX_RICH_CHARACTERS:
            return html_text

    return dynamic_rich_html(story)



# ============================================================
# IMAGE BRANDING: ONLY @TheTechNewsroom
# ============================================================

def find_font(
    bold=False,
):
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    )

    for path in candidates:
        if os.path.exists(path):
            return path

    return None


def download_image(
    url,
    referer,
):
    if not url:
        return None

    try:
        response = session.get(
            url,
            headers={
                **HEADERS,
                "Referer": referer,
            },
            timeout=20,
            stream=True,
        )

        if response.status_code >= 400:
            return None

        content_type = (
            response.headers.get(
                "content-type",
                "",
            )
            .lower()
        )

        if (
            content_type
            and not content_type.startswith(
                "image/"
            )
        ):
            return None

        buf = BytesIO()

        for chunk in response.iter_content(
            65536
        ):
            if not chunk:
                continue

            buf.write(
                chunk
            )

            if buf.tell() > 8_000_000:
                return None

        buf.seek(0)

        image = Image.open(
            buf
        )
        image.load()

        if (
            image.width < 400
            or image.height < 250
        ):
            return None

        return image.convert(
            "RGB"
        )

    except Exception as exc:
        logger.warning(
            "Image download failed: %s",
            exc,
        )
        return None


def crop_cover(
    image,
    size=(1200, 675),
):
    target_w, target_h = size

    ratio = max(
        target_w / image.width,
        target_h / image.height,
    )

    resized = image.resize(
        (
            int(
                image.width
                * ratio
            ),
            int(
                image.height
                * ratio
            ),
        ),
        Image.Resampling.LANCZOS,
    )

    left = (
        resized.width
        - target_w
    ) // 2

    top = (
        resized.height
        - target_h
    ) // 2

    return resized.crop(
        (
            left,
            top,
            left + target_w,
            top + target_h,
        )
    )


def image_average_brightness(
    image,
):
    small = image.resize(
        (1, 1)
    ).convert(
        "L"
    )
    return small.getpixel(
        (0, 0)
    )


def branded_card(
    photo,
):
    base = crop_cover(
        photo
    ).convert(
        "RGBA"
    )

    brightness = image_average_brightness(
        base
    )

    # Adaptive personal-brand chip:
    # light chip on dark images, dark chip on light images.
    if brightness < 125:
        bg = (
            245,
            245,
            245,
            225,
        )
        fg = (
            20,
            24,
            28,
            255,
        )
    else:
        bg = (
            18,
            22,
            28,
            205,
        )
        fg = (
            245,
            245,
            245,
            255,
        )

    overlay = Image.new(
        "RGBA",
        base.size,
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(
        overlay
    )

    font_path = find_font(
        bold=True
    )

    if font_path:
        font = ImageFont.truetype(
            font_path,
            24,
        )
    else:
        font = ImageFont.load_default()

    text = "@TheTechNewsroom"

    bbox = draw.textbbox(
        (0, 0),
        text,
        font=font,
    )

    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    padding_x = 22
    padding_y = 10

    chip_w = (
        text_w
        + padding_x * 2
    )
    chip_h = (
        text_h
        + padding_y * 2
    )

    x2 = 1200 - 28
    y2 = 675 - 24
    x1 = x2 - chip_w
    y1 = y2 - chip_h

    draw.rounded_rectangle(
        (
            x1,
            y1,
            x2,
            y2,
        ),
        radius=18,
        fill=bg,
    )

    draw.text(
        (
            x1 + padding_x,
            y1 + padding_y - 1,
        ),
        text,
        font=font,
        fill=fg,
    )

    return Image.alpha_composite(
        base,
        overlay,
    ).convert(
        "RGB"
    )


def prepare_image(
    story,
    index,
):
    image = download_image(
        story.get(
            "image_url",
            "",
        ),
        story["url"],
    )

    if image is None:
        image = Image.new(
            "RGB",
            (1200, 675),
            (28, 38, 50),
        )

        font_path = find_font(
            bold=True
        )

        if font_path:
            font = ImageFont.truetype(
                font_path,
                48,
            )
        else:
            font = ImageFont.load_default()

        draw = ImageDraw.Draw(
            image
        )

        draw.text(
            (50, 50),
            "Tech News",
            font=font,
            fill="white",
        )

    branded = branded_card(
        image
    )

    path = f"/tmp/news_{index}.jpg"

    branded.save(
        path,
        "JPEG",
        quality=88,
        optimize=True,
    )

    return path


# ============================================================
# TELEGRAM RICH MESSAGES
# ============================================================

def telegram_call(
    method,
    data=None,
    files=None,
):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )

    last = {
        "ok": False,
        "description": "Unknown error",
    }

    for attempt in range(
        1,
        6,
    ):
        try:
            response = session.post(
                url,
                data=data or {},
                files=files,
                timeout=90,
            )

            result = response.json()

            if result.get(
                "ok"
            ):
                return result

            last = result

            if response.status_code == 429:
                retry_after = int(
                    result.get(
                        "parameters",
                        {},
                    ).get(
                        "retry_after",
                        5,
                    )
                )

                logger.warning(
                    "Telegram 429; waiting %ss",
                    retry_after,
                )

                time.sleep(
                    max(
                        1,
                        retry_after,
                    )
                )
                continue

            if response.status_code >= 500:
                time.sleep(
                    2 * attempt
                )
                continue

            break

        except Exception as exc:
            last = {
                "ok": False,
                "description": str(exc),
            }

            time.sleep(
                2 * attempt
            )

    return last



def send_bot_api_fallback(image_path, rich_html):
    """Last-resort Bot API photo send with a safe caption length."""
    text = re.sub(r"<br\s*/?>", "\n", rich_html, flags=re.I)
    text = re.sub(r"</(p|h1|h2|h3|footer|summary|details|tr|td)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > 900:
        text = text[:900].rsplit(" ", 1)[0].rstrip() + "..."

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as photo:
            response = session.post(
                url,
                data={"chat_id": TELEGRAM_CHANNEL, "caption": text},
                files={"photo": photo},
                timeout=90,
            )
        return response.json()
    except Exception as exc:
        return {"ok": False, "description": str(exc)}

def send_rich_photo(
    image_path,
    rich_html,
):
    rich_message = {
        "html": rich_html,
        "media": [
            {
                "id": "newsphoto",
                "media": {
                    "type": "photo",
                    "media": "attach://photo",
                },
            }
        ],
        "skip_entity_detection": False,
    }

    with open(
        image_path,
        "rb",
    ) as photo:

        return telegram_call(
            "sendRichMessage",
            data={
                "chat_id": TELEGRAM_CHANNEL,
                "rich_message": json.dumps(
                    rich_message,
                    ensure_ascii=False,
                ),
            },
            files={
                "photo": photo
            },
        )


# ============================================================
# KNOWLEDGE / EVENT RECORD
# ============================================================

def make_event_id(
    story,
):
    event_key = safe_text(
        story.get(
            "event_key"
        )
    )

    if event_key:
        return (
            re.sub(
                r"[^a-z0-9]+",
                "_",
                event_key.lower(),
            ).strip("_")
        )

    return canonical_url(
        story["url"]
    )


def store_event(
    story,
    published=False,
    message_id=None,
):
    event_id = make_event_id(
        story
    )

    event = {
        "event_id": event_id,
        "canonical_url": story[
            "canonical"
        ],
        "original_url": story[
            "url"
        ],
        "source": story[
            "source"
        ],
        "region": story[
            "region"
        ],
        "topic": story.get(
            "topic",
            "",
        ),
        "institution": story.get(
            "institution",
            "",
        ),
        "event_cluster_id": story.get(
            "event_cluster_id",
            event_id,
        ),
        "event_confidence": story.get(
            "event_confidence",
            0,
        ),
        "event_source_count": story.get(
            "event_source_count",
            0,
        ),
        "headline": story[
            "headline"
        ],
        "summary": story[
            "summary"
        ],
        "highlights": story[
            "highlights"
        ],
        "concepts": story.get(
            "concepts",
            [],
        ),
        "key_numbers": story.get(
            "key_numbers",
            [],
        ),
        "published_at": story[
            "published_date"
        ],
        "selected_at": now_iso(),
        "status": (
            "published"
            if published
            else "selected"
        ),
        "message_id": message_id,
    }

    STATE["events"][
        event_id
    ] = event

    return event_id


# ============================================================
# ============================================================



# ============================================================
# VERSION 1 FALLBACK POOLS
# ============================================================

def build_candidate_pool(ranked, needed):
    """Build a verification pool that favors important stories and sector diversity.

    Diversity is a soft preference. A high-scoring story always beats a low-scoring story,
    and no sector is forced when the latest news does not support it.
    """
    if not ranked:
        return []

    eligible = [dict(x) for x in ranked if x.get("importance_score", 0) >= 7 and x.get("important") is True]
    target = max(RANKING_POOL_SIZE, needed * 2)
    target = min(target, len(eligible))

    # Preferred high-signal topics. These are not quotas; they only help break ties.
    preferred = {
        "GitHub Trends": 0,
        "Startups": 0,
        "Acquisitions and Mergers": 0,
        "AI Models and Products": 0,
        "Hugging Face": 0,
        "Cybersecurity": 0,
        "Major Outages": 0,
        "Operating Systems": 0,
        "Smartphones": 0,
        "Major Tech Companies": 0,
    }

    selected = []
    used_topics = set()
    remaining = list(eligible)

    # First pass: preserve the best story from distinct important sectors.
    for item in remaining:
        topic = item.get("topic", "")
        if topic not in used_topics and len(selected) < target:
            selected.append(item)
            used_topics.add(topic)

    # Second pass: fill by global editorial rank. No quota is imposed.
    for item in remaining:
        if len(selected) >= target:
            break
        if item not in selected:
            selected.append(item)

    selected.sort(key=lambda x: (
        -x.get("importance_score", 0),
        x.get("editor_rank", 9999),
    ))
    return selected


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "unsupported_claims": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
    },
    "required": ["supported", "unsupported_claims"],
    "additionalProperties": False,
}


def claims_grounded(story, article_text):
    """Second-pass editorial verification for non-numeric factual claims."""
    claims = [
        story.get("headline", ""),
        story.get("summary", ""),
        *story.get("highlights", []),
    ]
    claims = [safe_text(x) for x in claims if safe_text(x)]

    prompt = """
You are a strict fact-checking editor. Compare the generated claims with the source article.
Mark supported=true only if every material factual claim in the headline, summary and highlights
is directly supported by the source article, either explicitly or by a faithful paraphrase.
Do not reject normal wording changes. Reject invented facts, unsupported causal claims, wrong dates,
wrong institutions, wrong people, wrong figures, exaggerated rankings, or claims stronger than the source.
Return only the JSON schema.
"""

    user = (
        "SOURCE ARTICLE:\n" + article_text[:12000]
        + "\n\nGENERATED CLAIMS:\n- " + "\n- ".join(claims)
    )

    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "story_claim_verification",
                    "strict": True,
                    "schema": VERIFY_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=500,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return bool(data.get("supported")), data.get("unsupported_claims", [])
    except Exception as exc:
        logger.warning("Claim verification failed: %s", exc)
        # Verification infrastructure failure must not silently become a hard drop.
        # Numeric grounding remains mandatory; this pass is advisory on verifier outage.
        return True, []


def process_story_candidate(item):
    """
    Extract, generate, ground and normalize one candidate.
    Returns a publishable story or None.
    """
    article_text, image_url = extract_article(item)

    if not article_text:
        logger.warning(
            "DROP extraction: %s",
            item.get("title"),
        )
        return None

    story = generate_story(
        item,
        article_text,
    )

    if not story:
        logger.warning(
            "DROP generation: %s",
            item.get("title"),
        )
        return None

    region = item.get(
        "region",
        "Tech",
    )

    story["topic"] = canonical_topic(
        story.get("topic") or item.get("topic"),
        region,
    )

    story["image_url"] = (
        image_url
        or item.get("image")
    )

    grounded, bad_number = numeric_grounded(
        story,
        article_text,
    )

    if not grounded:
        logger.warning(
            "Numeric grounding failed: %s (%s)",
            story.get("headline"),
            bad_number,
        )

        retry_story = generate_story(
            {
                **item,
                "grounding_warning": bad_number,
            },
            article_text,
        )

        if not retry_story:
            return None

        retry_story["topic"] = canonical_topic(
            retry_story.get("topic") or item.get("topic"),
            region,
        )
        retry_story["image_url"] = (
            image_url
            or item.get("image")
        )

        grounded_retry, _ = numeric_grounded(
            retry_story,
            article_text,
        )

        if not grounded_retry:
            logger.warning(
                "DROP numeric grounding: %s",
                story.get("headline"),
            )
            return None

        story = retry_story

    verified, unsupported_claims = claims_grounded(story, article_text)
    if not verified:
        logger.warning(
            "Claim verification failed: %s | claims=%s",
            story.get("headline"),
            unsupported_claims,
        )

        retry_story = generate_story(
            {**item, "grounding_warning": ", ".join(unsupported_claims[:3])},
            article_text,
        )
        if not retry_story:
            return None

        retry_story["topic"] = canonical_topic(
            retry_story.get("topic") or item.get("topic"),
            region,
        )
        retry_story["image_url"] = image_url or item.get("image")

        grounded_retry, _ = numeric_grounded(retry_story, article_text)
        if not grounded_retry:
            return None

        verified_retry, _ = claims_grounded(retry_story, article_text)
        if not verified_retry:
            logger.warning("DROP claim grounding: %s", story.get("headline"))
            return None
        story = retry_story

    story["topic"] = canonical_topic(
        story.get("topic") or item.get("topic"),
        region,
    )
    story["institution"] = item.get(
        "institution",
        "",
    )
    story["event_key"] = item.get(
        "event_key",
        "",
    )
    story["event_cluster_id"] = item.get(
        "event_cluster_id",
        "",
    )
    story["event_confidence"] = item.get(
        "event_confidence",
        0,
    )
    story["event_source_count"] = item.get(
        "event_source_count",
        0,
    )
    story["category_hashtags"] = category_hashtags(
        story
    )

    return story


# ============================================================
# MAIN
# ============================================================

def is_already_published_candidate(item):
    canonical = safe_text(item.get("canonical"))
    if canonical and canonical in POSTED_URLS:
        return True

    title = safe_text(item.get("title"))
    if not title:
        return False

    for event in STATE.get("events", {}).values():
        if event.get("status") != "published":
            continue
        if event.get("region") != item.get("region"):
            continue
        published_at = parse_datetime(event.get("published_at"))
        if not published_at or (NOW_BD - published_at).total_seconds() > EVENT_RETENTION_DAYS * 86400:
            continue
        previous_title = safe_text(event.get("headline"))
        if previous_title and title_similarity(title, previous_title) >= 0.90:
            return True
    return False


def available_candidates(region, source_pool=None):
    candidates = []
    seen = set()

    for item in STATE.get("queue", {}).values():
        if item.get("region") != region:
            continue
        if item.get("status") not in {"pending", "selected"}:
            continue

        published = parse_datetime(item.get("published_date"))
        if not published or not (DISCOVERY_START <= published <= DISCOVERY_END):
            continue

        url = safe_text(item.get("url"))
        canonical = safe_text(item.get("canonical"))
        if not canonical or canonical in seen:
            continue

        if source_pool == "primary" and not primary_domain_allowed(url, region):
            continue
        if source_pool == "fallback" and not fallback_domain_allowed(url, region):
            continue
        if source_pool is None and not allowed_source_for_region(url, region):
            continue

        if is_already_published_candidate(item):
            continue
        if title_duplicate_against_list(item.get("title", ""), candidates, threshold=0.94):
            continue

        candidates.append(dict(item))
        seen.add(canonical)

    candidates.sort(
        key=lambda x: parse_datetime(x.get("published_date"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return candidates[:MAX_RSS_CANDIDATES]


def prepare_ranked_region(region, candidates):
    ranked = rank_candidates(candidates, region)
    logger.info("%s RANKED RETURNED: %d", region, len(ranked))

    clustered = collapse_event_clusters(ranked)
    logger.info("%s AFTER EVENT DEDUP: %d", region, len(clustered))

    eligible = [
        item for item in clustered
        if item.get("importance_score", 0) >= 7
        and item.get("important") is True
    ]
    logger.info("%s IMPORTANCE PASS (score>=7): %d", region, len(eligible))

    persist_event_cluster_state(eligible)
    return eligible


def process_ranked_region(region, ranked):
    pool = build_candidate_pool(ranked, MAX_STORIES_PER_RUN)
    valid = []
    attempted = 0
    rejected = 0

    for item in pool:
        if len(valid) >= MAX_STORIES_PER_RUN:
            break
        attempted += 1
        story = process_story_candidate(item)
        if not story:
            rejected += 1
            continue

        # Final duplicate check after generation.
        if is_already_published_candidate({**item, "title": story.get("headline", item.get("title"))}):
            logger.info("DROP already published event: %s", story.get("headline", ""))
            rejected += 1
            continue

        story["topic"] = canonical_topic(story.get("topic"), region)
        story["category_hashtags"] = category_hashtags(story)
        valid.append(story)
        logger.info(
            "ACCEPT %s #%d: rank=%s title=%s",
            region,
            len(valid),
            item.get("editor_rank", "?"),
            story.get("headline", ""),
        )

    logger.info(
        "%s FINAL VALID: %d/%d | pool=%d attempted=%d rejected=%d",
        region,
        len(valid),
        MAX_STORIES_PER_RUN,
        len(pool),
        attempted,
        rejected,
    )
    return valid


def run():
    logger.info("THE TECH NEWSROOM V1 UPDATE-ONLY")
    logger.info("Channel=%s Mode=%s", TELEGRAM_CHANNEL, NEWS_MODE)
    logger.info("LOOKBACK=%d hours | %s -> %s", DISCOVERY_LOOKBACK_HOURS, DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat())

    prune_state()
    refresh_category_coverage()
    collect_rss()

    tech_count = queue_candidates_for_region("Tech")
    tech_count += google_news_gap_fill("Tech", tech_count, DISCOVERY_TARGET_PER_REGION)
    exa_gap_fill("Tech", tech_count, DISCOVERY_TARGET_PER_REGION)

    save_state(STATE)

    candidates = available_candidates("Tech", source_pool="primary")
    logger.info("DISCOVERY CANDIDATES: TECH=%d", len(candidates))

    ranked = prepare_ranked_region("Tech", candidates)
    logger.info("UNIQUE EVENTS: TECH=%d", len(ranked))

    for item in ranked[:12]:
        logger.info("RANK TECH #%s | %s | %s", item.get("editor_rank", "?"), item.get("title", ""), item.get("rank_reason", ""))

    stories = process_ranked_region("Tech", ranked)
    logger.info("FINAL: TECH=%d MAX=%d", len(stories), MAX_STORIES_PER_RUN)

    if not stories:
        logger.info("No tech story cleared the importance and verification bar this run.")

    published_count = 0
    for index, story in enumerate(stories, start=1):
        rich_html = fit_rich_html(story)
        if rich_visible_length(rich_html) > MAX_RICH_CHARACTERS:
            logger.error("Rich message exceeds Telegram limit: %s", story["headline"])
            continue

        image_path = prepare_image(story, index)
        result = send_rich_photo(image_path, rich_html)
        if not result.get("ok"):
            logger.warning("Rich Message publish failed; trying Bot API fallback: %s", result.get("description"))
            result = send_bot_api_fallback(image_path, rich_html)

        if result.get("ok"):
            published_count += 1
            message = result.get("result", {})
            message_id = message.get("message_id") if isinstance(message, dict) else None
            canonical = story["canonical"]
            POSTED_URLS.add(canonical)
            save_posted_url(canonical)

            queue_item = STATE["queue"].get(canonical)
            if queue_item:
                queue_item["status"] = "posted"
                queue_item["posted_at"] = now_iso()

            store_event(story, published=True, message_id=message_id)
            remember_posted_event(story)
            update_category_coverage(story)
            STATE["recent_titles"].append(normalize_title(story["headline"]))
            logger.info("Published %d/%d: [%s] %s", published_count, MAX_STORIES_PER_RUN, story.get("region", ""), story["headline"])
        else:
            logger.error("Telegram failed: %s", result.get("description"))
        save_state(STATE)
        time.sleep(POST_DELAY_SECONDS)

    save_state(STATE)
    logger.info("Finished. Published=%d/%d", published_count, MAX_STORIES_PER_RUN)


# ============================================================
# SELF TEST
# ============================================================

def self_test():
    sample = {
        "headline": "Major AI Platform Launches New Tool for Millions",
        "summary": "The platform launched a new AI tool that adds a major capability for users across its widely used technology ecosystem.",
        "highlights": [
            "The new tool is now available to users of the platform.",
            "The launch adds a major capability to the existing AI product.",
            "The company positioned the release as a significant expansion of its product offering.",
            "Availability begins immediately in supported markets.",
        ],
        "why_it_matters": "The launch can change how everyday users interact with the platform. It also signals a broader shift in how major technology companies are integrating AI into consumer products.",
        "whats_next": "Readers should watch for wider availability, usage limits and follow-up product updates.",
        "bold_terms": ["AI", "tool", "platform"],
        "source": "TechCrunch", "url": "https://example.com/story", "region": "Tech",
        "topic": "AI Models and Products", "institution": "OpenAI",
    }
    rendered = dynamic_rich_html(sample)
    assert complete_text("A normal sentence.")
    assert complete_text("An incomplete sentence—") is False
    assert "WHY IT MATTERS" in rendered
    assert "WHAT'S NEXT" in rendered
    assert "<aside>" not in rendered
    assert rendered.count("• ") == 4
    assert rendered.index("<h1>Major AI Platform") < rendered.index("KEY HIGHLIGHTS") < rendered.index("WHY IT MATTERS") < rendered.index("WHAT'S NEXT")

    sample_three = dict(sample)
    sample_three["highlights"] = sample_three["highlights"][:3]
    rendered_three = dynamic_rich_html(sample_three)
    assert rendered_three.count("• ") == 3

    sample_five = dict(sample)
    sample_five["highlights"] = sample_five["highlights"] + ["The release continues the company's broader AI strategy."]
    rendered_five = dynamic_rich_html(sample_five)
    assert rendered_five.count("• ") == 5
    assert rendered.index("#AI") > rendered.index("WHAT'S NEXT")
    assert "<footer><b>Source:</b>" in rendered
    import inspect
    assert "@TheTechNewsroom" in inspect.getsource(branded_card)
    assert likely_same_event("AI platform launches major tool", "AI platform launches major tool")
    assert canonical_url("https://www.example.com/story/?utm_source=x") == "example.com/story"
    assert "ai" in extract_entities("AI platform launches a major update")
    clustered = cluster_ranked_events([
        {"title": "AI platform launches major tool", "source": "TechCrunch", "url": "https://techcrunch.com/a", "published_date": now_iso(), "region": "Tech"},
        {"title": "AI platform launches major tool", "source": "The Verge", "url": "https://theverge.com/a", "published_date": now_iso(), "region": "Tech"},
    ])
    assert len(clustered) >= 1
    assert clustered[0]["event_cluster_size"] >= 1
    assert canonical_topic("ChatGPT") == "AI Models and Products"
    assert "#AI" in category_hashtags(sample) and "#Tech" in category_hashtags(sample)
    logger.info("TheTechNewsroom V1 self-test passed.")


def visible_text_for_test(
    rendered,
):
    text = re.sub(
        r"<[^>]+>",
        "",
        rendered,
    )
    return html.unescape(
        text
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--self-test",
        action="store_true",
    )

    args = parser.parse_args()

    if args.self_test:
        self_test()
    else:
        run()

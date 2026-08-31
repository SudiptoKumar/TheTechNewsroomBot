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

VALID_NEWS_MODES = {"update", "shadow"}
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

# V1 editorial target: structured, auditable importance scoring for clearly important tech stories.
# All qualifying tech stories compete in one ranked pool. There is no per-run story-count cap.
RANKING_POOL_SIZE = 999999
JOURNAL_FILE = "news_journal_{month}.jsonl"
RUN_REPORT_DIR = "run_reports"
DISCOVERY_LOOKBACK_HOURS = 72

# Reliability / quality
POST_DELAY_SECONDS = 20
ROLLING_DISCOVERY_HOURS = DISCOVERY_LOOKBACK_HOURS
FUTURE_TOLERANCE_MINUTES = 10
QUEUE_RETENTION_DAYS = 15
EVENT_RETENTION_DAYS = 15
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
    from urllib.parse import parse_qsl, urlencode, urlunsplit
    parsed = urlparse(raw if re.match(r"^https?://", raw, re.I) else "https://" + raw)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("amp.")
    path = re.sub(r"/(?:amp|amp\.html)/?$", "/", parsed.path or "/", flags=re.I)
    path = re.sub(r"/index\.html?$", "/", path, flags=re.I)
    path = re.sub(r"/{2,}", "/", path).rstrip("/") or "/"
    tracking = ("utm_", "mc_", "gclid", "fbclid", "ref", "source", "cmp", "sr_share", "at_medium", "at_campaign", "sh")
    pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        low = key.lower()
        if low in {"amp", "outputtype"} or any(low.startswith(prefix) for prefix in tracking):
            continue
        pairs.append((key, value))
    return urlunsplit(("https", host, path, urlencode(sorted(pairs)), ""))


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

    # The model is instructed to return plain text, but may occasionally
    # leak Markdown emphasis/code markers into JSON fields. Strip those
    # markers before any Telegram Rich HTML rendering so they never appear
    # literally in published posts.
    text = re.sub(r"[\*_`]+", "", text)

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
        # Permanent publication index. Queue/event caches may expire, but published
        # identity records are retained so an already-published story cannot re-enter.
        "published_history": {
            "records": {},
            "urls": {},
            "title_hashes": {},
            "content_hashes": {},
        },
    }


def _hash_text(value):
    return hashlib.sha256(safe_text(value).encode("utf-8")).hexdigest()


def normalize_content_for_hash(text):
    value = safe_text(text).lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^a-z0-9%$€£¥.,:/_-]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def content_hash(text):
    normalized = normalize_content_for_hash(text)
    return _hash_text(normalized) if normalized else ""


def content_excerpt_for_history(text, limit=1600):
    return normalize_content_for_hash(text)[:limit]


def _history_record_id(record):
    seed = "|".join([
        safe_text(record.get("canonical")),
        safe_text(record.get("headline")),
        safe_text(record.get("published_at")),
        safe_text(record.get("event_key")),
    ])
    return "pub_" + _hash_text(seed)[:20]


def _index_legacy_published_event(history, event_id, event):
    canonical = safe_text(event.get("canonical_url"))
    headline = safe_text(event.get("headline"))
    if not canonical and not headline:
        return
    record = {
        "history_id": f"legacy_{_hash_text(str(event_id))[:20]}",
        "canonical": canonical,
        "headline": headline,
        "topic": safe_text(event.get("topic")),
        "institution": safe_text(event.get("institution")),
        "event_key": safe_text(event.get("event_key")),
        "event_cluster_id": safe_text(event.get("event_cluster_id")),
        "published_at": safe_text(event.get("published_at")),
        "source": safe_text(event.get("source")),
        "title_hash": _hash_text(normalize_title(headline)) if headline else "",
        "content_hash": safe_text(event.get("content_hash")),
        "entity_fingerprint": safe_text(event.get("entity_fingerprint")),
    }
    history["records"].setdefault(record["history_id"], record)
    if canonical:
        history["urls"].setdefault(canonical, record["history_id"])
    if headline:
        history["title_hashes"].setdefault(_hash_text(normalize_title(headline)), record["history_id"])
    if record["content_hash"]:
        history["content_hashes"].setdefault(record["content_hash"], record["history_id"])


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

        history = base.get("published_history")
        if not isinstance(history, dict):
            history = default_state()["published_history"]
        history.setdefault("records", {})
        history.setdefault("urls", {})
        history.setdefault("title_hashes", {})
        history.setdefault("content_hashes", {})

        # Published history is permanent but intentionally compact. Older versions
        # stored long article excerpts and duplicate arrays, which caused the JSON
        # file to grow continuously on GitHub.
        compact_records = {}
        compact_urls = {}
        compact_titles = {}
        compact_contents = {}
        for rid, record in history.get("records", {}).items():
            headline = safe_text(record.get("headline"))
            normalized = normalize_title(headline)
            compact = {
                "history_id": safe_text(record.get("history_id")) or rid,
                "canonical": canonical_url(record.get("canonical", "")),
                "headline": headline,
                "topic": canonical_topic(record.get("topic", ""), "Tech"),
                "institution": safe_text(record.get("institution")),
                "event_key": safe_text(record.get("event_key")),
                "event_cluster_id": safe_text(record.get("event_cluster_id")),
                "published_at": safe_text(record.get("published_at")),
                "source": safe_text(record.get("source")),
                "title_hash": safe_text(record.get("title_hash")) or (_hash_text(normalized) if normalized else ""),
                "content_hash": safe_text(record.get("content_hash")),
                "entity_fingerprint": safe_text(record.get("entity_fingerprint")),
            }
            compact_records[compact["history_id"]] = compact
            if compact["canonical"]:
                compact_urls[compact["canonical"]] = compact["history_id"]
            if compact["title_hash"]:
                compact_titles[compact["title_hash"]] = compact["history_id"]
            if compact["content_hash"]:
                compact_contents[compact["content_hash"]] = compact["history_id"]

        # Migrate published events still present in the legacy event store into
        # the permanent index. Exact URLs remain covered by posted_urls.txt.
        for event_id, event in base.get("events", {}).items():
            if event.get("status") == "published":
                rid = f"legacy_{_hash_text(str(event_id))[:20]}"
                headline = safe_text(event.get("headline"))
                rec = {
                    "history_id": rid,
                    "canonical": canonical_url(event.get("canonical_url", "")),
                    "headline": headline,
                    "topic": canonical_topic(event.get("topic", ""), "Tech"),
                    "institution": safe_text(event.get("institution")),
                    "event_key": safe_text(event.get("event_key")),
                    "event_cluster_id": safe_text(event.get("event_cluster_id")),
                    "published_at": safe_text(event.get("published_at")),
                    "source": safe_text(event.get("source")),
                    "title_hash": _hash_text(normalize_title(headline)) if headline else "",
                    "content_hash": safe_text(event.get("content_hash")),
                    "entity_fingerprint": safe_text(event.get("entity_fingerprint")),
                }
                compact_records[rid] = rec
                if rec["canonical"]: compact_urls[rec["canonical"]] = rid
                if rec["title_hash"]: compact_titles[rec["title_hash"]] = rid
                if rec["content_hash"]: compact_contents[rec["content_hash"]] = rid

        history["records"] = compact_records
        history["urls"] = compact_urls
        history["title_hashes"] = compact_titles
        history["content_hashes"] = compact_contents
        base["published_history"] = history
        base["schema_version"] = 2
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
    urls = set()
    try:
        with open(
            POSTED_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            for line in f:
                raw = safe_text(line)
                if raw:
                    urls.add(canonical_url(raw.split("\t", 1)[-1]))
    except FileNotFoundError:
        pass
    return {u for u in urls if u}


def save_posted_url(canonical):
    if not canonical:
        return

    with open(
        POSTED_FILE,
        "a",
        encoding="utf-8",
    ) as f:
        f.write(f"{now_iso()}\t{canonical_url(canonical)}\n")


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
    STATE["posted_event_ids"] = list(dict.fromkeys(STATE.get("posted_event_ids", [])))

    titles = STATE.get(
        "recent_titles",
        [],
    )

    STATE["recent_titles"] = titles[-400:]
    rewrite_posted_file_15d()


def rewrite_posted_file_15d():
    cutoff = datetime.now(BD_TZ) - timedelta(days=15)
    lines = []
    for record in STATE.get("published_history", {}).get("records", {}).values():
        dt = parse_datetime(record.get("published_at"))
        canonical = canonical_url(record.get("canonical", ""))
        if canonical and dt and dt >= cutoff:
            lines.append(f"{dt.isoformat()}\t{canonical}")
    if lines:
        with open(POSTED_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(set(lines))) + "\n")


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

HARD_GENRE_PATTERNS = {
    "review": re.compile(r"\b(review|hands[- ]on|first look|unboxing|we tested|vs\.?|best \d+)\b", re.I),
    "rumor": re.compile(r"\b(rumor|rumour|leak(?:ed|s)?|allegedly|reportedly (?:will|may)|could (?:launch|arrive)|expected (?:in|to)|in the works|tipped to)\b", re.I),
    "vehicle": re.compile(r"\b(EV|electric (?:car|truck|SUV)|robotaxi|autonomous vehicl|charging network|Cybertruck)\b", re.I),
    "health": re.compile(r"\b(clinical|FDA (?:approval|clearance)|biotech|medtech|patients|therapeutic|drug|trial results|pharmaceutical)\b", re.I),
    "energy": re.compile(r"\b(solar|wind farm|grid|megawatt|battery storage|utility|power plant)\b", re.I),
    "podcast": re.compile(r"\b(podcast|webinar|roundtable|fireside|AMA recap)\b", re.I),
    "opinion": re.compile(r"\b(opinion|op-ed|why I|the case for|analysis:)\b", re.I),
}


def deterministic_prefilter(item):
    text = f"{safe_text(item.get('title'))} {safe_text(item.get('excerpt'))}"
    hits = [name for name, regex in HARD_GENRE_PATTERNS.items() if regex.search(text)]
    if not hits:
        return True
    title = safe_text(item.get("title"))
    # Escape hatch for potentially major events whose title contains a banned-genre keyword.
    return bool(
        item.get("trending")
        or re.search(r"\b(breach|vulnerability|outage|lawsuit|acquisition|acquires|launches|announces)\b", title, re.I)
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

    if not deterministic_prefilter(item):
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
                    "id": {"type": "integer", "minimum": 1},
                    "rank": {"type": "integer", "minimum": 1},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "impact": {"type": "integer", "minimum": 0, "maximum": 30},
                    "reach": {"type": "integer", "minimum": 0, "maximum": 25},
                    "novelty": {"type": "integer", "minimum": 0, "maximum": 20},
                    "certainty": {"type": "integer", "minimum": 0, "maximum": 15},
                    "durability": {"type": "integer", "minimum": 0, "maximum": 10},
                    "important": {"type": "boolean"},
                    "topic": {"type": "string"},
                    "institution": {"type": "string"},
                    "event_key": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "id", "rank", "score", "impact", "reach", "novelty",
                    "certainty", "durability", "important", "topic", "institution",
                    "event_key", "reason"
                ],
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
    return f"""
You are the senior editor of @TheTechNewsroom for everyday technology users.
Score every candidate using ONLY the supplied metadata and return EVERY candidate exactly once.

100-POINT RUBRIC
Impact 0-30: consequence for users or the technology industry.
Reach 0-25: size of the affected audience or ecosystem.
Novelty 0-20: genuinely new event/change versus routine continuation.
Certainty 0-15: strength and clarity of supplied evidence; multiple independent sources strengthen certainty.
Durability 0-10: likely lasting significance beyond the immediate news cycle.
TOTAL = impact + reach + novelty + certainty + durability.

BANDS
90-100 extraordinary; 80-89 major; 70-79 strongly important; 65-69 borderline publishable; 55-64 below the bar; 0-54 reject.
important=true ONLY when total >=65.

RULES
- Thin metadata: certainty <=6 and total <=64.
- Trending/corroboration may support certainty but can never rescue a weak story across 65 by itself.
- Famous company reputation is never a substitute for event impact.
- A clearly already-published underlying event is score 0.
- Reviews, hands-ons, unboxings, rumors, leaks/speculation, vehicle/EV, medical/biotech, energy, podcasts, opinion, routine funding/features, and low-level engineering are normally below 65 unless genuinely industry-changing.
- No category quotas. Sector diversity is only a later soft ordering preference.
- Do not invent facts or use outside knowledge.

Allowed topics: {', '.join(TOPICS[region])}

Return id, rank, score, impact, reach, novelty, certainty, durability, important, topic, institution, event_key, reason.
"""


def _rank_batch(batch, region, batch_no):
    lines = []
    for idx, item in enumerate(batch, start=1):
        published = item.get("published_date", "")
        age_note = ""
        dt = parse_datetime(published)
        age_hours = None
        if dt:
            age_hours = max(0.0, (NOW_BD - dt).total_seconds() / 3600)
            age_note = f"Age: {age_hours:.1f} hours"
        lines.append("\n".join([
            f"ID: {idx}",
            f"Title: {item.get('title','')}",
            f"Source: {item.get('source','')}",
            f"Published: {published}",
            age_note,
            f"TRENDING: {str(bool(item.get('trending'))).lower()}",
            f"CORROBORATING SOURCES: {int(item.get('corroboration_count', 1) or 1)}",
            f"Description/Excerpt: {trim_source_text(item.get('excerpt',''), 800)}",
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
            max_completion_tokens=5000,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return data.get("ranked", [])
    except Exception as exc:
        logger.error("Ranking batch %d failed: %s", batch_no, exc)
        return []


def _validated_component_score(row, item):
    """Recompute the score from structured components instead of trusting the LLM total."""
    def bounded(name, lo, hi):
        try:
            value = int(row.get(name, 0))
        except Exception:
            value = 0
        return max(lo, min(hi, value))

    impact = bounded("impact", 0, 30)
    reach = bounded("reach", 0, 25)
    novelty = bounded("novelty", 0, 20)
    certainty = bounded("certainty", 0, 15)
    durability = bounded("durability", 0, 10)
    if int(item.get("corroboration_count", 1) or 1) >= 2:
        certainty = min(15, certainty + 2)
    score = impact + reach + novelty + certainty + durability

    # Deterministic editorial hard gates.
    excerpt = safe_text(item.get("excerpt", ""))
    title = safe_text(item.get("title", ""))
    if len((title + " " + excerpt).strip()) < THIN_EXCERPT_CHARS:
        certainty = min(certainty, 6)
        score = min(score, 64)
    return max(0, min(100, score)), impact, reach, novelty, certainty, durability


def _annotate_corroboration(candidates):
    items = list(candidates)
    for item in items:
        sources = {safe_text(item.get("source"))}
        for other in items:
            if other is item:
                continue
            if same_event_window(item, other, hours=72) and title_similarity(item.get("title", ""), other.get("title", "")) >= 0.76:
                source = safe_text(other.get("source"))
                if source:
                    sources.add(source)
        item["corroboration_count"] = len(sources)
    return items


def rank_candidates(candidates, region):
    """Rank the discovery pool in bounded LLM batches and merge into one global pool."""
    if not candidates:
        return []

    regional = _annotate_corroboration(candidates)
    regional = enrich_thin_excerpts(regional)
    regional.sort(
        key=lambda x: parse_datetime(x.get("published_date")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    regional = regional[:80]

    batch_size = 15
    ranked_rows = []
    for offset in range(0, len(regional), batch_size):
        batch = regional[offset:offset + batch_size]
        batch_no = offset // batch_size + 1
        logger.info("%s RANK BATCH %d: %d candidates", region, batch_no, len(batch))
        rows = _rank_batch(batch, region, batch_no)
        by_id = {idx: item for idx, item in enumerate(batch, start=1)}
        seen_ids = set()
        for row in rows:
            try:
                idx = int(row["id"])
            except Exception:
                continue
            if idx not in by_id or idx in seen_ids:
                continue
            seen_ids.add(idx)
            item = dict(by_id[idx])
            score, impact, reach, novelty, certainty, durability = _validated_component_score(row, item)
            important = score >= 65
            item.update({
                "importance_score": score,
                "importance_components": {
                    "impact": impact,
                    "reach": reach,
                    "novelty": novelty,
                    "certainty": certainty,
                    "durability": durability,
                },
                "important": important,
                "topic": canonical_topic(safe_text(row.get("topic")), region),
                "institution": safe_text(row.get("institution")),
                "event_key": safe_text(row.get("event_key")),
                "rank_reason": safe_text(row.get("reason")),
                "batch_rank": int(row.get("rank", 9999)),
            })
            ranked_rows.append(item)

    ranked_rows.sort(key=lambda x: (
        -x.get("importance_score", 0),
        -(parse_datetime(x.get("published_date")).timestamp() if parse_datetime(x.get("published_date")) else 0),
        x.get("batch_rank", 9999),
    ))

    for rank, item in enumerate(ranked_rows, start=1):
        item["editor_rank"] = rank

    logger.info("%s RANK MODEL ROWS: %d/%d", region, len(ranked_rows), len(regional))
    distribution = {}
    for item in ranked_rows:
        distribution[str(item.get("importance_score", 0))] = distribution.get(str(item.get("importance_score", 0)), 0) + 1
    logger.info("%s SCORE DISTRIBUTION: %s", region, json.dumps(distribution, sort_keys=True))
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
    STATE["posted_event_ids"] = ids
    return event_id


# ============================================================
# ARTICLE EXTRACTION
# ============================================================

def find_article_image_candidates(
    url,
    page_html=None,
    final_url=None,
):
    """Return multiple likely article-image URLs from page metadata/HTML.

    RSS image URLs can be stale, thumbnail-only, blocked, or non-image redirects.
    A single `item.image or og:image` choice therefore causes valid article images
    to be skipped. We collect several candidates and validate them later.
    """
    candidates = []

    try:
        base_url = final_url or url

        if page_html is None:
            response = session.get(
                url,
                headers={**HEADERS, "Referer": url},
                timeout=20,
            )
            if response.status_code >= 400:
                return []
            page_html = response.text
            base_url = response.url

        soup = BeautifulSoup(page_html, "html.parser")

        def add(value):
            value = safe_text(value).strip()
            if not value:
                return
            value = urljoin(base_url, value)
            if value and value not in candidates:
                candidates.append(value)

        # High-confidence social/article metadata first.
        for attrs in (
            {"property": "og:image"},
            {"property": "og:image:url"},
            {"name": "twitter:image"},
            {"name": "twitter:image:src"},
        ):
            for tag in soup.find_all("meta", attrs=attrs):
                add(tag.get("content"))

        # JSON-LD article/image fields.
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text(" ", strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue

            stack = data if isinstance(data, list) else [data]
            while stack:
                node = stack.pop()
                if isinstance(node, list):
                    stack.extend(node)
                    continue
                if not isinstance(node, dict):
                    continue
                for key in ("image", "thumbnailUrl", "contentUrl"):
                    value = node.get(key)
                    if isinstance(value, str):
                        add(value)
                    elif isinstance(value, dict):
                        add(value.get("url") or value.get("contentUrl"))
                    elif isinstance(value, list):
                        for sub in value:
                            add(sub if isinstance(sub, str) else (sub or {}).get("url") if isinstance(sub, dict) else "")
                for key in ("@graph", "itemListElement", "mainEntity"):
                    value = node.get(key)
                    if isinstance(value, list):
                        stack.extend(value)
                    elif isinstance(value, dict):
                        stack.append(value)

        # Common responsive/lazy-loaded image attributes. Prefer larger srcset
        # candidates when possible.
        for tag in soup.find_all("img"):
            for attr in (
                "data-src", "data-lazy-src", "data-original",
                "data-image", "data-image-url", "src",
            ):
                add(tag.get(attr))
            srcset = tag.get("srcset") or tag.get("data-srcset") or ""
            parts = []
            for token in srcset.split(","):
                bit = token.strip().split()
                if bit:
                    width = 0
                    if len(bit) > 1 and bit[1].endswith("w"):
                        try:
                            width = int(bit[1][:-1])
                        except ValueError:
                            width = 0
                    parts.append((width, bit[0]))
            for _, value in sorted(parts, reverse=True):
                add(value)

        # Explicit image preload/link tags.
        for tag in soup.find_all("link"):
            rel = " ".join(tag.get("rel") or []).lower()
            if "image" in rel:
                add(tag.get("href"))

    except Exception as exc:
        logger.info("Article image metadata lookup failed %s: %s", url, exc)

    return candidates[:20]


def find_og_image(
    url,
    page_html=None,
    final_url=None,
):
    """Backward-compatible helper returning the first likely article image."""
    candidates = find_article_image_candidates(
        url, page_html, final_url
    )
    return candidates[0] if candidates else ""


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

            image_candidates = []
            if item.get("image"):
                image_candidates.append(item.get("image"))
            image_candidates.extend(
                find_article_image_candidates(
                    url,
                    page_html,
                    response.url,
                )
            )

            if text and len(safe_text(text)) >= 500:
                return (
                    safe_text(text),
                    image_candidates[0] if image_candidates else "",
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
        "the_context": {"type": "string"},
        "bottom_line": {"type": "string"},
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
        "the_context",
        "bottom_line",
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
- The Context: 2-4 complete sentences of background explaining how the story came about.
  This can cover a launch, lawsuit, funding round, acquisition, product change, security incident,
  research paper, or other relevant background.
- Bottom Line: exactly ONE complete sentence giving the central takeaway or "so what" of the story.
- No repetition between sections.
- No "..." or "…".
- Never end a headline or highlight with an ellipsis.
- No hashtags in generated fields.
- No Markdown or HTML in JSON fields. Do not use **bold**, __bold__, `code`, or other Markdown markers.

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
## THE CONTEXT (collapsed by default)
2-4 sentences of background
## BOTTOM LINE (collapsed by default)
1 sentence takeaway
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

            the_context = clean_generated_text(data.get("the_context"))
            bottom_line = clean_generated_text(data.get("bottom_line"))
            context_count = len(re.findall(r"(?<=[.!?])\s+", the_context)) + (1 if the_context and the_context[-1] in ".!?" else 0)
            bottom_count = len(re.findall(r"(?<=[.!?])\s+", bottom_line)) + (1 if bottom_line and bottom_line[-1] in ".!?" else 0)
            if not the_context or not bottom_line or not (2 <= context_count <= 4) or bottom_count != 1:
                raise ValueError("Invalid The Context or Bottom Line")

            if (
                not headline
                or not summary
                or not complete_text(headline)
                or not complete_text(summary)
                or any(not complete_text(x) for x in highlights)
                or not complete_text(the_context)
                or not complete_text(bottom_line)
            ):
                raise ValueError("Incomplete story")

            story = {
                **item,
                "headline": trim_source_text(headline, 110),
                "summary": trim_source_text(summary, 260),
                "highlights": [trim_source_text(x, 130) for x in highlights],
                "the_context": trim_source_text(the_context, 520),
                "bottom_line": trim_source_text(bottom_line, 220),
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
        clean_generated_text(x)
        for x in story.get(
            "bold_terms",
            [],
        )
        if clean_generated_text(x)
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
        "<blockquote expandable><b>THE CONTEXT</b><br>"
        + bold_terms_html(story.get("the_context", ""), terms)
        + "</blockquote>",
        "<blockquote expandable><b>BOTTOM LINE</b><br>"
        + bold_terms_html(story.get("bottom_line", ""), terms)
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
        (260, 130, 520, 220),
        (220, 115, 440, 190),
        (190, 100, 380, 170),
        (160, 85, 320, 150),
    ]

    for summary_len, highlight_len, context_len, bottom_len in variants:
        candidate = dict(story)
        candidate["summary"] = trim_source_text(story["summary"], summary_len)
        candidate["highlights"] = [trim_source_text(x, highlight_len) for x in story.get("highlights", [])]
        candidate["the_context"] = trim_source_text(story.get("the_context", ""), context_len)
        candidate["bottom_line"] = trim_source_text(story.get("bottom_line", ""), bottom_len)
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


def display_source_name(source):
    """Return a reader-friendly publication label for the image chip."""
    raw = safe_text(source).strip()
    if not raw:
        return "Source"

    aliases = {
        "WIRED": "Wired",
        "ZDNET": "ZDNET",
        "9to5Google": "9to5Google",
        "MacRumors": "MacRumors",
        "WABetaInfo": "WABetaInfo",
        "TestingCatalog": "TestingCatalog",
        "AI News": "AI News",
        "Unite.AI": "Unite.AI",
        "The Decoder": "The Decoder",
        "SiliconANGLE": "SiliconANGLE",
        "Techmeme": "Techmeme",
        "MIT Technology Review": "MIT Technology Review",
    }
    if raw in aliases:
        return aliases[raw]
    return raw


def _logo_candidates_from_homepage(homepage):
    candidates = []
    try:
        response = session.get(
            homepage,
            headers=HEADERS,
            timeout=15,
        )
        if not response.ok:
            return candidates
        soup = BeautifulSoup(response.text, "html.parser")

        def score_tag(tag):
            rel = " ".join(tag.get("rel") or []).lower()
            sizes = safe_text(tag.get("sizes")).lower()
            score = 0
            if "apple-touch-icon" in rel:
                score += 100
            elif "icon" in rel:
                score += 60
            if "mask-icon" in rel:
                score += 20
            if sizes == "any":
                score += 10
            m = re.search(r"(\d+)x(\d+)", sizes)
            if m:
                score += min(64, int(m.group(1)) + int(m.group(2)))
            return score

        tags = [
            tag for tag in soup.find_all("link")
            if any(x in " ".join(tag.get("rel") or []).lower() for x in ("icon", "apple-touch-icon"))
            and tag.get("href")
        ]
        tags.sort(key=score_tag, reverse=True)
        candidates.extend(urljoin(homepage, safe_text(tag.get("href"))) for tag in tags)

        # OpenGraph/logo and JSON-LD organization logos are usually better than
        # a generic 16px favicon when present.
        for attrs in (
            {"property": "og:logo"},
            {"property": "og:image"},
        ):
            for tag in soup.find_all("meta", attrs=attrs):
                if tag.get("content"):
                    candidates.append(urljoin(homepage, safe_text(tag["content"])))

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text(" ", strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            stack = data if isinstance(data, list) else [data]
            while stack:
                node = stack.pop()
                if isinstance(node, list):
                    stack.extend(node)
                    continue
                if not isinstance(node, dict):
                    continue
                logo = node.get("logo")
                if isinstance(logo, str):
                    candidates.append(urljoin(homepage, logo))
                elif isinstance(logo, dict) and logo.get("url"):
                    candidates.append(urljoin(homepage, safe_text(logo["url"])))
                graph = node.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
    except Exception as exc:
        logger.info("Source logo page lookup failed for %s: %s", homepage, exc)
    return candidates


def download_source_logo(source, article_url=""):
    """Download the publication logo/icon for an image-less fallback.

    We prefer the publication's own high-resolution icon/logo. Google Favicon is
    only a final fallback. Small source icons are intentionally enlarged later.
    """
    source = display_source_name(source)
    host = (urlparse(article_url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if not host:
        host_map = {
            "TechCrunch": "techcrunch.com", "The Verge": "theverge.com",
            "WIRED": "wired.com", "Wired": "wired.com",
            "Ars Technica": "arstechnica.com", "Engadget": "engadget.com",
            "MIT Technology Review": "technologyreview.com", "Hacker News": "news.ycombinator.com",
            "VentureBeat": "venturebeat.com", "Techmeme": "techmeme.com",
            "TechRadar": "techradar.com", "ZDNET": "zdnet.com",
            "9to5Google": "9to5google.com", "WABetaInfo": "wabetainfo.com",
            "TestingCatalog": "testingcatalog.com", "AI News": "artificialintelligence-news.com",
            "Unite.AI": "unite.ai", "The Decoder": "the-decoder.com",
            "SiliconANGLE": "siliconangle.com", "Android Authority": "androidauthority.com",
            "MacRumors": "macrumors.com",
        }
        host = host_map.get(source, "")

    candidates = []
    if host:
        homepage = f"https://{host}/"
        candidates.extend(_logo_candidates_from_homepage(homepage))
        # Site-local fallback before third-party favicon service.
        candidates.extend([
            f"https://{host}/apple-touch-icon.png",
            f"https://{host}/favicon.ico",
        ])
        candidates.append(
            f"https://www.google.com/s2/favicons?domain={quote(host)}&sz=256"
        )

    seen = set()
    ranked = []
    for candidate in candidates:
        candidate = safe_text(candidate).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        # Prefer non-Google candidates over the generic favicon endpoint.
        priority = 0 if "google.com/s2/favicons" in candidate else 10
        ranked.append((priority, candidate))

    ranked.sort(key=lambda x: -x[0])
    for _, candidate in ranked:
        try:
            response = session.get(
                candidate,
                headers=HEADERS,
                timeout=15,
            )
            if response.status_code >= 400:
                continue
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not content_type.startswith("image/"):
                continue
            if len(response.content) > 4_000_000:
                continue
            logo = Image.open(BytesIO(response.content))
            logo.load()
            if logo.width < 24 or logo.height < 24:
                continue
            return logo.convert("RGBA")
        except Exception as exc:
            logger.info("Source logo download failed for %s: %s", source, exc)

    return None


def trim_logo_transparency(logo):
    """Trim transparent borders so small favicon canvases can be enlarged cleanly."""
    rgba = logo.convert("RGBA")
    alpha = rgba.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        return rgba.crop(bbox)
    return rgba


def fallback_canvas_from_logo(source_logo):
    """Create a neutral high-contrast 1200x675 canvas for the source logo."""
    if source_logo is None:
        return Image.new("RGB", (1200, 675), (28, 38, 50))

    logo = trim_logo_transparency(source_logo)
    sample = Image.new("RGBA", logo.size, (255, 255, 255, 255))
    sample.alpha_composite(logo)
    lum = image_average_brightness(sample.convert("RGB"))

    # Choose a strong neutral opposite to the logo's overall luminance so the
    # enlarged logo remains readable without relying on a tiny badge.
    bg = (245, 247, 250) if lum < 128 else (25, 32, 41)
    return Image.new("RGB", (1200, 675), bg)

def draw_channel_chip(draw, font, base_size=(1200, 675)):
    """Draw the standard bottom-right @TheTechNewsroom chip."""
    width, height = base_size
    channel_text = "@TheTechNewsroom"
    bbox = draw.textbbox((0, 0), channel_text, font=font)
    padding_x = 18
    padding_y = 9
    margin_x = 28
    margin_y = 24
    chip_w = (bbox[2] - bbox[0]) + padding_x * 2
    chip_h = (bbox[3] - bbox[1]) + padding_y * 2
    x2 = width - margin_x
    y2 = height - margin_y
    x1 = x2 - chip_w
    y1 = y2 - chip_h
    return (x1, y1, x2, y2, padding_x, padding_y, channel_text)


def branded_card(
    photo,
    source,
    source_position="left",
    source_logo=None,
):
    """Crop an article image and apply only the standard channel chip.

    For image-less fallback cards, center a large source logo; if unavailable,
    center the bold source name. The channel username is always bottom-right.
    """
    base = crop_cover(photo).convert("RGBA")
    brightness = image_average_brightness(base)

    if brightness < 125:
        chip_bg = (245, 245, 245, 225)
        chip_fg = (20, 24, 28, 255)
    else:
        chip_bg = (18, 22, 28, 205)
        chip_fg = (245, 245, 245, 255)

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font_path = find_font(bold=True)
    font = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
    source_font = ImageFont.truetype(font_path, 62) if font_path else font

    x1, y1, x2, y2, padding_x, padding_y, channel_text = draw_channel_chip(draw, font)
    draw.rounded_rectangle((x1, y1, x2, y2), radius=16, fill=chip_bg)
    draw.text((x1 + padding_x, y1 + padding_y - 1), channel_text, font=font, fill=chip_fg)

    if source_position == "center":
        label = display_source_name(source)
        if source_logo is not None:
            logo = trim_logo_transparency(source_logo)
            # Large on purpose: aim for roughly 60-70% of the card's visual
            # width/height rather than showing a tiny favicon.
            max_w, max_h = 660, 430
            scale = min(
                max_w / max(1, logo.width),
                max_h / max(1, logo.height),
            )
            logo = logo.resize(
                (max(1, int(logo.width * scale)), max(1, int(logo.height * scale))),
                Image.Resampling.LANCZOS,
            )
            # Pick a contrasting backing from the logo itself.
            preview = Image.new("RGBA", logo.size, (255, 255, 255, 255))
            preview.alpha_composite(logo)
            logo_lum = image_average_brightness(preview.convert("RGB"))
            backing_color = (22, 28, 36, 235) if logo_lum > 128 else (255, 255, 255, 238)
            pad_x, pad_y = 42, 34
            backing = Image.new(
                "RGBA",
                (logo.width + pad_x * 2, logo.height + pad_y * 2),
                backing_color,
            )
            center_x = base.width // 2
            center_y = base.height // 2
            bx = center_x - backing.width // 2
            by = center_y - backing.height // 2
            overlay.alpha_composite(backing, (bx, by))
            overlay.alpha_composite(logo, (center_x - logo.width // 2, center_y - logo.height // 2))
        else:
            bbox = draw.textbbox((0, 0), label, font=source_font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            center_x = base.width // 2
            center_y = base.height // 2
            box_pad_x, box_pad_y = 36, 22
            bx1 = center_x - text_w // 2 - box_pad_x
            by1 = center_y - text_h // 2 - box_pad_y
            bx2 = center_x + text_w // 2 + box_pad_x
            by2 = center_y + text_h // 2 + box_pad_y
            draw.rounded_rectangle((bx1, by1, bx2, by2), radius=22, fill=(18, 22, 28, 225))
            draw.text((center_x - text_w // 2, center_y - text_h // 2 - 2), label, font=source_font, fill=(255, 255, 255, 255))

    return Image.alpha_composite(base, overlay).convert("RGB")


def prepare_image(
    story,
    index,
):
    """Resolve an article image robustly, then fall back to source branding."""
    image = None
    candidate_urls = []
    initial = safe_text(story.get("image_url", ""))
    if initial:
        candidate_urls.append(initial)

    # If the stored/RSS image is broken, inspect the article itself for OG,
    # JSON-LD, srcset and lazy-loaded images instead of giving up immediately.
    try:
        response = session.get(
            story["url"],
            headers={**HEADERS, "Referer": story["url"]},
            timeout=20,
        )
        if response.ok:
            candidate_urls.extend(
                find_article_image_candidates(
                    story["url"],
                    response.text,
                    response.url,
                )
            )
    except Exception as exc:
        logger.info("Image metadata refresh failed %s: %s", story.get("url", ""), exc)

    # Exa can sometimes expose an image different from the RSS thumbnail.
    try:
        if len(candidate_urls) < 3 and story.get("url"):
            result_set = get_exa().get_contents(
                [story["url"]],
                text={"max_characters": 2000},
            )
            if result_set.results:
                exa_image = safe_text(getattr(result_set.results[0], "image", ""))
                if exa_image:
                    candidate_urls.append(exa_image)
    except Exception:
        pass

    seen = set()
    for image_url in candidate_urls:
        image_url = safe_text(image_url).strip()
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        image = download_image(image_url, story["url"])
        if image is not None:
            logger.info("IMAGE SOURCE: article | %s", image_url)
            break

    image_was_missing = image is None
    source_logo = None

    if image is None:
        source_logo = download_source_logo(
            story.get("source", "Source"),
            story.get("url", ""),
        )
        image = fallback_canvas_from_logo(source_logo)
        if source_logo is not None:
            logger.info("IMAGE FALLBACK: source logo | %s", story.get("source", "Source"))
        else:
            logger.info("IMAGE FALLBACK: source name | %s", story.get("source", "Source"))

    branded = branded_card(
        image,
        story.get("source", "Source"),
        source_position="center" if image_was_missing else "left",
        source_logo=source_logo,
    )

    path = f"/tmp/news_{index}.jpg"
    branded.save(path, "JPEG", quality=88, optimize=True)
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
        "event_key": story.get("event_key", ""),
        "content_hash": story.get("content_hash", ""),
        "content_excerpt": story.get("content_excerpt", ""),
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
    """Build a verification pool using quality first and sector diversity as a soft tie-breaker."""
    if not ranked:
        return []

    eligible = [dict(x) for x in ranked if x.get("importance_score", 0) >= 7 and x.get("important") is True]
    # V1 publishes every qualifying candidate; the argument is retained for compatibility.
    target = len(eligible)
    if target <= 0:
        return []

    def sector_group(item):
        topic = canonical_topic(item.get("topic"), "Tech")
        for group, topics in CATEGORY_GROUPS.items():
            if topic in topics:
                return group
        return "Other"

    selected = []
    remaining = list(eligible)
    used_groups = set()

    # Quality remains dominant. Diversity can only win when the candidate is within one
    # point of the current best score, preventing a weak sector-filler from overtaking a
    # clearly superior story.
    while remaining and len(selected) < target:
        remaining.sort(key=lambda x: (
            -x.get("importance_score", 0),
            -(parse_datetime(x.get("published_date")).timestamp() if parse_datetime(x.get("published_date")) else 0),
            x.get("editor_rank", 9999),
        ))
        best = remaining[0]
        best_score = best.get("importance_score", 0)
        near_best = [x for x in remaining if x.get("importance_score", 0) >= best_score - 1]
        diverse = next((x for x in near_best if sector_group(x) not in used_groups), None)
        pick = diverse or best
        selected.append(pick)
        used_groups.add(sector_group(pick))
        remaining.remove(pick)

    # Final order is always global editorial score so generation proceeds from strongest to weaker.
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

    # Check extracted article content before generation so syndicated copies with
    # different URLs/titles cannot consume generation budget.
    if is_already_published_candidate(item, article_text=article_text):
        logger.info("DROP already published content: %s", item.get("title", ""))
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
    story["content_hash"] = content_hash(article_text)
    story["content_excerpt"] = content_excerpt_for_history(article_text)

    return story


# ============================================================
# MAIN
# ============================================================

def _published_history():
    history = STATE.setdefault("published_history", {})
    history.setdefault("records", {})
    history.setdefault("urls", {})
    history.setdefault("title_hashes", {})
    history.setdefault("content_hashes", {})
    return history


def _history_title_similarity(item, record):
    return title_similarity(
        safe_text(item.get("title") or item.get("headline")),
        safe_text(record.get("headline")),
    )


def _history_entity_overlap(item, record):
    left_text = f"{item.get('title') or item.get('headline', '')} {item.get('excerpt', '')}"
    right_text = safe_text(record.get("headline", ""))
    left = extract_entities(left_text)
    right = extract_entities(right_text)
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def is_already_published_candidate(item, article_text=""):
    canonical = canonical_url(safe_text(item.get("canonical") or item.get("url")))
    if canonical and canonical in POSTED_URLS:
        logger.info("DUPLICATE URL: %s", canonical)
        return True

    history = _published_history()
    if canonical and canonical in history["urls"]:
        logger.info("DUPLICATE HISTORY URL: %s", canonical)
        return True

    cluster_id = safe_text(item.get("event_cluster_id"))
    if cluster_id and cluster_id in STATE.get("posted_event_ids", []):
        logger.info("DUPLICATE POSTED EVENT CLUSTER: %s", cluster_id)
        return True

    title = safe_text(item.get("title") or item.get("headline"))
    if not title:
        return False

    title_key = _hash_text(normalize_title(title))
    if title_key in history["title_hashes"]:
        logger.info("DUPLICATE HISTORY EXACT TITLE: %s", title)
        return True

    text_hash = content_hash(article_text) if article_text else ""
    if text_hash and text_hash in history["content_hashes"]:
        logger.info("DUPLICATE HISTORY CONTENT HASH: %s", title)
        return True

    topic = canonical_topic(item.get("topic"), item.get("region", "Tech"))
    institution = safe_text(item.get("institution"))
    event_key = safe_text(item.get("event_key"))

    for record in history["records"].values():
        rec_topic = canonical_topic(record.get("topic"), item.get("region", "Tech"))
        if topic and rec_topic and topic != rec_topic:
            continue

        title_score = _history_title_similarity(item, record)
        entity_score = _history_entity_overlap(item, record)
        rec_event_key = safe_text(record.get("event_key"))
        rec_institution = safe_text(record.get("institution"))

        # Same event key + same topic + meaningful title/entity agreement.
        if event_key and rec_event_key and event_key == rec_event_key:
            if title_score >= 0.55 or entity_score >= 0.34:
                logger.info("DUPLICATE HISTORY EVENT KEY: %s", title)
                return True

        # Very strong cross-source title match is safe to treat as the same story.
        if title_score >= 0.94:
            logger.info("DUPLICATE HISTORY TITLE SIMILARITY %.3f: %s", title_score, title)
            return True

        # Same topic + same institution + substantial entity/title overlap catches
        # syndicated/reworded copies without blocking unrelated stories in the topic.
        if (
            institution
            and rec_institution
            and institution.lower() == rec_institution.lower()
            and title_score >= 0.80
            and entity_score >= 0.40
        ):
            logger.info("DUPLICATE HISTORY TOPIC/ENTITY MATCH: %s", title)
            return True

        # Exact content_hash above covers unchanged syndications. The permanent
        # record intentionally does not retain article bodies, keeping GitHub state small.

    return False


def remember_published_history(story, article_text=""):
    history = _published_history()
    canonical = canonical_url(safe_text(story.get("canonical") or story.get("url")))
    normalized_title = normalize_title(story.get("headline", ""))
    c_hash = safe_text(story.get("content_hash")) or content_hash(article_text)
    excerpt = content_excerpt_for_history(article_text) if article_text else safe_text(story.get("content_excerpt", ""))

    record = {
        "canonical": canonical,
        "headline": safe_text(story.get("headline")),
        "topic": canonical_topic(story.get("topic"), story.get("region", "Tech")),
        "institution": safe_text(story.get("institution")),
        "event_key": safe_text(story.get("event_key")),
        "event_cluster_id": safe_text(story.get("event_cluster_id")),
        "published_at": safe_text(story.get("published_date")),
        "source": safe_text(story.get("source")),
        "title_hash": _hash_text(normalize_title(story.get("headline", ""))),
        "content_hash": c_hash,
        "entity_fingerprint": _hash_text("|".join(sorted(extract_entities(story.get("headline", "")))))[:24],
    }
    record["history_id"] = _history_record_id(record)
    history["records"][record["history_id"]] = record
    if canonical:
        history["urls"][canonical] = record["history_id"]
    if normalized_title:
        history["title_hashes"][_hash_text(normalized_title)] = record["history_id"]
    if c_hash:
        history["content_hashes"][c_hash] = record["history_id"]
    return record["history_id"]


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
        if item.get("importance_score", 0) >= 65
        and item.get("important") is True
    ]
    logger.info("%s IMPORTANCE PASS (score>=65): %d", region, len(eligible))
    logger.info("%s SCORE BANDS: >=90=%d | 80-89=%d | 70-79=%d | 65-69=%d", region,
                sum(1 for x in eligible if x.get("importance_score", 0) >= 90),
                sum(1 for x in eligible if 80 <= x.get("importance_score", 0) < 90),
                sum(1 for x in eligible if 70 <= x.get("importance_score", 0) < 80),
                sum(1 for x in eligible if 65 <= x.get("importance_score", 0) < 70))

    persist_event_cluster_state(eligible)
    return eligible


def process_ranked_region(region, ranked):
    pool = build_candidate_pool(ranked, len(ranked))
    valid = []
    attempted = 0
    rejected = 0

    for item in pool:
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
        "%s FINAL VALID: %d | pool=%d attempted=%d rejected=%d",
        region,
        len(valid),
        len(pool),
        attempted,
        rejected,
    )
    return valid


def append_journal(kind,payload):
    journal_path = JOURNAL_FILE.format(month=datetime.now(BD_TZ).strftime("%Y-%m"))
    with open(journal_path,"a",encoding="utf-8") as f:
        f.write(json.dumps({"ts":now_iso(),"kind":kind,**payload},ensure_ascii=False,separators=(",",":"))+"\n")

def write_run_report(metrics):
    os.makedirs(RUN_REPORT_DIR,exist_ok=True)
    path=os.path.join(RUN_REPORT_DIR,datetime.now(BD_TZ).strftime("%Y-%m-%dT%H-%M-%S.json"))
    with open(path,"w",encoding="utf-8") as f: json.dump(metrics,f,ensure_ascii=False,indent=2)
    return path

def run():
    logger.info("THE TECH NEWSROOM EVENT-CENTRIC V1")
    metrics={"started_at":now_iso()}
    prune_state(); refresh_category_coverage(); collect_rss()
    count=queue_candidates_for_region("Tech")
    count += google_news_gap_fill("Tech",count,DISCOVERY_TARGET_PER_REGION)
    exa_gap_fill("Tech",count,DISCOVERY_TARGET_PER_REGION); save_state(STATE)
    candidates=available_candidates("Tech",source_pool="primary"); metrics["discovered"]=len(candidates)
    append_journal("discovery",{"count":len(candidates)})
    ranked=prepare_ranked_region("Tech",candidates); metrics["ranked"]=len(ranked); metrics["events"]=len({x.get("event_cluster_id") for x in ranked})
    stories=process_ranked_region("Tech",ranked); metrics["verified"]=len(stories)
    if NEWS_MODE == "shadow":
        metrics["finished_at"]=now_iso(); write_run_report(metrics); save_state(STATE)
        logger.info("SHADOW complete. No Telegram posts sent.")
        return
    for idx,story in enumerate(stories,1):
        rich_html=fit_rich_html(story)
        if rich_visible_length(rich_html)>MAX_RICH_CHARACTERS: continue
        append_journal("publish_reserved",{"event_id":make_event_id(story),"canonical":story.get("canonical"),"headline":story.get("headline")})
        image_path=prepare_image(story,idx); result=send_rich_photo(image_path,rich_html)
        if not result.get("ok"): result=send_bot_api_fallback(image_path,rich_html)
        if result.get("ok"):
            metrics["published"]=metrics.get("published",0)+1
            msg=result.get("result",{}); mid=msg.get("message_id") if isinstance(msg,dict) else None
            canonical=story["canonical"]; POSTED_URLS.add(canonical); save_posted_url(canonical)
            q=STATE["queue"].get(canonical)
            if q: q["status"]="posted"; q["posted_at"]=now_iso()
            store_event(story,published=True,message_id=mid); remember_published_history(story); remember_posted_event(story); update_category_coverage(story); STATE["recent_titles"].append(normalize_title(story["headline"]))
            append_journal("published",{"event_id":make_event_id(story),"canonical":canonical,"headline":story.get("headline"),"message_id":mid,"topic":story.get("topic"),"score":story.get("importance_score",0)})
        else:
            metrics["publish_failed"]=metrics.get("publish_failed",0)+1
            append_journal("publish_failed",{"event_id":make_event_id(story),"canonical":story.get("canonical"),"headline":story.get("headline"),"error":result.get("description","")})
        save_state(STATE); time.sleep(POST_DELAY_SECONDS)
    metrics.setdefault("published",0); metrics["finished_at"]=now_iso(); write_run_report(metrics); save_state(STATE)
    logger.info("Finished. Published=%d qualifying stories",metrics["published"])


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
        "the_context": "The launch follows the company's broader push to expand AI capabilities across its technology platform. The move builds on earlier product work and extends those capabilities to more users.",
        "bottom_line": "The release matters because it expands a major AI capability to a broader technology audience.",
        "bold_terms": ["AI", "tool", "platform"],
        "source": "TechCrunch", "url": "https://example.com/story", "region": "Tech",
        "topic": "AI Models and Products", "institution": "OpenAI",
    }
    rendered = dynamic_rich_html(sample)
    assert complete_text("A normal sentence.")
    markdown_sample = dict(sample)
    markdown_sample["summary"] = "A **major AI** launch adds `new` capabilities."
    markdown_sample["highlights"] = ["The **platform** expands AI tools.", "Users get __broader__ access.", "The `release` is available now."]
    markdown_sample["bold_terms"] = ["**platform**", "__AI__", "`release`"]
    markdown_rendered = dynamic_rich_html(markdown_sample)
    assert "**" not in markdown_rendered
    assert "__" not in markdown_rendered
    assert "`" not in markdown_rendered
    assert "<b>platform</b>" in markdown_rendered
    assert "<b>AI</b>" in markdown_rendered
    assert complete_text("An incomplete sentence—") is False
    assert "THE CONTEXT" in rendered
    assert "BOTTOM LINE" in rendered
    assert rendered.count('<blockquote expandable>') == 2
    assert "<aside>" not in rendered
    assert rendered.count("• ") == 4
    assert rendered.index("<h1>Major AI Platform") < rendered.index("KEY HIGHLIGHTS") < rendered.index("THE CONTEXT") < rendered.index("BOTTOM LINE")

    sample_three = dict(sample)
    sample_three["highlights"] = sample_three["highlights"][:3]
    rendered_three = dynamic_rich_html(sample_three)
    assert rendered_three.count("• ") == 3

    sample_five = dict(sample)
    sample_five["highlights"] = sample_five["highlights"] + ["The release continues the company's broader AI strategy."]
    rendered_five = dynamic_rich_html(sample_five)
    assert rendered_five.count("• ") == 5
    assert rendered.index("#AI") > rendered.index("BOTTOM LINE")
    assert "<footer><b>Source:</b>" in rendered
    import inspect
    branded_source = inspect.getsource(branded_card)
    prepare_source = inspect.getsource(prepare_image)
    assert "draw_channel_chip" in branded_source
    assert "channel_text = \"@TheTechNewsroom\"" in inspect.getsource(draw_channel_chip)
    assert "download_source_logo" in prepare_source
    assert likely_same_event("AI platform launches major tool", "AI platform launches major tool")
    assert canonical_url("https://www.example.com/story/?utm_source=x") == "https://example.com/story"
    assert "ai" in extract_entities("AI platform launches a major update")
    clustered = cluster_ranked_events([
        {"title": "AI platform launches major tool", "source": "TechCrunch", "url": "https://techcrunch.com/a", "published_date": now_iso(), "region": "Tech"},
        {"title": "AI platform launches major tool", "source": "The Verge", "url": "https://theverge.com/a", "published_date": now_iso(), "region": "Tech"},
    ])
    assert len(clustered) >= 1
    assert clustered[0]["event_cluster_size"] >= 1
    assert canonical_topic("ChatGPT") == "AI Models and Products"
    assert "#AI" in category_hashtags(sample) and "#Tech" in category_hashtags(sample)

    # Event-centric structured 100-point scoring tests.
    row = {"impact": 30, "reach": 25, "novelty": 20, "certainty": 15, "durability": 10, "score": 2}
    total, impact, reach, novelty, certainty, durability = _validated_component_score(
        row, {"title": "Major AI model launch", "excerpt": "Major model launch with broad impact across users and products. The announcement includes enough concrete detail about the product, availability, users, and significance to establish why this is an important technology event for a broad audience."}
    )
    assert (total, impact, reach, novelty, certainty, durability) == (100, 30, 25, 20, 15, 10)
    thin_total, *_ = _validated_component_score(
        row, {"title": "AI update", "excerpt": "Short"}
    )
    assert thin_total <= 64
    trending_total, *_ = _validated_component_score(
        {"impact": 20, "reach": 15, "novelty": 10, "certainty": 5, "durability": 5},
        {"title": "Trending story", "excerpt": "Clear metadata describing a modest technology development with enough detail to judge that it is useful but not broadly important to everyday technology users.", "trending": True},
    )
    assert trending_total < 65

    # Structured ranking merge test: every batch remains recoverable and the merged
    # pool is globally sorted by the recomputed score.
    original_rank_batch = globals()["_rank_batch"]
    original_enrich = globals()["enrich_thin_excerpts"]
    try:
        globals()["enrich_thin_excerpts"] = lambda items: items
        def fake_rank_batch(batch, region, batch_no):
            return [
                {
                    "id": i, "rank": i, "score": 0,
                    "impact": 26 if i == 1 else 20, "reach": 20, "novelty": 15,
                    "certainty": 12, "durability": 8, "important": True,
                    "topic": "AI Models and Products" if i == 1 else "Cybersecurity",
                    "institution": "OpenAI" if i == 1 else "Google",
                    "event_key": f"event_{batch_no}_{i}",
                    "reason": "test",
                } for i in range(1, len(batch) + 1)
            ]
        globals()["_rank_batch"] = fake_rank_batch
        fake_candidates = [
            {"title": f"Story {i}", "excerpt": "A sufficiently descriptive technology story with enough useful metadata to establish the event, audience impact, novelty, and evidence for editorial ranking.",
             "published_date": now_iso(), "source": "TechCrunch", "url": f"https://example.com/{i}", "region": "Tech"}
            for i in range(31)
        ]
        merged = rank_candidates(fake_candidates, "Tech")
        assert len(merged) == 31
        assert merged[0]["importance_score"] >= merged[-1]["importance_score"]
        assert merged[0]["editor_rank"] == 1
    finally:
        globals()["_rank_batch"] = original_rank_batch
        globals()["enrich_thin_excerpts"] = original_enrich

    # Sector diversity remains a soft preference: it operates on similarly scored
    # candidates and never lowers the publication threshold.
    diverse_ranked = []
    topic_list = [
        "AI Models and Products", "Cybersecurity", "Startups",
        "GitHub Trends", "Operating Systems", "Major Tech Companies",
        "AI Models and Products", "Cybersecurity",
    ]
    for i, topic in enumerate(topic_list, start=1):
        diverse_ranked.append({
            "title": f"Important story {i}", "importance_score": 65 if i != 7 else 82,
            "important": True, "topic": topic, "editor_rank": i,
            "published_date": now_iso(),
        })
    pool = build_candidate_pool(diverse_ranked, len(diverse_ranked))
    assert len(pool) == 8
    assert pool[0]["importance_score"] == 82
    assert len({canonical_topic(x["topic"]) for x in pool[:6]}) >= 4

    # Permanent duplicate protection tests.
    state_backup = json.loads(json.dumps(STATE))
    urls_backup = set(POSTED_URLS)
    try:
        STATE["published_history"] = default_state()["published_history"]
        STATE["posted_event_ids"] = []
        published_story = dict(sample)
        published_story.update({
            "canonical": "example.com/original-story",
            "source": "TechCrunch",
            "published_date": now_iso(),
            "event_key": "openai_new_model_launch",
            "institution": "OpenAI",
            "topic": "AI Models and Products",
            "region": "Tech",
            "concepts": ["AI model", "coding"],
            "key_numbers": [],
        })
        original_article = "OpenAI launched a new AI model that improves reasoning and coding for developers."
        published_story["content_hash"] = content_hash(original_article)
        published_story["content_excerpt"] = content_excerpt_for_history(original_article)
        remember_published_history(published_story)

        assert is_already_published_candidate({
            "canonical": "https://www.example.com/original-story?utm_source=x",
            "title": "Changed wording",
            "region": "Tech",
        })
        assert is_already_published_candidate({
            "canonical": "other.com/syndicated-copy",
            "title": "OpenAI launches a new AI model for developers",
            "topic": "AI Models and Products",
            "institution": "OpenAI",
            "event_key": "openai_new_model_launch",
            "excerpt": original_article,
            "region": "Tech",
        })
        assert is_already_published_candidate({
            "canonical": "other.com/rehosted",
            "title": "Different headline",
            "region": "Tech",
        }, article_text=original_article)
        assert not is_already_published_candidate({
            "canonical": "other.com/unrelated",
            "title": "OpenAI opens a new office in Tokyo",
            "topic": "Major Tech Companies",
            "institution": "OpenAI",
            "region": "Tech",
        })

        STATE["events"] = {
            "old-published": {"status": "published", "published_at": "2020-01-01T00:00:00+06:00", "headline": "Old story"},
            "old-selected": {"status": "selected", "selected_at": "2020-01-01T00:00:00+06:00"},
        }
        prune_state()
        assert "old-published" not in STATE["events"]
        assert "old-selected" not in STATE["events"]
        assert canonical_url("https://www.example.com/story/index.html?utm_source=x") == "https://example.com/story"
    finally:
        STATE.clear()
        STATE.update(state_backup)
        POSTED_URLS.clear()
        POSTED_URLS.update(urls_backup)

    assert DISCOVERY_LOOKBACK_HOURS == 72
    assert "shadow" in VALID_NEWS_MODES
    many=[{"title":f"Qualifying {i}","importance_score":65+(i%35),"important":True,"topic":"AI Models and Products","editor_rank":i,"published_date":now_iso()} for i in range(1,21)]
    assert len(build_candidate_pool(many,len(many)))==20
    _self_test_image_helpers()
    logger.info("TheTechNewsroom self-test passed.")



def _self_test_image_helpers():
    from types import SimpleNamespace

    original_get = session.get
    try:
        class DummyResponse:
            status_code = 200
            ok = True
            url = "https://example.com/story"
            text = '<html><head>\n<meta property="og:image" content="/images/hero.jpg">\n<meta name="twitter:image" content="/images/twitter.jpg">\n<script type="application/ld+json">{"@type":"NewsArticle","image":{"url":"/images/jsonld.jpg"}}</script>\n</head><body><img src="/images/lazy-small.jpg" data-src="/images/lazy-large.jpg" srcset="/images/small.jpg 400w, /images/large.jpg 1200w"></body></html>'

        def fake_get(url, **kwargs):
            return DummyResponse()

        session.get = fake_get
        candidates = find_article_image_candidates("https://example.com/story")
        assert candidates[0].endswith("/images/hero.jpg")
        assert any(x.endswith("/images/jsonld.jpg") for x in candidates)
        assert any(x.endswith("/images/large.jpg") for x in candidates)

        dark_logo = Image.new("RGBA", (100, 100), (20, 20, 20, 255))
        light_logo = Image.new("RGBA", (100, 100), (245, 245, 245, 255))
        dark_canvas = fallback_canvas_from_logo(dark_logo)
        light_canvas = fallback_canvas_from_logo(light_logo)
        assert dark_canvas.size == (1200, 675)
        assert light_canvas.size == (1200, 675)
        assert dark_canvas.getpixel((0, 0)) != light_canvas.getpixel((0, 0))

        card = branded_card(dark_canvas, "Wired", source_position="center", source_logo=dark_logo)
        assert card.size == (1200, 675)

        text_card = branded_card(Image.new("RGB", (1200, 675), (30, 40, 50)), "Wired", source_position="center", source_logo=None)
        assert text_card.size == (1200, 675)
    finally:
        session.get = original_get

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

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

TELEGRAM_CHANNEL = (os.environ.get("TELEGRAM_CHANNEL") or "@BusinessNewsroom").strip()

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

# Version 1 editorial target: exactly three Bangladesh + three International
# valid stories whenever enough eligible news exists. No cross-region quota
# competition: each region is ranked independently from the same 24-hour window.
STORIES_PER_REGION = 3
MAX_STORIES_PER_RUN = STORIES_PER_REGION * 2
RANKING_POOL_PER_REGION = 12
DISCOVERY_LOOKBACK_HOURS = 24

# Reliability / quality
POST_DELAY_SECONDS = 3.5
ROLLING_DISCOVERY_HOURS = DISCOVERY_LOOKBACK_HOURS
FUTURE_TOLERANCE_MINUTES = 10
QUEUE_RETENTION_DAYS = 4
EVENT_RETENTION_DAYS = 30
MAX_RSS_CANDIDATES_PER_REGION = 120
MAX_EXA_CANDIDATES_PER_REGION = 30
MAX_GOOGLE_NEWS_CANDIDATES_PER_REGION = 20
THIN_EXCERPT_CHARS = 150
MAX_EXCERPT_ENRICH_PER_REGION = 12
MAX_SOURCE_PER_REGION_PER_RUN = 99
MAX_RICH_CHARACTERS = 32768

# Lightweight English stopwords used only by the conservative event/entity
# deduplication layer. This is deliberately small so legitimate business
# entities and meaningful terms are not filtered out.
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
    # Bangladesh
    {
        "name": "The Business Standard",
        "region": "Bangladesh",
        "url": "https://www.tbsnews.net/top-news/rss.xml",
    },
    {
        "name": "The Daily Star",
        "region": "Bangladesh",
        "url": "https://www.thedailystar.net/frontpage/rss.xml",
    },
    {
        "name": "bdnews24",
        "region": "Bangladesh",
        "url": "https://bdnews24.com/?widgetName=rssfeed&widgetId=1150&getXmlFeed=true",
    },
    {
        "name": "Dhaka Tribune",
        "region": "Bangladesh",
        "url": "https://www.dhakatribune.com/feed",
    },
    {
        "name": "The Financial Express",
        "region": "Bangladesh",
        "url": "https://thefinancialexpress.com.bd/rss.xml",
    },
    {
        "name": "New Age",
        "region": "Bangladesh",
        "url": "https://www.newagebd.net/rss",
    },
    # International
    {
        "name": "CNBC",
        "region": "International",
        "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    },
    {
        "name": "MarketWatch",
        "region": "International",
        "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories.xml",
    },
    {
        "name": "Financial Times",
        "region": "International",
        "url": "https://www.ft.com/rss/home",
    },
]


# ============================================================
# TAXONOMY: BUSINESS + ECONOMIC NEWS
# ============================================================

TOPICS = {
    "Bangladesh": [
        "Bangladesh Economy",
        "Banking",
        "Bangladesh Bank",
        "Monetary Policy",
        "Inflation",
        "GDP",
        "Employment",
        "Poverty",
        "Government Budget",
        "Tax and VAT",
        "Foreign Exchange",
        "Top Currency Rates",
        "Foreign Reserves",
        "Remittance",
        "Export and Import",
        "RMG",
        "Food Prices",
        "DSE and CSE",
        "Share Market",
        "BSEC",
        "Gold",
        "Commodities",
        "Fuel and Oil",
        "Energy",
        "Industry",
        "FDI",
        "Economic Projects",
        "Economic Agreements",
        "Economic Institutions",
    ],
    "International": [
        "IMF",
        "World Bank",
        "ADB",
        "AIIB",
        "IsDB",
        "BIS",
        "WTO",
        "UNCTAD",
        "OPEC",
        "Fed",
        "ECB",
        "Bank of England",
        "Bank of Japan",
        "PBOC",
        "RBI",
        "Global Economy",
        "Global Inflation",
        "Global Interest Rates",
        "Global Forex",
        "Global Markets",
        "Trade",
        "Tariffs",
        "Sanctions",
        "Oil",
        "Gold",
        "Commodities",
        "Shipping",
        "Supply Chain",
        "FDI",
        "Multinational Companies",
        "Economic Crisis",
        "Economic Reports",
        "Economic Appointments",
    ],
}

INSTITUTIONS = [
    "Bangladesh Bank",
    "BSEC",
    "NBR",
    "EPB",
    "BBS",
    "BEZA",
    "IMF",
    "World Bank",
    "ADB",
    "AIIB",
    "IsDB",
    "BIS",
    "WTO",
    "UNCTAD",
    "OPEC",
    "Federal Reserve",
    "European Central Bank",
    "Bank of England",
    "Bank of Japan",
    "People's Bank of China",
    "Reserve Bank of India",
]

SOURCE_NAMES = {
    "tbsnews.net": "The Business Standard",
    "thedailystar.net": "The Daily Star",
    "thefinancialexpress.com.bd": "The Financial Express",
    "dhakatribune.com": "Dhaka Tribune",
    "businesspostbd.com": "The Business Post",
    "newagebd.net": "New Age",
    "bdnews24.com": "bdnews24",
    "reuters.com": "Reuters",
    "cnbc.com": "CNBC",
    "bloomberg.com": "Bloomberg",
    "ft.com": "Financial Times",
    "marketwatch.com": "MarketWatch",
    "forbes.com": "Forbes",
}



# ============================================================
# CATEGORY METADATA
# ============================================================

# Categories that should normally receive at least one meaningful
# update per day when a valid story exists.
CATEGORY_HASHTAGS = {
    "Bangladesh Economy": ["#BangladeshEconomy", "#EconomyBD"],
    "Banking": ["#Banking", "#Finance"],
    "Bangladesh Bank": ["#BangladeshBank", "#Banking"],
    "Monetary Policy": ["#MonetaryPolicy", "#BangladeshBank"],
    "Inflation": ["#Inflation", "#BangladeshEconomy"],
    "GDP": ["#GDP", "#BangladeshEconomy"],
    "Foreign Exchange": ["#Forex", "#USDBDT"],
    "Top Currency Rates": ["#Currency", "#Forex"],
    "Foreign Reserves": ["#ForeignReserves", "#BangladeshEconomy"],
    "Remittance": ["#Remittance", "#BangladeshEconomy"],
    "Export and Import": ["#ExportImport", "#Trade"],
    "RMG": ["#RMG", "#BangladeshExports"],
    "Food Prices": ["#FoodPrices", "#BangladeshEconomy"],
    "Share Market": ["#ShareMarket", "#DSE"],
    "DSE and CSE": ["#DSE", "#CSE"],
    "BSEC": ["#BSEC", "#CapitalMarket"],
    "Gold": ["#Gold", "#PreciousMetals"],
    "Commodities": ["#Commodities", "#CommodityPrices"],
    "Energy": ["#Energy", "#Power"],
    "Fuel and Oil": ["#Oil", "#Fuel"],
    "FDI": ["#FDI", "#Investment"],
    "Government Budget": ["#Budget", "#BangladeshEconomy"],
    "Tax and VAT": ["#Tax", "#VAT"],
    "Global Economy": ["#GlobalEconomy", "#WorldEconomy"],
    "Global Inflation": ["#Inflation", "#GlobalEconomy"],
    "Global Interest Rates": ["#InterestRates", "#MonetaryPolicy"],
    "Global Forex": ["#Forex", "#GlobalMarkets"],
    "Global Markets": ["#GlobalMarkets", "#StockMarket"],
    "Global Trade": ["#GlobalTrade", "#InternationalTrade"],
    "Fed": ["#FederalReserve", "#InterestRates"],
    "IMF": ["#IMF", "#InternationalFinance"],
    "World Bank": ["#WorldBank", "#InternationalFinance"],
    "ADB": ["#ADB", "#DevelopmentFinance"],
    "WTO": ["#WTO", "#GlobalTrade"],
    "Oil": ["#Oil", "#Commodities"],
    "Supply Chain": ["#SupplyChain", "#GlobalTrade"],
}

CATEGORY_GROUPS = {
    "Bangladesh Core": {
        "Bangladesh Economy",
        "Banking",
        "Bangladesh Bank",
        "Monetary Policy",
        "Inflation",
        "Foreign Exchange",
        "Foreign Reserves",
        "Remittance",
    },
    "Bangladesh Market": {
        "Share Market",
        "DSE and CSE",
        "BSEC",
        "Gold",
        "Commodities",
        "Top Currency Rates",
        "Fuel and Oil",
    },
    "Bangladesh Trade": {
        "Export and Import",
        "RMG",
        "FDI",
        "Trade",
        "Economic Agreements",
    },
    "International Core": {
        "Global Economy",
        "Global Inflation",
        "Global Interest Rates",
        "Global Forex",
        "Global Markets",
        "Global Trade",
        "IMF",
        "World Bank",
        "ADB",
        "WTO",
        "Fed",
    },
}


TOPIC_ALIASES = {
    "food": "Food Prices",
    "food prices": "Food Prices",
    "commodity": "Commodities",
    "commodity prices": "Commodities",
    "gold & precious metals": "Gold",
    "precious metals": "Gold",
    "share market": "Share Market",
    "stock market": "Share Market",
    "export & import": "Export and Import",
    "exports and imports": "Export and Import",
    "forex": "Foreign Exchange",
    "currency": "Top Currency Rates",
}

def canonical_topic(topic, region="Bangladesh"):
    raw = safe_text(topic)
    key = raw.lower().strip()

    if key in TOPIC_ALIASES:
        return TOPIC_ALIASES[key]

    allowed = (
        TOPICS.get("Bangladesh", [])
        + TOPICS.get("International", [])
    )

    for item in allowed:
        if key == item.lower():
            return item

    if region == "Bangladesh":
        patterns = [
            (("export", "import", "trade deficit"), "Export and Import"),
            (("gold", "precious metal"), "Gold"),
            (("commodity",), "Commodities"),
            (("dsex", "dse", "cse", "ipo", "dividend", "stock", "share"), "Share Market"),
            (("usd/bdt", "exchange rate", "forex", "currency", "dollar"), "Foreign Exchange"),
            (("bank", "lending", "deposit", "npl"), "Banking"),
            (("repo", "policy rate", "crr", "slr", "monetary"), "Monetary Policy"),
            (("inflation", "cpi"), "Inflation"),
            (("rmg", "garment", "textile"), "RMG"),
            (("remittance",), "Remittance"),
            (("reserve",), "Foreign Reserves"),
            (("budget", "adp"), "Government Budget"),
            (("tax", "vat", "nbr"), "Tax and VAT"),
            (("energy", "power", "electricity", "gas", "lng"), "Energy"),
        ]
    else:
        patterns = [
            (("imf",), "IMF"),
            (("world bank",), "World Bank"),
            (("adb",), "ADB"),
            (("wto",), "WTO"),
            (("fed", "federal reserve"), "Fed"),
            (("ecb",), "ECB"),
            (("bank of england", "boe"), "Bank of England"),
            (("bank of japan", "boj"), "Bank of Japan"),
            (("pboc", "people's bank of china"), "PBOC"),
            (("rbi", "reserve bank of india"), "RBI"),
            (("inflation", "cpi"), "Global Inflation"),
            (("interest rate", "rate cut", "rate hike"), "Global Interest Rates"),
            (("forex", "currency", "dollar"), "Global Forex"),
            (("stock", "equity", "nasdaq", "s&p", "dow jones"), "Global Markets"),
            (("trade", "tariff"), "Global Trade"),
            (("oil", "brent", "wti"), "Oil"),
            (("gold", "precious metal"), "Gold"),
            (("commodity", "wheat", "corn", "soybean"), "Commodities"),
            (("shipping", "supply chain"), "Supply Chain"),
        ]

    for needles, canonical in patterns:
        if any(
            needle in key
            for needle in needles
        ):
            return canonical

    return (
        "Bangladesh Economy"
        if region == "Bangladesh"
        else "Global Economy"
    )

def category_hashtags(story):
    """
    Generate 2-3 stable hashtags from the validated topic,
    institution, and relevant story categories.
    """
    tags = []
    topic = safe_text(story.get("topic"))
    institution = safe_text(story.get("institution"))

    for tag in CATEGORY_HASHTAGS.get(topic, []):
        if tag not in tags:
            tags.append(tag)

    inst_map = {
        "Bangladesh Bank": "#BangladeshBank",
        "BSEC": "#BSEC",
        "IMF": "#IMF",
        "World Bank": "#WorldBank",
        "ADB": "#ADB",
        "WTO": "#WTO",
        "Federal Reserve": "#FederalReserve",
    }

    if institution in inst_map and inst_map[institution] not in tags:
        tags.append(inst_map[institution])

    if story.get("region") == "Bangladesh":
        if "#Bangladesh" not in tags:
            tags.append("#Bangladesh")
    else:
        if "#GlobalEconomy" not in tags:
            tags.append("#GlobalEconomy")

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
logger = logging.getLogger("business-news-bot")

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
    if is_domain_allowed(url, PRIMARY_BD_DOMAINS + FALLBACK_BD_DOMAINS):
        return "Bangladesh"
    return "International"


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

exa = Exa(
    api_key=EXA_API_KEY
)

cerebras = Cerebras(
    api_key=CEREBRAS_API_KEY
)


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
PRIMARY_BD_DOMAINS = [
    "tbsnews.net",
    "thefinancialexpress.com.bd",
    "thedailystar.net",
    "dhakatribune.com",
    "newagebd.net",
]

PRIMARY_INTL_DOMAINS = [
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "visualcapitalist.com",
    "economist.com",
]

FALLBACK_BD_DOMAINS = [
    "bdnews24.com",
    "businesspostbd.com",
    "unb.com.bd",
    "en.prothomalo.com",
]

FALLBACK_INTL_DOMAINS = [
    "finance.yahoo.com",
    "yahoo.com",
    "forbes.com",
    "cnbc.com",
    "marketwatch.com",
    "investing.com",
    "fortune.com",
]

ALL_PRIMARY_DOMAINS = PRIMARY_BD_DOMAINS + PRIMARY_INTL_DOMAINS
ALL_FALLBACK_DOMAINS = FALLBACK_BD_DOMAINS + FALLBACK_INTL_DOMAINS
ALL_ALLOWED_DOMAINS = ALL_PRIMARY_DOMAINS + ALL_FALLBACK_DOMAINS

def normalized_domain(url_or_source):
    raw = safe_text(url_or_source).lower()
    if "://" in raw:
        raw = urlparse(raw).netloc
    return raw.split(":")[0].removeprefix("www.").strip().rstrip("/")

def is_domain_allowed(url, domains):
    domain = normalized_domain(url)
    return any(domain == d or domain.endswith("." + d) for d in domains)

def primary_domain_allowed(url, region=None):
    domains = (
        PRIMARY_BD_DOMAINS if region == "Bangladesh"
        else PRIMARY_INTL_DOMAINS if region == "International"
        else ALL_PRIMARY_DOMAINS
    )
    return is_domain_allowed(url, domains)

def fallback_domain_allowed(url, region):
    domains = FALLBACK_BD_DOMAINS if region == "Bangladesh" else FALLBACK_INTL_DOMAINS
    return is_domain_allowed(url, domains)

def allowed_source_for_region(url, region):
    return primary_domain_allowed(url, region) or fallback_domain_allowed(url, region)

# ============================================================
# GOOGLE NEWS RSS: FREE GAP FILL (TRIED BEFORE PAID EXA)
# ============================================================
#
# Covers sources that have no clean native RSS feed of their own
# (Reuters/Bloomberg business, and BD outlets like Business Post,
# UNB, Prothom Alo English, Bangladesh Bank coverage), at no API
# cost. Google News RSS links are redirect tokens, not the real
# article URL, so every item is resolved to its real publisher URL
# before it is treated as a candidate. If resolution fails, the
# item is skipped rather than queued with a bad URL.

GOOGLE_NEWS_QUERIES = {
    "Bangladesh": [
        "site:tbsnews.net business OR economy OR banking",
        "site:thefinancialexpress.com.bd business OR economy OR banking",
        "site:thedailystar.net business OR economy OR banking",
        "site:dhakatribune.com business OR economy OR banking",
        "site:newagebd.net business OR economy OR banking",
    ],
    "International": [
        "site:reuters.com business OR economy OR markets",
        "site:bloomberg.com economy OR markets",
        "site:ft.com economy OR markets OR companies",
        "site:visualcapitalist.com economy OR markets OR business",
        "site:economist.com economy OR business OR finance",
    ],
}


GOOGLE_NEWS_LOCALE = {
    "Bangladesh": ("en-BD", "BD", "BD:en"),
    "International": ("en-US", "US", "US:en"),
}


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

    queries = GOOGLE_NEWS_QUERIES.get(region, [])
    hl, gl, ceid = GOOGLE_NEWS_LOCALE.get(
        region,
        ("en-US", "US", "US:en"),
    )

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

                if added >= MAX_GOOGLE_NEWS_CANDIDATES_PER_REGION:
                    return added

        except Exception as exc:
            logger.warning(
                "Google News gap fill failed %s: %s",
                region,
                exc,
            )

    return added


def exa_gap_fill(region, existing_count, needed, fallback=False):
    if existing_count >= max(6, needed * 3):
        return 0

    if region == "Bangladesh":
        domains = FALLBACK_BD_DOMAINS if fallback else PRIMARY_BD_DOMAINS
        queries = [
            "latest Bangladesh banking economy monetary policy inflation",
            "latest Bangladesh Bank policy banking reserves forex remittance",
            "latest Bangladesh budget tax exports imports trade deficit",
            "latest Bangladesh stock market business companies investment",
            "latest Bangladesh industry RMG investment FDI business",
        ]
    else:
        domains = FALLBACK_INTL_DOMAINS if fallback else PRIMARY_INTL_DOMAINS
        queries = [
            "latest global central bank inflation interest rates business",
            "latest global trade tariffs oil gold markets economy",
            "latest global companies earnings mergers investment",
            "latest global finance banking markets corporate news",
        ]

    added = 0
    for query in queries:
        try:
            results = exa.search_and_contents(
                query,
                type="auto",
                category="news",
                num_results=6,
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
                    "title": title,
                    "url": url,
                    "canonical": canonical_url(url),
                    "published_dt": published_dt.isoformat(),
                    "published_date": published_dt.isoformat(),
                    "source": source_name(url),
                    "region": region,
                    "excerpt": safe_text(
                        " ".join(
                            getattr(result, "highlights", [])
                            if isinstance(getattr(result, "highlights", []), list)
                            else str(getattr(result, "highlights", ""))
                        )
                    )[:2000],
                    "image": safe_text(getattr(result, "image", "")),
                    "discovery": "exa_fallback" if fallback else "exa",
                    "source_pool": "fallback" if fallback else "primary",
                }

                if not candidate_basic_allowed({
                    **item,
                    "published_dt": published_dt,
                }):
                    continue
                if item["canonical"] in POSTED_URLS:
                    continue
                if item["canonical"] in STATE["queue"]:
                    continue

                queue_candidate(item)
                added += 1

                if added >= MAX_EXA_CANDIDATES_PER_REGION:
                    return added

        except Exception as exc:
            logger.warning(
                "Exa %s discovery failed %s: %s",
                "fallback" if fallback else "primary",
                region,
                exc,
            )

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
                    "topic": {"type": "string"},
                    "institution": {"type": "string"},
                    "event_key": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "rank", "topic", "institution", "event_key", "reason"],
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
        if enriched >= MAX_EXCERPT_ENRICH_PER_REGION:
            break
        excerpt = safe_text(item.get("excerpt", ""))
        if len(excerpt) >= THIN_EXCERPT_CHARS:
            continue
        fuller = enrich_thin_excerpt(item)
        if fuller and len(fuller) > len(excerpt):
            item["excerpt"] = fuller
            enriched += 1
    return regional


def rank_candidates(candidates, region):
    """Rank all usable candidates without hard score formulas or early rejection.

    The LLM acts only as an editor/ranker: it does not decide eligibility.
    Eligibility remains the simple ingestion/state rules used elsewhere.
    """
    if not candidates:
        return []

    regional = sorted(
        candidates,
        key=lambda x: parse_datetime(x.get("published_date")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:60]
    regional = enrich_thin_excerpts(regional)

    lines = []
    for idx, item in enumerate(regional, start=1):
        published = item.get("published_date", "")
        age_note = ""
        dt = parse_datetime(published)
        if dt:
            age_hours = max(0.0, (NOW_BD - dt).total_seconds() / 3600)
            age_note = f"Age: {age_hours:.1f} hours"
        lines.append(
            "\n".join([
                f"ID: {idx}",
                f"Title: {item.get('title','')}",
                f"Source: {item.get('source','')}",
                f"Published: {published}",
                age_note,
                f"Excerpt: {trim_source_text(item.get('excerpt',''), 700)}",
                "",
            ])
        )

    topic_list = ", ".join(TOPICS[region])
    prompt = f"""
You are the editor-in-chief of @BusinessNewsroom.

Rank these {region} business/economic news candidates from MOST IMPORTANT to LEAST IMPORTANT.
Use the previous 24 hours as the editorial window.

Your job is ranking, not aggressive filtering. Keep ordinary candidates in the ranking unless
an item is clearly not business/economic news or is an obvious duplicate of another candidate.

Prioritize:
1. A genuinely important new business/economic development.
2. Material policy, banking, financial, trade, market, currency, commodity, corporate,
   institutional, or macroeconomic developments.
3. The newest development when two stories are otherwise similarly important.
4. Clear factual evidence and a credible source.
5. Stories that people would reasonably want to read now.

Deprioritize:
- routine or trivial updates when materially stronger news exists
- promotional fluff
- lifestyle/entertainment content
- opinion/editorial content when no new factual development exists
- exact duplicate coverage of the same event

Do not manufacture importance. Do not assign numeric scores.
Return EVERY candidate with its editorial rank and the requested metadata.

Allowed topic taxonomy:
{topic_list}
"""

    try:
        response = cerebras.chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "\n".join(lines)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "business_news_v1_rank",
                    "strict": True,
                    "schema": RANK_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=6000,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        by_id = {idx: item for idx, item in enumerate(regional, start=1)}
        rows = []
        for row in data.get("ranked", []):
            idx = int(row["id"])
            if idx not in by_id:
                continue
            item = dict(by_id[idx])
            item.update({
                "editor_rank": int(row["rank"]),
                "topic": canonical_topic(safe_text(row.get("topic")), region),
                "institution": safe_text(row.get("institution")),
                "event_key": safe_text(row.get("event_key")),
                "rank_reason": safe_text(row.get("reason")),
            })
            rows.append(item)

        # Never let a partial model response erase candidates.
        returned_ids = {safe_text(x.get("canonical")) for x in rows}
        next_rank = max([x.get("editor_rank", 0) for x in rows] or [0]) + 1
        missing = [x for x in regional if safe_text(x.get("canonical")) not in returned_ids]
        for item in missing:
            fallback = dict(item)
            fallback.update({
                "editor_rank": next_rank,
                "topic": canonical_topic(fallback.get("topic", ""), region),
                "institution": fallback.get("institution", ""),
                "event_key": fallback.get("event_key", ""),
                "rank_reason": "Kept as recoverable fallback candidate.",
            })
            rows.append(fallback)
            next_rank += 1

        rows.sort(key=lambda x: (
            x.get("editor_rank", 9999),
            -(parse_datetime(x.get("published_date")).timestamp() if parse_datetime(x.get("published_date")) else 0),
        ))
        return rows

    except Exception as exc:
        logger.error("Editorial ranking failed for %s: %s", region, exc)
        fallback = []
        for idx, item in enumerate(regional, start=1):
            row = dict(item)
            row.update({
                "editor_rank": idx,
                "topic": canonical_topic(row.get("topic", ""), region),
                "institution": row.get("institution", ""),
                "event_key": row.get("event_key", ""),
                "rank_reason": "Recency fallback after ranking-service failure.",
            })
            fallback.append(row)
        return fallback


# ============================================================
# VERSION 1 EVENT DEDUPLICATION
# ============================================================

def extract_entities(text):
    words = re.findall(r"[A-Za-z][A-Za-z&'-]{2,}", safe_text(text).lower())
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
            same_key = bool(
                safe_text(item.get("event_key"))
                and safe_text(item.get("event_key")) == safe_text(representative.get("event_key"))
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
        result_set = exa.get_contents(
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
            "minItems": 2,
            "maxItems": 3,
        },
        "bold_terms": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
        },
        "what_to_know": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "meaning": {"type": "string"},
                },
                "required": ["term", "meaning"],
                "additionalProperties": False,
            },
            "minItems": 1,
            "maxItems": 3,
        },
        "vocabulary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "word": {"type": "string"},
                    "synonyms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "antonyms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
                "required": ["word", "synonyms", "antonyms"],
                "additionalProperties": False,
            },
            "minItems": 3,
            "maxItems": 3,
        },
    },
    "required": [
        "headline",
        "summary",
        "highlights",
        "bold_terms",
        "what_to_know",
        "vocabulary",
    ],
    "additionalProperties": False,
}


def first_sentence(text):
    text = clean_generated_text(
        text
    )

    # Conservative sentence extraction. Avoids common financial
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
You are a senior newspaper business editor and economic knowledge editor for @BusinessNewsroom.

Create a compact Telegram news card from the source article.

Primary topic:
{topic_hint}

Return ONLY valid JSON matching the schema.

PUBLIC CONTENT:
- Headline: 6-14 words, accurate, newspaper style.
- Summary: exactly ONE complete sentence, about 18-28 words.
- Highlights: 2-3 short factual points.
- No repetition between summary and highlights.
- No "..." or "…".
- Never end a headline or highlight with an ellipsis.
- No hashtags in generated fields.
- No Markdown or HTML in JSON fields.

WHAT TO KNOW:
- Return 1-3 genuinely useful knowledge points directly related to this news story.
- Use short terms, definitions, meanings, mechanisms, institutions, policies or concepts that help the reader understand the story.
- Use only information supported by the article or stable, directly relevant knowledge.

VOCABULARY:
- Return EXACTLY 3 important vocabulary words from the story.
- Each word must start with a capital letter.
- Each synonym and antonym must start with lowercase letters.
- Return exactly 2 synonyms and 2 antonyms for each word.
- Prefer useful business/economics vocabulary, not trivial words.

BOLD TERMS:
- Include important names, institutions, companies, figures, rates, percentages,
  policies and financial terms appearing in the generated headline, summary or highlights.

The public post will contain ONLY:
Photo, headline, one-line summary, Key Highlights, What to Know, Vocabulary,
hashtags and Source. Do not create context, why-it-matters, specialized, background,
market-context or any other top-level section.
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
            response = cerebras.chat.completions.create(
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
                        "name": "business_news_story_v04_1",
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
                clean_generated_text(
                    x
                )
                for x in data.get(
                    "highlights",
                    [],
                )
                if clean_generated_text(
                    x
                )
            ][:3]

            what_to_know = []
            for item_know in data.get("what_to_know", []):
                term = clean_generated_text(item_know.get("term"))
                meaning = clean_generated_text(item_know.get("meaning"))
                if term and meaning:
                    what_to_know.append({
                        "term": trim_source_text(term, 80),
                        "meaning": trim_source_text(meaning, 220),
                    })
            what_to_know = what_to_know[:3]

            vocabulary = []
            for vocab in data.get("vocabulary", []):
                word = safe_text(vocab.get("word"))
                synonyms = [safe_text(x) for x in vocab.get("synonyms", [])[:2] if safe_text(x)]
                antonyms = [safe_text(x) for x in vocab.get("antonyms", [])[:2] if safe_text(x)]
                if word and len(synonyms) == 2 and len(antonyms) == 2:
                    word = word[:1].upper() + word[1:]
                    synonyms = [x[:1].lower() + x[1:] for x in synonyms]
                    antonyms = [x[:1].lower() + x[1:] for x in antonyms]
                    vocabulary.append({
                        "word": trim_source_text(word, 50),
                        "synonyms": [trim_source_text(x, 50) for x in synonyms],
                        "antonyms": [trim_source_text(x, 50) for x in antonyms],
                    })
            vocabulary = vocabulary[:3]

            if (
                not headline
                or not summary
                or len(highlights) < 2
                or len(what_to_know) < 1
                or len(vocabulary) != 3
                or not complete_text(headline)
                or not complete_text(summary)
                or any(not complete_text(x) for x in highlights)
            ):
                raise ValueError("Incomplete story")

            story = {
                **item,
                "headline": trim_source_text(headline, 110),
                "summary": trim_source_text(summary, 260),
                "highlights": [trim_source_text(x, 130) for x in highlights],
                "bold_terms": [safe_text(x) for x in data.get("bold_terms", []) if safe_text(x)],
                "what_to_know": what_to_know,
                "vocabulary": vocabulary,
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

YEAR_RE = re.compile(
    r"^(?:19|20)\d{2}$"
)


def normalize_number(
    raw,
):
    text = (
        safe_text(raw)
        .lower()
        .replace(",", "")
        .replace("৳", "tk")
        .replace("$", "usd")
    )

    return re.sub(
        r"\s+",
        "",
        text,
    )


def numeric_tokens(text):
    tokens = []

    for match in NUMBER_RE.finditer(
        safe_text(text)
    ):
        token = safe_text(
            match.group(0)
        )

        stripped = re.sub(
            r"[^\d.]",
            "",
            token,
        )

        if (
            YEAR_RE.match(
                stripped
            )
            and not any(
                x in token.lower()
                for x in (
                    "tk",
                    "usd",
                    "bdt",
                    "$",
                    "€",
                    "£",
                    "¥",
                    "%",
                    "million",
                    "billion",
                    "crore",
                    "lakh",
                )
            )
        ):
            continue

        if token:
            tokens.append(
                token
            )

    return tokens


def numeric_grounded(
    story,
    article_text,
):
    source_numbers = [
        normalize_number(x)
        for x in numeric_tokens(
            article_text
        )
    ]

    generated_text = " ".join(
        [
            story.get(
                "headline",
                "",
            ),
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

    for token in numeric_tokens(
        generated_text
    ):
        normalized = normalize_number(
            token
        )

        if not normalized:
            continue

        # Require either exact normalized occurrence or a sufficiently
        # close numeric token from source.
        if normalized not in source_numbers:
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
        "<h1><b>" + escape_rich_html(story["headline"]) + "</b></h1>",
        "<p>" + bold_terms_html(story["summary"], terms) + "</p>",
        "<h2>Key Highlights</h2>",
        "<p>" + "<br>".join(
            "• " + bold_terms_html(point, terms)
            for point in story["highlights"]
        ) + "</p>",
    ]

    know_body = []
    for item in story.get("what_to_know", []):
        term = escape_rich_html(item.get("term", ""))
        meaning = bold_terms_html(item.get("meaning", ""), terms)
        know_body.append(f"<p><b>{term}:</b> {meaning}</p>")

    parts.append(
        "<details><summary>What to Know</summary>"
        + "".join(know_body)
        + "</details>"
    )

    vocab_lines = []
    for idx, item in enumerate(story.get("vocabulary", [])[:3], start=1):
        word = escape_rich_html(item.get("word", ""))
        synonyms = ", ".join(escape_rich_html(x) for x in item.get("synonyms", [])[:2])
        antonyms = ", ".join(escape_rich_html(x) for x in item.get("antonyms", [])[:2])
        vocab_lines.append(
            f"<p>{idx}. <b>{word}</b>: {synonyms} | {antonyms}</p>"
        )

    parts.append(
        "<details><summary>Vocabulary</summary>"
        + "".join(vocab_lines)
        + "</details>"
    )

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
        (260, 130, 3, 3, 220),
        (220, 115, 3, 3, 180),
        (190, 100, 2, 3, 150),
        (160, 85, 2, 2, 120),
    ]

    for summary_len, highlight_len, count, know_count, know_len in variants:
        candidate = dict(story)
        candidate["summary"] = trim_source_text(story["summary"], summary_len)
        candidate["highlights"] = [trim_source_text(x, highlight_len) for x in story["highlights"][:count]]
        candidate["what_to_know"] = [
            {
                "term": x["term"],
                "meaning": trim_source_text(x["meaning"], know_len),
            }
            for x in story.get("what_to_know", [])[:know_count]
        ]
        html_text = dynamic_rich_html(candidate)
        if rich_visible_length(html_text) <= MAX_RICH_CHARACTERS:
            return html_text

    return dynamic_rich_html(story)




# ============================================================
# IMAGE HANDLING: NO SOURCE/BRAND OVERLAY
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
    """Return the cropped news photo without adding any source/brand overlay."""
    return crop_cover(photo).convert("RGB")


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
            "Business News",
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
    """Keep a generous ranked recovery pool for downstream failures."""
    if not ranked:
        return []
    pool_size = max(RANKING_POOL_PER_REGION, needed * 2)
    return [dict(item) for item in ranked[:pool_size]]


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
        response = cerebras.chat.completions.create(
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
        "Bangladesh",
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
    return candidates[:MAX_RSS_CANDIDATES_PER_REGION]


def prepare_ranked_region(region, candidates):
    ranked = rank_candidates(candidates, region)
    ranked = collapse_event_clusters(ranked)
    persist_event_cluster_state(ranked)
    return ranked


def process_ranked_region(region, ranked):
    pool = build_candidate_pool(ranked, STORIES_PER_REGION)
    valid = []
    attempted = 0
    rejected = 0

    for item in pool:
        if len(valid) >= STORIES_PER_REGION:
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
        STORIES_PER_REGION,
        len(pool),
        attempted,
        rejected,
    )
    return valid


def run():
    logger.info("BUSINESSNEWSROOM V1 UPDATE-ONLY")
    logger.info("Channel=%s Mode=%s", TELEGRAM_CHANNEL, NEWS_MODE)
    logger.info("LOOKBACK=%d hours | %s -> %s", DISCOVERY_LOOKBACK_HOURS, DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat())

    prune_state()
    refresh_category_coverage()

    collect_rss()

    bd_count = queue_candidates_for_region("Bangladesh")
    intl_count = queue_candidates_for_region("International")

    # Free discovery first, then Exa only when a region is below the desired 24-hour candidate pool.
    bd_count += google_news_gap_fill("Bangladesh", bd_count, DISCOVERY_TARGET_PER_REGION)
    intl_count += google_news_gap_fill("International", intl_count, DISCOVERY_TARGET_PER_REGION)
    exa_gap_fill("Bangladesh", bd_count, DISCOVERY_TARGET_PER_REGION)
    exa_gap_fill("International", intl_count, DISCOVERY_TARGET_PER_REGION)

    save_state(STATE)

    bd_candidates = available_candidates("Bangladesh", source_pool="primary")
    intl_candidates = available_candidates("International", source_pool="primary")

    logger.info("DISCOVERY CANDIDATES: BD=%d INTL=%d TOTAL=%d", len(bd_candidates), len(intl_candidates), len(bd_candidates) + len(intl_candidates))

    ranked_bd = prepare_ranked_region("Bangladesh", bd_candidates)
    ranked_intl = prepare_ranked_region("International", intl_candidates)

    logger.info("UNIQUE EVENTS: BD=%d INTL=%d", len(ranked_bd), len(ranked_intl))

    for item in (ranked_bd[:8] + ranked_intl[:8]):
        logger.info(
            "RANK %s #%s | %s | %s",
            item.get("region", ""),
            item.get("editor_rank", "?"),
            item.get("title", ""),
            item.get("rank_reason", ""),
        )

    bd_stories = process_ranked_region("Bangladesh", ranked_bd)
    intl_stories = process_ranked_region("International", ranked_intl)

    stories = bd_stories + intl_stories
    logger.info(
        "FINAL: BD=%d/%d INTL=%d/%d TOTAL=%d/%d",
        len(bd_stories), STORIES_PER_REGION,
        len(intl_stories), STORIES_PER_REGION,
        len(stories), MAX_STORIES_PER_RUN,
    )

    if len(bd_stories) < STORIES_PER_REGION or len(intl_stories) < STORIES_PER_REGION:
        logger.warning(
            "Six-story target not reached. The bot exhausted the available valid candidates in one or both regions; no story is fabricated."
        )

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
        "headline": "Bangladesh Bank Cuts Repo Rate",
        "summary": "Bangladesh Bank cut the repo rate to support growth while monitoring inflation.",
        "highlights": [
            "The repo rate fell by 25 basis points.",
            "The move could lower bank borrowing costs.",
        ],
        "bold_terms": ["Bangladesh Bank", "repo rate", "25 basis points", "inflation"],
        "what_to_know": [
            {"term": "Repo Rate", "meaning": "The rate at which the central bank lends to eligible banks."},
            {"term": "CRR", "meaning": "Cash Reserve Ratio, the required reserve portion of deposits."},
        ],
        "vocabulary": [
            {"word": "Merger", "synonyms": ["combination", "consolidation"], "antonyms": ["split", "division"]},
            {"word": "Profit", "synonyms": ["gain", "earnings"], "antonyms": ["loss", "deficit"]},
            {"word": "Debt", "synonyms": ["liability", "borrowing"], "antonyms": ["asset", "surplus"]},
        ],
        "source": "The Business Standard",
        "url": "https://example.com/story",
        "region": "Bangladesh",
        "topic": "Banking",
        "institution": "Bangladesh Bank",
    }

    rendered = dynamic_rich_html(sample)

    assert complete_text("A normal sentence.")
    assert complete_text("An incomplete sentence—") is False
    assert "Key Context" not in rendered
    assert "Why It Matters" not in rendered
    assert "<details><summary>What to Know</summary>" in rendered
    assert "<details><summary>Vocabulary</summary>" in rendered
    assert "<details>" in rendered
    assert "<b>Merger</b>: combination, consolidation | split, division" in rendered
    assert "#Banking" in rendered
    assert "• " in rendered
    assert "@BusinessNewsroom" not in rendered
    assert likely_same_event(
        "Fed cuts rates by 25 basis points",
        "Fed cuts rates by 25 basis points",
    )
    assert canonical_url("https://www.example.com/story/?utm_source=x") == "example.com/story"
    assert "bangladesh" in extract_entities("Bangladesh Bank cuts the repo rate")
    clustered = cluster_ranked_events([
        {"title": "Bangladesh Bank cuts repo rate", "source": "TBS", "url": "https://tbsnews.net/a", "published_date": now_iso(), "region": "Bangladesh"},
        {"title": "Central bank lowers repo rate", "source": "Reuters", "url": "https://reuters.com/a", "published_date": now_iso(), "region": "Bangladesh"},
    ])
    assert len(clustered) >= 1
    assert clustered[0]["event_cluster_size"] >= 1
    logger.info("BusinessNewsroom V1 self-test passed.")

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

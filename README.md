# TheTechNewsroom V1

> Event-centric technology news intelligence for Telegram, powered by GitHub Actions, Exa, and Cerebras.

TheTechNewsroom discovers, normalizes, filters, groups, ranks, verifies, and publishes important technology news to **@TheTechNewsroom**. The system treats the underlying **event** as the editorial unit rather than treating every article as a separate story.

## Editorial Mission

The audience is everyday technology users. The channel prioritizes technology news with real and direct consumer or broad industry impact:

- AI models, products, launches, breakthroughs, and major strategic moves
- Major smartphone, operating-system, browser, search, social, cloud, and app-store changes
- Major cybersecurity and privacy incidents
- Major outages affecting widely used technology services
- Major technology-company strategic moves
- Important new consumer technology products
- Major technology-industry shifts
- Genuine new capabilities in trending GitHub repositories
- Startups reaching unicorn status or shipping products with broad real-world impact
- Major Y Combinator product launches or milestones
- Major Hugging Face open-model releases or meaningful state-of-the-art shifts

Category membership does not automatically make a story important. The same significance standard applies across all categories.

## Source Policy

The configured technology publication universe includes:

- TechCrunch
- The Verge
- WIRED
- Ars Technica
- Engadget
- MIT Technology Review
- Hacker News
- VentureBeat
- Techmeme
- TechRadar
- ZDNET
- 9to5Google
- WABetaInfo
- TestingCatalog
- AI News
- Unite.AI
- The Decoder
- SiliconANGLE
- Android Authority
- MacRumors

**Techmeme and Hacker News are discovery/corroboration sources, not preferred final article sources.** The bot does not favor a publication simply because it is in the source list. When multiple sources cover the same event, the system prefers the strongest available reporting source while retaining corroboration information.

Google News RSS and Exa are used for gap-fill discovery within the allowed technology source universe.

## Discovery

The current discovery flow is:

```text
RSS feeds
   ↓
Google News RSS gap fill
   ↓
Exa gap fill
   ↓
Canonical URL normalization
   ↓
72-hour eligibility window
   ↓
Deterministic editorial prefilter
   ↓
Published-event duplicate check
   ↓
Event clustering / deduplication
   ↓
Structured LLM ranking in bounded batches
   ↓
Global event ranking
   ↓
Soft sector/source diversity ordering
   ↓
Article extraction
   ↓
Story generation
   ↓
Numeric + claim verification
   ↓
Image recovery / branding
   ↓
Telegram publication
   ↓
Persistent state + journal + run report
```

Freshness is a ranking signal, not a hard 24-hour admission gate:

- Under 24 hours: strongest freshness signal
- 24–72 hours: acceptable
- Over 72 hours: normally lower priority unless highly significant or still developing
- Unknown age: not automatically rejected

## Deterministic Prefilter

Obvious low-value genres are rejected before expensive LLM ranking when their metadata clearly identifies them as:

- Reviews, hands-ons, first looks, or unboxings
- Rumors, leaks, speculation, or unreleased-product reporting
- Vehicle/EV/robotaxi and related fleet or charging stories
- HealthTech, biotech, medtech, medical, clinical, or pharmaceutical news
- Energy, utilities, grid, and routine battery-market stories
- Routine startup/VC/finance news
- Low-level engineering deep dives aimed at engineers
- Podcasts, webinars, event recordings, and roundtables
- Opinion/commentary without a concrete new event
- Minor app features, routine patches, bug fixes, and incremental updates

Exceptional industry-changing events may use an editorial escape hatch rather than being rejected solely from a keyword.

## Event-Centric Duplicate Protection

Articles are first normalized and then grouped around the underlying event.

Duplicate checks include:

1. Canonical URL identity
2. Permanent published URL history
3. Published event-cluster identity
4. Exact normalized headline hash
5. Article-content hash
6. Same event key with topic/entity agreement
7. Strong cross-source headline similarity
8. Conservative topic + institution + entity similarity
9. Very strong article-content similarity

The same event reported by another publication should normally become corroboration, not another Telegram post.

Follow-up coverage is allowed only when it represents a materially new development, such as:

- A stage change
- A meaningful scope change
- A significant magnitude change
- A new actor taking action
- A reversal
- A newly confirmed consequence

A different URL or different wording alone does not make an old event new.

## Scoring

The ranking system uses a structured **0–100** score. The LLM provides the components and the program validates and recomputes the total.

| Axis | Range |
|---|---:|
| Impact | 0–30 |
| Reach | 0–25 |
| Novelty | 0–20 |
| Certainty | 0–15 |
| Durability | 0–10 |
| **Total** | **0–100** |

Score interpretation:

```text
85–100  Exceptional
75–84   Major
65–74   Clearly important
55–64   Borderline
Below 55  Reject
```

### Publication threshold

**Score >= 65** is the publication threshold.

The following rules are enforced:

- Thin metadata cannot receive a publishable score.
- A famous company does not automatically make an event important.
- Trending/corroboration cannot rescue an otherwise weak story.
- Duplicate/repetitive coverage is rejected.
- The final score is recomputed from the structured components rather than blindly trusting an LLM total.
- Sector diversity is a soft ordering preference only. It is never a category quota and never lowers the importance threshold.

## Unlimited Qualifying Publication

There is **no per-run post-count limit**.

If the run produces:

```text
3 qualifying stories  → publish 3
10 qualifying stories → publish 10
20 qualifying stories → publish 20
```

The bot does not suppress an important story simply because a fixed post quota has been reached.

The only editorial/technical gates are quality, duplicate protection, successful generation, verification, and successful Telegram delivery.

## Telegram Post Format

The public post follows this structure:

```text
PHOTO

HEADLINE

One-sentence summary.

KEY HIGHLIGHTS
• Point one
• Point two
• Point three

THE CONTEXT
2–4 sentences of background, collapsed by default.

BOTTOM LINE
One-sentence "so what", collapsed by default.

#AI #OpenAI
Source: The Verge
```

### Content rules

- Headline: 6–14 words
- Summary: exactly one sentence
- Highlights: 3–5 factual bullets
- The Context: 2–4 sentences
- Bottom Line: exactly one sentence
- Hashtags: relevant to the actual story
- Source: publication name
- No unsupported numbers or claims
- Markdown artifacts such as `**`, `__`, and backticks are sanitized before HTML rendering
- `THE CONTEXT` and `BOTTOM LINE` use Telegram expandable blockquotes

## Image Pipeline

### Normal article image

The bot attempts to recover and use the article's actual image.

Image candidates can come from:

- RSS image metadata
- `og:image`
- Twitter image metadata
- JSON-LD image data
- Lazy-loaded image attributes
- `srcset`
- Preload/image metadata

The image is formatted for the Telegram card.

The article image must **not** receive the channel name in the upper-left corner.

The current branding keeps:

```text
@TheTechNewsroom
```

at the bottom-right.

### Missing article image

Fallback order:

```text
Article image unavailable
        ↓
Try source website logo
        ↓
Logo found → large centered source logo
        ↓
Logo unavailable → source name in bold at center
        ↓
@TheTechNewsroom remains bottom-right
```

The fallback uses a contrast-aware background so the source logo remains visible.

## Verification

Every generated story passes verification before publication:

1. **Numeric grounding**
   - Checks generated numbers against the source.
   - Equivalent representations such as `$1B` and `$1 billion` are treated as the same value.

2. **Claim verification**
   - Checks the headline, summary, and highlights against the extracted article.

Failed verification causes the candidate to be rejected rather than publishing unsupported information.

## Persistent State

The repository uses:

```text
news_state.json
posted_urls.txt
```

plus the event-centric operational directories:

```text
data/
├── journal/
└── reports/
```

### State policy

- Detailed operational queue/event data is retained for the configured 15-day operational window.
- Published-history information is retained for long-term duplicate protection.
- Published history stores compact identifying information rather than full article bodies.
- `posted_urls.txt` stores timestamped URL history used for duplicate protection and cleanup.
- `data/journal/` contains append-only compact run/event records.
- `data/reports/` contains per-run diagnostics.

## Run Diagnostics

Each run records metrics such as:

```text
Discovered
Ranked
After event deduplication
Importance pass
Verification results
Published count
Score distribution
```

The journal and reports are designed to make discovery, ranking, duplicate, and publication problems diagnosable from GitHub Actions logs and repository state.

## GitHub Actions

Workflow location:

```text
.github/workflows/newbot.yml
```

The workflow:

1. Checks out the repository
2. Sets up Python 3.12
3. Installs `requirements.txt`
4. Runs `python -m py_compile main.py`
5. Runs `python main.py --self-test`
6. Runs `python main.py`
7. Commits changed state under `news_state.json`, `posted_urls.txt`, and `data/`

The workflow uses concurrency protection so a scheduled run and a manual run cannot execute against the same state simultaneously.

## Required GitHub Secrets

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

Optional:

```text
TELEGRAM_ADMIN_CHAT_ID
CEREBRAS_MODEL
```

The workflow sets:

```text
TELEGRAM_CHANNEL=@TheTechNewsroom
NEWS_MODE=update
```

## Repository Structure

```text
TheTechNewsroomBot/
├── .github/
│   └── workflows/
│       └── newbot.yml
├── data/
│   ├── journal/
│   │   └── .gitkeep
│   └── reports/
│       └── .gitkeep
├── README.md
├── main.py
├── news_state.json
├── posted_urls.txt
├── requirements.txt
└── .gitignore
```

`.gitkeep` files are only Git placeholders for empty directories. They contain no executable code.

## Local Checks

Compile:

```bash
python -m py_compile main.py
```

Self-test:

```bash
python main.py --self-test
```

The self-test covers the editorial score calculation, threshold behavior, duplicate/event handling, canonical URLs, Telegram rich-text structure, Markdown sanitization, image fallback hooks, and unlimited qualifying-story selection.

## Operating Principle

The core rule is simple:

> **Find genuinely important technology events, verify them, avoid publishing the same event twice, and publish every qualifying story.**

The system should prefer accuracy and significance over volume, but it must never use an arbitrary post-count limit to suppress a story that clears the editorial bar.

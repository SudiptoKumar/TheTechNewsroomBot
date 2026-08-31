# TheTechNewsroom V3

> Automated tech news intelligence for Telegram, powered by GitHub Actions, Exa, and Cerebras, with batched editorial ranking and soft sector diversity.

TheTechNewsroom discovers, filters, ranks, verifies, and publishes the most important technology stories to the Telegram channel **@TheTechNewsroom**. All eligible stories compete in one ranked pool, and the bot publishes the strongest available stories rather than forcing weak category quotas.

## Editorial Mission

The channel is written for **everyday technology users**, not engineers or industry insiders. It prioritizes stories with real, direct impact on people's lives:

- Everyday consumer technology and how it changes
- AI models, tools, and products
- Smartphone and OS news: releases, updates, platform-level changes (not reviews, hands-on impressions, or rumors)
- Major online platforms (search, social, cloud, app stores)
- Cybersecurity and privacy incidents
- Major technology companies and their strategic moves
- Genuinely important new products
- Major shifts in the technology industry
- Trending GitHub repositories (via trendshift.io), when the repo is a real new tool or capability, not routine repo churn
- Start-ups that reach unicorn status or ship something with broad real-world impact
- Y Combinator companies with a major product launch or milestone, not routine seed/pre-seed funding
- Hugging Face: major open-model releases or leaderboard shifts that meaningfully move the state of the art

A topic on this list makes a story **eligible** for coverage — it does not by itself make the story **important**. The same significance bar applies across every category above, including AI and cybersecurity.

## Ranked Story Pool

Every run scores all eligible candidates and publishes only the ones that clear the importance bar (score ≥ 7). Stories compete in a single ranked pool rather than fixed per-category quotas. The publisher uses a soft sector-diversity preference so strong runs can cover different areas such as AI, cybersecurity, platforms, GitHub, startups, and major industry moves, but it never lowers the importance bar or invents a sector quota.

## Primary Source Universe

The primary source universe contains 20 tech publications:

| Source | Domain |
|---|---|
| TechCrunch | `techcrunch.com` |
| The Verge | `theverge.com` |
| WIRED | `wired.com` |
| Ars Technica | `arstechnica.com` |
| Engadget | `engadget.com` |
| MIT Technology Review | `technologyreview.com` |
| Hacker News | `news.ycombinator.com` |
| VentureBeat | `venturebeat.com` |
| Techmeme | `techmeme.com` |
| TechRadar | `techradar.com` |
| ZDNET | `zdnet.com` |
| 9to5Google | `9to5google.com` |
| WABetaInfo | `wabetainfo.com` |
| TestingCatalog | `testingcatalog.com` |
| AI News | `artificialintelligence-news.com` |
| Unite.AI | `unite.ai` |
| The Decoder | `the-decoder.com` |
| SiliconANGLE | `siliconangle.com` |
| Android Authority | `androidauthority.com` |
| MacRumors | `macrumors.com` |

RSS is attempted first. Google News RSS and Exa provide gap-fill discovery using the same allowed tech domains.

## Editorial Ranking

Every candidate is ranked in bounded LLM batches, then merged into one global pool. The final score is recomputed from five structured dimensions rather than trusting a single model-generated number:

- Impact: 0-4
- Breadth: 0-2
- Novelty: 0-2
- Evidence: 0-1
- Freshness: 0-1

Total score = 0-10. Stories score 7 or higher only when the structured total meets the publication bar. Thin metadata is capped at 6. Trending is a supporting signal and cannot lift a sub-7 story into publication. Sector diversity is a soft tie-breaker among similarly scored candidates, never a quota.

```text
9-10  Extraordinary and rare
8     Strong, clearly important
7     Important and publishable
6     Interesting but below the publication bar
4-5   Niche or moderate value
0-3   Weak, repetitive, promotional, speculative, or excluded
```


## Content That Should Not Be Published

These normally score 0–3 unless the underlying event is genuinely industry-changing:

- Product reviews, hands-on impressions, first looks, or unboxings
- Rumors, leaks, or speculation about unreleased products ("in the works," "may launch," "expected in 202X")
- Car/EV/truck news, autonomous-vehicle and robotaxi rollouts, and related B2B fleet or charging deals
- Small EV/vehicle-space startup funding rounds
- HealthTech, biotech, and medtech startup news (funding, clinic openings, expansions, milestones)
- Medical, clinical, and pharmaceutical news of any kind — this is a technology channel, not a health channel
- Aircraft or rocket test flights/recovery, unless a genuine industry first
- Energy and utilities news: solar, wind, battery, grid infrastructure, climate/energy policy
- Home energy-storage and battery-market news, even consumer-framed
- Low-level engineering deep-dives (CPU/ISA design, compilers, kernels, protocols) aimed at engineers, not everyday users
- Podcasts, event recordings, webinars, roundtables
- Local/municipal politics, immigration and border-policy stories (unless tied to a major tech platform change)
- Crypto/blockchain legal or regulatory disputes with no everyday-user impact
- VC, finance, and legal-regulatory industry news, including routine startup funding announcements
- Minor feature additions to existing apps/platforms, including small fintech features
- Tech-policy opinion and commentary essays that aren't reporting a concrete new event
- Municipal license-plate-reader/surveillance-camera network stories (e.g. Flock funding, contract changes)
- Police-technology narrative features and profiles

## Duplicate & Already-Published Handling

- Exact canonical URLs are stored permanently in `posted_urls.txt`; common tracking/query variants, `www`, `/amp`, and `index.html` variants normalize to the same URL identity.
- `news_state.json` maintains a permanent `published_history` index containing published URL, normalized headline, topic, institution, event key, article-content hash, and a content excerpt. Published history is never retention-pruned.
- Duplicate protection runs before ranking, before generation, and after generation. It checks exact URL, exact normalized title, article-content hash, posted event-cluster ID, and conservative cross-source topic/event/entity similarity.
- Same-topic coverage is not rejected merely because it shares a category. It is rejected when the stored evidence indicates the same underlying story/event.
- Follow-up coverage may publish when it is materially a new event, even when it concerns the same company/topic.
- When multiple items describe the same event, prefer the most authoritative or original source.

## Recency & Trend Signals

- **Age < 24h** — strong freshness signal
- **Age 24–72h** — acceptable
- **Age > 72h** — normally lower priority unless still highly significant or developing
- **Age unknown** — not automatically penalized

`TRENDING` (reported by multiple distinct sources) is a supporting signal only — it must never on its own make a weak story important.

## Discovery Flow

```text
RSS feeds
   ↓
Google News RSS gap fill
   ↓
Exa gap fill
   ↓
Source validation
   ↓
24-hour filtering
   ↓
URL deduplication
   ↓
Event deduplication
   ↓
LLM editorial ranking in bounded batches (importance classifier)
   ↓
Global score merge + soft sector diversification
   ↓
Top tech events
   ↓
Article extraction
   ↓
Story generation
   ↓
Numeric grounding + claim verification
   ↓
Branded image
   ↓
Telegram Rich Message
   ↓
Persistent state
```

## Telegram Output Structure

```text
Photo
Headline
1-sentence news summary
## KEY HIGHLIGHTS
• Major fact
• Major fact
• Major fact
... (3-5 dynamically)
**THE CONTEXT** (collapsed by default)
2-4 sentences of background explaining how the story came about.
**BOTTOM LINE** (collapsed by default)
1 sentence takeaway, the "so what" of the story.
#hashtag #hashtag #hashtag
**Source:** [Publication]
```

### Content Rules

- Headline: 6–14 words, accurate and newspaper-style.
- Summary: exactly one complete sentence.
- Highlights: 3–5 concise factual points, chosen dynamically without padding or repetition.
- The Context: 2–4 complete sentences of relevant background, rendered as a collapsed Telegram expandable blockquote.
- Bottom Line: exactly 1 complete sentence stating the central takeaway, rendered as a collapsed Telegram expandable blockquote.

## Image Pipeline

The bot extracts an article image where possible, resizes/crops it to the 1200×675 card format, adds the publication/source name at the bottom-left, and keeps the `@TheTechNewsroom` brand chip at the bottom-right. The same source + channel branding is applied to the fallback tech-news card when no usable source image exists.

## Verification

Two verification passes run before publishing:

1. **Numeric grounding** — checks generated numeric facts against the source article.
2. **Claim verification** — checks the generated headline, summary, and highlights against the article.

Failed verification triggers regeneration or candidate rejection rather than unsupported publication.

## Scheduling

Defined by the included GitHub Actions workflow (`.github/workflows/`), which also supports manual runs. Set the cron schedule and timezone to match your posting cadence.

## Required Secrets

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

## Publishing Rule (V1)

There is no fixed number of posts per run. Publish every genuinely important, verified, non-duplicate story with an editorial score of 7 or higher. If 3 qualify, publish 3. If 20 qualify, publish 20. Never publish weak stories solely to reach a target, and never suppress a qualifying story solely because a post-count limit was reached.

## Local Checks

The editorial ranker processes candidates in batches of 15 to prevent structured-output truncation. Every qualifying story with score >= 7 competes in one global ranked pool. There is no per-run story-count cap: the bot attempts every qualifying, non-duplicate story and publishes every one that passes generation and verification. Sector diversity is a soft tie-breaker only.

Compile:

```bash
python -m py_compile main.py
```

Self-test:

```bash
EXA_API_KEY=dummy CEREBRAS_API_KEY=dummy TELEGRAM_BOT_TOKEN=dummy python main.py --self-test
```

Normal run:

```bash
python main.py
```


## V1 Event-Centric Ranking and State

- Discovery ingests up to 72 hours; freshness is a ranking signal, not a hard 24-hour gate.
- Articles are resolved into event clusters before editorial selection.
- Techmeme and Hacker News are discovery/corroboration sources, not favored publication sources.
- Final ranking uses a deterministic 100-point score: Impact 0-30, Reach 0-25, Novelty 0-20, Certainty 0-15, Durability 0-10.
- Publication threshold is 65/100.
- There is no per-run publication count cap. Every qualifying, verified, non-duplicate story may be published.
- Source diversity affects ordering only; it never removes a qualifying story.
- `data/journal/` stores compact append-only run/publish records.
- `data/reports/` stores compact per-run diagnostics.
- Operational URL/event data is retained for 15 days; permanent published fingerprints remain for duplicate protection.

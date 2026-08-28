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

Every candidate is scored 0–10 based on actual significance, not headline excitement, using metadata only (title, description, source, age, trending flag) — never outside knowledge or assumed context.

```text
9-10  Exceptional importance (rare — reserved for a handful of stories a week)
7-8   Clearly important
4-6   Interesting but usually not publishable (default band)
0-3   Low-value, repetitive, promotional, rumor/speculation, or niche
```

**9–10 — Exceptional importance.** Major frontier AI launches or breakthroughs; AI products affecting millions of users; massive breaches or critical widely-exploitable vulnerabilities; major outages affecting widely-used services (including partial/ongoing degradation of major AI providers like Claude, ChatGPT, Gemini); platform changes affecting hundreds of millions of users; industry-changing deals; major moves from companies like Apple, Google, Microsoft, OpenAI, Meta, or Amazon.

**7–8 — Clearly important.** Significant Android/iOS/Windows/Linux/browser updates; important AI model or product updates; major privacy or security changes; important new consumer products; significant cybersecurity incidents; major product launches; large user-base milestones; funding or M&A activity big enough to move a whole industry.

**4–6 — Interesting but not important.** The default band. Minor product announcements, small feature additions, routine software updates, developer-only changes, niche stories, a trending GitHub repo or benchmark with no larger story, or a funding/YC item with no major product attached. These are normally marked **not important**.

**0–3 — Low importance.** Minor updates and bug fixes, clickbait, unsupported rumors, opinion/promotional content, duplicate coverage, or stories with no meaningful technological impact.

A story is publishable only when its score is **≥ 7**. When in doubt between two scores, choose the lower one — skipping a weak story is safer than publishing one.

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

- A "Recently published" list of prior headlines is checked on every run. Follow-up coverage of the same event, product launch, company milestone, or outage — even from a different source, with different numbers or wording — is marked **not important**.
- When multiple items describe the same event, judge the underlying significance rather than treating repetition itself as importance, and prefer the most authoritative or original source.

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
## WHY IT MATTERS
2-4 sentences of editorial context.
[WHAT'S NEXT appears inside a collapsed-by-default block]
#hashtag #hashtag #hashtag
**Source:** [Publication]
```

### Content Rules

- Headline: 6–14 words, accurate and newspaper-style.
- Summary: exactly one complete sentence.
- Highlights: 3–5 concise factual points, chosen dynamically without padding or repetition.
- Why It Matters: 2–4 complete sentences of editorial context.
- What's Next: 1–2 sentences on what readers should watch, rendered as a collapsed Telegram expandable blockquote.

## Image Pipeline

The bot extracts an article image where possible, resizes/crops it to the 1200×675 card format, adds the `@TheTechNewsroom` brand chip, and falls back to a generated tech-news card when no usable source image exists.

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

## Local Checks

The editorial ranker processes candidates in batches of 15 to prevent structured-output truncation. The run keeps a recovery pool of up to 24 important candidates and tries candidates sequentially until six verified stories are produced or the eligible pool is exhausted. Six is a maximum, not a quota.

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

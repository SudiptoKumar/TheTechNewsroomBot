# TheTechNewsroom V1

> Automated, event-centric tech news intelligence for Telegram, powered by GitHub Actions, Exa, and Cerebras.

The bot discovers technology news, normalizes and filters articles, resolves them into underlying events, ranks events, verifies generated stories, and publishes every qualifying important story to **@TheTechNewsroom**. There is no fixed post-count cap.

## Editorial Mission

The channel is for everyday technology users. Priority coverage includes consumer technology, AI models/tools/products, smartphones and operating systems, major online platforms, cybersecurity/privacy, major tech companies, genuinely important products, major industry shifts, useful new GitHub projects, meaningful startup/unicorn milestones, major Y Combinator product launches, and important Hugging Face model releases.

A category makes a story eligible for consideration, not automatically important. Reviews, rumors/leaks, most EV/vehicle news, medical/biotech, energy, podcasts, opinions, routine funding, routine feature updates, and low-level engineering content are normally rejected unless the underlying event is genuinely industry-changing.

## Event-Centric Pipeline

```text
RSS / Google News / Exa
        ↓
canonical URL normalization
        ↓
cheap deterministic prefilter
        ↓
candidate corroboration scan
        ↓
batched LLM ranking
        ↓
100-point global ranking
        ↓
event clustering / cross-source dedup
        ↓
already-published event guard
        ↓
article extraction
        ↓
story generation
        ↓
numeric grounding
        ↓
claim verification
        ↓
image recovery + source branding
        ↓
publish every verified score>=65 story
        ↓
append-only journal + run report
```

The ranking unit is the underlying event, not a publication article. Multiple sources covering the same event should produce one publishable event, with corroboration improving certainty rather than creating multiple posts.

## Discovery and Recency

- Ingest window: **72 hours**.
- Under 24 hours: strongest freshness signal.
- 24-72 hours: acceptable and still rankable.
- Older/developing stories should normally re-enter only when materially updated.
- Unknown publication age is not automatically rejected, but low-confidence metadata cannot earn a high score.

RSS remains the primary source path. Google News RSS and Exa are recall/gap-fill sources and are normalized into the same event pool.

## 100-Point Editorial Score

Each candidate receives:

```text
Impact       0-30
Reach        0-25
Novelty      0-20
Certainty    0-15
Durability   0-10
-----------------
Total        0-100
```

Bands:

```text
90-100  Extraordinary
80-89   Major
70-79   Strongly important
65-69   Borderline publishable
55-64   Interesting but below bar
0-54    Reject
```

`important=true` only when the computed total is **65 or higher**. The program recomputes the total from the returned components instead of trusting the model's raw score.

Thin metadata is capped below the publication threshold. Trending/corroboration can improve certainty, but cannot rescue a weak story across the 65 bar by itself. A famous company does not make a weak event important.

## Duplicate Protection

Duplicate protection is event-oriented and runs before ranking, before generation, and after generation. It checks:

- canonical URL identity
- existing published URL history
- exact normalized headline hash
- exact article-content hash
- previously published event/cluster IDs
- same-topic event-key + title/entity agreement
- strong cross-source title/entity similarity

The same underlying event is treated as one event even when the outlet, wording, or numbers differ. A genuinely new development can pass as a new event when it changes scope, stage, magnitude, actor, consequence, or reverses a prior decision.

Published history is permanent but compact. Full temporary queue/event data is retained for **15 days**. `posted_urls.txt` is an operational 15-day index; permanent protection comes from the compact published-history fingerprints.

## State and Diagnostics

```text
news_state.json
  temporary queue/events: 15-day retention
  published_history: compact permanent fingerprints

posted_urls.txt
  timestamped URLs from the most recent 15 days

news_journal_YYYY-MM.jsonl
  append-only run/publish audit trail

run_reports/
  one JSON report per run
```

The journal records discovery, publish reservation, publish success, and publish failure events. Run reports expose candidate volume and publication outcomes so ranking/dedup/verification issues are diagnosable from logs.

## Telegram Output

```text
Photo

HEADLINE

One-sentence summary.

KEY HIGHLIGHTS
• Fact
• Fact
• Fact

THE CONTEXT  (collapsed by default)
Background and relevant history.

BOTTOM LINE  (collapsed by default)
One-sentence takeaway.

#AI #OpenAI
Source: The Verge
```

The image pipeline uses the best recoverable article image. If unavailable, it tries the publication logo. If the logo also fails, it uses a large centered source name. `@TheTechNewsroom` remains bottom-right on fallback cards.

## Publishing Rule

There is **no per-run post limit**. Every genuinely important, non-duplicate story that reaches the publication bar and passes verification can be published. If 3 qualify, publish 3. If 20 qualify, publish 20.

The bot may pace the verified queue between sends to reduce burst flooding, but pacing never changes editorial eligibility.

## Optional Shadow Mode

Set:

```text
NEWS_MODE=shadow
```

The pipeline runs discovery, ranking, deduplication, extraction, generation, and verification, writes its run report, and sends no Telegram posts. Production uses `NEWS_MODE=update`.

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

## Local Checks

```bash
python -m py_compile main.py
python main.py --self-test
python main.py
```

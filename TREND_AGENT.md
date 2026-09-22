# Trend Analysis Agent

## 1. Purpose

This agent analyzes temporal trends in an already-clean, structured, longitudinal social-media cybersecurity dataset.

It does **not** handle data collection, scraping, APIs, legal access, database setup, preprocessing, Grafana, Prometheus, or infrastructure.

Its responsibilities are to:

- detect topics, entities, keywords, and narratives that are changing over time;
- distinguish popularity from genuine temporal change;
- compare recent activity with historical baselines;
- identify emerging, accelerating, declining, recurring, stable, and anomalous patterns;
- retrieve evidence for detected trends;
- produce evidence-grounded explanations without inventing causes.

Core principle:

```text
statistical / temporal analysis
        ↓
candidate trend
        ↓
evidence retrieval
        ↓
semantic interpretation
        ↓
final explanation
```

The LLM should interpret trends, not decide them solely from subjective reading.

---

## 2. Operational Definition of a Trend

A **trend** is a meaningful temporal change in the prevalence, participation, engagement, semantic framing, or recurrence of a topic, entity, keyword, phrase, or narrative relative to an appropriate historical baseline.

A trend is **not** simply something with high absolute volume.

For example:

- A topic with 1,000 posts every week may be popular but stable.
- A topic rising from 20 to 150 posts in a few days may be accelerating.
- A topic that was absent historically and suddenly appears may be emerging.
- A topic that jumps for one day and immediately disappears may be a spike rather than a sustained trend.

The system should support the following trend types:

- **Emerging** — previously rare or absent, now appearing with meaningful volume.
- **Accelerating** — growth rate is increasing over consecutive periods.
- **Sustained** — elevated activity persists across multiple periods.
- **Stable High-Volume** — consistently popular but not meaningfully changing.
- **Declining** — prevalence or participation is decreasing relative to baseline.
- **Spiking** — sharp short-lived increase without persistence.
- **Recurring** — a topic reappears after a period of inactivity.
- **Uncertain** — evidence is insufficient or contradictory.

---

## 3. Expected Input Schema

The input dataset is assumed to be clean and longitudinal.

Possible fields:

```text
post_id
timestamp
text
author_id
source
language
topic_id
topic_label
entities
keywords
urls
engagement metrics
embedding
relevance scores
```

Minimum fields needed for temporal trend analysis:

```text
post_id
timestamp
text
```

Recommended additional fields:

```text
author_id
topic_id
entities
keywords
source
engagement metrics
urls
embedding
```

---

## 4. Units of Analysis

The agent should support trend analysis at several levels.

### 4.1 Topic level

Examples:

- ransomware
- phishing
- AI-enabled scams
- hospital cyberattacks
- foreign interference

### 4.2 Entity level

Examples:

- companies
- government agencies
- organizations
- CVE identifiers
- malware families
- threat actors
- locations

### 4.3 Keyword / phrase level

Examples:

- `voice cloning`
- `CRA scam`
- `zero-day`
- `credential theft`

### 4.4 Narrative / semantic level

Detect when a topic remains active but its framing or subtopics change.

Example:

```text
phishing discussion

past:
email + password + login

recent:
AI + voice + deepfake + phone
```

---

## 5. Temporal Window Design

The system must support configurable temporal windows rather than hard-coded periods.

Conceptually distinguish three windows.

### Recent window

The period currently being evaluated.

Examples:

```text
last 24 hours
last 3 days
last 7 days
last 14 days
```

### Comparison window

The immediately preceding period used for direct comparison.

Examples:

```text
current 7 days vs previous 7 days
current 14 days vs previous 14 days
```

### Historical baseline

A longer period representing normal historical behavior.

Examples:

```text
previous 30 days
previous 8 weeks
all available historical data excluding the current window
```

Multiple time scales should be supported because they reveal different behaviors:

```text
24h  → breaking event
3d   → emerging signal
7d   → short-term trend
14d  → sustained trend
30d+ → historical context / recurrence
```

The system should also support event-centered windows:

```text
before event
incident period
after event
```

---

## 6. Core Trend Metrics

The first implementation should prioritize interpretable metrics.

### 6.1 Post volume

```text
volume(topic, window) = number of posts assigned to the topic in the window
```

Use for:

- basic activity measurement
- detecting volume changes

Limitations:

- sensitive to overall platform activity;
- susceptible to reposts or duplicated content;
- high volume does not itself imply a trend.

---

### 6.2 Topic share

```text
topic_share = topic_post_count / all_relevant_post_count
```

Example:

```text
Topic A:
100 → 200 posts

All posts:
1,000 → 3,000

Topic share:
10% → 6.7%
```

Although raw volume increased, the topic became less prominent relative to the overall conversation.

Use topic share to distinguish platform-wide growth from topic-specific growth.

---

### 6.3 Absolute growth

```text
absolute_growth = recent_count - baseline_count_adjusted
```

Useful when interpreting the actual scale of change.

---

### 6.4 Relative growth

```text
relative_growth = (recent_rate - baseline_rate) / max(baseline_rate, epsilon)
```

Rates should be normalized by time-window length when windows differ.

Use for:

- comparing growth across topics of different sizes.

Limitations:

- extremely unstable for very rare topics;
- requires a minimum-volume threshold.

---

### 6.5 Unique-author count

```text
unique_authors = number of distinct authors discussing the topic
```

Use to distinguish:

```text
500 posts from 450 users
```

from:

```text
500 posts from 20 users
```

A broad increase in distinct participants is stronger evidence of a distributed trend.

---

### 6.6 Unique-author growth

Compare distinct-participant counts between periods.

Useful for detecting whether increased volume represents broader participation.

---

### 6.7 Engagement

Possible measures:

```text
likes
replies
reposts
combined engagement
median engagement per post
```

Prefer robust statistics such as median alongside totals because a single viral post can dominate total engagement.

---

### 6.8 Unique URL count

Useful for identifying whether a trend reflects:

- one heavily repeated article;
- multiple independent information sources.

---

### 6.9 Moving average

Example:

```text
3-day moving average of daily topic volume
```

Useful for smoothing noisy daily counts and observing trajectory.

---

### 6.10 Acceleration

Measures whether growth itself is increasing.

Simple implementation:

```text
growth_t = value_t - value_t-1
acceleration_t = growth_t - growth_t-1
```

Positive acceleration across consecutive periods may indicate an increasingly important topic.

---

### 6.11 Volatility

Measure variation in recent activity.

Useful for distinguishing stable growth from erratic spikes.

Possible measure:

```text
std(daily_volume) / mean(daily_volume)
```

---

### 6.12 Anomaly score

Simple baseline:

```text
z = (current_value - historical_mean) / historical_std
```

Use for identifying unusual deviations from a topic's own historical behavior.

Avoid relying on z-score when historical observations are sparse or highly non-normal.

---

### 6.13 Novelty

Measures whether the topic has historically appeared.

Possible implementation:

```text
novelty = low historical prevalence + meaningful recent prevalence
```

Questions:

- Did the topic exist during the historical baseline?
- When was it last observed?
- Is this its first meaningful appearance?

---

### 6.14 Persistence

Measures whether elevated activity continues across multiple buckets.

Example:

```text
number of consecutive days above historical threshold
```

Important for separating:

```text
one-day spike
```

from:

```text
four-day sustained increase
```

---

### 6.15 Recurrence

A topic is recurring when:

```text
historically active
→ becomes inactive / returns to baseline
→ becomes active again
```

Track previous peaks and inactive periods.

---

## 7. Candidate Trend Detection

The first implementation should be rule-based and explainable.

Do not create an opaque composite score at the beginning.

A candidate trend may be flagged when multiple conditions are satisfied.

Example emerging-trend rule:

```text
recent_count >= minimum_volume
AND
relative_growth >= growth_threshold
AND
topic_share_change > share_threshold
AND
unique_author_growth > author_threshold
```

Example output:

```text
Topic: AI-enabled CRA scams

Flagged because:
- post count increased 182%
- topic share increased from 1.1% to 4.6%
- unique authors increased 205%
- activity remained elevated for 4 consecutive days
```

Every candidate trend must expose the metrics responsible for the flag.

---

## 8. Trend Classification Logic

### Emerging

Evidence:

- very low historical prevalence;
- meaningful recent volume;
- broader participation;
- recent appearance is not explained solely by duplication.

### Accelerating

Evidence:

- positive growth across multiple consecutive periods;
- increasing slope / acceleration;
- increasing share and/or participation.

### Sustained

Evidence:

- activity above baseline for multiple consecutive periods;
- persistence above threshold;
- not solely one-day viral activity.

### Stable High-Volume

Evidence:

- consistently high volume;
- little relative or share change;
- no major anomaly.

### Declining

Evidence:

- negative relative growth;
- decreasing topic share;
- decreasing participant count.

### Spiking

Evidence:

- sudden large increase;
- low persistence;
- rapid return toward baseline.

### Recurring

Evidence:

- historical peaks exist;
- recent reappearance after inactivity or normalization.

### Uncertain

Use when:

- sample size is too small;
- metrics disagree;
- one account dominates activity;
- historical baseline is insufficient.

---

## 9. Topic-Level Analysis

For each topic, calculate a temporal profile containing:

```text
volume
conversation share
unique authors
engagement
unique URLs
moving average
growth
acceleration
anomaly score
novelty
persistence
recurrence
```

The system should be able to answer:

- Is the topic becoming more common?
- Is it becoming more prominent relative to other topics?
- Are more people participating?
- Is activity sustained?
- Is it historically unusual?
- Is it genuinely new or recurring?

---

## 10. Entity-Level Analysis

Track entities independently of topics.

Entity types may include:

```text
organization
company
government agency
location
threat actor
malware family
CVE
software/product
```

For each entity, build time series for:

```text
mention count
unique-author count
engagement
co-occurring topics
co-occurring entities
conversation share
```

Example question:

```text
Which CVEs are becoming more prominent this week?
```

The same trend metrics used for topics can be applied to entity mentions.

---

## 11. Keyword and Phrase Trends

Raw word frequency alone should not define a trend.

Use several signals:

### Relative frequency

```text
phrase_count / all_terms_or_posts
```

### Temporal frequency change

Compare phrase prevalence between recent and historical windows.

### N-grams

Track multi-word expressions such as:

```text
voice cloning
CRA scam
remote access trojan
zero day
```

### Temporal TF-IDF style comparison

Words or phrases should receive higher importance when they are:

- frequent in the recent period;
- uncommon in historical periods.

### Co-occurrence changes

Track changes in terms appearing near or within the same posts.

Example:

```text
phishing

historical associations:
email, login, password

recent associations:
AI, voice, deepfake, phone
```

This can signal narrative change.

---

## 12. Semantic / Narrative Shift Analysis

This is an optional advanced layer.

The goal is to detect when discussion about the same broad topic changes meaning or framing.

Possible methods:

### Embedding centroid shift

Calculate the mean embedding for topic posts in two periods and compare cosine distance.

Large distance may indicate semantic change.

### Representative-post comparison

Select representative posts from historical and recent periods and compare their themes.

### Subtopic composition shift

Example:

```text
Topic: phishing

Historical:
70% email phishing
20% credential theft
10% phone scams

Recent:
25% email phishing
20% credential theft
55% AI voice scams
```

The parent topic remains stable while its internal composition changes.

### Co-occurrence shift

Compare top associated entities and phrases across periods.

### LLM interpretation

The LLM may summarize the observed semantic differences after quantitative or embedding-based evidence is available.

It must not infer unsupported causes.

---

## 13. Spike vs Sustained Trend

The system should explicitly distinguish spikes from persistent changes.

Example rules:

### Spike

```text
very high one-period increase
AND
activity returns close to baseline within N periods
```

### Sustained trend

```text
activity remains above baseline for >= N consecutive periods
```

### Accelerating trend

```text
positive growth across consecutive periods
AND
growth rate itself is increasing
```

### Noise

Potential indicators:

```text
very small sample
single-author dominance
single-URL dominance
one-period activity only
high duplicate ratio
```

---

## 14. Evidence Retrieval

Every trend explanation must be grounded in retrieved evidence.

For each detected trend, retrieve:

```text
recent representative posts
historical representative posts
most-engaged posts
earliest recent posts
top entities
top phrases
top URLs
unique-author distribution
time-series metrics
```

Evidence retrieval should prioritize diversity rather than returning many near-identical posts.

---

## 15. Trend Interpretation Agent

The LLM receives an already-detected candidate trend and its evidence bundle.

It should answer:

1. What is changing?
2. When did the change begin?
3. Is the trend new, recurring, or historically common?
4. Is activity broad-based or driven by a small number of users?
5. Is the increase temporary or persistent?
6. Which entities and phrases are associated with the change?
7. Are multiple subtopics contributing?
8. Is there evidence of semantic or narrative change?
9. What quantitative evidence supports the interpretation?
10. What uncertainty remains?

Preferred language:

```text
The data suggests...
The increase appears associated with...
The recent period shows...
A likely contributor within the observed dataset is...
The available evidence is insufficient to determine...
```

Avoid unsupported causal language such as:

```text
This happened because...
```

unless the dataset directly supports the claim.

---

## 16. Agent Tools

### `get_topic_timeseries`

Purpose:

Return a topic's temporal metrics.

Inputs:

```text
topic_id
start_time
end_time
bucket_size
```

Outputs:

```text
timestamp
post_count
topic_share
unique_authors
engagement
unique_urls
```

---

### `compare_windows`

Purpose:

Compare recent and historical periods.

Inputs:

```text
object_type: topic | entity | keyword
object_id
recent_window
baseline_window
```

Outputs:

```text
recent metrics
baseline metrics
absolute growth
relative growth
share change
unique-author growth
engagement change
```

---

### `get_top_trends`

Purpose:

Return candidate trends for a specified period.

Inputs:

```text
window
trend_types
filters
minimum_volume
```

Outputs:

```text
candidate trend objects with detection evidence
```

---

### `get_trend_metrics`

Purpose:

Return all calculated metrics for one candidate trend.

Inputs:

```text
trend_id
```

Outputs:

```text
volume
share
growth
authors
engagement
novelty
persistence
anomaly
```

---

### `get_representative_posts`

Purpose:

Return diverse supporting posts.

Inputs:

```text
topic_id or trend_id
window
count
selection_method
```

Selection methods may include:

```text
representative
most_engaged
earliest
latest
diverse
```

---

### `get_top_entities`

Purpose:

Return entities most associated with a topic or period.

Inputs:

```text
topic_id optional
window
entity_type optional
```

---

### `get_top_phrases`

Purpose:

Return important words or n-grams for a period or topic.

Inputs:

```text
topic_id optional
window
comparison_window optional
```

Outputs should distinguish:

```text
high-frequency
newly emerging
rapidly increasing
```

---

### `get_author_distribution`

Purpose:

Determine whether activity is broad-based or account-concentrated.

Outputs may include:

```text
unique authors
posts per author
share produced by top 1/5/10 authors
```

---

### `get_engagement_distribution`

Purpose:

Determine whether engagement is broadly distributed or driven by a few viral posts.

---

### `detect_anomalies`

Purpose:

Identify objects behaving unusually relative to historical baselines.

Inputs:

```text
window
object_type
historical_period
```

---

### `detect_semantic_shift`

Purpose:

Compare semantic content for the same topic between two windows.

Inputs:

```text
topic_id
window_a
window_b
```

Outputs may include:

```text
embedding shift
changed phrases
changed entities
changed subtopic distribution
representative examples
```

---

## 17. Output Schema

Example structured output:

```json
{
  "trend_id": "trend_001",
  "object_type": "topic",
  "object_id": "topic_12",
  "topic": "AI-enabled CRA phishing",
  "trend_type": "emerging",
  "analysis_window": {
    "start": "2026-09-17T00:00:00Z",
    "end": "2026-09-20T00:00:00Z"
  },
  "baseline_window": {
    "start": "2026-09-03T00:00:00Z",
    "end": "2026-09-16T23:59:59Z"
  },
  "metrics": {
    "recent_count": 142,
    "baseline_rate_adjusted_count": 18,
    "relative_growth": 6.89,
    "recent_topic_share": 0.049,
    "baseline_topic_share": 0.011,
    "unique_authors_recent": 103,
    "unique_author_growth": 4.1,
    "persistence_days": 4,
    "anomaly_score": 3.7
  },
  "detection_reasons": [
    "post volume increased substantially",
    "conversation share increased",
    "unique-author participation increased",
    "activity persisted across multiple days"
  ],
  "evidence": {
    "representative_posts": [],
    "top_entities": [],
    "top_phrases": [],
    "top_urls": []
  },
  "interpretation": "The data suggests a recent broad-based increase in discussion of AI-enabled CRA phishing scams.",
  "confidence": "medium",
  "limitations": []
}
```

---

## 18. Supported User Queries

The agent should support requests such as:

```text
What is trending right now?

What changed in the last 3 days?

Which topics are newly emerging?

Which topics are declining?

What is unusual compared with the past month?

Why is Topic X trending?

Is Topic X genuinely growing or experiencing a temporary spike?

Which entities are becoming more prominent?

Has the narrative around Topic X changed?

Compare Topic X this week with last week.

Show only sustained trends.

Show only emerging trends with broad user participation.

Which topics are historically popular but currently stable?
```

---

## 19. Guardrails

The agent must:

- distinguish popularity from temporal trend;
- compare against explicit historical baselines;
- distinguish spike from sustained growth;
- expose quantitative evidence for each detection;
- avoid treating a single viral post as broad-based activity;
- identify small-sample situations;
- identify concentration among a small number of users;
- account for duplicate/amplification effects when metadata is available;
- distinguish correlation from causation;
- avoid inventing external causes;
- express uncertainty when evidence is weak;
- make window definitions explicit;
- never hide detection logic behind unexplained LLM judgment.

---

## 20. MVP

The minimum useful implementation should include only:

### Temporal aggregation

```text
daily / configurable time buckets
```

### Topic and entity metrics

```text
post count
topic share
unique authors
engagement
```

### Window comparison

```text
recent vs comparison / historical baseline
```

### Simple trend signals

```text
absolute growth
relative growth
share change
unique-author growth
moving average
simple anomaly score
persistence
novelty
```

### Candidate classification

```text
Emerging
Accelerating
Sustained
Stable High-Volume
Declining
Spiking
Recurring
Uncertain
```

### Evidence retrieval

```text
representative posts
top entities
top phrases
```

### LLM interpretation

The LLM explains only trends already identified by the quantitative layer.

---

## 21. Advanced Extensions

Only after the MVP works:

- semantic shift detection;
- dynamic topic evolution;
- change-point detection;
- cross-topic relationships;
- diffusion analysis;
- cross-source comparison;
- event attribution;
- forecasting;
- adaptive window selection;
- learned trend ranking.

---

## 22. Implementation Order

### Phase 1 — Time aggregation

Implement reusable time-bucket functions for topics, entities, authors, and engagement.

### Phase 2 — Window comparison

Implement configurable recent, comparison, and historical windows.

### Phase 3 — Basic metrics

Implement:

```text
volume
share
growth
unique authors
engagement
```

### Phase 4 — Trend rules

Implement transparent detection for:

```text
emerging
accelerating
sustained
stable
spiking
declining
recurring
```

### Phase 5 — Persistence and anomaly

Add:

```text
moving average
z-score / simple anomaly
persistence
novelty
```

### Phase 6 — Evidence retrieval

Implement representative-post, entity, phrase, author, and URL evidence retrieval.

### Phase 7 — Interpretation agent

Allow an LLM to consume structured metrics and evidence and generate bounded explanations.

### Phase 8 — Semantic shifts

Add embedding/subtopic/co-occurrence-based narrative-change analysis.

### Phase 9 — Evaluation and tuning

Evaluate:

```text
window length
minimum volume
trend thresholds
persistence thresholds
anomaly thresholds
rare-topic behavior
```

---

## 23. Open Methodological Questions

The implementation should keep these configurable because they require experimentation:

- What is the optimal time bucket: hour, day, or week?
- Which recent-window lengths are most informative?
- Which historical baseline is appropriate?
- What minimum volume should be required before computing growth?
- How should rare topics be handled?
- How much weight should be given to conversation share?
- How much user concentration should invalidate a trend?
- How should viral but short-lived spikes be treated?
- What duration defines persistence?
- How should recurring topics be distinguished from genuinely new topics?
- Which anomaly method performs best?
- What semantic-shift threshold is meaningful?
- Should trends be ranked using explicit rules or a transparent weighted score?

These should be treated as research choices rather than hidden defaults.

---

## 24. Core Design Principle

The system should always preserve this separation:

```text
WHAT changed?
→ quantitative trend detection

HOW did it change?
→ temporal / distributional analysis

WHAT does the change appear to mean?
→ evidence-grounded semantic interpretation

WHY did it happen?
→ only state when supported by evidence; otherwise remain uncertain
```

The Trend Analysis Agent should therefore behave as an analytical layer over temporal data, not as a free-form summarization chatbot.

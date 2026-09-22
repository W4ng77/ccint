# ccint — Canada Cyber Social Intelligence

**Project wrap-up: design rationale, what was built, what it found, and what it cannot do.**

*Status as of 2026-09-21. Corpus window 2026-08-22 → 2026-09-21 (30 days).*

---

## 0. Summary

A longitudinal system that continuously collects Canada-related cybersecurity
discussion from social media, stores it as a timestamped corpus, detects
temporal change in it, and has an LLM agent produce evidence-grounded analysis
of what it finds.

| | |
|---|---:|
| Posts collected | 90,503 |
| Distinct authors | 26,810 |
| Canada × cyber relevant posts | 650 (0.72%) |
| Relevant authors | 372 |
| **Relevant posts per day** | **21.3** |
| Collection runs | 78 (6 backfill, 72 incremental) |
| Lines of code (src + tests + migrations) | 6,380 |
| Tests | 191 passing |

**The single most important result is a negative one.** After building the
statistical layer properly, no topic in the current window is distinguishable
from random noise — including the one that looks like `+56%` growth. Section 4.2
explains why that is the correct answer rather than a failure, and Section 4.3
quantifies exactly how much more data would be needed to change it.

---

## 1. Why start with Bluesky

### 1.1 The constraint that drives everything

This is a **longitudinal** study. That word has an operational consequence that
is easy to underestimate: *the corpus accrues with wall-clock time, and a day
not collected is a day permanently lost.* Social platforms delete posts, rotate
identifiers, and do not guarantee that historical search stays complete.

So the first question was not "which platform is best?" but **"which platform
can I start collecting from today?"** Every week spent evaluating platforms is a
week of corpus that does not exist. A perfect source chosen in month three
produces a shorter time series than an adequate source chosen in week one.

### 1.2 Selection criteria

| Criterion | Why it was disqualifying if absent |
|---|---|
| API access without an approval queue | Weeks of latency = weeks of missing corpus |
| Historical search with date bounds | Needed to seed a baseline on day 0 (§3.3) |
| Stable post + author identifiers | Required for deduplication and author-level analysis |
| Terms of service permitting research collection | Non-negotiable |
| Affordable at continuous polling rates | The collector must never stop |

### 1.3 How the candidates measured up

- **X / Twitter** — research API access is gated and priced well beyond this
  project. Rejected on access, not on data quality; it would likely be the
  richest source.
- **Reddit** — viable and still the strongest candidate for a second source.
  Canadian subreddits (r/canada, r/cybersecurity with Canadian threads) are
  genuinely discussion-shaped rather than broadcast-shaped. Deferred, not
  rejected — see §6.
- **Mastodon** — federated architecture means no global search endpoint. You
  can only search instances you federate with, which makes corpus completeness
  undefinable. Rejected on methodology.
- **Bluesky** — open AT Protocol, free, app-password authentication with no
  approval step, and `app.bsky.feed.searchPosts` accepts `since`/`until`
  bounds. Verified empirically that history is retrievable at least 3 months
  back.

### 1.4 The honest version of this choice

**Bluesky was chosen for API availability, not for representativeness.**

Its user base is skewed — tech-forward, disproportionately early-adopter,
significantly smaller than the platforms where most Canadians actually discuss
anything. Nothing measured here should be read as "what Canadians think about
cybersecurity." It is "what appears on Bluesky," and the gap between those two
is real and unmeasured.

This is stated in the limitations section of every report the system generates,
not just in this document. It is a design commitment, not a caveat added at
write-up time.

The measured consequence of the choice: **0.72% of everything collected is
relevant**, i.e. ~21 posts per day. That number is small enough to constrain
what the analytical layer can honestly claim, which turns out to be the
dominant finding of the project (§4.2–4.3).

---

## 2. Design principles

Five constraints were fixed before any code was written. Everything downstream
is a consequence of them.

### P1 — Raw data is never discarded

Every API response is stored verbatim in `raw_payload` (JSONB). Derived fields
are computed from it, never in place of it.

**Why:** every judgment this system makes will eventually be found wrong. If
the raw response is intact, being wrong costs a recomputation. If it is not, it
costs a re-collection — which for a longitudinal corpus may be impossible.

*The single exception:* the NUL character (U+0000), which PostgreSQL cannot
store in `text` or `jsonb`. It is stripped recursively and documented, because
it carries no semantics and cannot be represented at all.

### P2 — Judgments are versioned, never overwritten

Labels live in a separate `post_labels` table keyed by `label_version`.
Multiple versions coexist on the same posts.

**Why:** "is this post Canada-related?" is a definition, and definitions
evolve. Overwriting the old answer destroys the ability to ask *how* the
definition changed the results. Both `rules_v1` and `rules_v2` are currently in
the database and can be compared row by row — which is exactly how §4.1 was
measured.

### P3 — Every number carries provenance

Each report records `as_of`, window bounds, `label_version`, `query_version`,
`trend_method`, `null_method`, agent model, prompt version, and generation
timestamp. Every analysis run is persisted to `analysis_runs`.

**Why:** a number without provenance cannot be reproduced, and a number that
cannot be reproduced cannot be corrected.

### P4 — Time is always UTC

Enforced at three levels: the database is altered to UTC, every connection sets
`TimeZone=UTC`, and an assertion runs after each migration.

**Why:** this is not cosmetic. `date_trunc('day', timestamptz)` bucket
boundaries depend on the session timezone. A cluster that inherited
`America/Toronto` — which is what happened on first run — would silently bucket
by local day boundaries and produce 23- and 25-hour days across DST
transitions. Daily counts are the primary unit of analysis, so this would
corrupt every result invisibly.

### P5 — System failure must be distinguishable from social trend

`collection_runs` records mode, status, window bounds, fetch/insert counts,
error counts, and error detail for every run. Reports lead with a data-health
warning when the window contains failures or gaps.

**Why:** a collector that dies for ten hours produces a dip in volume that is
visually identical to a decline in public discussion. Without run-level
bookkeeping, the system cannot tell the difference — and will confidently
report the outage as a finding. This happened (§5.1) and the bookkeeping is what
caught it.

---

## 3. What was built

### 3.1 The central design decision: collect broad, judge narrow

**The collector searches 34 cybersecurity terms. Zero of them mention Canada.**

```
25 English : ransomware, phishing, data breach, cyberattack, cybersecurity,
             malware, infostealer, zero-day, ddos, hacked, data leak, CVE,
             credential stuffing, spyware, botnet, threat actor, ...
 9 French  : cybersécurité, rançongiciel, hameçonnage, fuite de données,
             cyberattaque, logiciel malveillant, violation de données, ...
```

Everything is stored. Canada relevance is decided **afterwards**, offline,
against the stored corpus:

```
Bluesky API ──34 cyber terms──▶ 90,503 posts (global cyber discussion)
                                      │  all stored, nothing filtered
                                 posts table
                                      │  offline rule pass — no network
                              post_labels (versioned)
                                      ▼
                                 650 relevant (0.72%)
```

**Rationale — a deliberate asymmetry in reversibility:**

| | If this judgment is wrong |
|---|---|
| Collection filter (cyber terms) | **Irreversible.** Posts not fetched cannot be recovered. |
| Relevance filter (Canada terms) | **Reversible.** Raw payload is intact; recompute. |

Cybersecurity vocabulary is stable enough to serve as a collection boundary.
"What counts as Canada-related" is precisely the definition most likely to
change — so it belongs on the recomputable side.

**This was vindicated three times in one day.** Three separate errors were found
in the Canada rules, and *none* required re-collection:

| Discovery | Impact |
|---|---|
| `GTA` = **Grand Theft Auto**, not Greater Toronto Area | 18.1% of "relevant" was video-game news |
| `CRA` = EU **Cyber Resilience Act**, not Canada Revenue Agency | part of a 15.4% abbreviation error rate |
| `CSIS` = csis.org, a **US think tank** | same |

Had "canada" been used as a *search* term, these errors would have been frozen
into the corpus permanently — the correctly-relevant posts that the flawed
query never retrieved simply would not exist to recover.

**The cost is 139× storage overhead.** 89,853 of 90,503 posts are irrelevant and
kept anyway. Disk is cheap; lost history is not.

A second-order consequence worth noting: this design means **the Canada
definition can be upgraded from rules to a classifier, or to entity linking,
without touching collection at all.** The rule-based labeler is a starting
point, not an architectural commitment.

### 3.2 Why `GTA` is instructive beyond its own fix

The obvious fix for an ambiguous abbreviation is "require cybersecurity context
nearby." That fix **does not work here**, and understanding why shaped the
final design.

`CRA`, `GRC`, `CSE`, `CSIS` are ambiguous *between two cybersecurity meanings*:

- `CRA` — Canada Revenue Agency **vs.** EU Cyber Resilience Act
- `GRC` — Gendarmerie royale du Canada **vs.** Governance / Risk / Compliance
- `CSIS` — Canadian Security Intelligence Service **vs.** csis.org (US)

Requiring cyber context is satisfied by *both* readings, so it discriminates
nothing. The working mechanism is **corroboration**: a weak abbreviation only
counts if an independent *Canada-side* signal also fires. Implemented as
`requires_corroboration` on weak abbreviation groups, with `RCMP` kept as the
sole strong abbreviation.

Measured effect: `is_relevant` precision **56.0% → 86.3%**, recall
**85.6% → 91.7%** (§4.1).

### 3.3 Two collection modes, and why they are separate

| | `backfill` | `incremental` |
|---|---|---|
| Cadence | one-off, manual | **every 15 minutes** (cron) |
| Window per run | a full history span (30 days used) | **30 minutes** |
| Internal structure | **chunked by day**, 34 terms per day | single window, 34 terms |
| Runs to date | 6 | 72 |
| Mean collection lag | **354 h** | **4 h** |
| Mean likes observed | **3.59** | **1.50** |

**Why `incremental` exists:** the corpus accrues with wall-clock time. A stopped
collector loses that day permanently. This is the component that must never
stop.

**Why `backfill` exists:** trend detection compares two windows. With 7-day
windows and only incremental collection, the first report would be impossible
until day 14. Backfill gives the trend layer a baseline on day 0. In practice
the first report was produced the same night collection started.

**Why day-chunking is mandatory, not an optimization:** `searchPosts` returns
results in reverse-chronological order and pages backwards. Once a page limit
is hit, **the truncated portion is always the older dates**. Paging naively over
a 30-day span systematically under-samples the past — which surfaces in the
trend layer as a clean upward slope that is entirely an artifact of collection.
Day-chunking gives each day an independent page budget. *(Side benefit: process
RSS dropped from 0.5 GB to 4 MB, because each chunk flushes to the database.)*

**Why they are two modes rather than one mechanism:** their sampling properties
genuinely differ. The 2.4× gap in observed likes above is **pure measurement
artifact** — `like_count` is a snapshot taken at collection time, and backfill
posts had two weeks to accumulate engagement while incremental posts had four
hours. A system that did not distinguish the modes would report "last week was
2.4× more engaging" as a social finding. `collection_runs.mode` makes the
distinction auditable, and reports disclose the mix.

**Self-healing:** the incremental window is computed from the last successful
checkpoint, not from `now - 30min`. After a 10-hour outage, the next run's
window is automatically 10 hours wide. The 15-minute overlap (deliberate, and
harmless because `post_uri` is a primary key) catches posts that enter the
search index late.

### 3.4 Pipeline

```
  collect ──▶ posts (raw_payload intact)
                 │
              label ──▶ post_labels  (versioned; offline; no network)
                 │
         ┌───────┴────────┐
    detect_trends    null model          ← window comparison, then
   (window_compare)  (permutation)         significance testing
         └───────┬────────┘
            broadcast filter              ← who is posting, and is anyone listening
                 │
          LLM agent (tool use)            ← reads actual posts; every cited
                 │                          post_id verified against tool output
              report.md                   ← provenance + health + stats +
                                            significance + agent narrative
```

### 3.5 Implemented components

| Component | What it does |
|---|---|
| **Collector** | AT Protocol XRPC; persistent session with `refreshJwt`; exponential backoff; rate-limit-aware; day-chunked backfill |
| **Ingest** | Idempotent upsert; run bookkeeping on an independent connection; stale-run reaping; timestamp repair |
| **Labeler** | Versioned rule engine; bilingual lexicons; cyber / Canada / topic classification; corroboration logic; negative guards |
| **Evaluation** | Stratified sampling, Wilson confidence intervals, precision/recall/F1 per label version |
| **Trend detection** | Adjacent-window comparison; posts *and* unique authors; no composite score |
| **Null model** | Permutation test, two null hypotheses, family-wise error correction |
| **Broadcast filter** | Behavioral identification of automated feeds and promotional accounts |
| **Agent** | Anthropic or local OpenAI-compatible backend; 3 tools; structured output; hallucination verification |
| **Reporting** | Markdown with full provenance; data-health warnings; persisted to `analysis_runs` |
| **Ops** | cron-driven collection; health check; log rotation; 191 tests |

---

## 4. Findings

### 4.1 The labeler was substantially wrong, and this was measurable

A 100-post stratified sample was drawn across four strata (relevant /
cyber-only / Canada-only / neither); 99 of 100 were annotated. Scores are
stratum-weighted with Wilson confidence intervals.

| Metric | `rules_v1` | `rules_v2` |
|---|---:|---:|
| `is_relevant` **precision** | **56.0%** | **86.3%** |
| `is_relevant` **recall** | 85.6% | **91.7%** |
| F1 | 67.7% | **88.9%** |

**Nearly half of what v0 called "Canada-related cybersecurity discussion" was
not.** The headline pilot number was revised from 27/day to **21.3/day**.

This matters methodologically more than numerically: **v0 reported a number it
had never validated.** Everything downstream inherited a 44% false-positive
rate silently. The fix was not a better rule — it was *measuring the rule at
all*.

### 4.2 No topic in the current window is distinguishable from noise

v0's trend detection answered "which topic changed most." It could not answer
"is that change larger than random fluctuation." A permutation test was added
to close that gap.

| topic | Δ | growth | null sd | **p (FWER)** | verdict |
|---|---:|---:|---:|---:|---|
| `data_breach` | +10 | +56% | 7.45 | **0.718** | noise |
| `other` | +4 | +5% | 13.84 | 0.991 | noise |
| `malware_infostealer` | +3 | +100% | 3.29 | 0.998 | noise |
| `fraud_financial` | +2 | +22% | 4.69 | 1.000 | noise |
| `ransomware` | −5 | −20% | 7.47 | 0.969 | noise |

**Under the null hypothesis, random fluctuation alone produces a swing of ±28
in *some* topic. The largest observed change is +10.**

Two null models are implemented:

- **post-level** — reshuffle window assignment per post. Ignores author
  clustering, so its p-values are optimistic. Kept as a comparison baseline.
- **author-level** (default) — circularly shift each author's *entire* posting
  history by a whole number of days across the 14-day span. This preserves each
  author's post count, topic mix, and burst structure, and destroys only the
  alignment between author activity and the calendar. It rules out the
  explanation *"this topic rose because a few prolific accounts happened to
  cluster."*

The author-level null distribution is ~20% wider than the post-level one
(`other`: sd 8.52 → 13.84), confirming the corpus is over-dispersed.

**Multiple comparisons are corrected.** Choosing the largest of 10 topics to
narrate is a textbook look-elsewhere effect. Each permutation additionally
records the maximum |Δ| across all topics, giving a family-wise corrected
p-value via the max-statistic method.

#### Why z-scores were rejected

The v1 specification called for z-score-based detection. This was tested and
rejected on evidence:

> Real corpus max |z| ranges **0.2 – 0.9**. Pure Poisson noise produces |z| with
> a **median of 1.4 – 1.7** and a **95th percentile of 3.0 – 4.0**.

**The z-score scores noise higher than it scores the real data.** It is
anti-informative at this volume. The cause is that the Poisson variance
assumption fails: one author posting eight times, or a single news item being
widely reshared, breaks independence. Permutation testing makes no distributional
assumption and is therefore valid where the z-score is not.

### 4.3 How much more data would be needed: ~8×

Rather than assuming √n scaling, it was measured — by replicating the author
pool k times and re-running the permutation test.

| scale k | relevant / window | null max\|Δ\| p95 | proportionally scaled Δ | detectable |
|---:|---:|---:|---:|:---:|
| 1 | 158 | 28.0 | 10 | ✗ |
| 2 | 316 | 40.0 | 20 | ✗ |
| 4 | 632 | 56.0 | 40 | ✗ |
| **8** | **1,264** | **78.0** | **80** | **✓** |
| 16 | 2,528 | 108.0 | 160 | ✓ |

Observed scaling matches √k. **An effect of the currently observed size needs
roughly 8× the data to become detectable**, reachable either by:

- lengthening windows to ~56 days (≈4 months of total span), or
- increasing collection volume ~8× (more terms, more platforms).

This is the quantitative form of a concern that had previously only been
expressible as "the data might not be enough." It is a √n problem; no amount of
parameter tuning resolves it.

### 4.4 A quarter of the corpus has no audience

| | |
|---|---:|
| Broadcast accounts | **11 of 372 (3.0%)** |
| Posts they contribute | **158 of 650 (24.3%)** |
| Their mean engagement | **0.20** |
| Everyone else's mean engagement | **5.34** |

They are two kinds of account:

1. **Automated threat-intelligence feeds** — `ecrime.ch`,
   `cyberintelligence.bsky.social`, `ransomlook.bsky.social`,
   `falconfeedsio.bsky.social`. Ransomware leak-site monitors that
   auto-post every new victim.
2. **Security conference promotion** — `bsidesedmonton.bsky.social`,
   `hackfest.bsky.social`. Repeated CFP and event announcements.

**Neither is public discussion.** Counting them toward topic volume means
measuring a bot's posting rate and calling it social attention.

**The concrete damage:** of 45 `ransomware` posts in 14 days, **40 came from 5
automated feeds** (4.5 posts/author, versus 1.0–1.4 for every other topic).
Excluding them removes `ransomware` from the trend ranking entirely. Every
`ransomware` rise or fall in earlier reports was measuring feed uptime.

Detection is **behavioral, not a handle blocklist** — blocklists rot and do not
transfer across time windows:

```
n_posts ≥ 5   AND   n_active_days ≥ 4   AND   zero-engagement rate ≥ 60%
```

**A trap that had to be avoided here:** `like_count` is a snapshot taken at
collection time. Incremental collection captures posts ~4 hours old; backfill
averaged 354 hours. Without a guard, "zero engagement" would classify *every
freshly collected post* as a bot. The zero-engagement rate therefore only counts
posts with ≥24 h of settling time. The confound was then checked directly and
does not drive the result — broadcast accounts actually have *longer*
collection lag (359 h vs 323 h), and restricting to posts with >168 h of
settling preserves the gap (76.7% vs 50.0% zero-engagement; 0.19 vs 3.15 mean
likes). The guard stays regardless, because the next dataset may not be so
forgiving.

### 4.5 The agent separates two questions that are easy to conflate

When the null model returns `noise`, the system automatically switches the
agent's prompt. The original prompt opened with *"the statistical layer has
determined this is the top candidate trend; your task is to explain it"* —
which, applied to a change indistinguishable from noise, is an instruction to
confabulate.

The replacement prompt asks a different question: *"setting volume aside
entirely, is there a coherent story in these posts or not?"* — and states
explicitly that "no, there is no story" is a correct and valuable answer.

It forces a verdict on two **independent** axes:

| field | this window |
|---|---|
| `trend_claim_supported` | `not_supported` — the volume rise does not hold up |
| `coherence` | `one_story` — but the content genuinely is one event |

**These do not contradict each other**, and the distinction is the useful
output: there is a real incident in the posts (an Alberta voter-list breach),
but its effect on volume is indistinguishable from noise. *The event is
reportable; "it is trending" is not.*

Evidence integrity is enforced mechanically: every `post_id` the agent cites is
checked against the set of posts the tools actually returned. Hallucinated IDs
are not silently dropped — they are flagged in the published report, so readers
can judge that run's reliability. In the current run, all 5 cited IDs verified,
and the agent additionally flagged 2 of them as belonging to a *different*
incident under the same topic label.

---

## 5. Limitations

### 5.1 Known and unresolved

**Single source.** Bluesky only. Not representative of Canadian discourse, and
the size of that gap is unmeasured. This is the largest limitation and it is
structural.

**Volume is ~8× short of detecting trends of the observed magnitude** (§4.3).
The system currently produces defensible *descriptions* of a window and
defensible *negative* results. It cannot yet produce positive trend claims.

**The evaluation set has an independence problem.** The gold set was
annotated by the same agent that wrote the labeler being evaluated. Systematic
blind spots in the rules are likely to be reproduced in the annotations, which
would inflate both precision and recall. *Independent annotation by a domain
expert is the highest-value next step for validity*, and is cheap — it is one
TSV file.

**The evaluation set covers only backfill data.** All 99 labeled samples came
from backfill collection. Incremental data — which differs systematically in
collection lag and observed engagement — is unevaluated.

**`other` absorbs ~44% of relevant posts.** The topic lexicon does not cover
nearly half the corpus, which limits what cross-topic comparison can mean.

**Strong weekday effect.** Weekend volume is ~45% of weekday volume (roughly
15 vs 33 relevant/day). Any window shorter than 7 days, or any comparison where
the two windows contain different numbers of weekends, is badly confounded. The
14-day/2-window design balances this by construction, but it constrains window
choice.

**Engagement metrics are snapshots, not final values** (§3.3). Cross-mode
engagement comparison is invalid without a correction that does not yet exist.

**French coverage is thin.** 22 of 650 relevant posts. Measured cause: French
technical vocabulary is largely unused in practice — `rançongiciel` returned 1
post in 7 days, `hameçonnage` 5, while `cybersécurité` returned hundreds. The
Canada lexicon therefore carries French signal through place and institution
names rather than translated jargon. Whether this under-represents Francophone
discussion or accurately reflects it is **not currently known**.

**Rule-based relevance.** 86.3% precision means roughly one in seven "relevant"
posts is not. Entity linking or a trained classifier would be better; the
versioned-labels design (P2) means either can be added without re-collection.

### 5.2 Explicitly out of scope

Sentiment analysis, embeddings and semantic drift, entity extraction, named
network analysis, Prometheus/Grafana, real-time alerting, a web UI. These were
deferred deliberately — not for effort reasons, but because **the corpus cannot
currently support conclusions drawn from them**, and building them would
manufacture confidence rather than knowledge.

### 5.3 Ethical and legal posture

Collection uses the documented public API under its terms of service, with
authenticated access at ~1% of the published rate limit. Only public posts are
collected. No attempt is made to deanonymize, aggregate profiles, or track
individuals; author identifiers exist solely to detect posting concentration,
which is a methodological control (§4.4), not a subject of study. A request to
create synthetic accounts to expand access was declined as a terms-of-service
violation.

---

## 6. Implementation notes — what broke and what it taught

Every item below was found by the system's own instrumentation rather than by
inspection, which is the argument for principles P3 and P5.

| Incident | Root cause | Consequence if undetected |
|---|---|---|
| **10h23m collection outage** | Bluesky returns **HTTP 400** with `{"error":"ExpiredToken"}` for expired tokens, not 401. The refresh path only triggered on 401. | A 10-hour hole in the time series, visually identical to a drop in discussion |
| **Reverse-pagination truncation** | Search returns newest-first; page limits always truncate the oldest dates | A fabricated upward trend across the entire backfill period |
| **`createdAt` is client-supplied** | 53 posts backdated >2 days (one to 2016), 238 future-dated | Posts landing in the wrong analysis window |
| **Inherited `America/Toronto` timezone** | New cluster inherits host timezone; `date_trunc` depends on session TZ | 23- and 25-hour days across DST; silently wrong daily counts |
| **NUL byte crash** | PostgreSQL cannot store U+0000; a post contained one | Killed a backfill run after 52,079 posts |
| **`partial` status too coarse** | Any HTTP error marked the run partial; token refresh happens ~every 2h | Every run flagged `partial`, drowning the P5 signal entirely |
| **Stale `running` runs** | `SIGKILL` bypasses exception handlers | Permanently open run records |
| **Duplicate CLI command registration** | Two `analyze` definitions; the later silently shadowed the earlier | Edits applied to dead code |

**The outage is the most instructive.** A test named
`test_401_refreshes_without_new_login` existed and passed. It tested *the error
shape I had imagined*, not the one the API actually produces. The refresh token
was valid for another three months and was never called. The lesson is narrow and
transferable: **a test written against an assumed contract validates the
assumption, not the integration.** The fix now matches on the error body across
both status codes, with four regression tests derived from captured real
responses.

---

## 7. What I would do next, in priority order

1. **Independent annotation of the evaluation set.** Cheapest available
   improvement to validity, and it currently gates the credibility of every
   precision/recall number in this document (§5.1).
2. **Add Reddit as a second source.** Directly attacks the two largest
   limitations at once — single-source bias and the 8× volume shortfall — and
   Reddit is discussion-shaped rather than broadcast-shaped, which §4.4 suggests
   matters a great deal.
3. **Extend the evaluation set to incremental data**, so the labeler is
   validated on both sampling regimes rather than one.
4. **Longer windows (28–56 days) once the corpus supports them**, per the power
   analysis. Slower-moving, but actually detectable.
5. **Correct engagement for collection lag**, enabling valid cross-mode
   comparison and a better-grounded broadcast filter.
6. **Only then** revisit entities, keywords, and semantic drift — once there is
   enough data for them to mean something.

---

## 8. Repository guide

| Path | Contents |
|---|---|
| `HANDOFF.md` | v0 specification |
| `TREND_AGENT.md` | v1 specification (trend analysis agent) |
| `README.md` | Implementation detail, measured API behavior, decision records, full findings |
| `PROJECT_WRAPUP.md` | This document |
| `reports/wave1_all.md` | First-wave analysis, full corpus |
| `reports/wave1_no_broadcast.md` | First-wave analysis, broadcast accounts excluded |
| `eval/sample.tsv` | 100-post annotated evaluation set |
| `src/ccint/collectors/` | Bluesky AT Protocol client |
| `src/ccint/labelers/` | Versioned rule engine + bilingual lexicons |
| `src/ccint/analytics/` | Trend detection, null model, broadcast filter |
| `src/ccint/agent/` | Tool definitions, prompts, analysis loop |
| `migrations/` | Numbered SQL migrations |
| `tests/` | 191 tests |

```bash
ccint collect backfill --since 2026-08-22    # seed history
ccint collect incremental                     # cron, every 15 min
ccint label --version rules_v2                # offline, recomputable
ccint eval score                              # precision / recall / F1
ccint analyze --window 7 --null-iter 5000 --exclude-broadcast
```

---

## 9. Closing note

The most useful thing this project produced is **the ability to tell that it has
not found a trend.**

A version of this system that reported `data_breach +56%` would look more
successful and would be wrong. The layers that produce the negative result — the
null model, the power analysis, the broadcast filter, the evaluation set, the
prompt that permits the agent to say "there is no story here" — are the actual
deliverable. The corpus will grow; the discipline to know when it is large
enough has to be built first.

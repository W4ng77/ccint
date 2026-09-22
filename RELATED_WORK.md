# Related Work

*All six works below were retrieved and read for this review; claims about their
methods are taken from the papers themselves, not from memory. Where a design
choice in **ccint** was changed because of one of them, that is marked
**[adopted]**; where we deliberately diverge, **[diverge]**.*

---

## 1. Where this project sits

Cybersecurity social-media mining splits along two axes that the literature
rarely separates:

|  | **extract artefacts** | **measure discourse** |
|---|---|---|
| **near-real-time** | Sabottke et al. 2015; Huang & Ban 2020; STINER 2026; TIBlender 2026 | — |
| **longitudinal** | — | AIGT (ACL 2025); **this project** |

The artefact line asks *what is the IoC and how fast can we get it*. The
discourse line asks *what is being talked about, by whom, and is that changing*.
ccint is on the discourse line, and the closest methodological neighbour is
therefore **not** a CTI system but the AIGT monitoring study — a point that only
became clear after reading them side by side.

---

## 2. Traditional cyber social mining pipelines

**Sabottke, Suciu & Dumitraş, "Vulnerability Disclosure in the Age of Social
Media: Exploiting Twitter for Predicting Real-World Exploits."** *24th USENIX
Security Symposium*, 2015.

The canonical demonstration that social media carries exploit signal ahead of
formal disclosure, and — more importantly for us — the canonical demonstration
that such a detector must be evaluated against an adversary who knows it exists.

**Huang & Ban, "Monitoring Social Media for Vulnerability-Threat Prediction and
Topic Analysis."** *IEEE TrustCom 2020*, pp. 1771–1776.
DOI 10.1109/TrustCom50675.2020.00243.

A two-stage pipeline: supervised filtering of non-cybersecurity tweets, then
topic labelling of what survives, feeding a vulnerability-exploitation
likelihood model. This is structurally the same shape as ccint's
`collect → relevance → topic` chain.

**[diverge] Filtering at collection vs. filtering downstream.** These pipelines
filter to the target population as data arrives. ccint deliberately does not:
the collector searches 34 cybersecurity terms and *zero* Canada terms, stores
everything, and decides Canada-relevance offline against the stored corpus
(README §3.1). The asymmetry is about reversibility — a collection filter is
irreversible, a relevance filter is recomputable. This paid for itself three
times in one day when `GTA` turned out to mean *Grand Theft Auto* and `CRA` the
EU *Cyber Resilience Act*; all three errors were fixed by writing a new
`label_version`, with no re-collection.

**[adopted] Two-stage relevance-then-topic.** We keep this structure, and
README §12 shows why the second stage needs to be more than keywords: 30% of
our residual `other` bucket turned out to belong in an existing topic, missed
because news prose says *"court records were accessed"* where a rule expects
*"data breach"*.

---

## 3. Actionable entity extraction

**Ech-Chammakhy, Motii, Azrara & Chbili, "STINER: Automated Extraction of
Strategic Cyber Threat Intelligence from X."** arXiv:2608.14418, August 2026.

A taxonomy of eight *strategic* entity types — TARGET, ACTOR, SECTOR, LOCATION,
SIZE, DATA_TYPE, PRICE, DATE — with 2,100 expert-annotated tweets and a
DarkBERT-based extractor reaching 89.3% strict F1.

Three findings bear directly on ccint:

**[adopted] Cohen's κ as the annotation-quality statistic.** STINER reports
κ = 0.84 between two security researchers at span level. We had been reporting
subset proportions ("recall 89%, restraint 96%"), which are not comparable to
anything. We now compute κ between the author's hand-coding and `llm_v1` on the
103 topic-codable posts of `eval/other_sample.tsv`: **κ = 0.870, 93.2%
agreement** (`scripts/stats_from_related_work.py`). The number is in STINER's
range, but it measures something weaker and we say so in the script and in the
report: the human coder here is also the person who wrote the prompt, so this
is *agreement*, not *independent validation*. STINER's two-annotator design is
the thing we still lack, and it remains the highest-priority open item.

**[diverge] Encoder models beat generative LLMs at this task.** STINER measures
zero-shot Llama-3 at 27.8% F1, rising only to 74.6% fine-tuned — still 15 points
below DarkBERT at ~2,700× the latency. This is a useful corrective to our own
Phase 5 result, where an LLM beat rules decisively. The lesson is
task-dependence, not LLM superiority: *classification into a small closed set*
suits a generative model; *span extraction* does not. If ccint adds an entity
layer it should be an encoder, not the 4B chat model.

**Not yet adopted:** the eight-type taxonomy itself. ccint currently extracts no
entities, which is the single largest capability gap against this literature.
STINER's schema is the obvious thing to implement, and its TARGET/SECTOR/
LOCATION fields would let "Canada-relevance" become an entity-linking decision
rather than a lexicon decision — replacing the weakest component in the system.

---

## 4. Agentic CTI

**Nakano, Koide & Chiba, "TIBlender: Early-Warning Threat Intelligence from
Cross-Platform Social Media Evidence."** arXiv:2606.04580, June 2026. NTT
Security Holdings / Tokyo Metropolitan University.

A five-stage multi-agent pipeline over X, Reddit, Telegram and Discord:
adaptive collection → campaign-level clustering (HDBSCAN + IoC Jaccard ≥ 0.25)
→ four role-specialised investigation agents (Infrastructure, Technical, Social,
Actor) with external tools (RDAP/WHOIS, passive DNS, Certificate Transparency,
NVD) → an adaptive evaluation loop → structured STIX 2.1 output. 873,973 posts
over 31 days.

This is what ccint's agent layer would look like if taken seriously, and it
supplies three methods we can use at our scale:

**[adopted, partially] Two independent judges from different providers.** Our
agent verifies that every cited `post_id` was actually returned by a tool, which
catches fabricated evidence but not wrong reasoning over real evidence.
TIBlender runs two judges from *different model providers* in parallel without
letting either see the other's output, precisely to avoid shared bias, and
requires unanimous PROCEED. We cannot replicate the different-provider property
on a single local model, and we will not pretend otherwise; what we can adopt
now is the **conservative-consensus structure** and the explicit
SKIP / NEED_MORE_INFO verdict, rather than forcing a verdict every time.

**[to adopt] Feed-Scoped Absence (FSA) as a novelty metric.** TIBlender reports
what fraction of its IoCs are *absent* from each public feed (83.0–99.6%), plus
mean lead time against those feeds (72 h ahead of PhishTank, 94 h ahead of CISA
KEV). This is the metric ccint most conspicuously lacks: we have never measured
whether anything we surface is not already in the Canadian Centre for Cyber
Security's advisories. **An FSA-style comparison against the Cyber Centre feed
is the cheapest available test of whether this system adds information at all**,
and it is now the top item in §7.

**[adopted] Confidence tiers grounded in independent-source count.** TIBlender
assigns HIGH/MEDIUM/LOW from number of independent sources, shared-hosting
detection and cross-tool consistency. ccint's `actor_v1` profiles already carry
a sample-size tier (`established` ≥ 5 posts / `provisional` 3–4 / `single` 1–2)
for exactly this reason: we found that 38 of the accounts first labelled
`incident_feed` had posted **once**, and were ordinary people resharing one
breach story (README §13.3). Reliability tiering is not optional at small n.

**[diverge] Cross-platform collection.** TIBlender's platform-contribution
analysis shows that excluding a single platform removes up to 50% of reports in
some threat categories — Telegram alone is 66.3% of its raw volume. ccint is
single-platform by necessity, and this result is the strongest external evidence
for how severe that limitation is. We cite it in the limitations section rather
than leaving "single source" as an unquantified caveat.

---

## 5. Social-media analysis agent architecture

**Xue, Cui, Qian et al., "Linking Heterogeneous Data with Coordinated Agent
Flows for Social Media Analysis" (SIA).** arXiv:2510.26172.

SIA organises analysis as a stage-synchronised flow — goal decomposition →
query → mining → visualisation → reporting — with a *data coordinator* that
unifies tabular, textual and network data and maintains cross-stage
dependencies. Its interface is built so an analyst can trace, validate and
refine the agent's reasoning.

**[adopted] Insight-type → technique mapping.** SIA is organised by a bottom-up
taxonomy connecting *what kind of insight is wanted* to *which mining and
visualisation technique suits it*. ccint arrived at a narrower version of the
same idea from the data side: the `--actor-mode` switch (`attention` / `events`
/ `ecosystem` / `all`) exists because the same corpus answers different
questions under different exclusions — `ransomware` vanishes from the ranking
under `attention` but persists under `events` (README §13.5). SIA is the
principled framing of that instinct.

**[diverge] Coordinator-orchestrated vs. pipeline-fixed.** SIA lets agents plan
the analysis strategy. ccint's pipeline is fixed and the agent is confined to
*explaining* a candidate the statistical layer has already selected — it cannot
re-rank, and its prompt says so. At n ≈ 21 relevant posts/day, giving an agent
latitude to choose the analysis is how you get a narrative for noise. We regard
the fixed pipeline as correct *for this corpus size* and would revisit it at
SIA's scale.

---

## 6. Longitudinal social-media monitoring — the closest analogue

**Sun, Zhang, Shen, Zhang, Liu, Backes, Zhang & He, "Are We in the AI-Generated
Text World Already? Quantifying and Monitoring AIGT on Social Media."**
*ACL 2025* (Long Papers, 2025.acl-long.1120); arXiv:2412.18148. HKUST(GZ) /
CISPA.

2.4M posts from Medium, Quora and Reddit, January 2022 – October 2024, scored by
a trained detector (OSM-Det) and tracked as a monthly *AI Attribution Rate*.
They compare detected AIGT against human text along four dimensions —
linguistic patterns, topic distributions, engagement levels, and author follower
distributions — and report the platform divergence (Medium 1.77% → 37.03%;
Reddit 1.31% → 2.45%).

This is the paper whose *shape* ccint most resembles: a detector applied to a
longitudinal corpus, then distributional comparison across topic, engagement and
author strata.

**[adopted] Mann–Whitney U for engagement comparison.** They test engagement
differences with Mann–Whitney U rather than comparing means, which is correct
for the heavy-tailed, zero-inflated engagement distributions social data
produces. We had been reporting bare means (0.20 vs 5.34) with no test. Now
(`scripts/stats_from_related_work.py`), on posts with ≥24 h of settling time:

| comparison | n | medians | U | p | rank-biserial r |
|---|---|---|---|---|---|
| intel feed vs individual | 107 vs 72 | 0 vs 1 | 2375 | **1.6 × 10⁻⁷** | +0.383 |
| promotion vs individual | 53 vs 72 | 0 vs 1 | 1432 | **9.0 × 10⁻³** | +0.249 |
| intel feed vs promotion | 107 vs 53 | 0 vs 0 | 2310 | **1.2 × 10⁻²** | +0.185 |

The third row matters most: **intel feeds and promotional accounts differ from
each other**, not only from individuals. That is empirical support for splitting
the old single `is_broadcast` flag into two axes, which we had justified only on
conceptual grounds.

**[adopted] Stratified distributional comparison as the unit of analysis.**
Their four dimensions map onto what ccint now produces: topic distribution
(README §12.5), engagement (§13), author strata (`actor_type`). Framing the
output as *distributional comparison* rather than *trend detection* is a better
description of what a corpus this size can support.

**[diverge] Detector validation.** OSM-Det is validated on held-out synthetic
data (accuracy 0.979) with no human annotation of real platform posts reported,
and the paper does not control for platform volume drift or discuss confounders
of the AAR trend. ccint's relevance labeller is weaker in absolute terms (86.3%
precision) but is validated against **hand-annotated real posts** with Wilson
intervals, and every window comparison is run against a permutation null model
(README §11.1). On a corpus 3,700× smaller, that discipline is the only thing
making a negative result meaningful.

**[diverge] Effect size vs. corpus size.** Their headline effect is a 20-fold
change over 34 months. Ours is +10 posts over 7 days, which our own power
analysis says needs ~8× more data to be detectable (README §11.3). The
comparison is clarifying rather than discouraging: it shows what *time depth*
buys, and that the honest move at month one is to report composition, not trend.

---

## 7. Evaluating the agent itself

**Xue, Cui, Qian, Hu & Xu, "SoMe: A Realistic Benchmark for LLM-based Social
Media Agents."** *AAAI 2026*; arXiv:2512.14720.

Eight tasks over 9.1M posts, 6,591 user profiles and 17,869 annotated queries,
evaluating 13 LLMs. Findings relevant to us:

- **Hallucination rates of 8–29%**, including *fabricated tool responses* —
  models inventing what a tool returned.
- **Tool-chain planning failures** dominate for smaller models: wrong sequence,
  wrong parameters, keyword mismatch.
- **Stronger reasoning models did worse**: DeepSeek-R1 underperformed its base
  model by 31.95–56.14% on agentic tasks.
- Process is scored as well as answers — One-attempt Success Rate for tool-chain
  planning, alongside final-answer accuracy.

**[adopted] Process evaluation, not just output evaluation.** ccint records
every tool call in `analysis_runs` and prints them in the report, but has never
*scored* them. SoMe's OSR is directly implementable: our reports already contain
the trajectory.

**[adopted, already present] Grounding checks belong in the harness.** SoMe finds
models fabricating tool responses; notably it reports no explicit citation
checking. ccint verifies every cited `post_id` against the set the tools
actually served and publishes hallucinations as warnings rather than dropping
them — on the current run, 5/5 cited ids verified. Given an 8–29% hallucination
band across mainstream models, we treat this as non-negotiable, not a nicety.

**[diverge] Local 4B model.** SoMe's results predict a small model should do
badly. Ours did not — 5/5 evidence ids verified, and it independently flagged
two cited posts as belonging to a *different* incident under the same label. The
honest reading is that our agent's task is far narrower than SoMe's (explain one
pre-selected topic with three tools, no multi-hop planning), so this is not
evidence against their finding; it is evidence that **narrowing the agent's job
is what makes a small model viable**.

---

## 8. Summary: what this project takes, and what it still lacks

| From | Method | Status |
|---|---|---|
| STINER | Cohen's κ for annotation agreement | **done** — κ = 0.870 |
| AIGT ACL 2025 | Mann–Whitney U for engagement | **done** — p = 1.6 × 10⁻⁷ |
| AIGT ACL 2025 | Distributional comparison as the framing | **done** |
| TIBlender | Reliability tiers at small n | **done** — `established`/`provisional`/`single` |
| SIA | Insight-type → analysis-mode mapping | **done** — `--actor-mode` |
| SoMe | Verified citations in the harness | **done** |
| TIBlender | Feed-Scoped Absence vs. Cyber Centre advisories | **next** |
| TIBlender | Conservative-consensus judging with SKIP verdict | **next** |
| SoMe | One-attempt Success Rate on tool chains | **next** |
| STINER | Eight-type strategic entity extraction (encoder) | **open** |
| TIBlender | Cross-platform collection | **open** — single platform |

**The three capability gaps, in order of how badly they hurt:**

1. **No entity extraction.** Every comparable system produces actionable
   artefacts; ccint produces topic counts. STINER's taxonomy is the fix, and it
   would also let Canada-relevance become entity linking rather than a lexicon.
2. **Single platform.** TIBlender quantifies the cost: dropping one platform
   removes up to 50% of reports in some categories.
3. **No external-baseline comparison.** Until an FSA-style measurement is run
   against the Cyber Centre feed, we cannot say this system surfaces anything a
   practitioner did not already have.

**What this project has that these papers do not:** a permutation-based null
model with family-wise correction, so that "no trend" is a reportable result;
a hand-annotated relevance gold set with Wilson intervals; engagement controlled
for collection-time snapshot bias; and an actor typology that separates
machine-generated intelligence signal from organic social attention instead of
discarding both as noise. These matter precisely *because* the corpus is small —
they are what keeps a 650-post study from reporting a 56% rise that a
permutation test puts at p = 0.73.

---

## References

1. C. Sabottke, O. Suciu, T. Dumitraş. Vulnerability Disclosure in the Age of
   Social Media: Exploiting Twitter for Predicting Real-World Exploits.
   *24th USENIX Security Symposium*, 2015.
2. S.-Y. Huang, T. Ban. Monitoring Social Media for Vulnerability-Threat
   Prediction and Topic Analysis. *IEEE TrustCom*, 2020, 1771–1776.
   DOI 10.1109/TrustCom50675.2020.00243.
3. Y. Ech-Chammakhy, A. Motii, O. Azrara, J. Chbili. STINER: Automated
   Extraction of Strategic Cyber Threat Intelligence from X.
   arXiv:2608.14418, 2026. Code: github.com/ChammakhYasir/STINER
4. H. Nakano, T. Koide, D. Chiba. TIBlender: Early-Warning Threat Intelligence
   from Cross-Platform Social Media Evidence. arXiv:2606.04580, 2026.
5. Xue et al. Linking Heterogeneous Data with Coordinated Agent Flows for
   Social Media Analysis (SIA). arXiv:2510.26172.
6. Z. Sun, Z. Zhang, X. Shen, Z. Zhang, Y. Liu, M. Backes, Y. Zhang, X. He.
   Are We in the AI-Generated Text World Already? Quantifying and Monitoring
   AIGT on Social Media. *ACL 2025* (2025.acl-long.1120); arXiv:2412.18148.
7. D. Xue, J. Cui, S. Qian, C. Hu, C. Xu. SoMe: A Realistic Benchmark for
   LLM-based Social Media Agents. *AAAI 2026*; arXiv:2512.14720.
   Code: github.com/LivXue/SoMe

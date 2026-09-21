# Architecture — Lead Qualification Tool

*Describes build 1.1.2, rubric version `1.1-frozen-on-training`. For why the tool exists, see [CASE_STUDY.md](CASE_STUDY.md); for the reasoning behind the design, see [DECISIONS.md](DECISIONS.md).*

## 1. Pipeline

```
leads CSV
  │
  ├─ validate ─────────── file exists, six required columns, at least one row
  │
  ├─ tier ─────────────── LLM call 1 (tiering). Unique industry strings, not leads.
  │                       Cache-first; live call only for unseen strings.
  │                         → industry_tier_cache.json  (shared across files)
  │
  ├─ score ────────────── four factors → fit score, urgency score → priority score
  │
  ├─ guardrails ───────── borderline / incomplete / unscoreable → REVIEW
  │
  ├─ order ────────────── queue position across all scored leads
  │                         → <stem>_scored.csv, <stem>_run_report.json
  │
  ├─ filter QUALIFIED ─── only QUALIFIED leads go on to messaging
  │
  ├─ select variant ───── v1_value / v2_engagement / v3_generic, in code
  │
  ├─ generate messages ── LLM call 2 (messaging). Batched per variant.
  │                         → <stem>_message_cache.json,
  │                           <stem>_message_run_report.json
  │
  └─ build report ─────── no API call. Reasons, statistics, samples, queue.
                            → <stem>_output_report.json, <stem>_output_report.csv
```

Data flows through live objects in one notebook session. `run_pipeline()` holds a `list[ScoredLead]`; messaging writes five message fields onto each lead in place, and the report stage reads the same list. No file is re-read and no join on `lead_id` happens anywhere; because `lead_id` is positional, a re-join is where a message would most likely land on the wrong lead.

## 2. Components

| Component | Owns | Does not |
|---|---|---|
| `rubric_scorer.py` | Field cleaning, per-factor scoring, weighted means, guardrails, queue position, the `ScoredLead` dataclass, the per-lead reasoning string | Call any API; emit message fields in the scored table |
| `message_generator.py` | Variant selection, payload building, prompt assembly, batching, parsing, reconciliation by `lead_id`, message cache, retries, leak check | Score leads; decide which leads qualify; know about the report |
| `report_builder.py` | Outcome-reason derivation, summary statistics, sample selection, decision-grouped queue, JSON/CSV/console output, output naming | Call any API; rewrite, trim or regenerate a message |
| `Lead_Qualification_Tool.ipynb` | The token pacer, `TIER_PROMPT`, the tiering call, input validation, `run_pipeline()`, API-key resolution | Hold scoring, messaging or reporting logic |

Every number that moves a score, and every label, reason text and output name, comes from `config.yaml`. The modules hardcode policy, not values: missing-data handling, linear interpolation, the tie-break chain, each guardrail's condition, and alignment by `lead_id`. The one exception is `TIER_PROMPT`, business text that lives in the notebook rather than in config.

## 3. Data contract

**Required columns** (`missing_data.completeness_fields`): `name`, `company`, `company_size`, `industry`, `source`, `last_interaction_date`. A missing file, a missing column or an empty file stops the run with a message naming the problem.

**Missing values.** A value counts as missing if it is null or equals one of the sentinels in `missing_data.sentinel_values`: `NA`, `N/A`, `None`, `Unknown`, `Unknown Sector`, `Unknown Source`, `-`, or an empty string.

**`company_size`** is handled as a float throughout, because pandas coerces the column to float whenever nulls are present. It is formatted as an integer only for display.

**`lead_id`** is assigned per file from row order: `L` plus a zero-padded row number (`runtime.lead_id_prefix`). The same ID means different leads in different files.

**`processing_date: auto`** resolves to the newest `last_interaction_date` in the file, never the system clock. The supplied dates are about 960 days old, so measuring against today would score every lead as cold.

**`source`** must be one of the six keys of `factors.source.class_lookup`: `Inbound demo request`, `Content download`, `Webinar attendee`, `Referral`, `LinkedIn outreach`, `Sales call`. Any other value scores as missing and the source factor is dropped. Unlike a blank or sentinel value, an unmapped value does not by itself route the lead to REVIEW.

## 4. Scoring model

Each factor produces a raw score from 1 to 10 and carries two weights:

| Factor | `score_weight` | `urgency_weight` | Raw score |
|---|---|---|---|
| industry | 1.75 | 0.0 | tier from the tiering call: `tier_1` 9.0, `tier_2` 6.0, `tier_3` 3.0 |
| company_size | 1.25 | 0.0 | band range, interpolated: startup 1–10 → 1.0–3.0, smb 11–500 → 4.0–7.0, mid_market 501–5,000 → 8.0–10.0, enterprise 5,001+ → 8.0–10.0 (capped at 10,000) |
| recency | 1.0 | 1.0 | segment range, interpolated by days since contact: ≤30 → 9.0–8.0, ≤60 → 8.0–6.0, ≤90 → 6.0–4.0, beyond → 4.0–2.0 (capped at 180 days) |
| source | 0.75 | 0.6 | `buyer_initiated` 9.0, `seller_initiated` 4.0 |

Size and recency interpolate linearly inside their band's range, so scores never jump at a boundary. Source and industry are categorical.

- **Fit score** = weighted mean of the present factors using `score_weight`. A missing factor is dropped and the remaining weights are renormalised; it is never imputed.
- **Urgency score** = weighted mean using `urgency_weight`, which only recency and source carry.
- **Priority score** = 0.8 × fit + 0.2 × urgency (`priority.blend`). If no urgency factor is present, priority falls back to fit alone.
- **Priority bands**: HIGH at 7.5 or above, MEDIUM at 5.0 or above, otherwise LOW.

All three scores are rounded to one decimal (`runtime.score_precision`) before anything reads them.

**Decision.** Fit score at or above `decision.qualify_cutoff` (7.0) → QUALIFIED; below → REJECTED. Three guardrails then run, and each can override the decision but never a score:

| Guardrail | Fires when | Sets |
|---|---|---|
| `no_scoreable_factors` | every factor is missing, so no fit score exists | REVIEW |
| `incomplete_record` | any required field is null or a sentinel | REVIEW |
| `borderline_score` | fit score within ±0.25 of the cutoff (`decision.review_margin`), inclusive | REVIEW |

Every rule that matches is recorded in `guardrails_fired`. Missing data therefore never causes REJECTED.

**Queue position** (`priority_rank`) is assigned across all scored leads by priority score (descending), then fit score (descending), recency in days (ascending), company size (descending) and `lead_id` (ascending). The full chain makes the order reproducible.

**Reasoning string.** Each lead carries a one-line trace:

```
fit 9.0/10, urgency 8.3, priority 8.8 [HIGH] | source: 9.0/10 x w0.75 -> +1.42 (Content download -> buyer_initiated) | industry: ... | company_size: ... | recency: ...
```

Each segment shows raw score, weight, contribution to the fit score and source value; a dropped factor reads `DROPPED (reason)`.

## 5. LLM boundaries

Both calls use `openai/gpt-oss-120b` on Groq's OpenAI-compatible endpoint, with a 60-second request timeout. Their contracts are deliberately opposite, so each has its own client and parser.

| | Tiering | Messaging |
|---|---|---|
| Unit of work | unique industry strings | QUALIFIED leads, grouped by variant |
| Batch size | `llm.batch_size` = 25 strings | `llm_messages.batch_size` = 4 leads |
| Temperature | 0.0 | 0.7 |
| `max_tokens` | 2,000 | 4,500 |
| On truncation | fail closed: batch discarded, not retried; its strings score as missing | salvage: keep every complete message, re-send only the missing leads |
| Retries | up to `llm.max_retries` = 3 attempts per batch, for transport, 429, 5xx and parse failures | `llm_messages.content_retries` = 1 content retry, each attempt with up to 3 transport attempts |
| Cache scope | shared across all input files | one cache file per input file |
| Cache key | exact industry string | `sha256(first_name\|company\|industry\|source\|variant)` |

Auth, permission and bad-request errors are never retried. Retries back off by `llm.retry_backoff_seconds` (2.0 s) × attempt number.

**Messaging prompt.** `org_profile` + variant instructions + `shared_rules` + a JSON array of five fields per lead (`lead_id`, `first_name`, `company`, `industry`, `source`). No score reaches the model.

**Variant selection** happens in code, before any call: MEDIUM or LOW priority band → `v3_generic`; else urgency score above fit score → `v2_engagement`; else `v1_value`.

## 6. Rate-limit envelope

The build runs within Groq's free tier, which allows 8,000 tokens per minute for this model. That limit applies to the account, so tiering and messaging draw on the same budget.

The notebook's `TokenPacer` enforces it:

- **Window.** A 60-second sliding window of actual reported token usage.
- **Budget.** 8,000 × 0.85 headroom = 6,800 tokens per window.
- **Reservation.** Each call reserves its stage's `max_tokens + 1,000` (3,000 tiering, 5,500 messaging) and sleeps until that fits. An empty window always proceeds, so the loop always exits.
- **One ledger** for both stages, charged with reported usage (the reservation if none is reported).
- **429 handling.** The pacer charges the full reservation. For messaging it then waits 60 seconds and re-raises, so the module's own transport-retry counter still bounds the attempts.

The consequence: a messaging reservation of 5,500 tokens leaves only 1,300 of the 6,800 budget for everything else in the window, so each messaging call waits for the previous call to age out. In practice the pipeline makes about one LLM call per minute, so runtime scales with the number of calls, not leads. A 50-lead run took 525.2 s for 2 tiering calls and 8 messaging calls. **100 leads take roughly 15–20 minutes.**

## 7. Caching

**Tier cache** (`industry_tier_cache.json`). Keyed by the exact industry string, shared across every input file, and written to disk after any live classification. The same string always receives the same tier. The committed cache covers every industry string in `leads/leads_sample_50.csv`, so that file scores with no tiering call.

**Message cache** (`<stem>_message_cache.json`). Keyed by `sha256(first_name|company|industry|source|variant)`, never by `lead_id`, with one cache file per input file. Records store `lead_id` for audit only; a mismatch at the same key is reported as `lead_id_drift`.

**What the keys do not cover.** The message key does not include the vendor profile, the prompt text, the temperature or the model. The tier key does not include the tiering prompt or the model. Changing any of these serves stale entries until the relevant cache file is removed.

**Weights and scores.** Scores are never cached. After a weight change, only leads whose decision or variant changes need new messages; the rest are served from cache.

## 8. Failure handling

- **Bounded, nested retries.** For messaging, transport retry sits inside content retry. The worst case is 2 content attempts × 3 transport attempts = 6 HTTP requests per batch, after which the remaining leads are flagged. Every loop in the pipeline has an exit condition.
- **Alignment by `lead_id`.** A message is accepted only if its `lead_id` was sent in that batch, it is the first object for that ID, and its text is non-empty and passes the leak check. Unknown and duplicate IDs are recorded, not used.
- **Salvage and re-send.** Complete objects from a truncated reply are kept; only the missing leads go into the content retry.
- **Error bodies surfaced.** On a non-200 response the provider's own error text is kept and reported, never reduced to a status code.
- **Named User-Agent.** Every request sends one. Default library user agents are blocked at the provider's edge (Cloudflare error 1010).
- **Leak check.** Every message is checked for scoring vocabulary before it is accepted: the words `fit`, `fits`, `score`, `scores`, `scoring`, `priority`, `priorities`, `rank`, `ranked`, `ranking`, `qualified`, `qualification` (whole-word), and the substrings `tier_`, `{`, `[`. `leaked_internal()` returns the flagged term, but only the reason code `leaked_internal` is stored on the lead.
- **Failure reasons.** A lead without a message after its retries carries one of `api_error:<detail>`, `parse_failure`, `truncated`, `missing_from_reply`, `empty_message` or `leaked_internal` in `message_fail_reason`, and stays without a message.
- **Degraded paths.**
  - Zero QUALIFIED leads: messaging is skipped, and the report is produced with an empty sample list and a `sample_note`.
  - A lead with every factor missing is processed but not scored, and goes to REVIEW. The summary reports `total_processed` and `scored` separately.
  - A missing file, column, API key or config block stops the run with an error naming it.

## 9. Configuration map

| Block | Controls | Status |
|---|---|---|
| `meta` | rubric version, calibration basis, description | descriptive |
| `runtime` | processing date, score precision, `lead_id` prefix | frozen |
| `missing_data` | sentinel values, required fields | frozen |
| `factors` | weights, bands, tiers, source classes, caps | frozen (calibrated) |
| `priority` | fit/urgency blend, priority bands | frozen (calibrated) |
| `decision` | cutoff, review margin, capacity context (not used in scoring) | frozen (calibrated) |
| `guardrails` | which review rules are enabled | frozen |
| `llm` | tiering call: endpoint, model, key variable, temperature, tokens, batch size, retries | frozen |
| `output` | fallback file names, used only when no input stem is available | legacy default |
| `llm_messages` | messaging call settings, variant selection, prompts, cache and report names | tunable |
| `report` | reason vocabulary and codes, display order, sample rules, output naming, CSV columns | tunable |

"Frozen" means set on the calibration file and not changed since.

**Where target-organisation text lives.** Adapting the tool to a different vendor means rewriting:

- `TIER_PROMPT` (notebook)
- `llm_messages.org_profile`
- `llm_messages.variants`
- `llm_messages.shared_rules`
- the `detail` texts in `report.reason_lookup`
- `meta.description`

`TIER_PROMPT` is the only one of these outside `config.yaml`.

## 10. Outputs

Every output name derives from the input file's stem (`report.naming`, with the `leads_` prefix stripped), so no run overwrites another run's files.

| File | Purpose |
|---|---|
| `<stem>_output_report.json` | the full report |
| `<stem>_output_report.csv` | one row per lead in queue order, for a sales team (UTF-8 with BOM for Excel) |
| `<stem>_scored.csv` | per-lead scores, per-factor raw scores and reasoning |
| `<stem>_run_report.json` | scoring and tiering diagnostics |
| `<stem>_message_run_report.json` | per-batch messaging diagnostics and rates |
| `<stem>_message_cache.json` | message cache for this input file |

Keys of the report JSON:

- `summary`: decision counts, rates with their `n` and denominator, common rejection and review reasons, priority-band counts, message counts, capacity context.
- `queue`: leads grouped by decision (QUALIFIED, REVIEW, REJECTED), each group in queue order.
- `leads`: one record per lead with scores, decision, reasoning, factor contributions, guardrails fired, outcome reasons and message fields.
- `sample_messages`: 3–5 messages, chosen deterministically.
- `run_diagnostics`: tokens, batches and rates per LLM stage.
- `sample_note`, `build_warnings`: notes and warnings.

A committed example from a 50-lead run is in `sample_run/`.

## 11. Extension points

| Change | What to touch |
|---|---|
| Re-weight a factor | its `score_weight` / `urgency_weight`. Config only |
| Give a factor a path into priority | set its `urgency_weight` above 0.0. Config only |
| Turn a factor off | set both weights to 0.0. Config only |
| Re-cut size bands or recency segments | the `bands` lists. Config only |
| Re-map sources | `factors.source.class_lookup` and `class_scores`. Config only |
| Change cutoff, margin or priority bands | `decision` and `priority.bands`. Config only |
| Add a rejection reason | an entry in `report.reason_lookup` plus a binding in `report.rejection_factor_codes`, together. Config only |
| Change output naming | `report.naming` patterns (`{stem}` is the only variable). Config only |
| Add a fifth factor | a config block with both weights, plus a scorer function in `rubric_scorer.py` called from `score_lead()` |
| Add an auto-reject rule | a branch in `apply_guardrails()` and an entry in `guardrails`, together, after the review rules |

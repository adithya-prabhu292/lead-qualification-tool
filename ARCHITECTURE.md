# Architecture — Lead Qualification Tool

*Build 1.2.0-dev, rubric `1.2-frozen-on-training`. For why the tool exists see [CASE_STUDY.md](CASE_STUDY.md); for the reasoning behind the design see [DECISIONS.md](DECISIONS.md).*

## 1. Pipeline

A run starts at `cli.py`, which loads the config, resolves the key and calls `pipeline.run_pipeline()`. Everything below is inside that call.

```
leads CSV
  │
  ├─ validate ─────────── file exists, six required columns, at least one row
  ├─ tier ─────────────── LLM call 1 (tiering). Unique industry strings, not leads.
  │                       Cache-first; live call only for unseen strings.
  │                         → industry_tier_cache.json  (shared across files)
  ├─ score ────────────── four factors → fit score, urgency score → priority score
  ├─ guardrails ───────── borderline / incomplete or unreadable / unscoreable → REVIEW
  ├─ order ────────────── queue position across all scored leads
  │                         → <stem>_scored.csv, <stem>_run_report.json
  ├─ filter QUALIFIED ─── only QUALIFIED leads go on to messaging
  ├─ select variant ───── v1_value / v2_engagement / v3_generic, in code
  ├─ generate messages ── LLM call 2 (messaging). Batched per variant.
  │                         → <stem>_message_cache.json,
  │                           <stem>_message_run_report.json
  └─ build report ─────── no API call. Reasons, statistics, samples, queue.
                            → <stem>_output_report.json, <stem>_output_report.csv
```

Data flows through live objects in one process. `run_pipeline()` holds a `list[ScoredLead]`; messaging writes five message fields onto each lead in place, and the report stage reads the same list. Nothing is re-read and nothing joins on `lead_id` — it is positional, so a re-join is where a message would most likely land on the wrong lead.

## 2. Components

| Component | Owns | Does not |
|---|---|---|
| `cli.py` | Arguments, config loading, key resolution, tier-cache path, exit codes | Hold pipeline logic |
| `pipeline.py` | The token pacer, the tiering call and its retries, validation, stage composition, `run_pipeline()` | Hold scoring, messaging or report logic |
| `rubric_scorer.py` | Field cleaning, per-factor scoring, weighted means, guardrails, queue position, `ScoredLead`, the reasoning string | Call any API; emit message fields in the table |
| `message_generator.py` | Variant selection, payloads, prompts, batching, parsing, matching replies to leads, cache, retries, leak check | Score leads; decide who qualifies; know the report |
| `report_builder.py` | Outcome reasons, summary statistics, sample selection, decision-grouped queue, JSON/CSV/console output, naming | Call any API; rewrite or regenerate a message |

Every number that moves a score, and every label, reason text and output name, comes from `config.yaml`. The modules hardcode policy, not values: missing-data handling, interpolation, the tie-break chain, each guardrail's condition, alignment by `lead_id`. There is no exception left — the tiering prompt, the last business text in code, moved to `llm.tier_prompt` in this build.

### 2.1 Pipeline interface

`pipeline.py` holds no run state: config, key, pacer, output directory and both LLM clients are passed in, importing it has no side effects, and all output goes through a `log` callable. Each stage can therefore be called on its own with settings supplied per run — which lets a caller drive one run across several short requests, or drive it against stubbed clients with no network.

| Function | Responsibility |
|---|---|
| `load_config(path)` | Load and return the config |
| `resolve_api_key(cfg)` | Read the variable named in `llm.api_key_env` |
| `read_leads(path)` | Read the CSV; a missing file names the path |
| `validate_leads(df, cfg)` | Required columns present, at least one row |
| `TokenPacer` | `gate(ceiling, log)`, `charge(usage, ceiling)`, `used()` |
| `make_paced_message_client(pacer, log)` | The client given to `generate_messages()` |
| `call_tiering_api(prompt, cfg, api_key, pacer)` | The tiering HTTP call |
| `parse_tier_reply(text)` | The tiering parser; a list, or a parse-error dict |
| `classify_industry_batch(labels, …, client=None)` | One batch, with its retries and splits |
| `tier_industries(df, …, client=None)` | Cache-first tiering → `(tier_map, meta)` |
| `score_leads(df, cfg, tier_map)` | The scoring module → `(leads, processing_date)` |
| `generate_messages_for(qualified, …, client=None)` | The messaging module → message report or `None` |
| `run_pipeline(input_csv, cfg, api_key, out_dir, …)` | Composes the stages → `(report, leads)`; writes to `out_dir` |

## 3. Data contract

**Required columns** (`missing_data.completeness_fields`): `name`, `company`, `company_size`, `industry`, `source`, `last_interaction_date`. A missing file, column or row stops the run with a message naming the problem.

**Missing values.** A value is missing if it is null or one of the `missing_data.sentinel_values`: `NA`, `N/A`, `None`, `Unknown`, `Unknown Sector`, `Unknown Source`, `-`, empty string.

**Unreadable values.** A value its factor cannot score counts the same way: a `source` outside the mapped list, an `industry` the tiering call did not classify, a `company_size` that is not a readable headcount, a `last_interaction_date` that will not parse. Each joins `missing_fields` and routes the lead to REVIEW. Otherwise the lead is decided on whichever factors survived, with nothing recorded to say one was dropped.

**`company_size`** is a float throughout, because pandas coerces the column to float whenever nulls are present; it is formatted as an integer only for display.

**`lead_id`** is assigned per file from row order: `L` plus a zero-padded row number (`runtime.lead_id_prefix`). The same ID means different leads in different files.

**`processing_date: auto`** resolves to the newest `last_interaction_date` in the file, never the system clock: the supplied dates are about 960 days old, so measuring against today would score every lead cold.

**`source`** must be one of the six keys of `factors.source.class_lookup`: `Inbound demo request`, `Content download`, `Webinar attendee`, `Referral`, `LinkedIn outreach`, `Sales call`. Any other value drops the factor and sends the lead to REVIEW.

## 4. Scoring model

Each factor produces a raw score from 1 to 10 and carries two weights:

| Factor | `score_weight` | `urgency_weight` | Raw score |
|---|---|---|---|
| industry | 1.75 | 0.0 | tier from the tiering call: `tier_1` 9.0, `tier_2` 6.0, `tier_3` 3.0 |
| company_size | 1.25 | 0.0 | interpolated in band: startup 1–10 → 1.0–3.0, smb 11–500 → 4.0–7.0, mid_market 501–5,000 and enterprise 5,001+ → 8.0–10.0 (capped at 10,000) |
| recency | 1.0 | 1.0 | interpolated by days since contact: ≤30 → 9.0–8.0, ≤60 → 8.0–6.0, ≤90 → 6.0–4.0, beyond → 4.0–2.0 (capped at 180 days) |
| source | 0.75 | 0.6 | `buyer_initiated` 9.0, `seller_initiated` 4.0 |

Size and recency interpolate linearly inside their band, so scores never jump at a boundary; source and industry are categorical.

- **Fit score** = weighted mean of present factors using `score_weight`; a missing factor is dropped and the rest renormalised, never imputed.
- **Urgency score** = weighted mean using `urgency_weight`, carried only by recency and source.
- **Priority score** = 0.8 × fit + 0.2 × urgency (`priority.blend`), falling back to fit alone when no urgency factor is present.
- **Priority bands**: HIGH from 7.5, MEDIUM from 5.0, otherwise LOW.

All three are rounded to one decimal (`runtime.score_precision`) before anything reads them.

**Decision.** Fit at or above `decision.qualify_cutoff` (7.0) → QUALIFIED, below → REJECTED. Three guardrails then run, each able to override the decision, never a score:

| Guardrail | Fires when | Sets |
|---|---|---|
| `no_scoreable_factors` | every factor is missing, so no fit score exists | REVIEW |
| `incomplete_record` | any required field is null, a sentinel, or present but unreadable | REVIEW |
| `borderline_score` | fit score within ±0.25 of the cutoff (`decision.review_margin`), inclusive | REVIEW |

Every matching rule is recorded in `guardrails_fired`, so missing or unreadable data never causes REJECTED. **Completeness** is the fraction of the six columns that were usable, computed after both checks.

**Queue position** (`priority_rank`) orders all scored leads by priority score, fit score, recency days ascending, company size, then `lead_id`. The full chain makes the order reproducible.

**Reasoning string.** Each lead carries a one-line trace, one segment per factor:

```
fit 9.0/10, urgency 8.3, priority 8.8 [HIGH] | source: 9.0/10 x w0.75 -> +1.42 (Content download -> buyer_initiated) | industry: ... | recency: ...
```

Each segment shows raw score, weight, contribution and source value; a dropped factor reads `DROPPED (reason)`.

## 5. LLM boundaries

Both calls use `openai/gpt-oss-120b` on Groq's OpenAI-compatible endpoint, with a 60-second timeout. Their contracts are deliberately opposite, so each has its own client and parser.

| | Tiering | Messaging |
|---|---|---|
| Unit of work | unique industry strings | QUALIFIED leads, by variant |
| Batch size | `llm.batch_size` = 25 strings | `llm_messages.batch_size` = 4 leads |
| Temperature | 0.0 | 0.7 |
| `max_tokens` | 2,000 | 4,500 |
| On truncation | fail closed: reply discarded whole, batch split once into halves | salvage: keep complete messages, re-send the missing leads |
| Retries | `llm.max_retries` = 3 per label list, for transport, 429, 5xx and parse failures | `llm_messages.content_retries` = 1, each attempt with up to 3 transport attempts |
| Cache scope | shared across input files | one file per input |
| Cache key | exact industry string | `sha256(first_name\|company\|industry\|source\|variant)` |

Auth, permission and bad-request errors are never retried. Backoff is `llm.retry_backoff_seconds` (2.0 s) × attempt number.

**Tiering recovery.** A batch is classified in two passes: the first sends every label, the second re-sends once only those that did not come back aligned — absent from the reply, spelt differently, or carrying a tier outside `tier_scores`. The second runs only if the first recovered something, since a first pass that recovered nothing has already spent its retries on those labels, so one label list costs at most 2 × `llm.max_retries` HTTP calls. A truncated reply instead splits the batch once into two halves, each going through the same two passes; a half that truncates again is not split further and its labels stay unclassified, routing those leads to REVIEW. `missing_label_resends` and `truncation_splits` are recorded in the run report.

**Messaging prompt.** `org_profile` + variant instructions + `shared_rules` + a JSON array of five fields per lead (`lead_id`, `first_name`, `company`, `industry`, `source`). No score reaches the model.

**Variant selection** happens in code, before any call: MEDIUM or LOW band → `v3_generic`; else urgency above fit → `v2_engagement`; else `v1_value`.

**Without an API key** the run still produces a report: messaging warns once and serves whatever the cache holds, and a QUALIFIED lead with no entry is recorded as failed. Tiering is the exception — uncached strings stop the run, because proceeding would silently send those leads to REVIEW.

## 6. Rate-limit envelope

The build runs within Groq's free tier, which allows 8,000 tokens per minute for this model. That limit applies to the account, so tiering and messaging draw on the same budget.

`pipeline.TokenPacer` enforces it, and is passed into the run rather than held globally, so a caller can supply one. It meters a 60-second sliding window of reported usage against a budget of 8,000 × 0.85 headroom = 6,800 tokens, on one ledger for both stages. Each call reserves its stage's `max_tokens + 1,000` (3,000 tiering, 5,500 messaging) and sleeps until it fits; an empty window always proceeds, so the loop always exits. On a 429 the pacer charges the full reservation, then for messaging waits 60 seconds and re-raises, leaving the module's own transport-retry counter to bound the attempts.

A messaging reservation of 5,500 leaves 1,300 of the 6,800 budget for the rest of the window, so each messaging call waits for the previous to age out. The pipeline therefore makes about one call per minute, and runtime scales with calls, not leads. A 50-lead run took 525.2 s over 2 tiering and 8 messaging calls; a 100-lead run made 1 tiering call and 10 message batches for 29,640 tokens. **100 leads take roughly 15–20 minutes.**

## 7. Caching

**Tier cache** (`industry_tier_cache.json`). Keyed by the exact industry string, shared across every input file, written after any live classification, so the same string always receives the same tier. The committed cache covers every industry string in all four files in `leads/`, so any of them scores with no tiering call.

**Message cache** (`<stem>_message_cache.json`). Keyed by `sha256(first_name|company|industry|source|variant)`, never by `lead_id`, one file per input. Records store `lead_id` for audit only; a mismatch there is reported as `lead_id_drift`.

**What the keys do not cover.** Neither key includes the prompt text or the model, and the message key excludes the vendor profile and temperature too. Changing any of them serves stale entries, so **a prompt change requires purging the affected cache entries** — otherwise a message written under the old prompt is replayed as though it followed the new one. Records store `variant`, which makes a targeted purge possible.

**Weights and scores.** Scores are never cached, so after a weight change only leads whose decision or variant changed need new messages.

## 8. Failure handling

- **Bounded, nested retries.** For messaging, transport retry sits inside content retry: worst case 2 content × 3 transport = 6 requests per batch, after which the remaining leads are flagged. Tiering's bounds are in §5. Every loop has an exit condition.
- **Alignment by `lead_id`.** A message is accepted only if its `lead_id` was sent in that batch, is the first object for that ID, and its text is non-empty and passes the leak check. Unknown and duplicate IDs are recorded, not used.
- **Salvage and re-send.** Complete objects from a truncated messaging reply are kept, and only the missing leads go to the content retry. Tiering never keeps part of one.
- **Error bodies surfaced.** On a non-200 the provider's own error text is reported, never reduced to a status code.
- **Named User-Agent.** Every request sends one. Default library user agents are blocked at the provider's edge (Cloudflare error 1010).
- **Leak check.** Every message is checked for scoring vocabulary before acceptance: `fit`, `score`, `priority`, `rank`, `qualified` and their inflections, whole-word, plus the substrings `tier_`, `{`, `[`. `find_leaked_term()` returns the flagged term, stored with the reason as `leaked_internal:<term>`.
- **Failure reasons.** A lead left without a message carries one of `api_error:<detail>`, `parse_failure`, `truncated`, `missing_from_reply`, `empty_message` or `leaked_internal:<term>` in `message_fail_reason`.
- **Degraded paths.**
  - Zero QUALIFIED leads: messaging is skipped and the report carries an empty sample list and a `sample_note`.
  - A lead with every factor missing is processed but not scored, and goes to REVIEW; the summary reports `total_processed` and `scored` separately.
  - No API key: see §5.
  - A missing file, column, config file or config block stops the run with an error naming it; the command exits 1 with no traceback.

## 9. Configuration map

| Block | Controls | Status |
|---|---|---|
| `meta` | rubric version, calibration basis, revision history, description | descriptive |
| `runtime` | processing date, score precision, `lead_id` prefix | frozen |
| `missing_data` | sentinel values, required fields | frozen |
| `factors` | weights, bands, tiers, source classes, caps | frozen (calibrated) |
| `priority` | fit/urgency blend, priority bands | frozen (calibrated) |
| `decision` | cutoff, review margin, capacity context (not used in scoring) | frozen (calibrated) |
| `guardrails` | which review rules are enabled | frozen |
| `llm` | tiering call: endpoint, model, key variable, temperature, tokens, batch size, retries, and `tier_prompt` | frozen |
| `llm_messages` | messaging call settings, variant selection, prompts, cache and report names | tunable |
| `report` | reason vocabulary and codes, display order, sample rules, output naming, CSV columns | tunable |

"Frozen" means set on the calibration file and unchanged since.

**Where target-organisation text lives.** Retargeting the tool means rewriting `llm.tier_prompt`, `llm_messages.org_profile`, `llm_messages.variants`, `llm_messages.shared_rules`, the `detail` texts in `report.reason_lookup`, and `meta.description`. All six are in `config.yaml`, so a new target organisation is a config change, not a code change — though changing the tiering prompt invalidates the tier cache, and changing the messaging prompts invalidates the message caches (§7).

## 10. Outputs

Every file a run writes lands in `--out-dir`, which defaults to `output/`; the tier cache is the exception, being shared across input files, so it stays beside `config.yaml`. Every output name derives from the input stem (`report.naming`, `leads_` stripped), so no run overwrites another's files.

| File | Purpose |
|---|---|
| `<stem>_output_report.json` | the full report |
| `<stem>_output_report.csv` | one row per lead in queue order, for a sales team (UTF-8 BOM, for Excel) |
| `<stem>_scored.csv` | per-lead scores, per-factor raw scores, reasoning |
| `<stem>_run_report.json` | scoring and tiering diagnostics |
| `<stem>_message_run_report.json` | per-batch messaging diagnostics, rates |
| `<stem>_message_cache.json` | message cache for this input file |

Keys of the report JSON: `summary` (decision counts, rates with their `n` and denominator, common rejection and review reasons, band counts, message counts, capacity context); `queue` (leads grouped by decision, each group in queue order); `leads` (one record per lead: scores, decision, reasoning, factor contributions, guardrails fired, outcome reasons, message fields); `sample_messages` (3–5, chosen deterministically); `run_diagnostics` (tokens, batches and rates per stage); `sample_note` and `build_warnings`.

A committed 50-lead example is in `sample_run/`.

## 11. Extension points

Config alone covers every tuning change: factor weights (`score_weight` / `urgency_weight`, both 0.0 disables a factor), the size and recency `bands`, `factors.source.class_lookup`, `decision`, `priority.bands`, `report.naming`, and a new rejection reason as a `report.reason_lookup` entry with its binding in `report.rejection_factor_codes`. Retargeting at another vendor is the six text locations in §9, plus a cache purge.

Two changes need code as well as config:

| Change | What to touch |
|---|---|
| Add a fifth factor | a config block with both weights, a scorer in `rubric_scorer.py` called from `score_lead()`, and an `UNSCOREABLE_FIELDS` entry if it reads a required column |
| Add an auto-reject rule | a branch in `apply_guardrails()` and an entry in `guardrails`, after the review rules |

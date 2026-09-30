# Decisions — Lead Qualification Tool

*Describes build 1.2.0-dev. Each entry records what was observed, what was decided (and what was rejected), and where the result lives in the code. For the system itself, see [ARCHITECTURE.md](ARCHITECTURE.md).*

## Rubric

**1. Industry fit judged on an operating-model axis, and cached**

- **Observed:** The lead files contain 48 distinct industry strings, including near-synonyms (SaaS / Software / Enterprise Software; Healthcare / HealthTech) that a literal lookup would score differently for no defensible reason.
- **Decided:** Tier each industry by how the prospect's own customer operations run (digital, mixed, physical), judged by a model, rather than maintaining a fixed list of verticals. The judgement is cached, so the same string always gets the same tier.
- **Shipped:** `llm.tier_prompt`, `factors.industry.tier_scores`, `industry_tier_cache.json`.

**2. Recency scored continuously, not in flat bands**

- **Observed:** With flat bands, a 1-day-old and a 29-day-old lead both scored 9.0 while a 31-day-old dropped to 6.0: a 3.0-point cliff across two days, in a quantity that decays smoothly.
- **Decided:** Interpolate linearly inside each segment, segments meeting at their endpoints so the score has no jumps. Finer bands were rejected; they move the cliffs rather than removing them.
- **Shipped:** `factors.recency.bands` (`score_range` per segment), `score_recency()`. `company_size` uses the same mechanism.

**3. Calibrate on one dataset, then freeze**

- **Observed:** With no conversion outcomes, any number set by looking at a file's results is fitted to that file.
- **Decided:** Every weight, band and cutoff was set on the 29-lead calibration file alone, then frozen before any other file was scored. The one revision to a number (continuous recency) was made before the other files were scored, from a defect found on the calibration file. Revision 1.2 changed routing only, and no weight, band or cutoff with it. Adjusting values after seeing other files' results was rejected as fitting.
- **Shipped:** `meta.version` (`1.2-frozen-on-training`), `meta.calibration_basis`, `meta.held_out`, `meta.revision_history`.

**4. Decision kept separate from queue position**

- **Observed:** The decision comes from the fit score; queue position comes from the priority score, which also weighs urgency. The two cross over: on the calibration file a REVIEW lead (fit 6.9) sits one queue position below a REJECTED lead (fit 6.5).
- **Decided:** Keep both quantities, and group the report by decision so the crossover reads correctly. A single flat ranking was rejected because it makes the crossover look like a bug.
- **Shipped:** `report.display.decision_order`, `group_queue_by_decision()`, the `priority_rank` field.

**5. Uncertain leads go to REVIEW; unusable data never causes REJECTED**

- **Observed:** Missing values arrive as nulls and as placeholder strings such as "Unknown Sector". A lead scored on three of four factors can land near the cutoff for reasons the data cannot settle.
- **Decided:** A fit score within ±0.25 of the cutoff, any field that is null, a placeholder or unreadable (#22), or no scoreable factors sends the lead to REVIEW. Guardrails can override a decision but never a score. Imputing missing values was rejected: zero silently disqualifies, a middle value silently inflates.
- **Shipped:** `guardrails`, `decision.review_margin`, `apply_guardrails()`.

**6. Cutoff set against team capacity, not accuracy**

- **Observed:** There is no ground truth to be accurate against, but there is a hard operational limit: the team works about 60 leads a month out of about 1,200.
- **Decided:** Choose the cutoff by sweeping candidate values against that capacity rather than picking a round number. At 7.0, 11 of the 29 calibration leads emit QUALIFIED. Qualified volume still runs several times capacity, which is why the output is a priority queue, not a shortlist.
- **Shipped:** `decision.qualify_cutoff`, `decision.business_context`, `cutoff_sweep()`.

**7. Enterprise scored level with mid-market**

- **Observed:** The stated ideal customer profile is 50–500 employees, yet larger companies bring more users, more operations and more lifetime value once won.
- **Decided:** Score the enterprise band (5,001+) on the same 8.0–10.0 range as mid-market, interpolated by headcount. "Stickiness" here means post-win lifetime value and expansion, not incumbent lock-in. Scoring enterprise down to match the profile was rejected. This is a recorded divergence from the profile, not an oversight.
- **Shipped:** `factors.company_size.bands`, `factors.company_size.upper_cap`.

## LLM integration

**8. Messages batched 4 per call, not 25**

- **Observed:** With 25 leads per call, the seven engagement-variant messages came back in one reply at 0.85 pairwise similarity — one message with the names changed.
- **Decided:** Four leads per call. Similarity fell sharply and every message landed in the 50–60 word target. The cost is 52% more tokens and more calls, accepted on output quality. The measurement was a single run per setting, and is recorded as such.
- **Shipped:** `llm_messages.batch_size: 4`.

**9. Truncation controlled by `max_tokens`, not batch size**

- **Observed:** The model is a reasoning model, and its reasoning tokens draw on the same completion budget as the output. Two-entry calls ranged from 901 to 2,549 completion tokens, so entry count barely predicts demand.
- **Decided:** Treat the completion budget as the lever for truncation and raise it. Lowering the batch size to prevent truncation was rejected; a four-lead batch truncated while a seven-lead batch did not.
- **Shipped:** `llm_messages.max_tokens: 4500`, recorded in every message run report.

**10. Model output matched to leads by ID, never by position**

- **Observed:** A four-lead batch truncated and returned two complete messages. Matched by position, those two would have been written onto the wrong two leads, silently and plausibly.
- **Decided:** Accept a message only when its echoed `lead_id` was in the batch sent; keep every complete object from a truncated reply; re-send only the missing leads. Positional matching was rejected outright. In that run the retry re-sent exactly the two missing leads and the batch completed.
- **Shipped:** `match_replies_to_leads()`, `parse_message_reply()`, `llm_messages.content_retries`.

**11. Tiering fails closed; messaging salvages**

- **Observed:** A truncated tiering reply cannot be trusted as a whole: keeping part of it risks scoring strings that were never properly classified. A truncated messaging reply still contains complete, usable messages.
- **Decided:** Give each call its own client and parser with opposite truncation behaviour, rather than one shared function. Tiering discards the whole reply; messaging keeps what completed. Failing closed governs what is *kept*, not whether to retry: tiering now retries a truncated batch by splitting it (#23), still without keeping any part of the reply.
- **Shipped:** `call_tiering_api()` in `pipeline.py`; `call_message_api()` in `message_generator.py`.

**12. Free-tier pacing treated as an operating envelope**

- **Observed:** Two early runs stopped against the 8,000 tokens-per-minute ceiling with the code unchanged. A reply cut off before any output still consumes its full completion budget.
- **Decided:** Pace calls against the limit instead of treating it as a failure rate, and charge tiering and messaging to one shared ledger, because the provider counts both against the same window. Pacing inside the messaging module was rejected; the limit belongs to the account, not the stage.
- **Shipped:** `TokenPacer` and `make_paced_message_client()` in `pipeline.py`, injected via `generate_messages(client=...)`.

**13. Prompt examples must obey the prompt's own rules**

- **Observed:** The example message in the balanced variant's prompt demonstrated a claim about the prospect's tools that the shared rules forbid, and the model reproduced it in a generated message.
- **Decided:** Treat an example as an instruction: it outranks a prohibition stated in prose, so it must satisfy every rule beside it.
- **Shipped:** The example in `llm_messages.variants.v3_generic` was rewritten and now meets the rule: 53 words, no leak-check term, no tool attributed to the prospect. Because a cache key excludes the prompt text, entries written under the old example were purged rather than replayed.

**14. Leak check as a code-layer output guard**

- **Observed:** The likeliest failure in generated copy is scoring vocabulary leaking into a message, and a prompt rule against it cannot guarantee compliance. Across the two held-out files the check fired twice in 39 qualified leads, and both leads ended without a message.
- **Decided:** Check every message in code before accepting it — whole-word scoring terms plus `tier_`, `{` and `[` — and reject on a match. Relying on the prompt rule alone was rejected. The flagged term is recorded with the reason, so a rejection can be read without opening the message.
- **Shipped:** `LEAK_WORDS`, `find_leaked_term()`, `message_fail_reason = leaked_internal:<term>`.

**15. Tier cache shared across files; message cache per file**

- **Observed:** The full-scale file contains all 29 calibration leads. With a shared message cache, its 11 qualified ones would be served from disk and silently excluded from the coverage rate.
- **Decided:** Share the tier cache, since classification must be consistent everywhere. Keep one message cache per input file, keyed by a hash of the payload and variant, never by `lead_id`, which is positional. A single shared message cache was rejected.
- **Shipped:** `industry_tier_cache.json`, `report.naming.message_cache`, `message_cache_key()`.

## Reporting and data

**16. Rejection reasons derived from the rubric's lowest bands**

- **Observed:** No field carried a rejection reason: `guardrails_fired` was empty on all 13 rejected calibration leads. A wider "below midpoint" threshold labelled six in-profile companies (55–450 employees) "below target size".
- **Decided:** A factor earns a reason only in its lowest band or class. Several reasons on one lead are ordered by how much each cost, so the binding constraint reads first. The vocabulary is closed: an unmapped code is surfaced, never treated as a new category.
- **Shipped:** `report.reason_lookup`, `report.rejection_factor_codes`, `derive_outcome_reasons()`, `_shortfall()`.

**17. Every rate states its denominator**

- **Observed:** Two figures are true at the cutoff: 14 of 29 calibration leads score at or above 7.0, but 11 emit QUALIFIED once the review margin applies. Quoting one without saying which misstates what the system does.
- **Decided:** Every rate carries its `n` and names its denominator. Projections are computed from raw counts, never from a rounded percentage, because rounding twice shifts the result.
- **Shipped:** `_rate()` and the `n` / `denominator` fields throughout `summary` and `run_diagnostics`.

**18. Provider error bodies always surfaced; named User-Agent**

- **Observed:** Early integration failures were diagnosis problems, not code defects. One was a block at the provider's edge (Cloudflare error 1010) that fingerprints default library user agents, and it looked opaque until the response body was printed.
- **Decided:** Send a named User-Agent on every request, and always keep the provider's error text rather than a bare status code. Catch-all error suppression was rejected.
- **Shipped:** Both LLM clients; `message_fail_reason = api_error:<detail>`.

**19. Input validated against the data, not its documentation**

- **Observed:** The dataset's accompanying notes did not match the data. They gave 30 calibration rows where there were 29, a later start date for the full-scale file, and blank cells for missing values where the data also used the string `NA`.
- **Decided:** Take the data as the source of truth. Validate the actual columns and treat every observed missing-value encoding as missing.
- **Shipped:** `read_leads()`, `validate_leads()`, `missing_data.sentinel_values`, `runtime.processing_date: auto`.

**20. The pipeline is stage functions with settings passed in**

- **Observed:** The notebook held the config, key, pacer and output paths in session globals, so no stage could be called on its own, or exercised without a live key.
- **Decided:** Move it into `pipeline.py`, where every stage takes what it needs as an argument, both LLM clients are injectable, and importing has no side effects. Module-level state was rejected: it cannot be driven a stage at a time, nor verified without the network.
- **Shipped:** `pipeline.py`, `run_pipeline()`, `cli.py`.

**21. A run without an API key still produces a report**

- **Observed:** The notebook raised before any work when no key was set, although the committed caches cover the sample file completely, so a fresh clone was unusable.
- **Decided:** With no key, messaging warns once and serves the cache; a qualified lead with no cache entry is recorded as failed. Tiering still stops the run, because an unclassified industry changes a lead's decision. Failing the whole run was rejected, and so was letting tiering proceed.
- **Shipped:** `run_pipeline()`, and the `sample_run/` cache that makes the sample replayable.

**22. A value that cannot be read is treated as missing**

- **Observed:** The `V1_INCOMPLETE` reason text already read "missing or unreadable", but the code checked only for blanks and placeholders. An unrecognised source, an unclassified industry, an unreadable size or date dropped its factor and the lead was decided on the rest, with nothing recorded to say so.
- **Decided:** A present field whose factor could not be scored joins `missing_fields`, so the guardrail in #5 routes the lead to REVIEW. This applies the existing rule rather than adding one: no new reason code, guardrail or report field.
- **Shipped:** `UNSCOREABLE_FIELDS`, `score_lead()`, `meta.version` `1.2-frozen-on-training`.

**23. Tiering recovers partial and truncated replies**

- **Observed:** A reply omitting some labels was returned as-is and those strings were never asked about again; a truncated reply discarded the batch with no retry. Under #22 both now send leads to REVIEW, so a dropped label has a visible cost.
- **Decided:** Re-send the missing labels once, and split a truncated batch into two halves, each through the same bounded path. Splitting further was rejected as unbounded. Nothing partial is kept from a truncated reply, so #11 is unchanged.
- **Shipped:** `classify_industry_batch()`, with `missing_label_resends` and `truncation_splits` in the run report.

## Limitations and blockers

- **No ground-truth outcomes.** No lead carries a conversion result, so the rubric is checked for consistency, not accuracy.
- **Four firmographic factors only.** There is no budget, timeline, job-title or behavioural signal.
- **Calibrated on 29 leads.** Every frozen value rests on one small file.
- **Not a blind test.** Size, industry and date coverage in the other datasets were inspected before the freeze.
- **Placeholder scores.** Some factor scores follow a low/medium/high convention pending a real stakeholder.
- **Messages vary across files.** At temperature 0.7, messages are reproducible only when re-running the same file from its own cache.
- **Pacing sets runtime.** The free-tier limit, not the code, determines how long a run takes.
- **One target organisation.** The rubric, prompts and caches hold only for the single target organisation.
- **Blunt leak check.** It matches ordinary words wherever they appear, including inside a company's own name: a message for Perfect Fit Enterprise was rejected on the word "fit". The flagged term is now recorded with the reason, so a false positive can be told from a real leak without opening the message.
- **One retry for messages.** A lead whose message fails after one content retry stays without a message.
- **Command line only.** The tool runs from `cli.py`; there is no web interface.

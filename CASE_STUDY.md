# Case Study — Lead Qualification Tool

*Describes build 1.1.2. For how the tool works internally, see [ARCHITECTURE.md](ARCHITECTURE.md).*

## The problem

A mid-market SaaS company receives around 1,200 inbound leads a month. Its sales team qualifies and contacts about 60 of them, so roughly 95% of inbound interest is never worked. Every decision is made by hand, and each one takes a rep 8–12 minutes. The company suspects it is missing good opportunities while spending effort on poor fits, but it has no systematic way to tell which is which.

The goal: automatically qualify leads, sequence outreach, and surface insights the sales team can act on immediately.

## Target organisation and assumptions

The tool is built for one hypothetical target organisation: a vendor of a CRM/ERP suite for customer onboarding, engagement and post-sale support. Its buyer is whoever owns customer operations at the prospect — a customer-operations, customer-success or revenue-operations lead, not IT or finance. The stated ideal customer profile is a company of 50–500 employees in an industry whose customer interactions are serviced digitally.

That specificity is deliberate. The factor weights, score bands, qualification cutoff, message prompts and cached industry judgements all encode this organisation's view of a good lead, and they hold only for it. A vendor selling a different product to a different buyer would need its own rubric.

No conversion outcomes exist for any of the leads — there is no record of which ones became customers — so the rubric cannot be measured for accuracy. It is built instead to be judged on clarity (every rule is written down), consistency (similar leads get the same treatment) and defensibility (every decision traces to a stated reason).

## What the tool does

The tool takes a CSV of leads and runs them through a single pipeline:

1. **Ingest** the CSV and check it has the expected columns.
2. **Classify industry fit.** A language model sorts each distinct industry label by how digitally that kind of business serves its own customers. This step is called *tiering*.
3. **Score four factors** — industry fit, company size, how recently the lead last engaged, and how the lead arrived — into a fit score out of 10.
4. **Decide** each lead: QUALIFIED, REVIEW or REJECTED.
5. **Order a priority queue**, so the team knows which qualified lead to contact first.
6. **Write a personalised first message** for each QUALIFIED lead. This step is called *messaging*.
7. **Produce a report** with every lead's decision and reasons, summary statistics and sample messages.

## Value to the target organisation

**Every lead gets a decision.** Today about 60 of 1,200 leads a month receive a considered decision; the rest are never examined. The tool gives every lead a decision with a traceable reason: which factors scored well or badly, and by how much.

**Uncertain leads go to a person, not a guess.** A lead whose fit score sits close to the cutoff, or whose record is incomplete, is routed to REVIEW for a human to decide. Missing data never causes a rejection.

**A queue, not a shortlist.** At the calibrated cutoff, qualified volume is several times the ~60 leads the team works today. A plain yes/no list would still leave reps choosing by hand, so qualified leads are ordered by a priority score that blends the fit score with an urgency score based on how recently the lead engaged and how it arrived. Each lead's queue position tells the team who to contact first.

**Time.** Reviewing all 1,200 leads a month by hand would take 160–240 rep-hours. The tool processes 100 leads unattended in roughly 15–20 minutes within free-tier limits.

**Insight into lead quality.** Every rejection carries a reason from a fixed vocabulary, such as "Industry outside core verticals" or "Low-intent channel", so the report shows where inbound lead quality is weak. In the committed sample run of 50 leads, industry fit was the most common reason, cited for 13 of the 23 rejected leads.

## Constraints

- **Free-tier model access.** Both model calls run on Groq's free tier, which allows 8,000 tokens per minute, shared between tiering and messaging. The tool paces its calls to stay inside that limit, and the pacing sets the runtime.
- **Batching.** Batched model calls were a requirement from the outset: the model is never called once per lead.
- **No ground truth.** Without conversion outcomes, results can be checked only for internal consistency, not against what actually happened.
- **Messy input.** The data contains missing values, "Unknown" placeholders, company sizes stored as decimals, and interaction dates about 960 days old. The dataset's accompanying notes did not match the data itself, so input is validated against the data, not its documentation.
- **Notebook runtime.** The tool runs as a notebook session in Google Colab.

## What was achieved

Build 1.1.2 delivers:

- A four-factor rubric with explicit weights, and traceable per-lead reasoning that shows how much each factor contributed to the fit score.
- Human-review routing for borderline and incomplete leads.
- Batched, paced model calls with bounded retries, so a failing call is retried a fixed number of times and then reported, never retried indefinitely.
- Model outputs matched to leads by ID, never by position, so a reordered or partial reply cannot attach one lead's message to another lead.
- Three message variants — value-led, engagement-led and balanced — chosen by the rubric, not by the model.
- Rejection reasons derived from the rubric itself and ordered by which factor cost the lead the most.
- Content-hash caching of industry judgements and messages.
- Reproducible re-runs: running the same file again gives the same scores, decisions and messages.

## Scale

Processes up to 100 leads per run in roughly 15–20 minutes within Groq free-tier limits. Runtime depends on the number of model calls, not the number of leads, because the free-tier limit allows about one call per minute.

## Limitations

The three most material limits: there are no conversion outcomes, so the rubric is defensible but unvalidated; it was calibrated on 29 leads, and the other datasets were inspected before the rubric was frozen, so they are not a blind test; and the rubric, prompts and caches hold only for the single target organisation. The full list is in [DECISIONS.md](DECISIONS.md#limitations-and-blockers).

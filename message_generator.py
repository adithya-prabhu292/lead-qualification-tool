"""
message_generator.py
====================
Outreach message generation (messaging) for the Lead Qualification Tool.

Contains no business text. Every prompt, rule and number is read from the
`llm_messages:` block of config.yaml. The module supplies mechanism only:
client, parser, variant selection, batching, reconciliation, cache, flagging.

Relationship to rubric_scorer.py
--------------------------------
`call_llm()` and `extract_json_array()` are NOT reused and NOT modified.
Tiering must fail closed on truncation; this stage must salvage from it.
The two behaviours cannot live in one function, so this module has its own
client (`call_message_api`) and its own parser (`parse_message_reply`).

Pipeline per run:
    assert lead_id uniqueness
    -> select variant per lead (code, no API call)
    -> build 5-field payloads
    -> cache lookup, split hits from misses
    -> group misses by variant, chunk at batch_size
    -> per batch: attempt 1 -> salvage -> match_replies_to_leads
                  -> content retry with ONLY the missing leads
                  -> write cache -> flag the remainder
    -> per-lead fields + batch diagnostics + measured rates
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

VARIANT_ORDER = ("v1_value", "v2_engagement", "v3_generic")

# Code-layer leak check. Not an injection defence —
# it catches the failure this stage is actually likely to produce, which is
# scoring vocabulary bleeding into copy. Word-boundary matched so "benefit"
# and "profit" do not trip "fit". Override by adding `leak_tokens:` to the
# llm_messages block if a term proves to be a live false positive.
LEAK_WORDS = (
    "fit", "fits", "score", "scores", "scoring", "priority", "priorities",
    "rank", "ranked", "ranking", "qualified", "qualification",
)
LEAK_SUBSTRINGS = ("tier_", "{", "[")

MESSAGE_FIELDS = {
    "message": None,
    "message_generated": False,
    "message_variant": None,
    "message_fail_reason": None,
    "message_word_count": None,
}


class MessageLLMError(Exception):
    """Carries whether the failure is worth retrying. Auth, permission and
    bad-request errors are not — retrying them only obscures the cause."""

    def __init__(self, message: str, retryable: bool, body: str = ""):
        self.retryable = retryable
        self.body = body
        super().__init__(message)


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------
def call_message_api(prompt: str, cfg: dict, api_key: str | None = None) -> dict:
    """Returns {"content", "finish_reason", "usage", "latency_s"}.

    Differs from rubric_scorer's call_llm() in exactly one behaviour: on
    finish_reason == "length" it RETURNS the partial content instead of
    raising, so the parser can salvage the completed entries.

    Everything else carries over unchanged: named User-Agent (Cloudflare edge
    block, error 1010), the provider's response BODY surfaced on non-200,
    retryable only on connection failure / 429 / 5xx, no bare except.

    `api_key` is an explicit third argument so a Colab secret can be passed in;
    it falls back to the environment variable named in `llm.api_key_env`.
    """
    llm = cfg["llm"]
    msgs = cfg["llm_messages"]

    if api_key is None:
        import os
        api_key = os.environ.get(llm["api_key_env"])
    if not api_key:
        raise MessageLLMError(
            f"no API key: {llm['api_key_env']} unset and none passed in",
            retryable=False,
        )

    t0 = time.time()
    try:
        resp = requests.post(
            llm["base_url"],
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "lead-intelligence/0.1",
            },
            json={
                "model": llm["model"],
                "temperature": msgs["temperature"],
                "max_tokens": msgs["max_tokens"],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=llm["request_timeout_seconds"],
        )
    except requests.RequestException as e:
        raise MessageLLMError(f"transport: {e}", retryable=True, body=str(e)) from None

    latency = time.time() - t0

    if resp.status_code != 200:
        body = resp.text[:400]
        raise MessageLLMError(
            f"HTTP {resp.status_code}: {body}",
            retryable=resp.status_code == 429 or resp.status_code >= 500,
            body=body,
        )

    payload = resp.json()
    choice = payload["choices"][0]
    return {
        "content": choice["message"]["content"],
        "finish_reason": choice.get("finish_reason"),
        "usage": payload.get("usage", {}) or {},
        "latency_s": round(latency, 2),
    }


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def parse_message_reply(text: str) -> tuple[list[dict], dict]:
    """Scan a possibly-truncated JSON array and return every COMPLETE object.
    Never raises.

    Walks forward tracking brace depth while respecting string state and
    backslash escapes, so a '{' inside a message body is not counted. Each
    time depth returns to 0 the span is one complete object and is parsed
    individually — a single malformed object is skipped and counted, it does
    not take down the batch. A partial object in flight at end of input is
    discarded and truncated_tail is set.
    """
    diag = {"truncated_tail": False, "n_objects": 0, "skipped_malformed": 0}
    if not text:
        return [], diag

    cleaned = re.sub(r"```(?:json)?", "", text)
    start = cleaned.find("[")
    if start == -1:
        return [], diag

    objects: list[dict] = []
    depth = 0
    in_str = False
    escaped = False
    obj_start: int | None = None

    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start is not None:
                    span = cleaned[obj_start:i + 1]
                    try:
                        obj = json.loads(span)
                    except json.JSONDecodeError:
                        diag["skipped_malformed"] += 1
                    else:
                        if isinstance(obj, dict):
                            objects.append(obj)
                        else:
                            diag["skipped_malformed"] += 1
                    obj_start = None

    if depth > 0:
        diag["truncated_tail"] = True
    diag["n_objects"] = len(objects)
    return objects, diag


def find_leaked_term(message: str, cfg: dict | None = None) -> str | None:
    """Return the offending token, or None. Code-layer, not an injection
    defence — see the note on LEAK_WORDS."""
    words = LEAK_WORDS
    if cfg:
        override = cfg.get("llm_messages", {}).get("leak_tokens")
        if override:
            words = tuple(override)
    low = message.lower()
    for sub in LEAK_SUBSTRINGS:
        if sub in low:
            return sub
    for w in words:
        if re.search(rf"\b{re.escape(w)}\b", low):
            return w
    return None


# ---------------------------------------------------------------------------
# variant selection + payload
# ---------------------------------------------------------------------------
def select_variant(lead, cfg: dict) -> str:
    """Deterministic, from frozen rubric output, before any API call.

    Band is checked FIRST. The second test compares urgency against fit, not
    priority against fit: the two are algebraically identical but the priority
    gap is one fifth the size and all three fields are rounded to
    runtime.score_precision before anything reads them. `>=` is the tie-break.
    """
    generic = set(cfg["llm_messages"]["selection"]["generic_bands"])
    if lead.priority_band in generic:
        return "v3_generic"
    if lead.urgency_score > lead.fit_score:
        return "v2_engagement"
    return "v1_value"


def build_message_payload(lead) -> dict:
    """Five fields, nothing else. No scoring artefact reaches the model."""
    return {
        "lead_id": lead.lead_id,
        "first_name": lead.name.split()[0],
        "company": lead.company,
        "industry": lead.industry,
        "source": lead.source,
    }


def message_cache_key(payload: dict, variant: str) -> str:
    """Content hash, never lead_id. lead_id is positional — the same id means
    different people across differently ordered or filtered files, and keying
    a cache on it is exactly how a repeated run hands one lead another lead's
    message. `variant` is in the key so a changed variant cannot serve a
    message of the wrong shape."""
    return hashlib.sha256(
        "|".join([
            payload["first_name"], payload["company"],
            payload["industry"], payload["source"],
            variant,
        ]).encode("utf-8")
    ).hexdigest()


def build_message_prompt(variant: str, payloads: list[dict], cfg: dict) -> str:
    """org_profile + variant shape + shared rules + payload. All text from
    config; this function only concatenates."""
    m = cfg["llm_messages"]
    return (
        f"{m['org_profile'].strip()}\n\n"
        f"{m['variants'][variant].strip()}\n\n"
        f"{m['shared_rules'].strip()}\n\n"
        f"LEADS:\n{json.dumps(payloads, indent=2, ensure_ascii=False)}"
    )


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
def load_message_cache(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"message cache at {path} is not valid JSON: {e}") from None


def save_message_cache(cache: dict, path: Path) -> None:
    Path(path).write_text(
        json.dumps(cache, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# per-lead field plumbing
# ---------------------------------------------------------------------------
def init_message_fields(lead) -> None:
    for k, v in MESSAGE_FIELDS.items():
        setattr(lead, k, v)


def lead_to_dict(lead) -> dict:
    """asdict() plus the message fields, so the record is complete whether or
    not rubric_scorer.ScoredLead declares them."""
    d = asdict(lead)
    for k in MESSAGE_FIELDS:
        d[k] = getattr(lead, k, MESSAGE_FIELDS[k])
    return d


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# transport retry — nested INSIDE content retry
# ---------------------------------------------------------------------------
def _call_with_transport_retry(prompt, cfg, api_key, client, log):
    llm = cfg["llm"]
    max_attempts = int(llm["max_retries"])
    backoff = float(llm["retry_backoff_seconds"])
    errors: list[str] = []
    last_body = ""

    for attempt in range(1, max_attempts + 1):
        try:
            result = client(prompt, cfg, api_key)
        except MessageLLMError as e:
            errors.append(str(e))
            last_body = e.body or str(e)
            if not e.retryable or attempt == max_attempts:
                return None, attempt, errors, last_body
            time.sleep(backoff * attempt)
            continue
        return result, attempt, errors, last_body

    return None, max_attempts, errors, last_body


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------
def match_replies_to_leads(objects: list[dict], sent_ids: list[str], cfg: dict) -> dict:
    """Alignment by lead_id, never by position."""
    sent = list(sent_ids)
    sent_set = set(sent)
    recovered: dict[str, dict] = {}
    seen: list[str] = []
    duplicates: list[str] = []
    unknown: list[str] = []
    rejected: dict[str, str] = {}      # lead_id -> reason for a present-but-bad object

    for obj in objects:
        lid = str(obj.get("lead_id", "")).strip()
        if not lid:
            continue
        if lid not in sent_set:
            if lid not in unknown:
                unknown.append(lid)
            continue
        if lid in seen:
            if lid not in duplicates:
                duplicates.append(lid)
            continue                      # first occurrence wins
        seen.append(lid)

        msg = str(obj.get("message", "") or "").strip()
        if not msg:
            rejected[lid] = "empty_message"
            continue
        token = find_leaked_term(msg, cfg)
        if token:
            rejected[lid] = "leaked_internal"
            continue
        recovered[lid] = {"message": msg, "leak_token": None}

    missing = [i for i in sent if i not in recovered]
    return {
        "recovered": recovered,
        "missing_ids": missing,
        "unknown_ids": unknown,
        "duplicate_ids": duplicates,
        "rejected": rejected,
        "n_seen": len(seen),
    }


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def generate_messages(
    leads: list,
    cfg: dict,
    api_key: str | None = None,
    source_file: str = "",
    cache_path: str | Path | None = None,
    client: Callable | None = None,
    log: Callable[[str], Any] = print,
) -> dict:
    """Generate one message per lead in `leads`. Mutates each lead in place
    with the five message fields and returns the run report.

    `leads` is expected to be the QUALIFIED set — filtering is the caller's
    visible decision, not a hidden one here.
    `client` is injectable so the call can be wrapped (the notebook's token
    pacer) or replaced by a stub that needs no API key.
    """
    m = cfg["llm_messages"]
    client = client or call_message_api
    cache_path = Path(cache_path or m["cache_path"])
    batch_size = int(m["batch_size"])
    content_retries = int(m["content_retries"])
    lo_words, hi_words = (int(x) for x in m["target_words"])

    # --- input assertion ----------------------------------------------------
    ids = [l.lead_id for l in leads]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert len(ids) == len(set(ids)), f"duplicate lead_id in input: {dupes}"

    # --- selection + payloads (no API call) ---------------------------------
    for lead in leads:
        init_message_fields(lead)
        lead.message_variant = select_variant(lead, cfg)

    payloads = {l.lead_id: build_message_payload(l) for l in leads}
    keys = {l.lead_id: message_cache_key(payloads[l.lead_id], l.message_variant)
            for l in leads}
    by_id = {l.lead_id: l for l in leads}

    # --- cache lookup -------------------------------------------------------
    cache = load_message_cache(cache_path)
    cached_ids: list[str] = []
    drift_warnings: list[dict] = []
    misses: list = []

    for lead in leads:
        rec = cache.get(keys[lead.lead_id])
        if rec and str(rec.get("message", "")).strip():
            stored_id = rec.get("lead_id")
            if stored_id and stored_id != lead.lead_id:
                warn = {"cache_key": keys[lead.lead_id],
                        "stored_lead_id": stored_id,
                        "current_lead_id": lead.lead_id,
                        "company": rec.get("company")}
                drift_warnings.append(warn)
                log(f"[msg] WARNING lead_id_drift: cached as {stored_id}, "
                    f"now {lead.lead_id} ({rec.get('company')}) — same content hash, "
                    f"message still served; the input file has changed")
            lead.message = rec["message"]
            lead.message_generated = True
            lead.message_word_count = len(rec["message"].split())
            lead.message_fail_reason = None
            cached_ids.append(lead.lead_id)
        else:
            misses.append(lead)

    log(f"[msg] leads n={len(leads)} | cached n={len(cached_ids)} | "
        f"to generate n={len(misses)}")

    # --- group misses by variant, chunk at batch_size -----------------------
    groups: dict[str, list] = {v: [] for v in VARIANT_ORDER}
    for lead in misses:
        groups[lead.message_variant].append(lead)
    for v in groups:
        groups[v].sort(key=lambda l: (l.priority_rank if l.priority_rank is not None
                                      else 10 ** 6, l.lead_id))

    diagnostics: list[dict] = []
    batch_summaries: list[dict] = []

    for variant in VARIANT_ORDER:
        group = groups[variant]
        if not group:
            continue
        chunks = [group[i:i + batch_size] for i in range(0, len(group), batch_size)]
        n_chunks = len(chunks)

        for bi, chunk in enumerate(chunks, start=1):
            tag = f"{variant}  batch {bi}/{n_chunks}"
            outstanding = [l.lead_id for l in chunk]
            batch_truncated = False
            resolved_attempt = None

            for attempt in range(1, content_retries + 2):
                if not outstanding:
                    break
                send = [payloads[i] for i in outstanding]
                prompt = build_message_prompt(variant, send, cfg)

                result, t_attempts, t_errors, err_body = _call_with_transport_retry(
                    prompt, cfg, api_key, client, log
                )

                rec = {
                    "variant": variant, "batch_index": bi, "attempt": attempt,
                    "n_sent": len(send), "n_objects_returned": 0, "n_recovered": 0,
                    "missing_ids": list(outstanding), "unknown_ids": [],
                    "duplicate_ids": [], "finish_reason": None,
                    "truncated_tail": False, "skipped_malformed": 0,
                    "transport_attempts": t_attempts, "errors": list(t_errors),
                    "prompt_tokens": None, "completion_tokens": None,
                    "latency_s": None,
                }

                if result is None:
                    detail = (err_body or "unknown")[:200]
                    reason = f"api_error:{detail}"
                    for lid in outstanding:
                        by_id[lid].message_fail_reason = reason
                    diagnostics.append(rec)
                    log(f"[msg] {tag}  attempt {attempt}   TRANSPORT FAILED after "
                        f"{t_attempts} attempts")
                    for e in t_errors:
                        log(f"        {e[:200]}")
                    break

                objects, pdiag = parse_message_reply(result["content"])
                usage = result.get("usage") or {}
                rec.update(
                    n_objects_returned=pdiag["n_objects"],
                    finish_reason=result.get("finish_reason"),
                    truncated_tail=pdiag["truncated_tail"],
                    skipped_malformed=pdiag["skipped_malformed"],
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    latency_s=result.get("latency_s"),
                )
                batch_truncated = batch_truncated or pdiag["truncated_tail"] \
                    or result.get("finish_reason") == "length"

                if pdiag["n_objects"] == 0:
                    rec["errors"].append("parse: no complete object recovered")
                    for lid in outstanding:
                        by_id[lid].message_fail_reason = "parse_failure"
                    diagnostics.append(rec)
                    log(f"[msg] {tag}  attempt {attempt}   sent={len(send)}  "
                        f"recovered=0  missing={len(outstanding)}")
                    log(f"        finish={rec['finish_reason']}  PARSE FAILURE  "
                        f"raw[:200]={result['content'][:200]!r}")
                    continue

                rc = match_replies_to_leads(objects, outstanding, cfg)
                now = _now()
                for lid, payload_rec in rc["recovered"].items():
                    lead = by_id[lid]
                    lead.message = payload_rec["message"]
                    lead.message_generated = True
                    lead.message_word_count = len(payload_rec["message"].split())
                    lead.message_fail_reason = None
                    cache[keys[lid]] = {
                        "message": payload_rec["message"],
                        "variant": variant,
                        "lead_id": lid,
                        "company": payloads[lid]["company"],
                        "source_file": source_file,
                        "model": cfg["llm"]["model"],
                        "temperature": m["temperature"],
                        "word_count": lead.message_word_count,
                        "generated_at": now,
                    }

                for lid, reason in rc["rejected"].items():
                    by_id[lid].message_fail_reason = reason
                # `truncated` is keyed off finish_reason == "length" — the
                # batch hit the cap — OR a partial object left in flight. A
                # severed array whose last object happened to close cleanly is
                # still a truncation, and the parser alone cannot see that.
                hit_cap = (result.get("finish_reason") == "length"
                           or pdiag["truncated_tail"])
                for lid in rc["missing_ids"]:
                    if lid not in rc["rejected"]:
                        by_id[lid].message_fail_reason = (
                            "truncated" if hit_cap else "missing_from_reply"
                        )

                rec.update(
                    n_recovered=len(rc["recovered"]),
                    missing_ids=list(rc["missing_ids"]),
                    unknown_ids=list(rc["unknown_ids"]),
                    duplicate_ids=list(rc["duplicate_ids"]),
                )
                diagnostics.append(rec)

                log(f"[msg] {tag}  attempt {attempt}   sent={len(send)}   "
                    f"recovered={len(rc['recovered'])}   missing={len(rc['missing_ids'])}")
                log(f"        finish={rec['finish_reason']}  truncated_tail="
                    f"{'yes' if pdiag['truncated_tail'] else 'no'}  "
                    f"tokens p{rec['prompt_tokens']}/c{rec['completion_tokens']}  "
                    f"{rec['latency_s']}s")
                if rc["unknown_ids"]:
                    log(f"        unknown ids returned (never sent): {rc['unknown_ids']}")
                if rc["duplicate_ids"]:
                    log(f"        duplicate ids (first kept): {rc['duplicate_ids']}")
                if rc["rejected"]:
                    for lid, reason in rc["rejected"].items():
                        log(f"        {lid} rejected: {reason}")
                if rc["missing_ids"]:
                    label = ("tail of batch, consistent with truncation"
                             if hit_cap else "parsed cleanly, id absent")
                    log(f"        missing ({label}): {' '.join(rc['missing_ids'])}")

                outstanding = list(rc["missing_ids"])
                if not outstanding:
                    resolved_attempt = attempt
                    break

            # cache is written after every batch, not once at the end
            save_message_cache(cache, cache_path)

            still_missing = [l.lead_id for l in chunk if not l.message_generated]
            n_ok = len(chunk) - len(still_missing)
            if not still_missing:
                verdict = "OK" if resolved_attempt == 1 else "RECONCILED"
                suffix = "" if resolved_attempt == 1 else \
                    f" after {resolved_attempt - 1} content retry"
                log(f"[msg] {tag}  {verdict}  {n_ok}/{len(chunk)}{suffix}")
            else:
                log(f"[msg] {tag}  UNRESOLVED  {n_ok}/{len(chunk)}")
                for lid in still_missing:
                    log(f"        {lid}  reason={by_id[lid].message_fail_reason}")

            batch_summaries.append({
                "variant": variant, "batch_index": bi, "n_leads": len(chunk),
                "n_generated": n_ok, "n_failed": len(still_missing),
                "truncated": batch_truncated,
                "clean_first_pass": (not still_missing and resolved_attempt == 1),
                "failed_ids": still_missing,
            })

    # --- report -------------------------------------------------------------
    generated = [l for l in leads if l.message_generated]
    failed = [l for l in leads if not l.message_generated]
    sent_leads = [l for l in leads if l.lead_id not in cached_ids]

    report = {
        "stage": "message_generation",
        "generated_at": _now(),
        "source_file": source_file,
        "rubric_version": cfg["meta"]["version"],
        "model": cfg["llm"]["model"],
        "temperature": m["temperature"],
        "max_tokens": m["max_tokens"],
        "batch_size": batch_size,
        "content_retries": content_retries,
        "target_words": [lo_words, hi_words],
        "n_leads": len(leads),
        "n_cached": len(cached_ids),
        "n_sent": len(sent_leads),
        "n_generated": len(generated),
        "n_failed": len(failed),
        "cached_ids": cached_ids,
        "lead_id_drift": drift_warnings,
        "variant_counts": {
            v: sum(1 for l in leads if l.message_variant == v) for v in VARIANT_ORDER
        },
        "batches": batch_summaries,
        "batch_diagnostics": diagnostics,
        "leads": [
            {
                "lead_id": l.lead_id,
                "company": l.company,
                "priority_rank": l.priority_rank,
                "priority_band": l.priority_band,
                "message_variant": l.message_variant,
                "message_generated": l.message_generated,
                "message_fail_reason": l.message_fail_reason,
                "message_word_count": l.message_word_count,
                "message": l.message,
            }
            for l in sorted(leads, key=lambda x: (x.priority_rank or 10 ** 6))
        ],
    }
    report["rates"] = compute_message_rates(leads, report)
    return report


# ---------------------------------------------------------------------------
# rates — every rate carries n= and a stated denominator
# ---------------------------------------------------------------------------
def compute_message_rates(leads: list, report: dict) -> dict:
    lo, hi = report["target_words"]
    cached = set(report["cached_ids"])
    sent = [l for l in leads if l.lead_id not in cached]
    diags = report["batch_diagnostics"]
    first = [d for d in diags if d["attempt"] == 1]

    n_sent = len(sent)
    # recovered on the first attempt, per diagnostics
    n_first_recovered = sum(d["n_recovered"] for d in first)

    per_variant = {}
    for v in VARIANT_ORDER:
        dv = [d for d in first if d["variant"] == v]
        sent_v = sum(d["n_sent"] for d in dv)
        rec_v = sum(d["n_recovered"] for d in dv)
        if sent_v:
            per_variant[v] = {
                "n_sent": sent_v, "n_recovered": rec_v,
                "coverage_pct": round(100 * rec_v / sent_v, 1),
            }

    n_batches = len(report["batches"])
    n_clean = sum(1 for b in report["batches"] if b["clean_first_pass"])
    n_trunc = sum(1 for b in report["batches"] if b["truncated"])

    words = [l.message_word_count for l in leads if l.message_generated
             and l.message_word_count is not None]
    in_target = [w for w in words if lo <= w <= hi]
    prompt_tok = sum(d["prompt_tokens"] or 0 for d in diags)
    completion_tok = sum(d["completion_tokens"] or 0 for d in diags)

    return {
        "first_pass_lead_coverage": {
            "n": n_sent, "denominator": "leads sent (cached excluded)",
            "recovered": n_first_recovered,
            "pct": round(100 * n_first_recovered / n_sent, 1) if n_sent else None,
        },
        "per_variant_first_pass_coverage": per_variant,
        "first_pass_batch_clean_rate": {
            "n": n_batches, "denominator": "batches", "clean": n_clean,
            "pct": round(100 * n_clean / n_batches, 1) if n_batches else None,
        },
        "post_retry_lead_coverage": {
            "n": len(leads), "denominator": "qualified leads",
            "generated": report["n_generated"],
            "pct": round(100 * report["n_generated"] / len(leads), 1) if leads else None,
        },
        "truncation_rate": {
            "n": n_batches, "denominator": "batches", "truncated": n_trunc,
            "pct": round(100 * n_trunc / n_batches, 1) if n_batches else None,
        },
        "word_count_compliance": {
            "n": len(words), "denominator": "messages generated",
            "target": [lo, hi], "in_target": len(in_target),
            "pct": round(100 * len(in_target) / len(words), 1) if words else None,
            "min": min(words) if words else None,
            "median": sorted(words)[len(words) // 2] if words else None,
            "max": max(words) if words else None,
        },
        "tokens": {
            "prompt_tokens": prompt_tok,
            "completion_tokens": completion_tok,
            "total_tokens": prompt_tok + completion_tok,
            "denominator": f"message stage only, {n_sent} leads sent",
        },
    }

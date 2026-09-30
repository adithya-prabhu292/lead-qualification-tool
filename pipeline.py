"""
pipeline.py
===========
Orchestration for the Lead Qualification Tool.

Composes the three modules into a run: validate -> tier -> score -> messages
-> report. It owns the two things the modules deliberately do not: the token
pacer that meters both LLM boundaries against one per-minute window, and the
industry-tier normalisation call that scoring needs pre-computed.

Contains no business numbers and no business text beyond the tiering prompt,
which is carried verbatim from the training run so the tiers stay comparable
across files.

Design rules this module holds to
---------------------------------
  * No module-level run state. Config, API key, pacer and output paths are
    passed in. The free-tier ceilings below are defaults, not state.
  * Importing does nothing. No file is read, no option is set, no network
    call is made at import time.
  * All output goes through `log`. Nothing here calls `print` directly.
  * Both LLM clients are injectable, so the pipeline can be driven from tests
    and from a server without reaching the network.

Stage functions can be called one at a time with settings passed in, which is
how the hosted demo drives a run across several short requests.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests

import message_generator as MG
import report_builder as RB
import rubric_scorer as R

# ---------------------------------------------------------------------------
# Token pacer - one ledger, both LLM boundaries
#
# Groq's on_demand TPM ceiling for this model is 8,000. Two earlier runs
# failed against it, and neither failure was a pipeline defect: a call that
# returns finish_reason=length with an empty body still burns the full
# completion cap. The pacer meters a 60s sliding window of ACTUAL reported
# usage and refuses to start a call that would not fit.
# ---------------------------------------------------------------------------
TPM_LIMIT = 8000            # provider's stated ceiling for this model + tier
TPM_HEADROOM = 0.85         # room for estimate error between report and reality
WINDOW_S = 60.0


class TokenPacer:
    """Sliding-window token budget. Shared by tiering and messaging because
    the provider charges both against the same per-minute window."""

    def __init__(self, limit=TPM_LIMIT, headroom=TPM_HEADROOM, window=WINDOW_S):
        self.budget = limit * headroom
        self.window = window
        self._spend = []        # (finished_at, total_tokens)

    def gate(self, ceiling: int, log=print) -> None:
        """Block until `ceiling` more tokens fit in the window."""
        while True:
            now = time.time()
            self._spend[:] = [(t, n) for t, n in self._spend if now - t < self.window]
            used = sum(n for _, n in self._spend)
            # An empty window always proceeds: a single call cannot be paced
            # below its own cost. This is also the loop's exit condition.
            if not self._spend or used + ceiling <= self.budget:
                return
            wait = self.window - (now - self._spend[0][0]) + 1
            log(f"[rate] window {used:.0f} + next call ~{ceiling} exceeds "
                f"{self.budget:.0f} - sleeping {wait:.0f}s")
            time.sleep(wait)

    def charge(self, usage: dict | None, ceiling: int) -> int:
        """Record actual usage; fall back to the ceiling when none is reported."""
        usage = usage or {}
        total = (usage.get("total_tokens")
                 or usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
                 or ceiling)
        self._spend.append((time.time(), total))
        return total

    def used(self) -> float:
        now = time.time()
        return sum(n for t, n in self._spend if now - t < self.window)


# ---------------------------------------------------------------------------
# setup - config, key, input
# ---------------------------------------------------------------------------
def load_config(path: str | Path = "config.yaml") -> dict:
    """The frozen rubric and every run setting. One reader for the whole run."""
    return R.load_config(path)


def resolve_api_key(cfg: dict) -> str | None:
    """The environment variable named in config, or None. The key is named in
    config and never hardcoded, and this is the only place it is read."""
    return os.environ.get(cfg["llm"]["api_key_env"]) or None


def log_settings(cfg: dict, api_key: str | None, log: Callable[[str], Any] = print) -> None:
    """The run's settings, as the run log's opening block. Every number here
    is read from config, so the log doubles as a record of what was frozen."""
    llm, msg = cfg["llm"], cfg["llm_messages"]
    log(f"rubric        {cfg['meta']['version']}   "
        f"(frozen: {cfg['meta']['calibration_basis']})")
    log(f"build         {RB.BUILD}")
    log(f"model         {llm['model']}")
    log("")
    log(f"cutoff        {cfg['decision']['qualify_cutoff']}  "
        f"review margin +/-{cfg['decision']['review_margin']}")
    log("weights       " + "  ".join(
        f"{k}={v['score_weight']}" for k, v in cfg["factors"].items()))
    log(f"blend         fit {cfg['priority']['blend']['fit']} / "
        f"urgency {cfg['priority']['blend']['urgency']}")
    log("")
    # Two batch_size keys exist and they count different things. Printing both,
    # labelled, is the only way a rate in the report is readable later.
    log(f"llm.batch_size           {llm['batch_size']:>3}   "
        f"unique INDUSTRY STRINGS per tiering call  (temp {llm['temperature']})")
    log(f"llm_messages.batch_size  {msg['batch_size']:>3}   "
        f"LEADS per message call                    "
        f"(temp {msg['temperature']}, max_tokens {msg['max_tokens']})")
    log("")
    log(f"API key ({llm['api_key_env']}) present: {bool(api_key)}")


def read_leads(path: str | Path) -> pd.DataFrame:
    """Read the input CSV. A missing file names the path it looked for."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"input CSV not found: {path}")
    return pd.read_csv(path)


def validate_leads(df: pd.DataFrame, cfg: dict, name: str = "input") -> pd.DataFrame:
    """Hard stop naming the problem, not a KeyError three frames deep."""
    required = list(cfg["missing_data"]["completeness_fields"])
    absent = [c for c in required if c not in df.columns]
    if absent:
        raise ValueError(
            f"{name} is missing required column(s): {absent}. "
            f"Required: {required}. Found: {list(df.columns)}"
        )
    if len(df) == 0:
        raise ValueError(f"{name} has headers but no rows.")
    return df


# ---------------------------------------------------------------------------
# Scoring-stage LLM boundary - industry tier normalisation
#
# Runs once over the set of UNIQUE industry strings, never once per lead, and
# caches to disk. The same string must receive the same tier in every file, so
# the cache is shared across files by design - unlike the message cache, which
# is deliberately per-file.
#
# Temperature 0 and fail-closed on truncation: a partially-classified batch is
# discarded rather than half-trusted. A string the model declines to tier is
# treated as missing, never defaulted to a middle tier.
#
# The prompt itself is business text, and since build 1.2.0-dev it lives
# in config as llm.tier_prompt with the rest of the business text.
# ---------------------------------------------------------------------------
class TierLLMError(Exception):
    """Carries whether the failure is worth retrying. Auth, permission and
    bad-request errors are not - retrying them just burns the budget.

    `truncated` separates the one non-retryable failure that a smaller batch
    can fix from the ones it cannot. Re-sending the same batch after a
    truncation would truncate again; sending half of it may not."""

    def __init__(self, message, retryable, truncated=False):
        self.retryable = retryable
        self.truncated = truncated
        super().__init__(message)


def call_tiering_api(prompt: str, cfg: dict, api_key: str | None,
                     pacer: TokenPacer, log: Callable[[str], Any] = print) -> dict:
    """-> {content, usage}. Paced, and the provider's own error body is
    surfaced rather than swallowed into a bare status code."""
    llm = cfg["llm"]
    ceiling = llm["max_tokens"] + 1000
    pacer.gate(ceiling, log)
    try:
        resp = requests.post(
            llm["base_url"],
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                # Default library user agents get fingerprinted and blocked by
                # Cloudflare at the provider edge (error 1010) before the
                # request ever reaches the API.
                "User-Agent": "lead-intelligence/1.1.2",
            },
            json={
                "model": llm["model"],
                "temperature": llm["temperature"],
                "max_tokens": llm["max_tokens"],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=llm["request_timeout_seconds"],
        )
    except requests.RequestException as e:
        raise TierLLMError(f"transport: {e}", retryable=True) from None

    if resp.status_code != 200:
        if resp.status_code == 429:
            pacer.charge(None, ceiling)
        # The server's own explanation lives in the body. Surfacing it is the
        # difference between "HTTP 403" and "model_not_found".
        raise TierLLMError(f"HTTP {resp.status_code}: {resp.text[:400]}",
                           retryable=resp.status_code == 429 or resp.status_code >= 500)

    body = resp.json()
    pacer.charge(body.get("usage"), ceiling)
    choice = body["choices"][0]
    if choice.get("finish_reason") == "length":
        # Fail closed: a truncated classification array cannot be trusted, so
        # the whole reply is discarded. The caller may retry a smaller batch.
        raise TierLLMError("response truncated: finish_reason=length",
                           retryable=False, truncated=True)
    return {"content": choice["message"]["content"], "usage": body.get("usage") or {}}


def parse_tier_reply(text: str):
    """Returns a list, or a parse_error dict. Never raises."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        return {"parse_error": "no JSON array found", "raw": text[:300]}
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        return {"parse_error": str(e), "raw": cleaned[start:end + 1][:300]}


def _classify_labels_once(labels, cfg: dict, api_key: str | None,
                          pacer: TokenPacer, log: Callable[[str], Any],
                          call: Callable, diag: dict) -> dict:
    """One label list through the retry-bounded path. -> the labels recovered.

    Bounded by llm.max_retries, which covers transport failures, 429, 5xx,
    parse failures and replies where zero labels aligned. Auth, permission
    and 400 errors are not retried. Accumulates into the caller's `diag`, so
    a batch's totals span every pass made for it.
    """
    llm = cfg["llm"]
    valid_tiers = set(cfg["factors"]["industry"]["tier_scores"])
    prompt = llm["tier_prompt"].format(labels="\n".join(f"- {l}" for l in labels))

    for attempt in range(1, llm["max_retries"] + 1):
        diag["attempts"] += 1
        try:
            result = call(prompt, cfg, api_key, pacer)
        except TierLLMError as e:
            diag["errors"].append(str(e))
            if e.truncated:
                diag["truncated"] = True
            if not e.retryable:
                break
            time.sleep(llm["retry_backoff_seconds"] * attempt)
            continue

        u = result["usage"]
        diag["prompt_tokens"] += int(u.get("prompt_tokens", 0))
        diag["completion_tokens"] += int(u.get("completion_tokens", 0))

        parsed = parse_tier_reply(result["content"])
        if isinstance(parsed, dict):
            diag["errors"].append(f"parse: {parsed['parse_error']}")
            time.sleep(llm["retry_backoff_seconds"] * attempt)
            continue

        diag["n_out"] += len(parsed)
        mapping = {
            str(e.get("industry", "")).strip(): str(e.get("tier", "")).strip()
            for e in parsed
            if isinstance(e, dict)
            and str(e.get("tier", "")).strip() in valid_tiers
        }
        recovered = {l: mapping[l] for l in labels if l in mapping}
        if recovered:
            return recovered
        diag["errors"].append("zero labels aligned")
        time.sleep(llm["retry_backoff_seconds"] * attempt)

    return {}


def classify_industry_batch(labels, cfg: dict, api_key: str | None,
                            pacer: TokenPacer, log: Callable[[str], Any] = print,
                            client: Callable | None = None,
                            allow_split: bool = True):
    """-> (mapping, diagnostics). Alignment is by label, never by position.

    Two passes. The first sends every label. The second re-sends, once, only
    the labels that did not come back aligned: absent from the reply, spelt
    differently, or carrying a tier outside tier_scores. Without it a label
    the model simply skipped is never asked about again, and the lead behind
    it is scored with its industry silently dropped.

    Labels still missing after the re-send are unclassified, and their leads
    go to REVIEW through the completeness rule in rubric_scorer.

    Truncation. A reply with finish_reason=length is discarded whole - tiering
    fails closed and never keeps part of a truncated array. The batch is then
    split once into two halves, and each half goes through the same two passes.
    A half that truncates again is NOT split further: its labels are
    unclassified. A single label that truncates is unclassified, because there
    is nothing left to halve.

    Bounds. Each pass is bounded by llm.max_retries, and the re-send pass runs
    only when the first recovered something - a first pass that recovers
    nothing has already spent its retries on the same labels. So one attempt
    at a label list costs at most 2 * llm.max_retries HTTP calls, and a batch
    that truncates and is split costs at most 3 * that, once for the truncated
    attempt and once for each half. The split happens at most once.
    """
    call = client or call_tiering_api
    diag = {"n_in": len(labels), "attempts": 0, "errors": [], "n_out": 0,
            "n_aligned": 0, "misaligned": list(labels),
            "resends": 0, "recovered_by_resend": [],
            "truncated": False, "splits": 0,
            "prompt_tokens": 0, "completion_tokens": 0}

    recovered = _classify_with_resend(labels, cfg, api_key, pacer, log, call, diag)

    if diag["truncated"] and not recovered and len(labels) > 1 and allow_split:
        half = len(labels) // 2
        diag["splits"] = 1
        log(f"  tiering split: batch of {len(labels)} truncated, "
            f"retrying as {half} + {len(labels) - half}")
        for part in (labels[:half], labels[half:]):
            # allow_split=False: a half that truncates again is not split
            # further. Halving forever turns one bad reply into a call storm.
            part_map, part_diag = classify_industry_batch(
                part, cfg, api_key, pacer, log, client, allow_split=False)
            recovered.update(part_map)
            diag["attempts"] += part_diag["attempts"]
            diag["errors"] += part_diag["errors"]
            diag["n_out"] += part_diag["n_out"]
            diag["resends"] += part_diag["resends"]
            diag["recovered_by_resend"] += part_diag["recovered_by_resend"]
            diag["prompt_tokens"] += part_diag["prompt_tokens"]
            diag["completion_tokens"] += part_diag["completion_tokens"]

    diag["n_aligned"] = len(recovered)
    diag["misaligned"] = sorted(set(labels) - set(recovered))
    return recovered, diag


def _classify_with_resend(labels, cfg: dict, api_key: str | None,
                          pacer: TokenPacer, log: Callable[[str], Any],
                          call: Callable, diag: dict) -> dict:
    """The two passes over one label list: send all, then re-send the ones
    that did not align. Truncation is left for the caller to act on."""
    recovered = _classify_labels_once(labels, cfg, api_key, pacer, log, call, diag)

    missing = [l for l in labels if l not in recovered]
    if recovered and missing:
        diag["resends"] += 1
        log(f"  tiering re-send: {len(missing)} of {len(labels)} label(s) "
            f"missing from the reply")
        again = _classify_labels_once(missing, cfg, api_key, pacer, log, call, diag)
        diag["recovered_by_resend"] += [l for l in missing if l in again]
        recovered.update(again)

    return recovered


def tier_industries(df: pd.DataFrame, cfg: dict, api_key: str | None,
                    cache_path: str | Path, pacer: TokenPacer,
                    log: Callable[[str], Any] = print,
                    client: Callable | None = None) -> tuple[dict, dict]:
    """Cache-first. A live call is made only for strings never seen before."""
    llm = cfg["llm"]
    cache_path = Path(cache_path)
    tier_map = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    unique = sorted({s for s in df["industry"].dropna().astype(str).str.strip() if s})
    todo = [s for s in unique if s not in tier_map]
    log(f"  industries: n={len(unique)} unique | cached {len(unique) - len(todo)} | "
        f"to classify {len(todo)}")

    meta = {"n_strings_total": len(unique), "n_from_cache": len(unique) - len(todo),
            "n_classified_live": 0, "batches": 0, "parse_failures": 0,
            "missing_label_resends": 0, "truncation_splits": 0,
            "unclassified": [], "batch_size_key": "llm.batch_size",
            "batch_size": llm["batch_size"],
            "tokens": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    if todo:
        if not api_key:
            raise RuntimeError(
                f"{len(todo)} industry strings need classifying and no API key is set. "
                f"Set {llm['api_key_env']} in the environment and run again."
            )
        bs = int(llm["batch_size"])
        for i in range(0, len(todo), bs):
            mapping, diag = classify_industry_batch(
                todo[i:i + bs], cfg, api_key, pacer, log, client)
            meta["batches"] += 1
            meta["parse_failures"] += sum(1 for e in diag["errors"] if e.startswith("parse"))
            meta["missing_label_resends"] += diag["resends"]
            meta["truncation_splits"] += diag["splits"]
            meta["tokens"]["prompt_tokens"] += diag["prompt_tokens"]
            meta["tokens"]["completion_tokens"] += diag["completion_tokens"]
            tier_map.update(mapping)
            log(f"  tiering batch {meta['batches']}: sent={diag['n_in']} "
                f"aligned={diag['n_aligned']} attempts={diag['attempts']}"
                + (f" resent={diag['resends']}" if diag["resends"] else "")
                + (f" split={diag['splits']}" if diag["splits"] else "")
                + (f" | {diag['errors']}" if diag["errors"] else ""))
        meta["n_classified_live"] = len([s for s in todo if s in tier_map])
        meta["unclassified"] = [s for s in todo if s not in tier_map]
        cache_path.write_text(json.dumps(tier_map, indent=2, sort_keys=True),
                              encoding="utf-8")
        log(f"  tier cache written: {cache_path} (n={len(tier_map)})")

    meta["tokens"]["total_tokens"] = (meta["tokens"]["prompt_tokens"]
                                      + meta["tokens"]["completion_tokens"])
    # A string the model would not tier scores as missing, never as a middle
    # tier - that would assign a real score to something never classified.
    if meta["unclassified"]:
        log(f"  [warn] unclassified, will score as missing: {meta['unclassified']}")
    return tier_map, meta


# ---------------------------------------------------------------------------
# Messaging-stage LLM boundary - the paced message client
#
# Wraps MG.call_message_api without editing the module. On a 429 it charges
# the ceiling, waits out the window, then re-raises so the module's own
# transport counter still bounds the retry loop at 3 attempts.
# ---------------------------------------------------------------------------
def make_paced_message_client(pacer: TokenPacer, log: Callable[[str], Any] = print):
    """-> the client passed to generate_messages(client=...)."""

    def paced_message_client(prompt, cfg, api_key):
        ceiling = int(cfg["llm_messages"]["max_tokens"]) + 1000
        pacer.gate(ceiling, log)
        try:
            result = MG.call_message_api(prompt, cfg, api_key)
        except MG.MessageLLMError as e:
            if "429" in str(e):
                pacer.charge(None, ceiling)
                log(f"[rate] 429 despite pacing - sleeping {WINDOW_S:.0f}s before the "
                    f"transport retry")
                time.sleep(WINDOW_S)
            raise
        total = pacer.charge(result.get("usage"), ceiling)
        log(f"[rate] call used {total} tokens | window {pacer.used():.0f}/{pacer.budget:.0f}")
        return result

    return paced_message_client


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------
def score_leads(df: pd.DataFrame, cfg: dict, tier_map: dict):
    """-> (leads, processing_date). Deterministic; no API call."""
    return R.score_all_leads(df, cfg, tier_map)


def generate_messages_for(qualified: list, cfg: dict, api_key: str | None,
                          cache_path: str | Path, pacer: TokenPacer,
                          log: Callable[[str], Any] = print,
                          source_file: str = "",
                          client: Callable | None = None) -> dict | None:
    """-> the messaging module's report, or None when there is nobody to write
    to. Zero-qualified is a valid outcome, not an error."""
    if not qualified:
        return None
    return MG.generate_messages(
        qualified, cfg, api_key=api_key, source_file=source_file,
        cache_path=Path(cache_path),
        client=client or make_paced_message_client(pacer, log), log=log,
    )


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------
def run_pipeline(input_csv: str | Path, cfg: dict, api_key: str | None = None,
                 out_dir: str | Path = "output", pacer: TokenPacer | None = None,
                 log: Callable[[str], Any] = print,
                 tier_cache_path: str | Path | None = None,
                 tier_client: Callable | None = None,
                 message_client: Callable | None = None):
    """CSV in, report out. Every step has an exit condition.

    validate -> tier -> score/guardrails/rank -> filter QUALIFIED
             -> generate messages -> build/write report -> list files

    Every file the run writes lands in `out_dir`. The tier cache is the one
    exception: it is shared across files by design, so it stays where config
    points unless `tier_cache_path` overrides it.
    """
    pacer = pacer if pacer is not None else TokenPacer()
    path = Path(input_csv)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if tier_cache_path is None:
        tier_cache_path = cfg["factors"]["industry"]["normalisation"]["cache_path"]

    stem = RB.derive_output_stem(path, cfg)
    paths = RB.resolve_output_paths(cfg, stem, out_dir)
    t0 = time.time()

    log(f"── {path.name} ─────────────────────────────────────────")
    df = validate_leads(read_leads(path), cfg, path.name)
    log(f"  validated: n={len(df)} rows, {len(df.columns)} columns")

    # --- scoring -----------------------------------------------------------
    tier_map, s1 = tier_industries(df, cfg, api_key, tier_cache_path, pacer, log,
                                   tier_client)
    leads, processing_date = score_leads(df, cfg, tier_map)
    s1["processing_date"] = processing_date
    counts = {}
    for l in leads:
        counts[l.decision] = counts.get(l.decision, 0) + 1
    log(f"  scored: n={len(leads)} | processing_date {processing_date} | {counts}")

    R.leads_to_dataframe(leads).to_csv(paths["scored_table"], index=False)
    paths["stage1_report"].write_text(json.dumps(
        {"stage": "scoring", "source_file": path.name,
         "rubric_version": cfg["meta"]["version"], "build": RB.BUILD,
         "processing_date": str(processing_date), "n_leads": len(leads),
         "decisions": counts, "tiering": s1}, indent=2, default=str),
        encoding="utf-8")

    # --- messaging ---------------------------------------------------------
    qualified = [l for l in leads if l.decision == "QUALIFIED"]
    msg_report = None
    if not qualified:
        log("  no qualified leads - message generation skipped")
    else:
        if not api_key:
            # Warn, do not stop. Everything already in the cache is served, and
            # a lead with no cache entry fails through the messaging module's
            # own no-key error: non-retryable, so it costs no sleeps and no
            # calls. A run that can replay a whole file without a key is worth
            # more than a run that refuses to start.
            #
            # Tiering is not treated this way. It stops above, because an
            # unclassified industry silently changes a lead's decision, and
            # that is not a failure the report can show honestly.
            log(f"  [warn] no API key ({cfg['llm']['api_key_env']}); serving "
                f"messages from cache only. Any of the {len(qualified)} "
                f"qualified leads without a cache entry will be recorded as "
                f"failed.")
        log(f"  generating messages for n={len(qualified)} qualified leads "
            f"(batch_size {cfg['llm_messages']['batch_size']}, "
            f"temp {cfg['llm_messages']['temperature']})")
        msg_report = generate_messages_for(
            qualified, cfg, api_key, paths["message_cache"], pacer, log,
            source_file=path.name, client=message_client,
        )
        paths["message_report"].write_text(
            json.dumps(msg_report, indent=2, default=str), encoding="utf-8")
        log(f"  messages: generated {msg_report['n_generated']} | "
            f"cached {msg_report['n_cached']} | failed {msg_report['n_failed']} "
            f"of n={msg_report['n_leads']}")

    # --- report ------------------------------------------------------------
    report = RB.build_report(leads, cfg, source_file=path.name,
                             stage1_meta=s1, stage2_report=msg_report, log=log)
    written = RB.write_report(report, cfg, stem, out_dir)

    all_written = [str(paths["scored_table"]), str(paths["stage1_report"])]
    if msg_report is not None:
        all_written += [str(paths["message_report"]), str(paths["message_cache"])]
    all_written += written
    report["files_written"] = all_written

    # The caller lists the files. The run log's job ends with the run: the CLI
    # shows the report summary first and the paths last, and a server caller
    # wants the list as data, not as log lines.
    log("")
    log(f"  done in {time.time() - t0:.1f}s, {len(all_written)} files written")
    return report, leads

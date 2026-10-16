"""Run the existing CLI privately and publish only its validated aggregate report."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import html
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable
from urllib.parse import quote, unquote, urlsplit

import httpx

from aq_sdk import Client, publish_pr_comment, report
from aq_sdk.client import UUID
from aq_sdk.config import Config


class Refused(ValueError):
    """A fixed, credential-free integration error."""


class MissingReport(Refused):
    def __init__(self, code):
        super().__init__("No valid report was retained. Inspect the private run before any further submission.")
        self.code = code if type(code) is int and code in (0, 1, 2, 3) else None


SCORER_CONTROLS = ("id", "kind", "config", "threshold", "judge_provider", "judge_connection_id",
                   "uses_model", "needs_trace")


@dataclass
class Settings:
    repository: str
    pull_request: int
    commit: str
    agent_endpoint: str
    target: str
    dataset: str
    budget: str
    endpoint: str
    ui_url: str
    api_key: str = field(repr=False)
    github_token: str = field(repr=False)
    publisher_id: int
    check_key: str
    timeout: int
    reconcile_report: str
    base_commit: str
    base_endpoint: str
    baseline_experiment: str
    check_app_id: int
    head_repository: str
    head_repository_is_fork: bool
    base_repository_is_fork: bool
    publish_report_links: bool
    task: str = ""


def settings(env: dict[str, str]) -> Settings:
    event_name = env.get("GITHUB_EVENT_NAME")
    allow_forks = env.get("AQ_CI_ALLOW_FORKS", "false")
    publish_report_links = env.get("AQ_CI_PUBLISH_REPORT_LINKS", "true")
    if publish_report_links not in ("false", "true"):
        raise Refused("The publish-report-links input must be exactly true or false.")
    if event_name not in ("pull_request", "pull_request_target") or allow_forks not in ("false", "true"):
        raise Refused("Use same-repository pull_request by default, or explicitly opt in to cross-repository pull_request_target.")
    try:
        event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
        pr = event["pull_request"]
        repository = env["GITHUB_REPOSITORY"]
        number = pr["number"]
        sha = pr["head"]["sha"]
        base_sha = pr["base"]["sha"]
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}", repository) or repository.split('/')[1] in ('.', '..'):
            raise ValueError
        head_repository = pr["head"]["repo"]["full_name"]
        head_repository_is_fork = pr["head"]["repo"].get("fork")
        base_repository_is_fork = pr["base"]["repo"].get("fork")
        if (pr["base"]["repo"]["full_name"] != repository
                or not isinstance(head_repository, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}", head_repository)
                or head_repository.split("/")[1] in (".", "..")
                or type(head_repository_is_fork) is not bool or type(base_repository_is_fork) is not bool):
            raise Refused("The pull-request source repository or fork metadata is unknown or invalid.")
        same_repo = head_repository == repository
        if same_repo and head_repository_is_fork is not base_repository_is_fork:
            raise Refused("The same repository has contradictory fork metadata.")
        if not ((event_name == "pull_request" and same_repo)
                or (event_name == "pull_request_target" and allow_forks == "true" and not same_repo)):
            raise Refused("Same-repository PRs require pull_request; cross-repository pull_request_target requires explicit opt-in.")
        if type(number) is not int or number <= 0 or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha):
            raise ValueError
        commit = env["AQ_CI_COMMIT"].strip()
        if commit.lower() != sha.lower():
            raise Refused("The tested commit must match the trusted pull-request head.")
        endpoint_name = env["AQ_CI_AGENT_ENDPOINT"].strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", endpoint_name):
            raise Refused("An AgentCore endpoint name on the connected runtime is required.")
        if (not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", base_sha)
                or env["AQ_CI_BASE_COMMIT"].strip().lower() != base_sha.lower()):
            raise Refused("The protected base-build commit must match the trusted PR base.")
        base_endpoint = env["AQ_CI_BASE_ENDPOINT"].strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", base_endpoint):
            raise Refused("A protected base-build AgentCore endpoint is required.")
        baseline = env["AQ_CI_BASELINE_EXPERIMENT"].strip()
        if not UUID.fullmatch(baseline):
            raise Refused("An explicit reviewed baseline experiment UUID is required; current and names are not base-build evidence.")
        check_app_id = int(env["AQ_CI_CHECK_APP_ID"])
        if not 0 < check_app_id <= 2**63 - 1:
            raise ValueError
        api = env["AQ_ENDPOINT"].strip().rstrip("/")
        parts = urlsplit(api)
        parts.port
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or any(c.isspace() for c in api)):
            raise Refused("The evaluation API must be a credential-free HTTPS address.")
        budget = Decimal(env["AQ_CI_BUDGET"])
        if not budget.is_finite() or budget <= 0:
            raise Refused("A positive metered evaluation budget is required.")
        publisher_id = int(env["AQ_CI_PUBLISHER_ID"])
        if publisher_id <= 0:
            raise ValueError
        key = env["AQ_CI_CHECK_KEY"]
        report.comment_marker(key)
        timeout = int(env.get("AQ_CI_TIMEOUT", "1800"))
        if not 1 <= timeout <= 3600:
            raise Refused("The evaluation timeout must be from 1 to 3600 seconds.")
        if not env.get("AQ_API_KEY") or not env.get("AQ_CI_GITHUB_TOKEN"):
            raise Refused("Separate evaluation and repository credentials are required.")
        if any(secret in key for secret in (env["AQ_API_KEY"], env["AQ_CI_GITHUB_TOKEN"])):
            raise Refused("A credential cannot be a public check or comment key.")
        if not env.get("AQ_CI_TARGET", "").strip() or not env.get("AQ_CI_DATASET", "").strip():
            raise Refused("A connected agent and dataset are required.")
        raw_task = env.get("AQ_CI_TASK", "")
        task = raw_task.strip()
        if raw_task and not UUID.fullmatch(task):
            raise Refused("The optional task gate needs an explicit task UUID from trusted configuration.")
        task = task.lower()
        return Settings(repository, number, commit, endpoint_name, env["AQ_CI_TARGET"], env["AQ_CI_DATASET"],
                        str(budget), api, env.get("AQ_UI_URL", "").strip() or api, env["AQ_API_KEY"],
                        env["AQ_CI_GITHUB_TOKEN"], publisher_id, key, timeout,
                        env.get("AQ_CI_RECONCILE_REPORT", "").strip(), base_sha.lower(), base_endpoint,
                        baseline.lower(), check_app_id, head_repository, head_repository_is_fork,
                        base_repository_is_fork, publish_report_links == "true", task)
    except Refused:
        raise
    except (KeyError, TypeError, ValueError, OSError, InvalidOperation):
        raise Refused("Trusted pull-request context or required configuration is missing or invalid.") from None


def selected(value: dict, keys: tuple[str, ...]) -> dict:
    return {key: value[key] for key in keys if key in value}


def scorer_controls(specs: list[dict], *, resolved=False) -> str:
    """Compare configured controls using the service's existing provider semantics."""
    if not isinstance(specs, list) or not specs:
        raise Refused("The resolved scoring controls are missing or invalid.")
    controls, seen = [], set()
    for spec in specs:
        if (not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not UUID.fullmatch(spec["id"])
                or spec["id"] in seen or not isinstance(spec.get("kind"), str)
                or not isinstance(spec.get("config"), dict) or "threshold" not in spec
                or type(spec.get("uses_model")) is not bool or type(spec.get("needs_trace")) is not bool):
            raise Refused("The resolved scoring controls are missing or invalid.")
        seen.add(spec["id"])
        control = selected(spec, SCORER_CONTROLS)
        if spec["uses_model"]:
            provider = spec.get("judge_provider")
            if resolved and (not isinstance(provider, dict) or not isinstance(provider.get("kind"), str)
                             or not provider["kind"] or "region" not in provider):
                raise Refused("The current resolved judge provider is missing or invalid.")
            # compare._spec_key and LLMProvider.from_dict define historical
            # missing providers as Bedrock. This is configuration, not serving attestation.
            if provider is None:
                provider = {"kind": "bedrock"}
            if not isinstance(provider, dict):
                raise Refused("The resolved judge provider is missing or invalid.")
            kind = provider.get("kind")
            if kind is None:
                kind = "bedrock"
            if kind == "bedrock":
                region = provider.get("region") if "region" in provider else spec["config"].get("region") or "eu-central-1"
                connection = spec.get("judge_connection_id")
                if not isinstance(region, str) or not region or not isinstance(connection, str) or not UUID.fullmatch(connection):
                    raise Refused("The resolved judge region or connection is missing or invalid.")
                control["judge_provider"] = {"kind": kind, "region": region}
            elif kind in ("azure_openai", "eu_gateway", "openai_compatible"):
                if (not isinstance(provider.get("base_url"), str) or not provider["base_url"]
                        or type(provider.get("zero_data_retention")) is not bool
                        or (resolved and "api_version" not in provider)):
                    raise Refused("The resolved judge provider is missing or invalid.")
                control["judge_provider"] = {key: provider.get(key) for key in
                    ("kind", "region", "base_url", "api_version", "zero_data_retention")}
            else:
                raise Refused("The resolved judge provider is unsupported.")
        controls.append(control)
    try:
        # JSON preserves type differences that Python equality erases (true == 1).
        return json.dumps(sorted(controls, key=lambda sc: sc["id"]), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise Refused("The resolved scoring controls are missing or invalid.") from None


def trusted_pr(s: Settings, client: httpx.Client) -> None:
    """Confirm trusted event identities against GitHub, never a PR artifact or redirect."""
    response = client.get(f"/repos/{s.repository}/pulls/{s.pull_request}")
    if response.status_code != 200:
        raise Refused("GitHub did not confirm the intended pull request.")
    pr = response.json()
    try:
        valid = (pr["number"] == s.pull_request and type(pr["number"]) is int
                 and pr["base"]["repo"]["full_name"] == s.repository
                 and pr["head"]["repo"]["full_name"] == s.head_repository
                 and pr["head"]["repo"]["fork"] is s.head_repository_is_fork
                 and pr["base"]["repo"]["fork"] is s.base_repository_is_fork
                 and pr["head"]["sha"].lower() == s.commit.lower()
                 and pr["base"]["sha"].lower() == s.base_commit)
    except (KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise Refused("PR head, base or ownership changed. No evaluation or publication is authorized by this context.")


def github_client(s: Settings, transport=None):
    return httpx.Client(base_url="https://api.github.com", transport=transport, timeout=15,
                        follow_redirects=False, trust_env=False,
                        headers={"Authorization": "Bearer " + s.github_token,
                                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"})


def confirm_context(s: Settings, *, transport=None) -> None:
    with github_client(s, transport) as github:
        trusted_pr(s, github)


def complete_baseline(base: dict) -> None:
    summary = base.get("summary") or {}
    specs = base.get("scorers")
    def positive(x):
        return type(x) is int and x > 0
    if (base.get("status") != "completed" or not positive(base.get("repeats"))
            or not positive(base.get("dataset_version")) or not positive(summary.get("rows"))
            or not positive(summary.get("attempts")) or summary.get("target_errors") != 0
            or type(summary.get("target_errors")) is not int or summary.get("timeouts") != 0
            or type(summary.get("timeouts")) is not int or not isinstance(specs, list) or not specs):
        raise Refused("The reviewed baseline has missing, unfinished or failed measurement evidence.")
    expected = summary["rows"] * base["repeats"]
    if (summary["attempts"] != expected or type(summary.get("ok")) is not int or summary["ok"] != expected
            or any(key in summary and (type(summary[key]) is not int or summary[key] != 0)
                   for key in ("not_run", "rows_not_run", "rows_with_errors"))):
        raise Refused("Not every requested baseline row and repetition completed successfully.")
    progress = base.get("progress") or {}
    if any(key in progress and (type(progress[key]) is not int or progress[key] != expected)
           for key in ("total", "finished")):
        raise Refused("The retained plan and completed attempt counts do not agree.")
    scores = summary.get("scorers")
    ids = [spec.get("id") for spec in specs if isinstance(spec, dict)]
    if (len(ids) != len(specs) or any(not isinstance(sid, str) or not UUID.fullmatch(sid) for sid in ids)
            or len(set(ids)) != len(ids) or not isinstance(scores, list)
            or {sc.get("scorer_id") for sc in scores if isinstance(sc, dict)} != set(ids)
            or len(scores) != len(ids)
            or any(type(sc.get("pending")) is not int or sc["pending"] != 0
                   or type(sc.get("errors")) is not int or sc["errors"] != 0
                   or type(sc.get("scored")) is not int or sc["scored"] != expected
                   or ("rows_scored" in sc and (type(sc["rows_scored"]) is not int
                                               or sc["rows_scored"] != summary["rows"])) for sc in scores)):
        raise Refused("The baseline's required scoring evidence is missing or incomplete.")


def preflight(s: Settings, *, client=None, transport=None) -> dict:
    """Validate a protected pipeline's declared binding, not deployed-source attestation."""
    try:
        confirm_context(s, transport=transport)
        own = client is None
        if own:
            client = Client(Config(None, s.api_key, s.endpoint, s.ui_url),
                            transport=httpx.HTTPTransport(trust_env=False))
        try:
            agent, dataset = client.agent(s.target), client.dataset(s.dataset)
            if s.task:
                client.task(s.task, agent_id=agent["id"], dataset_id=dataset["id"])
            base = client.experiment(s.baseline_experiment)
            target = base.get("target") or {}
            if (agent.get("kind") != "agentcore_runtime" or agent.get("non_production") is not True
                    or base.get("id") != s.baseline_experiment or base.get("agent_id") != agent.get("id")
                    or base.get("dataset_id") != dataset.get("id") or target.get("kind") != "agentcore"
                    or target.get("agent_id") != agent.get("id") or target.get("commit_sha") != s.base_commit
                    or type(target.get("endpoint_overridden")) is not bool
                    or (target["endpoint_overridden"] and target.get("agent_endpoint") != s.base_endpoint)
                    or (not target["endpoint_overridden"] and target.get("agent_endpoint") is not None)
                    or target.get("effective_agent_endpoint") != s.base_endpoint):
                raise Refused("The immutable baseline does not match the protected PR-base agent, dataset and endpoint binding.")
            row_scope = "held_out" if s.task else "development"
            if (base.get("progress") or {}).get("row_scope") != row_scope:
                raise Refused("The reviewed baseline must match the selected task or development row scope.")
            if (base.get("progress") or {}).get("row_ids") is not None:
                raise Refused("The existing CLI cannot inherit a pinned-row baseline; no task was submitted.")
            # Existing store.includes_rejected treats a missing legacy choice as
            # include-all. The CLI submits False and cannot override that policy.
            if (base.get("progress") or {}).get("include_rejected") is not False:
                raise Refused("The existing CLI cannot preserve an include-all or unknown legacy rejected-row policy.")
            complete_baseline(base)
            if not s.reconcile_report:
                frozen = scorer_controls(base["scorers"])
                current = client.estimate({"scorers_only": True, "agent_id": agent["id"],
                    "dataset_id": dataset["id"], "dataset_version": base["dataset_version"],
                    "target": {"kind": "agentcore", "agent_id": agent["id"],
                               "agent_endpoint": s.agent_endpoint, "commit_sha": s.commit},
                    "scorer_ids": [sc["id"] for sc in base["scorers"]], "repeats": base["repeats"],
                    "row_scope": row_scope, "include_rejected": False})
                if not isinstance(current, dict) or scorer_controls(current.get("scorers"), resolved=True) != frozen:
                    raise Refused("Current scoring controls differ from the frozen baseline. No task was submitted.")
            return base
        finally:
            if own:
                client.close()
    except Refused:
        raise
    except Exception:
        raise Refused("The read-only base-build preflight could not be verified. No task was submitted.") from None


def aggregate_result(result: dict, *, publish_report_links: bool = True) -> dict:
    """Project public fields without changing the validated original execution result."""
    if type(publish_report_links) is not bool:
        raise Refused("The report-link projection needs an explicit boolean.")
    expected_body = report.pr_summary(result)
    if not publish_report_links:
        result = {key: value for key, value in result.items() if key != "link"}
        expected_body = report.pr_summary(result)
    def numeric(value):
        if type(value) not in (int, float):
            return None
        try:
            return value if math.isfinite(value) else None
        except OverflowError:
            return None

    def alpha(value):
        value = numeric(value)
        return value if value is not None and 0 < value < 1 else None

    def scorer(sc):
        out = selected(sc, ("scorer_id", "name", "status", "note", "base_mean", "candidate_mean",
                           "difference", "improved", "regressed", "unchanged", "excluded", "verdict"))
        if "alpha_regression" in sc:
            out["alpha_regression"] = alpha(sc["alpha_regression"])
        if "interval" in sc:
            interval = sc["interval"]
            out["interval"] = None
            if isinstance(interval, dict):
                bounds = {key: numeric(interval[key]) for key in ("lower", "upper") if key in interval}
                if bounds.get("lower") is not None and bounds.get("upper") is not None and bounds["lower"] > bounds["upper"]:
                    bounds["lower"] = bounds["upper"] = None
                if "alpha" in interval:
                    bounds["alpha"] = alpha(interval["alpha"])
                if "method" in interval:
                    method = interval["method"]
                    bounds["method"] = method if isinstance(method, str) and method in (
                        "bonferroni-clopper-pearson-discordance", "bootstrap-percentile-paired-difference") else "unknown"
                out["interval"] = bounds
        return out

    def experiment(exp):
        if exp is None:
            return None
        out = selected(exp, ("id", "agent_id", "dataset_id", "dataset_version", "repeats", "name", "status", "stop_reason"))
        out["target"] = selected(exp.get("target") or {}, ("kind", "agent_id", "commit_sha", "agent_endpoint",
                                                            "effective_agent_endpoint", "endpoint_overridden"))
        out["scorers"] = [selected(s, ("id", "name")) for s in exp.get("scorers", [])]
        summary = exp.get("summary") or {}
        out["summary"] = selected(summary, ("rows", "attempts", "ok", "target_errors", "timeouts", "not_run", "rows_not_run", "rows_with_errors"))
        out["summary"]["scorers"] = [selected(s, ("scorer_id", "name", "mean", "pass_rate", "errors", "pending", "scored", "rows_scored"))
                                     for s in summary.get("scorers", [])]
        planning = (exp.get("progress") or {}).get("planning") or {}
        out["progress"] = {**selected(exp.get("progress") or {}, ("total", "finished", "row_scope", "include_rejected")),
                           "planning": selected(planning, ("text",))}
        return out

    out = selected(result, ("exit_code", "reason", "verdict", "link"))
    out["experiment"] = experiment(result.get("experiment"))
    if result.get("task") is not None:
        out["task"] = selected(result["task"], ("id", "name", "agent_id", "dataset_id"))
        v = result.get("task_verdict")
        out["task_verdict"] = None
        if v is not None:
            threshold_keys = ("metric", "value", "scorer_id", "scorer", "category", "status", "observed", "bound", "cases",
                              "failures", "unresolved", "uncategorized_cases", "undeclared_category_cases",
                              "must_pass_status", "paired_quality_status", "reason")
            def measurement(value):
                kept = selected(value, ("status", "acceptance_reason", "requirements"))
                if "thresholds" in value:
                    kept["thresholds"] = None if value["thresholds"] is None else [selected(t, threshold_keys) for t in value["thresholds"]]
                if kept.get("requirements") is not None:
                    kept["requirements"] = [selected(r, ("code", "status")) for r in kept["requirements"]]
                return kept
            verdict = measurement(v)
            verdict.update(selected(v, ("experiment_id", "bar_version", "slice_metadata_status")))
            if "worst_slice" in v:
                verdict["worst_slice"] = None if v["worst_slice"] is None else selected(v["worst_slice"], ("dimension", "value", "status"))
            if "slices" in v:
                verdict["slices"] = None if v["slices"] is None else [
                    {**measurement(item), **selected(item, ("dimension", "value", "cases", "reason"))} for item in v["slices"]]
            out["task_verdict"] = verdict
    cmp = result.get("compare")
    out["compare"] = None
    if cmp is not None:
        out["compare"] = selected(cmp, ("matched_rows", "verdict", "decision", "note", "driven_by"))
        out["compare"]["base"] = experiment(cmp["base"])
        out["compare"]["candidate"] = experiment(cmp.get("candidate"))
        out["compare"]["scorers"] = [scorer(s) for s in cmp["scorers"]]
    if report.pr_summary(out) != expected_body:
        raise Refused("The aggregate report does not preserve the installed SDK's report contract.")
    return out


def guard_public_strings(public_strings: list[str], s: Settings) -> None:
    for secret in (s.api_key, s.github_token):
        variants = (secret, report._markdown(secret), html.escape(secret), quote(secret, safe=""))
        if any(value and value in text for value in variants for text in public_strings):
            raise Refused("The public aggregate contains a known credential. Publication was refused.")
    if s.publish_report_links:
        return
    known = set()
    for address in (s.endpoint, s.ui_url):
        try:
            hostname = urlsplit(address).hostname
        except ValueError:
            raise Refused("The private reporting addresses could not be checked.") from None
        for value in (address, hostname):
            if value:
                known.update(v.casefold() for v in (value, report._markdown(value), html.escape(value), quote(value, safe="")))
    for text in public_strings:
        # Decode only for known-address comparison. Never rewrite the public evidence.
        for _ in range(3):
            if any(value in text.casefold() for value in known):
                raise Refused("The public aggregate contains a private reporting address. Publication was refused.")
            text = html.unescape(unquote(text))
            text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)
            text = re.sub(r"\\([^\w\s])", r"\1", text)


def public_body(result: dict, s: Settings) -> str:
    body = report.pr_summary(result)
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from strings(key)
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    guard_public_strings([body, report.comment_marker(s.check_key), "Evaluation: " + s.check_key,
                          *strings(result)], s)
    return body


def private_text(text: str, s: Settings) -> str:
    """Remove the two known credentials from retained private debug output."""
    for secret in (s.api_key, s.github_token):
        variants = {secret, report._markdown(secret), html.escape(secret), quote(secret, safe="")}
        variants.update(json.dumps(value)[1:-1] for value in tuple(variants))
        for value in sorted(variants, key=len, reverse=True):
            text = text.replace(value, "[credential removed]")
    return text


def validate_result(result: dict, code: int, s: Settings, baseline: dict | None = None) -> None:
    if not isinstance(result, dict) or type(result.get("exit_code")) is not int or result["exit_code"] != code:
        raise Refused("The CLI did not produce a report matching its exit status.")
    if code not in (0, 1, 2, 3):
        raise Refused("The evaluation returned an unsupported exit status.")
    exp = result.get("experiment")
    if not isinstance(exp, dict) or (exp.get("target") or {}).get("commit_sha") != s.commit:
        raise Refused("The retained experiment does not bind the tested commit.")
    task = result.get("task")
    if s.task:
        if (not isinstance(task, dict) or task.get("id") != s.task
                or not task.get("agent_id") or task.get("agent_id") != exp.get("agent_id")
                or not task.get("dataset_id") or task.get("dataset_id") != exp.get("dataset_id")):
            raise Refused("The retained task does not bind the requested task, agent and dataset.")
    elif task is not None or result.get("task_verdict") is not None:
        raise Refused("The retained report contains an unrequested task gate.")
    if not s.reconcile_report:
        target = exp.get("target") or {}
        if (target.get("agent_endpoint") != s.agent_endpoint or target.get("endpoint_overridden") is not True
                or target.get("effective_agent_endpoint") != s.agent_endpoint):
            raise Refused("The retained experiment does not confirm the selected endpoint.")
    cmp = result.get("compare")
    if cmp is not None:
        if (not isinstance(cmp, dict) or not isinstance(cmp.get("verdict"), str)
                or cmp["verdict"] not in report.VERDICT_WORDS):
            raise Refused("A supported, explicit comparison verdict is required.")
        expected_decision = "adopt_candidate" if cmp["verdict"] == "improvement" else "keep_current"
        if cmp.get("decision") != expected_decision:
            raise Refused("The comparison verdict and decision conflict.")
        base = cmp.get("base") or {}
        candidate = cmp.get("candidate") or {}
        if candidate.get("id") != exp.get("id"):
            raise Refused("The comparison refers to another candidate experiment.")
        if any(candidate.get(field) != exp.get(field) for field in
               ("agent_id", "dataset_id", "dataset_version", "repeats", "target", "scorers")):
            raise Refused("The comparison changed the candidate's frozen configuration.")
        if (base.get("id") != s.baseline_experiment
                or (base.get("target") or {}).get("commit_sha") != s.base_commit
                or (base.get("target") or {}).get("effective_agent_endpoint") != s.base_endpoint):
            raise Refused("The retained comparison substituted the protected baseline.")
        if baseline is not None:
            for field in ("id", "agent_id", "dataset_id", "dataset_version", "repeats", "scorers", "target"):
                before, after = baseline.get(field), base.get(field)
                if s.reconcile_report and field == "target":
                    # A redacted historical artifact can reconcile an owned publication,
                    # never attest unrecorded private configuration or write a new result.
                    keys = ("kind", "agent_id", "commit_sha", "agent_endpoint", "effective_agent_endpoint", "endpoint_overridden")
                    before, after = selected(before or {}, keys), selected(after or {}, keys)
                if s.reconcile_report and field == "scorers":
                    before, after = ([selected(sc, ("id", "name")) for sc in side] for side in (before or [], after or []))
                if before != after:
                    raise Refused("The baseline configuration changed between preflight and comparison.")
            if any(exp.get(field) != baseline.get(field) for field in ("agent_id", "dataset_id", "dataset_version", "repeats")):
                raise Refused("The candidate does not preserve the baseline's measurement controls.")
            before = baseline.get("scorers") or []
            after = exp.get("scorers") or []
            keys = SCORER_CONTROLS
            if s.reconcile_report:
                keys = ("id",)
            if s.reconcile_report:
                before, after = (sorted((selected(sc, keys) for sc in side), key=lambda sc: str(sc.get("id")))
                                 for side in (before, after))
            else:
                before, after = scorer_controls(before), scorer_controls(after)
            if before != after:
                raise Refused("The candidate changed the baseline's required scoring controls.")
        row_scope = "held_out" if s.task else "development"
        if any((side.get("progress") or {}).get("row_scope") != row_scope for side in (base, exp, candidate)):
            raise Refused("The retained comparison changed the frozen task or development row scope.")
        if any((side.get("progress") or {}).get("include_rejected") is not False
               for side in (base, exp, candidate)):
            raise Refused("The retained comparison changed or omitted the frozen rejected-row policy.")
        if any((side.get("progress") or {}).get("row_ids") is not None for side in (base, exp, candidate)):
            raise Refused("The retained comparison introduced an unsupported pinned-row selection.")
    task_below = bool(s.task) and report.task_outcome(exp, result.get("task_verdict"))[0] == report.REGRESSION
    if code in (0, 1) and not task_below:
        complete_baseline(exp)
        complete_baseline((cmp or {}).get("base") or {})
        complete_baseline((cmp or {}).get("candidate") or {})
        matched = (cmp or {}).get("matched_rows")
        if (type(matched) is not int or matched <= 0
                or matched != exp["summary"]["rows"] or matched != cmp["base"]["summary"]["rows"]
                or matched != cmp["candidate"]["summary"]["rows"]):
            raise Refused("The comparison does not cover the same complete requested row set.")
    checked, _ = report.outcome(exp, result.get("compare"), None, compare_wanted=True, fail_on="regression",
                                task_wanted=bool(s.task), task_verdict=result.get("task_verdict"))
    if checked != code:
        raise Refused("Missing, incomplete or conflicting evidence cannot pass the evaluation gate.")
    if result.get("verdict") != (result.get("compare") or {}).get("verdict"):
        raise Refused("The evaluation report contains conflicting verdicts.")


def execute(s: Settings, private: Path) -> tuple[int, dict]:
    # No repository/publisher credentials, inherited Python paths, proxies or customer checkout enter the CLI.
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if k in os.environ}
    env.update(AQ_API_KEY=s.api_key, AQ_ENDPOINT=s.endpoint, AQ_UI_URL=s.ui_url)
    args = [sys.executable, "-I", "-m", "aq_sdk.cli", "eval", "run", "--dataset=" + s.dataset,
            "--target=" + s.target, "--agent-endpoint=" + s.agent_endpoint, "--commit=" + s.commit,
            "--compare-to=" + s.baseline_experiment, "--fail-on=regression",
            "--row-scope=" + ("held_out" if s.task else "development"), "--budget=" + s.budget,
            "--timeout=" + str(s.timeout), "--report-file=/dev/null", "--json"]
    if s.task:
        args.append("--task=" + s.task)
    # The public report is written only after the known-secret checks below.
    # Capture privately in memory, never stream raw stdout/stderr to the runner log.
    completed = subprocess.run(args, cwd=private, env=env, capture_output=True, text=True, check=False)
    (private / "result.redacted.private.json").write_text(private_text(completed.stdout, s))
    (private / "stderr.redacted.private.txt").write_text(private_text(completed.stderr, s))
    try:
        result = json.loads(completed.stdout)
        return completed.returncode, result
    except (ValueError, OSError):
        raise MissingReport(completed.returncode) from None


def publish_check(result: dict | None, s: Settings, *, reconcile_only=False, transport=None) -> dict:
    """Verify a nonpassing owned lifecycle before any success/neutral completion."""
    status = "uncertain" if reconcile_only else "failed"
    check_id = None
    try:
        # Only validated, complete reports can produce a merge-acceptable conclusion.
        if result is None:
            conclusion, body = "failure", "Evaluation could not be judged or publication could not be confirmed. Inspect the retained private records."
        else:
            validate_result(result, result.get("exit_code"), s)
            code = result["exit_code"]
            conclusion = ("failure" if code != 0 else "neutral" if result["verdict"] == "no_clear_difference" else "success")
            body = public_body(result, s)
        if len(body.encode("utf-8")) > 60000:
            raise Refused("The sanitized check summary is too large; no check was written.")
        name = "Evaluation: " + s.check_key
        guard_public_strings([name, body], s)
        identity = hashlib.sha256(f"{s.repository}:{s.pull_request}:{s.commit}:{s.base_commit}:{s.baseline_experiment}:{s.check_key}".encode()).hexdigest()
        wanted = {"status": "completed", "conclusion": conclusion, "external_id": identity,
                  "output": {"title": "Evaluation " + conclusion, "summary": body}}
        with github_client(s, transport) as client:
            trusted_pr(s, client)

            def owned():
                matches, seen = [], set()
                for page in range(1, 101):
                    response = client.get(f"/repos/{s.repository}/commits/{s.commit}/check-runs",
                                          params={"check_name": name, "filter": "all", "per_page": 100, "page": page})
                    if response.status_code != 200:
                        raise Refused("The owned check could not be read.")
                    data = response.json()
                    rows = data.get("check_runs") if isinstance(data, dict) else None
                    if not isinstance(rows, list) or len(rows) > 100:
                        raise Refused("GitHub returned an invalid check page.")
                    for row in rows:
                        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0 or row["id"] in seen:
                            raise Refused("Check pages are incomplete or conflicting.")
                        seen.add(row["id"])
                        app = row.get("app") or {}
                        if row.get("name") == name and row.get("external_id") == identity:
                            if not isinstance(app, dict) or type(app.get("id")) is not int:
                                raise Refused("The matching check's ownership is unknown.")
                            if app["id"] == s.check_app_id:
                                if row.get("head_sha") != s.commit:
                                    raise Refused("The owned check refers to another commit.")
                                matches.append(row)
                    if "next" not in response.links and len(rows) < 100:
                        if len(matches) > 1:
                            raise Refused("More than one owned check has this identity; no write was attempted.")
                        return matches[0] if matches else None
                raise Refused("Check pagination is incomplete; no write was attempted.")

            def matches(row, expected=None):
                expected = wanted if expected is None else expected
                return (row is not None and row.get("status") == "completed"
                        and row.get("conclusion") == expected["conclusion"]
                        and selected(row.get("output") or {}, ("title", "summary")) == expected["output"])

            current = owned()
            if current:
                check_id = current["id"]
            if matches(current):
                return {"status": "reconciled" if reconcile_only else "unchanged", "check_run_id": check_id, "conclusion": conclusion}
            if reconcile_only:
                return {"status": "uncertain", "check_run_id": check_id, "conclusion": None}
            # Establish the token's actual check App through a nonpassing write.
            # A configured App ID alone must never authorize a passing POST.
            initial = wanted if conclusion == "failure" else {
                "status": "completed", "conclusion": "failure", "external_id": identity,
                "output": {"title": "Evaluation publication pending", "summary": "Publisher ownership and terminal publication are not yet verified."}}
            originally_existing = current is not None

            def write_once(row, payload):
                nonlocal status, check_id
                method = "PATCH" if row else "POST"
                check_id = row["id"] if row else None
                path = f"/repos/{s.repository}/check-runs" + (f"/{check_id}" if row else "")
                sent = payload if row else {"name": name, "head_sha": s.commit, **payload}
                response = None
                status = "uncertain"
                try:
                    response = client.request(method, path, json=sent)
                except httpx.TransportError:
                    pass  # Reconcile this mutation with reads; do not repeat it.
                if response is not None and 400 <= response.status_code < 500 and response.status_code != 408:
                    status = "failed"
                    return None, False
                final = owned()
                confirmed = matches(final, payload)
                if confirmed:
                    check_id = final["id"]
                return final if confirmed else None, response is None or response.status_code not in (200, 201)

            initialized, uncertain_response = write_once(current, initial)
            if initialized is None:
                return {"status": status, "check_run_id": check_id, "conclusion": None}
            if conclusion != "failure":
                # Only a verified-owned, nonpassing lifecycle may be completed.
                trusted_pr(s, client)
                final, uncertain_response = write_once(initialized, wanted)
                if final is None:
                    return {"status": status, "check_run_id": check_id, "conclusion": None}
            return {"status": "reconciled" if uncertain_response else "updated" if originally_existing else "created",
                    "check_run_id": check_id, "conclusion": conclusion}
    except Exception:
        # Never print an HTTP response, credential, server error or private report.
        pass
    return {"status": status, "check_run_id": check_id, "conclusion": None}


def run(s: Settings, output: Path, *, evaluate: Callable = execute, publish: Callable = publish_pr_comment,
        verify: Callable = preflight, check_publish: Callable = publish_check,
        confirm: Callable = confirm_context) -> int:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    private = output.parent / (output.name + "-private")
    private.mkdir(mode=0o700, exist_ok=False)
    os.chmod(private, 0o700)
    code, status, comment_id = None, "failed", None
    checked = {"status": "failed", "check_run_id": None, "conclusion": None}
    try:
        baseline = verify(s)  # Scorer-only preflight precedes evaluation/session/task dispatch.
        if s.reconcile_report:
            result = json.loads(Path(s.reconcile_report).read_text())
            code = result.get("exit_code")
        else:
            code, result = evaluate(s, private)  # Exactly one CLI invocation. No replacement submission.
        validate_result(result, code, s, baseline)
        confirm(s)  # Recheck head/base drift before either publication side effect.
        aggregate = aggregate_result(result, publish_report_links=s.publish_report_links)
        body = public_body(aggregate, s)
        (output / "aggregate-result.json").write_text(json.dumps(aggregate, indent=2) + "\n")
        (output / "evaluation.md").write_text(body)
        publication = publish(aggregate, repository=s.repository, pull_request=s.pull_request, check_key=s.check_key,
                              github_token=s.github_token, publisher_id=s.publisher_id,
                              reconcile_only=bool(s.reconcile_report))
        status, comment_id = publication.status, publication.comment_id
        confirmed = status in ("created", "updated", "unchanged", "reconciled")
        checked = check_publish(aggregate if confirmed else None, s, reconcile_only=bool(s.reconcile_report))
        checks_confirmed = checked["status"] in ("created", "updated", "unchanged", "reconciled")
        return code if confirmed and checks_confirmed else code or 4
    except MissingReport as exc:
        code = exc.code
        if not s.reconcile_report:
            checked = check_publish(None, s)
        raise
    except Refused:
        if not s.reconcile_report:
            checked = check_publish(None, s)
        raise
    finally:
        # Preserve the actual process exit even when its report or publication failed.
        metadata = {"evaluation_exit_code": code if type(code) is int and code in (0, 1, 2, 3) else None,
                    "publication_status": status, "comment_id": comment_id, "check_publication": checked}
        (output / "publication.json").write_text(json.dumps(metadata, indent=2) + "\n")


def main() -> int:
    code = 2
    output = None
    try:
        s = settings(dict(os.environ))
        output = Path(tempfile.mkdtemp(prefix="aq-report-", dir=os.environ["RUNNER_TEMP"]))
        output.rmdir()  # run creates both output directories with restrictive permissions.
        code = 3
        code = run(s, output)
    except Refused as exc:
        print("Evaluation integration stopped: " + str(exc))
        if isinstance(exc, MissingReport) and exc.code in (1, 2, 3):
            code = exc.code
    except Exception:
        print("Evaluation integration stopped. Inspect the private run records before retrying.")
    if output is not None and (output / "publication.json").is_file():
        metadata = json.loads((output / "publication.json").read_text())
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as f:
            f.write("evaluation-exit-code=" + str(metadata["evaluation_exit_code"]) + "\n")
            f.write("publication-status=" + metadata["publication_status"] + "\n")
            f.write("check-publication-status=" + metadata["check_publication"]["status"] + "\n")
            f.write("report-dir=" + str(output) + "\n")
        print("Evaluation exit " + str(metadata["evaluation_exit_code"]) + ", publication " + metadata["publication_status"] + ".")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

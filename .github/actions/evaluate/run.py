"""Run the existing CLI privately and publish only its validated aggregate report."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable
from urllib.parse import quote, urlsplit
from uuid import UUID

from aq_sdk import publish_pr_comment, report


class Refused(ValueError):
    """A fixed, credential-free integration error."""


class MissingReport(Refused):
    def __init__(self, code):
        super().__init__("No valid report was retained. Inspect the private run before any further submission.")
        self.code = code if type(code) is int and code in (0, 1, 2, 3) else None


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
    task: str = ""


def settings(env: dict[str, str]) -> Settings:
    if env.get("GITHUB_EVENT_NAME") != "pull_request_target":
        raise Refused("Use a trusted base-branch pull_request_target workflow with separate credential-bearing jobs.")
    try:
        event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
        pr = event["pull_request"]
        repository = env["GITHUB_REPOSITORY"]
        number = pr["number"]
        sha = pr["head"]["sha"]
        same_repo = pr["head"]["repo"]["full_name"] == pr["base"]["repo"]["full_name"] == repository
        if not same_repo or pr["head"]["repo"].get("fork") is not False:
            raise Refused("Fork and unknown-repository pull requests cannot use evaluation or publication credentials.")
        if type(number) is not int or number <= 0 or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha):
            raise ValueError
        commit = env["AQ_CI_COMMIT"].strip()
        if commit.lower() != sha.lower():
            raise Refused("The tested commit must match the trusted pull-request head.")
        endpoint_name = env["AQ_CI_AGENT_ENDPOINT"].strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", endpoint_name):
            raise Refused("An AgentCore endpoint name on the connected runtime is required.")
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
        if not env.get("AQ_CI_TARGET", "").strip() or not env.get("AQ_CI_DATASET", "").strip():
            raise Refused("A connected agent and dataset are required.")
        task = env.get("AQ_CI_TASK", "")
        if task:
            try:
                task = str(UUID(task.strip()))
            except ValueError:
                raise Refused("The optional task must be an existing task UUID.") from None
        return Settings(repository, number, commit, endpoint_name, env["AQ_CI_TARGET"], env["AQ_CI_DATASET"],
                        str(budget), api, env.get("AQ_UI_URL", "").strip() or api, env["AQ_API_KEY"],
                        env["AQ_CI_GITHUB_TOKEN"], publisher_id, key, timeout,
                        env.get("AQ_CI_RECONCILE_REPORT", "").strip(), task)
    except Refused:
        raise
    except (KeyError, TypeError, ValueError, OSError, InvalidOperation):
        raise Refused("Trusted pull-request context or required configuration is missing or invalid.") from None


def selected(value: dict, keys: tuple[str, ...]) -> dict:
    return {key: value[key] for key in keys if key in value}


def aggregate_result(result: dict) -> dict:
    """Keep the existing SDK result shape, with only fields its aggregate renderer and gate use."""
    def experiment(exp):
        if exp is None:
            return None
        out = selected(exp, ("id", "agent_id", "dataset_id", "name", "status", "stop_reason"))
        out["target"] = selected(exp.get("target") or {}, ("commit_sha",))
        out["scorers"] = [selected(s, ("id", "name")) for s in exp.get("scorers", [])]
        summary = exp.get("summary") or {}
        out["summary"] = selected(summary, ("rows", "attempts", "target_errors", "timeouts"))
        out["summary"]["scorers"] = [selected(s, ("scorer_id", "name", "mean", "pass_rate", "errors", "pending", "scored"))
                                     for s in summary.get("scorers", [])]
        planning = (exp.get("progress") or {}).get("planning") or {}
        out["progress"] = {"planning": selected(planning, ("text",))}
        return out

    out = selected(result, ("exit_code", "reason", "verdict", "link"))
    out["experiment"] = experiment(result.get("experiment"))
    cmp = result.get("compare")
    out["compare"] = None
    if cmp is not None:
        out["compare"] = selected(cmp, ("matched_rows", "verdict", "decision", "note", "driven_by"))
        out["compare"]["base"] = experiment(cmp["base"])
        out["compare"]["scorers"] = [selected(s, ("scorer_id", "name", "status", "note", "base_mean", "candidate_mean",
            "difference", "improved", "regressed", "unchanged", "excluded", "verdict")) for s in cmp["scorers"]]
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
    if report.pr_summary(out) != report.pr_summary(result):
        raise Refused("The aggregate report does not preserve the installed SDK's report contract.")
    return out


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
    public_strings = [body, *strings(result)]
    for secret in (s.api_key, s.github_token):
        variants = (secret, report._markdown(secret), html.escape(secret), quote(secret, safe=""))
        if any(value and value in text for value in variants for text in public_strings):
            raise Refused("The public aggregate contains a known credential. Publication was refused.")
    return body


def private_text(text: str, s: Settings) -> str:
    """Remove the two known credentials from retained private debug output."""
    for secret in (s.api_key, s.github_token):
        variants = {secret, report._markdown(secret), html.escape(secret), quote(secret, safe="")}
        variants.update(json.dumps(value)[1:-1] for value in tuple(variants))
        for value in sorted(variants, key=len, reverse=True):
            text = text.replace(value, "[credential removed]")
    return text


def validate_result(result: dict, code: int, s: Settings) -> None:
    if not isinstance(result, dict) or type(result.get("exit_code")) is not int or result["exit_code"] != code:
        raise Refused("The CLI did not produce a report matching its exit status.")
    if code not in (0, 1, 2, 3):
        raise Refused("The evaluation returned an unsupported exit status.")
    exp = result.get("experiment")
    if not isinstance(exp, dict) or (exp.get("target") or {}).get("commit_sha") != s.commit:
        raise Refused("The retained experiment does not bind the tested commit.")
    if not s.reconcile_report:
        target = exp.get("target") or {}
        if target.get("agent_endpoint") != s.agent_endpoint or target.get("endpoint_overridden") is not True:
            raise Refused("The retained experiment does not confirm the selected endpoint.")
    task = result.get("task")
    if s.task:
        if (not isinstance(task, dict) or task.get("id") != s.task
                or not task.get("agent_id") or task.get("agent_id") != exp.get("agent_id")
                or not task.get("dataset_id") or task.get("dataset_id") != exp.get("dataset_id")):
            raise Refused("The retained task does not bind the requested task, agent and dataset.")
    elif task is not None or result.get("task_verdict") is not None:
        raise Refused("The retained report contains an unrequested task gate.")
    checked, _ = report.outcome(exp, result.get("compare"), None, compare_wanted=True, fail_on="regression",
                                task_wanted=bool(s.task), task_verdict=result.get("task_verdict"))
    if checked != code:
        raise Refused("Missing, incomplete or conflicting evidence cannot pass the regression gate.")
    if result.get("verdict") != (result.get("compare") or {}).get("verdict"):
        raise Refused("The evaluation report contains conflicting verdicts.")


def execute(s: Settings, private: Path) -> tuple[int, dict]:
    # No repository/publisher credentials, inherited Python paths, proxies or customer checkout enter the CLI.
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if k in os.environ}
    env.update(AQ_API_KEY=s.api_key, AQ_ENDPOINT=s.endpoint, AQ_UI_URL=s.ui_url)
    args = [sys.executable, "-I", "-m", "aq_sdk.cli", "eval", "run", "--dataset=" + s.dataset,
            "--target=" + s.target, "--agent-endpoint=" + s.agent_endpoint, "--commit=" + s.commit,
            "--compare-to=current", "--fail-on=regression", "--row-scope=" + ("held_out" if s.task else "development"), "--budget=" + s.budget,
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


def run(s: Settings, output: Path, *, evaluate: Callable = execute, publish: Callable = publish_pr_comment) -> int:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    private = output.parent / (output.name + "-private")
    private.mkdir(mode=0o700, exist_ok=False)
    os.chmod(private, 0o700)
    code, status, comment_id = None, "failed", None
    try:
        if s.reconcile_report:
            result = json.loads(Path(s.reconcile_report).read_text())
            code = result.get("exit_code")
        else:
            code, result = evaluate(s, private)  # Exactly one CLI invocation. No replacement submission.
        validate_result(result, code, s)
        aggregate = aggregate_result(result)
        aggregate_code, _ = report.outcome(aggregate["experiment"], aggregate.get("compare"), None,
                                           compare_wanted=True, fail_on="regression", task_wanted=bool(s.task),
                                           task_verdict=aggregate.get("task_verdict"))
        if aggregate_code != code:
            raise Refused("The aggregate does not preserve the requested outcome gate.")
        body = public_body(aggregate, s)
        (output / "aggregate-result.json").write_text(json.dumps(aggregate, indent=2) + "\n")
        (output / "evaluation.md").write_text(body)
        publication = publish(aggregate, repository=s.repository, pull_request=s.pull_request, check_key=s.check_key,
                              github_token=s.github_token, publisher_id=s.publisher_id,
                              reconcile_only=bool(s.reconcile_report))
        status, comment_id = publication.status, publication.comment_id
        return code if status in ("created", "updated", "unchanged", "reconciled") else code or 4
    except MissingReport as exc:
        code = exc.code
        raise
    finally:
        # Preserve the actual process exit even when its report or publication failed.
        metadata = {"evaluation_exit_code": code if type(code) is int and code in (0, 1, 2, 3) else None,
                    "publication_status": status, "comment_id": comment_id}
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
            f.write("report-dir=" + str(output) + "\n")
        print("Evaluation exit " + str(metadata["evaluation_exit_code"]) + ", publication " + metadata["publication_status"] + ".")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

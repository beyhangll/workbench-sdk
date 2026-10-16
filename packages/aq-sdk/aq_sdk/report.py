"""Words and exit codes for a finished run. The CLI and ``Eval`` share them.

Exit codes of ``aq eval run``:

- 0: the run is fine. With ``--fail-on regression`` it is not a regression.
- 1: the named task is below its bar, or the requested comparison gate found a regression.
- 2: the command or the connection is wrong. Nothing was judged.
- 3: the run cannot be judged. It did not finish, it had task errors, or no
  rows could be compared. A run that cannot be judged never passes.
"""
from __future__ import annotations

import html
import math
import re
from typing import Any
from urllib.parse import quote, urlsplit

from .config import Config

OK, REGRESSION, USAGE, UNJUDGED = 0, 1, 2, 3
DEVELOPMENT_UNKNOWN = "Development check unknown: frozen must-pass evidence could not be verified."
VERDICT_WORDS = {"improvement": "Improvement", "regression": "Regression", "no_clear_difference": "No clear difference"}
DECISION_WORDS = {"adopt_candidate": "Adopt candidate", "keep_current": "Keep current"}


def link(config: Config, candidate: dict[str, Any], base_id: str | None) -> str | None:
    """The comparison page in the AQ app, or the experiment page when there is no base."""
    try:
        ui = urlsplit(config.ui_url)
        if ui.query or ui.fragment or ui.username is not None or ui.password is not None:
            return None
    except ValueError:
        return None
    agent = candidate.get("agent_id")
    if not agent:
        return None
    if base_id:
        return f"{config.ui_url}/#/agents/{agent}/compare?base={base_id}&candidate={candidate['id']}"
    return f"{config.ui_url}/#/agents/{agent}/experiments/{candidate['id']}"


def _rows(n: int) -> str:
    return f"{n} row" if n == 1 else f"{n} rows"


def _num(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.2f}"


def _finite(value: Any) -> float | int | None:
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def summary_lines(exp: dict[str, Any]) -> list[str]:
    s = exp.get("summary") or {}
    errors = (s.get("target_errors") or 0) + (s.get("timeouts") or 0)
    lines = [f"Experiment \"{exp['name']}\" {exp['status']}. {s.get('rows', '?')} rows, "
             f"{s.get('attempts', '?')} attempts, {errors} task errors."]
    for sc in s.get("scorers") or []:
        rate = "" if sc.get("pass_rate") is None else f", pass rate {sc['pass_rate'] * 100:.0f}%"
        err = f", {sc['errors']} scorer errors" if sc.get("errors") else ""
        lines.append(f"  {sc['name']}: mean {_num(sc.get('mean'))}{rate}{err}")
    planning = (exp.get("progress") or {}).get("planning") or {}
    if planning.get("text"):
        lines.append(planning["text"])
    return lines


def _bounds_line(sc: dict[str, Any]) -> str:
    interval = sc.get("interval")
    interval = interval if isinstance(interval, dict) else {}
    lower, upper = _finite(interval.get("lower")), _finite(interval.get("upper"))
    if lower is not None and upper is not None and lower > upper:
        lower = upper = None
    methods = {"bonferroni-clopper-pearson-discordance": "Paired-difference confidence bounds",
               "bootstrap-percentile-paired-difference": "Descriptive paired-difference bounds"}
    method = interval.get("method")
    method = method if isinstance(method, str) and method in methods else "unknown"
    def bound(value):
        return "unknown" if value is None else f"{value:+.6g}"
    def alpha(value):
        value = _finite(value)
        return f"{value:.6g}" if value is not None and 0 < value < 1 else "unknown"
    # The API's interval.alpha belongs to the lower endpoint. Never infer the upper alpha from it.
    return (f"  {sc['name']}: {methods.get(method, 'Paired-difference bounds')} (candidate minus baseline): "
            f"lower {bound(lower)}, upper {bound(upper)}, method {method}, "
            f"lower one-sided alpha {alpha(interval.get('alpha'))}, "
            f"upper one-sided alpha {alpha(sc.get('alpha_regression'))}.")


def compare_lines(cmp: dict[str, Any]) -> list[str]:
    base = cmp["base"]
    lines = [f"Compared with \"{base['name']}\" on {cmp['matched_rows']} matched rows: "
             f"{VERDICT_WORDS.get(cmp['verdict'], cmp['verdict'])}. {DECISION_WORDS.get(cmp['decision'], cmp['decision'])}."]
    if cmp.get("note"):
        lines.append(f"  {cmp['note']}")
    for sc in cmp["scorers"]:
        if sc["status"] != "compared":
            lines.append(f"  {sc['name']}: not compared. {sc.get('note') or ''}".rstrip())
            continue
        difference = "unknown" if sc["difference"] is None else f"{sc['difference']:+.2f}"
        lines.append(f"  {sc['name']}: {_num(sc['base_mean'])} to {_num(sc['candidate_mean'])} "
                     f"({difference}), better on {_rows(sc['improved'])}, worse on {sc['regressed']}. "
                     f"{VERDICT_WORDS.get(sc['verdict'], sc['verdict'])}.")
        if "unchanged" in sc:
            lines.append(f"  {sc['name']}: {sc['unchanged']} same, {sc.get('excluded', 0)} excluded.")
        lines.append(_bounds_line(sc))
    return lines


def task_outcome(exp: dict[str, Any], verdict: dict[str, Any] | None,
                 error: str | None = None) -> tuple[int, str | None]:
    """Use the server's task verdict; never derive a second bar or confidence bound."""
    if (not isinstance(verdict, dict) or verdict.get("experiment_id") != exp.get("id")
            or type(verdict.get("bar_version")) is not int or verdict["bar_version"] < 1
            or verdict.get("status") not in ("meets", "below", "unknown", "not_evaluable")):
        return UNJUDGED, f"The candidate's task verdict could not be verified. {error or ''}".strip()
    if verdict["status"] == "below":
        return REGRESSION, f"The candidate is below the task's signed bar version {verdict['bar_version']}."
    thresholds = verdict.get("thresholds")
    requirements = verdict.get("requirements", [])
    slices = verdict.get("slices", [])
    worst = verdict.get("worst_slice")
    if (verdict["status"] != "meets" or not isinstance(thresholds, list) or not thresholds
            or any(not isinstance(t, dict) or t.get("status") != "meets"
                   or t.get("metric") == "must_pass" and (t.get("failures") != 0
                       or t.get("must_pass_status") != "meets" or t.get("paired_quality_status") != "meets")
                   for t in thresholds)
            or not isinstance(requirements, list)
            or any(not isinstance(r, dict) or r.get("status") != "meets" for r in requirements)
            or verdict.get("slice_metadata_status", "valid") != "valid" or not isinstance(slices, list)
            or any(not isinstance(s, dict) or s.get("status") != "meets" for s in slices)
            or worst is not None and (not isinstance(worst, dict) or worst.get("status") != "meets")
            or verdict.get("acceptance_reason")):
        return UNJUDGED, "The candidate's task acceptance is unresolved. Its task verdict cannot pass the gate."
    return OK, None


def _task_measurement_lines(v: dict[str, Any]) -> list[str]:
    lines = []
    def pct(value):
        value = _finite(value)
        return "unknown" if value is None else f"{value * 100:.1f}%"
    for t in v.get("thresholds") or []:
        metric = t.get("metric")
        names = {"quality": "Quality", "completion": "Completion", "must_pass": "Must-pass cases",
                 "latency_p95_ms": "Latency p95 (ms)", "cost_per_row_usd": "Historical cost per attempted row (USD)",
                 "cost_per_correct_task_usd": "Cost per fully correct task (USD)"}
        if metric not in names:
            continue
        label = names[metric] + (f" · {t['category']}" if t.get("category") else "")
        label += f" · {t['scorer']}" if t.get("scorer") else ""
        if metric == "must_pass":
            observation = (f"{t.get('failures', 'unknown')} failures in {t.get('cases', 'unknown')} cases, "
                           f"failure rate up to {pct(t.get('bound'))}; bar: 0 failures")
            if t.get("unresolved"):
                observation += f"; {t['unresolved']} unresolved"
            for field, words in (("uncategorized_cases", "uncategorized must-pass cases"),
                                 ("undeclared_category_cases", "must-pass cases in undeclared categories")):
                if t.get(field):
                    observation += f"; {t[field]} {words}"
            observation += (f"; must-pass half: {t.get('must_pass_status', 'unknown')}; "
                            f"paired answerable half: {t.get('paired_quality_status', 'unknown')}")
        elif metric in ("quality", "completion"):
            observation = (f"{pct(t.get('observed'))} of {t.get('cases', 'unknown')} rows, "
                           f"{pct(t.get('bound'))} lower bound; bar: ≥ {pct(t.get('value'))}")
        else:
            observation = f"{_num(_finite(t.get('observed')))} observed; bar: ≤ {_num(_finite(t.get('value')))}"
        lines.append(f"  {label}: {observation}. {t.get('status', 'unknown')}.")
        if t.get("reason"):
            lines.append("    " + t["reason"].replace("_", " "))
    if v.get("acceptance_reason"):
        lines.append("  Acceptance unresolved: " + v["acceptance_reason"].replace("_", " ") + ".")
    for r in v.get("requirements") or []:
        if r.get("status") != "meets":
            lines.append(f"  Acceptance requirement {r.get('code', 'unknown')}: {r.get('status', 'unknown')}.")
    return lines


def task_lines(task: dict[str, Any], verdict: dict[str, Any] | None) -> list[str]:
    """Show the same task counts and server-provided bounds as the board."""
    v = verdict if isinstance(verdict, dict) else {}
    lines = [f"Task \"{task['name']}\" ({task['id']}): {v.get('status', 'unknown')}. "
             f"Signed bar version {v.get('bar_version', 'unknown')}.", *_task_measurement_lines(v)]
    if "slice_metadata_status" in v:
        lines.append(f"  Slice metadata: {v['slice_metadata_status']}.")
    worst = v.get("worst_slice")
    if isinstance(worst, dict):
        lines.append(f"  Worst required slice: {worst.get('dimension', 'unknown')}="
                     f"{worst.get('value') if worst.get('value') is not None else 'unknown'} ({worst.get('status', 'unknown')}).")
    for s in v.get("slices") or []:
        if not isinstance(s, dict) or s.get("status") == "meets":
            continue
        lines.append(f"  Required slice {s.get('dimension', 'unknown')}="
                     f"{s.get('value') if s.get('value') is not None else 'unknown'}: "
                     f"{s.get('status', 'unknown')}, {s.get('cases', 'unknown')} cases.")
        lines.extend("  " + line for line in _task_measurement_lines(s))
        if s.get("reason"):
            lines.append("    " + s["reason"].replace("_", " ") + ".")
    return lines


def development_outcome(exp: dict[str, Any], check: dict[str, Any] | None) -> tuple[int, str | None]:
    """Validate server observations. Never derive a second release bar or confidence bound."""
    unverified = (UNJUDGED, DEVELOPMENT_UNKNOWN)
    if not isinstance(check, dict):
        return unverified
    if (check.get("kind") != "development_must_pass" or check.get("release_acceptance") is not False
            or check.get("row_scope") != "development" or (exp.get("progress") or {}).get("row_scope") != "development"
            or check.get("experiment_id") != exp.get("id")
            or any(check.get(key) != exp.get(key) or exp.get(key) is None for key in
                   ("dataset_id", "dataset_version_id", "dataset_version", "repeats"))
            or any(type(check.get(k)) is not int or check[k] < 1 for k in ("dataset_version", "repeats"))
            or check.get("reason") is not None):
        return unverified
    def integer(value):
        return type(value) is int and value >= 0
    if any(not integer(check.get(k)) for k in ("dataset_rows", "cases", "failures", "unresolved")):
        return unverified
    n, failed, unknown = (check[k] for k in ("cases", "failures", "unresolved"))
    if not n or n > check["dataset_rows"] or failed > n or unknown > n:
        return unverified
    for key, count in (("failed_rows", failed), ("unknown_rows", unknown)):
        values = check.get(key)
        if (not isinstance(values, list) or len(values) != count
                or any(type(v) is not int or not 0 < v <= check["dataset_rows"] for v in values) or len(set(values)) != count):
            return unverified
    expected = "failed" if failed else "unknown" if unknown else "passed"
    specs, observations = exp.get("scorers"), check.get("scorers")
    if (check.get("status") != expected or not isinstance(specs, list) or not specs
            or not isinstance(observations, list) or len(observations) != len(specs)
            or any(not isinstance(s, dict) or not isinstance(s.get("id"), str) for s in specs)
            or len({s["id"] for s in specs}) != len(specs)
            or any(not isinstance(s, dict) or type(s.get("cases")) is not int or s["cases"] != n
                   or any(not integer(s.get(k)) or s[k] > n for k in ("failures", "unresolved")) for s in observations)
            or {s.get("scorer_id") for s in observations} != {s["id"] for s in specs}
            or len({s.get("scorer_id") for s in observations}) != len(observations)
            or not max(s["failures"] for s in observations) <= failed <= sum(s["failures"] for s in observations)
            or not max(s["unresolved"] for s in observations) <= unknown <= sum(s["unresolved"] for s in observations)):
        return unverified
    for observed in (check, *observations):
        bound = _finite(observed.get("bound"))
        if observed["unresolved"] and bound is not None or not observed["unresolved"] and (bound is None or not 0 <= bound <= 1):
            return unverified
    if failed:
        return REGRESSION, f"Development check failed: {failed} of {n} must-pass cases failed."
    if unknown:
        return UNJUDGED, f"Development check unknown: {unknown} of {n} must-pass cases could not be scored."
    return OK, None


def development_lines(result: dict[str, Any]) -> list[str]:
    """Fixed wording and numeric ordinals only. Never render case content or private labels."""
    check = result.get("development_check")
    checked, reason = development_outcome(result.get("experiment") or {}, check)
    lines = ["## AQ development check", "Development only. Not release acceptance."]
    if checked != OK:
        lines.append(reason)
    else:
        lines.append(f"Development must-pass condition passed: all {check['cases']} cases passed.")
        if result.get("exit_code") != OK:
            lines.append("Development check did not pass the other existing check controls.")
    if isinstance(check, dict) and reason != DEVELOPMENT_UNKNOWN:
        for key, label in (("failed_rows", "Failed must-pass rows"), ("unknown_rows", "Unknown must-pass rows")):
            values = check.get(key) or []
            lines.append(label + ": " + (", ".join(str(v) for v in values) if values else "none") + ".")
        lines.append(f"Row numbers refer to frozen dataset version {check['dataset_version']}.")
        bound = _finite(check.get("bound"))
        lines.append("Must-pass failure-rate upper bound: " + (f"{bound:.6g}." if bound is not None else "unknown."))
    cmp = result.get("compare") or {}
    lines.append("Statistical result: " + VERDICT_WORDS.get(cmp.get("verdict"), "unknown") + ".")
    return lines


def outcome(exp: dict[str, Any], cmp: dict[str, Any] | None, compare_error: str | None, *, compare_wanted: bool,
            fail_on: str | None, allow_errors: int = 0, task_wanted: bool = False,
            task_verdict: dict[str, Any] | None = None, task_error: str | None = None,
            development_wanted: bool = False, development_check: dict[str, Any] | None = None) -> tuple[int, str | None]:
    """The exit code and the reason. See the module notes for what each code means."""
    if development_wanted:
        if task_wanted:
            return UNJUDGED, "Development checks cannot establish signed task acceptance."
        code, reason = development_outcome(exp, development_check)
        if code != OK:
            return code, reason
    if exp["status"] != "completed":
        why = exp.get("stop_reason") or "No reason was given."
        return UNJUDGED, f"The experiment ended as {exp['status']}. {why}"
    if task_wanted:
        task_code, task_reason = task_outcome(exp, task_verdict, task_error)
        if task_code != OK:
            return task_code, task_reason
    s = exp.get("summary") or {}
    pending = [sc["name"] for sc in s.get("scorers", []) if sc.get("pending", 0)]
    if pending:
        return UNJUDGED, "Scoring is incomplete. Still waiting for: " + ", ".join(pending) + "."
    if not fail_on:
        return OK, None
    if compare_wanted and cmp is None:
        return UNJUDGED, f"The comparison could not be made. {compare_error or ''}".strip()
    if cmp is not None and cmp["matched_rows"] == 0:
        return UNJUDGED, "No rows could be compared, so nothing was judged. Check that both runs scored the same rows."
    if cmp is not None and not any(sc.get("status") == "compared" for sc in cmp.get("scorers", [])):
        return UNJUDGED, "No scorer could be compared, so nothing was judged."
    required = {sc["id"]: sc["name"] for sc in exp.get("scorers", [])}
    if not required:
        return UNJUDGED, "The experiment did not report its requested scorers, so the gate cannot be checked."
    for side in (exp, cmp["base"]) if cmp is not None else (exp,):
        summary = side.get("summary") or {}
        rows, repeats = summary.get("rows"), side.get("repeats")
        if (side.get("status") != "completed"
                or type(rows) is not int or rows <= 0 or type(repeats) is not int or repeats <= 0
                or type(summary.get("attempts")) is not int or summary["attempts"] != rows * repeats
                or type(summary.get("ok")) is not int or not 0 < summary["ok"] <= rows * repeats
                or any(type(summary.get(key)) is not int or summary[key] < 0
                       for key in ("target_errors", "timeouts"))
                or summary["ok"] + summary["target_errors"] + summary["timeouts"] != rows * repeats
                or any(key in summary and (type(summary[key]) is not int or summary[key] != 0)
                       for key in ("not_run", "rows_not_run"))
                or any(key in (side.get("progress") or {})
                       and (type(side["progress"][key]) is not int or side["progress"][key] != rows * repeats)
                       for key in ("total", "finished"))):
            return UNJUDGED, "The candidate or baseline has incomplete or unverified planned attempts."
        summaries = {sc["scorer_id"]: sc for sc in summary.get("scorers", [])}
        missing = [name for sid, name in required.items() if sid not in summaries]
        if missing:
            return UNJUDGED, "Scoring is incomplete. No result was reported for: " + ", ".join(missing) + "."
        incomplete = [name for sid, name in required.items()
                      if summaries[sid].get("errors") != 0 or summaries[sid].get("pending") != 0
                      or type(summaries[sid].get("scored")) is not int
                      or summaries[sid]["scored"] != summary["ok"]]
        if incomplete:
            return UNJUDGED, "Scoring is incomplete or errored for: " + ", ".join(incomplete) + "."
    errors = s["target_errors"] + s["timeouts"]
    if errors > allow_errors:
        return UNJUDGED, (f"{errors} rows had task errors, and {allow_errors} are allowed. "
                          "Fix the task or raise the allowed errors.")
    if cmp is not None:
        compared = {sc["scorer_id"] for sc in cmp.get("scorers", []) if sc.get("status") == "compared"}
        unavailable = [name for sid, name in required.items() if sid not in compared]
        if unavailable:
            return UNJUDGED, "Requested scorers are not all comparable: " + ", ".join(unavailable) + "."
        statistics = {sc["scorer_id"]: sc for sc in cmp.get("scorers", []) if sc.get("status") == "compared"}
        unpaired = [name for sid, name in required.items()
                    if any(statistics[sid].get(field) is None
                           for field in ("base_mean", "candidate_mean", "difference"))]
        if unpaired:
            return UNJUDGED, "No scored pairs were reported for: " + ", ".join(unpaired) + "."
    if cmp is not None and cmp["verdict"] == "regression":
        by_id = {sc["scorer_id"]: sc["name"] for sc in cmp["scorers"]}
        names = ", ".join(by_id.get(i, i) for i in cmp.get("driven_by") or []) or "a scorer"
        return REGRESSION, f"Regression on {names}."
    return OK, None


def _markdown(text: str) -> str:
    text = html.escape(text).replace("@", "&#64;")
    return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", text).replace("\n", " ")


def pr_summary(result: dict[str, Any]) -> str:
    """Render only aggregate report fields. Never include row inputs, outputs, keys or target settings."""
    if result.get("development_must_pass") is True:
        return "\n\n".join(development_lines(result)) + "\n"
    code, exp, cmp = result.get("exit_code"), result.get("experiment"), result.get("compare")
    task = result.get("task")
    lines = ["## AQ evaluation"]
    if code == REGRESSION:
        lines.append("Result: below the task bar." if (result.get("task_verdict") or {}).get("status") == "below"
                     else "Result: regression.")
    elif code != OK:
        lines.append("Result: not judged.")
    elif task is not None:
        lines.append("Result: meets the task bar.")
    elif cmp is not None:
        lines.append(f"Result: {VERDICT_WORDS.get(cmp['verdict'], cmp['verdict'])}.")
    else:
        lines.append("Evaluation completed; no comparison verdict.")
    if exp:
        lines.extend(_markdown(line) for line in summary_lines(exp))
        commit = (exp.get("target") or {}).get("commit_sha")
        if commit:
            lines.append("Caller-declared commit: " + _markdown(commit) + ". Deployment identity is not attested.")
    if cmp:
        lines.extend(_markdown(line) for line in compare_lines(cmp))
    if task:
        lines.extend(_markdown(line) for line in task_lines(task, result.get("task_verdict")))
    if result.get("reason"):
        lines.append(_markdown(result["reason"]))
    url = result.get("link")
    if isinstance(url, str):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.query
                     and not any(c.isspace() or ord(c) < 32 or c == "\\" for c in parsed.netloc))
            parsed.port  # Reject malformed ports without including the URL in an error.
        except ValueError:
            valid = False
        if valid:
            safe = quote(url, safe=":/?#[]@!$&'*,;=+%")
            lines.append(f"[Open evaluation]({safe})")
    return "\n\n".join(lines) + "\n"


def comment_marker(key: str) -> str:
    """A stable caller-chosen check key, independent of the tested commit."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", key):
        raise ValueError("The comment key must be 1–80 letters, digits, dots, underscores or hyphens.")
    return f"<!-- aq-evaluation:{key} -->"


def existing_comment_id(comments: list[dict[str, Any]], *, key: str, author_id: int) -> int | None:
    """Select the oldest exact marked comment by the authenticated publisher, never another user's comment.

    The caller supplies all pages and performs the create/update using its separate repository credentials.
    """
    if type(author_id) is not int or author_id <= 0:
        raise ValueError("A trusted publisher user ID is required.")
    marker = comment_marker(key)
    matches = [c["id"] for c in comments if type(c.get("id")) is int
               and (c.get("user") or {}).get("id") == author_id
               and (c.get("body") or "").splitlines()[:1] == [marker]]
    return min(matches) if matches else None

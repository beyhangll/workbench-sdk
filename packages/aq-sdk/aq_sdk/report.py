"""Words and exit codes for a finished run. The CLI and ``Eval`` share them.

Exit codes of ``aq eval run``:

- 0: the run is fine. With ``--fail-on regression`` it is not a regression.
- 1: ``--fail-on regression`` and the comparison found a regression.
- 2: the command or the connection is wrong. Nothing was judged.
- 3: the run cannot be judged. It did not finish, it had task errors, or no
  rows could be compared. A run that cannot be judged never passes.
"""
from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote, urlsplit

from .config import Config

OK, REGRESSION, USAGE, UNJUDGED = 0, 1, 2, 3
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
    return lines


def outcome(exp: dict[str, Any], cmp: dict[str, Any] | None, compare_error: str | None, *, compare_wanted: bool,
            fail_on: str | None, allow_errors: int = 0) -> tuple[int, str | None]:
    """The exit code and the reason. See the module notes for what each code means."""
    if exp["status"] != "completed":
        why = exp.get("stop_reason") or "No reason was given."
        return UNJUDGED, f"The experiment ended as {exp['status']}. {why}"
    s = exp.get("summary") or {}
    pending = [sc["name"] for sc in s.get("scorers", []) if sc.get("pending", 0)]
    if pending:
        return UNJUDGED, "Scoring is incomplete. Still waiting for: " + ", ".join(pending) + "."
    if not fail_on:
        return OK, None
    errors = (s.get("target_errors") or 0) + (s.get("timeouts") or 0)
    if errors > allow_errors:
        return UNJUDGED, (f"{errors} rows had task errors, and {allow_errors} are allowed. "
                          "Fix the task or raise the allowed errors.")
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
        summaries = {sc["scorer_id"]: sc for sc in (side.get("summary") or {}).get("scorers", [])}
        missing = [name for sid, name in required.items() if sid not in summaries]
        if missing:
            return UNJUDGED, "Scoring is incomplete. No result was reported for: " + ", ".join(missing) + "."
        incomplete = [name for sid, name in required.items()
                      if summaries[sid].get("errors") != 0 or summaries[sid].get("pending") != 0
                      or not summaries[sid].get("scored")]
        if incomplete:
            return UNJUDGED, "Scoring is incomplete or errored for: " + ", ".join(incomplete) + "."
    if cmp is not None:
        compared = {sc["scorer_id"] for sc in cmp.get("scorers", []) if sc.get("status") == "compared"}
        unavailable = [name for sid, name in required.items() if sid not in compared]
        if unavailable:
            return UNJUDGED, "Requested scorers are not all comparable: " + ", ".join(unavailable) + "."
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
    code, exp, cmp = result.get("exit_code"), result.get("experiment"), result.get("compare")
    lines = ["## AQ evaluation"]
    if code == REGRESSION:
        lines.append("Result: regression.")
    elif code != OK:
        lines.append("Result: not judged.")
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

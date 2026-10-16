"""A completed subset must not pass the CLI's requested regression gate."""
from copy import deepcopy
import json

import pytest

from aq_sdk import report
from conftest import comparison, experiment
from test_cli import run


@pytest.mark.parametrize("side", ["candidate", "baseline"])
def test_unrun_rows_cannot_produce_a_passing_pr_report(client, aq, tmp_path, side):
    exp = aq.result if side == "candidate" else aq.compare["base"]
    exp["summary"].update(attempts=1, ok=1, not_run=2, rows_not_run=2)
    exp["summary"]["scorers"][0]["scored"] = 1
    exp["progress"]["finished"] = 1
    original = deepcopy(exp)
    path = tmp_path / "report.md"

    code, output, _ = run(client, "--compare-to", "current", "--fail-on", "regression",
                          "--json", "--report-file", str(path))

    assert code == report.UNJUDGED
    assert json.loads(output)["reason"]
    assert "Result: not judged." in path.read_text()
    assert "Result: No clear difference." not in path.read_text()
    # The report does not repair evidence or dispatch a replacement evaluation.
    for key in ("summary", "progress"):
        assert exp[key] == original[key]
    assert [p for m, p, _ in aq.calls if m == "POST"] == ["/v1/experiments"]


@pytest.mark.parametrize("side", ["candidate", "baseline"])
@pytest.mark.parametrize("missing", ["rows", "attempts", "ok", "repeats"])
def test_unknown_plan_or_attempt_counts_cannot_pass(side, missing):
    exp, cmp = experiment("candidate", "Candidate"), comparison()
    record = exp if side == "candidate" else cmp["base"]
    (record if missing == "repeats" else record["summary"]).pop(missing)
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression")[0] == report.UNJUDGED


@pytest.mark.parametrize("side", ["candidate", "baseline"])
@pytest.mark.parametrize("field,value", [
    ("rows", 0), ("repeats", True), ("attempts", True), ("attempts", 4),
    ("ok", 0), ("ok", True), ("ok", 4), ("not_run", 1), ("not_run", None),
    ("rows_not_run", 1), ("rows_not_run", False), ("total", 2), ("finished", 2),
])
def test_conflicting_or_invalid_completion_cannot_pass(side, field, value):
    exp, cmp = experiment("candidate", "Candidate"), comparison()
    record = exp if side == "candidate" else cmp["base"]
    container = record if field == "repeats" else record["progress"] if field in ("total", "finished") else record["summary"]
    container[field] = value
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression")[0] == report.UNJUDGED


@pytest.mark.parametrize("side", ["candidate", "baseline"])
@pytest.mark.parametrize("scored", [1, 4, True, None])
def test_every_successful_attempt_needs_its_requested_score(side, scored):
    exp, cmp = experiment("candidate", "Candidate"), comparison()
    record = exp if side == "candidate" else cmp["base"]
    record["summary"]["scorers"][0]["scored"] = scored
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression")[0] == report.UNJUDGED


def test_a_nonterminal_baseline_cannot_pass():
    exp, cmp = experiment("candidate", "Candidate"), comparison()
    cmp["base"]["status"] = "running"
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression")[0] == report.UNJUDGED


def test_repetitions_count_attempts_without_multiplying_rows():
    exp, cmp = experiment("candidate", "Candidate"), comparison()
    for record in (exp, cmp["base"]):
        record["repeats"] = 2
        record["summary"].update(attempts=6, ok=6, not_run=0, rows_not_run=0)
        record["summary"]["scorers"][0]["scored"] = 6
        record["progress"].update(total=6, finished=6)
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression")[0] == report.OK
    exp["summary"]["attempts"] = 3
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression")[0] == report.UNJUDGED


def test_allowed_task_error_does_not_require_an_impossible_score(client, aq, tmp_path):
    aq.result["summary"].update(ok=2, target_errors=1)
    aq.result["summary"]["scorers"][0]["scored"] = 2
    path = tmp_path / "report.md"
    code, _, _ = run(client, "--compare-to", "current", "--fail-on", "regression",
                      "--allow-errors", "1", "--report-file", str(path))
    assert code == report.OK and "Result: No clear difference." in path.read_text()
    assert [p for m, p, _ in aq.calls if m == "POST"] == ["/v1/experiments"]


def test_legacy_optional_counters_are_not_invented():
    exp, cmp = experiment("candidate", "Candidate"), comparison()
    for record in (exp, cmp["base"]):
        record.pop("progress")
    before = deepcopy((exp, cmp))
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression")[0] == report.OK
    assert (exp, cmp) == before


def test_unrun_evidence_remains_readable_without_a_regression_gate(client, aq, tmp_path):
    aq.result["summary"].update(attempts=1, ok=1, not_run=2, rows_not_run=2)
    aq.result["summary"]["scorers"][0]["scored"] = 1
    path = tmp_path / "diagnostic.md"
    code, _, _ = run(client, "--compare-to", "current", "--report-file", str(path))
    assert code == report.OK and "Result: No clear difference." in path.read_text()


@pytest.mark.parametrize("side", ["candidate", "baseline"])
@pytest.mark.parametrize("errors,timeouts", [(0, 0), (2, 0), (None, 0), (0, None), (True, 0), (-1, 0)])
def test_terminal_categories_must_account_for_every_attempt(side, errors, timeouts):
    exp, cmp = experiment("candidate", "Candidate"), comparison()
    record = exp if side == "candidate" else cmp["base"]
    record["summary"].update(ok=2, target_errors=errors, timeouts=timeouts)
    record["summary"]["scorers"][0]["scored"] = 2
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression", allow_errors=3)[0] == report.UNJUDGED


@pytest.mark.parametrize("field", ["target_errors", "timeouts"])
def test_missing_terminal_categories_are_unknown(field):
    exp, cmp = experiment("candidate", "Candidate"), comparison()
    cmp["base"]["summary"].pop(field)
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression")[0] == report.UNJUDGED


def test_completed_error_and_timeout_keep_the_allowed_error_policy():
    exp, cmp = experiment("candidate", "Candidate"), comparison()
    exp["summary"].update(ok=1, target_errors=1, timeouts=1)
    exp["summary"]["scorers"][0]["scored"] = 1
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression", allow_errors=2)[0] == report.OK
    assert report.outcome(exp, cmp, None, compare_wanted=True, fail_on="regression", allow_errors=1)[0] == report.UNJUDGED

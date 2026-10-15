"""Task PR gates consume the existing API verdict; no model or endpoint calls."""
from copy import deepcopy
import json

import pytest

from aq_sdk import report
from conftest import AGENT, DS, SC1
from test_cli import run

TASK = "d0000000-0000-0000-0000-000000000001"


@pytest.fixture
def task_gate(aq):
    aq.tasks = [{"id": TASK, "name": "Answer questions", "agent_id": AGENT, "dataset_id": DS,
                 "bar": {"version": 2}}]
    aq.task_verdict = {"status": "meets", "bar_version": 2, "experiment_id": aq.result["id"],
                       "thresholds": [{"metric": "quality", "value": 0.8, "scorer_id": SC1,
                                       "status": "meets", "observed": 1, "bound": 0.91, "cases": 30}],
                       "requirements": []}
    return aq


@pytest.mark.parametrize("reference", [TASK, "Answer questions"])
def test_named_task_passes_only_with_the_candidate_server_verdict(client, task_gate, reference, tmp_path):
    path = tmp_path / "task.md"
    code, output, err = run(client, "--compare-to", "current", "--task", reference,
                            "--json", "--report-file", str(path))
    result = json.loads(output)
    assert code == 0 and "Error:" not in err
    assert result["task"]["id"] == TASK and result["task_verdict"] == task_gate.task_verdict
    assert "Result: meets the task bar." in path.read_text()
    assert "91.0% lower bound" in path.read_text() and "bar: ≥ 80.0%" in path.read_text()
    assert len([p for m, p, _ in task_gate.calls if m == "POST"]) == 1


def test_task_gate_needs_no_comparison_when_scorers_are_explicit(client, task_gate):
    code, output, _ = run(client, "--scorer", "States the value", "--task", TASK)
    assert code == 0 and "Result: pass. The candidate meets the task's signed bar." in output
    assert not any(p == "/v1/compare" for _, p, _ in task_gate.calls)


@pytest.mark.parametrize("reference", ["", "   "])
def test_empty_task_selector_refuses_before_any_call(client, aq, reference):
    code, _, err = run(client, "--compare-to", "current", "--task", reference)
    assert code == 2 and "--task needs" in err and not aq.calls


@pytest.mark.parametrize("status,code", [("below", 1), ("unknown", 3)])
def test_required_slice_uses_server_overall_verdict_and_reports_its_bound(client, task_gate, tmp_path, status, code):
    threshold = {**task_gate.task_verdict["thresholds"][0], "cases": 10, "observed": 0.7,
                 "bound": 0.4321 if status == "below" else None, "status": status}
    task_gate.task_verdict.update(status=status, aggregate_status="meets", slice_metadata_status="valid",
        worst_slice={"dimension": "language", "value": "tr", "status": status},
        slices=[{"dimension": "language", "value": "tr", "cases": 10, "status": status, "thresholds": [threshold]}])
    path = tmp_path / "slice.md"
    actual, output, _ = run(client, "--compare-to", "current", "--task", TASK, "--report-file", str(path))
    assert actual == code
    for text in (output, path.read_text()):
        assert f"Worst required slice: language=tr ({status})" in text
        assert f"Required slice language=tr: {status}, 10 cases" in text
        assert ("43.2% lower bound" if status == "below" else "unknown lower bound") in text


def test_missing_slice_tag_stays_unknown_and_is_reported(client, task_gate, tmp_path):
    task_gate.task_verdict.update(status="unknown", slice_metadata_status="unknown", slices=[{
        "dimension": "entity", "value": None, "cases": 3, "status": "unknown", "thresholds": [],
        "reason": "missing_slice_tag"}])
    path = tmp_path / "missing-tag.md"
    code, _, _ = run(client, "--compare-to", "current", "--task", TASK, "--report-file", str(path))
    assert code == 3
    assert "Slice metadata: unknown" in path.read_text() and "missing slice tag" in path.read_text()


@pytest.mark.parametrize("comparison", ["improvement", "no_clear_difference"])
def test_below_task_blocks_even_when_comparison_would_pass(client, task_gate, comparison, tmp_path):
    task_gate.compare["verdict"] = comparison
    task_gate.task_verdict["status"] = "below"
    task_gate.task_verdict["thresholds"][0].update(status="below", observed=0.7, bound=0.55)
    path = tmp_path / "task.md"
    code, output, _ = run(client, "--compare-to", "current", "--fail-on", "regression", "--task", TASK,
                          "--json", "--report-file", str(path))
    assert code == 1 and "below the task's signed bar version 2" in json.loads(output)["reason"]
    assert "Result: below the task bar." in path.read_text() and "55.0% lower bound" in path.read_text()


def test_one_must_pass_failure_blocks_and_reports_the_exact_server_bound(client, task_gate, tmp_path):
    task_gate.task_verdict.update(status="below", thresholds=[{
        "metric": "must_pass", "value": 0, "scorer_id": SC1, "category": "refuse", "status": "below",
        "must_pass_status": "below", "paired_quality_status": "meets", "failures": 1, "cases": 30,
        "bound": 0.143234, "unresolved": 0}])
    path = tmp_path / "must-pass.md"
    code, output, _ = run(client, "--compare-to", "current", "--task", TASK, "--report-file", str(path))
    assert code == 1
    for text in (output, path.read_text()):
        assert "1 failures in 30 cases, failure rate up to 14.3%" in text
        assert "must-pass half: below; paired answerable half: meets" in text


@pytest.mark.parametrize("change", [
    {"status": "unknown"}, {"status": "not_evaluable"}, {"status": "unrecognized"},
    {"experiment_id": "another-experiment"}, {"bar_version": None}, {"bar_version": True},
    {"thresholds": []}, {"acceptance_reason": "held_out_required"},
    {"requirements": [{"code": "slice_coverage", "status": "unknown"}]},
    {"thresholds": [{"metric": "must_pass", "status": "meets", "failures": 1}]},
    {"thresholds": [{"metric": "quality", "status": "unknown"}]},
])
def test_unknown_incomplete_or_conflicting_task_evidence_cannot_pass(client, task_gate, change):
    task_gate.task_verdict.update(deepcopy(change))
    code, output, _ = run(client, "--compare-to", "current", "--task", TASK, "--json")
    assert code == 3 and json.loads(output)["reason"]


def test_unavailable_task_verdict_keeps_the_run_without_retrying_it(client, task_gate, tmp_path):
    task_gate.errors[("GET", f"/v1/experiments/{task_gate.result['id']}/verdict")] = (
        409, {"code": "no_bar", "message": "The task bar is unavailable."})
    path = tmp_path / "unknown.md"
    code, output, _ = run(client, "--compare-to", "current", "--task", TASK,
                          "--json", "--report-file", str(path))
    assert code == 3 and json.loads(output)["task_verdict"] is None
    assert "Result: not judged." in path.read_text()
    assert len([p for m, p, _ in task_gate.calls if m == "POST"]) == 1


@pytest.mark.parametrize("change", [{"agent_id": "another-agent"}, {"dataset_id": "another-dataset"}, {"bar": None}])
def test_wrong_agent_dataset_or_unsigned_task_refuses_before_submission(client, task_gate, change):
    task_gate.tasks[0].update(change)
    code, _, err = run(client, "--compare-to", "current", "--task", TASK)
    assert code == 2 and err
    assert not any(m == "POST" for m, _, _ in task_gate.calls)


def test_ambiguous_task_name_refuses_before_submission(client, task_gate):
    task_gate.tasks.append({**task_gate.tasks[0], "id": "another-task"})
    code, _, err = run(client, "--compare-to", "current", "--task", "Answer questions")
    assert code == 2 and "More than one task" in err
    assert not any(m == "POST" for m, _, _ in task_gate.calls)


def test_comparison_regression_still_blocks_a_task_that_meets(client, task_gate):
    task_gate.compare["verdict"] = "regression"
    code, output, _ = run(client, "--compare-to", "current", "--fail-on", "regression", "--task", TASK)
    assert code == 1 and "Regression on" in output


def test_task_failure_is_retained_even_with_other_unresolved_scoring(task_gate):
    task_gate.result["summary"]["scorers"][0]["pending"] = 1
    task_gate.task_verdict["status"] = "below"
    code, _ = report.outcome(task_gate.result, None, None, compare_wanted=False, fail_on=None,
                            task_wanted=True, task_verdict=task_gate.task_verdict)
    assert code == 1

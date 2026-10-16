"""Development checks are explicit, frozen and never release acceptance."""

from copy import deepcopy
import json

import pytest

from aq_sdk import report
from aq_sdk.errors import AQError
from test_cli import run


def observation(exp, *, failures=0, unknown=0):
    return {
        "kind": "development_must_pass",
        "release_acceptance": False,
        "experiment_id": exp["id"],
        **{
            k: exp[k]
            for k in ("dataset_id", "dataset_version", "dataset_version_id", "repeats")
        },
        "row_scope": "development",
        "status": "failed" if failures else "unknown" if unknown else "passed",
        "dataset_rows": 5,
        "cases": 5,
        "failures": failures,
        "unresolved": unknown,
        "failed_rows": list(range(1, failures + 1)),
        "unknown_rows": list(range(5 - unknown + 1, 6)),
        "bound": None if unknown else 0.8,
        "reason": None,
        "scorers": [
            {
                "scorer_id": exp["scorers"][0]["id"],
                "cases": 5,
                "failures": failures,
                "unresolved": unknown,
                "bound": None if unknown else 0.8,
            }
        ],
    }


@pytest.fixture
def development(aq):
    aq.result.update(
        dataset_version_id="fictional-frozen-version",
        progress={"row_scope": "development", "total": 3},
    )
    aq.result["development_check"] = observation(aq.result)
    aq.statuses = ["completed"]
    return aq


@pytest.mark.parametrize("verdict", ["no_clear_difference", "improvement"])
def test_failure_blocks_even_without_a_statistical_fail_option(
    client, development, verdict, tmp_path
):
    development.compare["verdict"] = verdict
    development.result["development_check"] = observation(
        development.result, failures=2
    )
    path = tmp_path / "comment.md"
    code, output, _ = run(
        client,
        "--compare-to",
        "current",
        "--development-must-pass",
        "--json",
        "--report-file",
        str(path),
    )
    assert code == 1
    result = json.loads(output)
    assert result["compare"]["verdict"] == verdict
    assert (
        "Development check failed: 2 of 5 must-pass cases failed." in path.read_text()
    )
    assert "Failed must-pass rows: 1, 2." in path.read_text()
    assert "Not release acceptance" in path.read_text()
    assert development.sent("POST", "/v1/experiments")["row_scope"] == "development"


def test_all_passing_leaves_the_existing_comparison_result(client, development):
    code, output, _ = run(
        client,
        "--compare-to",
        "current",
        "--fail-on",
        "regression",
        "--development-must-pass",
        "--json",
    )
    assert code == 0 and json.loads(output)["verdict"] == "no_clear_difference"


def test_unknown_cases_are_listed_and_do_not_pass(client, development, tmp_path):
    development.result["development_check"] = observation(development.result, unknown=1)
    path = tmp_path / "comment.md"
    code, _, _ = run(
        client,
        "--scorer",
        "States the value",
        "--development-must-pass",
        "--report-file",
        str(path),
    )
    assert code == 3
    assert "Unknown must-pass rows: 5." in path.read_text()
    assert "could not be scored" in path.read_text()


@pytest.mark.parametrize(
    "change",
    ["missing", "version", "run", "bool", "rows", "release", "scorer", "bound"],
)
def test_missing_or_conflicting_observation_cannot_pass(client, development, change):
    check = development.result["development_check"]
    if change == "missing":
        development.result.pop("development_check")
    elif change == "version":
        check["dataset_version"] += 1
    elif change == "run":
        check["experiment_id"] = "other-run"
    elif change == "bool":
        check["failures"] = False
    elif change == "rows":
        check["unknown_rows"] = ["protected content"]
    elif change == "release":
        check["release_acceptance"] = True
    elif change == "scorer":
        check["scorers"] = []
    else:
        check["bound"] = None
    code, output, _ = run(
        client, "--scorer", "States the value", "--development-must-pass", "--json"
    )
    assert code == 3 and "could not be verified" in json.loads(output)["reason"]


@pytest.mark.parametrize(
    "args", [("--task", "task"), ("--row-scope", "held_out"), ("--row-scope", "all")]
)
def test_incompatible_release_or_scope_refuses_before_start(client, development, args):
    code, _, _ = run(client, "--development-must-pass", *args)
    assert code == 2 and not any(m == "POST" for m, _, _ in development.calls)


def test_new_condition_does_not_change_legacy_or_signed_task_gate(development):
    exp = development.result
    bad = observation(exp, failures=1)
    assert report.outcome(exp, None, None, compare_wanted=False, fail_on=None)[0] == 0
    assert report.task_outcome(exp, bad)[0] == 3
    assert (
        report.outcome(
            exp,
            None,
            None,
            compare_wanted=False,
            fail_on=None,
            task_wanted=True,
            task_verdict=bad,
        )[0]
        == 3
    )


def test_public_development_comment_contains_only_approved_projection(development):
    result = {
        "experiment": development.result,
        "development_must_pass": True,
        "development_check": observation(development.result, failures=1),
        "exit_code": 1,
        "reason": "private fictional diagnostic",
        "compare": deepcopy(development.compare),
    }
    result["experiment"]["name"] = result["compare"]["base"]["name"] = (
        "private fictional diagnostic"
    )
    result["compare"]["rows"] = [{"input": "private fictional diagnostic"}]
    body = report.pr_summary(result)
    assert "private fictional diagnostic" not in body
    assert "Failed must-pass rows: 1." in body


def test_masked_readback_stays_unknown_without_error_echo_or_redispatch(
    client, development, monkeypatch, tmp_path
):
    def refused(_):
        raise AQError("private fictional diagnostic", code="masking_unavailable")

    monkeypatch.setattr(client, "development_check", refused)
    path = tmp_path / "comment.md"
    code, _, _ = run(
        client,
        "--scorer",
        "States the value",
        "--development-must-pass",
        "--report-file",
        str(path),
    )
    assert code == 3 and "private fictional diagnostic" not in path.read_text()
    assert (
        sum(m == "POST" and p == "/v1/experiments" for m, p, _ in development.calls)
        == 1
    )

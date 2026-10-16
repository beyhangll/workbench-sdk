"""Retain aggregate comparison bounds without changing evaluation verdicts."""
from copy import deepcopy

import pytest

from aq_sdk import report
from conftest import comparison, experiment
from test_cli import run


@pytest.mark.parametrize("method,description", [
    ("bonferroni-clopper-pearson-discordance", "Paired-difference confidence bounds"),
    ("bootstrap-percentile-paired-difference", "Descriptive paired-difference bounds"),
])
def test_cli_and_public_report_show_existing_bounds_without_changing_gate(client, aq, tmp_path, method, description):
    aq.compare["scorers"][0].update(
        interval={"lower": -0.12, "upper": 0.34, "method": method, "alpha": 0.025,
                  "private": "PRIVATE_INTERVAL_PAYLOAD"},
        alpha_improvement=0.025, alpha_regression=0.05,
    )
    path = tmp_path / "bounds.md"
    code, output, _ = run(client, "--compare-to", "current", "--fail-on", "regression", "--report-file", str(path))
    assert code == 0 and "Result: pass. No regression." in output
    for text in (output, path.read_text()):
        assert description in text and "lower -0.12, upper +0.34" in text
        assert method in text and "lower one-sided alpha 0.025, upper one-sided alpha 0.05" in text
        assert "95%" not in text and "PRIVATE_INTERVAL_PAYLOAD" not in text


@pytest.mark.parametrize("interval", [None, {}, {"lower": None, "upper": None}, False, [0, 0]])
def test_missing_bounds_stay_unknown_and_preserve_legacy_verdict(interval):
    cmp = comparison()
    cmp["scorers"][0]["interval"] = interval
    text = report.pr_summary({"exit_code": 0, "compare": cmp})
    assert "lower unknown, upper unknown" in text
    assert "lower one-sided alpha unknown, upper one-sided alpha unknown" in text
    assert "Result: No clear difference." in text


def test_zero_bounds_are_observed_and_missing_upper_alpha_is_not_inferred():
    cmp = comparison()
    cmp["scorers"][0]["interval"] = {
        "lower": 0, "upper": 0, "method": "bonferroni-clopper-pearson-discordance", "alpha": 0.05,
    }
    text = "\n".join(report.compare_lines(cmp))
    assert "lower +0, upper +0" in text
    assert "lower one-sided alpha 0.05, upper one-sided alpha unknown" in text


@pytest.mark.parametrize("invalid", [True, "PRIVATE_VALUE", float("nan"), float("inf"), 10 ** 400, [0.1], {}])
def test_malformed_numeric_bounds_and_alpha_are_not_reported_as_measurements(invalid):
    cmp = comparison()
    cmp["scorers"][0].update(interval={"lower": invalid, "upper": invalid, "alpha": invalid,
                                       "method": "PRIVATE_METHOD"}, alpha_regression=invalid)
    text = "\n".join(report.compare_lines(cmp))
    assert "lower unknown, upper unknown" in text and "method unknown" in text
    assert "lower one-sided alpha unknown, upper one-sided alpha unknown" in text
    assert "PRIVATE" not in text


def test_one_missing_endpoint_does_not_discard_the_observed_endpoint():
    cmp = comparison()
    cmp["scorers"][0]["interval"] = {"lower": None, "upper": 0.00123, "method": ["PRIVATE_METHOD"]}
    text = "\n".join(report.compare_lines(cmp))
    assert "lower unknown, upper +0.00123" in text and "method unknown" in text
    assert "PRIVATE" not in text


@pytest.mark.parametrize("verdict,code", [("improvement", 0), ("regression", 1), ("no_clear_difference", 0)])
def test_bounds_rendering_never_recomputes_the_verdict_or_mutates_evidence(verdict, code):
    cmp = comparison(verdict)
    cmp["scorers"][0].update(interval={"lower": -0.9, "upper": 0.9, "alpha": 0.025,
                                       "method": "bootstrap-percentile-paired-difference"}, alpha_regression=0.05)
    original = deepcopy(cmp)
    text = report.pr_summary({"exit_code": code, "experiment": experiment("candidate", "Candidate"), "compare": cmp})
    assert report.VERDICT_WORDS[verdict] in text
    assert report.outcome(experiment("candidate", "Candidate"), cmp, None, compare_wanted=True,
                          fail_on="regression")[0] == code
    assert cmp == original


def test_inverted_bounds_and_invalid_alpha_are_unknown():
    cmp = comparison()
    cmp["scorers"][0].update(interval={"lower": 0.5, "upper": -0.5, "alpha": 0,
                                       "method": "bonferroni-clopper-pearson-discordance"}, alpha_regression=1)
    text = "\n".join(report.compare_lines(cmp))
    assert "lower unknown, upper unknown" in text
    assert "lower one-sided alpha unknown, upper one-sided alpha unknown" in text


def test_uncompared_scorer_has_no_bounds_claim():
    cmp = comparison()
    cmp["scorers"][0].update(status="waiting_for_traces", interval={"lower": 0.2, "upper": 0.5})
    text = "\n".join(report.compare_lines(cmp))
    assert "not compared" in text and "bounds" not in text

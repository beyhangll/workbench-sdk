"""Trusted development condition through the product Action, without workflow execution."""

from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest

from aq_sdk import report
from test_action import (
    action,
    baseline_client,
    CheckAPI,
    configured,
    environment,
    payload,
)


def development_payload(code=0):
    result = payload(code)
    exp = result["experiment"]
    exp["dataset_version_id"] = "fictional-frozen-version"
    result["compare"]["base"]["dataset_version_id"] = "fictional-frozen-version"
    failed, unknown = int(code == 1), int(code == 3)
    result.update(
        development_must_pass=True,
        development_check={
            "kind": "development_must_pass",
            "release_acceptance": False,
            "experiment_id": exp["id"],
            **{
                k: exp[k]
                for k in (
                    "dataset_id",
                    "dataset_version_id",
                    "dataset_version",
                    "repeats",
                )
            },
            "dataset_rows": 3,
            "row_scope": "development",
            "status": "failed" if failed else "unknown" if unknown else "passed",
            "cases": 3,
            "failures": failed,
            "unresolved": unknown,
            "failed_rows": [2] if failed else [],
            "unknown_rows": [3] if unknown else [],
            "bound": None if unknown else 0.9,
            "reason": None,
            "scorers": [
                {
                    "scorer_id": exp["scorers"][0]["id"],
                    "cases": 3,
                    "failures": failed,
                    "unresolved": unknown,
                    "bound": None if unknown else 0.9,
                }
            ],
        },
    )
    return result


def test_only_trusted_explicit_setting_enables_development(tmp_path):
    env, _ = environment(tmp_path)
    assert not action.settings(env).development_must_pass
    env["AQ_CI_DEVELOPMENT_MUST_PASS"] = "true"
    assert action.settings(env).development_must_pass
    env["AQ_CI_TASK"] = "d0000000-0000-0000-0000-000000000001"
    with pytest.raises(action.Refused):
        action.settings(env)
    env.pop("AQ_CI_TASK")
    env["AQ_CI_DEVELOPMENT_MUST_PASS"] = "yes"
    with pytest.raises(action.Refused):
        action.settings(env)


@pytest.mark.parametrize("code", [0, 1, 3])
def test_development_result_survives_public_projection_and_check_publication(
    tmp_path, code
):
    settings = replace(configured(tmp_path), development_must_pass=True)
    result = development_payload(code)
    action.validate_result(result, code, settings)
    result["experiment"]["name"] = result["experiment"]["scorers"][0]["name"] = (
        "PRIVATE_PROTECTED_LABEL"
    )
    result["compare"]["base"]["name"] = "PRIVATE_PROTECTED_LABEL"
    result["reason"] = "PRIVATE_PROTECTED_LABEL"
    result["development_check"]["private"] = "PRIVATE_PROTECTED_LABEL"
    result["development_check"]["scorers"][0]["private"] = "PRIVATE_PROTECTED_LABEL"
    aggregate = action.aggregate_result(result)
    action.validate_result(aggregate, code, settings)
    assert "PRIVATE_PROTECTED_LABEL" not in json.dumps(aggregate)
    assert aggregate["development_check"]["release_acceptance"] is False
    body = action.public_body(aggregate, settings)
    if code == 1:
        assert "Development check failed: 1 of 3 must-pass cases failed." in body
        assert "Failed must-pass rows: 2." in body
    elif code == 3:
        assert "Unknown must-pass rows: 3." in body
    assert "Not release acceptance" in body
    github = CheckAPI()
    published = action.publish_check(
        aggregate, settings, transport=httpx.MockTransport(github)
    )
    assert published["status"] == "created"
    assert published["conclusion"] == ("neutral" if code == 0 else "failure")


@pytest.mark.parametrize("mode", ["unset", "different_run", "forged_exit", "missing"])
def test_unconfigured_or_conflicting_development_report_refuses(tmp_path, mode):
    s = replace(configured(tmp_path), development_must_pass=True)
    result = development_payload(0)
    if mode == "unset":
        s = replace(s, development_must_pass=False)
    elif mode == "different_run":
        result["development_check"]["experiment_id"] = "other"
    elif mode == "forged_exit":
        result["development_check"]["failures"] = 1
    else:
        result.pop("development_check")
    with pytest.raises(action.Refused):
        action.validate_result(result, 0, s)


def test_execute_passes_the_flag_without_repository_credentials(tmp_path, monkeypatch):
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(
            returncode=1, stdout=json.dumps(development_payload(1)), stderr=""
        )

    monkeypatch.setattr(action.subprocess, "run", run)
    s = replace(configured(tmp_path), development_must_pass=True)
    assert action.execute(s, tmp_path)[0] == 1
    assert "--development-must-pass" in seen[0][0]
    assert "AQ_CI_GITHUB_TOKEN" not in seen[0][1]["env"]


def test_missing_frozen_membership_refuses_in_preflight_without_dispatch(tmp_path):
    s, github = replace(configured(tmp_path), development_must_pass=True), CheckAPI()
    fake, client = baseline_client()
    try:
        with pytest.raises(action.Refused, match="frozen development"):
            action.preflight(s, client=client, transport=httpx.MockTransport(github))
    finally:
        client.close()
    assert not any(
        method == "POST" and path == "/v1/experiments" for method, path, _ in fake.calls
    )
    assert not github.writes


def test_development_failure_is_not_a_signed_task_verdict():
    result = development_payload(1)
    assert (
        report.task_outcome(result["experiment"], result["development_check"])[0]
        == report.UNJUDGED
    )


def test_verified_frozen_membership_allows_preflight_without_dispatch(tmp_path):
    s, github = replace(configured(tmp_path), development_must_pass=True), CheckAPI()
    fake, client = baseline_client()
    fake.base["dataset_version_id"] = "fictional-frozen-version"
    observed = development_payload()["development_check"]
    observed["experiment_id"] = fake.base["id"]
    fake.base["development_check"] = observed
    try:
        assert action.preflight(s, client=client, transport=httpx.MockTransport(github)) == fake.base
    finally:
        client.close()
    assert not any(method == "POST" and path == "/v1/experiments" for method, path, _ in fake.calls)


def test_projected_labels_preserve_frozen_baseline_checks(tmp_path):
    from test_action import baseline
    s = replace(configured(tmp_path), development_must_pass=True)
    result = development_payload(1)
    projected = action.aggregate_result(result)
    action.validate_result(projected, 1, s, baseline(), public_projection=True)
    projected["compare"]["base"]["scorers"][0]["id"] = "other-scorer"
    with pytest.raises(action.Refused):
        action.validate_result(projected, 1, s, baseline(), public_projection=True)

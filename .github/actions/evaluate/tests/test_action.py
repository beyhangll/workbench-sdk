"""Offline Action contracts. No cloud calls, model calls or real PR comments."""
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


action = load("evaluation_action", ROOT / ".github/actions/evaluate/run.py")
installer = load("evaluation_install", ROOT / ".github/actions/evaluate/install.py")
fixtures = load("sdk_fixtures", ROOT / "packages/aq-sdk/tests/conftest.py")
from aq_sdk import Client, cli, publisher, report
from aq_sdk.config import Config

REPO, PR, SHA = "owner/repository", 42, "a" * 40
API_KEY, COMMENT_TOKEN = "aq_live_test_key", "github_sentinel_token"
BASE_SHA, BASE_ENDPOINT, APP_ID = "b" * 40, "base_build", 321


def environment(tmp_path):
    event = {"pull_request": {"number": PR, "head": {"sha": SHA, "repo": {"full_name": REPO, "fork": False}},
                              "base": {"sha": BASE_SHA, "repo": {"full_name": REPO, "fork": False}}}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": REPO,
           "GITHUB_EVENT_PATH": str(event_path), "AQ_CI_COMMIT": SHA, "AQ_CI_AGENT_ENDPOINT": "pr_42",
           "AQ_CI_TARGET": "my-agent", "AQ_CI_DATASET": "weather", "AQ_CI_BUDGET": "1.25",
           "AQ_ENDPOINT": "https://evaluation.test", "AQ_API_KEY": API_KEY, "AQ_CI_GITHUB_TOKEN": COMMENT_TOKEN,
           "AQ_CI_PUBLISHER_ID": "123", "AQ_CI_CHECK_KEY": "agent.dataset", "AQ_CI_CHECK_APP_ID": str(APP_ID),
           "AQ_CI_BASELINE_EXPERIMENT": fixtures.BASE, "AQ_CI_BASE_COMMIT": BASE_SHA,
           "AQ_CI_BASE_ENDPOINT": BASE_ENDPOINT, "RUNNER_TEMP": str(tmp_path),
           "GITHUB_OUTPUT": str(tmp_path / "outputs")}
    return env, event


def baseline(row_scope="development"):
    base = fixtures.experiment(fixtures.BASE, "Reviewed base")
    base["target"] = {"kind": "agentcore", "agent_id": fixtures.AGENT, "commit_sha": BASE_SHA,
                      "agent_endpoint": BASE_ENDPOINT, "effective_agent_endpoint": BASE_ENDPOINT,
                      "endpoint_overridden": True}
    base["progress"].update(row_scope=row_scope, include_rejected=False)
    base["scorers"][0].update(config={}, threshold=None, uses_model=False, needs_trace=False)
    return base


def baseline_for(s):
    return baseline("held_out" if s.task else "development")


_real_run = action.run


def run(s, output, **kwargs):
    # Existing report/privacy unit cases use a frozen synthetic preflight; the
    # real read-only preflight and publisher are exercised in the dedicated tests.
    kwargs.setdefault("verify", lambda _: baseline_for(s))
    kwargs.setdefault("confirm", lambda _: None)
    kwargs.setdefault("check_publish", lambda result, s, **kw: {"status": "reconciled" if kw.get("reconcile_only") else "created", "check_run_id": 99, "conclusion": "failure" if result is None or result["exit_code"] else "neutral"})
    return _real_run(s, output, **kwargs)


def payload(code=0, verdict="no_clear_difference"):
    exp = fixtures.experiment("candidate", "Candidate")
    exp["target"] = {"kind": "agentcore", "agent_id": fixtures.AGENT, "commit_sha": SHA, "agent_endpoint": "pr_42", "effective_agent_endpoint": "pr_42", "endpoint_overridden": True,
                     "system_prompt": "PRIVATE_SYSTEM_PROMPT"}
    exp["progress"]["planning"] = {"text": "With three rows, differences remain uncertain."}
    cmp = fixtures.comparison(verdict)
    cmp["base"] = baseline()
    cmp["candidate"] = exp
    exp["progress"].update(row_scope="development", include_rejected=False)
    exp["scorers"] = json.loads(json.dumps(cmp["base"]["scorers"]))
    cmp["rows"] = [{"input": "PRIVATE_INPUT", "output": "PRIVATE_ANSWER"}]
    return {"exit_code": code, "experiment": exp, "compare": cmp, "verdict": verdict, "reason": None,
            "link": "https://evaluation.test/#/comparison"}


def configured(tmp_path):
    env, _ = environment(tmp_path)
    return action.settings(env)


TASK = "d0000000-0000-0000-0000-000000000001"


def task_payload(code=0, status="meets"):
    result = payload(code)
    exp = result["experiment"]
    result["task"] = {"id": TASK, "name": "Answer questions", "agent_id": exp["agent_id"],
                      "dataset_id": exp["dataset_id"], "private": "PRIVATE_TASK_PAYLOAD"}
    result["task_verdict"] = {"status": status, "bar_version": 2, "experiment_id": exp["id"],
        "thresholds": [{"metric": "must_pass", "value": 0, "scorer_id": fixtures.SC1, "status": status,
                        "must_pass_status": status, "paired_quality_status": "meets", "failures": int(code == 1),
                        "cases": 30, "bound": 0.143234 if code == 1 else 0.08, "unresolved": 0,
                        "private": "PRIVATE_CASE_PAYLOAD"}], "requirements": []}
    for side in (exp, result["compare"]["base"], result["compare"]["candidate"]):
        side["progress"]["row_scope"] = "held_out"
    return result


@pytest.mark.parametrize("code,status", [(0, "meets"), (1, "below"), (3, "unknown")])
def test_task_gate_survives_validation_and_safe_aggregation(tmp_path, code, status):
    s, result = configured(tmp_path), task_payload(code, status)
    s.task = TASK
    action.validate_result(result, code, s, baseline_for(s))
    aggregate = action.aggregate_result(result)
    action.validate_result(aggregate, code, s)
    text = action.public_body(aggregate, s)
    assert "30 cases, failure rate up to" in text
    assert "PRIVATE_" not in json.dumps(aggregate) and "PRIVATE_" not in text
    assert aggregate["task_verdict"]["thresholds"][0]["bound"] == result["task_verdict"]["thresholds"][0]["bound"]


@pytest.mark.parametrize("change", ["task", "agent", "dataset", "candidate", "missing", "tampered_exit"])
def test_substituted_or_incomplete_task_gate_refuses_before_publication(tmp_path, change):
    s, result = configured(tmp_path), task_payload()
    s.task = TASK
    if change == "task":
        result["task"]["id"] = "another-task"
    if change == "agent":
        result["task"]["agent_id"] = "another-agent"
    if change == "dataset":
        result["task"]["dataset_id"] = "another-dataset"
    if change == "candidate":
        result["task_verdict"]["experiment_id"] = "another-experiment"
    if change == "missing":
        result.pop("task_verdict")
    if change == "tampered_exit":
        result["task_verdict"]["status"] = "below"
    with pytest.raises(action.Refused):
        action.validate_result(result, 0, s, baseline_for(s))


def test_unconfigured_task_cannot_replace_the_legacy_regression_gate(tmp_path):
    with pytest.raises(action.Refused):
        action.validate_result(task_payload(), 0, configured(tmp_path), baseline())


@pytest.mark.parametrize("status,code", [("below", 1), ("unknown", 3)])
def test_required_slice_failure_counts_and_bounds_survive_safe_aggregation(tmp_path, status, code):
    s, result = configured(tmp_path), task_payload()
    s.task = TASK
    result["exit_code"] = code
    threshold = {**result["task_verdict"]["thresholds"][0], "status": status, "cases": 10,
                 "failures": 1 if status == "below" else 0, "bound": 0.3942 if status == "below" else None}
    result["task_verdict"].update(status=status, aggregate_status="meets", slice_metadata_status="valid",
        worst_slice={"dimension": "language", "value": "tr", "status": status, "private": "PRIVATE_SLICE"},
        slices=[{"dimension": "language", "value": "tr", "cases": 10, "status": status,
                 "thresholds": [threshold], "private": "PRIVATE_SLICE"},
                {"dimension": "language", "value": "en", "cases": 20, "status": "meets", "private": "PRIVATE_SLICE"}])
    aggregate = action.aggregate_result(result)
    action.validate_result(aggregate, code, s)
    # Preserve the public task projection, including explicitly reported passing slices.
    assert len(aggregate["task_verdict"]["slices"]) == 2
    assert "PRIVATE_" not in json.dumps(aggregate)
    text = action.public_body(aggregate, s)
    assert f"Worst required slice: language=tr ({status})" in text
    assert ("1 failures in 10 cases, failure rate up to 39.4%" if code == 1
            else "0 failures in 10 cases, failure rate up to unknown") in text


def test_task_uuid_is_read_from_trusted_settings_and_passed_to_cli(tmp_path, monkeypatch):
    env, _ = environment(tmp_path)
    env["AQ_CI_TASK"] = TASK
    s = action.settings(env)
    def execute(args, **kwargs):
        assert "--task=" + TASK in args
        assert "--fail-on=regression" in args
        return SimpleNamespace(returncode=1, stdout=json.dumps(task_payload(1, "below")), stderr="")
    monkeypatch.setattr(action.subprocess, "run", execute)
    assert action.execute(s, tmp_path)[0] == 1
    env["AQ_CI_TASK"] = "current"
    with pytest.raises(action.Refused):
        action.settings(env)


@pytest.mark.parametrize("method", ["bonferroni-clopper-pearson-discordance", "bootstrap-percentile-paired-difference"])
@pytest.mark.parametrize("code,verdict", [(0, "no_clear_difference"), (1, "regression")])
def test_bounds_survive_aggregate_and_publication_without_changing_exit(tmp_path, method, code, verdict):
    s, result = configured(tmp_path), payload(code, verdict)
    sc = result["compare"]["scorers"][0]
    sc.update(interval={"lower": -0.125, "upper": 0.25, "alpha": 0.025, "method": method,
                        "private_metadata": "PRIVATE_BOUND_METADATA"}, alpha_regression=0.05)
    aggregate = action.aggregate_result(result)
    bounds = aggregate["compare"]["scorers"][0]
    assert bounds["interval"] == {"lower": -0.125, "upper": 0.25, "alpha": 0.025, "method": method}
    assert bounds["alpha_regression"] == 0.05
    assert report.pr_summary(aggregate) == report.pr_summary(result)
    comments = Comments()
    dest = tmp_path / "bounded"
    assert run(s, dest, evaluate=lambda *_: (code, result), publish=comments.publish) == code
    assert len(comments.writes) == 1
    public = "\n".join(p.read_text() for p in dest.iterdir())
    assert "PRIVATE_" not in public and API_KEY not in public and COMMENT_TOKEN not in public
    assert "lower -0.125, upper +0.25" in public
    assert "lower one-sided alpha 0.025, upper one-sided alpha 0.05" in public
    assert ("Descriptive" if method.startswith("bootstrap") else "confidence") in public


@pytest.mark.parametrize("interval", [None, "PRIVATE_INTERVAL", ["PRIVATE_INTERVAL"], {},
    {"lower": {"private": "PRIVATE_LOWER"}, "upper": "PRIVATE_UPPER", "alpha": "PRIVATE_ALPHA", "method": "PRIVATE_METHOD"},
    {"lower": True, "upper": False, "alpha": True, "method": {"private": "PRIVATE_METHOD"}},
    {"lower": float("nan"), "upper": float("inf"), "alpha": float("-inf"), "method": "unknown"},
    {"lower": 10 ** 1000, "upper": -(10 ** 1000), "alpha": 0, "method": "unknown"},
    {"lower": 0.25, "upper": -0.125, "alpha": 1, "method": "bonferroni-clopper-pearson-discordance"}])
def test_malformed_bounds_stay_unknown_and_private_values_are_excluded(interval):
    result = payload()
    sc = result["compare"]["scorers"][0]
    sc.update(interval=interval, alpha_regression={"private": "PRIVATE_REGRESSION_ALPHA"})
    aggregate = action.aggregate_result(result)
    assert report.pr_summary(aggregate) == report.pr_summary(result)
    text = json.dumps(aggregate, allow_nan=False)
    assert "PRIVATE_" not in text
    assert aggregate["compare"]["scorers"][0]["alpha_regression"] is None
    assert "lower unknown, upper unknown" in report.pr_summary(aggregate)


def test_missing_bounds_are_not_inferred_from_other_comparison_values():
    result = payload()
    sc = result["compare"]["scorers"][0]
    sc.pop("interval", None)
    sc.pop("alpha_regression", None)
    aggregate = action.aggregate_result(result)
    got = aggregate["compare"]["scorers"][0]
    assert "interval" not in got and "alpha_regression" not in got
    assert report.pr_summary(aggregate) == report.pr_summary(result)
    assert "lower unknown, upper unknown" in report.pr_summary(aggregate)


class CheckAPI:
    """Synthetic authoritative GitHub read/write responses; never a real check."""
    def __init__(self):
        self.rows, self.writes = [], []
        self.timeout_after_delivery = self.unknown_delivery = False
        self.base_sha, self.head_sha = BASE_SHA, SHA
        self.head_repository, self.base_repository = REPO, REPO
        self.head_repository_is_fork = self.base_repository_is_fork = False
        self.actual_app_id = APP_ID

    def __call__(self, req):
        assert req.headers["authorization"] == "Bearer " + COMMENT_TOKEN
        assert API_KEY not in str(req.headers) and API_KEY not in req.content.decode()
        if req.url.path.endswith('/pulls/42'):
            return httpx.Response(200, json={"number": PR,
                "head": {"sha": self.head_sha, "repo": {"full_name": self.head_repository, "fork": self.head_repository_is_fork}},
                "base": {"sha": self.base_sha, "repo": {"full_name": self.base_repository, "fork": self.base_repository_is_fork}}})
        if req.method == 'GET':
            return httpx.Response(200, json={"total_count": len(self.rows), "check_runs": self.rows})
        self.writes.append(req)
        data = json.loads(req.content)
        if req.method == 'PATCH':
            data = {**self.rows[0], **data}
        row = {"id": 99, **data, "app": {"id": self.actual_app_id}}
        row['output'] = {**data['output'], 'annotations_count': 0, 'annotations_url': 'https://api.github.com/synthetic'}
        if not self.unknown_delivery:
            self.rows = [row]
        if self.timeout_after_delivery or self.unknown_delivery:
            raise httpx.ReadTimeout('Synthetic uncertain write', request=req)
        return httpx.Response(201 if req.method == 'POST' else 200, json=row)


class ActionFake(fixtures.FakeAQ):
    def __init__(self):
        super().__init__()
        self.result['scorers'] = baseline()['scorers']
        self.current_scorers = baseline()['scorers']
        self.preflight_response = None

    def __call__(self, req):
        if (req.method == 'POST' and req.url.path == '/v1/experiments/estimate'
                and json.loads(req.content).get('scorers_only') is True):
            assert req.headers['authorization'] == 'Bearer ' + API_KEY
            self.calls.append((req.method, req.url.path, self.body(req)))
            return httpx.Response(200, json=self.preflight_response if self.preflight_response is not None
                                  else {'scorers': self.current_scorers, 'calls': 0, 'cost_status': 'unknown',
                                        'estimated_cost_usd': None, 'target': None})
        response = super().__call__(req)
        if isinstance(self.result.get("target"), dict) and self.result["target"].get("agent_endpoint"):
            self.result["target"]["effective_agent_endpoint"] = self.result["target"]["agent_endpoint"]
            if response.status_code in (200, 201) and isinstance(response.json(), dict) and "target" in response.json():
                data = response.json()
                data["target"] = dict(self.result["target"])
                return httpx.Response(response.status_code, json=data)
        return response


def baseline_client(row_scope="development"):
    fake = ActionFake()
    fake.base = baseline(row_scope)
    fake.agents[0]['non_production'] = True
    return fake, Client(Config(None, API_KEY, 'https://evaluation.test', 'https://evaluation.test'),
                        transport=httpx.MockTransport(fake), sleep=lambda _: None)


@pytest.mark.parametrize('default', [False, True])
def test_real_read_only_preflight_accepts_protected_binding_not_current_role(tmp_path, default):
    s = configured(tmp_path)
    github = CheckAPI()
    fake, client = baseline_client()
    fake.base['role'] = 'candidate'  # A mutable current label is irrelevant.
    if default:
        fake.base['target'].update(agent_endpoint=None, endpoint_overridden=False)
    try:
        assert action.preflight(s, client=client, transport=httpx.MockTransport(github)) == fake.base
    finally:
        client.close()
    assert all(method == 'GET' or (path == '/v1/experiments/estimate' and body.get('scorers_only') is True)
               for method, path, body in fake.calls)
    assert sum(method == 'POST' for method, _, _ in fake.calls) == 1
    assert not github.writes


def model_scoring(fake, provider='bedrock'):
    frozen = fake.base['scorers'][0]
    frozen.update(kind='llm_judge', config={'model_id': 'synthetic-judge', 'region': 'eu-central-1'},
                  threshold=0.7, uses_model=True, needs_trace=False)
    if provider == 'bedrock':
        frozen.update(judge_provider={'kind': 'bedrock'}, judge_connection_id=fixtures.AGENT)
        current_provider = {'kind': 'bedrock', 'region': 'eu-central-1'}
    else:
        current_provider = {'kind': provider, 'region': 'eu', 'base_url': 'https://judge.test/v1',
                            'api_version': None, 'zero_data_retention': True}
        frozen['judge_provider'] = {**current_provider, 'credentials_secret': 'PRIVATE_SECRET_REFERENCE',
                                    'prices': {}, 'models': [{'id': 'synthetic-judge', 'name': 'Synthetic'}]}
    fake.current_scorers = [{**json.loads(json.dumps(frozen)), 'judge_provider': current_provider}]


@pytest.mark.parametrize('provider', ['bedrock', 'azure_openai', 'eu_gateway', 'openai_compatible'])
def test_preflight_matches_effective_model_controls_without_credential_identity(tmp_path, provider):
    s, github = configured(tmp_path), CheckAPI()
    fake, client = baseline_client()
    model_scoring(fake, provider)
    try:
        assert action.preflight(s, client=client, transport=httpx.MockTransport(github)) == fake.base
    finally:
        client.close()
    body = fake.sent('POST', '/v1/experiments/estimate')
    assert body['scorers_only'] is True and body['agent_id'] == fixtures.AGENT
    assert body['target']['kind'] == 'agentcore' and body['target']['agent_endpoint'] == s.agent_endpoint
    assert body['scorer_ids'] == [fixtures.SC1]
    assert body['dataset_version'] == fake.base['dataset_version'] and body['repeats'] == fake.base['repeats']
    assert not any(path == '/v1/experiments' and method == 'POST' for method, path, _ in fake.calls)
    assert not github.writes


@pytest.mark.parametrize('mutation', ['config', 'threshold', 'threshold_type', 'config_type', 'model', 'trace', 'uses_model', 'region',
    'provider', 'connection', 'missing_connection', 'missing_provider', 'missing_region', 'missing_config', 'duplicate', 'substituted_id',
    'missing_response', 'old_api', 'malformed_response', 'permission_denied'])
def test_current_scoring_drift_refuses_before_evaluator_dispatch(tmp_path, mutation):
    s, github, comments = configured(tmp_path), CheckAPI(), Comments()
    fake, client = baseline_client()
    model_scoring(fake)
    sc = fake.current_scorers[0]
    if mutation == 'config':
        sc['config']['include_tool_spans'] = True
    if mutation == 'threshold':
        sc['threshold'] = 0.9
    if mutation == 'threshold_type':
        fake.base['scorers'][0]['threshold'] = 1
        sc['threshold'] = True
    if mutation == 'config_type':
        fake.base['scorers'][0]['config']['include_tool_spans'] = 1
        sc['config']['include_tool_spans'] = True
    if mutation == 'model':
        sc['config']['model_id'] = 'other-judge'
    if mutation == 'trace':
        sc['needs_trace'] = True
    if mutation == 'uses_model':
        sc['uses_model'] = False
    if mutation == 'region':
        sc['judge_provider']['region'] = 'us-east-1'
    if mutation == 'provider':
        sc['judge_provider'] = {'kind': 'unsupported'}
    if mutation == 'connection':
        sc['judge_connection_id'] = fixtures.DS
    if mutation == 'missing_connection':
        sc.pop('judge_connection_id')
    if mutation == 'missing_provider':
        sc.pop('judge_provider')
    if mutation == 'missing_region':
        sc['judge_provider'].pop('region')
    if mutation == 'missing_config':
        sc.pop('config')
    if mutation == 'duplicate':
        fake.current_scorers *= 2
    if mutation == 'substituted_id':
        sc['id'] = fixtures.SC2
    if mutation == 'missing_response':
        fake.preflight_response = {}
    if mutation == 'old_api':
        fake.preflight_response = {'target': {'kind': 'agentcore'}, 'calls': 3}
    if mutation == 'malformed_response':
        fake.preflight_response = {'scorers': 'PRIVATE_BAD_RECORD'}
    dispatches, checks = [], []
    def transport(req):
        if mutation == 'permission_denied' and req.method == 'POST':
            return httpx.Response(403, json={'detail': {'message': 'PRIVATE_DENIED ' + API_KEY}})
        return fake(req)
    client.close()
    client = Client(Config(None, API_KEY, 'https://evaluation.test', 'https://evaluation.test'),
                    transport=httpx.MockTransport(transport))
    def evaluate(*args):
        dispatches.append(True)
        return 0, payload()
    def check(*args, **kwargs):
        checks.append(args[0])
        return {'status': 'created', 'check_run_id': 99, 'conclusion': 'failure'}
    try:
        with pytest.raises(action.Refused) as exc:
            _real_run(s, tmp_path/'preflight-drift',
                verify=lambda settings: action.preflight(settings, client=client, transport=httpx.MockTransport(github)),
                evaluate=evaluate, publish=comments.publish, check_publish=check)
    finally:
        client.close()
    assert not dispatches and not comments.writes and checks == [None]
    assert 'PRIVATE_' not in str(exc.value) and API_KEY not in str(exc.value)
    assert not any(method == 'POST' and path == '/v1/experiments' for method, path, _ in fake.calls)


def test_historical_default_provider_is_configured_routing_not_guessed_connection(tmp_path):
    s, github = configured(tmp_path), CheckAPI()
    fake, client = baseline_client()
    model_scoring(fake)
    fake.base['scorers'][0].pop('judge_provider')
    fake.base['scorers'][0]['config'].pop('region')
    fake.current_scorers[0]['config'].pop('region')
    try:
        assert action.preflight(s, client=client, transport=httpx.MockTransport(github)) == fake.base
        fake.base['scorers'][0].pop('judge_connection_id')
        fake.calls.clear()
        with pytest.raises(action.Refused):
            action.preflight(s, client=client, transport=httpx.MockTransport(github))
    finally:
        client.close()
    assert all(method == 'GET' for method, _, _ in fake.calls)


def test_read_only_reconciliation_does_not_compare_today_scorer_settings(tmp_path):
    s, github = configured(tmp_path), CheckAPI()
    s.reconcile_report = 'retained-aggregate.json'
    fake, client = baseline_client()
    fake.current_scorers = []
    try:
        assert action.preflight(s, client=client, transport=httpx.MockTransport(github)) == fake.base
    finally:
        client.close()
    assert all(method == 'GET' for method, _, _ in fake.calls)


@pytest.mark.parametrize('field', ['config', 'threshold', 'judge_provider', 'judge_connection_id'])
def test_scoring_changes_after_successful_preflight_still_refuse_publication(tmp_path, field):
    s, github, comments = configured(tmp_path), CheckAPI(), Comments()
    fake, client = baseline_client()
    model_scoring(fake)
    result = payload()
    result['compare']['base'] = json.loads(json.dumps(fake.base))
    result['experiment']['scorers'] = json.loads(json.dumps(fake.base['scorers']))
    sc = result['experiment']['scorers'][0]
    if field == 'config':
        sc[field]['model_id'] = 'changed-after-preflight'
    if field == 'threshold':
        sc[field] = 0.9
    if field == 'judge_provider':
        sc[field] = {'kind': 'bedrock', 'region': 'us-east-1'}
    if field == 'judge_connection_id':
        sc[field] = fixtures.DS
    dispatches, checks = [], []
    def evaluate(*args):
        dispatches.append(True)
        return 0, result
    def check(*args, **kwargs):
        checks.append(args[0])
        return {'status': 'created', 'check_run_id': 99, 'conclusion': 'failure'}
    try:
        with pytest.raises(action.Refused):
            _real_run(s, tmp_path/'later-drift',
                verify=lambda settings: action.preflight(settings, client=client, transport=httpx.MockTransport(github)),
                evaluate=evaluate, publish=comments.publish, check_publish=check)
    finally:
        client.close()
    assert dispatches == [True] and checks == [None] and not comments.writes


def test_judge_credential_references_and_catalog_prices_are_not_comparison_controls(tmp_path):
    s, github = configured(tmp_path), CheckAPI()
    fake, client = baseline_client()
    model_scoring(fake, 'openai_compatible')
    result = payload()
    result['compare']['base'] = json.loads(json.dumps(fake.base))
    result['experiment']['scorers'] = json.loads(json.dumps(fake.base['scorers']))
    provider = result['experiment']['scorers'][0]['judge_provider']
    provider.update(credentials_secret='OTHER_PRIVATE_REFERENCE', prices={'synthetic': 1}, models=[])
    try:
        captured = action.preflight(s, client=client, transport=httpx.MockTransport(github))
        action.validate_result(result, 0, s, captured)
    finally:
        client.close()
    public = json.dumps(action.aggregate_result(result))
    assert 'PRIVATE_' not in public and 'judge_provider' not in public and 'credentials_secret' not in public


@pytest.mark.parametrize('mutation', ['head_drift', 'base_drift', 'wrong_agent', 'wrong_dataset', 'wrong_commit',
    'wrong_effective_endpoint', 'missing_effective_endpoint', 'inconsistent_override', 'not_production_isolation',
    'missing_scope', 'all_scope', 'held_out_scope', 'unfinished_rows', 'missing_ok', 'pending', 'duplicate_scorer',
    'missing_scorer', 'wrong_plan_total', 'duplicate_attempts', 'pinned_rows', 'legacy_rejected_policy', 'include_rejected_policy'])
def test_preflight_refuses_missing_or_mismatched_evidence_with_zero_dispatch(tmp_path, mutation):
    s, github = configured(tmp_path), CheckAPI()
    fake, client = baseline_client()
    base = fake.base
    if mutation == 'head_drift':
        github.head_sha = 'c' * 40
    if mutation == 'base_drift':
        github.base_sha = 'c' * 40
    if mutation == 'wrong_agent':
        base['agent_id'] = fixtures.DS
    if mutation == 'wrong_dataset':
        base['dataset_id'] = fixtures.AGENT
    if mutation == 'wrong_commit':
        base['target']['commit_sha'] = SHA
    if mutation == 'wrong_effective_endpoint':
        base['target']['effective_agent_endpoint'] = 'another'
    if mutation == 'missing_effective_endpoint':
        base['target'].pop('effective_agent_endpoint')
    if mutation == 'inconsistent_override':
        base['target']['endpoint_overridden'] = False
    if mutation == 'not_production_isolation':
        fake.agents[0]['non_production'] = False
    if mutation == 'missing_scope':
        base['progress'].pop('row_scope')
    if mutation == 'all_scope':
        base['progress']['row_scope'] = 'all'
    if mutation == 'held_out_scope':
        base['progress']['row_scope'] = 'held_out'
    if mutation == 'unfinished_rows':
        base['summary'].update(attempts=1, ok=1, not_run=2, rows_not_run=2)
        base['summary']['scorers'][0].update(scored=1, rows_scored=1)
    if mutation == 'missing_ok':
        base['summary'].pop('ok')
    if mutation == 'pending':
        base['summary']['scorers'][0]['pending'] = 1
    if mutation == 'duplicate_scorer':
        base['summary']['scorers'] *= 2
    if mutation == 'missing_scorer':
        base['summary']['scorers'] = []
    if mutation == 'wrong_plan_total':
        base['progress']['total'] = 1
    if mutation == 'duplicate_attempts':
        base['summary']['scorers'][0]['rows_scored'] = 1
    if mutation == 'pinned_rows':
        base['progress']['row_ids'] = [fixtures.ROWS[0]['id']]
    if mutation == 'legacy_rejected_policy':
        base['progress'].pop('include_rejected')
    if mutation == 'include_rejected_policy':
        base['progress']['include_rejected'] = True
    try:
        with pytest.raises(action.Refused):
            action.preflight(s, client=client, transport=httpx.MockTransport(github))
    finally:
        client.close()
    assert not any(method == 'POST' for method, _, _ in fake.calls) and not github.writes


@pytest.mark.parametrize('selector', ['current', 'name', '', 'not-a-uuid'])
def test_settings_require_immutable_baseline_id_before_network(tmp_path, selector):
    env, _ = environment(tmp_path)
    env['AQ_CI_BASELINE_EXPERIMENT'] = selector
    with pytest.raises(action.Refused): action.settings(env)


@pytest.mark.parametrize('code,verdict,expected', [(0, 'improvement', 'success'),
    (0, 'no_clear_difference', 'neutral'), (1, 'regression', 'failure'), (3, 'no_clear_difference', 'failure')])
def test_owned_terminal_checks_keep_incomplete_nonpassing(tmp_path, code, verdict, expected):
    s, result, github = configured(tmp_path), payload(code, verdict), CheckAPI()
    if code == 3:
        result['experiment']['status'] = 'failed'
    out = action.publish_check(action.aggregate_result(result), s, transport=httpx.MockTransport(github))
    assert out['status'] == 'created' and out['conclusion'] == expected and out['check_run_id'] == 99
    assert len(github.writes) == (2 if code == 0 else 1)
    if code == 0:
        assert json.loads(github.writes[0].content)['conclusion'] == 'failure'
    text = '\n'.join(req.content.decode() for req in github.writes)
    assert 'PRIVATE_' not in text and API_KEY not in text and COMMENT_TOKEN not in text
    again = action.publish_check(action.aggregate_result(result), s, transport=httpx.MockTransport(github))
    assert again['status'] == 'unchanged' and len(github.writes) == (2 if code == 0 else 1)


@pytest.mark.parametrize('delivered', [False, True])
def test_check_uncertain_write_reconciles_only_with_reads(tmp_path, delivered):
    s, result, github = configured(tmp_path), payload(), CheckAPI()
    github.timeout_after_delivery = delivered
    github.unknown_delivery = not delivered
    transport = httpx.MockTransport(github)
    out = action.publish_check(action.aggregate_result(result), s, transport=transport)
    assert out['status'] == ('reconciled' if delivered else 'uncertain')
    if not delivered:
        assert out['conclusion'] is None
    assert len(github.writes) == (2 if delivered else 1)
    follow = action.publish_check(action.aggregate_result(result), s, reconcile_only=True, transport=transport)
    assert follow['status'] == ('reconciled' if delivered else 'uncertain') and len(github.writes) == (2 if delivered else 1)


def test_missing_report_can_only_publish_fixed_failure_check(tmp_path):
    github, s = CheckAPI(), configured(tmp_path)
    out = action.publish_check(None, s, transport=httpx.MockTransport(github))
    assert out['status'] == 'created' and out['conclusion'] == 'failure'
    assert 'Inspect the retained private records' in github.rows[0]['output']['summary']


def test_check_and_comment_publication_remain_separate_from_original_exit(tmp_path):
    s, result = configured(tmp_path), payload()
    out = tmp_path/'unconfirmed-check'
    code = run(s, out, evaluate=lambda *_: (0, result), publish=Comments().publish,
               check_publish=lambda *args, **kw: {'status': 'uncertain', 'check_run_id': None, 'conclusion': None})
    assert code == 4
    assert json.loads((out/'publication.json').read_text())['evaluation_exit_code'] == 0


@pytest.mark.parametrize('mutation', ['unfinished', 'retained_base_unfinished', 'copied_candidate_extra_rows', 'wrong_candidate', 'substituted_base', 'config_drift', 'scope_drift', 'effective_route', 'unknown_verdict', 'null_verdict', 'missing_verdict', 'malformed_verdict', 'conflicting_decision', 'missing_matched', 'null_matched', 'bool_matched', 'partial_rows', 'missing_required_scorer', 'scorer_config_drift', 'judge_connection_drift'])
def test_invalid_retained_measurement_never_publishes_passing_check(tmp_path, mutation):
    s, result = configured(tmp_path), payload()
    if mutation == 'unfinished':
        result['experiment']['summary'].update(attempts=1, ok=1, not_run=2)
        result['experiment']['summary']['scorers'][0]['scored'] = 1
    if mutation == 'retained_base_unfinished':
        result['compare']['base']['summary'].update(attempts=1, ok=1, not_run=2, rows_not_run=2)
        result['compare']['base']['summary']['scorers'][0].update(scored=1, rows_scored=1)
    if mutation == 'copied_candidate_extra_rows':
        candidate = json.loads(json.dumps(result['experiment']))
        candidate['summary'].update(rows=4, attempts=4, ok=4)
        candidate['summary']['scorers'][0].update(scored=4, rows_scored=4)
        candidate['progress'].update(total=4, finished=4)
        result['compare']['candidate'] = candidate
    if mutation == 'wrong_candidate':
        result['compare']['candidate'] = {**result['experiment'], 'id': fixtures.BASE}
    if mutation == 'substituted_base':
        result['compare']['base']['id'] = fixtures.AGENT
    if mutation == 'config_drift':
        result['compare']['base']['target']['system_prompt'] = 'Changed private configuration'
    if mutation == 'scope_drift':
        result['experiment']['progress']['row_scope'] = 'all'
    if mutation == 'effective_route':
        result['experiment']['target']['effective_agent_endpoint'] = 'another'
    if mutation == 'unknown_verdict':
        result['compare']['verdict'] = result['verdict'] = 'unrecognized'
    if mutation == 'null_verdict':
        result['compare']['verdict'] = result['verdict'] = None
    if mutation == 'missing_verdict':
        result['compare'].pop('verdict')
        result.pop('verdict')
    if mutation == 'malformed_verdict':
        result['compare']['verdict'] = result['verdict'] = {'private': 'PRIVATE_VERDICT'}
    if mutation == 'conflicting_decision':
        result['compare']['decision'] = 'adopt_candidate'
    if mutation == 'missing_matched':
        result['compare'].pop('matched_rows')
    if mutation == 'null_matched':
        result['compare']['matched_rows'] = None
    if mutation == 'bool_matched':
        result['compare']['matched_rows'] = True
    if mutation == 'partial_rows':
        result['compare']['matched_rows'] = 2
    if mutation == 'missing_required_scorer':
        expected = baseline()
        expected['scorers'].append({'id': fixtures.SC2, 'name': 'Required second scorer'})
        expected['summary']['scorers'].append({**expected['summary']['scorers'][0], 'scorer_id': fixtures.SC2})
        result['compare']['base'] = expected
    if mutation == 'scorer_config_drift':
        result['experiment']['scorers'][0]['threshold'] = 0.9
    if mutation == 'judge_connection_drift':
        result['experiment']['scorers'][0]['judge_connection_id'] = 'c0000000-0000-0000-0000-000000000010'
        result['compare']['candidate']['scorers'] = result['experiment']['scorers']
    conclusions, comments = [], Comments()
    def check(result, *args, **kwargs):
        conclusions.append(result)
        return {'status': 'created', 'check_run_id': 99, 'conclusion': 'failure'}
    with pytest.raises(action.Refused):
        run(s, tmp_path/'invalid', evaluate=lambda *_: (0, result), publish=comments.publish, check_publish=check,
            verify=(lambda _: expected) if mutation == 'missing_required_scorer' else (lambda _: baseline()))
    assert conclusions == [None] and not comments.writes


def test_rejected_row_policy_difference_cannot_be_hidden_by_overlap(tmp_path):
    expected = baseline()
    expected['summary'].update(rows=4, attempts=4, ok=4)
    expected['summary']['scorers'][0].update(scored=4, rows_scored=4)
    expected['progress'].update(total=4, finished=4, include_rejected=True)
    result = payload()
    result['compare']['base'] = expected
    result['experiment']['progress']['include_rejected'] = False
    assert result['compare']['matched_rows'] == 3
    with pytest.raises(action.Refused):
        run(configured(tmp_path), tmp_path/'subset', evaluate=lambda *_: (0,result), verify=lambda _: expected)


def test_post_execution_pr_drift_refuses_before_comment_publication(tmp_path):
    comments, checks = Comments(), []
    def drift(_): raise action.Refused('Synthetic PR base drift')
    with pytest.raises(action.Refused):
        run(configured(tmp_path), tmp_path/'drift', evaluate=lambda *_: (0, payload()), publish=comments.publish,
            confirm=drift, check_publish=lambda result, *args, **kwargs: checks.append(result) or
            {'status': 'failed', 'check_run_id': None, 'conclusion': None})
    assert not comments.writes and checks == [None]


def test_full_mocked_preflight_execution_comment_and_check_path(tmp_path):
    s, github, comments = configured(tmp_path), CheckAPI(), Comments()
    fake, client = baseline_client()
    result = payload(1, 'regression')
    ordered = []
    def verify(settings):
        base = action.preflight(settings, client=client, transport=httpx.MockTransport(github))
        ordered.append('preflight')
        return base
    def evaluate(*args):
        assert ordered == ['preflight']
        ordered.append('one-dispatch')
        return 1, result
    try:
        code = _real_run(s, tmp_path/'end-to-end', verify=verify, evaluate=evaluate, publish=comments.publish,
            confirm=lambda settings: action.confirm_context(settings, transport=httpx.MockTransport(github)),
            check_publish=lambda data, settings, **kwargs: action.publish_check(data, settings,
                transport=httpx.MockTransport(github), **kwargs))
    finally:
        client.close()
    assert code == 1 and ordered == ['preflight', 'one-dispatch']
    assert len(comments.writes) == len(github.writes) == 1
    assert github.rows[0]['conclusion'] == 'failure'


@pytest.mark.parametrize("status,code", [("meets", 0), ("below", 1), ("unknown", 3)])
def test_real_cli_task_gate_flows_through_mocked_preflight_comment_and_check(tmp_path, monkeypatch, status, code):
    s, github, comments = configured(tmp_path), CheckAPI(), Comments()
    s.task = TASK
    fake, client = baseline_client("held_out")
    fake.tasks = [{"id": TASK, "name": "Answer questions", "agent_id": fixtures.AGENT,
                   "dataset_id": fixtures.DS, "bar": {"version": 2}}]
    fake.task_verdict = task_payload(code, status)["task_verdict"]
    fake.task_verdict["experiment_id"] = fake.result["id"]
    fake.result["progress"].update(row_scope="held_out", include_rejected=False)
    fake.compare["base"], fake.compare["candidate"] = fake.base, fake.result
    dispatches = []
    def process(args, **kwargs):
        dispatches.append(args)
        out, err = io.StringIO(), io.StringIO()
        returned = cli.main(args[4:], out=out, err=err, client=client)
        return SimpleNamespace(returncode=returned, stdout=out.getvalue(), stderr=err.getvalue())
    monkeypatch.setattr(action.subprocess, "run", process)
    try:
        actual = _real_run(s, tmp_path / "task-check",
            verify=lambda settings: action.preflight(settings, client=client, transport=httpx.MockTransport(github)),
            publish=comments.publish,
            confirm=lambda settings: action.confirm_context(settings, transport=httpx.MockTransport(github)),
            check_publish=lambda result, settings, **kwargs: action.publish_check(result, settings,
                transport=httpx.MockTransport(github), **kwargs))
    finally:
        client.close()
    assert actual == code and len(dispatches) == 1 and len(comments.writes) == 1
    assert "--row-scope=held_out" in dispatches[0]
    assert fake.sent("POST", "/v1/experiments")["row_scope"] == "held_out"
    estimate = next(body for method, path, body in fake.calls if path == "/v1/experiments/estimate" and body.get("scorers_only"))
    assert estimate["row_scope"] == "held_out"
    assert github.rows[0]["conclusion"] == ("neutral" if code == 0 else "failure")
    assert "30 cases, failure rate up to" in github.rows[0]["output"]["summary"]
    assert len([p for m, p, _ in fake.calls if m == "POST" and p == "/v1/experiments"]) == 1


def test_unknown_check_owner_and_duplicate_identity_refuse_writes(tmp_path):
    s, result, github = configured(tmp_path), payload(), CheckAPI()
    transport = httpx.MockTransport(github)
    assert action.publish_check(action.aggregate_result(result), s, transport=transport)['status'] == 'created'
    github.writes.clear()
    original = dict(github.rows[0])
    github.rows[0]['app'] = {}
    assert action.publish_check(action.aggregate_result(result), s, transport=transport)['status'] == 'failed'
    assert not github.writes
    github.rows = [original, {**original, 'id': 100}]
    assert action.publish_check(action.aggregate_result(result), s, transport=transport)['status'] == 'failed'
    assert not github.writes


@pytest.mark.parametrize('verdict', ['improvement', 'no_clear_difference'])
def test_wrong_token_app_never_creates_a_merge_acceptable_check(tmp_path, verdict):
    s, github = configured(tmp_path), CheckAPI()
    github.actual_app_id = APP_ID + 1
    out = action.publish_check(action.aggregate_result(payload(0, verdict)), s, transport=httpx.MockTransport(github))
    assert out['status'] == 'uncertain' and out['conclusion'] is None
    assert len(github.writes) == 1 and github.rows[0]['app']['id'] != APP_ID
    assert github.rows[0]['conclusion'] == 'failure'
    assert not any(json.loads(req.content)['conclusion'] in ('success', 'neutral') for req in github.writes)


@pytest.mark.parametrize("mutation", ["fork", "unknown_fork", "repo", "event", "commit", "endpoint", "budget", "publisher"])
def test_bad_context_refuses_before_any_execution(tmp_path, mutation):
    env, event = environment(tmp_path)
    if mutation == "fork": event["pull_request"]["head"]["repo"]["fork"] = True
    if mutation == "unknown_fork": event["pull_request"]["head"]["repo"].pop("fork")
    if mutation == "repo": event["pull_request"]["head"]["repo"]["full_name"] = "other/repository"
    if mutation == "event": env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
    if mutation == "commit": env["AQ_CI_COMMIT"] = "b" * 40
    if mutation == "endpoint": env["AQ_CI_AGENT_ENDPOINT"] = "https://untrusted.test"
    if mutation == "budget": env["AQ_CI_BUDGET"] = "NaN"
    if mutation == "publisher": env["AQ_CI_PUBLISHER_ID"] = "actor-id"
    Path(env["GITHUB_EVENT_PATH"]).write_text(json.dumps(event))
    with pytest.raises(action.Refused):
        action.settings(env)


class Comments:
    def __init__(self):
        self.rows, self.writes = [], []
        self.uncertain = False

    def __call__(self, request):
        assert request.headers["Authorization"] == "Bearer " + COMMENT_TOKEN
        assert API_KEY not in str(request.headers) and API_KEY not in request.content.decode()
        if request.url.path.endswith("/pulls/42"):
            return httpx.Response(200, json={"number": PR, "base": {"repo": {"full_name": REPO}}})
        if request.method == "GET" and request.url.path.endswith("/issues/42/comments"):
            return httpx.Response(200, json=self.rows)
        if request.method == "GET":
            return httpx.Response(200, json=self.rows[0])
        self.writes.append(request)
        if self.uncertain:
            raise httpx.ReadTimeout(COMMENT_TOKEN + " private response", request=request)
        row = {"id": 10, "body": json.loads(request.content)["body"], "user": {"id": 123},
               "issue_url": "https://api.github.com/repos/owner/repository/issues/42"}
        self.rows = [row]
        return httpx.Response(201 if request.method == "POST" else 200, json=row)

    def publish(self, result, **kw):
        return publisher.publish_pr_comment(result, transport=httpx.MockTransport(self), **kw)


@pytest.mark.parametrize("verdict,code", [("improvement", 0), ("no_clear_difference", 0), ("regression", 1)])
def test_real_cli_to_existing_publisher_preserves_args_outcomes_and_privacy(tmp_path, monkeypatch, verdict, code):
    s = configured(tmp_path)
    fake = ActionFake()
    fake.compare = fixtures.comparison(verdict)
    fake.base = baseline()
    fake.compare["base"] = fake.base
    fake.compare["candidate"] = fake.result
    fake.result["progress"].update(row_scope="development", include_rejected=False)
    fake.result["rows"] = [{"output": "PRIVATE_ROW_OUTPUT"}]
    fake.compare["rows"] = [{"input": "PRIVATE_INPUT"}]
    client = Client(Config(None, "aq_live_test_key", "https://aq.test", "https://app.test"),
                    transport=httpx.MockTransport(fake), sleep=lambda _: None)
    calls = []
    def process(args, **kwargs):
        calls.append(args)
        assert kwargs["env"]["AQ_API_KEY"] == API_KEY
        assert COMMENT_TOKEN not in str(kwargs["env"]) and "GITHUB_TOKEN" not in kwargs["env"]
        assert API_KEY not in str(args) and "--report-file=/dev/null" in args
        out, err = io.StringIO(), io.StringIO()
        returned = cli.main(args[4:], out=out, err=err, client=client)
        return SimpleNamespace(returncode=returned, stdout=out.getvalue(), stderr=err.getvalue() + API_KEY)
    monkeypatch.setattr(action.subprocess, "run", process)
    comments = Comments()
    dest = tmp_path / "aggregate"
    assert run(s, dest, publish=comments.publish) == code
    assert len(calls) == len(comments.writes) == 1
    body = fake.sent("POST", "/v1/experiments")
    assert body["target"]["agent_endpoint"] == "pr_42" and body["target"]["commit_sha"] == SHA
    assert body["budget_usd"] == "1.25" and body["row_scope"] == "development"
    public = "\n".join(p.read_text() for p in dest.iterdir())
    assert "PRIVATE_" not in public and API_KEY not in public and COMMENT_TOKEN not in public
    assert report.VERDICT_WORDS[verdict].lower() in (dest / "evaluation.md").read_text().lower()
    private = tmp_path / "aggregate-private"
    assert private.stat().st_mode & 0o077 == 0
    assert API_KEY not in (private / "stderr.redacted.private.txt").read_text()


@pytest.mark.parametrize("credential", [API_KEY, COMMENT_TOKEN])
@pytest.mark.parametrize("field", ["reason", "name", "planning"])
@pytest.mark.parametrize("escaped", [False, True])
def test_both_credentials_are_refused_in_all_rendered_fields(tmp_path, credential, field, escaped):
    s, result = configured(tmp_path), payload()
    value = report._markdown(credential) if escaped else credential
    if field == "reason": result[field] = value
    if field == "name": result["experiment"][field] = value
    if field == "planning": result["experiment"]["progress"]["planning"]["text"] = value
    calls = []
    with pytest.raises(action.Refused):
        run(s, tmp_path / "aggregate", evaluate=lambda *_: (0, result), publish=lambda *a, **k: calls.append(a))
    assert not calls and not (tmp_path / "aggregate/evaluation.md").exists()


@pytest.mark.parametrize("credential", [API_KEY, COMMENT_TOKEN])
@pytest.mark.parametrize("escaped", [False, True])
def test_credentials_in_nested_aggregate_keys_are_refused(tmp_path, credential, escaped):
    s, result = configured(tmp_path), payload()
    value = report._markdown(credential) if escaped else credential
    result["compare"]["driven_by"] = {value: "synthetic value"}
    comments = Comments()
    with pytest.raises(action.Refused):
        run(s, tmp_path / "aggregate", evaluate=lambda *_: (0, result), publish=comments.publish)
    assert not comments.writes and not (tmp_path / "aggregate/aggregate-result.json").exists()


@pytest.mark.parametrize("mutation", ["missing", "exit_mismatch", "pending", "missing_scorer", "wrong_commit", "wrong_endpoint"])
def test_missing_incomplete_and_mismatched_reports_never_publish(tmp_path, mutation):
    s, result = configured(tmp_path), payload()
    if mutation == "missing": result = {}
    if mutation == "exit_mismatch": result["exit_code"] = 1
    if mutation == "pending": result["experiment"]["summary"]["scorers"][0]["pending"] = 1
    if mutation == "missing_scorer": result["compare"]["scorers"] = []
    if mutation == "wrong_commit": result["experiment"]["target"]["commit_sha"] = "b" * 40
    if mutation == "wrong_endpoint": result["experiment"]["target"]["agent_endpoint"] = "DEFAULT"
    comments = Comments()
    with pytest.raises(action.Refused):
        run(s, tmp_path / "aggregate", evaluate=lambda *_: (0, result), publish=comments.publish)
    assert not comments.writes


def test_failed_dispatched_run_is_not_rescued_by_publication(tmp_path):
    s, result = configured(tmp_path), payload(3)
    result["experiment"]["status"] = "failed"
    result["experiment"]["stop_reason"] = "Synthetic task failure"
    comments = Comments()
    assert run(s, tmp_path / "aggregate", evaluate=lambda *_: (3, result), publish=comments.publish) == 3
    assert len(comments.writes) == 1
    assert "not judged" in (tmp_path / "aggregate/evaluation.md").read_text()


def test_uncertain_publication_reconciles_read_only_without_new_evaluation(tmp_path):
    s, result = configured(tmp_path), payload()
    comments = Comments()
    comments.uncertain = True
    assert run(s, tmp_path / "first", evaluate=lambda *_: (0, result), publish=comments.publish) == 4
    assert len(comments.writes) == 1
    retained = tmp_path / "first/aggregate-result.json"
    s.reconcile_report = str(retained)
    def forbidden(*args): pytest.fail("Reconciliation must not evaluate again.")
    assert run(s, tmp_path / "second", evaluate=forbidden, publish=comments.publish) == 4
    assert len(comments.writes) == 1
    comments.rows = [{"id": 10, "user": {"id": 123}, "body": report.comment_marker(s.check_key) + "\n" + report.pr_summary(result),
                      "issue_url": "https://api.github.com/repos/owner/repository/issues/42"}]
    assert run(s, tmp_path / "third", evaluate=forbidden, publish=comments.publish) == 0
    assert len(comments.writes) == 1


def test_existing_comment_update_and_unchanged_do_not_create_duplicates(tmp_path):
    s, comments = configured(tmp_path), Comments()
    comments.rows = [{"id": 10, "user": {"id": 123}, "body": report.comment_marker(s.check_key) + "\nold",
                      "issue_url": "https://api.github.com/repos/owner/repository/issues/42"}]
    assert run(s, tmp_path / "one", evaluate=lambda *_: (0, payload()), publish=comments.publish) == 0
    assert comments.writes[0].method == "PATCH"
    assert run(s, tmp_path / "two", evaluate=lambda *_: (0, payload()), publish=comments.publish) == 0
    assert len(comments.writes) == 1


def test_prestart_failure_keeps_exit_and_redacts_private_errors(tmp_path, monkeypatch, capsys):
    env, _ = environment(tmp_path)
    for k, v in env.items(): monkeypatch.setenv(k, v)
    calls = []
    def process(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=2, stdout="", stderr=API_KEY + COMMENT_TOKEN)
    monkeypatch.setattr(action.subprocess, "run", process)
    monkeypatch.setattr(action, "run", run)
    monkeypatch.setattr(action, "publish_pr_comment", lambda *a, **k: pytest.fail("No valid report may not publish."))
    assert action.main() == 2
    assert len(calls) == 1
    assert "evaluation-exit-code=2" in (tmp_path / "outputs").read_text()
    assert API_KEY not in capsys.readouterr().out
    assert all(API_KEY not in p.read_text() and COMMENT_TOKEN not in p.read_text()
               for p in tmp_path.glob("*-private/*"))


def test_unexpected_exception_never_prints_credentials(tmp_path, monkeypatch, capsys):
    env, _ = environment(tmp_path)
    for k, v in env.items(): monkeypatch.setenv(k, v)
    original = run
    def failed(*args, **kw):
        raise RuntimeError(API_KEY + COMMENT_TOKEN)
    monkeypatch.setattr(action, "run", lambda s, output: original(s, output, evaluate=failed))
    assert action.main() == 3
    public = capsys.readouterr().out
    assert API_KEY not in public and COMMENT_TOKEN not in public and "RuntimeError" not in public


def test_substituted_reconciliation_commit_refuses_without_evaluation_or_write(tmp_path):
    s, result = configured(tmp_path), payload()
    result["experiment"]["target"]["commit_sha"] = "b" * 40
    retained = tmp_path / "substituted.json"
    retained.write_text(json.dumps(result))
    s.reconcile_report = str(retained)
    comments = Comments()
    with pytest.raises(action.Refused):
        run(s, tmp_path / "aggregate", evaluate=lambda *_: pytest.fail("No evaluation"), publish=comments.publish)
    assert not comments.writes


def test_settings_repr_does_not_contain_credentials(tmp_path):
    value = repr(configured(tmp_path))
    assert API_KEY not in value and COMMENT_TOKEN not in value


def test_private_debug_copies_remove_raw_and_json_encoded_known_credentials(tmp_path):
    s = configured(tmp_path)
    for secret in (API_KEY, COMMENT_TOKEN):
        escaped = report._markdown(secret)
        for text in (secret, escaped, json.dumps({"message": secret}), json.dumps({"message": escaped})):
            cleaned = action.private_text(text, s)
            assert "[credential removed]" in cleaned and secret not in cleaned and escaped not in cleaned


def test_installer_refuses_mutable_ref_and_uses_only_bundled_sdk(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.sys, "version_info", (3, 12, 0))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "install-output"))
    monkeypatch.setenv("AQ_ACTION_REF", "main")
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0))
    assert installer.install() == 2 and not calls
    monkeypatch.setenv("AQ_ACTION_REF", SHA)
    assert installer.install() == 0
    assert calls[1][-1] == str(ROOT / "packages/aq-sdk") and "--isolated" in calls[1]


@pytest.mark.parametrize("version", [(3, 11, 0), (3, 13, 0)])
def test_installer_refuses_unsupported_python_before_install(monkeypatch, version):
    monkeypatch.setattr(installer.sys, "version_info", version)
    monkeypatch.setenv("AQ_ACTION_REF", SHA)
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: pytest.fail("Unsupported Python must not install."))
    assert installer.install() == 2


def test_templates_keep_trusted_context_serialization_and_artifact_boundaries():
    metadata = yaml.safe_load((ROOT / ".github/actions/evaluate/action.yml").read_text())
    assert metadata["runs"]["using"] == "composite"
    assert metadata["inputs"]["task"]["default"] == ""
    assert metadata["runs"]["steps"][-1]["env"]["AQ_CI_TASK"] == "${{ inputs.task }}"
    assert "github.actor_id" not in json.dumps(metadata).lower()
    workflow = yaml.safe_load((ROOT / "docs/ci/github-agent-evaluation.yml").read_text())
    deploy, evaluate = workflow["jobs"].values()
    assert workflow["permissions"] == {} and workflow["concurrency"]["cancel-in-progress"] is False
    assert "vars.AQ_CHECK_KEY" in workflow["concurrency"]["group"]
    assert "concurrency" not in evaluate  # Deployment and evaluation share the same workflow lock.
    assert not any("checkout" in step.get("uses", "") for job in (deploy, evaluate) for step in job["steps"])
    assert "AQ_API_KEY" not in json.dumps(deploy) and "AQ_COMMENT_TOKEN" not in json.dumps(deploy)
    step = evaluate["steps"][0]
    assert step["with"]["publisher-id"] == "${{ vars.AQ_COMMENT_PUBLISHER_ID }}"
    assert step["with"]["baseline-experiment"] == "${{ vars.AQ_BASELINE_EXPERIMENT }}"
    assert step["with"]["base-commit"] == "${{ vars.AQ_BASE_DEPLOYED_COMMIT }}"
    assert step["with"]["base-endpoint"] == "${{ vars.AQ_BASE_AGENT_ENDPOINT }}"
    assert step["with"]["check-app-id"] == "${{ vars.AQ_CHECK_APP_ID }}"
    assert step["with"]["github-token"] == "${{ github.token }}"
    assert step["uses"].endswith("@REVIEWED_ACTION_COMMIT")
    artifact = evaluate["steps"][1]["with"]["path"]
    assert artifact.count("report-dir") == 3 and "private" not in artifact and "*" not in artifact
    assert evaluate["steps"][2]["if"] == "always()"
    config = yaml.safe_load((ROOT / "docs/ci/gitlab-agent-evaluation.yml").read_text())
    gitlab = config["evaluate-agent"]
    assert gitlab["variables"]["GIT_STRATEGY"] == "none"
    assert gitlab["resource_group"] == "agent-evaluation-$CI_PROJECT_ID-$CI_MERGE_REQUEST_IID"
    assert gitlab["environment"] == "agent-pr-evaluation" and gitlab["retry"] == 0
    assert gitlab["needs"] == [{"job": "deploy-test-endpoint", "artifacts": True}]
    rule = config["workflow"]["rules"][0]["if"]
    assert '"merge_request_event"' in rule and '"detached"' in rule
    assert "SOURCE_PROJECT_ID" in rule and "TARGET_PROJECT_ID" in rule
    assert "SOURCE_BRANCH_PROTECTED" in rule and "TARGET_BRANCH_PROTECTED" in rule
    assert config["include"][0]["ref"] == "CUSTOMER_REVIEWED_CI_COMMIT"
    assert gitlab["artifacts"]["paths"] == ["evaluation.md"]
    script = gitlab["script"][0]
    assert 'cd "$aq_ci_workdir"' in script and "python -I -m pip" in script
    assert "[sys.executable, '-I', '-m', 'aq_sdk.cli'" in script


def gitlab_environment(workspace):
    return {"AQ_DATASET": "weather", "AQ_AGENT": "my-agent", "AQ_TEST_ENDPOINT": "mr_42_123",
            "AQ_TESTED_COMMIT": SHA, "AQ_BUDGET_USD": "1", "AQ_API_KEY": API_KEY,
            "CI_PROJECT_DIR": str(workspace), "CI_PIPELINE_SOURCE": "merge_request_event",
            "CI_MERGE_REQUEST_EVENT_TYPE": "detached", "CI_PROJECT_ID": "7",
            "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "7", "CI_MERGE_REQUEST_TARGET_PROJECT_ID": "7",
            "CI_MERGE_REQUEST_SOURCE_BRANCH_PROTECTED": "true", "CI_MERGE_REQUEST_TARGET_BRANCH_PROTECTED": "true",
            "CI_MERGE_REQUEST_IID": "42", "CI_PIPELINE_ID": "123", "CI_COMMIT_SHA": SHA,
            "AQ_SDK_REVISION": SHA, "CI_JOB_TOKEN": "PRIVATE_JOB_TOKEN", "PYTHONPATH": "PRIVATE_CUSTOMER_CHECKOUT"}


@pytest.mark.parametrize("key,value", [
    ("CI_PIPELINE_SOURCE", "web"), ("CI_MERGE_REQUEST_EVENT_TYPE", "merged_result"),
    ("CI_MERGE_REQUEST_SOURCE_PROJECT_ID", "99"), ("CI_MERGE_REQUEST_TARGET_PROJECT_ID", "99"),
    ("CI_MERGE_REQUEST_SOURCE_BRANCH_PROTECTED", "false"), ("CI_MERGE_REQUEST_TARGET_BRANCH_PROTECTED", "false"),
    ("CI_PROJECT_ID", ""), ("CI_MERGE_REQUEST_IID", ""), ("CI_PIPELINE_ID", ""),
    ("AQ_TESTED_COMMIT", "b" * 40), ("CI_COMMIT_SHA", ""),
    ("AQ_TEST_ENDPOINT", "DEFAULT"), ("AQ_TEST_ENDPOINT", "mr_42_122"), ("AQ_SDK_REVISION", "main")])
def test_gitlab_preflight_refuses_untrusted_or_stale_deployment(tmp_path, monkeypatch, key, value):
    script = yaml.safe_load((ROOT / "docs/ci/gitlab-agent-evaluation.yml").read_text())["evaluate-agent"]["script"][0]
    code = re.findall(r"python -I - <<'PY'\n(.*?)\nPY", script, re.S)[0]
    env = gitlab_environment(tmp_path)
    env[key] = value
    for k, v in env.items(): monkeypatch.setenv(k, v)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Preflight refusal must not dispatch."))
    with pytest.raises(SystemExit): exec(compile(code, "gitlab-preflight", "exec"), {})


@pytest.mark.parametrize("case", ["no_clear_difference", "regression", "failed_run", "missing_report", "credential", "escaped_credential",
                                 "wrong_endpoint", "wrong_commit", "pending", "tampered_report", "missing_report_file"])
def test_gitlab_direct_cli_template_executes_offline(tmp_path, monkeypatch, capsys, case):
    config = yaml.safe_load((ROOT / "docs/ci/gitlab-agent-evaluation.yml").read_text())
    script = config["evaluate-agent"]["script"][0]
    preflight, code = re.findall(r"python -I - <<'PY'\n(.*?)\nPY", script, re.S)
    workspace, private_cwd = tmp_path / "workspace", tmp_path / "private-cwd"
    workspace.mkdir()
    private_cwd.mkdir(mode=0o700)
    (workspace / "evaluation.md").write_text("STALE_PUBLIC_REPORT")
    prefix = script.split("python -I", 1)[0]
    cleanup = subprocess.run(["bash", "-c", prefix + '\nrmdir "$aq_ci_workdir"'],
                             env={**os.environ, "CI_PROJECT_DIR": str(workspace)}, capture_output=True)
    assert cleanup.returncode == 0 and not (workspace / "evaluation.md").exists()
    monkeypatch.chdir(private_cwd)
    for key, value in gitlab_environment(workspace).items(): monkeypatch.setenv(key, value)
    exec(compile(preflight, "gitlab-preflight", "exec"), {})
    result = payload(1 if case == "regression" else 0, "regression" if case == "regression" else "no_clear_difference")
    result["experiment"]["target"]["agent_endpoint"] = "mr_42_123"
    if case == "failed_run":
        result["exit_code"] = 3
        result["experiment"]["status"] = "failed"
        result["experiment"]["stop_reason"] = "Synthetic task failure"
    if case == "credential": result["reason"] = API_KEY
    if case == "escaped_credential": result["reason"] = report._markdown(API_KEY)
    if case == "wrong_endpoint": result["experiment"]["target"]["agent_endpoint"] = "DEFAULT"
    if case == "wrong_commit": result["experiment"]["target"]["commit_sha"] = "b" * 40
    if case == "pending": result["experiment"]["summary"]["scorers"][0]["pending"] = 1
    calls = []
    def process(args, **kw):
        calls.append(args)
        assert args[:4] == [sys.executable, "-I", "-m", "aq_sdk.cli"]
        assert "--agent-endpoint=mr_42_123" in args and "--commit=" + SHA in args
        assert "--budget=1" in args and "--row-scope=development" in args
        assert "--compare-to=current" in args and "--fail-on=regression" in args
        assert "--report-file=.private/evaluation.md" in args
        assert "CI_JOB_TOKEN" not in kw["env"] and "PYTHONPATH" not in kw["env"]
        if case not in ("missing_report", "missing_report_file"):
            Path(".private/evaluation.md").write_text("ALTERED_REPORT" if case == "tampered_report" else report.pr_summary(result))
        return SimpleNamespace(returncode=2 if case == "missing_report" else result["exit_code"],
                               stdout="" if case == "missing_report" else json.dumps(result), stderr=API_KEY)
    monkeypatch.setattr(subprocess, "run", process)
    with pytest.raises(SystemExit) as exc: exec(compile(code, "gitlab-example", "exec"), {})
    assert exc.value.code == {"no_clear_difference": 0, "regression": 1, "failed_run": 3, "missing_report": 2,
                              "credential": 3, "escaped_credential": 3, "wrong_endpoint": 3, "wrong_commit": 3,
                              "pending": 3, "tampered_report": 3, "missing_report_file": 3}[case]
    assert len(calls) == 1
    assert API_KEY not in capsys.readouterr().out
    if case in ("no_clear_difference", "regression", "failed_run"):
        assert API_KEY not in (workspace / "evaluation.md").read_text()
    else:
        assert not (workspace / "evaluation.md").exists()
    for file in (private_cwd / ".private").iterdir():
        assert API_KEY not in file.read_text() and report._markdown(API_KEY) not in file.read_text()


@pytest.mark.parametrize("verdict,expected", [("no_clear_difference", 0), ("regression", 1)])
def test_gitlab_report_file_uses_real_cli_and_mocked_service(tmp_path, monkeypatch, verdict, expected):
    script = yaml.safe_load((ROOT / "docs/ci/gitlab-agent-evaluation.yml").read_text())["evaluate-agent"]["script"][0]
    preflight, code = re.findall(r"python -I - <<'PY'\n(.*?)\nPY", script, re.S)
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir(mode=0o700)
    monkeypatch.chdir(private)
    for key, value in gitlab_environment(workspace).items(): monkeypatch.setenv(key, value)
    exec(compile(preflight, "gitlab-preflight", "exec"), {})
    fake = ActionFake()
    fake.compare = fixtures.comparison(verdict)
    fake.base = baseline()
    fake.compare["base"] = fake.base
    fake.compare["candidate"] = fake.result
    fake.result["progress"].update(row_scope="development", include_rejected=False)
    fake.result["rows"] = [{"output": "PRIVATE_ROW_OUTPUT"}]
    client = Client(Config(None, "aq_live_test_key", "https://aq.test", "https://app.test"),
                    transport=httpx.MockTransport(fake), sleep=lambda _: None)
    def process(args, **kw):
        out, err = io.StringIO(), io.StringIO()
        returned = cli.main(args[4:], out=out, err=err, client=client)
        return SimpleNamespace(returncode=returned, stdout=out.getvalue(), stderr=err.getvalue())
    monkeypatch.setattr(subprocess, "run", process)
    with pytest.raises(SystemExit) as exc: exec(compile(code, "gitlab-example", "exec"), {})
    assert exc.value.code == expected
    submitted = [body for method, path, body in fake.calls if method == "POST" and path == "/v1/experiments"]
    assert len(submitted) == 1 and submitted[0]["target"]["commit_sha"] == SHA
    assert submitted[0]["target"]["agent_endpoint"] == "mr_42_123"
    assert (workspace / "evaluation.md").read_text() == (private / ".private/evaluation.md").read_text()
    assert "PRIVATE_ROW_OUTPUT" not in (workspace / "evaluation.md").read_text()


def test_isolated_interpreter_ignores_stale_workspace_imports(tmp_path):
    marker = tmp_path / "stale-executed"
    for name in ("pip.py", "sitecustomize.py", "aq_sdk.py"):
        (tmp_path / name).write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n")
    check = "import importlib.util,pathlib,sys; p=pathlib.Path(sys.argv[1]); s=importlib.util.find_spec('aq_sdk'); assert s is None or not pathlib.Path(s.origin).is_relative_to(p); import pip"
    result = subprocess.run([sys.executable, "-I", "-c", check, str(tmp_path)], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(tmp_path)}, capture_output=True)
    assert result.returncode == 0 and not marker.exists()


TASK_ID = "d1000000-0000-0000-0000-000000000001"


def public_task_payload(status="meets", code=0):
    result = payload(code)
    result["task"] = {
        "id": TASK_ID,
        "name": "Synthetic task",
        "agent_id": fixtures.AGENT,
        "dataset_id": fixtures.DS,
    }
    result["task_verdict"] = {
        "experiment_id": result["experiment"]["id"],
        "bar_version": 2,
        "status": status,
        "thresholds": [
            {
                "metric": "quality",
                "status": status,
                "value": 0.8,
                "observed": 1,
                "bound": 0.91,
                "cases": 30,
            }
        ],
        "requirements": [],
    }
    for side in (result["experiment"], result["compare"]["base"], result["compare"]["candidate"]):
        side["progress"]["row_scope"] = "held_out"
    return result


def test_task_input_is_retained_in_settings(tmp_path):
    env, _ = environment(tmp_path)
    env["AQ_CI_TASK"] = TASK_ID
    assert getattr(action.settings(env), "task", None) == TASK_ID


@pytest.mark.parametrize("status,code", [("meets", 0), ("below", 1), ("unknown", 3)])
def test_action_uses_existing_cli_task_gate_and_held_out_scope(
    tmp_path, monkeypatch, status, code
):
    env, _ = environment(tmp_path)
    env["AQ_CI_TASK"] = TASK_ID
    s = action.settings(env)
    fake = ActionFake()
    fake.tasks = [
        {
            "id": TASK_ID,
            "name": "Synthetic task",
            "agent_id": fixtures.AGENT,
            "dataset_id": fixtures.DS,
            "bar": {"version": 2},
        }
    ]
    fake.task_verdict = public_task_payload(status, code)["task_verdict"]
    fake.task_verdict["experiment_id"] = fake.result["id"]
    client = Client(
        Config(None, "aq_live_test_key", "https://aq.test", "https://app.test"),
        transport=httpx.MockTransport(fake),
        sleep=lambda _: None,
    )
    fake.base = baseline("held_out")
    fake.result["progress"].update(row_scope="held_out", include_rejected=False)
    fake.compare["base"], fake.compare["candidate"] = fake.base, fake.result
    calls = []

    def process(args, **kwargs):
        calls.append(args)
        out, err = io.StringIO(), io.StringIO()
        returned = cli.main(args[4:], out=out, err=err, client=client)
        return SimpleNamespace(
            returncode=returned, stdout=out.getvalue(), stderr=err.getvalue()
        )

    monkeypatch.setattr(action.subprocess, "run", process)
    dest = tmp_path / "aggregate"
    comments = Comments()
    assert run(s, dest, publish=comments.publish) == code
    assert len(calls) == 1 and len(comments.writes) == 1
    assert "--task=" + TASK_ID in calls[0]
    assert fake.sent("POST", "/v1/experiments")["row_scope"] == "held_out"
    saved = json.loads((dest / "aggregate-result.json").read_text())
    assert saved["task"]["id"] == TASK_ID and saved["task_verdict"]["status"] == status
    assert "Task" in (dest / "evaluation.md").read_text()


@pytest.mark.parametrize("mutation", ["missing", "task", "agent", "dataset", "verdict"])
def test_requested_task_binding_cannot_be_dropped_or_substituted(tmp_path, mutation):
    s = configured(tmp_path)
    s.task = TASK_ID
    result = public_task_payload()
    if mutation == "missing":
        result.pop("task")
    if mutation == "task":
        result["task"]["id"] = "d1000000-0000-0000-0000-000000000002"
    if mutation == "agent":
        result["task"]["agent_id"] = "another-agent"
    if mutation == "dataset":
        result["task"]["dataset_id"] = "another-dataset"
    if mutation == "verdict":
        result["task_verdict"]["experiment_id"] = "another-experiment"
    with pytest.raises(action.Refused):
        action.validate_result(result, 0, s)


def test_task_projection_keeps_gate_fields_but_not_private_rows(tmp_path):
    s = configured(tmp_path)
    s.task = TASK_ID
    result = public_task_payload()
    result["task"]["private"] = "PRIVATE_TASK_DATA"
    result["task_verdict"]["rows"] = [{"output": "PRIVATE_TASK_OUTPUT"}]
    dest = tmp_path / "aggregate"
    assert (
        run(s, dest, evaluate=lambda *_: (0, result), publish=Comments().publish)
        == 0
    )
    public = "\n".join(p.read_text() for p in dest.iterdir())
    assert "PRIVATE_TASK" not in public and TASK_ID in public


@pytest.mark.parametrize(
    "change",
    [
        {"thresholds": None},
        {"requirements": None},
        {"slices": None},
        {"slice_metadata_status": "unknown"},
        {"acceptance_reason": "held_out_required"},
        {"worst_slice": None},
        {"requirements": [{"code": "configuration_binding", "status": "unknown"}]},
        {
            "slices": [
                {
                    "dimension": "language",
                    "value": "synthetic",
                    "cases": 3,
                    "status": "unknown",
                    "thresholds": [
                        {"metric": "quality", "status": "unknown", "bound": None}
                    ],
                }
            ]
        },
    ],
)
def test_task_aggregate_preserves_nulls_unknown_requirements_and_slices(
    tmp_path, change
):
    s = configured(tmp_path)
    s.task = TASK_ID
    result = public_task_payload()
    result["task_verdict"].update(change)
    code, _ = report.outcome(
        result["experiment"],
        result["compare"],
        None,
        compare_wanted=True,
        fail_on="regression",
        task_wanted=True,
        task_verdict=result["task_verdict"],
    )
    result["exit_code"] = code
    dest = tmp_path / "aggregate"
    assert (
        run(
            s, dest, evaluate=lambda *_: (code, result), publish=Comments().publish
        )
        == code
    )
    saved = json.loads((dest / "aggregate-result.json").read_text())
    for key, value in change.items():
        assert saved["task_verdict"][key] == value


@pytest.mark.parametrize("value", ["   ", "task-name", "not-a-uuid"])
def test_invalid_task_selector_refuses_before_execution(tmp_path, value):
    env, _ = environment(tmp_path)
    env["AQ_CI_TASK"] = value
    with pytest.raises(action.Refused, match="task"):
        action.settings(env)


def test_task_reconciliation_refuses_another_requested_task_without_evaluation(
    tmp_path,
):
    s = configured(tmp_path)
    s.task = TASK_ID
    result = public_task_payload()
    retained = tmp_path / "retained.json"
    retained.write_text(json.dumps(result))
    s.reconcile_report = str(retained)
    s.task = "d1000000-0000-0000-0000-000000000002"
    comments = Comments()
    with pytest.raises(action.Refused):
        run(
            s,
            tmp_path / "aggregate",
            evaluate=lambda *_: pytest.fail("No repeat evaluation"),
            publish=comments.publish,
        )
    assert not comments.writes


def test_task_uuid_keeps_existing_canonical_form(tmp_path):
    env, _ = environment(tmp_path)
    env["AQ_CI_TASK"] = "  " + TASK_ID.upper() + "  "
    assert action.settings(env).task == TASK_ID


@pytest.mark.parametrize("scope", [None, "development", "all"])
def test_named_task_requires_held_out_baseline_before_dispatch(tmp_path, scope):
    s, github = configured(tmp_path), CheckAPI()
    s.task = TASK
    fake, client = baseline_client()
    fake.tasks = [{"id": TASK, "name": "Synthetic task", "agent_id": fixtures.AGENT,
                   "dataset_id": fixtures.DS, "bar": {"version": 2}}]
    if scope is None:
        fake.base["progress"].pop("row_scope")
    else:
        fake.base["progress"]["row_scope"] = scope
    try:
        with pytest.raises(action.Refused, match="row scope"):
            action.preflight(s, client=client, transport=httpx.MockTransport(github))
    finally:
        client.close()
    assert all(method == "GET" for method, _, _ in fake.calls)
    assert not github.writes


@pytest.mark.parametrize("owner", ["agent_id", "dataset_id"])
def test_task_owner_mismatch_refuses_before_any_evaluation(tmp_path, owner):
    s, github = configured(tmp_path), CheckAPI()
    s.task = TASK
    fake, client = baseline_client("held_out")
    fake.tasks = [{"id": TASK, "name": "Synthetic task", "agent_id": fixtures.AGENT,
                   "dataset_id": fixtures.DS, "bar": {"version": 2}}]
    fake.tasks[0][owner] = "different-owner"
    try:
        with pytest.raises(action.Refused):
            action.preflight(s, client=client, transport=httpx.MockTransport(github))
    finally:
        client.close()
    assert all(method == "GET" for method, _, _ in fake.calls)
    assert not github.writes


@pytest.mark.parametrize("owner", ["agent_id", "dataset_id"])
def test_task_binding_keeps_nonempty_owner_requirement(tmp_path, owner):
    s, result = configured(tmp_path), task_payload()
    s.task = TASK
    result["task"][owner] = result["experiment"][owner] = None
    with pytest.raises(action.Refused, match="task, agent and dataset"):
        action.validate_result(result, 0, s)


FOREIGN_REPO = "contributor/fictional-repository"


def source_environment(tmp_path, event_name="pull_request", head_repository=REPO,
                       head_fork=False, base_fork=False, opt_in=None):
    env, event = environment(tmp_path)
    env["GITHUB_EVENT_NAME"] = event_name
    if opt_in is not None:
        env["AQ_CI_ALLOW_FORKS"] = opt_in
    event["pull_request"]["head"]["repo"].update(full_name=head_repository, fork=head_fork)
    event["pull_request"]["base"]["repo"]["fork"] = base_fork
    Path(env["GITHUB_EVENT_PATH"]).write_text(json.dumps(event))
    return env


@pytest.mark.parametrize("event_name,head_repository,head_fork,base_fork,opt_in,allowed", [
    ("pull_request", REPO, False, False, None, True),
    ("pull_request", REPO, True, True, None, True),
    ("pull_request", REPO, False, False, "true", True),
    ("pull_request", FOREIGN_REPO, True, False, None, False),
    ("pull_request", FOREIGN_REPO, True, False, "true", False),
    ("pull_request_target", FOREIGN_REPO, True, False, None, False),
    ("pull_request_target", FOREIGN_REPO, True, False, "false", False),
    ("pull_request_target", FOREIGN_REPO, True, False, "true", True),
    ("pull_request_target", FOREIGN_REPO, False, True, "true", True),
    ("pull_request_target", REPO, False, False, None, False),
    ("pull_request_target", REPO, False, False, "true", False),
    ("pull_request", REPO, True, False, None, False),
    ("push", REPO, False, False, "true", False),
    ("workflow_dispatch", REPO, False, False, "true", False),
])
def test_pr_event_policy(tmp_path, event_name, head_repository, head_fork, base_fork, opt_in, allowed):
    env = source_environment(tmp_path, event_name, head_repository, head_fork, base_fork, opt_in)
    env["GITHUB_SHA"] = "c" * 40  # pull_request uses a merge SHA; this is not the tested head.
    if not allowed:
        with pytest.raises(action.Refused):
            action.settings(env)
        return
    s = action.settings(env)
    assert s.commit == SHA and s.base_commit == BASE_SHA
    assert s.head_repository == head_repository
    assert s.head_repository_is_fork is head_fork
    assert s.base_repository_is_fork is base_fork


@pytest.mark.parametrize("value", ["", "yes", "1", "TRUE", " true "])
def test_fork_opt_in_is_an_explicit_strict_boolean(tmp_path, value):
    env = source_environment(tmp_path, "pull_request_target", FOREIGN_REPO, True, False, value)
    with pytest.raises(action.Refused):
        action.settings(env)


@pytest.mark.parametrize("field,value", [
    ("head_fork", None), ("head_fork", 0), ("head_fork", "false"),
    ("base_fork", None), ("base_fork", 0), ("base_fork", "false"),
    ("head_repository", None), ("head_repository", "owner/repo/extra"),
    ("head_repository", "owner/.."), ("head_repository", ""),
])
def test_unknown_or_malformed_pr_source_refuses_before_credential_use(tmp_path, field, value):
    env = source_environment(tmp_path, **{field: value})
    class PrivateCredentials(dict):
        def get(self, key, *args):
            assert key not in ("AQ_API_KEY", "AQ_CI_GITHUB_TOKEN")
            return super().get(key, *args)
        def __getitem__(self, key):
            assert key not in ("AQ_API_KEY", "AQ_CI_GITHUB_TOKEN")
            return super().__getitem__(key)
    with pytest.raises(action.Refused):
        action.settings(PrivateCredentials(env))


def configured_source(tmp_path, cross_repository=False, fork_hosted=False):
    s = action.settings(source_environment(tmp_path,
        "pull_request_target" if cross_repository else "pull_request",
        FOREIGN_REPO if cross_repository else REPO,
        cross_repository or fork_hosted, fork_hosted, "true" if cross_repository else None))
    github = CheckAPI()
    github.head_repository = s.head_repository
    github.head_repository_is_fork = s.head_repository_is_fork
    github.base_repository_is_fork = s.base_repository_is_fork
    return s, github


@pytest.mark.parametrize("cross_repository", [False, True])
@pytest.mark.parametrize("field,value", [
    ("head_repository", "different/fictional-repository"), ("base_repository", "different/base"),
    ("head_repository_is_fork", None), ("head_repository_is_fork", "false"),
    ("base_repository_is_fork", None), ("base_repository_is_fork", True),
    ("head_sha", "c" * 40), ("base_sha", "c" * 40),
])
def test_actual_source_drift_refuses_before_evaluation(tmp_path, cross_repository, field, value):
    s, github = configured_source(tmp_path, cross_repository)
    setattr(github, field, value)
    fake, client = baseline_client()
    try:
        with pytest.raises(action.Refused):
            action.preflight(s, client=client, transport=httpx.MockTransport(github))
    finally:
        client.close()
    assert fake.calls == [] and github.writes == []


@pytest.mark.parametrize("cross_repository", [False, True])
@pytest.mark.parametrize("reconcile", [False, True])
def test_source_identity_drift_blocks_publication_and_reconcile(tmp_path, cross_repository, reconcile):
    s, github = configured_source(tmp_path, cross_repository)
    fake, client = baseline_client()
    comments, submissions = Comments(), []
    if reconcile:
        retained = tmp_path / "retained.json"
        retained.write_text(json.dumps(action.aggregate_result(payload())))
        s.reconcile_report = str(retained)
        github.head_repository = "different/fictional-repository"
    def evaluate(*_):
        submissions.append("one")
        github.head_repository = "different/fictional-repository"
        return 0, payload()
    try:
        with pytest.raises(action.Refused):
            _real_run(s, tmp_path / "drift",
                verify=lambda settings: action.preflight(settings, client=client, transport=httpx.MockTransport(github)),
                evaluate=evaluate, publish=comments.publish,
                confirm=lambda settings: action.confirm_context(settings, transport=httpx.MockTransport(github)),
                check_publish=lambda result, settings, **kw: action.publish_check(result, settings,
                    transport=httpx.MockTransport(github), **kw))
    finally:
        client.close()
    assert submissions == ([] if reconcile else ["one"])
    assert not comments.writes and not github.writes


@pytest.mark.parametrize("cross_repository,fork_hosted", [(False, False), (False, True), (True, False)])
def test_known_source_preflight_and_owned_check_stay_on_base_repository(tmp_path, cross_repository, fork_hosted):
    s, github = configured_source(tmp_path, cross_repository, fork_hosted)
    fake, client = baseline_client()
    try:
        assert action.preflight(s, client=client, transport=httpx.MockTransport(github)) == fake.base
        checked = action.publish_check(action.aggregate_result(payload()), s, transport=httpx.MockTransport(github))
    finally:
        client.close()
    assert checked["conclusion"] == "neutral" and len(github.writes) == 2
    assert all(req.url.path.startswith("/repos/" + REPO + "/") for req in github.writes)
    assert json.loads(github.writes[0].content)["conclusion"] == "failure"


def test_default_example_is_plain_same_repository_and_pinned_without_pr_checkout():
    path = ROOT / "docs/ci/github-agent-evaluation.yml"
    text = path.read_text()
    raw = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(raw["on"]) == {"pull_request"}
    parsed = yaml.safe_load(text)
    deploy, evaluate = parsed["jobs"].values()
    for job in (deploy, evaluate):
        assert job["if"] == "github.event.pull_request.head.repo.full_name == github.repository"
        assert not any("checkout" in step.get("uses", "") for step in job["steps"])
    assert evaluate["permissions"]["checks"] == "write"
    assert evaluate["permissions"]["pull-requests"] == "write"
    assert "issues" not in evaluate["permissions"]
    step = evaluate["steps"][0]
    assert step["uses"].endswith("@REVIEWED_ACTION_COMMIT")
    assert step["with"]["github-token"] == "${{ github.token }}"
    assert "allow-forks" not in step["with"]
    assert step["with"]["publish-report-links"] is False
    assert step["with"]["endpoint"] == "${{ secrets.AQ_ENDPOINT }}"
    assert step["with"]["ui-url"] == "${{ secrets.AQ_UI_URL }}"
    metadata = yaml.safe_load((ROOT / ".github/actions/evaluate/action.yml").read_text())
    assert metadata["inputs"]["allow-forks"]["default"] == "false"
    assert metadata["inputs"]["publish-report-links"]["default"] == "true"
    assert metadata["runs"]["steps"][-1]["env"]["AQ_CI_ALLOW_FORKS"] == "${{ inputs.allow-forks }}"


PRIVATE_UI = "https://private-ui.example.test/workbench"
PRIVATE_UI_HOST = "private-ui.example.test"


def private_reporting(tmp_path):
    env = source_environment(tmp_path)
    env["AQ_UI_URL"] = PRIVATE_UI
    env["AQ_CI_PUBLISH_REPORT_LINKS"] = "false"
    return action.settings(env)


@pytest.mark.parametrize("code,verdict", [(0, "no_clear_difference"), (1, "regression")])
def test_private_link_projection_preserves_original_and_every_other_report_field(tmp_path, code, verdict):
    s, original = private_reporting(tmp_path), payload(code, verdict)
    original["link"] = PRIVATE_UI + "/#/comparison"
    before = json.loads(json.dumps(original))
    projected = action.aggregate_result(original, publish_report_links=s.publish_report_links)
    expected = {key: value for key, value in original.items() if key != "link"}
    assert original == before and "link" not in projected
    assert report.pr_summary(projected) == report.pr_summary(expected)
    assert projected["exit_code"] == code and projected["verdict"] == verdict
    action.validate_result(projected, code, s)
    comments, github = Comments(), CheckAPI()
    assert run(s, tmp_path / "private-links", evaluate=lambda *_: (code, original), publish=comments.publish,
        check_publish=lambda data, settings, **kw: action.publish_check(data, settings,
            transport=httpx.MockTransport(github), **kw)) == code
    assert original == before and len(comments.writes) == 1
    public = "\n".join(p.read_text() for p in (tmp_path / "private-links").iterdir())
    assert PRIVATE_UI_HOST not in public and "evaluation.test" not in public


@pytest.mark.parametrize("value,allowed", [(None, True), ("true", True), ("false", True),
    ("", False), ("TRUE", False), ("0", False), (" false ", False), ("yes", False)])
def test_public_report_links_option_is_strict_and_backward_compatible(tmp_path, value, allowed):
    env = source_environment(tmp_path)
    if value is not None:
        env["AQ_CI_PUBLISH_REPORT_LINKS"] = value
    if not allowed:
        with pytest.raises(action.Refused):
            action.settings(env)
        return
    s = action.settings(env)
    assert s.publish_report_links is (value != "false")
    if s.publish_report_links:
        result = payload()
        assert action.aggregate_result(result)["link"] == result["link"]
        assert "evaluation.test" in action.public_body(action.aggregate_result(result), s)


@pytest.mark.parametrize("encoded", [
    PRIVATE_UI_HOST, PRIVATE_UI_HOST.upper(), PRIVATE_UI_HOST.replace(".", "%2E"),
    PRIVATE_UI_HOST.replace(".", "&#46;"), "private-ui\\.example\\.test",
    "\\u0070rivate-ui.example.test", "https%3A%2F%2Fprivate-ui.example.test%2Fworkbench",
    "https://evaluation.test", "https%3A%2F%2Fevaluation.test",
])
@pytest.mark.parametrize("location", ["reason", "nested_key", "planning"])
def test_private_addresses_in_remaining_public_fields_refuse_without_redaction(tmp_path, encoded, location):
    s, result = private_reporting(tmp_path), payload()
    if location == "reason":
        result["exit_code"] = 3
        result["experiment"]["status"] = "failed"
        result["reason"] = "Synthetic connection failure at " + encoded
    elif location == "nested_key":
        result["compare"]["driven_by"] = {encoded: "synthetic hint"}
    else:
        result["experiment"]["progress"]["planning"]["text"] = "Synthetic note: " + encoded
    before = json.loads(json.dumps(result))
    comments, github = Comments(), CheckAPI()
    out = tmp_path / "refused-host"
    with pytest.raises(action.Refused):
        run(s, out, evaluate=lambda *_: (result["exit_code"], result), publish=comments.publish,
            check_publish=lambda data, settings, **kw: action.publish_check(data, settings,
                transport=httpx.MockTransport(github), **kw))
    assert result == before and not comments.writes
    assert not (out / "aggregate-result.json").exists() and not (out / "evaluation.md").exists()
    assert len(github.writes) == 1 and github.rows[0]["conclusion"] == "failure"
    assert PRIVATE_UI_HOST not in json.dumps(github.rows) and "evaluation.test" not in json.dumps(github.rows)
    assert "not be judged" in github.rows[0]["output"]["summary"]


def test_private_address_in_check_key_cannot_leak_through_refusal_check(tmp_path):
    s, result = private_reporting(tmp_path), payload()
    s.check_key = PRIVATE_UI_HOST
    comments, github = Comments(), CheckAPI()
    with pytest.raises(action.Refused):
        run(s, tmp_path / "private-key", evaluate=lambda *_: (0, result), publish=comments.publish,
            check_publish=lambda data, settings, **kw: action.publish_check(data, settings,
                transport=httpx.MockTransport(github), **kw))
    assert not comments.writes and not github.writes
    assert not (tmp_path / "private-key/aggregate-result.json").exists()


def test_private_report_reconciliation_stays_read_only(tmp_path):
    s, result = private_reporting(tmp_path), payload()
    result["link"] = PRIVATE_UI + "/#/comparison"
    comments, github = Comments(), CheckAPI()
    def check(data, settings, **kw):
        return action.publish_check(data, settings, transport=httpx.MockTransport(github), **kw)
    assert run(s, tmp_path / "initial", evaluate=lambda *_: (0, result), publish=comments.publish, check_publish=check) == 0
    comments.writes.clear()
    github.writes.clear()
    s.reconcile_report = str(tmp_path / "initial/aggregate-result.json")
    assert run(s, tmp_path / "reconciled", evaluate=lambda *_: pytest.fail("Never redispatch"),
               publish=comments.publish, check_publish=check) == 0
    assert not comments.writes and not github.writes
    retained = json.loads(Path(s.reconcile_report).read_text())
    retained["reason"] = PRIVATE_UI_HOST
    tainted = tmp_path / "tainted.json"
    tainted.write_text(json.dumps(retained))
    s.reconcile_report = str(tainted)
    with pytest.raises(action.Refused):
        run(s, tmp_path / "tainted-reconcile", evaluate=lambda *_: pytest.fail("Never redispatch"),
            publish=comments.publish, check_publish=check)
    assert not comments.writes and not github.writes


def test_actual_cli_keeps_private_link_only_in_original_debug_record(tmp_path, monkeypatch):
    s = private_reporting(tmp_path)
    fake, old_client = baseline_client()
    old_client.close()
    fake.compare["base"], fake.compare["candidate"] = fake.base, fake.result
    fake.result["progress"].update(row_scope="development", include_rejected=False)
    client = Client(Config(None, API_KEY, s.endpoint, s.ui_url), transport=httpx.MockTransport(fake), sleep=lambda _: None)
    comments, github, dispatches = Comments(), CheckAPI(), []
    def process(args, **kwargs):
        dispatches.append(args)
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(args[4:], out=out, err=err, client=client)
        return SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())
    monkeypatch.setattr(action.subprocess, "run", process)
    try:
        actual = _real_run(s, tmp_path / "cli-private",
            verify=lambda settings: action.preflight(settings, client=client, transport=httpx.MockTransport(github)),
            publish=comments.publish,
            confirm=lambda settings: action.confirm_context(settings, transport=httpx.MockTransport(github)),
            check_publish=lambda data, settings, **kw: action.publish_check(data, settings,
                transport=httpx.MockTransport(github), **kw))
    finally:
        client.close()
    assert actual == 0 and len(dispatches) == 1
    original = json.loads((tmp_path / "cli-private-private/result.redacted.private.json").read_text())
    public = json.loads((tmp_path / "cli-private/aggregate-result.json").read_text())
    assert PRIVATE_UI_HOST in original["link"] and "link" not in public
    assert original["exit_code"] == public["exit_code"] == 0
    assert sum(method == "POST" and path == "/v1/experiments" for method, path, _ in fake.calls) == 1
    assert PRIVATE_UI_HOST not in json.dumps(public)
    assert PRIVATE_UI_HOST not in json.dumps(github.rows) and len(comments.writes) == 1


# Prompt files are synthetic data, never a checkout or executable PR input.
def prompt_configured(tmp_path, *, candidate="Fictional candidate prompt.\n", base="Fictional base prompt.\n"):
    env, _ = environment(tmp_path)
    head_file, base_file = tmp_path / "head-prompt.txt", tmp_path / "base-prompt.txt"
    head_file.write_bytes(candidate.encode("utf-8"))
    base_file.write_bytes(base.encode("utf-8"))
    env.update(AQ_CI_SYSTEM_PROMPT_FILE=str(head_file), AQ_CI_BASE_SYSTEM_PROMPT_FILE=str(base_file))
    return action.settings(env), env


def prompt_payload(s, code=0, verdict="no_clear_difference"):
    result = payload(code, verdict)
    for target, text in ((result["experiment"]["target"], s.candidate_prompt.text),
                         (result["compare"]["base"]["target"], s.base_prompt.text)):
        target["overrides"] = {"system_prompt": text, "model_id": None,
                               "max_tokens": None, "reasoning_effort": None, "skills": None}
    return result


@pytest.mark.parametrize("text", ["Fictional åäö.\n", "Fictional no final newline"])
def test_prompt_files_freeze_utf8_and_trailing_newlines_before_preflight(tmp_path, text):
    s, _ = prompt_configured(tmp_path, candidate=text)
    assert s.candidate_prompt.text.encode("utf-8") == text.encode("utf-8")
    assert s.candidate_prompt.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("bad", ["missing_pair", "missing", "symlink", "directory", "empty", "oversized", "invalid_utf8", "crlf", "credential"])
def test_invalid_prompt_files_refuse_before_any_network(tmp_path, bad):
    s, env = prompt_configured(tmp_path)
    path = Path(env["AQ_CI_SYSTEM_PROMPT_FILE"])
    if bad == "missing_pair":
        env.pop("AQ_CI_BASE_SYSTEM_PROMPT_FILE")
    elif bad == "missing":
        path.unlink()
    elif bad == "symlink":
        path.unlink()
        path.symlink_to(env["AQ_CI_BASE_SYSTEM_PROMPT_FILE"])
    elif bad == "directory":
        path.unlink()
        path.mkdir()
    else:
        path.write_bytes({"empty": b" \n", "oversized": b"x" * (256 * 1024 + 1), "invalid_utf8": b"\xff",
                          "crlf": b"Fictional.\r\n", "credential": API_KEY.encode()}[bad])
    with pytest.raises(action.Refused):
        action.settings(env)


def test_prompt_drift_refuses_before_evaluator_or_session_creation(tmp_path):
    s, env = prompt_configured(tmp_path)
    Path(env["AQ_CI_SYSTEM_PROMPT_FILE"]).write_text("Changed fictional prompt.")
    calls = []
    with pytest.raises(action.Refused):
        _real_run(s, tmp_path / "drift-prompts", verify=lambda _: calls.append("network"),
                  evaluate=lambda *_: calls.append("dispatch"), check_publish=lambda *_: {})
    assert calls == []


def test_prompt_bytes_reach_existing_cli_file_option_privately_and_serially(tmp_path, monkeypatch):
    s, _ = prompt_configured(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    def execute(args, **kwargs):
        path = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--system-prompt-file=")))
        assert path.parent == private
        assert path.read_bytes() == s.candidate_prompt.text.encode("utf-8")
        assert path.stat().st_mode & 0o777 == 0o600
        assert "--concurrency=1" in args and s.candidate_prompt.text not in " ".join(args)
        assert COMMENT_TOKEN not in json.dumps(kwargs, default=str)
        return SimpleNamespace(returncode=0, stdout=json.dumps(prompt_payload(s)), stderr="")
    monkeypatch.setattr(action.subprocess, "run", execute)
    assert action.execute(s, private)[0] == 0


@pytest.mark.parametrize("change", ["base", "candidate", "missing", "extra_model", "extra_unknown", "forged_digest"])
def test_requested_prompt_or_unrequested_override_cannot_be_substituted(tmp_path, change):
    s, _ = prompt_configured(tmp_path)
    result = prompt_payload(s)
    target = result["compare"]["base"]["target"] if change == "base" else result["experiment"]["target"]
    if change in ("base", "candidate"):
        target["overrides"]["system_prompt"] += " changed"
    elif change == "missing":
        target.pop("overrides")
    elif change == "extra_model":
        target["overrides"]["model_id"] = "other-model"
    elif change == "extra_unknown":
        target["overrides"]["skills"] = []
    else:
        target.pop("overrides")
        target["prompt_sha256"] = s.candidate_prompt.sha256
    with pytest.raises(action.Refused):
        action.validate_result(result, 0, s)


def test_only_derived_prompt_digests_survive_aggregate_and_reconciliation(tmp_path):
    s, _ = prompt_configured(tmp_path)
    original = prompt_payload(s)
    action.validate_result(original, 0, s)
    aggregate = action.aggregate_result(original, prompt_bindings=True)
    action.validate_result(aggregate, 0, s, public_projection=True)
    assert aggregate["experiment"]["target"]["prompt_sha256"] == s.candidate_prompt.sha256
    assert aggregate["compare"]["base"]["target"]["prompt_sha256"] == s.base_prompt.sha256
    assert s.candidate_prompt.text not in json.dumps(aggregate) and "overrides" not in aggregate["experiment"]["target"]
    assert original["experiment"]["target"]["overrides"]["system_prompt"] == s.candidate_prompt.text
    assert report.pr_summary(aggregate) == report.pr_summary(original)
    aggregate["experiment"]["target"]["prompt_sha256"] = "0" * 64
    with pytest.raises(action.Refused):
        action.validate_result(aggregate, 0, s, public_projection=True)


class PromptActionFake(ActionFake):
    def __init__(self, s):
        super().__init__()
        self.base = prompt_payload(s)["compare"]["base"]
        self.agents[0].update(non_production=True, supports_overrides=True)
        self.result["progress"].update(row_scope="development", include_rejected=False)
        self.compare["base"], self.compare["candidate"] = self.base, self.result
        self.estimate_change = lambda target: None

    def __call__(self, req):
        body = self.body(req)
        if req.method == "GET" and req.url.path == "/v1/experiments/" + fixtures.BASE:
            # Preserve this immutable base, including on post-execution reconciliation.
            return fixtures.FakeAQ.__call__(self, req)
        if req.method == "POST" and req.url.path == "/v1/experiments/estimate" and not body.get("scorers_only"):
            self.calls.append((req.method, req.url.path, body))
            target = {**body["target"], "endpoint_overridden": True,
                      "effective_agent_endpoint": body["target"]["agent_endpoint"]}
            self.estimate_change(target)
            return httpx.Response(200, json={"target": target, "calls": 3, "estimated_cost_usd": None})
        return super().__call__(req)


def prompt_client(s):
    fake = PromptActionFake(s)
    return fake, Client(Config(None, API_KEY, s.endpoint, s.ui_url),
                        transport=httpx.MockTransport(fake), sleep=lambda _: None)


@pytest.mark.parametrize("change", ["unsupported", "production", "base", "estimate_missing", "estimate_wrong", "estimate_model", "estimate_commit"])
def test_prompt_preflight_refuses_before_cli_dispatch(tmp_path, change):
    s, _ = prompt_configured(tmp_path)
    fake, client = prompt_client(s)
    if change == "unsupported":
        fake.agents[0]["supports_overrides"] = False
    elif change == "production":
        fake.agents[0]["non_production"] = False
    elif change == "base":
        fake.base["target"]["overrides"]["system_prompt"] = "Wrong base"
    elif change == "estimate_missing":
        fake.estimate_change = lambda target: target.pop("overrides")
    elif change == "estimate_wrong":
        fake.estimate_change = lambda target: target["overrides"].update(system_prompt="Ignored request")
    elif change == "estimate_model":
        fake.estimate_change = lambda target: target["overrides"].update(model_id="different-model")
    else:
        fake.estimate_change = lambda target: target.update(commit_sha=BASE_SHA)
    calls = []
    try:
        with pytest.raises(action.Refused):
            _real_run(s, tmp_path / "preflight-refusal", verify=lambda settings: action.preflight(settings,
                client=client, transport=httpx.MockTransport(CheckAPI())), evaluate=lambda *_: calls.append("dispatch"),
                check_publish=lambda *_: {})
    finally:
        client.close()
    assert not calls and not any(method == "POST" and path == "/v1/experiments" for method, path, _ in fake.calls)


@pytest.mark.parametrize("code,verdict", [(0, "no_clear_difference"), (1, "regression")])
def test_actual_cli_prompt_file_preflight_publication_and_reconcile(tmp_path, monkeypatch, code, verdict):
    s, _ = prompt_configured(tmp_path)
    fake, client = prompt_client(s)
    fake.compare.update(verdict=verdict, decision="adopt_candidate" if verdict == "improvement" else "keep_current")
    if code == 1:
        fake.compare["scorers"] = fixtures.comparison("regression")["scorers"]
    dispatches, comments, github = [], Comments(), CheckAPI()
    def process(args, **kwargs):
        dispatches.append(args)
        out, err = io.StringIO(), io.StringIO()
        returned = cli.main(args[4:], out=out, err=err, client=client)
        return SimpleNamespace(returncode=returned, stdout=out.getvalue(), stderr=err.getvalue())
    monkeypatch.setattr(action.subprocess, "run", process)
    def verify(settings):
        return action.preflight(settings, client=client, transport=httpx.MockTransport(github))
    def check(data, settings, **kw):
        return action.publish_check(data, settings, transport=httpx.MockTransport(github), **kw)
    try:
        dest = tmp_path / "prompt-publication"
        actual = _real_run(s, dest, verify=verify, publish=comments.publish,
                           confirm=lambda _: None, check_publish=check)
        assert actual == code and len(dispatches) == 1
        submissions = [body for method, path, body in fake.calls if method == "POST" and path == "/v1/experiments"]
        assert len(submissions) == 1 and submissions[0]["target"]["overrides"] == {"system_prompt": s.candidate_prompt.text}
        assert submissions[0]["concurrency"] == 1
        aggregate = json.loads((dest / "aggregate-result.json").read_text())
        metadata = json.loads((dest / "publication.json").read_text())
        assert metadata["system_prompt_bindings"]["candidate_sha256"] == s.candidate_prompt.sha256
        assert metadata["system_prompt_bindings"]["base_sha256"] == s.base_prompt.sha256
        assert aggregate["experiment"]["target"]["prompt_sha256"] == s.candidate_prompt.sha256
        assert len(comments.writes) == 1 and github.rows[0]["conclusion"] == ("neutral" if code == 0 else "failure")
        for file in dest.iterdir():
            assert s.candidate_prompt.text not in file.read_text() and s.base_prompt.text not in file.read_text()
        s.reconcile_report = str(dest / "aggregate-result.json")
        assert _real_run(s, tmp_path / "prompt-reconcile", verify=verify,
            evaluate=lambda *_: pytest.fail("No new dispatch"), publish=comments.publish,
            confirm=lambda _: None, check_publish=check) == code
        assert len(dispatches) == 1
    finally:
        client.close()


@pytest.mark.parametrize("field", ["reason", "name", "planning", "note", "scorer_note"])
@pytest.mark.parametrize("encoding", ["plain", "json", "url"])
def test_full_prompt_echo_refuses_all_public_surfaces(tmp_path, field, encoding):
    s, _ = prompt_configured(tmp_path)
    result = prompt_payload(s)
    echo = s.candidate_prompt.text
    if encoding == "json":
        echo = json.dumps(echo)[1:-1]
    elif encoding == "url":
        echo = action.quote(echo, safe="")
    if field == "reason":
        result["reason"] = echo
    elif field == "name":
        result["experiment"]["name"] = echo
    elif field == "planning":
        result["experiment"]["progress"]["planning"]["text"] = echo
    elif field == "note":
        result["compare"]["note"] = echo
    else:
        result["compare"]["scorers"][0]["note"] = echo
    checks, comments = [], Comments()
    with pytest.raises(action.Refused):
        _real_run(s, tmp_path / "prompt-leak", verify=lambda _: result["compare"]["base"],
            evaluate=lambda *_: (0, result), confirm=lambda _: None, publish=comments.publish,
            check_publish=lambda data, *a, **k: checks.append(data))
    assert not comments.writes and checks == [None]
    assert not (tmp_path / "prompt-leak/aggregate-result.json").exists()
    assert not (tmp_path / "prompt-leak/evaluation.md").exists()
    assert echo not in action.private_text(echo, s)


@pytest.mark.parametrize("phase", ["before_cli", "snapshot", "reconcile"])
def test_prompt_drift_after_planning_and_in_retained_binding_is_not_replaced(tmp_path, monkeypatch, phase):
    s, env = prompt_configured(tmp_path)
    calls = []
    if phase == "before_cli":
        Path(env["AQ_CI_BASE_SYSTEM_PROMPT_FILE"]).write_text("Changed base after planning")
        monkeypatch.setattr(action.subprocess, "run", lambda *_a, **_k: calls.append("dispatch"))
        with pytest.raises(action.Refused):
            action.execute(s, tmp_path)
        assert not calls
    elif phase == "snapshot":
        (tmp_path / "candidate-system-prompt.txt").symlink_to(env["AQ_CI_SYSTEM_PROMPT_FILE"])
        monkeypatch.setattr(action.subprocess, "run", lambda *_a, **_k: calls.append("dispatch"))
        with pytest.raises(action.Refused):
            action.execute(s, tmp_path)
        assert not calls
    else:
        result = prompt_payload(s)
        aggregate = action.aggregate_result(result, prompt_bindings=True)
        aggregate["compare"]["base"]["target"]["prompt_sha256"] = "0" * 64
        retained = tmp_path / "substituted.json"
        retained.write_text(json.dumps(aggregate))
        s.reconcile_report = str(retained)
        with pytest.raises(action.Refused):
            _real_run(s, tmp_path / "substituted", verify=lambda _: result["compare"]["base"],
                      evaluate=lambda *_: calls.append("dispatch"), check_publish=lambda *_: calls.append("check"))
        assert not calls


def test_prompt_file_symlink_parent_and_fifo_refuse_without_reading(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "prompt").write_text("Fictional")
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(action.Refused):
        action.read_prompt_file(str(alias / "prompt"))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(action.Refused):
        action.read_prompt_file(str(fifo))


def test_prompt_settings_hide_bytes_and_metadata_inputs_are_wired(tmp_path):
    s, _ = prompt_configured(tmp_path)
    assert s.candidate_prompt.text not in repr(s)
    meta = yaml.safe_load((ROOT / ".github/actions/evaluate/action.yml").read_text())
    for name, env in (("system-prompt-file", "AQ_CI_SYSTEM_PROMPT_FILE"),
                      ("base-system-prompt-file", "AQ_CI_BASE_SYSTEM_PROMPT_FILE")):
        assert meta["inputs"][name]["default"] == ""
        assert meta["runs"]["steps"][-1]["env"][env] == "${{ inputs." + name + " }}"


def test_no_prompt_selection_keeps_legacy_aggregate_without_digest(tmp_path):
    s, result = configured(tmp_path), payload()
    action.validate_result(result, 0, s)
    aggregate = action.aggregate_result(result)
    assert "prompt_sha256" not in aggregate["experiment"]["target"]
    action.validate_result(aggregate, 0, s, public_projection=True)


@pytest.mark.parametrize("overrides", [None, [], ["prompt"], "prompt", {"system_prompt": "Fictional candidate prompt.\n", "unexpected": None}])
def test_malformed_or_unknown_null_override_refuses_with_fixed_error(tmp_path, overrides):
    s, _ = prompt_configured(tmp_path)
    result = prompt_payload(s)
    result["experiment"]["target"]["overrides"] = overrides
    with pytest.raises(action.Refused):
        action.validate_result(result, 0, s)


def test_prompt_full_estimate_effective_endpoint_mismatch_refuses_before_dispatch(tmp_path):
    s, _ = prompt_configured(tmp_path)
    fake, client = prompt_client(s)
    fake.estimate_change = lambda target: target.update(effective_agent_endpoint="DifferentEndpoint")
    calls = []
    try:
        with pytest.raises(action.Refused):
            _real_run(s, tmp_path / "effective-endpoint-drift", verify=lambda settings: action.preflight(settings,
                client=client, transport=httpx.MockTransport(CheckAPI())), evaluate=lambda *_: calls.append("dispatch"),
                check_publish=lambda *_: {})
    finally:
        client.close()
    assert calls == []
    assert not any(method == "POST" and path == "/v1/experiments" for method, path, _ in fake.calls)


@pytest.mark.parametrize("credential", ["AQ_API_KEY", "AQ_CI_GITHUB_TOKEN"])
@pytest.mark.parametrize("encoding", ["raw", "html", "markdown", "url", "json"])
def test_encoded_known_credential_prompt_refuses_before_hash_or_network(tmp_path, monkeypatch, credential, encoding):
    _, env = prompt_configured(tmp_path)
    secret = 'synthetic-only<&"\\token'
    env[credential] = secret
    encoded = {"raw": secret, "html": action.html.escape(secret), "markdown": report._markdown(secret),
               "url": action.quote(secret, safe=""), "json": json.dumps(secret)[1:-1]}[encoding]
    Path(env["AQ_CI_SYSTEM_PROMPT_FILE"]).write_text("Do not publish " + encoded)
    monkeypatch.setattr(action.PromptFile, "sha256", property(lambda _: pytest.fail("No credential hash")))
    with pytest.raises(action.Refused):
        action.settings(env)


@pytest.mark.parametrize("side", ["base", "experiment", "comparison_candidate", "planning"])
@pytest.mark.parametrize("conflict", [None, "0" * 64])
def test_native_producer_prompt_hash_conflict_refuses(tmp_path, side, conflict):
    s, _ = prompt_configured(tmp_path)
    result = json.loads(json.dumps(prompt_payload(s)))
    if side == "planning":
        fake, client = prompt_client(s)
        fake.estimate_change = lambda target: target.update(prompt_sha256=conflict)
        try:
            with pytest.raises(action.Refused):
                action.preflight(s, client=client, transport=httpx.MockTransport(CheckAPI()))
        finally:
            client.close()
        assert not any(method == "POST" and path == "/v1/experiments" for method, path, _ in fake.calls)
    else:
        target = (result["compare"]["base"] if side == "base" else result["experiment"] if side == "experiment"
                  else result["compare"]["candidate"])["target"]
        target["prompt_sha256"] = conflict
        with pytest.raises(action.Refused):
            action.validate_result(result, 0, s)


def test_native_prompt_digest_is_reused_without_parallel_target_field(tmp_path):
    s, _ = prompt_configured(tmp_path)
    result = prompt_payload(s)
    for side in (result["experiment"], result["compare"]["base"], result["compare"]["candidate"]):
        side["target"]["prompt_sha256"] = hashlib.sha256(side["target"]["overrides"]["system_prompt"].encode()).hexdigest()
    action.validate_result(result, 0, s)
    aggregate = action.aggregate_result(result, prompt_bindings=True)
    assert aggregate["experiment"]["target"].get("prompt_sha256") == s.candidate_prompt.sha256
    assert "system_prompt_sha256" not in aggregate["experiment"]["target"]
    action.validate_result(aggregate, 0, s, public_projection=True)

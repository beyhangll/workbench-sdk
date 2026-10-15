"""Offline Action contracts. No cloud calls, model calls or real PR comments."""
import importlib.util
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
API_KEY, COMMENT_TOKEN = "aq_live_sentinel_key", "github_sentinel_token"


def environment(tmp_path):
    event = {"pull_request": {"number": PR, "head": {"sha": SHA, "repo": {"full_name": REPO, "fork": False}},
                              "base": {"repo": {"full_name": REPO}}}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    env = {"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_REPOSITORY": REPO,
           "GITHUB_EVENT_PATH": str(event_path), "AQ_CI_COMMIT": SHA, "AQ_CI_AGENT_ENDPOINT": "pr_42",
           "AQ_CI_TARGET": "my-agent", "AQ_CI_DATASET": "weather", "AQ_CI_BUDGET": "1.25",
           "AQ_ENDPOINT": "https://evaluation.test", "AQ_API_KEY": API_KEY, "AQ_CI_GITHUB_TOKEN": COMMENT_TOKEN,
           "AQ_CI_PUBLISHER_ID": "123", "AQ_CI_CHECK_KEY": "agent.dataset", "RUNNER_TEMP": str(tmp_path),
           "GITHUB_OUTPUT": str(tmp_path / "outputs")}
    return env, event


def payload(code=0, verdict="no_clear_difference"):
    exp = fixtures.experiment("candidate", "Candidate")
    exp["target"] = {"commit_sha": SHA, "agent_endpoint": "pr_42", "endpoint_overridden": True,
                     "system_prompt": "PRIVATE_SYSTEM_PROMPT"}
    exp["progress"]["planning"] = {"text": "With three rows, differences remain uncertain."}
    cmp = fixtures.comparison(verdict)
    cmp["rows"] = [{"input": "PRIVATE_INPUT", "output": "PRIVATE_ANSWER"}]
    return {"exit_code": code, "experiment": exp, "compare": cmp, "verdict": verdict, "reason": None,
            "link": "https://evaluation.test/#/comparison"}


def configured(tmp_path):
    env, _ = environment(tmp_path)
    return action.settings(env)


@pytest.mark.parametrize("mutation", ["fork", "unknown_fork", "repo", "event", "commit", "endpoint", "budget", "publisher"])
def test_bad_context_refuses_before_any_execution(tmp_path, mutation):
    env, event = environment(tmp_path)
    if mutation == "fork": event["pull_request"]["head"]["repo"]["fork"] = True
    if mutation == "unknown_fork": event["pull_request"]["head"]["repo"].pop("fork")
    if mutation == "repo": event["pull_request"]["head"]["repo"]["full_name"] = "other/repository"
    if mutation == "event": env["GITHUB_EVENT_NAME"] = "pull_request"
    if mutation == "commit": env["AQ_CI_COMMIT"] = "b" * 40
    if mutation == "endpoint": env["AQ_CI_AGENT_ENDPOINT"] = "https://untrusted.test"
    if mutation == "budget": env["AQ_CI_BUDGET"] = "NaN"
    if mutation == "publisher": env["AQ_CI_PUBLISHER_ID"] = "actor-id"
    Path(env["GITHUB_EVENT_PATH"]).write_text(json.dumps(event))
    with pytest.raises(action.Refused): action.settings(env)


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
    fake = fixtures.FakeAQ()
    fake.compare = fixtures.comparison(verdict)
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
    assert action.run(s, dest, publish=comments.publish) == code
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
        action.run(s, tmp_path / "aggregate", evaluate=lambda *_: (0, result), publish=lambda *a, **k: calls.append(a))
    assert not calls and not (tmp_path / "aggregate/evaluation.md").exists()


@pytest.mark.parametrize("credential", [API_KEY, COMMENT_TOKEN])
@pytest.mark.parametrize("escaped", [False, True])
def test_credentials_in_nested_aggregate_keys_are_refused(tmp_path, credential, escaped):
    s, result = configured(tmp_path), payload()
    value = report._markdown(credential) if escaped else credential
    result["compare"]["driven_by"] = {value: "synthetic value"}
    comments = Comments()
    with pytest.raises(action.Refused):
        action.run(s, tmp_path / "aggregate", evaluate=lambda *_: (0, result), publish=comments.publish)
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
        action.run(s, tmp_path / "aggregate", evaluate=lambda *_: (0, result), publish=comments.publish)
    assert not comments.writes


def test_failed_dispatched_run_is_not_rescued_by_publication(tmp_path):
    s, result = configured(tmp_path), payload(3)
    result["experiment"]["status"] = "failed"
    result["experiment"]["stop_reason"] = "Synthetic task failure"
    comments = Comments()
    assert action.run(s, tmp_path / "aggregate", evaluate=lambda *_: (3, result), publish=comments.publish) == 3
    assert len(comments.writes) == 1
    assert "not judged" in (tmp_path / "aggregate/evaluation.md").read_text()


def test_uncertain_publication_reconciles_read_only_without_new_evaluation(tmp_path):
    s, result = configured(tmp_path), payload()
    comments = Comments()
    comments.uncertain = True
    assert action.run(s, tmp_path / "first", evaluate=lambda *_: (0, result), publish=comments.publish) == 4
    assert len(comments.writes) == 1
    retained = tmp_path / "first/aggregate-result.json"
    s.reconcile_report = str(retained)
    def forbidden(*args): pytest.fail("Reconciliation must not evaluate again.")
    assert action.run(s, tmp_path / "second", evaluate=forbidden, publish=comments.publish) == 4
    assert len(comments.writes) == 1
    comments.rows = [{"id": 10, "user": {"id": 123}, "body": report.comment_marker(s.check_key) + "\n" + report.pr_summary(result),
                      "issue_url": "https://api.github.com/repos/owner/repository/issues/42"}]
    assert action.run(s, tmp_path / "third", evaluate=forbidden, publish=comments.publish) == 0
    assert len(comments.writes) == 1


def test_existing_comment_update_and_unchanged_do_not_create_duplicates(tmp_path):
    s, comments = configured(tmp_path), Comments()
    comments.rows = [{"id": 10, "user": {"id": 123}, "body": report.comment_marker(s.check_key) + "\nold",
                      "issue_url": "https://api.github.com/repos/owner/repository/issues/42"}]
    assert action.run(s, tmp_path / "one", evaluate=lambda *_: (0, payload()), publish=comments.publish) == 0
    assert comments.writes[0].method == "PATCH"
    assert action.run(s, tmp_path / "two", evaluate=lambda *_: (0, payload()), publish=comments.publish) == 0
    assert len(comments.writes) == 1


def test_prestart_failure_keeps_exit_and_redacts_private_errors(tmp_path, monkeypatch, capsys):
    env, _ = environment(tmp_path)
    for k, v in env.items(): monkeypatch.setenv(k, v)
    calls = []
    def process(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=2, stdout="", stderr=API_KEY + COMMENT_TOKEN)
    monkeypatch.setattr(action.subprocess, "run", process)
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
    original = action.run
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
        action.run(s, tmp_path / "aggregate", evaluate=lambda *_: pytest.fail("No evaluation"), publish=comments.publish)
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
    fake = fixtures.FakeAQ()
    fake.compare = fixtures.comparison(verdict)
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


def task_payload(status="meets", code=0):
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
    fake = fixtures.FakeAQ()
    fake.tasks = [
        {
            "id": TASK_ID,
            "name": "Synthetic task",
            "agent_id": fixtures.AGENT,
            "dataset_id": fixtures.DS,
            "bar": {"version": 2},
        }
    ]
    fake.task_verdict = task_payload(status, code)["task_verdict"]
    fake.task_verdict["experiment_id"] = fake.result["id"]
    client = Client(
        Config(None, "aq_live_test_key", "https://aq.test", "https://app.test"),
        transport=httpx.MockTransport(fake),
        sleep=lambda _: None,
    )
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
    assert action.run(s, dest, publish=comments.publish) == code
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
    result = task_payload()
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
    result = task_payload()
    result["task"]["private"] = "PRIVATE_TASK_DATA"
    result["task_verdict"]["rows"] = [{"output": "PRIVATE_TASK_OUTPUT"}]
    dest = tmp_path / "aggregate"
    assert (
        action.run(s, dest, evaluate=lambda *_: (0, result), publish=Comments().publish)
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
    result = task_payload()
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
        action.run(
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
    result = task_payload()
    retained = tmp_path / "retained.json"
    retained.write_text(json.dumps(result))
    s.reconcile_report = str(retained)
    s.task = "d1000000-0000-0000-0000-000000000002"
    comments = Comments()
    with pytest.raises(action.Refused):
        action.run(
            s,
            tmp_path / "aggregate",
            evaluate=lambda *_: pytest.fail("No repeat evaluation"),
            publish=comments.publish,
        )
    assert not comments.writes

"""Execute the customer example offline; no scheduling or evaluation is activated."""

import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import httpx
import pytest
import yaml
from aq_sdk import Client, cli
from aq_sdk.config import Config

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "scheduled_fixtures", ROOT / "packages/aq-sdk/tests/conftest.py"
)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
TASK = "d1000000-0000-0000-0000-000000000001"
KEY = "PRIVATE_SCHEDULE_KEY"


def template():
    return yaml.safe_load((ROOT / "docs/ci/scheduled-agent-evaluation.yml").read_text())


def snippet(step):
    return re.findall(
        r"<<'PY'\n(.*?)\nPY", template()["jobs"]["evaluate"]["steps"][step]["run"], re.S
    )[0]


def test_schedule_example_is_inactive_and_has_no_deployment_or_publisher():
    w = template()
    trigger = w.get("on", w.get(True))
    job = w["jobs"]["evaluate"]
    assert set(trigger) == {"schedule", "workflow_dispatch"}
    assert w["permissions"] == {} and not w["concurrency"]["cancel-in-progress"]
    assert (
        "EVALUATION_RUNNER_LABELS" in job["runs-on"] and "default_branch" in job["if"]
    )
    assert not any("checkout" in s.get("uses", "") for s in job["steps"])
    assert "AQ_COMMENT_TOKEN" not in json.dumps(
        w
    ) and "configure-aws" not in json.dumps(w)
    assert job["steps"][-1]["with"]["path"].count("report-dir") == 2
    assert "*" not in job["steps"][-1]["with"]["path"]
    assert "private" not in job["steps"][-1]["with"]["path"]


def test_mutable_install_pin_refuses_before_any_process(tmp_path, monkeypatch):
    monkeypatch.setenv("SDK_REVISION", "main")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("Invalid source pin must not install"),
    )
    with pytest.raises(SystemExit, match="reviewed full SDK commit"):
        exec(compile(snippet(1), "schedule-install", "exec"), {})


@pytest.mark.parametrize(
    "case,expected",
    [
        ("pass", 0),
        ("regression", 1),
        ("unknown", 3),
        ("credential", 3),
        ("bad_exit", 3),
        ("task_below", 1),
    ],
)
def test_scheduled_cli_runs_once_and_preserves_gates_and_privacy(
    tmp_path, monkeypatch, case, expected
):
    e = {
        "GITHUB_EVENT_NAME": "schedule",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "AQ_AGENT": "my-agent",
        "AQ_DATASET": "weather",
        "AQ_ENDPOINT": "https://evaluation.test",
        "AQ_UI_URL": "https://evaluation.test",
        "AQ_API_KEY": KEY,
        "AQ_BUDGET_USD": "1",
        "AQ_TIMEOUT": "1800",
        "GITHUB_TOKEN": "PRIVATE_UNRELATED_TOKEN",
    }
    if case == "task_below":
        e["AQ_TASK"] = TASK
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    fake = fixtures.FakeAQ()
    if case == "regression":
        fake.compare = fixtures.comparison("regression")
    if case == "unknown":
        fake.compare["scorers"][0]["status"] = "not_comparable"
    if case == "credential":
        fake.result["name"] = KEY
    if case == "task_below":
        fake.tasks = [
            {
                "id": TASK,
                "name": "Synthetic task",
                "agent_id": fixtures.AGENT,
                "dataset_id": fixtures.DS,
                "bar": {"version": 2},
            }
        ]
        fake.task_verdict = {
            "status": "below",
            "bar_version": 2,
            "experiment_id": fake.result["id"],
            "thresholds": [{"metric": "quality", "status": "below"}],
            "requirements": [],
        }
    client = Client(
        Config(None, "aq_live_test_key", "https://aq.test", "https://app.test"),
        transport=httpx.MockTransport(fake),
        sleep=lambda _: None,
    )
    calls = []

    def process(args, **kwargs):
        calls.append(args)
        assert "GITHUB_TOKEN" not in kwargs["env"] and KEY not in str(args)
        out, err = io.StringIO(), io.StringIO()
        returned = cli.main(args[4:], out=out, err=err, client=client)
        result = json.loads(out.getvalue())
        if case == "bad_exit":
            result["exit_code"] = 1
        return SimpleNamespace(
            returncode=returned, stdout=json.dumps(result), stderr=KEY
        )

    monkeypatch.setattr(subprocess, "run", process)
    with pytest.raises(SystemExit) as stopped:
        exec(compile(snippet(2), "schedule-evaluate", "exec"), {})
    assert stopped.value.code == expected and len(calls) == 1
    line = (tmp_path / "output").read_text().strip()
    root = Path(line.split("=", 1)[1])
    outcome = json.loads((root / "outcome.json").read_text())
    assert outcome["integration_exit_code"] == expected
    public = "\n".join(p.read_text() for p in root.iterdir() if p.is_file())
    assert KEY not in public and "PRIVATE_UNRELATED_TOKEN" not in public
    assert all(KEY not in p.read_text() for p in (root / "private").iterdir())
    if case in ("credential", "bad_exit"):
        assert not (root / "evaluation.md").exists()
    else:
        assert (root / "evaluation.md").exists()
    if case == "task_below":
        assert fake.sent("POST", "/v1/experiments")["row_scope"] == "held_out"
    else:
        assert fake.sent("POST", "/v1/experiments")["row_scope"] == "development"


def test_wrong_scheduled_event_refuses_before_evaluation(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("Untrusted event must not invoke CLI"),
    )
    with pytest.raises(SystemExit, match="Trusted scheduled context"):
        exec(compile(snippet(2), "schedule-evaluate", "exec"), {})

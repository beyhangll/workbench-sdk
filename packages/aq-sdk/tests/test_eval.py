from __future__ import annotations

import io

import pytest

from aq_sdk import AQError, Eval, RegressionError, get_dataset, get_scorer, list_datasets, list_scorers
import aq_sdk

from conftest import AGENT, BASE, DS, ROWS, SC1, comparison, experiment


def make(client, task=lambda x: f"answer for {x['prompt']}", **kw) -> tuple[Eval, io.StringIO]:
    out = io.StringIO()
    kw.setdefault("scorers", ["States the value"])
    return Eval("weather", task, client=client, out=out, poll_s=0, **kw), out


def test_runs_the_task_per_row_and_uploads_outputs_with_source_sdk_data(client, aq):
    seen = []

    def my_task(value):
        seen.append(value)
        return {"result": f"answer for {value['prompt']}"}

    ev, out = make(client, my_task)
    result = ev.run()
    assert sorted(v["prompt"] for v in seen) == ["question 1", "question 2", "question 3"]
    body = aq.sent("POST", "/v1/experiments/upload")
    assert body["dataset_id"] == DS and body["dataset_version"] == 3 and body["scorer_ids"] == [SC1]
    assert body["agent_id"] == AGENT and body["repeats"] == 1 and body["task_label"] == "my_task"
    assert {r["row_id"] for r in body["results"]} == {r["id"] for r in ROWS}
    first = next(r for r in body["results"] if r["row_id"] == ROWS[0]["id"])
    assert first["output"] == {"result": "answer for question 1"} and first["attempt"] == 0
    assert isinstance(first["latency_ms"], int) and "status" not in first
    assert result.experiment["status"] == "completed" and result.exit_code == 0 and result.compare is None
    assert "Open the experiment: https://app.test/#/agents/" + AGENT + "/experiments/" in out.getvalue()
    assert not any(p == "/v1/compare" for _, p, _ in aq.calls)


def test_a_task_that_raises_is_sent_as_an_error_not_a_score(client, aq):
    def flaky(value):
        if value["prompt"] == "question 2":
            raise ValueError("model timed out")
        return "ok"

    ev, out = make(client, flaky)
    result = ev.run()
    results = aq.sent("POST", "/v1/experiments/upload")["results"]
    bad = [r for r in results if r.get("status") == "error"]
    assert len(bad) == 1 and bad[0]["error"] == "ValueError: model timed out" and "output" not in bad[0]
    assert result.errors == ["ValueError: model timed out"]
    assert "1 task errors" in out.getvalue()


def test_outputs_that_are_not_json_are_sent_as_text(client, aq):
    class Answer:
        def __str__(self):
            return "strands answer"

    make(client, lambda x: Answer())[0].run()
    assert {r["output"] for r in aq.sent("POST", "/v1/experiments/upload")["results"]} == {"strands answer"}


def test_async_tasks_are_awaited(client, aq):
    async def task(x):
        return "async answer"

    make(client, task)[0].run()
    assert {r["output"] for r in aq.sent("POST", "/v1/experiments/upload")["results"]} == {"async answer"}


def test_compare_to_takes_version_repeats_and_scorers_from_the_base(client, aq):
    aq.base["dataset_version"], aq.base["repeats"] = 2, 2
    ev, out = make(client, compare_to="current", scorers=[])
    ev.run()
    body = aq.sent("POST", "/v1/experiments/upload")
    assert body["dataset_version"] == 2 and body["repeats"] == 2 and body["scorer_ids"] == [SC1]
    assert len(body["results"]) == 6 and {r["attempt"] for r in body["results"]} == {0, 1}
    assert dict(((m, p), 1) for m, p, _ in aq.calls)[("GET", "/v1/compare")]
    assert f"/compare?base={BASE}&candidate=" in out.getvalue()
    assert "Compared with \"Current version\" on 3 matched rows" in out.getvalue()


def test_fail_on_regression_raises_with_the_scorers_named(client, aq):
    aq.compare = comparison("regression")
    ev, out = make(client, compare_to="current", fail_on="regression")
    with pytest.raises(RegressionError) as err:
        ev.run()
    assert "States the value" in str(err.value)
    assert "Regression" in out.getvalue() and "worse on 5." in out.getvalue()


def test_fail_on_passes_when_there_is_no_regression_and_returns_the_result(client, aq):
    result = make(client, compare_to="current", fail_on="regression")[0].run()
    assert result.verdict == "no_clear_difference" and not result.regression and result.exit_code == 0


def test_without_fail_on_a_regression_is_reported_not_raised(client, aq):
    aq.compare = comparison("regression")
    result = make(client, compare_to="current")[0].run()
    assert result.regression and result.exit_code == 0


@pytest.mark.parametrize("case", ["errors", "no_rows_matched", "not_completed"])
def test_a_run_that_cannot_be_judged_never_passes_fail_on(client, aq, case):
    if case == "no_rows_matched":
        aq.compare = comparison(matched=0)
    if case == "not_completed":
        aq.result = experiment("c0000000-0000-0000-0000-000000000009", "x", status="failed", stop_reason="Scoring failed.")
    task = (lambda x: 1 / 0) if case == "errors" else (lambda x: "a")
    if case == "errors":
        aq.result["summary"]["target_errors"] = 3
    with pytest.raises(AQError) as err:
        make(client, task, compare_to="current", fail_on="regression")[0].run()
    assert not isinstance(err.value, RegressionError) and err.value.message


def test_allow_errors_lets_a_few_task_errors_through(client, aq):
    aq.result["summary"].update(ok=2, target_errors=1)
    aq.result["summary"]["scorers"][0]["scored"] = 2
    result = make(client, compare_to="current", fail_on="regression", allow_errors=1)[0].run()
    assert result.exit_code == 0


def test_setup_mistakes_are_plain_errors(client):
    with pytest.raises(AQError) as err:
        Eval("weather", lambda x: x, ["a"], fail_on="regression", client=client)
    assert "compare_to" in str(err.value)
    with pytest.raises(AQError) as err:
        make(client, scorers=["Nope"])[0].run()
    assert "States the value" in str(err.value)
    with pytest.raises(AQError) as err:
        make(client, scorers=[])[0].run()
    assert "scorer" in str(err.value).lower()
    with pytest.raises(AQError) as err:
        make(client, agent="ghost")[0].run()
    assert "my-agent" in str(err.value)


def test_the_agent_defaults_to_the_init_project_when_it_matches(client, aq):
    make(client)[0].run()
    assert aq.sent("POST", "/v1/experiments/upload")["agent_id"] == AGENT


def test_access_to_datasets_and_scorers(client, monkeypatch):
    monkeypatch.setattr(aq_sdk, "_client", lambda: client)
    assert [d["name"] for d in list_datasets()] == ["weather"]
    ds = get_dataset("weather")
    assert ds["version"] == 3 and len(ds["rows"]) == 3 and ds["rows"][0]["expected"] == {"value": "answer 1"}
    assert get_dataset("weather", version=1)["version"] == 1
    assert {s["name"] for s in list_scorers()} == {"States the value", "Short"}
    assert get_scorer("Short")["kind"] == "length"

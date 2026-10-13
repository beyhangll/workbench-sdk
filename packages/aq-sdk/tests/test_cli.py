from __future__ import annotations

import io
import json

import pytest

from aq_sdk import cli

from conftest import AGENT, BASE, DS, SC1, comparison, experiment


def run(client, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["eval", "run", "--dataset", "weather", "--target", "my-agent", "--poll", "0", "--budget", "1", *argv],
                    out=out, err=err, client=client)
    return code, out.getvalue(), err.getvalue()


def test_a_run_without_a_gate_exits_zero_and_prints_the_link(client, aq):
    code, out, err = run(client, "--scorer", "States the value")
    assert code == 0 and err == ""
    body = aq.sent("POST", "/v1/experiments")
    assert body["target"] == {"kind": "agentcore", "agent_id": AGENT, "overrides": {}}
    assert body["dataset_id"] == DS and body["dataset_version"] == 3 and body["scorer_ids"] == [SC1]
    assert "no overrides" in out and f"#/agents/{AGENT}/experiments/" in out


def test_no_regression_exits_zero_and_says_pass(client, aq):
    code, out, _ = run(client, "--compare-to", "current", "--fail-on", "regression")
    assert code == 0 and "Result: pass" in out
    body = aq.sent("POST", "/v1/experiments")
    assert body["scorer_ids"] == [SC1] and body["dataset_version"] == 2
    assert f"/compare?base={BASE}&candidate=" in out


@pytest.mark.parametrize("scope", ["all", "development", "held_out"])
def test_explicit_row_scope_reaches_run_request(client, aq, scope):
    aq.base["progress"]["row_scope"] = "development"
    code, _, _ = run(client, "--compare-to", "current", "--row-scope", scope)
    assert code == 0 and aq.sent("POST", "/v1/experiments")["row_scope"] == scope


def test_legacy_row_scope_defaults_to_all(client, aq):
    code, _, _ = run(client, "--scorer", "Short")
    assert code == 0 and aq.sent("POST", "/v1/experiments")["row_scope"] == "all"


@pytest.mark.parametrize("row_ids", [["selected-development-row"], []])
def test_pinned_baseline_rows_refuse_instead_of_expanding(client, aq, row_ids):
    aq.base["progress"].update(row_scope="development", row_ids=row_ids)
    code, _, err = run(client, "--compare-to", "current", "--row-scope", "development")
    assert code == 2 and "pinned row IDs" in err and "No run was started" in err
    assert not any(method == "POST" for method, path, body in aq.calls)


def test_a_regression_exits_one_and_names_the_scorers(client, aq):
    aq.compare = comparison("regression")
    code, out, _ = run(client, "--compare-to", "current", "--fail-on", "regression", "--system-prompt", "No tools.")
    assert code == 1 and "Result: fail. Regression on States the value." in out
    assert aq.sent("POST", "/v1/experiments")["target"]["overrides"] == {"system_prompt": "No tools."}


def test_a_regression_without_the_gate_still_exits_zero(client, aq):
    aq.compare = comparison("regression")
    code, out, _ = run(client, "--compare-to", "current")
    assert code == 0 and "Regression" in out


def test_overrides_from_a_file_and_a_model(client, aq, tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("Answer in one line.")
    run(client, "--scorer", "Short", "--system-prompt-file", str(f), "--model-id", "eu.amazon.nova-pro-v1:0",
        "--budget", "1.50", "--repeats", "2", "--name", "Try")
    body = aq.sent("POST", "/v1/experiments")
    assert body["target"]["overrides"] == {"system_prompt": "Answer in one line.", "model_id": "eu.amazon.nova-pro-v1:0"}
    assert (body["budget_usd"], body["repeats"], body["name"]) == ("1.50", 2, "Try")


@pytest.mark.parametrize("case,expected", [("failed", 3), ("no_rows", 3), ("task_errors", 3)])
def test_a_run_that_cannot_be_judged_exits_three(client, aq, case, expected):
    if case == "failed":
        aq.result = experiment("c0000000-0000-0000-0000-000000000009", "r", status="failed", stop_reason="Agent not reachable.")
    elif case == "no_rows":
        aq.compare = comparison(matched=0)
    else:
        aq.result["summary"]["target_errors"] = 2
    code, out, _ = run(client, "--compare-to", "current", "--fail-on", "regression")
    assert code == expected and "not judged" in out


def test_usage_and_connection_problems_exit_two(client, aq):
    code, _, err = run(client, "--fail-on", "regression")
    assert code == 2 and "--compare-to" in err
    code, _, err = run(client, "--compare-to", "current", "--scorer", "Nope")
    assert code == 2 and "States the value" in err
    aq.errors[("GET", "/v1/datasets")] = (401, {"code": "invalid_api_key", "message": "This API key is not valid.", "fix": "Create a new key."})
    code, _, err = run(client, "--scorer", "Short")
    assert code == 2 and "Create a new key." in err


def test_a_trace_only_agent_cannot_be_run(client, aq):
    aq.agents[0]["kind"] = "otlp"
    code, _, err = run(client, "--scorer", "Short")
    assert code == 2 and "only sends traces" in err


def test_json_output_has_the_exit_code_and_verdict(client, aq):
    aq.compare = comparison("regression")
    code, out, err = run(client, "--compare-to", "current", "--fail-on", "regression", "--json")
    data = json.loads(out)
    assert code == 1 and data["exit_code"] == 1 and data["verdict"] == "regression" and data["link"]
    assert "Starting" in err


def test_the_command_needs_its_arguments():
    with pytest.raises(SystemExit) as exit_:
        cli.parser().parse_args(["eval", "run"])
    assert exit_.value.code == 2


def test_words_for_one_row_and_scorer_names_in_the_failure(client, aq):
    aq.compare = comparison("regression")
    aq.compare["scorers"][0].update(improved=1, regressed=1)
    code, out, _ = run(client, "--compare-to", "current", "--fail-on", "regression")
    assert "better on 1 row, worse on 1." in out and "Regression on States the value." in out and code == 1


def test_the_budget_is_needed_and_is_sent():
    with pytest.raises(SystemExit) as exit_:
        cli.parser().parse_args(["eval", "run", "--dataset", "d", "--target", "t"])
    assert exit_.value.code == 2


def test_the_budget_goes_to_the_server(client, aq):
    run(client, "--scorer", "Short")
    assert aq.sent("POST", "/v1/experiments")["budget_usd"] == "1"


def _main(client, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["eval", "run", "--dataset", "weather", "--target", "my-agent", "--poll", "0", "--budget", "1", *argv],
                    out=out, err=err, client=client)
    return code, out.getvalue(), err.getvalue()


def test_an_unexpected_error_is_two_before_the_start_and_three_after(client, aq, monkeypatch):
    monkeypatch.setattr(client, "agent", lambda name: (_ for _ in ()).throw(RuntimeError("boom")))
    code, _, err = _main(client, "--scorer", "Short")
    assert code == 2 and "RuntimeError" in err
    monkeypatch.undo()
    monkeypatch.setattr(client, "wait", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    code, _, err = _main(client, "--scorer", "Short")
    assert code == 3 and "RuntimeError" in err


def test_a_stop_cancels_the_experiment_and_exits_three(client, aq, monkeypatch):
    seen = []
    monkeypatch.setattr(client, "experiment", lambda eid: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(client, "cancel", lambda eid: seen.append(eid))
    code, out, _ = _main(client, "--scorer", "Short")
    assert code == 3 and seen and "cancelled" in out


def test_sigterm_is_a_stop(client, aq, monkeypatch):
    import os
    import signal

    seen = []
    monkeypatch.setattr(client, "cancel", lambda eid: seen.append(eid))

    def term(eid):
        os.kill(os.getpid(), signal.SIGTERM)
        return {"status": "running", "progress": {}}

    monkeypatch.setattr(client, "experiment", term)
    before = signal.getsignal(signal.SIGTERM)
    code, _, _ = _main(client, "--scorer", "Short")
    assert code == 3 and seen and signal.getsignal(signal.SIGTERM) == before


def test_a_timeout_cancels_in_aq_and_exits_three(client, aq):
    aq.statuses = ["running"]
    code, out, _ = _main(client, "--scorer", "Short", "--timeout", "0")
    assert code == 3 and "cancelled in AQ" in out
    assert any(m == "POST" and p.endswith("/cancel") for m, p, _ in aq.calls)

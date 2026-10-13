"""Offline report and stable comment selection; no repository calls."""
from copy import deepcopy
import json

import pytest

from aq_sdk import report
from aq_sdk.config import Config
from conftest import AGENT, SC2, comparison, experiment
from test_cli import run


def test_cli_sends_endpoint_and_commit_without_changing_connected_agent(client, aq):
    endpoint = "pr_42"
    code, out, err = run(client, "--compare-to", "current", "--fail-on", "regression",
                         "--agent-endpoint", endpoint, "--commit", "a" * 40, "--json")
    assert code == 0
    body = aq.sent("POST", "/v1/experiments")
    assert body["target"]["agent_endpoint"] == endpoint and body["target"]["commit_sha"] == "a" * 40
    assert body["source"] == "sdk" and body["target"]["agent_id"] == AGENT
    assert not any(method in ("PUT", "PATCH") for method, path, body in aq.calls)
    assert "aq_live_test_key" not in out + err


def test_http_cli_refuses_the_unsupported_budget_contract_before_submission(client, aq):
    aq.agents[0]["kind"] = "http"
    code, _, err = run(client, "--compare-to", "current", "--agent-endpoint", "https://branch.test/answer")
    assert code == 2 and "HTTP target costs cannot be measured or capped" in err
    assert not any(method == "POST" for method, _, _ in aq.calls)


def mixed_scoring(aq):
    """A completed target with a deferred judge, using the existing experiment and compare responses."""
    spec = {"id": SC2, "name": "Tool evidence", "kind": "checklist_judge", "needs_trace": True, "uses_model": True}
    summary = {"scorer_id": SC2, "name": "Tool evidence", "mean": 1, "pass_rate": 1,
               "scored": 3, "errors": 0, "pending": 0}
    aq.scorers[1] = spec
    for exp in (aq.base, aq.result, aq.compare["base"]):
        exp["scorers"].append(deepcopy(spec))
        exp["summary"]["scorers"].append(deepcopy(summary))
    aq.compare["scorers"].append({**aq.compare["scorers"][0], "scorer_id": SC2, "name": "Tool evidence",
                                "base_mean": 1, "candidate_mean": 1, "difference": 0, "improved": 0,
                                "regressed": 0, "unchanged": 3, "excluded": 0})
    aq.statuses = ["completed"]


def test_pending_judge_settles_under_the_same_wait_without_target_redispatch(client, aq, monkeypatch, tmp_path):
    mixed_scoring(aq)
    aq.result["summary"]["scorers"][1].update(pending=3, scored=0, mean=None, pass_rate=None)
    sleeps = []
    def settle(seconds):
        sleeps.append(seconds)
        aq.result["summary"]["scorers"][1].update(pending=0, scored=3, mean=1, pass_rate=1)
    monkeypatch.setattr(client, "_sleep", settle)
    path = tmp_path / "evaluation.md"
    code, output, _ = run(client, "--compare-to", "current", "--fail-on", "regression", "--json",
                          "--report-file", str(path))
    assert code == 0 and len(sleeps) == 1
    assert json.loads(output)["experiment"]["summary"]["scorers"][1]["pending"] == 0
    assert "Result: No clear difference." in path.read_text()
    assert [p for m, p, _ in aq.calls if m == "POST"] == ["/v1/experiments"]


def test_pending_judge_deadline_keeps_incomplete_result_and_does_not_cancel_or_rerun(client, aq, tmp_path):
    mixed_scoring(aq)
    aq.result["summary"]["scorers"][1].update(pending=3, scored=0, mean=None, pass_rate=None)
    aq.compare["scorers"][1]["status"] = "waiting_for_traces"
    path = tmp_path / "evaluation.md"
    code, output, _ = run(client, "--compare-to", "current", "--fail-on", "regression", "--json",
                          "--timeout", "0", "--report-file", str(path))
    result = json.loads(output)
    assert code == 3 and "Still waiting for: Tool evidence" in result["reason"]
    assert result["experiment"]["status"] == "completed"
    assert result["experiment"]["summary"]["scorers"][1]["pending"] == 3
    assert "Result: not judged." in path.read_text()
    assert [p for m, p, _ in aq.calls if m == "POST"] == ["/v1/experiments"]


@pytest.mark.parametrize("case", ["error", "baseline_error", "waiting_comparison", "not_compared", "missing_summary",
                                 "missing_base_summary", "missing_pending_count", "missing_comparison",
                                 "missing_requested_scorers"])
def test_one_compared_scorer_does_not_pass_an_incomplete_required_gate(client, aq, case, tmp_path):
    mixed_scoring(aq)
    if case == "error":
        aq.result["summary"]["scorers"][1].update(errors=3, scored=0, mean=None, pass_rate=None)
        aq.compare["scorers"][1].update(candidate_mean=None, difference=None)
    elif case == "baseline_error":
        aq.compare["base"]["summary"]["scorers"][1]["errors"] = 1
    elif case in ("waiting_comparison", "not_compared"):
        aq.compare["scorers"][1]["status"] = "waiting_for_traces" if case == "waiting_comparison" else "not_compared"
    elif case == "missing_summary":
        aq.result["summary"]["scorers"].pop()
    elif case == "missing_base_summary":
        aq.compare["base"].pop("summary")
    elif case == "missing_pending_count":
        aq.result["summary"]["scorers"][1].pop("pending")
    elif case == "missing_comparison":
        aq.compare["scorers"].pop()
    else:
        aq.result.pop("scorers")
    path = tmp_path / "evaluation.md"
    code, output, _ = run(client, "--compare-to", "current", "--fail-on", "regression", "--json",
                          "--report-file", str(path))
    assert code == 3 and json.loads(output)["reason"]
    assert "Result: not judged." in path.read_text()
    assert [p for m, p, _ in aq.calls if m == "POST"] == ["/v1/experiments"]


def test_bad_commit_refuses_before_any_call(client, aq):
    code, _, err = run(client, "--scorer", "Short", "--commit", "main")
    assert code == 2 and "full" in err and not aq.calls


@pytest.mark.parametrize("verdict,words,code", [("improvement", "Improvement", 0),
    ("regression", "Regression", 1), ("no_clear_difference", "No clear difference", 0)])
def test_each_verdict_uses_compare_words_counts_and_planning(client, aq, tmp_path, verdict, words, code):
    aq.compare = comparison(verdict)
    aq.compare["scorers"][0].update(improved=9, regressed=0, unchanged=41, excluded=2)
    aq.result["progress"]["planning"] = {"text": "With 50 rows, this comparison could show a 20% difference."}
    aq.result["target"] = {"commit_sha": "a" * 40, "http": {"headers": "not-for-comment"}}
    path = tmp_path / "report.md"
    actual, out, _ = run(client, "--compare-to", "current", "--fail-on", "regression", "--report-file", str(path),
                          "--commit", "a" * 40)
    text = path.read_text()
    assert actual == code and words in text and "better on 9 rows, worse on 0" in text
    assert "41 same, 2 excluded" in text and aq.result["progress"]["planning"]["text"] in text
    assert "Caller-declared commit" in text and "Deployment identity is not attested" in text
    assert "Open evaluation" in text and "not-for-comment" not in text and "aq_live_test_key" not in text


def test_failed_run_has_a_not_judged_report(client, aq, tmp_path):
    aq.result.update(status="failed", stop_reason="Agent unavailable.")
    path = tmp_path / "failed.md"
    code, _, _ = run(client, "--compare-to", "current", "--fail-on", "regression", "--report-file", str(path))
    assert code == 3 and "Result: not judged" in path.read_text() and "Agent unavailable" in path.read_text()
    assert "Result: pass" not in path.read_text()


def test_no_compared_scorer_cannot_pass(client, aq, tmp_path):
    aq.compare["scorers"][0]["status"] = "not_compared"
    code, out, _ = run(client, "--compare-to", "current", "--fail-on", "regression")
    assert code == 3 and "No scorer could be compared" in out


def test_markdown_escapes_names_and_omits_row_payloads():
    exp = experiment("e", "<script>@someone</script>")
    cmp = comparison()
    cmp["rows"] = [{"input": "private question", "candidate_output_text": "private response"}]
    text = report.pr_summary({"exit_code": 0, "experiment": exp, "compare": cmp, "link": "javascript:bad"})
    assert "<script>" not in text and "@someone" not in text
    assert "private question" not in text and "private response" not in text and "javascript:" not in text


@pytest.mark.parametrize("url", ["https://user:credential-placeholder@ui.example.test", "https:///no-host",
    "https://ui.example.test:invalid", "https://[broken", "https://bad host.test", "https://bad\\host.test",
    "https://ui.example.test/?token=credential-placeholder"])
def test_public_report_omits_credential_bearing_or_invalid_links(url):
    text = report.pr_summary({"exit_code": 3, "link": url})
    assert "Open evaluation" not in text and "credential-placeholder" not in text and url not in text


@pytest.mark.parametrize("suffix", ["?token=credential-placeholder", "#token=credential-placeholder"])
def test_configured_query_or_fragment_is_not_forwarded_to_report(suffix):
    config = Config(project=None, api_key="key", endpoint="https://api.test", ui_url="https://ui.test/" + suffix)
    url = report.link(config, experiment("candidate", "candidate"), "baseline")
    assert url is None
    assert "credential-placeholder" not in report.pr_summary({"exit_code": 3, "link": url})


def test_normal_comparison_hash_parameters_remain_linked():
    config = Config(project=None, api_key="key", endpoint="https://api.test", ui_url="https://ui.test")
    url = report.link(config, experiment("candidate", "candidate"), "baseline")
    text = report.pr_summary({"exit_code": 3, "link": url})
    assert f"[Open evaluation](https://ui.test/#/agents/{AGENT}/compare?base=baseline&candidate=candidate)" in text


def test_public_report_encodes_markdown_destination_characters():
    text = report.pr_summary({"exit_code": 3, "link": "https://ui.example.test/path) [bad](<x>)\\\n"})
    assert "[Open evaluation](https://ui.example.test/path%29%20[bad]%28%3Cx%3E%29%5C%0A)" in text


def test_existing_comment_selection_is_stable_and_author_scoped():
    marker = report.comment_marker("agent.dataset")
    comments = [{"id": 7, "user": {"id": 12}, "body": marker + "\nold run"},
                {"id": 2, "user": {"id": 99}, "body": marker + "\nforeign"},
                {"id": 5, "user": {"id": 12}, "body": "quoted " + marker},
                {"id": 9, "user": {"id": 12}, "body": marker + "\nduplicate"}]
    chosen = report.existing_comment_id(comments, key="agent.dataset", author_id=12)
    assert chosen == 7
    comments[0]["body"] = marker + "\n" + report.pr_summary({"exit_code": 3, "reason": "Run failed."})
    assert report.existing_comment_id(list(reversed(comments)), key="agent.dataset", author_id=12) == chosen
    assert report.existing_comment_id(comments, key="another", author_id=12) is None
    with pytest.raises(ValueError):
        report.comment_marker("bad\n-->")


@pytest.mark.parametrize("author_id", [None, 0, True])
def test_comment_selection_requires_trusted_publisher_identity(author_id):
    with pytest.raises(ValueError):
        report.existing_comment_id([], key="check", author_id=author_id)


@pytest.mark.parametrize("target", [None, {"agent_endpoint": "pr_42", "commit_sha": "a" * 40},
    {"agent_endpoint": "DEFAULT", "endpoint_overridden": True, "commit_sha": "a" * 40},
    {"agent_endpoint": "pr_42", "endpoint_overridden": True, "commit_sha": "b" * 40}])
def test_missing_or_mismatched_preflight_metadata_refuses_before_start(client, aq, monkeypatch, target):
    monkeypatch.setattr(client, "estimate", lambda body: {"target": target})
    code, _, err = run(client, "--compare-to", "current", "--agent-endpoint", "pr_42", "--commit", "a" * 40)
    assert code == 2 and "No run was started" in err
    assert not any(method == "POST" and path == "/v1/experiments" for method, path, body in aq.calls)


def test_old_server_without_sdk_preflight_does_not_start(client, aq):
    aq.errors[("POST", "/v1/experiments/estimate")] = (403, {"message": "Route not allowed."})
    code, _, _ = run(client, "--compare-to", "current", "--agent-endpoint", "pr_42")
    assert code == 2
    assert not any(method == "POST" and path == "/v1/experiments" for method, path, body in aq.calls)


def test_started_metadata_mismatch_cancels_without_redispatch_or_success(client, aq, monkeypatch):
    original = client.start
    def changed(body):
        started = original(body)
        return {**started, "target": {}}
    monkeypatch.setattr(client, "start", changed)
    code, _, err = run(client, "--compare-to", "current", "--agent-endpoint", "pr_42")
    assert code == 3 and "Calls may already have occurred" in err
    assert sum(method == "POST" and path == "/v1/experiments" for method, path, body in aq.calls) == 1
    assert sum(method == "POST" and path.endswith("/cancel") for method, path, body in aq.calls) == 1

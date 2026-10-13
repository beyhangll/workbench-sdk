"""The real publisher against a mocked GitHub API. No repository comments are sent."""
from copy import deepcopy
import json

import httpx
import pytest

from aq_sdk import publish_pr_comment
from aq_sdk import publisher
from aq_sdk.report import comment_marker, pr_summary
from conftest import comparison, experiment

REPO, PR, USER, KEY, TOKEN = "owner/repository", 42, 123, "agent.dataset", "github_test_token"
ROOT = f"/repos/{REPO}"
ISSUE = f"{publisher.API}{ROOT}/issues/{PR}"


def result(code=0, verdict="no_clear_difference"):
    return {"exit_code": code, "experiment": experiment("candidate", "Candidate"),
            "compare": comparison(verdict), "reason": None, "link": "https://workbench.test/#/compare"}


def comment(identity=10, body="old", user=USER):
    return {"id": identity, "body": comment_marker(KEY) + "\n" + body, "user": {"id": user}, "issue_url": ISSUE}


class GitHub:
    def __init__(self):
        self.calls = []
        self.comments = []
        self.mode = "normal"
        self.read_error = None
        self.changed = None

    def __call__(self, request):
        self.calls.append(request)
        assert request.url.host == "api.github.com" and request.url.scheme == "https"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        path = request.url.path
        if self.read_error and request.method == "GET":
            return self.read_error
        if path == f"{ROOT}/pulls/{PR}":
            return httpx.Response(200, json={"number": PR, "base": {"repo": {"full_name": REPO}}})
        if request.method == "GET" and path == f"{ROOT}/issues/{PR}/comments":
            assert request.url.params["per_page"] == "100"
            start = (int(request.url.params["page"]) - 1) * 100
            return httpx.Response(200, json=self.comments[start:start + 100])
        if request.method == "GET" and path.startswith(f"{ROOT}/issues/comments/"):
            selected = self.changed or next(c for c in self.comments if c["id"] == int(path.split("/")[-1]))
            return httpx.Response(200, json=selected)
        assert request.method in ("PATCH", "POST")
        if self.mode == "refused":
            return httpx.Response(403, json={"message": f"private response {TOKEN}"})
        if self.mode == "uncertain":
            raise httpx.ReadTimeout(f"private payload {TOKEN}", request=request)
        body = json.loads(request.content)["body"]
        created = {"id": max([c["id"] for c in self.comments] + [0]) + 1,
                   "body": body, "user": {"id": USER}, "issue_url": ISSUE}
        if request.method == "POST":
            self.comments.append(created)
        else:
            created = next(c for c in self.comments if c["id"] == int(path.split("/")[-1]))
            created["body"] = body
        if self.mode == "timeout_after_write":
            raise httpx.ReadTimeout("response lost", request=request)
        if self.mode == "server_error_after_write":
            return httpx.Response(502, text="private server payload")
        return httpx.Response(201 if request.method == "POST" else 200, json=created)

    def publish(self, payload=None, **kwargs):
        options = dict(repository=REPO, pull_request=PR, check_key=KEY, github_token=TOKEN, publisher_id=USER,
                       transport=httpx.MockTransport(self))
        options.update(kwargs)
        return publish_pr_comment(result() if payload is None else payload, **options)

    def writes(self):
        return [c for c in self.calls if c.method != "GET"]


@pytest.mark.parametrize("verdict,code,words", [("improvement", 0, "Improvement"),
    ("no_clear_difference", 0, "No clear difference"), ("regression", 1, "regression")])
def test_create_uses_report_words_numbers_planning_and_separate_outcome(verdict, code, words):
    github = GitHub()
    payload = result(code, verdict)
    payload["experiment"]["progress"]["planning"] = {"text": "With 50 rows, this comparison could show a 20% difference."}
    payload["experiment"]["target"] = {"http": {"headers": "private credential"}}
    payload["compare"]["rows"] = [{"input": "private input", "output": "private business output"}]
    original = deepcopy(payload)
    published = github.publish(payload)
    assert published.status == "created" and published.evaluation_exit_code == code
    assert published.comment_id == 1 and payload == original
    body = json.loads(github.writes()[0].content)["body"]
    assert body == comment_marker(KEY) + "\n" + pr_summary(payload)
    assert f"Result: {words}." in body and "matched rows" in body
    assert payload["experiment"]["progress"]["planning"]["text"] in body
    assert "private" not in body and TOKEN not in body


def test_all_pages_are_read_then_only_oldest_marker_and_author_is_updated():
    github = GitHub()
    github.comments = [comment(i, user=999) for i in range(1, 101)] + [comment(101), comment(102)]
    published = github.publish()
    assert published.status == "updated" and published.comment_id == 101
    assert len(github.writes()) == 1 and github.writes()[0].url.path == f"{ROOT}/issues/comments/101"
    assert all(c["body"].endswith("old") for c in github.comments if c["id"] != 101)
    assert any(c.url.params.get("page") == "2" for c in github.calls)
    again = github.publish()
    assert again.status == "unchanged" and again.comment_id == 101 and len(github.writes()) == 1


@pytest.mark.parametrize("change", ["author", "marker", "pr"])
def test_changed_comment_is_not_overwritten(change):
    github = GitHub()
    github.comments = [comment()]
    github.changed = comment()
    if change == "author": github.changed["user"]["id"] = 999
    if change == "marker": github.changed["body"] = "unrelated text"
    if change == "pr": github.changed["issue_url"] = ISSUE + "0"
    assert github.publish().status == "failed" and not github.writes()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("mode", ["timeout_after_write", "server_error_after_write"])
def test_uncertain_write_is_reconciled_without_another_mutation(existing, mode):
    github = GitHub()
    github.comments = [comment()] if existing else []
    github.mode = mode
    published = github.publish(result(3))
    assert published.status == "reconciled" and published.evaluation_exit_code == 3
    assert len(github.writes()) == 1
    assert all(c.method == "GET" for c in github.calls[github.calls.index(github.writes()[0]) + 1:])
    assert "Result: not judged." in github.comments[0]["body"]


def test_unconfirmed_create_and_later_reconciliation_never_blindly_recreate():
    github = GitHub()
    github.mode = "uncertain"
    published = github.publish(result(1, "regression"))
    assert published.status == "uncertain" and published.evaluation_exit_code == 1
    assert "reconcile_only=True" in published.error and TOKEN not in published.error
    retry = github.publish(result(1, "regression"), reconcile_only=True)
    assert retry.status == "uncertain" and len(github.writes()) == 1
    github.comments = [comment(50, pr_summary(result(1, "regression")))]
    final = github.publish(result(1, "regression"), reconcile_only=True)
    assert final.status == "reconciled" and final.comment_id == 50 and len(github.writes()) == 1


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_publication_failure_does_not_replace_evaluation_outcome_or_expose_response(code):
    github = GitHub()
    github.mode = "refused"
    published = github.publish(result(code))
    assert published.status == "failed" and published.evaluation_exit_code == code
    assert published.error == "GitHub refused publication (HTTP 403)."
    assert len(github.writes()) == 1


@pytest.mark.parametrize("options", [{"repository": "owner/repo/../other"}, {"repository": "https://bad.test/r"},
    {"repository": "owner/.."}, {"pull_request": True}, {"pull_request": 0}, {"publisher_id": True},
    {"publisher_id": 0}, {"check_key": "bad\nmarker"}, {"github_token": "aq_live_secret"},
    {"github_token": "secret\n"}, {"reconcile_only": "false"}])
def test_invalid_or_untrusted_parameters_refuse_before_network(options):
    github = GitHub()
    published = github.publish(**options)
    assert published.status == "failed" and not github.calls


@pytest.mark.parametrize("payload", [{}, {"exit_code": True}, {"exit_code": None}, {"exit_code": 4}])
def test_missing_report_or_outcome_is_never_invented(payload):
    github = GitHub()
    published = github.publish(payload)
    assert published.status == "failed" and published.evaluation_exit_code is None and not github.calls


@pytest.mark.parametrize("response", [httpx.Response(302, headers={"location": "https://foreign.test/"}),
    httpx.Response(429, headers={"retry-after": "60"}), httpx.Response(401, text=TOKEN),
    httpx.Response(200, text="invalid JSON")])
def test_read_failure_redirect_or_rate_limit_never_writes(response):
    github = GitHub()
    github.read_error = response
    published = github.publish()
    assert published.status == "failed" and not github.writes() and len(github.calls) == 1
    assert TOKEN not in published.error


def test_pagination_bound_fails_closed_before_writing(monkeypatch):
    github = GitHub()
    github.comments = [comment(i, user=999) for i in range(1, 201)]
    monkeypatch.setattr(publisher, "MAX_PAGES", 2)
    published = github.publish()
    assert published.status == "failed" and "pagination limit" in published.error and not github.writes()


def test_failed_run_can_be_published_without_fabricating_success():
    github = GitHub()
    payload = {"exit_code": 3, "reason": "The evaluation did not finish."}
    published = github.publish(payload)
    assert published.status == "created" and published.evaluation_exit_code == 3
    assert "Result: not judged." in github.comments[0]["body"]
    assert "matched rows" not in github.comments[0]["body"]


def test_exit_zero_without_an_experiment_is_not_published_as_completed():
    github = GitHub()
    published = github.publish({"exit_code": 0})
    assert published.status == "failed" and published.evaluation_exit_code == 0 and not github.calls


def test_reconciliation_read_failure_retains_uncertain_publication():
    github = GitHub()
    github.read_error = httpx.Response(503, text="private payload")
    published = github.publish(reconcile_only=True)
    assert published.status == "uncertain" and not github.writes()


@pytest.mark.parametrize("response", [{"number": PR, "base": []},
    {"number": PR, "base": {"repo": {"full_name": "other/repository"}}},
    {"number": PR + 1, "base": {"repo": {"full_name": REPO}}}])
def test_wrong_or_malformed_pr_contract_is_refused(response):
    github = GitHub()
    github.read_error = httpx.Response(200, json=response)
    assert github.publish().status == "failed" and not github.writes()


@pytest.mark.parametrize("comments", [[{}], [{"id": 1, "body": "text", "user": None}], [comment(1), comment(1)]])
def test_malformed_or_duplicate_comment_page_never_creates(comments):
    github = GitHub()
    github.comments = comments
    assert github.publish().status == "failed" and not github.writes()


def test_uncertain_create_does_not_overwrite_a_different_report_on_reconciliation():
    github = GitHub()
    github.comments = [comment(body="another run")]
    published = github.publish(reconcile_only=True)
    assert published.status == "uncertain" and not github.writes()
    assert github.comments[0]["body"].endswith("another run")


@pytest.mark.parametrize("field", ["reason", "name"])
def test_escaped_repository_credential_is_refused_before_network(field):
    github = GitHub()
    payload = result(3)
    if field == "reason":
        payload["reason"] = "Unexpected credential " + TOKEN
    else:
        payload["experiment"]["name"] = "Unexpected credential " + TOKEN
    rendered = pr_summary(payload)
    assert TOKEN not in rendered and TOKEN.replace("_", r"\_") in rendered
    published = github.publish(payload)
    assert published.status == "failed" and published.evaluation_exit_code == 3 and not github.calls
    assert TOKEN not in published.error and TOKEN.replace("_", r"\_") not in published.error

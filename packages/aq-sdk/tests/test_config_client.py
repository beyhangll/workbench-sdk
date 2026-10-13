from __future__ import annotations

import httpx
import pytest

from aq_sdk import AQError, Client, build
from aq_sdk.config import Config

from conftest import DS, FakeAQ


def test_settings_come_from_arguments_then_environment(monkeypatch):
    monkeypatch.setenv("AQ_API_KEY", "aq_live_env")
    monkeypatch.setenv("AQ_ENDPOINT", "https://env.example/")
    monkeypatch.setenv("AQ_UI_URL", "https://ui.example")
    c = build("p")
    assert (c.api_key, c.endpoint, c.ui_url, c.project) == ("aq_live_env", "https://env.example", "https://ui.example", "p")
    c = build("p", "aq_live_arg", "http://localhost:8138")
    assert (c.api_key, c.endpoint) == ("aq_live_arg", "http://localhost:8138")


def test_missing_key_or_address_says_what_to_set(monkeypatch):
    monkeypatch.delenv("AQ_API_KEY", raising=False)
    monkeypatch.delenv("AQ_ENDPOINT", raising=False)
    with pytest.raises(AQError) as err:
        build("p", None, "https://x")
    assert "AQ_API_KEY" in str(err.value)
    with pytest.raises(AQError) as err:
        build("p", "aq_live_k", "aq.example.com")
    assert "AQ_ENDPOINT" in str(err.value) and err.value.fix


def test_ui_url_defaults_to_the_address(monkeypatch):
    monkeypatch.delenv("AQ_UI_URL", raising=False)
    assert build("p", "k", "https://aq.example").ui_url == "https://aq.example"


def test_dataset_lookup_by_name_id_and_a_helpful_miss(client):
    assert client.dataset("weather")["id"] == DS
    assert client.dataset(DS)["name"] == "weather"
    assert client.dataset("WEATHER")["id"] == DS
    with pytest.raises(AQError) as err:
        client.dataset("nope")
    assert "weather" in str(err.value) and err.value.fix


def test_rows_are_read_in_pages(client, aq, monkeypatch):
    import aq_sdk.client as module
    monkeypatch.setattr(module, "PAGE", 2)
    assert len(client.rows(DS, 3)) == 3
    assert aq.rows_pages == [0, 2]


def test_api_errors_keep_message_fix_and_code(client, aq):
    aq.errors[("GET", "/v1/scorers")] = (403, {"code": "missing_scope", "message": "No scorers:read.", "fix": "Make a key with it."})
    with pytest.raises(AQError) as err:
        client.scorers()
    e = err.value
    assert (e.status, e.code, e.message, e.fix) == (403, "missing_scope", "No scorers:read.", "Make a key with it.")
    assert str(e) == "No scorers:read. Make a key with it."


def test_a_plain_401_gets_a_fix(client, aq):
    aq.errors[("GET", "/v1/datasets")] = (401, "invalid api key")
    with pytest.raises(AQError) as err:
        client.datasets()
    assert err.value.fix and "key" in err.value.fix


def test_rate_limits_wait_and_retry_but_a_server_error_is_not_retried_on_post():
    sleeps, calls = [], []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.method)
        if req.method == "GET" and calls.count("GET") == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": "slow"})
        if req.method == "POST":
            return httpx.Response(503, json={"detail": "down"})
        return httpx.Response(200, json=[])

    c = Client(Config(None, "k", "https://aq.test", "https://aq.test"), transport=httpx.MockTransport(handler),
               sleep=sleeps.append)
    assert c.datasets() == [] and sleeps == [7.0]
    with pytest.raises(AQError) as err:
        c.post("/v1/experiments", {})
    assert err.value.status == 503 and calls.count("POST") == 1


def test_an_unreachable_server_is_a_plain_error():
    def handler(req):
        raise httpx.ConnectError("refused")

    c = Client(Config(None, "k", "https://aq.test", "https://aq.test"), transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(AQError) as err:
        c.datasets()
    assert "could not be reached" in str(err.value) and err.value.fix


def test_find_experiment_by_current_id_and_name(client, aq):
    from conftest import BASE
    assert client.find_experiment("current", dataset_id=DS, agent_id="a")["id"] == BASE
    assert client.find_experiment(BASE, dataset_id=DS, agent_id=None)["id"] == BASE
    assert client.find_experiment("Current version", dataset_id=DS, agent_id=None)["id"] == BASE
    with pytest.raises(AQError) as err:
        client.find_experiment("Missing", dataset_id=DS, agent_id=None)
    assert err.value.fix


def test_wait_polls_until_the_experiment_ends(client, aq):
    seen = []
    exp = client.wait("c0000000-0000-0000-0000-000000000009", poll_s=0, on_progress=seen.append)
    assert exp["status"] == "completed" and seen


def test_the_key_is_not_in_the_repr():
    c = Config(project="p", api_key="aq_live_secretvalue", endpoint="https://aq.test", ui_url="https://aq.test")
    assert "aq_live_secretvalue" not in repr(c) and "aq_live_secretvalue" not in str(c)


@pytest.mark.parametrize("address,fine", [
    ("https://aq.example.com", True), ("http://localhost:8138", True), ("http://127.0.0.1:8138", True),
    ("http://[::1]:8138", True), ("http://aq.localhost", True), ("http://aq.example.com", False),
    ("http://127.0.0.1.evil.example.com", False),
])
def test_plain_http_is_only_for_localhost(address, fine):
    if fine:
        assert build("p", "k", address).endpoint == address
    else:
        with pytest.raises(AQError) as err:
            build("p", "k", address)
        assert "https" in str(err.value) and err.value.fix


def test_two_completed_experiments_with_the_same_name_are_refused(client, aq):
    from conftest import BASE
    aq.base_list = None
    original = client.experiments
    client.experiments = lambda **f: [aq.base, {**aq.base, "id": "e0000000-0000-0000-0000-000000000002"}]
    with pytest.raises(AQError) as err:
        client.find_experiment("Current version", dataset_id=DS, agent_id=None)
    assert "2 completed" in str(err.value) and "ID" in err.value.fix
    client.experiments = original


def test_a_timeout_cancels_and_a_stop_cancels(client, aq):
    aq.statuses = ["running"]
    with pytest.raises(AQError) as err:
        client.wait("c0000000-0000-0000-0000-000000000009", poll_s=0, timeout_s=0)
    assert err.value.code == "wait_timeout" and "cancelled" in str(err.value)
    assert [p for m, p, _ in aq.calls if m == "POST"] == ["/v1/experiments/c0000000-0000-0000-0000-000000000009/cancel"]

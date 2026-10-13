"""A fake AQ for the unit tests. It answers the SDK's routes from memory. It exists only here."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aq_sdk import Client
from aq_sdk.config import Config, set_config

DS, AGENT, SC1, SC2 = "d0000000-0000-0000-0000-000000000001", "a0000000-0000-0000-0000-000000000001", \
    "5c000000-0000-0000-0000-000000000001", "5c000000-0000-0000-0000-000000000002"
BASE = "e0000000-0000-0000-0000-000000000001"
ROWS = [{"id": f"b000000{i}-0000-0000-0000-000000000000", "input": {"prompt": f"question {i}"},
         "expected": {"value": f"answer {i}"}} for i in range(1, 4)]


def experiment(eid: str, name: str, **over: Any) -> dict[str, Any]:
    exp = {"id": eid, "name": name, "agent_id": AGENT, "dataset_id": DS, "dataset_version": 2, "repeats": 1,
           "status": "completed", "stop_reason": None, "role": "candidate", "source": "sdk",
           "scorers": [{"id": SC1, "name": "States the value", "kind": "contains"}],
           "progress": {"finished": 3, "total": 3},
           "summary": {"rows": 3, "attempts": 3, "ok": 3, "target_errors": 0, "timeouts": 0,
                       "scorers": [{"scorer_id": SC1, "name": "States the value", "mean": 0.67, "pass_rate": 0.67,
                                    "scored": 3, "errors": 0, "pending": 0}]}}
    exp.update(over)
    return exp


def comparison(verdict: str = "no_clear_difference", matched: int = 3, base_name: str = "Current version") -> dict[str, Any]:
    return {"base": experiment(BASE, base_name, role="current", source="experiments"), "candidate": experiment("x", "x"),
            "matched_rows": matched, "verdict": verdict, "note": None, "driven_by": [SC1] if verdict != "no_clear_difference" else [],
            "decision": "adopt_candidate" if verdict == "improvement" else "keep_current",
            "scorers": [{"scorer_id": SC1, "name": "States the value", "base_mean": 0.9, "candidate_mean": 0.4,
                         "difference": -0.5, "improved": 0, "regressed": 5, "verdict": verdict, "status": "compared",
                         "note": None}]}


class FakeAQ:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.dataset = {"id": DS, "name": "weather", "current_version": 3, "row_count": 3}
        self.agents = [{"id": AGENT, "name": "my-agent", "service_name": "my-agent", "kind": "agentcore_runtime"}]
        self.scorers = [{"id": SC1, "name": "States the value", "kind": "contains"},
                        {"id": SC2, "name": "Short", "kind": "length"}]
        self.base = experiment(BASE, "Current version", role="current", source="experiments")
        self.statuses = ["running", "completed"]
        self.result = experiment("c0000000-0000-0000-0000-000000000009", "new run")
        self.compare = comparison()
        self.errors: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        self.rows_pages: list[int] = []

    def body(self, req: httpx.Request) -> dict[str, Any] | None:
        return json.loads(req.content) if req.content else None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        self.calls.append((method, path, self.body(req)))
        assert req.headers["authorization"] == "Bearer aq_live_test_key"
        if (method, path) in self.errors:
            status, detail = self.errors[(method, path)]
            return httpx.Response(status, json={"detail": detail})
        if (method, path) == ("GET", "/v1/datasets"):
            return httpx.Response(200, json=[self.dataset])
        if path == f"/v1/datasets/{DS}/rows":
            offset, limit = int(req.url.params.get("offset", 0)), int(req.url.params.get("limit", 1000))
            self.rows_pages.append(offset)
            return httpx.Response(200, json=ROWS[offset:offset + limit])
        if (method, path) == ("GET", "/v1/scorers"):
            return httpx.Response(200, json=self.scorers)
        if (method, path) == ("GET", "/v1/connected-agents"):
            return httpx.Response(200, json=self.agents)
        if (method, path) == ("GET", "/v1/experiments"):
            params = req.url.params
            if params.get("role") == "current":
                return httpx.Response(200, json=[self.base])
            return httpx.Response(200, json=[self.base])
        if (method, path) == ("GET", f"/v1/experiments/{BASE}"):
            return httpx.Response(200, json=self.base)
        if method == "GET" and path.startswith("/v1/experiments/"):
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(200, json={**self.result, "status": status} if status != "completed" else self.result)
        if (method, path) == ("POST", "/v1/experiments/estimate"):
            target = self.body(req)["target"]
            return httpx.Response(200, json={"target": {**target, "endpoint_overridden": "agent_endpoint" in target}})
        if (method, path) in (("POST", "/v1/experiments/upload"), ("POST", "/v1/experiments")):
            if path == "/v1/experiments":
                target = self.body(req)["target"]
                self.result["target"] = {**target, "endpoint_overridden": "agent_endpoint" in target}
            return httpx.Response(201, json={**self.result, "status": "running"})
        if method == "POST" and path.endswith("/cancel"):
            return httpx.Response(200, json={**self.result, "status": "cancelled"})
        if (method, path) == ("GET", "/v1/compare"):
            return httpx.Response(200, json=self.compare)
        return httpx.Response(404, json={"detail": {"code": "not_found", "message": "No such route in the fake.", "fix": None}})

    def sent(self, method: str, path: str) -> dict[str, Any]:
        return next(b for m, p, b in self.calls if (m, p) == (method, path))


@pytest.fixture
def aq() -> FakeAQ:
    return FakeAQ()


@pytest.fixture
def client(aq: FakeAQ) -> Client:
    config = Config(project="my-agent", api_key="aq_live_test_key", endpoint="https://aq.test", ui_url="https://app.test")
    set_config(config)
    c = Client(config, transport=httpx.MockTransport(aq), sleep=lambda s: None)
    yield c
    set_config(None)

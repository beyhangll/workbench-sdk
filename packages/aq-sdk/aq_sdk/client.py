"""A small client for the AQ routes the SDK and CLI use. Only a key with the right scopes works."""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from . import __version__
from .config import Config, get_config
from .errors import AQError

UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
TERMINAL = frozenset({"completed", "stopped_budget", "failed", "cancelled", "interrupted"})
PAGE = 1000
RETRY_AFTER_CAP_S = 30.0


def _error(resp: httpx.Response) -> AQError:
    message, fix, code = f"AQ answered {resp.status_code}.", None, None
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, dict):
        message = detail.get("message") or message
        fix, code = detail.get("fix"), detail.get("code")
    elif isinstance(detail, str):
        message = detail[:300]
    elif isinstance(detail, list) and detail:
        first = detail[0] if isinstance(detail[0], dict) else {}
        where = ".".join(str(p) for p in first.get("loc", [])[1:])
        message = f"AQ did not accept the request. {where}: {first.get('msg', '')}".strip()
        fix = "Check the values you pass."
    if resp.status_code == 401 and fix is None:
        fix = "Check the API key. It may be wrong, revoked or meant for another AQ address."
    return AQError(message, fix, status=resp.status_code, code=code)


class Client:
    def __init__(self, config: Config | None = None, *, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 60.0, sleep=time.sleep) -> None:
        self.config = config or get_config()
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=self.config.endpoint, transport=transport, timeout=timeout_s,
            headers={"Authorization": f"Bearer {self.config.api_key}", "User-Agent": f"aq-sdk/{__version__}"})

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, path: str, *, retry_server_errors: bool = False, **kwargs: Any) -> Any:
        """One call. Rate limits wait and retry. A server error retries only for reads."""
        attempts = 0
        while True:
            attempts += 1
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if retry_server_errors and attempts < 3:
                    self._sleep(attempts)
                    continue
                raise AQError(f"AQ could not be reached at {self.config.endpoint}: {type(exc).__name__}.",
                              "Check the address and your network, then run it again.") from None
            if resp.status_code == 429 and attempts <= 5:
                try:
                    wait = float(resp.headers.get("Retry-After", "10"))
                except ValueError:
                    wait = 10.0
                self._sleep(min(max(wait, 1.0), RETRY_AFTER_CAP_S))
                continue
            if resp.status_code in (502, 503, 504) and retry_server_errors and attempts < 3:
                self._sleep(attempts)
                continue
            if resp.status_code >= 400:
                raise _error(resp)
            return resp.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, retry_server_errors=True,
                            params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.request("POST", path, json=body)

    # ── Datasets, scorers, agents ────────────────────────────────────────

    def datasets(self) -> list[dict[str, Any]]:
        return self.get("/v1/datasets")

    def dataset(self, name_or_id: str) -> dict[str, Any]:
        return _pick(self.datasets(), name_or_id, "dataset", "Open Datasets in AQ to see the names.")

    def rows(self, dataset_id: str, version: int | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while True:
            page = self.get(f"/v1/datasets/{dataset_id}/rows", version=version, limit=PAGE, offset=len(rows))
            rows.extend(page)
            if len(page) < PAGE:
                return rows

    def scorers(self) -> list[dict[str, Any]]:
        return self.get("/v1/scorers")

    def scorer(self, name_or_id: str) -> dict[str, Any]:
        return _pick(self.scorers(), name_or_id, "scorer", "Open Scorers in AQ to see the names.")

    def agents(self) -> list[dict[str, Any]]:
        return self.get("/v1/connected-agents")

    def agent(self, name: str) -> dict[str, Any]:
        found = [a for a in self.agents() if name in (a["name"], a["id"], a.get("service_name"))]
        if len(found) == 1:
            return found[0]
        names = ", ".join(sorted(a["name"] for a in self.agents())) or "none"
        if found:
            raise AQError(f"More than one connected agent is called {name}.", "Pass the agent ID instead.")
        raise AQError(f"There is no connected agent called {name}. Connected agents: {names}.",
                      "Connect the agent in AQ first, or check the name.")

    # ── Experiments ──────────────────────────────────────────────────────

    def experiments(self, **filters: Any) -> list[dict[str, Any]]:
        return self.get("/v1/experiments", limit=200, **filters)

    def experiment(self, experiment_id: str) -> dict[str, Any]:
        return self.get(f"/v1/experiments/{experiment_id}")

    def compare(self, base: str, candidate: str) -> dict[str, Any]:
        return self.get("/v1/compare", base=base, candidate=candidate)

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.post("/v1/experiments", body)

    def estimate(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.post("/v1/experiments/estimate", body)

    def upload(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.post("/v1/experiments/upload", body)

    def cancel(self, experiment_id: str) -> dict[str, Any]:
        return self.post(f"/v1/experiments/{experiment_id}/cancel", {})

    def find_experiment(self, ref: str, *, dataset_id: str, agent_id: str | None) -> dict[str, Any]:
        """An experiment by ID, by name, or ``current`` for the agent's current version."""
        if UUID.match(ref):
            exp = self.experiment(ref)
            if exp["dataset_id"] != dataset_id:
                raise AQError(f"The experiment {exp['name']} ran on another dataset.",
                              "Pick an experiment that ran on the same dataset.")
            return exp
        if ref.lower() == "current":
            found = self.experiments(dataset_id=dataset_id, agent_id=agent_id, role="current")
            if not found:
                raise AQError("This dataset has no current-version experiment for this agent.",
                              "Run the agent as it is in AQ and mark it as the current version, or name another experiment.")
            return found[0]
        found = [e for e in self.experiments(dataset_id=dataset_id) if e["name"] == ref and e["status"] == "completed"]
        if len(found) > 1:
            raise AQError(f"{len(found)} completed experiments on this dataset are called {ref}.",
                          "Use the experiment ID, or rename the duplicates in AQ.")
        if not found:
            raise AQError(f"No completed experiment called {ref} ran on this dataset.",
                          "Use an experiment ID, an exact experiment name, or current.")
        return found[0]

    def _cancel_quietly(self, experiment_id: str) -> None:
        try:
            self.cancel(experiment_id)
        except AQError:
            pass

    def wait(self, experiment_id: str, *, poll_s: float = 3.0, timeout_s: float = 3600.0,
             on_progress=None) -> dict[str, Any]:
        """Wait for the run and pending scores. Return incomplete completed runs at the deadline for reporting."""
        deadline = time.monotonic() + timeout_s
        last = None
        try:
            while True:
                exp = self.experiment(experiment_id)
                pending = any(sc.get("pending", 0) for sc in (exp.get("summary") or {}).get("scorers", []))
                if exp["status"] in TERMINAL and (exp["status"] != "completed" or not pending):
                    return exp
                done = (exp.get("progress") or {}).get("finished")
                if on_progress and done != last:
                    on_progress(exp)
                    last = done
                if time.monotonic() > deadline:
                    if exp["status"] == "completed":
                        # The target already finished. Keep the pending grades and do not cancel or restart it.
                        return exp
                    self._cancel_quietly(experiment_id)
                    raise AQError(f"The experiment did not finish within {int(timeout_s)} seconds, so it was cancelled in AQ.",
                                  "Run it again with a longer timeout, or with fewer rows.", code="wait_timeout")
                self._sleep(poll_s)
        except (KeyboardInterrupt, SystemExit):
            self._cancel_quietly(experiment_id)
            raise


def _pick(items: list[dict[str, Any]], ref: str, what: str, hint: str) -> dict[str, Any]:
    exact = [i for i in items if ref in (i["name"], i["id"])]
    loose = exact or [i for i in items if i["name"].casefold() == ref.casefold()]
    if len(loose) == 1:
        return loose[0]
    if len(loose) > 1:
        raise AQError(f"More than one {what} is called {ref}.", f"Pass the {what} ID instead.")
    names = ", ".join(sorted(i["name"] for i in items)) or "none"
    raise AQError(f"There is no {what} called {ref}. Found: {names}.", hint)

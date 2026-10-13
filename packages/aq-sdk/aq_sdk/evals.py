"""Run a task over a dataset from AQ, score it with AQ scorers and upload the experiment.

``Eval`` runs your function on your machine, one call per dataset row (and
attempt). It sends the outputs to AQ. AQ scores them with the scorers you
saved in the app, the same code the app uses, so the numbers sit next to
experiments started in the app and compare with them. The experiment is
stored with source "sdk". AQ did not run the task, so its cost shows as
unknown.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TextIO

from . import __version__, report
from .client import Client
from .errors import AQError, RegressionError
from .tracing import span

MAX_ERROR_CHARS = 300


@dataclass
class EvalResult:
    experiment: dict[str, Any]
    compare: dict[str, Any] | None = None
    compare_error: str | None = None
    link: str | None = None
    exit_code: int = report.OK
    reason: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str | None:
        return None if self.compare is None else self.compare["verdict"]

    @property
    def regression(self) -> bool:
        return self.verdict == "regression"


def _jsonable(value: Any) -> Any:
    """Outputs travel as JSON. Anything else is sent as its text, such as a Strands result."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _call(task: Callable[[Any], Any], value: Any) -> Any:
    out = task(value)
    return asyncio.run(_await(out)) if inspect.isawaitable(out) else out


async def _await(awaitable: Any) -> Any:
    return await awaitable


def resolve_plan(client: Client, dataset: str, *, agent: str | None, compare_to: str | None,
                 version: int | None, repeats: int | None, scorers: Sequence[str],
                 agent_hint: str | None = None) -> dict[str, Any]:
    """Everything a run needs from AQ before it starts: dataset, version, rows' scorers, agent and the base.

    ``agent_hint`` (the project from ``init``) picks the agent only when it matches one. A named ``agent`` must exist.
    With ``compare_to``, the dataset version, the repeats and, when none are
    named, the scorers come from the base experiment, so the two can pair.
    """
    ds = client.dataset(dataset)
    agent_row = client.agent(agent) if agent else None
    if agent_row is None and agent_hint:
        agent_row = next((a for a in client.agents() if agent_hint in (a["name"], a.get("service_name"))), None)
    base = (client.find_experiment(compare_to, dataset_id=ds["id"], agent_id=agent_row["id"] if agent_row else None)
            if compare_to else None)
    if base is not None and agent_row is None and base.get("agent_id"):
        agent_row = next((a for a in client.agents() if a["id"] == base["agent_id"]), None)
    number = version or (base["dataset_version"] if base else ds["current_version"])
    if scorers:
        chosen = [client.scorer(s) for s in scorers]
    elif base is not None:
        chosen = [{"id": s["id"], "name": s["name"]} for s in base["scorers"]]
    else:
        raise AQError("No scorer was named.", "Name at least one scorer, or compare to an experiment to reuse its scorers.")
    return {"dataset": ds, "version": number, "base": base, "agent": agent_row, "scorers": chosen,
            "repeats": repeats or (base["repeats"] if base else 1)}


class Eval:
    """Run ``task`` on every row of ``dataset``, score it in AQ and upload the experiment.

        result = Eval(dataset="weather", task=lambda row: agent(row["prompt"]),
                      scorers=["States the true value"], compare_to="current").run()

    ``task`` gets the row's input as saved in AQ and returns the output. A
    task that raises is stored as an error for that row. It is never a zero.
    ``budget_usd`` caps what judge scorers may cost. Without it AQ uses its default.
    """

    def __init__(self, dataset: str, task: Callable[[Any], Any], scorers: Sequence[str] = (), *,
                 name: str | None = None, agent: str | None = None, dataset_version: int | None = None,
                 compare_to: str | None = None, repeats: int | None = None, concurrency: int = 4,
                 fail_on: str | None = None, allow_errors: int = 0, budget_usd: str | None = None,
                 client: Client | None = None,
                 out: TextIO | None = None, poll_s: float = 3.0) -> None:
        if fail_on not in (None, "regression"):
            raise AQError("fail_on must be regression or empty.", "Use fail_on=\"regression\".")
        if fail_on and not compare_to:
            raise AQError("fail_on needs compare_to.", "Name the experiment to compare with, or use compare_to=\"current\".")
        self.dataset, self.task, self.scorers = dataset, task, list(scorers)
        self.name, self.agent, self.dataset_version, self.compare_to = name, agent, dataset_version, compare_to
        self.repeats, self.concurrency, self.fail_on, self.allow_errors = repeats, max(1, concurrency), fail_on, allow_errors
        self.budget_usd, self.client, self.out, self.poll_s = budget_usd, client, out or sys.stdout, poll_s

    def _say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def _run_task(self, row: dict[str, Any], attempt: int) -> dict[str, Any]:
        started = time.perf_counter()
        result: dict[str, Any] = {"row_id": row["id"], "attempt": attempt}
        try:
            with span("aq.eval.task", **{"aq.eval.row_id": row["id"], "aq.eval.attempt": attempt}):
                result["output"] = _jsonable(_call(self.task, row["input"]))
        except Exception as exc:
            result.update(status="error", error=f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS])
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    def run(self) -> EvalResult:
        client = self.client or Client()
        plan = resolve_plan(client, self.dataset, agent=self.agent, compare_to=self.compare_to,
                            version=self.dataset_version, repeats=self.repeats, scorers=self.scorers,
                            agent_hint=client.config.project)
        ds, base, agent = plan["dataset"], plan["base"], plan["agent"]
        rows = client.rows(ds["id"], plan["version"])
        if not rows:
            raise AQError(f"Version {plan['version']} of {ds['name']} has no rows.", "Add rows to the dataset in AQ.")
        work = [(row, attempt) for attempt in range(plan["repeats"]) for row in rows]
        self._say(f"Running {getattr(self.task, '__name__', 'the task')} on {len(rows)} rows of {ds['name']} "
                  f"(version {plan['version']}), {plan['repeats']} attempt{'s' if plan['repeats'] != 1 else ''} each.")
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            results = list(pool.map(lambda w: self._run_task(*w), work))
        failed = [r for r in results if r.get("status") == "error"]
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        body = {"name": self.name or f"SDK run on {ds['name']} {stamp}", "dataset_id": ds["id"],
                "dataset_version": plan["version"], "agent_id": agent["id"] if agent else None,
                "scorer_ids": [s["id"] for s in plan["scorers"]], "repeats": plan["repeats"], "results": results,
                "task_label": getattr(self.task, "__name__", None), "sdk_version": __version__,
                "budget_usd": self.budget_usd}
        started = client.upload(body)
        self._say(f"Uploaded {len(results)} results ({len(failed)} task errors). AQ is scoring them.")
        exp = client.wait(started["id"], poll_s=self.poll_s)
        return self._finish(client, exp, base, [r["error"] for r in failed])

    def _finish(self, client: Client, exp: dict[str, Any], base: dict[str, Any] | None, errors: list[str]) -> EvalResult:
        for line in report.summary_lines(exp):
            self._say(line)
        cmp = cmp_error = None
        if base is not None and exp["status"] == "completed":
            try:
                cmp = client.compare(base["id"], exp["id"])
                for line in report.compare_lines(cmp):
                    self._say(line)
            except AQError as err:
                cmp_error = str(err)
                self._say(f"The comparison could not be made. {err}")
        url = report.link(client.config, exp, base["id"] if base else None)
        if url:
            self._say(f"Open the {'comparison' if base else 'experiment'}: {url}")
        else:
            self._say(f"Experiment ID: {exp['id']}. It has no agent, so the app shows it only by its ID.")
        code, reason = report.outcome(exp, cmp, cmp_error, compare_wanted=base is not None, fail_on=self.fail_on,
                                      allow_errors=self.allow_errors)
        result = EvalResult(experiment=exp, compare=cmp, compare_error=cmp_error, link=url, exit_code=code,
                            reason=reason, errors=errors)
        if code == report.REGRESSION:
            raise RegressionError(reason or "Regression.")
        if code != report.OK:
            raise AQError(reason or "The run could not be judged.")
        return result

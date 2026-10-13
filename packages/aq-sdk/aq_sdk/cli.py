"""The ``aq`` command. ``aq eval run`` starts an experiment in AQ the way the app does, waits and judges it.

    aq eval run --dataset questions --target my-agent --compare-to current --fail-on regression

The target is a connected agent. AQ runs it in your AWS account, with the
prompt or model you override, and scores it with the scorers saved in AQ. See
``report.py`` for the exit codes.
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from . import __version__, report
from .client import Client
from .config import build
from .errors import AQError
from .evals import resolve_plan

AGENT_TARGET_KINDS = {"agentcore_runtime": "agentcore", "bedrock_agent": "bedrock_agent", "http": "http"}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aq", description="Run AQ evaluations from your terminal or CI.")
    p.add_argument("--version", action="version", version=f"aq-sdk {__version__}")
    sub = p.add_subparsers(dest="group", required=True)
    ev = sub.add_parser("eval", help="Run evaluations.").add_subparsers(dest="command", required=True)
    run = ev.add_parser("run", help="Run a dataset against a connected agent and judge the result.",
                        description="Needs AQ_API_KEY and AQ_ENDPOINT. The key needs the scopes datasets:read, "
                                    "scorers:read, agents:read, experiments:read and experiments:write.")
    run.add_argument("--dataset", required=True, help="Dataset name or ID.")
    run.add_argument("--target", required=True, help="Name of a connected agent.")
    run.add_argument("--compare-to", help="Experiment ID, exact experiment name, or current.")
    run.add_argument("--fail-on", choices=["regression"], help="Exit 1 when the comparison finds a regression.")
    run.add_argument("--scorer", action="append", default=[], help="Scorer name or ID. Repeat for more. "
                     "Default: the scorers of the --compare-to experiment.")
    run.add_argument("--dataset-version", type=int, help="Default: the version of the --compare-to experiment, else the latest.")
    run.add_argument("--row-scope", choices=["all", "development", "held_out"], default="all",
                     help="Which dataset rows to run. Default: all. Use development for development verification.")
    run.add_argument("--repeats", type=int, help="Default: the repeats of the --compare-to experiment, else 1.")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--budget", required=True, help="Most this run may spend, in USD. Needed. AQ cuts it to its "
                     "server limit, 5 by default.")
    run.add_argument("--name", help="Name of the new experiment.")
    run.add_argument("--system-prompt", help="Override the agent's system prompt for this run.")
    run.add_argument("--system-prompt-file", help="Read the system prompt override from a file.")
    run.add_argument("--model-id", help="Override the agent's model for this run.")
    run.add_argument("--agent-endpoint", help="AgentCore endpoint name on the connected runtime, or an HTTP agent URL, for this run.")
    run.add_argument("--commit", help="Full commit SHA declared by the caller; does not attest the deployed code.")
    run.add_argument("--report-file", help="Write a Markdown evaluation summary for a pull request. Does not post it.")
    run.add_argument("--allow-errors", type=int, default=0, help="With --fail-on: rows with task errors that are still fine.")
    run.add_argument("--timeout", type=float, default=3600.0, help="Seconds to wait for the run.")
    run.add_argument("--poll", type=float, default=3.0, help="Seconds between status checks.")
    run.add_argument("--endpoint", help="AQ address. Default: AQ_ENDPOINT.")
    run.add_argument("--ui-url", help="Where people open the app, for the printed link. Default: AQ_UI_URL, then the address.")
    run.add_argument("--json", action="store_true", help="Print one JSON result instead of text.")
    return p


def _overrides(args: argparse.Namespace) -> dict[str, str]:
    if args.system_prompt and args.system_prompt_file:
        raise AQError("Use --system-prompt or --system-prompt-file, not both.")
    text = args.system_prompt
    if args.system_prompt_file:
        try:
            text = Path(args.system_prompt_file).read_text()
        except OSError as exc:
            raise AQError(f"The prompt file could not be read: {exc.strerror}.", "Check the path.") from None
    out = {}
    if text is not None:
        out["system_prompt"] = text
    if args.model_id:
        out["model_id"] = args.model_id
    return out


class _Stop(KeyboardInterrupt):
    """Raised by SIGTERM, so a stopped CI job cancels its experiment like Ctrl-C does."""


def _on_term(signum: int, frame: Any) -> None:
    raise _Stop()


def _metadata_confirmed(args: argparse.Namespace, response: dict[str, Any]) -> bool:
    target = response.get("target") or {}
    return ((args.agent_endpoint is None or (target.get("agent_endpoint") == args.agent_endpoint.strip()
                                            and target.get("endpoint_overridden") is True))
            and (args.commit is None or target.get("commit_sha") == args.commit))


def run_eval(args: argparse.Namespace, out: TextIO, err: TextIO, client: Client | None = None,
             state: dict[str, bool] | None = None) -> int:
    state = state if state is not None else {}

    def say(text: str) -> None:
        print(text, file=err if args.json else out, flush=True)

    if args.fail_on and not args.compare_to:
        raise AQError("--fail-on needs --compare-to.", "Name the experiment to compare with, or use --compare-to current.")
    overrides = _overrides(args)
    if args.commit is not None and not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", args.commit):
        raise AQError("--commit needs a full 40 or 64 character hexadecimal commit SHA.")
    client = client or Client(build(None, None, args.endpoint, args.ui_url))
    agent = client.agent(args.target)
    kind = AGENT_TARGET_KINDS.get(agent["kind"])
    if kind is None:
        raise AQError(f"The agent {agent['name']} only sends traces, so AQ cannot run it.",
                      "Pick a connected agent that AQ can run, such as an AgentCore runtime.")
    if kind == "http":
        raise AQError("API-key CLI runs require a budget, but HTTP target costs cannot be measured or capped.",
                      "Use a member run in the app without a budget for an HTTP target.")
    if args.agent_endpoint is not None and kind not in ("agentcore", "http"):
        raise AQError("--agent-endpoint needs a connected AgentCore or HTTP agent.")
    plan = resolve_plan(client, args.dataset, agent=agent["id"], compare_to=args.compare_to,
                        version=args.dataset_version, repeats=args.repeats, scorers=args.scorer)
    ds, base = plan["dataset"], plan["base"]
    if base is not None and (base.get("progress") or {}).get("row_ids") is not None:
        raise AQError("The comparison baseline uses pinned row IDs, which this CLI cannot preserve.",
                      "Use a run interface that supports the exact selected row IDs. No run was started.")
    body: dict[str, Any] = {
        "name": args.name or f"CLI run on {ds['name']} ({agent['name']})", "agent_id": agent["id"],
        "dataset_id": ds["id"], "dataset_version": plan["version"], "row_scope": args.row_scope,
        "target": {"kind": kind, "agent_id": agent["id"], "overrides": overrides},
        "scorer_ids": [s["id"] for s in plan["scorers"]], "repeats": plan["repeats"], "concurrency": args.concurrency}
    body["budget_usd"] = args.budget
    body["source"] = "sdk"
    if args.agent_endpoint is not None:
        body["target"]["agent_endpoint"] = args.agent_endpoint
    if args.commit is not None:
        body["target"]["commit_sha"] = args.commit
    if args.agent_endpoint is not None or args.commit is not None:
        estimate = client.estimate(body)
        if not _metadata_confirmed(args, estimate):
            raise AQError("The API estimate did not confirm the requested endpoint and commit. No run was started.",
                          "Use an API version that supports per-run endpoints and commit metadata.")
    say(f"Starting {agent['name']} on {ds['name']} version {plan['version']} "
        f"({ds['row_count']} rows in dataset, scope {args.row_scope}, "
        f"{plan['repeats']} attempt{'s' if plan['repeats'] != 1 else ''} each"
        f"{', ' + ' and '.join(k.replace('_', ' ') for k in overrides) + ' overridden' if overrides else ', no overrides'}).")
    started = client.start(body)
    state["started"] = True
    if not _metadata_confirmed(args, started):
        client._cancel_quietly(started["id"])
        raise AQError("The started experiment did not confirm the requested endpoint and commit. "
                      "Cancellation was requested. Calls may already have occurred.",
                      "Inspect the retained experiment before starting another run.")
    say(f"Experiment {started['id']} is running in AQ.")
    code, reason, exp, cmp, cmp_error = report.UNJUDGED, None, started, None, None
    previous = signal.signal(signal.SIGTERM, _on_term) if threading.current_thread() is threading.main_thread() else None
    try:
        exp = client.wait(started["id"], poll_s=args.poll, timeout_s=args.timeout, on_progress=lambda e: say(
            f"  {e['progress'].get('finished', 0)} of {e['progress'].get('total', '?')} calls done"))
    except KeyboardInterrupt:
        say("Stopped. The experiment was cancelled in AQ.")
        return report.UNJUDGED
    except AQError as err:
        if err.code != "wait_timeout":
            raise
        reason = str(err)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    if exp["status"] in ("completed", "stopped_budget", "failed", "cancelled", "interrupted"):
        if base is not None and exp["status"] == "completed":
            try:
                cmp = client.compare(base["id"], exp["id"])
            except AQError as err:
                cmp_error = str(err)
        code, reason = report.outcome(exp, cmp, cmp_error, compare_wanted=base is not None, fail_on=args.fail_on,
                                      allow_errors=args.allow_errors)
    url = report.link(client.config, exp, base["id"] if base else None)
    result = {"exit_code": code, "reason": reason, "verdict": cmp["verdict"] if cmp else None,
              "experiment": exp, "compare": cmp, "link": url}
    if args.report_file:
        try:
            Path(args.report_file).write_text(report.pr_summary(result), encoding="utf-8")
        except OSError as exc:
            raise AQError(f"The report could not be written: {exc.strerror}.", "Check the report path.") from None
    if args.json:
        print(json.dumps(result, indent=2), file=out)
    else:
        for line in report.summary_lines(exp):
            say(line)
        if cmp is not None:
            for line in report.compare_lines(cmp):
                say(line)
        elif cmp_error:
            say(f"The comparison could not be made. {cmp_error}")
        if url:
            say(f"Open the {'comparison' if base else 'experiment'}: {url}")
        if reason and code != report.OK:
            say(f"Result: {'fail' if code == report.REGRESSION else 'not judged'}. {reason}")
        elif args.fail_on:
            say("Result: pass. No regression.")
    return code


def main(argv: list[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None,
         client: Client | None = None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    args = parser().parse_args(argv)
    state: dict[str, bool] = {}
    # Only a regression exits 1. A problem before the experiment starts is 2. After it starts, the run is unjudged: 3.
    try:
        return run_eval(args, out, err, client, state)
    except AQError as exc:
        print(f"Error: {exc}", file=err)
        return report.UNJUDGED if state.get("started") else report.USAGE
    except KeyboardInterrupt:
        print("Stopped.", file=err)
        return report.UNJUDGED if state.get("started") else report.USAGE
    except Exception as exc:
        print(f"Error: aq stopped on an unexpected {type(exc).__name__}: {str(exc)[:200]}. Run it again. "
              "If it keeps happening, report it to the AQ team.", file=err)
        return report.UNJUDGED if state.get("started") else report.USAGE


if __name__ == "__main__":
    sys.exit(main())

# aq-sdk

Send traces to AQ and run evaluations from your code and from your own CI.
It uses the datasets and scorers you saved in the AQ app, so a run from code
shows up next to the runs you start in the app and compares with them.

Python 3.11 or newer.

```bash
pip install ./packages/aq-sdk
```

## Create a key

In AQ open Settings, API keys, and create a key with these scopes.

| Scope | What it allows |
|---|---|
| `traces:write` | Send traces (`init`) |
| `datasets:read` | Read datasets and their rows |
| `scorers:read` | Read scorers |
| `agents:read` | List connected agents, to pick a target. The list shows each agent's resource ARN, so give this scope only to keys that need it |
| `experiments:read` | Read experiments and comparisons |
| `experiments:write` | Start an experiment, upload results, cancel a run |

A key works on those routes only. Over plain `http://` the SDK refuses to send the key, except to localhost. It is not a member sign-in. It cannot change
datasets, scorers, agents, connections or settings, and it cannot create keys.
Experiments it starts are always candidates marked "SDK", and a key can cancel
only the experiments it started. Only a member marks the current version in
the app. A key starts an experiment only on a connected agent, with that
agent's own connection and settings, and always with a budget. The server cuts
the budget to its limit (5 USD by default). A key cannot run a bare model or an
address of its own. Each key may send 120 requests a minute unless you set
another limit on it.

Set the address and the key in the environment, or pass them to `init`.

```bash
export AQ_ENDPOINT=https://aq.example.com
export AQ_API_KEY=aq_live_...
export AQ_UI_URL=https://aq.example.com   # only if the app is on another address than the API
```

## Evaluate a Strands agent in ten lines

This runs your agent on every row of a dataset from AQ and scores the answers
with a scorer from AQ. It compares the result with the current version and
fails when it is worse. Supply your own existing agent and a dataset saved in
your workspace. `my_agent` below is your module, not a module in this distribution.

```python
import os
import aq_sdk as aq
from my_agent import invoke  # your existing Strands agent

aq.init("my-agent", os.environ["AQ_API_KEY"], os.environ["AQ_ENDPOINT"])
aq.Eval(dataset="weather", task=lambda row: invoke(row)["result"],
        scorers=["States the true value"], compare_to="current", fail_on="regression").run()
```

What happens:

1. `init` sends the agent's OpenTelemetry spans to AQ. Strands spans are
   included. `project` is the OpenTelemetry service name. AQ shows the traces
   on the connected agent whose service name is the same. Set the service name
   when you connect the agent. Other traces are stored without an agent.
2. `Eval` reads the dataset (the version of the `compare_to` experiment, so the
   rows pair), calls `task` once per row with the row's input, and uploads
   the outputs.
3. AQ scores the outputs with the saved scorers. This is the same code the
   app uses, so the numbers are the same kind of numbers.
4. The result is stored as an experiment with the source "SDK". It prints the
   scores, the comparison and a link to the comparison in the app.
5. With `fail_on="regression"`, a regression raises `aq_sdk.RegressionError`.
   A run that cannot be judged raises `aq_sdk.AQError`. It never passes.

A task that raises is stored as an error for that row. It is not scored and it
is not a zero. AQ did not run the task, so the cost of the run shows as unknown.

Things to know:

- `task` gets the row's input as saved in AQ and may be sync or async. Return a
  string or JSON. Anything else is sent as its text.
- Scorers that read the trace ("tool was called") need AQ to run the agent, so
  they are not available here. Use the CLI below for those. Judge scorers
  work and bill the AWS connection set on the judge.
- A judge scorer on an upload has a budget, 5 USD by default (`budget_usd=`),
  checked against an estimate before anything is stored. An upload is limited
  to 20 MB and its outputs are cut at 50,000 characters. Two uploads at most
  are scored at once per workspace.
- A scorer that checks latency is not available here, because AQ cannot
  measure your own code. The latency you send is shown but never scored.
- `Eval` takes `repeats`, `concurrency`, `dataset_version`, `agent`, `name`,
  `budget_usd` and `allow_errors`. With `compare_to`, the repeats and the scorers default to
  the ones of that experiment.

## Traces and custom spans

```python
aq.init(project="my-agent", api_key="aq_live_...", endpoint="https://aq.example.com")

@aq.trace                      # or @aq.trace(name="search", index="faq")
def plan(question): ...

with aq.span("lookup", tier="gold") as s:
    s.set_attribute("rows", 12)
```

If your program already set up an OpenTelemetry tracer provider, `init` adds
the AQ exporter to it. Call `aq.flush()` at the end of a short script.

## Read what you set up in the app

```python
aq.list_datasets()
ds = aq.get_dataset("weather")      # rows: id, input, expected
aq.list_scorers()
aq.get_scorer("States the true value")
```

## CLI

The CLI starts an experiment on a connected agent that supports its required
budget. AQ runs the agent in your AWS account, with the overrides you give, and
scores it. HTTP targets are unavailable through this API-key CLI: keys require
a budget, while HTTP costs cannot be measured or capped. A signed-in member can
run an HTTP target in the app without a budget.

```bash
aq eval run --dataset weather --target my-agent \
  --compare-to current --fail-on regression --budget 2
```

`--target` is the name of a connected agent that AQ can run. Options:

| Option | Meaning |
|---|---|
| `--compare-to` | An experiment ID, an exact experiment name, or `current` |
| `--fail-on regression` | Exit 1 when the comparison finds a regression |
| `--scorer NAME` | Repeat for more. Default: the scorers of the `--compare-to` experiment |
| `--system-prompt TEXT` or `--system-prompt-file PATH` | Override the agent's prompt for this run |
| `--model-id ID` | Override the agent's model for this run |
| `--budget USD` | Needed. Most the run may spend. Cut to the server limit |
| `--dataset-version`, `--repeats`, `--concurrency`, `--name` | As in the app |
| `--allow-errors N` | With `--fail-on`, rows with errors that are still fine |
| `--json` | Print one JSON result |

Overrides only work on agents marked as supporting overrides and non-production.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Fine. With `--fail-on regression`, there is no regression |
| 1 | `--fail-on regression` and the comparison found a regression |
| 2 | The command, the key or the connection is wrong, or an unexpected error came before the run started. Nothing was judged |
| 3 | The run cannot be judged. It did not finish, rows had errors, requested scoring is incomplete or errored, or requested scorers could not all be compared |

Ctrl-C and SIGTERM cancel the experiment in AQ. Only a regression exits 1.
The CLI waits for pending scores after target calls finish, within the same
`--timeout`. A deadline while target calls are running requests cancellation.
A deadline while only scores are pending reports an incomplete result without
cancelling or restarting the completed target run. With `--fail-on regression`,
every requested scorer must have settled results and a comparison. Missing
settlement fields from an older server cannot establish a passing gate.

Use repeats of 3 or more in CI. A baseline of 20 rows with one attempt each is
noisy: the same agent scored 40% and 60% on two runs in our own check, and a
clearly worse prompt can still read as "No clear difference". Run the current
version in the app with 3 repeats. `compare_to` and `--compare-to` then use 3
repeats too.

Wiring this into CI is your choice. Any CI that can run a command and read its
exit code works. Put the key in the CI secret store, install the package, run
the command above, and fail the job on a non-zero exit.

## Evaluate a branch endpoint

`--agent-endpoint` selects an AgentCore endpoint on the connected runtime for
one run. The name contains 1–48 letters, digits or underscores and starts with
a letter. The application API also accepts an HTTP URL on the same origin
(scheme, host and port), but the API-key CLI cannot run HTTP targets under the
current budget rules. Pull-request evaluation targets AgentCore. HTTP support
is deferred.
Connect another origin as a separate agent. Saved credentials are not forwarded
there. `--endpoint` still means the AQ API address. These options do
not deploy an agent or change its connected defaults. Deploy your branch first,
then supply the endpoint that serves the intended commit.

```text
aq eval run --dataset DATASET_ID --target AGENT_ID --agent-endpoint pr_42 --commit FULL_COMMIT_SHA --compare-to current --fail-on regression --budget 1 --report-file evaluation.md --json
```

Supply `AQ_API_KEY` from your repository's secret store and `AQ_ENDPOINT` as the
API address. Do not print the key or include it in command arguments, artifacts
or comments. The key needs `datasets:read`, `scorers:read`, `agents:read`,
`experiments:read` and `experiments:write`. Existing server budget limits apply.

`--commit` accepts a full 40- or 64-character hexadecimal commit SHA and records
the caller's declaration. It does not verify which code the endpoint serves.
The stored target keeps `agent_endpoint` as the requested override (null when
none was requested), `effective_agent_endpoint` as the resolved endpoint,
`endpoint_overridden` and `commit_sha`. An explicit endpoint change cannot be
marked as the connected agent's current version. A default endpoint retained
for execution does not become an explicit change when a child target is built.

Before submission the CLI checks the sanitized target snapshot returned by
`POST /v1/experiments/estimate`. This read-only route uses `experiments:write`
and applies the same connected-target restrictions as start. An old API that
cannot confirm the endpoint or commit is refused before a run starts. A
mismatch in the started experiment requests cancellation and reports an
unjudged result. Cancellation does not prove that no call occurred.

`--row-scope development` limits development verification to development rows.
The existing default remains `all`. `held_out` selects held-out rows. Comparison
baselines with pinned row IDs are refused because this CLI cannot preserve that
subset. It never substitutes the full dataset for that pinned selection.

### Reports and publishing

`--report-file` writes aggregate Markdown with the comparison words and counts,
the exact `progress.planning.text` when retained, and the app link. It excludes
row inputs, outputs and target credentials. Credential-bearing or invalid app
links are omitted. A configured UI URL with a query or fragment is not forwarded
into a public link, while the comparison route's hash parameters remain supported.
With `--json`, standard output is one result containing `exit_code`, `reason`,
`verdict`, `experiment`, `compare` and `link`. Progress goes to standard error.
“No clear difference” is an ordinary verdict. It passes the regression gate
only when all requested scorers have settled, comparable results.

Preserve the evaluation's exit code when publishing a report: 0 means no
regression under the requested gate, 1 means a regression, 2 means failure
before the experiment started, and 3 means unjudged or incomplete. Posting a
comment does not turn a nonzero exit into a successful check. An early error
or interruption may produce no report file. Report that failure without
fabricating row counts, a verdict or an experiment link.

`aq_sdk.publish_pr_comment` publishes the aggregate summary to GitHub using a
separate repository token. Pass the CLI JSON result and repository, PR number
and numeric publisher identity from trusted integration context:

```python
from aq_sdk import publish_pr_comment

publication = publish_pr_comment(
    result, repository=repository, pull_request=pr_number,
    check_key="agent.dataset", github_token=repository_token,
    publisher_id=publisher_id,
)
evaluation_exit_code = publication.evaluation_exit_code
```

The publisher ID identifies the account or bot that authors comments with that
repository token. It is **not** the workflow trigger's `GITHUB_ACTOR_ID`, a value
from a PR artifact, or an identity guessed from existing comments. Installation
tokens need not support `GET /user`. This SDK does not call it. The integration
must supply its authenticated publisher identity and serialize publishers for
each repository/PR/check key. Use a stable key per agent/dataset, independent of
commit or run number. Keys accept 1–80 letters, digits, dots, underscores or
hyphens. Never expose either repository or evaluation credentials to untrusted
fork code.

The publisher confirms the PR's base repository and reads up to 100 pages of
100 comments. Incomplete, malformed or exhausted pagination refuses a write.
It selects the oldest exact first-line marker by the trusted publisher, checks
that comment again before updating it, and never edits another author's comment.
If the rendered body is unchanged, it performs no write. It uses only
`api.github.com`, does not follow redirects, and never sends the evaluation key.
Moved repositories must use their current trusted name. These operations use
GitHub's [issue-comment API](https://docs.github.com/en/rest/issues/comments)
and [pagination contract](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api).

`publication.status` is `created`, `updated`, `unchanged`, `reconciled`, `failed`
or `uncertain`. `comment_id` is present when known. Errors contain no response
body, token or raw report. The evaluation exit code remains separate: a comment
publication cannot turn a failed evaluation into a pass. The integration must
report publication failure separately and must not publish an invented success
when the CLI produced no result.

A timeout, server error or unconfirmed write response triggers read-only
reconciliation for the exact body. There is no automatic second POST or PATCH.
If the result is still `uncertain`, retain the same report and retry only with
`reconcile_only=True`. This mode never writes. Do not call ordinary publication
again while a prior write is uncertain. A matching marker with a different body
does not confirm the earlier write. No automatic rate-limit retry is made.

The [evaluation Action](../../.github/actions/evaluate/README.md) and complete
GitHub/GitLab templates reuse these SDK interfaces. They keep raw results private,
install the SDK from reviewed source, separate credentials from branch-code
execution, and provide read-only reconciliation. This public distribution includes
the Action and its matching SDK. The customer integration still needs validation
of its trusted context, permissions, serialized execution and credential isolation. The SDK tests cover
create/update/reconciliation, failed evaluations, missing reports, all verdicts
and pagination with mocked GitHub HTTP. They do not establish real repository
publication or live agent execution. HTTP PR checks are deferred, with both
existing spending controls unchanged.

## Tests

```bash
pip install -e "./packages/aq-sdk[test]"
python -m pytest packages/aq-sdk/tests
```

The tests use a fake AQ in memory. Nothing in them counts as evidence that the
real service works.


## Named task acceptance

```text
aq eval run --dataset policy-checks --target connected-agent --compare-to current \
  --task policy-task --fail-on regression --budget 1 --report-file evaluation.md
```

`--task` selects an existing task by name or ID for the selected agent and
dataset. It must have a signed bar before submission. The client reads the
server's verdict for the retained experiment; it does not derive a second
acceptance threshold, approve a release or change the bar. The existing
`experiments:read` scope covers task resolution and verdict reads.

A task below its bar exits 1. Missing, incomplete, unavailable or conflicting
evidence exits 3. A passing verdict retains the signed bar version, counts,
server-provided bounds and required-slice status in JSON, console and Markdown
reports. Unknown bounds stay unknown. A development run cannot satisfy a
held-out acceptance requirement. With `--fail-on regression`, both gates must
pass; without a comparison, select scorers explicitly with `--scorer`.
Comparisons without scored pairs cannot pass merely because row IDs match.

The compatibility source is the reviewed task consumer at application revision
`14f18254338f9a27cfc340b791d91c3372ae6155`, checked against API revision
`3cd7deecf72100991155e956f150fbdbb6179ffe`. Required API reads are
`GET /v1/agent-tasks?agent_id=...` and
`GET /v1/experiments/{id}/verdict?task_id=...`; an API without them leaves the
result unresolved. Existing explicit-baseline resolution and owned-check
publication are retained. Future source updates should rerun the task-gate,
comparison-report and publication tests against their intended API contract.

The reusable Action and customer CI examples are separate release integration.
This CLI update does not expose a new Action input, move their reviewed pin,
publish a registry version, define a license or demonstrate customer CI access.

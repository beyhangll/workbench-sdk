# Evaluate an AgentCore pull request

This Action runs the existing `aq eval run` command once with an explicit
AgentCore endpoint, tested commit, development row scope, immutable baseline comparison,
regression gate and required budget. Optional `task` accepts an existing task UUID,
uses held-out row scope and adds the SDK's server-provided task acceptance gate.
Without it, the development comparison remains unchanged. It publishes one aggregate comment through
the SDK publisher and a separately verified owned check. It does not deploy the customer's agent or change its defaults.
HTTP targets are not supported by this interface. Same-repository PRs use plain
`pull_request` by default; cross-repository `pull_request_target` is an explicit
`allow-forks: true` opt-in, not the default trust policy.

Pin this Action to a full reviewed commit that contains it. The installer refuses
mutable branches or tags. It installs `packages/aq-sdk` from that same Action
checkout into an isolated Python 3.12 environment. It does not install a floating
package release or read SDK code from the customer's checkout.

This public repository contains the evaluation Action and its matching SDK.
Customers can fetch these files without access to the application source.
Use the immutable implementation commit shown in the examples. Customer
credentials, protected CI configuration and deployment remain separate setup.

## Trusted workflow and credentials

Use the [complete GitHub template](../../../docs/ci/github-agent-evaluation.yml).
Copy it into the customer's base branch and replace `REVIEWED_ACTION_COMMIT`.
The template uses `pull_request`, restricts both jobs to same-repository PRs,
and does not check out or execute PR code on either credential-bearing runner. The customer-owned CodeBuild project builds and
deploys the requested branch remotely, with its own isolated permissions and no
evaluation key or publisher token. The evaluation runs in a different fresh runner.
Cross-repository PRs are refused by the default template and Action policy.
A repository that is itself a fork can still have a same-repository PR; that
repository's `fork` metadata does not make the PR cross-repository.

**Same-repository is not proof that workflow code is immutable.** A plain
`pull_request` workflow can use the PR's merge-branch workflow definition. Use
trusted contributors and owner review of workflow edits, immutable reviewed
Action pins, and appropriately protected credential jobs/environments. An
in-Action context guard cannot protect secrets already exposed by an earlier
edited workflow step. For a fictional sample, keep the job same-repository-only with no PR checkout
or PR execution, restrict its key to the approved fictional workspace, and use
explicit per-run spending controls. Record the controls' actual enforcement and
limitations; a working cost estimate is not a billing guarantee or proof of a
per-key monetary cap. This Action creates none of those protections.

The optional fork path requires `pull_request_target` and `allow-forks: true`
in owner-controlled workflow configuration. It is accepted only for a known
cross-repository head. It is not permission to run untrusted fork code with
credentials: no fork checkout, downloaded executable artifact or PR-controlled
command belongs on that runner. Review GitHub's event policy, permissions,
protected environment and isolated deployment handoff before enabling it.
The supplied default template does not provision a fork build or enable that
mode. Opt-in grants no extra key, spending, endpoint or repository permission.
Both modes freeze head/base SHAs, head/base repository identity and their known
fork metadata, then recheck the exact authenticated GitHub context before
dispatch and publication. The tested source is `pull_request.head.sha`, not the
`GITHUB_SHA` merge commit used by a plain PR workflow.

The customer must already own an approved branch-build project. Its deployment
must preserve the connected runtime's default and expose a dedicated test
endpoint. It exports `AGENT_ENDPOINT` and `DEPLOYED_COMMIT` only after its own
source, rollout and rollback checks. The workflow verifies those fields before
evaluating. They remain pipeline declarations, not model or source attestation
provided by the evaluation service. Cleanup of test endpoints and build artifacts
belongs to that customer pipeline. Do not add evaluation credentials to it.

The protected base-build handoff must supply `baseline-experiment`, `base-commit`
and `base-endpoint`. Use an immutable experiment UUID selected by the
owner-controlled pipeline, never a PR artifact, experiment name or `current`
label. The Action confirms the authenticated PR head and base, the baseline's
agent/dataset ownership and persisted effective AgentCore endpoint, complete
attempt/scorer counts, and the selected row scope before dispatch. Without
`task`, the scope is `development`; with a named task, both baseline and candidate
must use `held_out`. Unknown scope, pinned row IDs and an unknown or include-all
rejected-row policy are refused because this CLI cannot preserve those choices.

The scorer-only estimate readback compares current resolved scorer settings,
thresholds, trace requirements, judge routing and contextual connections against
the frozen baseline. It does not execute a target or judge. An API without this
readback refuses. The candidate reuses the baseline's dataset version, scorer IDs
and repetitions. Changes detected in retained results still refuse publication.
These controls bind saved records to pipeline declarations; they are not
source/model attestation or complete billing evidence.

The example requires protected `AQ_BASELINE_EXPERIMENT`,
`AQ_BASE_DEPLOYED_COMMIT`, `AQ_BASE_AGENT_ENDPOINT` and `AQ_CHECK_APP_ID` values.
The recorded base-build commit must match the current PR base SHA. When that SHA
changes, the owner must provide a reviewed matching base-build record; this
Action does not select or deploy a fresh baseline automatically.

Set `check-app-id` to the trusted GitHub App owning checks, separately from the
comment publisher user ID. Fully judged improvement produces `success`; fully
judged no clear difference produces `neutral`. A regression, incomplete evidence
or unconfirmed comment publication cannot produce success or neutral. GitHub can
accept a neutral required check, so unknown measurement must remain nonpassing.

Before success or neutral, the Action writes a nonpassing check and verifies its
actual App ownership from GitHub. Only that verified lifecycle can then be
completed. A failure needs at most one planned mutation; success or neutral needs
at most two. Uncertain writes are reconciled with reads, never repeated blindly.
Configure required-check ownership for that App in the customer repository.
Actual sample-PR comment/check/regression acceptance remains a separate live
integration requirement, not an outcome established by the offline tests.

Configure the two protected environments with required reviewers and trusted
base-branch access. Put the narrowly scoped build role in the deployment
environment. Put `AQ_API_KEY` only in the evaluation environment. The example
uses the built-in `GITHUB_TOKEN` with job-scoped `pull-requests: write` and
`checks: write`, rather than requiring a new GitHub App. A separately supplied
publisher token is also supported when approved and narrowly scoped. The generic
example reads API/UI addresses from protected `AQ_ENDPOINT` and `AQ_UI_URL`
secrets, because ordinary variable values can appear in Action input logs before
its runtime guard. Supply trusted repository/environment variables for the
connected agent, existing development dataset, budget, stable check
key and `AQ_COMMENT_PUBLISHER_ID`. Configure `AQ_CHECK_KEY` as a repository or
organization variable, since workflow concurrency resolves before environment
variables are available. The publisher ID must identify the author of
comments made with the configured token; for `GITHUB_TOKEN`, configure its
verified GitHub Actions bot identity and App identity separately. Never derive it from `GITHUB_ACTOR_ID`,
PR text, existing comments or a downloaded artifact. Installation tokens need
not support `GET /user`. Use only the configured identity.

The evaluation key needs `datasets:read`, `scorers:read`, `agents:read`,
`experiments:read` and `experiments:write`. The separate repository token needs
access to the intended PR, its issue comments and check-run writes for the
configured GitHub App. The examples do not grant
repository permissions, mint credentials or configure environments. Those remain
owner setup requirements. Protect the default workflow and pin third-party
Actions. Do not run earlier untrusted commands on the evaluation runner.

The workflow-level concurrency group serializes the whole deployment and
evaluation sequence for the repository/PR/check key with `cancel-in-progress:
false`. An evaluation-only job lock could let a later deployment overwrite the
endpoint while an earlier evaluation still uses it. Keep the workflow group when
using the composite Action. Shared mutable endpoints across different checks
also need one common deployment/evaluation group or genuinely isolated endpoints.
A composite Action cannot declare job-level concurrency. Different integration
workflows that publish the same check must use the same group. A local lock or a
self-declared flag does not establish cross-run serialization.

GitHub documents why executing PR code in a privileged
job is unsafe in [Securely using pull_request_target](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target).
The customer must satisfy any applicable event policy. This artifact does not
change or bypass that policy. The branch-build step uses the existing
[CodeBuild StartBuild](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_StartBuild.html)
and [BatchGetBuilds](https://docs.aws.amazon.com/cli/latest/reference/codebuild/batch-get-builds.html)
interfaces. No customer runtime or infrastructure is created here.

## Optional paired system-prompt files

`system-prompt-file` and `base-system-prompt-file` are optional local data-file
paths. Supply both or neither. Without them, the existing endpoint-only path is
unchanged. Prompt files require the connected agent to explicitly support
overrides and be non-production. Both endpoint inputs may be `DEFAULT`; a prompt
comparison does not require another AgentCore endpoint or a deployment.

The trusted caller must obtain the candidate and protected-base bytes from their
exact head and base commits, for example through authenticated GitHub tree/blob
reads. Verify repository, path, blob identity and content before writing these
files. Do not check out PR code or execute downloaded files. A local file path or
its SHA256 alone is not Git source attestation. For owner-created sample PRs,
independently verify the entire PR workflow definition before opening them:
plain `pull_request` events can evaluate that definition even without checkout.

The Action freezes both files privately before preflight and refuses missing,
changed, empty, non-UTF-8, symlinked or non-regular inputs. The maximum is 256 KiB
per file. LF bytes, including a final newline, are preserved exactly. CR and CRLF
are explicitly refused because the unchanged CLI file reader would normalize
them. No truncation or silent normalization occurs. Known evaluation or publisher
credentials in either prompt refuse the run before planning or publication.

The immutable baseline's saved `target.overrides.system_prompt` must match the
protected-base bytes exactly. Active model or other overrides and unknown override
keys are refused; known unused override keys may remain `null`. The existing
scorer-only readback remains required. A separate planning-only estimate must
confirm the candidate prompt, connected agent, endpoint and tested commit before
the CLI can submit. No target or judge runs during either preflight. Planning
confirmation describes requested configuration, not observed serving attestation.

The Action passes a private exclusive file snapshot to the existing
`--system-prompt-file` interface and uses `--concurrency=1` in this new mode.
The no-file path retains its original concurrency behavior. Original files and
the private snapshot are rechecked before the single CLI invocation. The existing
runtime override contract rejects missing or mismatched prompt confirmations;
task failures and incomplete scoring cannot become a passing result. Usage and
billing limitations remain separate and are not resolved by prompt binding.

Only SHA256 bindings derived from validated saved target override text enter the
aggregate target and publication metadata. Raw prompts remain excluded. Both the
second publication validation and read-only reconciliation check those bindings
against the frozen files and protected run/commit ownership. Historical no-file
reports remain readable; they are never upgraded with today's prompt files.
Keep the same paired files and source binding when reconciling a prompt-file run.

Known complete prompt echoes, including common encoded forms, refuse public
free-text reports, comments and checks; private CLI debug copies remove those
known values too. This is not arbitrary fragment, personal-data or secret
detection. The existing generic failure check remains the only safe refusal
fallback. Discard restricted prompt snapshots with the fresh runner under the
caller's retention policy; upload only the existing aggregate allowlist.

## Outcomes, privacy and reconciliation

The Action captures full CLI JSON and stderr privately in memory. Debug copies
in the restricted sibling directory have both known credentials removed. Neither
is printed or uploaded. The CLI report file goes to `/dev/null` until the wrapper
validates and renders the same existing SDK aggregate contract. The CLI subprocess
receives only the evaluation credential. It does not receive the repository token,
customer checkout, inherited Python path or proxy configuration.

Only `evaluation.md`, `aggregate-result.json` and `publication.json` are intended
for the aggregate artifact. The JSON keeps the existing SDK result shape but
only the fields used by its aggregate renderer and outcome gate, including
complete-attempt counters and the API's paired confidence bounds and alpha values.
Malformed or missing bounds remain unknown. Renderer
equivalence is checked before publication. Raw rows, answers, target settings and
credentials are omitted. Both known credentials are checked before any public
report is written or published. If a credential appears, publication is refused.
This known-secret check is not a general personal-data scanner. Choose appropriate
public experiment/scorer names and planning text.

`publish-report-links` defaults to `true` for existing integrations. Set it to
`false` for a public report whose API/UI addresses must stay private. The inactive
example selects this mode; other public callers with private addresses should
also opt out. Supply private address values through
protected secrets as well, so caller workflow logs do not disclose unmasked values. The validated original CLI result
and restricted private debug record keep their real link. Only the public
projection omits the top-level report link; all other rendered fields, outcome
and gate evidence must remain equivalent. It does not invent a public UI URL or
redact arbitrary error text into a passing report.

In that mode, known API/UI addresses and hostnames are checked across remaining
public JSON keys/values, Markdown, the generated comment marker and check name,
including common encoded/escaped forms. If any remain, aggregate artifacts and
comment publication are refused. The existing generic failure check is the only
permitted fallback, and even its name must pass that guard. This is a bounded
known-address check, not a general personal-data or arbitrary-obfuscation scanner.

`evaluation-exit-code` retains the CLI outcome separately from
`publication-status` and `check-publication-status`. No clear difference is normal when every required scorer
is complete and comparable. Missing, errored or incompatible evidence cannot
pass. Publication success does not rescue a regression or incomplete evaluation.
A successful evaluation with failed or uncertain comment/check publication fails the Action
with exit 4. The original evaluation exit remains 0 in its metadata.

The existing publisher attempts one create or update. It reconciles an uncertain
write read-only and never sends an automatic second write. If publication is still
uncertain, do not rerun ordinary evaluation/publication. Retain the three
aggregate files and run this Action with `reconcile-report` pointing to the exact
retained `aggregate-result.json`, the same head/base commits, baseline UUID,
repository/PR and check key, and the trusted configured publisher and App IDs.
Keep the same report-link privacy mode. This mode skips evaluation and performs
only reads to reconcile comments and checks, with the exact PR source identity
still rechecked. It calls the SDK publisher only with `reconcile_only=True`. An altered or
cross-PR report cannot
create a comment or check through that path. It remains unresolved unless its exact body
matches the trusted publisher's existing comment.

Never upload the sibling `*-private` directory or a broad runner/workspace glob.
Use a fresh runner and discard private files according to the customer's approved
retention policy after any required private investigation. The example retains
only safe aggregates for seven days. Existing older Action pins retain their old
interface; adopting this version requires the explicit base inputs and App ID
and the selected event policy. Same-repository callers must use `pull_request`. Deleting local files does not delete the
retained experiment in the evaluation service.

Use the [direct-CLI GitLab template](../../../docs/ci/gitlab-agent-evaluation.yml)
for same-project merge requests between protected branches on GitLab 18.1 or
later. The owner must configure protected-resource access and an owner-controlled
CI configuration outside the MR branch. Pin the customer deployment include to
a reviewed full SHA. Its `deploy-test-endpoint` job runs branch code remotely
without evaluation credentials. It emits a dotenv artifact with `AQ_TEST_ENDPOINT`
and `AQ_TESTED_COMMIT`. Use a dedicated `mr_<MR IID>_<pipeline ID>` endpoint for
each pipeline and export its exact `CI_COMMIT_SHA` only after deployment checks.
The example accepts detached MR pipelines, not merged-result or fork pipelines.
It verifies these declarations before evaluation. They are not source attestation.

Scope the protected masked `AQ_API_KEY` to the `agent-pr-evaluation` environment.
The customer deploy job uses a different environment and must not receive that
key. Its dotenv output contains only the endpoint and commit, never credentials
or overrides for predefined CI variables. Use disposable runners. The evaluation job waits
for that deploy artifact. It uses a fresh private working directory and isolated Python
interpreters, since `GIT_STRATEGY: none` alone does not remove stale workspace
code. It uses no MR checkout and no GitHub publisher. The CLI writes `--report-file`
inside the private directory. Only a validated credential-free aggregate is copied
to the public artifact. Nonzero evaluation exits are preserved. Do not automatically
retry an unresolved deployment or evaluation. The SDK revision must
be a reviewed full SHA from trusted configuration. The example installs from
this public repository. No application-source token is required. Do not embed
an access token in the install URL. No project setting is changed by this template. See GitLab's [protected-resource requirements](https://docs.gitlab.com/ci/pipelines/merge_request_pipelines/#control-access-to-protected-variables-and-runners)
and [custom CI configuration path](https://docs.gitlab.com/ci/pipelines/settings/#specify-a-custom-cicd-configuration-file).

The workflow templates need customer deployment/access configuration and a
reviewed Action pin before use. Offline tests validate these integration contracts.
They do not establish live model quality, real publisher permissions, deployment
or formal qualification.

## Named tasks and releases

Set `task` only for an existing task on the selected agent and dataset with the
required owner-approved bar and data access. The Action retains and validates
the exact task/agent/dataset binding, verdict, required slices and must-pass
fields. Unknown, incomplete or mismatched acceptance cannot pass. It does not
compute a second qualification policy. Aggregate projection drops task row
payloads and is checked against the same SDK renderer and outcome gate.

The [release and scheduled-run guide](../../../docs/sdk-release.md) separates
prepared workflows from actual registry/customer activation. No package release
or customer CI run is established by this source change.

## Development must-pass condition

The optional trusted job environment `AQ_CI_DEVELOPMENT_MUST_PASS=true` enables
the existing CLI's `--development-must-pass` condition. Its default is `false`;
other values are refused. It cannot select a signed task gate at the same time.
This is product Action behavior, not an edit to any workflow or deployment.

The protected baseline must have compatible frozen development membership.
One verified failed must-pass case fails the check regardless of the statistical
verdict. Incomplete or unscored cases remain unknown and cannot pass. Complete
nonempty zero-failure coverage leaves the other check controls in force.

Comments say **Development only. Not release acceptance.** They list failed and
unknown cases by one-based row number in the whole frozen dataset version.
They never include questions, answers, traces or customer-authored labels.
Development aggregate artifacts also exclude these labels and private diagnostics.
Masking refusal stays unknown. Reconciliation must retain the same trusted mode;
it cannot upgrade an old artifact into a development or signed release pass.
Existing signed-task, held-out, prompt, baseline, scorer, publisher and budget
checks are unchanged. No workflow setting or new live run is enabled by this change.

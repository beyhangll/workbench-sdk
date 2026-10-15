# Evaluate an AgentCore pull request

This Action runs the existing `aq eval run` command once with an explicit
AgentCore endpoint, tested commit, development row scope, current comparison,
regression gate and required budget. Optional `task` accepts an existing task UUID,
uses held-out row scope and adds the SDK's server-provided task acceptance gate.
Without it, the development comparison remains unchanged. It publishes one aggregate comment through
the SDK publisher. It does not deploy the customer's agent or change its defaults.
HTTP targets are not supported by this interface.

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
The template deliberately does not check out or execute PR code on either
credential-bearing runner. The customer-owned CodeBuild project builds and
deploys the requested branch remotely, with its own isolated permissions and no
evaluation key or comment token. The evaluation runs in a different fresh runner.
Forks are refused by both the workflow and the Action.

The customer must already own an approved branch-build project. Its deployment
must preserve the connected runtime's default and expose a dedicated test
endpoint. It exports `AGENT_ENDPOINT` and `DEPLOYED_COMMIT` only after its own
source, rollout and rollback checks. The workflow verifies those fields before
evaluating. They remain pipeline declarations, not model or source attestation
provided by the evaluation service. Cleanup of test endpoints and build artifacts
belongs to that customer pipeline. Do not add evaluation credentials to it.

Configure the two protected environments with required reviewers and trusted
base-branch access. Put the narrowly scoped build role in the deployment
environment. Put `AQ_API_KEY` and `AQ_COMMENT_TOKEN` only in the evaluation
environment. Supply trusted repository/environment variables for the API and UI
addresses, connected agent, existing development dataset, budget, stable check
key and `AQ_COMMENT_PUBLISHER_ID`. Configure `AQ_CHECK_KEY` as a repository or
organization variable, since workflow concurrency resolves before environment
variables are available. The publisher ID must identify the author of
comments made with the configured token. Never derive it from `GITHUB_ACTOR_ID`,
PR text, existing comments or a downloaded artifact. Installation tokens need
not support `GET /user`. Use only the configured identity.

The evaluation key needs `datasets:read`, `scorers:read`, `agents:read`,
`experiments:read` and `experiments:write`. The separate repository token needs
access to the intended PR and its issue comments. The examples do not grant
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

GitHub documents the trust boundary and why executing PR code in a privileged
job is unsafe in [Securely using pull_request_target](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target).
The customer must satisfy any applicable event policy. This artifact does not
change or bypass that policy. The branch-build step uses the existing
[CodeBuild StartBuild](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_StartBuild.html)
and [BatchGetBuilds](https://docs.aws.amazon.com/cli/latest/reference/codebuild/batch-get-builds.html)
interfaces. No customer runtime or infrastructure is created here.

## Outcomes, privacy and reconciliation

The Action captures full CLI JSON and stderr privately in memory. Debug copies
in the restricted sibling directory have both known credentials removed. Neither
is printed or uploaded. The CLI report file goes to `/dev/null` until the wrapper
validates and renders the same existing SDK aggregate contract. The CLI subprocess
receives only the evaluation credential. It does not receive the repository token,
customer checkout, inherited Python path or proxy configuration.

Only `evaluation.md`, `aggregate-result.json` and `publication.json` are intended
for the aggregate artifact. The JSON keeps the existing SDK result shape but
only the fields used by its aggregate renderer and outcome gate. Renderer
equivalence is checked before publication. Raw rows, answers, target settings and
credentials are omitted. Both known credentials are checked before any public
report is written or published. If a credential appears, publication is refused.
This known-secret check is not a general personal-data scanner. Choose appropriate
public experiment/scorer names and planning text.

`evaluation-exit-code` retains the CLI outcome separately from
`publication-status`. No clear difference is normal when every required scorer
is complete and comparable. Missing, errored or incompatible evidence cannot
pass. Publication success does not rescue a regression or incomplete evaluation.
A successful evaluation with failed or uncertain publication fails the Action
with exit 4. The original evaluation exit remains 0 in its metadata.

The existing publisher attempts one create or update. It reconciles an uncertain
write read-only and never sends an automatic second write. If publication is still
uncertain, do not rerun ordinary evaluation/publication. Retain the three
aggregate files and run this Action with `reconcile-report` pointing to the exact
retained `aggregate-result.json`, the same commit, repository/PR and check key,
and the trusted configured publisher ID. This mode skips evaluation and calls
the SDK only with `reconcile_only=True`. An altered or cross-PR report cannot
create a comment through that path. It remains unresolved unless its exact body
matches the trusted publisher's existing comment.

Never upload the sibling `*-private` directory or a broad runner/workspace glob.
Use a fresh runner and discard private files according to the customer's approved
retention policy after any required private investigation. The example retains
only safe aggregates for seven days. Deleting local files does not delete the
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

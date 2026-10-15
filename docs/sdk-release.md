# Release preparation

The release workflow builds the existing SDK from a stable `vMAJOR.MINOR.PATCH`
tag matching `packages/aq-sdk/pyproject.toml`. It runs offline contracts, builds
a wheel and source archive, checks package metadata and retains transfer hashes.
Publication uses a separate job and publishes those same artifacts. A checksum
is transfer evidence, not independent source or serving attestation.

No version, tag, license or customer registry is selected by this preparation.
The current 0.1.0 metadata has no declared license, so publication refuses.
The owner must select customer-use terms and declare the corresponding package
license before a registry release. Existing Git installation remains useful
source distribution; it does not satisfy registry adoption.

## Owner setup before publication

Protect tag creation and the `package-release` environment with the required
owner review. The workflow's environment name does not prove those protections
exist. Configure the approved registry choice as `SDK_REGISTRY_KIND=codeartifact`.
Other registry types are refused; selecting another type requires an explicit
adaptation rather than an automatic fallback to PyPI.

Put the approved region, domain, domain-owner account, repository and narrowly
scoped OIDC publish role in that environment's `SDK_REGISTRY_REGION`,
`SDK_REGISTRY_DOMAIN`, `SDK_REGISTRY_OWNER`, `SDK_REGISTRY_REPOSITORY` and
`SDK_REGISTRY_ROLE` secrets. These values are not committed or printed by the
release helper. The role needs the selected repository/package's read and
publication permissions plus the applicable authorization-token and service
bearer-token permissions. It needs no deletion, IAM management or deployment
permissions. Actual trust and resource policies remain owner configuration.

Unconfigured or invalid inputs stop before requesting AWS credentials. Existing
package versions are refused. An uncertain version lookup or upload stops the
release without a replacement attempt by the helper. Inspect the registry before
rerunning; do not delete or overwrite an existing version to force success.
The upload tool can have its own transport behavior; one helper invocation is
not a guaranteed single HTTP request.

CodeArtifact authentication follows AWS's documented
[environment-variable interface](https://docs.aws.amazon.com/codeartifact/latest/ug/python-configure-twine.html).
The short-lived registry token goes only to the upload subprocess, whose output
is captured. Evaluation keys and repository comment tokens are excluded. No
registry, role, secret, environment or tag is created by these files.

## Customer evaluation

The [scheduled example](ci/scheduled-agent-evaluation.yml) stays under `docs/ci`;
it is not an active repository workflow. Choose an approved runner through
`EVALUATION_RUNNER_LABELS`, a protected evaluation environment, an existing
isolated target, dataset and metered budget before copying/enabling it. A named
task uses approved held-out rows and the server's task verdict; it is not
permission to open a formal confirmation or reserved dataset. Configure customer
storage/runner residency and private-artifact cleanup explicitly.

The example uses one immutable reviewed SDK Git pin, because an approved registry
is still an owner input. Replace only its install step with the approved
immutable registry version after publication. It submits once, preserves the
first failure/unknown and retains only aggregate Markdown plus exit metadata as
public artifacts. It has no PR publisher credential and performs no deployment.
GitHub's [schedule semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
require default-branch workflow configuration; the example also checks that
context. Schedule timing is not guaranteed. Existing PR templates remain separate.

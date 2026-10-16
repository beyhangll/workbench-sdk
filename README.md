# Workbench SDK

Customer tracing and evaluation SDK, reusable AgentCore evaluation Action, and CI examples.

- [SDK usage and API-key scopes](https://github.com/beyhangll/workbench-sdk/blob/f2bc2bf30a6455400f88f3a3891b864934081127/packages/aq-sdk/README.md)
- [Evaluation Action and trusted integration](https://github.com/beyhangll/workbench-sdk/blob/f2bc2bf30a6455400f88f3a3891b864934081127/.github/actions/evaluate/README.md)
- [GitHub customer workflow](https://github.com/beyhangll/workbench-sdk/blob/f2bc2bf30a6455400f88f3a3891b864934081127/docs/ci/github-agent-evaluation.yml)
- [Older GitLab direct-CLI example](docs/ci/gitlab-agent-evaluation.yml)

Install from a checkout with `python -m pip install ./packages/aq-sdk`, or use
the reviewed combined SDK/Action source:

```bash
python -m pip install "git+https://github.com/beyhangll/workbench-sdk.git@f2bc2bf30a6455400f88f3a3891b864934081127#subdirectory=packages/aq-sdk"
```
For the GitHub template in that pinned checkout, replace `REVIEWED_ACTION_COMMIT` with
`f2bc2bf30a6455400f88f3a3891b864934081127`. The Action installs the matching
SDK from that same checkout. The older GitLab direct-CLI example retains its
earlier SDK pin and `current` baseline selector; it does not implement the
GitHub Action's protected base-build and owned-check contract.

The SDK requires Python 3.11 or newer. The Action uses Python 3.12 and requires
a full reviewed commit SHA. CI evaluation targets connected AgentCore endpoints
and keeps the required metered budget. HTTP PR evaluation remains unsupported.

The examples require the customer's existing isolated deployment, evaluation
service, authorized dataset, protected credentials and trusted CI configuration.
This repository contains no application runtime or automatic evaluation workflow.

## License

Not yet declared. This distribution does not add a license.

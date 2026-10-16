# Workbench SDK

Customer tracing and evaluation SDK, reusable AgentCore evaluation Action, and CI examples.

- [SDK usage and API-key scopes](https://github.com/beyhangll/workbench-sdk/blob/f4c148508679b475212153577312f50cac19b19b/packages/aq-sdk/README.md)
- [Evaluation Action and trusted integration](https://github.com/beyhangll/workbench-sdk/blob/f4c148508679b475212153577312f50cac19b19b/.github/actions/evaluate/README.md)
- [GitHub customer workflow](https://github.com/beyhangll/workbench-sdk/blob/f4c148508679b475212153577312f50cac19b19b/docs/ci/github-agent-evaluation.yml)
- [Older GitLab direct-CLI example](docs/ci/gitlab-agent-evaluation.yml)

Install from a checkout with `python -m pip install ./packages/aq-sdk`, or use
the reviewed combined SDK/Action source:

```bash
python -m pip install "git+https://github.com/beyhangll/workbench-sdk.git@f4c148508679b475212153577312f50cac19b19b#subdirectory=packages/aq-sdk"
```
For the GitHub template in that pinned checkout, replace `REVIEWED_ACTION_COMMIT` with
`f4c148508679b475212153577312f50cac19b19b`. The Action installs the matching
SDK from that same checkout. The older GitLab direct-CLI example retains its
earlier SDK pin and `current` baseline selector; it does not implement the
GitHub Action's protected base-build and owned-check contract.

The SDK requires Python 3.11 or newer. The Action uses Python 3.12 and requires
a full reviewed commit SHA. CI evaluation targets connected AgentCore endpoints
and keeps the required metered budget. HTTP PR evaluation remains unsupported.
Same-repository PRs use `pull_request`; cross-repository `pull_request_target`
requires an explicit fork opt-in. For private API/UI addresses, use protected
secrets and set `publish-report-links: false`. See the Action's trust and privacy
limits before supplying credentials.

The examples require the customer's existing isolated deployment, evaluation
service, authorized dataset, protected credentials and trusted CI configuration.
This repository contains no application runtime or automatic evaluation workflow.

## License

Not yet declared. This distribution does not add a license.

# Workbench SDK

Customer tracing and evaluation SDK, reusable AgentCore evaluation Action, and CI examples.

- [SDK usage and API-key scopes](packages/aq-sdk/README.md)
- [Evaluation Action and trusted integration](.github/actions/evaluate/README.md)
- [GitHub customer workflow](docs/ci/github-agent-evaluation.yml)
- [GitLab customer pipeline](docs/ci/gitlab-agent-evaluation.yml)

Install from a checkout with `python -m pip install ./packages/aq-sdk`, or use
the reviewed implementation pinned by the examples:

```bash
python -m pip install "git+https://github.com/beyhangll/workbench-sdk.git@7e35c68ab6d13c225da4e35f289c754439c60ff7#subdirectory=packages/aq-sdk"
```
The SDK requires Python 3.11 or newer. The Action uses Python 3.12 and requires
a full reviewed commit SHA. CI evaluation targets connected AgentCore endpoints
and keeps the required metered budget. HTTP PR evaluation remains unsupported.

The examples require the customer's existing isolated deployment, evaluation
service, authorized dataset, protected credentials and trusted CI configuration.
This repository contains no application runtime or active run workflows.

## License

Not yet declared. This distribution does not add a license.

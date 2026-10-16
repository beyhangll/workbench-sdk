# Workbench SDK

Customer tracing and evaluation SDK, reusable AgentCore evaluation Action, and CI examples.

- [SDK usage and API-key scopes](https://github.com/beyhangll/workbench-sdk/blob/61150d4dfa291f394eb9323dd1e92d86d6ce9ea2/packages/aq-sdk/README.md)
- [Evaluation Action and trusted integration](https://github.com/beyhangll/workbench-sdk/blob/61150d4dfa291f394eb9323dd1e92d86d6ce9ea2/.github/actions/evaluate/README.md)
- [GitHub customer workflow](https://github.com/beyhangll/workbench-sdk/blob/61150d4dfa291f394eb9323dd1e92d86d6ce9ea2/docs/ci/github-agent-evaluation.yml)
- [Older GitLab direct-CLI example](docs/ci/gitlab-agent-evaluation.yml)

Install from a checkout with `python -m pip install ./packages/aq-sdk`, or use
the reviewed combined SDK/Action source:

```bash
python -m pip install "git+https://github.com/beyhangll/workbench-sdk.git@61150d4dfa291f394eb9323dd1e92d86d6ce9ea2#subdirectory=packages/aq-sdk"
```
For the GitHub template in that pinned checkout, replace `REVIEWED_ACTION_COMMIT` with
`61150d4dfa291f394eb9323dd1e92d86d6ce9ea2`. The Action installs the matching
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

## Prompt-file checks and retained sample

The reviewed source above is integrated into `develop`. It supports paired
`system-prompt-file` and `base-system-prompt-file` inputs through the existing
non-production override contract. Both files are validated and bound to the
persisted experiments. They do not change the deployed agent's default prompt.
This Git source pin is not a claim that `main`, a release tag or a package registry
contains the same code.

The [public sample](https://github.com/beyhangl/workbench-pr-check-sample) completed a separately approved 24-row round on 16 October 2026 with a genuine new baseline and unchanged prompt variants, scorers and references:

| Change | Leading-total / contains passes | Owned evaluation check |
| --- | --- | --- |
| Protected baseline | 0/24 /24/24 | Protected control |
| [Harmless prompt](https://github.com/beyhangl/workbench-pr-check-sample/pull/6) | 0/24 /24/24 | [Neutral](https://github.com/beyhangl/workbench-pr-check-sample/runs/117163400924) |
| [Wrong-total prompt](https://github.com/beyhangl/workbench-pr-check-sample/pull/7) | 0/24 /16/24 | [Failure](https://github.com/beyhangl/workbench-pr-check-sample/runs/117164740582) |

Both checks published real comments. The harmless workflow exited 0 without regression. The wrong-total workflow exited 1: contains had 8 paired losses and 0 gains, with an upper confidence bound about -0.01383. Leading-total had no baseline passes and supplied no regression evidence. No scorer, threshold or reference was weakened. A successful workflow or neutral check is not a qualification pass.

All 72 target requests completed serially without reported execution errors, with 72 expected prompt-digest acknowledgements. No judge, title, reflection, replacement or automatic rerun was submitted. Reported token cost was $0.025605 for this round, or$0.034618 including the earlier 24 requests. Runtime charges, hidden usage and actual billing remain unknown. The $0.50 per-experiment metering and $1 observed-cost stop are not complete-billing guarantees.

This is development evidence for the published SDK/Action contract. It does not establish release acceptance, complete accounting, independently attested deployment/model identity or general task quality. The existing zero-pass baseline limitation remains explicit. Separate development must-pass behavior is a product follow-up.

<details>
<summary>Original eight-row diagnostic, preserved without relabelling</summary>

The following text records the original round and its then-current missing failing-check result. The later round above supersedes that delivery status, without changing the original outcomes.

The [public sample](https://github.com/beyhangl/workbench-pr-check-sample)
retains two actual DEFAULT-route checks from 16 October 2026:

| Change | Target execution | Owned evaluation check |
| --- | --- | --- |
| [Harmless prompt change](https://github.com/beyhangl/workbench-pr-check-sample/pull/3) | Eight responses passed both deterministic graders | [Neutral](https://github.com/beyhangl/workbench-pr-check-sample/runs/117088860074) |
| [Deliberate wrong-total instruction](https://github.com/beyhangl/workbench-pr-check-sample/pull/4) | Four of eight numeric checks and six of eight contains checks passed | [Neutral](https://github.com/beyhangl/workbench-pr-check-sample/runs/117089706643) |

Both workflows completed and published comments. Neither comparison established
a clear difference against the imperfect eight-row baseline. A successful job
or exit 0 is not a statistical improvement or a qualification pass. The sample
has **not** demonstrated a clear regression producing a failing evaluation check.
Original scores, graders and paired bounds were retained without reruns.

The baseline and two candidates used 24 serial target requests in total, with no
model-judge, title or reflection calls. Their reported token-cost subtotal was
$0.009013. Runtime charges, hidden usage and actual billing remain unknown.
The $0.50 per-experiment metered budget was not a complete billing cap.

</details>

## License

Not yet declared. This distribution does not add a license.

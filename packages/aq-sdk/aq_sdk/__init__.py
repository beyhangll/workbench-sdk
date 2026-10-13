"""AQ Python SDK: send traces, run evaluations from code, and read what you set up in the app.

    import aq_sdk as aq

    aq.init(project="my-agent", api_key="aq_live_...", endpoint="https://aq.example.com")
    aq.Eval(dataset="questions", task=my_agent, scorers=["Correct"], compare_to="current").run()
"""
from __future__ import annotations

__version__ = "0.1.0"

from .client import Client  # noqa: E402
from .config import Config, build, get_config, set_config  # noqa: E402
from .errors import AQError, RegressionError  # noqa: E402
from .evals import Eval, EvalResult  # noqa: E402
from .publisher import PublicationResult, publish_pr_comment  # noqa: E402
from .tracing import flush, setup, span, trace  # noqa: E402

__all__ = [
    "AQError", "Client", "Config", "Eval", "EvalResult", "RegressionError", "__version__", "flush", "get_dataset",
    "get_scorer", "init", "list_datasets", "list_scorers", "PublicationResult", "publish_pr_comment", "span", "trace",
]


def init(project: str, api_key: str | None = None, endpoint: str | None = None, *, ui_url: str | None = None,
         traces: bool = True) -> Config:
    """Set the project, key and address. Starts sending OpenTelemetry spans to AQ unless ``traces`` is False.

    ``project`` is the OpenTelemetry service name. AQ shows the traces on the connected agent with the same service name.
    The key falls back to AQ_API_KEY, the address to AQ_ENDPOINT, and ``ui_url`` (where people open the app)
    to AQ_UI_URL and then to the address.
    """
    config = build(project, api_key, endpoint, ui_url)
    set_config(config)
    if traces:
        setup(config)
    return config


def list_datasets() -> list[dict]:
    """The datasets saved in AQ."""
    return _client().datasets()


def get_dataset(name: str, version: int | None = None) -> dict:
    """A dataset by name or ID with its rows, at ``version`` or at its current version.

    Returns the dataset record plus ``rows``, each row with ``id``, ``input`` and ``expected``.
    """
    c = _client()
    ds = c.dataset(name)
    number = version or ds["current_version"]
    return {**ds, "version": number, "rows": c.rows(ds["id"], number)}


def list_scorers() -> list[dict]:
    """The scorers saved in AQ."""
    return _client().scorers()


def get_scorer(name: str) -> dict:
    """A scorer by name or ID."""
    return _client().scorer(name)


def _client() -> Client:
    return Client()

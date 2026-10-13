"""Where the SDK finds the AQ address and key: ``init`` arguments first, then the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import AQError

ENV_KEY = "AQ_API_KEY"
ENV_ENDPOINT = "AQ_ENDPOINT"
ENV_UI = "AQ_UI_URL"
ENV_PROJECT = "AQ_PROJECT"


@dataclass(frozen=True)
class Config:
    project: str | None
    api_key: str = field(repr=False)
    endpoint: str
    ui_url: str


_current: Config | None = None


def build(project: str | None = None, api_key: str | None = None, endpoint: str | None = None,
          ui_url: str | None = None) -> Config:
    key = api_key or os.environ.get(ENV_KEY)
    if not key:
        raise AQError("There is no AQ API key.", f"Pass api_key to init(), or set {ENV_KEY}. "
                      "Create a key in AQ under Settings, API keys.")
    address = (endpoint or os.environ.get(ENV_ENDPOINT) or "").strip().rstrip("/")
    if not address.startswith(("http://", "https://")):
        raise AQError("There is no AQ address, or it does not start with http:// or https://.",
                      f"Pass endpoint to init(), or set {ENV_ENDPOINT}, for example https://aq.example.com.")
    parts = urlsplit(address)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1") or (parts.hostname or "").endswith(".localhost")
    if parts.scheme == "http" and not local:
        raise AQError("The AQ address uses http://, so the API key would travel in clear text.",
                      "Use the https:// address. Plain http:// is allowed only for localhost.")
    ui = (ui_url or os.environ.get(ENV_UI) or address).strip().rstrip("/")
    return Config(project=project or os.environ.get(ENV_PROJECT), api_key=key, endpoint=address, ui_url=ui)


def set_config(config: Config | None) -> None:
    global _current
    _current = config


def get_config() -> Config:
    """The settings from ``init``, or from the environment when ``init`` was not called."""
    return _current if _current is not None else build()

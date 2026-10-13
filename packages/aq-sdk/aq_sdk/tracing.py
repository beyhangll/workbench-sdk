"""Send spans to AQ over OpenTelemetry, and add your own spans.

``setup`` points an OTLP exporter at ``<endpoint>/v1/otel/v1/traces`` with the
API key. If your program already created an OpenTelemetry tracer provider
(for example through Strands), the exporter is added to it. Otherwise a new
provider is installed. Traces map to a connected agent by ``service.name``,
which is the ``project`` you pass to ``init``.
"""
from __future__ import annotations

import atexit
import functools
import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import Config

TRACES_PATH = "/v1/otel/v1/traces"
_installed: set[tuple[str, str]] = set()
_provider: TracerProvider | None = None


def setup(config: Config) -> TracerProvider | None:
    """Install the exporter once per endpoint and key. Returns the provider that carries it."""
    global _provider
    marker = (config.endpoint, config.api_key)
    if marker in _installed and _provider is not None:
        return _provider
    exporter = OTLPSpanExporter(endpoint=config.endpoint + TRACES_PATH,
                                headers={"Authorization": f"Bearer {config.api_key}"})
    processor = BatchSpanProcessor(exporter)
    current = otel_trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        provider = current
    else:
        resource = Resource.create({"service.name": config.project} if config.project else {})
        provider = TracerProvider(resource=resource)
        otel_trace.set_tracer_provider(provider)
    provider.add_span_processor(processor)
    if _provider is None:
        atexit.register(flush)
    _provider, _ = provider, _installed.add(marker)
    return provider


def flush(timeout_ms: int = 10_000) -> bool:
    """Send the spans that are still waiting. Call it before a short script ends."""
    return True if _provider is None else bool(_provider.force_flush(timeout_ms))


def _tracer() -> otel_trace.Tracer:
    return otel_trace.get_tracer("aq_sdk")


def _attribute(value: Any) -> Any:
    return value if isinstance(value, (str, bool, int, float)) else str(value)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[otel_trace.Span]:
    """A custom span. An exception inside the block is recorded on the span and raised again.

        with aq_sdk.span("lookup", customer_tier="gold") as s:
            s.set_attribute("rows", 12)
    """
    with _tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, _attribute(value))
        yield current


def trace(fn: Callable[..., Any] | None = None, *, name: str | None = None, **attributes: Any):
    """Decorator that wraps a function, sync or async, in a span named after it.

        @aq_sdk.trace
        def plan(question): ...

        @aq_sdk.trace(name="search", index="faq")
        async def search(query): ...
    """
    def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
        label = name or f.__qualname__
        if inspect.iscoroutinefunction(f):
            @functools.wraps(f)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                with span(label, **attributes):
                    return await f(*args, **kwargs)
            return awrapper

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(label, **attributes):
                return f(*args, **kwargs)
        return wrapper
    return wrap(fn) if fn is not None else wrap

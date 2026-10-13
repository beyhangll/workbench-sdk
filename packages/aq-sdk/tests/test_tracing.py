from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import aq_sdk
from aq_sdk import tracing


@pytest.fixture
def memory(monkeypatch):
    """A provider that keeps spans in memory, standing in for the global one."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(otel_trace, "get_tracer", lambda name, *a, **k: provider.get_tracer(name))
    return exporter


def test_context_manager_sets_attributes_and_records_exceptions(memory):
    with aq_sdk.span("lookup", tier="gold", count=3, odd=object()) as s:
        s.set_attribute("extra", True)
    with pytest.raises(ValueError):
        with aq_sdk.span("broken"):
            raise ValueError("boom")
    ok, bad = memory.get_finished_spans()
    assert ok.name == "lookup" and ok.attributes["tier"] == "gold" and ok.attributes["count"] == 3
    assert isinstance(ok.attributes["odd"], str) and ok.attributes["extra"] is True
    assert bad.status.status_code.name == "ERROR" and bad.events[0].name == "exception"


def test_decorator_wraps_sync_async_and_keeps_the_function(memory):
    @aq_sdk.trace
    def plain(x):
        """doc"""
        return x + 1

    @aq_sdk.trace(name="searching", index="faq")
    async def fetch(x):
        return x * 2

    assert plain(1) == 2 and plain.__doc__ == "doc" and plain.__name__ == "plain"
    assert asyncio.run(fetch(4)) == 8
    names = [s.name for s in memory.get_finished_spans()]
    assert names[0].endswith("plain") and names[1] == "searching"
    assert memory.get_finished_spans()[1].attributes["index"] == "faq"


def test_init_installs_an_otlp_exporter_to_the_aq_endpoint_with_the_key(monkeypatch):
    added = []

    class Fake(TracerProvider):
        def add_span_processor(self, processor):
            added.append(processor)

    made = []

    class FakeExporter:
        def __init__(self, **kwargs):
            made.append(kwargs)

        def export(self, spans):
            from opentelemetry.sdk.trace.export import SpanExportResult
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=0):
            return True

    monkeypatch.setattr(tracing, "OTLPSpanExporter", FakeExporter)
    provider = Fake()
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(tracing, "_installed", set())
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setattr(tracing.atexit, "register", lambda fn: None)
    aq_sdk.init("my-agent", "aq_live_k", "https://aq.example/")
    aq_sdk.init("my-agent", "aq_live_k", "https://aq.example/")
    assert len(added) == 1 and len(made) == 1
    assert made[0]["endpoint"] == "https://aq.example/v1/otel/v1/traces"
    assert made[0]["headers"] == {"Authorization": "Bearer aq_live_k"}


def test_init_without_tracing_only_sets_the_config(monkeypatch):
    monkeypatch.setattr(tracing, "setup", lambda config: pytest.fail("traces were off"))
    import aq_sdk as module
    monkeypatch.setattr(module, "setup", lambda config: pytest.fail("traces were off"))
    config = aq_sdk.init("p", "k", "https://aq.example", traces=False)
    assert config.endpoint == "https://aq.example"

"""Tracing behaviour for ``DecisionModel.ask``."""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from strands.decisions import LLMDecisionModel
from strands.models.model import Model
from strands.types.content import Messages
from strands.types.event_loop import Usage

DEFAULT_USAGE: Usage = {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5}


class _OutputModel(Model):
    """A model whose ``structured_output`` optionally opens a child span before yielding the output."""

    def __init__(self, *, child_span: bool = False) -> None:
        self.child_span = child_span

    def get_config(self) -> Any:
        return {}

    def update_config(self, **model_config: Any) -> None:
        pass

    async def structured_output(
        self, output_model: type[BaseModel], prompt: Messages, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        if self.child_span:
            child = trace_api.get_tracer("test").start_span("child_in_ask")
            child.end()
        yield {"event": {"metadata": {"usage": DEFAULT_USAGE, "metrics": {"latencyMs": 1}}}}
        yield {"output": output_model(**{"q": {"answer": "true"}})}

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        raise NotImplementedError
        yield  # pragma: no cover


@pytest.fixture(scope="module")
def _provider_exporter() -> InMemorySpanExporter:
    """Attach an in-memory exporter to the global tracer provider.

    ``DecisionModel.ask`` resolves its tracer from the global provider, and the model fixture
    opens a child span the same way, so both must share one provider. ``set_tracer_provider`` is
    one-way, so this attaches to an SDK provider another test already installed and installs its
    own only when none is set; the processor only records to memory, leaving later tests
    unaffected.
    """
    exporter = InMemorySpanExporter()
    provider = trace_api.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        trace_api.set_tracer_provider(TracerProvider())
        provider = trace_api.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        pytest.fail("the global tracer provider is not an SDK TracerProvider, so these spans cannot be captured")
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
def exporter(_provider_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    _provider_exporter.clear()
    return _provider_exporter


@pytest.mark.asyncio
async def test_parents_child_spans_created_inside_ask_under_the_decision_span(exporter):
    decision = LLMDecisionModel(model=_OutputModel(child_span=True))

    await decision.ask("x", {"q": {"question": "ok?", "choices": "boolean"}})

    spans = exporter.get_finished_spans()
    decision_span = next(span for span in spans if span.name == "decision")
    child_span = next(span for span in spans if span.name == "child_in_ask")
    assert child_span.parent is not None
    assert child_span.parent.span_id == decision_span.context.span_id


@pytest.mark.asyncio
async def test_runs_when_there_is_no_active_parent_span(exporter):
    decision = LLMDecisionModel(model=_OutputModel())

    await decision.ask("x", {"q": {"question": "ok?", "choices": "boolean"}})

    spans = exporter.get_finished_spans()
    decision_span = next(span for span in spans if span.name == "decision")
    assert decision_span.parent is None

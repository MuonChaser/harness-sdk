"""Base interface for making structured "decisions" from inside the SDK."""

import abc
import json
import math
from collections.abc import Mapping
from typing import Any

import opentelemetry.context as context_api
import opentelemetry.trace as trace_api
from opentelemetry.trace import SpanKind, StatusCode

from .types import DecisionInput, DecisionResult, Question, Uncertain


class DecisionModel(abc.ABC):
    """Abstract base for anything that can answer closed questions.

    The public ``ask`` opens a ``decision`` span, validates the question map, delegates to the
    subclass ``_ask``, records usage and the answer set as span attributes/events, and closes the
    span. Every implementation is traced identically.
    """

    async def ask(self, state: DecisionInput, questions: dict[str, Question], **kwargs: Any) -> DecisionResult:
        """Ask one or more closed questions over the given state.

        Args:
            state: A plain string, a one-shot content list, or a message history.
            questions: Map of identifier to question. The returned ``answers`` dict is keyed off
                this same map.
            **kwargs: Additional keyword arguments for future extensibility, forwarded to ``_ask``.

        Returns:
            A ``DecisionResult`` whose answers are keyed by the question identifiers.

        Raises:
            ValueError: If ``questions`` is empty or contains an invalid ``question``, ``choices``,
                or ``uncertainty``.
            StructuredOutputException: If the model declines to produce a valid answer set (e.g. a
                non-tool-use response, or an omitted answer). The underlying
                ``model.structured_output`` may also raise ``pydantic.ValidationError`` when the
                model returns an option outside a question's allowed choices; it propagates unchanged.
        """
        _validate_questions(questions)

        span = trace_api.get_tracer(__name__).start_span(
            "decision", kind=SpanKind.INTERNAL, attributes={"gen_ai.operation.name": "decision"}
        )
        span.add_event("gen_ai.decision.questions", {"ids": list(questions.keys())})

        token = context_api.attach(trace_api.set_span_in_context(span))
        try:
            result = await self._ask(state, questions, **kwargs)

            attributes: dict[str, Any] = {"gen_ai.decision.latency_ms": result.metadata.latency_ms}
            if result.usage is not None:
                attributes["gen_ai.usage.input_tokens"] = result.usage["inputTokens"]
                attributes["gen_ai.usage.output_tokens"] = result.usage["outputTokens"]
            span.set_attributes(attributes)

            span.add_event("gen_ai.decision.answers", {"answers": _serialize_answers(result.answers)})
            span.set_status(StatusCode.OK)

            return result
        except Exception as error:
            span.set_status(StatusCode.ERROR, str(error))
            span.record_exception(error)
            raise
        finally:
            context_api.detach(token)
            span.end()

    @abc.abstractmethod
    async def _ask(self, state: DecisionInput, questions: dict[str, Question], **kwargs: Any) -> DecisionResult:
        """Implementation-specific decision logic.

        Must return a complete ``DecisionResult``, including ``metadata.latency_ms`` sourced from
        the underlying model provider.
        """
        ...


def _validate_questions(questions: dict[str, Question]) -> None:
    if not questions:
        raise ValueError("ask requires at least one question")

    for question_id, question in questions.items():
        question_text = question.get("question")
        if not isinstance(question_text, str) or not question_text.strip():
            raise ValueError(f"question id=<{question_id}> | question must be a non-empty string")

        choices = question.get("choices")
        if choices != "boolean":
            if not isinstance(choices, list) or len(choices) == 0:
                raise ValueError(
                    f"question id=<{question_id}> | choices must be 'boolean' or a non-empty list of strings"
                )
            if len(set(choices)) != len(choices):
                raise ValueError(f"question id=<{question_id}> | choices must be unique")
            if any(not isinstance(choice, str) or len(choice) == 0 for choice in choices):
                raise ValueError(f"question id=<{question_id}> | choices must be non-empty strings")
            if "uncertain" in choices:
                raise ValueError(
                    f"question id=<{question_id}> | choice 'uncertain' is reserved for the Uncertain answer"
                )

        uncertainty = question.get("uncertainty")
        if uncertainty is not None and not isinstance(uncertainty, bool):
            if not isinstance(uncertainty, Mapping):
                raise ValueError(
                    f"question id=<{question_id}> | uncertainty must be a boolean or {{ min_confidence? }} object"
                )
            min_confidence = uncertainty.get("min_confidence")
            if min_confidence is not None and (
                isinstance(min_confidence, bool)
                or not isinstance(min_confidence, (int, float))
                or not math.isfinite(min_confidence)
                or min_confidence < 0
                or min_confidence > 1
            ):
                raise ValueError(f"question id=<{question_id}> | uncertainty.min_confidence must be in [0.0, 1.0]")


def _uncertainty_allowed(question: Question) -> bool:
    """Resolve whether ``Uncertain`` is permitted for a question, given its ``uncertainty`` config.

    Shared by every decision-model implementation in this package.
    """
    return question.get("uncertainty") is not False


def _serialize_answers(answers: dict[str, bool | str | Uncertain]) -> str:
    """Serialize an answer map to JSON for a span event, rendering ``Uncertain`` as a tagged object."""

    def encode(value: bool | str | Uncertain) -> Any:
        if isinstance(value, Uncertain):
            return {"uncertain": True} if value.reason is None else {"uncertain": True, "reason": value.reason}
        return value

    return json.dumps({key: encode(value) for key, value in answers.items()})

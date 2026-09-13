"""Error taxonomy. Every failure the user can see maps to exactly one of these classes.

Raw exception text never reaches the client: it can contain account identifiers, SQL, or
internal paths. The user sees the template message plus a request id that also appears in logs.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    DATA_UNAVAILABLE = "data_unavailable"
    LLM_UNAVAILABLE = "llm_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    GUARDRAIL_REJECTED = "guardrail_rejected"
    SQL_INVALID = "sql_invalid"
    INTERNAL = "internal"


USER_MESSAGES: dict[ErrorKind, str] = {
    ErrorKind.DATA_UNAVAILABLE: (
        "I'm having trouble reaching the Census data in Snowflake right now, so I can't "
        "answer this yet. Please try again in a moment."
    ),
    ErrorKind.LLM_UNAVAILABLE: (
        "The language model behind this assistant is temporarily unavailable. Please try again in a moment."
    ),
    ErrorKind.BUDGET_EXHAUSTED: (
        "This question took longer than my time budget allows, so I stopped rather than guess. "
        "Try narrowing it, for example to one state or one measure."
    ),
    ErrorKind.GUARDRAIL_REJECTED: (
        "I can only answer questions about US population and demographics from the Census "
        "dataset. Try asking about population, age, income, housing, or commuting for a state "
        "or county."
    ),
    ErrorKind.SQL_INVALID: (
        "I couldn't build a valid query for that question against the Census tables. "
        "Try rephrasing, or ask what data is available for the topic."
    ),
    ErrorKind.INTERNAL: ("Something went wrong on my side while answering. It has been logged. Please try again."),
}


class AgentError(Exception):
    """Base class for failures that should be shown to the user via the taxonomy."""

    kind: ErrorKind = ErrorKind.INTERNAL

    def __init__(self, detail: str = "", *, kind: ErrorKind | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if kind is not None:
            self.kind = kind

    @property
    def user_message(self) -> str:
        return USER_MESSAGES[self.kind]


class DataUnavailableError(AgentError):
    kind = ErrorKind.DATA_UNAVAILABLE


class LLMUnavailableError(AgentError):
    kind = ErrorKind.LLM_UNAVAILABLE


class BudgetExhaustedError(AgentError):
    kind = ErrorKind.BUDGET_EXHAUSTED


class GuardrailRejectedError(AgentError):
    kind = ErrorKind.GUARDRAIL_REJECTED


class SQLInvalidError(AgentError):
    kind = ErrorKind.SQL_INVALID


def classify_exception(exc: BaseException) -> ErrorKind:
    """Map any exception to a taxonomy kind. Unknown exceptions are INTERNAL."""
    if isinstance(exc, AgentError):
        return exc.kind
    if isinstance(exc, TimeoutError):
        return ErrorKind.BUDGET_EXHAUSTED
    return ErrorKind.INTERNAL

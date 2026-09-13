from __future__ import annotations

from census_agent.errors import (
    USER_MESSAGES,
    AgentError,
    BudgetExhaustedError,
    DataUnavailableError,
    ErrorKind,
    classify_exception,
)


def test_every_kind_has_a_user_message() -> None:
    assert set(USER_MESSAGES) == set(ErrorKind)
    for msg in USER_MESSAGES.values():
        assert len(msg) > 20


def test_user_message_never_contains_internal_detail() -> None:
    err = DataUnavailableError("account IGX-123 login failed with 250001")
    assert "IGX-123" not in err.user_message
    assert err.detail.startswith("account")


def test_classify_exception() -> None:
    assert classify_exception(BudgetExhaustedError()) is ErrorKind.BUDGET_EXHAUSTED
    assert classify_exception(TimeoutError()) is ErrorKind.BUDGET_EXHAUSTED
    assert classify_exception(ValueError("x")) is ErrorKind.INTERNAL
    assert classify_exception(AgentError("x", kind=ErrorKind.SQL_INVALID)) is ErrorKind.SQL_INVALID

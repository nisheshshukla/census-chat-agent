from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from census_agent.guardrails.classifier import Classifier, Verdict


class FakeMessages:
    def __init__(self, behavior: str) -> None:
        self.behavior = behavior

    async def parse(self, **kwargs: Any) -> SimpleNamespace:
        if self.behavior == "slow":
            await asyncio.sleep(0.2)
        if self.behavior == "boom":
            raise RuntimeError("api down")
        return SimpleNamespace(parsed_output=Verdict(category="off_topic", reason="poem"))


def client(behavior: str) -> Any:
    return SimpleNamespace(messages=FakeMessages(behavior))


async def test_classifier_returns_verdict() -> None:
    v = await Classifier(client("ok"), "model").classify("write a poem", "ctx")
    assert v is not None and v.category == "off_topic"


async def test_classifier_fails_open_on_timeout_and_error() -> None:
    assert await Classifier(client("slow"), "model", timeout_s=0.01).classify("q") is None
    assert await Classifier(client("boom"), "model").classify("q") is None

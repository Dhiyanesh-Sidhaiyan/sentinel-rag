"""LLM client over any OpenAI-compatible endpoint (LangChain ChatOpenAI).

Wraps every call with: concurrency limit (bulkhead), timeout, retries (inside the SDK), metrics.
The agent treats the LLM as *optional*: each capability has a deterministic fallback, so an LLM outage
degrades answer quality instead of taking the service down.
"""

import asyncio
import time
from typing import TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import LLM_CALLS, LLM_LATENCY

T = TypeVar("T", bound=BaseModel)
log = get_logger(__name__)


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        from langchain_openai import ChatOpenAI

        assert settings.llm_api_key is not None
        self._timeout = settings.llm_timeout_s
        self._sem = asyncio.Semaphore(settings.llm_max_concurrency)
        self._model = ChatOpenAI(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            temperature=0,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )

    async def _call(self, operation: str, coro_factory):
        start = time.perf_counter()
        async with self._sem:
            try:
                result = await asyncio.wait_for(coro_factory(), timeout=self._timeout * 2)
                LLM_CALLS.labels(operation, "ok").inc()
                return result
            except Exception as exc:
                LLM_CALLS.labels(operation, "error").inc()
                log.warning("llm_call_failed", operation=operation, error=type(exc).__name__)
                raise
            finally:
                LLM_LATENCY.labels(operation).observe(time.perf_counter() - start)

    async def generate(self, system: str, user: str, operation: str = "generate") -> str:
        msgs = [SystemMessage(content=system), HumanMessage(content=user)]
        msg = await self._call(operation, lambda: self._model.ainvoke(msgs))
        return str(msg.content).strip()

    async def structured(self, system: str, user: str, schema: type[T], operation: str) -> T:
        runnable = self._model.with_structured_output(schema)
        msgs = [SystemMessage(content=system), HumanMessage(content=user)]
        return await self._call(operation, lambda: runnable.ainvoke(msgs))


def build_llm(settings: Settings) -> LLMClient | None:
    return LLMClient(settings) if settings.llm_provider == "openai" else None

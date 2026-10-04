"""Prometheus metrics. Exposed at /metrics and scraped by the ServiceMonitor."""

import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import Counter, Histogram

HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "route", "status"]
)
AGENT_NODE_LATENCY = Histogram(
    "agent_node_duration_seconds", "LangGraph node latency", ["node"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
LLM_CALLS = Counter("llm_calls_total", "LLM calls", ["operation", "outcome"])
LLM_LATENCY = Histogram("llm_call_duration_seconds", "LLM call latency", ["operation"])
GUARDRAIL_EVENTS = Counter("guardrail_events_total", "Guardrail detections", ["stage", "type"])
RETRIEVAL_RESULTS = Histogram(
    "retrieval_relevant_chunks", "Relevant chunks after grading", buckets=(0, 1, 2, 3, 5, 8, 13)
)
INGESTED_CHUNKS = Counter("ingested_chunks_total", "Chunks indexed", ["outcome"])
CACHE_EVENTS = Counter("answer_cache_total", "Answer cache lookups", ["outcome"])


@contextmanager
def observe(hist: Histogram, **labels: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        hist.labels(**labels).observe(time.perf_counter() - start)

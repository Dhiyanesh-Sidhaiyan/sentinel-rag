"""Public API contracts (request/response). Strict validation at the boundary."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DOC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------- ingestion ----------
class DocumentIn(StrictModel):
    doc_id: str = Field(pattern=DOC_ID_PATTERN, description="Stable id; re-ingesting replaces the document")
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1)
    source: str | None = Field(default=None, max_length=500)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _limit_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 20:
            raise ValueError("metadata supports at most 20 keys")
        return v


class IngestRequest(StrictModel):
    documents: list[DocumentIn] = Field(min_length=1, max_length=100)


class DocumentIngestResult(BaseModel):
    doc_id: str
    chunks_indexed: int
    chunks_quarantined: int
    triples_extracted: int
    pii_redactions: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class IngestResponse(BaseModel):
    tenant_id: str
    documents: list[DocumentIngestResult]
    total_chunks: int
    total_triples: int
    latency_ms: float


# ---------- query ----------
class QueryRequest(StrictModel):
    question: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=20)
    use_cache: bool = True
    include_trace: bool = True


class Citation(BaseModel):
    ref: int
    doc_id: str
    chunk_id: str
    title: str
    section: str | None = None
    score: float
    snippet: str


class GraphFact(BaseModel):
    subject: str
    predicate: str
    object: str
    doc_id: str


class GuardrailReport(BaseModel):
    blocked: bool = False
    block_reason: str | None = None
    input_flags: list[str] = Field(default_factory=list)
    output_flags: list[str] = Field(default_factory=list)
    pii_redacted: list[str] = Field(default_factory=list)
    injection_score: float = 0.0


class PlanReport(BaseModel):
    route: Literal["retrieve", "direct", "blocked"]
    sub_queries: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    rewrites: int = 0


class TraceStep(BaseModel):
    node: str
    ms: float
    detail: dict[str, Any] = Field(default_factory=dict)


class QueryResponse(BaseModel):
    request_id: str
    answer: str
    citations: list[Citation]
    graph_facts: list[GraphFact]
    confidence: float = Field(ge=0, le=1)
    grounded: bool
    guardrails: GuardrailReport
    plan: PlanReport
    trace: list[TraceStep] = Field(default_factory=list)
    cached: bool = False
    latency_ms: float


# ---------- misc ----------
class EntityNeighborhood(BaseModel):
    entity: str
    facts: list[GraphFact]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    checks: dict[str, str] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: str
    detail: Any | None = None
    request_id: str | None = None

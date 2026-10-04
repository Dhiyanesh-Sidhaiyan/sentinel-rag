"""Internal domain models shared by infra, services and the agent."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


@dataclass(slots=True)
class SparseVector:
    indices: list[int]
    values: list[float]


@dataclass(slots=True)
class Chunk:
    chunk_id: str
    tenant_id: str
    doc_id: str
    chunk_index: int
    title: str
    section: str | None
    text: str
    source: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class ScoredChunk:
    chunk: Chunk
    score: float
    channel: str  # dense | sparse | fused


@dataclass(slots=True, frozen=True)
class Triple:
    subject: str
    predicate: str
    object: str
    doc_id: str
    chunk_id: str


# ----- structured LLM outputs (also produced by the offline heuristics) -----
class QueryPlan(BaseModel):
    route: Literal["retrieve", "direct"] = Field(
        description="'direct' only for greetings/small-talk; everything else 'retrieve'"
    )
    sub_queries: list[str] = Field(description="1-3 self-contained search queries", max_length=3)
    entities: list[str] = Field(default_factory=list, description="Named entities mentioned in the question")


class RelevanceGrade(BaseModel):
    index: int
    relevant: bool


class RelevanceGrades(BaseModel):
    grades: list[RelevanceGrade]


class GroundingVerdict(BaseModel):
    grounded: bool
    score: float = Field(ge=0, le=1, description="Fraction of claims supported by the context")
    unsupported_claims: list[str] = Field(default_factory=list)


class ExtractedTriple(BaseModel):
    subject: str
    predicate: str
    object: str


class TripleExtraction(BaseModel):
    triples: list[ExtractedTriple] = Field(default_factory=list, max_length=30)

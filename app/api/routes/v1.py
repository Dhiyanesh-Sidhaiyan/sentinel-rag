from pathlib import PurePath

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.deps import Principal, get_container, rate_limited
from app.container import Container
from app.infra.graphstore import norm_entity
from app.schemas.api import (
    DOC_ID_PATTERN,
    DocumentIn,
    EntityNeighborhood,
    GraphFact,
    IngestRequest,
    IngestResponse,
    QueryRequest,
    QueryResponse,
)

router = APIRouter(prefix="/v1", tags=["v1"])

_ALLOWED_TYPES = {".md", ".txt", ".pdf"}


def _request_id(request: Request) -> str:
    return request.state.request_id


@router.post("/ingest", response_model=IngestResponse, status_code=status.HTTP_201_CREATED)
async def ingest(body: IngestRequest, p: Principal = Depends(rate_limited),
                 c: Container = Depends(get_container)) -> IngestResponse:
    try:
        return await c.ingestion.ingest(p.tenant_id, body.documents)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.post("/ingest/file", response_model=IngestResponse, status_code=status.HTTP_201_CREATED)
async def ingest_file(
    file: UploadFile = File(...), doc_id: str = Form(..., pattern=DOC_ID_PATTERN),
    title: str | None = Form(None, max_length=300), p: Principal = Depends(rate_limited),
    c: Container = Depends(get_container),
) -> IngestResponse:
    suffix = PurePath(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"allowed types: {sorted(_ALLOWED_TYPES)}")
    data = await file.read(c.settings.max_upload_bytes + 1)
    if len(data) > c.settings.max_upload_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
    if suffix == ".pdf":
        import io

        from pypdf import PdfReader

        try:
            text = "\n\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
        except Exception as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unreadable PDF") from exc
    else:
        text = data.decode("utf-8", errors="replace")
    if not text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no extractable text")
    doc = DocumentIn(doc_id=doc_id, title=title or PurePath(file.filename or doc_id).stem, text=text,
                     source=file.filename)
    return await c.ingestion.ingest(p.tenant_id, [doc])


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(doc_id: str, p: Principal = Depends(rate_limited),
                          c: Container = Depends(get_container)) -> None:
    await c.ingestion.delete(p.tenant_id, doc_id)


@router.post("/query", response_model=QueryResponse)
async def query(body: QueryRequest, request: Request, p: Principal = Depends(rate_limited),
                c: Container = Depends(get_container)) -> QueryResponse:
    return await c.agent.query(p.tenant_id, body.question, _request_id(request), body.top_k, body.use_cache,
                               body.include_trace)


@router.post("/query/stream", response_class=StreamingResponse)
async def query_stream(body: QueryRequest, request: Request, p: Principal = Depends(rate_limited),
                       c: Container = Depends(get_container)) -> StreamingResponse:
    return StreamingResponse(
        c.agent.stream(p.tenant_id, body.question, _request_id(request), body.top_k),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/graph/entities/{name}", response_model=EntityNeighborhood)
async def entity_neighborhood(name: str, hops: int = 1, p: Principal = Depends(rate_limited),
                              c: Container = Depends(get_container)) -> EntityNeighborhood:
    hops = max(1, min(hops, 3))
    facts = await c.graph.neighborhood(p.tenant_id, [name], hops, 100)
    if not facts:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entity not found")
    return EntityNeighborhood(
        entity=norm_entity(name),
        facts=[GraphFact(subject=f.subject, predicate=f.predicate, object=f.object, doc_id=f.doc_id) for f in facts],
    )

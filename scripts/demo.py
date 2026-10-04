"""End-to-end demo: ingest data/sample_docs, run data/sample_requests/queries.json through the real HTTP API
(in-process ASGI), and write every request/response pair to data/sample_outputs/.

    python scripts/demo.py                          # in-process, offline mode
    python scripts/demo.py --base-url http://localhost:8000 --api-key "$API_KEY"   # against a running stack
"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "data" / "sample_docs"
OUT = ROOT / "data" / "sample_outputs"


def load_documents() -> list[dict]:
    docs = []
    for path in sorted(DOCS.glob("*.md")):
        text = path.read_text()
        title = text.splitlines()[0].lstrip("# ").strip()
        docs.append({"doc_id": path.stem, "title": title, "text": text, "source": f"data/sample_docs/{path.name}",
                     "metadata": {"category": "handbook"}})
    return docs


async def run(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ingest_body = {"documents": load_documents()}
    (ROOT / "data" / "sample_requests" / "ingest_request.json").write_text(json.dumps(ingest_body, indent=2) + "\n")

    r = await client.post("/v1/ingest", json=ingest_body, headers=headers)
    r.raise_for_status()
    (OUT / "00_ingest_response.json").write_text(json.dumps(r.json(), indent=2) + "\n")
    print(f"ingested: {r.json()['total_chunks']} chunks, {r.json()['total_triples']} triples")

    for q in json.loads((ROOT / "data" / "sample_requests" / "queries.json").read_text()):
        body = {"question": q["question"], "use_cache": False}
        r = await client.post("/v1/query", json=body, headers=headers)
        r.raise_for_status()
        resp = r.json()
        (OUT / f"{q['name']}.json").write_text(json.dumps({"request": body, "response": resp}, indent=2) + "\n")
        flag = "BLOCKED" if resp["guardrails"]["blocked"] else f"conf={resp['confidence']:.2f}"
        print(f"[{q['name']}] {flag} :: {resp['answer'][:110]}")

    r = await client.get("/v1/graph/entities/Payments API", params={"hops": 2}, headers=headers)
    (OUT / "13_graph_neighborhood.json").write_text(json.dumps(r.json(), indent=2) + "\n")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    ap.add_argument("--api-key", default=os.getenv("API_KEY"))
    args = ap.parse_args()

    if args.base_url:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=60) as client:
            await run(client, {"X-API-Key": args.api_key or ""})
        return

    from app.core.config import Settings
    from app.main import create_app

    key = secrets.token_urlsafe(24)
    settings = Settings(_env_file=None, log_level="WARNING",
                        api_keys_sha256={hashlib.sha256(key.encode()).hexdigest(): "orbitly"})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as client:
            await run(client, {"X-API-Key": key})


if __name__ == "__main__":
    asyncio.run(main())

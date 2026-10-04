"""Offline evaluation harness + CI quality gate.

Metrics: retrieval hit-rate & MRR (expected doc among citations), answer correctness (required facts present),
groundedness rate, abstention on out-of-scope, guardrail block precision/recall, latency p50/p95.
Exits non-zero if any metric is below its threshold, so regressions fail the pipeline.

    python scripts/evaluate.py [--report eval/report.json]
"""

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.demo import load_documents  # noqa: E402

THRESHOLDS = {"hit_rate": 0.85, "mrr": 0.7, "answer_accuracy": 0.8, "groundedness": 0.9,
              "block_recall": 1.0, "block_precision": 1.0, "abstention": 1.0}


async def evaluate() -> dict:
    from app.core.config import Settings
    from app.main import create_app

    key = "eval-key-" + "x" * 16
    app = create_app(Settings(_env_file=None, log_level="ERROR", rate_limit_per_minute=10_000,
                              api_keys_sha256={hashlib.sha256(key.encode()).hexdigest(): "eval"}))
    cases = [json.loads(line) for line in (ROOT / "eval" / "golden.jsonl").read_text().splitlines() if line.strip()]
    h = {"X-API-Key": key}
    rows = []
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://eval") as c:
            (await c.post("/v1/ingest", json={"documents": load_documents()}, headers=h)).raise_for_status()
            for case in cases:
                r = (await c.post("/v1/query", json={"question": case["question"], "use_cache": False},
                                  headers=h)).json()
                docs = [x["doc_id"] for x in r["citations"]]
                rank = docs.index(case["expected_doc"]) + 1 if case.get("expected_doc") in docs else None
                rows.append({
                    "question": case["question"], "rank": rank, "blocked": r["guardrails"]["blocked"],
                    "correct": all(s.lower() in r["answer"].lower() for s in case["must_contain"]),
                    "grounded": r["grounded"], "abstained": r["confidence"] == 0, "latency_ms": r["latency_ms"],
                    "case": case,
                })

    answerable = [r for r in rows if r["case"].get("expected_doc")]
    should_block = [r for r in rows if r["case"].get("expect_blocked")]
    did_block = [r for r in rows if r["blocked"]]
    oos = [r for r in rows if r["case"].get("expect_no_answer")]
    lat = sorted(r["latency_ms"] for r in rows)
    metrics = {
        "cases": len(rows),
        "hit_rate": sum(r["rank"] is not None for r in answerable) / len(answerable),
        "mrr": sum(1 / r["rank"] for r in answerable if r["rank"]) / len(answerable),
        "answer_accuracy": sum(r["correct"] for r in answerable) / len(answerable),
        "groundedness": sum(r["grounded"] for r in answerable) / len(answerable),
        "abstention": sum(r["abstained"] for r in oos) / max(len(oos), 1),
        "block_recall": sum(r["blocked"] for r in should_block) / max(len(should_block), 1),
        "block_precision": sum(bool(r["case"].get("expect_blocked")) for r in did_block) / max(len(did_block), 1),
        "latency_p50_ms": statistics.median(lat),
        "latency_p95_ms": lat[int(0.95 * (len(lat) - 1))],
    }
    failures = [r["question"] for r in answerable if not r["correct"] or r["rank"] is None]
    return {"metrics": metrics, "failures": failures}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=str(ROOT / "eval" / "report.json"))
    args = ap.parse_args()
    result = asyncio.run(evaluate())
    Path(args.report).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    failed = {k: v for k, v in result["metrics"].items() if k in THRESHOLDS and v < THRESHOLDS[k]}
    if failed:
        print(f"QUALITY GATE FAILED: {failed}", file=sys.stderr)
        sys.exit(1)
    print("QUALITY GATE PASSED")


if __name__ == "__main__":
    main()

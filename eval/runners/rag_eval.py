"""RAG evaluation runner — hybrid vs dense-only comparison.

Modes:
  retrieval-only: run hybrid + dense search, report precision@1/3/5, MRR
  e2e-text:       feed question to LLM+tool, check expected_fact in response

Usage:
    python -m eval.runners.rag_eval                   # retrieval-only
    python -m eval.runners.rag_eval --e2e             # e2e text mode (needs running server)
    python -m eval.runners.rag_eval --e2e --limit 5   # first 5 cases only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Allow running as `python -m eval.runners.rag_eval` from apps/server/
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

DATASET_PATH = Path(__file__).parent.parent / "datasets" / "rag.jsonl"
REPORT_DIR = Path(__file__).parent.parent / "reports"

PASS_THRESHOLD_P3 = 0.85  # hybrid precision@3 must beat this


def load_dataset(path: Path = DATASET_PATH) -> list[dict]:
    cases = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


# ── Metrics ───────────────────────────────────────────────────────────────────

def _precision_at_k(retrieved_ids: list[str], relevant_ids: list[str], k: int) -> float:
    if not relevant_ids:
        return 1.0  # no-answer case — precision undefined, treat as pass
    top_k = retrieved_ids[:k]
    hits = sum(1 for r in top_k if r in relevant_ids)
    return hits / k


def _recall_at_k(retrieved_ids: list[str], relevant_ids: list[str], k: int) -> float:
    if not relevant_ids:
        return 1.0
    top_k = retrieved_ids[:k]
    hits = sum(1 for r in top_k if r in relevant_ids)
    return hits / len(relevant_ids)


def _mrr(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    if not relevant_ids:
        return 1.0
    for rank, rid in enumerate(retrieved_ids, start=1):
        if rid in relevant_ids:
            return 1.0 / rank
    return 0.0


# ── Retrieval-only mode ───────────────────────────────────────────────────────

def run_retrieval_eval(cases: list[dict]) -> dict[str, Any]:
    import asyncio
    from bot.tools.rag_search import search_clinic_kb, search_dense_only

    hybrid_p1 = hybrid_p3 = hybrid_p5 = hybrid_mrr = 0.0
    dense_p1 = dense_p3 = dense_p5 = dense_mrr = 0.0
    results = []

    for case in cases:
        qid = case["id"]
        question = case["question"]
        expected = case.get("expected_doc_ids", [])

        # Hybrid retrieval
        hybrid_hits = asyncio.run(search_clinic_kb(question))
        hybrid_ids = [h["doc_id"] for h in hybrid_hits]

        # Dense-only retrieval (top-5 for fair comparison)
        dense_hits = search_dense_only(question, top_n=5)
        dense_ids = [h["doc_id"] for h in dense_hits]

        hp1 = _precision_at_k(hybrid_ids, expected, 1)
        hp3 = _precision_at_k(hybrid_ids, expected, 3)
        hp5 = _precision_at_k(hybrid_ids, expected, min(5, len(hybrid_ids)))
        hmrr = _mrr(hybrid_ids, expected)

        dp1 = _precision_at_k(dense_ids, expected, 1)
        dp3 = _precision_at_k(dense_ids, expected, 3)
        dp5 = _precision_at_k(dense_ids, expected, min(5, len(dense_ids)))
        dmrr = _mrr(dense_ids, expected)

        hybrid_p1 += hp1; hybrid_p3 += hp3; hybrid_p5 += hp5; hybrid_mrr += hmrr
        dense_p1 += dp1; dense_p3 += dp3; dense_p5 += dp5; dense_mrr += dmrr

        results.append({
            "id": qid,
            "question": question,
            "expected_doc_ids": expected,
            "hybrid_retrieved": hybrid_ids,
            "dense_retrieved": dense_ids,
            "hybrid_p@3": round(hp3, 3),
            "dense_p@3": round(dp3, 3),
        })

        print(
            f"  {qid}: hybrid_p@3={hp3:.2f} dense_p@3={dp3:.2f}  "
            f"retrieved={hybrid_ids[:3]} expected={expected}"
        )

    n = len(cases)
    summary = {
        "hybrid": {
            "p@1": round(hybrid_p1 / n, 3),
            "p@3": round(hybrid_p3 / n, 3),
            "p@5": round(hybrid_p5 / n, 3),
            "mrr": round(hybrid_mrr / n, 3),
        },
        "dense_only": {
            "p@1": round(dense_p1 / n, 3),
            "p@3": round(dense_p3 / n, 3),
            "p@5": round(dense_p5 / n, 3),
            "mrr": round(dense_mrr / n, 3),
        },
        "cases": results,
    }
    return summary


# ── E2E text mode ─────────────────────────────────────────────────────────────

def run_e2e_eval(cases: list[dict], limit: int | None = None) -> dict[str, Any]:
    """Feed question directly to search_clinic_kb and check expected_fact in result text."""
    import asyncio
    from bot.tools.rag_search import search_clinic_kb

    subset = cases[:limit] if limit else cases
    passed = 0
    results = []

    for case in subset:
        if not case.get("expected_fact"):
            # No-answer case — check that results are empty or score is low
            hits = asyncio.run(search_clinic_kb(case["question"]))
            top_score = hits[0]["score"] if hits else 0
            ok = top_score < 0.3  # low confidence → bot should say it doesn't know
            passed += int(ok)
            results.append({"id": case["id"], "pass": ok, "note": f"no-answer, top_score={top_score:.3f}"})
            print(f"  {case['id']}: {'PASS' if ok else 'FAIL'} (no-answer, top_score={top_score:.3f})")
            continue

        hits = asyncio.run(search_clinic_kb(case["question"]))
        combined_text = " ".join(h["snippet"] for h in hits).lower()
        fact = case["expected_fact"].lower()
        ok = fact in combined_text
        passed += int(ok)
        results.append({
            "id": case["id"],
            "pass": ok,
            "expected_fact": fact,
            "found_in_snippet": ok,
            "top_doc_ids": [h["doc_id"] for h in hits],
        })
        print(f"  {case['id']}: {'PASS' if ok else 'FAIL'} fact='{fact}' top_docs={[h['doc_id'] for h in hits]}")

    return {
        "pass_rate": round(passed / len(subset), 3),
        "passed": passed,
        "total": len(subset),
        "cases": results,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="RAG eval runner")
    parser.add_argument("--e2e", action="store_true", help="Run E2E text mode")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of cases")
    args = parser.parse_args()

    cases = load_dataset()
    print(f"\nRAG Eval — {len(cases)} cases\n")

    t0 = time.perf_counter()

    if args.e2e:
        print("Mode: E2E text (search only, no STT/TTS)")
        result = run_e2e_eval(cases, limit=args.limit)
        pass_rate = result["pass_rate"]
        print(f"\nE2E pass rate: {pass_rate:.1%}  ({result['passed']}/{result['total']})")
    else:
        print("Mode: Retrieval-only (hybrid vs dense-only)")
        result = run_retrieval_eval(cases[:args.limit] if args.limit else cases)
        h = result["hybrid"]
        d = result["dense_only"]
        print(f"\nHybrid  P@1={h['p@1']:.3f}  P@3={h['p@3']:.3f}  P@5={h['p@5']:.3f}  MRR={h['mrr']:.3f}")
        print(f"Dense   P@1={d['p@1']:.3f}  P@3={d['p@3']:.3f}  P@5={d['p@5']:.3f}  MRR={d['mrr']:.3f}")
        pass_rate = h["p@3"]

    elapsed = time.perf_counter() - t0

    # Write report
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_path = REPORT_DIR / f"rag_{ts}.json"
    report = {
        "timestamp": ts,
        "mode": "e2e" if args.e2e else "retrieval",
        "elapsed_s": round(elapsed, 1),
        **result,
    }
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport saved: {report_path}")

    if pass_rate < PASS_THRESHOLD_P3:
        print(f"\nFAIL: hybrid P@3 {pass_rate:.3f} < threshold {PASS_THRESHOLD_P3}")
        sys.exit(1)
    else:
        print(f"\nPASS ✓  (hybrid P@3={pass_rate:.3f} ≥ {PASS_THRESHOLD_P3})")


if __name__ == "__main__":
    main()

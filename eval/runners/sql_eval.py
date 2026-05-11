"""Text-to-SQL evaluation.

Usage:
    python -m eval.runners.sql_eval [--limit N] [--execute]

For each test case in eval/datasets/text_to_sql.jsonl:
- Generates SQL via the configured LLM provider (same path as production)
- Validates SQL with sqlglot (SELECT-only, no raw tables)
- Checks expected columns and tables are referenced
- Optionally executes against live DB (--execute flag)

Exit code 1 if fewer than 90% of non-skip cases pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "apps" / "server"))

DATASET = Path(__file__).parent.parent / "datasets" / "text_to_sql.jsonl"
PASS_THRESHOLD = 0.90


def load_cases(limit: int | None = None) -> list[dict]:
    cases = []
    with open(DATASET) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    if limit:
        cases = cases[:limit]
    return cases


async def _run_case(case: dict, execute: bool) -> tuple[bool, str]:
    from bot.tools.sql_query import (
        UnsafeSQLError,
        _generate_sql,
        validate_natural_language_sql_request,
        validate_sql,
    )

    question = case["question"]
    patient_id = case.get("patient_id", "")
    expect_refuse = case.get("expect_refuse", False)
    expect_valid = case.get("expect_valid_select", True)
    expect_tables = case.get("expect_tables", [])
    expect_cols = case.get("expect_columns_contain", [])

    # sql-003 is N/A (should use check_availability tool instead)
    if case["id"] == "sql-003":
        return True, "skipped (check_availability case, not SQL)"

    full_question = question
    if patient_id:
        full_question = f"{question} (patient_id={patient_id})"

    try:
        validate_natural_language_sql_request(full_question)
    except UnsafeSQLError as exc:
        if expect_refuse:
            return True, f"correctly refused before generation: {exc}"
        return False, f"unexpected refusal before generation: {exc}"

    # Generate SQL
    try:
        raw_sql = await _generate_sql(full_question)
    except Exception as exc:
        if expect_refuse:
            return True, f"generation error (expected refuse): {exc}"
        return False, f"SQL generation failed: {exc}"

    # Validate
    try:
        sql = validate_sql(raw_sql)
    except UnsafeSQLError as exc:
        if expect_refuse:
            return True, f"correctly refused: {exc}"
        return False, f"unexpected refusal: {exc}"

    if expect_refuse:
        return False, f"expected refusal but got valid SQL: {raw_sql[:80]}"

    if not expect_valid:
        return True, "not expected to produce valid SQL — skipped"

    # Check expected tables referenced
    sql_lower = sql.lower()
    for table in expect_tables:
        if table.lower() not in sql_lower:
            return False, f"expected table/view '{table}' not found in SQL"

    # Check expected columns referenced
    for col in expect_cols:
        if col.lower() not in sql_lower:
            return False, f"expected column '{col}' not found in SQL"

    if execute:
        from db.queries import execute_safe_sql
        try:
            rows = await execute_safe_sql(sql, max_rows=5)
            return True, f"executed OK, {len(rows)} rows"
        except Exception as exc:
            return False, f"execution error: {exc}"

    return True, f"SQL valid: {sql[:80]}…"


async def run_sql_eval(limit: int | None = None, execute: bool = False) -> None:
    if execute:
        from config import get_settings
        from db.pool import init_pool

        await init_pool(get_settings().database_url)

    cases = load_cases(limit=limit)
    print(f"\nText-to-SQL eval: {len(cases)} cases  (execute={execute})\n")

    passed = 0
    skipped = 0
    failed = 0
    results = []

    for case in cases:
        ok, reason = await _run_case(case, execute=execute)
        if "skipped" in reason:
            skipped += 1
            status = "SKIP"
        elif ok:
            passed += 1
            status = "PASS"
        else:
            failed += 1
            status = "FAIL"
        results.append({"id": case["id"], "status": status, "reason": reason})
        print(f"  [{status}] {case['id']:10s}  {reason}")

    evaluated = passed + failed
    pct = passed / evaluated * 100 if evaluated else 0
    print(f"\nSQL eval: {passed}/{evaluated} passed ({pct:.0f}%)  |  {skipped} skipped\n")

    if execute:
        from db.pool import close_pool

        await close_pool()

    if evaluated > 0 and pct < PASS_THRESHOLD * 100:
        print(f"FAIL — SQL eval below threshold {PASS_THRESHOLD * 100:.0f}% (got {pct:.0f}%)")
        sys.exit(1)
    else:
        print(f"PASS — SQL eval at {pct:.0f}% >= {PASS_THRESHOLD * 100:.0f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="Text-to-SQL eval")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--execute", action="store_true", help="Actually execute SQL against DB")
    args = parser.parse_args()
    asyncio.run(run_sql_eval(limit=args.limit, execute=args.execute))


if __name__ == "__main__":
    main()

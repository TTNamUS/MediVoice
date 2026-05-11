"""query_appointments_natural — NL → safe SELECT SQL → human-readable result.

Pipeline:
  1. The configured LLM generates SQL (SELECT only, appointment_summary view only).
  2. sqlglot validates AST — rejects writes and direct patients table access.
  3. asyncpg executes against DB (max 5 rows).
  4. On syntax error → one retry with error context.
  5. Format result as short TTS-friendly string.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import sqlglot
from config import get_settings
from loguru import logger

from bot.observability.otel_setup import get_tracer
from db.queries import execute_safe_sql

tracer = get_tracer("medivoice.tools.sql_query")

# ── Schema context fed to SQL-generation LLM ─────────────────────────────────

_VIEW_SCHEMA = """
-- The ONLY table/view you may query:
CREATE VIEW appointment_summary AS
    SELECT
        a.id,                    -- UUID
        a.reason,                -- text, e.g. 'routine cleaning'
        a.status,                -- 'confirmed' | 'cancelled'
        a.created_at,            -- timestamptz
        s.start_at,              -- timestamptz — appointment start
        s.end_at,                -- timestamptz — appointment end
        d.full_name AS doctor_name,  -- e.g. 'Dr. Chris Lee'
        d.specialty,             -- 'general'|'pediatric'|'orthodontics'|'hygiene'|'emergency'
        p.id AS patient_id       -- UUID (no phone, no dob, no full_name)
    FROM appointments a
    JOIN slots s ON s.id = a.slot_id
    JOIN doctors d ON d.id = s.doctor_id
    JOIN patients p ON p.id = a.patient_id;
"""

_FEW_SHOT = """
-- Example 1
-- Q: How many appointments does Dr. Lee have this week?
SELECT COUNT(*) AS appointment_count
FROM appointment_summary
WHERE doctor_name = 'Dr. Chris Lee'
  AND start_at >= date_trunc('week', NOW())
  AND start_at  < date_trunc('week', NOW()) + interval '7 days';

-- Example 2
-- Q: What time is my next appointment? (patient_id provided via context)
SELECT start_at, doctor_name, specialty
FROM appointment_summary
WHERE patient_id = '{patient_id}'
  AND start_at > NOW()
ORDER BY start_at
LIMIT 1;

-- Example 3
-- Q: How many hygiene appointments are scheduled for next Monday?
SELECT COUNT(*) AS count
FROM appointment_summary
WHERE specialty = 'hygiene'
  AND DATE(start_at) = CURRENT_DATE + (8 - EXTRACT(DOW FROM CURRENT_DATE))::int;

-- Example 4
-- Q: Show me all of Dr. Patel's appointments today
SELECT start_at, end_at, reason, status
FROM appointment_summary
WHERE doctor_name = 'Dr. Priya Patel'
  AND DATE(start_at) = CURRENT_DATE
ORDER BY start_at;

-- Example 5
-- Q: How many confirmed appointments are there this month?
SELECT COUNT(*) AS count
FROM appointment_summary
WHERE status = 'confirmed'
  AND date_trunc('month', start_at) = date_trunc('month', CURRENT_DATE);
"""

_SQL_SYSTEM_PROMPT = f"""You are a SQL expert generating read-only queries for a dental clinic appointment system.

SCHEMA (the only table/view you may use):
{_VIEW_SCHEMA}

FEW-SHOT EXAMPLES:
{_FEW_SHOT}

RULES (never break these):
1. Return ONLY a single valid PostgreSQL SELECT statement — no explanation, no markdown.
2. Never use INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, or any write operation.
3. Never query the patients, doctors, slots, or appointments tables directly — only appointment_summary.
4. Never include patient phone numbers, dates of birth, or full names in SELECT columns.
5. Always add LIMIT 5 unless the query uses COUNT(*) or aggregation.
6. Use standard PostgreSQL syntax (date_trunc, EXTRACT, interval, etc.).
7. Today's date is {{current_date}}.
"""


class UnsafeSQLError(Exception):
    pass


# ── Validation ────────────────────────────────────────────────────────────────

_FORBIDDEN_TABLES = {"patients", "doctors", "slots", "appointments"}
_WRITE_STATEMENT_TYPES = (
    sqlglot.exp.Insert,
    sqlglot.exp.Update,
    sqlglot.exp.Delete,
    sqlglot.exp.Drop,
    sqlglot.exp.Create,
)
_UNSAFE_REQUEST_TERMS = {
    "alter",
    "create",
    "delete",
    "drop",
    "erase",
    "insert",
    "remove",
    "truncate",
    "update",
}
_PII_REQUEST_TERMS = {
    "date of birth",
    "dob",
    "full name",
    "patient name",
    "phone",
}
_RAW_TABLE_REQUEST_TERMS = {"patients", "doctors", "slots", "appointments"}


def validate_natural_language_sql_request(question: str) -> None:
    """Reject unsafe or PII-seeking requests before asking an LLM for SQL."""
    normalized = question.lower()
    words = {
        token.strip(".,;:!?()[]{}\"'")
        for token in normalized.replace("-", " ").replace("_", " ").split()
    }

    if words & _UNSAFE_REQUEST_TERMS:
        raise UnsafeSQLError("Unsafe request: write or destructive operation requested")

    mentions_raw_sql = any(keyword in words for keyword in {"select", "from", "table"})
    mentions_raw_table = any(table in words for table in _RAW_TABLE_REQUEST_TERMS)
    if mentions_raw_sql and mentions_raw_table:
        raise UnsafeSQLError("Unsafe request: direct raw-table access requested")

    if any(term in normalized for term in _PII_REQUEST_TERMS):
        raise UnsafeSQLError("Unsafe request: PII fields are not available through text-to-SQL")


def validate_sql(sql: str) -> str:
    """Parse and validate SQL. Returns cleaned SQL or raises UnsafeSQLError."""
    sql = sql.strip().rstrip(";")
    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except Exception as exc:
        raise UnsafeSQLError(f"SQL parse error: {exc}") from exc

    if not statements or len(statements) != 1:
        raise UnsafeSQLError("Expected exactly one SQL statement")

    stmt = statements[0]

    # Must be a SELECT
    if not isinstance(stmt, sqlglot.exp.Select):
        raise UnsafeSQLError(f"Only SELECT is allowed, got {type(stmt).__name__}")

    # Check for any write operations anywhere in AST
    for write_type in _WRITE_STATEMENT_TYPES:
        if stmt.find(write_type):
            raise UnsafeSQLError(f"Write operation {write_type.__name__} not allowed")

    # Check for direct access to raw tables
    for table in stmt.find_all(sqlglot.exp.Table):
        tname = table.name.lower()
        if tname in _FORBIDDEN_TABLES:
            raise UnsafeSQLError(
                f"Direct access to '{tname}' table is not allowed. Use appointment_summary view."
            )

    return sql


# ── SQL generation ────────────────────────────────────────────────────────────


def _get_llm_client() -> tuple[Any, str, str]:
    settings = get_settings()
    provider = settings.llm_provider
    model = settings.active_llm_model()

    if provider == "openai":
        from openai import OpenAI

        return OpenAI(api_key=settings.openai_api_key), provider, model
    if provider == "anthropic":
        import anthropic

        return anthropic.Anthropic(api_key=settings.anthropic_api_key), provider, model
    raise ValueError(f"SQL generation does not support LLM_PROVIDER={provider!r}")


async def _generate_sql(question: str, error_context: str = "") -> str:
    """Call the configured LLM to generate SQL for the given question."""
    client, provider, model = _get_llm_client()
    system = _SQL_SYSTEM_PROMPT.replace("{current_date}", date.today().isoformat())

    user_content = question
    if error_context:
        user_content = (
            f"{question}\n\n"
            f"Your previous SQL failed with: {error_context}\n"
            "Please fix the SQL and return only the corrected statement."
        )

    if provider == "openai":
        response = client.chat.completions.create(
            model=model,
            max_completion_tokens=256,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        )
        return (response.choices[0].message.content or "").strip()

    if provider == "anthropic":
        response = client.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip()

    raise ValueError(f"SQL generation does not support LLM_PROVIDER={provider!r}")


# ── Result formatting ─────────────────────────────────────────────────────────


def _format_result_for_voice(question: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "I didn't find any matching appointments."

    if len(rows) == 1:
        row = rows[0]
        # Aggregate result (COUNT, SUM, etc.)
        if len(row) == 1:
            val = list(row.values())[0]
            return f"The result is {val}."
        # Single row with multiple columns
        parts = [f"{k.replace('_', ' ')}: {v}" for k, v in row.items() if v is not None]
        return ". ".join(parts) + "."

    # Multiple rows — summarize
    summary_parts = []
    for i, row in enumerate(rows[:3]):
        parts = [str(v) for v in row.values() if v is not None]
        summary_parts.append(", ".join(parts))
    result = "; ".join(summary_parts)
    if len(rows) > 3:
        result += f", and {len(rows) - 3} more"
    return result + "."


# ── Tool entry point ──────────────────────────────────────────────────────────

QUERY_APPOINTMENTS_TOOL = {
    "name": "query_appointments_natural",
    "description": (
        "Answer questions about appointments using natural language. "
        "Translates the question into a safe read-only SQL query and returns the result. "
        "Examples: 'How many appointments does Dr. Lee have this week?', "
        "'What time is my next appointment?'. "
        "Never use this for booking — use book_appointment instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Natural language question about appointment data",
            },
            "patient_id": {
                "type": "string",
                "description": "Current patient UUID (optional — inject for 'my appointment' queries)",
            },
        },
        "required": ["question"],
    },
}


async def query_appointments_natural(question: str, patient_id: str = "") -> dict:
    with tracer.start_as_current_span("tool.query_appointments_natural") as span:
        span.set_attribute("tool.name", "query_appointments_natural")
        t0 = time.perf_counter()

        full_question = question
        if patient_id:
            full_question = question.replace("{patient_id}", patient_id)

        sql = ""
        try:
            try:
                validate_natural_language_sql_request(full_question)
            except UnsafeSQLError as exc:
                span.set_attribute("tool.unsafe_sql", True)
                logger.warning("Unsafe SQL request rejected: %s", exc)
                return {
                    "success": False,
                    "message": "I can't retrieve that information.",
                    "reason": "unsafe_sql",
                }

            # Step 1: Generate SQL
            raw_sql = await _generate_sql(full_question)
            logger.debug("Generated SQL: %s", raw_sql)

            # Step 2: Validate
            try:
                sql = validate_sql(raw_sql)
            except UnsafeSQLError as exc:
                span.set_attribute("tool.unsafe_sql", True)
                logger.warning("Unsafe SQL rejected: %s", exc)
                return {
                    "success": False,
                    "message": "I can't retrieve that information.",
                    "reason": "unsafe_sql",
                }

            # Step 3: Execute
            try:
                rows = await execute_safe_sql(sql, max_rows=5)
            except Exception as exec_err:
                # Step 4: Retry once with error context
                logger.warning("SQL execution failed, retrying: %s", exec_err)
                retry_sql_raw = await _generate_sql(full_question, error_context=str(exec_err))
                try:
                    sql = validate_sql(retry_sql_raw)
                    rows = await execute_safe_sql(sql, max_rows=5)
                except Exception as retry_err:
                    span.set_attribute("tool.success", False)
                    span.set_attribute("tool.error_type", "retry_failed")
                    logger.error("SQL retry also failed: %s", retry_err)
                    return {
                        "success": False,
                        "message": "I couldn't look that up right now.",
                    }

            latency_ms = (time.perf_counter() - t0) * 1000
            span.set_attribute("tool.success", True)
            span.set_attribute("tool.latency_ms", round(latency_ms, 1))
            span.set_attribute("tool.row_count", len(rows))

            voice_answer = _format_result_for_voice(question, rows)
            return {
                "success": True,
                "answer": voice_answer,
                "rows": rows,
            }

        except Exception as exc:
            span.set_attribute("tool.success", False)
            span.set_attribute("tool.error_type", type(exc).__name__)
            logger.exception("query_appointments_natural error")
            return {
                "success": False,
                "message": "I couldn't look that up right now.",
            }

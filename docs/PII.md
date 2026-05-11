# PII Handling — MediVoice

## What data flows through the system

| Data item | Source | Stored? | Logged? | In OTel spans? |
|-----------|--------|---------|---------|----------------|
| Patient full name | Voice input / DB | Postgres `patients` table | No | No |
| Patient phone | Voice input / DB | Postgres `patients` table | No | Redacted (`[PHONE]`) |
| Date of birth | Voice input / DB | Postgres `patients` table | No | Redacted (`[DOB]`) |
| Appointment reason | Voice input | Postgres `appointments` table | No | No |
| Doctor name | DB | Postgres `doctors` table | Yes (non-PII) | Yes |
| Specialty | DB | Postgres `doctors` table | Yes (non-PII) | Yes |
| Patient ID (UUID) | Generated | DB + tool results | Yes | Yes (UUID, no name) |
| Slot ID (UUID) | Generated | DB + tool results | Yes | Yes |
| Transcript text | STT | Not persisted | Debug only | No |

## PII firewall: `appointment_summary` view

The `appointment_summary` view is the only data source exposed to the **text-to-SQL** tool.
It intentionally excludes:
- `patients.phone`
- `patients.dob`
- `patients.full_name`

The SQL validator (`validate_sql` in `bot/tools/sql_query.py`) rejects any query that references the `patients`, `doctors`, `slots`, or `appointments` base tables directly.

## OTel span redaction

All span attribute values are passed through `bot/observability/redact.py` before export.
Patterns redacted:
- US phone numbers (common separators: `-`, `.`, space, parentheses)
- Date patterns (YYYY-MM-DD, MM/DD/YYYY, DD-MM-YYYY)
- Email addresses

The `MetricsBridge` stage in the pipeline applies redaction before emitting spans to the OTel Collector.

## Tool result PII policy

| Tool | What's returned | What's omitted |
|------|-----------------|----------------|
| `lookup_patient` | `patient.id`, `patient.full_name` | `phone`, `dob` |
| `check_availability` | `slot_id`, `doctor_name`, `specialty`, `start_at` | Patient data |
| `book_appointment` | `appointment_id`, `status` | Patient name, phone |
| `query_appointments_natural` | Aggregate counts or appointment times | Patient names, phones, DOBs |

## What is never stored

- Raw audio (PCM) — streamed only, not persisted.
- Full call transcripts — not written to disk or DB.
- Phone numbers in application logs — redacted to `[PHONE]`.

## Regulatory notes

This is a demo system for a portfolio project. A production deployment handling real patient data would require:
- HIPAA Business Associate Agreement (BAA) with all cloud vendors.
- Encryption at rest for the `patients` table.
- Audit logging for all access to patient records.
- Data retention and deletion policies.
- Formal privacy notice to callers.

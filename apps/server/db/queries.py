"""Typed asyncpg query helpers for MediVoice."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from db.pool import get_pool

# ── Domain models ─────────────────────────────────────────────────────────────


@dataclass
class Patient:
    id: str
    full_name: str
    phone: str | None
    dob: date | None


@dataclass
class Doctor:
    id: str
    full_name: str
    specialty: str
    languages: list[str]


@dataclass
class Slot:
    id: str
    doctor_id: str
    doctor_name: str
    specialty: str
    start_at: datetime
    end_at: datetime
    status: str


@dataclass
class Appointment:
    id: str
    slot_id: str
    patient_id: str
    reason: str | None
    status: str
    idempotency_key: str


# ── Exceptions ────────────────────────────────────────────────────────────────


class SlotAlreadyBooked(Exception):
    pass


# ── Queries ───────────────────────────────────────────────────────────────────


async def find_patient_by_phone(phone: str) -> Patient | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id, full_name, phone, dob FROM patients WHERE phone = $1",
        phone,
    )
    if row is None:
        return None
    return Patient(
        id=str(row["id"]),
        full_name=row["full_name"],
        phone=row["phone"],
        dob=row["dob"],
    )


async def create_patient(full_name: str, phone: str, dob: date | None = None) -> Patient:
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO patients (full_name, phone, dob)
        VALUES ($1, $2, $3)
        ON CONFLICT (phone) DO UPDATE SET full_name = EXCLUDED.full_name
        RETURNING id, full_name, phone, dob
        """,
        full_name,
        phone,
        dob,
    )
    return Patient(
        id=str(row["id"]),
        full_name=row["full_name"],
        phone=row["phone"],
        dob=row["dob"],
    )


async def find_available_slots(
    specialty: str,
    date_start: datetime,
    date_end: datetime,
    time_of_day: str | None = None,
    limit: int = 5,
) -> list[Slot]:
    """Return up to `limit` available slots filtered by specialty and date range."""
    pool = get_pool()

    hour_filter = ""
    if time_of_day == "morning":
        hour_filter = "AND EXTRACT(HOUR FROM s.start_at) < 12"
    elif time_of_day == "afternoon":
        hour_filter = "AND EXTRACT(HOUR FROM s.start_at) >= 12"

    rows = await pool.fetch(
        f"""
        SELECT s.id, s.doctor_id, s.start_at, s.end_at, s.status,
               d.full_name AS doctor_name, d.specialty
        FROM slots s
        JOIN doctors d ON d.id = s.doctor_id
        WHERE d.specialty ILIKE $1
          AND s.status = 'available'
          AND s.start_at BETWEEN $2 AND $3
          {hour_filter}
        ORDER BY s.start_at
        LIMIT $4
        """,
        specialty,
        date_start,
        date_end,
        limit,
    )
    return [
        Slot(
            id=str(r["id"]),
            doctor_id=str(r["doctor_id"]),
            doctor_name=r["doctor_name"],
            specialty=r["specialty"],
            start_at=r["start_at"],
            end_at=r["end_at"],
            status=r["status"],
        )
        for r in rows
    ]


async def book_slot(
    slot_id: str,
    patient_id: str,
    reason: str,
    idempotency_key: str,
) -> Appointment:
    """Book a slot in a transaction. Raises SlotAlreadyBooked on conflict."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock the slot row
            slot = await conn.fetchrow(
                "SELECT id, status FROM slots WHERE id = $1 FOR UPDATE",
                uuid.UUID(slot_id),
            )
            if slot is None or slot["status"] != "available":
                raise SlotAlreadyBooked(f"Slot {slot_id} is not available")

            # Check idempotency — return existing if already booked with same key
            existing = await conn.fetchrow(
                "SELECT id, slot_id, patient_id, reason, status, idempotency_key "
                "FROM appointments WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing:
                return Appointment(
                    id=str(existing["id"]),
                    slot_id=str(existing["slot_id"]),
                    patient_id=str(existing["patient_id"]),
                    reason=existing["reason"],
                    status=existing["status"],
                    idempotency_key=existing["idempotency_key"],
                )

            # Mark slot as booked
            await conn.execute(
                "UPDATE slots SET status = 'booked' WHERE id = $1",
                uuid.UUID(slot_id),
            )

            # Create appointment
            row = await conn.fetchrow(
                """
                INSERT INTO appointments (slot_id, patient_id, reason, idempotency_key)
                VALUES ($1, $2, $3, $4)
                RETURNING id, slot_id, patient_id, reason, status, idempotency_key
                """,
                uuid.UUID(slot_id),
                uuid.UUID(patient_id),
                reason,
                idempotency_key,
            )
            return Appointment(
                id=str(row["id"]),
                slot_id=str(row["slot_id"]),
                patient_id=str(row["patient_id"]),
                reason=row["reason"],
                status=row["status"],
                idempotency_key=row["idempotency_key"],
            )


async def execute_safe_sql(sql: str, max_rows: int = 5) -> list[dict]:
    """Execute a validated SELECT-only SQL query against the DB."""
    pool = get_pool()
    rows = await pool.fetch(sql)
    return [dict(r) for r in rows[:max_rows]]


def make_idempotency_key(slot_id: str, patient_id: str) -> str:
    return hashlib.sha256(f"{slot_id}:{patient_id}".encode()).hexdigest()[:32]

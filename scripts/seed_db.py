#!/usr/bin/env python
"""Seed Postgres with 5 doctors, ~800 slots, 10 test patients.

Usage:
    uv run python scripts/seed_db.py
    uv run python scripts/seed_db.py --no-truncate

Fixed random seed — always produces the same data.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import asyncpg

# Allow importing from apps/server
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "server"))

from config import get_settings

SEED = 42
random.seed(SEED)

UTC = timezone.utc

# ── Fixture data ──────────────────────────────────────────────────────────────

DOCTORS = [
    {"full_name": "Dr. Jamie Kim",    "specialty": "general",       "languages": ["English", "Korean"]},
    {"full_name": "Dr. Priya Patel",  "specialty": "pediatric",     "languages": ["English", "Hindi"]},
    {"full_name": "Dr. Chris Lee",    "specialty": "orthodontics",   "languages": ["English"]},
    {"full_name": "Dr. Arjun Singh",  "specialty": "hygiene",       "languages": ["English", "Punjabi"]},
    {"full_name": "Dr. Linh Nguyen",  "specialty": "emergency",     "languages": ["English", "Vietnamese"]},
]

TEST_PATIENTS = [
    {"full_name": "Alice Thompson",  "phone": "555-0101", "dob": date(1990, 3, 14)},
    {"full_name": "Bob Martinez",    "phone": "555-0102", "dob": date(1985, 7, 22)},
    {"full_name": "Carol Johnson",   "phone": "555-0103", "dob": date(1995, 11, 5)},
    {"full_name": "David Wilson",    "phone": "555-0104", "dob": date(1978, 1, 30)},
    {"full_name": "Eva Brown",       "phone": "555-0105", "dob": date(2002, 9, 18)},
    {"full_name": "Frank Davis",     "phone": "555-0106", "dob": date(1965, 4, 12)},
    {"full_name": "Grace Miller",    "phone": "555-0107", "dob": date(1998, 6, 25)},
    {"full_name": "Henry Garcia",    "phone": "555-0108", "dob": date(1972, 12, 8)},
    {"full_name": "Iris White",      "phone": "555-0109", "dob": date(1988, 2, 17)},
    {"full_name": "Jack Anderson",   "phone": "555-0110", "dob": date(2000, 8, 3)},
]


def _slot_times(weeks: int = 4) -> list[tuple[datetime, datetime]]:
    """Generate Mon–Fri 9–17 (skip 12–13) 30-min slots for N weeks from today."""
    slots = []
    today = date.today()
    # Start from next Monday
    days_until_monday = (7 - today.weekday()) % 7 or 7
    start_date = today + timedelta(days=days_until_monday)

    for week in range(weeks):
        for day in range(5):  # Mon=0 … Fri=4
            current = start_date + timedelta(weeks=week, days=day)
            for hour in range(9, 17):
                if hour == 12:  # skip lunch
                    continue
                start = datetime(current.year, current.month, current.day, hour, 0, tzinfo=UTC)
                end = start + timedelta(minutes=30)
                slots.append((start, end))
    return slots


async def seed(dsn: str, truncate: bool) -> None:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        if truncate:
            print("Truncating existing data…")
            await conn.execute(
                "TRUNCATE appointments, slots, patients, doctors RESTART IDENTITY CASCADE"
            )

        # ── Doctors ───────────────────────────────────────────────────────────
        print("Inserting doctors…")
        doctor_ids: list[str] = []
        for d in DOCTORS:
            row = await conn.fetchrow(
                """
                INSERT INTO doctors (full_name, specialty, languages)
                VALUES ($1, $2, $3)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                d["full_name"], d["specialty"], d["languages"],
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT id FROM doctors WHERE full_name = $1", d["full_name"]
                )
            doctor_ids.append(str(row["id"]))

        # ── Slots ─────────────────────────────────────────────────────────────
        print("Generating slots…")
        times = _slot_times(weeks=4)
        slot_rows: list[tuple] = []
        for doc_id in doctor_ids:
            for start, end in times:
                slot_rows.append((doc_id, start, end, "available"))

        await conn.executemany(
            """
            INSERT INTO slots (doctor_id, start_at, end_at, status)
            VALUES ($1::uuid, $2, $3, $4)
            ON CONFLICT DO NOTHING
            """,
            slot_rows,
        )
        total_slots = len(slot_rows)
        print(f"  Inserted {total_slots} slots across {len(doctor_ids)} doctors")

        # ── Pre-book ~20% of slots ────────────────────────────────────────────
        print("Inserting test patients…")
        patient_ids: list[str] = []
        for p in TEST_PATIENTS:
            row = await conn.fetchrow(
                """
                INSERT INTO patients (full_name, phone, dob)
                VALUES ($1, $2, $3)
                ON CONFLICT (phone) DO UPDATE SET full_name = EXCLUDED.full_name
                RETURNING id
                """,
                p["full_name"], p["phone"], p["dob"],
            )
            patient_ids.append(str(row["id"]))

        print("Pre-booking ~20% of slots…")
        all_slot_ids = [
            str(r["id"])
            for r in await conn.fetch(
                "SELECT id FROM slots WHERE status = 'available' ORDER BY start_at"
            )
        ]
        random.shuffle(all_slot_ids)
        to_book = all_slot_ids[: int(len(all_slot_ids) * 0.20)]

        import hashlib
        for i, slot_id in enumerate(to_book):
            patient_id = random.choice(patient_ids)
            ikey = hashlib.sha256(f"{slot_id}:{patient_id}".encode()).hexdigest()[:32]
            await conn.execute(
                "UPDATE slots SET status = 'booked' WHERE id = $1::uuid", slot_id
            )
            await conn.execute(
                """
                INSERT INTO appointments (slot_id, patient_id, reason, idempotency_key)
                VALUES ($1::uuid, $2::uuid, $3, $4)
                ON CONFLICT DO NOTHING
                """,
                slot_id, patient_id, "routine check-up", ikey,
            )

        booked_count = await conn.fetchval("SELECT COUNT(*) FROM appointments")
        available_count = await conn.fetchval(
            "SELECT COUNT(*) FROM slots WHERE status = 'available'"
        )
        print(f"  Pre-booked: {booked_count}  |  Still available: {available_count}")
        print("Seed complete.")

    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed MediVoice Postgres database")
    parser.add_argument(
        "--no-truncate", action="store_true", help="Skip TRUNCATE (append only)"
    )
    args = parser.parse_args()

    settings = get_settings()
    asyncio.run(seed(settings.database_url, truncate=not args.no_truncate))


if __name__ == "__main__":
    main()

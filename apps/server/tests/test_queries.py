"""Integration tests for db/queries.py — requires live Postgres (docker-compose).

Run:
    make up      # start Postgres
    make test
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

# Skip all tests if no DB available
pytestmark = pytest.mark.asyncio

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://medivoice:medivoice@localhost:5432/medivoice"
)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def pool():
    try:
        from db.pool import close_pool, get_pool, init_pool

        await init_pool(DATABASE_URL)
        yield get_pool()
        await close_pool()
    except Exception:
        pytest.skip("Postgres not available")


@pytest_asyncio.fixture
async def clean_test_patient(pool):
    """Create a test patient and clean up after the test."""
    phone = "555-TEST-01"
    await pool.execute("DELETE FROM patients WHERE phone = $1", phone)
    yield phone
    await pool.execute(
        "DELETE FROM appointments WHERE patient_id IN (SELECT id FROM patients WHERE phone = $1)",
        phone,
    )
    await pool.execute("DELETE FROM patients WHERE phone = $1", phone)


async def test_find_patient_not_found(pool):
    from db.queries import find_patient_by_phone

    result = await find_patient_by_phone("000-0000")
    assert result is None


async def test_create_and_find_patient(pool, clean_test_patient):
    from datetime import date

    from db.queries import create_patient, find_patient_by_phone

    phone = clean_test_patient
    patient = await create_patient("Test User", phone, date(1990, 1, 1))
    assert patient.full_name == "Test User"
    assert patient.phone == phone

    found = await find_patient_by_phone(phone)
    assert found is not None
    assert found.id == patient.id


async def test_find_available_slots_returns_list(pool):
    from db.queries import find_available_slots

    now = datetime.now(UTC)
    slots = await find_available_slots(
        specialty="general",
        date_start=now,
        date_end=now + timedelta(days=30),
        limit=5,
    )
    # May be empty if DB not seeded — just check type
    assert isinstance(slots, list)
    for s in slots:
        assert s.status == "available"
        assert s.specialty.lower() == "general"


async def test_book_slot_idempotent(pool):
    from datetime import date

    from db.queries import (
        book_slot,
        create_patient,
        find_available_slots,
        make_idempotency_key,
    )

    # Find an available slot
    now = datetime.now(UTC)
    slots = await find_available_slots(
        specialty="general",
        date_start=now,
        date_end=now + timedelta(days=30),
        limit=1,
    )
    if not slots:
        pytest.skip("No available slots (run make seed first)")

    slot = slots[0]
    patient = await create_patient("Idem Test", "555-IDEM-01", date(1985, 5, 5))
    ikey = make_idempotency_key(slot.id, patient.id)

    try:
        appt1 = await book_slot(slot.id, patient.id, "test booking", ikey)
        appt2 = await book_slot(slot.id, patient.id, "test booking", ikey)
        # Second call must return the same appointment (idempotent)
        assert appt1.id == appt2.id
    finally:
        # Clean up
        await pool.execute("DELETE FROM appointments WHERE idempotency_key = $1", ikey)
        await pool.execute("UPDATE slots SET status='available' WHERE id=$1::uuid", slot.id)
        await pool.execute("DELETE FROM patients WHERE phone='555-IDEM-01'")


async def test_book_slot_already_booked(pool):
    from datetime import date

    from db.queries import (
        SlotAlreadyBooked,
        book_slot,
        create_patient,
        find_available_slots,
        make_idempotency_key,
    )

    now = datetime.now(UTC)
    slots = await find_available_slots(
        specialty="general",
        date_start=now,
        date_end=now + timedelta(days=30),
        limit=1,
    )
    if not slots:
        pytest.skip("No available slots (run make seed first)")

    slot = slots[0]
    p1 = await create_patient("Patient A", "555-CONF-01", date(1990, 1, 1))
    p2 = await create_patient("Patient B", "555-CONF-02", date(1990, 1, 1))

    ikey1 = make_idempotency_key(slot.id, p1.id)
    ikey2 = make_idempotency_key(slot.id, p2.id)

    try:
        await book_slot(slot.id, p1.id, "first booking", ikey1)
        with pytest.raises(SlotAlreadyBooked):
            await book_slot(slot.id, p2.id, "second booking", ikey2)
    finally:
        await pool.execute(
            "DELETE FROM appointments WHERE idempotency_key IN ($1, $2)", ikey1, ikey2
        )
        await pool.execute("UPDATE slots SET status='available' WHERE id=$1::uuid", slot.id)
        await pool.execute("DELETE FROM patients WHERE phone IN ('555-CONF-01','555-CONF-02')")


async def test_execute_safe_sql(pool):
    from db.queries import execute_safe_sql

    rows = await execute_safe_sql("SELECT 1 AS n", max_rows=5)
    assert rows == [{"n": 1}]

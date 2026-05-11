"""check_availability and book_appointment tools."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from loguru import logger

from bot.observability.otel_setup import get_tracer
from db.queries import SlotAlreadyBooked, book_slot, find_available_slots, make_idempotency_key

tracer = get_tracer("medivoice.tools.appointments")


# ── Tool definitions ──────────────────────────────────────────────────────────

CHECK_AVAILABILITY_TOOL = {
    "name": "check_availability",
    "description": (
        "Check available appointment slots for a given dental specialty and preferred date range. "
        "Returns up to 5 available slots with doctor name, date, and time. "
        "Specialties: general, pediatric, orthodontics, hygiene, emergency."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "specialty": {
                "type": "string",
                "description": "Dental specialty, e.g. 'hygiene', 'orthodontics', 'general'",
            },
            "preferred_date": {
                "type": "string",
                "description": "ISO date string for the preferred date (YYYY-MM-DD), e.g. '2025-05-13'",
            },
            "time_of_day": {
                "type": "string",
                "enum": ["morning", "afternoon", "any"],
                "description": "Preferred time of day. Omit or use 'any' for no preference.",
            },
        },
        "required": ["specialty", "preferred_date"],
    },
}

BOOK_APPOINTMENT_TOOL = {
    "name": "book_appointment",
    "description": (
        "Book an appointment for a patient in an available slot. "
        "Always confirm the slot and patient details with the caller before booking. "
        "This operation is idempotent — calling it twice with the same slot and patient "
        "will not create a duplicate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slot_id": {
                "type": "string",
                "description": "UUID of the slot to book (from check_availability result)",
            },
            "patient_id": {
                "type": "string",
                "description": "UUID of the patient (from lookup_patient result)",
            },
            "reason": {
                "type": "string",
                "description": "Brief reason for the visit, e.g. 'routine cleaning'",
            },
        },
        "required": ["slot_id", "patient_id", "reason"],
    },
}


# ── Voice-friendly formatting ─────────────────────────────────────────────────


def _format_slots_for_voice(slots: list) -> str:
    """Convert slot list to a natural spoken sentence."""
    if not slots:
        return "no available slots"

    parts = []
    for s in slots:
        day = s.start_at.strftime("%A")  # "Tuesday"
        month_day = s.start_at.strftime("%B %-d")  # "May 13"  (Linux)
        hour = s.start_at.strftime("%-I %p").lower()  # "2 pm"
        parts.append(f"{day} {month_day} at {hour} with {s.doctor_name}")

    if len(parts) == 1:
        return parts[0]
    return ", or ".join([", ".join(parts[:-1]), parts[-1]])


# ── Tool handlers ─────────────────────────────────────────────────────────────


async def check_availability(
    specialty: str,
    preferred_date: str,
    time_of_day: str = "any",
) -> dict:
    with tracer.start_as_current_span("tool.check_availability") as span:
        span.set_attribute("tool.name", "check_availability")
        span.set_attribute("tool.specialty", specialty)
        t0 = time.perf_counter()
        try:
            target = datetime.fromisoformat(preferred_date).replace(tzinfo=UTC)
            # Search preferred date ± 2 days
            date_start = target - timedelta(days=2)
            date_end = target + timedelta(days=5)

            tod = time_of_day if time_of_day != "any" else None
            slots = await find_available_slots(
                specialty=specialty,
                date_start=date_start,
                date_end=date_end,
                time_of_day=tod,
                limit=5,
            )

            latency_ms = (time.perf_counter() - t0) * 1000
            span.set_attribute("tool.success", True)
            span.set_attribute("tool.latency_ms", round(latency_ms, 1))
            span.set_attribute("tool.slot_count", len(slots))

            if not slots:
                return {
                    "available": False,
                    "message": f"No available {specialty} slots near {preferred_date}.",
                    "slots": [],
                }

            slot_dicts = [
                {
                    "slot_id": s.id,
                    "doctor_name": s.doctor_name,
                    "specialty": s.specialty,
                    "start_at": s.start_at.isoformat(),
                    "end_at": s.end_at.isoformat(),
                }
                for s in slots
            ]
            voice_summary = _format_slots_for_voice(slots)
            return {
                "available": True,
                "voice_summary": f"I have {voice_summary}. Which works for you?",
                "slots": slot_dicts,
            }

        except Exception as exc:
            span.set_attribute("tool.success", False)
            span.set_attribute("tool.error_type", type(exc).__name__)
            logger.exception("check_availability error")
            return {"available": False, "error": "Could not check availability right now"}


async def book_appointment(slot_id: str, patient_id: str, reason: str) -> dict:
    with tracer.start_as_current_span("tool.book_appointment") as span:
        span.set_attribute("tool.name", "book_appointment")
        t0 = time.perf_counter()
        try:
            ikey = make_idempotency_key(slot_id, patient_id)
            appointment = await book_slot(
                slot_id=slot_id,
                patient_id=patient_id,
                reason=reason,
                idempotency_key=ikey,
            )
            latency_ms = (time.perf_counter() - t0) * 1000
            span.set_attribute("tool.success", True)
            span.set_attribute("tool.latency_ms", round(latency_ms, 1))
            logger.info("book_appointment: created appointment_id=%s", appointment.id)
            return {
                "booked": True,
                "appointment_id": appointment.id,
                "status": appointment.status,
                "message": "Your appointment is confirmed.",
            }

        except SlotAlreadyBooked:
            span.set_attribute("tool.success", False)
            span.set_attribute("tool.error_type", "SlotAlreadyBooked")
            logger.warning("book_appointment: slot already booked slot_id=%s", slot_id)
            return {
                "booked": False,
                "error": "slot_already_booked",
                "message": "That slot is no longer available. Let me find another one.",
            }
        except Exception as exc:
            span.set_attribute("tool.success", False)
            span.set_attribute("tool.error_type", type(exc).__name__)
            logger.exception("book_appointment error")
            return {"booked": False, "error": "Could not book the appointment right now"}

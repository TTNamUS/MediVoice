"""lookup_patient tool — find a patient by phone number."""

from __future__ import annotations

import time

from loguru import logger

from bot.observability.otel_setup import get_tracer
from db.queries import Patient, find_patient_by_phone

tracer = get_tracer("medivoice.tools.patient_lookup")

LOOKUP_PATIENT_TOOL = {
    "name": "lookup_patient",
    "description": (
        "Look up a registered patient by their phone number. "
        "Always read back the phone number to the caller before calling this tool. "
        "Returns patient name and ID if found, or null if not registered."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "phone": {
                "type": "string",
                "description": "Patient phone number, e.g. '555-0173'",
            }
        },
        "required": ["phone"],
    },
}


async def lookup_patient(phone: str) -> dict:
    """Tool handler: look up patient by phone number."""
    with tracer.start_as_current_span("tool.lookup_patient") as span:
        span.set_attribute("tool.name", "lookup_patient")
        t0 = time.perf_counter()
        try:
            patient: Patient | None = await find_patient_by_phone(phone)
            latency_ms = (time.perf_counter() - t0) * 1000
            span.set_attribute("tool.success", True)
            span.set_attribute("tool.latency_ms", round(latency_ms, 1))
            span.set_attribute("tool.found", patient is not None)

            if patient is None:
                logger.info("lookup_patient: not found (phone redacted)")
                return {"found": False, "patient": None}

            logger.info("lookup_patient: found patient_id=%s", patient.id)
            return {
                "found": True,
                "patient": {
                    "id": patient.id,
                    "full_name": patient.full_name,
                    # dob / phone intentionally omitted from tool result
                },
            }
        except Exception as exc:
            span.set_attribute("tool.success", False)
            span.set_attribute("tool.error_type", type(exc).__name__)
            logger.exception("lookup_patient error")
            return {"found": False, "error": "Could not look up patient at this time"}

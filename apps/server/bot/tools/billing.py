"""Billing tool stubs — mock data; no real billing system needed for demo."""

from __future__ import annotations

import time

from loguru import logger

from bot.observability.otel_setup import get_tracer

tracer = get_tracer("medivoice.tools.billing")

# Mock invoice data — keyed by patient_id prefix or invoice number
_MOCK_INVOICES = {
    "INV-001": {
        "invoice_number": "INV-001",
        "date": "2025-04-15",
        "description": "Routine cleaning + X-rays",
        "total": "$180.00",
        "insurance_covered": "$144.00",
        "patient_owes": "$36.00",
        "status": "outstanding",
    },
    "INV-002": {
        "invoice_number": "INV-002",
        "date": "2025-03-02",
        "description": "Crown installation — tooth #14",
        "total": "$1,200.00",
        "insurance_covered": "$600.00",
        "patient_owes": "$600.00",
        "status": "paid",
    },
    "INV-003": {
        "invoice_number": "INV-003",
        "date": "2025-04-28",
        "description": "Orthodontic adjustment",
        "total": "$85.00",
        "insurance_covered": "$0.00",
        "patient_owes": "$85.00",
        "status": "outstanding",
    },
}

LOOKUP_INVOICE_TOOL = {
    "name": "lookup_invoice",
    "description": (
        "Look up invoice or billing details by invoice number or patient ID. "
        "Returns amount owed, insurance coverage, and payment status. "
        "Never make up billing amounts — always call this tool first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "invoice_number": {
                "type": "string",
                "description": "Invoice number, e.g. 'INV-001' (from billing statement)",
            },
            "patient_id": {
                "type": "string",
                "description": "Patient UUID (optional — returns most recent invoice if set)",
            },
        },
        "required": [],
    },
}


async def lookup_invoice(invoice_number: str = "", patient_id: str = "") -> dict:
    with tracer.start_as_current_span("tool.lookup_invoice") as span:
        span.set_attribute("tool.name", "lookup_invoice")
        t0 = time.perf_counter()

        # Try by invoice number first
        invoice = None
        if invoice_number:
            key = invoice_number.upper()
            invoice = _MOCK_INVOICES.get(key)

        # Fall back: return most recent outstanding if patient_id provided
        if invoice is None and patient_id:
            outstanding = [v for v in _MOCK_INVOICES.values() if v["status"] == "outstanding"]
            if outstanding:
                invoice = outstanding[0]

        # Default: return INV-001 as demo fallback
        if invoice is None:
            invoice = _MOCK_INVOICES["INV-001"]

        latency_ms = (time.perf_counter() - t0) * 1000
        span.set_attribute("tool.success", True)
        span.set_attribute("tool.latency_ms", round(latency_ms, 1))
        span.set_attribute("tool.invoice_number", invoice["invoice_number"])

        logger.info("lookup_invoice: %s status=%s", invoice["invoice_number"], invoice["status"])

        voice_summary = (
            f"Invoice {invoice['invoice_number']} from {invoice['date']} "
            f"for {invoice['description']}. "
            f"Total: {invoice['total']}. "
            f"Insurance covered {invoice['insurance_covered']}, "
            f"your balance is {invoice['patient_owes']}. "
            f"Status: {invoice['status']}."
        )
        return {
            "found": True,
            "invoice": invoice,
            "voice_summary": voice_summary,
        }

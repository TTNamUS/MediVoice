"""Agent registry — defines each agent's prompt path, tools, and optional config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


@dataclass
class AgentDefinition:
    name: str
    prompt_path: Path
    tool_names: list[str]
    # Optional: override the default Cartesia voice ID for this agent
    voice_id: str | None = None

    def load_prompt_template(self) -> str:
        return self.prompt_path.read_text(encoding="utf-8")


# ── Agent definitions ─────────────────────────────────────────────────────────

AGENTS: dict[str, AgentDefinition] = {
    "triage": AgentDefinition(
        name="triage",
        prompt_path=PROMPTS_DIR / "triage.md",
        tool_names=["transfer_to"],
    ),
    "booking": AgentDefinition(
        name="booking",
        prompt_path=PROMPTS_DIR / "booking.md",
        tool_names=["lookup_patient", "check_availability", "book_appointment", "transfer_back"],
    ),
    "faq": AgentDefinition(
        name="faq",
        prompt_path=PROMPTS_DIR / "faq.md",
        tool_names=["search_clinic_kb", "transfer_back"],
    ),
    "billing": AgentDefinition(
        name="billing",
        prompt_path=PROMPTS_DIR / "billing.md",
        tool_names=["lookup_invoice", "search_clinic_kb", "transfer_to_human", "transfer_back"],
    ),
}

# All callable tool functions keyed by name — populated by orchestrator at runtime
# to avoid circular imports. Tools are registered here at import time.
TOOL_DEFINITIONS: dict[str, dict] = {}  # name → OpenAI-style tool schema


def register_tool_definition(name: str, schema: dict) -> None:
    TOOL_DEFINITIONS[name] = schema


def get_tool_schemas(tool_names: list[str]) -> list[dict]:
    return [TOOL_DEFINITIONS[n] for n in tool_names if n in TOOL_DEFINITIONS]


def _to_function_schema(schema: dict) -> FunctionSchema:
    input_schema = schema.get("input_schema", {})
    return FunctionSchema(
        name=schema["name"],
        description=schema.get("description", ""),
        properties=input_schema.get("properties", {}),
        required=input_schema.get("required", []),
    )


def get_tools_schema(tool_names: list[str]) -> ToolsSchema:
    """Return a Pipecat ToolsSchema for the named app tool schemas."""
    return ToolsSchema(
        standard_tools=[_to_function_schema(schema) for schema in get_tool_schemas(tool_names)],
    )

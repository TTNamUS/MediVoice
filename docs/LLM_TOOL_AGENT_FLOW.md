# MediVoice LLM, Tools, Agents, and Handoff Flow

This document explains how the MediVoice voice pipeline routes audio through STT,
LLM inference, tool execution, agent handoff, TTS, and back to the caller.

## Pipecat Model

Pipecat pipelines are ordered chains of frame processors. Frames carry audio,
text, context, function-call events, and control signals through the chain. In
the standard voice-agent shape, audio enters from a transport, STT produces user
text, the user context aggregator updates the LLM context, the LLM generates
text or tool calls, TTS converts text to audio, and the transport sends audio
back to the user.

Pipecat function calling has two required parts:

- Tool schema visibility: functions must be exposed to the LLM through
  `LLMContext(tools=ToolsSchema(...))`.
- Tool execution: function names must be registered on the LLM service with
  handlers that receive `FunctionCallParams` and return results via
  `params.result_callback(result)`.

In this project, `bot.agents.registry.get_tools_schema()` converts the local
tool dictionaries into Pipecat `FunctionSchema` objects and wraps them in
`ToolsSchema`. `bot.pipeline._as_pipecat_tool_handler()` adapts simple app tool
functions like `lookup_patient(phone=...)` to Pipecat's `FunctionCallParams`
handler API.

## Runtime Pipeline

Defined in `apps/server/bot/pipeline.py`:

```text
transport.input()
  -> DeepgramSTTService
  -> user_aggregator
  -> llm
  -> CartesiaTTSService
  -> MetricsBridge
  -> transport.output()
  -> assistant_aggregator
```

Stage roles:

- `transport.input()`: receives user audio from LiveKit browser WebRTC or SIP.
- `DeepgramSTTService`: converts audio frames into transcript frames.
- `user_aggregator`: appends completed user turns to the shared `LLMContext`
  and pushes an `LLMContextFrame` toward the LLM.
- `llm`: OpenAI, Anthropic, or Gemini service selected by settings. It reads
  the current `LLMContext`, including messages and visible tools.
- `CartesiaTTSService`: converts streamed LLM text frames into spoken audio.
- `MetricsBridge`: observes frames and emits session metrics.
- `transport.output()`: sends generated audio back to the caller.
- `assistant_aggregator`: records what was actually spoken and records tool
  call/result frames into context.

## Normal Conversation Flow

```text
Caller speaks
  -> LiveKit input emits audio frames
  -> Deepgram transcribes speech
  -> user_aggregator adds {"role": "user", "content": "..."} to LLMContext
  -> user_aggregator sends LLMContextFrame(context)
  -> LLM receives messages + current visible tools
  -> LLM emits response text
  -> Cartesia synthesizes response audio
  -> LiveKit output plays audio to caller
  -> assistant_aggregator adds spoken assistant text to LLMContext
```

The initial context starts as the triage agent:

```python
LLMContext(
    messages=[{"role": "system", "content": triage_prompt}],
    tools=get_tools_schema(AGENTS["triage"].tool_names),
)
```

Currently, triage only exposes `transfer_to`, so the first LLM is expected to
classify intent and route instead of answering directly.

## Tool Call Flow

When the LLM decides it needs a tool:

```text
LLM sees visible tool schema
  -> LLM returns a function/tool call name + JSON arguments
  -> Pipecat matches the function name registered by llm.register_function(...)
  -> _as_pipecat_tool_handler extracts params.arguments
  -> actual app tool runs, for example lookup_patient(phone="555-0173")
  -> handler calls params.result_callback(result)
  -> assistant_aggregator stores tool call + tool result in LLMContext
  -> Pipecat triggers another LLM pass with the updated context
  -> LLM uses the tool result to produce caller-facing text
  -> TTS speaks that text
```

Important distinction:

- `register_tool_definition(...)` stores schemas in the app registry so each
  agent can build its own visible tool list.
- `LLMContext(tools=...)` exposes schemas to the model.
- `llm.register_function(...)` registers executable handlers.

All three must be correct. If a schema is missing from `LLMContext`, the model
does not know the tool exists. If a handler is missing from `register_function`,
the model can request the tool but Pipecat cannot execute it.

## Agent Roles

Agents are configured in `apps/server/bot/agents/registry.py`.

| Agent | Prompt | Visible tools | Responsibility |
| --- | --- | --- | --- |
| `triage` | `prompts/triage.md` | `transfer_to` | Understand caller intent and route quickly. |
| `booking` | `prompts/booking.md` | `lookup_patient`, `check_availability`, `book_appointment`, `transfer_back` | Appointment scheduling flow. |
| `faq` | `prompts/faq.md` | `search_clinic_kb`, `transfer_back` | Clinic information using RAG. |
| `billing` | `prompts/billing.md` | `lookup_invoice`, `transfer_to_human`, `transfer_back` | Invoice/payment questions and escalation. |

`query_appointments_natural` is registered as an executable handler in
`pipeline.py`, but it is not currently visible to any agent because it is not in
any `AgentDefinition.tool_names`. The SQL eval suite (`make eval-sql`) passes at
100% because the eval runner invokes it directly — not through an agent. Add it
to the billing or analytics agent's `tool_names` before expecting a live caller
to trigger it.

## Tool Roles

| Tool | Used by | Purpose |
| --- | --- | --- |
| `transfer_to` | `triage` | Switch from triage to `booking`, `faq`, `billing`, or `human`. |
| `transfer_back` | specialists | Return to triage when intent changes or task is complete. |
| `transfer_to_human` | `billing` | Escalate to a live human agent. |
| `lookup_patient` | `booking` | Find registered patient by phone number. |
| `check_availability` | `booking` | Find available appointment slots by specialty/date/time preference. |
| `book_appointment` | `booking` | Reserve a slot for a known patient. |
| `search_clinic_kb` | `faq` | Search clinic knowledge base via hybrid RAG. |
| `lookup_invoice` | `billing` | Return invoice/payment details from billing data. |
| `query_appointments_natural` | not visible to any agent (eval runner only) | Convert natural-language appointment analytics questions to safe SQL. |

## Handoff Flow

The `AgentOrchestrator` in `apps/server/bot/agents/orchestrator.py` owns
session-level routing state:

- `current_agent`: active agent name.
- `running_summary`: compact context passed across handoffs.
- shared `LLMContext`: the real context used by the aggregators and LLM.

When `transfer_to(agent_name, summary)` runs:

```text
Triage LLM calls transfer_to
  -> orchestrator.transfer_to(...)
  -> _do_transfer validates target and updates running_summary
  -> _apply_agent loads target prompt
  -> first system message in LLMContext is replaced with target prompt
  -> context messages are trimmed to system + last few turns
  -> context tools are replaced with get_tools_schema(target_agent.tool_names)
  -> tool result says handoff succeeded
  -> next LLM pass uses the specialist prompt and specialist tools
```

This is a hot swap. The Pipecat pipeline is not restarted. Only the shared
`LLMContext` changes.

## Example: Booking Call

```text
Caller: "I need a cleaning next Tuesday."

1. Audio enters through LiveKit.
2. Deepgram transcribes the utterance.
3. user_aggregator appends the user message and triggers the LLM.
4. Triage prompt sees booking intent.
5. LLM calls transfer_to(agent_name="booking", summary="Caller wants a hygiene cleaning next Tuesday.")
6. Orchestrator swaps prompt/tools to booking.
7. Booking agent asks for phone number.
8. Caller gives phone number.
9. Booking agent calls lookup_patient(phone=...).
10. Tool result is added to context; LLM continues.
11. Booking agent calls check_availability(...).
12. Caller chooses a slot.
13. Booking agent calls book_appointment(...).
14. LLM confirms the appointment.
15. Cartesia speaks the confirmation.
```

## Example: FAQ Call

```text
Caller: "What are your hours?"

1. Triage routes to faq through transfer_to.
2. Orchestrator swaps to FAQ prompt and exposes search_clinic_kb.
3. FAQ agent calls search_clinic_kb(query="Sunrise Dental Clinic hours").
4. RAG tool returns relevant snippets.
5. LLM answers briefly from the returned knowledge.
6. TTS speaks the answer.
```

## Implementation Notes

- Tools are visible per-agent, not globally. Handlers are registered globally so
  any agent can execute a tool after that tool becomes visible in context.
- The model should not call hidden tools because the current `LLMContext.tools`
  only contains the active agent's schemas.
- The pipeline factory is shared by browser and SIP entrypoints, so LLM/tool
  behavior is identical for both.
- `LLMContext` currently uses a system message for agent prompts. Pipecat docs
  generally recommend `system_instruction`, but this project intentionally
  mutates the first context message to implement dynamic multi-agent handoff.

## Source References

- Pipecat pipeline and frame processing: https://docs.pipecat.ai/pipecat/learn/pipeline
- Pipecat context management: https://docs.pipecat.ai/pipecat/learn/context-management
- Pipecat function calling: https://docs.pipecat.ai/pipecat/learn/function-calling
- Pipecat LLM frames: https://docs.pipecat.ai/api-reference/server/frames/llm-frames
- Pipecat TTS placement: https://docs.pipecat.ai/pipecat/learn/text-to-speech

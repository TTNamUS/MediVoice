# Architecture — MediVoice

> Real-time voice AI receptionist for Sunrise Dental Clinic.
> Built on Pipecat (voice pipeline) + Next.js (browser client) + FastAPI (HTTP) + gRPC (metrics).

## System diagram

```
┌─────────────────────────────────────────────────────────┐
│                    Client (browser)                      │
│  Next.js 15 · livekit-client · LiveKit WebRTC           │
│  /       → LiveKitVoiceClient                           │
│  /clinic → branded LiveKitVoiceClient                   │
└───────────────────────┬─────────────────────────────────┘
                        │ POST /connect  (HTTPS)
                        │ WebRTC audio  (LiveKit room)
                        ▼
┌─────────────────────────────────────────────────────────┐
│                 Bot Server  :8000 (FastAPI)              │
│                          :50051 (gRPC)         │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │              Pipecat Pipeline                    │   │
│  │                                                  │   │
│  │  Transport.input()                               │   │
│  │       │ PCM audio                               │   │
│  │  DeepgramSTTService  (nova-3, streaming)        │   │
│  │       │ transcript                              │   │
│  │  Context aggregator (user)                      │   │
│  │       │ messages[]                              │   │
│  │  LLMService  ─────────────────────────────────┐ │   │
│  │    Anthropic claude-haiku-4-5 (default)       │ │   │
│  │    OpenAI gpt-4o-mini          (via env)      │ │   │
│  │    Google gemini-2.0-flash     (via env)      │ │   │
│  │       │ text stream            └──────────────┘ │   │
│  │  CartesiaTTSService  (sonic-2)                  │   │
│  │       │ PCM audio                               │   │
│  │  MetricsBridge  → OTel spans (ttfb_ms, tokens) │   │
│  │       │                                         │   │
│  │  Transport.output()                             │   │
│  │  Context aggregator (assistant)                 │   │
│  └──────────────────────────────────────────────┘   │
│                                                         │
└──────────┬──────────────────────┬───────────────────────┘
           │                      │
           ▼                      ▼
┌──────────────────┐   ┌──────────────────────────────┐
│  LiveKit SIP     │   │  Observability stack          │
│                  │   │  OTel Collector :4319         │
│  PSTN → WebRTC   │   │    → Jaeger  (traces)  :16686 │
│  /livekit/dispatch│   │    → Langfuse (LLM traces)   │
└──────────────────┘   └──────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────┐
│  Data layer                                          │
│  Qdrant  :6333   dense + sparse vectors (clinic KB)  │
│  Postgres :5432  patients, doctors, slots, appointments│
└──────────────────────────────────────────────────────┘
```

## Component table

| Component | Technology | Purpose |
|-----------|------------|---------|
| Browser client | Next.js 15, livekit-client, LiveKit WebRTC | Browser voice UI |
| Bot server | FastAPI, Pipecat, uvicorn | Pipeline orchestration, REST API |
| gRPC server | grpcio, grpc-reflection | Health check, metrics, eval trigger |
| STT | Deepgram nova-3 (streaming) | Lowest English TTFB |
| LLM | Anthropic / OpenAI / Gemini (configurable) | Intent + response generation |
| TTS | Cartesia Sonic-2 | ~90 ms TTFB, high naturalness |
| VAD | Silero | Turn end detection, barge-in |
| Phone transport (Wk 6) | LiveKit SIP, LivekitTransport | PSTN dial-in via Opus/WebRTC |
| Vector DB | Qdrant | Hybrid (BM25 sparse + dense) RAG |
| Relational DB | Postgres 16 | Booking, patient records |
| OTel | Collector → Jaeger + Langfuse | Distributed traces, LLM observability |
| Infra | Docker Compose | Local Qdrant, Postgres, Jaeger, OTel |
| CI | GitHub Actions | Lint + test + nightly eval |
| Deploy | Pipecat Cloud / Fly.io + Vercel | Production hosting |
| K8s | Deployment, HPA, Ingress | Kubernetes manifests for prod |

## Data flow — single voice turn

```
User speaks
  → VAD detects end-of-speech
  → PCM audio → Deepgram STT → transcript text
  → Context aggregator appends user message
  → LLM streams response tokens
  → Tool call → RAG search / DB query → result injected
  → Cartesia TTS streams audio
  → MetricsBridge records TTFB spans → OTel collector
  → Audio → LiveKit transport → User hears response
```

## Multi-agent architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Single Pipecat pipeline (never rebuilt)                     │
│                                                              │
│  AgentOrchestrator (owns agent state per session)            │
│  ├── current_agent: "triage" | "booking" | "faq" | "billing" │
│  └── running_summary: capped at 150 words                    │
│                                                              │
│  On transfer_to(agent, summary):                             │
│    1. Append summary to running_summary                      │
│    2. Swap system prompt in OpenAILLMContext.messages[0]     │
│    3. Swap tool list in OpenAILLMContext.tools               │
│    4. Trim context: keep system + last 6 non-system turns    │
│    5. Return [[AGENT:x]] marker → UI badge updates           │
└─────────────────────────────────────────────────────────────┘
```

| Agent | Tools | Prompt | Routing target |
|-------|-------|--------|----------------|
| Triage | `transfer_to` | Classify + route only | → booking / faq / billing / human |
| Booking | `lookup_patient`, `check_availability`, `book_appointment`, `transfer_back` | Scheduling flow | → triage |
| FAQ | `search_clinic_kb`, `transfer_back` | Clinic info | → triage |
| Billing | `lookup_invoice`, `transfer_to_human`, `transfer_back` | Invoice + payment | → triage or human |

**Key design**: all tool functions are registered on the LLM service once at pipeline startup. The orchestrator swaps only the context (system prompt + tool schemas visible to the LLM). This avoids pipeline restart on every handoff.

**Context cap**: after each handoff, context is trimmed to system message + last 6 turns to prevent bloat from multi-hop sessions.

## Multi-provider LLM

Configured via `LLM_PROVIDER` env var. The `_build_llm()` factory in [pipeline.py](../apps/server/bot/pipeline.py) returns the right Pipecat service; all other pipeline stages are provider-agnostic.

| Provider | Default model | Swap cost |
|----------|--------------|-----------|
| `anthropic` (default) | claude-haiku-4-5-20251001 | — |
| `openai` | gpt-4o-mini | 1 env var change |
| `gemini` | gemini-2.0-flash | 1 env var change |

Prompt caching is enabled when `LLM_PROVIDER=anthropic` (Anthropic-specific API).

## Multi-transport design

Both browser WebRTC and SIP phone paths use `LiveKitTransport` and share the same `build_pipeline()` factory. Only the room creation entry point differs. Fixing a bug in `pipeline.py` fixes both paths simultaneously — this is enforced by an inline comment in `pipeline.py` as an architectural invariant.

| | LiveKit WebRTC | LiveKit SIP |
|---|---|---|
| Entry point | POST `/connect` | POST `/livekit/dispatch` |
| Use case | Browser voice UI | PSTN phone dial-in |
| Codec | VP8/Opus (WebRTC) | Opus (WebRTC leg of SIP) |
| Audio quality | High | High (Opus > µ-law) |
| OTel tag | `transport=livekit`, `entrypoint=browser` | `transport=livekit`, `entrypoint=sip` |
| Bot token | LiveKit JWT (VideoGrants) | LiveKit JWT (VideoGrants) |
| Hangup signal | Room closed event | `participant_left` (kind=SIP) |

## Why LiveKit over Twilio Media Streams

Both can receive PSTN phone calls, but LiveKit is the better fit for this project:

| Criterion | LiveKit SIP | Twilio Media Streams |
|-----------|-------------|---------------------|
| Audio codec | Opus (WebRTC leg) — wideband, high quality | µ-law / PCMU — narrowband 8 kHz G.711 |
| Architecture | Full WebRTC media server — native room, track, participant model | WebSocket stream of raw µ-law frames — not a "framework" |
| Python SDK | `livekit`, `livekit-agents` — official, actively maintained | `twilio` SDK + manual WebSocket handling |
| Pipecat support | `LiveKitTransport` — same server transport used by browser path | Requires custom transport adapter |
| Alignment | Explicitly named in job description ("streaming frameworks such as LiveKit") | |
| Self-host | LiveKit open-source (Docker) + LiveKit Cloud | Twilio SaaS only |
| Recording / egress | Built-in room recording, egress pipelines | Requires separate recording service |
| Swap effort | `build_livekit_transport()` in `bot/transports/livekit_sip.py` | ~3 h to build custom transport adapter |

**Why Twilio was dropped**: Twilio Media Streams sends raw µ-law frames over WebSocket — useful but architecturally a "streaming sink" rather than a media framework. LiveKit is a full WebRTC media server with native SIP bridging, official Python SDK, and direct Pipecat support. It's also explicitly named in the job description.

**Swap cost back to Twilio**: ~3 h. All logic lives in `bot/transports/` and `api/livekit.py` — `pipeline.py` is untouched.

## Vector DB: why Qdrant

| Criterion | Qdrant | Pinecone | Weaviate | Milvus |
|-----------|--------|----------|----------|--------|
| Self-host | ✅ free | ❌ SaaS only | ✅ | ✅ |
| Managed cloud | ✅ | ✅ | ✅ | ✅ |
| Native sparse vectors (BM25) | ✅ built-in | ❌ requires separate BM25 service | ⚠️ plugin | ✅ |
| Hybrid search (dense + sparse) | ✅ single query | ❌ | ⚠️ separate queries + merge | ✅ |
| Named vectors (multi-vector) | ✅ | ❌ | ✅ | ✅ |
| Python SDK quality | ✅ excellent | ✅ | ✅ | ⚠️ |
| Swap effort from Qdrant | — | ~2 h | ~4 h | ~3 h |

**Why Qdrant wins**: Only vector DB with native sparse vector support + built-in hybrid search in a single query. Pinecone requires a separate BM25 service. Weaviate hybrid search requires two queries merged in application code.

All Qdrant calls go through `bot/tools/rag_search.py` — swapping to Pinecone/Weaviate touches only that file.

## Hybrid RAG pipeline

```
User query
  → Dense search: voyage-3 embedding → top-20 from "dense" named vector
  → Sparse search: BM25 sparse vector → top-20 from "sparse" named vector
  → RRF fusion: score = Σ 1/(60 + rank_i) → unified top-10
  → Cross-encoder rerank: ms-marco-MiniLM-L-6-v2 (CPU ~30ms) → top-3
  → top-3 chunks injected into LLM context as tool result
```

Eval result: hybrid P@3 = 0.883 ✓ (target ≥ 0.85). Dense-only P@3 = 0.883 on this set; hybrid advantage is clearer at P@5 (0.883 vs 0.820) and MRR (1.000 vs 0.975). See `eval/reports/eval-rag.json`.

Fallback chain:
- voyage-3 unavailable → text-embedding-3-small (OpenAI)
- cross-encoder unavailable → return RRF top-3 without reranking (logged as warning)

## Booking flow

```
Caller: "I'd like to book a cleaning next Tuesday afternoon"
  → bot calls lookup_patient(phone)
      ├── found → use patient.id
      └── not found → collect name + DOB → create patient
  → bot calls check_availability(specialty="hygiene", preferred_date="...", time_of_day="afternoon")
      → returns ≤5 slots with voice_summary
  → bot reads options ("I have Tuesday at 2 PM with Dr. Singh…")
  → caller picks → bot calls book_appointment(slot_id, patient_id, reason)
      → Postgres transaction: SELECT FOR UPDATE slot → mark booked → INSERT appointment
      → idempotency_key = sha256(slot_id:patient_id) — safe to retry
  → bot reads confirmation
```

### Text-to-SQL pipeline

```
Caller: "How many appointments does Dr. Lee have this week?"
  → bot calls query_appointments_natural(question)
      → Step 1: Claude Haiku generates SQL from appointment_summary schema + 5 few-shot examples
      → Step 2: sqlglot validates AST (SELECT-only, no raw tables, no PII columns)
           └── unsafe → return "I can't retrieve that information"
      → Step 3: asyncpg executes (max 5 rows)
           └── syntax error → retry once with error context
      → Step 4: format as TTS-friendly string
  → bot reads: "Dr. Lee has 4 appointments this week."
```

PII firewall: `appointment_summary` view excludes `patients.phone`, `patients.dob`, `patients.full_name`.
SQL validator blocks direct access to `patients`, `doctors`, `slots`, `appointments` base tables.

## Key design decisions

1. **Same pipeline for all transports**: avoids feature divergence; transport is a dependency-injected parameter.
2. **Multi-provider LLM from day 1**: recruiter / interviewer can ask "what if you used OpenAI?" — answer is "set one env var."
3. **Prompt caching on system prompt**: Anthropic charges per input token; caching the ~800-token system prompt saves ~$0.006/turn at scale.
4. **MetricsBridge as a pipeline stage**: Pipecat's `MetricsFrame` is emitted by each service; intercepting it in-band avoids external hooks and captures all timing without modifying service code.
5. **Qdrant for RAG**: only vector DB with native sparse vector support needed for BM25+dense hybrid without a separate BM25 index service.
6. **Text-to-SQL via view only**: `appointment_summary` is the PII firewall — LLM never sees raw patient data columns; `sqlglot` AST validation blocks write ops and table bypasses.
7. **Idempotent booking**: `idempotency_key = sha256(slot_id:patient_id)` lets the voice client retry on network failure without creating duplicate appointments.
8. **LiveKit over Twilio for SIP**: Opus codec (wideband) vs µ-law (narrowband), native WebRTC framework vs raw frame stream. Swap cost back to Twilio is ~3 h.
9. **Async bot spawn on session start**: LiveKit webhook requires < 5 s response; bot joins room asynchronously via `asyncio.create_task`. Browser `/connect` uses the same async spawn pattern.
